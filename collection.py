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


SOURCES = [
    ("FTA Newsroom", "https://www.transit.dot.gov/about/news", "Funding", "Official", "RSS", "High"),
    ("Grants.gov", "https://www.grants.gov", "Funding", "Official", "API", "High"),
    ("SAM.gov Contract Opportunities", "https://sam.gov/content/opportunities", "Procurement", "Official", "API", "High"),
    ("Federal Register (DOT/FTA)", "https://www.federalregister.gov", "Policy", "Official", "API", "High"),
    ("Mass Transit Magazine", "https://www.masstransitmag.com/rss", "Funding", "Trade", "RSS", "High"),
    ("METRO Magazine", "https://www.metro-magazine.com/rss", "Funding", "Trade", "RSS", "High"),
    ("Railway Age", "https://www.railwayage.com/feed", "Funding", "Trade", "RSS", "High"),
    ("Progressive Railroading", "https://www.progressiverailroading.com/rss", "Procurement", "Trade", "RSS", "Med"),
    ("Smart Cities Dive", "https://www.smartcitiesdive.com/feeds/news/", "Policy", "Trade", "RSS", "Med"),
    ("APTA", "https://www.apta.com/feed/", "Policy", "Association", "RSS", "High"),
    ("Eno Center for Transportation", "https://enotrans.org/feed/", "Policy", "Association", "RSS", "Med"),
]


def seed_sources(conn):
    with conn.cursor() as cur:
        for name, url, pillar, typ, method, trust in SOURCES:
            cur.execute(
                "INSERT INTO sources (name, url, pillar, type, method, trust) VALUES (%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT DO NOTHING", (name, url, pillar, typ, method, trust))
    conn.commit()
    return len(SOURCES)


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


def fetch_source(url, limit=15):
    import feedparser
    feed = feedparser.parse(url)
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
        cur.execute("SELECT name, url, method FROM sources ORDER BY id")
        sources = cur.fetchall()
    if limit_sources:
        sources = sources[:limit_sources]
    added = 0
    for name, url, method in sources:
        if method != "RSS":
            continue
        try:
            entries = fetch_source(url)
        except Exception as e:
            print(f"  ! {name}: fetch failed ({e})")
            continue
        for e in entries:
            if not e["link"] or item_exists(conn, e["link"]):
                continue
            c = classify(e)
            if not c or c.get("relevance") == "low":
                continue
            insert_item(conn, c.get("pillar"), c.get("headline") or e["title"],
                        c.get("summary"), name, e["link"], e["published"], c.get("relevance", "med"))
            added += 1
        print(f"  {name}: processed")
    print(f"Added {added} pending items.")
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
            print(f"Seeded {seed_sources(conn)} sources.")
        if a.run:
            run(conn, a.limit)
        if not (a.seed or a.run):
            print("Nothing to do. Use --seed and/or --run.")


if __name__ == "__main__":
    main()
