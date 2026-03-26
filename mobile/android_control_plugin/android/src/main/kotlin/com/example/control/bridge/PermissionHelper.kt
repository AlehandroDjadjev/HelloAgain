package com.example.control.bridge

import android.content.Context
import android.content.Intent
import android.provider.Settings
import android.text.TextUtils
import android.view.accessibility.AccessibilityManager

/**
 * Checks Android permission state and navigates to settings.
 * All methods are safe to call before the AccessibilityService is running.
 */
object PermissionHelper {

    fun getPermissionStatus(context: Context): Map<String, Any> = mapOf(
        "accessibilityService" to isAccessibilityServiceEnabled(context),
        "overlayPermission" to Settings.canDrawOverlays(context),
    )

    fun isAccessibilityServiceEnabled(context: Context): Boolean {
        val serviceName = "${context.packageName}/" +
            "com.example.control.service.AutomationAccessibilityService"
        val am = context.getSystemService(Context.ACCESSIBILITY_SERVICE)
            as AccessibilityManager
        if (!am.isEnabled) return false

        val enabledServices = Settings.Secure.getString(
            context.contentResolver,
            Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES,
        ) ?: return false

        return TextUtils.SimpleStringSplitter(':').apply {
            setString(enabledServices)
        }.any { it.equals(serviceName, ignoreCase = true) }
    }

    fun openAccessibilitySettings(context: Context) {
        val intent = Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS).apply {
            flags = Intent.FLAG_ACTIVITY_NEW_TASK
        }
        context.startActivity(intent)
    }
}
