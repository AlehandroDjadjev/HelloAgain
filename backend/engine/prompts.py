import json
from typing import Any, Iterable


def render_attribute_inventory(attributes: Iterable[dict]) -> str:
    rows = [f"{item['name']}:{item['score']:.3f}" for item in attributes]
    return ", ".join(rows) if rows else "none"


def build_parser_step_one_system_prompt(mode: str, attribute_inventory_text: str) -> str:
    return f"""
Return exactly one JSON object and nothing else.
Mode: "{mode}".
Parse the user's prompt into graph updates for the main user state.
Attributes are any useful preference, mood, context, need, or tendency signal.

You are fed a prompt which you have to parse to update a graph neural network.
The prompt comes from a user with who you have to analyze and build description of their character and full emotional state.
You are to translate prompts into attribute prompt - nitpicking their details.
With this you add in attributes with innit scores and / or change the weights of ANY slightly affected attributes.

This is step 1 of 2.
-Step 1 is entirely for `user_state` and `prompt_context`.
-Step 2 will use this exact result to do the action mapping.
-Keep the output compact so it fits a 512 token response limit.

CORE UPDATE RULES:
-Treat `CURRENT ATTRIBUTE INVENTORY` as the authoritative active user context for this request.
-`user_state.new_attributes` is only for truly new semantic attributes that ARE MISSING from the current inventory.
-Only lower or decay user scores when the prompt explicitly states that reduction. Mark those cases with `explicit_decay=true`.
-Attributes in `user_state.new_attributes` cannot appear in `user_state.updates` - they only get innit, not updates in the same prompt.
-Avoid abstract placeholder attributes like `need`, `mood`, `desire`,`feeling`, `state`. Use very specific attributes instead.
-Also extract the user's positive solution / desired outcome separately from the current-state attributes.
-Keep desired solution attributes runtime-only inside `prompt_context.desired_attributes`. Do not write desired-only attributes into `user_state` unless the prompt clearly says the user already has them now.
-If the prompt states problems / negative current state but does not clearly state the desired outcome, also return up to 2 `prompt_context.opposite_attributes` that describe the positive counter-state or solution (example: sad -> happy, lonely -> connected).
-Desired and opposite attributes are for matching only. They are not stored in the user profile.
-Be selective, not exhaustive. Prefer the smallest valid JSON that preserves the important graph signal.
-Prefer 2-4 strong prompt attributes, up to 2 desired attributes, up to 2 opposite attributes.
-Try and update the user states as much as possible to reflect micro mentions in what is said - all the little things that relate to it.
-Attribute names are the keys. Scores are the values.
-Scores are floats in the range -1.0 to 1.0.

REQUIRED JSON SHAPE:
{{
  "user_state": {{
    "new_attributes": {{
      "attribute_name": 0.5
    }},
    "updates": {{
      "attribute_name": {{
        "target_score": 0.2,
        "explicit_decay": false
      }}
    }}
  }},
  "prompt_context": {{
    "desired_attributes": {{
      "positive_attribute_name": 0.4
    }},
    "opposite_attributes": {{
      "positive_counter_attribute_name": 0.0
    }}
  }}
}}

The score values are decided by the specific realtion of each score to the prompt / enviorment.
You are to directly translate stated things to action - no phylosophycal interpretation.

CURRENT ATTRIBUTE INVENTORY:
{attribute_inventory_text}

FIELD MEANING:
- `user_state.new_attributes` = attribute -> initial_score.
- `user_state.updates` = attribute -> object with `target_score` and optional `explicit_decay`.
- cant have the same attribute in boths
- `prompt_context.desired_attributes` = positive_attribute -> score.
- `prompt_context.opposite_attributes` = positive_counter_state -> score.
""".strip()


def build_parser_step_two_system_prompt(
    mode: str,
    action_inventory_text: str,
    step_one_response: dict[str, Any],
) -> str:
    step_one_json = json.dumps(step_one_response, ensure_ascii=True, indent=2)
    return f"""
Return exactly one JSON object and nothing else.
Mode: "{mode}".
Parse the user's prompt into action graph updates for the same request.

This is step 2 of 2.
-Step 1 already parsed `user_state` and `prompt_context`.
-Use the full step 1 response below as the authoritative state parse for this request.
-Return only the fields in stage 2.
-Step 2 is only for `action_candidate` and `edge_signal`, use the stage 1 packet entirely for building the actions.
-Keep the output compact so it fits a 512 token response limit.

CORE ACTION RULES:
-You cannot reuse or change existing INVENTORY actions - each new action is a fresh thing.
-In add mode your entire task is to create a new action with the attributes for it extracted from the state / prompt/
-In fetch mode, the chosen action will absorb the full request state, while background actions only absorb overlap fields.
-In conversation mode, do not invent a new action unless the user is clearly defining one.
-In add mode, also return up to 2 strong positive outcome attributes in `action_candidate.desired_attribute_map` when the action helps the user in a good / healing / useful direction. This is the positive side of the action.
-In fetch mode, use direct desired attributes when the user says what they want. Use opposite attributes only when the request is mostly a problem-state request with no direct desired outcome.
-Desired and opposite attributes are for matching only. They are not stored in the user profile.
-Be selective, not exhaustive. Prefer the smallest valid JSON that preserves the important graph signal.
-Prefer up to 4 action attributes and up to 2 desired action attributes.
-Attribute names are the keys. Scores are the values.
-Scores are floats in the range -1.0 to 1.0.
-When the user improves some negative scores on attributes on degrades positive scores we shouldnt directly chane the same attributes in actions. 
Changes with them are about how the meaning of the action changes for the user. It should otherwise holds its history.

REQUIRED JSON SHAPE:
{{
  "action_candidate": {{
    "name": "action name if add mode or if clearly implied",
    "wanted_strength": 0.9,
    "attribute_map": {{
      "attribute_name": 0.0
    }},
    "desired_attribute_map": {{
      "positive_outcome_attribute_name": 0.2
    }}
  }},
  "edge_signal": {{
    "kind": "neutral|desire|positive|negative|memory|fetch",
    "strength": 0.0,
    "reason": "why that direct signal makes sense"
  }}
}}

FIELD MEANING:
- `action_candidate.attribute_map` = attribute -> score.
- `action_candidate.desired_attribute_map` = positive_outcome_attribute -> score.

STEP 1 RESPONSE FOR ACTION MAPPING CONTEXT:
{step_one_json}

CURRENT ACTION INVENTORY:
{action_inventory_text}
""".strip()


def build_action_inventory_text(actions: Iterable[dict]) -> str:
    rows = []
    for action in actions:
        attrs = ",".join(
            f"{name}:{score:.3f}" for name, score in sorted(action["attribute_map"].items())[:8]
        )
        desired = ",".join(
            f"{name}:{score:.3f}" for name, score in sorted(action.get("desired_attribute_map", {}).items())[:4]
        )
        rows.append(f"{action['name']}:state[{attrs}] desired[{desired or 'none'}]")
    return "; ".join(rows) if rows else "none"
