import 'package:flutter/foundation.dart' show AsyncCallback;
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_dotenv/flutter_dotenv.dart';

import '../api/agent_client.dart';
import '../pipeline/orchestrator.dart';
import '../pipeline/pipeline_state.dart';

class HomeScreen extends StatefulWidget {
  const HomeScreen({super.key});

  @override
  State<HomeScreen> createState() => _HomeScreenState();
}

class _HomeScreenState extends State<HomeScreen> {
  late final TextEditingController _commandCtrl;
  late final TextEditingController _urlCtrl;
  late PipelineOrchestrator _orch;
  final _scrollCtrl = ScrollController();
  bool _showUrlField = false;

  // Extra fields for WhatsApp recipient / message
  final _recipientCtrl = TextEditingController(text: 'Alex');
  final _messageCtrl = TextEditingController(text: 'Hello from HelloAgain');

  @override
  void initState() {
    super.initState();
    _commandCtrl =
        TextEditingController(text: 'Send hello to Alex on WhatsApp');
    _urlCtrl = TextEditingController(
        text: dotenv.get('API_BASE_URL', fallback: 'http://10.0.2.2:8000'));
    _orch = PipelineOrchestrator(
      client: AgentClient(baseUrl: _urlCtrl.text),
    );
    _orch.addListener(_onOrchestratorChange);
  }

  void _onOrchestratorChange() {
    // Auto-scroll log to bottom
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (_scrollCtrl.hasClients) {
        _scrollCtrl.jumpTo(_scrollCtrl.position.maxScrollExtent);
      }
    });
    // Show confirmation dialog if needed
    if (_orch.pendingConfirmation != null &&
        _orch.phase == PipelinePhase.awaitingConfirmation) {
      WidgetsBinding.instance.addPostFrameCallback((_) {
        _showConfirmationDialog(_orch.pendingConfirmation!);
      });
    }
  }

  @override
  void dispose() {
    _orch.removeListener(_onOrchestratorChange);
    _commandCtrl.dispose();
    _urlCtrl.dispose();
    _recipientCtrl.dispose();
    _messageCtrl.dispose();
    _scrollCtrl.dispose();
    super.dispose();
  }

  void _rebuildOrchestrator() {
    _orch.removeListener(_onOrchestratorChange);
    _orch = PipelineOrchestrator(
      client: AgentClient(baseUrl: _urlCtrl.text),
    );
    _orch.addListener(_onOrchestratorChange);
    setState(() {});
  }

  Future<void> _run() async {
    final command = _commandCtrl.text.trim();
    if (command.isEmpty) return;
    FocusScope.of(context).unfocus();
    await _orch.run(
      command,
      extras: {
        'recipient': _recipientCtrl.text.trim(),
        'message': _messageCtrl.text.trim(),
      },
    );
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

        return Scaffold(
          backgroundColor: cs.surface,
          appBar: AppBar(
            backgroundColor: Colors.transparent,
            elevation: 0,
            title: Row(
              children: [
                Icon(Icons.smart_toy_outlined, color: cs.primary, size: 22),
                const SizedBox(width: 8),
                Text('HelloAgain',
                    style: TextStyle(
                        color: cs.primary,
                        fontWeight: FontWeight.w800,
                        letterSpacing: -0.5)),
                const SizedBox(width: 8),
                Container(
                  padding:
                      const EdgeInsets.symmetric(horizontal: 8, vertical: 2),
                  decoration: BoxDecoration(
                    color: cs.primaryContainer,
                    borderRadius: BorderRadius.circular(20),
                  ),
                  child: Text(
                    'Text → Gestures',
                    style: TextStyle(
                        fontSize: 11,
                        color: cs.onPrimaryContainer,
                        fontWeight: FontWeight.w600),
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
                onPressed: () =>
                    setState(() => _showUrlField = !_showUrlField),
                tooltip: 'Backend URL',
              ),
            ],
          ),
          body: Column(
            children: [
              // ── URL config (collapsible) ──────────────────────────────
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
                                fontFamily: 'monospace', fontSize: 13),
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

              // ── Command input ─────────────────────────────────────────
              Padding(
                padding: const EdgeInsets.fromLTRB(16, 8, 16, 0),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    TextField(
                      controller: _commandCtrl,
                      enabled: !isRunning,
                      maxLines: 2,
                      minLines: 1,
                      textInputAction: TextInputAction.done,
                      onSubmitted: (_) => _run(),
                      decoration: InputDecoration(
                        labelText: 'What would you like to do?',
                        hintText:
                            'Send hello to Alex on WhatsApp',
                        prefixIcon: const Icon(Icons.mic_none),
                        border: OutlineInputBorder(
                          borderRadius: BorderRadius.circular(12),
                        ),
                      ),
                    ),
                    const SizedBox(height: 8),
                    Row(
                      children: [
                        Expanded(
                          child: TextField(
                            controller: _recipientCtrl,
                            enabled: !isRunning,
                            decoration: InputDecoration(
                              labelText: 'Recipient (WhatsApp)',
                              isDense: true,
                              border: OutlineInputBorder(
                                  borderRadius: BorderRadius.circular(8)),
                            ),
                          ),
                        ),
                        const SizedBox(width: 8),
                        Expanded(
                          flex: 2,
                          child: TextField(
                            controller: _messageCtrl,
                            enabled: !isRunning,
                            decoration: InputDecoration(
                              labelText: 'Message',
                              isDense: true,
                              border: OutlineInputBorder(
                                  borderRadius: BorderRadius.circular(8)),
                            ),
                          ),
                        ),
                      ],
                    ),
                  ],
                ),
              ),

              // ── Control bar ───────────────────────────────────────────
              Padding(
                padding: const EdgeInsets.all(16),
                child: _ControlBar(
                  phase: phase,
                  onRun: isRunning ? null : _run,
                  onPause: _orch.canPause ? _orch.pause : null,
                  onCancel: _orch.canCancel ? _orch.cancel : null,
                ),
              ),

              // ── Phase indicator ───────────────────────────────────────
              _PhaseIndicator(phase: phase, errorMessage: _orch.errorMessage),

              // ── Step list ─────────────────────────────────────────────
              if (_orch.steps.isNotEmpty) ...[
                Padding(
                  padding: const EdgeInsets.fromLTRB(16, 8, 16, 4),
                  child: Row(
                    children: [
                      Text('Steps',
                          style: TextStyle(
                              fontSize: 12,
                              fontWeight: FontWeight.w600,
                              color: cs.onSurface.withAlpha(160),
                              letterSpacing: 0.5)),
                      const SizedBox(width: 8),
                      Text(
                        '${_orch.steps.where((s) => s.status == StepStatus.success).length}'
                        '/${_orch.steps.length}',
                        style: TextStyle(
                            fontSize: 12, color: cs.onSurface.withAlpha(120)),
                      ),
                    ],
                  ),
                ),
                SizedBox(
                  height: 44,
                  child: ListView.separated(
                    scrollDirection: Axis.horizontal,
                    padding: const EdgeInsets.symmetric(horizontal: 16),
                    itemCount: _orch.steps.length,
                    separatorBuilder: (context, index) => const SizedBox(width: 4),
                    itemBuilder: (_, i) => _StepChip(step: _orch.steps[i]),
                  ),
                ),
              ],

              const Divider(height: 24),

              // ── Execution log ─────────────────────────────────────────
              Padding(
                padding: const EdgeInsets.fromLTRB(16, 0, 16, 4),
                child: Row(
                  children: [
                    Text('Log',
                        style: TextStyle(
                            fontSize: 12,
                            fontWeight: FontWeight.w600,
                            color: cs.onSurface.withAlpha(160),
                            letterSpacing: 0.5)),
                    const Spacer(),
                    if (_orch.log.isNotEmpty)
                      TextButton(
                        onPressed: () {
                          final text =
                              _orch.log.map((e) => '[${e.timeLabel}] ${e.message}').join('\n');
                          Clipboard.setData(ClipboardData(text: text));
                          ScaffoldMessenger.of(context).showSnackBar(
                            const SnackBar(
                                content: Text('Log copied to clipboard')),
                          );
                        },
                        style:
                            TextButton.styleFrom(padding: EdgeInsets.zero),
                        child: const Text('Copy', style: TextStyle(fontSize: 12)),
                      ),
                  ],
                ),
              ),
              Expanded(
                child: _LogView(
                  entries: _orch.log,
                  scrollController: _scrollCtrl,
                ),
              ),
            ],
          ),
        );
      },
    );
  }
}

// ── Control bar ──────────────────────────────────────────────────────────────

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
                          color: cs.onPrimary),
                    )
                  : Icon(done ? Icons.replay : Icons.play_arrow_rounded),
              label: Text(
                isRunning
                    ? phase.label
                    : done
                        ? 'Run again'
                        : 'Execute',
                style: const TextStyle(
                    fontSize: 15, fontWeight: FontWeight.w600),
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

// ── Phase indicator ───────────────────────────────────────────────────────────

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
        label = 'Waiting for your confirmation…';
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
            child: Text(label,
                style: TextStyle(color: fg, fontSize: 13, fontWeight: FontWeight.w500),
                maxLines: 2,
                overflow: TextOverflow.ellipsis),
          ),
        ],
      ),
    );
  }
}

// ── Step chip ──────────────────────────────────────────────────────────────────

class _StepChip extends StatelessWidget {
  const _StepChip({required this.step});
  final StepEntry step;

  @override
  Widget build(BuildContext context) {
    final cs = Theme.of(context).colorScheme;

    final (bg, fg, icon) = switch (step.status) {
      StepStatus.success => (Colors.green.withAlpha(25), Colors.green, Icons.check),
      StepStatus.failed =>
        (cs.errorContainer, cs.onErrorContainer, Icons.close),
      StepStatus.running => (
          cs.primaryContainer,
          cs.onPrimaryContainer,
          Icons.sync
        ),
      StepStatus.skipped => (
          cs.surfaceContainerHighest,
          cs.onSurface.withAlpha(100),
          Icons.skip_next
        ),
      StepStatus.pending => (
          cs.surfaceContainerHighest,
          cs.onSurface.withAlpha(160),
          null
        ),
    };

    return Tooltip(
      message: '${step.id}: ${step.label}',
      child: Container(
        padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 6),
        decoration: BoxDecoration(
          color: bg,
          borderRadius: BorderRadius.circular(20),
          border: step.status == StepStatus.running
              ? Border.all(color: cs.primary, width: 1.5)
              : null,
        ),
        child: Row(
          mainAxisSize: MainAxisSize.min,
          children: [
            if (icon != null) ...[
              Icon(icon, size: 14, color: fg),
              const SizedBox(width: 4),
            ],
            Text(
              step.id,
              style: TextStyle(
                  fontSize: 11,
                  color: fg,
                  fontWeight: step.status == StepStatus.running
                      ? FontWeight.w700
                      : FontWeight.w500,
                  fontFamily: 'monospace'),
            ),
          ],
        ),
      ),
    );
  }
}

// ── Log view ──────────────────────────────────────────────────────────────────

class _LogView extends StatelessWidget {
  const _LogView({
    required this.entries,
    required this.scrollController,
  });

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
            Text('Pipeline log will appear here',
                style: TextStyle(
                    color: cs.onSurface.withAlpha(80), fontSize: 13)),
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
                    fontFamily: 'monospace', fontSize: 12, height: 1.5),
                children: [
                  TextSpan(
                      text: '[${entry.timeLabel}] ',
                      style: const TextStyle(color: Color(0xFF64748B))),
                  TextSpan(text: entry.message, style: TextStyle(color: color)),
                ],
              ),
            ),
          );
        },
      ),
    );
  }
}

// ── Confirmation dialog ───────────────────────────────────────────────────────

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
      title: const Text('Confirm Action',
          textAlign: TextAlign.center,
          style: TextStyle(fontWeight: FontWeight.w700)),
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
            _Row(label: 'Message', value: conf.contentPreview),
            const SizedBox(height: 8),
          ],
          Container(
            padding: const EdgeInsets.all(12),
            decoration: BoxDecoration(
              color: cs.surfaceContainerHighest,
              borderRadius: BorderRadius.circular(8),
            ),
            child: Text(conf.actionSummary,
                style: const TextStyle(fontSize: 14)),
          ),
          const SizedBox(height: 12),
          Container(
            padding:
                const EdgeInsets.symmetric(horizontal: 10, vertical: 6),
            decoration: BoxDecoration(
              color: cs.errorContainer.withAlpha(60),
              borderRadius: BorderRadius.circular(6),
            ),
            child: Row(
              children: [
                Icon(Icons.warning_amber_outlined,
                    size: 14, color: cs.error),
                const SizedBox(width: 6),
                Expanded(
                  child: Text(
                      'This action cannot be undone. Only approve if you intended this.',
                      style:
                          TextStyle(fontSize: 11, color: cs.error)),
                ),
              ],
            ),
          ),
        ],
      ),
      actions: [
        TextButton(
          onPressed: onReject,
          child: const Text('Cancel'),
        ),
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
          child: Text(label,
              style: TextStyle(
                  fontSize: 12,
                  color: cs.onSurface.withAlpha(160),
                  fontWeight: FontWeight.w600)),
        ),
        Expanded(
          child: Text(value,
              style: const TextStyle(fontSize: 13)),
        ),
      ],
    );
  }
}
