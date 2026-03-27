import 'dart:async';
import 'dart:js_interop';

@JS('helloAgainVoice')
external _HelloAgainVoiceBridge? get _helloAgainVoice;

@JS()
@staticInterop
class _HelloAgainVoiceBridge {}

extension _HelloAgainVoiceBridgeApi on _HelloAgainVoiceBridge {
  external bool isSpeechRecognitionSupported();
  external JSPromise<JSString> startRecognition(JSString language);
  external JSPromise<JSAny?> playBase64Audio(
    JSString audioBase64,
    JSString mimeType,
  );
  external void stopAudio();
}

class BrowserVoiceBridge {
  _HelloAgainVoiceBridge? get _api => _helloAgainVoice;

  bool get isSpeechRecognitionSupported =>
      _api?.isSpeechRecognitionSupported() ?? false;

  Future<String> startRecognition({String language = 'bg-BG'}) async {
    final transcript = await _requireApi()
        .startRecognition(language.toJS)
        .toDart;
    final cleanTranscript = transcript.toDart.trim();
    if (cleanTranscript.isEmpty) {
      throw StateError('No speech was captured.');
    }
    return cleanTranscript;
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
