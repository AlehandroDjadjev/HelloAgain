package com.example.control.bridge

import android.content.Context
import android.util.Log
import com.example.control.model.SelectorDto
import com.example.control.model.SessionConfigDto
import com.example.control.service.AutomationAccessibilityService
import io.flutter.plugin.common.MethodCall
import io.flutter.plugin.common.MethodChannel
import java.util.concurrent.Executors

/**
 * Routes MethodChannel calls to [AutomationAccessibilityService] via the
 * [DeviceControlGateway] interface.
 *
 * Threading: all gateway calls are dispatched on a single-thread executor so
 * they never block the Flutter platform thread. Results are posted back on the
 * platform thread via [MethodChannel.Result].
 *
 * Service unavailability: if [AutomationAccessibilityService.instance] is null,
 * permission-checking methods still work (they use [appContext]); all action
 * methods return "SERVICE_NOT_ENABLED".
 */
class MethodChannelHandler(private val appContext: Context) :
    MethodChannel.MethodCallHandler {

    private val executor = Executors.newSingleThreadExecutor()

    override fun onMethodCall(call: MethodCall, result: MethodChannel.Result) {
        // Permission and settings calls do not require the service to be running
        when (call.method) {
            "getPermissionStatus" -> {
                result.success(PermissionHelper.getPermissionStatus(appContext))
                return
            }
            "listLaunchablePackages" -> {
                result.success(LaunchableAppsHelper.listLaunchablePackages(appContext))
                return
            }
            "openAccessibilitySettings" -> {
                PermissionHelper.openAccessibilitySettings(appContext)
                result.success(null)
                return
            }
        }

        // All other calls require the running AccessibilityService
        val service = AutomationAccessibilityService.instance
        if (service == null) {
            result.error(
                "SERVICE_NOT_ENABLED",
                "AccessibilityService is not running. " +
                    "Enable it in Settings → Accessibility.",
                null,
            )
            return
        }

        // Dispatch to background thread; post result back on platform thread
        executor.submit {
            try {
                val value = dispatch(call, service)
                result.success(value)
            } catch (e: Exception) {
                Log.e(TAG, "Error handling ${call.method}", e)
                result.error("UNEXPECTED_ERROR", e.message, null)
            }
        }
    }

    @Suppress("UNCHECKED_CAST")
    private fun dispatch(
        call: MethodCall,
        service: AutomationAccessibilityService,
    ): Any? {
        return when (call.method) {
            "startSession" -> {
                val config = SessionConfigDto.fromMap(
                    call.arguments as Map<String, Any?>
                )
                service.startSession(config).toMap()
            }
            "stopSession" -> {
                val sessionId = call.argument<String>("sessionId") ?: ""
                service.stopSession(sessionId).toMap()
            }
            "isPackageInstalled" -> {
                val pkg = call.argument<String>("packageName") ?: return false
                service.isPackageInstalled(pkg)
            }
            "launchApp" -> {
                val pkg = call.argument<String>("packageName") ?: ""
                service.launchApp(pkg).toMap()
            }
            "getScreenState" -> service.getScreenState().toMap()
            "findElement" -> {
                val selector = SelectorDto.fromMap(
                    call.arguments as Map<String, Any?>
                )
                service.findElement(selector)?.toMap()
            }
            "findElements" -> {
                val selector = SelectorDto.fromMap(
                    call.arguments as Map<String, Any?>
                )
                service.findElements(selector).map { it.toMap() }
            }
            "tapElement" -> {
                val selector = SelectorDto.fromMap(
                    call.arguments as Map<String, Any?>
                )
                service.tapElement(selector).toMap()
            }
            "longPressElement" -> {
                val selector = SelectorDto.fromMap(
                    call.arguments as Map<String, Any?>
                )
                service.longPressElement(selector).toMap()
            }
            "focusElement" -> {
                val selector = SelectorDto.fromMap(
                    call.arguments as Map<String, Any?>
                )
                service.focusElement(selector).toMap()
            }
            "typeText" -> {
                val text = call.argument<String>("text") ?: ""
                service.typeText(text).toMap()
            }
            "clearFocusedField" -> service.clearFocusedField().toMap()
            "scroll" -> {
                val direction = call.argument<String>("direction") ?: "down"
                service.scroll(direction).toMap()
            }
            "swipe" -> {
                val startX = call.argument<Int>("startX") ?: 0
                val startY = call.argument<Int>("startY") ?: 0
                val endX = call.argument<Int>("endX") ?: 0
                val endY = call.argument<Int>("endY") ?: 0
                val durationMs = (call.argument<Number>("durationMs") ?: 300L).toLong()
                service.swipe(startX, startY, endX, endY, durationMs).toMap()
            }
            "goBack" -> service.goBack().toMap()
            "goHome" -> service.goHome().toMap()
            "takeScreenshot" -> service.takeScreenshot()
            "getLastScreenshotError" -> service.getLastScreenshotError()
            else -> null.also { Log.w(TAG, "Unhandled method: ${call.method}") }
        }
    }

    companion object {
        private const val TAG = "MethodChannelHandler"
    }
}
