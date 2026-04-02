import 'dart:async';
import 'dart:convert';

import 'package:flutter/material.dart';

import '../api/agent_client.dart';
import '../config/backend_base_url.dart';
import '../pipeline/orchestrator.dart';
import '../pipeline/pipeline_state.dart';
import '../services/navigation_overlay_service.dart';

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

  late final TextEditingController _promptController;
  late final PipelineOrchestrator _orch;
  final _overlayService = const NavigationOverlayService();
  bool _showDebug = false;
  bool _overlayPermissionMissing = false;
  bool _awaitingOverlayPermissionReturn = false;
  bool _overlayVisible = false;
  bool _completionHandled = false;
  bool _bringingAppToFront = false;
  bool _pendingReturnToHome = false;
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
    unawaited(_refreshOverlayPermissionState());

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
    unawaited(_overlayService.hide());
    _promptController.dispose();
    super.dispose();
  }

  void _onOrchestratorChanged() {
    if (!mounted) return;
    if (_orch.phase != _lastObservedPhase) {
      _lastObservedPhase = _orch.phase;
      unawaited(_syncNavigationOverlay());
      if (_orch.phase == PipelinePhase.completed && !_completionHandled) {
        _pendingReturnToHome = true;
        unawaited(_finishCompletedFlow());
      }
    }
    setState(() {});
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    if (state != AppLifecycleState.resumed) {
      return;
    }
    if (_awaitingOverlayPermissionReturn) {
      _awaitingOverlayPermissionReturn = false;
      unawaited(_handleOverlayPermissionReturn());
    }
    if (_pendingReturnToHome) {
      unawaited(_finishCompletedFlow());
    }
  }

  Future<void> _startCommand() async {
    final prompt = _promptController.text.trim();
    if (prompt.isEmpty || _orch.phase.isRunning) {
      return;
    }

    FocusScope.of(context).unfocus();
    await _refreshOverlayPermissionState();
    await _showStartupOverlayIfPossible(prompt);
    await _runCommandFlow(prompt);
  }

  Future<void> _runCommandFlow(String prompt) async {
    await _orch.preparePhoneCommand(
      prompt,
      reasoningProvider: _reasoningProvider,
    );
    if (!mounted) return;
    if (!_orch.hasPreparedCommand || _orch.errorMessage != null) {
      return;
    }
    await _orch.executePrepared();
  }

  String _statusText() {
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
        return _orch.errorMessage ?? 'The command could not be started.';
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

  Future<void> _refreshOverlayPermissionState() async {
    if (!_overlayService.isSupported) {
      return;
    }
    final granted = await _overlayService.hasPermission();
    if (!mounted) return;
    setState(() {
      _overlayPermissionMissing = !granted;
    });
  }

  Future<void> _handleOverlayPermissionReturn() async {
    await _refreshOverlayPermissionState();
    if (!mounted || _overlayPermissionMissing || !_orch.phase.isRunning) {
      return;
    }
    await _syncNavigationOverlay();
  }

  Future<void> _finishCompletedFlow() async {
    if (!mounted || _completionHandled || _orch.phase != PipelinePhase.completed) {
      return;
    }

    await _overlayService.hide();
    _overlayVisible = false;

    final lifecycleState = WidgetsBinding.instance.lifecycleState;
    if (lifecycleState != AppLifecycleState.resumed) {
      if (!_bringingAppToFront) {
        _bringingAppToFront = true;
        await _overlayService.bringToFront();
        _bringingAppToFront = false;
      }
      return;
    }

    _completionHandled = true;
    _pendingReturnToHome = false;

    if (!mounted) {
      return;
    }

    Navigator.of(context, rootNavigator: true).popUntil((route) => route.isFirst);
  }

  Future<void> _syncNavigationOverlay() async {
    if (!_overlayService.isSupported) {
      return;
    }

    if (_orch.phase.isRunning) {
      final hasPermission = await _overlayService.hasPermission();
      if (!hasPermission) {
        _overlayVisible = false;
        return;
      }
      await _overlayService.show(
        title: _guardTitle(),
        message: _guardBody().isNotEmpty ? _guardBody() : _statusText(),
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
                                      style: theme.textTheme.bodyMedium?.copyWith(
                                        fontWeight: FontWeight.w600,
                                      ),
                                    ),
                                  ),
                                ],
                              ),
                            ),
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
                                        style: theme.textTheme.titleMedium?.copyWith(
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
                                    Text('[${entry.timeLabel}] ${entry.message}'),
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
                                      style: theme.textTheme.titleLarge?.copyWith(
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
