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
  const [locks, runs, factors, links, tenants, metrics] = await Promise.all([api("/periods"), api("/runs"), api("/periods/factors"), api("/collect-links"), api("/lookup/tenant"), api("/collect-links/metrics")]);
  const ok = runs.filter(r => r.status === "succeeded");
  $("#adm-extra").innerHTML = `<div class="card" style="margin-bottom:14px"><h2>Reporting period locks</h2>
    <p class="muted small">A locked period freezes its data, its calculation and the emission factors behind it. Unlocking needs a reason and is audited.</p>
    <table><tr><th>Label</th><th>Period</th><th>Locked</th><th>Status</th><th></th></tr>${locks.map(l => `<tr><td>${esc(l.label)}</td><td class="small">${d10(l.period_start)} to ${d10(l.period_end)}</td><td class="small">${esc(l.locked_by)} · ${dt16(l.locked_at)}<br>${esc(l.reason || "")}</td>
      <td>${l.active ? `<span class="chip bad">🔒 Locked</span>` : `<span class="muted small">Unlocked ${dt16(l.unlocked_at)}<br>${esc(l.unlock_reason || "")}</span>`}</td><td>${l.active ? `<button class="sm" data-act="unlockPeriod" data-id="${l.lock_id}">Unlock…</button>` : ""}</td></tr>`).join("") || `<tr><td colspan="5" class="muted">No locks yet.</td></tr>`}</table>
    <form data-form="lock" class="row" style="margin-top:8px"><select name="run_id" required>${ok.filter(r => !r.locked).map(r => `<option value="${r.run_id}">${d10(r.period_start)} to ${d10(r.period_end)} · ${String(r.run_id).slice(0, 8)}</option>`).join("")}</select>
    <input name="label" placeholder="Label, e.g. FY2025 annual report" required><input name="reason" placeholder="Reason" required><button class="primary">Lock period</button></form></div>
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
  revokeLink: el => guard(async () => { await api(`/collect-links/${el.dataset.id}/revoke`, { method: "POST" }); route(); }),
});
Object.assign(FORMS, {
  lock: f => guard(async () => { await api("/periods/lock", { method: "POST", json: { run_id: f.run_id.value, label: f.label.value, reason: f.reason.value } }); toast("Period locked"); route(); }),
  newlink: f => guard(async () => { const ids = [...f.querySelectorAll("[name=m]:checked")].map(x => x.value); if (!ids.length) return toast("Choose at least one metric", true);
    const r = await api("/collect-links", { method: "POST", json: { tenant_id: f.tenant_id.value, metric_ids: ids, period_start: f.s.value, period_end: f.e.value, message: f.message.value || null } });
    const url = location.origin + r.path; $("#link-out").innerHTML = `<div class="card" style="margin-top:8px;background:var(--good-bg)"><b>Link created.</b> Copy it now; it is shown only once.<br><input readonly style="width:100%" value="${esc(url)}" onclick="this.select()"></div>`; }),
});
