import 'dart:async';

import 'package:android_control_plugin/android_control_plugin.dart';

import '../api/agent_client.dart';
import 'pipeline_state.dart';

/// Drives the step-by-step execution loop.
///
/// The [StepRunner] is the only place that directly calls [DeviceControlChannel]
/// and [AgentClient] during execution. [PipelineOrchestrator] instantiates one
/// [StepRunner] per session and delegates [runLoop] to it.
///
/// Resilience notes:
///   - If the network drops, the next [getNextStep] call will throw; the runner
///     logs it and retries after [_networkRetryDelay].
///   - On app resume from background the orchestrator can call [runLoop] again
///     on the same [StepRunner] instance — it resumes from [_completedIds].
///   - Unexpected foreground app changes are detected via screen state and
///     reported to [onUnexpectedAppChange]; the backend will return
///     "retry" / "manual_takeover" accordingly.
class StepRunner {
  StepRunner({
    required this.client,
    required this.gateway,
    required this.sessionId,
    required this.planId,
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
  final String planId;

  /// Package name we expect to be in the foreground during execution
  /// (e.g. "com.whatsapp"). Empty string disables the check.
  final String expectedPackage;

  // ── Callbacks (wired by orchestrator) ────────────────────────────────────

  final void Function(String stepId) onStepStarted;
  final void Function(String stepId, ActionResult result) onStepCompleted;
  final void Function(String message, LogLevel level) onLog;

  /// Called when the backend returns "confirm".
  /// The runner STOPS and waits — the orchestrator must call [continueAfterConfirm].
  final Future<void> Function(Map<String, dynamic> confirmAction) onConfirmation;

  final void Function() onComplete;
  final void Function(String reason) onAbort;
  final void Function(String reason) onManualTakeover;

  /// Fired whenever the foreground package differs from [expectedPackage].
  final void Function(String? actualPackage) onUnexpectedAppChange;

  // ── Limits ────────────────────────────────────────────────────────────────

  static const _sessionTimeout     = Duration(minutes: 5);
  static const _pollInterval        = Duration(milliseconds: 300);
  static const _networkRetryDelay   = Duration(seconds: 2);
  static const _maxNetworkRetries   = 3;
  static const _defaultStepTimeout  = Duration(seconds: 10);

  // ── Mutable execution state (persists across resume) ─────────────────────

  final _completedIds = <String>[];
  Map<String, dynamic>? _lastResult;
  final _retryCounts = <String, int>{};  // client-side retry count
  DateTime? _sessionStart;
  bool _cancelled = false;

  // ── Public API ────────────────────────────────────────────────────────────

  void cancel() => _cancelled = true;

  /// Drives the execution loop until it completes, aborts, or pauses for
  /// confirmation. Safe to call again after a confirmation is approved.
  Future<void> runLoop() async {
    _sessionStart ??= DateTime.now();
    _cancelled = false;

    while (!_cancelled) {
      // ── Session-level timeout ───────────────────────────────────────────
      if (DateTime.now().difference(_sessionStart!) > _sessionTimeout) {
        _log('Session timeout (${_sessionTimeout.inMinutes} min exceeded).', LogLevel.error);
        onAbort('Session timeout');
        return;
      }

      // ── Get fresh screen state ──────────────────────────────────────────
      final screenState = await _safeGetScreenState();

      // Detect unexpected foreground app before asking backend
      if (screenState != null && expectedPackage.isNotEmpty) {
        final fg = screenState['foreground_package'] as String?;
        if (fg != null && fg.isNotEmpty && fg != expectedPackage) {
          onUnexpectedAppChange(fg);
          _log('Unexpected foreground: $fg (expected $expectedPackage)', LogLevel.warning);
          // Still proceed — backend will decide retry vs manual_takeover
        }
      }

      // ── Ask backend for next action ─────────────────────────────────────
      Map<String, dynamic> resp;
      try {
        resp = await _withNetworkRetry(() => client.getNextStep(
          sessionId,
          planId: planId,
          screenState: screenState,
          completedActionIds: List.unmodifiable(_completedIds),
          lastActionResult: _lastResult,
        ));
      } catch (e) {
        _log('Backend unreachable: $e', LogLevel.error);
        onAbort('Network error: $e');
        return;
      }

      final backendStatus = resp['status'] as String? ?? 'execute';
      final action = (resp['next_action'] as Map?)?.cast<String, dynamic>();
      final reason = resp['reason'] as String? ?? '';

      switch (backendStatus) {
        case 'complete':
          _log('All steps complete.', LogLevel.success);
          onComplete();
          return;

        case 'abort':
          _log('Backend aborted: $reason', LogLevel.error);
          onAbort(reason.isNotEmpty ? reason : 'Execution aborted by backend');
          return;

        case 'manual_takeover':
          _log('Manual takeover required: $reason', LogLevel.warning);
          onManualTakeover(reason);
          return;

        case 'confirm':
          if (action != null) {
            _log('Confirmation required for ${action['id']}', LogLevel.warning);
            await onConfirmation(action);
            return; // orchestrator resumes via runLoop() after approval
          }
          // No action provided — treat as a transient backend state; retry
          await Future.delayed(_pollInterval);

        case 'retry':
          final stepId = action?['id'] as String? ?? '';
          final count  = (_retryCounts[stepId] ?? 0) + 1;
          _retryCounts[stepId] = count;
          _log(
            'Backend says retry${reason.isNotEmpty ? ': $reason' : ''} '
            '(client count: $count)',
            LogLevel.warning,
          );
          if (count > 5) {
            onAbort('Client-side retry limit exceeded for step "$stepId"');
            return;
          }
          await Future.delayed(_pollInterval);

        case 'execute':
        default:
          if (action == null) {
            // Shouldn't happen — treat as complete
            onComplete();
            return;
          }
          final done = await _executeStep(action, screenState);
          if (!done) return; // onAbort already called
      }
    }
  }

  // ── Step execution ────────────────────────────────────────────────────────

  /// Returns false if the loop must stop (abort triggered inside).
  Future<bool> _executeStep(
    Map<String, dynamic> action,
    Map<String, dynamic>? screenStateBeforeAction,
  ) async {
    final stepId      = action['id'] as String;
    final stepType    = action['type'] as String;
    final params      = (action['params'] as Map?)?.cast<String, dynamic>() ?? {};
    final timeoutMs   = (action['timeout_ms'] as int?) ?? _defaultStepTimeout.inMilliseconds;

    _log('▶ [$stepId] $stepType  timeout=${timeoutMs}ms');
    onStepStarted(stepId);

    final stopwatch = Stopwatch()..start();
    ActionResult result;

    try {
      result = await _dispatchWithTimeout(stepType, params, timeoutMs);
    } catch (e) {
      result = ActionResult(success: false, code: 'EXCEPTION', message: e.toString());
    }

    stopwatch.stop();
    onStepCompleted(stepId, result);

    // Get screen state after action for the result payload
    final screenAfter = await _safeGetScreenState();

    _log(
      '${result.success ? '✓' : '✗'} [$stepId]  '
      'code=${result.code.isEmpty ? 'OK' : result.code}  '
      '${stopwatch.elapsedMilliseconds}ms',
      result.success ? LogLevel.success : LogLevel.error,
    );

    // Post result to backend
    Map<String, dynamic> decision;
    try {
      decision = await _withNetworkRetry(() => client.postActionResult(
        sessionId,
        planId: planId,
        actionId: stepId,
        success: result.success,
        code: result.code,
        message: result.message ?? '',
        screenState: screenAfter,
        durationMs: stopwatch.elapsedMilliseconds,
      ));
    } catch (e) {
      _log('Failed to post result for $stepId: $e', LogLevel.error);
      onAbort('Network error posting result: $e');
      return false;
    }

    _lastResult = {'success': result.success, 'code': result.code};

    if (result.success) {
      _completedIds.add(stepId);
      // Reset client-side retry counter on success
      _retryCounts.remove(stepId);
    }

    final decisionStatus = decision['status'] as String? ?? 'continue';
    _log('Decision: $decisionStatus');

    if (decisionStatus == 'abort') {
      onAbort('Backend aborted after result (code=${result.code})');
      return false;
    }

    return true;
  }

  // ── Action dispatch ───────────────────────────────────────────────────────

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
        return gateway.launchApp(params['package'] as String? ?? '');

      case 'WAIT_FOR_APP':
        return _waitForApp(
          params['package'] as String? ?? '',
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
        final lpCandidates = _selectorCandidates(params);
        if (lpCandidates.isNotEmpty) {
          return _longPressWithFallback(lpCandidates);
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

      default:
        return ActionResult(
          success: false,
          code: 'UNKNOWN_ACTION_TYPE',
          message: 'Unhandled action type: $type',
        );
    }
  }

  // ── Selector-candidate fallback helpers ───────────────────────────────────

  /// Extract the selector_candidates list from params (empty if not present).
  List<Map<String, dynamic>> _selectorCandidates(Map<String, dynamic> params) {
    final raw = params['selector_candidates'];
    if (raw is! List) return const [];
    return raw.map((e) => (e as Map).cast<String, dynamic>()).toList();
  }

  /// Try each candidate selector for TAP until one succeeds.
  Future<ActionResult> _tapWithFallback(
      List<Map<String, dynamic>> candidates) async {
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
      List<Map<String, dynamic>> candidates) async {
    final tried = <String>[];
    for (final candidate in candidates) {
      tried.add(_candidateLabel(candidate));
      try {
        final result =
            await gateway.longPressElement(_selectorFromMap(candidate));
        if (result.success) return result;
      } catch (_) {}
    }
    return _allSelectorsFailed('LONG_PRESS', tried);
  }

  Future<ActionResult> _focusWithFallback(
      List<Map<String, dynamic>> candidates) async {
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
      List<Map<String, dynamic>> candidates) async {
    final node = await _findAnyCandidate(candidates);
    if (node != null) return ActionResult(success: true, code: 'OK');
    return _allSelectorsFailed('FIND', candidates.map(_candidateLabel).toList());
  }

  Future<UiNode?> _findAnyCandidate(
      List<Map<String, dynamic>> candidates) async {
    for (final candidate in candidates) {
      try {
        final node = await gateway.findElement(_selectorFromMap(candidate));
        if (node != null) return node;
      } catch (_) {}
    }
    return null;
  }

  ActionResult _allSelectorsFailed(String action, List<String> tried) =>
      ActionResult(
        success: false,
        code: 'ALL_SELECTORS_FAILED',
        message: '$action: all ${tried.length} selectors failed. '
            'Tried: ${tried.join(' | ')}',
      );

  /// Build a Selector from a raw map (without the wrapping "selector" key).
  Selector _selectorFromMap(Map<String, dynamic> sel) => Selector(
        viewId:              sel['view_id']               as String?,
        textEquals:          sel['text']                  as String?,
        textContains:        sel['text_contains']         as String?,
        contentDescEquals:   sel['content_desc']          as String?,
        contentDescContains: sel['content_desc_contains'] as String?,
        className:           sel['class_name']            as String?,
        clickable:           sel['clickable']             as bool?,
        indexInParent:       (sel['index_in_parent'] as num?)?.toInt(),
      );

  String _candidateLabel(Map<String, dynamic> sel) {
    final parts = <String>[];
    for (final k in const [
      'view_id', 'content_desc', 'content_desc_contains',
      'text', 'text_contains', 'class_name',
    ]) {
      if (sel[k] != null) parts.add('$k=${sel[k]}');
    }
    return parts.isEmpty ? sel.toString() : parts.join(', ');
  }

  // ── Polling helpers ───────────────────────────────────────────────────────

  Future<ActionResult> _waitForApp(String pkg, Duration timeout) async {
    if (pkg.isEmpty) {
      return ActionResult(success: false, code: 'MISSING_PARAM', message: 'package not specified');
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

  Future<ActionResult> _waitForElement(Selector selector, Duration timeout) async {
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

  // ── Network retry ─────────────────────────────────────────────────────────

  Future<T> _withNetworkRetry<T>(Future<T> Function() call) async {
    int attempt = 0;
    while (true) {
      try {
        return await call();
      } catch (e) {
        attempt++;
        if (attempt >= _maxNetworkRetries) rethrow;
        _log('Network error (attempt $attempt/$_maxNetworkRetries): $e', LogLevel.warning);
        await Future.delayed(_networkRetryDelay);
      }
    }
  }

  // ── Misc helpers ──────────────────────────────────────────────────────────

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
    return Selector(
      viewId:              sel['view_id']               as String?,
      textEquals:          sel['text']                  as String?,
      textContains:        sel['text_contains']         as String?,
      contentDescEquals:   sel['content_desc']          as String?,
      contentDescContains: sel['content_desc_contains'] as String?,
      className:           sel['class_name']            as String?,
      clickable:           sel['clickable']             as bool?,
      indexInParent:       (sel['index_in_parent'] as num?)?.toInt(),
    );
  }

  void _log(String msg, [LogLevel level = LogLevel.info]) {
    onLog(msg, level);
  }
}
