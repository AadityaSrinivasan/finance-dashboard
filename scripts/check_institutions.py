"""Look up which Plaid products each of your institutions supports.

Uses /institutions/search, which only reads Plaid's public institution
catalog. It does NOT create Items and does not use up any Production slots.

Run:  .venv\\Scripts\\python scripts\\check_institutions.py "My Bank" "My Brokerage"
"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.plaid_client import get_client, plaid_error_summary  # noqa: E402

import plaid  # noqa: E402
from plaid.model.country_code import CountryCode  # noqa: E402
from plaid.model.institutions_search_request import InstitutionsSearchRequest  # noqa: E402
from plaid.model.institutions_search_request_options import InstitutionsSearchRequestOptions  # noqa: E402

WANTED = ["transactions", "investments", "liabilities", "balance"]


def main():
    queries = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not queries:
        print('Usage: python scripts\\check_institutions.py "Bank name" ["Another bank" ...] [--json]')
        return
    client, env = get_client()
    print(f"Plaid environment used for lookup: {env}\n")
    out = {}
    for q in queries:
        req = InstitutionsSearchRequest(
            query=q,
            country_codes=[CountryCode("US")],
            options=InstitutionsSearchRequestOptions(include_optional_metadata=True),
        )
        try:
            resp = client.institutions_search(req).to_dict()
        except plaid.ApiException as e:
            print(f"{q}: lookup failed -> {plaid_error_summary(e)}")
            continue
        rows = []
        for inst in resp["institutions"][:6]:
            prods = [str(p) for p in inst.get("products", [])]
            rows.append({
                "id": inst["institution_id"],
                "name": inst["name"],
                "oauth": inst.get("oauth"),
                "supports": {p: (p in prods) for p in WANTED},
                "all_products": prods,
            })
        out[q] = rows
        print(f"=== search: {q!r} ===")
        for r in rows:
            flags = ", ".join(f"{p}={'yes' if v else 'no'}" for p, v in r["supports"].items())
            print(f"  {r['name']} ({r['id']}) oauth={r['oauth']}  {flags}")
        print()
    if "--json" in sys.argv:
        print(json.dumps(out, indent=2, default=str))


if __name__ == "__main__":
    main()
