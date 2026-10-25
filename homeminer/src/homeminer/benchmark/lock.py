"""Isolation lock: one benchmark or mining session at a time, across processes.

Running CPU and GPU work together would contaminate both measurements (shared
power budget, heat, memory bandwidth and the CPU time the GPU miner needs), so
HomeMiner serialises them unless you explicitly allow it.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

try:
    import psutil
except ImportError:  # pragma: no cover
    psutil = None  # type: ignore[assignment]


class BusyError(RuntimeError):
    pass


def _alive(pid: int) -> bool:
    if pid == os.getpid():
        return True
    if psutil is not None:
        return psutil.pid_exists(pid)
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


class IsolationLock:
    _local = threading.Lock()
    _held: dict[str, str] = {}

    def __init__(self, data_dir: Path, allow_simultaneous: bool = False):
        self.dir = data_dir / "locks"
        self.allow_simultaneous = allow_simultaneous

    def holders(self) -> list[dict]:
        out = []
        if not self.dir.exists():
            return out
        for path in self.dir.glob("*.lock"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                continue
            if _alive(int(data.get("pid", -1))):
                out.append(data)
            else:
                path.unlink(missing_ok=True)  # stale lock from a crashed run
        return out

    def acquire(self, label: str) -> None:
        with IsolationLock._local:
            busy = [h for h in self.holders() if h.get("label") != label]
            if busy and not self.allow_simultaneous:
                names = ", ".join(h["label"] for h in busy)
                raise BusyError(f"{names} is already running. Benchmarks run one at a time so results stay "
                                "comparable. Stop it first.")
            if any(h.get("label") == label for h in self.holders()):
                raise BusyError(f"{label} is already running.")
            self.dir.mkdir(parents=True, exist_ok=True)
            (self.dir / f"{label}.lock").write_text(
                json.dumps({"label": label, "pid": os.getpid(), "since": time.time()}), encoding="utf-8")

    def release(self, label: str) -> None:
        with IsolationLock._local:
            (self.dir / f"{label}.lock").unlink(missing_ok=True)

    def others_running(self, label: str) -> bool:
        return any(h.get("label") != label for h in self.holders())
