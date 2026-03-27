import base64
import json
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase

from voice_gateway.domain.contracts import (
    LLMResult,
    SpeechSynthesisResult,
    TranscriptionResult,
)


class _FakeSTTProvider:
    def transcribe(self, audio_data, language=None):
        return TranscriptionResult(
            text="Recognized speech",
            source="fake_stt",
            warnings=[],
        )

    def status(self):
        return "ready"


class _FakeLLMProvider:
    def generate_reply(self, prompt, session_id, user_id):
        return LLMResult(
            text=f"Reply for: {prompt}",
            source="fake_llm",
            warnings=[],
        )

    def status(self):
        return "ready"


class _FakeTTSProvider:
    def synthesize(self, text, voice_id=None):
        wav_bytes = base64.b64decode(
            "UklGRiQAAABXQVZFZm10IBAAAAABAAEARKwAAIhYAQACABAAZGF0YQAAAAA=",
        )
        return SpeechSynthesisResult(
            audio_bytes=wav_bytes,
            source="fake_tts",
            mime_type="audio/wav",
            warnings=[],
        )

    def status(self):
        return "ready"


class VoiceGatewayTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.providers = {
            "stt_provider": _FakeSTTProvider(),
            "llm_provider": _FakeLLMProvider(),
            "tts_provider": _FakeTTSProvider(),
        }

    def test_transcribe_endpoint(self):
        with patch.multiple("voice_gateway.views.gateway_core", **self.providers):
            response = self.client.post(
                "/api/voice-gateway/transcribe/",
                data={"audio": self._make_audio_file()},
            )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["status"], "success")
        self.assertEqual(data["transcript"], "Recognized speech")
        self.assertEqual(data["provider"], "fake_stt")

    def test_speak_endpoint(self):
        with patch.multiple("voice_gateway.views.gateway_core", **self.providers):
            response = self.client.post(
                "/api/voice-gateway/speak/",
                data=json.dumps({"text": "Hello from test"}),
                content_type="application/json",
            )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["status"], "success")
        self.assertEqual(data["text"], "Hello from test")
        self.assertEqual(data["provider"], "fake_tts")
        self.assertTrue(data["audio_base64"])
        self.assertEqual(data["audio_mime_type"], "audio/wav")

    def test_conversation_endpoint(self):
        with patch.multiple("voice_gateway.views.gateway_core", **self.providers):
            response = self.client.post(
                "/api/voice-gateway/conversation/",
                data={"audio": self._make_audio_file()},
            )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["status"], "success")
        self.assertEqual(data["transcript"], "Recognized speech")
        self.assertEqual(data["assistant_text"], "Reply for: Recognized speech")
        self.assertEqual(
            data["provider_status"],
            {"stt": "fake_stt", "llm": "fake_llm", "tts": "fake_tts"},
        )
        self.assertTrue(data["assistant_audio_base64"])

    def test_health_endpoint(self):
        with patch.multiple("voice_gateway.views.gateway_core", **self.providers):
            response = self.client.get("/api/voice-gateway/health/")

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["status"], "ok")
        self.assertEqual(
            data["providers"],
            {"stt": "ready", "llm": "ready", "tts": "ready"},
        )

    def _make_audio_file(self):
        return SimpleUploadedFile(
            "test.wav",
            b"RIFF....WAVEfmt ",
            content_type="audio/wav",
        )
