#!/usr/bin/env python3
"""Export the NTD figures the public site's data landing pages are built from.

WHY A SNAPSHOT, not a live fetch. The public API exposes no raw NTD data - the only way in is
POST /api/ask, which spends a model call per question and is capped at 200 a day for the whole
internet. Building a few hundred pages through it would be slow, expensive and non-deterministic.
NTD publishes once a year, so a committed snapshot is the honest shape for this data: the build
reads local JSON, every build is reproducible, and the public surface stays exactly as it was.

Re-run it when a new NTD report year lands:

    python tools/ntd_seo_export.py --db /data/ntd.duckdb --out site/src/data

THE QUALITY BAR lives here, not in the page templates. An agency earns a page only if it has
enough real data to fill one - see QUALIFY below. Everything else is skipped, because a page with
three numbers on it is worse than no page at all.
"""

import argparse
import json
import os

import duckdb

# An agency earns a page when it has all of this. These are deliberately strict: 302 agencies
# clear the ridership bar and 301 of those have the history, so nothing thin gets through.
MIN_UPT = 1_000_000          # annual unlinked passenger trips, agency-wide, latest year
MIN_YEARS = 3                # distinct report years, so a trend is a trend and not two dots
MIN_MODES_DATA = 1           # at least one mode with both ridership and operating expense


def rows(con, sql, params=None):
    cur = con.execute(sql, params or [])
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def one(con, sql, params=None):
    r = rows(con, sql, params)
    return r[0] if r else None


def slugify(name, ntd_id):
    keep = []
    for ch in (name or "").lower():
        keep.append(ch if ch.isalnum() else "-")
    s = "".join(keep)
    while "--" in s:
        s = s.replace("--", "-")
    return (s.strip("-")[:60] or "agency") + "-" + str(ntd_id)


def national_context(con, year):
    """Per-mode medians and counts for the latest year - what "above average" is measured against."""
    return {
        r["mode"]: r for r in rows(con, """
        SELECT mode,
               count(*)                          AS agencies,
               median(cost_per_rider)            AS median_cost_per_rider,
               median(fare_recovery)             AS median_fare_recovery,
               median(cost_per_revenue_hour)     AS median_cost_per_revenue_hour,
               SUM(upt)                          AS total_upt
          FROM ntd_service
         WHERE report_year = ? AND upt > 0 AND operating_expense > 0
         GROUP BY mode""", [year])
    }


def qualifying(con, year, limit=None):
    sql = """
        WITH totals AS (
          SELECT ntd_id, SUM(upt) AS upt
            FROM ntd_service
           WHERE report_year = ? AND upt > 0 AND operating_expense > 0
           GROUP BY ntd_id
        ), hist AS (
          SELECT ntd_id, count(DISTINCT report_year) AS years
            FROM ntd_history WHERE upt > 0 AND operating_expense > 0 GROUP BY ntd_id
        )
        SELECT t.ntd_id, t.upt, h.years
          FROM totals t JOIN hist h USING (ntd_id)
         WHERE t.upt >= ? AND h.years >= ?
         ORDER BY t.upt DESC"""
    out = rows(con, sql, [year, MIN_UPT, MIN_YEARS])
    return out[:limit] if limit else out


def agency_page(con, ntd_id, year, ctx, national_rank):
    """Everything one agency profile needs. Returns None if it cannot be filled properly."""
    head = one(con, """SELECT any_value(agency) AS agency, any_value(city) AS city,
                              any_value(state) AS state
                         FROM ntd_service WHERE ntd_id = ? AND report_year = ?
                        GROUP BY ntd_id""", [ntd_id, year])
    if not head or not head.get("agency"):
        return None

    modes = rows(con, """
        SELECT mode, upt, operating_expense, cost_per_rider, fare_recovery,
               cost_per_revenue_hour, passenger_miles, avg_trip_miles, voms,
               vehicle_revenue_hours, fares
          FROM ntd_service
         WHERE ntd_id = ? AND report_year = ? AND upt > 0 AND operating_expense > 0
         ORDER BY upt DESC""", [ntd_id, year])
    if len(modes) < MIN_MODES_DATA:
        return None

    # Agency-wide totals, and the inflation-adjusted trend that makes a 10-year line honest.
    trend = rows(con, """
        SELECT report_year AS year, SUM(upt) AS upt,
               SUM(operating_expense_real) AS opex_real,
               CASE WHEN SUM(upt) > 0 THEN SUM(operating_expense_real) / SUM(upt) END AS cost_per_rider_real
          FROM ntd_history
         WHERE ntd_id = ? AND upt > 0 AND operating_expense > 0
         GROUP BY report_year ORDER BY report_year""", [ntd_id])

    total_upt = sum(m["upt"] or 0 for m in modes)
    total_opex = sum(m["operating_expense"] or 0 for m in modes)
    total_fares = sum(m["fares"] or 0 for m in modes if m.get("fares"))

    # How this agency sits against every other agency running the same mode. This is the
    # comparison a reader actually wants, and it is the part a template cannot fake.
    for m in modes:
        c = ctx.get(m["mode"]) or {}
        med = c.get("median_cost_per_rider")
        m["mode_median_cost_per_rider"] = med
        m["vs_mode_median_pct"] = (
            round((m["cost_per_rider"] - med) / med * 100) if med and m.get("cost_per_rider") else None)
        m["mode_agencies"] = c.get("agencies")
        rank = one(con, """SELECT count(*) + 1 AS r FROM ntd_service
                            WHERE report_year = ? AND mode = ? AND upt > 0 AND upt > ?""",
                   [year, m["mode"], m["upt"]])
        m["mode_rank_by_upt"] = rank["r"] if rank else None

    state_rank = one(con, """
        SELECT count(*) + 1 AS r FROM (
          SELECT ntd_id, SUM(upt) AS u FROM ntd_service
           WHERE report_year = ? AND state = ? AND upt > 0 GROUP BY ntd_id HAVING SUM(upt) > ?)""",
        [year, head.get("state"), total_upt])

    return {
        "ntd_id": ntd_id,
        "slug": slugify(head["agency"], ntd_id),
        "agency": head["agency"],
        "city": head.get("city"),
        "state": head.get("state"),
        "year": year,
        "total_upt": total_upt,
        "total_opex": total_opex,
        "total_fares": total_fares or None,
        "cost_per_rider": (total_opex / total_upt) if total_upt else None,
        "fare_recovery": (total_fares / total_opex) if total_fares and total_opex else None,
        "national_rank_by_upt": national_rank,
        "state_rank_by_upt": state_rank["r"] if state_rank else None,
        "modes": modes,
        "trend": trend,
    }


# The ranked lists people actually search for: mode x metric, curated, not every permutation.
# `floor` keeps a two-bus operator out of a "cheapest" list, where it would win on noise.
def R(slug, mode, metric, order, floor, title, lede):
    """One ranked list. `floor` keeps a tiny operator from topping a "cheapest" list on noise."""
    return {"slug": slug, "mode": mode, "metric": metric, "order": order, "floor": floor,
            "title": title, "lede": lede}


RANKINGS = [
    # Heavy rail (16 systems) - the subway comparisons people search by name.
    R("cheapest-heavy-rail-cost-per-rider", "Heavy Rail", "cost_per_rider", "asc", 1_000_000,
      "Cheapest Heavy Rail Systems by Cost per Rider",
      "US heavy rail (subway and metro) systems ranked by operating cost per passenger trip, cheapest first."),
    R("most-expensive-heavy-rail-cost-per-rider", "Heavy Rail", "cost_per_rider", "desc", 1_000_000,
      "Most Expensive Heavy Rail Systems by Cost per Rider",
      "US heavy rail systems ranked by operating cost per passenger trip, most expensive first."),
    R("highest-ridership-heavy-rail", "Heavy Rail", "upt", "desc", 0,
      "Highest-Ridership Heavy Rail Systems",
      "US subway and metro systems ranked by annual unlinked passenger trips."),
    R("best-farebox-recovery-heavy-rail", "Heavy Rail", "fare_recovery", "desc", 1_000_000,
      "Best Farebox Recovery Among Heavy Rail Systems",
      "US heavy rail systems ranked by the share of operating cost covered by fares."),

    # Light rail (22 systems).
    R("cheapest-light-rail-cost-per-rider", "Light Rail", "cost_per_rider", "asc", 500_000,
      "Cheapest Light Rail Systems by Cost per Rider",
      "US light rail systems ranked by operating cost per passenger trip, cheapest first."),
    R("most-expensive-light-rail-cost-per-rider", "Light Rail", "cost_per_rider", "desc", 500_000,
      "Most Expensive Light Rail Systems by Cost per Rider",
      "US light rail systems ranked by operating cost per passenger trip, most expensive first."),
    R("highest-ridership-light-rail", "Light Rail", "upt", "desc", 0,
      "Highest-Ridership Light Rail Systems",
      "US light rail systems ranked by annual unlinked passenger trips."),

    # Commuter rail (27 systems).
    R("cheapest-commuter-rail-cost-per-rider", "Commuter Rail", "cost_per_rider", "asc", 500_000,
      "Cheapest Commuter Rail Systems by Cost per Rider",
      "US commuter rail operators ranked by operating cost per passenger trip, cheapest first."),
    R("most-expensive-commuter-rail-cost-per-rider", "Commuter Rail", "cost_per_rider", "desc", 500_000,
      "Most Expensive Commuter Rail Systems by Cost per Rider",
      "US commuter rail operators ranked by operating cost per passenger trip, most expensive first."),
    R("highest-ridership-commuter-rail", "Commuter Rail", "upt", "desc", 0,
      "Highest-Ridership Commuter Rail Systems",
      "US commuter rail operators ranked by annual unlinked passenger trips."),
    R("best-farebox-recovery-commuter-rail", "Commuter Rail", "fare_recovery", "desc", 500_000,
      "Best Farebox Recovery Among Commuter Rail Systems",
      "US commuter rail operators ranked by the share of operating cost covered by fares."),

    # Bus - 1,162 reporters, so every bus list carries a floor or it becomes a list of village
    # minibuses with three riders and a rounding error.
    R("cheapest-bus-systems-cost-per-rider", "Bus", "cost_per_rider", "asc", 5_000_000,
      "Cheapest Bus Systems by Cost per Rider",
      "Large US bus operators ranked by operating cost per passenger trip, cheapest first."),
    R("most-expensive-bus-systems-cost-per-rider", "Bus", "cost_per_rider", "desc", 5_000_000,
      "Most Expensive Bus Systems by Cost per Rider",
      "Large US bus operators ranked by operating cost per passenger trip, most expensive first."),
    R("highest-ridership-bus-agencies", "Bus", "upt", "desc", 0,
      "Highest-Ridership Bus Agencies",
      "US bus operators ranked by annual unlinked passenger trips."),
    R("best-farebox-recovery-bus", "Bus", "fare_recovery", "desc", 5_000_000,
      "Best Farebox Recovery Among Large Bus Systems",
      "Large US bus operators ranked by the share of operating cost covered by fares."),

    # The smaller modes, where a ranked list is genuinely hard to find anywhere else.
    R("highest-ridership-bus-rapid-transit", "Bus Rapid Transit", "upt", "desc", 0,
      "Highest-Ridership Bus Rapid Transit Systems",
      "US BRT systems ranked by annual unlinked passenger trips."),
    R("highest-ridership-streetcar", "Streetcar", "upt", "desc", 0,
      "Highest-Ridership Streetcar Systems",
      "US streetcar systems ranked by annual unlinked passenger trips."),
    R("highest-ridership-ferry", "Ferryboat", "upt", "desc", 0,
      "Highest-Ridership Ferry Systems",
      "US passenger ferry operators ranked by annual unlinked passenger trips."),
    R("most-expensive-demand-response-cost-per-rider", "Demand Response", "cost_per_rider", "desc", 500_000,
      "Most Expensive Demand-Response (Paratransit) Services by Cost per Rider",
      "Large US demand-response and paratransit operations ranked by operating cost per passenger trip."),
    R("highest-ridership-demand-response", "Demand Response", "upt", "desc", 0,
      "Highest-Ridership Demand-Response (Paratransit) Services",
      "US demand-response and paratransit operations ranked by annual unlinked passenger trips."),
]


METRIC_LABEL = {
    "cost_per_rider": "Cost per rider",
    "upt": "Annual trips",
    "fare_recovery": "Farebox recovery",
}


def ranking_page(con, spec, year, ctx, slugs):
    metric = spec["metric"]
    direction = "ASC" if spec["order"] == "asc" else "DESC"
    data = rows(con, f"""
        SELECT ntd_id, agency, city, state, upt, operating_expense, cost_per_rider,
               fare_recovery, cost_per_revenue_hour
          FROM ntd_service
         WHERE report_year = ? AND mode = ? AND upt >= ? AND upt > 0
               AND operating_expense > 0 AND {metric} IS NOT NULL
         ORDER BY {metric} {direction}
         LIMIT 40""", [year, spec["mode"], spec["floor"]])
    if len(data) < 5:
        return None                      # too few to be a ranking; publishing it would be filler
    for i, r in enumerate(data, 1):
        r["rank"] = i
        r["slug"] = slugs.get(r["ntd_id"])
    c = ctx.get(spec["mode"]) or {}
    return {
        "slug": spec["slug"], "title": spec["title"], "lede": spec["lede"],
        "mode": spec["mode"], "metric": metric, "metric_label": METRIC_LABEL[metric],
        "order": spec["order"], "floor": spec["floor"], "year": year,
        "rows": data,
        "mode_agencies": c.get("agencies"),
        "mode_median_cost_per_rider": c.get("median_cost_per_rider"),
        "mode_total_upt": c.get("total_upt"),
    }


def main():
    ap = argparse.ArgumentParser(description="Export NTD data for the public site's landing pages.")
    ap.add_argument("--db", default=os.environ.get("NTD_DB", "/data/ntd.duckdb"))
    ap.add_argument("--out", default="site/src/data")
    ap.add_argument("--limit", type=int, default=0, help="only the N largest agencies (0 = all that qualify)")
    a = ap.parse_args()

    con = duckdb.connect(a.db, read_only=True)
    year = one(con, "SELECT max(report_year) AS y FROM ntd_service")["y"]
    ctx = national_context(con, year)

    picked = qualifying(con, year, a.limit or None)
    all_qualifying = len(qualifying(con, year))
    agencies, skipped = [], 0
    for rank, row in enumerate(picked, 1):
        page = agency_page(con, row["ntd_id"], year, ctx, rank)
        if page:
            agencies.append(page)
        else:
            skipped += 1

    slugs = {x["ntd_id"]: x["slug"] for x in agencies}
    rankings = [r for r in (ranking_page(con, s, year, ctx, slugs) for s in RANKINGS) if r]

    os.makedirs(a.out, exist_ok=True)
    meta = {
        "year": year,
        "generated_from": "National Transit Database (FTA) annual reports",
        "qualify": {"min_upt": MIN_UPT, "min_years": MIN_YEARS},
        "agencies_qualifying": all_qualifying,
        "agencies_exported": len(agencies),
        "modes": {m: {k: v for k, v in c.items() if k != "mode"} for m, c in ctx.items()},
    }
    for name, payload in (("ntd-agencies.json", {"meta": meta, "agencies": agencies}),
                          ("ntd-rankings.json", {"meta": meta, "rankings": rankings})):
        p = os.path.join(a.out, name)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, separators=(",", ":"), default=float)
        print("  %-22s %8d bytes" % (name, os.path.getsize(p)))

    print("\nreport year %s | %d agencies qualify, %d exported, %d skipped as too thin | %d rankings"
          % (year, all_qualifying, len(agencies), skipped, len(rankings)))


if __name__ == "__main__":
    main()
