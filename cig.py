#!/usr/bin/env python3
"""
Transit411 CIG pipeline ingester (versioned history).

Loads the FTA Capital Investment Grants (CIG) Dashboard into Postgres (cig_projects).
Each monthly snapshot is KEPT (keyed by snapshot_date), so the table becomes a time
series: phase advances, rating changes, cost drift and slipping grant dates are all
recoverable by comparing snapshots. Source: the monthly PDF at transit.dot.gov/CIG
(which blocks automated downloads, so usually loaded via the Grants tab's Upload
dashboard). Parser validated against the 2026-09-11 dashboard.

  python cig.py --latest              # find & download the newest dashboard (403 from transit.dot.gov today)
  python cig.py --file dash.pdf       # load a local PDF (snapshot date from its filename)
  python cig.py --url https://...pdf  # download & load a specific (e.g. archived) dashboard
"""
import argparse
import os
import re
from datetime import datetime

BOUNDS = [31, 263, 333, 413, 441, 462, 482, 503, 523, 593, 664, 708, 755, 803, 851,
          898, 946, 993, 1032, 1070, 1095, 1142, 1204]
FIELDS = ["name", "sponsor", "city", "state", "phase", "length", "stations", "excl_brt",
          "cost", "cig_request", "cig_share", "nepa", "pd_entry", "eng_entry", "lonp_req",
          "lonp_dec", "lonp_action", "req_rating_date", "proj_rating_date", "rating",
          "noncig_status", "est_grant"]
PHASES = {"PD", "Eng", "Const", "FFGA", "CGA"}
CIG_URL = "https://www.transit.dot.gov/CIG"


def _binidx(x):
    for i in range(len(BOUNDS) - 1):
        if BOUNDS[i] <= x < BOUNDS[i + 1]:
            return i
    return None


def _cluster(words, tol=4):
    words = sorted(words, key=lambda w: (w["top"], w["x0"]))
    lines, cur, anchor = [], [], None
    for w in words:
        if anchor is None or abs(w["top"] - anchor) <= tol:
            cur.append(w); anchor = w["top"] if anchor is None else anchor
        else:
            lines.append(cur); cur = [w]; anchor = w["top"]
    if cur:
        lines.append(cur)
    return lines


def parse_pdf(path):
    import pdfplumber
    rows = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            for line in _cluster(page.extract_words(use_text_flow=True)):
                cells = [""] * (len(BOUNDS) - 1)
                for w in line:
                    i = _binidx(w["x0"])
                    if i is not None and w["text"] != "$":
                        cells[i] = (cells[i] + " " + w["text"]).strip()
                rec = dict(zip(FIELDS, cells))
                if re.match(r"^[A-Z]{2}(-[A-Z]{2})?$", rec["state"]) and rec["phase"] in PHASES:
                    rows.append(rec)
    return rows


def clean_num(s):
    if not s:
        return None
    s = s.replace("$", "").replace(",", "").strip()
    if s.upper() in ("TBD", "N/A", "") or "-" in s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def derive_mode(rec):
    n = rec["name"].lower()
    if "streetcar" in n:
        return "Streetcar"
    if "commuter rail" in n:
        return "Commuter Rail"
    if "light rail" in n or "lrt" in n:
        return "Light Rail"
    if "heavy rail" in n or "subway" in n or "metrorail" in n:
        return "Heavy Rail"
    eb = (rec.get("excl_brt") or "").upper()
    if eb not in ("N/A", "TBD", "") or "brt" in n or "bus rapid" in n:
        return "BRT"
    if "rail" in n or "link" in n or "line" in n or "streetcar" in n:
        return "Rail"
    return None


def date_in(s):
    """The date in a dashboard file name or URL, as YYYY-MM-DD, or None. Handles both
    2026-09-11 and 09-11-2026 styles."""
    s = s or ""
    m = re.search(r"(20\d{2})-(\d{2})-(\d{2})", s)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    m = re.search(r"(\d{2})-(\d{2})-(20\d{2})", s)
    if m:
        return f"{m.group(3)}-{m.group(1)}-{m.group(2)}"
    return None


def snapshot_date(path_or_url):
    return date_in(path_or_url) or datetime.utcnow().strftime("%Y-%m-%d")


def find_latest_pdf():
    import requests
    r = requests.get(CIG_URL, timeout=30, headers={"User-Agent": "Transit411/1.0"})
    if r.status_code == 403:
        raise SystemExit(f"{CIG_URL} refused the request (HTTP 403 - the site blocks automated access).\n"
                         "Download the dashboard PDF in a browser and use Upload dashboard on the Command Center's\n"
                         "Grants tab (or: docker compose run --rm -v \"$PWD/<file>.pdf:/tmp/dash.pdf\" cig --file /tmp/dash.pdf)")
    r.raise_for_status()
    links = re.findall(r'href="([^"]+\.pdf)"', r.text, flags=re.I)
    dash = [l for l in links if "dashboard" in l.lower()]
    if not dash:
        raise SystemExit("No dashboard PDF link found on " + CIG_URL)
    # Newest by the date in the file name (a plain string sort puts 09-...-2025 after 03-...-2026).
    url = max(dash, key=lambda u: (date_in(u) or "", u))
    if url.startswith("/"):
        url = "https://www.transit.dot.gov" + url
    return url


def download(url, dest="/tmp/cig.pdf"):
    import requests
    r = requests.get(url, timeout=60, headers={"User-Agent": "Transit411/1.0"})
    if r.status_code == 403:
        raise SystemExit(f"{url} refused the request (HTTP 403 - the site blocks automated access). "
                         "Download it in a browser and use Upload dashboard on the Grants tab.")
    r.raise_for_status()
    open(dest, "wb").write(r.content)
    return dest


MILESTONE_COLS = ["lonp_req", "lonp_dec", "lonp_action", "req_rating_date", "proj_rating_date"]


def create_table(conn):
    with conn.cursor() as cur:
        cur.execute("""CREATE TABLE IF NOT EXISTS cig_projects (
            id BIGSERIAL PRIMARY KEY, snapshot_date DATE, project_name TEXT, sponsor TEXT,
            city TEXT, state TEXT, mode TEXT, phase TEXT, length_mi TEXT, stations TEXT,
            cost_musd NUMERIC, cost_raw TEXT, cig_request_musd NUMERIC, cig_request_raw TEXT,
            cig_share TEXT, rating TEXT, noncig_status TEXT, est_grant TEXT, nepa TEXT,
            pd_entry TEXT, eng_entry TEXT, lonp_req TEXT, lonp_dec TEXT, lonp_action TEXT,
            req_rating_date TEXT, proj_rating_date TEXT, fetched_at TIMESTAMPTZ DEFAULT now())""")
        # Tables created before milestone dates existed get the new columns.
        for col in MILESTONE_COLS:
            cur.execute(f"ALTER TABLE cig_projects ADD COLUMN IF NOT EXISTS {col} TEXT")
        cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS cig_uniq ON cig_projects (snapshot_date, project_name, sponsor)")
        cur.execute("CREATE INDEX IF NOT EXISTS cig_proj_idx ON cig_projects (project_name)")
    conn.commit()


MIN_ROWS = 20  # the dashboard lists far more; fewer means the PDF layout changed and parsing failed


def load(conn, rows, snap):
    create_table(conn)
    if len(rows) < MIN_ROWS:
        raise SystemExit(f"Only {len(rows)} projects parsed (expected {MIN_ROWS}+); the dashboard layout may have "
                         "changed. Existing cig_projects data was left unchanged.")
    # One row per project per snapshot (the unique key); keep the first if the PDF repeats one.
    seen, unique = set(), []
    for r in rows:
        k = (r["name"], r["sponsor"])
        if k not in seen:
            seen.add(k)
            unique.append(r)
    with conn.cursor() as cur:
        # Versioned: replace only this snapshot's rows and keep every other month, in one transaction
        # so a failure part-way leaves this snapshot as it was.
        cur.execute("DELETE FROM cig_projects WHERE snapshot_date=%s", (snap,))
        for r in unique:
            cur.execute(
                "INSERT INTO cig_projects (snapshot_date, project_name, sponsor, city, state, mode, phase, "
                "length_mi, stations, cost_musd, cost_raw, cig_request_musd, cig_request_raw, cig_share, "
                "rating, noncig_status, est_grant, nepa, pd_entry, eng_entry, lonp_req, lonp_dec, "
                "lonp_action, req_rating_date, proj_rating_date) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (snap, r["name"], r["sponsor"], r["city"], r["state"], derive_mode(r), r["phase"],
                 r["length"], r["stations"], clean_num(r["cost"]), r["cost"] or None,
                 clean_num(r["cig_request"]), r["cig_request"] or None, r["cig_share"] or None,
                 r["rating"] or None, r["noncig_status"] or None, r["est_grant"] or None,
                 r["nepa"] or None, r["pd_entry"] or None, r["eng_entry"] or None,
                 r["lonp_req"] or None, r["lonp_dec"] or None, r["lonp_action"] or None,
                 r["req_rating_date"] or None, r["proj_rating_date"] or None))
    conn.commit()
    return len(unique)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file")
    ap.add_argument("--url")
    ap.add_argument("--latest", action="store_true")
    a = ap.parse_args()
    if a.file:
        path, src = a.file, a.file
    elif a.url:
        path, src = download(a.url), a.url
    elif a.latest:
        url = find_latest_pdf()
        print("Latest dashboard:", url)
        path, src = download(url), url
    else:
        raise SystemExit("Use --latest, --url URL, or --file PATH")
    rows = parse_pdf(path)
    snap = snapshot_date(src)
    print(f"Parsed {len(rows)} projects (snapshot {snap}).")
    import psycopg
    dsn = os.environ.get("DATABASE_URL", "postgresql://transit411:transit411@db:5432/transit411")
    with psycopg.connect(dsn) as conn:
        n = load(conn, rows, snap)
    print(f"Loaded {n} projects as snapshot {snap} (other snapshots preserved).")


if __name__ == "__main__":
    main()
