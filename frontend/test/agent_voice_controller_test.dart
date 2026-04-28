import 'package:flutter_test/flutter_test.dart';
import 'package:frontend/src/api/voice_gateway_client.dart';
import 'package:frontend/src/voice/agent_voice_controller.dart';
import 'package:frontend/src/voice/background_voice_service.dart';

class _FakeBackgroundVoiceService extends BackgroundVoiceService {
  String? question;
  String? baseUrl;
  String? userId;
  String? sessionId;
  String? language;
  Duration? timeout;
  String? transcriptToReturn;

  @override
  Future<String?> runSingleTurn({
    required String question,
    required String baseUrl,
    required String userId,
    required String sessionId,
    required String language,
    Duration timeout = const Duration(seconds: 16),
  }) async {
    this.question = question;
    this.baseUrl = baseUrl;
    this.userId = userId;
    this.sessionId = sessionId;
    this.language = language;
    this.timeout = timeout;
    return transcriptToReturn;
  }
}

void main() {
  test('AgentVoiceController uses background voice service for single-turn reply', () async {
    final backgroundService = _FakeBackgroundVoiceService()
      ..transcriptToReturn = 'pencho@example.com';
    final controller = AgentVoiceController(
      client: VoiceGatewayClient(baseUrl: 'http://example.test'),
      backgroundService: backgroundService,
      onTranscript: (_) async {},
      userId: 'user-1',
      sessionId: 'session-1',
      language: 'bg-BG',
    );

    final transcript = await controller.askQuestionOnceInBackground(
      'What email should I use?',
    );

    expect(transcript, 'pencho@example.com');
    expect(backgroundService.question, 'What email should I use?');
    expect(backgroundService.baseUrl, 'http://example.test');
    expect(backgroundService.userId, 'user-1');
    expect(backgroundService.sessionId, 'session-1');
    expect(backgroundService.language, '');
    expect(backgroundService.timeout, const Duration(seconds: 18));
  });
}
