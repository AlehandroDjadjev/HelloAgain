import logging
from typing import Dict, Any

from voice_gateway.domain.contracts import (
    VoiceGatewayRequest,
    BackendSpeakRequest,
    VoiceGatewayResponse,
    ConversationTurn,
)
from voice_gateway.services.routing import agent_dispatcher
from voice_gateway.services.shaping import response_shaper
from voice_gateway.services.memory import memory_service
from voice_gateway.services.providers import MockSTTProvider, MockTTSProvider

logger = logging.getLogger(__name__)

class VoiceGatewayCore:
    """
    Central Communication Hub.
    No decision-making. Just routes IO between the User, the Agents, and the Shape/TTS layers.
    """

    def __init__(self):
        self.stt_provider = MockSTTProvider()
        self.tts_provider = MockTTSProvider()

    def process_user_request(self, request: VoiceGatewayRequest) -> VoiceGatewayResponse:
        """
        Flow 1: User says something. Gateway routes it to the backend system.
        """
        turn = ConversationTurn(source_message=request.message)

        try:
            # 1. Dispatch to the right backend system via simple triggers
            agent_name = agent_dispatcher.dispatch_request(request)
            turn.agent_name = agent_name

            if not agent_name:
                # No agent handles this, return a generic shape
                return self._finalize_turn(turn, "FallbackAgent", {"status": "success", "message": "unrecognized_intent"})

            # 2. Call the agent (In a real system this sends an HTTP request or event to the backend agent)
            # For this MVP, we simulate a synchronous RPC call to the backend agent.
            raw_response = self._mock_call_agent(agent_name, request.message)

            # 3. Shape the response into speech
            return self._finalize_turn(turn, agent_name, raw_response)

        except Exception as e:
            logger.exception(f"Error processing user request: {e}")
            turn.error = str(e)
            return self._error_response()

    def process_agent_request(self, request: BackendSpeakRequest) -> VoiceGatewayResponse:
        """
        Flow 2: Backend system proactively wants to speak to the user.
        """
        turn = ConversationTurn(source_message="[AGENT_INITIATED]", agent_name=request.agent_name)
        
        try:
            # Shape the raw data provided by the agent into spoken Bulgarian
            return self._finalize_turn(turn, request.agent_name, request.raw_data)
            
        except Exception as e:
            logger.exception(f"Error processing agent speak request: {e}")
            turn.error = str(e)
            return self._error_response()

    def _finalize_turn(self, turn: ConversationTurn, agent_name: str, raw_data: Dict[str, Any]) -> VoiceGatewayResponse:
        """
        Passes data through the shaping layer and builds the final TTS response.
        """
        spoken_text = response_shaper.shape_response(agent_name, raw_data)
        gateway_response = VoiceGatewayResponse(
            spoken_text=spoken_text,
            structured_data={"agent_used": agent_name}
        )
        turn.shaped_response = gateway_response
        return gateway_response
        
    def _error_response(self) -> VoiceGatewayResponse:
        return VoiceGatewayResponse(
            status="error",
            spoken_text="Извинете, има проблем със системата. Моля, опитайте по-късно.",
        )

    def _mock_call_agent(self, agent_name: str, message: str) -> Dict[str, Any]:
        """
        Simulates the backend system processing the forwarded request.
        """
        if agent_name == "AccountAgent":
            return {"status": "success", "balance": "150.50"}
        elif agent_name == "GreetingAgent":
            return {"status": "success", "welcomed": True}
        else:
            return {"status": "success"}

# Singleton instance
gateway_core = VoiceGatewayCore()
