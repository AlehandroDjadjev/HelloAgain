import 'package:flutter/services.dart';
import '../events/automation_event.dart';

/// Wraps [EventChannel] to deliver a typed [Stream<AutomationEvent>].
///
/// Usage:
///   AutomationEventChannel.events.listen((event) {
///     switch (event) {
///       case ScreenStateUpdated(:final screenState): ...
///       case SensitiveScreenDetected(): ...
///       ...
///     }
///   });
class AutomationEventChannel {
  static const _eventChannel =
      EventChannel('com.example.control/events');

  static Stream<AutomationEvent>? _broadcast;

  /// A broadcast stream of [AutomationEvent]s from the Android layer.
  /// Unknown event types are silently filtered. Errors are forwarded as
  /// [AutomationError] events (not thrown).
  static Stream<AutomationEvent> get events {
    _broadcast ??= _eventChannel
        .receiveBroadcastStream()
        .where((raw) => raw is Map)
        .map((raw) => AutomationEvent.fromMap(
              (raw as Map).cast<Object?, Object?>(),
            ))
        .where((event) => event != null)
        .cast<AutomationEvent>()
        .asBroadcastStream();
    return _broadcast!;
  }

  /// Convenience filter: only [ScreenStateUpdated] events.
  static Stream<ScreenStateUpdated> get screenStateUpdates =>
      events.whereType<ScreenStateUpdated>();

  /// Convenience filter: only [SensitiveScreenDetected] events.
  static Stream<SensitiveScreenDetected> get sensitiveScreens =>
      events.whereType<SensitiveScreenDetected>();

  /// Convenience filter: only [ConfirmationRequested] events.
  static Stream<ConfirmationRequested> get confirmationRequests =>
      events.whereType<ConfirmationRequested>();
}
