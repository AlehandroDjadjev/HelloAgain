"""
Build graph tensors for GAT training and inference.

Uses cosine-similarity KNN to construct the graph — each node connects
to its top-K most similar neighbours.  No mutual-KNN requirement, no
certainty gating, no heuristic filters.  This is the standard approach
used in GraphSAGE / DGL / PyG link-prediction examples.
"""

from __future__ import annotations

import torch
import torch.nn.functional as F

from .feature_schema import get_feature_names, vector_to_list


def build_graph(
    use_social_edges: bool = True,
    enabled_features: list[str] | None = None,
    neighbor_k: int = 5,
    min_similarity: float = 0.15,
) -> tuple[tuple[torch.Tensor, torch.Tensor, torch.Tensor], list[int]]:
    from recommendations.models import ElderProfile, SocialEdge

    feature_names = get_feature_names()
    profiles = list(ElderProfile.objects.all().order_by("id"))
    if not profiles:
        return (
            torch.zeros((0, len(feature_names)), dtype=torch.float),
            torch.zeros((2, 0), dtype=torch.long),
            torch.zeros((0, 2), dtype=torch.float),
        ), []

    elder_ids = [profile.id for profile in profiles]
    id_to_idx = {profile.id: index for index, profile in enumerate(profiles)}

    x = torch.tensor(
        [vector_to_list(profile.feature_vector, feature_names=feature_names) for profile in profiles],
        dtype=torch.float,
    )
    x_for_graph = mask_feature_tensor(x, enabled_features)

    if use_social_edges:
        edges = list(SocialEdge.objects.filter(elder_a__in=profiles, elder_b__in=profiles))
    else:
        edges = []

    if edges:
        src: list[int] = []
        dst: list[int] = []
        weights: list[list[float]] = []
        for edge in edges:
            left_idx, right_idx = id_to_idx[edge.elder_a_id], id_to_idx[edge.elder_b_id]
            # Use direct cosine similarity for the edge weight
            left_vec = x_for_graph[left_idx]
            right_vec = x_for_graph[right_idx]
            cosine = float(F.cosine_similarity(left_vec.unsqueeze(0), right_vec.unsqueeze(0)).item())
            compatibility = max(0.0, cosine)
            src.extend([left_idx, right_idx])
            dst.extend([right_idx, left_idx])
            weights.extend([[compatibility, edge.gat_weight], [compatibility, edge.gat_weight]])
        edge_index = torch.tensor([src, dst], dtype=torch.long)
        edge_attr = torch.tensor(weights, dtype=torch.float)
    else:
        edge_index, edge_attr = _build_sparse_similarity_graph(
            x_for_graph,
            profiles,
            neighbor_k=neighbor_k,
            min_similarity=min_similarity,
        )

    return (x, edge_index, edge_attr), elder_ids


def _build_sparse_similarity_graph(
    x: torch.Tensor,
    profiles: list,
    neighbor_k: int = 5,
    min_similarity: float = 0.15,
) -> tuple[torch.Tensor, torch.Tensor]:
    """Build a KNN graph using cosine similarity.

    Each node connects to its top-K most similar neighbours (no mutual
    requirement).  Edges are symmetric: if A→B exists then B→A exists.
    """
    node_count = x.size(0)
    if node_count <= 1:
        return torch.zeros((2, 0), dtype=torch.long), torch.zeros((0, 2), dtype=torch.float)

    # Cosine similarity matrix
    normed = F.normalize(x, dim=1)
    sim = torch.mm(normed, normed.t())
    sim.fill_diagonal_(-1.0)  # exclude self-loops

    top_k = max(1, min(neighbor_k, node_count - 1))

    seen_edges: dict[tuple[int, int], float] = {}
    for source_idx in range(node_count):
        scores, indices = sim[source_idx].topk(top_k)
        for score_val, target_idx_tensor in zip(scores.tolist(), indices.tolist()):
            target_idx = int(target_idx_tensor)
            if score_val < min_similarity:
                continue
            key = _pair_key(source_idx, target_idx)
            seen_edges[key] = max(float(score_val), seen_edges.get(key, -1.0))

    src: list[int] = []
    dst: list[int] = []
    weights: list[list[float]] = []
    for (source_idx, target_idx), weight in seen_edges.items():
        src.extend([source_idx, target_idx])
        dst.extend([target_idx, source_idx])
        weights.extend([[weight, 0.0], [weight, 0.0]])

    if not src:
        return torch.zeros((2, 0), dtype=torch.long), torch.zeros((0, 2), dtype=torch.float)

    return torch.tensor([src, dst], dtype=torch.long), torch.tensor(weights, dtype=torch.float)


def edge_pairs_from_index(edge_index: torch.Tensor, edge_attr: torch.Tensor | None = None) -> list[dict]:
    seen: dict[tuple[int, int], dict] = {}
    if edge_index.numel() == 0:
        return []

    weights = edge_attr.tolist() if edge_attr is not None and edge_attr.numel() else None
    for idx in range(edge_index.size(1)):
        source_idx = int(edge_index[0, idx].item())
        target_idx = int(edge_index[1, idx].item())
        if source_idx == target_idx:
            continue
        key = _pair_key(source_idx, target_idx)
        raw = [1.0, 0.0] if weights is None else weights[idx]
        compatibility_weight = float(raw[0]) if isinstance(raw, list) else float(raw)
        social_weight = float(raw[1]) if isinstance(raw, list) and len(raw) > 1 else 0.0
        seen[key] = {
            "source_idx": key[0],
            "target_idx": key[1],
            "weight": round(compatibility_weight, 4),
            "compatibility_weight": round(compatibility_weight, 4),
            "social_weight": round(social_weight, 4),
        }
    return list(seen.values())


def _pair_key(source_idx: int, target_idx: int) -> tuple[int, int]:
    return (source_idx, target_idx) if source_idx < target_idx else (target_idx, source_idx)


def mask_feature_tensor(x: torch.Tensor, enabled_features: list[str] | None) -> torch.Tensor:
    if not enabled_features:
        return x

    enabled = set(enabled_features)
    masked = x.clone()
    for index, feature_name in enumerate(get_feature_names()):
        if feature_name not in enabled:
            masked[:, index] = 0.0
    return masked
