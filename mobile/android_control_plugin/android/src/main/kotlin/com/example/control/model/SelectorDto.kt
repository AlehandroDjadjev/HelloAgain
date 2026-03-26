package com.example.control.model

/**
 * Identifies a UI node for a device action.
 *
 * Resolution priority (mirrors the shared JSON Schema):
 *   elementRef → viewId → contentDescEquals → textEquals
 *   → textContains / contentDescContains → className + indexInParent
 *
 * All fields are optional. NodeMatcher applies AND logic: every non-null
 * field must match. At least one field must be non-null.
 */
data class SelectorDto(
    /** Snapshot-scoped ref like "n12". Resolved before tree traversal. */
    val elementRef: String? = null,
    val textEquals: String? = null,
    val textContains: String? = null,
    val contentDescEquals: String? = null,
    val contentDescContains: String? = null,
    val viewId: String? = null,
    val className: String? = null,
    val packageName: String? = null,
    val clickable: Boolean? = null,
    val enabled: Boolean? = null,
    val focused: Boolean? = null,
    val indexInParent: Int? = null,
) {
    companion object {
        fun fromMap(map: Map<String, Any?>): SelectorDto = SelectorDto(
            elementRef = map["elementRef"] as? String,
            textEquals = map["textEquals"] as? String,
            textContains = map["textContains"] as? String,
            contentDescEquals = map["contentDescEquals"] as? String,
            contentDescContains = map["contentDescContains"] as? String,
            viewId = map["viewId"] as? String,
            className = map["className"] as? String,
            packageName = map["packageName"] as? String,
            clickable = map["clickable"] as? Boolean,
            enabled = map["enabled"] as? Boolean,
            focused = map["focused"] as? Boolean,
            indexInParent = (map["indexInParent"] as? Number)?.toInt(),
        )
    }
}
