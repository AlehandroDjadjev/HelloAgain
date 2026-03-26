import abc
from typing import Optional, Dict, Any

class SpeechToTextProvider(abc.ABC):
    """
    Abstract interface for Speech-to-Text conversion.
    Swappable with different providers (e.g., Whisper, Google, Azure).
    """

    @abc.abstractmethod
    def transcribe(self, audio_data: bytes) -> str:
        pass


class TextToSpeechProvider(abc.ABC):
    """
    Abstract interface for Text-to-Speech conversion.
    Swappable (e.g., ElevenLabs, Google, Azure).
    """

    @abc.abstractmethod
    def synthesize(self, text: str, voice_id: Optional[str] = None) -> bytes:
        pass


class VADProvider(abc.ABC):
    """
    Abstract interface for Voice Activity Detection.
    To be used later to handle continuous real-time audio streams.
    """

    @abc.abstractmethod
    def is_speech(self, audio_chunk: bytes) -> bool:
        pass


class MockSTTProvider(SpeechToTextProvider):
    def transcribe(self, audio_data: bytes) -> str:
        return "Моля, проверете статуса на сметката ми."  # Translated: "Please check my account status."

class MockTTSProvider(TextToSpeechProvider):
    def synthesize(self, text: str, voice_id: Optional[str] = None) -> bytes:
        # Mocking an audio byte stream for the synthesized text
        return b"MOCK_AUDIO_DATA_" + text.encode("utf-8")

class MockVADProvider(VADProvider):
    def is_speech(self, audio_chunk: bytes) -> bool:
        return True
