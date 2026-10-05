"""Walk-forward backtest of the club scripts against bookmaker odds.

Data: football-data.co.uk results with Pinnacle odds for the Premier League
(E0), Championship (E1) and Primeira Liga (P1), seasons 2018-19 to 2025-26.
Run `python club_backtest.py --fetch` once to download them into data/clubs/.

Every week (Monday to Sunday) of 2019-20 to 2025-26 is predicted from matches
played before that Monday only, using each script's own fitting and scoring
functions, i.e. the way the scripts are used (run weekly, 7-day window):

  PL   pl_model.py           previous season + current season so far, fading promoted prior
  PL   two seasons, old prior  the same with the old 0.80/1.20 prior, not fading
  PPL  liga_portugal_model.py          previous season + current season so far, fading promoted prior
  PPL  two seasons, old prior  likewise
  ELC  cl_corners_model.py    previous + current season, fading relegated/promoted priors
  ELC  two seasons, old prior  the same without division priors (0.80/1.20 for any newcomer)
  ELC  current season only   cl_corners_model.py's fit before 2026-10-04, for comparison

Promoted/relegated priors are measured from the other test seasons only (leave one
season out), so a season's own results never inform its priors.

The benchmark is Pinnacle: its early price (collected Friday for weekend games,
Tuesday for midweek, i.e. when the scripts would be run) and its closing price.
Bets are simulated at the early price; closing-line value (CLV) says whether
those prices were beaten by the close. Writes docs/CLUB_BACKTEST_RESULTS.md.
"""
import contextlib
import csv
import hashlib
import io
import math
import os
import statistics
import sys
import time
import urllib.request
from collections import namedtuple
from concurrent.futures import ProcessPoolExecutor
from datetime import date, datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, "data", "clubs")
REPORT_PATH = os.path.join(HERE, "docs", "CLUB_BACKTEST_RESULTS.md")
URL = "https://www.football-data.co.uk/mmz4281/{season}/{div}.csv"
SEASONS = ["1819", "1920", "2021", "2122", "2223", "2324", "2425", "2526"]
LEAGUES = {"E0": "Premier League", "E1": "Championship", "P1": "Primeira Liga"}
# SHA-256 of the files as downloaded on 2026-10-04 (the report was generated from these).
FILES = {
    "1819_E0.csv": "7c096b3c2ecd54c6993d22eeea73450c2bde11e3457238b226b8f43c62dfc35e",
    "1819_E1.csv": "112eedf787e26e00aac1442ef86e7fee5d4a43f86552ff2dac31ceb95a849487",
    "1819_P1.csv": "e9caf283aa870e4e866b5716660fb86aa5c598cca2f2750ef3642e429a961db5",
    "1920_E0.csv": "100037618b94f94057400bb02bf6bac4ef74ddaa58cde4b38370839c39caee61",
    "1920_E1.csv": "f31450c623c0b74b89692584a85924cc652d33ec89757acf4b2dda5e26261d57",
    "1920_P1.csv": "b65f3fa55dd1362af093ad5f823f6b0ec1925e796048688bcbdebc19fab8b240",
    "2021_E0.csv": "5afe63f69401457b8354eaacee24f9a3e520b3c3af6329564a9783e20d789c62",
    "2021_E1.csv": "cf93295e310c60e5b97ae7466d772f8ec6a908ad401e38fceb7e8617369d5073",
    "2021_P1.csv": "c6bb505a2a2a3f5a71f5d37cbe9f717fb683d706b66170b6b3bef7947a5252cb",
    "2122_E0.csv": "335afcbabeb2939fa10ab39ba3e8215072d0b577cb8d0705c1e44c56e934e703",
    "2122_E1.csv": "a181451be4801b9f838daf5f6aaf21e9f09d36042be9e6c15388e71001e772ea",
    "2122_P1.csv": "c4477c4f40da93fbba978fa52b76dc962df3a998a464f9c5ae5462d1b36e3a63",
    "2223_E0.csv": "8442792d3b614c94ea3cf381bd2736805889cc1713169035368fff19c3d02380",
    "2223_E1.csv": "cd38ab0586ea8cb4d573c363ff70749117f116da5b2741e5b0ea37da05860b1b",
    "2223_P1.csv": "acb35c152f1198de9d02cdea622bc815e1438bae00d1822e0fe48a787405a84a",
    "2324_E0.csv": "b2e057b0ed959f198b0f63d2391c01239f3608e6de5db68edab3f88e04d07ff3",
    "2324_E1.csv": "5737f6cc95c95092b28317f41d3123e2fb09345434efc1c84bbdf15830849ab3",
    "2324_P1.csv": "681e132bee3948b432436e907a935ea8fc4a8ccace86dad67d61be0159035758",
    "2425_E0.csv": "d0c8ce4a96d886cf60cf101f570f4a3893844226f91c7bd769eb568c49edbfa4",
    "2425_E1.csv": "f34c340446c916374be66bdc2fb25b87ee7f2662712ca009653d03ac705535f9",
    "2425_P1.csv": "e3cceff81e471c59e72c1f60f81b2e62df21d1a58af167062d4915484da00255",
    "2526_E0.csv": "3e3a8352f9ada6789c508d6ca184424421fed56a30400904a4a327c583407e62",
    "2526_E1.csv": "98954c319950f19158624b17a154ef1c56eb7b8d169ef317f28f06d11d0b9a74",
    "2526_P1.csv": "f1353dc7404bfa0d27daa3ca6c164bb450ec20558ee02fffb7160fae182213dc",
}
# (label, league code, script module, fitting method)
CONFIGS = [
    ("PL: pl_model.py", "E0", "pl_model", "two_seasons"),
    ("PPL: liga_portugal_model.py", "P1", "liga_portugal_model", "two_seasons"),
    ("ELC: cl_corners_model.py", "E1", "cl_corners_model", "league"),
    ("ELC: two seasons, old prior", "E1", "cl_corners_model", "league_old_prior"),
    ("ELC: current season only", "E1", "cl_corners_model", "current_only"),
    ("PL: two seasons, old prior", "E0", "pl_model", "two_seasons_old_prior"),
    ("PPL: two seasons, old prior", "P1", "liga_portugal_model", "two_seasons_old_prior"),
    ("PL: regular_prediction_model.py", "E0", "regular_prediction_model", "regular"),
    ("PPL: regular_prediction_model.py", "P1", "regular_prediction_model", "regular"),
    ("PL: regular, current season only (old)", "E0", "regular_prediction_model", "regular_old"),
    ("PPL: regular, current season only (old)", "P1", "regular_prediction_model", "regular_old"),
]
THRESHOLDS = (0.0, 0.05, 0.10)   # minimum model edge (EV at the early price) to bet
OLD_NEWCOMER_PRIOR = (0.80, 1.20)  # what every new Championship side got before 2026-10-04

Match = namedtuple("Match", "season day home away hg ag open1x2 close1x2 open_ou close_ou")
Pred = namedtuple("Pred", "match p1x2 p_over")


# ----------------------------- data ---------------------------------------

def fetch_data():
    os.makedirs(DATA_DIR, exist_ok=True)
    for name in FILES:
        season, div = name[:-4].split("_")
        with urllib.request.urlopen(URL.format(season=season, div=div)) as resp, \
                open(os.path.join(DATA_DIR, name), "wb") as f:
            f.write(resp.read())
        time.sleep(1)
    print(f"  downloaded {len(FILES)} files into data/clubs/")


def check_data():
    changed = []
    for name, want in FILES.items():
        path = os.path.join(DATA_DIR, name)
        if not os.path.exists(path):
            raise SystemExit(f"Missing data/clubs/{name} -- run: python club_backtest.py --fetch")
        with open(path, "rb") as f:
            if hashlib.sha256(f.read()).hexdigest() != want:
                changed.append(name)
    if changed:   # football-data.co.uk occasionally corrects old files
        print(f"  [warn] {len(changed)} file(s) differ from the 2026-10-04 download "
              f"(e.g. {changed[0]}); results may differ slightly from the committed report.")


def _odds(row, *keys):
    vals = []
    for k in keys:
        try:
            v = float(row.get(k) or "")
        except ValueError:
            return None
        if v <= 1.0:
            return None
        vals.append(v)
    return tuple(vals)


def _day(s):
    for fmt in ("%d/%m/%Y", "%d/%m/%y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    raise ValueError(f"unparseable date {s!r}")


def load_league(div):
    """-> {season: [Match, ...] sorted by date}."""
    out = {}
    for season in SEASONS:
        with open(os.path.join(DATA_DIR, f"{season}_{div}.csv"), encoding="utf-8-sig", errors="replace") as f:
            rows = [r for r in csv.DictReader(f) if (r.get("HomeTeam") or "").strip() and (r.get("FTHG") or "").strip()]
        ms = [Match(season, _day(r["Date"]), r["HomeTeam"].strip(), r["AwayTeam"].strip(),
                    int(r["FTHG"]), int(r["FTAG"]),
                    _odds(r, "PSH", "PSD", "PSA"), _odds(r, "PSCH", "PSCD", "PSCA"),
                    _odds(r, "P>2.5", "P<2.5"), _odds(r, "PC>2.5", "PC<2.5")) for r in rows]
        out[season] = sorted(ms, key=lambda m: m.day)
    return out


def teams_of(matches):
    return {t for m in matches for t in (m.home, m.away)}


def newcomers(e0, e1, prev, season):
    """Championship sides new to the division in `season` -> {team: 'relegated' | 'promoted'}.
    Division membership is known before a season starts, so this uses no results."""
    pl_prev, elc_prev = teams_of(e0[prev]), teams_of(e1[prev])
    return {t: ("relegated" if t in pl_prev else "promoted")
            for t in teams_of(e1[season]) if t not in elc_prev}


def promoted_priors(league, exclude):
    """Geometric-mean first-season attack / defence (goals per game vs the league
    average) of sides promoted into `league`, over the test seasons except `exclude`.
    The same measurement gave pl_model.py / liga_portugal_model.py's PROMOTED_ATT/DEF."""
    acc = []
    for prev, season in zip(SEASONS, SEASONS[1:]):
        if season == exclude:
            continue
        ms = league[season]
        per_team_game = sum(m.hg + m.ag for m in ms) / (2 * len(ms))
        for t in teams_of(ms) - teams_of(league[prev]):
            games = [m for m in ms if t in (m.home, m.away)]
            gf = sum(m.hg if m.home == t else m.ag for m in games)
            ga = sum(m.ag if m.home == t else m.hg for m in games)
            acc.append((gf / len(games) / per_team_game, ga / len(games) / per_team_game))
    gm = lambda xs: math.exp(statistics.mean(math.log(max(x, 0.05)) for x in xs))
    return gm([a for a, _ in acc]), gm([d for _, d in acc])


def newcomer_priors(e0, e1, exclude):
    """Geometric-mean first-season attack / defence (goals scored / conceded per game vs
    the division average) of relegated and promoted sides, over the test seasons except
    `exclude`. The same measurement gave cl_corners_model.py's ELC_*_ATT/DEF."""
    acc = {"relegated": [], "promoted": []}
    for prev, season in zip(SEASONS, SEASONS[1:]):
        if season == exclude:
            continue
        elc = e1[season]
        per_team_game = sum(m.hg + m.ag for m in elc) / (2 * len(elc))
        for t, kind in newcomers(e0, e1, prev, season).items():
            games = [m for m in elc if t in (m.home, m.away)]
            gf = sum(m.hg if m.home == t else m.ag for m in games)
            ga = sum(m.ag if m.home == t else m.hg for m in games)
            acc[kind].append((gf / len(games) / per_team_game, ga / len(games) / per_team_game))
    gm = lambda xs: math.exp(statistics.mean(math.log(max(x, 0.05)) for x in xs))
    return {k: (gm([a for a, _ in v]), gm([d for _, d in v])) for k, v in acc.items()}


# ------------------------- walk-forward model ------------------------------

def old_fit_team_ratings(module, gv):
    """The league scripts' fit before the fading promoted prior: with no priors every
    side (promoted ones included) starts at and is shrunk toward 1.0, exactly as before."""
    return module.fit_team_ratings(gv, priors={})

def predict_week(module, method, history, before, week, priors=None):
    """Fit as the script does on the data available before the week, then score
    the week's matches with the script's own matrix and market functions.
    `priors` ({team: (attack, defence)}) is used by the Championship league fit."""
    priors = priors or {}
    team_prior = None
    if method in ("two_seasons", "two_seasons_old_prior"):
        gv = [(m.home, m.away, m.hg, m.ag, m.day.isoformat()) for m in history + before]
        with contextlib.redirect_stdout(io.StringIO()):
            if method == "two_seasons":   # fading promoted prior, as the scripts now run
                att, dff, avg_h, avg_a = module.fit_team_ratings(gv, priors=priors)
            else:                         # before: shrink everyone toward 1.0
                att, dff, avg_h, avg_a = old_fit_team_ratings(module, gv)
            rho = module.fit_rho(gv, att, dff, avg_h, avg_a, 1.0)
        if method == "two_seasons":
            prior_att = prior_def = 1.0
            team_prior = lambda t: priors.get(t, (module.PROMOTED_ATT, module.PROMOTED_DEF))
        else:
            prior_att, prior_def = OLD_NEWCOMER_PRIOR
    elif method == "regular":   # the standard model: two seasons, its generic newcomer prior
        gv = [(m.home, m.away, m.hg, m.ag, m.day.isoformat()) for m in history + before]
        with contextlib.redirect_stdout(io.StringIO()):
            att, dff, avg_h, avg_a = module.fit_team_ratings(gv, priors=priors)
            rho = module.fit_rho(gv, att, dff, avg_h, avg_a, 1.0)
        prior_att, prior_def = module.NEWCOMER_ATT, module.NEWCOMER_DEF
    elif method == "regular_old":   # the standard model before: current season only
        if len(before) >= 10:
            gv = [(m.home, m.away, m.hg, m.ag, m.day.isoformat()) for m in before]
            with contextlib.redirect_stdout(io.StringIO()):
                att, dff, avg_h, avg_a = module.fit_team_ratings(gv, priors={})
                rho = module.fit_rho(gv, att, dff, avg_h, avg_a, 1.0)
        else:
            att, dff, avg_h, avg_a, rho = {}, {}, 1.50, 1.15, -0.13
        prior_att = prior_def = 1.0
    elif method == "league":   # cl_corners_model's Championship fit, with division priors
        gv = [(m.home, m.away, m.hg, m.ag, m.day.isoformat()) for m in history + before]
        att, dff, avg_h, avg_a = module.fit_league_ratings(gv, priors=priors)
        rho = module.fit_rho_league(gv, att, dff, avg_h, avg_a)
        prior_att = prior_def = 1.0
        team_prior = lambda t: priors.get(t, (1.0, 1.0))
    elif method == "league_old_prior":   # the same fit before division priors
        gv = [(m.home, m.away, m.hg, m.ag, m.day.isoformat()) for m in history + before]
        att, dff, avg_h, avg_a = module.fit_league_ratings(gv)
        rho = module.fit_rho_league(gv, att, dff, avg_h, avg_a)
        prior_att, prior_def = OLD_NEWCOMER_PRIOR
    else:   # cl_corners_model: current season only, neutral fallback below 10 matches
        if len(before) >= 10:
            att, dff, avg_h, avg_a = module.fit_team_ratings([(m.home, m.away, m.hg, m.ag) for m in before])
        else:
            att, dff, avg_h, avg_a = {}, {}, 1.50, 1.15
        rho, prior_att, prior_def = -0.13, 1.0, 1.0
    preds = []
    for m in week:
        hp = team_prior(m.home) if team_prior else (prior_att, prior_def)
        ap = team_prior(m.away) if team_prior else (prior_att, prior_def)
        h_att, h_def = att.get(m.home, hp[0]), dff.get(m.home, hp[1])
        a_att, a_def = att.get(m.away, ap[0]), dff.get(m.away, ap[1])
        lh, la = round(avg_h * h_att * a_def, 3), round(avg_a * a_att * h_def, 3)
        s = module.summarize_matrix(module.build_score_matrix(lh, la, rho))
        preds.append(Pred(m, (s["home"], s["draw"], s["away"]), s["over_2_5"]))
    return preds


def run_config(config):
    label, div, module_name, method = config
    module = __import__(module_name)
    by_season = load_league(div)
    e0 = load_league("E0") if method == "league" else None
    preds = []
    for prev, season in zip(SEASONS, SEASONS[1:]):
        current = by_season[season]
        priors = {}
        if method == "league":
            values = newcomer_priors(e0, by_season, exclude=season)
            priors = {t: values[kind] for t, kind in newcomers(e0, by_season, prev, season).items()}
        elif method == "two_seasons":
            value = promoted_priors(by_season, exclude=season)
            priors = {t: value for t in teams_of(current) - teams_of(by_season[prev])}
        elif method == "regular":   # a fixed generic prior, not measured per league
            priors = {t: (module.NEWCOMER_ATT, module.NEWCOMER_DEF)
                      for t in teams_of(current) - teams_of(by_season[prev])}
        for monday in sorted({m.day - timedelta(days=m.day.weekday()) for m in current}):
            before = [m for m in current if m.day < monday]
            week = [m for m in current if monday <= m.day < monday + timedelta(days=7)]
            preds += predict_week(module, method, by_season[prev], before, week, priors)
    return label, preds


# --------------------------- evaluation ------------------------------------

def fair(odds):
    inv = [1 / o for o in odds]
    return [x / sum(inv) for x in inv]


def outcome(m):
    return 0 if m.hg > m.ag else 1 if m.hg == m.ag else 2


def mean_se_t(xs):
    if len(xs) < 2:
        return (xs[0] if xs else float("nan")), float("nan"), float("nan")
    mu, se = statistics.mean(xs), statistics.stdev(xs) / math.sqrt(len(xs))
    return mu, se, (mu / se if se > 0 else float("nan"))


def accuracy(preds):
    rows = [p for p in preds if p.match.open1x2 and p.match.close1x2]
    ll = {"model": [], "open": [], "close": []}
    for p in rows:
        k = outcome(p.match)
        ll["model"].append(-math.log(max(p.p1x2[k], 1e-12)))
        ll["open"].append(-math.log(fair(p.match.open1x2)[k]))
        ll["close"].append(-math.log(fair(p.match.close1x2)[k]))
    ou = [p for p in preds if p.match.close_ou]
    ou_model = [-math.log(max(p.p_over if p.match.hg + p.match.ag >= 3 else 1 - p.p_over, 1e-12)) for p in ou]
    ou_close = [-math.log(fair(p.match.close_ou)[0 if p.match.hg + p.match.ag >= 3 else 1]) for p in ou]
    return {
        "n": len(rows), "ll": {k: statistics.mean(v) for k, v in ll.items()},
        "d_close": mean_se_t([a - b for a, b in zip(ll["model"], ll["close"])]),
        "d_open": mean_se_t([a - b for a, b in zip(ll["model"], ll["open"])]),
        "n_ou": len(ou), "ou_model": statistics.mean(ou_model), "ou_close": statistics.mean(ou_close),
        "d_ou": mean_se_t([a - b for a, b in zip(ou_model, ou_close)]),
    }


def blend(preds, w):
    """Log loss of (1-w) x fair early price + w x model, minus the early price alone,
    per match. Negative at some w > 0 means the model carries information the early
    price lacks, even if the model alone is worse."""
    diffs = []
    for p in preds:
        if not (p.match.open1x2 and p.match.close1x2):
            continue
        k, mkt = outcome(p.match), fair(p.match.open1x2)
        mixed = (1 - w) * mkt[k] + w * p.p1x2[k]
        diffs.append(-math.log(mixed) + math.log(mkt[k]))
    return mean_se_t(diffs)


def bets(preds, market, thr):
    """Flat 1-unit bets on every selection whose model EV at the early price
    exceeds thr. -> list of (season, profit, clv), clv = EV at the fair closing price."""
    out = []
    for p in preds:
        m = p.match
        if market == "1x2":
            if not (m.open1x2 and m.close1x2):
                continue
            probs, opened, closed, won = p.p1x2, m.open1x2, fair(m.close1x2), outcome(m)
        else:
            if not (m.open_ou and m.close_ou):
                continue
            probs, opened, closed = (p.p_over, 1 - p.p_over), m.open_ou, fair(m.close_ou)
            won = 0 if m.hg + m.ag >= 3 else 1
        for k in range(len(probs)):
            if probs[k] * opened[k] - 1 > thr:
                out.append((m.season, (opened[k] - 1) if k == won else -1.0, opened[k] * closed[k] - 1))
    return out


def fmt_season(s):
    return f"20{s[:2]}-{s[2:]}"


def main():
    if "--fetch" in sys.argv:
        fetch_data()
    check_data()
    t0 = time.time()
    with ProcessPoolExecutor(max_workers=len(CONFIGS)) as pool:
        results = dict(pool.map(run_config, CONFIGS))
    print(f"  walk-forward done in {time.time() - t0:.0f}s")

    out = ["# Club backtest results", "",
           f"Generated by `club_backtest.py` on {date.today().isoformat()} in {time.time() - t0:.0f}s. "
           "Do not edit by hand; re-run the script.", "",
           "## Setup", "",
           "- **Data:** football-data.co.uk, Premier League (E0), Championship (E1) and Primeira Liga (P1), "
           f"{fmt_season(SEASONS[0])} to {fmt_season(SEASONS[-1])}.",
           f"  The test seasons are {fmt_season(SEASONS[1])} to {fmt_season(SEASONS[-1])}; {fmt_season(SEASONS[0])} "
           "is fitting history only.",
           "- **Walk-forward:** every week, Monday to Sunday, is predicted from matches before that Monday only. "
           "Predictions use each script's own `fit_team_ratings`, `fit_rho`, `build_score_matrix` and "
           "`summarize_matrix`, with home advantage applied once, as the scripts now do.",
           "- **Promoted, relegated or unseen teams:** the deployed configs use the scripts' measured, fading "
           "priors (promoted sides in the league scripts; relegated and promoted sides in the Championship), "
           "re-measured with each test season left out. The \"old prior\" rows use the former 0.80/1.20 "
           "judgment call. `regular_prediction_model.py` uses its fixed generic newcomer prior "
           "(0.73/1.16, between the measured PL and Primeira Liga values, so not out of sample for those "
           "two leagues); its \"old\" rows are the script before 2026-10-05, fitted on the current season "
           "only with unseen sides at 1.0.",
           "- **Market:** Pinnacle. The *early* price (PSH/PSD/PSA, P>2.5/P<2.5) is collected on Friday for "
           "weekend games and Tuesday for midweek, which is when you would run the scripts. The *closing* price is "
           "PSCH/PSCD/PSCA and PC>2.5/PC<2.5. Margins are removed proportionally to get fair probabilities.",
           "- **Odds coverage:** only matches with both Pinnacle early and closing 1X2 odds are scored against the "
           "market or bet on. In the final season's files, Pinnacle closing odds stop on "
           + ", ".join(f"{LEAGUES[div]} {max(p.match.day for p in results[label] if p.match.season == SEASONS[-1] and p.match.close1x2)}"
                       for label, div, *_ in CONFIGS[:3]) + ", so that season is only partly tested.",
           "- **Bets:** flat 1-unit stakes, placed at the early price, on every selection where the model's EV at "
           "that price exceeds the threshold.",
           "  - **ROI:** profit per unit staked.",
           "  - **CLV:** the bet's EV at the fair *closing* price. It is the standard and much less noisy test of "
           "edge: positive CLV means the bets beat the market close.",
           "- **Caveats:** results are reported for every league, market and threshold, so expect some "
           "|t| ≈ 2 by chance. Treat a single significant cell with suspicion. Pinnacle limits and real-world "
           "price availability are ignored.", "",
           "## 1. Accuracy against the market (1X2 and O/U 2.5 log loss; lower is better)", "",
           "| Model | Matches | Model | Pinnacle early | Pinnacle close | Model − close (t) | Model − early (t) "
           "| O/U 2.5: model / close (t) |",
           "|---|---|---|---|---|---|---|---|"]
    for label, *_ in CONFIGS:
        a = accuracy(results[label])
        out.append(f"| {label} | {a['n']} | {a['ll']['model']:.4f} | {a['ll']['open']:.4f} | {a['ll']['close']:.4f} | "
                   f"{a['d_close'][0]:+.4f} ({a['d_close'][2]:+.1f}) | {a['d_open'][0]:+.4f} ({a['d_open'][2]:+.1f}) | "
                   f"{a['ou_model']:.4f} / {a['ou_close']:.4f} ({a['d_ou'][2]:+.1f}) |")
    out += ["", "**Does the model add information to the early price?** This blends the two as "
            "(1 − w) × early + w × model. A negative Δ (blend minus early alone) means it does.", "",
            "| Model | w = 0.1 | w = 0.2 | w = 0.3 |", "|---|---|---|---|"]
    for label, *_ in CONFIGS:
        cells = [f"{d:+.4f} ({t:+.1f})" for d, _, t in (blend(results[label], w) for w in (0.1, 0.2, 0.3))]
        out.append(f"| {label} | " + " | ".join(cells) + " |")
    def paired_ll(a, b, keep=lambda m: True):
        """-> (1X2 diff stats, O/U 2.5 diff stats, n): per-match log loss of a minus b."""
        d1x2, dou = [], []
        for pa, pb in zip(results[a], results[b]):
            m = pa.match
            if keep(m):
                k = outcome(m)
                d1x2.append(-math.log(max(pa.p1x2[k], 1e-12)) + math.log(max(pb.p1x2[k], 1e-12)))
                over = m.hg + m.ag >= 3
                qa, qb = (pa.p_over, pb.p_over) if over else (1 - pa.p_over, 1 - pb.p_over)
                dou.append(-math.log(max(qa, 1e-12)) + math.log(max(qb, 1e-12)))
        return mean_se_t(d1x2), mean_se_t(dou), len(d1x2)

    e0, e1, p1 = load_league("E0"), load_league("E1"), load_league("P1")

    def early(sides, league):
        """Matches involving one of `sides[season]` in the first 10 weeks of its season."""
        starts = {s: league[s][0].day for s in SEASONS[1:]}
        return lambda m: ((m.home in sides[m.season] or m.away in sides[m.season])
                          and (m.day - starts[m.season]).days < 70)

    promoted = {div: {s: teams_of(lg[s]) - teams_of(lg[p]) for p, s in zip(SEASONS, SEASONS[1:])}
                for div, lg in (("E0", e0), ("P1", p1))}
    new_sides = {s: set(newcomers(e0, e1, p, s)) for p, s in zip(SEASONS, SEASONS[1:])}
    early_newcomer = early(new_sides, e1)
    rows = [
            ("PL: fading promoted prior − old 0.80/1.20 prior, all matches", "PL: pl_model.py",
             "PL: two seasons, old prior", lambda m: True),
            ("PL: same, matches with a promoted side, first 10 weeks", "PL: pl_model.py",
             "PL: two seasons, old prior", early(promoted["E0"], e0)),
            ("PPL: fading promoted prior − old 0.80/1.20 prior, all matches", "PPL: liga_portugal_model.py",
             "PPL: two seasons, old prior", lambda m: True),
            ("PPL: same, matches with a promoted side, first 10 weeks", "PPL: liga_portugal_model.py",
             "PPL: two seasons, old prior", early(promoted["P1"], p1)),("two-season fit with division priors (deployed) − old current-season-only fit", "ELC: cl_corners_model.py",
             "ELC: current season only", lambda m: True),
            ("division priors − old 0.80/1.20 prior, all matches", "ELC: cl_corners_model.py",
             "ELC: two seasons, old prior", lambda m: True),
            ("division priors − old prior, matches with a relegated/promoted side, first 10 weeks",
             "ELC: cl_corners_model.py", "ELC: two seasons, old prior", early_newcomer),
            ("regular: two seasons + newcomer prior − old current-season-only fit, PL",
             "PL: regular_prediction_model.py", "PL: regular, current season only (old)", lambda m: True),
            ("regular: same, PL, first 10 weeks of each season", "PL: regular_prediction_model.py",
             "PL: regular, current season only (old)", early({s: teams_of(e0[s]) for s in SEASONS[1:]}, e0)),
            ("regular: two seasons + newcomer prior − old current-season-only fit, PPL",
             "PPL: regular_prediction_model.py", "PPL: regular, current season only (old)", lambda m: True),
            ("regular: same, PPL, first 10 weeks of each season", "PPL: regular_prediction_model.py",
             "PPL: regular, current season only (old)", early({s: teams_of(p1[s]) for s in SEASONS[1:]}, p1))]
    out += ["", "**Model-change comparisons (per-match log loss over all test matches; negative = first is better; "
            "ELC rows compare Championship versions):**", "",
            "| Comparison | Matches | Δ 1X2 (t) | Δ O/U 2.5 (t) |", "|---|---|---|---|"]
    for text, a, b, keep in rows:
        r1, ro, n = paired_ll(a, b, keep)
        out.append(f"| {text} | {n} | {r1[0]:+.4f} ({r1[2]:+.1f}) | {ro[0]:+.4f} ({ro[2]:+.1f}) |")
    out.append("")
    out += ["", "## 2. Betting at Pinnacle's early price", "",
            "| Model | Market | Min edge | Bets | ROI (t) | CLV (t) |", "|---|---|---|---|---|---|"]
    summary = []
    for label, *_ in CONFIGS:
        for market in ("1x2", "ou"):
            for thr in THRESHOLDS:
                b = bets(results[label], market, thr)
                if not b:
                    out.append(f"| {label} | {market.upper()} | {thr:.0%} | 0 | – | – |")
                    continue
                roi, clv = mean_se_t([x[1] for x in b]), mean_se_t([x[2] for x in b])
                out.append(f"| {label} | {'1X2' if market == '1x2' else 'O/U 2.5'} | {thr:.0%} | {len(b)} | "
                           f"{roi[0]:+.1%} ({roi[2]:+.1f}) | {clv[0]:+.1%} ({clv[2]:+.1f}) |")
                summary.append((label, market, thr, len(b), roi, clv))
    out += ["", "## 3. By season (1X2, minimum edge 5%)", "",
            "| Model | " + " | ".join(fmt_season(s) for s in SEASONS[1:]) + " |",
            "|---" * (len(SEASONS)) + "|"]
    for label, *_ in CONFIGS:
        b = bets(results[label], "1x2", 0.05)
        cells = []
        for s in SEASONS[1:]:
            xs = [x for x in b if x[0] == s]
            cells.append(f"{statistics.mean(x[1] for x in xs):+.0%} ROI, {statistics.mean(x[2] for x in xs):+.1%} CLV "
                         f"({len(xs)})" if xs else "–")
        out.append(f"| {label} | " + " | ".join(cells) + " |")
    best_clv = max((r for r in summary if not math.isnan(r[5][2])), key=lambda r: r[5][2])
    out += ["", "## 4. Reading this", "",
            f"- **Highest CLV t-stat of all {len(summary)} cells:** {best_clv[0]}, "
            f"{'1X2' if best_clv[1] == '1x2' else 'O/U 2.5'} at {best_clv[2]:.0%} minimum edge: "
            f"CLV {best_clv[5][0]:+.1%} (t {best_clv[5][2]:+.1f}) over {best_clv[3]} bets.",
            "- **How to read CLV:** a model with real edge should show positive CLV consistently across leagues and "
            "thresholds, not in one cell. Negative CLV means the market moved against the model's picks before "
            "kickoff. That is the usual signature of a model that is less informed than the market.", ""]

    os.makedirs(os.path.dirname(REPORT_PATH), exist_ok=True)
    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")
    for line in out[out.index("## 1. Accuracy against the market (1X2 and O/U 2.5 log loss; lower is better)"):]:
        print("  " + line)
    print(f"\n  Report written to {os.path.relpath(REPORT_PATH, HERE)} ({time.time() - t0:.0f}s)")


if __name__ == "__main__":
    # The summary uses a true minus sign, which a Windows code page can't encode when
    # output is redirected to a file.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    main()
