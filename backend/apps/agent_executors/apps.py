from django.apps import AppConfig


class AgentExecutorsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.agent_executors"
    label = "agent_executors"
    verbose_name = "Agent Executors"

    def ready(self) -> None:
        # Importing each executor module triggers @ExecutorRegistry.register
        import apps.agent_executors.whatsapp.executor  # noqa: F401
        import apps.agent_executors.maps.executor      # noqa: F401
        import apps.agent_executors.chrome.executor    # noqa: F401
        import apps.agent_executors.gmail.executor     # noqa: F401
        import apps.agent_executors.brawlstars.executor  # noqa: F401
