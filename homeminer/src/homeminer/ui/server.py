"""Local web interface.

Security model:
  * Listens on 127.0.0.1 only, on a random port by default.
  * Every /api call needs a per-launch random token, delivered in the URL
    fragment (never sent to the server or written to history by the browser)
    and then sent in the X-HomeMiner-Token header. Other web pages cannot set
    that header on a cross-site request without a CORS preflight, which this
    server never approves.
  * The Host and Origin headers must name this server, which blocks DNS
    rebinding.
  * No wallet address is logged; events are redacted before display.
"""

from __future__ import annotations

import json
import logging
import math
import mimetypes
import secrets
import threading
import time
import webbrowser
from collections import deque
from dataclasses import asdict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from .. import __version__, discovery as discovery_mod, ledger as ledger_mod, routes
from ..benchmark import sha256_reference
from ..benchmark.results import ResultStore
from ..benchmark.runner import BenchmarkRunner
from ..benchmark.telemetry import SystemTelemetry
from ..config import Config, ConfigError
from ..discovery import value
from ..market import snapshot as snapshot_mod
from ..miners.registry import tools_status
from ..mining import CONFIRM_PHRASE, MiningSession
from ..process import ProcessSupervisor
from ..redact import redact
from ..wallet import PayoutStore, mask, validate_btc_address

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"
MAX_BODY = 64 * 1024

SETTING_KEYS = {
    "economics.electricity_eur_per_kwh": float,
    "economics.wear_cost_eur_per_day": float,
    "economics.withdrawal_fee_target_pct": float,
    "power.wall_meter_cpu_watts": float,
    "power.wall_meter_gpu_watts": float,
    "fees.nicehash.mining_fee_pct": float,
    "fees.nicehash.withdrawal_fee_btc_onchain": float,
    "fees.nicehash.withdrawal_min_btc_onchain": float,
    "fees.nicehash.payout_min_btc": float,
    "fees.nicehash.verification": str,
    "fees.nicehash.verified_on": str,
}
SETTING_BOUNDS = {
    "economics.electricity_eur_per_kwh": (0, 5),
    "economics.wear_cost_eur_per_day": (0, 100),
    "economics.withdrawal_fee_target_pct": (0.1, 100),
    "power.wall_meter_cpu_watts": (0, 1000),
    "power.wall_meter_gpu_watts": (0, 1000),
    "fees.nicehash.mining_fee_pct": (0, 50),
    "fees.nicehash.withdrawal_fee_btc_onchain": (0, 0.01),
    "fees.nicehash.withdrawal_min_btc_onchain": (0, 0.1),
    "fees.nicehash.payout_min_btc": (0, 0.1),
}


def json_safe(obj: Any) -> Any:
    """Replace infinities and NaN (e.g. "never reaches the payout threshold") with None for JSON."""
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, deque)):
        return [json_safe(v) for v in obj]
    return obj


class App:
    def __init__(self, config: Config, discovery_fn: Callable[..., dict] | None = None,
                 market_refresh: Callable[[Config], Any] = snapshot_mod.refresh,
                 runner: BenchmarkRunner | None = None, session: MiningSession | None = None,
                 monitor: bool = True, telemetry_factory: Callable[[], Any] | None = None):
        self.config = config
        self.token = secrets.token_urlsafe(32)
        self.supervisor = runner.supervisor if runner else ProcessSupervisor(config.tools_dir)
        self.discovery_fn = discovery_fn or (lambda: discovery_mod.collect(tools_status=tools_status(config)))
        self.market_refresh = market_refresh
        self.discovery: dict | None = self._load_json("discovery.json")
        self.runner = runner or BenchmarkRunner(config, self.supervisor, discovery=self.discovery)
        self.session = session or MiningSession(config, self.supervisor)
        self.snapshot = snapshot_mod.load_cached(config)
        self.store = ResultStore(config.data_dir)
        self.ledger = ledger_mod.Ledger(config.data_dir)
        self.payouts = PayoutStore(config.data_dir)
        self.events: deque[dict] = deque(maxlen=200)
        self.history: deque[dict] = deque(maxlen=300)
        self.sha_ref: dict | None = None
        self.job: dict[str, Any] | None = None
        self.last_dry_run: dict[str, Any] = {}
        self._lock = threading.RLock()
        self._closing = threading.Event()
        self._telemetry_factory = telemetry_factory
        if monitor:
            threading.Thread(target=self._monitor, name="monitor", daemon=True).start()
        self.event("info", f"HomeMiner {__version__} started. Nothing runs until you start it.")

    # -- helpers -----------------------------------------------------------------

    def _load_json(self, name: str) -> dict | None:
        try:
            return json.loads((self.config.data_dir / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def event(self, level: str, message: str) -> None:
        self.events.appendleft({"t": time.time(), "level": level, "message": redact(message)})

    def _monitor(self) -> None:
        telemetry = None
        while not self._closing.is_set():
            try:
                if telemetry is None:
                    has_gpu = bool((self.discovery or {}).get("gpus"))
                    telemetry = (self._telemetry_factory() if self._telemetry_factory
                                 else SystemTelemetry(gpu_index=0 if has_gpu else None))
                sample = telemetry.sample()
                entry = sample.to_dict()
                api = self.session.state.api if self.session.state.running else {}
                entry["hashrate"] = api.get("hashrate_10s")
                self.history.append(entry)
            except Exception as exc:  # noqa: BLE001 - monitoring must never crash the UI
                log.debug("monitor sample failed: %s", exc)
            self._closing.wait(2.0)

    def close(self) -> None:
        self._closing.set()
        self.emergency_stop(log_event=False)

    # -- actions -----------------------------------------------------------------

    def run_discovery(self) -> dict:
        report = self.discovery_fn()
        self.discovery = report
        self.runner.discovery = report
        path = self.config.data_dir / "discovery.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        self.event("info", "Discovery finished (read-only).")
        return report

    def busy(self) -> bool:
        return bool(self.job and self.job["thread"].is_alive()) or self.session.state.running

    def start_benchmark(self, device: str, dry_run: bool, size: str | None = None) -> dict:
        with self._lock:
            plan = self.runner.plan(device, size=size)
            if dry_run:
                result = self.runner.run(plan, dry_run=True)
                self.last_dry_run[device] = result.to_dict()
                self.event("info", f"{device.upper()} dry run: {'ready' if not plan.blockers else 'blocked'}.")
                return {"ok": True, "dry_run": result.to_dict()}
            if self.busy() and not self.config.get("benchmark", "allow_simultaneous"):
                return {"ok": False, "error": "Something is already running. CPU and GPU benchmarks run one at a time."}
            if plan.blockers:
                return {"ok": False, "error": " ".join(plan.blockers), "plan": plan.to_dict()}
            stop_event = threading.Event()

            def work() -> None:
                result = self.runner.run(plan, stop_event=stop_event)
                hs = f"{result.hashrate_hs:,.1f} H/s" if result.hashrate_hs else "no hash rate"
                level = "info" if result.status == "completed" else "warning"
                self.event(level, f"{device.upper()} benchmark {result.status}: {hs}. "
                                  f"{result.abort_reason or ' '.join(result.errors[:2])}")

            thread = threading.Thread(target=work, name=f"bench-{device}", daemon=True)
            self.job = {"kind": "benchmark", "device": device, "thread": thread, "stop": stop_event,
                        "started": time.time(), "plan": plan.to_dict()}
            thread.start()
            self.event("info", f"{device.upper()} benchmark started ({plan.algorithm}, {plan.size} hashes).")
            return {"ok": True}

    def stop(self) -> dict:
        if self.job and self.job["thread"].is_alive():
            self.job["stop"].set()
            self.event("info", "Stopping the benchmark.")
        if self.session.state.running:
            self.session.stop()
            self.event("info", "Mining stopped.")
        return {"ok": True}

    def emergency_stop(self, log_event: bool = True) -> dict:
        if self.job:
            self.job["stop"].set()
        if self.session.state.running:
            self.session.stop("Emergency stop.")
        report = self.supervisor.emergency_stop()
        if log_event:
            self.event("error", f"EMERGENCY STOP: killed {len(report['killed'])} tracked and "
                                f"{len(report['swept'])} other miner process(es).")
        return {"ok": True, **report}

    def pause(self) -> dict:
        if self.job and self.job["thread"].is_alive():
            return {"ok": False, "error": "Benchmarks cannot be paused: a pause would corrupt the measurement. "
                                          "Stop it and run it again."}
        if not self.session.state.running:
            return {"ok": False, "error": "Nothing is mining."}
        return {"ok": self.session.pause()}

    def resume(self) -> dict:
        if not self.session.state.running:
            return {"ok": False, "error": "Nothing is mining."}
        return {"ok": self.session.resume()}

    def refresh_market(self) -> dict:
        snap = self.market_refresh(self.config)
        self.snapshot = snap
        snapshot_mod.save(self.config, snap)
        self.event("info" if not snap.errors else "warning",
                   "Market data refreshed." + (f" {len(snap.errors)} problem(s)." if snap.errors else ""))
        return {"ok": True}

    def set_setting(self, key: str, raw: Any) -> dict:
        if key not in SETTING_KEYS:
            return {"ok": False, "error": f"{key} cannot be changed here."}
        try:
            val = SETTING_KEYS[key](raw)
        except (TypeError, ValueError):
            return {"ok": False, "error": "Not a valid number."}
        if key in SETTING_BOUNDS:
            lo, hi = SETTING_BOUNDS[key]
            if not lo <= val <= hi:
                return {"ok": False, "error": f"Must be between {lo} and {hi}."}
        if key == "fees.nicehash.verification" and val not in ("primary", "secondary", "unverified"):
            return {"ok": False, "error": "Use primary, secondary or unverified."}
        try:
            self.config.set_ui_value(tuple(key.split(".")), val)
        except ConfigError as exc:
            return {"ok": False, "error": str(exc)}
        self.event("info", f"Set {key} = {val}.")
        return {"ok": True}

    def start_mining(self, address: str, confirmation: str, remember: bool) -> dict:
        algos = set(self.snapshot.nicehash) if self.snapshot else None
        address = address or self.payouts.load("nicehash") or ""
        blockers = self.session.start(address, confirmation, algos)
        if blockers:
            return {"ok": False, "error": " ".join(blockers), "blockers": blockers}
        if remember:
            self.payouts.save("nicehash", address)
        self.event("warning", f"Mining started to NiceHash address {mask(address)}.")
        return {"ok": True}

    # -- state -------------------------------------------------------------------

    def state(self) -> dict:
        d = self.discovery or {}
        gpus = d.get("gpus") or []
        g0 = gpus[0] if gpus else {}
        results = self.store.all()

        def latest(device: str) -> dict | None:
            mine = [r for r in results if r.device == device]
            return sorted(mine, key=lambda r: r.finished_at or "")[-1].to_dict() if mine else None

        job = None
        if self.job:
            job = {k: v for k, v in self.job.items() if k not in ("thread", "stop")}
            job["running"] = self.job["thread"].is_alive()
        snap = self.snapshot
        market = None
        if snap:
            market = {
                "created_at": snap.created_at, "age_minutes": snap.age_minutes(), "from_cache": snap.from_cache,
                "errors": snap.errors,
                "quotes": {k: snap.nicehash[k].to_dict() for k in ("RANDOMXMONERO", "SHA256") if k in snap.nicehash},
                "listed": sorted(snap.nicehash),
                "network": snap.network.to_dict() if snap.network else None,
                "price": snap.price.to_dict() if snap.price else None,
            }
        econ = self.config.get("economics")
        fees = self.config.get("fees", "nicehash")
        power = self.config.get("power")
        saved = self.payouts.load("nicehash")
        return {
            "version": __version__,
            "kill_on_close": self.supervisor.kill_on_close,
            "hardware": {
                "discovered": bool(d), "generated_at": d.get("generated_at"),
                "os": value(d, "os", "name"), "os_build": value(d, "os", "build"),
                "cpu": value(d, "cpu", "name"), "cores": value(d, "cpu", "physical_cores"),
                "threads": value(d, "cpu", "logical_processors"), "l3_mb": value(d, "cpu", "l3_cache_mb"),
                "microcode": value(d, "cpu", "microcode_revision"),
                "ram_total_gb": value(d, "memory", "total_gb"), "ram_available_gb": value(d, "memory", "available_gb"),
                "gpu": value(g0, "name"), "vram_mb": value(g0, "vram_mb"),
                "gpu_driver": value(g0, "driver_version"), "compute_capability": value(g0, "compute_capability"),
                "cuda": value(d, "nvidia", "driver_cuda_version"),
                "limitations": d.get("limitations", []), "compatibility": d.get("compatibility", []),
            },
            "job": job,
            "running": self.supervisor.running_labels(),
            "benchmarks": {"cpu": latest("cpu"), "gpu": latest("gpu")},
            "dry_runs": self.last_dry_run,
            "telemetry": list(self.history)[-120:],
            "settings": {
                "economics.electricity_eur_per_kwh": econ["electricity_eur_per_kwh"],
                "economics.wear_cost_eur_per_day": econ["wear_cost_eur_per_day"],
                "economics.withdrawal_fee_target_pct": econ["withdrawal_fee_target_pct"],
                "power.wall_meter_cpu_watts": power["wall_meter_cpu_watts"],
                "power.wall_meter_gpu_watts": power["wall_meter_gpu_watts"],
                "fees.nicehash.mining_fee_pct": fees["mining_fee_pct"],
                "fees.nicehash.withdrawal_fee_btc_onchain": fees["withdrawal_fee_btc_onchain"],
                "fees.nicehash.withdrawal_min_btc_onchain": fees["withdrawal_min_btc_onchain"],
                "fees.nicehash.payout_min_btc": fees["payout_min_btc"],
                "fees.nicehash.verification": fees["verification"],
                "fees.nicehash.verified_on": fees["verified_on"],
                "fees_source": fees["source"],
            },
            "market": market,
            "routes": routes.compare(self.config, snap, self.store, self.sha_ref),
            "ledger": self.ledger.summary(),
            "mining": {
                **asdict(self.session.state),
                "approved": bool(self.config.get("mining", "approved")),
                "confirm_phrase": CONFIRM_PHRASE,
                "stratum": self.config.get("mining", "nicehash_stratum"),
                "saved_address": mask(saved) if saved else None,
            },
            "events": list(self.events)[:50],
        }


def make_handler(app: App, port_ref: dict) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "HomeMiner"
        sys_version = ""

        def log_message(self, fmt: str, *args: Any) -> None:  # keep the console quiet and redacted
            log.debug(redact(fmt % args))

        # -- plumbing --------------------------------------------------------------

        def _allowed_origin(self) -> bool:
            port = port_ref["port"]
            hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
            if self.headers.get("Host", "") not in hosts:
                return False
            origin = self.headers.get("Origin")
            return origin is None or origin in {f"http://{h}" for h in hosts}

        def _security_headers(self) -> None:
            self.send_header("Content-Security-Policy",
                             "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
                             "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cache-Control", "no-store")

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self._security_headers()
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, data: Any) -> None:
            body = json.dumps(json_safe(data), default=str, allow_nan=False).encode("utf-8")
            self._send(status, body, "application/json; charset=utf-8")

        def _authorised(self) -> bool:
            token = self.headers.get("X-HomeMiner-Token", "")
            return secrets.compare_digest(token, app.token)

        def _body(self) -> dict | None:
            if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
                return None
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                return None
            try:
                return json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                return None

        # -- verbs -------------------------------------------------------------------

        def do_OPTIONS(self) -> None:  # noqa: N802 - never approve cross-origin preflights
            self._send(HTTPStatus.FORBIDDEN, b"", "text/plain")

        def do_GET(self) -> None:  # noqa: N802
            if not self._allowed_origin():
                return self._send(HTTPStatus.FORBIDDEN, b"Forbidden", "text/plain")
            path = self.path.split("?", 1)[0]
            if path in ("/", "/index.html"):
                return self._static("index.html")
            if path.startswith("/static/"):
                return self._static(path[len("/static/"):])
            if path == "/api/state":
                if not self._authorised():
                    return self._json(HTTPStatus.UNAUTHORIZED, {"error": "Missing or wrong session token."})
                return self._json(HTTPStatus.OK, app.state())
            return self._send(HTTPStatus.NOT_FOUND, b"Not found", "text/plain")

        def _static(self, name: str) -> None:
            target = (STATIC / name).resolve()
            if not target.is_relative_to(STATIC.resolve()) or not target.is_file():
                return self._send(HTTPStatus.NOT_FOUND, b"Not found", "text/plain")
            ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype in ("application/javascript",):
                ctype += "; charset=utf-8"
            self._send(HTTPStatus.OK, target.read_bytes(), ctype)

        def do_POST(self) -> None:  # noqa: N802
            if not self._allowed_origin():
                return self._send(HTTPStatus.FORBIDDEN, b"Forbidden", "text/plain")
            if not self._authorised():
                return self._json(HTTPStatus.UNAUTHORIZED, {"error": "Missing or wrong session token."})
            body = self._body()
            if body is None:
                return self._json(HTTPStatus.BAD_REQUEST, {"error": "Send a JSON body."})
            path = self.path.split("?", 1)[0]
            try:
                result = self._dispatch(path, body)
            except ValueError as exc:
                result = {"ok": False, "error": str(exc)}
            except Exception as exc:  # noqa: BLE001
                log.exception("request failed")
                result = {"ok": False, "error": redact(f"{type(exc).__name__}: {exc}")}
            if result is None:
                return self._json(HTTPStatus.NOT_FOUND, {"error": "Unknown action."})
            self._json(HTTPStatus.OK, result)

        def _dispatch(self, path: str, body: dict) -> dict | None:
            if path == "/api/emergency-stop":
                return app.emergency_stop()
            if path == "/api/stop":
                return app.stop()
            if path == "/api/pause":
                return app.pause()
            if path == "/api/resume":
                return app.resume()
            if path == "/api/discover":
                app.run_discovery()
                return {"ok": True}
            if path == "/api/benchmark":
                device = body.get("device")
                if device not in ("cpu", "gpu"):
                    return {"ok": False, "error": "device must be cpu or gpu"}
                return app.start_benchmark(device, bool(body.get("dry_run", True)), body.get("size"))
            if path == "/api/market/refresh":
                return app.refresh_market()
            if path == "/api/settings":
                return app.set_setting(str(body.get("key")), body.get("value"))
            if path == "/api/ledger":
                app.ledger.add(str(body.get("kind")), body.get("amount_btc"), str(body.get("source") or ""),
                               str(body.get("note") or ""))
                return {"ok": True}
            if path == "/api/pool-hashrate":
                app.store.record_pool_verified(str(body.get("device")), "rx/0", float(body.get("hashrate_hs")),
                                               str(body.get("note") or ""))
                return {"ok": True}
            if path == "/api/address/validate":
                return {"ok": True, "check": validate_btc_address(str(body.get("address") or "")).to_dict()}
            if path == "/api/address/forget":
                app.payouts.forget("nicehash")
                return {"ok": True}
            if path == "/api/sha256-reference":
                app.sha_ref = sha256_reference.measure(3.0)
                return {"ok": True, "result": app.sha_ref}
            if path == "/api/mining/start":
                return app.start_mining(str(body.get("address") or ""), str(body.get("confirmation") or ""),
                                        bool(body.get("remember")))
            return None

    return Handler


def serve(config: Config, port: int = 0, open_browser: bool = True, app: App | None = None) -> None:
    app = app or App(config)
    port_ref = {"port": port}
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(app, port_ref))
    port_ref["port"] = httpd.server_address[1]
    url = f"http://127.0.0.1:{port_ref['port']}/#t={app.token}"
    print("HomeMiner is running locally. Open this address in your browser (it contains a one-time session token):")
    print(f"  {url}")
    print("Press Ctrl+C here to quit; any running miner is stopped.")
    if open_browser:
        webbrowser.open(url)
    try:
        httpd.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        app.close()
        httpd.server_close()


def start_in_thread(app: App, port: int = 0) -> tuple[ThreadingHTTPServer, int]:
    """Start the server for tests or embedding; returns (server, port)."""
    port_ref = {"port": port}
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(app, port_ref))
    port_ref["port"] = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True).start()
    return httpd, port_ref["port"]
