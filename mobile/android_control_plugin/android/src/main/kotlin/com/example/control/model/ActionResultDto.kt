package com.example.control.model

/**
 * Result of any device action. Always returned even on failure so the backend
 * can log the reason and decide on retry / escalation.
 */
data class ActionResultDto(
    val success: Boolean,
    /** Machine-readable code. "OK" on success; one of ActionErrorCode values on failure. */
    val code: String,
    val message: String? = null,
    val updatedScreenState: ScreenStateDto? = null,
) {
    fun toMap(): Map<String, Any?> = mapOf(
        "success" to success,
        "code" to code,
        "message" to message,
        "updatedScreenState" to updatedScreenState?.toMap(),
    )

    companion object {
        fun success(message: String? = null, screen: ScreenStateDto? = null) =
            ActionResultDto(true, "OK", message, screen)

        fun failure(code: String, message: String) =
            ActionResultDto(false, code, message, null)
    }
}
