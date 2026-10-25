"""One timestamped set of market inputs, cached in the data folder."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..config import Config
from . import bitcoin, nicehash
from .http import age_minutes, fetch_json, fetch_text


@dataclass
class MarketSnapshot:
    created_at: str
    nicehash: dict[str, nicehash.NiceHashQuote] = field(default_factory=dict)
    nicehash_meta: dict[str, Any] = field(default_factory=dict)
    network: bitcoin.NetworkStats | None = None
    price: bitcoin.PriceQuote | None = None
    errors: list[str] = field(default_factory=list)
    from_cache: bool = False

    def age_minutes(self, now: datetime | None = None) -> float | None:
        return age_minutes(self.created_at, now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "created_at": self.created_at,
            "nicehash": {k: v.to_dict() for k, v in self.nicehash.items()},
            "nicehash_meta": self.nicehash_meta,
            "network": self.network.to_dict() if self.network else None,
            "price": self.price.to_dict() if self.price else None,
            "errors": self.errors,
            "from_cache": self.from_cache,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MarketSnapshot:
        quotes = {}
        for name, q in (data.get("nicehash") or {}).items():
            q = dict(q)
            q.pop("btc_per_hs_day", None)
            quotes[name] = nicehash.NiceHashQuote(**q)
        net = data.get("network")
        if net:
            net = dict(net)
            net.pop("reward_btc", None)
            net = bitcoin.NetworkStats(**net)
        price = data.get("price")
        return cls(
            created_at=data["created_at"], nicehash=quotes, nicehash_meta=data.get("nicehash_meta") or {},
            network=net, price=bitcoin.PriceQuote(**price) if price else None,
            errors=list(data.get("errors") or []), from_cache=bool(data.get("from_cache")),
        )


def refresh(config: Config, json_fetcher=fetch_json, text_fetcher=fetch_text) -> MarketSnapshot:
    m = config.get("market")
    timeout = float(m.get("timeout_s", 15))
    snap = MarketSnapshot(created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
    quotes, meta = nicehash.fetch_quotes(m["nicehash_api"], timeout, json_fetcher)
    snap.nicehash, snap.nicehash_meta = quotes, meta
    snap.errors.extend(meta.get("warnings", []))
    snap.network, errs = bitcoin.fetch_network(m["mempool_api"], m["blockchain_info_api"], timeout,
                                               json_fetcher, text_fetcher)
    snap.errors.extend(errs)
    snap.price, errs = bitcoin.fetch_price_eur(m["mempool_api"], m["coingecko_api"], timeout, json_fetcher)
    snap.errors.extend(errs)
    return snap


def cache_path(config: Config) -> Path:
    return config.data_dir / "market_cache.json"


def save(config: Config, snap: MarketSnapshot) -> None:
    path = cache_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(snap.to_dict(), indent=2), encoding="utf-8")


def load_cached(config: Config) -> MarketSnapshot | None:
    try:
        data = json.loads(cache_path(config).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    try:
        snap = MarketSnapshot.from_dict(data)
    except (KeyError, TypeError):
        return None
    snap.from_cache = True
    return snap
