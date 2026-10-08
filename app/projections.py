"""Future projections: pure math, no database, no network.

Model (simulated month by month from today):
  * Each bucket grows at its annual rate, applied monthly as an effective rate:
    monthly = (1 + annual)^(1/12) - 1. So "3.8% APY" means exactly +3.8% after
    12 months with no deposits ($10,000 -> $10,380.00).
  * Growth for a month is earned on the balance at the start of that month;
    deposits made during the month start earning the next month.
  * Per-paycheck contributions follow the real biweekly calendar (a paycheck
    every 14 days from your next payday), so some months get 3 paychecks.
    Monthly contributions happen once a month.
  * Money comes from three places:
      - "pretax": out of your paycheck before tax (401(k), HSA). Raising it lowers
        take-home by the amount times (1 - your marginal tax rate).
      - "posttax": out of your paycheck after tax (ESPP). Lowers take-home 1:1.
      - "take_home": you move it yourself from take-home pay (brokerage, HYSA...).
  * Whatever is left of take-home pay + other income after spending and the
    take-home contributions stays in the checking bucket. If that would push
    checking below $0, that month's take-home transfers are trimmed (you can't
    move money you don't have) and a warning reports the total skipped. Only
    spending above take-home can make checking negative.
  * Employer contributions per bucket: a % of salary (401(k)), a % of what you
    put in (ASPP match) or a fixed $ per paycheck (HSA). They're added on top of
    your own contribution and don't come out of your pay.
  * A bucket can have an annual limit (IRS limits for 401(k), HSA...). Going over
    it triggers a warning; with "stop at limit" contributions pause until January,
    as payroll does.
  * Salary growth steps up once a year. If "contributions follow salary" is on,
    contributions scale with salary too. Spending can rise with inflation.
  * "Today's dollars" divides each month's value by cumulative inflation.
"""
from __future__ import annotations

import copy
import math
from calendar import monthrange
from datetime import date, timedelta

STOCK_KINDS = {"brokerage", "401k"}


# ---------------- small helpers ----------------

def monthly_rate(annual_pct: float) -> float:
    """Effective monthly rate equivalent to an annual percentage yield."""
    return (1 + annual_pct / 100.0) ** (1 / 12) - 1


def add_months(d: date, n: int) -> date:
    y, m = divmod(d.month - 1 + n, 12)
    y += d.year
    m += 1
    return date(y, m, min(d.day, monthrange(y, m)[1]))


def _ceil_div(a: int, b: int) -> int:
    return -((-a) // b)


def paydays_in(anchor: date, start: date, end: date) -> int:
    """How many biweekly paydays (anchor + 14k) fall in [start, end)."""
    return _ceil_div((end - anchor).days, 14) - _ceil_div((start - anchor).days, 14)


def next_payday(last_payday: date, today: date) -> date:
    """First payday on or after today, given any past payday."""
    d = last_payday
    if d >= today:
        return d
    k = _ceil_div((today - d).days, 14)
    return d + timedelta(days=14 * k)


# ---------------- inputs ----------------

DEFAULTS = {
    "horizon_years": 10,
    "salary_growth_pct": 0.0,
    "inflation_pct": 3.0,
    "show_real": False,
    "checking_floor": True,   # trim transfers from take-home rather than letting checking go negative
    "contributions_follow_salary": False,
    "spending_follows_inflation": True,
    "range_pp": 3.0,
    "gross_per_paycheck": 0.0,
    "take_home_per_paycheck": 0.0,
    "monthly_spending": 0.0,
    "other_monthly_income": 0.0,
    "marginal_tax_pct": 30.0,
    "employer_match_pct": 0.0,
    "k401_limit": 24500.0,
    "k401_ytd": 0.0,
    "next_payday": None,      # ISO date; defaults to start date
    "start_date": None,       # ISO date; defaults to today
    "buckets": [],
    "lump_sums": [],
}

BUCKET_DEFAULTS = {
    "id": None, "name": "Bucket", "kind": "custom", "start": 0.0, "contribution": 0.0,
    "frequency": "monthly", "rate_pct": 0.0, "funding": "take_home", "stock": False,
    "base_contribution": 0.0,
    "annual_limit": None,     # e.g. IRS limit; your own contributions per calendar year
    "stop_at_limit": False,   # True: contributions stop for the year at the limit (as payroll does)
    "ytd": 0.0,               # already contributed this calendar year (counts toward the limit)
    "employer_amount": 0.0,   # what your employer adds (see employer_basis)
    "employer_basis": "pct_salary",  # pct_salary | pct_contribution | per_paycheck
}


def normalize(inp: dict) -> dict:
    out = copy.deepcopy(DEFAULTS)
    out.update({k: v for k, v in (inp or {}).items() if v is not None})
    out["horizon_years"] = max(1, min(40, int(out["horizon_years"])))
    buckets = []
    for i, b in enumerate(out["buckets"]):
        nb = dict(BUCKET_DEFAULTS)
        nb.update({k: v for k, v in b.items() if v is not None})
        nb["id"] = str(nb["id"] or f"b{i}")
        if nb["employer_basis"] not in ("pct_salary", "pct_contribution", "per_paycheck"):
            nb["employer_basis"] = "pct_salary"
        for f in ("start", "contribution", "rate_pct", "base_contribution", "ytd", "employer_amount"):
            nb[f] = float(nb[f] or 0)
        if nb["kind"] in STOCK_KINDS:
            nb["stock"] = True
        if nb["kind"] == "401k":  # the 401(k) limit and YTD come from the scenario-level settings
            nb["annual_limit"] = float(out.get("k401_limit") or 0) or None
            nb["ytd"] = float(out.get("k401_ytd") or 0)
        nb["annual_limit"] = float(nb["annual_limit"]) if nb["annual_limit"] else None
        nb["stop_at_limit"] = bool(nb["stop_at_limit"])
        if nb["kind"] == "checking":
            nb["contribution"] = 0.0  # checking receives whatever is left over
        buckets.append(nb)
    if not any(b["kind"] == "checking" for b in buckets):
        buckets.insert(0, dict(BUCKET_DEFAULTS, id="checking", name="Checking", kind="checking"))
    out["buckets"] = buckets
    for f in ("salary_growth_pct", "inflation_pct", "range_pp", "gross_per_paycheck", "take_home_per_paycheck",
              "monthly_spending", "other_monthly_income", "marginal_tax_pct", "employer_match_pct",
              "k401_limit", "k401_ytd"):
        out[f] = float(out[f] or 0)
    return out


# ---------------- employer contributions ----------------

def _employer_basis(inp: dict, b: dict):
    """(amount, basis) for what your employer adds to this bucket.
    basis: 'pct_salary' (e.g. 401(k): 7.5% of pay), 'pct_contribution' (e.g. ASPP: 15% of what
    you put in) or 'per_paycheck' (a fixed $ amount, e.g. HSA)."""
    amt, basis = b["employer_amount"], b["employer_basis"]
    if not amt and b["kind"] == "401k" and inp["employer_match_pct"]:
        amt, basis = inp["employer_match_pct"], "pct_salary"  # older saved scenarios
    return amt, basis


def _employer(inp: dict, b: dict, your_contribution: float, n_pay: int, sal: float, cf: float) -> float:
    amt, basis = _employer_basis(inp, b)
    if not amt:
        return 0.0
    if basis == "pct_contribution":
        return your_contribution * amt / 100.0
    if basis == "per_paycheck":
        return amt * cf * n_pay
    return inp["gross_per_paycheck"] * sal * amt / 100.0 * n_pay  # pct_salary


def _employer_counts_toward_limit(b: dict) -> bool:
    # IRS: 401(k) employer money doesn't count toward your $24,500 limit; HSA employer money does.
    return b["kind"] != "401k"


# ---------------- simulation ----------------

def _simulate(inp: dict, rate_shift_pp: float = 0.0) -> dict:
    start = date.fromisoformat(inp["start_date"]) if inp["start_date"] else date.today()
    anchor = date.fromisoformat(inp["next_payday"]) if inp["next_payday"] else start
    months = inp["horizon_years"] * 12
    buckets = inp["buckets"]
    checking = next(b for b in buckets if b["kind"] == "checking")
    mr = inp["marginal_tax_pct"] / 100.0
    g = inp["salary_growth_pct"] / 100.0
    infl = inp["inflation_pct"] / 100.0

    rates = {b["id"]: monthly_rate(b["rate_pct"] + (rate_shift_pp if b["stock"] else 0.0)) for b in buckets}
    bal = {b["id"]: b["start"] for b in buckets}
    external = 0.0
    for ls in inp["lump_sums"]:
        bid, amt = str(ls.get("bucket_id")), float(ls.get("amount") or 0)
        if bid not in bal or not amt:
            continue
        bal[bid] += amt
        if ls.get("from_checking", True) and bid != checking["id"]:
            bal[checking["id"]] -= amt  # moving money you already have
        else:
            external += amt  # new money from outside (e.g. a windfall)

    # Take-home pay before any of the modelled deductions (so changing them moves take-home).
    pre_deduction_th = inp["take_home_per_paycheck"]
    for b in buckets:
        if b["frequency"] == "paycheck" and b["funding"] == "pretax":
            pre_deduction_th += b["base_contribution"] * (1 - mr)
        elif b["frequency"] == "paycheck" and b["funding"] == "posttax":
            pre_deduction_th += b["base_contribution"]

    series = {bid: [v] for bid, v in bal.items()}
    contrib = {bid: [0.0] for bid in bal}
    growth = {bid: [0.0] for bid in bal}
    emp_hist = {bid: [0.0] for bid in bal}
    employer = [0.0]
    dates = [start]
    deflator = [1.0]
    # Your own contributions per calendar year: actually made (capped) and what you planned (uncapped)
    made = {b["id"]: {start.year: b["ytd"]} for b in buckets}
    wanted = {b["id"]: {start.year: b["ytd"]} for b in buckets}
    first_negative_checking = None
    skipped = 0.0
    cid = checking["id"]

    for m in range(1, months + 1):
        ps, pe = add_months(start, m - 1), add_months(start, m)
        yi = (m - 1) // 12
        sal = (1 + g) ** yi
        cf = sal if inp["contributions_follow_salary"] else 1.0
        n_pay = paydays_in(anchor, ps, pe)
        spend = inp["monthly_spending"] * ((1 + infl) ** yi if inp["spending_follows_inflation"] else 1.0)

        take_home = pre_deduction_th * sal * n_pay
        th_funded = 0.0
        month_employer = 0.0
        plan = {}  # bid -> [growth, your contribution, employer contribution]
        for b in buckets:
            bid = b["id"]
            if b["kind"] == "checking":
                continue
            gr = bal[bid] * rates[bid]
            c = b["contribution"] * cf * (n_pay if b["frequency"] == "paycheck" else 1)
            emp = _employer(inp, b, c, n_pay, sal, cf)
            counts = _employer_counts_toward_limit(b)
            wanted[bid][ps.year] = wanted[bid].get(ps.year, 0.0) + c + (emp if counts else 0.0)
            lim = b["annual_limit"]
            if lim and b["stop_at_limit"]:  # contributions pause for the rest of the year, as payroll does
                allowed = max(0.0, lim - made[bid].get(ps.year, 0.0))
                if not counts:
                    c = min(c, allowed)
                elif _employer_basis(inp, b)[1] == "pct_contribution":
                    c = min(c, allowed / (1 + _employer_basis(inp, b)[0] / 100.0))
                else:
                    emp = min(emp, allowed)
                    c = min(c, allowed - emp)
                emp = _employer(inp, b, c, n_pay, sal, cf) if _employer_basis(inp, b)[1] == "pct_contribution" else emp
            if b["funding"] == "pretax":
                take_home -= c * (1 - mr)
            elif b["funding"] == "posttax":
                take_home -= c
            else:
                th_funded += c
            plan[bid] = [gr, c, emp]

        gr_c = bal[cid] * rates[cid]
        c_chk = take_home + inp["other_monthly_income"] - spend - th_funded
        if inp["checking_floor"] and th_funded > 0 and bal[cid] + gr_c + c_chk < 0:
            # You can't transfer money you don't have: trim this month's transfers from take-home.
            cut = min(-(bal[cid] + gr_c + c_chk), th_funded)
            scale = (th_funded - cut) / th_funded
            for b in buckets:
                if b["id"] in plan and b["funding"] == "take_home":
                    plan[b["id"]][1] *= scale
                    if _employer_basis(inp, b)[1] == "pct_contribution":
                        plan[b["id"]][2] *= scale  # a match on money you didn't put in doesn't happen
            c_chk += cut
            skipped += cut

        by_id = {b["id"]: b for b in buckets}
        for bid, (gr, c, emp) in plan.items():
            made[bid][ps.year] = made[bid].get(ps.year, 0.0) + c + (emp if _employer_counts_toward_limit(by_id[bid]) else 0.0)
            month_employer += emp
            emp_hist[bid].append(emp)
            bal[bid] += gr + c + emp
            series[bid].append(bal[bid])
            contrib[bid].append(c + emp)
            growth[bid].append(gr)
        bal[cid] += gr_c + c_chk
        series[cid].append(bal[cid])
        contrib[cid].append(c_chk)
        growth[cid].append(gr_c)
        if bal[cid] < 0 and first_negative_checking is None:
            first_negative_checking = pe
        employer.append(month_employer)
        dates.append(pe)
        deflator.append((1 + infl) ** (m / 12))

    return {"dates": dates, "series": series, "contrib": contrib, "growth": growth, "employer": employer,
            "deflator": deflator, "external": external, "wanted": wanted, "skipped": skipped, "emp_hist": emp_hist,
            "first_negative_checking": first_negative_checking, "start": start, "anchor": anchor}


def _monthly_plan(inp: dict) -> dict:
    """A typical month today (26 paychecks / 12): what's available vs planned."""
    mr = inp["marginal_tax_pct"] / 100.0
    per_month = 26 / 12
    take_home_month = inp["take_home_per_paycheck"] * per_month
    planned = 0.0
    for b in inp["buckets"]:
        if b["kind"] == "checking":
            continue
        n = per_month if b["frequency"] == "paycheck" else 1
        c = b["contribution"] * n
        base = b["base_contribution"] * n if b["frequency"] == "paycheck" else 0.0
        if b["funding"] == "pretax":      # changes vs. today move take-home, softened by tax savings
            take_home_month -= (c - base) * (1 - mr)
        elif b["funding"] == "posttax":
            take_home_month -= c - base
        else:
            planned += c
    available = take_home_month + inp["other_monthly_income"] - inp["monthly_spending"]
    return {"take_home_month": take_home_month, "available": available, "planned_from_take_home": planned,
            "shortfall": max(0.0, planned - available)}


def project(raw_inp: dict) -> dict:
    inp = normalize(raw_inp)
    base = _simulate(inp)
    low = _simulate(inp, -inp["range_pp"])
    high = _simulate(inp, +inp["range_pp"])
    real = inp["show_real"]
    defl = base["deflator"] if real else [1.0] * len(base["deflator"])

    def conv(values):
        return [round(v / d, 2) for v, d in zip(values, defl)]

    ids = [b["id"] for b in inp["buckets"]]
    n = len(base["dates"])

    def total_of(sim):
        return [sum(sim["series"][i][m] for i in ids) for m in range(n)]

    total = total_of(base)
    out_buckets = []
    for b in inp["buckets"]:
        bid = b["id"]
        s = base["series"][bid]
        contributed = sum(c / d for c, d in zip(base["contrib"][bid], defl))
        end = s[-1] / defl[-1]
        start_v = s[0]
        out_buckets.append({
            "id": bid, "name": b["name"], "kind": b["kind"], "stock": b["stock"],
            "balances": conv(s), "start": round(start_v, 2), "end": round(end, 2),
            "contributed": round(contributed, 2), "growth": round(end - start_v - contributed, 2),
            "employer": round(sum(e / d for e, d in zip(base["emp_hist"].get(bid, []), defl)), 2),
            "end_low": round(low["series"][bid][-1] / defl[-1], 2) if b["stock"] else None,
            "end_high": round(high["series"][bid][-1] / defl[-1], 2) if b["stock"] else None,
        })

    # Year-by-year table
    yearly = {}
    for bid in ids + ["total"]:
        rows = []
        for y in range(inp["horizon_years"]):
            m0, m1 = y * 12, (y + 1) * 12
            if bid == "total":
                s0 = sum(base["series"][i][m0] for i in ids) / defl[m0]
                s1 = sum(base["series"][i][m1] for i in ids) / defl[m1]
                c = sum(base["contrib"][i][m] / defl[m] for i in ids for m in range(m0 + 1, m1 + 1))
            else:
                s0 = base["series"][bid][m0] / defl[m0]
                s1 = base["series"][bid][m1] / defl[m1]
                c = sum(base["contrib"][bid][m] / defl[m] for m in range(m0 + 1, m1 + 1))
            rows.append({"year": y + 1, "date": str(base["dates"][m1]), "start": round(s0, 2),
                         "contributions": round(c, 2), "growth": round(s1 - s0 - c, 2), "end": round(s1, 2)})
        yearly[bid] = rows

    start_total = sum(b["start"] for b in inp["buckets"])  # before any lump-sum moves
    end_total = total[-1] / defl[-1]
    contributed_total = sum(c / d for i in ids for c, d in zip(base["contrib"][i], defl))
    employer_total = sum(e / d for e, d in zip(base["employer"], defl))

    # Warnings
    warnings = []
    plan = _monthly_plan(inp)
    if plan["available"] < 0:
        warnings.append({"type": "overspending", "amount": round(-plan["available"], 2),
                         "message": f"Your average spending is ${-plan['available']:,.0f}/month more than your "
                                    "take-home pay plus other income."})
    if plan["shortfall"] > 0.5:
        warnings.append({"type": "shortfall", "amount": round(plan["shortfall"], 2),
                         "message": f"Your planned transfers from take-home pay (${plan['planned_from_take_home']:,.0f}/month) "
                                    f"are ${plan['shortfall']:,.0f}/month more than what's left after average spending "
                                    f"(${max(plan['available'], 0):,.0f}/month). Checking covers the gap until it runs "
                                    "low; after that the projection trims your transfers."})
    if base["first_negative_checking"]:
        warnings.append({"type": "checking_negative", "date": str(base["first_negative_checking"]),
                         "message": f"Checking would drop below $0 around {base['first_negative_checking']:%B %Y}."})
    if base["skipped"] > 0.5:
        warnings.append({"type": "transfers_trimmed", "amount": round(base["skipped"], 2),
                         "message": f"${base['skipped']:,.0f} of planned transfers from take-home pay were skipped over "
                                    "the whole period because checking would have gone below $0."})
    k401_by_year = {}
    for b in inp["buckets"]:
        if not b["annual_limit"]:
            continue
        yearly_wanted = base["wanted"][b["id"]]
        if b["kind"] == "401k":
            k401_by_year = yearly_wanted
        over = {y: v for y, v in sorted(yearly_wanted.items()) if v > b["annual_limit"] + 0.5}
        if not over:
            continue
        y0 = next(iter(over))
        what = ("The projection stops contributions at the limit each year, as payroll does."
                if b["stop_at_limit"] else "Payroll normally stops contributions at the limit.")
        warnings.append({"type": "k401_limit" if b["kind"] == "401k" else "annual_limit", "bucket": b["name"],
                         "years": [str(y) for y in over],
                         "message": f"{b['name']} contributions would be ${over[y0]:,.0f} in {y0}, over the "
                                    f"${b['annual_limit']:,.0f} annual limit. {what} (Limits usually rise a little "
                                    "each year; the projection keeps today's limit.)"})

    return {
        "dates": [str(d) for d in base["dates"]],
        "total": conv(total),
        "total_low": conv(total_of(low)),
        "total_high": conv(total_of(high)),
        "buckets": out_buckets,
        "yearly": yearly,
        "summary": {
            "start_total": round(start_total, 2),
            "end_total": round(end_total, 2),
            "end_low": round(total_of(low)[-1] / defl[-1], 2),
            "end_high": round(total_of(high)[-1] / defl[-1], 2),
            "contributed": round(contributed_total + base["external"], 2),
            "employer_contributed": round(employer_total, 2),
            "growth": round(end_total - start_total - contributed_total - base["external"], 2),
            "real_dollars": real,
            "first_payday": str(base["anchor"]),
            "transfers_skipped": round(base["skipped"], 2),
        },
        "plan": {k: round(v, 2) for k, v in plan.items()},
        "k401_by_year": {str(k): round(v, 2) for k, v in sorted(k401_by_year.items())},
        "warnings": warnings,
    }
