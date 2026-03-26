import 'dart:math' as math;

/// Builds valid action-plan JSON for known apps.
///
/// Every plan produced here satisfies the Pydantic ActionPlan schema:
///   - All steps are typed
///   - REQUEST_CONFIRMATION immediately precedes any step with requires_confirmation=true
///   - Sensitivity is set appropriately
class PlanBuilder {
  PlanBuilder._();

  /// Detect which app the user is targeting based on intent data.
  static String detectApp(Map<String, dynamic> intent) {
    return (intent['app_package'] as String?) ?? '';
  }

  /// Build a plan for the given session, intent and optional extra params.
  /// Returns null if the app is not recognised.
  static Map<String, dynamic>? build({
    required String sessionId,
    required Map<String, dynamic> intent,
    Map<String, dynamic> extras = const {},
  }) {
    final pkg = detectApp(intent);
    final planId = _uuid();
    final goal = (intent['goal'] as String?) ?? extras['command'] as String? ?? 'Unknown';

    return switch (pkg) {
      'com.whatsapp' => _whatsapp(
          sessionId: sessionId,
          planId: planId,
          goal: goal,
          recipient: extras['recipient'] as String? ??
              (intent['entities'] as Map?)?['recipient'] as String? ??
              'Test Contact',
          message: extras['message'] as String? ?? 'Hello from HelloAgain',
        ),
      'com.google.android.apps.maps' => _maps(
          sessionId: sessionId,
          planId: planId,
          goal: goal,
          destination: extras['destination'] as String? ?? 'Central Park',
        ),
      'com.google.android.gm' => _gmail(
          sessionId: sessionId,
          planId: planId,
          goal: goal,
          to: extras['to'] as String? ?? 'test@example.com',
          subject: extras['subject'] as String? ?? 'Hello',
          body: extras['body'] as String? ?? 'Sent via HelloAgain',
        ),
      'com.android.chrome' => _chrome(
          sessionId: sessionId,
          planId: planId,
          goal: goal,
          query: extras['query'] as String? ?? goal,
        ),
      _ => null,
    };
  }

  // ── WhatsApp send message ────────────────────────────────────────────────

  static Map<String, dynamic> _whatsapp({
    required String sessionId,
    required String planId,
    required String goal,
    required String recipient,
    required String message,
  }) => {
        'plan_id': planId,
        'session_id': sessionId,
        'goal': goal,
        'app_package': 'com.whatsapp',
        'version': 1,
        'steps': [
          _step('wa_1', 'OPEN_APP', {'package': 'com.whatsapp'},
              hint: 'chat_list', sensitivity: 'low'),
          _step('wa_2', 'WAIT_FOR_APP',
              {'package': 'com.whatsapp', 'timeoutMs': 6000},
              hint: 'chat_list', sensitivity: 'low'),
          _step('wa_3', 'TAP_ELEMENT',
              {'selector': _sel(contentDescContains: 'Search')},
              hint: 'search_open', sensitivity: 'low'),
          _step('wa_4', 'TYPE_TEXT', {'text': recipient},
              hint: 'search_results', sensitivity: 'low'),
          _step(
              'wa_5',
              'TAP_ELEMENT',
              {
                'selector': _sel(
                    className: 'android.widget.TextView', indexInParent: 0)
              },
              hint: 'chat_open',
              sensitivity: 'low'),
          _step('wa_6', 'TYPE_TEXT', {'text': message},
              hint: 'message_typed', sensitivity: 'medium'),
          // REQUEST_CONFIRMATION must immediately precede the step that requires confirmation
          _step('wa_7', 'REQUEST_CONFIRMATION',
              {'message': 'Send "$message" to $recipient on WhatsApp?'},
              hint: 'confirmation_shown', sensitivity: 'medium'),
          _step('wa_8', 'TAP_ELEMENT',
              {'selector': _sel(contentDescContains: 'Send')},
              hint: 'message_sent',
              sensitivity: 'high',
              requiresConfirmation: true),
        ],
      };

  // ── Google Maps navigate ─────────────────────────────────────────────────

  static Map<String, dynamic> _maps({
    required String sessionId,
    required String planId,
    required String goal,
    required String destination,
  }) => {
        'plan_id': planId,
        'session_id': sessionId,
        'goal': goal,
        'app_package': 'com.google.android.apps.maps',
        'version': 1,
        'steps': [
          _step('maps_1', 'OPEN_APP',
              {'package': 'com.google.android.apps.maps'},
              hint: 'map_view', sensitivity: 'low'),
          _step('maps_2', 'WAIT_FOR_APP',
              {'package': 'com.google.android.apps.maps', 'timeoutMs': 6000},
              hint: 'map_view', sensitivity: 'low'),
          _step('maps_3', 'TAP_ELEMENT',
              {'selector': _sel(contentDescContains: 'Search')},
              hint: 'search_open', sensitivity: 'low'),
          _step('maps_4', 'TYPE_TEXT', {'text': destination},
              hint: 'search_results', sensitivity: 'low'),
          _step('maps_5', 'TAP_ELEMENT',
              {'selector': _sel(textContains: destination)},
              hint: 'destination_selected', sensitivity: 'low'),
          _step('maps_6', 'TAP_ELEMENT',
              {'selector': _sel(contentDescContains: 'Directions')},
              hint: 'directions_shown', sensitivity: 'low'),
        ],
      };

  // ── Gmail compose ────────────────────────────────────────────────────────

  static Map<String, dynamic> _gmail({
    required String sessionId,
    required String planId,
    required String goal,
    required String to,
    required String subject,
    required String body,
  }) => {
        'plan_id': planId,
        'session_id': sessionId,
        'goal': goal,
        'app_package': 'com.google.android.gm',
        'version': 1,
        'steps': [
          _step('gm_1', 'OPEN_APP', {'package': 'com.google.android.gm'},
              hint: 'inbox', sensitivity: 'low'),
          _step('gm_2', 'WAIT_FOR_APP',
              {'package': 'com.google.android.gm', 'timeoutMs': 6000},
              hint: 'inbox', sensitivity: 'low'),
          _step('gm_3', 'TAP_ELEMENT',
              {'selector': _sel(contentDescContains: 'Compose')},
              hint: 'compose_open', sensitivity: 'low'),
          _step('gm_4', 'TAP_ELEMENT',
              {'selector': _sel(contentDescContains: 'To')},
              hint: 'to_focused', sensitivity: 'low'),
          _step('gm_5', 'TYPE_TEXT', {'text': to},
              hint: 'to_filled', sensitivity: 'low'),
          _step('gm_6', 'TAP_ELEMENT',
              {'selector': _sel(contentDescContains: 'Subject')},
              hint: 'subject_focused', sensitivity: 'low'),
          _step('gm_7', 'TYPE_TEXT', {'text': subject},
              hint: 'subject_filled', sensitivity: 'low'),
          _step('gm_8', 'TAP_ELEMENT',
              {'selector': _sel(contentDescContains: 'Compose email')},
              hint: 'body_focused', sensitivity: 'low'),
          _step('gm_9', 'TYPE_TEXT', {'text': body},
              hint: 'body_filled', sensitivity: 'medium'),
          _step('gm_10', 'REQUEST_CONFIRMATION',
              {'message': 'Send email to $to with subject "$subject"?'},
              hint: 'confirmation_shown', sensitivity: 'medium'),
          _step('gm_11', 'TAP_ELEMENT',
              {'selector': _sel(contentDescContains: 'Send')},
              hint: 'email_sent',
              sensitivity: 'high',
              requiresConfirmation: true),
        ],
      };

  // ── Chrome search ─────────────────────────────────────────────────────────

  static Map<String, dynamic> _chrome({
    required String sessionId,
    required String planId,
    required String goal,
    required String query,
  }) => {
        'plan_id': planId,
        'session_id': sessionId,
        'goal': goal,
        'app_package': 'com.android.chrome',
        'version': 1,
        'steps': [
          _step('ch_1', 'OPEN_APP', {'package': 'com.android.chrome'},
              hint: 'browser_open', sensitivity: 'low'),
          _step('ch_2', 'WAIT_FOR_APP',
              {'package': 'com.android.chrome', 'timeoutMs': 6000},
              hint: 'browser_open', sensitivity: 'low'),
          _step('ch_3', 'TAP_ELEMENT',
              {'selector': _sel(contentDescContains: 'Search')},
              hint: 'omnibox_focused', sensitivity: 'low'),
          _step('ch_4', 'TYPE_TEXT', {'text': query},
              hint: 'query_typed', sensitivity: 'low'),
          _step('ch_5', 'TAP_ELEMENT',
              {'selector': _sel(contentDescContains: 'Search', clickable: true)},
              hint: 'results_shown', sensitivity: 'low'),
        ],
      };

  // ── Step helpers ─────────────────────────────────────────────────────────

  static Map<String, dynamic> _step(
    String id,
    String type,
    Map<String, dynamic> params, {
    required String hint,
    required String sensitivity,
    bool requiresConfirmation = false,
  }) => {
        'id': id,
        'type': type,
        'params': params,
        'expected_outcome': {'screen_hint': hint},
        'timeout_ms': type == 'WAIT_FOR_APP' ? 6000 : 5000,
        'retry_policy': {'max_attempts': 2},
        'sensitivity': sensitivity,
        'requires_confirmation': requiresConfirmation,
      };

  static Map<String, dynamic> _sel({
    String? viewId,
    String? textEquals,
    String? textContains,
    String? contentDescEquals,
    String? contentDescContains,
    String? className,
    bool? clickable,
    int? indexInParent,
  }) => {
        if (viewId != null) 'view_id': viewId,
        if (textEquals != null) 'text': textEquals,
        if (textContains != null) 'text_contains': textContains,
        if (contentDescEquals != null) 'content_desc': contentDescEquals,
        if (contentDescContains != null)
          'content_desc_contains': contentDescContains,
        if (className != null) 'class_name': className,
        if (clickable != null) 'clickable': clickable,
        if (indexInParent != null) 'index_in_parent': indexInParent,
      };

  static String _uuid() {
    final r = math.Random();
    String h(int n) => r.nextInt(n).toRadixString(16).padLeft(4, '0');
    return '${h(65536)}${h(65536)}-${h(65536)}-4${h(4096).substring(1)}'
        '-${(8 + r.nextInt(4)).toRadixString(16)}${h(4096).substring(1)}'
        '-${h(65536)}${h(65536)}${h(65536)}';
  }
}
