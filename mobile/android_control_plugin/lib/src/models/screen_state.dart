import 'ui_node.dart';

/// Mirrors Kotlin ScreenStateDto and the backend ScreenState schema.
/// When [isSensitive] is true, [nodes] is always empty — the service
/// never transmits sensitive node trees.
class ScreenState {
  final int timestampMs;
  final String? foregroundPackage;
  final String? windowTitle;
  final String screenHash;
  final String? focusedElementRef;
  final bool isSensitive;
  final List<UiNode> nodes;

  const ScreenState({
    required this.timestampMs,
    this.foregroundPackage,
    this.windowTitle,
    required this.screenHash,
    this.focusedElementRef,
    required this.isSensitive,
    required this.nodes,
  });

  factory ScreenState.fromMap(Map<Object?, Object?> map) {
    final rawNodes = map['nodes'] as List<Object?>? ?? [];
    return ScreenState(
      timestampMs: (map['timestampMs'] as num).toInt(),
      foregroundPackage: map['foregroundPackage'] as String?,
      windowTitle: map['windowTitle'] as String?,
      screenHash: (map['screenHash'] as String?) ?? '',
      focusedElementRef: map['focusedElementRef'] as String?,
      isSensitive: (map['isSensitive'] as bool?) ?? false,
      nodes: rawNodes
          .whereType<Map<Object?, Object?>>()
          .map(UiNode.fromMap)
          .toList(),
    );
  }

  Map<String, Object?> toMap() => {
        'timestampMs': timestampMs,
        'foregroundPackage': foregroundPackage,
        'windowTitle': windowTitle,
        'screenHash': screenHash,
        'focusedElementRef': focusedElementRef,
        'isSensitive': isSensitive,
        'nodes': nodes.map((n) => n.toMap()).toList(),
      };

  /// Django ScreenState schema serialisation (snake_case, ISO timestamp).
  Map<String, Object?> toJson() => {
        'foreground_package': foregroundPackage ?? '',
        'window_title': windowTitle,
        'screen_hash': screenHash,
        'focused_element_ref': focusedElementRef,
        'is_sensitive': isSensitive,
        'nodes': nodes.map((n) => n.toJson()).toList(),
        'captured_at': DateTime.fromMillisecondsSinceEpoch(timestampMs)
            .toUtc()
            .toIso8601String(),
      };
}
