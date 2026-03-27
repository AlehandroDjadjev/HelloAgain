import 'package:flutter/foundation.dart';
import 'package:flutter/services.dart';

class BackgroundVoiceService {
  const BackgroundVoiceService();

  static const _channel = MethodChannel('com.example.frontend/voice_service');

  bool get isSupported =>
      !kIsWeb && defaultTargetPlatform == TargetPlatform.android;

  Future<void> start() async {
    if (!isSupported) {
      return;
    }
    try {
      await _channel.invokeMethod<void>('start');
    } on PlatformException {
      // The agent can still run in the foreground even if the notification
      // service could not be started on this device.
    }
  }

  Future<void> stop() async {
    if (!isSupported) {
      return;
    }
    try {
      await _channel.invokeMethod<void>('stop');
    } on PlatformException {
      // Ignore stop failures during teardown.
    }
  }
}
