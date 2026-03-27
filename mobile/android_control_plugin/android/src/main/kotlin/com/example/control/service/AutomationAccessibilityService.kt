package com.example.control.service

import android.accessibilityservice.AccessibilityService
import android.accessibilityservice.GestureDescription
import android.content.Intent
import android.graphics.Bitmap
import android.graphics.Path
import android.os.Build
import android.os.Bundle
import android.util.Log
import android.view.Display
import android.view.accessibility.AccessibilityEvent
import android.view.accessibility.AccessibilityNodeInfo
import com.example.control.gateway.DeviceControlGateway
import com.example.control.model.ActionResultDto
import com.example.control.model.ScreenStateDto
import com.example.control.model.SelectorDto
import com.example.control.model.SessionConfigDto
import com.example.control.model.UiNodeDto
import com.example.control.util.NodeMatcher
import com.example.control.util.ScreenTreeSerializer
import java.io.ByteArrayOutputStream
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit

/**
 * Core Android automation engine.
 *
 * Architecture notes:
 *  - Declared in the plugin AndroidManifest, merged into the host app at build time.
 *  - Runs in the host app's process. Flutter communicates via the companion [instance]
 *    singleton reference, accessed from [AndroidControlPlugin] on the Flutter engine thread.
 *  - All gateway methods are called on a background thread by the Flutter bridge (Stage 4).
 *  - [onAccessibilityEvent] fires on the main thread; the callback is invoked inline.
 *    Heavy processing must be offloaded by the caller.
 *  - [onInterrupt] is called when the service is interrupted (e.g. screen locked).
 *    All transient state is cleared immediately.
 *
 * Memory contract:
 *  - rootInActiveWindow creates a new object each call; every method that fetches it
 *    must call .recycle() in a finally block.
 *  - Nodes obtained via NodeMatcher (obtain()'d copies) are recycled after use.
 */
class AutomationAccessibilityService : AccessibilityService(), DeviceControlGateway {

    companion object {
        private const val TAG = "AutomationA11y"

        /**
         * Singleton reference set in [onServiceConnected] and cleared in [onDestroy].
         * The Flutter bridge checks this to determine if the service is running.
         */
        @Volatile
        var instance: AutomationAccessibilityService? = null
            private set
    }

    /** Currently active session, set by [startSession]. Null when idle. */
    @Volatile
    private var activeSession: SessionConfigDto? = null

    /**
     * Lightweight callback invoked on every accessibility event.
     * Set by the Flutter bridge to receive screen-change notifications.
     * Cleared on [onInterrupt] and [stopSession].
     */
    @Volatile
    private var eventCallback: ((ScreenStateDto) -> Unit)? = null

    @Volatile
    private var lastScreenshotError: String? = null

    // ── Service lifecycle ─────────────────────────────────────────────────────

    override fun onServiceConnected() {
        super.onServiceConnected()
        instance = this
        Log.i(TAG, "AccessibilityService connected")
        // Re-attach any callback that was registered before the service started
        com.example.control.bridge.EventChannelHandler.pendingCallback?.let {
            setEventCallback(it)
        }
    }

    override fun onAccessibilityEvent(event: AccessibilityEvent?) {
        event ?: return
        val callback = eventCallback ?: return
        // Snapshot is lightweight; callback decides whether to forward to backend
        try {
            val snapshot = snapshotScreenState()
            callback(snapshot)
        } catch (e: Exception) {
            Log.w(TAG, "Error in onAccessibilityEvent snapshot: ${e.message}")
        }
    }

    override fun onInterrupt() {
        // Service interrupted (e.g. screen locked, another a11y service gained priority).
        // Clear transient execution state so no stale action can execute after resume.
        Log.w(TAG, "AccessibilityService interrupted — clearing transient state")
        eventCallback = null
    }

    override fun onDestroy() {
        super.onDestroy()
        instance = null
        activeSession = null
        eventCallback = null
        Log.i(TAG, "AccessibilityService destroyed")
    }

    // ── Event callback management (used by Flutter bridge) ───────────────────

    fun setEventCallback(callback: (ScreenStateDto) -> Unit) {
        eventCallback = callback
    }

    fun clearEventCallback() {
        eventCallback = null
    }

    // ── Screen snapshot ───────────────────────────────────────────────────────

    /**
     * Walk the current accessibility tree and return a structured snapshot.
     * Returns an empty-node snapshot if [rootInActiveWindow] is null.
     */
    fun snapshotScreenState(): ScreenStateDto {
        val root = rootInActiveWindow
        val pkg = root?.packageName?.toString()
        val title = windows?.firstOrNull { it.isActive }?.title?.toString()
        return try {
            ScreenTreeSerializer.serialize(
                root,
                pkg,
                title,
                allowSensitiveNodes = activeSession?.allowSensitiveNodes ?: false,
            )
        } finally {
            root?.recycle()
        }
    }

    // ── DeviceControlGateway implementation ───────────────────────────────────

    override fun getPermissionStatus(): Map<String, Boolean> = mapOf(
        "accessibilityService" to true,
        "overlayPermission" to android.provider.Settings.canDrawOverlays(this),
    )

    override fun startSession(config: SessionConfigDto): ActionResultDto {
        activeSession = config
        val serviceIntent = Intent(this, AutomationForegroundService::class.java).apply {
            putExtra(AutomationForegroundService.EXTRA_SESSION_ID, config.sessionId)
        }
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            startForegroundService(serviceIntent)
        } else {
            startService(serviceIntent)
        }
        Log.i(TAG, "Session started: ${config.sessionId}")
        val screen = snapshotScreenState()
        return ActionResultDto.success("Session started: ${config.sessionId}", screen)
    }

    override fun stopSession(sessionId: String): ActionResultDto {
        activeSession = null
        eventCallback = null
        stopService(Intent(this, AutomationForegroundService::class.java))
        Log.i(TAG, "Session stopped: $sessionId")
        return ActionResultDto.success("Session stopped: $sessionId")
    }

    override fun getForegroundApp(): String? {
        val root = rootInActiveWindow ?: return null
        return try {
            root.packageName?.toString()
        } finally {
            root.recycle()
        }
    }

    override fun isPackageInstalled(packageName: String): Boolean {
        return try {
            packageManager.getPackageInfo(packageName, 0)
            true
        } catch (_: android.content.pm.PackageManager.NameNotFoundException) {
            false
        }
    }

    override fun launchApp(packageName: String): ActionResultDto {
        val intent = packageManager.getLaunchIntentForPackage(packageName)
            ?: return ActionResultDto.failure("APP_NOT_FOUND", "No launch intent for '$packageName'")
        intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        startActivity(intent)
        val screen = snapshotScreenState()
        return ActionResultDto.success("Launched $packageName", screen)
    }

    override fun getScreenState(): ScreenStateDto = snapshotScreenState()

    override fun findElement(selector: SelectorDto): UiNodeDto? {
        val root = rootInActiveWindow ?: return null
        return try {
            val node = NodeMatcher.findFirst(root, selector) ?: return null
            try {
                ScreenTreeSerializer.nodeToDto(node, "match_0")
            } finally {
                node.recycle()
            }
        } finally {
            root.recycle()
        }
    }

    override fun findElements(selector: SelectorDto): List<UiNodeDto> {
        val root = rootInActiveWindow ?: return emptyList()
        return try {
            val nodes = NodeMatcher.findAll(root, selector)
            try {
                nodes.mapIndexed { i, n -> ScreenTreeSerializer.nodeToDto(n, "match_$i") }
            } finally {
                nodes.forEach { it.recycle() }
            }
        } finally {
            root.recycle()
        }
    }

    override fun tapElement(selector: SelectorDto): ActionResultDto =
        performOnNode(selector, "TAP") { node ->
            performClickWithAncestorFallback(node)
        }

    override fun longPressElement(selector: SelectorDto): ActionResultDto =
        performOnNode(selector, "LONG_PRESS") { node ->
            node.performAction(AccessibilityNodeInfo.ACTION_LONG_CLICK)
        }

    override fun focusElement(selector: SelectorDto): ActionResultDto {
        val root = rootInActiveWindow
            ?: return ActionResultDto.failure("NO_ROOT", "rootInActiveWindow is null")
        return try {
            Log.d(TAG, "FOCUS requested selector=${selectorSummary(selector)}")
            val node = NodeMatcher.findFirst(root, selector)
                ?: return ActionResultDto.failure(
                    "ELEMENT_NOT_FOUND",
                    "No node matches selector for action FOCUS",
                    takeScreenshot(),
                )
            try {
                Log.d(TAG, "FOCUS matched ${nodeSummary(node)}")
                if (!node.isEnabled) {
                    return ActionResultDto.failure(
                        "ELEMENT_NOT_CLICKABLE",
                        "Matched node is disabled",
                        takeScreenshot(),
                    )
                }

                if (node.isFocused) {
                    return ActionResultDto.success("FOCUS already satisfied", snapshotScreenState())
                }

                val focusOk = focusNode(node)
                val screen = snapshotScreenState()
                if (focusOk) {
                    Log.d(TAG, "FOCUS succeeded for ${nodeSummary(node)}")
                    ActionResultDto.success("FOCUS", screen)
                } else {
                    val focusSummary = currentInputFocusSummary(root)
                    val message = (
                        "FOCUS returned false for selector=${selectorSummary(selector)} " +
                            "matched=${nodeSummary(node)} currentFocus=$focusSummary"
                        )
                    Log.w(TAG, message)
                    ActionResultDto.failure(
                        "ACTION_FAILED",
                        message,
                    )
                }
            } finally {
                node.recycle()
            }
        } finally {
            root.recycle()
        }
    }

    override fun typeText(text: String): ActionResultDto {
        val root = rootInActiveWindow
            ?: return ActionResultDto.failure("NO_ROOT", "rootInActiveWindow is null")
        return try {
            val focused = root.findFocus(AccessibilityNodeInfo.FOCUS_INPUT)
                ?: return ActionResultDto.failure(
                    "ELEMENT_NOT_FOUND", "No focused editable field found"
                )
            try {
                val args = Bundle()
                args.putCharSequence(
                    AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, text
                )
                val ok = focused.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, args)
                val screen = snapshotScreenState()
                if (ok) ActionResultDto.success("Text set (${text.length} chars)", screen)
                else ActionResultDto.failure("ACTION_FAILED", "ACTION_SET_TEXT returned false")
            } finally {
                focused.recycle()
            }
        } finally {
            root.recycle()
        }
    }

    override fun clearFocusedField(): ActionResultDto {
        val root = rootInActiveWindow
            ?: return ActionResultDto.failure("NO_ROOT", "rootInActiveWindow is null")
        return try {
            val focused = root.findFocus(AccessibilityNodeInfo.FOCUS_INPUT)
                ?: return ActionResultDto.failure("ELEMENT_NOT_FOUND", "No focused editable field")
            try {
                val args = Bundle()
                args.putCharSequence(
                    AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, ""
                )
                val ok = focused.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, args)
                val screen = snapshotScreenState()
                if (ok) ActionResultDto.success("Field cleared", screen)
                else ActionResultDto.failure("ACTION_FAILED", "Clear via ACTION_SET_TEXT returned false")
            } finally {
                focused.recycle()
            }
        } finally {
            root.recycle()
        }
    }

    override fun scroll(direction: String): ActionResultDto {
        val root = rootInActiveWindow
            ?: return ActionResultDto.failure("NO_ROOT", "rootInActiveWindow is null")
        return try {
            val scrollable = findFirstScrollable(root)
                ?: return ActionResultDto.failure(
                    "ELEMENT_NOT_FOUND", "No scrollable container in current window"
                )
            try {
                val action = when (direction.lowercase()) {
                    "down", "forward" -> AccessibilityNodeInfo.ACTION_SCROLL_FORWARD
                    "up", "backward" -> AccessibilityNodeInfo.ACTION_SCROLL_BACKWARD
                    else -> return ActionResultDto.failure(
                        "INVALID_PARAMS", "Unknown scroll direction '$direction'"
                    )
                }
                val ok = scrollable.performAction(action)
                val screen = snapshotScreenState()
                if (ok) ActionResultDto.success("Scrolled $direction", screen)
                else ActionResultDto.failure("ACTION_FAILED", "Scroll action returned false")
            } finally {
                scrollable.recycle()
            }
        } finally {
            root.recycle()
        }
    }

    override fun swipe(
        startX: Int,
        startY: Int,
        endX: Int,
        endY: Int,
        durationMs: Long,
    ): ActionResultDto {
        val path = Path().apply {
            moveTo(startX.toFloat(), startY.toFloat())
            lineTo(endX.toFloat(), endY.toFloat())
        }
        val stroke = GestureDescription.StrokeDescription(path, 0L, durationMs)
        val gesture = GestureDescription.Builder().addStroke(stroke).build()

        var gestureSucceeded = false
        val latch = CountDownLatch(1)

        dispatchGesture(
            gesture,
            object : GestureResultCallback() {
                override fun onCompleted(gestureDescription: GestureDescription) {
                    gestureSucceeded = true
                    latch.countDown()
                }
                override fun onCancelled(gestureDescription: GestureDescription) {
                    latch.countDown()
                }
            },
            null,
        )

        // Wait for the gesture callback, bounded by duration + generous buffer
        latch.await(durationMs + 2000L, TimeUnit.MILLISECONDS)
        val screen = snapshotScreenState()
        return if (gestureSucceeded) {
            ActionResultDto.success("Swipe completed", screen)
        } else {
            ActionResultDto.failure("GESTURE_CANCELLED", "Swipe gesture was cancelled by the system")
        }
    }

    override fun goBack(): ActionResultDto {
        performGlobalAction(GLOBAL_ACTION_BACK)
        val screen = snapshotScreenState()
        return ActionResultDto.success("BACK", screen)
    }

    override fun goHome(): ActionResultDto {
        performGlobalAction(GLOBAL_ACTION_HOME)
        val screen = snapshotScreenState()
        return ActionResultDto.success("HOME", screen)
    }

    override fun takeScreenshot(): ByteArray? {
        lastScreenshotError = null
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.R) {
            lastScreenshotError = "SCREENSHOT_UNSUPPORTED: requires Android 11 / API 30+"
            Log.w(TAG, "takeScreenshot failed: $lastScreenshotError")
            return null
        }
        var result: ByteArray? = null
        var failureReason: String? = null
        val latch = CountDownLatch(1)
        takeScreenshot(
            Display.DEFAULT_DISPLAY,
            mainExecutor,
            object : TakeScreenshotCallback {
                override fun onSuccess(screenshot: ScreenshotResult) {
                    val bmp = Bitmap.wrapHardwareBuffer(
                        screenshot.hardwareBuffer, screenshot.colorSpace
                    )?.copy(Bitmap.Config.ARGB_8888, false)
                    screenshot.hardwareBuffer.close()
                    if (bmp != null) {
                        val out = ByteArrayOutputStream()
                        bmp.compress(Bitmap.CompressFormat.JPEG, 65, out)
                        bmp.recycle()
                        result = out.toByteArray()
                    } else {
                        failureReason = "SCREENSHOT_BITMAP_CONVERSION_FAILED"
                    }
                    latch.countDown()
                }
                override fun onFailure(errorCode: Int) {
                    failureReason = "SCREENSHOT_CAPTURE_FAILED(code=$errorCode)"
                    Log.w(TAG, "takeScreenshot onFailure code=$errorCode")
                    latch.countDown()
                }
            },
        )
        val completed = latch.await(3, TimeUnit.SECONDS)
        if (!completed) {
            failureReason = "SCREENSHOT_TIMEOUT: capture did not complete within 3000ms"
        }
        if (result == null) {
            lastScreenshotError = failureReason ?: "SCREENSHOT_FAILED: capture returned no image"
            Log.w(TAG, "takeScreenshot failed: $lastScreenshotError")
        }
        return result
    }

    override fun getLastScreenshotError(): String? = lastScreenshotError

    // ── Private helpers ───────────────────────────────────────────────────────

    /**
     * Generic helper that finds a node, performs an [action], captures screen state,
     * and returns a typed result. Recycles both the root and the matched node.
     */
    private fun performOnNode(
        selector: SelectorDto,
        actionName: String,
        action: (AccessibilityNodeInfo) -> Boolean,
    ): ActionResultDto {
        val root = rootInActiveWindow
            ?: return ActionResultDto.failure("NO_ROOT", "rootInActiveWindow is null")
        return try {
            Log.d(TAG, "$actionName requested selector=${selectorSummary(selector)}")
            val node = NodeMatcher.findFirst(root, selector)
                ?: return ActionResultDto.failure(
                    "ELEMENT_NOT_FOUND",
                    "No node matches selector for action $actionName",
                    takeScreenshot(),
                )
            try {
                Log.d(TAG, "$actionName matched ${nodeSummary(node)}")
                if (!node.isEnabled) {
                    return ActionResultDto.failure(
                        "ELEMENT_NOT_CLICKABLE",
                        "Matched node is disabled",
                        takeScreenshot(),
                    )
                }
                val ok = action(node)
                val screen = snapshotScreenState()
                if (ok) {
                    Log.d(TAG, "$actionName succeeded for ${nodeSummary(node)}")
                    ActionResultDto.success(actionName, screen)
                } else {
                    val message = (
                        "$actionName returned false for selector=${selectorSummary(selector)} " +
                            "matched=${nodeSummary(node)}"
                        )
                    Log.w(TAG, message)
                    ActionResultDto.failure("ACTION_FAILED", message)
                }
            } finally {
                node.recycle()
            }
        } finally {
            root.recycle()
        }
    }

    /**
     * BFS for the first scrollable node in the tree.
     * Returns an obtain()'d copy — caller must recycle.
     */
    private fun findFirstScrollable(root: AccessibilityNodeInfo): AccessibilityNodeInfo? {
        if (root.isScrollable) return AccessibilityNodeInfo.obtain(root)
        for (i in 0 until root.childCount) {
            val child = root.getChild(i) ?: continue
            try {
                val found = findFirstScrollable(child)
                if (found != null) return found
            } finally {
                child.recycle()
            }
        }
        return null
    }

    private fun focusNode(node: AccessibilityNodeInfo): Boolean {
        if (node.performAction(AccessibilityNodeInfo.ACTION_FOCUS)) {
            return true
        }
        if (node.isEditable) {
            return performClickWithAncestorFallback(node)
        }
        return false
    }

    private fun performClickWithAncestorFallback(node: AccessibilityNodeInfo): Boolean {
        if (node.performAction(AccessibilityNodeInfo.ACTION_CLICK)) {
            return true
        }

        var parent = node.parent
        while (parent != null) {
            try {
                if (parent.isEnabled && parent.isClickable) {
                    Log.d(TAG, "Trying clickable ancestor ${nodeSummary(parent)}")
                    if (parent.performAction(AccessibilityNodeInfo.ACTION_CLICK)) {
                        return true
                    }
                }

                val nextParent = parent.parent
                parent.recycle()
                parent = nextParent
            } catch (e: Exception) {
                try {
                    parent.recycle()
                } catch (_: Exception) {
                }
                return false
            }
        }

        return false
    }

    private fun selectorSummary(selector: SelectorDto): String = listOfNotNull(
        selector.elementRef?.let { "elementRef=$it" },
        selector.viewId?.let { "viewId=$it" },
        selector.className?.let { "className=$it" },
        selector.textEquals?.let { "textEquals=$it" },
        selector.textContains?.let { "textContains=$it" },
        selector.contentDescEquals?.let { "contentDescEquals=$it" },
        selector.contentDescContains?.let { "contentDescContains=$it" },
        selector.packageName?.let { "packageName=$it" },
        selector.clickable?.let { "clickable=$it" },
        selector.enabled?.let { "enabled=$it" },
        selector.focused?.let { "focused=$it" },
        selector.indexInParent?.let { "indexInParent=$it" },
    ).joinToString(", ", prefix = "{", postfix = "}").ifEmpty { "{}" }

    private fun nodeSummary(node: AccessibilityNodeInfo): String {
        val bounds = android.graphics.Rect()
        node.getBoundsInScreen(bounds)
        return listOfNotNull(
            "class=${node.className}",
            node.viewIdResourceName?.let { "viewId=$it" },
            node.text?.toString()?.takeIf { it.isNotBlank() }?.let { "text=$it" },
            node.contentDescription?.toString()?.takeIf { it.isNotBlank() }?.let { "contentDesc=$it" },
            "clickable=${node.isClickable}",
            "enabled=${node.isEnabled}",
            "focused=${node.isFocused}",
            "editable=${node.isEditable}",
            "bounds=$bounds",
        ).joinToString(", ", prefix = "{", postfix = "}")
    }

    private fun currentInputFocusSummary(root: AccessibilityNodeInfo): String {
        val focused = root.findFocus(AccessibilityNodeInfo.FOCUS_INPUT) ?: return "{none}"
        return try {
            nodeSummary(focused)
        } finally {
            focused.recycle()
        }
    }
}
