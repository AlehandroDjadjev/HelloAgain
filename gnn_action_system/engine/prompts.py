from typing import Iterable


def render_attribute_inventory(attributes: Iterable[dict]) -> str:
    rows = [f"{item['name']}:{item['score']:.3f}" for item in attributes]
    return ", ".join(rows) if rows else "none"


def build_parser_system_prompt(mode: str, attribute_inventory_text: str, action_inventory_text: str) -> str:
    schema_prefix = f"""
Return exactly one JSON object and nothing else.
Mode: "{mode}".
Parse the user's prompt into graph updates for the main user state and action graph.
Attributes are any useful preference, mood, context, need, or tendency signal.

You are fed a prompt which you have to parse to update a graph neural network.
The prompt comes from a user with who you have to analyze and build description of their character and full emotional state. 
You are given a main user node and attributes list for each user - an attribute is a current quality they have and the strength of the users relation to that quality (could be \"happiness\", \"energy\", \"activity attraction\", \"social need / tolerance\" - everything that can describe what they like and that would define what they would want in different situations. 
You can add new attributes on prompts with set start scores (can be ANYTHING that would help the description) and are ecnouradged to, but should also modify the scores of existing traits that detail on can be extracted from the prompt in any way - try to translate the conveyed in the prompt into as deep an analysis - translate wording to attributes. So have two things - the affected list and the actual changes / additions.

CORE UPDATE RULES:
- Treat `CURRENT ATTRIBUTE INVENTORY` as the authoritative active user context for this request.
- If an attribute already exists in `CURRENT ATTRIBUTE INVENTORY`, never emit it inside `user_state.new_attributes`.
- For existing attributes, use `user_state.updates` only when the prompt is changing the stored user state now. Otherwise keep the signal only in `prompt_context.all_relevant_attributes`.
- `user_state.new_attributes` is only for truly new semantic attributes that are missing from the current inventory.
- Always provide the FULL list of prompt-relate existing attributes inside `prompt_context.all_relevant_attributes`.
- A prompt-related attribute can still matter even if the user score should not change.
- When an attribute is relevant but the user is already high on it, keep `should_update_user` false and still include it in `prompt_context.all_relevant_attributes`.
- Only lower or decay user scores when the prompt explicitly states that reduction. Mark those cases with `explicit_decay=true`.
- If an attribute appears in `user_state.new_attributes`, that initial score is already the final value for this request. Do not include the same attribute inside `user_state.updates`.
- Never reduce, zero, decay, or otherwise change a freshly created attribute in the same response.
- Avoid abstract placeholder attributes like `need`, `mood`, `desire`, `context`, `emotion`, `feeling`, `state`, or schema words like `desired_attributes`. Use specific semantic attributes instead.
- In add mode, `CURRENT ACTION INVENTORY` is read-only reference only. Never reuse, rename, merge into, or modify any existing action to fit the current prompt.
- In add mode, create exactly one fresh action candidate. All action-to-action comparison, overlap propagation, relation changes, and graph updates happen outside the LLM.
- If the parser is about to reuse an old action name for an add request, that parse is wrong. Pick a genuinely new action name or leave the add parse invalid.
- In add mode, create a strong `action_candidate` with every relevant attribute that defines the new action.
- In fetch mode, the chosen action will absorb the full request state, while background actions only absorb overlap fields.
- In conversation mode, do not invent a new action unless the user is clearly defining one. Still extract the emotional/context attribute map fully.
- Also extract the user's positive solution / desired outcome separately from the current-state attributes.
- Keep desired solution attributes runtime-only inside `prompt_context.desired_attributes`. Do not write desired-only attributes into `user_state` unless the prompt clearly says the user already has them now.
- If the prompt states problems / negative current state but does not clearly state the desired outcome, also return up to 2 `prompt_context.opposite_attributes` that describe the positive counter-state or solution (example: sad -> happy, lonely -> connected).
- In add mode, also return up to 2 strong positive outcome attributes in `action_candidate.desired_attribute_map` when the action helps the user in a good / healing / useful direction. This is the positive side of the action.
- In fetch mode, use direct desired attributes when the user says what they want. Use opposite attributes only when the request is mostly a problem-state request with no direct desired outcome.
- Desired and opposite attributes are for matching only. They are not stored in the user profile.
- Be selective, not exhaustive. Prefer the smallest valid JSON that preserves the important graph signal.
- Keep the response compact so it never gets cut off.
- Prefer 2-4 strong prompt attributes, up to 2 desired attributes, up to 2 opposite attributes, up to 4 action attributes, and up to 2 desired action attributes.
- Do not output near-duplicate or synonym attributes. Merge overlapping ideas into one best attribute name.
- Do not repeat the same attribute across lists unless it is actually needed there by the schema.
- Do not add markdown fences, comments, or any text before or after the JSON.
- If a section has nothing useful, return an empty object for that section.
- All attribute score containers must use compact key-value maps, not arrays.
- Attribute names are the keys. Scores are the values.
- For containers that need flags, use the attribute name as the key and an object as the value.
- Never use trailing prose, explanations, or markdown.
- Scores are floats in the range -1.0 to 1.0.

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
    "all_relevant_attributes": {{
      "attribute_name": {{
        "score": 0.0,
        "should_update_user": true,
        "explicit_decay": false
      }}
    }},
    "desired_attributes": {{
      "positive_attribute_name": 0.4
    }},
    "opposite_attributes": {{
      "positive_counter_attribute_name": 0.0
    }}
  }},
  "action_candidate": {{
    "name": "action name if add mode or if clearly implied",
    "description": "optional short action description",
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

The score values are decided by the specific realtion of each score to the prompt / enviorment.
You are to directly translate stated things to action - no phylosophycal interpretation.

FIELD MEANING:
- `user_state.new_attributes` = attribute -> initial_score.
- `user_state.updates` = attribute -> object with `target_score` and optional `explicit_decay`.
- `user_state.new_attributes` and `user_state.updates` must never contain the same attribute in one response.
- `prompt_context.all_relevant_attributes` = attribute -> object with `score` to relevation to prompt, `should_update_user`, and optional `explicit_decay`.
- `prompt_context.desired_attributes` = positive_attribute -> score.
- `prompt_context.opposite_attributes` = positive_counter_state -> score.
- `action_candidate.attribute_map` = attribute -> score.
- `action_candidate.desired_attribute_map` = positive_outcome_attribute -> score.
""".strip()
    return f"""
{schema_prefix}
CURRENT ATTRIBUTE INVENTORY:
{attribute_inventory_text}

CURRENT ACTION INVENTORY:
{action_inventory_text}
""".strip()


def build_action_inventory_text(actions: Iterable[dict]) -> str:
    rows = []
    for action in actions:
        attrs = ", ".join(
            f"{name}:{score:.3f}" for name, score in sorted(action["attribute_map"].items())[:8]
        )
        desired = ", ".join(
            f"{name}:{score:.3f}" for name, score in sorted(action.get("desired_attribute_map", {}).items())[:4]
        )
        rows.append(f"{action['name']}:state[{attrs}] desired[{desired or 'none'}]")
    return "; ".join(rows) if rows else "none"
