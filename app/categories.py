"""Categories, rules, and transfer detection.

Every transaction gets exactly one category. Precedence:
  1. Your manual choice for that transaction
  2. Transfer match: money left one of your accounts and the same amount arrived
     in another of your accounts within 7 days
  3. Rules (yours and the built-in ones), lowest priority number first
  4. Plaid's category
  5. Uncategorized

Each category has a "kind". Only 'spending' and 'income' count toward
spending/income totals. 'transfer' (Transfer, Credit Card Payment) is excluded
everywhere. 'investing' is tracked separately as money invested.
"""
import re
from datetime import date

from .db import now_iso

CATEGORIES = [
    # name, kind, color
    ("Paycheck", "income", "#2e7d32"),
    ("Interest & Dividends", "income", "#43a047"),
    ("Other Income", "income", "#66bb6a"),
    ("Rent", "spending", "#5c6bc0"),
    ("Utilities", "spending", "#7986cb"),
    ("Groceries", "spending", "#26a69a"),
    ("Dining", "spending", "#ef6c00"),
    ("Shopping", "spending", "#ab47bc"),
    ("Subscriptions", "spending", "#ec407a"),
    ("Car Payment", "spending", "#8d6e63"),
    ("Gas", "spending", "#a1887f"),
    ("Transportation", "spending", "#78909c"),
    ("Travel", "spending", "#29b6f6"),
    ("Entertainment", "spending", "#ffa726"),
    ("Medical", "spending", "#e57373"),
    ("Personal Care", "spending", "#f06292"),
    ("Services", "spending", "#9575cd"),
    ("Home", "spending", "#4db6ac"),
    ("Gifts & Donations", "spending", "#ffb74d"),
    ("Government & Taxes", "spending", "#90a4ae"),
    ("Bank Fees", "spending", "#bdbdbd"),
    ("Loan Payments", "spending", "#6d4c41"),
    ("Payments to people", "spending", "#26c6da"),
    ("Reimbursements", "spending", "#4dd0e1"),  # money people pay you back; negative amounts reduce spending
    ("Cash & ATM", "spending", "#827717"),
    ("Uncategorized", "spending", "#9e9e9e"),
    ("Investing", "investing", "#1565c0"),
    ("Transfer", "transfer", "#b0bec5"),
    ("Credit Card Payment", "transfer", "#cfd8dc"),
]

# Built-in rules (editable/disable-able in the app) plus your starting custom rules.
SEED_RULES = [
    # name, pattern, match_type, category, account_type, direction, priority, is_system
    ("Card payment sent from bank",
     "credit crd|crd epay|card payment|credit card|payment to crd|card autopay|card services",
     "contains", "Credit Card Payment", "depository", "out", 10, 1),
    ("Card payment received on card",
     "payment thank you|automatic payment|autopay|payment received|online payment|mobile payment|ach payment|epayment",
     "contains", "Credit Card Payment", "credit", "in", 11, 1),
    # Common US brokerages; add your own on the Rules page.
    ("Money moved to/from a brokerage",
     "brokerage|robinhood|schwab|vanguard|e*trade|etrade|webull|wealthfront|betterment|interactive brokers|"
     "merrill edge|m1 finance|fidelity investments|public.com",
     "word", "Investing", "depository", None, 20, 1),
    ("Spotify", "spotify", "word", "Subscriptions", None, None, 50, 0),
    ("Apple / iCloud", "apple|icloud", "word", "Subscriptions", None, None, 50, 0),
    ("Car loan", "auto finance|auto loan|motors finance|car payment", "contains", "Car Payment", None, "out", 50, 0),
    ("Paycheck direct deposit", "payroll|dir dep|direct dep|direct deposit|salary", "contains",
     "Paycheck", "depository", "in", 60, 1),
]
# Added in later versions; inserted once into existing databases.
SEED_RULES_V2 = [
    # Venmo/Zelle payments to other people are real spending (Plaid labels them "transfers").
    # Payments to your OWN accounts are still caught by transfer matching, which runs first.
    ("Venmo / Zelle payments to people", "venmo|zelle|cash app|square cash", "word",
     "Payments to people", "depository", "out", 70, 1),
]

TRANSFER_HINT = re.compile(r"transfer|xfer|payment|pymt|epay|autopay|online banking|\bcrd\b|thank you", re.I)


def seed_categories_and_rules(conn):
    for i, (name, kind, color) in enumerate(CATEGORIES):
        conn.execute("INSERT OR IGNORE INTO categories(name,kind,color,sort) VALUES(?,?,?,?)",
                     (name, kind, color, i))
    row = conn.execute("SELECT value FROM settings WHERE key='rules_seed_version'").fetchone()
    version = int(row[0]) if row else (1 if conn.execute("SELECT COUNT(*) FROM rules").fetchone()[0] else 0)
    batches = [(1, SEED_RULES), (2, SEED_RULES_V2)]
    for v, batch in batches:
        if version >= v:
            continue
        for name, pattern, mt, cat, atype, direction, prio, system in batch:
            conn.execute(
                "INSERT INTO rules(name,pattern,match_type,category,account_type,direction,priority,"
                "is_system,enabled,created_at) VALUES(?,?,?,?,?,?,?,?,1,?)",
                (name, pattern, mt, cat, atype, direction, prio, system, now_iso()))
    conn.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('rules_seed_version',?)", (str(batches[-1][0]),))


def category_kinds(conn) -> dict:
    return {r["name"]: r["kind"] for r in conn.execute("SELECT name, kind FROM categories")}


# ---------- Plaid category mapping ----------

def map_plaid_category(primary: str, detailed: str, amount: float) -> str:
    p = (primary or "").upper()
    d = (detailed or "").upper()
    if not p:
        return None
    if p == "INCOME":
        if "WAGES" in d or "SALARY" in d:
            return "Paycheck"
        if "INTEREST" in d or "DIVIDEND" in d:
            return "Interest & Dividends"
        return "Other Income"
    if p in ("TRANSFER_IN", "TRANSFER_OUT"):
        if "INVESTMENT" in d or "RETIREMENT" in d:
            return "Investing"
        if p == "TRANSFER_OUT" and "WITHDRAWAL" in d:
            return "Cash & ATM"
        if "APPS" in d or "PEER" in d:  # Venmo/Zelle etc. to or from other people
            return "Payments to people" if (amount or 0) > 0 else "Reimbursements"
        return "Transfer"
    if p == "LOAN_PAYMENTS":
        if "CREDIT_CARD" in d:
            return "Credit Card Payment"
        if "CAR" in d:
            return "Car Payment"
        return "Loan Payments"
    if p == "LOAN_DISBURSEMENTS":
        return "Transfer"
    if p == "FOOD_AND_DRINK":
        return "Groceries" if "GROCER" in d else "Dining"
    if p == "TRANSPORTATION":
        return "Gas" if "GAS" in d else "Transportation"
    if p == "RENT_AND_UTILITIES":
        return "Rent" if d.endswith("_RENT") or d == "RENT" else "Utilities"
    if p == "GOVERNMENT_AND_NON_PROFIT":
        return "Gifts & Donations" if "DONATION" in d else "Government & Taxes"
    simple = {
        "BANK_FEES": "Bank Fees", "ENTERTAINMENT": "Entertainment", "GENERAL_MERCHANDISE": "Shopping",
        "HOME_IMPROVEMENT": "Home", "MEDICAL": "Medical", "PERSONAL_CARE": "Personal Care",
        "GENERAL_SERVICES": "Services", "TRAVEL": "Travel",
    }
    if p in simple:
        return simple[p]
    return None  # unknown/new taxonomy value: fall through to default


# ---------- Rules ----------

def _compile(rule):
    parts = [x.strip().lower() for x in (rule["pattern"] or "").split("|") if x.strip()]
    if not parts:
        return None
    mt = rule["match_type"] or "word"
    try:
        if mt == "regex":
            return re.compile(rule["pattern"], re.I)
        if mt == "contains":
            return re.compile("|".join(re.escape(x) for x in parts), re.I)
        return re.compile("|".join(r"(?<![a-z0-9])" + re.escape(x) + r"(?![a-z0-9])" for x in parts), re.I)
    except re.error:
        return None


def txn_text(t) -> str:
    return " | ".join(x for x in (t["merchant_name"], t["name"], t["original_description"]) if x).lower()


def _day_ok(day, dmin, dmax):
    if dmin is None or dmax is None:
        return True
    if dmin <= dmax:
        return dmin <= day <= dmax
    return day >= dmin or day <= dmax  # wraps around month end, e.g. 28..3


def rule_matches(rule, rx, t, acct_type) -> bool:
    if rx is None:
        return False
    amt = t["amount"] or 0
    if rule["direction"] == "out" and amt <= 0:
        return False
    if rule["direction"] == "in" and amt >= 0:
        return False
    if rule["account_id"] and rule["account_id"] != t["account_id"]:
        return False
    if rule["account_type"] and rule["account_type"] != acct_type:
        return False
    if rule["amount_min"] is not None and abs(amt) < rule["amount_min"]:
        return False
    if rule["amount_max"] is not None and abs(amt) > rule["amount_max"]:
        return False
    if not _day_ok(int(t["date"][8:10]), rule["day_min"], rule["day_max"]):
        return False
    return bool(rx.search(txn_text(t)))


def load_rules(conn):
    rules = [dict(r) for r in conn.execute(
        "SELECT * FROM rules WHERE enabled=1 ORDER BY priority, id")]
    return [(r, _compile(r)) for r in rules]


# ---------- Transfer matching ----------

def match_transfers(txns, acct_types):
    """Pair an outflow from one of your accounts with an equal inflow to another
    of your accounts within 7 days. Returns {transaction_id: (pair_id, category)}."""
    cands = []
    for t in txns:
        if t["user_category"] or t["pending"]:
            continue
        if acct_types.get(t["account_id"]) not in ("depository", "credit"):
            continue
        transferish = (t["pfc_primary"] or "").upper() in ("TRANSFER_IN", "TRANSFER_OUT", "LOAN_PAYMENTS") \
            or TRANSFER_HINT.search(txn_text(t))
        if transferish and t["amount"]:
            cands.append(t)
    by_amount = {}
    for t in cands:
        by_amount.setdefault(round(abs(t["amount"]), 2), []).append(t)
    result = {}
    for amt, group in by_amount.items():
        outs = [t for t in group if t["amount"] > 0]
        ins = [t for t in group if t["amount"] < 0]
        used = set()
        for o in sorted(outs, key=lambda x: x["date"]):
            od = date.fromisoformat(o["date"])
            best, best_gap = None, None
            for i in ins:
                if i["transaction_id"] in used or i["account_id"] == o["account_id"]:
                    continue
                gap = abs((date.fromisoformat(i["date"]) - od).days)
                if gap <= 7 and (best_gap is None or gap < best_gap):
                    best, best_gap = i, gap
            if best:
                used.add(best["transaction_id"])
                pair = f"{o['transaction_id']}~{best['transaction_id']}"
                is_card = "credit" in (acct_types.get(o["account_id"]), acct_types.get(best["account_id"]))
                cat = "Credit Card Payment" if is_card else "Transfer"
                result[o["transaction_id"]] = (pair, cat)
                result[best["transaction_id"]] = (pair, cat)
    return result


# ---------- Apply everything ----------

def categorize_all(conn):
    acct_types = {r["account_id"]: r["type"] for r in conn.execute("SELECT account_id, type FROM accounts")}
    txns = [dict(r) for r in conn.execute("SELECT * FROM transactions")]
    rules = load_rules(conn)
    pairs = match_transfers(txns, acct_types)
    changed = 0
    for t in txns:
        pair_id, cat, src, rule_id = None, None, None, None
        if t["user_category"]:
            cat, src = t["user_category"], "user"
        elif t["transaction_id"] in pairs:
            pair_id, cat = pairs[t["transaction_id"]]
            src = "transfer_match"
        else:
            for rule, rx in rules:
                if rule_matches(rule, rx, t, acct_types.get(t["account_id"])):
                    cat, src, rule_id = rule["category"], "rule", rule["id"]
                    break
            if not cat:
                cat = map_plaid_category(t["pfc_primary"], t["pfc_detailed"], t["amount"])
                # A purchase ON a card can't be a payment TO a card (Plaid sometimes
                # tags e.g. a cafeteria named after a bank as a card payment).
                if cat == "Credit Card Payment" and acct_types.get(t["account_id"]) == "credit" and (t["amount"] or 0) > 0:
                    cat = None
                if cat:
                    low = (t["pfc_confidence"] or "").upper() in ("LOW", "UNKNOWN")
                    src = "plaid_low" if low else "plaid"
            if not cat:
                cat = "Other Income" if (t["amount"] or 0) < 0 else "Uncategorized"
                src = "default"
        if (cat, src, rule_id, pair_id) != (t["category"], t["category_source"], t["rule_id"], t["transfer_pair_id"]):
            conn.execute("UPDATE transactions SET category=?, category_source=?, rule_id=?, transfer_pair_id=? "
                         "WHERE transaction_id=?", (cat, src, rule_id, pair_id, t["transaction_id"]))
            changed += 1
    return changed


def make_similar_rule_from_txn(conn, t, category, name=None):
    """'Auto-tag similar payments going forward' (e.g. Venmo rent): same account,
    same keyword, amount within 10%, within 4 days of the same day of month."""
    amt = abs(t["amount"])
    day = int(t["date"][8:10])
    dmin = (day - 4 - 1) % 31 + 1
    dmax = (day + 4 - 1) % 31 + 1
    text = txn_text(t)
    keyword = "venmo" if "venmo" in text else (t["merchant_name"] or t["name"] or "").strip().lower()
    keyword = keyword.split(" des:")[0][:40]
    cur = conn.execute(
        "INSERT INTO rules(name,pattern,match_type,category,account_id,direction,amount_min,amount_max,"
        "day_min,day_max,priority,is_system,enabled,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,0,1,?)",
        (name or f"{category}: similar '{keyword}' payments (~${amt:,.0f} around day {day})",
         keyword, "contains", category, t["account_id"], "out" if t["amount"] > 0 else "in",
         round(amt * 0.9, 2), round(amt * 1.1, 2), dmin, dmax, 40, now_iso()))
    return cur.lastrowid
