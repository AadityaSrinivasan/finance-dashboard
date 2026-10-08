"""Builds the 'My current habits' starting point for the Future tab from your
real data, and stores named scenarios in the local database."""
import json
import statistics
from datetime import date, timedelta

from .db import get_setting, now_iso, set_setting
from .projections import next_payday

SCHEMA = """
CREATE TABLE IF NOT EXISTS future_scenarios (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT UNIQUE NOT NULL,
  data TEXT NOT NULL,
  created_at TEXT,
  updated_at TEXT
);
"""

SETTING_DEFAULTS = {
    "future_k401_limit": "24500",       # IRS elective deferral limit (2026: $24,500, per irs.gov)
    "future_k401_limit_year": "2026",
    "future_hsa_limit": "4400",         # IRS 2026 HSA limit, self-only coverage (family: $8,750)
    "future_marginal_tax_pct": "30",    # rough federal + state rate on your last dollars of pay
    "future_spending_months": "3",
    "future_transfer_months": "6",
}


def ensure(conn):
    conn.executescript(SCHEMA)
    for k, v in SETTING_DEFAULTS.items():
        if get_setting(conn, k) is None:
            set_setting(conn, k, v)


def settings(conn) -> dict:
    ensure(conn)
    return {k: float(get_setting(conn, k)) for k in SETTING_DEFAULTS}


def _full_months_back(n: int):
    """The last n complete calendar months, oldest first, as (start, end_exclusive, 'YYYY-MM')."""
    first_this = date.today().replace(day=1)
    out = []
    end = first_this
    for _ in range(n):
        start = (end - timedelta(days=1)).replace(day=1)
        out.append((start, end, start.strftime("%Y-%m")))
        end = start
    return list(reversed(out))


def _sum(conn, sql, args):
    return conn.execute(sql, args).fetchone()[0] or 0.0


def baseline(conn) -> dict:
    """Inputs for 'My current habits', plus where each number came from."""
    st = settings(conn)
    kinds = {r["name"]: r["kind"] for r in conn.execute("SELECT name, kind FROM categories")}
    spend_cats = [k for k, v in kinds.items() if v == "spending"]
    info = {}

    # ---- Pay stub ----
    stub = conn.execute("SELECT * FROM paystubs WHERE pay_date IS NOT NULL ORDER BY pay_date DESC LIMIT 1").fetchone()
    f = {}
    if stub:
        f = {r["field"]: (r["current"] or 0, r["ytd"] or 0) for r in conn.execute(
            "SELECT field,current,ytd FROM paystub_fields WHERE paystub_id=?", (stub["id"],))}
    cur = lambda k: f.get(k, (0, 0))[0]  # noqa: E731
    gross_pp = cur("gross")
    k401_pp = cur("k401_pretax") + cur("k401_roth")

    # What your employer adds, read from the stub's "Employer Paid Benefits" lines
    emp_lines = [dict(r) for r in conn.execute(
        "SELECT label, current FROM paystub_lines WHERE paystub_id=? AND LOWER(section) LIKE '%employer%'",
        (stub["id"],))] if stub else []

    def emp_sum(*words):
        return round(sum((ln["current"] or 0) for ln in emp_lines
                         if any(w in (ln["label"] or "").lower() for w in words)), 2)

    emp_401k_lines = [{"label": ln["label"], "amount": ln["current"],
                       "pct": round(100 * (ln["current"] or 0) / gross_pp, 2) if gross_pp else 0}
                      for ln in emp_lines if "401" in (ln["label"] or "")]
    emp_401k = sum(x["amount"] or 0 for x in emp_401k_lines)
    emp_aspp = emp_sum("aspp", "espp", "stock purchase")
    emp_hsa = emp_sum("hsa", "health savings")
    today = date.today()
    nxt = next_payday(date.fromisoformat(stub["pay_date"]), today) if stub else today
    info["paystub"] = {"pay_date": stub["pay_date"] if stub else None, "gross": gross_pp, "k401": k401_pp,
                       "espp": cur("espp"), "hsa": cur("hsa"), "net_pay": cur("net_pay"),
                       "employer_401k": round(emp_401k, 2), "employer_401k_lines": emp_401k_lines,
                       "employer_401k_pct": round(100 * emp_401k / gross_pp, 2) if gross_pp else 0,
                       "k401_pct": round(100 * k401_pp / gross_pp, 2) if gross_pp else 0,
                       "employer_aspp": emp_aspp, "espp_pct": round(100 * cur("espp") / gross_pp, 2) if gross_pp else 0,
                       "employer_aspp_pct_of_contribution": round(100 * emp_aspp / cur("espp"), 2) if cur("espp") else 0,
                       "employer_hsa": emp_hsa}

    # Take-home: most recent paycheck deposit that looks like a regular paycheck (within 10% of stub net pay)
    take_home_pp, th_source = cur("net_pay"), "pay stub net pay"
    for r in conn.execute("SELECT date, -amount AS a FROM transactions WHERE category='Paycheck' AND amount<0 "
                          "ORDER BY date DESC LIMIT 6"):
        if cur("net_pay") and abs(r["a"] - cur("net_pay")) <= 0.1 * cur("net_pay"):
            take_home_pp, th_source = r["a"], f"your {r['date']} paycheck deposit"
            break
    info["take_home_source"] = th_source

    # 401(k) already contributed this calendar year: stub YTD + paychecks since that stub
    k401_ytd = 0.0
    paychecks_since = 0
    if stub and stub["pay_date"][:4] == str(today.year):
        d = date.fromisoformat(stub["pay_date"]) + timedelta(days=14)
        while d <= today:
            paychecks_since += 1
            d += timedelta(days=14)
        k401_ytd = f.get("k401_pretax", (0, 0))[1] + f.get("k401_roth", (0, 0))[1] + paychecks_since * k401_pp

    # ---- Spending: average of the last N complete months ----
    months = _full_months_back(int(st["future_spending_months"]))
    q = ",".join("?" * len(spend_cats)) or "''"
    # If you've told the dashboard your regular rent (a monthly planned payment named "rent"),
    # use spending WITHOUT rent from those months plus that rent, so one-off rent months
    # (free months, move-in costs, an old apartment) don't skew it.
    from .cashcalendar import planned_rent
    rent = planned_rent(conn)
    cats = [c for c in spend_cats if not (rent and c == "Rent")]
    q = ",".join("?" * len(cats)) or "''"
    per_month = []
    for s, e, label in months:
        v = _sum(conn, f"SELECT SUM(amount) FROM transactions WHERE date>=? AND date<? AND category IN ({q})",
                 [str(s), str(e)] + cats)
        per_month.append({"month": label, "amount": round(v, 2)})
    spending = statistics.mean(m["amount"] for m in per_month) if per_month else 0.0
    if rent:
        spending += rent["amount"]
    info["spending_months"] = per_month
    info["spending_rent"] = {"amount": rent["amount"], "from": rent["first_date"]} if rent else None

    # ---- Balances ----
    accts = [dict(a) for a in conn.execute(
        "SELECT * FROM accounts WHERE hidden=0 AND include_in_networth=1 AND type IN ('depository','investment')")]

    sub = lambda a: (a["subtype"] or "").lower()  # noqa: E731
    name = lambda a: f"{a['display_name'] or ''} {a['name'] or ''}".lower()  # noqa: E731
    used = set()
    groups = {}

    def take(key, pred):
        """Sum the balances of matching accounts (each account counted once) and remember them."""
        picked = [a for a in accts if pred(a) and a["account_id"] not in used]
        used.update(a["account_id"] for a in picked)
        groups[key] = picked
        return round(sum(a["current_balance"] or 0 for a in picked), 2)

    checking = take("checking", lambda a: a["type"] == "depository" and sub(a) != "savings")
    hysa = take("hysa", lambda a: a["type"] == "depository" and sub(a) == "savings")
    espp = take("espp", lambda a: "stock plan" in sub(a) or "stock plan" in name(a) or "espp" in name(a))
    brokerage = take("brokerage", lambda a: sub(a) == "brokerage")
    k401 = take("401k", lambda a: sub(a) in ("401k", "401a", "403b", "457b"))
    roth = take("roth", lambda a: "roth" in sub(a) or "ira" in sub(a))
    hsa = take("hsa", lambda a: sub(a) == "hsa")
    crypto = take("crypto", lambda a: "crypto" in sub(a))

    # Institution names come from YOUR database (nothing bank-specific is written in this code).
    def institutions(key):
        names = []
        for a in sorted(groups[key], key=lambda a: -(a["current_balance"] or 0)):
            n = (a["institution_name"] or "").replace("Demo:", "").strip()
            if n and n not in names and ((a["current_balance"] or 0) > 0 or not names):
                names.append(n)
        return names

    def label(base, key):
        names = institutions(key)
        return f"{base} ({', '.join(names)})" if names else base

    def keywords(key):
        """First word of each institution's name (e.g. 'Acme Invest' -> 'acme'), used to spot transfers."""
        return {n.split()[0].lower() for n in institutions(key) if n.split()}

    # ---- Transfers into brokerage / retirement accounts: average of the last N complete months ----
    tmonths = _full_months_back(int(st["future_transfer_months"]))
    t0, t1 = str(tmonths[0][0]), str(tmonths[-1][1])

    def transfers(words):
        rows = []
        for s, e, mlabel in tmonths:
            v = 0.0
            if words:
                cond = " OR ".join(["LOWER(name) LIKE ? OR LOWER(COALESCE(merchant_name,'')) LIKE ?"] * len(words))
                args = [x for w in words for x in (f"%{w}%", f"%{w}%")]
                v = _sum(conn, f"SELECT SUM(amount) FROM transactions WHERE category='Investing' AND date>=? "
                               f"AND date<? AND ({cond})", [str(s), str(e)] + args)
            rows.append({"month": mlabel, "amount": round(v, 2)})
        return rows

    brk_words = keywords("brokerage") | keywords("crypto")
    ret_words = keywords("roth") - brk_words
    brk_rows, ret_rows = transfers(sorted(brk_words)), transfers(sorted(ret_words))
    brk_avg = max(0.0, statistics.mean(r["amount"] for r in brk_rows))
    ret_avg = max(0.0, statistics.mean(r["amount"] for r in ret_rows))
    brk_label, ret_label = label("Brokerage", "brokerage"), label("Roth IRA", "roth")
    info["brokerage_months"], info["retirement_months"] = brk_rows, ret_rows
    info["brokerage_label"], info["retirement_label"] = brk_label, ret_label
    fam = _sum(conn, "SELECT -SUM(amount) FROM transactions WHERE category='Family support' AND date>=? AND date<?",
               (t0, t1))
    info["family_support_monthly_avg"] = round(fam / len(tmonths), 2)

    def b(bid, name, kind, start, contribution=0.0, frequency="monthly", rate=0.0, funding="take_home",
          stock=False, base=0.0, removable=True, note=""):
        return {"id": bid, "name": name, "kind": kind, "start": start, "contribution": round(contribution, 2),
                "frequency": frequency, "rate_pct": rate, "funding": funding, "stock": stock,
                "base_contribution": round(base, 2), "linked": bid, "start_from_data": True,
                "removable": removable, "note": note}

    buckets = [
        b("checking", "Checking", "checking", checking, rate=0.0, removable=False),
        b("hysa", "High-Yield Savings", "hysa", hysa, rate=3.8, removable=False,
          note="From your savings account(s)." if hysa else "Not connected yet: starts at $0."),
        b("brokerage", brk_label, "brokerage", brokerage, brk_avg, rate=7.0, stock=True, removable=False,
          note=f"Contribution = average net transfers to it over the last {len(tmonths)} months."),
        b("401k", label("401(k)", "401k"), "401k", k401, k401_pp, "paycheck", 7.0, "pretax", True, k401_pp,
          removable=False,
          note="Your contribution and your company's are both from your pay stub. If you lower your own "
               "contribution, check whether your match drops too."),
    ]
    buckets[-1].update(employer_amount=info["paystub"]["employer_401k_pct"], employer_basis="pct_salary")
    # Any other investment account with money in it gets its own bucket (empty ones are skipped).
    for o in [a for a in accts if a["account_id"] not in used and a["type"] == "investment"
              and abs(a["current_balance"] or 0) >= 0.01]:
        buckets.append(b(o["account_id"], o["display_name"] or o["name"], "custom", round(o["current_balance"] or 0, 2),
                         rate=7.0, stock=True))
    if roth or ret_avg:
        buckets.append(b("roth", ret_label, "custom", roth, ret_avg, rate=7.0, stock=True,
                         note=f"Contribution = average transfers to it over the last {len(tmonths)} months."))
    if hsa or cur("hsa"):
        hb = b("hsa", label("HSA", "hsa"), "custom", hsa, cur("hsa"), "paycheck", 3.8, "pretax", False, cur("hsa"),
               note="Per-paycheck amount from your pay stub; held in a cash fund. Stops each year at the IRS limit "
                    "(employer HSA money counts toward it too).")
        hsa_ytd = 0.0
        if stub and stub["pay_date"][:4] == str(today.year):
            hsa_ytd = f.get("hsa", (0, 0))[1] + paychecks_since * cur("hsa")
            hsa_ytd += sum((r["ytd"] or 0) for r in conn.execute(
                "SELECT ytd FROM paystub_lines WHERE paystub_id=? AND LOWER(section) LIKE '%employer%' "
                "AND LOWER(label) LIKE '%hsa%'", (stub["id"],)))
        hb.update(annual_limit=st["future_hsa_limit"], stop_at_limit=True, ytd=round(hsa_ytd, 2),
                  employer_amount=emp_hsa, employer_basis="per_paycheck")
        buckets.append(hb)
    if crypto:
        buckets.append(b("crypto", label("Crypto", "crypto"), "custom", crypto, rate=7.0, stock=True,
                         note="Crypto can swing far more than the range shown."))
    if cur("espp") or espp:
        buckets.append(b("espp", label("ESPP", "espp"), "custom", espp, cur("espp"), "paycheck", 7.0, "posttax", True,
                         cur("espp"),
                         note=("Balance from your stock plan account; " if espp else "Enter your stock plan balance; ")
                              + "your contribution and the company match are from your pay stub. "
                                "A single company's stock can swing more than the range shown."))
        buckets[-1].update(employer_amount=info["paystub"]["employer_aspp_pct_of_contribution"],
                           employer_basis="pct_contribution")

    inputs = {
        "horizon_years": 10, "salary_growth_pct": 0.0, "inflation_pct": 3.0, "show_real": False,
        "contributions_follow_salary": False, "spending_follows_inflation": True, "range_pp": 3.0,
        "gross_per_paycheck": round(gross_pp, 2), "take_home_per_paycheck": round(take_home_pp, 2),
        "monthly_spending": round(spending, 2), "other_monthly_income": 0.0,
        "marginal_tax_pct": st["future_marginal_tax_pct"], "employer_match_pct": 0.0,
        "k401_limit": st["future_k401_limit"], "k401_ytd": round(k401_ytd, 2),
        "next_payday": str(nxt), "start_date": str(today), "buckets": buckets, "lump_sums": [],
    }
    info["k401_limit_year"] = int(st["future_k401_limit_year"])
    info["debts_excluded"] = round(sum(a["current_balance"] or 0 for a in conn.execute(
        "SELECT current_balance FROM accounts WHERE hidden=0 AND type IN ('credit','loan')")), 2)
    return {"inputs": inputs, "info": info}


# ---------------- scenarios ----------------

def list_scenarios(conn):
    ensure(conn)
    return [{"id": r["id"], "name": r["name"], "updated_at": r["updated_at"], "inputs": json.loads(r["data"])}
            for r in conn.execute("SELECT * FROM future_scenarios ORDER BY name")]


def save_scenario(conn, name: str, inputs: dict):
    ensure(conn)
    name = name.strip()[:80]
    if not name:
        raise ValueError("Give the scenario a name.")
    conn.execute("INSERT INTO future_scenarios(name,data,created_at,updated_at) VALUES(?,?,?,?) "
                 "ON CONFLICT(name) DO UPDATE SET data=excluded.data, updated_at=excluded.updated_at",
                 (name, json.dumps(inputs), now_iso(), now_iso()))
    return conn.execute("SELECT id FROM future_scenarios WHERE name=?", (name,)).fetchone()["id"]


def delete_scenario(conn, sid: int):
    conn.execute("DELETE FROM future_scenarios WHERE id=?", (sid,))
