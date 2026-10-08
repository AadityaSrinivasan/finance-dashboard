# PLAID_PLAN

## Goal

"Build a personal finance dashboard that runs ONLY on my local computer": net worth (bank checking, two credit cards at one issuer, a brokerage, a workplace 401(k)), spending by category with custom rules, transfer de-duplication, paycheck detection, pay stub import, cash-flow Sankey, investment rate, investment allocation, CSV fallback. Read-only. Plaid Trial plan (10 Production Items, never freed). (workflow_id: wf_525b6c006e9d21c0)

## Scope

- Products (SELECT-005, LIAB-007, INV-015):
  - **transactions**: in `products`. Cash and card activity via `/transactions/sync`. Free cached balances come with `/accounts/get`.
  - **investments**: in `additional_consented_products`. Brokerage and 401(k) holdings via `/investments/holdings/get`. Consent now, billed on first use, no coverage cut.
  - **liabilities**: in `additional_consented_products`. Card statement balance, due date and minimum payment via `/liabilities/get`, called only on Items with credit/loan accounts (LIAB-008).
  - **balance**: not a products entry (BAL-008). `/accounts/balance/get` only runs behind the explicit "Live balance" button, never on open or on a schedule (BAL-001/002).
- Rejected alternatives:
  - Investments/liabilities in `products`: would cut coverage and bill at Item creation (PITFALL-001, CONV-002).
  - `/transactions/recurring/get`: paid add-on (GUIDE-016, TXN-016). The user forbade anything needing a paid plan, so recurring charges are detected locally instead.
  - `/investments/transactions/get`: would start the Investments Transactions subscription (INV-002) and isn't needed for any requested view.
  - `/transactions/refresh` and `/investments/refresh`: paid add-ons, not used.
- `transactions.days_requested = 730` at first Link (TXN-009). This can't be raised later without re-linking, which would cost Item slots.
- PFC taxonomy pinned to v2 via `personal_finance_category_version: "v2"` (TXN-006).
- Platform: Python 3.13 + FastAPI + SQLite backend, plain HTML/JS frontend, Plaid Link JS from cdn.plaid.com (WEB-010). Bound to 127.0.0.1 only.
- Environment: sandbox until the user approves Production (user rule: ASK FIRST).

## Human tasks

| Task | Why it matters | Where | State |
|------|----------------|-------|-------|
| OAuth redirect URI | Not needed: desktop web opens OAuth banks in a pop-up without `redirect_uri`, per Plaid OAuth docs. User chose pop-up mode. Dashboard `redirect_uris` confirmed empty; app sends no `redirect_uri` unless `PLAID_REDIRECT_URI` is set. | https://dashboard.plaid.com/developers/api | not_applicable |
| Webhook receiver | App is localhost-only (user security rule #3), so no public receiver can exist. User explicitly accepted manual "Sync now" plus auto-sync on open (max every 6h) as the update mechanism (WEBHOOK-005, CHECK-014). Revisit only if the app is ever hosted or tunneled. | https://dashboard.plaid.com/developers/webhooks | not_applicable (polling accepted 2026-10-04) |
| Production access + product entitlement (transactions, investments, liabilities, balance; US) | Without it, Production Link fails | dashboard_get_state → products | verified 2026-10-04 (production authorized; all four in production_by_country.US) |
| Access to separately gated OAuth institutions in Production | Some large brokerages are gated separately (INV-014) | https://dashboard.plaid.com → OAuth institutions page | verified 2026-10-04: linked in Production; retirement accounts returned with holdings |
| Link customization / Data Transparency Messaging in Production | INVALID_LINK_CUSTOMIZATION would block Production Link (PITFALL-002) | https://dashboard.plaid.com/link | verified 2026-10-04: Production link_token created and 4 Production Links succeeded |
| A savings bank the user wanted connected | Plaid returned INSTITUTION_NO_LONGER_SUPPORTED in Link (no Item created) | — | not_applicable: use a manual account |
| Back up token encryption key | Losing it strands the stored access tokens (and Trial slots) | `scripts/backup_key.py` | needs_human |

## Implementation checklist

- [x] Secrets only from `.env` at runtime (GUIDE-001). `.env` read-deny rule in `.claude/settings.json`; `.gitignore` covers .env, db, pay stubs, CSVs, tokens, certs.
- [x] `/link/token/create` server-side: transactions in products, investments + liabilities consented, days_requested 730, optional redirect_uri on every call incl. update mode (GUIDE-010, OAUTH-005).
- [x] `/item/public_token/exchange` immediately, persisted before responding (GUIDE-013/015). Access token Fernet-encrypted at rest, key in Windows Credential Manager (DATA-001). Never returned to the browser or logged (GUIDE-002/006).
- [x] Duplicate-institution guard before exchange and before Production Link (ITEM-001/011). Production new-Item Link requires explicit confirmation plus institution name; blocked at 10 used.
- [x] Permanent `item_log` of every Production Item; "X of 10 used" in UI.
- [x] Update mode for ITEM_LOGIN_REQUIRED / pending expiry ("Fix connection"), plus account-selection update mode ("Add/remove accounts"). Never delete-and-relink (ITEM-003). `/item/get` read on every sync for error and consent expiry (ITEM-010).
- [x] `/transactions/sync`: count 500, buffer all pages, persist data + cursor in one DB transaction, restart from the starting cursor on TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION. Applies added/modified/removed. Pending→posted keeps user edits via pending_transaction_id (TXN-001/002/004/007/011).
- [x] Errors branch on error_code. display_message or own friendly copy shown, request_id logged (ITEM-002). Capped, jittered backoff for transient codes only (ITEM-004/013). Items synced serially (ITEM-013).
- [x] `/liabilities/get` only when credit/loan accounts exist (LIAB-008). APR picked by apr_type (LIAB-003). All fields nullable in UI (LIAB-002).
- [x] `/investments/holdings/get`, joined to securities locally (INV-001/010). Cash holdings handled (INV-007).
- [x] Balance semantics per account type (BAL-004). Credit shown as debt.
- [x] Institution names from `/institutions/get_by_id` (GUIDE-017).
- [x] Link onSuccess/onExit/onEvent metadata stored locally in `link_events` (WEB-003, CONV-015). Nothing leaves the machine.
- [x] Remove (`/item/remove`) enabled in Sandbox only. Disabled in Production because Trial slots are never freed (ITEM-005).
- [x] Categories, rules, transfer matching, paycheck detection, recurring detection, pay stub parser, cash-flow/Sankey, investments allocation, CSV import with Plaid dedupe, manual accounts, daily net-worth snapshots, daily Production DB backup.

## Acceptance

- Round 1 (2026-10-04): **PASS**. CHECK-001–005 and 008–015 passed; CHECK-006 and CHECK-007 not_applicable (localhost-only, so no webhook receiver, with polling accepted by the user; desktop pop-up OAuth needs no redirect URI). Caveats: the Link UI credential step is still to be done by the user once in Sandbox, and Production behavior (real statement balances and due dates, 401(k) visibility) is unverified until the user approves Production.

## Maintenance

This integration is maintained with the Plaid MCP; this file is its durable state. Agents working in this repository: consult build_guidance before modifying any Plaid-touching code, and re-run build_check_acceptance before reporting such a change as working. The acceptance above applies only to the code as it was verified — later changes invalidate it.

## How to test

### Sandbox demo
1. Make sure `.env` has `PLAID_ENV=sandbox`. Double-click `start.bat`.
2. Go to *Accounts & data* → **Load Sandbox demo data**.
3. Expected: 4 demo connections (a bank, a card issuer with 2 cards, a brokerage, a 401(k)). *Net worth* shows balances. *Cash flow* shows the Sankey. Card payments are labeled "matched transfer" on the *Transactions* page.

### Plaid Link (connect window)
1. *Accounts & data* → **Add a bank account** → pick "First Platypus Bank" or any non-demo bank.
2. Username `user_good`, password `pass_good`.
3. Expected: a new connection appears. If the toast says history is still downloading, click *Sync now* after a minute.

### Re-login (update mode)
1. On a Sandbox connection click **Simulate expiry**, then **Sync**. A red banner appears.
2. Click **Fix connection** and complete Plaid's window (`user_good` / `pass_good`).
3. Expected: status returns to "connected". The Item count is unchanged.

### Venmo rent
1. *Transactions* → search "venmo" → click **Rent?** on a ~$1,150 payment → choose "similar recurring payments" → Save.
2. Expected: all monthly rent payments become Rent, and small Venmo payments stay "Payments to people".

### Pay stubs
1. Put a PDF in `pay_stub_year_to_date`, then *Income & pay stubs* → **Import new pay stubs**.
2. Expected: the stub shows "confirmed" when all totals reconcile, otherwise "needs review" with fields to check.

## Decisions made

- Pop-up OAuth on desktop, no HTTPS/redirect URI (user, 2026-10-04).
- ESPP/ASPP counts toward investment rate. HSA and employer match are shown but not counted (user, 2026-10-04).
- Updates via "Sync now" plus auto-sync on open, max every 6h. No webhooks because the app is localhost-only (user, 2026-10-04).
- Recurring charges detected locally rather than via the paid Recurring Transactions add-on (Claude, per the user's no-paid-plan rule).
- Venmo/Zelle outflows count as spending ("Payments to people") unless matched to the user's own account (Claude; Plaid labels them TRANSFER_OUT).
- Separate SQLite file per Plaid environment. Rules, settings and pay stubs are copied to Production on first use.
- Route paths: local-only API under `/api/*` (Claude; single-user local app with no external consumers).

## Production status

- Production is live. Item usage and the permanent Item log are shown in the app (*Accounts & data*); no duplicates were created. The workplace 401(k) appeared under the provider's regular login, so no separate workplace-portal connection was needed.
- Verified with real data: checking balance matched the bank's app; investment balances equal holdings totals; year-to-date take-home equals paycheck deposits; 401(k)/HSA balances consistent with pay-stub contributions; card payments matched as transfers.
- Personal categorization choices (family support, reimbursements, rent) live in the local database as rules, not in this file.

## Open questions

- None blocking.
