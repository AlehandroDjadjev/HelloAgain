# GNN Action System

A Django-backed prototype for a **single-user dynamic preference graph** driven by natural-language prompts.

This project implements the flow defined in the chat:

- one main user profile node
- a shared attribute table
- persistent action nodes
- per-prompt user-state updates
- add-action and fetch-action flows
- cross-updates into related existing actions
- a weighted heterogenous message-passing GNN used to rank actions
- Qwen prompt parsing through `transformers` + `bitsandbytes` 4-bit loading

## Core idea

Every prompt is first parsed by Qwen into a structured JSON plan:

- new attributes to create
- user score changes
- prompt-affected attributes
- explicit or implicit action candidates
- touched existing actions with relevance
- a wanted/intensity signal

The graph then updates:

1. **User attribute state** is shifted.
2. **Action nodes** are created or updated.
3. **Related old actions** absorb a relevance-weighted fraction of the current user state on overlap fields.
4. The **direct user-action edge** is updated.
5. The current graph is passed into a weighted heterogenous GNN.
6. Fetch mode ranks all actions using the GNN score blended with direct edge and vector similarity.

## Project layout

- `engine/llm_parser.py` – Qwen loading and JSON prompt parsing
- `engine/prompts.py` – parser prompt templates
- `engine/graph_service.py` – graph update rules and flow orchestration
- `engine/gnn.py` – weighted heterogenous message-passing network
- `controller/models.py` – Django ORM schema for attributes, scores, actions, and edges
- `controller/views.py` – simple controller API + HTML panel

## Main entities

### Attribute
Shared semantic anchor.

### UserAttributeScore
Current scored relation from the main user profile to each attribute.

### Action
Persistent node representing a requestable or remembered action.

### ActionAttributeScore
The running action-side aggregate of states that have pointed to that action.
This is the "stack of histories" in compressed form.

### UserActionEdge
Direct personal memory edge between the user and an action.

## Flows

### Add action
Used when the prompt is defining or strongly stating a new action.

- Parse prompt.
- Update user state.
- Create or update the new action.
- Apply the full changed user-state to the new action.
- Compare the new action against existing actions.
- Push relevance-weighted overlap updates into existing actions.
- Update direct user-action edge.
- Optionally run online positive-edge training for the GNN.

### Fetch action
Used when the prompt asks what fits now.

- Parse prompt.
- Update user state.
- Rank all actions with the GNN.
- Pick the best action.
- Update the chosen action with the full changed user-state.
- Update other related actions using overlap-only updates weighted by relevance.
- Strengthen direct user-action edge for the fetched action.

## Running

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python manage.py migrate
python manage.py runserver
```

Open:

- `http://127.0.0.1:8000/` for the controller page
- JSON API endpoints:
  - `POST /api/add-action/`
  - `POST /api/fetch-action/`
  - `GET /api/state/`

## Required model configuration

The default loader targets `Qwen/Qwen3-4B-Instruct-2507` in 4-bit NF4 using bitsandbytes.
Set:

```bash
export QWEN_MODEL_ID=Qwen/Qwen3-4B-Instruct-2507
```

Optional:

```bash
export QWEN_DEVICE_MAP=auto
export QWEN_MAX_NEW_TOKENS=4096
```

## Notes

- This is a serious prototype, not a polished production product.
- The GNN is **working code**, but it is still a lightweight online-ranking design.
- The prompt parser is intentionally verbose and asks the model to emit a large JSON plan.
- The parser layer is the main place to keep evolving the behavior.
