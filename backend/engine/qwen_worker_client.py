from __future__ import annotations

import json
from typing import Any, Dict
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .qwen_config import QwenConfig


class QwenWorkerClient:
    def __init__(self, config: QwenConfig | None = None) -> None:
        self.config = config or QwenConfig()

    def _get_json(self, path: str, timeout: float) -> Dict[str, Any]:
        url = self.config.server_base_url.rstrip("/") + path
        request = Request(url, method="GET")
        try:
            with urlopen(request, timeout=timeout) as response:
                raw = response.read().decode("utf-8")
        except HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Qwen server HTTP {exc.code}: {raw}") from exc
        except URLError as exc:
            raise RuntimeError(
                "Could not reach the Qwen server. Start it manually with `python main.py` "
                f"or set QWEN_SERVER_URL. Target was {url}."
            ) from exc

        try:
            return json.loads(raw or "{}")
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Qwen server returned invalid JSON: {raw[:1000]}") from exc

    def _post_json(self, path: str, payload: Dict[str, Any], timeout: float) -> Dict[str, Any]:
        url = self.config.server_base_url.rstrip("/") + path
        body = json.dumps(payload).encode("utf-8")
        request = Request(
            url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=timeout) as response:
                raw = response.read().decode("utf-8")
        except HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Qwen server HTTP {exc.code}: {raw}") from exc
        except URLError as exc:
            raise RuntimeError(
                "Could not reach the Qwen server. Start it manually with `python main.py` "
                f"or set QWEN_SERVER_URL. Target was {url}."
            ) from exc

        try:
            return json.loads(raw or "{}")
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Qwen server returned invalid JSON: {raw[:1000]}") from exc

    def generate(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        generation_overrides: Dict[str, Any] | None = None,
    ) -> str:
        generation: Dict[str, Any] = {
            "max_new_tokens": self.config.max_new_tokens,
            "repetition_penalty": self.config.repetition_penalty,
            "do_sample": self.config.do_sample,
            "json_continuation_budget": self.config.json_continuation_budget,
            "json_continuation_chunk": self.config.json_continuation_chunk,
        }
        if generation_overrides:
            generation.update(generation_overrides)
        if self.config.do_sample:
            generation["temperature"] = self.config.temperature
            generation["top_p"] = self.config.top_p

        payload = self._post_json(
            "/generate",
            {
                "messages": [
                    {
                        "role": "system",
                        "content": system_prompt,
                    },
                    {
                        "role": "user",
                        "content": user_prompt,
                    },
                ],
                "generation": generation,
            },
            timeout=self.config.worker_request_timeout,
        )
        return str(payload.get("raw_text", ""))

    def health(self) -> Dict[str, Any]:
        return self._get_json("/health", timeout=self.config.worker_startup_timeout)
