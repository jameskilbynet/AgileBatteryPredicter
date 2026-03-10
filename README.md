# AgileBatteryPredicter

Connects to the Octopus Energy API, pulls your **Agile Octopus** half-hourly consumption data, models battery storage savings across five UK battery systems, and produces a self-contained HTML report with cost-of-ownership and solar analysis.

---

## Features

- Fetches real half-hourly consumption data via the Octopus Kraken GraphQL + REST APIs
- Falls back to a synthetic UK load profile if API data is unavailable (e.g. SMETS1 meters)
- Models five UK battery systems with degradation, round-trip efficiency, and warranty cycles
- Sweeps charge/discharge threshold combinations to find the optimum Agile strategy per battery
- 15-year net profit and IRR calculations for each battery
- Solar (PV) + battery combined analysis across 3kWp, 4kWp, and 6kWp system sizes
- Smart Export Guarantee (SEG) export earnings included
- Fully self-contained HTML report with Chart.js charts — no server required
[View raw](octopus_bttery_report.html)
---

## Requirements

```
pip install requests
```

Python 3.8 or later. No other dependencies.

---

## Setup

1. Get your API key from [octopus.energy/dashboard/developer](https://octopus.energy/dashboard/developer/)

2. Set it as an environment variable (recommended):
   ```bash
   export OCTOPUS_API_KEY=sk_live_your_key_here
   ```
   Or copy `.env.example` to `.env` and fill it in — then `source` it before running:
   ```bash
   cp .env.example .env
   # edit .env, then:
   source .env && export OCTOPUS_API_KEY
   ```

---

## Usage

### Battery Analysis (main report)

```bash
python3 octopus_battery_analysis.py
# or pass the key directly:
python3 octopus_battery_analysis.py --api-key sk_live_...
```

Opens/creates `octopus_battery_report.html` — open it in any browser.

### Account Diagnostic

Use this first if the main script returns no consumption data:

```bash
python3 octopus_diagnose.py
# or:
python3 octopus_diagnose.py --api-key sk_live_...
```

Prints every meter/MPAN combination on your account and probes each for available data, so you can see exactly what the API returns.

---

## Configuration

Edit the constants near the top of `octopus_battery_analysis.py`:

| Constant | Default | Description |
|---|---|---|
| `CHARGE_BELOW_P_PER_KWH` | `10.0` | Default charge threshold (p/kWh) |
| `DISCHARGE_ABOVE_P_PER_KWH` | `20.0` | Default discharge threshold (p/kWh) |
| `OPT_CHARGE_THRESHOLDS` | `-10…20` | Grid swept during optimisation |
| `OPT_DISCHARGE_THRESHOLDS` | `10…40` | Grid swept during optimisation |
| `SOLAR_SIZES_KWP` | `[3, 4, 6]` | PV system sizes to model (kWp) |
| `SOLAR_INSTALL_COST_PER_KWP` | `1600` | Installed cost per kWp (£) |
| `SEG_EXPORT_RATE_P` | `15.0` | Smart Export Guarantee rate (p/kWh) |

Battery models (capacity, cost, efficiency, warranty) are defined in the `BATTERY_MODELS` list and can be edited or extended.

---

## Report sections

| Section | Description |
|---|---|
| Usage Summary | Total kWh, daily average, spend, peak/negative rates |
| Monthly charts | Consumption (kWh) and cost (£) by month |
| Rate distribution | kWh consumed by Agile rate band |
| Battery Recommendation | Default vs optimised strategy, payback, 15yr net profit, IRR |
| Solar Analysis | PV yield, self-consumption, export earnings, combined ROI |

See [example_report.html](example_report.html) for a sample output.

---

## Notes

- **SMETS1 meters** often have a 24–48 hour data lag. If consumption data is unavailable the tool falls back to a synthetic UK load profile (3,100 kWh/year) and flags this clearly in the report.
- The report is entirely offline once generated — share it as a single HTML file.
- Never commit your API key. It is read at runtime from `--api-key` or `OCTOPUS_API_KEY`.
