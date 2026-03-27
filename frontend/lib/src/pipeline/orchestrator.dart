import 'dart:async';

import 'package:android_control_plugin/android_control_plugin.dart';
import 'package:flutter/foundation.dart';

import '../api/agent_client.dart';
import 'pipeline_state.dart';
import 'step_runner.dart';

/// Drives the text-to-execution pipeline for LLM-in-the-loop automation.
class PipelineOrchestrator extends ChangeNotifier {
  PipelineOrchestrator({required this.client});

  final AgentClient client;
  final _gateway = const DeviceControlChannel();

  PipelinePhase phase = PipelinePhase.idle;
  String? sessionId;
  Map<String, dynamic>? parsedIntent;
  List<StepEntry> steps = [];
  int currentStepIndex = -1;
  String currentReasoning = '';
  ConfirmationRequest? pendingConfirmation;
  String? errorMessage;
  final List<LogEntry> log = [];

  bool get canPause =>
      phase == PipelinePhase.executing ||
      phase == PipelinePhase.awaitingConfirmation;
  bool get canResume => phase == PipelinePhase.idle && sessionId != null;
  bool get canCancel => phase.isRunning;

  StepEntry? get currentStep =>
      currentStepIndex >= 0 && currentStepIndex < steps.length
      ? steps[currentStepIndex]
      : null;

  StepRunner? _runner;
  bool _cancelRequested = false;

  bool get _cancelled => _cancelRequested;

  Future<void> run(String command, {String reasoningProvider = 'local'}) async {
    if (phase.isRunning) return;

    _reset();
    _log('Starting pipeline: "$command"');
    _log('Reasoning provider: ${_reasoningProviderLabel(reasoningProvider)}');

    try {
      await _createSession(reasoningProvider);
      if (_cancelled) return;

      await _parseIntent(command);
      if (_cancelled) return;

      await _startAndroidSession();
      if (_cancelled) return;

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
      try {
        await client.cancelSession(sessionId!);
      } catch (_) {}
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
    _log('Confirmation rejected, aborting.', level: LogLevel.warning);
    pendingConfirmation = null;
    _setPhase(PipelinePhase.cancelled);
  }

  Future<void> _createSession(String reasoningProvider) async {
    _setPhase(PipelinePhase.creatingSession);
    _log('Creating agent session...');
    final resp = await client.createSession(
      inputMode: 'text',
      reasoningProvider: reasoningProvider,
      supportedPackages: const [
        'com.whatsapp',
        'com.google.android.apps.maps',
        'com.android.chrome',
        'com.google.android.gm',
        'com.supercell.brawlstars',
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
    final app =
        parsedIntent!['target_app'] ??
        parsedIntent!['app_package'] ??
        'unknown';
    final risk = parsedIntent!['risk_level'] ?? 'low';
    _log('Intent ready: app=$app risk=$risk', level: LogLevel.success);
    notifyListeners();
  }

  Future<void> _startAndroidSession() async {
    _log('Starting Android accessibility session...');
    final appPackage =
        parsedIntent?['target_app'] as String? ??
        parsedIntent?['app_package'] as String? ??
        '';
    final result = await _gateway.startSession(
      SessionConfig(
        sessionId: sessionId!,
        allowedPackages: [appPackage],
        confirmationMode: 'always',
        allowTextEntry: true,
        allowSendActions: true,
      ),
    );
    if (result.code == 'SERVICE_NOT_ENABLED') {
      _log(
        'Accessibility service not enabled. Steps will simulate without device execution.',
        level: LogLevel.warning,
      );
    } else if (!result.success) {
      _log(
        'Android session start warning (${result.code}). Continuing anyway.',
        level: LogLevel.warning,
      );
    } else {
      _log('Android session started.', level: LogLevel.success);
    }
  }

  Future<void> _startExecutionLoop() async {
    _setPhase(PipelinePhase.executing);

    _runner = StepRunner(
      client: client,
      gateway: _gateway,
      sessionId: sessionId!,
      expectedPackage:
          parsedIntent?['target_app'] as String? ??
          parsedIntent?['app_package'] as String? ??
          '',
      onStepStarted: (step) {
        _upsertStep(step, StepStatus.running);
        currentReasoning = step.reasoning;
        notifyListeners();
      },
      onStepCompleted: (stepId, result) {
        _markStep(
          stepId,
          result.success ? StepStatus.success : StepStatus.failed,
        );
        _log(
          '${result.success ? 'OK' : 'FAIL'} $stepId ${result.code.isEmpty ? 'OK' : result.code}',
          level: result.success ? LogLevel.success : LogLevel.error,
        );
      },
      onLog: (msg, lvl) => _log(msg, level: lvl),
      onConfirmation: (action) async {
        _setPhase(PipelinePhase.awaitingConfirmation);
        await _fetchAndShowConfirmation(action);
      },
      onComplete: () {
        _log('Pipeline complete.', level: LogLevel.success);
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
        _log(
          'App changed to ${pkg ?? '(unknown)'}; backend will decide retry or takeover.',
          level: LogLevel.warning,
        );
      },
    );

    await _runner!.runLoop();
  }

  Future<void> _fetchAndShowConfirmation(
    Map<String, dynamic> confirmAction,
  ) async {
    final params =
        (confirmAction['params'] as Map?)?.cast<String, dynamic>() ?? {};
    currentReasoning = params['content_preview'] as String? ?? currentReasoning;

    try {
      final resp = await client.getPendingConfirmation(sessionId!);
      final hasPending = resp['has_pending'] as bool? ?? false;
      if (hasPending) {
        final confData = (resp['confirmation'] as Map?)
            ?.cast<String, dynamic>();
        if (confData != null) {
          pendingConfirmation = ConfirmationRequest.fromJson(confData);
          notifyListeners();
          return;
        }
      }
    } catch (_) {}

    pendingConfirmation = ConfirmationRequest(
      confirmationId: '',
      stepId: confirmAction['id'] as String? ?? '',
      appName:
          parsedIntent?['target_app'] as String? ??
          parsedIntent?['app_package'] as String? ??
          'App',
      actionSummary:
          params['action_summary'] as String? ?? 'Confirm this action?',
      recipient: params['recipient'] as String? ?? '',
      contentPreview: params['content_preview'] as String? ?? '',
    );
    notifyListeners();
  }

  void _upsertStep(StepEntry step, StepStatus status) {
    final idx = steps.indexWhere((entry) => entry.id == step.id);
    final next = StepEntry(
      id: step.id,
      type: step.type,
      label: step.label,
      reasoning: step.reasoning,
      status: status,
    );
    if (idx == -1) {
      steps = [...steps, next];
      currentStepIndex = steps.length - 1;
    } else {
      final updated = [...steps];
      updated[idx] = next;
      steps = updated;
      currentStepIndex = idx;
    }
    notifyListeners();
  }

  void _markStep(String id, StepStatus status) {
    final idx = steps.indexWhere((entry) => entry.id == id);
    if (idx == -1) return;
    final current = steps[idx];
    final updated = [...steps];
    updated[idx] = StepEntry(
      id: current.id,
      type: current.type,
      label: current.label,
      reasoning: current.reasoning,
      status: status,
    );
    steps = updated;
    currentStepIndex = idx;
    notifyListeners();
  }

  void _setPhase(PipelinePhase p) {
    phase = p;
    notifyListeners();
  }

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
    phase = PipelinePhase.idle;
    sessionId = null;
    parsedIntent = null;
    steps = [];
    currentStepIndex = -1;
    currentReasoning = '';
    pendingConfirmation = null;
    errorMessage = null;
    log.clear();
    _cancelRequested = false;
    _runner = null;
  }

  static String _reasoningProviderLabel(String provider) {
    switch (provider) {
      case 'openai':
        return 'OpenAI API';
      case 'local':
      default:
        return 'Local model';
    }
  }
}
