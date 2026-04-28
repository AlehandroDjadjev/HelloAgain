import 'dart:async';

import 'package:android_control_plugin/android_control_plugin.dart';
import 'package:flutter/foundation.dart';

import '../api/agent_client.dart';
import '../native/mic_bridge_client.dart';
import 'pipeline_state.dart';
import 'step_runner.dart';

typedef StepRunnerFactory = StepRunner Function({
  required AgentClient client,
  required DeviceControlChannel gateway,
  required String sessionId,
  required String expectedPackage,
  required void Function(StepEntry step) onStepStarted,
  required void Function(String stepId, ActionResult result) onStepCompleted,
  required void Function(String message, LogLevel level) onLog,
  required Future<void> Function(Map<String, dynamic> confirmAction)
  onConfirmation,
  required Future<void> Function(Map<String, dynamic> userInputAction)
  onUserInputRequested,
  required void Function() onComplete,
  required void Function(String reason) onAbort,
  required void Function(String reason) onManualTakeover,
  required void Function(String? actualPackage) onUnexpectedAppChange,
});

/// Drives the text-to-execution pipeline for LLM-in-the-loop automation.
class PipelineOrchestrator extends ChangeNotifier {
  PipelineOrchestrator({
    required this.client,
    DeviceControlChannel? gateway,
    StepRunnerFactory? stepRunnerFactory,
    MicBridgeClient? micBridgeClient,
  }) : _gateway = gateway ?? const DeviceControlChannel(),
       _stepRunnerFactory = stepRunnerFactory ?? _defaultStepRunnerFactory,
       _micBridgeClient =
           micBridgeClient ?? MicBridgeClient(baseUrl: client.baseUrl),
       _ownsMicBridgeClient = micBridgeClient == null {
    _micBridgeEventsSubscription = _micBridgeClient.events.listen(
      _handleMicBridgeEvent,
      onError: (Object error, StackTrace stackTrace) {
        _log('Mic bridge event error: $error', level: LogLevel.warning);
      },
    );
  }

  final AgentClient client;
  final DeviceControlChannel _gateway;
  final StepRunnerFactory _stepRunnerFactory;
  final MicBridgeClient _micBridgeClient;
  final bool _ownsMicBridgeClient;

  PipelinePhase phase = PipelinePhase.idle;
  String? sessionId;
  Map<String, dynamic>? parsedIntent;
  List<StepEntry> steps = [];
  int currentStepIndex = -1;
  String currentReasoning = '';
  ConfirmationRequest? pendingConfirmation;
  UserInputRequest? pendingUserInput;
  String? errorMessage;
  final List<LogEntry> log = [];
  List<String> _supportedPackages = const [];

  bool get canPause =>
      phase == PipelinePhase.executing ||
      phase == PipelinePhase.awaitingConfirmation ||
      phase == PipelinePhase.awaitingUserInput;
  bool get canResume => phase == PipelinePhase.idle && sessionId != null;
  bool get canCancel => phase.isRunning;

  StepEntry? get currentStep =>
      currentStepIndex >= 0 && currentStepIndex < steps.length
      ? steps[currentStepIndex]
      : null;

  StepRunner? _runner;
  bool _cancelRequested = false;
  StreamSubscription<MicBridgeEvent>? _micBridgeEventsSubscription;

  bool get _cancelled => _cancelRequested;
  bool get hasPreparedCommand => sessionId != null && parsedIntent != null;

  Future<void> ensureMicBridgeReady() => _micBridgeClient.ensureServiceReady();
  Future<void> stopMicBridgeService() => _micBridgeClient.stopService();

  Future<void> run(String command, {String reasoningProvider = 'openai'}) async {
    if (phase.isRunning) return;

    await prepare(command, reasoningProvider: reasoningProvider);
    if (_cancelled || !hasPreparedCommand) return;
    await executePrepared();
  }

  Future<void> prepare(
    String command, {
    String reasoningProvider = 'openai',
  }) async {
    if (phase.isRunning) return;

    _reset();
    _log('Starting pipeline: "$command"');
    _log('Reasoning provider: ${_reasoningProviderLabel(reasoningProvider)}');

    try {
      await _discoverSupportedPackages();
      await _createSession(reasoningProvider);
      if (_cancelled) return;

      await _parseIntent(command);
      if (_cancelled) return;

      _setPhase(PipelinePhase.idle);
    } on AgentApiException catch (e) {
      _fail('API error ${e.statusCode}: ${e.shortMessage}');
    } catch (e) {
      _fail(e.toString());
    }
  }

  Future<void> prepareNavigation(String command) async {
    if (phase.isRunning) return;

    _reset();
    _log('Starting navigation pipeline: "$command"');
    _log('Navigation mode: API-only Google Maps plan');

    try {
      await _discoverSupportedPackages();
      if (_cancelled) return;

      _setPhase(PipelinePhase.creatingSession);
      _log('Preparing navigation session...');
      final resp = await client.prepareNavigation(
        prompt: command,
        deviceId: 'flutter-navigation',
        supportedPackages: _supportedPackages,
      );
      sessionId = resp['session_id'] as String?;
      _syncMicBridgeSessionContext();
      parsedIntent = (resp['intent'] as Map?)?.cast<String, dynamic>() ?? {};
      final destination =
          ((parsedIntent?['entities'] as Map?)?['destination'] ?? '').toString();
      if (destination.isNotEmpty) {
        _log('Navigation ready for $destination.', level: LogLevel.success);
      } else {
        _log('Navigation session prepared.', level: LogLevel.success);
      }
      _setPhase(PipelinePhase.idle);
    } on AgentApiException catch (e) {
      _fail('API error ${e.statusCode}: ${e.shortMessage}');
    } catch (e) {
      _fail(e.toString());
    }
  }

  Future<void> preparePhoneCommand(
    String command, {
    String reasoningProvider = 'openai',
  }) async {
    if (phase.isRunning) return;

    _reset();
    _log('Starting phone command pipeline: "$command"');
    _log('Reasoning provider: ${_reasoningProviderLabel(reasoningProvider)}');

    try {
      await _discoverSupportedPackages();
      if (_cancelled) return;

      _setPhase(PipelinePhase.creatingSession);
      _log('Submitting prompt through the one-shot phone command API...');
      final resp = await client.startPhoneCommand(
        prompt: command,
        deviceId: 'flutter-phone-command',
        inputMode: 'text',
        reasoningProvider: reasoningProvider,
        supportedPackages: _supportedPackages,
      );
      sessionId = resp['session_id'] as String?;
      _syncMicBridgeSessionContext();
      parsedIntent = (resp['intent'] as Map?)?.cast<String, dynamic>() ?? {};
      final goal = (parsedIntent?['goal'] ?? '').toString().trim();
      if (goal.isNotEmpty) {
        _log('Phone command ready: $goal', level: LogLevel.success);
      } else {
        _log('Phone command session prepared.', level: LogLevel.success);
      }
      _setPhase(PipelinePhase.idle);
    } on AgentApiException catch (e) {
      _fail('API error ${e.statusCode}: ${e.shortMessage}');
    } catch (e) {
      _fail(e.toString());
    }
  }

  Future<void> executePrepared() async {
    if (phase.isRunning) return;
    if (!hasPreparedCommand) {
      _fail('No prepared command is ready to execute.');
      return;
    }
    try {
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
    if (phase == PipelinePhase.awaitingUserInput) {
      await _micBridgeClient.cancelListening(
        requestId: pendingUserInput?.queryId,
      );
    }
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
    await _micBridgeClient.cancelListening(requestId: pendingUserInput?.queryId);
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

  Future<void> discardPrepared() async {
    _runner?.cancel();
    await _micBridgeClient.cancelListening(requestId: pendingUserInput?.queryId);
    if (sessionId != null) {
      try {
        await client.cancelSession(sessionId!);
      } catch (_) {}
    }
    _reset();
    notifyListeners();
  }

  Future<void> _discoverSupportedPackages() async {
    _setPhase(PipelinePhase.creatingSession);
    _log('Discovering installed Android apps...');
    _supportedPackages = await _gateway.listLaunchablePackages();
    if (_supportedPackages.isEmpty) {
      _log(
        'Could not discover launchable apps. Continuing with backend fallback handling.',
        level: LogLevel.warning,
      );
    } else {
      _log(
        'Discovered ${_supportedPackages.length} launchable apps on this device.',
      );
    }
  }

  Future<void> _createSession(String reasoningProvider) async {
    _log('Creating agent session...');
    final resp = await client.createSession(
      inputMode: 'text',
      reasoningProvider: reasoningProvider,
      supportedPackages: _supportedPackages,
    );
    sessionId = resp['session_id'] as String;
    _syncMicBridgeSessionContext();
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
        parsedIntent?['app_package'] as String? ??
        parsedIntent?['target_app'] as String? ??
          '';
    final result = await _gateway.startSession(
      SessionConfig(
        sessionId: sessionId!,
        allowedPackages: _supportedPackages.isNotEmpty
            ? _supportedPackages
            : [if (appPackage.isNotEmpty) appPackage],
        confirmationMode: 'always',
        allowTextEntry: true,
        allowSendActions: true,
        allowSensitiveNodes: true,
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

    _runner = _stepRunnerFactory(
      client: client,
      gateway: _gateway,
      sessionId: sessionId!,
      expectedPackage:
          parsedIntent?['app_package'] as String? ??
          parsedIntent?['target_app'] as String? ??
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
      onUserInputRequested: (action) async {
        await _handleUserInputRequested(UserInputRequest.fromJson(action));
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

  static StepRunner _defaultStepRunnerFactory({
    required AgentClient client,
    required DeviceControlChannel gateway,
    required String sessionId,
    required String expectedPackage,
    required void Function(StepEntry step) onStepStarted,
    required void Function(String stepId, ActionResult result) onStepCompleted,
    required void Function(String message, LogLevel level) onLog,
    required Future<void> Function(Map<String, dynamic> confirmAction)
    onConfirmation,
    required Future<void> Function(Map<String, dynamic> userInputAction)
    onUserInputRequested,
    required void Function() onComplete,
    required void Function(String reason) onAbort,
    required void Function(String reason) onManualTakeover,
    required void Function(String? actualPackage) onUnexpectedAppChange,
  }) {
    return StepRunner(
      client: client,
      gateway: gateway,
      sessionId: sessionId,
      expectedPackage: expectedPackage,
      onStepStarted: onStepStarted,
      onStepCompleted: onStepCompleted,
      onLog: onLog,
      onConfirmation: onConfirmation,
      onUserInputRequested: onUserInputRequested,
      onComplete: onComplete,
      onAbort: onAbort,
      onManualTakeover: onManualTakeover,
      onUnexpectedAppChange: onUnexpectedAppChange,
    );
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

  Future<void> _handleUserInputRequested(UserInputRequest request) async {
    if (sessionId == null) {
      _fail('Cannot collect clarification without an active session.');
      return;
    }

    var currentRequest = request;
    while (!_cancelled) {
      pendingUserInput = currentRequest;
      currentReasoning = currentRequest.question;
      _log(
        'Clarification requested (${currentRequest.attempt}/${currentRequest.maxAttempts}).',
        level: LogLevel.warning,
      );
      _setPhase(PipelinePhase.awaitingUserInput);

      try {
        final result = await _micBridgeClient.beginOneShotQuery(
          currentRequest.question,
        );
        if (_cancelled) {
          return;
        }

        final spokenReply = (result?.transcript ?? '').trim();
        final submittedTranscript = spokenReply.isEmpty
            ? '__silence__'
            : spokenReply;
        final decision = await client.submitUserInput(
          sessionId!,
          queryId: currentRequest.queryId,
          transcript: submittedTranscript,
          source: 'voice',
        );
        if (_cancelled) {
          return;
        }

        final status = (decision['status'] ?? '').toString();
        switch (status) {
          case 'resolved':
            pendingUserInput = null;
            notifyListeners();
            _log('Clarification resolved.', level: LogLevel.success);
            _setPhase(PipelinePhase.executing);
            await _runner?.runLoop();
            return;
          case 'needs_user_input':
            final nextRequest = UserInputRequest.fromJson(decision);
            if (nextRequest.queryId.isEmpty ||
                nextRequest.queryId != currentRequest.queryId) {
              _fail(
                'Clarification contract error: backend returned a mismatched query id.',
              );
              return;
            }
            if (nextRequest.attempt <= currentRequest.attempt) {
              _fail(
                'Clarification contract error: follow-up attempt did not advance.',
              );
              return;
            }
            if (nextRequest.attempt > nextRequest.maxAttempts) {
              _fail(
                'Clarification contract error: follow-up exceeded max attempts.',
              );
              return;
            }
            currentRequest = nextRequest;
            final whyUnresolved = currentRequest.whyUnresolved.trim();
            _log(
              whyUnresolved.isEmpty
                  ? 'Clarification still unresolved; asking a follow-up.'
                  : 'Clarification unresolved: $whyUnresolved',
              level: LogLevel.warning,
            );
            continue;
          case 'manual_takeover':
            pendingUserInput = null;
            errorMessage =
                (decision['reason'] ??
                        decision['why_unresolved'] ??
                        'Manual takeover required after clarification.')
                    .toString();
            _log(errorMessage!, level: LogLevel.warning);
            _setPhase(PipelinePhase.failed);
            notifyListeners();
            return;
          default:
            _fail('Unexpected clarification response: ${decision['status']}');
            return;
        }
      } on AgentApiException catch (e) {
        _fail('Clarification API error ${e.statusCode}: ${e.shortMessage}');
        return;
      } catch (e) {
        _fail('Clarification failed: $e');
        return;
      }
    }
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

  void _syncMicBridgeSessionContext() {
    _micBridgeClient.updateSessionContext(sessionId: sessionId);
  }

  void _handleMicBridgeEvent(MicBridgeEvent event) {
    switch (event.type) {
      case MicBridgeEventType.serviceReady:
        _log('Mic bridge ready in armed-idle mode.');
        return;
      case MicBridgeEventType.ttsStarted:
        _log('Clarification prompt started.');
        return;
      case MicBridgeEventType.ttsFinished:
        _log('Clarification prompt finished.');
        return;
      case MicBridgeEventType.listeningStarted:
        _log('Listening for one clarification reply...');
        return;
      case MicBridgeEventType.partialTranscript:
        final transcript = (event.transcript ?? '').trim();
        if (transcript.isNotEmpty) {
          _log('Heard reply: "$transcript"');
        }
        return;
      case MicBridgeEventType.finalTranscript:
        if ((event.transcript ?? '').trim().isNotEmpty) {
          _log('Captured final clarification reply.', level: LogLevel.success);
        }
        return;
      case MicBridgeEventType.error:
        final message = event.message?.trim() ?? 'Mic bridge error.';
        _log('Mic bridge: $message', level: LogLevel.warning);
        return;
      case MicBridgeEventType.unknown:
        return;
    }
  }

  void _reset() {
    phase = PipelinePhase.idle;
    sessionId = null;
    _syncMicBridgeSessionContext();
    parsedIntent = null;
    steps = [];
    currentStepIndex = -1;
    currentReasoning = '';
    pendingConfirmation = null;
    pendingUserInput = null;
    errorMessage = null;
    log.clear();
    _supportedPackages = const [];
    _cancelRequested = false;
    _runner = null;
  }

  static String _reasoningProviderLabel(String provider) {
    switch (provider) {
      case 'openai':
        return 'OpenAI API';
      case 'local':
        return 'Local model';
      default:
        return provider;
    }
  }

  @override
  void dispose() {
    _runner?.cancel();
    _micBridgeEventsSubscription?.cancel();
    if (_ownsMicBridgeClient) {
      unawaited(_micBridgeClient.dispose());
    }
    super.dispose();
  }
}
