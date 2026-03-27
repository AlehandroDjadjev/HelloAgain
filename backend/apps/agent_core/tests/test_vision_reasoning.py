from __future__ import annotations

from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase, override_settings

from apps.agent_core.services.vision_reasoning import VisionReasoningService


class VisionReasoningServiceConfigTests(SimpleTestCase):
    @override_settings(
        OPENAI_LLM_MODEL="gpt-5-mini",
        OPENAI_LLM_API_KEY="sk-test",
        OPENAI_LLM_BASE_URL="https://api.openai.com/v1",
        OPENAI_LLM_TIMEOUT=27,
    )
    @patch("apps.agent_core.services.vision_reasoning.LLMClient.from_reasoning_provider")
    def test_openai_reasoning_provider_uses_session_provider(self, mock_from_reasoning_provider):
        mock_client = MagicMock()
        mock_from_reasoning_provider.return_value = mock_client

        service = VisionReasoningService(reasoning_provider="openai")

        self.assertIs(service._llm, mock_client)
        mock_from_reasoning_provider.assert_called_once_with("openai")
