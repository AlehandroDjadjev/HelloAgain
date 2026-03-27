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
    val parentRef: String?,
    val clickable: Boolean,
    val longClickable: Boolean,
    val scrollable: Boolean,
    val enabled: Boolean,
    val focused: Boolean,
    val selected: Boolean,
    val editable: Boolean,
    val checkable: Boolean,
    val checked: Boolean,
    val bounds: RectDto,
    val indexInParent: Int,
    val childCount: Int,
    val children: List<String> = emptyList(),
) {
    fun toMap(): Map<String, Any?> = mapOf(
        "elementRef" to elementRef,
        "className" to className,
        "text" to text,
        "contentDesc" to contentDesc,
        "viewId" to viewId,
        "packageName" to packageName,
        "parentRef" to parentRef,
        "clickable" to clickable,
        "longClickable" to longClickable,
        "scrollable" to scrollable,
        "enabled" to enabled,
        "focused" to focused,
        "selected" to selected,
        "editable" to editable,
        "checkable" to checkable,
        "checked" to checked,
        "bounds" to bounds.toMap(),
        "indexInParent" to indexInParent,
        "childCount" to childCount,
        "children" to children,
    )
}
