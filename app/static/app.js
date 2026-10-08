/* Finance Dashboard frontend. Talks only to this local server (and Plaid Link). */
"use strict";

// ---------------- helpers ----------------
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const money = (v, dec = 2) => v == null || isNaN(v) ? "—" :
  Number(v).toLocaleString("en-US", { style: "currency", currency: "USD", minimumFractionDigits: dec, maximumFractionDigits: dec });
const money0 = (v) => money(v, 0);
const pct = (v) => v == null ? "—" : `${Number(v).toFixed(1)}%`;
const cssVar = (n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim();

async function api(path, opts = {}) {
  const init = { method: opts.method || "GET", headers: { "X-Local-App": "1" } };
  if (opts.body instanceof FormData) init.body = opts.body;
  else if (opts.body !== undefined) { init.headers["Content-Type"] = "application/json"; init.body = JSON.stringify(opts.body); }
  const r = await fetch(path, init);
  let data = null;
  try { data = await r.json(); } catch { /* empty */ }
  if (!r.ok) {
    const msg = (data && (data.detail || data.message)) || `Request failed (${r.status})`;
    const err = new Error(typeof msg === "string" ? msg : JSON.stringify(msg));
    err.data = data;
    throw err;
  }
  return data;
}

function toast(msg, ms = 3500) {
  const t = $("#toast"); t.textContent = msg; t.classList.add("show");
  clearTimeout(toast._t); toast._t = setTimeout(() => t.classList.remove("show"), ms);
}
function showError(e) { console.error(e); toast("⚠ " + (e.message || e), 7000); }

const dlg = $("#dlg");
function openDialog(title, bodyHtml, buttons = []) {
  $("#dlg-title").textContent = title;
  $("#dlg-body").innerHTML = bodyHtml;
  const foot = $("#dlg-foot"); foot.innerHTML = "";
  for (const b of buttons) {
    const el = document.createElement("button");
    el.className = "btn " + (b.cls || ""); el.textContent = b.label;
    el.onclick = async () => { try { if ((await b.onClick?.()) !== false) dlg.close(); } catch (e) { showError(e); } };
    foot.appendChild(el);
  }
  foot.hidden = buttons.length === 0;
  if (!dlg.open) dlg.showModal();
}
$("#dlg-close").onclick = () => dlg.close();

function chartLayout(extra = {}) {
  return Object.assign({
    paper_bgcolor: "rgba(0,0,0,0)", plot_bgcolor: "rgba(0,0,0,0)",
    font: { color: cssVar("--text"), family: "Segoe UI, Roboto, sans-serif", size: 12 },
    margin: { l: 50, r: 16, t: 10, b: 40 },
    xaxis: { gridcolor: cssVar("--border"), zerolinecolor: cssVar("--border") },
    yaxis: { gridcolor: cssVar("--border"), zerolinecolor: cssVar("--border"), tickprefix: "$", separatethousands: true },
    legend: { orientation: "h", y: -0.18 },
    // Solid, high-contrast hover box (Plotly otherwise copies our transparent background into it).
    hoverlabel: { bgcolor: "#ffffff", bordercolor: "#8a94a3", font: { color: "#111111", size: 13 }, namelength: -1 },
  }, extra);
}
const plotCfg = { displayModeBar: false, responsive: true };
function plot(id, data, layout) {
  const el = document.getElementById(id);
  if (!data.length || data.every((d) => !(d.values || d.y || d.link?.value || []).length)) {
    Plotly.purge(el); el.innerHTML = '<div class="empty">No data yet</div>'; return;
  }
  // Only clear our own "No data yet" placeholder; wiping Plotly's DOM breaks later redraws.
  if (el.querySelector(".empty")) el.innerHTML = "";
  Plotly.react(el, data, chartLayout(layout), plotCfg);
}

// ---------------- state ----------------
const S = { status: null, categories: [], accounts: [], periods: null, catColors: {} };

async function loadBasics() {
  [S.status, S.categories, S.accounts, S.periods] = await Promise.all([
    api("/api/status"), api("/api/categories"), api("/api/accounts/all"), api("/api/periods")]);
  S.catColors = Object.fromEntries(S.categories.map((c) => [c.name, c.color]));
  const env = S.status.env;
  const b = $("#env-badge"); b.textContent = env === "production" ? "PRODUCTION" : "SANDBOX (test data)";
  b.className = "env " + env;
  $("#last-sync").textContent = S.status.last_sync ? "Synced " + S.status.last_sync.replace("T", " ").slice(0, 16) : "";
  fillPeriodSelects();
  fillCategorySelects();
  fillAccountSelects();
  renderGlobalAlerts();
}

function renderGlobalAlerts() {
  const out = [];
  for (const it of S.status.items) {
    if (it.status === "login_required" || it.status === "error")
      out.push(`<div class="alert bad"><b>${esc(it.institution_name)}</b> needs attention: ${esc(it.error_message || it.error_code)}.
        <a href="#connections">Fix connection</a></div>`);
    else if (it.consent_expiring_soon)
      out.push(`<div class="alert warn"><b>${esc(it.institution_name)}</b> access expires ${esc((it.consent_expiration || "").slice(0, 10))}.
        <a href="#connections">Renew it</a> (no new Item is created).</div>`);
  }
  if (S.status.paystubs_needing_review)
    out.push(`<div class="alert warn">${S.status.paystubs_needing_review} pay stub(s) need your review. <a href="#income">Review</a></div>`);
  $("#global-alerts").innerHTML = out.join("");
}

function periodOptions(includeAll = false) {
  const p = S.periods || { months: [], years: [] };
  const now = new Date().getFullYear();
  let html = includeAll ? '<option value="">All time</option>' : "";
  html += `<option value="ytd">${now} year-to-date</option><option value="last12">Last 12 months</option>`;
  html += '<optgroup label="Month">' + p.months.map((m) => {
    const [y, mo] = m.split("-"); const d = new Date(+y, +mo - 1, 1);
    return `<option value="${m}">${d.toLocaleString("en-US", { month: "long", year: "numeric" })}</option>`;
  }).join("") + "</optgroup>";
  html += '<optgroup label="Year">' + p.years.filter((y) => y !== now).map((y) => `<option value="year:${y}">${y}</option>`).join("") + "</optgroup>";
  return html;
}
function fillPeriodSelects() {
  for (const id of ["cf-period", "sp-period", "tx-period"]) {
    const el = document.getElementById(id); const v = el.value;
    el.innerHTML = periodOptions(id === "tx-period");
    if (v !== undefined && [...el.options].some((o) => o.value === v)) el.value = v;
    else if (id === "tx-period") el.value = "last12";
  }
}
function catOptionsHtml(selected, withAuto = false) {
  const kinds = { spending: "Spending", income: "Income", investing: "Investing", transfer: "Excluded (not spending/income)" };
  let html = withAuto ? '<option value="">— automatic —</option>' : "";
  for (const [k, label] of Object.entries(kinds)) {
    html += `<optgroup label="${label}">` + S.categories.filter((c) => c.kind === k)
      .map((c) => `<option ${c.name === selected ? "selected" : ""}>${esc(c.name)}</option>`).join("") + "</optgroup>";
  }
  return html;
}
function fillCategorySelects() {
  $("#tx-category").innerHTML = '<option value="">All categories</option>' + catOptionsHtml();
  for (const el of $$(".cat-options")) el.innerHTML = catOptionsHtml();
}
function fillAccountSelects() {
  const opts = S.accounts.map((a) => `<option value="${esc(a.account_id)}">${esc(a.label)} (${esc(a.institution_name || "")})</option>`).join("");
  $("#tx-account").innerHTML = '<option value="">All accounts</option>' + opts;
  for (const el of $$(".acct-options")) el.innerHTML = '<option value="">Any</option>' + opts;
}

// ---------------- navigation ----------------
const loaders = {};
function show(view) {
  if (!document.getElementById("view-" + view)) view = "overview";
  $$(".view").forEach((v) => v.classList.toggle("active", v.id === "view-" + view));
  $$("#nav button").forEach((b) => b.classList.toggle("active", b.dataset.view === view));
  loaders[view]?.().catch(showError);
}
$("#nav").addEventListener("click", (e) => { const v = e.target.dataset?.view; if (v) location.hash = v; });
window.addEventListener("hashchange", () => show(location.hash.slice(1)));

// ---------------- Net worth ----------------
loaders.overview = async () => {
  const [a, hist, monthly] = await Promise.all([api("/api/accounts"), api("/api/networth/history"), api("/api/monthly")]);
  $("#nw-kpis").innerHTML = `
    <div class="kpi"><div class="label">Net worth</div><div class="value">${money(a.net_worth)}</div></div>
    <div class="kpi"><div class="label">Assets</div><div class="value pos">${money(a.assets)}</div></div>
    <div class="kpi"><div class="label">Debts</div><div class="value neg">${money(a.liabilities)}</div></div>`;
  if (!a.groups.length) {
    $("#nw-accounts").innerHTML = `<div class="empty">No accounts yet. Go to <a href="#connections">Accounts &amp; data</a> to add one.</div>`;
  } else {
    let html = '<table><thead><tr><th>Account</th><th>Details</th><th class="num">Balance</th></tr></thead><tbody>';
    for (const g of a.groups) {
      html += `<tr class="group-head"><td colspan="2">${esc(g.name)}</td><td class="num">${money(g.total)}</td></tr>`;
      for (const acc of g.accounts) {
        let det = `<span class="muted small">${esc(acc.institution_name || "")} · ${esc(acc.subtype || acc.type)}</span>`;
        if (acc.type === "credit") {
          const l = acc.liability || {};
          det += `<div class="small">Statement balance <b>${money(l.last_statement_balance)}</b>` +
            ` · Due <b>${esc(l.next_due_date || "—")}</b>` +
            (l.minimum_payment != null ? ` · Min ${money(l.minimum_payment)}` : "") +
            (acc.credit_limit ? ` · Limit ${money0(acc.credit_limit)}` : "") +
            (l.is_overdue ? ' <span class="badge bad">overdue</span>' : "") + "</div>";
        }
        if (acc.source === "manual") det += ' <span class="badge">manual</span>';
        if (acc.item && acc.item.status !== "ok") det += ' <span class="badge bad">needs re-login</span>';
        if (!acc.include_in_networth) det += ' <span class="badge">not in net worth</span>';
        const bal = acc.type === "credit" ? `<span class="neg">${money(-Math.abs(acc.current_balance || 0))}</span><div class="small muted">current balance</div>` : money(acc.current_balance);
        html += `<tr><td>${esc(acc.label)}</td><td>${det}</td><td class="num">${bal}</td></tr>`;
      }
    }
    $("#nw-accounts").innerHTML = html + "</tbody></table>";
  }
  plot("nw-chart", hist.length ? [
    { x: hist.map((h) => h.date), y: hist.map((h) => h.net_worth), name: "Net worth", type: "scatter", mode: hist.length > 1 ? "lines" : "markers",
      line: { color: cssVar("--accent"), width: 2.5 }, marker: { size: 8 } },
  ] : [], { showlegend: false, xaxis: { type: "date", tickformat: "%b %d", gridcolor: cssVar("--border") },
    yaxis: { tickprefix: "$", tickformat: ",.0f", gridcolor: cssVar("--border") } });
  const m = monthly.months;
  plot("monthly-chart", m.length ? [
    { x: m.map((r) => r.month), y: m.map((r) => r.paychecks + r.other_income), name: "Income (take-home)", type: "bar", marker: { color: "#2e7d32" },
      text: m.map((r) => r.paycheck_count === 3 ? "3 paychecks" : ""), textposition: "outside" },
    { x: m.map((r) => r.month), y: m.map((r) => r.spending), name: "Spending", type: "bar", marker: { color: "#ef6c00" } },
    { x: m.map((r) => r.month), y: m.map((r) => r.investing), name: "Invested (brokerage)", type: "bar", marker: { color: "#1565c0" } },
  ] : [], { barmode: "group" });
  $("#monthly-note").textContent = monthly.typical_paycheck
    ? `Typical paycheck ${money(monthly.typical_paycheck)}. Paid every 2 weeks, that's 26 a year, about ${money(monthly.avg_monthly_from_typical)} per month on average. Months marked "3 paychecks" will look higher.`
    : "";
};

// ---------------- Cash flow ----------------
loaders.cashflow = async () => {
  const period = $("#cf-period").value || "ytd";
  const c = await api("/api/cashflow?period=" + encodeURIComponent(period));
  const notes = c.payroll.notes.map((n) => `<div class="alert info small">${esc(n)}</div>`);
  if (!c.gross) notes.unshift(`<div class="alert warn small">No pay stub data for ${esc(c.label)}, so gross pay, taxes and 401(k) are unknown. The diagram starts at take-home pay.</div>`);
  $("#cf-notes").innerHTML = notes.join("");
  $("#cf-kpis").innerHTML = [
    ["Gross pay", money(c.gross), `${c.payroll.stubs_used} stub(s)${c.payroll.estimated ? ` + ${c.payroll.estimated} estimated` : ""}`],
    ["Taxes", money(c.tax_total), c.gross ? pct(100 * c.tax_total / c.gross) + " of gross" : ""],
    ["401(k)", money(c.k401), "from pay stubs"],
    ["Take-home", money(c.take_home), `${c.payroll.paycheck_count} paycheck deposit(s)`],
    ["Spending", money(c.spending_total), c.take_home ? pct(100 * c.spending_total / c.take_home) + " of take-home" : ""],
    ["Invested (brokerage)", money(c.invested_brokerage), "net money moved to your brokerage accounts"],
    ["Left over", `<span class="${c.leftover >= 0 ? "pos" : "neg"}">${money(c.leftover)}</span>`, "take-home + other income − spending − investing"],
  ].map(([l, v, n]) => `<div class="kpi"><div class="label">${l}</div><div class="value">${v}</div><div class="note">${n}</div></div>`).join("");

  const sk = c.sankey;
  const nodeColor = (name) => S.catColors[name] || ({
    "Gross pay": "#455a64", "Taxes": "#c62828", "Federal tax": "#e53935", "State tax": "#ef5350", "Local tax": "#e57373",
    "Social Security": "#f06292", "Medicare": "#ec407a", "Other taxes": "#f48fb1", "401(k)": "#1565c0", "ESPP / ASPP": "#1e88e5",
    "HSA": "#42a5f5", "Benefits & other deductions": "#90a4ae", "Take-home pay": "#2e7d32", "Money available": "#388e3c",
    "Other income": "#66bb6a", "Paychecks (no pay stub)": "#81c784", "Invested (brokerage)": "#0d47a1", "Left over": "#00897b",
    "From savings": "#b26a00", "Withdrawn from investments": "#5c6bc0", "Other spending": "#9e9e9e",
  }[name] || "#78909c");
  const total = (i) => Math.max(sk.links.filter((l) => l.target === i).reduce((a, l) => a + l.value, 0),
    sk.links.filter((l) => l.source === i).reduce((a, l) => a + l.value, 0));
  // Fixed columns: sources | taxes, deductions, take-home | money available | where it went
  // Sources (nodes nothing flows into) go in the first column.
  const hasIncoming = new Set(sk.links.map((l) => l.target));
  const col1 = ["Taxes", "401(k)", "ESPP / ASPP", "HSA", "Benefits & other deductions", "Take-home pay"];
  const colOf = (n) => !hasIncoming.has(sk.nodes.indexOf(n)) ? 0 : col1.includes(n) ? 1 : n === "Money available" ? 2 : 3;
  const xs = [0.001, 0.3, 0.6, 0.999];
  const pos = sk.nodes.map(() => [0, 0]);
  for (let c = 0; c < 4; c++) {
    const idx = sk.nodes.map((n, i) => i).filter((i) => colOf(sk.nodes[i]) === c);
    const sum = idx.reduce((a, i) => a + total(i), 0) || 1;
    const gap = idx.length > 1 ? Math.min(0.03, 0.3 / idx.length) : 0;
    const scale = 1 - gap * (idx.length - 1) - 0.02;
    let y = 0.01;
    for (const i of idx) { const h = scale * total(i) / sum; pos[i] = [xs[c], Math.min(0.99, y + h / 2)]; y += h + gap; }
  }
  plot("cf-sankey", sk.links.length ? [{
    type: "sankey", arrangement: "snap",
    node: { label: sk.nodes.map((n, i) => `${n}  ${money0(total(i))}`), color: sk.nodes.map(nodeColor), pad: 12, thickness: 16,
      x: pos.map((p) => p[0]), y: pos.map((p) => p[1]), line: { width: 0 }, hovertemplate: "%{label}<extra></extra>" },
    link: { source: sk.links.map((l) => l.source), target: sk.links.map((l) => l.target), value: sk.links.map((l) => l.value),
      color: sk.links.map((l) => nodeColor(sk.nodes[l.target]) + "55"), hovertemplate: "%{source.label} → %{target.label}: %{value:$,.2f}<extra></extra>" },
  }] : [], { margin: { l: 8, r: 8, t: 8, b: 8 } });

  const inv = c.investing;
  $("#cf-invest").innerHTML = `
    <div class="kpis" style="margin-bottom:10px">
      <div class="kpi"><div class="label">% of gross pay</div><div class="value">${pct(inv.pct_gross)}</div></div>
      <div class="kpi"><div class="label">% of take-home</div><div class="value">${pct(inv.pct_take_home)}</div></div>
      <div class="kpi"><div class="label">Total invested</div><div class="value">${money(inv.total)}</div></div>
    </div>
    <table>${Object.entries(inv.parts).map(([k, v]) => `<tr><td>${esc(k)}</td><td class="num">${money(v)}</td></tr>`).join("")}</table>
    <p class="small muted">Your 401(k) comes out before your paycheck reaches the bank, so it's taken from pay stubs.
    ${inv.includes.employer_match ? "" : `Employer 401(k) match (${money(c.employer_401k)}) is shown but not counted.`}
    ${inv.includes.hsa ? "" : `HSA (${money(c.hsa)}) not counted.`} Change this in <a href="#connections">Settings</a>.</p>`;
  $("#cf-spend-pct").innerHTML = c.spending.length ? `<table><thead><tr><th>Category</th><th class="num">Amount</th><th class="num">% take-home</th><th></th></tr></thead><tbody>` +
    c.spending.map((s) => `<tr class="click" data-cat="${esc(s.category)}"><td><span class="dot" style="background:${S.catColors[s.category] || "#999"}"></span>${esc(s.category)}</td>
      <td class="num">${money(s.amount)}</td><td class="num">${pct(s.pct_take_home)}</td>
      <td style="width:30%"><div class="bar"><span style="width:${Math.min(100, s.pct_take_home || 0)}%;background:${S.catColors[s.category] || "#999"}"></span></div></td></tr>`).join("") +
    `</tbody></table>` : '<div class="empty">No spending in this period</div>';
  $$("#cf-spend-pct tr.click").forEach((tr) => tr.onclick = () => showCategory(tr.dataset.cat, period));

  const pf = c.payroll.fields;
  const rowsP = [["Gross pay", pf.gross], ["Federal tax", pf.federal_tax], ["State tax", pf.state_tax], ["Local tax", pf.local_tax],
    ["Social Security", pf.social_security], ["Medicare", pf.medicare], ["Other taxes", pf.other_taxes], ["401(k) pre-tax", pf.k401_pretax],
    ["401(k) Roth", pf.k401_roth], ["HSA", pf.hsa], ["ESPP / ASPP", pf.espp], ["Other pre-tax (benefits)", pf.other_pretax],
    ["Other after-tax", pf.other_posttax], ["Net pay (pay stubs)", pf.net_pay], ["Employer 401(k) (not in your pay)", pf.employer_401k]];
  $("#cf-payroll").innerHTML = `<table>${rowsP.filter(([, v]) => v).map(([k, v]) => `<tr><td>${k}</td><td class="num">${money(v)}</td><td class="num muted">${pf.gross ? pct(100 * v / pf.gross) : ""}</td></tr>`).join("") ||
    '<tr><td class="muted">No pay stub data for this period.</td></tr>'}</table>
    <p class="small muted">Paycheck deposits found in your bank for this period: ${money(c.payroll.bank_paycheck_total)}.</p>`;
};
$("#cf-period").onchange = () => loaders.cashflow().catch(showError);

// ---------------- Spending ----------------
loaders.spending = async () => {
  const period = $("#sp-period").value || "ytd";
  const [s, monthly] = await Promise.all([api("/api/spending?period=" + encodeURIComponent(period)), loadTrend()]);
  $("#sp-total").textContent = `Total ${money(s.total)} · ${s.label}`;
  $("#sp-table").innerHTML = s.categories.length ? `<table><thead><tr><th>Category</th><th class="num">Amount</th><th class="num">%</th><th class="num">#</th></tr></thead><tbody>` +
    s.categories.map((c) => `<tr class="click" data-cat="${esc(c.category)}"><td><span class="dot" style="background:${c.color || "#999"}"></span>${esc(c.category)}</td>
    <td class="num">${money(c.amount)}</td><td class="num">${pct(c.pct)}</td><td class="num muted">${c.count}</td></tr>`).join("") + "</tbody></table>"
    : '<div class="empty">No spending in this period</div>';
  $$("#sp-table tr.click").forEach((tr) => tr.onclick = () => showCategory(tr.dataset.cat, period));
  plot("sp-account-chart", s.categories.map((c) => ({
    type: "bar", orientation: "h", name: c.category, y: s.accounts, x: s.accounts.map((a) => c.by_account[a] || 0),
    marker: { color: c.color }, hovertemplate: `${c.category}: %{x:$,.2f}<extra></extra>`,
  })), { barmode: "stack", margin: { l: 170, r: 10, t: 10, b: 40 }, xaxis: { tickprefix: "$", gridcolor: cssVar("--border") }, yaxis: { automargin: true, tickprefix: "" }, showlegend: false });
  const cats = [...new Set(monthly.flatMap((m) => Object.keys(m.cats)))];
  plot("sp-trend", cats.map((c) => ({
    type: "bar", name: c, x: monthly.map((m) => m.month), y: monthly.map((m) => m.cats[c] || 0), marker: { color: S.catColors[c] },
    hovertemplate: `${c}: %{y:$,.2f}<extra></extra>`,
  })), { barmode: "stack" });
};
async function loadTrend() {
  const months = S.periods.months.slice(0, 12).reverse();
  const res = await Promise.all(months.map((m) => api("/api/spending?period=" + m)));
  return months.map((m, i) => ({ month: m, cats: Object.fromEntries(res[i].categories.filter((c) => c.amount > 0).map((c) => [c.category, c.amount])) }));
}
$("#sp-period").onchange = () => loaders.spending().catch(showError);

async function showCategory(cat, period) {
  const tx = await api(`/api/transactions?category=${encodeURIComponent(cat)}&period=${encodeURIComponent(period || "")}`);
  const total = tx.reduce((a, t) => a + t.amount, 0);
  openDialog(`${cat} · ${tx.length} transactions · ${money(total)}`, `<div class="table-wrap">${txTable(tx)}</div>`);
  wireTxTable($("#dlg-body"), () => showCategory(cat, period));
}

// ---------------- Transactions ----------------
function txTable(tx) {
  if (!tx.length) return '<div class="empty">No transactions</div>';
  return `<table><thead><tr><th>Date</th><th>Description</th><th>Account</th><th>Category</th><th class="num">Amount</th></tr></thead><tbody>` +
    tx.map((t) => {
      const desc = t.merchant_name || t.name;
      const flags = [
        t.pending ? '<span class="badge">pending</span>' : "",
        t.source === "csv" ? '<span class="badge">CSV</span>' : "",
        t.category_source === "user" ? '<span class="badge accent">set by you</span>' : "",
        t.category_source === "transfer_match" ? '<span class="badge good">matched transfer</span>' : "",
        t.category_source === "rule" ? '<span class="badge">rule</span>' : "",
        t.category_source === "plaid_low" || t.category_source === "default" ? '<span class="badge warn">review</span>' : "",
      ].join(" ");
      const amt = t.amount > 0 ? money(-t.amount) : `<span class="pos">+${money(-t.amount).replace("-", "")}</span>`;
      const venmo = /venmo/i.test((t.name || "") + (t.merchant_name || "")) && t.amount > 0 && t.category !== "Rent"
        ? ` <button class="btn small" data-rent="${esc(t.transaction_id)}">Rent?</button>` : "";
      return `<tr data-id="${esc(t.transaction_id)}"><td style="white-space:nowrap">${esc(t.date)}</td>
        <td><div class="txn-name" title="${esc(t.original_description || t.name)}">${esc(desc)}</div><div class="small muted">${t.merchant_name && t.name !== t.merchant_name ? esc(t.name) + " " : ""}${flags}${t.note ? " · " + esc(t.note) : ""}</div></td>
        <td class="small">${esc(t.account_label)}</td>
        <td><select class="cat-select" data-txn="${esc(t.transaction_id)}" title="${t.category_source === "user" ? "Set by you" : "Automatic"}">${catOptionsHtml(t.category, false)}${t.category_source === "user" ? '<option value="">↺ Back to automatic</option>' : ""}</select>${venmo}</td>
        <td class="num">${amt}</td></tr>`;
    }).join("") + "</tbody></table>";
}

function wireTxTable(root, refresh) {
  const byId = (id) => root.querySelector(`tr[data-id="${CSS.escape(id)}"]`);
  for (const sel of root.querySelectorAll("select.cat-select")) {
    const tr = sel.closest("tr");
    sel.onchange = () => categorizeDialog(sel.dataset.txn, sel.value, tr, refresh);
  }
  for (const b of root.querySelectorAll("[data-rent]")) {
    b.onclick = () => categorizeDialog(b.dataset.rent, "Rent", byId(b.dataset.rent), refresh, true);
  }
}

function categorizeDialog(txnId, category, tr, refresh, rentMode = false) {
  const desc = tr?.querySelector(".txn-name")?.textContent || "";
  if (!category) { // back to automatic
    api(`/api/transactions/${encodeURIComponent(txnId)}`, { method: "PATCH", body: { category: "" } })
      .then(() => { toast("Back to automatic category"); refresh?.(); }).catch(showError);
    return;
  }
  const amount = tr?.querySelector("td.num")?.textContent || "";
  const day = (tr?.querySelector("td")?.textContent || "").slice(8, 10);
  openDialog(`Set category: ${category}`, `
    <p>Transaction: <b>${esc(desc)}</b> ${esc(amount)}</p>
    <p>Apply to:</p>
    <label style="flex-direction:row;gap:8px;color:var(--text);font-size:14px"><input type="radio" name="apply" value="" ${rentMode ? "" : "checked"}> Just this transaction</label><br>
    <label style="flex-direction:row;gap:8px;color:var(--text);font-size:14px"><input type="radio" name="apply" value="merchant"> All transactions from this merchant (creates a rule)</label><br>
    <label style="flex-direction:row;gap:8px;color:var(--text);font-size:14px"><input type="radio" name="apply" value="similar" ${rentMode ? "checked" : ""}> This one, plus similar recurring payments going forward: same account, similar amount (±10%), around day ${esc(day)} of the month (creates a rule)</label>
    ${rentMode ? '<p class="small muted">Good for rent paid to a roommate on Venmo: other Venmo payments (dinners, etc.) won\'t match because their amount or timing differs.</p>' : ""}`,
  [{ label: "Cancel", onClick: () => refresh?.() },
    { label: "Save", cls: "primary", onClick: async () => {
      const apply = $('input[name="apply"]:checked', $("#dlg-body"))?.value || null;
      const r = await api(`/api/transactions/${encodeURIComponent(txnId)}`, { method: "PATCH", body: { category, apply_to: apply } });
      toast(r.rule_id ? `Saved and created a rule (${r.recategorized} transactions updated)` : "Saved");
      refresh?.();
    } }]);
}

loaders.transactions = async () => {
  const q = new URLSearchParams();
  const p = $("#tx-period").value; if (p) q.set("period", p);
  if ($("#tx-account").value) q.set("account_id", $("#tx-account").value);
  if ($("#tx-category").value) q.set("category", $("#tx-category").value);
  if ($("#tx-search").value.trim()) q.set("search", $("#tx-search").value.trim());
  if ($("#tx-review").checked) q.set("review", "true");
  const tx = await api("/api/transactions?" + q);
  $("#tx-count").textContent = `${tx.length} transactions`;
  $("#tx-table").innerHTML = txTable(tx);
  wireTxTable($("#tx-table"), () => loaders.transactions().catch(showError));
};
for (const id of ["tx-period", "tx-account", "tx-category", "tx-review"]) document.getElementById(id).onchange = () => loaders.transactions().catch(showError);
let _searchT; $("#tx-search").oninput = () => { clearTimeout(_searchT); _searchT = setTimeout(() => loaders.transactions().catch(showError), 300); };

// ---------------- Income & pay stubs ----------------
loaders.income = async () => {
  const [monthly, stubs] = await Promise.all([api("/api/monthly?months=24"), api("/api/paystubs")]);
  const m = monthly.months.filter((r) => r.paychecks || r.month >= monthly.months[monthly.months.length - 12]?.month);
  plot("inc-chart", m.length ? [{
    type: "bar", x: m.map((r) => r.month), y: m.map((r) => r.paychecks), marker: { color: m.map((r) => r.paycheck_count >= 3 ? "#1b5e20" : "#66bb6a") },
    text: m.map((r) => r.paycheck_count ? `${r.paycheck_count}×` : ""), textposition: "outside", hovertemplate: "%{x}: %{y:$,.2f}<extra></extra>",
  }] : [], { showlegend: false });
  $("#inc-note").textContent = monthly.typical_paycheck ? `Dark green bars are months with 3 paychecks. Typical paycheck ${money(monthly.typical_paycheck)}, about ${money(monthly.avg_monthly_from_typical)} per month averaged over a year (26 paychecks ÷ 12).` : "No paychecks detected yet.";
  $("#inc-table").innerHTML = `<table><thead><tr><th>Month</th><th class="num">Paychecks</th><th class="num">Count</th><th class="num">Other income</th></tr></thead><tbody>` +
    m.slice().reverse().map((r) => `<tr><td>${r.month}</td><td class="num">${money(r.paychecks)}</td><td class="num">${r.paycheck_count}${r.paycheck_count >= 3 ? ' <span class="badge accent">3-paycheck month</span>' : ""}</td><td class="num">${money(r.other_income)}</td></tr>`).join("") + "</tbody></table>";
  renderStubs(stubs);
};

function renderStubs({ stubs, fields }) {
  if (!stubs.length) { $("#stub-list").innerHTML = '<div class="empty">No pay stubs imported yet.</div>'; return; }
  $("#stub-list").innerHTML = stubs.map((s) => `
    <div class="card" style="box-shadow:none">
      <div class="toolbar" style="justify-content:space-between">
        <div><b>Paid ${esc(s.pay_date || "?")}</b> <span class="muted">· period ${esc(s.period_start || "?")} – ${esc(s.period_end || "?")} · ${esc(s.employer || "")} · ${esc(s.file_name)}</span>
        ${s.status === "confirmed" ? '<span class="badge good">confirmed</span>' : '<span class="badge warn">needs review</span>'}</div>
        <div><button class="btn small" data-edit-stub="${s.id}">${s.status === "confirmed" ? "Edit" : "Review &amp; confirm"}</button>
          <button class="btn small danger" data-del-stub="${s.id}">Remove</button></div>
      </div>
      ${s.parse_notes ? `<div class="alert warn small">${esc(s.parse_notes).replace(/\n/g, "<br>")}</div>` : ""}
      <table><thead><tr><th></th><th class="num">This period</th><th class="num">Year-to-date</th><th></th></tr></thead><tbody>
      ${fields.map(([f, label]) => { const v = s.fields[f] || {}; if (!v.current && !v.ytd && v.confidence === "high") return "";
        return `<tr><td>${esc(label)}</td><td class="num">${money(v.current)}</td><td class="num">${money(v.ytd)}</td>
        <td>${v.confidence === "low" || v.confidence === "missing" ? '<span class="badge warn">check</span>' : v.confidence === "user" ? '<span class="badge accent">edited</span>' : ""}</td></tr>`; }).join("")}
      </tbody></table>
      <details><summary class="small muted">All lines read from the PDF</summary>
        <table class="small">${s.lines.map((l) => `<tr><td class="muted">${esc(l.section)}</td><td>${esc(l.label)}</td><td class="num">${money(l.current)}</td><td class="num">${money(l.ytd)}</td></tr>`).join("")}</table></details>
    </div>`).join("");
  $$("[data-edit-stub]").forEach((b) => b.onclick = () => editStub(stubs.find((s) => s.id == b.dataset.editStub), fields));
  $$("[data-del-stub]").forEach((b) => b.onclick = async () => {
    if (!confirm("Remove this pay stub from the dashboard? (The PDF file is not deleted.)")) return;
    await api("/api/paystubs/" + b.dataset.delStub, { method: "DELETE" }); loaders.income();
  });
}

function editStub(s, fields) {
  openDialog(`Pay stub paid ${s.pay_date || "?"}`, `
    <p class="small muted">Correct anything that doesn't match your PDF, then confirm. Fields marked "check" couldn't be read confidently.</p>
    <label>Payment date <input type="date" id="stub-paydate" value="${esc(s.pay_date || "")}"></label>
    <table style="margin-top:10px"><thead><tr><th></th><th>This period</th><th>Year-to-date</th></tr></thead><tbody>
    ${fields.map(([f, label]) => { const v = s.fields[f] || {}; const flag = v.confidence === "low" || v.confidence === "missing";
      return `<tr><td>${esc(label)} ${flag ? '<span class="badge warn">check</span>' : ""}</td>
      <td><input type="number" step="0.01" data-f="${f}" data-k="current" value="${v.current ?? ""}" style="width:130px"></td>
      <td><input type="number" step="0.01" data-f="${f}" data-k="ytd" value="${v.ytd ?? ""}" style="width:130px"></td></tr>`; }).join("")}
    </tbody></table><div id="stub-check" class="small" style="margin-top:8px"></div>`,
  [{ label: "Cancel" }, { label: "Save & confirm", cls: "primary", onClick: async () => {
    const out = {};
    for (const inp of $$("#dlg-body input[data-f]")) { (out[inp.dataset.f] ||= {})[inp.dataset.k] = inp.value === "" ? 0 : +inp.value; }
    await api("/api/paystubs/" + s.id, { method: "PUT", body: { fields: out, pay_date: $("#stub-paydate").value || null, confirm: true } });
    toast("Pay stub confirmed"); await loadBasics(); loaders.income();
  } }]);
  const check = () => {
    const v = (f, k) => +($(`#dlg-body input[data-f="${f}"][data-k="${k}"]`)?.value || 0);
    const ded = ["federal_tax", "state_tax", "local_tax", "social_security", "medicare", "other_taxes", "k401_pretax", "k401_roth", "hsa", "espp", "other_pretax", "other_posttax"];
    const msgs = ["current", "ytd"].map((k) => {
      const diff = v("gross", k) - ded.reduce((a, f) => a + v(f, k), 0) - v("net_pay", k);
      return Math.abs(diff) < 0.02 ? `<span class="pos">✓ ${k === "ytd" ? "YTD" : "This period"} adds up</span>` : `<span class="neg">${k === "ytd" ? "YTD" : "This period"}: gross − deductions − net = ${money(diff)}</span>`;
    });
    $("#stub-check").innerHTML = msgs.join(" &nbsp; ");
  };
  $$("#dlg-body input[data-f]").forEach((i) => i.oninput = check); check();
}
$("#stub-import").onclick = async () => {
  try {
    const r = await api("/api/paystubs/import", { method: "POST", body: { force: false } });
    toast(`Imported ${r.imported.length}, already had ${r.skipped.length}${r.errors.length ? ", errors: " + r.errors.join("; ") : ""}`, 6000);
    await loadBasics(); loaders.income();
  } catch (e) { showError(e); }
};
$("#stub-reparse").onclick = async () => {
  if (!confirm("Re-read every PDF in the folder? Manual edits to pay stubs will be replaced by what's read from the files.")) return;
  try { await api("/api/paystubs/import", { method: "POST", body: { force: true } }); await loadBasics(); loaders.income(); } catch (e) { showError(e); }
};

// ---------------- Investments ----------------
loaders.investments = async () => {
  const inv = await api("/api/investments");
  $("#inv-kpis").innerHTML = `<div class="kpi"><div class="label">Total invested</div><div class="value">${money(inv.total)}</div></div>` +
    inv.by_account.map((a) => `<div class="kpi"><div class="label">${esc(a.name)}</div><div class="value">${money0(a.value)}</div><div class="note">${pct(a.pct)}</div></div>`).join("");
  const donut = (rows) => rows.length ? [{ type: "pie", hole: 0.55, labels: rows.map((r) => r.name), values: rows.map((r) => r.value),
    textinfo: "percent", hovertemplate: "%{label}: %{value:$,.2f} (%{percent})<extra></extra>", sort: false }] : [];
  plot("inv-account", donut(inv.by_account), { margin: { l: 10, r: 10, t: 10, b: 10 }, legend: { orientation: "v" } });
  plot("inv-type", donut(inv.by_type), { margin: { l: 10, r: 10, t: 10, b: 10 }, legend: { orientation: "v" } });
  plot("inv-holding", inv.by_holding.length ? [{ type: "bar", x: inv.by_holding.map((h) => h.name), y: inv.by_holding.map((h) => h.value),
    text: inv.by_holding.map((h) => pct(h.pct)), textposition: "outside", marker: { color: cssVar("--accent") }, hovertemplate: "%{x}: %{y:$,.2f}<extra></extra>" }] : [], { showlegend: false });
  $("#inv-table").innerHTML = inv.holdings.length ? `<table><thead><tr><th>Holding</th><th>Account</th><th>Type</th><th class="num">Quantity</th><th class="num">Price</th><th class="num">Value</th><th class="num">% of total</th><th class="num">Gain</th></tr></thead><tbody>` +
    inv.holdings.map((h) => `<tr><td><b>${esc(h.ticker || "")}</b> ${esc(h.name)}</td><td class="small">${esc(h.account)}</td><td>${esc(h.type)}</td>
      <td class="num">${h.quantity == null ? "—" : (+h.quantity).toLocaleString(undefined, { maximumFractionDigits: 4 })}</td><td class="num">${money(h.price)}</td>
      <td class="num">${money(h.value)}</td><td class="num">${pct(h.pct)}</td>
      <td class="num">${h.cost_basis ? `<span class="${h.value >= h.cost_basis ? "pos" : "neg"}">${money(h.value - h.cost_basis)}</span>` : "—"}</td></tr>`).join("") + "</tbody></table>"
    : '<div class="empty">No holdings yet</div>';
};

// ---------------- Recurring ----------------
loaders.recurring = async () => {
  const r = await api("/api/recurring");
  const active = r.items.filter((i) => i.active);
  const subs = active.filter((i) => i.category === "Subscriptions");
  $("#rec-kpis").innerHTML = `
    <div class="kpi"><div class="label">Active recurring / month</div><div class="value">${money(r.monthly_total_active)}</div><div class="note">${active.length} charges</div></div>
    <div class="kpi"><div class="label">Subscriptions / month</div><div class="value">${money(subs.reduce((a, i) => a + i.monthly_cost, 0))}</div><div class="note">${subs.length} subscriptions</div></div>`;
  $("#rec-table").innerHTML = r.items.length ? `<table><thead><tr><th>Merchant</th><th>Category</th><th>Account</th><th>How often</th><th class="num">Typical charge</th><th class="num">Monthly cost</th><th>Last</th><th>Next expected</th></tr></thead><tbody>` +
    r.items.map((i) => `<tr style="${i.active ? "" : "opacity:.55"}"><td>${esc(i.merchant)} ${i.active ? "" : '<span class="badge">stopped?</span>'}</td>
      <td><span class="dot" style="background:${S.catColors[i.category] || "#999"}"></span>${esc(i.category)}</td><td class="small">${esc(i.account)}</td>
      <td>${esc(i.frequency)}</td><td class="num">${money(i.typical_amount)}</td><td class="num"><b>${money(i.monthly_cost)}</b></td>
      <td>${esc(i.last_date)}</td><td>${i.active ? esc(i.next_expected) : "—"}</td></tr>`).join("") + "</tbody></table>"
    : '<div class="empty">No recurring charges detected yet (needs at least 3 repeats).</div>';
};

// ---------------- Rules ----------------
loaders.rules = async () => {
  const rules = await api("/api/rules");
  $("#rules-table").innerHTML = `<table><thead><tr><th>Priority</th><th>Rule</th><th>Matches text</th><th>Conditions</th><th>Category</th><th class="num">Used</th><th></th></tr></thead><tbody>` +
    rules.map((r) => {
      const cond = [r.account_label ? `account: ${esc(r.account_label)}` : "", r.account_type ? `${esc(r.account_type)} accounts` : "",
        r.direction ? (r.direction === "out" ? "money out" : "money in") : "",
        r.amount_min != null || r.amount_max != null ? `$${r.amount_min ?? 0}–$${r.amount_max ?? "∞"}` : "",
        r.day_min ? `day ${r.day_min}–${r.day_max}` : ""].filter(Boolean).join(", ");
      return `<tr style="${r.enabled ? "" : "opacity:.5"}"><td>${r.priority}</td><td>${esc(r.name)} ${r.is_system ? '<span class="badge">built-in</span>' : ""}</td>
        <td><code>${esc(r.pattern)}</code> <span class="small muted">${esc(r.match_type)}</span></td><td class="small">${cond || "—"}</td>
        <td><span class="dot" style="background:${S.catColors[r.category] || "#999"}"></span>${esc(r.category)}</td><td class="num">${r.matches}</td>
        <td style="white-space:nowrap"><button class="btn small" data-rule-edit="${r.id}">Edit</button>
          <button class="btn small" data-rule-toggle="${r.id}">${r.enabled ? "Disable" : "Enable"}</button>
          <button class="btn small danger" data-rule-del="${r.id}">Delete</button></td></tr>`;
    }).join("") + "</tbody></table>";
  const byId = Object.fromEntries(rules.map((r) => [r.id, r]));
  $$("[data-rule-edit]").forEach((b) => b.onclick = () => fillRuleForm(byId[b.dataset.ruleEdit]));
  $$("[data-rule-toggle]").forEach((b) => b.onclick = async () => {
    const r = byId[b.dataset.ruleToggle]; await api("/api/rules/" + r.id, { method: "PUT", body: { ...ruleBody(r), enabled: !r.enabled } }); loaders.rules();
  });
  $$("[data-rule-del]").forEach((b) => b.onclick = async () => {
    if (!confirm("Delete this rule?")) return; await api("/api/rules/" + b.dataset.ruleDel, { method: "DELETE" }); loaders.rules();
  });
};
function ruleBody(r) {
  const num = (v) => v === "" || v == null ? null : +v;
  return { name: r.name, pattern: r.pattern, match_type: r.match_type || "word", category: r.category, account_id: r.account_id || null,
    account_type: r.account_type || null, direction: r.direction || null, amount_min: num(r.amount_min), amount_max: num(r.amount_max),
    day_min: num(r.day_min), day_max: num(r.day_max), priority: +(r.priority ?? 50), enabled: r.enabled === undefined ? true : !!r.enabled };
}
function fillRuleForm(r) {
  const f = $("#rule-form");
  for (const k of ["id", "name", "pattern", "match_type", "category", "account_id", "direction", "amount_min", "amount_max", "day_min", "day_max", "priority"])
    f.elements[k].value = r[k] ?? "";
  f.dataset.accountType = r.account_type || ""; f.dataset.enabled = r.enabled ? "1" : "0";
  $("#rule-form-title").textContent = `Edit rule: ${r.name}`; $("#rule-cancel").hidden = false;
  f.scrollIntoView({ behavior: "smooth" });
}
function resetRuleForm() {
  const f = $("#rule-form"); f.reset(); f.elements.id.value = ""; f.dataset.accountType = ""; f.dataset.enabled = "1";
  $("#rule-form-title").textContent = "Add a rule"; $("#rule-cancel").hidden = true; $("#rule-test-result").innerHTML = "";
}
function formRule() {
  const f = $("#rule-form"); const d = Object.fromEntries(new FormData(f));
  return ruleBody({ ...d, account_type: f.dataset.accountType || null, enabled: f.dataset.enabled !== "0" });
}
$("#rule-cancel").onclick = resetRuleForm;
$("#rule-form").onsubmit = async (e) => {
  e.preventDefault();
  try {
    const id = $("#rule-form").elements.id.value;
    const r = await api(id ? "/api/rules/" + id : "/api/rules", { method: id ? "PUT" : "POST", body: formRule() });
    toast(`Rule saved. ${r.recategorized} transactions updated.`); resetRuleForm(); loaders.rules();
  } catch (err) { showError(err); }
};
$("#rule-test").onclick = async () => {
  try {
    const r = await api("/api/rules/test", { method: "POST", body: formRule() });
    $("#rule-test-result").innerHTML = `<b>${r.count}</b> transactions would match.` + (r.sample.length ? `<div class="table-wrap" style="max-height:260px;overflow:auto">` +
      `<table>${r.sample.map((t) => `<tr><td>${esc(t.date)}</td><td>${esc(t.merchant_name || t.name)}</td><td class="small">${esc(t.account_label)}</td><td>${esc(t.category)}</td><td class="num">${money(-t.amount)}</td></tr>`).join("")}</table></div>` : "");
  } catch (err) { showError(err); }
};

// ---------------- Connections ----------------
loaders.connections = async () => {
  await loadBasics();
  const st = S.status;
  const used = st.production_slots_used, lim = st.production_slot_limit;
  $("#conn-env").innerHTML = st.env === "production"
    ? `<div class="alert good"><b>Production mode</b>: real accounts. <b>${used} of ${lim}</b> Production Items used.
       <div class="slots">${Array.from({ length: lim }, (_, i) => `<span class="${i < used ? "used" : ""}"></span>`).join("")}</div></div>`
    : `<div class="alert warn"><b>Sandbox mode</b>: fake test data only. Production Items used so far: <b>${used} of ${lim}</b>.
       To switch to your real accounts, change <code>PLAID_ENV</code> in .env to <code>production</code> and restart (see README).</div>`;
  $("#demo-btn").hidden = st.env !== "sandbox";
  $("#conn-items").innerHTML = st.items.length ? `<table><thead><tr><th>Institution</th><th>Accounts</th><th>Status</th><th>Last synced</th><th></th></tr></thead><tbody>` +
    st.items.map((it) => `<tr><td><b>${esc(it.institution_name)}</b><div class="small muted">connected ${esc((it.created_at || "").slice(0, 10))}</div></td>
      <td class="small">${it.accounts.map((a) => `${esc(a.display_name || a.name)} ${a.mask ? "••" + esc(a.mask) : ""} <span class="muted">(${esc(a.subtype)})</span>`).join("<br>")}</td>
      <td>${it.status === "ok" ? '<span class="badge good">connected</span>' : `<span class="badge bad">${esc(it.status.replace("_", " "))}</span><div class="small">${esc(it.error_message || "")}</div>`}
        ${it.consent_expiring_soon ? `<div><span class="badge warn">access expires ${esc(it.consent_expiration.slice(0, 10))}</span></div>` : ""}</td>
      <td class="small">${esc((it.last_sync_at || "never").replace("T", " ").slice(0, 16))}</td>
      <td style="white-space:nowrap">
        <button class="btn small" data-sync="${esc(it.item_id)}">Sync</button>
        <button class="btn small ${it.status !== "ok" ? "primary" : ""}" data-update="${esc(it.item_id)}" title="Re-login on this same connection. Does not create a new Item.">Fix connection</button>
        <button class="btn small" data-addacct="${esc(it.item_id)}" title="Share additional accounts from this same login (e.g. a new credit card)">Add/remove accounts</button>
        <button class="btn small" data-live="${esc(it.item_id)}" title="Ask the bank for up-to-the-second balances (slow; Plaid Balance)">Live balance</button>
        ${st.env === "sandbox" ? `<button class="btn small" data-expire="${esc(it.item_id)}" title="Sandbox only: simulate an expired login">Simulate expiry</button>
        <button class="btn small danger" data-remove="${esc(it.item_id)}">Remove</button>` : ""}
      </td></tr>`).join("") + "</tbody></table>"
    : `<div class="empty">No bank connections yet.${st.env === "sandbox" ? " Click <b>Load Sandbox demo data</b> for a quick demo, or <b>Add a bank account</b> to try Plaid's connect window (use username <code>user_good</code>, password <code>pass_good</code>)." : ""}</div>`;
  const wire = (attr, fn) => $$(`[data-${attr}]`).forEach((b) => b.onclick = () => fn(b.dataset[attr.replace(/-(\w)/g, (_, c) => c.toUpperCase())], b));
  wire("sync", async (id, b) => { await busy(b, () => api(`/api/items/${id}/sync`, { method: "POST" })).then(syncReport); loaders.connections(); });
  wire("update", (id) => openLink({ item_id: id }));
  wire("addacct", (id) => openLink({ item_id: id, account_selection: true }));
  wire("live", async (id, b) => { await busy(b, () => api(`/api/items/${id}/live-balance`, { method: "POST" })); toast("Live balances updated"); });
  wire("expire", async (id) => { await api(`/api/sandbox/reset-login/${id}`, { method: "POST" }); toast("Login expired (simulated). Click Sync to see it detected."); });
  wire("remove", async (id) => {
    if (!confirm("Remove this Sandbox connection and its data?")) return;
    await api(`/api/items/${id}/remove`, { method: "POST" }); loaders.connections();
  });

  const pl = st.production_log;
  $("#prod-log").innerHTML = pl.length ? `<table><thead><tr><th>#</th><th>Date</th><th>Institution</th><th>Event</th><th>Counts toward 10</th><th>Note</th></tr></thead><tbody>` +
    pl.map((r, i) => `<tr><td>${i + 1}</td><td>${esc((r.created_at || "").replace("T", " ").slice(0, 16))}</td><td>${esc(r.institution_name)}</td><td>${esc(r.event)}</td>
      <td>${r.counts_toward_limit ? "yes" : "no"}</td><td class="small">${esc(r.note || "")}</td></tr>`).join("") + "</tbody></table>"
    : `<p class="muted">No Production Items yet. <b>${used} of ${lim}</b> used.</p>`;

  const manual = S.accounts.filter((a) => a.source === "manual");
  $("#manual-list").innerHTML = manual.length ? `<table>${manual.map((a) => `<tr><td>${esc(a.label)} <span class="small muted">${esc(a.institution_name || "")}</span></td>
    <td><input type="number" step="0.01" value="${a.current_balance ?? 0}" data-manbal="${esc(a.account_id)}" style="width:120px"></td>
    <td><button class="btn small" data-mansave="${esc(a.account_id)}">Update balance</button> <button class="btn small danger" data-mandel="${esc(a.account_id)}">Delete</button></td></tr>`).join("")}</table>` : "";
  wire("mansave", async (id) => {
    await api("/api/accounts/" + id, { method: "PUT", body: { current_balance: +$(`[data-manbal="${CSS.escape(id)}"]`).value } }); toast("Balance updated");
  });
  wire("mandel", async (id) => { if (!confirm("Delete this manual account?")) return; await api("/api/accounts/" + id, { method: "DELETE" }); loaders.connections(); });

  const settings = await api("/api/settings");
  const lbl = { invest_include_espp: "ESPP / ASPP contributions", invest_include_hsa: "HSA contributions", invest_include_employer_match: "Employer 401(k) match" };
  $("#settings-form").innerHTML = Object.entries(lbl).map(([k, l]) =>
    `<label style="flex-direction:row;gap:8px;color:var(--text);font-size:14px;margin-bottom:6px"><input type="checkbox" data-setting="${k}" ${settings[k] ? "checked" : ""}> ${l}</label><br>`).join("");
  $$("[data-setting]").forEach((c) => c.onchange = async () => { await api("/api/settings", { method: "PUT", body: { [c.dataset.setting]: c.checked } }); toast("Saved"); });

  $("#acct-settings").innerHTML = S.accounts.length ? `<table><thead><tr><th>Account</th><th>Display name</th><th>Net worth</th><th>Hidden</th></tr></thead><tbody>` +
    S.accounts.map((a) => `<tr><td class="small">${esc(a.name)} ${a.mask ? "••" + esc(a.mask) : ""}<div class="muted">${esc(a.institution_name || "")}</div></td>
      <td><input data-dname="${esc(a.account_id)}" value="${esc(a.display_name || "")}" placeholder="${esc(a.name)}" size="16"></td>
      <td><input type="checkbox" data-innw="${esc(a.account_id)}" ${a.include_in_networth ? "checked" : ""}></td>
      <td><input type="checkbox" data-hide="${esc(a.account_id)}" ${a.hidden ? "checked" : ""}></td></tr>`).join("") + "</tbody></table>" : '<p class="muted">No accounts yet.</p>';
  $$("[data-dname]").forEach((i) => i.onchange = () => api("/api/accounts/" + i.dataset.dname, { method: "PUT", body: { display_name: i.value } }).then(() => toast("Renamed")).catch(showError));
  $$("[data-innw]").forEach((i) => i.onchange = () => api("/api/accounts/" + i.dataset.innw, { method: "PUT", body: { include_in_networth: i.checked } }).then(() => toast("Saved")).catch(showError));
  $$("[data-hide]").forEach((i) => i.onchange = () => api("/api/accounts/" + i.dataset.hide, { method: "PUT", body: { hidden: i.checked } }).then(() => toast("Saved")).catch(showError));
};

async function busy(btn, fn) {
  const old = btn.innerHTML; btn.disabled = true; btn.innerHTML = '<span class="spinner"></span>';
  try { return await fn(); } finally { btn.disabled = false; btn.innerHTML = old; }
}

function syncReport(r) {
  const results = r.results || [r];
  const bad = results.filter((x) => x.error || x.needs_update);
  const importing = results.filter((x) => { const s = x.steps?.transactions?.status; return s && s !== "HISTORICAL_UPDATE_COMPLETE"; });
  if (bad.length) toast(`Synced with problems: ${bad.map((b) => b.error?.display_message || b.error?.error_code || "needs re-login").join("; ")}`, 8000);
  else if (importing.length) toast("Connected. Plaid is still downloading your transaction history (up to 2 years). Click 'Sync now' again in a minute or two.", 10000);
  else toast(`Synced ${results.length} connection(s)`);
}

// Auto-sync when the dashboard opens, at most every 6 hours (Plaid refreshes bank data ~1-4x/day).
async function autoSyncIfStale() {
  const last = S.status.last_sync ? new Date(S.status.last_sync) : null;
  if (!S.status.items.length || (last && Date.now() - last.getTime() < 6 * 3600 * 1000)) return;
  const btn = $("#sync-btn");
  try { toast("Syncing with your banks…"); const r = await busy(btn, () => api("/api/sync", { method: "POST" })); syncReport(r); await refreshCurrent(); }
  catch (e) { showError(e); }
}

$("#sync-btn").onclick = async (e) => {
  try { const r = await busy(e.currentTarget, () => api("/api/sync", { method: "POST" })); syncReport(r); await refreshCurrent(); }
  catch (err) { showError(err); }
};
$("#demo-btn").onclick = async (e) => {
  try {
    const r = await busy(e.currentTarget, () => api("/api/sandbox/demo", { method: "POST" }));
    const msgs = r.results.map((x) => `${x.demo}: ${x.ok ? "ok" : (x.message || "failed")}${x.error ? " (" + (x.error.error_code || "") + ")" : ""}`);
    toast("Demo data: " + msgs.join(", "), 8000); await refreshCurrent();
  } catch (err) { showError(err); }
};

// ---------------- Plaid Link ----------------
async function openLink({ item_id = null, account_selection = false } = {}) {
  try {
    let body = { item_id, account_selection };
    if (!item_id && S.status.env === "production") {
      const ok = await confirmProduction();
      if (!ok) return;
      body = { ...body, confirm_production: true, institution: ok };
    }
    const { link_token } = await api("/api/link/token", { method: "POST", body });
    if (S.status.redirect_uri) localStorage.setItem("plaid_link", JSON.stringify({ link_token, item_id }));
    startLink(link_token, item_id);
  } catch (e) { showError(e); }
}

function startLink(token, item_id, receivedRedirectUri) {
  const handler = Plaid.create({
    token, receivedRedirectUri,
    onSuccess: async (public_token, metadata) => {
      handler.destroy?.();
      localStorage.removeItem("plaid_link");
      const meta = { institution: metadata.institution, accounts: metadata.accounts, link_session_id: metadata.link_session_id, status: metadata.status };
      try {
        toast("Saving connection and downloading data…", 15000);
        if (item_id) { const r = await api("/api/link/update-done", { method: "POST", body: { item_id, metadata: meta } }); syncReport(r); }
        else {
          const r = await api("/api/link/exchange", { method: "POST", body: { public_token, metadata: meta } });
          if (!r.ok) openDialog("Not connected", `<p>${esc(r.message)}</p>`, [{ label: "OK" }]);
          else syncReport(r.sync);
        }
        await refreshCurrent();
      } catch (e) { showError(e); }
    },
    onExit: (err, metadata) => {
      handler.destroy?.();
      api("/api/link/event", { method: "POST", body: { event: err ? "EXIT_ERROR" : "EXIT", metadata: { ...metadata, error: err } } }).catch(() => {});
      if (err) openDialog("Connection didn't finish", `<p>${esc(err.display_message || err.error_message || err.error_code)}</p>
        <p class="small muted">Error code ${esc(err.error_code)}${metadata?.request_id ? " · request " + esc(metadata.request_id) : ""}. You can try again.</p>`, [{ label: "OK" }]);
    },
    onEvent: (name, metadata) => {
      if (["OPEN", "SELECT_INSTITUTION", "OPEN_OAUTH", "SUBMIT_CREDENTIALS", "HANDOFF", "ERROR", "EXIT"].includes(name))
        api("/api/link/event", { method: "POST", body: { event: name, metadata: { institution: { institution_id: metadata.institution_id, name: metadata.institution_name },
          link_session_id: metadata.link_session_id, request_id: metadata.request_id, error: metadata.error_code ? { error_code: metadata.error_code, error_type: metadata.error_type } : null } } }).catch(() => {});
    },
  });
  handler.open();
}

function confirmProduction() {
  return new Promise((resolve) => {
    const st = S.status;
    const connected = st.items.map((i) => esc(i.institution_name)).join(", ") || "none";
    openDialog("Connect a real account (Production)", `
      <div class="alert warn">This creates a new Production <b>Item</b>. You have used <b>${st.production_slots_used} of ${st.production_slot_limit}</b>.
      Items can never be given back, even if removed.</div>
      <p>Already connected: <b>${connected}</b>. If the account you want is at one of these, cancel and use <b>Fix connection</b> or <b>Add/remove accounts</b> instead.</p>
      <label>Which institution are you connecting? <input id="prod-inst" placeholder="e.g. My Bank" size="30"></label>
      <p class="small muted">Connect one institution at a time. In the Plaid window, pick exactly this institution.</p>`,
    [{ label: "Cancel", onClick: () => resolve(null) },
      { label: "Use 1 Item and continue", cls: "primary", onClick: () => {
        const v = $("#prod-inst").value.trim(); if (!v) { toast("Type the institution name first"); return false; } resolve(v);
      } }]);
    dlg.addEventListener("close", () => resolve(null), { once: true });
  });
}
$("#connect-btn").onclick = () => openLink();

// OAuth return (only used if PLAID_REDIRECT_URI is configured)
function resumeOAuthIfNeeded() {
  if (!location.search.includes("oauth_state_id")) return false;
  const saved = JSON.parse(localStorage.getItem("plaid_link") || "null");
  if (!saved) return false;
  startLink(saved.link_token, saved.item_id, window.location.href);
  history.replaceState(null, "", "/#connections");
  return true;
}

// ---------------- Manual account / CSV / verify ----------------
$("#manual-form").onsubmit = async (e) => {
  e.preventDefault();
  const d = Object.fromEntries(new FormData(e.target)); d.balance = +d.balance || 0; d.institution ||= "Manual";
  try { await api("/api/accounts/manual", { method: "POST", body: d }); e.target.reset(); toast("Account added"); loaders.connections(); } catch (err) { showError(err); }
};

let csvState = null;
$("#csv-preview-btn").onclick = async () => {
  const f = $("#csv-file").files[0]; if (!f) return toast("Choose a CSV file first");
  const fd = new FormData(); fd.append("file", f);
  try { csvState = await api("/api/csv/preview", { method: "POST", body: fd }); csvState.flip = false; renderCsvPreview(); } catch (e) { showError(e); }
};
function renderCsvPreview() {
  const p = csvState;
  const acctOpts = S.accounts.map((a) => `<option value="${esc(a.account_id)}">${esc(a.label)} (${esc(a.institution_name || "")})</option>`).join("");
  let table;
  if (p.kind === "positions") {
    table = `<p>Holdings snapshot · ${p.count} positions · total ${money(p.total_value)}. Importing replaces this account's current holdings.</p>
      <table class="small">${p.sample.map((r) => `<tr><td>${esc(r.ticker)}</td><td>${esc(r.name)}</td><td class="num">${r.quantity ?? ""}</td><td class="num">${money(r.value)}</td></tr>`).join("")}</table>`;
  } else {
    table = `<p>${p.count} transactions${p.date_range ? ` from ${p.date_range[0]} to ${p.date_range[1]}` : ""}${p.skipped_rows ? ` · ${p.skipped_rows} unreadable rows skipped` : ""}.
      Check that money out/in looks right:</p>
      <table class="small">${p.sample.map((r) => `<tr><td>${esc(r.date)}</td><td>${esc(r.description)}</td><td class="num">${r.amount > 0 ? `<span class="neg">out ${money(r.amount)}</span>` : `<span class="pos">in ${money(-r.amount)}</span>`}</td></tr>`).join("")}</table>
      <label style="flex-direction:row;gap:6px;margin-top:6px"><input type="checkbox" id="csv-flip" ${p.flip ? "checked" : ""}> Money in/out is backwards, flip it</label>`;
  }
  $("#csv-result").innerHTML = `<div class="alert info small">Detected: <b>${esc(p.format)}</b></div>${table}
    <div class="form" style="margin-top:10px"><label>Import into account<select id="csv-account">${acctOpts}</select></label>
    <button class="btn primary" id="csv-go">Import</button></div>
    <p class="small muted">No matching account? Add a manual account first.</p>`;
  $("#csv-flip")?.addEventListener("change", async (e) => {
    csvState = { ...(await api("/api/csv/repreview", { method: "POST", body: { file_id: p.file_id, account_id: "", flip: e.target.checked } })), flip: e.target.checked };
    renderCsvPreview();
  });
  $("#csv-go").onclick = async (e) => {
    try {
      const r = await busy(e.currentTarget, () => api("/api/csv/import", { method: "POST", body: { file_id: p.file_id, account_id: $("#csv-account").value, flip: !!p.flip } }));
      $("#csv-result").innerHTML = r.format === "positions"
        ? `<div class="alert good">Imported ${r.holdings} holdings (total ${money(r.total_value)}).</div>`
        : `<div class="alert good">Imported <b>${r.imported}</b> new transactions. Skipped ${r.already_from_plaid} already from Plaid and ${r.already_imported} already imported${r.unreadable_rows ? `, ${r.unreadable_rows} unreadable rows` : ""}.</div>`;
    } catch (err) { showError(err); }
  };
}

$("#verify-btn").onclick = async (e) => {
  try {
    const v = await busy(e.currentTarget, () => api("/api/verify"));
    const pairs = {};
    for (const p of v.transfer_pairs) (pairs[p.transfer_pair_id] ||= []).push(p);
    $("#verify-out").innerHTML = `
      <h3>1. Balances (compare with your bank apps)</h3>
      <table><thead><tr><th>Account</th><th class="num">Current</th><th class="num">Available</th><th>As of</th></tr></thead><tbody>
      ${v.accounts.map((a) => `<tr><td>${esc(a.label)} <span class="small muted">${esc(a.institution_name || "")}</span></td><td class="num">${money(a.current_balance)}</td><td class="num">${money(a.available_balance)}</td><td class="small">${esc((a.balance_updated_at || "").replace("T", " ").slice(0, 16))}</td></tr>`).join("")}</tbody></table>
      <p class="small muted">Plaid's regular balances are refreshed by the bank roughly daily. Use "Live balance" for an up-to-the-second check.</p>
      ${v.investment_check.length ? `<p><b>Investment accounts: balance vs. holdings</b></p><table class="small">${v.investment_check.map((c) => `<tr><td>${esc(c.label)}</td>
        <td class="num">balance ${money(c.balance)}</td><td class="num">holdings ${money(c.holdings_total)}</td>
        <td>${Math.abs(c.difference) < 1 ? '<span class="pos">✓ match</span>' : `<span class="neg">differ by ${money(c.difference)}</span>`}</td></tr>`).join("")}</table>
        <p class="small muted">Net worth uses the balance; the Investments page uses holdings. A difference usually means delayed prices or cash not reported as a holding.</p>` : ""}
      <h3 style="margin-top:16px">2. Transfers matched between your accounts (excluded from spending and income)</h3>
      ${Object.values(pairs).length ? `<table class="small">${Object.values(pairs).slice(0, 40).map((pr) => pr.map((p, i) => `<tr${i ? "" : ' style="border-top:2px solid var(--border)"'}><td>${esc(p.date)}</td><td>${esc(p.account_label)}</td><td>${esc(p.name)}</td><td class="num">${money(-p.amount)}</td><td>${esc(p.category)}</td></tr>`).join("")).join("")}</table>` : '<p class="muted">None found.</p>'}
      <h3 style="margin-top:16px">3. Might be transfers but are counted as spending/income</h3>
      ${v.possible_missed_transfers.length ? `<table class="small">${v.possible_missed_transfers.map((t) => `<tr><td>${esc(t.date)}</td><td>${esc(t.account_label)}</td><td>${esc(t.name)}</td><td class="num">${money(-t.amount)}</td><td>${esc(t.category)}</td></tr>`).join("")}</table>
        <p class="small muted">If any of these moved money between your own accounts, set the category to "Transfer" on the Transactions page.</p>` : '<p class="pos">✓ Nothing suspicious.</p>'}
      <h3 style="margin-top:16px">4. Pay stubs</h3>
      ${v.paystubs.map((s) => `<p>${esc(s.pay_date)}: ${s.status === "confirmed" ? '<span class="pos">✓ totals add up / confirmed</span>' : '<span class="neg">needs review</span>'}
        · gross ${money(s.fields.gross?.current)} · net ${money(s.fields.net_pay?.current)} · YTD gross ${money(s.fields.gross?.ytd)}</p>`).join("") || '<p class="muted">No pay stubs.</p>'}
      <p>Year-to-date paycheck deposits found in bank: <b>${money(v.ytd_bank_paychecks)}</b> · take-home used in cash flow: <b>${money(v.ytd_takehome_used)}</b></p>
      ${v.ytd_cashflow_notes.map((n) => `<div class="alert info small">${esc(n)}</div>`).join("")}`;
  } catch (err) { showError(err); }
};

async function refreshCurrent() {
  await loadBasics();
  show(location.hash.slice(1) || "overview");
}

// ---------------- boot ----------------
(async () => {
  try {
    await loadBasics();
    if (!resumeOAuthIfNeeded()) { show(location.hash.slice(1) || "overview"); autoSyncIfStale(); }
  } catch (e) { showError(e); }
})();
