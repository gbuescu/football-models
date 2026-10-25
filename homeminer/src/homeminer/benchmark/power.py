"""Power figures, always labelled with how they were obtained.

  measured-wall    a plug-in meter reading you entered (most trustworthy)
  measured-sensor  a hardware sensor (nvidia-smi board power, LibreHardwareMonitor package power)
  estimated        a simple model from utilisation; treat with caution

Figures are the *extra* power drawn by the benchmark: the reading while it
runs minus an idle baseline taken just before it starts.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from .telemetry import Sample, summarise


@dataclass
class PowerFigure:
    watts: float | None
    kind: str  # measured-wall | measured-sensor | estimated | unavailable
    method: str
    baseline_w: float | None = None
    during_w: float | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def _avg(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def cpu_power(samples: list[Sample], baseline: list[Sample], power_cfg: dict) -> PowerFigure:
    wall = float(power_cfg.get("wall_meter_cpu_watts") or 0)
    if wall > 0:
        return PowerFigure(round(wall, 1), "measured-wall", "Wall-meter reading you entered for the CPU benchmark.")
    during = _avg([s.cpu_power_w for s in samples if s.cpu_power_w is not None])
    if during is not None:
        base = _avg([s.cpu_power_w for s in baseline if s.cpu_power_w is not None])
        extra = during - (base or 0.0)
        return PowerFigure(round(max(extra, 0.0), 1), "measured-sensor",
                           "CPU package power from LibreHardwareMonitor, minus the idle baseline. "
                           "Excludes VRM, RAM and PSU losses.", base, round(during, 1))
    util = summarise(samples).cpu_util_avg
    if util is None:
        return PowerFigure(None, "unavailable", "No CPU utilisation or power data.")
    idle = float(power_cfg.get("cpu_idle_watts", 10))
    full = float(power_cfg.get("cpu_full_load_watts", 65))
    extra = max(full - idle, 0.0) * util / 100.0
    return PowerFigure(round(extra, 1), "estimated",
                       f"Model: ({full:.0f} W full load - {idle:.0f} W idle) x {util:.0f}% average utilisation. "
                       "Your board's power limits may differ; a wall meter gives a real figure.")


def gpu_power(samples: list[Sample], baseline: list[Sample], power_cfg: dict) -> PowerFigure:
    wall = float(power_cfg.get("wall_meter_gpu_watts") or 0)
    if wall > 0:
        return PowerFigure(round(wall, 1), "measured-wall", "Wall-meter reading you entered for the GPU benchmark.")
    during = _avg([s.gpu_power_w for s in samples if s.gpu_power_w is not None])
    if during is not None:
        base = _avg([s.gpu_power_w for s in baseline if s.gpu_power_w is not None])
        extra = during - (base or 0.0)
        return PowerFigure(round(max(extra, 0.0), 1), "measured-sensor",
                           "GPU board power from nvidia-smi, minus the idle baseline. Excludes PSU losses.",
                           None if base is None else round(base, 1), round(during, 1))
    util = summarise(samples).gpu_util_avg
    board = float(power_cfg.get("gpu_board_power_watts", 145))
    if util is None:
        return PowerFigure(None, "unavailable", "No GPU utilisation or power data.")
    return PowerFigure(round(board * util / 100.0, 1), "estimated",
                       f"Model: {board:.0f} W reference board power x {util:.0f}% average utilisation.")
