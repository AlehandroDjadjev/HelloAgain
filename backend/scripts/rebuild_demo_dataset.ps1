param(
    [int]$Count = 24,
    [int]$Epochs = 120
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root "venv\Scripts\python.exe"

if (-not (Test-Path $python)) {
    throw "Virtual environment Python not found at $python"
}

Push-Location $root
try {
    & $python manage.py rebuild_demo_dataset --count $Count --epochs $Epochs

    @'
import os
import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "core.settings")
django.setup()

from recommendations.models import ElderProfile
from recommendations.gat.recommender import get_graph_snapshot
from recommendations.services.compatibility_engine import compare_people
from recommendations.gat.feature_schema import get_recommended_core_features

snapshot = get_graph_snapshot()
print("")
print("Graph summary")
print("node_count:", snapshot["node_count"])
print("edge_count:", snapshot["edge_count"])
print("graph_params:", snapshot["graph_params"])

profiles = list(ElderProfile.objects.order_by("id"))
if len(profiles) >= 2:
    left = profiles[0]
    features = get_recommended_core_features()
    scored = []
    for right in profiles[1:]:
        result = compare_people(
            left.feature_vector,
            right.feature_vector,
            left_confidence=left.feature_confidence,
            right_confidence=right.feature_confidence,
            graph_score=0.0,
            embedding_score=0.0,
            features=features,
        )
        scored.append((result["compatibility_score"], right.display_name, result["score_breakdown"]))
    scored.sort(key=lambda item: item[0], reverse=True)
    print("")
    print("Top 3 vs", left.display_name)
    for score, name, breakdown in scored[:3]:
        print(
            f"- {name}: score={score:.4f}, certainty={breakdown['certainty_score']:.4f}, "
            f"graph_affinity={breakdown['graph_affinity']:.4f}, distinctive={breakdown['distinctive_aligned_feature_count']}"
        )
    print("")
    print("Bottom 3 vs", left.display_name)
    for score, name, breakdown in scored[-3:]:
        print(
            f"- {name}: score={score:.4f}, certainty={breakdown['certainty_score']:.4f}, "
            f"graph_affinity={breakdown['graph_affinity']:.4f}, distinctive={breakdown['distinctive_aligned_feature_count']}"
        )
'@ | & $python -
}
finally {
    Pop-Location
}
