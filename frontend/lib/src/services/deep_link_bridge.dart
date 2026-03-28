import 'dart:async';

import 'package:flutter/services.dart';

class DeepLinkBridge {
  DeepLinkBridge._() {
    _channel.setMethodCallHandler(_handleNativeCall);
  }

  static final DeepLinkBridge instance = DeepLinkBridge._();

  static const MethodChannel _channel = MethodChannel('hello_again/deep_link');

  final StreamController<Uri> _links = StreamController<Uri>.broadcast();

  Stream<Uri> get links => _links.stream;

  Future<Uri?> consumeInitialLink() async {
    final raw = await _channel.invokeMethod<String>('consumeInitialDeepLink');
    return _parseUri(raw);
  }

  Future<void> _handleNativeCall(MethodCall call) async {
    if (call.method != 'onDeepLink') {
      return;
    }
    final uri = _parseUri(call.arguments as String?);
    if (uri != null) {
      _links.add(uri);
    }
  }

  static Uri? _parseUri(String? raw) {
    final value = (raw ?? '').trim();
    if (value.isEmpty) {
      return null;
    }
    return Uri.tryParse(value);
  }
}
