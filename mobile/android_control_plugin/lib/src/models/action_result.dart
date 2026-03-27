import 'dart:convert';
import 'dart:typed_data';

import 'screen_state.dart';

/// Mirrors Kotlin ActionResultDto.
/// Every device action returns one of these — never throws.
class ActionResult {
  final bool success;

  /// Machine-readable code. "OK" on success; ActionErrorCode value on failure.
  final String code;
  final String? message;
  final ScreenState? updatedScreenState;

  /// Base64-encoded JPEG captured at the moment of failure.
  /// Non-null only when code is ELEMENT_NOT_FOUND or ELEMENT_NOT_CLICKABLE
  /// and the device is running API 30+.
  final String? screenshotBase64;

  const ActionResult({
    required this.success,
    required this.code,
    this.message,
    this.updatedScreenState,
    this.screenshotBase64,
  });

  factory ActionResult.fromMap(Map<Object?, Object?> map) {
    final rawScreen = map['updatedScreenState'];
    final screenshotBytes = map['screenshotJpeg'] as Uint8List?;
    return ActionResult(
      success: map['success'] as bool,
      code: map['code'] as String,
      message: map['message'] as String?,
      updatedScreenState: rawScreen != null
          ? ScreenState.fromMap(rawScreen as Map<Object?, Object?>)
          : null,
      screenshotBase64:
          screenshotBytes != null ? base64Encode(screenshotBytes) : null,
    );
  }

  /// Used when the bridge itself fails before reaching the Kotlin layer.
  factory ActionResult.bridgeError(String message) => ActionResult(
        success: false,
        code: 'BRIDGE_ERROR',
        message: message,
      );

  factory ActionResult.serviceNotEnabled() => const ActionResult(
        success: false,
        code: 'SERVICE_NOT_ENABLED',
        message:
            'AccessibilityService is not running. Enable it in Android Settings.',
      );

  Map<String, Object?> toMap() => {
        'success': success,
        'code': code,
        'message': message,
        'updatedScreenState': updatedScreenState?.toMap(),
        if (screenshotBase64 != null) 'screenshotBase64': screenshotBase64,
      };

  bool get isSuccess => success;
  bool get isFailure => !success;

  @override
  String toString() => 'ActionResult(success=$success, code=$code, msg=$message)';
}
