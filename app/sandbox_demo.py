"""SANDBOX ONLY: creates fake test connections that mimic your real setup
(bank checking, two credit cards at one issuer, a brokerage, a 401(k)) using Plaid's
custom Sandbox users. Never runs against Production."""
import json
import random
from datetime import date, timedelta

from plaid.model.products import Products
from plaid.model.sandbox_item_reset_login_request import SandboxItemResetLoginRequest
from plaid.model.sandbox_public_token_create_request import SandboxPublicTokenCreateRequest
from plaid.model.sandbox_public_token_create_request_options import SandboxPublicTokenCreateRequestOptions
from plaid.model.sandbox_public_token_create_request_options_transactions import \
    SandboxPublicTokenCreateRequestOptionsTransactions

from . import crypto
from .db import get_db
from .plaid_client import get_client
from .sync import PlaidCallError, call, exchange_public_token

DAYS = 185


def _require_sandbox():
    _, env = get_client()
    if env != "sandbox":
        raise ValueError("Demo data can only be created in Sandbox.")


def _t(d, amount, desc):
    return {"date_transacted": d.isoformat(), "date_posted": d.isoformat(), "amount": round(amount, 2),
            "description": desc, "currency": "USD"}


def build_configs(today=None):
    rnd = random.Random(42)
    today = today or date.today()
    start = today - timedelta(days=DAYS)

    def days():
        d = start
        while d <= today:
            yield d
            d += timedelta(days=1)

    # ---- Credit cards (one issuer) ----
    travel_card, cashback_card = [], []
    card_month_totals = {"travel_card": {}, "cashback_card": {}}

    def charge(card, lst, d, amt, desc):
        lst.append(_t(d, amt, desc))
        key = d.strftime("%Y-%m")
        card_month_totals[card][key] = card_month_totals[card].get(key, 0) + amt

    for d in days():
        if d.day == 3:
            charge("cashback_card", cashback_card, d, 11.99, "SPOTIFY USA 877-778-1161 NY")
        if d.day == 12:
            charge("cashback_card", cashback_card, d, 2.99, "APPLE.COM/BILL 866-712-7753 CA")
        if d.day == 18:
            charge("cashback_card", cashback_card, d, 15.49, "NETFLIX.COM LOS GATOS CA")
        if d.weekday() == 5:
            charge("travel_card", travel_card, d, rnd.uniform(28, 95), rnd.choice(
                ["CORNER MARKET ANYTOWN US", "SHAKE SHACK ANYTOWN US", "STARBUCKS STORE 12345",
                 "CHIPOTLE 2231 ANYTOWN US", "SWEETGREEN ANYTOWN US"]))
        if d.weekday() == 1:
            charge("cashback_card", cashback_card, d, rnd.uniform(40, 130), rnd.choice(
                ["KROGER #521 ANYTOWN US", "WHOLEFDS ANYTOWN US", "TRADER JOE S #654"]))
        if d.day in (7, 21):
            charge("cashback_card", cashback_card, d, rnd.uniform(15, 90), rnd.choice(
                ["AMAZON MKTPL*AB12CD34", "TARGET 00012345 ANYTOWN US", "CVS/PHARMACY #01234"]))
        if d.day in (9, 25):
            charge("travel_card", travel_card, d, rnd.uniform(12, 38), "UBER   *TRIP HELP.UBER.COM")
        if d.day == 14:
            charge("cashback_card", cashback_card, d, rnd.uniform(35, 55), "SHELL OIL 57444123 ANYTOWN US")
        if d.day == 28 and d.month % 2 == 0:
            charge("travel_card", travel_card, d, rnd.uniform(180, 420), rnd.choice(
                ["UNITED AIRLINES 0162345678", "MARRIOTT HOTELS BOSTON MA", "AIRBNB * HMXYZ12"]))

    # ---- Bank checking ----
    bank = []
    # Biweekly paychecks (every other Friday)
    anchor = date(2026, 1, 2)
    d = anchor
    while d - timedelta(days=14) >= start:
        d -= timedelta(days=14)
    while d < start:
        d += timedelta(days=14)
    while d <= today:
        bank.append(_t(d, -2100.00, "ACME CORP DES:PAYROLL ID:XXXXX12345 INDN:DEMO USER CO ID:XXXXX PPD"))
        bank.append(_t(d + timedelta(days=1), 300.00, "INVESTCO BROKERAGE DES:FUNDS ID:XXXXX5678 INDN:DEMO USER CO ID:XXXXX WEB"))
        d += timedelta(days=14)
    for d in days():
        if d.day == 1:
            rent_day = d - timedelta(days=rnd.choice([0, 0, 1, 2]))
            if rent_day >= start:
                bank.append(_t(rent_day, 1150.00, "VENMO DES:PAYMENT ID:XXXXX1234 INDN:DEMO USER CO ID:XXXXX WEB"))
        if d.day == 15:
            bank.append(_t(d, 412.33, "AUTO FINANCE DES:LOAN PYMT ID:XXXXX9988 INDN:DEMO USER CO ID:XXXXX PPD"))
        if d.day == 10:
            bank.append(_t(d, rnd.uniform(78, 125), "DOMINION ENERGY DES:BILLPAY ID:XXXXX4455"))
        if d.day == 22:
            bank.append(_t(d, 65.00, "VERIZON WIRELESS DES:PAYMENTREC ID:XXXXX3322"))
        if d.day == 17:
            bank.append(_t(d, round(rnd.uniform(18, 48), 2), "VENMO DES:PAYMENT ID:XXXXX7777 INDN:DEMO USER"))
        if d.day == 26:
            bank.append(_t(d, 60.00, "BANK ATM 09/26 #000001234 WITHDRWL ANYTOWN US"))
        if d.day == 28:
            bank.append(_t(d, -0.41, "Interest Earned"))
        # Card payments: previous month's charges, paid on the 5th and 20th
        for card, pay_day, label, lst in (("travel_card", 5, "Travel card", travel_card), ("cashback_card", 20, "Cash back card", cashback_card)):
            if d.day == pay_day:
                prev = (d.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
                amt = round(card_month_totals[card].get(prev, 0), 2)
                if amt:
                    bank.append(_t(d, amt, "CARD SERVICES CREDIT CRD DES:EPAY ID:XXXXX2468 INDN:DEMO USER CO ID:XXXXX WEB"))
                    lst.append(_t(d + timedelta(days=1), -amt, "Payment Thank You-Mobile"))
    # One withdrawal back from the brokerage
    bank.append(_t(today - timedelta(days=40), -200.00, "INVESTCO BROKERAGE DES:DEBITS ID:XXXXX5679 INDN:DEMO USER"))

    def sec(ticker, name):
        return {"ticker_symbol": ticker, "name": name, "currency": "USD"}

    def hold(ticker, name, qty, price, cost):
        return {"institution_price": price, "institution_price_as_of": today.isoformat(), "cost_basis": cost,
                "quantity": qty, "currency": "USD", "security": sec(ticker, name)}

    configs = {
        "bank": ({"override_accounts": [{
            "type": "depository", "subtype": "checking", "starting_balance": 6215.40,
            "meta": {"name": "Adv Plus Banking", "official_name": "Advantage Checking", "mask": "0001"},
            "transactions": bank}]}, ["transactions"], "ins_109508", "Demo: Bank"),
        "cards": ({"override_accounts": [
            {"type": "credit", "subtype": "credit card", "starting_balance": 1240.55,
             "meta": {"name": "Travel Rewards Card", "official_name": "Travel Rewards Card", "mask": "4321",
                      "limit": 15000},
             "liability": {"type": "credit", "purchase_apr": 21.49, "cash_apr": 29.49, "balance_transfer_apr": 21.49,
                           "special_apr": 0, "last_payment_amount": 512.10, "minimum_payment_amount": 40,
                           "is_overdue": False},
             "transactions": travel_card},
            {"type": "credit", "subtype": "credit card", "starting_balance": 486.20,
             "meta": {"name": "Cash Back Card", "official_name": "Cash Back Card", "mask": "8765",
                      "limit": 9000},
             "liability": {"type": "credit", "purchase_apr": 20.24, "cash_apr": 29.49, "balance_transfer_apr": 20.24,
                           "special_apr": 0, "last_payment_amount": 389.77, "minimum_payment_amount": 35,
                           "is_overdue": False},
             "transactions": cashback_card}]}, ["transactions", "liabilities"], "ins_109511", "Demo: Card Issuer"),
        "brokerage": ({"override_accounts": [{
            "type": "investment", "subtype": "brokerage", "starting_balance": 14390.0,
            "meta": {"name": "Individual Brokerage", "mask": "1111"},
            "holdings": [hold("VTI", "Vanguard Total Stock Market ETF", 25, 285.10, 6200),
                         hold("VOO", "Vanguard S&P 500 ETF", 8, 560.00, 3900),
                         hold("AAPL", "Apple Inc.", 10, 232.40, 1700),
                         hold("NVDA", "NVIDIA Corporation", 15, 178.00, 1100)]}]},
                      ["investments"], "ins_109512", "Demo: Brokerage"),
        "retirement": ({"override_accounts": [{
            "type": "investment", "subtype": "401k", "starting_balance": 11285.0,
            "meta": {"name": "EMPLOYER 401K", "mask": "2222"},
            "holdings": [hold("IDX500", "S&P 500 Index Fund", 22, 215.00, 4100),
                         hold("TD2060", "Target Date 2060 Fund", 280, 16.50, 4300),
                         hold("INTLIDX", "International Index Fund", 40, 54.00, 2000)]}]},
                     ["investments"], "ins_109509", "Demo: 401(k) Provider"),
    }
    return configs


def create_demo():
    _require_sandbox()
    client, _ = get_client()
    results = []
    with get_db() as conn:
        existing = {r["institution_name"] for r in conn.execute("SELECT institution_name FROM items WHERE status!='removed'")}
    for key, (cfg, products, inst_id, label) in build_configs().items():
        if label in existing:
            results.append({"demo": key, "ok": True, "message": "already loaded"})
            continue
        req = SandboxPublicTokenCreateRequest(
            institution_id=inst_id,
            initial_products=[Products(p) for p in products],
            options=SandboxPublicTokenCreateRequestOptions(
                override_username="user_custom", override_password=json.dumps(cfg),
                transactions=SandboxPublicTokenCreateRequestOptionsTransactions(days_requested=730)))
        try:
            r = call(client.sandbox_public_token_create, req)
        except PlaidCallError as e:
            results.append({"demo": key, "ok": False, "message": e.info.get("error_message"),
                            "error": {"error_code": e.info.get("error_code")}})
            continue
        res = exchange_public_token(r["public_token"], {"institution": {"institution_id": inst_id, "name": label},
                                                        "link_session_id": f"sandbox-demo-{key}"})
        if res.get("ok"):
            with get_db() as conn:
                conn.execute("UPDATE items SET institution_name=? WHERE item_id=?", (label, res["item_id"]))
                conn.execute("UPDATE accounts SET institution_name=? WHERE item_id=?", (label, res["item_id"]))
                conn.execute("UPDATE item_log SET institution_name=? WHERE item_id=?", (label, res["item_id"]))
        results.append({"demo": key, "ok": res.get("ok"), "message": res.get("message"),
                        "sync": res.get("sync", {}).get("steps"), "error": res.get("sync", {}).get("error")})
    return results


def simulate_login_expired(item_id: str):
    """Sandbox: force ITEM_LOGIN_REQUIRED so the 'Fix connection' (update mode) flow can be tested."""
    _require_sandbox()
    client, _ = get_client()
    with get_db() as conn:
        row = conn.execute("SELECT access_token_enc FROM items WHERE item_id=?", (item_id,)).fetchone()
    call(client.sandbox_item_reset_login, SandboxItemResetLoginRequest(access_token=crypto.decrypt(row["access_token_enc"])))
    return {"ok": True}
