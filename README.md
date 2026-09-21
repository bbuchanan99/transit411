# Transit411 — NTD data module

The portable core of **Ask NTD**: pull real National Transit Database figures,
build a clean mode-aware database, and answer questions with real numbers.
Runs anywhere Python runs (your laptop today, your NAS later). No server required.

## What it does
- Pulls **real NTD data** from data.transportation.gov (dataset `npsm-38gk`,
  "NTD Annual Data — Service Data and Operating Expenses by Mode", report years 2015+).
- Normalizes it to one row per **agency + mode**, with modes kept distinct
  (Heavy Rail = subway, Light Rail, Commuter Rail, Bus, …).
- Computes `cost_per_rider = operating_expense / unlinked_passenger_trips`.
- Builds three tables:
  - `ntd_service` — latest report year only (the table the examples below use).
  - `ntd_history` — every report year, for trends: trips, expenses (total and by function),
    fares, passenger miles, revenue miles/hours, peak vehicles, plus derived ratios.
    Dollar columns are kept **raw** (as reported) and also **inflation-adjusted**
    (`*_real`, in latest-year dollars using CPI-U).
  - `cpi` — the CPI-U factors behind the `*_real` columns. Add a year to `CPI_U` in
    `build_db.py` when NTD publishes a new one.
- NTD report years follow each agency's fiscal year, so CPI adjustment by calendar
  year is a close approximation, not exact.
- Answers questions — either raw read-only SQL, or plain English via a model.

## Setup
```bash
pip install -r requirements.txt
```

## 1. Build the database
Run this where the DOT domain is reachable (i.e. not behind a blocked network):
```bash
python build_db.py                 # fetches the latest from data.transportation.gov -> ntd.duckdb
# or, if you downloaded the flat file yourself:
python build_db.py --file service.csv
# quick test with a partial fetch:
python build_db.py --sample 5000
```

## 2. Ask questions
```bash
# raw SQL (always works, no key needed):
python query.py --sql "SELECT agency, mode, ROUND(cost_per_rider,2) cpr \
  FROM ntd_service WHERE mode='Heavy Rail' ORDER BY cpr LIMIT 10"

# natural language (set a key first):
export ANTHROPIC_API_KEY=sk-ant-...
python query.py "cheapest subways per rider"
python query.py "compare light rail vs commuter rail cost per rider"
```
The natural-language path turns your question into ONE read-only SELECT and runs
it against real data — the same flow as the web preview, now on real numbers.

## Guardrails (built in)
- Only `SELECT` runs; INSERT/UPDATE/DELETE/DROP/etc. are refused.
- The database is opened read-only for queries.
- The model writes the *query*; the data provides the *answer* — numbers are
  never taken from the model's memory.

## Swapping in a local LLM
`query.py` calls the Anthropic API for NL→SQL. To use your local model instead,
replace the `nl_to_sql()` body with a call to your Ollama endpoint — nothing
else changes. Keep a frontier model for the trickier questions if you like.

## Where this fits
This module is the portable first step. Next it becomes a container on the NAS:
Postgres + pgvector for content, this ingestion on a schedule, and a small API
that the Transit411 site calls. This same code moves in unchanged.
