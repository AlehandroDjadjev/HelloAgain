import 'dart:async';
import 'dart:convert';
import 'dart:math' as math;
import 'dart:ui' show lerpDouble;

import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:http/http.dart' as http;
import 'package:shared_preferences/shared_preferences.dart';

import 'browser_voice_bridge.dart';

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();
  await SystemChrome.setPreferredOrientations(const [
    DeviceOrientation.portraitUp,
  ]);
  runApp(const AgentBoardApp());
}

class AgentBoardApp extends StatelessWidget {
  const AgentBoardApp({super.key});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'Hello Again',
      debugShowCheckedModeBanner: false,
      theme: ThemeData(
        scaffoldBackgroundColor: _BoardPalette.appShell,
        colorScheme: ColorScheme.fromSeed(
          seedColor: _BoardPalette.accent,
          brightness: Brightness.light,
          surface: _BoardPalette.surface,
          primary: _BoardPalette.ink,
        ),
        textTheme: ThemeData.light().textTheme.apply(
          bodyColor: _BoardPalette.ink,
          displayColor: _BoardPalette.ink,
        ),
        useMaterial3: true,
        scaffoldBackgroundColor: const Color(0xFFF5EFE6),
        colorScheme: ColorScheme.fromSeed(
          seedColor: const Color(0xFFBB5A3C),
          brightness: Brightness.light,
          surface: const Color(0xFFFFFBF7),
        ),
      ),
      home: const HelloAgainShell(),
    );
  }
}

enum HelloAgainStage { booting, intro, onboarding, board }

class HelloAgainShell extends StatefulWidget {
  const HelloAgainShell({super.key});

  @override
  State<HelloAgainShell> createState() => _HelloAgainShellState();
}

class _HelloAgainShellState extends State<HelloAgainShell> {
  static const _tokenKey = 'hello_again.account_token';

  late final AgentBackendClient _backendClient;
  late final BrowserVoiceBridge _voiceBridge;

  SharedPreferences? _prefs;
  HelloAgainStage _stage = HelloAgainStage.booting;
  AppAccountSession? _session;
  bool _showContinue = false;
  bool _isListening = false;
  bool _isWorking = false;
  bool _isConfirming = false;
  int _currentStepIndex = 0;
  String _statusText = 'Подготвяме Hello Again...';
  String _promptText = '';
  String _transcriptPreview = '';
  final Map<String, String> _answers = <String, String>{};

  static const List<_RegistrationStep> _steps = [
    _RegistrationStep(
      id: 'name',
      title: 'Вашето име',
      prompt: 'Здравейте. Кажете ми как искате приложението да Ви нарича.',
    ),
    _RegistrationStep(
      id: 'phone_number',
      title: 'Телефонен номер',
      prompt:
          'Сега кажете телефонния си номер бавно, цифра по цифра, за да създам профила Ви.',
    ),
    _RegistrationStep(
      id: 'description',
      title: 'Няколко думи за Вас',
      prompt:
          'Разкажете ми с няколко спокойни изречения какъв човек сте, за да Ви опозная.',
    ),
    _RegistrationStep(
      id: 'ideal_company',
      title: 'Приятна компания',
      prompt:
          'С какви хора се чувствате най-спокойно и приятно да прекарвате време?',
    ),
    _RegistrationStep(
      id: 'favorite_things',
      title: 'Любими теми',
      prompt:
          'За какво най-много обичате да говорите или какво обичате да правите напоследък?',
    ),
    _RegistrationStep(
      id: 'good_meetup',
      title: 'Хубава среща',
      prompt: 'Какво прави една среща топла, спокойна и успешна за Вас?',
    ),
  ];

  @override
  void initState() {
    super.initState();
    _backendClient = AgentBackendClient();
    _voiceBridge = createBrowserVoiceBridge();
    unawaited(_bootstrap());
  }

  @override
  void dispose() {
    _voiceBridge.stopRecognition();
    _voiceBridge.stopAudio();
    super.dispose();
  }

  Future<void> _bootstrap() async {
    final prefs = await SharedPreferences.getInstance();
    AppAccountSession? session;
    final storedToken = prefs.getString(_tokenKey) ?? '';
    if (storedToken.isNotEmpty) {
      try {
        session = await _backendClient.fetchCurrentSession(token: storedToken);
      } catch (_) {
        await prefs.remove(_tokenKey);
      }
    }

    if (!mounted) return;
    setState(() {
      _prefs = prefs;
      _session = session;
      _stage = HelloAgainStage.intro;
      _statusText = session == null
          ? 'Спокойното начало е готово.'
          : 'Добре дошли отново. Отварям Вашето място.';
    });
  }

  void _handleIntroFinished() {
    if (_session != null) {
      setState(() {
        _stage = HelloAgainStage.board;
      });
      return;
    }
    setState(() {
      _showContinue = true;
      _statusText = 'Натиснете „Продължи“ и ще Ви преведа през регистрацията.';
    });
  }

  Future<void> _startRegistration() async {
    if (_isWorking) return;
    setState(() {
      _showContinue = false;
      _stage = HelloAgainStage.onboarding;
      _currentStepIndex = 0;
      _answers.clear();
      _transcriptPreview = '';
      _isConfirming = false;
    });
    await _runCurrentStep();
  }

  Future<void> _runCurrentStep() async {
    final step = _steps[_currentStepIndex];
    if (!mounted) return;
    setState(() {
      _promptText = step.prompt;
      _statusText = 'Сега ще Ви задам въпроса на глас.';
      _transcriptPreview = '';
      _isWorking = true;
      _isListening = false;
      _isConfirming = false;
    });

    try {
      while (mounted) {
        await _speakOnboardingText(step.prompt);
        if (!mounted) return;
        setState(() {
          _isListening = true;
          _isConfirming = false;
          _statusText = 'Слушам Ви внимателно...';
        });

        final capturedTurn = await _voiceBridge.captureAudioTurn(
          language: 'bg-BG',
        );
        final transcript = await _resolveCapturedTranscript(capturedTurn);
        final normalized = _normalizeAnswer(step.id, transcript);

        if (normalized.isEmpty) {
          await _speakOnboardingText(
            'Не успях да Ви чуя добре. Ще повторя въпроса.',
          );
          if (!mounted) return;
          setState(() {
            _isListening = false;
            _statusText = 'Не успях да чуя отговора ясно. Повтарям въпроса.';
          });
          continue;
        }

        if (!mounted) return;
        setState(() {
          _transcriptPreview = normalized;
          _isListening = false;
          _isConfirming = true;
          _statusText =
              'Чух: „$normalized“. Кажете „да“ за потвърждение или „не“ за повторение.';
        });

        final confirmed = await _confirmTranscript(normalized);
        if (!mounted) return;
        if (!confirmed) {
          setState(() {
            _isConfirming = false;
            _statusText = 'Добре, ще задам въпроса отново.';
          });
          continue;
        }

        _answers[step.id] = normalized;
        break;
      }

      if (!mounted) return;
      setState(() {
        _isConfirming = false;
      });

      if (_currentStepIndex == _steps.length - 1) {
        await _submitRegistration();
        return;
      }

      setState(() {
        _currentStepIndex += 1;
      });
      await Future<void>.delayed(const Duration(milliseconds: 260));
      await _runCurrentStep();
    } catch (error) {
      if (!mounted) return;
      setState(() {
        _isListening = false;
        _isConfirming = false;
        _isWorking = false;
        _statusText =
            'Не успях да чуя ясно. Натиснете веднъж и ще повторя текущия въпрос.';
      });
    }
  }

  Future<bool> _confirmTranscript(String transcript) async {
    await _speakOnboardingText(
      'Чух: $transcript. Ако това е правилно, кажете да. Ако не е правилно, кажете не и ще повторя въпроса.',
    );

    while (mounted) {
      setState(() {
        _isListening = true;
        _statusText = 'Моля, кажете само „да“ или „не“.';
      });

      final capturedTurn = await _voiceBridge.captureAudioTurn(
        language: 'bg-BG',
      );
      final confirmation = await _resolveCapturedTranscript(capturedTurn);
      final normalized = _normalizeConfirmationAnswer(confirmation);
      if (normalized != null) {
        setState(() {
          _isListening = false;
        });
        return normalized;
      }

      setState(() {
        _isListening = false;
        _statusText = 'Не разбрах потвърждението. Ще попитам отново.';
      });
      await _speakOnboardingText('Не разбрах. Моля кажете само да или не.');
    }

    return false;
  }

  Future<String> _resolveCapturedTranscript(
    CapturedAudioTurn capturedTurn,
  ) async {
    final directTranscript = (capturedTurn.transcript ?? '').trim();
    if (directTranscript.isNotEmpty) {
      return directTranscript;
    }
    final payload = await _backendClient.transcribeSpeechTurn(
      audioBase64: capturedTurn.audioBase64,
      audioMimeType: capturedTurn.mimeType,
      userId: 'hello_again_registration',
      sessionId: 'registration_${DateTime.now().millisecondsSinceEpoch}',
      language: capturedTurn.language,
    );
    return (payload['transcript'] ?? payload['message'] ?? '')
        .toString()
        .trim();
  }

  String _normalizeAnswer(String stepId, String transcript) {
    final clean = transcript.trim();
    if (stepId != 'phone_number') {
      return clean;
    }

    final digitWords = <String, String>{
      'zero': '0',
      'oh': '0',
      'one': '1',
      'two': '2',
      'three': '3',
      'four': '4',
      'for': '4',
      'five': '5',
      'six': '6',
      'seven': '7',
      'eight': '8',
      'nine': '9',
      'plus': '+',
      'нула': '0',
      'едно': '1',
      'две': '2',
      'два': '2',
      'три': '3',
      'четири': '4',
      'пет': '5',
      'шест': '6',
      'седем': '7',
      'осем': '8',
      'девет': '9',
    };

    digitWords.addAll(const {
      'нула': '0',
      'едно': '1',
      'две': '2',
      'два': '2',
      'три': '3',
      'четири': '4',
      'пет': '5',
      'шест': '6',
      'седем': '7',
      'осем': '8',
      'девет': '9',
      'плюс': '+',
    });

    final pieces = clean
        .toLowerCase()
        .replaceAll('-', ' ')
        .split(RegExp(r'\s+'))
        .where((item) => item.trim().isNotEmpty);

    final buffer = StringBuffer();
    for (final piece in pieces) {
      if (digitWords.containsKey(piece)) {
        buffer.write(digitWords[piece]);
      } else {
        buffer.write(piece.replaceAll(RegExp(r'[^0-9+]'), ''));
      }
    }
    return buffer.toString().trim();
  }

  bool? _normalizeConfirmationAnswer(String transcript) {
    final words = transcript
        .trim()
        .toLowerCase()
        .replaceAll(RegExp(r'[^a-zа-я0-9\s]+', caseSensitive: false), ' ')
        .split(RegExp(r'\s+'))
        .where((item) => item.isNotEmpty)
        .toList();

    const yesWords = {
      'да',
      'yes',
      'yep',
      'correct',
      'правилно',
      'точно',
      'добре',
      'става',
    };
    const noWords = {'не', 'no', 'wrong', 'грешно', 'повтори', 'отново'};

    if (words.any(yesWords.contains)) {
      return true;
    }
    if (words.any(noWords.contains)) {
      return false;
    }
    return null;
  }

  Future<void> _speakOnboardingText(String text) async {
    try {
      final payload = await _backendClient.speakText(
        text: text,
        language: 'bg-BG',
      );
      final audioBase64 = (payload['audio_base64'] ?? '').toString().trim();
      final mimeType = (payload['audio_mime_type'] ?? 'audio/wav')
          .toString()
          .trim();
      if (audioBase64.isNotEmpty) {
        await _voiceBridge.playBase64Audio(
          audioBase64: audioBase64,
          mimeType: mimeType.isEmpty ? 'audio/wav' : mimeType,
        );
        return;
      }
    } catch (_) {}

    await _voiceBridge.playText(text);
  }

  Future<void> _submitRegistration() async {
    if (!mounted) return;
    setState(() {
      _statusText = 'Създавам Вашия профил...';
      _isWorking = true;
      _isConfirming = false;
    });

    final onboardingAnswers = <String, String>{
      'ideal_company': _answers['ideal_company'] ?? '',
      'favorite_things': _answers['favorite_things'] ?? '',
      'good_meetup': _answers['good_meetup'] ?? '',
    };

    try {
      final session = await _backendClient.registerVoiceProfile(
        name: _answers['name'] ?? '',
        phoneNumber: _answers['phone_number'] ?? '',
        description: _answers['description'] ?? '',
        onboardingAnswers: onboardingAnswers,
      );
      await _prefs?.setString(_tokenKey, session.token);
      if (!mounted) return;
      setState(() {
        _session = session;
        _stage = HelloAgainStage.board;
        _isWorking = false;
      });
    } catch (error) {
      if (!mounted) return;
      setState(() {
        _isWorking = false;
        _statusText =
            'Регистрацията не можа да завърши. Натиснете веднъж и ще повторя текущата стъпка. ${error.toString()}';
      });
    }
  }

  @override
  Widget build(BuildContext context) {
    switch (_stage) {
      case HelloAgainStage.booting:
        return const Scaffold(body: Center(child: CircularProgressIndicator()));
      case HelloAgainStage.intro:
        return IntroOnboardingScreen(
          showContinue: _showContinue,
          statusText: _statusText,
          onFinished: _handleIntroFinished,
          onContinue: _startRegistration,
        );
      case HelloAgainStage.onboarding:
        final step = _steps[_currentStepIndex];
        return RegistrationScreen(
          title: step.title,
          prompt: _promptText,
          statusText: _statusText,
          transcript: _transcriptPreview,
          isListening: _isListening,
          isWorking: _isWorking,
          isConfirming: _isConfirming,
          stepNumber: _currentStepIndex + 1,
          stepCount: _steps.length,
          onRetry: _runCurrentStep,
        );
      case HelloAgainStage.board:
        final session = _session;
        return AgentBoardScreen(
          userId: session?.userId.toString() ?? 'whitespace_frontend',
          accountToken: session?.token,
          welcomeText: session == null
              ? null
              : 'Добре дошли, ${session.displayName}. Вашето място е готово.',
        );
    }
  }
}

class IntroOnboardingScreen extends StatefulWidget {
  const IntroOnboardingScreen({
    super.key,
    required this.showContinue,
    required this.statusText,
    required this.onFinished,
    required this.onContinue,
  });

  final bool showContinue;
  final String statusText;
  final VoidCallback onFinished;
  final Future<void> Function() onContinue;

  @override
  State<IntroOnboardingScreen> createState() => _IntroOnboardingScreenState();
}

class _IntroOnboardingScreenState extends State<IntroOnboardingScreen>
    with SingleTickerProviderStateMixin {
  late final AnimationController _controller;
  bool _finished = false;

  @override
  void initState() {
    super.initState();
    _controller = AnimationController(
      vsync: this,
      duration: const Duration(milliseconds: 1400),
    )..forward();
    _controller.addStatusListener((status) {
      if (!_finished && status == AnimationStatus.completed) {
        _finished = true;
        widget.onFinished();
      }
    });
  }

  @override
  void dispose() {
    _controller.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final slide = Tween<Offset>(
      begin: const Offset(0, 1.4),
      end: Offset.zero,
    ).animate(CurvedAnimation(parent: _controller, curve: Curves.easeOutCubic));

    return Scaffold(
      backgroundColor: const Color(0xFFF4EDE3),
      body: _WarmPaperBackground(
        child: SafeArea(
          child: Padding(
            padding: const EdgeInsets.fromLTRB(24, 24, 24, 30),
            child: Column(
              children: [
                const Spacer(),
                SlideTransition(
                  position: slide,
                  child: const Text(
                    'Hello Again',
                    textAlign: TextAlign.center,
                    style: TextStyle(
                      fontSize: 48,
                      fontWeight: FontWeight.w700,
                      color: Color(0xFF3C2A20),
                      letterSpacing: -1.2,
                    ),
                  ),
                ),
                const SizedBox(height: 18),
                Text(
                  widget.statusText,
                  textAlign: TextAlign.center,
                  style: const TextStyle(
                    fontSize: 17,
                    height: 1.45,
                    color: Color(0xFF6A5447),
                  ),
                ),
                const Spacer(),
                AnimatedOpacity(
                  opacity: widget.showContinue ? 1 : 0,
                  duration: const Duration(milliseconds: 420),
                  curve: Curves.easeOut,
                  child: AnimatedSlide(
                    offset: widget.showContinue
                        ? Offset.zero
                        : const Offset(0, 0.16),
                    duration: const Duration(milliseconds: 420),
                    curve: Curves.easeOutCubic,
                    child: IgnorePointer(
                      ignoring: !widget.showContinue,
                      child: SizedBox(
                        width: double.infinity,
                        child: FilledButton(
                          onPressed: widget.onContinue,
                          style: FilledButton.styleFrom(
                            backgroundColor: const Color(0xFFB56B4D),
                            foregroundColor: Colors.white,
                            padding: const EdgeInsets.symmetric(vertical: 18),
                            shape: RoundedRectangleBorder(
                              borderRadius: BorderRadius.circular(22),
                            ),
                          ),
                          child: const Text(
                            'Продължи',
                            style: TextStyle(
                              fontSize: 18,
                              fontWeight: FontWeight.w700,
                            ),
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
      ),
    );
  }
}

class RegistrationScreen extends StatelessWidget {
  const RegistrationScreen({
    super.key,
    required this.title,
    required this.prompt,
    required this.statusText,
    required this.transcript,
    required this.isListening,
    required this.isWorking,
    required this.isConfirming,
    required this.stepNumber,
    required this.stepCount,
    required this.onRetry,
  });

  final String title;
  final String prompt;
  final String statusText;
  final String transcript;
  final bool isListening;
  final bool isWorking;
  final bool isConfirming;
  final int stepNumber;
  final int stepCount;
  final Future<void> Function() onRetry;

  @override
  Widget build(BuildContext context) {
    final progress = stepCount == 0 ? 0.0 : stepNumber / stepCount;
    final indicatorColor = isListening
        ? const Color(0xFFB56B4D)
        : isConfirming
        ? const Color(0xFF7A8B67)
        : const Color(0xFFC8B6A1);

    return Scaffold(
      backgroundColor: const Color(0xFFF4EDE3),
      body: _WarmPaperBackground(
        child: SafeArea(
          child: Padding(
            padding: const EdgeInsets.fromLTRB(16, 18, 16, 18),
            child: Column(
              children: [
                Container(
                  width: double.infinity,
                  padding: const EdgeInsets.fromLTRB(20, 18, 20, 20),
                  decoration: BoxDecoration(
                    color: Colors.white.withValues(alpha: 0.82),
                    borderRadius: BorderRadius.circular(28),
                    border: Border.all(color: const Color(0xFFE5D6C7)),
                    boxShadow: [
                      BoxShadow(
                        color: const Color(0xFF8B6A55).withValues(alpha: 0.09),
                        blurRadius: 26,
                        offset: const Offset(0, 12),
                      ),
                    ],
                  ),
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Text(
                        'Стъпка $stepNumber от $stepCount',
                        style: const TextStyle(
                          fontSize: 13,
                          fontWeight: FontWeight.w700,
                          color: Color(0xFF8E725F),
                          letterSpacing: 0.2,
                        ),
                      ),
                      const SizedBox(height: 12),
                      ClipRRect(
                        borderRadius: BorderRadius.circular(999),
                        child: LinearProgressIndicator(
                          value: progress.clamp(0, 1),
                          minHeight: 6,
                          backgroundColor: const Color(0xFFE9DDD1),
                          valueColor: const AlwaysStoppedAnimation<Color>(
                            Color(0xFFB56B4D),
                          ),
                        ),
                      ),
                      const SizedBox(height: 22),
                      Text(
                        title,
                        style: const TextStyle(
                          fontSize: 28,
                          height: 1.1,
                          fontWeight: FontWeight.w700,
                          color: Color(0xFF2F241D),
                        ),
                      ),
                      const SizedBox(height: 14),
                      Text(
                        prompt,
                        style: const TextStyle(
                          fontSize: 18,
                          height: 1.48,
                          color: Color(0xFF625247),
                        ),
                      ),
                    ],
                  ),
                ),
                const SizedBox(height: 16),
                Expanded(
                  child: Container(
                    width: double.infinity,
                    padding: const EdgeInsets.all(20),
                    decoration: BoxDecoration(
                      color: Colors.white.withValues(alpha: 0.70),
                      borderRadius: BorderRadius.circular(28),
                      border: Border.all(color: const Color(0xFFE8DCCF)),
                    ),
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        AnimatedContainer(
                          duration: const Duration(milliseconds: 220),
                          padding: const EdgeInsets.symmetric(
                            horizontal: 12,
                            vertical: 8,
                          ),
                          decoration: BoxDecoration(
                            color: indicatorColor.withValues(alpha: 0.12),
                            borderRadius: BorderRadius.circular(999),
                          ),
                          child: Row(
                            mainAxisSize: MainAxisSize.min,
                            children: [
                              Container(
                                width: 10,
                                height: 10,
                                decoration: BoxDecoration(
                                  color: indicatorColor,
                                  shape: BoxShape.circle,
                                ),
                              ),
                              const SizedBox(width: 8),
                              Text(
                                isListening
                                    ? 'Слушам'
                                    : isConfirming
                                    ? 'Чакам потвърждение'
                                    : 'Вашият отговор',
                                style: TextStyle(
                                  fontSize: 14,
                                  fontWeight: FontWeight.w700,
                                  color: indicatorColor,
                                ),
                              ),
                            ],
                          ),
                        ),
                        const SizedBox(height: 18),
                        Text(
                          transcript.isEmpty
                              ? 'Говорете спокойно. Ще попълня отговора вместо Вас.'
                              : transcript,
                          style: const TextStyle(
                            fontSize: 24,
                            height: 1.42,
                            color: Color(0xFF312620),
                          ),
                        ),
                        const Spacer(),
                        Container(
                          width: double.infinity,
                          padding: const EdgeInsets.all(16),
                          decoration: BoxDecoration(
                            color: const Color(0xFFF8F1E8),
                            borderRadius: BorderRadius.circular(20),
                          ),
                          child: Text(
                            statusText,
                            style: const TextStyle(
                              fontSize: 15,
                              height: 1.45,
                              color: Color(0xFF6D5A4E),
                            ),
                          ),
                        ),
                      ],
                    ),
                  ),
                ),
                const SizedBox(height: 14),
                SizedBox(
                  width: double.infinity,
                  child: OutlinedButton(
                    onPressed: isWorking ? null : onRetry,
                    style: OutlinedButton.styleFrom(
                      foregroundColor: const Color(0xFF6B5444),
                      side: const BorderSide(color: Color(0xFFD6C4B2)),
                      backgroundColor: Colors.white.withValues(alpha: 0.60),
                      padding: const EdgeInsets.symmetric(vertical: 18),
                      shape: RoundedRectangleBorder(
                        borderRadius: BorderRadius.circular(22),
                      ),
                    ),
                    child: Text(
                      isListening ? 'Слушам...' : 'Повтори въпроса',
                      style: const TextStyle(
                        fontSize: 18,
                        fontWeight: FontWeight.w700,
                      ),
                    ),
                  ),
                ),
              ],
            ),
          ),
        ),
      ),
    );
  }
}

class _WarmPaperBackground extends StatelessWidget {
  const _WarmPaperBackground({required this.child});

  final Widget child;

  @override
  Widget build(BuildContext context) {
    return Stack(
      fit: StackFit.expand,
      children: [
        const ColoredBox(color: Color(0xFFF4EDE3)),
        Positioned(
          top: -28,
          right: -10,
          child: _BackdropOrb(diameter: 180, color: const Color(0xFFE8D7C6)),
        ),
        Positioned(
          top: 120,
          left: -46,
          child: _BackdropOrb(diameter: 128, color: const Color(0xFFE3D4C3)),
        ),
        Positioned(
          bottom: -44,
          right: 18,
          child: _BackdropOrb(diameter: 168, color: const Color(0xFFDDCBB8)),
        ),
        Positioned.fill(
          child: DecoratedBox(
            decoration: BoxDecoration(
              color: Colors.white.withValues(alpha: 0.06),
              border: Border.all(color: Colors.white.withValues(alpha: 0.22)),
            ),
          ),
        ),
        child,
      ],
    );
  }
}

class _BackdropOrb extends StatelessWidget {
  const _BackdropOrb({required this.diameter, required this.color});

  final double diameter;
  final Color color;

  @override
  Widget build(BuildContext context) {
    return Container(
      width: diameter,
      height: diameter,
      decoration: BoxDecoration(
        shape: BoxShape.circle,
        color: color.withValues(alpha: 0.42),
        boxShadow: [
          BoxShadow(
            color: color.withValues(alpha: 0.22),
            blurRadius: 48,
            spreadRadius: 8,
          ),
        ],
      ),
    );
  }
}

class _RegistrationStep {
  const _RegistrationStep({
    required this.id,
    required this.title,
    required this.prompt,
  });

  final String id;
  final String title;
  final String prompt;
}

class AppAccountSession {
  const AppAccountSession({
    required this.token,
    required this.userId,
    required this.displayName,
  });

  final String token;
  final int userId;
  final String displayName;
}

class AgentBoardScreen extends StatefulWidget {
  const AgentBoardScreen({
    super.key,
    required this.userId,
    this.accountToken,
    this.welcomeText,
  });

  final String userId;
  final String? accountToken;
  final String? welcomeText;

  @override
  State<AgentBoardScreen> createState() => _AgentBoardScreenState();
}

class _AgentBoardScreenState extends State<AgentBoardScreen> {
  late final SceneController _sceneController;
  late final AgentBackendClient _backendClient;
  late final BrowserVoiceBridge _voiceBridge;
  final TextEditingController _promptController = TextEditingController();
  late final String _sessionId;
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
  Timer? _debugRefreshDebounce;
  bool _debugPanelOpen = true;
  bool _debugAutoApply = false;
  String _reasoningProvider = 'qwen';
  String _debugMemoryType = 'ram';
  bool _debugDeleteAfterClick = false;
  int _debugColorIndex = 0;
  double _debugWidth = 184;
  double _debugHeight = 164;
  double _debugScale = 1;
  double _debugX = 72;
  double _debugY = 168;
  double _debugInnerInset = 18;
  late final TextEditingController _debugNameController;
  late final TextEditingController _debugTextController;

  @override
  void initState() {
    super.initState();
    _sceneController = SceneController();
    _backendClient = AgentBackendClient();
    _voiceBridge = createBrowserVoiceBridge();
    _debugNameController = TextEditingController(text: 'style_studio_card');
    _debugTextController = TextEditingController(text: 'Calm, readable memory');
    _sessionId = 'whitespace_${DateTime.now().millisecondsSinceEpoch}';
    if ((widget.welcomeText ?? '').trim().isNotEmpty) {
      _lastSpeech = widget.welcomeText!.trim();
    }
    unawaited(_hydrateBoardFromBackend());
  }

  @override
  void dispose() {
    _voiceLoopEnabled = false;
    _voiceLoopToken += 1;
    _debugRefreshDebounce?.cancel();
    _voiceBridge.stopRecognition();
    _voiceBridge.stopAudio();
    _sceneController.dispose();
    _promptController.dispose();
    _debugNameController.dispose();
    _debugTextController.dispose();
    super.dispose();
  }

  Future<void> _hydrateBoardFromBackend() async {
    try {
      final payload = await _backendClient.fetchBoardMemory(
        token: widget.accountToken,
      );
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
        userId: widget.userId,
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
        userId: widget.userId,
        sessionId: _sessionId,
        reasoningProvider: _reasoningProvider,
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
    final directTranscript = (capturedTurn.transcript ?? '').trim();
    if (directTranscript.isNotEmpty) {
      return directTranscript;
    }
    final payload = await _backendClient.transcribeSpeechTurn(
      audioBase64: capturedTurn.audioBase64,
      audioMimeType: capturedTurn.mimeType,
      userId: widget.userId,
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
      } else if (speechText.isNotEmpty) {
        _trackSpeechPlayback(
          _voiceBridge.playText(speechText).catchError((_) {}),
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

  Color get _selectedDebugColor =>
      _debugObjectColors[_debugColorIndex
          .clamp(0, _debugObjectColors.length - 1)
          .toInt()];

  Future<void> _applyDebugDraft({bool announce = true}) async {
    final name = _debugNameController.text.trim();
    if (name.isEmpty) {
      if (!mounted) return;
      setState(() {
        _statusText =
            'Debug studio needs an object name before it can preview.';
      });
      return;
    }

    await _sceneController.executeCommandMap({
      'action': 'create',
      'name': name,
      'text': _debugTextController.text.trim().isEmpty
          ? name
          : _debugTextController.text.trim(),
      'width': _debugWidth,
      'height': _debugHeight,
      'x': _debugX,
      'y': _debugY,
      'baseScale': _debugScale,
      'innerInset': _debugInnerInset,
      'memoryType': _debugMemoryType,
      'deleteAfterClick': _debugDeleteAfterClick,
      'color': _selectedDebugColor.toARGB32(),
    });

    if (!mounted || !announce) return;
    setState(() {
      _statusText =
          'Debug studio refreshed "$name" so you can inspect the new styling.';
    });
  }

  void _scheduleDebugRefresh() {
    if (!_debugAutoApply) return;
    _debugRefreshDebounce?.cancel();
    _debugRefreshDebounce = Timer(const Duration(milliseconds: 140), () {
      unawaited(_applyDebugDraft(announce: false));
    });
  }

  Future<void> _moveDebugObject(Size boardSize) async {
    final name = _debugNameController.text.trim();
    if (name.isEmpty) {
      await _applyDebugDraft();
      return;
    }

    if (!_sceneController.objects.containsKey(name)) {
      await _applyDebugDraft(announce: false);
    }

    final maxX = math.max(0.0, boardSize.width - _debugWidth);
    final maxY = math.max(0.0, boardSize.height - _debugHeight);
    final emptyRect = _sceneController.findLargestEmptyRect();
    double targetX;
    double targetY;

    if (emptyRect != null &&
        emptyRect.width >= _debugWidth &&
        emptyRect.height >= _debugHeight) {
      targetX = emptyRect.left + ((emptyRect.width - _debugWidth) * 0.08);
      targetY = emptyRect.top + ((emptyRect.height - _debugHeight) * 0.08);
    } else {
      targetX = (_debugX + boardSize.width * 0.23) % (maxX == 0 ? 1 : maxX);
      targetY = (_debugY + boardSize.height * 0.18) % (maxY == 0 ? 1 : maxY);
    }

    targetX = targetX.clamp(0.0, maxX).toDouble();
    targetY = targetY.clamp(0.0, maxY).toDouble();

    await _sceneController.executeCommandMap({
      'action': 'move',
      'name': name,
      'x': targetX,
      'y': targetY,
    });

    if (!mounted) return;
    setState(() {
      _debugX = targetX;
      _debugY = targetY;
      _statusText = 'Debug studio moved "$name" to a new spot on the board.';
    });
  }

  Future<void> _scaleDebugObject({required bool enlarge}) async {
    final name = _debugNameController.text.trim();
    if (name.isEmpty) {
      await _applyDebugDraft();
      return;
    }

    if (!_sceneController.objects.containsKey(name)) {
      await _applyDebugDraft(announce: false);
    }

    await _sceneController.executeCommandMap({
      'action': enlarge ? 'enlarge' : 'shrink',
      'name': name,
      'factor': enlarge ? 1.14 : 0.88,
    });

    final object = _sceneController.objects[name];
    if (!mounted || object == null) return;
    setState(() {
      _debugScale = object.baseScale;
      _statusText = enlarge
          ? 'Debug studio enlarged "$name" for animation testing.'
          : 'Debug studio shrank "$name" for animation testing.';
    });
  }

  Future<void> _deleteDebugObject() async {
    final name = _debugNameController.text.trim();
    if (name.isEmpty) return;
    if (!_sceneController.objects.containsKey(name)) {
      if (!mounted) return;
      setState(() {
        _statusText = 'There is no "$name" object on the board to delete yet.';
      });
      return;
    }

    await _sceneController.executeCommandMap({
      'action': 'delete',
      'name': name,
    });

    if (!mounted) return;
    setState(() {
      _statusText = 'Debug studio triggered the delete animation for "$name".';
    });
  }

  Future<void> _openDebugObject() async {
    final name = _debugNameController.text.trim();
    final object = _sceneController.objects[name];
    if (object == null) {
      if (!mounted) return;
      setState(() {
        _statusText = 'Create the debug object first, then you can open it.';
      });
      return;
    }
    await _openObjectResult(object);
  }

  void _loadDebugValuesFromObject(SceneObjectData object) {
    setState(() {
      _debugNameController.text = object.name;
      _debugTextController.text = object.text;
      _debugWidth = object.width;
      _debugHeight = object.height;
      _debugScale = object.baseScale;
      _debugX = object.x;
      _debugY = object.y;
      _debugInnerInset = object.innerInset;
      _debugMemoryType = object.memoryType;
      _debugDeleteAfterClick = object.deleteAfterClick;
      final paletteIndex = _debugObjectColors.indexWhere(
        (color) => color.toARGB32() == object.color.toARGB32(),
      );
      _debugColorIndex = paletteIndex >= 0 ? paletteIndex : 0;
      _statusText =
          'Debug studio imported "${object.name}" so you can restyle it.';
    });
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
                final isCompact = constraints.maxWidth < 720;
                final horizontalPadding = isCompact ? 14.0 : 18.0;
                final topInset = isCompact ? 14.0 : 18.0;
                final bottomInset = isCompact ? 14.0 : 18.0;
                final isCompact = constraints.maxWidth < 1100;
                final studioWidth = _debugPanelOpen
                    ? math.min(
                        isCompact ? constraints.maxWidth - 32 : 360.0,
                        360.0,
                      )
                    : 58.0;
                final composerWidth = isCompact
                    ? constraints.maxWidth - 52
                    : math.min(
                        560.0,
                        math.max(
                          320.0,
                          constraints.maxWidth - studioWidth - 96,
                        ),
                      );

                return Stack(
                  children: [
                    Positioned.fill(
                      child: DecoratedBox(
                        decoration: const BoxDecoration(
                          gradient: LinearGradient(
                            begin: Alignment.topLeft,
                            end: Alignment.bottomRight,
                            colors: [
                              Color(0xFFF6F2FF),
                              Color(0xFFEFF8FF),
                              Color(0xFFFFF4F8),
                            ],
                          ),
                        ),
                        child: Stack(
                          children: const [
                            Positioned(
                              top: -120,
                              right: -80,
                              child: _BoardBackdropOrb(
                                diameter: 300,
                                color: Color(0x30C6D8FF),
                              ),
                            ),
                            Positioned(
                              bottom: -160,
                              left: -100,
                              child: _BoardBackdropOrb(
                                diameter: 360,
                                color: Color(0x2FFFD3E1),
                              ),
                            ),
                          ],
                        ),
                      ),
                    ),
                    Positioned.fill(
                      child: Padding(
                        padding: const EdgeInsets.all(14),
                        child: LayoutBuilder(
                          builder: (context, boardConstraints) {
                            final boardSize = Size(
                              boardConstraints.maxWidth,
                              boardConstraints.maxHeight,
                            );
                            _sceneController.setBoardSize(boardSize);

                            return ClipRRect(
                              borderRadius: BorderRadius.circular(34),
                              child: DecoratedBox(
                                decoration: BoxDecoration(
                                  color: _BoardPalette.boardBase,
                                  boxShadow: [
                                    BoxShadow(
                                      color: _BoardPalette.shadow,
                                      blurRadius: 28,
                                      offset: const Offset(0, 14),
                                    ),
                                  ],
                                ),
                                child: Stack(
                                  children: [
                                    Positioned.fill(
                                      child: DecoratedBox(
                                        decoration: const BoxDecoration(
                                          gradient: LinearGradient(
                                            begin: Alignment.topCenter,
                                            end: Alignment.bottomCenter,
                                            colors: [
                                              Color(0xFFFFFFFF),
                                              Color(0xFFF8FBFF),
                                              Color(0xFFFFF8FB),
                                            ],
                                          ),
                                        ),
                                      ),
                                    ),
                                    Positioned.fill(
                                      child: DecoratedBox(
                                        decoration: BoxDecoration(
                                          gradient: LinearGradient(
                                            begin: Alignment.topLeft,
                                            end: Alignment.bottomRight,
                                            colors: [
                                              const Color(0x14000000),
                                              Colors.transparent,
                                              const Color(0x12FFFFFF),
                                            ],
                                          ),
                                        ),
                                      ),
                                    ),
                                    Positioned.fill(
                                      child: CustomPaint(
                                        painter: GridPainter(),
                                      ),
                                    ),
                                    Positioned.fill(
                                      child: DecoratedBox(
                                        decoration: BoxDecoration(
                                          gradient: LinearGradient(
                                            begin: Alignment.topCenter,
                                            end: Alignment.bottomCenter,
                                            colors: [
                                              Colors.white.withValues(
                                                alpha: 0.06,
                                              ),
                                              Colors.transparent,
                                              const Color(0x0ABCCEFF),
                                            ],
                                          ),
                                        ),
                                      ),
                                    ),
                                    ..._sceneController.objects.values.map(
                                      (object) => BoardObjectWidget(
                                        key: ValueKey(object.name),
                                        data: object,
                                        onTap: () => _openObjectResult(object),
                                        onDeleteComplete: () => _sceneController
                                            .finalizeDelete(object.name),
                                        onDragPositionChanged: (x, y) {
                                          _sceneController
                                              .setObjectPositionFromDrag(
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
                            );
                          },
                        ),
                      ),
                    ),
                    Positioned(
                      top: 26,
                      left: 26,
                      child: AgentResponseCard(
                        speech: _lastSpeech,
                        status: _statusText,
                        isBusy: _isBusy,
                        compact: isCompact,
                      ),
                    ),
                    Positioned(
                      top: 26,
                      right: 26,
                      child: ConstrainedBox(
                        constraints: BoxConstraints(
                          maxWidth: studioWidth,
                          maxHeight: constraints.maxHeight - 52,
                        ),
                        child: BoardDebugStudio(
                          isOpen: _debugPanelOpen,
                          autoApply: _debugAutoApply,
                          boardSize: Size(
                            constraints.maxWidth - 28,
                            constraints.maxHeight - 28,
                          ),
                          nameController: _debugNameController,
                          textController: _debugTextController,
                          selectedColorIndex: _debugColorIndex,
                          palette: _debugObjectColors,
                          memoryType: _debugMemoryType,
                          deleteAfterClick: _debugDeleteAfterClick,
                          widthValue: _debugWidth,
                          heightValue: _debugHeight,
                          scaleValue: _debugScale,
                          xValue: _debugX,
                          yValue: _debugY,
                          insetValue: _debugInnerInset,
                          reasoningProvider: _reasoningProvider,
                          objectNames: _sceneController.objects.keys.toList()
                            ..sort(),
                          onToggleOpen: () {
                            setState(() {
                              _debugPanelOpen = !_debugPanelOpen;
                            });
                          },
                          onToggleAutoApply: (value) {
                            setState(() {
                              _debugAutoApply = value;
                            });
                            if (value) {
                              _scheduleDebugRefresh();
                            }
                          },
                          onImportObject: (name) {
                            final object = _sceneController.objects[name];
                            if (object != null) {
                              _loadDebugValuesFromObject(object);
                            }
                          },
                          onColorSelected: (index) {
                            setState(() {
                              _debugColorIndex = index;
                            });
                            _scheduleDebugRefresh();
                          },
                          onMemoryTypeChanged: (value) {
                            setState(() {
                              _debugMemoryType = value;
                            });
                            _scheduleDebugRefresh();
                          },
                          onReasoningProviderChanged: (value) {
                            setState(() {
                              _reasoningProvider = value;
                            });
                          },
                          onDeleteAfterClickChanged: (value) {
                            setState(() {
                              _debugDeleteAfterClick = value;
                            });
                            _scheduleDebugRefresh();
                          },
                          onWidthChanged: (value) {
                            setState(() {
                              _debugWidth = value;
                              _debugX = _debugX.clamp(
                                0.0,
                                math.max(
                                  0.0,
                                  constraints.maxWidth - 28 - value,
                                ),
                              );
                            });
                            _scheduleDebugRefresh();
                          },
                          onHeightChanged: (value) {
                            setState(() {
                              _debugHeight = value;
                              _debugY = _debugY.clamp(
                                0.0,
                                math.max(
                                  0.0,
                                  constraints.maxHeight - 28 - value,
                                ),
                              );
                            });
                            _scheduleDebugRefresh();
                          },
                          onScaleChanged: (value) {
                            setState(() {
                              _debugScale = value;
                            });
                            _scheduleDebugRefresh();
                          },
                          onXChanged: (value) {
                            setState(() {
                              _debugX = value;
                            });
                            _scheduleDebugRefresh();
                          },
                          onYChanged: (value) {
                            setState(() {
                              _debugY = value;
                            });
                            _scheduleDebugRefresh();
                          },
                          onInsetChanged: (value) {
                            setState(() {
                              _debugInnerInset = value;
                            });
                            _scheduleDebugRefresh();
                          },
                          onDraftChanged: _scheduleDebugRefresh,
                          onApplyPressed: () {
                            unawaited(_applyDebugDraft());
                          },
                          onMovePressed: () {
                            unawaited(
                              _moveDebugObject(
                                Size(
                                  constraints.maxWidth - 28,
                                  constraints.maxHeight - 28,
                                ),
                              ),
                            );
                          },
                          onEnlargePressed: () {
                            unawaited(_scaleDebugObject(enlarge: true));
                          },
                          onShrinkPressed: () {
                            unawaited(_scaleDebugObject(enlarge: false));
                          },
                          onDeletePressed: () {
                            unawaited(_deleteDebugObject());
                          },
                          onOpenPressed: () {
                            unawaited(_openDebugObject());
                          },
                        ),
                      ),
                    ),
                    Positioned(
                      left: 26,
                      bottom: 26,
                      child: ConstrainedBox(
                        constraints: BoxConstraints(maxWidth: composerWidth),
                        child: _AgentComposer(
                          promptController: _promptController,
                          isBusy: _isBusy,
                          isListening: _isListening,
                          voiceLoopEnabled: _voiceLoopEnabled,
                          onSubmit: _sendPrompt,
                          onToggleVoiceLoop: _toggleVoiceLoop,
                        ),
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
      constraints: const BoxConstraints(maxWidth: 380),
      child: AnimatedContainer(
        duration: const Duration(milliseconds: 260),
        curve: Curves.easeOutCubic,
        padding: const EdgeInsets.all(18),
        decoration: BoxDecoration(
          color: Colors.white.withValues(alpha: 0.78),
          borderRadius: BorderRadius.circular(28),
          boxShadow: const [
            BoxShadow(
              color: _BoardPalette.shadow,
              blurRadius: 20,
              offset: Offset(0, 10),
            ),
          ],
        ),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              children: [
                AnimatedContainer(
                  duration: const Duration(milliseconds: 220),
                  width: 10,
                  height: 10,
                  decoration: BoxDecoration(
                    shape: BoxShape.circle,
                    color: isBusy
                        ? const Color(0xFFC98E64)
                        : const Color(0xFF96A57D),
                    boxShadow: [
                      BoxShadow(
                        color:
                            (isBusy
                                    ? const Color(0x33C98E64)
                                    : const Color(0x3396A57D))
                                .withValues(alpha: 0.9),
                        blurRadius: 12,
                        spreadRadius: 1.4,
                      ),
                    ],
                  ),
                ),
                const SizedBox(width: 10),
                Text(
                  isBusy ? 'Semi Agent Working' : 'Semi Agent',
                  style: const TextStyle(
                    fontSize: 12.5,
                    fontWeight: FontWeight.w700,
                    letterSpacing: 0.5,
                  ),
                ),
              ],
            ),
            const SizedBox(height: 12),
            Text(
              speech,
              style: TextStyle(
                color: _BoardPalette.ink.withValues(alpha: 0.92),
                fontSize: 15,
                fontWeight: FontWeight.w600,
                height: 1.35,
              ),
            ),
            const SizedBox(height: 14),
            Container(
              padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 10),
              decoration: BoxDecoration(
                color: Colors.white.withValues(alpha: 0.72),
                borderRadius: BorderRadius.circular(18),
              ),
              child: Text(
                status,
                style: TextStyle(
                  color: _BoardPalette.ink.withValues(alpha: 0.68),
                  fontSize: 12.5,
                  height: 1.35,
                ),
              ),
            ),
          ],
        ),
      ),
    );
  }
}

class _AgentComposer extends StatelessWidget {
  const _AgentComposer({
    required this.promptController,
    required this.isBusy,
    required this.isListening,
    required this.voiceLoopEnabled,
    required this.onSubmit,
    required this.onToggleVoiceLoop,
  });

  final TextEditingController promptController;
  final bool isBusy;
  final bool isListening;
  final bool voiceLoopEnabled;
  final VoidCallback onSubmit;
  final VoidCallback onToggleVoiceLoop;

  @override
  Widget build(BuildContext context) {
    final voiceEnabled = isListening || voiceLoopEnabled;

    return Container(
      padding: const EdgeInsets.all(10),
      decoration: BoxDecoration(
        color: Colors.white.withValues(alpha: 0.76),
        borderRadius: BorderRadius.circular(30),
        boxShadow: const [
          BoxShadow(
            color: _BoardPalette.shadow,
            blurRadius: 18,
            offset: Offset(0, 10),
          ),
        ],
      ),
      child: Row(
        children: [
          Expanded(
            child: Container(
              decoration: BoxDecoration(
                color: Colors.white.withValues(alpha: 0.82),
                borderRadius: BorderRadius.circular(22),
              ),
              child: TextField(
                enabled: !isBusy && !isListening,
                controller: promptController,
                onSubmitted: (_) => onSubmit(),
                style: const TextStyle(
                  fontSize: 15,
                  fontWeight: FontWeight.w500,
                  height: 1.35,
                ),
                decoration: InputDecoration(
                  hintText: isListening
                      ? 'Listening in Chrome...'
                      : voiceLoopEnabled
                      ? 'Voice mode is active. Speak your next request...'
                      : 'Type a prompt or use the microphone...',
                  hintStyle: TextStyle(
                    color: _BoardPalette.ink.withValues(alpha: 0.42),
                    fontSize: 14,
                  ),
                  border: InputBorder.none,
                  contentPadding: const EdgeInsets.symmetric(
                    horizontal: 18,
                    vertical: 16,
                  ),
                ),
              ),
            ),
          ),
          const SizedBox(width: 10),
          _ComposerButton(
            onTap: (isBusy && !voiceLoopEnabled) ? null : onToggleVoiceLoop,
            background: voiceEnabled
                ? const Color(0xFFB8C0C8)
                : Colors.white.withValues(alpha: 0.72),
            foreground: voiceEnabled ? Colors.white : _BoardPalette.ink,
            icon: voiceEnabled ? Icons.hearing : Icons.mic_none_rounded,
          ),
          const SizedBox(width: 10),
          _ComposerButton(
            onTap: (isBusy || isListening) ? null : onSubmit,
            background: const Color(0xFF5A646C),
            foreground: Colors.white,
            label: isBusy
                ? 'Running'
                : isListening
                ? 'Listening'
                : 'Send',
            isWide: true,
          ),
        ],
      ),
    );
  }
}

class _ComposerButton extends StatelessWidget {
  const _ComposerButton({
    this.onTap,
    required this.background,
    required this.foreground,
    this.icon,
    this.label,
    this.isWide = false,
  });

  final VoidCallback? onTap;
  final Color background;
  final Color foreground;
  final IconData? icon;
  final String? label;
  final bool isWide;

  @override
  Widget build(BuildContext context) {
    final disabled = onTap == null;
    return GestureDetector(
      onTap: onTap,
      child: AnimatedOpacity(
        duration: const Duration(milliseconds: 180),
        opacity: disabled ? 0.45 : 1,
        child: Container(
          width: isWide ? null : 56,
          height: 56,
          padding: EdgeInsets.symmetric(horizontal: isWide ? 18 : 0),
          decoration: BoxDecoration(
            color: background,
            borderRadius: BorderRadius.circular(22),
            boxShadow: [
              BoxShadow(
                color: background.withValues(alpha: 0.18),
                blurRadius: 12,
                offset: const Offset(0, 6),
              ),
            ],
          ),
          alignment: Alignment.center,
          child: label != null
              ? Text(
                  label!,
                  style: TextStyle(
                    color: foreground,
                    fontSize: 14,
                    fontWeight: FontWeight.w700,
                    letterSpacing: 0.2,
                  ),
                )
              : Icon(icon, color: foreground, size: 22),
        ),
      ),
    );
  }
}

class BoardDebugStudio extends StatelessWidget {
  const BoardDebugStudio({
    super.key,
    required this.isOpen,
    required this.autoApply,
    required this.boardSize,
    required this.nameController,
    required this.textController,
    required this.selectedColorIndex,
    required this.palette,
    required this.reasoningProvider,
    required this.memoryType,
    required this.deleteAfterClick,
    required this.widthValue,
    required this.heightValue,
    required this.scaleValue,
    required this.xValue,
    required this.yValue,
    required this.insetValue,
    required this.objectNames,
    required this.onToggleOpen,
    required this.onToggleAutoApply,
    required this.onImportObject,
    required this.onColorSelected,
    required this.onReasoningProviderChanged,
    required this.onMemoryTypeChanged,
    required this.onDeleteAfterClickChanged,
    required this.onWidthChanged,
    required this.onHeightChanged,
    required this.onScaleChanged,
    required this.onXChanged,
    required this.onYChanged,
    required this.onInsetChanged,
    required this.onDraftChanged,
    required this.onApplyPressed,
    required this.onMovePressed,
    required this.onEnlargePressed,
    required this.onShrinkPressed,
    required this.onDeletePressed,
    required this.onOpenPressed,
  });

  final bool isOpen;
  final bool autoApply;
  final Size boardSize;
  final TextEditingController nameController;
  final TextEditingController textController;
  final int selectedColorIndex;
  final List<Color> palette;
  final String reasoningProvider;
  final String memoryType;
  final bool deleteAfterClick;
  final double widthValue;
  final double heightValue;
  final double scaleValue;
  final double xValue;
  final double yValue;
  final double insetValue;
  final List<String> objectNames;
  final VoidCallback onToggleOpen;
  final ValueChanged<bool> onToggleAutoApply;
  final ValueChanged<String> onImportObject;
  final ValueChanged<int> onColorSelected;
  final ValueChanged<String> onReasoningProviderChanged;
  final ValueChanged<String> onMemoryTypeChanged;
  final ValueChanged<bool> onDeleteAfterClickChanged;
  final ValueChanged<double> onWidthChanged;
  final ValueChanged<double> onHeightChanged;
  final ValueChanged<double> onScaleChanged;
  final ValueChanged<double> onXChanged;
  final ValueChanged<double> onYChanged;
  final ValueChanged<double> onInsetChanged;
  final VoidCallback onDraftChanged;
  final VoidCallback onApplyPressed;
  final VoidCallback onMovePressed;
  final VoidCallback onEnlargePressed;
  final VoidCallback onShrinkPressed;
  final VoidCallback onDeletePressed;
  final VoidCallback onOpenPressed;

  @override
  Widget build(BuildContext context) {
    final maxX = math.max(0.0, boardSize.width - widthValue);
    final maxY = math.max(0.0, boardSize.height - heightValue);

    return AnimatedContainer(
      duration: const Duration(milliseconds: 260),
      curve: Curves.easeOutCubic,
      width: isOpen ? null : 58,
      decoration: BoxDecoration(
        color: Colors.white.withValues(alpha: 0.78),
        borderRadius: BorderRadius.circular(28),
        boxShadow: const [
          BoxShadow(
            color: _BoardPalette.shadow,
            blurRadius: 20,
            offset: Offset(0, 10),
          ),
        ],
      ),
      child: isOpen
          ? Padding(
              padding: const EdgeInsets.all(18),
              child: SingleChildScrollView(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Row(
                      children: [
                        const Expanded(
                          child: Column(
                            crossAxisAlignment: CrossAxisAlignment.start,
                            children: [
                              Text(
                                'Style Studio',
                                style: TextStyle(
                                  fontSize: 17,
                                  fontWeight: FontWeight.w700,
                                ),
                              ),
                              SizedBox(height: 4),
                              Text(
                                'Preview layout, motion, and agent engine without touching manual JSON.',
                                style: TextStyle(
                                  fontSize: 12.5,
                                  height: 1.35,
                                  color: _BoardPalette.mutedInk,
                                ),
                              ),
                            ],
                          ),
                        ),
                        IconButton(
                          onPressed: onToggleOpen,
                          icon: const Icon(Icons.close_rounded),
                          color: _BoardPalette.ink,
                        ),
                      ],
                    ),
                    const SizedBox(height: 16),
                    if (objectNames.isNotEmpty)
                      Padding(
                        padding: const EdgeInsets.only(bottom: 12),
                        child: Wrap(
                          spacing: 10,
                          runSpacing: 10,
                          crossAxisAlignment: WrapCrossAlignment.center,
                          children: [
                            _StudioPill(
                              label: '${objectNames.length} live objects',
                              icon: Icons.view_in_ar_rounded,
                            ),
                            PopupMenuButton<String>(
                              onSelected: onImportObject,
                              color: _BoardPalette.surface,
                              itemBuilder: (context) {
                                return objectNames
                                    .map(
                                      (name) => PopupMenuItem<String>(
                                        value: name,
                                        child: Text(name),
                                      ),
                                    )
                                    .toList();
                              },
                              child: const _StudioPill(
                                label: 'Import existing',
                                icon: Icons.file_download_outlined,
                              ),
                            ),
                          ],
                        ),
                      ),
                    _StudioLabel(label: 'Object name'),
                    _StudioTextField(
                      controller: nameController,
                      onChanged: (_) => onDraftChanged(),
                    ),
                    const SizedBox(height: 10),
                    _StudioLabel(label: 'Visible label'),
                    _StudioTextField(
                      controller: textController,
                      onChanged: (_) => onDraftChanged(),
                    ),
                    const SizedBox(height: 16),
                    const _StudioLabel(label: 'Color mood'),
                    Wrap(
                      spacing: 10,
                      runSpacing: 10,
                      children: [
                        for (int index = 0; index < palette.length; index++)
                          _StudioPaletteSwatch(
                            color: palette[index],
                            selected: index == selectedColorIndex,
                            onTap: () => onColorSelected(index),
                          ),
                      ],
                    ),
                    const SizedBox(height: 16),
                    const _StudioLabel(label: 'Reasoning engine'),
                    Wrap(
                      spacing: 8,
                      runSpacing: 8,
                      children: [
                        for (final option in const ['qwen', 'openai'])
                          ChoiceChip(
                            label: Text(
                              option == 'qwen' ? 'Qwen local' : 'OpenAI',
                            ),
                            selected: option == reasoningProvider,
                            onSelected: (_) =>
                                onReasoningProviderChanged(option),
                            selectedColor: _BoardPalette.accentSoft,
                            backgroundColor: Colors.white.withValues(
                              alpha: 0.72,
                            ),
                            labelStyle: TextStyle(
                              color: option == reasoningProvider
                                  ? _BoardPalette.ink
                                  : _BoardPalette.mutedInk,
                              fontWeight: FontWeight.w600,
                            ),
                            side: BorderSide.none,
                          ),
                      ],
                    ),
                    const SizedBox(height: 16),
                    const _StudioLabel(label: 'Memory behavior'),
                    Wrap(
                      spacing: 8,
                      runSpacing: 8,
                      children: [
                        for (final option in const ['ram', 'memory', 'instant'])
                          ChoiceChip(
                            label: Text(option),
                            selected: option == memoryType,
                            onSelected: (_) => onMemoryTypeChanged(option),
                            selectedColor: _BoardPalette.accentSoft,
                            backgroundColor: Colors.white.withValues(
                              alpha: 0.72,
                            ),
                            labelStyle: TextStyle(
                              color: option == memoryType
                                  ? _BoardPalette.ink
                                  : _BoardPalette.mutedInk,
                              fontWeight: FontWeight.w600,
                            ),
                            side: BorderSide.none,
                          ),
                      ],
                    ),
                    const SizedBox(height: 12),
                    SwitchListTile.adaptive(
                      value: deleteAfterClick,
                      onChanged: onDeleteAfterClickChanged,
                      activeThumbColor: _BoardPalette.accent,
                      activeTrackColor: _BoardPalette.accentSoft,
                      contentPadding: EdgeInsets.zero,
                      title: const Text(
                        'Delete after one tap',
                        style: TextStyle(
                          fontSize: 13.5,
                          fontWeight: FontWeight.w600,
                        ),
                      ),
                    ),
                    SwitchListTile.adaptive(
                      value: autoApply,
                      onChanged: onToggleAutoApply,
                      activeThumbColor: _BoardPalette.accent,
                      activeTrackColor: _BoardPalette.accentSoft,
                      contentPadding: EdgeInsets.zero,
                      title: const Text(
                        'Auto apply edits',
                        style: TextStyle(
                          fontSize: 13.5,
                          fontWeight: FontWeight.w600,
                        ),
                      ),
                    ),
                    const SizedBox(height: 4),
                    _StudioSlider(
                      label: 'Width',
                      value: widthValue,
                      min: 120,
                      max: 280,
                      onChanged: onWidthChanged,
                    ),
                    _StudioSlider(
                      label: 'Height',
                      value: heightValue,
                      min: 110,
                      max: 240,
                      onChanged: onHeightChanged,
                    ),
                    _StudioSlider(
                      label: 'Scale',
                      value: scaleValue,
                      min: 0.7,
                      max: 1.8,
                      divisions: 22,
                      precision: 2,
                      onChanged: onScaleChanged,
                    ),
                    _StudioSlider(
                      label: 'Inner inset',
                      value: insetValue,
                      min: 10,
                      max: 30,
                      onChanged: onInsetChanged,
                    ),
                    _StudioSlider(
                      label: 'Horizontal position',
                      value: xValue.clamp(0.0, maxX).toDouble(),
                      min: 0,
                      max: maxX == 0 ? 1 : maxX,
                      onChanged: onXChanged,
                    ),
                    _StudioSlider(
                      label: 'Vertical position',
                      value: yValue.clamp(0.0, maxY).toDouble(),
                      min: 0,
                      max: maxY == 0 ? 1 : maxY,
                      onChanged: onYChanged,
                    ),
                    const SizedBox(height: 8),
                    Wrap(
                      spacing: 10,
                      runSpacing: 10,
                      children: [
                        _StudioActionButton(
                          label: 'Apply card',
                          onTap: onApplyPressed,
                          background: _BoardPalette.ink,
                          foreground: Colors.white,
                        ),
                        _StudioActionButton(
                          label: 'Move',
                          onTap: onMovePressed,
                        ),
                        _StudioActionButton(
                          label: 'Enlarge',
                          onTap: onEnlargePressed,
                        ),
                        _StudioActionButton(
                          label: 'Shrink',
                          onTap: onShrinkPressed,
                        ),
                        _StudioActionButton(
                          label: 'Delete',
                          onTap: onDeletePressed,
                        ),
                        _StudioActionButton(
                          label: 'Open',
                          onTap: onOpenPressed,
                        ),
                      ],
                    ),
                  ],
                ),
              ),
            )
          : IconButton(
              onPressed: onToggleOpen,
              icon: const Icon(Icons.tune_rounded),
              color: _BoardPalette.ink,
              tooltip: 'Open style studio',
            ),
    );
  }
}

class _StudioLabel extends StatelessWidget {
  const _StudioLabel({required this.label});

  final String label;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.only(bottom: 6),
      child: Text(
        label,
        style: const TextStyle(
          fontSize: 12,
          fontWeight: FontWeight.w700,
          color: _BoardPalette.mutedInk,
          letterSpacing: 0.2,
        ),
      ),
    );
  }
}

class _StudioTextField extends StatelessWidget {
  const _StudioTextField({required this.controller, this.onChanged});

  final TextEditingController controller;
  final ValueChanged<String>? onChanged;
  final bool compact;

  @override
  Widget build(BuildContext context) {
    return TextField(
      controller: controller,
      onChanged: onChanged,
      style: const TextStyle(fontSize: 14.5, fontWeight: FontWeight.w500),
      decoration: InputDecoration(
        filled: true,
        fillColor: Colors.white.withValues(alpha: 0.76),
        contentPadding: const EdgeInsets.symmetric(
          horizontal: 14,
          vertical: 12,
        ),
        border: OutlineInputBorder(
          borderRadius: BorderRadius.circular(18),
          borderSide: BorderSide.none,
        ),
        enabledBorder: OutlineInputBorder(
          borderRadius: BorderRadius.circular(18),
          borderSide: BorderSide.none,
        ),
        focusedBorder: OutlineInputBorder(
          borderRadius: BorderRadius.circular(18),
          borderSide: BorderSide.none,
        ),
      ),
    );
  }
}

class _StudioSlider extends StatelessWidget {
  const _StudioSlider({
    required this.label,
    required this.value,
    required this.min,
    required this.max,
    required this.onChanged,
    this.divisions,
    this.precision = 0,
  });

  final String label;
  final double value;
  final double min;
  final double max;
  final ValueChanged<double> onChanged;
  final int? divisions;
  final int precision;

  @override
  Widget build(BuildContext context) {
    final displayValue = precision == 0
        ? value.round().toString()
        : value.toStringAsFixed(precision);

    return Padding(
      padding: const EdgeInsets.only(bottom: 8),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              Expanded(
                child: Text(
                  label,
                  style: const TextStyle(
                    fontSize: 12.5,
                    fontWeight: FontWeight.w600,
                    color: _BoardPalette.mutedInk,
                  ),
                ),
              ),
              Text(
                displayValue,
                style: const TextStyle(
                  fontSize: 12.5,
                  fontWeight: FontWeight.w700,
                ),
              ),
            ],
          ),
          SliderTheme(
            data: SliderTheme.of(context).copyWith(
              activeTrackColor: _BoardPalette.accent,
              inactiveTrackColor: _BoardPalette.accentSoft,
              thumbColor: _BoardPalette.ink,
              overlayColor: _BoardPalette.accent.withValues(alpha: 0.14),
            ),
            child: Slider(
              value: value.clamp(min, max).toDouble(),
              min: min,
              max: max,
              divisions: divisions,
              onChanged: onChanged,
            ),
          ),
        ],
      ),
    );
  }
}

class _StudioActionButton extends StatelessWidget {
  const _StudioActionButton({
    required this.label,
    required this.onTap,
    this.background = _BoardPalette.surface,
    this.foreground = _BoardPalette.ink,
  });

  final String label;
  final VoidCallback onTap;
  final Color background;
  final Color foreground;

  @override
  Widget build(BuildContext context) {
    return GestureDetector(
      onTap: onTap,
      child: Container(
        padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 12),
        decoration: BoxDecoration(
          color: background,
          borderRadius: BorderRadius.circular(18),
          boxShadow: [
            BoxShadow(
              color: background.withValues(alpha: 0.18),
              blurRadius: 14,
              offset: const Offset(0, 8),
            ),
          ],
        ),
        child: Text(
          label,
          style: TextStyle(
            color: foreground,
            fontSize: 13,
            fontWeight: FontWeight.w700,
          ),
        ),
      ),
    );
  }
}

class _StudioPaletteSwatch extends StatelessWidget {
  const _StudioPaletteSwatch({
    required this.color,
    required this.selected,
    required this.onTap,
  });

  final Color color;
  final bool selected;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    return GestureDetector(
      onTap: onTap,
      child: AnimatedContainer(
        duration: const Duration(milliseconds: 180),
        width: 34,
        height: 34,
        decoration: BoxDecoration(
          color: color,
          shape: BoxShape.circle,
          boxShadow: [
            BoxShadow(
              color: (selected ? _BoardPalette.ink : color).withValues(
                alpha: selected ? 0.18 : 0.28,
              ),
              blurRadius: selected ? 18 : 14,
              offset: const Offset(0, 8),
            ),
          ],
        ),
      ),
    );
  }
}

class _StudioPill extends StatelessWidget {
  const _StudioPill({required this.label, required this.icon});

  final String label;
  final IconData icon;

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 9),
      decoration: BoxDecoration(
        color: Colors.white.withValues(alpha: 0.70),
        borderRadius: BorderRadius.circular(999),
      ),
      child: Row(
        mainAxisSize: MainAxisSize.min,
        children: [
          Icon(icon, size: 16, color: _BoardPalette.mutedInk),
          const SizedBox(width: 7),
          Text(
            label,
            style: const TextStyle(fontSize: 12.5, fontWeight: FontWeight.w700),
          ),
        ],
      ),
    );
  }
}

class _BoardBackdropOrb extends StatelessWidget {
  const _BoardBackdropOrb({required this.diameter, required this.color});

  final double diameter;
  final Color color;

  @override
  Widget build(BuildContext context) {
    return IgnorePointer(
      child: Container(
        width: diameter,
        height: diameter,
        decoration: BoxDecoration(
          shape: BoxShape.circle,
          gradient: RadialGradient(
            colors: [color, color.withValues(alpha: 0.16), Colors.transparent],
          ),
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
    final sharedInterests =
        (matchSummary?['shared_interests'] as List?) ?? const [];

    return Column(
      mainAxisSize: MainAxisSize.min,
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        _UserSectionLabel(
          text: 'Name',
          value: (user['display_name'] ?? user['username'] ?? 'Unknown user')
              .toString(),
        ),
        _UserSectionLabel(text: 'Friend status', value: friendStatus),
        if (description.isNotEmpty)
          _UserSectionLabel(text: 'Description', value: description),
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
          _UserSectionLabel(text: 'Email', value: email),
        if (phoneNumber != null && phoneNumber.isNotEmpty)
          _UserSectionLabel(text: 'Phone', value: phoneNumber),
      ],
    );
  }
}

class _UserSectionLabel extends StatelessWidget {
  const _UserSectionLabel({required this.text, required this.value});

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
              color: Colors.black.withValues(alpha: 0.58),
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
      if (defaultTargetPlatform == TargetPlatform.android) {
        return 'http://10.0.2.2:8000';
      }
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

  Future<AppAccountSession> fetchCurrentSession({required String token}) async {
    final payload = await _getJson('/api/accounts/me/', token: token);
    final profile = Map<String, dynamic>.from(
      payload['profile'] as Map? ?? const {},
    );
    return AppAccountSession(
      token: token,
      userId: int.tryParse((profile['user_id'] ?? '0').toString()) ?? 0,
      displayName: (profile['display_name'] ?? profile['name'] ?? 'Friend')
          .toString(),
    );
  }

  Future<AppAccountSession> registerVoiceProfile({
    required String name,
    required String phoneNumber,
    required String description,
    required Map<String, String> onboardingAnswers,
  }) async {
    final payload = await _postJson('/api/accounts/register/', {
      'name': name,
      'phone_number': phoneNumber,
      'description': description,
      'onboarding_answers': onboardingAnswers,
      'phone_permission_granted': true,
      'microphone_permission_granted': true,
      'voice_navigation_enabled': true,
      'onboarding_completed': true,
    });
    final profile = Map<String, dynamic>.from(
      payload['profile'] as Map? ?? const {},
    );
    return AppAccountSession(
      token: (payload['token'] ?? '').toString(),
      userId: int.tryParse((profile['user_id'] ?? '0').toString()) ?? 0,
      displayName: (profile['display_name'] ?? name).toString(),
    );
  }

  Future<Map<String, dynamic>> fetchBoardMemory({String? token}) {
    return _getJson('/api/agent/board-memory/', token: token);
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

  Future<Map<String, dynamic>> speakText({
    required String text,
    String language = 'bg-BG',
  }) {
    return _postJson('/api/voice/speak/', {'text': text, 'language': language});
  }

  Future<Map<String, dynamic>> startAgentRun({
    required String prompt,
    required Map<String, dynamic> boardState,
    required Map<String, dynamic> largestEmptySpace,
    required String userId,
    required String sessionId,
    required String reasoningProvider,
  }) {
    return _postJson('/api/agent/run/start/', {
      'prompt': prompt,
      'board_state': boardState,
      'largest_empty_space': largestEmptySpace,
      'user_id': userId,
      'session_id': sessionId,
      'reasoning_provider': reasoningProvider,
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

  Future<Map<String, dynamic>> _getJson(String path, {String? token}) async {
    final response = await http.get(
      _baseUri.resolve(path),
      headers: _headers(token: token),
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
    Map<String, dynamic> payload, {
    String? token,
  }) async {
    final response = await http.post(
      _baseUri.resolve(path),
      headers: _headers(token: token),
      body: jsonEncode(payload),
    );
    if (response.statusCode < 200 || response.statusCode >= 300) {
      throw Exception(
        'POST $path failed with ${response.statusCode}: ${response.body}',
      );
    }
    return _decodeJson(response.body);
  }

  Map<String, String> _headers({String? token}) {
    final headers = <String, String>{
      'Content-Type': 'application/json; charset=utf-8',
      'Accept': 'application/json',
    };
    if ((token ?? '').trim().isNotEmpty) {
      headers['Authorization'] = 'Token ${token!.trim()}';
    }
    return headers;
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
    Color(0xFFFFC8D9),
    Color(0xFFC8E6FF),
    Color(0xFFD5CCFF),
    Color(0xFFFFDEB8),
    Color(0xFFCCF1D6),
    Color(0xFFFFF0A8),
    Color(0xFFFFD3F3),
    Color(0xFFCFE7DD),
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
      0.08,
    );
  }

  double _randomInnerInset() {
    return 10 + _random.nextInt(16).toDouble();
  }

  Color? _colorFromJson(dynamic value) {
    if (value == null) return null;

    if (value is int) {
      return _desaturateColor(Color(value), 0.08);
    }

    final raw = value.toString().trim();
    if (raw.isEmpty) return null;

    final lower = raw.toLowerCase();

    const byName = <String, Color>{
      'red': Color(0xFFFFC8D9),
      'blue': Color(0xFFC8E6FF),
      'green': Color(0xFFCCF1D6),
      'orange': Color(0xFFFFDEB8),
      'purple': Color(0xFFD5CCFF),
      'yellow': Color(0xFFFFF0A8),
      'teal': Color(0xFFBFEDE8),
      'pink': Color(0xFFFFD3F3),
      'random': Color(0x00000000),
    };

    if (lower == 'random') {
      return _randomMainColor();
    }

    if (byName.containsKey(lower)) {
      return _desaturateColor(byName[lower]!, 0.08);
    }

    final clean = lower.replaceFirst('#', '');
    final hex = clean.length == 6 ? 'ff$clean' : clean;
    final parsed = int.tryParse(hex, radix: 16);
    if (parsed == null) return null;

    return _desaturateColor(Color(parsed), 0.08);
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
  bool _isHovered = false;
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
          duration: const Duration(milliseconds: 760),
        )..addListener(() {
          setState(() {});
        });

    _scaleController =
        AnimationController(
          vsync: this,
          duration: const Duration(milliseconds: 300),
        )..addListener(() {
          setState(() {});
        });

    _deleteController =
        AnimationController(
            vsync: this,
            duration: const Duration(milliseconds: 520),
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
    final milliseconds = (distance / 520.0 * 1000)
        .clamp(320.0, 1600.0)
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
    final moveCompression = lerpDouble(1.0, 0.88, motionEffect)!;
    final moveDarkenAmount = lerpDouble(0.0, 0.18, motionEffect)!;

    final scaleValue = _scaleController.isAnimating
        ? _computeScalePopValue(
            _scaleController.value,
            _scaleAnimStart,
            _scaleAnimTarget,
          )
        : _displayScale;

    final deleteProgress = _deleteController.value;
    final opacity = _computeDeleteOpacity(deleteProgress);
    final deleteScale = _computeDeleteScale(deleteProgress);
    final hoverScale = _isHovered && !_isDragging ? 1.018 : 1.0;
    final visualScale = scaleValue * moveCompression * deleteScale * hoverScale;
    final verticalLift = _isDragging
        ? -4.0
        : (_isHovered ? -2.0 : -1.5 * motionEffect);
    final deleteLift = lerpDouble(
      0.0,
      -18.0,
      Curves.easeOut.transform(deleteProgress),
    )!;
    final borderRadius = BorderRadius.circular(
      math.min(widget.data.width, widget.data.height) * 0.08,
    );
    final baseColor = _darken(widget.data.color, moveDarkenAmount);

    return Positioned(
      left: livePosition.dx,
      top: livePosition.dy,
      child: Transform.translate(
        offset: Offset(0, verticalLift + deleteLift),
        child: Transform.scale(
          scale: visualScale,
          alignment: Alignment.center,
          child: Opacity(
            opacity: opacity,
            child: RepaintBoundary(
              child: MouseRegion(
                cursor: SystemMouseCursors.click,
                onEnter: (_) {
                  if (_isDragging) return;
                  setState(() {
                    _isHovered = true;
                  });
                },
                onExit: (_) {
                  setState(() {
                    _isHovered = false;
                  });
                },
                child: GestureDetector(
                  behavior: HitTestBehavior.opaque,
                  onTap: widget.onTap,
                  onPanStart: (details) {
                    final box = context.findRenderObject() as RenderBox?;
                    if (box == null) return;
                    _dragPointerOffset = box.globalToLocal(
                      details.globalPosition,
                    );
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
                    child: DecoratedBox(
                      decoration: BoxDecoration(
                        color: baseColor,
                        borderRadius: borderRadius,
                        boxShadow: [
                          BoxShadow(
                            color: baseColor.withValues(alpha: 0.20),
                            blurRadius: _isHovered ? 16 : 12,
                            offset: const Offset(0, 8),
                          ),
                        ],
                      ),
                      child: Center(
                        child: Padding(
                          padding: const EdgeInsets.symmetric(
                            horizontal: 16,
                            vertical: 12,
                          ),
                          child: Text(
                            widget.data.text,
                            textAlign: TextAlign.center,
                            maxLines: 3,
                            overflow: TextOverflow.ellipsis,
                            style: TextStyle(
                              color: _BoardPalette.ink,
                              fontWeight: FontWeight.w700,
                              fontSize: math
                                  .max(
                                    14,
                                    math.min(
                                          widget.data.width,
                                          widget.data.height,
                                        ) *
                                        0.125,
                                  )
                                  .clamp(14.0, 22.0)
                                  .toDouble(),
                              height: 1.18,
                            ),
                          ),
                        ),
                      ),
                    ),
                  ),
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
    final overshootMagnitude = (targetScale - fromScale).abs() * 0.11 + 0.012;
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

    if (t <= 0.55) {
      return 1;
    }

    final local = Curves.easeInOutCubicEmphasized.transform((t - 0.55) / 0.45);
    return lerpDouble(1.0, 0.0, local)!;
  }

  double _computeDeleteScale(double t) {
    if (!_deleteController.isAnimating && !widget.data.isDeleting) {
      return 1;
    }

    if (t <= 0.36) {
      final local = Curves.easeOutCubic.transform(t / 0.36);
      return lerpDouble(1.0, 1.09, local)!;
    }

    final local = Curves.easeInOutCubicEmphasized.transform((t - 0.36) / 0.64);
    return lerpDouble(1.09, 0.68, local)!;
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
        color: const Color(0x326D727A),
        verticalPositions: buildPositions(5000),
        horizontalPositions: buildPositions(5000),
      ),
      _GridLayer(
        color: const Color(0x38636870),
        verticalPositions: buildPositions(5000),
        horizontalPositions: buildPositions(5000),
      ),
      _GridLayer(
        color: const Color(0x426A7078),
        verticalPositions: buildPositions(5000),
        horizontalPositions: buildPositions(5000),
      ),
    ];
  }

  @override
  void paint(Canvas canvas, Size size) {
    for (int layerIndex = 0; layerIndex < _layers.length; layerIndex++) {
      final layer = _layers[layerIndex];
      final paint = Paint()
        ..color = layer.color
        ..strokeWidth = 0.95;

      for (final x in layer.verticalPositions) {
        if (x < 0 || x > size.width) continue;
        _drawBrokenVerticalLine(
          canvas,
          paint,
          x: x,
          height: size.height,
          seed: layerIndex * 100000 + x.round(),
        );
      }

      for (final y in layer.horizontalPositions) {
        if (y < 0 || y > size.height) continue;
        _drawBrokenHorizontalLine(
          canvas,
          paint,
          y: y,
          width: size.width,
          seed: layerIndex * 100000 + y.round() + 50000,
        );
      }
    }
  }

  void _drawBrokenVerticalLine(
    Canvas canvas,
    Paint paint, {
    required double x,
    required double height,
    required int seed,
  }) {
    final random = math.Random(seed);
    if (random.nextDouble() < 0.34) {
      canvas.drawLine(Offset(x, 0), Offset(x, height), paint);
      return;
    }

    double cursor = 0;
    while (cursor < height) {
      final segmentLength = 110 + random.nextInt(180).toDouble();
      final segmentEnd = math.min(height, cursor + segmentLength);
      canvas.drawLine(Offset(x, cursor), Offset(x, segmentEnd), paint);
      if (segmentEnd >= height) {
        break;
      }
      cursor = segmentEnd + 14 + random.nextInt(54).toDouble();
    }
  }

  void _drawBrokenHorizontalLine(
    Canvas canvas,
    Paint paint, {
    required double y,
    required double width,
    required int seed,
  }) {
    final random = math.Random(seed);
    if (random.nextDouble() < 0.34) {
      canvas.drawLine(Offset(0, y), Offset(width, y), paint);
      return;
    }

    double cursor = 0;
    while (cursor < width) {
      final segmentLength = 110 + random.nextInt(180).toDouble();
      final segmentEnd = math.min(width, cursor + segmentLength);
      canvas.drawLine(Offset(cursor, y), Offset(segmentEnd, y), paint);
      if (segmentEnd >= width) {
        break;
      }
      cursor = segmentEnd + 14 + random.nextInt(54).toDouble();
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

class _BoardPalette {
  static const Color appShell = Color(0xFFF4F3FF);
  static const Color boardBase = Color(0xFFFCFCFF);
  static const Color surface = Color(0xFFEFF4FF);
  static const Color accentSoft = Color(0xFFD8E4FF);
  static const Color accent = Color(0xFF7E93D6);
  static const Color ink = Color(0xFF435065);
  static const Color mutedInk = Color(0xFF71809A);
  static const Color shadow = Color(0x142B3470);
}

const List<Color> _debugObjectColors = [
  Color(0xFFFFC8D9),
  Color(0xFFC8E6FF),
  Color(0xFFD5CCFF),
  Color(0xFFFFDEB8),
  Color(0xFFCCF1D6),
  Color(0xFFFFF0A8),
];
