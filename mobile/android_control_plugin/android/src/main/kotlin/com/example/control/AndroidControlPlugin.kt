package com.example.control

import android.content.Context
import android.util.Log
import com.example.control.bridge.EventChannelHandler
import com.example.control.bridge.MethodChannelHandler
import io.flutter.embedding.engine.plugins.FlutterPlugin
import io.flutter.plugin.common.EventChannel
import io.flutter.plugin.common.MethodChannel

/**
 * Flutter plugin entry point for the Android control layer.
 *
 * Registers:
 *  - MethodChannel ["com.example.control/gateway"]: routes Dart calls to
 *    [AutomationAccessibilityService] via [MethodChannelHandler].
 *  - EventChannel  ["com.example.control/events"]:  streams screen-change
 *    and automation events from [AutomationAccessibilityService] to Dart
 *    via [EventChannelHandler].
 *
 * The plugin handles graceful degradation when the AccessibilityService is
 * not yet running: permission-check calls succeed, action calls return
 * SERVICE_NOT_ENABLED, and the event stream opens silently (events begin
 * flowing once the service starts).
 */
class AndroidControlPlugin : FlutterPlugin {

    private lateinit var methodChannel: MethodChannel
    private lateinit var eventChannel: EventChannel
    private lateinit var appContext: Context

    private lateinit var methodHandler: MethodChannelHandler
    private val eventHandler = EventChannelHandler()

    override fun onAttachedToEngine(binding: FlutterPlugin.FlutterPluginBinding) {
        appContext = binding.applicationContext

        methodHandler = MethodChannelHandler(appContext)

        methodChannel = MethodChannel(
            binding.binaryMessenger,
            GATEWAY_CHANNEL,
        )
        methodChannel.setMethodCallHandler(methodHandler)

        eventChannel = EventChannel(
            binding.binaryMessenger,
            EVENTS_CHANNEL,
        )
        eventChannel.setStreamHandler(eventHandler)

        Log.i(TAG, "Channels registered: gateway=$GATEWAY_CHANNEL events=$EVENTS_CHANNEL")
    }

    override fun onDetachedFromEngine(binding: FlutterPlugin.FlutterPluginBinding) {
        methodChannel.setMethodCallHandler(null)
        eventChannel.setStreamHandler(null)
        Log.i(TAG, "Channels detached")
    }

    companion object {
        private const val TAG = "AndroidControlPlugin"
        const val GATEWAY_CHANNEL = "com.example.control/gateway"
        const val EVENTS_CHANNEL = "com.example.control/events"
    }
}
