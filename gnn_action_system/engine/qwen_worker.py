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
        self._processor = None
        self._model = None
        self._device_label = "unloaded"

    def load(self) -> None:
        if self._model is not None and self._processor is not None:
            return

        print(f"[qwen-worker] loading tokenizer and model: {self.config.model_id}", file=sys.stderr, flush=True)

        try:
            from transformers import AutoProcessor, BitsAndBytesConfig, Qwen3VLForConditionalGeneration
            import torch
        except ModuleNotFoundError as exc:
            missing_name = getattr(exc, "name", None) or str(exc)
            raise RuntimeError(
                "Qwen VL dependencies are missing from the active environment. "
                f"Missing module: {missing_name}. "
                "Install the project requirements again, especially `torchvision`, `Pillow`, and `six`."
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
            self._processor = _load_processor(self.config, AutoProcessor)
        except Exception as exc:
            raise RuntimeError(f"Could not load Qwen processor: {exc}") from exc

        tokenizer = getattr(self._processor, "tokenizer", None)
        if tokenizer is not None:
            tokenizer.padding_side = "left"
            tokenizer.truncation_side = "left"
            if tokenizer.pad_token_id is None:
                tokenizer.pad_token = tokenizer.eos_token

        load_kwargs: Dict[str, Any] = {
            "low_cpu_mem_usage": self.config.low_cpu_mem_usage,
        }
        attn_impl = _qwen_attn_impl(have_cuda)
        if attn_impl is not None:
            load_kwargs["attn_implementation"] = attn_impl

        if have_cuda:
            with _qwen_safe_loader_context(self.config):
                if quant_config is not None:
                    load_kwargs["quantization_config"] = quant_config
                    load_kwargs["device_map"] = {"": self.config.gpu_index}
                    self._model = _load_model(Qwen3VLForConditionalGeneration, self.config, **load_kwargs).eval()
                else:
                    load_kwargs["torch_dtype"] = model_dtype
                    load_kwargs["device_map"] = {"": self.config.gpu_index}
                    self._model = _load_model(Qwen3VLForConditionalGeneration, self.config, **load_kwargs).eval()
        else:
            self._model = _load_model(
                Qwen3VLForConditionalGeneration,
                self.config,
                torch_dtype=torch.float32,
                **load_kwargs,
            ).eval().to(device)

        if tokenizer is not None and hasattr(self._model, "generation_config"):
            self._model.generation_config.pad_token_id = tokenizer.pad_token_id
            self._model.generation_config.eos_token_id = tokenizer.eos_token_id
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

        print(
            "[qwen-worker] generation started "
            f"(messages={len(messages)}, max_new_tokens={int(generation.get('max_new_tokens', self.config.max_new_tokens))}, "
            f"device={self._device_label})",
            file=sys.stderr,
            flush=True,
        )

        conversation = [
            {
                "role": message.get("role", "user"),
                "content": [{"type": "text", "text": message.get("content", "")}],
            }
            for message in messages
        ]
        rendered = self._processor.apply_chat_template(
            conversation,
            tokenize=False,
            add_generation_prompt=True,
        )
        inputs = self._processor(text=[rendered], return_tensors="pt", padding=True)
        for key, value in list(inputs.items()):
            if hasattr(value, "to"):
                inputs[key] = value.to(self._model.device)

        generate_kwargs: Dict[str, Any] = {
            "max_new_tokens": int(generation.get("max_new_tokens", self.config.max_new_tokens)),
            "do_sample": bool(generation.get("do_sample", self.config.do_sample)),
            "repetition_penalty": float(generation.get("repetition_penalty", self.config.repetition_penalty)),
            "pad_token_id": self._processor.tokenizer.pad_token_id,
        }
        if generate_kwargs["do_sample"]:
            generate_kwargs["temperature"] = float(generation.get("temperature", self.config.temperature))
            generate_kwargs["top_p"] = float(generation.get("top_p", self.config.top_p))

        with torch.inference_mode():
            generated = self._model.generate(
                **inputs,
                **generate_kwargs,
            )
        prompt_len = int(inputs["input_ids"].shape[1]) if "input_ids" in inputs else 0
        output_ids = generated[:, prompt_len:]
        outputs = self._processor.batch_decode(output_ids, skip_special_tokens=True)
        print("[qwen-worker] generation finished", file=sys.stderr, flush=True)
        return str(outputs[0] if outputs else "")

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


def emit(message: Dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Persistent Qwen worker process")
    parser.add_argument("--warmup", action="store_true", help="Load the model immediately and keep the worker alive.")
    parser.add_argument("--once", action="store_true", help="Load the model once and exit after warmup.")
    return parser.parse_args()


def _qwen_attn_impl(have_cuda: bool) -> str | None:
    if not have_cuda:
        return None
    return "sdpa"


def _load_processor(config: QwenConfig, auto_processor_cls):
    primary_kwargs = {
        "min_pixels": config.min_pixels,
        "max_pixels": config.max_pixels,
    }
    try:
        return _from_pretrained_with_local_fallback(auto_processor_cls, config.model_id, **primary_kwargs)
    except Exception:
        return _from_pretrained_with_local_fallback(auto_processor_cls, config.model_id)


def _load_model(model_cls, config: QwenConfig, **kwargs):
    return _from_pretrained_with_local_fallback(model_cls, config.model_id, **kwargs)


def _from_pretrained_with_local_fallback(factory, model_id: str, **kwargs):
    local_snapshot = _resolve_local_snapshot_path(model_id)
    if local_snapshot is not None:
        print(
            f"[qwen-worker] loading from local cache snapshot: {local_snapshot}",
            file=sys.stderr,
            flush=True,
        )
        return factory.from_pretrained(str(local_snapshot), local_files_only=True, **kwargs)

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
        return factory.from_pretrained(model_id, **fallback_kwargs)


def _resolve_local_snapshot_path(model_id: str) -> Path | None:
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
        if candidate.exists():
            return candidate

    snapshot_dirs = [path for path in snapshots_dir.iterdir() if path.is_dir()]
    if not snapshot_dirs:
        return None
    snapshot_dirs.sort(key=lambda item: item.stat().st_mtime, reverse=True)
    return snapshot_dirs[0]


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
