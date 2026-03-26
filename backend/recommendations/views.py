from __future__ import annotations

import json
import random
import importlib.util
from pathlib import Path

from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from .models import ElderProfile, SocialEdge, TrainingRun
from .serializers import ElderProfileCreateSerializer, ElderProfileSerializer, TrainingRunSerializer
from .gat.feature_schema import (
    get_default_feature_vector,
    get_feature_details,
    get_feature_groups,
    get_feature_names,
    get_recommended_core_features,
    normalize_feature_name,
)
from .services.compatibility_engine import compare_people, dominant_traits
from .services.feature_extraction import extract_feature_profile, extraction_to_vectors
from .services.profile_ingestion import apply_interaction_signals, hydrate_profile_from_description
from .services.recommendation_explainer import explain_recommendation
from .services.graph_training import record_training_run

# --- Helpers ---

def _json_ok(data: dict, status: int = 200) -> JsonResponse:
    return JsonResponse(data, status=status)

def _json_error(message: str, status: int = 400, code: str | None = None, details: dict | None = None) -> JsonResponse:
    payload = {"status": "error", "message": message}
    if code: payload["code"] = code
    if details: payload["details"] = details
    return JsonResponse(payload, status=status)

def _parse_body(request) -> dict:
    if not request.body: return {}
    return json.loads(request.body)

def _generate_unique_identity() -> tuple[str, str]:
    """Generates a professional default identity for new users."""
    adjectives = ["Wise", "Kind", "Active", "Gentle", "Bright", "Peaceful", "Strong", "Caring"]
    nouns = ["Mentor", "Guide", "Pioneer", "Guardian", "Pillar", "Beacon", "Champion"]
    display_name = f"{random.choice(adjectives)} {random.choice(nouns)}"
    username = normalize_feature_name(display_name) + str(random.randint(10, 99))
    return display_name, username

# --- Core API Views ---

@csrf_exempt
@require_http_methods(["GET", "POST"])
def elders_collection(request):
    """
    GET: List all elder profiles.
    POST: Register a new elder. If a 'description' is provided, features are automatically extracted.
    """
    if request.method == "GET":
        profiles = ElderProfile.objects.all().order_by("-id")
        return _json_ok({"elders": ElderProfileSerializer(profiles, many=True).data})

    # POST: Registration
    try:
        body = _parse_body(request)
    except json.JSONDecodeError:
        return _json_error("Invalid JSON body.")

    # Use serializer for validation, but handle 'description' logic manually
    serializer = ElderProfileCreateSerializer(data=body)
    if not serializer.is_valid():
        return JsonResponse({"errors": serializer.errors}, status=400)

    display_name = body.get("display_name")
    username = body.get("username")
    description = body.get("description", "").strip()

    if not display_name or not username:
        auto_name, auto_user = _generate_unique_identity()
        display_name = display_name or auto_name
        username = username or auto_user

    if ElderProfile.objects.filter(username=username).exists():
        username = f"{username}_{random.randint(10, 99)}"

    # Create the base profile
    profile = ElderProfile.objects.create(
        username=username,
        display_name=display_name,
        description=description,
    )

    # Automatically analyze description to populate features
    if description:
        hydrate_profile_from_description(
            profile=profile,
            description=description,
            manual_overrides=body.get("manual_overrides", {}),
            vector_source="onboarding_description",
        )

    return _json_ok(ElderProfileSerializer(profile).data, status=201)

@require_http_methods(["GET"])
def profile_detail(request, elder_id: int):
    """Retrieve a single profile."""
    profile = get_object_or_404(ElderProfile, pk=elder_id)
    return _json_ok(ElderProfileSerializer(profile).data)

@csrf_exempt
@require_http_methods(["PUT", "PATCH"])
def update_profile(request, elder_id: int):
    """Update profile details or features."""
    profile = get_object_or_404(ElderProfile, pk=elder_id)
    try:
        body = _parse_body(request)
    except json.JSONDecodeError:
        return _json_error("Invalid JSON body.")

    # Update basic info
    if "display_name" in body: profile.display_name = str(body["display_name"])[:120]
    if "description" in body: profile.description = str(body["description"])

    # If description changed significantly, optionally re-run extraction
    # Here we just save the new values
    profile.save()

    # Apply feature overrides if provided
    manual_overrides = body.get("manual_overrides") or body.get("feature_vector")
    if isinstance(manual_overrides, dict):
        current_vector = profile.feature_vector or {}
        for k, v in manual_overrides.items():
            if k in get_feature_names():
                current_vector[k] = float(max(0.0, min(1.0, v)))
        profile.feature_vector = current_vector
        profile.save()

    return _json_ok(ElderProfileSerializer(profile).data)

@csrf_exempt
@require_http_methods(["POST"])
def update_features(request, elder_id: int):
    """
    Apply behavioral signals (EMA) to update the adapted feature vector.
    Used for long-term learning from user interactions.
    """
    profile = get_object_or_404(ElderProfile, pk=elder_id)
    try:
        body = _parse_body(request)
    except json.JSONDecodeError:
        return _json_error("Invalid JSON body.")

    signals = body.get("signals", {})
    alpha = float(body.get("alpha", 0.12)) # Default learning rate
    apply_interaction_signals(profile=profile, signals=signals, alpha=alpha)
    
    return _json_ok(ElderProfileSerializer(profile).data)

@csrf_exempt
@require_http_methods(["POST"])
def compare_users(request):
    """
    Explicitly compare two users (or description vs user) for compatibility.
    This is the core 'Judging' engine functionality.
    """
    try:
        body = _parse_body(request)
    except json.JSONDecodeError:
        return _json_error("Invalid JSON body.")

    # Extract vectors for comparison
    def get_vec_and_conf(uid=None, desc=None):
        if uid:
            p = get_object_or_404(ElderProfile, pk=int(uid))
            return p.feature_vector, p.feature_confidence, p
        elif desc:
            ext = extract_feature_profile(str(desc))
            _, vec, conf, _, _ = extraction_to_vectors(ext)
            return vec, conf, None
        return None, None, None

    v_left, c_left, p_left = get_vec_and_conf(body.get("left_id"), body.get("left_description"))
    v_right, c_right, p_right = get_vec_and_conf(body.get("right_id"), body.get("right_description"))

    if not v_left or not v_right:
        return _json_error("Provide left_id/description and right_id/description.")

    # Perform comparison
    comparison = compare_people(v_left, v_right, left_confidence=c_left, right_confidence=c_right)
    
    # Add metadata for response
    comparison["left"] = ElderProfileSerializer(p_left).data if p_left else {"description": body.get("left_description")}
    comparison["right"] = ElderProfileSerializer(p_right).data if p_right else {"description": body.get("right_description")}
    
    return _json_ok(comparison)

@require_http_methods(["GET"])
def find_friends(request, elder_id: int):
    """
    Find most compatible friends using GAT model embeddings.
    This is the core 'Recommendation' functionality.
    """
    profile = get_object_or_404(ElderProfile, pk=elder_id)
    top_k = max(1, min(int(request.GET.get("k", 5)), 20))

    try:
        from .gat.recommender import get_embedding_snapshot
        snapshot = get_embedding_snapshot()
        
        if elder_id not in snapshot["elder_ids"]:
            return _json_error("Profile not found in recommendation graph. Try training the model.", status=404)

        query_idx = snapshot["elder_ids"].index(elder_id)
        query_emb = snapshot["embeddings"][query_idx]
        
        recommendations = []
        for i, target_id in enumerate(snapshot["elder_ids"]):
            if target_id == elder_id: continue
            
            target_profile = snapshot["profiles"].get(target_id)
            if not target_profile: continue
            
            # Simple dot-product similarity in GAT embedding space
            similarity = float((query_emb * snapshot["embeddings"][i]).sum().item())
            graph_score = max(0.0, min(1.0, (similarity + 1.0) / 2.0))
            
            # Use explainer for rich feedback
            explanation = explain_recommendation(
                query_profile=profile,
                candidate_profile=target_profile,
                graph_score=graph_score,
                embedding_score=graph_score
            )
            
            recommendations.append({
                "elder_id": target_id,
                "name": target_profile.display_name,
                "score": explanation["compatibility_score"],
                "why_they_match": explanation["why_they_match"],
                "shared_interests": explanation["shared_interests"]
            })
            
        recommendations.sort(key=lambda x: x["score"], reverse=True)
        return _json_ok({
            "elder_id": elder_id,
            "recommendations": recommendations[:top_k]
        })
    except Exception as e:
        return _json_error(f"Recommendation failed: {str(e)}", status=500)

# --- Model Management & Diagnostics ---

@csrf_exempt
@require_http_methods(["POST"])
def train_model(request):
    """Refine the GAT model based on the current social graph."""
    try:
        recommender = __import__("recommendations.gat.recommender", fromlist=["*"])
        train_model_fn = getattr(recommender, "train_model")
        
        report = train_model_fn(epochs=180, persist=True)
        run = record_training_run(report=report, config={"epochs": 180}, model_family="legacy_gat")
        
        return _json_ok({"status": "success", "metrics": report, "run_id": run.id})
    except Exception as e:
        return _json_error(f"Training failed: {str(e)}", status=500)

@require_http_methods(["GET"])
def health_status(request):
    """System health and model status."""
    model_path = Path(__file__).resolve().parent / "gat" / "checkpoints" / "elder_gat.pt"
    return _json_ok({
        "status": "online",
        "counts": {
            "profiles": ElderProfile.objects.count(),
            "edges": SocialEdge.objects.count(),
        },
        "model": {
            "checkpoint_exists": model_path.exists(),
            "torch_available": importlib.util.find_spec("torch") is not None,
        }
    })

@require_http_methods(["GET"])
def feature_schema(request):
    """Retrieve the available feature definitions."""
    return _json_ok({
        "features": get_feature_details(),
        "groups": get_feature_groups()
    })
