import json

from homeminer import compat, discovery
from homeminer.discovery import value

from .conftest import FakeRunner, windows_registry


def test_windows_report_reads_stated_hardware(windows_report):
    r = windows_report
    assert r["read_only"] is True
    assert value(r, "os", "name") == "Windows 11"
    assert value(r, "os", "edition") == "Professional"
    assert value(r, "cpu", "name").endswith("i5-14400F")
    assert value(r, "cpu", "physical_cores") == 10
    assert value(r, "cpu", "logical_processors") == 16
    assert value(r, "cpu", "l3_cache_mb") == 20.0
    assert value(r, "cpu", "microcode_revision") == "0x12F"
    assert value(r, "cpu", "intel_13th_14th_gen_desktop") is True
    assert value(r, "memory", "type") == "DDR4"
    assert r["board"]["product"]["value"].startswith("PRO B760M-P")


def test_gpu_facts_and_sources(windows_report):
    gpu = windows_report["gpus"][0]
    assert value(gpu, "name") == "NVIDIA GeForce GTX 970"
    assert value(gpu, "vram_mb") == 4096
    assert value(gpu, "driver_version") == "580.97"
    assert gpu["compute_capability"] == {"value": "5.2", "source": "nvidia-smi"}
    assert value(gpu, "power_draw_w") is None  # "[N/A]" in the fixture
    assert value(windows_report, "nvidia", "driver_cuda_version") == "13.0"


def test_compute_capability_falls_back_to_labelled_lookup(monkeypatch):
    monkeypatch.setattr(discovery, "find_nvidia_smi", lambda p: "nvidia-smi.exe")
    monkeypatch.setattr(discovery, "is_elevated", lambda: False)
    report = discovery.collect(runner=FakeRunner(compute_cap=False), registry=windows_registry(),
                               platform_name="win32", psutil_mod=None)
    cc = report["gpus"][0]["compute_capability"]
    assert cc["value"] == "5.2" and cc["source"] == "lookup"


def test_missing_nvidia_smi_falls_back_to_wmi(monkeypatch):
    monkeypatch.setattr(discovery, "find_nvidia_smi", lambda p: None)
    monkeypatch.setattr(discovery, "is_elevated", lambda: False)
    report = discovery.collect(runner=FakeRunner(gpu=False), registry=windows_registry(), platform_name="win32",
                               psutil_mod=None)
    gpu = report["gpus"][0]
    assert gpu["name"]["source"] == "windows-cim"
    assert "nvidia-smi is unavailable" in gpu["warning"]
    assert any("nvidia-smi not found" in item["message"] for item in report["limitations"])


def test_report_has_no_machine_identifiers(windows_report):
    blob = json.dumps(windows_report).lower()
    for forbidden in ("serial", "uuid", "macaddress", "hostname", "computername", "username"):
        assert forbidden not in blob


def test_discovery_only_runs_read_only_commands(monkeypatch):
    monkeypatch.setattr(discovery, "find_nvidia_smi", lambda p: "nvidia-smi.exe")
    runner = FakeRunner()
    discovery.collect(runner=runner, registry=windows_registry(), platform_name="win32", psutil_mod=None)
    allowed = {"powershell.exe", "nvidia-smi.exe", "whoami", "powercfg"}
    for call in runner.calls:
        assert call[0] in allowed, call
        joined = " ".join(call).lower()
        for verb in ("set-", "new-", "remove-", "add-mp", "install", "-setactive", "/setactive", "--persistence",
                     "-pm ", "--power-limit", "-pl ", "--applications-clocks", "-ac "):
            assert verb not in joined, call
    script = discovery.CIM_SCRIPT.lower()
    assert "serialnumber" not in script and "uuid" not in script


def test_limitations_cover_known_constraints(windows_report):
    text = " ".join(item["message"] for item in windows_report["limitations"])
    assert "R580" in text and "CUDA 13" in text
    assert "QuickMiner" in text
    assert "3.5 GB" in text
    assert "0x12F" in text and "Loaded microcode: 0x12F" in text
    assert "Lock pages in memory" in text
    assert "will not disable it" in text


def test_compatibility_for_gtx970(windows_report):
    by_key = {item["key"]: item for item in windows_report["compatibility"]}
    assert by_key["cpu-rx0"]["status"] == "compatible"
    assert by_key["cpu-rx0"]["recommended"]["cache_optimal_threads"] == 10
    assert by_key["cpu-rx0"]["recommended"]["threads_at_hint"] == 8
    assert by_key["gpu-rx0"]["status"] == "likely"
    assert by_key["gpu-rx0"]["recommended"]["cuda_build"] == "cuda12_9"
    assert by_key["gpu-kawpow"]["status"] == "incompatible"
    assert by_key["gpu-etchash"]["status"] == "incompatible"
    assert by_key["gpu-autolykos"]["status"] == "unlikely"
    assert by_key["gpu-zhash"]["status"] == "unverified"
    assert "approval" in " ".join(by_key["gpu-zhash"]["reasons"])
    assert by_key["gpu-sha256"]["status"] == "educational"


def test_text_report_renders(windows_report):
    text = discovery.to_text(windows_report)
    assert "read-only" in text and "GTX 970" in text and "[lookup]" in text


def test_helpers():
    assert discovery.parse_microcode(bytes([0, 0, 0, 0, 0x2B, 0x01, 0, 0])) == "0x12B"
    assert discovery.parse_microcode(None) is None
    assert discovery.parse_power_scheme("Power Scheme GUID: abc  (High performance)") == "High performance"
    assert discovery.parse_cuda_version("CUDA Version: 12.9 ") == "12.9"
    rows = discovery.parse_nvsmi_csv("A, [Not Supported], 5\n", ["name", "power.draw", "x"])
    assert rows == [{"name": "A", "power.draw": None, "x": "5"}]
    assert compat.pick_cuda_build("12.4") == "cuda11_8"
    assert compat.pick_cuda_build("10.0") is None
    assert compat.is_forbidden_target("ETH") and not compat.is_forbidden_target("XMR")
