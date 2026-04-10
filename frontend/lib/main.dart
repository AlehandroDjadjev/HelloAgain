import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_dotenv/flutter_dotenv.dart';

import 'onboarding.dart';
import 'src/screens/navigation_launcher_screen.dart';
import 'src/services/deep_link_bridge.dart';
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
  runApp(const HelloAgainApp());
}

class HelloAgainApp extends StatefulWidget {
  const HelloAgainApp({super.key});

  @override
  State<HelloAgainApp> createState() => _HelloAgainAppState();
}

class _HelloAgainAppState extends State<HelloAgainApp> {
  final GlobalKey<NavigatorState> _navigatorKey = GlobalKey<NavigatorState>();
  StreamSubscription<Uri>? _deepLinkSub;
  Uri? _pendingDeepLink;

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
      home: const HelloAgainShell(),
    );
  }
}
