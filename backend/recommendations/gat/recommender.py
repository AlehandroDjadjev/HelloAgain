from __future__ import annotations

import copy
import math
import os
import random
import time
from datetime import datetime, timezone

import torch
import torch.optim as optim

from .feature_schema import (
    FEATURE_NAMES,
    get_default_feature_vector,
    get_feature_groups,
    get_feature_names,
)
from .feature_updater import compute_compatibility_score
from .gat_model import create_model, cosine_similarity_matrix, top_k_recommendations
from .graph_builder import balance_feature_tensor, build_graph, edge_pairs_from_index

_MODEL_DIR = os.path.join(os.path.dirname(__file__), "checkpoints")
_MODEL_PATH = os.path.join(_MODEL_DIR, "elder_gat.pt")
_cached_bundle = None

_DERIVED_FEATURE_SPECS: dict[str, dict[str, float]] = {
    "community_drive": {
        "extroversion": 0.28,
        "positivity": 0.22,
        "emotional_warmth": 0.22,
        "activity_level": 0.18,
        "humor": 0.10,
    },
    "reflective_depth": {
        "openness": 0.20,
        "story_telling": 0.30,
        "nostalgia_index": 0.24,
        "interest_history": 0.16,
        "verbosity": 0.10,
    },
    "care_anchor": {
        "empathy": 0.30,
        "patience": 0.25,
        "interest_family": 0.20,
        "agreeableness": 0.15,
        "emotional_warmth": 0.10,
    },
    "spiritual_grounding": {
        "interest_religion": 0.40,
        "spiritual_alignment": 0.40,
        "positivity": 0.20,
    },
    "conversation_ease": {
        "verbosity": 0.28,
        "humor": 0.24,
        "emotional_warmth": 0.24,
        "story_telling": 0.16,
        "directness": 0.08,
    },
    "cozy_compatibility": {
        "prefers_small_groups": 0.34,
        "patience": 0.24,
        "empathy": 0.18,
        "emotional_warmth": 0.14,
        "nostalgia_index": 0.10,
    },
    "active_curiosity": {
        "openness": 0.28,
        "interest_travel": 0.20,
        "interest_nature": 0.20,
        "activity_level": 0.20,
        "interest_arts": 0.12,
    },
}


def get_derived_feature_names() -> list[str]:
    return list(_DERIVED_FEATURE_SPECS.keys())


def _all_search_feature_names() -> list[str]:
    return get_feature_names() + get_derived_feature_names()


def _is_derived_feature(feature_name: str) -> bool:
    return feature_name in _DERIVED_FEATURE_SPECS


def _compute_derived_feature_value(feature_name: str, feature_vector: dict) -> float:
    defaults = get_default_feature_vector()
    spec = _DERIVED_FEATURE_SPECS[feature_name]
    return sum(
        float(feature_vector.get(base_name, defaults.get(base_name, 0.5))) * weight
        for base_name, weight in spec.items()
    )


def _augment_feature_tensor(base_x: torch.Tensor, selected_features: list[str]) -> torch.Tensor:
    base_feature_names = get_feature_names()
    base_indices = {name: index for index, name in enumerate(base_feature_names)}
    columns: list[torch.Tensor] = []
    for feature_name in selected_features:
        if feature_name in base_indices:
            columns.append(base_x[:, base_indices[feature_name]].unsqueeze(1))
            continue
        if _is_derived_feature(feature_name):
            derived_column = torch.zeros((base_x.size(0), 1), dtype=base_x.dtype, device=base_x.device)
            for base_name, weight in _DERIVED_FEATURE_SPECS[feature_name].items():
                if base_name not in base_indices:
                    continue
                derived_column += base_x[:, base_indices[base_name]].unsqueeze(1) * weight
            # Derived columns are useful context, but they should not fully duplicate
            # the base traits they are computed from.
            columns.append((derived_column.clamp(0.0, 1.0) * 0.7) + 0.15)
    if not columns:
        return balance_feature_tensor(base_x)
    return balance_feature_tensor(torch.cat(columns, dim=1))


def _normalize_enabled_features(enabled_features: list[str] | None) -> list[str]:
    feature_names = _all_search_feature_names()
    if not enabled_features:
        return list(FEATURE_NAMES)

    enabled_set = set(enabled_features)
    normalized = [feature for feature in feature_names if feature in enabled_set]
    return normalized or list(FEATURE_NAMES)


def _default_graph_params() -> dict:
    return {
        "use_social_edges": True,
        "neighbor_k": 5,
        "min_similarity": 0.15,
    }


def _default_model_params() -> dict:
    return {
        "model_family": "pyg_gatv2_ranker",
        "hidden_channels": 48,
        "out_channels": 16,
        "heads": 4,
        "feature_dropout": 0.10,
        "attention_dropout": 0.10,
    }


def _default_training_params() -> dict:
    return {
        "epochs": 180,
        "learning_rate": 3e-3,
        "weight_decay": 5e-4,
        "negative_ratio": 2,
        "label_smoothing": 0.05,
        "gradient_clip": 1.0,
        "patience": 20,
        "min_delta": 0.003,
        "ranking_k": 5,
        "seed": 42,
    }


def _config_value(config: dict | None, key: str, default):
    if config is None:
        return default
    if key in config:
        return config[key]
    return default


def _nested_config_value(config: dict | None, section: str, key: str, default):
    if config is None:
        return default
    section_value = config.get(section)
    if isinstance(section_value, dict) and key in section_value:
        return section_value[key]
    return config.get(key, default)


def _normalize_training_config(
    *,
    config: dict | None = None,
    epochs: int | None = None,
    enabled_features: list[str] | None = None,
    mode: str = "baseline",
) -> dict:
    graph_defaults = _default_graph_params()
    model_defaults = _default_model_params()
    training_defaults = _default_training_params()

    selected_features = enabled_features
    if selected_features is None and isinstance(config, dict):
        raw_features = config.get("enabled_features")
        if isinstance(raw_features, list):
            selected_features = raw_features

    if epochs is None and isinstance(config, dict):
        raw_epochs = config.get("epochs")
        if raw_epochs is not None:
            epochs = int(raw_epochs)

    graph_params = {
        "use_social_edges": bool(
            _nested_config_value(config, "graph_params", "use_social_edges", graph_defaults["use_social_edges"])
        ),
        "neighbor_k": max(
            1,
            int(_nested_config_value(config, "graph_params", "neighbor_k", graph_defaults["neighbor_k"])),
        ),
        "min_similarity": max(
            0.0,
            min(
                1.0,
                float(
                    _nested_config_value(
                        config,
                        "graph_params",
                        "min_similarity",
                        graph_defaults["min_similarity"],
                    )
                ),
            ),
        ),
    }

    model_params = {
        "model_family": str(
            _nested_config_value(
                config,
                "model_params",
                "model_family",
                _config_value(config, "model_family", model_defaults["model_family"]),
            )
        ).strip()
        or "legacy_gat",
        "hidden_channels": max(
            8,
            int(
                _nested_config_value(
                    config,
                    "model_params",
                    "hidden_channels",
                    model_defaults["hidden_channels"],
                )
            ),
        ),
        "out_channels": max(
            4,
            int(
                _nested_config_value(
                    config,
                    "model_params",
                    "out_channels",
                    model_defaults["out_channels"],
                )
            ),
        ),
        "heads": max(
            1,
            int(_nested_config_value(config, "model_params", "heads", model_defaults["heads"])),
        ),
        "feature_dropout": max(
            0.0,
            min(
                0.6,
                float(
                    _nested_config_value(
                        config,
                        "model_params",
                        "feature_dropout",
                        model_defaults["feature_dropout"],
                    )
                ),
            ),
        ),
        "attention_dropout": max(
            0.0,
            min(
                0.6,
                float(
                    _nested_config_value(
                        config,
                        "model_params",
                        "attention_dropout",
                        model_defaults["attention_dropout"],
                    )
                ),
            ),
        ),
    }

    training_params = {
        "epochs": max(
            20,
            int(epochs if epochs is not None else _config_value(config, "epochs", training_defaults["epochs"])),
        ),
        "learning_rate": max(
            1e-4,
            float(
                _nested_config_value(
                    config,
                    "training_params",
                    "learning_rate",
                    training_defaults["learning_rate"],
                )
            ),
        ),
        "weight_decay": max(
            0.0,
            float(
                _nested_config_value(
                    config,
                    "training_params",
                    "weight_decay",
                    training_defaults["weight_decay"],
                )
            ),
        ),
        "negative_ratio": max(
            1,
            int(
                _nested_config_value(
                    config,
                    "training_params",
                    "negative_ratio",
                    training_defaults["negative_ratio"],
                )
            ),
        ),
        "label_smoothing": max(
            0.0,
            min(
                0.2,
                float(
                    _nested_config_value(
                        config,
                        "training_params",
                        "label_smoothing",
                        training_defaults["label_smoothing"],
                    )
                ),
            ),
        ),
        "gradient_clip": max(
            0.1,
            float(
                _nested_config_value(
                    config,
                    "training_params",
                    "gradient_clip",
                    training_defaults["gradient_clip"],
                )
            ),
        ),
        "patience": max(
            3,
            int(
                _nested_config_value(
                    config,
                    "training_params",
                    "patience",
                    training_defaults["patience"],
                )
            ),
        ),
        "min_delta": max(
            0.0,
            float(
                _nested_config_value(
                    config,
                    "training_params",
                    "min_delta",
                    training_defaults["min_delta"],
                )
            ),
        ),
        "ranking_k": max(
            1,
            int(
                _nested_config_value(
                    config,
                    "training_params",
                    "ranking_k",
                    training_defaults["ranking_k"],
                )
            ),
        ),
        "seed": int(
            _nested_config_value(
                config,
                "training_params",
                "seed",
                training_defaults["seed"],
            )
        ),
    }

    return {
        "mode": mode,
        "enabled_features": _normalize_enabled_features(selected_features),
        "graph_params": graph_params,
        "model_params": model_params,
        "training_params": training_params,
    }


def _checkpoint_exists() -> bool:
    return os.path.exists(_MODEL_PATH)


def _load_checkpoint_payload() -> dict | None:
    if not _checkpoint_exists():
        return None
    try:
        payload = torch.load(_MODEL_PATH, map_location="cpu")
    except Exception:
        return None

    if not isinstance(payload, dict):
        return {"state_dict": payload}
    return payload


def _instantiate_model(in_channels: int, model_params: dict | None = None) -> ElderGAT:
    params = _default_model_params()
    if isinstance(model_params, dict):
        params.update(model_params)
    return create_model(
        model_family=str(params.get("model_family", "legacy_gat")),
        in_channels=in_channels,
        hidden_channels=int(params["hidden_channels"]),
        out_channels=int(params["out_channels"]),
        heads=int(params["heads"]),
        feature_dropout=float(params["feature_dropout"]),
        attn_dropout=float(params["attention_dropout"]),
    )


def _checkpoint_mtime() -> float | None:
    if not _checkpoint_exists():
        return None
    return os.path.getmtime(_MODEL_PATH)


def _load_bundle() -> dict:
    global _cached_bundle
    current_mtime = _checkpoint_mtime()
    if _cached_bundle is not None and _cached_bundle.get("mtime") == current_mtime:
        return _cached_bundle

    payload = _load_checkpoint_payload() or {}
    enabled_features = _normalize_enabled_features(payload.get("enabled_features"))
    model_params = _default_model_params()
    model_params.update(payload.get("model_params", {}))
    graph_params = _default_graph_params()
    graph_params.update(payload.get("graph_params", {}))

    model = _instantiate_model(in_channels=len(enabled_features), model_params=model_params)
    state_dict = payload.get("state_dict")
    if isinstance(state_dict, dict):
        try:
            model.load_state_dict(state_dict)
        except Exception:
            pass
    model.eval()

    _cached_bundle = {
        "mtime": current_mtime,
        "payload": payload,
        "model": model,
        "enabled_features": enabled_features,
        "graph_params": graph_params,
        "model_params": model_params,
        "model_family": str(model_params.get("model_family", payload.get("model_family", "legacy_gat"))),
    }
    return _cached_bundle


def _get_model():
    return _load_bundle()["model"]


def get_model_family_for_inference() -> str:
    return str(_load_bundle().get("model_family", "legacy_gat"))


def get_enabled_features_for_inference() -> list[str]:
    return list(_load_bundle()["enabled_features"])


def get_graph_params_for_inference() -> dict:
    return dict(_load_bundle()["graph_params"])


def invalidate_model_cache():
    global _cached_bundle
    _cached_bundle = None


def get_active_checkpoint_metadata() -> dict:
    payload = _load_bundle()["payload"]
    validation_metrics = payload.get("validation_metrics", {})
    test_metrics = payload.get("test_metrics", {})
    updated_at = None
    if _checkpoint_exists():
        updated_at = datetime.fromtimestamp(
            os.path.getmtime(_MODEL_PATH),
            tz=timezone.utc,
        ).isoformat()
    return {
        "checkpoint_exists": _checkpoint_exists(),
        "updated_at": updated_at,
        "model_family": get_model_family_for_inference(),
        "enabled_features": get_enabled_features_for_inference(),
        "graph_params": dict(_load_bundle()["graph_params"]),
        "model_params": dict(_load_bundle()["model_params"]),
        "validation_metrics": validation_metrics,
        "test_metrics": test_metrics,
    }


def get_embedding_snapshot() -> dict:
    from recommendations.models import ElderProfile

    inference_features = get_enabled_features_for_inference()
    graph_params = get_graph_params_for_inference()
    graph_tensors, elder_ids = build_graph(enabled_features=inference_features, **graph_params)
    base_x, edge_index, edge_attr = graph_tensors
    x = _augment_feature_tensor(base_x, inference_features)
    model = _get_model()
    model.eval()
    with torch.no_grad():
        embeddings = model(x, edge_index, edge_attr)["embeddings"]

    profiles = {
        profile.id: profile
        for profile in ElderProfile.objects.filter(id__in=elder_ids)
    }
    return {
        "embeddings": embeddings,
        "elder_ids": elder_ids,
        "profiles": profiles,
        "enabled_features": inference_features,
        "graph_params": graph_params,
        "edge_index": edge_index,
        "edge_attr": edge_attr,
    }


def _shared_interests(vec_a: dict, vec_b: dict, threshold: float = 0.65) -> list[str]:
    groups = get_feature_groups()
    interest_features = groups.get("Interests & Activities", [])
    return [
        f.replace("interest_", "").replace("_", " ").title()
        for f in interest_features
        if vec_a.get(f, 0.5) >= threshold and vec_b.get(f, 0.5) >= threshold
    ]


def _relative_scores(values: list[float], temperature: float = 0.18) -> list[float]:
    if not values:
        return []
    max_value = max(values)
    exp_values = [math.exp((value - max_value) / temperature) for value in values]
    total = sum(exp_values)
    if total == 0:
        return [0.0 for _ in values]
    return [value / total for value in exp_values]


def _roc_auc(scores: list[float], labels: list[int]) -> float:
    positive_scores = [score for score, label in zip(scores, labels) if label == 1]
    negative_scores = [score for score, label in zip(scores, labels) if label == 0]
    if not positive_scores or not negative_scores:
        return 0.5

    better = 0.0
    total = len(positive_scores) * len(negative_scores)
    for pos_score in positive_scores:
        for neg_score in negative_scores:
            if pos_score > neg_score:
                better += 1.0
            elif pos_score == neg_score:
                better += 0.5
    return better / total


def _classification_metrics(logits: torch.Tensor, labels: torch.Tensor) -> dict:
    if logits.numel() == 0 or labels.numel() == 0:
        return {
            "accuracy": 0.0,
            "precision": 0.0,
            "recall": 0.0,
            "f1_score": 0.0,
            "roc_auc": 0.5,
            "positive_rate": 0.0,
        }

    probabilities = torch.sigmoid(logits)
    predictions = (probabilities >= 0.5).float()

    tp = float(((predictions == 1) & (labels == 1)).sum().item())
    fp = float(((predictions == 1) & (labels == 0)).sum().item())
    tn = float(((predictions == 0) & (labels == 0)).sum().item())
    fn = float(((predictions == 0) & (labels == 1)).sum().item())

    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1_score = (2 * precision * recall) / (precision + recall) if (precision + recall) else 0.0
    accuracy = (tp + tn) / (tp + tn + fp + fn) if (tp + tn + fp + fn) else 0.0
    roc_auc = _roc_auc(probabilities.tolist(), [int(label) for label in labels.tolist()])
    positive_rate = float(labels.mean().item()) if labels.numel() else 0.0

    return {
        "accuracy": round(accuracy, 4),
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1_score": round(f1_score, 4),
        "roc_auc": round(roc_auc, 4),
        "positive_rate": round(positive_rate, 4),
    }


def _feature_homophily_ratio(x: torch.Tensor, edge_index: torch.Tensor, threshold: float = 0.7) -> dict:
    del threshold
    edge_pairs = edge_pairs_from_index(edge_index)
    if not edge_pairs:
        return {
            "homophily_ratio": 0.0,
            "mean_edge_similarity": 0.0,
            "global_pair_similarity": 0.0,
        }

    normalized = torch.nn.functional.normalize(x, dim=1)
    similarities: list[float] = []
    for pair in edge_pairs:
        source_idx = pair["source_idx"]
        target_idx = pair["target_idx"]
        similarity = float(torch.dot(normalized[source_idx], normalized[target_idx]).item())
        similarities.append(similarity)

    all_pair_similarities: list[float] = []
    for source_idx in range(normalized.size(0)):
        for target_idx in range(source_idx + 1, normalized.size(0)):
            similarity = float(torch.dot(normalized[source_idx], normalized[target_idx]).item())
            all_pair_similarities.append(similarity)

    edge_mean = sum(similarities) / len(similarities)
    global_mean = sum(all_pair_similarities) / len(all_pair_similarities) if all_pair_similarities else edge_mean
    if global_mean >= 0.999:
        homophily_ratio = 0.0
    else:
        homophily_ratio = max(0.0, min(1.0, (edge_mean - global_mean) / (1.0 - global_mean)))

    return {
        "homophily_ratio": round(homophily_ratio, 4),
        "mean_edge_similarity": round(edge_mean, 4),
        "global_pair_similarity": round(global_mean, 4),
    }


def _compatibility_matrix(profiles: list) -> torch.Tensor:
    size = len(profiles)
    matrix = torch.zeros((size, size), dtype=torch.float)
    for source_idx, source_profile in enumerate(profiles):
        for target_idx, target_profile in enumerate(profiles):
            if source_idx == target_idx:
                matrix[source_idx, target_idx] = -1.0
            else:
                matrix[source_idx, target_idx] = compute_compatibility_score(
                    source_profile.feature_vector,
                    target_profile.feature_vector,
                    source_profile.feature_confidence,
                    target_profile.feature_confidence,
                    get_enabled_features_for_inference(),
                )
    return matrix


def _feature_similarity_from_vectors(vec_a: dict, vec_b: dict, selected_features: list[str]) -> float:
    values_a = []
    values_b = []
    defaults = get_default_feature_vector()
    for feature_name in selected_features:
        if _is_derived_feature(feature_name):
            values_a.append(_compute_derived_feature_value(feature_name, vec_a))
            values_b.append(_compute_derived_feature_value(feature_name, vec_b))
        else:
            values_a.append(float(vec_a.get(feature_name, defaults.get(feature_name, 0.5))))
            values_b.append(float(vec_b.get(feature_name, defaults.get(feature_name, 0.5))))

    dot = sum(left * right for left, right in zip(values_a, values_b))
    norm_a = math.sqrt(sum(value * value for value in values_a))
    norm_b = math.sqrt(sum(value * value for value in values_b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def _measure_inference_latency(
    model,
    x: torch.Tensor,
    edge_index: torch.Tensor,
    edge_attr: torch.Tensor | None,
    runs: int = 5,
) -> dict:
    if x.size(0) == 0:
        return {"inference_latency_ms": 0.0, "ms_per_edge": 0.0}

    model.eval()
    with torch.no_grad():
        model(x, edge_index, edge_attr)
        start = time.perf_counter()
        for _ in range(runs):
            model(x, edge_index, edge_attr)
        elapsed_ms = ((time.perf_counter() - start) / runs) * 1000.0

    edge_count = max(1, len(edge_pairs_from_index(edge_index, edge_attr)))
    return {
        "inference_latency_ms": round(elapsed_ms, 3),
        "ms_per_edge": round(elapsed_ms / edge_count, 5),
    }


def _pair_key(source_idx: int, target_idx: int) -> tuple[int, int]:
    return (source_idx, target_idx) if source_idx < target_idx else (target_idx, source_idx)


def _edge_records(edge_index: torch.Tensor, edge_attr: torch.Tensor | None = None) -> list[dict]:
    return edge_pairs_from_index(edge_index, edge_attr)


def _edge_index_from_pairs(pairs: list[tuple[int, int]]) -> torch.Tensor:
    if not pairs:
        return torch.zeros((2, 0), dtype=torch.long)
    src = [source_idx for source_idx, _ in pairs]
    dst = [target_idx for _, target_idx in pairs]
    return torch.tensor([src, dst], dtype=torch.long)


def _directed_graph_tensors(records: list[dict]) -> tuple[torch.Tensor, torch.Tensor]:
    if not records:
        return torch.zeros((2, 0), dtype=torch.long), torch.zeros((0, 2), dtype=torch.float)

    src: list[int] = []
    dst: list[int] = []
    weights: list[list[float]] = []
    for record in records:
        source_idx = int(record["source_idx"])
        target_idx = int(record["target_idx"])
        weight = float(record.get("weight", 1.0))
        social_weight = float(record.get("social_weight", 0.0))
        src.extend([source_idx, target_idx])
        dst.extend([target_idx, source_idx])
        weights.extend([[weight, social_weight], [weight, social_weight]])
    return torch.tensor([src, dst], dtype=torch.long), torch.tensor(weights, dtype=torch.float)


def _pair_tensor_from_records(records: list[dict]) -> torch.Tensor:
    if not records:
        return torch.zeros((2, 0), dtype=torch.long)
    src = [int(record["source_idx"]) for record in records]
    dst = [int(record["target_idx"]) for record in records]
    return torch.tensor([src, dst], dtype=torch.long)


def _split_positive_edges(edge_records: list[dict], node_count: int, seed: int) -> dict[str, list[dict]]:
    if len(edge_records) <= 2:
        return {"train": list(edge_records), "validation": [], "test": []}

    ordered = sorted(
        edge_records,
        key=lambda item: (-float(item.get("weight", 1.0)), item["source_idx"], item["target_idx"]),
    )
    rng = random.Random(seed)
    assigned_train: dict[tuple[int, int], dict] = {}

    for node_idx in range(node_count):
        if any(node_idx in (record["source_idx"], record["target_idx"]) for record in assigned_train.values()):
            continue
        for record in ordered:
            if node_idx not in (record["source_idx"], record["target_idx"]):
                continue
            key = _pair_key(record["source_idx"], record["target_idx"])
            if key in assigned_train:
                continue
            assigned_train[key] = record
            break

    total_edges = len(ordered)
    validation_target = max(1, round(total_edges * 0.15))
    test_target = max(1, round(total_edges * 0.15))
    train_target = max(len(assigned_train), total_edges - validation_target - test_target)
    if train_target + validation_target + test_target > total_edges:
        overflow = (train_target + validation_target + test_target) - total_edges
        if validation_target >= test_target:
            validation_target = max(0, validation_target - overflow)
        else:
            test_target = max(0, test_target - overflow)

    remaining = [
        record
        for record in ordered
        if _pair_key(record["source_idx"], record["target_idx"]) not in assigned_train
    ]
    rng.shuffle(remaining)

    train = list(assigned_train.values())
    validation: list[dict] = []
    test: list[dict] = []

    while remaining and len(train) < train_target:
        train.append(remaining.pop())
    while remaining and len(validation) < validation_target:
        validation.append(remaining.pop())
    while remaining and len(test) < test_target:
        test.append(remaining.pop())
    train.extend(remaining)

    return {"train": train, "validation": validation, "test": test}


def _all_positive_pair_keys(records: list[dict]) -> set[tuple[int, int]]:
    return {
        _pair_key(int(record["source_idx"]), int(record["target_idx"]))
        for record in records
    }


def _sample_negative_records(
    compatibility_matrix: torch.Tensor,
    known_positive_pairs: set[tuple[int, int]],
    sample_size: int,
    *,
    lower: float,
    upper: float,
) -> list[dict]:
    if sample_size <= 0:
        return []

    size = compatibility_matrix.size(0)
    band_candidates: list[tuple[float, int, int]] = []
    fallback_candidates: list[tuple[float, int, int]] = []
    for source_idx in range(size):
        for target_idx in range(source_idx + 1, size):
            pair = _pair_key(source_idx, target_idx)
            if pair in known_positive_pairs:
                continue
            compatibility = float(compatibility_matrix[source_idx, target_idx].item())
            bucket = band_candidates if lower <= compatibility <= upper else fallback_candidates
            bucket.append((compatibility, source_idx, target_idx))

    band_candidates.sort(reverse=True)
    fallback_candidates.sort(reverse=True)
    chosen = band_candidates[:sample_size]
    if len(chosen) < sample_size:
        chosen.extend(fallback_candidates[: max(0, sample_size - len(chosen))])

    return [
        {"source_idx": source_idx, "target_idx": target_idx, "weight": round(score, 4)}
        for score, source_idx, target_idx in chosen
    ]


def _positive_targets(records: list[dict], node_count: int) -> dict[int, set[int]]:
    targets = {node_idx: set() for node_idx in range(node_count)}
    for record in records:
        source_idx = int(record["source_idx"])
        target_idx = int(record["target_idx"])
        targets[source_idx].add(target_idx)
        targets[target_idx].add(source_idx)
    return targets


def _ranking_metrics(
    embeddings: torch.Tensor,
    records: list[dict],
    all_positive_records: list[dict],
    k: int,
) -> dict:
    if embeddings.numel() == 0 or not records:
        return {
            f"mrr_at_{k}": 0.0,
            f"recall_at_{k}": 0.0,
            "evaluated_pairs": 0,
        }

    similarity = cosine_similarity_matrix(embeddings).cpu()
    positives_by_query = _positive_targets(all_positive_records, embeddings.size(0))

    reciprocal_ranks: list[float] = []
    recall_hits: list[float] = []
    for record in records:
        source_idx = int(record["source_idx"])
        target_idx = int(record["target_idx"])
        for query_idx, candidate_idx in ((source_idx, target_idx), (target_idx, source_idx)):
            scores = similarity[query_idx].clone()
            scores[query_idx] = float("-inf")
            for blocked_idx in positives_by_query.get(query_idx, set()):
                if blocked_idx != candidate_idx:
                    scores[blocked_idx] = float("-inf")

            target_score = float(scores[candidate_idx].item())
            rank = int((scores > target_score).sum().item()) + 1
            reciprocal_ranks.append(1.0 / rank if rank <= k else 0.0)
            recall_hits.append(1.0 if rank <= k else 0.0)

    return {
        f"mrr_at_{k}": round(sum(reciprocal_ranks) / len(reciprocal_ranks), 4),
        f"recall_at_{k}": round(sum(recall_hits) / len(recall_hits), 4),
        "evaluated_pairs": len(reciprocal_ranks),
    }


def _scores_and_labels(
    model: ElderGAT,
    embeddings: torch.Tensor,
    pos_edge_index: torch.Tensor,
    neg_edge_index: torch.Tensor,
) -> tuple[torch.Tensor, torch.Tensor]:
    pos_scores = model.decode(embeddings, pos_edge_index)
    neg_scores = model.decode(embeddings, neg_edge_index)
    scores = torch.cat([pos_scores, neg_scores], dim=0)
    labels = torch.cat(
        [
            torch.ones(pos_scores.size(0), dtype=embeddings.dtype, device=embeddings.device),
            torch.zeros(neg_scores.size(0), dtype=embeddings.dtype, device=embeddings.device),
        ],
        dim=0,
    )
    return scores, labels


def _loss_from_scores(scores: torch.Tensor, labels: torch.Tensor) -> float:
    if scores.numel() == 0 or labels.numel() == 0:
        return 0.0
    return float(torch.nn.functional.binary_cross_entropy_with_logits(scores, labels).item())


def _report_sort_key(report: dict, prefix: str = "validation") -> tuple[float, float, float, float, int]:
    ranking_k = int(report.get("ranking_k", 5))
    return (
        -float(report.get(f"{prefix}_mrr_at_{ranking_k}", 0.0)),
        -float(report.get(f"{prefix}_recall_at_{ranking_k}", 0.0)),
        float(report.get(f"{prefix}_loss", 999.0)),
        float(report.get("inference_latency_ms", 9999.0)),
        int(report.get("feature_count", 0)),
    )


def _is_report_better(candidate: dict, incumbent: dict | None, prefix: str = "validation") -> bool:
    if incumbent is None:
        return True
    return _report_sort_key(candidate, prefix=prefix) < _report_sort_key(incumbent, prefix=prefix)


def _validation_improved(candidate: dict, incumbent: dict | None, min_delta: float) -> bool:
    if incumbent is None:
        return True

    ranking_k = int(candidate.get("ranking_k", incumbent.get("ranking_k", 5)))
    candidate_mrr = float(candidate.get(f"validation_mrr_at_{ranking_k}", 0.0))
    incumbent_mrr = float(incumbent.get(f"validation_mrr_at_{ranking_k}", 0.0))
    if candidate_mrr > incumbent_mrr + 1e-6:
        return True

    candidate_recall = float(candidate.get(f"validation_recall_at_{ranking_k}", 0.0))
    incumbent_recall = float(incumbent.get(f"validation_recall_at_{ranking_k}", 0.0))
    if abs(candidate_mrr - incumbent_mrr) <= 1e-6 and candidate_recall > incumbent_recall + 1e-6:
        return True

    candidate_loss = float(candidate.get("validation_loss", 999.0))
    incumbent_loss = float(incumbent.get("validation_loss", 999.0))
    return (
        abs(candidate_mrr - incumbent_mrr) <= 1e-6
        and abs(candidate_recall - incumbent_recall) <= 1e-6
        and candidate_loss < (incumbent_loss - min_delta)
    )


def _trial_summary(report: dict, rank: int | None = None) -> dict:
    summary = {
        "final_loss": report["final_loss"],
        "validation_loss": report["validation_loss"],
        "validation_mrr_at_5": report["validation_mrr_at_5"],
        "validation_recall_at_5": report["validation_recall_at_5"],
        "test_mrr_at_5": report["test_mrr_at_5"],
        "test_recall_at_5": report["test_recall_at_5"],
        "roc_auc": report["roc_auc"],
        "f1_score": report["f1_score"],
        "feature_count": report["feature_count"],
        "enabled_features": report["enabled_features"],
        "disabled_features": report["disabled_features"],
        "derived_features_used": report["derived_features_used"],
        "graph_params": report["graph_params"],
        "model_params": report["model_params"],
    }
    if rank is not None:
        summary["rank"] = rank
    return summary


def _train_single_config(config: dict, *, persist: bool = False) -> dict:
    graph_params = copy.deepcopy(config["graph_params"])
    model_params = copy.deepcopy(config["model_params"])
    training_params = copy.deepcopy(config["training_params"])
    enabled_features = list(config["enabled_features"])
    mode = config.get("mode", "baseline")

    graph_tensors, elder_ids = build_graph(
        use_social_edges=graph_params["use_social_edges"],
        enabled_features=enabled_features,
        neighbor_k=graph_params["neighbor_k"],
        min_similarity=graph_params["min_similarity"],
    )
    base_x, full_edge_index, full_edge_attr = graph_tensors
    x = _augment_feature_tensor(base_x, enabled_features)
    node_count = x.size(0)

    if node_count < 2:
        return {"error": "Need at least 2 elders to train."}

    full_edge_records = _edge_records(full_edge_index, full_edge_attr)
    if not full_edge_records:
        return {"error": "Need at least 1 graph edge to train."}

    from recommendations.models import ElderProfile

    profiles = list(ElderProfile.objects.filter(id__in=elder_ids).order_by("id"))
    compatibility_matrix = _compatibility_matrix(profiles)
    split = _split_positive_edges(full_edge_records, node_count=node_count, seed=training_params["seed"])
    train_records = split["train"]
    validation_records = split["validation"]
    test_records = split["test"]
    known_positive_pairs = _all_positive_pair_keys(full_edge_records)

    train_message_edge_index, train_message_edge_attr = _directed_graph_tensors(train_records)
    train_positive_edges = _pair_tensor_from_records(train_records)
    validation_positive_edges = _pair_tensor_from_records(validation_records)
    test_positive_edges = _pair_tensor_from_records(test_records)

    train_negative_records = _sample_negative_records(
        compatibility_matrix,
        known_positive_pairs,
        sample_size=max(1, len(train_records) * int(training_params["negative_ratio"])),
        lower=0.45,
        upper=0.75,
    )
    validation_negative_records = _sample_negative_records(
        compatibility_matrix,
        known_positive_pairs,
        sample_size=max(0, len(validation_records)),
        lower=0.45,
        upper=0.75,
    )
    test_negative_records = _sample_negative_records(
        compatibility_matrix,
        known_positive_pairs,
        sample_size=max(0, len(test_records)),
        lower=0.45,
        upper=0.75,
    )
    train_negative_edges = _pair_tensor_from_records(train_negative_records)
    validation_negative_edges = _pair_tensor_from_records(validation_negative_records)
    test_negative_edges = _pair_tensor_from_records(test_negative_records)

    torch.manual_seed(int(training_params["seed"]))
    model = _instantiate_model(in_channels=len(enabled_features), model_params=model_params)
    optimizer = optim.AdamW(
        model.parameters(),
        lr=float(training_params["learning_rate"]),
        weight_decay=float(training_params["weight_decay"]),
    )
    scheduler = optim.lr_scheduler.CosineAnnealingLR(
        optimizer,
        T_max=max(1, int(training_params["epochs"])),
    )

    best_state_dict = copy.deepcopy(model.state_dict())
    best_report: dict | None = None
    best_epoch = 0
    patience_counter = 0
    loss_curve: list[float] = []

    for epoch_idx in range(int(training_params["epochs"])):
        model.train()
        optimizer.zero_grad()
        result = model(
            x,
            train_message_edge_index,
            train_message_edge_attr,
            pos_edge_index=train_positive_edges,
            neg_edge_index=train_negative_edges,
            label_smoothing=float(training_params["label_smoothing"]),
        )
        loss = result["loss"]
        if loss is None:
            break
        loss.backward()
        torch.nn.utils.clip_grad_norm_(model.parameters(), max_norm=float(training_params["gradient_clip"]))
        optimizer.step()
        scheduler.step()
        loss_curve.append(round(float(loss.item()), 4))

        model.eval()
        with torch.no_grad():
            embeddings = model(x, train_message_edge_index, train_message_edge_attr)["embeddings"]

        validation_scores, validation_labels = _scores_and_labels(
            model,
            embeddings,
            validation_positive_edges,
            validation_negative_edges,
        )
        validation_loss = _loss_from_scores(validation_scores, validation_labels)
        validation_ranking = _ranking_metrics(
            embeddings,
            validation_records,
            full_edge_records,
            k=int(training_params["ranking_k"]),
        )
        epoch_report = {
            "validation_loss": round(validation_loss, 4),
            f"validation_mrr_at_{training_params['ranking_k']}": validation_ranking[f"mrr_at_{training_params['ranking_k']}"],
            f"validation_recall_at_{training_params['ranking_k']}": validation_ranking[f"recall_at_{training_params['ranking_k']}"],
            "ranking_k": int(training_params["ranking_k"]),
        }
        if _validation_improved(epoch_report, best_report, float(training_params["min_delta"])):
            best_report = epoch_report
            best_state_dict = copy.deepcopy(model.state_dict())
            best_epoch = epoch_idx + 1
            patience_counter = 0
        else:
            patience_counter += 1
            if patience_counter >= int(training_params["patience"]):
                break

    model.load_state_dict(best_state_dict)
    model.eval()
    with torch.no_grad():
        best_embeddings = model(x, train_message_edge_index, train_message_edge_attr)["embeddings"]

    train_scores, train_labels = _scores_and_labels(
        model,
        best_embeddings,
        train_positive_edges,
        train_negative_edges,
    )
    validation_scores, validation_labels = _scores_and_labels(
        model,
        best_embeddings,
        validation_positive_edges,
        validation_negative_edges,
    )
    test_scores, test_labels = _scores_and_labels(
        model,
        best_embeddings,
        test_positive_edges,
        test_negative_edges,
    )

    train_ranking = _ranking_metrics(best_embeddings, train_records, full_edge_records, k=int(training_params["ranking_k"]))
    validation_ranking = _ranking_metrics(best_embeddings, validation_records, full_edge_records, k=int(training_params["ranking_k"]))
    test_ranking = _ranking_metrics(best_embeddings, test_records, full_edge_records, k=int(training_params["ranking_k"]))

    test_classification = _classification_metrics(test_scores.cpu(), test_labels.cpu())
    homophily_metrics = _feature_homophily_ratio(x.cpu(), train_message_edge_index.cpu())
    latency_metrics = _measure_inference_latency(model, x, train_message_edge_index, train_message_edge_attr)

    search_feature_names = _all_search_feature_names()
    disabled_features = [feature for feature in search_feature_names if feature not in enabled_features]
    actual_epochs = len(loss_curve)
    validation_loss = _loss_from_scores(validation_scores, validation_labels)
    test_loss = _loss_from_scores(test_scores, test_labels)

    report = {
        "mode": mode,
        "model_family": str(model_params.get("model_family", "legacy_gat")),
        "trained_epochs": actual_epochs,
        "best_epoch": best_epoch,
        "stopped_early": actual_epochs < int(training_params["epochs"]),
        "final_loss": round(loss_curve[-1] if loss_curve else 0.0, 4),
        "validation_loss": round(validation_loss, 4),
        "test_loss": round(test_loss, 4),
        "loss_curve": loss_curve,
        "enabled_features": enabled_features,
        "disabled_features": disabled_features,
        "derived_features_used": [feature for feature in enabled_features if _is_derived_feature(feature)],
        "feature_count": len(enabled_features),
        "node_count": int(node_count),
        "edge_count": len(full_edge_records),
        "train_graph_edge_count": len(train_records),
        "train_pos_edges_used": int(train_positive_edges.size(1)),
        "validation_pos_edges_used": int(validation_positive_edges.size(1)),
        "test_pos_edges_used": int(test_positive_edges.size(1)),
        "pos_edges_used": int(train_positive_edges.size(1)),
        "neg_edges_used": int(train_negative_edges.size(1)),
        "train_neg_edges_used": int(train_negative_edges.size(1)),
        "validation_neg_edges_used": int(validation_negative_edges.size(1)),
        "test_neg_edges_used": int(test_negative_edges.size(1)),
        "train_loss": round(_loss_from_scores(train_scores, train_labels), 4),
        f"train_mrr_at_{training_params['ranking_k']}": train_ranking[f"mrr_at_{training_params['ranking_k']}"],
        f"train_recall_at_{training_params['ranking_k']}": train_ranking[f"recall_at_{training_params['ranking_k']}"],
        f"validation_mrr_at_{training_params['ranking_k']}": validation_ranking[f"mrr_at_{training_params['ranking_k']}"],
        f"validation_recall_at_{training_params['ranking_k']}": validation_ranking[f"recall_at_{training_params['ranking_k']}"],
        f"test_mrr_at_{training_params['ranking_k']}": test_ranking[f"mrr_at_{training_params['ranking_k']}"],
        f"test_recall_at_{training_params['ranking_k']}": test_ranking[f"recall_at_{training_params['ranking_k']}"],
        "validation_mrr_at_5": validation_ranking[f"mrr_at_{training_params['ranking_k']}"] if int(training_params["ranking_k"]) == 5 else 0.0,
        "validation_recall_at_5": validation_ranking[f"recall_at_{training_params['ranking_k']}"] if int(training_params["ranking_k"]) == 5 else 0.0,
        "test_mrr_at_5": test_ranking[f"mrr_at_{training_params['ranking_k']}"] if int(training_params["ranking_k"]) == 5 else 0.0,
        "test_recall_at_5": test_ranking[f"recall_at_{training_params['ranking_k']}"] if int(training_params["ranking_k"]) == 5 else 0.0,
        "ranking_k": int(training_params["ranking_k"]),
        "graph_params": graph_params,
        "model_params": model_params,
        "training_params": training_params,
        **test_classification,
        **homophily_metrics,
        **latency_metrics,
    }
    report["search_config"] = copy.deepcopy(config)

    if persist:
        os.makedirs(_MODEL_DIR, exist_ok=True)
        checkpoint_payload = {
            "state_dict": model.state_dict(),
            "model_family": str(model_params.get("model_family", "legacy_gat")),
            "enabled_features": enabled_features,
            "graph_params": graph_params,
            "model_params": model_params,
            "training_params": training_params,
            "validation_metrics": {
                "loss": report["validation_loss"],
                f"mrr_at_{training_params['ranking_k']}": report[f"validation_mrr_at_{training_params['ranking_k']}"],
                f"recall_at_{training_params['ranking_k']}": report[f"validation_recall_at_{training_params['ranking_k']}"],
            },
            "test_metrics": {
                "loss": report["test_loss"],
                f"mrr_at_{training_params['ranking_k']}": report[f"test_mrr_at_{training_params['ranking_k']}"],
                f"recall_at_{training_params['ranking_k']}": report[f"test_recall_at_{training_params['ranking_k']}"],
                "roc_auc": report["roc_auc"],
                "f1_score": report["f1_score"],
            },
            "loss_curve": loss_curve,
            "saved_at": datetime.now(tz=timezone.utc).isoformat(),
            "mode": mode,
        }
        torch.save(checkpoint_payload, _MODEL_PATH)
        invalidate_model_cache()

    return report


def train_model(
    epochs: int = 180,
    enabled_features: list[str] | None = None,
    persist: bool = True,
    mode: str = "baseline",
    config: dict | None = None,
):
    if mode == "aggressive":
        return search_feature_combinations(
            epochs=epochs,
            iterations=24,
            base_enabled_features=enabled_features,
            apply_best=persist,
            config=config,
        )

    normalized = _normalize_training_config(
        config=config,
        epochs=epochs,
        enabled_features=enabled_features,
        mode=mode,
    )
    return _train_single_config(normalized, persist=persist)


def _feature_improves_on_removal(baseline: dict, candidate: dict) -> bool:
    if candidate.get("error"):
        return False
    baseline_mrr = float(baseline.get("validation_mrr_at_5", 0.0))
    candidate_mrr = float(candidate.get("validation_mrr_at_5", 0.0))
    if candidate_mrr >= baseline_mrr + 0.01:
        return True
    return (
        candidate_mrr >= baseline_mrr - 1e-6
        and float(candidate.get("validation_loss", 999.0)) <= float(baseline.get("validation_loss", 999.0)) - 0.02
    )


def _add_back_improves(current: dict, candidate: dict) -> bool:
    if candidate.get("error"):
        return False
    if float(candidate.get("validation_mrr_at_5", 0.0)) >= float(current.get("validation_mrr_at_5", 0.0)) + 0.01:
        return True
    return float(candidate.get("validation_recall_at_5", 0.0)) >= float(current.get("validation_recall_at_5", 0.0)) + 0.02


def search_feature_combinations(
    epochs: int = 180,
    iterations: int = 24,
    base_enabled_features: list[str] | None = None,
    min_features: int = 4,
    apply_best: bool = True,
    config: dict | None = None,
) -> dict:
    base_config = _normalize_training_config(
        config=config,
        epochs=epochs,
        enabled_features=base_enabled_features,
        mode="aggressive",
    )
    candidate_features = list(base_config["enabled_features"])
    if len(candidate_features) < 2:
        raise ValueError("Need at least two features to search combinations.")

    min_features = max(2, min(min_features, len(candidate_features)))
    rng = random.Random(int(base_config["training_params"]["seed"]))

    graph_runs: list[dict] = []
    graph_candidates = [
        {"neighbor_k": neighbor_k, "min_similarity": min_similarity}
        for neighbor_k in (2, 3, 4, 5)
        for min_similarity in (0.48, 0.52, 0.56)
    ]
    for graph_candidate in graph_candidates:
        graph_config = copy.deepcopy(base_config)
        graph_config["graph_params"].update(graph_candidate)
        graph_run = _train_single_config(graph_config, persist=False)
        graph_runs.append(graph_run)

    top_graph_runs = sorted(graph_runs, key=_report_sort_key)[:3]
    best_graph_params = copy.deepcopy(top_graph_runs[0]["graph_params"])

    baseline_config = copy.deepcopy(base_config)
    baseline_config["graph_params"] = best_graph_params
    baseline_config["enabled_features"] = list(candidate_features)
    baseline_run = _train_single_config(baseline_config, persist=False)

    removal_trials: list[dict] = []
    dropped_features: list[dict] = []
    for feature_name in candidate_features:
        subset = [feature for feature in candidate_features if feature != feature_name]
        if len(subset) < min_features:
            continue
        trial_config = copy.deepcopy(baseline_config)
        trial_config["enabled_features"] = subset
        trial = _train_single_config(trial_config, persist=False)
        removal_trials.append(trial)
        if _feature_improves_on_removal(baseline_run, trial):
            dropped_features.append(
                {
                    "feature": feature_name,
                    "wins": 1,
                    "mean_loss_gain": round(
                        float(baseline_run["validation_loss"]) - float(trial["validation_loss"]),
                        4,
                    ),
                }
            )

    reduced_features = [
        feature for feature in candidate_features if feature not in {item["feature"] for item in dropped_features}
    ]
    if len(reduced_features) < min_features:
        reduced_features = list(candidate_features)

    reduced_config = copy.deepcopy(baseline_config)
    reduced_config["enabled_features"] = reduced_features
    current_best_run = _train_single_config(reduced_config, persist=False)

    greedy_trials: list[dict] = []
    greedy_changed = True
    while greedy_changed and len(current_best_run["enabled_features"]) > min_features:
        greedy_changed = False
        best_candidate: dict | None = None
        for feature_name in list(current_best_run["enabled_features"]):
            subset = [
                feature
                for feature in current_best_run["enabled_features"]
                if feature != feature_name
            ]
            if len(subset) < min_features:
                continue
            trial_config = copy.deepcopy(baseline_config)
            trial_config["enabled_features"] = subset
            trial = _train_single_config(trial_config, persist=False)
            greedy_trials.append(trial)
            if _feature_improves_on_removal(current_best_run, trial) and _is_report_better(trial, best_candidate):
                best_candidate = trial

        if best_candidate is not None:
            current_best_run = best_candidate
            greedy_changed = True

    add_back_trials: list[dict] = []
    current_features = list(current_best_run["enabled_features"])
    for derived_feature in get_derived_feature_names():
        if derived_feature in current_features:
            continue
        subset = sorted(set(current_features + [derived_feature]))
        trial_config = copy.deepcopy(baseline_config)
        trial_config["enabled_features"] = subset
        trial = _train_single_config(trial_config, persist=False)
        add_back_trials.append(trial)
        if _add_back_improves(current_best_run, trial):
            current_best_run = trial
            current_features = list(trial["enabled_features"])

    hyperparameter_trials: list[dict] = [current_best_run]
    hyperparameter_space = {
        "hidden_channels": (32, 48, 64),
        "heads": (2, 4),
        "learning_rate": (1e-3, 3e-3, 1e-2),
        "weight_decay": (1e-4, 5e-4),
        "feature_dropout": (0.15, 0.20, 0.30),
        "attention_dropout": (0.10, 0.15, 0.20),
        "negative_ratio": (1, 2, 3),
    }
    for _ in range(max(1, int(iterations))):
        trial_config = copy.deepcopy(baseline_config)
        trial_config["enabled_features"] = list(current_best_run["enabled_features"])
        trial_config["model_params"].update(
            {
                "hidden_channels": rng.choice(hyperparameter_space["hidden_channels"]),
                "heads": rng.choice(hyperparameter_space["heads"]),
                "feature_dropout": rng.choice(hyperparameter_space["feature_dropout"]),
                "attention_dropout": rng.choice(hyperparameter_space["attention_dropout"]),
            }
        )
        trial_config["training_params"].update(
            {
                "learning_rate": rng.choice(hyperparameter_space["learning_rate"]),
                "weight_decay": rng.choice(hyperparameter_space["weight_decay"]),
                "negative_ratio": rng.choice(hyperparameter_space["negative_ratio"]),
            }
        )
        trial = _train_single_config(trial_config, persist=False)
        hyperparameter_trials.append(trial)

    ranked_trials = sorted(hyperparameter_trials, key=_report_sort_key)
    best_run = ranked_trials[0]
    if apply_best:
        persisted_config = copy.deepcopy(best_run["search_config"])
        persisted_config["mode"] = "aggressive"
        best_run = _train_single_config(persisted_config, persist=True)

    baseline_loss = float(baseline_run.get("validation_loss", 0.0))
    best_loss = float(best_run.get("validation_loss", 0.0))
    loss_delta = baseline_loss - best_loss
    loss_delta_pct = 0.0 if baseline_loss == 0 else (loss_delta / baseline_loss) * 100.0

    return {
        "mode": "aggressive",
        "iterations_requested": int(iterations),
        "tested_subsets": len(graph_runs) + len(removal_trials) + len(greedy_trials) + len(add_back_trials) + len(hyperparameter_trials),
        "epochs_per_trial": int(base_config["training_params"]["epochs"]),
        "apply_best": apply_best,
        "baseline_run": baseline_run,
        "best_run": best_run,
        "loss_improvement": round(loss_delta, 4),
        "loss_improvement_pct": round(loss_delta_pct, 2),
        "mrr_improvement": round(
            float(best_run.get("validation_mrr_at_5", 0.0)) - float(baseline_run.get("validation_mrr_at_5", 0.0)),
            4,
        ),
        "recall_improvement": round(
            float(best_run.get("validation_recall_at_5", 0.0)) - float(baseline_run.get("validation_recall_at_5", 0.0)),
            4,
        ),
        "removal_wins": dropped_features,
        "top_graph_runs": [
            _trial_summary(report, rank=index + 1)
            for index, report in enumerate(top_graph_runs)
        ],
        "top_trials": [
            _trial_summary(report, rank=index + 1)
            for index, report in enumerate(ranked_trials[:5])
        ],
        "selected_features": list(best_run["enabled_features"]),
        "graph_params": dict(best_run["graph_params"]),
        "model_params": dict(best_run["model_params"]),
        "feature_stage": {
            "initial_feature_count": len(candidate_features),
            "reduced_feature_count": len(current_best_run["enabled_features"]),
            "dropped_features": [item["feature"] for item in dropped_features],
        },
    }


def get_recommendations(elder_id: int, top_k: int = 5) -> list[dict]:
    from recommendations.models import ElderProfile

    inference_features = get_enabled_features_for_inference()
    graph_params = get_graph_params_for_inference()
    graph_tensors, elder_ids = build_graph(enabled_features=inference_features, **graph_params)
    base_x, edge_index, edge_attr = graph_tensors

    if elder_id not in elder_ids:
        raise ValueError(f"ElderProfile id={elder_id} not found in graph.")

    query_idx = elder_ids.index(elder_id)
    model = _get_model()
    x = _augment_feature_tensor(base_x, inference_features)

    model.eval()
    with torch.no_grad():
        result = model(x, edge_index, edge_attr)
    embeddings = result["embeddings"]

    recommendations_raw = top_k_recommendations(embeddings, query_idx, k=top_k)
    id_to_profile = {p.id: p for p in ElderProfile.objects.filter(id__in=elder_ids)}
    query_profile = id_to_profile[elder_id]

    enriched: list[dict] = []
    combined_scores: list[float] = []
    for idx, score in recommendations_raw:
        rec_id = elder_ids[idx]
        rec_profile = id_to_profile.get(rec_id)
        if not rec_profile:
            continue
        embedding_similarity = max(0.0, min(1.0, (float(score) + 1.0) / 2.0))
        feature_similarity = max(
            0.0,
            min(
                1.0,
                (
                    _feature_similarity_from_vectors(
                        query_profile.feature_vector,
                        rec_profile.feature_vector,
                        inference_features,
                    )
                    + 1.0
                )
                / 2.0,
            ),
        )
        combined_score = (0.6 * embedding_similarity) + (0.4 * feature_similarity)
        combined_scores.append(combined_score)
        enriched.append(
            {
                "elder_id": rec_id,
                "name": rec_profile.display_name,
                "shared_interests": _shared_interests(query_profile.feature_vector, rec_profile.feature_vector),
                "raw_similarity": round(embedding_similarity, 4),
                "feature_similarity": round(feature_similarity, 4),
            }
        )

    relative_scores = _relative_scores(combined_scores)
    output = []
    for item, relative_score in zip(enriched, relative_scores):
        output.append(
            {
                "elder_id": item["elder_id"],
                "name": item["name"],
                "score": round(relative_score, 4),
                "raw_similarity": item["raw_similarity"],
                "feature_similarity": item["feature_similarity"],
                "shared_interests": item["shared_interests"],
            }
        )
    return output


def get_graph_snapshot() -> dict:
    from recommendations.models import ElderProfile

    inference_features = get_enabled_features_for_inference()
    graph_params = get_graph_params_for_inference()
    graph_tensors, elder_ids = build_graph(enabled_features=inference_features, **graph_params)
    x, edge_index, edge_attr = graph_tensors
    profiles = {
        profile.id: profile
        for profile in ElderProfile.objects.filter(id__in=elder_ids)
    }

    nodes = []
    for idx, elder_id in enumerate(elder_ids):
        profile = profiles.get(elder_id)
        if not profile:
            continue
        top_traits = sorted(
            (
                (name, profile.feature_vector.get(name, 0.5))
                for name in get_feature_names()
            ),
            key=lambda item: item[1],
            reverse=True,
        )[:3]
        nodes.append(
            {
                "id": elder_id,
                "index": idx,
                "name": profile.display_name,
                "description": profile.description,
                "top_traits": [name for name, _ in top_traits],
            }
        )

    edges = []
    for edge in edge_pairs_from_index(edge_index, edge_attr):
        edges.append(
            {
                "source_id": elder_ids[edge["source_idx"]],
                "target_id": elder_ids[edge["target_idx"]],
                "weight": edge["weight"],
                "compatibility_weight": edge.get("compatibility_weight", edge["weight"]),
                "social_weight": edge.get("social_weight", 0.0),
            }
        )

    return {
        "nodes": nodes,
        "edges": edges,
        "node_count": len(nodes),
        "edge_count": len(edges),
        "enabled_features": inference_features,
        "graph_params": graph_params,
    }
