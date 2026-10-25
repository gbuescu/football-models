"""NiceHash public API: what NiceHash currently pays per algorithm.

Endpoints (public, no account or key needed):
  GET /main/api/v2/public/simplemultialgo/info  -> "paying" per algorithm
  GET /main/api/v2/mining/algorithms            -> "marketFactor" and display unit

HomeMiner reads ``paying`` as BTC per ``marketFactor`` H/s per day, which is how
NiceHash's developer documentation describes the factor. That reading is
checked on every refresh: NiceHash's SHA256 price is compared with Bitcoin's
network hashprice, which it should roughly track because SHA256 buyers mine
Bitcoin. If they disagree wildly, every NiceHash estimate is marked unreliable.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from .http import Fetched, fetch_json

INFO_PATH = "/main/api/v2/public/simplemultialgo/info"
ALGORITHMS_PATH = "/main/api/v2/mining/algorithms"


@dataclass
class NiceHashQuote:
    algorithm: str
    title: str | None
    paying: float
    market_factor: float
    display_unit: str | None
    enabled: bool
    fetched_at: str
    source: str

    @property
    def btc_per_hs_day(self) -> float:
        return self.paying / self.market_factor

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["btc_per_hs_day"] = self.btc_per_hs_day
        return out


def _float(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def parse(info: dict, algorithms: dict, fetched_at: str, source: str) -> tuple[dict[str, NiceHashQuote], list[str]]:
    warnings: list[str] = []
    meta = {}
    for item in (algorithms or {}).get("miningAlgorithms", []) or []:
        name = str(item.get("algorithm", "")).upper()
        if name:
            meta[name] = item
    quotes: dict[str, NiceHashQuote] = {}
    for item in (info or {}).get("miningAlgorithms", []) or []:
        name = str(item.get("algorithm", "")).upper()
        paying = _float(item.get("paying"))
        if not name or paying is None:
            continue
        m = meta.get(name)
        if m is None:
            warnings.append(f"{name}: no unit information from NiceHash; skipped.")
            continue
        factor = _float(m.get("marketFactor"))
        if not factor or factor <= 0:
            warnings.append(f"{name}: invalid marketFactor; skipped.")
            continue
        quotes[name] = NiceHashQuote(
            algorithm=name, title=item.get("title") or m.get("title"), paying=paying, market_factor=factor,
            display_unit=m.get("displayMarketFactor"), enabled=bool(m.get("enabled", True)),
            fetched_at=fetched_at, source=source,
        )
    if not quotes:
        warnings.append("NiceHash returned no usable prices.")
    return quotes, warnings


def fetch_quotes(api_base: str, timeout: float = 15.0, fetcher=fetch_json) -> tuple[dict[str, NiceHashQuote], dict]:
    info: Fetched = fetcher(f"{api_base}{INFO_PATH}", timeout)
    algos: Fetched = fetcher(f"{api_base}{ALGORITHMS_PATH}", timeout)
    meta = {"info": info.to_dict(), "algorithms": algos.to_dict(), "warnings": []}
    if not info.ok or not algos.ok:
        meta["warnings"].append("NiceHash API unreachable: no NiceHash estimate can be made.")
        return {}, meta
    quotes, warnings = parse(info.data, algos.data, info.fetched_at, info.url)
    meta["warnings"].extend(warnings)
    return quotes, meta
