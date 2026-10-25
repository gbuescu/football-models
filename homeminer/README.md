# HomeMiner

HomeMiner benchmarks a home Windows PC's CPU and GPU, separately and safely,
then works out what that hardware could earn in BTC. It only uses measured hash
rates and timestamped public prices. If it cannot estimate earnings reliably, it
says so and shows no number.

It was built for one PC: Intel Core i5-14400F, NVIDIA GeForce GTX 970 (4 GB),
MSI PRO B760M-P, Windows 11 Pro, with a BTC payout. It runs on other Windows
10/11 PCs with an NVIDIA card, but the guidance is written for that one.

**Read [docs/FEASIBILITY.md](docs/FEASIBILITY.md) first.** In short:

- The CPU (RandomX through XMRig) is the only device with a credible route to
  BTC: selling hash power on a marketplace such as NiceHash, which pays in BTC.
  Whether NiceHash still buys RandomX is checked live on your PC.
- The GTX 970 is close to end of life for mining: not supported by NiceHash's own
  GPU miners, too little memory for DAG algorithms, and on NVIDIA's last Maxwell
  driver branch. HomeMiner can still measure its RandomX speed offline.
- Mining Bitcoin directly (SHA-256) on this hardware would earn well under one
  satoshi a day. It is included for education only.
- Free electricity removes one cost. It does not make mining profitable.

## What HomeMiner will and will not do

| It will | It will not |
|---|---|
| Read hardware facts (read-only) | Change drivers, BIOS, voltages, clocks or power limits |
| Download official miner releases **after you approve**, verified against `SHA256SUMS` (and GPG if available) | Run any miner it did not install and verify, or write its own miner |
| Run offline benchmarks one device at a time, under conservative limits | Run CPU and GPU benchmarks together (unless you change a setting, and then results are flagged) |
| Stop work above temperature limits, on request, or by emergency stop | Disable or add exclusions to Windows Defender |
| Estimate earnings from measured hash rates and timestamped prices | Invent figures, or present a benchmark as guaranteed income |
| Ask for a BTC address only when a payout route needs one | Ask for seed phrases, private keys or exchange passwords |
| Start real mining only after config approval **and** a typed confirmation each session | Withdraw, trade or send BTC, or keep mining after it closes |
| Keep results, ledger and any saved address in `%LOCALAPPDATA%\HomeMiner` | Write wallet details to the repository, logs or diagnostic reports |
| | Add services, scheduled tasks, startup entries or other persistence |

## Quick start (Windows, PowerShell)

Requires Python 3.11 or newer (from python.org, or `winget install --id Python.Python.3.12 -e`).

```powershell
cd path\to\homeminer
# If scripts are blocked, allow them for this window only:
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass

# 1. Read-only hardware report (no Python needed)
.\scripts\Get-HomeMinerDiagnostics.ps1

# 2. Python environment (installs psutil; no mining software)
.\scripts\Setup-HomeMiner.ps1 -Dev

# 3. Open the interface
.\scripts\Start-HomeMiner.ps1
```

In the interface, run **discovery**, then **Dry run** on the CPU card to see
every check and the exact command. A real benchmark needs XMRig, which is only
installed when you approve it:

```powershell
.\scripts\Install-Miner.ps1 -Miner xmrig        # asks you to type APPROVE
.\scripts\Install-Miner.ps1 -Miner xmrig-cuda   # optional, for the GPU benchmark
```

Then press **Start CPU benchmark**, then **Refresh market data** to see the route
comparison and estimates.

## The interface

A local page served on `127.0.0.1` only, protected by a one-time session token.

- **Hardware status:** discovery results and limitations, each value labelled
  with its source (`windows-cim`, `registry`, `nvidia-smi`, `psutil` or `lookup`).
- **CPU benchmark / GPU benchmark:** compatibility, the latest result (hash rate,
  duration, peak temperature and utilisation, power and whether it was measured
  or estimated, memory requirement, errors), dry run, start and stop.
- **Monitoring:** live CPU/GPU utilisation, temperatures, GPU power and mining
  hash rate, sampled every 2 seconds.
- **Route comparison:** NiceHash (Route A) against direct SHA-256 mining
  (Route B), with fees, payout rules, requirements and risks.
- **Assumptions:** electricity price (default €0.00/kWh), wear allowance,
  wall-meter readings and NiceHash fees, all editable; market data with sources
  and fetch times.
- **Estimates:** gross, each fee, net per day/month/year in BTC and EUR, payout
  timing, break-even electricity price, and why an estimate is unreliable when
  it is.
- **Ledger:** estimated earnings, unpaid balance, confirmed marketplace earnings
  and BTC received in your wallet, kept apart.
- **Mining controls:** start, pause, resume, stop, gated as described below.
- **EMERGENCY STOP:** always on screen (also Ctrl+Shift+X). It kills every
  tracked miner and any process running from `tools\`. If the interface itself
  hangs, run `.\scripts\Stop-HomeMiner.ps1` or close the HomeMiner window: on
  Windows miners are tied to HomeMiner's process and end with it.

## Real mining is off by default

A mining session starts only when all of these hold:

1. You set `[mining] approved = true` in `config\homeminer.toml` yourself.
2. You type the confirmation phrase in the interface, every session.
3. You enter your NiceHash mining address, which passes format, checksum and
   network checks.
4. XMRig was installed by HomeMiner and its file hashes still match.
5. HomeMiner is not running as Administrator.
6. No benchmark is running.
7. Fresh market data shows NiceHash currently lists RandomX.
8. The stratum URL points at `nicehash.com`.

## How estimates are built

```
gross/day   = measured hash rate × NiceHash price for that algorithm
net/day     = gross − XMRig donation (1%) − NiceHash fee (2%) − withdrawal fee spread over time
EUR/day     = net × BTC/EUR price − electricity (W × 24 h × €/kWh) − wear allowance
```

- **Hash rate:** a local benchmark or a pool-verified figure you enter. Pool
  figures take priority. An assumed rate is never used for Route A.
- **Prices:** NiceHash's public API, timestamped. HomeMiner reads its price
  units, then checks them every refresh: NiceHash's SHA256 price must sit near
  Bitcoin's network hashprice (from mempool.space or blockchain.info). If it does
  not, estimates are withheld.
- **Unreliable estimates show no numbers:** this happens with no measured hash
  rate, an algorithm NiceHash does not list, prices more than a day old, or a
  failed unit check. Weaker inputs (estimated power, fees from secondary
  sources, quotes older than 30 minutes, local-only benchmarks) give warnings.
- **Excluded costs** (listed in the interface): hardware wear, heat and noise,
  solar export opportunity cost, taxes, BTC price and custodial risk, your time.

## Command line

```powershell
.\.venv\Scripts\python -m homeminer discover            # read-only report
.\.venv\Scripts\python -m homeminer benchmark cpu --dry-run
.\.venv\Scripts\python -m homeminer benchmark cpu       # asks before starting
.\.venv\Scripts\python -m homeminer market              # fetch timestamped inputs
.\.venv\Scripts\python -m homeminer estimate --electricity 0.0
.\.venv\Scripts\python -m homeminer validate-address    # prompts; nothing stored
.\.venv\Scripts\python -m homeminer install-miner xmrig --approve
.\.venv\Scripts\python -m homeminer emergency-stop
.\.venv\Scripts\python -m homeminer sha256-reference    # educational, not a miner
```

## Where things are kept

| What | Where | In Git? |
|---|---|---|
| Source, docs, scripts, sample config | this folder | yes |
| Your config | `config\homeminer.toml` | no (git-ignored) |
| Approved miner binaries + hash manifest | `tools\` | no |
| Discovery report, benchmark results, run logs, market cache, ledger, UI settings, saved payout address | `%LOCALAPPDATA%\HomeMiner` | no (outside the repo) |

## Project layout

```
src/homeminer/
  discovery.py, compat.py, hardware_db.py   Phase 1: read-only discovery and compatibility
  benchmark/                                Phase 3: runner, telemetry, power labels, isolation lock
  miners/                                   official release definitions, verified install, XMRig commands
  market/                                   NiceHash, Bitcoin network and price fetchers (timestamped)
  profit.py, routes.py, ledger.py           Phases 2 and 4: estimates, route comparison, ledger
  wallet.py, redact.py                      Phase 5: address validation, log redaction
  mining.py, process.py                     gated mining session, process supervision, emergency stop
  ui/                                       Phase 6: local interface
scripts/                                    PowerShell setup, diagnostics, install, start, stop, tests
tests/                                      automated tests with synthetic fixtures
docs/FEASIBILITY.md                         feasibility report with sources
docs/SECURITY.md                            security and troubleshooting guide
```

## Tests

```powershell
.\scripts\Invoke-HomeMinerTests.ps1
```

Tests use synthetic fixtures (clearly labelled, never real market data) and a
fake miner script that prints XMRig-style output without hashing. They never
download or run real mining software. The benchmark-process tests use a POSIX
test double and are skipped on Windows.

## Using this folder as its own repository

HomeMiner lives in a self-contained folder. To give it its own history:

```powershell
git subtree split --prefix=homeminer -b homeminer-only
```

## Licence

MIT. XMRig and xmrig-cuda are separate projects under their own licences
(GPL-3.0); HomeMiner downloads their official releases only after your approval
and does not redistribute them.
