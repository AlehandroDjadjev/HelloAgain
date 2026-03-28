package com.example.frontend

import android.app.Activity
import android.content.Context
import android.content.Intent
import android.graphics.PixelFormat
import android.graphics.drawable.GradientDrawable
import android.hardware.display.DisplayManager
import android.net.Uri
import android.os.Build
import android.provider.Settings
import android.util.Log
import android.util.TypedValue
import android.view.Gravity
import android.view.View
import android.view.WindowManager
import android.widget.FrameLayout
import android.widget.ImageView
import com.google.android.gms.auth.api.identity.GetPhoneNumberHintIntentRequest
import com.google.android.gms.auth.api.identity.Identity
import androidx.core.content.ContextCompat
import io.flutter.embedding.android.FlutterActivity
import io.flutter.embedding.engine.FlutterEngine
import io.flutter.plugin.common.MethodChannel

class MainActivity : FlutterActivity() {

    companion object {
        private const val TAG = "HelloAgainMainActivity"
        private const val VOICE_SERVICE_CHANNEL = "com.example.frontend/voice_service"
        private const val NAVIGATION_OVERLAY_CHANNEL = "hello_again/navigation_overlay"
        private const val PHONE_HINT_CHANNEL = "hello_again/phone_hint"
        private const val DEEP_LINK_CHANNEL = "hello_again/deep_link"
        private const val PHONE_HINT_REQUEST_CODE = 4842
    }

    private var pendingPhoneHintResult: MethodChannel.Result? = null
    private var pendingDeepLink: String? = null
    private var deepLinkChannel: MethodChannel? = null
    private var overlayWindowManager: WindowManager? = null
    private var overlayWindowContext: Context? = null
    private var overlayView: View? = null

    override fun configureFlutterEngine(flutterEngine: FlutterEngine) {
        super.configureFlutterEngine(flutterEngine)

        deepLinkChannel = MethodChannel(
            flutterEngine.dartExecutor.binaryMessenger,
            DEEP_LINK_CHANNEL,
        ).also { channel ->
            channel.setMethodCallHandler { call, result ->
                when (call.method) {
                    "consumeInitialDeepLink" -> {
                        result.success(pendingDeepLink ?: intent?.dataString)
                        pendingDeepLink = null
                    }
                    else -> result.notImplemented()
                }
            }
        }

        MethodChannel(
            flutterEngine.dartExecutor.binaryMessenger,
            PHONE_HINT_CHANNEL,
        ).setMethodCallHandler { call, result ->
            when (call.method) {
                "requestPhoneNumberHint" -> requestPhoneNumberHint(result)
                else -> result.notImplemented()
            }
        }

        MethodChannel(
            flutterEngine.dartExecutor.binaryMessenger,
            VOICE_SERVICE_CHANNEL,
        ).setMethodCallHandler { call, result ->
            when (call.method) {
                "start" -> {
                    startVoiceService()
                    result.success(null)
                }
                "stop" -> {
                    stopService(Intent(this, VoiceAssistantForegroundService::class.java))
                    result.success(null)
                }
                else -> result.notImplemented()
            }
        }

        MethodChannel(
            flutterEngine.dartExecutor.binaryMessenger,
            NAVIGATION_OVERLAY_CHANNEL,
        ).setMethodCallHandler { call, result ->
            when (call.method) {
                "hasPermission" -> {
                    result.success(Settings.canDrawOverlays(this))
                }
                "requestPermission" -> {
                    try {
                        requestOverlayPermission()
                        result.success(null)
                    } catch (error: Exception) {
                        Log.e(TAG, "Could not open overlay permission screen", error)
                        result.error(
                            "OVERLAY_PERMISSION_REQUEST_FAILED",
                            error.message ?: "Could not open the overlay permission screen.",
                            null,
                        )
                    }
                }
                "show" -> {
                    if (!Settings.canDrawOverlays(this)) {
                        result.error(
                            "OVERLAY_PERMISSION_MISSING",
                            "Display over other apps permission is required for the floating navigator bubble.",
                            null,
                        )
                        return@setMethodCallHandler
                    }
                    val title = call.argument<String>("title")
                    val message = call.argument<String>("message")
                    try {
                        showNavigationOverlay(title, message)
                        result.success(null)
                    } catch (error: Exception) {
                        Log.e(TAG, "Could not start navigation overlay", error)
                        result.error(
                            "OVERLAY_START_FAILED",
                            error.message ?: "Could not start the floating navigator bubble.",
                            null,
                        )
                    }
                }
                "hide" -> {
                    try {
                        hideNavigationOverlay()
                        result.success(null)
                    } catch (error: Exception) {
                        Log.e(TAG, "Could not stop navigation overlay", error)
                        result.error(
                            "OVERLAY_STOP_FAILED",
                            error.message ?: "Could not stop the floating navigator bubble.",
                            null,
                        )
                    }
                }
                "bringToFront" -> {
                    try {
                        bringAppToFront()
                        result.success(null)
                    } catch (error: Exception) {
                        Log.e(TAG, "Could not bring app to front", error)
                        result.error(
                            "BRING_TO_FRONT_FAILED",
                            error.message ?: "Could not bring Hello Again back to the foreground.",
                            null,
                        )
                    }
                }
                else -> result.notImplemented()
            }
        }

        pendingDeepLink = intent?.dataString ?: pendingDeepLink
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        val deepLink = intent.dataString ?: return
        pendingDeepLink = deepLink
        deepLinkChannel?.invokeMethod("onDeepLink", deepLink)
    }

    override fun onDestroy() {
        hideNavigationOverlay()
        super.onDestroy()
    }

    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        super.onActivityResult(requestCode, resultCode, data)

        if (requestCode != PHONE_HINT_REQUEST_CODE) {
            return
        }

        val callback = pendingPhoneHintResult ?: return
        pendingPhoneHintResult = null

        if (resultCode != Activity.RESULT_OK || data == null) {
            callback.success(null)
            return
        }

        try {
            val phoneNumber = Identity.getSignInClient(this).getPhoneNumberFromIntent(data)
            callback.success(phoneNumber)
        } catch (error: Exception) {
            callback.error(
                "PHONE_HINT_PARSE_FAILED",
                error.message ?: "Could not extract phone number from Android hint result.",
                null,
            )
        }
    }

    private fun requestPhoneNumberHint(result: MethodChannel.Result) {
        if (pendingPhoneHintResult != null) {
            result.error(
                "PHONE_HINT_IN_PROGRESS",
                "A phone-number hint request is already running.",
                null,
            )
            return
        }

        val request = GetPhoneNumberHintIntentRequest.builder().build()
        Identity.getSignInClient(this)
            .getPhoneNumberHintIntent(request)
            .addOnSuccessListener { pendingIntent ->
                try {
                    pendingPhoneHintResult = result
                    startIntentSenderForResult(
                        pendingIntent.intentSender,
                        PHONE_HINT_REQUEST_CODE,
                        null,
                        0,
                        0,
                        0,
                        null,
                    )
                } catch (error: Exception) {
                    pendingPhoneHintResult = null
                    result.error(
                        "PHONE_HINT_LAUNCH_FAILED",
                        error.message ?: "Could not open the Android phone-number picker.",
                        null,
                    )
                }
            }
            .addOnFailureListener { error ->
                result.error(
                    "PHONE_HINT_REQUEST_FAILED",
                    error.message ?: "Android could not prepare the phone-number picker.",
                    null,
                )
            }
    }

    private fun startVoiceService() {
        val intent = Intent(this, VoiceAssistantForegroundService::class.java)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            startForegroundService(intent)
        } else {
            startService(intent)
        }
    }

    private fun requestOverlayPermission() {
        val intent = Intent(
            Settings.ACTION_MANAGE_OVERLAY_PERMISSION,
            Uri.parse("package:$packageName"),
        ).apply {
            addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        }
        startActivity(intent)
    }

    private fun showNavigationOverlay(title: String?, message: String?) {
        val activeWindowManager = resolveNavigationOverlayWindowManager()
        if (overlayView != null) {
            return
        }

        val overlayUiContext = overlayWindowContext ?: this
        val container = FrameLayout(overlayUiContext).apply {
            layoutParams = FrameLayout.LayoutParams(dp(64), dp(64))
            background = GradientDrawable().apply {
                shape = GradientDrawable.OVAL
                setColor(0xF4FFF7E8.toInt())
                setStroke(dp(2), 0x22000000)
            }
            elevation = dp(12).toFloat()
        }

        val iconHolder = FrameLayout(overlayUiContext).apply {
            layoutParams = FrameLayout.LayoutParams(dp(52), dp(52), Gravity.CENTER)
            background = GradientDrawable().apply {
                shape = GradientDrawable.OVAL
                setColor(0xFFE2F0E7.toInt())
            }
        }
        val icon = ImageView(overlayUiContext).apply {
            layoutParams = FrameLayout.LayoutParams(dp(28), dp(28), Gravity.CENTER)
            setImageResource(R.mipmap.ic_launcher)
            contentDescription = "Hello Again navigator"
        }
        iconHolder.addView(icon)

        val pulseDot = View(overlayUiContext).apply {
            layoutParams = FrameLayout.LayoutParams(dp(14), dp(14), Gravity.TOP or Gravity.END).apply {
                topMargin = dp(8)
                marginEnd = dp(8)
            }
            background = GradientDrawable().apply {
                shape = GradientDrawable.OVAL
                setColor(ContextCompat.getColor(overlayUiContext, android.R.color.holo_green_light))
            }
        }

        container.addView(iconHolder)
        container.addView(pulseDot)
        container.alpha = 0.98f

        val layoutParams = WindowManager.LayoutParams(
            dp(64),
            dp(64),
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY
            } else {
                @Suppress("DEPRECATION")
                WindowManager.LayoutParams.TYPE_PHONE
            },
            WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE or
                WindowManager.LayoutParams.FLAG_NOT_TOUCHABLE or
                WindowManager.LayoutParams.FLAG_LAYOUT_IN_SCREEN,
            PixelFormat.TRANSLUCENT,
        ).apply {
            gravity = Gravity.END or Gravity.CENTER_VERTICAL
            x = dp(18)
            y = dp(48)
        }

        activeWindowManager.addView(container, layoutParams)
        overlayView = container
        Log.i(TAG, "Navigation overlay bubble attached")
    }

    private fun hideNavigationOverlay() {
        val view = overlayView ?: return
        try {
            overlayWindowManager?.removeView(view)
        } catch (error: Exception) {
            Log.w(TAG, "Could not remove navigation overlay", error)
        } finally {
            overlayView = null
        }
    }

    private fun bringAppToFront() {
        val launchIntent = packageManager.getLaunchIntentForPackage(packageName)
            ?: Intent(this, MainActivity::class.java)

        launchIntent.addFlags(
            Intent.FLAG_ACTIVITY_NEW_TASK or
                Intent.FLAG_ACTIVITY_SINGLE_TOP or
                Intent.FLAG_ACTIVITY_CLEAR_TOP or
                Intent.FLAG_ACTIVITY_REORDER_TO_FRONT,
        )
        startActivity(launchIntent)
    }

    private fun resolveNavigationOverlayWindowManager(): WindowManager {
        overlayWindowManager?.let { return it }

        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
            val displayManager = getSystemService(DisplayManager::class.java)
            val primaryDisplay = displayManager?.getDisplay(android.view.Display.DEFAULT_DISPLAY)
            if (primaryDisplay != null) {
                runCatching {
                    createDisplayContext(primaryDisplay).createWindowContext(
                        WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY,
                        null,
                    )
                }.onSuccess { createdContext ->
                    val createdWindowManager =
                        createdContext.getSystemService(WindowManager::class.java)
                    if (createdWindowManager != null) {
                        overlayWindowContext = createdContext
                        overlayWindowManager = createdWindowManager
                        Log.i(TAG, "Using dedicated overlay window context")
                        return createdWindowManager
                    }
                }.onFailure { error ->
                    Log.w(TAG, "Could not create navigation overlay window context", error)
                }
            }
        }

        @Suppress("DEPRECATION")
        val fallback = getSystemService(WINDOW_SERVICE) as WindowManager
        overlayWindowContext = this
        overlayWindowManager = fallback
        Log.i(TAG, "Using fallback WindowManager for navigation overlay")
        return fallback
    }

    private fun dp(value: Int): Int =
        TypedValue.applyDimension(
            TypedValue.COMPLEX_UNIT_DIP,
            value.toFloat(),
            resources.displayMetrics,
        ).toInt()
}
