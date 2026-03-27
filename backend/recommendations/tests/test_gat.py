"""
tests/test_gat.py
-----------------
Unit tests for the GAT engine — covering feature schema, updater logic,
scoring calibration, and model architecture.

Run with:  venv\\Scripts\\python.exe -m pytest recommendations/tests/test_gat.py -v
"""

import math
import pytest
import torch


# ---------------------------------------------------------------------------
# Feature schema tests
# ---------------------------------------------------------------------------

class TestFeatureSchema:
    def test_feature_dim(self):
        from recommendations.gat.feature_schema import FEATURE_NAMES, FEATURE_DIM
        assert len(FEATURE_NAMES) >= 64
        assert FEATURE_DIM == len(FEATURE_NAMES)

    def test_default_vector_neutral(self):
        from recommendations.gat.feature_schema import DEFAULT_FEATURE_VECTOR, FEATURE_NAMES
        assert all(v == 0.5 for v in DEFAULT_FEATURE_VECTOR.values())
        assert set(DEFAULT_FEATURE_VECTOR.keys()) == set(FEATURE_NAMES)

    def test_vector_roundtrip(self):
        from recommendations.gat.feature_schema import (
            DEFAULT_FEATURE_VECTOR, vector_to_list, list_to_vector
        )
        as_list = vector_to_list(DEFAULT_FEATURE_VECTOR)
        back = list_to_vector(as_list)
        assert back == DEFAULT_FEATURE_VECTOR

    def test_feature_groups_cover_all(self):
        from recommendations.gat.feature_schema import FEATURE_NAMES, FEATURE_GROUPS
        grouped = [f for fs in FEATURE_GROUPS.values() for f in fs]
        assert sorted(grouped) == sorted(FEATURE_NAMES)


# ---------------------------------------------------------------------------
# Feature updater tests
# ---------------------------------------------------------------------------

class TestFeatureUpdater:
    def test_ema_shift(self):
        from recommendations.gat.feature_updater import update_feature_vector
        current = {"interest_music": 0.5}
        signals = {"interest_music": 1.0}
        updated = update_feature_vector(current, signals, alpha=0.15)
        expected = 0.85 * 0.5 + 0.15 * 1.0
        assert abs(updated["interest_music"] - expected) < 1e-6

    def test_unknown_features_ignored(self):
        from recommendations.gat.feature_updater import update_feature_vector
        current = {"extroversion": 0.5}
        signals = {"NOT_A_REAL_FEATURE": 0.9}
        updated = update_feature_vector(current, signals)
        assert "NOT_A_REAL_FEATURE" not in updated
        assert updated["extroversion"] == 0.5

    def test_clamping(self):
        from recommendations.gat.feature_updater import update_feature_vector
        current = {"positivity": 0.5}
        signals = {"positivity": 99.0}  # out of range
        updated = update_feature_vector(current, signals, alpha=0.5)
        assert 0.0 <= updated["positivity"] <= 1.0

    def test_compatibility_score_identical(self):
        """Two identical vectors (with variance) should produce near-perfect score."""
        from recommendations.gat.feature_updater import compute_compatibility_score
        from recommendations.gat.feature_schema import DEFAULT_FEATURE_VECTOR
        # Create a non-constant vector (mean-centered cosine needs variance)
        vec = dict(DEFAULT_FEATURE_VECTOR)
        vec["extroversion"] = 0.9
        vec["patience"] = 0.2
        vec["humor"] = 0.8
        score = compute_compatibility_score(vec, vec)
        assert score >= 0.99, f"Identical vectors should score ~1.0, got {score}"

    def test_compatibility_score_constant_identical(self):
        """Two constant vectors (all 0.5) should still be 1.0 (identical)."""
        from recommendations.gat.feature_updater import compute_compatibility_score
        from recommendations.gat.feature_schema import DEFAULT_FEATURE_VECTOR
        score = compute_compatibility_score(DEFAULT_FEATURE_VECTOR, DEFAULT_FEATURE_VECTOR)
        assert score >= 0.99, f"Identical constant vectors should score 1.0, got {score}"

    def test_compatibility_score_range(self):
        from recommendations.gat.feature_updater import compute_compatibility_score
        from recommendations.gat.feature_schema import DEFAULT_FEATURE_VECTOR
        other = {k: 0.0 for k in DEFAULT_FEATURE_VECTOR}
        score = compute_compatibility_score(DEFAULT_FEATURE_VECTOR, other)
        assert 0.0 <= score <= 1.0

    def test_compatibility_opposite_low(self):
        """Two opposite-direction vectors should get a low score."""
        from recommendations.gat.feature_updater import compute_compatibility_score
        # One vector high on everything, the other low
        vec_a = {"extroversion": 0.9, "patience": 0.8, "humor": 0.9, "empathy": 0.85}
        vec_b = {"extroversion": 0.1, "patience": 0.2, "humor": 0.1, "empathy": 0.15}
        score = compute_compatibility_score(
            vec_a, vec_b, features=["extroversion", "patience", "humor", "empathy"]
        )
        assert score < 0.15, f"Opposite vectors should score very low, got {score}"

    def test_compatibility_parallel_high(self):
        """Vectors with the same relative pattern should score high (Pearson)."""
        from recommendations.gat.feature_updater import compute_compatibility_score
        vec_a = {"extroversion": 0.8, "openness": 0.6, "humor": 0.9}
        vec_b = {"extroversion": 0.6, "openness": 0.4, "humor": 0.7}
        score = compute_compatibility_score(
            vec_a, vec_b, features=["extroversion", "openness", "humor"]
        )
        assert score >= 0.95, f"Parallel patterns should score high, got {score}"


# ---------------------------------------------------------------------------
# Compatibility engine tests
# ---------------------------------------------------------------------------

class TestCompatibilityEngine:
    def test_compare_people_identical(self):
        """Identical non-neutral profiles should get a high overall score."""
        from recommendations.services.compatibility_engine import compare_people
        vec = {
            "extroversion": 0.85, "patience": 0.25, "humor": 0.80,
            "empathy": 0.90, "activity_level": 0.70, "prefers_small_groups": 0.20,
        }
        result = compare_people(vec, vec)
        assert result["compatibility_score"] >= 0.75, \
            f"Identical profiles should be highly compatible, got {result['compatibility_score']}"

    def test_compare_people_opposite(self):
        """Opposite profiles should score noticeably lower."""
        from recommendations.services.compatibility_engine import compare_people
        vec_a = {
            "extroversion": 0.90, "patience": 0.20, "humor": 0.85,
            "activity_level": 0.90, "prefers_small_groups": 0.10,
        }
        vec_b = {
            "extroversion": 0.10, "patience": 0.85, "humor": 0.15,
            "activity_level": 0.10, "prefers_small_groups": 0.90,
        }
        result = compare_people(vec_a, vec_b)
        assert result["compatibility_score"] < 0.55, \
            f"Opposite profiles should score low, got {result['compatibility_score']}"

    def test_neutral_features_dont_inflate_score(self):
        """Two all-neutral vectors should not score high."""
        from recommendations.services.compatibility_engine import compare_people
        neutral = {"extroversion": 0.50, "patience": 0.50, "humor": 0.50}
        result = compare_people(neutral, neutral)
        # Neutral features are de-weighted, so the score shouldn't be inflated
        # The exact value depends on embedding/graph defaults, but it should
        # be notably lower than for genuinely matching profiles
        assert result["compatibility_score"] < 0.85, \
            f"Neutral-on-neutral shouldn't inflate score, got {result['compatibility_score']}"


# ---------------------------------------------------------------------------
# Synthetic profile diversity tests
# ---------------------------------------------------------------------------

class TestSyntheticProfiles:
    def test_different_archetypes_produce_different_vectors(self):
        """Two different archetypes should produce genuinely different feature vectors."""
        from .factories.profiles import generate_synthetic_profile
        from recommendations.gat.feature_schema import get_feature_names
        from recommendations.gat.feature_updater import compute_compatibility_score

        features = get_feature_names()
        host = generate_synthetic_profile(features, seed=1, preferred_archetype="community_host")
        storyteller = generate_synthetic_profile(features, seed=1, preferred_archetype="quiet_storyteller")

        # They should have different feature values
        diffs = [abs(host["feature_vector"].get(f, 0.5) - storyteller["feature_vector"].get(f, 0.5)) for f in features]
        max_diff = max(diffs)
        assert max_diff > 0.3, f"Different archetypes should diverge on at least some features, max diff was {max_diff}"

    def test_same_archetype_has_jitter(self):
        """Two profiles from the same archetype with different seeds should not be identical."""
        from .factories.profiles import generate_synthetic_profile
        from recommendations.gat.feature_schema import get_feature_names

        features = get_feature_names()
        a = generate_synthetic_profile(features, seed=1, preferred_archetype="community_host")
        b = generate_synthetic_profile(features, seed=2, preferred_archetype="community_host")

        diffs = [abs(a["feature_vector"].get(f, 0.5) - b["feature_vector"].get(f, 0.5)) for f in features]
        total_diff = sum(diffs)
        assert total_diff > 0.5, f"Same archetype with different seeds should have jitter, total diff = {total_diff}"


# ---------------------------------------------------------------------------
# GAT model tests
# ---------------------------------------------------------------------------

class TestGATModel:
    def _dummy_graph(self, n_nodes: int = 5):
        """Create a simple fully-connected dummy graph."""
        from recommendations.gat.feature_schema import FEATURE_DIM
        x = torch.rand(n_nodes, FEATURE_DIM)
        src = [i for i in range(n_nodes) for j in range(n_nodes) if i != j]
        dst = [j for i in range(n_nodes) for j in range(n_nodes) if i != j]
        edge_index = torch.tensor([src, dst], dtype=torch.long)
        return x, edge_index

    def test_forward_shape(self):
        from recommendations.gat.gat_model import ElderGAT
        model = ElderGAT()
        model.eval()
        x, edge_index = self._dummy_graph(5)
        with torch.no_grad():
            result = model(x, edge_index)
        embeddings = result["embeddings"]
        assert embeddings.shape == (5, 16), f"Expected (5, 16), got {embeddings.shape}"

    def test_embeddings_normalised(self):
        from recommendations.gat.gat_model import ElderGAT
        model = ElderGAT()
        model.eval()
        x, edge_index = self._dummy_graph(5)
        with torch.no_grad():
            result = model(x, edge_index)
        norms = result["embeddings"].norm(dim=-1)
        assert torch.allclose(norms, torch.ones(5), atol=1e-5), "Embeddings should be unit vectors"

    def test_loss_with_edges(self):
        from recommendations.gat.gat_model import ElderGAT
        model = ElderGAT()
        x, edge_index = self._dummy_graph(5)
        pos_edge = torch.tensor([[0, 1], [1, 0]], dtype=torch.long)
        neg_edge = torch.tensor([[0, 2], [2, 0]], dtype=torch.long)
        result = model(x, edge_index, None, pos_edge, neg_edge)
        assert result["loss"] is not None
        assert float(result["loss"].detach()) >= 0

    def test_top_k_recommendations(self):
        from recommendations.gat.gat_model import top_k_recommendations
        embeddings = torch.randn(10, 16)
        embeddings = torch.nn.functional.normalize(embeddings, dim=-1)
        recs = top_k_recommendations(embeddings, query_idx=0, k=3)
        assert len(recs) == 3
        assert all(idx != 0 for idx, _ in recs), "Self should be excluded"
        scores = [s for _, s in recs]
        assert scores == sorted(scores, reverse=True), "Should be sorted descending"

    def test_edge_weights_change_attention_output(self):
        from recommendations.gat.gat_model import ElderGAT

        torch.manual_seed(7)
        model = ElderGAT()
        model.eval()
        x, edge_index = self._dummy_graph(4)
        light_weights = torch.full((edge_index.size(1),), 0.1)
        heavy_weights = torch.full((edge_index.size(1),), 0.9)

        with torch.no_grad():
            light = model(x, edge_index, light_weights)["embeddings"]
            heavy = model(x, edge_index, heavy_weights)["embeddings"]

        assert not torch.allclose(light, heavy), "Edge weights should affect the sparse attention output"

    def test_identical_nodes_have_high_similarity(self):
        """Two identical feature vectors fed through the GAT should produce similar embeddings."""
        from recommendations.gat.gat_model import ElderGAT, cosine_similarity_matrix
        from recommendations.gat.feature_schema import FEATURE_DIM

        torch.manual_seed(42)
        model = ElderGAT()
        model.eval()

        identical_features = torch.rand(1, FEATURE_DIM)
        different_features = torch.rand(1, FEATURE_DIM)
        x = torch.cat([identical_features, identical_features, different_features], dim=0)
        edge_index = torch.tensor([[0,0,1,1,2,2],[1,2,0,2,0,1]], dtype=torch.long)

        with torch.no_grad():
            embeddings = model(x, edge_index)["embeddings"]
            sim = cosine_similarity_matrix(embeddings)

        sim_identical = float(sim[0, 1].item())
        sim_different_0 = float(sim[0, 2].item())
        sim_different_1 = float(sim[1, 2].item())
        assert sim_identical > sim_different_0 or sim_identical > sim_different_1, \
            f"Identical nodes should be more similar: identical={sim_identical:.4f}, " \
            f"diff0={sim_different_0:.4f}, diff1={sim_different_1:.4f}"


class TestTrainingHelpers:
    def test_split_positive_edges_preserves_train_coverage(self):
        from recommendations.gat.recommender import _split_positive_edges

        records = [
            {"source_idx": 0, "target_idx": 1, "weight": 0.9},
            {"source_idx": 1, "target_idx": 2, "weight": 0.8},
            {"source_idx": 2, "target_idx": 3, "weight": 0.7},
            {"source_idx": 0, "target_idx": 3, "weight": 0.6},
        ]

        split = _split_positive_edges(records, node_count=4, seed=42)
        train_nodes = {
            idx
            for record in split["train"]
            for idx in (record["source_idx"], record["target_idx"])
        }

        assert train_nodes == {0, 1, 2, 3}
        assert split["train"], "Expected non-empty train split"

    def test_ranking_metrics_use_filtered_candidates(self):
        from recommendations.gat.recommender import _ranking_metrics

        embeddings = torch.tensor(
            [
                [1.0, 0.0, 0.0],
                [0.8, 0.6, 0.0],
                [0.9, 0.0, 0.435],
            ],
            dtype=torch.float,
        )
        embeddings = torch.nn.functional.normalize(embeddings, dim=-1)
        eval_records = [{"source_idx": 0, "target_idx": 1, "weight": 0.9}]
        all_positive_records = eval_records + [{"source_idx": 0, "target_idx": 2, "weight": 0.95}]

        metrics = _ranking_metrics(
            embeddings,
            eval_records,
            all_positive_records,
            k=1,
        )

        assert metrics["mrr_at_1"] == 1.0
        assert metrics["recall_at_1"] == 1.0

    def test_negative_sampling_prefers_band_candidates(self):
        from recommendations.gat.recommender import _sample_negative_records

        compatibility = torch.tensor(
            [
                [-1.0, 0.9, 0.6, 0.2],
                [0.9, -1.0, 0.55, 0.4],
                [0.6, 0.55, -1.0, 0.7],
                [0.2, 0.4, 0.7, -1.0],
            ]
        )
        negatives = _sample_negative_records(
            compatibility,
            known_positive_pairs={(0, 1)},
            sample_size=2,
            lower=0.45,
            upper=0.75,
        )

        sampled_pairs = {
            tuple(sorted((record["source_idx"], record["target_idx"])))
            for record in negatives
        }
        assert sampled_pairs.issubset({(0, 2), (1, 2), (2, 3)})
