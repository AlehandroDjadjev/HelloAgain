import 'dart:async';
import 'dart:collection';
import 'dart:math' as math;
import 'dart:typed_data';

import 'package:audioplayers/audioplayers.dart';
import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';
import 'package:record/record.dart';

import 'src/api/voice_gateway_client.dart';
import 'src/config/backend_base_url.dart';

class VoiceLabScreen extends StatefulWidget {
  const VoiceLabScreen({super.key});

  @override
  State<VoiceLabScreen> createState() => _VoiceLabScreenState();
}

class _VoiceLabScreenState extends State<VoiceLabScreen> {
  static const _sampleRate = 16000;
  static const _channels = 1;
  static const _speechThreshold = 0.035;
  static const _silenceWindow = Duration(milliseconds: 900);
  static const _maxTurnLength = Duration(seconds: 14);
  static const _minTurnLength = Duration(milliseconds: 450);
  static const _preSpeechChunkLimit = 8;

  late final VoiceGatewayClient _client;
  late final String _sessionId;
  final List<_Message> _messages = [];
  final Queue<Uint8List> _preSpeechChunks = Queue<Uint8List>();
  final List<Uint8List> _turnChunks = [];

  AudioRecorder? _recorder;
  AudioPlayer? _player;
  StreamSubscription<Uint8List>? _recSub;
  StreamSubscription<PlayerState>? _playerStateSub;

  VoiceGatewayHealth? _health;
  Map<String, String> _providerStatus = const {};
  Uint8List? _lastAudio;
  String _lastMimeType = 'audio/wav';
  String _status = kIsWeb
      ? 'Tap start to enable the microphone in Chrome.'
      : 'Preparing always-listening mode...';
  String _language = 'bg-BG';
  String? _error;
  String? _healthError;
  double _micLevel = 0;
  bool _enabled = false;
  bool _listening = false;
  bool _processing = false;
  bool _playing = false;
  bool _loadingHealth = true;
  bool _finalizing = false;
  bool _speechDetected = false;
  DateTime? _speechStartedAt;
  DateTime? _lastVoiceAt;

  bool get _requiresManualStart => kIsWeb;

  @override
  void initState() {
    super.initState();
    _client = VoiceGatewayClient(baseUrl: _resolveBaseUrl());
    _sessionId = 'voice-${DateTime.now().millisecondsSinceEpoch}';
    unawaited(_initialize());
  }

  String _resolveBaseUrl() {
    return resolveBackendBaseUrl();
  }

  Future<void> _initialize() async {
    await _refreshHealth();
    if (_requiresManualStart || !mounted) return;
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (mounted) {
        unawaited(_startConversation(autoStarted: true));
      }
    });
  }

  Future<AudioRecorder> _ensureRecorder() async =>
      _recorder ??= AudioRecorder();

  AudioPlayer _ensurePlayer() {
    final existing = _player;
    if (existing != null) return existing;
    final player = AudioPlayer();
    _playerStateSub = player.onPlayerStateChanged.listen((state) {
      if (mounted) {
        setState(() => _playing = state == PlayerState.playing);
      }
    });
    _player = player;
    return player;
  }

  Future<void> _refreshHealth({bool silent = false}) async {
    if (!silent && mounted) {
      setState(() {
        _loadingHealth = true;
        _healthError = null;
      });
    }
    try {
      final health = await _client.getHealth();
      if (!mounted) return;
      setState(() {
        _health = health;
        _loadingHealth = false;
      });
    } catch (error) {
      if (!mounted) return;
      setState(() {
        _healthError = _describeError(error);
        _loadingHealth = false;
      });
    }
  }

  Future<void> _toggleConversation() async {
    if (_enabled) {
      await _stopConversation();
    } else {
      await _startConversation();
    }
  }

  Future<void> _startConversation({bool autoStarted = false}) async {
    if (_enabled && (_listening || _processing)) return;
    try {
      final recorder = await _ensureRecorder();
      if (!await recorder.hasPermission()) {
        throw VoiceGatewayException(
          _requiresManualStart
              ? 'Chrome microphone permission is required.'
              : 'Microphone permission is required for always-listening mode.',
        );
      }
      if (!mounted) return;
      setState(() {
        _enabled = true;
        _error = null;
        _status = autoStarted
            ? 'Microphone ready. Listening for speech...'
            : 'Conversation enabled. Listening for speech...';
      });
      await _startListeningLoop();
    } catch (error) {
      if (!mounted) return;
      setState(() {
        _enabled = false;
        _error = _describeError(error);
        _status = 'Unable to start conversation.';
      });
    }
  }

  Future<void> _stopConversation() async {
    _enabled = false;
    _resetTurn();
    await _stopRecorder();
    await _player?.stop();
    if (!mounted) return;
    setState(() {
      _processing = false;
      _micLevel = 0;
      _status = _requiresManualStart
          ? 'Conversation stopped. Tap start when ready.'
          : 'Conversation stopped.';
    });
  }

  Future<void> _startListeningLoop() async {
    if (!_enabled || _listening || _processing) return;
    final recorder = await _ensureRecorder();
    await _stopRecorder();
    _resetTurn();
    final stream = await recorder.startStream(
      const RecordConfig(
        encoder: AudioEncoder.pcm16bits,
        sampleRate: _sampleRate,
        numChannels: _channels,
        echoCancel: true,
        noiseSuppress: true,
      ),
    );
    _recSub = stream.listen(
      _handleChunk,
      onError: (Object error, StackTrace stackTrace) {
        unawaited(_handleRecorderError(error));
      },
      cancelOnError: true,
    );
    if (!mounted) return;
    setState(() {
      _listening = true;
      _status = 'Listening for speech...';
      _micLevel = 0;
    });
  }

  Future<void> _stopRecorder() async {
    await _recSub?.cancel();
    _recSub = null;
    try {
      await _recorder?.stop();
    } catch (_) {}
    if (mounted) {
      setState(() {
        _listening = false;
        _micLevel = 0;
      });
    }
  }

  void _handleChunk(Uint8List chunk) {
    if (!_enabled || _processing || _finalizing) return;
    final level = _pcmLevel(chunk);
    if (mounted) {
      setState(
        () => _micLevel = ((_micLevel * 0.6) + (level * 0.4)).clamp(0, 1),
      );
    }
    final now = DateTime.now();
    if (_speechDetected) {
      _turnChunks.add(chunk);
      if (level >= _speechThreshold) _lastVoiceAt = now;
      if (_speechStartedAt != null &&
          now.difference(_speechStartedAt!) >= _maxTurnLength) {
        unawaited(_finishTurn());
      } else if (_lastVoiceAt != null &&
          now.difference(_lastVoiceAt!) >= _silenceWindow) {
        unawaited(_finishTurn());
      }
      return;
    }
    _preSpeechChunks.addLast(chunk);
    while (_preSpeechChunks.length > _preSpeechChunkLimit) {
      _preSpeechChunks.removeFirst();
    }
    if (level >= _speechThreshold) {
      _speechDetected = true;
      _speechStartedAt = now;
      _lastVoiceAt = now;
      _turnChunks
        ..clear()
        ..addAll(_preSpeechChunks)
        ..add(chunk);
      _preSpeechChunks.clear();
      if (mounted) {
        setState(() => _status = 'Speech detected. Keep talking...');
      }
    }
  }

  Future<void> _finishTurn() async {
    if (_finalizing || !_speechDetected) return;
    _finalizing = true;
    final startedAt = _speechStartedAt;
    final pcmBytes = _joinChunks(_turnChunks);
    await _stopRecorder();
    if (startedAt == null ||
        DateTime.now().difference(startedAt) < _minTurnLength ||
        pcmBytes.isEmpty) {
      _resetTurn();
      _finalizing = false;
      if (_enabled) await _startListeningLoop();
      return;
    }
    if (mounted) {
      setState(() {
        _processing = true;
        _error = null;
        _status = 'Sending audio to the conversation endpoint...';
      });
    }
    try {
      final response = await _client.conversation(
        audioBytes: _wrapPcmAsWav(
          pcmBytes,
          sampleRate: _sampleRate,
          channels: _channels,
        ),
        language: _language,
        sessionId: _sessionId,
      );
      _providerStatus = response.providerStatus;
      _lastAudio = response.assistantAudioBytes;
      _lastMimeType = response.assistantAudioMimeType;
      if (!mounted) return;
      setState(() {
        if (response.transcript.trim().isNotEmpty) {
          _messages.add(_Message('You', response.transcript.trim()));
        }
        if (response.assistantText.trim().isNotEmpty) {
          _messages.add(_Message('HelloAgain', response.assistantText.trim()));
        }
        _status = 'Playing assistant reply...';
      });
      unawaited(_refreshHealth(silent: true));
      await _playReply(
        response.assistantAudioBytes,
        response.assistantAudioMimeType,
      );
    } catch (error) {
      if (mounted) {
        setState(() {
          _error = _describeError(error);
          _status = 'Turn failed. Listening again...';
        });
      }
    } finally {
      _resetTurn();
      _finalizing = false;
      if (mounted) {
        setState(() => _processing = false);
      }
      if (_enabled) await _startListeningLoop();
    }
  }

  Future<void> _playReply(Uint8List bytes, String mimeType) async {
    final player = _ensurePlayer();
    await player.stop();
    await player.play(BytesSource(bytes, mimeType: mimeType));
    try {
      await player.onPlayerComplete.first.timeout(const Duration(seconds: 45));
    } on TimeoutException {
      // Some platforms may not emit a completion event consistently for bytes playback.
    }
  }

  Future<void> _replayLastReply() async {
    final audio = _lastAudio;
    if (audio == null || _processing) return;
    try {
      await _playReply(audio, _lastMimeType);
    } catch (error) {
      if (mounted) {
        setState(() => _error = _describeError(error));
      }
    }
  }

  Future<void> _handleRecorderError(Object error) async {
    await _stopRecorder();
    _resetTurn();
    if (!mounted) return;
    setState(() {
      _error = _describeError(error);
      _status = 'Microphone stream stopped unexpectedly.';
    });
  }

  void _resetTurn() {
    _speechDetected = false;
    _speechStartedAt = null;
    _lastVoiceAt = null;
    _preSpeechChunks.clear();
    _turnChunks.clear();
  }

  String _describeError(Object error) =>
      error.toString().replaceFirst('Exception: ', '');

  @override
  void dispose() {
    unawaited(_recSub?.cancel());
    unawaited(_playerStateSub?.cancel());
    unawaited(_player?.dispose());
    unawaited(_recorder?.dispose());
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: const Color(0xFF08131A),
      appBar: AppBar(
        backgroundColor: Colors.transparent,
        elevation: 0,
        title: const Text('Voice Conversation'),
      ),
      body: RefreshIndicator(
        onRefresh: _refreshHealth,
        child: ListView(
          padding: const EdgeInsets.all(16),
          children: [
            Card(
              child: Padding(
                padding: const EdgeInsets.all(16),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(_requiresManualStart ? 'Chrome mode' : 'Mobile mode'),
                    const SizedBox(height: 8),
                    Text(_status),
                    const SizedBox(height: 12),
                    LinearProgressIndicator(
                      value: _micLevel == 0 ? null : _micLevel,
                    ),
                    const SizedBox(height: 12),
                    DropdownButtonFormField<String>(
                      initialValue: _language,
                      items: const [
                        DropdownMenuItem(
                          value: 'bg-BG',
                          child: Text('Bulgarian'),
                        ),
                        DropdownMenuItem(
                          value: 'en-US',
                          child: Text('English'),
                        ),
                      ],
                      onChanged: (_listening || _processing)
                          ? null
                          : (value) {
                              if (value != null) {
                                setState(() => _language = value);
                              }
                            },
                    ),
                    const SizedBox(height: 12),
                    Row(
                      children: [
                        Expanded(
                          child: FilledButton.icon(
                            onPressed: _processing ? null : _toggleConversation,
                            icon: Icon(
                              _enabled ? Icons.stop : Icons.play_arrow,
                            ),
                            label: Text(
                              _enabled
                                  ? 'Stop'
                                  : _requiresManualStart
                                  ? 'Start conversation'
                                  : 'Start / retry',
                            ),
                          ),
                        ),
                        const SizedBox(width: 12),
                        OutlinedButton.icon(
                          onPressed: _lastAudio == null
                              ? null
                              : _replayLastReply,
                          icon: Icon(
                            _playing ? Icons.graphic_eq : Icons.replay,
                          ),
                          label: Text(_playing ? 'Playing' : 'Replay'),
                        ),
                      ],
                    ),
                    if (_error != null) ...[
                      const SizedBox(height: 12),
                      Text(
                        _error!,
                        style: const TextStyle(color: Colors.redAccent),
                      ),
                    ],
                  ],
                ),
              ),
            ),
            const SizedBox(height: 12),
            Card(
              child: Padding(
                padding: const EdgeInsets.all(16),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Row(
                      children: [
                        const Expanded(child: Text('Gateway health')),
                        IconButton(
                          onPressed: _loadingHealth ? null : _refreshHealth,
                          icon: _loadingHealth
                              ? const SizedBox(
                                  width: 18,
                                  height: 18,
                                  child: CircularProgressIndicator(
                                    strokeWidth: 2,
                                  ),
                                )
                              : const Icon(Icons.refresh),
                        ),
                      ],
                    ),
                    if (_healthError != null)
                      Text(
                        _healthError!,
                        style: const TextStyle(color: Colors.redAccent),
                      )
                    else if (_health != null)
                      Wrap(
                        spacing: 8,
                        runSpacing: 8,
                        children: _health!.providers.entries
                            .map(
                              (e) => Chip(label: Text('${e.key}: ${e.value}')),
                            )
                            .toList(),
                      ),
                    if (_providerStatus.isNotEmpty) ...[
                      const SizedBox(height: 12),
                      Wrap(
                        spacing: 8,
                        runSpacing: 8,
                        children: _providerStatus.entries
                            .map(
                              (e) => Chip(label: Text('${e.key}: ${e.value}')),
                            )
                            .toList(),
                      ),
                    ],
                  ],
                ),
              ),
            ),
            const SizedBox(height: 12),
            Card(
              child: Padding(
                padding: const EdgeInsets.all(16),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    const Text('Conversation'),
                    const SizedBox(height: 12),
                    if (_messages.isEmpty)
                      const Text('The first spoken turn will appear here.')
                    else
                      ..._messages.map(
                        (message) => Padding(
                          padding: const EdgeInsets.only(bottom: 12),
                          child: Column(
                            crossAxisAlignment: CrossAxisAlignment.start,
                            children: [
                              Text(
                                message.role,
                                style: const TextStyle(
                                  fontWeight: FontWeight.bold,
                                ),
                              ),
                              const SizedBox(height: 4),
                              Text(message.text),
                            ],
                          ),
                        ),
                      ),
                  ],
                ),
              ),
            ),
          ],
        ),
      ),
    );
  }
}

class _Message {
  const _Message(this.role, this.text);

  final String role;
  final String text;
}

Uint8List _joinChunks(List<Uint8List> chunks) {
  final builder = BytesBuilder(copy: false);
  for (final chunk in chunks) {
    builder.add(chunk);
  }
  return builder.toBytes();
}

Uint8List _wrapPcmAsWav(
  Uint8List pcmBytes, {
  required int sampleRate,
  required int channels,
}) {
  final header = ByteData(44);
  final byteRate = sampleRate * channels * 2;
  final blockAlign = channels * 2;
  final dataLength = pcmBytes.length;
  void writeAscii(int offset, String value) {
    for (var i = 0; i < value.length; i++) {
      header.setUint8(offset + i, value.codeUnitAt(i));
    }
  }

  writeAscii(0, 'RIFF');
  header.setUint32(4, 36 + dataLength, Endian.little);
  writeAscii(8, 'WAVE');
  writeAscii(12, 'fmt ');
  header.setUint32(16, 16, Endian.little);
  header.setUint16(20, 1, Endian.little);
  header.setUint16(22, channels, Endian.little);
  header.setUint32(24, sampleRate, Endian.little);
  header.setUint32(28, byteRate, Endian.little);
  header.setUint16(32, blockAlign, Endian.little);
  header.setUint16(34, 16, Endian.little);
  writeAscii(36, 'data');
  header.setUint32(40, dataLength, Endian.little);
  return Uint8List.fromList([...header.buffer.asUint8List(), ...pcmBytes]);
}

double _pcmLevel(Uint8List chunk) {
  if (chunk.lengthInBytes < 2) return 0;
  final bytes = ByteData.sublistView(chunk);
  final sampleCount = chunk.lengthInBytes ~/ 2;
  var sumSquares = 0.0;
  for (var offset = 0; offset < chunk.lengthInBytes; offset += 2) {
    final sample = bytes.getInt16(offset, Endian.little) / 32768.0;
    sumSquares += sample * sample;
  }
  return math.sqrt(sumSquares / sampleCount).clamp(0.0, 1.0).toDouble();
}
