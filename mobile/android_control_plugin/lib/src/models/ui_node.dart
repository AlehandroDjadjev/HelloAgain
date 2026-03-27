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
  final String? parentRef;
  final bool clickable;
  final bool longClickable;
  final bool scrollable;
  final bool enabled;
  final bool focused;
  final bool selected;
  final bool editable;
  final bool checkable;
  final bool checked;
  final Rect bounds;
  final int indexInParent;
  final int childCount;
  final List<String> children;

  const UiNode({
    required this.elementRef,
    this.className,
    this.text,
    this.contentDesc,
    this.viewId,
    this.packageName,
    this.parentRef,
    required this.clickable,
    required this.longClickable,
    required this.scrollable,
    required this.enabled,
    required this.focused,
    required this.selected,
    required this.editable,
    required this.checkable,
    required this.checked,
    required this.bounds,
    required this.indexInParent,
    required this.childCount,
    this.children = const [],
  });

  factory UiNode.fromMap(Map<Object?, Object?> map) {
    final rawChildren = map['children'] as List<Object?>? ?? const [];
    return UiNode(
      elementRef: map['elementRef'] as String,
      className: map['className'] as String?,
      text: map['text'] as String?,
      contentDesc: map['contentDesc'] as String?,
      viewId: map['viewId'] as String?,
      packageName: map['packageName'] as String?,
      parentRef: map['parentRef'] as String?,
      clickable: map['clickable'] as bool,
      longClickable: (map['longClickable'] as bool?) ?? false,
      scrollable: (map['scrollable'] as bool?) ?? false,
      enabled: map['enabled'] as bool,
      focused: map['focused'] as bool,
      selected: (map['selected'] as bool?) ?? false,
      editable: map['editable'] as bool,
      checkable: (map['checkable'] as bool?) ?? false,
      checked: (map['checked'] as bool?) ?? false,
      bounds: Rect.fromMap(map['bounds'] as Map<Object?, Object?>),
      indexInParent: (map['indexInParent'] as num?)?.toInt() ?? 0,
      childCount: (map['childCount'] as num).toInt(),
      children: rawChildren.whereType<String>().toList(),
    );
  }

  Map<String, Object?> toMap() => {
        'elementRef': elementRef,
        'className': className,
        'text': text,
        'contentDesc': contentDesc,
        'viewId': viewId,
        'packageName': packageName,
        'parentRef': parentRef,
        'clickable': clickable,
        'longClickable': longClickable,
        'scrollable': scrollable,
        'enabled': enabled,
        'focused': focused,
        'selected': selected,
        'editable': editable,
        'checkable': checkable,
        'checked': checked,
        'bounds': bounds.toMap(),
        'indexInParent': indexInParent,
        'childCount': childCount,
        'children': children,
      };

  /// Django-compatible serialisation (maps to AccessibilityNode schema).
  Map<String, Object?> toJson() => {
        'ref': elementRef,
        'class_name': className,
        'text': text,
        'content_desc': contentDesc,
        'view_id': viewId,
        'package_name': packageName,
        'parent_ref': parentRef,
        'clickable': clickable,
        'long_clickable': longClickable,
        'scrollable': scrollable,
        'enabled': enabled,
        'focused': focused,
        'selected': selected,
        'editable': editable,
        'checkable': checkable,
        'checked': checked,
        'index_in_parent': indexInParent,
        'bounds': bounds.toJson(),
        'child_count': childCount,
        'children': children,
      };
}
