#!/usr/bin/env python3
"""
Transit411 collection engine - populates the review queue.

  python collection.py --seed        # load the source registry into Postgres (once)
  python collection.py --run         # fetch sources, summarize, score, insert pending items
  python collection.py --run --limit 5   # only the first 5 sources (testing)

Writes to `collected_items` and `sources`. The Command Center's Collection tab
reviews what this produces. Meant to run on a schedule (docker compose run --rm collect),
not inside a web request.
"""
import argparse
import math
import os
from datetime import datetime, timezone


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


def seed_sources(conn):
    """Sync the registry into Postgres by source name: add new sources and update existing ones
    (URL, method, notes...), so corrections here reach the database. Safe to re-run."""
    added = updated = 0
    with conn.cursor() as cur:
        for name, url, pillar, typ, method, trust, notes in SOURCES:
            cur.execute("UPDATE sources SET url=%s, pillar=%s, type=%s, method=%s, trust=%s, notes=%s "
                        "WHERE name=%s", (url, pillar, typ, method, trust, notes, name))
            if cur.rowcount:
                updated += 1
            else:
                cur.execute("INSERT INTO sources (name, url, pillar, type, method, trust, notes) "
                            "VALUES (%s,%s,%s,%s,%s,%s,%s)", (name, url, pillar, typ, method, trust, notes))
                added += 1
    conn.commit()
    return added, updated


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
        max_tokens=400,
        system=("You triage transit-industry news for an editorial queue aimed at agency staff, "
                "consultants and contractors. Given a feed item, return ONLY JSON:\n"
                '{"pillar":"Funding|Procurement|People|Policy","headline":"rewritten, <=14 words, original wording",'
                '"summary":"1-2 sentence paraphrase, no copied text","relevance":"high|med|low"}\n'
                "Bias toward funding/procurement/people/policy that affects the capital pipeline. "
                "Low relevance for operations, safety-incident, or consumer stories."),
        messages=[{"role": "user", "content": f"Title: {entry.get('title','')}\nSummary: {entry.get('summary','')}\nLink: {entry.get('link','')}"}])
    text = "".join(b.text for b in msg.content if b.type == "text").strip()
    text = re.sub(r"^```json|^```|```$", "", text, flags=re.M).strip()
    try:
        return json.loads(text)
    except Exception:
        return None


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


def insert_item(conn, pillar, headline, summary, source_name, source_url, published, relevance):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO collected_items (pillar, headline, summary, source_name, source_url, published, relevance, status) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,'pending')",
            (pillar, headline, summary, source_name, source_url,
             published.date() if published else None, relevance))
    conn.commit()


def run(conn, limit_sources=None):
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
                continue
            insert_item(conn, c.get("pillar"), c.get("headline") or e["title"],
                        c.get("summary"), name, e["link"], e["published"], c.get("relevance", "med"))
            kept += 1
        added += kept
        print(f"  {name}: {len(entries)} in feed, {new} new, {kept} queued, {low} low relevance"
              + (f", {errors} not classified" if errors else ""))
    print(f"Added {added} pending items from {len(sources) - len(failed)} of {len(sources)} sources."
          + (f" Failed: {', '.join(failed)}." if failed else ""))
    return added


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", action="store_true")
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--limit", type=int)
    a = ap.parse_args()
    import psycopg
    dsn = os.environ.get("DATABASE_URL", "postgresql://transit411:transit411@db:5432/transit411")
    with psycopg.connect(dsn) as conn:
        if a.seed:
            added, updated = seed_sources(conn)
            print(f"Sources: {added} added, {updated} updated ({len(SOURCES)} in the registry).")
        if a.run:
            run(conn, a.limit)
        if not (a.seed or a.run):
            print("Nothing to do. Use --seed and/or --run.")


if __name__ == "__main__":
    main()
