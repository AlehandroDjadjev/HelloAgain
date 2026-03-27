"""
Compact accessibility-tree formatting for LLM prompts.

The formatter favors actionable, information-rich nodes and stays inside a
rough token budget so smaller local models do not get overwhelmed.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Iterable

from django.conf import settings

SENSITIVE_SENTINEL = "[SCREEN OMITTED: sensitive content detected]"

_LAYOUT_CLASSES = frozenset({
    "android.view.View",
    "android.widget.FrameLayout",
    "android.widget.LinearLayout",
    "View",
    "FrameLayout",
    "LinearLayout",
})

_RECENT_HISTORY_WINDOW = 5


@dataclass(frozen=True)
class _NodeView:
    ref: str
    raw: dict
    order: int
    depth: int
    relevance: int
    parent_ref: str | None
    child_refs: tuple[str, ...]


def format_screen_for_llm(
    screen_state: dict,
    max_nodes: int | None = None,
    token_budget: int | None = None,
) -> str:
    """
    Format the current accessibility tree for prompt inclusion.

    The output always starts with a compact header line so callers can split it
    into ``screen_header`` and ``screen_tree``.
    """
    header = _build_header(screen_state)
    if screen_state.get("is_sensitive"):
        return f"{header}\n{SENSITIVE_SENTINEL}"

    nodes = screen_state.get("nodes") or []
    if not nodes:
        return f"{header}\n(no visible nodes)"

    node_map = _index_nodes(nodes)
    root_refs = _find_root_refs(node_map.values())
    depths = _compute_depths(node_map, root_refs)
    views = {
        ref: _NodeView(
            ref=ref,
            raw=node,
            order=idx,
            depth=depths.get(ref, 0),
            relevance=_score_node(node),
            parent_ref=_text_value(node, "parent_ref") or None,
            child_refs=tuple(_child_refs(node)),
        )
        for idx, (ref, node) in enumerate(node_map.items())
    }

    budget = _effective_screen_budget(token_budget)
    threshold = 0
    rendered = _render_screen(root_refs, views, threshold, max_nodes=max_nodes)
    while (
        threshold < 8
        and (
            rendered.tokens > budget
            or (max_nodes is not None and rendered.node_count > max_nodes)
        )
    ):
        threshold += 1
        rendered = _render_screen(root_refs, views, threshold, max_nodes=max_nodes)

    return "\n".join([header, rendered.text or "(no relevant nodes)"])


def summarize_step_history(
    step_history: list[dict],
    max_steps: int = _RECENT_HISTORY_WINDOW,
    token_budget: int | None = None,
) -> str:
    """
    Summarize recent execution history for the LLM.

    Recent steps are shown with detail. Older steps are collapsed into a short
    narrative summary so the prompt retains useful context without spending too
    many tokens.
    """
    if not step_history:
        return "(no steps yet)"

    budget = token_budget or int(getattr(settings, "LLM_TOKEN_BUDGET_HISTORY", 2000))
    recent_limit = max(1, min(max_steps, _RECENT_HISTORY_WINDOW))
    recent = list(step_history[-recent_limit:])
    older = list(step_history[:-recent_limit])

    older_line = _summarize_older_steps(older)
    recent_lines = [_format_recent_step(step, include_reasoning=True) for step in recent]
    text = "\n".join([line for line in [older_line, *recent_lines] if line])
    if _estimate_tokens(text) <= budget:
        return text

    recent_lines = [_format_recent_step(step, include_reasoning=False) for step in recent]
    text = "\n".join([line for line in [older_line, *recent_lines] if line])
    if _estimate_tokens(text) <= budget:
        return text

    trimmed_recent = recent[-max(1, recent_limit - 2):]
    text = "\n".join(
        [line for line in [_summarize_older_steps(older + recent[:-len(trimmed_recent)]),
                           *[_format_recent_step(step, include_reasoning=False) for step in trimmed_recent]]
         if line]
    )
    if _estimate_tokens(text) <= budget:
        return text

    compact = _summarize_older_steps(step_history)
    return compact or "(history omitted)"


def _build_header(screen_state: dict) -> str:
    fg = screen_state.get("foreground_package") or "unknown"
    title = screen_state.get("window_title") or "(untitled)"
    focused = screen_state.get("focused_element_ref") or "none"
    node_count = len(screen_state.get("nodes") or [])
    return (
        f"Foreground: {fg} | Window: {title} | Focused: {focused} | "
        f"Visible nodes: {node_count}"
    )


def _index_nodes(nodes: Iterable[dict]) -> dict[str, dict]:
    indexed: dict[str, dict] = {}
    for idx, node in enumerate(nodes):
        ref = str(node.get("ref") or f"anon_{idx}")
        indexed[ref] = node
    return indexed


def _find_root_refs(nodes: Iterable[dict]) -> list[str]:
    all_refs: list[str] = []
    seen_children: set[str] = set()
    for node in nodes:
        ref = str(node.get("ref") or "")
        if ref:
            all_refs.append(ref)
        seen_children.update(_child_refs(node))
    roots = [ref for ref in all_refs if ref not in seen_children]
    return roots or all_refs


def _compute_depths(node_map: dict[str, dict], root_refs: list[str]) -> dict[str, int]:
    depths: dict[str, int] = {}
    queue: deque[tuple[str, int]] = deque((ref, 0) for ref in root_refs if ref in node_map)
    while queue:
        ref, depth = queue.popleft()
        prev = depths.get(ref)
        if prev is not None and prev <= depth:
            continue
        depths[ref] = depth
        for child_ref in _child_refs(node_map.get(ref) or {}):
            if child_ref in node_map:
                queue.append((child_ref, depth + 1))
    for ref in node_map:
        depths.setdefault(ref, 0)
    return depths


def _score_node(node: dict) -> int:
    score = 0
    if _is_true(node, "clickable") or _is_true(node, "editable"):
        score += 3
    if _is_true(node, "scrollable") or _is_true(node, "long_clickable"):
        score += 1
    if _text_value(node, "text") or _text_value(node, "content_desc"):
        score += 2
    if _text_value(node, "view_id"):
        score += 2
    if _is_true(node, "focused"):
        score += 1

    class_name = _short_class_name(_text_value(node, "class_name") or "")
    has_text = bool(_text_value(node, "text") or _text_value(node, "content_desc"))
    if class_name in {"View", "FrameLayout", "LinearLayout"} and not has_text:
        score -= 1
    if _bounds_area(node) < 100:
        score -= 2
    return score


def _threshold_for_depth(depth: int) -> int:
    if depth <= 2:
        return 0
    if depth <= 4:
        return 2
    return 4


@dataclass
class _RenderedScreen:
    text: str
    tokens: int
    node_count: int


def _render_screen(
    root_refs: list[str],
    views: dict[str, _NodeView],
    threshold_boost: int,
    max_nodes: int | None,
) -> _RenderedScreen:
    allowed = {
        ref
        for ref, view in views.items()
        if view.relevance >= (_threshold_for_depth(view.depth) + threshold_boost)
    }

    lines: list[str] = []
    node_count = 0
    truncated = False

    def append_line(line: str) -> bool:
        nonlocal node_count, truncated
        if max_nodes is not None and node_count >= max_nodes:
            truncated = True
            return False
        lines.append(line)
        node_count += 1
        return True

    def walk(ref: str, visible_depth: int) -> None:
        if ref not in views:
            return

        view = views[ref]
        if ref not in allowed:
            for child_ref in _dedupe_child_refs(view.child_refs, views):
                if isinstance(child_ref, tuple):
                    continue
                if isinstance(child_ref, str):
                    walk(child_ref, visible_depth)
            return

        line = f"{'  ' * visible_depth}{_format_node_line(view, views)}"
        if not append_line(line):
            return

        for child_ref in _dedupe_child_refs(view.child_refs, views):
            if isinstance(child_ref, tuple):
                marker = f"{'  ' * (visible_depth + 1)}{child_ref[1]}"
                if not append_line(marker):
                    return
            elif isinstance(child_ref, str):
                walk(child_ref, visible_depth + 1)

    for root_ref in root_refs:
        if truncated:
            break
        walk(root_ref, 0)

    for ref in views:
        if truncated:
            break
        if ref not in root_refs:
            continue

    if truncated:
        lines.append("[... additional nodes omitted]")

    text = "\n".join(lines)
    return _RenderedScreen(text=text, tokens=_estimate_tokens(text), node_count=node_count)


def _dedupe_child_refs(
    child_refs: tuple[str, ...],
    views: dict[str, _NodeView],
) -> list[str | tuple[str, str]]:
    if len(child_refs) <= 5:
        return list(child_refs)

    deduped: list[str | tuple[str, str]] = []
    i = 0
    while i < len(child_refs):
        ref = child_refs[i]
        view = views.get(ref)
        if view is None:
            deduped.append(ref)
            i += 1
            continue

        signature = _sibling_signature(view)
        run: list[str] = [ref]
        j = i + 1
        while j < len(child_refs):
            next_ref = child_refs[j]
            next_view = views.get(next_ref)
            if next_view is None or _sibling_signature(next_view) != signature:
                break
            run.append(next_ref)
            j += 1

        if len(run) > 5:
            deduped.extend(run[:3])
            class_name = _short_class_name(_text_value(view.raw, "class_name") or "item")
            deduped.append(("marker", f"[... {len(run) - 3} more {class_name} items]"))
        else:
            deduped.extend(run)
        i = j
    return deduped


def _sibling_signature(view: _NodeView) -> tuple:
    raw = view.raw
    return (
        _short_class_name(_text_value(raw, "class_name") or ""),
        bool(_text_value(raw, "text")),
        bool(_text_value(raw, "content_desc")),
        _is_true(raw, "clickable"),
        _is_true(raw, "editable"),
        min(len(view.child_refs), 3),
    )


def _format_node_line(view: _NodeView, views: dict[str, _NodeView]) -> str:
    node = view.raw
    parts = [f"[{view.ref}]", _short_class_name(_text_value(node, "class_name") or "View")]
    text = _truncate_inline(_text_value(node, "text"), 48)
    content_desc = _truncate_inline(_text_value(node, "content_desc"), 48)
    view_id = _truncate_inline(_text_value(node, "view_id"), 36)
    descendant_label = _truncate_inline(_best_inherited_label(view, views), 48)
    kind = _infer_node_kind(view, views, descendant_label)
    actions = _action_hints(node)
    region = _region_hint(view, views)
    index_in_parent = _int_value(node, "index_in_parent")
    child_count = _int_value(node, "child_count")

    if text:
        parts.append(f"\"{text}\"")
    if content_desc:
        parts.append(f"contentDesc='{content_desc}'")
    if view_id:
        parts.append(f"id={view_id}")
    if kind:
        parts.append(f"kind={kind}")
    if descendant_label and _is_true(node, "clickable") and not text and not content_desc:
        parts.append(f"label='{descendant_label}'")
    if view.parent_ref:
        parts.append(f"parent={view.parent_ref}")
        parts.append(f"idx={index_in_parent}")
    if child_count > 0 and _is_true(node, "clickable"):
        parts.append(f"children={child_count}")
    if actions:
        parts.append(f"actions={','.join(actions)}")
    if region:
        parts.append(f"region={region}")
    parts.append(f"clickable={str(_is_true(node, 'clickable')).lower()}")
    if _is_true(node, "long_clickable"):
        parts.append("longClickable=true")
    if _is_true(node, "scrollable"):
        parts.append("scrollable=true")
    parts.append(f"editable={str(_is_true(node, 'editable')).lower()}")
    parts.append(f"focused={str(_is_true(node, 'focused')).lower()}")
    parts.append(f"enabled={str(_is_true(node, 'enabled', default=True)).lower()}")
    if _is_true(node, "selected"):
        parts.append("selected=true")
    if _is_true(node, "checkable"):
        parts.append("checkable=true")
    if _is_true(node, "checked"):
        parts.append("checked=true")
    parts.append(f"depth={view.depth}")
    return " ".join(parts)


def _infer_node_kind(view: _NodeView, views: dict[str, _NodeView], descendant_label: str) -> str:
    node = view.raw
    class_name = _short_class_name(_text_value(node, "class_name") or "View")
    class_lower = class_name.lower()
    view_id = _text_value(node, "view_id").lower()

    if "toolbar" in view_id:
        return "toolbar"
    if _is_true(node, "editable") or "edittext" in class_lower or "autocomplete" in class_lower:
        return "input"
    if _is_true(node, "scrollable") or any(
        name in class_lower for name in ("recyclerview", "listview", "scrollview", "viewpager")
    ):
        return "list"
    if _is_true(node, "checkable") or any(
        name in class_lower for name in ("checkbox", "switch", "radiobutton", "togglebutton")
    ):
        return "toggle"
    if "imagebutton" in class_lower or ("imageview" in class_lower and _is_true(node, "clickable")):
        return "icon_button"
    if "button" in class_lower:
        return "button"
    if "textview" in class_lower:
        if "title" in view_id:
            return "title"
        if "subtitle" in view_id:
            return "subtitle"
        if "header" in view_id:
            return "header"
        return "text"
    if descendant_label and _is_true(node, "clickable"):
        return "row"
    if _is_true(node, "clickable"):
        return "control"
    return "container"


def _action_hints(node: dict) -> list[str]:
    hints: list[str] = []
    if _is_true(node, "clickable"):
        hints.append("tap")
    if _is_true(node, "long_clickable"):
        hints.append("long_press")
    if _is_true(node, "editable"):
        hints.extend(["focus", "type"])
    if _is_true(node, "checkable"):
        hints.append("toggle")
    if _is_true(node, "scrollable"):
        hints.append("scroll")
    return hints


def _region_hint(view: _NodeView, views: dict[str, _NodeView]) -> str:
    bounds = view.raw.get("bounds") or {}
    if not isinstance(bounds, dict):
        return ""

    bottom = int(bounds.get("bottom", 0))
    top = int(bounds.get("top", 0))
    max_bottom = max(
        int((candidate.raw.get("bounds") or {}).get("bottom", 0))
        for candidate in views.values()
    )
    if max_bottom <= 0:
        return ""

    center = (top + bottom) / 2
    if center <= max_bottom / 3:
        return "top"
    if center >= (max_bottom * 2) / 3:
        return "bottom"
    return "middle"


def _first_descendant_label(view: _NodeView, views: dict[str, _NodeView]) -> str:
    queue: deque[str] = deque(view.child_refs)
    seen: set[str] = set()
    best_content_desc = ""

    while queue:
        ref = queue.popleft()
        if ref in seen:
            continue
        seen.add(ref)

        child = views.get(ref)
        if child is None:
            continue

        text = _text_value(child.raw, "text")
        if text:
            return text

        if not best_content_desc:
            content_desc = _text_value(child.raw, "content_desc")
            if content_desc:
                best_content_desc = content_desc

        queue.extend(child.child_refs)

    return best_content_desc


def _best_inherited_label(view: _NodeView, views: dict[str, _NodeView]) -> str:
    descendant_label = _first_descendant_label(view, views)
    if descendant_label:
        return descendant_label
    return _flat_sibling_label(view, views)


def _flat_sibling_label(view: _NodeView, views: dict[str, _NodeView]) -> str:
    ordered = sorted(views.values(), key=lambda item: item.order)
    max_lookahead = 5
    fallback = ""

    for candidate in ordered[view.order + 1:view.order + 1 + max_lookahead]:
        raw = candidate.raw
        if _is_true(raw, "clickable"):
            break
        text = _text_value(raw, "text")
        if text:
            if not fallback:
                fallback = text
            if not _looks_like_section_header(text):
                return text
        content_desc = _text_value(raw, "content_desc")
        if content_desc:
            if not fallback:
                fallback = content_desc
            if not _looks_like_section_header(content_desc):
                return content_desc

    return fallback


def _looks_like_section_header(value: str) -> bool:
    normalized = " ".join(str(value or "").split())
    if not normalized:
        return False

    if any(ch.isdigit() for ch in normalized):
        return False

    letters = "".join(ch for ch in normalized if ch.isalpha())
    return bool(letters) and letters.isupper() and 3 <= len(letters) <= 24


def _summarize_older_steps(steps: list[dict]) -> str:
    if not steps:
        return ""
    phrases = [_history_phrase(step) for step in steps[-4:]]
    if not phrases:
        return f"Earlier steps: {len(steps)} step(s)."
    prefix = "Earlier steps: "
    suffix = ""
    omitted = len(steps) - len(phrases)
    if omitted > 0:
        suffix = f", plus {omitted} earlier step(s)"
    return prefix + ", ".join(phrases) + suffix + "."


def _history_phrase(step: dict) -> str:
    action_type = str(step.get("action_type") or "UNKNOWN")
    params = step.get("params") or {}
    success = bool(step.get("result_success"))
    result_code = str(step.get("result_code") or "")

    if action_type == "OPEN_APP":
        package_name = (
            params.get("package_name")
            or params.get("package")
            or "target app"
        )
        phrase = f"opened {package_name}"
    elif action_type in {"TAP_ELEMENT", "LONG_PRESS_ELEMENT", "FOCUS_ELEMENT"}:
        ref = ((params.get("selector") or {}).get("element_ref")) or "an element"
        phrase = f"interacted with {ref}"
    elif action_type == "TYPE_TEXT":
        phrase = "typed into a field"
    elif action_type == "SCROLL":
        phrase = f"scrolled {params.get('direction') or 'screen'}"
    elif action_type == "BACK":
        phrase = "went back"
    else:
        phrase = action_type.lower().replace("_", " ")

    if not success:
        return f"{phrase} (failed: {result_code or 'UNKNOWN'})"
    return phrase


def _format_recent_step(step: dict, include_reasoning: bool) -> str:
    index = step.get("step_index", "?")
    action_type = str(step.get("action_type") or "UNKNOWN")
    params = step.get("params") or {}
    success = bool(step.get("result_success"))
    result_code = str(step.get("result_code") or ("OK" if success else "UNKNOWN"))
    detail = _summarize_params(params)
    status = "SUCCESS" if success else f"FAILED ({result_code})"
    line = f"Step {index}: {action_type} {detail} -> {status}"
    reasoning = _truncate_inline(str(step.get("reasoning") or ""), 100)
    if include_reasoning and reasoning:
        line += f" | reasoning: {reasoning}"
    return line


def _summarize_params(params: dict) -> str:
    if not params:
        return "{}"
    parts: list[str] = []
    selector = params.get("selector") or {}
    if isinstance(selector, dict) and selector.get("element_ref"):
        parts.append(f"ref={selector['element_ref']}")
    if "text" in params:
        parts.append("text")
    if "package_name" in params:
        parts.append(f"package={params['package_name']}")
    elif "package" in params:
        parts.append(f"package={params['package']}")
    if "direction" in params:
        parts.append(f"direction={params['direction']}")
    if "action_summary" in params:
        parts.append(_truncate_inline(str(params["action_summary"]), 40))
    if not parts:
        parts.append(str(params)[:40])
    return "{" + ", ".join(parts) + "}"


def _effective_screen_budget(token_budget: int | None) -> int:
    configured = int(getattr(settings, "LLM_TOKEN_BUDGET_SCREEN_STATE", 6000))
    max_context = int(getattr(settings, "LLM_MAX_CONTEXT", 12000))
    system_budget = int(getattr(settings, "LLM_TOKEN_BUDGET_SYSTEM_PROMPT", 2000))
    history_budget = int(getattr(settings, "LLM_TOKEN_BUDGET_HISTORY", 2000))
    response_budget = int(getattr(settings, "LLM_TOKEN_BUDGET_RESPONSE", 500))

    available = max(1000, max_context - system_budget - history_budget - response_budget)
    effective = min(token_budget or configured, configured, available)
    if max_context <= 16000:
        effective = min(effective, max(1800, available))
    return max(1000, effective)


def _estimate_tokens(text: str) -> int:
    if not text:
        return 0
    return max(1, len(text) // 4)


def _child_refs(node: dict) -> list[str]:
    children = node.get("children") or node.get("child_refs") or []
    refs: list[str] = []
    for child in children:
        if isinstance(child, str):
            refs.append(child)
        elif isinstance(child, dict) and child.get("ref"):
            refs.append(str(child["ref"]))
    return refs


def _text_value(node: dict, snake_name: str) -> str:
    camel = {
        "class_name": "className",
        "content_desc": "contentDesc",
        "view_id": "viewId",
        "parent_ref": "parentRef",
    }.get(snake_name)
    value = node.get(snake_name)
    if not value and camel:
        value = node.get(camel)
    return str(value or "")


def _int_value(node: dict, snake_name: str) -> int:
    camel = {
        "index_in_parent": "indexInParent",
        "child_count": "childCount",
    }.get(snake_name)
    value = node.get(snake_name)
    if value is None and camel:
        value = node.get(camel)
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _is_true(node: dict, snake_name: str, default: bool = False) -> bool:
    camel = {
        "content_desc": "contentDesc",
        "view_id": "viewId",
        "class_name": "className",
        "parent_ref": "parentRef",
        "long_clickable": "longClickable",
        "index_in_parent": "indexInParent",
        "child_count": "childCount",
    }.get(snake_name, snake_name)
    if snake_name in node:
        return bool(node.get(snake_name))
    if camel in node:
        return bool(node.get(camel))
    return default


def _bounds_area(node: dict) -> int:
    bounds = node.get("bounds") or {}
    if isinstance(bounds, dict):
        left = int(bounds.get("left", 0))
        top = int(bounds.get("top", 0))
        right = int(bounds.get("right", 0))
        bottom = int(bounds.get("bottom", 0))
        return max(0, right - left) * max(0, bottom - top)
    if isinstance(bounds, (list, tuple)) and len(bounds) == 4:
        left, top, right, bottom = bounds
        return max(0, int(right) - int(left)) * max(0, int(bottom) - int(top))
    return 0


def _short_class_name(class_name: str) -> str:
    if not class_name:
        return "View"
    return class_name.rsplit(".", 1)[-1]


def _truncate_inline(value: str, limit: int) -> str:
    value = " ".join(str(value or "").split())
    if len(value) <= limit:
        return value
    return value[: limit - 3] + "..."
