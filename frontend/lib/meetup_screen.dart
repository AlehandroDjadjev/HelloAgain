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
  Map<String, dynamic>? bestMatch;
  bool isLoading = false;
  String? errorMessage;
  GoogleMapController? mapController;
  final Set<Marker> _markers = {};
  final FlutterLocalNotificationsPlugin _notificationsPlugin =
      FlutterLocalNotificationsPlugin();
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

    const initializationSettingsAndroid = AndroidInitializationSettings(
      '@mipmap/ic_launcher',
    );
    const initializationSettings = InitializationSettings(
      android: initializationSettingsAndroid,
    );

    await _notificationsPlugin.initialize(settings: initializationSettings);

    final androidImpl = _notificationsPlugin
        .resolvePlatformSpecificImplementation<
          AndroidFlutterLocalNotificationsPlugin
        >();
    await androidImpl?.requestNotificationsPermission();
    await androidImpl?.requestExactAlarmsPermission();
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

      if (response.statusCode == 200) {
        final data = jsonDecode(response.body) as Map<String, dynamic>;
        final match = data['best_match'] as Map<String, dynamic>?;

        setState(() {
          bestMatch = match;
          _updateMarkers();
        });

        if (match != null && mapController != null) {
          mapController!.animateCamera(
            CameraUpdate.newLatLngZoom(
              LatLng(
                (match['place_lat'] as num).toDouble(),
                (match['place_lng'] as num).toDouble(),
              ),
              15.5,
            ),
          );
        }
      } else {
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
        errorMessage = 'Няма връзка със сървъра.';
      });
    } finally {
      if (mounted) {
        setState(() {
          isLoading = false;
        });
      }
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
        content: Text(
          'Напомнянето е запазено за ${selectedTime.format(context)}!',
        ),
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

    if (scheduledMeeting.isBefore(now)) {
      scheduledMeeting = scheduledMeeting.add(const Duration(days: 1));
    }

    const androidDetails = AndroidNotificationDetails(
      'meetup_channel',
      'Meetup Reminders',
      channelDescription: 'Reminders for your meetups',
      importance: Importance.max,
      priority: Priority.high,
    );

    if (!kIsWeb && bestMatch != null) {
      final meetStartStr =
          '${scheduledMeeting.hour.toString().padLeft(2, '0')}:${scheduledMeeting.minute.toString().padLeft(2, '0')}';
      await _notificationsPlugin.show(
        id: 0,
        title: 'Срещата наближава!',
        body:
            'Имате среща в ${bestMatch!['place_name']} след 30 минути (в $meetStartStr).',
        notificationDetails: platformDetails,
      );
    }
  }

  @override
  Widget build(BuildContext context) {
    final initialPos = LatLng(
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
                target: initialPos,
                zoom: 14,
              ),
              markers: _markers,
              myLocationButtonEnabled: false,
              myLocationEnabled: false,
              zoomControlsEnabled: false,
              mapToolbarEnabled: false,
              style: _kMapStyle,
              onMapCreated: (controller) {
                mapController = controller;
              },
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
                  Center(
                    child: Container(
                      padding: const EdgeInsets.symmetric(
                        horizontal: 24,
                        vertical: 10,
                      ),
                      decoration: BoxDecoration(
                        color: const Color(0xFFEFF6FF),
                        borderRadius: BorderRadius.circular(50),
                      ),
                      child: Row(
                        mainAxisSize: MainAxisSize.min,
                        children: [
                          const Icon(
                            Icons.schedule_rounded,
                            color: _kAccent,
                            size: 20,
                          ),
                          const SizedBox(width: 8),
                          Text(
                            bestMatch!['recommended_time']
                                .toString()
                                .split(' ')
                                .last,
                            style: const TextStyle(
                              fontSize: 20,
                              fontWeight: FontWeight.w700,
                              color: _kAccent,
                            ),
                          ),
                        ],
                      ),
                    ),
                  ),
                  const SizedBox(height: 16),
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
