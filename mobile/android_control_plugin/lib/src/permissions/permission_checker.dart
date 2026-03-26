import 'package:flutter/services.dart';

/// Utility for querying and navigating to Android permission settings.
/// All methods are static. Safe to call regardless of AccessibilityService state.
class PermissionChecker {
  const PermissionChecker();

  static const _channel = MethodChannel('com.example.control/gateway');

  /// Returns {accessibilityService: bool, overlayPermission: bool}.
  static Future<Map<String, bool>> getPermissionStatus() async {
    try {
      final raw = await _channel.invokeMethod<Map<Object?, Object?>>(
        'getPermissionStatus',
      );
      return _castBoolMap(raw);
    } on PlatformException {
      return {
        'accessibilityService': false,
        'overlayPermission': false,
      };
    }
  }

  /// Returns true if the AccessibilityService is currently running.
  static Future<bool> isAccessibilityServiceEnabled() async {
    final status = await getPermissionStatus();
    return status['accessibilityService'] ?? false;
  }

  /// Opens the Android Accessibility Settings so the user can enable the service.
  static Future<void> openAccessibilitySettings() async {
    try {
      await _channel.invokeMethod<void>('openAccessibilitySettings');
    } on PlatformException {
      // Intent dispatch failures are silent — user sees nothing happen.
    }
  }

  static Map<String, bool> _castBoolMap(Map<Object?, Object?>? raw) {
    if (raw == null) return {};
    return {
      for (final entry in raw.entries)
        if (entry.key is String) entry.key as String: entry.value as bool,
    };
  }
}
