import 'dart:convert';

import 'package:flutter/foundation.dart';
import 'package:http/http.dart' as http;
import 'package:shared_preferences/shared_preferences.dart';

const String defaultBackendUrl = 'http://10.0.2.2:8000';

String normalizeBaseUrl(String raw) {
  final trimmed = raw.trim();
  if (trimmed.isEmpty) {
    return defaultBackendUrl;
  }
  return trimmed.endsWith('/') ? trimmed.substring(0, trimmed.length - 1) : trimmed;
}

class ApiException implements Exception {
  const ApiException({
    required this.message,
    required this.statusCode,
    this.fieldErrors = const <String, List<String>>{},
  });

  final String message;
  final int statusCode;
  final Map<String, List<String>> fieldErrors;

  factory ApiException.fromResponse(http.Response response) {
    dynamic payload;
    try {
      payload = jsonDecode(response.body);
    } catch (_) {
      payload = null;
    }

    if (payload is Map<String, dynamic>) {
      return ApiException(
        message: (payload['message'] ?? 'Request failed.').toString(),
        statusCode: response.statusCode,
        fieldErrors: _normalizeErrors(payload['errors']),
      );
    }

    return ApiException(
      message: 'Request failed with status ${response.statusCode}.',
      statusCode: response.statusCode,
    );
  }

  static Map<String, List<String>> _normalizeErrors(dynamic raw) {
    if (raw is! Map) {
      return const <String, List<String>>{};
    }
    final normalized = <String, List<String>>{};
    for (final entry in raw.entries) {
      normalized[entry.key.toString()] = _collectMessages(entry.value);
    }
    return normalized;
  }

  static List<String> _collectMessages(dynamic value) {
    if (value is List) {
      return value.expand(_collectMessages).toList();
    }
    if (value is Map) {
      return value.entries
          .map((entry) => '${entry.key}: ${_collectMessages(entry.value).join(', ')}')
          .toList();
    }
    if (value == null) {
      return const <String>[];
    }
    return <String>[value.toString()];
  }

  String messageForField(String fieldName) {
    final errors = fieldErrors[fieldName];
    if (errors == null || errors.isEmpty) {
      return '';
    }
    return errors.join('\n');
  }

  @override
  String toString() => 'ApiException($statusCode, $message)';
}

double? _toDouble(dynamic value) {
  if (value is num) {
    return value.toDouble();
  }
  return null;
}

class MatchSummary {
  const MatchSummary({
    required this.compatibilityScore,
    required this.certaintyScore,
    required this.friendshipSummary,
    required this.whyTheyMatch,
    required this.possibleFriction,
    required this.sharedInterests,
  });

  final double compatibilityScore;
  final double certaintyScore;
  final String friendshipSummary;
  final List<String> whyTheyMatch;
  final List<String> possibleFriction;
  final List<String> sharedInterests;

  factory MatchSummary.fromJson(Map<String, dynamic> json) {
    return MatchSummary(
      compatibilityScore: _toDouble(json['compatibility_score']) ?? 0.0,
      certaintyScore: _toDouble(json['certainty_score']) ?? 0.0,
      friendshipSummary: (json['friendship_summary'] ?? '').toString(),
      whyTheyMatch: (json['why_they_match'] as List? ?? const <dynamic>[])
          .map((item) => item.toString())
          .toList(),
      possibleFriction: (json['possible_friction'] as List? ?? const <dynamic>[])
          .map((item) => item.toString())
          .toList(),
      sharedInterests: (json['shared_interests'] as List? ?? const <dynamic>[])
          .map((item) => item.toString())
          .toList(),
    );
  }
}

class TraitScore {
  const TraitScore({
    required this.label,
    required this.feature,
    required this.value,
    required this.confidence,
  });

  final String label;
  final String feature;
  final double value;
  final double confidence;

  factory TraitScore.fromJson(Map<String, dynamic> json) {
    return TraitScore(
      label: (json['label'] ?? json['feature'] ?? '').toString(),
      feature: (json['feature'] ?? '').toString(),
      value: _toDouble(json['value']) ?? 0.0,
      confidence: _toDouble(json['confidence']) ?? 0.0,
    );
  }
}

class ContactAccess {
  const ContactAccess({
    required this.canViewEmail,
    required this.canViewPhone,
  });

  final bool canViewEmail;
  final bool canViewPhone;

  factory ContactAccess.fromJson(Map<String, dynamic> json) {
    return ContactAccess(
      canViewEmail: json['can_view_email'] == true,
      canViewPhone: json['can_view_phone'] == true,
    );
  }
}

class AppProfile {
  const AppProfile({
    required this.userId,
    required this.username,
    required this.displayName,
    required this.description,
    required this.friendStatus,
    required this.topTraits,
    required this.matchedFromContacts,
    required this.graphScore,
    required this.contactAccess,
    required this.email,
    required this.phoneNumber,
    required this.matchSummary,
    required this.elderProfileId,
    required this.contactsPermissionGranted,
    required this.sharePhoneWithFriends,
    required this.shareEmailWithFriends,
    required this.onboardingAnswers,
    this.homeLat,
    this.homeLng,
    this.matchPercent,
    this.rawScore,
    this.scoreComponents = const <String, double>{},
    this.discoveryMode,
  });

  final int userId;
  final int? elderProfileId;
  final String username;
  final String displayName;
  final String description;
  final String friendStatus;
  final List<TraitScore> topTraits;
  final bool matchedFromContacts;
  final double? graphScore;
  final ContactAccess contactAccess;
  final String? email;
  final String? phoneNumber;
  final MatchSummary? matchSummary;
  final bool contactsPermissionGranted;
  final bool sharePhoneWithFriends;
  final bool shareEmailWithFriends;
  final Map<String, String> onboardingAnswers;
  final double? homeLat;
  final double? homeLng;
  final int? matchPercent;
  final double? rawScore;
  final Map<String, double> scoreComponents;
  final String? discoveryMode;

  factory AppProfile.fromJson(Map<String, dynamic> json) {
    final rawAnswers = json['onboarding_answers'];
    final parsedAnswers = <String, String>{};
    if (rawAnswers is Map) {
      for (final entry in rawAnswers.entries) {
        parsedAnswers[entry.key.toString()] = entry.value?.toString() ?? '';
      }
    }
    final rawScoreComponents = <String, double>{};
    final scoreComponentsPayload = json['score_components'];
    if (scoreComponentsPayload is Map) {
      for (final entry in scoreComponentsPayload.entries) {
        final parsed = _toDouble(entry.value);
        if (parsed != null) {
          rawScoreComponents[entry.key.toString()] = parsed;
        }
      }
    }

    final access = json['contact_access'];
    return AppProfile(
      userId: (json['user_id'] as num?)?.toInt() ?? 0,
      elderProfileId: (json['elder_profile_id'] as num?)?.toInt(),
      username: (json['username'] ?? '').toString(),
      displayName: (json['display_name'] ?? '').toString(),
      description: (json['description'] ?? '').toString(),
      friendStatus: (json['friend_status'] ?? 'none').toString(),
      topTraits: (json['top_traits'] as List? ?? const <dynamic>[])
          .whereType<Map>()
          .map((item) => TraitScore.fromJson(item.cast<String, dynamic>()))
          .toList(),
      matchedFromContacts: json['matched_from_contacts'] == true,
      graphScore: _toDouble(json['graph_score']),
      contactAccess: access is Map<String, dynamic>
          ? ContactAccess.fromJson(access)
          : const ContactAccess(canViewEmail: false, canViewPhone: false),
      email: json['email']?.toString(),
      phoneNumber: json['phone_number']?.toString(),
      matchSummary: json['match_summary'] is Map<String, dynamic>
          ? MatchSummary.fromJson(json['match_summary'] as Map<String, dynamic>)
          : null,
      contactsPermissionGranted: json['contacts_permission_granted'] == true,
      sharePhoneWithFriends: json['share_phone_with_friends'] != false,
      shareEmailWithFriends: json['share_email_with_friends'] != false,
      onboardingAnswers: parsedAnswers,
      homeLat: _toDouble(json['home_lat']),
      homeLng: _toDouble(json['home_lng']),
      matchPercent: (json['match_percent'] as num?)?.toInt(),
      rawScore: _toDouble(json['raw_score']),
      scoreComponents: rawScoreComponents,
      discoveryMode: json['discovery_mode']?.toString(),
    );
  }

  AppProfile copyWith({
    String? displayName,
    String? description,
    String? friendStatus,
    String? email,
    String? phoneNumber,
    bool? contactsPermissionGranted,
    bool? sharePhoneWithFriends,
    bool? shareEmailWithFriends,
    Map<String, String>? onboardingAnswers,
    double? homeLat,
    double? homeLng,
    int? matchPercent,
    double? rawScore,
    Map<String, double>? scoreComponents,
    String? discoveryMode,
  }) {
    return AppProfile(
      userId: userId,
      elderProfileId: elderProfileId,
      username: username,
      displayName: displayName ?? this.displayName,
      description: description ?? this.description,
      friendStatus: friendStatus ?? this.friendStatus,
      topTraits: topTraits,
      matchedFromContacts: matchedFromContacts,
      graphScore: graphScore,
      contactAccess: contactAccess,
      email: email ?? this.email,
      phoneNumber: phoneNumber ?? this.phoneNumber,
      matchSummary: matchSummary,
      contactsPermissionGranted:
          contactsPermissionGranted ?? this.contactsPermissionGranted,
      sharePhoneWithFriends:
          sharePhoneWithFriends ?? this.sharePhoneWithFriends,
      shareEmailWithFriends:
          shareEmailWithFriends ?? this.shareEmailWithFriends,
      onboardingAnswers: onboardingAnswers ?? this.onboardingAnswers,
      homeLat: homeLat ?? this.homeLat,
      homeLng: homeLng ?? this.homeLng,
      matchPercent: matchPercent ?? this.matchPercent,
      rawScore: rawScore ?? this.rawScore,
      scoreComponents: scoreComponents ?? this.scoreComponents,
      discoveryMode: discoveryMode ?? this.discoveryMode,
    );
  }
}

class MeetupInviteRow {
  const MeetupInviteRow({
    required this.id,
    required this.status,
    required this.direction,
    required this.requesterUserId,
    required this.requesterDisplayName,
    required this.invitedUserId,
    required this.invitedDisplayName,
    required this.proposedTime,
    required this.placeName,
    required this.placeLat,
    required this.placeLng,
    required this.weather,
    required this.temperature,
    required this.score,
    required this.payload,
  });

  final int id;
  final String status;
  final String direction;
  final int requesterUserId;
  final String requesterDisplayName;
  final int invitedUserId;
  final String invitedDisplayName;
  final DateTime? proposedTime;
  final String placeName;
  final double placeLat;
  final double placeLng;
  final String weather;
  final double? temperature;
  final double score;
  final Map<String, dynamic> payload;

  factory MeetupInviteRow.fromJson(Map<String, dynamic> json) {
    DateTime? proposedTime;
    final rawTime = json['proposed_time']?.toString();
    if (rawTime != null && rawTime.isNotEmpty) {
      proposedTime = DateTime.tryParse(rawTime);
    }

    final rawPayload = json['payload'];
    final parsedPayload = <String, dynamic>{};
    if (rawPayload is Map) {
      for (final entry in rawPayload.entries) {
        parsedPayload[entry.key.toString()] = entry.value;
      }
    }

    return MeetupInviteRow(
      id: (json['id'] as num?)?.toInt() ?? 0,
      status: (json['status'] ?? '').toString(),
      direction: (json['direction'] ?? '').toString(),
      requesterUserId: (json['requester_user_id'] as num?)?.toInt() ?? 0,
      requesterDisplayName: (json['requester_display_name'] ?? '').toString(),
      invitedUserId: (json['invited_user_id'] as num?)?.toInt() ?? 0,
      invitedDisplayName: (json['invited_display_name'] ?? '').toString(),
      proposedTime: proposedTime,
      placeName: (json['place_name'] ?? '').toString(),
      placeLat: _toDouble(json['place_lat']) ?? 0.0,
      placeLng: _toDouble(json['place_lng']) ?? 0.0,
      weather: (json['weather'] ?? '').toString(),
      temperature: _toDouble(json['temperature']),
      score: _toDouble(json['score']) ?? 0.0,
      payload: parsedPayload,
    );
  }
}

class MeetupInviteBucket {
  const MeetupInviteBucket({
    required this.incoming,
    required this.outgoing,
  });

  final List<MeetupInviteRow> incoming;
  final List<MeetupInviteRow> outgoing;

  factory MeetupInviteBucket.fromJson(Map<String, dynamic> json) {
    return MeetupInviteBucket(
      incoming: (json['incoming'] as List? ?? const <dynamic>[])
          .whereType<Map>()
          .map((item) => MeetupInviteRow.fromJson(item.cast<String, dynamic>()))
          .toList(),
      outgoing: (json['outgoing'] as List? ?? const <dynamic>[])
          .whereType<Map>()
          .map((item) => MeetupInviteRow.fromJson(item.cast<String, dynamic>()))
          .toList(),
    );
  }
}

class FriendRequestRow {
  const FriendRequestRow({
    required this.id,
    required this.status,
    required this.direction,
    required this.message,
    required this.counterparty,
  });

  final int id;
  final String status;
  final String direction;
  final String message;
  final AppProfile counterparty;

  factory FriendRequestRow.fromJson(Map<String, dynamic> json) {
    return FriendRequestRow(
      id: (json['id'] as num?)?.toInt() ?? 0,
      status: (json['status'] ?? '').toString(),
      direction: (json['direction'] ?? '').toString(),
      message: (json['message'] ?? '').toString(),
      counterparty:
          AppProfile.fromJson((json['counterparty'] as Map).cast<String, dynamic>()),
    );
  }
}

class FriendRequestBucket {
  const FriendRequestBucket({
    required this.incoming,
    required this.outgoing,
  });

  final List<FriendRequestRow> incoming;
  final List<FriendRequestRow> outgoing;

  factory FriendRequestBucket.fromJson(Map<String, dynamic> json) {
    return FriendRequestBucket(
      incoming: (json['incoming'] as List? ?? const <dynamic>[])
          .whereType<Map>()
          .map((item) => FriendRequestRow.fromJson(item.cast<String, dynamic>()))
          .toList(),
      outgoing: (json['outgoing'] as List? ?? const <dynamic>[])
          .whereType<Map>()
          .map((item) => FriendRequestRow.fromJson(item.cast<String, dynamic>()))
          .toList(),
    );
  }
}

class AuthResult {
  const AuthResult({
    required this.token,
    required this.profile,
  });

  final String token;
  final AppProfile profile;

  factory AuthResult.fromJson(Map<String, dynamic> json) {
    return AuthResult(
      token: (json['token'] ?? '').toString(),
      profile: AppProfile.fromJson((json['profile'] as Map).cast<String, dynamic>()),
    );
  }
}

class SocialApiClient {
  SocialApiClient({
    required this.baseUrl,
    this.token,
    http.Client? client,
  }) : _client = client ?? http.Client();

  final http.Client _client;
  String baseUrl;
  String? token;

  Uri _buildUri(String path, [Map<String, String>? queryParameters]) {
    final normalizedBase = normalizeBaseUrl(baseUrl);
    final uri = Uri.parse('$normalizedBase$path');
    if (queryParameters == null || queryParameters.isEmpty) {
      return uri;
    }
    return uri.replace(
      queryParameters: {
        ...uri.queryParameters,
        ...queryParameters,
      },
    );
  }

  Future<Map<String, dynamic>> _request(
    String method,
    String path, {
    Map<String, dynamic>? body,
    Map<String, String>? queryParameters,
  }) async {
    final request = http.Request(method, _buildUri(path, queryParameters))
      ..headers.addAll(
        <String, String>{
          'Accept': 'application/json',
          if (body != null) 'Content-Type': 'application/json',
          if (token != null && token!.isNotEmpty) 'Authorization': 'Token $token',
        },
      );
    if (body != null) {
      request.body = jsonEncode(body);
    }
    final response = await _client.send(request);

    final materialized = await http.Response.fromStream(response);
    if (materialized.statusCode < 200 || materialized.statusCode >= 300) {
      throw ApiException.fromResponse(materialized);
    }

    if (materialized.body.isEmpty) {
      return const <String, dynamic>{};
    }

    final payload = jsonDecode(materialized.body);
    if (payload is Map<String, dynamic>) {
      return payload;
    }
    return const <String, dynamic>{};
  }

  Future<AuthResult> register({
    required String username,
    required String displayName,
    required String email,
    required String password,
    required String phoneNumber,
    required String description,
  }) async {
    final payload = await _request(
      'POST',
      '/api/accounts/register/',
      body: <String, dynamic>{
        'username': username,
        'display_name': displayName,
        'email': email,
        'password': password,
        'phone_number': phoneNumber,
        'description': description,
      },
    );
    return AuthResult.fromJson(payload);
  }

  Future<AuthResult> login({
    required String identifier,
    required String password,
  }) async {
    final payload = await _request(
      'POST',
      '/api/accounts/login/',
      body: <String, dynamic>{
        'identifier': identifier,
        'password': password,
      },
    );
    return AuthResult.fromJson(payload);
  }

  Future<void> logout() async {
    await _request('POST', '/api/accounts/logout/');
  }

  Future<AppProfile> fetchMe() async {
    final payload = await _request('GET', '/api/accounts/me/');
    return AppProfile.fromJson((payload['profile'] as Map).cast<String, dynamic>());
  }

  Future<AppProfile> updateMe(Map<String, dynamic> body) async {
    final payload = await _request('PATCH', '/api/accounts/me/', body: body);
    return AppProfile.fromJson((payload['profile'] as Map).cast<String, dynamic>());
  }

  Future<List<AppProfile>> fetchDiscovery({String query = ''}) async {
    final payload = await _request(
      'GET',
      '/api/accounts/discovery/',
      queryParameters: query.trim().isEmpty ? null : <String, String>{'q': query.trim()},
    );
    return (payload['results'] as List? ?? const <dynamic>[])
        .whereType<Map>()
        .map((item) => AppProfile.fromJson(item.cast<String, dynamic>()))
        .toList();
  }

  Future<List<AppProfile>> fetchDescriptionDiscovery({
    required String description,
    int limit = 8,
  }) async {
    final payload = await _request(
      'POST',
      '/api/accounts/discovery/query/',
      body: <String, dynamic>{
        'description': description,
        'limit': limit,
      },
    );
    return (payload['results'] as List? ?? const <dynamic>[])
        .whereType<Map>()
        .map((item) => AppProfile.fromJson(item.cast<String, dynamic>()))
        .toList();
  }

  Future<FriendRequestBucket> fetchFriendRequests() async {
    final payload = await _request('GET', '/api/accounts/friend-requests/');
    return FriendRequestBucket.fromJson(payload);
  }

  Future<List<AppProfile>> fetchFriends() async {
    final payload = await _request('GET', '/api/accounts/friends/');
    return (payload['friends'] as List? ?? const <dynamic>[])
        .whereType<Map>()
        .map((item) => AppProfile.fromJson(item.cast<String, dynamic>()))
        .toList();
  }

  Future<AppProfile> fetchUser(int userId) async {
    final payload = await _request('GET', '/api/accounts/users/$userId/');
    return AppProfile.fromJson((payload['profile'] as Map).cast<String, dynamic>());
  }

  Future<FriendRequestRow> sendFriendRequest({
    required int targetUserId,
    String message = 'Let us connect on HelloAgain.',
  }) async {
    final payload = await _request(
      'POST',
      '/api/accounts/friend-requests/',
      body: <String, dynamic>{
        'target_user_id': targetUserId,
        'message': message,
      },
    );
    return FriendRequestRow.fromJson(
      (payload['friend_request'] as Map).cast<String, dynamic>(),
    );
  }

  Future<FriendRequestRow> respondToFriendRequest({
    required int requestId,
    required String action,
  }) async {
    final payload = await _request(
      'POST',
      '/api/accounts/friend-requests/$requestId/respond/',
      body: <String, dynamic>{'action': action},
    );
    return FriendRequestRow.fromJson(
      (payload['friend_request'] as Map).cast<String, dynamic>(),
    );
  }

  Future<void> importContacts(List<Map<String, String>> contacts) async {
    await _request(
      'POST',
      '/api/accounts/contacts/import/',
      body: <String, dynamic>{
        'source': 'device',
        'replace_existing': true,
        'contacts': contacts,
      },
    );
  }

  Future<void> logActivity({
    required String eventType,
    int? targetUserId,
    String discoveryMode = 'direct',
    String queryText = '',
    Map<String, dynamic> metadata = const <String, dynamic>{},
  }) async {
    final body = <String, dynamic>{
      'event_type': eventType,
      'discovery_mode': discoveryMode,
      'metadata': metadata,
    };
    if (targetUserId != null) {
      body['target_user_id'] = targetUserId;
    }
    if (queryText.trim().isNotEmpty) {
      body['query_text'] = queryText.trim();
    }
    await _request(
      'POST',
      '/api/accounts/activities/',
      body: body,
    );
  }

  Future<MeetupInviteRow> proposeMeetup({
    required AppProfile friend,
    DateTime? proposedTime,
    AppProfile? me,
  }) async {
    final body = <String, dynamic>{
      'friend_user_id': friend.userId,
      if (proposedTime != null) 'proposed_time': proposedTime.toIso8601String(),
    };
    if (me?.homeLat != null && me?.homeLng != null) {
      body['requester_location'] = <String, dynamic>{
        'lat': me!.homeLat,
        'lng': me.homeLng,
      };
    }
    if (friend.homeLat != null && friend.homeLng != null) {
      body['friend_location'] = <String, dynamic>{
        'lat': friend.homeLat,
        'lng': friend.homeLng,
      };
    }

    final payload = await _request(
      'POST',
      '/api/meetup/friends/propose/',
      body: body,
    );
    return MeetupInviteRow.fromJson(
      (payload['invite'] as Map).cast<String, dynamic>(),
    );
  }

  Future<MeetupInviteBucket> fetchMeetupInvites() async {
    final payload = await _request('GET', '/api/meetup/friends/invites/');
    return MeetupInviteBucket.fromJson(payload);
  }

  Future<MeetupInviteRow> respondMeetupInvite({
    required int inviteId,
    required String action,
  }) async {
    final payload = await _request(
      'POST',
      '/api/meetup/friends/invites/$inviteId/respond/',
      body: <String, dynamic>{'action': action},
    );
    return MeetupInviteRow.fromJson(
      (payload['invite'] as Map).cast<String, dynamic>(),
    );
  }
}

class SessionController extends ChangeNotifier {
  SessionController({
    required SharedPreferences preferences,
  })  : _preferences = preferences,
        baseUrl =
            normalizeBaseUrl(preferences.getString(_backendUrlKey) ?? defaultBackendUrl),
        contactsPromptSeen = preferences.getBool(_contactsPromptSeenKey) ?? false {
    api = SocialApiClient(
      baseUrl: baseUrl,
      token: preferences.getString(_tokenKey),
    );
    token = preferences.getString(_tokenKey);
  }

  static const String _backendUrlKey = 'backend_url';
  static const String _tokenKey = 'auth_token';
  static const String _contactsPromptSeenKey = 'contacts_prompt_seen';

  final SharedPreferences _preferences;
  late final SocialApiClient api;

  bool isBootstrapping = true;
  String baseUrl;
  String? token;
  AppProfile? me;
  bool contactsPromptSeen;

  Future<void> bootstrap() async {
    try {
      if (token != null && token!.isNotEmpty) {
        me = await api.fetchMe();
      }
    } on ApiException {
      await _clearSession(notifyListenersAfter: false);
    } finally {
      isBootstrapping = false;
      notifyListeners();
    }
  }

  Future<void> setBaseUrl(String value) async {
    baseUrl = normalizeBaseUrl(value);
    api.baseUrl = baseUrl;
    await _preferences.setString(_backendUrlKey, baseUrl);
    notifyListeners();
  }

  Future<void> login({
    required String identifier,
    required String password,
  }) async {
    final result = await api.login(identifier: identifier, password: password);
    await _storeAuthenticatedState(result);
  }

  Future<void> register({
    required String username,
    required String displayName,
    required String email,
    required String password,
    required String phoneNumber,
    required String description,
  }) async {
    final result = await api.register(
      username: username,
      displayName: displayName,
      email: email,
      password: password,
      phoneNumber: phoneNumber,
      description: description,
    );
    await _storeAuthenticatedState(result);
  }

  Future<void> refreshMe() async {
    me = await api.fetchMe();
    notifyListeners();
  }

  Future<AppProfile> updateMe(Map<String, dynamic> body) async {
    me = await api.updateMe(body);
    notifyListeners();
    return me!;
  }

  Future<void> logout() async {
    try {
      await api.logout();
    } catch (_) {
      // Keep logout resilient even if the backend is unavailable.
    }
    await _clearSession();
  }

  Future<void> markContactsPromptSeen() async {
    contactsPromptSeen = true;
    await _preferences.setBool(_contactsPromptSeenKey, true);
    notifyListeners();
  }

  Future<void> resetContactsPrompt() async {
    contactsPromptSeen = false;
    await _preferences.setBool(_contactsPromptSeenKey, false);
    notifyListeners();
  }

  Future<void> _storeAuthenticatedState(AuthResult result) async {
    token = result.token;
    me = result.profile;
    api.token = result.token;
    contactsPromptSeen = false;
    await _preferences.setString(_tokenKey, result.token);
    await _preferences.setBool(_contactsPromptSeenKey, false);
    notifyListeners();
  }

  Future<void> _clearSession({bool notifyListenersAfter = true}) async {
    token = null;
    me = null;
    api.token = null;
    contactsPromptSeen = false;
    await _preferences.remove(_tokenKey);
    await _preferences.setBool(_contactsPromptSeenKey, false);
    if (notifyListenersAfter) {
      notifyListeners();
    }
  }
}
