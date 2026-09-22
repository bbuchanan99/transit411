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
        # PDF link found on a project's FTA page by a browser pass (load_pdf_links), when known.
        cur.execute("ALTER TABLE cig_profile_pages ADD COLUMN IF NOT EXISTS pdf_url TEXT")
        cur.execute("ALTER TABLE cig_profile_pages ADD COLUMN IF NOT EXISTS pdf_checked_at TIMESTAMPTZ")
        # Every distinct copy of FTA's listing page (its project rows), and what changed vs. the one before.
        cur.execute("""CREATE TABLE IF NOT EXISTS cig_profile_listings (
            id BIGSERIAL PRIMARY KEY, captured_at TIMESTAMPTZ DEFAULT now(), source TEXT,
            rows_sha256 TEXT, rows JSONB, changes JSONB)""")
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


def _lkey(r):
    return (r["state"], cig.norm_name(r["name"]))


def listing_changes(old, new):
    """What changed between two listing snapshots (lists of rows): added, removed, stage and link changes."""
    o, n = {_lkey(r): r for r in old}, {_lkey(r): r for r in new}
    out = []
    for k, r in n.items():
        if k not in o:
            out.append({"kind": "added", "name": r["name"], "state": r["state"], "profile_url": r["profile_url"],
                        "detail": f"new on FTA's page ({r['stage'] or 'stage ?'})"})
            continue
        was = o[k]
        if (was.get("stage") or "") != (r.get("stage") or ""):
            out.append({"kind": "stage", "name": r["name"], "state": r["state"], "profile_url": r["profile_url"],
                        "detail": f"stage {was.get('stage') or '?'} → {r.get('stage') or '?'}"})
        if (was.get("profile_url") or "") != (r.get("profile_url") or ""):
            out.append({"kind": "link", "name": r["name"], "state": r["state"], "profile_url": r["profile_url"],
                        "detail": "profile link changed"})
    for k, r in o.items():
        if k not in n:
            out.append({"kind": "removed", "name": r["name"], "state": r["state"], "profile_url": r["profile_url"],
                        "detail": "no longer on FTA's page"})
    return out


def to_download(conn, changes=()):
    """Profiles worth downloading: current projects whose listing entry changed (new, stage, link) or
    whose FTA page links a PDF we haven't archived, plus current projects with no archived version."""
    from urllib.parse import unquote
    with conn.cursor() as cur:
        cur.execute("""SELECT g.project_name, g.sponsor, g.listing_name, g.state, g.profile_url, g.pdf_url,
                         (SELECT array_agg(coalesce(v.pdf_url,'') || '|' || coalesce(v.file_name,'')) FROM cig_profile_versions v
                          WHERE v.project_name=g.project_name AND v.sponsor=g.sponsor)
                       FROM cig_profile_pages g
                       JOIN (SELECT DISTINCT project_name, coalesce(sponsor,'') AS sponsor FROM cig_projects
                             WHERE snapshot_date=(SELECT max(snapshot_date) FROM cig_projects)) p
                         ON p.project_name=g.project_name AND p.sponsor=g.sponsor ORDER BY g.project_name""")
        pages = cur.fetchall()
    changed = {(c["state"], cig.norm_name(c["name"])): c["detail"] for c in changes if c["kind"] != "removed"}
    out = []
    for name, sponsor, lname, state, url, pdf, archived in pages:
        archived = [a.split("|", 1) for a in (archived or [])]
        n = len(archived)
        # Have we archived the file FTA's page links? Same link, or the same file name (links are URL-encoded).
        have_pdf = bool(pdf) and any(u == pdf or f == unquote(pdf.rsplit("/", 1)[-1]) for u, f in archived)
        why = []
        if (state, cig.norm_name(lname or "")) in changed:
            why.append(changed[(state, cig.norm_name(lname or ""))])
        if pdf and not have_pdf:
            why.append("FTA's page links a file not yet archived: " + pdf.rsplit("/", 1)[-1])
        if not n:
            why.append("no archived version")
        if why:
            out.append({"project_name": name, "sponsor": sponsor, "profile_url": url, "pdf_url": pdf, "why": "; ".join(why)})
    return out


def load_listing(conn, page, source="upload"):
    """Store each current project's profile_url from a listing page, keep the listing as a snapshot, and
    report what changed since the previous one and which profiles to download. Returns a coverage report."""
    import json
    create_tables(conn)
    rows = parse_listing(page)
    if not rows:
        raise ValueError("No project table found. Save FTA's Current CIG Projects page (Ctrl+S) and load that file.")
    rows_json = json.dumps(sorted(rows, key=lambda r: (r["state"], r["name"])), sort_keys=True)
    rows_sha = hashlib.sha256(rows_json.encode()).hexdigest()
    with conn.cursor() as cur:
        cur.execute("SELECT id, to_char(captured_at AT TIME ZONE 'America/New_York','YYYY-MM-DD HH24:MI'), rows_sha256, rows "
                    "FROM cig_profile_listings ORDER BY id DESC LIMIT 1")
        prev = cur.fetchone()
    changes = listing_changes(prev[3], rows) if prev else []
    if not prev or prev[2] != rows_sha:  # a listing identical to the last one isn't stored again
        with conn.cursor() as cur:
            cur.execute("INSERT INTO cig_profile_listings (source, rows_sha256, rows, changes) VALUES (%s,%s,%s::jsonb,%s::jsonb)",
                        (source, rows_sha, rows_json, json.dumps(changes)))
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
            "compared_with": prev[1] if prev else None, "changes": changes, "to_download": to_download(conn, changes),
            "renamed": [f"{p[0]} <- {r['name']}" for p, r in pairs if cig.norm_name(p[0]) != cig.norm_name(r["name"])],
            "listed_not_in_dashboard": [f"{r['state']} {r['name']}" for r in extra],
            "projects_not_listed": [f"{p[2]} {p[0]}" for p in missing]}


def load_pdf_links(conn, links):
    """Record the PDF each project's FTA page links to ({profile_url: pdf_url}, e.g. read in a browser),
    so to_download() can tell which profiles FTA has replaced. Returns to_download()."""
    from urllib.parse import urlparse
    create_tables(conn)
    with conn.cursor() as cur:
        for page, pdf in links.items():
            if pdf and urlparse(pdf).hostname not in FTA_HOSTS:
                continue
            cur.execute("UPDATE cig_profile_pages SET pdf_url=%s, pdf_checked_at=now() WHERE profile_url=%s", (pdf, page))
    conn.commit()
    return to_download(conn)


# ---- Step 3: archive versions ---------------------------------------------------------------------
def pdf_text(path):
    import pdfplumber
    with pdfplumber.open(path) as pdf:
        return "\n".join((p.extract_text() or "") for p in pdf.pages)


def normalize(text):
    """Text for hashing and diffing: Unicode-normalized, whitespace collapsed, blank lines and bare
    page numbers dropped, so re-exports of the same content hash the same."""
    import unicodedata
    out = []
    for line in unicodedata.normalize("NFKC", text or "").splitlines():
        line = re.sub(r"\s+", " ", line).strip()
        if not line or re.fullmatch(r"(page\s*)?\d{1,3}(\s*of\s*\d{1,3})?", line, re.I):
            continue
        out.append(line)
    return "\n".join(out)


def slugify(*parts):
    s = re.sub(r"[^a-z0-9]+", "-", " ".join(p for p in parts if p).lower()).strip("-")
    return s[:80] or "project"


def fta_date(file_name):
    """The date FTA put in a profile's file name (e.g. ...-AR27-04-07-26.pdf -> 2026-04-07), or None."""
    # The last valid M-D-Y in the name: "AR26-11-19-25" also contains "26-11-19" (cycle AR26), which isn't one.
    s, found = file_name or "", None
    for i in range(len(s)):
        m = re.match(r"(\d{1,2})-(\d{1,2})-(\d{4}|\d{2})(?!\d)", s[i:])
        if m and (i == 0 or not s[i - 1].isdigit()):
            mo, d, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
            y = y + 2000 if y < 100 else y
            if 1 <= mo <= 12 and 1 <= d <= 31:
                found = f"{y:04d}-{mo:02d}-{d:02d}"
    return found


def find_project(path, projects):
    """The single current project a profile PDF belongs to (its title + state), else (None, reason)."""
    prof = cig.read_profile(path)
    hits = [p for p in projects if cig.match_profile(p[0], p[2], [prof])]
    if len(hits) == 1:
        return hits[0], prof["title"]
    return None, ("matches several projects: " + ", ".join(h[0] for h in hits) if hits
                  else f"no current project matches “{prof['title']}”")


def ingest(conn, data, file_name, source, project=None, pdf_url=None, projects=None):
    """Archive one profile PDF (bytes). A version is stored only when its normalized text differs from
    every stored version of that project; returns {status: new|changed|unchanged|unmatched|unreadable}."""
    import tempfile
    create_tables(conn)
    if not data.startswith(b"%PDF"):
        return {"status": "unreadable", "file": file_name, "message": "not a PDF"}
    with tempfile.NamedTemporaryFile(suffix=".pdf") as tmp:
        tmp.write(data)
        tmp.flush()
        title = None
        if project is None:
            project, title = find_project(tmp.name, projects if projects is not None else current_projects(conn))
            if project is None:
                return {"status": "unmatched", "file": file_name, "message": title}
        text = normalize(pdf_text(tmp.name))
    if len(text) < 200:
        return {"status": "unreadable", "file": file_name, "message": "no text in the PDF (a scan?)"}
    name, sponsor, state = project[0], project[1] or "", project[2]
    th, fh = hashlib.sha256(text.encode()).hexdigest(), hashlib.sha256(data).hexdigest()
    with conn.cursor() as cur:
        cur.execute("SELECT id, text_sha256, text FROM cig_profile_versions WHERE project_name=%s AND sponsor=%s "
                    "ORDER BY captured_at DESC, id DESC", (name, sponsor))
        versions = cur.fetchall()
        same = next((v for v in versions if v[1] == th), None)
        if same:
            return {"status": "unchanged", "file": file_name, "project_name": name, "sponsor": sponsor,
                    "version_id": same[0], "is_latest": same is versions[0]}
        prev = versions[0] if versions else None
        cur.execute("SELECT profile_url FROM cig_profile_pages WHERE project_name=%s AND sponsor=%s", (name, sponsor))
        row = cur.fetchone()
        captured = datetime.now(timezone.utc)
        folder = os.path.join(ARCHIVE_DIR, slugify(state, name))
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, captured.strftime("%Y-%m-%dT%H%M%S") + ".pdf")
        with open(path, "wb") as out:
            out.write(data)
        diff, added, removed = make_diff(prev[2], text) if prev else (None, None, None)
        cur.execute("""INSERT INTO cig_profile_versions (project_name, sponsor, profile_url, captured_at, text_sha256,
                         file_sha256, file_path, file_name, file_bytes, pdf_url, source, fta_date, changed,
                         prev_version_id, text, diff, lines_added, lines_removed)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                    (name, sponsor, row[0] if row else None, captured, th, fh, path, file_name, len(data), pdf_url,
                     source, fta_date(file_name), prev is not None, prev[0] if prev else None, text, diff,
                     added, removed))
        vid = cur.fetchone()[0]
    conn.commit()
    return {"status": "changed" if prev else "new", "file": file_name, "project_name": name, "sponsor": sponsor,
            "version_id": vid, "lines_added": added, "lines_removed": removed}


MAX_DIFF_LINES = 3000  # a full rewrite is summarized by its counts rather than stored line by line


def make_diff(old, new):
    """(unified diff, lines added, lines removed) between two normalized texts, one line of context."""
    import difflib
    lines = list(difflib.unified_diff((old or "").splitlines(), (new or "").splitlines(),
                                      "previous", "this version", n=1, lineterm=""))
    added = sum(1 for l in lines if l.startswith("+") and not l.startswith("+++"))
    removed = sum(1 for l in lines if l.startswith("-") and not l.startswith("---"))
    if len(lines) > MAX_DIFF_LINES:
        lines = lines[:MAX_DIFF_LINES] + [f"... diff truncated ({added} lines added, {removed} removed in all)"]
    return "\n".join(lines), added, removed


def rediff(conn):
    """Recompute every stored diff (e.g. after changing normalize()). Returns the number updated."""
    with conn.cursor() as cur:
        cur.execute("SELECT v.id, p.text, v.text FROM cig_profile_versions v "
                    "JOIN cig_profile_versions p ON p.id = v.prev_version_id")
        rows = cur.fetchall()
        for vid, old, new in rows:
            d, a, r = make_diff(old, new)
            cur.execute("UPDATE cig_profile_versions SET diff=%s, lines_added=%s, lines_removed=%s WHERE id=%s",
                        (d, a, r, vid))
    conn.commit()
    return len(rows)


def record_run(conn, trigger, listing_status, results, message=None):
    n = lambda s: sum(1 for r in results if r["status"] == s)
    with conn.cursor() as cur:
        cur.execute("INSERT INTO cig_profile_runs (trigger, listing_status, files, new_versions, changed, unchanged, "
                    "unmatched, message) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                    (trigger, listing_status, len(results), n("new") + n("changed"), n("changed"), n("unchanged"),
                     [f"{r['file']}: {r.get('message', '')}" for r in results if r["status"] in ("unmatched", "unreadable")],
                     message))
    conn.commit()


def seed(conn):
    """Baseline versions from the profile PDFs already kept by cig.py --profiles (DATA_DIR/cig/profiles),
    each attached to the project it was matched to (cig_projects.profile_file)."""
    with conn.cursor() as cur:
        cur.execute("SELECT project_name, coalesce(sponsor,''), state, profile_file FROM cig_projects "
                    "WHERE snapshot_date=(SELECT max(snapshot_date) FROM cig_projects) AND profile_file IS NOT NULL")
        rows = cur.fetchall()
    results = []
    for name, sponsor, state, f in rows:
        path = cig.profile_path(f)
        if not path:
            results.append({"status": "unreadable", "file": f, "message": "kept copy not found"})
            continue
        with open(path, "rb") as fh:
            results.append(ingest(conn, fh.read(), f, "seed", project=(name, sponsor, state)))
    record_run(conn, "seed", None, results)
    return results


def process_inbox(conn, trigger="inbox"):
    """Archive every PDF in the inbox (and load a saved listing page if one is there). Files are moved
    to inbox/processed, or inbox/unmatched when no single project matches (upload those on the Grants
    tab and pick the project)."""
    import shutil
    os.makedirs(INBOX_DIR, exist_ok=True)
    results, listing = [], None
    projects = current_projects(conn)
    for f in sorted(os.listdir(INBOX_DIR)):
        src = os.path.join(INBOX_DIR, f)
        if not os.path.isfile(src):
            continue
        low = f.lower()
        if low.endswith((".html", ".htm")):
            with open(src, encoding="utf-8", errors="replace") as fh:
                try:
                    listing = load_listing(conn, fh.read())
                    projects = current_projects(conn)
                    dest = "processed"
                except ValueError as e:
                    results.append({"status": "unreadable", "file": f, "message": str(e)})
                    dest = "unmatched"
        elif low.endswith(".pdf"):
            with open(src, "rb") as fh:
                r = ingest(conn, fh.read(), f, trigger, projects=projects)
            results.append(r)
            dest = "unmatched" if r["status"] in ("unmatched", "unreadable") else "processed"
        else:
            continue
        os.makedirs(os.path.join(INBOX_DIR, dest), exist_ok=True)
        shutil.move(src, os.path.join(INBOX_DIR, dest, f))
    return results, listing


def fetch_from_fta(conn, pause=3.0):
    """One honest try of FTA's listing page. transit.dot.gov currently answers 403 (it blocks automated
    access), and then nothing else is requested. If it's ever served, each project's profile page is
    read for its PDF link, one request every few seconds, stopping at the first refusal; a PDF whose
    link matches the latest archived version is not downloaded again."""
    import time
    from urllib.parse import urljoin, urlparse
    import requests
    try:
        r = requests.get(LISTING_URL, headers=UA, timeout=30, allow_redirects=False)
    except requests.RequestException as e:
        return f"listing unreachable ({type(e).__name__})", []
    if r.status_code != 200:
        return f"listing HTTP {r.status_code}" + (" (FTA blocks automated access)" if r.status_code == 403 else ""), []
    load_listing(conn, r.text)
    with conn.cursor() as cur:
        cur.execute("""SELECT g.project_name, g.sponsor, g.state, g.profile_url,
                         (SELECT pdf_url FROM cig_profile_versions v WHERE v.project_name=g.project_name
                            AND v.sponsor=g.sponsor ORDER BY captured_at DESC, id DESC LIMIT 1)
                       FROM cig_profile_pages g WHERE g.profile_url IS NOT NULL""")
        pages = cur.fetchall()
    results = []
    for name, sponsor, state, url, last_pdf in pages:
        if urlparse(url).hostname not in FTA_HOSTS:
            continue
        time.sleep(pause)
        p = requests.get(url, headers=UA, timeout=30, allow_redirects=False)
        if p.status_code != 200:
            return f"listing ok; profile pages HTTP {p.status_code} (stopped)", results
        links = [urljoin(url, h) for h in re.findall(r'href="([^"]+\.pdf)"', p.text, re.I)]
        pdf = next((u for u in links if urlparse(u).hostname in FTA_HOSTS), None)
        if not pdf or pdf == last_pdf:
            continue  # no PDF link, or the same file we already have
        time.sleep(pause)
        d = requests.get(pdf, headers=UA, timeout=60, allow_redirects=False)
        if d.status_code != 200:
            return f"listing ok; profile PDFs HTTP {d.status_code} (stopped)", results
        results.append(ingest(conn, d.content, pdf.rsplit("/", 1)[-1], "fta", project=(name, sponsor, state), pdf_url=pdf))
    return "listing ok; profiles checked", results


def weekly(conn, trigger="weekly"):
    """The scheduled check: the inbox, then one polite try of FTA's site. Recorded in cig_profile_runs."""
    create_tables(conn)
    results, listing = process_inbox(conn, trigger)
    status, fetched = fetch_from_fta(conn)
    results += fetched
    record_run(conn, trigger, status, results,
               f"listing loaded from inbox: {listing['matched']} of {listing['projects']} matched" if listing else None)
    return {"listing_status": status, "results": results, "listing": listing}


def _print(results):
    for r in results:
        print(f"  {r['status']:10} {r.get('project_name') or '-':50.50} {r.get('file', '')}"
              + (f"  ({r['message']})" if r.get("message") else ""))
    counts = {}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    print("  totals:", counts)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--listing", metavar="HTML", help="profile links from a saved copy of FTA's listing page")
    ap.add_argument("--seed", action="store_true", help="baseline versions from the PDFs kept by cig.py --profiles")
    ap.add_argument("--ingest", nargs="+", metavar="PDF", help="archive these profile PDFs")
    ap.add_argument("--weekly", action="store_true", help="inbox + one polite try of FTA's listing page")
    ap.add_argument("--rediff", action="store_true", help="recompute every stored diff")
    ap.add_argument("--pdf-links", metavar="JSON", help="{profile page url: pdf url} read from FTA's pages in a browser")
    ap.add_argument("--to-download", action="store_true", help="list the profiles worth downloading")
    a = ap.parse_args()
    import psycopg
    dsn = os.environ.get("DATABASE_URL", "postgresql://transit411:transit411@db:5432/transit411")
    with psycopg.connect(dsn) as conn:
        create_tables(conn)
        if a.listing:
            with open(a.listing, encoding="utf-8", errors="replace") as fh:
                rep = load_listing(conn, fh.read())
            for k, v in rep.items():
                if isinstance(v, list) and v and isinstance(v[0], dict):
                    print(f"{k}: {len(v)}")
                    for x in v:
                        print("   ", x.get("kind") or x.get("project_name"), "|", x.get("name") or "", x.get("detail") or x.get("why"))
                else:
                    print(f"{k}: {v}")
        if a.pdf_links:
            import json
            with open(a.pdf_links, encoding="utf-8") as fh:
                load_pdf_links(conn, json.load(fh))
            a.to_download = True
        if a.to_download:
            todo = to_download(conn)
            print(f"TO DOWNLOAD: {len(todo)}")
            for x in todo:
                print(f"   {x['project_name'][:48]:48} {x['why']}")
        if a.seed:
            print("SEED")
            _print(seed(conn))
        if a.ingest:
            projects = current_projects(conn)
            res = []
            for f in a.ingest:
                with open(f, "rb") as fh:
                    res.append(ingest(conn, fh.read(), os.path.basename(f), "command line", projects=projects))
            record_run(conn, "command line", None, res)
            _print(res)
        if a.weekly:
            out = weekly(conn, "command line")
            print("listing:", out["listing_status"])
            _print(out["results"])
        if a.rediff:
            print("diffs recomputed:", rediff(conn))


if __name__ == "__main__":
    main()
