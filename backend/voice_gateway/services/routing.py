from typing import Dict, Any, Optional
from voice_gateway.domain.contracts import VoiceGatewayRequest

class AgentDispatcher:
    """
    Very simple dispatcher. DOES NOT make intelligent decisions.
    Merely looks for basic triggers to hand off the message to pre-registered backend systems.
    """

    def dispatch_request(self, request: VoiceGatewayRequest) -> Optional[str]:
        """
        Returns the name of the backend agent to notify, if any.
        """
        message_lower = request.message.lower()

        # Basic hardcoded triggers (not an AI router)
        if "сметка" in message_lower or "баланс" in message_lower:
            return "AccountAgent"
        elif "здравей" in message_lower or "здрасти" in message_lower:
            return "GreetingAgent"
        
        return None

# Create singleton instance
agent_dispatcher = AgentDispatcher()
