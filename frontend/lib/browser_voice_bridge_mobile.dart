import 'dart:async';
import 'dart:convert';
import 'dart:collection';
import 'dart:math' as math;
import 'dart:typed_data';

import 'package:audioplayers/audioplayers.dart';
import 'package:flutter_tts/flutter_tts.dart';
import 'package:permission_handler/permission_handler.dart';
import 'package:record/record.dart';

class CapturedAudioTurn {
  const CapturedAudioTurn({
    required this.audioBase64,
    required this.mimeType,
    required this.language,
    this.transcript,
  });

  final String audioBase64;
  final String mimeType;
  final String language;
  final String? transcript;
}

class BrowserVoiceBridge {
  final FlutterTts _tts = FlutterTts();
  final AudioPlayer _player = AudioPlayer();
  final AudioRecorder _recorder = AudioRecorder();

  static const int _sampleRate = 16000;
  static const int _channels = 1;
  static const double _speechThreshold = 0.010;
  static const double _softSpeechThreshold = 0.0055;
  static const int _rollingNoiseWindowMs = 3200;
  static const double _speechStartRatio = 1.8;
  static const double _speechEndRatio = 1.25;
  static const int _minSpeechMs = 360;
  static const int _endSilenceMs = 850;
  static const int _maxTurnMs = 18000;
  static const double _minSpeechAmplitude = 0.007;
  static const Duration _maxTurnLength = Duration(milliseconds: _maxTurnMs);
  static const Duration _minTurnLength = Duration(milliseconds: _minSpeechMs);
  static const Duration _silenceWindow = Duration(milliseconds: _endSilenceMs);
  static const Duration _speechBootstrapWindow = Duration(milliseconds: 1200);
  static const Duration _noSpeechWindow = Duration(seconds: 8);
  static const int _preSpeechChunkLimit = 8;

  bool _initialized = false;
  String? _resolvedTtsLanguage;

  bool get isSpeechRecognitionSupported => true;

  Future<void> primeVoiceExperience() async {
    await _ensureInitialized('bg-BG');
    await _ensureMicrophonePermission();
    try {
      await _recorder.stop();
    } catch (_) {}
    await _tts.stop();
    await _player.stop();
  }

  Future<CapturedAudioTurn> captureAudioTurn({
    String language = 'bg-BG',
  }) async {
    await _ensureInitialized(language);
    if (!await _ensureMicrophonePermission()) {
      throw StateError('Microphone permission was not granted.');
    }

    try {
      await _recorder.stop();
    } catch (_) {}
    await _player.stop();
    await _tts.stop();

    final stream = await _recorder.startStream(
      const RecordConfig(
        encoder: AudioEncoder.pcm16bits,
        sampleRate: _sampleRate,
        numChannels: _channels,
        echoCancel: true,
        noiseSuppress: true,
      ),
    );

    final completer = Completer<CapturedAudioTurn>();
    final preSpeechChunks = ListQueue<Uint8List>();
    final turnChunks = <Uint8List>[];
    var speechDetected = false;
    DateTime? speechStartedAt;
    DateTime? lastVoiceAt;
    DateTime? belowVoiceSince;
    final listeningStartedAt = DateTime.now();
    final noiseFloor = _RollingNoiseFloor(
      windowMs: _rollingNoiseWindowMs,
      fallbackLevel: _minSpeechAmplitude / _speechStartRatio,
    );
    StreamSubscription<Uint8List>? sub;

    Future<void> finishCapture() async {
      if (completer.isCompleted) {
        return;
      }

      await sub?.cancel();
      sub = null;
      try {
        await _recorder.stop();
      } catch (_) {}

      final pcmBytes = _joinChunks(turnChunks);
      if (speechStartedAt == null ||
          DateTime.now().difference(speechStartedAt!) < _minTurnLength ||
          pcmBytes.isEmpty) {
        completer.completeError(StateError('No speech was captured.'));
        return;
      }

      final wavBytes = _wrapPcmAsWav(
        pcmBytes,
        sampleRate: _sampleRate,
        channels: _channels,
      );
      completer.complete(
        CapturedAudioTurn(
          audioBase64: base64Encode(wavBytes),
          mimeType: 'audio/wav',
          language: language,
        ),
      );
    }

    sub = stream.listen(
      (chunk) {
        if (completer.isCompleted) {
          return;
        }

        final level = _pcmLevel(chunk);
        final now = DateTime.now();
        final floor = noiseFloor.level;
        final speechStartLevel = math.max(
          _minSpeechAmplitude,
          floor * _speechStartRatio,
        );
        final speechEndLevel = math.max(
          _softSpeechThreshold,
          floor * _speechEndRatio,
        );

        if (speechDetected) {
          turnChunks.add(chunk);
          if (level >= speechEndLevel || level >= _speechThreshold) {
            lastVoiceAt = now;
            belowVoiceSince = null;
          } else {
            belowVoiceSince ??= now;
          }

          if (speechStartedAt != null &&
              now.difference(speechStartedAt!) >= _maxTurnLength) {
            unawaited(finishCapture());
          } else if (belowVoiceSince != null &&
              now.difference(belowVoiceSince!) >= _silenceWindow) {
            unawaited(finishCapture());
          } else if (lastVoiceAt != null &&
              now.difference(lastVoiceAt!) >= _silenceWindow * 3) {
            unawaited(finishCapture());
          }
          return;
        }

        preSpeechChunks.add(chunk);
        while (preSpeechChunks.length > _preSpeechChunkLimit) {
          preSpeechChunks.removeFirst();
        }

        final timeListening = now.difference(listeningStartedAt);
        final crossedSpeechThreshold =
            (level > speechStartLevel && level >= _minSpeechAmplitude) ||
            (timeListening >= _speechBootstrapWindow &&
                noiseFloor.sampleCount < 4 &&
                level >= _speechThreshold);

        if (crossedSpeechThreshold) {
          speechDetected = true;
          speechStartedAt = now;
          lastVoiceAt = now;
          belowVoiceSince = null;
          turnChunks
            ..clear()
            ..addAll(preSpeechChunks)
            ..add(chunk);
          preSpeechChunks.clear();
        } else if (timeListening >= _noSpeechWindow) {
          unawaited(finishCapture());
        } else {
          noiseFloor.add(level, now);
        }
      },
      onError: (Object error, StackTrace stackTrace) async {
        if (!completer.isCompleted) {
          await sub?.cancel();
          sub = null;
          try {
            await _recorder.stop();
          } catch (_) {}
          completer.completeError(StateError(error.toString()));
        }
      },
      cancelOnError: true,
    );

    return completer.future.timeout(
      const Duration(seconds: 24),
      onTimeout: () async {
        await sub?.cancel();
        try {
          await _recorder.stop();
        } catch (_) {}
        throw TimeoutException('Timed out while listening for speech.');
      },
    );
  }

  Future<void> playBase64Audio({
    required String audioBase64,
    required String mimeType,
  }) async {
    final clean = audioBase64.trim();
    if (clean.isEmpty) {
      return;
    }

    await _ensureInitialized('bg-BG');
    await _tts.stop();
    await _player.stop();
    await _player.play(BytesSource(base64Decode(clean), mimeType: mimeType));
    await _waitForPlaybackToFinish();
  }

  Future<void> playText(String text) async {
    final clean = text.trim();
    if (clean.isEmpty) {
      return;
    }
    await _ensureInitialized('bg-BG');
    await _player.stop();
    await _tts.stop();
    await _tts.speak(clean);
  }

  void stopRecognition() {
    unawaited(_recorder.stop());
  }

  void stopAudio() {
    unawaited(_tts.stop());
    unawaited(_player.stop());
  }

  Future<void> _ensureInitialized(String preferredLanguage) async {
    if (_initialized) {
      return;
    }
    await _tts.awaitSpeakCompletion(true);
    await _tts.setVolume(1.0);
    await _tts.setSpeechRate(0.42);
    await _tts.setPitch(1.0);
    await _tts.setQueueMode(0);
    try {
      await _tts.setAudioAttributesForNavigation();
    } catch (_) {}
    await _player.setReleaseMode(ReleaseMode.stop);
    await _player.setAudioContext(
      AudioContext(
        android: const AudioContextAndroid(
          isSpeakerphoneOn: true,
          contentType: AndroidContentType.speech,
          usageType: AndroidUsageType.assistant,
          audioFocus: AndroidAudioFocus.gainTransientMayDuck,
        ),
        iOS: AudioContextIOS(
          category: AVAudioSessionCategory.playAndRecord,
          options: const {
            AVAudioSessionOptions.defaultToSpeaker,
            AVAudioSessionOptions.allowBluetooth,
            AVAudioSessionOptions.allowBluetoothA2DP,
            AVAudioSessionOptions.mixWithOthers,
          },
        ),
      ),
    );
    _resolvedTtsLanguage = await _resolveTtsLanguage(preferredLanguage);
    await _tts.setLanguage(_resolvedTtsLanguage!);
    _initialized = true;
  }

  Future<bool> _ensureMicrophonePermission() async {
    if (await _recorder.hasPermission()) {
      return true;
    }
    final status = await Permission.microphone.request();
    if (status.isGranted) {
      return await _recorder.hasPermission();
    }
    return false;
  }

  Future<String> _resolveTtsLanguage(String preferredLanguage) async {
    final wantsBulgarian = preferredLanguage.toLowerCase().startsWith('bg');
    final candidates = wantsBulgarian
        ? const ['bg-BG', 'bg_BG', 'bg', 'en-US']
        : const ['en-US', 'en_US', 'en'];

    for (final candidate in candidates) {
      try {
        final available = await _tts.isLanguageAvailable(candidate);
        if (available == true) {
          return candidate;
        }
      } catch (_) {}
    }

    return wantsBulgarian ? 'bg-BG' : 'en-US';
  }

  Future<void> _waitForPlaybackToFinish() async {
    try {
      await Future.any<void>([
        _player.onPlayerComplete.first,
        _player.onPlayerStateChanged.firstWhere(
          (state) => state != PlayerState.playing,
        ),
      ]).timeout(const Duration(seconds: 12));
    } on TimeoutException {
      // Some Android devices do not emit a reliable completion signal for
      // bytes playback, so do not block the next turn indefinitely.
    }
  }

  Uint8List _joinChunks(List<Uint8List> chunks) {
    final builder = BytesBuilder(copy: false);
    for (final chunk in chunks) {
      builder.add(chunk);
    }
    return builder.takeBytes();
  }

  double _pcmLevel(Uint8List bytes) {
    if (bytes.length < 2) {
      return 0;
    }

    var maxAmplitude = 0.0;
    var sumSquares = 0.0;
    var sampleCount = 0;
    for (var i = 0; i + 1 < bytes.length; i += 2) {
      final sample = bytes[i] | (bytes[i + 1] << 8);
      final signed = sample >= 0x8000 ? sample - 0x10000 : sample;
      final normalized = signed / 32768.0;
      final amplitude = normalized.abs();
      if (amplitude > maxAmplitude) {
        maxAmplitude = amplitude;
      }
      sumSquares += normalized * normalized;
      sampleCount += 1;
    }
    if (sampleCount == 0) {
      return 0;
    }
    final rms = math.sqrt(sumSquares / sampleCount);
    return math.max(rms, maxAmplitude * 0.55);
  }

  Uint8List _wrapPcmAsWav(
    Uint8List pcmBytes, {
    required int sampleRate,
    required int channels,
  }) {
    final byteRate = sampleRate * channels * 2;
    final blockAlign = channels * 2;
    final dataLength = pcmBytes.length;
    final totalLength = 44 + dataLength;
    final out = ByteData(totalLength);

    void writeAscii(int offset, String value) {
      for (var i = 0; i < value.length; i += 1) {
        out.setUint8(offset + i, value.codeUnitAt(i));
      }
    }

    writeAscii(0, 'RIFF');
    out.setUint32(4, totalLength - 8, Endian.little);
    writeAscii(8, 'WAVE');
    writeAscii(12, 'fmt ');
    out.setUint32(16, 16, Endian.little);
    out.setUint16(20, 1, Endian.little);
    out.setUint16(22, channels, Endian.little);
    out.setUint32(24, sampleRate, Endian.little);
    out.setUint32(28, byteRate, Endian.little);
    out.setUint16(32, blockAlign, Endian.little);
    out.setUint16(34, 16, Endian.little);
    writeAscii(36, 'data');
    out.setUint32(40, dataLength, Endian.little);

    final wavBytes = out.buffer.asUint8List();
    wavBytes.setRange(44, totalLength, pcmBytes);
    return wavBytes;
  }
}

class _RollingNoiseFloor {
  _RollingNoiseFloor({
    required this.windowMs,
    required this.fallbackLevel,
  });

  final int windowMs;
  final double fallbackLevel;
  final ListQueue<_NoiseFrame> _frames = ListQueue<_NoiseFrame>();
  double _sum = 0;

  int get sampleCount => _frames.length;

  double get level {
    if (_frames.isEmpty) {
      return fallbackLevel;
    }
    return math.max(fallbackLevel, _sum / _frames.length);
  }

  void add(double value, DateTime at) {
    _frames.add(_NoiseFrame(value, at));
    _sum += value;
    final cutoff = at.subtract(Duration(milliseconds: windowMs));
    while (_frames.isNotEmpty && _frames.first.at.isBefore(cutoff)) {
      _sum -= _frames.removeFirst().value;
    }
  }
}

class _NoiseFrame {
  const _NoiseFrame(this.value, this.at);

  final double value;
  final DateTime at;
}

BrowserVoiceBridge createBrowserVoiceBridge() => BrowserVoiceBridge();
