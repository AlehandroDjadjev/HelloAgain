import 'package:android_control_plugin/android_control_plugin.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:frontend/src/api/agent_client.dart';
import 'package:frontend/src/native/mic_bridge_client.dart';
import 'package:frontend/src/pipeline/orchestrator.dart';
import 'package:frontend/src/pipeline/pipeline_state.dart';
import 'package:frontend/src/pipeline/step_runner.dart';

class _FakeOrchestratorClient extends AgentClient {
  _FakeOrchestratorClient() : super(baseUrl: 'http://test');

  String? lastQueryId;
  String? lastTranscript;
  int submitUserInputCalls = 0;
  Map<String, dynamic> submitUserInputResponse = {
    'status': 'resolved',
    'resolved': true,
    'attempt': 1,
    'entity_updates': {'recipient': 'Alex Johnson'},
    'resolved_values': {'recipient': 'Alex Johnson'},
    'missing_fields': const <String>[],
    'matched_candidate_id': 'candidate_2',
    'reason_unresolved': '',
    'followup_question': '',
    'should_fallback': false,
  };

  @override
  Future<Map<String, dynamic>> submitUserInput(
    String sessionId, {
    required String queryId,
    required String transcript,
    String source = 'voice',
  }) async {
    submitUserInputCalls++;
    lastQueryId = queryId;
    lastTranscript = transcript;
    return submitUserInputResponse;
  }
}

class _FakeGateway extends DeviceControlChannel {
  const _FakeGateway();

  @override
  Future<ActionResult> startSession(SessionConfig config) async =>
      const ActionResult(success: true, code: 'OK');

  @override
  Future<ActionResult> stopSession(String sessionId) async =>
      const ActionResult(success: true, code: 'OK');
}

class _FakeMicBridgeClient extends MicBridgeClient {
  _FakeMicBridgeClient() : super(baseUrl: 'http://test');

  int ensureServiceReadyCalls = 0;
  int beginOneShotQueryCalls = 0;
  int cancelListeningCalls = 0;
  String? lastQuestion;
  MicBridgeQueryResult? nextResult = const MicBridgeQueryResult(
    requestId: 'request-1',
    transcript: 'Alex Johnson',
  );

  @override
  Stream<MicBridgeEvent> get events => const Stream<MicBridgeEvent>.empty();

  @override
  Future<void> ensureServiceReady() async {
    ensureServiceReadyCalls++;
  }

  @override
  Future<MicBridgeQueryResult?> beginOneShotQuery(
    String question, {
    Duration timeout = const Duration(seconds: 18),
  }) async {
    beginOneShotQueryCalls++;
    lastQuestion = question;
    return nextResult;
  }

  @override
  Future<void> cancelListening({String? requestId}) async {
    cancelListeningCalls++;
  }

  @override
  Future<void> dispose() async {}
}

class _ScriptedStepRunner extends StepRunner {
  _ScriptedStepRunner({
    required super.client,
    required super.gateway,
    required super.sessionId,
    required super.expectedPackage,
    required super.onStepStarted,
    required super.onStepCompleted,
    required super.onLog,
    required super.onConfirmation,
    required super.onUserInputRequested,
    required super.onComplete,
    required super.onAbort,
    required super.onManualTakeover,
    required super.onUnexpectedAppChange,
    required this.clarificationAction,
  });

  final Map<String, dynamic> clarificationAction;
  int runLoopCalls = 0;

  @override
  Future<void> runLoop() async {
    runLoopCalls++;
    if (runLoopCalls == 1) {
      await onUserInputRequested(clarificationAction);
      return;
    }
    onComplete();
  }
}

void main() {
  test('PipelineOrchestrator resumes after clarification reply resolves', () async {
    final client = _FakeOrchestratorClient();
    final micBridgeClient = _FakeMicBridgeClient();
    late _ScriptedStepRunner runner;

    final orchestrator = PipelineOrchestrator(
      client: client,
      gateway: const _FakeGateway(),
      micBridgeClient: micBridgeClient,
      stepRunnerFactory: ({
        required client,
        required gateway,
        required sessionId,
        required expectedPackage,
        required onStepStarted,
        required onStepCompleted,
        required onLog,
        required onConfirmation,
        required onUserInputRequested,
        required onComplete,
        required onAbort,
        required onManualTakeover,
        required onUnexpectedAppChange,
      }) {
        runner = _ScriptedStepRunner(
          client: client,
          gateway: gateway,
          sessionId: sessionId,
          expectedPackage: expectedPackage,
          onStepStarted: onStepStarted,
          onStepCompleted: onStepCompleted,
          onLog: onLog,
          onConfirmation: onConfirmation,
          onUserInputRequested: onUserInputRequested,
          onComplete: onComplete,
          onAbort: onAbort,
          onManualTakeover: onManualTakeover,
          onUnexpectedAppChange: onUnexpectedAppChange,
          clarificationAction: {
            'id': 'query_123',
            'type': 'REQUEST_USER_INPUT',
            'params': {
              'query_id': 'query_123',
              'question': 'Which Alex should I message?',
              'required_fields': ['recipient'],
              'candidates': ['Alex Chen', 'Alex Johnson'],
              'attempt': 1,
              'max_attempts': 3,
              'reason': 'multiple_visible_matches',
            },
          },
        );
        return runner;
      },
    );

    orchestrator.sessionId = 'session-1';
    orchestrator.parsedIntent = {'app_package': 'com.whatsapp'};
    micBridgeClient.nextResult = const MicBridgeQueryResult(
      requestId: 'request-1',
      transcript: 'Alex Johnson',
    );
    micBridgeClient.lastQuestion = null;

    await orchestrator.executePrepared();

    expect(orchestrator.phase, PipelinePhase.completed);
    expect(orchestrator.pendingUserInput, isNull);
    expect(client.submitUserInputCalls, 1);
    expect(client.lastQueryId, 'query_123');
    expect(client.lastTranscript, 'Alex Johnson');
    expect(micBridgeClient.beginOneShotQueryCalls, 1);
    expect(micBridgeClient.lastQuestion, 'Which Alex should I message?');
    expect(runner.runLoopCalls, 2);
  });

  test('PipelineOrchestrator fails if clarification follow-up does not advance', () async {
    final client = _FakeOrchestratorClient()
      ..submitUserInputResponse = {
        'status': 'needs_user_input',
        'query_id': 'query_123',
        'question': 'Which Alex should I message?',
        'followup_question': 'Which Alex should I message again?',
        'required_fields': ['recipient'],
        'candidates': ['Alex Chen', 'Alex Johnson'],
        'attempt': 1,
        'max_attempts': 3,
        'reason': 'multiple_visible_matches',
        'why_unresolved': 'multiple_matches',
      };
    final micBridgeClient = _FakeMicBridgeClient();
    late _ScriptedStepRunner runner;

    final orchestrator = PipelineOrchestrator(
      client: client,
      gateway: const _FakeGateway(),
      micBridgeClient: micBridgeClient,
      stepRunnerFactory: ({
        required client,
        required gateway,
        required sessionId,
        required expectedPackage,
        required onStepStarted,
        required onStepCompleted,
        required onLog,
        required onConfirmation,
        required onUserInputRequested,
        required onComplete,
        required onAbort,
        required onManualTakeover,
        required onUnexpectedAppChange,
      }) {
        runner = _ScriptedStepRunner(
          client: client,
          gateway: gateway,
          sessionId: sessionId,
          expectedPackage: expectedPackage,
          onStepStarted: onStepStarted,
          onStepCompleted: onStepCompleted,
          onLog: onLog,
          onConfirmation: onConfirmation,
          onUserInputRequested: onUserInputRequested,
          onComplete: onComplete,
          onAbort: onAbort,
          onManualTakeover: onManualTakeover,
          onUnexpectedAppChange: onUnexpectedAppChange,
          clarificationAction: {
            'id': 'query_123',
            'type': 'REQUEST_USER_INPUT',
            'params': {
              'query_id': 'query_123',
              'question': 'Which Alex should I message?',
              'required_fields': ['recipient'],
              'candidates': ['Alex Chen', 'Alex Johnson'],
              'attempt': 1,
              'max_attempts': 3,
              'reason': 'multiple_visible_matches',
            },
          },
        );
        return runner;
      },
    );

    orchestrator.sessionId = 'session-1';
    orchestrator.parsedIntent = {'app_package': 'com.whatsapp'};
    micBridgeClient.nextResult = const MicBridgeQueryResult(
      requestId: 'request-2',
      transcript: 'Alex',
    );

    await orchestrator.executePrepared();

    expect(orchestrator.phase, PipelinePhase.failed);
    expect(
      orchestrator.errorMessage,
      contains('follow-up attempt did not advance'),
    );
    expect(runner.runLoopCalls, 1);
  });
}
