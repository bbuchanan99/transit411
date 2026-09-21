#!/usr/bin/env python3
"""
Query the NTD database. Two ways in:
  python query.py --sql "SELECT agency, mode, cost_per_rider FROM ntd_service WHERE mode='Heavy Rail' ORDER BY cost_per_rider LIMIT 10"
  python query.py "cheapest subways per rider"          # natural language (needs ANTHROPIC_API_KEY)

The natural-language path is the real Ask NTD engine: the model turns your
question into ONE read-only SELECT, which runs against real NTD data. It is
conversation-aware: pass prior turns and follow-ups like "what about Texas?"
modify the previous query instead of starting over. Swap the model call for a
local LLM (Ollama) later without changing anything else.
"""
import argparse
import os
import re
import duckdb

# WITH is allowed so trend queries can use CTEs; the database is also opened read-only.
SELECT_ONLY = re.compile(r"^\s*(SELECT|WITH)\b", re.I)
FORBIDDEN = re.compile(r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|ATTACH|COPY|PRAGMA|REPLACE)\b", re.I)

SCHEMA_DOC = """Table ntd_service: the LATEST report year only, one row per agency+mode, with exactly the same
columns as ntd_history below. Use it for "current" / single-year questions about the latest year.
- upt = annual unlinked passenger trips. operating_expense in USD. cost_per_rider = operating_expense / upt.

Table ntd_history(ntd_id, agency, city, state, mode_code, mode, report_year, upt, operating_expense, fares,
  passenger_miles, vehicle_revenue_miles, vehicle_revenue_hours, voms, opex_vehicle_operations,
  opex_vehicle_maintenance, opex_facility_maintenance, opex_general_admin, cost_per_rider, fare_recovery,
  cost_per_revenue_hour, avg_trip_miles, real_dollar_year, cpi_factor, operating_expense_real, fares_real,
  cost_per_rider_real, cost_per_revenue_hour_real).
- Every report year (2015 onward), one row per agency+mode+year. Use it for trends, history, growth, "since", "vs 2019".
- Most large agencies run several modes, so an agency has several rows per year. When the question names an
  agency but no mode and doesn't ask "by mode", give the agency-wide total: GROUP BY year and aggregate, e.g.
  SUM(upt), SUM(operating_expense_real) / SUM(upt) AS cost_per_rider_real. Only split by mode when asked,
  and then include mode in the SELECT so every row is labeled. Never return several unlabeled rows for one year.
- Some rows have NULL measures (a mode that didn't report that year); add e.g. upt > 0 to skip them.
- To total a measure per year use SUM(measure) with GROUP BY report_year (plus mode/agency if shown).
  Never GROUP BY a measure column such as operating_expense.
- If the question gives no year or time span, answer for the latest year only (ntd_service). Never average or
  sum across several years unless the question asks for it.
  Example, "MBTA cost per rider each year since 2015":
    SELECT report_year, SUM(upt) AS upt, ROUND(SUM(operating_expense_real) / SUM(upt), 2) AS cost_per_rider_real
    FROM ntd_history WHERE ntd_id = '10003' AND report_year >= 2015 AND upt > 0
    GROUP BY report_year ORDER BY report_year
  Example, "MBTA cost per rider by mode since 2019":
    SELECT report_year, mode, cost_per_rider_real FROM ntd_history
    WHERE ntd_id = '10003' AND report_year >= 2019 ORDER BY mode, report_year
- Dollar columns without a suffix are RAW (nominal, as reported that year). Columns ending in _real are
  inflation-adjusted to real_dollar_year dollars with CPI-U; cpi_factor converts any raw dollar column the same way.
  For money trends across years default to the _real columns and say so; give raw values when asked for
  "reported", "nominal" or "actual" dollars. Totals across rows: SUM(operating_expense_real) / SUM(upt), not AVG.
- fare_recovery = fares / operating_expense. voms = vehicles operated in maximum service.
- 2020-2021 ridership was depressed by COVID; "2019 vs latest" is a common recovery comparison.
- An agency may be missing some years; for a like-for-like trend, keep agencies present in every compared year.

Table cpi(year, cpi_u, base_year, factor_to_base): the inflation factors used above.

Both NTD tables:
- mode is a friendly label: 'Bus', 'Heavy Rail', 'Light Rail', 'Commuter Rail', 'Streetcar', 'Ferryboat', ...
- Heavy Rail = subway / metro / rapid transit. Light Rail = LRT / streetcar-style. Commuter Rail = regional rail.
  These are DISTINCT modes; never treat subway and light rail as the same.
- Filter a state with state = 'CA'. Agency names can vary slightly by year in ntd_history, so group trends by ntd_id.
- agency holds the full legal name, never the public brand or acronym (no row says 'MBTA', 'BART' or 'TriMet').
  For these well-known systems filter by ntd_id:
  MTA New York City Transit / NYC subway='20008', MTA Bus='20188', LIRR='20100', Metro-North='20078',
  PATH='20098', NJ Transit='20080', CTA Chicago='50066', Metra='50118', LA Metro='90154',
  WMATA / DC Metro='30030', MBTA Boston='10003', SEPTA='30019', SFMTA / Muni='90015', BART='90003',
  AC Transit='90014', VTA San Jose='90013', King County Metro='00001', Sound Transit='00040',
  Washington State Ferries='00035', Miami-Dade Transit='40034', Broward County Transit='40029',
  LYNX Orlando='40035', Houston METRO='60008', DART Dallas='60056', VIA San Antonio='60011',
  CapMetro Austin='60048', San Diego MTS='90026', OCTA='90036', Long Beach Transit='90023',
  Valley Metro / Phoenix='90032', RTC Las Vegas='90045', MARTA Atlanta='40022', TriMet Portland='00008',
  RTD Denver='80006', UTA Utah='80001', MTA Maryland / Baltimore='30034', Ride On Montgomery County='30051',
  Metro Transit Minneapolis='50027', TheBus Honolulu='90002', Pittsburgh Regional Transit / PRT='30022',
  Milwaukee County Transit='50008', Cleveland RTA='50015', Metro St. Louis='70006', Bee-Line Westchester='20076',
  NICE Nassau='20206'.
  For any other agency, match distinctive words of the full name with ILIKE (e.g. agency ILIKE '%Tampa%'),
  optionally with city/state, and include agency in the output so the match is visible.
Return ONE read-only query (SELECT, or WITH ... SELECT) only."""


def run_sql(con, sql):
    if not SELECT_ONLY.match(sql) or FORBIDDEN.search(sql):
        raise ValueError("Only read-only SELECT queries are allowed.")
    return con.execute(sql).df()


def nl_to_sql(question, history=None):
    """history: prior turns [{'question': str, 'sql': str}, ...]; follow-ups modify the last query."""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return None
    import anthropic
    client = anthropic.Anthropic(api_key=key)
    messages = []
    # Only complete turns, so user/assistant messages alternate; the last 6 are plenty of context.
    for turn in [t for t in (history or []) if t.get("question") and t.get("sql")][-6:]:
        messages.append({"role": "user", "content": turn["question"][:1000]})
        messages.append({"role": "assistant", "content": turn["sql"][:4000]})
    messages.append({"role": "user", "content": question})
    msg = client.messages.create(
        model=os.environ.get("ASK_NTD_MODEL", "claude-haiku-4-5"),
        max_tokens=2000,
        system="You translate a question into ONE read-only DuckDB SELECT over the tables below. "
               "This may be a running conversation: resolve follow-ups (e.g. 'what about Texas?', "
               "'and for buses?', 'just since 2019') against the PREVIOUS query, changing only what the "
               "user changed. If the user clearly starts a new topic, ignore the prior turns. "
               "Return ONLY the SQL, no prose, no markdown fences.\n" + SCHEMA_DOC,
        messages=messages,
    )
    if msg.stop_reason == "max_tokens":
        raise ValueError("The generated query was too long and got cut off; try a narrower question.")
    text = "".join(b.text for b in msg.content if b.type == "text").strip()
    return re.sub(r"^```sql|^```|```$", "", text, flags=re.M).strip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("question", nargs="*")
    ap.add_argument("--db", default="ntd.duckdb")
    ap.add_argument("--sql", help="run raw read-only SQL")
    a = ap.parse_args()
    con = duckdb.connect(a.db, read_only=True)

    if a.sql:
        print(run_sql(con, a.sql).to_string(index=False))
        return

    q = " ".join(a.question).strip()
    if not q:
        print('Ask a question, e.g.:  python query.py "cheapest subways per rider"')
        return
    sql = nl_to_sql(q)
    if not sql:
        print("Natural language needs ANTHROPIC_API_KEY. Meanwhile use --sql, e.g.:")
        print("  python query.py --sql \"SELECT agency, mode, ROUND(cost_per_rider,2) cpr "
              "FROM ntd_service WHERE mode='Heavy Rail' ORDER BY cpr LIMIT 10\"")
        return
    print("Generated SQL:\n" + sql + "\n")
    print(run_sql(con, sql).to_string(index=False))


if __name__ == "__main__":
    main()
