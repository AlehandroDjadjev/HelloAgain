package com.example.control.service

import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Intent
import android.os.Build
import android.os.IBinder
import android.util.Log
import androidx.core.app.NotificationCompat

/**
 * Keeps a user-visible persistent notification active during any automation session.
 *
 * Design rule: The user must always be able to see that automation is active and
 * must be able to stop it. This service fulfils that contract.
 *
 * Lifecycle:
 *  - Started by the Flutter bridge when a session begins.
 *  - Stopped when the session ends (stopSession) or the user taps the stop action.
 *  - foregroundServiceType = specialUse (declared in AndroidManifest).
 */
class AutomationForegroundService : Service() {

    companion object {
        private const val TAG = "AutomationFgService"
        private const val NOTIFICATION_ID = 9001
        private const val CHANNEL_ID = "helloagain_automation"
        private const val CHANNEL_NAME = "Automation Sessions"

        const val ACTION_STOP = "com.example.control.ACTION_STOP_SESSION"
        const val EXTRA_SESSION_ID = "session_id"
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        createNotificationChannel()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_STOP -> {
                val sessionId = intent.getStringExtra(EXTRA_SESSION_ID) ?: ""
                Log.i(TAG, "User stopped session via notification: $sessionId")
                AutomationAccessibilityService.instance?.stopSession(sessionId)
                stopSelf()
                return START_NOT_STICKY
            }
        }

        val sessionId = intent?.getStringExtra(EXTRA_SESSION_ID) ?: "unknown"
        Log.i(TAG, "Foreground service starting for session: $sessionId")

        val stopIntent = Intent(this, AutomationForegroundService::class.java).apply {
            action = ACTION_STOP
            putExtra(EXTRA_SESSION_ID, sessionId)
        }
        val stopPendingIntent = PendingIntent.getService(
            this,
            0,
            stopIntent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )

        val notification = NotificationCompat.Builder(this, CHANNEL_ID)
            .setContentTitle("Automation Active")
            .setContentText("HelloAgain is running a task. Tap Stop to cancel.")
            .setSmallIcon(android.R.drawable.ic_dialog_info)
            .setOngoing(true)
            .setPriority(NotificationCompat.PRIORITY_LOW)
            .addAction(
                android.R.drawable.ic_delete,
                "Stop",
                stopPendingIntent,
            )
            .build()

        startForeground(NOTIFICATION_ID, notification)
        return START_STICKY
    }

    override fun onDestroy() {
        super.onDestroy()
        Log.i(TAG, "Foreground service destroyed")
    }

    private fun createNotificationChannel() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val channel = NotificationChannel(
                CHANNEL_ID,
                CHANNEL_NAME,
                NotificationManager.IMPORTANCE_LOW,
            ).apply {
                description = "Active automation session indicator"
                setShowBadge(false)
            }
            val nm = getSystemService(NotificationManager::class.java)
            nm.createNotificationChannel(channel)
        }
    }
}
