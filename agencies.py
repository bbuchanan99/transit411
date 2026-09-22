#!/usr/bin/env python3
"""
Transit411 agency reference: the shared table behind article pages' Explore module and the
collector's per-agency daily searches.

reference/agencies.json is the source of truth (committed and reviewable); --sync copies it into
the Postgres `agencies` table so the API and the collector can query it. Fields left blank in the
JSON stay blank - nothing is invented, and the site omits what it doesn't have.

  python agencies.py --sync           # JSON -> Postgres (safe to re-run; keeps tab edits, see below)
  python agencies.py --list           # what's stored, and which agencies have a daily search
  python agencies.py --searches       # create/refresh the daily Google News search per enabled agency

search_enabled in the JSON is only the DEFAULT for a new row: turning an agency's search on or off
in the Command Center's Sources tab wins, and --sync leaves it alone.
"""
import argparse
import json
import os

REF_FILE = os.environ.get("AGENCIES_FILE",
                          os.path.join(os.path.dirname(os.path.abspath(__file__)), "reference", "agencies.json"))
FIELDS = ["name", "state", "ntd_id", "cig_sponsor", "website", "newsroom", "procurement_url"]


def load_file(path=None):
    with open(path or REF_FILE, encoding="utf-8") as fh:
        return json.load(fh).get("agencies", [])


def create_table(conn):
    with conn.cursor() as cur:
        cur.execute("""CREATE TABLE IF NOT EXISTS agencies (
            id BIGSERIAL PRIMARY KEY, name TEXT NOT NULL, aliases TEXT[] NOT NULL DEFAULT '{}',
            state TEXT, ntd_id TEXT, cig_sponsor TEXT, website TEXT, newsroom TEXT, procurement_url TEXT,
            search_enabled BOOLEAN NOT NULL DEFAULT false, updated_at TIMESTAMPTZ DEFAULT now())""")
        cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS agencies_name_uniq ON agencies (lower(name))")
    conn.commit()


def sync(conn, path=None):
    """Copy the JSON into the table. Reference fields are refreshed from the file; search_enabled is
    set only when the row is new, so the Sources tab stays in charge of what runs."""
    create_table(conn)
    rows = load_file(path)
    added = updated = 0
    with conn.cursor() as cur:
        for a in rows:
            vals = [(a.get(f) or None) for f in FIELDS]
            cur.execute("SELECT id FROM agencies WHERE lower(name)=lower(%s)", (a["name"],))
            row = cur.fetchone()
            if row:
                cur.execute("UPDATE agencies SET " + ", ".join(f"{f}=%s" for f in FIELDS)
                            + ", aliases=%s, updated_at=now() WHERE id=%s",
                            vals + [a.get("aliases") or [], row[0]])
                updated += 1
            else:
                cur.execute("INSERT INTO agencies (" + ", ".join(FIELDS) + ", aliases, search_enabled) "
                            "VALUES (" + ", ".join(["%s"] * len(FIELDS)) + ", %s, %s)",
                            vals + [a.get("aliases") or [], bool(a.get("search_enabled"))])
                added += 1
    conn.commit()
    return added, updated, len(rows)


def all_agencies(conn):
    create_table(conn)
    with conn.cursor() as cur:
        cur.execute("SELECT id, name, aliases, state, ntd_id, cig_sponsor, website, newsroom, procurement_url, "
                    "search_enabled FROM agencies ORDER BY name")
        keys = ["id", "name", "aliases", "state", "ntd_id", "cig_sponsor", "website", "newsroom",
                "procurement_url", "search_enabled"]
        return [dict(zip(keys, r)) for r in cur.fetchall()]


def match(name, agencies):
    """An agency tag (from a post) or a CIG sponsor -> the reference row, by name or alias."""
    n = (name or "").strip().lower()
    if not n:
        return None
    for a in agencies:
        if n == a["name"].lower() or n in {x.lower() for x in (a["aliases"] or [])} \
                or n == (a["cig_sponsor"] or "").lower():
            return a
    return None


# ---- the daily per-agency Google News search -----------------------------------------------------
SEARCH_WINDOW_DAYS = int(os.environ.get("AGENCY_SEARCH_DAYS", "2"))


def agency_query(a, days=None):
    """The search for one agency: its name in quotes plus 'transit', over a short window."""
    return f'"{a["name"]}" transit when:{days or SEARCH_WINDOW_DAYS}d'


def source_name(a):
    return f"Agency: {a['name']}"


def sync_searches(conn, days=None, dry_run=False):
    """One `sources` row per search-enabled agency (type 'Agency', fetched like the keyword searches).
    Rows for agencies switched off stay in the table, disabled, so their settings and history survive."""
    import collection
    collection.migrate_sources(conn)
    agencies = all_agencies(conn)
    added = updated = disabled = 0
    with conn.cursor() as cur:
        for a in agencies:
            q = agency_query(a, days)
            name, url = source_name(a), collection.google_news_url(q)
            cur.execute("SELECT id, enabled, edited_at IS NOT NULL FROM sources WHERE lower(name)=lower(%s)", (name,))
            row = cur.fetchone()
            if not row and not a["search_enabled"]:
                continue  # never created, and not wanted yet
            if dry_run:
                added += 0 if row else 1
                continue
            if not row:
                cur.execute("INSERT INTO sources (name, url, pillar, type, method, trust, notes, query, search_days, "
                            "origin, enabled) VALUES (%s,%s,'News','Agency','RSS','Med',%s,%s,%s,'registry',%s)",
                            (name, url, f"Daily Google News search for {a['name']}.", q,
                             days or SEARCH_WINDOW_DAYS, a["search_enabled"]))
                added += 1
            elif not row[2]:  # not edited in the Sources tab: keep its URL/query in step with the agency
                cur.execute("UPDATE sources SET url=%s, query=%s, search_days=%s WHERE id=%s",
                            (url, q, days or SEARCH_WINDOW_DAYS, row[0]))
                updated += 1
            if row and row[1] and not a["search_enabled"] and not row[2]:
                cur.execute("UPDATE sources SET enabled=false WHERE id=%s", (row[0],))
                disabled += 1
    conn.commit()
    return {"added": added, "updated": updated, "disabled": disabled,
            "enabled_agencies": sum(1 for a in agencies if a["search_enabled"]), "agencies": len(agencies)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sync", action="store_true", help="reference/agencies.json -> the agencies table")
    ap.add_argument("--list", action="store_true", help="show stored agencies")
    ap.add_argument("--searches", action="store_true", help="create/refresh each enabled agency's daily search")
    ap.add_argument("--days", type=int, help="search window for --searches (default %d)" % SEARCH_WINDOW_DAYS)
    ap.add_argument("--file", help="a different agencies.json")
    a = ap.parse_args()
    import psycopg
    dsn = os.environ.get("DATABASE_URL", "postgresql://transit411:transit411@db:5432/transit411")
    with psycopg.connect(dsn) as conn:
        if a.sync:
            added, updated, total = sync(conn, a.file)
            print(f"Agencies: {added} added, {updated} updated ({total} in reference/agencies.json).")
        if a.searches:
            print("Agency searches:", sync_searches(conn, a.days))
        if a.list:
            rows = all_agencies(conn)
            print(f"{len(rows)} agencies; {sum(1 for r in rows if r['search_enabled'])} with a daily search:")
            for r in rows:
                print(f"  {'on ' if r['search_enabled'] else 'off'} {r['name'][:38]:38} {r['state'] or '--':3} "
                      f"ntd={r['ntd_id'] or '-':6} cig={r['cig_sponsor'] or '-':20} {r['website'] or ''}")
        if not (a.sync or a.searches or a.list):
            print("Nothing to do. Use --sync, --searches and/or --list.")


if __name__ == "__main__":
    main()
