import 'dart:async';
import 'dart:typed_data';

import 'package:audioplayers/audioplayers.dart';
import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';
import 'package:flutter_dotenv/flutter_dotenv.dart';
import 'package:record/record.dart';

import 'src/api/voice_gateway_client.dart';

const _bg = Color(0xFF06141B);
const _panel = Color(0xFF0F2630);
const _panelAlt = Color(0xFF153744);
const _accent = Color(0xFF36CFC9);
const _warm = Color(0xFFFFB86B);
const _text = Color(0xFFF4FEFF);
const _muted = Color(0xFF93B7BE);

class VoiceLabScreen extends StatefulWidget {
  const VoiceLabScreen({super.key});

  @override
  State<VoiceLabScreen> createState() => _VoiceLabScreenState();
}

class _VoiceLabScreenState extends State<VoiceLabScreen> {
  static const _sampleRate = 16000;
  static const _channels = 1;

  late final VoiceGatewayClient _client;
  final _ttsController = TextEditingController(
    text: 'Здравей! Това е тест на гласовия gateway.',
  );
  final List<Uint8List> _pcmChunks = [];

  AudioRecorder? _recorder;
  AudioPlayer? _player;
  StreamSubscription<Amplitude>? _ampSub;
  StreamSubscription<Uint8List>? _recSub;
  StreamSubscription<PlayerState>? _playerStateSub;
  Completer<void>? _recordingDone;

  bool _isRecording = false;
  bool _isTranscribing = false;
  bool _isSynthesizing = false;
  bool _isPlaying = false;
  bool _isLoadingHealth = true;

  double _micLevel = 0;
  String _transcript = 'Hold the button and speak. Release it to run STT.';
  String _selectedLanguage = 'bg-BG';
  String? _sttProvider;
  String? _ttsProvider;
  String? _sttError;
  String? _ttsError;
  String? _healthError;
  VoiceGatewayHealth? _health;
  Uint8List? _lastAudio;
  String _lastMimeType = 'audio/wav';

  @override
  void initState() {
    super.initState();
    _client = VoiceGatewayClient(baseUrl: _resolveBaseUrl());
    unawaited(_refreshHealth());
  }

  String _resolveBaseUrl() {
    try {
      final configured = dotenv.get('API_BASE_URL');
      if (configured.trim().isNotEmpty) {
        return configured.trim();
      }
    } catch (_) {}
    return kIsWeb ? 'http://localhost:8000' : 'http://10.0.2.2:8000';
  }

  Future<AudioRecorder> _ensureRecorder() async =>
      _recorder ??= AudioRecorder();

  AudioPlayer _ensurePlayer() {
    final existing = _player;
    if (existing != null) {
      return existing;
    }
    final player = AudioPlayer();
    _playerStateSub = player.onPlayerStateChanged.listen((state) {
      if (!mounted) return;
      setState(() => _isPlaying = state == PlayerState.playing);
    });
    _player = player;
    return player;
  }

  Future<void> _refreshHealth({bool silent = false}) async {
    if (!silent && mounted) {
      setState(() {
        _isLoadingHealth = true;
        _healthError = null;
      });
    }
    try {
      final health = await _client.getHealth();
      if (!mounted) return;
      setState(() {
        _health = health;
        _isLoadingHealth = false;
      });
    } catch (error) {
      if (!mounted) return;
      setState(() {
        _healthError = _describeError(error);
        _isLoadingHealth = false;
      });
    }
  }

  Future<void> _toggleTapRecording() async {
    if (_isRecording) {
      await _stopRecordingAndTranscribe();
    } else {
      await _startRecording();
    }
  }

  Future<void> _startRecording() async {
    if (_isRecording || _isTranscribing || _isSynthesizing) return;
    try {
      final recorder = await _ensureRecorder();
      if (!await recorder.hasPermission()) {
        throw const VoiceGatewayException(
          'Microphone permission is required to test speech-to-text.',
        );
      }

      await _recSub?.cancel();
      await _ampSub?.cancel();
      _recordingDone = Completer<void>();
      _pcmChunks.clear();

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
        _pcmChunks.add,
        onDone: () => _recordingDone?.complete(),
        onError: (Object error, StackTrace stackTrace) {
          if (!(_recordingDone?.isCompleted ?? true)) {
            _recordingDone?.completeError(error, stackTrace);
          }
        },
        cancelOnError: true,
      );

      _ampSub = recorder
          .onAmplitudeChanged(const Duration(milliseconds: 100))
          .listen((amp) {
            if (!mounted) return;
            setState(
              () => _micLevel = (((amp.current + 45) / 45).clamp(
                0,
                1,
              )).toDouble(),
            );
          });

      await _player?.stop();
      if (!mounted) return;
      setState(() {
        _isRecording = true;
        _sttError = null;
        _transcript = 'Listening... release the button when you are done.';
        _micLevel = 0.18;
      });
    } catch (error) {
      if (!mounted) return;
      setState(() {
        _sttError = _describeError(error);
        _isRecording = false;
        _micLevel = 0;
      });
    }
  }

  Future<void> _stopRecordingAndTranscribe() async {
    if (!_isRecording || _recorder == null) return;
    setState(() {
      _isRecording = false;
      _isTranscribing = true;
      _micLevel = 0;
    });
    try {
      await _recorder!.stop();
      if (_recordingDone != null) {
        await _recordingDone!.future.timeout(const Duration(seconds: 2));
      }
      final pcmBytes = _collapseBytes(_pcmChunks);
      if (pcmBytes.isEmpty) {
        throw const VoiceGatewayException(
          'No audio was captured. Try holding the button a bit longer.',
        );
      }
      final response = await _client.transcribe(
        audioBytes: _wrapPcm16AsWav(
          pcmBytes,
          sampleRate: _sampleRate,
          channels: _channels,
        ),
        language: _selectedLanguage,
      );
      if (!mounted) return;
      setState(() {
        _transcript = response.transcript;
        _sttProvider = response.provider;
        _sttError = null;
      });
      unawaited(_refreshHealth(silent: true));
    } catch (error) {
      if (!mounted) return;
      setState(() => _sttError = _describeError(error));
    } finally {
      await _ampSub?.cancel();
      _ampSub = null;
      await _recSub?.cancel();
      _recSub = null;
      _recordingDone = null;
      if (mounted) setState(() => _isTranscribing = false);
    }
  }

  Future<void> _speakTypedText() async {
    final text = _ttsController.text.trim();
    if (text.isEmpty || _isSynthesizing) return;
    FocusScope.of(context).unfocus();
    setState(() {
      _isSynthesizing = true;
      _ttsError = null;
    });
    try {
      final response = await _client.speak(text: text);
      _lastAudio = response.audioBytes;
      _lastMimeType = response.mimeType;
      if (!mounted) return;
      setState(() => _ttsProvider = response.provider);
      await _playAudio(response.audioBytes, mimeType: response.mimeType);
      unawaited(_refreshHealth(silent: true));
    } catch (error) {
      if (!mounted) return;
      setState(() => _ttsError = _describeError(error));
    } finally {
      if (mounted) setState(() => _isSynthesizing = false);
    }
  }

  Future<void> _playLastAudio() async {
    if (_lastAudio == null || _isSynthesizing) return;
    try {
      await _playAudio(_lastAudio!, mimeType: _lastMimeType);
    } catch (error) {
      if (mounted) setState(() => _ttsError = _describeError(error));
    }
  }

  Future<void> _playAudio(Uint8List bytes, {required String mimeType}) async {
    final player = _ensurePlayer();
    await player.stop();
    await player.play(BytesSource(bytes, mimeType: mimeType));
  }

  String _describeError(Object error) =>
      error.toString().replaceFirst('Exception: ', '');

  @override
  void dispose() {
    _ttsController.dispose();
    unawaited(_ampSub?.cancel());
    unawaited(_recSub?.cancel());
    unawaited(_playerStateSub?.cancel());
    unawaited(_player?.dispose());
    unawaited(_recorder?.dispose());
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: _bg,
      body: SafeArea(
        child: RefreshIndicator(
          color: _accent,
          onRefresh: _refreshHealth,
          child: ListView(
            physics: const BouncingScrollPhysics(
              parent: AlwaysScrollableScrollPhysics(),
            ),
            padding: const EdgeInsets.fromLTRB(20, 18, 20, 24),
            children: [
              _hero(),
              const SizedBox(height: 14),
              _healthCard(),
              const SizedBox(height: 16),
              _sttCard(),
              const SizedBox(height: 16),
              _ttsCard(),
            ],
          ),
        ),
      ),
    );
  }

  Widget _hero() {
    return Container(
      padding: const EdgeInsets.all(22),
      decoration: BoxDecoration(
        gradient: const LinearGradient(
          colors: [Color(0xFF0E2D38), Color(0xFF164B55), Color(0xFF3D2E24)],
          begin: Alignment.topLeft,
          end: Alignment.bottomRight,
        ),
        borderRadius: BorderRadius.circular(28),
        boxShadow: [
          BoxShadow(
            color: Colors.black.withValues(alpha: 0.28),
            blurRadius: 28,
            offset: const Offset(0, 18),
          ),
        ],
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Container(
            padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
            decoration: BoxDecoration(
              color: Colors.white.withValues(alpha: 0.12),
              borderRadius: BorderRadius.circular(999),
            ),
            child: const Text(
              'VOICE LAB',
              style: TextStyle(
                color: _text,
                fontSize: 12,
                fontWeight: FontWeight.w800,
                letterSpacing: 1.4,
              ),
            ),
          ),
          const SizedBox(height: 18),
          const Text(
            'Test STT and TTS against the backend voice gateway.',
            style: TextStyle(
              color: _text,
              fontSize: 28,
              fontWeight: FontWeight.w800,
              height: 1.05,
            ),
          ),
          const SizedBox(height: 12),
          const Text(
            'Push to talk for speech-to-text, then type anything you want spoken back through text-to-speech.',
            style: TextStyle(color: _muted, fontSize: 15, height: 1.45),
          ),
          const SizedBox(height: 18),
          Wrap(
            spacing: 10,
            runSpacing: 10,
            children: [
              _miniChip(
                icon: Icons.mic_rounded,
                label: _isRecording ? 'Recording live' : 'Mic ready',
                accent: _accent,
              ),
              _miniChip(
                icon: Icons.graphic_eq_rounded,
                label: _selectedLanguage,
                accent: _warm,
              ),
              _miniChip(
                icon: Icons.volume_up_rounded,
                label: _isPlaying ? 'Playing audio' : 'Playback ready',
                accent: const Color(0xFF9DEDB3),
              ),
            ],
          ),
        ],
      ),
    );
  }

  Widget _healthCard() {
    return _card(
      color: _panel,
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              const Expanded(
                child: Text(
                  'Gateway Health',
                  style: TextStyle(
                    color: _text,
                    fontSize: 18,
                    fontWeight: FontWeight.w700,
                  ),
                ),
              ),
              IconButton(
                onPressed: _isLoadingHealth ? null : () => _refreshHealth(),
                icon: _isLoadingHealth
                    ? const SizedBox(
                        width: 18,
                        height: 18,
                        child: CircularProgressIndicator(strokeWidth: 2),
                      )
                    : const Icon(Icons.refresh_rounded, color: _muted),
              ),
            ],
          ),
          if (_healthError != null) ...[
            const SizedBox(height: 8),
            Text(
              _healthError!,
              style: const TextStyle(color: Color(0xFFFF9797)),
            ),
          ] else if (_health != null) ...[
            const SizedBox(height: 10),
            Wrap(
              spacing: 10,
              runSpacing: 10,
              children: _health!.providers.entries
                  .map((entry) => _statusPill(entry.key, entry.value))
                  .toList(),
            ),
          ] else ...[
            const SizedBox(height: 8),
            const Text(
              'Pull to refresh if the backend has just started.',
              style: TextStyle(color: _muted),
            ),
          ],
        ],
      ),
    );
  }

  Widget _sttCard() {
    final isBusy = _isRecording || _isTranscribing;
    return _card(
      color: _panelAlt,
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          const Text(
            'Speech to Text',
            style: TextStyle(
              color: _text,
              fontSize: 22,
              fontWeight: FontWeight.w800,
            ),
          ),
          const SizedBox(height: 8),
          const Text(
            'Hold the button to record. Release it to upload the captured audio to the STT endpoint.',
            style: TextStyle(color: _muted, height: 1.4),
          ),
          const SizedBox(height: 18),
          DropdownButtonFormField<String>(
            initialValue: _selectedLanguage,
            decoration: InputDecoration(
              labelText: 'Recognition language',
              labelStyle: const TextStyle(color: _muted),
              filled: true,
              fillColor: Colors.black.withValues(alpha: 0.14),
              border: OutlineInputBorder(
                borderRadius: BorderRadius.circular(18),
                borderSide: BorderSide.none,
              ),
            ),
            dropdownColor: _panel,
            style: const TextStyle(color: _text),
            items: const [
              DropdownMenuItem(
                value: 'bg-BG',
                child: Text('Bulgarian (bg-BG)'),
              ),
              DropdownMenuItem(value: 'en-US', child: Text('English (en-US)')),
            ],
            onChanged: isBusy
                ? null
                : (value) {
                    if (value != null) {
                      setState(() => _selectedLanguage = value);
                    }
                  },
          ),
          const SizedBox(height: 20),
          Center(
            child: GestureDetector(
              onLongPressStart: (_) => _startRecording(),
              onLongPressEnd: (_) => _stopRecordingAndTranscribe(),
              child: AnimatedContainer(
                duration: const Duration(milliseconds: 140),
                width: 182 + (_micLevel * 18),
                height: 182 + (_micLevel * 18),
                decoration: BoxDecoration(
                  shape: BoxShape.circle,
                  gradient: LinearGradient(
                    colors: _isRecording
                        ? const [Color(0xFFFF915B), Color(0xFFFF5F6D)]
                        : const [Color(0xFF2AD6C7), Color(0xFF137E7F)],
                  ),
                  boxShadow: [
                    BoxShadow(
                      color: (_isRecording ? Colors.redAccent : _accent)
                          .withValues(alpha: 0.35),
                      blurRadius: 30 + (_micLevel * 18),
                      spreadRadius: 1 + (_micLevel * 2),
                    ),
                  ],
                ),
                child: Column(
                  mainAxisAlignment: MainAxisAlignment.center,
                  children: [
                    Icon(
                      _isRecording
                          ? Icons.mic_rounded
                          : Icons.keyboard_voice_rounded,
                      color: Colors.white,
                      size: 48,
                    ),
                    const SizedBox(height: 8),
                    Text(
                      _isRecording ? 'Release to send' : 'Hold to talk',
                      style: const TextStyle(
                        color: Colors.white,
                        fontSize: 16,
                        fontWeight: FontWeight.w800,
                      ),
                    ),
                    const SizedBox(height: 4),
                    Padding(
                      padding: const EdgeInsets.symmetric(horizontal: 18),
                      child: Text(
                        _isRecording
                            ? 'Recording now'
                            : 'Long press or tap below',
                        textAlign: TextAlign.center,
                        style: TextStyle(
                          color: Colors.white.withValues(alpha: 0.82),
                          fontSize: 12,
                          height: 1.35,
                        ),
                      ),
                    ),
                  ],
                ),
              ),
            ),
          ),
          const SizedBox(height: 16),
          SizedBox(
            width: double.infinity,
            child: FilledButton.icon(
              onPressed: _isTranscribing ? null : _toggleTapRecording,
              icon: _isTranscribing
                  ? const SizedBox(
                      width: 18,
                      height: 18,
                      child: CircularProgressIndicator(
                        strokeWidth: 2.2,
                        color: Colors.white,
                      ),
                    )
                  : Icon(
                      _isRecording
                          ? Icons.stop_rounded
                          : Icons.play_arrow_rounded,
                    ),
              style: FilledButton.styleFrom(
                backgroundColor: Colors.black.withValues(alpha: 0.24),
                foregroundColor: _text,
                padding: const EdgeInsets.symmetric(vertical: 16),
              ),
              label: Text(
                _isRecording
                    ? 'Stop and transcribe'
                    : _isTranscribing
                    ? 'Transcribing...'
                    : 'Tap to start or stop',
              ),
            ),
          ),
          const SizedBox(height: 18),
          Container(
            width: double.infinity,
            padding: const EdgeInsets.all(16),
            decoration: BoxDecoration(
              color: Colors.black.withValues(alpha: 0.18),
              borderRadius: BorderRadius.circular(20),
            ),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                const Text(
                  'Transcript',
                  style: TextStyle(
                    color: _muted,
                    fontSize: 12,
                    fontWeight: FontWeight.w700,
                    letterSpacing: 1.1,
                  ),
                ),
                const SizedBox(height: 10),
                Text(
                  _transcript,
                  style: const TextStyle(
                    color: _text,
                    fontSize: 18,
                    fontWeight: FontWeight.w600,
                    height: 1.35,
                  ),
                ),
                if (_sttProvider != null) ...[
                  const SizedBox(height: 14),
                  _metaLine('Provider', _sttProvider!),
                ],
                if (_sttError != null) ...[
                  const SizedBox(height: 14),
                  Text(
                    _sttError!,
                    style: const TextStyle(
                      color: Color(0xFFFF9797),
                      height: 1.4,
                    ),
                  ),
                ],
              ],
            ),
          ),
        ],
      ),
    );
  }

  Widget _ttsCard() {
    return _card(
      color: _panel,
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          const Text(
            'Text to Speech',
            style: TextStyle(
              color: _text,
              fontSize: 22,
              fontWeight: FontWeight.w800,
            ),
          ),
          const SizedBox(height: 8),
          const Text(
            'Type anything here and the frontend will call the TTS endpoint, then play the returned audio immediately.',
            style: TextStyle(color: _muted, height: 1.4),
          ),
          const SizedBox(height: 18),
          TextField(
            controller: _ttsController,
            minLines: 4,
            maxLines: 6,
            style: const TextStyle(color: _text, fontSize: 16, height: 1.4),
            decoration: InputDecoration(
              hintText: 'Type a phrase you want spoken out loud...',
              hintStyle: const TextStyle(color: _muted),
              filled: true,
              fillColor: Colors.black.withValues(alpha: 0.18),
              border: OutlineInputBorder(
                borderRadius: BorderRadius.circular(20),
                borderSide: BorderSide.none,
              ),
            ),
          ),
          const SizedBox(height: 16),
          Wrap(
            spacing: 10,
            runSpacing: 10,
            children: [
              _suggestion(
                'Здравей! Това е проверка на текста към реч.',
                () => _ttsController.text =
                    'Здравей! Това е проверка на текста към реч.',
              ),
              _suggestion(
                'Напомни ми да звънна на Мария следобед.',
                () => _ttsController.text =
                    'Напомни ми да звънна на Мария следобед.',
              ),
            ],
          ),
          const SizedBox(height: 18),
          Row(
            children: [
              Expanded(
                child: FilledButton.icon(
                  onPressed: _isSynthesizing ? null : _speakTypedText,
                  icon: _isSynthesizing
                      ? const SizedBox(
                          width: 18,
                          height: 18,
                          child: CircularProgressIndicator(
                            strokeWidth: 2.2,
                            color: Colors.white,
                          ),
                        )
                      : const Icon(Icons.volume_up_rounded),
                  style: FilledButton.styleFrom(
                    backgroundColor: _warm,
                    foregroundColor: const Color(0xFF2E1B05),
                    padding: const EdgeInsets.symmetric(vertical: 16),
                  ),
                  label: Text(_isSynthesizing ? 'Generating...' : 'Speak text'),
                ),
              ),
              const SizedBox(width: 12),
              OutlinedButton.icon(
                onPressed: _lastAudio == null ? null : _playLastAudio,
                style: OutlinedButton.styleFrom(
                  foregroundColor: _text,
                  side: BorderSide(color: Colors.white.withValues(alpha: 0.16)),
                  padding: const EdgeInsets.symmetric(
                    horizontal: 16,
                    vertical: 16,
                  ),
                ),
                icon: Icon(
                  _isPlaying ? Icons.graphic_eq : Icons.replay_rounded,
                ),
                label: const Text('Replay'),
              ),
            ],
          ),
          if (_ttsProvider != null || _ttsError != null) ...[
            const SizedBox(height: 16),
            Container(
              width: double.infinity,
              padding: const EdgeInsets.all(16),
              decoration: BoxDecoration(
                color: Colors.black.withValues(alpha: 0.18),
                borderRadius: BorderRadius.circular(20),
              ),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  if (_ttsProvider != null)
                    _metaLine('Provider', _ttsProvider!),
                  if (_ttsError != null) ...[
                    if (_ttsProvider != null) const SizedBox(height: 10),
                    Text(
                      _ttsError!,
                      style: const TextStyle(
                        color: Color(0xFFFF9797),
                        height: 1.4,
                      ),
                    ),
                  ],
                ],
              ),
            ),
          ],
        ],
      ),
    );
  }

  Widget _card({required Color color, required Widget child}) {
    return Container(
      padding: const EdgeInsets.all(20),
      decoration: BoxDecoration(
        color: color,
        borderRadius: BorderRadius.circular(28),
        border: Border.all(color: Colors.white.withValues(alpha: 0.06)),
      ),
      child: child,
    );
  }

  Widget _miniChip({
    required IconData icon,
    required String label,
    required Color accent,
  }) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 9),
      decoration: BoxDecoration(
        color: Colors.black.withValues(alpha: 0.18),
        borderRadius: BorderRadius.circular(999),
        border: Border.all(color: accent.withValues(alpha: 0.35)),
      ),
      child: Row(
        mainAxisSize: MainAxisSize.min,
        children: [
          Icon(icon, color: accent, size: 18),
          const SizedBox(width: 8),
          Text(
            label,
            style: const TextStyle(
              color: _text,
              fontSize: 13,
              fontWeight: FontWeight.w700,
            ),
          ),
        ],
      ),
    );
  }

  Widget _statusPill(String label, String value) {
    final isReady = value.contains('ready') || value.contains('configured');
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 10),
      decoration: BoxDecoration(
        color: Colors.black.withValues(alpha: 0.16),
        borderRadius: BorderRadius.circular(18),
      ),
      child: Row(
        mainAxisSize: MainAxisSize.min,
        children: [
          Container(
            width: 10,
            height: 10,
            decoration: BoxDecoration(
              color: isReady
                  ? const Color(0xFF75E3A2)
                  : const Color(0xFFFF8A8A),
              shape: BoxShape.circle,
            ),
          ),
          const SizedBox(width: 8),
          Text(
            '${label.toUpperCase()}: $value',
            style: const TextStyle(
              color: _text,
              fontSize: 13,
              fontWeight: FontWeight.w600,
            ),
          ),
        ],
      ),
    );
  }

  Widget _metaLine(String label, String value) {
    return RichText(
      text: TextSpan(
        children: [
          TextSpan(
            text: '$label: ',
            style: const TextStyle(
              color: _muted,
              fontSize: 13,
              fontWeight: FontWeight.w700,
            ),
          ),
          TextSpan(
            text: value,
            style: const TextStyle(
              color: _text,
              fontSize: 13,
              fontWeight: FontWeight.w500,
            ),
          ),
        ],
      ),
    );
  }

  Widget _suggestion(String text, VoidCallback onTap) {
    return ActionChip(
      onPressed: onTap,
      backgroundColor: Colors.black.withValues(alpha: 0.16),
      side: BorderSide(color: Colors.white.withValues(alpha: 0.08)),
      label: Text(
        text,
        style: const TextStyle(
          color: _text,
          fontSize: 13,
          fontWeight: FontWeight.w600,
        ),
      ),
    );
  }
}

Uint8List _collapseBytes(List<Uint8List> chunks) {
  final builder = BytesBuilder(copy: false);
  for (final chunk in chunks) {
    builder.add(chunk);
  }
  return builder.toBytes();
}

Uint8List _wrapPcm16AsWav(
  Uint8List pcmBytes, {
  required int sampleRate,
  required int channels,
}) {
  final header = ByteData(44);
  final byteRate = sampleRate * channels * 2;
  final blockAlign = channels * 2;
  final dataLength = pcmBytes.length;

  void writeAscii(int offset, String value) {
    for (var index = 0; index < value.length; index++) {
      header.setUint8(offset + index, value.codeUnitAt(index));
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

  final builder = BytesBuilder(copy: false)
    ..add(header.buffer.asUint8List())
    ..add(pcmBytes);
  return builder.toBytes();
}
