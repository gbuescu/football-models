# Security and troubleshooting guide

## Security model

HomeMiner runs third-party mining software on a PC you also use for everything
else. These are the risks it guards against, and how.

| Risk | What HomeMiner does |
|---|---|
| A tampered or fake miner download | Downloads only from the official GitHub release over HTTPS. Refuses the archive unless its SHA-256 matches the release's `SHA256SUMS`. Checks the GPG signature when gpg is installed and the key is in your keyring. Writes a manifest of every file's hash and re-checks it before each launch; any change blocks the launch. Files it did not install are never run. |
| A miner gaining Administrator rights | Refuses to launch miners from an elevated session, so XMRig cannot change security policy (huge-pages privilege) or load its MSR driver. MSR writes are disabled on the command line as well. |
| Mining that keeps running | No services, scheduled tasks, Run keys or startup entries. On Windows, miners run inside a kill-on-close Job Object, so they end when HomeMiner ends. Emergency stop kills tracked miners and anything running from `tools\`. |
| Overheating and instability | Conservative defaults: 50% of threads, idle priority, temperature limits (GPU 80 °C, CPU 90 °C when readable), a 45-minute limit per benchmark. No overclocking, voltage or power-limit changes. A benchmark whose hash sum does not match XMRig's reference is marked unusable. |
| Wallet theft | Never asks for seed phrases, private keys, extended private keys or exchange passwords, and refuses to accept them (it recognises the formats). Never initiates withdrawals, trades or transactions. |
| Leaking personal details | Addresses are kept only in memory, or in `%LOCALAPPDATA%\HomeMiner\payout.json` if you tick "remember". Logs, events and reports pass through a redaction filter (BTC addresses, your Windows user name, computer name). Discovery never collects serial numbers, UUIDs or MAC addresses. Configuration files that contain an address or secret are refused. Market requests carry no identifiers. |
| Other web pages controlling HomeMiner | The interface listens on 127.0.0.1 only. Every API call needs a random per-launch token, delivered in the URL fragment and sent in a custom header. Host and Origin headers are checked (blocks DNS rebinding). Cross-origin preflights are always refused. A strict Content Security Policy blocks inline scripts. |

### Your BTC address

- For NiceHash you mine to your **NiceHash mining address**, shown in your
  NiceHash wallet. It belongs to your NiceHash account; it is not your own
  wallet. Withdrawals to your own wallet are set up on nicehash.com.
- HomeMiner checks format, checksum (Base58Check, bech32, bech32m) and network
  (mainnet). It rejects testnet addresses, Lightning invoices, private keys and
  seed phrases. A valid format does not prove an address is yours: confirm it in
  your wallet app.
- A native SegWit address (`bc1q…`) is the safe choice for withdrawals. Whether
  NiceHash sends to Taproot (`bc1p…`) was not confirmed.
- Never paste a seed phrase anywhere. No legitimate mining service needs it.
- Be wary of anyone asking for a fee to "unlock" or "reconnect" a payout. That
  is a common scam.

### Windows Defender

Windows Defender usually flags mining software as a potentially unwanted
application, and may quarantine XMRig right after installation. HomeMiner will
not disable Defender and will not add exclusions. Your options, in order of
caution:

1. **Accept the quarantine** and stop here. You lose the benchmark, nothing else.
2. **Allow the specific detection** in Windows Security → Virus & threat
   protection → Protection history, after checking that HomeMiner's verification
   passed (`homeminer discover` shows "SHA256SUMS match").
3. **Exclude only HomeMiner's `tools` folder**, yourself, in Windows Security.
   This widens what Defender ignores, so only do it if you accept that. Remove
   the exclusion when you are done.

Never disable real-time protection to mine.

### Huge pages (optional)

RandomX is faster with huge pages, which need the "Lock pages in memory" right.
HomeMiner never grants it and never runs XMRig elevated (XMRig can grant it to
itself when elevated). If you want it:

1. Run `secpol.msc` (Windows Pro includes it).
2. Local Policies → User Rights Assignment → **Lock pages in memory**.
3. Add your own user account, then sign out and back in.

This lets programs you run keep memory locked in RAM. Undo it the same way.
Discovery reports whether the right is granted, and each benchmark records
whether huge pages were used.

### CPU temperature and power sensors (optional)

Windows has no built-in CPU temperature or package-power reading. If
LibreHardwareMonitor is running and publishing its WMI sensors, HomeMiner reads
them. LibreHardwareMonitor loads a kernel driver to read sensors; install it only
if you accept that. Without it, CPU temperature shows as unavailable and CPU
power is estimated and labelled "estimated". A plug-in wall meter is the most
trustworthy power measurement: enter its reading (extra watts while the
benchmark runs) in the interface.

### Intel 13th/14th Gen stability

Intel attributes "Vmin shift" instability in 13th/14th Gen desktop CPUs to
elevated voltage and temperature over long periods. Intel recommends the latest
BIOS (microcode 0x12F) with Intel Default Settings. Before long runs, check MSI's
support page for your board's latest BIOS and its release notes. HomeMiner reports
the loaded microcode revision but never changes BIOS settings. Updating the BIOS
is your decision.

## Troubleshooting

| Symptom | Likely cause and fix |
|---|---|
| Scripts will not run ("running scripts is disabled") | Run `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass` in that window only. |
| "Python 3.11 or newer was not found" | Install Python from python.org (tick "Add to PATH") or `winget install --id Python.Python.3.12 -e`. The Microsoft Store alias is not a real install. |
| Benchmark blocked: "Not installed… needs your approval" | Run `.\scripts\Install-Miner.ps1 -Miner xmrig`. |
| Blocked: "No HomeMiner manifest" or "has changed since installation" | Defender may have removed or altered a file, or the files were copied in by hand. Reinstall with `Install-Miner.ps1`. |
| Install refused: "SHA-256 mismatch" | The download does not match the official checksum. Do not use it. Try again later; if it persists, check the release page by hand. |
| "GPG signature not checked" | gpg is missing or the publisher's key is not in your keyring. SHA256SUMS still matched. To add the check, install Gpg4win, import XMRig's key from its website and GitHub profile, compare fingerprints, and reinstall. |
| Blocked: "running as Administrator" | Close the window and start HomeMiner from a normal PowerShell window. |
| Blocked: "Another benchmark or mining session is running" | Wait for it, or press Stop. CPU and GPU are measured separately on purpose. |
| GPU: "could not load the CUDA plugin" | Use the xmrig-cuda build that matches your driver: discovery's GPU entry names it (CUDA 12.9 needs a driver that reports CUDA 12.9 or newer). Change `[miners.xmrig_cuda] asset` and reinstall. Install xmrig-cuda into its own folder; HomeMiner passes its path to XMRig. |
| GPU: "ran out of memory" | The GTX 970 has 3.5 GB of fast memory and Windows uses some for the desktop. Close GPU-heavy apps (browsers with hardware acceleration, games) and retry. |
| "Hash sum did NOT match" | The hardware computed wrong results: an unstable overclock or memory profile, overheating, or the Intel instability above. Load BIOS defaults, check temperatures, and do not mine until it matches. |
| "Earnings cannot be estimated reliably" | Read the reasons listed. Usually: no benchmark yet, market data not refreshed, or NiceHash not listing RandomX. |
| "NiceHash API unreachable" | A firewall, proxy or outage. HomeMiner then shows no NiceHash estimate rather than guessing. |
| "NiceHash's SHA256 price is … implausible" | NiceHash changed its API units or returned bad data. Estimates stay withheld; report it or check NiceHash's API documentation. |
| CPU temperature shows "needs LibreHardwareMonitor" | Expected on Windows; see above. |
| The interface says "No session token" | Open the full address printed in the HomeMiner window; it ends in `#t=…`. |
| Emergency stop in the browser did nothing | Run `.\scripts\Stop-HomeMiner.ps1`, close the HomeMiner window, or end `xmrig.exe` in Task Manager. |
| A miner is still running after closing HomeMiner | Should not happen on Windows (kill-on-close job). Run `.\scripts\Stop-HomeMiner.ps1` and report it. |
