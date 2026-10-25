"""Read-only hardware and software discovery.

Nothing here installs software, changes drivers, writes to the registry or
changes any system setting. Every probe only reads. Each reported value carries
a ``source`` so a reader can tell measured facts from lookups:

  windows-cim     Get-CimInstance (WMI) via PowerShell
  registry        read-only Windows registry query
  nvidia-smi      NVIDIA driver tool
  psutil / os     Python runtime
  lookup          HomeMiner's reference table (not read from the machine)

Serial numbers, UUIDs, MAC addresses, the computer name and the user name are
never collected.
"""

from __future__ import annotations

import csv
import io
import json
import os
import platform
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from . import __version__, hardware_db
from .redact import redact_obj


@dataclass
class CommandResult:
    returncode: int
    stdout: str
    stderr: str = ""
    missing: bool = False


Runner = Callable[[list[str], float], CommandResult]
RegistryReader = Callable[[str, str], Any]

_CREATE_NO_WINDOW = 0x08000000


def default_runner(args: list[str], timeout: float = 20.0) -> CommandResult:
    kwargs: dict[str, Any] = {"capture_output": True, "text": True, "timeout": timeout}
    if sys.platform == "win32":
        kwargs["creationflags"] = _CREATE_NO_WINDOW
    try:
        proc = subprocess.run(args, **kwargs)  # noqa: S603 - fixed argument lists only
    except FileNotFoundError:
        return CommandResult(127, "", "not found", missing=True)
    except subprocess.TimeoutExpired:
        return CommandResult(124, "", "timed out")
    except OSError as exc:
        return CommandResult(126, "", str(exc))
    return CommandResult(proc.returncode, proc.stdout or "", proc.stderr or "")


def default_registry_reader(key_path: str, value_name: str) -> Any:
    """Read one value under HKEY_LOCAL_MACHINE, or None."""
    if sys.platform != "win32":
        return None
    import winreg  # noqa: PLC0415 - Windows only

    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path, 0, winreg.KEY_READ) as key:
            value, _ = winreg.QueryValueEx(key, value_name)
            return value
    except OSError:
        return None


def fact(value: Any, source: str, note: str = "") -> dict[str, Any]:
    out = {"value": value, "source": source}
    if note:
        out["note"] = note
    return out


# --- PowerShell / CIM --------------------------------------------------------

# Read-only queries. No serial numbers, UUIDs or names are selected.
CIM_SCRIPT = r"""
$ErrorActionPreference = 'SilentlyContinue'
$o = [ordered]@{}
$o.cpu = @(Get-CimInstance Win32_Processor | Select-Object Name, Manufacturer, NumberOfCores, NumberOfLogicalProcessors, L2CacheSize, L3CacheSize, MaxClockSpeed)
$o.os = Get-CimInstance Win32_OperatingSystem | Select-Object Caption, Version, BuildNumber, OSArchitecture, TotalVisibleMemorySize, FreePhysicalMemory
$o.board = Get-CimInstance Win32_BaseBoard | Select-Object Manufacturer, Product
$o.bios = Get-CimInstance Win32_BIOS | Select-Object SMBIOSBIOSVersion, @{n='ReleaseDate';e={ if ($_.ReleaseDate) { $_.ReleaseDate.ToString('yyyy-MM-dd') } }}
$o.dimms = @(Get-CimInstance Win32_PhysicalMemory | Select-Object Capacity, Speed, ConfiguredClockSpeed, SMBIOSMemoryType)
$o.video = @(Get-CimInstance Win32_VideoController | Select-Object Name, DriverVersion, AdapterRAM, VideoProcessor)
$o.psversion = $PSVersionTable.PSVersion.ToString()
try { $o.defender = Get-MpComputerStatus | Select-Object AMServiceEnabled, RealTimeProtectionEnabled } catch { $o.defender = $null }
$o | ConvertTo-Json -Depth 4 -Compress
"""


def _as_list(value: Any) -> list:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def run_cim(runner: Runner) -> dict[str, Any] | None:
    result = runner(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", CIM_SCRIPT], 60.0)
    if result.missing or result.returncode != 0 or not result.stdout.strip():
        return None
    try:
        return json.loads(result.stdout)
    except ValueError:
        return None


# --- OS ----------------------------------------------------------------------

_NT_KEY = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion"


def probe_os(platform_name: str, cim: dict | None, registry: RegistryReader) -> dict[str, Any]:
    out: dict[str, Any] = {"platform": fact(platform_name, "os")}
    if platform_name != "win32":
        out["name"] = fact(f"{platform.system()} {platform.release()}", "os",
                           "Not Windows: HomeMiner targets Windows 10/11; this run is for development only.")
        out["architecture"] = fact(platform.machine(), "os")
        return out

    build_raw = registry(_NT_KEY, "CurrentBuild") or (cim or {}).get("os", {}).get("BuildNumber")
    try:
        build = int(build_raw)
    except (TypeError, ValueError):
        build = None
    edition = registry(_NT_KEY, "EditionID")
    display = registry(_NT_KEY, "DisplayVersion")
    ubr = registry(_NT_KEY, "UBR")
    # The registry's ProductName still says "Windows 10" on Windows 11; the build decides.
    family = None
    if build is not None:
        family = "Windows 11" if build >= 22000 else "Windows 10" if build >= 10240 else "Windows (older)"
    out["name"] = fact(family, "registry", "Derived from the build number (22000+ is Windows 11).")
    out["edition"] = fact(edition, "registry")
    out["display_version"] = fact(display, "registry")
    out["build"] = fact(f"{build}.{ubr}" if build is not None and ubr is not None else build, "registry")
    caption = (cim or {}).get("os", {}).get("Caption")
    if caption:
        out["caption"] = fact(caption, "windows-cim")
    arch = (cim or {}).get("os", {}).get("OSArchitecture") or platform.machine()
    out["architecture"] = fact(arch, "windows-cim" if cim else "os")
    return out


# --- CPU ---------------------------------------------------------------------

_CPU_KEY = r"HARDWARE\DESCRIPTION\System\CentralProcessor\0"


def parse_microcode(raw: Any) -> str | None:
    """Windows stores the loaded microcode revision in the upper half of 'Update Revision'."""
    if isinstance(raw, (bytes, bytearray)) and len(raw) >= 8:
        value = int.from_bytes(raw[4:8], "little") or int.from_bytes(raw[0:4], "little")
        return f"0x{value:X}" if value else None
    if isinstance(raw, int) and raw:
        return f"0x{raw:X}"
    return None


def _linux_cpuinfo() -> dict[str, str]:
    try:
        text = Path("/proc/cpuinfo").read_text()
    except OSError:
        return {}
    info: dict[str, str] = {}
    for line in text.splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            info.setdefault(key.strip(), value.strip())
    return info


def _windows_feature(feature: int) -> bool | None:
    if sys.platform != "win32":
        return None
    try:
        import ctypes  # noqa: PLC0415

        return bool(ctypes.windll.kernel32.IsProcessorFeaturePresent(feature))
    except Exception:  # noqa: BLE001
        return None


PF_AVX2_INSTRUCTIONS_AVAILABLE = 40


def probe_cpu(platform_name: str, cim: dict | None, registry: RegistryReader,
              psutil_mod: Any = None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    cpus = _as_list((cim or {}).get("cpu"))
    first = cpus[0] if cpus else {}
    name = (first.get("Name") or "").strip() or (registry(_CPU_KEY, "ProcessorNameString") or "").strip()
    flags: set[str] = set()
    if not name and platform_name != "win32":
        info = _linux_cpuinfo()
        name = info.get("model name", "") or platform.processor()
        flags = set(info.get("flags", "").split())
    out["name"] = fact(name or None, "windows-cim" if first.get("Name") else "registry" if platform_name == "win32" else "os")
    out["sockets"] = fact(len(cpus) if cpus else None, "windows-cim")

    if first:
        out["physical_cores"] = fact(sum(int(c.get("NumberOfCores") or 0) for c in cpus), "windows-cim")
        out["logical_processors"] = fact(sum(int(c.get("NumberOfLogicalProcessors") or 0) for c in cpus), "windows-cim")
        if first.get("L3CacheSize"):
            out["l3_cache_mb"] = fact(round(int(first["L3CacheSize"]) / 1024, 1), "windows-cim")
        if first.get("L2CacheSize"):
            out["l2_cache_mb"] = fact(round(int(first["L2CacheSize"]) / 1024, 1), "windows-cim")
        if first.get("MaxClockSpeed"):
            out["base_clock_mhz"] = fact(int(first["MaxClockSpeed"]), "windows-cim",
                                         "Windows reports the base clock here, not the turbo clock.")
    else:
        physical = psutil_mod.cpu_count(logical=False) if psutil_mod else None
        out["physical_cores"] = fact(physical, "psutil")
        out["logical_processors"] = fact(os.cpu_count(), "os")

    spec = hardware_db.lookup_cpu(name)
    if spec:
        out["core_layout"] = fact(f"{spec.performance_cores} P-cores + {spec.efficiency_cores} E-cores", "lookup")
        out["base_power_w"] = fact(spec.base_power_w, "lookup")
        out["max_turbo_power_w"] = fact(spec.max_turbo_power_w, "lookup")
        if "l3_cache_mb" not in out:
            out["l3_cache_mb"] = fact(spec.l3_mb, "lookup")

    if platform_name == "win32":
        avx2 = _windows_feature(PF_AVX2_INSTRUCTIONS_AVAILABLE)
        out["avx2"] = fact(avx2, "windows-api")
        out["microcode_revision"] = fact(parse_microcode(registry(_CPU_KEY, "Update Revision")), "registry")
    elif flags:
        out["avx2"] = fact("avx2" in flags, "os")
        out["aes"] = fact("aes" in flags, "os")
        out["sha_ni"] = fact("sha_ni" in flags, "os")

    out["intel_13th_14th_gen_desktop"] = fact(hardware_db.is_intel_13th_14th_gen_desktop(name), "lookup")
    return out


# --- Memory ------------------------------------------------------------------

_MEMORY_TYPES = {20: "DDR", 21: "DDR2", 24: "DDR3", 26: "DDR4", 34: "DDR5", 35: "LPDDR5"}


def probe_memory(cim: dict | None, psutil_mod: Any = None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if psutil_mod is not None:
        vm = psutil_mod.virtual_memory()
        out["total_gb"] = fact(round(vm.total / 1024**3, 1), "psutil")
        out["available_gb"] = fact(round(vm.available / 1024**3, 1), "psutil")
    elif cim and cim.get("os"):
        os_info = cim["os"]
        out["total_gb"] = fact(round(int(os_info.get("TotalVisibleMemorySize") or 0) / 1024**2, 1), "windows-cim")
        out["available_gb"] = fact(round(int(os_info.get("FreePhysicalMemory") or 0) / 1024**2, 1), "windows-cim")
    else:
        try:
            meminfo = Path("/proc/meminfo").read_text()
            total = int(re.search(r"MemTotal:\s+(\d+)", meminfo).group(1))
            avail = int(re.search(r"MemAvailable:\s+(\d+)", meminfo).group(1))
            out["total_gb"] = fact(round(total / 1024**2, 1), "os")
            out["available_gb"] = fact(round(avail / 1024**2, 1), "os")
        except (OSError, AttributeError):
            out["total_gb"] = fact(None, "unavailable")
            out["available_gb"] = fact(None, "unavailable")

    dimms = _as_list((cim or {}).get("dimms"))
    if dimms:
        types = {_MEMORY_TYPES.get(int(d.get("SMBIOSMemoryType") or 0), "unknown") for d in dimms}
        speeds = {d.get("ConfiguredClockSpeed") or d.get("Speed") for d in dimms}
        out["modules"] = fact(len(dimms), "windows-cim")
        out["type"] = fact("/".join(sorted(types)), "windows-cim")
        out["configured_speed_mts"] = fact(sorted(s for s in speeds if s), "windows-cim")
    return out


# --- NVIDIA ------------------------------------------------------------------

NVSMI_FIELDS = [
    "name", "memory.total", "memory.used", "driver_version", "temperature.gpu",
    "utilization.gpu", "power.draw", "power.limit", "pstate", "fan.speed",
]

_NA = {"", "n/a", "[n/a]", "[not supported]", "not supported", "[unknown error]"}


def find_nvidia_smi(platform_name: str) -> str | None:
    found = shutil.which("nvidia-smi")
    if found:
        return found
    if platform_name == "win32":
        candidates = [
            Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "nvidia-smi.exe",
            Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "NVIDIA Corporation" / "NVSMI" / "nvidia-smi.exe",
        ]
        for candidate in candidates:
            if candidate.exists():
                return str(candidate)
    return None


def _clean(value: str) -> str | None:
    value = (value or "").strip()
    return None if value.lower() in _NA else value


def _num(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def parse_nvsmi_csv(text: str, fields: list[str]) -> list[dict[str, str | None]]:
    rows = []
    for row in csv.reader(io.StringIO(text), skipinitialspace=True):
        if not row or len(row) != len(fields):
            continue
        rows.append({f: _clean(v) for f, v in zip(fields, row)})
    return rows


def parse_cuda_version(text: str) -> str | None:
    match = re.search(r"CUDA Version:\s*([\d.]+)", text or "")
    return match.group(1) if match else None


def query_nvidia(runner: Runner, exe: str, fields: list[str] | None = None) -> list[dict[str, str | None]] | None:
    fields = fields or NVSMI_FIELDS
    result = runner([exe, f"--query-gpu={','.join(fields)}", "--format=csv,noheader,nounits"], 20.0)
    if result.returncode != 0:
        return None
    return parse_nvsmi_csv(result.stdout, fields)


def probe_gpus(platform_name: str, runner: Runner, cim: dict | None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Return (gpus, nvidia_tool_info)."""
    exe = find_nvidia_smi(platform_name)
    tool: dict[str, Any] = {"nvidia_smi": fact(bool(exe), "os")}
    gpus: list[dict[str, Any]] = []
    rows = query_nvidia(runner, exe) if exe else None

    if exe and rows is None:
        tool["error"] = "nvidia-smi was found but the query failed. The driver may not be loaded."
    if exe:
        header = runner([exe], 20.0)
        tool["driver_cuda_version"] = fact(parse_cuda_version(header.stdout), "nvidia-smi",
                                           "Highest CUDA runtime this driver supports.")
        cc_rows = query_nvidia(runner, exe, ["compute_cap"])
    else:
        cc_rows = None

    for index, row in enumerate(rows or []):
        name = row.get("name") or ""
        spec = hardware_db.lookup_gpu(name)
        reported_cc = (cc_rows[index].get("compute_cap") if cc_rows and index < len(cc_rows) else None)
        gpu: dict[str, Any] = {
            "index": index,
            "name": fact(name, "nvidia-smi"),
            "vram_mb": fact(_num(row.get("memory.total")), "nvidia-smi"),
            "vram_used_mb": fact(_num(row.get("memory.used")), "nvidia-smi"),
            "driver_version": fact(row.get("driver_version"), "nvidia-smi"),
            "temperature_c": fact(_num(row.get("temperature.gpu")), "nvidia-smi"),
            "utilization_pct": fact(_num(row.get("utilization.gpu")), "nvidia-smi"),
            "power_draw_w": fact(_num(row.get("power.draw")), "nvidia-smi",
                                 "" if row.get("power.draw") else "This card does not report power draw."),
            "power_limit_w": fact(_num(row.get("power.limit")), "nvidia-smi"),
            "pstate": fact(row.get("pstate"), "nvidia-smi"),
        }
        if reported_cc:
            gpu["compute_capability"] = fact(reported_cc, "nvidia-smi")
        elif spec:
            gpu["compute_capability"] = fact(spec.compute_capability, "lookup",
                                             "Driver did not report compute capability.")
        else:
            gpu["compute_capability"] = fact(None, "unavailable")
        cc = gpu["compute_capability"]["value"]
        arch = spec.architecture if spec else hardware_db.architecture_for_cc(cc)
        gpu["architecture"] = fact(arch, "lookup")
        if spec and spec.board_power_w:
            gpu["reference_board_power_w"] = fact(spec.board_power_w, "lookup")
        if spec and spec.note:
            gpu["memory_note"] = fact(spec.note, "lookup")
        gpus.append(gpu)

    if not gpus:
        # Fall back to Windows' view so a missing driver is still visible.
        for video in _as_list((cim or {}).get("video")):
            name = video.get("Name") or ""
            if "nvidia" not in name.lower():
                continue
            spec = hardware_db.lookup_gpu(name)
            gpus.append({
                "index": None,
                "name": fact(name, "windows-cim"),
                "driver_version": fact(video.get("DriverVersion"), "windows-cim",
                                       "Windows driver version string, not NVIDIA's version number."),
                "vram_mb": fact(None, "unavailable", "WMI caps AdapterRAM at 4 GB, so it is not used."),
                "compute_capability": fact(spec.compute_capability if spec else None, "lookup"),
                "architecture": fact(spec.architecture if spec else None, "lookup"),
                "warning": "nvidia-smi is unavailable, so live temperature and power cannot be read.",
            })
    return gpus, tool


# --- Prerequisites -----------------------------------------------------------

def is_elevated() -> bool | None:
    try:
        if sys.platform == "win32":
            import ctypes  # noqa: PLC0415

            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        return os.geteuid() == 0
    except Exception:  # noqa: BLE001
        return None


def parse_whoami_priv(text: str) -> bool | None:
    if not text:
        return None
    return "SeLockMemoryPrivilege" in text


def parse_power_scheme(text: str) -> str | None:
    match = re.search(r"\(([^)]+)\)\s*$", (text or "").strip())
    return match.group(1) if match else None


def find_gpg(platform_name: str) -> str | None:
    found = shutil.which("gpg")
    if found:
        return found
    if platform_name == "win32":
        for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles")):
            if base and (Path(base) / "GnuPG" / "bin" / "gpg.exe").exists():
                return str(Path(base) / "GnuPG" / "bin" / "gpg.exe")
    return None


def probe_prerequisites(platform_name: str, runner: Runner, cim: dict | None,
                        psutil_mod: Any, tools_status: dict[str, Any] | None) -> dict[str, Any]:
    out: dict[str, Any] = {
        "python": fact(platform.python_version(), "os",
                       "OK" if sys.version_info >= (3, 11) else "HomeMiner needs Python 3.11 or newer."),
        "psutil": fact(psutil_mod is not None, "os", "" if psutil_mod else "Install with scripts\\Setup-HomeMiner.ps1"),
        "elevated": fact(is_elevated(), "os",
                         "HomeMiner refuses to launch miners from an Administrator session."),
        "gpg": fact(bool(find_gpg(platform_name)), "os",
                    "Optional: lets HomeMiner check the miner release's GPG signature (Gpg4win)."),
    }
    if platform_name == "win32":
        out["powershell"] = fact((cim or {}).get("psversion"), "windows-cim")
        priv = runner(["whoami", "/priv", "/fo", "csv"], 15.0)
        out["lock_pages_in_memory"] = fact(
            parse_whoami_priv(priv.stdout) if priv.returncode == 0 else None, "whoami",
            "Needed for huge pages. HomeMiner never grants it; see the security guide.")
        scheme = runner(["powercfg", "/getactivescheme"], 15.0)
        out["power_plan"] = fact(parse_power_scheme(scheme.stdout) if scheme.returncode == 0 else None, "powercfg")
        defender = (cim or {}).get("defender") or {}
        out["defender_realtime"] = fact(
            defender.get("RealTimeProtectionEnabled"), "windows-cim",
            "Expected to flag miners. HomeMiner never disables Defender or adds exclusions.")
    if tools_status is not None:
        out["miners"] = tools_status
    return out


# --- Top level ---------------------------------------------------------------

def collect(runner: Runner = default_runner, registry: RegistryReader = default_registry_reader,
            platform_name: str | None = None, psutil_mod: Any = "auto",
            tools_status: dict[str, Any] | None = None) -> dict[str, Any]:
    """Run every read-only probe and return a redacted report."""
    platform_name = platform_name or sys.platform
    if psutil_mod == "auto":
        try:
            import psutil as psutil_mod  # noqa: PLC0415
        except ImportError:
            psutil_mod = None

    cim = run_cim(runner) if platform_name == "win32" else None
    gpus, nvidia_tool = probe_gpus(platform_name, runner, cim)
    report: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "homeminer_version": __version__,
        "read_only": True,
        "os": probe_os(platform_name, cim, registry),
        "cpu": probe_cpu(platform_name, cim, registry, psutil_mod),
        "memory": probe_memory(cim, psutil_mod),
        "gpus": gpus,
        "nvidia": nvidia_tool,
        "prerequisites": probe_prerequisites(platform_name, runner, cim, psutil_mod, tools_status),
    }
    if cim:
        board = cim.get("board") or {}
        bios = cim.get("bios") or {}
        report["board"] = {
            "manufacturer": fact(board.get("Manufacturer"), "windows-cim"),
            "product": fact(board.get("Product"), "windows-cim"),
            "bios_version": fact(bios.get("SMBIOSBIOSVersion"), "windows-cim"),
            "bios_date": fact(bios.get("ReleaseDate"), "windows-cim"),
        }

    from . import compat  # noqa: PLC0415 - avoids an import cycle

    report["limitations"] = compat.limitations(report)
    report["compatibility"] = compat.assess(report)
    return redact_obj(report)


def value(report: dict, *path: Any) -> Any:
    """Fetch ``report[path...]['value']``, tolerating missing keys."""
    node: Any = report
    for key in path:
        if isinstance(node, list):
            if not isinstance(key, int) or key >= len(node):
                return None
            node = node[key]
        elif isinstance(node, dict):
            node = node.get(key)
        else:
            return None
    if isinstance(node, dict) and "value" in node:
        return node["value"]
    return node


def to_text(report: dict) -> str:
    lines = [f"HomeMiner discovery report ({report.get('generated_at')}) — read-only", ""]

    def section(title: str, data: dict) -> None:
        lines.append(f"[{title}]")
        for key, item in data.items():
            if isinstance(item, dict) and "value" in item:
                note = f"  ({item['note']})" if item.get("note") else ""
                lines.append(f"  {key:<28} {item['value']!s:<32} [{item['source']}]{note}")
        lines.append("")

    section("Operating system", report.get("os", {}))
    section("CPU", report.get("cpu", {}))
    section("Memory", report.get("memory", {}))
    if report.get("board"):
        section("Motherboard", report["board"])
    for gpu in report.get("gpus", []):
        section(f"GPU {gpu.get('index')}", gpu)
    if not report.get("gpus"):
        lines += ["[GPU]", "  No NVIDIA GPU detected.", ""]
    section("NVIDIA tools", report.get("nvidia", {}))
    prereq = {k: v for k, v in report.get("prerequisites", {}).items() if k != "miners"}
    section("Prerequisites", prereq)
    miners = report.get("prerequisites", {}).get("miners") or {}
    if miners:
        lines.append("[Miner software]")
        for name, status in miners.items():
            lines.append(f"  {name:<28} {status.get('summary')}")
        lines.append("")

    lines.append("[Limitations]")
    for item in report.get("limitations", []):
        lines.append(f"  - ({item['severity']}) {item['area']}: {item['message']}")
    lines.append("")
    lines.append("[Algorithm compatibility]")
    for item in report.get("compatibility", []):
        lines.append(f"  - {item['device']:<4} {item['algorithm']:<26} {item['status']:<13} {item['summary']}")
    return "\n".join(lines)
