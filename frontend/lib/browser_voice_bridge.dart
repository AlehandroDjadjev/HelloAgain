export 'browser_voice_bridge_stub.dart'
    if (dart.library.io) 'browser_voice_bridge_mobile.dart'
    if (dart.library.html) 'browser_voice_bridge_web.dart';
