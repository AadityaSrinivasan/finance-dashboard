"""Web server. Listens on 127.0.0.1 only (see __main__.py)."""
import logging
from contextlib import asynccontextmanager
from datetime import date, timedelta
from typing import Optional

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import analytics, cashcalendar, csv_import, future, paystubs, projections, sandbox_demo, sync
from .categories import categorize_all, make_similar_rule_from_txn, txn_text
from .config import BASE_DIR, PORT, PRODUCTION_ITEM_LIMIT, plaid_env, redirect_uri
from .crypto import TokenKeyMissing
from .db import backup_production_db, get_db, get_setting, init_db, now_iso, rows, set_setting
from .sync import PlaidCallError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("app")

STATIC = BASE_DIR / "app" / "static"


@asynccontextmanager
async def lifespan(_app):
    init_db()
    backup_production_db()
    log.info("Plaid environment: %s", plaid_env())
    yield


app = FastAPI(title="Finance Dashboard", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")

ALLOWED_HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}


@app.middleware("http")
async def local_only_guard(request: Request, call_next):
    # Block DNS-rebinding (wrong Host header) and cross-site requests from other
    # web pages: every API call must carry a custom header that browsers won't
    # let other sites add without permission.
    if request.headers.get("host") not in ALLOWED_HOSTS:
        return JSONResponse({"detail": "Forbidden host"}, status_code=403)
    if request.url.path.startswith("/api/") and request.headers.get("x-local-app") != "1":
        return JSONResponse({"detail": "Missing app header"}, status_code=403)
    resp = await call_next(request)
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.exception_handler(PlaidCallError)
async def _plaid_err(request, exc: PlaidCallError):
    i = exc.info
    return JSONResponse(status_code=400, content={
        "detail": i.get("display_message") or i.get("error_message") or "Plaid error",
        "error_code": i.get("error_code"), "error_type": i.get("error_type"), "request_id": i.get("request_id")})


@app.exception_handler(TokenKeyMissing)
async def _key_err(request, exc):
    return JSONResponse(status_code=500, content={"detail": str(exc)})


@app.exception_handler(ValueError)
async def _val_err(request, exc):
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/oauth-return")
def oauth_return():
    # Only used if you set PLAID_REDIRECT_URI (optional HTTPS mode); the page resumes Link.
    return FileResponse(STATIC / "index.html")


# ---------------- Status / connections ----------------

@app.get("/api/status")
def status():
    env = plaid_env()
    with get_db() as conn:
        items = rows(conn.execute(
            "SELECT item_id,institution_id,institution_name,created_at,status,error_code,error_message,"
            "last_sync_at,consent_expiration FROM items WHERE status!='removed' ORDER BY created_at"))
        for it in items:
            it["accounts"] = rows(conn.execute(
                "SELECT account_id,name,display_name,mask,type,subtype FROM accounts WHERE item_id=?", (it["item_id"],)))
            ce = it.get("consent_expiration")
            it["consent_expiring_soon"] = bool(ce and ce[:10] <= (date.today() + timedelta(days=14)).isoformat())
        log_rows = rows(conn.execute("SELECT * FROM item_log ORDER BY id"))
        used = sync.production_slots_used(conn)
        prod_log = []
        if env != "production":
            from .db import db_path
            if db_path("production").exists():
                with get_db("production") as pconn:
                    used = sync.production_slots_used(pconn)
                    prod_log = rows(pconn.execute("SELECT * FROM item_log WHERE env='production' ORDER BY id"))
        else:
            prod_log = [r for r in log_rows if r["env"] == "production"]
        needs_review = conn.execute("SELECT COUNT(*) FROM paystubs WHERE status='needs_review'").fetchone()[0]
        last_sync = conn.execute("SELECT MAX(last_sync_at) FROM items").fetchone()[0]
    return {"env": env, "items": items, "item_log": log_rows, "production_log": prod_log,
            "production_slots_used": used, "production_slot_limit": PRODUCTION_ITEM_LIMIT,
            "redirect_uri": redirect_uri(), "paystubs_needing_review": needs_review, "last_sync": last_sync}


class LinkTokenReq(BaseModel):
    item_id: Optional[str] = None
    account_selection: bool = False
    confirm_production: bool = False
    institution: Optional[str] = None


@app.post("/api/link/token")
def link_token(body: LinkTokenReq):
    env = plaid_env()
    if not body.item_id and env == "production":
        if not body.confirm_production or not (body.institution or "").strip():
            raise ValueError("Production connections need explicit confirmation and the institution name.")
        with get_db() as conn:
            want = body.institution.strip().lower()
            for it in conn.execute("SELECT item_id, institution_name FROM items WHERE status!='removed'"):
                have = (it["institution_name"] or "").lower()
                if want in have or have in want:
                    raise ValueError(f"{it['institution_name']} is already connected. Use 'Fix connection' "
                                     "on it instead of creating a new Item.")
    return sync.create_link_token(body.item_id, body.account_selection)


class ExchangeReq(BaseModel):
    public_token: str
    metadata: dict = {}


@app.post("/api/link/exchange")
def link_exchange(body: ExchangeReq):
    sync.record_link_event("SUCCESS", body.metadata)
    return sync.exchange_public_token(body.public_token, body.metadata)


class UpdateDoneReq(BaseModel):
    item_id: str
    metadata: dict = {}


@app.post("/api/link/update-done")
def link_update_done(body: UpdateDoneReq):
    sync.record_link_event("UPDATE_SUCCESS", body.metadata)
    return sync.update_mode_finished(body.item_id, body.metadata)


class LinkEventReq(BaseModel):
    event: str
    metadata: dict = {}


@app.post("/api/link/event")
def link_event(body: LinkEventReq):
    sync.record_link_event(body.event[:40], body.metadata)
    return {"ok": True}


@app.post("/api/sync")
def sync_everything():
    return {"results": sync.sync_all()}


@app.post("/api/items/{item_id}/sync")
def sync_one(item_id: str):
    return sync.sync_item(item_id)


@app.post("/api/items/{item_id}/live-balance")
def live_balance(item_id: str):
    return sync.refresh_live_balances(item_id)


@app.post("/api/items/{item_id}/remove")
def remove(item_id: str):
    sync.remove_item(item_id)
    return {"ok": True}


@app.post("/api/sandbox/demo")
def sandbox_demo_create():
    return {"results": sandbox_demo.create_demo()}


@app.post("/api/sandbox/reset-login/{item_id}")
def sandbox_reset(item_id: str):
    return sandbox_demo.simulate_login_expired(item_id)


# ---------------- Accounts ----------------

@app.get("/api/accounts")
def accounts():
    with get_db() as conn:
        return analytics.accounts_overview(conn)


@app.get("/api/accounts/all")
def accounts_all():
    with get_db() as conn:
        out = rows(conn.execute("SELECT account_id,item_id,source,name,display_name,mask,type,subtype,"
                                "institution_name,current_balance,hidden,include_in_networth FROM accounts "
                                "ORDER BY institution_name, name"))
        for a in out:
            a["label"] = analytics._acct_label(a)
        return out


class ManualAccount(BaseModel):
    name: str
    institution: str = "Manual"
    type: str = "investment"
    subtype: str = "other"
    balance: float = 0


@app.post("/api/accounts/manual")
def add_manual(body: ManualAccount):
    if body.type not in ("depository", "credit", "investment", "loan", "other"):
        raise ValueError("Unknown account type")
    import uuid
    aid = f"manual-{uuid.uuid4().hex[:12]}"
    with get_db() as conn:
        conn.execute("INSERT INTO accounts(account_id,source,name,type,subtype,institution_name,current_balance,"
                     "currency,balance_updated_at,updated_at) VALUES(?,'manual',?,?,?,?,?,'USD',?,?)",
                     (aid, body.name, body.type, body.subtype, body.institution, body.balance, now_iso(), now_iso()))
        sync.take_snapshot(conn)
    return {"account_id": aid}


class AccountEdit(BaseModel):
    display_name: Optional[str] = None
    hidden: Optional[bool] = None
    include_in_networth: Optional[bool] = None
    current_balance: Optional[float] = None


@app.put("/api/accounts/{account_id}")
def edit_account(account_id: str, body: AccountEdit):
    with get_db() as conn:
        a = conn.execute("SELECT * FROM accounts WHERE account_id=?", (account_id,)).fetchone()
        if not a:
            raise HTTPException(404)
        if body.display_name is not None:
            conn.execute("UPDATE accounts SET display_name=? WHERE account_id=?", (body.display_name or None, account_id))
        if body.hidden is not None:
            conn.execute("UPDATE accounts SET hidden=? WHERE account_id=?", (int(body.hidden), account_id))
        if body.include_in_networth is not None:
            conn.execute("UPDATE accounts SET include_in_networth=? WHERE account_id=?",
                         (int(body.include_in_networth), account_id))
        if body.current_balance is not None:
            if a["source"] != "manual":
                raise ValueError("Balances of connected accounts come from Plaid.")
            conn.execute("UPDATE accounts SET current_balance=?, balance_updated_at=? WHERE account_id=?",
                         (body.current_balance, now_iso(), account_id))
        sync.take_snapshot(conn)
    return {"ok": True}


@app.delete("/api/accounts/{account_id}")
def delete_manual(account_id: str):
    with get_db() as conn:
        a = conn.execute("SELECT source FROM accounts WHERE account_id=?", (account_id,)).fetchone()
        if not a or a["source"] != "manual":
            raise ValueError("Only manual accounts can be deleted here.")
        for tbl in ("transactions", "holdings", "balance_snapshots"):
            conn.execute(f"DELETE FROM {tbl} WHERE account_id=?", (account_id,))
        conn.execute("DELETE FROM accounts WHERE account_id=?", (account_id,))
        sync.take_snapshot(conn)
    return {"ok": True}


@app.get("/api/networth/history")
def nw_history():
    with get_db() as conn:
        return analytics.networth_history(conn)


# ---------------- Transactions / categories / rules ----------------

@app.get("/api/transactions")
def transactions(period: Optional[str] = None, category: Optional[str] = None, account_id: Optional[str] = None,
                 search: Optional[str] = None, kind: Optional[str] = None, review: bool = False, limit: int = 1000):
    start = end = None
    if period:
        start, end, _ = analytics.resolve_period(period)
    with get_db() as conn:
        return analytics.query_transactions(conn, start, end, category, account_id, search, kind, review, limit)


class TxnEdit(BaseModel):
    category: Optional[str] = None       # "" clears your manual category
    note: Optional[str] = None
    apply_to: Optional[str] = None       # None | 'merchant' | 'similar'


@app.patch("/api/transactions/{txn_id}")
def edit_txn(txn_id: str, body: TxnEdit):
    with get_db() as conn:
        t = conn.execute("SELECT * FROM transactions WHERE transaction_id=?", (txn_id,)).fetchone()
        if not t:
            raise HTTPException(404)
        if body.category is not None:
            if body.category and not conn.execute("SELECT 1 FROM categories WHERE name=?", (body.category,)).fetchone():
                raise ValueError("Unknown category")
            conn.execute("UPDATE transactions SET user_category=? WHERE transaction_id=?",
                         (body.category or None, txn_id))
        if body.note is not None:
            conn.execute("UPDATE transactions SET note=? WHERE transaction_id=?", (body.note or None, txn_id))
        rule_id = None
        if body.apply_to == "similar" and body.category:
            rule_id = make_similar_rule_from_txn(conn, dict(t), body.category)
        elif body.apply_to == "merchant" and body.category:
            key = analytics._merchant_key(dict(t))
            if key:
                cur = conn.execute(
                    "INSERT INTO rules(name,pattern,match_type,category,priority,is_system,enabled,created_at) "
                    "VALUES(?,?,?,?,45,0,1,?)", (f"{key} -> {body.category}", key, "contains", body.category, now_iso()))
                rule_id = cur.lastrowid
        changed = categorize_all(conn)
    return {"ok": True, "rule_id": rule_id, "recategorized": changed}


@app.get("/api/categories")
def categories():
    with get_db() as conn:
        return rows(conn.execute("SELECT * FROM categories ORDER BY sort, name"))


class CategoryReq(BaseModel):
    name: str
    kind: str = "spending"
    color: str = "#607d8b"


@app.post("/api/categories")
def add_category(body: CategoryReq):
    if body.kind not in ("spending", "income", "transfer", "investing"):
        raise ValueError("Unknown kind")
    with get_db() as conn:
        conn.execute("INSERT OR IGNORE INTO categories(name,kind,color,sort) VALUES(?,?,?,200)",
                     (body.name.strip(), body.kind, body.color))
    return {"ok": True}


@app.get("/api/rules")
def get_rules():
    with get_db() as conn:
        out = rows(conn.execute("SELECT * FROM rules ORDER BY priority, id"))
        counts = {r["rule_id"]: r["n"] for r in conn.execute(
            "SELECT rule_id, COUNT(*) n FROM transactions WHERE rule_id IS NOT NULL GROUP BY rule_id")}
        labels = analytics.account_labels(conn)
        for r in out:
            r["matches"] = counts.get(r["id"], 0)
            r["account_label"] = labels.get(r["account_id"]) if r["account_id"] else None
        return out


class RuleReq(BaseModel):
    name: str
    pattern: str
    match_type: str = "word"
    category: str
    account_id: Optional[str] = None
    account_type: Optional[str] = None
    direction: Optional[str] = None
    amount_min: Optional[float] = None
    amount_max: Optional[float] = None
    day_min: Optional[int] = None
    day_max: Optional[int] = None
    priority: int = 50
    enabled: bool = True


RULE_COLS = ["name", "pattern", "match_type", "category", "account_id", "account_type", "direction",
             "amount_min", "amount_max", "day_min", "day_max", "priority", "enabled"]


def _rule_vals(body: RuleReq):
    if body.match_type not in ("word", "contains", "regex"):
        raise ValueError("match_type must be word, contains or regex")
    d = body.dict()
    for k in ("account_id", "account_type", "direction"):
        d[k] = d[k] or None
    d["enabled"] = int(d["enabled"])
    return [d[c] for c in RULE_COLS]


@app.post("/api/rules")
def add_rule(body: RuleReq):
    with get_db() as conn:
        conn.execute(f"INSERT INTO rules({','.join(RULE_COLS)},is_system,created_at) "
                     f"VALUES({','.join('?' * len(RULE_COLS))},0,?)", _rule_vals(body) + [now_iso()])
        n = categorize_all(conn)
    return {"ok": True, "recategorized": n}


@app.put("/api/rules/{rule_id}")
def update_rule(rule_id: int, body: RuleReq):
    with get_db() as conn:
        conn.execute(f"UPDATE rules SET {','.join(c + '=?' for c in RULE_COLS)} WHERE id=?",
                     _rule_vals(body) + [rule_id])
        n = categorize_all(conn)
    return {"ok": True, "recategorized": n}


@app.delete("/api/rules/{rule_id}")
def delete_rule(rule_id: int):
    with get_db() as conn:
        conn.execute("DELETE FROM rules WHERE id=?", (rule_id,))
        n = categorize_all(conn)
    return {"ok": True, "recategorized": n}


@app.post("/api/rules/test")
def test_rule(body: RuleReq):
    """Preview which transactions a rule would match, without saving it."""
    from .categories import _compile, rule_matches
    rule = dict(zip(RULE_COLS, _rule_vals(body)))
    rx = _compile(rule)
    with get_db() as conn:
        types = {r["account_id"]: r["type"] for r in conn.execute("SELECT account_id,type FROM accounts")}
        labels = analytics.account_labels(conn)
        hits = [dict(t) for t in conn.execute("SELECT * FROM transactions ORDER BY date DESC")
                if rule_matches(rule, rx, dict(t), types.get(t["account_id"]))]
    for h in hits:
        h["account_label"] = labels.get(h["account_id"])
    return {"count": len(hits), "sample": hits[:50]}


# ---------------- Analytics ----------------

@app.get("/api/spending")
def get_spending(period: str = "ytd"):
    start, end, label = analytics.resolve_period(period)
    with get_db() as conn:
        return {"label": label, **analytics.spending(conn, start, end)}


@app.get("/api/monthly")
def get_monthly(months: int = 12):
    with get_db() as conn:
        return analytics.monthly_summary(conn, months)


@app.get("/api/cashflow")
def get_cashflow(period: str = "ytd"):
    with get_db() as conn:
        return analytics.cashflow(conn, period)


@app.get("/api/recurring")
def get_recurring():
    with get_db() as conn:
        return analytics.recurring(conn)


@app.get("/api/investments")
def get_investments():
    with get_db() as conn:
        return analytics.investments(conn)


@app.get("/api/periods")
def periods():
    with get_db() as conn:
        r = conn.execute("SELECT MIN(date) a, MAX(date) b FROM transactions").fetchone()
    t = date.today()
    first = date.fromisoformat(r["a"]) if r["a"] else t
    months = []
    d = t.replace(day=1)
    while d >= first.replace(day=1) and len(months) < 36:
        months.append(d.strftime("%Y-%m"))
        d = (d - timedelta(days=1)).replace(day=1)
    years = list(range(t.year, first.year - 1, -1))
    return {"months": months, "years": years}


# ---------------- Pay stubs ----------------

@app.get("/api/paystubs")
def get_paystubs():
    with get_db() as conn:
        return {"stubs": paystubs.list_paystubs(conn), "fields": paystubs.FIELDS}


class ImportReq(BaseModel):
    force: bool = False


@app.post("/api/paystubs/import")
def import_paystubs(body: ImportReq):
    return paystubs.import_folder(force=body.force)


class StubEdit(BaseModel):
    fields: dict = {}          # {field: {"current": x, "ytd": y}}
    pay_date: Optional[str] = None
    confirm: bool = False


@app.put("/api/paystubs/{stub_id}")
def edit_paystub(stub_id: int, body: StubEdit):
    with get_db() as conn:
        for f, v in body.fields.items():
            if f not in paystubs.FIELD_NAMES:
                continue
            conn.execute("INSERT INTO paystub_fields(paystub_id,field,current,ytd,confidence) VALUES(?,?,?,?,'user') "
                         "ON CONFLICT(paystub_id,field) DO UPDATE SET current=excluded.current, ytd=excluded.ytd, "
                         "confidence='user'", (stub_id, f, float(v.get("current") or 0), float(v.get("ytd") or 0)))
        if body.pay_date:
            date.fromisoformat(body.pay_date)
            conn.execute("UPDATE paystubs SET pay_date=? WHERE id=?", (body.pay_date, stub_id))
        if body.confirm:
            conn.execute("UPDATE paystubs SET status='confirmed' WHERE id=?", (stub_id,))
    return {"ok": True}


@app.delete("/api/paystubs/{stub_id}")
def delete_paystub(stub_id: int):
    with get_db() as conn:
        conn.execute("DELETE FROM paystub_lines WHERE paystub_id=?", (stub_id,))
        conn.execute("DELETE FROM paystub_fields WHERE paystub_id=?", (stub_id,))
        conn.execute("DELETE FROM paystubs WHERE id=?", (stub_id,))
    return {"ok": True}


# ---------------- CSV ----------------

@app.post("/api/csv/preview")
async def csv_preview(file: UploadFile = File(...)):
    raw = await file.read()
    if len(raw) > 20_000_000:
        raise ValueError("File too large")
    fid = csv_import.save_upload(file.filename, raw)
    return csv_import.preview(fid)


class CsvImportReq(BaseModel):
    file_id: str
    account_id: str
    flip: bool = False


@app.post("/api/csv/repreview")
def csv_repreview(body: CsvImportReq):
    return csv_import.preview(body.file_id, body.flip)


@app.post("/api/csv/import")
def csv_do_import(body: CsvImportReq):
    return csv_import.run_import(body.file_id, body.account_id, body.flip)


# ---------------- 30-day cash calendar ----------------

@app.get("/api/cash-calendar")
def cash_calendar(days: int = 30):
    with get_db() as conn:
        return cashcalendar.build(conn, max(7, min(90, days)))


class PlannedReq(BaseModel):
    name: str
    amount: float
    first_date: str
    repeat_monthly: bool = True


@app.post("/api/planned")
def planned_add(body: PlannedReq):
    with get_db() as conn:
        cashcalendar.add_planned(conn, body.name, body.amount, body.first_date, body.repeat_monthly)
    return {"ok": True}


@app.delete("/api/planned/{pid}")
def planned_delete(pid: int):
    with get_db() as conn:
        cashcalendar.delete_planned(conn, pid)
    return {"ok": True}


# ---------------- Future (projections) ----------------

@app.get("/api/future/baseline")
def future_baseline():
    with get_db() as conn:
        return future.baseline(conn)


class ProjectReq(BaseModel):
    inputs: dict


SHARED_KEYS = ("horizon_years", "inflation_pct", "show_real", "range_pp", "start_date")


@app.post("/api/future/project")
def future_project(body: ProjectReq):
    with get_db() as conn:
        base_inputs = future.baseline(conn)["inputs"]
    # 'My current habits' keeps your real-data defaults; only the view settings are shared.
    for k in SHARED_KEYS:
        if k in body.inputs:
            base_inputs[k] = body.inputs[k]
    return {"adjusted": projections.project(body.inputs), "baseline": projections.project(base_inputs)}


@app.get("/api/future/scenarios")
def future_scenarios():
    with get_db() as conn:
        return future.list_scenarios(conn)


class ScenarioReq(BaseModel):
    name: str
    inputs: dict


@app.post("/api/future/scenarios")
def future_save(body: ScenarioReq):
    with get_db() as conn:
        return {"id": future.save_scenario(conn, body.name, body.inputs)}


@app.delete("/api/future/scenarios/{sid}")
def future_delete(sid: int):
    with get_db() as conn:
        future.delete_scenario(conn, sid)
    return {"ok": True}


class FutureSettingsReq(BaseModel):
    k401_limit: Optional[float] = None
    k401_limit_year: Optional[int] = None
    marginal_tax_pct: Optional[float] = None


@app.put("/api/future/settings")
def future_settings(body: FutureSettingsReq):
    with get_db() as conn:
        future.ensure(conn)
        if body.k401_limit is not None:
            if not 1000 <= body.k401_limit <= 200000:
                raise ValueError("That 401(k) limit looks wrong.")
            set_setting(conn, "future_k401_limit", body.k401_limit)
        if body.k401_limit_year is not None:
            set_setting(conn, "future_k401_limit_year", body.k401_limit_year)
        if body.marginal_tax_pct is not None:
            set_setting(conn, "future_marginal_tax_pct", max(0.0, min(60.0, body.marginal_tax_pct)))
    return {"ok": True}


# ---------------- Settings / verification ----------------

SETTING_KEYS = ["invest_include_espp", "invest_include_hsa", "invest_include_employer_match"]


@app.get("/api/settings")
def get_settings():
    with get_db() as conn:
        return {k: get_setting(conn, k) == "1" for k in SETTING_KEYS}


@app.put("/api/settings")
def put_settings(body: dict):
    with get_db() as conn:
        for k in SETTING_KEYS:
            if k in body:
                set_setting(conn, k, "1" if body[k] else "0")
    return {"ok": True}


@app.get("/api/verify")
def verify():
    """Numbers to double-check against your bank apps (build step 6)."""
    with get_db() as conn:
        labels = analytics.account_labels(conn)
        accts = rows(conn.execute("SELECT account_id,type,subtype,current_balance,available_balance,"
                                  "balance_updated_at,institution_name FROM accounts WHERE hidden=0"))
        for a in accts:
            a["label"] = labels.get(a["account_id"])
        pairs = rows(conn.execute(
            "SELECT transfer_pair_id, transaction_id, account_id, date, name, amount, category FROM transactions "
            "WHERE transfer_pair_id IS NOT NULL ORDER BY date DESC LIMIT 200"))
        for p in pairs:
            p["account_label"] = labels.get(p["account_id"])
        # Money out of bank that looks like a transfer but wasn't matched or excluded
        kinds = {r["name"]: r["kind"] for r in conn.execute("SELECT name,kind FROM categories")}
        suspicious = []
        for t in conn.execute("SELECT * FROM transactions WHERE date>=? ORDER BY date DESC",
                              ((date.today() - timedelta(days=365)).isoformat(),)):
            t = dict(t)
            # Payments to/from other people (Zelle, Venmo) are expected; skip those categories.
            if t["category"] in ("Payments to people", "Reimbursements", "Family support", "Betting", "Rent"):
                continue
            if kinds.get(t["category"]) in ("spending", "income") and \
                    any(w in txn_text(t) for w in ("transfer", "xfer", "payment to", "crd", "epay",
                                                   "brokerage", "invest", "securities")):
                t["account_label"] = labels.get(t["account_id"])
                suspicious.append(t)
        stubs = paystubs.list_paystubs(conn)
        cf = analytics.cashflow(conn, "ytd")
        inv = analytics.investments(conn)
        inv_check = []
        for a in accts:
            if a["type"] != "investment":
                continue
            held = sum(h["value"] for h in inv["holdings"] if h["account_id"] == a["account_id"])
            inv_check.append({"label": a["label"], "balance": a["current_balance"], "holdings_total": round(held, 2),
                              "difference": round((a["current_balance"] or 0) - held, 2)})
    return {"accounts": accts, "investment_check": inv_check,
            "transfer_pairs": pairs, "possible_missed_transfers": suspicious[:100],
            "paystubs": stubs, "ytd_cashflow_notes": cf["payroll"]["notes"],
            "ytd_bank_paychecks": cf["payroll"]["bank_paycheck_total"], "ytd_takehome_used": cf["take_home"]}
