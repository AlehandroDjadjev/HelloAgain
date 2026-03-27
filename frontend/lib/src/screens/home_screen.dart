import 'dart:async';

import 'package:android_control_plugin/android_control_plugin.dart';
import 'package:flutter/foundation.dart' show AsyncCallback;
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import '../api/agent_client.dart';
import '../api/voice_gateway_client.dart';
import '../config/backend_base_url.dart';
import '../pipeline/orchestrator.dart';
import '../pipeline/pipeline_state.dart';
import '../voice/agent_voice_controller.dart';

class HomeScreen extends StatefulWidget {
  const HomeScreen({super.key});

  @override
  State<HomeScreen> createState() => _HomeScreenState();
}

class _HomeScreenState extends State<HomeScreen> {
  late final TextEditingController _commandCtrl;
  late final TextEditingController _urlCtrl;
  late PipelineOrchestrator _orch;
  late AgentVoiceController _voiceController;
  final _scrollCtrl = ScrollController();
  bool _showUrlField = false;
  bool _isLeavingApp = false;
  String _reasoningProvider = 'local';
  String _lastSubmittedCommand = '';
  PipelinePhase _lastObservedPhase = PipelinePhase.idle;
  String? _activeConfirmationKey;

  @override
  void initState() {
    super.initState();
    _commandCtrl = TextEditingController(
      text: 'Search up Jeffrey Epstien on Chrome',
    );
    _urlCtrl = TextEditingController(text: _resolveDefaultBaseUrl());
    _orch = PipelineOrchestrator(client: AgentClient(baseUrl: _urlCtrl.text));
    _orch.addListener(_onOrchestratorChange);
    _voiceController = _buildVoiceController(_urlCtrl.text);
    _voiceController.addListener(_onVoiceControllerChange);
  }

  String _resolveDefaultBaseUrl() {
    return resolveBackendBaseUrl();
  }

  void _onOrchestratorChange() {
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (_scrollCtrl.hasClients) {
        _scrollCtrl.jumpTo(_scrollCtrl.position.maxScrollExtent);
      }
    });
    if (_orch.pendingConfirmation != null &&
        _orch.phase == PipelinePhase.awaitingConfirmation) {
      final conf = _orch.pendingConfirmation!;
      final confirmationKey = conf.confirmationId.isNotEmpty
          ? conf.confirmationId
          : conf.stepId;
      if (_activeConfirmationKey == confirmationKey) {
        return;
      }
      _activeConfirmationKey = confirmationKey;
      WidgetsBinding.instance.addPostFrameCallback((_) {
        _showConfirmationDialog(conf);
      });
      if (_voiceController.enabled) {
        unawaited(_voiceController.speakText(_buildConfirmationSpeech(conf)));
      }
    } else if (_orch.phase != PipelinePhase.awaitingConfirmation) {
      _activeConfirmationKey = null;
    }

    if (_lastObservedPhase != _orch.phase) {
      _lastObservedPhase = _orch.phase;
      _handlePhaseTransition(_orch.phase);
    }
  }

  void _onVoiceControllerChange() {
    if (mounted) {
      setState(() {});
    }
  }

  @override
  void dispose() {
    _orch.removeListener(_onOrchestratorChange);
    _voiceController.removeListener(_onVoiceControllerChange);
    unawaited(_voiceController.stop());
    _voiceController.dispose();
    _commandCtrl.dispose();
    _urlCtrl.dispose();
    _scrollCtrl.dispose();
    super.dispose();
  }

  void _rebuildOrchestrator() {
    final shouldRestartVoice = _voiceController.enabled;
    _orch.removeListener(_onOrchestratorChange);
    _orch = PipelineOrchestrator(client: AgentClient(baseUrl: _urlCtrl.text));
    _orch.addListener(_onOrchestratorChange);
    _lastObservedPhase = _orch.phase;

    _voiceController.removeListener(_onVoiceControllerChange);
    unawaited(_voiceController.stop());
    _voiceController.dispose();
    _voiceController = _buildVoiceController(_urlCtrl.text);
    _voiceController.addListener(_onVoiceControllerChange);
    if (shouldRestartVoice) {
      unawaited(_voiceController.start());
    }
    setState(() {});
  }

  AgentVoiceController _buildVoiceController(String baseUrl) {
    return AgentVoiceController(
      client: VoiceGatewayClient(baseUrl: baseUrl),
      onTranscript: _handleVoiceTranscript,
    );
  }

  Future<void> _run({
    String? commandOverride,
    bool triggeredByVoice = false,
  }) async {
    final command = (commandOverride ?? _commandCtrl.text).trim();
    if (command.isEmpty) return;
    _lastSubmittedCommand = command;
    FocusScope.of(context).unfocus();
    if (_voiceController.enabled) {
      await _voiceController.pauseForTask(
        status: triggeredByVoice
            ? 'Running your voice command in the background...'
            : 'Running the agent command...',
      );
    }
    await _orch.run(command, reasoningProvider: _reasoningProvider);
  }

  Future<void> _handleVoiceTranscript(String transcript) async {
    if (_orch.phase.isRunning) {
      await _voiceController.speakText(
        'I am still busy with the previous request.',
        resumeWhenDone: true,
      );
      return;
    }

    _commandCtrl.value = TextEditingValue(
      text: transcript,
      selection: TextSelection.collapsed(offset: transcript.length),
    );
    _lastSubmittedCommand = transcript;

    await _voiceController.pauseForTask(
      status: 'Voice command captured. Starting the agent...',
    );
    await _voiceController.speakText('Working on it.');
    unawaited(_run(commandOverride: transcript, triggeredByVoice: true));
  }

  void _handlePhaseTransition(PipelinePhase nextPhase) {
    if (!_voiceController.enabled) {
      return;
    }
    switch (nextPhase) {
      case PipelinePhase.completed:
        unawaited(
          _voiceController.speakText(
            _buildCompletionSpeech(),
            resumeWhenDone: true,
          ),
        );
        return;
      case PipelinePhase.failed:
        unawaited(
          _voiceController.speakText(
            _buildFailureSpeech(),
            resumeWhenDone: true,
          ),
        );
        return;
      case PipelinePhase.cancelled:
        unawaited(
          _voiceController.speakText(
            'The request was cancelled.',
            resumeWhenDone: true,
          ),
        );
        return;
      default:
        return;
    }
  }

  String _buildCompletionSpeech() {
    final command = _lastSubmittedCommand.trim();
    if (command.isEmpty) {
      return 'Done. The request is complete.';
    }
    return 'Done. I finished $command.';
  }

  String _buildFailureSpeech() {
    final errorMessage = _orch.errorMessage?.trim() ?? '';
    if (errorMessage.isEmpty) {
      return 'I could not finish that request.';
    }
    return 'I ran into a problem. $errorMessage';
  }

  String _buildConfirmationSpeech(ConfirmationRequest conf) {
    final summary = conf.actionSummary.trim();
    if (summary.isEmpty) {
      return 'I need your confirmation before I continue.';
    }
    return 'I need your confirmation before I continue. $summary';
  }

  Future<void> _toggleHandsFree() async {
    try {
      if (_voiceController.enabled) {
        await _voiceController.stop();
      } else {
        await _voiceController.start();
      }
    } catch (error) {
      if (!mounted) {
        return;
      }
      ScaffoldMessenger.of(
        context,
      ).showSnackBar(SnackBar(content: Text(error.toString())));
    }
  }

  Future<void> _leaveApp() async {
    if (_isLeavingApp) return;

    FocusScope.of(context).unfocus();
    setState(() => _isLeavingApp = true);

    final result = await AndroidControl.gateway.goHome();

    if (!mounted) return;
    setState(() => _isLeavingApp = false);

    if (!result.success) {
      final message =
          result.message ??
          'Unable to leave the app right now (${result.code}).';
      ScaffoldMessenger.of(
        context,
      ).showSnackBar(SnackBar(content: Text(message)));
    }
  }

  void _showConfirmationDialog(ConfirmationRequest conf) {
    if (!mounted) return;
    showDialog<void>(
      context: context,
      barrierDismissible: false,
      builder: (ctx) => _ConfirmationDialog(
        conf: conf,
        onApprove: () {
          Navigator.of(ctx).pop();
          if (_voiceController.enabled) {
            unawaited(_voiceController.speakText('Continuing.'));
          }
          _orch.approveConfirmation();
        },
        onReject: () {
          Navigator.of(ctx).pop();
          _orch.rejectConfirmation();
        },
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    return ListenableBuilder(
      listenable: _orch,
      builder: (context, _) {
        final phase = _orch.phase;
        final isRunning = phase.isRunning;
        final cs = Theme.of(context).colorScheme;
        final currentStep = _orch.currentStep;

        return Scaffold(
          backgroundColor: cs.surface,
          appBar: AppBar(
            backgroundColor: Colors.transparent,
            elevation: 0,
            title: Row(
              children: [
                Icon(Icons.smart_toy_outlined, color: cs.primary, size: 22),
                const SizedBox(width: 8),
                Text(
                  'HelloAgain',
                  style: TextStyle(
                    color: cs.primary,
                    fontWeight: FontWeight.w800,
                    letterSpacing: -0.5,
                  ),
                ),
                const SizedBox(width: 8),
                Container(
                  padding: const EdgeInsets.symmetric(
                    horizontal: 8,
                    vertical: 2,
                  ),
                  decoration: BoxDecoration(
                    color: cs.primaryContainer,
                    borderRadius: BorderRadius.circular(20),
                  ),
                  child: Text(
                    'LLM Loop',
                    style: TextStyle(
                      fontSize: 11,
                      color: cs.onPrimaryContainer,
                      fontWeight: FontWeight.w600,
                    ),
                  ),
                ),
              ],
            ),
            actions: [
              IconButton(
                icon: Icon(
                  _showUrlField ? Icons.settings : Icons.settings_outlined,
                  color: cs.onSurface.withAlpha(160),
                ),
                onPressed: () => setState(() => _showUrlField = !_showUrlField),
                tooltip: 'Backend URL',
              ),
            ],
          ),
          body: LayoutBuilder(
            builder: (context, constraints) {
              final logHeight = constraints.maxHeight < 760
                  ? 220.0
                  : constraints.maxHeight * 0.34;

              return ListView(
                padding: const EdgeInsets.only(bottom: 16),
                children: [
                  AnimatedContainer(
                    duration: const Duration(milliseconds: 200),
                    height: _showUrlField ? 72 : 0,
                    child: SingleChildScrollView(
                      child: Padding(
                        padding: const EdgeInsets.fromLTRB(16, 0, 16, 8),
                        child: Row(
                          children: [
                            Expanded(
                              child: TextField(
                                controller: _urlCtrl,
                                style: const TextStyle(
                                  fontFamily: 'monospace',
                                  fontSize: 13,
                                ),
                                decoration: InputDecoration(
                                  labelText: 'Backend URL',
                                  isDense: true,
                                  border: OutlineInputBorder(
                                    borderRadius: BorderRadius.circular(8),
                                  ),
                                ),
                              ),
                            ),
                            const SizedBox(width: 8),
                            FilledButton.tonal(
                              onPressed: _rebuildOrchestrator,
                              child: const Text('Set'),
                            ),
                          ],
                        ),
                      ),
                    ),
                  ),
                  Padding(
                    padding: const EdgeInsets.fromLTRB(16, 8, 16, 0),
                    child: TextField(
                      controller: _commandCtrl,
                      enabled: !isRunning,
                      maxLines: 3,
                      minLines: 1,
                      textInputAction: TextInputAction.done,
                      onSubmitted: (_) => _run(),
                      decoration: InputDecoration(
                        labelText: 'What should the agent do?',
                        hintText: 'Send Alex on WhatsApp I am running late',
                        prefixIcon: const Icon(Icons.chat_bubble_outline),
                        border: OutlineInputBorder(
                          borderRadius: BorderRadius.circular(12),
                        ),
                      ),
                    ),
                  ),
                  Padding(
                    padding: const EdgeInsets.fromLTRB(16, 12, 16, 0),
                    child: _ReasoningProviderCard(
                      selectedProvider: _reasoningProvider,
                      onChanged: isRunning
                          ? null
                          : (value) =>
                                setState(() => _reasoningProvider = value),
                    ),
                  ),
                  Padding(
                    padding: const EdgeInsets.fromLTRB(16, 12, 16, 0),
                    child: _HandsFreeVoiceCard(
                      enabled: _voiceController.enabled,
                      listening: _voiceController.listening,
                      processing: _voiceController.processing,
                      speaking: _voiceController.speaking,
                      micLevel: _voiceController.micLevel,
                      status: _voiceController.status,
                      error: _voiceController.error,
                      lastTranscript: _voiceController.lastTranscript,
                      onToggle: _toggleHandsFree,
                    ),
                  ),
                  Padding(
                    padding: const EdgeInsets.all(16),
                    child: _ControlBar(
                      phase: phase,
                      onRun: isRunning ? null : () => _run(),
                      onPause: _orch.canPause ? _orch.pause : null,
                      onCancel: _orch.canCancel ? _orch.cancel : null,
                    ),
                  ),
                  Padding(
                    padding: const EdgeInsets.fromLTRB(16, 0, 16, 12),
                    child: _DebugActionsCard(
                      isLeavingApp: _isLeavingApp,
                      onLeaveApp: _isLeavingApp ? null : _leaveApp,
                    ),
                  ),
                  _PhaseIndicator(
                    phase: phase,
                    errorMessage: _orch.errorMessage,
                  ),
                  const SizedBox(height: 12),
                  _ReasoningPanel(
                    reasoning: _orch.currentReasoning,
                    currentStepType: currentStep?.type ?? '',
                  ),
                  if (_orch.steps.isNotEmpty) ...[
                    Padding(
                      padding: const EdgeInsets.fromLTRB(16, 12, 16, 4),
                      child: Row(
                        children: [
                          Text(
                            'Live Steps',
                            style: TextStyle(
                              fontSize: 12,
                              fontWeight: FontWeight.w600,
                              color: cs.onSurface.withAlpha(160),
                              letterSpacing: 0.5,
                            ),
                          ),
                          const SizedBox(width: 8),
                          Text(
                            '${_orch.steps.where((s) => s.status == StepStatus.success).length}'
                            '/${_orch.steps.length}',
                            style: TextStyle(
                              fontSize: 12,
                              color: cs.onSurface.withAlpha(120),
                            ),
                          ),
                        ],
                      ),
                    ),
                    SizedBox(
                      height: 112,
                      child: ListView.separated(
                        scrollDirection: Axis.horizontal,
                        padding: const EdgeInsets.symmetric(horizontal: 16),
                        itemCount: _orch.steps.length,
                        separatorBuilder: (_, index) =>
                            const SizedBox(width: 8),
                        itemBuilder: (_, i) => _StepChip(step: _orch.steps[i]),
                      ),
                    ),
                  ],
                  const Divider(height: 24),
                  Padding(
                    padding: const EdgeInsets.fromLTRB(16, 0, 16, 4),
                    child: Row(
                      children: [
                        Text(
                          'Execution Log',
                          style: TextStyle(
                            fontSize: 12,
                            fontWeight: FontWeight.w600,
                            color: cs.onSurface.withAlpha(160),
                            letterSpacing: 0.5,
                          ),
                        ),
                        const Spacer(),
                        if (_orch.log.isNotEmpty)
                          TextButton(
                            onPressed: () {
                              final text = _orch.log
                                  .map((e) => '[${e.timeLabel}] ${e.message}')
                                  .join('\n');
                              Clipboard.setData(ClipboardData(text: text));
                              ScaffoldMessenger.of(context).showSnackBar(
                                const SnackBar(
                                  content: Text('Log copied to clipboard'),
                                ),
                              );
                            },
                            style: TextButton.styleFrom(
                              padding: EdgeInsets.zero,
                            ),
                            child: const Text(
                              'Copy',
                              style: TextStyle(fontSize: 12),
                            ),
                          ),
                      ],
                    ),
                  ),
                  SizedBox(
                    height: logHeight,
                    child: _LogView(
                      entries: _orch.log,
                      scrollController: _scrollCtrl,
                    ),
                  ),
                ],
              );
            },
          ),
        );
      },
    );
  }
}

class _ReasoningProviderCard extends StatelessWidget {
  const _ReasoningProviderCard({
    required this.selectedProvider,
    required this.onChanged,
  });

  final String selectedProvider;
  final ValueChanged<String>? onChanged;

  @override
  Widget build(BuildContext context) {
    final cs = Theme.of(context).colorScheme;

    return Container(
      width: double.infinity,
      padding: const EdgeInsets.all(14),
      decoration: BoxDecoration(
        color: cs.surfaceContainerHigh,
        borderRadius: BorderRadius.circular(12),
        border: Border.all(color: cs.outlineVariant),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(
            'Reasoning Engine',
            style: TextStyle(
              fontSize: 13,
              fontWeight: FontWeight.w700,
              color: cs.onSurface,
            ),
          ),
          const SizedBox(height: 6),
          Text(
            'Choose whether intent parsing and step-by-step reasoning should run on the local model or through the OpenAI API.',
            style: TextStyle(
              fontSize: 13,
              height: 1.35,
              color: cs.onSurface.withAlpha(170),
            ),
          ),
          const SizedBox(height: 12),
          SegmentedButton<String>(
            segments: const [
              ButtonSegment<String>(
                value: 'local',
                icon: Icon(Icons.memory_outlined),
                label: Text('Local Model'),
              ),
              ButtonSegment<String>(
                value: 'openai',
                icon: Icon(Icons.cloud_outlined),
                label: Text('OpenAI API'),
              ),
            ],
            selected: {selectedProvider},
            onSelectionChanged: onChanged == null
                ? null
                : (selection) {
                    if (selection.isNotEmpty) {
                      onChanged!(selection.first);
                    }
                  },
          ),
          const SizedBox(height: 10),
          Text(
            selectedProvider == 'openai'
                ? 'OpenAI mode uses the backend OPENAI_LLM_API_KEY and OPENAI_LLM_MODEL settings.'
                : 'Local mode uses the backend LOCAL_LLM_PROVIDER and LOCAL_LLM_MODEL settings.',
            style: TextStyle(fontSize: 12, color: cs.onSurface.withAlpha(145)),
          ),
        ],
      ),
    );
  }
}

class _ControlBar extends StatelessWidget {
  const _ControlBar({
    required this.phase,
    required this.onRun,
    required this.onPause,
    required this.onCancel,
  });

  final PipelinePhase phase;
  final VoidCallback? onRun;
  final AsyncCallback? onPause;
  final AsyncCallback? onCancel;

  @override
  Widget build(BuildContext context) {
    final cs = Theme.of(context).colorScheme;
    final isRunning = phase.isRunning;
    final done = phase.isTerminal;

    return Row(
      children: [
        Expanded(
          flex: 3,
          child: SizedBox(
            height: 52,
            child: FilledButton.icon(
              onPressed: onRun,
              icon: isRunning
                  ? SizedBox(
                      width: 18,
                      height: 18,
                      child: CircularProgressIndicator(
                        strokeWidth: 2.5,
                        color: cs.onPrimary,
                      ),
                    )
                  : Icon(done ? Icons.replay : Icons.play_arrow_rounded),
              label: Text(
                isRunning
                    ? phase.label
                    : done
                    ? 'Run again'
                    : 'Execute',
                style: const TextStyle(
                  fontSize: 15,
                  fontWeight: FontWeight.w600,
                ),
              ),
            ),
          ),
        ),
        if (isRunning) ...[
          const SizedBox(width: 8),
          SizedBox(
            height: 52,
            child: OutlinedButton.icon(
              onPressed: onPause == null ? null : () => onPause!(),
              icon: const Icon(Icons.pause),
              label: const Text('Pause'),
            ),
          ),
          const SizedBox(width: 8),
          SizedBox(
            height: 52,
            child: OutlinedButton.icon(
              onPressed: onCancel == null ? null : () => onCancel!(),
              icon: const Icon(Icons.stop),
              label: const Text('Cancel'),
              style: OutlinedButton.styleFrom(
                foregroundColor: cs.error,
                side: BorderSide(color: cs.error.withAlpha(100)),
              ),
            ),
          ),
        ],
      ],
    );
  }
}

class _HandsFreeVoiceCard extends StatelessWidget {
  const _HandsFreeVoiceCard({
    required this.enabled,
    required this.listening,
    required this.processing,
    required this.speaking,
    required this.micLevel,
    required this.status,
    required this.error,
    required this.lastTranscript,
    required this.onToggle,
  });

  final bool enabled;
  final bool listening;
  final bool processing;
  final bool speaking;
  final double micLevel;
  final String status;
  final String? error;
  final String lastTranscript;
  final VoidCallback onToggle;

  @override
  Widget build(BuildContext context) {
    final cs = Theme.of(context).colorScheme;
    final activityLabel = speaking
        ? 'Speaking'
        : processing
        ? 'Processing'
        : listening
        ? 'Listening'
        : enabled
        ? 'Standing by'
        : 'Disabled';

    return Container(
      width: double.infinity,
      padding: const EdgeInsets.all(14),
      decoration: BoxDecoration(
        color: cs.primaryContainer.withAlpha(60),
        borderRadius: BorderRadius.circular(12),
        border: Border.all(color: cs.primary.withAlpha(70)),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              Icon(Icons.keyboard_voice_rounded, color: cs.primary, size: 20),
              const SizedBox(width: 8),
              Text(
                'Hands-Free Agent Voice',
                style: TextStyle(
                  color: cs.onSurface,
                  fontSize: 13,
                  fontWeight: FontWeight.w700,
                ),
              ),
              const Spacer(),
              FilledButton.tonalIcon(
                onPressed: onToggle,
                icon: Icon(enabled ? Icons.stop : Icons.play_arrow_rounded),
                label: Text(enabled ? 'Stop' : 'Start'),
              ),
            ],
          ),
          const SizedBox(height: 8),
          Text(
            'Uses /transcribe for live voice commands and /speak for spoken replies while the phone agent keeps running in the background.',
            style: TextStyle(
              fontSize: 13,
              height: 1.35,
              color: cs.onSurface.withAlpha(180),
            ),
          ),
          const SizedBox(height: 12),
          LinearProgressIndicator(
            value: enabled ? (micLevel == 0 ? null : micLevel) : 0,
          ),
          const SizedBox(height: 10),
          Wrap(
            spacing: 8,
            runSpacing: 8,
            children: [
              Chip(label: Text(activityLabel)),
              Chip(
                label: Text(enabled ? 'Background ready' : 'Background idle'),
              ),
            ],
          ),
          const SizedBox(height: 10),
          Text(status, style: TextStyle(fontSize: 13, color: cs.onSurface)),
          if (lastTranscript.trim().isNotEmpty) ...[
            const SizedBox(height: 8),
            Text(
              'Last transcript: $lastTranscript',
              style: TextStyle(
                fontSize: 12,
                color: cs.onSurface.withAlpha(170),
                fontStyle: FontStyle.italic,
              ),
            ),
          ],
          if (error != null && error!.trim().isNotEmpty) ...[
            const SizedBox(height: 8),
            Text(
              error!,
              style: const TextStyle(color: Colors.redAccent, fontSize: 12),
            ),
          ],
        ],
      ),
    );
  }
}

class _PhaseIndicator extends StatelessWidget {
  const _PhaseIndicator({required this.phase, this.errorMessage});

  final PipelinePhase phase;
  final String? errorMessage;

  @override
  Widget build(BuildContext context) {
    final cs = Theme.of(context).colorScheme;

    Color bg;
    Color fg;
    IconData icon;
    String label;

    switch (phase) {
      case PipelinePhase.idle:
        bg = cs.surfaceContainerHighest;
        fg = cs.onSurface.withAlpha(160);
        icon = Icons.radio_button_unchecked;
        label = 'Ready';
      case PipelinePhase.completed:
        bg = Colors.green.withAlpha(30);
        fg = Colors.green;
        icon = Icons.check_circle_outline;
        label = 'Completed successfully';
      case PipelinePhase.failed:
        bg = cs.errorContainer;
        fg = cs.onErrorContainer;
        icon = Icons.error_outline;
        label = errorMessage ?? 'Pipeline failed';
      case PipelinePhase.cancelled:
        bg = cs.surfaceContainerHighest;
        fg = cs.onSurface.withAlpha(160);
        icon = Icons.cancel_outlined;
        label = 'Cancelled';
      case PipelinePhase.awaitingConfirmation:
        bg = Colors.orange.withAlpha(30);
        fg = Colors.orange;
        icon = Icons.touch_app_outlined;
        label = 'Waiting for your confirmation...';
      default:
        bg = cs.primaryContainer.withAlpha(80);
        fg = cs.onPrimaryContainer;
        icon = Icons.autorenew;
        label = phase.label;
    }

    return Container(
      margin: const EdgeInsets.symmetric(horizontal: 16),
      padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 10),
      decoration: BoxDecoration(
        color: bg,
        borderRadius: BorderRadius.circular(10),
      ),
      child: Row(
        children: [
          Icon(icon, color: fg, size: 18),
          const SizedBox(width: 10),
          Expanded(
            child: Text(
              label,
              style: TextStyle(
                color: fg,
                fontSize: 13,
                fontWeight: FontWeight.w500,
              ),
              maxLines: 2,
              overflow: TextOverflow.ellipsis,
            ),
          ),
        ],
      ),
    );
  }
}

class _DebugActionsCard extends StatelessWidget {
  const _DebugActionsCard({
    required this.isLeavingApp,
    required this.onLeaveApp,
  });

  final bool isLeavingApp;
  final VoidCallback? onLeaveApp;

  @override
  Widget build(BuildContext context) {
    final cs = Theme.of(context).colorScheme;

    return Container(
      width: double.infinity,
      padding: const EdgeInsets.all(14),
      decoration: BoxDecoration(
        color: cs.errorContainer.withAlpha(70),
        borderRadius: BorderRadius.circular(12),
        border: Border.all(color: cs.error.withAlpha(70)),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              Icon(Icons.bug_report_outlined, color: cs.error, size: 18),
              const SizedBox(width: 8),
              Text(
                'Debug Device Actions',
                style: TextStyle(
                  color: cs.onSurface,
                  fontSize: 13,
                  fontWeight: FontWeight.w700,
                ),
              ),
            ],
          ),
          const SizedBox(height: 8),
          Text(
            'Use this test button to force Android to go to the home screen and leave HelloAgain.',
            style: TextStyle(
              fontSize: 13,
              height: 1.35,
              color: cs.onSurface.withAlpha(180),
            ),
          ),
          const SizedBox(height: 12),
          SizedBox(
            width: double.infinity,
            child: FilledButton.icon(
              onPressed: onLeaveApp,
              style: FilledButton.styleFrom(
                backgroundColor: cs.error,
                foregroundColor: cs.onError,
              ),
              icon: isLeavingApp
                  ? SizedBox(
                      width: 18,
                      height: 18,
                      child: CircularProgressIndicator(
                        strokeWidth: 2.2,
                        color: cs.onError,
                      ),
                    )
                  : const Icon(Icons.exit_to_app_rounded),
              label: Text(
                isLeavingApp ? 'Leaving App...' : 'Leave App (Go Home)',
                style: const TextStyle(fontWeight: FontWeight.w700),
              ),
            ),
          ),
        ],
      ),
    );
  }
}

class _ReasoningPanel extends StatelessWidget {
  const _ReasoningPanel({
    required this.reasoning,
    required this.currentStepType,
  });

  final String reasoning;
  final String currentStepType;

  @override
  Widget build(BuildContext context) {
    final cs = Theme.of(context).colorScheme;
    final hasReasoning = reasoning.trim().isNotEmpty;

    return Container(
      width: double.infinity,
      margin: const EdgeInsets.symmetric(horizontal: 16),
      padding: const EdgeInsets.all(14),
      decoration: BoxDecoration(
        color: cs.surfaceContainerHigh,
        borderRadius: BorderRadius.circular(12),
        border: Border.all(color: cs.outlineVariant),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(
            currentStepType.isNotEmpty
                ? 'Current Reasoning · $currentStepType'
                : 'Current Reasoning',
            style: TextStyle(
              fontSize: 12,
              fontWeight: FontWeight.w700,
              color: cs.onSurface.withAlpha(180),
              letterSpacing: 0.4,
            ),
          ),
          const SizedBox(height: 8),
          Text(
            hasReasoning
                ? reasoning
                : 'The agent will show its step-by-step reasoning here.',
            style: TextStyle(
              fontSize: 14,
              height: 1.4,
              color: hasReasoning ? cs.onSurface : cs.onSurface.withAlpha(120),
            ),
          ),
        ],
      ),
    );
  }
}

class _StepChip extends StatelessWidget {
  const _StepChip({required this.step});

  final StepEntry step;

  @override
  Widget build(BuildContext context) {
    final cs = Theme.of(context).colorScheme;

    final (bg, fg, icon) = switch (step.status) {
      StepStatus.success => (
        Colors.green.withAlpha(25),
        Colors.green,
        Icons.check,
      ),
      StepStatus.failed => (
        cs.errorContainer,
        cs.onErrorContainer,
        Icons.close,
      ),
      StepStatus.running => (
        cs.primaryContainer,
        cs.onPrimaryContainer,
        Icons.sync,
      ),
      StepStatus.skipped => (
        cs.surfaceContainerHighest,
        cs.onSurface.withAlpha(100),
        Icons.skip_next,
      ),
      StepStatus.pending => (
        cs.surfaceContainerHighest,
        cs.onSurface.withAlpha(160),
        null,
      ),
    };

    final details = step.reasoning.isNotEmpty ? step.reasoning : step.label;

    return Material(
      color: Colors.transparent,
      child: InkWell(
        borderRadius: BorderRadius.circular(16),
        onTap: () => _showStepDetails(context, details),
        child: Container(
          width: 280,
          padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 10),
          decoration: BoxDecoration(
            color: bg,
            borderRadius: BorderRadius.circular(16),
            border: step.status == StepStatus.running
                ? Border.all(color: cs.primary, width: 1.5)
                : null,
          ),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Row(
                children: [
                  if (icon != null) ...[
                    Icon(icon, size: 14, color: fg),
                    const SizedBox(width: 6),
                  ],
                  Expanded(
                    child: Text(
                      step.type,
                      style: TextStyle(
                        fontSize: 12,
                        color: fg,
                        fontWeight: FontWeight.w700,
                      ),
                      overflow: TextOverflow.ellipsis,
                    ),
                  ),
                ],
              ),
              const SizedBox(height: 8),
              Expanded(
                child: Text(
                  details,
                  maxLines: 4,
                  overflow: TextOverflow.ellipsis,
                  style: TextStyle(
                    fontSize: 12,
                    height: 1.3,
                    color: fg.withAlpha(220),
                  ),
                ),
              ),
              const SizedBox(height: 6),
              Text(
                'Tap to expand',
                style: TextStyle(
                  fontSize: 11,
                  color: fg.withAlpha(180),
                  fontWeight: FontWeight.w600,
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }

  void _showStepDetails(BuildContext context, String details) {
    final cs = Theme.of(context).colorScheme;
    showModalBottomSheet<void>(
      context: context,
      showDragHandle: true,
      builder: (context) => SafeArea(
        child: Padding(
          padding: const EdgeInsets.fromLTRB(16, 8, 16, 24),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Text(
                step.type,
                style: TextStyle(
                  fontSize: 16,
                  fontWeight: FontWeight.w800,
                  color: cs.onSurface,
                ),
              ),
              const SizedBox(height: 12),
              Text(
                details,
                style: TextStyle(
                  fontSize: 14,
                  height: 1.45,
                  color: cs.onSurface,
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }
}

class _LogView extends StatelessWidget {
  const _LogView({required this.entries, required this.scrollController});

  final List<LogEntry> entries;
  final ScrollController scrollController;

  @override
  Widget build(BuildContext context) {
    final cs = Theme.of(context).colorScheme;

    if (entries.isEmpty) {
      return Center(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            Icon(Icons.terminal, size: 32, color: cs.onSurface.withAlpha(60)),
            const SizedBox(height: 8),
            Text(
              'Execution log will appear here',
              style: TextStyle(color: cs.onSurface.withAlpha(80), fontSize: 13),
            ),
          ],
        ),
      );
    }

    return Container(
      margin: const EdgeInsets.fromLTRB(16, 0, 16, 16),
      padding: const EdgeInsets.all(12),
      decoration: BoxDecoration(
        color: const Color(0xFF0A0A0A),
        borderRadius: BorderRadius.circular(12),
        border: Border.all(color: Colors.white.withAlpha(15)),
      ),
      child: ListView.builder(
        controller: scrollController,
        itemCount: entries.length,
        itemBuilder: (_, i) {
          final entry = entries[i];
          final color = switch (entry.level) {
            LogLevel.success => const Color(0xFF4ADE80),
            LogLevel.error => const Color(0xFFF87171),
            LogLevel.warning => const Color(0xFFFBBF24),
            LogLevel.info => const Color(0xFFE2E8F0),
          };
          return Padding(
            padding: const EdgeInsets.only(bottom: 2),
            child: RichText(
              text: TextSpan(
                style: const TextStyle(
                  fontFamily: 'monospace',
                  fontSize: 12,
                  height: 1.5,
                ),
                children: [
                  TextSpan(
                    text: '[${entry.timeLabel}] ',
                    style: const TextStyle(color: Color(0xFF64748B)),
                  ),
                  TextSpan(
                    text: entry.message,
                    style: TextStyle(color: color),
                  ),
                ],
              ),
            ),
          );
        },
      ),
    );
  }
}

class _ConfirmationDialog extends StatelessWidget {
  const _ConfirmationDialog({
    required this.conf,
    required this.onApprove,
    required this.onReject,
  });

  final ConfirmationRequest conf;
  final VoidCallback onApprove;
  final VoidCallback onReject;

  @override
  Widget build(BuildContext context) {
    final cs = Theme.of(context).colorScheme;

    return AlertDialog(
      icon: Icon(Icons.touch_app, color: cs.primary, size: 32),
      title: const Text(
        'Confirm Action',
        textAlign: TextAlign.center,
        style: TextStyle(fontWeight: FontWeight.w700),
      ),
      content: Column(
        mainAxisSize: MainAxisSize.min,
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          if (conf.appName.isNotEmpty) ...[
            _Row(label: 'App', value: conf.appName),
            const SizedBox(height: 8),
          ],
          if (conf.recipient.isNotEmpty) ...[
            _Row(label: 'To', value: conf.recipient),
            const SizedBox(height: 8),
          ],
          if (conf.contentPreview.isNotEmpty) ...[
            _Row(label: 'Reason', value: conf.contentPreview),
            const SizedBox(height: 8),
          ],
          Container(
            padding: const EdgeInsets.all(12),
            decoration: BoxDecoration(
              color: cs.surfaceContainerHighest,
              borderRadius: BorderRadius.circular(8),
            ),
            child: Text(
              conf.actionSummary,
              style: const TextStyle(fontSize: 14),
            ),
          ),
          const SizedBox(height: 12),
          Container(
            padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 6),
            decoration: BoxDecoration(
              color: cs.errorContainer.withAlpha(60),
              borderRadius: BorderRadius.circular(6),
            ),
            child: Row(
              children: [
                Icon(Icons.warning_amber_outlined, size: 14, color: cs.error),
                const SizedBox(width: 6),
                Expanded(
                  child: Text(
                    'This action may be irreversible. Only approve if it matches your intent.',
                    style: TextStyle(fontSize: 11, color: cs.error),
                  ),
                ),
              ],
            ),
          ),
        ],
      ),
      actions: [
        TextButton(onPressed: onReject, child: const Text('Cancel')),
        FilledButton(
          onPressed: onApprove,
          child: const Text('Approve & Execute'),
        ),
      ],
    );
  }
}

class _Row extends StatelessWidget {
  const _Row({required this.label, required this.value});

  final String label;
  final String value;

  @override
  Widget build(BuildContext context) {
    final cs = Theme.of(context).colorScheme;
    return Row(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        SizedBox(
          width: 64,
          child: Text(
            label,
            style: TextStyle(
              fontSize: 12,
              color: cs.onSurface.withAlpha(160),
              fontWeight: FontWeight.w600,
            ),
          ),
        ),
        Expanded(child: Text(value, style: const TextStyle(fontSize: 13))),
      ],
    );
  }
}
