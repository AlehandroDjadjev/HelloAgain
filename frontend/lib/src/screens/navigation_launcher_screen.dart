import 'dart:async';
import 'dart:convert';

import 'package:android_control_plugin/android_control_plugin.dart';
import 'package:flutter/material.dart';

import '../api/agent_client.dart';
import '../api/voice_gateway_client.dart';
import '../config/backend_base_url.dart';
import '../pipeline/orchestrator.dart';
import '../pipeline/pipeline_state.dart';
import '../services/navigation_overlay_service.dart';
import '../voice/agent_voice_controller.dart';

class NavigationLauncherScreen extends StatefulWidget {
  const NavigationLauncherScreen({
    super.key,
    this.initialPrompt,
    this.autoRunOnOpen = false,
  });

  final String? initialPrompt;
  final bool autoRunOnOpen;

  @override
  State<NavigationLauncherScreen> createState() =>
      _NavigationLauncherScreenState();
}

class _NavigationLauncherScreenState extends State<NavigationLauncherScreen>
    with WidgetsBindingObserver {
  static const _reasoningProvider = 'openai';
  static const _voiceLanguage = 'bg-BG';

  late final TextEditingController _promptController;
  late final PipelineOrchestrator _orch;
  late final AgentVoiceController _voiceController;
  final _overlayService = const NavigationOverlayService();
  final _deviceGateway = const DeviceControlChannel();
  bool _showDebug = false;
  bool _accessibilityPermissionMissing = false;
  bool _overlayPermissionMissing = false;
  bool _awaitingAccessibilityPermissionReturn = false;
  bool _awaitingOverlayPermissionReturn = false;
  bool _voiceStarting = false;
  bool _overlayVisible = false;
  bool _completionHandled = false;
  bool _bringingAppToFront = false;
  bool _awaitingClarificationResponse = false;
  bool _awaitingPostCompletionInstruction = false;
  String? _pendingClarificationPrompt;
  String? _pendingClarificationQuestion;
  PipelinePhase? _postTaskPhase;
  String? _postTaskStatusMessage;
  String? _lastTerminalMessage;
  String _lastSubmittedPrompt = '';
  String? _voiceError;
  PipelinePhase _lastObservedPhase = PipelinePhase.idle;

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    _promptController = TextEditingController(
      text: widget.initialPrompt?.trim().isNotEmpty == true
          ? widget.initialPrompt!.trim()
          : '',
    );
    _orch = PipelineOrchestrator(
      client: AgentClient(baseUrl: resolveBackendBaseUrl()),
    )..addListener(_onOrchestratorChanged);
    _voiceController = AgentVoiceController(
      client: VoiceGatewayClient(baseUrl: resolveBackendBaseUrl()),
      onTranscript: _handleVoiceTranscript,
      language: _voiceLanguage,
    )..addListener(_onVoiceControllerChanged);
    unawaited(_refreshPermissionState());
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (!mounted) return;
      unawaited(_ensureHandsFreeVoiceReady());
    });

    if (widget.autoRunOnOpen) {
      WidgetsBinding.instance.addPostFrameCallback((_) {
        if (!mounted) return;
        unawaited(_startCommand());
      });
    }
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    _orch.removeListener(_onOrchestratorChanged);
    _voiceController.removeListener(_onVoiceControllerChanged);
    unawaited(_voiceController.stop());
    _voiceController.dispose();
    unawaited(_overlayService.hide());
    _promptController.dispose();
    super.dispose();
  }

  void _onOrchestratorChanged() {
    if (!mounted) return;
    if (_orch.phase != _lastObservedPhase) {
      _lastObservedPhase = _orch.phase;
      unawaited(_syncNavigationOverlay());
      if (_isTerminalPhase(_orch.phase) && !_completionHandled) {
        _completionHandled = true;
        unawaited(_enterPostCompletionConversation(_orch.phase));
      } else if (!_isTerminalPhase(_orch.phase)) {
        _completionHandled = false;
      }
    }
    setState(() {});
  }

  void _onVoiceControllerChanged() {
    if (!mounted) return;
    final nextError = _voiceController.error?.trim();
    if ((_voiceError ?? '') != (nextError ?? '')) {
      _voiceError = nextError?.isEmpty == true ? null : nextError;
      unawaited(_syncNavigationOverlay());
    }
    setState(() {});
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    if (state != AppLifecycleState.resumed) {
      return;
    }
    if (_awaitingAccessibilityPermissionReturn) {
      _awaitingAccessibilityPermissionReturn = false;
      unawaited(_handleAccessibilityPermissionReturn());
    }
    if (_awaitingOverlayPermissionReturn) {
      _awaitingOverlayPermissionReturn = false;
      unawaited(_handleOverlayPermissionReturn());
    }
  }

  Future<void> _startCommand({
    String? promptOverride,
    Map<String, dynamic> contextOverride = const {},
  }) async {
    final prompt = (promptOverride ?? _promptController.text).trim();
    if (prompt.isEmpty || _orch.phase.isRunning) {
      return;
    }

    _lastSubmittedPrompt = prompt;
    _resetConversationWaitState();
    FocusScope.of(context).unfocus();
    await _refreshPermissionState();
    await _showStartupOverlayIfPossible(prompt);
    await _runCommandFlow(prompt, context: contextOverride);
  }

  Future<void> _runCommandFlow(
    String prompt, {
    Map<String, dynamic> context = const {},
  }) async {
    await _orch.preparePhoneCommand(
      prompt,
      reasoningProvider: _reasoningProvider,
      context: context,
    );
    if (!mounted) return;
    if (!_orch.hasPreparedCommand || _orch.errorMessage != null) {
      return;
    }
    final clarificationQuestion = _preparedClarificationQuestion();
    if (clarificationQuestion != null) {
      await _orch.discardPrepared();
      if (!mounted) return;
      await _beginClarification(prompt, clarificationQuestion);
      return;
    }
    await _orch.executePrepared();
  }

  String _statusText() {
    if (_awaitingPostCompletionInstruction && _postTaskStatusMessage != null) {
      return _postTaskStatusMessage!;
    }
    if (_awaitingClarificationResponse) {
      return _pendingClarificationQuestion ??
          'Трябва ми още една подробност, преди да продължа.';
    }
    if (_awaitingPostCompletionInstruction) {
      return 'Задачата е изпълнена. Кажете следваща команда или кажете готово, за да се върна.';
    }
    if (_accessibilityPermissionMissing && !_orch.phase.isRunning) {
      return 'Enable Accessibility Control so Hello Again can operate the phone directly.';
    }
    if (_overlayPermissionMissing && !_orch.phase.isRunning) {
      return 'The phone flow can still run, but the floating navigator bubble needs "Display over other apps" to appear outside the app.';
    }
    switch (_orch.phase) {
      case PipelinePhase.creatingSession:
        return 'Creating the command session...';
      case PipelinePhase.parsingIntent:
        return 'Understanding your prompt...';
      case PipelinePhase.executing:
        return 'Starting the phone flow...';
      case PipelinePhase.awaitingConfirmation:
        return 'Waiting for confirmation on the device...';
      case PipelinePhase.completed:
        return 'Phone flow completed.';
      case PipelinePhase.failed:
        return _orch.errorMessage ?? 'Командата не можа да бъде стартирана.';
      case PipelinePhase.cancelled:
        return 'The command was cancelled.';
      case PipelinePhase.idle:
        final summary = _intentSummaryText();
        if (summary.isNotEmpty && _orch.sessionId != null) {
          return 'Prepared: $summary';
        }
        return 'Loading the phone flow...';
    }
  }

  Future<void> _refreshPermissionState() async {
    final permissionStatus = await PermissionChecker.getPermissionStatus();
    final accessibilityMissing =
        permissionStatus['accessibilityService'] != true;

    var overlayMissing = false;
    if (_overlayService.isSupported) {
      final granted = await _overlayService.hasPermission();
      overlayMissing = !granted;
    }

    if (!mounted) return;
    setState(() {
      _accessibilityPermissionMissing = accessibilityMissing;
      _overlayPermissionMissing = overlayMissing;
    });
  }

  Future<void> _handleAccessibilityPermissionReturn() async {
    await _refreshPermissionState();
  }

  Future<void> _handleOverlayPermissionReturn() async {
    await _refreshPermissionState();
    if (!mounted ||
        _overlayPermissionMissing ||
        !(_orch.phase.isRunning ||
            _awaitingClarificationResponse ||
            _awaitingPostCompletionInstruction)) {
      return;
    }
    await _syncNavigationOverlay();
  }

  Future<void> _returnToAppAfterConversation() async {
    if (!mounted) {
      return;
    }

    await _overlayService.hide();
    _overlayVisible = false;
    await _orch.endConversationLoop();
    _resetConversationWaitState();

    if (!mounted) {
      return;
    }

    final navigator = Navigator.of(context, rootNavigator: true);
    if (navigator.canPop()) {
      navigator.popUntil((route) => route.isFirst);
    }

    final lifecycleState = WidgetsBinding.instance.lifecycleState;
    if (lifecycleState != AppLifecycleState.resumed && !_bringingAppToFront) {
      _bringingAppToFront = true;
      await _overlayService.bringToFront();
      _bringingAppToFront = false;
    }
  }

  Future<void> _syncNavigationOverlay() async {
    if (!_overlayService.isSupported) {
      return;
    }

    if (_orch.phase.isRunning ||
        _awaitingClarificationResponse ||
        _awaitingPostCompletionInstruction) {
      final hasPermission = await _overlayService.hasPermission();
      if (!hasPermission) {
        _overlayVisible = false;
        return;
      }
      await _overlayService.show(
        title: _overlayTitle(),
        message: _overlayMessage(),
      );
      _overlayVisible = true;
      return;
    }

    if (_overlayVisible) {
      await _overlayService.hide();
      _overlayVisible = false;
    }
  }

  Future<void> _showStartupOverlayIfPossible(String prompt) async {
    if (_overlayPermissionMissing || !_overlayService.isSupported) {
      return;
    }
    await _overlayService.show(
      title: 'Launching phone command',
      message: prompt.isNotEmpty
          ? 'Starting "$prompt" on the phone now.'
          : 'Starting the phone command now.',
    );
    _overlayVisible = true;
  }

  String _intentSummaryText() {
    final intent = _orch.parsedIntent ?? const <String, dynamic>{};
    final goal = (intent['goal'] ?? '').toString().trim();
    if (goal.isNotEmpty) {
      return goal;
    }
    final app = (intent['target_app'] ?? intent['app_package'] ?? '')
        .toString()
        .trim();
    return app;
  }

  String _overlayTitle() {
    if (_awaitingClarificationResponse) {
      return 'Need one more detail';
    }
    if (_awaitingPostCompletionInstruction) {
      return _postTaskPhase == PipelinePhase.completed
          ? 'Phone task complete'
          : 'Phone task update';
    }
    return _guardTitle();
  }

  String _overlayMessage() {
    if (_awaitingClarificationResponse || _awaitingPostCompletionInstruction) {
      return _statusText();
    }
    return _guardBody().isNotEmpty ? _guardBody() : _statusText();
  }

  bool get _showPhoneGuard => _orch.phase.isRunning;

  String _guardTitle() {
    switch (_orch.phase) {
      case PipelinePhase.creatingSession:
      case PipelinePhase.parsingIntent:
        return 'Preparing the phone command';
      case PipelinePhase.executing:
        return 'Phone control in progress';
      case PipelinePhase.awaitingConfirmation:
        return 'Waiting on the phone';
      case PipelinePhase.idle:
      case PipelinePhase.completed:
      case PipelinePhase.failed:
      case PipelinePhase.cancelled:
        return 'Phone command';
    }
  }

  String _guardBody() {
    switch (_orch.phase) {
      case PipelinePhase.creatingSession:
        return 'The model is preparing the session and getting ready to access the phone. Please wait a moment.';
      case PipelinePhase.parsingIntent:
        return 'The prompt is being interpreted right now. Keep the device steady so the command can start cleanly.';
      case PipelinePhase.executing:
        return 'Hello Again is currently working through the phone. Please do not tap, swipe, type, or switch apps until it finishes.';
      case PipelinePhase.awaitingConfirmation:
        return 'The model is waiting at a phone confirmation step. Avoid touching the phone unless you intentionally want to approve the action.';
      case PipelinePhase.idle:
      case PipelinePhase.completed:
      case PipelinePhase.failed:
      case PipelinePhase.cancelled:
        return '';
    }
  }

  Future<void> _ensureHandsFreeVoiceReady() async {
    if (_voiceController.enabled || _voiceStarting) {
      return;
    }
    _voiceStarting = true;
    try {
      await _voiceController.start();
      _voiceError = null;
    } catch (error) {
      _voiceError = error.toString();
    } finally {
      _voiceStarting = false;
      if (mounted) {
        setState(() {});
      }
      unawaited(_syncNavigationOverlay());
    }
  }

  Future<void> _toggleHandsFreeVoice() async {
    if (_voiceController.enabled) {
      await _voiceController.stop();
      return;
    }
    await _ensureHandsFreeVoiceReady();
  }

  Future<void> _handleVoiceTranscript(String transcript) async {
    final spokenText = transcript.trim();
    if (spokenText.isEmpty) {
      return;
    }

    if (_awaitingClarificationResponse) {
      await _handleClarificationTranscript(spokenText);
      return;
    }

    if (_awaitingPostCompletionInstruction) {
      await _handlePostCompletionTranscript(spokenText);
      return;
    }

    if (_orch.phase == PipelinePhase.awaitingConfirmation &&
        _orch.pendingConfirmation != null) {
      await _handleVoiceConfirmation(spokenText);
      return;
    }

    if (_orch.phase.isRunning) {
      await _handleRunningVoiceInterrupt(spokenText);
      return;
    }

    _promptController.value = TextEditingValue(
      text: spokenText,
      selection: TextSelection.collapsed(offset: spokenText.length),
    );
    await _startCommand(promptOverride: spokenText);
  }

  Future<void> _beginClarification(String prompt, String question) async {
    setState(() {
      _awaitingClarificationResponse = true;
      _pendingClarificationPrompt = prompt;
      _pendingClarificationQuestion = question;
    });
    await _syncNavigationOverlay();

    if (_voiceController.enabled) {
      await _voiceController.speakText(question, resumeWhenDone: true);
      return;
    }

    if (WidgetsBinding.instance.lifecycleState != AppLifecycleState.resumed) {
      await _overlayService.bringToFront();
    }
  }

  Future<void> _handleClarificationTranscript(String transcript) async {
    final basePrompt = (_pendingClarificationPrompt ?? _lastSubmittedPrompt)
        .trim();
    final mergedPrompt = _mergePromptWithClarification(basePrompt, transcript);
    _resetConversationWaitState();
    _promptController.value = TextEditingValue(
      text: mergedPrompt,
      selection: TextSelection.collapsed(offset: mergedPrompt.length),
    );
    await _voiceController.pauseForTask(
      status: 'Обновявам командата с вашия отговор...',
    );
    await _startCommand(promptOverride: mergedPrompt);
  }

  Future<void> _enterPostCompletionConversation(PipelinePhase phase) async {
    if (!mounted) {
      return;
    }

    setState(() {
      _awaitingPostCompletionInstruction = true;
      _postTaskPhase = phase;
      _postTaskStatusMessage = 'Подготвям следващия отговор...';
    });
    await _syncNavigationOverlay();

    if (_voiceController.enabled) {
      await _voiceController.pauseForTask(
        status: 'Подготвям следващия отговор...',
      );
    }

    final terminalResponse = await _fetchTerminalResponse(phase);
    if (!mounted || !_awaitingPostCompletionInstruction) {
      return;
    }

    final terminalPrompt = (terminalResponse['message'] ?? '')
        .toString()
        .trim();
    final statusLine = (terminalResponse['status_line'] ?? terminalPrompt)
        .toString()
        .trim();
    final spokenPrompt = terminalPrompt.isNotEmpty
        ? terminalPrompt
        : statusLine;

    setState(() {
      _lastTerminalMessage = spokenPrompt;
      _postTaskStatusMessage = statusLine.isNotEmpty
          ? statusLine
          : spokenPrompt;
    });
    await _syncNavigationOverlay();
    if (_voiceController.enabled) {
      await _voiceController.speakText(spokenPrompt, resumeWhenDone: true);
      return;
    }

    if (WidgetsBinding.instance.lifecycleState != AppLifecycleState.resumed) {
      await _overlayService.bringToFront();
    }
  }

  Future<void> _handlePostCompletionTranscript(String transcript) async {
    final followUpContext = await _buildFollowUpContext();
    final decision = await _decidePostTaskAction(
      transcript,
      context: followUpContext,
    );
    final action = (decision['decision'] ?? '').toString().trim();
    final replyMessage = (decision['reply_message'] ?? '').toString().trim();

    if (action == 'return_to_app') {
      if (replyMessage.isNotEmpty) {
        await _voiceController.speakText(replyMessage);
      }
      await _returnToAppAfterConversation();
      return;
    }

    if (action == 'ask_for_clarification') {
      if (replyMessage.isNotEmpty) {
        await _voiceController.speakText(replyMessage, resumeWhenDone: true);
      } else {
        await _voiceController.resumeListening();
      }
      return;
    }

    final nextInstruction = (decision['next_instruction'] ?? transcript)
        .toString()
        .trim();
    _resetConversationWaitState();
    _promptController.value = TextEditingValue(
      text: nextInstruction,
      selection: TextSelection.collapsed(offset: nextInstruction.length),
    );
    await _voiceController.pauseForTask(
      status: replyMessage.isNotEmpty
          ? replyMessage
          : 'Подготвям следващата задача на телефона...',
    );
    await _startCommand(
      promptOverride: nextInstruction,
      contextOverride: followUpContext,
    );
  }

  Future<Map<String, dynamic>> _buildFollowUpContext() async {
    final intent = _orch.parsedIntent ?? const <String, dynamic>{};
    final entities =
        (intent['entities'] as Map?)?.cast<String, dynamic>() ??
        const <String, dynamic>{};
    final context = <String, dynamic>{
      'follow_up_mode': true,
      'previous_goal': (intent['goal'] ?? '').toString().trim(),
      'previous_app_package': (intent['app_package'] ?? '').toString().trim(),
      'previous_app_name': (intent['target_app'] ?? '').toString().trim(),
      'previous_entities': Map<String, dynamic>.from(entities),
    };

    try {
      final screenState = await _deviceGateway.getScreenState();
      final currentPackage = (screenState.foregroundPackage ?? '').trim();
      final currentTitle = (screenState.windowTitle ?? '').trim();
      if (currentPackage.isNotEmpty) {
        context['current_app_package'] = currentPackage;
      }
      if (currentTitle.isNotEmpty) {
        context['current_window_title'] = currentTitle;
      }
    } catch (_) {}

    if ((context['current_app_package'] ?? '').toString().trim().isEmpty) {
      final previousPackage = (context['previous_app_package'] ?? '')
          .toString()
          .trim();
      if (previousPackage.isNotEmpty) {
        context['current_app_package'] = previousPackage;
      }
    }

    if ((context['current_app_name'] ?? '').toString().trim().isEmpty) {
      final previousName = (context['previous_app_name'] ?? '')
          .toString()
          .trim();
      if (previousName.isNotEmpty) {
        context['current_app_name'] = previousName;
      }
    }

    return context;
  }

  Future<Map<String, dynamic>> _decidePostTaskAction(
    String transcript, {
    required Map<String, dynamic> context,
  }) async {
    final sessionId = _orch.sessionId;
    if (sessionId == null || sessionId.isEmpty) {
      return {
        'decision': 'ask_for_clarification',
        'reply_message':
            'Не успях да обработя отговора ви. Кажете отново дали да продължим на телефона или да се върнем в приложението.',
        'next_instruction': '',
      };
    }

    try {
      final response = await _orch.client.decidePostTaskAction(
        sessionId,
        transcript: transcript,
        phase: _terminalPhaseName(_postTaskPhase ?? _orch.phase),
        currentAppPackage: (context['current_app_package'] ?? '')
            .toString()
            .trim(),
        currentAppName: (context['current_app_name'] ?? '').toString().trim(),
        currentWindowTitle: (context['current_window_title'] ?? '')
            .toString()
            .trim(),
        lastAssistantMessage:
            (_lastTerminalMessage ?? _postTaskStatusMessage ?? '').trim(),
      );
      final decision = (response['decision'] ?? '').toString().trim();
      if (decision.isNotEmpty) {
        return response;
      }
    } catch (_) {}

    return {
      'decision': 'ask_for_clarification',
      'reply_message':
          'Не успях да обработя отговора ви. Кажете отново дали да продължим на телефона или да се върнем в приложението.',
      'next_instruction': '',
    };
  }

  Future<void> _handleVoiceConfirmation(String transcript) async {
    final decision = _parseVoiceDecision(transcript);
    switch (decision) {
      case _VoiceDecision.approve:
        await _voiceController.pauseForTask(
          status: 'Продължавам със стъпката за потвърждение...',
        );
        await _voiceController.speakText('Продължавам.');
        await _orch.approveConfirmation();
        return;
      case _VoiceDecision.reject:
        await _voiceController.pauseForTask(
          status: 'Спирам тази стъпка за потвърждение...',
        );
        await _orch.rejectConfirmation();
        return;
      case _VoiceDecision.unknown:
        await _voiceController.speakText(
          'Кажете да, за да продължа, или не, за да спра.',
          resumeWhenDone: true,
        );
        return;
    }
  }

  Future<void> _handleRunningVoiceInterrupt(String transcript) async {
    final normalized = _normalizeVoiceText(transcript);
    if (_matchesAny(normalized, const [
      'cancel',
      'stop',
      'never mind',
      'pause',
      'hold on',
      'wait',
      'откажи',
      'спри',
      'пауза',
      'изчакай',
    ])) {
      await _voiceController.speakText(
        normalized.contains('pause') ||
                normalized.contains('hold on') ||
                normalized.contains('wait')
            ? 'Поставям текущата задача на пауза.'
            : 'Отменям текущата задача на телефона.',
      );
      if (normalized.contains('pause') ||
          normalized.contains('hold on') ||
          normalized.contains('wait')) {
        await _orch.pause();
      } else {
        await _orch.cancel();
      }
      return;
    }
    if (_matchesAny(normalized, const [
      'status',
      'what are you doing',
      'progress',
      'статус',
      'какво правиш',
      'докъде стигна',
    ])) {
      final status = _orch.currentReasoning.trim();
      await _voiceController.speakText(
        status.isEmpty ? 'Все още работя по задачата на телефона.' : status,
      );
    }
  }

  String? _preparedClarificationQuestion() {
    final intent = _orch.parsedIntent ?? const <String, dynamic>{};
    final explicitNeeds = _readBool(intent['needs_clarification']);
    final explicitQuestion = (intent['clarification_question'] ?? '')
        .toString()
        .trim();
    if (!explicitNeeds) {
      return null;
    }
    if (explicitQuestion.isNotEmpty) {
      return explicitQuestion;
    }
    return 'Трябва ми още една подробност, за да продължа.';
  }

  String _mergePromptWithClarification(String basePrompt, String answer) {
    final cleanBase = basePrompt.trim();
    final cleanAnswer = answer.trim();
    if (cleanBase.isEmpty) {
      return cleanAnswer;
    }
    if (cleanAnswer.isEmpty) {
      return cleanBase;
    }
    return '$cleanBase. $cleanAnswer';
  }

  Future<Map<String, dynamic>> _fetchTerminalResponse(
    PipelinePhase phase,
  ) async {
    final sessionId = _orch.sessionId;
    if (sessionId == null || sessionId.isEmpty) {
      return _genericTerminalResponseFallback(phase);
    }

    try {
      final response = await _orch.client.getTerminalResponse(
        sessionId,
        phase: _terminalPhaseName(phase),
        errorMessage: _orch.errorMessage ?? '',
        currentReasoning: _orch.currentReasoning,
      );
      final message = (response['message'] ?? '').toString().trim();
      final statusLine = (response['status_line'] ?? '').toString().trim();
      if (message.isEmpty && statusLine.isEmpty) {
        return _genericTerminalResponseFallback(phase);
      }
      return response;
    } catch (_) {
      return _genericTerminalResponseFallback(phase);
    }
  }

  Map<String, dynamic> _genericTerminalResponseFallback(PipelinePhase phase) {
    const message =
        'Задачата приключи. Можете да дадете нова инструкция за телефона или да поискате връщане към основното приложение.';
    return {
      'phase': _terminalPhaseName(phase),
      'message': message,
      'status_line': message,
    };
  }

  void _resetConversationWaitState() {
    if (!mounted) {
      _awaitingClarificationResponse = false;
      _awaitingPostCompletionInstruction = false;
      _pendingClarificationPrompt = null;
      _pendingClarificationQuestion = null;
      _postTaskPhase = null;
      _postTaskStatusMessage = null;
      _lastTerminalMessage = null;
      return;
    }
    setState(() {
      _awaitingClarificationResponse = false;
      _awaitingPostCompletionInstruction = false;
      _pendingClarificationPrompt = null;
      _pendingClarificationQuestion = null;
      _postTaskPhase = null;
      _postTaskStatusMessage = null;
      _lastTerminalMessage = null;
    });
    unawaited(_syncNavigationOverlay());
  }

  bool _isTerminalPhase(PipelinePhase phase) =>
      phase == PipelinePhase.completed ||
      phase == PipelinePhase.failed ||
      phase == PipelinePhase.cancelled;

  String _terminalPhaseName(PipelinePhase phase) {
    switch (phase) {
      case PipelinePhase.completed:
        return 'completed';
      case PipelinePhase.failed:
        return 'failed';
      case PipelinePhase.cancelled:
        return 'cancelled';
      case PipelinePhase.creatingSession:
      case PipelinePhase.parsingIntent:
      case PipelinePhase.executing:
      case PipelinePhase.awaitingConfirmation:
      case PipelinePhase.idle:
        return 'completed';
    }
  }

  _VoiceDecision _parseVoiceDecision(String transcript) {
    final normalized = _normalizeVoiceText(transcript);
    if (_matchesAny(normalized, const [
      'yes',
      'да',
      'approve',
      'confirm',
      'continue',
      'продължи',
      'done',
      'готово',
      'that is all',
      'that s all',
      'all done',
      'finished',
    ])) {
      return _VoiceDecision.approve;
    }
    if (_matchesAny(normalized, const [
      'no',
      'не',
      'reject',
      'cancel',
      'stop',
      'откажи',
      'спри',
      'not yet',
    ])) {
      return _VoiceDecision.reject;
    }
    return _VoiceDecision.unknown;
  }

  String _normalizeVoiceText(String value) => value
      .toLowerCase()
      .replaceAll(RegExp(r'[^0-9a-zа-я\s]'), ' ')
      .replaceAll(RegExp(r'\s+'), ' ')
      .trim();

  bool _matchesAny(String normalized, List<String> phrases) {
    for (final phrase in phrases) {
      if (normalized == phrase || normalized.contains(phrase)) {
        return true;
      }
    }
    return false;
  }

  bool _readBool(Object? value) {
    if (value is bool) {
      return value;
    }
    final lowered = value?.toString().trim().toLowerCase();
    return lowered == 'true' ||
        lowered == '1' ||
        lowered == 'yes' ||
        lowered == 'да';
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final scheme = theme.colorScheme;
    final isLoading =
        _orch.phase == PipelinePhase.creatingSession ||
        _orch.phase == PipelinePhase.parsingIntent ||
        _orch.phase == PipelinePhase.executing;

    return PopScope(
      canPop: !_showPhoneGuard,
      child: Scaffold(
        backgroundColor: const Color(0xFFF6F1E8),
        appBar: AppBar(
          backgroundColor: Colors.transparent,
          elevation: 0,
          title: const Text('Phone Command'),
          actions: [
            TextButton.icon(
              onPressed: () {
                setState(() {
                  _showDebug = !_showDebug;
                });
              },
              icon: Icon(
                _showDebug ? Icons.terminal : Icons.terminal_outlined,
                color: scheme.primary,
              ),
              label: Text(
                _showDebug ? 'Hide Debug' : 'Show Debug',
                style: TextStyle(color: scheme.primary),
              ),
            ),
          ],
        ),
        body: SafeArea(
          child: Stack(
            children: [
              Center(
                child: ConstrainedBox(
                  constraints: const BoxConstraints(maxWidth: 680),
                  child: ListView(
                    padding: const EdgeInsets.fromLTRB(20, 12, 20, 28),
                    children: [
                      Container(
                        padding: const EdgeInsets.all(24),
                        decoration: BoxDecoration(
                          color: Colors.white.withValues(alpha: 0.92),
                          borderRadius: BorderRadius.circular(28),
                          border: Border.all(
                            color: Colors.black.withValues(alpha: 0.06),
                          ),
                          boxShadow: [
                            BoxShadow(
                              color: Colors.black.withValues(alpha: 0.08),
                              blurRadius: 28,
                              offset: const Offset(0, 14),
                            ),
                          ],
                        ),
                        child: Column(
                          crossAxisAlignment: CrossAxisAlignment.start,
                          children: [
                            Text(
                              'Simple phone command',
                              style: theme.textTheme.headlineSmall?.copyWith(
                                fontWeight: FontWeight.w700,
                              ),
                            ),
                            const SizedBox(height: 8),
                            Text(
                              'This is the stripped-down version of the original phone-control page. It keeps the same OpenAI command flow, but only shows the prompt, loading state, and optional debug output.',
                              style: theme.textTheme.bodyMedium?.copyWith(
                                color: Colors.black.withValues(alpha: 0.64),
                                height: 1.4,
                              ),
                            ),
                            const SizedBox(height: 22),
                            TextField(
                              controller: _promptController,
                              enabled: !isLoading,
                              minLines: 2,
                              maxLines: 4,
                              onSubmitted: (_) => _startCommand(),
                              decoration: InputDecoration(
                                hintText:
                                    'Open Chrome and search for the weather in Sofia',
                                filled: true,
                                fillColor: const Color(0xFFF9F6F0),
                                border: OutlineInputBorder(
                                  borderRadius: BorderRadius.circular(22),
                                  borderSide: BorderSide(
                                    color: Colors.black.withValues(alpha: 0.08),
                                  ),
                                ),
                                enabledBorder: OutlineInputBorder(
                                  borderRadius: BorderRadius.circular(22),
                                  borderSide: BorderSide(
                                    color: Colors.black.withValues(alpha: 0.08),
                                  ),
                                ),
                                focusedBorder: OutlineInputBorder(
                                  borderRadius: BorderRadius.circular(22),
                                  borderSide: BorderSide(
                                    color: scheme.primary,
                                    width: 1.4,
                                  ),
                                ),
                                contentPadding: const EdgeInsets.all(18),
                              ),
                            ),
                            const SizedBox(height: 16),
                            Wrap(
                              spacing: 10,
                              runSpacing: 10,
                              children: [
                                FilledButton.icon(
                                  onPressed: isLoading
                                      ? null
                                      : () => _startCommand(),
                                  icon: isLoading
                                      ? const SizedBox(
                                          width: 16,
                                          height: 16,
                                          child: CircularProgressIndicator(
                                            strokeWidth: 2,
                                          ),
                                        )
                                      : const Icon(Icons.play_arrow_rounded),
                                  label: Text(
                                    _awaitingPostCompletionInstruction
                                        ? 'Run Next Command'
                                        : 'Run Phone Command',
                                  ),
                                ),
                                OutlinedButton.icon(
                                  onPressed: _voiceStarting
                                      ? null
                                      : _toggleHandsFreeVoice,
                                  icon: Icon(
                                    _voiceController.enabled
                                        ? Icons.hearing_disabled_outlined
                                        : Icons.keyboard_voice_outlined,
                                  ),
                                  label: Text(
                                    _voiceController.enabled
                                        ? 'Stop Hands-Free Voice'
                                        : 'Start Hands-Free Voice',
                                  ),
                                ),
                                if (_awaitingPostCompletionInstruction)
                                  TextButton.icon(
                                    onPressed: _returnToAppAfterConversation,
                                    icon: const Icon(Icons.reply_rounded),
                                    label: const Text('Return To App'),
                                  ),
                              ],
                            ),
                            const SizedBox(height: 20),
                            Container(
                              padding: const EdgeInsets.symmetric(
                                horizontal: 16,
                                vertical: 14,
                              ),
                              decoration: BoxDecoration(
                                color: const Color(0xFFF7F2EA),
                                borderRadius: BorderRadius.circular(20),
                              ),
                              child: Row(
                                children: [
                                  if (isLoading)
                                    const SizedBox(
                                      width: 20,
                                      height: 20,
                                      child: CircularProgressIndicator(
                                        strokeWidth: 2.2,
                                      ),
                                    )
                                  else
                                    Icon(
                                      _orch.phase == PipelinePhase.completed
                                          ? Icons.check_circle_outline
                                          : Icons.bolt_outlined,
                                      color: scheme.primary,
                                    ),
                                  const SizedBox(width: 14),
                                  Expanded(
                                    child: Text(
                                      _statusText(),
                                      style: theme.textTheme.bodyMedium
                                          ?.copyWith(
                                            fontWeight: FontWeight.w600,
                                          ),
                                    ),
                                  ),
                                ],
                              ),
                            ),
                            const SizedBox(height: 12),
                            Container(
                              width: double.infinity,
                              padding: const EdgeInsets.all(16),
                              decoration: BoxDecoration(
                                color: const Color(0xFFEAF1F5),
                                borderRadius: BorderRadius.circular(20),
                              ),
                              child: Column(
                                crossAxisAlignment: CrossAxisAlignment.start,
                                children: [
                                  Row(
                                    children: [
                                      Icon(
                                        _voiceController.enabled
                                            ? Icons.record_voice_over_rounded
                                            : Icons.mic_off_outlined,
                                        color: scheme.primary,
                                      ),
                                      const SizedBox(width: 10),
                                      Expanded(
                                        child: Text(
                                          _voiceController.enabled
                                              ? 'Hands-free voice is active.'
                                              : _voiceStarting
                                              ? 'Starting hands-free voice...'
                                              : 'Hands-free voice is off.',
                                          style: theme.textTheme.bodyMedium
                                              ?.copyWith(
                                                fontWeight: FontWeight.w700,
                                              ),
                                        ),
                                      ),
                                    ],
                                  ),
                                  const SizedBox(height: 8),
                                  Text(
                                    _voiceController.enabled
                                        ? _voiceController.status
                                        : (_voiceError?.trim().isNotEmpty ??
                                              false)
                                        ? _voiceError!
                                        : 'Turn it on so the agent can ask follow-up questions and keep listening after each phone task finishes.',
                                    style: theme.textTheme.bodyMedium?.copyWith(
                                      color: Colors.black.withValues(
                                        alpha: 0.68,
                                      ),
                                      height: 1.4,
                                    ),
                                  ),
                                ],
                              ),
                            ),
                            if (_accessibilityPermissionMissing) ...[
                              const SizedBox(height: 10),
                              Align(
                                alignment: Alignment.centerLeft,
                                child: TextButton.icon(
                                  onPressed: () async {
                                    _awaitingAccessibilityPermissionReturn =
                                        true;
                                    await PermissionChecker.openAccessibilitySettings();
                                  },
                                  icon: const Icon(
                                    Icons.accessibility_new_rounded,
                                  ),
                                  label: const Text(
                                    'Enable Accessibility Control',
                                  ),
                                ),
                              ),
                            ],
                            if (_overlayPermissionMissing) ...[
                              const SizedBox(height: 10),
                              Align(
                                alignment: Alignment.centerLeft,
                                child: TextButton.icon(
                                  onPressed: () async {
                                    _awaitingOverlayPermissionReturn = true;
                                    await _overlayService.requestPermission();
                                  },
                                  icon: const Icon(
                                    Icons.picture_in_picture_alt_outlined,
                                  ),
                                  label: const Text('Enable Floating Bubble'),
                                ),
                              ),
                            ],
                            if (_intentSummaryText().isNotEmpty) ...[
                              const SizedBox(height: 16),
                              Container(
                                padding: const EdgeInsets.all(16),
                                decoration: BoxDecoration(
                                  color: const Color(0xFFEAF4EF),
                                  borderRadius: BorderRadius.circular(20),
                                ),
                                child: Row(
                                  children: [
                                    const Icon(Icons.task_alt_outlined),
                                    const SizedBox(width: 12),
                                    Expanded(
                                      child: Text(
                                        _intentSummaryText(),
                                        style: theme.textTheme.titleMedium
                                            ?.copyWith(
                                              fontWeight: FontWeight.w700,
                                            ),
                                      ),
                                    ),
                                  ],
                                ),
                              ),
                            ],
                          ],
                        ),
                      ),
                      if (_showDebug) ...[
                        const SizedBox(height: 16),
                        Container(
                          padding: const EdgeInsets.all(18),
                          decoration: BoxDecoration(
                            color: const Color(0xFF192127),
                            borderRadius: BorderRadius.circular(22),
                          ),
                          child: DefaultTextStyle(
                            style: const TextStyle(
                              color: Color(0xFFE8F1EE),
                              fontSize: 12.5,
                              height: 1.45,
                            ),
                            child: Column(
                              crossAxisAlignment: CrossAxisAlignment.start,
                              children: [
                                const Text(
                                  'Debug Console',
                                  style: TextStyle(
                                    fontWeight: FontWeight.w700,
                                    fontSize: 14,
                                  ),
                                ),
                                const SizedBox(height: 12),
                                Text('Phase: ${_orch.phase.label}'),
                                Text('Session: ${_orch.sessionId ?? '-'}'),
                                if (_orch.errorMessage != null)
                                  Text('Error: ${_orch.errorMessage}'),
                                const SizedBox(height: 12),
                                if (_orch.parsedIntent != null)
                                  SelectableText(
                                    const JsonEncoder.withIndent(
                                      '  ',
                                    ).convert(_orch.parsedIntent),
                                  ),
                                if (_orch.log.isNotEmpty) ...[
                                  const SizedBox(height: 12),
                                  for (final entry in _orch.log)
                                    Text(
                                      '[${entry.timeLabel}] ${entry.message}',
                                    ),
                                ],
                              ],
                            ),
                          ),
                        ),
                      ],
                    ],
                  ),
                ),
              ),
              if (_showPhoneGuard)
                Positioned.fill(
                  child: AbsorbPointer(
                    child: DecoratedBox(
                      decoration: BoxDecoration(
                        color: Colors.black.withValues(alpha: 0.34),
                      ),
                      child: Center(
                        child: Container(
                          constraints: const BoxConstraints(maxWidth: 420),
                          margin: const EdgeInsets.all(24),
                          padding: const EdgeInsets.all(24),
                          decoration: BoxDecoration(
                            color: Colors.white.withValues(alpha: 0.96),
                            borderRadius: BorderRadius.circular(28),
                            boxShadow: [
                              BoxShadow(
                                color: Colors.black.withValues(alpha: 0.16),
                                blurRadius: 30,
                                offset: const Offset(0, 14),
                              ),
                            ],
                          ),
                          child: Column(
                            mainAxisSize: MainAxisSize.min,
                            crossAxisAlignment: CrossAxisAlignment.start,
                            children: [
                              Row(
                                children: [
                                  Container(
                                    width: 48,
                                    height: 48,
                                    decoration: BoxDecoration(
                                      color: const Color(0xFFEAF4EF),
                                      borderRadius: BorderRadius.circular(16),
                                    ),
                                    child: const Padding(
                                      padding: EdgeInsets.all(12),
                                      child: CircularProgressIndicator(
                                        strokeWidth: 2.8,
                                      ),
                                    ),
                                  ),
                                  const SizedBox(width: 14),
                                  Expanded(
                                    child: Text(
                                      _guardTitle(),
                                      style: theme.textTheme.titleLarge
                                          ?.copyWith(
                                            fontWeight: FontWeight.w800,
                                          ),
                                    ),
                                  ),
                                ],
                              ),
                              const SizedBox(height: 16),
                              Text(
                                _guardBody(),
                                style: theme.textTheme.bodyLarge?.copyWith(
                                  color: Colors.black.withValues(alpha: 0.74),
                                  height: 1.45,
                                ),
                              ),
                              const SizedBox(height: 16),
                              Container(
                                width: double.infinity,
                                padding: const EdgeInsets.symmetric(
                                  horizontal: 14,
                                  vertical: 12,
                                ),
                                decoration: BoxDecoration(
                                  color: const Color(0xFFF7F2EA),
                                  borderRadius: BorderRadius.circular(18),
                                ),
                                child: Text(
                                  _statusText(),
                                  style: theme.textTheme.bodyMedium?.copyWith(
                                    fontWeight: FontWeight.w700,
                                  ),
                                ),
                              ),
                              if (_intentSummaryText().isNotEmpty) ...[
                                const SizedBox(height: 12),
                                Text(
                                  'Current prompt: ${_intentSummaryText()}',
                                  style: theme.textTheme.bodyMedium?.copyWith(
                                    color: Colors.black.withValues(alpha: 0.64),
                                  ),
                                ),
                              ],
                            ],
                          ),
                        ),
                      ),
                    ),
                  ),
                ),
            ],
          ),
        ),
      ),
    );
  }
}

enum _VoiceDecision { approve, reject, unknown }
