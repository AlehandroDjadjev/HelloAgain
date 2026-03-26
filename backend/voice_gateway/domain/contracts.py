from dataclasses import dataclass, field
from typing import Dict, Any, Optional

@dataclass
class MemoryContext:
    """
    STRICTLY LIGHTWEIGHT session state.
    Used ONLY for UI tracking and current operation context (e.g. knowing if we are waiting for a user confirmation).
    Not a long-term vector DB memory store.
    """
    user_id: str
    session_id: str
    current_action: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class VoiceGatewayRequest:
    """
    Incoming request FROM the user to the Gateway.
    The message has been transcribed by Faster-Whisper (STT) or sent as text.
    """
    user_id: str
    session_id: str
    message: str


@dataclass
class BackendSpeakRequest:
    """
    Request FROM a backend agent TO the Voice Gateway, asking it to speak to the user.
    """
    user_id: str
    session_id: str
    agent_name: str
    raw_data: Dict[str, Any]


@dataclass
class VoiceGatewayResponse:
    """
    The output format pushed to the user (via Piper TTS).
    """
    spoken_text: str  # Short, calm, easy to understand Bulgarian sentence
    status: str = "success"
    structured_data: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ConversationTurn:
    """
    Minimal ledger for debugging.
    """
    source_message: Optional[str] = None
    agent_name: Optional[str] = None
    shaped_response: Optional[VoiceGatewayResponse] = None
    error: Optional[str] = None
