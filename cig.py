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


# ---- Load history: every dashboard load (or refusal) and a kept copy of its PDF ------------------
PDF_DIR = os.environ.get("CIG_PDF_DIR", "/data/cig")


def create_loads_table(conn):
    with conn.cursor() as cur:
        cur.execute("""CREATE TABLE IF NOT EXISTS cig_loads (
            id BIGSERIAL PRIMARY KEY, loaded_at TIMESTAMPTZ DEFAULT now(), snapshot_date DATE,
            source TEXT, name TEXT, status TEXT, projects INTEGER, message TEXT,
            file_path TEXT, file_bytes INTEGER, sha256 TEXT)""")
    conn.commit()


def keep_pdf(body, snap):
    """Save a copy of the dashboard PDF as <snapshot>_<hash>.pdf in PDF_DIR. Returns (path, sha256),
    or (None, sha256) if the folder isn't available (the load still goes ahead)."""
    import hashlib
    sha = hashlib.sha256(body).hexdigest()
    try:
        os.makedirs(PDF_DIR, exist_ok=True)
        path = os.path.join(PDF_DIR, f"{snap or 'undated'}_{sha[:10]}.pdf")
        if not os.path.exists(path):
            with open(path, "wb") as f:
                f.write(body)
        return path, sha
    except OSError:
        return None, sha


def record_load(conn, snap, source, name, status, projects=None, message=None, body=None):
    """Log a load attempt in cig_loads, keeping the PDF when it loaded. Never raises: history
    bookkeeping must not break a load."""
    try:
        path, sha = keep_pdf(body, snap) if (body and status == "loaded") else (None, None)
        create_loads_table(conn)
        with conn.cursor() as cur:
            cur.execute("INSERT INTO cig_loads (snapshot_date, source, name, status, projects, message, file_path, "
                        "file_bytes, sha256) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                        (snap, source, (name or "")[:500], status, projects, (message or "")[:500] or None,
                         path, len(body) if body else None, sha))
        conn.commit()
    except Exception as e:
        print(f"(could not record load history: {type(e).__name__}: {e})")


# ---- Month-over-month changes ---------------------------------------------------------------------
PHASE_ORDER = {"PD": 1, "Eng": 2, "FFGA": 3, "CGA": 3, "Const": 4}
CHANGE_FIELDS = [("phase", "phase"), ("rating", "rating"), ("cost_musd", "cost"), ("cig_request_musd", "CIG request"),
                 ("cig_share", "CIG share"), ("est_grant", "est. grant"), ("noncig_status", "local match")]


def snapshots(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('cig_projects') IS NOT NULL")
        if not cur.fetchone()[0]:
            return []
        cur.execute("SELECT snapshot_date, count(*) FROM cig_projects GROUP BY 1 ORDER BY 1 DESC")
        return [(d.isoformat(), n) for d, n in cur.fetchall()]


def changes(conn, to_snap=None, from_snap=None):
    """Differences between two snapshots, matched on (project_name, sponsor): new and dropped projects,
    and per-field changes. Defaults: latest snapshot vs the one before it. A project FTA renames shows
    up as one dropped + one new."""
    snaps = [s for s, _ in snapshots(conn)]
    if len(snaps) < 2:
        return {"snapshots": snaps, "from": None, "to": snaps[0] if snaps else None, "changes": []}
    to_snap = to_snap if to_snap in snaps else snaps[0]
    older = [s for s in snaps if s < to_snap]
    from_snap = from_snap if from_snap in older else (older[0] if older else None)
    if not from_snap:
        return {"snapshots": snaps, "from": None, "to": to_snap, "changes": []}
    cols = ["project_name", "sponsor", "city", "state", "mode"] + [f for f, _ in CHANGE_FIELDS]
    rows = {}
    with conn.cursor() as cur:
        for snap in (from_snap, to_snap):
            cur.execute(f"SELECT {', '.join(cols)} FROM cig_projects WHERE snapshot_date=%s", (snap,))
            rows[snap] = {(r[0], r[1]): dict(zip(cols, r)) for r in cur.fetchall()}
    a, b = rows[from_snap], rows[to_snap]
    num = lambda v: float(v) if v is not None else None
    out = []
    for key in sorted(set(a) | set(b), key=lambda k: (k[0] or "").lower()):
        pa, pb = a.get(key), b.get(key)
        p = pb or pa
        base = {"project_name": p["project_name"], "sponsor": p["sponsor"], "state": p["state"], "mode": p["mode"]}
        if pa is None:
            out.append({**base, "type": "new", "detail": {"phase": pb["phase"], "cig_request_musd": num(pb["cig_request_musd"])}})
            continue
        if pb is None:
            out.append({**base, "type": "dropped", "detail": {"phase": pa["phase"], "cig_request_musd": num(pa["cig_request_musd"])}})
            continue
        for field, label in CHANGE_FIELDS:
            va, vb = pa[field], pb[field]
            if field.endswith("_musd"):
                va, vb = num(va), num(vb)
            if va == vb:
                continue
            ch = {**base, "type": field, "label": label, "before": va, "after": vb}
            if field == "phase":
                oa, ob = PHASE_ORDER.get(va or ""), PHASE_ORDER.get(vb or "")
                ch["direction"] = "advanced" if (oa and ob and ob > oa) else "back" if (oa and ob and ob < oa) else None
            if field.endswith("_musd") and va is not None and vb is not None:
                ch["delta"] = round(vb - va, 1)
                ch["pct"] = round((vb - va) / va * 100, 1) if va else None
            out.append(ch)
    order = {"new": 0, "dropped": 1, "phase": 2, "rating": 3, "est_grant": 4, "cig_request_musd": 5,
             "cost_musd": 6, "cig_share": 7, "noncig_status": 8}
    out.sort(key=lambda c: (order.get(c["type"], 9), (c["project_name"] or "").lower()))
    return {"snapshots": snaps, "from": from_snap, "to": to_snap, "changes": out}


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
    with open(path, "rb") as f:
        body = f.read()
    with psycopg.connect(dsn) as conn:
        try:
            n = load(conn, rows, snap)
        except SystemExit as e:  # the too-few-rows safeguard: log the refusal, then report it
            conn.rollback()
            record_load(conn, snap, "command line", src, "refused", len(rows), str(e))
            raise
        record_load(conn, snap, "command line", src, "loaded", n, body=body)
    print(f"Loaded {n} projects as snapshot {snap} (other snapshots preserved).")


if __name__ == "__main__":
    main()
