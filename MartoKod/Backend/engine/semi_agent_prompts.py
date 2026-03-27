from __future__ import annotations

import json
from typing import Any, Dict, List


def _pretty_json(payload: Dict[str, Any] | List[Any] | None) -> str:
    return json.dumps(payload or {}, ensure_ascii=True, indent=2)


def build_step_one_mcp_prompt(
    *,
    registry: Dict[str, Any],
    chain_history: List[Dict[str, Any]],
) -> str:
    return f"""
You are a Qwen worker inside a hardcoded semi agent. This is part of a set reasoning chain and you must stay aware of which exact step you are in.
The user wording is the contract for this system. Keep the tone of the instructions and do not simplify the intent away:
We have a couple of steps we go through sort of like a thought chain but its not a dynamic one - a set order"
First we run a sort of "mcp" layer. Now this doesnt use real mcps but exposes ready endpoints that have set actions they offer in a json - these actions produce a result which we can just use with the model and each at-home mcp comes with a desription of how its actions behave
The idea is to see where and how we can apply different stuff to drive a multiple step process - start with an abstract mcp then a another can be run ontop after.
We still have to select if its even relevant for some as tools can be wildly different.
But just be aware of the type of descition you are making - the first step of the reasoning process.

This is step 1.
- Step 1 decides whether MCPs are needed and which MCP calls to make.
- After step 1 finishes, the speech branch starts from these results in parallel.
- Step 2 waits for the MCP results, can recall stage 1, and does the whiteboard work.
- Keep the full JSON within 256 output tokens.

Return exactly one JSON object and nothing else.

JSON shape:
{{
  "stage": "step_1_mcp",
  "step_number": 1,
  "chain_position": "mcp layer",
  "needs_mcps": true,
  "request_kind": "mechanical|profile|mixed",
  "memory_hint": "instant|ram|memory",
  "reasoning_summary": "short string",
  "why_this_is_part_of_the_chain": "short string",
  "board_intent": "what step 2 should try to do on the board",
  "speech_intent": "what step 3 should sound like",
  "mcp_calls": [
    {{
      "call_id": "gnn_actions.fetch_action.1",
      "mcp_id": "gnn_actions",
      "tool_name": "add_action|fetch_action|conversation",
      "arguments": {{
        "prompt": "prompt to send to the tool"
      }},
      "why": "why this call is needed"
    }}
  ]
}}

Rules:
- Use only MCPs that exist in the registry.
- For purely mechanical requests that do not need the emotional profile or GNN action memory, set "needs_mcps" to false and return an empty "mcp_calls" list.
- For requests that do relate to updating and working with their emotional profile, or requests about remembered actions and what fits now, use the gnn_actions MCP.
- The request kind should be "mechanical", "profile", or "mixed".
- The memory hint should follow the exact user wording: mechanical temporary stuff is instant, temporary stuff that is a request on the level of opening an app on the phone is ram, and a request to find a friend or open an important doc is memory.
- Keep the reasoning summary compact and actionable.
- Do not write prose outside the JSON object.

Current at-home MCP registry:
{_pretty_json(registry)}

Previous chain history:
{_pretty_json(chain_history)}
""".strip()


def build_step_two_board_prompt(
    *,
    board_state: Dict[str, Any],
    largest_empty_space: Dict[str, Any],
    step_one_plan: Dict[str, Any],
    mcp_results: List[Dict[str, Any]],
    chain_history: List[Dict[str, Any]],
) -> str:
    return f"""
You are now in step 2 of the same hardcoded semi agent.
This is the user wording you must preserve the spirit of:
"The second qwen request waits for each of the called mcps to return (so these arent mcps, jsut endpoints for results - the qwen doesnt do anything for each feature). It has an option to cycle back and call an mcp step one here with the results -> so if we have multi layer work we can just have it go on as long as it needs to. But the real "reasoning" is mostly layer 1."
"It takes the results of step one and has to use board interaction on it - check the frontend for all the interactions we expose to it."
"Now it first neceseraly fetches the board state json - all objects with bboxes on the board and uses the empty space function."
"It then has the option to create a new object on the board that is a widget to the result of an mcp."
"They way it happens is we create the object to send out on the board and have a persistent json map for objects to actual mcp results that get opened when we get a click object action."
"That whitespace object should stay light and readable. Its visible title is only a very optimized summarized name, while the more complex mcp structure stays mapped in the background."
"Do not dump an mcp response json into the object name or label. The goal of the board object is to figure out the best summarization name for the result."
"If there is added content, treat it as second layer protocol content that opens from the object, not as part of the visible title."
"The idea is to have a very active one time change on the board for the request. So as we create the new object on the board it has to be big, it has to be created in the center of the board OR in a good empty space and we have to move other objects out the way and or shrink them down."
"We have to interact with other objects on the whiteboard state if they'll take attention away from the current object."
"Just encourage qwen to use all the provided steps it has - they arent many but they do exist to create movement on the board.
"We also need a basic memmory system with jsons of the board objects we can saved in a memmory folder on the backend. We have 3 types of memmory."
"One is instant - this jsut creates an object on the board which will have a tag to dissapear after its clicked -> use it jsut for one time routing down for mcps results ON REQUESTS that are very mechanical."
"The second type of mmemory for an object is in ram -> exists only in the loaded flutter app, it exists in whiteboard states but it isnt actually saved."
"And actual memmmory - a json of the whiteboard state that is saved and updated only for objects that are tagged to have propper memory turned on."

Available whiteboard actions:
- "state" fetches the board state json.
- "findLargestEmptySpace" returns the biggest open bbox on the board.
- "create" creates a new object.
- "move" moves an existing object.
- "enlarge" scales an object up.
- "shrink" scales an object down.
- "delete" removes an object.
- "click" represents opening a result widget.

This is step 2.
- The speech branch has already started from the finished stage 1 results.
- Keep the full JSON within 256 output tokens.
Return exactly one JSON object and nothing else.

JSON shape:
{{
  "stage": "step_2_board",
  "step_number": 2,
  "chain_position": "board interaction",
  "cycle_back_to_step_one": false,
  "reasoning_summary": "short string",
  "board_explanation": "what the board is doing and why",
  "memory_plan": {{
    "default_memory_type": "instant|ram|memory",
    "why": "why this memory type fits"
  }},
  "focus_object": {{
    "name": "object name",
    "text": "short summarized whitespace object title",
    "width": 320,
    "height": 220,
    "memory_type": "instant|ram|memory",
    "delete_after_click": true,
    "linked_call_ids": ["call id"],
    "result_title": "title for click open",
    "result_summary": "summary for click open"
  }},
  "additional_mcp_calls": [
    {{
      "call_id": "gnn_actions.conversation.2",
      "mcp_id": "gnn_actions",
      "tool_name": "add_action|fetch_action|conversation",
      "arguments": {{
        "prompt": "prompt to send to the tool"
      }},
      "why": "why the extra call is needed"
    }}
  ],
  "board_commands": [
    {{
      "action": "move|shrink|enlarge|create|delete",
      "name": "object name",
      "x": 0,
      "y": 0,
      "factor": 1.0,
      "width": 320,
      "height": 220,
      "text": "label"
    }}
  ],
  "result_bindings": [
    {{
      "object_name": "object name",
      "linked_call_ids": ["call id"],
      "memory_type": "instant|ram|memory",
      "delete_after_click": true,
      "result_title": "title for click open",
      "result_summary": "summary for click open"
    }}
  ]
}}

Rules:
- You already have the fetched board state and the largest empty space.
- First think about the current board state and empty space, then plan the board change.
- Prefer one strong current focus object for this request.
- Make the focus object big.
- The created whitespace object is a lightweight board shell for the result.
- The visible object `text` must be a compact summarized title, ideally 2 to 6 words.
- Keep the full MCP structure in the background mapping through `linked_call_ids` and `result_bindings`.
- Never paste raw MCP JSON, argument blobs, or long response text into `name` or `text`.
- If there is richer added content, place it in the second layer protocol behind the object using `result_title` and `result_summary`, not in the visible board title.
- Put it in the center of the board OR in a good empty space.
- If other objects will take attention away from it, move them and or shrink them down.
- If no extra MCP work is needed, keep "cycle_back_to_step_one" false and "additional_mcp_calls" empty.
- If more MCP work is truly needed, set "cycle_back_to_step_one" true and request only the smallest necessary extra calls.
- Keep the output grounded in the exposed actions only.
- Do not write prose outside the JSON object.

Current board state:
{_pretty_json(board_state)}

Largest empty space:
{_pretty_json(largest_empty_space)}

Step 1 plan:
{_pretty_json(step_one_plan)}

MCP results so far:
{_pretty_json(mcp_results)}

Previous chain history:
{_pretty_json(chain_history)}
""".strip()


def build_step_three_speech_prompt(
    *,
    original_prompt: str,
    step_one_plan: Dict[str, Any],
    mcp_results: List[Dict[str, Any]],
    step_two_plan: Dict[str, Any],
    final_board_state: Dict[str, Any],
) -> str:
    return f"""
You are in step 3 of the same hardcoded 3 step semi agent.

Instructions:

"The final step is to just generate a basic speech responce to the prompt. It has context on what mcp / interaction work has been done and has the new whiteboard state and based on the prompt + all the steps generates a shared responce and explanation of what its done. I mean respond to the prompt but only with awarness of the other "reasoning" or steps we've done. Its a speech responce."

This is the final speech step. You are not planning anymore. You are responding with awareness of the other work that happened.

Rules:
- Return plain text only.
- Respond to the user prompt directly.
- Mention the important MCP or whiteboard work only when it helps the response.
- Sound like a shared response from the full chain, not like isolated step output.
- Keep it concise and natural.
- Keep the reply within 256 output tokens.

Original user prompt:
{original_prompt}

Step 1 plan:
{_pretty_json(step_one_plan)}

MCP results:
{_pretty_json(mcp_results)}

Step 2 board plan:
{_pretty_json(step_two_plan)}

Final whiteboard state:
{_pretty_json(final_board_state)}
""".strip()
