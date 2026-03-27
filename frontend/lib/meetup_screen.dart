import 'dart:async';
import 'dart:convert';

import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';
import 'package:flutter_dotenv/flutter_dotenv.dart';
import 'package:flutter_local_notifications/flutter_local_notifications.dart';
import 'package:geolocator/geolocator.dart';
import 'package:google_maps_flutter/google_maps_flutter.dart';
import 'package:http/http.dart' as http;

const _kBackground = Color(0xFFF8FAFC);
const _kCard = Colors.white;
const _kAccent = Color(0xFF2563EB);
const _kText = Color(0xFF111827);
const _kMuted = Color(0xFF6B7280);
const _kMapStyle = '''
[
  {"elementType":"geometry","stylers":[{"color":"#f8f8f8"}]},
  {"elementType":"labels.icon","stylers":[{"visibility":"off"}]},
  {"elementType":"labels.text.fill","stylers":[{"color":"#9e9e9e"}]},
  {"featureType":"administrative.locality","elementType":"labels.text.fill","stylers":[{"color":"#555555"}]},
  {"featureType":"poi","elementType":"labels","stylers":[{"visibility":"off"}]},
  {"featureType":"poi.park","elementType":"geometry","stylers":[{"color":"#e8f5e9"}]},
  {"featureType":"road","elementType":"geometry","stylers":[{"color":"#ffffff"}]},
  {"featureType":"road.arterial","elementType":"labels.text.fill","stylers":[{"color":"#aaaaaa"}]},
  {"featureType":"road.highway","elementType":"geometry","stylers":[{"color":"#f0f0f0"}]},
  {"featureType":"road.highway","elementType":"labels","stylers":[{"visibility":"off"}]},
  {"featureType":"transit","stylers":[{"visibility":"off"}]},
  {"featureType":"water","elementType":"geometry","stylers":[{"color":"#dce9f5"}]},
  {"featureType":"water","elementType":"labels.text.fill","stylers":[{"color":"#9e9e9e"}]}
]
''';

class MeetupScreen extends StatefulWidget {
  const MeetupScreen({super.key, required this.userPosition});

  final Position userPosition;

  @override
  State<MeetupScreen> createState() => _MeetupScreenState();
}

class _MeetupScreenState extends State<MeetupScreen> {
  final FlutterLocalNotificationsPlugin _notificationsPlugin =
      FlutterLocalNotificationsPlugin();
  final Set<Marker> _markers = {};

  GoogleMapController? _mapController;
  Map<String, dynamic>? _bestMatch;
  bool _isLoading = false;
  String? _errorMessage;

  @override
  void initState() {
    super.initState();
    unawaited(_initNotifications());
  }

  String _backendBaseUrl() {
    final configured = dotenv.env['API_BASE_URL']?.trim();
    if (configured != null && configured.isNotEmpty) {
      return configured;
    }
    if (kIsWeb) {
      return 'http://localhost:8000';
    }
    return defaultTargetPlatform == TargetPlatform.android
        ? 'http://10.0.2.2:8000'
        : 'http://localhost:8000';
  }

  Future<void> _initNotifications() async {
    if (kIsWeb) return;

    const androidSettings = AndroidInitializationSettings(
      '@mipmap/ic_launcher',
    );
    const settings = InitializationSettings(android: androidSettings);
    await _notificationsPlugin.initialize(settings: settings);

    final androidPlugin = _notificationsPlugin
        .resolvePlatformSpecificImplementation<
          AndroidFlutterLocalNotificationsPlugin
        >();
    await androidPlugin?.requestNotificationsPermission();
    await androidPlugin?.requestExactAlarmsPermission();
  }

  Future<void> fetchRecommendation() async {
    setState(() {
      _isLoading = true;
      _errorMessage = null;
    });

    try {
      final response = await http.post(
        Uri.parse('${_backendBaseUrl()}/api/meetup/recommend/'),
        headers: {'Content-Type': 'application/json'},
        body: jsonEncode({
          'participants': [
            {
              'lat': widget.userPosition.latitude,
              'lng': widget.userPosition.longitude,
            },
          ],
        }),
      );

      if (response.statusCode != 200) {
        final body = jsonDecode(response.body);
        final apiError = body is Map<String, dynamic>
            ? body['error'] as String?
            : null;
        setState(() {
          _errorMessage =
              apiError ?? 'Could not find a suitable meeting place.';
          _isLoading = false;
        });
        return;
      }

      final data = jsonDecode(response.body) as Map<String, dynamic>;
      final bestMatch = data['best_match'] as Map<String, dynamic>?;
      setState(() {
        _bestMatch = bestMatch;
        _isLoading = false;
        _updateMarkers();
      });

      if (_bestMatch != null && _mapController != null) {
        await _mapController!.animateCamera(
          CameraUpdate.newLatLngZoom(
            LatLng(
              (_bestMatch!['place_lat'] as num).toDouble(),
              (_bestMatch!['place_lng'] as num).toDouble(),
            ),
            15.5,
          ),
        );
      }
    } catch (_) {
      setState(() {
        _errorMessage = 'Could not connect to the meetup service.';
        _isLoading = false;
      });
    }
  }

  void _updateMarkers() {
    _markers
      ..clear()
      ..add(
        Marker(
          markerId: const MarkerId('you'),
          position: LatLng(
            widget.userPosition.latitude,
            widget.userPosition.longitude,
          ),
          infoWindow: const InfoWindow(title: 'You'),
        ),
      );

    if (_bestMatch == null) {
      return;
    }

    _markers.add(
      Marker(
        markerId: const MarkerId('best_match'),
        position: LatLng(
          (_bestMatch!['place_lat'] as num).toDouble(),
          (_bestMatch!['place_lng'] as num).toDouble(),
        ),
        icon: BitmapDescriptor.defaultMarkerWithHue(220),
        infoWindow: InfoWindow(
          title: _bestMatch!['place_name'] as String?,
          snippet: 'Suggested meetup',
        ),
      ),
    );
  }

  Future<void> _showReminderDialog() async {
    final selectedTime = await showTimePicker(
      context: context,
      initialTime: TimeOfDay.now(),
      builder: (context, child) {
        return Theme(
          data: Theme.of(
            context,
          ).copyWith(colorScheme: const ColorScheme.light(primary: _kAccent)),
          child: child!,
        );
      },
    );

    if (selectedTime == null) {
      return;
    }

    await _scheduleNotification(selectedTime);
    if (!mounted) {
      return;
    }

    ScaffoldMessenger.of(context).showSnackBar(
      SnackBar(
        content: Text('Reminder saved for ${selectedTime.format(context)}.'),
      ),
    );
  }

  Future<void> _scheduleNotification(TimeOfDay time) async {
    if (kIsWeb || _bestMatch == null) {
      return;
    }

    final now = DateTime.now();
    var scheduled = DateTime(
      now.year,
      now.month,
      now.day,
      time.hour,
      time.minute,
    );
    if (scheduled.isBefore(now)) {
      scheduled = scheduled.add(const Duration(days: 1));
    }

    const androidDetails = AndroidNotificationDetails(
      'meetup_channel',
      'Meetup Reminders',
      channelDescription: 'Reminders for your meetups',
      importance: Importance.max,
      priority: Priority.high,
    );

    final timeLabel =
        '${scheduled.hour.toString().padLeft(2, '0')}:${scheduled.minute.toString().padLeft(2, '0')}';

    await _notificationsPlugin.show(
      id: 0,
      title: 'Meetup reminder',
      body:
          'You have a meetup at ${_bestMatch!['place_name']} around $timeLabel.',
      notificationDetails: const NotificationDetails(android: androidDetails),
    );
  }

  @override
  Widget build(BuildContext context) {
    final initialPosition = LatLng(
      widget.userPosition.latitude,
      widget.userPosition.longitude,
    );

    return Scaffold(
      backgroundColor: _kBackground,
      appBar: AppBar(
        title: const Text('Meetup Planner'),
        backgroundColor: _kCard,
        foregroundColor: _kText,
        elevation: 0,
      ),
      body: Column(
        children: [
          Expanded(
            flex: 5,
            child: GoogleMap(
              initialCameraPosition: CameraPosition(
                target: initialPosition,
                zoom: 14,
              ),
              markers: _markers,
              myLocationButtonEnabled: false,
              myLocationEnabled: false,
              zoomControlsEnabled: false,
              mapToolbarEnabled: false,
              style: _kMapStyle,
              onMapCreated: (controller) => _mapController = controller,
            ),
          ),
          Container(
            color: _kCard,
            padding: const EdgeInsets.fromLTRB(24, 24, 24, 32),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.stretch,
              mainAxisSize: MainAxisSize.min,
              children: [
                if (_errorMessage != null) ...[
                  Text(
                    _errorMessage!,
                    textAlign: TextAlign.center,
                    style: const TextStyle(color: Colors.redAccent),
                  ),
                  const SizedBox(height: 16),
                ],
                if (_bestMatch == null &&
                    !_isLoading &&
                    _errorMessage == null) ...[
                  const Text(
                    'Find a good place and time for your meetup.',
                    textAlign: TextAlign.center,
                    style: TextStyle(
                      fontSize: 20,
                      fontWeight: FontWeight.w700,
                      color: _kText,
                    ),
                  ),
                  const SizedBox(height: 16),
                ],
                if (_bestMatch != null) ...[
                  Text(
                    _bestMatch!['place_name'] as String? ?? 'Suggested place',
                    textAlign: TextAlign.center,
                    style: const TextStyle(
                      fontSize: 24,
                      fontWeight: FontWeight.w800,
                      color: _kText,
                    ),
                  ),
                  const SizedBox(height: 10),
                  Text(
                    (_bestMatch!['recommended_time'] ?? '').toString(),
                    textAlign: TextAlign.center,
                    style: const TextStyle(
                      fontSize: 16,
                      fontWeight: FontWeight.w600,
                      color: _kAccent,
                    ),
                  ),
                  const SizedBox(height: 12),
                  TextButton.icon(
                    onPressed: _showReminderDialog,
                    icon: const Icon(Icons.alarm_add, color: _kAccent),
                    label: const Text(
                      'Set reminder',
                      style: TextStyle(color: _kAccent),
                    ),
                  ),
                  const SizedBox(height: 12),
                ],
                SizedBox(
                  height: 56,
                  child: ElevatedButton(
                    onPressed: _isLoading ? null : fetchRecommendation,
                    style: ElevatedButton.styleFrom(
                      backgroundColor: _kAccent,
                      foregroundColor: Colors.white,
                      elevation: 0,
                      shape: RoundedRectangleBorder(
                        borderRadius: BorderRadius.circular(14),
                      ),
                    ),
                    child: _isLoading
                        ? const SizedBox(
                            width: 24,
                            height: 24,
                            child: CircularProgressIndicator(
                              color: Colors.white,
                              strokeWidth: 3,
                            ),
                          )
                        : const Text(
                            'Find meetup',
                            style: TextStyle(
                              fontSize: 17,
                              fontWeight: FontWeight.w700,
                            ),
                          ),
                  ),
                ),
                const SizedBox(height: 8),
                const Text(
                  'Your current location is used as the starting point.',
                  textAlign: TextAlign.center,
                  style: TextStyle(color: _kMuted, fontSize: 13),
                ),
              ],
            ),
          ),
        ],
      ),
    );
  }
}
