"""Profitability arithmetic, market parsing and route comparison.

All prices here are synthetic test values. They exercise the arithmetic and the
reliability rules; they are not real market data.
"""

from datetime import datetime, timedelta, timezone

import pytest

from homeminer import ledger, profit, routes
from homeminer.benchmark.results import BenchmarkResult, ResultStore
from homeminer.market import bitcoin, nicehash, snapshot
from homeminer.market.http import Fetched

from .conftest import load_json

NOW = datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)
DIFFICULTY = 140.58e12


def _iso(minutes_ago: float) -> str:
    return (NOW - timedelta(minutes=minutes_ago)).isoformat(timespec="seconds")


def _quotes(fetched_at: str):
    quotes, warnings = nicehash.parse(load_json("synthetic_nicehash_info.json"),
                                      load_json("synthetic_nicehash_algorithms.json"), fetched_at, "https://test")
    assert warnings == []
    return quotes


def _network(fees=0.02):
    return bitcoin.NetworkStats(DIFFICULTY, 971_712, 3.125, fees, _iso(1), ["https://test"])


def _inputs(**overrides):
    base = dict(device="cpu", nicehash_algorithm="RANDOMXMONERO", hashrate_hs=7662.1,
                hashrate_kind="local_benchmark", hashrate_ref="r1", watts=40.0, power_kind="measured-wall",
                fees_verification="primary")
    base.update(overrides)
    return profit.EstimateInputs(**base)


def test_block_subsidy_schedule():
    assert bitcoin.block_subsidy_btc(0) == 50
    assert bitcoin.block_subsidy_btc(840_000) == 3.125
    assert bitcoin.block_subsidy_btc(971_712) == 3.125
    assert bitcoin.block_subsidy_btc(1_050_000) == 1.5625
    assert bitcoin.block_subsidy_btc(64 * 210_000) == 0


def test_hashprice_matches_hand_calculation():
    hp = profit.network_hashprice_btc_per_hs_day(DIFFICULTY, 3.125)
    assert hp * 1e12 == pytest.approx(4.4718e-7, rel=1e-4)  # BTC per TH/s per day, as in FEASIBILITY.md


def test_parse_nicehash_units():
    quotes = _quotes(_iso(1))
    rx = quotes["RANDOMXMONERO"]
    assert rx.market_factor == 1e6 and rx.display_unit == "MH"
    assert rx.btc_per_hs_day == pytest.approx(2e-13)


def test_calibration_accepts_consistent_units_and_rejects_wrong_ones():
    good = profit.calibrate(_quotes(_iso(1)), _network())
    assert good.ok and good.ratio == pytest.approx(1.0, abs=0.01)
    bad_quotes = _quotes(_iso(1))
    bad_quotes["SHA256"].paying = 4.5e-19  # as if the price were per H/s rather than per TH/s
    bad = profit.calibrate(bad_quotes, _network())
    assert not bad.ok
    missing = profit.calibrate({}, _network())
    assert not missing.ok and missing.ratio is None


def test_nicehash_estimate_arithmetic():
    quotes = _quotes(_iso(5))
    cal = profit.calibrate(quotes, _network())
    price = bitcoin.PriceQuote(60_000.0, _iso(5), "https://test")
    est = profit.estimate_nicehash(_inputs(electricity_eur_per_kwh=0.0), quotes["RANDOMXMONERO"], cal, price, now=NOW)
    assert est.reliable, [i.message for i in est.issues]
    gross = 7662.1 * 2e-13
    credited = gross * 0.99 * 0.98
    net = credited * (1 - 0.0001 / 0.002)
    assert est.btc["gross_per_day"] == pytest.approx(gross)
    assert est.btc["credited_per_day"] == pytest.approx(credited)
    assert est.btc["net_per_day"] == pytest.approx(net)
    assert est.btc["net_per_month"] == pytest.approx(net * 30.4375)
    assert est.btc["net_per_year"] == pytest.approx(net * 365.25)
    assert est.payout["withdrawal_amount_btc"] == pytest.approx(0.002)
    assert est.payout["days_to_withdrawal"] == pytest.approx(0.002 / credited)
    assert est.payout["days_to_internal_min"] == pytest.approx(0.00001 / credited)
    assert any("60-day rule" in n for n in est.payout["notes"])
    assert est.electricity["kwh_per_day"] == pytest.approx(0.96)
    assert est.electricity["cost_eur_per_day"] == 0.0
    assert est.eur["net_after_costs_per_day"] == pytest.approx(net * 60_000)
    assert est.break_even["max_electricity_eur_per_kwh"] == pytest.approx(net * 60_000 / 0.96)
    assert "Not applicable" in est.break_even["hardware"]
    assert est.kind == "estimate" and "not BTC received" in est.disclaimer
    assert est.excluded_costs


def test_electricity_price_is_editable():
    quotes = _quotes(_iso(5))
    cal = profit.calibrate(quotes, _network())
    price = bitcoin.PriceQuote(60_000.0, _iso(5), "https://test")
    est = profit.estimate_nicehash(_inputs(electricity_eur_per_kwh=0.30), quotes["RANDOMXMONERO"], cal, price, now=NOW)
    assert est.electricity["cost_eur_per_day"] == pytest.approx(0.96 * 0.30)
    assert est.eur["net_after_costs_per_day"] < 0


@pytest.mark.parametrize("overrides,quote_age,expected", [
    ({"hashrate_hs": None}, 5, "No measured hash rate"),
    ({"hashrate_kind": "assumption"}, 5, "assumption"),
    ({}, 2000, "more than a day old"),
])
def test_unreliable_inputs_give_no_numbers(overrides, quote_age, expected):
    quotes = _quotes(_iso(quote_age))
    est = profit.estimate_nicehash(_inputs(**overrides), quotes["RANDOMXMONERO"],
                                   profit.calibrate(quotes, _network()), None, now=NOW)
    assert not est.reliable
    assert any(expected in i.message for i in est.issues if i.severity == "blocker")
    assert est.btc == {} and est.eur is None


def test_unlisted_algorithm_and_failed_calibration_block():
    est = profit.estimate_nicehash(_inputs(), None, None, None, now=NOW)
    assert not est.reliable and any("does not currently list" in i.message for i in est.issues)
    quotes = _quotes(_iso(5))
    quotes["SHA256"].paying = 1.0
    est = profit.estimate_nicehash(_inputs(), quotes["RANDOMXMONERO"], profit.calibrate(quotes, _network()),
                                   None, now=NOW)
    assert not est.reliable


def test_warnings_for_weaker_inputs():
    quotes = _quotes(_iso(45))
    est = profit.estimate_nicehash(_inputs(power_kind="estimated", fees_verification="secondary"),
                                   quotes["RANDOMXMONERO"], profit.calibrate(quotes, _network()), None, now=NOW)
    assert est.reliable
    messages = " ".join(i.message for i in est.issues if i.severity == "warning")
    assert "45 minutes old" in messages
    assert "estimated from utilisation" in messages
    assert "secondary sources" in messages
    assert "local benchmark" in messages
    assert "BTC/EUR price unavailable" in messages
    assert est.eur is None


def test_solo_expectation_matches_feasibility_report():
    exp = profit.solo_expectation(5e9, bitcoin.NetworkStats(DIFFICULTY, 971_712, 3.125, None, _iso(1)))
    assert exp.sats_per_day == pytest.approx(0.2236, rel=1e-3)
    assert exp.sats_per_year == pytest.approx(81.67, rel=1e-3)
    assert exp.expected_years_to_block == pytest.approx(3.8266e6, rel=1e-3)
    assert exp.probability_block_within_year == pytest.approx(2.613e-7, rel=1e-3)
    assert exp.days_to_1000_sats / 365.25 == pytest.approx(12.2, rel=1e-2)


# --- market fetching with fake transports -------------------------------------

def _fake_fetchers(fail_nicehash=False, fail_mempool=False):
    def json_fetcher(url, timeout):
        if "nicehash" in url:
            if fail_nicehash:
                return Fetched(url, _iso(0), False, error="blocked")
            name = "synthetic_nicehash_info.json" if "simplemultialgo" in url else "synthetic_nicehash_algorithms.json"
            return Fetched(url, _iso(0), True, load_json(name))
        if "mempool" in url and fail_mempool:
            return Fetched(url, _iso(0), False, error="blocked")
        if url.endswith("/v1/mining/hashrate/3d"):
            return Fetched(url, _iso(0), True, {"currentDifficulty": DIFFICULTY, "currentHashrate": 1e21})
        if "reward-stats" in url:
            return Fetched(url, _iso(0), True, {"totalFee": str(int(0.02 * 144 * 1e8))})
        if url.endswith("/v1/prices"):
            return Fetched(url, _iso(0), True, {"time": 0, "EUR": 60000})
        if "coingecko" in url:
            return Fetched(url, _iso(0), True, {"bitcoin": {"eur": 59000}})
        return Fetched(url, _iso(0), False, error="unknown")

    def text_fetcher(url, timeout):
        if "mempool" in url and fail_mempool:
            return Fetched(url, _iso(0), False, error="blocked")
        if url.endswith("/blocks/tip/height") or url.endswith("getblockcount"):
            return Fetched(url, _iso(0), True, "971712")
        if url.endswith("getdifficulty"):
            return Fetched(url, _iso(0), True, str(DIFFICULTY))
        return Fetched(url, _iso(0), False, error="unknown")

    return json_fetcher, text_fetcher


def test_snapshot_refresh_and_cache(cfg):
    jf, tf = _fake_fetchers()
    snap = snapshot.refresh(cfg, jf, tf)
    assert set(snap.nicehash) == {"SHA256", "RANDOMXMONERO", "KAWPOW"}
    assert snap.network.difficulty == DIFFICULTY and snap.network.subsidy_btc == 3.125
    assert snap.network.avg_fees_btc_per_block == pytest.approx(0.02)
    assert snap.price.eur == 60000 and "mempool" in snap.price.source
    snapshot.save(cfg, snap)
    cached = snapshot.load_cached(cfg)
    assert cached.from_cache and cached.created_at == snap.created_at
    assert cached.nicehash["RANDOMXMONERO"].paying == snap.nicehash["RANDOMXMONERO"].paying


def test_snapshot_fallbacks_and_failures(cfg):
    jf, tf = _fake_fetchers(fail_nicehash=True, fail_mempool=True)
    snap = snapshot.refresh(cfg, jf, tf)
    assert snap.nicehash == {}
    assert any("NiceHash API unreachable" in e for e in snap.errors)
    assert snap.network.difficulty == DIFFICULTY and "blockchain.info" in snap.network.sources[0]
    assert snap.price.eur == 59000 and "coingecko" in snap.price.source


def test_http_refuses_plain_http():
    from homeminer.market.http import fetch

    assert not fetch("http://example.invalid/x").ok


# --- ledger -------------------------------------------------------------------

def test_ledger_keeps_kinds_apart(cfg):
    book = ledger.Ledger(cfg.data_dir)
    book.add("unpaid_balance", "0.00000420", "NiceHash dashboard", as_of="2026-10-24T00:00:00+00:00")
    book.add("unpaid_balance", "0.00000500", "NiceHash dashboard", as_of="2026-10-25T00:00:00+00:00")
    book.add("received_in_wallet", 0, "My wallet app")
    summary = book.summary()
    assert summary["unpaid_balance"]["amount_btc"] == "0.00000500"
    assert summary["received_in_wallet"]["amount_btc"] == "0.00000000"
    assert summary["confirmed_pool"]["amount_btc"] is None
    assert summary["estimated"]["label"].startswith("Estimated")
    with pytest.raises(ValueError):
        book.add("received_in_wallet", "-1", "x")
    with pytest.raises(ValueError):
        book.add("confirmed_pool", "0.1", "")
    with pytest.raises(ValueError):
        book.add("bogus", "0.1", "x")


# --- route comparison ---------------------------------------------------------

def test_route_comparison(cfg):
    jf, tf = _fake_fetchers()
    snap = snapshot.refresh(cfg, jf, tf)
    store = ResultStore(cfg.data_dir)
    store.save(BenchmarkResult(id="b1", device="cpu", algorithm="rx/0", status="completed", hashrate_hs=7662.1,
                               finished_at=_iso(0), verification="match",
                               power={"watts": 38.5, "kind": "estimated", "method": "model"}))
    out = routes.compare(cfg, snap, store, sha256_reference={"hashrate_hs": 1.5e6, "method": "test"})
    cpu = out["route_a"]["estimates"]["cpu"]
    gpu = out["route_a"]["estimates"]["gpu"]
    assert cpu["reliable"] and cpu["inputs"]["power_kind"] == "estimated"
    assert not gpu["reliable"]
    assert "Route A on the CPU" in out["verdict"]
    assert "not the same as mining Bitcoin" in out["receiving_vs_mining"]
    assert out["route_b"]["upper_bound"]["sats_per_year"] < 200
    assert out["route_b"]["cpu_reference"]["hashrate_kind"] == "local_reference"
    assert "Not recommended" in out["route_b"]["recommendation"]

    cfg.data["power"]["wall_meter_cpu_watts"] = 52
    out = routes.compare(cfg, snap, store)
    assert out["route_a"]["estimates"]["cpu"]["inputs"]["power_kind"] == "measured-wall"


def test_route_comparison_without_market_data(cfg):
    out = routes.compare(cfg, None, ResultStore(cfg.data_dir))
    assert out["verdict"].startswith("Earnings cannot be estimated reliably")
    assert out["route_b"]["upper_bound"] is None
    assert out["route_a"]["fees"]["mining_fee_pct"] == 2.0
