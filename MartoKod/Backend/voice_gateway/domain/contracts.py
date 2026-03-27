import base64
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class VoiceConversationRequest:
    user_id: str = "anonymous"
    session_id: str = "default_session"
    message: str = ""
    language: Optional[str] = None


@dataclass
class TranscriptionResult:
    text: str
    source: str
    warnings: List[str] = field(default_factory=list)


@dataclass
class LLMResult:
    text: str
    source: str
    warnings: List[str] = field(default_factory=list)


@dataclass
class SpeechSynthesisResult:
    audio_bytes: bytes
    source: str
    mime_type: str = "audio/wav"
    warnings: List[str] = field(default_factory=list)


@dataclass
class VoiceConversationResponse:
    transcript: str
    assistant_text: str
    assistant_audio_bytes: bytes
    assistant_audio_mime_type: str = "audio/wav"
    status: str = "success"
    provider_status: Dict[str, str] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)

    def to_api_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "transcript": self.transcript,
            "assistant_text": self.assistant_text,
            "assistant_audio_base64": base64.b64encode(
                self.assistant_audio_bytes,
            ).decode("ascii"),
            "assistant_audio_mime_type": self.assistant_audio_mime_type,
            "provider_status": self.provider_status,
            "warnings": self.warnings,
        }
