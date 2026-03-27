import 'dart:async';

import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:geolocator/geolocator.dart';
import 'package:permission_handler/permission_handler.dart' as permission;
import 'package:shared_preferences/shared_preferences.dart';

import 'src/theme/app_theme.dart';
import 'android_phone_number_hint.dart';
import 'browser_voice_bridge.dart';
import 'meetup_screen.dart';
import 'voice_lab_screen.dart';
import 'weather_screen.dart';
import 'whitespace_app.dart' hide AgentBoardScreen;
import 'whitespace_app.dart' as whitespace show AgentBoardScreen;

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();
  await SystemChrome.setPreferredOrientations(const [
    DeviceOrientation.portraitUp,
  ]);
  runApp(const HelloAgainApp());
}

class HelloAgainApp extends StatelessWidget {
  const HelloAgainApp({super.key});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'Hello Again',
      debugShowCheckedModeBanner: false,
      theme: buildHelloAgainTheme(
        scaffoldBackgroundColor: const Color(0xFFF4EDE3),
        seedColor: const Color(0xFFB56B4D),
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

  static const _tokenKey = 'hello_again.account_token';
  static const _onboardingSessionKey = 'hello_again.onboarding_session_id';

  late final AgentBackendClient _backendClient;
  late final BrowserVoiceBridge _voiceBridge;

  SharedPreferences? _prefs;
  HelloAgainStage _stage = HelloAgainStage.booting;
  AppAccountSession? _session;
  bool _showContinue = false;
  bool _isListening = false;
  bool _isWorking = false;
  bool _isConfirming = false;
  String _statusText = 'Подготвяме Hello Again...';
  String _assistantReply = '';
  String _transcriptPreview = '';
  String _conversationMode = 'collecting';
  String _onboardingSessionId = '';
  String _draftPhoneNumber = '';
  String _recognizedPhone = '';

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
    final storedToken = prefs.getString(_tokenKey) ?? '';
    final storedOnboardingSession = prefs.getString(_onboardingSessionKey) ?? '';

    AppAccountSession? session;
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
      _onboardingSessionId = storedOnboardingSession;
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
      _statusText = 'Натиснете „Продължи“ и ще започнем разговора.';
    });
  }

  Future<void> _startOnboarding() async {
    if (_isWorking) return;

    final phonePermissionGranted = await _ensurePhonePermissionForSetup();
    if (!phonePermissionGranted) {
      if (!mounted) return;
      setState(() {
        _showContinue = true;
        _statusText =
            'Нужен е достъп до телефонния номер, за да продължим настройката.';
      });
      return;
    }

    setState(() {
      _showContinue = false;
      _stage = HelloAgainStage.onboarding;
      _assistantReply = '';
      _transcriptPreview = '';
      _conversationMode = 'collecting';
      _draftPhoneNumber = '';
      _recognizedPhone = '';
      _statusText = 'Подготвям разговора.';
      _isConfirming = false;
    });
    await _beginOrResumeOnboarding();
  }

  Future<bool> _ensurePhonePermissionForSetup() async {
    if (kIsWeb || defaultTargetPlatform != TargetPlatform.android) {
      return true;
    }

    final current = await permission.Permission.phone.status;
    if (current.isGranted || current.isLimited) {
      return true;
    }

    final requested = await permission.Permission.phone.request();
    if (requested.isGranted || requested.isLimited) {
      return true;
    }

    if (requested.isPermanentlyDenied && mounted) {
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(
          content: const Text(
            'Достъпът до телефонния номер е изключен. Разрешете го от настройките.',
          ),
          action: SnackBarAction(
            label: 'Настройки',
            onPressed: permission.openAppSettings,
          ),
        ),
      );
    }

    return false;
  }

  bool get _shouldUseAndroidPhoneHint =>
      AndroidPhoneNumberHint.isSupported &&
      _stage == HelloAgainStage.onboarding &&
      _conversationMode == 'collecting' &&
      _draftPhoneNumber.trim().isEmpty;

  Future<void> _beginOrResumeOnboarding() async {
    if (!mounted) return;
    setState(() {
      _isWorking = true;
      _isListening = false;
      _isConfirming = false;
      _statusText = 'Подготвям разговора.';
    });

    try {
      final payload = await _backendClient.startOnboarding(
        sessionId: _onboardingSessionId.isEmpty ? null : _onboardingSessionId,
      );
      await _handleOnboardingPayload(payload, autoContinue: true);
    } catch (error) {
      if (!mounted) return;
      setState(() {
        _isWorking = false;
        _statusText =
            'Не успях да започна разговора. Натиснете веднъж и ще опитам пак. ${error.toString()}';
      });
    }
  }

  Future<void> _captureNextOnboardingTurn() async {
    if (_shouldUseAndroidPhoneHint) {
      await _requestAndSubmitAndroidPhoneNumber();
      return;
    }

    if (_conversationMode == 'login_confirmation') {
      await _handleLoginConfirmation();
      return;
    }

    if (_isWorking && !_isConfirming) return;
    if (!mounted) return;

    setState(() {
      _isWorking = true;
      _isListening = true;
      _isConfirming = false;
      _statusText = 'Слушам Ви внимателно...';
    });

    try {
      final capturedTurn = await _voiceBridge.captureAudioTurn(language: 'bg-BG');
      final transcript = await _resolveCapturedTranscript(capturedTurn);
      final cleanTranscript = transcript.trim();

      if (cleanTranscript.isEmpty) {
        throw StateError('Не беше разпозната реч.');
      }

      if (!mounted) return;
      setState(() {
        _transcriptPreview = cleanTranscript;
        _isListening = false;
      });

      final payload = await _backendClient.sendOnboardingTurn(
        sessionId: _onboardingSessionId,
        message: cleanTranscript,
      );
      await _handleOnboardingPayload(payload, autoContinue: true);
    } catch (error) {
      if (!mounted) return;
      setState(() {
        _isListening = false;
        _isWorking = false;
        _statusText =
            'Не успях да чуя ясно. Натиснете веднъж и ще опитам отново.';
      });
    }
  }

  Future<void> _handleLoginConfirmation() async {
    final phoneConfirmed = await _askYesNo(
      _recognizedPhone.isEmpty
          ? 'Правилно ли разбрах, че вече имате профил?'
          : 'Чух номер $_recognizedPhone. Правилен ли е?',
    );
    if (!mounted) return;

    if (!phoneConfirmed) {
      final payload = await _backendClient.confirmOnboardingLogin(
        sessionId: _onboardingSessionId,
        phoneConfirmed: false,
        loginConfirmed: false,
      );
      await _handleOnboardingPayload(payload, autoContinue: true);
      return;
    }

    final loginConfirmed = await _askYesNo(
      'Искате ли да влезете в съществуващия си профил?',
    );
    if (!mounted) return;

    final payload = await _backendClient.confirmOnboardingLogin(
      sessionId: _onboardingSessionId,
      phoneConfirmed: true,
      loginConfirmed: loginConfirmed,
    );
    await _handleOnboardingPayload(payload, autoContinue: true);
  }

  Future<void> _requestAndSubmitAndroidPhoneNumber() async {
    if (!_shouldUseAndroidPhoneHint) {
      return;
    }

    if (!mounted) return;
    setState(() {
      _isWorking = true;
      _isListening = false;
      _isConfirming = false;
      _statusText = 'Изберете телефонния си номер от устройството.';
    });

    await _speakOnboardingText(
      'За да продължим, изберете телефонния си номер от телефона.',
    );

    try {
      final phoneNumber = await AndroidPhoneNumberHint.requestPhoneNumberHint();
      final cleanPhoneNumber = (phoneNumber ?? '').trim();

      if (cleanPhoneNumber.isEmpty) {
        if (!mounted) return;
        setState(() {
          _isWorking = false;
          _isListening = false;
          _isConfirming = false;
          _assistantReply =
              'За да влезете или да създадете профил, е нужно да изберете телефонния си номер от устройството.';
          _statusText =
              'Не мога да продължа без телефонен номер от устройството. Натиснете веднъж и ще опитам пак.';
        });
        return;
      }

      if (!mounted) return;
      setState(() {
        _transcriptPreview = cleanPhoneNumber;
        _statusText = 'Получих телефонния номер. Продължавам.';
      });

      final payload = await _backendClient.sendOnboardingTurn(
        sessionId: _onboardingSessionId,
        message: 'Телефонният ми номер е $cleanPhoneNumber.',
      );
      await _handleOnboardingPayload(payload, autoContinue: true);
    } catch (_) {
      if (!mounted) return;
      setState(() {
        _isWorking = false;
        _isListening = false;
        _isConfirming = false;
        _assistantReply =
            'Не успях да взема телефонния номер от Android.';
        _statusText =
            'Android не успя да покаже избора за телефонен номер. Натиснете веднъж и ще опитам пак.';
      });
    }
  }

  Future<bool> _askYesNo(String prompt) async {
    await _speakOnboardingText(prompt);

    while (mounted) {
      setState(() {
        _isListening = true;
        _isConfirming = true;
        _statusText = 'Моля, кажете само „да“ или „не“.';
      });

      final capturedTurn = await _voiceBridge.captureAudioTurn(language: 'bg-BG');
      final confirmation = await _resolveCapturedTranscript(capturedTurn);
      final normalized = _normalizeConfirmationAnswer(confirmation);

      if (normalized != null) {
        if (!mounted) return normalized;
        setState(() {
          _isListening = false;
          _transcriptPreview = confirmation.trim();
        });
        return normalized;
      }

      if (!mounted) return false;
      setState(() {
        _isListening = false;
        _statusText = 'Не разбрах потвърждението. Ще попитам отново.';
      });
      await _speakOnboardingText('Не разбрах. Моля, кажете само да или не.');
    }

    return false;
  }

  Future<void> _handleOnboardingPayload(
    Map<String, dynamic> payload, {
    required bool autoContinue,
  }) async {
    final draft = Map<String, dynamic>.from(payload['draft'] as Map? ?? const {});
    final assistantReply = (payload['assistant_reply'] ?? '').toString().trim();
    final mode = (payload['mode'] ?? 'collecting').toString().trim();
    final recognizedPhone = (payload['recognized_phone'] ?? '').toString().trim();
    final token = (payload['token'] ?? '').toString().trim();
    final profile = Map<String, dynamic>.from(payload['profile'] as Map? ?? const {});
    final sessionId = (draft['session_id'] ?? '').toString().trim();

    if (sessionId.isNotEmpty) {
      _onboardingSessionId = sessionId;
      await _prefs?.setString(_onboardingSessionKey, sessionId);
    }

    if (!mounted) return;
    setState(() {
      _assistantReply = assistantReply;
      _conversationMode = mode.isEmpty ? 'collecting' : mode;
      _draftPhoneNumber = (draft['phone_number'] ?? '').toString().trim();
      _recognizedPhone = recognizedPhone;
      _isWorking = false;
      _isListening = false;
      _isConfirming = mode == 'login_confirmation';
      _statusText = assistantReply.isEmpty
          ? 'Продължаваме разговора.'
          : assistantReply;
    });

    if (assistantReply.isNotEmpty) {
      await _speakOnboardingText(assistantReply);
    }

    if (token.isNotEmpty) {
      final displayName = (profile['display_name'] ?? profile['name'] ?? 'Приятел')
          .toString();
      final userId = int.tryParse((profile['user_id'] ?? '0').toString()) ?? 0;
      await _prefs?.setString(_tokenKey, token);
      await _prefs?.remove(_onboardingSessionKey);
      if (!mounted) return;
      setState(() {
        _session = AppAccountSession(
          token: token,
          userId: userId,
          displayName: displayName,
        );
        _stage = HelloAgainStage.board;
      });
      return;
    }

    if (_conversationMode == 'ready_to_register') {
      await _completeOnboardingRegistration();
      return;
    }

    if (autoContinue && _conversationMode != 'completed') {
      await _captureNextOnboardingTurn();
    }
  }

  Future<void> _completeOnboardingRegistration() async {
    if (!mounted) return;
    setState(() {
      _isWorking = true;
      _isListening = false;
      _isConfirming = false;
      _statusText = 'Създавам Вашия профил...';
    });

    try {
      final payload = await _backendClient.completeOnboarding(
        sessionId: _onboardingSessionId,
      );
      await _handleOnboardingPayload(payload, autoContinue: false);
    } catch (error) {
      if (!mounted) return;
      setState(() {
        _isWorking = false;
        _statusText =
            'Регистрацията не можа да завърши. Натиснете веднъж и ще опитам пак. ${error.toString()}';
      });
    }
  }

  Future<String> _resolveCapturedTranscript(CapturedAudioTurn capturedTurn) async {
    final directTranscript = (capturedTurn.transcript ?? '').trim();
    if (directTranscript.isNotEmpty) {
      return directTranscript;
    }
    final payload = await _backendClient.transcribeSpeechTurn(
      audioBase64: capturedTurn.audioBase64,
      audioMimeType: capturedTurn.mimeType,
      userId: 'hello_again_onboarding',
      sessionId: _onboardingSessionId.isEmpty
          ? 'onboarding_${DateTime.now().millisecondsSinceEpoch}'
          : _onboardingSessionId,
      language: capturedTurn.language,
    );
    return (payload['transcript'] ?? payload['message'] ?? '').toString().trim();
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
    const noWords = {
      'не',
      'no',
      'wrong',
      'грешно',
      'повтори',
      'отново',
    };

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
      final payload = await _backendClient.speakText(text: text, language: 'bg-BG');
      final audioBase64 = (payload['audio_base64'] ?? '').toString().trim();
      final mimeType =
          (payload['audio_mime_type'] ?? 'audio/wav').toString().trim();
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

  @override
  Widget build(BuildContext context) {
    switch (_stage) {
      case HelloAgainStage.booting:
        return const Scaffold(
          body: Center(child: CircularProgressIndicator()),
        );
      case HelloAgainStage.intro:
        return IntroOnboardingScreen(
          showContinue: _showContinue,
          statusText: _statusText,
          onFinished: _handleIntroFinished,
          onContinue: _startOnboarding,
        );
      case HelloAgainStage.onboarding:
        return RegistrationScreen(
          assistantReply: _assistantReply,
          statusText: _statusText,
          transcript: _transcriptPreview,
          isListening: _isListening,
          isWorking: _isWorking,
          isConfirming: _isConfirming,
          conversationMode: _conversationMode,
          retryLabel:
              _shouldUseAndroidPhoneHint ? 'Избери номера' : 'Повтори разговора',
          onRetry: _captureNextOnboardingTurn,
        );
      case HelloAgainStage.board:
        final session = _session;
        return AgentBoardScreen(
          userId: session?.userId.toString() ?? 'hello_again_frontend',
          accountToken: session?.token,
          welcomeText: session == null
              ? null
              : 'Добре дошли, ${session.displayName}. Вашето място е готово.',
        );
    }
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
  int _selectedIndex = 0;

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      body: IndexedStack(
        index: _selectedIndex,
        children: [
          whitespace.AgentBoardScreen(
            userId: widget.userId,
            accountToken: widget.accountToken,
            welcomeText: widget.welcomeText,
          ),
          const VoiceLabScreen(),
          const _LocationGate(
            title: 'Weather',
            message:
                'Location access is needed to show the local weather forecast.',
            childBuilder: _weatherBuilder,
          ),
          _LocationGate(
            title: 'Meetup',
            message:
                'Location access is needed to suggest a good meetup place near you.',
            childBuilder: (position) => _meetupBuilder(
              position,
              accountToken: widget.accountToken,
            ),
          ),
        ],
      ),
      bottomNavigationBar: NavigationBar(
        selectedIndex: _selectedIndex,
        onDestinationSelected: (value) {
          setState(() {
            _selectedIndex = value;
          });
        },
        backgroundColor: const Color(0xFF162040),
        indicatorColor: const Color(0xFF3B82F6).withValues(alpha: 0.25),
        labelBehavior: NavigationDestinationLabelBehavior.alwaysShow,
        destinations: const [
          NavigationDestination(
            icon: Icon(Icons.dashboard_outlined),
            selectedIcon: Icon(Icons.dashboard),
            label: 'Space',
          ),
          NavigationDestination(
            icon: Icon(Icons.mic_none_rounded),
            selectedIcon: Icon(Icons.mic_rounded),
            label: 'Voice',
          ),
          NavigationDestination(
            icon: Icon(Icons.wb_sunny_outlined),
            selectedIcon: Icon(Icons.wb_sunny),
            label: 'Weather',
          ),
          NavigationDestination(
            icon: Icon(Icons.place_outlined),
            selectedIcon: Icon(Icons.place),
            label: 'Meetup',
          ),
        ],
      ),
    );
  }
}

class _LocationGate extends StatefulWidget {
  const _LocationGate({
    required this.title,
    required this.message,
    required this.childBuilder,
  });

  final String title;
  final String message;
  final Widget Function(Position position) childBuilder;

  @override
  State<_LocationGate> createState() => _LocationGateState();
}

class _LocationGateState extends State<_LocationGate> {
  Future<Position>? _positionFuture;

  @override
  void initState() {
    super.initState();
    _positionFuture = _loadPosition();
  }

  Future<Position> _loadPosition() async {
    final serviceEnabled = await Geolocator.isLocationServiceEnabled();
    if (!serviceEnabled) {
      throw StateError('Location services are turned off.');
    }

    var permission = await Geolocator.checkPermission();
    if (permission == LocationPermission.denied) {
      permission = await Geolocator.requestPermission();
    }

    if (permission == LocationPermission.denied ||
        permission == LocationPermission.deniedForever) {
      throw StateError('Location permission was not granted.');
    }

    return Geolocator.getCurrentPosition();
  }

  void _retry() {
    setState(() {
      _positionFuture = _loadPosition();
    });
  }

  @override
  Widget build(BuildContext context) {
    return FutureBuilder<Position>(
      future: _positionFuture,
      builder: (context, snapshot) {
        if (snapshot.connectionState != ConnectionState.done) {
          return Scaffold(
            appBar: AppBar(title: Text(widget.title)),
            body: const Center(child: CircularProgressIndicator()),
          );
        }

        if (snapshot.hasData) {
          return widget.childBuilder(snapshot.data!);
        }

        return Scaffold(
          appBar: AppBar(title: Text(widget.title)),
          body: Center(
            child: Padding(
              padding: const EdgeInsets.all(24),
              child: Column(
                mainAxisSize: MainAxisSize.min,
                children: [
                  const Icon(Icons.location_on_outlined, size: 48),
                  const SizedBox(height: 16),
                  Text(
                    widget.message,
                    textAlign: TextAlign.center,
                    style: const TextStyle(fontSize: 18, height: 1.4),
                  ),
                  const SizedBox(height: 12),
                  Text(
                    snapshot.error?.toString() ?? 'Location is unavailable.',
                    textAlign: TextAlign.center,
                    style: const TextStyle(color: Colors.black54),
                  ),
                  const SizedBox(height: 20),
                  FilledButton(
                    onPressed: _retry,
                    child: const Text('Try again'),
                  ),
                ],
              ),
            ),
          ),
        );
      },
    );
  }
}

Widget _weatherBuilder(Position position) =>
    WeatherScreen(userPosition: position);

Widget _meetupBuilder(
  Position position, {
  String? accountToken,
}) =>
    MeetupScreen(userPosition: position, accountToken: accountToken);

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
    required this.assistantReply,
    required this.statusText,
    required this.transcript,
    required this.isListening,
    required this.isWorking,
    required this.isConfirming,
    required this.conversationMode,
    required this.retryLabel,
    required this.onRetry,
  });

  final String assistantReply;
  final String statusText;
  final String transcript;
  final bool isListening;
  final bool isWorking;
  final bool isConfirming;
  final String conversationMode;
  final String retryLabel;
  final Future<void> Function() onRetry;

  @override
  Widget build(BuildContext context) {
    final indicatorColor = isListening
        ? const Color(0xFFB56B4D)
        : isConfirming
        ? const Color(0xFF7A8B67)
        : const Color(0xFFC8B6A1);

    final statusLabel = isListening
        ? 'Слушам'
        : isConfirming
        ? 'Чакам потвърждение'
        : conversationMode == 'ready_to_register'
        ? 'Подготвям профила'
        : 'Разговор';

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
                        statusLabel,
                        style: const TextStyle(
                          fontSize: 13,
                          fontWeight: FontWeight.w700,
                          color: Color(0xFF8E725F),
                          letterSpacing: 0.2,
                        ),
                      ),
                      const SizedBox(height: 18),
                      Text(
                        assistantReply.isEmpty
                            ? 'Кажете ми нещо за себе си, както Ви е удобно.'
                            : assistantReply,
                        style: const TextStyle(
                          fontSize: 28,
                          height: 1.22,
                          fontWeight: FontWeight.w700,
                          color: Color(0xFF2F241D),
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
                                statusLabel,
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
                              ? 'Когато сте готови, говорете спокойно. Аз ще продължа разговора.'
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
                    onPressed: isListening ? null : onRetry,
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
                      isWorking && !isConfirming ? 'Изчакайте...' : 'Повтори разговора',
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
          child: _BackdropOrb(
            diameter: 180,
            color: const Color(0xFFE8D7C6),
          ),
        ),
        Positioned(
          top: 120,
          left: -46,
          child: _BackdropOrb(
            diameter: 128,
            color: const Color(0xFFE3D4C3),
          ),
        ),
        Positioned(
          bottom: -44,
          right: 18,
          child: _BackdropOrb(
            diameter: 168,
            color: const Color(0xFFDDCBB8),
          ),
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
  const _BackdropOrb({
    required this.diameter,
    required this.color,
  });

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
