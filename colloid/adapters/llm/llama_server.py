"""Lifecycle management for a local llama.cpp model server (``llama_cpp.server``).

The engine starts the server before a run and stops it afterwards. It serves several GGUF
models from one process (each addressable by alias through the OpenAI-compatible API).
The server is idle - zero CPU - while the evaluator benchmarks, because the engine
alternates strictly between a *proposal phase* (LLM calls) and an *evaluation phase*
(builds and measurements); LLM inference never overlaps with a timed benchmark.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from collections.abc import Mapping
from pathlib import Path

import httpx


class LlamaServer:
    def __init__(
        self,
        models: Mapping[str, str],
        *,
        host: str = "127.0.0.1",
        port: int = 8088,
        n_ctx: int = 8192,
        n_threads: int = 4,
        log_path: Path = Path("/opt/colloid/logs/llama_server.log"),
        python: str = sys.executable,
    ) -> None:
        self.models = dict(models)  # alias -> gguf path
        self.host, self.port = host, port
        self.n_ctx, self.n_threads = n_ctx, n_threads
        self.log_path = log_path
        self.python = python
        self.proc: subprocess.Popen[bytes] | None = None

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def healthy(self) -> bool:
        try:
            return httpx.get(f"{self.base_url}/v1/models", timeout=2.0).status_code == 200
        except httpx.HTTPError:
            return False

    def start(self, timeout: float = 180.0) -> None:
        if self.healthy():
            return
        for alias, path in self.models.items():
            if not Path(path).exists():
                raise FileNotFoundError(f"model file for {alias} not found: {path}")
        config = {
            "host": self.host,
            "port": self.port,
            "models": [
                {"model": path, "model_alias": alias, "n_ctx": self.n_ctx, "n_threads": self.n_threads, "n_batch": 512, "verbose": False}
                for alias, path in self.models.items()
            ],
        }
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        cfg_path = self.log_path.with_suffix(".json")
        cfg_path.write_text(json.dumps(config, indent=2))
        log = open(self.log_path, "ab")  # noqa: SIM115 - handle owned by the child process
        self.proc = subprocess.Popen(
            [self.python, "-m", "llama_cpp.server", "--config_file", str(cfg_path)], stdout=log, stderr=log, start_new_session=True
        )
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(f"llama server exited with {self.proc.returncode}; see {self.log_path}")
            if self.healthy():
                return
            time.sleep(0.5)
        self.stop()
        raise TimeoutError("llama server did not become healthy")

    def stop(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None
