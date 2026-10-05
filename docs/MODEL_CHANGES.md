# Model changes, October 2026

What changed in the four scripts, why, and the evidence for each change. Every modelling change was
tested walk-forward before it was adopted: each week predicted from earlier matches only, compared
as paired per-match log loss, where negative means better and *t* is the paired t-statistic. The
club backtest (`club_backtest.py`, results in `docs/CLUB_BACKTEST_RESULTS.md`) is the main test bed.

**The headline result: these models have no edge over the market.** They are significantly less
accurate than Pinnacle's prices, including the early price you would bet at. Closing-line value is
negative in every league, market and threshold tested. The changes below make the models better
calibrated and stop them failing silently. They do not make them profitable.

## Summary

| Change | Scripts | Evidence |
|---|---|---|
| Home advantage applied once, not twice | all four | in-sample PL home goals 1.55 predicted vs 1.53 observed (was 1.86) |
| Zero-goal collapse fixed (pseudo-goal, floor, shrinkage) | all four | the CL script crashed after matchday 1; now runs |
| CL/ELC fixture names resolved, with warnings | `cl_corners_model.py` | 3 of 8 CL names silently got neutral ratings |
| Last season plus the current one fitted | all four | Championship 1X2 −0.0099 (t −3.7); CL 1X2 −0.044 (t −6.3); standard model PL −0.044 (t −10.0) |
| Fixtures fetched from the API | all four | — |
| Measured, fading priors for new sides | `pl_model.py`, `liga_portugal_model.py`, `cl_corners_model.py` | PL 1X2 −0.0058 (t −4.4); Championship −0.0034 (t −3.2); CL −0.022 (t −2.4) |
| Longer CL history for returning clubs | `cl_corners_model.py` | partly validated, see section 7 |
| Club backtest against Pinnacle odds | `club_backtest.py` | no edge, see section 9 |
| Seasons from today's date; rate-limit retry everywhere | all four | see section 10 |

## 1. Home advantage applied once

All four scripts multiplied the league's home-goal average by a home-advantage constant (1.20 to
1.35). The home-goal average already contains home advantage, so it was counted twice. The scripts'
own in-sample fits on 2025-26 show the effect:

| | Home goals pred / obs | Home win pred / obs |
|---|---|---|
| Premier League, before | 1.86 / 1.53 | 48.4% / 42.6% |
| Premier League, after | 1.55 / 1.53 | 41.8% / 42.6% |
| Liga Portugal, before | 2.05 / 1.50 | 52.7% / 41.2% |
| Liga Portugal, after | 1.58 / 1.50 | 44.2% / 41.2% |

The fits now use `home_advantage=1.0`, and the decay-weighted home and away goal averages carry each
competition's measured split. The `HOME_ADV`, `CL_HOME_ADV` and `ELC_HOME_ADV` constants are gone.
The corner proxy keeps its own `HOME_CORNER_ADV`, applied once.

## 2. Zero-goal collapse

The rating update is `(goals / expected) ** lr`. For a side on 0 goals that is exactly 0, whatever
`lr` is. In `cl_corners_model.py`, five CL sides had not scored after matchday 1, so their attack
went to 0 and `log(0)` crashed the script.

- **The fix:** every ratio now adds one pseudo-goal at the expected rate, `(goals + 1) / (expected +
  1)`. That is negligible over a season and removes the collapse.
- **Backstops:** `cl_corners_model.py` also gets the league scripts' sample-size shrinkage, and a
  0.1 floor.
- **The league scripts:** they get the same pseudo-goal, because fitting the current season (section
  4) exposed promoted sides to the same collapse. The walk-forward effect was neutral to positive:
  PL O/U 2.5 −0.0060 (t −3.4), 1X2 noise.

## 3. Name resolution in `cl_corners_model.py`

Fixture names were matched exactly, so 'Bayern München', 'Barcelona' and 'Atletico Madrid' (the
API has 'Bayern', 'Barça' and 'Atleti') silently got neutral 1.0 ratings. Names are now matched
exactly, accent- and punctuation-insensitively, or via an `ALIASES` table. There is deliberately no
fuzzy guessing. An unmatched name prints a `[warn]` line instead of passing silently.

## 4. Last season plus the current one

**League scripts.** `pl_model.py` and `liga_portugal_model.py` used to fit the completed season
only, so in October they ignored everything played since August. Stage one still analyses the
completed season (`SEASON`, with the rebuilt-table sanity check). Stage two now fits that season
plus the current one (`CURRENT = SEASON + 1`), and the existing time-decay weights recent matches
most.

**Championship.** It is now fitted the same way, on `ELC_SEASON - 1` plus `ELC_SEASON`, with
time-decay, shrinkage and a fitted rho. Club backtest: 1X2 log loss 1.0557 vs 1.0656 for the old
current-season-only fit (−0.0099, t −3.7).

**Champions League.** After one matchday a current-season-only fit knows almost nothing (it had
Viking ahead of Bayern). The CL is now fitted on previous seasons plus the current one (see section 7
for the window). Walk-forward over 2024-25 and 2025-26, 378 matches, against the old current-season
fit:

| | First 4 weeks (72) | Weeks 5+ (306) | All (378) |
|---|---|---|---|
| Exact-score log-likelihood | −0.039 (t −1.5) | −0.061 (t −4.2) | −0.057 (t −4.5) |
| 1X2 log loss | −0.033 (t −2.4) | −0.046 (t −5.8) | −0.044 (t −6.3) |

This was the largest single improvement found.

## 5. Fixtures from the API

All four scripts now predict every `SCHEDULED`/`TIMED` match from today through `DAYS_AHEAD` (7)
days. Use `--days N` to change the window, for example `--days 14` to reach the next CL matchday
over an international break. Team names come from the API, so they resolve exactly. Listing games in
`FIXTURES` (`CL_FIXTURES` / `ELC_FIXTURES` in `cl_corners_model.py`) predicts those instead.

## 6. Measured, fading priors for new sides

The old promoted-side prior (0.80 attack / 1.20 defence) was a judgment call. It also applied only
before a side's first match, after which the fit shrank the side toward league average like
everyone else. Promoted sides were treated as average from game two.

**Measured values.** First-season goals scored and conceded per game relative to the league
average, from football-data.co.uk 2019-20 to 2025-26 (defence above 1 means it concedes more):

| New side | Sides | Attack | Defence |
|---|---|---|---|
| Promoted to the Premier League | 21 | 0.69 | 1.25 |
| Promoted to Liga Portugal | 19 | 0.78 | 1.08 |
| Relegated to the Championship | 21 | 1.18 | 0.79 |
| Promoted to the Championship | 21 | 0.88 | 1.10 |
| CL newcomer from outside the big five leagues | 16 | 0.63 | 1.24 |

**How they are applied.**
- **Fading target:** a new side starts at its prior and is shrunk toward a target that fades from
  the prior to 1.0 as it plays, worth `PRIOR_GAMES` = 5 games of its own results. A prior that never
  fades was tried and cost accuracy later in the season.
- **Detection:** promoted sides are teams in this season's data but not last season's, with no extra
  API call. Relegated Championship sides are those in last season's Premier League (one API call).
- **CL newcomers:** a CL newcomer's country comes from the API's CL team list. Newcomers from England,
  Spain, Germany, Italy or France stay average, because a prior for them was not reliably better.
  AS Monaco counts as France.
- **Normalisation:** ratings stay normalised to a geometric mean of 1. Rescaling to the priors' mean
  was tried and dropped, because these priors aren't reciprocal and it shifted every match's goal
  level by about 1%.

**Evidence** (priors re-measured with each test season left out):

| | 1X2 (t) |
|---|---|
| Premier League, all matches (2660) | −0.0058 (−4.4) |
| Premier League, promoted-side games, first 10 weeks (160) | −0.045 (−2.9) |
| Liga Portugal, all matches (2142) | −0.0008 (−0.9): no measurable effect, kept for consistency |
| Championship, all matches (3864) | −0.0034 (−3.2) |
| CL, all matches, 2024-25 and 2025-26 (378) | −0.022 (−2.4) |

The half-time fits use the same priors. That part is not separately tested, because the backtest
data has no half-time odds.

## 7. CL history window

A CL newcomer is a side with no CL match in the fitted window. With one previous season, clubs that
qualify most years but missed a season, such as Porto, Feyenoord and Shakhtar, got the weak
newcomer prior. Real Betis v Porto was 80/13/7.

`CL_HISTORY = 3` previous seasons are now fitted (2023-24 to 2025-26 for 2026-27), with time-decay,
so those clubs are rated from their own CL results. Betis v Porto is now 61/18/21.

- **Partly validated:** two previous seasons against one, walk-forward on 2025-26 (189 matches): 1X2
  −0.012 (t −1.2) overall and −0.054 (t −2.0) in the first four weeks. That is consistent, but not
  significant overall.
- **The third season can't be tested.** The free tier serves the CL only from 2023-24, so no test
  season has three seasons of history before it. It is adopted by extension. In practice it only
  reaches clubs that have been out for two seasons, and those matches carry about 0.2 weight each
  after decay.
- **Side effect:** a side whose only CL data is old is held near average, not near the newcomer
  prior. Slovan Bratislava lost all eight 2024-25 games, but Bratislava v Stuttgart is 40/20/40.
  That is probably too kind to Bratislava.

## 8. API usage

The free tier allows 10 requests a minute. `cl_corners_model.py` now makes 9 a run, or 10 with
`--days 10` or more:
- **Requests:** four CL seasons, the CL team list, two Championship seasons, last season's Premier
  League, and fixtures.
- **Corners:** they reuse the match lists already fetched.
- **Fixtures for both competitions:** they come from one cross-competition request per 10 days. That
  endpoint rejects longer ranges, and its `dateTo` is exclusive.
- **Rate limit:** if the limit is hit, the script waits for the counter to reset and retries once.
  Before, the error was caught and the script silently fitted without that season.

The other scripts make 3 or 4 requests a run, and since section 10 they retry the same way.

## 9. Club backtest

`club_backtest.py` replays 2019-20 to 2025-26 for the Premier League, Championship and Liga Portugal
against Pinnacle's early and closing prices (football-data.co.uk). Each week is predicted from
earlier matches only, using each script's own fitting and scoring code.

| | 1X2 log loss: model / Pinnacle early / Pinnacle close |
|---|---|
| Premier League (`pl_model.py`) | 0.985 / 0.962 / 0.958 |
| Liga Portugal (`liga_portugal_model.py`) | 0.957 / 0.925 / 0.921 |
| Championship (`cl_corners_model.py`) | 1.052 / 1.040 / 1.038 |

- **Accuracy:** every model is significantly less accurate than the market (t +5 to +8).
- **No added information:** blending the model into the early price never helps, so the model
  carries nothing the market lacks.
- **Closing-line value:** it is negative in every cell, −2.6% to −4.6%, which is the robust sign of a
  less-informed model. ROI at early prices is negative too.
- **The gate:** a future change should show positive closing-line value in `club_backtest.py`
  across leagues before anyone stakes money on it.
- **Not testable:** there are no free CL odds, so the CL fit can't be tested against the market.

## 10. Seasons, rate limits and the standard model

**Seasons follow today's date.** The season constants used to need bumping every August, and
forgetting gave no error. Each script now has `current_season()`, which returns the starting year of
the season in progress, or of the next one from June. The league scripts' `SEASON` is the last
completed season, and `cl_corners_model.py`'s `ELC_SEASON` and `CL_SEASON` are the current one. Every
API request still passes the season explicitly. Pin any of them to a year to override.

**API fixtures resolve by exact name.** Fixtures fetched from the API carry the data's own team
names, so the league scripts now match them exactly and never by shared words. A name with no data is
a side with no match yet, and it gets the promoted prior. The hand-kept `PROMOTED` lists now only
matter for hand-typed fixtures. `liga_portugal_model.py`'s word matcher had no promoted guard at all,
so this closes a real gap there.

**Rate-limit retry in every script.** The league scripts and the standard model used to catch every
API error and carry on without that season's data. All four now wait for the limit to reset and
retry once.

**`regular_prediction_model.py` brought up to the league scripts' fit.**
- **What it was missing:** it fetched matches with no season parameter, so it fitted the current
  season only. It had no prior for new sides, and hand-typed fixture names had to match exactly.
- **What it does now:**
  - **Seasons:** it fits last season plus the current one, time-decayed.
  - **New sides:** a side with no match last season starts from a fading prior,
    `NEWCOMER_ATT/DEF` = 0.73/1.16, between the measured Premier League and Liga Portugal values.
  - **Names:** hand-typed names resolve exactly, accent-insensitively or via `ALIASES`, with a
    warning if they don't.

| Club backtest, 1X2 log loss | Before | After | Δ (t) | League script |
|---|---|---|---|---|
| Premier League | 1.0327 | 0.9854 | −0.044 (−10.0) | 0.9847 |
| Liga Portugal | 1.0394 | 0.9570 | −0.081 (−14.8) | 0.9571 |

- **Where the gain comes from:** almost all of it is the second season. The prior's values come from
  these two leagues, so that part is not an out-of-sample test.
- **Against the market:** like every model here, it has negative closing-line value.

## Known limitations

- **No edge.** See section 9.
- **CL returners and strong newcomers are rated crudely.** Weak returners like Slovan Bratislava come
  out too generous. Strong clubs new to the window from strong non-big-five leagues, such as
  Fenerbahçe, are rated like minnows until they have played a few games. A proper fix needs outside
  data such as UEFA coefficients. Treat early-league-phase CL prices on those sides with caution.
- **`regular_prediction_model.py` uses a generic newcomer prior.** It isn't measured for your
  league, and in a second division it underrates relegated clubs.
- **Corners are a goal-rating proxy** unless you have paid corner data.
- **Hand-typed fixtures** still rely on the `PROMOTED` name lists in the league scripts. Update them
  each summer if you type fixtures in rather than fetching them.
