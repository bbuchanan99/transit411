#!/usr/bin/env python3
"""
Transit411 collection engine - populates the review queue.

  python collection.py --seed        # load the source registry into Postgres (once)
  python collection.py --run         # fetch sources, summarize, score, insert pending items
  python collection.py --run --limit 5   # only the first 5 sources (testing)
  python collection.py --schedule    # stay running; collect daily at COLLECT_AT (the scheduler service)
  python collection.py --migrate     # add new columns to an existing database
  python collection.py --backfill    # one-off: add facets to items collected before facets existed

Writes to `collected_items` and `sources`. The Command Center's Collection tab
reviews what this produces. The daily run can be switched off from the Command Center's
Auto-collect toggle (stored in app_settings) to save model tokens during development.
"""
import argparse
import math
import os
from datetime import datetime, timezone
from urllib.parse import quote_plus


def _to_dt(v):
    if v is None:
        return None
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    try:
        return datetime.fromisoformat(str(v)).replace(tzinfo=timezone.utc)
    except Exception:
        return None


def freshness(published, deadline=None, evergreen=False, now=None):
    """Return (status, score 0-100). News decays fast; deadlines stay Live until they pass;
    developing stories decay slowly."""
    now = now or datetime.now(timezone.utc)
    published, deadline = _to_dt(published), _to_dt(deadline)
    age = max(0.0, (now - published).total_seconds() / 86400) if published else 999.0
    if deadline:
        until = (deadline - now).total_seconds() / 86400
        if until < 0:
            return ("Expired", 6)
        return ("Live", int(max(60, min(100, 100 - min(until, 60) * 0.5))))
    if evergreen:
        return ("Developing", int(max(22, 100 * math.exp(-age / 12))))
    score = int(100 * math.exp(-age / 3))
    status = "Fresh" if age <= 2 else "Recent" if age <= 7 else "Aging" if age <= 21 else "Stale"
    return (status, score)


# (name, url, pillar, type, method, trust, notes). Only method 'RSS' is fetched by --run.
# Sites that refuse automated requests (HTTP 403 bot protection) are 'Manual': we don't try to get
# around those blocks. Checked from the NAS on 2026-09-21.
BLOCKED = "Returns HTTP 403 to automated requests (bot protection), checked 2026-09-21. Review manually."
SOURCES = [
    ("FTA Newsroom", "https://www.transit.dot.gov/about/news", "Funding", "Official", "Manual", "High",
     "News web page, not a feed. " + BLOCKED),
    ("Grants.gov", "https://www.grants.gov", "Funding", "Official", "API", "High", None),
    ("SAM.gov Contract Opportunities", "https://sam.gov/content/opportunities", "Procurement", "Official", "API", "High", None),
    ("Federal Register (DOT/FTA)", "https://www.federalregister.gov", "Policy", "Official", "API", "High", None),
    ("Mass Transit Magazine",
     "https://www.masstransitmag.com/__rss/website-scheduled-content.xml?input=%7B%22sectionAlias%22%3A%22home%22%7D",
     "Funding", "Trade", "RSS", "High", "Feed advertised on the site's home page (/rss is a 404)."),
    ("METRO Magazine", "https://www.metro-magazine.com/rss", "Funding", "Trade", "RSS", "High", None),
    ("Railway Age", "https://www.railwayage.com/feed", "Funding", "Trade", "Manual", "High", BLOCKED),
    ("Progressive Railroading", "https://www.progressiverailroading.com/rss/", "Procurement", "Trade", "Manual", "Med",
     "/rss/ is a page listing the site's feeds, not a feed; pick a specific feed URL to automate."),
    ("Smart Cities Dive", "https://www.smartcitiesdive.com/feeds/news/", "Policy", "Trade", "RSS", "Med", None),
    ("APTA", "https://www.apta.com/feed/", "Policy", "Association", "Manual", "High", BLOCKED),
    ("Eno Center for Transportation", "https://enotrans.org/feed/", "Policy", "Association", "Manual", "Med", BLOCKED),
]

# Keyword searches via Google News RSS. These route AROUND sites that block bots:
# Google has already indexed the trade press, so a keyword search surfaces their stories
# without us hitting their 403 walls. "when:Nd" limits each query to the last N days.
SEARCH_QUERIES = [
    ("transit funding when:21d", "Funding"),
    ("FTA grant OR NOFO transit when:30d", "Funding"),
    ("transit capital project funding when:21d", "Funding"),
    ("transit ballot measure OR sales tax when:30d", "Funding"),
    ("light rail OR bus rapid transit contract award when:30d", "Procurement"),
    # Split from "transit progressive design-build OR RFP OR RFQ" (2 hits in 30 days; these found 12 and 39).
    ('transit agency RFP OR "request for proposals" OR "request for qualifications" when:30d', "Procurement"),
    ('transit "design-build" when:30d', "Procurement"),
    ("transit agency CEO OR general manager appointed when:30d", "People"),
    ("FTA OR transit surface transportation reauthorization when:30d", "Policy"),
]


def google_news_url(query):
    return ("https://news.google.com/rss/search?q=" + quote_plus(query)
            + "&hl=en-US&gl=US&ceid=US:en")


# Rendered as ordinary RSS sources so the existing fetch/classify/dedup pipeline handles them.
SEARCH_SOURCES = [
    (f"Google News: {q.split(' when:')[0]}", google_news_url(q), pillar, "Search", "RSS", "Med",
     "Keyword search via Google News RSS.")
    for q, pillar in SEARCH_QUERIES
]

# Everything --seed loads and --run fetches.
ALL_SOURCES = SOURCES + SEARCH_SOURCES


def seed_sources(conn):
    """Sync the registry into Postgres by source name: add new sources and update existing ones
    (URL, method, notes...), so corrections here reach the database. Keyword searches that were
    removed from SEARCH_QUERIES are marked 'Retired' so --run stops fetching them. Safe to re-run."""
    added = updated = 0
    with conn.cursor() as cur:
        for name, url, pillar, typ, method, trust, notes in ALL_SOURCES:
            cur.execute("UPDATE sources SET url=%s, pillar=%s, type=%s, method=%s, trust=%s, notes=%s "
                        "WHERE name=%s", (url, pillar, typ, method, trust, notes, name))
            if cur.rowcount:
                updated += 1
            else:
                cur.execute("INSERT INTO sources (name, url, pillar, type, method, trust, notes) "
                            "VALUES (%s,%s,%s,%s,%s,%s,%s)", (name, url, pillar, typ, method, trust, notes))
                added += 1
        cur.execute("UPDATE sources SET method='Retired', notes='No longer in SEARCH_QUERIES.' "
                    "WHERE type='Search' AND method <> 'Retired' AND NOT (name = ANY(%s))",
                    ([s[0] for s in SEARCH_SOURCES],))
        retired = cur.rowcount
    conn.commit()
    return added, updated, retired


def classify(entry):
    """entry: {title, summary, link}. Returns {pillar, headline, summary, relevance} or None."""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return None
    import json
    import re
    import anthropic
    client = anthropic.Anthropic(api_key=key)
    msg = client.messages.create(
        model=os.environ.get("COLLECT_MODEL", "claude-haiku-4-5"),
        max_tokens=600,
        system=("You triage transit-industry news for an editorial queue aimed at agency staff, "
                "consultants and contractors. Given a feed item, return ONLY JSON:\n"
                '{"pillar":"Funding|Procurement|People|Policy","headline":"rewritten, <=14 words, original wording",'
                '"summary":"1-2 sentence paraphrase, no copied text","relevance":"high|med|low",'
                '"agencies":["exact transit agency names mentioned"],"state":"2-letter US state code or empty",'
                '"mode":["any of: Bus, BRT, Light Rail, Heavy Rail, Commuter Rail, Streetcar, Ferry, Multimodal"],'
                '"programs":["any of: CIG New Starts, CIG Small Starts, CIG Core Capacity, TIFIA, RRIF, RAISE, INFRA, Formula, Ballot Measure, P3"],'
                '"tags":["short free-form topic, project, or firm tags"]}\n'
                "Use exact agency names; leave any array empty when nothing applies. "
                "Bias toward funding/procurement/people/policy that affects the capital pipeline. "
                "Low relevance for operations, safety-incident, or consumer stories."),
        messages=[{"role": "user", "content": f"Title: {entry.get('title','')}\nSummary: {entry.get('summary','')}\nLink: {entry.get('link','')}"}])
    text = "".join(b.text for b in msg.content if b.type == "text").strip()
    text = re.sub(r"^```json|^```|```$", "", text, flags=re.M).strip()
    try:
        return json.loads(text)
    except Exception:
        return None


MODES = ["Bus", "BRT", "Light Rail", "Heavy Rail", "Commuter Rail", "Streetcar", "Ferry", "Multimodal"]
PROGRAMS = ["CIG New Starts", "CIG Small Starts", "CIG Core Capacity", "TIFIA", "RRIF", "RAISE", "INFRA",
            "Formula", "Ballot Measure", "P3"]
US_STATES = set("AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ "
                "NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY PR GU VI AS MP".split())


def clean_facets(c):
    """Make the model's facet fields safe to store and filter on: arrays really are lists of short
    strings (a bare string like "Bus" becomes ["Bus"]), mode/programs keep only the allowed values
    (case-insensitive), and state is a real 2-letter code or None."""
    def as_list(v, limit=12, size=120):
        if isinstance(v, str):
            v = [v]
        if not isinstance(v, list):
            return []
        out = []
        for x in v:
            s = str(x).strip()[:size] if isinstance(x, (str, int, float)) else ""
            if s and s not in out:
                out.append(s)
        return out[:limit]

    def pick(v, allowed):
        canon = {a.lower(): a for a in allowed}
        return [canon[s.lower()] for s in as_list(v) if s.lower() in canon]

    state = str(c.get("state") or "").strip().upper()
    return {"agencies": as_list(c.get("agencies")), "mode": pick(c.get("mode"), MODES),
            "programs": pick(c.get("programs"), PROGRAMS), "tags": as_list(c.get("tags"), size=60),
            "state": state if state in US_STATES else None}


USER_AGENT = "Mozilla/5.0 (compatible; Transit411FeedReader/1.0; +https://github.com/bbuchanan99/transit411)"


class FetchError(Exception):
    pass


def fetch_source(url, limit=15):
    """Return up to `limit` entries; raise FetchError with a readable reason when the source
    is blocked, missing, or not actually a feed (instead of silently returning nothing)."""
    import feedparser
    import requests
    try:
        r = requests.get(url, timeout=30, headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, */*;q=0.8"})
    except requests.RequestException as e:
        raise FetchError(f"request failed: {type(e).__name__}")
    if r.status_code == 403:
        raise FetchError("HTTP 403 - site blocks automated requests")
    if r.status_code >= 400:
        raise FetchError(f"HTTP {r.status_code}")
    feed = feedparser.parse(r.content)
    if not feed.entries and feed.bozo:
        ctype = r.headers.get("content-type", "unknown").split(";")[0]
        raise FetchError(f"not a feed (got {ctype})")
    out = []
    for e in feed.entries[:limit]:
        published = None
        if getattr(e, "published_parsed", None):
            published = datetime(*e.published_parsed[:6], tzinfo=timezone.utc)
        out.append({"title": getattr(e, "title", ""), "summary": getattr(e, "summary", ""),
                    "link": getattr(e, "link", ""), "published": published})
    return out


def item_exists(conn, url):
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM collected_items WHERE source_url=%s LIMIT 1", (url,))
        return cur.fetchone() is not None


def insert_item(conn, pillar, headline, summary, source_name, source_url, published, relevance,
                status="pending", agencies=None, mode=None, programs=None, tags=None, state=None):
    """status 'filtered' records an item the model rated low relevance: kept out of the review
    queue, but its link is remembered so later runs never pay to triage it again."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO collected_items (pillar, headline, summary, source_name, source_url, published, relevance, status, "
            "agencies, mode, programs, tags, state) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s, %s,%s,%s,%s,%s)",
            (pillar, headline, summary, source_name, source_url,
             published.date() if published else None, relevance, status,
             agencies or [], mode or [], programs or [], tags or [], state))
    conn.commit()


def run(conn, limit_sources=None):
    migrate(conn)
    with conn.cursor() as cur:
        # Only RSS sources are fetched for now; --limit counts those, not skipped API/Scrape ones.
        cur.execute("SELECT name, url FROM sources WHERE method = 'RSS' ORDER BY id")
        sources = cur.fetchall()
    if limit_sources:
        sources = sources[:limit_sources]
    added, failed = 0, []
    for name, url in sources:
        try:
            entries = fetch_source(url)
        except FetchError as e:
            print(f"  ! {name}: FAILED - {e}")
            failed.append(name)
            continue
        new = low = errors = kept = 0
        for e in entries:
            if not e["link"] or item_exists(conn, e["link"]):
                continue
            new += 1
            try:
                c = classify(e)
            except Exception as ex:  # one bad model call shouldn't stop the whole run
                errors += 1
                print(f"    - classify failed for {e['link']}: {type(ex).__name__}")
                continue
            if not c:
                errors += 1
                continue
            if c.get("relevance") == "low":
                low += 1
                insert_item(conn, c.get("pillar"), c.get("headline") or e["title"], c.get("summary"),
                            name, e["link"], e["published"], "low", status="filtered")
                continue
            insert_item(conn, c.get("pillar"), c.get("headline") or e["title"],
                        c.get("summary"), name, e["link"], e["published"], c.get("relevance", "med"),
                        **clean_facets(c))
            kept += 1
        added += kept
        print(f"  {name}: {len(entries)} in feed, {new} new, {kept} queued, {low} low relevance"
              + (f", {errors} not classified" if errors else ""))
    print(f"Added {added} pending items from {len(sources) - len(failed)} of {len(sources)} sources."
          + (f" Failed: {', '.join(failed)}." if failed else ""))
    return {"added": added, "sources": len(sources), "failed": failed}


# ---- Settings + daily schedule -------------------------------------------------------------
# app_settings holds small JSON values shared with the Command Center:
#   auto_collect      {"enabled": bool}            - the header toggle; checked right before each run
#   collect_schedule  {"at", "tz", "next_run"}     - written by the scheduler for display
#   collect_last_run  {"at", "added", "failed", "skipped", "error"}
SETTINGS_DDL = ("CREATE TABLE IF NOT EXISTS app_settings (key TEXT PRIMARY KEY, value JSONB NOT NULL, "
                "updated_at TIMESTAMPTZ DEFAULT now())")


def get_setting(conn, key, default=None):
    with conn.cursor() as cur:
        cur.execute(SETTINGS_DDL)
        cur.execute("SELECT value FROM app_settings WHERE key=%s", (key,))
        row = cur.fetchone()
    conn.commit()
    return row[0] if row else default


def set_setting(conn, key, value):
    from psycopg.types.json import Jsonb
    with conn.cursor() as cur:
        cur.execute(SETTINGS_DDL)
        cur.execute("INSERT INTO app_settings (key, value, updated_at) VALUES (%s, %s, now()) "
                    "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
                    (key, Jsonb(value)))
    conn.commit()


def auto_collect_enabled(conn):
    return bool(get_setting(conn, "auto_collect", {"enabled": True}).get("enabled", True))


def schedule_loop(dsn):
    """Run the collector once a day at COLLECT_AT (HH:MM) in COLLECT_TZ. The Auto-collect toggle is
    checked at run time, so turning it off skips the run and spends no model tokens."""
    import time
    from datetime import timedelta
    from zoneinfo import ZoneInfo
    import psycopg
    at = os.environ.get("COLLECT_AT", "06:00")
    tz = ZoneInfo(os.environ.get("COLLECT_TZ", "America/New_York"))
    hh, mm = (int(x) for x in at.split(":"))
    print(f"Scheduler: daily collection at {at} {tz.key}", flush=True)
    while True:
        try:
            now = datetime.now(tz)
            nxt = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
            if nxt <= now:
                nxt += timedelta(days=1)
            with psycopg.connect(dsn) as conn:
                set_setting(conn, "collect_schedule", {"at": at, "tz": tz.key, "next_run": nxt.isoformat()})
            print(f"Scheduler: next run {nxt.isoformat()}", flush=True)
            while (remaining := (nxt - datetime.now(tz)).total_seconds()) > 0:
                time.sleep(min(remaining, 300))
            with psycopg.connect(dsn) as conn:
                stamp = datetime.now(tz).isoformat()
                if not auto_collect_enabled(conn):
                    print("Scheduler: auto-collect is off; skipping this run.", flush=True)
                    set_setting(conn, "collect_last_run", {"at": stamp, "skipped": True})
                    continue
                try:
                    result = run(conn)
                    set_setting(conn, "collect_last_run", {"at": stamp, **result})
                except Exception as e:
                    print(f"Scheduler: run failed: {e}", flush=True)
                    set_setting(conn, "collect_last_run", {"at": stamp, "error": str(e)[:300]})
        except Exception as e:  # e.g. database restarting: wait and try again, never exit
            print(f"Scheduler: {type(e).__name__}: {e}; retrying in 60s", flush=True)
            time.sleep(60)


def migrate(conn):
    """Add facet + featured columns to an existing database. Idempotent; safe to run every time."""
    stmts = [
        "ALTER TABLE collected_items ADD COLUMN IF NOT EXISTS agencies TEXT[]",
        "ALTER TABLE collected_items ADD COLUMN IF NOT EXISTS mode TEXT[]",
        "ALTER TABLE collected_items ADD COLUMN IF NOT EXISTS programs TEXT[]",
        "ALTER TABLE collected_items ADD COLUMN IF NOT EXISTS tags TEXT[]",
        "ALTER TABLE collected_items ADD COLUMN IF NOT EXISTS state TEXT",
        "ALTER TABLE content_posts ADD COLUMN IF NOT EXISTS source_name TEXT",
        "ALTER TABLE content_posts ADD COLUMN IF NOT EXISTS source_url TEXT",
        "ALTER TABLE content_posts ADD COLUMN IF NOT EXISTS agencies TEXT[]",
        "ALTER TABLE content_posts ADD COLUMN IF NOT EXISTS mode TEXT[]",
        "ALTER TABLE content_posts ADD COLUMN IF NOT EXISTS programs TEXT[]",
        "ALTER TABLE content_posts ADD COLUMN IF NOT EXISTS tags TEXT[]",
        "ALTER TABLE content_posts ADD COLUMN IF NOT EXISTS state TEXT",
        "ALTER TABLE content_posts ADD COLUMN IF NOT EXISTS featured BOOLEAN DEFAULT false",
        "ALTER TABLE content_posts ADD COLUMN IF NOT EXISTS featured_until TIMESTAMPTZ",
        "ALTER TABLE content_posts ADD COLUMN IF NOT EXISTS sponsor TEXT",
        "ALTER TABLE content_posts ADD COLUMN IF NOT EXISTS source_type TEXT DEFAULT 'collected'",
        "CREATE INDEX IF NOT EXISTS ci_agencies_idx ON collected_items USING GIN (agencies)",
        "CREATE INDEX IF NOT EXISTS cp_agencies_idx ON content_posts USING GIN (agencies)",
        "CREATE INDEX IF NOT EXISTS cp_tags_idx ON content_posts USING GIN (tags)",
        "CREATE INDEX IF NOT EXISTS cp_mode_idx ON content_posts USING GIN (mode)",
        "CREATE INDEX IF NOT EXISTS cp_programs_idx ON content_posts USING GIN (programs)",
    ]
    with conn.cursor() as cur:
        for st in stmts:
            cur.execute(st)
    conn.commit()


def classify_facets(item):
    """Facets only, for items triaged before facets existed. Shorter prompt and reply than
    classify(); leaves headline/summary/relevance alone."""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return None
    import json
    import re
    import anthropic
    client = anthropic.Anthropic(api_key=key)
    msg = client.messages.create(
        model=os.environ.get("COLLECT_MODEL", "claude-haiku-4-5"),
        max_tokens=300,
        system=("Tag a transit-industry news item. Return ONLY JSON:\n"
                '{"agencies":["exact transit agency names mentioned"],"state":"2-letter US state code or empty",'
                f'"mode":["any of: {", ".join(MODES)}"],"programs":["any of: {", ".join(PROGRAMS)}"],'
                '"tags":["short free-form topic, project, or firm tags"]}\n'
                "Use exact agency names; leave any array empty when nothing applies."),
        messages=[{"role": "user", "content": f"Headline: {item['headline']}\nSummary: {item.get('summary') or ''}"
                                              f"\nLink: {item.get('source_url') or ''}"}])
    text = "".join(b.text for b in msg.content if b.type == "text").strip()
    text = re.sub(r"^```json|^```|```$", "", text, flags=re.M).strip()
    try:
        return json.loads(text)
    except Exception:
        return None


def backfill(conn, limit=None):
    """One-off: add facets to pending/approved/published items collected before facets existed
    (agencies IS NULL), and copy them onto any posts made from those items. Items whose model call
    fails stay NULL, so re-running retries just those."""
    migrate(conn)
    with conn.cursor() as cur:
        cur.execute("SELECT id, headline, summary, source_url FROM collected_items "
                    "WHERE agencies IS NULL AND status IN ('pending','approved','published') ORDER BY id"
                    + (" LIMIT %s" % int(limit) if limit else ""))
        items = [dict(zip(("id", "headline", "summary", "source_url"), r)) for r in cur.fetchall()]
    done = failed = 0
    for it in items:
        try:
            c = classify_facets(it)
        except Exception as e:
            print(f"  - item {it['id']}: {type(e).__name__}")
            c = None
        if not c:
            failed += 1
            continue
        f = clean_facets(c)
        with conn.cursor() as cur:
            cur.execute("UPDATE collected_items SET agencies=%s, mode=%s, programs=%s, tags=%s, state=%s "
                        "WHERE id=%s", (f["agencies"], f["mode"], f["programs"], f["tags"], f["state"], it["id"]))
            cur.execute("UPDATE content_posts SET agencies=%s, mode=%s, programs=%s, tags=%s, state=%s "
                        "WHERE item_id=%s", (f["agencies"], f["mode"], f["programs"], f["tags"], f["state"], it["id"]))
        conn.commit()
        done += 1
        if done % 25 == 0:
            print(f"  {done}/{len(items)} tagged", flush=True)
    print(f"Backfill: {done} tagged, {failed} failed (re-run to retry), {len(items)} needed facets.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", action="store_true")
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--migrate", action="store_true")
    ap.add_argument("--backfill", action="store_true",
                    help="one-off: add facets to existing items collected before facets existed")
    ap.add_argument("--schedule", action="store_true",
                    help="stay running and collect daily at COLLECT_AT, unless Auto-collect is off")
    a = ap.parse_args()
    import psycopg
    dsn = os.environ.get("DATABASE_URL", "postgresql://transit411:transit411@db:5432/transit411")
    if a.schedule:
        schedule_loop(dsn)
        return
    with psycopg.connect(dsn) as conn:
        if a.migrate:
            migrate(conn)
            print("Schema migrated (facet + featured columns ensured).")
        if a.seed:
            added, updated, retired = seed_sources(conn)
            print(f"Sources: {added} added, {updated} updated, {retired} retired "
                  f"({len(ALL_SOURCES)} in the registry).")
        if a.run:
            # Manual runs always go ahead; the Auto-collect toggle only governs the daily schedule.
            run(conn, a.limit)
        if a.backfill:
            backfill(conn, None if a.run else a.limit)
        if not (a.seed or a.run or a.migrate or a.backfill):
            print("Nothing to do. Use --migrate, --seed, --run, --backfill and/or --schedule.")


if __name__ == "__main__":
    main()
