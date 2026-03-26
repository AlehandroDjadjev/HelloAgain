import random
import math

# --- Archetype Signatures ---
# These define the core personality traits for different user clusters.
# Features use 0.0-1.0 scale (0.5 is neutral).

_ARCHETYPES = [
    {
        "name": "community_host",
        "feature_signature": {
            "extroversion": 0.88, "prefers_small_groups": 0.18, "verbosity": 0.82,
            "activity_level": 0.78, "emotional_warmth": 0.80, "hosting_preference": 0.90,
            "independence": 0.35, "tolerance_for_noise": 0.82, "community_involvement": 0.85,
        },
        "bios": ["Warm, outgoing host who loves organizing dinner parties and neighborhood gatherings."]
    },
    {
        "name": "quiet_storyteller",
        "feature_signature": {
            "extroversion": 0.22, "prefers_small_groups": 0.88, "verbosity": 0.35,
            "story_telling": 0.90, "patience": 0.85, "nostalgia_index": 0.88,
            "activity_level": 0.28, "interest_history": 0.85, "tolerance_for_noise": 0.20,
        },
        "bios": ["Reflective storyteller who enjoys quiet afternoons sharing memories and life lessons over tea."]
    },
    {
        "name": "active_outdoors",
        "feature_signature": {
            "extroversion": 0.75, "activity_level": 0.92, "interest_nature": 0.90,
            "interest_fitness": 0.85, "independence": 0.70, "adventurousness": 0.88,
            "prefers_morning": 0.85, "physical_stamina": 0.82,
        },
        "bios": ["Energetic nature lover who is always up for a morning hike or a brisk walk in the park."]
    },
    {
        "name": "nostalgic_historian",
        "feature_signature": {
            "interest_history": 0.95, "interest_books": 0.88, "nostalgia_index": 0.92,
            "patience": 0.80, "story_telling": 0.75, "intellectual_curiosity": 0.85,
            "prefers_small_groups": 0.70, "activity_level": 0.35, "traditionalism": 0.88,
        },
        "bios": ["Knowledgeable historian who finds peace in dusty archives and local lore."]
    }
]

def generate_synthetic_profile(feature_names, preferred_archetype=None, seed=None):
    """Generates a diverse synthetic profile with feature variance."""
    if seed is not None:
        random.seed(seed)
        
    arch = None
    if preferred_archetype:
        arch = next((a for a in _ARCHETYPES if a["name"] == preferred_archetype), None)
    if not arch:
        arch = random.choice(_ARCHETYPES)
        
    sig = arch["feature_signature"]
    vector = {}
    
    for f in feature_names:
        base = sig.get(f, 0.5)
        # Add sigma=0.06 Gaussian jitter to make profiles unique
        jitter = random.gauss(0, 0.06)
        vector[f] = max(0.0, min(1.0, base + jitter))
        
    return {
        "display_name": f"Synthetic {arch['name'].replace('_', ' ').title()}",
        "description": random.choice(arch["bios"]),
        "feature_vector": vector,
        "archetype": arch["name"]
    }

def get_archetype_names():
    return [a["name"] for a in _ARCHETYPES]
