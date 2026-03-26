"""
LLMClient — unified interface over multiple LLM backends.

Supported providers (configure via Django settings):
  "ollama"        local Ollama API  (default: http://localhost:11434)
  "groq"          Groq cloud        (https://api.groq.com/openai/v1)
  "openai"        OpenAI-compatible (https://api.openai.com/v1)
  "transformers"  local HuggingFace transformers (lazy-loaded, GPU optional)

Settings keys:
  LLM_PROVIDER   str   default "ollama"
  LLM_MODEL      str   default "llama3.2"
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
import time
from dataclasses import dataclass
from typing import Any

import requests

logger = logging.getLogger(__name__)

# ── Exceptions ────────────────────────────────────────────────────────────────

class LLMError(Exception):
    """Raised when the LLM call fails after retries."""


class LLMParseError(LLMError):
    """Raised when the LLM response cannot be parsed as JSON."""


# ── Provider base ─────────────────────────────────────────────────────────────

_PROVIDER_DEFAULTS: dict[str, dict] = {
    "ollama":       {"base_url": "http://localhost:11434",       "model": "qwen2.5:14b"},
    "groq":         {"base_url": "https://api.groq.com/openai/v1", "model": "llama-3.1-8b-instant"},
    "openai":       {"base_url": "https://api.openai.com/v1",    "model": "gpt-4o-mini"},
    "transformers": {"base_url": "",                             "model": "Qwen/Qwen2.5-14B-Instruct"},
}


@dataclass
class LLMResponse:
    content: str
    raw: dict


# ── LLMClient ─────────────────────────────────────────────────────────────────

class LLMClient:
    """
    Unified LLM interface.  Call generate() — everything else is internal.
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

        self._tf_pipeline = None  # lazy-loaded for "transformers" provider

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

    # ── Public API ────────────────────────────────────────────────────────────

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
                sp = sp + "\n\nCRITICAL: Your previous response was not valid JSON. Output ONLY a JSON object. No prose, no markdown code fences, no explanation."
                up = up + "\n\nRemember: output ONLY valid JSON."

            try:
                raw_content = self._call(sp, up, json_mode=json_mode, json_schema=json_schema)
                return _parse_json(raw_content)
            except LLMParseError as exc:
                logger.warning(
                    "LLM JSON parse error (attempt %d): %s", attempt + 1, exc
                )
                last_error = exc
                strict_retry = True
                time.sleep(0.5)
            except LLMError as exc:
                logger.warning(
                    "LLM call error (attempt %d): %s", attempt + 1, exc
                )
                last_error = exc
                time.sleep(1)

        raise last_error or LLMError("LLM call failed after retries")

    # ── Provider dispatch ─────────────────────────────────────────────────────

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
            return self._call_openai_compat(
                system_prompt, user_prompt, json_mode, json_schema
            )
        if self.provider == "transformers":
            return self._call_transformers(system_prompt, user_prompt)
        raise LLMError(f"Unknown LLM provider: {self.provider!r}")

    # ── Ollama ────────────────────────────────────────────────────────────────

    def _call_ollama(
        self, system_prompt: str, user_prompt: str, json_mode: bool
    ) -> str:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user",   "content": user_prompt},
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

    # ── OpenAI-compatible (Groq, OpenAI, LM Studio, vLLM) ────────────────────

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
                {"role": "user",   "content": user_prompt},
            ],
        }

        if json_schema:
            # Structured outputs (OpenAI >= Nov 2024, Groq structured outputs)
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
                f"OpenAI-compat HTTP {exc.response.status_code}: "
                f"{exc.response.text[:200]}"
            ) from exc
        except requests.RequestException as exc:
            raise LLMError(f"OpenAI-compat request error: {exc}") from exc

        data = resp.json()
        return data["choices"][0]["message"]["content"]

    # ── Local HuggingFace transformers (Qwen2.5 and compatible) ─────────────────

    def _call_transformers(self, system_prompt: str, user_prompt: str) -> str:
        """
        Lazily loads the model + tokenizer on first call.
        Uses the model's own chat template (apply_chat_template) so it works
        correctly with Qwen2.5, Llama-3, Mistral, and other instruction models.

        Requires:
          pip install transformers torch accelerate
          For 4-bit quantisation: pip install bitsandbytes
        """
        model_obj, tokenizer = self._get_tf_model()

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": user_prompt},
        ]

        # apply_chat_template formats the conversation with the model's own
        # special tokens and instruction format (e.g. <|im_start|> for Qwen)
        text = tokenizer.apply_chat_template(
            messages,
            tokenize=False,
            add_generation_prompt=True,
        )
        inputs = tokenizer(text, return_tensors="pt").to(model_obj.device)

        import torch
        with torch.inference_mode():
            output_ids = model_obj.generate(
                **inputs,
                max_new_tokens=512,
                do_sample=False,
                temperature=None,
                top_p=None,
                pad_token_id=tokenizer.eos_token_id,
            )

        # Decode only the newly generated tokens (strip the prompt)
        new_ids = output_ids[0][inputs["input_ids"].shape[1]:]
        return tokenizer.decode(new_ids, skip_special_tokens=True).strip()

    def _get_tf_model(self):
        """Return (model, tokenizer), loading once and caching on the instance."""
        if self._tf_pipeline is not None:
            return self._tf_pipeline  # already loaded, returns (model, tokenizer)

        try:
            from transformers import AutoModelForCausalLM, AutoTokenizer  # type: ignore[import]
            import torch  # type: ignore[import]
        except ImportError as exc:
            raise LLMError(
                "transformers / torch not installed. "
                "Run: pip install transformers torch accelerate"
            ) from exc

        logger.info(
            "Loading transformers model %s — this may take several minutes on first run …",
            self.model,
        )

        tokenizer = AutoTokenizer.from_pretrained(
            self.model,
            trust_remote_code=True,   # required for Qwen models
        )

        # Try 4-bit quantisation first (saves ~8 GB VRAM for 14B); fall back to
        # fp16/bf16 if bitsandbytes is not installed.
        try:
            from transformers import BitsAndBytesConfig  # type: ignore[import]
            bnb_config = BitsAndBytesConfig(
                load_in_4bit=True,
                bnb_4bit_use_double_quant=True,
                bnb_4bit_quant_type="nf4",
                bnb_4bit_compute_dtype=torch.bfloat16,
            )
            model = AutoModelForCausalLM.from_pretrained(
                self.model,
                quantization_config=bnb_config,
                device_map="auto",
                trust_remote_code=True,
            )
            logger.info("Loaded %s in 4-bit quantisation", self.model)
        except Exception:
            # bitsandbytes unavailable or CUDA not present — load in auto dtype
            model = AutoModelForCausalLM.from_pretrained(
                self.model,
                torch_dtype="auto",
                device_map="auto",
                trust_remote_code=True,
            )
            logger.info("Loaded %s in full/half precision", self.model)

        model.eval()
        self._tf_pipeline = (model, tokenizer)
        return model, tokenizer


# ── JSON parsing helper ────────────────────────────────────────────────────────

def _parse_json(text: str) -> dict:
    """
    Parse LLM output as JSON, stripping common markdown noise.
    Raises LLMParseError if the result is still not valid JSON.
    """
    text = text.strip()

    # Strip markdown code fences (```json ... ``` or ``` ... ```)
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

    # Find the first { ... } substring in case there is preamble text
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end >= start:
        text = text[start : end + 1]

    try:
        result = json.loads(text)
    except json.JSONDecodeError as exc:
        raise LLMParseError(f"Cannot parse LLM output as JSON: {exc}") from exc

    if not isinstance(result, dict):
        raise LLMParseError(
            f"Expected JSON object, got {type(result).__name__}"
        )

    return result
