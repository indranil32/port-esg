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
    <div class="card" style="margin-bottom:14px"><h2>Import reference data (CSV)</h2><p class="muted small">Upserts by code and is audited. Columns: metrics <code>metric_code,name,pillar,unit,frequency,source_classes</code>; framework mappings <code>framework_code,framework_name,disclosure_code,title,metric_codes</code>; legal register <code>ref,title,kind,authority,entity_code,obligation,owner,permit_expiry,review_due</code>. Use | between list values.</p>
    <div class="row">${[["metrics", "Metric library"], ["framework-map", "Framework mappings"], ["legal", "Legal register"]].map(([k, l]) => `<div><label>${l}</label><input type="file" accept=".csv" id="imp-${k}"> <button class="sm" data-act="importRef" data-k="${k}">Import</button></div>`).join("")}</div><div id="imp-out" class="small"></div></div>
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
  importRef: el => guard(async () => { const f = $("#imp-" + el.dataset.k).files[0]; if (!f) return toast("Choose a CSV file first", true); const fd = new FormData(); fd.append("file", f);
    const r = await api("/import/" + el.dataset.k, { method: "POST", form: fd }); $("#imp-out").innerHTML = `<b>${esc(JSON.stringify({ ...r, errors: undefined }))}</b>` + (r.errors.length ? "<ul>" + r.errors.slice(0, 10).map(e => `<li>row ${e.row}: ${esc(e.error)}</li>`).join("") + "</ul>" : ""); }),
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
  const tabs = [["objectives", "Objectives & actions"], ["nc", "Nonconformities"], ["legal", "Legal & permits"], ["enpi", "Energy baselines"], ["audits", "Internal audits"], ["reviews", "Management reviews"]]
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
  } else if (t === "legal") {
    const L = await api("/ems/legal"), CL = { compliant: "good", non_compliant: "bad", under_review: "warn" };
    body = `<div class="scroll"><table><tr><th>Ref</th><th>Requirement</th><th>Kind</th><th>Permit expiry</th><th>Next review</th><th>Status</th><th></th></tr>${L.map(l => `<tr><td>${esc(l.ref)}</td><td>${esc(l.title)}<div class="muted small">${esc(l.authority || "")} ${esc(l.obligation || "")}</div></td><td class="small">${l.kind}</td>
      <td class="small">${d10(l.permit_expiry)}${l.permit_soon ? ` <span class="chip bad">soon</span>` : ""}</td><td class="small">${d10(l.review_due)}${l.review_overdue ? ` <span class="chip bad">overdue</span>` : ""}</td>
      <td><span class="chip ${CL[l.status]}">${l.status.replace("_", " ")}</span><div class="muted small">${esc(l.evaluation_note || "")}</div></td><td>${w ? `<button class="sm" data-act="emsEval" data-id="${l.requirement_id}">Evaluate…</button>` : ""}</td></tr>`).join("") || `<tr><td colspan="7" class="muted">Nothing registered yet.</td></tr>`}</table></div>
    ${w ? `<h3>Add a requirement or permit</h3><form data-form="emslegal"><div class="form"><div class="field"><label>Reference</label><input name="ref" required></div><div class="field"><label>Title</label><input name="title" required></div>
      <div class="field"><label>Kind</label><select name="kind"><option>law</option><option>permit</option><option>standard</option><option>customer</option><option>other</option></select></div><div class="field"><label>Authority</label><input name="authority"></div>
      <div class="field"><label>Obligation</label><input name="obligation"></div><div class="field"><label>Permit expiry</label><input type="date" name="permit_expiry"></div><div class="field"><label>Next review</label><input type="date" name="review_due"></div></div><button class="primary">Add</button></form>` : ""}`;
  } else if (t === "nc") {
    const N = await api("/ems/nonconformities");
    body = `<div class="scroll"><table><tr><th>Ref</th><th>Issue</th><th>Severity</th><th>Status</th><th>Due</th><th>Actions</th><th></th></tr>${N.map(n => `<tr><td>${esc(n.nc_ref)}</td><td>${esc(n.description)}<div class="muted small">${n.source}${n.root_cause ? " · cause: " + esc(n.root_cause) : ""}${n.effectiveness_note ? " · check: " + esc(n.effectiveness_note) : ""}</div></td><td>${n.severity}</td><td>${n.status.replace("_", " ")}</td>
      <td class="small">${d10(n.due_date)}${n.overdue ? ` <span class="chip bad">overdue</span>` : ""}</td><td class="small">${n.actions_done}/${n.actions}</td>
      <td>${w ? (n.status === "open" ? `<button class="sm" data-act="ncCause" data-id="${n.nc_id}">Root cause…</button>` : n.status === "corrective_action" ? `<button class="sm" data-act="ncAction" data-id="${n.nc_id}">+ Action</button> <button class="sm" data-act="ncVerify" data-id="${n.nc_id}">To verification</button>` : n.status === "verification" ? `<button class="sm" data-act="ncClose" data-id="${n.nc_id}">Close…</button>` : "") : ""}</td></tr>`).join("") || `<tr><td colspan="7" class="muted">None raised.</td></tr>`}</table></div>
    <p class="muted small">Flow: raise → root cause → corrective actions → all done → verification → close with an effectiveness check. A major one must be closed by someone other than who raised it.</p>
    ${w ? `<h3>Raise a nonconformity</h3><form data-form="emsnc"><div class="form"><div class="field"><label>Reference</label><input name="nc_ref" required></div><div class="field"><label>Source</label><select name="source"><option>audit</option><option>incident</option><option>complaint</option><option>inspection</option><option>legal</option><option>other</option></select></div>
      <div class="field"><label>Standard</label><select name="standard"><option value="">–</option><option value="ISO14001">ISO 14001</option><option value="ISO50001">ISO 50001</option></select></div><div class="field"><label>Severity</label><select name="severity"><option>minor</option><option>major</option></select></div>
      <div class="field"><label>Description</label><input name="description" required></div><div class="field"><label>Due</label><input type="date" name="due_date"></div></div><button class="primary">Raise</button></form>` : ""}`;
  } else if (t === "enpi") {
    const [B, ents] = await Promise.all([api("/ems/enpi/baselines"), api("/lookup/entity")]);
    body = `<p class="muted small">Energy performance against a baseline: monthly consumption is modelled on an activity driver (TEU moved or vessel calls) over a baseline period; later months are compared with what the model expected for the same activity.</p>
    ${B.map(b => `<div class="card" style="margin:8px 0"><b>${esc(b.name)}</b> · ${esc(b.entity)} · driver ${b.driver} · ${d10(b.baseline_start)} to ${d10(b.baseline_end)} · ${b.n_months} months · fit R² ${b.r2 == null ? "n/a" : Number(b.r2).toFixed(2)}${b.r2 != null && b.r2 < 0.5 ? ` <span class="chip warn">weak fit</span>` : ""}
      <div id="enpi-${b.baseline_id}"><button class="sm" data-act="enpiShow" data-id="${b.baseline_id}">Show performance this year</button></div></div>`).join("") || `<p class="muted">No baselines yet. Needs at least 6 months of meter data.</p>`}
    ${w ? `<h3>Create a baseline</h3><form data-form="emsenpi"><div class="form"><div class="field"><label>Name</label><input name="name" required></div><div class="field"><label>Entity</label><select name="entity_id">${ents.map(e => `<option value="${e.id}">${esc(e.label)}</option>`).join("")}</select></div>
      <div class="field"><label>Driver</label><select name="driver"><option value="teu">TEU moved</option><option value="vessel_calls">Vessel calls</option><option value="none">None (flat)</option></select></div>
      <div class="field"><label>Baseline from</label><input type="date" name="s" required></div><div class="field"><label>to</label><input type="date" name="e" required></div></div><button class="primary">Create baseline</button></form>` : ""}`;
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

Object.assign(ACT, {
  emsEval: el => { const st = prompt("Status: compliant, non_compliant or under_review"); if (!st) return; const note = prompt("Note (required if non_compliant):") || null; const nr = prompt("Next review date (YYYY-MM-DD, optional):") || null;
    guard(async () => { await api(`/ems/legal/${el.dataset.id}/evaluate`, { method: "POST", json: { status: st, note, next_review: nr } }); route(); }); },
  ncCause: el => { const c = prompt("Root cause:"); if (c) guard(async () => { await api(`/ems/nonconformities/${el.dataset.id}/root-cause`, { method: "POST", json: { root_cause: c } }); route(); }); },
  ncAction: el => { const title = prompt("Corrective action:"); if (!title) return; const due = prompt("Due (YYYY-MM-DD, optional):") || null; const owner = prompt("Owner (optional):") || null;
    guard(async () => { await api(`/ems/nonconformities/${el.dataset.id}/actions`, { method: "POST", json: { title, due_date: due, owner } }); route(); }); },
  ncVerify: el => guard(async () => { await api(`/ems/nonconformities/${el.dataset.id}/verify`, { method: "POST" }); route(); }),
  ncClose: el => { const n = prompt("How did you check the fix worked?"); if (n) guard(async () => { await api(`/ems/nonconformities/${el.dataset.id}/close`, { method: "POST", json: { effectiveness_note: n } }); route(); }); },
  enpiShow: el => guard(async () => { const y = new Date().getFullYear(), p = await api(`/ems/enpi/baselines/${el.dataset.id}/performance?start=${y}-01-01&end=${new Date().toISOString().slice(0, 10)}`);
    $("#enpi-" + el.dataset.id).innerHTML = `<p class="small">Actual ${fmt(p.actual_kwh)} kWh vs expected ${fmt(p.expected_kwh)} kWh: <b>${p.saving_pct == null ? "n/a" : p.saving_pct + "%"}</b> ${p.saving_kwh >= 0 ? "below" : "above"} expectation. <span class="muted">${esc(p.note)}</span></p>
      <table><tr><th>Month</th><th class="num">Actual</th><th class="num">Expected</th><th class="num">Saving</th><th class="num">Cumulative</th></tr>${p.months.map(m => `<tr><td>${d10(m.month).slice(0, 7)}</td><td class="num">${fmt(m.actual_kwh)}</td><td class="num">${fmt(m.expected_kwh)}</td><td class="num">${fmt(m.saving_kwh)}</td><td class="num">${fmt(m.cumulative_saving_kwh)}</td></tr>`).join("")}</table>`; }),
});
Object.assign(FORMS, {
  emslegal: f => guard(async () => { await api("/ems/legal", { method: "POST", json: { ref: f.ref.value, title: f.title.value, kind: f.kind.value, authority: f.authority.value || null, obligation: f.obligation.value || null, permit_expiry: f.permit_expiry.value || null, review_due: f.review_due.value || null } }); route(); }),
  emsnc: f => guard(async () => { await api("/ems/nonconformities", { method: "POST", json: { nc_ref: f.nc_ref.value, source: f.source.value, standard: f.standard.value || null, severity: f.severity.value, description: f.description.value, due_date: f.due_date.value || null } }); route(); }),
  emsenpi: f => guard(async () => { await api("/ems/enpi/baselines", { method: "POST", json: { name: f.name.value, entity_id: f.entity_id.value, driver: f.driver.value, baseline_start: f.s.value, baseline_end: f.e.value } }); toast("Baseline created"); route(); }),
});
