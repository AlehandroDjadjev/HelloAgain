package com.example.control.model

/**
 * Serialised representation of one AccessibilityNodeInfo in the current snapshot.
 * elementRef is stable within the snapshot but must not be persisted across snapshots.
 */
data class UiNodeDto(
    val elementRef: String,
    val className: String?,
    val text: String?,
    val contentDesc: String?,
    val viewId: String?,
    val packageName: String?,
    val clickable: Boolean,
    val enabled: Boolean,
    val focused: Boolean,
    val editable: Boolean,
    val bounds: RectDto,
    val childCount: Int,
) {
    fun toMap(): Map<String, Any?> = mapOf(
        "elementRef" to elementRef,
        "className" to className,
        "text" to text,
        "contentDesc" to contentDesc,
        "viewId" to viewId,
        "packageName" to packageName,
        "clickable" to clickable,
        "enabled" to enabled,
        "focused" to focused,
        "editable" to editable,
        "bounds" to bounds.toMap(),
        "childCount" to childCount,
    )
}
