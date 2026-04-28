# HelloAgain API Guide

## Agent Result Surfaces

- `user_connection`: compact person card or thread popup
- `meetup_invite`: meetup proposal card
- `phone_command_launcher`: launcher for the phone action flow
- `weather_snapshot`: popup-first weather card
- `outing_suggestion`: person plus outdoor place and time suggestion
- `summary_only`: human-readable fallback summary with no raw JSON shown to the user

## Agent MCP Discovery

- `GET /api/agent/mcp-registry/`
  Returns the MCP registry used by the semi-agent planner.
- `GET /api/agent/mcps/<mcp_id>/`
  Returns one MCP descriptor.
- `POST /api/agent/mcps/<mcp_id>/invoke/`
  Invokes a specific MCP tool directly.

Important MCPs:

- `connections.find_connection`
- `connections.update_profile`
- `phone_command.open_phone_command`
- `weather.get_current_weather`
- `meetup.propose_friend_meetup`

## Agent Run Flow

- `POST /api/agent/run/start/`
  Starts the parallel speech plus whitespace run.

Request body:

```json
{
  "prompt": "Какво е времето навън?",
  "user_id": "123",
  "session_id": "session_1",
  "board_state": { "board": { "width": 800, "height": 600 }, "objects": [] },
  "largest_empty_space": { "bbox": { "x": 0, "y": 0, "width": 800, "height": 600 } },
  "location": { "lat": 42.6977, "lng": 23.3219, "timezone": "Europe/Sofia" }
}
```

Notes:

- `location` is optional.
- The backend uses current device location first.
- If `location` is missing, weather and outing flows fall back to stored home coordinates when available.

Polling endpoints:

- `GET /api/agent/run/<run_id>/speech/`
- `GET /api/agent/run/<run_id>/whitespace/`

Whitespace responses may include:

- `board_commands`
- `result_bindings`
- `auto_open_viewer`

`auto_open_viewer` is used for popup-first results such as weather.

## Maps / Navigation

- `POST /api/agent/navigation/`

Use this when the app needs a prepared navigation or map handoff from an agent prompt.

## Weather

- `POST /api/weather/current/`

Request body:

```json
{
  "location": { "lat": 42.6977, "lng": 23.3219 },
  "timezone": "Europe/Sofia"
}
```

Behavior:

- Returns a normalized `weather_snapshot` payload.
- If `location` is missing and the request is authenticated, the endpoint tries the profile home coordinates.
- The frontend should present this as a popup-first weather card, not as raw JSON.

## Meetups

### Ranking / Recommendation

- `POST /api/meetup/recommend/`

Use this for meetup spot ranking and best-location suggestions.

### Friend Meetup Lifecycle

- `POST /api/meetup/friends/propose/`
- `GET /api/meetup/invites/`
- `GET /api/meetup/meeting/`
- `GET /api/meetup/notifications/`
- `POST /api/meetup/invites/<id>/respond/`

Rules:

- `propose_friend_meetup` is for a known accepted friend.
- Outdoor discovery prompts can also produce `outing_suggestion`, which reuses meetup ranking logic without creating a formal invite.

## Outdoor Suggestion Flow

For prompts like "find me a place to go outside":

1. `connections.find_connection` selects the best matching person.
2. The meetup service enriches that result with an `outing_suggestion` when both users have usable coordinates.
3. The frontend shows one calm card with the person, place, and suggested time.

If coordinates are missing, the backend falls back to the person recommendation without failing the whole response.
