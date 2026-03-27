import 'dart:async';
import 'dart:convert';

import 'package:audioplayers/audioplayers.dart';
import 'package:flutter_tts/flutter_tts.dart';
import 'package:speech_to_text/speech_recognition_result.dart';
import 'package:speech_to_text/speech_to_text.dart';

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
  final SpeechToText _speech = SpeechToText();
  final FlutterTts _tts = FlutterTts();
  final AudioPlayer _player = AudioPlayer();

  bool _initialized = false;
  String _capturedWords = '';

  bool get isSpeechRecognitionSupported => true;

  Future<CapturedAudioTurn> captureAudioTurn({
    String language = 'bg-BG',
  }) async {
    await _ensureInitialized(language);
    _capturedWords = '';

    final completer = Completer<CapturedAudioTurn>();
    late final void Function(String status) onStatus;
    onStatus = (status) {
      if (completer.isCompleted) {
        return;
      }
      if (status == 'done' || status == 'notListening') {
        final transcript = _capturedWords.trim();
        if (transcript.isEmpty) {
          completer.completeError(StateError('No speech was captured.'));
          return;
        }
        completer.complete(
          CapturedAudioTurn(
            audioBase64: '',
            mimeType: 'text/plain',
            language: language,
            transcript: transcript,
          ),
        );
      }
    };

    final ready = await _speech.initialize(
      onStatus: onStatus,
      onError: (error) {
        if (!completer.isCompleted) {
          completer.completeError(StateError(error.errorMsg));
        }
      },
    );
    if (!ready) {
      throw StateError('Speech recognition is unavailable on this device.');
    }

    final localeId = await _resolveLocale(language);
    final started = await _speech.listen(
      onResult: (SpeechRecognitionResult result) {
        _capturedWords = result.recognizedWords;
        if (result.finalResult && !completer.isCompleted) {
          completer.complete(
            CapturedAudioTurn(
              audioBase64: '',
              mimeType: 'text/plain',
              language: localeId,
              transcript: result.recognizedWords.trim(),
            ),
          );
        }
      },
      // ignore: deprecated_member_use
      partialResults: true,
      // ignore: deprecated_member_use
      cancelOnError: false,
      localeId: localeId,
      listenFor: const Duration(seconds: 20),
      pauseFor: const Duration(seconds: 3),
      // ignore: deprecated_member_use
      listenMode: ListenMode.confirmation,
    );
    if (!started) {
      throw StateError('Speech recognition could not start.');
    }

    return completer.future.timeout(
      const Duration(seconds: 24),
      onTimeout: () async {
        await _speech.stop();
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
    await _player.onPlayerComplete.first.timeout(
      const Duration(seconds: 30),
      onTimeout: () {},
    );
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
    unawaited(_speech.stop());
  }

  void stopAudio() {
    unawaited(_tts.stop());
    unawaited(_player.stop());
  }

  Future<void> _ensureInitialized(String preferredLanguage) async {
    if (_initialized) {
      return;
    }
    await _speech.initialize();
    await _tts.awaitSpeakCompletion(true);
    await _tts.setSpeechRate(0.42);
    await _tts.setPitch(1.0);
    await _player.setReleaseMode(ReleaseMode.stop);
    final localeId = await _resolveLocale(preferredLanguage);
    await _tts.setLanguage(
      localeId.toLowerCase().startsWith('bg') ? 'bg-BG' : 'en-US',
    );
    _initialized = true;
  }

  Future<String> _resolveLocale(String preferredLanguage) async {
    final preferred = preferredLanguage.replaceAll('-', '_');
    final locales = await _speech.locales();
    for (final candidate in [preferred, 'bg_BG', 'en_US', 'en_GB']) {
      final match = locales.where((item) => item.localeId == candidate);
      if (match.isNotEmpty) {
        return candidate;
      }
    }
    return preferred;
  }
}

BrowserVoiceBridge createBrowserVoiceBridge() => BrowserVoiceBridge();
