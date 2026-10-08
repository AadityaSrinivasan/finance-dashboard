"""Fallback CSV import for when a Plaid connection is broken.

Recognizes common export layouts from banks, card issuers and brokerages
(activity reports, account history and positions files), plus most
other bank CSVs with Date / Description / Amount (or Debit / Credit) columns.

Duplicates are skipped:
  * rows already imported from a CSV before (same account, date, amount, description)
  * rows that Plaid already pulled (same account, same amount, date within 3 days)
"""
import csv
import hashlib
import io
import re
import uuid
from datetime import date, datetime, timedelta

from .categories import categorize_all
from .config import CSV_DIR
from .db import get_db, now_iso
from .sync import take_snapshot

DATE_COLS = ["transaction date", "trans. date", "date", "posting date", "posted date", "activity date",
             "run date", "trade date", "settlement date"]
DESC_COLS = ["description", "payee", "action", "name", "merchant", "memo", "transaction description"]
AMOUNT_COLS = ["amount", "amount ($)", "amount (usd)", "transaction amount", "net amount"]


def _norm(h):
    return (h or "").strip().strip("﻿").lower()


def parse_amount(s):
    if s is None:
        return None
    s = str(s).strip().replace("$", "").replace(",", "").replace("USD", "").strip()
    if not s or s in ("-", "--"):
        return None
    neg = s.startswith("(") and s.endswith(")") or s.startswith("-")
    s = s.strip("()-+ ")
    try:
        v = float(s)
    except ValueError:
        return None
    return -v if neg else v


def parse_date(s):
    s = (s or "").strip().split(" ")[0]
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y", "%m-%d-%Y", "%d-%b-%Y", "%b %d %Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _find(headers, names):
    for n in names:
        if n in headers:
            return headers.index(n)
    return None


def read_table(raw: bytes):
    """Find the real header row (some banks put a summary block above it)."""
    text = raw.decode("utf-8-sig", errors="replace")
    rows = list(csv.reader(io.StringIO(text)))
    for i, r in enumerate(rows[:40]):
        h = [_norm(x) for x in r]
        has_date = _find(h, DATE_COLS) is not None
        is_positions = "symbol" in h and any(x in h for x in ("current value", "market value", "value"))
        has_amt = _find(h, AMOUNT_COLS) is not None or ("debit" in h and "credit" in h)
        if (has_date and has_amt) or is_positions:
            return h, [x for x in rows[i + 1:] if any(c.strip() for c in x)]
    raise ValueError("Couldn't find a header row with Date and Amount columns in this CSV.")


def detect(headers):
    h = set(headers)
    if "symbol" in h and any(x in h for x in ("current value", "market value", "value")) and "quantity" in h:
        return "positions", "Investment positions (holdings)"
    if "trans code" in h and "instrument" in h:
        return "transactions", "Brokerage activity report"
    if "run date" in h and "action" in h:
        return "transactions", "Brokerage account history"
    if "transaction date" in h and "post date" in h and "category" in h:
        return "transactions", "Credit card export"
    if "details" in h and "posting date" in h:
        return "transactions", "Checking export"
    if "running bal." in h:
        return "transactions", "Bank export (with running balance)"
    return "transactions", "Generic bank CSV"


def parse_transactions(headers, rows, flip=False):
    """Returns rows in Plaid sign convention: positive = money out."""
    di, ds, ai = _find(headers, DATE_COLS), _find(headers, DESC_COLS), _find(headers, AMOUNT_COLS)
    dbi = headers.index("debit") if "debit" in headers else None
    cri = headers.index("credit") if "credit" in headers else None
    out, bad = [], 0
    for r in rows:
        try:
            d = parse_date(r[di]) if di is not None else None
            desc = " ".join(r[ds].split()) if ds is not None and ds < len(r) else ""
            if ai is not None and ai < len(r) and parse_amount(r[ai]) is not None:
                amt = -parse_amount(r[ai])  # CSV: negative = money out
            elif dbi is not None or cri is not None:
                debit = parse_amount(r[dbi]) if dbi is not None and dbi < len(r) else None
                credit = parse_amount(r[cri]) if cri is not None and cri < len(r) else None
                amt = abs(debit or 0) - abs(credit or 0)
            else:
                amt = None
        except (IndexError, ValueError):
            d, amt = None, None
        if not d or amt is None or not desc or "beginning balance" in desc.lower():
            bad += 1
            continue
        out.append({"date": d.isoformat(), "description": desc, "amount": -amt if flip else amt})
    return out, bad


def parse_positions(headers, rows):
    si, ni = headers.index("symbol"), _find(headers, ["description", "name", "security name"])
    qi = headers.index("quantity")
    vi = _find(headers, ["current value", "market value", "value"])
    pi = _find(headers, ["last price", "price", "current price"])
    ci = _find(headers, ["cost basis total", "cost basis", "total cost basis"])
    out = []
    for r in rows:
        if len(r) <= max(si, qi, vi):
            continue
        sym = r[si].strip().strip("*")
        val = parse_amount(r[vi])
        if not sym or val is None or sym.lower().startswith(("pending", "account total")):
            continue
        name = r[ni].strip() if ni is not None and ni < len(r) else sym
        typ = "cash" if re.search(r"money market|core|spaxx|fdrxx|cash", f"{sym} {name}", re.I) else "other"
        out.append({"ticker": sym, "name": name, "quantity": parse_amount(r[qi]), "value": val,
                    "price": parse_amount(r[pi]) if pi is not None and pi < len(r) else None,
                    "cost_basis": parse_amount(r[ci]) if ci is not None and ci < len(r) else None, "type": typ})
    return out


def save_upload(filename: str, raw: bytes) -> str:
    CSV_DIR.mkdir(exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", filename or "upload.csv")[-80:]
    name = f"{datetime.now():%Y%m%d-%H%M%S}-{safe}"
    (CSV_DIR / name).write_bytes(raw)
    return name


def _load(file_id: str):
    path = (CSV_DIR / file_id).resolve()
    if path.parent != CSV_DIR.resolve() or not path.exists():
        raise ValueError("Upload not found")
    return path.read_bytes()


def preview(file_id: str, flip=False):
    headers, rows = read_table(_load(file_id))
    kind, fmt = detect(headers)
    if kind == "positions":
        parsed = parse_positions(headers, rows)
        return {"file_id": file_id, "kind": kind, "format": fmt, "headers": headers, "count": len(parsed),
                "sample": parsed[:15], "total_value": round(sum(p["value"] for p in parsed), 2)}
    parsed, bad = parse_transactions(headers, rows, flip)
    return {"file_id": file_id, "kind": kind, "format": fmt, "headers": headers, "count": len(parsed),
            "skipped_rows": bad, "sample": parsed[:15],
            "date_range": [min(p["date"] for p in parsed), max(p["date"] for p in parsed)] if parsed else None}


def run_import(file_id: str, account_id: str, flip=False) -> dict:
    headers, rows = read_table(_load(file_id))
    kind, fmt = detect(headers)
    with get_db() as conn:
        acct = conn.execute("SELECT * FROM accounts WHERE account_id=?", (account_id,)).fetchone()
        if not acct:
            raise ValueError("Pick an account to import into.")
        if kind == "positions":
            return _import_positions(conn, acct, parse_positions(headers, rows))
        parsed, bad = parse_transactions(headers, rows, flip)
        imported = dup_csv = dup_plaid = 0
        claimed = set()
        seen = {}
        for p in parsed:
            key = f"{account_id}|{p['date']}|{p['amount']:.2f}|{p['description'].lower()}"
            seen[key] = seen.get(key, 0) + 1
            h = hashlib.sha1(f"{key}|{seen[key]}".encode()).hexdigest()
            if conn.execute("SELECT 1 FROM transactions WHERE import_hash=?", (h,)).fetchone():
                dup_csv += 1
                continue
            d = date.fromisoformat(p["date"])
            match = conn.execute(
                "SELECT transaction_id FROM transactions WHERE account_id=? AND source='plaid' "
                "AND ABS(amount-?)<0.005 AND date BETWEEN ? AND ? ORDER BY ABS(julianday(date)-julianday(?))",
                (account_id, p["amount"], str(d - timedelta(days=3)), str(d + timedelta(days=3)), p["date"])).fetchall()
            match = [m for m in match if m["transaction_id"] not in claimed]
            if match:
                claimed.add(match[0]["transaction_id"])
                dup_plaid += 1
                continue
            user_cat = "Transfer" if acct["type"] == "investment" else None  # trades/deposits aren't spending
            conn.execute(
                "INSERT INTO transactions(transaction_id,account_id,source,date,name,original_description,amount,"
                "currency,pending,user_category,note,import_hash,created_at,updated_at) "
                "VALUES(?,?,'csv',?,?,?,?,'USD',0,?,?,?,?,?)",
                (f"csv-{uuid.uuid4().hex}", account_id, p["date"], p["description"], p["description"], p["amount"],
                 user_cat, f"Imported from {fmt} CSV", h, now_iso(), now_iso()))
            imported += 1
        categorize_all(conn)
    return {"format": fmt, "imported": imported, "already_imported": dup_csv, "already_from_plaid": dup_plaid,
            "unreadable_rows": bad}


def _import_positions(conn, acct, positions):
    aid = acct["account_id"]
    conn.execute("DELETE FROM holdings WHERE account_id=?", (aid,))  # CSV snapshot replaces older data
    for p in positions:
        sid = "csv-" + hashlib.sha1(f"{p['ticker']}|{p['name']}".encode()).hexdigest()[:16]
        conn.execute("INSERT OR REPLACE INTO securities(security_id,name,ticker,type,is_cash_equivalent) "
                     "VALUES(?,?,?,?,?)", (sid, p["name"], p["ticker"], p["type"], 1 if p["type"] == "cash" else 0))
        conn.execute("INSERT OR REPLACE INTO holdings(account_id,security_id,quantity,institution_price,"
                     "institution_value,cost_basis,price_as_of,source,updated_at) VALUES(?,?,?,?,?,?,?,'csv',?)",
                     (aid, sid, p["quantity"], p["price"], p["value"], p["cost_basis"], date.today().isoformat(),
                      now_iso()))
    total = round(sum(p["value"] for p in positions), 2)
    if acct["source"] == "manual" or acct["type"] == "investment":
        conn.execute("UPDATE accounts SET current_balance=?, balance_updated_at=? WHERE account_id=?",
                     (total, now_iso(), aid))
    take_snapshot(conn)
    return {"format": "positions", "holdings": len(positions), "total_value": total}
