package com.example.control.bridge

import android.os.Handler
import android.os.Looper
import android.util.Log
import com.example.control.model.ScreenStateDto
import com.example.control.service.AutomationAccessibilityService
import io.flutter.plugin.common.EventChannel

/**
 * Manages the EventChannel stream from the AccessibilityService to Flutter.
 *
 * Lifecycle:
 *  - [onListen] is called when Dart subscribes. Installs the screen-change
 *    callback on [AutomationAccessibilityService] and starts forwarding events.
 *  - [onCancel] is called when Dart unsubscribes. Clears the callback.
 *
 * Thread safety:
 *  - [AutomationAccessibilityService.onAccessibilityEvent] fires on the Android
 *    main thread, so [EventSink.success] is called on the main thread — which is
 *    correct for EventChannel sinks.
 *
 * Service lifecycle:
 *  - If the service is killed and restarted, the next event snapshot will
 *    resume delivery automatically because [AutomationAccessibilityService]
 *    calls the stored callback reference.
 */
class EventChannelHandler : EventChannel.StreamHandler {

    private val mainHandler = Handler(Looper.getMainLooper())

    override fun onListen(arguments: Any?, sink: EventChannel.EventSink) {
        Log.i(TAG, "EventChannel: Dart subscribed")

        val service = AutomationAccessibilityService.instance
        if (service == null) {
            Log.w(TAG, "EventChannel: service not running at subscribe time; " +
                "events will flow once the service starts.")
        }

        // Install the callback. The service may be null now but will be set
        // later when/if the OS starts the AccessibilityService.
        val callback: (ScreenStateDto) -> Unit = { screenState ->
            val event = buildScreenStateEvent(screenState)
            mainHandler.post {
                try {
                    sink.success(event)
                    if (screenState.isSensitive) {
                        sink.success(buildSensitiveScreenEvent(screenState))
                    }
                } catch (e: Exception) {
                    Log.w(TAG, "Error posting event to sink: ${e.message}")
                }
            }
        }

        service?.setEventCallback(callback)

        // Store callback so we can re-install if the service restarts
        pendingCallback = callback
    }

    override fun onCancel(arguments: Any?) {
        Log.i(TAG, "EventChannel: Dart unsubscribed")
        AutomationAccessibilityService.instance?.clearEventCallback()
        pendingCallback = null
    }

    // ── Event map builders ────────────────────────────────────────────────────

    private fun buildScreenStateEvent(screenState: ScreenStateDto): Map<String, Any?> =
        mapOf(
            "type" to "screenStateUpdated",
            "screenState" to screenState.toMap(),
        )

    private fun buildSensitiveScreenEvent(screenState: ScreenStateDto): Map<String, Any?> =
        mapOf(
            "type" to "sensitiveScreenDetected",
            "sessionId" to "",
            "foregroundPackage" to (screenState.foregroundPackage ?: ""),
        )

    /**
     * Emit a [ConfirmationRequested] event to Dart.
     * Called by [AutomationAccessibilityService] when a REQUEST_CONFIRMATION
     * step is reached during plan execution.
     */
    fun emitConfirmationRequested(
        sink: EventChannel.EventSink,
        sessionId: String,
        actionId: String,
        prompt: String,
    ) {
        mainHandler.post {
            sink.success(
                mapOf(
                    "type" to "confirmationRequested",
                    "sessionId" to sessionId,
                    "actionId" to actionId,
                    "prompt" to prompt,
                )
            )
        }
    }

    /**
     * Emit an [ActionFailed] event to Dart.
     */
    fun emitActionFailed(
        sink: EventChannel.EventSink,
        actionId: String,
        code: String,
        message: String?,
    ) {
        mainHandler.post {
            sink.success(
                mapOf(
                    "type" to "actionFailed",
                    "actionId" to actionId,
                    "code" to code,
                    "message" to message,
                )
            )
        }
    }

    companion object {
        private const val TAG = "EventChannelHandler"

        /**
         * Stored so a restarted service can pick up the active callback.
         * Set by [onListen], cleared by [onCancel].
         */
        @Volatile
        var pendingCallback: ((ScreenStateDto) -> Unit)? = null
    }
}
