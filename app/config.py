"""Settings loaded from .env at runtime. Secret values are never printed."""
import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)
DB_PATH = DATA_DIR / "finance.db"
PAYSTUB_DIR = BASE_DIR / "pay_stub_year_to_date"
CSV_DIR = BASE_DIR / "csv_imports"
CERT_DIR = BASE_DIR / "certs"

# The app only ever binds to the loopback interface (see __main__.py).
PORT = int(os.getenv("APP_PORT", "8765"))

PRODUCTION_ITEM_LIMIT = 10


def plaid_env() -> str:
    env = (os.getenv("PLAID_ENV") or "sandbox").strip().strip('"').lower()
    if env not in ("sandbox", "production"):
        raise RuntimeError("PLAID_ENV in .env must be 'sandbox' or 'production'.")
    return env


def plaid_credentials():
    """Return (client_id, secret, env). Raises without revealing values."""
    env = plaid_env()
    client_id = (os.getenv("PLAID_CLIENT_ID") or "").strip().strip('"')
    var = "PLAID_SECRET_PRODUCTION" if env == "production" else "PLAID_SECRET_SANDBOX"
    secret = (os.getenv(var) or "").strip().strip('"')
    missing = [n for n, v in (("PLAID_CLIENT_ID", client_id), (var, secret)) if not v]
    if missing:
        raise RuntimeError(f"Missing in .env: {', '.join(missing)}")
    return client_id, secret, env


def redirect_uri():
    """Optional OAuth redirect URI. Not needed on desktop: Plaid opens the bank's
    login in a pop-up. If set, it must exactly match one registered in the Plaid
    Dashboard and (in Production) be HTTPS."""
    return (os.getenv("PLAID_REDIRECT_URI") or "").strip().strip('"') or None
