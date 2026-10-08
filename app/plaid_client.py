"""Plaid API client. All Plaid calls happen server-side, read-only products only."""
import json
import logging

import plaid
from plaid.api import plaid_api

from .config import plaid_credentials

log = logging.getLogger("plaid")

_client = None
_client_env = None


def get_client():
    global _client, _client_env
    client_id, secret, env = plaid_credentials()
    if _client is None or _client_env != env:
        host = plaid.Environment.Production if env == "production" else plaid.Environment.Sandbox
        cfg = plaid.Configuration(host=host, api_key={"clientId": client_id, "secret": secret})
        _client = plaid_api.PlaidApi(plaid.ApiClient(cfg))
        _client_env = env
    return _client, env


def plaid_error(e: plaid.ApiException) -> dict:
    """Parse a Plaid error into safe fields (never includes request bodies/tokens)."""
    try:
        body = json.loads(e.body)
    except Exception:
        return {"error_type": "UNKNOWN", "error_code": str(getattr(e, "status", "")),
                "error_message": "Unparseable Plaid error", "display_message": None, "request_id": None}
    return {k: body.get(k) for k in
            ("error_type", "error_code", "error_message", "display_message", "request_id")}


def plaid_error_summary(e: plaid.ApiException) -> str:
    d = plaid_error(e)
    return f"{d['error_type']}/{d['error_code']}: {d['error_message']} (request_id={d['request_id']})"
