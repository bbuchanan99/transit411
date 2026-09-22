#!/usr/bin/env python3
"""
Transit411 CIG project profile archive (versioned, with change tracking).

Each project in FTA's CIG pipeline has a project profile PDF, listed on FTA's "Current CIG
Projects" page. Every distinct version of a profile is archived with a text hash, so a revised
profile is detected and diffed against the one before - the same snapshot-history idea as
cig_projects.

transit.dot.gov blocks automated access to its pages (Akamai returns HTTP 403 to the listing page
and to each project's profile page), so we don't scrape around it. Instead:
  - profile links come from a copy of the listing page saved in a browser (Grants tab >
    Load projects page, or --listing FILE);
  - profile PDFs come from browser downloads: upload them on the Grants tab, or drop them in the
    inbox folder (DATA_DIR/cig_profiles/inbox), which the weekly check picks up.
The weekly check also tries the listing page once, as itself; if FTA ever serves it, profiles are
fetched from there (unchanged files are not downloaded again).

  python cig_profiles.py --listing page.html   # profile links (profile_url) from a saved listing page
  python cig_profiles.py --seed                # baseline versions from the PDFs already kept (cig.py --profiles)
  python cig_profiles.py --ingest a.pdf b.pdf  # archive profile PDFs
  python cig_profiles.py --weekly              # inbox + one polite try of the listing page
"""
import argparse
import hashlib
import html as htmlmod
import os
import re
from datetime import datetime, timezone

import cig

ARCHIVE_DIR = os.environ.get("CIG_PROFILE_DIR", "/data/cig_profiles")
INBOX_DIR = os.path.join(ARCHIVE_DIR, "inbox")
LISTING_URL = ("https://www.transit.dot.gov/funding/grant-programs/capital-investments/"
               "current-capital-investment-grant-cig-projects")
UA = {"User-Agent": "Transit411/1.0 (+https://transit411.net)"}
FTA_HOSTS = {"www.transit.dot.gov", "transit.dot.gov"}


def create_tables(conn):
    with conn.cursor() as cur:
        # One row per project: its profile page on FTA's site (profile_url), from the listing page.
        cur.execute("""CREATE TABLE IF NOT EXISTS cig_profile_pages (
            project_name TEXT NOT NULL, sponsor TEXT NOT NULL DEFAULT '', state TEXT,
            profile_url TEXT, listing_name TEXT, listing_stage TEXT, updated_at TIMESTAMPTZ DEFAULT now(),
            PRIMARY KEY (project_name, sponsor))""")
        # Every distinct version of a project's profile. changed = the text differs from the version
        # before (the first version is the baseline, changed = false); diff is against prev_version_id.
        cur.execute("""CREATE TABLE IF NOT EXISTS cig_profile_versions (
            id BIGSERIAL PRIMARY KEY, project_name TEXT NOT NULL, sponsor TEXT NOT NULL DEFAULT '',
            profile_url TEXT, captured_at TIMESTAMPTZ NOT NULL DEFAULT now(), text_sha256 TEXT NOT NULL,
            file_sha256 TEXT, file_path TEXT, file_name TEXT, file_bytes INTEGER, pdf_url TEXT,
            source TEXT, fta_date TEXT, changed BOOLEAN NOT NULL DEFAULT false,
            prev_version_id BIGINT REFERENCES cig_profile_versions(id),
            text TEXT, diff TEXT, lines_added INTEGER, lines_removed INTEGER)""")
        cur.execute("CREATE INDEX IF NOT EXISTS cig_pv_proj ON cig_profile_versions (project_name, sponsor, captured_at)")
        # Each weekly/on-demand check: what it tried and what it found.
        cur.execute("""CREATE TABLE IF NOT EXISTS cig_profile_runs (
            id BIGSERIAL PRIMARY KEY, ran_at TIMESTAMPTZ DEFAULT now(), trigger TEXT,
            listing_status TEXT, files INTEGER, new_versions INTEGER, changed INTEGER, unchanged INTEGER,
            unmatched TEXT[], message TEXT)""")
    conn.commit()


def current_projects(conn):
    """The latest snapshot's projects: [(project_name, sponsor, state)]."""
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('cig_projects') IS NOT NULL")
        if not cur.fetchone()[0]:
            return []
        cur.execute("SELECT project_name, coalesce(sponsor,''), state FROM cig_projects "
                    "WHERE snapshot_date=(SELECT max(snapshot_date) FROM cig_projects) ORDER BY project_name")
        return cur.fetchall()


# ---- Step 2: profile links from FTA's listing page ------------------------------------------------
def _cell(s):
    return re.sub(r"\s+", " ", htmlmod.unescape(re.sub(r"<[^>]+>", " ", s)).replace("\xa0", " ")).strip()


def parse_listing(page):
    """The listing page's project table -> [{state, city, name, stage, profile_url}]. The table's
    columns are State | City | Project Name | Stage of Development | Project Profile (a link)."""
    out = []
    for tbl in re.findall(r"<table\b.*?</table>", page, re.S | re.I):
        heads = [_cell(h).lower() for h in re.findall(r"<th[^>]*>(.*?)</th>", tbl, re.S | re.I)]
        if "project name" not in heads or not any("profile" in h for h in heads):
            continue
        col = {h: i for i, h in enumerate(heads)}
        i_state, i_city, i_name = col.get("state", 0), col.get("city", 1), col["project name"]
        i_stage = next((i for h, i in col.items() if h.startswith("stage")), None)
        i_prof = next(i for h, i in col.items() if "profile" in h)
        for row in re.findall(r"<tr[^>]*>(.*?)</tr>", tbl, re.S | re.I):
            cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.S | re.I)
            if len(cells) <= max(i_name, i_prof):
                continue
            href = re.search(r'href="([^"]+)"', cells[i_prof])
            url = htmlmod.unescape(href.group(1)).strip() if href else None
            if url and url.startswith("/"):
                url = "https://www.transit.dot.gov" + url
            out.append({"state": _cell(cells[i_state]), "city": _cell(cells[i_city]), "name": _cell(cells[i_name]),
                        "stage": _cell(cells[i_stage]) if i_stage is not None else None, "profile_url": url})
    return out


def match_listing(rows, projects):
    """Pair listing rows with current projects (same state, normalized name; cig.match_profile's rules).
    Returns (pairs [(project, row)], unmatched listing rows, projects without a row)."""
    cands = [{"key": cig.norm_name(r["name"]), "states": [s for s in r["state"].split("-") if s], "row": r}
             for r in rows]
    pairs, used = [], set()
    for p in projects:
        m = cig.match_profile(p[0], p[2], cands)
        if m and id(m) not in used:
            used.add(id(m))
            pairs.append((p, m["row"]))
    matched = {id(r) for _, r in pairs}
    return pairs, [r for r in rows if id(r) not in matched], [p for p in projects if p not in {q for q, _ in pairs}]


def load_listing(conn, page):
    """Store each current project's profile_url from a listing page. Returns a coverage report."""
    create_tables(conn)
    rows = parse_listing(page)
    if not rows:
        raise ValueError("No project table found. Save FTA's Current CIG Projects page (Ctrl+S) and load that file.")
    projects = current_projects(conn)
    pairs, extra, missing = match_listing(rows, projects)
    with conn.cursor() as cur:
        for (name, sponsor, state), r in pairs:
            cur.execute("""INSERT INTO cig_profile_pages (project_name, sponsor, state, profile_url, listing_name,
                             listing_stage, updated_at) VALUES (%s,%s,%s,%s,%s,%s,now())
                           ON CONFLICT (project_name, sponsor) DO UPDATE SET state=EXCLUDED.state,
                             profile_url=EXCLUDED.profile_url, listing_name=EXCLUDED.listing_name,
                             listing_stage=EXCLUDED.listing_stage, updated_at=now()""",
                        (name, sponsor, state, r["profile_url"], r["name"], r["stage"]))
    conn.commit()
    return {"listed": len(rows), "projects": len(projects), "matched": len(pairs),
            "renamed": [f"{p[0]} <- {r['name']}" for p, r in pairs if cig.norm_name(p[0]) != cig.norm_name(r["name"])],
            "listed_not_in_dashboard": [f"{r['state']} {r['name']}" for r in extra],
            "projects_not_listed": [f"{p[2]} {p[0]}" for p in missing]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--listing", metavar="HTML", help="profile links from a saved copy of FTA's listing page")
    a = ap.parse_args()
    import psycopg
    dsn = os.environ.get("DATABASE_URL", "postgresql://transit411:transit411@db:5432/transit411")
    with psycopg.connect(dsn) as conn:
        create_tables(conn)
        if a.listing:
            with open(a.listing, encoding="utf-8", errors="replace") as fh:
                rep = load_listing(conn, fh.read())
            for k, v in rep.items():
                print(f"{k}: {v}")


if __name__ == "__main__":
    main()
