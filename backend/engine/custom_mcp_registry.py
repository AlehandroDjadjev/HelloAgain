from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict


class CustomMcpRegistry:
    def __init__(self, base_dir: Path | None = None) -> None:
        self.base_dir = base_dir or Path(__file__).resolve().parent.parent / "config" / "custom_mcps"
        self.registry_path = self.base_dir / "registry.json"

    def _read_json(self, path: Path) -> Dict[str, Any]:
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        if not isinstance(payload, dict):
            raise ValueError(f"Custom MCP config must be a JSON object: {path}")
        return payload

    @staticmethod
    def _join_url(base_url: str, path: str) -> str:
        if not base_url:
            return path
        return base_url.rstrip("/") + path

    def load_registry(self, *, base_url: str = "") -> Dict[str, Any]:
        payload = deepcopy(self._read_json(self.registry_path))
        for item in payload.get("mcps", []):
            if not isinstance(item, dict):
                continue
            mcp_id = str(item.get("id", "")).strip()
            if not mcp_id:
                continue
            descriptor_url = str(item.get("descriptor_url") or f"/api/agent/mcps/{mcp_id}/")
            item["descriptor_url"] = self._join_url(base_url, descriptor_url)
            item["invoke_url"] = self._join_url(base_url, f"/api/agent/mcps/{mcp_id}/invoke/")
        return payload

    def load_descriptor(self, mcp_id: str, *, base_url: str = "") -> Dict[str, Any]:
        clean_id = str(mcp_id or "").strip()
        if not clean_id:
            raise ValueError("mcp_id is required")
        path = self.base_dir / f"{clean_id}.json"
        payload = deepcopy(self._read_json(path))
        payload["descriptor_url"] = self._join_url(base_url, f"/api/agent/mcps/{clean_id}/")
        payload["invoke_url"] = self._join_url(base_url, f"/api/agent/mcps/{clean_id}/invoke/")
        for tool in payload.get("tools", []):
            if not isinstance(tool, dict):
                continue
            raw_path = str(tool.get("path", "")).strip()
            if raw_path:
                tool["url"] = self._join_url(base_url, raw_path)
        return payload
