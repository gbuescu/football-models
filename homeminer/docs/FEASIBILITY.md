# HomeMiner feasibility report

Prepared 2026-10-25, before any software was installed and before any benchmark
was run. Nothing in this report was measured on your PC. Hardware facts come
from your stated configuration and manufacturer specifications; HomeMiner's
discovery module (`homeminer discover`) re-checks them on the machine itself.

Each source is tagged with how far it can be trusted:

| Tag | Meaning |
|---|---|
| **primary** | Read directly from the official source (release page, source code, vendor notice) |
| **secondary** | An official page quoted through a search result; the page itself could not be opened from the build environment |
| **anecdotal** | User reports, forum posts, third-party sites |
| **estimate** | Arithmetic or an extrapolation, with the inputs shown |

## Summary

1. **The CPU is the only device with a credible route to BTC.** The i5-14400F
   can run RandomX (the Monero proof-of-work) with XMRig, and RandomX hash power
   can be sold for BTC on a hash-power marketplace such as NiceHash. Whether
   NiceHash still buys RandomX is **not confirmed**: NiceHash Miner stopped
   bundling XMRig after version 3.1.0.9, and NiceHash's public API (which says
   which algorithms are listed) could not be reached from the build environment.
   HomeMiner checks the list on your PC before it shows any estimate.
2. **The GTX 970 is close to end of life for mining.** NiceHash QuickMiner and
   Excavator list the GTX 1000 series as their oldest supported GeForce cards.
   Its 4 GB (3.5 GB fast + 0.5 GB slow) rules out every DAG-based algorithm that
   still pays (KawPow's DAG is about 5.9 GB). NVIDIA's R580 driver branch is the
   last for Maxwell. The one benchmark that can run offline and be checked is
   RandomX on the GPU through XMRig's CUDA plugin. Expect it to be slower than
   the CPU.
3. **Mining Bitcoin directly (SHA-256) is not viable on this hardware.** Even if
   the GPU and CPU together managed 5 GH/s (a deliberately generous upper
   bound), the expected return is about **0.22 satoshi a day, or roughly 82
   satoshis a year**. Solo mining at that rate would take about **3.8 million
   years** on average to find a block. This route is in HomeMiner for education
   only.
4. **Free electricity does not make this profitable. It removes one cost.**
   Hardware wear, heat, noise and lost alternatives remain (section 6). Selling
   hash power also means trusting a custodial marketplace with the balance
   until you withdraw it.
5. **Receiving BTC is not the same as mining Bitcoin.** On NiceHash you sell
   hash power on some other algorithm; the buyer chooses which coin to mine and
   NiceHash pays you in BTC. Your PC never mines Bitcoin in Route A.

**Recommended next step:** run discovery on the PC, then approve installing
XMRig 6.26.0 (official release, checked against its SHA256SUMS) so the CPU
benchmark can run offline. No wallet address or account is needed until you
decide to sell hash power.

## 1. Hardware: stated versus verified

| Component | Stated | Specification facts used by HomeMiner | Verified on PC? |
|---|---|---|---|
| CPU | Intel Core i5-14400F | 6 P-cores + 4 E-cores = 10 cores / 16 threads, 20 MB L3, 65 W base power, 148 W maximum turbo power, no integrated graphics (Intel specification) | Not yet: `homeminer discover` |
| GPU | GTX 970, 4 GB | Maxwell GM204, compute capability 5.2, 145 W board power, 4 GB as 3.5 GB + 0.5 GB slower segment (NVIDIA specification) | Not yet |
| Motherboard | MSI PRO B760M-P | B760 chipset: no CPU core-ratio overclocking (HomeMiner never overclocks anyway). DDR4 and DDR5 versions exist; discovery reports which memory is fitted | Not yet |
| RAM | Not stated | RandomX "fast mode" needs about 2.3 GB free (2,080 MiB dataset + 2 MiB per thread) | Not yet |
| OS | Windows 11 Pro | Windows reports builds 22000+ as Windows 11 even where the registry still says "Windows 10" | Not yet |
| Electricity | €0.00/kWh | User-supplied assumption; editable in the UI | n/a |

## 2. Driver, software and platform constraints

| Constraint | Effect on this PC | Source |
|---|---|---|
| NVIDIA's R580 driver branch is the last to support Maxwell, Pascal and Volta | GTX 970 stays on R580. New features and CUDA releases will not reach it. NVIDIA's notice was published for Unix; Windows is expected to follow but this was not confirmed. Third-party packagers expect R580 to reach end of life around June 2028 | primary (Phoronix, quoting NVIDIA); secondary (negativo17) |
| CUDA 13 toolkit no longer targets Maxwell | GPU miners built only with CUDA 13 will not run. Use CUDA 12.x or older builds | primary (NVIDIA deprecation, as above) |
| xmrig-cuda 6.22.1 (2025-06-04) ships CUDA 10.2, 11.8 and 12.9 Windows builds | The build files compile `sm_50` for every CUDA version; Maxwell code built for 5.0 runs on 5.2, so the GTX 970 should be supported. Not tested on a GTX 970 | primary (release page, `cmake/CUDA.cmake`) |
| Windows Defender flags miners as potentially unwanted | Expect XMRig to be quarantined. HomeMiner never disables Defender and never adds exclusions. You decide; see the security guide | primary (NiceHash Miner release notes recommend an exclusion; HomeMiner does not) |
| Huge pages need the "Lock pages in memory" right | Usually a noticeable RandomX speed-up (not measured here; the benchmark reports which mode ran). XMRig can grant the right itself when run as Administrator. HomeMiner refuses to launch miners elevated, so the right is used only if you have already granted it | anecdotal (XMRig documentation, not re-checked) |
| XMRig's MSR mod writes CPU model-specific registers through a kernel driver | Disabled (`--randomx-wrmsr=-1`). It needs Administrator rights and changes CPU behaviour, which is outside "no hardware modification" | primary (XMRig options) |
| Intel 13th/14th Gen "Vmin shift" instability | Intel says elevated voltage and temperature over long periods caused it, and recommends the latest BIOS (microcode 0x12F) with Intel Default Settings. Sustained mining is exactly that kind of load. Discovery reports your microcode revision; check MSI's BIOS notes for your board before long runs. HomeMiner does not change BIOS, voltage or power limits | secondary (Intel community notice via elevenforum, HotHardware) |
| Many boards ship with CPU power limits above Intel's 65 W base power | The CPU power estimate is a model, not a measurement. A wall-plug meter gives a real figure; the UI accepts it | anecdotal (check your BIOS power-limit setting) |

## 3. Algorithm compatibility

| Algorithm | Device | Memory | Status on this PC | Miner | Benchmark | Source |
|---|---|---|---|---|---|---|
| RandomX `rx/0` (Monero, current) | CPU | 2,080 MiB dataset + 2 MiB per thread; best with 2 MB L3 per thread (20 MB L3 → about 10 threads) | **Compatible** | XMRig 6.26.0 (2026-03-28) | Offline (`--bench`), with a reference checksum | primary (XMRig source) |
| RandomX v2 `rx/2` | CPU | As above | Supported by XMRig 6.26.0. Monero's switch is still on a test network (stressnet fork 2026-10-05) with no mainnet date | XMRig 6.26.0 | Offline | primary (XMRig source); secondary (cryptoticker) |
| RandomX `rx/0` | GPU | Dataset (2,080 MiB) fits inside the 3.5 GB fast segment | **Likely compatible, not tested** | XMRig 6.26.0 + xmrig-cuda 6.22.1 (CUDA 12.9 build) | Offline | primary (release and build files) |
| KawPow (Ravencoin) | GPU | DAG about 5.9 GB on 2026-10-20 | **Incompatible** | — | — | anecdotal (Kryptex DAG calculator) |
| Etchash (Ethereum Classic) | GPU | DAG above 4 GB by 2026 (about 4.5 GB from the ECIP-1099 epoch schedule) | **Incompatible** | — | — | estimate |
| Autolykos2 (Ergo) | GPU | Grows about 5% every 51,200 blocks | Treated as incompatible; not verified | — | — | secondary (Ergo blog) |
| Equihash 144,5 (ZHash), BeamHash III | GPU | Low; believed to fit 4 GB, not verified | **Unverified** | lolMiner (Maxwell CUDA support added in 1.25; current 1.98a does not say) | Pool-connected only: needs approval and a payout account | primary (lolMiner release notes) |
| Ethash on Ethereum mainnet | — | — | **Never.** Ethereum is proof of stake and cannot be mined. HomeMiner rejects it | — | — | — |
| SHA-256d (Bitcoin) | CPU/GPU | Trivial | Runs, but worthless (section 5). No maintained official GPU SHA-256 miner exists for this card | — | Educational reference only | estimate |

## 4. Route A: NiceHash (BTC-denominated hash-power marketplace)

**How it works.** You sell hash power on one algorithm (RandomX for the CPU).
Buyers point it at whatever coin they want. NiceHash credits your NiceHash
wallet in BTC, and you withdraw to your own BTC address. You are paid in
Bitcoin; you are not mining Bitcoin.

| Item | Finding | Source |
|---|---|---|
| Official Windows software | NiceHash Miner 3.1.1.6 (2025-10-08). Pre-installed plugins: Excavator, GMiner. Optional: NanoMiner, lolMiner. XMRig was last bundled in 3.1.0.9 (2023-08-23) | primary (GitHub releases) |
| NiceHash QuickMiner | Excavator-based. Lists GTX 1000 / RTX 2000 / RTX 3000 (4 GB minimum), CPUs with AVX2 via optional XMRig, Windows 10 | primary (GitHub README) |
| GTX 970 support | Not supported by Excavator or QuickMiner (Pascal is the oldest generation listed). NiceHash Miner sets a 4 GB minimum, and GMiner/lolMiner support for Maxwell is not confirmed for current releases | primary / unverified |
| CPU support | No current NiceHash app bundles XMRig. Users report running XMRig on its own against NiceHash's stratum in NiceHash mode | anecdotal (r/NiceHash, April 2025) |
| Is RandomX still listed? | **Unknown.** The NiceHash public API was blocked from the build environment. HomeMiner reads `GET https://api2.nicehash.com/main/api/v2/public/simplemultialgo/info` on your PC and shows "route unavailable" if `RANDOMXMONERO` is missing | — |
| Mining fee | 2% "service charge" on mining payouts to the NiceHash wallet | secondary (NiceHash "When and how do you get paid?") |
| Payout to NiceHash wallet | Minimum 0.00001 BTC, every 4 hours (or daily) | secondary (same page) |
| Withdrawal to your wallet | On-chain blockchain fee 0.0001 BTC; minimum 0.0002 BTC. Lightning withdrawals are also offered | secondary (NiceHash "FAQ for 60 Day Rule") |
| 60-day rule | NiceHash AG is regulated in Switzerland and will not hold funds for more than 60 days. Funds older than 50 days should be withdrawn, and older funds are moved with a fee | secondary / anecdotal |
| Account requirements | A NiceHash account is required to hold and withdraw a balance. Users report identity (KYC) checks before withdrawals. Whether you can still mine straight to an external address without an account was **not confirmed** | anecdotal |
| Address format | A standard BTC address for withdrawals. Whether NiceHash accepts Taproot (`bc1p…`) was not confirmed; a `bc1q…` (native SegWit) address is the safe choice | unverified |
| Counterparty risk | Your balance is custodial until you withdraw it. NiceHash was hacked in 2017; users mining to external wallets lost only small unpaid balances | secondary (news reports) |

HomeMiner treats every NiceHash fee as an editable assumption, records where it
came from and when it was checked, and marks estimates "fees unverified" until
you confirm them on nicehash.com.

**Not evaluated in depth.** Auto-exchange pools that pay in BTC exist. Each
converts coins for you and holds a custodial balance, so the same counterparty
caveats apply; HomeMiner does not endorse any of them. Mining Monero at a pool
and converting it yourself would mean exchange trades, which HomeMiner will
never make for you.

## 5. Route B: direct Bitcoin (SHA-256) mining — educational

Inputs: difficulty **140.58 T** (adjustment at block 971,712, reported by
Bitcoin.com News and PrimeXBT in October 2026, secondary), block subsidy
**3.125 BTC** (since the April 2024 halving), transaction fees ignored. HomeMiner
fetches live difficulty when it runs.

Expected BTC per day = hashrate × 86,400 × reward ÷ (difficulty × 2³²).

| Quantity | Value |
|---|---|
| Implied network hash rate | about 1,006 EH/s (10²¹ H/s) |
| Network "hashprice" | 4.47 × 10⁻⁷ BTC per TH/s per day (about 44.7 sat) |
| Your PC at a generous **5 GH/s** upper bound (assumption, not a measurement) | 2.2 × 10⁻⁹ BTC/day ≈ **0.22 sat/day ≈ 82 sat/year** |
| Average time to find a block solo at 5 GH/s | about **3.8 million years** |
| Chance of finding a block within a year | about 2.6 × 10⁻⁷ (1 in 3.8 million) |
| Time to accumulate even a 1,000-satoshi pool payout | about 12 years |

**Why this hardware cannot compete.** Bitcoin's difficulty is set by
purpose-built SHA-256 ASICs. Current single units are rated in the hundreds of
TH/s, tens of thousands of times the upper bound above. At that bound the GTX 970
would use about 29,000 J per TH; ASICs are in the tens of joules per TH. A CPU
is slower still. These figures show scale only. This report does not recommend
buying mining hardware, and nothing here suggests meaningful BTC earnings are
likely.

## 6. Economics at €0.00/kWh

No earnings figure appears in this report because no hash rate has been
measured. HomeMiner computes estimates only from **measured** benchmark hash
rates multiplied by **timestamped** NiceHash prices. If either is missing,
stale, or fails its sanity check, it says the estimate is unreliable instead of
showing a number.

Costs that remain when electricity is free:

- **Hardware wear.** Fans and pumps run continuously, thermal paste and pads
  dry out faster, and a 2014-era GPU's capacitors age.
- **Heat and noise.** Up to about 210 W of extra heat in the room (65 W CPU base
  power + 145 W GPU board power). In summer that may mean cooling costs.
- **Opportunity cost.** If the electricity comes from solar panels with an
  export tariff, every kWh used for mining is a kWh not sold. The real price is
  the feed-in tariff, not zero.
- **Someone else's meter.** If the €0.00 figure reflects electricity that a
  landlord, employer or shared building pays for, check that you are allowed to
  use it this way.
- **Tax.** Mining income may be taxable where you live. HomeMiner does not give
  tax advice.
- **Custodial and price risk.** BTC held on a marketplace, or later sold, can
  fall in value.

## 7. Plan and approval gates

| Step | What happens | Needs your approval? |
|---|---|---|
| 1 | `homeminer discover` or `scripts\Get-HomeMinerDiagnostics.ps1`: read-only report | No (read-only) |
| 2 | Install XMRig 6.26.0 from the official GitHub release, verified against `SHA256SUMS` (and its GPG signature if Gpg4win is present) | **Yes** |
| 3 | CPU RandomX benchmark, offline, conservative thread limit | Yes (you start it) |
| 4 | Optional: install xmrig-cuda 6.22.1 and run the GPU RandomX benchmark | **Yes** |
| 5 | Fetch NiceHash prices and BTC difficulty, then review the estimate | No (read-only network requests, no identifiers sent) |
| 6 | Start real mining. Needs a NiceHash account and its mining address | **Yes**, every session |

## Sources

Accessed 2026-10-25.

- XMRig releases and v6.26.0 notes: <https://github.com/xmrig/xmrig/releases> (primary)
- XMRig source: `src/base/crypto/Algorithm.cpp`, `src/base/net/stratum/benchmark/BenchClient.cpp`, `src/backend/common/benchmark/BenchState_test.h` at <https://github.com/xmrig/xmrig> (primary)
- xmrig-cuda releases and `cmake/CUDA.cmake`: <https://github.com/xmrig/xmrig-cuda> (primary)
- NiceHash Miner releases: <https://github.com/nicehash/NiceHashMiner/releases> (primary)
- NiceHash QuickMiner README: <https://github.com/nicehash/NiceHashQuickMiner> (primary)
- NiceHash, "When and how do you get paid?": <https://www.nicehash.com/support/mining-help/earnings-and-payments/when-and-how-do-you-get-paid> (secondary)
- NiceHash, "FAQ for 60 Day Rule": <https://www.nicehash.com/support/general-help/60-day-rule/faq-60-day-rule> (secondary)
- NiceHash, Software Developers (API): <https://www.nicehash.com/software-developers> (secondary)
- Phoronix, "NVIDIA Confirms 580 Linux Driver Is The Last For Maxwell / Pascal / Volta": <https://www.phoronix.com/news/NVIDIA-580-Linux-Driver-Last-HW> (primary quote of NVIDIA)
- negativo17, NVIDIA driver 580 LTS repository: <https://negativo17.org/nvidia-driver-580-lts-repository/> (secondary)
- lolMiner 1.25 release notes: <https://github.com/Lolliedieb/lolMiner-releases/releases/tag/1.25>; current releases: <https://github.com/Lolliedieb/lolMiner-releases/releases> (primary)
- Kryptex RVN DAG size calculator: <https://pool.kryptex.com/en/rvn/dag-size-calculator> (anecdotal)
- Bitcoin.com News, difficulty 140.58 T: <https://news.bitcoin.com/bitcoin-mining-difficulty-jumps-5-92-third-largest-hike-2026/> (secondary)
- Intel 0x12F microcode notice: <https://www.elevenforum.com/t/intel-core-13th-and-14th-gen-vmin-shift-instabilty-update-new-microcode-update-0x12f.35919/>; <https://hothardware.com/news/intel-new-firmware-fix-vmin-shift> (secondary)
- Monero FCMP++ stressnet: <https://cryptoticker.io/en/monero-fcmp-stressnet-fork-october-2026/> (secondary)
- r/NiceHash thread on XMRig removal (April 2025): <https://red.127.pp.ua/r/NiceHash/comments/1ka613q/cpu_mining> (anecdotal)
- Ergo, "Ergo and the Autolykos Consensus Mechanism": <https://ergoplatform.org/en/blog/2019_12_22_mining_for_fun> (secondary)
