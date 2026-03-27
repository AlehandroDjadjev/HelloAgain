import 'package:flutter/foundation.dart';
import 'package:flutter/services.dart';

class AndroidPhoneNumberHint {
  static const MethodChannel _channel = MethodChannel(
    'hello_again/phone_hint',
  );

  static bool get isSupported =>
      !kIsWeb && defaultTargetPlatform == TargetPlatform.android;

  static Future<String?> requestPhoneNumberHint() async {
    if (!isSupported) {
      return null;
    }

    try {
      final phoneNumber = await _channel.invokeMethod<String>(
        'requestPhoneNumberHint',
      );
      final cleanPhoneNumber = (phoneNumber ?? '').trim();
      return cleanPhoneNumber.isEmpty ? null : cleanPhoneNumber;
    } on MissingPluginException {
      return null;
    }
  }
}
