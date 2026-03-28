package com.example.frontend

import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.pm.ServiceInfo
import android.content.Context
import android.content.Intent
import android.graphics.PixelFormat
import android.hardware.display.DisplayManager
import android.graphics.drawable.GradientDrawable
import android.os.Build
import android.os.IBinder
import android.provider.Settings
import android.util.TypedValue
import android.util.Log
import android.view.Gravity
import android.view.View
import android.view.WindowManager
import android.widget.FrameLayout
import android.widget.ImageView
import androidx.core.content.ContextCompat
import androidx.core.app.NotificationCompat

class NavigationOverlayService : Service() {

    companion object {
        private const val TAG = "HelloAgainOverlay"
        const val ACTION_SHOW = "hello_again.navigation_overlay.SHOW"
        const val ACTION_HIDE = "hello_again.navigation_overlay.HIDE"
        const val EXTRA_TITLE = "title"
        const val EXTRA_MESSAGE = "message"

        private const val CHANNEL_ID = "helloagain_navigation_overlay"
        private const val CHANNEL_NAME = "Navigation status"
        private const val NOTIFICATION_ID = 9102
    }

    private var windowManager: WindowManager? = null
    private var windowContext: Context? = null
    private var overlayView: View? = null

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        try {
            resolveOverlayWindowContext()
        } catch (error: Exception) {
            Log.e(TAG, "Failed to resolve overlay window context", error)
            windowContext = this
            @Suppress("DEPRECATION")
            windowManager = getSystemService(WINDOW_SERVICE) as WindowManager
        }
        createNotificationChannel()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        return try {
            when (intent?.action) {
                ACTION_HIDE -> {
                    hideOverlay()
                    stopForegroundAndSelf()
                    return START_NOT_STICKY
                }
                ACTION_SHOW, null -> {
                    val title = intent?.getStringExtra(EXTRA_TITLE)?.trim().orEmpty()
                    val message = intent?.getStringExtra(EXTRA_MESSAGE)?.trim().orEmpty()
                    if (!Settings.canDrawOverlays(this)) {
                        stopForegroundAndSelf()
                        return START_NOT_STICKY
                    }
                    startOverlayForeground(title, message)
                    showOrUpdateOverlay(title, message)
                    START_STICKY
                }
                else -> START_NOT_STICKY
            }
        } catch (error: Exception) {
            Log.e(TAG, "Overlay service failed to start", error)
            hideOverlay()
            stopForegroundAndSelf()
            START_NOT_STICKY
        }
    }

    override fun onDestroy() {
        try {
            hideOverlay()
        } catch (error: Exception) {
            Log.e(TAG, "Overlay cleanup failed", error)
        }
        super.onDestroy()
    }

    private fun startOverlayForeground(title: String, message: String) {
        val openAppIntent =
            packageManager.getLaunchIntentForPackage(packageName)
                ?: Intent(this, MainActivity::class.java)
        val openAppPendingIntent = PendingIntent.getActivity(
            this,
            0,
            openAppIntent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )

        val contentTitle = if (title.isNotEmpty()) title else "Navigator active"
        val contentText = if (message.isNotEmpty()) {
            message
        } else {
            "Hello Again is still working through the phone."
        }

        val notification = NotificationCompat.Builder(this, CHANNEL_ID)
            .setContentTitle(contentTitle)
            .setContentText(contentText)
            .setSmallIcon(android.R.drawable.ic_dialog_map)
            .setContentIntent(openAppPendingIntent)
            .setOngoing(true)
            .setPriority(NotificationCompat.PRIORITY_LOW)
            .build()

        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            startForeground(
                NOTIFICATION_ID,
                notification,
                ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE,
            )
        } else {
            startForeground(NOTIFICATION_ID, notification)
        }
    }

    private fun showOrUpdateOverlay(title: String, message: String) {
        ensureOverlayView()
    }

    private fun ensureOverlayView() {
        if (overlayView != null) {
            return
        }

        val activeWindowManager = windowManager ?: run {
            Log.e(TAG, "No WindowManager available for floating bubble")
            throw IllegalStateException("No WindowManager available for overlay bubble.")
        }

        val overlayUiContext = windowContext ?: this

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

        try {
            activeWindowManager.addView(container, layoutParams)
        } catch (error: Exception) {
            Log.e(TAG, "Could not attach floating bubble view", error)
            throw error
        }
        overlayView = container
    }

    private fun hideOverlay() {
        val view = overlayView ?: return
        try {
            windowManager?.removeView(view)
        } catch (_: Exception) {
        } finally {
            overlayView = null
        }
    }

    private fun stopForegroundAndSelf() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.N) {
            stopForeground(STOP_FOREGROUND_REMOVE)
        } else {
            @Suppress("DEPRECATION")
            stopForeground(true)
        }
        stopSelf()
    }

    private fun createNotificationChannel() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val channel = NotificationChannel(
                CHANNEL_ID,
                CHANNEL_NAME,
                NotificationManager.IMPORTANCE_LOW,
            ).apply {
                description = "Shows when Hello Again is navigating through the phone."
                setShowBadge(false)
            }
            val notificationManager = getSystemService(NotificationManager::class.java)
            notificationManager.createNotificationChannel(channel)
        }
    }

    private fun resolveOverlayWindowContext() {
        val fallbackWindowManager = runCatching {
            @Suppress("DEPRECATION")
            getSystemService(WINDOW_SERVICE) as WindowManager
        }.getOrNull()

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
                        windowContext = createdContext
                        windowManager = createdWindowManager
                        Log.i(TAG, "Using dedicated overlay window context")
                        return
                    }
                    Log.w(TAG, "Overlay window context returned null WindowManager; falling back")
                }.onFailure { error ->
                    Log.w(TAG, "Could not create overlay window context; falling back", error)
                }
            }
        }

        windowContext = this
        windowManager = fallbackWindowManager
        if (windowManager == null) {
            Log.e(TAG, "Fallback WindowManager is unavailable")
        } else {
            Log.i(TAG, "Using fallback service WindowManager for overlay")
        }
    }

    private fun dp(value: Int): Int =
        TypedValue.applyDimension(
            TypedValue.COMPLEX_UNIT_DIP,
            value.toFloat(),
            resources.displayMetrics,
        ).toInt()
}
