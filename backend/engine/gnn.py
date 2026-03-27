from dataclasses import dataclass
from typing import Dict, List

import torch
import torch.nn as nn
import torch.nn.functional as F


@dataclass
class GraphTensors:
    user_vector: torch.Tensor
    action_matrix: torch.Tensor
    user_action_weights: torch.Tensor
    user_attr_weights: torch.Tensor
    action_attr_weights: torch.Tensor
    attribute_names: List[str]
    action_names: List[str]


class WeightedGraphLayer(nn.Module):
    def __init__(self, hidden_dim: int) -> None:
        super().__init__()
        self.self_user = nn.Linear(hidden_dim, hidden_dim)
        self.self_action = nn.Linear(hidden_dim, hidden_dim)
        self.self_attr = nn.Linear(hidden_dim, hidden_dim)
        self.user_to_attr = nn.Linear(hidden_dim, hidden_dim, bias=False)
        self.attr_to_user = nn.Linear(hidden_dim, hidden_dim, bias=False)
        self.action_to_attr = nn.Linear(hidden_dim, hidden_dim, bias=False)
        self.attr_to_action = nn.Linear(hidden_dim, hidden_dim, bias=False)
        self.user_to_action = nn.Linear(hidden_dim, hidden_dim, bias=False)
        self.action_to_user = nn.Linear(hidden_dim, hidden_dim, bias=False)
        self.norm = nn.LayerNorm(hidden_dim)

    def forward(self, user_h, action_h, attr_h, graph: GraphTensors):
        user_from_attr = torch.matmul(graph.user_attr_weights.unsqueeze(0), self.attr_to_user(attr_h))
        user_from_action = torch.matmul(graph.user_action_weights.unsqueeze(0), self.action_to_user(action_h))
        new_user = self.self_user(user_h) + user_from_attr + user_from_action

        attr_from_user = graph.user_attr_weights.unsqueeze(1) * self.user_to_attr(user_h)
        attr_from_action = torch.matmul(graph.action_attr_weights.t(), self.action_to_attr(action_h))
        new_attr = self.self_attr(attr_h) + attr_from_user + attr_from_action

        action_from_attr = torch.matmul(graph.action_attr_weights, self.attr_to_action(attr_h))
        action_from_user = graph.user_action_weights.unsqueeze(1) * self.user_to_action(user_h)
        new_action = self.self_action(action_h) + action_from_attr + action_from_user

        new_user = self.norm(F.gelu(new_user))
        new_attr = self.norm(F.gelu(new_attr))
        new_action = self.norm(F.gelu(new_action))
        return new_user, new_action, new_attr


class PreferenceGNN(nn.Module):
    def __init__(self, attr_dim: int, hidden_dim: int = 96, layers: int = 3) -> None:
        super().__init__()
        self.hidden_dim = hidden_dim
        self.user_encoder = nn.Linear(attr_dim, hidden_dim)
        self.action_encoder = nn.Linear(attr_dim, hidden_dim)
        self.attribute_embedding = nn.Embedding(attr_dim, hidden_dim)
        self.layers = nn.ModuleList([WeightedGraphLayer(hidden_dim) for _ in range(layers)])
        self.scorer = nn.Sequential(
            nn.Linear(hidden_dim * 2, hidden_dim),
            nn.GELU(),
            nn.Linear(hidden_dim, 1),
        )

    def forward(self, graph: GraphTensors) -> Dict[str, torch.Tensor]:
        device = next(self.parameters()).device
        graph = GraphTensors(
            user_vector=graph.user_vector.to(device),
            action_matrix=graph.action_matrix.to(device),
            user_action_weights=graph.user_action_weights.to(device),
            user_attr_weights=graph.user_attr_weights.to(device),
            action_attr_weights=graph.action_attr_weights.to(device),
            attribute_names=graph.attribute_names,
            action_names=graph.action_names,
        )
        user_h = self.user_encoder(graph.user_vector.unsqueeze(0))
        action_h = self.action_encoder(graph.action_matrix)
        attr_indices = torch.arange(len(graph.attribute_names), device=device)
        attr_h = self.attribute_embedding(attr_indices)

        for layer in self.layers:
            user_h, action_h, attr_h = layer(user_h, action_h, attr_h, graph)

        repeated_user = user_h.repeat(action_h.shape[0], 1)
        scores = self.scorer(torch.cat([repeated_user, action_h], dim=-1)).squeeze(-1)
        dot_scores = (repeated_user * action_h).sum(dim=-1)
        return {
            "user_embedding": user_h.squeeze(0),
            "action_embeddings": action_h,
            "scores": 0.5 * scores + 0.5 * dot_scores,
        }


class OnlineTrainer:
    def __init__(self, model: PreferenceGNN, learning_rate: float = 2e-3) -> None:
        self.model = model
        self.optimizer = torch.optim.Adam(self.model.parameters(), lr=learning_rate)

    def train_positive_edge(self, graph: GraphTensors, positive_index: int, epochs: int = 10) -> None:
        if len(graph.action_names) < 2:
            return
        device = next(self.model.parameters()).device
        positive = torch.tensor([positive_index], dtype=torch.long, device=device)
        negative = torch.tensor(
            [idx for idx in range(len(graph.action_names)) if idx != positive_index][: min(4, len(graph.action_names) - 1)],
            dtype=torch.long,
            device=device,
        )
        if negative.numel() == 0:
            return

        self.model.train()
        for _ in range(epochs):
            outputs = self.model(graph)
            scores = outputs["scores"]
            pos_score = scores[positive].mean()
            neg_score = scores[negative].mean()
            loss = F.softplus(-(pos_score - neg_score))
            self.optimizer.zero_grad()
            loss.backward()
            self.optimizer.step()
