import 'dart:async';
import 'dart:convert';

import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';
import 'package:flutter_local_notifications/flutter_local_notifications.dart';
import 'package:geolocator/geolocator.dart';
import 'package:google_maps_flutter/google_maps_flutter.dart';
import 'package:http/http.dart' as http;
import 'package:permission_handler/permission_handler.dart' as permission;
import 'package:shared_preferences/shared_preferences.dart';

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
  const MeetupScreen({
    super.key,
    required this.userPosition,
    this.accountToken,
  });

  final Position userPosition;
  final String? accountToken;

  @override
  State<MeetupScreen> createState() => _MeetupScreenState();
}

class _MeetupScreenState extends State<MeetupScreen> {
  static const _shownNotificationIdsKey =
      'hello_again.meetup.shown_notification_ids';
  final FlutterLocalNotificationsPlugin _notificationsPlugin =
      FlutterLocalNotificationsPlugin();
  final Set<Marker> _markers = {};

  GoogleMapController? _mapController;
  Map<String, dynamic>? _bestMatch;
  List<Map<String, dynamic>> _recommendations = const [];
  List<Map<String, double>> _lastParticipants = [];
  List<_FriendOption> _friends = const [];
  int? _selectedFriendUserId;
  bool _isLoadingFriends = true;
  bool _isLoading = false;
  bool _notificationsPermissionGranted = false;
  bool _notificationPermissionChecked = false;
  String? _errorMessage;
  Timer? _notificationsPollTimer;
  Set<int> _shownNotificationIds = <int>{};

  @override
  void initState() {
    super.initState();
    unawaited(_initNotifications());
    unawaited(_loadFriends());
  }

  @override
  void dispose() {
    _notificationsPollTimer?.cancel();
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
    final prefs = await SharedPreferences.getInstance();
    _shownNotificationIds =
        (prefs.getStringList(_shownNotificationIdsKey) ?? const [])
            .map(int.tryParse)
            .whereType<int>()
            .toSet();
    final granted = await _ensureNotificationPermission(
      androidImpl: androidImpl,
    );
    if (!mounted) return;
    setState(() {
      _notificationsPermissionGranted = granted;
      _notificationPermissionChecked = true;
    });
    if (granted) {
      _startNotificationsPolling();
      unawaited(_pollMeetupNotifications());
    }
  }

  Future<bool> _ensureNotificationPermission({
    AndroidFlutterLocalNotificationsPlugin? androidImpl,
  }) async {
    if (kIsWeb) return false;
    final status = await permission.Permission.notification.status;
    if (status.isGranted) {
      await androidImpl?.requestExactAlarmsPermission();
      return true;
    }
    final requested = await permission.Permission.notification.request();
    final pluginGranted =
        await androidImpl?.requestNotificationsPermission() ?? false;
    final granted = requested.isGranted || pluginGranted;
    if (granted) {
      await androidImpl?.requestExactAlarmsPermission();
    }
    return granted;
  }

  void _startNotificationsPolling() {
    _notificationsPollTimer?.cancel();
    _notificationsPollTimer = Timer.periodic(
      const Duration(seconds: 30),
      (_) => unawaited(_pollMeetupNotifications()),
    );
  }

  Future<void> _pollMeetupNotifications() async {
    final token = (widget.accountToken ?? '').trim();
    if (kIsWeb || !_notificationsPermissionGranted || token.isEmpty) {
      return;
    }
    try {
      final response = await http.get(
        Uri.parse('${_backendBaseUrl()}/api/meetup/friends/notifications/'),
        headers: _authHeaders(),
      );
      if (response.statusCode != 200) {
        return;
      }
      final data = jsonDecode(response.body) as Map<String, dynamic>;
      final notifications = (data['notifications'] as List? ?? const [])
          .whereType<Map>()
          .map((row) => row.cast<String, dynamic>())
          .toList();
      final dueReminders = (data['due_reminders'] as List? ?? const [])
          .whereType<Map>()
          .map((row) => row.cast<String, dynamic>())
          .toList();

      final deliverable = <Map<String, dynamic>>[
        ...notifications.where((item) => item['read_at'] == null),
        ...dueReminders.where((item) => item['read_at'] == null),
      ];
      final unseen = deliverable
          .where(
            (item) => !_shownNotificationIds.contains(_notificationId(item)),
          )
          .toList();
      if (unseen.isEmpty) {
        return;
      }

      final shownIds = <int>[];
      for (final item in unseen) {
        final notificationId = _notificationId(item);
        if (notificationId <= 0) {
          continue;
        }
        await _notificationsPlugin.show(
          id: notificationId,
          title: (item['title'] ?? 'Hello Again').toString(),
          body: (item['body'] ?? '').toString(),
          notificationDetails: _meetupNotificationDetails(),
        );
        shownIds.add(notificationId);
        _shownNotificationIds.add(notificationId);
      }

      if (shownIds.isNotEmpty) {
        final prefs = await SharedPreferences.getInstance();
        await prefs.setStringList(
          _shownNotificationIdsKey,
          _shownNotificationIds.map((item) => item.toString()).toList(),
        );
        await _markNotificationsRead(shownIds);
      }
    } catch (_) {}
  }

  int _notificationId(Map<String, dynamic> item) {
    return int.tryParse((item['id'] ?? '0').toString()) ?? 0;
  }

  NotificationDetails _meetupNotificationDetails() {
    const androidDetails = AndroidNotificationDetails(
      'meetup_channel',
      'Meetup Reminders',
      channelDescription: 'Meetup invites and reminders from Hello Again',
      importance: Importance.max,
      priority: Priority.high,
    );
    return const NotificationDetails(android: androidDetails);
  }

  Future<void> _markNotificationsRead(List<int> ids) async {
    if (ids.isEmpty) {
      return;
    }
    try {
      await http.post(
        Uri.parse('${_backendBaseUrl()}/api/meetup/friends/notifications/'),
        headers: _authHeaders(withJson: true),
        body: jsonEncode({'notification_ids': ids}),
      );
    } catch (_) {}
  }

  Map<String, String> _authHeaders({bool withJson = false}) {
    final headers = <String, String>{};
    if (withJson) {
      headers['Content-Type'] = 'application/json';
    }
    final token = (widget.accountToken ?? '').trim();
    if (token.isNotEmpty) {
      headers['Authorization'] = 'Token $token';
    }
    return headers;
  }

  Future<void> _loadFriends() async {
    setState(() {
      _isLoadingFriends = true;
      _errorMessage = null;
    });

    try {
      final response = await http.get(
        Uri.parse('${_backendBaseUrl()}/api/accounts/friends/'),
        headers: _authHeaders(),
      );

      if (response.statusCode != 200) {
        setState(() {
          _isLoadingFriends = false;
          _errorMessage =
              'Не успях да заредя приятелите. Влез отново в профила си.';
        });
        return;
      }

      final data = jsonDecode(response.body) as Map<String, dynamic>;
      final rows = (data['friends'] as List? ?? const [])
          .whereType<Map>()
          .map((row) => row.cast<String, dynamic>())
          .map(_FriendOption.fromJson)
          .toList();

      setState(() {
        _friends = rows;
        _selectedFriendUserId = rows.isNotEmpty ? rows.first.userId : null;
        _isLoadingFriends = false;
      });
    } catch (_) {
      setState(() {
        _isLoadingFriends = false;
        _errorMessage = 'Няма връзка със сървъра за приятели.';
      });
    }
  }

  Future<void> fetchRecommendation() async {
    final selectedFriendId = _selectedFriendUserId;
    if (selectedFriendId == null) {
      setState(() {
        _errorMessage = 'Избери приятел, за да намерим среща.';
      });
      return;
    }

    final selectedFriend = _friends.firstWhere(
      (item) => item.userId == selectedFriendId,
      orElse: () => _FriendOption.empty(),
    );

    setState(() {
      _isLoading = true;
      _errorMessage = null;
    });

    final payload = {
      'friend_user_id': selectedFriendId,
      'proposed_time': DateTime.now()
          .add(const Duration(hours: 2))
          .toIso8601String(),
      'requester_location': {
        'lat': widget.userPosition.latitude,
        'lng': widget.userPosition.longitude,
      },
      if (selectedFriend.homeLat != null && selectedFriend.homeLng != null)
        'friend_location': {
          'lat': selectedFriend.homeLat,
          'lng': selectedFriend.homeLng,
        },
    };

    try {
      final response = await http.post(
        Uri.parse('${_backendBaseUrl()}/api/meetup/friends/propose/'),
        headers: _authHeaders(withJson: true),
        body: jsonEncode(payload),
      );

      if (response.statusCode != 201 && response.statusCode != 200) {
        final body = jsonDecode(response.body);
        final apiError = body is Map<String, dynamic>
            ? (body['error'] as String? ?? body['message'] as String?)
            : null;
        setState(() {
          _errorMessage =
              apiError ?? 'Не успях да намеря подходящо място за среща.';
          _isLoading = false;
        });
        return;
      }

      final data = jsonDecode(response.body) as Map<String, dynamic>;
      final invite = data['invite'] as Map<String, dynamic>?;
      final payloadData = invite?['payload'] as Map<String, dynamic>?;
      final bestMatch = (payloadData?['best_match'] as Map?)
          ?.cast<String, dynamic>();
      final recommendations = bestMatch == null
          ? const <Map<String, dynamic>>[]
          : [bestMatch];

      _lastParticipants = [
        {
          'lat': widget.userPosition.latitude,
          'lng': widget.userPosition.longitude,
        },
        {
          'lat': selectedFriend.homeLat ?? widget.userPosition.latitude,
          'lng': selectedFriend.homeLng ?? widget.userPosition.longitude,
        },
      ];

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
    if (!_notificationsPermissionGranted) {
      final androidImpl = _notificationsPlugin
          .resolvePlatformSpecificImplementation<
            AndroidFlutterLocalNotificationsPlugin
          >();
      final granted = await _ensureNotificationPermission(
        androidImpl: androidImpl,
      );
      if (!mounted) return;
      setState(() {
        _notificationsPermissionGranted = granted;
        _notificationPermissionChecked = true;
      });
      if (!granted) {
        ScaffoldMessenger.of(context).showSnackBar(
          const SnackBar(
            content: Text(
              'Разреши известията на телефона, за да получаваш напомняния за срещи.',
            ),
          ),
        );
        return;
      }
      _startNotificationsPolling();
    }

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

    if (selectedTime == null) return;

    await _scheduleNotification(selectedTime);
    if (!mounted) return;
    ScaffoldMessenger.of(context).showSnackBar(
      SnackBar(
        content: Text(
          'Напомнянето е записано за ${selectedTime.format(context)}.',
        ),
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

    final meetStartStr =
        '${scheduled.hour.toString().padLeft(2, '0')}:'
        '${scheduled.minute.toString().padLeft(2, '0')}';
    await _notificationsPlugin.show(
      id: 0,
      title: 'Напомняне за среща',
      body: 'Имаш среща в ${_bestMatch!['place_name']} около $meetStartStr.',
      notificationDetails: _meetupNotificationDetails(),
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
                        'Среща с приятел от базата',
                        style: TextStyle(
                          color: _kText,
                          fontWeight: FontWeight.w700,
                        ),
                      ),
                      const SizedBox(height: 8),
                      if (_isLoadingFriends)
                        const Padding(
                          padding: EdgeInsets.symmetric(vertical: 8),
                          child: LinearProgressIndicator(minHeight: 2),
                        )
                      else if (_friends.isEmpty)
                        const Text(
                          'Нямаш приети приятели. Добави приятел, за да предложим среща.',
                          style: TextStyle(color: _kMuted),
                        )
                      else ...[
                        DropdownButtonFormField<int>(
                          initialValue: _selectedFriendUserId,
                          decoration: const InputDecoration(
                            labelText: 'Избери приятел',
                            border: OutlineInputBorder(),
                          ),
                          items: _friends
                              .map(
                                (friend) => DropdownMenuItem<int>(
                                  value: friend.userId,
                                  child: Text(friend.displayName),
                                ),
                              )
                              .toList(),
                          onChanged: (value) {
                            setState(() {
                              _selectedFriendUserId = value;
                            });
                          },
                        ),
                        const SizedBox(height: 8),
                        Builder(
                          builder: (context) {
                            final selected = _friends.where(
                              (friend) =>
                                  friend.userId == _selectedFriendUserId,
                            );
                            if (selected.isEmpty) {
                              return const SizedBox.shrink();
                            }
                            final friend = selected.first;
                            final description = friend.description.trim();
                            final locationText =
                                (friend.homeLat != null &&
                                    friend.homeLng != null)
                                ? 'Локация: ${friend.homeLat!.toStringAsFixed(5)}, ${friend.homeLng!.toStringAsFixed(5)}'
                                : 'Локация: липсва в профила на приятеля';
                            return Column(
                              crossAxisAlignment: CrossAxisAlignment.start,
                              children: [
                                if (description.isNotEmpty)
                                  Text(
                                    'Описание: $description',
                                    style: const TextStyle(
                                      color: _kMuted,
                                      fontSize: 12.5,
                                    ),
                                  ),
                                const SizedBox(height: 4),
                                Text(
                                  locationText,
                                  style: const TextStyle(
                                    color: _kMuted,
                                    fontSize: 12.5,
                                  ),
                                ),
                              ],
                            );
                          },
                        ),
                      ],
                    ],
                  ),
                ),
                const SizedBox(height: 14),
                if (_notificationPermissionChecked &&
                    !_notificationsPermissionGranted) ...[
                  Container(
                    padding: const EdgeInsets.all(12),
                    decoration: BoxDecoration(
                      color: const Color(0xFFFFF7ED),
                      borderRadius: BorderRadius.circular(12),
                      border: Border.all(color: const Color(0xFFFCD34D)),
                    ),
                    child: Row(
                      children: [
                        const Icon(
                          Icons.notifications_off_outlined,
                          color: Color(0xFFB45309),
                        ),
                        const SizedBox(width: 10),
                        const Expanded(
                          child: Text(
                            'Разреши известията, за да получаваш покани и напомняния за срещи на телефона.',
                            style: TextStyle(
                              color: Color(0xFF92400E),
                              fontSize: 12.5,
                            ),
                          ),
                        ),
                        TextButton(
                          onPressed: () async {
                            final androidImpl = _notificationsPlugin
                                .resolvePlatformSpecificImplementation<
                                  AndroidFlutterLocalNotificationsPlugin
                                >();
                            final granted = await _ensureNotificationPermission(
                              androidImpl: androidImpl,
                            );
                            if (!mounted) return;
                            setState(() {
                              _notificationsPermissionGranted = granted;
                              _notificationPermissionChecked = true;
                            });
                            if (granted) {
                              _startNotificationsPolling();
                              unawaited(_pollMeetupNotifications());
                            }
                          },
                          child: const Text('Разреши'),
                        ),
                      ],
                    ),
                  ),
                  const SizedBox(height: 14),
                ],
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
                    (_bestMatch!['recommended_when_bg'] ??
                            _bestMatch!['recommended_time'] ??
                            '')
                        .toString(),
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
                      style: TextStyle(
                        fontWeight: FontWeight.w700,
                        color: _kText,
                      ),
                    ),
                    const SizedBox(height: 8),
                    ..._recommendations
                        .skip(1)
                        .take(3)
                        .map(
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
                                  style: const TextStyle(
                                    color: _kMuted,
                                    fontSize: 12.5,
                                  ),
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
                            'Намери среща с приятел',
                            style: TextStyle(
                              fontSize: 17,
                              fontWeight: FontWeight.w700,
                            ),
                          ),
                  ),
                ),
                const SizedBox(height: 8),
                const Text(
                  'Срещата се изчислява по реални приятели, описания, предпочитания и локации от базата.',
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

class _FriendOption {
  const _FriendOption({
    required this.userId,
    required this.displayName,
    required this.description,
    required this.homeLat,
    required this.homeLng,
  });

  final int userId;
  final String displayName;
  final String description;
  final double? homeLat;
  final double? homeLng;

  factory _FriendOption.fromJson(Map<String, dynamic> json) {
    return _FriendOption(
      userId: int.tryParse((json['user_id'] ?? '0').toString()) ?? 0,
      displayName: (json['display_name'] ?? json['name'] ?? 'Приятел')
          .toString(),
      description: (json['description'] ?? '').toString(),
      homeLat: (json['home_lat'] as num?)?.toDouble(),
      homeLng: (json['home_lng'] as num?)?.toDouble(),
    );
  }

  factory _FriendOption.empty() {
    return const _FriendOption(
      userId: 0,
      displayName: '',
      description: '',
      homeLat: null,
      homeLng: null,
    );
  }
}
