"""Minimal HTTPS JSON/text fetching that records the source URL and time."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from .. import __version__

USER_AGENT = f"HomeMiner/{__version__} (+local benchmarking tool)"


@dataclass
class Fetched:
    url: str
    fetched_at: str
    ok: bool
    data: Any = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out.pop("data", None)
        return out


Fetcher = Callable[[str, float], Fetched]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def fetch(url: str, timeout: float = 15.0, as_json: bool = True) -> Fetched:
    if urllib.parse.urlparse(url).scheme != "https":
        return Fetched(url, _now(), False, error="Only HTTPS sources are allowed.")
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - https only
            body = response.read(2_000_000).decode("utf-8", errors="replace")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return Fetched(url, _now(), False, error=f"{type(exc).__name__}: {exc}")
    if not as_json:
        return Fetched(url, _now(), True, data=body.strip())
    try:
        return Fetched(url, _now(), True, data=json.loads(body))
    except ValueError as exc:
        return Fetched(url, _now(), False, error=f"Response was not JSON: {exc}")


def fetch_json(url: str, timeout: float = 15.0) -> Fetched:
    return fetch(url, timeout, as_json=True)


def fetch_text(url: str, timeout: float = 15.0) -> Fetched:
    return fetch(url, timeout, as_json=False)


def age_minutes(fetched_at: str | None, now: datetime | None = None) -> float | None:
    if not fetched_at:
        return None
    try:
        then = datetime.fromisoformat(fetched_at)
    except ValueError:
        return None
    now = now or datetime.now(timezone.utc)
    return (now - then).total_seconds() / 60.0
