import 'dart:async';
import 'dart:convert';
import 'dart:js_interop';

@JS('helloAgainVoice')
external _HelloAgainVoiceBridge? get _helloAgainVoice;

@JS()
@staticInterop
class _HelloAgainVoiceBridge {}

extension _HelloAgainVoiceBridgeApi on _HelloAgainVoiceBridge {
  external bool isSpeechCaptureSupported();
  external JSPromise<JSString> captureAudioTurn(JSString language);
  external void stopRecognition();
  external JSPromise<JSAny?> playBase64Audio(
    JSString audioBase64,
    JSString mimeType,
  );
  external void stopAudio();
}

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
  _HelloAgainVoiceBridge? get _api => _helloAgainVoice;

  bool get isSpeechRecognitionSupported =>
      _api?.isSpeechCaptureSupported() ?? false;

  Future<CapturedAudioTurn> captureAudioTurn({
    String language = 'bg-BG',
  }) async {
    final payload = await _requireApi().captureAudioTurn(language.toJS).toDart;
    final raw = payload.toDart.trim();
    if (raw.isEmpty) {
      throw StateError('No speech was captured.');
    }

    final decoded = jsonDecode(raw);
    if (decoded is! Map) {
      throw const FormatException(
        'Browser voice bridge did not return a valid audio payload.',
      );
    }

    final map = Map<String, dynamic>.from(decoded);
    final audioBase64 = (map['audioBase64'] ?? '').toString().trim();
    final mimeType = (map['mimeType'] ?? 'audio/webm').toString().trim();
    final resolvedLanguage = (map['language'] ?? language).toString().trim();

    if (audioBase64.isEmpty) {
      throw StateError('No speech was captured.');
    }

    return CapturedAudioTurn(
      audioBase64: audioBase64,
      mimeType: mimeType.isEmpty ? 'audio/webm' : mimeType,
      language: resolvedLanguage.isEmpty ? language : resolvedLanguage,
    );
  }

  Future<void> playBase64Audio({
    required String audioBase64,
    required String mimeType,
  }) async {
    if (audioBase64.trim().isEmpty) {
      return;
    }
    await _requireApi().playBase64Audio(audioBase64.toJS, mimeType.toJS).toDart;
  }

  Future<void> playText(String text) async {}

  void stopRecognition() {
    _api?.stopRecognition();
  }

  void stopAudio() {
    _api?.stopAudio();
  }

  _HelloAgainVoiceBridge _requireApi() {
    final api = _api;
    if (api == null) {
      throw StateError('Browser voice bridge is unavailable.');
    }
    return api;
  }
}

BrowserVoiceBridge createBrowserVoiceBridge() => BrowserVoiceBridge();
