from typing import Optional

from voice_gateway.domain.contracts import (
    VoiceConversationRequest,
    VoiceConversationResponse,
)
from voice_gateway.services.audio import prepare_audio_for_stt
from voice_gateway.services.providers import (
    GoogleCloudSpeechSTTProvider,
    OpenAILLMProvider,
    PiperTTSProvider,
)


class VoiceGatewayCore:
    def __init__(self, stt_provider=None, llm_provider=None, tts_provider=None):
        self.stt_provider = stt_provider or GoogleCloudSpeechSTTProvider()
        self.llm_provider = llm_provider or OpenAILLMProvider()
        self.tts_provider = tts_provider or PiperTTSProvider()

    def process_turn(
        self,
        request: VoiceConversationRequest,
        audio_bytes: Optional[bytes] = None,
        audio_content_type: Optional[str] = None,
    ) -> VoiceConversationResponse:
        provider_status = {}
        warnings = []

        if audio_bytes:
            transcription = self.transcribe_audio(
                audio_bytes,
                language=request.language,
                content_type=audio_content_type,
            )
            transcript = transcription.text
            provider_status["stt"] = transcription.source
            warnings.extend(transcription.warnings)
        else:
            transcript = request.message.strip()
            provider_status["stt"] = "skipped_text_input"

        if not transcript:
            raise ValueError("No transcript was produced from the incoming request.")

        llm_result = self.llm_provider.generate_reply(
            prompt=transcript,
            session_id=request.session_id,
            user_id=request.user_id,
        )
        provider_status["llm"] = llm_result.source
        warnings.extend(llm_result.warnings)

        synthesis = self.speak_text(llm_result.text)
        provider_status["tts"] = synthesis.source
        warnings.extend(synthesis.warnings)

        return VoiceConversationResponse(
            transcript=transcript,
            assistant_text=llm_result.text,
            assistant_audio_bytes=synthesis.audio_bytes,
            assistant_audio_mime_type=synthesis.mime_type,
            provider_status=provider_status,
            warnings=warnings,
        )

    def transcribe_audio(
        self,
        audio_bytes: bytes,
        language: Optional[str] = None,
        content_type: Optional[str] = None,
    ):
        prepared_audio = prepare_audio_for_stt(
            audio_bytes,
            content_type=content_type,
        )
        transcription = self.stt_provider.transcribe(
            prepared_audio.audio_bytes,
            language=language,
            content_type=prepared_audio.content_type,
        )
        transcription.warnings.extend(prepared_audio.warnings)
        return transcription

    def speak_text(self, text: str):
        return self.tts_provider.synthesize(text)

    def get_response(
        self,
        prompt: str,
        session_id: str,
        user_id: str,
    ) -> VoiceConversationResponse:
        normalized_prompt = prompt.strip()
        if not normalized_prompt:
            raise ValueError("Prompt is required.")

        llm_result = self.llm_provider.generate_reply(
            prompt=normalized_prompt,
            session_id=session_id,
            user_id=user_id,
        )
        synthesis = self.speak_text(llm_result.text)

        return VoiceConversationResponse(
            transcript=normalized_prompt,
            assistant_text=llm_result.text,
            assistant_audio_bytes=synthesis.audio_bytes,
            assistant_audio_mime_type=synthesis.mime_type,
            provider_status={
                "stt": "skipped_text_input",
                "llm": llm_result.source,
                "tts": synthesis.source,
            },
            warnings=[*llm_result.warnings, *synthesis.warnings],
        )

    def health_status(self) -> dict[str, str]:
        return {
            "stt": self.stt_provider.status(),
            "llm": self.llm_provider.status(),
            "tts": self.tts_provider.status(),
        }


gateway_core = VoiceGatewayCore()
