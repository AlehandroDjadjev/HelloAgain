import 'package:flutter/foundation.dart' show kIsWeb;
import 'package:flutter_dotenv/flutter_dotenv.dart';

String resolveBackendBaseUrl() {
  final configured = dotenv.env['API_BASE_URL']?.trim();
  if (configured != null && configured.isNotEmpty) {
    return configured;
  }

  if (kIsWeb) {
    return 'http://localhost:8000';
  }

  // Android devices in this project typically reach the local Django server
  // through `adb reverse tcp:8000 tcp:8000`, which maps loopback correctly.
  return 'http://127.0.0.1:8000';
}
