/// Android Control Plugin — Flutter bridge to the Kotlin AccessibilityService.
///
/// Usage:
///
/// ```dart
/// import 'package:android_control_plugin/android_control_plugin.dart';
///
/// // Check permissions first
/// final status = await AndroidControl.permissions.getPermissionStatus();
/// if (status['accessibilityService'] != true) {
///   await AndroidControl.permissions.openAccessibilitySettings();
///   return;
/// }
///
/// // Start a session
/// final result = await AndroidControl.gateway.startSession(SessionConfig(
///   sessionId: 'sess_001',
///   allowedPackages: ['com.whatsapp'],
/// ));
///
/// // Listen for events
/// AndroidControl.events.listen((event) {
///   switch (event) {
///     case ScreenStateUpdated(:final screenState):
///       print('New screen: ${screenState.foregroundPackage}');
///     case SensitiveScreenDetected():
///       print('Sensitive screen — aborting');
///     default: break;
///   }
/// });
///
/// // Execute a step
/// final tapResult = await AndroidControl.gateway.tapElement(
///   Selector.byViewId('com.whatsapp:id/menuitem_search'),
/// );
/// ```
library android_control_plugin;

// Models
export 'src/models/models.dart';

// API surface
export 'src/api/android_control_api.dart';

// MethodChannel implementation
export 'src/channel/device_control_channel.dart';

// EventChannel stream
export 'src/channel/automation_event_channel.dart';

// Event types
export 'src/events/automation_event.dart';

// Permission utilities
export 'src/permissions/permission_checker.dart';

// Facade
export 'src/android_control.dart';
