# Maintenance notes

Notes for anyone picking the scripts up later, including me. Several decisions
in here look like bugs and are not. Read this before changing the modelling
code.

For what the repo is and how to run it, see `README.md` and `SETUP.md`.

## The scripts

Four scripts, all in the repository root, all standard library only.

`regular_prediction_model.py` is the standard model and the base the others are
refined from. It is not a legacy file. If a competition has no script of its
own, this is the one to point at it.

`pl_model.py` and `liga_portugal_model.py` are the two mature league scripts,
both run against live data. `cl_corners_model.py` is a deliberately simpler
variant for the Champions League and the Championship.

Do not merge these into one architecture without deciding to do so on purpose.
They have different priors, different home advantage constants and different
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

**`SEASON` must be explicit.** football-data.org keys a season by its starting
year, so `2025` is the 2025-26 season. Requesting without the parameter returns
the current season, which in August has zero finished matches and silently
produces neutral 1.0 ratings for everyone. This is the easiest way to get
confident-looking rubbish out of these scripts.

**Promoted-side priors.** Newly promoted clubs appear nowhere in last season's
top-flight data. Rating them a neutral 1.0 would put them mid-table, so they
get `PROMOTED_ATT = 0.80` and `PROMOTED_DEF = 1.20` instead, and print
`PRIOR-BASED`. This is a judgment call, not a fitted value. Predictions for
those fixtures are partly manufactured: the model is echoing the assumption
back. Currently Coventry City, Ipswich Town and Hull City in the Premier
League, and Marítimo and Académico de Viseu in Liga Portugal.

**Name resolution is defensive on purpose.** Naive token matching silently
mapped Coventry City and Hull City to Man City, on the shared token "city",
neither club being in last season's data. That would have rated a promoted side
as Manchester City while printing `RELIABLE`. Three guards, all load-bearing:
promoted clubs short-circuit to the prior before any matching runs, an explicit
`ALIASES` table does the real work, and a match on a weak token alone ("city",
"united", "town", "albion") is rejected rather than guessed. Keep all three.
Removing any one brings back silent, confident-looking errors.

**Negative binomial for corner totals.** Corner counts are over-dispersed and
plain Poisson runs its tails too tight. `nb_pmf(k, mu, phi)` uses
`phi = variance/mean`, defaulting to 1.25 and re-estimated from data when real
corners exist. Measured effect at mu=10.2: Over 7.5 moved from 79.7% to 76.7%,
Over 13.5 from 15.1% to 17.4%. Goals stay Poisson with the Dixon-Coles
correction.

**`rho` differs by league and that is correct.** Fitted by log-likelihood grid.
Real data gave -0.1 for the Premier League and 0.0 for Liga Portugal, which
genuinely showed no low-score correction last season. A synthetic-data test
returns exactly 0.0, which validates the fitter, since that test data is
independent-Poisson by construction.

**Home advantage is per league**: 1.20 Premier League, 1.30 Liga Portugal,
1.20 Champions League, 1.35 Championship.

## Validation state

Both league scripts have been run against live data and the output sanity
checks out. Premier League: 380 matches fetched, rebuilt table gives Arsenal
champions on 85 points with West Ham, Burnley and Wolves relegated. Liga
Portugal: 306 matches, Porto champions on 88 points, Tondela and AVS down.

The rebuilt final table is the built-in fetch sanity check. If the champions
are wrong, the pull is broken. Stop and fix that before reading any prediction.

Also tested in a sandbox without network access: name resolution against
realistic API spellings, promoted-prior fallthrough, negative binomial against
Poisson tails, full-season analysis on synthetic 306 and 380 match seasons, and
graceful degradation when the API is unreachable.

## Known limitations, kept in the README on purpose

Matchday one predictions project a May snapshot onto an August squad, across a
summer of transfers and managerial changes. It is the least reliable week of
the season and no amount of arithmetic fixes that.

Small-sample competitions still overfit. The Champions League is roughly 180
matches, and `confidence_flag` fires `OVERFITTED`, `EXTREME RATIO` or
`HIGH-VARIANCE` accordingly. Trust goals markets over result markets when it
does.

The model cannot see injuries, rotation, motivation or team news.

All probabilities are estimates. Accumulators and bet builders are structurally
negative expected value once the bookmaker's margin and the correlation between
legs are priced in.

## If you are extending this

Worth doing, in rough order of value:

A shared `common.py` for the Poisson and Dixon-Coles core, which is currently
copy-pasted across all four scripts. It touches every file, so do it as its own
commit, and keep the per-league constants separate where they are.

A `--season` and `--competition` command line interface via `argparse`, instead
of editing module constants by hand.

Do not commit `.env`, generated CSV exports or output folders. `.gitignore`
covers all three.
