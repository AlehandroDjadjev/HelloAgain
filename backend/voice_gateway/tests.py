import base64
import json
import math
import os
import struct
import tempfile
import wave
from io import BytesIO
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import RequestFactory, SimpleTestCase

from voice_gateway.services.audio import prepare_audio_for_stt
from voice_gateway.services.gateway import VoiceGatewayCore
from voice_gateway.services.providers import (
    BACKEND_ENV_PATH,
    GoogleCloudSpeechSTTProvider,
    OpenAILLMProvider,
    ProviderNotReadyError,
)
from voice_gateway.views import (
    get_response_view,
    live_test_view,
    speak_view,
    transcribe_view,
)


def _wav_with_silence(
    *,
    sample_rate: int = 16000,
    tone_duration_ms: int = 500,
    leading_silence_ms: int = 450,
    trailing_silence_ms: int = 600,
) -> bytes:
    amplitude = 12000
    frequency = 440
    samples = []

    def append_silence(duration_ms: int):
        frame_count = int(sample_rate * duration_ms / 1000)
        samples.extend([0] * frame_count)

    def append_tone(duration_ms: int):
        frame_count = int(sample_rate * duration_ms / 1000)
        for index in range(frame_count):
            value = int(
                amplitude
                * math.sin(2 * math.pi * frequency * (index / sample_rate)),
            )
            samples.append(value)

    append_silence(leading_silence_ms)
    append_tone(tone_duration_ms)
    append_silence(trailing_silence_ms)

    output = BytesIO()
    with wave.open(output, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(sample_rate)
        writer.writeframes(
            b"".join(struct.pack("<h", sample) for sample in samples),
        )
    return output.getvalue()


class DummyTranscription:
    def __init__(self, text: str):
        self.text = text
        self.source = "google_stub"
        self.warnings = ["stubbed=true"]


class DummyLLMResult:
    def __init__(self, text: str):
        self.text = text
        self.source = "openai_stub"
        self.warnings = ["llm_stub=true"]


class DummySynthesis:
    def __init__(self, audio_bytes: bytes):
        self.audio_bytes = audio_bytes
        self.source = "piper_stub"
        self.mime_type = "audio/wav"
        self.warnings = ["tts_stub=true"]


class DummySTTProvider:
    def transcribe(self, audio_data, language=None, content_type=None):
        return DummyTranscription("Trimmed speech")

    def status(self):
        return "ready"


class DummyLLMProvider:
    def generate_reply(self, prompt, session_id, user_id):
        return DummyLLMResult(f"Echo: {prompt}")

    def status(self):
        return "ready"


class DummyTTSProvider:
    def __init__(self, audio_bytes: bytes):
        self.audio_bytes = audio_bytes

    def synthesize(self, text, voice_id=None):
        return DummySynthesis(self.audio_bytes)

    def status(self):
        return "ready"


class AudioPreparationTests(SimpleTestCase):
    def test_prepare_audio_for_stt_trims_silence_with_vad(self):
        wav_bytes = _wav_with_silence()

        prepared = prepare_audio_for_stt(wav_bytes, content_type="audio/wav")

        self.assertTrue(prepared.vad_applied)
        self.assertTrue(prepared.speech_detected)
        self.assertIsNotNone(prepared.original_duration_ms)
        self.assertIsNotNone(prepared.processed_duration_ms)
        self.assertLess(prepared.processed_duration_ms, prepared.original_duration_ms)
        self.assertIn("vad_applied=true", prepared.warnings)


class GatewayFlowTests(SimpleTestCase):
    def test_get_response_generates_text_and_audio(self):
        wav_bytes = _wav_with_silence(tone_duration_ms=150)
        gateway = VoiceGatewayCore(
            stt_provider=DummySTTProvider(),
            llm_provider=DummyLLMProvider(),
            tts_provider=DummyTTSProvider(wav_bytes),
        )

        result = gateway.get_response(
            prompt="Hello there",
            session_id="session-1",
            user_id="user-1",
        )

        self.assertEqual(result.transcript, "Hello there")
        self.assertEqual(result.assistant_text, "Echo: Hello there")
        self.assertEqual(result.assistant_audio_bytes, wav_bytes)
        self.assertEqual(result.provider_status["llm"], "openai_stub")
        self.assertEqual(result.provider_status["tts"], "piper_stub")


class GoogleSttProviderTests(SimpleTestCase):
    def test_provider_loads_api_key_from_backend_env_file(self):
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as tmp_file:
            tmp_file.write('GOOGLE_STT_API_KEY="test-from-dotenv"\n')
            temp_path = tmp_file.name

        try:
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("GOOGLE_STT_API_KEY", None)
                with patch(
                    "voice_gateway.services.providers.BACKEND_ENV_PATH",
                    new=type(BACKEND_ENV_PATH)(temp_path),
                ):
                    provider = GoogleCloudSpeechSTTProvider()
            self.assertEqual(provider.api_key, "test-from-dotenv")
        finally:
            os.unlink(temp_path)

    def test_provider_requires_api_key_only(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GOOGLE_STT_API_KEY", None)
            with patch(
                "voice_gateway.services.providers.BACKEND_ENV_PATH",
                new=type(BACKEND_ENV_PATH)("missing-test.env"),
            ):
                provider = GoogleCloudSpeechSTTProvider()

            with self.assertRaises(ProviderNotReadyError) as exc:
                provider.transcribe(b"fake audio")

        self.assertIn("GOOGLE_STT_API_KEY", str(exc.exception))
        self.assertNotIn("GOOGLE_APPLICATION_CREDENTIALS", str(exc.exception))

    def test_status_reports_missing_api_key(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GOOGLE_STT_API_KEY", None)
            with patch(
                "voice_gateway.services.providers.BACKEND_ENV_PATH",
                new=type(BACKEND_ENV_PATH)("missing-test.env"),
            ):
                provider = GoogleCloudSpeechSTTProvider()

        self.assertEqual(provider.status(), "unavailable: api_key_missing")


class OpenAiProviderTests(SimpleTestCase):
    def test_default_system_prompt_is_older_adult_friendly(self):
        provider = OpenAILLMProvider(api_key="test-key")

        self.assertIn("older adult", provider.system_prompt.lower())
        self.assertIn("avoid emojis", provider.system_prompt.lower())
        self.assertIn("difficult words", provider.system_prompt.lower())

    def test_provider_loads_openai_key_from_backend_env_file(self):
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as tmp_file:
            tmp_file.write('OPENAI_LLM_API_KEY="test-openai-dotenv"\n')
            temp_path = tmp_file.name

        try:
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("OPENAI_LLM_API_KEY", None)
                with patch(
                    "voice_gateway.services.providers.BACKEND_ENV_PATH",
                    new=type(BACKEND_ENV_PATH)(temp_path),
                ):
                    provider = OpenAILLMProvider()
            self.assertEqual(provider.api_key, "test-openai-dotenv")
        finally:
            os.unlink(temp_path)


class VoiceGatewayViewTests(SimpleTestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.wav_bytes = _wav_with_silence()

    def test_transcribe_view_returns_message_from_uploaded_audio(self):
        upload = SimpleUploadedFile(
            "speech.wav",
            self.wav_bytes,
            content_type="audio/wav",
        )
        request = self.factory.post(
            "/voice/transcribe/",
            data={"audio": upload, "language": "bg-BG"},
        )

        with patch("voice_gateway.views.gateway_core.transcribe_audio") as mock_transcribe:
            mock_transcribe.return_value = DummyTranscription("Hello from audio")
            response = transcribe_view(request)

        self.assertEqual(response.status_code, 200)
        payload = json.loads(response.content.decode("utf-8"))
        self.assertEqual(payload["message"], "Hello from audio")
        self.assertEqual(payload["transcript"], "Hello from audio")

    def test_speak_view_can_return_binary_audio(self):
        request = self.factory.post(
            "/voice/speak/",
            data='{"text":"Speak this","response_format":"audio"}',
            content_type="application/json",
            HTTP_ACCEPT="audio/wav",
        )

        with patch("voice_gateway.views.gateway_core.speak_text") as mock_speak:
            mock_speak.return_value = DummySynthesis(self.wav_bytes)
            response = speak_view(request)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "audio/wav")
        self.assertEqual(response.content, self.wav_bytes)

    def test_get_response_view_returns_text_and_audio_payload(self):
        request = self.factory.post(
            "/voice/get-response/",
            data='{"prompt":"Hi"}',
            content_type="application/json",
        )

        with patch("voice_gateway.views.gateway_core.get_response") as mock_response:
            from voice_gateway.domain.contracts import VoiceConversationResponse

            mock_response.return_value = VoiceConversationResponse(
                transcript="Hi",
                assistant_text="Echo: Hi",
                assistant_audio_bytes=self.wav_bytes,
                provider_status={"llm": "openai_stub", "tts": "piper_stub"},
            )
            response = get_response_view(request)

        self.assertEqual(response.status_code, 200)
        payload = json.loads(response.content.decode("utf-8"))
        self.assertEqual(payload["prompt"], "Hi")
        self.assertEqual(payload["assistant_text"], "Echo: Hi")
        self.assertEqual(
            payload["assistant_audio_base64"],
            base64.b64encode(self.wav_bytes).decode("ascii"),
        )

    def test_live_test_view_renders_conversation_page(self):
        request = self.factory.get("/voice/live-test/")

        response = live_test_view(request)

        self.assertEqual(response.status_code, 200)
        content = response.content.decode("utf-8")
        self.assertIn("Allow Mic And Start", content)
        self.assertIn("Live two-way conversation.", content)
