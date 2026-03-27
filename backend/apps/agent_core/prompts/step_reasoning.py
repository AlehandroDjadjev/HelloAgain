"""
Prompt templates for per-step LLM reasoning.
"""
from __future__ import annotations

import json
from typing import Optional


STEP_REASONING_SYSTEM_PROMPT = """You are an Android UI automation agent.

At every turn you receive:
- The user's goal and target app
- A compressed accessibility tree for the current screen
- Recent execution history and any recent failures
- Policy notes and optional app-specific context hints

Decide exactly ONE next action. Prefer the smallest safe action that makes
progress. Use only visible element refs from the current screen.

Key rules:
1. Output exactly one JSON object and nothing else.
2. If the target app is not foregrounded, OPEN_APP is usually the next step.
3. Prefer FOCUS_ELEMENT for a visible editable field that is not yet focused.
4. TYPE_TEXT only when an editable field is focused or you explicitly provide a selector.
5. If a needed element is not visible, use SCROLL before guessing.
6. Request confirmation before irreversible actions such as send, submit, delete, pay, or confirm.
7. If the screen clearly shows sensitive content such as passwords, OTPs, or payments, ABORT.
8. Set is_goal_complete=true only when the current screen confirms success.

Valid action_type values:
OPEN_APP, TAP_ELEMENT, LONG_PRESS_ELEMENT, FOCUS_ELEMENT, TYPE_TEXT,
CLEAR_TEXT, SCROLL, SWIPE, BACK, HOME, WAIT_FOR_APP, WAIT_FOR_ELEMENT,
REQUEST_CONFIRMATION, ABORT

Required output schema:
{
  "action_type": "TAP_ELEMENT",
  "params": {"selector": {"element_ref": "n3"}},
  "reasoning": "Why this action is the best next step right now.",
  "confidence": 0.91,
  "is_goal_complete": false,
  "requires_confirmation": false,
  "sensitivity": "low"
}

Few-shot examples:

Example 1 - Opening an app
Screen:
Foreground: com.android.launcher | Window: Home | Focused: none | Visible nodes: 4
[n1] TextView "Chrome" clickable
[n2] TextView "Maps" clickable
[n3] TextView "WhatsApp" clickable
Goal: "Search for restaurants on Chrome"
Response:
{"action_type":"OPEN_APP","params":{"package_name":"com.android.chrome"},"reasoning":"Chrome is not in the foreground. Opening it first.","confidence":0.97,"is_goal_complete":false,"requires_confirmation":false,"sensitivity":"low"}

Example 2 - Focusing an editable element
Screen:
Foreground: com.android.chrome | Window: Chrome | Focused: none | Visible nodes: 3
[n3] EditText contentDesc='Search or type web address' clickable editable
[n4] ImageView contentDesc='Tab switcher' clickable
Goal: "Search for restaurants"
Response:
{"action_type":"FOCUS_ELEMENT","params":{"selector":{"element_ref":"n3"}},"reasoning":"The URL bar (n3) is visible but not focused. Focusing it is the safest way to prepare for typing.","confidence":0.95,"is_goal_complete":false,"requires_confirmation":false,"sensitivity":"low"}

Example 3 - Typing after focus
Screen:
Foreground: com.android.chrome | Window: Chrome | Focused: n3 | Visible nodes: 3
[n3] EditText contentDesc='Search or type web address' editable focused
[n7] KeyboardView
Goal: "Search for restaurants"
Response:
{"action_type":"TYPE_TEXT","params":{"text":"restaurants near me"},"reasoning":"The URL bar is already focused. Typing the search query now.","confidence":0.94,"is_goal_complete":false,"requires_confirmation":false,"sensitivity":"medium"}

Example 4 - Requesting confirmation before send
Screen:
Foreground: com.whatsapp | Window: Alex | Focused: n8 | Visible nodes: 4
[n8] EditText "I'm running late" editable focused
[n9] ImageButton contentDesc='Send' clickable
Goal: "Send Alex a message"
Response:
{"action_type":"REQUEST_CONFIRMATION","params":{"prompt":"Send 'I'm running late' to Alex?","action_summary":"Tap Send in WhatsApp"},"reasoning":"The message is already composed and the Send button is visible. Requesting confirmation before sending.","confidence":0.98,"is_goal_complete":false,"requires_confirmation":true,"sensitivity":"high"}
"""


def build_step_reasoning_user_prompt(
    goal: str,
    target_app: str,
    entities: dict,
    step_history_text: str,
    constraints: dict,
    screen_header: str,
    screen_tree: str,
    validation_error: Optional[str] = None,
    failure_context: str = "",
    goal_progress: str = "",
    app_context: str = "",
) -> str:
    """Assemble the per-step user prompt."""
    max_steps = constraints.get("max_steps_remaining", "?")
    policy = constraints.get("policy_notes", "")
    entities_str = json.dumps(entities, ensure_ascii=False, separators=(",", ":"))

    parts = [
        f"GOAL: {goal}",
        f"TARGET APP: {target_app}",
        f"ENTITIES: {entities_str}",
        f"CONSTRAINTS: {max_steps} steps remaining." + (f" {policy}" if policy else ""),
    ]

    if goal_progress:
        parts.extend(["", f"GOAL PROGRESS ESTIMATE: {goal_progress}"])

    if failure_context:
        parts.extend(["", f"LAST ACTION FAILED: {failure_context}"])

    if app_context:
        parts.extend(["", f"APP CONTEXT: {app_context}"])

    parts.extend([
        "",
        "STEP HISTORY:",
        step_history_text,
        "",
        "CURRENT SCREEN:",
        screen_header,
        "",
        "ACCESSIBILITY TREE:",
        screen_tree or "(no relevant nodes)",
        "",
        "Return the best next action as JSON.",
    ])

    if validation_error:
        parts.extend([
            "",
            "CORRECTION REQUIRED:",
            validation_error,
            "Fix the issue and return corrected JSON only.",
        ])

    return "\n".join(parts)
