import 'dart:convert';
import 'package:http/http.dart' as http;

/// All Django backend calls for the agent pipeline.
/// Throws [AgentApiException] on non-2xx responses.
class AgentClient {
  static const _requestTimeout = Duration(seconds: 90);

  AgentClient({required String baseUrl})
    : _base = baseUrl.endsWith('/')
          ? baseUrl.substring(0, baseUrl.length - 1)
          : baseUrl;

  final String _base;

  // ── Session lifecycle ──────────────────────────────────────────────────────

  Future<Map<String, dynamic>> createSession({
    String deviceId = 'flutter-test',
    String inputMode = 'text',
    String reasoningProvider = 'openai',
    List<String> supportedPackages = const [],
  }) async {
    return _post('/api/agent/sessions/', {
      'device_id': deviceId,
      'input_mode': inputMode,
      'reasoning_provider': reasoningProvider,
      'supported_packages': supportedPackages,
    });
  }

  Future<Map<String, dynamic>> submitCommand({
    required String prompt,
    String deviceId = 'flutter-test',
    String inputMode = 'text',
    String reasoningProvider = 'openai',
    List<String> supportedPackages = const [],
  }) => _post('/api/agent/command/', {
    'prompt': prompt,
    'device_id': deviceId,
    'input_mode': inputMode,
    'reasoning_provider': reasoningProvider,
    'supported_packages': supportedPackages,
  });

  Future<Map<String, dynamic>> startPhoneCommand({
    required String prompt,
    String deviceId = 'flutter-test',
    String inputMode = 'text',
    String reasoningProvider = 'openai',
    List<String> supportedPackages = const [],
  }) => _post('/api/agent/phone-command/', {
    'prompt': prompt,
    'device_id': deviceId,
    'input_mode': inputMode,
    'reasoning_provider': reasoningProvider,
    'supported_packages': supportedPackages,
  });

  Future<Map<String, dynamic>> prepareNavigation({
    required String prompt,
    String deviceId = 'flutter-test',
    List<String> supportedPackages = const [],
  }) => _post('/api/agent/navigation/prepare/', {
    'prompt': prompt,
    'device_id': deviceId,
    'supported_packages': supportedPackages,
  });

  Future<Map<String, dynamic>> pauseSession(String sessionId) =>
      _post('/api/agent/sessions/$sessionId/pause/', {});

  Future<Map<String, dynamic>> resumeSession(String sessionId) =>
      _post('/api/agent/sessions/$sessionId/resume/', {});

  Future<Map<String, dynamic>> cancelSession(String sessionId) =>
      _post('/api/agent/sessions/$sessionId/cancel/', {});

  Future<Map<String, dynamic>> getSession(String sessionId) =>
      _get('/api/agent/sessions/$sessionId/');

  // ── Intent & planning ──────────────────────────────────────────────────────

  Future<Map<String, dynamic>> submitIntent(
    String sessionId,
    String transcript,
  ) => _post('/api/agent/sessions/$sessionId/intent/', {
    'transcript': transcript,
  });

  Future<Map<String, dynamic>> submitPlan(
    String sessionId,
    Map<String, dynamic> plan,
  ) => _post('/api/agent/sessions/$sessionId/plan/', {'plan': plan});

  Future<Map<String, dynamic>> approvePlan(
    String sessionId, {
    String? planId,
    String confirmationMode = 'hard',
  }) => _post('/api/agent/sessions/$sessionId/approve/', {
    if (planId != null) 'plan_id': planId,
    'user_confirmation_mode': confirmationMode,
  });

  // ── Execution loop ─────────────────────────────────────────────────────────

  Future<Map<String, dynamic>> getNextStep(
    String sessionId, {
    Map<String, dynamic>? screenState,
  }) => _post('/api/agent/sessions/$sessionId/next-step/', {
    if (screenState != null) 'screen_state': screenState,
  });

  Future<Map<String, dynamic>> postActionResult(
    String sessionId, {
    required String actionId,
    required bool success,
    String code = '',
    String message = '',
    Map<String, dynamic>? screenState,
    int durationMs = 0,
    String actionType = '',
    String reasoning = '',
    String? screenshotBase64,
  }) => _post('/api/agent/sessions/$sessionId/action-result/', {
    'action_id': actionId,
    'result': {'success': success, 'code': code, 'message': message},
    if (screenState != null) 'screen_state': screenState,
    'duration_ms': durationMs,
    'executed_at': DateTime.now().toUtc().toIso8601String(),
    if (actionType.isNotEmpty) 'action_type': actionType,
    if (reasoning.isNotEmpty) 'reasoning': reasoning,
    if (screenshotBase64 != null) 'screenshot_b64': screenshotBase64,
  });

  // ── Confirmation ───────────────────────────────────────────────────────────

  Future<Map<String, dynamic>> getPendingConfirmation(String sessionId) =>
      _get('/api/agent/sessions/$sessionId/pending-confirmation/');

  Future<Map<String, dynamic>> approveConfirmation(String confirmationId) =>
      _post('/api/agent/confirmations/$confirmationId/approve/', {});

  Future<Map<String, dynamic>> rejectConfirmation(String confirmationId) =>
      _post('/api/agent/confirmations/$confirmationId/reject/', {});

  // ── Device bridge ──────────────────────────────────────────────────────────

  Future<Map<String, dynamic>> heartbeat(
    String sessionId, {
    int currentStep = 0,
    String foregroundPackage = '',
  }) => _post('/api/agent/device/heartbeat/', {
    'session_id': sessionId,
    'current_step': currentStep,
    'foreground_package': foregroundPackage,
  });

  // ── Private helpers ────────────────────────────────────────────────────────

  Future<Map<String, dynamic>> _post(
    String path,
    Map<String, dynamic> body,
  ) async {
    final uri = Uri.parse('$_base$path');
    final resp = await http
        .post(
          uri,
          headers: {'Content-Type': 'application/json'},
          body: jsonEncode(body),
        )
        .timeout(_requestTimeout);
    return _decode(resp);
  }

  Future<Map<String, dynamic>> _get(String path) async {
    final uri = Uri.parse('$_base$path');
    final resp = await http
        .get(uri, headers: {'Content-Type': 'application/json'})
        .timeout(_requestTimeout);
    return _decode(resp);
  }

  static Map<String, dynamic> _decode(http.Response resp) {
    final body = utf8.decode(resp.bodyBytes);
    if (resp.statusCode >= 200 && resp.statusCode < 300) {
      final decoded = jsonDecode(body);
      if (decoded is Map<String, dynamic>) return decoded;
      return {'data': decoded};
    }
    throw AgentApiException(resp.statusCode, body);
  }
}

class AgentApiException implements Exception {
  final int statusCode;
  final String body;
  const AgentApiException(this.statusCode, this.body);

  @override
  String toString() => 'AgentApiException($statusCode): $body';

  String get shortMessage {
    try {
      final m = jsonDecode(body);
      return m['detail']?.toString() ??
          body.substring(0, body.length.clamp(0, 120));
    } catch (_) {
      return body.substring(0, body.length.clamp(0, 120));
    }
  }
}
