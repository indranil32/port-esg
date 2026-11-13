"use strict";
/* ESG System of Record: single-page UI (no build step). */
const $ = (s, el = document) => el.querySelector(s);
const esc = v => String(v ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmt = (n, d = 0) => n == null ? "–" : Number(n).toLocaleString(undefined, { maximumFractionDigits: d, minimumFractionDigits: d });
const d10 = s => (s || "").toString().slice(0, 10);
const dt16 = s => (s || "").toString().replace("T", " ").slice(0, 16);
const S = { token: localStorage.getItem("esg_token"), user: null, sample: false };

/* ---------- API ---------- */
async function api(path, opts = {}) {
  const o = { method: opts.method || "GET", headers: {} };
  if (S.token) o.headers.Authorization = "Bearer " + S.token;
  if (opts.json !== undefined) { o.headers["Content-Type"] = "application/json"; o.body = JSON.stringify(opts.json); }
  if (opts.form) o.body = opts.form;
  const r = await fetch("/api" + path, o);
  if (r.status === 401 && S.user) { logout(); throw new Error("Session expired"); }
  if (!r.ok) {
    let m = r.statusText; try { const j = await r.json(); m = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail); } catch (_) {}
    throw new Error(m);
  }
  return opts.blob ? r.blob() : r.json();
}
function toast(msg, err) {
  const t = document.createElement("div"); t.className = "toast" + (err ? " err" : ""); t.textContent = msg;
  $("#toast").appendChild(t); setTimeout(() => t.remove(), err ? 7000 : 3500);
}
async function guard(fn) { try { return await fn(); } catch (e) { toast(e.message, true); } }
function modal(html) {
  $("#modal-root").innerHTML = `<div class="overlay"><div class="modal">${html}</div></div>`;
  $(".overlay").addEventListener("mousedown", e => { if (e.target.classList.contains("overlay")) closeModal(); });
}
const closeModal = () => { $("#modal-root").innerHTML = ""; };
async function download(path, filename, openInTab) {
  const b = await api(path, { blob: true });
  const url = URL.createObjectURL(b);
  if (openInTab) window.open(url, "_blank");
  else { const a = document.createElement("a"); a.href = url; a.download = filename; a.click(); }
}

/* ---------- chips ---------- */
const STATUS = { on_track: ["good", "✓ On track"], at_risk: ["warn", "▲ At risk"], off_track: ["bad", "✕ Off track"], no_target: ["none", "– No target"] };
const SEV = { critical: ["bad", "● Critical"], warning: ["warn", "▲ Warning"], info: ["info", "ℹ Info"] };
const ASTAT = { open: ["bad", "Open"], acknowledged: ["warn", "Acknowledged"], resolved: ["none", "Resolved"] };
const VSTAT = { pending: ["warn", "Pending"], validated: ["good", "Validated"], rejected: ["bad", "Rejected"] };
const chip = (map, k) => { const [c, t] = map[k] || ["none", k || "–"]; return `<span class="chip ${c}">${esc(t)}</span>`; };
const has = (...roles) => roles.includes(S.user?.role);

/* ---------- charts ---------- */
const tip = $("#tip");
document.addEventListener("mousemove", e => {
  const t = e.target.closest?.("[data-tip]");
  if (!t) { tip.style.display = "none"; return; }
  tip.innerHTML = t.dataset.tip; tip.style.display = "block";
  tip.style.left = Math.min(e.clientX + 14, innerWidth - 270) + "px"; tip.style.top = (e.clientY + 14) + "px";
});
function niceMax(v) { const p = Math.pow(10, Math.floor(Math.log10(v || 1))); for (const m of [1, 1.2, 1.6, 2, 2.4, 3, 4, 5, 6, 8, 10]) if (m * p >= v) return m * p; return v; }
function stackedBars(cats, series) {
  const W = 640, H = 260, L = 58, B = 26, T = 8, R = 6, pw = W - L - R, ph = H - T - B;
  const totals = cats.map((_, i) => series.reduce((a, s) => a + (s.values[i] || 0), 0));
  const max = niceMax(Math.max(...totals, 1)), bw = pw / cats.length;
  let g = "";
  for (let k = 0; k <= 4; k++) {
    const y = T + ph - ph * k / 4;
    g += `<line x1="${L}" x2="${W - R}" y1="${y}" y2="${y}" stroke="#e2e0da" stroke-width="1"/><text x="${L - 6}" y="${y + 4}" text-anchor="end" font-size="11" fill="#52514e">${fmt(max * k / 4 / 1000, 1)}k</text>`;
  }
  cats.forEach((c, i) => {
    let y = T + ph; const x = L + i * bw + bw * 0.15, w = bw * 0.7;
    const tipHtml = `<b>${esc(c.label)}</b><br>` + series.map(s => `${esc(s.name)}: ${fmt(s.values[i] || 0)} t`).join("<br>") + `<br>Total: ${fmt(totals[i])} t`;
    series.forEach(s => {
      const h = (s.values[i] || 0) / max * ph; if (h <= 0) return;
      y -= h; g += `<rect x="${x}" y="${y}" width="${w}" height="${Math.max(h, 0)}" rx="2" fill="${s.color}" stroke="#fcfcfb" stroke-width="2"/>`;
    });
    g += `<text x="${x + w / 2}" y="${H - 8}" text-anchor="middle" font-size="11" fill="#52514e">${esc(c.label)}</text>`;
    g += `<rect x="${L + i * bw}" y="${T}" width="${bw}" height="${ph}" fill="transparent" data-tip="${esc(tipHtml)}"/>`;
  });
  return `<svg viewBox="0 0 ${W} ${H}" width="100%" role="img" aria-label="Monthly emissions by scope, thousand tonnes CO2e">${g}</svg>`;
}
function hbars(items, color = "var(--series-1)", unit = "t") {
  const max = Math.max(...items.map(i => i.value), 1);
  return items.map(i => `<div class="hbar" data-tip="${esc(i.label)}: ${fmt(i.value)} ${unit}"><span>${esc(i.label)}</span><span class="track"><span class="fill" style="display:block;width:${i.value / max * 100}%;background:${color}"></span></span><span class="right">${fmt(i.value)}</span></div>`).join("");
}
const legend = s => `<div class="legend">${s.map(x => `<span><i style="background:${x.color}"></i>${esc(x.name)}</span>`).join("")}</div>`;

/* ---------- shell / routing ---------- */
const NAV = [
  ["dashboard", "Dashboard", ["esg_admin", "data_owner", "auditor"]], ["emissions", "GHG inventory", ["esg_admin", "data_owner", "auditor"]],
  ["kpis", "KPIs & targets", ["esg_admin", "data_owner", "auditor"]], ["disclosures", "Disclosures", ["esg_admin", "data_owner", "auditor"]],
  ["field", "Field capture", ["field_operator", "esg_admin", "data_owner"]], ["capture", "Data capture", ["esg_admin", "data_owner", "tenant", "field_operator"]], ["approvals", "Approvals", ["esg_admin", "data_owner", "auditor"]],
  ["alerts", "Alerts", ["esg_admin", "data_owner", "auditor"]], ["trace", "Source trace", ["esg_admin", "data_owner", "auditor"]], ["audit", "Audit log", ["esg_admin", "auditor"]], ["admin", "Admin", ["esg_admin"]],
];
const ROUTES = { trace: (v) => traceView(v), field, dashboard, emissions, kpis, disclosures, capture, approvals, alerts, audit, admin };
function logout() { localStorage.removeItem("esg_token"); S.token = null; S.user = null; renderLogin(); }
function renderLogin(err) {
  $("#app").innerHTML = `<div class="login"><div class="card"><h1>ESG System of Record</h1><p class="muted">Sign in to continue.</p>
    <form data-form="login"><div class="field"><label>Email</label><input name="email" type="email" required autofocus autocomplete="username"></div>
    <div class="field"><label>Password</label><input name="password" type="password" required autocomplete="current-password"></div>
    ${err ? `<p class="err-list" style="margin-left:0">${esc(err)}</p>` : ""}<button class="primary" style="width:100%">Sign in</button></form></div></div>`;
}
async function boot() {
  if (!S.token) return renderLogin();
  try { S.user = await api("/auth/me"); } catch (_) { return renderLogin(); }
  if (!["tenant", "field_operator"].includes(S.user.role)) { try { S.sample = (await api("/entities")).some(e => /SAMPLE DATA/.test(e.name)); } catch (_) {} }
  const home = S.user.role === "tenant" ? "capture" : S.user.role === "field_operator" ? "field" : "dashboard";
  if (!location.hash) location.hash = "#/" + home;
  route();
}
async function route() {
  if (!S.user) return;
  const name = (location.hash.replace(/^#\//, "") || "dashboard").split("?")[0];
  const item = NAV.find(n => n[0] === name && n[2].includes(S.user.role)) || NAV.find(n => n[2].includes(S.user.role));
  const links = NAV.filter(n => n[2].includes(S.user.role)).map(n => `<a href="#/${n[0]}" class="${n[0] === item[0] ? "on" : ""}">${n[1]}</a>`).join("");
  $("#app").innerHTML = `<div class="shell"><nav><div class="brand">ESG System of Record<small>Ports cluster</small></div>${links}
    <div class="who">${esc(S.user.full_name || S.user.email)}<br>${esc(S.user.role.replace("_", " "))}<br><button class="sm" data-act="logout">Sign out</button></div></nav>
    <main>${S.sample ? `<div class="banner">Sample data: all figures, emission factors and thresholds are synthetic or illustrative. Replace them before real reporting.</div>` : ""}<div id="view"><p class="muted">Loading…</p></div></main></div>`;
  try { await ROUTES[item[0]]($("#view")); } catch (e) { $("#view").innerHTML = `<div class="card"><b>Could not load this page.</b><p class="muted">${esc(e.message)}</p></div>`; }
}
addEventListener("hashchange", route);

/* ---------- dashboard ---------- */
async function dashboard(v) {
  let sum;
  try { sum = await api("/reports/summary"); } catch (e) { v.innerHTML = noRun(e); return; }
  const [k, al] = await Promise.all([api("/reports/kpis"), api("/alerts?status=open&limit=6")]);
  const run = sum.run, kp = Object.fromEntries(k.rows.filter(r => r.entity_type === "port").map(r => [r.metric_code, r]));
  const sc = Object.fromEntries(sum.by_scope.map(r => [r.key, r.tco2e])), total = (sc[1] || 0) + (sc[2] || 0) + (sc[3] || 0);
  const months = [...new Set(sum.monthly.map(r => d10(r.key)))].sort();
  const label = m => new Date(m + "T00:00:00Z").toLocaleString("en", { month: "short", timeZone: "UTC" });
  const series = [[1, "Scope 1", "var(--series-1)"], [2, "Scope 2", "var(--series-2)"], [3, "Scope 3", "var(--series-3)"]].map(([n, name, c]) =>
    ({ name, color: { 1: "#2a78d6", 2: "#eb6834", 3: "#1baf7a" }[n], values: months.map(m => sum.monthly.filter(r => d10(r.key) === m && r.scope === n).reduce((a, r) => a + r.tco2e, 0)) }));
  const cnt = { on_track: 0, at_risk: 0, off_track: 0 }; Object.values(kp).forEach(r => { if (cnt[r.status] !== undefined) cnt[r.status]++; });
  const crit = sum.open_alerts.critical, est = (sum.data_quality.find(q => q.data_quality === "estimated") || { tco2e: 0 }).tco2e;
  const tile = (label, val, sub, st) => `<div class="card kpi"><div class="label">${label}</div><div class="val">${val}</div><div class="sub">${sub || ""} ${st ? chip(STATUS, st) : ""}</div></div>`;
  v.innerHTML = `<div class="row sb"><div><h1>Dashboard</h1><div class="muted small">${d10(run.period_start)} to ${d10(run.period_end)} · run ${String(run.run_id).slice(0, 8)} · factors ${esc(run.factor_set_version)}</div></div>
      <div class="row">${has("esg_admin", "data_owner") ? `<button data-act="calcDialog">Recalculate…</button>` : ""}<button data-act="annual">Annual report</button></div></div>
    <div class="grid g4" style="margin:14px 0">
      ${tile("Total emissions", fmt(total) + " t", "tCO₂e", kp.GHG_TOTAL?.status)}
      ${tile("Scope 1 / 2", fmt(sc[1] || 0) + " / " + fmt(sc[2] || 0), "tCO₂e", null)}
      ${tile("Scope 3 (screened)", fmt(sc[3] || 0) + " t", `${fmt(est / (total || 1) * 100)}% of total is estimated`, kp.GHG_S3?.status)}
      ${tile("Intensity (Scope 1+2)", fmt(kp.GHG_INT_TEU?.value, 1), "kgCO₂e per TEU", kp.GHG_INT_TEU?.status)}
      ${tile("LTIFR", fmt(kp.LTIFR?.value, 2), "per million hours", kp.LTIFR?.status)}
      ${tile("Berth hours on shore power", fmt(kp.SHORE_POWER_SHARE?.value, 1) + "%", "", kp.SHORE_POWER_SHARE?.status)}
      ${tile("Waste diverted", fmt(kp.WASTE_DIVERSION?.value, 1) + "%", "", kp.WASTE_DIVERSION?.status)}
      ${tile("Open alerts", fmt(sum.open_alerts.total), crit ? `<b style="color:var(--bad)">${crit} critical</b>` : "none critical")}
    </div>
    <div class="grid g2"><div class="card"><h2>Monthly emissions by scope</h2>${legend(series)}${stackedBars(months.map(m => ({ label: label(m) })), series)}
        <details><summary>Show table</summary><div class="scroll"><table><tr><th>Month</th>${series.map(s => `<th class="num">${s.name} (t)</th>`).join("")}</tr>${months.map((m, i) => `<tr><td>${label(m)}</td>${series.map(s => `<td class="num">${fmt(s.values[i])}</td>`).join("")}</tr>`).join("")}</table></div></details></div>
      <div class="card"><h2>Open alerts</h2>${al.length ? al.map(a => `<div style="margin-bottom:8px">${chip(SEV, a.severity)} <b>${esc(a.title)}</b><div class="muted small">${esc(a.message)}</div></div>`).join("") : `<p class="muted">No open alerts.</p>`}<a href="#/alerts">All alerts →</a></div></div>
    <div class="grid g2e" style="margin-top:14px"><div class="card"><h2>Emissions by entity (tCO₂e)</h2>${hbars(sum.by_entity.map(r => ({ label: r.key, value: r.tco2e })).sort((a, b) => b.value - a.value))}</div>
      <div class="card"><h2>KPI status</h2><div class="row" style="margin-bottom:8px">${chip(STATUS, "on_track")} ${cnt.on_track} ${chip(STATUS, "at_risk")} ${cnt.at_risk} ${chip(STATUS, "off_track")} ${cnt.off_track}</div>
        <table>${Object.values(kp).filter(r => r.status === "off_track").map(r => `<tr><td>${esc(r.name)}</td><td class="num">${fmt(r.value, 2)} ${esc(r.unit)}</td><td class="num muted">target ${fmt(r.target_value, 1)}</td></tr>`).join("") || `<tr><td class="muted">Nothing off track.</td></tr>`}</table><a href="#/kpis">All KPIs →</a></div></div>`;
}
const noRun = e => `<div class="card"><h1>No results yet</h1><p class="muted">${esc(e.message)}</p>${has("esg_admin", "data_owner") ? `<button class="primary" data-act="calcDialog">Run calculation</button>` : ""}</div>`;
function calcDialog() {
  const y = new Date().getFullYear(), today = new Date().toISOString().slice(0, 10);
  modal(`<h2>Run GHG and KPI calculation</h2><p class="muted small">Creates a new run; earlier runs are kept for comparison.</p><form data-form="calc">
    <div class="row"><div class="field"><label>From</label><input type="date" name="s" value="${y}-01-01" required></div><div class="field"><label>To</label><input type="date" name="e" value="${today}" required></div></div>
    <button class="primary">Run</button> <button type="button" data-act="closeModal">Cancel</button></form>`);
}

/* ---------- emissions ---------- */
async function emissions(v) {
  const [runs, ents] = await Promise.all([api("/runs"), api("/entities")]);
  const ok = runs.filter(r => r.status === "succeeded");
  if (!ok.length) { v.innerHTML = noRun(new Error("Run a calculation to build the inventory.")); return; }
  v.innerHTML = `<div class="row sb"><h1>GHG inventory</h1><div class="row"><button data-act="csv" data-p="ghg.csv">Export CSV</button><button data-act="annual">Annual report</button>${has("esg_admin", "data_owner") ? `<button data-act="calcDialog">Recalculate…</button>` : ""}</div></div>
    <div class="card" style="margin:12px 0"><div class="row">
      <div><label>Run</label><select id="f-run">${ok.map(r => `<option value="${r.run_id}">${d10(r.period_start)} to ${d10(r.period_end)} · ${String(r.run_id).slice(0, 8)}${r.locked ? " 🔒" : ""}</option>`).join("")}</select></div>
      <div><label>Entity</label><select id="f-ent"><option value="">All (port)</option>${ents.filter(e => e.entity_type !== "port").map(e => `<option value="${e.entity_id}">${esc(e.name)}</option>`).join("")}</select></div>
      <div><label>Consolidation</label><select id="f-app"><option value="all">All entities (100%)</option><option value="equity">Equity share</option><option value="operational">Operational control</option></select></div>
      <div><label>Group by</label><select id="f-grp"><option value="activity">Activity</option><option value="category">Scope / category</option><option value="entity">Entity</option><option value="month">Month</option><option value="scope">Scope</option></select></div></div></div>
    <div id="ghg-out"></div><p class="muted small" style="margin-top:10px">Scope 1: fuel and refrigerants. Scope 2: grid and shore-power electricity. Scope 3: purchased goods (cat 1), vessel auxiliary engines at berth without shore power (cat 9), tenant electricity (cat 13). Emission factors are illustrative until replaced with approved ones.</p>`;
  const load = () => guard(async () => {
    const q = `?run_id=${$("#f-run").value}${$("#f-ent").value ? "&entity=" + $("#f-ent").value : ""}&group=${$("#f-grp").value}&approach=${$("#f-app").value}`;
    const r = await api("/reports/ghg" + q), rows = r.rows, tot = rows.reduce((a, x) => a + x.tco2e, 0);
    $("#ghg-out").innerHTML = `<div class="card"><div class="row sb"><h2>${fmt(tot)} tCO₂e</h2><span class="muted small">${rows.length} rows</span></div>${hbars(rows.map(x => ({ label: (x.key + "").slice(0, 28) + (x.scope && $("#f-grp").value !== "scope" ? ` · S${x.scope}` : ""), value: x.tco2e })).slice(0, 14))}
      <details open><summary>Table</summary><div class="scroll"><table><tr><th>${$("#f-grp").value}</th><th>Scope</th><th class="num">tCO₂e</th><th class="num">Share</th><th class="num">Activity rows</th></tr>${rows.map(x => `<tr><td>${esc(/^\d{4}-\d{2}-\d{2}/.test(String(x.key)) ? String(x.key).slice(0, 7) : x.key)}</td><td>${x.scope ?? ""}</td><td class="num">${fmt(x.tco2e, 1)}</td><td class="num">${fmt(x.tco2e / tot * 100, 1)}%</td><td class="num">${x.activities}</td></tr>`).join("")}</table></div></details></div>`;
  });
  ["f-run", "f-ent", "f-grp", "f-app"].forEach(id => $("#" + id).addEventListener("change", load)); load();
}

/* ---------- KPIs ---------- */
async function kpis(v) {
  const r = await api("/reports/kpis"), run = r.run;
  const rows = r.rows; const pill = { E: "Environmental", S: "Social", G: "Governance" };
  v.innerHTML = `<div class="row sb"><div><h1>KPIs & targets</h1><div class="muted small">${d10(run.period_start)} to ${d10(run.period_end)} · cumulative targets are pro-rated to the days elapsed</div></div>
    <div class="row"><button data-act="csv" data-p="kpis.csv">Export CSV</button></div></div>
    ${["E", "S", "G"].map(p => `<h3>${pill[p]}</h3><div class="scroll"><table><tr><th>Metric</th><th>Level</th><th class="num">Value</th><th>Unit</th><th class="num">Target (annual)</th><th>Status</th></tr>
      ${rows.filter(x => x.pillar === p).map(x => `<tr><td>${esc(x.name)}</td><td>${x.entity_type === "port" ? "Port" : esc(x.entity)}</td><td class="num">${fmt(x.value, 2)}</td><td class="muted">${esc(x.unit)}</td><td class="num">${x.target_value == null ? "–" : fmt(x.target_value, 1)}</td><td>${chip(STATUS, x.status)}</td></tr>`).join("")}</table></div>`).join("")}`;
}

/* ---------- disclosures ---------- */
async function disclosures(v) {
  const r = await api("/reports/disclosures"), CV = { data: ["good", "✓ Data available"], mapped_no_data: ["warn", "▲ Mapped, no data"], not_mapped: ["bad", "✕ No metric mapped"] };
  const fws = [...new Set(r.rows.map(x => x.framework))];
  v.innerHTML = `<h1>Disclosure coverage</h1><p class="muted small">Which framework disclosures are backed by a computed metric. Mappings are indicative; confirm them against your reporting scope.</p>` +
    fws.map(f => { const rs = r.rows.filter(x => x.framework === f), n = rs.filter(x => x.coverage === "data").length;
      return `<h3>${esc(rs[0].framework_name)} · ${n} of ${rs.length} disclosures with data</h3><div class="scroll"><table><tr><th>Disclosure</th><th>Title</th><th>Metrics</th><th class="num">Value</th><th>Coverage</th></tr>${rs.map(x => `<tr><td><b>${esc(x.disclosure_code)}</b></td><td>${esc(x.title)}</td><td class="small">${esc(x.metrics.join(", ") || "–")}</td><td class="num">${x.value == null ? "–" : fmt(x.value, 1)}</td><td>${chip(CV, x.coverage)}</td></tr>`).join("")}</table></div>`; }).join("");
}

/* ---------- data capture ---------- */
const HIDDEN = new Set(["ingested_at", "validation_status", "approved_by", "created_by", "source_system_id", "source_record_id", "version", "reviewer", "review_date"]);
let CAP = { table: null, tab: "form", groups: [] };
async function capture(v) {
  CAP.groups = await api("/tables");
  if (!CAP.linkable) { try { CAP.linkable = (await api("/captures/config")).linkable; } catch (_) { CAP.linkable = []; } }
  if (!CAP.table) CAP.table = CAP.groups[0].tables[0].table;
  v.innerHTML = `<h1>Data capture</h1><p class="muted small">Every entry is validated against the data model, stored as pending, and needs a second person's approval (four-eyes) before it is validated.</p>
    <div class="split"><div class="tlist card" id="tlist"></div><div id="cap-main"></div></div>`;
  drawTableList(); await drawCapture();
}
function drawTableList() {
  $("#tlist").innerHTML = CAP.groups.map(g => `<h4>${esc(g.group)}</h4>` + g.tables.map(t => `<a data-act="pickTable" data-t="${t.table}" class="${t.table === CAP.table ? "on" : ""}">${t.table.replace(/_/g, " ")}${t.writable ? "" : " (read)"}</a>`).join("")).join("");
}
async function drawCapture() {
  const sch = await api("/schema/" + CAP.table); CAP.sch = sch;
  const tabs = [["form", "Enter data"], ["csv", "Upload CSV"], ["recent", "Records"]].filter(t => sch.writable || t[0] === "recent");
  if (!tabs.some(t => t[0] === CAP.tab)) CAP.tab = tabs[0][0];
  $("#cap-main").innerHTML = `<div class="card"><h2>${CAP.table.replace(/_/g, " ")}</h2><div class="tabs">${tabs.map(t => `<button data-act="capTab" data-t="${t[0]}" class="${t[0] === CAP.tab ? "on" : ""}">${t[1]}</button>`).join("")}</div><div id="cap-body"></div></div>`;
  if (CAP.tab === "form") { $("#cap-body").innerHTML = `<form data-form="ingest">${formFields(sch, {})}${evidenceBlock()}<div id="cap-result"></div><button class="primary">Submit for approval</button></form>`; fillLookups($("#cap-body")); }
  else if (CAP.tab === "csv") csvTab(sch); else await recentTab(sch);
}
const baseType = p => Array.isArray(p.type) ? p.type.find(x => x !== "null") : p.type;
function visibleFields(sch) {
  const req = new Set(sch.schema.required || []);
  return Object.entries(sch.schema.properties).filter(([k]) => !HIDDEN.has(k) && !(sch.pk.includes(k) && !req.has(k)) && !(S.user.role === "tenant" && k === "tenant_id"));
}
function formFields(sch, vals) {
  const req = new Set(sch.schema.required || []);
  return `<div class="form">` + visibleFields(sch).map(([k, p]) => {
    const t = baseType(p), val = vals[k], r = req.has(k), id = "f_" + k, lbl = `<label class="${r ? "req" : ""}" for="${id}">${k.replace(/_/g, " ")}</label>`;
    const orig = val == null ? "" : String(val), attrs = `id="${id}" name="${k}" data-orig="${esc(orig)}" data-type="${t}" ${r ? "required" : ""}`;
    let inp;
    if (k === "evidence_doc_id" || k === "doc_id") inp = `<input type="file" id="${id}" name="${k}" data-file="1">`;
    else if (p.description?.startsWith("FK to ")) inp = `<select ${attrs} data-lookup="${p.description.slice(6).split(".")[0]}" data-sel="${esc(orig)}"><option value="">${r ? "Select…" : "(none)"}</option></select>`;
    else if (p.enum) { let en = p.enum.filter(x => x !== null); if (CAP.table === "tenant_esg_submission" && k === "status") en = ["draft", "submitted"];
      inp = `<select ${attrs}>${r ? "" : `<option value=""></option>`}${en.map(x => `<option ${String(val ?? "") === String(x) ? "selected" : ""}>${esc(x)}</option>`).join("")}</select>`; }
    else if (t === "boolean") inp = `<select ${attrs}><option value=""></option><option ${val === true ? "selected" : ""}>true</option><option ${val === false ? "selected" : ""}>false</option></select>`;
    else if (p.format === "date") inp = `<input type="date" ${attrs} value="${esc(d10(val))}">`;
    else if (p.format === "date-time") inp = `<input type="datetime-local" ${attrs} value="${val ? esc(String(val).replace(" ", "T").slice(0, 16)) : ""}" title="UTC">`;
    else if (t === "integer") inp = `<input type="number" step="1" ${attrs} value="${esc(orig)}" ${p.minimum != null ? `min="${p.minimum}"` : ""}>`;
    else if (t === "number") inp = `<input type="number" step="any" ${attrs} value="${esc(orig)}" ${p.minimum != null ? `min="${p.minimum}"` : ""} ${p.maximum != null ? `max="${p.maximum}"` : ""}>`;
    else if (t === "array") inp = `<input ${attrs} value="${esc(Array.isArray(val) ? val.join("|") : orig)}" placeholder="a|b|c">`;
    else inp = `<input ${attrs} value="${esc(orig)}">`;
    return `<div class="field">${lbl}${inp}</div>`;
  }).join("") + `</div>`;
}
async function fillLookups(root) {
  const cache = {};
  for (const sel of root.querySelectorAll("select[data-lookup]")) {
    const t = sel.dataset.lookup;
    try { cache[t] = cache[t] || await api("/lookup/" + t);
      sel.insertAdjacentHTML("beforeend", cache[t].map(o => `<option value="${esc(o.id)}" ${o.id === sel.dataset.sel ? "selected" : ""}>${esc(o.label)}</option>`).join("")); } catch (_) {}
  }
}
async function collect(form, onlyChanged) {
  const rec = {};
  for (const el of form.querySelectorAll("[name]")) {
    const k = el.name;
    if (el.dataset.evidence) continue;
    if (el.dataset.file) {
      if (el.files[0]) { const fd = new FormData(); fd.append("file", el.files[0]); rec[k] = (await api("/evidence", { method: "POST", form: fd })).doc_id; }
      continue;
    }
    let raw = el.value; if (raw === "" || (onlyChanged && raw === el.dataset.orig)) continue;
    const t = el.dataset.type;
    if (t === "integer") raw = parseInt(raw, 10); else if (t === "number") raw = parseFloat(raw);
    else if (t === "boolean") raw = raw === "true"; else if (t === "array") raw = raw.split("|").map(s => s.trim()).filter(Boolean);
    else if (el.type === "datetime-local") raw = raw.length === 16 ? raw + ":00Z" : raw;
    rec[k] = raw;
  }
  return rec;
}
function showResult(res) {
  const ok = res.inserted > 0 && !res.errors.length;
  return `<div class="card" style="margin:10px 0;background:${ok ? "var(--good-bg)" : "var(--surface-2)"}"><b>${res.inserted} of ${res.received} saved</b>${res.duplicates ? ` · ${res.duplicates} duplicate(s) skipped` : ""}
    ${res.errors.length ? `<ul class="err-list">${res.errors.slice(0, 20).map(e => `<li>Row ${e.index + 1}: ${esc(e.error)}</li>`).join("")}</ul>` : ""}</div>`;
}
function csvTab(sch) {
  const props = Object.keys(sch.schema.properties).filter(k => !HIDDEN.has(k) && !(sch.pk.includes(k) && !(sch.schema.required || []).includes(k)));
  $("#cap-body").innerHTML = `<p class="muted small">First row must be column names. Arrays use <code>|</code> between values. Dates as YYYY-MM-DD, timestamps as ISO 8601 (UTC).</p>
    <p class="small"><b>Columns:</b> ${props.map(k => `<code>${k}${(sch.schema.required || []).includes(k) ? "*" : ""}</code>`).join(" ")}</p>
    <div class="row" style="margin:10px 0"><button data-act="template">Download template</button><input type="file" id="csvfile" accept=".csv,text/csv"><button class="primary" data-act="uploadCsv">Upload</button></div><div id="cap-result"></div>`;
}
async function recentTab(sch) {
  const r = await api(`/records/${CAP.table}?limit=15`);
  const cols = visibleFields(sch).map(([k]) => k).slice(0, 7);
  $("#cap-body").innerHTML = `<div class="muted small" style="margin-bottom:6px">${fmt(r.total)} records · newest first</div><div class="scroll"><table><tr>${cols.map(c => `<th>${c.replace(/_/g, " ")}</th>`).join("")}<th>Status</th>${sch.writable ? "<th></th>" : ""}</tr>
    ${r.rows.map((row, i) => `<tr>${cols.map(c => `<td>${esc(String(row[c] ?? "").length > 24 && /^[0-9a-f-]{36}$/.test(row[c]) ? String(row[c]).slice(0, 8) : String(row[c] ?? "").slice(0, 40))}</td>`).join("")}<td>${row.validation_status ? chip(VSTAT, row.validation_status) : esc(row.status || "")}</td>
    ${sch.writable ? `<td><button class="sm" data-act="editRec" data-i="${i}">Correct</button></td>` : ""}</tr>`).join("")}</table></div>`;
  CAP.rows = r.rows;
}
async function editRec(i) {
  const row = CAP.rows[i], sch = CAP.sch;
  modal(`<h2>Correct record</h2><p class="muted small">The change is audited and the record returns to pending approval.</p><form data-form="patch" data-id="${esc(row._id)}">${formFields(sch, row)}<div id="cap-result"></div><button class="primary">Save correction</button> <button type="button" data-act="closeModal">Cancel</button></form>`);
  fillLookups($("#modal-root"));
}

/* ---------- approvals ---------- */
function evCell(r) {
  const ev = r._evidence || [], fl = [...ev.flatMap(e => e.flags || []), ...(r._checks || [])];
  const seen = new Set(), uniq = fl.filter(f => !seen.has(f.code) && seen.add(f.code));
  const read = ev[0]?.candidates?.[0];
  return `${ev.length ? `<button class="sm" data-act="viewEv" data-id="${esc(r._id)}">📷 ${ev.length}</button>` : ""} ${read && r.register_value != null ? `<span class="small ${Math.abs(read.value - r.register_value) < 0.06 ? "muted" : "bad-t"}">photo: ${fmt(read.value, 1)}</span>` : ""}
    ${uniq.filter(f => f.severity !== "info").map(f => `<span class="chip ${FLAGSEV[f.severity]}" title="${esc(f.message)}">${esc(f.code.replace(/_/g, " "))}</span>`).join(" ")}`;
}
let APR = { table: null };
async function approvals(v) {
  const sum = await api("/validation/summary"), tot = sum.reduce((a, r) => a + r.pending, 0);
  v.innerHTML = `<h1>Approvals</h1><p class="muted small">${tot} record(s) pending. You cannot approve what you entered yourself.</p>
    <div class="grid" style="grid-template-columns:repeat(auto-fill,minmax(190px,1fr))">${sum.filter(r => r.pending || r.table_name === APR.table).map(r => `<a class="card" style="text-decoration:none;color:inherit;cursor:pointer;${r.table_name === APR.table ? "outline:2px solid var(--accent)" : ""}" data-act="pickQueue" data-t="${r.table_name}"><div class="small muted">${r.table_name.replace(/_/g, " ")}</div><div style="font-size:22px;font-weight:650">${r.pending}</div><div class="small muted">pending · oldest ${d10(r.oldest_pending) || "–"}</div></a>`).join("") || `<p class="muted">Nothing pending.</p>`}</div><div id="queue" style="margin-top:16px"></div>`;
  if (APR.table) drawQueue();
}
async function drawQueue() {
  const rows = await api("/validation/" + APR.table + "?limit=100"), sch = await api("/schema/" + APR.table);
  const cols = Object.keys(sch.schema.properties).filter(k => !HIDDEN.has(k) && !sch.pk.includes(k)).slice(0, 7); APR.rows = rows;
  $("#queue").innerHTML = `<div class="card"><h2>${APR.table.replace(/_/g, " ")}: pending</h2><div class="scroll"><table><tr>${cols.map(c => `<th>${c.replace(/_/g, " ")}</th>`).join("")}<th>Evidence</th><th>Entered by</th>${has("esg_admin", "data_owner") ? "<th></th>" : ""}</tr>
    ${rows.map((r, i) => `<tr>${cols.map(c => `<td>${esc(c === "meter_id" && r._meter ? r._meter : String(r[c] ?? "").slice(0, 28))}</td>`).join("")}<td>${evCell(r)}</td><td class="small">${esc(r.created_by || "")}</td>${has("esg_admin", "data_owner") ? `<td style="white-space:nowrap"><button class="sm primary" data-act="decide" data-i="${i}" data-a="validate">Validate</button> <button class="sm danger" data-act="decide" data-i="${i}" data-a="reject">Reject</button></td>` : ""}</tr>`).join("") || `<tr><td class="muted">Queue is empty.</td></tr>`}</table></div></div>`;
}


/* ---------- field evidence: photo + OCR ---------- */
const FLAGSEV = { critical: "bad", warning: "warn", info: "info" };
const flagList = fl => (fl || []).length ? `<ul class="flags">${fl.map(f => `<li class="${FLAGSEV[f.severity] || "info"}"><b>${esc(f.code.replace(/_/g, " "))}</b> ${esc(f.message)}</li>`).join("")}</ul>` : `<p class="small muted">No integrity flags.</p>`;
const worst = fl => (fl || []).reduce((w, f) => ({ critical: 3, warning: 2, info: 1 }[f.severity] > ({ critical: 3, warning: 2, info: 1 }[w] || 0) ? f.severity : w), null);
const flagChip = fl => { const w = worst(fl); return w ? `<span class="chip ${FLAGSEV[w]}">${(n => n + " flag" + (n === 1 ? "" : "s"))(fl.filter(f => f.severity !== "info").length || fl.length)}</span>` : ""; };
const blobUrl = async id => URL.createObjectURL(await api(`/captures/${id}/image`, { blob: true }));
function position() {
  return new Promise(res => navigator.geolocation ? navigator.geolocation.getCurrentPosition(p => res(p.coords), () => res(null), { timeout: 4000, maximumAge: 60000 }) : res(null));
}
async function uploadPhoto(file, meterId) {
  const fd = new FormData(); fd.append("file", file);
  if (meterId) fd.append("meter_id", meterId);
  fd.append("client_captured_at", new Date(file.lastModified || Date.now()).toISOString());
  const pos = await position();
  if (pos) { fd.append("gps_lat", pos.latitude); fd.append("gps_lon", pos.longitude); fd.append("gps_accuracy_m", pos.accuracy); }
  return api("/captures", { method: "POST", form: fd });
}
const toLocalInput = iso => { const d = new Date(iso || Date.now()); d.setMinutes(d.getMinutes() - d.getTimezoneOffset()); return d.toISOString().slice(0, 16); };
let FLD = { meters: [], cap: null, url: null };
async function field(v) {
  FLD.meters = await api("/captures/meters"); FLD.cap = null;
  v.innerHTML = `<h1>Field capture</h1><p class="muted small">Photograph the meter display. The number is read from the photo for you to confirm; the photo, its time and location travel with the reading to the approver.</p>
    <div class="card" id="fld-step1"><div class="field"><label class="req" for="fld-meter">Meter</label><select id="fld-meter"><option value="">Select the meter…</option>${FLD.meters.map(m => `<option value="${m.meter_id}">${esc(m.meter_code)} · ${esc(m.meter_type)}${m.location_desc ? " · " + esc(m.location_desc) : ""}</option>`).join("")}</select></div>
      <div id="fld-last" class="small muted" style="margin:-4px 0 10px"></div>
      <label class="bigbtn primary"><input type="file" id="fld-file" accept="image/*" capture="environment" hidden>📷 Take or choose photo</label><span id="fld-busy" class="muted small"></span></div>
    <div id="fld-step2"></div><div class="card" style="margin-top:14px"><h2>My recent captures</h2><div id="fld-recent"></div></div>`;
  $("#fld-meter").onchange = e => { const m = FLD.meters.find(x => x.meter_id === e.target.value), l = m?.last_reading;
    $("#fld-last").textContent = m ? (l ? `Previous reading: ${fmt(l.value, 1)} ${m.uom} on ${dt16(l.read_at)} (${l.status}).` : "No earlier reading: this will be the baseline.") + (m.register_digits ? ` Dial has ${m.register_digits} whole digits.` : "") : ""; };
  $("#fld-file").onchange = e => guard(async () => {
    const f = e.target.files[0]; if (!f) return;
    if (!$("#fld-meter").value) { e.target.value = ""; return toast("Choose the meter first", true); }
    $("#fld-busy").textContent = "Reading photo…";
    try { FLD.cap = await uploadPhoto(f, $("#fld-meter").value); } finally { $("#fld-busy").textContent = ""; }
    if (FLD.url) URL.revokeObjectURL(FLD.url); FLD.url = URL.createObjectURL(f); drawFieldStep2(); });
  fieldRecent();
}
function drawFieldStep2() {
  const c = FLD.cap, m = FLD.meters.find(x => x.meter_id === $("#fld-meter").value), top = c.candidates[0];
  $("#fld-step2").innerHTML = `<div class="card" style="margin-top:14px"><div class="evgrid"><img class="evimg" src="${FLD.url}" alt="Meter photo">
    <form data-form="reading"><h2>Confirm the reading</h2>
      <p class="small muted">${c.ocr_provider === "none" ? "No OCR engine is running; type the number from the display." : `Read by ${esc(c.ocr_provider)}: ${c.candidates.length ? c.candidates.map(x => `<button type="button" class="sm" data-act="pickCand" data-v="${x.value}">${fmt(x.value, 1)} <span class="muted">${Math.round(x.confidence * 100)}%</span></button>`).join(" ") : "no number found"}`}</p>
      <div class="field"><label class="req" for="fld-val">Register value (${esc(m.uom)})</label><input id="fld-val" name="value" type="number" step="any" required value="${top ? top.value : ""}" inputmode="decimal"></div>
      <div class="field"><label for="fld-at">Time of reading</label><input id="fld-at" name="read_at" type="datetime-local" value="${toLocalInput(c.exif_taken_at || c.client_captured_at)}"></div>
      <div class="field"><label><input type="checkbox" name="rollover"> Dial wrapped past its maximum since the last reading (rollover)</label></div>
      <div class="field"><label for="fld-note">Note</label><input id="fld-note" name="note"></div>
      ${flagList(c.flags.filter(f => f.severity !== "info" || ["no_gps", "timestamp_unverified"].includes(f.code)))}
      <div id="fld-result"></div><button class="primary">Submit for approval</button> <button type="button" data-act="fldDiscard">Discard photo</button></form></div></div>`;
}
async function fieldRecent() {
  const rows = await api("/captures?limit=15");
  $("#fld-recent").innerHTML = rows.length ? `<div class="scroll"><table><tr><th>When</th><th>Meter</th><th>Typed</th><th>Photo read</th><th>Status</th><th></th></tr>${rows.map(r =>
    `<tr><td>${dt16(r.created_at)}</td><td>${esc(r.meter_code || "–")}</td><td>${r.entered_value == null ? "–" : fmt(r.entered_value, 1)}</td><td>${r.candidates[0] ? fmt(r.candidates[0].value, 1) : "–"}</td><td>${esc(r.status)} ${flagChip(r.flags)}</td>
    <td><button class="sm" data-act="viewCap" data-id="${r.capture_id}">View</button></td></tr>`).join("")}</table></div>` : `<p class="muted">No photos yet.</p>`;
}
function evidenceBlock() {
  return CAP.linkable?.includes(CAP.table) ? `<div class="field evblock"><label>Evidence photo (optional)</label><input type="file" name="__evidence" accept="image/*" capture="environment" data-evidence="1">
    <span class="small muted">Attached to this record for the approver. The time and location of the photo are captured with it.</span></div>` : "";
}
async function viewEvidence(caps) {
  const parts = [];
  for (const c of caps) {
    let url = ""; try { url = await blobUrl(c.capture_id); } catch (_) {}
    parts.push(`<div class="evgrid"><a href="${url}" target="_blank"><img class="evimg" src="${url}" alt="Evidence photo"></a><div>
      <table class="kv"><tr><td>Captured by</td><td>${esc(c.captured_by)}</td></tr>
      <tr><td>Photo time</td><td>${dt16(c.exif_taken_at) || "–"} <span class="muted small">${c.exif_taken_at ? "camera metadata" : c.client_captured_at ? "device clock: " + dt16(c.client_captured_at) : ""}</span></td></tr>
      <tr><td>Location</td><td>${c.gps_lat != null ? `${Number(c.gps_lat).toFixed(5)}, ${Number(c.gps_lon).toFixed(5)} <span class="muted small">(${esc(c.gps_source || "")})</span>` : "–"}</td></tr>
      <tr><td>Device</td><td>${esc(c.device || "–")}</td></tr>
      <tr><td>Typed value</td><td>${c.entered_value == null ? "–" : fmt(c.entered_value, 2)}</td></tr>
      <tr><td>Read from photo</td><td>${(c.candidates || []).length ? c.candidates.slice(0, 3).map(x => `${fmt(x.value, 2)} (${Math.round(x.confidence * 100)}%)`).join(", ") : "–"} <span class="muted small">${esc(c.ocr_provider || "")}</span></td></tr></table>
      ${flagList(c.flags)}
      ${(c.ocr_lines || []).length ? `<details><summary>All text found in the photo</summary><pre>${esc(c.ocr_lines.map(l => `${l.text}  (${Math.round(l.confidence * 100)}%)`).join("\n"))}</pre></details>` : ""}</div></div>`);
  }
  modal(`<h2>Evidence</h2>${parts.join("<hr>") || "<p class='muted'>No photo attached to this record.</p>"}<p><button data-act="closeModal">Close</button></p>`);
}

/* ---------- alerts ---------- */
let AL = { tab: "inbox", status: "live" };
async function alerts(v) {
  v.innerHTML = `<div class="row sb"><h1>Alerts</h1><div class="row">${has("esg_admin", "data_owner") ? `<button data-act="evalNow">Evaluate rules now</button>` : ""}${has("esg_admin") ? `<button data-act="testNotify">Test webhook</button>` : ""}</div></div>
    <div class="tabs"><button data-act="alTab" data-t="inbox" class="${AL.tab === "inbox" ? "on" : ""}">Inbox</button><button data-act="alTab" data-t="rules" class="${AL.tab === "rules" ? "on" : ""}">Rules</button></div><div id="al-body"></div>`;
  if (AL.tab === "inbox") {
    const q = AL.status === "live" ? "" : AL.status === "all" ? "" : "?status=" + AL.status;
    let rows = await api("/alerts" + q); if (AL.status === "live") rows = rows.filter(a => a.status !== "resolved");
    $("#al-body").innerHTML = `<div class="row" style="margin-bottom:10px"><label style="margin:0">Show</label><select id="al-st">${[["live", "Open + acknowledged"], ["open", "Open"], ["acknowledged", "Acknowledged"], ["resolved", "Resolved"], ["all", "All"]].map(([k, t]) => `<option value="${k}" ${k === AL.status ? "selected" : ""}>${t}</option>`).join("")}</select></div>
      <div class="scroll"><table><tr><th>Severity</th><th>Alert</th><th>Raised</th><th>Status</th><th>Delivery</th><th></th></tr>${rows.map(a => `<tr><td>${chip(SEV, a.severity)}</td><td><b>${esc(a.title)}</b><div class="muted small">${esc(a.message)}</div><div class="muted small">${esc(a.rule_name)}</div></td><td class="small">${dt16(a.raised_at)}</td><td>${chip(ASTAT, a.status)}${a.acknowledged_by ? `<div class="small muted">${esc(a.acknowledged_by)}</div>` : ""}</td><td class="small muted">${esc(a.notify_status || "–")}</td>
      <td style="white-space:nowrap">${a.status !== "resolved" && has("esg_admin", "data_owner") ? `${a.status === "open" ? `<button class="sm" data-act="ack" data-id="${a.alert_id}">Acknowledge</button> ` : ""}<button class="sm" data-act="resolveAlert" data-id="${a.alert_id}">Resolve</button>` : ""}</td></tr>`).join("") || `<tr><td class="muted">No alerts in this view.</td></tr>`}</table></div>`;
    $("#al-st").addEventListener("change", e => { AL.status = e.target.value; alerts(v); });
  } else {
    const rules = await api("/alert-rules"); AL.rules = rules;
    $("#al-body").innerHTML = `<div class="scroll"><table><tr><th>Rule</th><th>Type</th><th>Severity</th><th>Channels</th><th>Live</th><th>Enabled</th>${has("esg_admin") ? "<th></th>" : ""}</tr>${rules.map((r, i) => `<tr><td><b>${esc(r.name)}</b><div class="small muted">${esc(JSON.stringify(r.params))}</div></td><td class="small">${esc(r.rule_type)}</td><td>${chip(SEV, r.severity)}</td><td class="small">${esc(r.channels.join(", ") || "in-app only")}</td><td>${r.live_alerts}</td><td>${r.enabled ? "Yes" : "No"}</td>${has("esg_admin") ? `<td><button class="sm" data-act="editRule" data-i="${i}">Edit</button></td>` : ""}</tr>`).join("")}</table></div>
      <p class="muted small">Thresholds shown are illustrative; set them to your regulatory limits. Rules are evaluated every few minutes; alerts clear automatically when the condition ends.</p>`;
  }
}
function editRule(i) {
  const r = AL.rules[i];
  modal(`<h2>${esc(r.name)}</h2><form data-form="rule" data-id="${r.rule_id}"><div class="form"><div class="field"><label>Severity</label><select name="severity">${["info", "warning", "critical"].map(s => `<option ${s === r.severity ? "selected" : ""}>${s}</option>`).join("")}</select></div>
    <div class="field"><label>Enabled</label><select name="enabled"><option value="true" ${r.enabled ? "selected" : ""}>Yes</option><option value="false" ${!r.enabled ? "selected" : ""}>No</option></select></div></div>
    <div class="field"><label>Channels</label><label style="display:inline"><input type="checkbox" name="ch" value="webhook" ${r.channels.includes("webhook") ? "checked" : ""} style="width:auto"> webhook</label> &nbsp; <label style="display:inline"><input type="checkbox" name="ch" value="email" ${r.channels.includes("email") ? "checked" : ""} style="width:auto"> email</label></div>
    <div class="field"><label>Email recipients (comma separated)</label><input name="recipients" value="${esc(r.recipients.join(", "))}"></div>
    <div class="field"><label>Parameters (JSON)</label><textarea name="params" rows="6">${esc(JSON.stringify(r.params, null, 2))}</textarea></div>
    <button class="primary">Save</button> <button type="button" data-act="closeModal">Cancel</button></form>`);
}

/* ---------- audit & admin ---------- */
async function audit(v) {
  v.innerHTML = `<h1>Audit log</h1><p class="muted small">Append-only. The database rejects edits and deletes.</p><div class="row" style="margin:10px 0"><input id="au-a" placeholder="Actor contains…"><input id="au-t" placeholder="Table"><input id="au-x" placeholder="Action"><button data-act="auFilter">Filter</button></div><div id="au-out"></div>`;
  await auFilter();
}
async function auFilter() {
  const p = new URLSearchParams(); [["actor", "au-a"], ["table", "au-t"], ["action", "au-x"]].forEach(([k, id]) => { const val = $("#" + id).value.trim(); if (val) p.set(k, val); });
  const rows = await api("/audit-log?" + p);
  $("#au-out").innerHTML = `<div class="scroll"><table><tr><th>When</th><th>Who</th><th>Action</th><th>Table</th><th>Record</th><th>Detail</th><th>IP / request</th></tr>${rows.map(r => `<tr><td class="small">${dt16(r.ts)}</td><td class="small">${esc(r.actor)}</td><td>${esc(r.action)}</td><td class="small">${esc(r.table_name || "")}</td><td class="small">${esc((r.record_id || "").slice(0, 13))}</td><td class="small">${esc(r.detail || "")}${r.after ? `<details><summary>changes</summary>${r.before ? `<pre>before: ${esc(JSON.stringify(r.before, null, 1))}</pre>` : ""}<pre>after: ${esc(JSON.stringify(r.after, null, 1).slice(0, 1500))}</pre></details>` : ""}</td><td class="small muted">${esc(r.ip || "")}<br>${esc(r.request_id || "")}</td></tr>`).join("")}</table></div>`;
}
async function admin(v) {
  const [users, src, tenants] = await Promise.all([api("/admin/users"), api("/sources"), api("/lookup/tenant")]);
  v.innerHTML = `<h1>Administration</h1><div class="card" style="margin-bottom:14px"><h2>Users</h2><div class="scroll"><table><tr><th>Email</th><th>Name</th><th>Role</th><th>Tenant</th><th>Last login</th><th>Active</th><th></th></tr>${users.map(u => `<tr><td>${esc(u.email)}</td><td>${esc(u.full_name || "")}</td><td>${esc(u.role)}</td><td class="small">${esc(u.tenant || "")}</td><td class="small">${dt16(u.last_login) || "never"}</td><td>${u.active ? "Yes" : "No"}</td><td><button class="sm" data-act="toggleUser" data-id="${u.user_id}" data-a="${u.active}">${u.active ? "Disable" : "Enable"}</button></td></tr>`).join("")}</table></div>
    <h3>Add user</h3><form data-form="newuser"><div class="form"><div class="field"><label>Email</label><input name="email" type="email" required></div><div class="field"><label>Name</label><input name="full_name"></div>
    <div class="field"><label>Role</label><select name="role"><option>data_owner</option><option>esg_admin</option><option>auditor</option><option>tenant</option></select></div>
    <div class="field"><label>Tenant (tenant role only)</label><select name="tenant_id"><option value=""></option>${tenants.map(t => `<option value="${t.id}">${esc(t.label)}</option>`).join("")}</select></div>
    <div class="field"><label>Initial password (10+ chars)</label><input name="password" type="password" minlength="10" required></div></div><button class="primary">Create user</button></form></div>
    <div class="card"><h2>Source systems</h2><div class="scroll"><table><tr><th>Code</th><th>Name</th><th>Class</th><th>Mode</th><th>Refresh</th></tr>${src.map(s => `<tr><td><b>${esc(s.code)}</b></td><td>${esc(s.name)}</td><td class="small">${esc(s.source_class)}</td><td>${esc(s.ingestion_mode || "")}</td><td>${esc(s.refresh_frequency || "")}</td></tr>`).join("")}</table></div>
    <div id="adm-extra"></div><p class="muted small">External systems post to <code>POST /api/ingest/{table}</code> with a bearer token. Interactive API docs: <a href="/docs" target="_blank">/docs</a>.</p></div>`;
  adminExtra();
}

/* ---------- actions ---------- */
const ACT = {
  logout, closeModal, calcDialog,
  csv: el => guard(() => download("/reports/" + el.dataset.p, el.dataset.p)),
  annual: () => guard(() => download("/reports/annual.html", "", true)),
  pickTable: el => { CAP.table = el.dataset.t; CAP.tab = "form"; drawTableList(); guard(drawCapture); },
  capTab: el => { CAP.tab = el.dataset.t; guard(drawCapture); },
  template: () => { const b = new Blob([visibleFields(CAP.sch).map(([k]) => k).join(",") + "\n"], { type: "text/csv" }); const a = document.createElement("a"); a.href = URL.createObjectURL(b); a.download = CAP.table + "_template.csv"; a.click(); },
  uploadCsv: () => guard(async () => { const f = $("#csvfile").files[0]; if (!f) return toast("Choose a CSV file first", true); const fd = new FormData(); fd.append("file", f);
    const res = await api(`/ingest/${CAP.table}/csv`, { method: "POST", form: fd }); $("#cap-result").innerHTML = showResult(res); }),
  editRec: el => editRec(+el.dataset.i),
  pickQueue: el => { APR.table = el.dataset.t; route(); },
  decide: el => guard(async () => { const r = APR.rows[+el.dataset.i], a = el.dataset.a; let comment = "";
    if (a === "reject") { comment = prompt("Reason for rejection (required):"); if (!comment) return; }
    await api(`/validation/${APR.table}/${r._id}`, { method: "POST", json: { action: a, comment } }); toast(a === "validate" ? "Validated" : "Rejected"); route(); }),
  evalNow: () => guard(async () => { const r = await api("/alerts/evaluate", { method: "POST" }); toast(`Evaluated ${r.length} rules: ${r.reduce((a, x) => a + (x.raised || 0), 0)} new, ${r.reduce((a, x) => a + (x.resolved || 0), 0)} cleared`); route(); }),
  testNotify: () => guard(async () => toast("Webhook test: " + (await api("/alerts/test-notification", { method: "POST" })).status)),
  alTab: el => { AL.tab = el.dataset.t; route(); },
  ack: el => guard(async () => { await api(`/alerts/${el.dataset.id}/ack`, { method: "POST" }); route(); }),
  resolveAlert: el => guard(async () => { await api(`/alerts/${el.dataset.id}/resolve`, { method: "POST" }); route(); }),
  pickCand: el => { $("#fld-val").value = el.dataset.v; },
  fldDiscard: () => guard(async () => { await api(`/captures/${FLD.cap.capture_id}/discard`, { method: "POST" }); FLD.cap = null; $("#fld-step2").innerHTML = ""; $("#fld-file").value = ""; fieldRecent(); toast("Photo discarded"); }),
  viewCap: el => guard(async () => viewEvidence([await api("/captures/" + el.dataset.id)])),
  viewEv: el => guard(async () => viewEvidence(await api(`/captures/for/${APR.table}/${encodeURIComponent(el.dataset.id)}`))),
  editRule, auFilter: () => guard(auFilter),
  toggleUser: el => guard(async () => { await api("/admin/users/" + el.dataset.id, { method: "PATCH", json: { active: el.dataset.a !== "true" } }); route(); }),
};
document.addEventListener("click", e => { const el = e.target.closest("[data-act]"); if (el && ACT[el.dataset.act]) { e.preventDefault(); ACT[el.dataset.act](el); } });
const FORMS = {
  login: async f => { try { const r = await api("/auth/login", { method: "POST", json: { email: f.email.value, password: f.password.value } });
    S.token = r.token; localStorage.setItem("esg_token", r.token); location.hash = ""; await boot(); } catch (e) { renderLogin(e.message); } },
  calc: f => guard(async () => { const r = await api("/calc/run", { method: "POST", json: { period_start: f.s.value, period_end: f.e.value } }); closeModal();
    toast(`Calculated ${r.activities} activities` + (r.warnings.length ? `. Warnings: ${r.warnings.join("; ")}` : "")); route(); }),
  ingest: f => guard(async () => { const rec = await collect(f, false), photo = f.querySelector("[data-evidence]")?.files[0];
    let cap = null; if (photo) cap = await uploadPhoto(photo, rec.meter_id);
    const res = await api(`/ingest/${CAP.table}`, { method: "POST", json: { source_system_code: cap ? "FIELD_APP" : "MANUAL", records: [rec] } });
    if (cap && res.inserted) { try { await api(`/captures/${cap.capture_id}/link`, { method: "POST", json: { table: CAP.table, record_id: res.ids[0] } }); res.linked = true; } catch (e) { toast("Saved, but the photo could not be attached: " + e.message, true); } }
    $("#cap-result").innerHTML = showResult(res) + (res.linked ? flagList(cap.flags.filter(x => x.severity !== "info")) : ""); if (res.inserted) f.reset(); }),
  patch: f => guard(async () => { const rec = await collect(f, true); if (!Object.keys(rec).length) return toast("Nothing changed", true);
    await api(`/records/${CAP.table}/${encodeURIComponent(f.dataset.id)}`, { method: "PATCH", json: rec }); closeModal(); toast("Correction saved; awaiting approval"); drawCapture(); }),
  reading: f => guard(async () => {
    const body = { capture_id: FLD.cap.capture_id, meter_id: $("#fld-meter").value, register_value: parseFloat(f.value.value), rollover: f.rollover.checked, note: f.note.value || null };
    if (f.read_at.value) body.read_at = new Date(f.read_at.value).toISOString();
    const r = await api("/captures/submit-reading", { method: "POST", json: body });
    toast("Submitted for approval" + (r.flags.some(x => x.severity === "critical") ? " (flagged for review)" : ""));
    FLD.cap = null; $("#fld-step2").innerHTML = `<div class="card" style="margin-top:14px;background:var(--good-bg)"><b>Reading submitted.</b>${flagList(r.flags.filter(x => x.severity !== "info"))}</div>`;
    $("#fld-file").value = ""; fieldRecent(); }),
  rule: f => guard(async () => { let params; try { params = JSON.parse(f.params.value); } catch (_) { return toast("Parameters must be valid JSON", true); }
    await api("/alert-rules/" + f.dataset.id, { method: "PATCH", json: { severity: f.severity.value, enabled: f.enabled.value === "true", channels: [...f.querySelectorAll("[name=ch]:checked")].map(x => x.value),
      recipients: f.recipients.value.split(",").map(s => s.trim()).filter(Boolean), params } }); closeModal(); toast("Rule saved"); route(); }),
  newuser: f => guard(async () => { await api("/admin/users", { method: "POST", json: { email: f.email.value, full_name: f.full_name.value, role: f.role.value, password: f.password.value, tenant_id: f.tenant_id.value || null } }); toast("User created"); route(); }),
};
document.addEventListener("submit", e => { const f = e.target.closest("[data-form]"); if (f && FORMS[f.dataset.form]) { e.preventDefault(); FORMS[f.dataset.form](f); } });
boot();
