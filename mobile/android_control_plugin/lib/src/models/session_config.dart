/// Mirrors Kotlin SessionConfigDto.
/// Sent to Android when starting a new automation session.
class SessionConfig {
  final String sessionId;

  /// Packages this session is permitted to interact with.
  final List<String> allowedPackages;

  /// "always" | "onIrreversible" | "never"
  final String confirmationMode;
  final int maxStepCount;
  final bool allowTextEntry;
  final bool allowSendActions;

  const SessionConfig({
    required this.sessionId,
    required this.allowedPackages,
    this.confirmationMode = 'always',
    this.maxStepCount = 30,
    this.allowTextEntry = true,
    this.allowSendActions = false,
  });

  factory SessionConfig.fromMap(Map<Object?, Object?> map) {
    return SessionConfig(
      sessionId: map['sessionId'] as String,
      allowedPackages:
          (map['allowedPackages'] as List<Object?>?)?.cast<String>() ?? [],
      confirmationMode:
          (map['confirmationMode'] as String?) ?? 'always',
      maxStepCount: (map['maxStepCount'] as num?)?.toInt() ?? 30,
      allowTextEntry: (map['allowTextEntry'] as bool?) ?? true,
      allowSendActions: (map['allowSendActions'] as bool?) ?? false,
    );
  }

  Map<String, Object?> toMap() => {
        'sessionId': sessionId,
        'allowedPackages': allowedPackages,
        'confirmationMode': confirmationMode,
        'maxStepCount': maxStepCount,
        'allowTextEntry': allowTextEntry,
        'allowSendActions': allowSendActions,
      };
}
