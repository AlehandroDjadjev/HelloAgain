class BrowserVoiceBridge {
  bool get isSpeechRecognitionSupported => false;

  Future<String> captureSpeechTurn({String language = 'bg-BG'}) async {
    throw UnsupportedError(
      'Browser speech capture is only available on Flutter web in Chrome-like browsers.',
    );
  }

  Future<void> playBase64Audio({
    required String audioBase64,
    required String mimeType,
  }) async {}

  void stopRecognition() {}

  void stopAudio() {}
}

BrowserVoiceBridge createBrowserVoiceBridge() => BrowserVoiceBridge();
