import json
import sys
import types
from unittest.mock import Mock, patch

from django.test import TestCase

from recommendations.gat.feature_schema import reset_custom_features
from recommendations.models import ElderProfile


class ApiViewTests(TestCase):
    def setUp(self):
        reset_custom_features()
        ElderProfile.objects.create(
            username="alpha",
            display_name="Alpha",
            description="Warm and social.",
        )
        ElderProfile.objects.create(
            username="beta",
            display_name="Beta",
            description="Calm and reflective.",
        )

    def tearDown(self):
        reset_custom_features()

    @patch("recommendations.views._get_ollama_status")
    def test_health_status_returns_structured_json(self, mock_ollama_status):
        mock_ollama_status.return_value = {
            "reachable": False,
            "models": [],
            "error": "offline",
        }

        response = self.client.get("/api/health/")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["backend"], "ok")
        self.assertEqual(payload["database"]["elders"], 2)
        self.assertIn("checkpoint_exists", payload["model"])
        self.assertIn("active_checkpoint", payload["model"])
        self.assertFalse(payload["ollama"]["reachable"])
        self.assertIn("recommended_core_count", payload["schema"])

    @patch("recommendations.views._get_ollama_status")
    def test_seed_returns_502_when_model_missing(self, mock_ollama_status):
        mock_ollama_status.return_value = {
            "reachable": True,
            "models": [],
        }

        response = self.client.post(
            "/api/seed/",
            data=json.dumps({"model": "llama3.2:1b"}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 502)
        payload = response.json()
        self.assertEqual(payload["code"], "OLLAMA_MODEL_MISSING")
        self.assertEqual(payload["details"]["suggested_model"], "llama3.2:1b")
        self.assertEqual(ElderProfile.objects.count(), 2)

    def test_train_passes_enabled_features_and_returns_curve(self):
        train_model = Mock(
            return_value={
                "mode": "baseline",
                "trained_epochs": 7,
                "final_loss": 0.3123,
                "validation_loss": 0.4012,
                "test_loss": 0.4221,
                "loss_curve": [0.9, 0.4, 0.3123],
                "enabled_features": ["extroversion", "openness"],
                "disabled_features": ["humor"],
                "derived_features_used": [],
                "feature_count": 2,
                "pos_edges_used": 4,
                "neg_edges_used": 2,
                "node_count": 2,
                "edge_count": 1,
                "validation_mrr_at_5": 0.5,
                "validation_recall_at_5": 1.0,
                "test_mrr_at_5": 0.5,
                "test_recall_at_5": 1.0,
                "graph_params": {"neighbor_k": 3, "min_similarity": 0.56},
                "model_params": {"hidden_channels": 48, "heads": 4},
                "accuracy": 0.75,
                "precision": 0.8,
                "recall": 0.6667,
                "f1_score": 0.7273,
                "roc_auc": 0.81,
                "positive_rate": 0.5,
                "homophily_ratio": 0.6,
                "mean_edge_similarity": 0.71,
                "inference_latency_ms": 2.4,
                "ms_per_edge": 0.12,
            }
        )
        fake_module = types.SimpleNamespace(train_model=train_model)

        with patch.dict(sys.modules, {"recommendations.gat.recommender": fake_module}):
            response = self.client.post(
                "/api/train/",
                data=json.dumps(
                    {
                        "epochs": 7,
                        "mode": "baseline",
                        "enabled_features": ["extroversion", "openness"],
                    }
                ),
                content_type="application/json",
            )

        self.assertEqual(response.status_code, 200)
        train_model.assert_called_once_with(
            epochs=7,
            enabled_features=["extroversion", "openness"],
            persist=True,
            mode="baseline",
            config={"model_family": "legacy_gat"},
        )
        self.assertEqual(response.json()["loss_curve"], [0.9, 0.4, 0.3123])
        self.assertEqual(response.json()["validation_mrr_at_5"], 0.5)

    def test_feature_search_returns_best_trial_payload(self):
        search_feature_combinations = Mock(
            return_value={
                "mode": "aggressive",
                "iterations_requested": 6,
                "tested_subsets": 6,
                "epochs_per_trial": 40,
                "apply_best": True,
                "loss_improvement": 0.082,
                "loss_improvement_pct": 12.5,
                "mrr_improvement": 0.1,
                "recall_improvement": 0.2,
                "removal_wins": [
                    {"feature": "humor", "wins": 3, "mean_loss_gain": 0.031}
                ],
                "baseline_run": {
                    "trained_epochs": 40,
                    "final_loss": 0.644,
                    "validation_loss": 0.55,
                    "loss_curve": [0.9, 0.7, 0.644],
                    "enabled_features": ["extroversion", "openness", "humor"],
                    "disabled_features": [],
                    "derived_features_used": [],
                    "feature_count": 3,
                    "pos_edges_used": 4,
                    "neg_edges_used": 2,
                    "node_count": 2,
                    "edge_count": 1,
                    "validation_mrr_at_5": 0.4,
                    "validation_recall_at_5": 0.8,
                    "test_mrr_at_5": 0.38,
                    "test_recall_at_5": 0.75,
                    "graph_params": {"neighbor_k": 3, "min_similarity": 0.56},
                    "model_params": {"hidden_channels": 48, "heads": 4},
                    "accuracy": 0.75,
                    "precision": 0.8,
                    "recall": 0.6667,
                    "f1_score": 0.7273,
                    "roc_auc": 0.81,
                    "positive_rate": 0.5,
                    "homophily_ratio": 0.6,
                    "mean_edge_similarity": 0.71,
                    "inference_latency_ms": 2.4,
                    "ms_per_edge": 0.12,
                },
                "best_run": {
                    "trained_epochs": 40,
                    "final_loss": 0.562,
                    "validation_loss": 0.44,
                    "loss_curve": [0.88, 0.65, 0.562],
                    "enabled_features": ["extroversion", "openness"],
                    "disabled_features": ["humor"],
                    "derived_features_used": [],
                    "feature_count": 2,
                    "pos_edges_used": 4,
                    "neg_edges_used": 2,
                    "node_count": 2,
                    "edge_count": 1,
                    "validation_mrr_at_5": 0.5,
                    "validation_recall_at_5": 1.0,
                    "test_mrr_at_5": 0.46,
                    "test_recall_at_5": 0.9,
                    "graph_params": {"neighbor_k": 3, "min_similarity": 0.56},
                    "model_params": {"hidden_channels": 48, "heads": 4},
                    "accuracy": 0.83,
                    "precision": 0.83,
                    "recall": 0.83,
                    "f1_score": 0.83,
                    "roc_auc": 0.86,
                    "positive_rate": 0.5,
                    "homophily_ratio": 0.64,
                    "mean_edge_similarity": 0.74,
                    "inference_latency_ms": 2.2,
                    "ms_per_edge": 0.11,
                },
                "top_trials": [
                    {
                        "rank": 1,
                        "final_loss": 0.562,
                        "validation_loss": 0.44,
                        "validation_mrr_at_5": 0.5,
                        "validation_recall_at_5": 1.0,
                        "test_mrr_at_5": 0.46,
                        "test_recall_at_5": 0.9,
                        "roc_auc": 0.86,
                        "f1_score": 0.83,
                        "feature_count": 2,
                        "enabled_features": ["extroversion", "openness"],
                        "disabled_features": ["humor"],
                        "derived_features_used": [],
                        "graph_params": {"neighbor_k": 3, "min_similarity": 0.56},
                        "model_params": {"hidden_channels": 48, "heads": 4},
                    }
                ],
            }
        )
        fake_module = types.SimpleNamespace(
            search_feature_combinations=search_feature_combinations
        )

        with patch.dict(sys.modules, {"recommendations.gat.recommender": fake_module}):
            response = self.client.post(
                "/api/feature-search/",
                data=json.dumps(
                    {
                        "epochs": 40,
                        "iterations": 6,
                        "enabled_features": ["extroversion", "openness", "humor"],
                    }
                ),
                content_type="application/json",
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["best_run"]["final_loss"], 0.562)
        self.assertEqual(search_feature_combinations.call_count, 3)
        search_feature_combinations.assert_any_call(
            epochs=40,
            iterations=6,
            base_enabled_features=["extroversion", "openness", "humor"],
            min_features=4,
            apply_best=False,
            config={"model_family": "pyg_gatv2_ranker", "iterations": 6, "min_features": 4},
        )

    def test_train_supports_aggressive_mode(self):
        train_model = Mock(
            return_value={
                "mode": "aggressive",
                "best_run": {"validation_mrr_at_5": 0.6},
                "top_trials": [],
            }
        )
        fake_module = types.SimpleNamespace(train_model=train_model)

        with patch.dict(sys.modules, {"recommendations.gat.recommender": fake_module}):
            response = self.client.post(
                "/api/train/",
                data=json.dumps(
                    {
                        "mode": "aggressive",
                        "epochs": 30,
                        "config": {"training_params": {"negative_ratio": 3}},
                    }
                ),
                content_type="application/json",
            )

        self.assertEqual(response.status_code, 200)
        train_model.assert_called_once_with(
            epochs=30,
            enabled_features=None,
            persist=True,
            mode="aggressive",
            config={"training_params": {"negative_ratio": 3}, "model_family": "legacy_gat"},
        )
        self.assertEqual(response.json()["mode"], "aggressive")

    @patch("recommendations.views._create_seeded_profile")
    def test_seed_batch_returns_created_profiles(self, mock_create_seeded_profile):
        created_one = ElderProfile(
            id=3,
            username="gamma",
            display_name="Gamma",
            description="Friendly and patient.",
            feature_vector={},
        )
        created_two = ElderProfile(
            id=4,
            username="delta",
            display_name="Delta",
            description="Reflective and kind.",
            feature_vector={},
        )
        mock_create_seeded_profile.side_effect = [created_one, created_two]

        response = self.client.post(
            "/api/seed/batch/",
            data=json.dumps({"count": 2, "model": "llama3.2:1b"}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 201)
        payload = response.json()
        self.assertEqual(payload["count"], 2)
        self.assertEqual(len(payload["profiles"]), 2)
        self.assertEqual(payload["profiles"][0]["display_name"], "Gamma")

    def test_graph_snapshot_returns_payload(self):
        get_graph_snapshot = Mock(
            return_value={
                "nodes": [
                    {
                        "id": 1,
                        "index": 0,
                        "name": "Alpha",
                        "description": "Warm and social.",
                        "top_traits": ["extroversion", "humor"],
                    }
                ],
                "edges": [
                    {"source_id": 1, "target_id": 2, "weight": 0.88},
                ],
                "node_count": 2,
                "edge_count": 1,
                "enabled_features": ["extroversion", "humor"],
            }
        )
        fake_module = types.SimpleNamespace(get_graph_snapshot=get_graph_snapshot)

        with patch.dict(sys.modules, {"recommendations.gat.recommender": fake_module}):
            response = self.client.get("/api/graph/")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["node_count"], 2)
        self.assertEqual(payload["edges"][0]["weight"], 0.88)

    def test_custom_feature_can_be_created_and_removed(self):
        create_response = self.client.post(
            "/api/schema/features/",
            data=json.dumps({"name": "loves_chess", "group": "Lifestyle"}),
            content_type="application/json",
        )
        self.assertEqual(create_response.status_code, 201)
        create_payload = create_response.json()
        self.assertIn("loves_chess", create_payload["schema"]["feature_names"])

        schema_response = self.client.get("/api/schema/")
        self.assertEqual(schema_response.status_code, 200)
        self.assertIn("loves_chess", schema_response.json()["feature_names"])

        delete_response = self.client.delete("/api/schema/features/loves_chess/")
        self.assertEqual(delete_response.status_code, 200)
        self.assertNotIn("loves_chess", delete_response.json()["schema"]["feature_names"])

    def test_reset_workspace_clears_profiles(self):
        response = self.client.post(
            "/api/reset/",
            data=json.dumps({}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(ElderProfile.objects.count(), 0)

    def test_create_elder_extracts_feature_vector_from_description(self):
        response = self.client.post(
            "/api/elders/",
            data=json.dumps(
                {
                    "display_name": "Gamma",
                    "description": "Warm, talkative, funny, and happiest in quiet one-on-one conversations about family and old songs.",
                }
            ),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 201)
        payload = response.json()
        self.assertEqual(payload["vector_source"], "description_hybrid")
        self.assertIn("feature_confidence", payload)
        self.assertGreater(payload["feature_vector"]["emotional_warmth"], 0.5)
        self.assertIn("dominant_traits", payload)

    def test_intake_preview_returns_clarification_for_vague_description(self):
        response = self.client.post(
            "/api/elders/intake-preview/",
            data=json.dumps(
                {
                    "display_name": "Preview",
                    "description": "Likes company sometimes.",
                }
            ),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "needs_clarification")
        self.assertGreaterEqual(len(payload["questions"]), 3)
        self.assertIn("placeholder", payload["questions"][0])
        self.assertIn("help_text", payload["questions"][0])

    def test_create_elder_accepts_free_text_clarification_answers(self):
        response = self.client.post(
            "/api/elders/",
            data=json.dumps(
                {
                    "display_name": "Clarified",
                    "description": "Likes company sometimes.",
                    "clarification_answers": {
                        "preferred_company": "She prefers one-to-one company and quiet visits.",
                        "conversation_style": "She is quiet, thoughtful, and a good listener.",
                        "shared_time": "Home visits and tea at home suit her best.",
                        "pace_and_routine": "She likes a steady routine and a calm pace.",
                    },
                }
            ),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 201)
        payload = response.json()
        self.assertGreater(payload["feature_confidence"]["prefers_small_groups"], 0.5)
        self.assertGreater(payload["feature_vector"]["prefers_small_groups"], 0.5)
        self.assertIn("clarification_answers", payload["extraction_evidence"])

    def test_compare_endpoint_returns_structured_breakdown(self):
        response = self.client.post(
            "/api/compare/",
            data=json.dumps({"left_id": 1, "right_id": 2}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIn("compatibility_score", payload)
        self.assertIn("score_breakdown", payload)
        self.assertIn("top_matches", payload)
        self.assertIn("top_mismatches", payload)
