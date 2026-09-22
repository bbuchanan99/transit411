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
  python cig.py --profiles DIR        # build reference/cig_modes.json from FTA project profile PDFs
  python cig.py --remode              # re-apply mode + profile links to every stored snapshot

Mode comes only from FTA sources (see assign_mode): the dashboard's exclusive-BRT column, then the
profile's "Proposed Project:" field; otherwise NULL (Unspecified).
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


# ---- Mode, from FTA sources only ---------------------------------------------------------------
# The dashboard has no mode column. Mode comes from (1) the dashboard's "Length of Exclusive BRT"
# column (a number or TBD => BRT; N/A => not BRT) and (2) the "Proposed Project: <mode>" field of
# FTA's project profile PDFs (reference/cig_modes.json, built by --profiles). If neither states it,
# or they disagree, mode is NULL ("Unspecified"). No guessing from project names.
REF_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reference")
MODES_FILE = os.environ.get("CIG_MODES_FILE", os.path.join(REF_DIR, "cig_modes.json"))
STATE_CODES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA", "colorado": "CO",
    "connecticut": "CT", "delaware": "DE", "district of columbia": "DC", "washington, dc": "DC", "florida": "FL",
    "georgia": "GA", "hawaii": "HI", "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD", "massachusetts": "MA", "michigan": "MI",
    "minnesota": "MN", "mississippi": "MS", "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY", "north carolina": "NC",
    "north dakota": "ND", "ohio": "OH", "oklahoma": "OK", "oregon": "OR", "pennsylvania": "PA", "puerto rico": "PR",
    "rhode island": "RI", "south carolina": "SC", "south dakota": "SD", "tennessee": "TN", "texas": "TX",
    "utah": "UT", "vermont": "VT", "virginia": "VA", "washington": "WA", "west virginia": "WV", "wisconsin": "WI",
    "wyoming": "WY",
}


def canon_mode(raw):
    """A profile's 'Proposed Project:' value -> a standard mode, or None if it isn't one we recognize."""
    s = (raw or "").lower()
    for key, mode in (("bus rapid", "BRT"), ("brt", "BRT"), ("light rail", "Light Rail"), ("heavy rail", "Heavy Rail"),
                      ("commuter rail", "Commuter Rail"), ("streetcar", "Streetcar")):
        if re.search(r"\b" + key + r"\b", s):
            return mode
    return None


def norm_name(s):
    """Project-name key for matching dashboard rows to profiles."""
    s = (s or "").lower().replace("&", " and ")
    s = re.sub(r"\([^)]*\)", " ", s)
    s = re.sub(r"\bbus rapid transit\b", "brt", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    s = re.sub(r"\b(the|project|program)\b", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _states_in(place):
    """'Tacoma, Washington' -> {'WA'}; 'Portland, Oregon and Vancouver, Washington' -> {'OR','WA'}."""
    p = (place or "").lower()
    found = {code for name, code in STATE_CODES.items() if re.search(r"\b" + re.escape(name) + r"\b", p)}
    if "washington, dc" in p or "district of columbia" in p:
        found.discard("WA")
    return found


def read_profile(path):
    """One FTA project profile PDF -> title, place, program, states, stated mode (if any), and
    whether its text mentions rail vs. BRT (used only to detect conflicts, never to assign a mode)."""
    import pdfplumber
    with pdfplumber.open(path) as pdf:
        text = "\n".join((p.extract_text() or "") for p in pdf.pages[:2])
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    title = lines[0] if lines else os.path.basename(path)
    place = lines[1] if len(lines) > 1 else ""
    program = next((l for l in lines[2:5] if re.search(r"\b(Starts|Capacity)\b", l)), "")
    m = re.search(r"Proposed Project:\s*(.+)", text)
    low = text.lower()
    return {
        "title": title, "place": place, "program": program, "states": sorted(_states_in(place)),
        "key": norm_name(title), "mode_field": m.group(1).strip() if m else None,
        "mode": canon_mode(m.group(1)) if m else None,
        "mentions_rail": bool(re.search(r"\b(light rail|heavy rail|commuter rail|streetcar|metrorail|subway)\b", low)),
        "mentions_brt": bool(re.search(r"\b(bus rapid transit|brt)\b", low)),
    }


def build_modes(src_dir, out_path=MODES_FILE, keep_dir=None):
    """Read every profile PDF in src_dir into the committed lookup (reference/cig_modes.json), and
    copy the PDFs into keep_dir (served as each project's 'FTA project profile' link)."""
    import glob
    import hashlib
    import json
    import shutil
    keep_dir = keep_dir or os.path.join(PDF_DIR, "profiles")
    os.makedirs(keep_dir, exist_ok=True)
    out = []
    for f in sorted(glob.glob(os.path.join(src_dir, "*.pdf"))):
        prof = read_profile(f)
        name = os.path.basename(f)
        with open(f, "rb") as fh:
            prof["sha256"] = hashlib.sha256(fh.read()).hexdigest()
        prof["file"] = name
        shutil.copyfile(f, os.path.join(keep_dir, name))
        out.append(prof)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump({"generated": datetime.utcnow().strftime("%Y-%m-%d"), "source":
                   "FTA CIG project profile PDFs (Proposed Project field)", "profiles": out}, fh, indent=1)
    return out


_profiles_cache = None


def load_profiles():
    global _profiles_cache
    if _profiles_cache is None:
        import json
        try:
            with open(MODES_FILE, encoding="utf-8") as fh:
                _profiles_cache = json.load(fh).get("profiles", [])
        except (OSError, ValueError):
            _profiles_cache = []
    return _profiles_cache


def match_profile(name, state, profiles=None):
    """The profile for a dashboard project: same normalized name (or a close one) in the same state."""
    from difflib import SequenceMatcher
    profiles = load_profiles() if profiles is None else profiles
    key = norm_name(name)
    states = set((state or "").split("-"))
    same_state = [p for p in profiles if not p["states"] or states & set(p["states"])]
    exact = [p for p in same_state if p["key"] == key]
    if exact:
        return exact[0]
    # One name's distinctive words all appear in the other (e.g. "BART Silicon Valley Phase II" vs
    # "... Phase II Extension Project", "Veirs Mill Road Flash BRT" vs "Veirs Mill Road BRT"), and
    # only one profile in the state qualifies.
    filler = {"extension", "corridor", "flash", "new", "starts"}
    words = set(key.split()) - filler
    contained = [p for p in same_state
                 if words and (set(p["key"].split()) - filler) and
                 (words <= set(p["key"].split()) - filler or set(p["key"].split()) - filler <= words)]
    if len(contained) == 1:
        return contained[0]
    scored = sorted(((SequenceMatcher(None, key, p["key"]).ratio(), p) for p in same_state), key=lambda t: -t[0])
    if scored and scored[0][0] >= 0.88 and (len(scored) == 1 or scored[0][0] - scored[1][0] >= 0.05):
        return scored[0][1]
    return None


def assign_mode(excl_brt, profile):
    """(mode, source) from FTA sources only; mode None means Unspecified."""
    col = (excl_brt or "").strip().upper()
    col_says_brt = col not in ("", "N/A")
    pmode = profile["mode"] if profile else None
    if col_says_brt:
        if pmode and pmode != "BRT":
            return None, f"conflict: dashboard BRT column vs profile ({pmode})"
        if profile and not pmode and profile["mentions_rail"]:
            # e.g. Interstate Bridge Replacement: light rail AND bus-on-shoulder BRT. One mode can't say it.
            return None, "conflict: dashboard BRT column but the profile also describes a rail mode"
        return "BRT", "FTA dashboard (Length of Exclusive BRT)"
    if pmode:
        return pmode, "FTA project profile"
    return None, "not stated by FTA sources"


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


MILESTONE_COLS = ["lonp_req", "lonp_dec", "lonp_action", "req_rating_date", "proj_rating_date",
                  # mode provenance: the raw BRT-column value, where mode came from, the linked profile PDF
                  "excl_brt", "mode_source", "profile_file"]


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
            prof = match_profile(r["name"], r["state"])
            mode, mode_source = assign_mode(r["excl_brt"], prof)
            cur.execute(
                "INSERT INTO cig_projects (snapshot_date, project_name, sponsor, city, state, mode, phase, "
                "length_mi, stations, cost_musd, cost_raw, cig_request_musd, cig_request_raw, cig_share, "
                "rating, noncig_status, est_grant, nepa, pd_entry, eng_entry, lonp_req, lonp_dec, "
                "lonp_action, req_rating_date, proj_rating_date, excl_brt, mode_source, profile_file) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (snap, r["name"], r["sponsor"], r["city"], r["state"], mode, r["phase"],
                 r["length"], r["stations"], clean_num(r["cost"]), r["cost"] or None,
                 clean_num(r["cig_request"]), r["cig_request"] or None, r["cig_share"] or None,
                 r["rating"] or None, r["noncig_status"] or None, r["est_grant"] or None,
                 r["nepa"] or None, r["pd_entry"] or None, r["eng_entry"] or None,
                 r["lonp_req"] or None, r["lonp_dec"] or None, r["lonp_action"] or None,
                 r["req_rating_date"] or None, r["proj_rating_date"] or None,
                 r["excl_brt"] or None, mode_source, prof["file"] if prof else None))
    conn.commit()
    return len(unique)


def remode(conn, kept_pdfs=()):
    """Re-apply mode + profile links to every stored snapshot (e.g. after rebuilding cig_modes.json).
    Rows loaded before excl_brt was stored get it from a kept dashboard PDF of the same snapshot, or
    from the same project in another snapshot (a project's BRT status doesn't change month to month)."""
    create_table(conn)
    with conn.cursor() as cur:
        for path in kept_pdfs:  # fill excl_brt from kept dashboard PDFs
            snap = date_in(os.path.basename(path))
            if not snap:
                continue
            for r in parse_pdf(path):
                cur.execute("UPDATE cig_projects SET excl_brt=%s WHERE snapshot_date=%s AND project_name=%s "
                            "AND sponsor=%s AND excl_brt IS NULL", (r["excl_brt"] or None, snap, r["name"], r["sponsor"]))
        cur.execute("""UPDATE cig_projects p SET excl_brt = q.excl_brt FROM (
                         SELECT DISTINCT ON (project_name, sponsor) project_name, sponsor, excl_brt FROM cig_projects
                         WHERE excl_brt IS NOT NULL ORDER BY project_name, sponsor, snapshot_date DESC) q
                       WHERE p.excl_brt IS NULL AND p.project_name = q.project_name AND p.sponsor = q.sponsor""")
        cur.execute("SELECT id, project_name, state, excl_brt FROM cig_projects")
        counts = {}
        for rid, name, state, excl in cur.fetchall():
            prof = match_profile(name, state)
            mode, src = assign_mode(excl, prof) if excl is not None or prof else (None, "not stated by FTA sources")
            cur.execute("UPDATE cig_projects SET mode=%s, mode_source=%s, profile_file=%s WHERE id=%s",
                        (mode, src, prof["file"] if prof else None, rid))
            counts[src.split(":")[0]] = counts.get(src.split(":")[0], 0) + 1
    conn.commit()
    return counts


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
    ap.add_argument("--profiles", metavar="DIR",
                    help="build the mode lookup (reference/cig_modes.json or --out) from FTA project profile PDFs "
                         "in DIR, and keep copies of them for the project links")
    ap.add_argument("--out", help="with --profiles: where to write the lookup JSON")
    ap.add_argument("--remode", action="store_true",
                    help="re-apply mode + profile links to every stored snapshot")
    a = ap.parse_args()
    if a.profiles:
        profs = build_modes(a.profiles, a.out or MODES_FILE)
        print(f"Profiles read: {len(profs)}; with a stated mode: {sum(1 for p in profs if p['mode'])}; "
              f"lookup written to {a.out or MODES_FILE}")
        if not a.remode:
            return
    if a.remode:
        import glob
        import psycopg
        global _profiles_cache
        if a.out:
            os.environ["CIG_MODES_FILE"] = a.out
            globals()["MODES_FILE"] = a.out
        _profiles_cache = None
        dsn = os.environ.get("DATABASE_URL", "postgresql://transit411:transit411@db:5432/transit411")
        with psycopg.connect(dsn) as conn:
            counts = remode(conn, sorted(glob.glob(os.path.join(PDF_DIR, "*.pdf"))))
        print("Mode sources across all stored rows:", counts)
        return
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
