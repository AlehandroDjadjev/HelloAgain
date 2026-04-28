package com.example.frontend

import android.Manifest
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import android.os.Handler
import android.os.Looper
import androidx.core.content.ContextCompat
import io.flutter.plugin.common.BinaryMessenger
import io.flutter.plugin.common.EventChannel
import io.flutter.plugin.common.MethodCall
import io.flutter.plugin.common.MethodChannel

class MicBridgePlugin(
    private val context: Context,
    messenger: BinaryMessenger,
) : MethodChannel.MethodCallHandler, EventChannel.StreamHandler {

    companion object {
        private const val METHOD_CHANNEL_NAME = "com.example.frontend/mic_bridge"
        private const val EVENT_CHANNEL_NAME = "com.example.frontend/mic_bridge/events"
        private val mainHandler = Handler(Looper.getMainLooper())

        @Volatile
        private var eventSink: EventChannel.EventSink? = null

        @Volatile
        private var serviceRunning = false

        fun setServiceRunning(running: Boolean) {
            serviceRunning = running
            if (running) {
                emitEvent("serviceReady", payload = mapOf("state" to "armed_idle"))
            }
        }

        fun isServiceRunning(): Boolean = serviceRunning

        fun emitTtsStarted(requestId: String, prompt: String) {
            emitEvent(
                "ttsStarted",
                requestId = requestId,
                payload = mapOf("prompt" to prompt),
            )
        }

        fun emitTtsFinished(requestId: String) {
            emitEvent("ttsFinished", requestId = requestId)
        }

        fun emitListeningStarted(requestId: String, timeoutMs: Long) {
            emitEvent(
                "listeningStarted",
                requestId = requestId,
                payload = mapOf("timeoutMs" to timeoutMs),
            )
        }

        fun emitPartialTranscript(requestId: String, transcript: String) {
            emitEvent(
                "partialTranscript",
                requestId = requestId,
                payload = mapOf("transcript" to transcript),
            )
        }

        fun emitFinalTranscript(
            requestId: String,
            transcript: String,
            audioBase64: String,
            audioMimeType: String,
        ) {
            emitEvent(
                "finalTranscript",
                requestId = requestId,
                payload = mapOf(
                    "transcript" to transcript,
                    "audioBase64" to audioBase64,
                    "audioMimeType" to audioMimeType,
                ),
            )
        }

        fun emitError(requestId: String?, code: String, message: String) {
            emitEvent(
                "error",
                requestId = requestId,
                payload = mapOf(
                    "code" to code,
                    "message" to message,
                ),
            )
        }

        private fun emitEvent(
            event: String,
            requestId: String? = null,
            payload: Map<String, Any?> = emptyMap(),
        ) {
            val envelope = linkedMapOf<String, Any?>(
                "event" to event,
                "requestId" to requestId,
            )
            envelope.putAll(payload)
            mainHandler.post {
                eventSink?.success(envelope)
            }
        }
    }

    private val methodChannel = MethodChannel(messenger, METHOD_CHANNEL_NAME)
    private val eventChannel = EventChannel(messenger, EVENT_CHANNEL_NAME)

    init {
        methodChannel.setMethodCallHandler(this)
        eventChannel.setStreamHandler(this)
    }

    fun dispose() {
        methodChannel.setMethodCallHandler(null)
        eventChannel.setStreamHandler(null)
    }

    override fun onMethodCall(call: MethodCall, result: MethodChannel.Result) {
        when (call.method) {
            "startMicBridgeService" -> {
                if (!hasRecordAudioPermission()) {
                    result.error(
                        "MIC_PERMISSION_MISSING",
                        "Microphone permission is required before starting the mic bridge service.",
                        null,
                    )
                    return
                }
                startMicBridgeService(
                    Intent(context, MicBridgeForegroundService::class.java).apply {
                        action = MicBridgeForegroundService.ACTION_START_SERVICE
                    },
                )
                result.success(null)
            }

            "stopMicBridgeService" -> {
                if (isServiceRunning()) {
                    startMicBridgeService(
                        Intent(context, MicBridgeForegroundService::class.java).apply {
                            action = MicBridgeForegroundService.ACTION_STOP_SERVICE
                        },
                    )
                }
                result.success(null)
            }

            "beginOneShotQuery" -> {
                val args = call.arguments as? Map<*, *>
                val requestId = args?.get("request_id")?.toString()?.trim().orEmpty()
                if (requestId.isBlank()) {
                    result.error(
                        "MIC_BRIDGE_MISSING_REQUEST_ID",
                        "Mic bridge query is missing request_id.",
                        null,
                    )
                    return
                }
                if (!isServiceRunning()) {
                    result.error(
                        "MIC_BRIDGE_NOT_READY",
                        "Mic bridge service must be started while the app is visible before beginning a background query.",
                        null,
                    )
                    return
                }
                if (!hasRecordAudioPermission()) {
                    result.error(
                        "MIC_PERMISSION_MISSING",
                        "Microphone permission is required before beginning a background query.",
                        null,
                    )
                    return
                }

                val timeoutMs = when (val value = args?.get("timeout_ms")) {
                    is Number -> value.toLong()
                    is String -> value.toLongOrNull() ?: 12000L
                    else -> 12000L
                }

                startMicBridgeService(
                    Intent(context, MicBridgeForegroundService::class.java).apply {
                        action = MicBridgeForegroundService.ACTION_BEGIN_ONE_SHOT_QUERY
                        putExtra(MicBridgeForegroundService.EXTRA_REQUEST_ID, requestId)
                        putExtra(
                            MicBridgeForegroundService.EXTRA_QUESTION,
                            args?.get("question")?.toString() ?: "",
                        )
                        putExtra(
                            MicBridgeForegroundService.EXTRA_BASE_URL,
                            args?.get("base_url")?.toString() ?: "",
                        )
                        putExtra(
                            MicBridgeForegroundService.EXTRA_USER_ID,
                            args?.get("user_id")?.toString() ?: "",
                        )
                        putExtra(
                            MicBridgeForegroundService.EXTRA_SESSION_ID,
                            args?.get("session_id")?.toString() ?: "",
                        )
                        putExtra(
                            MicBridgeForegroundService.EXTRA_LANGUAGE,
                            args?.get("language")?.toString() ?: "",
                        )
                        putExtra(MicBridgeForegroundService.EXTRA_TIMEOUT_MS, timeoutMs)
                    },
                )
                result.success(
                    mapOf(
                        "request_id" to requestId,
                        "accepted" to true,
                    ),
                )
            }

            "cancelListening" -> {
                val requestId =
                    (call.arguments as? Map<*, *>)?.get("request_id")?.toString()?.trim().orEmpty()
                if (isServiceRunning()) {
                    startMicBridgeService(
                        Intent(context, MicBridgeForegroundService::class.java).apply {
                            action = MicBridgeForegroundService.ACTION_CANCEL_LISTENING
                            if (requestId.isNotBlank()) {
                                putExtra(MicBridgeForegroundService.EXTRA_REQUEST_ID, requestId)
                            }
                        },
                    )
                }
                result.success(null)
            }

            else -> result.notImplemented()
        }
    }

    override fun onListen(arguments: Any?, events: EventChannel.EventSink?) {
        eventSink = events
        if (isServiceRunning()) {
            emitEvent("serviceReady", payload = mapOf("state" to "armed_idle"))
        }
    }

    override fun onCancel(arguments: Any?) {
        if (eventSink != null) {
            eventSink = null
        }
    }

    private fun startMicBridgeService(intent: Intent) {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            ContextCompat.startForegroundService(context, intent)
        } else {
            context.startService(intent)
        }
    }

    private fun hasRecordAudioPermission(): Boolean =
        ContextCompat.checkSelfPermission(
            context,
            Manifest.permission.RECORD_AUDIO,
        ) == PackageManager.PERMISSION_GRANTED
}
