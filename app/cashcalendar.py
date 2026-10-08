"""30-day cash calendar for your checking account(s).

Builds a day-by-day forecast from things the dashboard already knows:
  * paychecks: biweekly from your next payday, at your latest paycheck amount
  * credit card statements still owed (statement balance minus payments since),
    on their due dates; the following statement is estimated if it falls inside
    the window
  * recurring charges detected on checking (car loan, utilities...)
  * planned payments you add yourself (e.g. rent)
  * everyday spending straight from checking (Venmo, ATM...), estimated weekly
    from the last 3 months
Money people pay you back is not counted (it's irregular), so the forecast leans
conservative.
"""
import statistics
from datetime import date, timedelta

from . import analytics, future
from .categories import category_kinds
from .db import now_iso
from .projections import add_months

SCHEMA = """
CREATE TABLE IF NOT EXISTS planned_items (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  amount REAL NOT NULL,          -- positive = money out of checking, negative = money in
  first_date TEXT NOT NULL,
  repeat_monthly INTEGER DEFAULT 1,
  created_at TEXT
);
"""


def ensure(conn):
    conn.executescript(SCHEMA)


def _checking_accounts(conn):
    return [dict(a) for a in conn.execute(
        "SELECT * FROM accounts WHERE type='depository' AND COALESCE(subtype,'')!='savings' AND hidden=0")]


def build(conn, days: int = 30) -> dict:
    ensure(conn)
    today = date.today()
    end = today + timedelta(days=days)
    checking = _checking_accounts(conn)
    chk_ids = {a["account_id"] for a in checking}
    start_bal = sum((a["available_balance"] if a["available_balance"] is not None else a["current_balance"]) or 0
                    for a in checking)
    events = []

    def add(d, name, amount, kind, estimate=False, note=""):
        if today <= d <= end:
            events.append({"date": str(d), "name": name, "amount": round(amount, 2), "kind": kind,
                           "estimate": estimate, "note": note})

    # Paychecks
    base = future.baseline(conn)["inputs"]
    pay = base["take_home_per_paycheck"]
    d = date.fromisoformat(base["next_payday"])
    while d <= end:
        add(d, "Paycheck", -pay, "paycheck", note="latest paycheck amount")
        d += timedelta(days=14)

    # Credit card statements
    for card in conn.execute("SELECT a.*, l.last_statement_balance, l.last_statement_date, l.next_due_date "
                             "FROM accounts a JOIN liabilities l USING(account_id) WHERE a.hidden=0"):
        label = card["display_name"] or card["name"]
        stmt, sdate, due = card["last_statement_balance"] or 0, card["last_statement_date"], card["next_due_date"]
        paid = 0.0
        if sdate:
            paid = -(conn.execute("SELECT SUM(amount) FROM transactions WHERE account_id=? AND amount<0 "
                                  "AND category='Credit Card Payment' AND date>?", (card["account_id"], sdate))
                     .fetchone()[0] or 0)
        remaining = max(0.0, stmt - paid)
        if due and remaining > 0.005:
            dd = date.fromisoformat(due)
            if dd < today:
                add(today, f"{label} statement (was due {dd:%b %d})", remaining, "card",
                    note="no payment seen yet; it may already be on its way")
            else:
                add(dd, f"{label} statement", remaining, "card",
                    note="due today; no payment seen yet" if dd == today else "")
        if due:
            nd = add_months(date.fromisoformat(due), 1)
            new_charges = max(0.0, (card["current_balance"] or 0) - remaining)
            if new_charges > 0.005:
                add(nd, f"{label} next statement", new_charges, "card", estimate=True,
                    note="estimated from charges since the last statement")

    # Recurring charges on checking
    rec = analytics.recurring(conn)["items"]
    rec_keys = set()
    for r in rec:
        if not r["active"] or r["account_id"] not in chk_ids:
            continue
        rec_keys.add((r["merchant_key"], r["account_id"]))
        nd = date.fromisoformat(r["next_expected"])
        while nd < today:
            nd += timedelta(days=round(r["period_days"]))
        while nd <= end:
            add(nd, r["merchant"], r["typical_amount"], "recurring", estimate=True, note=f"recurring ({r['frequency']})")
            nd += timedelta(days=round(r["period_days"]))

    # Planned items you added
    for p in conn.execute("SELECT * FROM planned_items"):
        nd = date.fromisoformat(p["first_date"])
        if p["repeat_monthly"]:
            k = 0
            while add_months(nd, k) < today:
                k += 1
            while add_months(nd, k) <= end:
                add(add_months(nd, k), p["name"], p["amount"], "planned", note="you added this")
                k += 1
        else:
            add(nd, p["name"], p["amount"], "planned", note="you added this")

    # Everyday spending straight from checking (not cards, not recurring, not rent)
    kinds = category_kinds(conn)
    months = []
    first = today.replace(day=1)
    for i in range(1, 4):
        s = add_months(first, -i)
        e = add_months(first, -i + 1)
        total = 0.0
        for t in conn.execute("SELECT * FROM transactions WHERE date>=? AND date<? AND pending=0",
                              (str(s), str(e))):
            t = dict(t)
            if t["account_id"] not in chk_ids or kinds.get(t["category"]) != "spending" or t["category"] == "Rent":
                continue
            if (analytics._merchant_key(t), t["account_id"]) in rec_keys or t["amount"] <= 0:
                continue  # money paid back to you isn't counted on: it's irregular
            total += t["amount"]
        months.append(max(0.0, total))
    weekly = statistics.mean(months) * 12 / 52 if months else 0.0
    if weekly > 1:
        d = today + timedelta(days=(4 - today.weekday()) % 7 or 7)  # Fridays
        while d <= end:
            add(d, "Everyday spending from checking", weekly, "everyday", estimate=True,
                note="weekly average of Venmo/Zelle/ATM etc. paid directly from checking, last 3 months")
            d += timedelta(days=7)

    # Running balance
    events.sort(key=lambda e: (e["date"], e["amount"] > 0))  # money in first on the same day
    bal = start_bal
    low = {"amount": start_bal, "date": str(today)}
    for e in events:
        bal -= e["amount"]
        e["balance"] = round(bal, 2)
        if bal < low["amount"]:
            low = {"amount": round(bal, 2), "date": e["date"]}

    last = conn.execute("SELECT MAX(date) FROM transactions WHERE category='Rent' AND amount>0").fetchone()[0]
    rent_recent = [dict(r) for r in conn.execute(
        "SELECT date, amount FROM transactions WHERE category='Rent' AND amount>0 AND substr(date,1,7)=? "
        "ORDER BY amount DESC", (last[:7],))] if last else []
    has_rent = any("rent" in e["name"].lower() for e in events) or planned_rent(conn) is not None
    return {
        "start_balance": round(start_bal, 2), "start_note": "available balance (includes pending transactions)",
        "end_balance": round(bal, 2), "lowest": low, "events": events, "days": days,
        "rent_suggestion": None if has_rent or not rent_recent else
        {"payments": rent_recent, "amount": rent_recent[0]["amount"], "day": int(rent_recent[0]["date"][8:10])},
        "planned": [dict(p) for p in conn.execute("SELECT * FROM planned_items ORDER BY first_date")],
    }


def planned_rent(conn):
    """Your monthly rent if you've added it as a planned payment (name contains 'rent')."""
    ensure(conn)
    r = conn.execute("SELECT * FROM planned_items WHERE repeat_monthly=1 AND LOWER(name) LIKE '%rent%' "
                     "AND amount>0 ORDER BY id DESC LIMIT 1").fetchone()
    return dict(r) if r else None


def add_planned(conn, name: str, amount: float, first_date: str, repeat_monthly: bool):
    ensure(conn)
    date.fromisoformat(first_date)
    if not name.strip():
        raise ValueError("Give it a name.")
    conn.execute("INSERT INTO planned_items(name,amount,first_date,repeat_monthly,created_at) VALUES(?,?,?,?,?)",
                 (name.strip()[:80], float(amount), first_date, int(bool(repeat_monthly)), now_iso()))


def delete_planned(conn, pid: int):
    ensure(conn)
    conn.execute("DELETE FROM planned_items WHERE id=?", (pid,))
