from __future__ import annotations

from pathlib import Path

from django.core.management.base import BaseCommand

from recommendations.gat.graph_builder import build_graph, edge_pairs_from_index
from recommendations.gat.feature_schema import get_feature_names, get_recommended_core_features
from recommendations.gat.recommender import train_model
from recommendations.models import ElderProfile, SocialEdge, TrainingRun
from recommendations.services.graph_training import record_training_run
from recommendations.services.profile_ingestion import hydrate_profile_from_description
from recommendations.synthetic_profiles import generate_synthetic_profile, get_archetype_names


class Command(BaseCommand):
    help = "Reset the demo workspace, generate fresh synthetic profiles with clarification answers, and retrain the GAT model."

    def add_arguments(self, parser):
        parser.add_argument("--count", type=int, default=24)
        parser.add_argument("--epochs", type=int, default=120)
        parser.add_argument("--seed-offset", type=int, default=100)

    def handle(self, *args, **options):
        count = max(8, min(int(options["count"]), 96))
        epochs = max(20, int(options["epochs"]))
        seed_offset = int(options["seed_offset"])

        self.stdout.write("Resetting existing workspace data...")
        SocialEdge.objects.all().delete()
        TrainingRun.objects.all().delete()
        ElderProfile.objects.all().delete()
        self._clear_checkpoint()

        archetypes = get_archetype_names()
        created = []
        self.stdout.write(f"Generating {count} synthetic profiles...")
        for index in range(count):
            preferred_archetype = archetypes[index % len(archetypes)] if archetypes else None
            synthetic = generate_synthetic_profile(
                feature_names=get_feature_names(),
                seed=seed_offset + index,
                preferred_archetype=preferred_archetype,
            )
            profile = ElderProfile.objects.create(
                username=f"demo_person_{index + 1:02d}",
                display_name=f"Demo Person {index + 1:02d}",
                description=synthetic["description"],
            )
            hydrate_profile_from_description(
                profile=profile,
                description=synthetic["description"],
                clarification_answers=synthetic.get("clarification_answers") or {},
                vector_source="synthetic_seed",
            )
            created.append(profile)

        self.stdout.write("Training refreshed model checkpoint...")
        graph_params = self._select_demo_graph_params()
        report = train_model(
            epochs=epochs,
            enabled_features=get_recommended_core_features(),
            persist=True,
            mode="baseline",
            config={
                "model_family": "legacy_gat",
                "graph_params": graph_params,
            },
        )
        run = record_training_run(
            report=report,
            config={
                "epochs": epochs,
                "enabled_features": get_recommended_core_features(),
                "graph_params": graph_params,
                "mode": "baseline",
            },
            model_family="legacy_gat",
            promotion_status="promoted",
            promoted=True,
        )

        self.stdout.write(self.style.SUCCESS("Demo dataset rebuilt successfully."))
        self.stdout.write(f"Profiles created: {len(created)}")
        self.stdout.write(f"Training run id: {run.id}")
        self.stdout.write(f"Graph params: {graph_params}")
        self.stdout.write(f"Validation MRR@5: {report.get('validation_mrr_at_5', 0.0)}")
        self.stdout.write(f"Validation recall@5: {report.get('validation_recall_at_5', 0.0)}")

    def _clear_checkpoint(self) -> None:
        checkpoint_path = Path(__file__).resolve().parents[2] / "gat" / "checkpoints" / "elder_gat.pt"
        if checkpoint_path.exists():
            checkpoint_path.unlink()
        try:
            from recommendations.gat.recommender import invalidate_model_cache

            invalidate_model_cache()
        except Exception:
            pass

    def _select_demo_graph_params(self) -> dict:
        feature_subset = get_recommended_core_features()
        candidates = [
            {"use_social_edges": False, "neighbor_k": 3, "min_similarity": 0.58},
            {"use_social_edges": False, "neighbor_k": 3, "min_similarity": 0.56},
            {"use_social_edges": False, "neighbor_k": 4, "min_similarity": 0.54},
            {"use_social_edges": False, "neighbor_k": 4, "min_similarity": 0.52},
            {"use_social_edges": False, "neighbor_k": 5, "min_similarity": 0.5},
        ]
        minimum_edges = 6
        fallback = candidates[-1]
        for params in candidates:
            _, edge_index, edge_attr = build_graph(enabled_features=feature_subset, **params)[0]
            edge_count = len(edge_pairs_from_index(edge_index, edge_attr))
            if edge_count >= minimum_edges:
                return params
        return fallback
