#!/usr/bin/env python3
"""
Transit411 Command Center - the private home-base dashboard (LAN only).
  uvicorn command_center:app --host 0.0.0.0 --port 8080
Env: API_URL (default http://api:8000), DATABASE_URL (default postgresql://transit411:transit411@db:5432/transit411)
"""
import os
from typing import List, Optional
import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

try:
    import psycopg
except Exception:
    psycopg = None

API_URL = os.environ.get("API_URL", "http://api:8000")
DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://transit411:transit411@db:5432/transit411")
app = FastAPI(title="Transit411 Command Center")
# Shared component bundle (t411.js / t411.css), also used by the public site.
app.mount("/static", StaticFiles(directory=os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")),
          name="static")


@app.get("/api/status")
def status():
    out = {"api": {"ok": False}, "db": {"ok": False}}
    try:
        out["api"] = httpx.get(f"{API_URL}/health", timeout=5).json()
    except Exception as e:
        out["api"] = {"ok": False, "error": str(e)}
    if psycopg:
        try:
            with psycopg.connect(DATABASE_URL, connect_timeout=5) as c, c.cursor() as cur:
                cur.execute("SELECT count(*) FROM information_schema.tables WHERE table_schema='public'")
                out["db"] = {"ok": True, "tables": cur.fetchone()[0]}
        except Exception as e:
            out["db"] = {"ok": False, "error": str(e)}
    else:
        out["db"] = {"ok": False, "error": "psycopg not installed"}
    return out


class Turn(BaseModel):
    question: Optional[str] = None
    sql: Optional[str] = None


class Ask(BaseModel):
    question: str
    history: Optional[List[Turn]] = None


@app.post("/api/ask")
def ask(a: Ask):
    payload = {"question": a.question, "history": [t.model_dump() for t in (a.history or [])]}
    try:
        r = httpx.post(f"{API_URL}/ask", json=payload, timeout=60)
    except httpx.HTTPError as e:
        raise HTTPException(502, f"API unreachable: {e}")
    if r.status_code != 200:
        raise HTTPException(r.status_code, r.text)
    return r.json()


def _db():
    if not psycopg:
        raise HTTPException(500, "psycopg not installed")
    return psycopg.connect(DATABASE_URL, connect_timeout=5)


# Every searchable text field of an item, for the Collection tab's search box.
_SEARCH_TEXT = ("concat_ws(' ', headline, summary, source_name, pillar, state, array_to_string(agencies, ' '), "
                "array_to_string(tags, ' '), array_to_string(mode, ' '), array_to_string(programs, ' '))")


def _collection_where(status, q=None, pillar=None, agency=None, mode=None, program=None, state=None, tag=None):
    """WHERE clause + params for the Collection tab's filters. Every word of q must appear somewhere
    (case-insensitive); facet filters are exact matches on the stored values."""
    where, params = ["status=%s"], [status]
    for word in (q or "").split()[:8]:
        where.append(_SEARCH_TEXT + " ILIKE %s ESCAPE '\\'")
        params.append("%" + word.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")
    for col, val in (("pillar", pillar), ("state", state)):
        if val:
            where.append(f"{col}=%s"); params.append(val)
    for col, val in (("agencies", agency), ("mode", mode), ("programs", program), ("tags", tag)):
        if val:
            where.append(f"%s = ANY({col})"); params.append(val)
    return " AND ".join(where), params


@app.get("/api/collection")
def collection(status: str = "pending", q: Optional[str] = None, pillar: Optional[str] = None,
               agency: Optional[str] = None, mode: Optional[str] = None, program: Optional[str] = None,
               state: Optional[str] = None, tag: Optional[str] = None):
    from collection import freshness
    items = []
    where, params = _collection_where(status, q, pillar, agency, mode, program, state, tag)
    try:
        with _db() as c, c.cursor() as cur:
            cur.execute(
                "SELECT id, pillar, headline, summary, source_name, source_url, published, deadline, relevance, status, "
                "agencies, mode, programs, tags, state "
                f"FROM collected_items WHERE {where} ORDER BY collected_at DESC LIMIT 200", params)
            names = [d[0] for d in cur.description]
            for row in cur.fetchall():
                it = dict(zip(names, row))
                st, sc = freshness(it.get("published"), it.get("deadline"))
                it["fresh_status"], it["fresh_score"] = st, sc
                it["published"] = it["published"].isoformat() if it.get("published") else None
                it["deadline"] = it["deadline"].isoformat() if it.get("deadline") else None
                for k in ("agencies", "mode", "programs", "tags"):
                    it[k] = it.get(k) or []
                items.append(it)
            cur.execute(f"SELECT count(*) FROM collected_items WHERE {where}", params)
            matched = cur.fetchone()[0]
            cur.execute("SELECT status, count(*) FROM collected_items GROUP BY status")
            counts = {row[0]: row[1] for row in cur.fetchall()}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"DB error: {e}")
    items.sort(key=lambda x: x["fresh_score"], reverse=True)
    return {"items": items, "counts": counts, "matched": matched}


@app.get("/api/collection/facets")
def collection_facets(status: str = "pending"):
    """Values present in this status's items, with counts, for the Collection tab's filter menus."""
    out = {}
    try:
        with _db() as c, c.cursor() as cur:
            for key, col in (("pillar", "pillar"), ("state", "state")):
                cur.execute(f"SELECT {col}, count(*) FROM collected_items WHERE status=%s AND {col} IS NOT NULL "
                            f"AND {col} <> '' GROUP BY 1 ORDER BY 2 DESC, 1", (status,))
                out[key] = [{"value": v, "count": n} for v, n in cur.fetchall()]
            for key, col, limit in (("mode", "mode", 50), ("program", "programs", 50),
                                    ("agency", "agencies", 40), ("tag", "tags", 40)):
                cur.execute(f"SELECT v, count(*) FROM collected_items, unnest({col}) AS v WHERE status=%s "
                            f"GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT {limit}", (status,))
                out[key] = [{"value": v, "count": n} for v, n in cur.fetchall()]
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"DB error: {e}")
    return out


@app.post("/api/collection/{item_id}/{action}")
def collection_action(item_id: int, action: str):
    # Publishing goes through /api/publish/{id}, which also creates the post; no "publish" shortcut here.
    mapping = {"approve": "approved", "skip": "skipped", "reset": "pending"}
    if action not in mapping:
        raise HTTPException(400, "unknown action")
    try:
        with _db() as c, c.cursor() as cur:
            cur.execute("UPDATE collected_items SET status=%s WHERE id=%s", (mapping[action], item_id))
            # Moving a published item back (e.g. "Return to pending") takes its post off the site too.
            _ensure_posts_link(cur)
            cur.execute("UPDATE content_posts SET status='draft' WHERE item_id=%s AND status='published'",
                        (item_id,))
            took_down = cur.rowcount > 0
            c.commit()
    except Exception as e:
        raise HTTPException(502, f"DB error: {e}")
    if took_down:
        _request_site_rebuild("post taken down")
    return {"id": item_id, "status": mapping[action]}


# ---- Public site rebuilds (Cloudflare Pages deploy hook) ------------------------------------------
# The site is static, so published/unpublished posts show up after a rebuild. With CF_PAGES_DEPLOY_HOOK
# set (in .env; it's a secret URL), publish/unpublish request one, delayed REBUILD_DELAY seconds so a
# burst of publishes becomes a single build.
import threading  # noqa: E402

DEPLOY_HOOK = os.environ.get("CF_PAGES_DEPLOY_HOOK", "").strip()
DEPLOY_HOOK_PREFIX = "https://api.cloudflare.com/client/v4/pages/webhooks/deploy_hooks/"
REBUILD_DELAY = 60
_rebuild = {"pending_since": None, "reason": None, "last_at": None, "last_ok": None, "last_detail": None}
_rebuild_lock = threading.Lock()


def _hook_ok():
    return DEPLOY_HOOK.startswith(DEPLOY_HOOK_PREFIX)


def _fire_rebuild():
    from datetime import datetime, timezone
    ok, detail = False, None
    try:
        r = httpx.post(DEPLOY_HOOK, timeout=20)
        ok = r.status_code < 300
        detail = "build started" if ok else f"Cloudflare returned HTTP {r.status_code}"
    except httpx.HTTPError as e:
        detail = f"couldn't reach Cloudflare ({type(e).__name__})"
    with _rebuild_lock:
        _rebuild.update(pending_since=None, last_at=datetime.now(timezone.utc).isoformat(), last_ok=ok,
                        last_detail=detail)


def _request_site_rebuild(reason, delay=REBUILD_DELAY):
    """Schedule one rebuild (no-op if the hook isn't configured or one is already scheduled)."""
    from datetime import datetime, timezone
    if not _hook_ok():
        return False
    with _rebuild_lock:
        if _rebuild["pending_since"]:
            return True
        _rebuild.update(pending_since=datetime.now(timezone.utc).isoformat(), reason=reason)
    t = threading.Timer(delay, _fire_rebuild)
    t.daemon = True
    t.start()
    return True


def _rebuild_state():
    with _rebuild_lock:
        s = dict(_rebuild)
    s["configured"] = _hook_ok()
    s["misconfigured"] = bool(DEPLOY_HOOK) and not _hook_ok()
    s["delay_seconds"] = REBUILD_DELAY
    return s


@app.get("/api/site/rebuild")
def site_rebuild_status():
    return _rebuild_state()


@app.post("/api/site/rebuild")
def site_rebuild_now():
    """The Publish tab's "Rebuild site now" button (runs in a few seconds)."""
    if not _hook_ok():
        raise HTTPException(400, "Site rebuilds aren't set up: add CF_PAGES_DEPLOY_HOOK to .env on the NAS.")
    _request_site_rebuild("manual", delay=2)
    return _rebuild_state()


class Toggle(BaseModel):
    enabled: bool


def _auto_collect_state(c):
    from collection import get_setting
    return {"enabled": bool(get_setting(c, "auto_collect", {"enabled": True}).get("enabled", True)),
            "schedule": get_setting(c, "collect_schedule"),
            "last_run": get_setting(c, "collect_last_run")}


@app.get("/api/auto-collect")
def auto_collect():
    try:
        with _db() as c:
            return _auto_collect_state(c)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"DB error: {e}")


@app.post("/api/auto-collect")
def set_auto_collect(t: Toggle):
    """The header switch. Off = the scheduler skips its daily run (no model tokens spent)."""
    from collection import set_setting
    try:
        with _db() as c:
            set_setting(c, "auto_collect", {"enabled": t.enabled})
            return _auto_collect_state(c)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"DB error: {e}")


def _ensure_posts_link(cur):
    # Which collected item a post came from, so publish/unpublish keep both in step.
    # (init.sql has it for new databases; this adds it to the existing one.)
    cur.execute("ALTER TABLE content_posts ADD COLUMN IF NOT EXISTS item_id BIGINT")


@app.get("/api/publish/ready")
def publish_ready():
    items = []
    try:
        with _db() as c, c.cursor() as cur:
            cur.execute("SELECT id, pillar, headline, summary, source_name, source_url, published "
                        "FROM collected_items WHERE status='approved' ORDER BY collected_at DESC LIMIT 200")
            names = [d[0] for d in cur.description]
            for row in cur.fetchall():
                it = dict(zip(names, row))
                it["published"] = it["published"].isoformat() if it.get("published") else None
                items.append(it)
    except Exception as e:
        raise HTTPException(502, f"DB error: {e}")
    return {"items": items}


@app.post("/api/publish/{item_id}")
def publish_item(item_id: int):
    import re
    try:
        with _db() as c, c.cursor() as cur:
            cur.execute("SELECT pillar, headline, summary, source_name, source_url, "
                        "agencies, mode, programs, tags, state "
                        "FROM collected_items WHERE id=%s AND status='approved'", (item_id,))
            row = cur.fetchone()
            if not row:
                raise HTTPException(404, "no approved item with that id")
            pillar, headline, summary, source_name, source_url, agencies, mode, programs, tags, state = row
            base = re.sub(r"[^a-z0-9]+", "-", (headline or "post").lower()).strip("-")[:60] or "post"
            slug = f"{base}-{item_id}"
            body = summary or ""
            if source_url:
                body += f"\n\nSource: {source_name or ''} - {source_url}"
            _ensure_posts_link(cur)
            cur.execute("INSERT INTO content_posts (slug, pillar, title, body, status, publish_at, item_id, "
                        "source_name, source_url, agencies, mode, programs, tags, state) "
                        "VALUES (%s,%s,%s,%s,'published', now(), %s, %s,%s,%s,%s,%s,%s,%s) "
                        "ON CONFLICT (slug) DO UPDATE SET status='published', publish_at=now(), "
                        "item_id=EXCLUDED.item_id, source_name=EXCLUDED.source_name, source_url=EXCLUDED.source_url, "
                        "agencies=EXCLUDED.agencies, mode=EXCLUDED.mode, programs=EXCLUDED.programs, "
                        "tags=EXCLUDED.tags, state=EXCLUDED.state RETURNING id",
                        (slug, pillar, headline, body, item_id, source_name, source_url,
                         agencies or [], mode or [], programs or [], tags or [], state))
            post_id = cur.fetchone()[0]
            cur.execute("UPDATE collected_items SET status='published' WHERE id=%s", (item_id,))
            c.commit()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"DB error: {e}")
    _request_site_rebuild("post published")
    return {"post_id": post_id, "item_id": item_id, "status": "published"}


@app.get("/api/posts")
def posts(pillar: Optional[str] = None, agency: Optional[str] = None, mode: Optional[str] = None,
          program: Optional[str] = None, tag: Optional[str] = None, state: Optional[str] = None,
          featured: Optional[bool] = None):
    where, params = ["status='published'"], []
    if pillar:
        where.append("pillar=%s"); params.append(pillar)
    if agency:
        where.append("%s = ANY(agencies)"); params.append(agency)
    if mode:
        where.append("%s = ANY(mode)"); params.append(mode)
    if program:
        where.append("%s = ANY(programs)"); params.append(program)
    if tag:
        where.append("%s = ANY(tags)"); params.append(tag)
    if state:
        where.append("state=%s"); params.append(state)
    # A placement only counts as featured until featured_until passes.
    live = "(COALESCE(featured, false) AND (featured_until IS NULL OR featured_until > now()))"
    if featured is not None:
        where.append(f"{live} = %s"); params.append(featured)
    sql = ("SELECT id, slug, pillar, title, status, publish_at, body, source_name, source_url, "
           f"agencies, mode, programs, tags, state, {live} AS featured, featured_until, sponsor FROM content_posts "
           "WHERE " + " AND ".join(where) + f" ORDER BY {live} DESC, publish_at DESC NULLS LAST LIMIT 200")
    out = []
    try:
        with _db() as c, c.cursor() as cur:
            cur.execute(sql, params)
            names = [d[0] for d in cur.description]
            for row in cur.fetchall():
                p = dict(zip(names, row))
                p["publish_at"] = p["publish_at"].isoformat() if p.get("publish_at") else None
                p["featured_until"] = p["featured_until"].isoformat() if p.get("featured_until") else None
                # body is the summary plus a trailing "Source: name - url" line (source fields are separate).
                p["summary"] = (p.get("body") or "").split("\n\nSource:")[0].strip() or None
                out.append(p)
    except Exception as e:
        raise HTTPException(502, f"DB error: {e}")
    return {"posts": out}


@app.post("/api/posts/{post_id}/unpublish")
def unpublish(post_id: int):
    """Take a post off the site and put its item back in "Ready to publish", so it can be republished."""
    try:
        with _db() as c, c.cursor() as cur:
            _ensure_posts_link(cur)
            cur.execute("UPDATE content_posts SET status='draft' WHERE id=%s RETURNING item_id", (post_id,))
            row = cur.fetchone()
            if not row:
                raise HTTPException(404, "no post with that id")
            if row[0] is not None:
                cur.execute("UPDATE collected_items SET status='approved' WHERE id=%s AND status='published'",
                            (row[0],))
            c.commit()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"DB error: {e}")
    _request_site_rebuild("post unpublished")
    return {"post_id": post_id, "status": "draft"}


@app.post("/api/posts/{post_id}/feature")
def feature(post_id: int, on: bool = True, sponsor: Optional[str] = None, days: int = 30):
    """Mark a post as a featured/sponsored placement (e.g. a paid People-on-the-move highlight)
    for `days` days (1-365); it drops out of featured ordering automatically when that passes."""
    if not 1 <= days <= 365:
        raise HTTPException(400, "days must be between 1 and 365")
    try:
        with _db() as c, c.cursor() as cur:
            if on:
                cur.execute("UPDATE content_posts SET featured=true, sponsor=%s, "
                            "featured_until = now() + make_interval(days => %s) WHERE id=%s",
                            ((sponsor or "").strip()[:120] or None, days, post_id))
            else:
                cur.execute("UPDATE content_posts SET featured=false, featured_until=NULL WHERE id=%s", (post_id,))
            if not cur.rowcount:
                raise HTTPException(404, "no post with that id")
            c.commit()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"DB error: {e}")
    return {"post_id": post_id, "featured": on}


@app.get("/api/cig")
def cig(phase: Optional[str] = None, mode: Optional[str] = None, rating: Optional[str] = None,
        state: Optional[str] = None, sponsor: Optional[str] = None):
    """The latest snapshot of the CIG pipeline (older snapshots are history, see /api/cig/history)."""
    import cig as cigmod
    empty = {"summary": {"snapshot": None, "projects": 0, "total_cig_musd": 0.0, "by_phase": {},
                         "snapshots": 0}, "projects": []}
    out = []
    try:
        with _db() as c, c.cursor() as cur:
            cur.execute("SELECT to_regclass('cig_projects') IS NOT NULL")
            if not cur.fetchone()[0]:  # nothing loaded yet: an empty pipeline, not an error
                return empty
            cigmod.create_table(c)  # adds milestone columns to a table created before they existed
            cur.execute("SELECT max(snapshot_date), count(DISTINCT snapshot_date) FROM cig_projects")
            snap, n_snaps = cur.fetchone()
            if not snap:
                return empty
            where, params = ["snapshot_date=%s"], [snap]
            for col, val in (("phase", phase), ("mode", mode), ("rating", rating), ("state", state)):
                if val:
                    where.append(f"{col}=%s"); params.append(val)
            if sponsor:
                where.append("sponsor ILIKE %s"); params.append(f"%{sponsor}%")
            cur.execute("SELECT count(*), COALESCE(sum(cig_request_musd),0) FROM cig_projects WHERE snapshot_date=%s",
                        (snap,))
            total_projects, total_cig = cur.fetchone()
            cur.execute("SELECT phase, count(*) FROM cig_projects WHERE snapshot_date=%s GROUP BY phase", (snap,))
            by_phase = {ph: n for ph, n in cur.fetchall()}
            cur.execute("SELECT id, project_name, sponsor, city, state, mode, phase, cost_musd, cost_raw, "
                        "cig_request_musd, cig_request_raw, cig_share, rating, noncig_status, est_grant, "
                        "nepa, pd_entry, eng_entry, lonp_req, lonp_dec, lonp_action, req_rating_date, "
                        "proj_rating_date FROM cig_projects WHERE " + " AND ".join(where) +
                        " ORDER BY cig_request_musd DESC NULLS LAST, project_name", params)
            names = [d[0] for d in cur.description]
            for row in cur.fetchall():
                pr = dict(zip(names, row))
                for k in ("cost_musd", "cig_request_musd"):
                    if pr.get(k) is not None:
                        pr[k] = float(pr[k])
                out.append(pr)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"DB error: {e}")
    return {"summary": {"snapshot": snap.isoformat(), "projects": total_projects, "total_cig_musd": float(total_cig),
                        "by_phase": by_phase, "snapshots": n_snaps}, "projects": out}


@app.get("/api/cig/history")
def cig_history(name: str, sponsor: Optional[str] = None):
    """One project across every loaded snapshot, oldest first."""
    where, params = ["project_name=%s"], [name]
    if sponsor:
        where.append("sponsor=%s"); params.append(sponsor)
    out = []
    try:
        with _db() as c, c.cursor() as cur:
            cur.execute("SELECT to_regclass('cig_projects') IS NOT NULL")
            if not cur.fetchone()[0]:
                return {"name": name, "history": []}
            cur.execute("SELECT to_char(snapshot_date,'YYYY-MM-DD'), phase, rating, cost_musd, cig_request_musd, "
                        "est_grant, noncig_status FROM cig_projects WHERE " + " AND ".join(where) +
                        " ORDER BY snapshot_date", params)
            for d_, ph, rt, cost, cigm, est, nc in cur.fetchall():
                out.append({"snapshot_date": d_, "phase": ph, "rating": rt,
                            "cost_musd": float(cost) if cost is not None else None,
                            "cig_request_musd": float(cigm) if cigm is not None else None,
                            "est_grant": est, "noncig_status": nc})
    except Exception as e:
        raise HTTPException(502, f"DB error: {e}")
    return {"name": name, "history": out}


MAX_CIG_PDF = 25 * 1024 * 1024


@app.post("/api/cig/upload")
async def cig_upload(request: Request, filename: str = ""):
    """Load a CIG dashboard PDF downloaded in a browser. The raw PDF is the request body; `filename`
    supplies the snapshot date (e.g. ...09-11-2026.pdf)."""
    body = await request.body()
    if len(body) > MAX_CIG_PDF:
        raise HTTPException(413, "That file is over 25 MB; the CIG dashboard PDF is much smaller.")
    return await _load_cig_pdf(body, filename, "file name", "upload", filename)


async def _load_cig_pdf(body, name, what, source, label):
    """Parse a dashboard PDF and load it as the snapshot dated in `name` (upload file name or link).
    Shared by upload and load-from-link; cig.load's safeguard refuses a PDF that parses to too few
    projects, leaving the pipeline as it was. Every attempt is logged in cig_loads (source/label say
    where it came from) and a loaded PDF is kept for the Dashboard files list."""
    import asyncio
    import tempfile
    import cig
    if not body.startswith(b"%PDF"):
        raise HTTPException(400, "That isn't a PDF. Use the CIG dashboard PDF from transit.dot.gov/CIG.")
    snap = cig.date_in(name)
    snap_used = snap or cig.snapshot_date("")

    def parse_and_load():
        with tempfile.NamedTemporaryFile(suffix=".pdf") as f:
            f.write(body)
            f.flush()
            rows = cig.parse_pdf(f.name)
        with _db() as c:
            try:
                n = cig.load(c, rows, snap_used)
            except SystemExit as e:
                c.rollback()
                cig.record_load(c, snap_used, source, label, "refused", len(rows), str(e))
                raise
            cig.record_load(c, snap_used, source, label, "loaded", n, body=body)
        return n

    try:
        n = await asyncio.to_thread(parse_and_load)  # parsing takes a few seconds; keep the server responsive
    except SystemExit as e:  # cig.load's "too few projects" safeguard
        raise HTTPException(422, str(e))
    except HTTPException:
        raise
    except Exception as e:
        msg = f"Couldn't read that PDF ({type(e).__name__}: {e}). The current pipeline is unchanged."
        try:
            with _db() as c:
                cig.record_load(c, snap_used, source, label, "refused", None, msg)
        except Exception:
            pass
        raise HTTPException(422, msg)
    _request_site_rebuild("CIG dashboard loaded")  # the public site's CIG figures and page
    return {"projects": n, "snapshot": snap,
            "note": None if snap else f"No date in the {what}, so today's date was used as the snapshot date."}


@app.get("/api/cig/changes")
def cig_changes(to: Optional[str] = None, since: Optional[str] = None):
    """What changed between two snapshots (default: latest vs the previous one)."""
    import cig as cigmod
    try:
        with _db() as c:
            return cigmod.changes(c, to, since)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"DB error: {e}")


@app.get("/api/cig/loads")
def cig_loads():
    """Every dashboard load or refusal, newest first, for the Dashboard files list."""
    import cig as cigmod
    try:
        with _db() as c, c.cursor() as cur:
            cigmod.create_loads_table(c)
            cur.execute("SELECT id, to_char(loaded_at AT TIME ZONE 'America/New_York', 'YYYY-MM-DD HH24:MI'), "
                        "to_char(snapshot_date,'YYYY-MM-DD'), source, name, status, projects, message, "
                        "file_path IS NOT NULL, file_bytes FROM cig_loads ORDER BY loaded_at DESC, id DESC LIMIT 200")
            keys = ["id", "loaded_at", "snapshot_date", "source", "name", "status", "projects", "message",
                    "has_file", "file_bytes"]
            return {"loads": [dict(zip(keys, r)) for r in cur.fetchall()]}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"DB error: {e}")


@app.get("/api/cig/loads/{load_id}/file")
def cig_load_file(load_id: int):
    """The kept copy of a loaded dashboard PDF."""
    import cig as cigmod
    from fastapi.responses import FileResponse
    try:
        with _db() as c, c.cursor() as cur:
            cigmod.create_loads_table(c)
            cur.execute("SELECT file_path, snapshot_date FROM cig_loads WHERE id=%s", (load_id,))
            row = cur.fetchone()
    except Exception as e:
        raise HTTPException(502, f"DB error: {e}")
    base = os.path.realpath(cigmod.PDF_DIR)
    path = os.path.realpath(row[0]) if row and row[0] else None
    # Only ever serve files from the kept-PDF folder.
    if not path or not path.startswith(base + os.sep) or not os.path.isfile(path):
        raise HTTPException(404, "No kept copy of that file.")
    return FileResponse(path, media_type="application/pdf",
                        filename=f"CIG-Dashboard-{row[1].isoformat() if row[1] else 'undated'}.pdf")


class CigLink(BaseModel):
    url: str


CIG_HOSTS = {"www.transit.dot.gov", "transit.dot.gov"}


@app.post("/api/cig/load-url")
async def cig_load_url(link: CigLink):
    """Fetch a dashboard PDF from its transit.dot.gov link and load it. FTA's /CIG page blocks
    automated requests but the PDF files themselves don't, so pasting the link skips the download.
    Only https PDF links on transit.dot.gov are fetched (no redirects to other hosts), so this can't
    be used to make the NAS request arbitrary addresses."""
    import asyncio
    from urllib.parse import urlparse
    import requests
    url = link.url.strip()
    u = urlparse(url)
    if u.scheme != "https" or u.hostname not in CIG_HOSTS or not u.path.lower().endswith(".pdf"):
        raise HTTPException(400, "Paste the dashboard's PDF link from transit.dot.gov "
                                 "(https://www.transit.dot.gov/sites/fta.dot.gov/files/...pdf).")

    def fetch():
        with requests.get(url, timeout=(10, 60), stream=True, allow_redirects=False,
                          headers={"User-Agent": "Transit411/1.0"}) as r:
            if r.status_code in (301, 302, 303, 307, 308):
                raise HTTPException(422, "That link redirects elsewhere; paste the PDF's final link.")
            if r.status_code == 403:
                raise HTTPException(422, "transit.dot.gov refused that request (HTTP 403). "
                                         "Download the PDF in your browser and use Upload dashboard instead.")
            if r.status_code != 200:
                raise HTTPException(422, f"transit.dot.gov returned HTTP {r.status_code} for that link.")
            data = bytearray()
            for chunk in r.iter_content(64 * 1024):
                data += chunk
                if len(data) > MAX_CIG_PDF:
                    raise HTTPException(413, "That file is over 25 MB; the CIG dashboard PDF is much smaller.")
            return bytes(data)

    try:
        body = await asyncio.to_thread(fetch)
    except HTTPException:
        raise
    except requests.RequestException as e:
        raise HTTPException(502, f"Couldn't download that link ({type(e).__name__}).")
    return await _load_cig_pdf(body, u.path, "link", "link", url)


CIG_SCHEMA_DOC = """Table cig_projects - the FTA Capital Investment Grants pipeline, ONE ROW PER PROJECT PER MONTHLY SNAPSHOT.
Columns:
- snapshot_date (date): which monthly dashboard the row is from. The CURRENT pipeline is the latest snapshot; unless the question is about history/change over time, filter to it: snapshot_date = (SELECT max(snapshot_date) FROM cig_projects).
- project_name, sponsor (the transit agency), city, state (2-letter code)
- mode: 'Bus','BRT','Light Rail','Heavy Rail','Commuter Rail','Streetcar','Rail' (may be NULL)
- phase: 'PD' (Project Development) or 'Eng' (Engineering)
- rating: 'H','MH','M','ML','L' (High..Low); NULL if unrated. "Medium or better" = rating IN ('M','MH','H').
- cost_musd (numeric, total project cost in $millions), cig_request_musd (numeric, CIG funding sought in $millions), cig_share (text like '49%')
- noncig_status: 'Committed' or 'In Progress' (local match). est_grant (text, e.g. 'Spring 2027').
- milestone dates (text): nepa, pd_entry, eng_entry, lonp_req, lonp_dec, req_rating_date, proj_rating_date.
For "what changed / advanced / trend" questions, compare rows across snapshot_date for the same project_name; otherwise use the latest snapshot. Return ONE read-only SELECT, at most 200 rows."""


def _cig_nl_to_sql(question, history=None, snapshot_dates=None):
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return None
    import re
    import anthropic
    client = anthropic.Anthropic(api_key=key)
    messages = []
    # Only complete turns, so user/assistant messages alternate.
    for t in [t for t in (history or []) if t.get("question") and t.get("sql")][-6:]:
        messages.append({"role": "user", "content": t["question"][:1000]})
        messages.append({"role": "assistant", "content": t["sql"][:4000]})
    messages.append({"role": "user", "content": question})
    msg = client.messages.create(
        model=os.environ.get("ASK_NTD_MODEL", "claude-haiku-4-5"), max_tokens=2000,
        system="You translate a question into ONE read-only PostgreSQL SELECT over the table below. "
               "Return ONLY the SQL - no prose, no markdown fences.\n" + CIG_SCHEMA_DOC
               + ("\nSnapshots loaded (snapshot_date values, newest first): " + ", ".join(snapshot_dates)
                  + ". Dashboards are dated mid-month, so a month means the snapshot in that month (e.g. "
                  "date_trunc('month', snapshot_date) = '2026-07-01'); never assume the 1st. For 'between month A "
                  "and month B' compare those two snapshots." if snapshot_dates else ""),
        messages=messages)
    if msg.stop_reason == "max_tokens":
        raise ValueError("The generated query was too long and got cut off; try a narrower question.")
    text = "".join(b.text for b in msg.content if b.type == "text").strip()
    return re.sub(r"^```sql|^```|```$", "", text, flags=re.M).strip()


CIG_READER = "cig_reader"  # database role that can read cig_projects and nothing else
_cig_reader_ready = False


def _ensure_cig_reader(cur):
    """Create the restricted role Ask CIG queries run as (idempotent, once per process)."""
    global _cig_reader_ready
    if _cig_reader_ready:
        return
    cur.execute(f"""DO $$ BEGIN
        IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{CIG_READER}') THEN
            CREATE ROLE {CIG_READER} NOLOGIN;
        END IF; END $$""")
    cur.execute(f"REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {CIG_READER}")
    cur.execute(f"GRANT USAGE ON SCHEMA public TO {CIG_READER}")
    cur.execute(f"GRANT SELECT ON cig_projects TO {CIG_READER}")
    _cig_reader_ready = True


def _check_cig_sql(sql):
    """One read-only SELECT (or WITH ... SELECT) and nothing that could change role, settings or
    reach outside the database. The query also runs as cig_reader in a read-only transaction, so
    this is a first line of defence, not the only one."""
    import re
    s = sql.strip()
    if s.endswith(";"):
        s = s[:-1].rstrip()
    if ";" in re.sub(r"'(?:[^']|'')*'", "''", s):
        raise HTTPException(400, "Only a single query is allowed.")
    if not re.match(r"^\s*(SELECT|WITH)\b", s, re.I) or re.search(
            r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|GRANT|REVOKE|TRUNCATE|COPY|MERGE|SET|RESET|DO|CALL|"
            r"EXECUTE|PREPARE|LISTEN|NOTIFY|VACUUM|LOCK|set_config|pg_read\w*|pg_ls_dir|pg_sleep\w*|lo_\w+|dblink\w*)\b",
            s, re.I):
        raise HTTPException(400, "Only read-only SELECT queries over cig_projects are allowed.")
    return s


class CigAsk(BaseModel):
    question: str
    history: Optional[List[Turn]] = None


@app.post("/api/cig/ask")
def cig_ask(a: CigAsk):
    import re
    import datetime as _dt
    from decimal import Decimal
    import anthropic
    import cig as cigmod
    try:
        with _db() as c:
            snaps = [s for s, _ in cigmod.snapshots(c)]
    except Exception:
        snaps = []
    try:
        sql = _cig_nl_to_sql(a.question, [t.model_dump() for t in (a.history or [])], snaps)
    except anthropic.AnthropicError as e:
        raise HTTPException(502, f"Anthropic API error: {getattr(e, 'message', None) or e}")
    except ValueError as e:
        raise HTTPException(422, str(e))
    if not sql:
        raise HTTPException(400, "Set ANTHROPIC_API_KEY for natural-language CIG search.")
    sql = _check_cig_sql(sql)
    try:
        with _db() as c, c.cursor() as cur:
            _ensure_cig_reader(cur)
            c.commit()
            # Read-only transaction, as the cig_reader role (cig_projects only), with a time limit.
            cur.execute("SET TRANSACTION READ ONLY")
            cur.execute(f"SET LOCAL ROLE {CIG_READER}")
            cur.execute("SET LOCAL statement_timeout = '5s'")
            cur.execute(sql)
            cols = [d[0] for d in cur.description]
            rows = [list(r) for r in cur.fetchmany(200)]
            c.rollback()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(400, {"error": f"Query error: {e}", "sql": sql})

    def safe(v):
        if isinstance(v, Decimal):
            return float(v)
        if isinstance(v, (_dt.date, _dt.datetime)):
            return v.isoformat()
        return v
    return {"question": a.question, "sql": sql, "columns": cols,
            "rows": [[safe(v) for v in r] for r in rows]}


@app.get("/", response_class=HTMLResponse)
def home():
    return DASHBOARD


DASHBOARD = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Transit411 - Command Center</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Archivo:wght@600;700;800;900&family=Spectral:wght@400;500;600&family=JetBrains+Mono:wght@400;600&display=swap">
<link rel="stylesheet" href="/static/t411.css">
<script src="/static/t411.js"></script>
<style>
:root{--bg:#F2EEE4;--panel:#F7F4ED;--card:#FFF;--ink:#17140F;--muted:#6A6458;--line:#D8D2C4;--soft:#E7E1D4;--accent:#C0341F;--ok:#1F6B4A;--bar:#C0341F;--track:#EDE7D8}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font-family:'Spectral',Georgia,serif}
.disp{font-family:'Archivo',sans-serif}
header{background:var(--ink);color:var(--panel);padding:16px 24px;display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:12px}
.brand{font-family:'Archivo',sans-serif;font-weight:900;font-size:22px;letter-spacing:-1px}.brand span{color:var(--accent)}
.sub{font-family:'Archivo',sans-serif;font-size:10px;letter-spacing:2.5px;text-transform:uppercase;color:#A69F90}
.status{display:flex;gap:16px;align-items:center;flex-wrap:wrap;font-family:'Archivo',sans-serif;font-size:12px}
.ac{display:flex;align-items:center;gap:8px;cursor:pointer;padding-right:16px;border-right:1px solid #3A352C}
.switch{position:relative;width:38px;height:22px;border-radius:999px;border:none;background:#5A5448;cursor:pointer;padding:0;transition:background .15s}
.switch .knob{position:absolute;top:3px;left:3px;width:16px;height:16px;border-radius:50%;background:#F7F4ED;transition:left .15s}
.switch[aria-checked="true"]{background:#1F6B4A}.switch[aria-checked="true"] .knob{left:19px}
.switch:disabled{opacity:.5;cursor:default}.switch:focus-visible{outline:2px solid #EE6A54;outline-offset:2px}
#acTxt{min-width:24px;color:#A69F90}
.pill{display:flex;align-items:center;gap:7px}.dot{width:9px;height:9px;border-radius:50%;background:#8A8375}.dot.up{background:#5FBF8F}.dot.down{background:#E8604B}
.tabs{display:flex;gap:2px;background:var(--panel);border-bottom:1px solid var(--line);padding:0 16px}
.tab{font-family:'Archivo',sans-serif;font-size:13px;font-weight:700;padding:13px 18px;border:none;background:transparent;color:var(--muted);cursor:pointer;border-bottom:3px solid transparent}
.tab.on{color:var(--ink);border-bottom-color:var(--accent)}
.wrap{max-width:980px;margin:0 auto;padding:24px 20px 60px}
.panel{display:none}.panel.on{display:block}
h2.disp{font-size:22px;font-weight:800;letter-spacing:-.4px;margin:0 0 4px}
p.lead{color:var(--muted);font-size:14px;margin:0 0 16px}
.askhead{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:10px}
.follow{font-family:'Archivo',sans-serif;font-size:12px;font-weight:700;color:var(--accent);display:none}
.follow.on{display:inline}
.newq{font-family:'Archivo',sans-serif;font-size:12px;font-weight:800;text-transform:uppercase;letter-spacing:.4px;background:transparent;border:1px solid var(--line);color:var(--muted);padding:8px 14px;border-radius:8px;cursor:pointer}
form{display:flex;gap:10px;margin-bottom:10px}
input[type=text]{flex-grow:1;padding:14px 15px;border:1px solid var(--line);background:var(--card);color:var(--ink);font-size:15px;font-family:'Spectral',serif;border-radius:9px}
button.go{font-family:'Archivo',sans-serif;font-weight:800;font-size:13px;text-transform:uppercase;letter-spacing:.5px;background:var(--accent);color:#fff;border:none;padding:0 26px;border-radius:9px;cursor:pointer}
.examples{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:16px}
.csearch{display:flex;gap:8px;flex-wrap:wrap;margin:-4px 0 10px}
.csearch input{flex:1 1 260px;min-width:0;padding:11px 13px;border:1px solid var(--line);background:var(--card);color:var(--ink);font-size:14px;font-family:'Spectral',serif;border-radius:9px}
.csearch select{flex:0 1 150px;min-width:0;padding:9px 8px;border:1px solid var(--line);background:var(--card);color:var(--ink);font-family:'Archivo',sans-serif;font-size:12px;border-radius:9px}
.csearch select.on{border-color:var(--accent);color:var(--accent)}
.cactive{display:flex;gap:8px;flex-wrap:wrap;align-items:center;min-height:4px;margin-bottom:12px;font-family:'Archivo',sans-serif;font-size:12px;color:var(--muted)}
.fpill{display:inline-flex;align-items:center;gap:6px;border:1px solid var(--accent);color:var(--accent);background:transparent;border-radius:999px;padding:4px 10px;font-family:'Archivo',sans-serif;font-size:12px;font-weight:700;cursor:pointer}
.facets{display:flex;gap:6px;flex-wrap:wrap;margin:0 0 10px}
.fchip{font-family:'Archivo',sans-serif;font-size:11px;font-weight:700;padding:3px 9px;border-radius:999px;border:1px solid var(--line);background:var(--panel);color:var(--ink);cursor:pointer}
.fchip:hover{border-color:var(--accent);color:var(--accent)}
.fchip.k-state{background:var(--ink);color:var(--panel);border-color:var(--ink)}
.fchip.k-mode{border-color:#1F6B7A;color:#1F6B7A}
.fchip.k-program{border-color:#1F6B4A;color:#1F6B4A}
.fchip.k-agency{font-weight:800}
.fchip.k-tag{background:transparent;color:var(--muted);font-weight:600}
.ex{font-family:'Archivo',sans-serif;font-size:12px;font-weight:600;padding:7px 12px;border:1px solid var(--line);background:transparent;color:var(--ink);border-radius:999px;cursor:pointer}.ex:hover{border-color:var(--accent);color:var(--accent)}
.rcard{background:var(--card);border:1px solid var(--line);border-radius:12px;overflow:hidden;margin-bottom:14px}
.rh{background:var(--ink);color:var(--panel);padding:11px 18px;font-family:'Archivo',sans-serif;font-size:13px}
.rh .fu{color:#EE6A54;font-size:10px;font-weight:800;letter-spacing:1px;text-transform:uppercase;margin-right:8px}
.chart{padding:12px 18px 6px}
.brow{display:flex;align-items:center;gap:10px;margin:6px 0}
.blabel{font-family:'Archivo',sans-serif;font-size:12px;width:240px;flex-shrink:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.btrack{flex-grow:1;background:var(--track);border-radius:4px;height:15px}.bfill{background:var(--bar);height:15px;border-radius:4px}
.bval{font-family:'JetBrains Mono',monospace;font-size:12px;font-weight:600;width:80px;text-align:right}
.twrap{padding:4px 18px 10px;overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:13px;font-family:'Archivo',sans-serif}
th{text-align:left;color:var(--muted);border-bottom:2px solid var(--line);padding:8px 12px 8px 0;white-space:nowrap}
td{padding:8px 12px 8px 0;border-bottom:1px solid var(--soft);white-space:nowrap}td.num{font-family:'JetBrains Mono',monospace}
details{margin:4px 18px 14px;border:1px solid var(--line);border-radius:8px;background:var(--panel)}
summary{cursor:pointer;padding:10px 13px;font-family:'Archivo',sans-serif;font-size:12px;font-weight:700;color:var(--muted)}
pre{margin:0;padding:0 13px 13px;font-family:'JetBrains Mono',monospace;font-size:12px;white-space:pre-wrap;color:var(--ink)}
.soon{padding:40px 24px;text-align:center;color:var(--muted);font-family:'Archivo',sans-serif;border:1px dashed var(--line);border-radius:12px}
.err{padding:16px 18px;color:var(--accent);font-family:'Archivo',sans-serif;font-size:14px}
.loading{padding:20px 18px;color:var(--muted);font-family:'Archivo',sans-serif}
</style></head><body>
<header>
  <div><div class="brand">TRANSIT<span>411</span></div><div class="sub">Command Center</div></div>
  <div class="status">
    <label class="ac" id="acWrap" title="Loading auto-collect status...">
      <span>Auto-collect</span>
      <button type="button" class="switch" id="acSwitch" role="switch" aria-checked="false" aria-label="Auto-collect daily" disabled><span class="knob"></span></button>
      <span id="acTxt">...</span>
    </label>
    <div class="pill"><span class="dot" id="apiDot"></span><span id="apiTxt">API...</span></div>
    <div class="pill"><span class="dot" id="dbDot"></span><span id="dbTxt">DB...</span></div>
  </div>
</header>
<div class="tabs">
  <button class="tab on" data-t="ask">Ask NTD</button>
  <button class="tab" data-t="collect">Collection</button>
  <button class="tab" data-t="sources">Sources</button>
  <button class="tab" data-t="publish">Publish</button>
  <button class="tab" data-t="grants">Grants</button>
  <button class="tab" data-t="askcig">Ask CIG</button>
</div>
<div class="wrap">
  <div class="panel on" id="p-ask">
    <div class="askhead"><div><h2 class="disp">Ask NTD</h2><p class="lead">Ask, then keep asking - follow-ups like "what about Texas?" build on your last question. Hit New question to start fresh.</p></div></div>
    <form id="askForm"><input type="text" id="q" placeholder="Ask a question, then follow up..." autocomplete="off"><button class="go" type="submit">Ask</button></form>
    <div class="askhead"><div class="examples" id="ex"></div><div><span class="follow" id="follow">Following your thread</span> <button class="newq" id="newq" type="button">New question</button></div></div>
    <div id="out"></div>
  </div>
  <div class="panel" id="p-collect">
    <div class="askhead"><div><h2 class="disp">Collection queue</h2><p class="lead">Items the engine gathered, freshest first - approve what runs, skip the rest. Populate with the collector job.</p></div><button class="newq" id="cRefresh" type="button">Refresh</button></div>
    <div class="examples" id="cChips"></div>
    <div class="csearch">
      <input type="search" id="cQ" placeholder="Search headlines, summaries, agencies, tags..." autocomplete="off" aria-label="Search the collection">
      <select id="cPillar" aria-label="Pillar"></select>
      <select id="cMode" aria-label="Mode"></select>
      <select id="cProgram" aria-label="Program"></select>
      <select id="cState" aria-label="State"></select>
      <button class="newq" id="cClear" type="button">Clear</button>
    </div>
    <div class="cactive" id="cActive"></div>
    <div id="cOut"></div>
  </div>
  <div class="panel" id="p-sources"><div class="soon">Source registry - phase 2b. The watchlist that feeds the collection engine.</div></div>
  <div class="panel" id="p-publish">
    <div class="askhead"><div><h2 class="disp">Publish</h2><p class="lead">Approved items become live posts. Publishing writes to content_posts - what the public site reads - and the site rebuilds itself about a minute later.</p></div><div style="display:flex;gap:8px"><button class="newq" id="pRebuild" type="button">Rebuild site now</button><button class="newq" id="pRefresh" type="button">Refresh</button></div></div>
    <div id="pSite" style="font-family:Archivo,sans-serif;font-size:12px;color:var(--muted);margin:-4px 0 14px"></div>
    <div style="font-family:Archivo,sans-serif;font-size:12px;font-weight:800;letter-spacing:1px;text-transform:uppercase;color:var(--muted);margin:6px 0 10px">Ready to publish</div>
    <div id="pReady"></div>
    <div style="font-family:Archivo,sans-serif;font-size:12px;font-weight:800;letter-spacing:1px;text-transform:uppercase;color:var(--muted);margin:26px 0 10px">Published</div>
    <div id="pPosts"></div>
  </div>
  <div class="panel" id="p-grants">
    <div class="askhead"><div><h2 class="disp">CIG Pipeline</h2><p class="lead">FTA Capital Investment Grants dashboard - every New/Small/Core project seeking funding, where it stands, and what it wants. Click a project for its milestone dates and month-by-month history. FTA updates it about monthly: open <a href="https://www.transit.dot.gov/CIG" target="_blank" rel="noopener noreferrer">transit.dot.gov/CIG</a>, then copy the dashboard PDF's link into the box below (or download it and upload it).</p></div>
      <div style="display:flex;gap:8px"><button class="go" style="padding:9px 16px" id="gUploadBtn" type="button">Upload dashboard</button><button class="newq" id="gRefresh" type="button">Refresh</button></div></div>
    <input type="file" id="gFile" accept="application/pdf,.pdf" hidden>
    <form class="csearch" id="gLinkForm" style="margin:0 0 12px">
      <input type="url" id="gLink" placeholder="...or paste the dashboard PDF link (https://www.transit.dot.gov/sites/fta.dot.gov/files/...pdf)" aria-label="Dashboard PDF link" autocomplete="off">
      <button class="newq" id="gLinkBtn" type="submit">Load from link</button>
    </form>
    <div id="gMsg"></div>
    <div id="gSummary" style="margin-bottom:14px"></div>
    <div id="gChanges"></div>
    <details class="rcard" id="gLoadsBox" style="padding:0 18px;margin-bottom:14px">
      <summary style="padding:13px 0;font-family:Archivo,sans-serif;font-weight:800;font-size:14px;cursor:pointer">Dashboard files <span id="gLoadsCount" style="font-weight:600;color:var(--muted)"></span></summary>
      <div id="gLoads" style="padding-bottom:12px"></div>
    </details>
    <div class="examples" id="gChips"></div>
    <div id="gOut"></div>
  </div>
  <div class="panel" id="p-askcig">
    <div class="askhead"><div><h2 class="disp">Ask CIG</h2><p class="lead">Ask the Capital Investment Grants pipeline in plain English. Your question becomes a read-only SQL query over cig_projects, run and shown.</p></div></div>
    <form id="cigAskForm"><input type="text" id="cigQ" placeholder="e.g. BRT projects seeking over $100M rated Medium or better" autocomplete="off"><button class="go" type="submit">Ask</button></form>
    <div class="examples" id="cigEx"></div>
    <div id="cigAskOut"></div>
  </div>
</div>
<script>
let thread=[];  // [{question, sql, columns, rows}]
const EX=["ridership trend by mode","cheapest heavy rail systems per rider","highest ridership rail systems","most expensive bus systems per rider"];
const exWrap=document.getElementById("ex");
EX.forEach(t=>{const b=document.createElement("button");b.className="ex";b.textContent=t;b.onclick=()=>{document.getElementById("q").value=t;doAsk(t);};exWrap.appendChild(b);});

document.querySelectorAll(".tab").forEach(t=>t.onclick=()=>{
  document.querySelectorAll(".tab").forEach(x=>x.classList.toggle("on",x===t));
  document.querySelectorAll(".panel").forEach(p=>p.classList.toggle("on",p.id==="p-"+t.dataset.t));
});
document.getElementById("newq").onclick=()=>{thread=[];document.getElementById("out").innerHTML="";document.getElementById("follow").classList.remove("on");document.getElementById("q").focus();};

async function refreshStatus(){
  try{const s=await (await fetch("/api/status")).json();
    setPill("api",s.api&&s.api.ok,s.api&&s.api.ok?("API "+(s.api.rows!=null?s.api.rows.toLocaleString()+" rows":"ok")):"API down");
    setPill("db",s.db&&s.db.ok,s.db&&s.db.ok?("DB "+(s.db.tables!=null?s.db.tables+" tables":"ok")):"DB down");
  }catch(e){setPill("api",false,"API down");setPill("db",false,"DB down");}
}
function setPill(k,up,txt){document.getElementById(k+"Dot").className="dot "+(up?"up":"down");document.getElementById(k+"Txt").textContent=txt;}

// Everything from the API, the model or the database is untrusted text: escape it before it
// goes into innerHTML (phase 2b will show headlines scraped from other sites).
function esc(v){return String(v??"").replace(/[&<>"']/g,ch=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[ch]));}
function isNum(v){return typeof v==="number"&&isFinite(v);}
// Format by what the column holds (same rules as the Ask NTD page), not by the size of the number.
function kind(c,rows){
  const n=c.toLowerCase(),vals=rows.map(r=>r[c]).filter(v=>v!==null&&v!==undefined);
  if(!vals.length||!vals.every(isNum))return "text";
  if(/(^|_)(year|yr)$|^year/.test(n))return "year";
  if(/(^|_)id$/.test(n))return "text";
  if(/percent|pct/.test(n))return "pct";
  if(/recovery|ratio/.test(n))return "ratio";
  if(/factor|cpi/.test(n))return "num";
  if(/cost|expense|opex|fare|dollar|spend|price|cpr|_real/.test(n))return "money";
  return "num";
}
function fmt(v,k){
  if(v===null||v===undefined)return "—";
  if(k==="text"||!isNum(v))return String(v);
  if(k==="year")return String(Math.round(v));
  if(k==="pct")return v.toFixed(1)+"%";
  if(k==="ratio")return (v*100).toFixed(1)+"%";
  const a=Math.abs(v);let s;
  if(a>=1e9)s=(a/1e9).toFixed(2)+"B";
  else if(a>=1e6)s=(a/1e6).toFixed(1)+"M";
  else if(Number.isInteger(v)||a>=1000)s=Math.round(a).toLocaleString();
  else s=a.toLocaleString(undefined,{minimumFractionDigits:k==="money"?2:0,maximumFractionDigits:2});
  return (v<0?"-":"")+(k==="money"?"$":"")+s;
}
function pickChartCol(cols,kinds){ // last numeric column (not a year or ID) = the metric asked about
  for(let i=cols.length-1;i>=0;i--){if(!["text","year"].includes(kinds[cols[i]]))return cols[i];}return null;}
function pickLabel(cols,rows,kinds){ // a readable name, never an ID; add mode/year when they tell rows apart
  const text=cols.filter(c=>kinds[c]==="text"&&!/(^|_)id$|^ntd_id$|mode_code/.test(c.toLowerCase()));
  const varies=c=>new Set(rows.map(r=>r[c])).size>1;
  const first=["agency","name","mode","state","city"].find(p=>text.includes(p))||text[0];
  const parts=first?[first]:[];
  if(first&&first!=="mode"&&text.includes("mode")&&varies("mode"))parts.push("mode");
  const yr=cols.find(c=>kinds[c]==="year");
  if(yr&&varies(yr))parts.push(yr);
  if(!parts.length)return null;
  return r=>parts.map(c=>fmt(r[c],kinds[c])).join(" · ");
}

function cardHtml(res,isFollow){
  const cols=res.columns||[],rows=res.rows||[];
  const tag=(isFollow?'<span class="fu">follow-up</span>':'')+'Q: '+esc(res.question);
  if(!rows.length)return '<div class="rcard"><div class="rh">'+tag+'</div><div class="err">No rows returned.</div></div>';
  const kinds=Object.fromEntries(cols.map(c=>[c,kind(c,rows)]));
  const metric=pickChartCol(cols,kinds),label=pickLabel(cols,rows,kinds);
  let chart="";
  const plotted=metric&&label?rows.filter(r=>isNum(r[metric])):[];
  if(plotted.length){const max=Math.max(...plotted.map(r=>Math.abs(r[metric])))||1;chart='<div class="chart">'+plotted.slice(0,15).map(r=>{
    const pct=Math.max(3,(Math.abs(r[metric])/max)*100),l=esc(label(r));return '<div class="brow"><div class="blabel" title="'+l+'">'+l+'</div><div class="btrack"><div class="bfill" style="width:'+pct+'%"></div></div><div class="bval">'+esc(fmt(r[metric],kinds[metric]))+'</div></div>';}).join("")+'</div>';}
  const th=cols.map(c=>"<th>"+esc(c)+"</th>").join("");
  const tb=rows.map(r=>"<tr>"+cols.map(c=>'<td class="'+(kinds[c]==="text"?"":"num")+'">'+esc(fmt(r[c],kinds[c]))+"</td>").join("")+"</tr>").join("");
  const sql=res.sql?'<details><summary>View the query it ran</summary><pre>'+esc(res.sql)+'</pre></details>':"";
  return '<div class="rcard"><div class="rh">'+tag+' &middot; '+rows.length+' rows'+(chart?' &middot; charting '+esc(metric):'')+'</div>'+chart+'<div class="twrap"><table><thead><tr>'+th+'</tr></thead><tbody>'+tb+'</tbody></table></div>'+sql+'</div>';
}
// The API's {"detail": ...} arrives wrapped once more by this server's proxy; unwrap to the message.
function errText(t){for(let i=0;i<2;i++){try{const d=JSON.parse(t).detail;t=typeof d==="string"?d:(d&&d.error)||JSON.stringify(d);}catch(_){break;}}return t;}
function renderThread(){document.getElementById("out").innerHTML=thread.map((r,i)=>cardHtml(r,i>0)).join("");
  document.getElementById("follow").classList.toggle("on",thread.length>0);
  window.scrollTo(0,document.body.scrollHeight);}

async function doAsk(q){
  const isFollow=thread.length>0;
  document.getElementById("out").insertAdjacentHTML("beforeend",'<div class="rcard" id="pending"><div class="loading">Reading "'+esc(q)+'"'+(isFollow?" (in context)":"")+' and running the query...</div></div>');
  window.scrollTo(0,document.body.scrollHeight);
  try{
    const r=await fetch("/api/ask",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({question:q,history:thread.map(t=>({question:t.question,sql:t.sql}))})});
    const pend=document.getElementById("pending"); if(pend)pend.remove();
    if(!r.ok){const t=await r.text();document.getElementById("out").insertAdjacentHTML("beforeend",'<div class="rcard"><div class="err">'+esc(errText(t))+'</div></div>');return;}
    const res=await r.json();
    thread.push({question:res.question||q,sql:res.sql,columns:res.columns,rows:res.rows});
    renderThread();
    document.getElementById("q").value="";
  }catch(e){const pend=document.getElementById("pending");if(pend)pend.remove();document.getElementById("out").insertAdjacentHTML("beforeend",'<div class="rcard"><div class="err">Could not reach the API.</div></div>');}
}
document.getElementById("askForm").onsubmit=e=>{e.preventDefault();const v=document.getElementById("q").value.trim();if(v)doAsk(v);};
// ---- Collection tab ----
let cFilter="pending";
const cChips=document.getElementById("cChips");
[["pending","Pending"],["approved","Approved"],["skipped","Skipped"],["published","Published"],["filtered","Auto-filtered"]].forEach(([k,lbl])=>{
  const b=document.createElement("button");b.className="ex";b.textContent=lbl;
  b.onclick=()=>{cFilter=k;document.querySelectorAll("#cChips .ex").forEach(x=>x.style.borderColor=(x===b?"var(--accent)":""));loadFacets();loadCollection();};
  if(k==="pending")b.style.borderColor="var(--accent)";cChips.appendChild(b);});
document.getElementById("cRefresh").onclick=()=>{loadFacets();loadCollection();};
document.querySelector('.tab[data-t="collect"]').addEventListener("click",()=>{loadFacets();loadCollection();});

// Search + facet filters. Dropdowns hold pillar/mode/program/state; agency and tag filters are set by
// clicking a chip on a card. Every value comes from the database, so it's escaped wherever it's shown.
const cF={q:"",pillar:"",mode:"",program:"",state:"",agency:"",tag:""};
const CSEL={pillar:["cPillar","All pillars"],mode:["cMode","All modes"],program:["cProgram","All programs"],state:["cState","All states"]};
const CLABEL={pillar:"Pillar",mode:"Mode",program:"Program",state:"State",agency:"Agency",tag:"Tag",q:"Search"};
function fillSelect(k,opts){
  const [id,all]=CSEL[k],sel=document.getElementById(id),cur=cF[k];
  const vals=opts.map(o=>o.value);if(cur&&!vals.includes(cur))opts=[{value:cur,count:0},...opts];
  sel.innerHTML='<option value="">'+all+'</option>'+opts.map(o=>'<option value="'+esc(o.value)+'"'+(o.value===cur?" selected":"")+'>'+esc(o.value)+' ('+o.count+')</option>').join("");
  sel.classList.toggle("on",!!cur);
}
async function loadFacets(){
  try{const r=await fetch("/api/collection/facets?status="+encodeURIComponent(cFilter));if(!r.ok)return;
    const d=await r.json();Object.keys(CSEL).forEach(k=>fillSelect(k,d[k]||[]));}catch(e){}
}
function setFilter(k,v){cF[k]=v;if(CSEL[k]){const s=document.getElementById(CSEL[k][0]);s.value=v;s.classList.toggle("on",!!v);}
  if(k==="q")document.getElementById("cQ").value=v;loadCollection();}
Object.keys(CSEL).forEach(k=>document.getElementById(CSEL[k][0]).addEventListener("change",e=>setFilter(k,e.target.value)));
let cQTimer;document.getElementById("cQ").addEventListener("input",e=>{clearTimeout(cQTimer);cQTimer=setTimeout(()=>setFilter("q",e.target.value.trim()),300);});
document.getElementById("cClear").onclick=()=>{Object.keys(cF).forEach(k=>cF[k]="");document.getElementById("cQ").value="";
  Object.keys(CSEL).forEach(k=>{const s=document.getElementById(CSEL[k][0]);s.value="";s.classList.remove("on");});loadCollection();};
function renderActive(matched,shown){
  const on=Object.keys(cF).filter(k=>cF[k]);const box=document.getElementById("cActive");
  box.innerHTML=(on.length?on.map(k=>'<button type="button" class="fpill" data-clear="'+k+'" title="Remove this filter">'+esc(CLABEL[k])+': '+esc(cF[k])+' &times;</button>').join(""):"")
    +(matched!=null?'<span>'+matched+' match'+(matched===1?"":"es")+(matched>shown?' (showing newest '+shown+')':'')+'</span>':"");
}
document.getElementById("cActive").addEventListener("click",e=>{const b=e.target.closest("[data-clear]");if(b)setFilter(b.dataset.clear,"");});
function facetChips(it){
  const chip=(k,v)=>'<button type="button" class="fchip k-'+k+'" data-fk="'+k+'" data-fv="'+esc(v)+'" title="Show only '+esc(CLABEL[k].toLowerCase())+': '+esc(v)+'">'+esc(v)+'</button>';
  const parts=[].concat(it.state?[chip("state",it.state)]:[],(it.mode||[]).map(v=>chip("mode",v)),(it.programs||[]).map(v=>chip("program",v)),
    (it.agencies||[]).map(v=>chip("agency",v)),(it.tags||[]).map(v=>chip("tag",v)));
  return parts.length?'<div class="facets">'+parts.join("")+'</div>':"";
}
const FCOLOR={Live:"#C0341F",Fresh:"#1F6B4A",Recent:"#1F6B7A",Aging:"#B07A1E",Stale:"#6A6458",Developing:"#3D5C8F",Expired:"#6A6458"};
// Uses the page's esc() above (it also escapes quotes, which attributes need). Feed links are
// untrusted: only http(s) URLs become links, so a "javascript:" link can't run on click.
function safeUrl(u){try{const x=new URL(String(u));return (x.protocol==="http:"||x.protocol==="https:")?x.href:null;}catch(_){return null;}}
async function loadCollection(){
  const out=document.getElementById("cOut");
  out.innerHTML='<div class="rcard"><div class="loading">Loading the queue...</div></div>';
  try{
    const qs=new URLSearchParams({status:cFilter});Object.keys(cF).forEach(k=>{if(cF[k])qs.set(k,cF[k]);});
    const r=await fetch("/api/collection?"+qs.toString());
    if(!r.ok){out.innerHTML='<div class="rcard"><div class="err">'+esc(errText(await r.text()))+'</div></div>';return;}
    const d=await r.json();
    renderActive(d.matched,(d.items||[]).length);
    const filtered=Object.keys(cF).some(k=>cF[k]);
    if(!d.items||!d.items.length){out.innerHTML='<div class="rcard"><div class="loading">'+(filtered?'No '+esc(cFilter)+' items match these filters.':'Nothing '+esc(cFilter)+'. Populate with: docker compose run --rm collect --run')+'</div></div>';return;}
    out.innerHTML=d.items.map(cCard).join("");
  }catch(e){out.innerHTML='<div class="rcard"><div class="err">Could not reach the queue.</div></div>';}
}
function cCard(it){
  const fc=FCOLOR[it.fresh_status]||"#6A6458";const acted=cFilter!=="pending";
  return '<div class="rcard" style="padding:16px 18px">'
    +'<div style="display:flex;gap:10px;align-items:center;margin-bottom:8px;font-family:Archivo,sans-serif;font-size:10px;font-weight:800;letter-spacing:1px;text-transform:uppercase">'
    +'<span style="color:var(--accent)">'+esc(it.pillar)+'</span>'
    +'<span style="color:'+fc+';border:1px solid '+fc+';border-radius:999px;padding:2px 8px">'+esc(it.fresh_status)+'</span>'
    +'<span style="color:var(--muted)">'+esc(it.relevance)+' relevance</span></div>'
    +'<div style="font-family:Archivo,sans-serif;font-weight:700;font-size:17px;line-height:1.3;margin-bottom:6px">'+esc(it.headline)+'</div>'
    +'<div style="font-size:14px;margin-bottom:10px">'+esc(it.summary)+'</div>'
    +facetChips(it)
    +'<div style="font-family:Archivo,sans-serif;font-size:12px;color:var(--muted);display:flex;gap:10px;flex-wrap:wrap;align-items:center">'
    +'<b style="color:var(--ink)">'+esc(it.source_name)+'</b>'+(it.published?'<span>'+esc(it.published)+'</span>':'')
    +(safeUrl(it.source_url)?'<a href="'+esc(safeUrl(it.source_url))+'" target="_blank" rel="noopener noreferrer">source</a>':'')+'</div>'
    +'<div style="display:flex;gap:8px;margin-top:12px">'
    +(acted?'<button class="ex" data-id="'+it.id+'" data-act="reset">Return to pending</button>'
           :'<button class="go" style="padding:9px 18px" data-id="'+it.id+'" data-act="approve">Approve</button><button class="ex" data-id="'+it.id+'" data-act="skip">Skip</button>')
    +'</div></div>';
}
async function cAct(id,action){try{await fetch("/api/collection/"+id+"/"+action,{method:"POST"});loadCollection();}catch(e){}}
document.getElementById("cOut").addEventListener("click",e=>{
  const f=e.target.closest("[data-fk]");if(f){setFilter(f.dataset.fk,f.dataset.fv);window.scrollTo({top:0,behavior:"smooth"});return;}
  const b=e.target.closest("[data-act]");if(b)cAct(b.dataset.id,b.dataset.act);});
// ---- Auto-collect switch (header) ----
const acSwitch=document.getElementById("acSwitch");
function when(iso){if(!iso)return "";const d=new Date(iso);return d.toLocaleString(undefined,{weekday:"short",month:"short",day:"numeric",hour:"numeric",minute:"2-digit"});}
function showAuto(s){
  acSwitch.disabled=false;acSwitch.setAttribute("aria-checked",s.enabled?"true":"false");
  document.getElementById("acTxt").textContent=s.enabled?"On":"Off";
  const lines=[s.enabled?"Daily collection is ON.":"Daily collection is OFF - the scheduled run is skipped, no tokens used."];
  if(s.schedule)lines.push("Schedule: daily at "+s.schedule.at+" ("+s.schedule.tz+")"+(s.enabled&&s.schedule.next_run?", next "+when(s.schedule.next_run):""));
  else lines.push("Scheduler hasn't reported in yet.");
  const lr=s.last_run;
  if(lr)lines.push("Last run "+when(lr.at)+": "+(lr.skipped?"skipped (off)":lr.error?"error - "+lr.error:(lr.added+" items added"+(lr.failed&&lr.failed.length?", failed: "+lr.failed.join(", "):""))));
  document.getElementById("acWrap").title=lines.join("\n");
}
async function loadAuto(){try{const r=await fetch("/api/auto-collect");if(r.ok)showAuto(await r.json());else document.getElementById("acTxt").textContent="?";}catch(e){document.getElementById("acTxt").textContent="?";}}
acSwitch.onclick=async()=>{
  const want=acSwitch.getAttribute("aria-checked")!=="true";acSwitch.disabled=true;
  try{const r=await fetch("/api/auto-collect",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({enabled:want})});
    if(r.ok)showAuto(await r.json());else{acSwitch.disabled=false;alert("Couldn't change auto-collect: "+errText(await r.text()));}}
  catch(e){acSwitch.disabled=false;alert("Couldn't reach the Command Center.");}
};
loadAuto();setInterval(loadAuto,60000);
// ---- Publish tab ----
document.getElementById("pRefresh").onclick=loadPublish;
// Public-site rebuild status (Cloudflare Pages deploy hook).
async function loadSiteStatus(){
  const el=document.getElementById("pSite");
  try{
    const s=await (await fetch("/api/site/rebuild")).json();
    const t=iso=>iso?new Date(iso).toLocaleString(undefined,{month:"short",day:"numeric",hour:"numeric",minute:"2-digit"}):"";
    let msg;
    if(!s.configured) msg=s.misconfigured?"Site rebuilds: CF_PAGES_DEPLOY_HOOK in .env doesn't look like a Cloudflare Pages deploy hook URL."
      :"Site rebuilds aren't set up yet (add CF_PAGES_DEPLOY_HOOK to .env on the NAS). Until then, redeploy in Cloudflare after publishing.";
    else if(s.pending_since) msg="Site rebuild scheduled ("+esc(s.reason||"")+") - it starts within a minute; the live site updates 1-2 minutes after that.";
    else if(s.last_at) msg="Last site rebuild: "+t(s.last_at)+" - "+(s.last_ok?"started OK":"failed: "+esc(s.last_detail||""))+".";
    else msg="Site rebuilds are set up. Publishing or unpublishing triggers one automatically.";
    el.innerHTML=msg;
    document.getElementById("pRebuild").disabled=!s.configured;
    if(s.pending_since)setTimeout(loadSiteStatus,15000);
  }catch(e){el.textContent="";}
}
document.getElementById("pRebuild").onclick=async()=>{
  const r=await fetch("/api/site/rebuild",{method:"POST"});
  if(!r.ok)alert(errText(await r.text()));
  loadSiteStatus();
};
document.querySelector('.tab[data-t="publish"]').addEventListener("click",loadPublish);
async function loadPublish(){
  const ready=document.getElementById("pReady"),posts=document.getElementById("pPosts");
  ready.innerHTML='<div class="rcard"><div class="loading">Loading...</div></div>';
  loadSiteStatus();
  try{const d=await (await fetch("/api/publish/ready")).json();
    ready.innerHTML=(d.items&&d.items.length)?d.items.map(pReadyCard).join(""):'<div class="rcard"><div class="loading">Nothing approved yet - approve items in the Collection tab.</div></div>';
  }catch(e){ready.innerHTML='<div class="rcard"><div class="err">Could not load approved items.</div></div>';}
  try{const d=await (await fetch("/api/posts")).json();
    posts.innerHTML=(d.posts&&d.posts.length)?d.posts.map(pPostRow).join(""):'<div class="rcard"><div class="loading">No published posts yet.</div></div>';
  }catch(e){posts.innerHTML='<div class="rcard"><div class="err">Could not load posts.</div></div>';}
}
function pReadyCard(it){
  return '<div class="rcard" style="padding:16px 18px">'
    +'<div style="font-family:Archivo,sans-serif;font-size:10px;font-weight:800;letter-spacing:1px;text-transform:uppercase;color:var(--accent);margin-bottom:6px">'+esc(it.pillar)+'</div>'
    +'<div style="font-family:Archivo,sans-serif;font-weight:700;font-size:17px;line-height:1.3;margin-bottom:6px">'+esc(it.headline)+'</div>'
    +'<div style="font-size:14px;margin-bottom:10px">'+esc(it.summary)+'</div>'
    +'<div style="display:flex;gap:10px;align-items:center"><button class="go" style="padding:9px 18px" data-pub="'+it.id+'">Publish</button>'
    +(safeUrl(it.source_url)?'<a href="'+esc(safeUrl(it.source_url))+'" target="_blank" rel="noopener noreferrer" style="font-family:Archivo,sans-serif;font-size:12px">source</a>':'')+'</div></div>';
}
function pPostRow(p){
  return '<div class="rcard" style="padding:14px 18px;display:flex;justify-content:space-between;align-items:center;gap:12px">'
    +'<div><div style="font-family:Archivo,sans-serif;font-size:10px;font-weight:800;letter-spacing:1px;text-transform:uppercase;color:var(--muted)">'+esc(p.pillar)+' - '+esc((p.publish_at||"").slice(0,10))+'</div>'
    +'<div style="font-family:Archivo,sans-serif;font-weight:700;font-size:16px;line-height:1.3">'+esc(p.title)+'</div></div>'
    +'<button class="ex" data-unpub="'+p.id+'">Unpublish</button></div>';
}
async function pPost(url,what,b){ // POST, and say so if it didn't work instead of silently reloading
  b.disabled=true;
  try{const r=await fetch(url,{method:"POST"});if(!r.ok)alert("Couldn't "+what+": "+errText(await r.text()));}
  catch(e){alert("Couldn't reach the Command Center.");}
  loadPublish();
}
document.getElementById("pReady").addEventListener("click",e=>{const b=e.target.closest("[data-pub]");if(b)pPost("/api/publish/"+b.dataset.pub,"publish",b);});
document.getElementById("pPosts").addEventListener("click",e=>{const b=e.target.closest("[data-unpub]");if(b)pPost("/api/posts/"+b.dataset.unpub+"/unpublish","unpublish",b);});
// ---- CIG Pipeline tab ----
let gPhase="";
document.getElementById("gRefresh").onclick=loadCIG;
// Upload a dashboard PDF downloaded in the browser; the server parses and loads it.
function gNote(kind,text){document.getElementById("gMsg").innerHTML='<div class="rcard"><div class="'+kind+'">'+esc(text)+'</div></div>';}
document.getElementById("gUploadBtn").onclick=()=>document.getElementById("gFile").click();
document.getElementById("gFile").addEventListener("change",async e=>{
  const f=e.target.files[0];e.target.value="";if(!f)return;
  const btn=document.getElementById("gUploadBtn");btn.disabled=true;gNote("loading","Reading "+f.name+"...");
  try{
    const r=await fetch("/api/cig/upload?filename="+encodeURIComponent(f.name),{method:"POST",headers:{"Content-Type":"application/pdf"},body:f});
    const t=await r.text();
    if(!r.ok){gNote("err","Not loaded: "+errText(t));}
    else{const d=JSON.parse(t);gNote("loading","Loaded "+d.projects+" projects"+(d.snapshot?" (snapshot "+d.snapshot+")":"")+"."+(d.note?" "+d.note:""));loadCIG();}
  }catch(err){gNote("err","Couldn't reach the Command Center.");}
  btn.disabled=false;
});
// Or paste the PDF's link: the server fetches it from transit.dot.gov and loads it the same way.
document.getElementById("gLinkForm").addEventListener("submit",async e=>{
  e.preventDefault();
  const inp=document.getElementById("gLink"),url=inp.value.trim();if(!url)return;
  const btn=document.getElementById("gLinkBtn");btn.disabled=true;gNote("loading","Downloading and reading the dashboard...");
  try{
    const r=await fetch("/api/cig/load-url",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({url})});
    const t=await r.text();
    if(!r.ok){gNote("err","Not loaded: "+errText(t));}
    else{const d=JSON.parse(t);inp.value="";gNote("loading","Loaded "+d.projects+" projects"+(d.snapshot?" (snapshot "+d.snapshot+")":"")+"."+(d.note?" "+d.note:""));loadCIG();}
  }catch(err){gNote("err","Couldn't reach the Command Center.");}
  btn.disabled=false;
});
document.querySelector('.tab[data-t="grants"]').addEventListener("click",loadCIG);
const gChips=document.getElementById("gChips");
[["","All phases"],["PD","Project Development"],["Eng","Engineering"]].forEach(([k,lbl])=>{
  const b=document.createElement("button");b.className="ex";b.textContent=lbl;
  b.onclick=()=>{gPhase=k;document.querySelectorAll("#gChips .ex").forEach(x=>x.style.borderColor=(x===b?"var(--accent)":""));loadCIG();};
  if(k==="")b.style.borderColor="var(--accent)";gChips.appendChild(b);});
function gStat(v,l){return '<div><div style="font-family:Archivo,sans-serif;font-weight:900;font-size:22px">'+v+'</div><div style="font-family:Archivo,sans-serif;font-size:11px;letter-spacing:.5px;text-transform:uppercase;color:var(--muted)">'+l+'</div></div>';}
async function loadCIG(){
  const out=document.getElementById("gOut"),sum=document.getElementById("gSummary");
  out.innerHTML='<div class="rcard"><div class="loading">Loading the pipeline...</div></div>';
  loadChanges();loadLoads();
  try{
    const q=gPhase?("?phase="+encodeURIComponent(gPhase)):"";
    const d=await (await fetch("/api/cig"+q)).json();
    const s=d.summary||{},bp=s.by_phase||{};
    sum.innerHTML='<div class="rcard" style="padding:16px 18px;display:flex;gap:26px;flex-wrap:wrap;align-items:center">'
      +gStat(s.projects||0,"projects")+gStat("$"+(((s.total_cig_musd||0)/1000).toFixed(1))+"B","CIG requested")
      +gStat(bp.PD||0,"in development")+gStat(bp.Eng||0,"in engineering")
      +(()=>{const age=s.snapshot?Math.floor((Date.now()-new Date(s.snapshot+"T12:00:00"))/864e5):null;const stale=age!=null&&age>45;
        return '<div style="font-family:Archivo,sans-serif;font-size:11px;margin-left:auto;color:'+(stale?"var(--accent)":"var(--muted)")+'"'
          +(stale?' title="Download the newest dashboard at transit.dot.gov/CIG and upload it"':'')+'>snapshot '+esc(s.snapshot||"-")
          +(stale?' &middot; '+age+' days old - time to upload a new one':'')
          +(s.snapshots>1?' &middot; '+s.snapshots+' months of history':'')+'</div>';})()+'</div>';
    if(!d.projects||!d.projects.length){out.innerHTML='<div class="rcard"><div class="loading">'+(s.projects?'No projects in this phase.':'No projects loaded yet. Download the CIG dashboard PDF at transit.dot.gov/CIG, then click Upload dashboard.')+'</div></div>';return;}
    // Shared component (static/t411.js): the same table, milestones and history the public site uses.
    T411.renderCigTable(out, d.projects);
  }catch(e){out.innerHTML='<div class="rcard"><div class="err">Could not load the pipeline.</div></div>';}
}

// ---- What changed (shared component) ----
let gSince="";
async function loadChanges(){
  const box=document.getElementById("gChanges");
  try{
    const r=await fetch("/api/cig/changes"+(gSince?"?since="+encodeURIComponent(gSince):""));
    if(!r.ok){box.innerHTML="";return;}
    const d=await r.json();
    const older=(d.snapshots||[]).filter(s=>d.to&&s<d.to);
    const sel=older.length>1?'<label style="font-family:Archivo,sans-serif;font-size:12px;color:var(--muted)">Compare with '
      +'<select id="gSince" style="font-family:Archivo,sans-serif;font-size:12px;padding:4px 6px;border:1px solid var(--line);border-radius:6px;background:var(--card)">'
      +older.map(s=>'<option value="'+esc(s)+'"'+(s===d.from?" selected":"")+'>'+esc(s)+(s===older[0]?" (previous)":"")+'</option>').join("")+'</select></label>':"";
    T411.renderCigChanges(box,d,{headerExtra:sel,onProject:(name,sponsor)=>{
      if(!T411.openCigProject(document.getElementById("gOut"),name,sponsor))
        gNote("loading",name+" isn't in the current table"+(gPhase?" (try All phases)":" (it was dropped from the latest dashboard)")+".");
    }});
    const s=document.getElementById("gSince");if(s)s.onchange=e=>{gSince=e.target.value;loadChanges();};
  }catch(e){box.innerHTML="";}
}

// ---- Dashboard files: every load or refusal, with the kept PDF ----
const GSRC={upload:"Upload",link:"Link","command line":"Command line","before tracking":"Before tracking"};
function gSize(b){return b?(b/1048576).toFixed(1)+" MB":"";}
async function loadLoads(){
  const box=document.getElementById("gLoads"),cnt=document.getElementById("gLoadsCount");
  try{
    const r=await fetch("/api/cig/loads");if(!r.ok){box.innerHTML='<div class="err">Could not load the file list.</div>';return;}
    const d=await r.json(),L=d.loads||[];
    cnt.textContent="("+L.length+")";
    if(!L.length){box.innerHTML='<div class="loading" style="padding:0">No dashboard loads recorded yet.</div>';return;}
    box.innerHTML='<div class="t411-scroll" style="padding:0"><table class="t411-table"><thead><tr><th>Loaded</th><th>Snapshot</th><th>How</th><th>File / link</th><th>Projects</th><th>Result</th><th>PDF</th></tr></thead><tbody>'
      +L.map(x=>{
        const url=safeUrl(x.name);
        const name=url?'<a href="'+esc(url)+'" target="_blank" rel="noopener noreferrer" title="'+esc(x.name)+'">'+esc(x.name.split("/").pop())+'</a>':esc(x.name||"-");
        const ok=x.status==="loaded";
        const res=ok?'<span style="color:#1F6B4A;font-weight:700">Loaded</span>'
          :'<span style="color:var(--accent);font-weight:700" title="'+esc(x.message||"")+'">Refused</span> <span style="color:var(--muted);white-space:normal">'+esc((x.message||"").split(";")[0])+'</span>';
        const pdf=x.has_file?'<a href="/api/cig/loads/'+x.id+'/file" target="_blank" rel="noopener">Open</a> <span style="color:var(--muted)">'+gSize(x.file_bytes)+'</span>':'<span style="color:var(--muted)">-</span>';
        return '<tr><td>'+esc(x.loaded_at)+'</td><td>'+esc(x.snapshot_date||"-")+'</td><td>'+esc(GSRC[x.source]||x.source||"-")+'</td><td>'+name+'</td><td class="num">'+(x.projects!=null?x.projects:"-")+'</td><td>'+res+'</td><td>'+pdf+'</td></tr>';
      }).join("")+'</tbody></table></div>';
  }catch(e){box.innerHTML='<div class="err">Could not load the file list.</div>';}
}

// ---- Ask CIG tab ----
const CIGEX=["Which projects want the most CIG funding?","BRT projects seeking over $100M","Projects in Engineering rated Medium or better","Projects in Texas"];
const cigEx=document.getElementById("cigEx");
CIGEX.forEach(t=>{const b=document.createElement("button");b.className="ex";b.textContent=t;b.onclick=()=>{document.getElementById("cigQ").value=t;cigDoAsk(t);};cigEx.appendChild(b);});
document.getElementById("cigAskForm").onsubmit=e=>{e.preventDefault();const v=document.getElementById("cigQ").value.trim();if(v)cigDoAsk(v);};
async function cigDoAsk(q){
  const out=document.getElementById("cigAskOut");
  out.innerHTML='<div class="rcard"><div class="loading">Reading \u201c'+esc(q)+'\u201d and running the query...</div></div>';
  try{
    const r=await fetch("/api/cig/ask",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({question:q})});
    if(!r.ok){out.innerHTML='<div class="rcard"><div class="err">'+esc(errText(await r.text()))+'</div></div>';return;}
    const d=await r.json();
    const sqlBlock='<details style="margin:6px 18px 14px"><summary style="cursor:pointer;font-family:Archivo,sans-serif;font-size:12px;font-weight:700;color:var(--muted)">View the query it ran</summary><pre style="white-space:pre-wrap;font-family:JetBrains Mono,monospace;font-size:12px;padding:8px 0;color:var(--ink)">'+esc(d.sql||"")+'</pre></details>';
    if(!d.rows||!d.rows.length){out.innerHTML='<div class="rcard"><div class="rh" style="background:var(--ink);color:var(--panel);padding:11px 18px;font-family:Archivo,sans-serif;font-size:13px">'+esc(q)+' &middot; no rows</div>'+sqlBlock+'</div>';return;}
    const th=d.columns.map(c=>"<th>"+esc(c)+"</th>").join("");
    const tb=d.rows.map(row=>"<tr>"+row.map(v=>'<td>'+esc(v==null?"":v)+"</td>").join("")+"</tr>").join("");
    out.innerHTML='<div class="rcard"><div class="rh" style="background:var(--ink);color:var(--panel);padding:11px 18px;font-family:Archivo,sans-serif;font-size:13px">'+esc(q)+' &middot; '+d.rows.length+' rows</div>'
      +'<div class="twrap" style="padding:6px 18px;overflow-x:auto"><table style="width:100%;border-collapse:collapse;font-size:13px;font-family:Archivo,sans-serif"><thead><tr>'+th+'</tr></thead><tbody>'+tb+'</tbody></table></div>'+sqlBlock+'</div>';
  }catch(e){out.innerHTML='<div class="rcard"><div class="err">Could not reach Ask CIG.</div></div>';}
}
refreshStatus();setInterval(refreshStatus,15000);
</script></body></html>"""
