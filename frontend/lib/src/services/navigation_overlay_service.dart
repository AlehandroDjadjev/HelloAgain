import 'package:flutter/foundation.dart';
import 'package:flutter/services.dart';

class NavigationOverlayService {
  const NavigationOverlayService();

  static const _channel = MethodChannel('hello_again/navigation_overlay');

  bool get isSupported =>
      !kIsWeb && defaultTargetPlatform == TargetPlatform.android;

  Future<bool> hasPermission() async {
    if (!isSupported) {
      return false;
    }
    try {
      final result = await _channel.invokeMethod<bool>('hasPermission');
      return result ?? false;
    } on PlatformException {
      return false;
    }
  }

  Future<void> requestPermission() async {
    if (!isSupported) {
      return;
    }
    try {
      await _channel.invokeMethod<void>('requestPermission');
    } on PlatformException {
      // Ignore permission prompt failures on unsupported devices.
    }
  }

  Future<void> show({
    required String title,
    required String message,
  }) async {
    if (!isSupported) {
      return;
    }
    try {
      await _channel.invokeMethod<void>('show', <String, dynamic>{
        'title': title,
        'message': message,
      });
    } on PlatformException {
      // Ignore overlay failures so phone control can continue.
    }
  }

  Future<void> hide() async {
    if (!isSupported) {
      return;
    }
    try {
      await _channel.invokeMethod<void>('hide');
    } on PlatformException {
      // Ignore stop failures during teardown.
    }
  }

  Future<void> bringToFront() async {
    if (!isSupported) {
      return;
    }
    try {
      await _channel.invokeMethod<void>('bringToFront');
    } on PlatformException {
      // Ignore foregrounding failures so teardown can still continue.
    }
  }
}
