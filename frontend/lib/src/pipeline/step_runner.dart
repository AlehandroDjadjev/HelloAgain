import 'dart:async';

import 'package:android_control_plugin/android_control_plugin.dart';

import '../api/agent_client.dart';
import 'pipeline_state.dart';

/// Drives the backend-directed execution loop.
class StepRunner {
  StepRunner({
    required this.client,
    required this.gateway,
    required this.sessionId,
    required this.expectedPackage,
    required this.onStepStarted,
    required this.onStepCompleted,
    required this.onLog,
    required this.onConfirmation,
    required this.onComplete,
    required this.onAbort,
    required this.onManualTakeover,
    required this.onUnexpectedAppChange,
  });

  final AgentClient client;
  final DeviceControlChannel gateway;
  final String sessionId;
  final String expectedPackage;

  final void Function(StepEntry step) onStepStarted;
  final void Function(String stepId, ActionResult result) onStepCompleted;
  final void Function(String message, LogLevel level) onLog;
  final Future<void> Function(Map<String, dynamic> confirmAction)
  onConfirmation;
  final void Function() onComplete;
  final void Function(String reason) onAbort;
  final void Function(String reason) onManualTakeover;
  final void Function(String? actualPackage) onUnexpectedAppChange;

  static const _sessionTimeout = Duration(minutes: 5);
  static const _pollInterval = Duration(milliseconds: 300);
  static const _networkRetryDelay = Duration(seconds: 2);
  static const _maxNetworkRetries = 3;
  static const _defaultStepTimeout = Duration(seconds: 10);

  final _retryCounts = <String, int>{};
  DateTime? _sessionStart;
  bool _cancelled = false;

  void cancel() => _cancelled = true;

  Future<void> runLoop() async {
    _sessionStart ??= DateTime.now();
    _cancelled = false;

    while (!_cancelled) {
      if (DateTime.now().difference(_sessionStart!) > _sessionTimeout) {
        _log(
          'Session timeout (${_sessionTimeout.inMinutes} min exceeded).',
          LogLevel.error,
        );
        onAbort('Session timeout');
        return;
      }

      final screenState = await _safeGetScreenState();

      if (screenState != null && expectedPackage.isNotEmpty) {
        final fg = screenState['foreground_package'] as String?;
        if (fg != null && fg.isNotEmpty && fg != expectedPackage) {
          onUnexpectedAppChange(fg);
          _log(
            'Unexpected foreground: $fg (expected $expectedPackage)',
            LogLevel.warning,
          );
        }
      }

      Map<String, dynamic> resp;
      try {
        resp = await _withNetworkRetry(
          () => client.getNextStep(sessionId, screenState: screenState),
        );
      } catch (e) {
        _log('Backend unreachable: $e', LogLevel.error);
        onAbort('Network error: $e');
        return;
      }

      final backendStatus = resp['status'] as String? ?? 'execute';
      final action = (resp['next_action'] as Map?)?.cast<String, dynamic>();
      final reason = resp['reason'] as String? ?? '';
      final reasoning = resp['reasoning'] as String? ?? '';

      switch (backendStatus) {
        case 'complete':
          if (reasoning.isNotEmpty) {
            _log('Reasoning: $reasoning', LogLevel.info);
          }
          _log('All steps complete.', LogLevel.success);
          onComplete();
          return;

        case 'abort':
          if (reasoning.isNotEmpty) {
            _log('Reasoning: $reasoning', LogLevel.warning);
          }
          _log('Backend aborted: $reason', LogLevel.error);
          onAbort(reason.isNotEmpty ? reason : 'Execution aborted by backend');
          return;

        case 'manual_takeover':
          if (reasoning.isNotEmpty) {
            _log('Reasoning: $reasoning', LogLevel.warning);
          }
          _log('Manual takeover required: $reason', LogLevel.warning);
          onManualTakeover(reason);
          return;

        case 'confirm':
          if (action != null) {
            if (reasoning.isNotEmpty) {
              final params =
                  (action['params'] as Map?)?.cast<String, dynamic>() ?? {};
              params.putIfAbsent('content_preview', () => reasoning);
              action['params'] = params;
              _log('Reasoning: $reasoning', LogLevel.info);
            }
            _log('Confirmation required for ${action['id']}', LogLevel.warning);
            await onConfirmation(action);
            return;
          }
          await Future.delayed(_pollInterval);
          break;

        case 'retry':
          final stepId = action?['id'] as String? ?? '';
          final count = (_retryCounts[stepId] ?? 0) + 1;
          _retryCounts[stepId] = count;
          if (reasoning.isNotEmpty) {
            _log('Reasoning: $reasoning', LogLevel.info);
          }
          _log(
            'Backend says retry${reason.isNotEmpty ? ': $reason' : ''} (client count: $count)',
            LogLevel.warning,
          );
          if (count > 5) {
            onAbort('Client-side retry limit exceeded for step "$stepId"');
            return;
          }
          await Future.delayed(_pollInterval);
          break;

        case 'execute':
        default:
          if (action == null) {
            onComplete();
            return;
          }
          final done = await _executeStep(action, reasoning);
          if (!done) return;
      }
    }
  }

  Future<bool> _executeStep(
    Map<String, dynamic> action,
    String reasoning,
  ) async {
    final stepId = action['id'] as String;
    final stepType = action['type'] as String;
    final params = (action['params'] as Map?)?.cast<String, dynamic>() ?? {};
    final timeoutMs =
        (action['timeout_ms'] as int?) ?? _defaultStepTimeout.inMilliseconds;

    final step = StepEntry(
      id: stepId,
      type: stepType,
      label: _stepLabel(stepType, reasoning),
      reasoning: reasoning,
      status: StepStatus.running,
    );
    onStepStarted(step);

    _log('[$stepId] $stepType timeout=${timeoutMs}ms', LogLevel.info);
    if (reasoning.isNotEmpty) {
      _log('Reasoning: $reasoning', LogLevel.info);
    }

    final stopwatch = Stopwatch()..start();
    ActionResult result;

    try {
      result = await _dispatchWithTimeout(stepType, params, timeoutMs);
    } catch (e) {
      result = ActionResult(
        success: false,
        code: 'EXCEPTION',
        message: e.toString(),
      );
    }

    stopwatch.stop();
    onStepCompleted(stepId, result);

    final screenAfter = await _safeGetScreenState();

    _log(
      '[$stepId] ${result.success ? 'OK' : 'FAIL'} '
      'code=${result.code.isEmpty ? 'OK' : result.code} '
      '${stopwatch.elapsedMilliseconds}ms',
      result.success ? LogLevel.success : LogLevel.error,
    );
    if ((result.message ?? '').trim().isNotEmpty) {
      _log('[$stepId] message=${result.message}', LogLevel.error);
    }

    Map<String, dynamic> decision;
    try {
      decision = await _withNetworkRetry(
        () => client.postActionResult(
          sessionId,
          actionId: stepId,
          success: result.success,
          code: result.code,
          message: result.message ?? '',
          screenState: screenAfter,
          durationMs: stopwatch.elapsedMilliseconds,
          actionType: stepType,
          reasoning: reasoning,
          screenshotBase64: result.screenshotBase64,
        ),
      );
    } catch (e) {
      _log('Failed to post result for $stepId: $e', LogLevel.error);
      onAbort('Network error posting result: $e');
      return false;
    }

    if (result.success) {
      _retryCounts.remove(stepId);
    }

    final decisionStatus = decision['status'] as String? ?? 'continue';
    final decisionReason = decision['reason'] as String? ?? '';
    final decisionReasoning = decision['reasoning'] as String? ?? '';
    if (decisionReasoning.isNotEmpty) {
      _log('Post-step reasoning: $decisionReasoning', LogLevel.info);
    }
    _log(
      decisionReason.isNotEmpty
          ? 'Decision: $decisionStatus ($decisionReason)'
          : 'Decision: $decisionStatus',
      decisionStatus == 'abort' ? LogLevel.error : LogLevel.info,
    );

    if (decisionStatus == 'abort') {
      onAbort(
        decisionReason.isNotEmpty
            ? decisionReason
            : (result.message?.trim().isNotEmpty ?? false)
            ? result.message!.trim()
            : 'Backend aborted after result (code=${result.code})',
      );
      return false;
    }

    if (decisionStatus == 'manual_takeover') {
      onManualTakeover(
        decisionReason.isNotEmpty
            ? decisionReason
            : 'Manual takeover required after result (code=${result.code})',
      );
      return false;
    }

    if (decisionStatus == 'complete') {
      _log('All steps complete.', LogLevel.success);
      onComplete();
      return false;
    }

    return true;
  }

  Future<ActionResult> _dispatchWithTimeout(
    String type,
    Map<String, dynamic> params,
    int timeoutMs,
  ) {
    final future = _dispatch(type, params, timeoutMs);
    return future.timeout(
      Duration(milliseconds: timeoutMs),
      onTimeout: () => ActionResult(
        success: false,
        code: 'TIMEOUT',
        message: '$type timed out after ${timeoutMs}ms',
      ),
    );
  }

  Future<ActionResult> _dispatch(
    String type,
    Map<String, dynamic> params,
    int timeoutMs,
  ) async {
    switch (type) {
      case 'OPEN_APP':
        return gateway.launchApp(
          params['package_name'] as String? ??
              params['package'] as String? ??
              '',
        );

      case 'WAIT_FOR_APP':
        return _waitForApp(
          params['package_name'] as String? ??
              params['package'] as String? ??
              '',
          Duration(milliseconds: timeoutMs),
        );

      case 'WAIT_FOR_ELEMENT':
        return _waitForElement(
          _selectorFromParams(params),
          Duration(milliseconds: timeoutMs),
        );

      case 'GET_SCREEN_STATE':
        await gateway.getScreenState();
        return ActionResult(success: true, code: 'OK');

      case 'FIND_ELEMENT':
        final candidates = _selectorCandidates(params);
        if (candidates.isNotEmpty) {
          return _findWithFallback(candidates);
        }
        final node = await gateway.findElement(_selectorFromParams(params));
        return ActionResult(
          success: node != null,
          code: node != null ? 'OK' : 'ELEMENT_NOT_FOUND',
        );

      case 'TAP_ELEMENT':
        final tapCandidates = _selectorCandidates(params);
        if (tapCandidates.isNotEmpty) {
          return _tapWithFallback(tapCandidates);
        }
        return gateway.tapElement(_selectorFromParams(params));

      case 'LONG_PRESS_ELEMENT':
        final longPressCandidates = _selectorCandidates(params);
        if (longPressCandidates.isNotEmpty) {
          return _longPressWithFallback(longPressCandidates);
        }
        return gateway.longPressElement(_selectorFromParams(params));

      case 'FOCUS_ELEMENT':
        final focusCandidates = _selectorCandidates(params);
        if (focusCandidates.isNotEmpty) {
          return _focusWithFallback(focusCandidates);
        }
        return gateway.focusElement(_selectorFromParams(params));

      case 'TYPE_TEXT':
        return gateway.typeText(params['text'] as String? ?? '');

      case 'CLEAR_TEXT':
        return gateway.clearFocusedField();

      case 'SCROLL':
        return gateway.scroll(params['direction'] as String? ?? 'down');

      case 'SWIPE':
        return gateway.swipe(
          (params['start_x'] as num?)?.toInt() ?? 500,
          (params['start_y'] as num?)?.toInt() ?? 1000,
          (params['end_x'] as num?)?.toInt() ?? 500,
          (params['end_y'] as num?)?.toInt() ?? 300,
          (params['duration_ms'] as num?)?.toInt() ?? 300,
        );

      case 'BACK':
        return gateway.goBack();

      case 'HOME':
        return gateway.goHome();

      case 'ASSERT_SCREEN':
        final state = await gateway.getScreenState();
        final expectedPkg = params['foreground_package'] as String?;
        if (expectedPkg != null && state.foregroundPackage != expectedPkg) {
          return ActionResult(
            success: false,
            code: 'SCREEN_MISMATCH',
            message: 'Expected $expectedPkg, got ${state.foregroundPackage}',
          );
        }
        return ActionResult(success: true, code: 'OK');

      case 'ASSERT_ELEMENT':
        final assertCandidates = _selectorCandidates(params);
        final node = assertCandidates.isNotEmpty
            ? await _findAnyCandidate(assertCandidates)
            : await gateway.findElement(_selectorFromParams(params));
        return ActionResult(
          success: node != null,
          code: node != null ? 'OK' : 'ELEMENT_NOT_FOUND',
          message: node == null ? 'Element not found' : null,
        );

      case 'ABORT':
        return ActionResult(
          success: false,
          code: 'ABORTED',
          message: params['reason'] as String? ?? 'ABORT step reached',
        );

      case 'GET_SCREENSHOT':
        // SCREENSHOT_UNAVAILABLE is a soft, non-fatal outcome: the device
        // doesn't support screenshots (API < 30) or the capture timed out.
        // The backend treats it as "continue" so the LLM can fall back to
        // accessibility-only reasoning instead of aborting.
        final screenshotB64 = await gateway.takeScreenshot();
        return ActionResult(
          success: screenshotB64 != null,
          code: screenshotB64 != null ? 'OK' : 'SCREENSHOT_UNAVAILABLE',
          screenshotBase64: screenshotB64,
        );

      case 'TAP_COORDINATES':
        // Single-point gesture with a short duration is equivalent to a tap.
        final x = (params['x'] as num?)?.toInt() ?? 540;
        final y = (params['y'] as num?)?.toInt() ?? 960;
        return gateway.swipe(x, y, x, y, 50);

      default:
        return ActionResult(
          success: false,
          code: 'UNKNOWN_ACTION_TYPE',
          message: 'Unhandled action type: $type',
        );
    }
  }

  List<Map<String, dynamic>> _selectorCandidates(Map<String, dynamic> params) {
    final raw = params['selector_candidates'];
    if (raw is! List) return const [];
    return raw.map((e) => (e as Map).cast<String, dynamic>()).toList();
  }

  Future<ActionResult> _tapWithFallback(
    List<Map<String, dynamic>> candidates,
  ) async {
    final tried = <String>[];
    for (final candidate in candidates) {
      tried.add(_candidateLabel(candidate));
      try {
        final result = await gateway.tapElement(_selectorFromMap(candidate));
        if (result.success) return result;
      } catch (_) {}
    }
    return _allSelectorsFailed('TAP', tried);
  }

  Future<ActionResult> _longPressWithFallback(
    List<Map<String, dynamic>> candidates,
  ) async {
    final tried = <String>[];
    for (final candidate in candidates) {
      tried.add(_candidateLabel(candidate));
      try {
        final result = await gateway.longPressElement(
          _selectorFromMap(candidate),
        );
        if (result.success) return result;
      } catch (_) {}
    }
    return _allSelectorsFailed('LONG_PRESS', tried);
  }

  Future<ActionResult> _focusWithFallback(
    List<Map<String, dynamic>> candidates,
  ) async {
    final tried = <String>[];
    for (final candidate in candidates) {
      tried.add(_candidateLabel(candidate));
      try {
        final result = await gateway.focusElement(_selectorFromMap(candidate));
        if (result.success) return result;
      } catch (_) {}
    }
    return _allSelectorsFailed('FOCUS', tried);
  }

  Future<ActionResult> _findWithFallback(
    List<Map<String, dynamic>> candidates,
  ) async {
    final node = await _findAnyCandidate(candidates);
    if (node != null) return ActionResult(success: true, code: 'OK');
    return _allSelectorsFailed(
      'FIND',
      candidates.map(_candidateLabel).toList(),
    );
  }

  Future<UiNode?> _findAnyCandidate(
    List<Map<String, dynamic>> candidates,
  ) async {
    for (final candidate in candidates) {
      try {
        final node = await gateway.findElement(_selectorFromMap(candidate));
        if (node != null) return node;
      } catch (_) {}
    }
    return null;
  }

  ActionResult _allSelectorsFailed(
    String action,
    List<String> tried,
  ) => ActionResult(
    success: false,
    code: 'ALL_SELECTORS_FAILED',
    message:
        '$action: all ${tried.length} selectors failed. Tried: ${tried.join(' | ')}',
  );

  Selector _selectorFromMap(Map<String, dynamic> sel) => Selector(
    elementRef: _stringValue(sel, 'element_ref', 'elementRef', 'ref'),
    viewId: _stringValue(sel, 'view_id', 'viewId'),
    textEquals: _stringValue(sel, 'text', 'textEquals'),
    textContains: _stringValue(sel, 'text_contains', 'textContains'),
    contentDescEquals: _stringValue(
      sel,
      'content_desc',
      'contentDesc',
      'contentDescEquals',
    ),
    contentDescContains: _stringValue(
      sel,
      'content_desc_contains',
      'contentDescContains',
    ),
    className: _stringValue(sel, 'class_name', 'className'),
    packageName: _stringValue(sel, 'package_name', 'packageName'),
    clickable: _boolValue(sel, 'clickable'),
    enabled: _boolValue(sel, 'enabled'),
    focused: _boolValue(sel, 'focused'),
    indexInParent: _intValue(sel, 'index_in_parent', 'indexInParent'),
  );

  String _candidateLabel(Map<String, dynamic> sel) {
    final parts = <String>[];
    for (final k in const [
      'element_ref',
      'elementRef',
      'ref',
      'view_id',
      'viewId',
      'content_desc',
      'contentDesc',
      'contentDescEquals',
      'content_desc_contains',
      'contentDescContains',
      'text',
      'textEquals',
      'text_contains',
      'textContains',
      'class_name',
      'className',
    ]) {
      if (sel[k] != null) parts.add('$k=${sel[k]}');
    }
    return parts.isEmpty ? sel.toString() : parts.join(', ');
  }

  Future<ActionResult> _waitForApp(String pkg, Duration timeout) async {
    if (pkg.isEmpty) {
      return ActionResult(
        success: false,
        code: 'MISSING_PARAM',
        message: 'package not specified',
      );
    }
    final deadline = DateTime.now().add(timeout);
    while (DateTime.now().isBefore(deadline)) {
      final state = await gateway.getScreenState();
      if (state.foregroundPackage == pkg) {
        return ActionResult(success: true, code: 'OK');
      }
      await Future.delayed(_pollInterval);
    }
    return ActionResult(
      success: false,
      code: 'TIMEOUT',
      message: '$pkg not in foreground within ${timeout.inSeconds}s',
    );
  }

  Future<ActionResult> _waitForElement(
    Selector selector,
    Duration timeout,
  ) async {
    final deadline = DateTime.now().add(timeout);
    while (DateTime.now().isBefore(deadline)) {
      final node = await gateway.findElement(selector);
      if (node != null) return ActionResult(success: true, code: 'OK');
      await Future.delayed(_pollInterval);
    }
    return ActionResult(
      success: false,
      code: 'TIMEOUT',
      message: 'Element not found within ${timeout.inSeconds}s',
    );
  }

  Future<T> _withNetworkRetry<T>(Future<T> Function() call) async {
    var attempt = 0;
    while (true) {
      try {
        return await call();
      } catch (e) {
        attempt++;
        if (attempt >= _maxNetworkRetries) rethrow;
        _log(
          'Network error (attempt $attempt/$_maxNetworkRetries): $e',
          LogLevel.warning,
        );
        await Future.delayed(_networkRetryDelay);
      }
    }
  }

  Future<Map<String, dynamic>?> _safeGetScreenState() async {
    try {
      final state = await gateway.getScreenState();
      return state.toJson();
    } catch (_) {
      return null;
    }
  }

  Selector _selectorFromParams(Map<String, dynamic> params) {
    final sel = (params['selector'] as Map?)?.cast<String, dynamic>() ?? params;
    return _selectorFromMap(sel);
  }

  String? _stringValue(Map<String, dynamic> sel, String key, [String? alt1, String? alt2]) {
    for (final candidate in [key, alt1, alt2]) {
      if (candidate == null) continue;
      final value = sel[candidate];
      if (value is String && value.isNotEmpty) return value;
    }
    return null;
  }

  bool? _boolValue(Map<String, dynamic> sel, String key, [String? alt1]) {
    for (final candidate in [key, alt1]) {
      if (candidate == null) continue;
      final value = sel[candidate];
      if (value is bool) return value;
    }
    return null;
  }

  int? _intValue(Map<String, dynamic> sel, String key, [String? alt1]) {
    for (final candidate in [key, alt1]) {
      if (candidate == null) continue;
      final value = sel[candidate];
      if (value is num) return value.toInt();
    }
    return null;
  }

  String _stepLabel(String type, String reasoning) {
    if (reasoning.isEmpty) return type;
    final compact = reasoning.replaceAll(RegExp(r'\s+'), ' ').trim();
    return compact.length <= 72
        ? '$type - $compact'
        : '$type - ${compact.substring(0, 69)}...';
  }

  void _log(String msg, [LogLevel level = LogLevel.info]) {
    onLog(msg, level);
  }
}
