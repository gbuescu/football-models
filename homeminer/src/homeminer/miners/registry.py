"""Miner release definitions and integrity checks.

HomeMiner only launches a miner binary that was installed by
``homeminer install-miner`` (after your approval) and whose file hashes still
match the manifest written at install time. No manifest, or any mismatch, means
the miner is treated as untrusted and will not run.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..config import Config

MANIFEST_NAME = "homeminer-manifest.json"


@dataclass(frozen=True)
class MinerRelease:
    key: str
    name: str
    version: str
    asset: str
    release_url: str
    download_base: str
    executable: str  # file inside the archive HomeMiner launches or loads
    homepage: str
    signed_sums: bool = True

    @property
    def install_dir_name(self) -> str:
        return f"{self.key}-{self.version}"

    @property
    def asset_url(self) -> str:
        return f"{self.download_base}/{self.asset}"

    @property
    def sums_url(self) -> str:
        return f"{self.download_base}/SHA256SUMS"

    @property
    def sig_url(self) -> str:
        return f"{self.download_base}/SHA256SUMS.sig"


def releases(config: Config) -> dict[str, MinerRelease]:
    x = config.get("miners", "xmrig")
    c = config.get("miners", "xmrig_cuda")
    return {
        "xmrig": MinerRelease(
            key="xmrig", name="XMRig", version=x["version"], asset=x["asset"],
            release_url=x["release_url"], download_base=x["download_base"],
            executable="xmrig.exe", homepage="https://github.com/xmrig/xmrig",
        ),
        "xmrig-cuda": MinerRelease(
            key="xmrig-cuda", name="xmrig-cuda (CUDA plugin for XMRig)", version=c["version"], asset=c["asset"],
            release_url=c["release_url"], download_base=c["download_base"],
            executable="xmrig-cuda.dll", homepage="https://github.com/xmrig/xmrig-cuda",
        ),
    }


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def install_dir(config: Config, release: MinerRelease) -> Path:
    return config.tools_dir / release.install_dir_name


def read_manifest(directory: Path) -> dict[str, Any] | None:
    try:
        return json.loads((directory / MANIFEST_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


@dataclass
class Verification:
    ok: bool
    summary: str
    executable: Path | None = None
    manifest: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "verified": self.ok,
            "summary": self.summary,
            "signature_verified": (self.manifest or {}).get("signature_verified"),
            "version": (self.manifest or {}).get("version"),
        }


def find_file(directory: Path, filename: str) -> Path | None:
    matches = sorted(p for p in directory.rglob(filename) if p.is_file())
    return matches[0] if matches else None


def verify_installed(config: Config, release: MinerRelease) -> Verification:
    """Check the installed files against the manifest written at install time."""
    directory = install_dir(config, release)
    if not directory.exists():
        return Verification(False, f"Not installed (expected in {directory.name}). Installing needs your approval.")
    manifest = read_manifest(directory)
    if manifest is None:
        return Verification(False, "No HomeMiner manifest: files were not installed by HomeMiner, so they are not trusted.")
    if manifest.get("version") != release.version or manifest.get("asset") != release.asset:
        return Verification(False, "Installed version differs from the configured release.", manifest=manifest)
    if not manifest.get("archive_sha256_matches_sums"):
        return Verification(False, "Install did not match the official SHA256SUMS.", manifest=manifest)
    files: dict[str, str] = manifest.get("files", {})
    for relative, expected in files.items():
        path = directory / relative
        if not path.is_file():
            return Verification(False, f"Missing file {relative}. Reinstall.", manifest=manifest)
        if sha256_file(path) != expected:
            return Verification(False, f"{relative} has changed since installation. Reinstall before use.",
                                manifest=manifest)
    executable = find_file(directory, release.executable)
    if executable is None:
        return Verification(False, f"{release.executable} not found in the release.", manifest=manifest)
    signature = manifest.get("signature_verified")
    sig_text = "GPG signature verified" if signature else "GPG signature not checked"
    return Verification(True, f"{release.name} {release.version}: SHA256SUMS match, {sig_text}.",
                        executable=executable, manifest=manifest)


def tools_status(config: Config) -> dict[str, Any]:
    return {key: verify_installed(config, rel).to_dict() for key, rel in releases(config).items()}
