from typing import Dict, Any

class ResponseShaper:
    """
    CRITICAL COMPONENT
    Converts raw agent output into spoken-friendly responses optimized for older Bulgarian users.
    Rules: Short, clear, polite, calm, one idea per sentence. NO jargon.
    """

    def shape_response(self, agent_name: str, raw_response: Dict[str, Any]) -> str:
        """
        Takes raw data and turns it into a short text string.
        In the future, an LLM might do this formatting. For MVP, we use templates based on the agent.
        """
        status = raw_response.get("status", "success")
        
        if status == "error":
            return "Извинете, възникна грешка. Моля, опитайте отново по-късно."

        if agent_name == "AccountAgent":
            balance = raw_response.get("balance", "неизвестен")
            return f"Разбрах. Проверих сметката ви. Разполагате с {balance} лева."

        if agent_name == "GreetingAgent":
            return "Здравейте! С какво мога да ви бъда полезен днес?"

        if agent_name == "FallbackAgent":
            return "Не съм сигурен, че разбрах добре. Бихте ли повторили въпроса си?"

        # Generic fallback
        return "Добре, записах го."

# Create singleton instance
response_shaper = ResponseShaper()
