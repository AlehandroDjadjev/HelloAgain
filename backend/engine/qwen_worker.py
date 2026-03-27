from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
import shutil
import sys
import traceback
from contextlib import contextmanager
from typing import Any, Dict, List

from .qwen_config import QwenConfig


class QwenWorkerRuntime:
    def __init__(self, config: QwenConfig | None = None) -> None:
        self.config = config or QwenConfig()
        self._tokenizer = None
        self._model = None
        self._device_label = "unloaded"

    def load(self) -> None:
        if self._model is not None and self._tokenizer is not None:
            return

        print(f"[qwen-worker] loading tokenizer and model: {self.config.model_id}", file=sys.stderr, flush=True)

        try:
            from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig
            import torch
        except ModuleNotFoundError as exc:
            missing_name = getattr(exc, "name", None) or str(exc)
            raise RuntimeError(
                "Qwen text-model dependencies are missing from the active environment. "
                f"Missing module: {missing_name}. "
                "Install the project requirements again, especially `transformers`, `torch`, `accelerate`, and `bitsandbytes`."
            ) from exc

        cuda_available = torch.cuda.is_available()
        if self.config.require_cuda and (self.config.force_cpu or not cuda_available):
            raise RuntimeError(
                "Qwen is configured to require CUDA, but torch cannot see a CUDA device. "
                "Install a CUDA-enabled PyTorch build in this venv or unset QWEN_REQUIRE_CUDA."
            )

        if not self.config.force_cpu and not cuda_available:
            hint = ""
            if shutil.which("nvidia-smi"):
                hint = (
                    " NVIDIA GPU tools are present on this machine, so the active Python environment "
                    "likely has a CPU-only PyTorch build installed."
                )
            print(
                "[qwen-worker] WARNING: torch.cuda.is_available() is False. "
                "Qwen will run on CPU and be very slow." + hint,
                file=sys.stderr,
                flush=True,
            )

        have_cuda = cuda_available and not self.config.force_cpu
        device = torch.device(f"cuda:{self.config.gpu_index}" if have_cuda else "cpu")
        model_dtype = torch.float16 if have_cuda else torch.float32
        quant_config = None
        if have_cuda and self.config.load_in_4bit:
            quant_config = BitsAndBytesConfig(
                load_in_4bit=True,
                bnb_4bit_quant_type="nf4",
                bnb_4bit_use_double_quant=True,
                bnb_4bit_compute_dtype=model_dtype,
            )

        try:
            self._tokenizer = _load_tokenizer(self.config, AutoTokenizer)
        except Exception as exc:
            raise RuntimeError(f"Could not load Qwen tokenizer: {exc}") from exc

        self._tokenizer.padding_side = "left"
        self._tokenizer.truncation_side = "left"
        if self._tokenizer.pad_token_id is None:
            self._tokenizer.pad_token = self._tokenizer.eos_token

        load_kwargs: Dict[str, Any] = {
            "low_cpu_mem_usage": self.config.low_cpu_mem_usage,
        }
        attn_impl = _qwen_attn_impl(self.config, have_cuda)
        if attn_impl is not None:
            load_kwargs["attn_implementation"] = attn_impl

        if have_cuda:
            with _qwen_safe_loader_context(self.config):
                if quant_config is not None:
                    load_kwargs["quantization_config"] = quant_config
                    load_kwargs["device_map"] = {"": self.config.gpu_index}
                    self._model = _load_model(AutoModelForCausalLM, self.config, **load_kwargs).eval()
                else:
                    load_kwargs["torch_dtype"] = model_dtype
                    load_kwargs["device_map"] = {"": self.config.gpu_index}
                    self._model = _load_model(AutoModelForCausalLM, self.config, **load_kwargs).eval()
        else:
            self._model = _load_model(
                AutoModelForCausalLM,
                self.config,
                torch_dtype=torch.float32,
                **load_kwargs,
            ).eval().to(device)

        if hasattr(self._model, "generation_config"):
            self._model.generation_config.pad_token_id = self._tokenizer.pad_token_id
            self._model.generation_config.eos_token_id = self._tokenizer.eos_token_id
        try:
            self._model.config.use_cache = True
        except Exception:
            pass
        self._device_label = self._resolve_model_device_label()
        print(
            f"[qwen-worker] model ready on {self._device_label}",
            file=sys.stderr,
            flush=True,
        )

    def generate(self, messages: List[Dict[str, str]], generation: Dict[str, Any] | None = None) -> str:
        self.load()
        generation = generation or {}
        import torch

        requested_max_new_tokens = int(generation.get("max_new_tokens", self.config.max_new_tokens))
        print(
            "[qwen-worker] generation started "
            f"(messages={len(messages)}, max_new_tokens={requested_max_new_tokens}, device={self._device_label})",
            file=sys.stderr,
            flush=True,
        )

        normalized_messages = [
            {
                "role": message.get("role", "user"),
                "content": message.get("content", ""),
            }
            for message in messages
        ]
        rendered = self._tokenizer.apply_chat_template(
            normalized_messages,
            tokenize=False,
            add_generation_prompt=True,
        )
        inputs = self._tokenizer([rendered], return_tensors="pt", padding=True)
        for key, value in list(inputs.items()):
            if hasattr(value, "to"):
                inputs[key] = value.to(self._model.device)

        generate_kwargs = self._build_generate_kwargs(generation)

        with torch.inference_mode():
            generated = self._model.generate(
                **inputs,
                **generate_kwargs,
            )

        prompt_len = int(inputs["input_ids"].shape[1]) if "input_ids" in inputs else 0
        output_ids = generated[:, prompt_len:]
        raw_text = self._decode_ids(output_ids)

        raw_text, output_ids = self._continue_json_if_needed(
            output_ids=output_ids,
            generated=generated,
            generation=generation,
            raw_text=raw_text,
        )
        normalized_output = _normalize_json_output(raw_text)
        print("[qwen-worker] generation finished", file=sys.stderr, flush=True)
        return normalized_output

    def health_payload(self) -> Dict[str, Any]:
        return {
            "status": "ok",
            "loaded": bool(self._model is not None),
            "model_id": self.config.model_id,
            "device": self._device_label,
            "cuda_available": _torch_cuda_available(),
            "gpu_name": _torch_gpu_name(),
            "force_cpu": self.config.force_cpu,
            "require_cuda": self.config.require_cuda,
            "load_in_4bit": self.config.load_in_4bit,
            "attn_implementation": self.config.attn_implementation,
            "max_new_tokens": self.config.max_new_tokens,
        }

    def handle(self, request: Dict[str, Any]) -> Dict[str, Any]:
        action = request.get("action", "generate")
        if action == "ping":
            self.load()
            return {"status": "ready", "model_id": self.config.model_id}
        if action == "generate":
            messages = request.get("messages") or []
            generation = request.get("generation") or {}
            return {"raw_text": self.generate(messages=messages, generation=generation)}
        raise ValueError(f"Unsupported worker action: {action}")

    def _resolve_model_device_label(self) -> str:
        hf_device_map = getattr(self._model, "hf_device_map", None)
        if isinstance(hf_device_map, dict) and hf_device_map:
            unique_devices = sorted({str(value) for value in hf_device_map.values()})
            return ",".join(unique_devices)

        try:
            parameter = next(self._model.parameters())
            return str(parameter.device)
        except Exception:
            pass

        model_device = getattr(self._model, "device", None)
        if model_device is not None:
            return str(model_device)
        return "unknown"

    def _build_generate_kwargs(self, generation: Dict[str, Any]) -> Dict[str, Any]:
        generate_kwargs: Dict[str, Any] = {
            "max_new_tokens": int(generation.get("max_new_tokens", self.config.max_new_tokens)),
            "do_sample": bool(generation.get("do_sample", self.config.do_sample)),
            "repetition_penalty": float(generation.get("repetition_penalty", self.config.repetition_penalty)),
            "pad_token_id": self._tokenizer.pad_token_id,
            "eos_token_id": self._tokenizer.eos_token_id,
            "use_cache": True,
        }
        if generate_kwargs["do_sample"]:
            generate_kwargs["temperature"] = float(generation.get("temperature", self.config.temperature))
            generate_kwargs["top_p"] = float(generation.get("top_p", self.config.top_p))
        return generate_kwargs

    def _continue_json_if_needed(
        self,
        *,
        output_ids,
        generated,
        generation: Dict[str, Any],
        raw_text: str,
    ):
        if _normalize_json_output(raw_text) == raw_text.strip() and _looks_like_complete_json_object(raw_text):
            return raw_text, output_ids

        remaining_budget = max(
            0,
            int(generation.get("json_continuation_budget", self.config.json_continuation_budget)),
        )
        if remaining_budget <= 0:
            return raw_text, output_ids

        import torch

        chunk_size = max(
            32,
            int(generation.get("json_continuation_chunk", self.config.json_continuation_chunk)),
        )
        current_generated = generated
        current_output_ids = output_ids
        current_text = raw_text

        while remaining_budget > 0 and _needs_json_continuation(current_text):
            step = min(chunk_size, remaining_budget)
            previous_full_len = int(current_generated.shape[1])
            continuation_inputs = {
                "input_ids": current_generated,
                "attention_mask": torch.ones_like(current_generated, device=current_generated.device),
            }
            continuation_kwargs = self._build_generate_kwargs(generation)
            continuation_kwargs["max_new_tokens"] = step

            with torch.inference_mode():
                current_generated = self._model.generate(
                    **continuation_inputs,
                    **continuation_kwargs,
                )

            appended_ids = current_generated[:, previous_full_len:]
            if int(appended_ids.shape[1]) == 0:
                break
            current_output_ids = torch.cat([current_output_ids, appended_ids], dim=1)
            current_text = self._decode_ids(current_output_ids)
            remaining_budget -= step
            print(
                f"[qwen-worker] JSON continuation step used {int(appended_ids.shape[1])} tokens, remaining budget={remaining_budget}",
                file=sys.stderr,
                flush=True,
            )

        return current_text, current_output_ids

    def _decode_ids(self, token_ids) -> str:
        outputs = self._tokenizer.batch_decode(token_ids, skip_special_tokens=True)
        return str(outputs[0] if outputs else "")


def emit(message: Dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Persistent Qwen worker process")
    parser.add_argument("--warmup", action="store_true", help="Load the model immediately and keep the worker alive.")
    parser.add_argument("--once", action="store_true", help="Load the model once and exit after warmup.")
    return parser.parse_args()


def _qwen_attn_impl(config: QwenConfig, have_cuda: bool) -> str | None:
    if not have_cuda:
        return None
    attn_impl = (config.attn_implementation or "sdpa").strip().lower()
    if attn_impl in {"", "none"}:
        return None
    return attn_impl


def _load_tokenizer(config: QwenConfig, auto_tokenizer_cls):
    return _from_pretrained_with_local_fallback(
        auto_tokenizer_cls,
        config.model_id,
        require_weights=False,
        use_fast=True,
    )


def _load_model(model_cls, config: QwenConfig, **kwargs):
    return _from_pretrained_with_local_fallback(
        model_cls,
        config.model_id,
        require_weights=True,
        **kwargs,
    )


def _from_pretrained_with_local_fallback(factory, model_id: str, *, require_weights: bool, **kwargs):
    local_snapshot = _resolve_local_snapshot_path(model_id, require_weights=require_weights)
    if local_snapshot is not None:
        print(
            f"[qwen-worker] loading from local cache snapshot: {local_snapshot}",
            file=sys.stderr,
            flush=True,
        )
        try:
            return factory.from_pretrained(str(local_snapshot), local_files_only=True, **kwargs)
        except Exception as exc:
            _raise_better_model_load_error(model_id, exc)

    try:
        return factory.from_pretrained(model_id, **kwargs)
    except Exception as exc:
        fallback_kwargs = dict(kwargs)
        fallback_kwargs["local_files_only"] = True
        print(
            f"[qwen-worker] online model lookup failed, retrying from local cache only: {exc}",
            file=sys.stderr,
            flush=True,
        )
        try:
            return factory.from_pretrained(model_id, **fallback_kwargs)
        except Exception as fallback_exc:
            _raise_better_model_load_error(model_id, fallback_exc)


def _raise_better_model_load_error(model_id: str, exc: Exception) -> None:
    message = str(exc)
    normalized = message.lower()
    if "qwen3_5" in normalized and ("model type" in normalized or "recognize this architecture" in normalized):
        raise RuntimeError(
            "The configured Qwen checkpoint uses the newer `qwen3_5` architecture, "
            "but the installed Transformers build in this project cannot load it yet. "
            f"Configured model: {model_id}. "
            "Use `Qwen/Qwen3-4B-Instruct-2507` or another supported checkpoint, "
            "or upgrade Transformers to a build that explicitly supports `qwen3_5`."
        ) from exc
    raise exc


def _resolve_local_snapshot_path(model_id: str, *, require_weights: bool) -> Path | None:
    cache_root = _huggingface_cache_root()
    if cache_root is None:
        return None

    model_dir = cache_root / ("models--" + model_id.replace("/", "--"))
    snapshots_dir = model_dir / "snapshots"
    if not snapshots_dir.exists():
        return None

    ref_path = model_dir / "refs" / "main"
    if ref_path.exists():
        revision = ref_path.read_text(encoding="utf-8").strip()
        candidate = snapshots_dir / revision
        if _snapshot_is_usable(candidate, require_weights=require_weights):
            return candidate

    snapshot_dirs = [path for path in snapshots_dir.iterdir() if path.is_dir()]
    if not snapshot_dirs:
        return None
    snapshot_dirs.sort(key=lambda item: item.stat().st_mtime, reverse=True)
    for snapshot_dir in snapshot_dirs:
        if _snapshot_is_usable(snapshot_dir, require_weights=require_weights):
            return snapshot_dir
    return None


def _snapshot_is_usable(snapshot_dir: Path, *, require_weights: bool) -> bool:
    if not snapshot_dir.exists():
        return False

    if not _has_tokenizer_files(snapshot_dir):
        return False

    if not require_weights:
        return True

    return _has_model_weight_files(snapshot_dir)


def _has_tokenizer_files(snapshot_dir: Path) -> bool:
    if (snapshot_dir / "tokenizer.json").exists():
        return True
    if (snapshot_dir / "tokenizer.model").exists():
        return True
    return (snapshot_dir / "vocab.json").exists() and (snapshot_dir / "merges.txt").exists()


def _has_model_weight_files(snapshot_dir: Path) -> bool:
    index_path = snapshot_dir / "model.safetensors.index.json"
    if index_path.exists():
        try:
            payload = json.loads(index_path.read_text(encoding="utf-8"))
        except Exception:
            return False
        weight_map = payload.get("weight_map")
        if not isinstance(weight_map, dict) or not weight_map:
            return False
        required_files = {str(name).strip() for name in weight_map.values() if str(name).strip()}
        return bool(required_files) and all((snapshot_dir / name).exists() for name in required_files)

    direct_weight_files = (
        "model.safetensors",
        "pytorch_model.bin",
        "pytorch_model.bin.index.json",
    )
    return any((snapshot_dir / filename).exists() for filename in direct_weight_files)


def _huggingface_cache_root() -> Path | None:
    hf_home = os.environ.get("HF_HOME")
    if hf_home:
        return Path(hf_home) / "hub"

    user_profile = os.environ.get("USERPROFILE")
    if not user_profile:
        return None
    return Path(user_profile) / ".cache" / "huggingface" / "hub"


def _torch_cuda_available() -> bool:
    try:
        import torch

        return bool(torch.cuda.is_available())
    except Exception:
        return False


def _torch_gpu_name() -> str | None:
    try:
        import torch

        if not torch.cuda.is_available():
            return None
        return str(torch.cuda.get_device_name(0))
    except Exception:
        return None


def _strip_code_fences(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```") and stripped.endswith("```"):
        lines = stripped.splitlines()
        if len(lines) >= 2:
            return "\n".join(lines[1:-1]).strip()
    return stripped


def _extract_first_json_object(text: str) -> str | None:
    decoder = json.JSONDecoder()
    candidate = _strip_code_fences(text)
    for start_index, char in enumerate(candidate):
        if char != "{":
            continue
        try:
            parsed, end_index = decoder.raw_decode(candidate[start_index:])
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            trailing = candidate[start_index + end_index :].strip()
            if not trailing:
                return json.dumps(parsed, ensure_ascii=False)
            return json.dumps(parsed, ensure_ascii=False)
    return None


def _normalize_json_output(text: str) -> str:
    candidate = _strip_code_fences(text)
    try:
        parsed = json.loads(candidate)
        if isinstance(parsed, dict):
            return json.dumps(parsed, ensure_ascii=False)
    except json.JSONDecodeError:
        pass

    extracted = _extract_first_json_object(candidate)
    if extracted is not None:
        return extracted
    return candidate.strip()


def _looks_like_complete_json_object(text: str) -> bool:
    candidate = _strip_code_fences(text)
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        return False
    return isinstance(parsed, dict)


def _needs_json_continuation(text: str) -> bool:
    candidate = _strip_code_fences(text)
    if _looks_like_complete_json_object(candidate):
        return False
    has_open_brace = "{" in candidate
    has_unclosed_brace = candidate.count("{") > candidate.count("}")
    has_unclosed_bracket = candidate.count("[") > candidate.count("]")
    ends_in_open_structure = candidate.rstrip().endswith((":", ",", "{", "[", '"'))
    return has_open_brace and (has_unclosed_brace or has_unclosed_bracket or ends_in_open_structure)


@contextmanager
def _qwen_safe_loader_context(config: QwenConfig):
    orig_async_load = os.environ.get("HF_DEACTIVATE_ASYNC_LOAD")
    orig_allocator_warmup = None
    modeling_utils = None

    try:
        if config.disable_async_load:
            os.environ["HF_DEACTIVATE_ASYNC_LOAD"] = "1"

        if config.disable_allocator_warmup:
            try:
                import transformers.modeling_utils as modeling_utils

                orig_allocator_warmup = getattr(modeling_utils, "caching_allocator_warmup", None)
                if orig_allocator_warmup is not None:
                    modeling_utils.caching_allocator_warmup = lambda *args, **kwargs: None
            except Exception as exc:
                print(f"[qwen-worker] allocator warmup patch failed: {exc}", file=sys.stderr, flush=True)

        yield
    finally:
        if modeling_utils is not None and orig_allocator_warmup is not None:
            try:
                modeling_utils.caching_allocator_warmup = orig_allocator_warmup
            except Exception:
                pass

        if orig_async_load is None:
            os.environ.pop("HF_DEACTIVATE_ASYNC_LOAD", None)
        else:
            os.environ["HF_DEACTIVATE_ASYNC_LOAD"] = orig_async_load


def main() -> int:
    logging.getLogger("transformers").setLevel(logging.ERROR)
    args = parse_args()
    runtime = QwenWorkerRuntime()

    if args.warmup or args.once:
        runtime.load()
        print("[qwen-worker] warmup complete", file=sys.stderr, flush=True)
        if args.once:
            return 0

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue

        request_id = None
        try:
            request = json.loads(line)
            request_id = request.get("id")
            payload = runtime.handle(request)
            emit({"id": request_id, "ok": True, "payload": payload})
        except Exception as exc:  # pragma: no cover - defensive worker boundary
            emit(
                {
                    "id": request_id,
                    "ok": False,
                    "error": {
                        "message": str(exc),
                        "traceback": traceback.format_exc(),
                    },
                }
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
