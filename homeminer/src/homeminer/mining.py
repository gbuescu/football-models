"""Real mining sessions. Off by default and gated several times over.

A session starts only when ALL of these hold:
  1. ``[mining] approved = true`` in your local config (you edit it yourself)
  2. you type the confirmation phrase in the UI for this session
  3. the payout route needs an address and a valid mainnet one was entered
  4. XMRig is installed by HomeMiner and its hashes still match
  5. HomeMiner is not running elevated
  6. no benchmark or other session is running
  7. fresh market data shows NiceHash currently lists the algorithm
  8. the stratum URL points at nicehash.com

HomeMiner never initiates withdrawals, trades or transactions.
"""

from __future__ import annotations

import json
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .benchmark.lock import BusyError, IsolationLock
from .benchmark.results import new_id
from .benchmark.runner import free_port
from .benchmark.telemetry import Sample, SystemTelemetry
from .compat import is_forbidden_target
from .config import Config
from .discovery import is_elevated
from .miners import xmrig
from .miners.registry import Verification, releases, verify_installed
from .process import ProcessSupervisor
from .wallet import validate_btc_address

CONFIRM_PHRASE = "START MINING"
LABEL = "mine-cpu"


@dataclass
class SessionState:
    running: bool = False
    paused: bool = False
    started_at: float | None = None
    stop_reason: str | None = None
    api: dict[str, Any] = field(default_factory=dict)
    run_dir: str | None = None


def _stratum_ok(url: str) -> bool:
    parsed = urllib.parse.urlparse(url)
    host = (parsed.hostname or "").lower()
    return parsed.scheme in ("stratum+ssl", "stratum+tcp") and (host == "nicehash.com" or host.endswith(".nicehash.com"))


class MiningSession:
    def __init__(self, config: Config, supervisor: ProcessSupervisor,
                 verifier: Callable[[Config, Any], Verification] = verify_installed,
                 elevated: Callable[[], bool | None] = is_elevated,
                 telemetry_factory: Callable[[], Any] | None = None):
        self.config = config
        self.supervisor = supervisor
        self.verifier = verifier
        self.elevated = elevated
        self.lock = IsolationLock(config.data_dir, bool(config.get("benchmark", "allow_simultaneous")))
        self.state = SessionState()
        self._port = 0
        self._token = ""
        self._monitor: threading.Thread | None = None
        self._stop = threading.Event()
        self.telemetry_factory = telemetry_factory or (lambda: SystemTelemetry(gpu_index=0))
        self.latest_sample: Sample | None = None

    def gates(self, address: str, confirmation: str, nicehash_algorithms: set[str] | None,
              coin: str | None = None) -> list[str]:
        cfg = self.config
        blockers: list[str] = []
        if not cfg.get("mining", "approved"):
            blockers.append("Mining is not approved. After reading docs/SECURITY.md, set [mining] approved = true "
                            "in config/homeminer.toml yourself.")
        if confirmation.strip() != CONFIRM_PHRASE:
            blockers.append(f"Type {CONFIRM_PHRASE} to confirm you want to start mining now.")
        if is_forbidden_target(coin):
            blockers.append("Ethereum mainnet is proof of stake and cannot be mined.")
        if cfg.get("mining", "route") != "nicehash":
            blockers.append("Only the NiceHash route is implemented for real mining.")
        check = validate_btc_address(address)
        if not check.valid:
            blockers.append(f"NiceHash mining address: {check.reason}")
        if not _stratum_ok(str(cfg.get("mining", "nicehash_stratum"))):
            blockers.append("The configured stratum URL is not a nicehash.com stratum address.")
        ver = self.verifier(cfg, releases(cfg)["xmrig"])
        if not ver.ok:
            blockers.append(f"XMRig: {ver.summary}")
        if self.elevated() and not cfg.get("general", "allow_elevated"):
            blockers.append("HomeMiner is running as Administrator. Restart it from a normal session.")
        if self.lock.others_running(LABEL) or self.state.running:
            blockers.append("A benchmark or mining session is already running.")
        if nicehash_algorithms is None:
            blockers.append("Refresh market data first, so HomeMiner can confirm NiceHash currently lists RandomX.")
        elif "RANDOMXMONERO" not in nicehash_algorithms:
            blockers.append("NiceHash does not currently list RANDOMXMONERO, so there is no buyer for this hash power.")
        return blockers

    def start(self, address: str, confirmation: str, nicehash_algorithms: set[str] | None) -> list[str]:
        blockers = self.gates(address, confirmation, nicehash_algorithms)
        if blockers:
            return blockers
        cfg = self.config
        ver = self.verifier(cfg, releases(cfg)["xmrig"])
        limits = cfg.get("limits")
        run_dir = cfg.data_dir / "mining" / new_id("cpu")
        run_dir.mkdir(parents=True, exist_ok=True)
        self._port, self._token = free_port(), secrets.token_hex(16)
        command = xmrig.nicehash_mining_command(
            ver.executable, cfg.get("mining", "nicehash_stratum"), address.strip(), cfg.get("mining", "worker_name"),
            "rx/0", limits["cpu_max_threads_hint"], limits["cpu_priority"], bool(limits["huge_pages"]),
            run_dir / "xmrig.log", self._port, self._token, int(cfg.get("miners", "xmrig", "donate_level_pct") or 1))
        try:
            self.lock.acquire(LABEL)
        except BusyError as exc:
            return [str(exc)]
        try:
            self.supervisor.start(LABEL, command, cwd=Path(ver.executable).parent, output_path=run_dir / "console.txt")
        except Exception:
            self.lock.release(LABEL)
            raise
        self.state = SessionState(running=True, started_at=time.time(), run_dir=str(run_dir))
        self._stop.clear()
        self._monitor = threading.Thread(target=self._watch, name="mining-monitor", daemon=True)
        self._monitor.start()
        return []

    def _watch(self) -> None:
        telemetry = self.telemetry_factory()
        limits = self.config.get("limits")
        while not self._stop.is_set():
            proc = self.supervisor.get(LABEL)
            if proc is None or not proc.running():
                self._finish(proc.stop_reason if proc and proc.stop_reason else "XMRig exited.")
                return
            sample = telemetry.sample()
            self.latest_sample = sample
            if sample.cpu_temp_c is not None and sample.cpu_temp_c >= limits["cpu_temp_limit_c"]:
                self.stop(f"CPU reached {sample.cpu_temp_c:.0f} °C.")
                return
            self.state.api = self.poll_api()
            self._stop.wait(2.0)

    def poll_api(self) -> dict[str, Any]:
        if not self._port:
            return {}
        request = urllib.request.Request(f"http://127.0.0.1:{self._port}/2/summary",
                                         headers={"Authorization": f"Bearer {self._token}"})
        try:
            with urllib.request.urlopen(request, timeout=2) as response:  # noqa: S310 - loopback only
                return xmrig.parse_api_summary(json.loads(response.read().decode("utf-8")))
        except (urllib.error.URLError, OSError, ValueError):
            return {}

    def pause(self) -> bool:
        ok = self.supervisor.pause(LABEL)
        self.state.paused = self.state.paused or ok
        return ok

    def resume(self) -> bool:
        ok = self.supervisor.resume(LABEL)
        if ok:
            self.state.paused = False
        return ok

    def stop(self, reason: str = "Stopped by you.") -> None:
        self._stop.set()
        self.supervisor.stop(LABEL, reason=reason)
        self._finish(reason)

    def _finish(self, reason: str) -> None:
        if self.state.running:
            self.state.running = False
            self.state.paused = False
            self.state.stop_reason = reason
        self.lock.release(LABEL)
