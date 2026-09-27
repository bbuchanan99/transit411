#!/usr/bin/env python3
"""
Ask Funding: a question about FTA formula apportionments becomes ONE read-only SELECT.

Same shape as query.py (Ask NTD) and the same guardrails - run_sql is imported from there rather
than re-implemented, so there is exactly one place that decides what counts as read-only.

What makes this data awkward, and what the schema note below spends its words on: FTA publishes no
UZA code and no NTD id, so the key is a name string like 'Boston, MA-NH'. A question about "MBTA"
has to be answered from a place, not an agency - and the model needs telling, or it will invent an
agency column that does not exist.

  python funding_query.py --sql "SELECT uza_name, sum(amount) FROM fta_apportionments GROUP BY 1 ORDER BY 2 DESC LIMIT 5"
  python funding_query.py "which urbanized areas get the most state of good repair money"
"""

import os
import re

from query import run_sql  # one definition of "read-only", shared with Ask NTD

FUNDING_DB = os.environ.get("FUNDING_DB", "/data/funding.duckdb")

SCHEMA_DOC = """Table fta_apportionments - Federal Transit Administration formula apportionments, one row per
(program, place, component). Columns:
  fiscal_year INTEGER   the FTA fiscal year, e.g. 2026
  program     TEXT      '5307' | '5337' | '5339'
  uza_name    TEXT      the urbanized area, e.g. 'Boston, MA-NH'. NULL on statewide rows.
  state       TEXT      state name spelled out, e.g. 'Massachusetts'. NULL where the table does not say.
  measure     TEXT      which component of the program the amount is (see below)
  amount      DOUBLE    WHOLE US DOLLARS. Never thousands, never millions.
  row_kind    TEXT      'uza' for an urbanized area, 'state' for a statewide allocation
  source_table TEXT     which FTA table the row came from

The three programs:
- '5307' Urbanized Area Formula (including Section 5340). Measures: 'Section 5307', '5307 STIC'
  (Small Transit Intensive Cities), '5340 Growing States', '5340 High Density'.
- '5337' State of Good Repair - rail and high-intensity bus maintenance. Measures:
  'High Intensity Fixed Guideway State of Good Repair', 'High Intensity Motorbus State of Good Repair'.
- '5339' Buses and Bus Facilities. Measure: 'Apportionment'. This one has BOTH urbanized-area rows
  (row_kind='uza') and statewide rows (row_kind='state'); a statewide row is money apportioned to the
  governor for small urbanized areas, not to a city.

HOW TO COUNT:
- Totals are NOT stored - they are derived. To total a place, SUM(amount); never look for a 'Total' row.
- A place's total across all programs: SUM(amount) ... GROUP BY uza_name.
- A program's national total: SUM(amount) WHERE program = '...'.
- A multi-state urbanized area has one 5307 row per state (Boston, MA-NH appears under both
  Massachusetts and New Hampshire). SUM them for the area; GROUP BY state to split them.
- Mixing row_kind='uza' and row_kind='state' double-counts nothing (they are different money), but
  say which you mean when the question is about "a state": a state's own allocation is
  row_kind='state', whereas all the money flowing to places in that state is WHERE state = '...'.

NAMING - read this before writing a WHERE:
- There is NO agency column and NO NTD id. FTA apportions to PLACES, not operators. A question about
  "MBTA", "BART" or "WMATA" is answered by the urbanized area those agencies serve.
- uza_name always carries a state suffix: 'Chicago, IL-IN', 'Washington-Arlington, DC-VA-MD',
  'New York-Jersey City-Newark, NY-NJ', 'Los Angeles-Long Beach-Anaheim, CA', 'Boston, MA-NH',
  'Philadelphia, PA-NJ-DE-MD', 'San Francisco-Oakland, CA', 'Seattle-Tacoma, WA', 'Atlanta, GA'.
- Because names are compound, ALWAYS match with ILIKE on a distinctive fragment rather than '=':
  uza_name ILIKE '%Boston%', uza_name ILIKE '%Los Angeles%'. Include uza_name in the SELECT so the
  reader can see which area matched.
- state is the full name ('California', not 'CA'). The two-letter code appears only inside uza_name.

If the question gives no year, use the most recent fiscal_year present.
Return ONE read-only query (SELECT, or WITH ... SELECT) only."""


def con(db=None):
    import duckdb
    return duckdb.connect(db or FUNDING_DB, read_only=True)


def nl_to_sql(question, history=None, model=None):
    """history: prior turns [{'question': str, 'sql': str}, ...]; follow-ups modify the last query."""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return None
    import anthropic
    client = anthropic.Anthropic(api_key=key)
    messages = []
    for turn in [t for t in (history or []) if t.get("question") and t.get("sql")][-6:]:
        messages.append({"role": "user", "content": turn["question"][:1000]})
        messages.append({"role": "assistant", "content": turn["sql"][:4000]})
    messages.append({"role": "user", "content": question})
    msg = client.messages.create(
        model=model or os.environ.get("ASK_FUNDING_MODEL", os.environ.get("ASK_NTD_MODEL", "claude-haiku-4-5")),
        max_tokens=2000,
        system="You translate a question into ONE read-only DuckDB SELECT over the table below. "
               "This may be a running conversation: resolve follow-ups (e.g. 'what about Texas?', "
               "'and for buses?') against the PREVIOUS query, changing only what the user changed. "
               "If the user clearly starts a new topic, ignore the prior turns. "
               "Return ONLY the SQL, no prose, no markdown fences.\n" + SCHEMA_DOC,
        messages=messages,
    )
    if msg.stop_reason == "max_tokens":
        raise ValueError("The generated query was too long and got cut off; try a narrower question.")
    text = "".join(b.text for b in msg.content if b.type == "text").strip()
    return re.sub(r"^```sql|^```|```$", "", text, flags=re.M).strip()


def main():
    import argparse
    ap = argparse.ArgumentParser(description="Query FTA apportionments.")
    ap.add_argument("question", nargs="?")
    ap.add_argument("--sql")
    ap.add_argument("--db", default=FUNDING_DB)
    a = ap.parse_args()
    c = con(a.db)
    sql = a.sql
    if not sql:
        if not a.question:
            raise SystemExit("Give a question, or --sql.")
        sql = nl_to_sql(a.question)
        if not sql:
            raise SystemExit("Set ANTHROPIC_API_KEY for the natural-language path.")
        print("-- " + sql.replace("\n", "\n-- ") + "\n")
    df = run_sql(c, sql)
    print(df.to_string(index=False) if len(df) else "(no rows)")


if __name__ == "__main__":
    main()
