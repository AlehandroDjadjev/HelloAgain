package com.example.frontend

import android.app.Activity
import android.content.Intent
import android.os.Build
import com.google.android.gms.auth.api.identity.GetPhoneNumberHintIntentRequest
import com.google.android.gms.auth.api.identity.Identity
import io.flutter.embedding.android.FlutterActivity
import io.flutter.embedding.engine.FlutterEngine
import io.flutter.plugin.common.MethodChannel

class MainActivity : FlutterActivity() {

    companion object {
        private const val VOICE_SERVICE_CHANNEL = "com.example.frontend/voice_service"
        private const val PHONE_HINT_CHANNEL = "hello_again/phone_hint"
        private const val DEEP_LINK_CHANNEL = "hello_again/deep_link"
        private const val PHONE_HINT_REQUEST_CODE = 4842
    }

    private var pendingPhoneHintResult: MethodChannel.Result? = null
    private var pendingDeepLink: String? = null
    private var deepLinkChannel: MethodChannel? = null

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

        pendingDeepLink = intent?.dataString ?: pendingDeepLink
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        val deepLink = intent.dataString ?: return
        pendingDeepLink = deepLink
        deepLinkChannel?.invokeMethod("onDeepLink", deepLink)
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
}
