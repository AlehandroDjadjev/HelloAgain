import 'dart:convert';
import 'dart:typed_data';

import 'package:http/http.dart' as http;

class VoiceGatewayClient {
  VoiceGatewayClient({required String baseUrl})
    : _base = baseUrl.endsWith('/')
          ? baseUrl.substring(0, baseUrl.length - 1)
          : baseUrl;

  final String _base;

  Future<VoiceGatewayHealth> getHealth() async {
    final data = await _get('/api/voice-gateway/health/');
    return VoiceGatewayHealth.fromJson(data);
  }

  Future<TranscriptionResponse> transcribe({
    required Uint8List audioBytes,
    String? language,
    String userId = 'flutter-voice-lab',
    String sessionId = 'voice-lab-session',
  }) async {
    final request =
        http.MultipartRequest(
            'POST',
            Uri.parse('$_base/api/voice-gateway/transcribe/'),
          )
          ..fields['user_id'] = userId
          ..fields['session_id'] = sessionId;

    if (language != null && language.trim().isNotEmpty) {
      request.fields['language'] = language.trim();
    }

    request.files.add(
      http.MultipartFile.fromBytes(
        'audio',
        audioBytes,
        filename: 'push_to_talk.wav',
      ),
    );

    final streamed = await request.send().timeout(const Duration(seconds: 60));
    final response = await http.Response.fromStream(streamed);
    final data = _decode(response);

    return TranscriptionResponse(
      transcript: (data['transcript'] ?? '').toString(),
      provider: (data['provider'] ?? 'unknown').toString(),
      warnings: _stringList(data['warnings']),
    );
  }

  Future<ConversationResponse> conversation({
    Uint8List? audioBytes,
    String message = '',
    String? language,
    String userId = 'flutter-voice-lab',
    String sessionId = 'voice-lab-session',
  }) async {
    final trimmedMessage = message.trim();
    Map<String, dynamic> data;

    if (audioBytes != null) {
      final request =
          http.MultipartRequest(
              'POST',
              Uri.parse('$_base/api/voice-gateway/conversation/'),
            )
            ..fields['user_id'] = userId
            ..fields['session_id'] = sessionId;

      if (language != null && language.trim().isNotEmpty) {
        request.fields['language'] = language.trim();
      }

      request.files.add(
        http.MultipartFile.fromBytes(
          'audio',
          audioBytes,
          filename: 'conversation_turn.wav',
        ),
      );

      final streamed = await request.send().timeout(
        const Duration(seconds: 90),
      );
      final response = await http.Response.fromStream(streamed);
      data = _decode(response);
    } else {
      data = await _post('/api/voice-gateway/conversation/', {
        'user_id': userId,
        'session_id': sessionId,
        'message': trimmedMessage,
        if (language != null && language.trim().isNotEmpty)
          'language': language.trim(),
      });
    }

    final audioBase64 = (data['assistant_audio_base64'] ?? '').toString();
    if (audioBase64.isEmpty) {
      throw const VoiceGatewayException(
        'Voice gateway returned no assistant audio.',
      );
    }

    return ConversationResponse(
      transcript: (data['transcript'] ?? '').toString(),
      assistantText: (data['assistant_text'] ?? '').toString(),
      assistantAudioBytes: base64Decode(audioBase64),
      assistantAudioMimeType: (data['assistant_audio_mime_type'] ?? 'audio/wav')
          .toString(),
      providerStatus: _stringMap(data['provider_status']),
      warnings: _stringList(data['warnings']),
    );
  }

  Future<SpeechResponse> speak({
    required String text,
    String userId = 'flutter-voice-lab',
    String sessionId = 'voice-lab-session',
  }) async {
    final data = await _post('/api/voice-gateway/speak/', {
      'user_id': userId,
      'session_id': sessionId,
      'text': text,
    });

    final audioBase64 = (data['audio_base64'] ?? '').toString();
    if (audioBase64.isEmpty) {
      throw const VoiceGatewayException('Voice gateway returned no audio.');
    }

    return SpeechResponse(
      text: (data['text'] ?? text).toString(),
      audioBytes: base64Decode(audioBase64),
      mimeType: (data['audio_mime_type'] ?? 'audio/wav').toString(),
      provider: (data['provider'] ?? 'unknown').toString(),
      warnings: _stringList(data['warnings']),
    );
  }

  Future<Map<String, dynamic>> _post(
    String path,
    Map<String, dynamic> body,
  ) async {
    final response = await http
        .post(
          Uri.parse('$_base$path'),
          headers: const {'Content-Type': 'application/json'},
          body: jsonEncode(body),
        )
        .timeout(const Duration(seconds: 30));
    return _decode(response);
  }

  Future<Map<String, dynamic>> _get(String path) async {
    final response = await http
        .get(
          Uri.parse('$_base$path'),
          headers: const {'Content-Type': 'application/json'},
        )
        .timeout(const Duration(seconds: 15));
    return _decode(response);
  }

  static Map<String, dynamic> _decode(http.Response response) {
    final body = utf8.decode(response.bodyBytes);
    final payload = body.isEmpty ? <String, dynamic>{} : jsonDecode(body);

    if (response.statusCode >= 200 && response.statusCode < 300) {
      if (payload is Map<String, dynamic>) {
        return payload;
      }
      return {'data': payload};
    }

    if (payload is Map<String, dynamic>) {
      final error = payload['error']?.toString();
      if (error != null && error.isNotEmpty) {
        throw VoiceGatewayException(error);
      }
    }

    throw VoiceGatewayException(
      'Voice gateway request failed with status ${response.statusCode}.',
    );
  }

  static List<String> _stringList(Object? value) {
    if (value is List) {
      return value.map((item) => item.toString()).toList();
    }
    return const [];
  }

  static Map<String, String> _stringMap(Object? value) {
    if (value is Map) {
      return value.map(
        (key, item) => MapEntry(key.toString(), item.toString()),
      );
    }
    return const {};
  }
}

class VoiceGatewayHealth {
  const VoiceGatewayHealth({required this.status, required this.providers});

  final String status;
  final Map<String, String> providers;

  factory VoiceGatewayHealth.fromJson(Map<String, dynamic> json) {
    final providers = <String, String>{};
    final rawProviders = json['providers'];
    if (rawProviders is Map<String, dynamic>) {
      for (final entry in rawProviders.entries) {
        providers[entry.key] = entry.value.toString();
      }
    }

    return VoiceGatewayHealth(
      status: (json['status'] ?? 'unknown').toString(),
      providers: providers,
    );
  }
}

class TranscriptionResponse {
  const TranscriptionResponse({
    required this.transcript,
    required this.provider,
    required this.warnings,
  });

  final String transcript;
  final String provider;
  final List<String> warnings;
}

class SpeechResponse {
  const SpeechResponse({
    required this.text,
    required this.audioBytes,
    required this.mimeType,
    required this.provider,
    required this.warnings,
  });

  final String text;
  final Uint8List audioBytes;
  final String mimeType;
  final String provider;
  final List<String> warnings;
}

class ConversationResponse {
  const ConversationResponse({
    required this.transcript,
    required this.assistantText,
    required this.assistantAudioBytes,
    required this.assistantAudioMimeType,
    required this.providerStatus,
    required this.warnings,
  });

  final String transcript;
  final String assistantText;
  final Uint8List assistantAudioBytes;
  final String assistantAudioMimeType;
  final Map<String, String> providerStatus;
  final List<String> warnings;
}

class VoiceGatewayException implements Exception {
  const VoiceGatewayException(this.message);

  final String message;

  @override
  String toString() => message;
}
