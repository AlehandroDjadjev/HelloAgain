import 'package:android_control_plugin/android_control_plugin.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:frontend/src/api/agent_client.dart';
import 'package:frontend/src/pipeline/step_runner.dart';

class _FakeAgentClient extends AgentClient {
  _FakeAgentClient({required this.nextStepResponse}) : super(baseUrl: 'http://test');

  final Map<String, dynamic> nextStepResponse;

  @override
  Future<Map<String, dynamic>> getNextStep(
    String sessionId, {
    Map<String, dynamic>? screenState,
  }) async => nextStepResponse;
}

class _FakeGateway extends DeviceControlChannel {
  const _FakeGateway();

  @override
  Future<ScreenState> getScreenState() async => ScreenState(
    timestampMs: 1,
    foregroundPackage: 'com.whatsapp',
    windowTitle: 'WhatsApp',
    screenHash: 'screen-1',
    isSensitive: false,
    nodes: const [],
  );

  @override
  Future<ActionResult> tapElement(Selector selector) async {
    throw UnimplementedError('tapElement should not run in this test');
  }
}

void main() {
  test('StepRunner routes needs_user_input to clarification callback', () async {
    Map<String, dynamic>? requestedAction;
    var startedSteps = 0;

    final runner = StepRunner(
      client: _FakeAgentClient(
        nextStepResponse: {
          'status': 'needs_user_input',
          'reasoning': 'Two visible matches require clarification.',
          'next_action': {
            'id': 'query_123',
            'type': 'REQUEST_USER_INPUT',
            'params': {
              'query_id': 'query_123',
              'question': 'Which Alex should I message?',
              'required_fields': ['recipient'],
              'candidates': ['Alex Chen', 'Alex Johnson'],
              'attempt': 1,
              'max_attempts': 3,
            },
          },
        },
      ),
      gateway: const _FakeGateway(),
      sessionId: 'session-1',
      expectedPackage: 'com.whatsapp',
      onStepStarted: (_) => startedSteps++,
      onStepCompleted: (stepId, result) {},
      onLog: (message, level) {},
      onConfirmation: (_) async {},
      onUserInputRequested: (action) async {
        requestedAction = action;
      },
      onComplete: () {},
      onAbort: (_) {},
      onManualTakeover: (_) {},
      onUnexpectedAppChange: (_) {},
    );

    await runner.runLoop();

    expect(startedSteps, 0);
    expect(requestedAction?['id'], 'query_123');
    expect(
      (requestedAction?['params'] as Map<String, dynamic>)['question'],
      'Which Alex should I message?',
    );
  });

  test(
    'StepRunner does not execute REQUEST_USER_INPUT when backend status is execute',
    () async {
      Map<String, dynamic>? requestedAction;
      var startedSteps = 0;

      final runner = StepRunner(
        client: _FakeAgentClient(
          nextStepResponse: {
            'status': 'execute',
            'next_action': {
              'id': 'query_123',
              'type': 'REQUEST_USER_INPUT',
              'params': {
                'query_id': 'query_123',
                'question': 'Which Alex should I message?',
              },
            },
          },
        ),
        gateway: const _FakeGateway(),
        sessionId: 'session-1',
        expectedPackage: 'com.whatsapp',
        onStepStarted: (_) => startedSteps++,
        onStepCompleted: (stepId, result) {},
        onLog: (message, level) {},
        onConfirmation: (_) async {},
        onUserInputRequested: (action) async {
          requestedAction = action;
        },
        onComplete: () {},
        onAbort: (_) {},
        onManualTakeover: (_) {},
        onUnexpectedAppChange: (_) {},
      );

      await runner.runLoop();

      expect(startedSteps, 0);
      expect(requestedAction?['type'], 'REQUEST_USER_INPUT');
    },
  );
}
