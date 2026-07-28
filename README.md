# Football prediction models

## Overview

A set of Python scripts that price football matches from historical results.
They pull finished matches from the [football-data.org](https://www.football-data.org)
v4 API, work out how strong each team has been in attack and defence, and turn
that into probabilities for the usual betting markets.

`regular_prediction_model.py` is the standard model. It is the base version,
and the one to start with for any competition that does not have a script of
its own. The other three scripts take the same core approach and refine it for
a particular league, competition or market. They are not interchangeable: they
use different constants, different priors, and in places different modelling
layers, because the competitions behave differently.

These are useful for research and for thinking about where a bookmaker's price
looks wrong. They are not guarantees, and they are not a betting system.

On the question of accuracy: I have tracked roughly a 70 to 80 percent
per-match hit rate in my own use. That is my own informal record-keeping over
matches I happened to bet or follow, not a formal backtest, and it is not a
promise about future results. It is not corrected for which markets I picked or
which matches I skipped. Take it as a rough personal figure rather than a
measured one.

## What is in the repo

### `regular_prediction_model.py`

The standard general model. Dixon-Coles goals model with a corners predictor on
top. Outputs 1X2, over/under 1.5, 2.5 and 3.5, both teams to score, the most
likely scoreline, double chance, and corner markets. It is the only script that
can read real corner counts from a second API if you have a key for one. Set
the competition code and fixture list in `main()`.

### `pl_model.py`

A refined Premier League version. Adds a full analysis of a completed season,
time-decay weighting so late-season form counts for more, sample-size
shrinkage, a half-time model, a negative binomial on corner totals, priors for
promoted clubs, and careful handling of club names. Home advantage is tuned for
the league.

### `liga_portugal_model.py`

A refined Liga Portugal version, built on the same two-stage shape as the
Premier League script. Tuned separately: home advantage is noticeably stronger,
the low-score correction fitted differently on real data, and name matching has
to cope with accented spellings rather than clubs sharing common words.

### `cl_corners_model.py`

A refined variant for the Champions League and the Championship, aimed at goals
and corners in a simpler output format. Cut down on purpose, since knockout and
cup competitions do not give you a full league season to fit against. It drops
the time-decay, shrinkage and half-time layers, and prints a small correct
score grid the league scripts do not.

## How the models work

Each script looks at what teams have actually done: how much they scored, how
much they conceded, home and away. From that it estimates an attacking rating
and a defensive rating for every side, relative to the league average, plus a
home advantage figure for the competition. Put two teams together and those
ratings give an expected goals figure for each, which turns into a probability
for every plausible scoreline, and from there into prices for the markets.

Two adjustments sit on top of that. Low-scoring matches happen slightly more
often than the plain arithmetic suggests, particularly 0-0 and 1-1, so there is
a correction for that, fitted from the season's own data rather than assumed.
Corners behave differently from goals, being more spread out, so the league
scripts widen the range on corner totals instead of treating them like goals.

The refined scripts add layers the standard model does not have. Weighting
recent matches more heavily than early-season ones. Reducing how far a team's
rating can drift when it has played few matches. A separate model fitted on
half-time scores, for half-time markets. Newly promoted clubs get an assumed
weak-side rating, since they have no top-flight record to fit against.

Reliability improves once a season is under way and there is enough of a match
record to fit. Early-season predictions are weak, and matchday one is the worst
of the lot: it projects a May snapshot onto an August squad, across a summer of
transfers and managerial changes. Promoted clubs stay shaky for longer, because
their rating is an assumption rather than a measurement until they have played
enough games. The scripts label those predictions rather than hiding them.

## Data limits

Worth being straight about what the data does and does not contain.

The free football-data.org tier gives scores and half-time scores, plus the
date and matchday. That is all. No shots, no possession, no cards, and no
per-player data. The rate limit is 10 requests a minute.

Corners are not in the free feed. Unless you have richer data, corner output is
a proxy worked out from goal ratings, on the reasoning that a side that
outscores its opponent usually wins more corners too. The scripts label this in
their output, and switch to real counts on their own if better data becomes
available. It is directionally useful and no more than that.

Player props are not supported and cannot be. There is no per-player data in
the feed, so any goalscorer or shots-on-target number would be invented.

The models cannot see injuries, suspensions, team news, line-ups, rotation,
weather, motivation or tactical changes. A side resting players before a cup
tie looks exactly like a side at full strength. Anything of that sort you have
to weigh yourself, on top of what the model gives you.

## Setup

You need Python 3.8 or newer. There is nothing to install: the scripts use only
the standard library, so there is no `requirements.txt`.

Get a free API key from
<https://www.football-data.org/client/register>, then copy the example
environment file and put your key in it:

```bash
cp .env.example .env
```

Open `.env` and set `FOOTBALL_DATA_KEY` to your key. The file is gitignored, so
it will not be committed. If you would rather not use a file, the scripts read
the same variable from the environment:

```bash
export FOOTBALL_DATA_KEY=your_key_here
```

There is one optional key, `APISPORTS_KEY`. Only `regular_prediction_model.py`
reads it, and only to pull real corner counts from API-Football. Leave it blank
and that script falls back to the corner proxy. The other three scripts ignore
it entirely.

Then run whichever script you want:

```bash
python regular_prediction_model.py
```

```bash
python pl_model.py
```

```bash
python liga_portugal_model.py
```

```bash
python cl_corners_model.py
```

Fixtures and settings are constants near the top of each file, or in `main()`
in the case of the standard model. The one setting worth knowing about is
`SEASON`, which is keyed by the season's starting year, so 2025 means the
2025-26 season. Always set it explicitly. Leaving it out asks the API for the
current season, which in August has no finished matches, and the scripts will
quietly rate every team as average instead of failing.

`SETUP.md` covers the same ground with a smoke test, and is the one to follow
on a fresh clone.

## Sanity checks

The two league scripts rebuild the final league table from the match record
they fetched, and print it before anything else. That is not decoration, it is
the check that the data pull worked.

Look at it every time. If the champions are wrong, or the relegated clubs are
wrong, then the fetch is broken, most likely because the season parameter is
pointing somewhere unintended. The ratings will still compute and the
predictions will still print, and they will all be worthless. Fix the pull
before reading anything below the table.

For the 2025-26 season, the Premier League script should show Arsenal top on 85
points with West Ham, Burnley and Wolves relegated, and the Liga Portugal
script should show Porto top on 88 points.

The standard model and the Champions League script do not do a full season
analysis, so they do not print a table. Check the fetched match count looks
sensible instead.

## Disclaimer

Personal research and entertainment only. Nothing here is betting advice.
Betting carries risk and you can lose money. Past performance does not
guarantee future results.
