#!/usr/bin/env python3
"""
Build a mode-aware NTD database for Transit411's Ask NTD tool.

Source: data.transportation.gov Socrata dataset `npsm-38gk`
  ("NTD Annual Data - Service Data and Operating Expenses by Mode", report years 2015+).
Run where the DOT domain is reachable (your laptop or NAS). Produces ntd.duckdb with:
  ntd_service  - latest report year, one row per agency+mode (the original table, unchanged)
  ntd_history  - every report year, one row per agency+mode+year, raw and inflation-adjusted
  cpi          - the CPI-U inflation factors used for the adjusted columns

  python build_db.py                      # fetch every year from data.transportation.gov
  python build_db.py --since 2019         # only report years 2019 onward
  python build_db.py --file service.csv   # or load a file you downloaded yourself
  python build_db.py --sample 5000        # small fetch for a quick test
"""
import argparse
import duckdb
import pandas as pd

SOCRATA_RESOURCE = "npsm-38gk"
SOCRATA_URL = f"https://data.transportation.gov/resource/{SOCRATA_RESOURCE}.json"

# NTD mode codes -> friendly labels. Heavy Rail = subway/metro; kept distinct from Light Rail.
MODE_LABELS = {
    "MB": "Bus", "CB": "Commuter Bus", "RB": "Bus Rapid Transit", "TB": "Trolleybus",
    "HR": "Heavy Rail", "LR": "Light Rail", "CR": "Commuter Rail", "YR": "Hybrid Rail",
    "SR": "Streetcar", "MG": "Monorail/Automated Guideway", "CC": "Cable Car",
    "IP": "Inclined Plane", "DR": "Demand Response", "DT": "Demand Response - Taxi",
    "VP": "Vanpool", "FB": "Ferryboat", "AR": "Alaska Railroad", "PB": "Publico",
    "TR": "Aerial Tramway", "OR": "Other Rail", "MO": "Other",
}

# The DOT dataset is "long": one row per agency/mode/type-of-service/year/measure, with the
# measure name in `field` and the number in `value`. Field name -> our column name.
# "Operating Expenses" is the total; the "Operating Expenses - ..." fields break it down.
MEASURES = {
    "Unlinked Passenger Trips": "upt",
    "Operating Expenses": "operating_expense",
    "Fares": "fares",
    "Passenger Miles Traveled": "passenger_miles",
    "Vehicle Revenue Miles": "vehicle_revenue_miles",
    "Vehicle Revenue Hours": "vehicle_revenue_hours",
    "Vehicles Operated in Maximum Service": "voms",
    "Operating Expenses - Vehicle Operations": "opex_vehicle_operations",
    "Operating Expenses - Vehicle Maintenance": "opex_vehicle_maintenance",
    "Operating Expenses - Facility Maintenance": "opex_facility_maintenance",
    "Operating Expenses - General Administration": "opex_general_admin",
}

# CPI-U, U.S. city average, all items, not seasonally adjusted (BLS series CUUR0000SA0):
# annual average of the 12 monthly values. Add the new year here when NTD publishes one.
CPI_U = {
    2015: 237.017, 2016: 240.007, 2017: 245.120, 2018: 251.107, 2019: 255.657,
    2020: 258.811, 2021: 270.970, 2022: 292.655, 2023: 304.702, 2024: 313.689,
}

KEYS = ["ntd_id", "agency", "city", "state", "mode_code", "report_year"]


def fetch_socrata(limit_rows=None, since=None):
    import requests
    fields = ",".join("'" + f + "'" for f in MEASURES)
    where = f"field in ({fields})" + (f" AND report_year >= '{since}'" if since else "")
    print(f"Fetching from {SOCRATA_URL}" + (f" (report years {since}+)" if since else " (all report years)"))
    rows, offset, page = [], 0, 50000
    while True:
        r = requests.get(SOCRATA_URL, params={"$where": where, "$order": ":id", "$limit": page,
                                              "$offset": offset}, timeout=180)
        r.raise_for_status()
        batch = r.json()
        if not batch:
            break
        rows.extend(batch)
        offset += len(batch)
        if (limit_rows and len(rows) >= limit_rows) or len(batch) < page:
            break
    return pd.DataFrame(rows)


def load_local(path):
    if path.lower().endswith((".xlsx", ".xls")):
        return pd.read_excel(path)
    return pd.read_csv(path, dtype=str)


def find_col(cols, *keywords):
    """Column named exactly the (single) keyword, else the first whose name contains all keywords."""
    if len(keywords) == 1:
        for c in cols:
            if str(c).lower() == keywords[0]:
                return c
    for c in cols:
        low = str(c).lower()
        if all(k in low for k in keywords):
            return c
    return None


def num(series):
    return pd.to_numeric(series.astype(str).str.replace(r"[,$\s]", "", regex=True), errors="coerce")


def to_long(df, year=None):
    """Reduce either input layout to rows of (KEYS..., measure, value)."""
    if {"field", "value"} <= set(df.columns):  # DOT layout: one row per measure
        df = df[df["field"].isin(MEASURES)]
        return pd.DataFrame({
            "ntd_id": df.get("ntd_id", ""), "agency": df.get("agency_name", ""),
            "city": df.get("city", ""), "state": df.get("state", ""), "mode_code": df["mode"],
            "report_year": df.get("report_year", year),
            "measure": df["field"].map(MEASURES), "value": df["value"],
        })

    # Flat file: one column per measure (e.g. sample_ntd.csv).
    cols = list(df.columns)
    c_mode = find_col(cols, "mode")
    c_agency = find_col(cols, "agency")
    c_city = find_col(cols, "city")
    c_state = find_col(cols, "state")
    c_id = find_col(cols, "ntd", "id") or find_col(cols, "id")
    c_year = find_col(cols, "report", "year")
    c_upt = find_col(cols, "unlinked", "passenger", "trips") or find_col(cols, "upt")
    c_opex = find_col(cols, "operating", "expense")
    if not (c_mode and c_upt and c_opex):
        raise SystemExit("Could not locate mode / UPT / operating-expense columns.\n"
                         f"Columns present: {cols}")
    flat = pd.DataFrame({
        "ntd_id": df[c_id] if c_id else "", "agency": df[c_agency] if c_agency else "",
        "city": df[c_city] if c_city else "", "state": df[c_state] if c_state else "",
        "mode_code": df[c_mode], "report_year": df[c_year] if c_year else year,
        "upt": df[c_upt], "operating_expense": df[c_opex],
    })
    return flat.melt(id_vars=KEYS, var_name="measure", value_name="value")


def build_history(long):
    """One row per agency+mode+year, one column per measure, summed across type of service
    (directly operated + purchased transportation)."""
    text = ["ntd_id", "agency", "city", "state"]
    long = long.assign(
        **{k: long[k].fillna("").astype(str).str.strip() for k in text},
        mode_code=long["mode_code"].astype(str).str.upper().str.strip(),
        report_year=pd.to_numeric(long["report_year"], errors="coerce").astype("Int64"),
        value=num(long["value"]),
    ).dropna(subset=["value"])
    wide = long.groupby(KEYS + ["measure"], dropna=False)["value"].sum().unstack("measure").reset_index()
    wide.columns.name = None
    wide = wide.reindex(columns=KEYS + list(MEASURES.values()))
    wide.insert(KEYS.index("mode_code") + 1, "mode",
                wide["mode_code"].map(MODE_LABELS).fillna(wide["mode_code"]))
    return wide


def cpi_table(years):
    """Factors that convert each year's dollars into dollars of the latest year we have CPI for."""
    known = sorted(y for y in years if y in CPI_U)
    missing = sorted(y for y in years if y not in CPI_U)
    if missing:
        print(f"WARNING: no CPI-U value for report year(s) {missing}; their inflation-adjusted "
              "columns will be empty. Add them to CPI_U in build_db.py.")
    if not known:
        return pd.DataFrame(columns=["year", "cpi_u", "base_year", "factor_to_base"])
    base = known[-1]
    return pd.DataFrame([{"year": y, "cpi_u": CPI_U[y], "base_year": base,
                          "factor_to_base": round(CPI_U[base] / CPI_U[y], 6)} for y in sorted(CPI_U)])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", help="local NTD CSV/XLSX; omit to fetch from data.transportation.gov")
    ap.add_argument("--db", default="ntd.duckdb")
    ap.add_argument("--sample", type=int, help="cap fetched rows (quick test)")
    ap.add_argument("--since", type=int, help="first report year to fetch (default: all)")
    ap.add_argument("--year", type=int, help="report year of a flat --file that has no year column")
    a = ap.parse_args()

    df = load_local(a.file) if a.file else fetch_socrata(a.sample, a.since)
    print(f"Loaded {len(df):,} raw rows, {len(df.columns)} columns")
    hist = build_history(to_long(df, a.year))
    years = sorted(int(y) for y in hist["report_year"].dropna().unique())
    print(f"Normalized to {len(hist):,} agency-mode-year rows across {hist['mode'].nunique()} modes, "
          f"report years: {', '.join(map(str, years)) or 'unknown'}")
    cpi = cpi_table(years)

    con = duckdb.connect(a.db)
    for t in ("ntd_service", "ntd_history", "cpi"):
        con.execute(f"DROP TABLE IF EXISTS {t}")
    con.register("h", hist)
    con.register("c", cpi)
    con.execute("CREATE TABLE cpi AS SELECT year::INTEGER AS year, cpi_u::DOUBLE AS cpi_u, "
                "base_year::INTEGER AS base_year, factor_to_base::DOUBLE AS factor_to_base FROM c")
    con.execute("""
        CREATE TABLE ntd_history AS
        SELECT h.*,
               ROUND(CASE WHEN upt > 0 THEN operating_expense / upt END, 2)                     AS cost_per_rider,
               ROUND(CASE WHEN operating_expense > 0 THEN fares / operating_expense END, 4)     AS fare_recovery,
               ROUND(CASE WHEN vehicle_revenue_hours > 0
                          THEN operating_expense / vehicle_revenue_hours END, 2)                AS cost_per_revenue_hour,
               ROUND(CASE WHEN upt > 0 THEN passenger_miles / upt END, 2)                       AS avg_trip_miles,
               cpi.base_year                                                                   AS real_dollar_year,
               cpi.factor_to_base                                                              AS cpi_factor,
               ROUND(operating_expense * cpi.factor_to_base, 0)                                AS operating_expense_real,
               ROUND(fares * cpi.factor_to_base, 0)                                            AS fares_real,
               ROUND(CASE WHEN upt > 0 THEN operating_expense * cpi.factor_to_base / upt END, 2) AS cost_per_rider_real,
               ROUND(CASE WHEN vehicle_revenue_hours > 0
                          THEN operating_expense * cpi.factor_to_base / vehicle_revenue_hours END, 2)
                                                                                               AS cost_per_revenue_hour_real
        FROM h LEFT JOIN cpi ON cpi.year = h.report_year
        ORDER BY report_year, ntd_id, mode_code""")
    # Latest year only. The original columns come first so existing queries keep working; the rest
    # match ntd_history, so any column works in either table.
    con.execute("""
        CREATE TABLE ntd_service AS
        SELECT ntd_id, agency, city, state, mode_code, mode, upt, operating_expense, cost_per_rider,
               * EXCLUDE (ntd_id, agency, city, state, mode_code, mode, upt, operating_expense, cost_per_rider)
        FROM ntd_history
        WHERE report_year IS NOT DISTINCT FROM (SELECT MAX(report_year) FROM ntd_history)
          AND upt > 0 AND operating_expense IS NOT NULL""")
    con.unregister("h")
    con.unregister("c")

    latest = con.execute(
        "SELECT mode, COUNT(*) agencies, ROUND(AVG(cost_per_rider),2) avg_cost_per_rider "
        "FROM ntd_service GROUP BY mode ORDER BY agencies DESC").df()
    trend = con.execute("""
        SELECT report_year, COUNT(*) agency_modes,
               ROUND(SUM(upt) / 1e6, 1) upt_millions,
               ROUND(SUM(operating_expense) / 1e9, 2) opex_billions,
               ROUND(SUM(operating_expense_real) / 1e9, 2) opex_billions_real,
               ROUND(SUM(operating_expense) / SUM(upt), 2) cost_per_rider,
               ROUND(SUM(operating_expense_real) / SUM(upt), 2) cost_per_rider_real
        FROM ntd_history WHERE upt > 0 AND operating_expense IS NOT NULL
        GROUP BY report_year ORDER BY report_year""").df()
    n_service = con.execute("SELECT COUNT(*) FROM ntd_service").fetchone()[0]
    con.close()

    print(f"\nWrote {a.db}: ntd_service ({n_service:,} rows, latest year), "
          f"ntd_history ({len(hist):,} rows), cpi ({len(cpi)} rows)\n")
    print("Latest year by mode:")
    print(latest.to_string(index=False))
    if len(cpi):
        print(f"\nNational totals by year (real = {int(cpi['base_year'].iloc[0])} dollars, CPI-U):")
    print(trend.to_string(index=False))


if __name__ == "__main__":
    main()
