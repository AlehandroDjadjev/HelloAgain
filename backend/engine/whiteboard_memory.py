from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, List


def _to_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _to_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    if value is None:
        return default
    return bool(value)


class WhiteboardMemoryStore:
    DEFAULT_BOARD = {"width": 1000.0, "height": 700.0}
    MEMORY_TYPES = {"instant", "ram", "memory"}

    def __init__(self, memory_dir: Path | None = None) -> None:
        self.memory_dir = memory_dir or Path(__file__).resolve().parent.parent / "memory"
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        self.board_state_path = self.memory_dir / "board_state.json"
        self.result_map_path = self.memory_dir / "object_results.json"
        self.runtime_result_map: Dict[str, Dict[str, Any]] = {}
        self._ensure_files()

    def _ensure_files(self) -> None:
        if not self.board_state_path.exists():
            self._write_json(
                self.board_state_path,
                {
                    "board": dict(self.DEFAULT_BOARD),
                    "objects": [],
                },
            )
        if not self.result_map_path.exists():
            self._write_json(self.result_map_path, {"results": {}})

    def _read_json(self, path: Path, default: Dict[str, Any]) -> Dict[str, Any]:
        if not path.exists():
            return deepcopy(default)
        with path.open("r", encoding="utf-8") as handle:
            try:
                payload = json.load(handle)
            except json.JSONDecodeError:
                return deepcopy(default)
        return payload if isinstance(payload, dict) else deepcopy(default)

    def _write_json(self, path: Path, payload: Dict[str, Any]) -> None:
        with path.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=True, indent=2, sort_keys=True)

    def _clean_name(self, value: Any) -> str:
        return " ".join(str(value or "").strip().split())

    def normalize_board_state(self, payload: Dict[str, Any] | None) -> Dict[str, Any]:
        payload = payload if isinstance(payload, dict) else {}
        board = payload.get("board") if isinstance(payload.get("board"), dict) else {}
        width = max(320.0, _to_float(board.get("width"), self.DEFAULT_BOARD["width"]))
        height = max(240.0, _to_float(board.get("height"), self.DEFAULT_BOARD["height"]))

        objects: List[Dict[str, Any]] = []
        seen: set[str] = set()
        for raw in payload.get("objects") or []:
            if not isinstance(raw, dict):
                continue
            bbox = raw.get("bbox") if isinstance(raw.get("bbox"), dict) else {}
            name = self._clean_name(raw.get("name") or raw.get("id"))
            if not name or name in seen:
                continue
            seen.add(name)

            object_width = max(24.0, _to_float(raw.get("width", bbox.get("width")), 140.0))
            object_height = max(24.0, _to_float(raw.get("height", bbox.get("height")), 120.0))
            x = _to_float(raw.get("x", bbox.get("x")), 0.0)
            y = _to_float(raw.get("y", bbox.get("y")), 0.0)
            x = min(max(0.0, x), max(0.0, width - object_width))
            y = min(max(0.0, y), max(0.0, height - object_height))

            memory_type = str(raw.get("memoryType") or raw.get("memory_type") or "ram").strip().lower()
            if memory_type not in self.MEMORY_TYPES:
                memory_type = "ram"

            tags = []
            for item in raw.get("tags") or []:
                clean = self._clean_name(item)
                if clean:
                    tags.append(clean)

            extra_data = raw.get("extraData", raw.get("extra_data"))
            if not isinstance(extra_data, dict):
                extra_data = {}

            objects.append(
                {
                    "name": name,
                    "text": self._clean_name(raw.get("text") or name),
                    "x": x,
                    "y": y,
                    "width": object_width,
                    "height": object_height,
                    "baseScale": max(0.15, min(8.0, _to_float(raw.get("baseScale", raw.get("base_scale")), 1.0))),
                    "innerInset": max(0.0, _to_float(raw.get("innerInset", raw.get("inner_inset")), 12.0)),
                    "color": raw.get("color"),
                    "memoryType": memory_type,
                    "resultId": self._clean_name(raw.get("resultId") or raw.get("result_id")) or None,
                    "deleteAfterClick": _to_bool(
                        raw.get("deleteAfterClick", raw.get("delete_after_click")),
                        default=memory_type == "instant",
                    ),
                    "tags": tags,
                    "extraData": deepcopy(extra_data),
                    "bbox": {
                        "x": x,
                        "y": y,
                        "width": object_width,
                        "height": object_height,
                    },
                }
            )

        return {
            "board": {"width": width, "height": height},
            "objects": objects,
        }

    def load_persistent_board_state(self) -> Dict[str, Any]:
        payload = self._read_json(
            self.board_state_path,
            {"board": dict(self.DEFAULT_BOARD), "objects": []},
        )
        return self.normalize_board_state(payload)

    def load_persistent_results(self) -> Dict[str, Dict[str, Any]]:
        payload = self._read_json(self.result_map_path, {"results": {}})
        results = payload.get("results")
        return results if isinstance(results, dict) else {}

    def find_largest_empty_space(self, board_state: Dict[str, Any] | None) -> Dict[str, Any]:
        state = self.normalize_board_state(board_state)
        width = state["board"]["width"]
        height = state["board"]["height"]

        xs = {0.0, width}
        ys = {0.0, height}
        for obj in state["objects"]:
            xs.add(float(obj["x"]))
            xs.add(float(obj["x"] + obj["width"]))
            ys.add(float(obj["y"]))
            ys.add(float(obj["y"] + obj["height"]))

        sorted_x = sorted(xs)
        sorted_y = sorted(ys)
        best_rect: Dict[str, float] | None = None
        best_area = -1.0

        for left_index, left in enumerate(sorted_x):
            for right in sorted_x[left_index + 1 :]:
                if right <= left:
                    continue
                for top_index, top in enumerate(sorted_y):
                    for bottom in sorted_y[top_index + 1 :]:
                        if bottom <= top:
                            continue
                        candidate = {
                            "x": left,
                            "y": top,
                            "width": right - left,
                            "height": bottom - top,
                        }
                        if self._overlaps_any(candidate, state["objects"]):
                            continue
                        area = candidate["width"] * candidate["height"]
                        if area > best_area:
                            best_area = area
                            best_rect = candidate

        return {
            "ok": True,
            "action": "findLargestEmptySpace",
            "board": dict(state["board"]),
            "bbox": best_rect,
        }

    def _overlaps_any(self, candidate: Dict[str, float], objects: List[Dict[str, Any]]) -> bool:
        for obj in objects:
            rect = {
                "x": float(obj["x"]),
                "y": float(obj["y"]),
                "width": float(obj["width"]),
                "height": float(obj["height"]),
            }
            if self.rectangles_overlap(candidate, rect):
                return True
        return False

    @staticmethod
    def rectangles_overlap(first: Dict[str, float], second: Dict[str, float]) -> bool:
        return (
            first["x"] < second["x"] + second["width"]
            and first["x"] + first["width"] > second["x"]
            and first["y"] < second["y"] + second["height"]
            and first["y"] + first["height"] > second["y"]
        )

    def apply_commands(self, board_state: Dict[str, Any] | None, commands: List[Dict[str, Any]]) -> Dict[str, Any]:
        state = self.normalize_board_state(board_state)
        board = state["board"]
        ordered_names = [obj["name"] for obj in state["objects"]]
        objects = {obj["name"]: deepcopy(obj) for obj in state["objects"]}

        for command in commands:
            if not isinstance(command, dict):
                continue
            action = str(command.get("action", "")).strip()
            name = self._clean_name(command.get("name"))
            if action == "create":
                if not name:
                    continue
                new_object = self.normalize_board_state(
                    {
                        "board": board,
                        "objects": [command],
                    }
                )["objects"][0]
                objects[name] = new_object
                if name not in ordered_names:
                    ordered_names.append(name)
                continue

            if not name or name not in objects:
                continue
            current = objects[name]

            if action == "move":
                target_x = min(
                    max(0.0, _to_float(command.get("x"), current["x"])),
                    max(0.0, board["width"] - current["width"]),
                )
                target_y = min(
                    max(0.0, _to_float(command.get("y"), current["y"])),
                    max(0.0, board["height"] - current["height"]),
                )
                current["x"] = target_x
                current["y"] = target_y
            elif action in {"enlarge", "shrink"}:
                default_factor = 1.2 if action == "enlarge" else 0.85
                factor = _to_float(command.get("factor"), default_factor)
                current["baseScale"] = max(0.15, min(8.0, current["baseScale"] * factor))
            elif action == "delete":
                objects.pop(name, None)
                ordered_names = [item for item in ordered_names if item != name]
                continue

            current["bbox"] = {
                "x": current["x"],
                "y": current["y"],
                "width": current["width"],
                "height": current["height"],
            }

        return {
            "board": dict(board),
            "objects": [objects[name] for name in ordered_names if name in objects],
        }

    def save_persistent_board_state(self, board_state: Dict[str, Any]) -> Dict[str, Any]:
        normalized = self.normalize_board_state(board_state)
        memory_objects = [obj for obj in normalized["objects"] if obj.get("memoryType") == "memory"]
        payload = {
            "board": dict(normalized["board"]),
            "objects": memory_objects,
        }
        self._write_json(self.board_state_path, payload)
        self._prune_persistent_results({obj.get("resultId") for obj in memory_objects if obj.get("resultId")})
        return payload

    def _prune_persistent_results(self, active_result_ids: set[str]) -> None:
        results = self.load_persistent_results()
        filtered = {
            result_id: payload
            for result_id, payload in results.items()
            if result_id in active_result_ids
        }
        self._write_json(self.result_map_path, {"results": filtered})

    def register_result_bindings(self, bindings: List[Dict[str, Any]]) -> None:
        if not bindings:
            return

        persistent_results = self.load_persistent_results()
        persistent_changed = False
        for binding in bindings:
            if not isinstance(binding, dict):
                continue
            result_id = self._clean_name(binding.get("result_id") or binding.get("resultId"))
            if not result_id:
                continue
            memory_type = str(binding.get("memory_type") or binding.get("memoryType") or "ram").strip().lower()
            if memory_type not in self.MEMORY_TYPES:
                memory_type = "ram"
            payload = deepcopy(binding)
            payload["result_id"] = result_id
            payload["memory_type"] = memory_type
            if memory_type == "memory":
                persistent_results[result_id] = payload
                persistent_changed = True
            else:
                self.runtime_result_map[result_id] = payload

        if persistent_changed:
            self._write_json(self.result_map_path, {"results": persistent_results})

    def resolve_result_binding(self, result_id: str | None) -> Dict[str, Any] | None:
        clean_id = self._clean_name(result_id)
        if not clean_id:
            return None
        if clean_id in self.runtime_result_map:
            return deepcopy(self.runtime_result_map[clean_id])
        persistent = self.load_persistent_results()
        if clean_id in persistent:
            return deepcopy(persistent[clean_id])
        return None

    def remove_result_binding(self, result_id: str | None) -> None:
        clean_id = self._clean_name(result_id)
        if not clean_id:
            return
        self.runtime_result_map.pop(clean_id, None)
        persistent = self.load_persistent_results()
        if clean_id in persistent:
            persistent.pop(clean_id, None)
            self._write_json(self.result_map_path, {"results": persistent})
