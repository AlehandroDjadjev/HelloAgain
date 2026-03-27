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
  bool get isSpeechRecognitionSupported => false;

  Future<CapturedAudioTurn> captureAudioTurn({
    String language = 'bg-BG',
  }) async {
    throw UnsupportedError(
      'Browser audio capture is only available on Flutter web in Chrome-like browsers.',
    );
  }

  Future<void> playBase64Audio({
    required String audioBase64,
    required String mimeType,
  }) async {}

  Future<void> playText(String text) async {}

  void stopRecognition() {}

  void stopAudio() {}
}

BrowserVoiceBridge createBrowserVoiceBridge() => BrowserVoiceBridge();
