"""All the numbers the dashboard shows. Transfers between your own accounts and
credit card payments are excluded from spending and income everywhere."""
import calendar
import re
import statistics
from collections import defaultdict
from datetime import date, timedelta

from .categories import category_kinds
from .db import get_setting
from .paystubs import FIELD_NAMES, FIELDS
from .sync import signed_balance

ACCOUNT_GROUPS = {"depository": "Cash", "credit": "Credit cards", "investment": "Investments",
                  "loan": "Loans", "other": "Other"}


# ---------------- Periods ----------------

def resolve_period(period: str):
    """'2026-09' (month), 'ytd', 'year:2025', 'last12', or 'YYYY-MM-DD..YYYY-MM-DD'."""
    t = date.today()
    period = (period or "ytd").strip()
    if period == "ytd":
        return date(t.year, 1, 1), t, f"{t.year} year-to-date"
    if period.startswith("year:"):
        y = int(period[5:])
        return date(y, 1, 1), min(date(y, 12, 31), t), str(y)
    if period == "last12":
        return (t.replace(day=1) - timedelta(days=335)).replace(day=1), t, "Last 12 months"
    if ".." in period:
        a, b = period.split("..")
        return date.fromisoformat(a), date.fromisoformat(b), f"{a} to {b}"
    if re.fullmatch(r"\d{4}-\d{2}", period):
        y, m = map(int, period.split("-"))
        return date(y, m, 1), date(y, m, calendar.monthrange(y, m)[1]), date(y, m, 1).strftime("%B %Y")
    raise ValueError("Unknown period")


def _acct_label(a):
    name = a["display_name"] or a["name"] or "Account"
    return f"{name} ••{a['mask']}" if a["mask"] else name


def account_labels(conn):
    return {a["account_id"]: _acct_label(a) for a in conn.execute("SELECT * FROM accounts")}


# ---------------- Accounts / net worth ----------------

def accounts_overview(conn):
    liab = {r["account_id"]: dict(r) for r in conn.execute("SELECT * FROM liabilities")}
    items = {r["item_id"]: dict(r) for r in conn.execute(
        "SELECT item_id,status,error_code,error_message,last_sync_at,consent_expiration FROM items")}
    groups = defaultdict(list)
    assets = liabilities = 0.0
    for a in conn.execute("SELECT * FROM accounts WHERE hidden=0 ORDER BY type, institution_name, name"):
        a = dict(a)
        a["label"] = _acct_label(a)
        a["signed_balance"] = signed_balance(a)
        a["liability"] = liab.get(a["account_id"])
        a["item"] = items.get(a["item_id"])
        groups[ACCOUNT_GROUPS.get(a["type"], "Other")].append(a)
        if a["include_in_networth"]:
            if a["signed_balance"] >= 0:
                assets += a["signed_balance"]
            else:
                liabilities += -a["signed_balance"]
    order = ["Cash", "Investments", "Credit cards", "Loans", "Other"]
    return {
        "groups": [{"name": g, "accounts": groups[g], "total": round(sum(x["signed_balance"] for x in groups[g]), 2)}
                   for g in order if groups.get(g)],
        "assets": round(assets, 2), "liabilities": round(liabilities, 2), "net_worth": round(assets - liabilities, 2),
    }


def networth_history(conn):
    return [dict(r) for r in conn.execute("SELECT * FROM networth_snapshots ORDER BY date")]


# ---------------- Transactions ----------------

def query_transactions(conn, start=None, end=None, category=None, account_id=None, search=None,
                       kind=None, review=False, limit=1000):
    kinds = category_kinds(conn)
    sql = "SELECT * FROM transactions WHERE 1=1"
    args = []
    if start:
        sql += " AND date>=?"
        args.append(str(start))
    if end:
        sql += " AND date<=?"
        args.append(str(end))
    if category:
        sql += " AND category=?"
        args.append(category)
    if account_id:
        sql += " AND account_id=?"
        args.append(account_id)
    if search:
        sql += " AND (LOWER(name) LIKE ? OR LOWER(merchant_name) LIKE ? OR LOWER(original_description) LIKE ?)"
        args += [f"%{search.lower()}%"] * 3
    if review:
        sql += " AND category_source IN ('plaid_low','default')"
    sql += " ORDER BY date DESC, transaction_id LIMIT ?"
    args.append(limit)
    labels = account_labels(conn)
    out = []
    for t in conn.execute(sql, args):
        t = dict(t)
        t["kind"] = kinds.get(t["category"], "spending")
        if kind and t["kind"] != kind:
            continue
        t["account_label"] = labels.get(t["account_id"], "?")
        out.append(t)
    return out


def _txns_in(conn, start, end):
    kinds = category_kinds(conn)
    rows = []
    for t in conn.execute("SELECT * FROM transactions WHERE date>=? AND date<=?", (str(start), str(end))):
        t = dict(t)
        t["kind"] = kinds.get(t["category"], "spending")
        rows.append(t)
    return rows


# ---------------- Spending ----------------

def spending(conn, start, end):
    labels = account_labels(conn)
    colors = {r["name"]: r["color"] for r in conn.execute("SELECT name,color FROM categories")}
    by_cat = defaultdict(float)
    by_cat_acct = defaultdict(lambda: defaultdict(float))
    counts = defaultdict(int)
    for t in _txns_in(conn, start, end):
        if t["kind"] != "spending":
            continue
        by_cat[t["category"]] += t["amount"]
        by_cat_acct[t["category"]][labels.get(t["account_id"], "?")] += t["amount"]
        counts[t["category"]] += 1
    total = sum(v for v in by_cat.values())
    cats = sorted(by_cat.items(), key=lambda kv: -kv[1])
    accounts = sorted({a for c in by_cat_acct.values() for a in c})
    return {
        "total": round(total, 2),
        "categories": [{"category": c, "amount": round(v, 2), "count": counts[c], "color": colors.get(c),
                        "pct": round(100 * v / total, 1) if total else 0,
                        "by_account": {a: round(x, 2) for a, x in by_cat_acct[c].items()}} for c, v in cats],
        "accounts": accounts,
    }


def monthly_summary(conn, months=12):
    """Per month: paychecks, other income, spending, investing, net. 3-paycheck months flagged."""
    t = date.today()
    start = (t.replace(day=1) - timedelta(days=31 * (months - 1))).replace(day=1)
    data = defaultdict(lambda: {"paychecks": 0.0, "paycheck_count": 0, "other_income": 0.0, "spending": 0.0,
                                "investing": 0.0})
    for x in _txns_in(conn, start, t):
        m = x["date"][:7]
        if x["category"] == "Paycheck" and x["amount"] < 0:
            data[m]["paychecks"] += -x["amount"]
            data[m]["paycheck_count"] += 1
        elif x["kind"] == "income":
            data[m]["other_income"] += -x["amount"]
        elif x["kind"] == "spending":
            data[m]["spending"] += x["amount"]
        elif x["kind"] == "investing":
            data[m]["investing"] += x["amount"]
    out = []
    cur = start
    while cur <= t:
        m = cur.strftime("%Y-%m")
        d = data[m]
        d = {k: round(v, 2) if isinstance(v, float) else v for k, v in d.items()}
        d["month"] = m
        d["net"] = round(d["paychecks"] + d["other_income"] - d["spending"] - d["investing"], 2)
        out.append(d)
        cur = (cur.replace(day=28) + timedelta(days=4)).replace(day=1)
    while out and not any(out[0][k] for k in ("paychecks", "other_income", "spending", "investing")):
        out.pop(0)  # drop months before your data starts
    pay = [x for x in conn.execute(
        "SELECT -amount AS a FROM transactions WHERE category='Paycheck' AND amount<0 AND date>=?",
        (str(t - timedelta(days=120)),))]
    typical = statistics.median([r["a"] for r in pay]) if pay else None
    return {"months": out, "typical_paycheck": round(typical, 2) if typical else None,
            "avg_monthly_from_typical": round(typical * 26 / 12, 2) if typical else None}


# ---------------- Payroll (pay stubs + paycheck deposits) ----------------

def _stubs(conn):
    out = []
    for s in conn.execute("SELECT * FROM paystubs WHERE pay_date IS NOT NULL ORDER BY pay_date"):
        s = dict(s)
        s["f"] = {r["field"]: (r["current"] or 0, r["ytd"] or 0) for r in conn.execute(
            "SELECT field,current,ytd FROM paystub_fields WHERE paystub_id=?", (s["id"],))}
        out.append(s)
    return out


def payroll_breakdown(conn, start, end, use_ytd=False):
    """Gross -> taxes -> deductions -> net for paychecks in [start, end].

    - A paycheck with its own pay stub uses the stub's real numbers.
    - A paycheck deposit with no stub, whose amount is within 5% of a stub's net
      pay, is ESTIMATED from the nearest stub (flagged in the app).
    - Other paycheck deposits count as take-home with unknown gross.
    - Year-to-date uses the latest stub's YTD column (includes bonuses and any
      paychecks before your first imported stub)."""
    stubs = _stubs(conn)
    keys = [f for f, _ in FIELDS]
    tot = {k: 0.0 for k in keys}
    notes = []
    deposits = [dict(r) for r in conn.execute(
        "SELECT transaction_id,date,-amount AS amount FROM transactions WHERE category='Paycheck' AND amount<0 "
        "AND date>=? AND date<=? ORDER BY date", (str(start), str(end)))]
    bank_total = sum(d["amount"] for d in deposits)
    n_actual = n_est = 0
    unmatched = 0.0
    stubs_in = [s for s in stubs if str(start) <= s["pay_date"] <= str(end)]
    if use_ytd and stubs_in:
        latest = stubs_in[-1]
        for k in keys:
            tot[k] += latest["f"].get(k, (0, 0))[1]
        n_actual = len(stubs_in)
        cutoff = (date.fromisoformat(latest["pay_date"]) + timedelta(days=3)).isoformat()
        before = sum(d["amount"] for d in deposits if d["date"] <= cutoff)
        after = [d for d in deposits if d["date"] > cutoff]
        extra = before - latest["f"].get("net_pay", (0, 0))[1]
        if extra > 50:
            unmatched += extra
            notes.append(f"${extra:,.2f} of paycheck deposits this year aren't covered by your pay stub's "
                         "YTD totals (for example, a previous job). Counted as take-home only.")
        elif extra < -50:
            notes.append(f"Your pay stub's YTD net pay is ${-extra:,.2f} more than the paycheck deposits found "
                         "in your bank (part of a paycheck may go to another account, or bank history is incomplete).")
        stub_pool = stubs
    else:
        after = deposits
        for s in stubs_in:
            for k in keys:
                tot[k] += s["f"].get(k, (0, 0))[0]
            n_actual += 1
        stub_pool = stubs
        # Remove deposits that are matched to a stub in range
        matched = set()
        for s in stubs_in:
            sd = date.fromisoformat(s["pay_date"])
            for d in after:
                if d["transaction_id"] not in matched and abs((date.fromisoformat(d["date"]) - sd).days) <= 4:
                    matched.add(d["transaction_id"])
                    break
        after = [d for d in after if d["transaction_id"] not in matched]
        after = _apply_prestub(conn, stubs, after, tot, keys, notes)
    for d in after:
        dd = date.fromisoformat(d["date"])
        near = min(stub_pool, key=lambda s: abs((date.fromisoformat(s["pay_date"]) - dd).days), default=None)
        if near and near["f"].get("net_pay", (0, 0))[0] and \
                abs(d["amount"] - near["f"]["net_pay"][0]) <= 0.05 * near["f"]["net_pay"][0]:
            for k in keys:
                tot[k] += near["f"].get(k, (0, 0))[0]
            # Use what actually arrived as take-home; put the small difference in deductions (or gross).
            diff = near["f"]["net_pay"][0] - d["amount"]
            tot["net_pay"] -= diff
            if diff > 0:
                tot["other_posttax"] += diff
            else:
                tot["gross"] += -diff
            n_est += 1
        else:
            unmatched += d["amount"]
    if n_est:
        notes.append(f"{n_est} paycheck(s) had no pay stub; their taxes and deductions are estimated from your "
                     "closest pay stub. Add those stubs for exact numbers.")
    if unmatched and not use_ytd:
        notes.append(f"${unmatched:,.2f} of paycheck deposits have no matching pay stub. They're counted as "
                     "take-home only (gross pay and taxes unknown).")
    return {"fields": {k: round(v, 2) for k, v in tot.items()}, "stubs_used": n_actual, "estimated": n_est,
            "unmatched_takehome": round(unmatched, 2), "bank_paycheck_total": round(bank_total, 2),
            "paycheck_count": len(deposits), "notes": notes}


def _apply_prestub(conn, stubs, deposits, tot, keys, notes):
    """Paycheck deposits from before your first pay stub of a year (for example a
    signing bonus plus earlier paychecks) are exactly what that stub's YTD column
    minus its own period covers. If those deposits add up to that net pay (within
    2%), break them down using it. Returns the deposits still unexplained."""
    remaining = list(deposits)
    for y in sorted({s["pay_date"][:4] for s in stubs}):
        s0 = min((s for s in stubs if s["pay_date"][:4] == y), key=lambda s: s["pay_date"])
        prior = {k: s0["f"].get(k, (0, 0))[1] - s0["f"].get(k, (0, 0))[0] for k in keys}
        if prior["net_pay"] <= 1:
            continue
        cutoff = (date.fromisoformat(s0["pay_date"]) - timedelta(days=4)).isoformat()
        employer = " ".join(re.sub(r"[^a-z ]", " ", (s0["employer"] or "").lower()).split()[:2])
        window = [dict(r) for r in conn.execute(
            "SELECT transaction_id, date, -amount AS amount, name FROM transactions WHERE category='Paycheck' "
            "AND amount<0 AND date>=? AND date<?", (f"{y}-01-01", cutoff))]
        window = [w for w in window if employer and employer in (w["name"] or "").lower()]
        total = sum(w["amount"] for w in window)
        if not total or abs(total - prior["net_pay"]) > 0.02 * prior["net_pay"]:
            continue
        ids = {w["transaction_id"] for w in window}
        used = [d for d in remaining if d["transaction_id"] in ids]
        for d in used:
            share = d["amount"] / total
            for k in keys:
                tot[k] += prior[k] * share
        if used:
            notes.append(f"Deposit(s) on {', '.join(d['date'] for d in used)} include pay from before your first "
                         f"{y} pay stub (e.g. a bonus); broken down using that stub's year-to-date totals.")
        remaining = [d for d in remaining if d["transaction_id"] not in ids]
    return remaining


# ---------------- Cash flow ----------------

def cashflow(conn, period="ytd"):
    start, end, label = resolve_period(period)
    use_ytd = period == "ytd" or (period.startswith("year:") and start.year == date.today().year)
    pr = payroll_breakdown(conn, start, end, use_ytd=use_ytd)
    f = pr["fields"]
    gross = f["gross"]
    taxes = {k: f[k] for k in ("federal_tax", "state_tax", "local_tax", "social_security", "medicare", "other_taxes")}
    tax_total = sum(taxes.values())
    k401 = f["k401_pretax"] + f["k401_roth"]
    take_home = f["net_pay"] + pr["unmatched_takehome"]

    other_income = 0.0
    other_by_cat = defaultdict(float)
    spend = defaultdict(float)
    invested_brokerage = 0.0
    for t in _txns_in(conn, start, end):
        if t["kind"] == "income" and t["category"] != "Paycheck":
            other_income += -t["amount"]
            other_by_cat[t["category"]] += -t["amount"]
        elif t["kind"] == "spending":
            spend[t["category"]] += t["amount"]
        elif t["kind"] == "investing":
            invested_brokerage += t["amount"]
    spend_total = sum(spend.values())
    available = take_home + other_income
    leftover = available - spend_total - invested_brokerage

    inc_espp = get_setting(conn, "invest_include_espp", "1") == "1"
    inc_hsa = get_setting(conn, "invest_include_hsa", "0") == "1"
    inc_match = get_setting(conn, "invest_include_employer_match", "0") == "1"
    invest_parts = {"401(k) (your contributions)": k401, "Brokerage transfers (net)": invested_brokerage}
    if inc_espp:
        invest_parts["ESPP / ASPP"] = f["espp"]
    if inc_hsa:
        invest_parts["HSA"] = f["hsa"]
    if inc_match:
        invest_parts["Employer 401(k) match"] = f["employer_401k"]
    invest_total = sum(invest_parts.values())

    def pct(a, b):
        return round(100 * a / b, 1) if b else None

    sankey = _sankey(gross, taxes, k401, f, take_home - pr["unmatched_takehome"], pr["unmatched_takehome"],
                     other_by_cat, spend, invested_brokerage, leftover)
    return {
        "period": period, "label": label, "start": str(start), "end": str(end),
        "gross": round(gross, 2), "taxes": {k: round(v, 2) for k, v in taxes.items()}, "tax_total": round(tax_total, 2),
        "k401": round(k401, 2), "espp": f["espp"], "hsa": f["hsa"], "other_pretax": f["other_pretax"],
        "other_posttax": f["other_posttax"], "employer_401k": f["employer_401k"],
        "take_home": round(take_home, 2), "other_income": round(other_income, 2),
        "spending_total": round(spend_total, 2),
        "spending": [{"category": c, "amount": round(v, 2), "pct_take_home": pct(v, take_home),
                      "pct_available": pct(v, available)}
                     for c, v in sorted(spend.items(), key=lambda kv: -kv[1])],
        "invested_brokerage": round(invested_brokerage, 2), "leftover": round(leftover, 2),
        "investing": {"parts": {k: round(v, 2) for k, v in invest_parts.items()}, "total": round(invest_total, 2),
                      "pct_gross": pct(invest_total, gross), "pct_take_home": pct(invest_total, take_home),
                      "includes": {"espp": inc_espp, "hsa": inc_hsa, "employer_match": inc_match}},
        "payroll": pr, "sankey": sankey,
    }


def _sankey(gross, taxes, k401, f, stub_net, unmatched, other_by_cat, spend, invested, leftover):
    nodes, links = [], []

    def n(name):
        if name not in nodes:
            nodes.append(name)
        return nodes.index(name)

    def link(a, b, v):
        if v and v > 0.005:
            links.append({"source": n(a), "target": n(b), "value": round(v, 2)})

    if gross > 0:
        link("Gross pay", "Taxes", sum(taxes.values()))  # breakdown is in the paycheck table
        link("Gross pay", "401(k)", k401)
        link("Gross pay", "ESPP / ASPP", f["espp"])
        link("Gross pay", "HSA", f["hsa"])
        link("Gross pay", "Benefits & other deductions", f["other_pretax"] + f["other_posttax"])
        link("Gross pay", "Take-home pay", stub_net)
        link("Take-home pay", "Money available", stub_net)
    link("Paychecks (no pay stub)", "Money available", unmatched)
    for c, v in sorted(other_by_cat.items(), key=lambda kv: -kv[1]):
        link(c, "Money available", v)  # e.g. Family support, Other Income, Interest
    if invested < 0:
        link("Withdrawn from investments", "Money available", -invested)
    if leftover < 0:
        link("From savings", "Money available", -leftover)
    for c, v in spend.items():
        if v < 0:  # e.g. Reimbursements: money paid back to you offsets spending
            link(c, "Money available", -v)
    top = sorted(((c, v) for c, v in spend.items() if v > 0), key=lambda kv: -kv[1])
    for c, v in top[:12]:
        link("Money available", c, v)
    link("Money available", "Other spending", sum(v for _, v in top[12:]))
    if invested > 0:
        link("Money available", "Invested (brokerage)", invested)
    if leftover > 0:
        link("Money available", "Left over", leftover)
    return {"nodes": nodes, "links": links}


# ---------------- Recurring charges ----------------

EVERYDAY = {"Groceries", "Dining", "Shopping", "Gas", "Transportation", "Cash & ATM", "Payments to people",
            "Uncategorized", "Travel"}
PERIODS = [("weekly", 7, 2), ("every 2 weeks", 14, 3), ("monthly", 30.4, 4), ("quarterly", 91, 10),
           ("yearly", 365, 20)]


def _merchant_key(t):
    if t["merchant_name"]:
        return t["merchant_name"].strip().lower()
    s = (t["name"] or "").lower()
    s = re.split(r"\bdes:|\bid:|\bindn:|\bco id:|#", s)[0]
    s = re.sub(r"[\d*]+", " ", s)
    s = re.sub(r"[^a-z& ]", " ", s)
    return " ".join(s.split()[:3])


def recurring(conn):
    since = (date.today() - timedelta(days=400)).isoformat()
    labels = account_labels(conn)
    kinds = category_kinds(conn)
    merchants = defaultdict(list)
    for t in conn.execute("SELECT * FROM transactions WHERE date>=? AND amount>0 AND pending=0", (since,)):
        t = dict(t)
        if kinds.get(t["category"]) != "spending":
            continue
        merchants[(_merchant_key(t), t["account_id"])].append(t)
    # Split each merchant into amount clusters (so $1,150 Venmo rent isn't mixed with $25 Venmo dinners)
    groups = []
    for (key, acct), txns in merchants.items():
        clusters = []
        for t in sorted(txns, key=lambda x: x["amount"]):
            if clusters and t["amount"] <= statistics.median(x["amount"] for x in clusters[-1]) * 1.6:
                clusters[-1].append(t)
            else:
                clusters.append([t])
        groups += [((key, acct), c) for c in clusters]
    out = []
    today = date.today()
    for (key, acct), txns in groups:
        if not key or len(txns) < 2:
            continue
        txns.sort(key=lambda x: x["date"])
        ds = [date.fromisoformat(x["date"]) for x in txns]
        gaps = [(b - a).days for a, b in zip(ds, ds[1:]) if (b - a).days > 0]
        if not gaps:
            continue
        med_gap = statistics.median(gaps)
        match = next(((nm, days, tol) for nm, days, tol in PERIODS if abs(med_gap - days) <= tol), None)
        if not match:
            continue
        nm, days, tol = match
        if len(txns) < (2 if days >= 91 else 3):
            continue
        regular = sum(1 for g in gaps if abs(g - days) <= tol * 1.5) / len(gaps)
        amts = [x["amount"] for x in txns]
        med_amt = statistics.median(amts)
        spread = statistics.median([abs(a - med_amt) for a in amts]) / med_amt if med_amt else 1
        # Everyday categories must repeat at a near-identical amount to count (e.g. a
        # gym or Prime membership); bills like utilities may vary more.
        max_spread = 0.05 if txns[-1]["category"] in EVERYDAY else 0.3
        if regular < 0.6 or spread > max_spread:
            continue
        last = ds[-1]
        active = (today - last).days <= days * 1.6 + 5
        out.append({
            "merchant": txns[-1]["merchant_name"] or key.title(), "category": txns[-1]["category"],
            "account": labels.get(acct, "?"), "account_id": acct, "merchant_key": key,
            "period_days": days, "frequency": nm, "typical_amount": round(med_amt, 2),
            "monthly_cost": round(med_amt * 30.4 / days, 2), "occurrences": len(txns),
            "last_date": str(last), "next_expected": str(last + timedelta(days=round(days))), "active": active,
        })
    out.sort(key=lambda x: (not x["active"], -x["monthly_cost"]))
    return {"items": out, "monthly_total_active": round(sum(x["monthly_cost"] for x in out if x["active"]), 2)}


# ---------------- Investments ----------------

TYPE_LABELS = {"equity": "Stocks", "etf": "ETFs", "mutual fund": "Mutual funds", "cash": "Cash",
               "fixed income": "Bonds / fixed income", "cryptocurrency": "Crypto", "derivative": "Options",
               "loan": "Loans", "other": "Other"}


def investments(conn):
    labels = account_labels(conn)
    accts = {a["account_id"]: dict(a) for a in conn.execute("SELECT * FROM accounts WHERE type='investment' AND hidden=0")}
    rows = []
    for h in conn.execute(
            "SELECT h.*, s.name AS sec_name, s.ticker, s.type AS sec_type, s.is_cash_equivalent, s.close_price "
            "FROM holdings h LEFT JOIN securities s ON s.security_id=h.security_id"):
        h = dict(h)
        if h["account_id"] not in accts:
            continue
        value = h["institution_value"]
        if value is None and h["quantity"] is not None:
            value = h["quantity"] * (h["institution_price"] or h["close_price"] or 0)
        # Plaid flags some crypto as cash-equivalent; the security type wins when present.
        typ = h["sec_type"] or ("cash" if h["is_cash_equivalent"] else "other")
        rows.append({"account": labels.get(h["account_id"]), "account_id": h["account_id"],
                     "name": h["sec_name"] or h["security_id"], "ticker": h["ticker"],
                     "type": TYPE_LABELS.get(typ, typ.title()), "quantity": h["quantity"],
                     "price": h["institution_price"], "value": round(value or 0, 2),
                     "cost_basis": h["cost_basis"], "price_as_of": h["price_as_of"]})
    # Investment accounts with a balance but no holdings detail (e.g. manual 401(k))
    for aid, a in accts.items():
        held = sum(r["value"] for r in rows if r["account_id"] == aid)
        bal = a["current_balance"] or 0
        if not any(r["account_id"] == aid for r in rows) and bal:
            rows.append({"account": labels.get(aid), "account_id": aid, "name": "Account balance (no holdings detail)",
                         "ticker": None, "type": "Unknown", "quantity": None, "price": None, "value": round(bal, 2),
                         "cost_basis": None, "price_as_of": a["balance_updated_at"]})
        elif bal and bal - held > 1 and not any(r["account_id"] == aid and r["type"] == "Cash" for r in rows):
            rows.append({"account": labels.get(aid), "account_id": aid, "name": "Uninvested cash", "ticker": "CASH",
                         "type": "Cash", "quantity": None, "price": None, "value": round(bal - held, 2),
                         "cost_basis": None, "price_as_of": a["balance_updated_at"]})
    total = sum(r["value"] for r in rows)

    def agg(key):
        d = defaultdict(float)
        for r in rows:
            d[r[key]] += r["value"]
        return [{"name": k, "value": round(v, 2), "pct": round(100 * v / total, 1) if total else 0}
                for k, v in sorted(d.items(), key=lambda kv: -kv[1])]

    by_holding = defaultdict(float)
    for r in rows:
        by_holding[r["ticker"] or r["name"]] += r["value"]
    for r in rows:
        r["pct"] = round(100 * r["value"] / total, 1) if total else 0
    rows.sort(key=lambda r: -r["value"])
    return {"total": round(total, 2), "holdings": rows, "by_account": agg("account"), "by_type": agg("type"),
            "by_holding": [{"name": k, "value": round(v, 2), "pct": round(100 * v / total, 1) if total else 0}
                           for k, v in sorted(by_holding.items(), key=lambda kv: -kv[1])]}
