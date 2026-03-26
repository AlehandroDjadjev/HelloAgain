import os
from dataclasses import dataclass


def env_flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class QwenConfig:
    model_id: str = os.getenv("QWEN_MODEL_ID", "Qwen/Qwen3-VL-2B-Instruct")
    device_map: str = os.getenv("QWEN_DEVICE_MAP", "auto")
    max_new_tokens: int = int(os.getenv("QWEN_MAX_NEW_TOKENS", "3072"))
    temperature: float = float(os.getenv("QWEN_TEMPERATURE", "0.15"))
    top_p: float = float(os.getenv("QWEN_TOP_P", "0.95"))
    repetition_penalty: float = float(os.getenv("QWEN_REPETITION_PENALTY", "1.05"))
    do_sample: bool = env_flag("QWEN_DO_SAMPLE", False)
    load_in_4bit: bool = env_flag("QWEN_LOAD_IN_4BIT", False)
    force_cpu: bool = env_flag("QWEN_FORCE_CPU", False)
    require_cuda: bool = env_flag("QWEN_REQUIRE_CUDA", False)
    gpu_index: int = int(os.getenv("QWEN_GPU_INDEX", "0"))
    low_cpu_mem_usage: bool = env_flag("QWEN_LOW_CPU_MEM_USAGE", True)
    disable_async_load: bool = env_flag("QWEN_DISABLE_ASYNC_LOAD", True)
    disable_allocator_warmup: bool = env_flag("QWEN_DISABLE_ALLOCATOR_WARMUP", True)
    min_pixels: int = int(os.getenv("QWEN_MIN_PIXELS", str(256 * 28 * 28)))
    max_pixels: int = int(os.getenv("QWEN_MAX_PIXELS", str(512 * 28 * 28)))
    worker_module: str = os.getenv("QWEN_WORKER_MODULE", "engine.qwen_worker")
    worker_startup_timeout: float = float(os.getenv("QWEN_WORKER_STARTUP_TIMEOUT", "600"))
    worker_request_timeout: float = float(os.getenv("QWEN_WORKER_REQUEST_TIMEOUT", "600"))
    worker_hide_window: bool = env_flag("QWEN_WORKER_HIDE_WINDOW", True)
    server_host: str = os.getenv("QWEN_SERVER_HOST", "127.0.0.1")
    server_port: int = int(os.getenv("QWEN_SERVER_PORT", "8011"))

    @property
    def server_base_url(self) -> str:
        return os.getenv("QWEN_SERVER_URL", f"http://{self.server_host}:{self.server_port}")
