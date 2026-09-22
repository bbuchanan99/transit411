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


def _ensure_slugs(cur):
    """Every post needs a stable slug: it is its page on the site (/article/<slug>). Fills in any
    missing one from the title and id, and keeps them unique."""
    cur.execute("""UPDATE content_posts SET slug = left(regexp_replace(lower(coalesce(title,'post')),
                     '[^a-z0-9]+', '-', 'g'), 60) || '-' || id
                   WHERE slug IS NULL OR btrim(slug) = ''""")
    cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS content_posts_slug_uniq ON content_posts (slug)")


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


def _publish_one(cur, item_id):
    """Turn one approved collected item into a published post. Returns the post id, or None if the item
    isn't (or is no longer) approved. The caller commits."""
    import re
    cur.execute("SELECT pillar, headline, summary, source_name, source_url, "
                "agencies, mode, programs, tags, state "
                "FROM collected_items WHERE id=%s AND status='approved' FOR UPDATE", (item_id,))
    row = cur.fetchone()
    if not row:
        return None
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
    return post_id


class PublishAll(BaseModel):
    ids: list[int]


@app.post("/api/publish-all")
def publish_all(p: PublishAll):
    """The Publish tab's "Publish all": publish exactly the items the page showed (so anything approved
    after it loaded waits for the next look), in one transaction, with a single site rebuild."""
    ids = list(dict.fromkeys(p.ids))[:200]
    if not ids:
        raise HTTPException(400, "Nothing to publish.")
    published, skipped = [], []
    try:
        with _db() as c, c.cursor() as cur:
            for item_id in ids:
                post_id = _publish_one(cur, item_id)
                if post_id:
                    published.append({"item_id": item_id, "post_id": post_id})
                else:
                    skipped.append(item_id)  # already published, skipped or unapproved meanwhile
            c.commit()
    except Exception as e:
        raise HTTPException(502, f"DB error (nothing was published): {e}")
    if published:
        _request_site_rebuild(f"{len(published)} posts published")
    return {"published": len(published), "skipped": skipped, "posts": published, "rebuild": _rebuild_state()}


@app.post("/api/publish/{item_id}")
def publish_item(item_id: int):
    try:
        with _db() as c, c.cursor() as cur:
            post_id = _publish_one(cur, item_id)
            if not post_id:
                raise HTTPException(404, "no approved item with that id")
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
          featured: Optional[bool] = None, slug: Optional[str] = None):
    """Published posts. `slug` returns just that post (its page on the site is /article/<slug>)."""
    where, params = ["status='published'"], []
    if slug:
        where.append("slug=%s"); params.append(slug)
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
            _ensure_slugs(cur)
            c.commit()
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


@app.get("/api/posts/{slug}")
def post_by_slug(slug: str):
    """One published post by its slug - the article page at /article/<slug>."""
    found = posts(slug=slug)["posts"]
    if not found:
        raise HTTPException(404, "No published post with that slug.")
    return found[0]


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
    empty = {"summary": {"snapshot": None, "projects": 0, "total_cig_musd": 0.0, "by_phase": {}, "by_mode": {},
                         "snapshots": 0}, "projects": []}
    out = []
    try:
        with _db() as c, c.cursor() as cur:
            cur.execute("SELECT to_regclass('cig_projects') IS NOT NULL")
            if not cur.fetchone()[0]:  # nothing loaded yet: an empty pipeline, not an error
                return empty
            cigmod.create_table(c)  # adds milestone columns to a table created before they existed
            import cig_profiles
            cig_profiles.create_tables(c)
            cur.execute("SELECT max(snapshot_date), count(DISTINCT snapshot_date) FROM cig_projects")
            snap, n_snaps = cur.fetchone()
            if not snap:
                return empty
            where, params = ["p.snapshot_date=%s"], [snap]
            if mode == "Unspecified":  # FTA's sources don't state one (mode IS NULL)
                where.append("p.mode IS NULL"); mode = None
            for col, val in (("phase", phase), ("mode", mode), ("rating", rating), ("state", state)):
                if val:
                    where.append(f"p.{col}=%s"); params.append(val)
            if sponsor:
                where.append("p.sponsor ILIKE %s"); params.append(f"%{sponsor}%")
            cur.execute("SELECT count(*), COALESCE(sum(cig_request_musd),0) FROM cig_projects WHERE snapshot_date=%s",
                        (snap,))
            total_projects, total_cig = cur.fetchone()
            cur.execute("SELECT phase, count(*) FROM cig_projects WHERE snapshot_date=%s GROUP BY phase", (snap,))
            by_phase = {ph: n for ph, n in cur.fetchall()}
            cur.execute("SELECT coalesce(mode,'Unspecified'), count(*) FROM cig_projects WHERE snapshot_date=%s "
                        "GROUP BY 1 ORDER BY 2 DESC, 1", (snap,))
            by_mode = {m: n for m, n in cur.fetchall()}
            # profile_url = the project's page on FTA's Current CIG Projects list (cig_profile_pages);
            # profile_versions / profile_changed_at summarize its archived profile versions.
            cur.execute("SELECT p.id, p.project_name, p.sponsor, p.city, p.state, p.mode, p.phase, p.cost_musd, "
                        "p.cost_raw, p.cig_request_musd, p.cig_request_raw, p.cig_share, p.rating, p.noncig_status, "
                        "p.est_grant, p.nepa, p.pd_entry, p.eng_entry, p.lonp_req, p.lonp_dec, p.lonp_action, "
                        "p.req_rating_date, p.proj_rating_date, p.mode_source, p.profile_file, g.profile_url, "
                        "v.n AS profile_versions, v.changed_at AS profile_changed_at "
                        "FROM cig_projects p LEFT JOIN cig_profile_pages g "
                        "ON g.project_name=p.project_name AND g.sponsor=coalesce(p.sponsor,'') "
                        "LEFT JOIN (SELECT project_name, sponsor, count(*) AS n, "
                        "  to_char(max(captured_at) FILTER (WHERE changed), 'YYYY-MM-DD') AS changed_at "
                        "  FROM cig_profile_versions GROUP BY 1, 2) v "
                        "ON v.project_name=p.project_name AND v.sponsor=coalesce(p.sponsor,'') WHERE "
                        + " AND ".join(where) + " ORDER BY p.cig_request_musd DESC NULLS LAST, p.project_name", params)
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
                        "by_phase": by_phase, "by_mode": by_mode, "snapshots": n_snaps}, "projects": out}


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


@app.get("/api/cig/profile")
def cig_profile(file: str):
    """An FTA project profile PDF (the source of a project's mode), by the profile_file on its /api/cig row."""
    import cig as cigmod
    from fastapi.responses import FileResponse
    path = cigmod.profile_path(file)
    if not path:
        raise HTTPException(404, "No FTA profile with that name.")
    return FileResponse(path, media_type="application/pdf", content_disposition_type="inline",
                        filename=os.path.basename(path))


# ---- Contacts: the master list we own (contacts.py). Command Center only, except /api/subscribe ----
class ContactIn(BaseModel):
    email: Optional[str] = None
    name: Optional[str] = None
    tags: Optional[List[str]] = None
    source: Optional[str] = None
    status: Optional[str] = None


CONTACT_COLS = ["id", "email", "name", "status", "source", "tags", "created_at", "confirmed_at",
                "unsubscribed_at", "last_event_at", "last_event"]


def _contact_json(row):
    d = dict(zip(CONTACT_COLS, row))
    for k in ("created_at", "confirmed_at", "unsubscribed_at", "last_event_at"):
        d[k] = d[k].isoformat() if d.get(k) else None
    return d


@app.get("/api/contacts")
def list_contacts(status: Optional[str] = None, tag: Optional[str] = None, q: Optional[str] = None,
                  limit: int = 200, offset: int = 0):
    """The contact list with counts by status and the tags in use."""
    import contacts as cmod
    where, params = [], []
    if status:
        where.append("status=%s"); params.append(status)
    if tag:
        where.append("%s = ANY(tags)"); params.append(tag)
    if q:
        where.append("(email ILIKE %s OR coalesce(name,'') ILIKE %s)")
        params += [f"%{q.strip()}%"] * 2
    try:
        with _db() as c, c.cursor() as cur:
            cmod.create_tables(c)
            cur.execute(f"SELECT {', '.join(CONTACT_COLS)} FROM contacts"
                        + (" WHERE " + " AND ".join(where) if where else "")
                        + " ORDER BY created_at DESC, id DESC LIMIT %s OFFSET %s",
                        params + [max(1, min(limit, 1000)), max(0, offset)])
            rows = [_contact_json(r) for r in cur.fetchall()]
            cur.execute("SELECT count(*) FROM contacts" + (" WHERE " + " AND ".join(where) if where else ""), params)
            matching = cur.fetchone()[0]
            cur.execute("SELECT DISTINCT unnest(tags) FROM contacts ORDER BY 1")
            tags = [r[0] for r in cur.fetchall()]
            stats = cmod.counts(c)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"DB error: {e}")
    return {"contacts": rows, "matching": matching, "counts": stats, "tags": tags, "statuses": cmod.STATUSES}


@app.post("/api/contacts")
def add_contact(c_in: ContactIn):
    """Add one contact by hand (status defaults to subscribed here - you added them deliberately)."""
    import contacts as cmod
    status = c_in.status or "subscribed"
    if status not in cmod.STATUSES:
        raise HTTPException(400, f"status must be one of: {', '.join(cmod.STATUSES)}")
    try:
        with _db() as c:
            cid, what = cmod.upsert(c, c_in.email, c_in.name, c_in.source or "manual", c_in.tags or [], status)
            if what == "invalid":
                raise HTTPException(400, "That doesn't look like an email address.")
            if what == "suppressed":
                raise HTTPException(409, "That address unsubscribed, bounced or complained; it stays suppressed.")
            with c.cursor() as cur:
                cur.execute(f"SELECT {', '.join(CONTACT_COLS)} FROM contacts WHERE id=%s", (cid,))
                return {"contact": _contact_json(cur.fetchone()), "result": what}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"DB error: {e}")


@app.put("/api/contacts/{contact_id}")
def update_contact(contact_id: int, c_in: ContactIn):
    """Edit a contact's name, tags or status. Setting a suppressed status also adds a suppression."""
    import contacts as cmod
    try:
        with _db() as c, c.cursor() as cur:
            cmod.create_tables(c)
            cur.execute("SELECT id FROM contacts WHERE id=%s", (contact_id,))
            if not cur.fetchone():
                raise HTTPException(404, "No such contact.")
            if c_in.name is not None:
                cur.execute("UPDATE contacts SET name=%s WHERE id=%s", (c_in.name.strip() or None, contact_id))
            if c_in.tags is not None:
                clean = sorted({t.strip()[:40] for t in c_in.tags if t and t.strip()})
                cur.execute("UPDATE contacts SET tags=%s WHERE id=%s", (clean, contact_id))
            c.commit()
            if c_in.status:
                cmod.set_status(c, contact_id, c_in.status, event="edited in Command Center")
            cur.execute(f"SELECT {', '.join(CONTACT_COLS)} FROM contacts WHERE id=%s", (contact_id,))
            return {"contact": _contact_json(cur.fetchone())}
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(502, f"DB error: {e}")


@app.post("/api/contacts/import")
async def import_contacts(request: Request, source: str = "csv import", tags: str = ""):
    """Import a CSV (raw body). Dedupes on email; suppressed addresses are never re-added."""
    import contacts as cmod
    body = await request.body()
    if len(body) > 5 * 1024 * 1024:
        raise HTTPException(413, "That file is over 5 MB.")
    text = body.decode("utf-8-sig", errors="replace")
    default_tags = [t.strip() for t in tags.split(",") if t.strip()]
    try:
        with _db() as c:
            return cmod.import_csv(c, text, source, default_tags)
    except Exception as e:
        raise HTTPException(422, f"Couldn't read that CSV ({type(e).__name__}: {e}).")


@app.get("/api/contacts/export.csv")
def export_contacts(status: Optional[str] = None):
    from fastapi.responses import Response as FileResp
    import contacts as cmod
    try:
        with _db() as c:
            csv_text = cmod.export_csv(c, status)
    except Exception as e:
        raise HTTPException(502, f"DB error: {e}")
    stamp = datetime_now().strftime("%Y-%m-%d")
    return FileResp(csv_text, media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="transit411-contacts-{stamp}.csv"'})


class Subscribe(BaseModel):
    email: str
    name: Optional[str] = None
    source: Optional[str] = None


@app.post("/api/subscribe")
def subscribe(s: Subscribe):
    """Public newsletter signup (the only public write, proxied by readonly-api). Creates a PENDING
    contact - nothing is sent yet and nobody is subscribed until they confirm (phase 2). The reply is
    deliberately the same whether or not the address is already on the list or suppressed, so this
    can't be used to find out who is."""
    import contacts as cmod
    import email_sender as sender
    try:
        with _db() as c:
            cid, what = cmod.upsert(c, s.email, (s.name or "")[:120], (s.source or "site")[:60], [], "pending")
            if what == "added" and sender.configured():
                # Double opt-in: send the confirmation now. A send failure is logged, not surfaced -
                # the contact is saved either way and the email can be re-sent from the Contacts tab.
                try:
                    with c.cursor() as cur:
                        cur.execute("SELECT id, email, name, confirm_token, unsub_token FROM contacts WHERE id=%s", (cid,))
                        row = cur.fetchone()
                    sender.send_confirmation(c, dict(zip(("id", "email", "name", "confirm_token", "unsub_token"), row)))
                except Exception as e:
                    sender.log_event(c, cmod.normalize(s.email), "error", detail=f"confirmation: {type(e).__name__}: {e}"[:300])
    except Exception as e:
        raise HTTPException(502, f"DB error: {e}")
    if what == "invalid":
        raise HTTPException(400, "Please enter a valid email address.")
    return {"ok": True, "status": "pending",
            "message": "Thanks — check your inbox for a confirmation link."}


# ---- Email: double opt-in, unsubscribe and the SES feedback loop (email_sender.py) ---------------
# /confirm, /unsubscribe and /api/email/sns are the only email paths the tunnel exposes.
PUBLIC_SITE = os.environ.get("PUBLIC_SITE_URL", "https://transit411.net").rstrip("/")


def esc_html(s):
    return (str(s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;"))


def _page(title, body, ok=True):
    """A small self-contained page for links people click from an email."""
    from fastapi.responses import HTMLResponse
    accent = "#1F6B4A" if ok else "#C0341F"
    return HTMLResponse(
        "<!doctype html><html><head><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
        "<title>" + esc_html(title) + " - Transit411</title></head>"
        "<body style='margin:0;background:#F2EEE4;color:#17140F;font-family:Georgia,serif'>"
        "<div style='max-width:560px;margin:8vh auto;background:#fff;border:1px solid #D8D2C4;padding:32px'>"
        "<div style='font-family:Arial,sans-serif;font-weight:800;font-size:22px;letter-spacing:-.5px'>"
        "TRANSIT<span style='color:#C0341F'>411</span></div>"
        "<h1 style='font-family:Arial,sans-serif;font-size:21px;margin:18px 0 10px;color:" + accent + "'>"
        + esc_html(title) + "</h1>"
        "<div style='font-size:16px;line-height:1.6'>" + body + "</div>"
        "<p style='margin-top:26px'><a href='" + PUBLIC_SITE + "' style='color:#C0341F;font-family:Arial,sans-serif;"
        "font-weight:700'>Go to Transit411</a></p></div></body></html>")


@app.get("/confirm")
def confirm(t: str = ""):
    """Double opt-in: the link in the confirmation email. Only this makes someone subscribed."""
    import contacts as cmod
    if not t or len(t) > 100:
        return _page("That link doesn't look right", "<p>Please use the button in the confirmation email.</p>", False)
    try:
        with _db() as c, c.cursor() as cur:
            cmod.create_tables(c)
            cur.execute("SELECT id, email, status FROM contacts WHERE confirm_token=%s", (t,))
            row = cur.fetchone()
            if not row:
                return _page("That link has expired", "<p>We couldn't match that confirmation link. Sign up again "
                             "on the site and we'll send a fresh one.</p>", False)
            cid, email, status = row
            if cmod.is_suppressed(c, email):
                return _page("This address is unsubscribed", "<p>" + esc_html(email) + " asked not to receive email "
                             "from us, so we've left it that way. Sign up again if that was a mistake.</p>", False)
            if status != "subscribed":
                cmod.set_status(c, cid, "subscribed", event="confirmed opt-in")
    except Exception as e:
        raise HTTPException(502, f"DB error: {e}")
    return _page("You're subscribed", "<p>Thanks &mdash; <strong>" + esc_html(email) + "</strong> is confirmed for "
                 "Transit411 Weekly Intelligence, every Thursday.</p><p>Every issue has a one-click unsubscribe link.</p>")


def _unsubscribe_token(t):
    """Mark the holder of this token unsubscribed and suppress the address. Returns the address."""
    import contacts as cmod
    with _db() as c, c.cursor() as cur:
        cmod.create_tables(c)
        cur.execute("SELECT id, email, status FROM contacts WHERE unsub_token=%s", (t,))
        row = cur.fetchone()
        if not row:
            return None
        cid, email, status = row
        if status != "unsubscribed":
            cmod.set_status(c, cid, "unsubscribed", event="unsubscribed via link")
        else:
            cmod.suppress(c, email, "unsubscribed")
        return email


@app.get("/unsubscribe")
def unsubscribe(t: str = ""):
    """One click, done - no confirmation step, no login. The address is suppressed for good."""
    if not t or len(t) > 100:
        return _page("That link doesn't look right", "<p>Please use the unsubscribe link in the email.</p>", False)
    try:
        email = _unsubscribe_token(t)
    except Exception as e:
        raise HTTPException(502, f"DB error: {e}")
    if not email:
        return _page("Already unsubscribed", "<p>We couldn't match that link, which usually means the address is "
                     "already off the list.</p>")
    return _page("You're unsubscribed", "<p><strong>" + esc_html(email) + "</strong> has been removed and added to "
                 "our do-not-email list. You won't get Transit411 email again unless you sign up afresh.</p>")


@app.post("/unsubscribe")
async def unsubscribe_one_click(request: Request, t: str = ""):
    """RFC 8058 one-click: the mail client POSTs here from the List-Unsubscribe-Post header."""
    import re as _re
    if not t:
        body = (await request.body()).decode("utf-8", errors="replace")
        m = _re.search(r"(?:^|&)t=([^&\s]+)", body)
        t = m.group(1) if m else ""
    if not t:
        raise HTTPException(400, "Missing token.")
    try:
        email = _unsubscribe_token(t)
    except Exception as e:
        raise HTTPException(502, f"DB error: {e}")
    return {"ok": True, "unsubscribed": bool(email)}


@app.post("/api/email/sns")
async def email_sns(request: Request):
    """SES bounce and complaint notifications, via SNS. Every message is verified against the SNS
    signing certificate before it can change anything; a hard bounce or a complaint suppresses the
    address permanently."""
    import asyncio
    import json
    import email_sender as sender
    raw = await request.body()
    if len(raw) > 512 * 1024:
        raise HTTPException(413, "Too large.")
    try:
        msg = json.loads(raw.decode("utf-8", errors="replace"))
    except ValueError:
        raise HTTPException(400, "Expected an SNS JSON message.")
    if not await asyncio.to_thread(sender.verify_sns, msg):
        raise HTTPException(403, "That message didn't verify as coming from SNS.")

    def work():
        with _db() as c:
            return sender.handle_sns(c, msg)
    try:
        return await asyncio.to_thread(work)
    except Exception as e:
        raise HTTPException(502, f"Couldn't process that notification: {type(e).__name__}")


@app.get("/api/email/status")
def email_status():
    """Is SES wired up, and what has it done lately (Command Center only)."""
    import email_sender as sender
    out = {"configured": sender.configured(), "region": sender.REGION, "from": sender.FROM,
           "link_base": sender.LINK_BASE, "max_per_second": sender.MAX_PER_SECOND,
           "topic_arn": sender.SNS_TOPIC_ARN or None, "events": [], "by_type": {}}
    try:
        with _db() as c, c.cursor() as cur:
            sender.create_tables(c)
            cur.execute("SELECT type, count(*) FROM email_events GROUP BY type ORDER BY 2 DESC")
            out["by_type"] = {t: n for t, n in cur.fetchall()}
            cur.execute("SELECT to_char(created_at AT TIME ZONE 'America/New_York','YYYY-MM-DD HH24:MI'), "
                        "email, type, detail FROM email_events ORDER BY id DESC LIMIT 25")
            out["events"] = [dict(zip(("at", "email", "type", "detail"), r)) for r in cur.fetchall()]
    except Exception as e:
        raise HTTPException(502, f"DB error: {e}")
    return out


@app.post("/api/contacts/{contact_id}/confirmation")
async def send_confirmation_email(contact_id: int):
    """Send (or re-send) the double opt-in email to one contact."""
    import asyncio
    import email_sender as sender

    def work():
        with _db() as c, c.cursor() as cur:
            cur.execute("SELECT id, email, name, confirm_token, unsub_token, status FROM contacts WHERE id=%s",
                        (contact_id,))
            row = cur.fetchone()
            if not row:
                raise HTTPException(404, "No such contact.")
            contact = dict(zip(("id", "email", "name", "confirm_token", "unsub_token", "status"), row))
            return sender.send_confirmation(c, contact), contact["email"]
    try:
        mid, email = await asyncio.to_thread(work)
        return {"sent": True, "email": email, "message_id": mid}
    except HTTPException:
        raise
    except sender.NotConfigured as e:
        raise HTTPException(400, str(e))
    except ValueError as e:
        raise HTTPException(409, str(e))
    except Exception as e:
        raise HTTPException(502, f"SES error: {type(e).__name__}: {e}")


class TestEmail(BaseModel):
    email: str


@app.post("/api/email/test")
async def email_test(t: TestEmail):
    """Send a one-off test message (in the SES sandbox, only to addresses verified in SES)."""
    import asyncio
    import contacts as cmod
    import email_sender as sender
    to = cmod.normalize(t.email)
    if not to:
        raise HTTPException(400, "That doesn't look like an email address.")

    def work():
        with _db() as c:
            if cmod.is_suppressed(c, to):
                raise ValueError("That address is on the do-not-email list.")
            return sender.send_test(c, to)
    try:
        return {"sent": True, "email": to, "message_id": await asyncio.to_thread(work)}
    except sender.NotConfigured as e:
        raise HTTPException(400, str(e))
    except ValueError as e:
        raise HTTPException(409, str(e))
    except Exception as e:
        raise HTTPException(502, f"SES error: {type(e).__name__}: {e}")


# ---- Sources tab: the collector's registry (sources table), editable here ------------------------
# Command Center only (not in readonly-api's allowlist). collection.py reads the table fresh on every
# run, so changes apply to the next run; --seed leaves rows edited, added or deleted here alone.
SOURCE_PILLARS = ["Funding", "Procurement", "People", "Policy", "Data"]
SOURCE_TYPES = ["Official", "Trade", "Association", "Aggregator", "Agency", "Data", "Search"]
SOURCE_METHODS = ["RSS", "API", "Scrape", "Manual", "Retired"]  # only RSS is fetched today
SOURCE_TRUST = ["High", "Med", "Low"]
SOURCE_COLS = ["id", "name", "url", "pillar", "type", "method", "trust", "notes", "enabled", "origin", "query",
               "search_days", "edited_at", "last_fetched_at", "last_ok_at", "last_error", "last_error_at",
               "fail_streak", "last_entries", "last_new", "last_queued"]


class SourceIn(BaseModel):
    kind: Optional[str] = None          # 'feed' or 'search' (create only)
    name: Optional[str] = None
    url: Optional[str] = None
    query: Optional[str] = None         # keyword searches: the Google News query...
    search_days: Optional[int] = None   # ...and how many days back it looks
    pillar: Optional[str] = None
    type: Optional[str] = None
    method: Optional[str] = None
    trust: Optional[str] = None
    notes: Optional[str] = None
    enabled: Optional[bool] = None


def _source_fields(s: SourceIn, is_search: bool, current=None):
    """Validated column values for a create/update. Query-based rows (keyword searches and the
    per-agency daily searches) build their URL from the query and window - a keyword search is also
    named after its query, while an agency search keeps its "Agency: <name>" label; feeds need an
    http(s) URL."""
    import re
    import collection
    from urllib.parse import urlparse
    cur = current or {}
    out = {}

    def pick(field, allowed, default=None):
        v = getattr(s, field)
        if v is None:
            return cur.get(field, default)
        if v not in allowed:
            raise HTTPException(400, f"{field} must be one of: {', '.join(allowed)}")
        return v
    out["pillar"] = pick("pillar", SOURCE_PILLARS, "Funding")
    out["trust"] = pick("trust", SOURCE_TRUST, "Med")
    if s.notes is not None:
        out["notes"] = s.notes.strip()[:500] or None
    if s.enabled is not None:
        out["enabled"] = bool(s.enabled)
    if is_search:
        query = (s.query if s.query is not None else cur.get("query") or "").strip()
        days = s.search_days if s.search_days is not None else (cur.get("search_days") or 30)
        if not query:
            raise HTTPException(400, "Enter the search words.")
        if len(query) > 200:
            raise HTTPException(400, "Keep the search under 200 characters.")
        if not 1 <= int(days) <= 365:
            raise HTTPException(400, "Look back between 1 and 365 days.")
        name, url = collection.search_source(query, days)
        if cur.get("type") == "Agency":  # keep the agency's label and its own query text
            name, url = cur["name"], collection.google_news_url(query if "when:" in query else f"{query} when:{days}d")
        out.update(name=name, url=url, query=re.sub(r"\s+when:\d+d\s*$", "", query), search_days=int(days),
                   type=cur.get("type") or "Search",
                   method=cur.get("method") if cur.get("method") in ("RSS", "Retired") else "RSS")
        if s.method in ("RSS", "Retired"):
            out["method"] = s.method
    else:
        name = (s.name if s.name is not None else cur.get("name") or "").strip()
        url = (s.url if s.url is not None else cur.get("url") or "").strip()
        if not name or len(name) > 120:
            raise HTTPException(400, "Enter a name (up to 120 characters).")
        u = urlparse(url)
        if u.scheme not in ("http", "https") or not u.netloc:
            raise HTTPException(400, "Enter the feed's full http(s) URL.")
        out.update(name=name, url=url, type=pick("type", [t for t in SOURCE_TYPES if t != "Search"], "Trade"),
                   method=pick("method", SOURCE_METHODS, "RSS"))
    return out


def _source_row(cur, source_id):
    cur.execute(f"SELECT {', '.join(SOURCE_COLS)} FROM sources WHERE id=%s AND deleted_at IS NULL", (source_id,))
    row = cur.fetchone()
    if not row:
        raise HTTPException(404, "No such source.")
    return dict(zip(SOURCE_COLS, row))


def _iso(v):
    return v.isoformat() if hasattr(v, "isoformat") else v


@app.get("/api/agencies")
def list_agencies():
    """The agency reference table (reference/agencies.json, synced to Postgres). Read-only; the site
    uses it at build time for each article's Explore module."""
    import agencies as agmod
    try:
        with _db() as c:
            rows = agmod.all_agencies(c)
    except Exception as e:
        raise HTTPException(502, f"DB error: {e}")
    return {"agencies": rows}


@app.get("/api/sources")
def list_sources():
    """Every source (feeds and keyword searches) with its health and the items it has produced."""
    import collection
    try:
        with _db() as c, c.cursor() as cur:
            collection.migrate_sources(c)
            cur.execute(f"""SELECT {', '.join('s.' + k for k in SOURCE_COLS)},
                              count(i.id), count(i.id) FILTER (WHERE i.status='pending'),
                              count(i.id) FILTER (WHERE i.status IN ('approved','published')),
                              count(i.id) FILTER (WHERE i.status='published'),
                              to_char(max(i.collected_at) AT TIME ZONE 'America/New_York','YYYY-MM-DD HH24:MI')
                            FROM sources s LEFT JOIN collected_items i ON i.source_name = s.name
                            WHERE s.deleted_at IS NULL GROUP BY s.id ORDER BY (s.type='Search'), s.id""")
            rows = []
            for r in cur.fetchall():
                d = dict(zip(SOURCE_COLS, r[:len(SOURCE_COLS)]))
                d.update(items=r[-5], pending=r[-4], kept=r[-3], published=r[-2], last_item=r[-1])
                for k in ("edited_at", "last_fetched_at", "last_ok_at", "last_error_at"):
                    d[k] = _iso(d[k])
                rows.append(d)
            last_run = collection.get_setting(c, "collect_last_run")
            schedule = collection.get_setting(c, "collect_schedule")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"DB error: {e}")
    return {"sources": rows, "last_run": last_run, "schedule": schedule,
            "choices": {"pillar": SOURCE_PILLARS, "type": [t for t in SOURCE_TYPES if t != "Search"],
                        "method": SOURCE_METHODS, "trust": SOURCE_TRUST}}


@app.post("/api/sources")
def create_source(s: SourceIn):
    """Add a feed or a Google News keyword search (origin 'user'; --seed never touches it)."""
    import collection
    is_search = s.kind == "search"
    f = _source_fields(s, is_search)
    f.setdefault("enabled", True)
    try:
        with _db() as c, c.cursor() as cur:
            collection.migrate_sources(c)
            cur.execute("SELECT id, deleted_at IS NOT NULL FROM sources WHERE lower(name)=lower(%s)", (f["name"],))
            clash = cur.fetchone()
            if clash and not clash[1]:
                raise HTTPException(409, f"A source named “{f['name']}” already exists.")
            if clash:  # re-adding a deleted registry source: bring the row back with the new settings
                cur.execute("DELETE FROM sources WHERE id=%s", (clash[0],))
            cols = list(f) + ["origin", "edited_at"]
            cur.execute(f"INSERT INTO sources ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))}) RETURNING id",
                        list(f.values()) + ["user", datetime_now()])
            sid = cur.fetchone()[0]
            c.commit()
            return _source_json(_source_row(cur, sid))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"DB error: {e}")


def datetime_now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc)


def _source_json(d):
    return {k: _iso(v) for k, v in d.items()}


@app.put("/api/sources/{source_id}")
def update_source(source_id: int, s: SourceIn):
    """Edit a source. Marks it edited, so --seed keeps this version. A rename also relabels the items
    it has collected (collected_items.source_name), so its counts and dedup history carry over."""
    try:
        with _db() as c, c.cursor() as cur:
            cur_row = _source_row(cur, source_id)
            f = _source_fields(s, cur_row["type"] in ("Search", "Agency"), cur_row)
            if f["name"].lower() != cur_row["name"].lower():
                cur.execute("SELECT 1 FROM sources WHERE lower(name)=lower(%s) AND id<>%s", (f["name"], source_id))
                if cur.fetchone():
                    raise HTTPException(409, f"A source named “{f['name']}” already exists.")
            if f["name"] != cur_row["name"]:
                cur.execute("UPDATE collected_items SET source_name=%s WHERE source_name=%s", (f["name"], cur_row["name"]))
            f["edited_at"] = datetime_now()
            cur.execute(f"UPDATE sources SET {', '.join(k + '=%s' for k in f)} WHERE id=%s", list(f.values()) + [source_id])
            c.commit()
            return _source_json(_source_row(cur, source_id))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"DB error: {e}")


@app.post("/api/sources/{source_id}/enabled")
def set_source_enabled(source_id: int, t: Toggle):
    """Switch a source on or off without losing its settings (the next run skips disabled ones)."""
    try:
        with _db() as c, c.cursor() as cur:
            _source_row(cur, source_id)
            cur.execute("UPDATE sources SET enabled=%s, edited_at=now() WHERE id=%s", (t.enabled, source_id))
            c.commit()
            return _source_json(_source_row(cur, source_id))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"DB error: {e}")


@app.delete("/api/sources/{source_id}")
def delete_source(source_id: int):
    """Remove a source. Items it already collected stay. A registry source (from collection.py) is kept
    as a deleted marker so --seed doesn't add it back; one added here is removed outright."""
    try:
        with _db() as c, c.cursor() as cur:
            row = _source_row(cur, source_id)
            if row["origin"] == "registry":
                cur.execute("UPDATE sources SET deleted_at=now(), enabled=false WHERE id=%s", (source_id,))
            else:
                cur.execute("DELETE FROM sources WHERE id=%s", (source_id,))
            c.commit()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"DB error: {e}")
    return {"deleted": source_id, "name": row["name"]}


@app.post("/api/sources/{source_id}/test")
def test_source(source_id: int):
    """Fetch the feed once, the same way a run does, but without the model or the queue: shows whether
    it works and what it returns. Records the result as the source's health."""
    import collection
    with _db() as c, c.cursor() as cur:
        row = _source_row(cur, source_id)
    try:
        entries = collection.fetch_source(row["url"], limit=50)
    except collection.FetchError as e:
        with _db() as c:
            collection.record_health(c, source_id, error=str(e))
        return {"ok": False, "error": str(e)}
    with _db() as c:
        collection.record_health(c, source_id, entries=len(entries))
    return {"ok": True, "entries": len(entries),
            "sample": [{"title": e["title"][:160], "link": e["link"],
                        "published": e["published"].date().isoformat() if e["published"] else None} for e in entries[:5]]}


# ---- CIG project profile archive (cig_profiles.py): every version of each profile, with diffs ----
# Command Center only (not in readonly-api's allowlist).
MAX_LISTING_HTML = 10 * 1024 * 1024


def _pv_row(r):
    keys = ["id", "captured_at", "fta_date", "source", "file_name", "file_bytes", "changed", "prev_version_id",
            "lines_added", "lines_removed", "has_file"]
    return dict(zip(keys, r))


@app.get("/api/cig/profile-versions")
def cig_profile_versions(name: str, sponsor: Optional[str] = ""):
    """A project's profile page link and every archived version of its profile, newest first."""
    import cig_profiles
    try:
        with _db() as c, c.cursor() as cur:
            cig_profiles.create_tables(c)
            cur.execute("SELECT profile_url, listing_name, listing_stage FROM cig_profile_pages "
                        "WHERE project_name=%s AND sponsor=%s", (name, sponsor or ""))
            page = cur.fetchone()
            cur.execute("SELECT id, to_char(captured_at AT TIME ZONE 'America/New_York','YYYY-MM-DD HH24:MI'), fta_date, "
                        "source, file_name, file_bytes, changed, prev_version_id, lines_added, lines_removed, "
                        "file_path IS NOT NULL FROM cig_profile_versions WHERE project_name=%s AND sponsor=%s "
                        "ORDER BY captured_at DESC, id DESC", (name, sponsor or ""))
            versions = [_pv_row(r) for r in cur.fetchall()]
    except Exception as e:
        raise HTTPException(502, f"DB error: {e}")
    return {"name": name, "sponsor": sponsor, "profile_url": page[0] if page else None,
            "listing_name": page[1] if page else None, "listing_stage": page[2] if page else None,
            "versions": versions}


def _pv_get(version_id, cols):
    try:
        with _db() as c, c.cursor() as cur:
            cur.execute(f"SELECT {cols} FROM cig_profile_versions WHERE id=%s", (version_id,))
            row = cur.fetchone()
    except Exception as e:
        raise HTTPException(502, f"DB error: {e}")
    if not row:
        raise HTTPException(404, "No such profile version.")
    return row


@app.get("/api/cig/profile-versions/{version_id}/file")
def cig_profile_version_file(version_id: int):
    """The archived PDF of one profile version (only ever from the archive folder)."""
    import cig_profiles
    from fastapi.responses import FileResponse
    fp, fname = _pv_get(version_id, "file_path, file_name")
    base = os.path.realpath(cig_profiles.ARCHIVE_DIR)
    path = os.path.realpath(fp) if fp else None
    if not path or not path.startswith(base + os.sep) or not os.path.isfile(path):
        raise HTTPException(404, "The archived file is missing.")
    return FileResponse(path, media_type="application/pdf", content_disposition_type="inline",
                        filename=fname or os.path.basename(path))


@app.get("/api/cig/profile-versions/{version_id}/diff")
def cig_profile_version_diff(version_id: int):
    """What changed in this version's text vs. the version before it."""
    diff, added, removed, prev = _pv_get(version_id, "diff, lines_added, lines_removed, prev_version_id")
    return {"id": version_id, "prev_version_id": prev, "diff": diff, "lines_added": added, "lines_removed": removed}


@app.post("/api/cig/profiles/upload")
async def cig_profiles_upload(request: Request, filename: str = "", name: Optional[str] = None,
                              sponsor: Optional[str] = None):
    """Archive a profile PDF downloaded in a browser (raw body). Matched to a project by its title and
    state, or to name/sponsor when given (for a PDF that doesn't match on its own)."""
    import asyncio
    import cig_profiles
    body = await request.body()
    if len(body) > MAX_CIG_PDF:
        raise HTTPException(413, "That file is over 25 MB.")
    if not body.startswith(b"%PDF"):
        raise HTTPException(400, "That isn't a PDF.")

    def work():
        with _db() as c:
            project = None
            if name:
                with c.cursor() as cur:
                    cur.execute("SELECT project_name, coalesce(sponsor,''), state FROM cig_projects WHERE project_name=%s "
                                "AND coalesce(sponsor,'')=%s ORDER BY snapshot_date DESC LIMIT 1", (name, sponsor or ""))
                    project = cur.fetchone()
                if not project:
                    raise HTTPException(404, "No such project.")
            r = cig_profiles.ingest(c, body, os.path.basename(filename) or "upload.pdf", "upload", project=project)
            cig_profiles.record_run(c, "upload", None, [r])
            return r
    try:
        return await asyncio.to_thread(work)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(422, f"Couldn't read that PDF ({type(e).__name__}: {e}).")


@app.post("/api/cig/profiles/listing")
async def cig_profiles_listing(request: Request):
    """Load profile links from FTA's Current CIG Projects page, saved in a browser (raw HTML body)."""
    import cig_profiles
    body = await request.body()
    if len(body) > MAX_LISTING_HTML:
        raise HTTPException(413, "That file is over 10 MB.")
    try:
        with _db() as c:
            return cig_profiles.load_listing(c, body.decode("utf-8", errors="replace"))
    except ValueError as e:
        raise HTTPException(422, str(e))
    except Exception as e:
        raise HTTPException(502, f"DB error: {e}")


@app.post("/api/cig/profiles/check")
async def cig_profiles_check():
    """Run the weekly check now: the inbox folder, then one try of FTA's listing page."""
    import asyncio
    import cig_profiles

    def work():
        with _db() as c:
            out = cig_profiles.weekly(c, "manual")
        return {"listing_status": out["listing_status"], "listing": out["listing"],
                "results": [{k: r.get(k) for k in ("status", "file", "project_name", "message")} for r in out["results"]]}
    try:
        return await asyncio.to_thread(work)
    except Exception as e:
        raise HTTPException(502, f"Check failed: {e}")


@app.get("/api/cig/profiles/status")
def cig_profiles_status():
    """Archive coverage against the current dashboard, and the last check."""
    import cig_profiles
    try:
        with _db() as c, c.cursor() as cur:
            cig_profiles.create_tables(c)
            cur.execute("""SELECT count(*), count(g.profile_url), count(v.k)
                           FROM cig_projects p
                           LEFT JOIN cig_profile_pages g ON g.project_name=p.project_name AND g.sponsor=coalesce(p.sponsor,'')
                           LEFT JOIN (SELECT DISTINCT project_name, sponsor, 1 AS k FROM cig_profile_versions) v
                             ON v.project_name=p.project_name AND v.sponsor=coalesce(p.sponsor,'')
                           WHERE p.snapshot_date=(SELECT max(snapshot_date) FROM cig_projects)""")
            projects, linked, archived = cur.fetchone()
            cur.execute("SELECT count(*), count(*) FILTER (WHERE changed) FROM cig_profile_versions")
            versions, changed = cur.fetchone()
            cur.execute("SELECT to_char(ran_at AT TIME ZONE 'America/New_York','YYYY-MM-DD HH24:MI'), trigger, "
                        "listing_status, files, new_versions, changed, unchanged, unmatched FROM cig_profile_runs "
                        "WHERE trigger IN ('weekly','manual','command line') ORDER BY id DESC LIMIT 1")
            last = cur.fetchone()
            cur.execute("SELECT to_char(captured_at AT TIME ZONE 'America/New_York','YYYY-MM-DD HH24:MI'), changes "
                        "FROM cig_profile_listings ORDER BY id DESC LIMIT 1")
            listing = cur.fetchone()
            todo = cig_profiles.to_download(c, listing[1] if listing else [])
    except Exception as e:
        raise HTTPException(502, f"DB error: {e}")
    keys = ["at", "trigger", "listing_status", "files", "new_versions", "changed", "unchanged", "unmatched"]
    return {"projects": projects, "linked": linked, "archived": archived, "versions": versions, "changed": changed,
            "inbox": cig_profiles.INBOX_DIR, "last_check": dict(zip(keys, last)) if last else None,
            "listing_at": listing[0] if listing else None, "listing_changes": listing[1] if listing else [],
            "to_download": todo}


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
- mode: 'BRT','Light Rail','Heavy Rail','Commuter Rail','Streetcar', or NULL where FTA's sources don't state it (call NULL "Unspecified"; never guess a mode from the project name). mode_source says where it came from.
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
.chip-sm{display:inline-block;border:1px solid var(--line);border-radius:999px;padding:1px 8px;font-family:'Archivo',sans-serif;font-size:11px;font-weight:600;color:var(--muted)}
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
  <button class="tab" data-t="contacts">Contacts</button>
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
  <div class="panel" id="p-sources">
    <div class="askhead"><div><h2 class="disp">Sources</h2><p class="lead">The feeds and Google News keyword searches the collector reads. Changes apply to the next run (the daily one, or <code>collect --run</code> on the NAS). Only enabled <b>RSS</b> sources are fetched; API, Scrape and Manual entries are a watchlist.</p></div>
      <div style="display:flex;gap:8px"><button class="newq" id="sAddFeed" type="button">Add feed</button><button class="newq" id="sAddSearch" type="button">Add keyword search</button><button class="newq" id="sRefresh" type="button">Refresh</button></div></div>
    <div id="sRun" style="font-family:Archivo,sans-serif;font-size:12px;color:var(--muted);margin:-4px 0 12px"></div>
    <div id="sForm"></div>
    <div id="sMsg"></div>
    <div id="sOut"></div>
  </div>
  <div class="panel" id="p-contacts">
    <div class="askhead"><div><h2 class="disp">Contacts</h2><p class="lead">The newsletter list &mdash; ours, in Postgres. Signups from the site arrive as <b>pending</b>; only <b>subscribed</b> contacts will ever be emailed. Unsubscribed, bounced and complained addresses are suppressed and can't be re-added by an import.</p></div>
      <div style="display:flex;gap:8px"><button class="newq" id="kAddBtn" type="button">Add contact</button><button class="newq" id="kImportBtn" type="button">Import CSV</button><a class="newq" id="kExport" href="/api/contacts/export.csv" style="text-decoration:none;display:inline-block">Export CSV</a><button class="newq" id="kTestBtn" type="button" title="Send a test message through SES">Send test</button><button class="newq" id="kRefresh" type="button">Refresh</button></div></div>
    <input type="file" id="kFile" accept=".csv,text/csv" hidden>
    <div id="kEmail" style="font-family:Archivo,sans-serif;font-size:12px;color:var(--muted);margin:-4px 0 12px"></div>
    <div id="kCounts" style="margin-bottom:12px"></div>
    <form class="csearch" id="kSearchForm">
      <input type="text" id="kQ" placeholder="Search email or name" aria-label="Search contacts" autocomplete="off">
      <select id="kStatus" aria-label="Status"><option value="">All statuses</option></select>
      <select id="kTag" aria-label="Tag"><option value="">All tags</option></select>
      <button class="newq" type="submit">Search</button>
    </form>
    <div id="kForm"></div>
    <div id="kMsg"></div>
    <div id="kOut"></div>
  </div>
  <div class="panel" id="p-publish">
    <div class="askhead"><div><h2 class="disp">Publish</h2><p class="lead">Approved items become live posts. Publishing writes to content_posts - what the public site reads - and the site rebuilds itself about a minute later.</p></div><div style="display:flex;gap:8px"><button class="newq" id="pRebuild" type="button">Rebuild site now</button><button class="newq" id="pRefresh" type="button">Refresh</button></div></div>
    <div id="pSite" style="font-family:Archivo,sans-serif;font-size:12px;color:var(--muted);margin:-4px 0 14px"></div>
    <div style="display:flex;align-items:center;gap:12px;margin:6px 0 10px"><div style="font-family:Archivo,sans-serif;font-size:12px;font-weight:800;letter-spacing:1px;text-transform:uppercase;color:var(--muted)">Ready to publish <span id="pReadyCount"></span></div>
      <button class="go" id="pPubAll" type="button" style="padding:7px 14px;margin-left:auto" hidden>Publish all</button></div>
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
    <div class="rcard" id="gProf" style="padding:12px 18px;margin-bottom:14px;display:flex;gap:12px;align-items:center;flex-wrap:wrap">
      <div style="font-family:Archivo,sans-serif;font-weight:800;font-size:14px">Project profiles</div>
      <div id="gProfStat" style="font-family:Archivo,sans-serif;font-size:12px;color:var(--muted);flex:1;min-width:240px"></div>
      <button class="newq" id="gProfUpBtn" type="button" title="Profile PDFs downloaded from each project's page on transit.dot.gov">Upload profiles</button>
      <button class="newq" id="gListBtn" type="button" title="FTA's Current CIG Projects page, saved in your browser (Ctrl+S, Webpage HTML only)">Load projects page</button>
      <button class="newq" id="gProfCheck" type="button" title="Process the inbox folder and try FTA's listing page once">Check now</button>
      <input type="file" id="gProfFile" accept="application/pdf,.pdf" multiple hidden>
      <input type="file" id="gListFile" accept=".html,.htm,text/html" hidden>
      <div id="gProfTodo" style="flex-basis:100%"></div>
    </div>
    <details class="rcard" id="gLoadsBox" style="padding:0 18px;margin-bottom:14px">
      <summary style="padding:13px 0;font-family:Archivo,sans-serif;font-weight:800;font-size:14px;cursor:pointer">Dashboard files <span id="gLoadsCount" style="font-weight:600;color:var(--muted)"></span></summary>
      <div id="gLoads" style="padding-bottom:12px"></div>
    </details>
    <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap"><div class="examples" id="gChips"></div>
      <label style="font-family:Archivo,sans-serif;font-size:12px;color:var(--muted);margin-bottom:10px">Mode
        <select id="gMode" title="Mode from FTA sources; Unspecified where they don't state it" style="font-family:Archivo,sans-serif;font-size:12px;padding:5px 8px;border:1px solid var(--line);border-radius:6px;background:var(--card);color:var(--ink)"><option value="">All modes</option></select></label></div>
    <div id="gOut"></div>
    <div id="gChanges" style="margin-top:18px"></div>
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
// Publish all: exactly the items shown (ids captured at load), in one request, one site rebuild.
var pReadyIds=[];
document.getElementById("pPubAll").onclick=async()=>{
  const b=document.getElementById("pPubAll"),n=pReadyIds.length;if(!n)return;
  if(!confirm("Publish all "+n+" items in Ready to publish? They go live on the site after one rebuild (about 2-3 minutes)."))return;
  b.disabled=true;b.textContent="Publishing "+n+"...";
  try{
    const r=await fetch("/api/publish-all",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({ids:pReadyIds})});
    const t=await r.text();
    if(!r.ok)alert("Couldn't publish: "+errText(t));
    else{const d=JSON.parse(t);if(d.skipped&&d.skipped.length)alert("Published "+d.published+". "+d.skipped.length+" were no longer approved and were skipped.");}
  }catch(e){alert("Couldn't reach the Command Center.");}
  loadPublish();
};
async function loadPublish(){
  const ready=document.getElementById("pReady"),posts=document.getElementById("pPosts");
  ready.innerHTML='<div class="rcard"><div class="loading">Loading...</div></div>';
  loadSiteStatus();
  const all=document.getElementById("pPubAll"),cnt=document.getElementById("pReadyCount");
  pReadyIds=[];all.hidden=true;cnt.textContent="";
  try{const d=await (await fetch("/api/publish/ready")).json();
    const items=d.items||[];
    pReadyIds=items.map(it=>it.id);
    cnt.textContent=items.length?"("+items.length+")":"";
    all.hidden=items.length<2;all.disabled=false;all.textContent="Publish all "+items.length;
    ready.innerHTML=items.length?items.map(pReadyCard).join(""):'<div class="rcard"><div class="loading">Nothing approved yet - approve items in the Collection tab.</div></div>';
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
// ---- Contacts tab: the newsletter list (contacts + suppressions) ----
let kData=null,kFilter={q:"",status:"",tag:""};
document.querySelector('.tab[data-t="contacts"]').addEventListener("click",loadContacts);
document.getElementById("kRefresh").onclick=loadContacts;
document.getElementById("kSearchForm").addEventListener("submit",e=>{
  e.preventDefault();
  kFilter={q:document.getElementById("kQ").value.trim(),status:document.getElementById("kStatus").value,tag:document.getElementById("kTag").value};
  loadContacts();
});
["kStatus","kTag"].forEach(id=>document.getElementById(id).addEventListener("change",()=>document.getElementById("kSearchForm").requestSubmit()));
function kMsg(kind,html){document.getElementById("kMsg").innerHTML=html?'<div class="rcard"><div class="'+kind+'">'+html+'</div></div>':"";}
const KSTATUS={pending:["Pending","var(--muted)"],subscribed:["Subscribed","#1F6B4A"],unsubscribed:["Unsubscribed","var(--accent)"],bounced:["Bounced","var(--accent)"],complained:["Complained","var(--accent)"]};
function kBadge(s){const x=KSTATUS[s]||[s,"var(--muted)"];return '<span style="font-family:Archivo,sans-serif;font-size:11px;font-weight:800;color:'+x[1]+'">'+esc(x[0])+'</span>';}
function kDate(iso){return iso?esc(new Date(iso).toLocaleDateString(undefined,{month:"short",day:"numeric",year:"numeric"})):"";}
function kRow(x){
  return '<tr><td style="font-weight:600">'+esc(x.email)+'</td><td>'+esc(x.name||"")+'</td><td>'+kBadge(x.status)+'</td>'
    +'<td>'+esc(x.source||"")+'</td><td style="white-space:normal">'+(x.tags||[]).map(t=>'<span class="chip-sm">'+esc(t)+'</span>').join(" ")+'</td>'
    +'<td>'+kDate(x.created_at)+'</td><td style="white-space:nowrap">'
    +(["pending"].includes(x.status)?'<button class="t411-linkbtn" data-kconfirm="'+x.id+'" title="Send the double opt-in email">Send confirmation</button> ':"")
    +'<button class="t411-linkbtn" data-kedit="'+x.id+'">Edit</button>'
    +(["unsubscribed","bounced","complained"].includes(x.status)?"":' <button class="t411-linkbtn" data-kunsub="'+x.id+'">Unsubscribe</button>')+'</td></tr>';
}
async function loadContacts(){
  const out=document.getElementById("kOut");
  loadEmailStatus();
  if(!kData)out.innerHTML='<div class="rcard"><div class="loading">Loading contacts...</div></div>';
  const qs=new URLSearchParams();
  if(kFilter.q)qs.set("q",kFilter.q);if(kFilter.status)qs.set("status",kFilter.status);if(kFilter.tag)qs.set("tag",kFilter.tag);
  try{
    const r=await fetch("/api/contacts"+(qs.toString()?"?"+qs:""));
    if(!r.ok){out.innerHTML='<div class="rcard"><div class="err">'+esc(errText(await r.text()))+'</div></div>';return;}
    kData=await r.json();
    const c=kData.counts||{},bs=c.by_status||{};
    document.getElementById("kCounts").innerHTML='<div class="rcard" style="padding:14px 18px;display:flex;gap:26px;flex-wrap:wrap;align-items:center">'
      +gStat(c.total||0,"contacts")+gStat(bs.subscribed||0,"subscribed")+gStat(bs.pending||0,"pending")
      +gStat((bs.unsubscribed||0)+(bs.bounced||0)+(bs.complained||0),"suppressed")
      +'<div style="font-family:Archivo,sans-serif;font-size:11px;color:var(--muted);margin-left:auto">'+(c.suppressed||0)+' on the do-not-email list</div></div>';
    const sel=document.getElementById("kStatus");
    if(sel.options.length<2)sel.innerHTML='<option value="">All statuses</option>'+(kData.statuses||[]).map(s=>'<option value="'+esc(s)+'">'+esc((KSTATUS[s]||[s])[0])+'</option>').join("");
    sel.value=kFilter.status;
    const tsel=document.getElementById("kTag");
    tsel.innerHTML='<option value="">All tags</option>'+(kData.tags||[]).map(t=>'<option value="'+esc(t)+'">'+esc(t)+'</option>').join("");
    tsel.value=kFilter.tag;
    const L=kData.contacts||[];
    out.innerHTML='<div class="rcard"><div style="padding:12px 18px 0;font-family:Archivo,sans-serif;font-size:12px;color:var(--muted)">'
      +(kData.matching||0)+' matching'+(L.length<(kData.matching||0)?' (showing '+L.length+')':'')+'</div>'
      +'<div class="t411-scroll"><table class="t411-table"><thead><tr><th>Email</th><th>Name</th><th>Status</th><th>Source</th><th>Tags</th><th>Added</th><th></th></tr></thead><tbody>'
      +(L.length?L.map(kRow).join(""):'<tr><td colspan="7" style="color:var(--muted)">No contacts match.</td></tr>')+'</tbody></table></div></div>';
  }catch(e){out.innerHTML='<div class="rcard"><div class="err">Could not load contacts.</div></div>';}
}
// SES status + test send + resending the double opt-in email.
async function loadEmailStatus(){
  const el=document.getElementById("kEmail");
  try{
    const r=await fetch("/api/email/status");if(!r.ok){el.textContent="";return;}
    const s=await r.json();
    const t=s.by_type||{};
    const bits=Object.keys(t).length?Object.keys(t).map(k=>t[k]+" "+k).join(" · "):"nothing sent yet";
    el.innerHTML=s.configured
      ? "SES ready — sending as "+esc(s.from)+" ("+esc(s.region)+", "+s.max_per_second+"/sec) · links at "+esc(s.link_base)+"<br>"+esc(bits)
      : '<span style="color:var(--accent)">SES isn\'t configured yet</span> — add AWS_REGION, AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY and SES_FROM to .env on the NAS. Signups are still captured as pending.';
    document.getElementById("kTestBtn").disabled=!s.configured;
  }catch(e){el.textContent="";}
}
document.getElementById("kTestBtn").onclick=async()=>{
  const to=prompt("Send a test message to (in the SES sandbox this must be an address verified in SES):","");
  if(!to)return;
  kMsg("loading","Sending a test to "+esc(to)+"...");
  try{
    const r=await fetch("/api/email/test",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({email:to})});
    const t=await r.text();
    kMsg(r.ok?"loading":"err",r.ok?"Test sent to "+esc(to)+" (SES id "+esc(JSON.parse(t).message_id)+").":"Not sent: "+errText(t));
  }catch(e){kMsg("err","Couldn't reach the Command Center.");}
  loadEmailStatus();
};
document.getElementById("kAddBtn").onclick=()=>kShowForm(null);
function kShowForm(x){
  const inp='style="padding:9px 10px;border:1px solid var(--line);background:var(--card);color:var(--ink);font-size:14px;font-family:Spectral,serif;border-radius:8px"';
  const sel='style="padding:8px;border:1px solid var(--line);background:var(--card);color:var(--ink);font-family:Archivo,sans-serif;font-size:12px;border-radius:8px"';
  const lab=(t,inner)=>'<label style="display:flex;flex-direction:column;gap:4px;font-family:Archivo,sans-serif;font-size:11px;font-weight:700;color:var(--muted);flex:1 1 180px">'+t+inner+'</label>';
  const statuses=(kData&&kData.statuses)||["pending","subscribed","unsubscribed","bounced","complained"];
  document.getElementById("kForm").innerHTML='<div class="rcard" style="padding:16px 18px"><div style="font-family:Archivo,sans-serif;font-weight:800;font-size:14px;margin-bottom:10px">'
    +(x?"Edit "+esc(x.email):"Add a contact")+'</div><div style="display:flex;gap:10px;flex-wrap:wrap">'
    +(x?"":lab("Email",'<input id="kfEmail" type="email" '+inp+' placeholder="name@agency.gov">'))
    +lab("Name",'<input id="kfName" type="text" '+inp+' value="'+esc(x?x.name||"":"")+'">')
    +lab("Tags (comma separated)",'<input id="kfTags" type="text" '+inp+' value="'+esc(x?(x.tags||[]).join(", "):"")+'">')
    +lab("Status",'<select id="kfStatus" '+sel+'>'+statuses.map(s=>'<option value="'+esc(s)+'"'+((x?x.status:"subscribed")===s?" selected":"")+'>'+esc((KSTATUS[s]||[s])[0])+'</option>').join("")+'</select>')
    +(x?"":lab("Source",'<input id="kfSource" type="text" '+inp+' value="manual">'))
    +'</div><div style="display:flex;gap:8px;margin-top:12px"><button class="go" id="kfSave" type="button" style="padding:9px 18px">'+(x?"Save":"Add")+'</button>'
    +'<button class="newq" id="kfCancel" type="button">Cancel</button><span id="kfErr" class="err" style="padding:8px 4px"></span></div></div>';
  document.getElementById("kfCancel").onclick=()=>{document.getElementById("kForm").innerHTML="";};
  document.getElementById("kfSave").onclick=async()=>{
    const v=id=>{const e=document.getElementById(id);return e?e.value:undefined;};
    const tags=(v("kfTags")||"").split(",").map(t=>t.trim()).filter(Boolean);
    const body=x?{name:v("kfName"),tags,status:v("kfStatus")}
      :{email:v("kfEmail"),name:v("kfName"),tags,status:v("kfStatus"),source:v("kfSource")};
    const b=document.getElementById("kfSave");b.disabled=true;
    try{
      const r=await fetch(x?"/api/contacts/"+x.id:"/api/contacts",{method:x?"PUT":"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
      const t=await r.text();
      if(!r.ok){document.getElementById("kfErr").textContent=errText(t);b.disabled=false;return;}
      document.getElementById("kForm").innerHTML="";kMsg("loading",(x?"Saved ":"Added ")+esc(JSON.parse(t).contact.email)+".");loadContacts();
    }catch(e){document.getElementById("kfErr").textContent="Couldn't reach the Command Center.";b.disabled=false;}
  };
}
document.getElementById("kImportBtn").onclick=()=>document.getElementById("kFile").click();
document.getElementById("kFile").addEventListener("change",async e=>{
  const f=e.target.files[0];e.target.value="";if(!f)return;
  const tags=prompt("Tag every contact in this file with (optional, comma separated):","")||"";
  kMsg("loading","Importing "+esc(f.name)+"...");
  try{
    const r=await fetch("/api/contacts/import?source="+encodeURIComponent("csv: "+f.name)+"&tags="+encodeURIComponent(tags),
      {method:"POST",headers:{"Content-Type":"text/csv"},body:f});
    const t=await r.text();
    if(!r.ok){kMsg("err","Not imported: "+errText(t));return;}
    const d=JSON.parse(t);
    kMsg("loading","Imported "+esc(f.name)+": "+d.added+" added, "+d.updated+" already on the list, "+d.suppressed+" suppressed (skipped), "
      +d.invalid+" not valid addresses"+(d.duplicate_in_file?", "+d.duplicate_in_file+" duplicate rows in the file":"")+" — "+d.rows+" rows read.");
    loadContacts();
  }catch(err){kMsg("err","Couldn't reach the Command Center.");}
});
document.getElementById("p-contacts").addEventListener("click",async e=>{
  const ed=e.target.closest("[data-kedit]");
  if(ed){const x=(kData.contacts||[]).find(c=>c.id===+ed.dataset.kedit);if(x)kShowForm(x);return;}
  const cf=e.target.closest("[data-kconfirm]");
  if(cf){const x=(kData.contacts||[]).find(c=>c.id===+cf.dataset.kconfirm);if(!x)return;
    kMsg("loading","Sending the confirmation email to "+esc(x.email)+"...");
    try{const r=await fetch("/api/contacts/"+x.id+"/confirmation",{method:"POST"});const t=await r.text();
      kMsg(r.ok?"loading":"err",r.ok?"Confirmation sent to "+esc(x.email)+". They're subscribed once they click it.":"Not sent: "+errText(t));}
    catch(err){kMsg("err","Couldn't reach the Command Center.");}
    loadContacts();return;}
  const un=e.target.closest("[data-kunsub]");
  if(un){const x=(kData.contacts||[]).find(c=>c.id===+un.dataset.kunsub);if(!x)return;
    if(!confirm("Mark "+x.email+" unsubscribed? They go on the do-not-email list and can't be re-added by an import."))return;
    const r=await fetch("/api/contacts/"+x.id,{method:"PUT",headers:{"Content-Type":"application/json"},body:JSON.stringify({status:"unsubscribed"})});
    kMsg(r.ok?"loading":"err",r.ok?esc(x.email)+" is unsubscribed and suppressed.":esc(errText(await r.text())));loadContacts();}
});

// ---- Sources tab: the collector's registry (feeds + Google News keyword searches) ----
let sData=null;
document.querySelector('.tab[data-t="sources"]').addEventListener("click",loadSources);
document.getElementById("sRefresh").onclick=loadSources;
document.getElementById("sAddFeed").onclick=()=>sShowForm("feed",null);
document.getElementById("sAddSearch").onclick=()=>sShowForm("search",null);
function sMsg(kind,html){document.getElementById("sMsg").innerHTML=html?'<div class="rcard"><div class="'+kind+'">'+html+'</div></div>':"";}
function sAgo(iso){
  if(!iso)return"";const m=(Date.now()-new Date(iso))/6e4;
  return m<2?"just now":m<90?Math.round(m)+" min ago":m<2880?Math.round(m/60)+" h ago":Math.round(m/1440)+" days ago";
}
function sHealth(x){
  const muted=t=>'<span style="color:var(--muted)">'+t+'</span>';
  if(!x.enabled)return muted("Off");
  if(x.method!=="RSS")return muted(x.method==="Retired"?"Retired - not fetched":"Not fetched ("+esc(x.method)+" - watchlist only)");
  if(!x.last_fetched_at)return muted("Not fetched yet");
  const failing=x.last_error&&(!x.last_ok_at||x.last_error_at>x.last_ok_at);
  if(failing)return '<span style="color:var(--accent);font-weight:700">Failing</span> '+esc(x.last_error)
    +muted(" · "+x.fail_streak+" run"+(x.fail_streak===1?"":"s")+" in a row · last OK "+(x.last_ok_at?sAgo(x.last_ok_at):"never"));
  return '<span style="color:#1F6B4A;font-weight:700">OK</span> '+muted(sAgo(x.last_ok_at)
    +(x.last_entries!=null?" · "+x.last_entries+" in feed":"")+(x.last_new!=null?", "+x.last_new+" new, "+(x.last_queued||0)+" queued":""));
}
function sRow(x){
  const url=safeUrl(x.url);
  const title=(x.type==="Search"||x.type==="Agency")
    ?'<div style="font-weight:700">'+esc(x.type==="Agency"?x.name.replace(/^Agency: /,""):x.query||x.name)+'</div>'
      +(x.type==="Agency"?'<div style="color:var(--muted);font-size:11px">'+esc(x.query||"")+'</div>':'')
      +'<div style="color:var(--muted);font-size:11px">last '+(x.search_days||30)+' days · '+(url?'<a href="'+esc(url)+'" target="_blank" rel="noopener noreferrer">Google News</a>':'')+'</div>'
    :'<div style="font-weight:700">'+esc(x.name)+'</div><div style="font-size:11px;max-width:340px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">'+(url?'<a href="'+esc(url)+'" target="_blank" rel="noopener noreferrer" title="'+esc(x.url)+'">'+esc(x.url)+'</a>':esc(x.url))+'</div>';
  const note=x.notes?'<div style="color:var(--muted);font-size:11px;white-space:normal;max-width:340px">'+esc(x.notes)+'</div>':"";
  const tag=x.origin==="user"?' <span class="t411-badge" title="Added in this tab">added here</span>':(x.edited_at?' <span class="t411-badge" title="Changed in this tab; collect --seed leaves it as is">edited</span>':'');
  return '<tr style="'+(x.enabled?'':'opacity:.55')+'"><td><input type="checkbox" data-on="'+x.id+'"'+(x.enabled?" checked":"")+' aria-label="Enabled" title="'+(x.enabled?"On - click to switch off":"Off - click to switch on")+'"></td>'
    +'<td style="white-space:normal">'+title+note+'</td><td>'+esc(x.pillar||"-")+tag+'</td>'
    +'<td>'+esc(x.type==="Search"?"Search":x.type||"-")+' · '+esc(x.method||"-")+'</td><td>'+esc(x.trust||"-")+'</td>'
    +'<td class="num" title="'+x.pending+' pending, '+x.kept+' approved or published'+(x.last_item?", newest "+esc(x.last_item):"")+'">'+x.items+(x.published?' <span style="color:var(--muted)">('+x.published+' pub.)</span>':'')+'</td>'
    +'<td style="white-space:normal;min-width:200px">'+sHealth(x)+'</td>'
    +'<td style="white-space:nowrap">'+(x.method==="RSS"?'<button class="t411-linkbtn" data-test="'+x.id+'">Test</button> ':'')
    +'<button class="t411-linkbtn" data-edit="'+x.id+'">Edit</button> <button class="t411-linkbtn" data-del="'+x.id+'">Delete</button></td></tr>';
}
function sTable(title,list){
  const on=list.filter(x=>x.enabled&&x.method==="RSS").length;
  return '<div class="rcard"><div style="padding:12px 18px 0;font-family:Archivo,sans-serif;font-weight:800;font-size:14px">'+title
    +' <span style="font-weight:600;color:var(--muted)">('+list.length+' · '+on+' fetched each run)</span></div>'
    +'<div class="t411-scroll"><table class="t411-table"><thead><tr><th>On</th><th>Source</th><th>Pillar</th><th>Type · method</th><th>Trust</th><th>Items</th><th>Health (last run)</th><th></th></tr></thead><tbody>'
    +(list.length?list.map(sRow).join(""):'<tr><td colspan="8" style="color:var(--muted)">None yet.</td></tr>')+'</tbody></table></div></div>';
}
async function loadSources(){
  const out=document.getElementById("sOut");
  if(!sData)out.innerHTML='<div class="rcard"><div class="loading">Loading sources...</div></div>';
  try{
    const r=await fetch("/api/sources");if(!r.ok){out.innerHTML='<div class="rcard"><div class="err">'+esc(errText(await r.text()))+'</div></div>';return;}
    sData=await r.json();
    const S=sData.sources||[],lr=sData.last_run,sc=sData.schedule;
    document.getElementById("sRun").innerHTML=(lr&&lr.at?"Last run "+esc(new Date(lr.at).toLocaleString())+(lr.skipped?" - skipped (Auto-collect off)"
        :(lr.error?" - failed: "+esc(lr.error):" - "+(lr.added||0)+" items queued from "+((lr.sources||0)-((lr.failed||[]).length))+" of "+(lr.sources||0)+" sources"+((lr.failed||[]).length?"; failed: "+esc(lr.failed.join(", ")):""))):"No run recorded yet")
      +(sc&&sc.next_run?" · next run "+esc(new Date(sc.next_run).toLocaleString()):"");
    out.innerHTML=sTable("Feeds",S.filter(x=>x.type!=="Search"&&x.type!=="Agency"))
      +sTable("Google News keyword searches",S.filter(x=>x.type==="Search"))
      +(S.some(x=>x.type==="Agency")?sTable("Agency searches (one per agency in reference/agencies.json)",S.filter(x=>x.type==="Agency")):"");
  }catch(e){out.innerHTML='<div class="rcard"><div class="err">Could not load sources.</div></div>';}
}
function sOpts(list,cur){return list.map(v=>'<option'+(v===cur?" selected":"")+'>'+esc(v)+'</option>').join("");}
function sShowForm(kind,x){
  const ch=(sData&&sData.choices)||{pillar:["Funding","Procurement","People","Policy","Data"],type:["Official","Trade","Association","Aggregator","Agency","Data"],method:["RSS","API","Scrape","Manual","Retired"],trust:["High","Med","Low"]};
  const lab=(t,inner)=>'<label style="display:flex;flex-direction:column;gap:4px;font-family:Archivo,sans-serif;font-size:11px;font-weight:700;color:var(--muted);flex:1 1 150px">'+t+inner+'</label>';
  const inp='style="padding:9px 10px;border:1px solid var(--line);background:var(--card);color:var(--ink);font-size:14px;font-family:Spectral,serif;border-radius:8px"';
  const sel='style="padding:8px;border:1px solid var(--line);background:var(--card);color:var(--ink);font-family:Archivo,sans-serif;font-size:12px;border-radius:8px"';
  const search=kind==="search";
  const f=search
    ?lab('Search words (Google News syntax: OR, "exact phrase")','<input id="fQuery" type="text" '+inp+' value="'+esc(x?x.query||"":"")+'" placeholder="e.g. transit agency budget deficit">')
      +lab("Look back (days)",'<input id="fDays" type="number" min="1" max="365" '+inp+' value="'+esc(x?x.search_days||30:30)+'">')
    :lab("Name",'<input id="fName" type="text" '+inp+' value="'+esc(x?x.name:"")+'" placeholder="e.g. Transit Talent">')
      +lab("Feed URL (RSS/Atom)",'<input id="fUrl" type="text" '+inp+' value="'+esc(x?x.url:"")+'" placeholder="https://...">')
      +lab("Type",'<select id="fType" '+sel+'>'+sOpts(ch.type,x?x.type:"Trade")+'</select>')
      +lab("Method",'<select id="fMethod" '+sel+' title="Only RSS is fetched">'+sOpts(ch.method,x?x.method:"RSS")+'</select>');
  document.getElementById("sForm").innerHTML='<div class="rcard" style="padding:16px 18px"><div style="font-family:Archivo,sans-serif;font-weight:800;font-size:14px;margin-bottom:10px">'
    +(x?"Edit "+(search?"keyword search":"source"):"Add "+(search?"a Google News keyword search":"a feed"))+'</div>'
    +'<div style="display:flex;gap:10px;flex-wrap:wrap">'+f
    +lab("Pillar",'<select id="fPillar" '+sel+'>'+sOpts(ch.pillar,x?x.pillar:"Funding")+'</select>')
    +lab("Trust",'<select id="fTrust" '+sel+'>'+sOpts(ch.trust,x?x.trust:"Med")+'</select>')+'</div>'
    +'<div style="margin-top:10px">'+lab("Notes (optional)",'<input id="fNotes" type="text" '+inp+' value="'+esc(x?x.notes||"":"")+'">')+'</div>'
    +'<div style="display:flex;gap:8px;margin-top:12px"><button class="go" id="fSave" type="button" style="padding:9px 18px">'+(x?"Save":"Add")+'</button>'
    +'<button class="newq" id="fCancel" type="button">Cancel</button><span id="fErr" class="err" style="padding:8px 4px"></span></div></div>';
  document.getElementById("fCancel").onclick=()=>{document.getElementById("sForm").innerHTML="";};
  document.getElementById("fSave").onclick=async()=>{
    const v=id=>{const e=document.getElementById(id);return e?e.value:undefined;};
    const body=search?{kind:"search",query:v("fQuery"),search_days:parseInt(v("fDays"),10)||30,pillar:v("fPillar"),trust:v("fTrust"),notes:v("fNotes")}
      :{kind:"feed",name:v("fName"),url:v("fUrl"),type:v("fType"),method:v("fMethod"),pillar:v("fPillar"),trust:v("fTrust"),notes:v("fNotes")};
    const b=document.getElementById("fSave");b.disabled=true;
    try{const r=await fetch(x?"/api/sources/"+x.id:"/api/sources",{method:x?"PUT":"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
      const t=await r.text();
      if(!r.ok){document.getElementById("fErr").textContent=errText(t);b.disabled=false;return;}
      const d=JSON.parse(t);document.getElementById("sForm").innerHTML="";
      sMsg("loading",esc((x?"Saved ":"Added ")+(d.type==="Search"?d.query:d.name))+". It applies to the next run."+(d.method==="RSS"?' <button class="t411-linkbtn" data-test="'+d.id+'">Test it now</button>':""));
      loadSources();
    }catch(e){document.getElementById("fErr").textContent="Couldn't reach the Command Center.";b.disabled=false;}
  };
  document.getElementById("sForm").scrollIntoView({behavior:"smooth",block:"nearest"});
}
async function sTest(id){
  const x=(sData.sources||[]).find(s=>s.id===id);
  sMsg("loading","Fetching "+esc(x?(x.query||x.name):"the source")+"...");
  try{const r=await fetch("/api/sources/"+id+"/test",{method:"POST"});const d=await r.json();
    if(!r.ok)sMsg("err",esc(errText(JSON.stringify(d))));
    else if(!d.ok)sMsg("err","Test failed: "+esc(d.error)+".");
    else sMsg("loading","Works: "+d.entries+" item"+(d.entries===1?"":"s")+" in the feed (nothing was queued or sent to the model)."
      +(d.sample.length?'<ul style="margin:8px 0 0 18px;padding:0">'+d.sample.map(e=>'<li>'+(safeUrl(e.link)?'<a href="'+esc(safeUrl(e.link))+'" target="_blank" rel="noopener noreferrer">'+esc(e.title)+'</a>':esc(e.title))+(e.published?' <span style="color:var(--muted)">'+esc(e.published)+'</span>':"")+'</li>').join("")+'</ul>':""));
  }catch(e){sMsg("err","Couldn't reach the Command Center.");}
  loadSources();
}
document.getElementById("p-sources").addEventListener("click",async e=>{
  const t=e.target.closest("[data-test]");if(t){sTest(+t.dataset.test);return;}
  const ed=e.target.closest("[data-edit]");
  if(ed){const x=sData.sources.find(s=>s.id===+ed.dataset.edit);if(x)sShowForm(x.type==="Search"||x.type==="Agency"?"search":"feed",x);return;}
  const del=e.target.closest("[data-del]");
  if(del){const x=sData.sources.find(s=>s.id===+del.dataset.del);if(!x)return;
    if(!confirm("Delete "+(x.query||x.name)+"? The next run stops fetching it. Items it already collected stay."))return;
    const r=await fetch("/api/sources/"+x.id,{method:"DELETE"});
    sMsg(r.ok?"loading":"err",r.ok?"Deleted "+esc(x.query||x.name)+".":esc(errText(await r.text())));loadSources();}
});
document.getElementById("p-sources").addEventListener("change",async e=>{
  const cb=e.target.closest("[data-on]");if(!cb)return;
  cb.disabled=true;
  try{const r=await fetch("/api/sources/"+cb.dataset.on+"/enabled",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({enabled:cb.checked})});
    if(!r.ok){sMsg("err",esc(errText(await r.text())));cb.checked=!cb.checked;}}
  catch(err){cb.checked=!cb.checked;sMsg("err","Couldn't reach the Command Center.");}
  loadSources();
});

// ---- CIG Pipeline tab ----
let gPhase="",gMode="";
document.getElementById("gRefresh").onclick=loadCIG;
document.getElementById("gMode").onchange=e=>{gMode=e.target.value;loadCIG();};
// Mode options with counts, in a fixed order (Unspecified last); only modes present in the snapshot.
const G_MODES=["BRT","Light Rail","Heavy Rail","Commuter Rail","Streetcar","Unspecified"];
function gModeOptions(byMode){
  const sel=document.getElementById("gMode"),keys=Object.keys(byMode||{});
  const order=G_MODES.filter(m=>keys.includes(m)).concat(keys.filter(m=>!G_MODES.includes(m)).sort());
  sel.innerHTML='<option value="">All modes</option>'+order.map(m=>'<option value="'+esc(m)+'"'+(m===gMode?" selected":"")+'>'+esc(m)+' ('+byMode[m]+')</option>').join("");
}
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
  loadChanges();loadLoads();loadProfStatus();
  try{
    const qs=new URLSearchParams();if(gPhase)qs.set("phase",gPhase);if(gMode)qs.set("mode",gMode);
    const d=await (await fetch("/api/cig"+(qs.toString()?"?"+qs:""))).json();
    const s=d.summary||{},bp=s.by_phase||{};
    gModeOptions(s.by_mode);
    sum.innerHTML='<div class="rcard" style="padding:16px 18px;display:flex;gap:26px;flex-wrap:wrap;align-items:center">'
      +gStat(s.projects||0,"projects")+gStat("$"+(((s.total_cig_musd||0)/1000).toFixed(1))+"B","CIG requested")
      +gStat(bp.PD||0,"in development")+gStat(bp.Eng||0,"in engineering")
      +(()=>{const age=s.snapshot?Math.floor((Date.now()-new Date(s.snapshot+"T12:00:00"))/864e5):null;const stale=age!=null&&age>45;
        return '<div style="font-family:Archivo,sans-serif;font-size:11px;margin-left:auto;color:'+(stale?"var(--accent)":"var(--muted)")+'"'
          +(stale?' title="Download the newest dashboard at transit.dot.gov/CIG and upload it"':'')+'>snapshot '+esc(s.snapshot||"-")
          +(stale?' &middot; '+age+' days old - time to upload a new one':'')
          +(s.snapshots>1?' &middot; '+s.snapshots+' months of history':'')+'</div>';})()+'</div>';
    if(!d.projects||!d.projects.length){out.innerHTML='<div class="rcard"><div class="loading">'+(s.projects?'No projects match this phase and mode.':'No projects loaded yet. Download the CIG dashboard PDF at transit.dot.gov/CIG, then click Upload dashboard.')+'</div></div>';return;}
    // Shared component (static/t411.js): the same table, milestones and history the public site uses,
    // plus (here only) each project's archived profile versions and diffs.
    T411.renderCigTable(out, d.projects, {
      profileVersions: p => fetch("/api/cig/profile-versions?name="+encodeURIComponent(p.project_name)+"&sponsor="+encodeURIComponent(p.sponsor||""))
        .then(r => r.ok ? r.json() : Promise.reject(r.status)),
      profileOpts: {
        fileUrl: id => "/api/cig/profile-versions/"+id+"/file",
        fetchDiff: id => fetch("/api/cig/profile-versions/"+id+"/diff").then(r => r.ok ? r.json() : Promise.reject(r.status)),
        onUpload: async (p, f) => {
          const r = await fetch("/api/cig/profiles/upload?filename="+encodeURIComponent(f.name)+"&name="+encodeURIComponent(p.project_name)
            +"&sponsor="+encodeURIComponent(p.sponsor||""), {method:"POST", headers:{"Content-Type":"application/pdf"}, body:f});
          const t = await r.text();
          if (!r.ok) throw new Error("Not archived: "+errText(t));
          const x = JSON.parse(t);
          return {text: gProfText(x), reload: () => { loadCIG(); loadProfStatus(); }};
        }
      }
    });
  }catch(e){out.innerHTML='<div class="rcard"><div class="err">Could not load the pipeline.</div></div>';}
}

// ---- Project profiles: archive status, uploads, the saved listing page, and the weekly check ----
function gProfText(x){
  const who=x.project_name?" ("+x.project_name+")":"";
  return {new:"Archived as the first version"+who+".",changed:"New version archived"+who+": +"+(x.lines_added||0)+" / −"+(x.lines_removed||0)+" lines changed.",
    unchanged:"Already archived"+who+" - same text as "+(x.is_latest?"the current":"an earlier")+" version, so nothing was stored.",
    unmatched:"Not archived: "+(x.message||"no matching project")+". Open the project below and use Upload a newer version.",
    unreadable:"Not archived: "+(x.message||"unreadable")+"."}[x.status]||x.status;
}
async function loadProfStatus(){
  const el=document.getElementById("gProfStat");
  try{
    const r=await fetch("/api/cig/profiles/status");if(!r.ok){el.textContent="";return;}
    const s=await r.json(),lc=s.last_check;
    el.innerHTML=esc(s.archived+" of "+s.projects+" projects archived · "+s.linked+" linked to FTA's page · "+s.versions+" versions ("+s.changed+" revisions)")
      +(lc?'<br>Last check '+esc(lc.at)+': '+esc(lc.listing_status||"")+(lc.files?" · "+esc(lc.files+" file(s) from the inbox"):""):"<br>No weekly check yet")
      +'<br><span title="'+esc(s.inbox)+'">Inbox: data/cig_profiles/inbox on the NAS</span>'
      +(s.listing_at?' · FTA page loaded '+esc(s.listing_at)+((s.listing_changes||[]).length?" ("+s.listing_changes.length+" change(s) vs. the copy before)":""):"");
    // What to fetch next: listing changes (new, stage, link), a newer PDF on FTA's page, or nothing archived.
    const td=s.to_download||[],box=document.getElementById("gProfTodo");
    box.innerHTML=td.length?'<details'+(td.length<=8?" open":"")+'><summary style="cursor:pointer;font-family:Archivo,sans-serif;font-weight:700;font-size:12px">To download ('+td.length+')</summary>'
      +'<div style="font-family:Archivo,sans-serif;font-size:12px;padding:6px 0 2px">'+td.map(x=>{
        const u=safeUrl(x.pdf_url)||safeUrl(x.profile_url);
        return '<div style="padding:3px 0">'+(u?'<a href="'+esc(u)+'" target="_blank" rel="noopener noreferrer">'+esc(x.project_name)+'</a>':esc(x.project_name))
          +' <span style="color:var(--muted)">— '+esc(x.why)+'</span></div>';}).join("")
      +'<div style="color:var(--muted);padding-top:4px">Open each in your browser, download the PDF, then Upload profiles (or drop them in the inbox).</div></div></details>'
      :'<div style="font-family:Archivo,sans-serif;font-size:12px;color:var(--muted)">Nothing to download: every listed project has its current profile archived.</div>';
  }catch(e){el.textContent="";}
}
document.getElementById("gProfUpBtn").onclick=()=>document.getElementById("gProfFile").click();
document.getElementById("gProfFile").addEventListener("change",async e=>{
  const files=[...e.target.files];e.target.value="";if(!files.length)return;
  const btn=document.getElementById("gProfUpBtn");btn.disabled=true;const lines=[];
  for(const f of files){
    gNote("loading","Archiving "+f.name+"...");
    try{const r=await fetch("/api/cig/profiles/upload?filename="+encodeURIComponent(f.name),{method:"POST",headers:{"Content-Type":"application/pdf"},body:f});
      const t=await r.text();lines.push(f.name+": "+(r.ok?gProfText(JSON.parse(t)):"Not archived: "+errText(t)));}
    catch(err){lines.push(f.name+": couldn't reach the Command Center.");}
  }
  gNote("loading",lines.join("  •  "));btn.disabled=false;loadCIG();loadProfStatus();
});
document.getElementById("gListBtn").onclick=()=>document.getElementById("gListFile").click();
document.getElementById("gListFile").addEventListener("change",async e=>{
  const f=e.target.files[0];e.target.value="";if(!f)return;
  gNote("loading","Reading "+f.name+"...");
  try{const r=await fetch("/api/cig/profiles/listing",{method:"POST",headers:{"Content-Type":"text/html"},body:f});
    const t=await r.text();
    if(!r.ok){gNote("err","Not loaded: "+errText(t));}
    else{const d=JSON.parse(t);
      gNote("loading","Profile links: "+d.matched+" of "+d.projects+" projects matched ("+d.listed+" on FTA's page)."
        +(d.compared_with?" Compared with the copy from "+d.compared_with+": "+(d.changes.length?d.changes.map(c=>c.name+" ("+c.detail+")").join("; "):"no changes")+".":"")
        +" To download: "+d.to_download.length+"."
        +(d.projects_not_listed.length?" Not on FTA's page: "+d.projects_not_listed.join(", ")+".":"")
        +(d.listed_not_in_dashboard.length?" On FTA's page but not the dashboard: "+d.listed_not_in_dashboard.join(", ")+".":""));
      loadCIG();loadProfStatus();}
  }catch(err){gNote("err","Couldn't reach the Command Center.");}
});
document.getElementById("gProfCheck").onclick=async()=>{
  const btn=document.getElementById("gProfCheck");btn.disabled=true;gNote("loading","Checking the inbox and FTA's listing page...");
  try{const r=await fetch("/api/cig/profiles/check",{method:"POST"});const t=await r.text();
    if(!r.ok)gNote("err",errText(t));
    else{const d=JSON.parse(t),res=d.results||[];
      gNote("loading","FTA: "+d.listing_status+". Inbox: "+(res.length?res.map(x=>x.file+" → "+x.status).join(", "):"empty")+".");
      loadCIG();}
  }catch(err){gNote("err","Couldn't reach the Command Center.");}
  btn.disabled=false;loadProfStatus();
};

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
        gNote("loading",name+" isn't in the current table"+(gPhase||gMode?" (try All phases and All modes)":" (it was dropped from the latest dashboard)")+".");
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
