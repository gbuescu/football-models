"""Bitcoin network inputs (difficulty, height, fees) and the BTC/EUR price.

Primary source: mempool.space's public API. Fallbacks: blockchain.info for
network figures and CoinGecko for the price. Every value keeps the URL it came
from and when it was fetched.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from .http import Fetched, fetch_json, fetch_text

HALVING_INTERVAL = 210_000
SATS = 100_000_000


def block_subsidy_btc(height: int) -> float:
    halvings = height // HALVING_INTERVAL
    if halvings >= 64:
        return 0.0
    return (50 * SATS >> halvings) / SATS


@dataclass
class NetworkStats:
    difficulty: float
    height: int | None
    subsidy_btc: float
    avg_fees_btc_per_block: float | None
    fetched_at: str
    sources: list[str] = field(default_factory=list)

    @property
    def reward_btc(self) -> float:
        return self.subsidy_btc + (self.avg_fees_btc_per_block or 0.0)

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["reward_btc"] = self.reward_btc
        return out


@dataclass
class PriceQuote:
    eur: float
    fetched_at: str
    source: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _float(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def fetch_network(mempool: str, blockchain_info: str, timeout: float = 15.0,
                  json_fetcher=fetch_json, text_fetcher=fetch_text) -> tuple[NetworkStats | None, list[str]]:
    errors: list[str] = []
    stats: Fetched = json_fetcher(f"{mempool}/v1/mining/hashrate/3d", timeout)
    height_doc: Fetched = text_fetcher(f"{mempool}/blocks/tip/height", timeout)
    difficulty = _float((stats.data or {}).get("currentDifficulty")) if stats.ok and isinstance(stats.data, dict) else None
    height = int(_float(height_doc.data) or 0) if height_doc.ok else None
    sources = [stats.url, height_doc.url]
    fetched_at = stats.fetched_at

    if difficulty is None or not height:
        errors.append("mempool.space unavailable; trying blockchain.info.")
        d_doc: Fetched = text_fetcher(f"{blockchain_info}/q/getdifficulty", timeout)
        h_doc: Fetched = text_fetcher(f"{blockchain_info}/q/getblockcount", timeout)
        difficulty = difficulty or (_float(d_doc.data) if d_doc.ok else None)
        height = height or (int(_float(h_doc.data) or 0) if h_doc.ok else None)
        sources = [d_doc.url, h_doc.url]
        fetched_at = d_doc.fetched_at
    if not difficulty or difficulty <= 0:
        errors.append("Bitcoin difficulty unavailable from every source.")
        return None, errors
    if not height:
        errors.append("Block height unavailable: assuming the post-2024 subsidy of 3.125 BTC.")

    fees = None
    reward: Fetched = json_fetcher(f"{mempool}/v1/mining/reward-stats/144", timeout)
    if reward.ok and isinstance(reward.data, dict):
        total_fee = _float(reward.data.get("totalFee"))
        if total_fee is not None:
            fees = total_fee / 144 / SATS
            sources.append(reward.url)
    subsidy = block_subsidy_btc(height) if height else 3.125
    return NetworkStats(difficulty, height or None, subsidy, fees, fetched_at, sources), errors


def fetch_price_eur(mempool: str, coingecko: str, timeout: float = 15.0,
                    json_fetcher=fetch_json) -> tuple[PriceQuote | None, list[str]]:
    doc: Fetched = json_fetcher(f"{mempool}/v1/prices", timeout)
    if doc.ok and isinstance(doc.data, dict) and _float(doc.data.get("EUR")):
        return PriceQuote(float(doc.data["EUR"]), doc.fetched_at, doc.url), []
    cg: Fetched = json_fetcher(f"{coingecko}/simple/price?ids=bitcoin&vs_currencies=eur", timeout)
    try:
        return PriceQuote(float(cg.data["bitcoin"]["eur"]), cg.fetched_at, cg.url), []
    except (TypeError, KeyError, ValueError):
        return None, ["BTC/EUR price unavailable: amounts are shown in BTC only."]
