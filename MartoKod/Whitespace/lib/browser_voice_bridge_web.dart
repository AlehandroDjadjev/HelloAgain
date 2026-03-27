import 'dart:async';
import 'dart:js_interop';

@JS('helloAgainVoice')
external _HelloAgainVoiceBridge? get _helloAgainVoice;

@JS()
@staticInterop
class _HelloAgainVoiceBridge {}

extension _HelloAgainVoiceBridgeApi on _HelloAgainVoiceBridge {
  external bool isSpeechCaptureSupported();
  external JSPromise<JSString> captureSpeechTurn(JSString language);
  external void stopRecognition();
  external JSPromise<JSAny?> playBase64Audio(
    JSString audioBase64,
    JSString mimeType,
  );
  external void stopAudio();
}

class BrowserVoiceBridge {
  _HelloAgainVoiceBridge? get _api => _helloAgainVoice;

  bool get isSpeechRecognitionSupported =>
      _api?.isSpeechCaptureSupported() ?? false;

  Future<String> captureSpeechTurn({String language = 'bg-BG'}) async {
    final payload = await _requireApi().captureSpeechTurn(language.toJS).toDart;
    final transcript = payload.toDart.trim();
    if (transcript.isEmpty) {
      throw StateError('No speech was captured.');
    }
    return transcript;
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
