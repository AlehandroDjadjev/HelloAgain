/// Mirrors Kotlin RectDto and the backend Bounds schema.
class Rect {
  final int left;
  final int top;
  final int right;
  final int bottom;

  const Rect({
    required this.left,
    required this.top,
    required this.right,
    required this.bottom,
  });

  /// Deserialise from MethodChannel Map (camelCase keys from Kotlin toMap()).
  factory Rect.fromMap(Map<Object?, Object?> map) {
    return Rect(
      left: (map['left'] as num).toInt(),
      top: (map['top'] as num).toInt(),
      right: (map['right'] as num).toInt(),
      bottom: (map['bottom'] as num).toInt(),
    );
  }

  /// Serialise to MethodChannel Map.
  Map<String, Object?> toMap() => {
        'left': left,
        'top': top,
        'right': right,
        'bottom': bottom,
      };

  /// Serialise to Django backend format (snake_case keys).
  Map<String, Object?> toJson() => toMap();

  @override
  String toString() => 'Rect($left,$top,$right,$bottom)';

  @override
  bool operator ==(Object other) =>
      other is Rect &&
      left == other.left &&
      top == other.top &&
      right == other.right &&
      bottom == other.bottom;

  @override
  int get hashCode => Object.hash(left, top, right, bottom);
}
