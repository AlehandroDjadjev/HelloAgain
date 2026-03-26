import 'dart:async';

import 'package:android_control_plugin/android_control_plugin.dart';
import 'package:flutter/foundation.dart';

import '../api/agent_client.dart';
import 'pipeline_state.dart';
import 'plan_builder.dart';
import 'step_runner.dart';

/// Drives the full text→gestures pipeline.
///
/// UI binds to this [ChangeNotifier] and rebuilds on every state update.
/// The execution loop is delegated to [StepRunner], which keeps this class
/// focused on pipeline orchestration (session, intent, plan, approval).
class PipelineOrchestrator extends ChangeNotifier {
  PipelineOrchestrator({required this.client});

  final AgentClient client;
  final _gateway = const DeviceControlChannel();

  // ── Observable state ──────────────────────────────────────────────────────

  PipelinePhase phase = PipelinePhase.idle;
  String? sessionId;
  String? planId;
  Map<String, dynamic>? parsedIntent;
  List<StepEntry> steps = [];
  int currentStepIndex = 0;
  ConfirmationRequest? pendingConfirmation;
  String? errorMessage;
  final List<LogEntry> log = [];

  bool get canPause  => phase == PipelinePhase.executing ||
                        phase == PipelinePhase.awaitingConfirmation;
  bool get canResume => phase == PipelinePhase.idle && sessionId != null;
  bool get canCancel => phase.isRunning;

  StepRunner? _runner;

  // ── Control ───────────────────────────────────────────────────────────────

  /// Full pipeline: session → intent → plan → approve → execute loop.
  Future<void> run(String command, {Map<String, dynamic> extras = const {}}) async {
    if (phase.isRunning) return;

    _reset();
    _log('Starting pipeline: "$command"');

    try {
      await _createSession(command);       if (_cancelled) return;
      await _parseIntent(command);         if (_cancelled) return;
      await _buildAndSubmitPlan(command, extras); if (_cancelled) return;
      await _approvePlan();                if (_cancelled) return;
      await _startAndroidSession();        if (_cancelled) return;
      await _startExecutionLoop();
    } on AgentApiException catch (e) {
      _fail('API error ${e.statusCode}: ${e.shortMessage}');
    } catch (e) {
      _fail(e.toString());
    }
  }

  Future<void> pause() async {
    if (!canPause || sessionId == null) return;
    _runner?.cancel();
    try {
      await client.pauseSession(sessionId!);
      _log('Session paused.', level: LogLevel.warning);
      _setPhase(PipelinePhase.idle);
    } catch (e) {
      _log('Pause failed: $e', level: LogLevel.error);
    }
  }

  Future<void> cancel() async {
    _cancelRequested = true;
    _runner?.cancel();
    if (sessionId != null) {
      try { await client.cancelSession(sessionId!); } catch (_) {}
    }
    _log('Session cancelled.', level: LogLevel.warning);
    _setPhase(PipelinePhase.cancelled);
  }

  Future<void> approveConfirmation() async {
    final conf = pendingConfirmation;
    if (conf == null) return;
    try {
      if (conf.confirmationId.isNotEmpty) {
        await client.approveConfirmation(conf.confirmationId);
      }
      _log('Confirmation approved.', level: LogLevel.success);
      pendingConfirmation = null;
      notifyListeners();
      _setPhase(PipelinePhase.executing);
      // Resume runner loop — StepRunner picks up from where it left off
      await _runner?.runLoop();
    } catch (e) {
      _fail('Approve failed: $e');
    }
  }

  Future<void> rejectConfirmation() async {
    final conf = pendingConfirmation;
    if (conf == null) return;
    _runner?.cancel();
    try {
      if (conf.confirmationId.isNotEmpty) {
        await client.rejectConfirmation(conf.confirmationId);
      }
    } catch (_) {}
    _log('Confirmation rejected — aborting.', level: LogLevel.warning);
    pendingConfirmation = null;
    _setPhase(PipelinePhase.cancelled);
  }

  // ── Pipeline stages ───────────────────────────────────────────────────────

  Future<void> _createSession(String command) async {
    _setPhase(PipelinePhase.creatingSession);
    _log('Creating agent session…');
    final resp = await client.createSession(
      inputMode: 'text',
      supportedPackages: const [
        'com.whatsapp',
        'com.google.android.apps.maps',
        'com.android.chrome',
        'com.google.android.gm',
      ],
    );
    sessionId = resp['session_id'] as String;
    _log('Session created: $sessionId', level: LogLevel.success);
    notifyListeners();
  }

  Future<void> _parseIntent(String command) async {
    _setPhase(PipelinePhase.parsingIntent);
    _log('Parsing intent: "$command"');
    final resp = await client.submitIntent(sessionId!, command);
    parsedIntent = (resp['intent'] as Map?)?.cast<String, dynamic>() ?? {};
    final app  = parsedIntent!['target_app'] ?? 'unknown';
    final risk = parsedIntent!['risk_level']  ?? 'low';
    _log('Intent: app=$app  risk=$risk', level: LogLevel.success);
    notifyListeners();
  }

  Future<void> _buildAndSubmitPlan(
    String command,
    Map<String, dynamic> extras,
  ) async {
    _setPhase(PipelinePhase.buildingPlan);
    _log('Building action plan…');

    final plan = PlanBuilder.build(
      sessionId: sessionId!,
      intent: parsedIntent!,
      extras: {'command': command, ...extras},
    );

    if (plan == null) {
      throw Exception(
        'No plan template for app "${parsedIntent!['target_app']}". '
        'Try a command mentioning WhatsApp, Maps, Gmail, or Chrome.',
      );
    }

    planId = plan['plan_id'] as String;
    _log('Plan: ${(plan['steps'] as List).length} steps for ${plan['app_package']}');

    steps = (plan['steps'] as List).map((s) {
      final sm = s as Map<String, dynamic>;
      return StepEntry(
        id:    sm['id']   as String,
        type:  sm['type'] as String,
        label: _stepLabel(sm),
      );
    }).toList();
    notifyListeners();

    await client.submitPlan(sessionId!, plan);
    _log('Plan submitted.', level: LogLevel.success);
  }

  Future<void> _approvePlan() async {
    _setPhase(PipelinePhase.approvingPlan);
    _log('Approving plan…');
    await client.approvePlan(sessionId!, planId: planId);
    _log('Plan approved — execution authorised.', level: LogLevel.success);
  }

  Future<void> _startAndroidSession() async {
    _log('Starting Android accessibility session…');
    final result = await _gateway.startSession(SessionConfig(
      sessionId: sessionId!,
      allowedPackages: [parsedIntent!['target_app'] as String? ?? ''],
      confirmationMode: 'always',
      allowTextEntry: true,
      allowSendActions: true,
    ));
    if (result.code == 'SERVICE_NOT_ENABLED') {
      _log('Accessibility service not enabled — steps will simulate without device execution.',
          level: LogLevel.warning);
    } else if (!result.success) {
      _log('Android session start warning (${result.code}) — continuing.',
          level: LogLevel.warning);
    } else {
      _log('Android session started.', level: LogLevel.success);
    }
  }

  Future<void> _startExecutionLoop() async {
    _setPhase(PipelinePhase.executing);

    _runner = StepRunner(
      client:          client,
      gateway:         _gateway,
      sessionId:       sessionId!,
      planId:          planId!,
      expectedPackage: parsedIntent?['target_app'] as String? ?? '',

      onStepStarted: (id) {
        _markStep(id, StepStatus.running);
      },

      onStepCompleted: (id, result) {
        _markStep(id, result.success ? StepStatus.success : StepStatus.failed);
        _log(
          '${result.success ? '✓' : '✗'} $id  ${result.code.isEmpty ? 'OK' : result.code}',
          level: result.success ? LogLevel.success : LogLevel.error,
        );
      },

      onLog: (msg, lvl) => _log(msg, level: lvl),

      onConfirmation: (action) async {
        _setPhase(PipelinePhase.awaitingConfirmation);
        await _fetchAndShowConfirmation(action);
      },

      onComplete: () {
        _log('Pipeline complete!', level: LogLevel.success);
        _setPhase(PipelinePhase.completed);
        _gateway.stopSession(sessionId!).ignore();
      },

      onAbort: (reason) => _fail(reason),

      onManualTakeover: (reason) {
        errorMessage = reason;
        _log('Manual takeover required: $reason', level: LogLevel.warning);
        _setPhase(PipelinePhase.failed);
        notifyListeners();
      },

      onUnexpectedAppChange: (pkg) {
        _log('App changed to $pkg — backend will decide retry/takeover.',
            level: LogLevel.warning);
      },
    );

    await _runner!.runLoop();
  }

  // ── Confirmation ──────────────────────────────────────────────────────────

  Future<void> _fetchAndShowConfirmation(Map<String, dynamic> confirmAction) async {
    try {
      final resp  = await client.getPendingConfirmation(sessionId!);
      final hasPending = resp['has_pending'] as bool? ?? false;
      if (hasPending) {
        final confData = (resp['confirmation'] as Map?)?.cast<String, dynamic>();
        if (confData != null) {
          pendingConfirmation = ConfirmationRequest.fromJson(confData);
          notifyListeners();
          return;
        }
      }
    } catch (_) {}

    // Fallback: synthesise from the action params when backend has no record yet
    final params = (confirmAction['params'] as Map?)?.cast<String, dynamic>() ?? {};
    pendingConfirmation = ConfirmationRequest(
      confirmationId: '',
      stepId:         confirmAction['id'] as String? ?? '',
      appName:        parsedIntent?['target_app'] as String? ?? 'App',
      actionSummary:  params['action_summary'] as String? ?? 'Confirm this action?',
      recipient:      params['recipient']       as String? ?? '',
      contentPreview: params['content_preview'] as String? ?? '',
    );
    notifyListeners();
  }

  // ── Helpers ───────────────────────────────────────────────────────────────

  void _markStep(String id, StepStatus s) {
    final idx = steps.indexWhere((e) => e.id == id);
    if (idx != -1) {
      steps[idx].status = s;
      currentStepIndex  = idx;
      notifyListeners();
    }
  }

  void _setPhase(PipelinePhase p)  { phase = p; notifyListeners(); }

  void _log(String msg, {LogLevel level = LogLevel.info}) {
    log.add(LogEntry(msg, level: level));
    notifyListeners();
  }

  void _fail(String msg) {
    errorMessage = msg;
    _log('ERROR: $msg', level: LogLevel.error);
    _setPhase(PipelinePhase.failed);
  }

  void _reset() {
    phase             = PipelinePhase.idle;
    sessionId         = null;
    planId            = null;
    parsedIntent      = null;
    steps             = [];
    currentStepIndex  = 0;
    pendingConfirmation = null;
    errorMessage      = null;
    log.clear();
    _cancelRequested  = false;
    _runner           = null;
  }

  bool _cancelRequested = false;
  bool get _cancelled   => _cancelRequested;

  static String _stepLabel(Map<String, dynamic> step) {
    final type   = step['type'] as String;
    final params = (step['params'] as Map?)?.cast<String, dynamic>() ?? {};
    return switch (type) {
      'OPEN_APP'             => 'Open ${params['package'] as String? ?? ''}',
      'WAIT_FOR_APP'         => 'Wait for ${params['package'] as String? ?? ''}',
      'WAIT_FOR_ELEMENT'     => 'Wait for element',
      'TAP_ELEMENT'          => 'Tap element',
      'TYPE_TEXT'            => 'Type "${params['text'] as String? ?? ''}"',
      'REQUEST_CONFIRMATION' => 'Request confirmation',
      'SCROLL'               => 'Scroll ${params['direction'] as String? ?? ''}',
      'BACK'                 => 'Go back',
      'HOME'                 => 'Go home',
      _                      => type,
    };
  }
}
