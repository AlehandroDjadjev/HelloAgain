package com.example.control.model

/**
 * Full screen snapshot. nodes is EMPTY when isSensitive = true to prevent
 * sensitive content from leaking to the backend or logs.
 */
data class ScreenStateDto(
    val timestampMs: Long,
    val foregroundPackage: String?,
    val windowTitle: String?,
    val screenHash: String,
    val focusedElementRef: String?,
    val isSensitive: Boolean,
    val nodes: List<UiNodeDto>,
) {
    fun toMap(): Map<String, Any?> = mapOf(
        "timestampMs" to timestampMs,
        "foregroundPackage" to foregroundPackage,
        "windowTitle" to windowTitle,
        "screenHash" to screenHash,
        "focusedElementRef" to focusedElementRef,
        "isSensitive" to isSensitive,
        "nodes" to nodes.map { it.toMap() },
    )
}
