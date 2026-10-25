import http.client
import json
import math

import pytest

from homeminer.benchmark.runner import BenchmarkRunner
from homeminer.benchmark.telemetry import Sample
from homeminer.miners.registry import Verification
from homeminer.mining import CONFIRM_PHRASE, MiningSession, _stratum_ok
from homeminer.process import ProcessSupervisor
from homeminer.ui.server import App, json_safe, start_in_thread

ADDR = "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4"


class StaticTelemetry:
    def sample(self):
        return Sample(t=0, cpu_util_pct=3.0, gpu_temp_c=40.0)


def _unverified(config, release):
    return Verification(False, "Not installed. Installing needs your approval.")


@pytest.fixture
def server(cfg, windows_report):
    supervisor = ProcessSupervisor(cfg.tools_dir)
    runner = BenchmarkRunner(cfg, supervisor, discovery=windows_report, verifier=_unverified, elevated=lambda: False)
    session = MiningSession(cfg, supervisor, verifier=_unverified, elevated=lambda: False)
    app = App(cfg, discovery_fn=lambda: windows_report, market_refresh=lambda c: None, runner=runner,
              session=session, monitor=False)
    app.run_discovery()
    httpd, port = start_in_thread(app)
    yield app, port
    httpd.shutdown()
    httpd.server_close()


def _request(port, method, path, body=None, token=None, host=None, origin=None, content_type="application/json"):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    headers = {"Host": host or f"127.0.0.1:{port}"}
    if token:
        headers["X-HomeMiner-Token"] = token
    if origin:
        headers["Origin"] = origin
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = content_type
    conn.request(method, path, body=data, headers=headers)
    res = conn.getresponse()
    raw = res.read()
    conn.close()
    try:
        return res.status, json.loads(raw), dict(res.getheaders())
    except ValueError:
        return res.status, raw, dict(res.getheaders())


def test_page_is_served_with_strict_headers(server):
    app, port = server
    status, body, headers = _request(port, "GET", "/")
    assert status == 200 and b"EMERGENCY STOP" in body
    assert "default-src 'self'" in headers["Content-Security-Policy"]
    assert headers["X-Frame-Options"] == "DENY"
    assert "Access-Control-Allow-Origin" not in headers


def test_api_requires_token(server):
    app, port = server
    assert _request(port, "GET", "/api/state")[0] == 401
    assert _request(port, "GET", "/api/state", token="wrong")[0] == 401
    assert _request(port, "POST", "/api/emergency-stop", body={})[0] == 401
    status, state, _ = _request(port, "GET", "/api/state", token=app.token)
    assert status == 200
    assert state["hardware"]["cpu"].endswith("i5-14400F")
    assert state["mining"]["approved"] is False


def test_dns_rebinding_and_cross_origin_are_refused(server):
    app, port = server
    assert _request(port, "GET", "/api/state", token=app.token, host=f"evil.example:{port}")[0] == 403
    assert _request(port, "POST", "/api/stop", body={}, token=app.token, origin="https://evil.example")[0] == 403
    assert _request(port, "OPTIONS", "/api/stop")[0] == 403
    assert _request(port, "POST", "/api/stop", body={}, token=app.token, content_type="text/plain")[0] == 400


def test_static_path_traversal_blocked(server):
    app, port = server
    assert _request(port, "GET", "/static/../server.py")[0] == 404


def test_emergency_stop_endpoint(server):
    app, port = server
    status, out, _ = _request(port, "POST", "/api/emergency-stop", body={}, token=app.token)
    assert status == 200 and out["ok"] is True and out["killed"] == []
    assert app.events[0]["level"] == "error"


def test_benchmark_dry_run_and_blocked_start(server):
    app, port = server
    _, out, _ = _request(port, "POST", "/api/benchmark", body={"device": "cpu", "dry_run": True}, token=app.token)
    assert out["ok"] and out["dry_run"]["status"] == "dry-run"
    assert any("approval" in e for e in out["dry_run"]["errors"])
    _, out, _ = _request(port, "POST", "/api/benchmark", body={"device": "cpu", "dry_run": False}, token=app.token)
    assert out["ok"] is False and "approval" in out["error"]
    _, out, _ = _request(port, "POST", "/api/pause", body={}, token=app.token)
    assert out["ok"] is False


def test_settings_are_validated(server):
    app, port = server
    _, out, _ = _request(port, "POST", "/api/settings", token=app.token,
                         body={"key": "economics.electricity_eur_per_kwh", "value": "0.31"})
    assert out["ok"] and app.config.get("economics", "electricity_eur_per_kwh") == 0.31
    _, out, _ = _request(port, "POST", "/api/settings", token=app.token,
                         body={"key": "economics.electricity_eur_per_kwh", "value": "-1"})
    assert out["ok"] is False
    _, out, _ = _request(port, "POST", "/api/settings", token=app.token, body={"key": "mining.approved", "value": 1})
    assert out["ok"] is False


def test_address_validation_endpoint_does_not_store(server):
    app, port = server
    _, out, _ = _request(port, "POST", "/api/address/validate", token=app.token, body={"address": ADDR})
    assert out["check"]["valid"]
    assert app.payouts.load("nicehash") is None
    assert ADDR not in json.dumps(list(app.events))


def test_mining_is_gated_through_the_api(server):
    app, port = server
    _, out, _ = _request(port, "POST", "/api/mining/start", token=app.token,
                         body={"address": ADDR, "confirmation": CONFIRM_PHRASE, "remember": True})
    assert out["ok"] is False
    text = " ".join(out["blockers"])
    assert "not approved" in text and "XMRig" in text and "Refresh market data" in text
    assert app.payouts.load("nicehash") is None


def test_mining_gates(cfg):
    session = MiningSession(cfg, ProcessSupervisor(cfg.tools_dir), verifier=lambda c, r: Verification(True, "ok"),
                            elevated=lambda: False, telemetry_factory=StaticTelemetry)
    blockers = session.gates(ADDR, "start", {"RANDOMXMONERO"})
    assert any("not approved" in b for b in blockers) and any(CONFIRM_PHRASE in b for b in blockers)
    cfg.data["mining"]["approved"] = True
    assert session.gates(ADDR, CONFIRM_PHRASE, {"RANDOMXMONERO"}) == []
    assert any("does not currently list" in b for b in session.gates(ADDR, CONFIRM_PHRASE, {"SHA256"}))
    assert any("mining address" in b for b in session.gates("bc1qbad", CONFIRM_PHRASE, {"RANDOMXMONERO"}))
    assert any("proof of stake" in b for b in session.gates(ADDR, CONFIRM_PHRASE, {"RANDOMXMONERO"}, coin="ETH"))
    cfg.data["mining"]["nicehash_stratum"] = "stratum+tcp://pool.example.com:3333"
    assert any("not a nicehash.com" in b for b in session.gates(ADDR, CONFIRM_PHRASE, {"RANDOMXMONERO"}))
    session.elevated = lambda: True
    assert any("Administrator" in b for b in session.gates(ADDR, CONFIRM_PHRASE, {"RANDOMXMONERO"}))


def test_stratum_check():
    assert _stratum_ok("stratum+ssl://randomxmonero.auto.nicehash.com:443")
    assert not _stratum_ok("stratum+ssl://nicehash.com.evil.example:443")
    assert not _stratum_ok("https://randomxmonero.auto.nicehash.com")


def test_json_safe():
    assert json_safe({"a": math.inf, "b": [math.nan, 1.5]}) == {"a": None, "b": [None, 1.5]}
