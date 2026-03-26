import 'api/android_control_api.dart';
import 'channel/automation_event_channel.dart';
import 'channel/device_control_channel.dart';
import 'events/automation_event.dart';
import 'permissions/permission_checker.dart';

/// Top-level facade for the Android control plugin.
///
/// Provides singleton access to the gateway, event stream, and permission
/// helpers without requiring consumers to instantiate individual classes.
abstract final class AndroidControl {
  AndroidControl._();

  /// Typed MethodChannel gateway — routes calls to AutomationAccessibilityService.
  static AndroidControlApi get gateway => const DeviceControlChannel();

  /// Broadcast stream of [AutomationEvent]s from the Android layer.
  static Stream<AutomationEvent> get events => AutomationEventChannel.events;

  /// Only [ScreenStateUpdated] events.
  static Stream<ScreenStateUpdated> get screenStateUpdates =>
      AutomationEventChannel.screenStateUpdates;

  /// Only [SensitiveScreenDetected] events.
  static Stream<SensitiveScreenDetected> get sensitiveScreens =>
      AutomationEventChannel.sensitiveScreens;

  /// Only [ConfirmationRequested] events.
  static Stream<ConfirmationRequested> get confirmationRequests =>
      AutomationEventChannel.confirmationRequests;

  // ── Permission helpers (safe before service is running) ───────────────────

  /// Returns {accessibilityService: bool, overlayPermission: bool}.
  static Future<Map<String, bool>> getPermissionStatus() =>
      PermissionChecker.getPermissionStatus();

  /// True if the AccessibilityService is currently enabled.
  static Future<bool> isAccessibilityServiceEnabled() =>
      PermissionChecker.isAccessibilityServiceEnabled();

  /// Navigate the user to Android Accessibility Settings.
  static Future<void> openAccessibilitySettings() =>
      PermissionChecker.openAccessibilitySettings();
}
