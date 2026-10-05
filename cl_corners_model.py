"""Champions League and Championship, goals and corners.

A cut-down variant for knockout and cup competitions, where a full league
season's worth of matches does not exist. Deliberately simpler than the league
scripts: no time-decay, no half-time model, and a plain Poisson tail on corner
totals rather than a negative binomial. It also prints a small correct-score
grid, which the league scripts do not.

Small samples mean the ratings here overfit more than the league versions do.
Treat the goals markets as more trustworthy than the result markets. Since
2026-10-04 (docs/MODEL_CHANGES.md): ratings use the league scripts' sample-size
shrinkage toward average and a 0.1 floor. Without them, one matchday of CL data
crashed the fit (sides on 0 goals) or, floored only, priced PSG at 96% against
Liverpool. Home advantage comes from the competition's own home/away goal
split, applied once. Both competitions are now fitted the way the league
scripts are (previous season plus current, time-decayed, fitted rho): for the CL
this was the largest single gain found (docs/MODEL_CHANGES.md). fit_team_ratings
above is kept for corner counts only.
"""

import math
import os
import time
import unicodedata
import urllib.error
import urllib.request
import json
import sys
from collections import defaultdict
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


API_KEY  = _load_api_key()
BASE_URL = "https://api.football-data.org/v4"

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
        # Over the free tier's 10 requests a minute (a run makes 9-10): wait for the
        # counter to reset and retry once, rather than silently fitting without a season.
        wait = int(e.headers.get("X-RequestCounter-Reset") or 60) + 1
        print(f"  [rate limit] waiting {wait}s...")
        time.sleep(wait)
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())


def current_season(today=None):
    """Starting year of the season in progress, as football-data.org keys seasons
    (2026 = 2026-27). From June the next season counts as current: the last one is
    over and the new one has no finished matches yet. Assumes an August-May season."""
    today = today or date.today()
    return today.year if today.month >= 6 else today.year - 1


DAYS_AHEAD = 7   # fixture window in days; override with --days N

# Fixtures come from the API: every scheduled match in the next DAYS_AHEAD days. To
# predict specific games instead, list them as (home, away, kickoff, note), e.g.
# ("Real Madrid", "Bayern München", "Tue 20:00", "QF leg 1"); names go through resolve().
CL_FIXTURES  = []
ELC_FIXTURES = []

# The Championship is a league, so it is fitted the way the league scripts are: the
# previous season plus the current one so far, time-decayed, with a fitted rho. In the
# club backtest this beat a current-season-only fit (docs/MODEL_CHANGES.md).
ELC_SEASON       = current_season()   # season in progress (starting year), from today's date
ELC_DECAY        = 0.0035
# Priors for sides new to the division: their ratings start here and are shrunk back
# toward them (not toward average) until their own results take over. Measured on
# football-data.co.uk 2019-20 to 2025-26 as first-season goals scored / conceded per
# game relative to the division average, 21 sides each way (docs/MODEL_CHANGES.md).
ELC_RELEGATED_ATT, ELC_RELEGATED_DEF = 1.18, 0.79   # down from the Premier League
ELC_PROMOTED_ATT,  ELC_PROMOTED_DEF  = 0.88, 1.10   # up from League One
PRIOR_GAMES = 5   # a newcomer's prior fades as it plays, worth about this many games (ELC and CL)

# The CL is fitted on the previous CL_HISTORY seasons plus the current one so far,
# time-decayed like the Championship. Several previous seasons rather than one let
# clubs that qualify regularly but missed a season or two (Porto, Feyenoord, Shakhtar)
# keep a rating from their own CL results instead of a newcomer prior. A newcomer is a
# side with no CL match in that window. Newcomers from outside the big five leagues
# start with a weak prior that fades like the ELC ones: first-season attack 0.63 /
# defence 1.24 vs the CL average, 16 such sides (new against the season before) over
# 2024-25 and 2025-26 and stable across both. Newcomers from the big five stay
# average: a prior for them was not reliably better (docs/MODEL_CHANGES.md). Each team's
# country comes from the CL team list.
CL_SEASON = current_season()   # season in progress (starting year), from today's date
CL_HISTORY = 3       # previous CL seasons fitted (the free tier serves 2023-24 onward)
CL_BIG5 = {"England", "Spain", "Germany", "Italy", "France"}
CL_LEAGUE_COUNTRY = {"Monaco": "France"}     # AS Monaco plays in Ligue 1
CL_NEWCOMER_ATT, CL_NEWCOMER_DEF = 0.63, 1.24
# Fallback home/away averages if no CL data can be fetched: the API's 2023-24 to
# 2025-26 seasons (503 matches). With several seasons of data they are not otherwise used.
CL_PRIOR_HOME, CL_PRIOR_AWAY = 1.90, 1.44


def days_ahead():
    """--days N on the command line, else DAYS_AHEAD."""
    if "--days" in sys.argv[1:-1]:
        return int(sys.argv[sys.argv.index("--days") + 1])
    return DAYS_AHEAD


def fetch_fixtures(competitions, days):
    """Scheduled matches from today through `days` days ahead -> {competition:
    [(home, away, kickoff, note), ...]}, kickoff in local time, note = stage and
    matchday. All competitions come in one request per 10 days (the cross-competition
    endpoint's longest date range), which keeps a run within the free tier's 10
    requests a minute. That endpoint's dateTo is exclusive, so chunks run [lo, hi)."""
    start = datetime.now(timezone.utc).date()
    end = start + timedelta(days=days + 1)
    raw, lo = [], start
    while lo < end:
        hi = min(end, lo + timedelta(days=10))
        try:
            raw += api_get("/matches", {"competitions": ",".join(competitions),
                                        "dateFrom": lo.isoformat(),
                                        "dateTo": hi.isoformat()}).get("matches", [])
        except Exception as e:
            print(f"  [fixtures {lo} to {hi}] API error: {e}")
        lo = hi
    out = {c: [] for c in competitions}
    for m in sorted(raw, key=lambda m: (m["utcDate"], m["homeTeam"].get("name") or "")):
        code = (m.get("competition") or {}).get("code")
        home = m["homeTeam"].get("shortName") or m["homeTeam"].get("name")
        away = m["awayTeam"].get("shortName") or m["awayTeam"].get("name")
        if code not in out or m.get("status") not in ("SCHEDULED", "TIMED") or not home or not away:
            continue   # knockout ties not yet drawn have no teams
        ko = datetime.fromisoformat(m["utcDate"].replace("Z", "+00:00")).astimezone()
        stage = (m.get("stage") or "").replace("_", " ").capitalize()
        note = " ".join(x for x in (stage, f"MD{m['matchday']}" if m.get("matchday") else "") if x)
        out[code].append((home, away, ko.strftime("%a %d %b %H:%M"), note or None))
    for c in competitions:
        print(f"  [{c}] {len(out[c])} scheduled fixtures in the next {days} days.")
    return out


_RAW_MATCHES = {}   # (competition, season) -> the API's match list, reused for corners


def fetch_season(competition, season, limit=600):
    """Finished matches of one season -> [(home, away, hg, ag, utcDate), ...] by date."""
    try:
        data = api_get(f"/competitions/{competition}/matches",
                       {"status": "FINISHED", "season": season, "limit": limit})
    except Exception as e:
        print(f"  [{competition} {season}] API error: {e}")
        return []
    _RAW_MATCHES[(competition, season)] = data.get("matches", [])
    out = []
    for m in data.get("matches", []):
        hg, ag = m["score"]["fullTime"]["home"], m["score"]["fullTime"]["away"]
        if hg is None or ag is None:
            continue
        out.append((m["homeTeam"]["shortName"], m["awayTeam"]["shortName"], int(hg), int(ag),
                    m.get("utcDate", "")))
    out.sort(key=lambda x: x[4])
    print(f"  [{competition} {season}-{season+1}] Fetched {len(out)} finished matches.")
    return out


# Fixture spelling -> the API's shortName, which uses its own short forms ('Barça',
# 'Atleti', 'Bayern'). An unmatched name would silently get a neutral 1.0 rating.
ALIASES = {
    "Bayern München":      ["Bayern"],
    "Bayern Munich":       ["Bayern"],
    "Barcelona":           ["Barça"],
    "Atletico Madrid":     ["Atleti"],
    "Paris Saint-Germain": ["PSG"],
    "Inter Milan":         ["Inter"],
    "Borussia Dortmund":   ["Dortmund"],
    "Manchester City":     ["Man City"],
    "Manchester United":   ["Man United"],
    "Shakhtar Donetsk":    ["Shaktar"],
    "Slovan Bratislava":   ["Sl. Bratislava"],
    "AEK Athens":          ["PAE AEK"],
    "Bodo/Glimt":          ["Bodø/Glimt"],   # 'ø' has no ASCII decomposition, so _flat drops it
}


def _flat(s):
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii").lower()
    return "".join(ch for ch in s if ch.isalnum())


def resolve(name, data_teams):
    """Fixture name -> the API's name, or None. Exact, accent/punctuation-insensitive
    or ALIASES matches only; no token guessing (see pl_model.py for why)."""
    flat = {_flat(t): t for t in data_teams}
    for cand in [name] + ALIASES.get(name, []):
        hit = flat.get(_flat(cand))
        if hit:
            return hit
    return None


def resolve_fixtures(label, names, matches):
    """-> {fixture name: data name}. Unmatched names keep their spelling, so they get
    a neutral 1.0 rating, but are flagged rather than passed through silently."""
    data_teams = {t for m in matches for t in m[:2]}
    out = {}
    for nm in sorted(set(names)):
        hit = resolve(nm, data_teams)
        out[nm] = hit or nm
        if hit and hit != nm:
            print(f"  [{label}] {nm} -> {hit}")
        elif not hit and data_teams:
            print(f"  [{label}] [warn] '{nm}' not in this season's data -> neutral 1.0 rating")
    return out


def auto_shrinkage(n_matches):
    """Same schedule as the league scripts: pull harder toward average when data is thin."""
    if   n_matches < 100:  return 0.12
    elif n_matches < 200:  return 0.08
    elif n_matches < 300:  return 0.05
    else:                  return 0.03


def fit_team_ratings(matches, home_advantage=1.0, n_iter=200, lr=0.01):
    """Shared by goals and corners: a corner record is just
    (home, away, home_count, away_count), the same shape as a scoreline."""
    teams = set()
    games = defaultdict(int)
    for h, a, _, _ in matches:
        teams.add(h); teams.add(a)
        games[h] += 1; games[a] += 1
    shrinkage = auto_shrinkage(len(matches))

    attack  = {t: 1.0 for t in teams}
    defence = {t: 1.0 for t in teams}
    avg_home = sum(hg for _, _, hg, _ in matches) / len(matches)
    avg_away = sum(ag for _, _, _, ag in matches) / len(matches)

    for _ in range(n_iter):
        att_num = defaultdict(float); att_den = defaultdict(float)
        def_num = defaultdict(float); def_den = defaultdict(float)

        for h, a, hg, ag in matches:
            lam_h = avg_home * attack[h] * defence[a] * home_advantage
            lam_a = avg_away * attack[a] * defence[h]
            att_num[h] += hg;  att_den[h] += lam_h / attack[h]
            att_num[a] += ag;  att_den[a] += lam_a / attack[a]
            def_num[a] += hg;  def_den[a] += lam_h / defence[a]
            def_num[h] += ag;  def_den[h] += lam_a / defence[h]

        for t in teams:
            shrink_t = shrinkage * (20 / (games[t] + 20))
            # One pseudo-goal at the expected rate in each ratio: (0/x)**lr is 0 whatever
            # lr is, so without it a side on 0 goals after one match collapses to the floor
            # and distorts everyone else's normalised rating. Negligible over a full season.
            if att_den[t] > 0:
                attack[t]  = attack[t] * (((att_num[t] + 1) / (att_den[t] + 1)) ** lr)
                attack[t]  = attack[t] * (1 - shrink_t) + shrink_t
            if def_den[t] > 0:
                defence[t] = defence[t] * (((def_num[t] + 1) / (def_den[t] + 1)) ** lr)
                defence[t] = defence[t] * (1 - shrink_t) + shrink_t
            # Floor: a side on 0 goals scored (or conceded) must never reach 0 and crash log().
            attack[t] = max(attack[t], 0.1); defence[t] = max(defence[t], 0.1)

        att_mean = math.exp(sum(math.log(v) for v in attack.values())  / len(teams))
        def_mean = math.exp(sum(math.log(v) for v in defence.values()) / len(teams))
        attack  = {t: v / att_mean for t, v in attack.items()}
        defence = {t: v / def_mean for t, v in defence.items()}

    return attack, defence, avg_home, avg_away


def fit_league_ratings(matches, priors=None, n_iter=300, lr=0.01, decay=ELC_DECAY):
    """The league scripts' fit (pl_model.py), for the Championship: matches are
    (home, away, hg, ag, date) over two seasons, time-decayed by match order, shrunk
    by sample size, with one pseudo-goal per ratio. Home advantage comes from the
    weighted home/away averages, applied once. `priors` maps a team to (attack,
    defence): it starts there and is shrunk toward a target that slides from the
    prior to 1.0 as the team plays (worth PRIOR_GAMES games). A prior that never
    fades kept dragging newcomers back all season and cost accuracy later on."""
    teams = set()
    for h, a, _, _, _ in matches:
        teams.add(h); teams.add(a)
    shrinkage = auto_shrinkage(len(matches))
    priors = priors or {}
    ms = sorted(matches, key=lambda x: x[4])
    n = len(ms)
    weights = [math.exp(-decay * (n - 1 - i)) for i in range(n)]
    avg_home = sum(w * hg for (_, _, hg, _, _), w in zip(ms, weights)) / sum(weights)
    avg_away = sum(w * ag for (_, _, _, ag, _), w in zip(ms, weights)) / sum(weights)
    team_matches = defaultdict(float)
    for (h, a, _, _, _), w in zip(ms, weights):
        team_matches[h] += w; team_matches[a] += w
    fade = {t: PRIOR_GAMES / (PRIOR_GAMES + team_matches[t]) for t in teams}
    a_prior = {t: 1 + (priors.get(t, (1.0, 1.0))[0] - 1) * fade[t] for t in teams}
    d_prior = {t: 1 + (priors.get(t, (1.0, 1.0))[1] - 1) * fade[t] for t in teams}
    # Ratings are still normalised to a geometric mean of 1. Rescaling to the priors'
    # mean (main.py's 4c fix) needs reciprocal attack/defence priors; these are not
    # (0.79 x 1.10 != 1), and doing it cut every match's expected goals by ~1%.
    attack  = dict(a_prior)
    defence = dict(d_prior)
    for _ in range(n_iter):
        att_num = defaultdict(float); att_den = defaultdict(float)
        def_num = defaultdict(float); def_den = defaultdict(float)
        for (h, a, hg, ag, _), w in zip(ms, weights):
            lam_h = avg_home * attack[h] * defence[a]
            lam_a = avg_away * attack[a] * defence[h]
            att_num[h] += w * hg;  att_den[h] += w * lam_h / attack[h]
            att_num[a] += w * ag;  att_den[a] += w * lam_a / attack[a]
            def_num[a] += w * hg;  def_den[a] += w * lam_h / defence[a]
            def_num[h] += w * ag;  def_den[h] += w * lam_a / defence[h]
        for t in teams:
            shrink_t = shrinkage * (20 / (team_matches[t] + 20))
            if att_den[t] > 0:
                attack[t]  = attack[t] * (((att_num[t] + 1) / (att_den[t] + 1)) ** lr)
                attack[t]  = attack[t] * (1 - shrink_t) + shrink_t * a_prior[t]
            if def_den[t] > 0:
                defence[t] = defence[t] * (((def_num[t] + 1) / (def_den[t] + 1)) ** lr)
                defence[t] = defence[t] * (1 - shrink_t) + shrink_t * d_prior[t]
        att_mean = math.exp(sum(math.log(v) for v in attack.values())  / len(teams))
        def_mean = math.exp(sum(math.log(v) for v in defence.values()) / len(teams))
        attack  = {t: v / att_mean for t, v in attack.items()}
        defence = {t: v / def_mean for t, v in defence.items()}
    return attack, defence, avg_home, avg_away


def fit_rho_league(matches, attack, defence, avg_home, avg_away):
    """Dixon-Coles rho by likelihood over the league scripts' grid."""
    best_rho, best_ll = -0.13, float("-inf")
    for rho_test in [-0.25, -0.20, -0.18, -0.15, -0.13, -0.10, -0.08, -0.05, 0.0, 0.05]:
        ll = 0.0
        for h, a, hg, ag, _ in matches:
            lh = avg_home * attack.get(h, 1.0) * defence.get(a, 1.0)
            la = avg_away * attack.get(a, 1.0) * defence.get(h, 1.0)
            p = poisson_pmf(hg, lh) * poisson_pmf(ag, la) * dixon_coles_tau(hg, ag, lh, la, rho_test)
            if p > 0:
                ll += math.log(p)
        if ll > best_ll:
            best_ll, best_rho = ll, rho_test
    return best_rho


def poisson_pmf(k, lam):
    if lam <= 0: return 1.0 if k == 0 else 0.0
    return math.exp(-lam) * (lam ** k) / math.factorial(k)


def dixon_coles_tau(i, j, lh, la, rho=-0.13):
    if   i == 0 and j == 0: return 1 - lh * la * rho
    elif i == 1 and j == 0: return 1 + la * rho
    elif i == 0 and j == 1: return 1 + lh * rho
    elif i == 1 and j == 1: return 1 - rho
    else:                   return 1.0


def build_score_matrix(lh, la, rho=-0.13, max_goals=7):
    ph = [poisson_pmf(i, lh) for i in range(max_goals + 1)]
    pa = [poisson_pmf(j, la) for j in range(max_goals + 1)]
    matrix = [[ph[i] * pa[j] * dixon_coles_tau(i, j, lh, la, rho)
               for j in range(max_goals + 1)]
              for i in range(max_goals + 1)]
    total = sum(matrix[i][j] for i in range(max_goals+1) for j in range(max_goals+1))
    return [[v / total for v in row] for row in matrix]


def summarize_matrix(matrix, max_goals=7):
    s = dict.fromkeys(["home","draw","away",
                        "over_1_5","under_1_5",
                        "over_2_5","under_2_5",
                        "over_3_5","under_3_5",
                        "btts_yes","btts_no"], 0.0)
    for i in range(max_goals + 1):
        for j in range(max_goals + 1):
            p = matrix[i][j]; g = i + j
            if   i > j:  s["home"] += p
            elif i == j: s["draw"] += p
            else:        s["away"] += p
            s["over_1_5"]  += p if g >= 2 else 0
            s["under_1_5"] += p if g <= 1 else 0
            s["over_2_5"]  += p if g >= 3 else 0
            s["under_2_5"] += p if g <= 2 else 0
            s["over_3_5"]  += p if g >= 4 else 0
            s["under_3_5"] += p if g <= 3 else 0
            if i >= 1 and j >= 1: s["btts_yes"] += p
            else:                 s["btts_no"]  += p
    return s


def prob_to_odds(p):
    return f"{1/p:.2f}" if p > 0.0001 else "inf"


def most_likely_score(matrix, max_goals=7):
    best_p, best = 0.0, (0, 0)
    for i in range(max_goals + 1):
        for j in range(max_goals + 1):
            if matrix[i][j] > best_p:
                best_p = matrix[i][j]; best = (i, j)
    return best, best_p


def print_prediction(home, away, lh, la, league=None, kickoff=None, note=None, rho=-0.13):
    matrix  = build_score_matrix(lh, la, rho)
    summary = summarize_matrix(matrix)
    ml, mlp = most_likely_score(matrix)

    print("=" * 72)
    tag = f"[{league}]" if league else ""
    ko  = f"[{kickoff}]" if kickoff else ""
    print(f"  {tag} {ko}")
    print(f"  {home}  vs  {away}")
    if note: print(f"  Note: {note}")
    print(f"  xG  {home}: {lh:.2f}   {away}: {la:.2f}   (rho={rho})")
    print("-" * 72)
    print(f"  1X2 :  Home {summary['home']*100:5.1f}% ({prob_to_odds(summary['home'])})  "
          f"Draw {summary['draw']*100:5.1f}% ({prob_to_odds(summary['draw'])})  "
          f"Away {summary['away']*100:5.1f}% ({prob_to_odds(summary['away'])})")
    print(f"  O/U :  O1.5 {summary['over_1_5']*100:5.1f}%  "
          f"O2.5 {summary['over_2_5']*100:5.1f}%  "
          f"O3.5 {summary['over_3_5']*100:5.1f}%")
    print(f"  BTTS:  Yes {summary['btts_yes']*100:5.1f}%   No {summary['btts_no']*100:5.1f}%")
    print(f"  Best score: {ml[0]}-{ml[1]}  ({mlp*100:.1f}%)")

    h4, a4 = home[:4], away[:4]
    print(f"\n  {'':6s}  {a4+'0':7s}{a4+'1':7s}{a4+'2':7s}{a4+'3':7s}")
    for i in range(4):
        row = f"  {h4+str(i):7s}"
        for j in range(4):
            row += f"  {matrix[i][j]*100:4.1f}%"
        print(row)
    print()


# Corners, from football-data.org only. No third-party APIs here, unlike
# regular_prediction_model.py, which can also read API-Football.
# football-data.org serves corners only on the paid statistics add-on, so on
# the free tier fetch_corner_matches() returns [] and the model falls back to a
# goal-rating proxy: sides that out-score and out-concede are projected to win
# more corners, damped. It picks up real counts on its own if the add-on is
# ever enabled on the account.
CORNER_BASE       = 5.0    # corners per team, so roughly 10 in an even game
CORNER_PROXY_BETA = 0.6    # damping when deriving corner ratings from goals
HOME_CORNER_ADV   = 1.10   # home sides win slightly more corners

# Optional manual override: {"CL": [("Real Madrid","Bayern München",6,5), ...]}
CORNER_DATA = {}

def fetch_corner_matches(competition, season=None, limit=380):
    """-> [(home, away, home_corners, away_corners), ...], empty on the free
    tier. The statistics field path varies by plan and could not be tested
    without the paid add-on, so if this comes back empty on a paid plan, print
    a single match detail with api_get(f'/matches/{id}') and fix the keys.
    Reuses the season's match list if fetch_season already has it."""
    matches = _RAW_MATCHES.get((competition, season))
    if matches is None:
        params = {"status": "FINISHED", "limit": limit}
        if season is not None:
            params["season"] = season
        try:
            matches = api_get(f"/competitions/{competition}/matches", params).get("matches", [])
        except Exception as e:
            print(f"  [{competition}] corner fetch error: {e}")
            return []
    out = []
    for m in matches:
        hn = m["homeTeam"]["shortName"]; an = m["awayTeam"]["shortName"]
        stats = m.get("statistics")
        if not isinstance(stats, dict):
            continue
        home_s = stats.get("homeTeam") or stats.get("home") or {}
        away_s = stats.get("awayTeam") or stats.get("away") or {}
        hc = home_s.get("corners") if isinstance(home_s, dict) else None
        ac = away_s.get("corners") if isinstance(away_s, dict) else None
        if hc is not None and ac is not None:
            out.append((hn, an, int(hc), int(ac)))
    if out:
        print(f"  [{competition}] Fetched corners for {len(out)} matches.")
    else:
        print(f"  [{competition}] No corner stats on this plan -> goal-rating proxy.")
    return out


def corner_ratings(competition, att, dff, season=None):
    """Return (catt, cconc, avg_hc, avg_ac, source, home_mult). Fits on real corner
    data if available (paid add-on / CORNER_DATA), else proxies from goal ratings.
    Fitted averages already carry the home edge (home_mult 1.0); the proxy's neutral
    CORNER_BASE gets HOME_CORNER_ADV once."""
    cmatches = CORNER_DATA.get(competition) or fetch_corner_matches(competition, season)
    if cmatches and len(cmatches) >= 10:
        catt, cconc, avg_hc, avg_ac = fit_team_ratings(cmatches)
        return catt, cconc, avg_hc, avg_ac, f"fitted on {len(cmatches)} corner matches", 1.0
    catt  = {t: att.get(t, 1.0) ** CORNER_PROXY_BETA for t in att}
    cconc = {t: dff.get(t, 1.0) ** CORNER_PROXY_BETA for t in dff}
    return (catt, cconc, CORNER_BASE, CORNER_BASE, "GOAL-RATING PROXY (no corner data)",
            HOME_CORNER_ADV)


def summarize_corners(matrix):
    n = len(matrix)
    exp_h = exp_a = hm = tie = am = 0.0
    home_marg = [0.0]*n; away_marg = [0.0]*n; totals = {}
    for i in range(n):
        for j in range(n):
            p = matrix[i][j]
            exp_h += i*p; exp_a += j*p
            home_marg[i] += p; away_marg[j] += p
            if   i > j:  hm  += p
            elif i == j: tie += p
            else:        am  += p
            totals[i+j] = totals.get(i+j, 0.0) + p
    ou = {L: sum(p for t, p in totals.items() if t > L)
          for L in [7.5, 8.5, 9.5, 10.5, 11.5, 12.5]}
    return {"exp_h": exp_h, "exp_a": exp_a, "exp_tot": exp_h+exp_a,
            "home_more": hm, "tie": tie, "away_more": am, "ou": ou,
            "totals": totals, "home_marg": home_marg, "away_marg": away_marg}


def print_corner_prediction(home, away, lh, la, league=None, kickoff=None, note=None):
    matrix = build_score_matrix(lh, la, 0.0, max_goals=16)   # rho=0 -> indep Poisson
    s = summarize_corners(matrix)
    od = lambda p: f"{1/p:.2f}" if p > 0.0001 else "inf"
    hk = max(1, int(lh)); ak = max(1, int(la))
    p_h = sum(s["home_marg"][x] for x in range(hk, len(s["home_marg"])))
    p_a = sum(s["away_marg"][x] for x in range(ak, len(s["away_marg"])))
    top = sorted(s["totals"].items(), key=lambda x: -x[1])[:3]
    ou = s["ou"]
    print("=" * 72)
    tag = f"[{league}]" if league else ""
    ko  = f"[{kickoff}]" if kickoff else ""
    print(f"  {tag} {ko}")
    print(f"  {home}  vs  {away}   (CORNERS)")
    if note: print(f"  Note: {note}")
    print(f"  xCorners  {home}: {lh:.1f}   {away}: {la:.1f}   total: {s['exp_tot']:.1f}")
    print("-" * 72)
    print(f"  Totals:  O8.5 {ou[8.5]*100:4.1f}% ({od(ou[8.5])})   "
          f"O9.5 {ou[9.5]*100:4.1f}% ({od(ou[9.5])})   "
          f"O10.5 {ou[10.5]*100:4.1f}% ({od(ou[10.5])})   "
          f"O11.5 {ou[11.5]*100:4.1f}% ({od(ou[11.5])})")
    print(f"  Most corners:  {home} {s['home_more']*100:4.1f}%   "
          f"Tie {s['tie']*100:4.1f}%   {away} {s['away_more']*100:4.1f}%")
    print(f"  Team totals:  {home} over {hk-0.5:.1f}: {p_h*100:4.1f}% ({od(p_h)})   "
          f"{away} over {ak-0.5:.1f}: {p_a*100:4.1f}% ({od(p_a)})")
    print(f"  Most likely total: " + ", ".join(f"{t} ({p*100:.0f}%)" for t, p in top))
    print()


def main():
    days = days_ahead()
    print(f"Champions League + Championship  |  fixtures in the next {days} days")
    print("Dixon-Coles Poisson Model  (goals + corners)")
    print("=" * 72)

    RHO = -0.13

    print(f"\nFetching Champions League data (previous {CL_HISTORY} seasons + current so far)...")
    cl_prev = [m for back in range(CL_HISTORY, 0, -1) for m in fetch_season("CL", CL_SEASON - back)]
    cl_matches = cl_prev + fetch_season("CL", CL_SEASON)
    cl_prev_teams = {t for m in cl_prev for t in m[:2]}
    try:   # each club's country, to tell big-five newcomers from the rest
        cl_country = {t["shortName"]: (t.get("area") or {}).get("name") for t in
                      api_get("/competitions/CL/teams", {"season": CL_SEASON}).get("teams", [])}
    except Exception as e:
        print(f"  [warn] CL team list unavailable ({e}) -> all CL newcomers rated average.")
        cl_country = {}
    time.sleep(0.5)

    def cl_prior(team):
        """(attack, defence) prior: weak for a newcomer from outside the big five, else 1.0."""
        country = CL_LEAGUE_COUNTRY.get(cl_country.get(team), cl_country.get(team))
        if team in cl_prev_teams or country is None or country in CL_BIG5:
            return 1.0, 1.0
        return CL_NEWCOMER_ATT, CL_NEWCOMER_DEF
    print("Fetching Championship data (previous season + current so far)...")
    elc_prev = fetch_season("ELC", ELC_SEASON - 1)
    elc_matches = elc_prev + fetch_season("ELC", ELC_SEASON)
    # Last season's Premier League sides identify who came down this season.
    pl_prev_teams = {t for m in fetch_season("PL", ELC_SEASON - 1) for t in m[:2]}
    elc_prev_teams = {t for m in elc_prev for t in m[:2]}
    if not pl_prev_teams:
        print("  [warn] no Premier League data -> relegated sides get the promoted prior.")
    time.sleep(0.5)

    def elc_prior(team):
        """(attack, defence) prior: relegated, promoted, or 1.0 for last season's sides."""
        if team in pl_prev_teams:
            return ELC_RELEGATED_ATT, ELC_RELEGATED_DEF
        if team not in elc_prev_teams:
            return ELC_PROMOTED_ATT, ELC_PROMOTED_DEF
        return 1.0, 1.0

    if len(cl_matches) >= 10:
        print(f"\nFitting CL team ratings (league method, {CL_HISTORY + 1} seasons)...")
        cl_teams = {t for m in cl_matches for t in m[:2]} | set(cl_country)
        cl_priors = {t: cl_prior(t) for t in cl_teams if cl_prior(t) != (1.0, 1.0)}
        cl_att, cl_def, cl_avg_h, cl_avg_a = fit_league_ratings(cl_matches, priors=cl_priors)
        cl_rho = fit_rho_league(cl_matches, cl_att, cl_def, cl_avg_h, cl_avg_a)
        print(f"  CL rho: {cl_rho}; goal averages home {cl_avg_h:.2f} / away {cl_avg_a:.2f}")
        print(f"  CL newcomers outside the big five (prior {CL_NEWCOMER_ATT}/{CL_NEWCOMER_DEF}): "
              f"{sorted(cl_priors)}")
    else:
        print("  Not enough CL data -- using neutral ratings and long-run CL averages.")
        cl_att = {}; cl_def = {}; cl_avg_h = CL_PRIOR_HOME; cl_avg_a = CL_PRIOR_AWAY; cl_rho = RHO

    if len(elc_matches) >= 10:
        print("Fitting Championship team ratings (league method, two seasons)...")
        elc_teams = {t for m in elc_matches for t in m[:2]}
        elc_att, elc_def, elc_avg_h, elc_avg_a = fit_league_ratings(
            elc_matches, priors={t: elc_prior(t) for t in elc_teams})
        elc_rho = fit_rho_league(elc_matches, elc_att, elc_def, elc_avg_h, elc_avg_a)
        print(f"  Championship rho: {elc_rho}")
        print(f"  Relegated (prior {ELC_RELEGATED_ATT}/{ELC_RELEGATED_DEF}): "
              f"{sorted(t for t in elc_teams if t in pl_prev_teams)}")
        print(f"  Promoted  (prior {ELC_PROMOTED_ATT}/{ELC_PROMOTED_DEF}): "
              f"{sorted(t for t in elc_teams if elc_prior(t) == (ELC_PROMOTED_ATT, ELC_PROMOTED_DEF))}")
    else:
        print("  Not enough ELC data -- using neutral ratings.")
        elc_att = {}; elc_def = {}; elc_avg_h = 1.50; elc_avg_a = 1.15; elc_rho = RHO

    # Home advantage is applied once: the fitted (or fallback) home/away averages
    # already carry it, so no CL/ELC multiplier goes on top (docs/MODEL_CHANGES.md).
    def lam_cl(home, away):
        # A side with no CL match in the fitted seasons yet uses its newcomer prior.
        h_att = cl_att.get(home, cl_prior(home)[0]); h_def = cl_def.get(home, cl_prior(home)[1])
        a_att = cl_att.get(away, cl_prior(away)[0]); a_def = cl_def.get(away, cl_prior(away)[1])
        return round(cl_avg_h*h_att*a_def,3), round(cl_avg_a*a_att*h_def,3)

    def lam_elc(home, away):
        # A side with no match in either season yet uses its relegated/promoted prior.
        h_att = elc_att.get(home, elc_prior(home)[0]); h_def = elc_def.get(home, elc_prior(home)[1])
        a_att = elc_att.get(away, elc_prior(away)[0]); a_def = elc_def.get(away, elc_prior(away)[1])
        return round(elc_avg_h*h_att*a_def,3), round(elc_avg_a*a_att*h_def,3)

    # Corner ratings are fitted per competition, not shared.
    cl_catt, cl_ccon, cl_chc, cl_cac, cl_csrc, cl_cha = corner_ratings("CL", cl_att, cl_def, CL_SEASON)
    elc_catt, elc_ccon, elc_chc, elc_cac, elc_csrc, elc_cha = corner_ratings("ELC", elc_att, elc_def, ELC_SEASON)

    def clam_cl(home, away):
        return (round(cl_chc*cl_catt.get(home,1.0)*cl_ccon.get(away,1.0)*cl_cha,3),
                round(cl_cac*cl_catt.get(away,1.0)*cl_ccon.get(home,1.0),3))

    def clam_elc(home, away):
        return (round(elc_chc*elc_catt.get(home,1.0)*elc_ccon.get(away,1.0)*elc_cha,3),
                round(elc_cac*elc_catt.get(away,1.0)*elc_ccon.get(home,1.0),3))

    print("\nFixtures:")
    wanted = [c for c, listed in (("CL", CL_FIXTURES), ("ELC", ELC_FIXTURES)) if not listed]
    fetched = fetch_fixtures(wanted, days) if wanted else {}
    cl_fixtures = CL_FIXTURES or fetched.get("CL", [])
    elc_fixtures = ELC_FIXTURES or fetched.get("ELC", [])
    if not cl_fixtures and not elc_fixtures:
        print(f"  Nothing scheduled in the next {days} days (international break?). Try --days 14.")
        return

    print("\nName resolution (fixture name -> API name):")
    cl_names = resolve_fixtures("CL", [t for f in cl_fixtures for t in f[:2]], cl_matches)
    elc_names = resolve_fixtures("ELC", [t for f in elc_fixtures for t in f[:2]], elc_matches)

    if cl_fixtures:
        print(f"\n{'='*72}\nCHAMPIONS LEAGUE -- GOALS\n{'='*72}\n")
        for home, away, ko, note in cl_fixtures:
            lh, la = lam_cl(cl_names[home], cl_names[away])
            print_prediction(home, away, lh, la, league="Champions League", kickoff=ko, note=note, rho=cl_rho)

        print(f"\n{'='*72}\nCHAMPIONS LEAGUE -- CORNERS  ({cl_csrc})\n{'='*72}\n")
        for home, away, ko, note in cl_fixtures:
            lhc, lac = clam_cl(cl_names[home], cl_names[away])
            print_corner_prediction(home, away, lhc, lac, league="Champions League", kickoff=ko, note=note)

    if elc_fixtures:
        print(f"\n{'='*72}\nCHAMPIONSHIP -- GOALS\n{'='*72}\n")
        for home, away, ko, note in elc_fixtures:
            lh, la = lam_elc(elc_names[home], elc_names[away])
            print_prediction(home, away, lh, la, league="Championship", kickoff=ko, note=note, rho=elc_rho)

        print(f"\n{'='*72}\nCHAMPIONSHIP -- CORNERS  ({elc_csrc})\n{'='*72}\n")
        for home, away, ko, note in elc_fixtures:
            lhc, lac = clam_elc(elc_names[home], elc_names[away])
            print_corner_prediction(home, away, lhc, lac, league="Championship", kickoff=ko, note=note)


if __name__ == "__main__":
    main()
