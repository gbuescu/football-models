"""Standard club prediction model.

A Dixon-Coles goals model with a corners predictor on top. This is the base
version the league-specific scripts are refined from, and the one to start
with for a competition that has no script of its own.

Set the competition code in main(). It fits last season plus the current one so
far, time-decayed, with a fading weak-side prior for sides new this season.
Fixtures come from the API (every scheduled match in the next DAYS_AHEAD days, or
--days N) unless FIXTURES lists specific games. Standard library only.
"""

import math
import os
import sys
import time
import unicodedata
import urllib.error
import urllib.request
import json
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


def current_season(today=None):
    """Starting year of the season in progress, as football-data.org keys seasons
    (2026 = 2026-27). From June the next season counts as current: the last one is
    over and the new one has no finished matches yet. Assumes an August-May season."""
    today = today or date.today()
    return today.year if today.month >= 6 else today.year - 1


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
        # Over the free tier's 10 requests a minute: wait for the counter to reset and
        # retry once, rather than silently fitting without a season.
        wait = int(e.headers.get("X-RequestCounter-Reset") or 60) + 1
        print(f"  [rate limit] waiting {wait}s...")
        time.sleep(wait)
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())

DAYS_AHEAD = 7   # fixture window in days; override with --days N
FIXTURES   = []  # optional manual list: ("Home Team", "Away Team", "Kickoff")
ALIASES    = {}  # hand-typed fixture name -> [API spellings]; API fixtures need none

# The fit uses last season plus the current one so far. SEASON is the current season's
# starting year (2026 = 2026-27); None works it out from today's date, which suits an
# August-May league. Pin it for a calendar-year league such as the Brasileirao.
SEASON = None
# A side with no match last season (in a league, a promoted club) starts from a weak
# prior that fades over PRIOR_GAMES games of its own results. These values sit between
# those measured for the Premier League (0.69 / 1.25) and Liga Portugal (0.78 / 1.08);
# use your league's own if you can measure it (docs/MODEL_CHANGES.md). In a second
# division the newcomers include relegated clubs, which this prior would underrate.
NEWCOMER_ATT, NEWCOMER_DEF = 0.73, 1.16
PRIOR_GAMES = 5

def days_ahead():
    """--days N on the command line, else DAYS_AHEAD."""
    if "--days" in sys.argv[1:-1]:
        return int(sys.argv[sys.argv.index("--days") + 1])
    return DAYS_AHEAD

def fetch_fixtures(competition, days):
    """Scheduled matches from today through `days` days ahead -> [(home, away,
    kickoff), ...], kickoff in local time, names as the API spells them."""
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

def fetch_season(competition, season, limit=500):
    """Finished matches of one season -> [(home, away, hg, ag, utcDate), ...]. The
    season is always passed: without it the API serves the current season, which in
    August has no finished matches."""
    try:
        data = api_get(f"/competitions/{competition}/matches",
                       {"status": "FINISHED", "season": season, "limit": limit})
    except Exception as e:
        print(f"  [{competition} {season}] API error: {e}")
        return []
    matches = []
    for m in data.get("matches", []):
        ft = (m.get("score") or {}).get("fullTime") or {}
        hg, ag = ft.get("home"), ft.get("away")
        if hg is None or ag is None:
            continue
        home = m["homeTeam"].get("shortName") or m["homeTeam"].get("name")
        away = m["awayTeam"].get("shortName") or m["awayTeam"].get("name")
        matches.append((home, away, int(hg), int(ag), m.get("utcDate", "")))
    print(f"  [{competition} {season}-{season + 1}] Fetched {len(matches)} finished matches.")
    return matches

def _flat(name):
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii").lower()
    return "".join(ch for ch in s if ch.isalnum())

def resolve(name, data_teams):
    """Fixture name -> the data's spelling, or None. Exact, then accent- and
    punctuation-insensitive, then ALIASES. No fuzzy guessing: a wrong guess would
    silently price the wrong team."""
    flat = {_flat(t): t for t in data_teams}
    for cand in [name] + ALIASES.get(name, []):
        if cand in data_teams:
            return cand
        if _flat(cand) in flat:
            return flat[_flat(cand)]
    return None

def auto_shrinkage(n_matches):
    if   n_matches < 100:  return 0.12
    elif n_matches < 200:  return 0.08
    elif n_matches < 300:  return 0.05
    else:                  return 0.03

def fit_team_ratings(matches, home_advantage=1.0, n_iter=300, lr=0.01,
                     decay=0.0035, shrinkage=None, priors=None):
    """matches: [(home, away, home_count, away_count, date), ...], goals or corners.
    priors: {team: (attack, defence)} for sides new this season. Such a side starts at
    its prior and is shrunk toward a target fading from it to 1.0 over PRIOR_GAMES
    games; everyone else is shrunk toward 1.0."""
    if shrinkage is None:
        shrinkage = auto_shrinkage(len(matches))
        print(f"  Auto-selected shrinkage: {shrinkage} for {len(matches)} matches")
    teams = set()
    for h, a, _, _, _ in matches:
        teams.add(h); teams.add(a)
    matches_sorted = sorted(matches, key=lambda x: x[4])
    n = len(matches_sorted)
    weights = [math.exp(-decay * (n - 1 - i)) for i in range(n)]
    avg_home = sum(w * hg for (_, _, hg, _, _), w in zip(matches_sorted, weights)) / sum(weights)
    avg_away = sum(w * ag for (_, _, _, ag, _), w in zip(matches_sorted, weights)) / sum(weights)
    team_matches = defaultdict(float)
    for (h, a, _, _, _), w in zip(matches_sorted, weights):
        team_matches[h] += w; team_matches[a] += w
    priors = priors or {}
    fade = {t: PRIOR_GAMES / (PRIOR_GAMES + team_matches[t]) for t in teams}
    a_tgt = {t: 1 + (priors.get(t, (1.0, 1.0))[0] - 1) * fade[t] for t in teams}
    d_tgt = {t: 1 + (priors.get(t, (1.0, 1.0))[1] - 1) * fade[t] for t in teams}
    attack, defence = dict(a_tgt), dict(d_tgt)
    for _ in range(n_iter):
        att_num = defaultdict(float); att_den = defaultdict(float)
        def_num = defaultdict(float); def_den = defaultdict(float)
        for (h, a, hg, ag, _), w in zip(matches_sorted, weights):
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
    best_rho, best_ll = -0.13, float("-inf")
    for rho_test in [-0.25, -0.20, -0.18, -0.15, -0.13, -0.10, -0.08, -0.05, 0.0, 0.05]:
        ll = 0.0
        for h, a, hg, ag, _ in matches:
            h_att = attack.get(h, 1.0); h_def = defence.get(h, 1.0)
            a_att = attack.get(a, 1.0); a_def = defence.get(a, 1.0)
            lh = avg_home * h_att * a_def * home_advantage
            la = avg_away * a_att * h_def
            ph = poisson_pmf(hg, lh); pa = poisson_pmf(ag, la)
            tau = dixon_coles_tau(hg, ag, lh, la, rho_test)
            p = ph * pa * tau
            if p > 0: ll += math.log(p)
        if ll > best_ll: best_ll, best_rho = ll, rho_test
    return best_rho

def calibrate_probability(p, strength=0.08):
    return p * (1 - strength) + 0.5 * strength

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

def summarize_matrix(matrix, max_goals=7, calibrate=True):
    s = dict.fromkeys(["home","draw","away","over_1_5","under_1_5","over_2_5",
                       "under_2_5","over_3_5","under_3_5","btts_yes","btts_no"], 0.0)
    for i in range(max_goals + 1):
        for j in range(max_goals + 1):
            p = matrix[i][j]; g = i + j
            if   i > j:  s["home"] += p
            elif i == j: s["draw"] += p
            else:        s["away"] += p
            if g >= 2: s["over_1_5"] += p
            else:      s["under_1_5"] += p
            if g >= 3: s["over_2_5"] += p
            else:      s["under_2_5"] += p
            if g >= 4: s["over_3_5"] += p
            else:      s["under_3_5"] += p
            if i >= 1 and j >= 1: s["btts_yes"] += p
            else:                 s["btts_no"]  += p
    if calibrate:
        for k in ["home", "draw", "away"]:
            s[k] = calibrate_probability(s[k])
        tot = s["home"] + s["draw"] + s["away"]
        if tot > 0:
            s["home"] /= tot; s["draw"] /= tot; s["away"] /= tot
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

def confidence_flag(lh, la, prior_based=False):
    if prior_based: return "PRIOR-BASED -- a side has no match in either season yet"
    if lh > 3.5 or la > 3.5: return "OVERFITTED -- goals markets only"
    if lh / max(la, 0.01) > 4 or la / max(lh, 0.01) > 4: return "EXTREME RATIO -- result markets unreliable"
    if lh + la > 4.5: return "HIGH-VARIANCE -- goals likely but result uncertain"
    return "RELIABLE"

def print_prediction(home, away, lh, la, league=None, kickoff=None, rho=-0.13,
                     prior_based=False):
    matrix  = build_score_matrix(lh, la, rho)
    summary = summarize_matrix(matrix, calibrate=True)
    ml, mlp = most_likely_score(matrix)
    flag    = confidence_flag(lh, la, prior_based)
    print("=" * 72)
    print(f"  [{league}] [{kickoff}]" if league else "")
    print(f"  {home}  vs  {away}")
    print(f"  Confidence: {flag}")
    print(f"  xG  {home}: {lh:.2f}   {away}: {la:.2f}   (rho={rho})")
    print("-" * 72)
    print(f"  1X2 :  Home {summary['home']*100:5.1f}% ({prob_to_odds(summary['home'])})  "
          f"Draw {summary['draw']*100:5.1f}% ({prob_to_odds(summary['draw'])})  "
          f"Away {summary['away']*100:5.1f}% ({prob_to_odds(summary['away'])})")
    print(f"  O/U :  O1.5 {summary['over_1_5']*100:5.1f}%  "
          f"O2.5 {summary['over_2_5']*100:5.1f}%  O3.5 {summary['over_3_5']*100:5.1f}%")
    print(f"  BTTS:  Yes {summary['btts_yes']*100:5.1f}%   No {summary['btts_no']*100:5.1f}%")
    print(f"  Best score: {ml[0]}-{ml[1]}  ({mlp*100:.1f}%)")
    print(f"  WDW home: {(summary['home']+summary['draw'])*100:.1f}%  "
          f"WDW away: {(summary['away']+summary['draw'])*100:.1f}%")
    print()

# Corners. The same machinery as goals, but it only means anything if it is
# fit on actual corner counts. football-data.org returns scores only, so either
# paste counts into CORNER_DATA or set APISPORTS_KEY for the API-Football feed.
# With neither, the model falls back to a goal-rating proxy: sides that
# out-score and out-concede tend to out-corner as well, damped. That is
# directionally useful, not a replacement for real corner data.
CORNER_BASE       = 5.0   # corners per team, so roughly 10 in an even game
CORNER_PROXY_BETA = 0.6   # damping when deriving corner ratings from goal ratings
CORNER_DATA = [
    # ("Arsenal", "Chelsea", 8, 4, "2026-03-01"),   # (home, away, hc, ac, date)
]
CORNER_API_KEY = _load_api_key("APISPORTS_KEY", required=False)

def fetch_corner_matches(league_id=None, season=None, limit=380):
    """API-Football adapter -> [(home, away, hc, ac, date), ...].
    Pulls finished fixtures, then each fixture's statistics, and reads the
    'Corner Kicks' stat. One stats call per fixture, so it burns request quota
    quickly. Cache the results. Field names vary by plan, so check yours."""
    if not CORNER_API_KEY or league_id is None or season is None:
        return []
    base = "https://v3.football.api-sports.io"
    hdr  = {"x-apisports-key": CORNER_API_KEY}
    def _get(path):
        req = urllib.request.Request(base + path, headers=hdr)
        with urllib.request.urlopen(req) as r:
            return json.loads(r.read())
    out = []
    fx = _get(f"/fixtures?league={league_id}&season={season}&status=FT")
    for f in fx.get("response", [])[:limit]:
        fid = f["fixture"]["id"]
        hn, an = f["teams"]["home"]["name"], f["teams"]["away"]["name"]
        dt = f["fixture"]["date"]
        hc = ac = None
        for side in _get(f"/fixtures/statistics?fixture={fid}").get("response", []):
            nm = side["team"]["name"]
            for s in side.get("statistics", []):
                if s.get("type") == "Corner Kicks":
                    v = s.get("value") or 0
                    if   nm == hn: hc = int(v)
                    elif nm == an: ac = int(v)
        if hc is not None and ac is not None:
            out.append((hn, an, hc, ac, dt))
    return out

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

def print_corner_prediction(home, away, lh, la, league=None, kickoff=None):
    matrix = build_score_matrix(lh, la, 0.0, max_goals=16)  # rho=0 -> indep Poisson
    s = summarize_corners(matrix)
    od = lambda p: f"{1/p:.2f}" if p > 0.0001 else "inf"
    hk = max(1, int(lh)); ak = max(1, int(la))
    p_h = sum(s["home_marg"][x] for x in range(hk, len(s["home_marg"])))
    p_a = sum(s["away_marg"][x] for x in range(ak, len(s["away_marg"])))
    top = sorted(s["totals"].items(), key=lambda x: -x[1])[:3]
    ou = s["ou"]
    print("=" * 72)
    print(f"  [{league}] [{kickoff}]" if league else "")
    print(f"  {home}  vs  {away}   (CORNERS)")
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
    # Competition codes: PL, ELC, PD, BL1, SA, FL1, CL, EL, EC, PPL
    COMPETITION, LEAGUE = "PL", "Premier League"
    season = SEASON or current_season()
    print(f"{LEAGUE}: fitting {season - 1}-{season} plus {season}-{season + 1} so far")
    prev = fetch_season(COMPETITION, season - 1)
    cur = fetch_season(COMPETITION, season)
    matches = prev + cur          # time-decay weights this season's matches most
    # Sides with no match last season are new this season: in a league, promoted clubs.
    prev_teams = {t for m in prev for t in m[:2]}
    newcomers = sorted({t for m in cur for t in m[:2]} - prev_teams) if prev else []
    priors = {t: (NEWCOMER_ATT, NEWCOMER_DEF) for t in newcomers}
    print(f"  New this season (prior {NEWCOMER_ATT}/{NEWCOMER_DEF}, fading over "
          f"{PRIOR_GAMES} games): {newcomers or 'none with matches yet'}")
    # Home advantage is applied once: avg_h / avg_a are the competition's own home and
    # away goal averages, so they already carry it (docs/MODEL_CHANGES.md).
    if len(matches) >= 10:
        att, dff, avg_h, avg_a = fit_team_ratings(matches, priors=priors)
        rho = fit_rho(matches, att, dff, avg_h, avg_a, 1.0)
        print(f"  Best rho: {rho}")
    else:
        print("  Not enough data -- neutral ratings and generic goal averages.")
        att, dff, avg_h, avg_a, rho = {}, {}, 1.50, 1.15, -0.13

    fixtures = FIXTURES or fetch_fixtures(COMPETITION, days_ahead())
    if not fixtures:
        print(f"  No scheduled {COMPETITION} fixtures in the window (international break?). Try --days 14.")
    # API fixtures use the data's own names, so a name with no data is a side with no
    # match yet. A hand-typed name with no data is more likely a spelling problem.
    names = {}
    for nm in sorted({t for f in fixtures for t in f[:2]}):
        names[nm] = resolve(nm, set(att))
        if names[nm] is None and att:
            print(f"  [note] {nm}: no match in either season yet -> newcomer prior" if not FIXTURES else
                  f"  [warn] {nm}: not in the data -- check the spelling or ALIASES; newcomer prior used")
        elif names[nm] and names[nm] != nm:
            print(f"  {nm} -> {names[nm]}")

    def rating(name, a_tbl, d_tbl, fallback):
        """(attack, defence, has_data) for a fixture name; `fallback` for a side with no data."""
        key = names.get(name)
        if key is None:   # with no data at all, every side is neutral, not prior-based
            return fallback[0], fallback[1], not att
        return a_tbl.get(key, fallback[0]), d_tbl.get(key, fallback[1]), True

    newcomer = (NEWCOMER_ATT, NEWCOMER_DEF) if att else (1.0, 1.0)

    def lam(home, away):
        h_att, h_def, h_ok = rating(home, att, dff, newcomer)
        a_att, a_def, a_ok = rating(away, att, dff, newcomer)
        return round(avg_h * h_att * a_def, 3), round(avg_a * a_att * h_def, 3), not (h_ok and a_ok)

    for home, away, ko in fixtures:
        lh, la, prior_based = lam(home, away)
        print_prediction(home, away, lh, la, league=LEAGUE, kickoff=ko, rho=rho,
                         prior_based=prior_based)

    print("#" * 72)
    print("#  CORNERS")
    print("#" * 72)
    HOME_CORNER_ADV = 1.10   # home sides win slightly more corners
    cmatches = CORNER_DATA or fetch_corner_matches()
    if cmatches and len(cmatches) >= 10:
        # Fitted home/away corner averages already carry the home edge.
        catt, cconc, avg_hc, avg_ac = fit_team_ratings(cmatches)
        c_ha = 1.0
        c_new = (1.0, 1.0)
        print(f"  Corner model: fitted on {len(cmatches)} corner records\n")
    else:
        catt  = {t: att.get(t, 1.0) ** CORNER_PROXY_BETA for t in att}
        cconc = {t: dff.get(t, 1.0) ** CORNER_PROXY_BETA for t in dff}
        avg_hc = avg_ac = CORNER_BASE
        c_ha = HOME_CORNER_ADV   # neutral base, so the home edge is applied here, once
        c_new = tuple(v ** CORNER_PROXY_BETA for v in newcomer)   # damped like the rest
        print("  Corner model: GOAL-RATING PROXY (no corner data supplied)\n")

    def clam(home, away):
        h_catt, h_ccon, _ = rating(home, catt, cconc, c_new)
        a_catt, a_ccon, _ = rating(away, catt, cconc, c_new)
        return (round(avg_hc * h_catt * a_ccon * c_ha, 3),
                round(avg_ac * a_catt * h_ccon, 3))

    for home, away, ko in fixtures:
        lhc, lac = clam(home, away)
        print_corner_prediction(home, away, lhc, lac, league=LEAGUE, kickoff=ko)

if __name__ == "__main__":
    main()
