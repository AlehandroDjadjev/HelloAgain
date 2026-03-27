package com.example.control.util

import android.view.accessibility.AccessibilityNodeInfo
import com.example.control.model.SelectorDto

/**
 * Finds AccessibilityNodeInfo objects in the live tree that match a SelectorDto.
 *
 * Match logic:
 *  - All non-null selector fields must match (AND logic).
 *  - textContains and contentDescContains use case-insensitive substring matching.
 *  - elementRef is resolved here using the same preorder traversal used by
 *    ScreenTreeSerializer when it assigns snapshot refs ("n0", "n1", ...).
 *
 * Memory contract:
 *  - findFirst returns an obtain()'d copy; caller must recycle().
 *  - findAll returns obtain()'d copies; caller must recycle() each.
 *  - The root passed in is never recycled here; caller owns it.
 *  - All child nodes fetched internally via getChild() are recycled after use.
 */
object NodeMatcher {

    /** Returns the first matching node (obtain()'d copy) or null. Caller must recycle. */
    fun findFirst(root: AccessibilityNodeInfo, selector: SelectorDto): AccessibilityNodeInfo? {
        selector.elementRef?.let { targetRef ->
            return resolveByElementRef(root, targetRef)
        }

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
        selector.elementRef?.let { targetRef ->
            return listOfNotNull(resolveByElementRef(root, targetRef))
        }

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

    private fun resolveByElementRef(
        root: AccessibilityNodeInfo,
        targetRef: String,
    ): AccessibilityNodeInfo? {
        val counter = intArrayOf(0)
        return resolveByElementRefRecursive(root, targetRef, counter)
    }

    private fun resolveByElementRefRecursive(
        node: AccessibilityNodeInfo,
        targetRef: String,
        counter: IntArray,
    ): AccessibilityNodeInfo? {
        val currentRef = "n${counter[0]++}"
        if (currentRef == targetRef) {
            return AccessibilityNodeInfo.obtain(node)
        }

        for (i in 0 until node.childCount) {
            val child = node.getChild(i) ?: continue
            try {
                val found = resolveByElementRefRecursive(child, targetRef, counter)
                if (found != null) {
                    return found
                }
            } finally {
                child.recycle()
            }
        }
        return null
    }

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
                if (stopAfterFirst && out.isNotEmpty()) return
            } finally {
                child.recycle()
            }
        }
    }

    /**
     * Returns true if [node] satisfies every non-null field in [selector].
     * elementRef is handled by findFirst/findAll before tree matching.
     */
    private fun matches(
        node: AccessibilityNodeInfo,
        selector: SelectorDto,
        indexInParent: Int,
    ): Boolean {
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
            val text = node.text?.toString() ?: return false
            if (!text.contains(it, ignoreCase = true)) return false
        }
        selector.contentDescEquals?.let {
            if (node.contentDescription?.toString() != it) return false
        }
        selector.contentDescContains?.let {
            val contentDesc = node.contentDescription?.toString() ?: return false
            if (!contentDesc.contains(it, ignoreCase = true)) return false
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
