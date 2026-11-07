"""Run lightsmith in child processes, isolated from the server.

Each job runs in a fresh ``python -I`` process with a scrubbed environment
(no database URLs, API keys or Stripe secrets) and a private working
directory. The parent enforces the wall-clock limit, polls host memory (RSS
of the whole process tree) and kills the tree on timeout, memory overrun or
cancellation. On the GPU, the child caps its own CUDA allocator with
``torch.cuda.set_per_process_memory_fraction``.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import psutil

# Environment variables a solver child may see. Everything else (secrets included) is dropped.
_ENV_KEEP = ("PATH", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "TEMP", "TMP", "TMPDIR", "LANG", "LC_ALL",
             "CUDA_VISIBLE_DEVICES", "CUDA_DEVICE_ORDER", "CUDA_HOME", "CUDA_PATH", "LD_LIBRARY_PATH",
             "NVIDIA_VISIBLE_DEVICES", "NVIDIA_DRIVER_CAPABILITIES", "OMP_NUM_THREADS", "MKL_NUM_THREADS",
             "OPENBLAS_NUM_THREADS", "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "USERPROFILE",
             "LOCALAPPDATA", "APPDATA", "PROGRAMDATA", "PROGRAMFILES", "COMMONPROGRAMFILES")


def child_env(workdir: Path, extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {k: os.environ[k] for k in _ENV_KEEP if k in os.environ}
    env["HOME"] = str(workdir)
    env["LIGHTSMITH_CACHE_DIR"] = str(workdir / "cache")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.update(extra or {})
    return env


def _command(*args: str) -> list[str]:
    return [sys.executable, "-I", "-X", "utf8", "-m", "lightsmith_cloud._child", *args]


def _popen(cmd: list[str], cwd: Path, env: dict[str, str], stdin=subprocess.DEVNULL) -> subprocess.Popen:
    kw: dict[str, Any] = {}
    if sys.platform == "win32":
        kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True
    return subprocess.Popen(cmd, cwd=cwd, env=env, stdin=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding="utf-8", errors="replace", bufsize=1, **kw)


def kill_tree(proc: subprocess.Popen) -> None:
    try:
        parent = psutil.Process(proc.pid)
        procs = parent.children(recursive=True) + [parent]
    except psutil.NoSuchProcess:
        procs = []
    for p in procs:
        try:
            p.kill()
        except psutil.NoSuchProcess:
            pass
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:  # pragma: no cover
        pass


def tree_rss(pid: int) -> int:
    try:
        p = psutil.Process(pid)
        return sum(q.memory_info().rss for q in [p, *p.children(recursive=True)])
    except psutil.Error:
        return 0


def _drain(stream) -> None:
    for _ in stream:
        pass


def _pump(stream, q: queue.Queue, tag: str) -> None:
    for line in stream:
        q.put((tag, line.rstrip("\n")))
    q.put((tag, None))


@dataclass
class Limits:
    timeout_s: float | None = None
    memory_bytes: int | None = None  # host RSS of the process tree
    gpu_memory_bytes: int | None = None  # CUDA allocator cap inside the child


@dataclass
class Outcome:
    status: str  # succeeded | failed | cancelled
    runtime_s: float
    error: dict | None = None
    device: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    peak_rss_bytes: int = 0
    result_kind: str | None = None


def run_job(request: dict, workdir: Path, limits: Limits, *, on_event: Callable[[dict], None],
            should_cancel: Callable[[], bool], poll_s: float = 0.2, log_tail: int = 40) -> Outcome:
    """Run one solve/compare request in a child; the result JSON is written to request['result_path']."""
    workdir.mkdir(parents=True, exist_ok=True)
    req = {**request, "memory_bytes": limits.memory_bytes, "gpu_memory_bytes": limits.gpu_memory_bytes}
    req_path = workdir / "request.json"
    req_path.write_text(json.dumps(req), encoding="utf-8")

    t0 = time.monotonic()
    proc = _popen(_command(str(req_path)), workdir, child_env(workdir))
    q: queue.Queue = queue.Queue()
    for stream, tag in ((proc.stdout, "out"), (proc.stderr, "err")):
        threading.Thread(target=_pump, args=(stream, q, tag), daemon=True).start()

    stderr_tail: deque[str] = deque(maxlen=log_tail)
    final: dict | None = None
    killed: dict | None = None
    cancelled = False
    peak = 0
    open_streams = 2
    while open_streams or proc.poll() is None:
        try:
            tag, line = q.get(timeout=poll_s)
            if line is None:
                open_streams -= 1
            elif tag == "err":
                stderr_tail.append(line)
                on_event({"type": "log", "stream": "stderr", "line": line[:2000]})
            else:
                try:
                    ev = json.loads(line)
                except ValueError:
                    on_event({"type": "log", "stream": "stdout", "line": line[:2000]})
                    continue
                if ev.get("type") in ("done", "error"):
                    final = ev
                else:
                    on_event(ev)
        except queue.Empty:
            pass
        if proc.poll() is not None or killed or cancelled:
            continue
        elapsed = time.monotonic() - t0
        rss = tree_rss(proc.pid)
        peak = max(peak, rss)
        if limits.timeout_s and elapsed > limits.timeout_s:
            killed = {"error": "Timeout", "message": f"job exceeded its runtime limit of {limits.timeout_s:g} s"}
        elif limits.memory_bytes and rss > limits.memory_bytes:
            killed = {"error": "MemoryLimitExceeded",
                      "message": f"solver used {rss / 2**30:.2f} GiB of host memory, above the "
                                 f"{limits.memory_bytes / 2**30:.2f} GiB limit; reduce the problem size"}
        elif should_cancel():
            cancelled = True
        if killed or cancelled:
            kill_tree(proc)
    proc.wait()
    runtime = time.monotonic() - t0

    if cancelled:
        return Outcome("cancelled", runtime, {"error": "Cancelled", "message": "job was cancelled"},
                       peak_rss_bytes=peak)
    if killed:
        return Outcome("failed", runtime, killed, peak_rss_bytes=peak)
    if final and final["type"] == "done" and proc.returncode == 0:
        return Outcome("succeeded", runtime, None, final.get("device") or {}, final.get("warnings") or [],
                       peak_rss_bytes=peak, result_kind=final.get("result_kind"))
    if final and final["type"] == "error":
        return Outcome("failed", runtime, {"error": final["error"], "message": final["message"]},
                       peak_rss_bytes=peak)
    tail = "\n".join(list(stderr_tail)[-10:])
    return Outcome("failed", runtime, {"error": "WorkerCrashed",
                                        "message": f"solver process exited with code {proc.returncode}; "
                                                   f"last output:\n{tail}"[:4000]}, peak_rss_bytes=peak)


class EstimateError(Exception):
    def __init__(self, error: str, message: str):
        super().__init__(message)
        self.error, self.message = error, message


class Estimator:
    """A warm, sandboxed child that answers estimate() requests one at a time.

    ``estimate()`` voxelises DDA problems, so a hostile spec could exhaust
    memory; the child is killed on timeout or memory overrun and restarted on
    the next request.
    """

    def __init__(self, workdir: Path, timeout_s: float = 60.0, memory_bytes: int | None = None,
                 max_requests: int = 500):
        self.workdir, self.timeout_s, self.memory_bytes, self.max_requests = workdir, timeout_s, memory_bytes, max_requests
        self._lock = threading.Lock()
        self._proc: subprocess.Popen | None = None
        self._q: queue.Queue = queue.Queue()
        self._served = 0
        self._seq = 0

    def _start(self) -> None:
        self.workdir.mkdir(parents=True, exist_ok=True)
        self._q = queue.Queue()
        self._proc = _popen(_command("--serve"), self.workdir, child_env(self.workdir), stdin=subprocess.PIPE)
        threading.Thread(target=_pump, args=(self._proc.stdout, self._q, "out"), daemon=True).start()
        threading.Thread(target=_drain, args=(self._proc.stderr,), daemon=True).start()
        self._served = 0

    def _stop(self) -> None:
        if self._proc is not None:
            kill_tree(self._proc)
            self._proc = None

    def close(self) -> None:
        with self._lock:
            self._stop()

    def estimate(self, simulation: dict, method: str, device: str) -> dict:
        return self._call({"simulation": simulation, "method": method, "device": device})

    def discretization(self, simulation: dict, method: str) -> dict:
        """What the backend will discretise (lightsmith.discretization), in the same sandboxed child as estimate."""
        return self._call({"op": "discretization", "simulation": simulation, "method": method, "device": "cpu"})

    def _call(self, payload: dict) -> dict:
        with self._lock:
            if self._proc is None or self._proc.poll() is not None or self._served >= self.max_requests:
                self._stop()
                self._start()
            self._seq += 1
            rid = self._seq
            assert self._proc is not None and self._proc.stdin is not None
            try:
                self._proc.stdin.write(json.dumps({"id": rid, **payload}) + "\n")
                self._proc.stdin.flush()
            except OSError:
                self._stop()
                raise EstimateError("WorkerCrashed", "estimate process is not running")
            self._served += 1
            t0 = time.monotonic()
            while True:
                try:
                    _, line = self._q.get(timeout=0.1)
                except queue.Empty:
                    line = ""
                if line is None:
                    self._stop()
                    raise EstimateError("WorkerCrashed", "estimate process exited unexpectedly")
                if line:
                    try:
                        reply = json.loads(line)
                    except ValueError:
                        continue
                    if reply.get("id") != rid:
                        continue
                    if reply["ok"]:
                        return reply["result"]
                    raise EstimateError(reply["error"], reply["message"])
                if time.monotonic() - t0 > self.timeout_s:
                    self._stop()
                    raise EstimateError("Timeout", f"estimate exceeded {self.timeout_s:g} s; the problem is too large")
                if self.memory_bytes and tree_rss(self._proc.pid) > self.memory_bytes:
                    self._stop()
                    raise EstimateError("MemoryLimitExceeded",
                                        f"estimate needed more than {self.memory_bytes / 2**30:.1f} GiB; "
                                        "the problem is too large")
