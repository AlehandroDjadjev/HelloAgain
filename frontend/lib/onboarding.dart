import 'dart:async';

import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';
import 'package:geolocator/geolocator.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:url_launcher/url_launcher.dart';

import 'browser_voice_bridge.dart';
import 'google_calendar_connect.dart';
import 'src/theme/app_theme.dart';
import 'whitespace_app.dart' show AgentBackendClient, AppAccountSession;
import 'whitespace_app.dart' as whitespace show AgentBoardScreen;

enum HelloAgainStage { booting, intro, onboarding, googleCalendar, board }

class HelloAgainShell extends StatefulWidget {
  const HelloAgainShell({super.key});

  @override
  State<HelloAgainShell> createState() => _HelloAgainShellState();
}

class _HelloAgainShellState extends State<HelloAgainShell>
    with WidgetsBindingObserver {
  static const _tokenKey = 'hello_again.account_token';
  static const _onboardingSessionKey = 'hello_again.onboarding_session_id';

  late final AgentBackendClient _backendClient;
  late final BrowserVoiceBridge _voiceBridge;

  SharedPreferences? _prefs;
  HelloAgainStage _stage = HelloAgainStage.booting;
  AppAccountSession? _session;
  bool _isListening = false;
  bool _isWorking = false;
  bool _isConfirming = false;
  bool _voiceReady = false;
  bool _hasStartedOnboarding = false;
  bool _conversationActivated = false;
  bool _hasCompletedIntroduction = false;
  bool _isGoogleCalendarWorking = false;
  bool _googleCalendarConnected = false;
  int _visualStep = 1;
  String _statusText = 'Preparing Hello Again...';
  String _conversationMode = 'collecting';
  String _onboardingSessionId = '';
  String _recognizedPhone = '';
  bool _hasCollectedOnboardingInput = false;
  String _pendingVoicePrompt = '';
  String _googleCalendarEmail = '';
  String _googleCalendarStatusText =
      'You can connect Google Calendar now or skip it.';
  Timer? _autoListenRetryTimer;
  Map<String, dynamic>? _registrationLocationPayload;
  AppAccountSession? _pendingGoogleCalendarSession;

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    _backendClient = AgentBackendClient();
    _voiceBridge = createBrowserVoiceBridge();
    unawaited(_bootstrap());
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    _autoListenRetryTimer?.cancel();
    _voiceBridge.stopRecognition();
    _voiceBridge.stopAudio();
    super.dispose();
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    if (state == AppLifecycleState.resumed &&
        _stage == HelloAgainStage.googleCalendar &&
        _pendingGoogleCalendarSession != null) {
      unawaited(_refreshGoogleCalendarStatus());
    }
  }

  Future<void> _bootstrap() async {
    final prefs = await SharedPreferences.getInstance();
    final storedToken = (prefs.getString(_tokenKey) ?? '').trim();
    final storedSession = (prefs.getString(_onboardingSessionKey) ?? '').trim();

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
      _onboardingSessionId = storedSession;
      _stage = session == null
          ? HelloAgainStage.onboarding
          : HelloAgainStage.board;
      _statusText = session == null
          ? 'Everything is ready for a calm start.'
          : 'Welcome back. Opening your space.';
    });
    unawaited(_ensureRegistrationLocation());
    if (session == null && !_hasStartedOnboarding) {
      _hasStartedOnboarding = true;
      unawaited(_startOnboarding());
    }
  }

  Future<void> _startOnboarding() async {
    if (_isWorking) return;
    _hasStartedOnboarding = true;
    setState(() {
      _stage = HelloAgainStage.onboarding;
      _visualStep = 1;
      _isListening = false;
      _isWorking = false;
      _isConfirming = false;
      _voiceReady = false;
      _conversationMode = 'collecting';
      _recognizedPhone = '';
      _hasCollectedOnboardingInput = false;
      _conversationActivated = false;
      _hasCompletedIntroduction = false;
      _pendingVoicePrompt = '';
      _statusText = 'Preparing your conversation.';
    });
    await _beginOrResumeOnboarding();
  }

  Future<void> _beginOrResumeOnboarding() async {
    if (!mounted) return;
    setState(() {
      _isWorking = true;
      _statusText = 'Preparing your conversation.';
    });
    try {
      final payload = await _backendClient.startOnboarding(
        sessionId: _onboardingSessionId.isEmpty ? null : _onboardingSessionId,
      );
      await _handleOnboardingPayload(payload, autoContinue: false);
    } catch (error) {
      if (!mounted) return;
      setState(() {
        _isWorking = false;
        _statusText = 'I could not start the conversation. ${error.toString()}';
      });
    }
  }

  void _scheduleAutoListenRetry([
    Duration delay = const Duration(milliseconds: 900),
  ]) {
    _autoListenRetryTimer?.cancel();
    if (!mounted ||
        _stage != HelloAgainStage.onboarding ||
        _isListening ||
        _isWorking ||
        _isConfirming ||
        !_conversationActivated) {
      return;
    }
    _autoListenRetryTimer = Timer(delay, () {
      if (!mounted ||
          _stage != HelloAgainStage.onboarding ||
          _isListening ||
          _isWorking ||
          _isConfirming ||
          !_conversationActivated) {
        return;
      }
      unawaited(_captureNextOnboardingTurn());
    });
  }

  void _scheduleLoginConfirmationRetry([
    Duration delay = const Duration(milliseconds: 700),
  ]) {
    _autoListenRetryTimer?.cancel();
    if (!mounted ||
        _stage != HelloAgainStage.onboarding ||
        _isListening ||
        _isWorking ||
        !_conversationActivated ||
        _conversationMode != 'login_confirmation') {
      return;
    }
    _autoListenRetryTimer = Timer(delay, () {
      if (!mounted ||
          _stage != HelloAgainStage.onboarding ||
          _isListening ||
          _isWorking ||
          !_conversationActivated ||
          _conversationMode != 'login_confirmation') {
        return;
      }
      unawaited(_handleLoginConfirmation());
    });
  }

  Future<void> _handlePrimaryAction() async {
    if (!mounted ||
        _stage != HelloAgainStage.onboarding ||
        _conversationActivated ||
        _isListening ||
        _isWorking) {
      return;
    }

    final ready = await _ensureVoiceReady(userInitiated: true);
    if (!ready || !mounted) {
      return;
    }

    setState(() {
      _conversationActivated = true;
    });

    await _captureNextOnboardingTurn();
  }

  Future<bool> _ensureVoiceReady({bool userInitiated = false}) async {
    if (_voiceReady) return true;
    if (!mounted) return false;
    setState(() {
      _isWorking = true;
      _statusText = 'Getting microphone and audio ready on this device...';
    });
    try {
      await _voiceBridge.primeVoiceExperience();
      if (!mounted) return false;
      setState(() {
        _voiceReady = true;
        _isWorking = false;
        _statusText = 'Voice is ready. I am listening.';
      });
      return true;
    } catch (_) {
      if (!mounted) return false;
      setState(() {
        _voiceReady = false;
        _isWorking = false;
        _statusText = userInitiated
            ? 'Voice could not start on this device yet. Microphone access is required.'
            : 'Microphone access is required so the app can listen and speak.';
      });
      return false;
    }
  }

  Future<void> _captureNextOnboardingTurn() async {
    if (!mounted ||
        _stage != HelloAgainStage.onboarding ||
        _isListening ||
        _isWorking) {
      return;
    }
    final ready = await _ensureVoiceReady();
    if (!ready) {
      if (!kIsWeb) _scheduleAutoListenRetry(const Duration(seconds: 2));
      return;
    }
    if (_pendingVoicePrompt.trim().isNotEmpty) {
      final prompt = _pendingVoicePrompt.trim();
      setState(() {
        _pendingVoicePrompt = '';
        _statusText = prompt;
      });
      await _speakOnboardingText(prompt);
      await Future<void>.delayed(const Duration(milliseconds: 260));
    }
    try {
      if (!mounted) return;
      setState(() {
        _isListening = true;
        _isWorking = false;
        _statusText = 'Listening carefully...';
      });
      final transcript = await _listenForOnboardingTranscript();
      if (!mounted) return;
      if (transcript.trim().isEmpty) {
        setState(() {
          _isListening = false;
          _statusText = 'I could not hear that clearly enough.';
        });
        _scheduleAutoListenRetry();
        return;
      }
      setState(() {
        _isListening = false;
        _isWorking = true;
        _statusText = 'Heard you. Processing...';
      });
      final payload = await _backendClient.sendOnboardingTurn(
        sessionId: _onboardingSessionId,
        message: transcript.trim(),
      );
      await _handleOnboardingPayload(
        payload,
        autoContinue: true,
        countAsProgress: true,
      );
    } catch (error) {
      if (!mounted) return;
      setState(() {
        _isListening = false;
        _isWorking = false;
        _statusText = _isRecoverableListeningError(error)
            ? 'Still listening. Say that again when you are ready.'
            : 'I could not hear that clearly enough.';
      });
      _scheduleAutoListenRetry();
    }
  }

  Future<void> _handleLoginConfirmation() async {
    if (!mounted ||
        _stage != HelloAgainStage.onboarding ||
        _isWorking ||
        _isListening) {
      return;
    }
    if (_pendingVoicePrompt.trim().isNotEmpty) {
      final prompt = _pendingVoicePrompt.trim();
      setState(() {
        _pendingVoicePrompt = '';
        _statusText = prompt;
      });
      await _speakOnboardingText(prompt);
    }
    setState(() => _isConfirming = true);
    try {
      final confirmationAccepted = await _askYesNo(
        _existingProfileConfirmationPrompt(),
      );
      if (confirmationAccepted == null) {
        if (mounted) {
          setState(() {
            _isConfirming = false;
            _statusText = 'Моля, отговорете с да или не.';
          });
        }
        _scheduleLoginConfirmationRetry();
        return;
      }
      if (!mounted) return;
      setState(() {
        _isWorking = true;
        _isListening = false;
        _statusText = 'Heard you. Processing...';
      });
      final payload = await _backendClient.confirmOnboardingLogin(
        sessionId: _onboardingSessionId,
        phoneConfirmed: confirmationAccepted,
        loginConfirmed: confirmationAccepted,
      );
      await _handleOnboardingPayload(
        payload,
        autoContinue: true,
        countAsProgress: true,
      );
    } finally {
      if (mounted) setState(() => _isConfirming = false);
    }
  }

  Future<bool?> _askYesNo(String prompt) async {
    if (!await _ensureVoiceReady()) return null;
    await _speakOnboardingText(prompt);
    await Future<void>.delayed(const Duration(milliseconds: 220));
    for (var attempt = 0; attempt < 2; attempt += 1) {
      try {
        if (!mounted) return null;
        setState(() {
          _isListening = true;
          _isWorking = false;
          _statusText = 'Listening carefully...';
        });
        final transcript = await _listenForOnboardingTranscript();
        final normalized = _normalizeBulgarianConfirmationAnswer(
          transcript.trim(),
        );
        if (!mounted) return null;
        setState(() {
          _isListening = false;
        });
        if (normalized != null) return normalized;
      } catch (_) {
        if (!mounted) return null;
        setState(() => _isListening = false);
      }
      await _speakOnboardingText('Моля, отговорете с да или не.');
    }
    return null;
  }

  String _existingProfileConfirmationPrompt() {
    final phone = _recognizedPhone.trim();
    if (phone.isEmpty) {
      return 'Открих съществуващ профил. Искате ли да влезете в него? Кажете да или не.';
    }
    return 'Открих съществуващ профил с номер $phone. Ако това е вашият номер и искате да влезете, кажете да. Иначе кажете не.';
  }

  Future<String> _listenForOnboardingTranscript() async {
    final capturedTurn = await _voiceBridge.captureAudioTurn(language: 'bg-BG');
    return _resolveCapturedTranscript(capturedTurn);
  }

  Future<void> _handleOnboardingPayload(
    Map<String, dynamic> payload, {
    required bool autoContinue,
    bool countAsProgress = false,
  }) async {
    final draft = Map<String, dynamic>.from(
      payload['draft'] as Map? ?? const {},
    );
    final assistantReply = (payload['assistant_reply'] ?? '').toString().trim();
    final mode = (payload['mode'] ?? 'collecting').toString().trim();
    final recognizedPhone = (payload['recognized_phone'] ?? '')
        .toString()
        .trim();
    final token = (payload['token'] ?? '').toString().trim();
    final profile = Map<String, dynamic>.from(
      payload['profile'] as Map? ?? const {},
    );
    final missingFields = ((payload['missing_fields'] as List?) ?? const [])
        .map((item) => item.toString().trim())
        .where((item) => item.isNotEmpty)
        .toList(growable: false);
    final sessionId = (draft['session_id'] ?? '').toString().trim();
    final draftPhone = (draft['phone_number'] ?? '').toString().trim();
    final draftSummary = (draft['dynamic_profile_summary'] ?? '')
        .toString()
        .trim();
    final isLoginConfirmationMode = mode == 'login_confirmation';
    final needsPhoneNumber = missingFields.contains('phone_number');
    final hasCollectedInput =
        countAsProgress ||
        _hasCollectedOnboardingInput ||
        isLoginConfirmationMode ||
        mode == 'ready_to_register' ||
        recognizedPhone.isNotEmpty ||
        draftPhone.isNotEmpty ||
        draftSummary.isNotEmpty;

    if (sessionId.isNotEmpty) {
      _onboardingSessionId = sessionId;
      await _prefs?.setString(_onboardingSessionKey, sessionId);
    }
    if (!mounted) return;
    setState(() {
      _conversationMode = mode.isEmpty ? 'collecting' : mode;
      _recognizedPhone = recognizedPhone;
      _hasCollectedOnboardingInput = hasCollectedInput;
      _hasCompletedIntroduction = _hasCompletedIntroduction || countAsProgress;
      if (_hasCompletedIntroduction) {
        _visualStep = 2;
      }
      _pendingVoicePrompt = _resolvedOnboardingPrompt(
        assistantReply: assistantReply,
        isLoginConfirmationMode: isLoginConfirmationMode,
        needsPhoneNumber: needsPhoneNumber,
      );
      _isListening = false;
      _isWorking = false;
      _isConfirming = isLoginConfirmationMode;
      _statusText = _resolvedOnboardingStatusText(
        assistantReply: assistantReply,
        isLoginConfirmationMode: isLoginConfirmationMode,
        needsPhoneNumber: needsPhoneNumber,
      );
    });

    if (token.isNotEmpty) {
      final displayName =
          (profile['display_name'] ?? profile['name'] ?? 'Friend')
              .toString()
              .trim();
      final userId = int.tryParse((profile['user_id'] ?? '0').toString()) ?? 0;
      await _prefs?.setString(_tokenKey, token);
      await _prefs?.remove(_onboardingSessionKey);
      if (!mounted) return;
      setState(() {
        _session = AppAccountSession(
          token: token,
          userId: userId,
          displayName: displayName.isEmpty ? 'Friend' : displayName,
          phoneNumber: recognizedPhone,
        );
      });
      await _enterGoogleCalendarStep(_session!);
      return;
    }

    if (_conversationMode == 'ready_to_register') {
      await _completeOnboardingRegistration();
      return;
    }
    if (!autoContinue ||
        _conversationMode == 'completed' ||
        !_conversationActivated) {
      return;
    }
    if (_conversationMode == 'login_confirmation') {
      await _handleLoginConfirmation();
      return;
    }
    _scheduleAutoListenRetry(
      assistantReply.isEmpty
          ? const Duration(milliseconds: 250)
          : const Duration(milliseconds: 450),
    );
  }

  Future<void> _completeOnboardingRegistration() async {
    if (!mounted) return;
    setState(() {
      _isWorking = true;
      _statusText = 'Creating your profile...';
    });
    try {
      final location = await _ensureRegistrationLocation();
      if (location == null) {
        throw StateError('Location access is required before registration can finish.');
      }
      final payload = await _backendClient.completeOnboarding(
        sessionId: _onboardingSessionId,
        homeLat: (location['lat'] as num).toDouble(),
        homeLng: (location['lng'] as num).toDouble(),
      );
      await _handleOnboardingPayload(payload, autoContinue: false);
    } catch (error) {
      if (!mounted) return;
      setState(() {
        _isWorking = false;
        _statusText = 'Registration could not finish. ${error.toString()}';
      });
    }
  }

  Future<Map<String, dynamic>?> _ensureRegistrationLocation() async {
    if (_registrationLocationPayload != null) {
      return _registrationLocationPayload;
    }
    try {
      final serviceEnabled = await Geolocator.isLocationServiceEnabled();
      if (!serviceEnabled) {
        return null;
      }
      var permission = await Geolocator.checkPermission();
      if (permission == LocationPermission.denied) {
        permission = await Geolocator.requestPermission();
      }
      if (permission == LocationPermission.denied ||
          permission == LocationPermission.deniedForever) {
        return null;
      }
      Position? position;
      try {
        position = await Geolocator.getCurrentPosition(
          locationSettings: const LocationSettings(
            accuracy: LocationAccuracy.medium,
          ),
        );
      } catch (_) {
        position = await Geolocator.getLastKnownPosition();
      }
      if (position == null) {
        return null;
      }
      _registrationLocationPayload = <String, dynamic>{
        'lat': position.latitude,
        'lng': position.longitude,
      };
      return _registrationLocationPayload;
    } catch (_) {
      return null;
    }
  }

  Future<void> _enterGoogleCalendarStep(AppAccountSession session) async {
    if (!mounted) return;
    setState(() {
      _pendingGoogleCalendarSession = session;
      _stage = HelloAgainStage.googleCalendar;
      _googleCalendarConnected = false;
      _googleCalendarEmail = '';
      _googleCalendarStatusText =
          'Optional. Connect Google Calendar now, or skip for now.';
    });
    await _refreshGoogleCalendarStatus();
  }

  Future<void> _refreshGoogleCalendarStatus() async {
    final session = _pendingGoogleCalendarSession;
    if (session == null) return;
    if (!mounted) return;
    setState(() {
      _isGoogleCalendarWorking = true;
    });
    try {
      final payload = await _backendClient.fetchGoogleCalendarStatus(
        token: session.token,
      );
      if (!mounted) return;
      setState(() {
        _googleCalendarConnected = (payload['connected'] ?? false) == true;
        _googleCalendarEmail = (payload['google_email'] ?? '').toString().trim();
        _googleCalendarStatusText = _googleCalendarConnected
            ? 'Google Calendar is connected. You can continue.'
            : 'Optional. Add meetups and reminders to your phone calendar automatically.';
      });
    } catch (_) {
      if (!mounted) return;
      setState(() {
        _googleCalendarStatusText =
            'Google Calendar is optional. You can connect now or skip for now.';
      });
    } finally {
      if (mounted) {
        setState(() {
          _isGoogleCalendarWorking = false;
        });
      }
    }
  }

  Future<void> _startGoogleCalendarConnect() async {
    final session = _pendingGoogleCalendarSession;
    if (session == null || _isGoogleCalendarWorking) return;
    setState(() {
      _isGoogleCalendarWorking = true;
      _googleCalendarStatusText =
          'Opening Google sign-in. Return to HelloAgain after you finish.';
    });
    try {
      final payload = await _backendClient.startGoogleCalendarConnect(
        token: session.token,
      );
      final authUrl = (payload['auth_url'] ?? '').toString().trim();
      if (authUrl.isEmpty) {
        throw StateError('Google Calendar auth URL is missing.');
      }
      await launchUrl(
        Uri.parse(authUrl),
        mode: LaunchMode.externalApplication,
      );
    } catch (error) {
      if (!mounted) return;
      setState(() {
        _googleCalendarStatusText =
            'Google Calendar could not start right now. ${error.toString()}';
      });
    } finally {
      if (mounted) {
        setState(() {
          _isGoogleCalendarWorking = false;
        });
      }
    }
  }

  Future<void> _skipGoogleCalendarForNow() async {
    await _continueIntoBoard();
  }

  Future<void> _continueIntoBoard() async {
    final session = _pendingGoogleCalendarSession ?? _session;
    if (session == null || !mounted) return;
    setState(() {
      _session = session;
      _stage = HelloAgainStage.board;
    });
  }

  String _resolvedOnboardingPrompt({
    required String assistantReply,
    required bool isLoginConfirmationMode,
    required bool needsPhoneNumber,
  }) {
    if (isLoginConfirmationMode) {
      return '';
    }
    if (needsPhoneNumber) {
      return 'Споделете телефонния си номер, за да продължите. Това е задължителна стъпка за вход.';
    }
    if (!_hasCompletedIntroduction) {
      return 'Кажете името си и малко за себе си.';
    }
    return assistantReply;
  }

  String _resolvedOnboardingStatusText({
    required String assistantReply,
    required bool isLoginConfirmationMode,
    required bool needsPhoneNumber,
  }) {
    if (isLoginConfirmationMode) {
      return 'Потвърдете дали това е вашият профил.';
    }
    if (needsPhoneNumber) {
      return 'Очаквам телефонния ви номер. Това е задължителна стъпка за вход.';
    }
    if (!_hasCompletedIntroduction) {
      return 'Очаквам да кажете името си и малко за себе си.';
    }
    return assistantReply.isEmpty
        ? 'Продължаваме спокойно напред.'
        : assistantReply;
  }

  Future<String> _resolveCapturedTranscript(
    CapturedAudioTurn capturedTurn,
  ) async {
    final directTranscript = (capturedTurn.transcript ?? '').trim();
    if (directTranscript.isNotEmpty) return directTranscript;
    final payload = await _backendClient.transcribeSpeechTurn(
      audioBase64: capturedTurn.audioBase64,
      audioMimeType: capturedTurn.mimeType,
      userId: 'hello_again_onboarding',
      sessionId: _onboardingSessionId.isEmpty
          ? 'onboarding_${DateTime.now().millisecondsSinceEpoch}'
          : _onboardingSessionId,
      language: capturedTurn.language,
    );
    return (payload['transcript'] ?? payload['message'] ?? '')
        .toString()
        .trim();
  }

  bool _isRecoverableListeningError(Object error) {
    final lowered = error.toString().toLowerCase();
    return lowered.contains('no speech') ||
        lowered.contains('no-speech') ||
        lowered.contains('timed out while listening') ||
        lowered.contains('did not return a transcript');
  }

  bool? _normalizeBulgarianConfirmationAnswer(String transcript) {
    final normalizedTranscript = transcript.trim().toLowerCase();
    if (normalizedTranscript.isEmpty) return null;
    final words = normalizedTranscript
        .replaceAll(RegExp('[^A-Za-z0-9\u0400-\u04FF\\s]+', unicode: true), ' ')
        .split(RegExp(r'\s+'))
        .where((item) => item.isNotEmpty)
        .toList();
    const yesWords = {
      'да',
      'da',
      'yes',
      'yep',
      'yeah',
      'correct',
      'right',
      'точно',
      'правилно',
      'потвърждавам',
    };
    const noWords = {
      'не',
      'ne',
      'no',
      'wrong',
      'repeat',
      'again',
      'nope',
      'грешно',
      'отново',
      'повтори',
    };
    if (words.isNotEmpty) {
      final firstWord = words.first;
      if (yesWords.contains(firstWord)) return true;
      if (noWords.contains(firstWord)) return false;
    }
    if (words.any(yesWords.contains)) return true;
    if (words.any(noWords.contains)) return false;
    return null;
  }

  Future<void> _speakOnboardingText(String text) async {
    final clean = text.trim();
    if (clean.isEmpty) return;
    try {
      final payload = await _backendClient.speakText(
        text: clean,
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
    await _voiceBridge.playText(clean);
  }

  int _currentStep() {
    if (_stage != HelloAgainStage.onboarding) {
      return 1;
    }
    return _visualStep.clamp(1, 2);
  }

  @override
  Widget build(BuildContext context) {
    switch (_stage) {
      case HelloAgainStage.booting:
      case HelloAgainStage.intro:
        return IntroOnboardingScreen(statusText: _statusText);
      case HelloAgainStage.onboarding:
        return RegistrationScreen(
          statusText: _statusText,
          currentStep: _currentStep(),
          voiceReady: _voiceReady,
          isListening: _isListening,
          isWorking: _isWorking,
          isConfirming: _isConfirming,
          conversationActivated: _conversationActivated,
          onPrimaryAction: _handlePrimaryAction,
        );
      case HelloAgainStage.googleCalendar:
        return GoogleCalendarConnectView(
          connected: _googleCalendarConnected,
          connectedEmail: _googleCalendarEmail,
          isWorking: _isGoogleCalendarWorking,
          statusText: _googleCalendarStatusText,
          onConnect: _startGoogleCalendarConnect,
          onSkip: _skipGoogleCalendarForNow,
          onContinue: _continueIntoBoard,
        );
      case HelloAgainStage.board:
        if (_session == null) {
          return IntroOnboardingScreen(statusText: 'Preparing Hello Again...');
        }
        return AgentBoardScreen(session: _session!);
    }
  }
}

class AgentBoardScreen extends StatelessWidget {
  const AgentBoardScreen({super.key, required this.session});

  final AppAccountSession session;

  @override
  Widget build(BuildContext context) {
    return whitespace.AgentBoardScreen(
      userId: session.userId.toString(),
      accountToken: session.token,
      welcomeText: 'Welcome back, ${session.displayName}. Your space is ready.',
    );
  }
}

class IntroOnboardingScreen extends StatelessWidget {
  const IntroOnboardingScreen({super.key, required this.statusText});

  final String statusText;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Scaffold(
      body: DecoratedBox(
        decoration: const BoxDecoration(
          gradient: LinearGradient(
            begin: Alignment.topLeft,
            end: Alignment.bottomRight,
            colors: [
              HelloAgainPalette.whiteSmoke,
              HelloAgainPalette.almondCream,
              HelloAgainPalette.dustGrey,
            ],
          ),
        ),
        child: Center(
          child: Padding(
            padding: const EdgeInsets.symmetric(horizontal: 28),
            child: ConstrainedBox(
              constraints: const BoxConstraints(maxWidth: 420),
              child: Container(
                padding: const EdgeInsets.all(28),
                decoration: BoxDecoration(
                  color: Colors.white.withValues(alpha: 0.72),
                  borderRadius: BorderRadius.circular(32),
                  border: Border.all(
                    color: HelloAgainPalette.dustGrey.withValues(alpha: 0.8),
                  ),
                  boxShadow: const [
                    BoxShadow(
                      color: Color(0x14000000),
                      blurRadius: 30,
                      offset: Offset(0, 14),
                    ),
                  ],
                ),
                child: Column(
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    Container(
                      width: 72,
                      height: 72,
                      decoration: const BoxDecoration(
                        color: HelloAgainPalette.bloodRed,
                        shape: BoxShape.circle,
                      ),
                      child: const Icon(
                        Icons.graphic_eq_rounded,
                        color: HelloAgainPalette.whiteSmoke,
                        size: 34,
                      ),
                    ),
                    const SizedBox(height: 24),
                    Text(
                      'Hello Again',
                      style: theme.textTheme.headlineMedium?.copyWith(
                        fontWeight: FontWeight.w800,
                      ),
                      textAlign: TextAlign.center,
                    ),
                    const SizedBox(height: 12),
                    Text(
                      statusText,
                      style: theme.textTheme.bodyLarge?.copyWith(
                        color: HelloAgainPalette.ink.withValues(alpha: 0.72),
                      ),
                      textAlign: TextAlign.center,
                    ),
                    const SizedBox(height: 24),
                    const CircularProgressIndicator(
                      color: HelloAgainPalette.blushedBrick,
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
}

class RegistrationScreen extends StatelessWidget {
  const RegistrationScreen({
    super.key,
    required this.statusText,
    required this.currentStep,
    required this.voiceReady,
    required this.isListening,
    required this.isWorking,
    required this.isConfirming,
    required this.conversationActivated,
    required this.onPrimaryAction,
  });

  final String statusText;
  final int currentStep;
  final bool voiceReady;
  final bool isListening;
  final bool isWorking;
  final bool isConfirming;
  final bool conversationActivated;
  final Future<void> Function() onPrimaryAction;

  String _body() {
    if (currentStep == 1) {
      return 'Кажете името си и малко за себе си!';
    }
    return 'Споделете телефонния си номер за да продължите!';
  }

  String _statusTextLine() {
    if (isListening) {
      return 'Listening now...';
    }
    if (isWorking || isConfirming) {
      return 'Processing your answer...';
    }
    if (!voiceReady) {
      return 'Microphone access is required.';
    }
    return 'Speak naturally when you are ready.';
  }

  String _primaryButtonLabel() {
    if (!conversationActivated) {
      return 'Започни Разговор';
    }
    return 'Разговорът започна';
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Scaffold(
      body: DecoratedBox(
        decoration: const BoxDecoration(
          gradient: LinearGradient(
            begin: Alignment.topCenter,
            end: Alignment.bottomCenter,
            colors: [
              HelloAgainPalette.whiteSmoke,
              HelloAgainPalette.almondCream,
            ],
          ),
        ),
        child: SafeArea(
          child: LayoutBuilder(
            builder: (context, constraints) {
              final compactLayout =
                  constraints.maxWidth < 360 || constraints.maxHeight < 760;
              final widthFactor = (constraints.maxWidth / 380).clamp(0.84, 1.0);
              final cardHeight = (constraints.maxHeight - 36)
                  .clamp(compactLayout ? 500.0 : 560.0, 680.0)
                  .toDouble();
              final titleFontSize =
                  ((theme.textTheme.headlineLarge?.fontSize ?? 32) *
                          widthFactor)
                      .clamp(28.0, 34.0)
                      .toDouble();
              final promptFontSize =
                  ((theme.textTheme.titleMedium?.fontSize ?? 18) * widthFactor)
                      .clamp(16.0, 20.0)
                      .toDouble();
              final speechFontSize =
                  ((theme.textTheme.titleSmall?.fontSize ?? 14) * widthFactor)
                      .clamp(13.0, 16.0)
                      .toDouble();
              final statusFontSize =
                  ((theme.textTheme.bodyMedium?.fontSize ?? 14) * widthFactor)
                      .clamp(13.0, 15.0)
                      .toDouble();

              return Center(
                child: SingleChildScrollView(
                  padding: const EdgeInsets.symmetric(
                    horizontal: 18,
                    vertical: 18,
                  ),
                  child: ConstrainedBox(
                    constraints: const BoxConstraints(maxWidth: 380),
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.stretch,
                      children: [
                        Container(
                          height: cardHeight,
                          padding: EdgeInsets.fromLTRB(
                            compactLayout ? 14 : 16,
                            compactLayout ? 16 : 18,
                            compactLayout ? 14 : 16,
                            compactLayout ? 16 : 18,
                          ),
                          decoration: BoxDecoration(
                            color: HelloAgainPalette.dustGrey.withValues(
                              alpha: 0.42,
                            ),
                            borderRadius: BorderRadius.circular(28),
                            border: Border.all(
                              color: HelloAgainPalette.whiteSmoke.withValues(
                                alpha: 0.9,
                              ),
                            ),
                            boxShadow: const [
                              BoxShadow(
                                color: Color(0x12000000),
                                blurRadius: 28,
                                offset: Offset(0, 14),
                              ),
                            ],
                          ),
                          child: Column(
                            crossAxisAlignment: CrossAxisAlignment.stretch,
                            children: [
                              Container(
                                height: 5,
                                decoration: BoxDecoration(
                                  color: HelloAgainPalette.whiteSmoke,
                                  borderRadius: BorderRadius.circular(999),
                                ),
                                child: TweenAnimationBuilder<double>(
                                  tween: Tween<double>(end: currentStep / 2),
                                  duration: const Duration(milliseconds: 360),
                                  curve: Curves.easeOutCubic,
                                  builder: (context, value, child) {
                                    return Align(
                                      alignment: Alignment.centerLeft,
                                      child: FractionallySizedBox(
                                        widthFactor: value.clamp(0.0, 1.0),
                                        child: child,
                                      ),
                                    );
                                  },
                                  child: Container(
                                    decoration: BoxDecoration(
                                      color: HelloAgainPalette.blushedBrick,
                                      borderRadius: BorderRadius.circular(999),
                                    ),
                                  ),
                                ),
                              ),
                              const SizedBox(height: 10),
                              Text(
                                'Step $currentStep of 2',
                                style: theme.textTheme.labelLarge?.copyWith(
                                  color: HelloAgainPalette.blushedBrick,
                                  fontWeight: FontWeight.w700,
                                ),
                                textAlign: TextAlign.left,
                              ),
                              const Spacer(),
                              Center(
                                child: Image.asset(
                                  'assets/icons/red_hand.png',
                                  width: compactLayout ? 72 : 84,
                                  height: compactLayout ? 72 : 84,
                                  fit: BoxFit.contain,
                                ),
                              ),
                              SizedBox(height: compactLayout ? 14 : 18),
                              Text(
                                'HelloAgain',
                                style: theme.textTheme.headlineLarge?.copyWith(
                                  fontSize: titleFontSize,
                                  fontWeight: FontWeight.w900,
                                  color: HelloAgainPalette.ink,
                                  letterSpacing: -1.2,
                                ),
                                textAlign: TextAlign.center,
                              ),
                              SizedBox(height: compactLayout ? 8 : 10),
                              Text(
                                _body(),
                                style: theme.textTheme.titleMedium?.copyWith(
                                  fontSize: promptFontSize,
                                  color: HelloAgainPalette.ink,
                                  fontWeight: FontWeight.w700,
                                  height: 1.35,
                                ),
                                textAlign: TextAlign.center,
                              ),
                              SizedBox(height: compactLayout ? 10 : 12),
                              Container(
                                padding: EdgeInsets.symmetric(
                                  horizontal: compactLayout ? 12 : 14,
                                  vertical: compactLayout ? 10 : 12,
                                ),
                                decoration: BoxDecoration(
                                  color: Colors.white.withValues(alpha: 0.66),
                                  borderRadius: BorderRadius.circular(18),
                                  border: Border.all(
                                    color: HelloAgainPalette.whiteSmoke,
                                  ),
                                ),
                                child: Text(
                                  statusText,
                                  style: theme.textTheme.titleSmall?.copyWith(
                                    fontSize: speechFontSize,
                                    color: HelloAgainPalette.ink,
                                    fontWeight: FontWeight.w600,
                                    height: 1.35,
                                  ),
                                  textAlign: TextAlign.center,
                                  maxLines: compactLayout ? 4 : 5,
                                  overflow: TextOverflow.ellipsis,
                                ),
                              ),
                              SizedBox(height: compactLayout ? 10 : 12),
                              Text(
                                _statusTextLine(),
                                style: theme.textTheme.bodyMedium?.copyWith(
                                  fontSize: statusFontSize,
                                  color: HelloAgainPalette.ink.withValues(
                                    alpha: 0.66,
                                  ),
                                  fontWeight: FontWeight.w600,
                                  height: 1.3,
                                ),
                                textAlign: TextAlign.center,
                              ),
                              const Spacer(),
                              FilledButton(
                                onPressed:
                                    !conversationActivated &&
                                        !isListening &&
                                        !isWorking &&
                                        !isConfirming
                                    ? () => unawaited(onPrimaryAction())
                                    : null,
                                style: FilledButton.styleFrom(
                                  backgroundColor: HelloAgainPalette.ink,
                                  foregroundColor: HelloAgainPalette.whiteSmoke,
                                  minimumSize: const Size.fromHeight(52),
                                  shape: RoundedRectangleBorder(
                                    borderRadius: BorderRadius.circular(12),
                                  ),
                                ),
                                child: Text(
                                  _primaryButtonLabel(),
                                  style: theme.textTheme.titleMedium?.copyWith(
                                    color: HelloAgainPalette.whiteSmoke,
                                    fontWeight: FontWeight.w700,
                                  ),
                                ),
                              ),
                            ],
                          ),
                        ),
                      ],
                    ),
                  ),
                ),
              );
            },
          ),
        ),
      ),
    );
  }
}
