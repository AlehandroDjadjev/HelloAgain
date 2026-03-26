"""
feature_updater.py
------------------
EMA-based feature vector updates and compatibility scoring.

Scoring uses **mean-centered cosine** (equivalent to Pearson correlation)
which properly discriminates vectors that are all clustered near 0.5.
This is the same approach used by collaborative filtering recommenders
(Netflix, Spotify) to handle rating-scale bias.

Ref: https://github.com/benfred/implicit  (ALS-based CF with centered cosine)
Ref: https://surprise.readthedocs.io/en/stable/similarities.html#surprise.similarities.pearson
"""

import math
import copy
from .feature_schema import get_default_feature_vector, get_feature_names


def update_feature_vector(
    current: dict[str, float],
    signals: dict[str, float],
    alpha: float = 0.15,
) -> dict[str, float]:
    """
    Blend *signals* into *current* feature vector with EMA.

    Parameters
    ----------
    current : dict
        The elder's existing feature vector (all features).
    signals : dict
        Partial or full dict of feature updates in [0.0, 1.0].
        Keys not present in *signals* are left unchanged.
    alpha : float
        Learning rate (0 < alpha < 1).  0.15 is a sensible default.

    Returns
    -------
    dict
        Updated feature vector (new object, does not mutate *current*).
    """
    updated = copy.deepcopy(current)
    feature_names = set(get_feature_names())
    defaults = get_default_feature_vector()
    for feature, signal_val in signals.items():
        if feature not in feature_names:
            continue  # ignore unknown feature names
        signal_val = float(max(0.0, min(1.0, signal_val)))  # clamp
        updated[feature] = (1.0 - alpha) * updated.get(feature, defaults.get(feature, 0.5)) + alpha * signal_val
    return updated


def apply_manual_override(
    current: dict[str, float],
    overrides: dict[str, float],
) -> dict[str, float]:
    """
    Hard-set specific features (e.g. from a profile form).
    Values are clamped to [0.0, 1.0].
    """
    updated = copy.deepcopy(current)
    feature_names = set(get_feature_names())
    for feature, val in overrides.items():
        if feature in feature_names:
            updated[feature] = float(max(0.0, min(1.0, val)))
    return updated


def compute_compatibility_score(
    vec_a: dict,
    vec_b: dict,
    confidence_a: dict[str, float] | None = None,
    confidence_b: dict[str, float] | None = None,
    features: list[str] | None = None,
) -> float:
    """Mean-centered cosine similarity (Pearson correlation).

    Subtracts each vector's mean before computing cosine, so that
    two vectors like [0.50, 0.50, 0.50] and [0.48, 0.52, 0.50] aren't
    falsely scored as near-identical (raw cosine ≈ 0.999).

    Identical vectors → 1.0.
    Uncorrelated vectors → ~0.0.
    Opposite vectors → negative (clamped to 0.0).

    This is the standard approach in CF recommenders (Netflix prize,
    Surprise library, implicit).
    """
    selected = features or get_feature_names()
    defaults = get_default_feature_vector()

    a_vals = [float(vec_a.get(f, defaults.get(f, 0.5))) for f in selected]
    b_vals = [float(vec_b.get(f, defaults.get(f, 0.5))) for f in selected]

    n = len(a_vals)
    if n == 0:
        return 0.0

    # Mean-center
    mean_a = sum(a_vals) / n
    mean_b = sum(b_vals) / n
    a_centered = [x - mean_a for x in a_vals]
    b_centered = [x - mean_b for x in b_vals]

    dot = sum(x * y for x, y in zip(a_centered, b_centered))
    norm_a = math.sqrt(sum(x * x for x in a_centered))
    norm_b = math.sqrt(sum(x * x for x in b_centered))

    if norm_a < 1e-9 or norm_b < 1e-9:
        # All features are the same value — no variance to compare.
        # Identical constant vectors are still "identical", but there's
        # no meaningful personality difference to detect.
        if norm_a < 1e-9 and norm_b < 1e-9:
            return 1.0  # both are constant → identical
        return 0.0  # one is constant, other isn't → can't compare

    pearson = dot / (norm_a * norm_b)
    # Clamp to [0, 1] — negative correlation is just "incompatible"
    return max(0.0, min(1.0, pearson))
