from __future__ import annotations

import torch
import torch.nn as nn
import torch.nn.functional as F

from .feature_schema import FEATURE_DIM

try:
    from torch_geometric.nn import GATv2Conv

    HAS_TORCH_GEOMETRIC = True
except Exception:
    GATv2Conv = None
    HAS_TORCH_GEOMETRIC = False


def _reduce_edge_attr(
    edge_index: torch.Tensor,
    edge_attr: torch.Tensor | None,
    *,
    device: torch.device,
    dtype: torch.dtype,
) -> torch.Tensor:
    if edge_index.numel() == 0:
        return torch.zeros((0,), device=device, dtype=dtype)
    if edge_attr is None or edge_attr.numel() == 0:
        return torch.ones((edge_index.size(1),), device=device, dtype=dtype)
    if edge_attr.dim() == 1:
        return edge_attr.to(device=device, dtype=dtype)
    primary = edge_attr[:, 0]
    if edge_attr.size(1) > 1:
        primary = primary + (0.35 * edge_attr[:, 1])
    return primary.to(device=device, dtype=dtype)


def _with_self_loops(
    edge_index: torch.Tensor,
    edge_attr: torch.Tensor | None,
    num_nodes: int,
    *,
    device: torch.device,
    dtype: torch.dtype,
) -> tuple[torch.Tensor, torch.Tensor]:
    reduced = _reduce_edge_attr(edge_index, edge_attr, device=device, dtype=dtype)
    loops = torch.arange(num_nodes, device=device, dtype=torch.long)
    loop_index = torch.stack([loops, loops], dim=0)
    loop_attr = torch.ones((num_nodes,), device=device, dtype=dtype)
    if edge_index.numel() == 0:
        return loop_index, loop_attr
    return (
        torch.cat([edge_index.to(device=device, dtype=torch.long), loop_index], dim=1),
        torch.cat([reduced, loop_attr], dim=0),
    )


class SparseGATv2Layer(nn.Module):
    def __init__(
        self,
        in_channels: int,
        out_channels: int,
        *,
        heads: int = 1,
        concat: bool = True,
        residual: bool = True,
        attn_dropout: float = 0.0,
        negative_slope: float = 0.2,
    ):
        super().__init__()
        self.in_channels = in_channels
        self.out_channels = out_channels
        self.heads = heads
        self.concat = concat
        self.attn_dropout = attn_dropout
        self.negative_slope = negative_slope
        self.lin = nn.Linear(in_channels, heads * out_channels, bias=False)
        self.att = nn.Parameter(torch.empty((heads, out_channels)))
        self.edge_bias = nn.Linear(1, heads, bias=False)
        self.bias = nn.Parameter(torch.zeros((heads * out_channels) if concat else out_channels))
        target_dim = (heads * out_channels) if concat else out_channels
        self.residual = nn.Linear(in_channels, target_dim, bias=False) if residual else None
        self.reset_parameters()

    def reset_parameters(self) -> None:
        nn.init.xavier_uniform_(self.lin.weight)
        nn.init.xavier_uniform_(self.att)
        nn.init.xavier_uniform_(self.edge_bias.weight)
        if self.residual is not None:
            nn.init.xavier_uniform_(self.residual.weight)
        nn.init.zeros_(self.bias)

    def forward(self, x: torch.Tensor, edge_index: torch.Tensor, edge_attr: torch.Tensor | None = None) -> torch.Tensor:
        num_nodes = x.size(0)
        if num_nodes == 0:
            target_dim = self.heads * self.out_channels if self.concat else self.out_channels
            return torch.zeros((0, target_dim), device=x.device, dtype=x.dtype)

        edge_index, reduced_attr = _with_self_loops(
            edge_index,
            edge_attr,
            num_nodes,
            device=x.device,
            dtype=x.dtype,
        )
        projected = self.lin(x).view(num_nodes, self.heads, self.out_channels)
        src, dst = edge_index[0], edge_index[1]
        projected_src = projected[src]
        projected_dst = projected[dst]

        logits = F.leaky_relu(projected_src + projected_dst, negative_slope=self.negative_slope)
        logits = (logits * self.att.unsqueeze(0)).sum(dim=-1)
        logits = logits + self.edge_bias(reduced_attr.unsqueeze(-1))

        scatter_index = dst.unsqueeze(-1).expand(-1, self.heads)
        max_per_target = torch.full((num_nodes, self.heads), float("-inf"), device=x.device, dtype=logits.dtype)
        max_per_target.scatter_reduce_(0, scatter_index, logits, reduce="amax", include_self=True)
        stabilized = logits - max_per_target[dst]
        exp_logits = torch.exp(stabilized)
        denom = torch.zeros((num_nodes, self.heads), device=x.device, dtype=logits.dtype)
        denom.scatter_add_(0, scatter_index, exp_logits)
        attention = exp_logits / denom[dst].clamp_min(1e-12)
        attention = F.dropout(attention, p=self.attn_dropout, training=self.training)

        messages = attention.unsqueeze(-1) * projected_src
        out = torch.zeros((num_nodes, self.heads, self.out_channels), device=x.device, dtype=projected.dtype)
        out.index_add_(0, dst, messages)
        out = out.reshape(num_nodes, self.heads * self.out_channels) if self.concat else out.mean(dim=1)
        if self.residual is not None:
            out = out + self.residual(x)
        return out + self.bias


class ElderGAT(nn.Module):
    def __init__(
        self,
        in_channels: int = FEATURE_DIM,
        hidden_channels: int = 48,
        out_channels: int = 16,
        heads: int = 4,
        feature_dropout: float = 0.20,
        attn_dropout: float = 0.15,
    ):
        super().__init__()
        self.feature_dropout = feature_dropout
        self.conv1 = SparseGATv2Layer(
            in_channels=in_channels,
            out_channels=hidden_channels,
            heads=heads,
            concat=True,
            residual=True,
            attn_dropout=attn_dropout,
        )
        self.conv2 = SparseGATv2Layer(
            in_channels=hidden_channels * heads,
            out_channels=out_channels,
            heads=heads,
            concat=False,
            residual=True,
            attn_dropout=attn_dropout,
        )

    def encode(self, x: torch.Tensor, edge_index: torch.Tensor, edge_attr: torch.Tensor | None = None) -> torch.Tensor:
        if x.size(0) == 0:
            return x
        x = F.dropout(x, p=self.feature_dropout, training=self.training)
        x = F.elu(self.conv1(x, edge_index, edge_attr))
        x = F.dropout(x, p=self.feature_dropout, training=self.training)
        x = self.conv2(x, edge_index, edge_attr)
        return F.normalize(x, p=2, dim=-1)

    def decode(self, z: torch.Tensor, edge_index: torch.Tensor) -> torch.Tensor:
        if edge_index.numel() == 0:
            return torch.zeros((0,), device=z.device, dtype=z.dtype)
        return (z[edge_index[0]] * z[edge_index[1]]).sum(dim=-1)

    def forward(
        self,
        x: torch.Tensor,
        edge_index: torch.Tensor,
        edge_attr: torch.Tensor | None = None,
        pos_edge_index: torch.Tensor | None = None,
        neg_edge_index: torch.Tensor | None = None,
        label_smoothing: float = 0.0,
    ) -> dict:
        z = self.encode(x, edge_index, edge_attr=edge_attr)
        result = {"embeddings": z, "loss": None}
        if pos_edge_index is None or neg_edge_index is None:
            return result
        pos_scores = self.decode(z, pos_edge_index)
        neg_scores = self.decode(z, neg_edge_index)
        scores = torch.cat([pos_scores, neg_scores], dim=0)
        labels = torch.cat(
            [
                torch.ones(pos_scores.size(0), device=z.device, dtype=z.dtype),
                torch.zeros(neg_scores.size(0), device=z.device, dtype=z.dtype),
            ],
            dim=0,
        )
        if label_smoothing > 0.0:
            labels = labels * (1.0 - (2.0 * label_smoothing)) + label_smoothing
        result["loss"] = F.binary_cross_entropy_with_logits(scores, labels)
        result["bce_loss"] = result["loss"]
        return result


class OpenSourceGATRanker(nn.Module):
    def __init__(
        self,
        in_channels: int = FEATURE_DIM,
        hidden_channels: int = 64,
        out_channels: int = 24,
        heads: int = 4,
        feature_dropout: float = 0.15,
        attn_dropout: float = 0.20,
    ):
        super().__init__()
        self.feature_dropout = feature_dropout
        self.using_pyg = HAS_TORCH_GEOMETRIC
        if self.using_pyg:
            self.conv1 = GATv2Conv(
                in_channels,
                hidden_channels,
                heads=heads,
                concat=True,
                dropout=attn_dropout,
                edge_dim=2,
                residual=True,
            )
            self.norm1 = nn.LayerNorm(hidden_channels * heads)
            self.conv2 = GATv2Conv(
                hidden_channels * heads,
                hidden_channels,
                heads=heads,
                concat=True,
                dropout=attn_dropout,
                edge_dim=2,
                residual=True,
            )
            self.norm2 = nn.LayerNorm(hidden_channels * heads)
            self.conv3 = GATv2Conv(
                hidden_channels * heads,
                out_channels,
                heads=2,
                concat=False,
                dropout=max(0.0, attn_dropout - 0.05),
                edge_dim=2,
                residual=True,
            )
            self.norm3 = nn.LayerNorm(out_channels)
        else:
            self.fallback = ElderGAT(
                in_channels=in_channels,
                hidden_channels=hidden_channels,
                out_channels=out_channels,
                heads=max(1, heads // 2),
                feature_dropout=feature_dropout,
                attn_dropout=attn_dropout,
            )
        self.pair_head = nn.Sequential(
            nn.Linear(out_channels * 3, out_channels),
            nn.ELU(),
            nn.Linear(out_channels, 1),
        )

    def encode(self, x: torch.Tensor, edge_index: torch.Tensor, edge_attr: torch.Tensor | None = None) -> torch.Tensor:
        if not self.using_pyg:
            return self.fallback.encode(x, edge_index, edge_attr)
        edge_features = edge_attr
        if edge_features is None or edge_features.numel() == 0:
            edge_features = torch.ones((edge_index.size(1), 2), dtype=x.dtype, device=x.device)
        elif edge_features.dim() == 1:
            edge_features = torch.stack([edge_features, torch.zeros_like(edge_features)], dim=1)
        x = F.dropout(x, p=self.feature_dropout, training=self.training)
        x = self.conv1(x, edge_index, edge_features)
        x = self.norm1(x)
        x = F.elu(x)
        x = F.dropout(x, p=self.feature_dropout, training=self.training)
        x = self.conv2(x, edge_index, edge_features)
        x = self.norm2(x)
        x = F.elu(x)
        x = F.dropout(x, p=self.feature_dropout, training=self.training)
        x = self.conv3(x, edge_index, edge_features)
        x = self.norm3(x)
        return F.normalize(x, p=2, dim=-1)

    def decode(self, z: torch.Tensor, edge_index: torch.Tensor) -> torch.Tensor:
        if edge_index.numel() == 0:
            return torch.zeros((0,), device=z.device, dtype=z.dtype)
        left = z[edge_index[0]]
        right = z[edge_index[1]]
        pair_features = torch.cat([left, right, torch.abs(left - right)], dim=-1)
        return self.pair_head(pair_features).squeeze(-1)

    def forward(
        self,
        x: torch.Tensor,
        edge_index: torch.Tensor,
        edge_attr: torch.Tensor | None = None,
        pos_edge_index: torch.Tensor | None = None,
        neg_edge_index: torch.Tensor | None = None,
        label_smoothing: float = 0.0,
    ) -> dict:
        z = self.encode(x, edge_index, edge_attr)
        result = {"embeddings": z, "loss": None}
        if pos_edge_index is None or neg_edge_index is None:
            return result

        pos_scores = self.decode(z, pos_edge_index)
        neg_scores = self.decode(z, neg_edge_index)
        scores = torch.cat([pos_scores, neg_scores], dim=0)
        labels = torch.cat(
            [
                torch.ones(pos_scores.size(0), device=z.device, dtype=z.dtype),
                torch.zeros(neg_scores.size(0), device=z.device, dtype=z.dtype),
            ]
        )
        if label_smoothing > 0.0:
            labels = labels * (1.0 - (2.0 * label_smoothing)) + label_smoothing
        bce = F.binary_cross_entropy_with_logits(scores, labels)
        pair_count = min(pos_scores.size(0), neg_scores.size(0))
        if pair_count > 0:
            bpr = -F.logsigmoid(pos_scores[:pair_count] - neg_scores[:pair_count]).mean()
        else:
            bpr = torch.zeros((), device=z.device, dtype=z.dtype)
        result["bce_loss"] = bce
        result["bpr_loss"] = bpr
        result["loss"] = (0.6 * bce) + (0.4 * bpr)
        return result


def create_model(model_family: str = "legacy_gat", **kwargs):
    if model_family == "pyg_gatv2_ranker":
        return OpenSourceGATRanker(**kwargs)
    return ElderGAT(**kwargs)


def cosine_similarity_matrix(embeddings: torch.Tensor) -> torch.Tensor:
    if embeddings.numel() == 0:
        return torch.zeros((0, 0), device=embeddings.device, dtype=embeddings.dtype)
    return torch.mm(embeddings, embeddings.t())


def top_k_recommendations(embeddings: torch.Tensor, query_idx: int, k: int = 5, exclude_self: bool = True):
    similarity = cosine_similarity_matrix(embeddings)[query_idx].clone()
    if exclude_self:
        similarity[query_idx] = -1.0
    k = min(k, embeddings.size(0) - (1 if exclude_self else 0))
    if k <= 0:
        return []
    scores, indices = similarity.topk(k)
    return [(int(index), float(score)) for index, score in zip(indices, scores)]
