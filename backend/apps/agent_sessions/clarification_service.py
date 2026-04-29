from __future__ import annotations

import json
import logging
import uuid
from typing import Optional

from apps.agent_core.llm_client import LLMClient, LLMError
from apps.audit_log.models import AuditActor, AuditEventType
from apps.audit_log.services import AuditService

from .models import AgentSession, SessionStatus
from .services import SessionService

logger = logging.getLogger(__name__)

_ORDINAL_ALIASES: dict[int, tuple[str, ...]] = {
    0: ("first", "1st", "one", "number one", "the first one", "option one"),
    1: ("second", "2nd", "two", "number two", "the second one", "option two"),
    2: ("third", "3rd", "three", "number three", "the third one", "option three"),
    3: ("fourth", "4th", "four", "number four", "the fourth one", "option four"),
    4: ("fifth", "5th", "five", "number five", "the fifth one", "option five"),
}
_NON_ANSWER_PHRASES = frozenset({
    "yes", "yeah", "yep", "ok", "okay", "sure", "continue", "go ahead",
    "no", "nope", "cancel", "stop", "skip",
})
_EMPTY_REPLY_SENTINELS = frozenset({"__silence__", "[silence]", "[no reply]"})


class ClarificationService:
    @staticmethod
    def create_pending_query(
        *,
        session: AgentSession,
        params: dict,
        screen_state: Optional[dict] = None,
        reasoning: str = "",
    ) -> dict:
        existing = dict(session.pending_user_input or {})
        if (
            existing.get("query_id")
            and existing.get("status") in {"pending", "retryable"}
            and session.status == SessionStatus.AWAITING_USER_INPUT
        ):
            return existing

        question = str(params.get("question") or "").strip()
        required_fields = [
            str(item).strip()
            for item in (params.get("required_fields") or [])
            if str(item).strip()
        ]
        candidates = [
            str(item).strip()
            for item in (params.get("candidates") or [])
            if str(item).strip()
        ]
        max_attempts = max(1, min(int(params.get("max_attempts") or 3), 3))
        candidate_records = _make_candidate_records(candidates)
        payload = {
            "query_id": uuid.uuid4().hex,
            "status": "pending",
            "question": question,
            "followup_question": question,
            "attempt_count": 1,
            "max_attempts": max_attempts,
            "required_fields": required_fields,
            "candidates": candidates,
            "candidate_records": candidate_records,
            "ui_context": _build_ui_context(screen_state, candidates=candidates),
            "reason": str(params.get("reason") or "").strip(),
            "last_user_reply": "",
            "why_unresolved": "",
            "reason_unresolved": "",
            "fallback_mode": "",
            "entity_updates": {},
            "resolved_values": {},
            "missing_fields": list(required_fields),
            "matched_candidate_id": "",
            "reasoning": reasoning[:500],
        }
        session.pending_user_input = payload
        session.save(update_fields=["pending_user_input", "updated_at"])
        SessionService.transition(session, SessionStatus.AWAITING_USER_INPUT)
        logger.info(
            "Clarification requested: session=%s query=%s attempt=%s/%s reason=%s question=%s",
            session.id,
            payload["query_id"],
            payload["attempt_count"],
            payload["max_attempts"],
            payload["reason"] or "unspecified",
            payload["question"],
        )
        AuditService.record(
            session=session,
            event_type=AuditEventType.USER_INPUT_REQUESTED,
            actor=AuditActor.SYSTEM,
            payload={
                "query_id": payload["query_id"],
                "question": payload["question"],
                "required_fields": payload["required_fields"],
                "candidates": payload["candidates"],
                "attempt_count": payload["attempt_count"],
                "max_attempts": payload["max_attempts"],
                "reason": payload["reason"],
            },
        )
        return payload

    @staticmethod
    def submit_reply(
        *,
        session: AgentSession,
        query_id: str,
        transcript: str,
        source: str = "voice",
    ) -> dict:
        pending = dict(session.pending_user_input or {})
        if session.status != SessionStatus.AWAITING_USER_INPUT:
            raise ValueError("Session is not awaiting user input.")
        if not pending or not pending.get("query_id"):
            raise ValueError("No pending user-input query exists for this session.")
        if str(pending.get("query_id")) != str(query_id):
            raise ValueError("Submitted query_id does not match the current pending query.")

        clean_transcript = " ".join(str(transcript or "").split()).strip()
        pending["last_user_reply"] = clean_transcript
        session.pending_user_input = pending
        session.save(update_fields=["pending_user_input", "updated_at"])
        AuditService.record(
            session=session,
            event_type=AuditEventType.USER_INPUT_RECEIVED,
            actor=AuditActor.USER,
            payload={
                "query_id": pending["query_id"],
                "source": source,
                "reply": clean_transcript[:500],
                "attempt_count": pending.get("attempt_count", 1),
            },
        )

        reflection = ClarificationService.reflect_reply(
            session=session,
            pending_query=pending,
            transcript=clean_transcript,
        )
        if reflection["resolved"]:
            logger.info(
                "Clarification resolved: session=%s query=%s matched_candidate=%s resolved_values=%s",
                session.id,
                pending["query_id"],
                reflection.get("matched_candidate_id") or "",
                reflection.get("resolved_values") or {},
            )
            merged_entities = dict(session.entities or {})
            merged_entities.update(reflection["resolved_values"])
            session.entities = merged_entities
            session.pending_user_input = {}
            session.save(update_fields=["entities", "pending_user_input", "updated_at"])
            SessionService.transition(session, SessionStatus.EXECUTING)
            AuditService.record(
                session=session,
                event_type=AuditEventType.USER_INPUT_RESOLVED,
                actor=AuditActor.SYSTEM,
                payload={
                    "query_id": pending["query_id"],
                    "entity_updates": reflection["resolved_values"],
                    "matched_candidate_id": reflection.get("matched_candidate_id", ""),
                },
            )
            return {
                "status": "resolved",
                "query_id": pending["query_id"],
                "resolved": True,
                "entity_updates": reflection["resolved_values"],
                "missing_fields": reflection["missing_fields"],
                "resolved_values": reflection["resolved_values"],
                "matched_candidate_id": reflection.get("matched_candidate_id"),
                "reason_unresolved": "",
                "why_unresolved": "",
                "followup_question": "",
                "should_fallback": False,
                "session_status": SessionStatus.EXECUTING,
            }

        if reflection["should_fallback"]:
            logger.warning(
                "Clarification fallback: session=%s query=%s attempt=%s/%s reason=%s",
                session.id,
                pending["query_id"],
                pending.get("attempt_count", 1),
                pending.get("max_attempts", 3),
                reflection.get("reason_unresolved") or "unresolved",
            )
            return ClarificationService.fallback(
                session=session,
                pending_query=pending,
                reflection=reflection,
            )

        next_attempt = int(pending.get("attempt_count") or 1) + 1
        pending.update({
            "status": "retryable",
            "attempt_count": next_attempt,
            "followup_question": reflection["followup_question"] or pending.get("followup_question") or pending.get("question", ""),
            "why_unresolved": reflection["reason_unresolved"],
            "reason_unresolved": reflection["reason_unresolved"],
            "entity_updates": reflection["resolved_values"],
            "resolved_values": reflection["resolved_values"],
            "missing_fields": reflection["missing_fields"],
            "matched_candidate_id": reflection.get("matched_candidate_id") or "",
            "last_user_reply": clean_transcript,
        })
        session.pending_user_input = pending
        session.save(update_fields=["pending_user_input", "updated_at"])
        logger.info(
            "Clarification follow-up: session=%s query=%s attempt=%s/%s reason=%s question=%s",
            session.id,
            pending["query_id"],
            pending["attempt_count"],
            pending["max_attempts"],
            pending.get("reason_unresolved") or "unresolved",
            pending.get("followup_question") or "",
        )
        AuditService.record(
            session=session,
            event_type=AuditEventType.USER_INPUT_REQUESTED,
            actor=AuditActor.SYSTEM,
            payload={
                "query_id": pending["query_id"],
                "question": pending.get("followup_question") or "",
                "required_fields": pending.get("required_fields") or [],
                "candidates": pending.get("candidates") or [],
                "attempt_count": pending["attempt_count"],
                "max_attempts": pending["max_attempts"],
                "reason": pending.get("reason", ""),
                "reason_unresolved": pending.get("reason_unresolved", ""),
            },
        )
        return {
            "status": "needs_user_input",
            "query_id": pending["query_id"],
            "resolved": False,
            "question": pending["followup_question"],
            "required_fields": pending["required_fields"],
            "candidates": pending.get("candidates") or [],
            "attempt": pending["attempt_count"],
            "max_attempts": pending["max_attempts"],
            "reason": pending.get("reason", ""),
            "why_unresolved": pending["why_unresolved"],
            "reason_unresolved": pending["reason_unresolved"],
            "missing_fields": pending.get("missing_fields") or list(pending.get("required_fields") or []),
            "resolved_values": pending.get("resolved_values") or {},
            "matched_candidate_id": pending.get("matched_candidate_id") or "",
            "followup_question": pending.get("followup_question") or "",
            "should_fallback": False,
            "entity_updates": pending.get("resolved_values") or {},
            "session_status": SessionStatus.AWAITING_USER_INPUT,
        }

    @staticmethod
    def reflect_reply(
        *,
        session: AgentSession,
        pending_query: dict,
        transcript: str,
    ) -> dict:
        candidate_records = _get_candidate_records(pending_query)
        required_fields = [
            str(item).strip()
            for item in (pending_query.get("required_fields") or [])
            if str(item).strip()
        ]
        current_entities = dict(session.entities or {})
        clean_transcript = " ".join(str(transcript or "").split()).strip()
        if not _normalize(clean_transcript):
            return _build_unresolved_result(
                pending_query=pending_query,
                required_fields=required_fields,
                current_entities=current_entities,
                reason_unresolved="empty_reply",
                followup_question=_build_followup_question(
                    pending_query=pending_query,
                    reason_unresolved="empty_reply",
                    missing_fields=required_fields,
                    candidate_records=candidate_records,
                ),
            )

        if _looks_like_non_answer(clean_transcript):
            return _build_unresolved_result(
                pending_query=pending_query,
                required_fields=required_fields,
                current_entities=current_entities,
                reason_unresolved="not_clarification_data",
                followup_question=_build_followup_question(
                    pending_query=pending_query,
                    reason_unresolved="not_clarification_data",
                    missing_fields=required_fields,
                    candidate_records=candidate_records,
                ),
            )

        deterministic = _deterministic_candidate_match(clean_transcript, candidate_records)
        if deterministic["state"] == "resolved":
            resolved_values = _build_entity_updates(required_fields, deterministic["value"])
            conflicts = _find_conflicting_fields(current_entities, resolved_values)
            if conflicts:
                return _build_unresolved_result(
                    pending_query=pending_query,
                    required_fields=required_fields,
                    current_entities=current_entities,
                    resolved_values=resolved_values,
                    matched_candidate_id=deterministic.get("candidate_id"),
                    reason_unresolved="conflicting_context",
                    followup_question=_build_followup_question(
                        pending_query=pending_query,
                        reason_unresolved="conflicting_context",
                        missing_fields=conflicts,
                        candidate_records=candidate_records,
                    ),
                )
            return _build_resolved_result(
                required_fields=required_fields,
                current_entities=current_entities,
                resolved_values=resolved_values,
                matched_candidate_id=deterministic.get("candidate_id"),
            )
        if deterministic["state"] == "ambiguous":
            return _build_unresolved_result(
                pending_query=pending_query,
                required_fields=required_fields,
                current_entities=current_entities,
                reason_unresolved="multiple_matches",
                followup_question=_build_followup_question(
                    pending_query=pending_query,
                    reason_unresolved="multiple_matches",
                    missing_fields=required_fields,
                    candidate_records=candidate_records,
                ),
            )

        if not candidate_records and len(required_fields) == 1:
            resolved_values = _build_entity_updates(required_fields, clean_transcript)
            conflicts = _find_conflicting_fields(current_entities, resolved_values)
            if conflicts:
                return _build_unresolved_result(
                    pending_query=pending_query,
                    required_fields=required_fields,
                    current_entities=current_entities,
                    resolved_values=resolved_values,
                    reason_unresolved="conflicting_context",
                    followup_question=_build_followup_question(
                        pending_query=pending_query,
                        reason_unresolved="conflicting_context",
                        missing_fields=conflicts,
                        candidate_records=candidate_records,
                    ),
                )
            return _build_resolved_result(
                required_fields=required_fields,
                current_entities=current_entities,
                resolved_values=resolved_values,
                matched_candidate_id=None,
            )

        model_reflection = _reflect_with_model(
            session=session,
            pending_query=pending_query,
            transcript=clean_transcript,
        )
        if model_reflection is not None:
            resolved_values = dict(model_reflection.get("resolved_values") or {})
            matched_candidate_id = model_reflection.get("matched_candidate_id")
            if model_reflection.get("resolved"):
                conflicts = _find_conflicting_fields(current_entities, resolved_values)
                if conflicts:
                    return _build_unresolved_result(
                        pending_query=pending_query,
                        required_fields=required_fields,
                        current_entities=current_entities,
                        resolved_values=resolved_values,
                        matched_candidate_id=matched_candidate_id,
                        reason_unresolved="conflicting_context",
                        followup_question=_build_followup_question(
                            pending_query=pending_query,
                            reason_unresolved="conflicting_context",
                            missing_fields=conflicts,
                            candidate_records=candidate_records,
                        ),
                    )
                return _build_resolved_result(
                    required_fields=required_fields,
                    current_entities=current_entities,
                    resolved_values=resolved_values,
                    matched_candidate_id=matched_candidate_id,
                )
            return _build_unresolved_result(
                pending_query=pending_query,
                required_fields=required_fields,
                current_entities=current_entities,
                resolved_values=resolved_values,
                matched_candidate_id=matched_candidate_id,
                reason_unresolved=str(model_reflection.get("reason_unresolved") or "model_unresolved"),
                followup_question=str(model_reflection.get("followup_question") or "").strip(),
            )

        return _build_unresolved_result(
            pending_query=pending_query,
            required_fields=required_fields,
            current_entities=current_entities,
            reason_unresolved="no_candidate_match",
            followup_question=_build_followup_question(
                pending_query=pending_query,
                reason_unresolved="no_candidate_match",
                missing_fields=required_fields,
                candidate_records=candidate_records,
            ),
        )

    @staticmethod
    def fallback(
        *,
        session: AgentSession,
        pending_query: dict,
        reflection: dict,
    ) -> dict:
        fallback_reason = (
            f"Could not resolve clarification after {pending_query.get('attempt_count', 0)} attempts: "
            f"{reflection.get('reason_unresolved') or 'unresolved'}."
        )
        pending_query.update({
            "status": "fallback",
            "why_unresolved": reflection.get("reason_unresolved") or "unresolved",
            "reason_unresolved": reflection.get("reason_unresolved") or "unresolved",
            "fallback_mode": "manual_takeover",
            "missing_fields": reflection.get("missing_fields") or list(pending_query.get("required_fields") or []),
            "resolved_values": reflection.get("resolved_values") or {},
            "matched_candidate_id": reflection.get("matched_candidate_id") or "",
        })
        session.pending_user_input = pending_query
        session.save(update_fields=["pending_user_input", "updated_at"])
        SessionService.transition(session, SessionStatus.EXECUTING)
        AuditService.record(
            session=session,
            event_type=AuditEventType.USER_INPUT_FALLBACK,
            actor=AuditActor.SYSTEM,
            payload={
                "query_id": pending_query.get("query_id", ""),
                "fallback_mode": "manual_takeover",
                "why_unresolved": pending_query.get("why_unresolved", ""),
                "attempt_count": pending_query.get("attempt_count", 0),
                "missing_fields": pending_query.get("missing_fields") or [],
                "matched_candidate_id": pending_query.get("matched_candidate_id") or "",
                "resolved_values": pending_query.get("resolved_values") or {},
            },
        )
        return {
            "status": "manual_takeover",
            "query_id": pending_query.get("query_id", ""),
            "resolved": False,
            "reason": fallback_reason,
            "missing_fields": reflection.get("missing_fields") or list(pending_query.get("required_fields") or []),
            "resolved_values": reflection.get("resolved_values") or {},
            "matched_candidate_id": reflection.get("matched_candidate_id") or "",
            "reason_unresolved": reflection.get("reason_unresolved") or "unresolved",
            "why_unresolved": reflection.get("reason_unresolved") or "unresolved",
            "followup_question": reflection.get("followup_question") or "",
            "should_fallback": True,
            "fallback_mode": "manual_takeover",
            "entity_updates": reflection.get("resolved_values") or {},
            "session_status": SessionStatus.EXECUTING,
        }


def _build_ui_context(screen_state: Optional[dict], *, candidates: list[str]) -> dict:
    if not screen_state:
        return {"candidates": candidates}
    nodes = []
    for node in (screen_state.get("nodes") or [])[:12]:
        if not isinstance(node, dict):
            continue
        label = str(node.get("text") or node.get("content_desc") or "").strip()
        if not label and not node.get("clickable"):
            continue
        nodes.append({
            "ref": str(node.get("ref") or ""),
            "label": label[:120],
            "clickable": bool(node.get("clickable")),
            "view_id": str(node.get("view_id") or ""),
        })
    return {
        "foreground_package": str(screen_state.get("foreground_package") or ""),
        "window_title": str(screen_state.get("window_title") or ""),
        "screen_hash": str(screen_state.get("screen_hash") or ""),
        "candidates": candidates,
        "visible_nodes": nodes,
    }


def _build_entity_updates(required_fields: list[str], value: str) -> dict:
    if not required_fields:
        return {}
    if len(required_fields) == 1:
        return {required_fields[0]: value}
    return {field: value for field in required_fields}


def _make_candidate_records(candidates: list[str]) -> list[dict]:
    return [
        {"id": f"candidate_{index + 1}", "label": label}
        for index, label in enumerate(candidates)
    ]


def _get_candidate_records(pending_query: dict) -> list[dict]:
    raw_records = pending_query.get("candidate_records")
    if isinstance(raw_records, list):
        records = []
        for index, item in enumerate(raw_records):
            if not isinstance(item, dict):
                continue
            label = str(item.get("label") or "").strip()
            if not label:
                continue
            records.append({
                "id": str(item.get("id") or f"candidate_{index + 1}"),
                "label": label,
            })
        if records:
            return records
    candidates = [
        str(item).strip()
        for item in (pending_query.get("candidates") or [])
        if str(item).strip()
    ]
    return _make_candidate_records(candidates)


def _normalize(text: str) -> str:
    normalized = " ".join(text.lower().split()).strip()
    if normalized in _EMPTY_REPLY_SENTINELS:
        return ""
    return normalized


def _deterministic_candidate_match(transcript: str, candidate_records: list[dict]) -> dict:
    if not candidate_records:
        return {"state": "none", "value": "", "candidate_id": None}

    normalized_reply = _normalize(transcript)
    if not normalized_reply:
        return {"state": "none", "value": "", "candidate_id": None}

    ordinal_matches: list[dict] = []
    for index, phrases in _ORDINAL_ALIASES.items():
        if index >= len(candidate_records):
            continue
        if any(phrase in normalized_reply for phrase in phrases):
            ordinal_matches.append(candidate_records[index])
    if len(ordinal_matches) == 1:
        return {
            "state": "resolved",
            "value": ordinal_matches[0]["label"],
            "candidate_id": ordinal_matches[0]["id"],
        }
    if len(ordinal_matches) > 1:
        return {"state": "ambiguous", "value": "", "candidate_id": None}

    exact_matches = [
        candidate
        for candidate in candidate_records
        if _normalize(candidate["label"]) == normalized_reply
    ]
    if len(exact_matches) == 1:
        return {
            "state": "resolved",
            "value": exact_matches[0]["label"],
            "candidate_id": exact_matches[0]["id"],
        }

    substring_matches: list[dict] = []
    for candidate in candidate_records:
        normalized_candidate = _normalize(candidate["label"])
        candidate_parts = [part for part in normalized_candidate.split(" ") if len(part) > 1]
        if normalized_candidate in normalized_reply:
            substring_matches.append(candidate)
            continue
        if any(part in normalized_reply for part in candidate_parts):
            substring_matches.append(candidate)
    unique_matches = list({item["id"]: item for item in substring_matches}.values())
    if len(unique_matches) == 1:
        return {
            "state": "resolved",
            "value": unique_matches[0]["label"],
            "candidate_id": unique_matches[0]["id"],
        }
    if len(unique_matches) > 1:
        return {"state": "ambiguous", "value": "", "candidate_id": None}
    return {"state": "none", "value": "", "candidate_id": None}


def _looks_like_non_answer(transcript: str) -> bool:
    normalized = _normalize(transcript)
    return normalized in _NON_ANSWER_PHRASES


def _find_conflicting_fields(current_entities: dict, resolved_values: dict) -> list[str]:
    conflicts: list[str] = []
    for field, value in resolved_values.items():
        current_value = str(current_entities.get(field) or "").strip()
        next_value = str(value or "").strip()
        if current_value and next_value and _normalize(current_value) != _normalize(next_value):
            conflicts.append(field)
    return conflicts


def _missing_required_fields(
    required_fields: list[str],
    current_entities: dict,
    resolved_values: dict,
) -> list[str]:
    missing: list[str] = []
    for field in required_fields:
        value = resolved_values.get(field)
        if value is None or not str(value).strip():
            value = current_entities.get(field)
        if value is None or not str(value).strip():
            missing.append(field)
    return missing


def _build_resolved_result(
    *,
    required_fields: list[str],
    current_entities: dict,
    resolved_values: dict,
    matched_candidate_id: Optional[str],
) -> dict:
    missing_fields = _missing_required_fields(required_fields, current_entities, resolved_values)
    resolved = not missing_fields
    return {
        "resolved": resolved,
        "missing_fields": missing_fields,
        "resolved_values": resolved_values if resolved else {},
        "matched_candidate_id": matched_candidate_id or None,
        "reason_unresolved": "" if resolved else "missing_required_fields",
        "why_unresolved": "" if resolved else "missing_required_fields",
        "followup_question": "",
        "should_fallback": False,
        "entity_updates": resolved_values if resolved else {},
    }


def _build_unresolved_result(
    *,
    pending_query: dict,
    required_fields: list[str],
    current_entities: dict,
    reason_unresolved: str,
    followup_question: str,
    resolved_values: Optional[dict] = None,
    matched_candidate_id: Optional[str] = None,
) -> dict:
    safe_values = dict(resolved_values or {})
    missing_fields = _missing_required_fields(required_fields, current_entities, safe_values)
    attempt_count = int(pending_query.get("attempt_count") or 1)
    max_attempts = int(pending_query.get("max_attempts") or 3)
    return {
        "resolved": False,
        "missing_fields": missing_fields or list(required_fields),
        "resolved_values": safe_values,
        "matched_candidate_id": matched_candidate_id or None,
        "reason_unresolved": reason_unresolved,
        "why_unresolved": reason_unresolved,
        "followup_question": followup_question,
        "should_fallback": attempt_count >= max_attempts,
        "entity_updates": safe_values,
    }


def _retry_question(pending_query: dict, suffix: str) -> str:
    base = str(pending_query.get("question") or "").strip()
    suffix = str(suffix or "").strip()
    if not base:
        return suffix
    if not suffix:
        return base
    return f"{base} {suffix}"


def _field_label(fields: list[str]) -> str:
    if not fields:
        return "detail"
    if len(fields) == 1:
        return fields[0].replace("_", " ")
    return ", ".join(field.replace("_", " ") for field in fields)


def _build_followup_question(
    *,
    pending_query: dict,
    reason_unresolved: str,
    missing_fields: list[str],
    candidate_records: list[dict],
) -> str:
    next_attempt = min(
        int(pending_query.get("attempt_count") or 1) + 1,
        int(pending_query.get("max_attempts") or 3),
    )
    prefix = "Final try:" if next_attempt >= int(pending_query.get("max_attempts") or 3) else "Please be more specific:"
    field_label = _field_label(missing_fields or list(pending_query.get("required_fields") or []))
    candidate_labels = [record["label"] for record in candidate_records[:5]]

    if candidate_labels:
        options = ", ".join(candidate_labels)
        if reason_unresolved == "empty_reply":
            return f"{prefix} I did not catch that. Please say just the {field_label}. Options: {options}."
        if reason_unresolved == "not_clarification_data":
            return f"{prefix} I need the {field_label}, not yes or no. Please choose exactly one: {options}."
        if reason_unresolved in {"multiple_matches", "no_candidate_match"}:
            return f"{prefix} Please choose exactly one {field_label}: {options}."
        if reason_unresolved == "conflicting_context":
            return f"{prefix} Your answer conflicts with the current {field_label}. Please choose exactly one: {options}."
        return f"{prefix} Please answer with exactly one option: {options}."

    if reason_unresolved == "empty_reply":
        return _retry_question(
            pending_query,
            f"I did not catch that. Please answer with just the {field_label}.",
        )
    if reason_unresolved == "not_clarification_data":
        return _retry_question(
            pending_query,
            f"I need the {field_label}, not yes or no.",
        )
    if reason_unresolved == "conflicting_context":
        return _retry_question(
            pending_query,
            f"Your answer conflicts with the current context. Please provide just the {field_label}.",
        )
    return _retry_question(
        pending_query,
        f"Please answer with just the {field_label}.",
    )


def _reflect_with_model(
    *,
    session: AgentSession,
    pending_query: dict,
    transcript: str,
) -> Optional[dict]:
    candidate_records = _get_candidate_records(pending_query)
    required_fields = [
        str(item).strip()
        for item in (pending_query.get("required_fields") or [])
        if str(item).strip()
    ]
    if not candidate_records and not required_fields:
        return None

    system_prompt = (
        "You map a user's clarification reply into a structured resolution. "
        "Only use the provided required_fields, candidate ids, candidate labels, and current entities. "
        "Prefer unresolved over guessing, and never turn a yes/no approval into resolved clarification data."
    )
    user_prompt = "\n".join([
        f"QUESTION: {pending_query.get('question', '')}",
        f"REQUIRED_FIELDS: {json.dumps(required_fields, ensure_ascii=True)}",
        f"CANDIDATE_RECORDS: {json.dumps(candidate_records, ensure_ascii=False)}",
        f"CURRENT_ENTITIES: {json.dumps(session.entities or {}, ensure_ascii=False)}",
        f"USER_REPLY: {transcript}",
        "",
        "Return JSON with:",
        '{"resolved":true|false,"missing_fields":[],"resolved_values":{},"matched_candidate_id":"","reason_unresolved":"","followup_question":"","should_fallback":false}',
    ])

    try:
        raw = LLMClient.from_reasoning_provider(session.reasoning_provider).generate(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            json_mode=True,
        )
    except LLMError as exc:
        logger.warning("Clarification reflection LLM failed for session=%s: %s", session.id, exc)
        return None

    if not isinstance(raw, dict):
        return None

    resolved = bool(raw.get("resolved"))
    matched_candidate_id = str(
        raw.get("matched_candidate_id")
        or raw.get("selected_candidate_id")
        or ""
    ).strip()
    selected_candidate = str(raw.get("selected_candidate") or "").strip()
    resolved_values = raw.get("resolved_values") or raw.get("entity_updates") or {}
    if not isinstance(resolved_values, dict):
        resolved_values = {}

    candidate_by_id = {record["id"]: record["label"] for record in candidate_records}
    candidate_by_label = {record["label"]: record["id"] for record in candidate_records}
    if matched_candidate_id and matched_candidate_id in candidate_by_id:
        resolved_values = _build_entity_updates(required_fields, candidate_by_id[matched_candidate_id])
    elif selected_candidate and selected_candidate in candidate_by_label:
        matched_candidate_id = candidate_by_label[selected_candidate]
        resolved_values = _build_entity_updates(required_fields, selected_candidate)
    elif matched_candidate_id and matched_candidate_id not in candidate_by_id:
        return None

    if resolved and resolved_values:
        missing_fields = [
            field
            for field in required_fields
            if field not in resolved_values or not str(resolved_values.get(field) or "").strip()
        ]
        if not missing_fields:
            return {
                "resolved": True,
                "missing_fields": [],
                "resolved_values": resolved_values,
                "matched_candidate_id": matched_candidate_id or None,
                "reason_unresolved": "",
                "followup_question": "",
                "should_fallback": False,
            }

    raw_missing_fields = raw.get("missing_fields")
    missing_fields = [
        str(item).strip()
        for item in (raw_missing_fields or required_fields)
        if str(item).strip()
    ]
    return {
        "resolved": False,
        "missing_fields": missing_fields,
        "resolved_values": resolved_values,
        "matched_candidate_id": matched_candidate_id or None,
        "reason_unresolved": str(raw.get("reason_unresolved") or raw.get("why_unresolved") or "").strip() or "model_unresolved",
        "followup_question": str(raw.get("followup_question") or "").strip(),
        "should_fallback": False,
    }
