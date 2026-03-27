from __future__ import annotations

import argparse
import json
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict

from .qwen_config import QwenConfig
from .qwen_worker import QwenWorkerRuntime


class QwenHttpHandler(BaseHTTPRequestHandler):
    runtime: QwenWorkerRuntime | None = None

    def _send_json(self, payload: Dict[str, Any], status: int = HTTPStatus.OK) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length).decode("utf-8") if length > 0 else "{}"
        return json.loads(raw or "{}")

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            runtime = self.runtime
            payload = runtime.health_payload() if runtime else {"status": "error", "detail": "Runtime not initialized"}
            self._send_json(payload)
            return
        self._send_json({"detail": "Not found"}, status=HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802
        runtime = self.runtime
        if runtime is None:
            self._send_json({"detail": "Runtime not initialized"}, status=HTTPStatus.INTERNAL_SERVER_ERROR)
            return

        try:
            if self.path == "/warmup":
                runtime.load()
                self._send_json({"status": "ready", "model_id": runtime.config.model_id})
                return

            if self.path == "/generate":
                payload = self._read_json()
                messages = payload.get("messages") or []
                generation = payload.get("generation") or {}
                raw_text = runtime.generate(messages=messages, generation=generation)
                self._send_json({"raw_text": raw_text})
                return

            self._send_json({"detail": "Not found"}, status=HTTPStatus.NOT_FOUND)
        except Exception as exc:  # pragma: no cover
            self._send_json({"detail": str(exc)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
        sys.stderr.write("[qwen-server] " + (format % args) + "\n")
        sys.stderr.flush()


def parse_args() -> argparse.Namespace:
    config = QwenConfig()
    parser = argparse.ArgumentParser(description="Manual Qwen HTTP server")
    parser.add_argument("--host", default=config.server_host)
    parser.add_argument("--port", type=int, default=config.server_port)
    parser.add_argument("--warmup", action="store_true", help="Load the model before serving requests.")
    return parser.parse_args()


def run_server() -> int:
    args = parse_args()
    config = QwenConfig(server_host=args.host, server_port=args.port)
    runtime = QwenWorkerRuntime(config)

    if args.warmup:
        runtime.load()
        print("[qwen-server] warmup complete", flush=True)

    QwenHttpHandler.runtime = runtime
    server = ThreadingHTTPServer((config.server_host, config.server_port), QwenHttpHandler)
    print(f"[qwen-server] listening on http://{config.server_host}:{config.server_port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("[qwen-server] shutting down", flush=True)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(run_server())
