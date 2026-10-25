"""XMRig command lines and output parsing.

XMRig's offline benchmark (``--bench``) hashes a fixed amount of RandomX work
with no pool, no wallet and no network, then prints the time taken and a hash
sum that XMRig compares against a reference value. The format strings parsed
here come from XMRig's ``BenchClient.cpp``:

    benchmark finished in %.3f seconds (%.1f h/s) hash sum = %016llX

The hash sum is printed green when it matches XMRig's reference, red when it
does not, and in grey/black when no reference exists for that size.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

BENCH_SIZES = ["250K", "500K"] + [f"{n}M" for n in range(1, 11)]
BENCH_ALGORITHMS = {"rx/0", "rx/2", "rx/wow"}

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
# Matched against the line with colour codes removed.
FINISHED_RE = re.compile(
    r"benchmark finished in\s+(?P<seconds>[\d.]+)\s+seconds\s+\((?P<hs>[\d.]+)\s+h/s\)"
    r"(?:\s+hash sum\s*=\s*(?P<sum>[0-9A-Fa-f]{16}))?"
)
# Matched against the raw line, to read the colour XMRig gave the hash sum.
SUM_COLOR_RE = re.compile(r"hash sum = (?P<color>(?:\x1b\[[0-9;]*m)*)(?P<sum>[0-9A-Fa-f]{16})")
START_RE = re.compile(r"start benchmark\s+hashes\s+(?P<size>\d+[KM]?)\s+algo\s+(?P<algo>\S+)")
GREEN = "\x1b[1;32m"
RED = "\x1b[1;31m"

# Lines that mean the hardware or setup cannot run the benchmark.
ERROR_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"failed to allocate RandomX dataset", re.I), "Not enough free RAM for the RandomX dataset."),
    (re.compile(r"out of memory|cudaErrorMemoryAllocation|CUDA_ERROR_OUT_OF_MEMORY", re.I),
     "The GPU ran out of memory."),
    (re.compile(r"failed to load CUDA plugin|cuda plugin.*not found|xmrig-cuda.*(?:not found|error)", re.I),
     "XMRig could not load the CUDA plugin (xmrig-cuda.dll)."),
    (re.compile(r"no CUDA devices|CUDA disabled|cuda.*no devices", re.I), "XMRig found no usable CUDA device."),
    (re.compile(r"CUDA (?:error|Error)\b.*", re.I), "A CUDA error occurred."),
    (re.compile(r"unsupported (?:GPU|device|architecture)|no kernel image is available", re.I),
     "This GPU architecture is not supported by the plugin build."),
    (re.compile(r"invalid (?:value for|argument)|unknown option|unrecognized option", re.I),
     "XMRig rejected a command-line option."),
]
HUGE_PAGES_RE = re.compile(r"huge pages\s+(?P<state>permission granted|unavailable|\d+%\s*\d*/\d*)", re.I)


@dataclass
class BenchOutput:
    finished: bool = False
    seconds: float | None = None
    hashrate_hs: float | None = None
    hash_sum: str | None = None
    verification: str = "unknown"  # match | mismatch | no-reference | unknown
    size: str | None = None
    algorithm: str | None = None
    huge_pages: str | None = None
    errors: list[str] = field(default_factory=list)
    error_lines: list[str] = field(default_factory=list)


def parse_bench_output(text: str) -> BenchOutput:
    out = BenchOutput()
    for raw in text.splitlines():
        line = ANSI_RE.sub("", raw)
        start = START_RE.search(line)
        if start:
            out.size, out.algorithm = start.group("size"), start.group("algo")
        hp = HUGE_PAGES_RE.search(line)
        if hp and out.huge_pages is None:
            out.huge_pages = hp.group("state")
        finished = FINISHED_RE.search(line)
        if finished:
            out.finished = True
            out.seconds = float(finished.group("seconds"))
            out.hashrate_hs = float(finished.group("hs"))
            out.hash_sum = finished.group("sum")
            colored = SUM_COLOR_RE.search(raw)
            color = colored.group("color") if colored else ""
            if GREEN in color:
                out.verification = "match"
            elif RED in color:
                out.verification = "mismatch"
            elif color:
                out.verification = "no-reference"
            continue
        for pattern, message in ERROR_PATTERNS:
            if pattern.search(line):
                if message not in out.errors:
                    out.errors.append(message)
                out.error_lines.append(line.strip()[:300])
                break
    return out


def validate_bench(algorithm: str, size: str) -> None:
    if algorithm not in BENCH_ALGORITHMS:
        raise ValueError(f"XMRig's offline benchmark supports {sorted(BENCH_ALGORITHMS)}, not {algorithm!r}.")
    if size not in BENCH_SIZES:
        raise ValueError(f"Benchmark size must be one of {BENCH_SIZES}.")


def size_to_hashes(size: str) -> int:
    return int(size[:-1]) * (1_000 if size.endswith("K") else 1_000_000)


def cpu_bench_command(exe: Path, algorithm: str, size: str, threads_hint_pct: int, priority: int,
                      huge_pages: bool, log_file: Path, http_port: int, http_token: str) -> list[str]:
    validate_bench(algorithm, size)
    args = [
        str(exe),
        f"--bench={size}",
        f"--algo={algorithm}",
        f"--cpu-max-threads-hint={max(1, min(int(threads_hint_pct), 100))}",
        f"--cpu-priority={max(0, min(int(priority), 5))}",
        # Never write CPU model-specific registers (needs Administrator and a kernel driver).
        "--randomx-wrmsr=-1",
        "--randomx-no-rdmsr",
        f"--log-file={log_file}",
        "--http-host=127.0.0.1",
        f"--http-port={http_port}",
        f"--http-access-token={http_token}",
    ]
    if not huge_pages:
        args.append("--no-huge-pages")
    return args


def gpu_bench_command(exe: Path, cuda_loader: Path, algorithm: str, size: str, device_index: int,
                      huge_pages: bool, log_file: Path, http_port: int, http_token: str) -> list[str]:
    validate_bench(algorithm, size)
    args = [
        str(exe),
        f"--bench={size}",
        f"--algo={algorithm}",
        "--no-cpu",
        "--cuda",
        f"--cuda-loader={cuda_loader}",
        f"--cuda-devices={int(device_index)}",
        "--randomx-wrmsr=-1",
        "--randomx-no-rdmsr",
        f"--log-file={log_file}",
        "--http-host=127.0.0.1",
        f"--http-port={http_port}",
        f"--http-access-token={http_token}",
    ]
    if not huge_pages:
        args.append("--no-huge-pages")
    return args


def nicehash_mining_command(exe: Path, stratum_url: str, mining_address: str, worker: str, algorithm: str,
                            threads_hint_pct: int, priority: int, huge_pages: bool, log_file: Path,
                            http_port: int, http_token: str, donate_level: int = 1) -> list[str]:
    """Real mining command. Only used after explicit approval; see MiningSession."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", worker):
        raise ValueError("Worker name may only contain letters, digits, '-' and '_' (max 32).")
    args = [
        str(exe),
        "-o", stratum_url,
        "-u", f"{mining_address}.{worker}",
        "-p", "x",
        "--nicehash",
        f"--algo={algorithm}",
        f"--donate-level={max(1, int(donate_level))}",
        f"--cpu-max-threads-hint={max(1, min(int(threads_hint_pct), 100))}",
        f"--cpu-priority={max(0, min(int(priority), 5))}",
        "--randomx-wrmsr=-1",
        "--randomx-no-rdmsr",
        f"--log-file={log_file}",
        "--http-host=127.0.0.1",
        f"--http-port={http_port}",
        f"--http-access-token={http_token}",
    ]
    if stratum_url.startswith("stratum+ssl://"):
        args.append("--tls")
    if not huge_pages:
        args.append("--no-huge-pages")
    return args


def parse_api_summary(data: dict) -> dict:
    """Pick live figures out of XMRig's HTTP API ``/2/summary`` response."""
    hashrate = (data.get("hashrate") or {}).get("total") or []
    results = data.get("results") or {}
    connection = data.get("connection") or {}

    def pick(i: int):
        return hashrate[i] if len(hashrate) > i and hashrate[i] is not None else None

    return {
        "hashrate_10s": pick(0),
        "hashrate_60s": pick(1),
        "hashrate_15m": pick(2),
        "shares_good": results.get("shares_good"),
        "shares_total": results.get("shares_total"),
        "pool": connection.get("pool"),
        "uptime_s": data.get("uptime"),
        "algo": data.get("algo"),
        "paused": data.get("paused"),
    }
