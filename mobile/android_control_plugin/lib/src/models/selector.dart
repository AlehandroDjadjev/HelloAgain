/// Mirrors Kotlin SelectorDto and the backend Selector schema.
///
/// Resolution priority (highest → lowest):
///   elementRef → viewId → contentDescEquals → textEquals
///   → textContains / contentDescContains → className + indexInParent
///
/// All fields are optional. NodeMatcher applies AND logic on the Kotlin side.
class Selector {
  final String? elementRef;
  final String? textEquals;
  final String? textContains;
  final String? contentDescEquals;
  final String? contentDescContains;
  final String? viewId;
  final String? className;
  final String? packageName;
  final bool? clickable;
  final bool? enabled;
  final bool? focused;
  final int? indexInParent;

  const Selector({
    this.elementRef,
    this.textEquals,
    this.textContains,
    this.contentDescEquals,
    this.contentDescContains,
    this.viewId,
    this.className,
    this.packageName,
    this.clickable,
    this.enabled,
    this.focused,
    this.indexInParent,
  });

  /// Convenience constructors for the most common selector strategies.
  const Selector.byRef(String ref) : this(elementRef: ref);
  const Selector.byViewId(String id) : this(viewId: id);
  const Selector.byContentDesc(String desc) : this(contentDescEquals: desc);
  const Selector.byText(String text) : this(textEquals: text);

  factory Selector.fromMap(Map<Object?, Object?> map) {
    return Selector(
      elementRef: map['elementRef'] as String?,
      textEquals: map['textEquals'] as String?,
      textContains: map['textContains'] as String?,
      contentDescEquals: map['contentDescEquals'] as String?,
      contentDescContains: map['contentDescContains'] as String?,
      viewId: map['viewId'] as String?,
      className: map['className'] as String?,
      packageName: map['packageName'] as String?,
      clickable: map['clickable'] as bool?,
      enabled: map['enabled'] as bool?,
      focused: map['focused'] as bool?,
      indexInParent: (map['indexInParent'] as num?)?.toInt(),
    );
  }

  Map<String, Object?> toMap() {
    final m = <String, Object?>{};
    if (elementRef != null) m['elementRef'] = elementRef;
    if (textEquals != null) m['textEquals'] = textEquals;
    if (textContains != null) m['textContains'] = textContains;
    if (contentDescEquals != null) m['contentDescEquals'] = contentDescEquals;
    if (contentDescContains != null) {
      m['contentDescContains'] = contentDescContains;
    }
    if (viewId != null) m['viewId'] = viewId;
    if (className != null) m['className'] = className;
    if (packageName != null) m['packageName'] = packageName;
    if (clickable != null) m['clickable'] = clickable;
    if (enabled != null) m['enabled'] = enabled;
    if (focused != null) m['focused'] = focused;
    if (indexInParent != null) m['indexInParent'] = indexInParent;
    return m;
  }
}
