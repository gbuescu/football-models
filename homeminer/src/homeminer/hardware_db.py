"""Reference specifications, used only when the hardware cannot report a value.

Every value taken from here is labelled ``lookup`` in discovery reports, so it
is never mistaken for something read from the machine.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class GpuSpec:
    pattern: str
    architecture: str
    compute_capability: str
    board_power_w: int | None
    note: str = ""


# Ordered: first match wins. Patterns match nvidia-smi / WMI names.
NVIDIA_GPUS: list[GpuSpec] = [
    GpuSpec(r"GTX 970\b", "Maxwell (GM204)", "5.2", 145,
            "4 GB is split into 3.5 GB at full speed and 0.5 GB on a much slower path."),
    GpuSpec(r"GTX 980 Ti", "Maxwell (GM200)", "5.2", 250),
    GpuSpec(r"GTX 980\b", "Maxwell (GM204)", "5.2", 165),
    GpuSpec(r"GTX 960", "Maxwell (GM206)", "5.2", 120),
    GpuSpec(r"GTX 950", "Maxwell (GM206)", "5.2", 90),
    GpuSpec(r"GTX 750", "Maxwell (GM107)", "5.0", 60),
    GpuSpec(r"TITAN X\b(?! ?\(?Pascal)", "Maxwell (GM200)", "5.2", 250),
    GpuSpec(r"GTX 10[5-8]0", "Pascal", "6.1", None),
    GpuSpec(r"GTX 16[5-6]0", "Turing", "7.5", None),
    GpuSpec(r"RTX 20[6-8]0", "Turing", "7.5", None),
    GpuSpec(r"RTX 30[5-9]0", "Ampere", "8.6", None),
    GpuSpec(r"RTX 40[5-9]0", "Ada Lovelace", "8.9", None),
    GpuSpec(r"RTX 50[5-9]0", "Blackwell", "12.0", None),
]


def lookup_gpu(name: str) -> GpuSpec | None:
    for spec in NVIDIA_GPUS:
        if re.search(spec.pattern, name or "", re.IGNORECASE):
            return spec
    return None


def architecture_for_cc(cc: str | None) -> str | None:
    if not cc:
        return None
    try:
        major = int(cc.split(".")[0])
    except ValueError:
        return None
    return {
        3: "Kepler", 5: "Maxwell", 6: "Pascal", 7: "Volta/Turing",
        8: "Ampere/Ada", 9: "Hopper", 10: "Blackwell", 12: "Blackwell",
    }.get(major)


@dataclass(frozen=True)
class CpuSpec:
    pattern: str
    performance_cores: int
    efficiency_cores: int
    threads: int
    l3_mb: int
    base_power_w: int
    max_turbo_power_w: int
    raptor_lake_desktop: bool


INTEL_CPUS: list[CpuSpec] = [
    CpuSpec(r"i5-14400F?\b", 6, 4, 16, 20, 65, 148, True),
    CpuSpec(r"i5-13400F?\b", 6, 4, 16, 20, 65, 148, True),
    CpuSpec(r"i5-14600KF?\b", 6, 8, 20, 24, 125, 181, True),
    CpuSpec(r"i7-14700KF?\b", 8, 12, 28, 33, 125, 253, True),
]

_RAPTOR_DESKTOP_RE = re.compile(r"\bi[3579]-1[34]\d{3}[A-Z]*\b", re.IGNORECASE)


def lookup_cpu(name: str) -> CpuSpec | None:
    for spec in INTEL_CPUS:
        if re.search(spec.pattern, name or "", re.IGNORECASE):
            return spec
    return None


def is_intel_13th_14th_gen_desktop(name: str) -> bool:
    """True for Core i3/i5/i7/i9 13xxx and 14xxx desktop parts (not mobile H/U/P)."""
    match = _RAPTOR_DESKTOP_RE.search(name or "")
    if not match:
        return False
    suffix = re.sub(r"^i[3579]-1[34]\d{3}", "", match.group(0), flags=re.IGNORECASE).upper()
    return not any(m in suffix for m in ("H", "U", "P", "HX"))
