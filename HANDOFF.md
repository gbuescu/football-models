# Maintenance notes

Notes for anyone picking the scripts up later, including me. Several decisions
in here look like bugs and are not. Read this before changing the modelling
code.

For what the repo is and how to run it, see `README.md` and `SETUP.md`.

## The scripts

Four scripts, all in the repository root, all standard library only, plus
`club_backtest.py`, which tests them against bookmaker odds.
`docs/MODEL_CHANGES.md` records what changed in October 2026 and the evidence
for each change. Read it before changing the modelling code.

`regular_prediction_model.py` is the standard model and the base the others are
refined from. It is not a legacy file. If a competition has no script of its
own, this is the one to point at it.

`pl_model.py` and `liga_portugal_model.py` are the two mature league scripts,
both run against live data. `cl_corners_model.py` is a simpler variant for
the Champions League and the Championship: simpler output and no half-time
layer, but since October 2026 the same multi-season, time-decayed fit as the
league scripts.

Do not merge these into one architecture without deciding to do so on purpose.
They have different priors, different fitting windows and different
modelling layers, and those differences are the point.

## Data constraints

Checked against football-data.org's own documentation and pricing rather than
assumed. These are the main source of confusion about why something does not
work.

The free tier is scores only. Per finished match you get the full-time score,
the half-time score, the date and the matchday. Nothing else.

No corners, shots, possession, cards or player data. Corners need the paid
statistics add-on, which at the time of checking meant the Standard tier plus
the add-on on top.

Player props cannot be modelled at all, because there is no per-player data
anywhere in the feed. Any goalscorer or shots-on-target number would be
fabricated.

Rate limit is 10 requests a minute on the free tier. Competition codes in use:
`PL`, `ELC`, `PD`, `BL1`, `SA`, `FL1`, `CL`, `EL` (often 403 on free), `EC`,
`PPL`, `WC`.

The consequence is that the corner models run on a goal-rating proxy, with
corner form derived from fitted goal ratings via `attack ** 0.6`, never from
actual corner counts. Every script labels this in its output. Do not widen that
claim or remove the label.

`fetch_corner_matches()` is already wired to read real corners and activates on
its own if the paid add-on is ever added. Its field path
(`statistics.homeTeam.corners`) is unverified, because it is a paid feature that
could not be tested. If it returns empty on a paid plan, dump
`api_get(f"/matches/{id}")` and fix the keys.

## Decisions that are deliberate

**The season is always explicit.** football-data.org keys a season by its
starting year, so `2025` is the 2025-26 season. Requesting without the
parameter returns the current season, which in August has zero finished
matches and silently produces neutral 1.0 ratings for everyone. This is the
easiest way to get confident-looking rubbish out of these scripts, so every
request passes a season. The seasons come from today's date via
`current_season()`: from June, the next season counts as current. In the
league scripts `SEASON` is the last completed season and
`CURRENT = SEASON + 1` is fitted alongside it. In `cl_corners_model.py`,
`ELC_SEASON` and `CL_SEASON` are the seasons in progress. Pin any of them to a
year to override.

**Promoted-side priors.** Newly promoted clubs appear nowhere in last season's
top-flight data. Rating them a neutral 1.0 would put them mid-table, so they
start from `PROMOTED_ATT` / `PROMOTED_DEF`, measured on past promoted sides
(Premier League 0.69 / 1.25, Liga Portugal 0.78 / 1.08). As they play, the
target they are shrunk toward fades from the prior to league average, worth
`PRIOR_GAMES` = 5 games. Before a promoted club's first match its predictions
print `PRIOR-BASED`, and the model is echoing the assumption back. The
Championship has its own relegated and promoted priors, and the Champions
League one for newcomers from outside the big five leagues. All of them are
measured, not judgment calls (`docs/MODEL_CHANGES.md`, section 6).

**Name resolution is defensive on purpose.** Naive token matching silently
mapped Coventry City and Hull City to Man City, on the shared token "city",
neither club being in last season's data. That would have rated a promoted side
as Manchester City while printing `RELIABLE`. Three guards, all load-bearing:
promoted clubs (the `PROMOTED` names) resolve by alias or exact name only and
never reach token matching, an explicit `ALIASES` table does the real work, and
a match on a weak token alone ("city", "united", "town", "albion") is rejected
rather than guessed. Fixtures fetched from the API use the data's own names, so
they skip token matching altogether. A name with no exact match there is a side
with no match yet and gets the promoted prior. The `PROMOTED` lists therefore
only matter for hand-typed fixtures. Keep all three.
Removing any one brings back silent, confident-looking errors.

**Negative binomial for corner totals.** Corner counts are over-dispersed and
plain Poisson runs its tails too tight. `nb_pmf(k, mu, phi)` uses
`phi = variance/mean`, defaulting to 1.25 and re-estimated from data when real
corners exist. Measured effect at mu=10.2: Over 7.5 moved from 79.7% to 76.7%,
Over 13.5 from 15.1% to 17.4%. Goals stay Poisson with the Dixon-Coles
correction.

**`rho` differs by league and that is correct.** Fitted by log-likelihood grid.
Real data gave -0.13 for the Premier League (with home advantage applied once)
and 0.0 for Liga Portugal, which genuinely showed no low-score correction last
season. A synthetic-data test returns exactly 0.0, which validates the fitter,
since that test data is independent-Poisson by construction.

**Home advantage is applied once.** It comes from each competition's own
decay-weighted home and away goal averages. The scripts used to multiply a
per-league constant on top (1.20 to 1.35) as well, which counted it twice:
Premier League home goals came out at 1.86 against 1.53 observed. Don't add a
multiplier back.

## Validation state

Both league scripts have been run against live data and the output sanity
checks out. Premier League: 380 matches fetched, rebuilt table gives Arsenal
champions on 85 points with West Ham, Burnley and Wolves relegated. Liga
Portugal: 306 matches, Porto champions on 88 points, Tondela and AVS down.

The rebuilt final table is the built-in fetch sanity check. If the champions
are wrong, the pull is broken. Stop and fix that before reading any prediction.

`club_backtest.py` replays 2019-20 to 2025-26 for the Premier League,
Championship and Liga Portugal against Pinnacle's early and closing odds. The
models are significantly less accurate than the market, and closing-line value
is negative in every league, market and threshold tested. There is no edge.
The Champions League can't be tested this way, because there are no free CL
odds.

Also tested in a sandbox without network access: name resolution against
realistic API spellings, promoted-prior fallthrough, negative binomial against
Poisson tails, full-season analysis on synthetic 306 and 380 match seasons, and
graceful degradation when the API is unreachable.

## Known limitations, kept in the README on purpose

Matchday one predictions project a May snapshot onto an August squad, across a
summer of transfers and managerial changes. It is the least reliable week of
the season and no amount of arithmetic fixes that.

Small samples still overfit. In the league scripts `confidence_flag` fires
`OVERFITTED`, `EXTREME RATIO` or `HIGH-VARIANCE` accordingly; trust goals
markets over result markets when it does. In the Champions League the weak
spot is early in the league phase. Clubs back after a long absence are held
near average, and strong newcomers from outside the big five are rated like
minnows until they have played (`docs/MODEL_CHANGES.md`, section 7).

The model cannot see injuries, rotation, motivation or team news.

All probabilities are estimates. Accumulators and bet builders are structurally
negative expected value once the bookmaker's margin and the correlation between
legs are priced in.

## If you are extending this

Run `club_backtest.py` before and after any modelling change. A change worth
keeping improves log loss walk-forward, and anything meant for betting has to
show positive closing-line value there first.

Worth doing, in rough order of value:

A shared `common.py` for the Poisson and Dixon-Coles core, which is currently
copy-pasted across all four scripts. It touches every file, so do it as its own
commit, and keep the per-league constants separate where they are.

A `--season` and `--competition` command line interface via `argparse`, instead
of editing module constants by hand.

Do not commit `.env`, generated CSV exports or output folders. `.gitignore`
covers all three.
