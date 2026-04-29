import 'dart:async';
import 'dart:convert';

import 'package:flutter/foundation.dart';
import 'package:flutter/services.dart';

enum MicBridgeEventType {
  serviceReady,
  ttsStarted,
  ttsFinished,
  listeningStarted,
  partialTranscript,
  finalTranscript,
  error,
  unknown,
}

enum MicBridgeServiceState { armedIdle, unknown }

class MicBridgeEvent {
  const MicBridgeEvent({
    required this.type,
    this.requestId,
    this.serviceState,
    this.prompt,
    this.timeoutMs,
    this.transcript,
    this.audioBytes,
    this.audioMimeType,
    this.code,
    this.message,
  });

  final MicBridgeEventType type;
  final String? requestId;
  final MicBridgeServiceState? serviceState;
  final String? prompt;
  final int? timeoutMs;
  final String? transcript;
  final Uint8List? audioBytes;
  final String? audioMimeType;
  final String? code;
  final String? message;

  factory MicBridgeEvent.fromMap(Map<String, dynamic> event) {
    final audioBase64 = (event['audioBase64'] ?? '').toString().trim();
    final stateLabel = (event['state'] ?? '').toString().trim();
    return MicBridgeEvent(
      type: _parseEventType((event['event'] ?? '').toString().trim()),
      requestId: _readNonEmptyString(event['requestId']),
      serviceState: stateLabel.isEmpty ? null : _parseServiceState(stateLabel),
      prompt: _readNonEmptyString(event['prompt']),
      timeoutMs: (event['timeoutMs'] as num?)?.toInt(),
      transcript: _readNonEmptyString(event['transcript']),
      audioBytes: audioBase64.isEmpty ? null : base64Decode(audioBase64),
      audioMimeType: _readNonEmptyString(event['audioMimeType']),
      code: _readNonEmptyString(event['code']),
      message: _readNonEmptyString(event['message']),
    );
  }

  static MicBridgeEventType _parseEventType(String value) {
    switch (value) {
      case 'serviceReady':
        return MicBridgeEventType.serviceReady;
      case 'ttsStarted':
        return MicBridgeEventType.ttsStarted;
      case 'ttsFinished':
        return MicBridgeEventType.ttsFinished;
      case 'listeningStarted':
        return MicBridgeEventType.listeningStarted;
      case 'partialTranscript':
        return MicBridgeEventType.partialTranscript;
      case 'finalTranscript':
        return MicBridgeEventType.finalTranscript;
      case 'error':
        return MicBridgeEventType.error;
      default:
        return MicBridgeEventType.unknown;
    }
  }

  static MicBridgeServiceState _parseServiceState(String value) {
    switch (value) {
      case 'armed_idle':
        return MicBridgeServiceState.armedIdle;
      default:
        return MicBridgeServiceState.unknown;
    }
  }

  static String? _readNonEmptyString(Object? value) {
    final stringValue = value?.toString().trim() ?? '';
    return stringValue.isEmpty ? null : stringValue;
  }
}

class MicBridgeQueryResult {
  const MicBridgeQueryResult({
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

class MicBridgeException implements Exception {
  const MicBridgeException(this.code, this.message);

  final String code;
  final String message;

  @override
  String toString() => 'MicBridgeException($code): $message';
}

class MicBridgeClient {
  MicBridgeClient({
    required String baseUrl,
    String userId = 'helloagain-agent',
    String? sessionId,
    String language = 'bg-BG',
    MethodChannel? methodChannel,
    EventChannel? eventChannel,
  }) : _baseUrl = baseUrl.trim(),
       _userId = userId.trim().isEmpty ? 'helloagain-agent' : userId.trim(),
       _sessionId = sessionId?.trim() ?? '',
       _language = language.trim(),
       _methodChannel =
           methodChannel ?? const MethodChannel('com.example.frontend/mic_bridge'),
       _eventChannel =
           eventChannel ??
               const EventChannel('com.example.frontend/mic_bridge/events');

  final MethodChannel _methodChannel;
  final EventChannel _eventChannel;
  final StreamController<MicBridgeEvent> _eventsController =
      StreamController<MicBridgeEvent>.broadcast();

  StreamSubscription<dynamic>? _nativeEventsSubscription;
  final String _baseUrl;
  String _userId;
  String _sessionId;
  final String _language;
  bool _serviceReady = false;
  bool _disposed = false;

  bool get isSupported =>
      !kIsWeb && defaultTargetPlatform == TargetPlatform.android;

  Stream<MicBridgeEvent> get events {
    _ensureEventSubscription();
    return _eventsController.stream;
  }

  void updateSessionContext({String? userId, String? sessionId}) {
    final nextUserId = (userId ?? '').trim();
    if (nextUserId.isNotEmpty) {
      _userId = nextUserId;
    }
    if (sessionId != null) {
      _sessionId = sessionId.trim();
    }
  }

  Future<void> ensureServiceReady() async {
    if (!isSupported || _disposed) {
      return;
    }
    _ensureEventSubscription();
    if (_serviceReady) {
      return;
    }
    final ready = events
        .firstWhere((event) => event.type == MicBridgeEventType.serviceReady)
        .timeout(const Duration(seconds: 5));
    await _methodChannel.invokeMethod<void>('startMicBridgeService');
    await ready;
  }

  Future<MicBridgeQueryResult?> beginOneShotQuery(
    String question, {
    Duration timeout = const Duration(seconds: 18),
  }) async {
    if (!isSupported || _disposed) {
      return null;
    }
    final cleanQuestion = question.trim();
    if (cleanQuestion.isEmpty) {
      return null;
    }

    await ensureServiceReady();
    final requestId = DateTime.now().microsecondsSinceEpoch.toString();
    final completion = events
        .firstWhere((event) {
          if (event.requestId != requestId) {
            return false;
          }
          return event.type == MicBridgeEventType.finalTranscript ||
              event.type == MicBridgeEventType.error;
        })
        .timeout(timeout + const Duration(seconds: 8));

    await _methodChannel.invokeMethod<void>('beginOneShotQuery', <String, dynamic>{
      'request_id': requestId,
      'question': cleanQuestion,
      'base_url': _baseUrl,
      'user_id': _userId,
      'session_id': _sessionId,
      'language': _language,
      'timeout_ms': timeout.inMilliseconds,
    });

    final event = await completion;
    switch (event.type) {
      case MicBridgeEventType.finalTranscript:
        final transcript = (event.transcript ?? '').trim();
        if (transcript.isEmpty) {
          return null;
        }
        return MicBridgeQueryResult(
          requestId: requestId,
          transcript: transcript,
          audioBytes: event.audioBytes,
          audioMimeType: event.audioMimeType,
        );
      case MicBridgeEventType.error:
        final code = event.code ?? 'mic_bridge_error';
        if (code == 'no_transcript' || code == 'listening_cancelled') {
          return null;
        }
        throw MicBridgeException(
          code,
          event.message ?? 'Mic bridge query failed.',
        );
      case MicBridgeEventType.serviceReady:
      case MicBridgeEventType.ttsStarted:
      case MicBridgeEventType.ttsFinished:
      case MicBridgeEventType.listeningStarted:
      case MicBridgeEventType.partialTranscript:
      case MicBridgeEventType.unknown:
        return null;
    }
  }

  Future<void> cancelListening({String? requestId}) async {
    if (!isSupported || _disposed) {
      return;
    }
    await _methodChannel.invokeMethod<void>('cancelListening', <String, dynamic>{
      if ((requestId ?? '').trim().isNotEmpty) 'request_id': requestId!.trim(),
    });
  }

  Future<void> stopService() async {
    if (!isSupported || _disposed) {
      return;
    }
    _serviceReady = false;
    await _methodChannel.invokeMethod<void>('stopMicBridgeService');
  }

  Future<void> dispose() async {
    if (_disposed) {
      return;
    }
    _disposed = true;
    await _nativeEventsSubscription?.cancel();
    await _eventsController.close();
  }

  void _ensureEventSubscription() {
    if (_nativeEventsSubscription != null || !isSupported || _disposed) {
      return;
    }
    _nativeEventsSubscription = _eventChannel.receiveBroadcastStream().listen(
      (dynamic rawEvent) {
        if (_disposed || rawEvent is! Map) {
          return;
        }
        final event = MicBridgeEvent.fromMap(
          Map<String, dynamic>.from(rawEvent),
        );
        if (event.type == MicBridgeEventType.serviceReady) {
          _serviceReady = true;
        }
        if (!_eventsController.isClosed) {
          _eventsController.add(event);
        }
      },
      onError: (Object error, StackTrace stackTrace) {
        if (!_eventsController.isClosed) {
          _eventsController.addError(error, stackTrace);
        }
      },
    );
  }
}
