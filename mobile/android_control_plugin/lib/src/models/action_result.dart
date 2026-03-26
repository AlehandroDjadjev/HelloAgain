import 'screen_state.dart';

/// Mirrors Kotlin ActionResultDto.
/// Every device action returns one of these — never throws.
class ActionResult {
  final bool success;

  /// Machine-readable code. "OK" on success; ActionErrorCode value on failure.
  final String code;
  final String? message;
  final ScreenState? updatedScreenState;

  const ActionResult({
    required this.success,
    required this.code,
    this.message,
    this.updatedScreenState,
  });

  factory ActionResult.fromMap(Map<Object?, Object?> map) {
    final rawScreen = map['updatedScreenState'];
    return ActionResult(
      success: map['success'] as bool,
      code: map['code'] as String,
      message: map['message'] as String?,
      updatedScreenState: rawScreen != null
          ? ScreenState.fromMap(rawScreen as Map<Object?, Object?>)
          : null,
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
      };

  bool get isSuccess => success;
  bool get isFailure => !success;

  @override
  String toString() => 'ActionResult(success=$success, code=$code, msg=$message)';
}
