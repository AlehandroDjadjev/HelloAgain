import 'rect.dart';

/// Mirrors Kotlin UiNodeDto and the backend AccessibilityNode schema.
/// Field names use camelCase in Dart; toJson() produces snake_case for the backend.
class UiNode {
  final String elementRef;
  final String? className;
  final String? text;
  final String? contentDesc;
  final String? viewId;
  final String? packageName;
  final bool clickable;
  final bool enabled;
  final bool focused;
  final bool editable;
  final Rect bounds;
  final int childCount;

  const UiNode({
    required this.elementRef,
    this.className,
    this.text,
    this.contentDesc,
    this.viewId,
    this.packageName,
    required this.clickable,
    required this.enabled,
    required this.focused,
    required this.editable,
    required this.bounds,
    required this.childCount,
  });

  factory UiNode.fromMap(Map<Object?, Object?> map) {
    return UiNode(
      elementRef: map['elementRef'] as String,
      className: map['className'] as String?,
      text: map['text'] as String?,
      contentDesc: map['contentDesc'] as String?,
      viewId: map['viewId'] as String?,
      packageName: map['packageName'] as String?,
      clickable: map['clickable'] as bool,
      enabled: map['enabled'] as bool,
      focused: map['focused'] as bool,
      editable: map['editable'] as bool,
      bounds: Rect.fromMap(map['bounds'] as Map<Object?, Object?>),
      childCount: (map['childCount'] as num).toInt(),
    );
  }

  Map<String, Object?> toMap() => {
        'elementRef': elementRef,
        'className': className,
        'text': text,
        'contentDesc': contentDesc,
        'viewId': viewId,
        'packageName': packageName,
        'clickable': clickable,
        'enabled': enabled,
        'focused': focused,
        'editable': editable,
        'bounds': bounds.toMap(),
        'childCount': childCount,
      };

  /// Django-compatible serialisation (maps to AccessibilityNode schema).
  Map<String, Object?> toJson() => {
        'ref': elementRef,
        'class_name': className,
        'text': text,
        'content_desc': contentDesc,
        'view_id': viewId,
        'clickable': clickable,
        'enabled': enabled,
        'focused': focused,
        'index_in_parent': 0,
        'bounds': bounds.toJson(),
      };
}
