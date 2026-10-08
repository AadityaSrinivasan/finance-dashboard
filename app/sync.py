"""Talks to Plaid. Read-only products only: Transactions, Investments,
Liabilities, Balance. Nothing here can move money.

Tokens are decrypted only in memory, right before a Plaid call, and are never
logged or returned to the browser."""
import json
import logging
import random
import time
from datetime import date

import plaid
from plaid.model.accounts_balance_get_request import AccountsBalanceGetRequest
from plaid.model.accounts_get_request import AccountsGetRequest
from plaid.model.country_code import CountryCode
from plaid.model.institutions_get_by_id_request import InstitutionsGetByIdRequest
from plaid.model.investments_holdings_get_request import InvestmentsHoldingsGetRequest
from plaid.model.item_get_request import ItemGetRequest
from plaid.model.item_public_token_exchange_request import ItemPublicTokenExchangeRequest
from plaid.model.item_remove_request import ItemRemoveRequest
from plaid.model.liabilities_get_request import LiabilitiesGetRequest
from plaid.model.link_token_create_request import LinkTokenCreateRequest
from plaid.model.link_token_create_request_update import LinkTokenCreateRequestUpdate
from plaid.model.link_token_create_request_user import LinkTokenCreateRequestUser
from plaid.model.link_token_transactions import LinkTokenTransactions
from plaid.model.personal_finance_category_version import PersonalFinanceCategoryVersion
from plaid.model.products import Products
from plaid.model.transactions_sync_request import TransactionsSyncRequest
from plaid.model.transactions_sync_request_options import TransactionsSyncRequestOptions

from . import crypto
from .categories import categorize_all
from .config import PRODUCTION_ITEM_LIMIT, redirect_uri
from .db import get_db, now_iso, today
from .plaid_client import get_client, plaid_error

log = logging.getLogger("sync")

RETRYABLE = {"INSTITUTION_DOWN", "INSTITUTION_NOT_RESPONDING", "PRODUCT_NOT_READY",
             "RATE_LIMIT_EXCEEDED", "INTERNAL_SERVER_ERROR", "PLANNED_MAINTENANCE"}
NEEDS_USER = {"ITEM_LOGIN_REQUIRED", "INVALID_CREDENTIALS", "ITEM_LOCKED", "ACCESS_NOT_GRANTED",
              "PENDING_EXPIRATION", "PENDING_DISCONNECT", "INVALID_UPDATED_USERNAME"}


class PlaidCallError(Exception):
    def __init__(self, info: dict):
        self.info = info
        super().__init__(f"{info.get('error_type')}/{info.get('error_code')}")


def call(fn, req, retries=3):
    """Call a Plaid endpoint with capped, jittered exponential backoff for
    transient errors only. Never logs request contents."""
    for attempt in range(retries + 1):
        try:
            return fn(req).to_dict()
        except plaid.ApiException as e:
            info = plaid_error(e)
            if info.get("error_code") in RETRYABLE and attempt < retries:
                time.sleep(min(8, 2 ** attempt) + random.random())
                continue
            log.warning("Plaid error %s/%s request_id=%s", info.get("error_type"),
                        info.get("error_code"), info.get("request_id"))
            raise PlaidCallError(info)


# ---------------- Link ----------------

def production_slots_used(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM item_log WHERE env='production' AND counts_toward_limit=1").fetchone()[0]


def create_link_token(item_id: str = None, account_selection: bool = False) -> dict:
    client, env = get_client()
    kwargs = dict(
        user=LinkTokenCreateRequestUser(client_user_id="local-owner"),
        client_name="My Finance Dashboard",
        country_codes=[CountryCode("US")],
        language="en",
    )
    uri = redirect_uri()
    if uri:
        kwargs["redirect_uri"] = uri  # must be sent on every call, including update mode
    if item_id:
        # Update mode: re-login (or add accounts) on an EXISTING Item. Never creates a new Item.
        with get_db() as conn:
            row = conn.execute("SELECT access_token_enc FROM items WHERE item_id=?", (item_id,)).fetchone()
        if not row:
            raise ValueError("Unknown connection")
        kwargs["access_token"] = crypto.decrypt(row["access_token_enc"])
        if account_selection:
            kwargs["update"] = LinkTokenCreateRequestUpdate(account_selection_enabled=True)
    else:
        if env == "production":
            with get_db() as conn:
                if production_slots_used(conn) >= PRODUCTION_ITEM_LIMIT:
                    raise ValueError("All 10 Production Items are used. New connections are blocked.")
        kwargs["products"] = [Products("transactions")]
        kwargs["additional_consented_products"] = [Products("investments"), Products("liabilities")]
        kwargs["transactions"] = LinkTokenTransactions(days_requested=730)
    resp = call(client.link_token_create, LinkTokenCreateRequest(**kwargs))
    return {"link_token": resp["link_token"], "expiration": str(resp.get("expiration")), "env": env}


def record_link_event(event: str, meta: dict):
    meta = meta or {}
    inst = meta.get("institution") or {}
    err = meta.get("error") or {}
    with get_db() as conn:
        conn.execute(
            "INSERT INTO link_events(created_at,event,institution_id,institution_name,link_session_id,"
            "error_type,error_code,request_id,detail) VALUES(?,?,?,?,?,?,?,?,?)",
            (now_iso(), event, inst.get("institution_id"), inst.get("name"), meta.get("link_session_id"),
             err.get("error_type"), err.get("error_code"), meta.get("request_id"),
             json.dumps({"status": meta.get("status"), "accounts": len(meta.get("accounts") or [])})))


def institution_name(institution_id: str, fallback: str = None) -> str:
    client, _ = get_client()
    try:
        r = call(client.institutions_get_by_id, InstitutionsGetByIdRequest(
            institution_id=institution_id, country_codes=[CountryCode("US")]))
        return r["institution"]["name"]
    except PlaidCallError:
        return fallback or institution_id


def exchange_public_token(public_token: str, meta: dict) -> dict:
    """Exchange right away (public tokens are single-use and expire in minutes)
    and persist before responding. Refuses to exchange a duplicate institution."""
    client, env = get_client()
    inst = (meta or {}).get("institution") or {}
    inst_id = inst.get("institution_id")
    with get_db() as conn:
        dup = conn.execute("SELECT item_id, institution_name FROM items WHERE institution_id=? "
                           "AND status!='removed'", (inst_id,)).fetchone() if inst_id else None
        if dup:
            conn.execute(
                "INSERT INTO item_log(env,item_id,institution_id,institution_name,created_at,event,"
                "counts_toward_limit,link_session_id,note) VALUES(?,?,?,?,?,?,?,?,?)",
                (env, None, inst_id, inst.get("name"), now_iso(), "duplicate_not_exchanged",
                 1 if env == "production" else 0, meta.get("link_session_id"),
                 "Institution already connected; token was NOT exchanged. Plaid may still count this "
                 "login as an Item, so it is counted here to be safe."))
            return {"ok": False, "duplicate": True,
                    "message": f"{dup['institution_name']} is already connected. This new login was not saved. "
                               "Use 'Fix connection' on the existing one instead."}

    resp = call(client.item_public_token_exchange, ItemPublicTokenExchangeRequest(public_token=public_token))
    access_token, item_id = resp["access_token"], resp["item_id"]
    name = institution_name(inst_id, inst.get("name")) if inst_id else inst.get("name")
    with get_db() as conn:
        conn.execute(
            "INSERT INTO items(item_id,institution_id,institution_name,access_token_enc,created_at,status,"
            "link_session_id,products) VALUES(?,?,?,?,?,?,?,?)",
            (item_id, inst_id, name, crypto.encrypt(access_token), now_iso(), "ok",
             meta.get("link_session_id"), "transactions,investments,liabilities"))
        conn.execute(
            "INSERT INTO item_log(env,item_id,institution_id,institution_name,created_at,event,"
            "counts_toward_limit,link_session_id) VALUES(?,?,?,?,?,?,?,?)",
            (env, item_id, inst_id, name, now_iso(), "created", 1 if env == "production" else 0,
             meta.get("link_session_id")))
    del access_token
    result = sync_item(item_id)
    return {"ok": True, "item_id": item_id, "institution": name, "sync": result}


def update_mode_finished(item_id: str, meta: dict):
    _, env = get_client()
    with get_db() as conn:
        conn.execute("UPDATE items SET status='ok', error_code=NULL, error_message=NULL WHERE item_id=?", (item_id,))
        row = conn.execute("SELECT institution_id, institution_name FROM items WHERE item_id=?", (item_id,)).fetchone()
        conn.execute(
            "INSERT INTO item_log(env,item_id,institution_id,institution_name,created_at,event,"
            "counts_toward_limit,link_session_id) VALUES(?,?,?,?,?,?,0,?)",
            (env, item_id, row["institution_id"], row["institution_name"], now_iso(), "update_mode",
             (meta or {}).get("link_session_id")))
    return sync_item(item_id)


def remove_item(item_id: str):
    client, env = get_client()
    if env == "production":
        raise ValueError("Removing Production connections is disabled: on the Trial plan it would "
                         "permanently waste one of your 10 Item slots.")
    with get_db() as conn:
        row = conn.execute("SELECT * FROM items WHERE item_id=?", (item_id,)).fetchone()
        if not row:
            raise ValueError("Unknown connection")
        try:
            call(client.item_remove, ItemRemoveRequest(access_token=crypto.decrypt(row["access_token_enc"])))
        except PlaidCallError as e:
            if e.info.get("error_code") not in ("ITEM_NOT_FOUND",):
                raise
        acct_ids = [r["account_id"] for r in conn.execute("SELECT account_id FROM accounts WHERE item_id=?", (item_id,))]
        for a in acct_ids:
            for tbl in ("transactions", "holdings", "liabilities", "balance_snapshots"):
                conn.execute(f"DELETE FROM {tbl} WHERE account_id=?", (a,))
        conn.execute("DELETE FROM accounts WHERE item_id=?", (item_id,))
        conn.execute("UPDATE items SET status='removed', removed_at=?, access_token_enc='' WHERE item_id=?",
                     (now_iso(), item_id))
        conn.execute("INSERT INTO item_log(env,item_id,institution_id,institution_name,created_at,event) "
                     "VALUES(?,?,?,?,?,?)", (env, item_id, row["institution_id"], row["institution_name"],
                                              now_iso(), "removed"))


# ---------------- Sync ----------------

def _log(conn, item_id, step, result, request_id=None):
    conn.execute("INSERT INTO sync_log(at,item_id,step,result,request_id) VALUES(?,?,?,?,?)",
                 (now_iso(), item_id, step, result, request_id))


def _upsert_accounts(conn, item_id, inst_name, accounts):
    for a in accounts:
        b = a.get("balances") or {}
        conn.execute(
            "INSERT INTO accounts(account_id,item_id,source,name,official_name,mask,type,subtype,institution_name,"
            "current_balance,available_balance,credit_limit,currency,persistent_account_id,balance_updated_at,updated_at) "
            "VALUES(?,?,'plaid',?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(account_id) DO UPDATE SET "
            "name=excluded.name, official_name=excluded.official_name, mask=excluded.mask, type=excluded.type, "
            "subtype=excluded.subtype, institution_name=excluded.institution_name, "
            "current_balance=excluded.current_balance, available_balance=excluded.available_balance, "
            "credit_limit=excluded.credit_limit, currency=excluded.currency, "
            "persistent_account_id=excluded.persistent_account_id, balance_updated_at=excluded.balance_updated_at, "
            "updated_at=excluded.updated_at",
            (a["account_id"], item_id, a.get("name"), a.get("official_name"), a.get("mask"),
             str(a.get("type")), str(a.get("subtype")), inst_name, b.get("current"), b.get("available"),
             b.get("limit"), b.get("iso_currency_code") or b.get("unofficial_currency_code"),
             a.get("persistent_account_id"), now_iso(), now_iso()))


def _pfc(t):
    p = t.get("personal_finance_category") or {}
    return p.get("primary"), p.get("detailed"), p.get("confidence_level")


def _upsert_txn(conn, t):
    primary, detailed, conf = _pfc(t)
    d = t.get("date")
    ad = t.get("authorized_date")
    conn.execute(
        "INSERT INTO transactions(transaction_id,account_id,source,date,authorized_date,name,merchant_name,"
        "original_description,amount,currency,pending,pending_transaction_id,pfc_primary,pfc_detailed,"
        "pfc_confidence,payment_channel,created_at,updated_at) VALUES(?,?,'plaid',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(transaction_id) DO UPDATE SET account_id=excluded.account_id, date=excluded.date, "
        "authorized_date=excluded.authorized_date, name=excluded.name, merchant_name=excluded.merchant_name, "
        "original_description=excluded.original_description, amount=excluded.amount, currency=excluded.currency, "
        "pending=excluded.pending, pending_transaction_id=excluded.pending_transaction_id, "
        "pfc_primary=excluded.pfc_primary, pfc_detailed=excluded.pfc_detailed, pfc_confidence=excluded.pfc_confidence, "
        "payment_channel=excluded.payment_channel, updated_at=excluded.updated_at",
        (t["transaction_id"], t["account_id"], str(d) if d else None, str(ad) if ad else None, t.get("name"),
         t.get("merchant_name"), t.get("original_description"), t.get("amount"),
         t.get("iso_currency_code") or t.get("unofficial_currency_code"), 1 if t.get("pending") else 0,
         t.get("pending_transaction_id"), primary, detailed, str(conf) if conf else None,
         str(t.get("payment_channel") or ""), now_iso(), now_iso()))
    # Pending -> posted: carry your manual category/note over to the posted transaction.
    if t.get("pending_transaction_id"):
        old = conn.execute("SELECT user_category, note FROM transactions WHERE transaction_id=?",
                           (t["pending_transaction_id"],)).fetchone()
        if old and (old["user_category"] or old["note"]):
            conn.execute("UPDATE transactions SET user_category=COALESCE(user_category,?), note=COALESCE(note,?) "
                         "WHERE transaction_id=?", (old["user_category"], old["note"], t["transaction_id"]))


def _sync_transactions(conn, client, token, item_id, start_cursor):
    """/transactions/sync: buffer every page, then save the data and the new
    cursor together. Restart from the starting cursor if data changes mid-way."""
    for _restart in range(3):
        cursor = start_cursor or ""
        added, modified, removed = [], [], []
        try:
            while True:
                req = TransactionsSyncRequest(
                    access_token=token, cursor=cursor, count=500,
                    options=TransactionsSyncRequestOptions(
                        include_original_description=True,
                        personal_finance_category_version=PersonalFinanceCategoryVersion("v2")))
                r = call(client.transactions_sync, req)
                added += r["added"]
                modified += r["modified"]
                removed += r["removed"]
                cursor = r["next_cursor"]
                if not r["has_more"]:
                    status = str(r.get("transactions_update_status") or "")
                    break
        except PlaidCallError as e:
            if e.info.get("error_code") == "TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION":
                continue
            raise
        for t in added + modified:
            _upsert_txn(conn, t)
        for t in removed:
            conn.execute("DELETE FROM transactions WHERE transaction_id=? AND source='plaid'",
                         (t["transaction_id"],))
        conn.execute("UPDATE items SET txn_cursor=? WHERE item_id=?", (cursor, item_id))
        return {"added": len(added), "modified": len(modified), "removed": len(removed), "status": status}
    raise PlaidCallError({"error_type": "SYNC", "error_code": "MUTATION_RETRIES_EXHAUSTED",
                          "error_message": "Transactions kept changing during sync; try again shortly."})


def _sync_liabilities(conn, client, token):
    r = call(client.liabilities_get, LiabilitiesGetRequest(access_token=token))
    for c in (r.get("liabilities") or {}).get("credit") or []:
        apr = next((a.get("apr_percentage") for a in (c.get("aprs") or [])
                    if str(a.get("apr_type")) == "purchase_apr"), None)
        conn.execute(
            "INSERT OR REPLACE INTO liabilities(account_id,last_statement_balance,last_statement_date,minimum_payment,"
            "next_due_date,last_payment_amount,last_payment_date,is_overdue,purchase_apr,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (c["account_id"], c.get("last_statement_balance"), _s(c.get("last_statement_issue_date")),
             c.get("minimum_payment_amount"), _s(c.get("next_payment_due_date")), c.get("last_payment_amount"),
             _s(c.get("last_payment_date")), None if c.get("is_overdue") is None else int(c["is_overdue"]),
             apr, now_iso()))
    return len((r.get("liabilities") or {}).get("credit") or [])


def _s(v):
    return str(v) if v is not None else None


def _sync_holdings(conn, client, token, account_ids):
    r = call(client.investments_holdings_get, InvestmentsHoldingsGetRequest(access_token=token))
    for s in r.get("securities") or []:
        conn.execute(
            "INSERT OR REPLACE INTO securities(security_id,name,ticker,type,subtype,is_cash_equivalent,close_price,"
            "close_price_as_of) VALUES(?,?,?,?,?,?,?,?)",
            (s["security_id"], s.get("name"), s.get("ticker_symbol"), _s(s.get("type")), _s(s.get("subtype")),
             1 if s.get("is_cash_equivalent") else 0, s.get("close_price"), _s(s.get("close_price_as_of"))))
    for a in account_ids:
        conn.execute("DELETE FROM holdings WHERE account_id=?", (a,))  # fresh Plaid data replaces any CSV snapshot
    for h in r.get("holdings") or []:
        conn.execute(
            "INSERT OR REPLACE INTO holdings(account_id,security_id,quantity,institution_price,institution_value,"
            "cost_basis,price_as_of,source,updated_at) VALUES(?,?,?,?,?,?,?,'plaid',?)",
            (h["account_id"], h["security_id"], h.get("quantity"), h.get("institution_price"),
             h.get("institution_value"), h.get("cost_basis"), _s(h.get("institution_price_as_of")), now_iso()))
    return len(r.get("holdings") or [])


FRIENDLY = {
    "ITEM_LOGIN_REQUIRED": "The bank needs you to log in again. Click 'Fix connection'.",
    "PENDING_DISCONNECT": "Your access is about to expire. Click 'Fix connection' to renew it.",
    "PENDING_EXPIRATION": "Your access is about to expire. Click 'Fix connection' to renew it.",
    "ITEM_LOCKED": "The bank locked this login. Unlock it on the bank's website, then click 'Fix connection'.",
    "INVALID_CREDENTIALS": "The bank rejected the login. Click 'Fix connection' to try again.",
    "ACCESS_NOT_GRANTED": "Some account access wasn't granted. Click 'Fix connection' to update permissions.",
}


def _mark_item_error(conn, item_id, info):
    code = info.get("error_code")
    status = "login_required" if code in NEEDS_USER else "error"
    # Plaid's display_message is written for end users; error_message is for developers.
    msg = info.get("display_message") or FRIENDLY.get(code) or \
        f"Couldn't reach the bank right now ({code}). Try syncing again later."
    conn.execute("UPDATE items SET status=?, error_code=?, error_message=? WHERE item_id=?",
                 (status, code, msg, item_id))


def sync_item(item_id: str) -> dict:
    client, env = get_client()
    out = {"item_id": item_id, "steps": {}}
    with get_db() as conn:
        item = conn.execute("SELECT * FROM items WHERE item_id=?", (item_id,)).fetchone()
        if not item or item["status"] == "removed":
            return {"item_id": item_id, "error": "not found"}
        token = crypto.decrypt(item["access_token_enc"])
        try:
            # Item health (error state, consent expiry)
            ig = call(client.item_get, ItemGetRequest(access_token=token))
            it = ig.get("item") or {}
            conn.execute("UPDATE items SET consent_expiration=? WHERE item_id=?",
                         (_s(it.get("consent_expiration_time")), item_id))
            if it.get("error") and (it["error"].get("error_code") in NEEDS_USER):
                _mark_item_error(conn, item_id, it["error"])
                out["needs_update"] = True
                return out

            accts = call(client.accounts_get, AccountsGetRequest(access_token=token))
            _upsert_accounts(conn, item_id, item["institution_name"], accts["accounts"])
            out["steps"]["accounts"] = len(accts["accounts"])
            types = {str(a.get("type")) for a in accts["accounts"]}

            out["steps"]["transactions"] = _sync_transactions(conn, client, token, item_id, item["txn_cursor"])

            if "credit" in types or "loan" in types:
                try:
                    out["steps"]["liabilities"] = _sync_liabilities(conn, client, token)
                except PlaidCallError as e:
                    out["steps"]["liabilities"] = f"skipped ({e.info.get('error_code')})"
            if "investment" in types:
                inv_ids = [a["account_id"] for a in accts["accounts"] if str(a.get("type")) == "investment"]
                try:
                    out["steps"]["holdings"] = _sync_holdings(conn, client, token, inv_ids)
                except PlaidCallError as e:
                    out["steps"]["holdings"] = f"skipped ({e.info.get('error_code')})"

            conn.execute("UPDATE items SET status='ok', error_code=NULL, error_message=NULL, last_sync_at=?, "
                         "last_sync_result=? WHERE item_id=?", (now_iso(), json.dumps(out["steps"]), item_id))
            _log(conn, item_id, "sync", "ok")
        except PlaidCallError as e:
            _mark_item_error(conn, item_id, e.info)
            _log(conn, item_id, "sync", f"{e.info.get('error_type')}/{e.info.get('error_code')}",
                 e.info.get("request_id"))
            out["error"] = {k: e.info.get(k) for k in ("error_code", "display_message", "error_message", "request_id")}
            out["needs_update"] = e.info.get("error_code") in NEEDS_USER
        finally:
            del token
        categorize_all(conn)
        take_snapshot(conn)
    return out


def sync_all() -> list:
    with get_db() as conn:
        ids = [r["item_id"] for r in conn.execute("SELECT item_id FROM items WHERE status!='removed'")]
    return [sync_item(i) for i in ids]  # one Item at a time (per-Item rate limits)


def refresh_live_balances(item_id: str) -> dict:
    """Real-time balance check (Plaid Balance product). Slow (3-30s) and
    rate-limited, so it only runs when you click the button."""
    client, _ = get_client()
    with get_db() as conn:
        item = conn.execute("SELECT * FROM items WHERE item_id=?", (item_id,)).fetchone()
        token = crypto.decrypt(item["access_token_enc"])
        try:
            r = call(client.accounts_balance_get, AccountsBalanceGetRequest(access_token=token), retries=1)
        finally:
            del token
        _upsert_accounts(conn, item_id, item["institution_name"], r["accounts"])
        take_snapshot(conn)
    return {"accounts": len(r["accounts"])}


# ---------------- Net worth snapshots ----------------

def signed_balance(acct) -> float:
    bal = acct["current_balance"] or 0
    if acct["type"] in ("credit", "loan"):
        return -abs(bal)
    return bal


def take_snapshot(conn):
    d = today()
    assets = liabilities = 0.0
    for a in conn.execute("SELECT * FROM accounts WHERE include_in_networth=1"):
        b = signed_balance(a)
        conn.execute("INSERT OR REPLACE INTO balance_snapshots(date,account_id,balance) VALUES(?,?,?)",
                     (d, a["account_id"], b))
        if b >= 0:
            assets += b
        else:
            liabilities += -b
    conn.execute("INSERT OR REPLACE INTO networth_snapshots(date,assets,liabilities,net_worth) VALUES(?,?,?,?)",
                 (d, round(assets, 2), round(liabilities, 2), round(assets - liabilities, 2)))
