"""Repository hygiene: no wallet identifiers, no persistence, no security weakening."""

import re
from pathlib import Path

from homeminer import wallet

ROOT = Path(__file__).parents[1]
TEXT_SUFFIXES = {".py", ".ps1", ".md", ".toml", ".js", ".css", ".html", ".json", ".txt", ".csv", ".log"}
CANDIDATE = re.compile(r"\b(?:(?:bc|tb|bcrt)1[02-9ac-hj-np-z]{6,87}|[13mn][1-9A-HJ-NP-Za-km-z]{24,34})\b", re.I)


def _files(*folders):
    for folder in folders:
        for path in (ROOT / folder).rglob("*"):
            if path.is_file() and path.suffix in TEXT_SUFFIXES and "__pycache__" not in path.parts:
                yield path


def test_no_btc_addresses_outside_test_vectors():
    offenders = []
    for path in [*_files("src", "docs", "scripts", "config"), ROOT / "README.md", ROOT / "pyproject.toml"]:
        for match in CANDIDATE.finditer(path.read_text(encoding="utf-8", errors="replace")):
            if wallet.validate_btc_address(match.group(0), expected_network=None).valid:
                offenders.append(f"{path.relative_to(ROOT)}: {match.group(0)}")
    assert offenders == []


def test_no_persistence_or_defender_changes_in_code():
    forbidden = [
        "schtasks", "register-scheduledtask", "new-scheduledtask", "new-service", "sc.exe create",
        "currentversion\\run", "currentversion\\\\run", "set-mppreference", "add-mppreference",
        "disablerealtimemonitoring", "shell:startup", "--power-limit", "nvidia-smi -pl", "-lgc", "--lock-gpu-clocks",
        "bcdedit", "netsh advfirewall",
    ]
    hits = []
    for path in _files("src", "scripts"):
        text = path.read_text(encoding="utf-8", errors="replace").lower()
        for needle in forbidden:
            if needle in text:
                hits.append(f"{path.relative_to(ROOT)}: {needle}")
    assert hits == []


def test_gitignore_keeps_private_data_out():
    ignore = (ROOT / ".gitignore").read_text()
    for entry in ("config/homeminer.toml", "tools/", "data/", "results/", "logs/", ".env"):
        assert entry in ignore


def test_fixtures_are_labelled_synthetic():
    for path in (ROOT / "tests" / "fixtures").glob("*"):
        if path.name == "fake_xmrig.py":
            assert "does no hashing" in path.read_text()
            continue
        assert path.name.startswith("synthetic_"), path.name
        if path.suffix == ".json":
            assert "Synthetic" in path.read_text()
