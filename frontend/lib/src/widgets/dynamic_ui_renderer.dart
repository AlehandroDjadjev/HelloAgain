import 'dart:convert';
import 'dart:typed_data';

import 'package:flutter/material.dart';
import 'package:image_picker/image_picker.dart';

typedef DynamicUiStateChanged =
    Future<void> Function(Map<String, dynamic> state);
typedef DynamicUiOpenViewer = Future<void> Function(Map<String, dynamic> viewer);

class DynamicUiViewer extends StatefulWidget {
  const DynamicUiViewer({
    super.key,
    required this.root,
    required this.initialState,
    this.voiceBindings = const [],
    this.onStateChanged,
    this.onOpenViewer,
  });

  final Map<String, dynamic> root;
  final Map<String, dynamic> initialState;
  final List<dynamic> voiceBindings;
  final DynamicUiStateChanged? onStateChanged;
  final DynamicUiOpenViewer? onOpenViewer;

  @override
  DynamicUiViewerState createState() => DynamicUiViewerState();
}

class DynamicUiViewerState extends State<DynamicUiViewer> {
  late Map<String, dynamic> _state;
  Map<String, String> _voiceCaptures = const {};

  @override
  void initState() {
    super.initState();
    _state = _deepMap(widget.initialState);
  }

  @override
  Widget build(BuildContext context) {
    return _renderNode(widget.root);
  }

  Future<bool> applyVoicePhrase(String phrase) async {
    final normalized = phrase.trim().toLowerCase();
    if (normalized.isEmpty) return false;
    for (final raw in widget.voiceBindings) {
      if (raw is! Map) continue;
      final phrases = raw['phrases'];
      if (phrases is! List) continue;
      Map<String, String>? captures;
      for (final item in phrases) {
        captures = _matchVoicePattern(normalized, item.toString());
        if (captures != null) break;
      }
      if (captures == null) continue;
      _voiceCaptures = captures;
      await _runAction(_voiceAction(raw));
      _voiceCaptures = const {};
      return true;
    }
    return false;
  }

  Widget _renderNode(dynamic rawNode) {
    if (rawNode is! Map) return const SizedBox.shrink();
    final node = Map<String, dynamic>.from(rawNode);
    final type = _readString(node['type']).toLowerCase();
    final style = _DynamicStyle.from(node['style']);
    final children = _children(node).map(_renderNode).toList(growable: false);

    Widget child;
    switch (type) {
      case 'card':
        child = _styledBox(
          style: style.withDefaults(
            padding: 18,
            radius: 22,
            background: 'surface',
            shadow: 'soft',
          ),
          child: _column(children, style.spacing),
        );
        break;
      case 'column':
        child = _column(children, style.spacing);
        break;
      case 'row':
        child = _row(children, style.spacing);
        break;
      case 'stack':
        child = Stack(children: children);
        break;
      case 'grid':
        child = _grid(node, children);
        break;
      case 'scroll':
        child = SingleChildScrollView(child: _column(children, style.spacing));
        break;
      case 'text':
        child = _text(node, style);
        break;
      case 'rich_text':
        child = _richText(node, style);
        break;
      case 'icon':
        child = Icon(
          _iconData(_readString(node['name'])),
          color: _color(style.color),
          size: _iconSize(style.textSize),
        );
        break;
      case 'image':
        child = _image(node, style);
        break;
      case 'image_picker':
        child = _imagePicker(node, style);
        break;
      case 'button':
        child = _button(node, style);
        break;
      case 'checkbox':
        child = _checkbox(node, style);
        break;
      case 'toggle':
        child = _toggle(node, style);
        break;
      case 'input':
        child = _input(node, style);
        break;
      case 'list':
        child = _list(node, style);
        break;
      case 'progress':
        child = _progress(node);
        break;
      case 'chip':
        child = _chip(node, style);
        break;
      case 'divider':
        child = Divider(color: _color('muted').withValues(alpha: 0.35));
        break;
      case 'spacer':
        child = SizedBox(height: _readDouble(node['height'], 12));
        break;
      default:
        child = _fallbackNode(type);
        break;
    }

    if (type != 'card' && style.hasBoxStyle) {
      child = _styledBox(style: style, child: child);
    }
    if (style.margin > 0) {
      child = Padding(padding: EdgeInsets.all(style.margin), child: child);
    }
    if (node['onTap'] is Map) {
      child = InkWell(
        borderRadius: BorderRadius.circular(style.radius > 0 ? style.radius : 18),
        onTap: () => _runAction(node['onTap']),
        child: child,
      );
    }
    return child;
  }

  Widget _column(List<Widget> children, double spacing) {
    return Column(
      mainAxisSize: MainAxisSize.min,
      crossAxisAlignment: CrossAxisAlignment.start,
      children: _spaced(children, spacing),
    );
  }

  Widget _row(List<Widget> children, double spacing) {
    return Row(
      mainAxisSize: MainAxisSize.min,
      crossAxisAlignment: CrossAxisAlignment.center,
      children: _spaced(children, spacing, horizontal: true),
    );
  }

  Widget _grid(Map<String, dynamic> node, List<Widget> children) {
    final columns = _readInt(node['columns'], 2).clamp(1, 4);
    return GridView.count(
      crossAxisCount: columns,
      crossAxisSpacing: 10,
      mainAxisSpacing: 10,
      shrinkWrap: true,
      physics: const NeverScrollableScrollPhysics(),
      childAspectRatio: _readDouble(node['aspectRatio'], 1.6),
      children: children,
    );
  }

  Widget _styledBox({required _DynamicStyle style, required Widget child}) {
    return Container(
      width: double.infinity,
      padding: EdgeInsets.all(style.padding),
      decoration: BoxDecoration(
        color: style.background.isEmpty ? null : _color(style.background),
        borderRadius: BorderRadius.circular(style.radius),
        boxShadow: _shadow(style.shadow),
      ),
      child: child,
    );
  }

  Widget _text(Map<String, dynamic> node, _DynamicStyle style) {
    return Text(
      _resolveText(_readString(node['text'])),
      textAlign: _textAlign(style.alignment),
      style: TextStyle(
        fontSize: _textSize(style.textSize),
        fontWeight: style.weight == 'bold' ? FontWeight.w800 : FontWeight.w500,
        height: 1.25,
        color: _color(style.color),
      ),
    );
  }

  Widget _richText(Map<String, dynamic> node, _DynamicStyle style) {
    final spans = node['spans'];
    if (spans is! List) return _text(node, style);
    return Text.rich(
      TextSpan(
        children: [
          for (final raw in spans)
            if (raw is Map)
              TextSpan(
                text: _resolveText(_readString(raw['text'])),
                style: TextStyle(
                  fontWeight: _readString(raw['weight']) == 'bold'
                      ? FontWeight.w800
                      : FontWeight.w500,
                  color: _color(_readString(raw['color'], style.color)),
                ),
              ),
        ],
      ),
      style: TextStyle(fontSize: _textSize(style.textSize), height: 1.3),
    );
  }

  Widget _image(Map<String, dynamic> node, _DynamicStyle style) {
    final url = _readString(_resolveValue(node['url']));
    final height = _readDouble(node['height'], 140);
    final radius = BorderRadius.circular(style.radius > 0 ? style.radius : 18);
    final child = _imageContent(url, height);
    if (child == null) return _fallbackNode('image');
    return ClipRRect(
      borderRadius: radius,
      child: child,
    );
  }

  Widget _imagePicker(Map<String, dynamic> node, _DynamicStyle style) {
    final target = _readString(node['target']);
    final url = _readString(_state[target]);
    final height = _readDouble(node['height'], 180);
    final preview = _imageContent(url, height);
    return Column(
      mainAxisSize: MainAxisSize.min,
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        if (preview != null)
          ClipRRect(
            borderRadius: BorderRadius.circular(
              style.radius > 0 ? style.radius : 18,
            ),
            child: preview,
          )
        else
          _mutedPanel(
            _readString(node['emptyText'], 'Изберете снимка от телефона.'),
          ),
        const SizedBox(height: 10),
        FilledButton.icon(
          onPressed: () => _pickImage(target),
          icon: const Icon(Icons.photo_library_rounded),
          label: Text(
            _resolveText(_readString(node['label'], 'Избери снимка')),
          ),
          style: FilledButton.styleFrom(
            backgroundColor: _color(
              style.background.isEmpty ? 'primary' : style.background,
            ),
            foregroundColor: Colors.white,
            minimumSize: const Size(48, 54),
            shape: RoundedRectangleBorder(
              borderRadius: BorderRadius.circular(18),
            ),
          ),
        ),
      ],
    );
  }

  Widget? _imageContent(String url, double height) {
    if (url.startsWith('data:image/')) {
      final comma = url.indexOf(',');
      if (comma <= 0) return null;
      try {
        final bytes = base64Decode(url.substring(comma + 1));
        return Image.memory(
          Uint8List.fromList(bytes),
          height: height,
          width: double.infinity,
          fit: BoxFit.cover,
          errorBuilder: (_, __, ___) => _fallbackNode('image'),
        );
      } catch (_) {
        return null;
      }
    }
    if (!url.startsWith('http://') && !url.startsWith('https://')) return null;
    return Image.network(
      url,
      height: height,
      width: double.infinity,
      fit: BoxFit.cover,
      errorBuilder: (_, __, ___) => _fallbackNode('image'),
    );
  }

  Widget _button(Map<String, dynamic> node, _DynamicStyle style) {
    return SizedBox(
      width: double.infinity,
      child: FilledButton(
        onPressed: () => _runAction(node['action']),
        style: FilledButton.styleFrom(
          backgroundColor: _color(
            style.background.isEmpty ? 'primary' : style.background,
          ),
          foregroundColor: Colors.white,
          minimumSize: const Size(48, 52),
          shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(18)),
        ),
        child: Text(
          _resolveText(_readString(node['label'], 'OK')),
          style: const TextStyle(fontSize: 16, fontWeight: FontWeight.w800),
        ),
      ),
    );
  }

  Widget _checkbox(Map<String, dynamic> node, _DynamicStyle style) {
    final target = _readString(node['target']);
    final label = _resolveText(_readString(node['label']));
    final value = _readBool(_state[target]);
    return CheckboxListTile(
      value: value,
      onChanged: (_) async {
        await _runAction({'type': 'state_toggle', 'target': target});
        await _runAction(node['onChange']);
      },
      title: Text(label, style: TextStyle(fontSize: _textSize(style.textSize))),
      controlAffinity: ListTileControlAffinity.leading,
      contentPadding: EdgeInsets.zero,
    );
  }

  Widget _toggle(Map<String, dynamic> node, _DynamicStyle style) {
    final target = _readString(node['target']);
    return SwitchListTile(
      value: _readBool(_state[target]),
      onChanged: (value) async {
        await _runAction({'type': 'state_set', 'target': target, 'value': value});
        await _runAction(node['onChange']);
      },
      title: Text(
        _resolveText(_readString(node['label'])),
        style: TextStyle(fontSize: _textSize(style.textSize)),
      ),
      contentPadding: EdgeInsets.zero,
    );
  }

  Widget _input(Map<String, dynamic> node, _DynamicStyle style) {
    final target = _readString(node['target']);
    final controller = TextEditingController(text: _readString(_state[target]));
    return TextField(
      controller: controller,
      minLines: _readInt(node['minLines'], 1),
      maxLines: _readInt(node['maxLines'], 4),
      style: TextStyle(fontSize: _textSize(style.textSize)),
      decoration: InputDecoration(
        hintText: _readString(node['placeholder']),
        filled: true,
        fillColor: _color('surface'),
        border: OutlineInputBorder(
          borderRadius: BorderRadius.circular(18),
          borderSide: BorderSide(color: _color('muted').withValues(alpha: 0.4)),
        ),
      ),
      onSubmitted: (value) async {
        await _runAction({'type': 'state_set', 'target': target, 'value': value});
        await _runAction(node['onSubmit']);
      },
      onChanged: (value) => _setStateValue(target, value, persist: false),
      onEditingComplete: () => _persistState(),
    );
  }

  Widget _list(Map<String, dynamic> node, _DynamicStyle style) {
    final target = _readString(node['target']);
    final items = _readList(_state[target]);
    if (items.isEmpty) {
      final empty = _readString(node['emptyText'], 'Няма добавени елементи.');
      return _mutedPanel(empty);
    }
    return Column(
      mainAxisSize: MainAxisSize.min,
      children: [
        for (var index = 0; index < items.length; index += 1)
          _listItem(target, index, items[index], style),
      ],
    );
  }

  Widget _listItem(String target, int index, dynamic item, _DynamicStyle style) {
    final itemMap = item is Map ? Map<String, dynamic>.from(item) : null;
    final label = itemMap == null
        ? item.toString()
        : _readString(itemMap['text'] ?? itemMap['label'] ?? itemMap['title']);
    final done = itemMap != null && _readBool(itemMap['done']);
    return Container(
      margin: const EdgeInsets.only(bottom: 8),
      padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 10),
      decoration: BoxDecoration(
        color: _color('surface'),
        borderRadius: BorderRadius.circular(16),
      ),
      child: Row(
        children: [
          Checkbox(
            value: done,
            onChanged: itemMap == null
                ? null
                : (_) => _toggleListItem(target, index, itemMap),
          ),
          Expanded(
            child: Text(
              label,
              style: TextStyle(
                fontSize: _textSize(style.textSize),
                decoration: done ? TextDecoration.lineThrough : null,
                color: done ? _color('muted') : _color(style.color),
              ),
            ),
          ),
          IconButton(
            tooltip: 'Премахни',
            onPressed: () =>
                _runAction({'type': 'state_remove', 'target': target, 'index': index}),
            icon: const Icon(Icons.close_rounded),
          ),
        ],
      ),
    );
  }

  Widget _progress(Map<String, dynamic> node) {
    final value = _readDouble(
      _resolveValue(node['value']),
      0,
    ).clamp(0.0, 1.0).toDouble();
    return ClipRRect(
      borderRadius: BorderRadius.circular(999),
      child: LinearProgressIndicator(
        value: value,
        minHeight: 12,
        backgroundColor: _color('muted').withValues(alpha: 0.22),
        valueColor: AlwaysStoppedAnimation<Color>(_color('primary')),
      ),
    );
  }

  Widget _chip(Map<String, dynamic> node, _DynamicStyle style) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
      decoration: BoxDecoration(
        color: _color(style.background.isEmpty ? 'accent' : style.background),
        borderRadius: BorderRadius.circular(999),
      ),
      child: Text(
        _resolveText(_readString(node['label'])),
        style: TextStyle(
          fontSize: 14,
          fontWeight: FontWeight.w800,
          color: _color(style.color),
        ),
      ),
    );
  }

  dynamic _voiceAction(Map raw) {
    final action = raw['action'];
    if (action is! String) return action;
    return {
      'type': action,
      if (raw['target'] != null) 'target': raw['target'],
      if (raw['value'] != null) 'value': raw['value'],
      if (raw['index'] != null) 'index': raw['index'],
      if (raw['field'] != null) 'field': raw['field'],
      if (raw['prompt'] != null) 'prompt': raw['prompt'],
    };
  }

  Future<void> _runAction(dynamic rawAction) async {
    if (rawAction is List) {
      for (final action in rawAction) {
        await _runAction(action);
      }
      return;
    }
    if (rawAction is! Map) return;
    final action = Map<String, dynamic>.from(rawAction);
    final type = _readString(action['type']).toLowerCase();
    final target = _readString(action['target']);
    switch (type) {
      case 'state_append':
        final items = _readList(_state[target]);
        items.add(_normalizeActionValue(action['value']));
        await _setStateValue(target, items);
        break;
      case 'state_update':
        await _updateStateItem(target, action);
        break;
      case 'state_remove':
        final items = _readList(_state[target]);
        final index = _readInt(action['index'], -1);
        if (index >= 0 && index < items.length) {
          items.removeAt(index);
          await _setStateValue(target, items);
        }
        break;
      case 'state_remove_value':
        await _removeStateValue(target, action);
        break;
      case 'state_toggle':
        await _setStateValue(target, !_readBool(_state[target]));
        break;
      case 'state_set':
        await _setStateValue(target, _normalizeActionValue(action['value']));
        break;
      case 'pick_image':
        await _pickImage(target);
        break;
      case 'phone_command':
        await _openPhoneCommand(action);
        break;
      case 'open_viewer':
        final viewer = action['viewer'];
        if (viewer is Map && widget.onOpenViewer != null) {
          await widget.onOpenViewer!(Map<String, dynamic>.from(viewer));
        }
        break;
      case 'confirm_action':
        await _confirmAction(action);
        break;
      case 'refresh_data':
      case 'call_backend_tool':
        await _setStateValue(
          '_status',
          'Това действие изисква разрешен backend инструмент.',
        );
        break;
      default:
        await _setStateValue('_status', 'Непознато действие: $type');
        break;
    }
  }

  Future<void> _confirmAction(Map<String, dynamic> action) async {
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (context) => AlertDialog(
        title: const Text('Потвърждение'),
        content: Text(_readString(action['message'], 'Да продължа ли?')),
        actions: [
          TextButton(
            onPressed: () => Navigator.of(context).pop(false),
            child: const Text('Не'),
          ),
          FilledButton(
            onPressed: () => Navigator.of(context).pop(true),
            child: const Text('Да'),
          ),
        ],
      ),
    );
    if (confirmed == true) {
      await _runAction(action['action']);
    }
  }

  Future<void> _pickImage(String target) async {
    if (target.isEmpty) return;
    try {
      final image = await ImagePicker().pickImage(
        source: ImageSource.gallery,
        maxWidth: 1280,
        imageQuality: 78,
      );
      if (image == null) return;
      final bytes = await image.readAsBytes();
      if (bytes.isEmpty) {
        await _setStateValue(
          '_status',
          'Не успях да прочета снимката.',
        );
        return;
      }
      final mime = image.mimeType ?? _mimeFromName(image.name);
      await _setStateValue(target, 'data:$mime;base64,${base64Encode(bytes)}');
      await _setStateValue('${target}Name', image.name);
    } catch (_) {
      await _setStateValue(
        '_status',
        'Не успях да отворя галерията.',
      );
    }
  }

  Future<void> _openPhoneCommand(Map<String, dynamic> action) async {
    final prompt = _resolveText(
      _readString(_normalizeActionValue(action['prompt'])),
    );
    if (prompt.isEmpty || widget.onOpenViewer == null) return;
    await widget.onOpenViewer!({
      'widget_type': 'phone_command_launcher',
      'title': _readString(action['title'], 'Телефон'),
      'summary': _readString(action['summary'], prompt),
      'prompt': prompt,
      'auto_run_on_open': _readBool(action['auto_run_on_open'] ?? true),
    });
  }

  Future<void> _updateStateItem(
    String target,
    Map<String, dynamic> action,
  ) async {
    final items = _readList(_state[target]);
    final index = _readInt(action['index'], -1);
    if (index < 0 || index >= items.length) return;
    final current = items[index];
    if (current is Map) {
      final value = _normalizeActionValue(action['value']);
      final updated = Map<String, dynamic>.from(current)
        ..addAll(value is Map ? _deepMap(value) : {'value': value});
      items[index] = updated;
    } else {
      items[index] = _normalizeActionValue(action['value']);
    }
    await _setStateValue(target, items);
  }

  Future<void> _removeStateValue(
    String target,
    Map<String, dynamic> action,
  ) async {
    final items = _readList(_state[target]);
    final value = _readString(_normalizeActionValue(action['value'])).toLowerCase();
    final field = _readString(action['field'], 'text');
    if (value.isEmpty) return;
    items.removeWhere((item) {
      final text = item is Map
          ? _readString(item[field] ?? item['text'] ?? item['label'] ?? item['title'])
          : _readString(item);
      return text.toLowerCase() == value;
    });
    await _setStateValue(target, items);
  }

  Future<void> _toggleListItem(
    String target,
    int index,
    Map<String, dynamic> item,
  ) async {
    final items = _readList(_state[target]);
    item['done'] = !_readBool(item['done']);
    items[index] = item;
    await _setStateValue(target, items);
  }

  Future<void> _setStateValue(
    String target,
    dynamic value, {
    bool persist = true,
  }) async {
    if (target.isEmpty) return;
    setState(() {
      _state[target] = value;
    });
    if (persist) await _persistState();
  }

  Future<void> _persistState() async {
    await widget.onStateChanged?.call(_deepMap(_state));
  }

  dynamic _normalizeActionValue(dynamic value) {
    if (value is String && value.startsWith(r'$voice.')) {
      return _voiceCaptures[value.substring(7)] ?? '';
    }
    if (value is String && value.startsWith(r'$state.')) {
      return _state[value.substring(7)] ?? '';
    }
    if (value is Map) {
      return value.map(
        (key, item) => MapEntry(key.toString(), _normalizeActionValue(item)),
      );
    }
    if (value is List) return value.map(_normalizeActionValue).toList();
    return value;
  }

  dynamic _resolveValue(dynamic value) {
    if (value is String && value.startsWith(r'$state.')) {
      return _state[value.substring(7)];
    }
    return value;
  }

  String _resolveText(String text) {
    return text.replaceAllMapped(RegExp(r'\{\{state\.([A-Za-z0-9_]+)\}\}'), (
      match,
    ) {
      return _readString(_state[match.group(1)]);
    });
  }

  Widget _mutedPanel(String text) {
    return Container(
      width: double.infinity,
      padding: const EdgeInsets.all(14),
      decoration: BoxDecoration(
        color: _color('surface'),
        borderRadius: BorderRadius.circular(18),
      ),
      child: Text(
        text,
        style: TextStyle(
          fontSize: 16,
          height: 1.3,
          fontWeight: FontWeight.w600,
          color: _color('muted'),
        ),
      ),
    );
  }

  Widget _fallbackNode(String type) {
    return _mutedPanel(type.isEmpty ? 'Неподдържан елемент.' : 'Неподдържан елемент: $type');
  }

  List<dynamic> _children(Map<String, dynamic> node) {
    final children = node['children'];
    return children is List ? children : const [];
  }

  List<Widget> _spaced(
    List<Widget> children,
    double spacing, {
    bool horizontal = false,
  }) {
    if (children.length < 2 || spacing <= 0) return children;
    final spaced = <Widget>[];
    for (var index = 0; index < children.length; index += 1) {
      if (index > 0) {
        spaced.add(SizedBox(width: horizontal ? spacing : 0, height: horizontal ? 0 : spacing));
      }
      spaced.add(horizontal ? Flexible(child: children[index]) : children[index]);
    }
    return spaced;
  }

  Color _color(String token) {
    switch (token.trim().toLowerCase()) {
      case 'primary':
        return const Color(0xFF8C1C13);
      case 'accent':
        return const Color(0xFFE7C4B4);
      case 'muted':
        return const Color(0xFF76635F);
      case 'surface':
      case '':
        return const Color(0xFFFFFBF7);
      default:
        return const Color(0xFF2E1B1A);
    }
  }

  List<BoxShadow> _shadow(String value) {
    switch (value) {
      case 'medium':
        return [
          BoxShadow(
            color: Colors.black.withValues(alpha: 0.10),
            blurRadius: 26,
            offset: const Offset(0, 14),
          ),
        ];
      case 'soft':
        return [
          BoxShadow(
            color: Colors.black.withValues(alpha: 0.06),
            blurRadius: 18,
            offset: const Offset(0, 9),
          ),
        ];
      default:
        return const [];
    }
  }

  double _textSize(String token) {
    switch (token) {
      case 'small':
        return 15;
      case 'large':
        return 22;
      case 'xlarge':
        return 30;
      default:
        return 18;
    }
  }

  double _iconSize(String token) => token == 'xlarge' ? 38 : 28;

  TextAlign _textAlign(String value) {
    switch (value) {
      case 'center':
        return TextAlign.center;
      case 'end':
        return TextAlign.end;
      default:
        return TextAlign.start;
    }
  }

  IconData _iconData(String name) {
    switch (name.toLowerCase()) {
      case 'check':
        return Icons.check_circle_rounded;
      case 'phone':
        return Icons.phone_rounded;
      case 'weather':
        return Icons.wb_sunny_rounded;
      case 'news':
        return Icons.article_rounded;
      case 'note':
        return Icons.sticky_note_2_rounded;
      case 'task':
        return Icons.task_alt_rounded;
      default:
        return Icons.auto_awesome_rounded;
    }
  }
}

class _DynamicStyle {
  const _DynamicStyle({
    required this.padding,
    required this.margin,
    required this.radius,
    required this.background,
    required this.textSize,
    required this.weight,
    required this.color,
    required this.alignment,
    required this.spacing,
    required this.shadow,
  });

  factory _DynamicStyle.from(dynamic raw) {
    final map = raw is Map ? Map<String, dynamic>.from(raw) : const <String, dynamic>{};
    return _DynamicStyle(
      padding: _readDouble(map['padding'], 0),
      margin: _readDouble(map['margin'], 0),
      radius: _readDouble(map['radius'], 0),
      background: _readString(map['background']),
      textSize: _readString(map['textSize'], 'medium'),
      weight: _readString(map['weight'], 'normal'),
      color: _readString(map['color'], 'primary'),
      alignment: _readString(map['alignment'], 'start'),
      spacing: _readDouble(map['spacing'], 12),
      shadow: _readString(map['shadow'], 'none'),
    );
  }

  bool get hasBoxStyle =>
      padding > 0 || radius > 0 || background.isNotEmpty || shadow != 'none';

  _DynamicStyle withDefaults({
    double? padding,
    double? radius,
    String? background,
    String? shadow,
  }) {
    return _DynamicStyle(
      padding: this.padding > 0 ? this.padding : padding ?? this.padding,
      margin: margin,
      radius: this.radius > 0 ? this.radius : radius ?? this.radius,
      background: this.background.isNotEmpty
          ? this.background
          : background ?? this.background,
      textSize: textSize,
      weight: weight,
      color: color,
      alignment: alignment,
      spacing: spacing,
      shadow: this.shadow != 'none' ? this.shadow : shadow ?? this.shadow,
    );
  }

  final double padding;
  final double margin;
  final double radius;
  final String background;
  final String textSize;
  final String weight;
  final String color;
  final String alignment;
  final double spacing;
  final String shadow;
}

Map<String, dynamic> _deepMap(dynamic value) {
  if (value is! Map) return <String, dynamic>{};
  return value.map(
    (key, item) => MapEntry(key.toString(), _deepValue(item)),
  );
}

dynamic _deepValue(dynamic value) {
  if (value is Map) return _deepMap(value);
  if (value is List) return value.map(_deepValue).toList();
  return value;
}

List<dynamic> _readList(dynamic value) {
  return value is List ? List<dynamic>.from(value.map(_deepValue)) : <dynamic>[];
}

String _readString(dynamic value, [String fallback = '']) {
  final text = value?.toString().trim() ?? '';
  return text.isEmpty ? fallback : text;
}

double _readDouble(dynamic value, double fallback) {
  if (value is num) return value.toDouble();
  return double.tryParse(value?.toString() ?? '') ?? fallback;
}

int _readInt(dynamic value, int fallback) {
  if (value is num) return value.toInt();
  return int.tryParse(value?.toString() ?? '') ?? fallback;
}

bool _readBool(dynamic value) {
  if (value is bool) return value;
  return value?.toString().trim().toLowerCase() == 'true';
}

Map<String, String>? _matchVoicePattern(String normalizedPhrase, String pattern) {
  final normalizedPattern = pattern.trim().toLowerCase();
  if (normalizedPattern.isEmpty) return null;
  if (!normalizedPattern.contains('*')) {
  return normalizedPattern == normalizedPhrase
      ? const <String, String>{}
      : null;
  }
  final parts = normalizedPattern.split('*');
  final prefix = parts.first.trim();
  final suffix = parts.length > 1 ? parts.sublist(1).join('*').trim() : '';
  if (prefix.isNotEmpty && !normalizedPhrase.startsWith(prefix)) return null;
  if (suffix.isNotEmpty && !normalizedPhrase.endsWith(suffix)) return null;
  final start = prefix.isEmpty ? 0 : prefix.length;
  final end = suffix.isEmpty
      ? normalizedPhrase.length
      : normalizedPhrase.length - suffix.length;
  if (end < start) return null;
  final capture = normalizedPhrase.substring(start, end).trim();
  if (capture.isEmpty) return null;
  return {'1': capture, 'text': capture};
}

String _mimeFromName(String name) {
  final lower = name.toLowerCase();
  if (lower.endsWith('.png')) return 'image/png';
  if (lower.endsWith('.webp')) return 'image/webp';
  if (lower.endsWith('.gif')) return 'image/gif';
  return 'image/jpeg';
}
