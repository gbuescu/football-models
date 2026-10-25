"""Algorithm catalogue and compatibility rules.

Memory requirements carry their source and date because several of them grow
over time (DAG sizes). Anything HomeMiner has not been able to verify is
marked "unverified" rather than assumed to work.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from .discovery import value

# Usable VRAM is reduced by what Windows and open apps already hold, plus headroom.
VRAM_HEADROOM_MB = 150
# The GTX 970 exposes 4 GB, but only the first 3.5 GB run at full speed.
GTX970_FAST_SEGMENT_MB = 3584
RANDOMX_DATASET_MB = 2080
RANDOMX_SCRATCHPAD_MB = 2


@dataclass(frozen=True)
class AlgorithmSpec:
    key: str
    algorithm: str
    title: str
    device: str
    miner: str | None
    nicehash_name: str | None
    memory_mb: float | None
    memory_note: str
    memory_source: str
    benchmark: str  # offline | pool | none
    route: str = "nicehash"  # nicehash | educational | forbidden


CATALOG: list[AlgorithmSpec] = [
    AlgorithmSpec("cpu-rx0", "rx/0", "RandomX (Monero, current)", "cpu", "XMRig 6.26.0", "RANDOMXMONERO",
                  RANDOMX_DATASET_MB, "2,080 MiB dataset in RAM plus 2 MiB per thread; best with 2 MB L3 per thread",
                  "XMRig source (primary)", "offline"),
    AlgorithmSpec("cpu-rx2", "rx/2", "RandomX v2 (Monero, upcoming)", "cpu", "XMRig 6.26.0", None,
                  RANDOMX_DATASET_MB, "As RandomX; Monero's switch was still on stressnet in October 2026",
                  "XMRig source (primary); cryptoticker (secondary)", "offline"),
    AlgorithmSpec("gpu-rx0", "rx/0", "RandomX on GPU (CUDA)", "gpu", "XMRig 6.26.0 + xmrig-cuda 6.22.1", "RANDOMXMONERO",
                  RANDOMX_DATASET_MB + 512, "2,080 MiB dataset in VRAM plus scratchpads (auto-sized)",
                  "XMRig/xmrig-cuda (primary); not tested on a GTX 970", "offline"),
    AlgorithmSpec("gpu-kawpow", "kawpow", "KawPow (Ravencoin)", "gpu", None, "KAWPOW",
                  5912, "DAG about 5.9 GB on 2026-10-20, growing 8 MB per epoch",
                  "Kryptex DAG calculator (anecdotal)", "none"),
    AlgorithmSpec("gpu-etchash", "etchash", "Etchash (Ethereum Classic)", "gpu", None, "ETCHASH",
                  4500, "DAG above 4 GB by 2026 (about 4.5 GB from the ECIP-1099 epoch schedule)",
                  "estimate", "none"),
    AlgorithmSpec("gpu-autolykos", "autolykos2", "Autolykos2 (Ergo)", "gpu", None, "AUTOLYKOS",
                  None, "Dataset grows about 5% every 51,200 blocks; current size not verified",
                  "Ergo blog (secondary)", "none"),
    AlgorithmSpec("gpu-zhash", "equihash144_5", "Equihash 144,5 (ZHash)", "gpu", "lolMiner (Maxwell support not reconfirmed)",
                  "ZHASH", None, "Low; believed to fit 4 GB, not verified", "unverified", "pool"),
    AlgorithmSpec("gpu-beamv3", "beamhash3", "BeamHash III", "gpu", "lolMiner (Maxwell support not reconfirmed)",
                  "BEAMV3", None, "Believed to fit 4 GB, not verified", "unverified", "pool"),
    AlgorithmSpec("cpu-sha256", "sha256d", "SHA-256d (Bitcoin)", "cpu", None, "SHA256",
                  0, "Trivial", "-", "none", route="educational"),
    AlgorithmSpec("gpu-sha256", "sha256d", "SHA-256d (Bitcoin)", "gpu", None, "SHA256",
                  0, "Trivial", "-", "none", route="educational"),
]

FORBIDDEN_COINS = {"eth", "ethereum", "eth-mainnet"}


def is_forbidden_target(coin: str | None) -> bool:
    """Ethereum mainnet is proof of stake and cannot be mined. Refuse it everywhere."""
    return (coin or "").strip().lower() in FORBIDDEN_COINS


def spec_for(key: str) -> AlgorithmSpec:
    for spec in CATALOG:
        if spec.key == key:
            return spec
    raise KeyError(key)


def specs_for_device(device: str) -> list[AlgorithmSpec]:
    return [s for s in CATALOG if s.device == device]


@dataclass
class Assessment:
    key: str
    algorithm: str
    title: str
    device: str
    status: str  # compatible | likely | unverified | unlikely | incompatible | educational
    summary: str
    reasons: list[str] = field(default_factory=list)
    miner: str | None = None
    benchmark: str = "none"
    nicehash_name: str | None = None
    recommended: dict[str, Any] = field(default_factory=dict)


def _float(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _cc_tuple(cc: str | None) -> tuple[int, int] | None:
    if not cc:
        return None
    try:
        major, _, minor = str(cc).partition(".")
        return int(major), int(minor or 0)
    except ValueError:
        return None


def usable_vram_mb(gpu: dict) -> float | None:
    total = _float(value(gpu, "vram_mb"))
    if total is None:
        return None
    name = str(value(gpu, "name") or "")
    if "GTX 970" in name.upper():
        total = min(total, GTX970_FAST_SEGMENT_MB)
    used = _float(value(gpu, "vram_used_mb")) or 0.0
    return max(total - used - VRAM_HEADROOM_MB, 0.0)


def recommended_cpu_threads(logical: int | None, l3_mb: float | None, hint_pct: int = 50) -> dict[str, Any]:
    """RandomX wants 2 MB of L3 per thread; HomeMiner then applies the conservative hint."""
    if not logical:
        return {}
    cache_limited = int(l3_mb // 2) if l3_mb else logical
    optimum = max(1, min(logical, cache_limited))
    hinted = max(1, (logical * max(1, min(hint_pct, 100))) // 100)
    return {
        "cache_optimal_threads": optimum,
        "threads_at_hint": min(hinted, optimum),
        "hint_pct": hint_pct,
    }


def pick_cuda_build(driver_cuda: str | None) -> str | None:
    """Choose the newest xmrig-cuda build the installed driver can run."""
    version = _cc_tuple(driver_cuda)
    if version is None:
        return None
    for build, need in (("cuda12_9", (12, 9)), ("cuda11_8", (11, 8)), ("cuda10_2", (10, 2))):
        if version >= need:
            return build
    return None


def assess(report: dict, cpu_hint_pct: int = 50) -> list[dict[str, Any]]:
    results: list[Assessment] = []

    # CPU
    logical = value(report, "cpu", "logical_processors")
    l3 = _float(value(report, "cpu", "l3_cache_mb"))
    avail_gb = _float(value(report, "memory", "available_gb"))
    threads = recommended_cpu_threads(int(logical) if logical else None, l3, cpu_hint_pct)
    for spec in specs_for_device("cpu"):
        if spec.route == "educational":
            results.append(Assessment(spec.key, spec.algorithm, spec.title, "cpu", "educational",
                                      "Runs, but worthless against ASICs. Educational estimate only.",
                                      benchmark="none", nicehash_name=spec.nicehash_name))
            continue
        reasons = []
        status = "compatible"
        need_gb = (RANDOMX_DATASET_MB + RANDOMX_SCRATCHPAD_MB * (threads.get("threads_at_hint") or 8) + 256) / 1024
        if avail_gb is not None and avail_gb < need_gb:
            status = "unlikely"
            reasons.append(f"Only {avail_gb} GB RAM free; RandomX fast mode needs about {need_gb:.1f} GB.")
        if value(report, "cpu", "avx2") is False:
            reasons.append("No AVX2: RandomX will run, but slower.")
        if spec.key == "cpu-rx2":
            reasons.append("Monero has not switched to RandomX v2 on mainnet yet; NiceHash pricing for it is unknown.")
        summary = (f"{spec.miner}; about {threads.get('threads_at_hint')} threads at the "
                   f"{cpu_hint_pct}% limit (cache optimum {threads.get('cache_optimal_threads')})."
                   if threads else f"{spec.miner}.")
        results.append(Assessment(spec.key, spec.algorithm, spec.title, "cpu", status, summary, reasons,
                                  spec.miner, spec.benchmark, spec.nicehash_name, threads))

    # GPUs
    driver_cuda = value(report, "nvidia", "driver_cuda_version")
    for gpu in report.get("gpus", []):
        cc = _cc_tuple(value(gpu, "compute_capability"))
        usable = usable_vram_mb(gpu)
        name = value(gpu, "name") or "GPU"
        for spec in specs_for_device("gpu"):
            reasons: list[str] = []
            if spec.route == "educational":
                results.append(Assessment(spec.key, spec.algorithm, spec.title, "gpu", "educational",
                                          "No maintained official GPU SHA-256 miner for this card; ASICs dominate.",
                                          benchmark="none", nicehash_name=spec.nicehash_name))
                continue
            status = "unverified"
            if spec.memory_mb is not None and usable is not None:
                if spec.memory_mb > usable:
                    status = "incompatible"
                    reasons.append(f"Needs about {spec.memory_mb:.0f} MB; about {usable:.0f} MB usable on {name}.")
            if spec.key == "gpu-rx0" and status != "incompatible":
                build = pick_cuda_build(driver_cuda)
                if cc is None or cc < (5, 0):
                    status = "incompatible"
                    reasons.append("xmrig-cuda needs compute capability 5.0 or newer.")
                elif build is None:
                    status = "unverified"
                    reasons.append("Driver CUDA version unknown; cannot pick an xmrig-cuda build.")
                else:
                    status = "likely"
                    reasons.append(f"Use the xmrig-cuda {build} build (driver supports CUDA {driver_cuda}).")
                    reasons.append("Not yet tested on this card: the offline benchmark will confirm.")
                results.append(Assessment(spec.key, spec.algorithm, spec.title, "gpu", status,
                                          "; ".join(reasons[:1]) or spec.memory_note, reasons, spec.miner,
                                          spec.benchmark, spec.nicehash_name, {"cuda_build": build}))
                continue
            if spec.key == "gpu-autolykos" and status == "unverified":
                status = "unlikely"
                reasons.append("Dataset size not verified; treated as not fitting a 4 GB Maxwell card.")
            if spec.benchmark == "pool" and status == "unverified":
                reasons.append("Can only be measured by connecting to a pool: needs your approval and a payout account.")
            summary = reasons[0] if reasons else spec.memory_note
            results.append(Assessment(spec.key, spec.algorithm, spec.title, "gpu", status, summary, reasons,
                                      spec.miner, spec.benchmark, spec.nicehash_name))
    return [asdict(r) for r in results]


def limitations(report: dict) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []

    def add(severity: str, area: str, message: str, source: str = "") -> None:
        out.append({"severity": severity, "area": area, "message": message, "source": source})

    if value(report, "os", "platform") != "win32":
        add("warning", "OS", "Not running on Windows. Results are for development only.")
    py_note = value(report, "prerequisites", "python")
    if py_note and tuple(int(x) for x in str(py_note).split(".")[:2]) < (3, 11):
        add("blocker", "Python", "HomeMiner needs Python 3.11 or newer.")
    if value(report, "prerequisites", "psutil") is False:
        add("warning", "Python", "psutil is not installed: CPU utilisation cannot be monitored.")
    if value(report, "prerequisites", "elevated"):
        add("warning", "Security", "This session is elevated (Administrator). HomeMiner will refuse to launch miners. "
            "Start it from a normal session.")

    cpu_name = value(report, "cpu", "name") or ""
    if value(report, "cpu", "intel_13th_14th_gen_desktop"):
        micro = value(report, "cpu", "microcode_revision") or "unknown"
        add("warning", "CPU",
            f"{cpu_name} is a 13th/14th Gen Intel desktop CPU. Intel links 'Vmin shift' instability to elevated "
            f"voltage and temperature over long periods, and recommends the latest BIOS (microcode 0x12F) with "
            f"Intel Default Settings. Loaded microcode: {micro}. Check MSI's BIOS notes before long runs. "
            "HomeMiner never changes BIOS, voltage or power limits.",
            "Intel community notice (secondary)")
    avail = _float(value(report, "memory", "available_gb"))
    if avail is not None and avail < 3:
        add("warning", "Memory", f"Only {avail} GB RAM available. RandomX fast mode needs about 2.3 GB free.")
    if value(report, "prerequisites", "lock_pages_in_memory") is False:
        add("info", "CPU", "'Lock pages in memory' is not granted, so huge pages are unavailable and RandomX "
            "runs slower. HomeMiner never grants it; the security guide explains how you can.")

    gpus = report.get("gpus", [])
    if not gpus:
        add("warning", "GPU", "No NVIDIA GPU detected. GPU benchmarks are unavailable.")
    if value(report, "nvidia", "nvidia_smi") is False and gpus:
        add("warning", "GPU", "nvidia-smi not found: GPU temperature, power and utilisation cannot be read.")
    for gpu in gpus:
        name = value(gpu, "name") or "GPU"
        cc = _cc_tuple(value(gpu, "compute_capability"))
        if cc and cc[0] == 5:
            add("warning", "Driver",
                f"{name} is Maxwell. NVIDIA's R580 driver branch is the last to support it, and the CUDA 13 "
                "toolkit no longer targets it. Use miner builds made with CUDA 12.x or older.",
                "NVIDIA via Phoronix (primary)")
        if cc and cc < (6, 0):
            add("warning", "NiceHash",
                f"{name} is not supported by NiceHash QuickMiner or Excavator (oldest listed: GTX 1000 series).",
                "NiceHash QuickMiner README (primary)")
        total = _float(value(gpu, "vram_mb"))
        if total is not None and total <= 4096 + 64:
            add("warning", "VRAM",
                f"{name} has {total:.0f} MB VRAM. DAG-based algorithms (KawPow, Etchash, Octopus) no longer fit.")
        if "GTX 970" in str(name).upper():
            add("info", "VRAM", "GTX 970: only the first 3.5 GB run at full speed; HomeMiner budgets for 3.5 GB.")
        if value(gpu, "power_draw_w") is None and value(gpu, "index") is not None:
            add("info", "Power", f"{name} does not report power draw; GPU power will be estimated, and labelled so.")

    if value(report, "prerequisites", "defender_realtime"):
        add("info", "Security", "Windows Defender real-time protection is on and is likely to quarantine miners. "
            "HomeMiner will not disable it or add exclusions. See the security guide.")
    if value(report, "prerequisites", "gpg") is False:
        add("info", "Verification", "gpg not found: miner downloads are checked against SHA256SUMS, but the GPG "
            "signature cannot be checked. Gpg4win adds that check.")
    miners = (report.get("prerequisites", {}) or {}).get("miners") or {}
    for miner, status in miners.items():
        if not status.get("verified"):
            add("info", "Miners", f"{miner} is not installed or not verified. Installing it needs your approval.")
    add("info", "Sensors", "Windows has no built-in CPU temperature or power reading. HomeMiner reads them from "
        "LibreHardwareMonitor if you run it; otherwise CPU power is estimated and labelled so.")
    return out
