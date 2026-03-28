import 'dart:async';
import 'dart:convert';
import 'dart:math' as math;
import 'dart:ui' show lerpDouble;

import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';
import 'package:flutter_dotenv/flutter_dotenv.dart';
import 'package:flutter/services.dart';
import 'package:http/http.dart' as http;
import 'package:shared_preferences/shared_preferences.dart';

import 'browser_voice_bridge.dart';
import 'src/config/backend_base_url.dart';
import 'src/screens/navigation_launcher_screen.dart';
import 'src/theme/app_theme.dart';

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();
  try {
    await dotenv.load(fileName: '.env');
  } catch (_) {
    // Keep startup resilient when the optional env file is not present yet.
  }
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
      theme: buildHelloAgainTheme(
        scaffoldBackgroundColor: const Color(0xFFF5EFE6),
        seedColor: const Color(0xFFBB5A3C),
        surfaceColor: const Color(0xFFFFFBF7),
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
    this.phoneNumber = '',
  });

  final String token;
  final int userId;
  final String displayName;
  final String phoneNumber;

  AppAccountSession copyWith({
    String? token,
    int? userId,
    String? displayName,
    String? phoneNumber,
  }) {
    return AppAccountSession(
      token: token ?? this.token,
      userId: userId ?? this.userId,
      displayName: displayName ?? this.displayName,
      phoneNumber: phoneNumber ?? this.phoneNumber,
    );
  }
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

  @override
  void initState() {
    super.initState();
    _sceneController = SceneController();
    _backendClient = AgentBackendClient();
    _voiceBridge = createBrowserVoiceBridge();
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
    _voiceBridge.stopRecognition();
    _voiceBridge.stopAudio();
    _sceneController.dispose();
    _promptController.dispose();
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

  Future<void> _openNavigationApiTestPage() async {
    if (_isBusy || _isListening) return;

    final prompt = _promptController.text.trim();
    if (prompt.isEmpty) {
      setState(() {
        _statusText = 'Write a prompt first, then I will open the phone command page and run it immediately.';
      });
      return;
    }

    setState(() {
      _statusText = 'Opening phone command with "$prompt"...';
    });

    if (!mounted) return;
    await Navigator.of(context).push(
      MaterialPageRoute(
        builder: (_) => NavigationLauncherScreen(
          initialPrompt: prompt,
          autoRunOnOpen: true,
        ),
      ),
    );

    if (!mounted) return;
    setState(() {
      _statusText = 'Returned from the navigation run page.';
    });
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
                final boardSize = Size(
                  constraints.maxWidth,
                  constraints.maxHeight,
                );
                _sceneController.setBoardSize(boardSize);

                return Stack(
                  children: [
                    Positioned.fill(
                      child: DecoratedBox(
                        decoration: const BoxDecoration(
                          gradient: LinearGradient(
                            begin: Alignment.topCenter,
                            end: Alignment.bottomCenter,
                            colors: [
                              Color(0xFFF9F3EB),
                              Color(0xFFF0E3D5),
                              Color(0xFFE6D3C3),
                            ],
                          ),
                        ),
                        child: Stack(
                          children: [
                            Positioned.fill(
                              child: DecoratedBox(
                                decoration: BoxDecoration(
                                  color: Colors.white.withValues(alpha: 0.48),
                                  borderRadius: BorderRadius.circular(0),
                                ),
                                child: CustomPaint(painter: GridPainter()),
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
                      top: topInset,
                      left: horizontalPadding,
                      right: isCompact ? horizontalPadding : null,
                      child: AgentResponseCard(
                        speech: _lastSpeech,
                        status: _statusText,
                        isBusy: _isBusy,
                        compact: isCompact,
                      ),
                    ),
                    Positioned(
                      left: horizontalPadding,
                      right: horizontalPadding,
                      bottom: bottomInset,
                      child: Column(
                        mainAxisSize: MainAxisSize.min,
                        children: [
                          Container(
                            decoration: BoxDecoration(
                              color: Colors.white.withValues(alpha: 0.84),
                              borderRadius: BorderRadius.circular(22),
                              border: Border.all(
                                color: Colors.black.withValues(alpha: 0.08),
                                width: 0.8,
                              ),
                              boxShadow: [
                                BoxShadow(
                                  color: Colors.black.withValues(alpha: 0.08),
                                  blurRadius: 24,
                                  offset: const Offset(0, 14),
                                ),
                              ],
                            ),
                            child: TextField(
                              enabled: !_isBusy && !_isListening,
                              controller: _promptController,
                              onSubmitted: (_) => _sendPrompt(),
                              minLines: isCompact ? 1 : 1,
                              maxLines: isCompact ? 3 : 2,
                              decoration: InputDecoration(
                                hintText: _isListening
                                    ? (kIsWeb
                                          ? 'Listening in the browser...'
                                          : 'Listening on your phone...')
                                    : _voiceLoopEnabled
                                    ? 'Voice mode is on. Speak your next request...'
                                    : 'Write a prompt or use the mic...',
                                hintStyle: TextStyle(
                                  color: Colors.black.withValues(alpha: 0.44),
                                ),
                                border: InputBorder.none,
                                contentPadding: const EdgeInsets.symmetric(
                                  horizontal: 16,
                                  vertical: 16,
                                ),
                              ),
                            ),
                          ),
                          const SizedBox(height: 12),
                          Row(
                            children: [
                              Expanded(
                                child: GestureDetector(
                                  onTap: (_isBusy && !_voiceLoopEnabled)
                                      ? null
                                      : _toggleVoiceLoop,
                                  child: Container(
                                    height: 54,
                                    decoration: BoxDecoration(
                                      color: (_isListening || _voiceLoopEnabled)
                                          ? const Color(0xFFD2604A)
                                          : const Color(0xFFB85A40),
                                      borderRadius: BorderRadius.circular(20),
                                      boxShadow: [
                                        BoxShadow(
                                          color: const Color(
                                            0xFFB85A40,
                                          ).withValues(alpha: 0.26),
                                          blurRadius: 18,
                                          offset: const Offset(0, 10),
                                        ),
                                      ],
                                    ),
                                    child: Row(
                                      mainAxisAlignment:
                                          MainAxisAlignment.center,
                                      children: [
                                        Icon(
                                          (_isListening || _voiceLoopEnabled)
                                              ? Icons.hearing
                                              : Icons.mic_none,
                                          color: Colors.white,
                                        ),
                                        const SizedBox(width: 10),
                                        Text(
                                          _voiceLoopEnabled
                                              ? 'Voice On'
                                              : 'Voice',
                                          style: const TextStyle(
                                            color: Colors.white,
                                            fontSize: 15,
                                            fontWeight: FontWeight.w700,
                                          ),
                                        ),
                                      ],
                                    ),
                                  ),
                                ),
                              ),
                              const SizedBox(width: 12),
                              Expanded(
                                child: GestureDetector(
                                  onTap: (_isBusy || _isListening)
                                      ? null
                                      : _sendPrompt,
                                  child: Container(
                                    height: 54,
                                    decoration: BoxDecoration(
                                      color: Colors.white.withValues(
                                        alpha: 0.88,
                                      ),
                                      borderRadius: BorderRadius.circular(20),
                                      border: Border.all(
                                        color: Colors.black.withValues(
                                          alpha: 0.08,
                                        ),
                                        width: 0.8,
                                      ),
                                    ),
                                    child: Center(
                                      child: Text(
                                        _isBusy
                                            ? 'Running'
                                            : _isListening
                                            ? 'Listening'
                                            : 'Send Prompt',
                                        style: TextStyle(
                                          color: Colors.black.withValues(
                                            alpha: 0.74,
                                          ),
                                          fontSize: 14,
                                          fontWeight: FontWeight.w700,
                                        ),
                                      ),
                                    ),
                                  ),
                                ),
                              ),
                            ],
                          ),
                          const SizedBox(height: 10),
                          GestureDetector(
                            onTap: (_isBusy || _isListening)
                                ? null
                                : _openNavigationApiTestPage,
                            child: Container(
                              height: 50,
                              decoration: BoxDecoration(
                                color: const Color(0xFF1F7A5A),
                                borderRadius: BorderRadius.circular(18),
                                boxShadow: [
                                  BoxShadow(
                                    color: const Color(0xFF1F7A5A).withValues(alpha: 0.24),
                                    blurRadius: 18,
                                    offset: const Offset(0, 10),
                                  ),
                                ],
                              ),
                              child: const Row(
                                mainAxisAlignment: MainAxisAlignment.center,
                                children: [
                                  Icon(
                                    Icons.map_outlined,
                                    color: Colors.white,
                                  ),
                                  SizedBox(width: 10),
                                  Text(
                                    'Run Phone Command',
                                    style: TextStyle(
                                      color: Colors.white,
                                      fontSize: 14,
                                      fontWeight: FontWeight.w700,
                                    ),
                                  ),
                                ],
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
    required this.compact,
  });

  final String speech;
  final String status;
  final bool isBusy;
  final bool compact;

  @override
  Widget build(BuildContext context) {
    return ConstrainedBox(
      constraints: BoxConstraints(maxWidth: compact ? double.infinity : 360),
      child: Container(
        padding: const EdgeInsets.all(16),
        decoration: BoxDecoration(
          color: Colors.white.withValues(alpha: 0.88),
          borderRadius: BorderRadius.circular(24),
          border: Border.all(
            color: Colors.black.withValues(alpha: 0.08),
            width: 0.8,
          ),
          boxShadow: [
            BoxShadow(
              color: Colors.black.withValues(alpha: 0.08),
              blurRadius: 24,
              offset: const Offset(0, 12),
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
  static const _requestTimeout = Duration(seconds: 20);

  AgentBackendClient({String? baseUrl})
    : _baseUri = Uri.parse(baseUrl ?? _resolveDefaultBaseUrl());

  final Uri _baseUri;

  static String _resolveDefaultBaseUrl() {
    const configuredBaseUrl = String.fromEnvironment('BACKEND_BASE_URL');
    if (configuredBaseUrl.isNotEmpty) {
      return configuredBaseUrl;
    }
    return resolveBackendBaseUrl();
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
      phoneNumber: (profile['phone_number'] ?? '').toString().trim(),
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
      phoneNumber: (profile['phone_number'] ?? phoneNumber).toString().trim(),
    );
  }

  Future<Map<String, dynamic>> startOnboarding({String? sessionId}) {
    return _postJson('/api/accounts/onboarding/start/', {
      if ((sessionId ?? '').trim().isNotEmpty) 'session_id': sessionId!.trim(),
    });
  }

  Future<Map<String, dynamic>> sendOnboardingTurn({
    required String sessionId,
    required String message,
  }) {
    return _postJson('/api/accounts/onboarding/turn/', {
      'session_id': sessionId,
      'message': message,
    });
  }

  Future<Map<String, dynamic>> confirmOnboardingLogin({
    required String sessionId,
    required bool phoneConfirmed,
    required bool loginConfirmed,
  }) {
    return _postJson('/api/accounts/onboarding/confirm-login/', {
      'session_id': sessionId,
      'phone_confirmed': phoneConfirmed,
      'login_confirmed': loginConfirmed,
    });
  }

  Future<Map<String, dynamic>> completeOnboarding({required String sessionId}) {
    return _postJson('/api/accounts/onboarding/complete/', {
      'session_id': sessionId,
      'microphone_permission_granted': true,
      'phone_permission_granted': true,
    });
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

  Future<Map<String, dynamic>> _getJson(String path, {String? token}) async {
    final response = await _sendWithTimeout(
      () => http.get(_baseUri.resolve(path), headers: _headers(token: token)),
      'GET',
      path,
    );
    if (response.statusCode < 200 || response.statusCode >= 300) {
      throw Exception(
        'GET $path failed with ${response.statusCode}: ${utf8.decode(response.bodyBytes)}',
      );
    }
    return _decodeJson(utf8.decode(response.bodyBytes));
  }

  Future<Map<String, dynamic>> _postJson(
    String path,
    Map<String, dynamic> payload, {
    String? token,
  }) async {
    final response = await _sendWithTimeout(
      () => http.post(
        _baseUri.resolve(path),
        headers: _headers(token: token),
        body: jsonEncode(payload),
      ),
      'POST',
      path,
    );
    if (response.statusCode < 200 || response.statusCode >= 300) {
      throw Exception(
        'POST $path failed with ${response.statusCode}: ${utf8.decode(response.bodyBytes)}',
      );
    }
    return _decodeJson(utf8.decode(response.bodyBytes));
  }

  Future<http.Response> _sendWithTimeout(
    Future<http.Response> Function() request,
    String method,
    String path,
  ) async {
    try {
      return await request().timeout(_requestTimeout);
    } on TimeoutException {
      throw Exception(
        '$method $path timed out after ${_requestTimeout.inSeconds}s while contacting ${_baseUri.origin}.',
      );
    }
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
