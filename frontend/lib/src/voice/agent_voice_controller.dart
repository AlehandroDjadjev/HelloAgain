import 'dart:async';
import 'dart:collection';
import 'dart:math' as math;
import 'dart:typed_data';

import 'package:audioplayers/audioplayers.dart';
import 'package:flutter/foundation.dart';
import 'package:record/record.dart';

import '../api/voice_gateway_client.dart';
import 'background_voice_service.dart';

class AgentVoiceController extends ChangeNotifier {
  AgentVoiceController({
    required VoiceGatewayClient client,
    required Future<void> Function(String transcript) onTranscript,
    BackgroundVoiceService backgroundService = const BackgroundVoiceService(),
    String userId = 'helloagain-agent',
    String? sessionId,
    String language = 'en-US',
  }) : _client = client,
       _backgroundService = backgroundService,
       _onTranscript = onTranscript,
       _userId = userId,
       _sessionId =
           sessionId ?? 'agent-voice-${DateTime.now().millisecondsSinceEpoch}',
       _language = language;

  static const _sampleRate = 16000;
  static const _channels = 1;
  static const _speechThreshold = 0.035;
  static const _singleTurnSpeechThreshold = 0.015;
  static const _silenceWindow = Duration(milliseconds: 900);
  static const _singleTurnSilenceWindow = Duration(milliseconds: 900);
  static const _maxTurnLength = Duration(seconds: 14);
  static const _singleTurnMaxTurnLength = Duration(seconds: 10);
  static const _minTurnLength = Duration(milliseconds: 450);
  static const _preSpeechChunkLimit = 8;

  final Future<void> Function(String transcript) _onTranscript;
  String _userId;
  String _sessionId;
  final BackgroundVoiceService _backgroundService;

  final VoiceGatewayClient _client;
  AudioRecorder? _recorder;
  AudioPlayer? _player;
  StreamSubscription<Uint8List>? _recSub;
  StreamSubscription<PlayerState>? _playerStateSub;
  final List<Uint8List> _turnChunks = <Uint8List>[];
  final ListQueue<Uint8List> _preSpeechChunks = ListQueue<Uint8List>();

  bool _enabled = false;
  bool _listening = false;
  bool _processing = false;
  bool _speaking = false;
  bool _finalizing = false;
  bool _speechDetected = false;
  bool _suspended = false;
  bool _disposed = false;
  double _micLevel = 0;
  String _status = 'Hands-free mode is off.';
  final String _language;
  String? _error;
  String _lastTranscript = '';
  DateTime? _speechStartedAt;
  DateTime? _lastVoiceAt;
  DateTime? _captureStartedAt;
  Completer<String?>? _singleTurnCompleter;
  Timer? _singleTurnTimer;
  double _turnPeakLevel = 0;

  bool get enabled => _enabled;
  bool get listening => _listening;
  bool get processing => _processing;
  bool get speaking => _speaking;
  bool get waitingForSingleTurn => _singleTurnCompleter != null;
  double get micLevel => _micLevel;
  String get status => _status;
  String get language => _language;
  String? get error => _error;
  String get lastTranscript => _lastTranscript;

  void updateSessionContext({String? userId, String? sessionId}) {
    final nextUserId = (userId ?? '').trim();
    final nextSessionId = (sessionId ?? '').trim();
    if (nextUserId.isNotEmpty) {
      _userId = nextUserId;
    }
    if (nextSessionId.isNotEmpty) {
      _sessionId = nextSessionId;
    }
  }

  Future<void> start() async {
    if (_enabled) {
      return;
    }
    final recorder = await _ensureRecorder();
    if (!await recorder.hasPermission()) {
      throw const VoiceGatewayException(
        'Microphone permission is required for hands-free mode.',
      );
    }

    await _backgroundService.start();
    _enabled = true;
    _suspended = false;
    _error = null;
    _status = 'Hands-free mode is active. Listening for speech...';
    _emit();
    await _startListeningLoop();
  }

  Future<void> stop() async {
    _enabled = false;
    _suspended = false;
    _resetTurn();
    await _stopRecorder();
    try {
      await _player?.stop();
    } catch (_) {}
    await _backgroundService.stop();
    _processing = false;
    _speaking = false;
    _micLevel = 0;
    _status = 'Hands-free mode is off.';
    _emit();
  }

  Future<void> pauseForTask({String? status}) async {
    _suspended = true;
    await _stopRecorder();
    if (status != null && status.trim().isNotEmpty) {
      _status = status.trim();
      _emit();
    }
  }

  Future<void> resumeListening({String? status}) async {
    _suspended = false;
    if (status != null && status.trim().isNotEmpty) {
      _status = status.trim();
      _emit();
    }
    if (_enabled && !_processing && !_speaking) {
      await _startListeningLoop();
    }
  }

  Future<void> speakText(String text, {bool resumeWhenDone = false}) async {
    final cleanText = text.trim();
    if (cleanText.isEmpty) {
      if (resumeWhenDone) {
        await resumeListening(
          status: 'Hands-free mode is active. Listening for speech...',
        );
      }
      return;
    }

    await _stopRecorder();
    _speaking = true;
    _error = null;
    _status = 'Preparing a spoken reply...';
    _emit();

    try {
      final speech = await _client.speak(
        text: cleanText,
        userId: _userId,
        sessionId: _sessionId,
      );
      _status = 'Speaking...';
      _emit();
      await _playAudio(speech.audioBytes, speech.mimeType);
    } catch (error) {
      _error = _describeError(error);
      _status = 'Could not play the spoken reply.';
      _emit();
    } finally {
      _speaking = false;
      if (resumeWhenDone) {
        _suspended = false;
      }
      _emit();
      if (_enabled && !_suspended && !_processing) {
        await _startListeningLoop();
      }
    }
  }

  Future<String> speakPrompt(
    String prompt, {
    bool resumeWhenDone = false,
  }) async {
    final cleanPrompt = prompt.trim();
    if (cleanPrompt.isEmpty) {
      if (resumeWhenDone) {
        await resumeListening(
          status: 'Hands-free mode is active. Listening for speech...',
        );
      }
      return '';
    }

    await _stopRecorder();
    _speaking = true;
    _error = null;
    _status = 'Generating a spoken reply...';
    _emit();

    try {
      final response = await _client.getResponse(
        prompt: cleanPrompt,
        userId: _userId,
        sessionId: _sessionId,
      );
      _status = 'Speaking...';
      _emit();
      await _playAudio(
        response.assistantAudioBytes,
        response.assistantAudioMimeType,
      );
      return response.assistantText;
    } catch (error) {
      _error = _describeError(error);
      _status = 'Could not generate the spoken reply.';
      _emit();
      return '';
    } finally {
      _speaking = false;
      if (resumeWhenDone) {
        _suspended = false;
      }
      _emit();
      if (_enabled && !_suspended && !_processing) {
        await _startListeningLoop();
      }
    }
  }

  Future<String?> askQuestionOnce(
    String question, {
    Duration timeout = const Duration(seconds: 18),
  }) async {
    final cleanQuestion = question.trim();
    if (cleanQuestion.isEmpty) {
      return null;
    }

    final wasEnabled = _enabled;
    if (!wasEnabled) {
      await start();
    }

    await pauseForTask(status: 'Asking a clarification question...');
    _singleTurnTimer?.cancel();
    final completer = Completer<String?>();
    _singleTurnCompleter = completer;
    _singleTurnTimer = Timer(timeout, () {
      unawaited(_handleSingleTurnTimeout(completer));
    });

    try {
      await speakText(cleanQuestion);
      if (!_enabled) {
        return null;
      }
      await resumeListening(status: 'Listening for one clarification reply...');
      return await completer.future;
    } finally {
      _singleTurnTimer?.cancel();
      _singleTurnTimer = null;
      if (identical(_singleTurnCompleter, completer)) {
        _singleTurnCompleter = null;
      }
      if (!wasEnabled) {
        await stop();
      }
    }
  }

  Future<String?> askQuestionOnceInBackground(
    String question, {
    Duration timeout = const Duration(seconds: 18),
  }) async {
    final cleanQuestion = question.trim();
    if (cleanQuestion.isEmpty) {
      return null;
    }

    await _stopRecorder();
    _processing = true;
    _suspended = true;
    _error = null;
    _status = 'Using Android background voice for a clarification reply...';
    _emit();

    try {
      final transcript = await _backgroundService.runSingleTurn(
        question: cleanQuestion,
        baseUrl: _client.baseUrl,
        userId: _userId,
        sessionId: _sessionId,
        language: '',
        timeout: timeout,
      );
      if (transcript == null || transcript.trim().isEmpty) {
        _status = 'No background clarification reply was detected.';
        _emit();
        return null;
      }
      _lastTranscript = transcript.trim();
      _status = 'Heard in background: "$_lastTranscript"';
      _emit();
      return _lastTranscript;
    } catch (error) {
      _error = _describeError(error);
      _status = 'Background clarification listening failed.';
      _emit();
      return null;
    } finally {
      _processing = false;
      _emit();
    }
  }

  Future<AudioRecorder> _ensureRecorder() async =>
      _recorder ??= AudioRecorder();

  AudioPlayer _ensurePlayer() {
    final player = _player;
    if (player != null) {
      return player;
    }
    final created = AudioPlayer();
    _playerStateSub = created.onPlayerStateChanged.listen((state) {
      final nextSpeaking = state == PlayerState.playing;
      if (_speaking != nextSpeaking) {
        _speaking = nextSpeaking;
        _emit();
      }
    });
    _player = created;
    return created;
  }

  Future<void> _startListeningLoop() async {
    if (!_enabled || _listening || _processing || _speaking || _suspended) {
      return;
    }
    final recorder = await _ensureRecorder();
    await _stopRecorder();
    _resetTurn();

    final stream = await recorder.startStream(
      const RecordConfig(
        encoder: AudioEncoder.pcm16bits,
        sampleRate: _sampleRate,
        numChannels: _channels,
        echoCancel: true,
        noiseSuppress: true,
      ),
    );

    _recSub = stream.listen(
      _handleChunk,
      onError: (Object error, StackTrace stackTrace) {
        unawaited(_handleRecorderError(error));
      },
      cancelOnError: true,
    );

    _listening = true;
    _micLevel = 0;
    _captureStartedAt = DateTime.now();
    _turnPeakLevel = 0;
    _status = 'Hands-free mode is active. Listening for speech...';
    _emit();
  }

  Future<void> _stopRecorder() async {
    await _recSub?.cancel();
    _recSub = null;
    try {
      await _recorder?.stop();
    } catch (_) {}
    if (_listening || _micLevel != 0) {
      _listening = false;
      _micLevel = 0;
      _emit();
    }
  }

  void _handleChunk(Uint8List chunk) {
    if (!_enabled || _processing || _speaking || _finalizing || _suspended) {
      return;
    }

    final level = _pcmLevel(chunk);
    final detectionThreshold = waitingForSingleTurn
        ? _singleTurnSpeechThreshold
        : _speechThreshold;
    if (level > _turnPeakLevel) {
      _turnPeakLevel = level;
    }
    _micLevel = ((_micLevel * 0.6) + (level * 0.4)).clamp(0, 1).toDouble();
    _emit();

    final now = DateTime.now();
    final silenceWindow = waitingForSingleTurn
        ? _singleTurnSilenceWindow
        : _silenceWindow;
    final maxTurnLength = waitingForSingleTurn
        ? _singleTurnMaxTurnLength
        : _maxTurnLength;
    if (waitingForSingleTurn) {
      _turnChunks.add(chunk);
    }

    if (_speechDetected) {
      if (!waitingForSingleTurn) {
        _turnChunks.add(chunk);
      }
      if (level >= detectionThreshold) {
        _lastVoiceAt = now;
      }

      if (_speechStartedAt != null &&
          now.difference(_speechStartedAt!) >= maxTurnLength) {
        unawaited(_finishTurn());
      } else if (_lastVoiceAt != null &&
          now.difference(_lastVoiceAt!) >= silenceWindow) {
        unawaited(_finishTurn());
      }
      return;
    }

    _preSpeechChunks.add(chunk);
    while (_preSpeechChunks.length > _preSpeechChunkLimit) {
      _preSpeechChunks.removeFirst();
    }

    if (level >= detectionThreshold) {
      _speechDetected = true;
      _speechStartedAt = now;
      _lastVoiceAt = now;
      if (!waitingForSingleTurn) {
        _turnChunks
          ..clear()
          ..addAll(_preSpeechChunks)
          ..add(chunk);
      }
      _preSpeechChunks.clear();
      _status = 'Speech detected. Keep talking...';
      _emit();
    }
  }

  Future<void> _finishTurn({bool forceSingleTurn = false}) async {
    if (_finalizing || (!_speechDetected && !forceSingleTurn)) {
      return;
    }
    _finalizing = true;
    final startedAt = _speechStartedAt ?? _captureStartedAt;
    final pcmBytes = _joinChunks(_turnChunks);
    await _stopRecorder();

    if (startedAt == null ||
        DateTime.now().difference(startedAt) <
            (waitingForSingleTurn
                ? const Duration(milliseconds: 250)
                : _minTurnLength) ||
        pcmBytes.isEmpty) {
      _resetTurn();
      _finalizing = false;
      if (_singleTurnCompleter != null &&
          !_singleTurnCompleter!.isCompleted &&
          waitingForSingleTurn) {
        _status = 'No reply detected for the clarification question.';
        _suspended = true;
        _emit();
        _singleTurnCompleter!.complete(null);
        return;
      }
      if (_enabled && !_suspended) {
        await _startListeningLoop();
      }
      return;
    }

    _processing = true;
    _error = null;
    _status = 'Transcribing with the voice gateway...';
    _emit();

    try {
      final response = await _client.transcribe(
        audioBytes: _wrapPcmAsWav(
          pcmBytes,
          sampleRate: _sampleRate,
          channels: _channels,
        ),
        language: waitingForSingleTurn ? null : _language,
        userId: _userId,
        sessionId: _sessionId,
      );
      final transcript = response.transcript.trim();
      if (transcript.isEmpty) {
        if (_singleTurnCompleter != null && !_singleTurnCompleter!.isCompleted) {
          _status = 'No speech detected for the clarification reply.';
          _suspended = true;
          _emit();
          _singleTurnCompleter!.complete(null);
          return;
        }
        _status = 'No speech detected. Listening again...';
        _emit();
        return;
      }

      _lastTranscript = transcript;
      _status = 'Heard: "$transcript"';
      _emit();
      if (_singleTurnCompleter != null && !_singleTurnCompleter!.isCompleted) {
        _suspended = true;
        _singleTurnCompleter!.complete(transcript);
        return;
      }
      await _onTranscript(transcript);
    } catch (error) {
      _error = _describeError(error);
      if (_singleTurnCompleter != null && !_singleTurnCompleter!.isCompleted) {
        _suspended = true;
        _singleTurnCompleter!.complete(null);
      }
      _status = 'Could not transcribe that turn. Listening again...';
      _emit();
    } finally {
      _processing = false;
      _resetTurn();
      _finalizing = false;
      _emit();
      if (_enabled && !_suspended && !_speaking) {
        await _startListeningLoop();
      }
    }
  }

  Future<void> _playAudio(Uint8List bytes, String mimeType) async {
    final player = _ensurePlayer();
    await player.stop();
    await player.play(BytesSource(bytes, mimeType: mimeType));
    try {
      await player.onPlayerComplete.first.timeout(const Duration(seconds: 45));
    } on TimeoutException {
      // Some Android devices do not emit a completion event reliably.
    }
  }

  Future<void> _handleRecorderError(Object error) async {
    await _stopRecorder();
    _resetTurn();
    _error = _describeError(error);
    _status = 'The microphone stream stopped unexpectedly.';
    _emit();
  }

  void _emit() {
    if (!_disposed) {
      notifyListeners();
    }
  }

  void _resetTurn() {
    _speechDetected = false;
    _speechStartedAt = null;
    _lastVoiceAt = null;
    _captureStartedAt = null;
    _turnPeakLevel = 0;
    _preSpeechChunks.clear();
    _turnChunks.clear();
  }

  Future<void> _handleSingleTurnTimeout(Completer<String?> completer) async {
    if (completer.isCompleted) {
      return;
    }
    if (_listening || _turnChunks.isNotEmpty) {
      await _finishTurn(forceSingleTurn: true);
    }
    if (!completer.isCompleted) {
      _status = 'No reply detected for the clarification question.';
      _suspended = true;
      _emit();
      completer.complete(null);
    }
  }

  String _describeError(Object error) =>
      error.toString().replaceFirst('Exception: ', '').trim();

  @override
  void dispose() {
    _disposed = true;
    _singleTurnTimer?.cancel();
    if (_singleTurnCompleter != null && !_singleTurnCompleter!.isCompleted) {
      _singleTurnCompleter!.complete(null);
    }
    unawaited(_recSub?.cancel());
    unawaited(_playerStateSub?.cancel());
    unawaited(_player?.dispose());
    unawaited(_recorder?.dispose());
    unawaited(_backgroundService.stop());
    super.dispose();
  }
}

Uint8List _joinChunks(List<Uint8List> chunks) {
  final builder = BytesBuilder(copy: false);
  for (final chunk in chunks) {
    builder.add(chunk);
  }
  return builder.toBytes();
}

Uint8List _wrapPcmAsWav(
  Uint8List pcmBytes, {
  required int sampleRate,
  required int channels,
}) {
  final header = ByteData(44);
  final byteRate = sampleRate * channels * 2;
  final blockAlign = channels * 2;
  final dataLength = pcmBytes.length;

  void writeAscii(int offset, String value) {
    for (var i = 0; i < value.length; i++) {
      header.setUint8(offset + i, value.codeUnitAt(i));
    }
  }

  writeAscii(0, 'RIFF');
  header.setUint32(4, 36 + dataLength, Endian.little);
  writeAscii(8, 'WAVE');
  writeAscii(12, 'fmt ');
  header.setUint32(16, 16, Endian.little);
  header.setUint16(20, 1, Endian.little);
  header.setUint16(22, channels, Endian.little);
  header.setUint32(24, sampleRate, Endian.little);
  header.setUint32(28, byteRate, Endian.little);
  header.setUint16(32, blockAlign, Endian.little);
  header.setUint16(34, 16, Endian.little);
  writeAscii(36, 'data');
  header.setUint32(40, dataLength, Endian.little);

  return Uint8List.fromList([...header.buffer.asUint8List(), ...pcmBytes]);
}

double _pcmLevel(Uint8List chunk) {
  if (chunk.lengthInBytes < 2) {
    return 0;
  }
  final bytes = ByteData.sublistView(chunk);
  final sampleCount = chunk.lengthInBytes ~/ 2;
  var sumSquares = 0.0;
  for (var offset = 0; offset < chunk.lengthInBytes; offset += 2) {
    final sample = bytes.getInt16(offset, Endian.little) / 32768.0;
    sumSquares += sample * sample;
  }
  return math.sqrt(sumSquares / sampleCount).clamp(0.0, 1.0).toDouble();
}
