"""Supervision of miner processes: start, pause, resume, stop, emergency stop.

Safety properties:
  * On Windows every miner is placed in a Job Object with KILL_ON_JOB_CLOSE, so
    closing HomeMiner (or HomeMiner crashing) ends the miner too. Nothing can
    keep mining in the background after HomeMiner exits.
  * Emergency stop kills every tracked miner immediately and then sweeps for any
    process whose executable lives in HomeMiner's tools folder.
  * HomeMiner never creates services, scheduled tasks, Run keys or startup items.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any

try:
    import psutil
except ImportError:  # pragma: no cover - psutil is a declared dependency
    psutil = None  # type: ignore[assignment]

log = logging.getLogger(__name__)

CREATE_NO_WINDOW = 0x08000000
BELOW_NORMAL_PRIORITY_CLASS = 0x00004000


class _WindowsJob:
    """Kill-on-close Job Object (Windows only)."""

    def __init__(self) -> None:
        import ctypes  # noqa: PLC0415
        from ctypes import wintypes  # noqa: PLC0415

        self._ctypes = ctypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self._kernel32 = kernel32

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [(n, ctypes.c_ulonglong) for n in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class BASIC(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class EXTENDED(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BASIC),
                ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.POINTER(EXTENDED),
                                                     wintypes.DWORD]

        self.handle = kernel32.CreateJobObjectW(None, None)
        if not self.handle:
            raise OSError(ctypes.get_last_error(), "CreateJobObjectW failed")
        info = EXTENDED()
        info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(self.handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
            raise OSError(ctypes.get_last_error(), "SetInformationJobObject failed")

    def assign(self, pid: int) -> None:
        handle = self._kernel32.OpenProcess(0x0100 | 0x0001, False, pid)  # SET_QUOTA | TERMINATE
        if not handle:
            raise OSError(self._ctypes.get_last_error(), "OpenProcess failed")
        try:
            if not self._kernel32.AssignProcessToJobObject(self.handle, handle):
                raise OSError(self._ctypes.get_last_error(), "AssignProcessToJobObject failed")
        finally:
            self._kernel32.CloseHandle(handle)


@dataclass
class ManagedProcess:
    label: str
    popen: subprocess.Popen
    args: list[str]
    started_at: float
    output_path: Path | None = None
    paused: bool = False
    stop_reason: str | None = None
    _output_handle: IO[Any] | None = field(default=None, repr=False)

    @property
    def pid(self) -> int:
        return self.popen.pid

    def running(self) -> bool:
        return self.popen.poll() is None


def _tree(pid: int) -> list[Any]:
    if psutil is None:
        return []
    try:
        parent = psutil.Process(pid)
        return [parent, *parent.children(recursive=True)]
    except psutil.Error:
        return []


class ProcessSupervisor:
    def __init__(self, tools_dir: Path | None = None) -> None:
        self.tools_dir = tools_dir
        self._lock = threading.RLock()
        self._procs: dict[str, ManagedProcess] = {}
        self._job: _WindowsJob | None = None
        if sys.platform == "win32":
            try:
                self._job = _WindowsJob()
            except Exception as exc:  # noqa: BLE001
                log.warning("Could not create a kill-on-close job object: %s", exc)

    @property
    def kill_on_close(self) -> bool:
        return self._job is not None

    def start(self, label: str, args: list[str], cwd: Path | None = None, output_path: Path | None = None,
              env: dict[str, str] | None = None) -> ManagedProcess:
        with self._lock:
            existing = self._procs.get(label)
            if existing and existing.running():
                raise RuntimeError(f"'{label}' is already running.")
            handle = output_path.open("wb") if output_path else subprocess.DEVNULL
            kwargs: dict[str, Any] = {"stdout": handle, "stderr": subprocess.STDOUT, "stdin": subprocess.DEVNULL,
                                      "cwd": str(cwd) if cwd else None, "env": env}
            if sys.platform == "win32":
                kwargs["creationflags"] = CREATE_NO_WINDOW | BELOW_NORMAL_PRIORITY_CLASS
            else:
                kwargs["start_new_session"] = True
            popen = subprocess.Popen(args, **kwargs)  # noqa: S603 - argument list built by HomeMiner
            if self._job is not None:
                try:
                    self._job.assign(popen.pid)
                except OSError as exc:
                    log.warning("Could not attach miner to the job object: %s", exc)
            proc = ManagedProcess(label, popen, list(args), time.time(), output_path,
                                  _output_handle=handle if output_path else None)
            self._procs[label] = proc
            return proc

    def get(self, label: str) -> ManagedProcess | None:
        return self._procs.get(label)

    def running_labels(self) -> list[str]:
        with self._lock:
            return [label for label, proc in self._procs.items() if proc.running()]

    def pause(self, label: str) -> bool:
        proc = self._procs.get(label)
        if not proc or not proc.running() or psutil is None:
            return False
        for p in _tree(proc.pid):
            try:
                p.suspend()
            except psutil.Error:
                pass
        proc.paused = True
        return True

    def resume(self, label: str) -> bool:
        proc = self._procs.get(label)
        if not proc or not proc.running() or psutil is None:
            return False
        for p in reversed(_tree(proc.pid)):
            try:
                p.resume()
            except psutil.Error:
                pass
        proc.paused = False
        return True

    def stop(self, label: str, reason: str = "stopped by user", timeout: float = 10.0) -> bool:
        proc = self._procs.get(label)
        if not proc:
            return False
        proc.stop_reason = proc.stop_reason or reason
        if proc.running():
            if proc.paused:
                self.resume(label)
            self._terminate(proc, timeout)
        self._close(proc)
        return True

    def _terminate(self, proc: ManagedProcess, timeout: float) -> None:
        tree = _tree(proc.pid)
        try:
            if sys.platform == "win32" or psutil is None:
                proc.popen.terminate()
            else:
                os.killpg(proc.pid, signal.SIGTERM)
        except (OSError, ProcessLookupError):
            pass
        try:
            proc.popen.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self._kill(proc, tree)

    def _kill(self, proc: ManagedProcess, tree: list[Any] | None = None) -> None:
        for p in tree if tree is not None else _tree(proc.pid):
            try:
                p.kill()
            except Exception:  # noqa: BLE001
                pass
        try:
            proc.popen.kill()
        except OSError:
            pass
        try:
            proc.popen.wait(timeout=5)
        except subprocess.TimeoutExpired:
            log.error("Process %s did not exit after kill", proc.pid)

    def _close(self, proc: ManagedProcess) -> None:
        if proc._output_handle is not None:
            try:
                proc._output_handle.close()
            except OSError:
                pass
            proc._output_handle = None

    def emergency_stop(self) -> dict[str, Any]:
        """Kill everything immediately. Safe to call at any time, repeatedly."""
        killed: list[str] = []
        with self._lock:
            for label, proc in list(self._procs.items()):
                if proc.running():
                    proc.stop_reason = "emergency stop"
                    self._kill(proc)
                    killed.append(f"{label} (pid {proc.pid})")
                self._close(proc)
        swept = sweep_tools_processes(self.tools_dir) if self.tools_dir else []
        return {"killed": killed, "swept": swept, "at": time.time()}


def sweep_tools_processes(tools_dir: Path | None) -> list[str]:
    """Kill any process whose executable lives under the tools folder."""
    if psutil is None or tools_dir is None:
        return []
    try:
        root = Path(tools_dir).resolve()
    except OSError:
        return []
    swept: list[str] = []
    for p in psutil.process_iter(["pid", "exe", "name"]):
        exe = p.info.get("exe")
        if not exe:
            continue
        try:
            if Path(exe).resolve().is_relative_to(root):
                p.kill()
                swept.append(f"{p.info.get('name')} (pid {p.info.get('pid')})")
        except (psutil.Error, OSError, ValueError):
            continue
    return swept
