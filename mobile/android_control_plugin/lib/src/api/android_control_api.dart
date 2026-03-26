import '../models/models.dart';

/// Complete surface of the Android device control layer exposed to Flutter.
/// Implemented by [DeviceControlChannel] via MethodChannel.
/// All methods are async and never throw — failures are encoded in [ActionResult].
abstract class AndroidControlApi {
  // ── Permissions ───────────────────────────────────────────────────────────

  /// Returns a map of permission name → granted status.
  /// Keys: "accessibilityService", "overlayPermission".
  /// Safe to call before the AccessibilityService is running.
  Future<Map<String, bool>> getPermissionStatus();

  /// Open the Android Accessibility Settings screen so the user can enable
  /// the service. Safe to call regardless of service state.
  Future<void> openAccessibilitySettings();

  // ── Session lifecycle ─────────────────────────────────────────────────────

  Future<ActionResult> startSession(SessionConfig config);
  Future<ActionResult> stopSession(String sessionId);

  // ── App inspection ────────────────────────────────────────────────────────

  Future<bool> isPackageInstalled(String packageName);
  Future<ActionResult> launchApp(String packageName);

  // ── Screen state ──────────────────────────────────────────────────────────

  Future<ScreenState> getScreenState();

  // ── Element lookup ────────────────────────────────────────────────────────

  Future<UiNode?> findElement(Selector selector);
  Future<List<UiNode>> findElements(Selector selector);

  // ── Actions ───────────────────────────────────────────────────────────────

  Future<ActionResult> tapElement(Selector selector);
  Future<ActionResult> longPressElement(Selector selector);
  Future<ActionResult> focusElement(Selector selector);
  Future<ActionResult> typeText(String text);
  Future<ActionResult> clearFocusedField();
  Future<ActionResult> scroll(String direction);
  Future<ActionResult> swipe(
    int startX,
    int startY,
    int endX,
    int endY,
    int durationMs,
  );
  Future<ActionResult> goBack();
  Future<ActionResult> goHome();
}
