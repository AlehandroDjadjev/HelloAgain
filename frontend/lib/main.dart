import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_dotenv/flutter_dotenv.dart';
import 'package:geolocator/geolocator.dart';

import 'src/screens/permission_screen.dart';
import 'meetup_screen.dart';
import 'voice_lab_screen.dart';
import 'weather_screen.dart';

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();
  try {
    await dotenv.load(fileName: '.env');
  } catch (_) {
    // Keep startup resilient when the optional env file is not present yet.
  }
  await SystemChrome.setPreferredOrientations([
    DeviceOrientation.portraitUp,
    DeviceOrientation.portraitDown,
  ]);
  runApp(const HelloAgainApp());
}

class HelloAgainApp extends StatelessWidget {
  const HelloAgainApp({super.key});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'HelloAgain',
      debugShowCheckedModeBanner: false,
      theme: _buildTheme(Brightness.light),
      darkTheme: _buildTheme(Brightness.dark),
      home: const PermissionScreen(),
    );
  }

  static ThemeData _buildTheme(Brightness brightness) {
    final isDark = brightness == Brightness.dark;
    const seed = Color(0xFF3B82F6);

    return ThemeData(
      useMaterial3: true,
      colorScheme: ColorScheme.fromSeed(
        seedColor: seed,
        brightness: brightness,
        surface: isDark ? const Color(0xFF0F172A) : Colors.white,
        surfaceContainerHighest: isDark
            ? const Color(0xFF1E293B)
            : const Color(0xFFF1F5F9),
      ),
      scaffoldBackgroundColor: isDark
          ? const Color(0xFF0F172A)
          : const Color(0xFFF8FAFC),
      cardTheme: CardThemeData(
        elevation: 0,
        shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(12)),
        color: isDark ? const Color(0xFF1E293B) : Colors.white,
      ),
    );
  }
}

class MainShell extends StatefulWidget {
  const MainShell({super.key, this.initialUserPosition, this.initialIndex = 2});

  final Position? initialUserPosition;
  final int initialIndex;

  @override
  State<MainShell> createState() => _MainShellState();
}

class _MainShellState extends State<MainShell> {
  late int _currentIndex;
  Position? _userPosition;
  bool _isRequestingLocation = false;
  String? _locationError;

  @override
  void initState() {
    super.initState();
    _currentIndex = widget.initialIndex;
    _userPosition = widget.initialUserPosition;
  }

  Future<void> _requestLocation() async {
    setState(() {
      _isRequestingLocation = true;
      _locationError = null;
    });

    try {
      final serviceEnabled = await Geolocator.isLocationServiceEnabled();
      if (!serviceEnabled) {
        throw Exception('Location services are turned off on this device.');
      }

      var permission = await Geolocator.checkPermission();
      if (permission == LocationPermission.denied) {
        permission = await Geolocator.requestPermission();
      }

      if (permission == LocationPermission.denied) {
        throw Exception('Location permission was denied.');
      }

      if (permission == LocationPermission.deniedForever) {
        throw Exception(
          'Location permission was permanently denied. Re-enable it in settings.',
        );
      }

      final position = await Geolocator.getCurrentPosition();
      if (!mounted) {
        return;
      }

      setState(() {
        _userPosition = position;
        _isRequestingLocation = false;
        _locationError = null;
      });
    } catch (error) {
      if (!mounted) {
        return;
      }
      setState(() {
        _isRequestingLocation = false;
        _locationError = error.toString().replaceFirst('Exception: ', '');
      });
    }
  }

  List<Widget> _buildPages() {
    final position = _userPosition;
    return [
      position != null
          ? MeetupScreen(userPosition: position)
          : _LocationFeaturePlaceholder(
              title: 'Meetup Planner',
              description:
                  'This page needs your current position to recommend a meeting point.',
              icon: Icons.people_alt_rounded,
              isLoading: _isRequestingLocation,
              errorMessage: _locationError,
              onRequestLocation: _requestLocation,
            ),
      position != null
          ? WeatherScreen(userPosition: position)
          : _LocationFeaturePlaceholder(
              title: 'Weather View',
              description:
                  'Grant location access to load the local forecast for this device.',
              icon: Icons.wb_sunny_rounded,
              isLoading: _isRequestingLocation,
              errorMessage: _locationError,
              onRequestLocation: _requestLocation,
            ),
      const VoiceLabScreen(),
    ];
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      body: IndexedStack(index: _currentIndex, children: _buildPages()),
      bottomNavigationBar: NavigationBar(
        selectedIndex: _currentIndex,
        onDestinationSelected: (index) => setState(() => _currentIndex = index),
        backgroundColor: const Color(0xFF162040),
        indicatorColor: const Color(0xFF3B82F6).withValues(alpha: 0.25),
        labelBehavior: NavigationDestinationLabelBehavior.alwaysShow,
        destinations: const [
          NavigationDestination(
            icon: Icon(Icons.people_alt_outlined, color: Colors.white70),
            selectedIcon: Icon(Icons.people_alt, color: Colors.white),
            label: 'Meetup',
          ),
          NavigationDestination(
            icon: Icon(Icons.wb_sunny_outlined, color: Colors.white70),
            selectedIcon: Icon(Icons.wb_sunny, color: Colors.white),
            label: 'Weather',
          ),
          NavigationDestination(
            icon: Icon(Icons.mic_none_rounded, color: Colors.white70),
            selectedIcon: Icon(
              Icons.keyboard_voice_rounded,
              color: Colors.white,
            ),
            label: 'Voice Lab',
          ),
        ],
      ),
      filledButtonTheme: FilledButtonThemeData(
        style: FilledButton.styleFrom(
          shape: RoundedRectangleBorder(
            borderRadius: BorderRadius.circular(12),
          ),
        ),
      ),
        style: OutlinedButton.styleFrom(
          shape: RoundedRectangleBorder(
            borderRadius: BorderRadius.circular(12),
    );
  }
}

class _LocationFeaturePlaceholder extends StatelessWidget {
  const _LocationFeaturePlaceholder({
    required this.title,
    required this.icon,
    required this.isLoading,
    required this.errorMessage,
    required this.onRequestLocation,
  });

  final String title;
  final String description;
  final IconData icon;
  final bool isLoading;
  final String? errorMessage;
  final VoidCallback onRequestLocation;

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: const Color(0xFF0C1A35),
      body: Center(
        child: SingleChildScrollView(
          padding: const EdgeInsets.all(24),
          child: Container(
            constraints: const BoxConstraints(maxWidth: 420),
            padding: const EdgeInsets.all(24),
            decoration: BoxDecoration(
              color: const Color(0xFF162040),
              borderRadius: BorderRadius.circular(28),
              border: Border.all(color: Colors.white.withValues(alpha: 0.08)),
            ),
            child: Column(
              mainAxisSize: MainAxisSize.min,
              children: [
                Container(
                  width: 92,
                  height: 92,
                  decoration: BoxDecoration(
                    color: const Color(0xFF3B82F6).withValues(alpha: 0.14),
                    shape: BoxShape.circle,
                  ),
                  child: Icon(icon, size: 46, color: Colors.white),
                ),
                const SizedBox(height: 18),
                Text(
                  title,
                  textAlign: TextAlign.center,
                  style: const TextStyle(
                    color: Colors.white,
                    fontSize: 24,
                    fontWeight: FontWeight.w800,
                  ),
                ),
                const SizedBox(height: 12),
                Text(
                  description,
                  textAlign: TextAlign.center,
                  style: const TextStyle(
                    color: Colors.white70,
                    fontSize: 15,
                    height: 1.45,
                  ),
                ),
                if (errorMessage != null) ...[
                  const SizedBox(height: 16),
                  Text(
                    errorMessage!,
                    textAlign: TextAlign.center,
                    style: const TextStyle(
                      color: Color(0xFFFCA5A5),
                      fontSize: 14,
                      height: 1.4,
                    ),
                  ),
                ],
                const SizedBox(height: 22),
                SizedBox(
                  width: double.infinity,
                  child: ElevatedButton.icon(
                    onPressed: isLoading ? null : onRequestLocation,
                    icon: isLoading
                        ? const SizedBox(
                            width: 18,
                            height: 18,
                            child: CircularProgressIndicator(
                              strokeWidth: 2.4,
                              color: Colors.white,
                            ),
                          )
                        : const Icon(Icons.my_location_rounded),
                    label: Text(isLoading ? 'Requesting...' : 'Allow location'),
                    style: ElevatedButton.styleFrom(
                      backgroundColor: const Color(0xFF3B82F6),
                      foregroundColor: Colors.white,
                      padding: const EdgeInsets.symmetric(vertical: 16),
                      shape: RoundedRectangleBorder(
                        borderRadius: BorderRadius.circular(18),
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
