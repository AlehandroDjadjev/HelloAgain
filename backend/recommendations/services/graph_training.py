from __future__ import annotations

import copy

from recommendations.models import TrainingRun


def _confidence_from_report(report: dict) -> str:
    validation_edges = int(report.get("validation_pos_edges_used", 0))
    node_count = int(report.get("node_count", 0))
    if validation_edges < 5 or node_count < 20:
        return "low"
    return "high"


def record_training_run(
    *,
    report: dict,
    config: dict,
    model_family: str,
    promotion_status: str,
    promoted: bool,
) -> TrainingRun:
    return TrainingRun.objects.create(
        mode=str(report.get("mode", config.get("mode", "baseline"))),
        model_family=model_family,
        status="completed" if "error" not in report else "failed",
        confidence=_confidence_from_report(report),
        promotion_status=promotion_status,
        promoted=promoted,
        config=copy.deepcopy(config),
        metrics=copy.deepcopy(report),
    )
