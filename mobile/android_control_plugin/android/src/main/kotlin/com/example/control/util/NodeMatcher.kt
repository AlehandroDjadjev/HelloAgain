package com.example.control.util

import android.view.accessibility.AccessibilityNodeInfo
import com.example.control.model.SelectorDto

/**
 * Finds AccessibilityNodeInfo objects in the live tree that match a SelectorDto.
 *
 * Match logic:
 *  - All non-null selector fields must match (AND logic).
 *  - textContains and contentDescContains use case-insensitive substring matching.
 *  - elementRef is NOT resolved here — it's snapshot-scoped and must be resolved
 *    by the service before the tree traversal (via the snapshot node list index).
 *
 * Memory contract:
 *  - findFirst returns an obtain()'d copy — caller must recycle() it.
 *  - findAll returns obtain()'d copies — caller must recycle() each.
 *  - The [root] passed in is never recycled here — caller owns it.
 *  - All child nodes fetched internally via getChild() are recycled after use.
 */
object NodeMatcher {

    /** Returns the first matching node (obtain()'d copy) or null. Caller must recycle. */
    fun findFirst(root: AccessibilityNodeInfo, selector: SelectorDto): AccessibilityNodeInfo? {
        val results = mutableListOf<AccessibilityNodeInfo>()
        collectMatches(
            node = root,
            selector = selector,
            out = results,
            stopAfterFirst = true,
            currentIndexInParent = 0,
        )
        return results.firstOrNull()
    }

    /** Returns all matching nodes (obtain()'d copies). Caller must recycle each. */
    fun findAll(root: AccessibilityNodeInfo, selector: SelectorDto): List<AccessibilityNodeInfo> {
        val results = mutableListOf<AccessibilityNodeInfo>()
        collectMatches(
            node = root,
            selector = selector,
            out = results,
            stopAfterFirst = false,
            currentIndexInParent = 0,
        )
        return results
    }

    // ── Private ───────────────────────────────────────────────────────────────

    private fun collectMatches(
        node: AccessibilityNodeInfo,
        selector: SelectorDto,
        out: MutableList<AccessibilityNodeInfo>,
        stopAfterFirst: Boolean,
        currentIndexInParent: Int,
    ) {
        if (matches(node, selector, currentIndexInParent)) {
            out.add(AccessibilityNodeInfo.obtain(node))
            if (stopAfterFirst) return
        }

        for (i in 0 until node.childCount) {
            val child = node.getChild(i) ?: continue
            try {
                collectMatches(child, selector, out, stopAfterFirst, i)
                // Early exit bubbles up when first match found
                if (stopAfterFirst && out.isNotEmpty()) return
            } finally {
                child.recycle()
            }
        }
    }

    /**
     * Returns true if [node] satisfies every non-null field in [selector].
     * elementRef is always false here — must be resolved upstream via snapshot index.
     */
    private fun matches(
        node: AccessibilityNodeInfo,
        selector: SelectorDto,
        indexInParent: Int,
    ): Boolean {
        // elementRef is resolved outside tree traversal
        if (selector.elementRef != null) return false

        selector.viewId?.let {
            if (node.viewIdResourceName != it) return false
        }
        selector.className?.let {
            if (node.className?.toString() != it) return false
        }
        selector.packageName?.let {
            if (node.packageName?.toString() != it) return false
        }
        selector.textEquals?.let {
            if (node.text?.toString() != it) return false
        }
        selector.textContains?.let {
            val t = node.text?.toString() ?: return false
            if (!t.contains(it, ignoreCase = true)) return false
        }
        selector.contentDescEquals?.let {
            if (node.contentDescription?.toString() != it) return false
        }
        selector.contentDescContains?.let {
            val cd = node.contentDescription?.toString() ?: return false
            if (!cd.contains(it, ignoreCase = true)) return false
        }
        selector.clickable?.let {
            if (node.isClickable != it) return false
        }
        selector.enabled?.let {
            if (node.isEnabled != it) return false
        }
        selector.focused?.let {
            if (node.isFocused != it) return false
        }
        selector.indexInParent?.let {
            if (indexInParent != it) return false
        }

        return true
    }
}
