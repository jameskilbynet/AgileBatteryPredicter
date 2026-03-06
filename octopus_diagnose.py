#!/usr/bin/env python3
"""
Octopus Energy — Account Diagnostic Tool
=========================================
Dumps the raw account structure and probes every meter/MPAN combination
found, so you can see exactly why the consumption endpoint returns 0.

Usage:
    python3 octopus_diagnose.py --api-key sk_live_...
    # or set env var: export OCTOPUS_API_KEY=sk_live_... then python3 octopus_diagnose.py
"""

import argparse
import os
import requests
import json
from datetime import datetime, timezone

BASE_URL = "https://api.octopus.energy/v1"
GRAPHQL_URL = "https://api.octopus.energy/v1/graphql/"


def get_token(api_key):
    resp = requests.post(
        GRAPHQL_URL,
        json={"query": 'mutation { obtainKrakenToken(input: {APIKey: "%s"}) { token } }' % api_key},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if "errors" in data:
        raise RuntimeError(data["errors"])
    return data["data"]["obtainKrakenToken"]["token"]


def get_account_number(token):
    resp = requests.post(
        GRAPHQL_URL,
        json={"query": "{ viewer { accounts { number } } }"},
        headers={"Authorization": f"JWT {token}"},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if "errors" in data:
        raise RuntimeError(data["errors"])
    return data["data"]["viewer"]["accounts"][0]["number"]


def probe(mpan, serial, api_key, label=""):
    """Hit the consumption endpoint with no filters; print count + first record."""
    url = f"{BASE_URL}/electricity-meter-points/{mpan}/meters/{serial}/consumption/"
    try:
        resp = requests.get(url, auth=(api_key, ""), params={"page_size": 5}, timeout=30)
        print(f"    HTTP {resp.status_code}")
        if resp.status_code != 200:
            print(f"    Body: {resp.text[:300]}")
            return
        data = resp.json()
        count = data.get("count", "?")
        results = data.get("results", [])
        print(f"    count={count}  results_on_page={len(results)}")
        if results:
            print(f"    First : {results[0]}")
            print(f"    Last  : {results[-1]}")
        else:
            print("    *** No results returned ***")
    except Exception as e:
        print(f"    ERROR: {e}")


def main():
    parser = argparse.ArgumentParser(description="Octopus Energy Account Diagnostic Tool")
    parser.add_argument(
        "--api-key",
        default=os.environ.get("OCTOPUS_API_KEY"),
        help="Octopus Energy API key (or set OCTOPUS_API_KEY env var)",
    )
    args = parser.parse_args()

    if not args.api_key:
        parser.error(
            "API key required. Pass --api-key sk_live_... or set the OCTOPUS_API_KEY environment variable."
        )
    api_key = args.api_key

    # ── 1. Auth ──────────────────────────────────────────────────────────────
    print("=" * 65)
    print("  Octopus Energy Diagnostic Tool")
    print("=" * 65)

    print("\n[1] Authenticating…")
    token = get_token(api_key)
    print("  ✓ Token obtained")

    account_number = get_account_number(token)
    print(f"  ✓ Account: {account_number}")

    # ── 2. Raw account REST dump ──────────────────────────────────────────────
    print(f"\n[2] Fetching raw account JSON from REST API…")
    resp = requests.get(f"{BASE_URL}/accounts/{account_number}/", auth=(api_key, ""), timeout=30)
    resp.raise_for_status()
    account = resp.json()

    print("\n  ── Raw account structure (truncated) ──")
    raw = json.dumps(account, indent=2)
    print(raw[:4000])
    if len(raw) > 4000:
        print(f"  … (truncated, total {len(raw)} chars)")

    # ── 3. List every meter and probe each one ────────────────────────────────
    print("\n[3] Probing every meter/MPAN combination found…")
    combos_tried = 0
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")

    for pi, prop in enumerate(account.get("properties", [])):
        addr = prop.get("address_line_1", f"property {pi}")
        moved_out = prop.get("moved_out_at", "")
        moved_in  = prop.get("moved_in_at", "")
        is_current = not moved_out or moved_out[:19] > now_str
        status = "✓ CURRENT" if is_current else f"✗ MOVED OUT {moved_out[:10]}"
        print(f"\n  Property {pi}: {addr}  [{status}]  moved_in={moved_in[:10]}")
        for emp in prop.get("electricity_meter_points", []):
            mpan = emp.get("mpan")
            profile = emp.get("profile_class")
            agreements = emp.get("agreements", [])
            tariff_codes = [a.get("tariff_code") for a in agreements]
            print(f"\n    MPAN: {mpan}  profile_class={profile}")
            print(f"    Agreements/tariffs: {tariff_codes}")
            for mi, meter in enumerate(emp.get("meters", [])):
                serial = meter.get("serial_number")
                is_export = meter.get("is_export", "?")
                make = meter.get("make", "?")
                model = meter.get("model", "?")
                print(f"\n    Meter {mi}: serial={serial}  is_export={is_export}  make={make}  model={model}")
                print(f"    → Probing consumption endpoint…")
                probe(mpan, serial, api_key)
                combos_tried += 1

    print(f"\n[4] Done. Tried {combos_tried} MPAN/meter combination(s).")
    print("=" * 65)


if __name__ == "__main__":
    main()
