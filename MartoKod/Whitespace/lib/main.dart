import 'dart:async';
import 'dart:convert';
import 'dart:math' as math;
import 'dart:ui' show lerpDouble;

import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';
import 'package:http/http.dart' as http;

import 'browser_voice_bridge.dart';

void main() {
  runApp(const AgentBoardApp());
}

class AgentBoardApp extends StatelessWidget {
  const AgentBoardApp({super.key});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'Agent Space',
      debugShowCheckedModeBanner: false,
      theme: ThemeData(
        scaffoldBackgroundColor: Colors.white,
        useMaterial3: true,
      ),
      home: const AgentBoardScreen(),
    );
  }
}

class AgentBoardScreen extends StatefulWidget {
  const AgentBoardScreen({super.key});

  @override
  State<AgentBoardScreen> createState() => _AgentBoardScreenState();
}

class _AgentBoardScreenState extends State<AgentBoardScreen> {
  late final SceneController _sceneController;
  late final AgentBackendClient _backendClient;
  late final BrowserVoiceBridge _voiceBridge;
  final TextEditingController _promptController = TextEditingController();
  late final String _sessionId;
  final String _userId = 'whitespace_frontend';
  String _lastSpeech =
      'The board is ready for the whitespace conversation pipeline.';
  String _statusText = 'Loading saved board memory...';
  bool _isBusy = false;
  bool _isListening = false;
  bool _speechReady = false;
  bool _whitespaceReady = false;
  bool _voiceLoopEnabled = false;
  int _voiceLoopToken = 0;
  Future<void>? _activeSpeechPlayback;

  @override
  void initState() {
    super.initState();
    _sceneController = SceneController();
    _backendClient = AgentBackendClient();
    _voiceBridge = createBrowserVoiceBridge();
    _sessionId = 'whitespace_${DateTime.now().millisecondsSinceEpoch}';
    unawaited(_hydrateBoardFromBackend());
  }

  @override
  void dispose() {
    _voiceLoopEnabled = false;
    _voiceLoopToken += 1;
    _voiceBridge.stopRecognition();
    _voiceBridge.stopAudio();
    _sceneController.dispose();
    _promptController.dispose();
    super.dispose();
  }

  Future<void> _hydrateBoardFromBackend() async {
    try {
      final payload = await _backendClient.fetchBoardMemory();
      final boardState = Map<String, dynamic>.from(
        payload['board_state'] as Map? ?? const {},
      );
      final objects = (boardState['objects'] as List?) ?? const [];
      if (objects.isNotEmpty) {
        await _sceneController.executeCommandMap({
          'action': 'hydrateScene',
          'objects': objects,
        });
      }
      if (!mounted) return;
      setState(() {
        _statusText = objects.isEmpty
            ? 'No saved board memory yet. The board starts clean.'
            : 'Loaded saved board memory from the backend.';
      });
    } catch (error) {
      if (!mounted) return;
      setState(() {
        _statusText =
            'Backend board memory unavailable. Working locally. ${_formatError(error)}';
      });
    }
  }

  Future<void> _openObjectResult(SceneObjectData object) async {
    if (_isBusy) return;
    setState(() {
      _isBusy = true;
      _statusText = 'Opening ${object.text}...';
    });

    try {
      final payload = await _backendClient.openObject(
        object: object.toJson(),
        userId: _userId,
      );
      await _applyCommands(payload['board_commands']);
      final viewer = Map<String, dynamic>.from(
        payload['viewer'] as Map? ?? const {},
      );
      if (mounted && viewer.isNotEmpty) {
        await showDialog<void>(
          context: context,
          builder: (context) => AgentResultDialog(viewer: viewer),
        );
      }
      if (!mounted) return;
      setState(() {
        _lastSpeech = (payload['speech_response'] ?? _lastSpeech).toString();
        _statusText = (payload['found'] ?? false) == true
            ? 'Opened the stored MCP result for ${object.text}.'
            : (payload['message'] ?? 'This object has no linked result.')
                  .toString();
      });
    } catch (error) {
      if (!mounted) return;
      setState(() {
        _statusText =
            'Could not open the board object right now. ${_formatError(error)}';
      });
    } finally {
      if (mounted) {
        setState(() {
          _isBusy = false;
        });
      }
    }
  }

  Future<void> _sendPrompt() async {
    final message = _promptController.text.trim();
    if (message.isEmpty || _isBusy || _isListening) return;
    await _submitPrompt(message: message, triggeredBySpeech: false);
  }

  void _toggleVoiceLoop() {
    if (_voiceLoopEnabled) {
      _stopVoiceLoop(
        statusText: _isBusy
            ? 'Voice conversation mode will stop after the current turn.'
            : 'Voice conversation mode stopped.',
      );
      return;
    }

    if (_isBusy) return;
    if (!_voiceBridge.isSpeechRecognitionSupported) {
      setState(() {
        _statusText =
            'Chrome speech input is unavailable here. Open the web app in a supported Chrome browser.';
      });
      return;
    }

    final token = _voiceLoopToken + 1;
    setState(() {
      _voiceLoopEnabled = true;
      _voiceLoopToken = token;
      _statusText = 'Voice conversation mode is on. Listening...';
    });
    unawaited(_runVoiceLoop(token));
  }

  void _stopVoiceLoop({String? statusText}) {
    _voiceLoopEnabled = false;
    _voiceLoopToken += 1;
    _voiceBridge.stopRecognition();
    if (!mounted) return;
    setState(() {
      _isListening = false;
      if (statusText != null && statusText.isNotEmpty) {
        _statusText = statusText;
      }
    });
  }

  Future<void> _runVoiceLoop(int token) async {
    while (mounted && _voiceLoopEnabled && token == _voiceLoopToken) {
      if (_isBusy) {
        await Future<void>.delayed(const Duration(milliseconds: 180));
        continue;
      }

      if (!mounted || !_voiceLoopEnabled || token != _voiceLoopToken) {
        return;
      }

      setState(() {
        _isListening = true;
        _statusText = 'Voice conversation mode is on. Listening...';
      });

      CapturedAudioTurn capturedTurn;
      try {
        capturedTurn = await _voiceBridge.captureAudioTurn(language: 'bg-BG');
      } catch (error) {
        if (!mounted || !_voiceLoopEnabled || token != _voiceLoopToken) {
          return;
        }
        setState(() {
          _statusText = _isRecoverableListeningError(error)
              ? 'Voice conversation mode is on. Still listening...'
              : 'Speech capture failed. ${_formatError(error)}';
        });
        setState(() {
          _isListening = false;
        });
        await Future<void>.delayed(
          Duration(
            milliseconds: _isRecoverableListeningError(error) ? 250 : 900,
          ),
        );
        continue;
      }

      if (!mounted || !_voiceLoopEnabled || token != _voiceLoopToken) {
        return;
      }

      setState(() {
        _isListening = false;
        _statusText = 'Speech captured. Transcribing with the voice gateway...';
      });

      String transcript;
      try {
        transcript = await _transcribeCapturedTurn(capturedTurn);
      } catch (error) {
        if (!mounted || !_voiceLoopEnabled || token != _voiceLoopToken) {
          return;
        }
        setState(() {
          _statusText = _isRecoverableListeningError(error)
              ? 'Voice conversation mode is on. Still listening...'
              : 'Speech transcription failed. ${_formatError(error)}';
        });
        await Future<void>.delayed(
          Duration(
            milliseconds: _isRecoverableListeningError(error) ? 250 : 900,
          ),
        );
        continue;
      }
      if (!mounted || !_voiceLoopEnabled || token != _voiceLoopToken) {
        return;
      }
      _fillPromptFromTranscript(transcript);
      await _submitPrompt(message: transcript, triggeredBySpeech: true);
      await _waitForActiveSpeechPlayback();

      if (!mounted || !_voiceLoopEnabled || token != _voiceLoopToken) {
        return;
      }

      setState(() {
        _statusText =
            'Voice conversation mode is on. Listening for the next turn...';
      });
      await Future<void>.delayed(const Duration(milliseconds: 220));
    }
  }

  Future<void> _submitPrompt({
    required String message,
    required bool triggeredBySpeech,
  }) async {
    final cleanMessage = message.trim();
    if (cleanMessage.isEmpty || _isBusy) return;

    setState(() {
      _isBusy = true;
      _speechReady = false;
      _whitespaceReady = false;
      _statusText = triggeredBySpeech
          ? 'Speech captured. Starting the whitespace pipeline...'
          : 'Starting the whitespace pipeline...';
    });

    try {
      final startPayload = await _backendClient.startAgentRun(
        prompt: cleanMessage,
        boardState: _sceneController.exportStateSnapshot(),
        largestEmptySpace: _sceneController.findLargestEmptySpaceSnapshot(),
        userId: _userId,
        sessionId: _sessionId,
      );

      final runId = (startPayload['run_id'] ?? '').toString();
      if (runId.isEmpty) {
        throw StateError('Backend did not return a run id.');
      }

      if (mounted) {
        setState(() {
          _statusText = _readPendingStatusText(startPayload);
        });
      }

      await Future.wait([_waitForSpeech(runId), _waitForWhitespace(runId)]);

      if (!mounted) return;
      _promptController.clear();
    } catch (error) {
      if (!mounted) return;
      setState(() {
        _statusText = 'The whitespace request failed. ${_formatError(error)}';
      });
    } finally {
      if (mounted) {
        setState(() {
          _isBusy = false;
        });
      }
    }
  }

  Future<String> _transcribeCapturedTurn(CapturedAudioTurn capturedTurn) async {
    final payload = await _backendClient.transcribeSpeechTurn(
      audioBase64: capturedTurn.audioBase64,
      audioMimeType: capturedTurn.mimeType,
      userId: _userId,
      sessionId: _sessionId,
      language: capturedTurn.language,
    );
    final transcript = (payload['transcript'] ?? payload['message'] ?? '')
        .toString()
        .trim();
    if (transcript.isEmpty) {
      throw StateError('The voice gateway did not return a transcript.');
    }
    return transcript;
  }

  Future<void> _waitForSpeech(String runId) async {
    while (true) {
      final payload = await _backendClient.fetchAgentSpeech(runId);
      final status = (payload['status'] ?? 'running').toString();
      if (status == 'running') {
        await Future<void>.delayed(const Duration(milliseconds: 450));
        continue;
      }
      if (status != 'completed') {
        throw StateError(
          (payload['detail'] ?? 'Speech generation failed.').toString(),
        );
      }

      final speechText = (payload['assistant_text'] ?? '').toString();
      final audioBase64 = (payload['assistant_audio_base64'] ?? '').toString();
      final audioMimeType =
          (payload['assistant_audio_mime_type'] ?? 'audio/wav').toString();

      if (!mounted) return;
      setState(() {
        _speechReady = true;
        if (speechText.isNotEmpty) {
          _lastSpeech = speechText;
        }
        if (!_whitespaceReady) {
          _statusText = 'Speech is ready. Finishing the whitespace actions...';
        }
      });

      if (audioBase64.isNotEmpty) {
        _trackSpeechPlayback(
          _voiceBridge
              .playBase64Audio(
                audioBase64: audioBase64,
                mimeType: audioMimeType,
              )
              .catchError((_) {}),
        );
      }
      return;
    }
  }

  Future<void> _waitForWhitespace(String runId) async {
    while (true) {
      final payload = await _backendClient.fetchAgentWhitespace(runId);
      final status = (payload['status'] ?? 'running').toString();
      if (status == 'running') {
        await Future<void>.delayed(const Duration(milliseconds: 450));
        continue;
      }
      if (status != 'completed') {
        throw StateError(
          (payload['detail'] ?? 'Whitespace processing failed.').toString(),
        );
      }

      await _applyCommands(payload['board_commands']);
      if (!mounted) return;
      setState(() {
        _whitespaceReady = true;
        _statusText = _speechReady
            ? _readStatusText(payload)
            : 'Whitespace actions are ready. Waiting for the speech response...';
      });
      return;
    }
  }

  Future<void> _applyCommands(dynamic rawCommands) async {
    if (rawCommands is! List) return;
    for (final rawCommand in rawCommands) {
      if (rawCommand is! Map) continue;
      await _sceneController.executeCommandMap(
        Map<String, dynamic>.from(rawCommand),
      );
    }
  }

  void _fillPromptFromTranscript(String transcript) {
    _promptController.text = transcript;
    _promptController.selection = TextSelection.collapsed(
      offset: transcript.length,
    );
  }

  void _trackSpeechPlayback(Future<void> playback) {
    _activeSpeechPlayback = playback;
    unawaited(
      playback.whenComplete(() {
        if (identical(_activeSpeechPlayback, playback)) {
          _activeSpeechPlayback = null;
        }
      }),
    );
  }

  Future<void> _waitForActiveSpeechPlayback() async {
    final playback = _activeSpeechPlayback;
    if (playback == null) {
      return;
    }
    try {
      await playback;
    } catch (_) {}
    if (identical(_activeSpeechPlayback, playback)) {
      _activeSpeechPlayback = null;
    }
  }

  bool _isRecoverableListeningError(Object error) {
    final lowered = error.toString().toLowerCase();
    return lowered.contains('no speech') ||
        lowered.contains('no-speech') ||
        lowered.contains('did not return a transcript') ||
        lowered.contains('aborted') ||
        lowered.contains('audio capture aborted');
  }

  String _readStatusText(Map<String, dynamic> payload) {
    final stepOne = Map<String, dynamic>.from(
      payload['step_one'] as Map? ?? const {},
    );
    final stepTwo = Map<String, dynamic>.from(
      payload['step_two'] as Map? ?? const {},
    );
    final mcpResults = (payload['mcp_results'] as List?) ?? const [];
    final requestKind = (stepOne['request_kind'] ?? 'mixed').toString();
    final memoryType =
        ((stepTwo['memory_plan'] as Map?)?['default_memory_type'] ?? 'ram')
            .toString();
    return 'Step 1: $requestKind. MCP calls: ${mcpResults.length}. Step 2 memory: $memoryType.';
  }

  String _readPendingStatusText(Map<String, dynamic> payload) {
    final stepOne = Map<String, dynamic>.from(
      payload['step_one'] as Map? ?? const {},
    );
    final mcpResults = (payload['mcp_results'] as List?) ?? const [];
    final requestKind = (stepOne['request_kind'] ?? 'mixed').toString();
    return 'Stage 1 finished as $requestKind. MCP calls: ${mcpResults.length}. Waiting for speech and whitespace...';
  }

  String _formatError(Object error) {
    final text = error.toString().trim();
    if (text.isEmpty) {
      return 'Unknown error.';
    }
    return text;
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      body: SafeArea(
        child: AnimatedBuilder(
          animation: _sceneController,
          builder: (context, _) {
            return LayoutBuilder(
              builder: (context, constraints) {
                final boardSize = Size(
                  constraints.maxWidth,
                  constraints.maxHeight,
                );
                _sceneController.setBoardSize(boardSize);

                return Stack(
                  children: [
                    Positioned.fill(
                      child: Container(
                        color: Colors.white,
                        child: Stack(
                          children: [
                            Positioned.fill(
                              child: CustomPaint(painter: GridPainter()),
                            ),
                            ..._sceneController.objects.values.map(
                              (object) => BoardObjectWidget(
                                key: ValueKey(object.name),
                                data: object,
                                onTap: () => _openObjectResult(object),
                                onDeleteComplete: () => _sceneController
                                    .finalizeDelete(object.name),
                                onDragPositionChanged: (x, y) {
                                  _sceneController.setObjectPositionFromDrag(
                                    object.name,
                                    x,
                                    y,
                                  );
                                },
                              ),
                            ),
                          ],
                        ),
                      ),
                    ),
                    Positioned(
                      top: 18,
                      left: 18,
                      child: AgentResponseCard(
                        speech: _lastSpeech,
                        status: _statusText,
                        isBusy: _isBusy,
                      ),
                    ),
                    Positioned(
                      left: 18,
                      right: 18,
                      bottom: 18,
                      child: Row(
                        children: [
                          Expanded(
                            child: Container(
                              decoration: BoxDecoration(
                                color: Colors.white.withValues(alpha: 0.34),
                                border: Border.all(
                                  color: Colors.black.withValues(alpha: 0.18),
                                  width: 0.7,
                                ),
                              ),
                              child: TextField(
                                enabled: !_isBusy && !_isListening,
                                controller: _promptController,
                                onSubmitted: (_) => _sendPrompt(),
                                decoration: InputDecoration(
                                  hintText: _isListening
                                      ? 'Listening in Chrome...'
                                      : _voiceLoopEnabled
                                      ? 'Voice mode is on. Speak your next request...'
                                      : 'Write prompt or use the mic...',
                                  hintStyle: TextStyle(
                                    color: Colors.black.withValues(alpha: 0.45),
                                  ),
                                  border: InputBorder.none,
                                  isDense: true,
                                  contentPadding: const EdgeInsets.symmetric(
                                    horizontal: 12,
                                    vertical: 12,
                                  ),
                                ),
                              ),
                            ),
                          ),
                          const SizedBox(width: 10),
                          GestureDetector(
                            onTap: (_isBusy && !_voiceLoopEnabled)
                                ? null
                                : _toggleVoiceLoop,
                            child: Container(
                              width: 44,
                              height: 44,
                              decoration: BoxDecoration(
                                color: (_isListening || _voiceLoopEnabled)
                                    ? const Color(0xFFD64444)
                                    : const Color(0xFFCB4A4A),
                                border: Border.all(
                                  color: Colors.black.withValues(alpha: 0.18),
                                  width: 0.7,
                                ),
                              ),
                              child: Icon(
                                (_isListening || _voiceLoopEnabled)
                                    ? Icons.hearing
                                    : Icons.mic_none,
                                color: Colors.white,
                                size: 18,
                              ),
                            ),
                          ),
                          const SizedBox(width: 10),
                          GestureDetector(
                            onTap: (_isBusy || _isListening)
                                ? null
                                : _sendPrompt,
                            child: Container(
                              padding: const EdgeInsets.symmetric(
                                horizontal: 14,
                                vertical: 12,
                              ),
                              decoration: BoxDecoration(
                                color: Colors.black.withValues(alpha: 0.045),
                                border: Border.all(
                                  color: Colors.black.withValues(alpha: 0.16),
                                  width: 0.7,
                                ),
                              ),
                              child: Text(
                                _isBusy
                                    ? 'Running'
                                    : _isListening
                                    ? 'Listening'
                                    : 'Send',
                                style: TextStyle(
                                  color: Colors.black.withValues(alpha: 0.70),
                                  fontSize: 13,
                                  fontWeight: FontWeight.w500,
                                ),
                              ),
                            ),
                          ),
                        ],
                      ),
                    ),
                  ],
                );
              },
            );
          },
        ),
      ),
    );
  }
}

class AgentResponseCard extends StatelessWidget {
  const AgentResponseCard({
    super.key,
    required this.speech,
    required this.status,
    required this.isBusy,
  });

  final String speech;
  final String status;
  final bool isBusy;

  @override
  Widget build(BuildContext context) {
    return ConstrainedBox(
      constraints: const BoxConstraints(maxWidth: 340),
      child: Container(
        padding: const EdgeInsets.all(14),
        decoration: BoxDecoration(
          color: Colors.white.withValues(alpha: 0.88),
          border: Border.all(color: Colors.black.withValues(alpha: 0.12), width: 0.8),
          boxShadow: [
            BoxShadow(
              color: Colors.black.withValues(alpha: 0.08),
              blurRadius: 18,
              offset: const Offset(0, 8),
            ),
          ],
        ),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(
              isBusy ? 'Semi Agent Running' : 'Semi Agent',
              style: const TextStyle(
                fontSize: 12,
                fontWeight: FontWeight.w700,
                letterSpacing: 0.4,
              ),
            ),
            const SizedBox(height: 8),
            Text(
              speech,
              style: TextStyle(
                color: Colors.black.withValues(alpha: 0.82),
                fontSize: 13,
                fontWeight: FontWeight.w600,
                height: 1.22,
              ),
            ),
            const SizedBox(height: 10),
            Text(
              status,
              style: TextStyle(
                color: Colors.black.withValues(alpha: 0.58),
                fontSize: 11.5,
                height: 1.2,
              ),
            ),
          ],
        ),
      ),
    );
  }
}

class AgentResultDialog extends StatelessWidget {
  const AgentResultDialog({super.key, required this.viewer});

  final Map<String, dynamic> viewer;

  @override
  Widget build(BuildContext context) {
    final title = (viewer['title'] ?? 'Board result').toString();
    final summary = (viewer['summary'] ?? '').toString();
    final payload = viewer['payload'];
    final widgetType = (viewer['widget_type'] ?? '').toString();
    final jsonText = JsonEncoder.withIndent('  ').convert(payload);

    return AlertDialog(
      title: Text(title),
      content: SizedBox(
        width: 520,
        child: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            if (summary.isNotEmpty)
              Padding(
                padding: const EdgeInsets.only(bottom: 12),
                child: Text(
                  summary,
                  style: TextStyle(
                    color: Colors.black.withValues(alpha: 0.72),
                    height: 1.24,
                  ),
                ),
              ),
            if (widgetType == 'user_profile' && viewer['user'] is Map)
              SizedBox(
                width: 520,
                height: 340,
                child: SingleChildScrollView(
                  child: AgentUserProfileView(
                    user: Map<String, dynamic>.from(viewer['user'] as Map),
                  ),
                ),
              )
            else
              SizedBox(
                height: 320,
                child: SingleChildScrollView(
                  child: SelectableText(
                    jsonText,
                    style: const TextStyle(fontSize: 12, height: 1.25),
                  ),
                ),
              ),
          ],
        ),
      ),
      actions: [
        TextButton(
          onPressed: () => Navigator.of(context).pop(),
          child: const Text('Close'),
        ),
      ],
    );
  }
}

class AgentUserProfileView extends StatelessWidget {
  const AgentUserProfileView({super.key, required this.user});

  final Map<String, dynamic> user;

  @override
  Widget build(BuildContext context) {
    final description = (user['description'] ?? '').toString();
    final friendStatus = (user['friend_status'] ?? 'none').toString();
    final email = user['email']?.toString();
    final phoneNumber = user['phone_number']?.toString();
    final topTraits = (user['top_traits'] as List?) ?? const [];
    final matchSummary = user['match_summary'] is Map
        ? Map<String, dynamic>.from(user['match_summary'] as Map)
        : null;
    final whyTheyMatch = (matchSummary?['why_they_match'] as List?) ?? const [];
    final sharedInterests = (matchSummary?['shared_interests'] as List?) ?? const [];

    return Column(
      mainAxisSize: MainAxisSize.min,
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        _UserSectionLabel(
          text: 'Name',
          value: (user['display_name'] ?? user['username'] ?? 'Unknown user')
              .toString(),
        ),
        _UserSectionLabel(
          text: 'Friend status',
          value: friendStatus,
        ),
        if (description.isNotEmpty)
          _UserSectionLabel(
            text: 'Description',
            value: description,
          ),
        if (topTraits.isNotEmpty)
          _UserSectionLabel(
            text: 'Top traits',
            value: topTraits
                .map((trait) {
                  if (trait is! Map) return '';
                  return (trait['label'] ?? trait['feature'] ?? '').toString();
                })
                .where((value) => value.toString().trim().isNotEmpty)
                .join(', '),
          ),
        if (sharedInterests.isNotEmpty)
          _UserSectionLabel(
            text: 'Shared interests',
            value: sharedInterests.map((item) => item.toString()).join(', '),
          ),
        if (whyTheyMatch.isNotEmpty)
          _UserSectionLabel(
            text: 'Why this match works',
            value: whyTheyMatch.map((item) => item.toString()).join(' '),
          ),
        if (email != null && email.isNotEmpty)
          _UserSectionLabel(
            text: 'Email',
            value: email,
          ),
        if (phoneNumber != null && phoneNumber.isNotEmpty)
          _UserSectionLabel(
            text: 'Phone',
            value: phoneNumber,
          ),
      ],
    );
  }
}

class _UserSectionLabel extends StatelessWidget {
  const _UserSectionLabel({
    required this.text,
    required this.value,
  });

  final String text;
  final String value;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.only(bottom: 10),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(
            text,
            style: TextStyle(
              color: Colors.black.withOpacity(0.58),
              fontSize: 11.5,
              fontWeight: FontWeight.w700,
              letterSpacing: 0.2,
            ),
          ),
          const SizedBox(height: 4),
          Text(
            value,
            style: const TextStyle(
              fontSize: 13,
              height: 1.3,
              fontWeight: FontWeight.w600,
            ),
          ),
        ],
      ),
    );
  }
}

class AgentBackendClient {
  AgentBackendClient({String? baseUrl})
    : _baseUri = Uri.parse(baseUrl ?? _resolveDefaultBaseUrl());

  final Uri _baseUri;

  static String _resolveDefaultBaseUrl() {
    const configuredBaseUrl = String.fromEnvironment('BACKEND_BASE_URL');
    if (configuredBaseUrl.isNotEmpty) {
      return configuredBaseUrl;
    }
    const localHosts = {'localhost', '127.0.0.1', '::1', '[::1]'};
    if (!kIsWeb) {
      return 'http://127.0.0.1:8000';
    }

    final browserUri = Uri.base;
    if (localHosts.contains(browserUri.host.toLowerCase()) &&
        browserUri.port != 8000) {
      // `flutter run -d chrome` serves the app on a random port, while Django
      // listens on 8000 by default.
      return Uri(
        scheme: browserUri.scheme.isEmpty ? 'http' : browserUri.scheme,
        host: browserUri.host,
        port: 8000,
      ).toString();
    }

    return browserUri.origin;
  }

  Future<Map<String, dynamic>> fetchBoardMemory() {
    return _getJson('/api/agent/board-memory/');
  }

  Future<Map<String, dynamic>> transcribeSpeechTurn({
    required String audioBase64,
    required String audioMimeType,
    required String userId,
    required String sessionId,
    required String language,
  }) {
    return _postJson('/api/voice/transcribe/', {
      'audio_base64': audioBase64,
      'audio_mime_type': audioMimeType,
      'user_id': userId,
      'session_id': sessionId,
      'language': language,
    });
  }

  Future<Map<String, dynamic>> startAgentRun({
    required String prompt,
    required Map<String, dynamic> boardState,
    required Map<String, dynamic> largestEmptySpace,
    required String userId,
    required String sessionId,
  }) {
    return _postJson('/api/agent/run/start/', {
      'prompt': prompt,
      'board_state': boardState,
      'largest_empty_space': largestEmptySpace,
      'user_id': userId,
      'session_id': sessionId,
    });
  }

  Future<Map<String, dynamic>> fetchAgentSpeech(String runId) {
    return _getJson('/api/agent/run/$runId/speech/');
  }

  Future<Map<String, dynamic>> fetchAgentWhitespace(String runId) {
    return _getJson('/api/agent/run/$runId/whitespace/');
  }

  Future<Map<String, dynamic>> openObject({
    required Map<String, dynamic> object,
    required String userId,
  }) {
    return _postJson('/api/agent/open-object/', {
      'object': object,
      'user_id': userId,
    });
  }

  Future<Map<String, dynamic>> _getJson(String path) async {
    final response = await http.get(
      _baseUri.resolve(path),
      headers: const {'Accept': 'application/json'},
    );
    if (response.statusCode < 200 || response.statusCode >= 300) {
      throw Exception(
        'GET $path failed with ${response.statusCode}: ${response.body}',
      );
    }
    return _decodeJson(response.body);
  }

  Future<Map<String, dynamic>> _postJson(
    String path,
    Map<String, dynamic> payload,
  ) async {
    final response = await http.post(
      _baseUri.resolve(path),
      headers: const {
        'Content-Type': 'application/json; charset=utf-8',
        'Accept': 'application/json',
      },
      body: jsonEncode(payload),
    );
    if (response.statusCode < 200 || response.statusCode >= 300) {
      throw Exception(
        'POST $path failed with ${response.statusCode}: ${response.body}',
      );
    }
    return _decodeJson(response.body);
  }

  Map<String, dynamic> _decodeJson(String body) {
    final decoded = jsonDecode(body);
    if (decoded is! Map) {
      throw FormatException('Backend did not return a JSON object.');
    }
    return Map<String, dynamic>.from(decoded);
  }
}

class SceneController extends ChangeNotifier {
  SceneController();
  final Map<String, SceneObjectData> _objects = <String, SceneObjectData>{};
  final math.Random _random = math.Random();

  Size _boardSize = const Size(1000, 700);

  Map<String, SceneObjectData> get objects => _objects;

  static const List<Color> _mainColors = [
    Color(0xFFD36E6A),
    Color(0xFF6E9ACC),
    Color(0xFF6EA886),
    Color(0xFFD59667),
    Color(0xFF9A77B7),
    Color(0xFFD7C46C),
    Color(0xFF5FA39A),
    Color(0xFFC4749C),
  ];

  void setBoardSize(Size size) {
    if ((_boardSize.width - size.width).abs() < 0.1 &&
        (_boardSize.height - size.height).abs() < 0.1) {
      return;
    }
    _boardSize = size;
  }

  void setObjectPositionFromDrag(String name, double x, double y) {
    final current = _objects[name];
    if (current == null) return;

    final clampedX = x
        .clamp(0.0, math.max(0.0, _boardSize.width - current.width))
        .toDouble();
    final clampedY = y
        .clamp(0.0, math.max(0.0, _boardSize.height - current.height))
        .toDouble();

    _objects[name] = current.copyWith(x: clampedX, y: clampedY);
    notifyListeners();
  }

  Future<Map<String, dynamic>> executeJson(String rawJson) async {
    final decoded = jsonDecode(rawJson);
    if (decoded is List) {
      Map<String, dynamic> lastResult = {'ok': true, 'count': decoded.length};
      for (final item in decoded) {
        lastResult = await executeCommandMap(
          Map<String, dynamic>.from(item as Map),
        );
      }
      return lastResult;
    }
    return executeCommandMap(Map<String, dynamic>.from(decoded as Map));
  }

  Map<String, dynamic> exportStateSnapshot() => _exportState();

  Map<String, dynamic> findLargestEmptySpaceSnapshot() =>
      _findLargestEmptySpaceFromJson();

  Future<Map<String, dynamic>> executeCommandMap(
    Map<String, dynamic> command,
  ) async {
    final action = (command['action'] ?? '').toString().trim();

    switch (action) {
      case 'create':
        return _createFromJson(command);
      case 'move':
        return _moveFromJson(command);
      case 'enlarge':
        return _resizeScaleFromJson(command, enlarge: true);
      case 'shrink':
        return _resizeScaleFromJson(command, enlarge: false);
      case 'delete':
        return _deleteFromJson(command);
      case 'click':
        return _clickFromJson(command);
      case 'findLargestEmptySpace':
        return _findLargestEmptySpaceFromJson();
      case 'state':
        return _exportState();
      case 'hydrateScene':
        return _hydrateSceneFromJson(command);
      default:
        throw UnsupportedError('Unknown action "$action".');
    }
  }

  Map<String, dynamic> _hydrateSceneFromJson(Map<String, dynamic> json) {
    final rawObjects = json['objects'];
    if (rawObjects is! List) {
      throw FormatException('"objects" must be a list.');
    }

    _objects.clear();

    for (final raw in rawObjects) {
      final entry = Map<String, dynamic>.from(raw as Map);
      final name = entry['name']?.toString().trim() ?? '';
      if (name.isEmpty) {
        continue;
      }

      final width = _readDouble(entry['width'], fallback: 120);
      final height = _readDouble(entry['height'], fallback: 120);
      final x = _readDouble(
        entry['x'],
        fallback: 0,
      ).clamp(0.0, math.max(0.0, _boardSize.width - width)).toDouble();
      final y = _readDouble(
        entry['y'],
        fallback: 0,
      ).clamp(0.0, math.max(0.0, _boardSize.height - height)).toDouble();

      final object = SceneObjectData(
        name: name,
        text: entry['text']?.toString() ?? name,
        x: x,
        y: y,
        width: width,
        height: height,
        color: _colorFromJson(entry['color']) ?? _randomMainColor(),
        baseScale: _readDouble(
          entry['baseScale'],
          fallback: 1.0,
        ).clamp(0.15, 8.0).toDouble(),
        isDeleting: false,
        innerInset: _tryReadDouble(entry['innerInset']) ?? _randomInnerInset(),
        memoryType: _readMemoryType(entry['memoryType']),
        resultId: entry['resultId']?.toString(),
        deleteAfterClick: _readBool(
          entry['deleteAfterClick'],
          fallback: _readMemoryType(entry['memoryType']) == 'instant',
        ),
        tags: _readTags(entry['tags']),
        extraData: _readExtraData(entry['extraData'] ?? entry['extra_data']),
      );

      _objects[name] = object;
    }

    notifyListeners();
    return _exportState();
  }

  Map<String, dynamic> _createFromJson(Map<String, dynamic> json) {
    final name = json['name']?.toString().trim() ?? '';
    if (name.isEmpty) {
      throw FormatException('"name" is required for create.');
    }

    final width = _readDouble(json['width'], fallback: 120);
    final height = _readDouble(json['height'], fallback: 120);
    final requestedX = _tryReadDouble(json['x']);
    final requestedY = _tryReadDouble(json['y']);

    double x;
    double y;

    if (requestedX != null && requestedY != null) {
      x = requestedX;
      y = requestedY;
    } else {
      final emptyRect = findLargestEmptyRect();
      if (emptyRect != null &&
          emptyRect.width >= width &&
          emptyRect.height >= height) {
        x = emptyRect.left;
        y = emptyRect.top;
      } else {
        x = 0;
        y = 0;
      }
    }

    x = x.clamp(0.0, math.max(0.0, _boardSize.width - width)).toDouble();
    y = y.clamp(0.0, math.max(0.0, _boardSize.height - height)).toDouble();

    final object = SceneObjectData(
      name: name,
      text: json['text']?.toString() ?? name,
      x: x,
      y: y,
      width: width,
      height: height,
      color: _colorFromJson(json['color']) ?? _randomMainColor(),
      baseScale: _readDouble(
        json['baseScale'],
        fallback: 1.0,
      ).clamp(0.15, 8.0).toDouble(),
      isDeleting: false,
      innerInset: _tryReadDouble(json['innerInset']) ?? _randomInnerInset(),
      memoryType: _readMemoryType(json['memoryType']),
      resultId: json['resultId']?.toString(),
      deleteAfterClick: _readBool(
        json['deleteAfterClick'],
        fallback: _readMemoryType(json['memoryType']) == 'instant',
      ),
      tags: _readTags(json['tags']),
      extraData: _readExtraData(json['extraData'] ?? json['extra_data']),
    );

    _objects[name] = object;
    notifyListeners();

    return {'ok': true, 'action': 'create', 'object': object.toJson()};
  }

  Map<String, dynamic> _moveFromJson(Map<String, dynamic> json) {
    final name = json['name']?.toString().trim() ?? '';
    final current = _objects[name];
    if (current == null) {
      throw StateError('Object "$name" not found.');
    }

    final x = _readDouble(
      json['x'],
      fallback: current.x,
    ).clamp(0.0, math.max(0.0, _boardSize.width - current.width)).toDouble();
    final y = _readDouble(
      json['y'],
      fallback: current.y,
    ).clamp(0.0, math.max(0.0, _boardSize.height - current.height)).toDouble();

    _objects[name] = current.copyWith(x: x, y: y);
    notifyListeners();

    return {'ok': true, 'action': 'move', 'object': _objects[name]!.toJson()};
  }

  Map<String, dynamic> _resizeScaleFromJson(
    Map<String, dynamic> json, {
    required bool enlarge,
  }) {
    final name = json['name']?.toString().trim() ?? '';
    final current = _objects[name];
    if (current == null) {
      throw StateError('Object "$name" not found.');
    }

    final factor = _readDouble(json['factor'], fallback: enlarge ? 1.2 : 0.85);

    final targetScale = (current.baseScale * factor)
        .clamp(0.15, 8.0)
        .toDouble();

    _objects[name] = current.copyWith(baseScale: targetScale);
    notifyListeners();

    return {
      'ok': true,
      'action': enlarge ? 'enlarge' : 'shrink',
      'object': _objects[name]!.toJson(),
    };
  }

  Map<String, dynamic> _deleteFromJson(Map<String, dynamic> json) {
    final name = json['name']?.toString().trim() ?? '';
    final current = _objects[name];
    if (current == null) {
      throw StateError('Object "$name" not found.');
    }

    _objects[name] = current.copyWith(isDeleting: true);
    notifyListeners();

    return {'ok': true, 'action': 'delete', 'scheduledDelete': name};
  }

  Future<Map<String, dynamic>> _clickFromJson(Map<String, dynamic> json) async {
    final name = json['name']?.toString().trim() ?? '';
    final current = _objects[name];
    if (current == null) {
      throw StateError('Object "$name" not found.');
    }

    return {'ok': true, 'action': 'click', 'name': name};
  }

  Map<String, dynamic> _findLargestEmptySpaceFromJson() {
    final rect = findLargestEmptyRect();
    return {
      'ok': true,
      'action': 'findLargestEmptySpace',
      'board': {'width': _boardSize.width, 'height': _boardSize.height},
      'bbox': rect == null
          ? null
          : {
              'x': rect.left,
              'y': rect.top,
              'width': rect.width,
              'height': rect.height,
            },
    };
  }

  Map<String, dynamic> _exportState() {
    return {
      'ok': true,
      'action': 'state',
      'board': {'width': _boardSize.width, 'height': _boardSize.height},
      'objects': _objects.values.map((object) => object.toJson()).toList(),
    };
  }

  void finalizeDelete(String name) {
    if (_objects.containsKey(name)) {
      _objects.remove(name);
      notifyListeners();
    }
  }

  Rect? findLargestEmptyRect() {
    final width = _boardSize.width;
    final height = _boardSize.height;

    if (width <= 0 || height <= 0) return null;

    final xs = <double>{0, width};
    final ys = <double>{0, height};

    for (final object in _objects.values.where((o) => !o.isDeleting)) {
      xs.add(object.x.clamp(0.0, width).toDouble());
      xs.add((object.x + object.width).clamp(0.0, width).toDouble());
      ys.add(object.y.clamp(0.0, height).toDouble());
      ys.add((object.y + object.height).clamp(0.0, height).toDouble());
    }

    final sortedX = xs.toList()..sort();
    final sortedY = ys.toList()..sort();

    Rect? bestRect;
    double bestArea = -1;

    for (int i = 0; i < sortedX.length; i++) {
      for (int j = i + 1; j < sortedX.length; j++) {
        final left = sortedX[i];
        final right = sortedX[j];
        if (right <= left) continue;

        for (int k = 0; k < sortedY.length; k++) {
          for (int m = k + 1; m < sortedY.length; m++) {
            final top = sortedY[k];
            final bottom = sortedY[m];
            if (bottom <= top) continue;

            final candidate = Rect.fromLTRB(left, top, right, bottom);
            if (_overlapsAny(candidate)) continue;

            final area = candidate.width * candidate.height;
            if (area > bestArea) {
              bestArea = area;
              bestRect = candidate;
            }
          }
        }
      }
    }

    return bestRect;
  }

  bool _overlapsAny(Rect candidate) {
    for (final object in _objects.values.where((o) => !o.isDeleting)) {
      final rect = Rect.fromLTWH(
        object.x,
        object.y,
        object.width,
        object.height,
      );
      if (_rectanglesOverlap(candidate, rect)) {
        return true;
      }
    }
    return false;
  }

  bool _rectanglesOverlap(Rect a, Rect b) {
    return a.left < b.right &&
        a.right > b.left &&
        a.top < b.bottom &&
        a.bottom > b.top;
  }

  Color _randomMainColor() {
    return _desaturateColor(
      _mainColors[_random.nextInt(_mainColors.length)],
      0.20,
    );
  }

  double _randomInnerInset() {
    return 10 + _random.nextInt(16).toDouble();
  }

  Color? _colorFromJson(dynamic value) {
    if (value == null) return null;

    if (value is int) {
      return _desaturateColor(Color(value), 0.20);
    }

    final raw = value.toString().trim();
    if (raw.isEmpty) return null;

    final lower = raw.toLowerCase();

    const byName = <String, Color>{
      'red': Color(0xFFD36E6A),
      'blue': Color(0xFF6E9ACC),
      'green': Color(0xFF6EA886),
      'orange': Color(0xFFD59667),
      'purple': Color(0xFF9A77B7),
      'yellow': Color(0xFFD7C46C),
      'teal': Color(0xFF5FA39A),
      'pink': Color(0xFFC4749C),
      'random': Color(0x00000000),
    };

    if (lower == 'random') {
      return _randomMainColor();
    }

    if (byName.containsKey(lower)) {
      return _desaturateColor(byName[lower]!, 0.20);
    }

    final clean = lower.replaceFirst('#', '');
    final hex = clean.length == 6 ? 'ff$clean' : clean;
    final parsed = int.tryParse(hex, radix: 16);
    if (parsed == null) return null;

    return _desaturateColor(Color(parsed), 0.20);
  }

  double _readDouble(dynamic value, {required double fallback}) {
    if (value == null) return fallback;
    if (value is num) return value.toDouble();
    return double.tryParse(value.toString()) ?? fallback;
  }

  double? _tryReadDouble(dynamic value) {
    if (value == null) return null;
    if (value is num) return value.toDouble();
    return double.tryParse(value.toString());
  }

  bool _readBool(dynamic value, {required bool fallback}) {
    if (value == null) return fallback;
    if (value is bool) return value;
    final lowered = value.toString().trim().toLowerCase();
    if (lowered == 'true') return true;
    if (lowered == 'false') return false;
    return fallback;
  }

  String _readMemoryType(dynamic value) {
    final lowered = value?.toString().trim().toLowerCase() ?? '';
    if (lowered == 'instant' || lowered == 'ram' || lowered == 'memory') {
      return lowered;
    }
    return 'ram';
  }

  List<String> _readTags(dynamic value) {
    if (value is! List) return const [];
    final tags = <String>[];
    for (final entry in value) {
      final tag = entry.toString().trim();
      if (tag.isEmpty || tags.contains(tag)) continue;
      tags.add(tag);
    }
    return tags;
  }

  Map<String, dynamic> _readExtraData(dynamic value) {
    if (value is! Map) return const <String, dynamic>{};
    return Map<String, dynamic>.from(value);
  }
}

class SceneObjectData {
  const SceneObjectData({
    required this.name,
    required this.text,
    required this.x,
    required this.y,
    required this.width,
    required this.height,
    required this.color,
    required this.baseScale,
    required this.isDeleting,
    required this.innerInset,
    required this.memoryType,
    required this.resultId,
    required this.deleteAfterClick,
    required this.tags,
    required this.extraData,
  });

  final String name;
  final String text;
  final double x;
  final double y;
  final double width;
  final double height;
  final Color color;
  final double baseScale;
  final bool isDeleting;
  final double innerInset;
  final String memoryType;
  final String? resultId;
  final bool deleteAfterClick;
  final List<String> tags;
  final Map<String, dynamic> extraData;

  SceneObjectData copyWith({
    String? name,
    String? text,
    double? x,
    double? y,
    double? width,
    double? height,
    Color? color,
    double? baseScale,
    bool? isDeleting,
    double? innerInset,
    String? memoryType,
    String? resultId,
    bool? deleteAfterClick,
    List<String>? tags,
    Map<String, dynamic>? extraData,
  }) {
    return SceneObjectData(
      name: name ?? this.name,
      text: text ?? this.text,
      x: x ?? this.x,
      y: y ?? this.y,
      width: width ?? this.width,
      height: height ?? this.height,
      color: color ?? this.color,
      baseScale: baseScale ?? this.baseScale,
      isDeleting: isDeleting ?? this.isDeleting,
      innerInset: innerInset ?? this.innerInset,
      memoryType: memoryType ?? this.memoryType,
      resultId: resultId ?? this.resultId,
      deleteAfterClick: deleteAfterClick ?? this.deleteAfterClick,
      tags: tags ?? this.tags,
      extraData: extraData ?? this.extraData,
    );
  }

  Map<String, dynamic> toJson() {
    return {
      'name': name,
      'text': text,
      'x': x,
      'y': y,
      'width': width,
      'height': height,
      'baseScale': baseScale,
      'isDeleting': isDeleting,
      'innerInset': innerInset,
      'color': color.toARGB32(),
      'memoryType': memoryType,
      'resultId': resultId,
      'deleteAfterClick': deleteAfterClick,
      'tags': tags,
      'extraData': extraData,
      'bbox': {'x': x, 'y': y, 'width': width, 'height': height},
    };
  }
}

class BoardObjectWidget extends StatefulWidget {
  const BoardObjectWidget({
    super.key,
    required this.data,
    required this.onTap,
    required this.onDeleteComplete,
    required this.onDragPositionChanged,
  });

  final SceneObjectData data;
  final VoidCallback onTap;
  final VoidCallback onDeleteComplete;
  final void Function(double x, double y) onDragPositionChanged;

  @override
  State<BoardObjectWidget> createState() => _BoardObjectWidgetState();
}

class _BoardObjectWidgetState extends State<BoardObjectWidget>
    with TickerProviderStateMixin {
  late Offset _fromPosition;
  late Offset _toPosition;

  late final AnimationController _moveController;
  late final AnimationController _scaleController;
  late final AnimationController _deleteController;

  double _displayScale = 1;
  double _scaleAnimStart = 1;
  double _scaleAnimTarget = 1;
  bool _deletionDone = false;
  bool _isDragging = false;
  Offset? _dragPointerOffset;

  @override
  void initState() {
    super.initState();
    _fromPosition = Offset(widget.data.x, widget.data.y);
    _toPosition = Offset(widget.data.x, widget.data.y);
    _displayScale = widget.data.baseScale;
    _scaleAnimStart = widget.data.baseScale;
    _scaleAnimTarget = widget.data.baseScale;

    _moveController =
        AnimationController(
          vsync: this,
          duration: const Duration(milliseconds: 900),
        )..addListener(() {
          setState(() {});
        });

    _scaleController =
        AnimationController(
          vsync: this,
          duration: const Duration(milliseconds: 220),
        )..addListener(() {
          setState(() {});
        });

    _deleteController =
        AnimationController(
            vsync: this,
            duration: const Duration(milliseconds: 380),
          )
          ..addListener(() {
            setState(() {});
          })
          ..addStatusListener((status) {
            if (status == AnimationStatus.completed && !_deletionDone) {
              _deletionDone = true;
              widget.onDeleteComplete();
            }
          });
  }

  @override
  void didUpdateWidget(covariant BoardObjectWidget oldWidget) {
    super.didUpdateWidget(oldWidget);

    if (oldWidget.data.x != widget.data.x ||
        oldWidget.data.y != widget.data.y) {
      _startMove(
        from: _currentVisualPosition(),
        to: Offset(widget.data.x, widget.data.y),
      );
    }

    if ((oldWidget.data.baseScale - widget.data.baseScale).abs() > 0.0001) {
      _startScalePop(
        fromScale: _currentScaleBase(),
        toScale: widget.data.baseScale,
      );
    }

    if (!oldWidget.data.isDeleting && widget.data.isDeleting) {
      _deleteController.forward(from: 0);
    }
  }

  @override
  void dispose() {
    _moveController.dispose();
    _scaleController.dispose();
    _deleteController.dispose();
    super.dispose();
  }

  Offset _currentVisualPosition() {
    final raw = _moveController.isAnimating ? _moveController.value : 1.0;
    final t = _moveController.isAnimating
        ? const SmoothVelocityCurve().transform(raw)
        : 1.0;
    return Offset.lerp(_fromPosition, _toPosition, t)!;
  }

  void _startMove({required Offset from, required Offset to}) {
    _fromPosition = from;
    _toPosition = to;

    final distance = (to - from).distance;
    final milliseconds = (distance / 430.0 * 1000)
        .clamp(420.0, 2600.0)
        .toDouble()
        .round();

    _moveController.duration = Duration(milliseconds: milliseconds);
    _moveController.forward(from: 0);
  }

  void _startScalePop({required double fromScale, required double toScale}) {
    _scaleAnimStart = fromScale;
    _scaleAnimTarget = toScale;
    _displayScale = toScale;
    _scaleController.forward(from: 0);
  }

  double _currentScaleBase() {
    if (!_scaleController.isAnimating) {
      return _displayScale;
    }

    return _computeScalePopValue(
      _scaleController.value,
      _scaleAnimStart,
      _scaleAnimTarget,
    );
  }

  @override
  Widget build(BuildContext context) {
    final moveRaw = _moveController.isAnimating ? _moveController.value : 1.0;
    final moveT = _moveController.isAnimating
        ? const SmoothVelocityCurve().transform(moveRaw)
        : 1.0;

    final livePosition = Offset.lerp(_fromPosition, _toPosition, moveT)!;

    final motionEffect = _isDragging ? 1.0 : _motionEffectProgress(moveRaw);
    final motionScale = lerpDouble(1.0, 0.8, motionEffect)!;
    final saturation = lerpDouble(1.0, 0.0, motionEffect)!;

    final scaleValue = _scaleController.isAnimating
        ? _computeScalePopValue(
            _scaleController.value,
            _scaleAnimStart,
            _scaleAnimTarget,
          )
        : _displayScale;

    final opacity = _computeDeleteOpacity(_deleteController.value);
    final visualScale = scaleValue * motionScale;

    final innerSize = math
        .max(
          12.0,
          math.min(widget.data.width, widget.data.height) -
              (widget.data.innerInset * 2),
        )
        .toDouble();

    final textBoxWidth = (innerSize * 0.72)
        .clamp(36.0, widget.data.width)
        .toDouble();

    return Positioned(
      left: livePosition.dx,
      top: livePosition.dy,
      child: Transform.scale(
        scale: visualScale,
        alignment: Alignment.center,
        child: Opacity(
          opacity: opacity,
          child: ColorFiltered(
            colorFilter: ColorFilter.matrix(_saturationMatrix(saturation)),
            child: GestureDetector(
              behavior: HitTestBehavior.opaque,
              onTap: widget.onTap,
              onPanStart: (details) {
                final box = context.findRenderObject() as RenderBox?;
                if (box == null) return;
                _dragPointerOffset = box.globalToLocal(details.globalPosition);
                setState(() {
                  _isDragging = true;
                });
              },
              onPanUpdate: (details) {
                final board = context
                    .findAncestorRenderObjectOfType<RenderBox>();
                final dragOffset = _dragPointerOffset;
                if (board == null || dragOffset == null) return;
                final localOnBoard = board.globalToLocal(
                  details.globalPosition,
                );
                widget.onDragPositionChanged(
                  localOnBoard.dx - dragOffset.dx,
                  localOnBoard.dy - dragOffset.dy,
                );
              },
              onPanEnd: (_) {
                setState(() {
                  _isDragging = false;
                  _dragPointerOffset = null;
                });
              },
              onPanCancel: () {
                setState(() {
                  _isDragging = false;
                  _dragPointerOffset = null;
                });
              },
              child: SizedBox(
                width: widget.data.width,
                height: widget.data.height,
                child: Stack(
                  children: [
                    Container(
                      width: widget.data.width,
                      height: widget.data.height,
                      decoration: BoxDecoration(
                        color: widget.data.color,
                        border: Border.all(
                          color: Colors.black.withValues(alpha: 0.08),
                          width: 1,
                        ),
                        boxShadow: [
                          BoxShadow(
                            color: widget.data.color.withValues(alpha: 0.22),
                            blurRadius: 22,
                            spreadRadius: 2,
                            offset: const Offset(0, 6),
                          ),
                          BoxShadow(
                            color: Colors.black.withValues(alpha: 0.12),
                            blurRadius: 24,
                            spreadRadius: 0.5,
                            offset: const Offset(0, 8),
                          ),
                        ],
                      ),
                    ),
                    Positioned.fill(
                      child: Center(
                        child: Container(
                          width: innerSize,
                          height: innerSize,
                          decoration: BoxDecoration(
                            color: _darken(widget.data.color, 0.22),
                          ),
                        ),
                      ),
                    ),
                    Positioned(
                      top: 8,
                      left: 8,
                      child: Container(
                        padding: const EdgeInsets.symmetric(
                          horizontal: 8,
                          vertical: 4,
                        ),
                        decoration: BoxDecoration(
                          color: Colors.black.withValues(alpha: 0.10),
                          border: Border.all(
                            color: Colors.black.withValues(alpha: 0.14),
                            width: 0.7,
                          ),
                        ),
                        child: Text(
                          widget.data.memoryType,
                          style: TextStyle(
                            color: _bestTextColor(widget.data.color),
                            fontSize: 10,
                            fontWeight: FontWeight.w700,
                            letterSpacing: 0.3,
                          ),
                        ),
                      ),
                    ),
                    if (widget.data.deleteAfterClick)
                      Positioned(
                        top: 8,
                        right: 8,
                        child: Container(
                          padding: const EdgeInsets.symmetric(
                            horizontal: 7,
                            vertical: 4,
                          ),
                          decoration: BoxDecoration(
                            color: Colors.white.withValues(alpha: 0.16),
                            border: Border.all(
                              color: Colors.black.withValues(alpha: 0.18),
                              width: 0.7,
                            ),
                          ),
                          child: Text(
                            'one tap',
                            style: TextStyle(
                              color: _bestTextColor(widget.data.color),
                              fontSize: 10,
                              fontWeight: FontWeight.w600,
                            ),
                          ),
                        ),
                      ),
                    Positioned.fill(
                      child: Center(
                        child: Container(
                          width: textBoxWidth,
                          padding: const EdgeInsets.symmetric(
                            horizontal: 10,
                            vertical: 8,
                          ),
                          decoration: BoxDecoration(
                            color: Colors.white.withValues(alpha: 0.10),
                            border: Border.all(
                              color: Colors.black.withValues(alpha: 0.18),
                              width: 0.7,
                            ),
                          ),
                          child: Text(
                            widget.data.text,
                            textAlign: TextAlign.center,
                            maxLines: 3,
                            overflow: TextOverflow.ellipsis,
                            style: TextStyle(
                              color: _bestTextColor(widget.data.color),
                              fontWeight: FontWeight.w700,
                              fontSize: math.max(
                                12,
                                math.min(
                                      widget.data.width,
                                      widget.data.height,
                                    ) *
                                    0.14,
                              ),
                              height: 1.05,
                            ),
                          ),
                        ),
                      ),
                    ),
                  ],
                ),
              ),
            ),
          ),
        ),
      ),
    );
  }

  double _motionEffectProgress(double raw) {
    if (!_moveController.isAnimating) return 0;

    const accelEnd = 0.15;
    const decelStart = 0.85;

    if (raw <= accelEnd) {
      return Curves.easeOut.transform(raw / accelEnd);
    }

    if (raw < decelStart) {
      return 1;
    }

    final tail = (raw - decelStart) / (1 - decelStart);
    return 1 - Curves.easeIn.transform(tail.clamp(0.0, 1.0).toDouble());
  }

  double _computeScalePopValue(double t, double fromScale, double targetScale) {
    final direction = targetScale >= fromScale ? 1.0 : -1.0;
    final overshootMagnitude = (targetScale - fromScale).abs() * 0.18 + 0.015;
    final overshoot = targetScale + (direction * overshootMagnitude);

    if (t < 0.68) {
      final local = Curves.easeOutCubic.transform(t / 0.68);
      return lerpDouble(fromScale, overshoot, local)!;
    }

    final local = Curves.easeOutCubic.transform((t - 0.68) / 0.32);
    return lerpDouble(overshoot, targetScale, local)!;
  }

  double _computeDeleteOpacity(double t) {
    if (!_deleteController.isAnimating && !widget.data.isDeleting) {
      return 1;
    }

    if (t <= 0.78) {
      final local = Curves.easeOut.transform(t / 0.78);
      return lerpDouble(1.0, 0.15, local)!;
    }

    final local = Curves.easeInOut.transform((t - 0.78) / 0.22);
    return lerpDouble(0.15, 0.0, local)!;
  }
}

class SmoothVelocityCurve extends Curve {
  const SmoothVelocityCurve();

  static const double _accelEnd = 0.15;
  static const double _decelStart = 0.85;
  static const double _startVelocity = 0.75;
  static const double _cruiseVelocity = 1.0;
  static const double _endVelocity = 0.0;

  static const double _area1 =
      ((_startVelocity + _cruiseVelocity) * 0.5) * _accelEnd;
  static const double _area2 = (_decelStart - _accelEnd) * _cruiseVelocity;
  static const double _area3 =
      ((_cruiseVelocity + _endVelocity) * 0.5) * (1 - _decelStart);
  static const double _totalArea = _area1 + _area2 + _area3;

  @override
  double transformInternal(double t) {
    if (t <= 0) return 0;
    if (t >= 1) return 1;

    if (t <= _accelEnd) {
      final slope = (_cruiseVelocity - _startVelocity) / _accelEnd;
      final area = (_startVelocity * t) + (0.5 * slope * t * t);
      return area / _totalArea;
    }

    if (t <= _decelStart) {
      final area = _area1 + ((t - _accelEnd) * _cruiseVelocity);
      return area / _totalArea;
    }

    final segmentT = t - _decelStart;
    final segmentDuration = 1 - _decelStart;
    final slope = (_endVelocity - _cruiseVelocity) / segmentDuration;
    final area =
        _area1 +
        _area2 +
        (_cruiseVelocity * segmentT) +
        (0.5 * slope * segmentT * segmentT);

    return area / _totalArea;
  }
}

class GridPainter extends CustomPainter {
  GridPainter();

  static final List<_GridLayer> _layers = _buildLayers();

  static List<_GridLayer> _buildLayers() {
    final random = math.Random(41);

    List<double> buildPositions(double maxSize) {
      final positions = <double>[];
      double cursor = 0;

      while (cursor <= maxSize) {
        positions.add(cursor);
        cursor += 52 + random.nextInt(180);
      }

      if (positions.isEmpty || positions.last < maxSize) {
        positions.add(maxSize);
      }

      if (positions.length > 2) {
        final removable = List<int>.generate(
          positions.length - 2,
          (i) => i + 1,
        );
        removable.shuffle(random);
        final removeCount = math.min(2, removable.length);
        final toRemove = removable.take(removeCount).toSet();

        return [
          for (int i = 0; i < positions.length; i++)
            if (!toRemove.contains(i)) positions[i],
        ];
      }

      return positions;
    }

    return [
      _GridLayer(
        color: const Color(0x14000000),
        verticalPositions: buildPositions(5000),
        horizontalPositions: buildPositions(5000),
      ),
      _GridLayer(
        color: const Color(0x22000000),
        verticalPositions: buildPositions(5000),
        horizontalPositions: buildPositions(5000),
      ),
      _GridLayer(
        color: const Color(0x30000000),
        verticalPositions: buildPositions(5000),
        horizontalPositions: buildPositions(5000),
      ),
    ];
  }

  @override
  void paint(Canvas canvas, Size size) {
    for (final layer in _layers) {
      final paint = Paint()
        ..color = layer.color
        ..strokeWidth = 0.8;

      for (final x in layer.verticalPositions) {
        if (x < 0 || x > size.width) continue;
        canvas.drawLine(Offset(x, 0), Offset(x, size.height), paint);
      }

      for (final y in layer.horizontalPositions) {
        if (y < 0 || y > size.height) continue;
        canvas.drawLine(Offset(0, y), Offset(size.width, y), paint);
      }
    }
  }

  @override
  bool shouldRepaint(covariant GridPainter oldDelegate) => false;
}

class _GridLayer {
  const _GridLayer({
    required this.color,
    required this.verticalPositions,
    required this.horizontalPositions,
  });

  final Color color;
  final List<double> verticalPositions;
  final List<double> horizontalPositions;
}

List<double> _saturationMatrix(double saturation) {
  final s = saturation.clamp(0.0, 1.0).toDouble();
  final inv = 1 - s;
  const rw = 0.2126;
  const gw = 0.7152;
  const bw = 0.0722;

  return <double>[
    inv * rw + s,
    inv * gw,
    inv * bw,
    0,
    0,
    inv * rw,
    inv * gw + s,
    inv * bw,
    0,
    0,
    inv * rw,
    inv * gw,
    inv * bw + s,
    0,
    0,
    0,
    0,
    0,
    1,
    0,
  ];
}

Color _desaturateColor(Color color, double amount) {
  final hsl = HSLColor.fromColor(color);
  final next = hsl.withSaturation(
    (hsl.saturation * (1 - amount)).clamp(0.0, 1.0).toDouble(),
  );
  return next.toColor();
}

Color _darken(Color color, double amount) {
  final hsl = HSLColor.fromColor(color);
  final next = hsl.withLightness(
    (hsl.lightness - amount).clamp(0.0, 1.0).toDouble(),
  );
  return next.toColor();
}

Color _bestTextColor(Color color) {
  return color.computeLuminance() > 0.58 ? Colors.black : Colors.white;
}
