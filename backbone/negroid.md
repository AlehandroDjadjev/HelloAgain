Audio Query Clarification Loop for the HelloAgain Phone Controller Agent
Current agent architecture in the HelloAgain repo
The repo already has the key building blocks needed for what you described: an action-execution loop, a human-in-the-loop confirmation path, and a voice stack capable of speaking prompts and transcribing user replies.

At a high level, the phone controller agent is an iterative “next-step → execute → post-result → decide → next-step” loop driven by a server-side step reasoner. On the client side, a Flutter runner repeatedly (a) collects screen state from the Android accessibility bridge, (b) asks the backend for the next action, (c) executes the action on-device, and (d) posts structured results back to the backend. When the backend returns a “confirmation required” instruction, the client pauses execution and routes the decision through the confirmation endpoints rather than blindly continuing.

Separately, the repo includes a voice controller and a voice gateway that support:

Microphone streaming / end-of-utterance turn detection and transcription (STT).
Exact text-to-speech (TTS) for deterministic prompts.
A “prompt → LLM → spoken reply” path for a more conversational voice UX.
This matters because your requested “audio query” mechanism is best implemented as a third pause-and-resume mode alongside existing execution and confirmation:

execution: device actions happen
confirmation: user approves/rejects a risky step
clarification: user supplies missing or ambiguous information so the agent can keep going
Conceptually, what you want is a mixed-initiative dialogue loop embedded inside the agent run: the agent proceeds autonomously, but when it cannot complete the next step without a missing slot or an unambiguous choice, it pauses, asks a spoken question, validates whether the answer “grounds” the missing information, and then resumes. This aligns with well-studied grounding/repair patterns in dialogue: when mutual understanding is insufficient, the system should request a targeted clarification and proceed once “common ground” is restored. 

Target behavior as a state machine
Your requested behavior becomes much easier to implement (and optimize) if you define it as a small, explicit state machine the backend and frontend both understand.

A minimal version, matching your description, looks like:

Executing
Normal next-action generation, device execution, result ingestion.
Needs clarification
Agent emits a spoken question because:
required data is missing (e.g., destination, recipient, amount, account), or
the UI shows multiple plausible targets (“Alex” appears multiple times), or
the model’s internal reasoning judges the next step ambiguous / underdetermined.
Clarification attempt 1
Ask the question via TTS, capture reply via STT, then “reflect”:
Did we get the missing slot(s)?
Is the answer singular (your term: the reply maps to a single actionable interpretation)?
Clarification attempt 2
If not singular, ask a more specific question (e.g., present choices extracted from the UI).
Clarification attempt 3
If still not resolvable, one final re-prompt and then a safe fallback.
Fallback
If the information still doesn’t unlock a safe action:
either escalate to manual takeover, or
convert to a structured on-screen picker (if available), or
abort with a clear explanation (only when continuing is unsafe).
Two design principles improve “streamlining” and reduce user annoyance:

First, ask only when needed. Voice UX guidelines consistently recommend confirmations and re-prompts sparingly and only when the cost of being wrong is high or when the system lacks required info. 

Second, when you ask, make the question maximally informative and minimally interruptive:

Show or speak available options (e.g., the top candidates visible in the UI).
Accept “over-answers” (user gives more than asked) and fill multiple slots in one turn.
Handle “under-answers” (user gives partial info) with a follow-up that preserves context. 
Backend design for “audio query + reflection + retry”
The backend is the right place to own:

when to ask,
what to ask,
how to validate “singularity,” and
how to map the user’s reply into durable session knowledge that the next-step reasoner can use.
A practical backend design is an additive extension to the existing loop, with three new concepts.

A first-class “request user input” decision
Introduce a new decision mode distinct from confirmations. Confirmations are fundamentally approval gates (yes/no). Your audio query is a data acquisition gate (free-form or choice-based).

Concretely, the backend should be able to return something like:

status = "needs_user_input"
question = "Which Alex should I message? I see Alex Chen and Alex Johnson."
required_fields = ["recipient"]
candidates = [...] (optional; derived from current screen state)
attempt = 1
max_attempts = 3
This new status becomes the backend’s way to pause execution without marking the run as failed.

A persisted “pending query” object on the session
To support retries and reflection deterministically, store a small pending query record on the session (or an adjacent table). It should include:

a stable query_id
attempt_count
required_fields (the slots you must fill)
a compact snapshot of relevant UI context (for example: visible candidate labels, ids, or “row labels”)
optionally, a strict schema for what counts as “singular”
This prevents the system from “forgetting” what it asked in attempt 1 when attempt 2 starts.

Reflection: deciding whether the reply is singular and sufficient
Reflection can be done with either:

a lightweight deterministic rules layer, plus
a very small LLM call for edge cases (“map this utterance to one of these 6 candidates”).
A robust reflection contract returns:

resolved: true/false
entity_updates: { ... } (what to merge into session entities)
followup_question: "..." (if unresolved)
why_unresolved: "missing recipient" | "multiple matches" | "conflict" | ...
The key is to define “singularity” operationally:

Singular if the reply maps to exactly one actionable interpretation given the candidate set and required fields.
Non-singular if:
multiple candidates match (“Alex” matches 3 contacts),
the user provided a category without selecting an item (“the second one” when ordering is unknown),
the answer conflicts with already-known constraints (wrong app, wrong account), or
critical slots remain missing.
A helpful trick: if the UI provides candidates, always validate against UI-derived candidates first instead of letting the model invent entities. This matches your instruction that the agent should primarily rely on “the interface and its gist” and only ask the user when necessary.

How this interacts with confirmations
Treat confirmations and clarifications as different gates:

Confirmation: “Should I tap Send?” (approval).
Clarification: “Which recipient?” / “What should the message say?” (data).
Voice UX guidance also supports explicit confirmations for higher-risk actions (purchase, send, delete), while keeping confirmations sparse otherwise. 

If you want voice-only confirmations, you can still keep the backend confirmation machinery, but allow the frontend to satisfy confirmation via voice (“yes/no”) instead of requiring a tap, only if your safety policy allows it. Many teams keep a “hard confirm” requirement for irreversible steps.

Frontend integration for audio queries
The client changes are straightforward if you treat audio query as another “pause mode” the runner can enter, similar to how it pauses for confirmation today.

Step runner changes
Extend the runner’s “backend status switch” to recognize needs_user_input and call a new callback, e.g.:

onUserInputRequested(payload)
That callback should:

pause device execution (stop sending actions),
invoke the voice controller to speak the question,
capture the user’s answer (STT),
send the answer back to the backend endpoint (e.g., POST /sessions/{id}/user-input/),
then resume the execution loop.
Voice UX: how to ask without breaking the flow
The repo’s voice controller pattern is already close to what you need: it can stop listening while speaking and then resume listening. The important additions are:

A “one-shot question” mode:
speak question
listen for exactly one user turn
return transcript to the pipeline
A “retry prompt” mode:
if reflection says “not singular,” speak the follow-up question
repeat up to max attempts without requiring the user to restate the whole task
This is essentially “slot filling,” but embedded inside a phone automation run. Alexa-style dialogue best practices explicitly call out handling both under-answering and over-answering while maintaining intent and minimizing back-and-forth. 

UX details that reduce latency and frustration
A few implementation details make the experience feel dramatically faster and more “agentic”:

When re-prompting, include the missing slot explicitly:
“I still need the destination address. Is it 123 Main St or 45 Park Ave?”
If you can extract candidates from the UI, speak 2–5 options max (not 20).
Allow the user to answer with:
“the first one,” “the Alex with the company logo,” “Alex Johnson,” etc.
Treat “silence” or unclear audio as a first-class outcome:
“I didn’t catch that—could you repeat just the name?”
These are consistent with grounding theory: short installments, explicit confirmation of mutual understanding, and quick repair when ambiguity is detected. 

Low-latency model and voice stack options
You asked specifically to use a “realtime GPT 1.5” plus the lowest-latency ElevenLabs stack. The up-to-date official docs strongly suggest two viable architectures.

Option A: Use OpenAI Realtime for the “question/reflection brain,” keep ElevenLabs for voice
OpenAI’s Realtime API supports long-lived, low-latency sessions over WebRTC (recommended for browser/mobile clients) and WebSocket (recommended for server-to-server). 

OpenAI’s docs show connecting to wss://api.openai.com/v1/realtime?model=gpt-realtime for server WebSockets. 

A 2025 OpenAI release post describes the gpt-realtime model as a more advanced speech-to-speech model and notes Realtime API features such as remote MCP servers, image inputs, and SIP phone calling support. 

If your “realtime GPT 1.5” refers to a specific internal model name you’re already using, the only thing I can say from public docs is: the current documented production Realtime model identifier is gpt-realtime. 

So, one critical integration question is the exact model id you want to target (see the questions section).

For the voice layer, ElevenLabs documents:

Flash v2.5 as an ultra-low latency TTS model (~75ms excluding network/app latency) with model ids like eleven_flash_v2_5. 
A tradeoff: Flash v2.5 disables some text normalization by default to preserve latency (phone numbers, dates, currencies can be read “unexpectedly”), and suggests pre-normalizing in your LLM or enabling apply_text_normalization (Enterprise-only per docs). 
Scribe v2 Realtime as a low-latency speech-to-text model (~150ms excluding network/app latency). 
This aligns extremely well with your goal: clarification questions are short, so Flash v2.5’s low latency shines, while the LLM can be responsible for normalizing anything that Flash would read awkwardly (e.g., “one two three four” for a code). 

Option B: Use OpenAI Realtime end-to-end for voice, reduce moving parts
If you want the absolute minimum pipeline latency and fewer vendor hops, OpenAI positions the Realtime API as a low-latency, multimodal interface that supports audio inputs and outputs and can stream speech incrementally. 

In practice, many teams still keep a dedicated TTS provider (like ElevenLabs) for voice identity/quality reasons, but if your priority is “fastest possible,” end-to-end Realtime is the architecture that removes one round-trip.

The tradeoff is engineering complexity: you must manage a long-lived Realtime session, audio chunking, interruption handling, and event processing (unless you adopt an SDK layer). OpenAI’s Agents SDK documentation describes a realtime layer (Python and TypeScript) that abstracts session lifecycle, streaming, history management, and tool approvals. 

Where “approvals / resumability” fits in
Your clarification loop is conceptually the same as “human-in-the-loop pause and resume,” which is exactly how OpenAI’s Agents SDK describes approval checkpoints and resumable runs: a run can pause, request approval/input, then resume from saved state. 

Even if you don’t adopt the SDK, mimicking that architecture (explicit paused states + resumable continuation) will make your agent’s behavior more reliable and easier to test.

Testing strategy, success metrics, and questions for you
What to test
Because this feature changes control flow (the agent can now pause for clarification rather than failing), tests should focus on correctness of state transitions and on eliminating infinite loops.

High-value tests include:

A run that requires one missing slot (e.g., “navigate to X,” but destination missing) and resolves after one audio query.
A run where the UI presents multiple matching choices (two contacts named Alex) and resolves after a second, more specific query.
A run where the user provides irrelevant info twice; the third attempt triggers a safe fallback (manual takeover or a structured picker).
A run where the agent requests confirmation and the user answers by voice; the system correctly routes “yes/no” to confirmation rather than treating it as a new command. This is explicitly called out as a common ambiguity in voice dialog systems. 
Metrics that show “more optimal and streamlined”
If you want to prove this is an improvement (not just a feature), track:

Mean time-to-completion (TTC) for common tasks.
Clarification rate: clarifications per run (aim: low, but non-zero).
Clarification success rate: % of clarifications resolved on attempt 1/2/3.
Failure mode shift: fewer “manual takeover” terminations for ambiguity-related failures.
User interruption rate (how often users cancel during clarifications).
Questions to lock down implementation details
To design the “actual path the model takes” in a way that matches your expectations, I need your answers to these specifics:

What exact model ids do you mean by “realtime GPT 1.5”? The current public Realtime docs name the model parameter gpt-realtime. 
 If you have an internal/preview model name, I need the exact string.

For ElevenLabs, can you confirm:

The TTS model id you want (eleven_flash_v2_5 is the documented Flash v2.5 id) 
The exact voice id / preset you intend to use (and whether you require number normalization, given Flash’s default behavior) 
Should “attempt 3 failed” fall back to manual takeover, or should it fall back to an on-screen picker UI (when candidates exist)? Voice UX guidance suggests you should avoid repeated clarification loops that frustrate users. 

What counts as “singularity” in your definition?

Is it strictly “maps to exactly one UI candidate,” or can the user provide extra constraints (e.g., “Alex from work”) and let the system search deeper?
Which kinds of user input are allowed to be captured and stored in session entities?

Message bodies, addresses, emails, phone numbers: these improve automation, but they also increase sensitivity. (Flash v2.5’s number normalization caveat makes this particularly relevant.) 
Do you want voice clarifications and voice confirmations to be able to execute irreversible actions without an on-screen confirm?

Voice systems often add explicit confirmations for higher-risk actions. 
Your current confirmation mechanism is a good place to enforce your safety policy—so the question is whether audio confirmation is sufficient, or whether you want “voice + visible confirm” for high-risk steps.
Where should the “clarification brain” live?

Inside the same step reasoner model call (the model decides to ask, and later decides it has enough info), or
As a separate reflection component that maps answers into typed slots and updates entities deterministically before resuming execution?
If you answer these, I can pin down the most streamlined variant of the loop (fewest model calls per clarification, smallest latency envelope, and the cleanest integration point with your existing runner + voice gateway).