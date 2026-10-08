/* 30-day cash calendar on the Net worth page. */
"use strict";

const CC = { data: null };
const ccStore = {
  get(k, d) { try { const v = localStorage.getItem("cc-" + k); return v === null ? d : v; } catch { return d; } },
  set(k, v) { try { localStorage.setItem("cc-" + k, v); } catch { /* ignore */ } },
};
$("#cc-buffer").value = ccStore.get("buffer", "1000");

async function loadCashCalendar() {
  CC.data = await api("/api/cash-calendar");
  renderCashCalendar();
}

function renderCashCalendar() {
  const c = CC.data; if (!c) return;
  const whatif = +$("#cc-whatif").value || 0;
  const buffer = +$("#cc-buffer").value || 0;
  const fmt = (v) => money(Math.round(v) || 0, 0);
  // Re-run the balance with the what-if move applied today
  let bal = c.start_balance - whatif;
  const rows = c.events.map((e) => { bal -= e.amount; return { ...e, bal }; });
  const all = [{ date: c.events[0]?.date || "", bal: c.start_balance - whatif }, ...rows];
  const low = all.reduce((m, r) => (r.bal < m.bal ? r : m), { bal: c.start_balance - whatif, date: new Date().toISOString().slice(0, 10) });
  const below = rows.filter((r) => r.bal < buffer);

  const alerts = [];
  if (low.bal < 0) alerts.push(`<div class="alert bad">Checking would go <b>below $0</b> (${fmt(low.bal)}) around ${esc(low.date)}.</div>`);
  else if (below.length) alerts.push(`<div class="alert warn">Checking would dip under your ${fmt(buffer)} minimum: lowest ${fmt(low.bal)} on ${esc(low.date)}.</div>`);
  if (c.rent_suggestion) {
    const rs = c.rent_suggestion;
    alerts.push(`<div class="alert info">No rent payment is expected in the next 30 days. Your last rent month had ${rs.payments.map((p) => `${money(p.amount)} on ${esc(p.date)}`).join(" and ")}.
      Add your rent under <b>Planned payments</b> below so it's counted. <button class="btn small" id="cc-rent-fill">Fill in ${money(rs.amount)} on the ${rs.day}th</button></div>`);
  }
  $("#cc-alerts").innerHTML = alerts.join("");
  $("#cc-rent-fill")?.addEventListener("click", () => {
    const rs = c.rent_suggestion; const t = new Date();
    let d = new Date(t.getFullYear(), t.getMonth(), rs.day); if (d < t) d = new Date(t.getFullYear(), t.getMonth() + 1, rs.day);
    $("#cc-p-name").value = "Rent"; $("#cc-p-amount").value = rs.amount; $("#cc-p-dir").value = "out";
    $("#cc-p-date").value = d.toISOString().slice(0, 10); $("#cc-p-repeat").checked = true;
    $("#cc-card details").open = true; $("#cc-p-amount").focus();
    toast("Check the amount and date, then click Add");
  });

  $("#cc-kpis").innerHTML = [
    ["Today", fmt(c.start_balance - whatif), whatif ? `after moving ${fmt(whatif)}` : esc(c.start_note)],
    ["Lowest point", `<span class="${low.bal < 0 ? "neg" : low.bal < buffer ? "" : "pos"}">${fmt(low.bal)}</span>`, esc(low.date)],
    [`In ${c.days} days`, fmt(rows.length ? rows[rows.length - 1].bal : c.start_balance - whatif), ""],
  ].map(([l, v, n]) => `<div class="kpi"><div class="label">${l}</div><div class="value">${v}</div><div class="note">${n}</div></div>`).join("");

  const today = new Date().toISOString().slice(0, 10);
  const xs = [today, ...rows.map((r) => r.date)], ys = [c.start_balance - whatif, ...rows.map((r) => r.bal)];
  plot("cc-chart", [
    { x: xs, y: ys, type: "scatter", mode: "lines+markers", line: { shape: "hv", width: 2.5, color: cssVar("--accent") }, marker: { size: 5 },
      name: "Checking", hovertemplate: "%{x}: %{y:$,.0f}<extra></extra>" },
    { x: [xs[0], xs[xs.length - 1]], y: [buffer, buffer], type: "scatter", mode: "lines", line: { dash: "dot", width: 1.5, color: cssVar("--warn") },
      name: "Your minimum", hovertemplate: "Minimum: %{y:$,.0f}<extra></extra>" },
  ], { showlegend: false, margin: { l: 55, r: 10, t: 10, b: 35 }, xaxis: { type: "date", tickformat: "%b %d", gridcolor: cssVar("--border") },
    yaxis: { tickprefix: "$", tickformat: ",.0f", gridcolor: cssVar("--border"), rangemode: "tozero" } });

  $("#cc-table").innerHTML = `<table><thead><tr><th>Date</th><th>What</th><th class="num">Amount</th><th class="num">Checking after</th></tr></thead><tbody>
    ${rows.map((r) => `<tr style="${r.bal < buffer ? "background:var(--warn-soft)" : ""}"><td style="white-space:nowrap">${esc(r.date.slice(5))}</td>
      <td>${esc(r.name)} ${r.estimate ? '<span class="badge">estimate</span>' : ""}<div class="small muted">${esc(r.note)}</div></td>
      <td class="num ${r.amount < 0 ? "pos" : ""}">${r.amount < 0 ? "+" + money(-r.amount) : money(-r.amount)}</td>
      <td class="num"><b class="${r.bal < 0 ? "neg" : ""}">${fmt(r.bal)}</b></td></tr>`).join("")}</tbody></table>`;

  $("#cc-planned").innerHTML = c.planned.length ? `<table>${c.planned.map((p) => `<tr><td>${esc(p.name)}</td><td class="num">${p.amount < 0 ? "+" + money(-p.amount) : money(p.amount)}</td>
    <td class="small">${p.repeat_monthly ? `monthly from ${esc(p.first_date)}` : esc(p.first_date)}</td>
    <td><button class="btn small danger" data-del-planned="${p.id}">Remove</button></td></tr>`).join("")}</table>` : '<p class="small muted">Nothing planned yet.</p>';
  $$("[data-del-planned]").forEach((b) => b.onclick = async () => { await api(`/api/planned/${b.dataset.delPlanned}`, { method: "DELETE" }); loadCashCalendar(); });
}

$("#cc-whatif").oninput = renderCashCalendar;
$("#cc-buffer").oninput = () => { ccStore.set("buffer", $("#cc-buffer").value); renderCashCalendar(); };
$("#cc-p-add").onclick = async () => {
  try {
    const amt = +$("#cc-p-amount").value;
    if (!amt || !$("#cc-p-date").value) return toast("Enter an amount and a date");
    await api("/api/planned", { method: "POST", body: { name: $("#cc-p-name").value || "Planned payment",
      amount: $("#cc-p-dir").value === "in" ? -Math.abs(amt) : Math.abs(amt), first_date: $("#cc-p-date").value,
      repeat_monthly: $("#cc-p-repeat").checked } });
    $("#cc-p-name").value = ""; $("#cc-p-amount").value = "";
    toast("Added"); loadCashCalendar();
  } catch (e) { showError(e); }
};

// Load alongside the Net worth page
const _ovLoader = loaders.overview;
loaders.overview = async () => { await Promise.all([_ovLoader(), loadCashCalendar()]); };
