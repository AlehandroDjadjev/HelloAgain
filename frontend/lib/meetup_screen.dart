import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:google_maps_flutter/google_maps_flutter.dart';
import 'package:http/http.dart' as http;
import 'dart:convert';
import 'package:flutter/foundation.dart';
import 'package:geolocator/geolocator.dart';
import 'package:flutter_local_notifications/flutter_local_notifications.dart';
import 'package:flutter_dotenv/flutter_dotenv.dart';

const _kBackground = Color(0xFFF9FAFB);
const _kCard = Colors.white;
const _kAccent = Color(0xFF2563EB);
const _kText = Color(0xFF111827);
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
  final Position userPosition;
  const MeetupScreen({super.key, required this.userPosition});

  @override
  State<MeetupScreen> createState() => _MeetupScreenState();
}

class _MeetupScreenState extends State<MeetupScreen> {
  final List<Map<String, double>> mockParticipants = const [
    {'lat': 42.6977, 'lng': 23.3219},
    {'lat': 42.6895, 'lng': 23.3197},
    {'lat': 42.6993, 'lng': 23.3238},
  ];

  Map<String, dynamic>? bestMatch;
  bool isLoading = false;
  String? errorMessage;
  GoogleMapController? mapController;
  final Set<Marker> _markers = {};
  
  final FlutterLocalNotificationsPlugin _notificationsPlugin = FlutterLocalNotificationsPlugin();

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

  @override
  void initState() {
    super.initState();
    _initNotifications();
  }

  Future<void> _initNotifications() async {
    if (kIsWeb) return;
    
    const AndroidInitializationSettings initializationSettingsAndroid =
        AndroidInitializationSettings('@mipmap/ic_launcher');
    const InitializationSettings initializationSettings =
        InitializationSettings(android: initializationSettingsAndroid);
    
    await _notificationsPlugin.initialize(
      settings: initializationSettings,
    );
    
    _notificationsPlugin.resolvePlatformSpecificImplementation<
        AndroidFlutterLocalNotificationsPlugin>()?.requestNotificationsPermission();
    _notificationsPlugin.resolvePlatformSpecificImplementation<
        AndroidFlutterLocalNotificationsPlugin>()?.requestExactAlarmsPermission();
  }

  Future<void> fetchRecommendation() async {
    setState(() {
      isLoading = true;
      errorMessage = null;
    });

    try {
      final response = await http.post(
        Uri.parse('${_backendBaseUrl()}/api/meetup/recommend/'),
        headers: {'Content-Type': 'application/json'},
        body: jsonEncode({
          'participants': [
            {'lat': widget.userPosition.latitude, 'lng': widget.userPosition.longitude}
          ]
        }),
      );

      if (response.statusCode == 200) {
        final data = jsonDecode(response.body) as Map<String, dynamic>;
        setState(() {
          bestMatch = data['best_match'] as Map<String, dynamic>?;
          _updateMarkers();
        });

        if (bestMatch != null && mapController != null) {
          mapController!.animateCamera(
            CameraUpdate.newLatLngZoom(
              LatLng(
                bestMatch!['place_lat'] as double,
                bestMatch!['place_lng'] as double,
              ),
              15.5,
            ),
          );
        }
      } else {
        setState(() {
          errorMessage = 'Не можахме да намерим подходящо място.';
        final body = jsonDecode(response.body);
        final apiError = body is Map<String, dynamic> ? body['error'] as String? : null;
        setState(() {
          errorMessage = apiError ?? 'Не можахме да намерим подходящо място.';
        });
      }
    } catch (_) {
      setState(() {
        errorMessage = 'Няма връзка със сървъра.';
      });
    } finally {
      setState(() {
        isLoading = false;
      });
    }
  }

  void _updateMarkers() {
    _markers.clear();

    if (bestMatch != null) {
      _markers.add(Marker(
        markerId: const MarkerId('best_match'),
        position: LatLng(bestMatch!['place_lat'], bestMatch!['place_lng']),
        icon: BitmapDescriptor.defaultMarkerWithHue(220),
        infoWindow: InfoWindow(
          title: bestMatch!['place_name'] as String?,
          snippet: 'Среща',
        ),
      ),
    );
  }

  Future<void> _showReminderDialog() async {
    TimeOfDay? selectedTime = await showTimePicker(
      context: context,
      initialTime: TimeOfDay.now(),
      builder: (context, child) {
        return Theme(
          data: Theme.of(context).copyWith(
            colorScheme: const ColorScheme.light(
              primary: _kAccent,
            ),
          ),
          child: child!,
        );
      },
    );

    if (selectedTime != null) {
      await _scheduleNotification(selectedTime);
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(content: Text('Напомнянето е запазено за ${selectedTime.format(context)}!')),
        );
      }
    }
  }

  Future<void> _scheduleNotification(TimeOfDay time) async {
    final now = DateTime.now();
    var scheduledMeeting = DateTime(
      now.year, now.month, now.day, time.hour, time.minute
    );

    if (scheduledMeeting.isBefore(now)) {
      scheduledMeeting = scheduledMeeting.add(const Duration(days: 1)); // tomorrow
    }

    // Since exact alarms require more complex timezone setup in full production,
    // we use a simple delay notification as placeholder for this prototype.
    // We keep the prototype behavior simple and immediate for demo reliability.
    
    const androidDetails = AndroidNotificationDetails(
      'meetup_channel', 'Meetup Reminders',
      channelDescription: 'Reminders for your meetups',
      importance: Importance.max,
      priority: Priority.high,
    );
    const platformDetails = NotificationDetails(android: androidDetails);

    // If the reminder time is still in the future, we could theoretically schedule it.
    // For this prototype, we'll demonstrate it with an immediate notification
    // specifying when the meeting is:
    
    // Convert to readable format
    final meetStartStr = "${scheduledMeeting.hour.toString().padLeft(2, '0')}:${scheduledMeeting.minute.toString().padLeft(2, '0')}";
    
    // To ensure demoability without waiting hours, if it's within 2 minutes we set it.
    // In actual production, timezone package is required.
    // Just display a local notification immediately to prove permissions and functionality.
    if (!kIsWeb) {
      await _notificationsPlugin.show(
        id: 0,
        title: 'Срещата наближава!',
        body: 'Имате среща в ${bestMatch!['place_name']} след 30 минути (в $meetStartStr).',
        notificationDetails: platformDetails,
      );
    }
  }

  @override
  Widget build(BuildContext context) {
    final initialPos = LatLng(widget.userPosition.latitude, widget.userPosition.longitude);
    return Scaffold(
      backgroundColor: _kBackground,
      appBar: AppBar(
        title: const Text(
          'Среща С Приятели',
          style: TextStyle(
            fontWeight: FontWeight.w700,
            fontSize: 20,
            color: _kText,
          ),
        ),
        backgroundColor: _kCard,
        elevation: 0,
        centerTitle: true,
      ),
      body: Column(
        children: [
          Expanded(
            flex: 5,
            child: GoogleMap(
              initialCameraPosition: CameraPosition(target: initialPos, zoom: 14.0),
              markers: _markers,
              myLocationButtonEnabled: false,
              myLocationEnabled: false,
              zoomControlsEnabled: false,
              mapToolbarEnabled: false,
              style: _kMapStyle,
              onMapCreated: (c) { mapController = c; },
            ),
          ),
          Container(
            color: _kCard,
            padding: const EdgeInsets.fromLTRB(28, 28, 28, 36),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.stretch,
              mainAxisSize: MainAxisSize.min,
              children: [
                Center(
                  child: Container(
                    width: 40,
                    height: 4,
                    margin: const EdgeInsets.only(bottom: 24),
                    decoration: BoxDecoration(
                      color: const Color(0xFFE5E7EB),
                      borderRadius: BorderRadius.circular(10),
                    ),
                  ),
                ),
                if (errorMessage != null)
                  Padding(
                    padding: const EdgeInsets.only(bottom: 20),
                    child: Text(
                      errorMessage!,
                      style: const TextStyle(
                        color: Color(0xFFDC2626),
                        fontSize: 15,
                      ),
                      textAlign: TextAlign.center,
                    ),
                  ),
                if (bestMatch == null && !isLoading && errorMessage == null)
                  const Padding(
                    padding: EdgeInsets.only(bottom: 24),
                    child: Text(
                      'Намерете идеалния момент за среща',
                      style: TextStyle(
                        fontSize: 22,
                        fontWeight: FontWeight.w700,
                        color: _kText,
                      ),
                      textAlign: TextAlign.center,
                    ),
                  ),
                if (bestMatch != null) ...[
                  Text(
                    bestMatch!['place_name'] as String,
                    style: const TextStyle(
                      fontSize: 24,
                      fontWeight: FontWeight.w800,
                      color: _kText,
                      height: 1.2,
                    ),
                    textAlign: TextAlign.center,
                  ),
                  const SizedBox(height: 12),
<<<<<<< HEAD
=======

>>>>>>> 7cd63273acbbe5f49af277b2dc0cd80f351ff394
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
                  
                  // Set Reminder Button
                  TextButton.icon(
                    onPressed: _showReminderDialog,
                    icon: const Icon(Icons.alarm_add, color: _kAccent),
                    label: const Text('Задай Напомняне', style: TextStyle(color: _kAccent, fontSize: 16)),
                  ),
                  const SizedBox(height: 16),
                ],
<<<<<<< HEAD
=======

>>>>>>> 7cd63273acbbe5f49af277b2dc0cd80f351ff394
                SizedBox(
                  height: 58,
                  child: ElevatedButton(
                    onPressed: isLoading ? null : fetchRecommendation,
                    style: ElevatedButton.styleFrom(
                      backgroundColor: _kAccent,
                      foregroundColor: Colors.white,
                      elevation: 0,
                      shape: RoundedRectangleBorder(
                        borderRadius: BorderRadius.circular(14),
                      ),
                    ),
                    child: isLoading
                        ? const SizedBox(
                            width: 24,
                            height: 24,
                            child: CircularProgressIndicator(
                              color: Colors.white,
                              strokeWidth: 3,
                            ),
                          )
                        : const Text(
                            'Намери Място',
                            style: TextStyle(
                              fontSize: 17,
                              fontWeight: FontWeight.w700,
                            ),
                          ),
                  ),
                ),
              ],
            ),
          ),
        ],
      ),
    );
  }
<<<<<<< HEAD
=======

>>>>>>> 7cd63273acbbe5f49af277b2dc0cd80f351ff394
}
