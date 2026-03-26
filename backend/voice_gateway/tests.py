import json
from django.test import TestCase, Client
from voice_gateway.domain.contracts import VoiceGatewayRequest
from voice_gateway.services.shaping import response_shaper
from voice_gateway.services.routing import agent_dispatcher

class VoiceGatewayUnitTests(TestCase):
    def test_routing_logic(self):
        """Test the dispatcher properly assigns agents based on simple triggers"""
        req_account = VoiceGatewayRequest(user_id="1", session_id="1", message="Искам да проверя моята сметка")
        agent_name = agent_dispatcher.dispatch_request(req_account)
        self.assertEqual(agent_name, "AccountAgent")

        req_greet = VoiceGatewayRequest(user_id="1", session_id="1", message="Здравейте, трябва ми помощ")
        agent_greet = agent_dispatcher.dispatch_request(req_greet)
        self.assertEqual(agent_greet, "GreetingAgent")

    def test_shaping_logic(self):
        """Test the shaping layer produces short, Bulgarian text."""
        # Account formatting
        text_account = response_shaper.shape_response("AccountAgent", {"status": "success", "balance": "100"})
        self.assertIn("100", text_account)
        self.assertIn("сметката", text_account)

        # Fallback formatting
        text_fallback = response_shaper.shape_response("FallbackAgent", {"status": "success"})
        self.assertIn("повторили", text_fallback)

    def test_user_api_endpoint(self):
        """Test E2E flow via the user interact API View."""
        client = Client()
        payload = {
            "user_id": "test_user_01",
            "session_id": "session_01",
            "message": "Здравейте!"
        }
        
        response = client.post('/api/voice-gateway/interact/', data=json.dumps(payload), content_type='application/json')
        
        self.assertEqual(response.status_code, 200)
        data = response.json()
        
        self.assertEqual(data["status"], "success")
        self.assertIn("Здравейте", data["spoken_text"])
        self.assertEqual(data["structured_data"]["agent_used"], "GreetingAgent")

    def test_agent_speak_endpoint(self):
        """Test E2E flow for an agent pushing a message."""
        client = Client()
        payload = {
            "user_id": "test_user_01",
            "session_id": "session_01",
            "agent_name": "AccountAgent",
            "raw_data": {"status": "success", "balance": "550"}
        }

        response = client.post('/api/voice-gateway/agent-speak/', data=json.dumps(payload), content_type='application/json')
        
        self.assertEqual(response.status_code, 200)
        data = response.json()
        
        self.assertEqual(data["status"], "success")
        self.assertIn("550", data["spoken_text"])
        self.assertEqual(data["structured_data"]["agent_used"], "AccountAgent")

