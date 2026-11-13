"use strict";
const $ = s => document.querySelector(s), esc = v => String(v ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const token = location.hash.slice(1); history.replaceState(null, "", location.pathname);   // keep the token out of history and referrers
const H = { "X-Collect-Token": token };
const dead = `<div class="card"><h1>Link not valid</h1><p class="muted">This link has expired, was already used, or was withdrawn. Please ask the port for a new one.</p></div>`;
(async () => {
  const r = await fetch("/api/collect", { headers: H });
  if (!r.ok) return $("#c").innerHTML = dead;
  const d = await r.json();
  $("#c").innerHTML = `<h1>${esc(d.tenant)}: ESG data request</h1><p class="muted">Period ${esc(d.period_start)} to ${esc(d.period_end)}. ${esc(d.message || "")}</p>
   <form id="f" class="card">${d.metrics.map((m, i) => `<div class="field"><label>${esc(m.name)} <span class="muted small">(${esc(m.unit || "")})</span></label>
     <input type="number" step="any" name="v${i}" value="" placeholder="${m.prefill ? "last time: " + esc(m.prefill.value) : ""}">
     ${m.prefill ? `<button type="button" class="sm" data-i="${i}" data-v="${esc(m.prefill.value)}">Same as last time</button>` : ""}</div>`).join("")}
   <div class="field"><label>Supporting file (bill, report) – optional</label><input type="file" name="file"></div><div id="msg"></div><button class="primary">Submit</button>
   <p class="muted small">Submissions are reviewed by the port before they are used.</p></form>`;
  document.querySelectorAll("button[data-i]").forEach(b => b.onclick = () => { document.querySelector(`[name=v${b.dataset.i}]`).value = b.dataset.v; });
  $("#f").onsubmit = async e => {
    e.preventDefault();
    const entries = d.metrics.map((m, i) => ({ metric_id: m.metric_id, value: Number(e.target["v" + i].value), raw: e.target["v" + i].value, uom: m.unit })).filter(x => x.raw !== "").map(({ raw, ...x }) => x);
    if (!entries.length) return $("#msg").innerHTML = `<p class="err">Enter at least one value.</p>`;
    const fd = new FormData(); fd.append("entries", JSON.stringify(entries)); const f = e.target.file.files[0]; if (f) fd.append("file", f);
    const res = await fetch("/api/collect", { method: "POST", headers: H, body: fd });
    if (res.ok) return $("#c").innerHTML = `<div class="card"><h1>Thank you</h1><p>Your data was received and is awaiting review.</p></div>`;
    if (res.status === 404) return $("#c").innerHTML = dead;
    let m = "Could not submit"; try { m = (await res.json()).detail; } catch (_) {} $("#msg").innerHTML = `<p style="color:var(--bad,#b00)">${esc(typeof m === "string" ? m : JSON.stringify(m))}</p>`;
  };
})();
