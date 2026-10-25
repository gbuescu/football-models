"use strict";

// The session token arrives in the URL fragment, which browsers never send to servers.
const TOKEN = (() => {
  const m = location.hash.match(/t=([A-Za-z0-9_-]+)/);
  if (m) {
    try { sessionStorage.setItem("homeminer-token", m[1]); } catch (e) { /* storage may be blocked */ }
    history.replaceState(null, "", location.pathname);
    return m[1];
  }
  try { return sessionStorage.getItem("homeminer-token") || ""; } catch (e) { return ""; }
})();

let state = null;

// ---------------------------------------------------------------- helpers

function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? "" : String(v));
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

function fill(id, ...children) {
  const el = document.getElementById(id);
  el.replaceChildren(...children.flat().filter((c) => c !== null && c !== undefined && c !== false));
}

const dash = "—";
function num(x, digits = 1) { return x === null || x === undefined ? dash : Number(x).toLocaleString(undefined, { maximumFractionDigits: digits, minimumFractionDigits: 0 }); }
function hs(x) {
  if (x === null || x === undefined) return dash;
  const units = [["GH/s", 1e9], ["MH/s", 1e6], ["kH/s", 1e3]];
  for (const [u, f] of units) if (x >= f) return `${num(x / f, 2)} ${u}`;
  return `${num(x, 1)} H/s`;
}
function btc(x) {
  if (x === null || x === undefined) return dash;
  const sats = x * 1e8;
  if (Math.abs(x) < 0.0001) return `${sats.toLocaleString(undefined, { maximumSignificantDigits: 4 })} sat (${x.toExponential(3)} BTC)`;
  return `${x.toFixed(8)} BTC`;
}
function eur(x) {
  if (x === null || x === undefined) return dash;
  const v = Number(x) === 0 ? 0 : Number(x); // no "€-0.0000"
  if (v !== 0 && Math.abs(v) < 0.01) return `€${v.toPrecision(3)}`;
  return `€${v.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 4 })}`;
}
function days(x) {
  if (x === null || x === undefined) return "never at this rate";
  if (x < 1) return `${num(x * 24, 1)} hours`;
  if (x > 3650) return `${num(x / 365.25, 0)} years`;
  return `${num(x, 1)} days`;
}
function ago(iso) {
  if (!iso) return dash;
  const mins = (Date.now() - Date.parse(iso)) / 60000;
  if (mins < 1) return "just now";
  if (mins < 120) return `${Math.round(mins)} min ago`;
  return `${Math.round(mins / 60)} h ago`;
}
function chip(text, kind) { return h("span", { class: `pill ${kind || ""}` }, text); }
function kv(rows) {
  return h("table", { class: "kv" }, h("tbody", {}, rows.filter(Boolean).map(([k, v, src]) =>
    h("tr", {}, h("td", {}, k), h("td", {}, v, src ? h("div", { class: "src" }, src) : null)))));
}
function issues(list) {
  if (!list || !list.length) return null;
  return h("ul", { class: "issues" }, list.map((i) => h("li", { class: i.severity || i.level || "info" }, i.message || i)));
}

async function api(path, body) {
  const opts = { method: body === undefined ? "GET" : "POST", headers: { "X-HomeMiner-Token": TOKEN } };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(path, opts);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || res.statusText);
  return data;
}

async function act(path, body, okMessage) {
  try {
    const out = await api(path, body || {});
    if (out.ok === false) alert(out.error || "Not done.");
    else if (okMessage) flash(okMessage);
    await refresh();
    return out;
  } catch (e) {
    alert(e.message);
    return null;
  }
}

function flash(message) {
  const banner = document.getElementById("banner");
  banner.textContent = message;
  banner.className = "banner";
}

// ---------------------------------------------------------------- renderers

function renderStatus(s) {
  const el = document.getElementById("status");
  if (s.mining.running) { el.textContent = s.mining.paused ? "Mining paused" : "MINING"; el.className = "pill bad"; }
  else if (s.job && s.job.running) { el.textContent = `Benchmarking ${s.job.device.toUpperCase()}`; el.className = "pill warn"; }
  else { el.textContent = "Idle"; el.className = "pill ok"; }
}

function renderHardware(s) {
  const hw = s.hardware;
  if (!hw.discovered) {
    fill("hw-body", h("p", { class: "muted" }, "No discovery report yet. Discovery only reads information; it changes nothing."));
    return;
  }
  const lim = hw.limitations.filter((l) => l.severity !== "info");
  fill("hw-body",
    kv([
      ["OS", `${hw.os || dash} (build ${hw.os_build || dash})`],
      ["CPU", hw.cpu, `${hw.cores ?? dash} cores / ${hw.threads ?? dash} threads, L3 ${hw.l3_mb ?? dash} MB, microcode ${hw.microcode || dash}`],
      ["RAM", `${num(hw.ram_total_gb)} GB total, ${num(hw.ram_available_gb)} GB available`],
      ["GPU", hw.gpu || "none detected", hw.gpu ? `${num(hw.vram_mb, 0)} MB VRAM, compute ${hw.compute_capability || dash}, driver ${hw.gpu_driver || dash}, CUDA ${hw.cuda || dash}` : null],
      ["Report", ago(hw.generated_at)],
    ]),
    h("h3", {}, "Limitations"),
    issues(lim.length ? lim : [{ severity: "info", message: "No blocking limitations." }]),
    h("details", {}, h("summary", {}, `All notes (${hw.limitations.length})`), issues(hw.limitations)));
}

function compatFor(s, device) {
  return (s.hardware.compatibility || []).filter((c) => c.device === device);
}

function renderBench(s, device) {
  const r = s.benchmarks[device];
  const dry = s.dry_runs[device];
  const compat = compatFor(s, device);
  const rows = [];
  const main = compat.find((c) => c.key === `${device}-rx0`);
  rows.push(["Algorithm / miner", main ? `${main.title}: ${main.miner}` : "RandomX (rx/0): XMRig", main ? `Compatibility: ${main.status}` : "Run discovery to check compatibility"]);
  if (r) {
    rows.push(["Status", r.status, r.result_kind === "pool_verified" ? "Pool-verified figure you entered" : "Local benchmark (offline, no pool)"]);
    rows.push(["Hash rate", hs(r.hashrate_hs), r.verification === "match" ? "XMRig reference check: match" :
      r.verification === "mismatch" ? "XMRig reference check: MISMATCH — do not mine" : `Reference check: ${r.verification}`]);
    rows.push(["Duration", r.duration_s ? `${num(r.duration_s)} s (XMRig: ${num(r.miner_seconds, 3)} s)` : dash, r.finished_at ? ago(r.finished_at) : null]);
    const t = r.telemetry || {};
    rows.push(device === "cpu"
      ? ["Peak temp / utilisation", `${t.cpu_temp_peak ?? dash} °C / ${t.cpu_util_peak ?? dash}% (avg ${t.cpu_util_avg ?? dash}%)`, `Temperature: ${(t.sources || {}).cpu_temp || dash}`]
      : ["Peak temp / utilisation", `${t.gpu_temp_peak ?? dash} °C / ${t.gpu_util_peak ?? dash}%`, `Memory peak ${t.gpu_mem_peak_mb ?? dash} MB; source ${(t.sources || {}).gpu || dash}`]);
    const p = r.power || {};
    rows.push(["Power (extra)", p.watts !== undefined && p.watts !== null ? `${num(p.watts)} W — ${p.kind}` : dash, p.method]);
    rows.push(["Memory requirement", r.memory_requirement || dash]);
    if (r.abort_reason) rows.push(["Stopped because", r.abort_reason]);
  } else {
    rows.push(["Status", "No benchmark yet"]);
  }
  const parts = [kv(rows)];
  if (r && (r.errors || []).length) parts.push(h("h3", {}, "Errors"), issues(r.errors.map((m) => ({ severity: "blocker", message: m }))));
  if (r && (r.notes || []).length) parts.push(issues(r.notes.map((m) => ({ severity: "warning", message: m }))));
  if (dry) {
    parts.push(h("details", { open: dry.errors.length ? true : null },
      h("summary", {}, `Last dry run: ${dry.errors.length ? "blocked" : "ready"}`),
      issues(dry.errors.map((m) => ({ severity: "blocker", message: m }))),
      issues((dry.notes || []).filter((n) => !n.startsWith("Dry run")).map((m) => ({ severity: "warning", message: m }))),
      dry.command && dry.command.length ? h("p", { class: "mono" }, dry.command.join(" ")) : null));
  }
  if (device === "gpu") {
    const others = compat.filter((c) => c.key !== "gpu-rx0");
    if (others.length) {
      parts.push(h("details", {}, h("summary", {}, "Other GPU algorithms"),
        h("table", { class: "data" }, h("thead", {}, h("tr", {}, h("th", {}, "Algorithm"), h("th", {}, "Status"), h("th", {}, "Why"))),
          h("tbody", {}, others.map((c) => h("tr", {}, h("td", {}, c.title), h("td", {}, c.status), h("td", {}, c.summary)))))));
    }
  }
  fill(`${device}-body`, parts);
}

function spark(values, max) {
  const v = values.filter((x) => x !== null && x !== undefined);
  if (v.length < 2) return null;
  const top = max || Math.max(...v, 1);
  const pts = v.map((x, i) => `${(i / (v.length - 1)) * 100},${34 - (x / top) * 32}`).join(" ");
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("class", "spark");
  svg.setAttribute("viewBox", "0 0 100 34");
  svg.setAttribute("preserveAspectRatio", "none");
  const line = document.createElementNS("http://www.w3.org/2000/svg", "polyline");
  line.setAttribute("points", pts);
  svg.append(line);
  return svg;
}

function renderMonitor(s) {
  const hist = s.telemetry || [];
  const last = hist[hist.length - 1] || {};
  const series = (k) => hist.map((x) => x[k]);
  const tile = (label, val, unit, key, max, src) => h("div", { class: "tile" },
    h("div", { class: "label" }, label),
    h("div", { class: "value" }, val === null || val === undefined ? dash : `${num(val)}${unit}`),
    key ? spark(series(key), max) : null,
    src ? h("span", { class: "src" }, src) : null);
  const api = s.mining.api || {};
  fill("mon-body",
    tile("CPU utilisation", last.cpu_util_pct, "%", "cpu_util_pct", 100, "psutil"),
    tile("CPU temperature", last.cpu_temp_c, " °C", "cpu_temp_c", 100, last.cpu_temp_c === null || last.cpu_temp_c === undefined ? "needs LibreHardwareMonitor" : "LibreHardwareMonitor"),
    tile("GPU temperature", last.gpu_temp_c, " °C", "gpu_temp_c", 100, "nvidia-smi"),
    tile("GPU utilisation", last.gpu_util_pct, "%", "gpu_util_pct", 100, "nvidia-smi"),
    tile("GPU board power", last.gpu_power_w, " W", "gpu_power_w", null, last.gpu_power_w === null || last.gpu_power_w === undefined ? "not reported by card" : "nvidia-smi sensor"),
    h("div", { class: "tile" }, h("div", { class: "label" }, "Mining hash rate (10 s)"),
      h("div", { class: "value" }, s.mining.running ? hs(api.hashrate_10s) : dash),
      spark(series("hashrate"), null),
      h("span", { class: "src" }, s.mining.running ? `XMRig API; shares ${api.shares_good ?? 0}/${api.shares_total ?? 0}` : "not mining")));
  document.getElementById("mon-src").textContent = `Sampled every 2 s. Benchmarks stop automatically above the configured temperature limits.${s.kill_on_close ? " Miners are tied to HomeMiner's process and end when it closes." : ""}`;
}

const SETTINGS = [
  ["economics.electricity_eur_per_kwh", "Electricity (€/kWh)", "0.0001"],
  ["economics.wear_cost_eur_per_day", "Hardware wear allowance (€/day)", "0.01"],
  ["economics.withdrawal_fee_target_pct", "Economic withdrawal: fee at most (%)", "0.1"],
  ["power.wall_meter_cpu_watts", "Wall meter, CPU benchmark (extra W, 0 = none)", "1"],
  ["power.wall_meter_gpu_watts", "Wall meter, GPU benchmark (extra W, 0 = none)", "1"],
  ["fees.nicehash.mining_fee_pct", "NiceHash mining fee (%)", "0.1"],
  ["fees.nicehash.payout_min_btc", "NiceHash minimum payout (BTC)", "0.00001"],
  ["fees.nicehash.withdrawal_fee_btc_onchain", "NiceHash on-chain withdrawal fee (BTC)", "0.00001"],
  ["fees.nicehash.withdrawal_min_btc_onchain", "NiceHash minimum withdrawal (BTC)", "0.00001"],
];

function renderSettings(s) {
  const form = document.getElementById("settings-form");
  if (!form.dataset.built) {
    for (const [key, label, step] of SETTINGS) {
      const input = h("input", { class: "num", type: "number", step, min: "0", id: `set-${key}`, "data-key": key });
      input.addEventListener("change", () => act("/api/settings", { key, value: input.value }, `Saved: ${label}`));
      form.append(h("label", { for: `set-${key}` }, label), input);
    }
    const sel = h("select", { id: "set-verification" }, ["primary", "secondary", "unverified"].map((v) => h("option", { value: v }, v)));
    sel.addEventListener("change", async () => {
      await act("/api/settings", { key: "fees.nicehash.verification", value: sel.value });
      if (sel.value === "primary") await act("/api/settings", { key: "fees.nicehash.verified_on", value: new Date().toISOString().slice(0, 10) });
    });
    form.append(h("label", { for: "set-verification" }, "NiceHash fees checked on nicehash.com?"), sel);
    form.dataset.built = "1";
  }
  for (const [key] of SETTINGS) {
    const input = document.getElementById(`set-${key}`);
    if (document.activeElement !== input) input.value = s.settings[key];
  }
  const sel = document.getElementById("set-verification");
  if (document.activeElement !== sel) sel.value = s.settings["fees.nicehash.verification"];
}

function renderMarket(s) {
  const m = s.market;
  if (!m) {
    fill("market-body", h("p", { class: "muted" }, "No market data yet. Refreshing contacts NiceHash's public API, mempool.space and (as fallbacks) blockchain.info and CoinGecko. Nothing identifying is sent."));
    return;
  }
  const rx = m.quotes.RANDOMXMONERO;
  const sha = m.quotes.SHA256;
  fill("market-body",
    kv([
      ["Fetched", `${ago(m.created_at)}${m.from_cache ? " (cached)" : ""}`, m.created_at],
      ["NiceHash RandomX", rx ? `${rx.paying} BTC per ${rx.display_unit || "unit"}/s per day` : "not listed", rx ? rx.source : "Route A cannot be estimated without it"],
      ["NiceHash SHA256", sha ? `${sha.paying} BTC per ${sha.display_unit || "unit"}/s per day` : "not listed", "Used to cross-check NiceHash's price units"],
      ["Bitcoin difficulty", m.network ? `${(m.network.difficulty / 1e12).toFixed(2)} T at block ${m.network.height ?? dash}` : dash, m.network ? m.network.sources.join(", ") : null],
      ["Block reward", m.network ? `${m.network.subsidy_btc} BTC + ${m.network.avg_fees_btc_per_block ? m.network.avg_fees_btc_per_block.toFixed(4) : "?"} BTC fees` : dash],
      ["BTC price", m.price ? `€${num(m.price.eur, 0)}` : dash, m.price ? `${m.price.source}, ${ago(m.price.fetched_at)}` : null],
    ]),
    issues((m.errors || []).map((e) => ({ severity: "warning", message: e }))));
}

function renderRoutes(s) {
  const r = s.routes;
  const a = r.route_a;
  const b = r.route_b;
  const ub = b.upper_bound;
  fill("routes-body",
    h("p", { class: "verdict" }, r.verdict),
    h("p", { class: "muted" }, r.receiving_vs_mining),
    h("div", { class: "routes" },
      h("div", { class: "route" },
        h("h3", {}, a.title), h("p", {}, a.what_it_is), h("p", { class: "src" }, a.software),
        kv([
          ["Mining fee", `${a.fees.mining_fee_pct}%`, `${a.fees.verification} source, checked ${a.fees.verified_on}`],
          ["Payout to NiceHash wallet", `min ${a.fees.payout_min_btc} BTC, every ${a.fees.payout_interval_hours} h`],
          ["Withdrawal (on-chain)", `fee ${a.fees.withdrawal_fee_btc_onchain} BTC, min ${a.fees.withdrawal_min_btc_onchain} BTC`],
          ["Inactive funds", `${a.fees.inactive_funds_days}-day rule`],
          ["Unit cross-check", a.calibration ? a.calibration.message : "Needs market data"],
        ]),
        h("h3", {}, "Requirements"), h("ul", {}, a.requirements.map((x) => h("li", {}, x))),
        h("h3", {}, "Risks"), h("ul", {}, a.risks.map((x) => h("li", {}, x)))),
      h("div", { class: "route" },
        h("h3", {}, b.title), h("p", {}, b.what_it_is),
        ub ? kv([
          ["Assumed hash rate", hs(ub.hashrate_hs), ub.hashrate_kind],
          ["Expected earnings", `${ub.sats_per_day.toPrecision(3)} sat/day, ${ub.sats_per_year.toPrecision(3)} sat/year`],
          ["Solo: average time to a block", `${num(ub.expected_years_to_block, 0)} years`],
          ["Chance of a block within a year", ub.probability_block_within_year.toExponential(2)],
          ["Share of network", ub.network_share.toExponential(2)],
          b.cpu_reference ? ["Python SHA-256d reference (1 thread)", hs(b.cpu_reference.hashrate_hs), b.cpu_reference.method] : null,
        ]) : h("p", { class: "muted" }, b.unavailable || "Refresh market data to compute this."),
        h("h3", {}, "Why it cannot compete"), h("ul", {}, b.why_not.map((x) => h("li", {}, x))),
        h("p", { class: "verdict" }, b.recommendation),
        h("div", { class: "actions" }, h("button", { onclick: () => act("/api/sha256-reference", {}, "SHA-256 reference measured.") }, "Measure Python SHA-256 reference (3 s)")))));
}

function renderEstimates(s) {
  const est = s.routes.route_a.estimates;
  const blocks = [];
  for (const device of ["cpu", "gpu"]) {
    const e = est[device];
    blocks.push(h("h3", {}, `${device.toUpperCase()} on NiceHash (${e.algorithm})`));
    if (!e.reliable) {
      blocks.push(h("p", { class: "verdict" }, "Cannot be estimated reliably:"), issues(e.issues));
      continue;
    }
    const x = e.btc;
    const eu = e.eur || {};
    const row = (label, b, euro) => h("tr", {}, h("td", {}, label), h("td", { class: "num" }, btc(b)), h("td", { class: "num" }, e.eur ? eur(euro) : dash));
    blocks.push(h("table", { class: "data" },
      h("thead", {}, h("tr", {}, h("th", {}, "Per day unless stated"), h("th", { class: "num" }, "BTC"), h("th", { class: "num" }, "EUR"))),
      h("tbody", {},
        row("Gross (measured hash rate × NiceHash price)", x.gross_per_day, eu.gross_per_day),
        row(`Miner donation (${e.inputs.miner_fee_pct}%)`, -x.miner_fee_per_day, null),
        row(`NiceHash mining fee (${e.inputs.marketplace_fee_pct}%)`, -x.marketplace_fee_per_day, null),
        row("Withdrawal fee, spread over time", -x.withdrawal_fee_per_day_amortised, null),
        row("Net estimate, per day", x.net_per_day, eu.net_revenue_per_day),
        row("Net estimate, per month", x.net_per_month, null),
        row("Net estimate, per year", x.net_per_year, null),
        row("Electricity, per day", null, eu.electricity_per_day === undefined ? null : -eu.electricity_per_day),
        row("Wear allowance, per day", null, eu.wear_per_day === undefined ? null : -eu.wear_per_day),
        row("After electricity and wear, per day", null, eu.net_after_costs_per_day),
        row("After electricity and wear, per month", null, eu.net_after_costs_per_month),
        row("After electricity and wear, per year", null, eu.net_after_costs_per_year))));
    const p = e.payout;
    blocks.push(kv([
      ["Hash rate used", hs(e.inputs.hashrate_hs), e.inputs.hashrate_kind === "pool_verified" ? "pool-verified" : "local benchmark"],
      ["Power used", e.electricity.watts !== null ? `${num(e.electricity.watts)} W (${e.electricity.power_kind})` : dash, `${num(e.electricity.kwh_per_day, 3)} kWh/day at €${e.electricity.eur_per_kwh}/kWh`],
      ["First NiceHash wallet payout", `after about ${days(p.days_to_internal_min)}`, `minimum ${p.internal_min_btc} BTC, paid every ${p.internal_interval_hours} h`],
      ["Economic withdrawal", `${p.withdrawal_amount_btc} BTC after about ${days(p.days_to_withdrawal)}`, `fee ${p.withdrawal_fee_btc} BTC = ${num(p.withdrawal_fee_pct_of_amount)}% of it`],
      ["Minimum withdrawal", `after about ${days(p.days_to_min_withdrawal)}`],
      ["Break-even electricity", e.break_even.max_electricity_eur_per_kwh !== null ? `€${e.break_even.max_electricity_eur_per_kwh.toFixed(4)}/kWh` : dash, e.break_even.note],
      ["Hardware break-even", e.break_even.hardware],
    ]));
    blocks.push(issues([...p.notes.map((n) => ({ severity: "warning", message: n })), ...e.issues]));
  }
  const any = est.cpu || est.gpu;
  blocks.push(h("details", {}, h("summary", {}, "Costs not included"), h("ul", {}, (any.excluded_costs || []).map((c) => h("li", {}, c)))));
  blocks.push(h("p", { class: "src" }, any.disclaimer));
  blocks.push(h("div", { class: "row" },
    h("select", { id: "pool-device" }, h("option", { value: "cpu" }, "CPU"), h("option", { value: "gpu" }, "GPU")),
    h("input", { id: "pool-hs", class: "num", placeholder: "H/s", inputmode: "decimal", autocomplete: "off" }),
    h("input", { id: "pool-note", class: "grow", placeholder: "Pool-verified hash rate: where from", autocomplete: "off" }),
    h("button", { onclick: () => act("/api/pool-hashrate", { device: document.getElementById("pool-device").value, hashrate_hs: document.getElementById("pool-hs").value, note: document.getElementById("pool-note").value }, "Pool-verified hash rate recorded.") }, "Record")));
  const focused = document.activeElement && document.activeElement.closest && document.activeElement.closest("#est-body");
  if (!focused) fill("est-body", blocks);
}

function renderLedger(s) {
  fill("ledger-body", h("table", { class: "data" },
    h("tbody", {}, Object.entries(s.ledger).map(([kind, v]) => h("tr", {},
      h("td", {}, v.label),
      h("td", { class: "num" }, v.amount_btc === null ? dash : `${v.amount_btc} BTC`),
      h("td", { class: "src" }, kind === "estimated" ? "see the estimate above" : v.as_of ? `${v.source}, ${ago(v.as_of)}` : "not entered"))))));
}

function renderMining(s) {
  const m = s.mining;
  fill("mining-body",
    issues([
      m.approved ? { severity: "warning", message: "Mining is approved in your config. You still confirm each session." }
                 : { severity: "info", message: "Real mining is OFF: [mining] approved = false in config/homeminer.toml. Read docs/SECURITY.md before changing it." },
      { severity: "info", message: `Confirmation phrase: ${m.confirm_phrase}` },
    ]),
    kv([
      ["Route", `NiceHash, XMRig on the CPU`, m.stratum],
      ["State", m.running ? (m.paused ? "paused" : "mining") : "stopped", m.stop_reason ? `Last stop: ${m.stop_reason}` : null],
      ["Saved address", m.saved_address || "none"],
    ]),
    h("p", { class: "src" }, "HomeMiner never asks for seed phrases, private keys or passwords, and never withdraws, trades or sends BTC."));
}

function renderEvents(s) {
  fill("events-body", s.events.map((e) => h("li", { class: e.level }, `${new Date(e.t * 1000).toLocaleTimeString()} — ${e.message}`)));
}

function render(s) {
  renderStatus(s);
  renderHardware(s);
  renderMonitor(s);
  renderBench(s, "cpu");
  renderBench(s, "gpu");
  renderSettings(s);
  renderMarket(s);
  renderRoutes(s);
  renderEstimates(s);
  renderLedger(s);
  renderMining(s);
  renderEvents(s);
}

async function refresh() {
  try {
    state = await api("/api/state");
    render(state);
  } catch (e) {
    const banner = document.getElementById("banner");
    banner.className = "banner bad";
    banner.textContent = TOKEN ? `Lost contact with HomeMiner: ${e.message}` : "No session token. Open the address printed in the HomeMiner window.";
  }
}

// ---------------------------------------------------------------- wiring

async function emergencyStop() {
  try {
    const out = await api("/api/emergency-stop", {});
    flash(`Emergency stop: killed ${out.killed.length} tracked and ${out.swept.length} other miner process(es).`);
  } catch (e) {
    alert(`Emergency stop request failed: ${e.message}. Run scripts\\Stop-HomeMiner.ps1 or close HomeMiner's window.`);
  }
  refresh();
}

document.addEventListener("DOMContentLoaded", () => {
  document.getElementById("emergency").addEventListener("click", emergencyStop);
  document.addEventListener("keydown", (ev) => {
    if (ev.ctrlKey && ev.shiftKey && (ev.key === "X" || ev.key === "x")) { ev.preventDefault(); emergencyStop(); }
  });
  document.getElementById("btn-discover").addEventListener("click", () => act("/api/discover", {}, "Discovery finished."));
  document.getElementById("btn-market").addEventListener("click", () => act("/api/market/refresh", {}, "Market data refreshed."));
  document.querySelectorAll("[data-bench]").forEach((b) => b.addEventListener("click", () => {
    const device = b.dataset.bench;
    const dry = b.dataset.dry === "1";
    if (!dry && !confirm(`Start the ${device.toUpperCase()} benchmark now? It runs XMRig offline (no pool, no wallet) under the configured limits.`)) return;
    act("/api/benchmark", { device, dry_run: dry }, dry ? `${device.toUpperCase()} dry run complete: see the card.` : `${device.toUpperCase()} benchmark started.`);
  }));
  document.querySelectorAll("[data-action]").forEach((b) => b.addEventListener("click", () => act(`/api/${b.dataset.action}`, {})));
  document.getElementById("btn-ledger").addEventListener("click", () => act("/api/ledger", {
    kind: document.getElementById("ledger-kind").value,
    amount_btc: document.getElementById("ledger-amount").value,
    source: document.getElementById("ledger-source").value,
  }, "Ledger entry added."));
  document.getElementById("btn-mine").addEventListener("click", async () => {
    const address = document.getElementById("mining-address").value;
    if (address) {
      const v = await api("/api/address/validate", { address });
      if (!v.check.valid) { alert(v.check.reason); return; }
    }
    if (!confirm("Start REAL mining to NiceHash now? You can stop at any time.")) return;
    const out = await act("/api/mining/start", {
      address, confirmation: document.getElementById("mining-confirm").value,
      remember: document.getElementById("mining-remember").checked,
    }, "Mining started.");
    if (out && out.ok) document.getElementById("mining-address").value = "";
  });
  refresh();
  setInterval(refresh, 2000);
});
