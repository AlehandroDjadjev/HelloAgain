package com.example.control.util

import android.graphics.Rect
import android.view.accessibility.AccessibilityNodeInfo
import com.example.control.model.RectDto
import com.example.control.model.ScreenStateDto
import com.example.control.model.UiNodeDto
import java.security.MessageDigest

/**
 * Walks the live AccessibilityNodeInfo tree and produces a ScreenStateDto snapshot.
 *
 * Key invariants:
 *  - All AccessibilityNodeInfo objects obtained via getChild() are recycled after use.
 *  - The root node passed in is NOT recycled here — callers own its lifecycle.
 *  - elementRef values ("n0", "n1", …) are stable within one snapshot but must
 *    not be stored or compared across different snapshots.
 *  - nodes is set to an empty list when isSensitive = true so sensitive content
 *    never reaches logs or the network layer.
 */
object ScreenTreeSerializer {

    private val SENSITIVE_KEYWORDS = setOf(
        "otp", "one-time", "security", "payment", "bank", "pin",
        "password", "passcode", "cvv", "credit card", "debit card",
    )

    /**
     * Produce a full ScreenStateDto from [root].
     * Pass null to get an error-state snapshot with an empty node list.
     */
    fun serialize(
        root: AccessibilityNodeInfo?,
        foregroundPackage: String?,
        windowTitle: String?,
        allowSensitiveNodes: Boolean = false,
    ): ScreenStateDto {
        if (root == null) {
            return ScreenStateDto(
                timestampMs = System.currentTimeMillis(),
                foregroundPackage = foregroundPackage,
                windowTitle = windowTitle,
                screenHash = "",
                focusedElementRef = null,
                isSensitive = false,
                nodes = emptyList(),
            )
        }

        val nodes = mutableListOf<UiNodeDto>()
        val counter = intArrayOf(0)
        val focusedRef = arrayOfNulls<String>(1)
        var sensitiveByFlag = false

        walkTree(
            node = root,
            out = nodes,
            counter = counter,
            focusedRef = focusedRef,
            onSensitiveFlag = { sensitiveByFlag = true },
        )

        val isSensitive = sensitiveByFlag || checkSensitiveByText(nodes)
        val screenHash = computeHash(foregroundPackage, nodes)

        return ScreenStateDto(
            timestampMs = System.currentTimeMillis(),
            foregroundPackage = foregroundPackage,
            windowTitle = windowTitle,
            screenHash = screenHash,
            focusedElementRef = focusedRef[0],
            isSensitive = isSensitive,
            // Redact node list for sensitive screens — hash is still stored for audit
            nodes = if (isSensitive && !allowSensitiveNodes) emptyList() else nodes,
        )
    }

    /**
     * Convert a single AccessibilityNodeInfo to a UiNodeDto with a given ref.
     * Used by findElement / findElements in the service.
     * Caller owns the lifecycle of [node].
     */
    fun nodeToDto(node: AccessibilityNodeInfo, ref: String): UiNodeDto {
        val bounds = Rect()
        node.getBoundsInScreen(bounds)
        return UiNodeDto(
            elementRef = ref,
            className = node.className?.toString(),
            text = node.text?.toString(),
            contentDesc = node.contentDescription?.toString(),
            viewId = node.viewIdResourceName,
            packageName = node.packageName?.toString(),
            parentRef = null,
            clickable = node.isClickable,
            longClickable = node.isLongClickable,
            scrollable = node.isScrollable,
            enabled = node.isEnabled,
            focused = node.isFocused,
            selected = node.isSelected,
            editable = node.isEditable,
            checkable = node.isCheckable,
            checked = node.isChecked,
            bounds = RectDto(bounds.left, bounds.top, bounds.right, bounds.bottom),
            indexInParent = 0,
            childCount = node.childCount,
            children = emptyList(),
        )
    }

    // ── Private helpers ───────────────────────────────────────────────────────

    private fun walkTree(
        node: AccessibilityNodeInfo,
        out: MutableList<UiNodeDto>,
        counter: IntArray,
        focusedRef: Array<String?>,
        onSensitiveFlag: () -> Unit,
        parentRef: String? = null,
        indexInParent: Int = 0,
    ): String {
        val ref = "n${counter[0]++}"
        val bounds = Rect()
        node.getBoundsInScreen(bounds)

        if (node.isPassword) onSensitiveFlag()
        if (node.isFocused) focusedRef[0] = ref

        val nodeIndex = out.size
        out.add(
            UiNodeDto(
                elementRef = ref,
                className = node.className?.toString(),
                text = node.text?.toString(),
                contentDesc = node.contentDescription?.toString(),
                viewId = node.viewIdResourceName,
                packageName = node.packageName?.toString(),
                parentRef = parentRef,
                clickable = node.isClickable,
                longClickable = node.isLongClickable,
                scrollable = node.isScrollable,
                enabled = node.isEnabled,
                focused = node.isFocused,
                selected = node.isSelected,
                editable = node.isEditable,
                checkable = node.isCheckable,
                checked = node.isChecked,
                bounds = RectDto(bounds.left, bounds.top, bounds.right, bounds.bottom),
                indexInParent = indexInParent,
                childCount = node.childCount,
                children = emptyList(),
            )
        )

        val childRefs = mutableListOf<String>()
        for (i in 0 until node.childCount) {
            val child = node.getChild(i) ?: continue
            try {
                childRefs += walkTree(
                    child,
                    out,
                    counter,
                    focusedRef,
                    onSensitiveFlag,
                    parentRef = ref,
                    indexInParent = i,
                )
            } finally {
                child.recycle()
            }
        }

        out[nodeIndex] = out[nodeIndex].copy(children = childRefs)
        return ref
    }

    /**
     * Secondary sensitive-screen heuristic based on visible text content.
     * Triggered after the node walk so we don't pay the cost unless needed.
     */
    private fun checkSensitiveByText(nodes: List<UiNodeDto>): Boolean {
        return nodes.any { node ->
            val combined = "${node.text.orEmpty()} ${node.contentDesc.orEmpty()}".lowercase()
            SENSITIVE_KEYWORDS.any { keyword -> combined.contains(keyword) }
        }
    }

    /**
     * Deterministic hash of the visible screen tree.
     * Inputs: foregroundPackage + ordered (className, text, contentDesc, focusMarker) per node.
     * Uses SHA-256 truncated to 16 hex chars (8 bytes) — sufficient for change detection.
     */
    private fun computeHash(
        foregroundPackage: String?,
        nodes: List<UiNodeDto>,
    ): String {
        val sb = StringBuilder()
        sb.append(foregroundPackage.orEmpty())
        nodes.forEach { node ->
            sb.append('|')
            sb.append(node.className.orEmpty())
            if (!node.contentDesc.isNullOrBlank()) sb.append(":cd=").append(node.contentDesc)
            if (!node.text.isNullOrBlank()) sb.append(":tx=").append(node.text)
            if (node.focused) sb.append(":F")
        }
        val digest = MessageDigest.getInstance("SHA-256")
        val bytes = digest.digest(sb.toString().toByteArray(Charsets.UTF_8))
        return bytes.take(8).joinToString("") { "%02x".format(it) }
    }
}
