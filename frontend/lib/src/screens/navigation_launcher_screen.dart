import 'dart:async';
import 'dart:convert';

import 'package:flutter/material.dart';

import '../api/agent_client.dart';
import '../config/backend_base_url.dart';
import '../pipeline/orchestrator.dart';
import '../pipeline/pipeline_state.dart';

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

class _NavigationLauncherScreenState extends State<NavigationLauncherScreen> {
  static const _reasoningProvider = 'openai';

  late final TextEditingController _promptController;
  late final PipelineOrchestrator _orch;
  bool _showDebug = false;

  @override
  void initState() {
    super.initState();
    _promptController = TextEditingController(
      text: widget.initialPrompt?.trim().isNotEmpty == true
          ? widget.initialPrompt!.trim()
          : '',
    );
    _orch = PipelineOrchestrator(
      client: AgentClient(baseUrl: resolveBackendBaseUrl()),
    )..addListener(_onOrchestratorChanged);

    if (widget.autoRunOnOpen) {
      WidgetsBinding.instance.addPostFrameCallback((_) {
        if (!mounted) return;
        unawaited(_startCommand());
      });
    }
  }

  @override
  void dispose() {
    _orch.removeListener(_onOrchestratorChanged);
    _promptController.dispose();
    super.dispose();
  }

  void _onOrchestratorChanged() {
    if (!mounted) return;
    setState(() {});
  }

  Future<void> _startCommand() async {
    final prompt = _promptController.text.trim();
    if (prompt.isEmpty || _orch.phase.isRunning) {
      return;
    }

    FocusScope.of(context).unfocus();
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

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final scheme = theme.colorScheme;
    final isLoading =
        _orch.phase == PipelinePhase.creatingSession ||
        _orch.phase == PipelinePhase.parsingIntent ||
        _orch.phase == PipelinePhase.executing;

    return Scaffold(
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
        child: Center(
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
                          hintText: 'Open Chrome and search for the weather in Sofia',
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
                                child: CircularProgressIndicator(strokeWidth: 2.2),
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
      ),
    );
  }
}
