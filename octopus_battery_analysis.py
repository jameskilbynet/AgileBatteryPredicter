#!/usr/bin/env python3
"""
Octopus Energy Battery Analysis Tool
=====================================
Connects to the Octopus Energy API, pulls your Agile tariff consumption data,
models battery storage savings, and generates a full HTML report with cost of
ownership calculations.

Usage:
    pip install requests
    python3 octopus_battery_analysis.py --api-key sk_live_...
    # or set env var: export OCTOPUS_API_KEY=sk_live_... then python3 octopus_battery_analysis.py

Author: Generated for James via Claude / Cowork
Date: March 2026
"""

import argparse
import csv
import requests
import json
import sys
import os
from datetime import datetime, timedelta, timezone
from collections import defaultdict
import math


def _load_dotenv():
    """Load key=value pairs from a .env file into os.environ (if it exists)."""
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(env_path):
        return
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                os.environ.setdefault(key.strip(), value.strip())

_load_dotenv()

# ─────────────────────────────────────────────
#  CONFIGURATION — edit these if needed
# ─────────────────────────────────────────────
# API_KEY is resolved at runtime from --api-key flag or OCTOPUS_API_KEY env var
API_KEY = None
BASE_URL = "https://api.octopus.energy/v1"
GRAPHQL_URL = "https://api.octopus.energy/v1/graphql/"
OUTPUT_FILE = "octopus_battery_report.html"

# Battery models to evaluate (UK market installed prices, March 2026 estimates)
BATTERY_MODELS = [
    {
        "name": "GivEnergy 9.5 kWh",
        "brand": "GivEnergy",
        "capacity_kwh": 9.5,
        "usable_kwh": 8.4,          # 88% usable
        "efficiency": 0.90,          # round-trip efficiency
        "installed_cost_gbp": 5500,
        "warranty_years": 10,
        "cycles_warranty": 6000,
        "degradation_pct_pa": 2.5,
    },
    {
        "name": "Tesla Powerwall 3 (13.5 kWh)",
        "brand": "Tesla",
        "capacity_kwh": 13.5,
        "usable_kwh": 13.5,
        "efficiency": 0.90,
        "installed_cost_gbp": 9500,
        "warranty_years": 10,
        "cycles_warranty": 3650,
        "degradation_pct_pa": 3.0,
    },
    {
        "name": "Solax Triple Power 10 kWh",
        "brand": "Solax",
        "capacity_kwh": 10.0,
        "usable_kwh": 8.8,
        "efficiency": 0.89,
        "installed_cost_gbp": 5800,
        "warranty_years": 10,
        "cycles_warranty": 6000,
        "degradation_pct_pa": 2.5,
    },
    {
        "name": "SunSynk 10.65 kWh",
        "brand": "SunSynk",
        "capacity_kwh": 10.65,
        "usable_kwh": 9.6,
        "efficiency": 0.91,
        "installed_cost_gbp": 6200,
        "warranty_years": 10,
        "cycles_warranty": 6000,
        "degradation_pct_pa": 2.5,
    },
    {
        "name": "Alpha ESS Storion T10 (10 kWh)",
        "brand": "Alpha ESS",
        "capacity_kwh": 10.0,
        "usable_kwh": 9.0,
        "efficiency": 0.88,
        "installed_cost_gbp": 5200,
        "warranty_years": 10,
        "cycles_warranty": 4000,
        "degradation_pct_pa": 3.0,
    },
]

# Agile charging strategy — default thresholds
CHARGE_BELOW_P_PER_KWH = 10.0   # Charge battery when rate is below this (pence/kWh)
DISCHARGE_ABOVE_P_PER_KWH = 20.0  # Discharge battery when rate is above this (pence/kWh)

# Optimisation grid — thresholds to sweep when finding the best strategy
OPT_CHARGE_THRESHOLDS    = [-10, -5, 0, 5, 10, 15, 20]   # p/kWh
OPT_DISCHARGE_THRESHOLDS = [10, 15, 20, 25, 30, 35, 40]  # p/kWh

# ─────────────────────────────────────────────
#  SOLAR CONFIGURATION
# ─────────────────────────────────────────────
SOLAR_SIZES_KWP = [3.0, 4.0, 6.0]           # System sizes to evaluate
SOLAR_INSTALL_COST_PER_KWP = 1600            # £/kWp all-in installed (UK 2026 estimate)
SEG_EXPORT_RATE_P = 15.0                     # Smart Export Guarantee rate (p/kWh, typical 2026)
SOLAR_DEGRADATION_PCT_PA = 0.5               # %/year (typical mono-crystalline panels)

# UK monthly solar yield (kWh per kWp per day) — south-facing, 30–40° pitch, UK average
UK_SOLAR_MONTHLY_KWH_PER_KWP = {
    1: 0.80,  2: 1.15,  3: 2.20,  4: 3.40,  5: 4.30,  6: 4.60,
    7: 4.40,  8: 3.90,  9: 3.00, 10: 1.80, 11: 0.95, 12: 0.70,
}


# ─────────────────────────────────────────────
#  API HELPERS
# ─────────────────────────────────────────────

def get_kraken_token():
    """Obtain a Kraken JWT token using the API key."""
    query = """
    mutation ObtainToken($apiKey: String!) {
        obtainKrakenToken(input: {APIKey: $apiKey}) {
            token
        }
    }
    """
    resp = requests.post(
        GRAPHQL_URL,
        json={"query": query, "variables": {"apiKey": API_KEY}},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if "errors" in data:
        raise RuntimeError(f"GraphQL error: {data['errors']}")
    return data["data"]["obtainKrakenToken"]["token"]


def get_account_number(token):
    """Retrieve just the account number via a minimal GraphQL query."""
    query = "{ viewer { accounts { number } } }"
    resp = requests.post(
        GRAPHQL_URL,
        json={"query": query},
        headers={"Authorization": f"JWT {token}"},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if "errors" in data:
        raise RuntimeError(f"GraphQL error: {data['errors']}")
    accounts = data["data"]["viewer"]["accounts"]
    if not accounts:
        raise RuntimeError("No accounts found on this API key.")
    return accounts[0]["number"]


def get_account_details_rest(account_number):
    """
    Fetch full account details (meters, tariffs) via the REST API.
    Returns (mpan, serial_number, tariff_code).
    """
    url = f"{BASE_URL}/accounts/{account_number}/"
    resp = requests.get(url, auth=(API_KEY, ""), timeout=30)
    resp.raise_for_status()
    data = resp.json()

    now = datetime.now(timezone.utc)
    now_str = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    # Walk properties → electricity_meter_points → meters / agreements
    # Skip any property the customer has already moved out of
    for prop in data.get("properties", []):
        moved_out = prop.get("moved_out_at")
        if moved_out:
            # Normalise: strip timezone suffix for a basic string comparison
            moved_out_clean = moved_out[:19].replace("T", "T")
            if moved_out_clean < now_str[:19]:
                addr = prop.get("address_line_1", "unknown")
                print(f"  → Skipping old property: {addr} (moved out {moved_out[:10]})")
                continue
        for emp in prop.get("electricity_meter_points", []):
            mpan = emp.get("mpan")
            # Skip export meters
            meters = [m for m in emp.get("meters", []) if not m.get("is_export", False)]
            if not meters:
                continue
            serial = meters[0].get("serial_number")

            # Find the most recent agreement whose valid_from is in the past.
            # Some accounts have all agreements with a valid_to date (even active ones).
            # Strategy: take agreement with latest valid_from that is <= now.
            tariff_code = None
            agreements = emp.get("agreements", [])
            # Sort by valid_from descending; pick the first one that has started
            valid_started = [
                ag for ag in agreements
                if ag.get("valid_from", "9999") <= now_str
            ]
            if valid_started:
                latest = sorted(valid_started,
                                key=lambda a: a.get("valid_from", ""), reverse=True)[0]
                tariff_code = latest.get("tariff_code")

            # If still None, just take the most recent one regardless
            if tariff_code is None and agreements:
                latest = sorted(agreements,
                                key=lambda a: a.get("valid_from", ""), reverse=True)[0]
                tariff_code = latest.get("tariff_code")

            if mpan and serial:
                return mpan, serial, tariff_code

    raise RuntimeError(
        "Could not find an electricity meter point in your account. "
        "Check the account has an active electricity meter."
    )


def fmt_dt(dt):
    """Format a datetime as a plain ISO 8601 string without microseconds."""
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def normalise_ts(ts):
    """
    Convert any ISO 8601 timestamp string to a canonical UTC 'YYYY-MM-DDTHH:MM:SSZ'
    string suitable for use as a dictionary key.
    Handles: trailing Z, +HH:MM offsets (BST = +01:00), microseconds.
    """
    if not ts:
        return ts
    try:
        # Strip microseconds
        if "." in ts:
            dot = ts.index(".")
            end = next((i for i, c in enumerate(ts[dot:], dot) if c in ("+", "-", "Z")), len(ts))
            ts = ts[:dot] + ts[end:]
        # Normalise Z → +00:00 so fromisoformat works on Python < 3.11
        ts_iso = ts.replace("Z", "+00:00")
        dt = datetime.fromisoformat(ts_iso)
        # Convert to UTC
        if dt.utcoffset() is not None:
            dt = dt.utctimetuple()
            return "%04d-%02d-%02dT%02d:%02d:%02dZ" % dt[:6]
        return ts[:19] + "Z"
    except Exception:
        return ts[:19] + "Z"


def probe_consumption(mpan, serial):
    """
    Make a single no-filter request to the consumption endpoint (returns newest data
    first by default) to confirm data exists and discover the latest available date.
    Returns (count_total, latest_period, oldest_period_on_page) or (0, None, None).
    """
    base = f"{BASE_URL}/electricity-meter-points/{mpan}/meters/{serial}/consumption/"
    resp = requests.get(base, auth=(API_KEY, ""), params={"page_size": 10}, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    results = data.get("results", [])
    total = data.get("count", 0)
    if not results:
        return 0, None, None
    # Default sort is newest-first, so results[0] is the most recent record
    latest = results[0].get("interval_end") or results[0].get("interval_start")
    oldest = results[-1].get("interval_start")
    return total, latest, oldest


def get_consumption(mpan, serial, date_from, date_to):
    """
    Fetch half-hourly electricity consumption via REST API (paginated).
    Octopus API max page_size is 100; we paginate automatically.
    SMETS1 note: data is only indexed up to ~48 hours ago, so period_to is
    capped at (now - 48 hours) to avoid getting 0 results.
    """
    results = []
    url = (
        f"{BASE_URL}/electricity-meter-points/{mpan}/meters/{serial}/consumption/"
        f"?period_from={fmt_dt(date_from)}&period_to={fmt_dt(date_to)}&page_size=100"
    )
    page = 1
    while url:
        resp = requests.get(url, auth=(API_KEY, ""), timeout=60)
        resp.raise_for_status()
        data = resp.json()
        # Debug: on first page, show the raw count and first record
        if page == 1:
            raw_count = data.get("count", "?")
            print(f"    API reports {raw_count} total records for this range.")
            if data.get("results"):
                print(f"    First record: {data['results'][0]}")
        batch = data.get("results", [])
        results.extend(batch)
        url = data.get("next")
        if url:
            page += 1
            if page % 10 == 0:
                print(f"    … fetched {len(results):,} intervals so far (page {page})")
    return results


def find_agile_product_and_tariff(tariff_code):
    """
    Given a tariff code (or None), return a (product_code, full_tariff_code) pair
    that is confirmed to exist on the Octopus products API.

    Strategy:
      1. Derive product code from tariff_code and verify it exists via HEAD/GET.
      2. If 404, search /v1/products/ for all Agile products, pick the most recent,
         and reconstruct the tariff code using the original region letter.
    """
    region = None
    product_code = None

    if tariff_code and len(tariff_code.split("-")) >= 4:
        parts = tariff_code.split("-")
        region = parts[-1]                    # e.g. 'C'
        product_code = "-".join(parts[2:-1])  # e.g. 'AGILE-24-10-01'
        # Quick check — does this product exist?
        check = requests.get(f"{BASE_URL}/products/{product_code}/", timeout=15)
        if check.status_code == 200:
            print(f"  ✓ Confirmed product: {product_code}  tariff: {tariff_code}")
            return product_code, tariff_code
        print(f"  ⚠  Product {product_code} not found (HTTP {check.status_code}). "
              "Searching products list…")

    # Search all Octopus products for Agile ones
    page_url = f"{BASE_URL}/products/?brand=OCTOPUS_ENERGY&is_variable=true&page_size=100"
    agile_products = []
    while page_url:
        resp = requests.get(page_url, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        for p in data.get("results", []):
            if "AGILE" in p.get("code", "").upper():
                agile_products.append(p)
        page_url = data.get("next")

    if not agile_products:
        print("  ⚠  No Agile products found via products API.")
        return None, None

    # Sort by available_from descending — use the most recent product
    agile_products.sort(key=lambda p: p.get("available_from") or "", reverse=True)
    print(f"  → Found {len(agile_products)} Agile product(s). "
          f"Most recent: {agile_products[0]['code']}")

    for prod in agile_products:
        pc = prod["code"]
        # Reconstruct tariff code using the known region, or try common regions
        regions_to_try = ([region] if region else []) + \
                         ["A","B","C","D","E","F","G","H","J","K","L","M","N","P"]
        for reg in regions_to_try:
            tc = f"E-1R-{pc}-{reg}"
            # Verify this tariff exists in the product
            check = requests.get(
                f"{BASE_URL}/products/{pc}/electricity-tariffs/{tc}/half-hour-periods/"
                f"?page_size=1", timeout=15
            )
            if check.status_code == 200:
                print(f"  ✓ Using product: {pc}  tariff: {tc}")
                return pc, tc
    print("  ⚠  Could not find a valid Agile tariff for any region.")
    return None, None


def fetch_agile_rates_for_tariff(product_code, tariff_code, date_from, date_to):
    """
    Paginate through Agile unit rates for a confirmed product+tariff.
    Agile rates live at /standard-unit-rates/ (one entry per 30-min slot,
    with valid_from/valid_to timestamps). page_size=1500 covers ~31 days.
    """
    results = []
    chunk_start = date_from
    while chunk_start < date_to:
        chunk_end = min(chunk_start + timedelta(days=30), date_to)
        url = (
            f"{BASE_URL}/products/{product_code}/electricity-tariffs/{tariff_code}"
            f"/standard-unit-rates/?period_from={fmt_dt(chunk_start)}"
            f"&period_to={fmt_dt(chunk_end)}&page_size=1500"
        )
        while url:
            resp = requests.get(url, timeout=60)
            resp.raise_for_status()
            data = resp.json()
            results.extend(data.get("results", []))
            url = data.get("next")
        chunk_start = chunk_end
    return results


def get_agile_rates(tariff_code, date_from, date_to):
    """
    Fetch half-hourly Agile unit rates for date_from → date_to.
    - Filters out OUTGOING / export products (only want import rates).
    - A full year may span two Agile product vintages; fetches each segment.
    - Uses /standard-unit-rates/ which is the correct endpoint for Agile.
    """
    # Step 1: Get all import Agile products, sorted oldest→newest
    page_url = f"{BASE_URL}/products/?brand=OCTOPUS_ENERGY&is_variable=true&page_size=100"
    agile_products = []
    while page_url:
        resp = requests.get(page_url, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        for p in data.get("results", []):
            code = p.get("code", "")
            # Keep only import Agile products — exclude OUTGOING / export variants
            if "AGILE" in code.upper() and "OUTGOING" not in code.upper():
                agile_products.append(p)
        page_url = data.get("next")

    if not agile_products:
        print("  ⚠  No import Agile products found.")
        return []

    agile_products.sort(key=lambda p: p.get("available_from") or "")
    print(f"  → Found {len(agile_products)} import Agile product(s): "
          f"{[p['code'] for p in agile_products]}")

    # Step 2: Determine region letter
    region = None
    if tariff_code and tariff_code.count("-") >= 4:
        region = tariff_code.split("-")[-1]
        print(f"  → Region from tariff code: {region}")

    if not region:
        # Auto-detect by probing most recent product with each region letter
        pc = agile_products[-1]["code"]
        for reg in ["A","B","C","D","E","F","G","H","J","K","L","M","N","P"]:
            tc = f"E-1R-{pc}-{reg}"
            r = requests.get(
                f"{BASE_URL}/products/{pc}/electricity-tariffs/{tc}"
                f"/standard-unit-rates/?page_size=1", timeout=10
            )
            if r.status_code == 200 and r.json().get("results"):
                region = reg
                print(f"  → Auto-detected region: {region}")
                break

    if not region:
        print("  ⚠  Could not determine grid region. Cannot fetch rates.")
        return []

    # Step 3: Fetch rates from whichever product(s) cover the date range
    all_rates = []
    for prod in agile_products:
        pc = prod["code"]
        prod_from_str = prod.get("available_from") or "2000-01-01T00:00:00Z"
        prod_to_str   = prod.get("available_to")    # None = still active

        try:
            prod_from = datetime.fromisoformat(
                prod_from_str.replace("Z", "+00:00")).replace(tzinfo=timezone.utc)
        except Exception:
            prod_from = date_from

        prod_to = date_to  # default: treat as still active
        if prod_to_str:
            try:
                prod_to = datetime.fromisoformat(
                    prod_to_str.replace("Z", "+00:00")).replace(tzinfo=timezone.utc)
            except Exception:
                pass

        seg_start = max(date_from, prod_from)
        seg_end   = min(date_to,   prod_to)
        if seg_start >= seg_end:
            continue  # this product doesn't cover our window

        tc = f"E-1R-{pc}-{region}"
        print(f"  → Fetching from {pc}  tariff {tc}")
        print(f"     window: {fmt_dt(seg_start)} → {fmt_dt(seg_end)}")
        try:
            chunk = fetch_agile_rates_for_tariff(pc, tc, seg_start, seg_end)
            print(f"     Got {len(chunk):,} rate records.")
            all_rates.extend(chunk)
        except Exception as e:
            print(f"     ⚠  Failed: {e}")

    return all_rates


# ─────────────────────────────────────────────
#  ANALYSIS ENGINE
# ─────────────────────────────────────────────

def build_rate_map(rates):
    """
    Build a dict mapping normalised UTC timestamp → rate (pence/kWh).
    Normalising ensures BST (+01:00) and UTC (Z) timestamps match correctly.
    """
    rate_map = {}
    for r in rates:
        key = normalise_ts(r["valid_from"])
        rate_map[key] = r["value_inc_vat"]
    return rate_map


def calculate_actual_costs(consumption, rate_map):
    """
    For each consumption interval, look up the rate and compute cost.
    Uses normalised UTC timestamps so BST consumption records match UTC rate keys.
    Returns list of dicts with interval data.
    """
    intervals = []
    missing_rate_count = 0
    for c in consumption:
        raw_period = c["interval_start"]
        norm_period = normalise_ts(raw_period)
        kwh = c["consumption"]
        rate = rate_map.get(norm_period)
        if rate is None:
            missing_rate_count += 1
            rate = 0.0
        cost_pence = kwh * rate
        intervals.append({
            "period": norm_period,   # always UTC for consistent grouping
            "kwh": kwh,
            "rate_p": rate,
            "cost_p": cost_pence,
        })
    if missing_rate_count > 0:
        print(f"  ⚠  {missing_rate_count} intervals had no matching rate (set to 0p).")
    return intervals


def model_battery_savings(intervals, battery_usable_kwh, battery_efficiency,
                          charge_threshold, discharge_threshold):
    """
    Simulate a simple time-of-use battery strategy on Agile:
      - Charge when rate < charge_threshold (p/kWh)
      - Discharge when rate > discharge_threshold (p/kWh)
    Returns (annual_saving_pence, charge_kwh_pa, discharge_kwh_pa)
    """
    # Group intervals by date
    by_date = defaultdict(list)
    for iv in intervals:
        date_str = iv["period"][:10]
        by_date[date_str].append(iv)

    total_saving_pence = 0.0
    total_charge_kwh = 0.0
    total_discharge_kwh = 0.0
    days_simulated = 0

    for date_str, day_ivs in sorted(by_date.items()):
        soc = 0.0  # state of charge (kWh)
        day_saving = 0.0
        day_charge = 0.0
        day_discharge = 0.0

        # Sort by period
        day_ivs_sorted = sorted(day_ivs, key=lambda x: x["period"])

        for iv in day_ivs_sorted:
            rate = iv["rate_p"]
            kwh_needed = iv["kwh"]  # consumption this slot

            if rate <= charge_threshold and soc < battery_usable_kwh:
                # Opportunity to charge — how much can we charge?
                space = battery_usable_kwh - soc
                charge_kwh = min(space, battery_usable_kwh / 4)  # max C/4 rate (4 slots)
                actual_stored = charge_kwh * battery_efficiency
                soc += actual_stored
                day_charge += charge_kwh
                # Cost of charging at cheap rate
                day_saving -= charge_kwh * rate  # we pay this

            elif rate >= discharge_threshold and soc > 0 and kwh_needed > 0:
                # Discharge to meet load
                discharge_kwh = min(soc, kwh_needed)
                soc -= discharge_kwh
                day_discharge += discharge_kwh
                # Saving: we avoid buying at expensive rate, minus what we paid to charge
                day_saving += discharge_kwh * rate

        total_saving_pence += day_saving
        total_charge_kwh += day_charge
        total_discharge_kwh += day_discharge
        days_simulated += 1

    # Annualise if we don't have a full year
    if days_simulated > 0:
        scale = 365.0 / days_simulated
    else:
        scale = 1.0

    # The "saving" above is (income from discharge at expensive rate) - (cost of charging at cheap rate)
    # But we already removed the charging cost from the saving calc above
    # So total_saving_pence is already net

    return (
        total_saving_pence * scale,
        total_charge_kwh * scale,
        total_discharge_kwh * scale,
        days_simulated,
    )


def optimise_thresholds(intervals, battery):
    """
    Grid-search over (charge_threshold, discharge_threshold) pairs to find
    the combination that maximises annual net savings for this battery.

    Returns:
        best_charge_p   – optimal charge threshold (p/kWh)
        best_discharge_p – optimal discharge threshold (p/kWh)
        best_saving_gbp – annual saving at optimal thresholds
        grid            – dict[(charge_p, discharge_p)] -> saving_gbp  (for heatmap)
    """
    best_saving = -float("inf")
    best_charge = CHARGE_BELOW_P_PER_KWH
    best_discharge = DISCHARGE_ABOVE_P_PER_KWH
    grid = {}

    for ct in OPT_CHARGE_THRESHOLDS:
        for dt in OPT_DISCHARGE_THRESHOLDS:
            if ct >= dt:
                grid[(ct, dt)] = None   # invalid combination
                continue
            saving_pence, _, _, _ = model_battery_savings(
                intervals, battery["usable_kwh"], battery["efficiency"], ct, dt
            )
            saving_gbp = saving_pence / 100
            grid[(ct, dt)] = round(saving_gbp, 2)
            if saving_gbp > best_saving:
                best_saving = saving_gbp
                best_charge = ct
                best_discharge = dt

    return best_charge, best_discharge, best_saving, grid


def calculate_tco(battery, annual_saving_gbp, analysis_years=15):
    """
    Calculate Total Cost of Ownership over analysis_years.
    Returns dict of key financial metrics.
    """
    cost = battery["installed_cost_gbp"]
    degradation = battery["degradation_pct_pa"] / 100.0
    warranty_years = battery["warranty_years"]

    cumulative_saving = 0.0
    year_by_year = []
    payback_year = None
    capacity_remaining = battery["usable_kwh"]

    for year in range(1, analysis_years + 1):
        # Degradation reduces effective saving each year
        capacity_factor = max(0.7, (1 - degradation) ** (year - 1))
        yr_saving = annual_saving_gbp * capacity_factor
        cumulative_saving += yr_saving
        net_position = cumulative_saving - cost

        if payback_year is None and net_position >= 0:
            # Interpolate exact payback
            prev_cum = cumulative_saving - yr_saving
            fraction = (cost - prev_cum) / yr_saving if yr_saving > 0 else 1
            payback_year = year - 1 + fraction

        year_by_year.append({
            "year": year,
            "capacity_factor": capacity_factor,
            "annual_saving_gbp": yr_saving,
            "cumulative_saving_gbp": cumulative_saving,
            "net_position_gbp": net_position,
            "in_warranty": year <= warranty_years,
        })

    total_saving = cumulative_saving
    roi_pct = ((total_saving - cost) / cost) * 100 if cost > 0 else 0
    irr = estimate_irr(cost, annual_saving_gbp, degradation, analysis_years)

    return {
        "installed_cost": cost,
        "annual_saving_year1": annual_saving_gbp,
        "payback_years": payback_year,
        "total_saving_15yr": total_saving,
        "net_profit_15yr": total_saving - cost,
        "roi_15yr_pct": roi_pct,
        "irr_pct": irr,
        "year_by_year": year_by_year,
    }


def estimate_irr(initial_cost, annual_saving, degradation, years):
    """Simple IRR estimation via binary search."""
    def npv(rate):
        total = -initial_cost
        for yr in range(1, years + 1):
            cf = annual_saving * max(0.7, (1 - degradation) ** (yr - 1))
            total += cf / ((1 + rate) ** yr)
        return total

    lo, hi = -0.5, 2.0
    for _ in range(100):
        mid = (lo + hi) / 2
        if npv(mid) > 0:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2 * 100  # as percentage


def summarise_usage(intervals):
    """Compute summary statistics from consumption intervals."""
    if not intervals:
        return {}

    total_kwh = sum(iv["kwh"] for iv in intervals)
    total_cost_pence = sum(iv["cost_p"] for iv in intervals)
    days = len(set(iv["period"][:10] for iv in intervals))

    # Separate intervals with matched rates (non-zero) from unmatched (rate == 0)
    # Negative rates are valid Agile prices — keep them in all rate calculations.
    matched = [iv for iv in intervals if iv["rate_p"] != 0.0]
    all_rates = [iv["rate_p"] for iv in matched]

    avg_rate = sum(all_rates) / len(all_rates) if all_rates else 0
    max_rate = max(all_rates) if all_rates else 0
    min_rate = min(all_rates) if all_rates else 0

    # Negative rate slots: count intervals where the Agile price went below 0p
    neg_intervals = [iv for iv in intervals if iv["rate_p"] < 0]
    neg_rate_count = len(neg_intervals)
    neg_rate_hours = neg_rate_count * 0.5
    neg_kwh = sum(iv["kwh"] for iv in neg_intervals)
    # cost_p is negative for negative-rate slots (rate * kwh < 0)
    # Taking the absolute value gives what Octopus effectively credited you
    neg_earnings_gbp = sum(abs(iv["cost_p"]) for iv in neg_intervals) / 100

    # Rate bands — only count intervals with a known rate (exclude unmatched 0p slots)
    cheap    = sum(iv["kwh"] for iv in intervals if iv["rate_p"] < 10  and iv["rate_p"] != 0.0)
    negative = sum(iv["kwh"] for iv in intervals if iv["rate_p"] < 0)
    medium   = sum(iv["kwh"] for iv in intervals if 10 <= iv["rate_p"] < 20)
    expensive= sum(iv["kwh"] for iv in intervals if iv["rate_p"] >= 20)

    return {
        "days": days,
        "total_kwh": total_kwh,
        "daily_avg_kwh": total_kwh / days if days else 0,
        "total_cost_gbp": total_cost_pence / 100,
        "daily_avg_cost_gbp": total_cost_pence / 100 / days if days else 0,
        "avg_rate_p": avg_rate,
        "max_rate_p": max_rate,
        "min_rate_p": min_rate,
        "negative_rate_hours": neg_rate_hours,
        "negative_rate_kwh": neg_kwh,
        "negative_earnings_gbp": neg_earnings_gbp,
        "negative_kwh": negative,
        "cheap_kwh": cheap,       # 0p–10p (excludes negative and unmatched)
        "medium_kwh": medium,     # 10p–20p
        "expensive_kwh": expensive,  # ≥20p
    }


# ─────────────────────────────────────────────
#  SOLAR ANALYSIS ENGINE
# ─────────────────────────────────────────────

def make_solar_gen_profile(intervals, solar_kwp):
    """
    Build a synthetic solar generation profile matched to consumption interval timestamps.
    Uses UK monthly yield averages with a daytime Gaussian bell-curve distribution
    centred around solar noon (~12:30 UTC, roughly correct year-round for the UK).

    Returns dict: normalised_period_str -> generation_kWh_per_half_hour_slot.
    """
    # Gaussian weights for each of 48 half-hour slots (slot 0 = 00:00 UTC)
    solar_noon_slot = 25    # ~12:30 UTC ≈ mean solar noon in UK
    sigma_slots = 6         # standard deviation ~3 hours (covers typical daylight)
    slot_weights = [
        math.exp(-0.5 * ((s - solar_noon_slot) / sigma_slots) ** 2)
        for s in range(48)
    ]
    weight_sum = sum(slot_weights)

    # Group intervals by date
    by_date = defaultdict(list)
    for iv in intervals:
        by_date[iv["period"][:10]].append(iv)

    gen_profile = {}
    for date_str, day_ivs in by_date.items():
        month = int(date_str[5:7])
        daily_kwh = UK_SOLAR_MONTHLY_KWH_PER_KWP.get(month, 2.0) * solar_kwp
        # Sort by period so slot indices correspond correctly
        day_sorted = sorted(day_ivs, key=lambda x: x["period"])
        for slot_idx, iv in enumerate(day_sorted):
            si = min(slot_idx, 47)
            gen_kwh = daily_kwh * slot_weights[si] / weight_sum
            gen_profile[iv["period"]] = gen_kwh

    return gen_profile


def model_solar_only(intervals, solar_kwp, seg_rate_p=None):
    """
    Model solar PV without battery storage.
    - Solar generation offsets grid consumption at the current Agile rate (import saving).
    - Excess generation is exported via SEG at seg_rate_p (export income).
    Returns dict with annualised figures and simple TCO.
    """
    if seg_rate_p is None:
        seg_rate_p = SEG_EXPORT_RATE_P

    gen_profile = make_solar_gen_profile(intervals, solar_kwp)

    total_gen = 0.0
    total_self_use = 0.0
    total_export = 0.0
    import_saving_pence = 0.0
    export_income_pence = 0.0

    for iv in intervals:
        gen = gen_profile.get(iv["period"], 0.0)
        rate = iv["rate_p"]
        consume = iv["kwh"]

        self_use = min(gen, consume)
        export = max(0.0, gen - consume)

        total_gen += gen
        total_self_use += self_use
        total_export += export
        # Only count saving at positive rates — at negative rates you're already earning
        import_saving_pence += self_use * max(rate, 0.0)
        export_income_pence += export * seg_rate_p

    days = len(set(iv["period"][:10] for iv in intervals))
    scale = 365.0 / days if days else 1.0

    annual_gen = total_gen * scale
    annual_self_use = total_self_use * scale
    annual_export = total_export * scale
    annual_import_saving = import_saving_pence / 100 * scale
    annual_export_income = export_income_pence / 100 * scale
    annual_total = annual_import_saving + annual_export_income
    self_pct = (total_self_use / total_gen * 100) if total_gen > 0 else 0.0
    install_cost = solar_kwp * SOLAR_INSTALL_COST_PER_KWP

    return {
        "solar_kwp": solar_kwp,
        "install_cost": install_cost,
        "annual_gen_kwh": annual_gen,
        "annual_self_use_kwh": annual_self_use,
        "annual_export_kwh": annual_export,
        "self_consumption_pct": self_pct,
        "annual_import_saving_gbp": annual_import_saving,
        "annual_export_income_gbp": annual_export_income,
        "annual_total_benefit_gbp": annual_total,
        "tco": calculate_solar_tco(solar_kwp, annual_total),
    }


def calculate_solar_tco(solar_kwp, annual_benefit_gbp, analysis_years=15):
    """TCO for standalone solar PV — 0.5%/year degradation, no battery replacement."""
    install_cost = solar_kwp * SOLAR_INSTALL_COST_PER_KWP
    degradation = SOLAR_DEGRADATION_PCT_PA / 100.0

    cumulative = 0.0
    payback_year = None

    for year in range(1, analysis_years + 1):
        factor = (1 - degradation) ** (year - 1)
        yr_benefit = annual_benefit_gbp * factor
        cumulative += yr_benefit
        if payback_year is None and cumulative >= install_cost:
            prev = cumulative - yr_benefit
            fraction = (install_cost - prev) / yr_benefit if yr_benefit > 0 else 1.0
            payback_year = year - 1 + fraction

    roi = ((cumulative - install_cost) / install_cost * 100) if install_cost > 0 else 0
    irr = estimate_irr(install_cost, annual_benefit_gbp, degradation, analysis_years)

    return {
        "installed_cost": install_cost,
        "annual_benefit_year1": annual_benefit_gbp,
        "payback_years": payback_year,
        "total_benefit_15yr": cumulative,
        "net_profit_15yr": cumulative - install_cost,
        "roi_15yr_pct": roi,
        "irr_pct": irr,
    }


def calculate_combined_tco(total_cost, annual_benefit_gbp,
                           solar_deg, battery_deg, analysis_years=15):
    """
    TCO for a combined solar + battery system.
    Uses the average of the two degradation rates for a conservative combined estimate.
    """
    avg_deg = (solar_deg + battery_deg) / 2.0

    cumulative = 0.0
    payback_year = None

    for year in range(1, analysis_years + 1):
        factor = max(0.7, (1 - avg_deg) ** (year - 1))
        yr_benefit = annual_benefit_gbp * factor
        cumulative += yr_benefit
        if payback_year is None and cumulative >= total_cost:
            prev = cumulative - yr_benefit
            fraction = (total_cost - prev) / yr_benefit if yr_benefit > 0 else 1.0
            payback_year = year - 1 + fraction

    roi = ((cumulative - total_cost) / total_cost * 100) if total_cost > 0 else 0
    irr = estimate_irr(total_cost, annual_benefit_gbp, avg_deg, analysis_years)

    return {
        "total_cost": total_cost,
        "annual_benefit_year1": annual_benefit_gbp,
        "payback_years": payback_year,
        "total_benefit_15yr": cumulative,
        "net_profit_15yr": cumulative - total_cost,
        "roi_15yr_pct": roi,
        "irr_pct": irr,
    }


def model_solar_plus_battery(intervals, solar_kwp, battery,
                              charge_p, discharge_p, seg_rate_p=None):
    """
    Model solar PV + battery storage combined.

    Per-slot priority:
    1. Solar meets load directly (self-consumption, saves at current Agile rate).
    2. Excess solar charges the battery (bounded by C/4 rate and usable capacity).
    3. Any remaining excess is exported via SEG.
    4. If Agile rate >= discharge_p and SOC > 0: battery discharges to cover load.
    5. If Agile rate <= charge_p and battery not full: charge from cheap grid supply.

    Returns dict with annualised figures and combined TCO.
    """
    if seg_rate_p is None:
        seg_rate_p = SEG_EXPORT_RATE_P

    gen_profile = make_solar_gen_profile(intervals, solar_kwp)

    by_date = defaultdict(list)
    for iv in intervals:
        by_date[iv["period"][:10]].append(iv)

    total_gen = 0.0
    total_self_use = 0.0
    total_export = 0.0
    total_batt_from_solar = 0.0
    import_saving_pence = 0.0
    export_income_pence = 0.0
    batt_net_pence = 0.0     # battery discharge income − grid charge cost
    days_sim = 0

    for date_str, day_ivs in sorted(by_date.items()):
        soc = 0.0
        day_ivs_sorted = sorted(day_ivs, key=lambda x: x["period"])
        days_sim += 1

        for iv in day_ivs_sorted:
            period = iv["period"]
            rate = iv["rate_p"]
            consume = iv["kwh"]
            gen = gen_profile.get(period, 0.0)

            total_gen += gen

            # 1. Solar self-consumption
            self_use = min(gen, consume)
            remaining_consume = consume - self_use
            remaining_gen = gen - self_use
            total_self_use += self_use
            import_saving_pence += self_use * max(rate, 0.0)

            # 2. Excess solar → battery
            if remaining_gen > 0 and soc < battery["usable_kwh"]:
                space = battery["usable_kwh"] - soc
                solar_to_batt = min(remaining_gen, space, battery["usable_kwh"] / 4)
                soc += solar_to_batt * battery["efficiency"]
                remaining_gen -= solar_to_batt
                total_batt_from_solar += solar_to_batt

            # 3. Remaining generation → export at SEG rate
            if remaining_gen > 0:
                total_export += remaining_gen
                export_income_pence += remaining_gen * seg_rate_p

            # 4. Battery discharges at expensive grid rates
            if rate >= discharge_p and soc > 0 and remaining_consume > 0:
                discharge = min(soc, remaining_consume)
                soc -= discharge
                remaining_consume -= discharge
                batt_net_pence += discharge * rate

            # 5. Charge battery from cheap grid supply
            if rate <= charge_p and soc < battery["usable_kwh"]:
                space = battery["usable_kwh"] - soc
                grid_charge = min(space, battery["usable_kwh"] / 4)
                soc += grid_charge * battery["efficiency"]
                batt_net_pence -= grid_charge * rate

    scale = 365.0 / days_sim if days_sim else 1.0

    annual_gen = total_gen * scale
    annual_self_use = total_self_use * scale
    annual_export = total_export * scale
    annual_import_saving = import_saving_pence / 100 * scale
    annual_export_income = export_income_pence / 100 * scale
    annual_batt_saving = batt_net_pence / 100 * scale
    annual_total = annual_import_saving + annual_export_income + annual_batt_saving
    self_pct = (total_self_use / total_gen * 100) if total_gen > 0 else 0.0

    combined_cost = battery["installed_cost_gbp"] + solar_kwp * SOLAR_INSTALL_COST_PER_KWP
    combined_tco = calculate_combined_tco(
        combined_cost, annual_total,
        SOLAR_DEGRADATION_PCT_PA / 100.0,
        battery["degradation_pct_pa"] / 100.0,
    )

    return {
        "solar_kwp": solar_kwp,
        "battery_name": battery["name"],
        "battery_cost": battery["installed_cost_gbp"],
        "solar_cost": int(solar_kwp * SOLAR_INSTALL_COST_PER_KWP),
        "combined_cost": combined_cost,
        "annual_gen_kwh": annual_gen,
        "annual_self_use_kwh": annual_self_use,
        "annual_export_kwh": annual_export,
        "annual_batt_from_solar_kwh": total_batt_from_solar * scale,
        "self_consumption_pct": self_pct,
        "annual_import_saving_gbp": annual_import_saving,
        "annual_export_income_gbp": annual_export_income,
        "annual_battery_saving_gbp": annual_batt_saving,
        "annual_total_benefit_gbp": annual_total,
        "tco": combined_tco,
    }


# ─────────────────────────────────────────────
#  HTML REPORT GENERATOR
# ─────────────────────────────────────────────

def _saving_to_colour(saving, min_s, max_s):
    """Map a saving value to a green CSS colour; grey for invalid (None)."""
    if saving is None:
        return "#e8e8e8"
    if max_s <= min_s:
        frac = 0.5
    else:
        frac = (saving - min_s) / (max_s - min_s)
    frac = max(0.0, min(1.0, frac))
    # Interpolate: low = hsl(120,20%,90%) → high = hsl(120,70%,35%)
    lightness = int(90 - frac * 55)
    saturation = int(20 + frac * 50)
    text = "white" if lightness < 55 else "#333"
    return f"hsl(120,{saturation}%,{lightness}%)", text


def _build_heatmap_table(battery_results):
    """
    Build an HTML heatmap table for the best battery's optimisation grid.
    Uses the battery with the shortest optimised payback.
    """
    best = min(battery_results,
               key=lambda x: (x.get("opt_tco") or x["tco"]).get("payback_years") or 999)
    grid = best.get("opt_grid", {})
    opt_c = best.get("opt_charge_p", CHARGE_BELOW_P_PER_KWH)
    opt_d = best.get("opt_discharge_p", DISCHARGE_ABOVE_P_PER_KWH)

    if not grid:
        return "<p style='color:#888'>No optimisation data available.</p>"

    valid_savings = [v for v in grid.values() if v is not None]
    min_s = min(valid_savings) if valid_savings else 0
    max_s = max(valid_savings) if valid_savings else 1

    charge_thresholds    = sorted(set(k[0] for k in grid))
    discharge_thresholds = sorted(set(k[1] for k in grid))

    html = """<div style="overflow-x:auto"><table style="border-collapse:collapse;font-size:0.8rem;margin:0 auto">"""
    # Header row: discharge thresholds
    html += "<tr><th style='background:#333;color:white;padding:6px 10px'>charge ↓ / discharge →</th>"
    for dt in discharge_thresholds:
        html += f"<th style='background:#333;color:white;padding:6px 10px'>≥{dt}p</th>"
    html += "</tr>"

    for ct in charge_thresholds:
        html += f"<tr><th style='background:#444;color:white;padding:6px 10px;text-align:right'>≤{ct}p</th>"
        for dt in discharge_thresholds:
            saving = grid.get((ct, dt))
            is_optimal = (ct == opt_c and dt == opt_d)
            if saving is None:
                cell_bg, cell_fg = "#e8e8e8", "#aaa"
                label = "—"
            else:
                result = _saving_to_colour(saving, min_s, max_s)
                cell_bg, cell_fg = result[0], result[1]
                label = f"£{saving:,.0f}"
            star = " ⭐" if is_optimal else ""
            border = "border:2px solid #ffcc00;" if is_optimal else ""
            html += (f"<td style='background:{cell_bg};color:{cell_fg};"
                     f"padding:7px 12px;text-align:center;{border}'>"
                     f"<strong>{label}</strong>{star}</td>")
        html += "</tr>"

    html += "</table></div>"
    html += (f"<p style='font-size:0.82rem;color:#555;margin-top:10px'>"
             f"Optimal: <strong>charge ≤{opt_c}p</strong> / <strong>discharge ≥{opt_d}p</strong> → "
             f"£{best.get('opt_saving_gbp', 0):,.0f}/yr saving | "
             f"default (≤{CHARGE_BELOW_P_PER_KWH}p/≥{DISCHARGE_ABOVE_P_PER_KWH}p): "
             f"£{best['annual_saving_gbp']:,.0f}/yr | "
             f"<strong>uplift: +£{best.get('opt_saving_gbp',0)-best['annual_saving_gbp']:,.0f}/yr</strong></p>")
    return html


def _build_2x_rows(results_1x, results_2x):
    """Build HTML table rows comparing current vs doubled-usage battery scenarios."""
    rows = ""
    lookup_2x = {r["battery"]["name"]: r for r in results_2x}
    for br in sorted(results_1x, key=lambda x: x["tco"]["payback_years"] or 999):
        b = br["battery"]
        t1 = br["tco"]
        t2 = lookup_2x.get(b["name"], {}).get("tco", {})
        pb1 = t1.get("payback_years")
        pb2 = t2.get("payback_years")
        pb1_str = f"{pb1:.1f} yrs" if pb1 else "Never"
        pb2_str = f"{pb2:.1f} yrs" if pb2 else "Never"
        s1 = br["annual_saving_gbp"]
        s2 = lookup_2x.get(b["name"], {}).get("annual_saving_gbp", 0)
        if pb1 and pb2:
            diff = pb1 - pb2
            diff_str = f"⬇ {diff:.1f} yrs faster" if diff > 0 else f"⬆ {abs(diff):.1f} yrs slower"
            diff_colour = "#00aa44" if diff > 0 else "#cc4400"
        else:
            diff_str = "—"
            diff_colour = "#888"
        rows += f"""
        <tr>
          <td><strong>{b['name']}</strong></td>
          <td>£{s1:,.0f}/yr</td><td>{pb1_str}</td>
          <td style="color:#0077cc;font-weight:600">£{s2:,.0f}/yr</td>
          <td style="color:#0077cc;font-weight:600">{pb2_str}</td>
          <td style="color:{diff_colour};font-weight:600">{diff_str}</td>
        </tr>"""
    return rows


def generate_html_report(usage_summary, battery_results, intervals, account_number, mpan,
                         tariff_code, using_synthetic=False,
                         battery_results_2x=None, doubled_summary=None,
                         solar_results=None, solar_battery_results=None):
    """Generate a comprehensive HTML report."""

    # Build JS data for charts
    # Monthly consumption
    monthly = defaultdict(lambda: {"kwh": 0, "cost": 0})
    for iv in intervals:
        month = iv["period"][:7]
        monthly[month]["kwh"] += iv["kwh"]
        monthly[month]["cost"] += iv["cost_p"] / 100

    months_sorted = sorted(monthly.keys())
    monthly_kwh_data = [round(monthly[m]["kwh"], 2) for m in months_sorted]
    monthly_cost_data = [round(monthly[m]["cost"], 2) for m in months_sorted]

    # Rate distribution
    rate_buckets = defaultdict(float)
    for iv in intervals:
        bucket = math.floor(iv["rate_p"] / 5) * 5
        rate_buckets[bucket] += iv["kwh"]
    rate_labels = sorted(rate_buckets.keys())
    rate_values = [round(rate_buckets[k], 2) for k in rate_labels]
    rate_labels_str = [f"{k}p–{k+5}p" for k in rate_labels]

    # Price band table rows
    total_kwh = usage_summary.get("total_kwh", 1) or 1
    band_subtotals = {"negative": (0.0, 0.0), "cheap": (0.0, 0.0),
                      "medium": (0.0, 0.0), "expensive": (0.0, 0.0)}
    band_table_rows = ""
    prev_category = None
    for label, label_str, kwh in zip(rate_labels, rate_labels_str, rate_values):
        midpoint = label + 2.5  # midpoint of 5p-wide band
        est_cost = kwh * midpoint / 100  # £
        pct = kwh / total_kwh * 100
        if label < 0:
            category, badge = "negative", '<span class="badge badge-green">Negative</span>'
            cost_color = "color:#00aa44"
            cost_str = f"−£{abs(est_cost):.2f}"
            bk = "negative"
        elif label < 10:
            category, badge = "cheap", '<span class="badge badge-green">Cheap</span>'
            cost_color = ""
            cost_str = f"£{est_cost:.2f}"
            bk = "cheap"
        elif label < 20:
            category, badge = "medium", '<span class="badge badge-amber">Medium</span>'
            cost_color = ""
            cost_str = f"£{est_cost:.2f}"
            bk = "medium"
        else:
            category, badge = "expensive", '<span class="badge badge-red">Expensive</span>'
            cost_color = ""
            cost_str = f"£{est_cost:.2f}"
            bk = "expensive"
        sub_kwh, sub_cost = band_subtotals[bk]
        band_subtotals[bk] = (sub_kwh + kwh, sub_cost + est_cost)

        if category != prev_category and prev_category is not None:
            # Insert subtotal row for previous category
            pk = {"negative": "negative", "cheap": "cheap", "medium": "medium", "expensive": "expensive"}[prev_category]
            sub_k, sub_c = band_subtotals[pk]
            # adjust: we added current row already, so sub includes current — recompute prev only
            # Actually band_subtotals is incremented each iter, so we need to store before increment
            pass  # handled below after loop
        prev_category = category

        cost_td = f'<td style="{cost_color}">{cost_str}</td>' if cost_color else f"<td>{cost_str}</td>"
        band_table_rows += (
            f"<tr><td>{label_str}</td><td>{kwh:,.1f} kWh</td>"
            f"<td>{pct:.1f}%</td>{cost_td}<td>{badge}</td></tr>\n        "
        )

    # rebuild with subtotals inserted between category groups
    band_table_rows = ""
    prev_category = None
    category_rows: dict = {}
    for label, label_str, kwh in zip(rate_labels, rate_labels_str, rate_values):
        midpoint = label + 2.5
        est_cost = kwh * midpoint / 100
        pct = kwh / total_kwh * 100
        if label < 0:
            category, badge = "negative", '<span class="badge badge-green">Negative</span>'
            cost_str = f"−£{abs(est_cost):.2f}"
            cost_style = ' style="color:#00aa44"'
        elif label < 10:
            category, badge = "cheap", '<span class="badge badge-green">Cheap</span>'
            cost_str = f"£{est_cost:.2f}"
            cost_style = ""
        elif label < 20:
            category, badge = "medium", '<span class="badge badge-amber">Medium</span>'
            cost_str = f"£{est_cost:.2f}"
            cost_style = ""
        else:
            category, badge = "expensive", '<span class="badge badge-red">Expensive</span>'
            cost_str = f"£{est_cost:.2f}"
            cost_style = ""
        if category not in category_rows:
            category_rows[category] = {"rows": "", "kwh": 0.0, "cost": 0.0}
        category_rows[category]["kwh"] += kwh
        category_rows[category]["cost"] += est_cost
        category_rows[category]["rows"] += (
            f"<tr><td>{label_str}</td><td>{kwh:,.1f} kWh</td>"
            f"<td>{pct:.1f}%</td><td{cost_style}>{cost_str}</td><td>{badge}</td></tr>\n        "
        )

    subtotal_labels = {
        "negative": "&lt;0p (Negative)", "cheap": "0–10p (Cheap)",
        "medium": "10–20p (Medium)", "expensive": "&gt;20p (Expensive)"
    }
    subtotal_cost_style = {"negative": ' style="color:#00aa44"', "cheap": "", "medium": "", "expensive": ""}
    for cat in ["negative", "cheap", "medium", "expensive"]:
        if cat not in category_rows:
            continue
        info = category_rows[cat]
        band_table_rows += info["rows"]
        sub_pct = info["kwh"] / total_kwh * 100
        is_neg = cat == "negative"
        cost_disp = f"−£{abs(info['cost']):.2f}" if is_neg else f"£{info['cost']:.2f}"
        band_table_rows += (
            f'<tr class="highlight"><td><strong>Subtotal: {subtotal_labels[cat]}</strong></td>'
            f'<td><strong>{info["kwh"]:,.1f} kWh</strong></td>'
            f'<td><strong>{sub_pct:.1f}%</strong></td>'
            f'<td{subtotal_cost_style[cat]}><strong>{cost_disp}</strong></td>'
            f'<td></td></tr>\n        '
        )
    grand_cost = sum(v["cost"] for v in category_rows.values())
    band_table_rows += (
        f'<tr style="background:rgba(88,64,255,0.3)">'
        f'<td><strong>Total</strong></td>'
        f'<td><strong>{total_kwh:,.0f} kWh</strong></td>'
        f'<td><strong>100%</strong></td>'
        f'<td><strong>£{grand_cost:,.0f}</strong></td>'
        f'<td></td></tr>'
    )

    # TCO chart data for best battery
    best = min(battery_results, key=lambda x: x["tco"]["payback_years"] or 999)
    tco_years = [str(y["year"]) for y in best["tco"]["year_by_year"]]
    tco_cum_saving = [round(y["cumulative_saving_gbp"], 0) for y in best["tco"]["year_by_year"]]
    tco_cost_line = [round(best["tco"]["installed_cost"], 0)] * len(tco_years)

    # Battery comparison table rows (default vs optimised)
    battery_rows = ""
    for i, br in enumerate(sorted(battery_results, key=lambda x: x["tco"]["payback_years"] or 999)):
        b = br["battery"]
        tco = br["tco"]
        opt_tco = br.get("opt_tco", tco)
        opt_cp  = br.get("opt_charge_p", CHARGE_BELOW_P_PER_KWH)
        opt_dp  = br.get("opt_discharge_p", DISCHARGE_ABOVE_P_PER_KWH)
        payback_str = f"{tco['payback_years']:.1f} yrs" if tco["payback_years"] else "Never"
        opt_pb_str  = f"{opt_tco['payback_years']:.1f} yrs" if opt_tco.get("payback_years") else "Never"
        saving_uplift = br.get("opt_saving_gbp", br["annual_saving_gbp"]) - br["annual_saving_gbp"]
        uplift_str = f"+£{saving_uplift:,.0f}/yr" if saving_uplift > 0.5 else "—"
        highlight = ' class="highlight"' if i == 0 else ""
        tag = " ⭐ Best" if i == 0 else ""
        battery_rows += f"""
        <tr{highlight}>
          <td><strong>{b['name']}</strong>{tag}</td>
          <td>{b['capacity_kwh']} kWh</td>
          <td>£{b['installed_cost_gbp']:,}</td>
          <td>£{tco['annual_saving_year1']:,.0f}</td>
          <td>{payback_str}</td>
          <td style="color:#0077cc;font-weight:600">≤{opt_cp}p / ≥{opt_dp}p</td>
          <td style="color:#0077cc;font-weight:600">£{opt_tco.get('annual_saving_year1', tco['annual_saving_year1']):,.0f}</td>
          <td style="color:#0077cc;font-weight:600">{opt_pb_str}</td>
          <td style="color:#00aa44;font-weight:600">{uplift_str}</td>
          <td>£{tco['net_profit_15yr']:,.0f}</td>
          <td>{tco['irr_pct']:.1f}%</td>
        </tr>"""

    # Recommendation text
    pb = best["tco"]["payback_years"]
    rec_note = ""
    if pb and pb < 7:
        rec_note = f"<p class='rec-good'>✅ <strong>Excellent investment</strong> — payback in under 7 years with a 15-year lifespan battery.</p>"
    elif pb and pb < 10:
        rec_note = f"<p class='rec-ok'>🟡 <strong>Good investment</strong> — payback in {pb:.1f} years, positive return over battery lifetime.</p>"
    elif pb:
        rec_note = f"<p class='rec-caution'>⚠️ <strong>Marginal investment</strong> — payback in {pb:.1f} years, close to battery warranty period. Consider smaller capacity.</p>"
    else:
        rec_note = f"<p class='rec-bad'>❌ <strong>Poor investment</strong> — savings do not cover the cost within 15 years on current usage and rates.</p>"

    us = usage_summary
    report_date = datetime.now().strftime("%d %B %Y %H:%M")
    annual_cost_est = us.get("daily_avg_cost_gbp", 0) * 365

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Octopus Energy Battery Analysis — James</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<style>
  :root {{
    --oct-bg: #100030;
    --oct-card: #180048;
    --oct-pink: #f050f8;
    --oct-cyan: #60f0f8;
    --oct-purple: #5840ff;
    --oct-text: #f0ffff;
    --oct-muted: rgba(240,255,255,0.55);
    --oct-green: #00dc64;
    --oct-amber: #ffaa00;
    --oct-border: rgba(255,255,255,0.08);
    --shadow: 0 2px 16px rgba(0,0,0,0.4);
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
          background: var(--oct-bg); color: var(--oct-text); line-height: 1.6; }}
  .header {{ background: linear-gradient(135deg, #2a0060 0%, #100030 100%);
             border-bottom: 3px solid var(--oct-pink);
             color: var(--oct-text); padding: 40px 40px 30px; }}
  .header h1 {{ font-size: 2rem; margin-bottom: 6px; color: var(--oct-cyan); }}
  .header p {{ opacity: 0.8; font-size: 0.95rem; }}
  .meta {{ display: flex; gap: 30px; margin-top: 18px; flex-wrap: wrap; }}
  .meta-item {{ background: rgba(255,255,255,0.08); border-radius: 8px;
                padding: 8px 16px; font-size: 0.85rem; border: 1px solid rgba(255,255,255,0.1); }}
  .meta-item strong {{ display: block; font-size: 1.1rem; color: var(--oct-cyan); }}
  .container {{ max-width: 1200px; margin: 0 auto; padding: 30px 20px; }}
  .grid-2 {{ display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }}
  .grid-3 {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 20px; }}
  .card {{ background: var(--oct-card); border-radius: 12px; padding: 24px;
           box-shadow: var(--shadow); border: 1px solid var(--oct-border); }}
  .card h2 {{ font-size: 1.1rem; color: var(--oct-cyan); margin-bottom: 16px;
              border-bottom: 1px solid var(--oct-border); padding-bottom: 10px; }}
  .stat-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 16px; }}
  .stat {{ text-align: center; padding: 16px 10px; background: rgba(255,255,255,0.05);
           border-radius: 10px; }}
  .stat .value {{ font-size: 1.8rem; font-weight: 700; color: var(--oct-pink); }}
  .stat .label {{ font-size: 0.78rem; color: var(--oct-muted); margin-top: 4px; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 0.85rem; }}
  th {{ background: var(--oct-purple); color: var(--oct-text); padding: 10px 12px; text-align: left; }}
  td {{ padding: 9px 12px; border-bottom: 1px solid var(--oct-border); color: var(--oct-text); }}
  tr:last-child td {{ border-bottom: none; }}
  tr.highlight {{ background: rgba(88,64,255,0.18); }}
  tr.highlight td {{ font-weight: 500; }}
  .chart-container {{ position: relative; height: 280px; }}
  .rec-good {{ background: rgba(0,220,100,0.1); border-left: 4px solid var(--oct-green);
               padding: 14px 18px; border-radius: 0 8px 8px 0; margin: 12px 0; }}
  .rec-ok {{ background: rgba(255,170,0,0.1); border-left: 4px solid var(--oct-amber);
             padding: 14px 18px; border-radius: 0 8px 8px 0; margin: 12px 0; }}
  .rec-caution {{ background: rgba(255,119,34,0.1); border-left: 4px solid #ff7722;
                  padding: 14px 18px; border-radius: 0 8px 8px 0; margin: 12px 0; }}
  .rec-bad {{ background: rgba(232,76,76,0.1); border-left: 4px solid #e84c4c;
              padding: 14px 18px; border-radius: 0 8px 8px 0; margin: 12px 0; }}
  .badge {{ display: inline-block; padding: 2px 8px; border-radius: 12px;
            font-size: 0.75rem; font-weight: 600; }}
  .badge-green {{ background: rgba(0,220,100,0.2); color: #44ee88; }}
  .badge-amber {{ background: rgba(255,170,0,0.2); color: #ffcc44; }}
  .badge-red {{ background: rgba(232,76,76,0.2); color: #ff8888; }}
  .section-title {{ font-size: 1.35rem; font-weight: 700; margin: 28px 0 14px;
                    color: var(--oct-cyan); }}
  .footnote {{ color: var(--oct-muted); font-size: 0.8rem; margin-top: 30px; line-height: 1.5; }}
  .tip {{ background: rgba(88,64,255,0.15); border: 1px solid rgba(88,64,255,0.3);
          border-radius: 10px; padding: 16px 20px; margin: 12px 0; font-size: 0.9rem; }}
  .tip strong {{ color: var(--oct-cyan); }}
  @media (max-width: 768px) {{
    .grid-2, .grid-3 {{ grid-template-columns: 1fr; }}
    .header h1 {{ font-size: 1.5rem; }}
  }}
</style>
</head>
<body>

<div class="header">
  <h1>⚡ Battery Storage Analysis</h1>
  <p>Personalised recommendation based on your Agile Octopus usage data</p>
  <div class="meta">
    <div class="meta-item"><strong>{account_number}</strong>Account</div>
    <div class="meta-item"><strong>Agile Octopus</strong>Tariff</div>
    <div class="meta-item"><strong>{us.get('days', 0)} days</strong>Data period</div>
    <div class="meta-item"><strong>£{annual_cost_est:,.0f}/yr</strong>Est. annual bill</div>
    <div class="meta-item"><strong>{report_date}</strong>Report generated</div>
  </div>
</div>

<div class="container">

  {"<!-- SYNTHETIC DATA WARNING --><div style='background:#fff3cd;border-left:4px solid #ffaa00;border-radius:0 8px 8px 0;padding:14px 18px;margin-bottom:20px;font-size:0.9rem'><strong>⚠ Indicative results — no smart meter data available via API</strong><br>Your meter hasn't shared half-hourly data with Octopus yet (common for SMETS1 meters or recently enrolled accounts). This report uses a <em>UK-typical load profile (3,100 kWh/yr)</em> paired with a representative Agile rate curve. Results will improve once real data becomes available — re-run the script in a few weeks.</div>" if using_synthetic else ""}

  <!-- USAGE SUMMARY -->
  <p class="section-title">📊 Your Usage Summary</p>
  <div class="card">
    <div class="stat-grid">
      <div class="stat">
        <div class="value">{us.get('total_kwh', 0):,.0f}</div>
        <div class="label">Total kWh consumed</div>
      </div>
      <div class="stat">
        <div class="value">{us.get('daily_avg_kwh', 0):.1f}</div>
        <div class="label">Daily avg kWh</div>
      </div>
      <div class="stat">
        <div class="value">£{us.get('total_cost_gbp', 0):,.0f}</div>
        <div class="label">Total cost (data period)</div>
      </div>
      <div class="stat">
        <div class="value">{us.get('avg_rate_p', 0):.1f}p</div>
        <div class="label">Avg rate (p/kWh)</div>
      </div>
      <div class="stat">
        <div class="value">{us.get('max_rate_p', 0):.1f}p</div>
        <div class="label">Peak rate (p/kWh)</div>
      </div>
      <div class="stat">
        <div class="value" style="color:{'#00cc66' if us.get('negative_rate_hours',0)>0 else '#999'}">{us.get('negative_rate_hours', 0):.0f}</div>
        <div class="label">Hours at negative rates</div>
      </div>
      <div class="stat" style="background:#e8faf0;border:2px solid #00cc66">
        <div class="value" style="color:#00aa44">£{us.get('negative_earnings_gbp', 0):.2f}</div>
        <div class="label">Earned at negative rates</div>
      </div>
      <div class="stat">
        <div class="value" style="color:#00cc66">{us.get('min_rate_p', 0):.1f}p</div>
        <div class="label">Lowest rate seen</div>
      </div>
    </div>
  </div>

  <!-- CHARTS ROW 1 -->
  <div class="grid-2" style="margin-top:20px">
    <div class="card">
      <h2>Monthly Consumption (kWh)</h2>
      <div class="chart-container">
        <canvas id="monthlyChart"></canvas>
      </div>
    </div>
    <div class="card">
      <h2>Monthly Cost (£)</h2>
      <div class="chart-container">
        <canvas id="monthlyCostChart"></canvas>
      </div>
    </div>
  </div>

  <!-- RATE DISTRIBUTION -->
  <div style="margin-top:20px" class="card">
    <h2>⚡ Agile Rate Distribution — Usage by Rate Band</h2>
    <div class="chart-container" style="height:220px">
      <canvas id="rateDistChart"></canvas>
    </div>
    <div style="margin-top:14px; display:flex; gap:20px; font-size:0.85rem; flex-wrap:wrap">
      <span>⚡ <strong style="color:#00cc66">{us.get('negative_kwh',0):,.1f} kWh</strong> consumed at &lt;0p — Octopus <strong style="color:#00aa44">credited you £{us.get('negative_earnings_gbp',0):.2f}</strong> for this</span>
      <span>🟢 <strong>{us.get('cheap_kwh',0):,.0f} kWh</strong> consumed at 0–10p (cheap)</span>
      <span>🟡 <strong>{us.get('medium_kwh',0):,.0f} kWh</strong> consumed at 10–20p (medium)</span>
      <span>🔴 <strong>{us.get('expensive_kwh',0):,.0f} kWh</strong> consumed at &gt;20p (expensive)</span>
    </div>
  </div>

  <!-- CONSUMPTION BY PRICE BAND TABLE -->
  <div class="card" style="margin-top:20px">
    <h2>📋 Consumption Breakdown by Price Band</h2>
    <div style="overflow-x:auto">
    <table>
      <thead>
        <tr>
          <th>Price Band</th>
          <th>kWh Consumed</th>
          <th>% of Total</th>
          <th>Est. Cost</th>
          <th>Category</th>
        </tr>
      </thead>
      <tbody>
        {band_table_rows}
      </tbody>
    </table>
    </div>
    <p style="font-size:0.8rem;color:var(--oct-muted);margin-top:10px">
      Est. cost uses the midpoint of each 5p band. Slight variance from reported total due to midpoint estimation. Negative cost = Octopus credits you.
    </p>
  </div>

  <!-- BATTERY RECOMMENDATION -->
  <p class="section-title">🔋 Battery Recommendation</p>
  {rec_note}

  <div class="card" style="margin-top:16px">
    <h2>Battery Comparison — Annual Savings & Cost of Ownership (15 years)</h2>
    <div style="overflow-x:auto">
    <table>
      <thead>
        <tr>
          <th rowspan="2">Battery</th>
          <th rowspan="2">Capacity</th>
          <th rowspan="2">Installed Cost</th>
          <th colspan="2" style="text-align:center;background:#555">Default strategy<br><small style="font-weight:normal">charge≤{CHARGE_BELOW_P_PER_KWH}p / discharge≥{DISCHARGE_ABOVE_P_PER_KWH}p</small></th>
          <th colspan="3" style="text-align:center;background:#0077aa">Optimised strategy 🎯</th>
          <th rowspan="2">Saving uplift</th>
          <th rowspan="2">15yr Net Profit</th>
          <th rowspan="2">IRR</th>
        </tr>
        <tr>
          <th style="background:#666">Year-1 saving</th><th style="background:#666">Payback</th>
          <th style="background:#0088bb">Best thresholds</th>
          <th style="background:#0088bb">Year-1 saving</th>
          <th style="background:#0088bb">Payback</th>
        </tr>
      </thead>
      <tbody>
        {battery_rows}
      </tbody>
    </table>
    </div>
  </div>

  <!-- THRESHOLD OPTIMISATION HEATMAP -->
  <div class="card" style="margin-top:20px">
    <h2>🎯 Threshold Optimisation — Annual Saving by Charge / Discharge Price ({best['battery']['name']})</h2>
    <p style="font-size:0.85rem;color:#555;margin-bottom:14px">
      Each cell shows the modelled annual saving (£) for a given pair of thresholds.
      <span style="background:#00cc66;color:white;padding:1px 6px;border-radius:3px">Darker green</span> = higher saving.
      Grey cells are invalid (charge price ≥ discharge price).
      The <strong>⭐</strong> marks the optimal combination from the grid search.
    </p>
    {_build_heatmap_table(battery_results)}
  </div>

  <!-- TCO CHART -->
  <div class="grid-2" style="margin-top:20px">
    <div class="card">
      <h2>📈 Cumulative Savings vs Investment — {best['battery']['name']}</h2>
      <div class="chart-container">
        <canvas id="tcoChart"></canvas>
      </div>
    </div>
    <div class="card">
      <h2>💡 Strategy: How Your Battery Would Work on Agile</h2>
      <div class="tip">
        <strong>Charge periods:</strong> When Agile rate is below <strong>{CHARGE_BELOW_P_PER_KWH}p/kWh</strong>
        (typically midnight–6am, sometimes negative). Your battery fills up at low cost.
      </div>
      <div class="tip">
        <strong>Discharge periods:</strong> When Agile rate is above <strong>{DISCHARGE_ABOVE_P_PER_KWH}p/kWh</strong>
        (typically 4–7pm peak). The battery powers your home instead of the grid.
      </div>
      <div class="tip" style="background:#e8faf0;border-left:3px solid #00cc66">
        <strong style="color:#00aa44">💰 Negative rates:</strong> Over this period you were
        <strong>credited £{us.get('negative_earnings_gbp', 0):.2f}</strong> across
        <strong>{us.get('negative_rate_hours', 0):.0f} hours</strong> of sub-zero pricing
        ({us.get('negative_kwh', 0):.1f} kWh consumed).
        A battery maximises this — charge to full at the start of every negative window
        and you capture the full credit even if your home load is small.
      </div>
      <div class="tip">
        <strong>Smart charging tip:</strong> Pair with an Octopus-compatible inverter (GivEnergy, SunSynk,
        Solax) to automate charging based on next-day Agile prices via the API.
      </div>
    </div>
  </div>

  <!-- BEST BATTERY YEAR BY YEAR -->
  <div class="card" style="margin-top:20px">
    <h2>📅 Year-by-Year Projection — {best['battery']['name']}</h2>
    <div style="overflow-x:auto">
    <table>
      <thead>
        <tr><th>Year</th><th>Capacity Factor</th><th>Annual Saving</th>
            <th>Cumulative Saving</th><th>Net Position</th><th>Warranty</th></tr>
      </thead>
      <tbody>
"""
    for yr in best["tco"]["year_by_year"]:
        net = yr["net_position_gbp"]
        net_class = "badge-green" if net >= 0 else "badge-red"
        net_sign = "+" if net >= 0 else ""
        warranty_badge = '<span class="badge badge-green">In warranty</span>' if yr["in_warranty"] else '<span class="badge badge-amber">Post-warranty</span>'
        html += f"""
        <tr>
          <td>Year {yr['year']}</td>
          <td>{yr['capacity_factor']*100:.0f}%</td>
          <td>£{yr['annual_saving_gbp']:,.0f}</td>
          <td>£{yr['cumulative_saving_gbp']:,.0f}</td>
          <td><span class="badge {net_class}">{net_sign}£{net:,.0f}</span></td>
          <td>{warranty_badge}</td>
        </tr>"""

    html += f"""
      </tbody>
    </table>
    </div>
  </div>

  <!-- DOUBLED USAGE SCENARIO -->
  <p class="section-title">📈 What If Your Usage Doubled?</p>
  <div class="card">
    <p style="font-size:0.9rem;color:#555;margin-bottom:16px">
      Adding an <strong>electric vehicle</strong>, <strong>heat pump</strong>, or other high-draw appliance
      could double your annual consumption from
      <strong>{usage_summary.get('total_kwh',0):,.0f} kWh</strong> to
      <strong>~{usage_summary.get('total_kwh',0)*2:,.0f} kWh/yr</strong>
      (est. annual bill <strong>£{(doubled_summary or usage_summary).get('total_cost_gbp',0):,.0f}</strong>
      vs current £{usage_summary.get('total_cost_gbp',0):,.0f}).
      Higher usage means more kWh shifted to cheap/negative slots — batteries pay back faster.
    </p>
    <div style="overflow-x:auto">
    <table>
      <thead>
        <tr>
          <th>Battery</th>
          <th colspan="2" style="text-align:center;background:#555">Current usage ({usage_summary.get('daily_avg_kwh',0):.1f} kWh/day)</th>
          <th colspan="2" style="text-align:center;background:#0077cc">If usage doubles ({usage_summary.get('daily_avg_kwh',0)*2:.1f} kWh/day)</th>
          <th>Payback improvement</th>
        </tr>
        <tr>
          <th></th>
          <th style="background:#666">Year-1 saving</th><th style="background:#666">Payback</th>
          <th style="background:#0088dd">Year-1 saving</th><th style="background:#0088dd">Payback</th>
          <th></th>
        </tr>
      </thead>
      <tbody>
        {_build_2x_rows(battery_results, battery_results_2x or [])}
      </tbody>
    </table>
    </div>
    <p style="font-size:0.8rem;color:#888;margin-top:12px">
      ⚡ Battery capacity stays the same — the extra saving comes from having more high-rate consumption
      to displace and more charge cycles needed, without needing a bigger battery.
      Note: very high usage may require two daily charge/discharge cycles; contact your installer.
    </p>
  </div>

  <!-- SOLAR SCENARIO -->"""

    # ── Build solar section HTML ──────────────────────────────────────────
    # Initialise chart data variables (populated inside the if block when solar data available)
    solar_monthly_labels = ["Jan","Feb","Mar","Apr","May","Jun",
                            "Jul","Aug","Sep","Oct","Nov","Dec"]
    solar_monthly_kwh = []   # filled below if solar data exists

    if solar_results and solar_battery_results:
        # Solar-only table rows
        solar_rows_html = ""
        for sr in solar_results:
            tco = sr["tco"]
            pb = tco.get("payback_years")
            pb_str = f"{pb:.1f} yrs" if pb else "Never"
            net = tco.get("net_profit_15yr", 0)
            net_col = "#00aa44" if net > 0 else "#cc2200"
            net_sign = "+" if net > 0 else ""
            solar_rows_html += f"""
        <tr>
          <td><strong>{sr['solar_kwp']:.0f} kWp</strong></td>
          <td>£{sr['install_cost']:,}</td>
          <td>{sr['annual_gen_kwh']:,.0f} kWh</td>
          <td>{sr['annual_self_use_kwh']:,.0f} kWh ({sr['self_consumption_pct']:.0f}%)</td>
          <td>{sr['annual_export_kwh']:,.0f} kWh</td>
          <td>£{sr['annual_import_saving_gbp']:,.0f}</td>
          <td>£{sr['annual_export_income_gbp']:,.0f}</td>
          <td style="font-weight:700;color:#0077cc">£{sr['annual_total_benefit_gbp']:,.0f}/yr</td>
          <td>{pb_str}</td>
          <td style="color:{net_col}">{net_sign}£{net:,.0f}</td>
        </tr>"""

        # Solar + battery table rows
        solar_batt_rows_html = ""
        for sbr in solar_battery_results:
            tco = sbr["tco"]
            pb = tco.get("payback_years")
            pb_str = f"{pb:.1f} yrs" if pb else "Never"
            net = tco.get("net_profit_15yr", 0)
            net_col = "#00aa44" if net > 0 else "#cc2200"
            net_sign = "+" if net > 0 else ""
            solar_batt_rows_html += f"""
        <tr>
          <td><strong>{sbr['solar_kwp']:.0f} kWp</strong></td>
          <td>£{sbr['solar_cost']:,}</td>
          <td>£{sbr['battery_cost']:,}</td>
          <td>£{sbr['combined_cost']:,}</td>
          <td>{sbr['annual_gen_kwh']:,.0f} kWh</td>
          <td>{sbr['self_consumption_pct']:.0f}%</td>
          <td>£{sbr['annual_import_saving_gbp']:,.0f}</td>
          <td>£{sbr['annual_export_income_gbp']:,.0f}</td>
          <td>£{sbr['annual_battery_saving_gbp']:,.0f}</td>
          <td style="font-weight:700;color:#ff6600">£{sbr['annual_total_benefit_gbp']:,.0f}/yr</td>
          <td>{pb_str}</td>
          <td style="color:{net_col}">{net_sign}£{net:,.0f}</td>
        </tr>"""

        # Three-way comparison: battery-only best vs solar-only best vs combo best
        batt_b = min(battery_results, key=lambda x: x["tco"]["payback_years"] or 999)
        solar_b = min(solar_results, key=lambda x: x["tco"]["payback_years"] or 999)
        combo_b = min(solar_battery_results, key=lambda x: x["tco"]["payback_years"] or 999)
        batt_pb = batt_b["tco"].get("payback_years")
        solar_pb = solar_b["tco"].get("payback_years")
        combo_pb = combo_b["tco"].get("payback_years")

        # Pick the winner (shortest payback)
        options = [(batt_pb or 999, "🔋 Battery"), (solar_pb or 999, "☀️ Solar"), (combo_pb or 999, "☀️🔋 Combo")]
        winner_label = min(options, key=lambda x: x[0])[1]
        winner_note = (
            f"<p style='margin-top:14px;padding:12px 16px;background:#eef8e8;border-radius:8px;"
            f"border-left:4px solid #00aa44;font-size:0.9rem'>"
            f"<strong>🏆 Best option for you: {winner_label}</strong> — shortest payback period "
            f"of the three. All figures use your actual Agile rate history and optimised "
            f"charge/discharge thresholds.</p>"
        )

        comparison_rows_html = f"""
        <tr>
          <td><strong>🔋 Battery only</strong><br>
              <small style="color:#777">{batt_b['battery']['name']}</small></td>
          <td>£{batt_b['battery']['installed_cost_gbp']:,}</td>
          <td>£{batt_b['annual_saving_gbp']:,.0f}/yr</td>
          <td>{'Never' if not batt_pb else f'{batt_pb:.1f} yrs'}</td>
          <td>£{batt_b['tco']['net_profit_15yr']:,.0f}</td>
          <td>{batt_b['tco']['irr_pct']:.1f}%</td>
        </tr>
        <tr>
          <td><strong>☀️ Solar only</strong><br>
              <small style="color:#777">{solar_b['solar_kwp']:.0f} kWp</small></td>
          <td>£{solar_b['install_cost']:,}</td>
          <td>£{solar_b['annual_total_benefit_gbp']:,.0f}/yr</td>
          <td>{'Never' if not solar_pb else f'{solar_pb:.1f} yrs'}</td>
          <td>£{solar_b['tco']['net_profit_15yr']:,.0f}</td>
          <td>{solar_b['tco']['irr_pct']:.1f}%</td>
        </tr>
        <tr style="background:#fff7ee">
          <td><strong>☀️🔋 Solar + Battery</strong><br>
              <small style="color:#777">{combo_b['solar_kwp']:.0f} kWp + {combo_b['battery_name']}</small></td>
          <td>£{combo_b['combined_cost']:,}</td>
          <td>£{combo_b['annual_total_benefit_gbp']:,.0f}/yr</td>
          <td>{'Never' if not combo_pb else f'{combo_pb:.1f} yrs'}</td>
          <td>£{combo_b['tco']['net_profit_15yr']:,.0f}</td>
          <td>{combo_b['tco']['irr_pct']:.1f}%</td>
        </tr>"""

        # Monthly solar generation bar chart data (for 4kWp illustrative)
        days_in_month = {1:31,2:28,3:31,4:30,5:31,6:30,7:31,8:31,9:30,10:31,11:30,12:31}
        solar_monthly_labels = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]
        solar_monthly_kwh = [
            round(UK_SOLAR_MONTHLY_KWH_PER_KWP[m] * 4.0 * days_in_month[m], 1)
            for m in range(1, 13)
        ]

        html += f"""
  <p class="section-title">☀️ Would Solar Help?</p>
  <div class="card">
    <p style="font-size:0.9rem;color:#555;margin-bottom:16px">
      Based on your usage profile, here is how solar panels would interact with your Agile Octopus
      tariff. Solar generation offsets grid imports at your current Agile rate (avoiding expensive
      peak pricing), and any surplus is exported via the
      <strong>Smart Export Guarantee (SEG)</strong> at ~{SEG_EXPORT_RATE_P:.0f}p/kWh.
      Costs are UK 2026 installed estimates (~£{SOLAR_INSTALL_COST_PER_KWP:,}/kWp all-in).
      No solar currently on your account — these are projections.
    </p>
    <div class="grid-2">
      <div>
        <h2>Indicative Monthly Generation — 4 kWp System</h2>
        <div class="chart-container" style="height:220px">
          <canvas id="solarMonthlyChart"></canvas>
        </div>
      </div>
      <div>
        <h2>How Agile + Solar Works Together</h2>
        <div class="tip" style="background:#fffbe8;border-left:3px solid #ffaa00">
          <strong style="color:#cc7700">☀️ Daytime (solar generating):</strong>
          Solar meets your load first, cutting Agile import costs.
          Excess charges the battery (if fitted) or exports at SEG rate.
        </div>
        <div class="tip" style="background:#e8faf0;border-left:3px solid #00cc66">
          <strong style="color:#009944">🌙 Evening peak (no solar):</strong>
          Battery (if fitted) discharges to cover peak-rate consumption,
          just as it would without solar — but it may already be full from
          daytime charging, meaning zero cheap-rate grid charging is needed.
        </div>
        <div class="tip">
          <strong>Self-consumption:</strong> Without a battery, ~{solar_results[1]['self_consumption_pct']:.0f}%
          of solar is used in your home. With a battery this rises significantly — the battery
          soaks up daytime surplus for use in the evening.
        </div>
      </div>
    </div>

    <h2 style="margin-top:20px">Solar Only — by System Size</h2>
    <div style="overflow-x:auto">
    <table>
      <thead>
        <tr>
          <th>System</th><th>Install Cost</th><th>Annual Generation</th>
          <th>Self-consumed</th><th>Exported to grid</th>
          <th>Import saving/yr</th><th>SEG income/yr</th>
          <th>Total benefit/yr</th><th>Payback</th><th>15yr Net Profit</th>
        </tr>
      </thead>
      <tbody>{solar_rows_html}</tbody>
    </table>
    </div>
    <p style="font-size:0.8rem;color:#888;margin-top:8px">
      Self-consumed % = proportion of generated solar energy used directly in your home.
      Export income uses the SEG rate of {SEG_EXPORT_RATE_P:.0f}p/kWh (typical 2026).
    </p>
  </div>

  <div class="card" style="margin-top:20px">
    <h2>Solar + Battery Combined ({batt_b['battery']['name']})</h2>
    <p style="font-size:0.87rem;color:#555;margin-bottom:12px">
      Adding a battery alongside solar maximises self-consumption: daytime surplus charges
      the battery rather than exporting at SEG rates, and the battery discharges during
      the evening Agile peak — capturing the full value of both technologies simultaneously.
    </p>
    <div style="overflow-x:auto">
    <table>
      <thead>
        <tr>
          <th>Solar</th><th>Solar cost</th><th>Battery cost</th><th>Total cost</th>
          <th>Annual generation</th><th>Self-use %</th>
          <th>Import saving/yr</th><th>SEG income/yr</th><th>Battery saving/yr</th>
          <th>Total benefit/yr</th><th>Payback</th><th>15yr Net Profit</th>
        </tr>
      </thead>
      <tbody>{solar_batt_rows_html}</tbody>
    </table>
    </div>
  </div>

  <div class="card" style="margin-top:20px">
    <h2>⚖️ Which Combination Is Best for You?</h2>
    <div style="overflow-x:auto">
    <table>
      <thead>
        <tr>
          <th>Scenario</th><th>Total Investment</th><th>Annual Benefit</th>
          <th>Payback</th><th>15yr Net Profit</th><th>IRR</th>
        </tr>
      </thead>
      <tbody>{comparison_rows_html}</tbody>
    </table>
    </div>
    {winner_note}
  </div>
"""
    else:
        html += "\n"

    html += f"""
  <!-- ASSUMPTIONS -->
  <div class="card" style="margin-top:20px">
    <h2>⚙️ Assumptions & Methodology</h2>
    <div style="display:grid; grid-template-columns:1fr 1fr; gap:20px; font-size:0.87rem">
      <div>
        <p><strong>Charging strategy:</strong> Charge below {CHARGE_BELOW_P_PER_KWH}p/kWh, discharge above {DISCHARGE_ABOVE_P_PER_KWH}p/kWh.</p>
        <p style="margin-top:8px"><strong>Charge rate:</strong> C/4 (25% of usable capacity per half-hour slot).</p>
        <p style="margin-top:8px"><strong>Standing charge:</strong> Not included (unchanged by battery).</p>
        <p style="margin-top:8px"><strong>Tariff changes:</strong> Assumes current Agile spread persists; actual rates vary daily.</p>
      </div>
      <div>
        <p><strong>Degradation:</strong> Applied per manufacturer spec, floored at 70% of original capacity.</p>
        <p style="margin-top:8px"><strong>IRR:</strong> Internal rate of return over 15-year analysis period.</p>
        <p style="margin-top:8px"><strong>Installed costs:</strong> UK market estimates (March 2026). Get 3 quotes for accuracy.</p>
        <p style="margin-top:8px"><strong>Solar modelling:</strong> UK monthly yield averages (south-facing, ~35° pitch). SEG export at {SEG_EXPORT_RATE_P:.0f}p/kWh. Actual yield varies by orientation, shading, and location.</p>
      </div>
    </div>
  </div>

  <p class="footnote">
    This report is generated from your Octopus Energy smart meter data via the public API.
    Installed battery and solar costs are market estimates (UK 2026); always obtain multiple quotes from
    MCS-certified installers. Solar yield uses UK monthly average irradiance data for a south-facing system.
    SEG (Smart Export Guarantee) export income modelled at {SEG_EXPORT_RATE_P:.0f}p/kWh — actual SEG rates
    vary by supplier. Standing charges, VAT changes, and future tariff shifts are not modelled.
    Report generated {report_date}.
  </p>

</div>

<script>
// Monthly consumption chart
new Chart(document.getElementById('monthlyChart'), {{
  type: 'bar',
  data: {{
    labels: {json.dumps(months_sorted)},
    datasets: [{{ label: 'kWh', data: {json.dumps(monthly_kwh_data)},
      backgroundColor: 'rgba(232,76,140,0.7)', borderColor: '#e84c8c',
      borderWidth: 1, borderRadius: 4 }}]
  }},
  options: {{ responsive: true, maintainAspectRatio: false,
    plugins: {{ legend: {{ display: false }} }},
    scales: {{ y: {{ beginAtZero: true }} }} }}
}});

// Monthly cost chart
new Chart(document.getElementById('monthlyCostChart'), {{
  type: 'bar',
  data: {{
    labels: {json.dumps(months_sorted)},
    datasets: [{{ label: '£', data: {json.dumps(monthly_cost_data)},
      backgroundColor: 'rgba(0,153,255,0.7)', borderColor: '#0099ff',
      borderWidth: 1, borderRadius: 4 }}]
  }},
  options: {{ responsive: true, maintainAspectRatio: false,
    plugins: {{ legend: {{ display: false }} }},
    scales: {{ y: {{ beginAtZero: true }} }} }}
}});

// Rate distribution chart
const rateColors = {json.dumps(rate_labels)}.map(r =>
  r < 0 ? 'rgba(0,200,100,0.8)' : r < 10 ? 'rgba(0,200,100,0.6)' :
  r < 20 ? 'rgba(255,170,0,0.7)' : 'rgba(232,76,140,0.7)');
new Chart(document.getElementById('rateDistChart'), {{
  type: 'bar',
  data: {{
    labels: {json.dumps(rate_labels_str)},
    datasets: [{{ label: 'kWh consumed', data: {json.dumps(rate_values)},
      backgroundColor: rateColors, borderRadius: 3 }}]
  }},
  options: {{ responsive: true, maintainAspectRatio: false,
    plugins: {{ legend: {{ display: false }} }},
    scales: {{ y: {{ beginAtZero: true }} }} }}
}});

// TCO chart
new Chart(document.getElementById('tcoChart'), {{
  type: 'line',
  data: {{
    labels: {json.dumps(tco_years)},
    datasets: [
      {{ label: 'Cumulative savings (£)', data: {json.dumps(tco_cum_saving)},
        borderColor: '#00cc66', backgroundColor: 'rgba(0,204,102,0.1)',
        fill: true, tension: 0.3, pointRadius: 3 }},
      {{ label: 'Battery cost (£)', data: {json.dumps(tco_cost_line)},
        borderColor: '#e84c8c', borderDash: [6,3], pointRadius: 0, tension: 0 }}
    ]
  }},
  options: {{ responsive: true, maintainAspectRatio: false,
    scales: {{ y: {{ beginAtZero: true }} }} }}
}});

// Solar monthly generation chart (4 kWp indicative)
if (document.getElementById('solarMonthlyChart')) {{
  new Chart(document.getElementById('solarMonthlyChart'), {{
    type: 'bar',
    data: {{
      labels: {json.dumps(solar_monthly_labels if solar_results else [])},
      datasets: [{{
        label: 'Generation (kWh)',
        data: {json.dumps(solar_monthly_kwh if solar_results else [])},
        backgroundColor: ['rgba(255,180,0,0.4)','rgba(255,180,0,0.4)',
          'rgba(255,200,0,0.6)','rgba(255,210,0,0.7)','rgba(255,220,0,0.85)',
          'rgba(255,230,0,0.95)','rgba(255,220,0,0.9)','rgba(255,210,0,0.8)',
          'rgba(255,190,0,0.7)','rgba(255,170,0,0.55)','rgba(255,160,0,0.4)',
          'rgba(255,150,0,0.35)'],
        borderColor: '#ffaa00', borderWidth: 1, borderRadius: 4
      }}]
    }},
    options: {{
      responsive: true, maintainAspectRatio: false,
      plugins: {{ legend: {{ display: false }},
        tooltip: {{ callbacks: {{ label: ctx => ctx.parsed.y + ' kWh' }} }} }},
      scales: {{ y: {{ beginAtZero: true,
        title: {{ display: true, text: 'kWh/month' }} }} }}
    }}
  }});
}}
</script>

</body>
</html>"""
    return html


# ─────────────────────────────────────────────
#  SYNTHETIC DATA FALLBACKS
# ─────────────────────────────────────────────

# UK half-hourly load profile (48 slots, 0 = midnight).
# Values are fractions of daily demand; sum ≈ 48 (so mean ≈ 1.0).
_UK_LOAD_PROFILE = [
    0.40, 0.35, 0.32, 0.30, 0.30, 0.32,  # 00:00–02:30
    0.35, 0.45, 0.65, 0.90, 1.05, 1.10,  # 03:00–05:30
    1.15, 1.20, 1.25, 1.30, 1.35, 1.40,  # 06:00–08:30
    1.30, 1.20, 1.10, 1.05, 1.00, 0.95,  # 09:00–11:30
    0.95, 0.95, 0.95, 1.00, 1.00, 1.05,  # 12:00–14:30
    1.10, 1.15, 1.20, 1.30, 1.50, 1.80,  # 15:00–17:30
    2.00, 1.90, 1.70, 1.50, 1.30, 1.15,  # 18:00–20:30
    1.00, 0.85, 0.70, 0.60, 0.50, 0.42,  # 21:00–23:30
]
_PROFILE_SUM = sum(_UK_LOAD_PROFILE)

# UK-typical Agile rate profile (pence/kWh) per half-hour slot
_UK_AGILE_PROFILE = [
    5.0, 4.0, 3.0, 2.0, 1.5, 1.0,   # 00:00–02:30  cheap overnight
    0.5, 1.0, 3.0, 8.0, 12.0, 14.0, # 03:00–05:30
   16.0,18.0,20.0,22.0,22.0,21.0,   # 06:00–08:30  morning peak
   18.0,16.0,15.0,14.0,13.0,13.0,   # 09:00–11:30
   13.0,13.0,14.0,15.0,16.0,18.0,   # 12:00–14:30
   20.0,22.0,25.0,28.0,32.0,35.0,   # 15:00–17:30  evening peak
   30.0,25.0,22.0,18.0,15.0,12.0,   # 18:00–20:30
    9.0, 7.0, 6.0, 6.0, 5.5, 5.0,   # 21:00–23:30
]


def make_synthetic_consumption(date_from, date_to, annual_kwh=3100.0):
    """
    Build synthetic half-hourly consumption records using a UK load profile.
    Returns a list in the same format as the Octopus REST API.
    """
    daily_kwh = annual_kwh / 365.0
    results = []
    day = date_from.replace(hour=0, minute=0, second=0, microsecond=0)
    while day < date_to:
        for slot in range(48):
            start = day + timedelta(minutes=slot * 30)
            end = start + timedelta(minutes=30)
            kwh = daily_kwh * (_UK_LOAD_PROFILE[slot] / _PROFILE_SUM)
            results.append({
                "interval_start": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "interval_end": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "consumption": round(kwh, 4),
            })
        day += timedelta(days=1)
    return results


def make_synthetic_agile_rates(date_from, date_to):
    """
    Build synthetic half-hourly Agile rate records using a representative UK profile.
    Returns a list in the same format as the Octopus products API.
    """
    import random
    random.seed(42)
    results = []
    day = date_from.replace(hour=0, minute=0, second=0, microsecond=0)
    while day < date_to:
        # Add a small daily random offset to simulate Agile volatility
        daily_offset = random.gauss(0, 2.0)
        for slot in range(48):
            start = day + timedelta(minutes=slot * 30)
            end = start + timedelta(minutes=30)
            rate = max(-5.0, _UK_AGILE_PROFILE[slot] + daily_offset + random.gauss(0, 1.0))
            results.append({
                "valid_from": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "valid_to": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "value_inc_vat": round(rate, 4),
            })
        day += timedelta(days=1)
    return results


# ─────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────

def main():
    global API_KEY

    parser = argparse.ArgumentParser(description="Octopus Energy Battery Analysis Tool")
    parser.add_argument(
        "--api-key",
        default=os.environ.get("OCTOPUS_API_KEY"),
        help="Octopus Energy API key (or set OCTOPUS_API_KEY env var)",
    )
    parser.add_argument(
        "--csv",
        metavar="FILE",
        nargs="?",
        const="agile_data.csv",
        default=None,
        help="Dump Agile pricing and usage data to a CSV file "
             "(default filename: agile_data.csv)",
    )
    args = parser.parse_args()

    if not args.api_key:
        parser.error(
            "API key required. Pass --api-key sk_live_... or set the OCTOPUS_API_KEY environment variable."
        )
    API_KEY = args.api_key

    print("=" * 60)
    print("  Octopus Energy Battery Analysis Tool")
    print("=" * 60)

    # 1. Authenticate
    print("\n[1/6] Authenticating with Octopus Kraken API...")
    try:
        token = get_kraken_token()
        print("  ✓ Token obtained.")
    except Exception as e:
        print(f"  ✗ Failed to obtain token: {e}")
        sys.exit(1)

    # 2. Get account info — two steps: account number via GraphQL, then meter details via REST
    print("\n[2/6] Fetching account & meter details...")
    try:
        # Step 2a: minimal GraphQL call just for account number
        account_number = get_account_number(token)
        print(f"  ✓ Account number: {account_number}")

        # Step 2b: REST call for meter/tariff details
        mpan, serial, tariff_code = get_account_details_rest(account_number)
        print(f"  ✓ MPAN: {mpan}")
        print(f"  ✓ Meter serial: {serial}")
        print(f"  ✓ Tariff code: {tariff_code}")
    except Exception as e:
        print(f"  ✗ Failed to get account info: {e}")
        import traceback; traceback.print_exc()
        sys.exit(1)

    # 3. Fetch consumption data
    print("\n[3/6] Downloading half-hourly consumption data...")
    now = datetime.now(timezone.utc)
    using_synthetic = False
    consumption = []

    # ── Probe: confirm data exists and find the latest available timestamp ──
    print("  → Probing meter for available data (no date filter) …")
    try:
        total_records, latest_ts, oldest_on_page = probe_consumption(mpan, serial)
        if total_records:
            print(f"  ✓ Probe: {total_records:,} records exist on this meter.")
            print(f"           Latest available : {latest_ts}")
            print(f"           Oldest (page 1)  : {oldest_on_page}")
        else:
            print("  ⚠  Probe returned 0 records — no consumption data accessible via API.")
            total_records = 0
            latest_ts = None
    except Exception as e:
        print(f"  ⚠  Probe failed ({e}). Will try date-filtered requests anyway.")
        total_records = 1   # optimistic — attempt the full fetch
        latest_ts = None

    if total_records == 0:
        # Truly no data — fall back to synthetic immediately
        consumption = []
    else:
        # ── Set period_to: SMETS1 data is only indexed up to ~48 hrs ago ──
        # Use the latest available timestamp from the probe if we have it;
        # otherwise cap at (now - 48 hours) as a safe default.
        if latest_ts:
            try:
                # Parse the timestamp; strip trailing Z and add UTC
                ts_clean = latest_ts.rstrip("Z").split("+")[0]
                period_to = datetime.fromisoformat(ts_clean).replace(tzinfo=timezone.utc)
                # Subtract 30 min so the boundary record is included
                period_to = period_to + timedelta(minutes=30)
            except Exception:
                period_to = now - timedelta(hours=48)
        else:
            period_to = now - timedelta(hours=48)

        print(f"  → Using period_to = {fmt_dt(period_to)} "
              f"(latest indexed data, avoids SMETS1 lag)")

        for days_back in [365, 180, 90, 30]:
            date_from = period_to - timedelta(days=days_back)
            try:
                print(f"  → Fetching last {days_back} days "
                      f"({fmt_dt(date_from)} → {fmt_dt(period_to)}) …")
                consumption = get_consumption(mpan, serial, date_from, period_to)
                if consumption:
                    print(f"  ✓ Retrieved {len(consumption):,} intervals "
                          f"≈ {len(consumption)/48:.0f} days of data.")
                    date_from = period_to - timedelta(days=days_back)
                    break
                else:
                    print(f"  ⚠  0 intervals returned for {days_back}-day window.")
            except Exception as e:
                print(f"  ⚠  Request failed ({e}).")

    if not consumption:
        print("\n  ⚠  No smart-meter consumption data returned by API.")
        print("     Falling back to a UK-typical half-hourly load profile (3,100 kWh/yr).")
        print("     Results are indicative — re-run once real data is accessible.")
        consumption = make_synthetic_consumption(now - timedelta(days=365), now)
        using_synthetic = True
        date_from = now - timedelta(days=365)
        period_to = now

    # 4. Fetch Agile rates (always use the same window as the consumption data)
    print("\n[4/6] Fetching Agile tariff rates...")
    rates = []
    try:
        rates = get_agile_rates(tariff_code, date_from, period_to)
        print(f"  ✓ Retrieved {len(rates):,} rate periods.")
        if rates:
            print(f"  ✓ Sample rate record: {rates[0]}")
    except Exception as e:
        print(f"  ⚠  Could not fetch rates ({e}). Will use estimated rates.")

    # If we have no rates, generate synthetic Agile-like rates for the period
    if not rates:
        print("  → Generating synthetic Agile rate profile for modelling purposes.")
        rates = make_synthetic_agile_rates(date_from, period_to)

    # 5. Analyse usage
    print("\n[5/6] Analysing usage patterns...")
    rate_map = build_rate_map(rates)
    intervals = calculate_actual_costs(consumption, rate_map)
    usage_summary = summarise_usage(intervals)

    if not usage_summary:
        print("  ✗ Could not build usage summary — no intervals to analyse.")
        sys.exit(1)

    syn_note = " (synthetic profile)" if using_synthetic else ""
    print(f"  Days analysed:      {usage_summary['days']}{syn_note}")
    print(f"  Total consumption:  {usage_summary['total_kwh']:,.1f} kWh")
    print(f"  Total cost:         £{usage_summary['total_cost_gbp']:,.2f}")
    print(f"  Daily avg:          {usage_summary['daily_avg_kwh']:.2f} kWh  /  "
          f"£{usage_summary['daily_avg_cost_gbp']:.2f}")
    print(f"  Average rate:       {usage_summary['avg_rate_p']:.2f}p/kWh")
    print(f"  Negative rate hrs:  {usage_summary['negative_rate_hours']:.0f} hours")
    print(f"  Negative rate kWh:  {usage_summary['negative_rate_kwh']:.1f} kWh")
    print(f"  Earned at neg rates:£{usage_summary['negative_earnings_gbp']:.2f}")

    # Optional CSV export
    if args.csv:
        csv_path = args.csv
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow([
                "period_utc", "consumption_kwh", "agile_rate_p_per_kwh",
                "cost_p", "cost_gbp",
            ])
            for iv in intervals:
                writer.writerow([
                    iv["period"],
                    round(iv["kwh"], 6),
                    round(iv["rate_p"], 4),
                    round(iv["cost_p"], 4),
                    round(iv["cost_p"] / 100, 6),
                ])
        print(f"\n  ✓ CSV saved to: {csv_path} ({len(intervals):,} rows)")

    # 6. Model battery savings — current usage AND doubled-usage scenario
    print("\n[6/6] Modelling battery savings & calculating TCO...")

    # Doubled-usage intervals: same rates/timestamps, consumption × 2
    doubled_intervals = [
        {**iv, "kwh": iv["kwh"] * 2, "cost_p": iv["cost_p"] * 2}
        for iv in intervals
    ]
    doubled_summary = summarise_usage(doubled_intervals)

    def model_one_battery(ivs, battery, charge_p, discharge_p):
        saving_pence, charge_kwh, discharge_kwh, days_sim = model_battery_savings(
            ivs, battery["usable_kwh"], battery["efficiency"], charge_p, discharge_p
        )
        annual_saving_gbp = saving_pence / 100
        tco = calculate_tco(battery, annual_saving_gbp, analysis_years=15)
        return annual_saving_gbp, charge_kwh, discharge_kwh, tco

    def model_all_batteries(ivs, label, run_optimiser=False):
        results = []
        for battery in BATTERY_MODELS:
            # Default thresholds
            s_gbp, chg, dis, tco = model_one_battery(
                ivs, battery, CHARGE_BELOW_P_PER_KWH, DISCHARGE_ABOVE_P_PER_KWH
            )
            pb = tco["payback_years"]
            pb_str = f"{pb:.1f} yrs" if pb else "Never"
            print(f"  [{label}] {battery['name']:40s}  "
                  f"saving £{s_gbp:,.0f}/yr  payback {pb_str}", end="")

            opt_charge = CHARGE_BELOW_P_PER_KWH
            opt_discharge = DISCHARGE_ABOVE_P_PER_KWH
            opt_saving = s_gbp
            opt_tco = tco
            grid = {}

            if run_optimiser:
                print("  → optimising…", end="", flush=True)
                opt_charge, opt_discharge, opt_gbp, grid = optimise_thresholds(ivs, battery)
                if opt_gbp > s_gbp:
                    opt_saving = opt_gbp
                    _, _, _, opt_tco = model_one_battery(ivs, battery, opt_charge, opt_discharge)
                    opt_pb = opt_tco["payback_years"]
                    opt_pb_str = f"{opt_pb:.1f} yrs" if opt_pb else "Never"
                    print(f"  optimal: charge≤{opt_charge}p / discharge≥{opt_discharge}p  "
                          f"saving £{opt_gbp:,.0f}/yr  payback {opt_pb_str}")
                else:
                    print("  (defaults already optimal)")
            else:
                print()

            results.append({
                "battery": battery,
                "annual_saving_gbp": s_gbp,
                "annual_charge_kwh": chg,
                "annual_discharge_kwh": dis,
                "tco": tco,
                "opt_charge_p": opt_charge,
                "opt_discharge_p": opt_discharge,
                "opt_saving_gbp": opt_saving,
                "opt_tco": opt_tco,
                "opt_grid": grid,
            })
        return results

    print("  ── Current usage (default thresholds) ──")
    battery_results = model_all_batteries(intervals, "1×", run_optimiser=True)
    print("  ── Doubled usage (default thresholds) ──")
    battery_results_2x = model_all_batteries(doubled_intervals, "2×", run_optimiser=False)

    # 6b. Solar scenario modelling
    print("\n  ── Solar PV scenario (current usage) ──")
    solar_results = []
    solar_battery_results = []

    # Pick the best battery (shortest optimised payback) for the solar+battery combo
    best_batt = min(battery_results, key=lambda x: (x.get("opt_tco") or x["tco"]).get("payback_years") or 999)
    best_batt_charge_p  = best_batt.get("opt_charge_p", CHARGE_BELOW_P_PER_KWH)
    best_batt_discharge_p = best_batt.get("opt_discharge_p", DISCHARGE_ABOVE_P_PER_KWH)

    for kwp in SOLAR_SIZES_KWP:
        # Solar only
        sr = model_solar_only(intervals, kwp)
        pb = sr["tco"].get("payback_years")
        pb_str = f"{pb:.1f} yrs" if pb else "Never"
        print(f"  [solar {kwp:.0f}kWp only]  gen {sr['annual_gen_kwh']:,.0f} kWh/yr  "
              f"benefit £{sr['annual_total_benefit_gbp']:,.0f}/yr  payback {pb_str}")
        solar_results.append(sr)

        # Solar + best battery combined
        sbr = model_solar_plus_battery(
            intervals, kwp, best_batt["battery"],
            best_batt_charge_p, best_batt_discharge_p
        )
        combo_pb = sbr["tco"].get("payback_years")
        combo_pb_str = f"{combo_pb:.1f} yrs" if combo_pb else "Never"
        print(f"  [solar {kwp:.0f}kWp + {best_batt['battery']['name']}]  "
              f"benefit £{sbr['annual_total_benefit_gbp']:,.0f}/yr  payback {combo_pb_str}")
        solar_battery_results.append(sbr)

    # 7. Generate report
    print("\n[7/7] Generating HTML report...")
    html = generate_html_report(
        usage_summary, battery_results, intervals,
        account_number, mpan, tariff_code or "Unknown",
        using_synthetic=using_synthetic,
        battery_results_2x=battery_results_2x,
        doubled_summary=doubled_summary,
        solar_results=solar_results,
        solar_battery_results=solar_battery_results,
    )
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"  ✓ Report saved to: {OUTPUT_FILE}")
    print(f"\n✅ Done! Open '{OUTPUT_FILE}' in your browser to view the report.")
    print("=" * 60)


if __name__ == "__main__":
    main()
