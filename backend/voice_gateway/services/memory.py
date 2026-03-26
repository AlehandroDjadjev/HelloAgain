from typing import Dict
from voice_gateway.domain.contracts import MemoryContext, ConversationTurn

class MemoryService:
    """
    Lightweight in-memory session store.
    Tracks minimal UI state for the current session only.
    """

    def __init__(self):
        self._memory_store: Dict[str, MemoryContext] = {}

    def get_context(self, user_id: str, session_id: str) -> MemoryContext:
        """
        Retrieves the current UI state context for a session.
        """
        key = f"{user_id}::{session_id}"
        if key not in self._memory_store:
            self._memory_store[key] = MemoryContext(user_id=user_id, session_id=session_id)
        
        return self._memory_store[key]

    def set_current_action(self, user_id: str, session_id: str, action: str) -> None:
        """
        Sets the current expected action (e.g., waiting for confirmation).
        """
        context = self.get_context(user_id, session_id)
        context.current_action = action
        key = f"{user_id}::{session_id}"
        self._memory_store[key] = context

# Create a singleton instance
memory_service = MemoryService()
