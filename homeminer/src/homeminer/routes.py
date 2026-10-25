"""Side-by-side comparison of the two routes to BTC."""

from __future__ import annotations

from typing import Any

from . import compat, profit
from .benchmark.results import BenchmarkResult, ResultStore
from .config import Config
from .market.snapshot import MarketSnapshot
from .wallet import ROUTE_ADDRESS_REQUIREMENTS

# Deliberately generous: well above anything a GTX 970 plus this CPU could do at SHA-256d.
SHA256_UPPER_BOUND_HS = 5e9

ROUTE_A_FACTS = {
    "title": "Route A: sell hash power on NiceHash, paid in BTC",
    "what_it_is": ("Your PC computes RandomX (or another algorithm) for buyers on NiceHash, who choose which coin "
                   "to mine with it. NiceHash credits you in BTC. You receive BTC; you are not mining Bitcoin."),
    "software": ("NiceHash Miner 3.1.1.6 (2025-10-08) bundles Excavator and GMiner, with NanoMiner and lolMiner "
                 "optional; it no longer bundles XMRig. QuickMiner/Excavator list the GTX 1000 series as the oldest "
                 "supported GeForce cards. HomeMiner uses official XMRig releases for the CPU instead."),
    "requirements": [
        ROUTE_ADDRESS_REQUIREMENTS["nicehash"],
        "Two-factor authentication on your NiceHash account is strongly advised.",
    ],
    "risks": [
        "Custodial: your balance sits with NiceHash until you withdraw it.",
        "NiceHash's 60-day rule: funds held longer are moved with a fee.",
        "Prices change every minute with buyer demand.",
    ],
}

ROUTE_B_FACTS = {
    "title": "Route B: mine Bitcoin directly (SHA-256), educational only",
    "what_it_is": "Your PC would hash Bitcoin block headers itself, solo or through a pool.",
    "why_not": [
        "Bitcoin's difficulty is set by specialised SHA-256 ASICs rated in the hundreds of TH/s each, tens of "
        "thousands of times more than this PC could manage even at the generous bound below.",
        "Per joule, ASICs are thousands of times more efficient, so free electricity does not close the gap.",
        "No maintained official GPU SHA-256 miner exists for a GTX 970, and HomeMiner will not write one.",
        "Pool payout thresholds would take years or decades to reach.",
    ],
    "recommendation": "Not recommended. No purchase is suggested, and meaningful BTC earnings are not likely.",
}


def _inputs(config: Config, device: str, result: BenchmarkResult | None) -> profit.EstimateInputs:
    fees = config.get("fees", "nicehash")
    econ = config.get("economics")
    power = (result.power or {}) if result else {}
    return profit.EstimateInputs(
        device=device,
        nicehash_algorithm="RANDOMXMONERO",
        hashrate_hs=result.hashrate_hs if result else None,
        hashrate_kind=result.result_kind if result else "missing",
        hashrate_ref=result.id if result else None,
        watts=power.get("watts"),
        power_kind=power.get("kind", "unavailable"),
        electricity_eur_per_kwh=float(econ["electricity_eur_per_kwh"]),
        wear_eur_per_day=float(econ["wear_cost_eur_per_day"]),
        marketplace_fee_pct=float(fees["mining_fee_pct"]),
        miner_fee_pct=float(config.get("miners", "xmrig", "donate_level_pct") or 1),
        payout_min_btc=float(fees["payout_min_btc"]),
        payout_interval_hours=float(fees["payout_interval_hours"]),
        withdrawal_fee_btc=float(fees["withdrawal_fee_btc_onchain"]),
        withdrawal_min_btc=float(fees["withdrawal_min_btc_onchain"]),
        withdrawal_target_pct=float(econ["withdrawal_fee_target_pct"]),
        inactive_funds_days=float(fees["inactive_funds_days"]),
        fees_verification=str(fees["verification"]),
        fees_source=str(fees["source"]),
        fees_verified_on=str(fees["verified_on"]),
        max_quote_age_min=float(config.get("market", "max_quote_age_minutes")),
    )


def _wall_override(config: Config, device: str, result: BenchmarkResult | None) -> BenchmarkResult | None:
    """A wall-meter reading entered after the benchmark replaces its power figure."""
    if result is None:
        return None
    wall = float(config.get("power", f"wall_meter_{device}_watts") or 0)
    if wall > 0:
        result.power = {"watts": wall, "kind": "measured-wall", "method": "Wall-meter reading you entered."}
    return result


def compare(config: Config, snapshot: MarketSnapshot | None, store: ResultStore,
            sha256_reference: dict | None = None) -> dict[str, Any]:
    calibration = profit.calibrate(snapshot.nicehash, snapshot.network) if snapshot else None
    estimates = {}
    for device in ("cpu", "gpu"):
        result = _wall_override(config, device, store.latest_usable(device, "rx/0"))
        quote = snapshot.nicehash.get("RANDOMXMONERO") if snapshot else None
        est = profit.estimate_nicehash(_inputs(config, device, result), quote, calibration,
                                       snapshot.price if snapshot else None)
        estimates[device] = est.to_dict()
        estimates[device]["benchmark"] = result.to_dict() if result else None

    route_b: dict[str, Any] = dict(ROUTE_B_FACTS)
    if snapshot and snapshot.network:
        route_b["upper_bound"] = profit.solo_expectation(SHA256_UPPER_BOUND_HS, snapshot.network,
                                                         "assumption (deliberately generous upper bound)").to_dict()
        if sha256_reference:
            route_b["cpu_reference"] = {
                **sha256_reference,
                **profit.solo_expectation(sha256_reference["hashrate_hs"], snapshot.network,
                                          "local_reference").to_dict(),
            }
    else:
        route_b["upper_bound"] = None
        route_b["unavailable"] = "Bitcoin difficulty could not be fetched, so Route B cannot be computed right now."

    reliable = [d for d, e in estimates.items() if e["reliable"]]
    if reliable:
        best = max(reliable, key=lambda d: estimates[d]["btc"]["net_per_day"])
        verdict = (f"Route A on the {best.upper()} is the only route with a measurable return. "
                   "Route B is not viable on this hardware.")
    else:
        reasons = sorted({i["message"] for e in estimates.values() for i in e["issues"] if i["severity"] == "blocker"})
        verdict = "Earnings cannot be estimated reliably yet. " + " ".join(reasons[:3])

    gpu_notes = [a for a in compat.CATALOG if a.device == "gpu" and a.route == "nicehash"]
    return {
        "verdict": verdict,
        "receiving_vs_mining": "Receiving BTC from a marketplace is not the same as mining Bitcoin.",
        "route_a": {**ROUTE_A_FACTS, "fees": config.get("fees", "nicehash"), "estimates": estimates,
                    "calibration": calibration.to_dict() if calibration else None,
                    "gpu_algorithms_considered": [
                        {"algorithm": a.title, "memory": a.memory_note, "source": a.memory_source} for a in gpu_notes]},
        "route_b": route_b,
        "market": snapshot.to_dict() if snapshot else None,
    }
