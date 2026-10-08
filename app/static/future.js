/* Future tab: projections with adjustable scenarios. All math runs on the local
   server (app/projections.py); this file only renders controls and results. */
"use strict";

const FU = { baseline: null, inputs: null, scenarios: [], current: "habits", dirty: false, result: null, mode: "stacked", timer: null };
const BUCKET_COLORS = ["#78909c", "#00897b", "#2e7d32", "#1565c0", "#6a1b9a", "#ef6c00", "#c2185b", "#5d4037", "#00838f", "#827717"];
const clone = (o) => JSON.parse(JSON.stringify(o));
const fmt0 = (v) => money(Math.round(v) || 0, 0);  // avoids "-$0"

// ---------- path helpers (e.g. "buckets.2.rate_pct") ----------
function getPath(obj, path) { return path.split(".").reduce((o, k) => (o == null ? o : o[k]), obj); }
function setPath(obj, path, value) {
  const keys = path.split("."); const last = keys.pop();
  keys.reduce((o, k) => o[k], obj)[last] = value;
}

// ---------- field builders ----------
function sliderField(path, label, { min, max, step, hint = "", unit = "" }) {
  const v = getPath(FU.inputs, path) ?? 0;
  return `<div class="fu-field"><div class="fu-label"><span>${esc(label)}${unit ? ` <span class="muted">(${esc(unit)})</span>` : ""}</span>
    <span class="hint">${hint}</span></div>
    <div class="fu-inputs"><input type="range" data-path="${path}" min="${min}" max="${max}" step="${step}" value="${Math.min(max, Math.max(min, v))}" aria-label="${esc(label)}">
    <input type="number" data-path="${path}" step="${step}" value="${v}" inputmode="decimal" aria-label="${esc(label)}"></div></div>`;
}
function numberField(path, label, { step = 0.01, hint = "" } = {}) {
  return `<div class="fu-field"><div class="fu-label"><span>${esc(label)}</span><span class="hint">${hint}</span></div>
    <div class="fu-inputs"><input type="number" data-path="${path}" step="${step}" value="${getPath(FU.inputs, path) ?? 0}" inputmode="decimal" style="width:100%"></div></div>`;
}
function toggleField(path, label) {
  return `<label class="fu-toggle"><input type="checkbox" data-path="${path}" ${getPath(FU.inputs, path) ? "checked" : ""}> ${esc(label)}</label>`;
}

function bindFields(root) {
  root.querySelectorAll("[data-path]").forEach((el) => {
    const ev = el.type === "checkbox" || el.tagName === "SELECT" ? "change" : "input";
    el.addEventListener(ev, () => {
      const path = el.dataset.path;
      let v;
      if (el.type === "checkbox") v = el.checked;
      else if (el.type === "number" || el.type === "range") { v = el.value === "" ? 0 : parseFloat(el.value); if (isNaN(v)) return; }
      else v = el.value;
      setPath(FU.inputs, path, v);
      if (/^buckets\.\d+\.start$/.test(path)) setPath(FU.inputs, path.replace(".start", ".start_from_data"), false);
      // keep the paired slider / number box in sync
      root.querySelectorAll(`[data-path="${path}"]`).forEach((o) => { if (o !== el && o.type !== "checkbox") o.value = v; });
      markDirty();
      if (el.dataset.rerender) renderControls();
      else updateLiveHints();
      schedule();
    });
  });
}

function markDirty() {
  FU.dirty = true;
  $("#fu-status").textContent = "Unsaved changes";
}

// ---------- controls ----------
function renderControls() {
  const i = FU.inputs, info = FU.baseline.info;
  $("#fu-global").innerHTML =
    sliderField("horizon_years", "Time horizon", { min: 1, max: 40, step: 1, unit: "years" }) +
    sliderField("salary_growth_pct", "Salary growth per year", { min: 0, max: 10, step: 0.1, unit: "%" }) +
    toggleField("contributions_follow_salary", "Contributions grow with salary") +
    sliderField("inflation_pct", "Inflation", { min: 0, max: 8, step: 0.1, unit: "%" }) +
    toggleField("show_real", "Show results in today's dollars (adjusted for inflation)") +
    toggleField("spending_follows_inflation", "Spending rises with inflation") +
    sliderField("range_pp", "Range for stock buckets", { min: 0, max: 6, step: 0.5, unit: "± % points", hint: "low / high case" }) +
    toggleField("checking_floor", "Skip transfers I can't afford (don't let checking go below $0)");

  $("#fu-income").innerHTML =
    sliderField("take_home_per_paycheck", "Take-home per paycheck", { min: 0, max: 10000, step: 1, unit: "$", hint: esc(info.take_home_source) }) +
    numberField("gross_per_paycheck", "Gross pay per paycheck", { hint: `≈ ${fmt0(i.gross_per_paycheck * 26)} / year (26 paychecks)` }) +
    sliderField("monthly_spending", "Average monthly spending", { min: 0, max: 15000, step: 10, unit: "$",
      hint: info.spending_rent ? `non-rent avg + ${fmt0(info.spending_rent.amount)} rent` : `avg of ${info.spending_months.map((m) => m.month).join(", ")}` }) +
    sliderField("other_monthly_income", "Other monthly income", { min: 0, max: 5000, step: 10, unit: "$",
      hint: info.family_support_monthly_avg ? `family support averaged ${fmt0(info.family_support_monthly_avg)}/mo lately` : "" }) +
    sliderField("marginal_tax_pct", "Tax rate on your last dollars of pay", { min: 0, max: 50, step: 1, unit: "%",
      hint: "used when you change pre-tax contributions" });

  $("#fu-buckets").innerHTML = i.buckets.map((b, n) => bucketEditor(b, n)).join("");
  $("#fu-add-form").innerHTML = `
    <div class="fu-row2" style="margin-top:10px">
      <label>Name<input id="fa-name" placeholder="e.g. Stock purchase plan"></label>
      <label>Starting balance<input id="fa-start" type="number" value="0" inputmode="decimal"></label>
      <label>Contribution<input id="fa-contrib" type="number" value="0" inputmode="decimal"></label>
      <label>How often<select id="fa-freq"><option value="monthly">Monthly</option><option value="paycheck">Per paycheck</option></select></label>
      <label>Annual return %<input id="fa-rate" type="number" value="7" step="0.1" inputmode="decimal"></label>
      <label>Paid from<select id="fa-fund"><option value="take_home">Take-home pay</option><option value="pretax">Paycheck, before tax</option><option value="posttax">Paycheck, after tax</option></select></label>
    </div>
    <label class="fu-toggle"><input type="checkbox" id="fa-stock" checked> Stock-based (show low/high range)</label>
    <button class="btn primary" id="fa-add">Add bucket</button>`;
  $("#fa-add").onclick = () => {
    const name = $("#fa-name").value.trim(); if (!name) return toast("Give the bucket a name");
    FU.inputs.buckets.push({ id: "c" + Date.now(), name, kind: "custom", start: +$("#fa-start").value || 0,
      contribution: +$("#fa-contrib").value || 0, frequency: $("#fa-freq").value, rate_pct: +$("#fa-rate").value || 0,
      funding: $("#fa-fund").value, stock: $("#fa-stock").checked, base_contribution: 0, removable: true });
    $(".fu-add").open = false; markDirty(); renderControls(); schedule();
  };

  $("#fu-lumps").innerHTML = i.lump_sums.length ? i.lump_sums.map((l, n) => `
    <div class="fu-lump">
      <select data-path="lump_sums.${n}.bucket_id" aria-label="Bucket">${i.buckets.map((b) => `<option value="${esc(b.id)}" ${b.id === l.bucket_id ? "selected" : ""}>${esc(b.name)}</option>`).join("")}</select>
      <input type="number" data-path="lump_sums.${n}.amount" value="${l.amount}" inputmode="decimal" aria-label="Amount">
      <button class="btn small danger" data-del-lump="${n}" aria-label="Remove">✕</button>
      <label class="fu-toggle" style="grid-column:1/-1;margin:0 0 6px"><input type="checkbox" data-path="lump_sums.${n}.from_checking" ${l.from_checking !== false ? "checked" : ""}> Move it out of checking (uncheck if it's new money, e.g. a bonus)</label>
    </div>`).join("") : '<p class="small muted">E.g. "what if I put $5,000 into the HYSA today?"</p>';

  $("#fu-sources").innerHTML = sourcesHtml();

  for (const id of ["#fu-global", "#fu-income", "#fu-buckets", "#fu-lumps", "#fu-sources"]) bindFields($(id));
  $$("[data-del-bucket]").forEach((btn) => btn.onclick = () => {
    const bid = FU.inputs.buckets[+btn.dataset.delBucket].id;
    FU.inputs.buckets.splice(+btn.dataset.delBucket, 1);
    FU.inputs.lump_sums = FU.inputs.lump_sums.filter((l) => l.bucket_id !== bid);
    markDirty(); renderControls(); schedule();
  });
  $$("[data-del-lump]").forEach((btn) => btn.onclick = () => { FU.inputs.lump_sums.splice(+btn.dataset.delLump, 1); markDirty(); renderControls(); schedule(); });
  $("#fu-save-limit")?.addEventListener("click", saveLimitSetting);
  fillYearSelect();
  updateLiveHints();
}

function bucketEditor(b, n) {
  const color = BUCKET_COLORS[n % BUCKET_COLORS.length];
  const p = `buckets.${n}`;
  const isChecking = b.kind === "checking";
  const perPay = b.frequency === "paycheck";
  let html = `<div class="fu-bucket" style="--bc:${color}">
    <div class="fu-bucket-head">${b.kind === "custom" ? `<input data-path="${p}.name" value="${esc(b.name)}" aria-label="Bucket name">` : `<b>${esc(b.name)}</b>`}
      ${b.removable !== false ? `<button class="btn small danger" data-del-bucket="${n}" aria-label="Remove bucket">✕</button>` : ""}</div>`;
  html += numberField(`${p}.start`, "Starting balance", { hint: b.start_from_data ? "from your accounts" : "" });
  if (isChecking) {
    html += `<p class="small muted" style="margin:-6px 0 12px">Gets whatever is left each month after spending and the transfers below.</p>`;
  } else {
    html += sliderField(`${p}.contribution`, perPay ? "You put in per paycheck" : "You put in per month",
      { min: 0, max: perPay ? 2000 : 5000, step: perPay ? 1 : 10, unit: "$",
        hint: `<span data-live="contrib-${n}"></span>` });
    html += `<div class="fu-row2">
      <label>How often<select data-path="${p}.frequency" data-rerender="1"><option value="monthly" ${!perPay ? "selected" : ""}>Monthly</option><option value="paycheck" ${perPay ? "selected" : ""}>Per paycheck (26/yr)</option></select></label>
      <label>Paid from<select data-path="${p}.funding"><option value="take_home" ${b.funding === "take_home" ? "selected" : ""}>Take-home pay</option><option value="pretax" ${b.funding === "pretax" ? "selected" : ""}>Paycheck, before tax</option><option value="posttax" ${b.funding === "posttax" ? "selected" : ""}>Paycheck, after tax</option></select></label></div>`;
  }
  if (!isChecking) html += employerEditor(b, n);
  html += sliderField(`${p}.rate_pct`, b.kind === "hysa" ? "Interest rate (APY)" : "Annual return", { min: -5, max: 15, step: 0.1, unit: "%" });
  if (b.kind === "401k") {
    html += numberField("k401_limit", "Annual limit for your own contributions", { step: 100, hint: `IRS ${FU.baseline.info.k401_limit_year} limit` });
  }
  if (!isChecking && b.kind !== "401k") {
    html += `<details><summary class="small muted" style="cursor:pointer;margin-bottom:8px">More options</summary>
      ${b.kind === "custom" ? toggleField(`${p}.stock`, "Stock-based (show low/high range)") : ""}
      ${numberField(`${p}.annual_limit`, "Annual contribution limit (0 = none)", { step: 100 })}
      ${toggleField(`${p}.stop_at_limit`, "Stop contributions at the limit each year")}</details>`;
  }
  if (b.note) html += `<p class="small muted" style="margin:4px 0 0">${esc(b.note)}</p>`;
  return html + "</div>";
}

const EMP_BASIS = {
  pct_salary: { label: "% of your pay", max: 15, step: 0.05 },
  pct_contribution: { label: "% of what you put in", max: 100, step: 0.5 },
  per_paycheck: { label: "$ per paycheck", max: 1000, step: 0.01 },
};

function employerEditor(b, n) {
  const p = `buckets.${n}`;
  const basis = EMP_BASIS[b.employer_basis] ? b.employer_basis : "pct_salary";
  const conf = EMP_BASIS[basis];
  const ps = FU.baseline.info.paystub;
  let fromStub = "";
  if (b.kind === "401k" && ps.employer_401k) fromStub = `Pay stub: ${ps.employer_401k_lines.map((l) => `${esc(l.label)} ${l.pct}%`).join(" + ")} = ${ps.employer_401k_pct}% while you put in ${ps.k401_pct}%.`;
  if (b.id === "espp" && ps.employer_aspp) fromStub = `Pay stub: ASPP company match ${money(ps.employer_aspp)} on your ${money(ps.espp)} (${ps.espp_pct}% of pay) = ${ps.employer_aspp_pct_of_contribution}% match.`;
  if (b.id === "hsa" && ps.employer_hsa) fromStub = `Pay stub: employer HSA ${money(ps.employer_hsa)} per paycheck (counts toward the HSA limit).`;
  return `<div class="fu-field"><div class="fu-label"><span>Your company adds</span><span class="hint" data-live="emp-${n}"></span></div>
    <div class="fu-inputs"><input type="range" data-path="${p}.employer_amount" min="0" max="${conf.max}" step="${conf.step}" value="${Math.min(conf.max, b.employer_amount || 0)}" aria-label="Company contribution">
    <input type="number" data-path="${p}.employer_amount" step="${conf.step}" value="${b.employer_amount || 0}" inputmode="decimal" aria-label="Company contribution"></div>
    <select data-path="${p}.employer_basis" data-rerender="1" style="margin-top:6px;width:100%" aria-label="Company contribution type">
      ${Object.entries(EMP_BASIS).map(([k, v]) => `<option value="${k}" ${k === basis ? "selected" : ""}>${v.label}</option>`).join("")}</select>
    ${fromStub ? `<p class="small muted" style="margin:6px 0 0">${fromStub}</p>` : ""}</div>`;
}

// Live "$ per paycheck / % of pay" hints next to contribution sliders.
function updateLiveHints() {
  const gross = FU.inputs.gross_per_paycheck || 0;
  FU.inputs.buckets.forEach((b, n) => {
    const perPay = b.frequency === "paycheck";
    const c = b.contribution || 0;
    const ce = document.querySelector(`[data-live="contrib-${n}"]`);
    if (ce) ce.textContent = perPay
      ? `${gross ? ((100 * c) / gross).toFixed(1) + "% of your pay · " : ""}≈ ${fmt0((c * 26) / 12)}/mo`
      : `≈ ${fmt0(c * 12)}/yr`;
    const ee = document.querySelector(`[data-live="emp-${n}"]`);
    if (ee) {
      const amt = b.employer_amount || 0;
      const basis = b.employer_basis || "pct_salary";
      const perPaycheck = basis === "pct_salary" ? (gross * amt) / 100 : basis === "per_paycheck" ? amt
        : (perPay ? (c * amt) / 100 : null);
      ee.textContent = !amt ? "nothing" : perPaycheck != null ? `≈ ${money(perPaycheck)}/paycheck · ${fmt0((perPaycheck * 26) / 12)}/mo`
        : `≈ ${fmt0((c * amt) / 100)}/mo`;
    }
  });
}

function sourcesHtml() {
  const info = FU.baseline.info;
  const list = (rows) => rows.map((r) => `${esc(r.month)}: ${money(r.amount, 0)}`).join(" · ");
  return `
    <p><b>Take-home:</b> ${esc(info.take_home_source)}. <b>Gross, 401(k), ESPP, HSA:</b> your ${esc(info.paystub.pay_date || "")} pay stub.</p>
    <p><b>Spending</b> (net of reimbursements; transfers and card payments excluded${info.spending_rent ? "; <b>rent left out</b>" : ""}): ${list(info.spending_months)}.
      ${info.spending_rent ? `Average plus your regular rent of <b>${money(info.spending_rent.amount)}</b> (planned payment from ${esc(info.spending_rent.from)}) = <b>${money(FU.baseline.inputs.monthly_spending)}</b>/month.` : ""}</p>
    <p><b>Transfers to ${esc(info.brokerage_label)}</b> (net): ${list(info.brokerage_months)}.</p>
    <p><b>Transfers to ${esc(info.retirement_label)}:</b> ${list(info.retirement_months)}.</p>
    <p>Lumpy months (for example a bonus month) push these averages around. Adjust the sliders if they don't reflect your normal habits.</p>
    <p><b>Not included:</b> credit card balances (${fmt0(info.debts_excluded)} now; paid from monthly spending) and loans that aren't connected (e.g. a car loan balance).</p>
    <div class="fu-field" style="margin-top:12px"><div class="fu-label"><span>401(k) limit setting</span><span class="hint">IRS: $24,500 for 2026 (age 50+ can add $8,000)</span></div>
      <div class="fu-inputs"><input type="number" id="fu-limit-val" value="${FU.inputs.k401_limit}" step="100" style="width:120px">
      <input type="number" id="fu-limit-year" value="${info.k401_limit_year}" style="width:80px" aria-label="Year">
      <button class="btn small" id="fu-save-limit">Save as default</button></div></div>`;
}

async function saveLimitSetting() {
  try {
    const val = +$("#fu-limit-val").value, year = +$("#fu-limit-year").value;
    await api("/api/future/settings", { method: "PUT", body: { k401_limit: val, k401_limit_year: year } });
    FU.inputs.k401_limit = val; FU.baseline.info.k401_limit_year = year;
    toast("Saved"); renderControls(); schedule();
  } catch (e) { showError(e); }
}

function fillYearSelect() {
  const sel = $("#fu-year-bucket"); const cur = sel.value || "total";
  sel.innerHTML = '<option value="total">All buckets (total)</option>' + FU.inputs.buckets.map((b) => `<option value="${esc(b.id)}">${esc(b.name)}</option>`).join("");
  sel.value = [...sel.options].some((o) => o.value === cur) ? cur : "total";
}

// ---------- compute ----------
function schedule() { clearTimeout(FU.timer); FU.timer = setTimeout(() => compute().catch(showError), 180); }

async function compute() {
  const r = await api("/api/future/project", { method: "POST", body: { inputs: FU.inputs } });
  FU.result = r;
  renderResults();
}

function renderResults() {
  const { adjusted: a, baseline: h } = FU.result;
  const s = a.summary, real = s.real_dollars;
  const endDate = new Date(a.dates[a.dates.length - 1]);
  const endLabel = endDate.toLocaleString("en-US", { month: "short", year: "numeric" });
  const diff = s.end_total - h.summary.end_total;

  $("#fu-warnings").innerHTML = a.warnings.map((w) => `<div class="alert ${w.type === "shortfall" || w.type === "overspending" || w.type === "checking_negative" ? "bad" : "warn"}">${esc(w.message)}</div>`).join("");
  $("#fu-kpis").innerHTML = [
    [`Projected total · ${endLabel}`, fmt0(s.end_total), `range ${fmt0(s.end_low)} – ${fmt0(s.end_high)}${real ? " · today's $" : ""}`],
    ["vs. current habits", `<span class="${diff >= 0 ? "diff-pos" : "diff-neg"}">${diff >= 0 ? "+" : "−"}${fmt0(Math.abs(diff))}</span>`, `habits reach ${fmt0(h.summary.end_total)}`],
    ["You put in", fmt0(s.contributed), s.employer_contributed ? `incl. ${fmt0(s.employer_contributed)} employer match` : "contributions + money left over"],
    ["Growth & interest", fmt0(s.growth), real ? "after inflation" : "earned on your money"],
    ["Starting today", fmt0(s.start_total), "sum of all buckets"],
  ].map(([l, v, n]) => `<div class="kpi"><div class="label">${l}</div><div class="value">${v}</div><div class="note">${n}</div></div>`).join("");

  // Chart
  const x = a.dates;
  const traces = [];
  const stacked = FU.mode === "stacked";
  a.buckets.forEach((b, n) => {
    traces.push({ x, y: b.balances, name: b.name, type: "scatter", mode: "lines",
      line: { width: stacked ? 0.5 : 2, color: BUCKET_COLORS[n % BUCKET_COLORS.length] },
      ...(stacked ? { stackgroup: "one" } : {}), hovertemplate: `${b.name}: %{y:$,.0f}<extra></extra>` });
  });
  traces.push({ x, y: a.total_low, name: "Low case", type: "scatter", mode: "lines", line: { width: 0 }, showlegend: false, hoverinfo: "skip" });
  traces.push({ x, y: a.total_high, name: "Range (low–high)", type: "scatter", mode: "lines", line: { width: 0 }, fill: "tonexty",
    fillcolor: "rgba(120,144,156,0.18)", hovertemplate: "High case: %{y:$,.0f}<extra></extra>" });
  traces.push({ x, y: a.total, name: "Total (your scenario)", type: "scatter", mode: "lines", line: { width: 3, color: cssVar("--accent") },
    hovertemplate: "Total: %{y:$,.0f}<extra></extra>" });
  traces.push({ x, y: h.total, name: "Total (current habits)", type: "scatter", mode: "lines", line: { width: 2, dash: "dash", color: cssVar("--muted") },
    hovertemplate: "Current habits: %{y:$,.0f}<extra></extra>" });
  plot("fu-chart", traces, { hovermode: "x unified", legend: { orientation: "h", y: -0.15 }, margin: { l: 60, r: 10, t: 10, b: 30 },
    yaxis: { tickprefix: "$", tickformat: "~s", gridcolor: cssVar("--border") }, xaxis: { type: "date", gridcolor: cssVar("--border") } });
  $("#fu-chart-title").textContent = `Projected net worth${real ? " (today's dollars)" : ""}`;

  // Comparison
  const row = (label, hv, av, money_ = true) => {
    const d = av - hv;
    const f = money_ ? fmt0 : (v) => v;
    return `<tr><td>${label}</td><td class="num">${f(hv)}</td><td class="num">${f(av)}</td>
      <td class="num ${d >= 0 ? "diff-pos" : "diff-neg"}">${Math.abs(d) < 0.5 ? "—" : (d > 0 ? "+" : "−") + f(Math.abs(d))}</td></tr>`;
  };
  $("#fu-compare").innerHTML = `<table><thead><tr><th></th><th class="num">Current habits</th><th class="num">Your scenario</th><th class="num">Difference</th></tr></thead><tbody>
    ${row(`Total in ${endLabel}`, h.summary.end_total, s.end_total)}
    ${row("Low case", h.summary.end_low, s.end_low)}
    ${row("High case", h.summary.end_high, s.end_high)}
    ${row("You put in", h.summary.contributed, s.contributed)}
    ${row("Growth & interest", h.summary.growth, s.growth)}
    ${row("Monthly transfers from take-home", h.plan.planned_from_take_home, a.plan.planned_from_take_home)}
    ${row("Left after spending (per month)", h.plan.available, a.plan.available)}
    </tbody></table>`;

  // Bucket cards
  $("#fu-bucket-cards").innerHTML = a.buckets.map((b, n) => `<div class="kpi" style="--bc:${BUCKET_COLORS[n % BUCKET_COLORS.length]}">
    <div class="label">${esc(b.name)}</div><div class="value" style="font-size:19px">${fmt0(b.end)}</div>
    <div class="note">${b.end_low != null ? `range ${fmt0(b.end_low)} – ${fmt0(b.end_high)}<br>` : ""}put in ${fmt0(b.contributed)} · growth ${fmt0(b.growth)}</div></div>`).join("");

  renderTable();
}

function renderTable() {
  if (!FU.result) return;
  const id = $("#fu-year-bucket").value || "total";
  const rows = FU.result.adjusted.yearly[id] || [];
  $("#fu-table").innerHTML = `<table><thead><tr><th>Year</th><th>Ends</th><th class="num">Starting balance</th><th class="num">Contributions</th><th class="num">Growth</th><th class="num">Ending balance</th></tr></thead><tbody>
    ${rows.map((r) => `<tr><td>${r.year}</td><td class="small muted">${esc(r.date)}</td><td class="num">${fmt0(r.start)}</td><td class="num">${fmt0(r.contributions)}</td>
      <td class="num">${fmt0(r.growth)}</td><td class="num"><b>${fmt0(r.end)}</b></td></tr>`).join("")}</tbody></table>
    ${id === "checking" || id === "total" ? '<p class="small muted">For checking, "contributions" is what was left over each year after spending and transfers (it can be negative).</p>' : ""}`;
}

// ---------- scenarios ----------
function fillScenarioSelect() {
  const sel = $("#fu-scenario");
  sel.innerHTML = `<option value="habits">My current habits (from your data)</option>` +
    FU.scenarios.map((s) => `<option value="${s.id}">${esc(s.name)}</option>`).join("");
  sel.value = String(FU.current);
  $("#fu-delete").hidden = FU.current === "habits";
}

function freshen(inputs) {
  // Saved scenarios keep your choices, but today's date, payday and linked balances come from current data.
  const base = FU.baseline.inputs;
  for (const k of ["start_date", "next_payday", "k401_ytd"]) inputs[k] = base[k];
  // Older scenarios stored the 401(k) match at the scenario level; it now lives on the 401(k) bucket.
  const k401 = inputs.buckets.find((b) => b.kind === "401k");
  if (k401 && inputs.employer_match_pct && !k401.employer_amount) {
    k401.employer_amount = inputs.employer_match_pct; k401.employer_basis = "pct_salary";
  }
  inputs.employer_match_pct = 0;
  for (const b of inputs.buckets) {
    const live = base.buckets.find((x) => x.id === b.linked);
    if (live && b.start_from_data) b.start = live.start;
    if (live && b.ytd !== undefined) b.ytd = live.ytd;
    if (live && b.employer_amount === undefined) { b.employer_amount = live.employer_amount; b.employer_basis = live.employer_basis; }
  }
  // Buckets added to your data since the scenario was saved (e.g. a new account) aren't added automatically.
  return inputs;
}

function loadScenario(value) {
  FU.current = value === "habits" ? "habits" : +value;
  FU.inputs = value === "habits" ? clone(FU.baseline.inputs) : freshen(clone(FU.scenarios.find((s) => s.id === +value).inputs));
  FU.dirty = false; $("#fu-status").textContent = "";
  fillScenarioSelect(); renderControls(); schedule();
}

$("#fu-scenario").onchange = (e) => {
  if (FU.dirty && !confirm("Discard your unsaved changes?")) { e.target.value = String(FU.current); return; }
  loadScenario(e.target.value);
};
$("#fu-reset").onclick = () => { if (!FU.dirty || confirm("Reset everything to your current habits?")) loadScenario("habits"); };
$("#fu-save").onclick = () => {
  const curName = FU.current === "habits" ? "" : (FU.scenarios.find((s) => s.id === FU.current)?.name || "");
  openDialog("Save scenario", `<label>Name<input id="fu-name" value="${esc(curName)}" placeholder="e.g. Max 401(k) + HYSA" size="30"></label>
    <p class="small muted">Saving with an existing name replaces it. Starting balances update automatically from your accounts when you load it later.</p>`,
  [{ label: "Cancel" }, { label: "Save", cls: "primary", onClick: async () => {
    const name = $("#fu-name").value.trim(); if (!name) { toast("Type a name"); return false; }
    const r = await api("/api/future/scenarios", { method: "POST", body: { name, inputs: FU.inputs } });
    FU.scenarios = await api("/api/future/scenarios"); FU.current = r.id; FU.dirty = false;
    $("#fu-status").textContent = "Saved"; fillScenarioSelect(); toast(`Saved "${name}"`);
  } }]);
};
$("#fu-delete").onclick = async () => {
  const s = FU.scenarios.find((x) => x.id === FU.current); if (!s || !confirm(`Delete scenario "${s.name}"?`)) return;
  await api(`/api/future/scenarios/${s.id}`, { method: "DELETE" });
  FU.scenarios = await api("/api/future/scenarios"); loadScenario("habits");
};
$("#fu-add-lump").onclick = () => {
  const hysa = FU.inputs.buckets.find((b) => b.kind === "hysa") || FU.inputs.buckets[0];
  FU.inputs.lump_sums.push({ bucket_id: hysa.id, amount: 5000, from_checking: true });
  markDirty(); renderControls(); schedule();
};
$("#fu-year-bucket").onchange = renderTable;
$("#fu-chart-mode").addEventListener("click", (e) => {
  const m = e.target.dataset?.mode; if (!m) return;
  FU.mode = m; $$("#fu-chart-mode button").forEach((b) => b.classList.toggle("on", b.dataset.mode === m));
  if (FU.result) renderResults();
});

loaders.future = async () => {
  try {
    if (!FU.baseline) {
      [FU.baseline, FU.scenarios] = await Promise.all([api("/api/future/baseline"), api("/api/future/scenarios")]);
      FU.inputs = clone(FU.baseline.inputs);
      fillScenarioSelect(); renderControls();
    }
    await compute();
  } catch (e) {
    const restart = /not found/i.test(e.message);
    $("#fu-warnings").innerHTML = `<div class="alert bad">The projection couldn't load: ${esc(e.message)}.
      ${restart ? "The app is probably still running older code. Close the black window and double-click start.bat, then reload this page." : ""}</div>`;
    throw e;
  }
};
