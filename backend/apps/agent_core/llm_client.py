"""
LLMClient - unified interface over multiple LLM backends.

Supported providers (configure via Django settings):
  "ollama"        local Ollama API  (default: http://localhost:11434)
  "groq"          Groq cloud        (https://api.groq.com/openai/v1)
  "openai"        OpenAI-compatible (https://api.openai.com/v1)
  "transformers"  local HuggingFace transformers (lazy-loaded, GPU optional)

Settings keys:
  LLM_PROVIDER   str   default "ollama"
  LLM_MODEL      str   default provider-specific
  LLM_API_KEY    str   default ""
  LLM_BASE_URL   str   default provider-specific
  LLM_TIMEOUT    int   default 30  (seconds per request)

Usage:
  from apps.agent_core.llm_client import LLMClient
  client = LLMClient.from_settings()
  result = client.generate(system_prompt="...", user_prompt="...", json_mode=True)
  # result is a dict (parsed JSON) or raises LLMError
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from threading import Thread
from typing import Any

import requests

logger = logging.getLogger(__name__)


class LLMError(Exception):
    """Raised when the LLM call fails after retries."""


class LLMParseError(LLMError):
    """Raised when the LLM response cannot be parsed as JSON."""


_PROVIDER_DEFAULTS: dict[str, dict] = {
    "ollama": {"base_url": "http://localhost:11434", "model": "qwen2.5:14b"},
    "groq": {"base_url": "https://api.groq.com/openai/v1", "model": "llama-3.1-8b-instant"},
    "openai": {"base_url": "https://api.openai.com/v1", "model": "gpt-5-mini"},
    "transformers": {"base_url": "", "model": "Qwen/Qwen3-14B"},
}


@dataclass
class LLMResponse:
    content: str
    raw: dict


@dataclass
class LocalModelSnapshot:
    source: str
    revision: str = ""
    shard_count: int = 0
    missing_shards: list[str] | None = None
    total_bytes: int = 0
    aux_missing: list[str] | None = None

    @property
    def is_complete(self) -> bool:
        return not (self.missing_shards or self.aux_missing)


class LLMClient:
    """
    Unified LLM interface. Call generate() - everything else is internal.
    """

    def __init__(
        self,
        provider: str = "ollama",
        model: str | None = None,
        api_key: str = "",
        base_url: str | None = None,
        timeout: int = 30,
    ) -> None:
        self.provider = provider.lower()
        defaults = _PROVIDER_DEFAULTS.get(self.provider, {})
        self.model = model or defaults.get("model", "llama3.2")
        self.api_key = api_key
        self.base_url = (base_url or defaults.get("base_url", "")).rstrip("/")
        self.timeout = timeout

        self._tf_pipeline = None
        self._tf_runtime_state: dict[str, Any] = {}

    @classmethod
    def from_settings(cls) -> "LLMClient":
        from django.conf import settings

        return cls(
            provider=getattr(settings, "LLM_PROVIDER", "ollama"),
            model=getattr(settings, "LLM_MODEL", None),
            api_key=getattr(settings, "LLM_API_KEY", ""),
            base_url=getattr(settings, "LLM_BASE_URL", None),
            timeout=getattr(settings, "LLM_TIMEOUT", 30),
        )

    @classmethod
    def from_reasoning_provider(cls, reasoning_provider: str | None) -> "LLMClient":
        from django.conf import settings

        provider_key = str(reasoning_provider or "local").lower()
        if provider_key == "openai":
            return cls(
                provider="openai",
                model=getattr(settings, "OPENAI_LLM_MODEL", "gpt-5-mini"),
                api_key=getattr(settings, "OPENAI_LLM_API_KEY", ""),
                base_url=getattr(settings, "OPENAI_LLM_BASE_URL", None),
                timeout=getattr(
                    settings,
                    "OPENAI_LLM_TIMEOUT",
                    getattr(settings, "LLM_TIMEOUT", 30),
                ),
            )

        if provider_key == "local":
            return cls(
                provider=getattr(settings, "LOCAL_LLM_PROVIDER", "transformers"),
                model=getattr(settings, "LOCAL_LLM_MODEL", "Qwen/Qwen3-14B"),
                api_key=getattr(settings, "LOCAL_LLM_API_KEY", ""),
                base_url=getattr(settings, "LOCAL_LLM_BASE_URL", None),
                timeout=getattr(
                    settings,
                    "LOCAL_LLM_TIMEOUT",
                    getattr(settings, "LLM_TIMEOUT", 30),
                ),
            )

        raise LLMError(f"Unknown reasoning provider: {reasoning_provider!r}")

    def generate(
        self,
        system_prompt: str,
        user_prompt: str,
        json_mode: bool = False,
        json_schema: dict | None = None,
    ) -> dict:
        """
        Call the configured LLM and return a parsed dict.

        json_mode=True instructs the provider to return JSON only.
        json_schema is passed as structured-output schema where supported.

        Raises LLMError on network failure after 1 retry.
        Raises LLMParseError if the response cannot be JSON-decoded after 1 retry.
        """
        last_error: Exception | None = None
        strict_retry = False

        for attempt in range(2):
            sp = system_prompt
            up = user_prompt
            if strict_retry:
                sp = (
                    sp
                    + "\n\nCRITICAL: Your previous response was not valid JSON. "
                    + "Output ONLY a JSON object. No prose, no markdown code fences, no explanation."
                )
                up = up + "\n\nRemember: output ONLY valid JSON."

            try:
                raw_content = self._call(sp, up, json_mode=json_mode, json_schema=json_schema)
                return _parse_json(raw_content)
            except LLMParseError as exc:
                logger.warning("LLM JSON parse error (attempt %d): %s", attempt + 1, exc)
                last_error = exc
                strict_retry = True
                time.sleep(0.5)
            except LLMError as exc:
                logger.warning("LLM call error (attempt %d): %s", attempt + 1, exc)
                last_error = exc
                time.sleep(1)

        raise last_error or LLMError("LLM call failed after retries")

    def _call(
        self,
        system_prompt: str,
        user_prompt: str,
        json_mode: bool,
        json_schema: dict | None,
    ) -> str:
        if self.provider == "ollama":
            return self._call_ollama(system_prompt, user_prompt, json_mode)
        if self.provider in ("groq", "openai"):
            return self._call_openai_compat(system_prompt, user_prompt, json_mode, json_schema)
        if self.provider == "transformers":
            return self._call_transformers(system_prompt, user_prompt, json_mode=json_mode)
        raise LLMError(f"Unknown LLM provider: {self.provider!r}")

    def _call_ollama(self, system_prompt: str, user_prompt: str, json_mode: bool) -> str:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "stream": False,
        }
        if json_mode:
            payload["format"] = "json"

        try:
            resp = requests.post(
                f"{self.base_url}/api/chat",
                json=payload,
                timeout=self.timeout,
            )
            resp.raise_for_status()
        except requests.Timeout as exc:
            raise LLMError(f"Ollama timeout after {self.timeout}s") from exc
        except requests.HTTPError as exc:
            raise LLMError(f"Ollama HTTP {exc.response.status_code}") from exc
        except requests.RequestException as exc:
            raise LLMError(f"Ollama request error: {exc}") from exc

        data = resp.json()
        return data["message"]["content"]

    def _call_openai_compat(
        self,
        system_prompt: str,
        user_prompt: str,
        json_mode: bool,
        json_schema: dict | None,
    ) -> str:
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }

        if json_schema:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "intent_response",
                    "strict": True,
                    "schema": json_schema,
                },
            }
        elif json_mode:
            payload["response_format"] = {"type": "json_object"}

        try:
            resp = requests.post(
                f"{self.base_url}/chat/completions",
                headers=headers,
                json=payload,
                timeout=self.timeout,
            )
            resp.raise_for_status()
        except requests.Timeout as exc:
            raise LLMError(f"OpenAI-compat timeout after {self.timeout}s") from exc
        except requests.HTTPError as exc:
            raise LLMError(
                f"OpenAI-compat HTTP {exc.response.status_code}: {exc.response.text[:200]}"
            ) from exc
        except requests.RequestException as exc:
            raise LLMError(f"OpenAI-compat request error: {exc}") from exc

        data = resp.json()
        return data["choices"][0]["message"]["content"]

    def _call_transformers(
        self,
        system_prompt: str,
        user_prompt: str,
        json_mode: bool = False,
    ) -> str:
        """
        Lazily loads the model + tokenizer on first call.
        Uses the model's own chat template so it works correctly with Qwen,
        Llama, Mistral, and other instruction models.
        """
        model_obj, tokenizer = self._get_tf_model()
        started = time.perf_counter()
        max_new_tokens = _transformers_max_new_tokens(json_mode)
        use_streaming_logs = _get_bool_env("LOCAL_LLM_LOG_STREAMING", False)

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        text = tokenizer.apply_chat_template(
            messages,
            tokenize=False,
            add_generation_prompt=True,
        )
        input_device = _infer_model_input_device(model_obj)
        inputs = _encode_transformers_inputs(tokenizer, text, input_device)
        prompt_tokens = int(inputs["input_ids"].shape[1])

        self._log_transformers_inference_start(
            prompt_chars=len(text),
            prompt_tokens=prompt_tokens,
            input_device=str(input_device),
            max_new_tokens=max_new_tokens,
            use_streaming_logs=use_streaming_logs,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
        )

        import torch

        with torch.inference_mode():
            if use_streaming_logs:
                output_text = self._generate_with_streaming_logs(
                    model_obj,
                    tokenizer,
                    inputs,
                    max_new_tokens=max_new_tokens,
                )
            else:
                output_ids = model_obj.generate(
                    **inputs,
                    max_new_tokens=max_new_tokens,
                    do_sample=False,
                    temperature=None,
                    top_p=None,
                    pad_token_id=_tokenizer_attr(tokenizer, "eos_token_id"),
                )
                new_ids = output_ids[0][prompt_tokens:]
                output_text = tokenizer.decode(new_ids, skip_special_tokens=True).strip()

        self._log_transformers_inference_end(
            output_text=output_text,
            prompt_tokens=prompt_tokens,
            elapsed_s=time.perf_counter() - started,
        )
        return output_text

    def _generate_with_streaming_logs(
        self,
        model_obj,
        tokenizer,
        inputs,
        *,
        max_new_tokens: int,
    ) -> str:
        from transformers import TextIteratorStreamer  # type: ignore[import]

        streamer = TextIteratorStreamer(
            tokenizer,
            skip_prompt=True,
            skip_special_tokens=True,
        )
        error_holder: list[BaseException] = []

        def _run_generate() -> None:
            try:
                model_obj.generate(
                    **inputs,
                    max_new_tokens=max_new_tokens,
                    do_sample=False,
                    temperature=None,
                    top_p=None,
                    pad_token_id=_tokenizer_attr(tokenizer, "eos_token_id"),
                    streamer=streamer,
                )
            except BaseException as exc:  # pragma: no cover
                error_holder.append(exc)

        worker = Thread(target=_run_generate, daemon=True)
        worker.start()

        accumulated = ""
        last_logged_chars = 0
        chunk_interval = int(os.environ.get("LOCAL_LLM_STREAM_LOG_EVERY_CHARS", "120"))
        preview_chars = int(os.environ.get("LOCAL_LLM_LOG_OUTPUT_PREVIEW_CHARS", "240"))

        for chunk in streamer:
            accumulated += chunk
            if (len(accumulated) - last_logged_chars) >= chunk_interval:
                logger.debug(
                    "Transformers generation progress model=%s chars=%d preview=%r",
                    self.model,
                    len(accumulated),
                    accumulated[-preview_chars:],
                )
                last_logged_chars = len(accumulated)

        worker.join()
        if error_holder:
            raise LLMError(f"Transformers generation error: {error_holder[0]}")
        return accumulated.strip()

    def _get_tf_model(self):
        """Return (model, tokenizer), loading once and caching on the instance."""
        if self._tf_pipeline is not None:
            return self._tf_pipeline

        try:
            from transformers import (  # type: ignore[import]
                AutoConfig,
                AutoModelForCausalLM,
                AutoModelForImageTextToText,
                AutoProcessor,
                AutoTokenizer,
            )
            import torch  # type: ignore[import]
        except ImportError as exc:
            raise LLMError(
                "transformers / torch not installed. Run: pip install transformers torch accelerate"
            ) from exc

        source, local_snapshot, local_files_only = self._resolve_transformers_source()
        self._log_transformers_load_start(source, local_snapshot, local_files_only)
        model_config = AutoConfig.from_pretrained(
            source,
            trust_remote_code=True,
            local_files_only=local_files_only,
        )
        uses_vl_processor = _is_vl_model_config(model_config)

        if uses_vl_processor:
            tokenizer = AutoProcessor.from_pretrained(
                source,
                trust_remote_code=True,
                local_files_only=local_files_only,
            )
            model_loader = AutoModelForImageTextToText
        else:
            tokenizer = AutoTokenizer.from_pretrained(
                source,
                trust_remote_code=True,
                local_files_only=local_files_only,
            )
            model_loader = AutoModelForCausalLM

        load_started = time.perf_counter()
        try:
            from transformers import BitsAndBytesConfig  # type: ignore[import]

            bnb_config = BitsAndBytesConfig(
                load_in_4bit=True,
                bnb_4bit_use_double_quant=True,
                bnb_4bit_quant_type="nf4",
                bnb_4bit_compute_dtype=_quant_4bit_compute_dtype(torch),
            )
            model = model_loader.from_pretrained(
                source,
                quantization_config=bnb_config,
                device_map=_quantized_device_map(torch),
                trust_remote_code=True,
                local_files_only=local_files_only,
            )
            precision_mode = "4-bit quantisation"
            logger.info("Loaded %s in 4-bit quantisation", self.model)
        except Exception as exc:
            logger.warning("4-bit quantisation load failed for %s: %s", self.model, exc)
            model = model_loader.from_pretrained(
                source,
                torch_dtype="auto",
                device_map="auto",
                trust_remote_code=True,
                local_files_only=local_files_only,
            )
            precision_mode = "full/half precision"
            logger.info("Loaded %s in full/half precision", self.model)

        model.eval()
        _sanitize_generation_config(model)
        self._tf_runtime_state = self._build_transformers_runtime_state(
            model=model,
            tokenizer=tokenizer,
            source=source,
            local_snapshot=local_snapshot,
            precision_mode=precision_mode,
            load_elapsed_s=time.perf_counter() - load_started,
        )
        self._log_transformers_load_complete()
        self._tf_pipeline = (model, tokenizer)
        return model, tokenizer

    def _resolve_transformers_source(self) -> tuple[str, LocalModelSnapshot | None, bool]:
        configured_path = os.environ.get("LOCAL_LLM_SNAPSHOT_PATH", "").strip()
        if configured_path:
            path = Path(configured_path)
            snapshot = _inspect_local_model_path(path)
            if snapshot.is_complete:
                return str(path), snapshot, True
            logger.warning(
                "Configured LOCAL_LLM_SNAPSHOT_PATH is incomplete path=%s missing_shards=%s aux_missing=%s",
                path,
                snapshot.missing_shards or [],
                snapshot.aux_missing or [],
            )

        model_path = Path(self.model)
        if model_path.exists():
            snapshot = _inspect_local_model_path(model_path)
            return str(model_path), snapshot, True

        snapshot = _find_complete_local_snapshot(self.model)
        if snapshot is not None and snapshot.is_complete:
            return snapshot.source, snapshot, True

        logger.info(
            "No complete local snapshot found for %s; using normal Transformers resolution.",
            self.model,
        )
        return self.model, snapshot, False

    def _build_transformers_runtime_state(
        self,
        model,
        tokenizer,
        source: str,
        local_snapshot: LocalModelSnapshot | None,
        precision_mode: str,
        load_elapsed_s: float,
    ) -> dict[str, Any]:
        try:
            footprint_mb = round(float(model.get_memory_footprint()) / (1024 ** 2), 1)
        except Exception:
            footprint_mb = None

        device_map = getattr(model, "hf_device_map", {}) or {}
        device_counts: dict[str, int] = {}
        for device in device_map.values():
            key = str(device)
            device_counts[key] = device_counts.get(key, 0) + 1

        return {
            "model": self.model,
            "source": source,
            "precision_mode": precision_mode,
            "dtype": str(getattr(model, "dtype", "unknown")),
            "load_elapsed_s": round(load_elapsed_s, 2),
            "memory_footprint_mb": footprint_mb,
            "input_device": str(_infer_model_input_device(model)),
            "placement": _placement_verdict(model),
            "device_counts": device_counts,
            "device_map": device_map,
            "vocab_size": _tokenizer_attr(tokenizer, "vocab_size"),
            "snapshot_complete": bool(local_snapshot and local_snapshot.is_complete),
            "snapshot_revision": local_snapshot.revision if local_snapshot else "",
            "snapshot_size_gb": round(
                (local_snapshot.total_bytes if local_snapshot else 0) / (1024 ** 3),
                2,
            ),
            "memory": _collect_runtime_memory(),
        }

    def _log_transformers_load_start(
        self,
        source: str,
        local_snapshot: LocalModelSnapshot | None,
        local_files_only: bool,
    ) -> None:
        logger.info(
            "Loading transformers model model=%s source=%s local_files_only=%s",
            self.model,
            source,
            local_files_only,
        )
        if local_snapshot is not None:
            logger.info(
                "Local snapshot state revision=%s complete=%s shards=%d missing_shards=%s aux_missing=%s size_gb=%.2f",
                local_snapshot.revision or "n/a",
                local_snapshot.is_complete,
                local_snapshot.shard_count,
                local_snapshot.missing_shards or [],
                local_snapshot.aux_missing or [],
                local_snapshot.total_bytes / (1024 ** 3),
            )
        memory = _collect_runtime_memory()
        logger.info(
            "Pre-load memory process_rss_mb=%.1f system_used_gb=%s gpu=%s",
            memory["process_rss_mb"],
            memory["system_used_gb"],
            memory["gpu"],
        )

    def _log_transformers_load_complete(self) -> None:
        state = self._tf_runtime_state or {}
        memory = state.get("memory", {})
        logger.info(
            "Transformers load complete model=%s source=%s precision=%s dtype=%s load_s=%s footprint_mb=%s input_device=%s placement=%s device_counts=%s",
            state.get("model", self.model),
            state.get("source", self.model),
            state.get("precision_mode", "unknown"),
            state.get("dtype", "unknown"),
            state.get("load_elapsed_s", "unknown"),
            state.get("memory_footprint_mb", "unknown"),
            state.get("input_device", "unknown"),
            state.get("placement", "unknown"),
            state.get("device_counts", {}),
        )
        logger.info(
            "Post-load memory process_rss_mb=%s system_used_gb=%s gpu=%s",
            memory.get("process_rss_mb"),
            memory.get("system_used_gb"),
            memory.get("gpu"),
        )

    def _log_transformers_inference_start(
        self,
        prompt_chars: int,
        prompt_tokens: int,
        input_device: str,
        max_new_tokens: int,
        use_streaming_logs: bool,
        system_prompt: str,
        user_prompt: str,
    ) -> None:
        preview_chars = int(os.environ.get("LOCAL_LLM_LOG_PROMPT_PREVIEW_CHARS", "220"))
        logger.info(
            "Transformers inference start model=%s prompt_chars=%d prompt_tokens=%d input_device=%s max_new_tokens=%d streaming_logs=%s",
            self.model,
            prompt_chars,
            prompt_tokens,
            input_device,
            max_new_tokens,
            use_streaming_logs,
        )
        logger.debug(
            "Transformers prompt preview system=%r user=%r",
            system_prompt[:preview_chars],
            user_prompt[:preview_chars],
        )

    def _log_transformers_inference_end(
        self,
        output_text: str,
        prompt_tokens: int,
        elapsed_s: float,
    ) -> None:
        preview_chars = int(os.environ.get("LOCAL_LLM_LOG_OUTPUT_PREVIEW_CHARS", "240"))
        output_tokens_est = max(0, len(output_text) // 4)
        tokens_per_s = round(output_tokens_est / elapsed_s, 2) if elapsed_s > 0 else None
        memory = _collect_runtime_memory()
        logger.info(
            "Transformers inference complete model=%s elapsed_s=%.2f prompt_tokens=%d output_chars=%d output_tokens_est=%d tokens_per_s=%s process_rss_mb=%.1f gpu=%s",
            self.model,
            elapsed_s,
            prompt_tokens,
            len(output_text),
            output_tokens_est,
            tokens_per_s,
            memory["process_rss_mb"],
            memory["gpu"],
        )
        logger.debug("Transformers output preview=%r", output_text[:preview_chars])


def _parse_json(text: str) -> dict:
    """
    Parse LLM output as JSON, stripping common markdown noise.
    Raises LLMParseError if the result is still not valid JSON.
    """
    text = text.strip()

    if text.startswith("```"):
        lines = text.splitlines()
        inner = []
        in_block = False
        for line in lines:
            if line.startswith("```"):
                in_block = not in_block
                continue
            if in_block or not line.startswith("```"):
                inner.append(line)
        text = "\n".join(inner).strip()

    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end >= start:
        text = text[start : end + 1]

    try:
        result = json.loads(text)
    except json.JSONDecodeError as exc:
        raise LLMParseError(f"Cannot parse LLM output as JSON: {exc}") from exc

    if not isinstance(result, dict):
        raise LLMParseError(f"Expected JSON object, got {type(result).__name__}")

    return result


def _transformers_max_new_tokens(json_mode: bool) -> int:
    if json_mode:
        return max(32, int(os.environ.get("LOCAL_LLM_JSON_MAX_NEW_TOKENS", "128")))
    return max(32, int(os.environ.get("LOCAL_LLM_MAX_NEW_TOKENS", "256")))


def _encode_transformers_inputs(tokenizer_obj, text: str, input_device):
    return tokenizer_obj(text=text, return_tensors="pt").to(input_device)


def _tokenizer_attr(tokenizer_obj, attr: str, default: Any = None):
    value = getattr(tokenizer_obj, attr, None)
    if value is not None:
        return value
    nested = getattr(tokenizer_obj, "tokenizer", None)
    if nested is not None:
        nested_value = getattr(nested, attr, None)
        if nested_value is not None:
            return nested_value
    return default


def _is_vl_model_config(config_obj) -> bool:
    return str(getattr(config_obj, "model_type", "")).lower() in {
        "qwen2_vl",
        "qwen2_5_vl",
        "qwen3_vl",
    }


def _find_complete_local_snapshot(model_name: str) -> LocalModelSnapshot | None:
    repo_dir = _hf_cache_root() / "hub" / f"models--{model_name.replace('/', '--')}" / "snapshots"
    if not repo_dir.exists():
        return None

    best: LocalModelSnapshot | None = None
    for path in sorted(repo_dir.iterdir(), key=lambda item: item.name, reverse=True):
        if not path.is_dir():
            continue
        snapshot = _inspect_local_model_path(path)
        if best is None:
            best = snapshot
        if snapshot.is_complete:
            return snapshot
    return best


def _inspect_local_model_path(path: Path) -> LocalModelSnapshot:
    aux_required = ["config.json", "tokenizer.json", "tokenizer_config.json"]
    aux_missing = [name for name in aux_required if not (path / name).exists()]
    total_bytes = 0
    shard_count = 0
    missing_shards: list[str] = []

    index_file = path / "model.safetensors.index.json"
    if index_file.exists():
        try:
            data = json.loads(index_file.read_text(encoding="utf-8"))
            shards = sorted(set((data.get("weight_map") or {}).values()))
            shard_count = len(shards)
            for shard_name in shards:
                shard_path = path / shard_name
                if shard_path.exists():
                    total_bytes += shard_path.stat().st_size
                else:
                    missing_shards.append(shard_name)
        except Exception:
            missing_shards.append("unreadable_index")
    else:
        single_file = path / "model.safetensors"
        if single_file.exists():
            shard_count = 1
            total_bytes = single_file.stat().st_size
        else:
            shard_files = sorted(path.glob("*.safetensors"))
            shard_count = len(shard_files)
            total_bytes = sum(item.stat().st_size for item in shard_files if item.exists())
            aux_missing.append("model.safetensors.index.json")

    for aux_name in ("config.json", "tokenizer.json", "tokenizer_config.json", "generation_config.json"):
        aux_path = path / aux_name
        if aux_path.exists():
            total_bytes += aux_path.stat().st_size

    return LocalModelSnapshot(
        source=str(path),
        revision=path.name,
        shard_count=shard_count,
        missing_shards=missing_shards,
        total_bytes=total_bytes,
        aux_missing=aux_missing,
    )


def _hf_cache_root() -> Path:
    explicit = os.environ.get("HF_HOME", "").strip()
    if explicit:
        return Path(explicit)
    return Path.home() / ".cache" / "huggingface"


def _collect_runtime_memory() -> dict[str, Any]:
    process_rss_mb = 0.0
    system_used_gb: float | None = None
    gpu: list[dict[str, Any]] = []

    try:
        import psutil  # type: ignore[import]

        proc = psutil.Process()
        process_rss_mb = round(proc.memory_info().rss / (1024 ** 2), 1)
        vm = psutil.virtual_memory()
        system_used_gb = round((vm.total - vm.available) / (1024 ** 3), 2)
    except Exception:
        pass

    try:
        import torch  # type: ignore[import]

        if torch.cuda.is_available():
            for idx in range(torch.cuda.device_count()):
                free_bytes, total_bytes = torch.cuda.mem_get_info(idx)
                gpu.append(
                    {
                        "device": idx,
                        "name": torch.cuda.get_device_name(idx),
                        "allocated_mb": round(torch.cuda.memory_allocated(idx) / (1024 ** 2), 1),
                        "reserved_mb": round(torch.cuda.memory_reserved(idx) / (1024 ** 2), 1),
                        "free_gb": round(free_bytes / (1024 ** 3), 2),
                        "total_gb": round(total_bytes / (1024 ** 3), 2),
                    }
                )
    except Exception:
        pass

    return {
        "process_rss_mb": process_rss_mb,
        "system_used_gb": system_used_gb,
        "gpu": gpu,
    }


def _infer_model_input_device(model_obj):
    device_map = getattr(model_obj, "hf_device_map", None) or {}
    if device_map:
        for device in device_map.values():
            device_str = str(device)
            if device_str not in {"cpu", "disk", "meta"}:
                try:
                    import torch  # type: ignore[import]

                    return torch.device(device_str)
                except Exception:
                    break
    return model_obj.device


def _sanitize_generation_config(model_obj) -> None:
    generation_config = getattr(model_obj, "generation_config", None)
    if generation_config is None:
        return

    # Some model configs ship sampling-only defaults like top_k even though
    # this backend uses deterministic decoding with do_sample=False.
    for attr in ("top_k", "top_p", "temperature", "typical_p", "min_p"):
        if hasattr(generation_config, attr):
            try:
                setattr(generation_config, attr, None)
            except Exception:
                logger.debug("Could not clear generation_config.%s", attr)


def _placement_verdict(model_obj) -> str:
    device_map = getattr(model_obj, "hf_device_map", None) or {}
    if not device_map:
        device = str(getattr(model_obj, "device", "unknown"))
        if device.startswith("cuda"):
            return "FULL_GPU"
        if device == "cpu":
            return "CPU_ONLY"
        return "UNKNOWN"

    values = {str(v) for v in device_map.values()}
    has_cuda = any(v.startswith("cuda") or v.isdigit() for v in values)
    has_cpu = "cpu" in values
    has_disk = "disk" in values
    if has_disk:
        return "DISK_OFFLOAD"
    if has_cuda and has_cpu:
        return "MIXED_CPU_GPU"
    if has_cuda:
        return "FULL_GPU"
    if has_cpu:
        return "CPU_ONLY"
    return "UNKNOWN"


def _quantized_device_map(torch_module):
    if _get_bool_env("LOCAL_LLM_4BIT_FORCE_GPU", True) and torch_module.cuda.is_available():
        return {"": 0}
    return "auto"


def _quant_4bit_compute_dtype(torch_module):
    requested = os.environ.get("LOCAL_LLM_4BIT_COMPUTE_DTYPE", "float16").strip().lower()
    if requested == "bfloat16":
        return torch_module.bfloat16
    return torch_module.float16


def _get_bool_env(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}
