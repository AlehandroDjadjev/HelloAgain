package com.example.frontend

import android.app.Activity
import android.content.Intent
import com.google.android.gms.auth.api.identity.GetPhoneNumberHintIntentRequest
import com.google.android.gms.auth.api.identity.Identity
import io.flutter.embedding.android.FlutterActivity
import io.flutter.embedding.engine.FlutterEngine
import io.flutter.plugin.common.MethodChannel

class MainActivity : FlutterActivity() {
    private val phoneHintChannel = "hello_again/phone_hint"
    private val phoneHintRequestCode = 40421
    private var pendingPhoneHintResult: MethodChannel.Result? = null

    override fun configureFlutterEngine(flutterEngine: FlutterEngine) {
        super.configureFlutterEngine(flutterEngine)

        MethodChannel(
            flutterEngine.dartExecutor.binaryMessenger,
            phoneHintChannel,
        ).setMethodCallHandler { call, result ->
            when (call.method) {
                "requestPhoneNumberHint" -> requestPhoneNumberHint(result)
                else -> result.notImplemented()
            }
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
                        phoneHintRequestCode,
                        null,
                        0,
                        0,
                        0,
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

    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        super.onActivityResult(requestCode, resultCode, data)

        if (requestCode != phoneHintRequestCode) {
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
}
