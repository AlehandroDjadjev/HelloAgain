from typing import Optional

from voice_gateway.domain.contracts import (
    BackendSpeakRequest,
    VoiceConversationRequest,
    VoiceConversationResponse,
    VoiceGatewayRequest,
    VoiceGatewayResponse,
)
from voice_gateway.services.providers import (
    GoogleCloudSpeechSTTProvider,
    PiperTTSProvider,
    PlaceholderQwenLLMProvider,
)


class VoiceGatewayCore:
    def __init__(self, stt_provider=None, llm_provider=None, tts_provider=None):
        self.stt_provider = stt_provider or GoogleCloudSpeechSTTProvider()
        self.llm_provider = llm_provider or PlaceholderQwenLLMProvider()
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

    def process_user_request(self, request: VoiceGatewayRequest) -> VoiceGatewayResponse:
        response = self.process_turn(
            VoiceConversationRequest(
                user_id=request.user_id,
                session_id=request.session_id,
                message=request.message,
            ),
        )
        return VoiceGatewayResponse(
            spoken_text=response.assistant_text,
            structured_data={
                "provider_status": response.provider_status,
                "warnings": response.warnings,
            },
        )

    def process_agent_request(
        self,
        request: BackendSpeakRequest,
    ) -> VoiceGatewayResponse:
        text = str(
            request.raw_data.get("text")
            or request.raw_data.get("message")
            or request.raw_data,
        ).strip()
        if not text:
            raise ValueError("No text was provided for agent_speak.")

        synthesis = self.tts_provider.synthesize(text)
        return VoiceGatewayResponse(
            spoken_text=text,
            structured_data={
                "provider_status": {"tts": synthesis.source},
                "warnings": synthesis.warnings,
                "audio_mime_type": synthesis.mime_type,
            },
        )

    def health_status(self) -> dict[str, str]:
        return {
            "stt": self.stt_provider.status(),
            "llm": self.llm_provider.status(),
            "tts": self.tts_provider.status(),
        }


gateway_core = VoiceGatewayCore()
