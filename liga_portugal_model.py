# -*- coding: utf-8 -*-
"""Liga Portugal (Primeira Liga) model, refined from
regular_prediction_model.py.

Same two-stage shape as pl_model.py. Stage one pulls every finished match of
the completed season and reports the league-wide splits, the final table
rebuilt from the match record, per-team home and away breakdowns and the common
scorelines, then writes the lot to CSV. Stage two fits Dixon-Coles ratings on
those matches plus the current season's finished ones (time-decay makes this
season count most) and predicts the API's scheduled fixtures for the next
DAYS_AHEAD days (--days N), unless FIXTURES lists specific games.

Tuned separately from the Premier League version. Home advantage is the
season's own home/away goal split, applied once (docs/MODEL_CHANGES.md; 1.26 in
2025-26, much like the PL), and the fitted rho came out at 0.0 on real data: this
league genuinely showed no low-score correction last season. Name resolution is
simpler here than in pl_model.py, because Portuguese club names do not collide
on shared tokens the way the English ones do, but it does have to cope with
accents.

The rebuilt table is the fetch sanity check. Porto should come out champions
for 2025-26. If the champions are wrong, the pull is broken and nothing below
it is worth reading.

Season keys work by starting year, so 2025 means 2025-26. Requesting without
the season parameter returns the current season, which in August has no
finished matches and quietly leaves every team on a neutral 1.0 rating. Hence
the season is always passed explicitly, worked out from today's date.
"""

import csv
import json
import math
import os
import sys
import time
import unicodedata
import urllib.error
import urllib.request
from collections import defaultdict, Counter
from datetime import date, datetime, timedelta, timezone


def _load_api_key(var="FOOTBALL_DATA_KEY", required=True):
    """Read a key from the environment, falling back to a local .env file.
    Parsed by hand rather than with python-dotenv to keep the script
    dependency-free. Never hardcode a key here: see .env.example."""
    val = os.environ.get(var)
    if val:
        return val
    here = os.path.dirname(os.path.abspath(__file__))
    for path in (os.path.join(here, ".env"), os.path.join(here, os.pardir, ".env")):
        try:
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line.startswith(var + "="):
                        return line.split("=", 1)[1].strip().strip("\"'")
        except OSError:
            continue
    if not required:
        return ""
    raise SystemExit(
        var + " is not set.\n"
        "  export " + var + "=your_key   (or: cp .env.example .env and edit it)\n"
        "  Free key: https://www.football-data.org/client/register"
    )


def current_season(today=None):
    """Starting year of the season in progress, as football-data.org keys seasons
    (2026 = 2026-27). From June the next season counts as current: the last one is
    over and the new one has no finished matches yet. Assumes an August-May season."""
    today = today or date.today()
    return today.year if today.month >= 6 else today.year - 1


API_KEY  = _load_api_key()
BASE_URL    = "https://api.football-data.org/v4"
COMPETITION = "PPL"          # Primeira Liga (free tier)

# Seasons follow today's date (current_season): SEASON is the last completed one and
# CURRENT the one in progress. To analyse an older season, pin it, e.g. SEASON = 2024.
SEASON      = current_season() - 1   # last completed season (starting year: 2025 = 2025-26)
CURRENT     = SEASON + 1             # season in progress: its finished matches are fitted too
DAYS_AHEAD  = 7              # fixture window in days; override with --days N
FETCH_LIMIT = 380            # a full 18-team season is 306 matches
MIN_MATCHES = 40             # below this the ratings are meaningless

# Home advantage is not a constant here: the season's own (decay-weighted) home and
# away goal averages already carry it, so it is applied exactly once. A hand-set
# HOME_ADV used to multiply on top and double-count it (docs/MODEL_CHANGES.md).
DECAY       = 0.0035         # time-decay, so late-season form outweighs August
RHO_DEFAULT = -0.13          # only a starting point; fit_rho found 0.0 on real data

# Promoted sides have no top-flight record to fit against. Attack below 1 means
# scores less than league average, defence above 1 means concedes more. Measured on
# football-data.co.uk 2019-20 to 2025-26 as first-season goals scored / conceded per
# game vs the league average (19 sides promoted to the Primeira Liga; was a judgment-call 0.80/1.20).
# A promoted side starts here and is shrunk toward a target that fades from this
# prior to league average as it plays (docs/MODEL_CHANGES.md).
PROMOTED_ATT = 0.78
PROMOTED_DEF = 1.08
PRIOR_GAMES  = 5      # the prior is worth about this many games of the side's own results
# Labels hand-typed FIXTURES with no data as promoted rather than misspelt. API
# fixtures need no list. Update it each summer if you type fixtures in by hand.
PROMOTED     = {"Maritimo", "Academico Viseu"}   # up from Liga Portugal 2

EXPORT_CSV   = f"liga_portugal_{SEASON}_{(SEASON + 1) % 100:02d}_matches.csv"

CORNER_BASE       = 5.0    # corners per team, so roughly 10 in an even game
CORNER_PROXY_BETA = 0.6    # damping when deriving corner form from goal form
HOME_CORNER_ADV   = 1.10   # home sides win slightly more corners
CORNER_DISPERSION = 1.25   # variance/mean for corner totals, re-estimated
                           # from data when real corner counts are available
CORNER_DATA       = []     # optional override: ("Benfica","Porto",7,4,"2026-05-01")

# Fixtures come from the API: every scheduled match in the next DAYS_AHEAD days
# (override with --days N). To predict specific games instead, list them here,
# e.g. ("Benfica", "FC Porto", "Sun 18 Oct 20:30"); names go through resolve().
FIXTURES = []


def api_get(endpoint, params=None):
    url = f"{BASE_URL}{endpoint}"
    if params:
        url += "?" + "&".join(f"{k}={v}" for k, v in params.items())
    req = urllib.request.Request(url, headers={"X-Auth-Token": API_KEY})
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        if e.code != 429:
            raise
        # Over the free tier's 10 requests a minute: wait for the counter to reset and
        # retry once, rather than silently fitting without a season.
        wait = int(e.headers.get("X-RequestCounter-Reset") or 60) + 1
        print(f"  [rate limit] waiting {wait}s...")
        time.sleep(wait)
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())


def fetch_season(competition, season, limit=FETCH_LIMIT):
    """Every finished match of one season, with every field the feed exposes.
    -> [{home, away, hg, ag, hth, hta, date, matchday}, ...]"""
    try:
        data = api_get(f"/competitions/{competition}/matches",
                       {"status": "FINISHED", "season": season, "limit": limit})
    except Exception as e:
        print(f"  [{competition} {season}] API error: {e}")
        return []
    out = []
    for m in data.get("matches", []):
        ft = m.get("score", {}).get("fullTime", {}) or {}
        ht = m.get("score", {}).get("halfTime", {}) or {}
        hg, ag = ft.get("home"), ft.get("away")
        if hg is None or ag is None:
            continue
        out.append({
            "home":     m["homeTeam"].get("shortName") or m["homeTeam"].get("name"),
            "away":     m["awayTeam"].get("shortName") or m["awayTeam"].get("name"),
            "hg":       int(hg),
            "ag":       int(ag),
            "hth":      ht.get("home"),
            "hta":      ht.get("away"),
            "date":     m.get("utcDate", ""),
            "matchday": m.get("matchday"),
        })
    out.sort(key=lambda x: x["date"])
    print(f"  [{competition} {season}-{season+1}] {len(out)} finished matches fetched.")
    return out


def days_ahead():
    """--days N on the command line, else DAYS_AHEAD."""
    if "--days" in sys.argv[1:-1]:
        return int(sys.argv[sys.argv.index("--days") + 1])
    return DAYS_AHEAD


def fetch_fixtures(competition, days):
    """Scheduled matches from today through `days` days ahead -> [(home, away,
    kickoff), ...], kickoff in local time. Names are the API's own, so they resolve
    exactly against the fitted data."""
    start = datetime.now(timezone.utc).date()
    try:
        data = api_get(f"/competitions/{competition}/matches",
                       {"dateFrom": start.isoformat(),
                        "dateTo": (start + timedelta(days=days)).isoformat()})
    except Exception as e:
        print(f"  [{competition} fixtures] API error: {e}")
        return []
    out = []
    for m in data.get("matches", []):
        home = m["homeTeam"].get("shortName") or m["homeTeam"].get("name")
        away = m["awayTeam"].get("shortName") or m["awayTeam"].get("name")
        if m.get("status") not in ("SCHEDULED", "TIMED") or not home or not away:
            continue
        ko = datetime.fromisoformat(m["utcDate"].replace("Z", "+00:00")).astimezone()
        out.append((home, away, ko.strftime("%a %d %b %H:%M")))
    print(f"  [{competition}] {len(out)} scheduled fixtures in the next {days} days.")
    return out


def export_csv(recs, path):
    if not recs:
        return
    cols = ["matchday", "date", "home", "away", "hg", "ag", "hth", "hta"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(recs)
    print(f"  Exported {len(recs)} match records -> {path}")


def goals_view(recs):
    return [(r["home"], r["away"], r["hg"], r["ag"], r["date"]) for r in recs]


def halftime_view(recs):
    return [(r["home"], r["away"], int(r["hth"]), int(r["hta"]), r["date"])
            for r in recs if r["hth"] is not None and r["hta"] is not None]


def _blank():
    return {"pld": 0, "w": 0, "d": 0, "l": 0, "gf": 0, "ga": 0,
            "hw": 0, "hd": 0, "hl": 0, "hgf": 0, "hga": 0,
            "aw": 0, "ad": 0, "al": 0, "agf": 0, "aga": 0,
            "cs": 0, "fts": 0, "btts": 0, "o25": 0}


def analyse_season(recs):
    """Per-team breakdown of every match played."""
    T = defaultdict(_blank)
    for r in recs:
        h, a, hg, ag = r["home"], r["away"], r["hg"], r["ag"]
        H, A = T[h], T[a]
        H["pld"] += 1; A["pld"] += 1
        H["gf"] += hg; H["ga"] += ag; H["hgf"] += hg; H["hga"] += ag
        A["gf"] += ag; A["ga"] += hg; A["agf"] += ag; A["aga"] += hg
        if hg > ag:
            H["w"] += 1; H["hw"] += 1; A["l"] += 1; A["al"] += 1
        elif ag > hg:
            A["w"] += 1; A["aw"] += 1; H["l"] += 1; H["hl"] += 1
        else:
            H["d"] += 1; H["hd"] += 1; A["d"] += 1; A["ad"] += 1
        if ag == 0: H["cs"]  += 1
        if hg == 0: A["cs"]  += 1
        if hg == 0: H["fts"] += 1
        if ag == 0: A["fts"] += 1
        if hg >= 1 and ag >= 1:
            H["btts"] += 1; A["btts"] += 1
        if hg + ag >= 3:
            H["o25"] += 1; A["o25"] += 1
    for t in T:
        T[t]["pts"] = T[t]["w"] * 3 + T[t]["d"]
        T[t]["gd"]  = T[t]["gf"] - T[t]["ga"]
    return dict(T)


def print_season_report(recs, T):
    n   = len(recs)
    hw  = sum(1 for r in recs if r["hg"] > r["ag"])
    dr  = sum(1 for r in recs if r["hg"] == r["ag"])
    aw  = n - hw - dr
    gh  = sum(r["hg"] for r in recs)
    ga  = sum(r["ag"] for r in recs)
    o15 = sum(1 for r in recs if r["hg"] + r["ag"] >= 2)
    o25 = sum(1 for r in recs if r["hg"] + r["ag"] >= 3)
    o35 = sum(1 for r in recs if r["hg"] + r["ag"] >= 4)
    bt  = sum(1 for r in recs if r["hg"] >= 1 and r["ag"] >= 1)
    hv  = [r for r in recs if r["hth"] is not None and r["hta"] is not None]
    pc  = lambda x: f"{x/n*100:5.1f}%"

    print("=" * 78)
    print(f"  STAGE 1 -- ANALYSIS OF ALL {n} MATCHES, {SEASON}-{SEASON+1}")
    print("=" * 78)
    print(f"  Result split : Home {pc(hw)}   Draw {pc(dr)}   Away {pc(aw)}")
    print(f"  Goals        : {(gh+ga)/n:.2f}/game   home {gh/n:.2f}  away {ga/n:.2f}"
          f"   (home/away ratio {gh/max(ga,1):.2f}, applied once as home advantage)")
    print(f"  Over/Under   : O1.5 {pc(o15)}   O2.5 {pc(o25)}   O3.5 {pc(o35)}")
    print(f"  BTTS         : Yes {pc(bt)}   No {pc(n-bt)}")
    if hv:
        hg1 = sum(r["hth"] + r["hta"] for r in hv)
        nil = sum(1 for r in hv if r["hth"] == 0 and r["hta"] == 0)
        print(f"  Half-time    : {hg1/len(hv):.2f} goals/game "
              f"({hg1/max(gh+ga,1)*100:.0f}% of all goals before the break)   "
              f"HT 0-0 {nil/len(hv)*100:.1f}%   [{len(hv)} matches with HT data]")
    sc = Counter((r["hg"], r["ag"]) for r in recs).most_common(6)
    print("  Top scorelines: " + ", ".join(f"{i}-{j} ({c/n*100:.1f}%)" for (i, j), c in sc))
    print()

    order = sorted(T, key=lambda t: (-T[t]["pts"], -T[t]["gd"], -T[t]["gf"]))
    print("-" * 78)
    print(f"  FINAL TABLE {SEASON}-{SEASON+1}  (reconstructed from the match record)")
    print("-" * 78)
    print(f"  {'#':>2}  {'TEAM':<22}{'P':>3}{'W':>4}{'D':>3}{'L':>4}"
          f"{'GF':>5}{'GA':>4}{'GD':>5}{'PTS':>5}")
    for i, t in enumerate(order, 1):
        s = T[t]
        print(f"  {i:>2}  {t:<22}{s['pld']:>3}{s['w']:>4}{s['d']:>3}{s['l']:>4}"
              f"{s['gf']:>5}{s['ga']:>4}{s['gd']:>+5}{s['pts']:>5}")
    print()

    print("-" * 78)
    print("  HOME / AWAY SPLITS AND PER-TEAM RATES")
    print("-" * 78)
    print(f"  {'TEAM':<22}{'HOME WDL':>10}{'AWAY WDL':>10}{'GF/g':>7}{'GA/g':>7}"
          f"{'CS':>4}{'FTS':>5}{'BTTS':>7}{'O2.5':>7}")
    for t in order:
        s = T[t]
        home_rec = f"{s['hw']}-{s['hd']}-{s['hl']}"
        away_rec = f"{s['aw']}-{s['ad']}-{s['al']}"
        print(f"  {t:<22}{home_rec:>10}{away_rec:>10}"
              f"{s['gf']/s['pld']:>7.2f}{s['ga']/s['pld']:>7.2f}"
              f"{s['cs']:>4}{s['fts']:>5}"
              f"{s['btts']/s['pld']*100:>6.0f}%{s['o25']/s['pld']*100:>6.0f}%")
    print()


# Fixture spellings -> API names. Accents are stripped before comparison, so
# "Vitoria" and "Vitória" match either way round.
STOPWORDS = {"fc", "sc", "cf", "ac", "cd", "ad", "sad", "gd", "cs", "cp",
             "de", "da", "do", "dos", "das", "clube", "futebol"}


def _tokens(name):
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii").lower()
    s = "".join(ch if ch.isalnum() else " " for ch in s)
    return {t for t in s.split() if t and t not in STOPWORDS}


def resolve(name, data_teams, exact_only=False):
    """'Vitoria de Guimaraes' -> 'Vitoria SC', 'Famalicao' -> 'Famalicao'.
    exact_only: the names come from the API, so one with no exact match is a side
    with no match yet (promoted), never a spelling to guess by shared words."""
    if name in data_teams:      # API-sourced fixtures use the data's own names
        return name
    if exact_only:
        return None
    q = _tokens(name)
    best, best_score = None, 0
    for t in data_teams:
        score = len(q & _tokens(t))
        if score > best_score:
            best, best_score = t, score
        elif score == best_score and score > 0 and best is not None:
            if len(_tokens(t)) < len(_tokens(best)):
                best = t
    return best if best_score >= 1 else None


def auto_shrinkage(n_matches):
    if   n_matches < 100:  return 0.12
    elif n_matches < 200:  return 0.08
    elif n_matches < 300:  return 0.05
    else:                  return 0.03


def fit_team_ratings(matches, home_advantage=1.0, n_iter=300, lr=0.01,
                     decay=DECAY, shrinkage=None, label="", priors=None):
    """matches: [(home, away, home_count, away_count, date), ...]
    Counts are goals, half-time goals or corners -- same machinery either way.
    priors: {team: (attack, defence)} for promoted sides. Such a side starts at its
    prior and is shrunk toward a target fading from it to 1.0 over PRIOR_GAMES games;
    everyone else is shrunk toward 1.0 as before."""
    if shrinkage is None:
        shrinkage = auto_shrinkage(len(matches))
        print(f"  {label}shrinkage {shrinkage} on {len(matches)} matches")
    teams = set()
    for h, a, _, _, _ in matches:
        teams.add(h); teams.add(a)
    ms = sorted(matches, key=lambda x: x[4])
    n = len(ms)
    weights = [math.exp(-decay * (n - 1 - i)) for i in range(n)]
    avg_home = sum(w * hg for (_, _, hg, _, _), w in zip(ms, weights)) / sum(weights)
    avg_away = sum(w * ag for (_, _, _, ag, _), w in zip(ms, weights)) / sum(weights)
    team_matches = defaultdict(float)
    for (h, a, _, _, _), w in zip(ms, weights):
        team_matches[h] += w; team_matches[a] += w
    priors = priors or {}
    fade = {t: PRIOR_GAMES / (PRIOR_GAMES + team_matches[t]) for t in teams}
    a_tgt = {t: 1 + (priors.get(t, (1.0, 1.0))[0] - 1) * fade[t] for t in teams}
    d_tgt = {t: 1 + (priors.get(t, (1.0, 1.0))[1] - 1) * fade[t] for t in teams}
    attack, defence = dict(a_tgt), dict(d_tgt)
    for _ in range(n_iter):
        att_num = defaultdict(float); att_den = defaultdict(float)
        def_num = defaultdict(float); def_den = defaultdict(float)
        for (h, a, hg, ag, _), w in zip(ms, weights):
            lam_h = avg_home * attack[h] * defence[a] * home_advantage
            lam_a = avg_away * attack[a] * defence[h]
            att_num[h] += w * hg;  att_den[h] += w * lam_h / attack[h]
            att_num[a] += w * ag;  att_den[a] += w * lam_a / attack[a]
            def_num[a] += w * hg;  def_den[a] += w * lam_h / defence[a]
            def_num[h] += w * ag;  def_den[h] += w * lam_a / defence[h]
        for t in teams:
            shrink_t = shrinkage * (20 / (team_matches[t] + 20))
            # One pseudo-goal at the expected rate: (0/x)**lr is 0 for any lr, so a side on
            # 0 goals (e.g. promoted, one game in) would collapse (docs/MODEL_CHANGES.md).
            if att_den[t] > 0:
                attack[t]  = attack[t] * (((att_num[t] + 1) / (att_den[t] + 1)) ** lr)
                attack[t]  = attack[t] * (1 - shrink_t) + shrink_t * a_tgt[t]
            if def_den[t] > 0:
                defence[t] = defence[t] * (((def_num[t] + 1) / (def_den[t] + 1)) ** lr)
                defence[t] = defence[t] * (1 - shrink_t) + shrink_t * d_tgt[t]
        att_mean = math.exp(sum(math.log(v) for v in attack.values())  / len(teams))
        def_mean = math.exp(sum(math.log(v) for v in defence.values()) / len(teams))
        attack  = {t: v / att_mean for t, v in attack.items()}
        defence = {t: v / def_mean for t, v in defence.items()}
    return attack, defence, avg_home, avg_away


def fit_rho(matches, attack, defence, avg_home, avg_away, home_advantage):
    best_rho, best_ll = RHO_DEFAULT, float("-inf")
    for rho_test in [-0.25, -0.20, -0.18, -0.15, -0.13, -0.10, -0.08, -0.05, 0.0, 0.05]:
        ll = 0.0
        for h, a, hg, ag, _ in matches:
            lh = avg_home * attack.get(h, 1.0) * defence.get(a, 1.0) * home_advantage
            la = avg_away * attack.get(a, 1.0) * defence.get(h, 1.0)
            p = (poisson_pmf(hg, lh) * poisson_pmf(ag, la)
                 * dixon_coles_tau(hg, ag, lh, la, rho_test))
            if p > 0:
                ll += math.log(p)
        if ll > best_ll:
            best_ll, best_rho = ll, rho_test
    return best_rho


def calibrate_probability(p, strength=0.08):
    return p * (1 - strength) + 0.5 * strength


def poisson_pmf(k, lam):
    if lam <= 0:
        return 1.0 if k == 0 else 0.0
    return math.exp(-lam) * (lam ** k) / math.factorial(k)


def nb_pmf(k, mu, phi):
    """Negative binomial by variance/mean ratio phi. phi<=1 -> Poisson.
    Corner counts are over-dispersed, so this widens the tails correctly."""
    if mu <= 0:
        return 1.0 if k == 0 else 0.0
    if phi <= 1.0 + 1e-9:
        return poisson_pmf(k, mu)
    r = mu / (phi - 1.0)
    p = r / (r + mu)
    return math.exp(math.lgamma(k + r) - math.lgamma(r) - math.lgamma(k + 1.0)
                    + r * math.log(p) + k * math.log(1.0 - p))


def dixon_coles_tau(i, j, lh, la, rho=RHO_DEFAULT):
    if   i == 0 and j == 0: return 1 - lh * la * rho
    elif i == 1 and j == 0: return 1 + la * rho
    elif i == 0 and j == 1: return 1 + lh * rho
    elif i == 1 and j == 1: return 1 - rho
    else:                   return 1.0


def build_score_matrix(lh, la, rho=RHO_DEFAULT, max_goals=7):
    ph = [poisson_pmf(i, lh) for i in range(max_goals + 1)]
    pa = [poisson_pmf(j, la) for j in range(max_goals + 1)]
    m = [[ph[i] * pa[j] * dixon_coles_tau(i, j, lh, la, rho)
          for j in range(max_goals + 1)] for i in range(max_goals + 1)]
    tot = sum(m[i][j] for i in range(max_goals + 1) for j in range(max_goals + 1))
    return [[v / tot for v in row] for row in m]


def summarize_matrix(matrix, calibrate=True):
    n = len(matrix)
    s = dict.fromkeys(["home", "draw", "away", "over_1_5", "over_2_5",
                       "over_3_5", "btts_yes", "btts_no"], 0.0)
    for i in range(n):
        for j in range(n):
            p = matrix[i][j]; g = i + j
            if   i > j:  s["home"] += p
            elif i == j: s["draw"] += p
            else:        s["away"] += p
            if g >= 2: s["over_1_5"] += p
            if g >= 3: s["over_2_5"] += p
            if g >= 4: s["over_3_5"] += p
            if i >= 1 and j >= 1: s["btts_yes"] += p
            else:                 s["btts_no"]  += p
    if calibrate:
        for k in ["home", "draw", "away"]:
            s[k] = calibrate_probability(s[k])
        t = s["home"] + s["draw"] + s["away"]
        if t > 0:
            s["home"] /= t; s["draw"] /= t; s["away"] /= t
    return s


def prob_to_odds(p):
    return f"{1/p:.2f}" if p > 0.0001 else "inf"


def most_likely_score(matrix):
    n = len(matrix)
    best_p, best = 0.0, (0, 0)
    for i in range(n):
        for j in range(n):
            if matrix[i][j] > best_p:
                best_p, best = matrix[i][j], (i, j)
    return best, best_p


def confidence_flag(lh, la, promoted):
    if promoted:             return "PRIOR-BASED -- promoted, no top-flight data"
    if lh > 3.5 or la > 3.5: return "OVERFITTED -- goals markets only"
    if lh / max(la, 0.01) > 4 or la / max(lh, 0.01) > 4:
        return "EXTREME RATIO -- result markets unreliable"
    if lh + la > 4.5:        return "HIGH-VARIANCE -- goals likely, result uncertain"
    return "RELIABLE"


def print_prediction(home, away, lh, la, ht=None, kickoff=None,
                     rho=RHO_DEFAULT, promoted=False):
    matrix = build_score_matrix(lh, la, rho)
    s = summarize_matrix(matrix)
    ml, mlp = most_likely_score(matrix)
    print("=" * 78)
    print(f"  [Liga Portugal] [{kickoff}]")
    print(f"  {home}  vs  {away}")
    print(f"  Confidence: {confidence_flag(lh, la, promoted)}")
    print(f"  xG  {home}: {lh:.2f}   {away}: {la:.2f}   (rho={rho})")
    print("-" * 78)
    print(f"  1X2 :  Home {s['home']*100:5.1f}% ({prob_to_odds(s['home'])})  "
          f"Draw {s['draw']*100:5.1f}% ({prob_to_odds(s['draw'])})  "
          f"Away {s['away']*100:5.1f}% ({prob_to_odds(s['away'])})")
    print(f"  O/U :  O1.5 {s['over_1_5']*100:5.1f}%  O2.5 {s['over_2_5']*100:5.1f}%  "
          f"O3.5 {s['over_3_5']*100:5.1f}%")
    print(f"  BTTS:  Yes {s['btts_yes']*100:5.1f}%   No {s['btts_no']*100:5.1f}%")
    print(f"  Best score: {ml[0]}-{ml[1]}  ({mlp*100:.1f}%)")
    print(f"  WDW home: {(s['home']+s['draw'])*100:.1f}%   "
          f"WDW away: {(s['away']+s['draw'])*100:.1f}%")
    if ht:
        lhh, lah = ht
        hm = build_score_matrix(lhh, lah, 0.0, max_goals=5)   # rho=0 at HT
        hs = summarize_matrix(hm, calibrate=True)
        print(f"  HT  :  xG {lhh:.2f}/{lah:.2f}   Home {hs['home']*100:4.1f}%  "
              f"Draw {hs['draw']*100:4.1f}%  Away {hs['away']*100:4.1f}%   "
              f"O0.5 {(1-hm[0][0])*100:4.1f}%  O1.5 {hs['over_1_5']*100:4.1f}%")
    print()


def print_corner_prediction(home, away, lh, la, phi, kickoff=None, source=""):
    # Head-to-head ("most corners") needs the joint grid -> independent Poisson.
    matrix = build_score_matrix(lh, la, 0.0, max_goals=18)
    n = len(matrix)
    hm = tie = am = 0.0
    for i in range(n):
        for j in range(n):
            p = matrix[i][j]
            if   i > j:  hm  += p
            elif i == j: tie += p
            else:        am  += p
    # Totals and team totals -> negative binomial (corners are over-dispersed).
    mu  = lh + la
    tot = [nb_pmf(k, mu, phi) for k in range(41)]
    ou  = {L: sum(p for k, p in enumerate(tot) if k > L)
           for L in [7.5, 8.5, 9.5, 10.5, 11.5, 12.5]}
    hk, ak = max(1, int(lh)), max(1, int(la))
    p_h = sum(nb_pmf(k, lh, phi) for k in range(hk, 41))
    p_a = sum(nb_pmf(k, la, phi) for k in range(ak, 41))
    top = sorted(enumerate(tot), key=lambda x: -x[1])[:3]
    od  = lambda p: f"{1/p:.2f}" if p > 0.0001 else "inf"
    print("=" * 78)
    print(f"  [Liga Portugal] [{kickoff}]   CORNERS   ({source})")
    print(f"  {home}  vs  {away}")
    print(f"  xCorners  {home}: {lh:.1f}   {away}: {la:.1f}   total: {mu:.1f}"
          f"   (dispersion phi={phi:.2f})")
    print("-" * 78)
    print(f"  Totals:  O8.5 {ou[8.5]*100:4.1f}% ({od(ou[8.5])})   "
          f"O9.5 {ou[9.5]*100:4.1f}% ({od(ou[9.5])})   "
          f"O10.5 {ou[10.5]*100:4.1f}% ({od(ou[10.5])})   "
          f"O11.5 {ou[11.5]*100:4.1f}% ({od(ou[11.5])})")
    print(f"  Most corners:  {home} {hm*100:4.1f}%   Tie {tie*100:4.1f}%   "
          f"{away} {am*100:4.1f}%")
    print(f"  Team totals:  {home} over {hk-0.5:.1f}: {p_h*100:4.1f}% ({od(p_h)})   "
          f"{away} over {ak-0.5:.1f}: {p_a*100:4.1f}% ({od(p_a)})")
    print("  Most likely total: " + ", ".join(f"{k} ({p*100:.0f}%)" for k, p in top))
    print()


def print_ratings(att, dff, avg_h, avg_a, rho):
    print("-" * 78)
    print(f"  FITTED DIXON-COLES RATINGS  (decay-weighted mean: "
          f"home {avg_h:.2f} / away {avg_a:.2f}, rho={rho})")
    print("-" * 78)
    print(f"  {'TEAM':<24}{'ATT':>8}{'DEF':>8}{'NET':>8}")
    for t in sorted(att, key=lambda t: -(att[t] / max(dff[t], 0.01))):
        print(f"  {t:<24}{att[t]:>8.3f}{dff[t]:>8.3f}{att[t]/max(dff[t],0.01):>8.2f}")
    print()


def fetch_corner_matches(competition, season, limit=FETCH_LIMIT):
    """Corners from football-data.org. Returns [] on the free tier, which is
    scores only, and real counts if the paid statistics add-on is on the
    account. The field path could not be tested without the add-on, so if this
    comes back empty on a paid plan, print a single match detail with
    api_get(f'/matches/{id}') and adjust the keys below."""
    try:
        data = api_get(f"/competitions/{competition}/matches",
                       {"status": "FINISHED", "season": season, "limit": limit})
    except Exception as e:
        print(f"  [corners {season}] API error: {e}")
        return []
    out = []
    for m in data.get("matches", []):
        stats = m.get("statistics")
        if not isinstance(stats, dict):
            continue
        hs  = stats.get("homeTeam") or stats.get("home") or {}
        as_ = stats.get("awayTeam") or stats.get("away") or {}
        hc = hs.get("corners") if isinstance(hs, dict) else None
        ac = as_.get("corners") if isinstance(as_, dict) else None
        if hc is None or ac is None:
            continue
        out.append((m["homeTeam"].get("shortName") or m["homeTeam"].get("name"),
                    m["awayTeam"].get("shortName") or m["awayTeam"].get("name"),
                    int(hc), int(ac), m.get("utcDate", "")))
    return out


def corner_ratings(competition, season, att, dff):
    """-> (catt, cconc, avg_hc, avg_ac, phi, source, home_mult). Fitted corner averages
    already carry the home edge (home_mult 1.0); the proxy's neutral CORNER_BASE gets
    HOME_CORNER_ADV once."""
    cm = list(CORNER_DATA) or fetch_corner_matches(competition, season)
    if len(cm) >= 10:
        catt, cconc, avg_hc, avg_ac = fit_team_ratings(
            cm, label="corners: ")
        totals = [h + a for _, _, h, a, _ in cm]
        mean = sum(totals) / len(totals)
        var  = sum((t - mean) ** 2 for t in totals) / max(len(totals) - 1, 1)
        phi  = min(max(var / max(mean, 0.01), 1.0), 2.0)
        return catt, cconc, avg_hc, avg_ac, phi, f"fitted on {len(cm)} corner matches", 1.0
    # No corner data -> derive corner form from the fitted GOAL ratings.
    catt  = {t: att[t] ** CORNER_PROXY_BETA for t in att}
    cconc = {t: dff[t] ** CORNER_PROXY_BETA for t in dff}
    return (catt, cconc, CORNER_BASE, CORNER_BASE, CORNER_DISPERSION,
            "GOAL-RATING PROXY -- no corner data on this plan", HOME_CORNER_ADV)


def main():
    print("=" * 78)
    days = days_ahead()
    print(f"  LIGA PORTUGAL  |  analyse {SEASON}-{SEASON+1} in full, fit it plus "
          f"{CURRENT}-{CURRENT+1} so far, predict the next {days} days")
    print("=" * 78)

    # ---------------- STAGE 1: analyse every match of last season -------------
    recs = fetch_season(COMPETITION, SEASON)
    if len(recs) < MIN_MATCHES:
        print(f"\n  Only {len(recs)} matches -- below MIN_MATCHES={MIN_MATCHES}.")
        print("  Ratings would be meaningless. Check the season param / API plan.")
        return
    export_csv(recs, EXPORT_CSV)
    table = analyse_season(recs)
    print_season_report(recs, table)

    # ---------------- STAGE 2: fit last season + this one so far, and apply -------
    cur = fetch_season(COMPETITION, CURRENT)
    fit_recs = recs + cur      # time-decay weights this season's matches most
    # Promoted: in this season's data but not last season's (no extra API call).
    last_teams = {r["home"] for r in recs} | {r["away"] for r in recs}
    promoted = sorted(({r["home"] for r in cur} | {r["away"] for r in cur}) - last_teams)
    priors = {t: (PROMOTED_ATT, PROMOTED_DEF) for t in promoted}
    print(f"  Promoted sides (prior {PROMOTED_ATT}/{PROMOTED_DEF}, fading over "
          f"{PRIOR_GAMES} games): {promoted or 'none with matches yet'}")
    gv = goals_view(fit_recs)
    att, dff, avg_h, avg_a = fit_team_ratings(gv, label="goals: ", priors=priors)
    rho = fit_rho(gv, att, dff, avg_h, avg_a, 1.0)
    print_ratings(att, dff, avg_h, avg_a, rho)

    hv = halftime_view(fit_recs)
    ht_ok = len(hv) >= MIN_MATCHES
    if ht_ok:
        h_att, h_dff, h_avg_h, h_avg_a = fit_team_ratings(hv, label="half-time: ", priors=priors)
    else:
        print(f"  Half-time scores on only {len(hv)} matches -> HT markets skipped.\n")

    fixtures = FIXTURES or fetch_fixtures(COMPETITION, days)
    if not fixtures:
        print(f"  No scheduled {COMPETITION} fixtures in the next {days} days "
              "(international break?). Try --days 14.")
        return
    data_teams = sorted({r["home"] for r in fit_recs} | {r["away"] for r in fit_recs})
    names = {t for f in fixtures for t in (f[0], f[1])}
    name_map, missing = {}, []
    for nm in names:
        hit = resolve(nm, data_teams, exact_only=not FIXTURES)
        if hit:
            name_map[nm] = hit
        else:
            missing.append(nm)
    print("  NAME RESOLUTION")
    for nm in sorted(name_map):
        if _tokens(nm) != _tokens(name_map[nm]):
            print(f"    {nm:<24} -> {name_map[nm]}")
    for nm in sorted(missing):
        tag = ("promoted, no match yet" if nm in PROMOTED or not FIXTURES
               else "NOT FOUND -- check spelling!")
        print(f"    {nm:<24} -> no data in {SEASON}-{SEASON+1} or {CURRENT}-{CURRENT+1} ({tag})")
    print()

    def ratings_for(name, a_tbl, d_tbl):
        key = name_map.get(name)
        if key is None:
            return PROMOTED_ATT, PROMOTED_DEF, True
        return a_tbl.get(key, PROMOTED_ATT), d_tbl.get(key, PROMOTED_DEF), False

    def lam(home, away, a_tbl, d_tbl, base_h, base_a):
        h_att, h_def, h_new = ratings_for(home, a_tbl, d_tbl)
        a_att, a_def, a_new = ratings_for(away, a_tbl, d_tbl)
        return (round(base_h * h_att * a_def, 3),
                round(base_a * a_att * h_def, 3),
                h_new or a_new)

    print("=" * 78)
    print("  STAGE 2 -- PREDICTIONS: GOALS")
    print("=" * 78)
    for home, away, ko in fixtures:
        lh, la, is_new = lam(home, away, att, dff, avg_h, avg_a)
        ht = None
        if ht_ok:
            hlh, hla, _ = lam(home, away, h_att, h_dff, h_avg_h, h_avg_a)
            ht = (hlh, hla)
        print_prediction(home, away, lh, la, ht=ht, kickoff=ko,
                         rho=rho, promoted=is_new)

    catt, cconc, c_h, c_a, phi, csrc, c_ha = corner_ratings(COMPETITION, SEASON, att, dff)
    print("=" * 78)
    print(f"  STAGE 2 -- PREDICTIONS: CORNERS   ({csrc})")
    print("=" * 78)
    pa_c, pd_c = PROMOTED_ATT ** CORNER_PROXY_BETA, PROMOTED_DEF ** CORNER_PROXY_BETA
    for home, away, ko in fixtures:
        h_ca, h_cd, h_new = ratings_for(home, catt, cconc)
        a_ca, a_cd, a_new = ratings_for(away, catt, cconc)
        if h_new: h_ca, h_cd = pa_c, pd_c
        if a_new: a_ca, a_cd = pa_c, pd_c
        lhc = round(c_h * h_ca * a_cd * c_ha, 3)
        lac = round(c_a * a_ca * h_cd, 3)
        print_corner_prediction(home, away, lhc, lac, phi, kickoff=ko, source=csrc)


if __name__ == "__main__":
    main()
