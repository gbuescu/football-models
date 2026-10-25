import hashlib
import io
import json
import zipfile
from pathlib import Path

import pytest

from homeminer.miners import install, registry, xmrig

from .conftest import fixture_text

GREEN, RED, CLEAR = "\x1b[1;32m", "\x1b[1;31m", "\x1b[0m"


def test_parses_log_file_line():
    out = xmrig.parse_bench_output(fixture_text("synthetic_xmrig_bench_cpu.log"))
    assert out.finished
    assert out.seconds == 130.512
    assert out.hashrate_hs == 7662.1
    assert out.hash_sum == "6C3C3A4F0E5D4B21"
    assert out.size == "1M" and out.algorithm == "rx/0"
    assert out.verification == "unknown"  # log files carry no colour
    assert out.errors == []


def _console_line(color: str) -> str:
    # Mirrors XMRig's BenchClient.cpp format string, colour codes included.
    return (f"[t]  bench    \x1b[1;37mbenchmark finished in {CLEAR}\x1b[1;36m5.345 seconds (187099.5 h/s){CLEAR}"
            f"\x1b[1;37m hash sum = {CLEAR}{color}0123456789ABCDEF{CLEAR}")


@pytest.mark.parametrize("color,expected", [(GREEN, "match"), (RED, "mismatch"), ("\x1b[1;30m", "no-reference")])
def test_reads_verification_colour(color, expected):
    out = xmrig.parse_bench_output(_console_line(color))
    assert out.finished and out.hashrate_hs == 187099.5
    assert out.verification == expected


def test_detects_gpu_out_of_memory():
    out = xmrig.parse_bench_output(fixture_text("synthetic_xmrig_bench_gpu_oom.log"))
    assert not out.finished
    assert "The GPU ran out of memory." in out.errors


def test_cpu_command_is_conservative_and_offline(tmp_path):
    args = xmrig.cpu_bench_command(Path("xmrig.exe"), "rx/0", "1M", 50, 0, True, tmp_path / "x.log", 4321, "tok")
    joined = " ".join(args)
    assert "--bench=1M" in joined and "--cpu-max-threads-hint=50" in joined and "--cpu-priority=0" in joined
    assert "--randomx-wrmsr=-1" in args and "--randomx-no-rdmsr" in args
    assert "--http-host=127.0.0.1" in args
    for forbidden in ("-o", "--url", "-u", "--user", "--coin"):
        assert forbidden not in args
    assert "--no-huge-pages" in xmrig.cpu_bench_command(Path("x"), "rx/0", "1M", 50, 0, False, tmp_path / "l", 1, "t")


def test_command_limits_are_clamped(tmp_path):
    args = xmrig.cpu_bench_command(Path("x"), "rx/0", "1M", 500, 9, True, tmp_path / "l", 1, "t")
    assert "--cpu-max-threads-hint=100" in args and "--cpu-priority=5" in args


def test_gpu_command_disables_cpu(tmp_path):
    args = xmrig.gpu_bench_command(Path("xmrig.exe"), Path("xmrig-cuda.dll"), "rx/0", "250K", 0, True,
                                   tmp_path / "x.log", 1, "t")
    assert "--no-cpu" in args and "--cuda" in args and "--cuda-devices=0" in args


def test_bench_rejects_unsupported_inputs():
    with pytest.raises(ValueError):
        xmrig.validate_bench("kawpow", "1M")
    with pytest.raises(ValueError):
        xmrig.validate_bench("rx/0", "3K")
    assert xmrig.size_to_hashes("250K") == 250_000 and xmrig.size_to_hashes("10M") == 10_000_000


def test_mining_command_validates_worker(tmp_path):
    with pytest.raises(ValueError):
        xmrig.nicehash_mining_command(Path("x"), "stratum+ssl://h:443", "addr", "bad name;rm", "rx/0", 50, 0, True,
                                      tmp_path / "l", 1, "t")
    args = xmrig.nicehash_mining_command(Path("x"), "stratum+ssl://h:443", "addr", "rig1", "rx/0", 50, 0, True,
                                         tmp_path / "l", 1, "t", donate_level=0)
    assert "--nicehash" in args and "--tls" in args and "--donate-level=1" in args


def test_api_summary_parse():
    data = {"hashrate": {"total": [100.0, 95.5, None]}, "results": {"shares_good": 3, "shares_total": 4},
            "connection": {"pool": "example:443"}, "uptime": 60, "algo": "rx/0", "paused": False}
    out = xmrig.parse_api_summary(data)
    assert out["hashrate_10s"] == 100.0 and out["hashrate_15m"] is None and out["shares_good"] == 3


# --- installer ---------------------------------------------------------------

def _release(tmp_path: Path, payload: dict[str, bytes], sums_override: str | None = None):
    """Build a fake release folder served over file:// for the installer."""
    rel_dir = tmp_path / "release"
    rel_dir.mkdir()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in payload.items():
            zf.writestr(name, data)
    archive = rel_dir / "xmrig-6.26.0-windows-x64.zip"
    archive.write_bytes(buf.getvalue())
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    (rel_dir / "SHA256SUMS").write_text(sums_override or f"{digest}  xmrig-6.26.0-windows-x64.zip\n")
    (rel_dir / "SHA256SUMS.sig").write_text("not a real signature")
    return rel_dir


def _cfg_with_base(cfg, rel_dir):
    cfg.data["miners"]["xmrig"]["download_base"] = rel_dir.as_uri()
    return cfg


def test_install_requires_approval(cfg):
    rel = registry.releases(cfg)["xmrig"]
    with pytest.raises(install.InstallError, match="approval"):
        install.install(cfg, rel, approved=False)


def test_install_refuses_non_https(cfg):
    rel = registry.releases(cfg)["xmrig"]
    rel = registry.MinerRelease(**{**rel.__dict__, "download_base": "http://example.invalid"})
    with pytest.raises(install.InstallError, match="Refusing"):
        install.install(cfg, rel, approved=True)


def test_install_verifies_and_writes_manifest(cfg, tmp_path):
    rel_dir = _release(tmp_path, {"xmrig-6.26.0/xmrig.exe": b"binary", "xmrig-6.26.0/README.md": b"r"})
    cfg = _cfg_with_base(cfg, rel_dir)
    rel = registry.releases(cfg)["xmrig"]
    report = install.install(cfg, rel, approved=True, allowed_schemes=("file",), gpg=False)
    assert report.sums_match and report.files == 2
    assert report.signature_verified is None and "not checked" in report.signature_note
    status = registry.verify_installed(cfg, rel)
    assert status.ok, status.summary
    assert status.executable.name == "xmrig.exe"
    manifest = json.loads((registry.install_dir(cfg, rel) / registry.MANIFEST_NAME).read_text())
    assert manifest["archive_sha256_matches_sums"] is True

    # Tampering after install is detected and blocks use.
    status.executable.write_bytes(b"tampered")
    assert not registry.verify_installed(cfg, rel).ok


def test_install_rejects_hash_mismatch(cfg, tmp_path):
    rel_dir = _release(tmp_path, {"xmrig.exe": b"x"}, sums_override="0" * 64 + "  xmrig-6.26.0-windows-x64.zip\n")
    cfg = _cfg_with_base(cfg, rel_dir)
    with pytest.raises(install.InstallError, match="mismatch"):
        install.install(cfg, registry.releases(cfg)["xmrig"], approved=True, allowed_schemes=("file",), gpg=False)
    assert not registry.install_dir(cfg, registry.releases(cfg)["xmrig"]).exists()


def test_install_rejects_zip_slip(cfg, tmp_path):
    rel_dir = _release(tmp_path, {"../../evil.exe": b"x"})
    cfg = _cfg_with_base(cfg, rel_dir)
    with pytest.raises(install.InstallError, match="escapes"):
        install.install(cfg, registry.releases(cfg)["xmrig"], approved=True, allowed_schemes=("file",), gpg=False)
    assert not (tmp_path / "evil.exe").exists()


def test_unmanaged_files_are_not_trusted(cfg):
    rel = registry.releases(cfg)["xmrig"]
    target = registry.install_dir(cfg, rel)
    target.mkdir(parents=True)
    (target / "xmrig.exe").write_bytes(b"from somewhere")
    status = registry.verify_installed(cfg, rel)
    assert not status.ok and "not trusted" in status.summary


def test_parse_sha256sums():
    text = f"{'a' * 64}  file-one.zip\n{'b' * 64} *file-two.zip\ngarbage line\n"
    assert install.parse_sha256sums(text) == {"file-one.zip": "a" * 64, "file-two.zip": "b" * 64}
