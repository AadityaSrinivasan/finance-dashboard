"""SQLite storage. One database file per Plaid environment so Sandbox test data
never mixes with your real Production data."""
import shutil
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime

from .config import DATA_DIR, plaid_env

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS items (
  item_id TEXT PRIMARY KEY,
  institution_id TEXT,
  institution_name TEXT,
  access_token_enc TEXT NOT NULL,
  created_at TEXT,
  status TEXT DEFAULT 'ok',          -- ok | login_required | pending_disconnect | error | removed
  error_code TEXT,
  error_message TEXT,
  txn_cursor TEXT,
  last_sync_at TEXT,
  last_sync_result TEXT,
  consent_expiration TEXT,
  link_session_id TEXT,
  products TEXT,
  removed_at TEXT
);

-- Permanent log of every Item ever created. Never deleted: on the Trial plan,
-- removing an Item does not give the slot back.
CREATE TABLE IF NOT EXISTS item_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  env TEXT,
  item_id TEXT,
  institution_id TEXT,
  institution_name TEXT,
  created_at TEXT,
  event TEXT,                        -- created | update_mode | duplicate_not_exchanged | removed
  counts_toward_limit INTEGER DEFAULT 0,
  link_session_id TEXT,
  note TEXT
);

CREATE TABLE IF NOT EXISTS accounts (
  account_id TEXT PRIMARY KEY,
  item_id TEXT,
  source TEXT,                       -- plaid | manual
  name TEXT,
  official_name TEXT,
  display_name TEXT,
  mask TEXT,
  type TEXT,
  subtype TEXT,
  institution_name TEXT,
  current_balance REAL,
  available_balance REAL,
  credit_limit REAL,
  currency TEXT,
  persistent_account_id TEXT,
  hidden INTEGER DEFAULT 0,
  include_in_networth INTEGER DEFAULT 1,
  balance_updated_at TEXT,
  updated_at TEXT
);

CREATE TABLE IF NOT EXISTS transactions (
  transaction_id TEXT PRIMARY KEY,
  account_id TEXT,
  source TEXT,                       -- plaid | csv | manual
  date TEXT,
  authorized_date TEXT,
  name TEXT,
  merchant_name TEXT,
  original_description TEXT,
  amount REAL,                       -- Plaid convention: positive = money out, negative = money in
  currency TEXT,
  pending INTEGER DEFAULT 0,
  pending_transaction_id TEXT,
  pfc_primary TEXT,
  pfc_detailed TEXT,
  pfc_confidence TEXT,
  payment_channel TEXT,
  category TEXT,
  category_source TEXT,              -- user | transfer_match | rule | plaid | default
  rule_id INTEGER,
  user_category TEXT,
  transfer_pair_id TEXT,
  note TEXT,
  import_hash TEXT,
  created_at TEXT,
  updated_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_txn_date ON transactions(date);
CREATE INDEX IF NOT EXISTS ix_txn_account ON transactions(account_id);
CREATE INDEX IF NOT EXISTS ix_txn_hash ON transactions(import_hash);

CREATE TABLE IF NOT EXISTS categories (
  name TEXT PRIMARY KEY,
  kind TEXT NOT NULL,                -- spending | income | transfer | investing
  color TEXT,
  sort INTEGER DEFAULT 100
);

CREATE TABLE IF NOT EXISTS rules (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT,
  pattern TEXT,                      -- words/phrases separated by |
  match_type TEXT DEFAULT 'word',    -- word | contains | regex
  category TEXT NOT NULL,
  account_id TEXT,
  account_type TEXT,                 -- depository | credit | NULL
  direction TEXT,                    -- out | in | NULL
  amount_min REAL,
  amount_max REAL,
  day_min INTEGER,
  day_max INTEGER,
  priority INTEGER DEFAULT 100,      -- lower runs first
  enabled INTEGER DEFAULT 1,
  is_system INTEGER DEFAULT 0,
  created_at TEXT
);

CREATE TABLE IF NOT EXISTS liabilities (
  account_id TEXT PRIMARY KEY,
  last_statement_balance REAL,
  last_statement_date TEXT,
  minimum_payment REAL,
  next_due_date TEXT,
  last_payment_amount REAL,
  last_payment_date TEXT,
  is_overdue INTEGER,
  purchase_apr REAL,
  updated_at TEXT
);

CREATE TABLE IF NOT EXISTS securities (
  security_id TEXT PRIMARY KEY,
  name TEXT,
  ticker TEXT,
  type TEXT,
  subtype TEXT,
  is_cash_equivalent INTEGER,
  close_price REAL,
  close_price_as_of TEXT
);

CREATE TABLE IF NOT EXISTS holdings (
  account_id TEXT,
  security_id TEXT,
  quantity REAL,
  institution_price REAL,
  institution_value REAL,
  cost_basis REAL,
  price_as_of TEXT,
  source TEXT,
  updated_at TEXT,
  PRIMARY KEY (account_id, security_id)
);

CREATE TABLE IF NOT EXISTS balance_snapshots (
  date TEXT,
  account_id TEXT,
  balance REAL,
  PRIMARY KEY (date, account_id)
);

CREATE TABLE IF NOT EXISTS networth_snapshots (
  date TEXT PRIMARY KEY,
  assets REAL,
  liabilities REAL,
  net_worth REAL
);

CREATE TABLE IF NOT EXISTS paystubs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  file_name TEXT,
  file_hash TEXT UNIQUE,
  employer TEXT,
  pay_date TEXT,
  period_start TEXT,
  period_end TEXT,
  status TEXT,                       -- needs_review | confirmed
  parse_notes TEXT,
  imported_at TEXT
);

CREATE TABLE IF NOT EXISTS paystub_lines (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  paystub_id INTEGER,
  section TEXT,
  label TEXT,
  current REAL,
  ytd REAL
);

CREATE TABLE IF NOT EXISTS paystub_fields (
  paystub_id INTEGER,
  field TEXT,
  current REAL,
  ytd REAL,
  confidence TEXT,                   -- high | low | missing | user
  PRIMARY KEY (paystub_id, field)
);

CREATE TABLE IF NOT EXISTS link_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT,
  event TEXT,
  institution_id TEXT,
  institution_name TEXT,
  link_session_id TEXT,
  error_type TEXT,
  error_code TEXT,
  request_id TEXT,
  detail TEXT
);

CREATE TABLE IF NOT EXISTS sync_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  at TEXT,
  item_id TEXT,
  step TEXT,
  result TEXT,
  request_id TEXT
);
"""


def db_path(env: str = None):
    return DATA_DIR / f"finance_{env or plaid_env()}.db"


def connect(env: str = None) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path(env), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


@contextmanager
def get_db(env: str = None):
    conn = connect(env)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def today() -> str:
    return date.today().isoformat()


def get_setting(conn, key, default=None):
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn, key, value):
    conn.execute("INSERT INTO settings(key,value) VALUES(?,?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))


def rows(cur):
    return [dict(r) for r in cur.fetchall()]


def backup_production_db(keep: int = 30):
    """Copy the Production database (which holds your irreplaceable access tokens)
    into data/backups once per day."""
    src = db_path("production")
    if not src.exists():
        return None
    bdir = DATA_DIR / "backups"
    bdir.mkdir(exist_ok=True)
    dest = bdir / f"finance_production_{date.today().isoformat()}.db"
    if not dest.exists():
        with sqlite3.connect(src) as s, sqlite3.connect(dest) as d:
            s.backup(d)
    backups = sorted(bdir.glob("finance_production_*.db"))
    for old in backups[:-keep]:
        old.unlink()
    return dest


def init_db(env: str = None):
    from .categories import categorize_all, seed_categories_and_rules  # avoid import cycle
    env = env or plaid_env()
    first_time = not db_path(env).exists()
    with get_db(env) as conn:
        conn.executescript(SCHEMA)
        seed_categories_and_rules(conn)
        categorize_all(conn)  # apply any updated rules/mappings
        defaults = {"invest_include_espp": "1", "invest_include_hsa": "0",
                    "invest_include_employer_match": "0"}
        for k, v in defaults.items():
            if get_setting(conn, k) is None:
                set_setting(conn, k, v)
    if first_time and env == "production" and db_path("sandbox").exists():
        copy_user_config("sandbox", "production")


def copy_user_config(src_env: str, dest_env: str):
    """When Production is first used, carry over your rules, settings, custom
    categories and pay stubs from Sandbox. Plaid data and tokens are never copied."""
    with get_db(src_env) as s, get_db(dest_env) as d:
        d.execute("DELETE FROM rules")
        for r in s.execute("SELECT * FROM rules WHERE account_id IS NULL"):
            r = dict(r)
            r.pop("id")
            cols = ",".join(r.keys())
            d.execute(f"INSERT INTO rules({cols}) VALUES({','.join('?' * len(r))})", list(r.values()))
        for r in s.execute("SELECT * FROM categories"):
            d.execute("INSERT OR IGNORE INTO categories(name,kind,color,sort) VALUES(?,?,?,?)",
                      (r["name"], r["kind"], r["color"], r["sort"]))
        for r in s.execute("SELECT * FROM settings"):
            set_setting(d, r["key"], r["value"])
        for p in s.execute("SELECT * FROM paystubs"):
            p = dict(p)
            old_id = p.pop("id")
            cols = ",".join(p.keys())
            cur = d.execute(f"INSERT OR IGNORE INTO paystubs({cols}) VALUES({','.join('?' * len(p))})",
                            list(p.values()))
            if not cur.rowcount:
                continue
            new_id = cur.lastrowid
            for ln in s.execute("SELECT section,label,current,ytd FROM paystub_lines WHERE paystub_id=?", (old_id,)):
                d.execute("INSERT INTO paystub_lines(paystub_id,section,label,current,ytd) VALUES(?,?,?,?,?)",
                          (new_id, *tuple(ln)))
            for f in s.execute("SELECT field,current,ytd,confidence FROM paystub_fields WHERE paystub_id=?", (old_id,)):
                d.execute("INSERT INTO paystub_fields(paystub_id,field,current,ytd,confidence) VALUES(?,?,?,?,?)",
                          (new_id, *tuple(f)))
