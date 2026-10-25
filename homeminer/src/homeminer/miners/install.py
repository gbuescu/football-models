"""Download and verify an official miner release. Runs only with explicit approval.

Steps:
  1. Download the release archive, SHA256SUMS and SHA256SUMS.sig over HTTPS
     from the project's official GitHub release.
  2. Refuse the archive unless its SHA-256 matches the entry in SHA256SUMS.
  3. If gpg is installed and the publisher's key is in your keyring, check the
     signature on SHA256SUMS. Otherwise say clearly that it was not checked.
  4. Extract safely (no paths outside the target folder) and write a manifest of
     every file's SHA-256, which is checked again before each launch.

Nothing is added to Windows Defender exclusions, the registry, startup items,
services or scheduled tasks.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .. import __version__
from ..config import Config
from ..discovery import find_gpg
from .registry import MANIFEST_NAME, MinerRelease, install_dir, sha256_file

USER_AGENT = f"HomeMiner/{__version__}"
MAX_DOWNLOAD_BYTES = 200 * 1024 * 1024


class InstallError(RuntimeError):
    pass


@dataclass
class InstallReport:
    release: str
    archive_sha256: str = ""
    sums_match: bool = False
    signature_verified: bool | None = None
    signature_note: str = ""
    installed_to: str = ""
    files: int = 0
    messages: list[str] = field(default_factory=list)


def parse_sha256sums(text: str) -> dict[str, str]:
    sums: dict[str, str] = {}
    for line in text.splitlines():
        parts = line.strip().split()
        if len(parts) >= 2 and len(parts[0]) == 64:
            name = parts[-1].lstrip("*")
            try:
                int(parts[0], 16)
            except ValueError:
                continue
            sums[name] = parts[0].lower()
    return sums


def download(url: str, dest: Path, allowed_schemes: tuple[str, ...] = ("https",), timeout: float = 60.0) -> Path:
    scheme = urllib.parse.urlparse(url).scheme
    if scheme not in allowed_schemes:
        raise InstallError(f"Refusing to download over '{scheme}': only {', '.join(allowed_schemes)} is allowed.")
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response, dest.open("wb") as out:  # noqa: S310
        total = 0
        while True:
            chunk = response.read(1024 * 256)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_DOWNLOAD_BYTES:
                raise InstallError("Download is larger than expected; refusing it.")
            out.write(chunk)
    return dest


def safe_extract(archive: Path, target: Path) -> list[Path]:
    target = target.resolve()
    extracted: list[Path] = []
    with zipfile.ZipFile(archive) as zf:
        for member in zf.infolist():
            destination = (target / member.filename).resolve()
            if not destination.is_relative_to(target):
                raise InstallError(f"Archive entry escapes the install folder: {member.filename}")
            if member.is_dir():
                destination.mkdir(parents=True, exist_ok=True)
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(member) as src, destination.open("wb") as dst:
                shutil.copyfileobj(src, dst)
            extracted.append(destination)
    return extracted


def check_signature(sums: Path, sig: Path, gpg: str | None) -> tuple[bool | None, str]:
    if not gpg:
        return None, "gpg is not installed, so the signature was not checked. SHA256SUMS still matched."
    result = subprocess.run([gpg, "--batch", "--verify", str(sig), str(sums)],  # noqa: S603
                            capture_output=True, text=True, timeout=60)
    if result.returncode == 0:
        return True, "GPG signature on SHA256SUMS is valid for a key in your keyring."
    if "No public key" in (result.stderr or "") or "public key not found" in (result.stderr or ""):
        return None, ("The publisher's GPG key is not in your keyring, so the signature was not checked. "
                      "Import it from the publisher's website and GitHub profile, compare fingerprints, then reinstall.")
    raise InstallError("GPG signature check FAILED. Do not use this download.")


def install(config: Config, release: MinerRelease, approved: bool,
            allowed_schemes: tuple[str, ...] = ("https",), gpg: str | None | bool = True) -> InstallReport:
    if not approved:
        raise InstallError("Installing third-party mining software needs your explicit approval (--approve).")
    if gpg is True:
        gpg = find_gpg(sys.platform)
    report = InstallReport(release=f"{release.name} {release.version}")
    with tempfile.TemporaryDirectory(prefix="homeminer-") as tmp:
        tmpdir = Path(tmp)
        archive = download(release.asset_url, tmpdir / release.asset, allowed_schemes)
        sums_path = download(release.sums_url, tmpdir / "SHA256SUMS", allowed_schemes)
        expected = parse_sha256sums(sums_path.read_text(encoding="utf-8", errors="replace")).get(release.asset)
        if not expected:
            raise InstallError(f"{release.asset} is not listed in the release's SHA256SUMS.")
        actual = sha256_file(archive)
        report.archive_sha256 = actual
        if actual != expected:
            raise InstallError(f"SHA-256 mismatch for {release.asset}: expected {expected}, got {actual}. Not installed.")
        report.sums_match = True
        report.messages.append(f"SHA-256 {actual} matches SHA256SUMS.")

        try:
            sig_path = download(release.sig_url, tmpdir / "SHA256SUMS.sig", allowed_schemes)
            report.signature_verified, report.signature_note = check_signature(sums_path, sig_path, gpg or None)
        except InstallError:
            raise
        except Exception as exc:  # noqa: BLE001 - a missing .sig is reported, not fatal
            report.signature_verified, report.signature_note = None, f"Signature file unavailable: {exc}"
        report.messages.append(report.signature_note)

        target = install_dir(config, release)
        if target.exists():
            shutil.rmtree(target)
        target.mkdir(parents=True)
        files = safe_extract(archive, target)
        manifest = {
            "name": release.name,
            "version": release.version,
            "asset": release.asset,
            "release_url": release.release_url,
            "archive_sha256": actual,
            "archive_sha256_matches_sums": True,
            "signature_verified": report.signature_verified,
            "installed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "files": {str(p.relative_to(target.resolve())).replace("\\", "/"): sha256_file(p) for p in files},
        }
        (target / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        report.installed_to = str(target)
        report.files = len(files)
    return report


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
