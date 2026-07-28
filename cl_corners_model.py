"""Champions League and Championship, goals and corners.

A cut-down variant for knockout and cup competitions, where a full league
season's worth of matches does not exist. Deliberately simpler than the league
scripts: no time-decay, no shrinkage, no half-time model, and a plain Poisson
tail on corner totals rather than a negative binomial. It also prints a small
correct-score grid, which the league scripts do not.

Small samples mean the ratings here overfit more than the league versions do.
Treat the goals markets as more trustworthy than the result markets.
"""

import math
import os
import time
import urllib.request
import json
from collections import defaultdict


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
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def fetch_finished_matches(competition, limit=380):
    try:
        data = api_get(f"/competitions/{competition}/matches",
                       {"status": "FINISHED", "limit": limit})
        matches = []
        for m in data.get("matches", []):
            home = m["homeTeam"]["shortName"]
            away = m["awayTeam"]["shortName"]
            hg   = m["score"]["fullTime"]["home"]
            ag   = m["score"]["fullTime"]["away"]
            if hg is None or ag is None:
                continue
            matches.append((home, away, int(hg), int(ag)))
        print(f"  [{competition}] Fetched {len(matches)} finished matches.")
        return matches
    except Exception as e:
        print(f"  [{competition}] API error: {e}. Using neutral ratings.")
        return []


def fit_team_ratings(matches, home_advantage=1.25, n_iter=200, lr=0.01):
    """Shared by goals and corners: a corner record is just
    (home, away, home_count, away_count), the same shape as a scoreline."""
    teams = set()
    for h, a, _, _ in matches:
        teams.add(h); teams.add(a)

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
            if att_den[t] > 0: attack[t]  *= (att_num[t] / att_den[t]) ** lr
            if def_den[t] > 0: defence[t] *= (def_num[t] / def_den[t]) ** lr

        att_mean = math.exp(sum(math.log(v) for v in attack.values())  / len(teams))
        def_mean = math.exp(sum(math.log(v) for v in defence.values()) / len(teams))
        attack  = {t: v / att_mean for t, v in attack.items()}
        defence = {t: v / def_mean for t, v in defence.items()}

    return attack, defence, avg_home, avg_away


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

def fetch_corner_matches(competition, limit=380):
    """-> [(home, away, home_corners, away_corners), ...], empty on the free
    tier. The statistics field path varies by plan and could not be tested
    without the paid add-on, so if this comes back empty on a paid plan, print
    a single match detail with api_get(f'/matches/{id}') and fix the keys."""
    try:
        data = api_get(f"/competitions/{competition}/matches",
                       {"status": "FINISHED", "limit": limit})
    except Exception as e:
        print(f"  [{competition}] corner fetch error: {e}")
        return []
    out = []
    for m in data.get("matches", []):
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


def corner_ratings(competition, att, dff):
    """Return (catt, cconc, avg_hc, avg_ac, source). Fits on real corner data
    if available (paid add-on / CORNER_DATA), else proxies from goal ratings."""
    cmatches = CORNER_DATA.get(competition) or fetch_corner_matches(competition)
    if cmatches and len(cmatches) >= 10:
        catt, cconc, avg_hc, avg_ac = fit_team_ratings(
            cmatches, home_advantage=HOME_CORNER_ADV)
        return catt, cconc, avg_hc, avg_ac, f"fitted on {len(cmatches)} corner matches"
    catt  = {t: att.get(t, 1.0) ** CORNER_PROXY_BETA for t in att}
    cconc = {t: dff.get(t, 1.0) ** CORNER_PROXY_BETA for t in dff}
    return catt, cconc, CORNER_BASE, CORNER_BASE, "GOAL-RATING PROXY (no corner data)"


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
    print("Champions League Quarter-Finals + Championship")
    print("Dixon-Coles Poisson Model  (goals + corners)")
    print("=" * 72)

    RHO = -0.13

    print("\nFetching Champions League data...")
    cl_matches = fetch_finished_matches("CL")
    time.sleep(0.5)
    print("Fetching Championship data...")
    elc_matches = fetch_finished_matches("ELC")
    time.sleep(0.5)

    if len(cl_matches) >= 10:
        print("\nFitting CL team ratings...")
        cl_att, cl_def, cl_avg_h, cl_avg_a = fit_team_ratings(cl_matches)
    else:
        print("  Not enough CL data -- using neutral ratings.")
        cl_att = {}; cl_def = {}; cl_avg_h = 1.50; cl_avg_a = 1.20

    if len(elc_matches) >= 10:
        print("Fitting Championship team ratings...")
        elc_att, elc_def, elc_avg_h, elc_avg_a = fit_team_ratings(elc_matches, home_advantage=1.35)
    else:
        print("  Not enough ELC data -- using neutral ratings.")
        elc_att = {}; elc_def = {}; elc_avg_h = 1.50; elc_avg_a = 1.15

    CL_HOME_ADV  = 1.20
    ELC_HOME_ADV = 1.35

    def lam_cl(home, away):
        h_att = cl_att.get(home,1.0); h_def = cl_def.get(home,1.0)
        a_att = cl_att.get(away,1.0); a_def = cl_def.get(away,1.0)
        return round(cl_avg_h*h_att*a_def*CL_HOME_ADV,3), round(cl_avg_a*a_att*h_def,3)

    def lam_elc(home, away):
        h_att = elc_att.get(home,1.0); h_def = elc_def.get(home,1.0)
        a_att = elc_att.get(away,1.0); a_def = elc_def.get(away,1.0)
        return round(elc_avg_h*h_att*a_def*ELC_HOME_ADV,3), round(elc_avg_a*a_att*h_def,3)

    # Corner ratings are fitted per competition, not shared.
    cl_catt, cl_ccon, cl_chc, cl_cac, cl_csrc = corner_ratings("CL", cl_att, cl_def)
    elc_catt, elc_ccon, elc_chc, elc_cac, elc_csrc = corner_ratings("ELC", elc_att, elc_def)

    def clam_cl(home, away):
        return (round(cl_chc*cl_catt.get(home,1.0)*cl_ccon.get(away,1.0)*HOME_CORNER_ADV,3),
                round(cl_cac*cl_catt.get(away,1.0)*cl_ccon.get(home,1.0),3))

    def clam_elc(home, away):
        return (round(elc_chc*elc_catt.get(home,1.0)*elc_ccon.get(away,1.0)*HOME_CORNER_ADV,3),
                round(elc_cac*elc_catt.get(away,1.0)*elc_ccon.get(home,1.0),3))

    cl_fixtures = [
        ("Real Madrid",  "Bayern München", "8:00 PM", "CL QF Leg 1 -- Tuesday"),
        ("Sporting CP",  "Arsenal",        "8:00 PM", "CL QF Leg 1 -- Tuesday"),
        ("Barcelona",    "Atletico Madrid","8:00 PM", "CL QF Leg 1 -- Wednesday"),
        ("PSG",          "Liverpool",      "8:00 PM", "CL QF Leg 1 -- Wednesday"),
    ]
    elc_fixtures = [
        ("Wrexham", "Southampton", "8:00 PM", None),
    ]

    print(f"\n{'='*72}\nCHAMPIONS LEAGUE QF -- GOALS\n{'='*72}\n")
    for home, away, ko, note in cl_fixtures:
        lh, la = lam_cl(home, away)
        print_prediction(home, away, lh, la, league="Champions League QF", kickoff=ko, note=note, rho=RHO)

    print(f"\n{'='*72}\nCHAMPIONS LEAGUE QF -- CORNERS  ({cl_csrc})\n{'='*72}\n")
    for home, away, ko, note in cl_fixtures:
        lhc, lac = clam_cl(home, away)
        print_corner_prediction(home, away, lhc, lac, league="Champions League QF", kickoff=ko, note=note)

    print(f"\n{'='*72}\nCHAMPIONSHIP -- GOALS\n{'='*72}\n")
    for home, away, ko, note in elc_fixtures:
        lh, la = lam_elc(home, away)
        print_prediction(home, away, lh, la, league="Championship", kickoff=ko, note=note, rho=RHO)

    print(f"\n{'='*72}\nCHAMPIONSHIP -- CORNERS  ({elc_csrc})\n{'='*72}\n")
    for home, away, ko, note in elc_fixtures:
        lhc, lac = clam_elc(home, away)
        print_corner_prediction(home, away, lhc, lac, league="Championship", kickoff=ko, note=note)


if __name__ == "__main__":
    main()
