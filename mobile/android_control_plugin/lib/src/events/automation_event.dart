import '../models/models.dart';

/// Base class for all events pushed from Android to Flutter.
sealed class AutomationEvent {
  const AutomationEvent();

  /// Deserialise from a raw MethodChannel / EventChannel map.
  /// Returns null for unknown event types (forward-compat).
  static AutomationEvent? fromMap(Map<Object?, Object?> map) {
    final type = map['type'] as String?;
    return switch (type) {
      'screenStateUpdated' => ScreenStateUpdated.fromMap(map),
      'foregroundAppChanged' => ForegroundAppChanged.fromMap(map),
      'confirmationRequested' => ConfirmationRequested.fromMap(map),
      'actionFailed' => ActionFailed.fromMap(map),
      'sensitiveScreenDetected' => SensitiveScreenDetected.fromMap(map),
      _ => null,
    };
  }
}

// ── Concrete event types ──────────────────────────────────────────────────────

/// Emitted after every accessibility event that results in a new screen snapshot.
final class ScreenStateUpdated extends AutomationEvent {
  final ScreenState screenState;
  const ScreenStateUpdated(this.screenState);

  factory ScreenStateUpdated.fromMap(Map<Object?, Object?> map) =>
      ScreenStateUpdated(
        ScreenState.fromMap(map['screenState'] as Map<Object?, Object?>),
      );
}

/// Emitted when the foreground app changes.
final class ForegroundAppChanged extends AutomationEvent {
  final String? packageName;
  const ForegroundAppChanged(this.packageName);

  factory ForegroundAppChanged.fromMap(Map<Object?, Object?> map) =>
      ForegroundAppChanged(map['packageName'] as String?);
}

/// Emitted when an action requires user confirmation before proceeding.
/// The Flutter layer must show a confirmation dialog and call approve/reject.
final class ConfirmationRequested extends AutomationEvent {
  final String sessionId;
  final String actionId;
  final String prompt;
  const ConfirmationRequested({
    required this.sessionId,
    required this.actionId,
    required this.prompt,
  });

  factory ConfirmationRequested.fromMap(Map<Object?, Object?> map) =>
      ConfirmationRequested(
        sessionId: map['sessionId'] as String,
        actionId: map['actionId'] as String,
        prompt: map['prompt'] as String,
      );
}

/// Emitted when a step fails during execution.
final class ActionFailed extends AutomationEvent {
  final String actionId;
  final String code;
  final String? message;
  const ActionFailed({
    required this.actionId,
    required this.code,
    this.message,
  });

  factory ActionFailed.fromMap(Map<Object?, Object?> map) => ActionFailed(
        actionId: map['actionId'] as String,
        code: map['code'] as String,
        message: map['message'] as String?,
      );
}

/// Emitted when [ScreenState.isSensitive] is true.
/// The executor must abort or pause immediately on receiving this.
final class SensitiveScreenDetected extends AutomationEvent {
  final String sessionId;
  final String foregroundPackage;
  const SensitiveScreenDetected({
    required this.sessionId,
    required this.foregroundPackage,
  });

  factory SensitiveScreenDetected.fromMap(Map<Object?, Object?> map) =>
      SensitiveScreenDetected(
        sessionId: map['sessionId'] as String,
        foregroundPackage: map['foregroundPackage'] as String,
      );
}
