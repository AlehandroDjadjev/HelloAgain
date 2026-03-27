from typing import Optional

from voice_gateway.domain.contracts import (
    VoiceConversationRequest,
    VoiceConversationResponse,
)
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
    ) -> VoiceConversationResponse:
        provider_status = {}
        warnings = []

        if audio_bytes:
            transcription = self.stt_provider.transcribe(
                audio_bytes,
                language=request.language,
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

        synthesis = self.tts_provider.synthesize(llm_result.text)
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

    def health_status(self) -> dict[str, str]:
        return {
            "stt": self.stt_provider.status(),
            "llm": self.llm_provider.status(),
            "tts": self.tts_provider.status(),
        }


gateway_core = VoiceGatewayCore()
