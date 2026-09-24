"""What people actually use, recorded so pricing can be decided from evidence.

Every Ask NTD / Ask CIG question and every signup writes a row here. That dataset is the point:
before charging for anything we should know which tool is used, how often, by how many distinct
sessions, and what shape of question people ask.

THIS MUST NEVER AFFECT A REQUEST. Logging happens on a background thread behind a bounded queue.
If the database is slow, the queue fills and events are dropped rather than making anyone wait; if
a write fails, it is counted and forgotten. `log()` does not raise, and callers do not check it.

Private: the table lives in the Command Center's database and no public endpoint reads or writes it.
Usage rows are written server-side, from endpoints the tunnel already exposes - nothing new was
opened to the internet for this.
"""

import json
import os
import queue
import threading

MAX_QUEUE = 500          # ~a minute of heavy traffic; beyond this we drop rather than block
MAX_QUERY_CHARS = 1000

EVENT_TYPES = ("ask_ntd", "ask_cig", "card_export", "data_export", "subscribe", "page_tool_view")

_q = queue.Queue(maxsize=MAX_QUEUE)
_worker = None
_lock = threading.Lock()
_stats = {"queued": 0, "written": 0, "dropped": 0, "failed": 0}


def schema(cur):
    """Idempotent; called before the first write and by the Usage view."""
    cur.execute("""CREATE TABLE IF NOT EXISTS usage_events (
        id           BIGSERIAL PRIMARY KEY,
        event_type   TEXT NOT NULL,
        contact_id   BIGINT,
        session_id   TEXT,
        query_text   TEXT,
        result_shape TEXT,
        meta         JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at   TIMESTAMPTZ NOT NULL DEFAULT now())""")
    cur.execute("CREATE INDEX IF NOT EXISTS usage_events_time_idx ON usage_events (created_at DESC)")
    cur.execute("CREATE INDEX IF NOT EXISTS usage_events_type_time_idx "
                "ON usage_events (event_type, created_at DESC)")
    # Plan scaffolding lives beside the contact it describes (entitlements.py reads it).
    cur.execute("ALTER TABLE contacts ADD COLUMN IF NOT EXISTS plan TEXT NOT NULL DEFAULT 'free'")
    cur.execute("ALTER TABLE contacts ADD COLUMN IF NOT EXISTS plan_since TIMESTAMPTZ")


def _connect():
    import psycopg
    return psycopg.connect(os.environ.get("DATABASE_URL"), connect_timeout=5)


def _run():
    """One background thread, one connection, reopened if it dies."""
    conn = None
    while True:
        item = _q.get()
        if item is None:
            return
        for attempt in (1, 2):
            try:
                if conn is None or conn.closed:
                    conn = _connect()
                    with conn.cursor() as cur:
                        schema(cur)
                    conn.commit()
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO usage_events (event_type, contact_id, session_id, query_text, "
                        "result_shape, meta) VALUES (%s, %s, %s, %s, %s, %s::jsonb)", item)
                conn.commit()
                _stats["written"] += 1
                break
            except Exception:
                try:
                    if conn is not None:
                        conn.close()
                except Exception:
                    pass
                conn = None                  # reconnect once, then give up on this event
                if attempt == 2:
                    _stats["failed"] += 1


def _ensure_worker():
    global _worker
    if _worker is not None and _worker.is_alive():
        return
    with _lock:
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_run, name="usage-log", daemon=True)
            _worker.start()


def log(event_type, contact_id=None, session_id=None, query_text=None, result_shape=None, **meta):
    """Record one event. Returns nothing, raises nothing, and blocks for no measurable time."""
    try:
        if event_type not in EVENT_TYPES:
            meta = dict(meta, unknown_event=event_type)
            event_type = "page_tool_view"
        _ensure_worker()
        _q.put_nowait((
            event_type,
            contact_id,
            (session_id or None) and str(session_id)[:64],
            (query_text or None) and str(query_text)[:MAX_QUERY_CHARS],
            (result_shape or None) and str(result_shape)[:32],
            json.dumps({k: v for k, v in meta.items() if v is not None}, default=str)[:4000],
        ))
        _stats["queued"] += 1
    except queue.Full:
        _stats["dropped"] += 1
    except Exception:
        _stats["dropped"] += 1      # a logging bug must never become a request failure


def stats():
    return dict(_stats, pending=_q.qsize())


# ---- what shape of answer the question produced -------------------------------------------------
#
# Mirrors pickCardType() in static/t411.js, which decides the same thing in the browser for the
# data card. Kept deliberately coarse: this is for "are people asking for rankings or trends?",
# not for drawing anything, so small disagreements with the JS do not matter.

_YEAR = ("year", "report_year", "snapshot_date", "date")
_NOT_MEASURE = ("id", "ntd_id", "code", "zip")


def _is_year(col):
    c = (col or "").lower()
    return c in _YEAR or c.endswith("_year") or c.endswith("_date")


def result_shape(columns, rows):
    """-> 'stat' | 'trend' | 'ranked' | 'comparison' | 'table' | 'empty'."""
    columns = list(columns or [])
    rows = list(rows or [])
    if not rows or not columns:
        return "empty"

    def value(row, col, i):
        return row.get(col) if isinstance(row, dict) else (row[i] if i < len(row) else None)

    numeric, labels = [], []
    for i, c in enumerate(columns):
        vals = [value(r, c, i) for r in rows[:20]]
        real = [v for v in vals if isinstance(v, (int, float)) and not isinstance(v, bool)]
        if real and len(real) >= max(1, len(vals) // 2):
            numeric.append(c)
        elif any(isinstance(v, str) and v.strip() for v in vals):
            labels.append(c)
    measures = [c for c in numeric
                if not _is_year(c) and not any(c.lower().endswith(s) or c.lower() == s
                                               for s in _NOT_MEASURE)]
    if any(_is_year(c) for c in columns) and len(rows) > 2 and measures:
        return "trend"
    if len(rows) == 1:
        return "stat" if measures else "table"
    if labels and measures:
        return "ranked" if len(rows) > 5 else "comparison"
    return "table"
