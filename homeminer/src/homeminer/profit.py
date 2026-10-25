"""Profitability and payout estimates.

Rules this module follows:
  * Estimates use a measured hash rate (local benchmark or pool-verified) and
    timestamped market prices. Missing, stale or inconsistent inputs produce an
    explicit "cannot estimate reliably" result with no numbers, never a guess.
  * Every figure is an *estimate* at today's prices and difficulty. It is not
    an unpaid balance, not confirmed earnings and not BTC in your wallet; the
    ledger module tracks those separately.
  * Electricity defaults to EUR 0.00/kWh but is always an editable input.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from .market.bitcoin import NetworkStats, PriceQuote
from .market.http import age_minutes
from .market.nicehash import NiceHashQuote

DAYS_PER_MONTH = 30.4375
DAYS_PER_YEAR = 365.25
HARD_MAX_QUOTE_AGE_MIN = 24 * 60
# NiceHash's SHA256 price should sit near Bitcoin's hashprice. Outside this band
# the unit interpretation (or the data) is wrong, and no estimate is trusted.
CALIBRATION_BAND = (1 / 3, 3.0)

EXCLUDED_COSTS = [
    "Hardware wear: fans, pumps, thermal paste/pads and an older GPU's capacitors age faster under constant load.",
    "Heat and noise in the room; possible extra cooling in summer.",
    "Opportunity cost: with solar, a kWh used here is a kWh not exported, so the real price is the feed-in tariff.",
    "Taxes on mining income, which depend on where you live.",
    "BTC price moves, and custodial risk while a balance sits on the marketplace.",
    "Internet usage and your own time.",
]

DISCLAIMER = ("Estimate only, at current prices and difficulty, which change constantly. It is not a forecast, "
              "not an unpaid balance, and not BTC received in your wallet.")


def network_hashprice_btc_per_hs_day(difficulty: float, reward_btc: float) -> float:
    """Expected BTC per day for 1 H/s of SHA-256d: 86,400 x reward / (difficulty x 2^32)."""
    return 86_400 * reward_btc / (difficulty * 2**32)


@dataclass
class Calibration:
    ok: bool
    ratio: float | None
    nicehash_btc_per_th_day: float | None
    network_btc_per_th_day: float | None
    message: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def calibrate(quotes: dict[str, NiceHashQuote], network: NetworkStats | None) -> Calibration:
    sha = quotes.get("SHA256")
    if sha is None or network is None:
        return Calibration(False, None, None, None,
                           "Could not cross-check NiceHash's price units (SHA256 price or Bitcoin difficulty missing).")
    nh = sha.btc_per_hs_day * 1e12
    net = network_hashprice_btc_per_hs_day(network.difficulty, network.reward_btc) * 1e12
    ratio = nh / net if net else None
    ok = ratio is not None and CALIBRATION_BAND[0] <= ratio <= CALIBRATION_BAND[1]
    message = (f"NiceHash pays {ratio:.2f}x Bitcoin's network hashprice for SHA256: price units look right."
               if ok else
               f"NiceHash's SHA256 price is {ratio:.3g}x Bitcoin's hashprice. That is implausible, so HomeMiner's "
               "reading of NiceHash's units (or the data) is wrong. No NiceHash estimate is shown.")
    return Calibration(ok, ratio, nh, net, message)


@dataclass
class Issue:
    severity: str  # blocker | warning
    message: str


@dataclass
class EstimateInputs:
    device: str
    nicehash_algorithm: str
    hashrate_hs: float | None
    hashrate_kind: str  # local_benchmark | pool_verified | assumption
    hashrate_ref: str | None
    watts: float | None
    power_kind: str  # measured-wall | measured-sensor | estimated | unavailable
    electricity_eur_per_kwh: float = 0.0
    wear_eur_per_day: float = 0.0
    marketplace_fee_pct: float = 2.0
    miner_fee_pct: float = 1.0
    payout_min_btc: float = 0.00001
    payout_interval_hours: float = 4.0
    withdrawal_fee_btc: float = 0.0001
    withdrawal_min_btc: float = 0.0002
    withdrawal_target_pct: float = 5.0
    inactive_funds_days: float = 60.0
    fees_verification: str = "secondary"
    fees_source: str = ""
    fees_verified_on: str = ""
    max_quote_age_min: float = 30.0


@dataclass
class Estimate:
    route: str
    device: str
    algorithm: str
    reliable: bool
    issues: list[Issue] = field(default_factory=list)
    kind: str = "estimate"
    inputs: dict[str, Any] = field(default_factory=dict)
    btc: dict[str, float | None] = field(default_factory=dict)
    eur: dict[str, float | None] | None = None
    electricity: dict[str, Any] = field(default_factory=dict)
    payout: dict[str, Any] = field(default_factory=dict)
    break_even: dict[str, Any] = field(default_factory=dict)
    excluded_costs: list[str] = field(default_factory=lambda: list(EXCLUDED_COSTS))
    sources: list[dict[str, Any]] = field(default_factory=list)
    calibration: dict[str, Any] | None = None
    disclaimer: str = DISCLAIMER

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def estimate_nicehash(inputs: EstimateInputs, quote: NiceHashQuote | None, calibration: Calibration | None,
                      price: PriceQuote | None, now: datetime | None = None) -> Estimate:
    est = Estimate(route="nicehash", device=inputs.device, algorithm=inputs.nicehash_algorithm, reliable=False,
                   inputs=asdict(inputs))
    issues = est.issues

    # --- inputs that make an estimate impossible -----------------------------
    if not inputs.hashrate_hs or inputs.hashrate_hs <= 0:
        issues.append(Issue("blocker", f"No measured hash rate for the {inputs.device.upper()}. Run its benchmark first."))
    elif inputs.hashrate_kind == "assumption":
        issues.append(Issue("blocker", "The hash rate is an assumption, not a measurement. NiceHash estimates "
                                       "need a measured figure."))
    if quote is None:
        issues.append(Issue("blocker", f"NiceHash does not currently list {inputs.nicehash_algorithm}, or its prices "
                                       "could not be fetched. This route cannot be estimated."))
    else:
        if not quote.enabled:
            issues.append(Issue("blocker", f"NiceHash lists {quote.algorithm} as disabled."))
        age = age_minutes(quote.fetched_at, now)
        if age is None or age > HARD_MAX_QUOTE_AGE_MIN:
            issues.append(Issue("blocker", "NiceHash prices are more than a day old. Refresh market data."))
        elif age > inputs.max_quote_age_min:
            issues.append(Issue("warning", f"NiceHash prices are {age:.0f} minutes old. Refresh for a current figure."))
        est.sources.append({"what": f"NiceHash paying price for {quote.algorithm}", "url": quote.source,
                            "fetched_at": quote.fetched_at})
    if calibration is None or calibration.ratio is None:
        issues.append(Issue("warning", (calibration.message if calibration else
                                        "NiceHash price units were not cross-checked.")))
    elif not calibration.ok:
        issues.append(Issue("blocker", calibration.message))
    if calibration is not None:
        est.calibration = calibration.to_dict()

    # --- inputs that weaken an estimate --------------------------------------
    if inputs.hashrate_kind == "local_benchmark":
        issues.append(Issue("warning", "Hash rate is from an offline local benchmark, not verified by a pool. "
                                       "Real pool performance may differ."))
    if inputs.fees_verification != "primary":
        issues.append(Issue("warning", f"NiceHash fees are from {inputs.fees_verification} sources "
                                       f"(checked {inputs.fees_verified_on}). Confirm them on nicehash.com."))
    if inputs.power_kind == "estimated":
        issues.append(Issue("warning", "Power is estimated from utilisation, not measured. Enter a wall-meter "
                                       "reading for a real figure."))
    elif inputs.power_kind == "unavailable" or inputs.watts is None:
        issues.append(Issue("warning", "Power draw unknown: electricity cost and break-even cannot be computed."))
    if price is None:
        issues.append(Issue("warning", "BTC/EUR price unavailable: euro amounts are not shown."))
    else:
        est.sources.append({"what": "BTC/EUR price", "url": price.source, "fetched_at": price.fetched_at})

    est.reliable = not any(i.severity == "blocker" for i in issues)
    if not est.reliable:
        return est

    # --- arithmetic ------------------------------------------------------------
    assert quote is not None and inputs.hashrate_hs
    gross = inputs.hashrate_hs * quote.btc_per_hs_day
    miner_fee = gross * inputs.miner_fee_pct / 100
    marketplace_fee = (gross - miner_fee) * inputs.marketplace_fee_pct / 100
    credited = gross - miner_fee - marketplace_fee

    economic = inputs.withdrawal_fee_btc / (inputs.withdrawal_target_pct / 100) if inputs.withdrawal_target_pct > 0 else 0
    withdrawal_amount = max(inputs.withdrawal_min_btc, economic)
    withdrawal_per_day = inputs.withdrawal_fee_btc * credited / withdrawal_amount if withdrawal_amount else 0.0
    net = credited - withdrawal_per_day

    est.btc = {
        "gross_per_day": gross,
        "miner_fee_per_day": miner_fee,
        "marketplace_fee_per_day": marketplace_fee,
        "credited_per_day": credited,
        "withdrawal_fee_per_day_amortised": withdrawal_per_day,
        "net_per_day": net,
        "net_per_month": net * DAYS_PER_MONTH,
        "net_per_year": net * DAYS_PER_YEAR,
    }

    days_to_internal = inputs.payout_min_btc / credited if credited > 0 else math.inf
    days_to_withdrawal = withdrawal_amount / credited if credited > 0 else math.inf
    days_to_min_withdrawal = inputs.withdrawal_min_btc / credited if credited > 0 else math.inf
    hold_limit = max(inputs.inactive_funds_days - 10, 0)
    payout_notes: list[str] = []
    if days_to_min_withdrawal > hold_limit:
        payout_notes.append(
            f"Even the minimum withdrawal ({inputs.withdrawal_min_btc:g} BTC) takes about {days_to_min_withdrawal:,.0f} "
            f"days, longer than NiceHash's {inputs.inactive_funds_days:.0f}-day rule allows funds to sit. Small balances "
            "may be moved with a fee before you can withdraw them economically.")
    elif days_to_withdrawal > hold_limit:
        payout_notes.append(
            f"An economic withdrawal ({withdrawal_amount:g} BTC, fee at most {inputs.withdrawal_target_pct:g}%) takes "
            f"about {days_to_withdrawal:,.0f} days, beyond the {inputs.inactive_funds_days:.0f}-day rule. Withdrawing "
            f"sooner costs a larger share: at the {inputs.withdrawal_min_btc:g} BTC minimum the fee is "
            f"{100 * inputs.withdrawal_fee_btc / inputs.withdrawal_min_btc:.0f}%.")
    payout_notes.append("Lightning withdrawals are offered by NiceHash and usually cost less; their fee is not modelled.")
    est.payout = {
        "internal_min_btc": inputs.payout_min_btc,
        "internal_interval_hours": inputs.payout_interval_hours,
        "days_to_internal_min": days_to_internal,
        "first_internal_payout_hours": max(inputs.payout_interval_hours, days_to_internal * 24),
        "withdrawal_amount_btc": withdrawal_amount,
        "withdrawal_fee_btc": inputs.withdrawal_fee_btc,
        "withdrawal_fee_pct_of_amount": 100 * inputs.withdrawal_fee_btc / withdrawal_amount if withdrawal_amount else None,
        "days_to_withdrawal": days_to_withdrawal,
        "days_to_min_withdrawal": days_to_min_withdrawal,
        "inactive_funds_days": inputs.inactive_funds_days,
        "notes": payout_notes,
    }

    kwh_day = inputs.watts * 24 / 1000 if inputs.watts is not None else None
    cost_day = kwh_day * inputs.electricity_eur_per_kwh if kwh_day is not None else None
    est.electricity = {
        "watts": inputs.watts, "power_kind": inputs.power_kind, "kwh_per_day": kwh_day,
        "eur_per_kwh": inputs.electricity_eur_per_kwh, "cost_eur_per_day": cost_day,
    }

    if price is not None:
        revenue = net * price.eur
        profit_day = revenue - (cost_day or 0.0) - inputs.wear_eur_per_day
        est.eur = {
            "btc_eur": price.eur,
            "gross_per_day": gross * price.eur,
            "fees_per_day": (miner_fee + marketplace_fee + withdrawal_per_day) * price.eur,
            "net_revenue_per_day": revenue,
            "electricity_per_day": cost_day,
            "wear_per_day": inputs.wear_eur_per_day,
            "net_after_costs_per_day": profit_day,
            "net_after_costs_per_month": profit_day * DAYS_PER_MONTH,
            "net_after_costs_per_year": profit_day * DAYS_PER_YEAR,
        }
        if kwh_day:
            max_price = (revenue - inputs.wear_eur_per_day) / kwh_day
            est.break_even = {
                "max_electricity_eur_per_kwh": max_price,
                "note": (f"Above EUR {max_price:.4f}/kWh this device loses money on electricity alone."
                         if max_price > 0 else "Loses money at any electricity price once wear is counted."),
            }
        else:
            est.break_even = {"max_electricity_eur_per_kwh": None,
                              "note": "Power draw unknown, so the break-even electricity price cannot be computed."}
    else:
        est.break_even = {"max_electricity_eur_per_kwh": None, "note": "Needs the BTC/EUR price."}
    est.break_even["hardware"] = ("Not applicable: this uses hardware you already own. HomeMiner does not suggest "
                                  "buying mining hardware.")
    return est


# --- Route B: direct SHA-256 (educational) ---------------------------------

@dataclass
class SoloExpectation:
    hashrate_hs: float
    hashrate_kind: str
    btc_per_day: float
    sats_per_day: float
    sats_per_year: float
    expected_days_to_block: float
    expected_years_to_block: float
    probability_block_within_year: float
    network_share: float
    days_to_1000_sats: float
    difficulty: float
    reward_btc: float
    source: dict[str, Any]
    note: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def solo_expectation(hashrate_hs: float, network: NetworkStats, hashrate_kind: str = "assumption") -> SoloExpectation:
    hashprice = network_hashprice_btc_per_hs_day(network.difficulty, network.reward_btc)
    btc_day = hashrate_hs * hashprice
    expected_seconds = network.difficulty * 2**32 / hashrate_hs
    expected_days = expected_seconds / 86_400
    network_hs = network.difficulty * 2**32 / 600
    return SoloExpectation(
        hashrate_hs=hashrate_hs, hashrate_kind=hashrate_kind,
        btc_per_day=btc_day, sats_per_day=btc_day * 1e8, sats_per_year=btc_day * 1e8 * DAYS_PER_YEAR,
        expected_days_to_block=expected_days, expected_years_to_block=expected_days / DAYS_PER_YEAR,
        probability_block_within_year=-math.expm1(-DAYS_PER_YEAR / expected_days),
        network_share=hashrate_hs / network_hs,
        days_to_1000_sats=1000 / (btc_day * 1e8) if btc_day > 0 else math.inf,
        difficulty=network.difficulty, reward_btc=network.reward_btc,
        source={"fetched_at": network.fetched_at, "urls": network.sources},
        note=("Educational. Bitcoin is mined by specialised ASICs; this hardware cannot compete, and no purchase "
              "is suggested."),
    )
