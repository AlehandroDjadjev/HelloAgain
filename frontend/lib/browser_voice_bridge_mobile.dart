import 'dart:async';
import 'dart:convert';
import 'dart:collection';
import 'dart:typed_data';

import 'package:audioplayers/audioplayers.dart';
import 'package:flutter_tts/flutter_tts.dart';
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
  static const double _speechThreshold = 0.045;
  static const Duration _maxTurnLength = Duration(seconds: 18);
  static const Duration _minTurnLength = Duration(milliseconds: 700);
  static const Duration _silenceWindow = Duration(milliseconds: 1800);
  static const int _preSpeechChunkLimit = 8;

  bool _initialized = false;

  bool get isSpeechRecognitionSupported => true;

  Future<CapturedAudioTurn> captureAudioTurn({
    String language = 'bg-BG',
  }) async {
    await _ensureInitialized(language);
    if (!await _recorder.hasPermission()) {
      throw StateError('Microphone permission was not granted.');
    }

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

        if (speechDetected) {
          turnChunks.add(chunk);
          if (level >= _speechThreshold) {
            lastVoiceAt = now;
          }

          if (speechStartedAt != null &&
              now.difference(speechStartedAt!) >= _maxTurnLength) {
            unawaited(finishCapture());
          } else if (lastVoiceAt != null &&
              now.difference(lastVoiceAt!) >= _silenceWindow) {
            unawaited(finishCapture());
          }
          return;
        }

        preSpeechChunks.add(chunk);
        while (preSpeechChunks.length > _preSpeechChunkLimit) {
          preSpeechChunks.removeFirst();
        }

        if (level >= _speechThreshold) {
          speechDetected = true;
          speechStartedAt = now;
          lastVoiceAt = now;
          turnChunks
            ..clear()
            ..addAll(preSpeechChunks)
            ..add(chunk);
          preSpeechChunks.clear();
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
    await _player.play(BytesSource(base64Decode(clean)));
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
    await _tts.setSpeechRate(0.42);
    await _tts.setPitch(1.0);
    await _player.setReleaseMode(ReleaseMode.stop);
    await _tts.setLanguage(
      preferredLanguage.toLowerCase().startsWith('bg') ? 'bg-BG' : 'en-US',
    );
    _initialized = true;
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
    for (var i = 0; i + 1 < bytes.length; i += 2) {
      final sample = bytes[i] | (bytes[i + 1] << 8);
      final signed = sample >= 0x8000 ? sample - 0x10000 : sample;
      final amplitude = signed.abs() / 32768.0;
      if (amplitude > maxAmplitude) {
        maxAmplitude = amplitude;
      }
    }
    return maxAmplitude;
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

BrowserVoiceBridge createBrowserVoiceBridge() => BrowserVoiceBridge();
