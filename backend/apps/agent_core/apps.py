from django.apps import AppConfig
from django.conf import settings

import logging

logger = logging.getLogger(__name__)
_READY_CHECK_DONE = False


class AgentCoreConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.agent_core"
    label = "agent_core"
    verbose_name = "Agent Core"

    def ready(self) -> None:
        global _READY_CHECK_DONE
        if _READY_CHECK_DONE:
            return
        _READY_CHECK_DONE = True

        from .llm_client import _PROVIDER_DEFAULTS

        provider = str(getattr(settings, "LLM_PROVIDER", "") or "").lower()
        model = str(getattr(settings, "LLM_MODEL", "") or "")
        api_key = str(getattr(settings, "LLM_API_KEY", "") or "")

        if provider not in _PROVIDER_DEFAULTS:
            logger.warning(
                "Agent Core startup: LLM_PROVIDER=%r is not recognized. Valid providers: %s",
                provider,
                ", ".join(sorted(_PROVIDER_DEFAULTS.keys())),
            )
        else:
            logger.info(
                "Agent Core startup: LLM provider=%s model=%s",
                provider,
                model or _PROVIDER_DEFAULTS[provider]["model"],
            )

        if provider in {"groq", "openai"} and not api_key:
            logger.warning(
                "Agent Core startup: LLM_API_KEY is empty for cloud provider '%s'. "
                "Offline development is still allowed, but live requests will fail.",
                provider,
            )
