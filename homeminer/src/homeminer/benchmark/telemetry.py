"""Read-only sampling of utilisation, temperature and power.

Sources, in order of preference:
  GPU  nvidia-smi (board sensor)
  CPU  psutil for utilisation; LibreHardwareMonitor's WMI namespace for package
       temperature and power, only if you run LibreHardwareMonitor yourself.
Windows has no built-in CPU temperature or power reading, so without
LibreHardwareMonitor those values are reported as unavailable, never guessed.
"""

from __future__ import annotations

import json
import statistics
import sys
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Protocol

from ..discovery import CommandResult, Runner, _clean, _num, default_runner, find_nvidia_smi

try:
    import psutil
except ImportError:  # pragma: no cover
    psutil = None  # type: ignore[assignment]


@dataclass
class Sample:
    t: float
    cpu_util_pct: float | None = None
    cpu_temp_c: float | None = None
    cpu_power_w: float | None = None
    gpu_util_pct: float | None = None
    gpu_temp_c: float | None = None
    gpu_power_w: float | None = None
    gpu_mem_used_mb: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class TelemetrySource(Protocol):
    def sample(self) -> Sample: ...


LHM_SCRIPT = (
    "$ErrorActionPreference='Stop';"
    "Get-CimInstance -Namespace root/LibreHardwareMonitor -ClassName Sensor | "
    "Where-Object { $_.Identifier -like '/intelcpu/*' -or $_.Identifier -like '/amdcpu/*' } | "
    "Where-Object { $_.SensorType -eq 'Temperature' -or $_.SensorType -eq 'Power' } | "
    "Select-Object Name, SensorType, Value | ConvertTo-Json -Compress"
)


def parse_lhm(text: str) -> tuple[float | None, float | None]:
    """Return (cpu_package_temp_c, cpu_package_power_w) from LibreHardwareMonitor JSON."""
    try:
        data = json.loads(text)
    except ValueError:
        return None, None
    rows = data if isinstance(data, list) else [data]
    temps: dict[str, float] = {}
    power = None
    for row in rows:
        name = str(row.get("Name", ""))
        kind = row.get("SensorType")
        val = row.get("Value")
        if not isinstance(val, (int, float)):
            continue
        if kind == "Temperature":
            temps[name] = float(val)
        elif kind == "Power" and name in ("CPU Package", "Package"):
            power = float(val)
    temp = next((temps[n] for n in ("CPU Package", "Core (Tctl/Tdie)", "Core Max") if n in temps), None)
    return temp, power


class SystemTelemetry:
    def __init__(self, gpu_index: int | None = None, runner: Runner = default_runner,
                 platform_name: str | None = None, lhm_interval_s: float = 5.0,
                 clock: Callable[[], float] = time.time):
        self.platform_name = platform_name or sys.platform
        self.runner = runner
        self.gpu_index = gpu_index
        self.clock = clock
        self.nvidia_smi = find_nvidia_smi(self.platform_name) if gpu_index is not None else None
        self._lhm_enabled = self.platform_name == "win32"
        self._lhm_interval = lhm_interval_s
        self._lhm_last = 0.0
        self._lhm_cache: tuple[float | None, float | None] = (None, None)
        if psutil is not None:
            psutil.cpu_percent(interval=None)  # prime: the first reading is meaningless

    def _gpu(self) -> dict[str, float | None]:
        if not self.nvidia_smi or self.gpu_index is None:
            return {}
        fields = ["utilization.gpu", "temperature.gpu", "power.draw", "memory.used"]
        result: CommandResult = self.runner(
            [self.nvidia_smi, f"--id={self.gpu_index}", f"--query-gpu={','.join(fields)}",
             "--format=csv,noheader,nounits"], 10.0)
        if result.returncode != 0 or not result.stdout.strip():
            return {}
        parts = [p.strip() for p in result.stdout.strip().splitlines()[0].split(",")]
        if len(parts) != len(fields):
            return {}
        util, temp, power, mem = (_num(_clean(p)) for p in parts)
        return {"gpu_util_pct": util, "gpu_temp_c": temp, "gpu_power_w": power, "gpu_mem_used_mb": mem}

    def _lhm(self) -> tuple[float | None, float | None]:
        if not self._lhm_enabled:
            return None, None
        now = self.clock()
        if now - self._lhm_last < self._lhm_interval:
            return self._lhm_cache
        self._lhm_last = now
        result = self.runner(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", LHM_SCRIPT], 10.0)
        if result.returncode != 0 or not result.stdout.strip():
            self._lhm_enabled = False  # not running: stop asking
            return None, None
        self._lhm_cache = parse_lhm(result.stdout)
        return self._lhm_cache

    def sample(self) -> Sample:
        s = Sample(t=self.clock())
        if psutil is not None:
            s.cpu_util_pct = float(psutil.cpu_percent(interval=None))
        s.cpu_temp_c, s.cpu_power_w = self._lhm()
        for key, val in self._gpu().items():
            setattr(s, key, val)
        return s


@dataclass
class TelemetrySummary:
    samples: int = 0
    cpu_util_avg: float | None = None
    cpu_util_peak: float | None = None
    cpu_temp_peak: float | None = None
    cpu_power_avg: float | None = None
    gpu_util_avg: float | None = None
    gpu_util_peak: float | None = None
    gpu_temp_peak: float | None = None
    gpu_power_avg: float | None = None
    gpu_power_peak: float | None = None
    gpu_mem_peak_mb: float | None = None
    sources: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def summarise(samples: list[Sample]) -> TelemetrySummary:
    def series(name: str) -> list[float]:
        return [getattr(s, name) for s in samples if getattr(s, name) is not None]

    def avg(name: str) -> float | None:
        values = series(name)
        return round(statistics.fmean(values), 1) if values else None

    def peak(name: str) -> float | None:
        values = series(name)
        return round(max(values), 1) if values else None

    summary = TelemetrySummary(
        samples=len(samples),
        cpu_util_avg=avg("cpu_util_pct"), cpu_util_peak=peak("cpu_util_pct"),
        cpu_temp_peak=peak("cpu_temp_c"), cpu_power_avg=avg("cpu_power_w"),
        gpu_util_avg=avg("gpu_util_pct"), gpu_util_peak=peak("gpu_util_pct"),
        gpu_temp_peak=peak("gpu_temp_c"), gpu_power_avg=avg("gpu_power_w"),
        gpu_power_peak=peak("gpu_power_w"), gpu_mem_peak_mb=peak("gpu_mem_used_mb"),
    )
    summary.sources = {
        "cpu_util": "psutil" if series("cpu_util_pct") else "unavailable",
        "cpu_temp": "LibreHardwareMonitor (WMI)" if series("cpu_temp_c") else "unavailable",
        "cpu_power": "LibreHardwareMonitor (WMI)" if series("cpu_power_w") else "unavailable",
        "gpu": "nvidia-smi" if series("gpu_util_pct") or series("gpu_temp_c") else "unavailable",
    }
    return summary
