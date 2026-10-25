from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from homeminer import config as config_mod
from homeminer.discovery import CommandResult

FIXTURES = Path(__file__).parent / "fixtures"


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    """A Config whose data and tools folders live in a temporary directory."""
    monkeypatch.setenv("HOMEMINER_DATA_DIR", str(tmp_path / "data"))
    data = copy.deepcopy(config_mod.DEFAULTS)
    data["general"]["tools_dir"] = str(tmp_path / "tools")
    return config_mod.Config(data, [])


class FakeRunner:
    """Replays synthetic command output keyed on the command line."""

    def __init__(self, gpu: bool = True, compute_cap: bool = True, whoami_lock_pages: bool = False):
        self.gpu = gpu
        self.compute_cap = compute_cap
        self.whoami_lock_pages = whoami_lock_pages
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str], timeout: float = 20.0) -> CommandResult:
        self.calls.append(list(args))
        exe = Path(args[0]).name.lower()
        if exe.startswith("powershell"):
            return CommandResult(0, fixture_text("synthetic_windows_cim.json"))
        if exe.startswith("nvidia-smi"):
            if not self.gpu:
                return CommandResult(127, "", missing=True)
            joined = " ".join(args)
            if "compute_cap" in joined:
                if self.compute_cap:
                    return CommandResult(0, "5.2\n")
                return CommandResult(2, "Field \"compute_cap\" is not a valid field to query.\n")
            if "--query-gpu" in joined:
                return CommandResult(0, fixture_text("synthetic_nvsmi_query.csv"))
            return CommandResult(0, fixture_text("synthetic_nvsmi_header.txt"))
        if exe.startswith("whoami"):
            priv = '"SeLockMemoryPrivilege","Lock pages in memory","Disabled"\n' if self.whoami_lock_pages else ""
            return CommandResult(0, '"Privilege Name","Description","State"\n' + priv)
        if exe.startswith("powercfg"):
            return CommandResult(0, "Power Scheme GUID: 381b4222-f694-41f0-9685-ff5bb260df2e  (Balanced)\n")
        return CommandResult(127, "", missing=True)


def windows_registry(values: dict | None = None):
    base = {
        ("SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion", "CurrentBuild"): "26100",
        ("SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion", "UBR"): 4061,
        ("SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion", "EditionID"): "Professional",
        ("SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion", "DisplayVersion"): "24H2",
        ("SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion", "ProductName"): "Windows 10 Pro",
        ("HARDWARE\\DESCRIPTION\\System\\CentralProcessor\\0", "Update Revision"): bytes(
            [0, 0, 0, 0, 0x2F, 0x01, 0, 0]),
    }
    base.update(values or {})
    return lambda key, name: base.get((key, name))


@pytest.fixture
def fake_runner():
    return FakeRunner()


@pytest.fixture
def windows_report(monkeypatch):
    """A discovery report built entirely from synthetic Windows fixtures."""
    from homeminer import discovery

    monkeypatch.setattr(discovery, "find_nvidia_smi", lambda platform_name: "nvidia-smi.exe")
    monkeypatch.setattr(discovery, "find_gpg", lambda platform_name: None)
    monkeypatch.setattr(discovery, "is_elevated", lambda: False)
    monkeypatch.setattr(discovery, "_windows_feature", lambda feature: True)
    return discovery.collect(runner=FakeRunner(), registry=windows_registry(), platform_name="win32",
                             psutil_mod=None)


def load_json(name: str) -> dict:
    return json.loads(fixture_text(name))
