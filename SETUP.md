# Setup

Run everything from the repository root. All four scripts sit in the root, so
there are no package paths to worry about.

## Before the first commit

Keep your key out of the repository. It belongs in `.env` or in an environment
variable, never written into a script.

This matters more than it might seem, because removing a key from a file does
not remove it from git history. If a key is ever committed, `git rm` will not
undo it: the history needs rewriting with something like `git filter-repo`, or
starting again from a fresh repo, and the key needs regenerating at
football-data.org either way. Easier to get it right first time.

Two checks worth running before you commit, both of which should come back
empty:

```bash
grep -rn "API_KEY *= *\"" *.py
```

```bash
git ls-files | grep -E "\.env$|\.csv$"
```

The first looks for a key written directly into a script. The second looks for
a `.env` file or a generated CSV export that has been staged by accident. If
either prints anything, stop and deal with it before going further.

## Requirements

Python 3.8 or newer, and nothing else. The scripts use only the standard
library, so there is no `requirements.txt` and no virtual environment needed
unless you want one.

## Getting a key

Register for a free key at <https://www.football-data.org/client/register>.

The free tier gives scores and half-time scores, and allows 10 requests a
minute. That is enough for all four scripts. Corners sit behind a paid
statistics add-on; without it the corner output is a proxy derived from goal
ratings, which the scripts label in their output.

## Configuring the key

Copy the example file and edit it:

```bash
cp .env.example .env
```

Set `FOOTBALL_DATA_KEY` to your key. `.env` is gitignored, so it will not be
committed.

`APISPORTS_KEY` is optional. Only `regular_prediction_model.py` reads it, to
pull real corner counts from API-Football. Leave it blank and that script uses
the corner proxy instead. The other three scripts ignore it.

The scripts check the environment first and fall back to `.env`, so this works
just as well:

```bash
export FOOTBALL_DATA_KEY=your_key_here
```

If neither is set, the script exits straight away with a message telling you
which variable is missing, rather than failing somewhere further in.

## Smoke test

First check all four scripts compile:

```bash
python -m py_compile pl_model.py liga_portugal_model.py cl_corners_model.py regular_prediction_model.py club_backtest.py
```

Silence means they compiled. Then run the Premier League script, which
exercises the most machinery. It predicts every Premier League match scheduled
in the next 7 days (add `--days 14` if that window falls in an international
break):

```bash
python pl_model.py
```

## What a good run looks like

The script fetches the season, then prints a summary of it, then the fitted
ratings, then the predictions. Working through it in order:

It should report 380 finished matches fetched for a completed Premier League
season, and write a CSV of the match record next to the script.

It then prints the final table, rebuilt from the matches it just fetched. For
2025-26 that means Arsenal top on 85 points, with West Ham, Burnley and Wolves
in the bottom three. **This is the real test.** If the champions or the
relegated clubs are wrong, the data pull is broken, most likely because
`SEASON` is pointing at the wrong year. Everything after this point will still
compute and print, and will be worthless. Fix the fetch before reading on.

The Liga Portugal script is the same idea: 306 matches, and Porto top on 88
points.

After the table it fetches the current season's finished matches and lists
the promoted clubs it found (teams in this season's data but not last
season's). Then come the fitted attack and defence ratings per club, fitted on
both seasons, then the predictions themselves. A few things in that output are
expected rather than faults:

Promoted clubs are rated from their current-season matches, starting from a
measured weak-side prior that fades as they play. Before a promoted club's
first top-flight match, its predictions are marked `PRIOR-BASED`: the model is
using the prior alone. Treat them as assumption rather than measurement.

Corner output is marked as a goal-rating proxy unless you have the paid
statistics add-on. That label is accurate and should stay.

The name resolution section lists any fixture spelling that had to be mapped to
a different spelling in the API data. Promoted clubs will show as having no
data for the previous season, which is expected. Anything else showing as not
found is a spelling problem in the fixture list, and worth fixing, because that
club will silently fall back to the promoted-side prior.

If the API is unreachable the scripts say so and carry on with neutral ratings
rather than crashing. That output is not meaningful, so do not read predictions
from a run that reported an API error. The usual cause is the rate limit of 10
requests a minute. The scripts wait for the limit to reset and retry once, so
an error here usually means the API is down or the key is wrong.

## Backtest

`club_backtest.py` replays 2019-20 to 2025-26 against Pinnacle's odds. The first
time, download the 24 football-data.co.uk CSVs (about 4 MB, into `data/clubs/`,
gitignored as CSVs):

```bash
python club_backtest.py --fetch
```

After that, run it without `--fetch`. It makes no API calls, but it imports the
model scripts, so `FOOTBALL_DATA_KEY` still has to be set. It takes about two
minutes and rewrites `docs/CLUB_BACKTEST_RESULTS.md`. The downloads are checked
against stored checksums, and a file football-data.co.uk has since corrected is
flagged with a warning.

## First commit

Only once the key rotation is done and the two checks above come back empty:

```bash
git init
```

```bash
git add .
```

```bash
git status
```

Read that status output properly and confirm `.env` and any generated CSV files
are not staged. Then commit:

```bash
git commit -m "Football prediction models"
```

## Creating the remote

Private is the safer default, given the key history:

```bash
gh repo create football-models --private --source=. --push
```

Or set the remote by hand:

```bash
git remote add origin git@github.com:<user>/football-models.git
```

```bash
git branch -M main
```

```bash
git push -u origin main
```

## After pushing

Confirm nothing unwanted made it in:

```bash
git ls-files
```

Read the list. It should be the four scripts, `club_backtest.py`, the
markdown files (including `docs/`), `.env.example` and `.gitignore`. If a `.env` or a CSV export is in there, the
key needs rotating again and the history needs rewriting.
