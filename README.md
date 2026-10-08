# Personal Finance Dashboard

A private dashboard for your money that runs only on this computer. It shows your net worth, spending by category, paychecks and pay stubs, investments, and where every dollar goes.

**Privacy:** everything is stored in a database file inside this folder (`data/`). The app only listens on `127.0.0.1`, so other computers can't reach it. The only outside connection is to Plaid, which downloads your bank data read-only. There's no cloud storage, no analytics and no tracking.

**Why this technology:** Python + FastAPI + SQLite is a small, widely used and well-maintained stack. It needs no separate database server, starts with one command, and keeps all your data in one local file.

---

## Start the app

Double-click **`start.bat`** in this folder.

A black window opens and your browser goes to **http://127.0.0.1:8765**. Keep the black window open while you use the dashboard. To stop the app, close that window.

(Or from a terminal in this folder: `.venv\Scripts\python -m app`)

## Sync your data

The dashboard syncs automatically when you open it, at most once every 6 hours. To sync any time, click **Sync now** (top right).

Banks update Plaid about 1–4 times a day, so syncing more often won't show newer data. For an up-to-the-second balance, use **Live balance** on the *Accounts & data* page. It's slower and goes straight to the bank.

## Add a new pay stub

1. Save the PDF into the **`pay_stub_year_to_date`** folder.
2. In the app, go to **Income & pay stubs** and click **Import new pay stubs**.
3. If a stub says **needs review**, click **Review & confirm**. Fix any numbers that don't match the PDF, then save. The app shows whether gross − deductions = net adds up.

The app never guesses. If a value can't be read confidently, the stub is flagged for you to check.

---

## Sandbox (test) vs Production (real accounts)

The badge in the top-right corner shows which mode you're in.

- **Sandbox** uses fake test data from Plaid and is free and unlimited. To try it, go to *Accounts & data* and click **Load Sandbox demo data**. Or click **Add a bank account**, pick any bank, and log in with username `user_good` and password `pass_good`.
- **Production** uses your real accounts. Your Plaid Trial plan allows **10 Production Items total, forever**. An Item is one login at one bank (two credit cards from the same bank share one login, so they're one Item). Removing an Item does **not** give the slot back.

Sandbox and Production keep separate database files (`data/finance_sandbox.db` and `data/finance_production.db`), so test data never mixes with real data. The first time Production starts, your rules, settings and pay stubs are copied over from Sandbox.

### Switching to Production

1. Open `.env` in Notepad and change the line `PLAID_ENV=sandbox` to `PLAID_ENV=production`. Save it.
2. Close the black app window and double-click `start.bat` again.
3. The badge now says **PRODUCTION**. Go to *Accounts & data* → **Add a bank account**. The app asks which institution you're connecting and shows how many of your 10 Items are used.
4. Connect **one institution at a time**. Many big banks and brokerages log you in on their own website, which opens in a **pop-up window**, so allow pop-ups for 127.0.0.1 if your browser asks.

The app protects your Item slots:

- It shows **X of 10 Items used** and keeps a permanent log of every Production Item.
- It refuses to start a new connection for an institution that's already connected.
- Removing Production connections is disabled.

### When a bank needs you to log in again

You'll see a red banner. Go to *Accounts & data* and click **Fix connection** on that bank. This re-logs you in on the **same** Item and does **not** use a new slot.

If you open a new account at a bank that's already connected (for example a new credit card), use **Add/remove accounts** on that bank.

---

## How the numbers work

- **Transfers between your own accounts are never counted as spending or income.** For example, when you pay a credit card from your checking account, the app finds the matching money-in on the card and labels both sides **Credit Card Payment**. Both are excluded from totals.
- **Money sent to a brokerage counts as Investing**, not spending.
- **Venmo payments** count as spending ("Payments to people"). To mark your rent, find a rent payment on the *Transactions* page and click **Rent?**. Choose "similar recurring payments" to auto-tag future ones: same account, amount within 10%, and around the same day of the month. Small Venmo payments for dinner and so on won't match.
- **Paychecks** are detected from your checking account deposits. Paid every 2 weeks, you get 26 paychecks a year, so 2 months each year have 3. Monthly totals use the actual paychecks that landed in that month, and 3-paycheck months are labeled.
- **401(k) contributions** come from your pay stubs, because they're taken out before your paycheck reaches the bank.
- **Investment rate** = (your 401(k) + ESPP + net money moved to your brokerage) ÷ gross pay, and also ÷ take-home pay. You can change what's included under *Accounts & data → Settings*.
- **Categories** start from Plaid's, then your rules apply, then your manual choice on a transaction wins over everything. Edit rules on the **Rules** page. Any transaction can be changed on the **Transactions** page. Pick **Transfer** if the app missed a transfer between your own accounts.

## The Future tab (projections)

The **Future** tab projects how your money could grow, starting from your real data:

- **Starting balances:** your accounts.
- **Pay and contributions:** your latest pay stub, covering salary, 401(k), ESPP and HSA per paycheck.
- **Take-home pay:** your latest paycheck deposit.
- **Spending:** your average over the last 3 full months.
- **Transfers to your brokerage and retirement accounts:** your average over the last 6 months.

Everything is adjustable with sliders: time horizon, return rates, contributions, raises, inflation, lump sums and extra buckets. **"My current habits"** always reflects your real data. Your changes appear next to it as **"My adjusted scenario"**, and you can **save named scenarios**. They're stored in your local database, and their starting balances refresh from your accounts when you load them.

How the math works, in plain words:

- **Returns are effective annual rates, compounded monthly.** "3.8%" means exactly +3.8% after a year with no deposits, so $10,000 becomes $10,380.
- **Per-paycheck contributions follow the real every-two-weeks calendar.** Some months get 3 paychecks.
- **Money left after spending and your transfers stays in checking.** If checking would go below $0, transfers are trimmed and a warning shows how much.
- **Annual limits are editable settings, not hard-coded.** The 2026 IRS limits are $24,500 for the 401(k) and $4,400 for an HSA with self-only coverage. Change them under "Where the starting numbers come from" or in each bucket's "More options".
- **Stock-based buckets show a low and a high case** (± 3 percentage points by default), because returns vary.
- **Credit card balances and loans that aren't connected are not included** in the projection.

The calculations live in `app/projections.py`. To run its tests:

```
.venv\Scripts\python -m unittest discover -s tests -v
```

## If a connection breaks: CSV import

Download a CSV from your bank's website. Exports from many common banks, card issuers and brokerages are recognized automatically, and brokerage "Positions" files update your holdings. Then go to *Accounts & data → Import a CSV*. Check the preview, pick the account and import. Rows Plaid already downloaded are skipped automatically, and importing the same file twice adds nothing.

## Accounts without Plaid (e.g. a stock purchase plan)

Go to *Accounts & data → Add a manual account*. Update its balance by hand whenever you like. It counts in your net worth and investments.

---

## Backups — please read

Your Plaid connections depend on two things:

1. **`data/finance_production.db`**: the database. The app copies it to `data/backups/` once a day and keeps 30 days.
2. **The encryption key** that protects your Plaid access tokens. It's stored in Windows Credential Manager, not in this folder, so a copy of this folder alone can't be used to read your accounts.

If you reinstall Windows or move to a new computer, you need **both**. Save a copy of the key now, to a USB stick or your password manager:

```
.venv\Scripts\python scripts\backup_key.py E:\finance-key.txt
```

To restore it later:

```
.venv\Scripts\python scripts\restore_key.py E:\finance-key.txt
```

Without the key, the saved connections can't be used. On the Trial plan, reconnecting would use new Item slots.

---

## Settings in `.env`

The app reads these from `.env`. Never share this file.

| Name | Meaning |
|---|---|
| `PLAID_CLIENT_ID` | Your Plaid client ID |
| `PLAID_SECRET_SANDBOX` | Sandbox secret |
| `PLAID_SECRET_PRODUCTION` | Production secret |
| `PLAID_ENV` | `sandbox` or `production` |
| `PLAID_REDIRECT_URI` | *Optional, normally leave unset.* Only for HTTPS redirect mode (below) |
| `APP_PORT` | *Optional.* Default `8765` |

### Optional: HTTPS redirect mode

You shouldn't need this. On a desktop browser, Plaid opens OAuth bank logins (banks that log you in on their own website) in a pop-up, and no redirect address is required. If a bank's pop-up ever fails, the fallback is to set up local HTTPS (mkcert) and register `https://localhost:8765/oauth-return` in the Plaid Dashboard under *API → Allowed redirect URIs*. Ask for help before doing this.

## Troubleshooting

- **The page doesn't load:** make sure the black window is open and shows "running at http://127.0.0.1:8765".
- **"Missing in .env":** a variable name in `.env` is misspelled or empty.
- **A bank shows a red banner:** click *Fix connection*.
- **Numbers look off:** use *Accounts & data → Check the numbers* to compare balances, see which transfers were matched, and list transactions that might be missed transfers.

## Folder layout

```
app/                      the application (Python server + web page)
data/                     your databases and backups (private, not shared)
pay_stub_year_to_date/    put pay stub PDFs here (private)
csv_imports/              uploaded CSV files (private)
scripts/                  institution check, key backup/restore
start.bat                 double-click to start
```
