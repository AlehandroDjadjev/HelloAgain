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
  const HomeScreen({
    super.key,
    this.initialCommand,
    this.autoRunOnOpen = false,
    this.startHandsFreeOnOpen = true,
  });

  final String? initialCommand;
  final bool autoRunOnOpen;
  final bool startHandsFreeOnOpen;

  @override
  State<HomeScreen> createState() => _HomeScreenState();
}

class _HomeScreenState extends State<HomeScreen> {
  static const _defaultReasoningProvider = 'openai';

  late final TextEditingController _commandCtrl;
  late final TextEditingController _urlCtrl;
  late PipelineOrchestrator _orch;
  late AgentVoiceController _voiceController;
  final _scrollCtrl = ScrollController();
  bool _showUrlField = false;
  bool _isLeavingApp = false;
  String _lastSubmittedCommand = '';
  PipelinePhase _lastObservedPhase = PipelinePhase.idle;
  String? _activeConfirmationKey;
  bool _awaitingIntentConfirmation = false;
  String? _pendingIntentSummary;

  @override
  void initState() {
    super.initState();
    final initialCommand = widget.initialCommand?.trim();
    _commandCtrl = TextEditingController(
      text: (initialCommand != null && initialCommand.isNotEmpty)
          ? initialCommand
          : 'Search up Jeffrey Epstien on Chrome',
    );
    _urlCtrl = TextEditingController(text: _resolveDefaultBaseUrl());
    _orch = PipelineOrchestrator(client: AgentClient(baseUrl: _urlCtrl.text));
    _orch.addListener(_onOrchestratorChange);
    _voiceController = _buildVoiceController(_urlCtrl.text);
    _voiceController.addListener(_onVoiceControllerChange);
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (mounted && widget.startHandsFreeOnOpen) {
        unawaited(_ensureAlwaysListening());
      }
    });
    WidgetsBinding.instance.addPostFrameCallback((_) {
      final command = widget.initialCommand?.trim() ?? '';
      if (!mounted || !widget.autoRunOnOpen || command.isEmpty) {
        return;
      }
      unawaited(_run(commandOverride: command));
    });
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
      if (_voiceController.enabled) {
        unawaited(
          _voiceController.speakText(
            _buildConfirmationSpeech(conf),
            resumeWhenDone: true,
          ),
        );
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
      language: 'bg-BG',
    );
  }

  Future<void> _run({
    String? commandOverride,
    bool conversational = false,
  }) async {
    final command = (commandOverride ?? _commandCtrl.text).trim();
    if (command.isEmpty) return;
    _lastSubmittedCommand = command;
    FocusScope.of(context).unfocus();
    await _orch.prepare(
      command,
      reasoningProvider: _defaultReasoningProvider,
    );
    if (!mounted) {
      return;
    }
    if (!_orch.hasPreparedCommand || _orch.errorMessage != null) {
      return;
    }
    final intentIssue = _preparedIntentIssue();
    if (intentIssue != null) {
      await _handleInvalidPreparedIntent(
        intentIssue,
        conversational: conversational,
      );
      return;
    }
    await _orch.executePrepared();
  }

  Future<void> _handleVoiceTranscript(String transcript) async {
    final spokenText = transcript.trim();
    if (spokenText.isEmpty) {
      return;
    }

    if (_awaitingIntentConfirmation) {
      await _handleIntentConfirmation(spokenText);
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

    if (_shouldIgnoreIdleTranscript(spokenText)) {
      return;
    }

    _commandCtrl.value = TextEditingValue(
      text: spokenText,
      selection: TextSelection.collapsed(offset: spokenText.length),
    );
    _lastSubmittedCommand = spokenText;

    await _prepareVoiceCommand(spokenText);
  }

  void _handlePhaseTransition(PipelinePhase nextPhase) {
    if (!_voiceController.enabled) {
      return;
    }
    switch (nextPhase) {
      case PipelinePhase.completed:
        unawaited(
          _voiceController.speakPrompt(
            _buildCompletionPrompt(),
            resumeWhenDone: true,
          ),
        );
        return;
      case PipelinePhase.failed:
        unawaited(
          _voiceController.speakPrompt(
            _buildFailurePrompt(),
            resumeWhenDone: true,
          ),
        );
        return;
      case PipelinePhase.cancelled:
        unawaited(
          _voiceController.speakText(
            'Заявката беше отменена.',
            resumeWhenDone: true,
          ),
        );
        return;
      default:
        return;
    }
  }

  String _buildConfirmationSpeech(ConfirmationRequest conf) {
    final summary = conf.actionSummary.trim();
    if (summary.isEmpty) {
      return 'Преди да продължа, искам потвърждение. Правилно ли разбирам, че мога да действам? Кажете да, за да продължа, или не, за да спра.';
    }
    return 'Преди да продължа, искам потвърждение. Правилно ли разбирам, че трябва да направя следното: $summary? Кажете да, за да продължа, или не, за да спра.';
  }

  String _buildIntentConfirmationSpeech() {
    final summary = (_pendingIntentSummary ?? _lastSubmittedCommand).trim();
    if (summary.isEmpty) {
      return 'Чух команда, но искам първо да потвърдя. Правилно ли разбрах какво искате да направя? Кажете да, ако съм разбрал правилно, или не, ако трябва да опитаме отново.';
    }
    return 'Правилно ли разбрах, че искате да направя следното: $summary? Кажете да, ако това е правилно, или не, ако трябва да коригирам командата.';
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

  Future<void> _ensureAlwaysListening() async {
    if (_voiceController.enabled) {
      return;
    }
    try {
      await _voiceController.start();
    } catch (error) {
      if (!mounted) {
        return;
      }
      ScaffoldMessenger.of(
        context,
      ).showSnackBar(SnackBar(content: Text(error.toString())));
    }
  }

  Future<void> _handleVoiceConfirmation(String transcript) async {
    final decision = _parseConfirmationDecision(transcript);
    switch (decision) {
      case _VoiceConfirmationDecision.approve:
        await _approveConfirmationByVoice();
        return;
      case _VoiceConfirmationDecision.reject:
        await _rejectConfirmationByVoice();
        return;
      case _VoiceConfirmationDecision.unknown:
        await _voiceController.speakText(
          'Моля, кажете да, за да продължа, или не, за да спра.',
          resumeWhenDone: true,
        );
        return;
    }
  }

  Future<void> _prepareVoiceCommand(String transcript) async {
    if (!_looksLikePotentialActionCommand(transcript)) {
      await _voiceController.speakText(
        _invalidIntentSpeech(),
        resumeWhenDone: true,
      );
      return;
    }
    await _voiceController.pauseForTask(
      status: 'Проверявам какво искате да направя...',
    );
    await _orch.prepare(
      transcript,
      reasoningProvider: _defaultReasoningProvider,
    );
    if (!mounted) {
      return;
    }
    if (!_orch.hasPreparedCommand || _orch.errorMessage != null) {
      await _voiceController.speakPrompt(
        _buildFailurePrompt(),
        resumeWhenDone: true,
      );
      return;
    }
    final intentIssue = _preparedIntentIssue();
    if (intentIssue != null) {
      await _handleInvalidPreparedIntent(intentIssue, conversational: true);
      return;
    }

    _pendingIntentSummary = _buildIntentSummary(transcript);
    _awaitingIntentConfirmation = true;
    await _voiceController.speakText(
      _buildIntentConfirmationSpeech(),
      resumeWhenDone: true,
    );
  }

  Future<void> _handleIntentConfirmation(String transcript) async {
    final decision = _parseConfirmationDecision(transcript);
    switch (decision) {
      case _VoiceConfirmationDecision.approve:
        await _approveIntentConfirmation();
        return;
      case _VoiceConfirmationDecision.reject:
        await _rejectIntentConfirmation();
        return;
      case _VoiceConfirmationDecision.unknown:
        await _voiceController.speakText(
          'Моля, кажете да, за да започна, или не, за да отменя.',
          resumeWhenDone: true,
        );
        return;
    }
  }

  Future<void> _approveConfirmationByVoice() async {
    await _voiceController.pauseForTask(status: 'Продължавам със заявката...');
    await _voiceController.speakText('Продължавам.');
    await _orch.approveConfirmation();
  }

  Future<void> _rejectConfirmationByVoice() async {
    await _voiceController.pauseForTask(status: 'Спирам заявката...');
    await _orch.rejectConfirmation();
  }

  Future<void> _handleRunningVoiceInterrupt(String transcript) async {
    final normalized = _normalizeVoiceText(transcript);
    if (_matchesAny(normalized, const [
      'cancel',
      'stop',
      'nevermind',
      'откажи',
      'спри',
      'прекрати',
    ])) {
      await _voiceController.speakText('Отменям текущата заявка.');
      await _orch.cancel();
      return;
    }
    if (_matchesAny(normalized, const [
      'pause',
      'hold on',
      'wait',
      'пауза',
      'изчакай',
      'чакай',
    ])) {
      await _voiceController.speakText('Поставям текущата заявка на пауза.');
      await _orch.pause();
      return;
    }
    if (_matchesAny(normalized, const [
      'status',
      'what are you doing',
      'статус',
      'какво правиш',
      'докъде стигна',
    ])) {
      final status = _orch.currentReasoning.trim();
      await _voiceController.speakText(
        status.isEmpty ? 'Още работя по заявката.' : status,
      );
    }
  }

  String _buildIntentSummary(String transcript) {
    final intent = _orch.parsedIntent ?? const <String, dynamic>{};
    final goal = (intent['goal'] ?? '').toString().trim();
    final app = _friendlyAppName(
      (intent['target_app'] ?? intent['app_package'] ?? '').toString(),
    ).trim();

    final parts = <String>[];
    if (goal.isNotEmpty) {
      parts.add(goal);
    } else {
      parts.add(transcript);
    }
    if (app.isNotEmpty && app != 'App') {
      parts.add('в $app');
    }
    return parts.join(', ');
  }

  String _buildCompletionPrompt() {
    final command = _lastSubmittedCommand.trim();
    if (command.isEmpty) {
      return 'Ти си гласът на телефонния агент HelloAgain. '
          'Отговори с едно кратко изречение на български, че заявката е изпълнена.';
    }
    return 'Ти си гласът на телефонния агент HelloAgain. '
        'Отговори с едно кратко изречение на български, че тази заявка е изпълнена: '
        '"$command".';
  }

  String _buildFailurePrompt() {
    final errorMessage = _orch.errorMessage?.trim() ?? '';
    if (errorMessage.isEmpty) {
      return 'Ти си гласът на телефонния агент HelloAgain. '
          'Отговори с едно кратко и спокойно изречение на български, че заявката не можа да бъде изпълнена.';
    }
    return 'Ти си гласът на телефонния агент HelloAgain. '
        'Отговори с едно кратко и спокойно изречение на български, че заявката не можа да бъде изпълнена. '
        'Причина: "$errorMessage".';
  }

  _VoiceConfirmationDecision _parseConfirmationDecision(String transcript) {
    final normalized = _normalizeVoiceText(transcript);
    if (_matchesAny(normalized, const [
      'yes',
      'approve',
      'confirm',
      'continue',
      'да',
      'добре',
      'потвърди',
      'продължи',
      'започвай',
    ])) {
      return _VoiceConfirmationDecision.approve;
    }
    if (_matchesAny(normalized, const [
      'no',
      'cancel',
      'reject',
      'stop',
      'не',
      'откажи',
      'спри',
      'не искам',
    ])) {
      return _VoiceConfirmationDecision.reject;
    }
    return _VoiceConfirmationDecision.unknown;
  }

  bool _shouldIgnoreIdleTranscript(String transcript) {
    final normalized = _normalizeVoiceText(transcript);
    if (normalized.isEmpty) {
      return true;
    }
    return _matchesAny(normalized, const [
      'thanks',
      'thank you',
      'okay',
      'ok',
      'cool',
      'great',
      'yes',
      'no',
      'благодаря',
      'добре',
      'окей',
      'да',
      'не',
    ]);
  }

  String _normalizeVoiceText(String value) => value
      .toLowerCase()
      .replaceAll(RegExp(r'[^0-9A-Za-z\u0400-\u04FF\s]'), ' ')
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

  bool _looksLikePotentialActionCommand(String transcript) {
    final normalized = _normalizeVoiceText(transcript);
    if (normalized.isEmpty || _shouldIgnoreIdleTranscript(transcript)) {
      return false;
    }
    if (RegExp(r'\b(?:https?|www)\b').hasMatch(normalized) ||
        RegExp(r'\b\S+\.(?:com|org|net|io|dev|bg)\b').hasMatch(normalized)) {
      return true;
    }
    return _matchesAny(normalized, const [
      'open',
      'launch',
      'start',
      'search',
      'find',
      'look up',
      'google',
      'navigate',
      'take me to',
      'drive to',
      'bring me to',
      'directions',
      'route',
      'send',
      'message',
      'email',
      'gmail',
      'chrome',
      'maps',
      'whatsapp',
      'browser',
      'website',
      'отвори',
      'пусни',
      'стартирай',
      'потърси',
      'търси',
      'намери',
      'изпрати',
      'съобщение',
      'имейл',
      'карти',
      'маршрут',
      'навигация',
      'заведи ме до',
      'закарай ме до',
      'отведи ме до',
      'хром',
      'браузър',
      'сайт',
      'линк',
      'уотсап',
    ]);
  }

  String? _preparedIntentIssue() {
    final intent = _orch.parsedIntent ?? const <String, dynamic>{};
    final goalType = (intent['goal_type'] ?? '').toString().trim();
    final goal = (intent['goal'] ?? '').toString().trim();
    final appPackage = (intent['app_package'] ?? '').toString().trim();
    final confidence =
        double.tryParse((intent['confidence'] ?? '').toString()) ?? 0.0;
    final ambiguityFlags = _stringValues(
      intent['ambiguity_flags'],
    ).map((value) => value.toLowerCase()).toList();
    final entities =
        (intent['entities'] as Map?)?.cast<String, dynamic>() ??
        const <String, dynamic>{};
    final destination = (entities['destination'] ?? '').toString().trim();
    final isNavigationIntent =
        (goalType == 'navigate_to' || goalType == 'start_navigation') &&
        appPackage == 'com.google.android.apps.maps' &&
        destination.isNotEmpty;

    if (goalType.isEmpty || goalType == 'invalid_request') {
      return 'The request was not parsed as a phone action command.';
    }
    if (goal.isEmpty) {
      return 'The parsed goal is too vague.';
    }
    if (appPackage.isEmpty) {
      return 'The target app is still unknown.';
    }
    if (confidence < 0.55 && !isNavigationIntent) {
      return 'The parser is not confident enough yet.';
    }
    if (!isNavigationIntent &&
        ambiguityFlags.any(
      (flag) =>
          flag.contains('not_actionable') ||
          flag.contains('unknown') ||
          flag.contains('ambiguous'),
    )) {
      return 'The parsed command is still ambiguous.';
    }
    return null;
  }

  List<String> _stringValues(Object? value) {
    if (value is List) {
      return value.map((entry) => entry.toString()).toList();
    }
    return const [];
  }

  String _invalidIntentSpeech() {
    return 'Това не звучи като ясна команда за действие на телефона. '
        'Кажете например: отвори Chrome, потърси нещо в Chrome или изпрати съобщение в WhatsApp.';
  }

  Future<void> _handleInvalidPreparedIntent(
    String issue, {
    required bool conversational,
  }) async {
    _awaitingIntentConfirmation = false;
    _pendingIntentSummary = null;
    await _orch.discardPrepared();
    if (!mounted) {
      return;
    }
    if (conversational) {
      await _voiceController.speakText(
        _invalidIntentSpeech(),
        resumeWhenDone: true,
      );
      return;
    }
    ScaffoldMessenger.of(
      context,
    ).showSnackBar(SnackBar(content: Text('Command not accepted: $issue')));
  }

  Future<void> _approveIntentConfirmation() async {
    _awaitingIntentConfirmation = false;
    _pendingIntentSummary = null;
    await _orch.discardPrepared();
    await _voiceController.pauseForTask(status: 'Започвам заявката...');
    await _voiceController.speakText('Започвам сега.');
    await _run(commandOverride: _lastSubmittedCommand, conversational: true);
  }

  Future<void> _rejectIntentConfirmation() async {
    _awaitingIntentConfirmation = false;
    _pendingIntentSummary = null;
    await _orch.discardPrepared();
    await _voiceController.speakText(
      'Добре, ще чакам нова команда.',
      resumeWhenDone: true,
    );
  }

  String _friendlyAppName(String packageName) {
    switch (packageName) {
      case 'com.android.chrome':
        return 'Chrome';
      case 'com.whatsapp':
        return 'WhatsApp';
      case 'com.google.android.apps.maps':
        return 'Google Maps';
      case 'com.google.android.gm':
        return 'Gmail';
      case 'com.supercell.brawlstars':
        return 'Brawl Stars';
      default:
        return packageName.isEmpty ? 'App' : packageName;
    }
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
                    child: const _ExecutionEngineCard(),
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

class _ExecutionEngineCard extends StatelessWidget {
  const _ExecutionEngineCard();

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
            'Execution Engine',
            style: TextStyle(
              fontSize: 13,
              fontWeight: FontWeight.w700,
              color: cs.onSurface,
            ),
          ),
          const SizedBox(height: 6),
          Text(
            'This page now runs through the OpenAI backend only. The local Qwen-style navigator path has been removed from the controls here.',
            style: TextStyle(
              fontSize: 13,
              height: 1.35,
              color: cs.onSurface.withAlpha(170),
            ),
          ),
          const SizedBox(height: 12),
          Row(
            children: [
              Icon(Icons.cloud_outlined, color: cs.primary, size: 18),
              const SizedBox(width: 8),
              Text(
                'ChatGPT / OpenAI API',
                style: TextStyle(
                  fontSize: 13,
                  fontWeight: FontWeight.w600,
                  color: cs.onSurface,
                ),
              ),
            ],
          ),
          const SizedBox(height: 10),
          Text(
            'Uses the backend OPENAI_LLM_API_KEY and OPENAI_LLM_MODEL settings for parsing and execution planning.',
            style: TextStyle(fontSize: 12, color: cs.onSurface.withAlpha(145)),
          ),
        ],
      ),
    );
  }
}

enum _VoiceConfirmationDecision { approve, reject, unknown }

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
            'Uses /transcribe for live STT, /get-response for conversational spoken updates, and /speak for exact prompts like confirmations while the phone agent keeps listening.',
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
