"""Benchmark result records, stored as JSON in the data folder (never in the repo).

``result_kind`` separates what was measured where:
  local_benchmark  XMRig's offline benchmark on this PC (no pool involved)
  pool_verified    a hash rate you copied from the pool/marketplace dashboard
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass
class BenchmarkResult:
    id: str
    device: str
    algorithm: str
    status: str  # completed | failed | aborted | blocked | dry-run
    result_kind: str = "local_benchmark"
    device_name: str | None = None
    miner: str | None = None
    miner_version: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    duration_s: float | None = None
    miner_seconds: float | None = None
    hashrate_hs: float | None = None
    hashes: int | None = None
    verification: str = "unknown"
    hash_sum: str | None = None
    huge_pages: str | None = None
    limits: dict[str, Any] = field(default_factory=dict)
    telemetry: dict[str, Any] = field(default_factory=dict)
    power: dict[str, Any] = field(default_factory=dict)
    memory_requirement: str | None = None
    errors: list[str] = field(default_factory=list)
    compatibility_errors: list[str] = field(default_factory=list)
    error_lines: list[str] = field(default_factory=list)
    abort_reason: str | None = None
    command: list[str] = field(default_factory=list)
    simultaneous: bool = False
    notes: list[str] = field(default_factory=list)
    plan: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def usable(self) -> bool:
        """A result the profitability engine may use."""
        return (self.status == "completed" and self.hashrate_hs is not None and self.hashrate_hs > 0
                and self.verification != "mismatch")


def new_id(device: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{device}-{uuid.uuid4().hex[:6]}"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ResultStore:
    def __init__(self, data_dir: Path):
        self.dir = data_dir / "benchmarks"

    def save(self, result: BenchmarkResult) -> Path:
        self.dir.mkdir(parents=True, exist_ok=True)
        path = self.dir / f"{result.id}.json"
        path.write_text(json.dumps(result.to_dict(), indent=2), encoding="utf-8")
        return path

    def all(self) -> list[BenchmarkResult]:
        if not self.dir.exists():
            return []
        out = []
        for path in sorted(self.dir.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                out.append(BenchmarkResult(**data))
            except (ValueError, TypeError, OSError):
                continue
        return out

    def latest_usable(self, device: str, algorithm: str | None = None,
                      kinds: tuple[str, ...] = ("pool_verified", "local_benchmark")) -> BenchmarkResult | None:
        """Prefer pool-verified figures, then the newest local benchmark."""
        candidates = [r for r in self.all() if r.device == device and r.usable
                      and (algorithm is None or r.algorithm == algorithm)]
        for kind in kinds:
            matching = [r for r in candidates if r.result_kind == kind]
            if matching:
                return sorted(matching, key=lambda r: r.finished_at or "")[-1]
        return None

    def record_pool_verified(self, device: str, algorithm: str, hashrate_hs: float, note: str) -> BenchmarkResult:
        if hashrate_hs <= 0:
            raise ValueError("Hash rate must be positive.")
        result = BenchmarkResult(
            id=new_id(device), device=device, algorithm=algorithm, status="completed",
            result_kind="pool_verified", hashrate_hs=float(hashrate_hs), started_at=now_iso(),
            finished_at=now_iso(), verification="pool-reported",
            notes=[f"Entered by you from the pool/marketplace dashboard. {note}".strip()],
        )
        self.save(result)
        return result
