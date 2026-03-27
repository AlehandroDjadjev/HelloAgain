from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List

import numpy as np
import torch
from django.db import IntegrityError, transaction
from django.utils import timezone

from controller.models import Action, ActionAttributeScore, Attribute, MainUserProfile, UserActionEdge, UserAttributeScore
from .gnn import GraphTensors, OnlineTrainer, PreferenceGNN
from .llm_parser import QwenPromptParser
from .prompts import build_action_inventory_text, render_attribute_inventory


def clamp(value: float, low: float = -1.0, high: float = 1.0) -> float:
    return max(low, min(high, float(value)))


@dataclass
class RankedAction:
    action: Action
    score: float
    vector_similarity: float
    direct_edge: float
    gnn_score: float


class GraphService:
    def __init__(self, parser: QwenPromptParser | None = None) -> None:
        self.parser = parser or QwenPromptParser()
        self.model: PreferenceGNN | None = None
        self.trainer: OnlineTrainer | None = None

    def _ensure_model(self, attr_dim: int) -> None:
        if self.model is not None and getattr(self.model, "input_attr_dim", None) == attr_dim:
            return
        self.model = PreferenceGNN(attr_dim=attr_dim)
        self.model.input_attr_dim = attr_dim
        self.trainer = OnlineTrainer(self.model)

    def _user(self) -> MainUserProfile:
        user, _ = MainUserProfile.objects.get_or_create(name="main_user")
        return user

    def _timestamp(self) -> str:
        return timezone.now().isoformat()

    def _attribute_inventory(self, user: MainUserProfile) -> List[dict]:
        rows = {
            row.attribute.name: row.score
            for row in UserAttributeScore.objects.filter(user=user).select_related("attribute")
        }
        inventory = []
        for attr in Attribute.objects.all().order_by("name"):
            inventory.append({"name": attr.name, "score": rows.get(attr.name, 0.0)})
        return inventory

    def _action_inventory(self) -> List[dict]:
        actions = []
        for action in Action.objects.all().prefetch_related("attribute_scores__attribute").order_by("name"):
            mapping = {row.attribute.name: row.score for row in action.attribute_scores.all()}
            actions.append(
                {
                    "name": action.name,
                    "description": action.description,
                    "attribute_map": mapping,
                    "desired_attribute_map": self._action_desired_vector(action),
                }
            )
        return actions

    def _plan(self, mode: str, prompt: str) -> Dict[str, Any]:
        user = self._user()
        attr_text = render_attribute_inventory(self._attribute_inventory(user))
        if mode == "add":
            action_text = "read-only reference only in add mode; do not reuse or modify existing actions"
        else:
            action_text = build_action_inventory_text(self._action_inventory())
        plan = self.parser.parse(
            mode=mode,
            user_prompt=prompt,
            attribute_inventory_text=attr_text,
            action_inventory_text=action_text,
        )
        return self._reconcile_plan_with_inventory(user, plan)

    def _reconcile_plan_with_inventory(self, user: MainUserProfile, plan: Dict[str, Any]) -> Dict[str, Any]:
        user_state = plan.setdefault("user_state", {})
        current_scores = self._current_user_vector(user)
        existing_attribute_names = {attr.name for attr in Attribute.objects.all().only("name")}
        existing_action_names = {action.name for action in Action.objects.all().only("name")}

        raw_new_attributes = list(user_state.get("new_attributes", []))
        raw_updates = list(user_state.get("updates", []))
        update_by_name = {
            str(item.get("attribute", "")).strip().lower(): dict(item)
            for item in raw_updates
            if str(item.get("attribute", "")).strip()
        }
        prompt_context = plan.setdefault("prompt_context", {})
        raw_relevant_attributes = list(prompt_context.get("all_relevant_attributes", []))
        filtered_relevant_attributes: List[Dict[str, Any]] = []

        reconciled_new_attributes: List[Dict[str, Any]] = []
        reconciliation_notes: List[str] = list(plan.get("reconciliation_notes") or [])

        for item in raw_new_attributes:
            name = str(item.get("name", "")).strip().lower()
            if not name:
                continue
            if name in existing_attribute_names:
                if name not in update_by_name:
                    target_score = clamp(item.get("initial_score", current_scores.get(name, 0.0)))
                    if current_scores.get(name) != target_score:
                        update_by_name[name] = {
                            "attribute": name,
                            "target_score": target_score,
                            "delta": None,
                            "reason": item.get("reason") or "Reconciled existing attribute from new_attributes.",
                            "explicit_decay": False,
                        }
                reconciliation_notes.append(f"Moved existing attribute `{name}` from new_attributes to updates/active context.")
                continue
            reconciled_new_attributes.append(item)

        user_state["new_attributes"] = reconciled_new_attributes
        user_state["updates"] = list(update_by_name.values())

        for item in raw_relevant_attributes:
            name = str(item.get("attribute", "")).strip().lower()
            if not name:
                continue
            if name not in existing_attribute_names:
                reconciliation_notes.append(f"Dropped non-inventory relevant attribute `{name}`.")
                continue
            filtered_relevant_attributes.append(item)
        prompt_context["all_relevant_attributes"] = filtered_relevant_attributes

        mode = str(plan.get("mode", "")).strip().lower()
        candidate = plan.get("action_candidate") or {}
        candidate_name = str(candidate.get("name", "")).strip().lower()
        if mode == "add":
            if not candidate_name:
                raise ValueError("Invalid add-action parse: action_candidate.name is required in add mode.")
            if candidate_name in existing_action_names:
                raise ValueError(
                    f"Invalid add-action parse: action '{candidate_name}' already exists. "
                    "Add mode must create a brand new action and cannot modify an existing one."
                )

        if reconciliation_notes:
            plan["reconciliation_notes"] = reconciliation_notes
        return plan

    def _get_or_create_attribute(self, name: str, initial_score: float = 0.0) -> Attribute:
        attr, created = Attribute.objects.get_or_create(name=name)
        if created:
            user = self._user()
            UserAttributeScore.objects.get_or_create(user=user, attribute=attr, defaults={"score": clamp(initial_score)})
        return attr

    def _get_or_create_action(self, name: str, description: str = "", base_summary: str = "", prompt_text: str = "") -> Action:
        action, created = Action.objects.get_or_create(
            name=name,
            defaults={
                "description": description,
                "base_summary": base_summary,
                "created_from_prompt": prompt_text,
            },
        )
        if not created:
            changed_fields: List[str] = []
            if description and action.description != description:
                action.description = description
                changed_fields.append("description")
            if base_summary and action.base_summary != base_summary:
                action.base_summary = base_summary
                changed_fields.append("base_summary")
            if prompt_text and action.created_from_prompt != prompt_text:
                action.created_from_prompt = prompt_text
                changed_fields.append("created_from_prompt")
            if changed_fields:
                changed_fields.append("updated_at")
                action.save(update_fields=changed_fields)
        return action

    def _create_action_for_add(self, name: str, description: str = "", base_summary: str = "", prompt_text: str = "") -> Action:
        cleaned_name = name.strip().lower()
        if not cleaned_name:
            raise ValueError("Invalid add-action parse: action_candidate.name is required in add mode.")
        try:
            return Action.objects.create(
                name=cleaned_name,
                description=description,
                base_summary=base_summary,
                created_from_prompt=prompt_text,
            )
        except IntegrityError as exc:
            raise ValueError(
                f"Invalid add-action parse: action '{cleaned_name}' already exists. "
                "Add mode must create a brand new action and cannot modify an existing one."
            ) from exc

    def _ensure_user_attribute(self, user: MainUserProfile, attr: Attribute, score: float = 0.0) -> UserAttributeScore:
        row, _ = UserAttributeScore.objects.get_or_create(user=user, attribute=attr, defaults={"score": clamp(score)})
        return row

    def _ensure_action_attribute(self, action: Action, attr: Attribute, score: float = 0.0) -> ActionAttributeScore:
        row, _ = ActionAttributeScore.objects.get_or_create(action=action, attribute=attr, defaults={"score": clamp(score)})
        return row

    def _ensure_user_action_edge(self, user: MainUserProfile, action: Action) -> UserActionEdge:
        row, _ = UserActionEdge.objects.get_or_create(user=user, action=action)
        return row

    def _current_user_vector(self, user: MainUserProfile) -> Dict[str, float]:
        return {
            row.attribute.name: row.score
            for row in UserAttributeScore.objects.filter(user=user).select_related("attribute")
        }

    def _append_history(self, stack: Any, entry: Dict[str, Any]) -> List[Dict[str, Any]]:
        history = list(stack or [])
        history.append(entry)
        return history

    def _update_user_state(self, user: MainUserProfile, plan: Dict[str, Any], prompt: str, mode: str) -> Dict[str, float]:
        changed: Dict[str, float] = {}
        timestamp = self._timestamp()
        existing_rows = {
            row.attribute.name: row
            for row in UserAttributeScore.objects.filter(user=user).select_related("attribute")
        }
        created_this_request: set[str] = set()

        for attr_payload in plan.get("user_state", {}).get("new_attributes", []):
            name = attr_payload["name"].strip().lower()
            if name in existing_rows:
                continue
            attr = self._get_or_create_attribute(name, attr_payload.get("initial_score", 0.0))
            row = self._ensure_user_attribute(user, attr, attr_payload.get("initial_score", 0.0))
            row.score = clamp(attr_payload.get("initial_score", 0.0))
            row.history_stack = self._append_history(
                row.history_stack,
                {
                    "timestamp": timestamp,
                    "mode": mode,
                    "prompt": prompt,
                    "previous_score": 0.0,
                    "new_score": row.score,
                    "reason": attr_payload.get("reason", ""),
                    "kind": "new_attribute",
                },
            )
            row.save(update_fields=["score", "history_stack", "updated_at"])
            existing_rows[name] = row
            changed[name] = row.score
            created_this_request.add(name)

        for payload in plan.get("user_state", {}).get("updates", []):
            name = payload["attribute"].strip().lower()
            if name in created_this_request:
                continue
            attr = self._get_or_create_attribute(name)
            row = existing_rows.get(name) or self._ensure_user_attribute(user, attr)
            previous_score = float(row.score)

            target_score = payload.get("target_score")
            if target_score is None:
                target_score = previous_score + float(payload.get("delta") or 0.0)
            target_score = clamp(target_score)

            if target_score < previous_score and not payload.get("explicit_decay", False):
                target_score = previous_score

            if target_score == previous_score:
                existing_rows[name] = row
                continue

            row.score = target_score
            row.history_stack = self._append_history(
                row.history_stack,
                {
                    "timestamp": timestamp,
                    "mode": mode,
                    "prompt": prompt,
                    "previous_score": previous_score,
                    "new_score": target_score,
                    "reason": payload.get("reason", ""),
                    "explicit_decay": bool(payload.get("explicit_decay", False)),
                    "kind": "user_update",
                },
            )
            row.save(update_fields=["score", "history_stack", "updated_at"])
            existing_rows[name] = row
            changed[name] = row.score

        return changed

    def _prompt_attribute_state(self, plan: Dict[str, Any]) -> Dict[str, float]:
        result: Dict[str, float] = {}
        for payload in plan.get("prompt_context", {}).get("all_relevant_attributes", []):
            name = payload["attribute"].strip().lower()
            result[name] = clamp(payload.get("score", 0.0))
        if result:
            return result

        for payload in plan.get("user_state", {}).get("new_attributes", []):
            name = payload["name"].strip().lower()
            result[name] = clamp(payload.get("initial_score", 0.0))

        for payload in plan.get("user_state", {}).get("updates", []):
            name = payload["attribute"].strip().lower()
            target_score = payload.get("target_score")
            if target_score is None:
                target_score = payload.get("delta", 0.0)
            result[name] = clamp(target_score)
        return result

    def _attribute_list_to_map(self, payload: List[Dict[str, Any]] | None) -> Dict[str, float]:
        result: Dict[str, float] = {}
        for item in payload or []:
            name = str(item.get("attribute", "")).strip().lower()
            if not name:
                continue
            result[name] = clamp(item.get("score", 0.0))
        return result

    def _prompt_desired_state(self, plan: Dict[str, Any]) -> Dict[str, float]:
        return self._attribute_list_to_map(plan.get("prompt_context", {}).get("desired_attributes"))

    def _prompt_opposite_state(self, plan: Dict[str, Any]) -> Dict[str, float]:
        return self._attribute_list_to_map(plan.get("prompt_context", {}).get("opposite_attributes"))

    def _prompt_solution_state(self, plan: Dict[str, Any]) -> Dict[str, float]:
        desired_state = self._prompt_desired_state(plan)
        if desired_state:
            return desired_state
        return self._prompt_opposite_state(plan)

    def _request_state(
        self,
        current_user_state: Dict[str, float],
        prompt_state: Dict[str, float],
        changed_user_state: Dict[str, float],
    ) -> Dict[str, float]:
        request_state: Dict[str, float] = {}
        for name, score in prompt_state.items():
            request_state[name] = current_user_state.get(name, score)
        for name, score in changed_user_state.items():
            request_state[name] = score
        return request_state

    def _recompute_action_attribute_score(self, history_stack: List[Dict[str, Any]], fallback: float) -> float:
        weighted_sum = 0.0
        total_weight = 0.0
        for item in history_stack:
            weight = max(0.0, min(1.0, float(item.get("weight", 1.0))))
            score = clamp(item.get("score", 0.0))
            weighted_sum += score * weight
            total_weight += weight
        if total_weight <= 0.0:
            return fallback
        return clamp(weighted_sum / total_weight)

    def _merge_into_action(
        self,
        action: Action,
        attribute_updates: Dict[str, float],
        *,
        scale: float = 1.0,
        overlap_only: bool = False,
        source_mode: str,
        prompt: str,
        summary: str,
        relation_kind: str,
    ) -> Dict[str, float]:
        if not attribute_updates:
            return {}

        scale = max(0.0, min(1.0, float(scale)))
        if scale <= 0.0:
            return {}

        timestamp = self._timestamp()
        existing = {
            row.attribute.name: row
            for row in ActionAttributeScore.objects.filter(action=action).select_related("attribute")
        }
        applied: Dict[str, float] = {}

        for name, score in attribute_updates.items():
            if overlap_only and name not in existing:
                continue
            attr = existing[name].attribute if name in existing else self._get_or_create_attribute(name)
            row = existing.get(name) or self._ensure_action_attribute(action, attr)
            entry = {
                "timestamp": timestamp,
                "mode": source_mode,
                "prompt": prompt,
                "summary": summary,
                "relation_kind": relation_kind,
                "score": clamp(score),
                "weight": scale,
                "applied_score": clamp(score * scale),
            }
            row.history_stack = self._append_history(row.history_stack, entry)
            row.score = self._recompute_action_attribute_score(row.history_stack, fallback=row.score)
            row.contribution_count = len(row.history_stack or [])
            row.save(update_fields=["score", "contribution_count", "history_stack", "updated_at"])
            existing[name] = row
            applied[name] = row.score

        if applied:
            action.history_stack = self._append_history(
                action.history_stack,
                {
                    "timestamp": timestamp,
                    "mode": source_mode,
                    "prompt": prompt,
                    "summary": summary,
                    "relation_kind": relation_kind,
                    "scale": scale,
                    "overlap_only": overlap_only,
                    "applied_attributes": [
                        {"attribute": name, "merged_score": score} for name, score in sorted(applied.items())
                    ],
                },
            )
            action.save(update_fields=["history_stack", "updated_at"])

        return applied

    def _action_vector(self, action: Action) -> Dict[str, float]:
        return {
            row.attribute.name: row.score
            for row in ActionAttributeScore.objects.filter(action=action).select_related("attribute")
        }

    def _action_desired_vector(self, action: Action) -> Dict[str, float]:
        raw = action.desired_attribute_map or {}
        if isinstance(raw, dict):
            return {
                str(name).strip().lower(): clamp(score)
                for name, score in raw.items()
                if str(name).strip()
            }
        if isinstance(raw, list):
            return self._attribute_list_to_map(raw)
        return {}

    def _set_action_desired_attributes(
        self,
        action: Action,
        desired_attribute_updates: Dict[str, float],
        *,
        source_mode: str,
        prompt: str,
        summary: str,
    ) -> Dict[str, float]:
        desired_attribute_updates = {
            name.strip().lower(): clamp(score)
            for name, score in desired_attribute_updates.items()
            if name and str(name).strip()
        }
        if not desired_attribute_updates:
            return self._action_desired_vector(action)

        previous = self._action_desired_vector(action)
        if previous == desired_attribute_updates:
            return previous

        action.desired_attribute_map = desired_attribute_updates
        action.history_stack = self._append_history(
            action.history_stack,
            {
                "timestamp": self._timestamp(),
                "mode": source_mode,
                "prompt": prompt,
                "summary": summary,
                "relation_kind": "desired_attribute_update",
                "previous_desired_attributes": previous,
                "desired_attributes": desired_attribute_updates,
            },
        )
        action.save(update_fields=["desired_attribute_map", "history_stack", "updated_at"])
        return desired_attribute_updates

    def _action_attribute_history(self, action: Action) -> Dict[str, List[Dict[str, Any]]]:
        return {
            row.attribute.name: list(row.history_stack or [])
            for row in ActionAttributeScore.objects.filter(action=action).select_related("attribute")
        }

    def _similarity(self, left: Dict[str, float], right: Dict[str, float]) -> float:
        keys = sorted(set(left.keys()) | set(right.keys()))
        if not keys:
            return 0.0
        l = np.array([left.get(key, 0.0) for key in keys], dtype=np.float32)
        r = np.array([right.get(key, 0.0) for key in keys], dtype=np.float32)
        l_norm = np.linalg.norm(l)
        r_norm = np.linalg.norm(r)
        if l_norm == 0.0 or r_norm == 0.0:
            return 0.0
        return float(np.dot(l, r) / (l_norm * r_norm))

    def _update_direct_edge(
        self,
        user: MainUserProfile,
        action: Action,
        *,
        signal_strength: float,
        kind: str,
        reason: str,
        prompt: str,
        mode: str,
    ) -> None:
        edge = self._ensure_user_action_edge(user, action)
        signal_strength = clamp(signal_strength)
        edge.signal_history = self._append_history(
            edge.signal_history,
            {
                "timestamp": self._timestamp(),
                "mode": mode,
                "prompt": prompt,
                "kind": kind,
                "strength": signal_strength,
                "reason": reason,
            },
        )
        edge.score = clamp(edge.score * 0.78 + signal_strength * 0.22)
        edge.confidence = max(0.0, min(1.0, edge.confidence * 0.9 + 0.1))
        edge.touch_count += 1
        edge.last_signal_kind = kind
        edge.save(update_fields=["score", "confidence", "touch_count", "last_signal_kind", "signal_history", "updated_at"])

    def _build_graph_tensors(self) -> GraphTensors:
        user = self._user()
        attributes = list(Attribute.objects.all().order_by("name"))
        actions = list(Action.objects.all().order_by("name"))

        attr_names = [attr.name for attr in attributes]
        action_names = [action.name for action in actions]
        attr_index = {name: idx for idx, name in enumerate(attr_names)}
        action_index = {name: idx for idx, name in enumerate(action_names)}

        attr_dim = max(512, len(attr_names) or 1)
        user_vector = torch.zeros(attr_dim, dtype=torch.float32)
        for row in UserAttributeScore.objects.filter(user=user).select_related("attribute"):
            idx = attr_index.get(row.attribute.name)
            if idx is not None:
                user_vector[idx] = float(row.score)

        action_matrix = torch.zeros((max(len(actions), 1), attr_dim), dtype=torch.float32)
        if not actions:
            action_names = ["__no_action__"]
        else:
            for row in ActionAttributeScore.objects.select_related("attribute", "action"):
                a_idx = action_index.get(row.action.name)
                attr_idx = attr_index.get(row.attribute.name)
                if a_idx is not None and attr_idx is not None:
                    action_matrix[a_idx, attr_idx] = float(row.score)

        user_action_weights = torch.zeros(max(len(actions), 1), dtype=torch.float32)
        if actions:
            for row in UserActionEdge.objects.filter(user=user).select_related("action"):
                idx = action_index.get(row.action.name)
                if idx is not None:
                    user_action_weights[idx] = float(row.score) * float(row.confidence)

        user_attr_weights = torch.zeros(attr_dim, dtype=torch.float32)
        for row in UserAttributeScore.objects.filter(user=user).select_related("attribute"):
            idx = attr_index.get(row.attribute.name)
            if idx is not None:
                user_attr_weights[idx] = abs(float(row.score)) * float(row.confidence)

        action_attr_weights = torch.zeros((max(len(actions), 1), attr_dim), dtype=torch.float32)
        if actions:
            for row in ActionAttributeScore.objects.select_related("attribute", "action"):
                a_idx = action_index.get(row.action.name)
                attr_idx = attr_index.get(row.attribute.name)
                if a_idx is not None and attr_idx is not None:
                    action_attr_weights[a_idx, attr_idx] = abs(float(row.score))

        graph = GraphTensors(
            user_vector=user_vector,
            action_matrix=action_matrix,
            user_action_weights=user_action_weights,
            user_attr_weights=user_attr_weights,
            action_attr_weights=action_attr_weights,
            attribute_names=attr_names + [f"__padding_{i}__" for i in range(attr_dim - len(attr_names))],
            action_names=action_names,
        )
        self._ensure_model(attr_dim)
        return graph

    def _rank_actions(self) -> List[RankedAction]:
        actions = list(Action.objects.all().order_by("name"))
        if not actions:
            return []
        graph = self._build_graph_tensors()
        if self.model is None:
            return []
        self.model.eval()
        with torch.no_grad():
            outputs = self.model(graph)
        gnn_scores = outputs["scores"].detach().cpu().numpy().tolist()[: len(actions)]
        user = self._user()
        user_vec = self._current_user_vector(user)
        direct = {row.action_id: row.score for row in UserActionEdge.objects.filter(user=user)}

        ranked: List[RankedAction] = []
        for idx, action in enumerate(actions):
            vec = self._action_vector(action)
            sim = self._similarity(user_vec, vec)
            direct_edge = float(direct.get(action.id, 0.0))
            gnn_score = float(gnn_scores[idx])
            final_score = 0.5 * gnn_score + 0.35 * sim + 0.15 * direct_edge
            ranked.append(
                RankedAction(
                    action=action,
                    score=final_score,
                    vector_similarity=sim,
                    direct_edge=direct_edge,
                    gnn_score=gnn_score,
                )
            )
        ranked.sort(key=lambda item: item.score, reverse=True)
        return ranked

    def _train_positive(self, action: Action) -> None:
        actions = list(Action.objects.all().order_by("name"))
        if not actions:
            return
        graph = self._build_graph_tensors()
        try:
            positive_index = [item.name for item in actions].index(action.name)
        except ValueError:
            return
        if self.trainer is not None:
            self.trainer.train_positive_edge(graph, positive_index=positive_index, epochs=8)

    def _top_candidates_payload(self, ranked: List[RankedAction], request_state: Dict[str, float]) -> List[Dict[str, Any]]:
        return [
            {
                "name": item.action.name,
                "score": item.score,
                "gnn_score": item.gnn_score,
                "vector_similarity": item.vector_similarity,
                "direct_edge": item.direct_edge,
                "request_similarity": self._similarity(request_state, self._action_vector(item.action)),
            }
            for item in ranked[:10]
        ]

    def _rank_fetch_actions(
        self,
        request_state: Dict[str, float],
        solution_state: Dict[str, float],
    ) -> List[Dict[str, Any]]:
        ranked = self._rank_actions()
        enriched: List[Dict[str, Any]] = []
        for item in ranked:
            action_vector = self._action_vector(item.action)
            desired_vector = self._action_desired_vector(item.action)
            request_similarity = self._similarity(request_state, action_vector)
            solution_similarity = self._similarity(solution_state, desired_vector) if solution_state and desired_vector else 0.0
            combined_fetch_score = item.score + 0.10 * request_similarity + 0.18 * solution_similarity
            enriched.append(
                {
                    "ranked": item,
                    "request_similarity": request_similarity,
                    "solution_similarity": solution_similarity,
                    "combined_fetch_score": combined_fetch_score,
                }
            )
        enriched.sort(key=lambda item: item["combined_fetch_score"], reverse=True)
        return enriched

    def _top_fetch_candidates_payload(self, ranked: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        payload: List[Dict[str, Any]] = []
        for item in ranked[:10]:
            base: RankedAction = item["ranked"]
            payload.append(
                {
                    "name": base.action.name,
                    "score": item["combined_fetch_score"],
                    "history_score": base.score,
                    "gnn_score": base.gnn_score,
                    "vector_similarity": base.vector_similarity,
                    "direct_edge": base.direct_edge,
                    "request_similarity": item["request_similarity"],
                    "solution_similarity": item["solution_similarity"],
                }
            )
        return payload

    def _action_payload(self, action: Action, user: MainUserProfile) -> Dict[str, Any]:
        edge = UserActionEdge.objects.filter(user=user, action=action).first()
        return {
            "name": action.name,
            "description": action.description,
            "base_summary": action.base_summary,
            "attributes": self._action_vector(action),
            "desired_attributes": self._action_desired_vector(action),
            "attribute_history": self._action_attribute_history(action),
            "history_stack": list(action.history_stack or []),
            "direct_edge": edge.score if edge else 0.0,
            "signal_history": list(edge.signal_history or []) if edge else [],
        }

    def _record_user_interaction(
        self,
        user: MainUserProfile,
        *,
        mode: str,
        prompt: str,
        plan: Dict[str, Any],
        changed_user_state: Dict[str, float],
        request_state: Dict[str, float],
        selected_action: str | None = None,
        background_updates: List[Dict[str, Any]] | None = None,
    ) -> None:
        entry = {
            "timestamp": self._timestamp(),
            "mode": mode,
            "prompt": prompt,
            "summary": plan.get("summary", ""),
            "description_append": plan.get("user_profile_update", {}).get("description_append", ""),
            "active_state_summary": plan.get("user_profile_update", {}).get("active_state_summary", ""),
            "prompt_attributes": plan.get("prompt_context", {}).get("all_relevant_attributes", []),
            "desired_attributes": plan.get("prompt_context", {}).get("desired_attributes", []),
            "opposite_attributes": plan.get("prompt_context", {}).get("opposite_attributes", []),
            "changed_user_state": changed_user_state,
            "request_state": request_state,
            "selected_action": selected_action,
            "background_updates": background_updates or [],
        }
        user.state_history = self._append_history(user.state_history, entry)

        description_seed = (
            plan.get("user_profile_update", {}).get("description_append")
            or plan.get("user_profile_update", {}).get("active_state_summary")
            or plan.get("summary", "")
        ).strip()
        if description_seed:
            existing = user.description.strip()
            if not existing:
                user.description = description_seed
            elif description_seed.lower() not in existing.lower():
                user.description = f"{description_seed} | {existing}"
        user.save(update_fields=["description", "state_history", "updated_at"])

    def _user_payload(self, user: MainUserProfile) -> Dict[str, Any]:
        return {
            "name": user.name,
            "description": user.description,
            "attributes": [
                {
                    "name": row.attribute.name,
                    "score": row.score,
                    "history_stack": list(row.history_stack or []),
                }
                for row in UserAttributeScore.objects.filter(user=user).select_related("attribute").order_by("attribute__name")
            ],
        }

    @transaction.atomic
    def add_action_flow(self, prompt: str) -> Dict[str, Any]:
        user = self._user()
        plan = self._plan("add", prompt)
        changed_user_state = self._update_user_state(user, plan, prompt, mode="add")
        current_user_state = self._current_user_vector(user)
        prompt_state = self._prompt_attribute_state(plan)
        request_state = self._request_state(current_user_state, prompt_state, changed_user_state)

        candidate = plan.get("action_candidate") or {}
        action_name = (candidate.get("name") or "unnamed action").strip().lower()
        action = self._create_action_for_add(
            name=action_name,
            description=candidate.get("description", ""),
            base_summary=plan.get("summary", ""),
            prompt_text=prompt,
        )
        action.hit_count += 1
        action.save(update_fields=["hit_count", "updated_at"])

        candidate_desired_map = self._attribute_list_to_map(candidate.get("desired_attribute_map"))
        if not candidate_desired_map:
            candidate_desired_map = self._prompt_solution_state(plan)
        self._set_action_desired_attributes(
            action,
            candidate_desired_map,
            source_mode="add",
            prompt=prompt,
            summary=plan.get("summary", ""),
        )

        candidate_map = {
            item["attribute"].strip().lower(): clamp(item.get("score", 0.0))
            for item in candidate.get("attribute_map", [])
        }
        full_action_update = {**request_state, **changed_user_state, **candidate_map}
        self._merge_into_action(
            action,
            full_action_update,
            scale=1.0,
            overlap_only=False,
            source_mode="add",
            prompt=prompt,
            summary=plan.get("summary", ""),
            relation_kind="direct_add",
        )

        background_updates: List[Dict[str, Any]] = []
        new_vector = self._action_vector(action)
        for other in Action.objects.exclude(id=action.id):
            other_vector = self._action_vector(other)
            similarity = max(0.0, self._similarity(new_vector, other_vector))
            if similarity <= 0.0:
                continue
            overlap_payload = {name: score for name, score in request_state.items() if name in other_vector}
            if not overlap_payload:
                continue
            applied = self._merge_into_action(
                other,
                overlap_payload,
                scale=similarity,
                overlap_only=True,
                source_mode="add",
                prompt=prompt,
                summary=plan.get("summary", ""),
                relation_kind="background_from_new_action",
            )
            if applied:
                background_updates.append(
                    {
                        "action": other.name,
                        "relevance": similarity,
                        "applied_attributes": applied,
                    }
                )

        signal = plan.get("edge_signal") or {}
        self._update_direct_edge(
            user,
            action,
            signal_strength=signal.get("strength", candidate.get("wanted_strength", 0.7)),
            kind=signal.get("kind", "desire"),
            reason=signal.get("reason", ""),
            prompt=prompt,
            mode="add",
        )
        self._record_user_interaction(
            user,
            mode="add",
            prompt=prompt,
            plan=plan,
            changed_user_state=changed_user_state,
            request_state=request_state,
            selected_action=action.name,
            background_updates=background_updates,
        )
        self._train_positive(action)

        return {
            "mode": "add",
            "plan": plan,
            "attribute_catalog": self._attribute_inventory(user),
            "user": self._user_payload(user),
            "action": self._action_payload(action, user),
            "changed_user_state": changed_user_state,
            "request_state": request_state,
            "background_updates": background_updates,
        }

    @transaction.atomic
    def fetch_action_flow(self, prompt: str) -> Dict[str, Any]:
        user = self._user()
        plan = self._plan("fetch", prompt)
        changed_user_state = self._update_user_state(user, plan, prompt, mode="fetch")
        current_user_state = self._current_user_vector(user)
        prompt_state = self._prompt_attribute_state(plan)
        request_state = self._request_state(current_user_state, prompt_state, changed_user_state)
        solution_state = self._prompt_solution_state(plan)
        ranked = self._rank_fetch_actions(request_state, solution_state)
        if not ranked:
            self._record_user_interaction(
                user,
                mode="fetch",
                prompt=prompt,
                plan=plan,
                changed_user_state=changed_user_state,
                request_state=request_state,
                selected_action=None,
                background_updates=[],
            )
            return {
                "mode": "fetch",
                "plan": plan,
                "attribute_catalog": self._attribute_inventory(user),
                "user": self._user_payload(user),
                "result": None,
                "message": "No actions available yet.",
                "changed_user_state": changed_user_state,
                "request_state": request_state,
                "top_candidates": [],
                "background_updates": [],
                "solution_state": solution_state,
            }

        chosen_item = ranked[0]
        chosen_ranked: RankedAction = chosen_item["ranked"]
        chosen = chosen_ranked.action
        chosen.hit_count += 1
        chosen.save(update_fields=["hit_count", "updated_at"])
        self._merge_into_action(
            chosen,
            request_state,
            scale=1.0,
            overlap_only=False,
            source_mode="fetch",
            prompt=prompt,
            summary=plan.get("summary", ""),
            relation_kind="chosen_fetch",
        )

        signal = plan.get("edge_signal") or {}
        self._update_direct_edge(
            user,
            chosen,
            signal_strength=signal.get("strength", 0.65),
            kind=signal.get("kind", "fetch"),
            reason=signal.get("reason", ""),
            prompt=prompt,
            mode="fetch",
        )

        background_updates: List[Dict[str, Any]] = []
        for item in ranked[1:]:
            ranked_action: RankedAction = item["ranked"]
            other_vector = self._action_vector(ranked_action.action)
            relevance = max(0.0, self._similarity(request_state, other_vector))
            if relevance <= 0.0 or relevance >= 1.0:
                continue
            overlap_payload = {name: score for name, score in request_state.items() if name in other_vector}
            if not overlap_payload:
                continue
            applied = self._merge_into_action(
                ranked_action.action,
                overlap_payload,
                scale=relevance,
                overlap_only=True,
                source_mode="fetch",
                prompt=prompt,
                summary=plan.get("summary", ""),
                relation_kind="background_fetch",
            )
            if applied:
                background_updates.append(
                    {
                        "action": ranked_action.action.name,
                        "relevance": relevance,
                        "applied_attributes": applied,
                    }
                )

        self._record_user_interaction(
            user,
            mode="fetch",
            prompt=prompt,
            plan=plan,
            changed_user_state=changed_user_state,
            request_state=request_state,
            selected_action=chosen.name,
            background_updates=background_updates,
        )
        self._train_positive(chosen)

        return {
            "mode": "fetch",
            "plan": plan,
            "attribute_catalog": self._attribute_inventory(user),
            "user": self._user_payload(user),
            "result": {
                "name": chosen.name,
                "score": chosen_item["combined_fetch_score"],
                "history_score": chosen_ranked.score,
                "gnn_score": chosen_ranked.gnn_score,
                "vector_similarity": chosen_ranked.vector_similarity,
                "direct_edge": chosen_ranked.direct_edge,
                "request_similarity": chosen_item["request_similarity"],
                "solution_similarity": chosen_item["solution_similarity"],
                "action": self._action_payload(chosen, user),
            },
            "changed_user_state": changed_user_state,
            "request_state": request_state,
            "solution_state": solution_state,
            "top_candidates": self._top_fetch_candidates_payload(ranked),
            "background_updates": background_updates,
        }

    @transaction.atomic
    def conversation_flow(self, prompt: str) -> Dict[str, Any]:
        user = self._user()
        plan = self._plan("conversation", prompt)
        changed_user_state = self._update_user_state(user, plan, prompt, mode="conversation")
        current_user_state = self._current_user_vector(user)
        prompt_state = self._prompt_attribute_state(plan)
        request_state = self._request_state(current_user_state, prompt_state, changed_user_state)
        ranked = self._rank_actions()

        background_updates: List[Dict[str, Any]] = []
        for item in ranked:
            action_vector = self._action_vector(item.action)
            relevance = max(0.0, self._similarity(request_state, action_vector))
            if relevance <= 0.0 or relevance >= 1.0:
                continue
            overlap_payload = {name: score for name, score in request_state.items() if name in action_vector}
            if not overlap_payload:
                continue
            applied = self._merge_into_action(
                item.action,
                overlap_payload,
                scale=relevance,
                overlap_only=True,
                source_mode="conversation",
                prompt=prompt,
                summary=plan.get("summary", ""),
                relation_kind="background_conversation",
            )
            if applied:
                background_updates.append(
                    {
                        "action": item.action.name,
                        "relevance": relevance,
                        "applied_attributes": applied,
                    }
                )

        self._record_user_interaction(
            user,
            mode="conversation",
            prompt=prompt,
            plan=plan,
            changed_user_state=changed_user_state,
            request_state=request_state,
            selected_action=None,
            background_updates=background_updates,
        )

        return {
            "mode": "conversation",
            "plan": plan,
            "attribute_catalog": self._attribute_inventory(user),
            "user": self._user_payload(user),
            "changed_user_state": changed_user_state,
            "request_state": request_state,
            "background_updates": background_updates,
            "top_candidates": self._top_candidates_payload(ranked, request_state),
        }

    def export_state(self) -> Dict[str, Any]:
        user = self._user()
        ranked = self._rank_actions()
        return {
            "attribute_catalog": self._attribute_inventory(user),
            "user": {
                "name": user.name,
                "description": user.description,
                "state_history": list(user.state_history or []),
                "attributes": [
                    {
                        "name": row.attribute.name,
                        "score": row.score,
                        "history_stack": list(row.history_stack or []),
                    }
                    for row in UserAttributeScore.objects.filter(user=user).select_related("attribute").order_by("attribute__name")
                ],
            },
            "actions": [
                self._action_payload(action, user)
                for action in Action.objects.all().order_by("name")
            ],
            "ranking": [
                {
                    "name": item.action.name,
                    "score": item.score,
                    "gnn_score": item.gnn_score,
                    "vector_similarity": item.vector_similarity,
                    "direct_edge": item.direct_edge,
                }
                for item in ranked[:20]
            ],
        }

    @transaction.atomic
    def reset_state(self) -> Dict[str, Any]:
        UserActionEdge.objects.all().delete()
        ActionAttributeScore.objects.all().delete()
        UserAttributeScore.objects.all().delete()
        Action.objects.all().delete()
        Attribute.objects.all().delete()
        MainUserProfile.objects.all().delete()
        self.model = None
        self.trainer = None
        return self.export_state()
