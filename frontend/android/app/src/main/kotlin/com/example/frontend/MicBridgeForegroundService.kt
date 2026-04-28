package com.example.frontend

import android.Manifest
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Intent
import android.content.pm.PackageManager
import android.content.pm.ServiceInfo
import android.media.AudioAttributes
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.MediaPlayer
import android.media.MediaRecorder
import android.os.Build
import android.os.IBinder
import android.util.Base64
import android.util.Log
import androidx.core.app.NotificationCompat
import androidx.core.content.ContextCompat
import org.json.JSONObject
import java.io.BufferedInputStream
import java.io.BufferedOutputStream
import java.io.ByteArrayOutputStream
import java.io.DataOutputStream
import java.io.File
import java.io.IOException
import java.net.HttpURLConnection
import java.net.URL
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.util.Locale
import java.util.concurrent.CountDownLatch
import java.util.concurrent.CancellationException
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import kotlin.collections.ArrayDeque
import kotlin.math.max
import kotlin.math.sqrt

class MicBridgeForegroundService : Service() {

    companion object {
        private const val TAG = "HelloAgainMicBridge"
        private const val CHANNEL_ID = "helloagain_mic_bridge"
        private const val CHANNEL_NAME = "Hands-free clarification microphone"
        private const val NOTIFICATION_ID = 9102
        private const val REQUEST_CODE_OPEN_APP = 9102
        private const val REQUEST_CODE_STOP = 9103
        private const val REQUEST_CODE_PREPARE = 9104

        const val ACTION_START_SERVICE = "com.example.frontend.mic_bridge.START"
        const val ACTION_STOP_SERVICE = "com.example.frontend.mic_bridge.STOP"
        const val ACTION_BEGIN_ONE_SHOT_QUERY = "com.example.frontend.mic_bridge.BEGIN_ONE_SHOT_QUERY"
        const val ACTION_CANCEL_LISTENING = "com.example.frontend.mic_bridge.CANCEL_LISTENING"

        const val EXTRA_REQUEST_ID = "request_id"
        const val EXTRA_QUESTION = "question"
        const val EXTRA_BASE_URL = "base_url"
        const val EXTRA_USER_ID = "user_id"
        const val EXTRA_SESSION_ID = "session_id"
        const val EXTRA_LANGUAGE = "language"
        const val EXTRA_TIMEOUT_MS = "timeout_ms"

        private const val SAMPLE_RATE = 16000
        private const val CHANNEL_COUNT = 1
        private const val SILENCE_WINDOW_MS = 900L
        private const val MAX_TURN_MS = 10000L
        private const val MIN_TURN_MS = 450L
        private const val PRE_SPEECH_CHUNK_LIMIT = 8
        private const val SPEECH_THRESHOLD = 0.015
    }

    private enum class BridgeState {
        ARMED_IDLE,
        SPEAKING_PROMPT,
        ACTIVE_LISTENING,
    }

    private data class QueryCaptureResult(
        val transcript: String,
        val wavBytes: ByteArray,
    )

    private class MicPermissionLostException(message: String) : IllegalStateException(message)

    private val executor = Executors.newSingleThreadExecutor()
    private val resourceLock = Any()

    @Volatile
    private var bridgeState = BridgeState.ARMED_IDLE

    @Volatile
    private var activeRequestId: String? = null

    @Volatile
    private var cancelRequested = false

    @Volatile
    private var currentRecorder: AudioRecord? = null

    @Volatile
    private var currentPlayer: MediaPlayer? = null

    @Volatile
    private var foregroundReady = false

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        createNotificationChannel()
        Log.i(TAG, "Mic bridge service started")
    }

    override fun onDestroy() {
        cancelRequested = true
        releaseCurrentRecorder()
        releaseCurrentPlayer()
        executor.shutdownNow()
        foregroundReady = false
        MicBridgePlugin.setServiceRunning(false)
        Log.i(TAG, "Mic bridge service stopped")
        super.onDestroy()
    }

    override fun onTaskRemoved(rootIntent: Intent?) {
        Log.i(TAG, "Mic bridge task removed; keeping sticky foreground service armed")
        super.onTaskRemoved(rootIntent)
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        ensureForeground(notificationMessageForCurrentState())
        val action = intent?.action ?: ACTION_START_SERVICE
        Log.i(TAG, "Mic bridge onStartCommand action=$action startId=$startId")

        when (action) {
            ACTION_START_SERVICE -> {
                if (!ensurePermissionOrRecover()) {
                    return START_STICKY
                }
                bridgeState = BridgeState.ARMED_IDLE
                updateNotification(notificationMessageForCurrentState())
                MicBridgePlugin.setServiceRunning(true)
                Log.i(TAG, "Mic bridge service started in armed-idle mode")
            }

            ACTION_STOP_SERVICE -> {
                cancelActiveRequest(
                    requestId = intent?.getStringExtra(EXTRA_REQUEST_ID),
                    code = "mic_bridge_stopped",
                    message = "Mic bridge service stopped.",
                )
                stopForeground(STOP_FOREGROUND_REMOVE)
                stopSelf()
            }

            ACTION_CANCEL_LISTENING -> {
                cancelActiveRequest(
                    requestId = intent?.getStringExtra(EXTRA_REQUEST_ID),
                    code = "listening_cancelled",
                    message = "Listening was cancelled.",
                )
                bridgeState = BridgeState.ARMED_IDLE
                updateNotification(notificationMessageForCurrentState())
            }

            ACTION_BEGIN_ONE_SHOT_QUERY -> {
                if (!ensurePermissionOrRecover()) {
                    return START_STICKY
                }
                val queryIntent = intent
                if (queryIntent == null) {
                    MicBridgePlugin.emitError(
                        null,
                        "invalid_query",
                        "Mic bridge query intent was missing.",
                    )
                    return START_STICKY
                }
                beginOneShotQuery(queryIntent)
            }
        }

        return START_STICKY
    }

    private fun beginOneShotQuery(intent: Intent) {
        val requestId = intent.getStringExtra(EXTRA_REQUEST_ID).orEmpty()
        val question = intent.getStringExtra(EXTRA_QUESTION).orEmpty()
        val baseUrl = intent.getStringExtra(EXTRA_BASE_URL).orEmpty()
        val userId = intent.getStringExtra(EXTRA_USER_ID).orEmpty()
        val sessionId = intent.getStringExtra(EXTRA_SESSION_ID).orEmpty()
        val language = intent.getStringExtra(EXTRA_LANGUAGE).orEmpty()
        val timeoutMs = intent.getLongExtra(EXTRA_TIMEOUT_MS, 12000L)

        if (requestId.isBlank() || question.isBlank() || baseUrl.isBlank()) {
            MicBridgePlugin.emitError(
                requestId.ifBlank { null },
                "invalid_query",
                "Mic bridge query is missing required fields.",
            )
            return
        }
        if (!foregroundReady) {
            MicBridgePlugin.emitError(
                requestId,
                "service_not_foreground",
                "Mic bridge is not ready yet. Try again after the service notification appears.",
            )
            return
        }
        if (activeRequestId != null) {
            MicBridgePlugin.emitError(
                requestId,
                "query_in_progress",
                "Another microphone bridge clarification request is already running.",
            )
            return
        }

        activeRequestId = requestId
        cancelRequested = false
        executor.execute {
            try {
                runOneShotQuery(
                    requestId = requestId,
                    question = question,
                    baseUrl = baseUrl,
                    userId = userId,
                    sessionId = sessionId,
                    language = language,
                    timeoutMs = timeoutMs,
                )
            } catch (cancelled: CancellationException) {
                Log.i(TAG, "Mic bridge query cancelled request=$requestId")
            } catch (permissionFailure: MicPermissionLostException) {
                Log.w(TAG, "Mic bridge query stopped due to permission loss request=$requestId")
            } catch (error: Exception) {
                Log.e(TAG, "Mic bridge query failed request=$requestId", error)
                MicBridgePlugin.emitError(
                    requestId,
                    "query_failed",
                    error.message ?: "Mic bridge query failed.",
                )
                bridgeState = BridgeState.ARMED_IDLE
                updateNotification("Mic bridge query failed. Ready for another clarification.")
            } finally {
                activeRequestId = null
                cancelRequested = false
                bridgeState = BridgeState.ARMED_IDLE
                updateNotification(notificationMessageForCurrentState())
            }
        }
    }

    private fun runOneShotQuery(
        requestId: String,
        question: String,
        baseUrl: String,
        userId: String,
        sessionId: String,
        language: String,
        timeoutMs: Long,
    ) {
        requireForegroundReadyForCapture(requestId)
        Log.i(TAG, "Mic bridge listening started request=$requestId")
        checkCancelled()
        bridgeState = BridgeState.SPEAKING_PROMPT
        updateNotification(notificationMessageForCurrentState())
        MicBridgePlugin.emitTtsStarted(requestId, question)
        val speech = requestSpeech(baseUrl, question, userId, sessionId)
        checkCancelled()
        playAudio(speech.first, speech.second)
        checkCancelled()
        MicBridgePlugin.emitTtsFinished(requestId)

        bridgeState = BridgeState.ACTIVE_LISTENING
        updateNotification(notificationMessageForCurrentState())
        MicBridgePlugin.emitListeningStarted(requestId, timeoutMs)
        val captureResult = recordAndTranscribe(
            baseUrl = baseUrl,
            userId = userId,
            sessionId = sessionId,
            language = language,
            timeoutMs = timeoutMs,
        )
        checkCancelled()

        if (captureResult == null || captureResult.transcript.isBlank()) {
            Log.i(TAG, "Mic bridge listening ended without transcript request=$requestId")
            MicBridgePlugin.emitError(
                requestId,
                "no_transcript",
                "No clarification reply was detected.",
            )
            return
        }

        Log.i(TAG, "Mic bridge transcript emitted request=$requestId")
        MicBridgePlugin.emitPartialTranscript(requestId, captureResult.transcript)
        MicBridgePlugin.emitFinalTranscript(
            requestId = requestId,
            transcript = captureResult.transcript,
            audioBase64 = Base64.encodeToString(captureResult.wavBytes, Base64.NO_WRAP),
            audioMimeType = "audio/wav",
        )
        updateNotification("Clarification reply captured.")
    }

    private fun requestSpeech(
        baseUrl: String,
        text: String,
        userId: String,
        sessionId: String,
    ): Pair<ByteArray, String> {
        val payload = JSONObject()
            .put("user_id", userId)
            .put("session_id", sessionId)
            .put("text", text)
            .put("response_format", "audio")
            .toString()
            .toByteArray(Charsets.UTF_8)
        val connection = openJsonConnection("${baseUrl.trimEnd('/')}/api/voice/speak/")
        connection.setRequestProperty("Accept", "audio/*")
        connection.doOutput = true
        connection.outputStream.use { output ->
            output.write(payload)
        }

        val code = connection.responseCode
        val contentType = connection.contentType?.lowercase(Locale.US).orEmpty()
        val responseBytes = readAllBytes(
            if (code in 200..299) connection.inputStream else connection.errorStream,
        )
        if (code !in 200..299) {
            throw IOException("Voice speak request failed with status $code.")
        }
        if (contentType.contains("audio/")) {
            return responseBytes to contentType
        }

        val data = JSONObject(responseBytes.toString(Charsets.UTF_8))
        val audioBase64 = data.optString("audio_base64")
        if (audioBase64.isBlank()) {
            throw IOException("Voice speak request returned no audio.")
        }
        return Base64.decode(audioBase64, Base64.DEFAULT) to
            data.optString("audio_mime_type", "audio/wav")
    }

    private fun recordAndTranscribe(
        baseUrl: String,
        userId: String,
        sessionId: String,
        language: String,
        timeoutMs: Long,
    ): QueryCaptureResult? {
        if (!hasRecordAudioPermission()) {
            handlePermissionFailure(activeRequestId, "Microphone permission was revoked before listening could start.")
            throw MicPermissionLostException("Microphone permission missing before listening.")
        }
        val minBufferSize = AudioRecord.getMinBufferSize(
            SAMPLE_RATE,
            AudioFormat.CHANNEL_IN_MONO,
            AudioFormat.ENCODING_PCM_16BIT,
        )
        val bufferSize = if (minBufferSize > 0) max(minBufferSize, 4096) else 4096
        val recorder = AudioRecord(
            MediaRecorder.AudioSource.VOICE_RECOGNITION,
            SAMPLE_RATE,
            AudioFormat.CHANNEL_IN_MONO,
            AudioFormat.ENCODING_PCM_16BIT,
            bufferSize,
        )
        setCurrentRecorder(recorder)

        val buffer = ByteArray(bufferSize)
        val preSpeechChunks = ArrayDeque<ByteArray>()
        val turnChunks = mutableListOf<ByteArray>()
        var speechDetected = false
        var speechStartedAt = 0L
        var lastVoiceAt = 0L
        val requestStartedAt = System.currentTimeMillis()

        try {
            if (recorder.state != AudioRecord.STATE_INITIALIZED) {
                throw IOException("AudioRecord could not be initialized.")
            }
            recorder.startRecording()
            Log.i(TAG, "Mic bridge mic opened")
            while (System.currentTimeMillis() - requestStartedAt < timeoutMs) {
                checkCancelled()
                if (!hasRecordAudioPermission()) {
                    handlePermissionFailure(activeRequestId, "Microphone permission was revoked during listening.")
                    throw MicPermissionLostException("Microphone permission missing during listening.")
                }
                val read = recorder.read(buffer, 0, buffer.size)
                if (read <= 0) {
                    continue
                }

                val chunk = buffer.copyOf(read)
                val level = pcmLevel(chunk)
                val now = System.currentTimeMillis()

                if (speechDetected) {
                    turnChunks.add(chunk)
                    if (level >= SPEECH_THRESHOLD) {
                        lastVoiceAt = now
                    }
                    val speechElapsed = now - speechStartedAt
                    val silenceElapsed = now - lastVoiceAt
                    if (speechElapsed >= MAX_TURN_MS || silenceElapsed >= SILENCE_WINDOW_MS) {
                        break
                    }
                    continue
                }

                preSpeechChunks.addLast(chunk)
                while (preSpeechChunks.size > PRE_SPEECH_CHUNK_LIMIT) {
                    preSpeechChunks.removeFirst()
                }

                if (level >= SPEECH_THRESHOLD) {
                    speechDetected = true
                    speechStartedAt = now
                    lastVoiceAt = now
                    turnChunks.addAll(preSpeechChunks)
                    turnChunks.add(chunk)
                    preSpeechChunks.clear()
                    Log.i(TAG, "Mic bridge speech detected request=$activeRequestId level=$level")
                }
            }
        } finally {
            clearCurrentRecorder(recorder)
        }
        Log.i(TAG, "Mic bridge listening ended request=$activeRequestId speechDetected=$speechDetected")

        val effectiveStartAt = if (speechDetected) speechStartedAt else requestStartedAt
        if (System.currentTimeMillis() - effectiveStartAt < MIN_TURN_MS) {
            return null
        }

        val pcmBytes = joinChunks(turnChunks)
        if (pcmBytes.isEmpty()) {
            return null
        }
        val wavBytes = wrapPcmAsWav(pcmBytes)
        val transcript = requestTranscription(
            baseUrl = baseUrl,
            audioBytes = wavBytes,
            userId = userId,
            sessionId = sessionId,
            language = language,
        ).trim()
        return if (transcript.isEmpty()) {
            null
        } else {
            QueryCaptureResult(transcript = transcript, wavBytes = wavBytes)
        }
    }

    private fun requestTranscription(
        baseUrl: String,
        audioBytes: ByteArray,
        userId: String,
        sessionId: String,
        language: String,
    ): String {
        val boundary = "----HelloAgainBoundary${System.currentTimeMillis()}"
        val connection = (URL("${baseUrl.trimEnd('/')}/api/voice/transcribe/").openConnection()
            as HttpURLConnection).apply {
            requestMethod = "POST"
            connectTimeout = 60000
            readTimeout = 60000
            doOutput = true
            useCaches = false
            setRequestProperty("Content-Type", "multipart/form-data; boundary=$boundary")
        }

        DataOutputStream(BufferedOutputStream(connection.outputStream)).use { output ->
            writeFormField(output, boundary, "user_id", userId)
            writeFormField(output, boundary, "session_id", sessionId)
            if (language.isNotBlank()) {
                writeFormField(output, boundary, "language", language)
            }
            writeFileField(
                output = output,
                boundary = boundary,
                fieldName = "audio",
                fileName = "clarification.wav",
                mimeType = "audio/wav",
                data = audioBytes,
            )
            output.writeBytes("--$boundary--\r\n")
            output.flush()
        }

        val code = connection.responseCode
        val body = readAllBytes(
            if (code in 200..299) connection.inputStream else connection.errorStream,
        ).toString(Charsets.UTF_8)
        if (code !in 200..299) {
            throw IOException("Voice transcribe request failed with status $code.")
        }
        return JSONObject(body).optString("transcript", "")
    }

    private fun playAudio(audioBytes: ByteArray, mimeType: String) {
        val suffix = when {
            mimeType.contains("mpeg", ignoreCase = true) -> ".mp3"
            mimeType.contains("aac", ignoreCase = true) -> ".aac"
            mimeType.contains("ogg", ignoreCase = true) -> ".ogg"
            else -> ".wav"
        }
        val tempFile = File.createTempFile("helloagain_mic_bridge_", suffix, cacheDir)
        tempFile.writeBytes(audioBytes)
        val latch = CountDownLatch(1)
        val mediaPlayer = MediaPlayer()
        setCurrentPlayer(mediaPlayer)
        try {
            mediaPlayer.setAudioAttributes(
                AudioAttributes.Builder()
                    .setUsage(AudioAttributes.USAGE_ASSISTANCE_ACCESSIBILITY)
                    .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
                    .build(),
            )
            mediaPlayer.setDataSource(tempFile.absolutePath)
            mediaPlayer.setOnCompletionListener {
                latch.countDown()
            }
            mediaPlayer.setOnErrorListener { _, _, _ ->
                latch.countDown()
                true
            }
            mediaPlayer.prepare()
            mediaPlayer.start()
            while (!cancelRequested && latch.count > 0) {
                latch.await(250, TimeUnit.MILLISECONDS)
            }
            checkCancelled()
        } finally {
            clearCurrentPlayer(mediaPlayer)
            tempFile.delete()
        }
    }

    private fun ensureForeground(message: String) {
        val notification = buildNotification(message)
        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                startForeground(
                    NOTIFICATION_ID,
                    notification,
                    ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE,
                )
            } else {
                startForeground(NOTIFICATION_ID, notification)
            }
            if (!foregroundReady) {
                Log.i(TAG, "Mic bridge foreground promoted")
            }
            foregroundReady = true
        } catch (error: Exception) {
            foregroundReady = false
            Log.e(TAG, "Mic bridge foreground promotion failed", error)
            throw error
        }
    }

    private fun updateNotification(message: String) {
        val notification = buildNotification(message)
        val notificationManager = getSystemService(NotificationManager::class.java)
        notificationManager.notify(NOTIFICATION_ID, notification)
    }

    private fun buildNotification(message: String): android.app.Notification {
        val openAppIntent =
            packageManager.getLaunchIntentForPackage(packageName)
                ?: Intent(this, MainActivity::class.java)
        val openAppPendingIntent = PendingIntent.getActivity(
            this,
            REQUEST_CODE_OPEN_APP,
            openAppIntent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
        val stopPendingIntent = PendingIntent.getService(
            this,
            REQUEST_CODE_STOP,
            Intent(this, MicBridgeForegroundService::class.java).apply {
                action = ACTION_STOP_SERVICE
            },
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
        val preparePendingIntent = PendingIntent.getService(
            this,
            REQUEST_CODE_PREPARE,
            Intent(this, MicBridgeForegroundService::class.java).apply {
                action = ACTION_START_SERVICE
            },
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
        return NotificationCompat.Builder(this, CHANNEL_ID)
            .setContentTitle("Hello Again mic bridge")
            .setContentText(message)
            .setSmallIcon(android.R.drawable.ic_btn_speak_now)
            .setContentIntent(openAppPendingIntent)
            .setOngoing(true)
            .setOnlyAlertOnce(true)
            .setPriority(NotificationCompat.PRIORITY_LOW)
            .addAction(
                android.R.drawable.ic_media_pause,
                "Prepare bridge",
                preparePendingIntent,
            )
            .addAction(
                android.R.drawable.ic_menu_close_clear_cancel,
                "Stop voice bridge",
                stopPendingIntent,
            )
            .build()
    }

    private fun cancelActiveRequest(requestId: String?, code: String, message: String) {
        val currentRequestId = activeRequestId
        if (requestId.isNullOrBlank() || currentRequestId == null || requestId == currentRequestId) {
            cancelRequested = true
            releaseCurrentRecorder()
            releaseCurrentPlayer()
            if (currentRequestId != null) {
                MicBridgePlugin.emitError(currentRequestId, code, message)
            }
        }
    }

    private fun checkCancelled() {
        if (cancelRequested) {
            throw CancellationException("Mic bridge query cancelled.")
        }
    }

    private fun openJsonConnection(url: String): HttpURLConnection {
        return (URL(url).openConnection() as HttpURLConnection).apply {
            requestMethod = "POST"
            connectTimeout = 30000
            readTimeout = 30000
            doOutput = true
            useCaches = false
            setRequestProperty("Content-Type", "application/json")
        }
    }

    private fun writeFormField(
        output: DataOutputStream,
        boundary: String,
        fieldName: String,
        value: String,
    ) {
        output.writeBytes("--$boundary\r\n")
        output.writeBytes("Content-Disposition: form-data; name=\"$fieldName\"\r\n\r\n")
        output.write(value.toByteArray(Charsets.UTF_8))
        output.writeBytes("\r\n")
    }

    private fun writeFileField(
        output: DataOutputStream,
        boundary: String,
        fieldName: String,
        fileName: String,
        mimeType: String,
        data: ByteArray,
    ) {
        output.writeBytes("--$boundary\r\n")
        output.writeBytes(
            "Content-Disposition: form-data; name=\"$fieldName\"; filename=\"$fileName\"\r\n",
        )
        output.writeBytes("Content-Type: $mimeType\r\n\r\n")
        output.write(data)
        output.writeBytes("\r\n")
    }

    private fun readAllBytes(stream: java.io.InputStream?): ByteArray {
        if (stream == null) {
            return ByteArray(0)
        }
        BufferedInputStream(stream).use { input ->
            val buffer = ByteArrayOutputStream()
            val data = ByteArray(8192)
            while (true) {
                val read = input.read(data)
                if (read <= 0) {
                    break
                }
                buffer.write(data, 0, read)
            }
            return buffer.toByteArray()
        }
    }

    private fun joinChunks(chunks: List<ByteArray>): ByteArray {
        val output = ByteArrayOutputStream()
        chunks.forEach { output.write(it) }
        return output.toByteArray()
    }

    private fun wrapPcmAsWav(pcmBytes: ByteArray): ByteArray {
        val header = ByteBuffer.allocate(44).order(ByteOrder.LITTLE_ENDIAN)
        val byteRate = SAMPLE_RATE * CHANNEL_COUNT * 2
        val blockAlign = CHANNEL_COUNT * 2
        header.put("RIFF".toByteArray(Charsets.US_ASCII))
        header.putInt(36 + pcmBytes.size)
        header.put("WAVE".toByteArray(Charsets.US_ASCII))
        header.put("fmt ".toByteArray(Charsets.US_ASCII))
        header.putInt(16)
        header.putShort(1.toShort())
        header.putShort(CHANNEL_COUNT.toShort())
        header.putInt(SAMPLE_RATE)
        header.putInt(byteRate)
        header.putShort(blockAlign.toShort())
        header.putShort(16.toShort())
        header.put("data".toByteArray(Charsets.US_ASCII))
        header.putInt(pcmBytes.size)
        return header.array() + pcmBytes
    }

    private fun pcmLevel(chunk: ByteArray): Double {
        if (chunk.size < 2) {
            return 0.0
        }
        val shortBuffer = ByteBuffer.wrap(chunk).order(ByteOrder.LITTLE_ENDIAN).asShortBuffer()
        var sumSquares = 0.0
        val sampleCount = shortBuffer.remaining()
        while (shortBuffer.hasRemaining()) {
            val sample = shortBuffer.get() / 32768.0
            sumSquares += sample * sample
        }
        return sqrt(sumSquares / sampleCount).coerceIn(0.0, 1.0)
    }

    private fun createNotificationChannel() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val channel = NotificationChannel(
                CHANNEL_ID,
                CHANNEL_NAME,
                NotificationManager.IMPORTANCE_LOW,
            ).apply {
                description = "Foreground microphone bridge for background clarification turns"
                setShowBadge(false)
            }
            val notificationManager = getSystemService(NotificationManager::class.java)
            notificationManager.createNotificationChannel(channel)
        }
    }

    private fun notificationMessageForCurrentState(): String {
        return when (bridgeState) {
            BridgeState.ARMED_IDLE -> {
                if (hasRecordAudioPermission()) {
                    "Mic bridge is armed and ready for clarification."
                } else {
                    "Microphone permission required. Open Hello Again to re-enable voice bridge."
                }
            }
            BridgeState.SPEAKING_PROMPT -> "Asking a clarification question..."
            BridgeState.ACTIVE_LISTENING -> "Listening for one clarification reply..."
        }
    }

    private fun ensurePermissionOrRecover(): Boolean {
        if (hasRecordAudioPermission()) {
            return true
        }
        handlePermissionFailure(
            activeRequestId,
            "Microphone permission is missing. Open Hello Again to prepare the voice bridge again.",
        )
        bridgeState = BridgeState.ARMED_IDLE
        MicBridgePlugin.setServiceRunning(false)
        updateNotification(notificationMessageForCurrentState())
        return false
    }

    private fun requireForegroundReadyForCapture(requestId: String) {
        if (!foregroundReady) {
            throw IllegalStateException("Mic bridge is not yet foreground-ready for request $requestId.")
        }
    }

    private fun handlePermissionFailure(requestId: String?, message: String) {
        Log.w(TAG, "Mic bridge permission failure: $message")
        MicBridgePlugin.setServiceRunning(false)
        MicBridgePlugin.emitError(requestId, "mic_permission_missing", message)
        updateNotification("Microphone permission required. Open Hello Again to prepare the voice bridge.")
    }

    private fun hasRecordAudioPermission(): Boolean =
        ContextCompat.checkSelfPermission(
            this,
            Manifest.permission.RECORD_AUDIO,
        ) == PackageManager.PERMISSION_GRANTED

    private fun setCurrentRecorder(recorder: AudioRecord) {
        synchronized(resourceLock) {
            currentRecorder?.safeStopAndRelease()
            currentRecorder = recorder
        }
    }

    private fun clearCurrentRecorder(recorder: AudioRecord) {
        synchronized(resourceLock) {
            if (currentRecorder === recorder) {
                currentRecorder = null
            }
        }
        recorder.safeStopAndRelease()
    }

    private fun releaseCurrentRecorder() {
        val recorderToRelease = synchronized(resourceLock) {
            currentRecorder.also {
                currentRecorder = null
            }
        }
        recorderToRelease.safeStopAndRelease()
    }

    private fun setCurrentPlayer(player: MediaPlayer) {
        synchronized(resourceLock) {
            currentPlayer?.safeStopAndRelease()
            currentPlayer = player
        }
    }

    private fun clearCurrentPlayer(player: MediaPlayer) {
        synchronized(resourceLock) {
            if (currentPlayer === player) {
                currentPlayer = null
            }
        }
        player.safeStopAndRelease()
    }

    private fun releaseCurrentPlayer() {
        val playerToRelease = synchronized(resourceLock) {
            currentPlayer.also {
                currentPlayer = null
            }
        }
        playerToRelease.safeStopAndRelease()
    }

    private fun AudioRecord?.safeStopAndRelease() {
        this ?: return
        try {
            stop()
        } catch (_: Exception) {
        }
        try {
            release()
        } catch (_: Exception) {
        }
    }

    private fun MediaPlayer?.safeStopAndRelease() {
        this ?: return
        try {
            stop()
        } catch (_: Exception) {
        }
        try {
            release()
        } catch (_: Exception) {
        }
    }
}
