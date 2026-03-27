package com.example.control.bridge

import android.content.Context
import android.content.Intent

object LaunchableAppsHelper {
    fun listLaunchablePackages(context: Context): List<String> {
        val packageManager = context.packageManager
        val launcherIntent = Intent(Intent.ACTION_MAIN).apply {
            addCategory(Intent.CATEGORY_LAUNCHER)
        }

        return packageManager.queryIntentActivities(launcherIntent, 0)
            .asSequence()
            .mapNotNull { it.activityInfo?.packageName }
            .filter { it.isNotBlank() }
            .filter { it != context.packageName }
            .distinct()
            .sorted()
            .toList()
    }
}
