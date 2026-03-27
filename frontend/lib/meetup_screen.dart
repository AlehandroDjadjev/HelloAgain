import 'dart:async';
import 'dart:convert';

import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';
import 'package:flutter_local_notifications/flutter_local_notifications.dart';
import 'package:geolocator/geolocator.dart';
import 'package:google_maps_flutter/google_maps_flutter.dart';
import 'package:http/http.dart' as http;

import 'src/config/backend_base_url.dart';

const _kBackground = Color(0xFFF8FAFC);
const _kCard = Colors.white;
const _kAccent = Color(0xFF2563EB);
const _kText = Color(0xFF111827);
const _kMuted = Color(0xFF6B7280);
const _kCardBorder = Color(0xFFE5E7EB);
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
  final TextEditingController _myDescriptionController = TextEditingController();
  final TextEditingController _friendDescriptionController =
      TextEditingController();
  final TextEditingController _friendLatController = TextEditingController();
  final TextEditingController _friendLngController = TextEditingController();

  GoogleMapController? _mapController;
  Map<String, dynamic>? _bestMatch;
  List<Map<String, dynamic>> _recommendations = const [];
  List<Map<String, double>> _lastParticipants = const [];
  bool _isLoading = false;
  String? _errorMessage;

  @override
  void initState() {
    super.initState();
    _friendLatController.text = widget.userPosition.latitude.toStringAsFixed(6);
    _friendLngController.text = widget.userPosition.longitude.toStringAsFixed(6);
    unawaited(_initNotifications());
  }

  @override
  void dispose() {
    _myDescriptionController.dispose();
    _friendDescriptionController.dispose();
    _friendLatController.dispose();
    _friendLngController.dispose();
    super.dispose();
  }

  String _backendBaseUrl() {
    return resolveBackendBaseUrl();
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
    final myDescription = _myDescriptionController.text.trim();
    final friendDescription = _friendDescriptionController.text.trim();
    if (myDescription.isEmpty || friendDescription.isEmpty) {
      setState(() {
        _errorMessage = 'Въведи и двете описания, за да намерим подходяща среща.';
      });
      return;
    }

    final friendLat = double.tryParse(_friendLatController.text.trim());
    final friendLng = double.tryParse(_friendLngController.text.trim());
    if (friendLat == null || friendLng == null) {
      setState(() {
        _errorMessage = 'Въведи валидни координати за приятеля.';
      });
      return;
    }

    setState(() {
      _isLoading = true;
      _errorMessage = null;
    });

    final payload = _buildRecommendationPayload(
      myDescription: myDescription,
      friendDescription: friendDescription,
      friendLat: friendLat,
      friendLng: friendLng,
    );
    _lastParticipants = (payload['participants'] as List)
        .whereType<Map>()
        .map(
          (row) => {
            'lat': (row['lat'] as num).toDouble(),
            'lng': (row['lng'] as num).toDouble(),
          },
        )
        .toList();

    try {
      final response = await http.post(
        Uri.parse('${_backendBaseUrl()}/api/meetup/recommend/'),
        headers: {'Content-Type': 'application/json'},
        body: jsonEncode(payload),
      );

      if (response.statusCode != 200) {
        final body = jsonDecode(response.body);
        final apiError =
            body is Map<String, dynamic> ? body['error'] as String? : null;
        setState(() {
          _errorMessage =
              apiError ?? 'Не успях да намеря подходящо място за среща.';
          _isLoading = false;
        });
        return;
      }

      final data = jsonDecode(response.body) as Map<String, dynamic>;
      final bestMatch = data['best_match'] as Map<String, dynamic>?;
      final recommendations = (data['recommendations'] as List? ?? const [])
          .whereType<Map>()
          .map((item) => item.cast<String, dynamic>())
          .toList();
      setState(() {
        _bestMatch = bestMatch;
        _recommendations = recommendations;
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
        _errorMessage = 'Няма връзка с услугата за срещи.';
        _isLoading = false;
      });
    }
  }

  Map<String, dynamic> _buildRecommendationPayload({
    required String myDescription,
    required String friendDescription,
    required double friendLat,
    required double friendLng,
  }) {
    final userLat = widget.userPosition.latitude;
    final userLng = widget.userPosition.longitude;
    return {
      'participants': [
        {'lat': userLat, 'lng': userLng},
        {'lat': friendLat, 'lng': friendLng},
      ],
      'participant_descriptions': [
        myDescription,
        friendDescription,
      ],
      'top_n': 5,
      'preferred_time': DateTime.now().add(const Duration(hours: 2)).toIso8601String(),
    };
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
          infoWindow: const InfoWindow(title: 'Ти'),
        ),
      );

    if (_lastParticipants.length > 1) {
      final friend = _lastParticipants[1];
      _markers.add(
        Marker(
          markerId: const MarkerId('demo_friend'),
          position: LatLng(friend['lat']!, friend['lng']!),
          icon: BitmapDescriptor.defaultMarkerWithHue(130),
          infoWindow: const InfoWindow(title: 'Приятел'),
        ),
      );
    }

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
          snippet: 'Предложена среща',
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
          data: Theme.of(context).copyWith(
            colorScheme: const ColorScheme.light(primary: _kAccent),
          ),
          child: child!,
        );
      },
    );

    if (selectedTime == null) return;

    await _scheduleNotification(selectedTime);
    if (!mounted) return;
    ScaffoldMessenger.of(context).showSnackBar(
      SnackBar(
        content: Text('Напомнянето е записано за ${selectedTime.format(context)}.'),
      ),
    );
  }

  Future<void> _scheduleNotification(TimeOfDay time) async {
    if (kIsWeb || _bestMatch == null) return;

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
    const platformDetails = NotificationDetails(android: androidDetails);

    final meetStartStr =
        '${scheduled.hour.toString().padLeft(2, '0')}:'
        '${scheduled.minute.toString().padLeft(2, '0')}';
    await _notificationsPlugin.show(
      id: 0,
      title: 'Напомняне за среща',
      body:
          'Имаш среща в ${_bestMatch!['place_name']} около $meetStartStr.',
      notificationDetails: platformDetails,
    );
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
        title: const Text('Планиране на среща'),
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
                _mapController = controller;
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
                Container(
                  padding: const EdgeInsets.all(12),
                  decoration: BoxDecoration(
                    color: const Color(0xFFF9FAFB),
                    borderRadius: BorderRadius.circular(12),
                    border: Border.all(color: _kCardBorder),
                  ),
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      const Text(
                        'Описание на двамата',
                        style: TextStyle(
                          color: _kText,
                          fontWeight: FontWeight.w700,
                        ),
                      ),
                      const SizedBox(height: 8),
                      TextField(
                        controller: _myDescriptionController,
                        minLines: 2,
                        maxLines: 3,
                        decoration: const InputDecoration(
                          labelText: 'Твоето описание',
                          hintText: 'Пример: Харесвам книги, музеи и спокойни разговори.',
                          border: OutlineInputBorder(),
                        ),
                      ),
                      const SizedBox(height: 8),
                      TextField(
                        controller: _friendDescriptionController,
                        minLines: 2,
                        maxLines: 3,
                        decoration: const InputDecoration(
                          labelText: 'Описание на приятеля',
                          hintText: 'Пример: Обича спорт, разходки и кафе.',
                          border: OutlineInputBorder(),
                        ),
                      ),
                      const SizedBox(height: 8),
                      Row(
                        children: [
                          Expanded(
                            child: TextField(
                              controller: _friendLatController,
                              keyboardType: const TextInputType.numberWithOptions(decimal: true, signed: true),
                              decoration: const InputDecoration(
                                labelText: 'Ширина на приятеля',
                                border: OutlineInputBorder(),
                              ),
                            ),
                          ),
                          const SizedBox(width: 8),
                          Expanded(
                            child: TextField(
                              controller: _friendLngController,
                              keyboardType: const TextInputType.numberWithOptions(decimal: true, signed: true),
                              decoration: const InputDecoration(
                                labelText: 'Дължина на приятеля',
                                border: OutlineInputBorder(),
                              ),
                            ),
                          ),
                        ],
                      ),
                    ],
                  ),
                ),
                const SizedBox(height: 14),
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
                    'Натисни бутона и ще предложа ден и час за среща.',
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
                    _bestMatch!['place_name'] as String? ?? 'Предложено място',
                    textAlign: TextAlign.center,
                    style: const TextStyle(
                      fontSize: 24,
                      fontWeight: FontWeight.w800,
                      color: _kText,
                    ),
                  ),
                  const SizedBox(height: 10),
                  Text(
                    (_bestMatch!['recommended_when_bg'] ?? _bestMatch!['recommended_time'] ?? '').toString(),
                    textAlign: TextAlign.center,
                    style: const TextStyle(
                      fontSize: 16,
                      fontWeight: FontWeight.w600,
                      color: _kAccent,
                    ),
                  ),
                  const SizedBox(height: 12),
                  Text(
                    'Ден: ${(_bestMatch!['recommended_day_bg'] ?? '').toString()} | Дата: ${(_bestMatch!['recommended_date_bg'] ?? '').toString()}',
                    textAlign: TextAlign.center,
                    style: const TextStyle(color: _kMuted, fontSize: 13),
                  ),
                  const SizedBox(height: 8),
                  TextButton.icon(
                    onPressed: _showReminderDialog,
                    icon: const Icon(Icons.alarm_add, color: _kAccent),
                    label: const Text(
                      'Задай напомняне',
                      style: TextStyle(color: _kAccent),
                    ),
                  ),
                  if (_recommendations.length > 1) ...[
                    const SizedBox(height: 12),
                    const Text(
                      'Още предложения',
                      style: TextStyle(fontWeight: FontWeight.w700, color: _kText),
                    ),
                    const SizedBox(height: 8),
                    ..._recommendations.skip(1).take(3).map(
                          (item) => Container(
                            margin: const EdgeInsets.only(bottom: 8),
                            padding: const EdgeInsets.all(10),
                            decoration: BoxDecoration(
                              borderRadius: BorderRadius.circular(10),
                              border: Border.all(color: _kCardBorder),
                            ),
                            child: Column(
                              crossAxisAlignment: CrossAxisAlignment.start,
                              children: [
                                Text(
                                  '${item['place_name']}',
                                  style: const TextStyle(
                                    color: _kText,
                                    fontWeight: FontWeight.w700,
                                  ),
                                ),
                                const SizedBox(height: 4),
                                Text(
                                  'Среща: ${(item['recommended_when_bg'] ?? item['recommended_time'] ?? '').toString()}',
                                  style: const TextStyle(color: _kMuted, fontSize: 12.5),
                                ),
                              ],
                            ),
                          ),
                        ),
                  ],
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
                            'Намери среща',
                            style: TextStyle(
                              fontSize: 17,
                              fontWeight: FontWeight.w700,
                            ),
                          ),
                  ),
                ),
                const SizedBox(height: 8),
                const Text(
                  'Резултатът е на български и показва основно кога е срещата.',
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
