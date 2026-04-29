import 'dart:async';
import 'dart:convert';

import 'package:flutter/foundation.dart';
import 'package:flutter/services.dart';

class MicBridgeTurnResult {
  const MicBridgeTurnResult({
    required this.requestId,
    required this.transcript,
    this.audioBytes,
    this.audioMimeType,
  });

  final String requestId;
  final String transcript;
  final Uint8List? audioBytes;
  final String? audioMimeType;
}

class BackgroundVoiceService {
  const BackgroundVoiceService();

  static const _methodChannel = MethodChannel('com.example.frontend/mic_bridge');
  static const _eventChannel = EventChannel('com.example.frontend/mic_bridge/events');
  static final Stream<Map<String, dynamic>> _events = _eventChannel
      .receiveBroadcastStream()
      .map((dynamic event) => Map<String, dynamic>.from(event as Map))
      .asBroadcastStream();

  bool get isSupported =>
      !kIsWeb && defaultTargetPlatform == TargetPlatform.android;
  Stream<Map<String, dynamic>> get events => _events;

  Future<void> start() async {
    if (!isSupported) {
      return;
    }
    try {
      final ready = _waitForEvent(
        eventName: 'serviceReady',
        timeout: const Duration(seconds: 5),
      );
      await _methodChannel.invokeMethod<void>('startMicBridgeService');
      await ready;
    } on PlatformException {
      // The agent can still run in the foreground even if the notification
      // service could not be started on this device.
    } on TimeoutException {
      // The service may still start successfully even if the ready event was
      // delayed while the app was transitioning states.
    }
  }

  Future<void> stop() async {
    if (!isSupported) {
      return;
    }
    try {
      await _methodChannel.invokeMethod<void>('stopMicBridgeService');
    } on PlatformException {
      // Ignore stop failures during teardown.
    }
  }

  Future<void> cancelListening({String? requestId}) async {
    if (!isSupported) {
      return;
    }
    try {
      await _methodChannel.invokeMethod<void>('cancelListening', <String, dynamic>{
        if ((requestId ?? '').trim().isNotEmpty) 'request_id': requestId!.trim(),
      });
    } on PlatformException {
      // Ignore cancellation failures during teardown or race conditions.
    }
  }

  Future<String?> runSingleTurn({
    required String question,
    required String baseUrl,
    required String userId,
    required String sessionId,
    required String language,
    Duration timeout = const Duration(seconds: 16),
  }) async {
    final result = await runSingleTurnDetailed(
      question: question,
      baseUrl: baseUrl,
      userId: userId,
      sessionId: sessionId,
      language: language,
      timeout: timeout,
    );
    final transcript = result?.transcript.trim() ?? '';
    return transcript.isEmpty ? null : transcript;
  }

  Future<MicBridgeTurnResult?> runSingleTurnDetailed({
    required String question,
    required String baseUrl,
    required String userId,
    required String sessionId,
    required String language,
    Duration timeout = const Duration(seconds: 16),
  }) async {
    if (!isSupported) {
      return null;
    }
    final cleanQuestion = question.trim();
    final cleanBaseUrl = baseUrl.trim();
    if (cleanQuestion.isEmpty || cleanBaseUrl.isEmpty) {
      return null;
    }

    final requestId = DateTime.now().microsecondsSinceEpoch.toString();
    final completion = _waitForRequestCompletion(
      requestId: requestId,
      timeout: timeout + const Duration(seconds: 8),
    );

    try {
      await _methodChannel.invokeMethod<Map<dynamic, dynamic>>(
        'beginOneShotQuery',
        <String, dynamic>{
        'request_id': requestId,
        'question': cleanQuestion,
        'base_url': cleanBaseUrl,
        'user_id': userId.trim(),
        'session_id': sessionId.trim(),
        'language': language.trim(),
        'timeout_ms': timeout.inMilliseconds,
      },
      );
      return await completion;
    } on TimeoutException {
      await cancelListening(requestId: requestId);
      return null;
    } on PlatformException {
      return null;
    }
  }

  Future<Map<String, dynamic>> _waitForEvent({
    required String eventName,
    required Duration timeout,
  }) {
    return _events.firstWhere((event) => event['event'] == eventName).timeout(timeout);
  }

  Future<MicBridgeTurnResult?> _waitForRequestCompletion({
    required String requestId,
    required Duration timeout,
  }) async {
    final event = await _events
        .firstWhere((event) {
          if (event['requestId']?.toString() != requestId) {
            return false;
          }
          final type = event['event']?.toString() ?? '';
          return type == 'finalTranscript' || type == 'error';
        })
        .timeout(timeout);
    if (event['event'] == 'error') {
      return null;
    }

    final transcript = (event['transcript'] ?? '').toString().trim();
    if (transcript.isEmpty) {
      return null;
    }
    final audioBase64 = (event['audioBase64'] ?? '').toString().trim();
    return MicBridgeTurnResult(
      requestId: requestId,
      transcript: transcript,
      audioBytes: audioBase64.isEmpty ? null : base64Decode(audioBase64),
      audioMimeType: (event['audioMimeType'] ?? '').toString().trim().isEmpty
          ? null
          : (event['audioMimeType'] ?? '').toString().trim(),
    );
  }
}
