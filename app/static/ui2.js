"use strict";
/* Round 1 UI: source trace, period locks, factors, third-party collection links. */

/* ---------- source trace ---------- */
const TR = { run: null };
async function traceView(v) {
  const runs = (await api("/runs")).filter(r => r.status === "succeeded");
  if (!runs.length) { v.innerHTML = `<h1>Source trace</h1><div class="card"><p class="muted">Run a calculation first.</p></div>`; return; }
  v.innerHTML = `<h1>Source trace</h1><p class="muted small">Follow any reported number down to the records, people, photos and files behind it.</p>
   <div class="card" style="margin:12px 0"><div class="row"><div><label>Run</label><select id="tr-run">${runs.map(r => `<option value="${r.run_id}">${d10(r.period_start)} to ${d10(r.period_end)} · ${String(r.run_id).slice(0, 8)}${r.locked ? " 🔒" : ""}</option>`).join("")}</select></div>
   <div><label>Scope</label><select id="tr-scope"><option value="">All</option><option>1</option><option>2</option><option>3</option></select></div>
   <div><label>Search</label><input id="tr-q" placeholder="activity or entity"></div><button data-act="trLoad">Show</button></div></div><div id="tr-out"></div>`;
  await trLoad();
}
async function trLoad() {
  const p = new URLSearchParams(); const sc = $("#tr-scope").value, q = $("#tr-q").value.trim(); if (sc) p.set("scope", sc); if (q) p.set("q", q);
  const r = await api(`/trace/runs/${$("#tr-run").value}/activities?` + p);
  $("#tr-out").innerHTML = `<div class="card scroll"><table><tr><th>Activity</th><th>Entity</th><th>Scope</th><th class="num">Quantity</th><th class="num">tCO₂e</th><th></th></tr>${r.rows.map(x =>
    `<tr><td>${esc(x.activity_type || x.activity || "")}</td><td class="small">${esc(x.entity || "")}</td><td>${x.scope ?? ""}</td><td class="num">${fmt(x.quantity, 1)} <span class="muted small">${esc(x.uom || "")}</span></td><td class="num">${fmt(x.tco2e, 2)}</td><td><button class="sm" data-act="trOpen" data-id="${x.activity_id}">Trace</button></td></tr>`).join("") || `<tr><td colspan="6" class="muted">No activities match.</td></tr>`}</table></div>`;
}
async function trOpen(id) {
  const d = await api("/trace/activity/" + id);
  const em = d.emissions.map(e => `<tr><td>${esc(e.gas || "")}</td><td class="num">${fmt(e.co2e_tonnes, 3)}</td><td class="small">factor ${esc(e.factor_value ?? "")} ${esc(e.factor_uom || "")}<br>${esc(e.factor_source || "")}</td></tr>`).join("");
  const src = d.sources.map((s, i) => {
    const row = Object.entries(s.row).filter(([k, val]) => val != null && !/(_id|hash|payload)$/.test(k)).slice(0, 10).map(([k, val]) => `${esc(k)}: <b>${esc(String(val).slice(0, 40))}</b>`).join(" · ");
    const caps = (s.captures || []).map(c => `<button class="sm" data-act="viewCap" data-id="${c.capture_id}">Photo · ${esc(c.captured_by)}</button>`).join(" ");
    const der = s.derived_from ? `<div class="small muted">Derived from hand-read register ${esc(s.derived_from.register_value ?? "")} on ${dt16(s.derived_from.read_at)}</div>` : "";
    return `<div class="card" style="margin:6px 0;padding:8px"><div class="small">${row}</div>${der}<div class="small muted">Entered by ${esc(s.row.created_by || s.row.source_system || "system")} · ${esc(s.row.validation_status || "")}${s.row.validated_by ? " by " + esc(s.row.validated_by) : ""}</div>${caps}</div>`;
  }).join("");
  modal(`<h2>${esc(d.activity.activity_type || "Activity")}</h2><p class="muted small">Run ${String(d.run.run_id).slice(0, 8)} · ${d10(d.run.period_start)} to ${d10(d.run.period_end)}</p>
    <table><tr><th>Gas</th><th class="num">tCO₂e</th><th>Emission factor</th></tr>${em}</table><h3>Source records</h3>${src || `<p class="muted">None.</p>`}${d.note ? `<p class="muted small">${esc(d.note)}</p>` : ""}<p><button data-act="closeModal">Close</button></p>`);
}

/* ---------- admin: locks, factors, collection links ---------- */
async function adminExtra() {
  const [locks, runs, factors, links, tenants, metrics, batches] = await Promise.all([api("/periods"), api("/runs"), api("/periods/factors"), api("/collect-links"), api("/lookup/tenant"), api("/collect-links/metrics"), api("/batches")]);
  const ok = runs.filter(r => r.status === "succeeded");
  $("#adm-extra").innerHTML = `<div class="card" style="margin-bottom:14px"><h2>Reporting period locks</h2>
    <p class="muted small">A locked period freezes its data, its calculation and the emission factors behind it. Unlocking needs a reason and is audited.</p>
    <table><tr><th>Label</th><th>Period</th><th>Locked</th><th>Status</th><th></th></tr>${locks.map(l => `<tr><td>${esc(l.label)}</td><td class="small">${d10(l.period_start)} to ${d10(l.period_end)}</td><td class="small">${esc(l.locked_by)} · ${dt16(l.locked_at)}<br>${esc(l.reason || "")}</td>
      <td>${l.active ? `<span class="chip bad">🔒 Locked</span>` : `<span class="muted small">Unlocked ${dt16(l.unlocked_at)}<br>${esc(l.unlock_reason || "")}</span>`}</td><td>${l.active ? `<button class="sm" data-act="unlockPeriod" data-id="${l.lock_id}">Unlock…</button>` : ""}</td></tr>`).join("") || `<tr><td colspan="5" class="muted">No locks yet.</td></tr>`}</table>
    <form data-form="lock" class="row" style="margin-top:8px"><select name="run_id" required>${ok.filter(r => !r.locked).map(r => `<option value="${r.run_id}">${d10(r.period_start)} to ${d10(r.period_end)} · ${String(r.run_id).slice(0, 8)}</option>`).join("")}</select>
    <input name="label" placeholder="Label, e.g. FY2025 annual report" required><input name="reason" placeholder="Reason" required><button class="primary">Lock period</button></form></div>
    <div class="card" style="margin-bottom:14px"><h2>Data loads (API and CSV)</h2><p class="muted small">Each load is a batch. Withdrawing a bad file rejects all of its records in one step; they stay on file and are excluded from every calculation.</p>
    <div class="scroll" style="max-height:240px"><table><tr><th>When</th><th>Table</th><th>By</th><th class="num">Records</th><th>Status</th><th></th></tr>${batches.map(b => `<tr><td class="small">${dt16(b.created_at)}</td><td class="small">${esc(b.table_name)}<br>${esc(b.filename || b.source_code || "")}</td><td class="small">${esc(b.created_by)}</td><td class="num">${b.inserted}</td>
      <td>${b.active ? "Loaded" : `<span class="muted small">Withdrawn ${b.withdrawn_count} · ${esc(b.withdraw_reason || "")}</span>`}</td><td>${b.active ? `<button class="sm" data-act="withdrawBatch" data-id="${b.batch_id}">Withdraw…</button>` : ""}</td></tr>`).join("") || `<tr><td colspan="6" class="muted">No loads yet.</td></tr>`}</table></div></div>
    <div class="card" style="margin-bottom:14px"><h2>Emission factors</h2><div class="scroll" style="max-height:240px"><table><tr><th>Factor</th><th class="num">Value</th><th>Valid from</th><th>Source</th><th></th></tr>${factors.map(f => `<tr><td class="small">${esc(f.activity_type + " / " + (f.fuel_or_grid || ""))}</td><td class="num">${esc(f.value)} <span class="muted small">${esc(f.uom || "")}</span></td><td class="small">${d10(f.valid_from)}</td><td class="small">${esc(f.source || "")}</td><td>${f.in_locked_period ? `<span class="chip bad">🔒 in locked period</span>` : ""}</td></tr>`).join("")}</table></div></div>
    <div class="card" style="margin-bottom:14px"><h2>Third-party data collection links</h2><p class="muted small">Send a tenant or contractor a private link to submit specific metrics without an account. The link expires, works once, and everything they send waits for approval.</p>
    <table><tr><th>Tenant</th><th>Period</th><th>Status</th><th>Expires</th><th></th></tr>${links.map(l => `<tr><td>${esc(l.tenant || "")}</td><td class="small">${d10(l.period_start)} to ${d10(l.period_end)}</td><td>${esc(l.status)}</td><td class="small">${dt16(l.expires_at)}</td><td>${l.status === "open" ? `<button class="sm" data-act="revokeLink" data-id="${l.link_id}">Revoke</button>` : ""}</td></tr>`).join("") || `<tr><td colspan="5" class="muted">None yet.</td></tr>`}</table>
    <form data-form="newlink" style="margin-top:8px"><div class="form"><div class="field"><label>Tenant</label><select name="tenant_id" required>${tenants.map(t => `<option value="${t.id}">${esc(t.label)}</option>`).join("")}</select></div>
    <div class="field"><label>From</label><input type="date" name="s" required></div><div class="field"><label>To</label><input type="date" name="e" required></div>
    <div class="field"><label>Message</label><input name="message"></div></div><label>Metrics requested</label><div class="row" style="flex-wrap:wrap">${metrics.map(m => `<label class="small"><input type="checkbox" name="m" value="${m.id}"> ${esc(m.label)}</label>`).join("")}</div>
    <button class="primary" style="margin-top:8px">Create link</button></form><div id="link-out"></div></div>`;
}
Object.assign(ACT, {
  trLoad: () => guard(trLoad), trOpen: el => guard(() => trOpen(el.dataset.id)),
  unlockPeriod: el => guard(async () => { const reason = prompt("Reason for unlocking (required):"); if (!reason) return;
    await api(`/periods/${el.dataset.id}/unlock`, { method: "POST", json: { reason } }); toast("Period unlocked"); route(); }),
  withdrawBatch: el => guard(async () => { const reason = prompt("Reason for withdrawing this load (required):"); if (!reason) return;
    const r = await api(`/batches/${el.dataset.id}/withdraw`, { method: "POST", json: { reason } }); toast(r.withdrawn + " records withdrawn"); route(); }),
  revokeLink: el => guard(async () => { await api(`/collect-links/${el.dataset.id}/revoke`, { method: "POST" }); route(); }),
});
Object.assign(FORMS, {
  lock: f => guard(async () => { await api("/periods/lock", { method: "POST", json: { run_id: f.run_id.value, label: f.label.value, reason: f.reason.value } }); toast("Period locked"); route(); }),
  newlink: f => guard(async () => { const ids = [...f.querySelectorAll("[name=m]:checked")].map(x => x.value); if (!ids.length) return toast("Choose at least one metric", true);
    const r = await api("/collect-links", { method: "POST", json: { tenant_id: f.tenant_id.value, metric_ids: ids, period_start: f.s.value, period_end: f.e.value, message: f.message.value || null } });
    const url = location.origin + r.path; $("#link-out").innerHTML = `<div class="card" style="margin-top:8px;background:var(--good-bg)"><b>Link created.</b> Copy it now; it is shown only once.<br><input readonly style="width:100%" value="${esc(url)}" onclick="this.select()"></div>`; }),
});

/* ---------- EMS: ISO 14001 / 50001 ---------- */
NAV.splice(NAV.findIndex(n => n[0] === "alerts") + 1, 0, ["ems", "EMS (ISO 14001/50001)", ["esg_admin", "data_owner", "auditor"]]);
ROUTES.ems = emsView;
const EMS = { tab: "objectives" };
async function emsView(v) {
  const t = EMS.tab, w = has("esg_admin", "data_owner");
  const tabs = [["objectives", "Objectives & actions"], ["audits", "Internal audits"], ["reviews", "Management reviews"]]
    .map(([k, l]) => `<button class="${k === t ? "primary" : ""}" data-act="emsTab" data-t="${k}">${l}</button>`).join(" ");
  let body = "";
  if (t === "objectives") {
    const [objs, acts, metrics, ents] = await Promise.all([api("/ems/objectives"), api("/ems/actions"), api("/lookup/metric"), api("/lookup/entity")]);
    body = `<div class="scroll"><table><tr><th>Std</th><th>Objective</th><th>Baseline → target</th><th class="num">Now</th><th class="num">Progress</th><th>Actions</th><th>Status</th><th></th></tr>${objs.map(o => `<tr><td class="small">${o.standard.replace("ISO", "")}</td><td>${esc(o.title)}<div class="muted small">${esc(o.metric_code || "")} ${esc(o.owner || "")} ${o.target_date ? "· by " + d10(o.target_date) : ""}</div></td>
      <td class="small">${o.baseline_value ?? "–"} → ${o.target_value ?? "–"}</td><td class="num">${o.current_value == null ? "–" : fmt(o.current_value, 1)}</td><td class="num">${o.progress_pct == null ? "–" : o.progress_pct + "%"}</td>
      <td class="small">${o.actions_done}/${o.actions} done${o.actions_overdue ? ` <span class="chip bad">${o.actions_overdue} overdue</span>` : ""}</td><td>${esc(o.status)}</td>
      <td>${w ? `<button class="sm" data-act="emsAction" data-id="${o.objective_id}">+ Action</button> ${o.status === "active" ? `<button class="sm" data-act="emsAchieved" data-id="${o.objective_id}">Achieved</button>` : ""}` : ""}</td></tr>`).join("") || `<tr><td colspan="8" class="muted">No objectives yet.</td></tr>`}</table></div>
    <h3>Actions</h3><div class="scroll"><table><tr><th>Action</th><th>Objective</th><th>Owner</th><th>Due</th><th>Status</th><th></th></tr>${acts.map(a => `<tr><td>${esc(a.title)}</td><td class="small">${esc(a.objective)}</td><td class="small">${esc(a.owner || "")}</td><td class="small">${d10(a.due_date)}${a.status !== "done" && a.due_date && d10(a.due_date) < new Date().toISOString().slice(0, 10) ? ` <span class="chip bad">overdue</span>` : ""}</td><td>${esc(a.status)}</td>
      <td>${w && a.status !== "done" ? `<button class="sm" data-act="emsDone" data-id="${a.action_id}">Mark done</button>` : ""}</td></tr>`).join("") || `<tr><td colspan="6" class="muted">None.</td></tr>`}</table></div>
    ${w ? `<h3>New objective</h3><form data-form="emsobj"><div class="form"><div class="field"><label>Standard</label><select name="standard"><option value="ISO14001">ISO 14001 (environment)</option><option value="ISO50001">ISO 50001 (energy)</option></select></div>
      <div class="field"><label>Objective</label><input name="title" required></div><div class="field"><label>Linked KPI (optional)</label><select name="metric_id"><option value=""></option>${metrics.map(m => `<option value="${m.id}">${esc(m.label)}</option>`).join("")}</select></div>
      <div class="field"><label>Baseline</label><input name="baseline_value" type="number" step="any"></div><div class="field"><label>Target</label><input name="target_value" type="number" step="any"></div>
      <div class="field"><label>Target date</label><input name="target_date" type="date"></div><div class="field"><label>Owner</label><input name="owner"></div></div><button class="primary">Add objective</button></form>` : ""}`;
  } else if (t === "audits") {
    const au = await api("/ems/audits");
    body = `<div class="scroll"><table><tr><th>Ref</th><th>Standard</th><th>Scope</th><th>Planned</th><th>Auditor</th><th>Status</th><th>Findings (open)</th><th></th></tr>${au.map(a => `<tr><td>${esc(a.audit_ref)}</td><td class="small">${a.standard}</td><td class="small">${esc(a.scope || "")}</td><td class="small">${d10(a.planned_date)}${a.overdue ? ` <span class="chip bad">overdue</span>` : ""}</td><td class="small">${esc(a.auditor || "")}</td><td>${a.status === "done" ? "Done " + d10(a.performed_date) : "Planned"}</td><td class="small">${a.findings} (${a.open_findings})</td>
      <td>${w && a.status === "planned" ? `<button class="sm" data-act="emsComplete" data-id="${a.audit_id}">Mark done</button>` : ""}</td></tr>`).join("") || `<tr><td colspan="8" class="muted">No audits yet.</td></tr>`}</table></div>
    <p class="muted small">Record findings under Data capture → Audit findings, using the audit reference above.</p>
    ${w ? `<h3>Plan an audit</h3><form data-form="emsaudit"><div class="form"><div class="field"><label>Reference</label><input name="audit_ref" required></div><div class="field"><label>Standard</label><select name="standard"><option value="ISO14001">ISO 14001</option><option value="ISO50001">ISO 50001</option></select></div>
      <div class="field"><label>Scope</label><input name="scope"></div><div class="field"><label>Planned date</label><input name="planned_date" type="date" required></div><div class="field"><label>Auditor</label><input name="auditor"></div></div><button class="primary">Plan audit</button></form>` : ""}`;
  } else {
    const rv = await api("/ems/reviews");
    body = `${rv.map(r => { const s = r.snapshot; return `<div class="card" style="margin:8px 0"><b>${r.standard} · ${d10(r.review_date)}</b> <span class="muted small">${esc(r.created_by)}</span>
      <p class="small">Attendees: ${esc(r.attendees || "–")}<br>Decisions: ${esc(r.decisions || "–")}</p>
      <p class="small muted">System state at the time: KPIs on track ${s.kpi_status?.on_track ?? 0}, at risk ${s.kpi_status?.at_risk ?? 0}, off track ${s.kpi_status?.off_track ?? 0} · open findings ${s.open_findings.n} (${s.open_findings.high} high) · overdue actions ${s.overdue_actions} · audits done in 12 months ${s.audits_done_12m} · environmental incidents in 12 months ${s.environmental_incidents_12m} · ${s.objectives.length} objectives</p></div>`; }).join("") || `<p class="muted">No reviews yet.</p>`}
    ${w ? `<h3>Record a management review</h3><p class="muted small">A snapshot of objectives, KPIs, findings and overdue actions is stored with it.</p><form data-form="emsreview"><div class="form"><div class="field"><label>Standard</label><select name="standard"><option value="ISO14001">ISO 14001</option><option value="ISO50001">ISO 50001</option></select></div>
      <div class="field"><label>Date</label><input name="review_date" type="date" required></div><div class="field"><label>Attendees</label><input name="attendees"></div><div class="field"><label>Decisions and actions</label><input name="decisions"></div></div><button class="primary">Record review</button></form>` : ""}`;
  }
  v.innerHTML = `<h1>Environmental and energy management</h1><p class="muted small">Objectives, action plans, internal audits and management reviews for ISO 14001 and ISO 50001. This helps run the system and evidence it; it does not certify it.</p><div class="row" style="margin:10px 0">${tabs}</div>${body}`;
}
Object.assign(ACT, {
  emsTab: el => { EMS.tab = el.dataset.t; route(); },
  emsAction: el => { const title = prompt("Action:"); if (!title) return; const due = prompt("Due date (YYYY-MM-DD, optional):") || null; const owner = prompt("Owner (optional):") || null;
    guard(async () => { await api(`/ems/objectives/${el.dataset.id}/actions`, { method: "POST", json: { title, due_date: due, owner } }); route(); }); },
  emsDone: el => guard(async () => { await api(`/ems/actions/${el.dataset.id}`, { method: "PATCH", json: { status: "done" } }); route(); }),
  emsAchieved: el => guard(async () => { await api(`/ems/objectives/${el.dataset.id}`, { method: "PATCH", json: { status: "achieved" } }); route(); }),
  emsComplete: el => guard(async () => { await api(`/ems/audits/${el.dataset.id}/complete`, { method: "POST" }); route(); }),
});
const num = x => x === "" ? null : Number(x);
Object.assign(FORMS, {
  emsobj: f => guard(async () => { await api("/ems/objectives", { method: "POST", json: { standard: f.standard.value, title: f.title.value, metric_id: f.metric_id.value || null, baseline_value: num(f.baseline_value.value), target_value: num(f.target_value.value), target_date: f.target_date.value || null, owner: f.owner.value || null } }); route(); }),
  emsaudit: f => guard(async () => { await api("/ems/audits", { method: "POST", json: { audit_ref: f.audit_ref.value, standard: f.standard.value, scope: f.scope.value || null, planned_date: f.planned_date.value, auditor: f.auditor.value || null } }); route(); }),
  emsreview: f => guard(async () => { await api("/ems/reviews", { method: "POST", json: { standard: f.standard.value, review_date: f.review_date.value, attendees: f.attendees.value || null, decisions: f.decisions.value || null } }); toast("Review recorded"); route(); }),
});
