import 'dart:async';

import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_dotenv/flutter_dotenv.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'onboarding.dart';
import 'src/screens/navigation_launcher_screen.dart';
import 'src/services/deep_link_bridge.dart';
import 'src/theme/app_theme.dart';

const bool _showDeveloperOnboardingTestButton =
    kDebugMode || bool.fromEnvironment('HELLO_AGAIN_FORCE_DEV_TEST_BUTTON');

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
  runApp(const HelloAgainApp());
}

class HelloAgainApp extends StatefulWidget {
  const HelloAgainApp({super.key});

  @override
  State<HelloAgainApp> createState() => _HelloAgainAppState();
}

class _HelloAgainAppState extends State<HelloAgainApp> {
  static const _tokenKey = 'hello_again.account_token';
  static const _onboardingSessionKey = 'hello_again.onboarding_session_id';

  final GlobalKey<NavigatorState> _navigatorKey = GlobalKey<NavigatorState>();
  StreamSubscription<Uri>? _deepLinkSub;
  Uri? _pendingDeepLink;
  int _shellVersion = 0;
  bool _isResettingOnboarding = false;

  @override
  void initState() {
    super.initState();
    _deepLinkSub = DeepLinkBridge.instance.links.listen(_handleDeepLink);
    WidgetsBinding.instance.addPostFrameCallback((_) async {
      final uri = await DeepLinkBridge.instance.consumeInitialLink();
      if (!mounted || uri == null) {
        return;
      }
      _handleDeepLink(uri);
    });
  }

  @override
  void dispose() {
    _deepLinkSub?.cancel();
    super.dispose();
  }

  void _handleDeepLink(Uri uri) {
    if (uri.scheme != 'helloagain' || uri.host != 'phone-command') {
      return;
    }
    final prompt = (uri.queryParameters['prompt'] ?? '').trim();
    if (prompt.isEmpty) {
      return;
    }

    final navigator = _navigatorKey.currentState;
    if (navigator == null) {
      _pendingDeepLink = uri;
      WidgetsBinding.instance.addPostFrameCallback((_) {
        final pending = _pendingDeepLink;
        _pendingDeepLink = null;
        if (pending != null && mounted) {
          _handleDeepLink(pending);
        }
      });
      return;
    }

    navigator.push(
      MaterialPageRoute(
        builder: (_) => NavigationLauncherScreen(
          initialPrompt: prompt,
          autoRunOnOpen: true,
        ),
      ),
    );
  }

  Future<void> _restartOnboardingForTesting() async {
    if (_isResettingOnboarding) {
      return;
    }

    setState(() {
      _isResettingOnboarding = true;
    });

    try {
      final prefs = await SharedPreferences.getInstance();
      await prefs.remove(_tokenKey);
      await prefs.remove(_onboardingSessionKey);

      _navigatorKey.currentState?.popUntil((route) => route.isFirst);

      if (!mounted) {
        return;
      }

      setState(() {
        _shellVersion += 1;
      });

      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(
          content: Text(
            'Developer test mode reset the local session and relaunched onboarding.',
          ),
        ),
      );
    } finally {
      if (mounted) {
        setState(() {
          _isResettingOnboarding = false;
        });
      }
    }
  }

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'Hello Again',
      debugShowCheckedModeBanner: false,
      navigatorKey: _navigatorKey,
      theme: buildHelloAgainTheme(
        scaffoldBackgroundColor: HelloAgainPalette.whiteSmoke,
        seedColor: HelloAgainPalette.blushedBrick,
        surfaceColor: HelloAgainPalette.almondCream,
      ),
      builder: (context, child) {
        final content = child ?? const SizedBox.shrink();
        if (!_showDeveloperOnboardingTestButton) {
          return content;
        }

        return Stack(
          children: [
            content,
            Positioned(
              right: 16,
              bottom: 16,
              child: SafeArea(
                child: Material(
                  color: Colors.transparent,
                  child: Container(
                    padding: const EdgeInsets.all(12),
                    decoration: BoxDecoration(
                      color: HelloAgainPalette.ink.withValues(alpha: 0.92),
                      borderRadius: BorderRadius.circular(20),
                      border: Border.all(
                        color: HelloAgainPalette.whiteSmoke.withValues(
                          alpha: 0.22,
                        ),
                      ),
                      boxShadow: const [
                        BoxShadow(
                          color: Color(0x22000000),
                          blurRadius: 18,
                          offset: Offset(0, 10),
                        ),
                      ],
                    ),
                    child: Column(
                      mainAxisSize: MainAxisSize.min,
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        Text(
                          'Developer only',
                          style: Theme.of(context).textTheme.labelSmall
                              ?.copyWith(
                                color: HelloAgainPalette.whiteSmoke.withValues(
                                  alpha: 0.82,
                                ),
                                fontWeight: FontWeight.w700,
                              ),
                        ),
                        const SizedBox(height: 8),
                        FilledButton.icon(
                          onPressed: _isResettingOnboarding
                              ? null
                              : _restartOnboardingForTesting,
                          icon: const Icon(Icons.science_outlined, size: 18),
                          label: Text(
                            _isResettingOnboarding
                                ? 'Resetting...'
                                : 'Test onboarding',
                          ),
                          style: FilledButton.styleFrom(
                            backgroundColor: HelloAgainPalette.blushedBrick,
                            foregroundColor: HelloAgainPalette.whiteSmoke,
                            visualDensity: VisualDensity.compact,
                            padding: const EdgeInsets.symmetric(
                              horizontal: 14,
                              vertical: 10,
                            ),
                          ),
                        ),
                      ],
                    ),
                  ),
                ),
              ),
            ),
          ],
        );
      },
      home: KeyedSubtree(
        key: ValueKey(_shellVersion),
        child: const HelloAgainShell(),
      ),
    );
  }
}
