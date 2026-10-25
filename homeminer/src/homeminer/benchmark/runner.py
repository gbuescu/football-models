"""Benchmark planning and execution.

A benchmark is XMRig's offline ``--bench`` mode: a fixed number of RandomX
hashes, no pool, no wallet, no network. CPU and GPU benchmarks never run at
the same time unless ``allow_simultaneous`` is set, and then the result is
flagged as contaminated.

``plan()`` performs every safety check without starting anything, so
``run(plan, dry_run=True)`` shows exactly what would happen.
"""

from __future__ import annotations

import secrets
import socket
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from .. import compat
from ..config import Config
from ..discovery import is_elevated, value
from ..miners import xmrig
from ..miners.registry import Verification, releases, verify_installed
from ..process import ProcessSupervisor
from ..redact import redact
from . import power as power_mod
from .lock import BusyError, IsolationLock
from .results import BenchmarkResult, ResultStore, new_id, now_iso
from .telemetry import Sample, SystemTelemetry, TelemetrySource, summarise

TOKEN_PLACEHOLDER = "<session-token>"


@dataclass
class BenchmarkPlan:
    id: str
    device: str
    algorithm: str
    size: str
    gpu_index: int | None
    device_name: str | None
    miner: str
    miner_version: str
    executable: str | None
    command: list[str]
    run_dir: str
    log_file: str
    output_file: str
    timeout_s: float
    temp_limits_c: dict[str, float]
    limits: dict[str, Any]
    memory_requirement: str
    expected_hashes: int
    blockers: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    verification: dict[str, Any] = field(default_factory=dict)
    simultaneous: bool = False
    http_port: int = 0
    _token: str = field(default="", repr=False)

    @property
    def label(self) -> str:
        return f"bench-{self.device}"

    def public_command(self) -> list[str]:
        return [redact(a.replace(self._token, TOKEN_PLACEHOLDER)) if self._token else redact(a) for a in self.command]

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("_token", None)
        data["command"] = self.public_command()
        data["run_dir"] = redact(self.run_dir)
        data["log_file"] = redact(self.log_file)
        data["output_file"] = redact(self.output_file)
        data["executable"] = redact(self.executable) if self.executable else None
        return data


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def default_telemetry(device: str, gpu_index: int | None) -> TelemetrySource:
    return SystemTelemetry(gpu_index=gpu_index)


class BenchmarkRunner:
    def __init__(self, config: Config, supervisor: ProcessSupervisor | None = None,
                 discovery: dict | None = None,
                 telemetry_factory: Callable[[str, int | None], TelemetrySource] = default_telemetry,
                 verifier: Callable[[Config, Any], Verification] = verify_installed,
                 elevated: Callable[[], bool | None] = is_elevated,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep,
                 baseline_samples: int = 3):
        self.config = config
        self.supervisor = supervisor or ProcessSupervisor(config.tools_dir)
        self.discovery = discovery
        self.telemetry_factory = telemetry_factory
        self.verifier = verifier
        self.elevated = elevated
        self.clock = clock
        self.sleep = sleep
        self.baseline_samples = baseline_samples
        self.store = ResultStore(config.data_dir)
        self.lock = IsolationLock(config.data_dir, bool(config.get("benchmark", "allow_simultaneous")))
        self.current: BenchmarkPlan | None = None
        self.latest_sample: Sample | None = None

    # -- planning --------------------------------------------------------------

    def plan(self, device: str, algorithm: str | None = None, size: str | None = None,
             gpu_index: int = 0) -> BenchmarkPlan:
        if device not in ("cpu", "gpu"):
            raise ValueError("device must be 'cpu' or 'gpu'")
        cfg = self.config
        bench = cfg.get("benchmark")
        limits = cfg.get("limits")
        algorithm = algorithm or bench[f"{device}_algorithm"]
        size = size or bench[f"{device}_size"]
        rels = releases(cfg)
        xmrig_rel = rels["xmrig"]
        blockers: list[str] = []
        warnings: list[str] = []

        bench_valid = True
        try:
            xmrig.validate_bench(algorithm, size)
        except ValueError as exc:
            blockers.append(str(exc))
            bench_valid = False

        ver = self.verifier(cfg, xmrig_rel)
        verification = {"xmrig": ver.to_dict()}
        if not ver.ok:
            blockers.append(f"XMRig: {ver.summary}")
        cuda_path: Path | None = None
        if device == "gpu":
            cuda_ver = self.verifier(cfg, rels["xmrig-cuda"])
            verification["xmrig-cuda"] = cuda_ver.to_dict()
            if not cuda_ver.ok:
                blockers.append(f"xmrig-cuda: {cuda_ver.summary}")
            cuda_path = cuda_ver.executable

        if self.elevated() and not cfg.get("general", "allow_elevated"):
            blockers.append("This session is running as Administrator. Miners are never launched elevated, so XMRig "
                            "cannot change system security policy. Restart HomeMiner from a normal session.")

        device_name = None
        key = f"{device}-{'rx0' if algorithm == 'rx/0' else 'rx2' if algorithm == 'rx/2' else algorithm}"
        if self.discovery:
            if device == "cpu":
                device_name = value(self.discovery, "cpu", "name")
            else:
                gpus = self.discovery.get("gpus") or []
                if not gpus:
                    blockers.append("No NVIDIA GPU was found by discovery.")
                elif gpu_index >= len(gpus):
                    blockers.append(f"GPU index {gpu_index} does not exist.")
                else:
                    device_name = value(gpus[gpu_index], "name")
            for item in self.discovery.get("compatibility", []):
                if item["key"] == key:
                    if item["status"] == "incompatible":
                        blockers.extend(item["reasons"] or [item["summary"]])
                    elif item["status"] in ("unlikely", "unverified"):
                        warnings.extend(item["reasons"] or [item["summary"]])
                    build = (item.get("recommended") or {}).get("cuda_build")
                    if build and build not in rels["xmrig-cuda"].asset:
                        warnings.append(f"Your driver suits the xmrig-cuda {build} build, but "
                                        f"{rels['xmrig-cuda'].asset} is configured.")
            if device == "cpu" and value(self.discovery, "prerequisites", "lock_pages_in_memory") is False:
                warnings.append("Huge pages are unavailable ('Lock pages in memory' not granted): the result "
                                "will be lower than a tuned system's.")
        else:
            warnings.append("No discovery report loaded: hardware compatibility was not checked.")

        simultaneous = False
        if self.lock.others_running(f"bench-{device}"):
            if self.lock.allow_simultaneous:
                simultaneous = True
                warnings.append("Another benchmark or mining session is running. This result will be flagged as "
                                "contaminated.")
            else:
                blockers.append("Another benchmark or mining session is running. Stop it first: CPU and GPU are "
                                "measured separately so the results stay honest.")

        run_id = new_id(device)
        run_dir = cfg.data_dir / "benchmarks" / "runs" / run_id
        log_file = run_dir / "xmrig.log"
        output_file = run_dir / "console.txt"
        port = free_port()
        token = secrets.token_hex(16)
        exe = ver.executable or Path(cfg.tools_dir / xmrig_rel.install_dir_name / xmrig_rel.executable)
        huge = bool(limits.get("huge_pages", True))
        command: list[str] = []
        if bench_valid and device == "cpu":
            command = xmrig.cpu_bench_command(
                exe, algorithm, size, limits["cpu_max_threads_hint"], limits["cpu_priority"], huge,
                log_file, port, token)
        elif bench_valid:
            loader = cuda_path or Path("xmrig-cuda.dll")
            command = xmrig.gpu_bench_command(exe, loader, algorithm, size, gpu_index, huge, log_file, port, token)

        spec_key = "cpu-rx0" if device == "cpu" else "gpu-rx0"
        spec = compat.spec_for(spec_key)
        threads = {}
        if self.discovery and device == "cpu":
            logical = value(self.discovery, "cpu", "logical_processors")
            l3 = value(self.discovery, "cpu", "l3_cache_mb")
            threads = compat.recommended_cpu_threads(int(logical) if logical else None,
                                                     float(l3) if l3 else None, limits["cpu_max_threads_hint"])
        return BenchmarkPlan(
            id=run_id, device=device, algorithm=algorithm, size=size,
            gpu_index=gpu_index if device == "gpu" else None, device_name=device_name,
            miner="XMRig" + (" + xmrig-cuda" if device == "gpu" else ""), miner_version=xmrig_rel.version,
            executable=str(exe), command=command, run_dir=str(run_dir), log_file=str(log_file),
            output_file=str(output_file), timeout_s=float(limits["max_benchmark_minutes"]) * 60,
            temp_limits_c={"cpu": float(limits["cpu_temp_limit_c"]), "gpu": float(limits["gpu_temp_limit_c"])},
            limits={
                "cpu_max_threads_hint_pct": limits["cpu_max_threads_hint"] if device == "cpu" else None,
                "cpu_priority": limits["cpu_priority"] if device == "cpu" else None,
                "huge_pages": huge, "msr_mod": "disabled", "threads": threads,
            },
            memory_requirement=spec.memory_note,
            expected_hashes=xmrig.size_to_hashes(size) if bench_valid else 0,
            blockers=blockers, warnings=warnings, verification=verification, simultaneous=simultaneous,
            http_port=port, _token=token,
        )

    # -- execution -------------------------------------------------------------

    def run(self, plan: BenchmarkPlan, dry_run: bool = False, stop_event: threading.Event | None = None,
            on_sample: Callable[[Sample], None] | None = None) -> BenchmarkResult:
        result = BenchmarkResult(
            id=plan.id, device=plan.device, algorithm=plan.algorithm, status="dry-run",
            device_name=plan.device_name, miner=plan.miner, miner_version=plan.miner_version,
            limits=plan.limits, memory_requirement=plan.memory_requirement, command=plan.public_command(),
            simultaneous=plan.simultaneous, plan=plan.to_dict(), hashes=plan.expected_hashes,
        )
        if dry_run:
            result.notes.append("Dry run: nothing was launched.")
            result.notes.extend(plan.warnings)
            result.errors = list(plan.blockers)
            return result
        if plan.blockers:
            result.status = "blocked"
            result.errors = list(plan.blockers)
            return result

        try:
            self.lock.acquire(plan.label)
        except BusyError as exc:
            result.status = "blocked"
            result.errors = [str(exc)]
            return result

        stop_event = stop_event or threading.Event()
        samples: list[Sample] = []
        baseline: list[Sample] = []
        abort_reason: str | None = None
        start_wall = time.time()
        self.current = plan
        try:
            telemetry = self.telemetry_factory(plan.device, plan.gpu_index if plan.device == "gpu" else 0)
            for _ in range(self.baseline_samples):
                baseline.append(telemetry.sample())
                self.sleep(1.0)

            Path(plan.run_dir).mkdir(parents=True, exist_ok=True)
            result.started_at = now_iso()
            started = self.clock()
            exe_dir = Path(plan.command[0]).parent if plan.command else None
            proc = self.supervisor.start(plan.label, plan.command, cwd=exe_dir, output_path=Path(plan.output_file))
            interval = float(self.config.get("benchmark", "sample_interval_s") or 2.0)
            while proc.running():
                if stop_event.is_set():
                    abort_reason = "Stopped by you."
                    break
                sample = telemetry.sample()
                samples.append(sample)
                self.latest_sample = sample
                if on_sample:
                    on_sample(sample)
                hot = self._too_hot(sample, plan)
                if hot:
                    abort_reason = hot
                    break
                if self.clock() - started > plan.timeout_s:
                    abort_reason = f"Exceeded the {plan.timeout_s / 60:.0f}-minute limit."
                    break
                self.sleep(interval)
            if abort_reason:
                self.supervisor.stop(plan.label, reason=abort_reason)
            else:
                self.supervisor.stop(plan.label, reason="finished")
            if proc.stop_reason == "emergency stop":
                abort_reason = "Emergency stop."
            result.duration_s = round(self.clock() - started, 1)
            result.finished_at = now_iso()
            returncode = proc.popen.returncode
        finally:
            self.lock.release(plan.label)
            self.current = None

        text = ""
        for path in (Path(plan.log_file), Path(plan.output_file)):
            try:
                text += path.read_text(encoding="utf-8", errors="replace") + "\n"
            except OSError:
                continue
        parsed = xmrig.parse_bench_output(text)
        result.miner_seconds = parsed.seconds
        result.hash_sum = parsed.hash_sum
        result.verification = parsed.verification
        result.huge_pages = parsed.huge_pages
        result.errors = parsed.errors
        result.error_lines = [redact(line) for line in parsed.error_lines[:20]]
        result.compatibility_errors = [e for e in parsed.errors if any(
            k in e for k in ("memory", "CUDA", "architecture", "device", "plugin"))]
        summary = summarise(samples)
        result.telemetry = summary.to_dict()
        power_cfg = self.config.get("power")
        figure = (power_mod.cpu_power if plan.device == "cpu" else power_mod.gpu_power)(samples, baseline, power_cfg)
        result.power = figure.to_dict()

        if abort_reason:
            result.status = "aborted"
            result.abort_reason = abort_reason
        elif parsed.finished and parsed.hashrate_hs:
            result.status = "completed"
            result.hashrate_hs = parsed.hashrate_hs
        else:
            result.status = "failed"
            if not result.errors:
                result.errors.append(f"XMRig exited (code {returncode}) without finishing the benchmark. "
                                     "See the run log in the data folder.")
        if result.verification == "mismatch":
            result.notes.append("The hash sum did NOT match XMRig's reference: this hardware produced wrong "
                                "results. Do not mine. Check temperatures, BIOS defaults and memory stability.")
        elif result.verification == "unknown" and result.status == "completed":
            result.notes.append("XMRig's log file does not show whether the hash sum matched its reference; "
                                "check the console colour (green = match) if you need certainty.")
        if plan.simultaneous:
            result.notes.append("Another workload ran at the same time: this figure is contaminated.")
        result.notes.extend(plan.warnings)
        if start_wall and result.duration_s is None:
            result.duration_s = round(time.time() - start_wall, 1)
        self.store.save(result)
        return result

    @staticmethod
    def _too_hot(sample: Sample, plan: BenchmarkPlan) -> str | None:
        if sample.gpu_temp_c is not None and sample.gpu_temp_c >= plan.temp_limits_c["gpu"]:
            return f"GPU reached {sample.gpu_temp_c:.0f} °C (limit {plan.temp_limits_c['gpu']:.0f} °C)."
        if sample.cpu_temp_c is not None and sample.cpu_temp_c >= plan.temp_limits_c["cpu"]:
            return f"CPU reached {sample.cpu_temp_c:.0f} °C (limit {plan.temp_limits_c['cpu']:.0f} °C)."
        return None
