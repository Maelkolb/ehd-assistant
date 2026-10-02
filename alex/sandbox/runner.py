"""Run model-written Python and bring back what it printed and displayed."""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

WORKER = Path(__file__).with_name("worker.py")
MAX_CODE_CHARS = 20_000
PASSED_ENVIRONMENT = ("SYSTEMROOT", "WINDIR", "PATH", "LANG", "LC_ALL", "TZ")


@dataclass
class SandboxResult:
    ok: bool
    output: str = ""
    error: str = ""
    artifacts: list[dict] = field(default_factory=list)
    duration_s: float = 0.0


class Sandbox:
    """mode: 'subprocess' runs the guarded worker locally; 'http' sends the
    code to the sandbox service; 'off' disables code execution."""

    def __init__(
        self,
        mode: str,
        store_path: Path,
        corpus_dir: Path | None = None,
        url: str = "",
        token: str = "",
        timeout_seconds: int = 40,
        memory_mb: int = 1024,
        max_parallel: int = 2,
    ):
        self.mode = mode if mode in ("subprocess", "http") else "off"
        self.store_path = Path(store_path).resolve()
        self.corpus_dir = Path(corpus_dir).resolve() if corpus_dir else None
        self.url = url.rstrip("/")
        self.token = token
        self.timeout_seconds = timeout_seconds
        self.memory_mb = memory_mb
        self.slots = threading.BoundedSemaphore(max_parallel)
        self.cache_lock = threading.Lock()
        self.chart_cache_template = Path(tempfile.gettempdir()) / "alex-sandbox-fonts"
        self.chart_cache_template.mkdir(exist_ok=True)
        if self.mode == "http" and not self.url:
            logger.warning("SANDBOX_MODE=http without SANDBOX_URL — code execution disabled.")
            self.mode = "off"

    @property
    def enabled(self) -> bool:
        return self.mode != "off"

    def run(self, code: str) -> SandboxResult:
        if not self.enabled:
            return SandboxResult(False, error="Code execution is not available.")
        if len(code) > MAX_CODE_CHARS:
            return SandboxResult(False, error=f"Code too long (max {MAX_CODE_CHARS} characters).")
        if not self.slots.acquire(timeout=self.timeout_seconds):
            return SandboxResult(False, error="The sandbox is busy; try again shortly.")
        try:
            if self.mode == "http":
                return self.run_remote(code)
            return self.run_local(code)
        finally:
            self.slots.release()

    def run_local(self, code: str) -> SandboxResult:
        workdir = Path(tempfile.mkdtemp(prefix="alex-sandbox-"))
        started = time.time()
        try:
            job = {
                "code": code,
                "workdir": str(workdir),
                "store_path": str(self.store_path),
                "corpus_dir": str(self.corpus_dir)
                if self.corpus_dir and self.corpus_dir.exists()
                else "",
                "memory_mb": self.memory_mb,
                "timeout_seconds": self.timeout_seconds,
            }
            job_path = workdir / "job.json"
            job_path.write_text(json.dumps(job), encoding="utf-8")
            chart_cache = workdir / "mpl"
            self.seed_chart_cache(chart_cache)
            environment = {
                name: os.environ[name] for name in PASSED_ENVIRONMENT if name in os.environ
            }
            environment.update(
                {
                    "HOME": str(workdir),
                    "USERPROFILE": str(workdir),
                    "TMPDIR": str(workdir),
                    "TEMP": str(workdir),
                    "TMP": str(workdir),
                    "MPLBACKEND": "Agg",
                    "MPLCONFIGDIR": str(chart_cache),
                    "PYTHONIOENCODING": "utf-8",
                    "PYTHONDONTWRITEBYTECODE": "1",
                    "PYTHONUTF8": "1",
                    "OPENBLAS_NUM_THREADS": "2",
                    "OMP_NUM_THREADS": "2",
                }
            )
            try:
                completed = subprocess.run(
                    [sys.executable, "-I", str(WORKER), str(job_path)],
                    cwd=workdir,
                    env=environment,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=self.timeout_seconds,
                    stdin=subprocess.DEVNULL,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except subprocess.TimeoutExpired:
                return SandboxResult(
                    False,
                    error=f"Timed out after {self.timeout_seconds} s. Work on less data "
                    "or aggregate in SQL first.",
                    duration_s=round(time.time() - started, 2),
                )
            result_path = workdir / "result.json"
            if not result_path.exists():
                reason = (completed.stderr or "").strip().splitlines()[-1:] or ["no result"]
                killed = completed.returncode < 0 or "MemoryError" in (completed.stderr or "")
                return SandboxResult(
                    False,
                    error="The run was stopped (memory or CPU limit)."
                    if killed
                    else f"The sandbox failed to start: {reason[0][:300]}",
                    duration_s=round(time.time() - started, 2),
                )
            result = json.loads(result_path.read_text(encoding="utf-8"))
            self.keep_chart_cache(chart_cache)
            return SandboxResult(
                result["ok"],
                result["output"],
                result["error"],
                result["artifacts"],
                result["duration_s"],
            )
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    def seed_chart_cache(self, chart_cache: Path) -> None:
        """Each run gets its own copy of matplotlib's font cache: building it
        takes seconds, sharing it would give runs a common writable place."""
        if any(self.chart_cache_template.glob("*")):
            shutil.copytree(self.chart_cache_template, chart_cache)
        else:
            chart_cache.mkdir()

    def keep_chart_cache(self, chart_cache: Path) -> None:
        with self.cache_lock:
            if not any(self.chart_cache_template.glob("*")):
                shutil.copytree(chart_cache, self.chart_cache_template, dirs_exist_ok=True)

    def run_remote(self, code: str) -> SandboxResult:
        import urllib.error
        import urllib.request

        request = urllib.request.Request(
            self.url + "/run",
            data=json.dumps({"code": code}).encode(),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.token}"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds + 15) as response:
                result = json.loads(response.read())
        except (urllib.error.URLError, TimeoutError, ValueError) as error:
            logger.error("Sandbox service unreachable: %s", error)
            return SandboxResult(False, error="The sandbox service is unavailable.")
        return SandboxResult(
            result.get("ok", False),
            result.get("output", ""),
            result.get("error", ""),
            result.get("artifacts", []),
            result.get("duration_s", 0.0),
        )
