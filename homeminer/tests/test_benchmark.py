import os
import shutil
import sys
import threading
import time
from pathlib import Path

import pytest

from homeminer.benchmark import power
from homeminer.benchmark.lock import BusyError, IsolationLock
from homeminer.benchmark.results import ResultStore
from homeminer.benchmark.runner import TOKEN_PLACEHOLDER, BenchmarkRunner
from homeminer.benchmark.telemetry import Sample, parse_lhm, summarise
from homeminer.miners.registry import Verification
from homeminer.process import ProcessSupervisor

from .conftest import FIXTURES

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="fake miner uses a POSIX shebang")


class FakeTelemetry:
    def __init__(self, temps=None):
        self.temps = list(temps or [])
        self.n = 0

    def sample(self):
        self.n += 1
        temp = self.temps.pop(0) if self.temps else 55.0
        return Sample(t=time.time(), cpu_util_pct=50.0, gpu_util_pct=99.0, gpu_temp_c=temp, gpu_power_w=120.0)


def _fake_exe(tmp_path: Path) -> Path:
    exe = tmp_path / "tools" / "xmrig-6.26.0" / "xmrig"
    exe.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(FIXTURES / "fake_xmrig.py", exe)
    exe.chmod(0o755)
    return exe


def _runner(cfg, tmp_path, verified=True, telemetry=None, discovery=None):
    exe = _fake_exe(tmp_path)

    def verifier(config, release):
        if not verified:
            return Verification(False, "Not installed. Installing needs your approval.")
        return Verification(True, "ok", executable=exe, manifest={"version": release.version})

    tel = telemetry or FakeTelemetry()
    return BenchmarkRunner(cfg, ProcessSupervisor(cfg.tools_dir), discovery=discovery,
                           telemetry_factory=lambda device, idx: tel, verifier=verifier,
                           elevated=lambda: False, sleep=lambda s: time.sleep(min(s, 0.05)), baseline_samples=1)


def test_dry_run_launches_nothing(cfg, tmp_path, windows_report):
    runner = _runner(cfg, tmp_path, discovery=windows_report)
    plan = runner.plan("cpu")
    result = runner.run(plan, dry_run=True)
    assert result.status == "dry-run"
    assert "Dry run: nothing was launched." in result.notes
    assert not Path(plan.run_dir).exists()
    assert runner.supervisor.running_labels() == []
    assert plan.limits["msr_mod"] == "disabled"
    assert plan.limits["threads"]["threads_at_hint"] == 8
    assert any(TOKEN_PLACEHOLDER in a for a in result.command)
    assert not any(plan._token in a for a in result.command)


def test_unverified_miner_blocks(cfg, tmp_path):
    runner = _runner(cfg, tmp_path, verified=False)
    result = runner.run(runner.plan("cpu"))
    assert result.status == "blocked"
    assert any("approval" in e for e in result.errors)


def test_elevated_session_blocks(cfg, tmp_path):
    runner = _runner(cfg, tmp_path)
    runner.elevated = lambda: True
    result = runner.run(runner.plan("cpu"))
    assert result.status == "blocked" and any("Administrator" in e for e in result.errors)


def test_incompatible_gpu_blocks(cfg, tmp_path, windows_report):
    report = dict(windows_report)
    report["compatibility"] = [dict(c) for c in windows_report["compatibility"]]
    for item in report["compatibility"]:
        if item["key"] == "gpu-rx0":
            item["status"], item["reasons"] = "incompatible", ["Needs more VRAM."]
    runner = _runner(cfg, tmp_path, discovery=report)
    result = runner.run(runner.plan("gpu"))
    assert result.status == "blocked" and "Needs more VRAM." in result.errors


def test_completed_cpu_benchmark(cfg, tmp_path, windows_report, monkeypatch):
    monkeypatch.setenv("FAKE_XMRIG_MODE", "ok")
    runner = _runner(cfg, tmp_path, discovery=windows_report)
    result = runner.run(runner.plan("cpu"))
    assert result.status == "completed", result.errors
    assert result.result_kind == "local_benchmark"
    assert result.hashrate_hs == 2000000.0
    assert result.verification == "match"  # read from the coloured console output
    assert result.power["kind"] == "estimated"
    assert result.telemetry["samples"] >= 1
    assert ResultStore(cfg.data_dir).latest_usable("cpu", "rx/0").id == result.id


def test_mismatch_is_not_usable(cfg, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_XMRIG_MODE", "mismatch")
    runner = _runner(cfg, tmp_path)
    result = runner.run(runner.plan("cpu"))
    assert result.verification == "mismatch"
    assert not result.usable
    assert any("Do not mine" in n for n in result.notes)


def test_gpu_out_of_memory_reports_compatibility_error(cfg, tmp_path, windows_report, monkeypatch):
    monkeypatch.setenv("FAKE_XMRIG_MODE", "oom")
    runner = _runner(cfg, tmp_path, discovery=windows_report)
    result = runner.run(runner.plan("gpu"))
    assert result.status == "failed"
    assert "The GPU ran out of memory." in result.compatibility_errors
    assert result.power["kind"] == "measured-sensor"


def test_thermal_limit_aborts(cfg, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_XMRIG_MODE", "hang")
    runner = _runner(cfg, tmp_path, telemetry=FakeTelemetry(temps=[50, 60, 85]))
    started = time.time()
    result = runner.run(runner.plan("gpu"))
    assert time.time() - started < 20
    assert result.status == "aborted" and "GPU reached 85" in result.abort_reason
    assert runner.supervisor.running_labels() == []


def test_user_stop_and_emergency_stop(cfg, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_XMRIG_MODE", "hang")
    runner = _runner(cfg, tmp_path)
    stop = threading.Event()
    timer = threading.Timer(0.5, stop.set)
    timer.start()
    result = runner.run(runner.plan("cpu"), stop_event=stop)
    assert result.status == "aborted" and result.abort_reason == "Stopped by you."

    holder = {}
    thread = threading.Thread(target=lambda: holder.setdefault("r", runner.run(runner.plan("cpu"))))
    thread.start()
    deadline = time.time() + 10
    while not runner.supervisor.running_labels() and time.time() < deadline:
        time.sleep(0.05)
    report = runner.supervisor.emergency_stop()
    thread.join(timeout=20)
    assert report["killed"]
    assert holder["r"].status == "aborted" and holder["r"].abort_reason == "Emergency stop."


def test_benchmarks_never_overlap(cfg, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_XMRIG_MODE", "hang")
    runner = _runner(cfg, tmp_path)
    stop = threading.Event()
    thread = threading.Thread(target=lambda: runner.run(runner.plan("cpu"), stop_event=stop))
    thread.start()
    deadline = time.time() + 10
    while not runner.supervisor.running_labels() and time.time() < deadline:
        time.sleep(0.05)
    try:
        second = runner.run(runner.plan("gpu"))
        assert second.status == "blocked"
        assert any("measured separately" in e for e in second.errors)
    finally:
        stop.set()
        thread.join(timeout=20)


def test_lock_detects_stale_holders(tmp_path):
    lock = IsolationLock(tmp_path)
    lock.dir.mkdir(parents=True)
    (lock.dir / "bench-cpu.lock").write_text('{"label": "bench-cpu", "pid": 999999999}')
    lock.acquire("bench-gpu")  # stale lock from a dead process is ignored
    with pytest.raises(BusyError):
        lock.acquire("bench-cpu")
    lock.release("bench-gpu")
    lock.acquire("bench-cpu")


def test_power_labels():
    samples = [Sample(t=0, cpu_util_pct=80.0, gpu_power_w=130.0), Sample(t=1, cpu_util_pct=60.0, gpu_power_w=110.0)]
    baseline = [Sample(t=0, gpu_power_w=15.0)]
    cpu = power.cpu_power(samples, baseline, {"cpu_idle_watts": 10, "cpu_full_load_watts": 65})
    assert cpu.kind == "estimated" and cpu.watts == pytest.approx(55 * 0.7, abs=0.1)
    gpu = power.gpu_power(samples, baseline, {})
    assert gpu.kind == "measured-sensor" and gpu.watts == 105.0 and gpu.baseline_w == 15.0
    wall = power.cpu_power(samples, baseline, {"wall_meter_cpu_watts": 71})
    assert wall.kind == "measured-wall" and wall.watts == 71
    lhm = [Sample(t=0, cpu_power_w=60.0)]
    assert power.cpu_power(lhm, [Sample(t=0, cpu_power_w=8.0)], {}).kind == "measured-sensor"


def test_telemetry_summary_and_lhm_parse():
    s = summarise([Sample(t=0, gpu_temp_c=60, cpu_util_pct=40), Sample(t=1, gpu_temp_c=71, cpu_util_pct=60)])
    assert s.gpu_temp_peak == 71 and s.cpu_util_avg == 50 and s.sources["cpu_temp"] == "unavailable"
    text = ('[{"Name":"CPU Package","SensorType":"Temperature","Value":67.5},'
            '{"Name":"CPU Package","SensorType":"Power","Value":58.2},'
            '{"Name":"Core Max","SensorType":"Temperature","Value":71.0}]')
    assert parse_lhm(text) == (67.5, 58.2)
    assert parse_lhm("not json") == (None, None)


def test_pool_verified_preferred(cfg):
    store = ResultStore(cfg.data_dir)
    store.record_pool_verified("cpu", "rx/0", 5000.0, "NiceHash rig page, 24 h average")
    assert store.latest_usable("cpu", "rx/0").result_kind == "pool_verified"
    with pytest.raises(ValueError):
        store.record_pool_verified("cpu", "rx/0", 0, "")
