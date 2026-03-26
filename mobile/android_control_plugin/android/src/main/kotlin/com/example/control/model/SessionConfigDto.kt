package com.example.control.model

/**
 * Configuration pushed from the backend when starting an automation session.
 * Mirrors PolicyConfig from the backend agent_policy schema.
 */
data class SessionConfigDto(
    val sessionId: String,
    /** Packages this session is permitted to interact with. */
    val allowedPackages: List<String>,
    /** "always" | "onIrreversible" | "never" — matches backend ConfirmationRecord.sensitivity rules. */
    val confirmationMode: String,
    val maxStepCount: Int,
    val allowTextEntry: Boolean,
    val allowSendActions: Boolean,
) {
    companion object {
        @Suppress("UNCHECKED_CAST")
        fun fromMap(map: Map<String, Any?>): SessionConfigDto = SessionConfigDto(
            sessionId = map["sessionId"] as String,
            allowedPackages = (map["allowedPackages"] as? List<*>)
                ?.filterIsInstance<String>() ?: emptyList(),
            confirmationMode = (map["confirmationMode"] as? String) ?: "always",
            maxStepCount = (map["maxStepCount"] as? Number)?.toInt() ?: 30,
            allowTextEntry = (map["allowTextEntry"] as? Boolean) ?: true,
            allowSendActions = (map["allowSendActions"] as? Boolean) ?: false,
        )
    }
}
