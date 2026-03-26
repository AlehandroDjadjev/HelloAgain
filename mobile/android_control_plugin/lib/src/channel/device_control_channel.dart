import 'package:flutter/services.dart';

import '../api/android_control_api.dart';
import '../models/models.dart';

/// [MethodChannel] implementation of [AndroidControlApi].
///
/// Every call is wrapped in a try/catch — PlatformExceptions and unexpected
/// errors are converted to [ActionResult.bridgeError] or typed failure results.
/// Nothing in this class ever throws to the caller.
class DeviceControlChannel implements AndroidControlApi {
  static const _channel = MethodChannel('com.example.control/gateway');

  const DeviceControlChannel();

  // ── Permissions ───────────────────────────────────────────────────────────

  @override
  Future<Map<String, bool>> getPermissionStatus() async {
    try {
      final raw = await _channel
          .invokeMethod<Map<Object?, Object?>>('getPermissionStatus');
      return _castBoolMap(raw);
    } on PlatformException {
      return {'accessibilityService': false, 'overlayPermission': false};
    }
  }

  @override
  Future<void> openAccessibilitySettings() async {
    try {
      await _channel.invokeMethod<void>('openAccessibilitySettings');
    } on PlatformException {
      // Non-fatal: user will simply not see the settings screen.
    }
  }

  // ── Session lifecycle ─────────────────────────────────────────────────────

  @override
  Future<ActionResult> startSession(SessionConfig config) =>
      _invokeAction('startSession', config.toMap());

  @override
  Future<ActionResult> stopSession(String sessionId) =>
      _invokeAction('stopSession', {'sessionId': sessionId});

  // ── App inspection ────────────────────────────────────────────────────────

  @override
  Future<bool> isPackageInstalled(String packageName) async {
    try {
      final result = await _channel.invokeMethod<bool>(
        'isPackageInstalled',
        {'packageName': packageName},
      );
      return result ?? false;
    } on PlatformException {
      return false;
    }
  }

  @override
  Future<ActionResult> launchApp(String packageName) =>
      _invokeAction('launchApp', {'packageName': packageName});

  // ── Screen state ──────────────────────────────────────────────────────────

  @override
  Future<ScreenState> getScreenState() async {
    try {
      final raw = await _channel
          .invokeMethod<Map<Object?, Object?>>('getScreenState');
      if (raw == null) {
        return _emptyScreenState();
      }
      return ScreenState.fromMap(raw);
    } on PlatformException {
      return _emptyScreenState(hash: 'error:PlatformException');
    }
  }

  // ── Element lookup ────────────────────────────────────────────────────────

  @override
  Future<UiNode?> findElement(Selector selector) async {
    try {
      final raw = await _channel.invokeMethod<Map<Object?, Object?>>(
        'findElement',
        selector.toMap(),
      );
      return raw != null ? UiNode.fromMap(raw) : null;
    } on PlatformException {
      return null;
    }
  }

  @override
  Future<List<UiNode>> findElements(Selector selector) async {
    try {
      final raw = await _channel.invokeMethod<List<Object?>>(
        'findElements',
        selector.toMap(),
      );
      return (raw ?? [])
          .whereType<Map<Object?, Object?>>()
          .map(UiNode.fromMap)
          .toList();
    } on PlatformException {
      return [];
    }
  }

  // ── Actions ───────────────────────────────────────────────────────────────

  @override
  Future<ActionResult> tapElement(Selector selector) =>
      _invokeAction('tapElement', selector.toMap());

  @override
  Future<ActionResult> longPressElement(Selector selector) =>
      _invokeAction('longPressElement', selector.toMap());

  @override
  Future<ActionResult> focusElement(Selector selector) =>
      _invokeAction('focusElement', selector.toMap());

  @override
  Future<ActionResult> typeText(String text) =>
      _invokeAction('typeText', {'text': text});

  @override
  Future<ActionResult> clearFocusedField() =>
      _invokeAction('clearFocusedField', null);

  @override
  Future<ActionResult> scroll(String direction) =>
      _invokeAction('scroll', {'direction': direction});

  @override
  Future<ActionResult> swipe(
    int startX,
    int startY,
    int endX,
    int endY,
    int durationMs,
  ) =>
      _invokeAction('swipe', {
        'startX': startX,
        'startY': startY,
        'endX': endX,
        'endY': endY,
        'durationMs': durationMs,
      });

  @override
  Future<ActionResult> goBack() => _invokeAction('goBack', null);

  @override
  Future<ActionResult> goHome() => _invokeAction('goHome', null);

  // ── Private helpers ───────────────────────────────────────────────────────

  Future<ActionResult> _invokeAction(
    String method,
    Map<String, Object?>? args,
  ) async {
    try {
      final raw = await _channel
          .invokeMethod<Map<Object?, Object?>>(method, args);
      if (raw == null) {
        return ActionResult.bridgeError('$method returned null');
      }
      return ActionResult.fromMap(raw);
    } on PlatformException catch (e) {
      if (e.code == 'SERVICE_NOT_ENABLED' ||
          e.code == 'SERVICE_UNAVAILABLE') {
        return ActionResult.serviceNotEnabled();
      }
      return ActionResult.bridgeError('${e.code}: ${e.message}');
    } catch (e) {
      return ActionResult.bridgeError(e.toString());
    }
  }

  static Map<String, bool> _castBoolMap(Map<Object?, Object?>? raw) {
    if (raw == null) return {};
    return {
      for (final entry in raw.entries)
        if (entry.key is String) entry.key as String: entry.value as bool,
    };
  }

  static ScreenState _emptyScreenState({String hash = ''}) => ScreenState(
        timestampMs: DateTime.now().millisecondsSinceEpoch,
        screenHash: hash,
        isSensitive: false,
        nodes: [],
      );
}
