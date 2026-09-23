#!/usr/bin/env python3
"""
Transit411 collection engine - populates the review queue.

  python collection.py --seed        # load the source registry into Postgres (once)
  python collection.py --run         # fetch sources, summarize, score, insert pending items
  python collection.py --run --limit 5   # only the first 5 sources (testing)
  python collection.py --schedule    # stay running; collect daily at COLLECT_AT (the scheduler service)
  python collection.py --migrate     # add new columns to an existing database
  python collection.py --backfill    # one-off: add facets to items collected before facets existed
  python collection.py --normalize   # re-apply agency name normalization (after editing AGENCY_ALIASES)
  python collection.py --list-sources  # what the next run will fetch (no fetching)

Writes to `collected_items` and `sources`. The Command Center's Collection tab
reviews what this produces; its Sources tab edits `sources` (each run reads it fresh). The daily run can be switched off from the Command Center's
Auto-collect toggle (stored in app_settings) to save model tokens during development.
"""
import argparse
import math
import os
import re
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


def search_source(query, days):
    """(name, url) for a Google News keyword search over the last `days` days."""
    query = re.sub(r"\s+when:\d+d\s*$", "", (query or "").strip())
    return f"Google News: {query}", google_news_url(f"{query} when:{int(days)}d")


def _split_search(q):
    """'transit funding when:21d' -> ('transit funding', 21)."""
    m = re.match(r"^(.*?)\s+when:(\d+)d\s*$", q.strip())
    return (m.group(1), int(m.group(2))) if m else (q.strip(), 30)


# Rendered as ordinary RSS sources so the existing fetch/classify/dedup pipeline handles them.
SEARCH_SOURCES = [
    (search_source(*_split_search(q))[0], google_news_url(q), pillar, "Search", "RSS", "Med",
     "Keyword search via Google News RSS.")
    for q, pillar in SEARCH_QUERIES
]

# Everything --seed loads and --run fetches.
ALL_SOURCES = SOURCES + SEARCH_SOURCES


def migrate_sources(conn):
    """Columns the Sources tab needs. origin: 'registry' (this file) or 'user' (added in the tab);
    edited_at: changed in the tab, so --seed leaves it alone; deleted_at: a registry source deleted
    in the tab (kept as a marker so --seed doesn't bring it back); last_*: per-source health from
    each run. Idempotent."""
    with conn.cursor() as cur:
        for col in ("enabled BOOLEAN NOT NULL DEFAULT true", "origin TEXT NOT NULL DEFAULT 'registry'",
                    "query TEXT", "search_days INTEGER", "edited_at TIMESTAMPTZ", "deleted_at TIMESTAMPTZ",
                    "last_fetched_at TIMESTAMPTZ", "last_ok_at TIMESTAMPTZ", "last_error TEXT",
                    "last_error_at TIMESTAMPTZ", "fail_streak INTEGER NOT NULL DEFAULT 0",
                    "last_entries INTEGER", "last_new INTEGER", "last_queued INTEGER"):
            cur.execute(f"ALTER TABLE sources ADD COLUMN IF NOT EXISTS {col}")
        cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS sources_name_uniq ON sources (lower(name))")
        # Keyword searches keep their query and window, so the tab can edit them (URL rebuilt from these).
        cur.execute("SELECT id, url FROM sources WHERE type='Search' AND query IS NULL")
        for sid, url in cur.fetchall():
            q = parse_qs_q(url)
            if q:
                query, days = _split_search(q)
                cur.execute("UPDATE sources SET query=%s, search_days=%s WHERE id=%s", (query, days, sid))
    conn.commit()


def seed_sources(conn):
    """Sync the registry into Postgres by source name: add new sources and update existing ones
    (URL, method, notes...), so corrections here reach the database. Sources edited, added or
    deleted in the Command Center's Sources tab are left as they are. Registry keyword searches
    removed from SEARCH_QUERIES are marked 'Retired' so --run stops fetching them. Safe to re-run."""
    migrate_sources(conn)
    added = updated = kept = 0
    with conn.cursor() as cur:
        for name, url, pillar, typ, method, trust, notes in ALL_SOURCES:
            query, days = _split_search(parse_qs_q(url)) if typ == "Search" else (None, None)
            cur.execute("SELECT id, origin, edited_at IS NOT NULL OR deleted_at IS NOT NULL FROM sources "
                        "WHERE lower(name)=lower(%s)", (name,))
            row = cur.fetchone()
            if row and (row[1] != "registry" or row[2]):
                kept += 1  # changed in the Sources tab: the tab's version wins
                continue
            if row:
                cur.execute("UPDATE sources SET url=%s, pillar=%s, type=%s, method=%s, trust=%s, notes=%s, "
                            "query=%s, search_days=%s WHERE id=%s",
                            (url, pillar, typ, method, trust, notes, query, days, row[0]))
                updated += 1
            else:
                cur.execute("INSERT INTO sources (name, url, pillar, type, method, trust, notes, query, search_days) "
                            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                            (name, url, pillar, typ, method, trust, notes, query, days))
                added += 1
        cur.execute("UPDATE sources SET method='Retired', notes='No longer in SEARCH_QUERIES.' "
                    "WHERE type='Search' AND origin='registry' AND edited_at IS NULL AND deleted_at IS NULL "
                    "AND method <> 'Retired' AND NOT (lower(name) = ANY(%s))",
                    ([s[0].lower() for s in SEARCH_SOURCES],))
        retired = cur.rowcount
    conn.commit()
    return added, updated, retired, kept


def parse_qs_q(url):
    from urllib.parse import parse_qs, urlparse
    return (parse_qs(urlparse(url).query).get("q") or [""])[0]


def record_health(conn, source_id, error=None, entries=None, new=None, queued=None):
    """Per-source health after a fetch: success resets the failure streak; a failure keeps the last
    good figures and counts consecutive failures."""
    with conn.cursor() as cur:
        if error:
            cur.execute("UPDATE sources SET last_fetched_at=now(), last_error=%s, last_error_at=now(), "
                        "fail_streak=fail_streak+1 WHERE id=%s", (error[:300], source_id))
        else:
            cur.execute("UPDATE sources SET last_fetched_at=now(), last_ok_at=now(), last_error=NULL, fail_streak=0, "
                        "last_entries=%s, last_new=coalesce(%s, last_new), last_queued=coalesce(%s, last_queued) "
                        "WHERE id=%s", (entries, new, queued, source_id))
    conn.commit()


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
                '"agencies":["transit agencies/authorities named (organizations only, not cities or regions), by their common public name"],"state":"2-letter US state code or empty",'
                '"mode":["any of: Bus, BRT, Light Rail, Heavy Rail, Commuter Rail, Streetcar, Ferry, Multimodal"],'
                '"programs":["any of: CIG New Starts, CIG Small Starts, CIG Core Capacity, TIFIA, RRIF, RAISE, INFRA, Formula, Ballot Measure, P3"],'
                '"tags":["short free-form topic, project, or firm tags"]}\n'
                "Leave any array empty when nothing applies. "
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
    state = state if state in US_STATES else None
    return {"agencies": normalize_agencies(as_list(c.get("agencies")), state), "mode": pick(c.get("mode"), MODES),
            "programs": pick(c.get("programs"), PROGRAMS), "tags": as_list(c.get("tags"), size=60),
            "state": state}


# ---- Agency names -------------------------------------------------------------------------------
# One standard name per agency: the common public name used in the industry (MBTA, BART, NJ Transit...).
# Aliases are matched case- and punctuation-insensitively. Add a line when a new variant shows up
# (see the agency list in the Collection tab's filters), then run `collect --normalize`.
AGENCY_ALIASES = {
    "MTA (New York)": ["MTA", "Metropolitan Transportation Authority", "New York MTA", "MTA New York",
                       "New York Metropolitan Transportation Authority", "NY MTA"],
    "MTA New York City Transit": ["New York City Transit", "NYC Transit", "NYCT", "NYC Transit Authority",
                                  "New York City Transit Authority"],
    "MTA Bus Company": ["MTA Bus"],
    "MTA Long Island Rail Road": ["Long Island Rail Road", "LIRR"],
    "MTA Metro-North Railroad": ["Metro-North", "Metro-North Railroad", "Metro North"],
    "Maryland Transit Administration": ["Maryland MTA", "MTA Maryland"],
    "PATH": ["Port Authority Trans-Hudson", "Port Authority Trans-Hudson Corporation"],
    "NJ Transit": ["New Jersey Transit", "New Jersey Transit Corporation", "NJT"],
    "MBTA": ["Massachusetts Bay Transportation Authority", "the T"],
    "MassDOT": ["Massachusetts Department of Transportation"],
    "SEPTA": ["Southeastern Pennsylvania Transportation Authority"],
    "WMATA": ["Washington Metropolitan Area Transit Authority", "Washington Metropolitan Transit Authority",
              "DC Metro", "Metro (Washington)"],
    "CTA": ["Chicago Transit Authority"],
    "Metra": ["Northeast Illinois Regional Commuter Railroad Corporation"],
    "Pace": ["Pace Suburban Bus"],
    "LA Metro": ["Los Angeles Metro", "Los Angeles County Metropolitan Transportation Authority", "LA County Metro",
                 "Metro Los Angeles", "LACMTA"],
    "BART": ["Bay Area Rapid Transit", "San Francisco Bay Area Rapid Transit District", "Bay Area Rapid Transit District"],
    "SFMTA (Muni)": ["SFMTA", "Muni", "San Francisco Municipal Transportation Agency", "San Francisco Muni"],
    "AC Transit": ["Alameda-Contra Costa Transit District"],
    "VTA": ["Santa Clara Valley Transportation Authority"],
    "SamTrans": ["San Mateo County Transit District"],
    "San Diego MTS": ["San Diego Metropolitan Transit System", "MTS"],
    "Big Blue Bus": ["Santa Monica Big Blue Bus"],
    "Sound Transit": ["Central Puget Sound Regional Transit Authority"],
    "King County Metro": ["King County Metro Transit"],
    "TriMet": ["Tri-County Metropolitan Transportation District of Oregon"],
    "DART": ["Dallas Area Rapid Transit"],
    "Houston METRO": ["Houston Metro", "Metropolitan Transit Authority of Harris County", "METRO Houston",
                      "Houston Transit Authority"],
    "CapMetro": ["Capital Metro", "Capital Metropolitan Transportation Authority"],
    "VIA Metropolitan Transit": ["VIA"],
    "RTD (Denver)": ["RTD", "Denver RTD", "Regional Transportation District"],
    "UTA": ["Utah Transit Authority"],
    "Valley Metro": ["Valley Metro Rail"],
    "MARTA": ["Metropolitan Atlanta Rapid Transit Authority"],
    "Miami-Dade Transit": ["Miami-Dade Department of Transportation and Public Works", "County of Miami-Dade"],
    "Metro Transit (Minneapolis)": ["Metro Transit"],
    "Met Council": ["Metropolitan Council"],
    "Pittsburgh Regional Transit": ["PRT", "Port Authority of Allegheny County"],
    "Greater Cleveland RTA": ["Greater Cleveland Regional Transit Authority", "GCRTA"],
    "Jacksonville Transportation Authority": ["JTA"],
    "Columbus Transit": ["Columbus Transit System"],
    "Canadian National Railway": ["Canadian National", "CN"],
    "FTA": ["Federal Transit Administration"],
}


def _akey(s):
    import re
    s = str(s).lower().replace("&", " and ")
    s = re.sub(r"^the\s+", "", s)
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


_AGENCY_INDEX = {}
for _canon, _aliases in AGENCY_ALIASES.items():
    for _a in [_canon, *_aliases]:
        _AGENCY_INDEX[_akey(_a)] = _canon


def normalize_agencies(names, state=None):
    """Map agency names to their standard name, drop group/region labels ("Bay Area transit agencies"),
    and de-duplicate. "MTA" means Maryland's agency when the item's state is MD, New York's otherwise.
    Unknown names are kept as written (trimmed)."""
    import re
    out = []
    for n in names or []:
        n = str(n).strip()
        k = _akey(n)
        if not k or re.search(r"\b(agencies|operators|region|area transit|systems)$", k) or k in ("dallas fort worth",):
            continue
        if k in ("mta", "maryland mta", "mta maryland") and state == "MD":
            canon = "Maryland Transit Administration"
        elif k == "regional transportation district" and state not in (None, "CO"):
            canon = n  # another region's "RTD"; only Denver's is RTD (Denver)
        else:
            canon = _AGENCY_INDEX.get(k, n)
        if canon not in out:
            out.append(canon)
    return out


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



# ---- Images: a candidate picture for each item, read from the source page's own metadata ----------
# We store the URL only (never a copy of the picture), it is never published without a human choosing
# it in the Publish tab, and a page that blocks us or has no image simply yields nothing.
OG_MAX_BYTES = 400_000
OG_PATTERNS = [
    r'<meta[^>]+property=["\']og:image(?::url)?["\'][^>]+content=["\']([^"\']+)["\']',
    r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image(?::url)?["\']',
    r'<meta[^>]+name=["\']twitter:image(?::src)?["\'][^>]+content=["\']([^"\']+)["\']',
]


def og_image(url, timeout=12):
    """The article's own og:image (or twitter:image), as an absolute https URL, or None. Only the
    first chunk of the page is read, and any failure - block, timeout, no tag - returns None."""
    import requests
    from urllib.parse import urljoin, urlparse
    if not url:
        return None
    try:
        with requests.get(url, timeout=timeout, stream=True, headers={
                "User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"}) as r:
            if r.status_code != 200 or "html" not in (r.headers.get("content-type") or ""):
                return None
            chunk = r.raw.read(OG_MAX_BYTES, decode_content=True) or b""
    except Exception:
        return None
    head = chunk.decode("utf-8", errors="replace")
    for pat in OG_PATTERNS:
        m = re.search(pat, head, re.I)
        if not m:
            continue
        found = urljoin(url, m.group(1).strip())
        u = urlparse(found)
        if u.scheme in ("http", "https") and u.netloc and len(found) <= 500:
            return found
    return None

def item_exists(conn, url):
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM collected_items WHERE source_url=%s LIMIT 1", (url,))
        return cur.fetchone() is not None


def insert_item(conn, pillar, headline, summary, source_name, source_url, published, relevance,
                status="pending", agencies=None, mode=None, programs=None, tags=None, state=None,
                image_url=None):
    """status 'filtered' records an item the model rated low relevance: kept out of the review
    queue, but its link is remembered so later runs never pay to triage it again."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO collected_items (pillar, headline, summary, source_name, source_url, published, relevance, status, "
            "agencies, mode, programs, tags, state, image_url) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s, %s,%s,%s,%s,%s,%s)",
            (pillar, headline, summary, source_name, source_url,
             published.date() if published else None, relevance, status,
             agencies or [], mode or [], programs or [], tags or [], state, image_url))
    conn.commit()


def active_sources(conn):
    """What --run fetches: enabled RSS sources (feeds and keyword searches) not deleted in the Sources
    tab. Read fresh on every run, so changes made in the tab apply to the next run."""
    migrate_sources(conn)
    with conn.cursor() as cur:
        cur.execute("SELECT id, name, url FROM sources WHERE method = 'RSS' AND enabled AND deleted_at IS NULL "
                    "ORDER BY id")
        return cur.fetchall()


def run(conn, limit_sources=None):
    migrate(conn)
    # Only RSS sources are fetched for now; --limit counts those, not skipped API/Scrape ones.
    sources = active_sources(conn)
    if limit_sources:
        sources = sources[:limit_sources]
    added, failed = 0, []
    for sid, name, url in sources:
        try:
            entries = fetch_source(url)
        except FetchError as e:
            print(f"  ! {name}: FAILED - {e}")
            failed.append(name)
            record_health(conn, sid, error=str(e))
            continue
        new = low = errors = kept = 0
        for e in entries:
            if not e["link"] or item_exists(conn, e["link"]):
                continue
            new += 1
            candidate = og_image(e["link"])
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
                        image_url=candidate, **clean_facets(c))
            kept += 1
        added += kept
        record_health(conn, sid, entries=len(entries), new=new, queued=kept)
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
                # Weekly CIG profile check (inbox + one try of FTA's listing page). No model tokens, so it
                # runs whether or not Auto-collect is on.
                if datetime.now(tz).strftime("%a").lower() == os.environ.get("CIG_PROFILES_DAY", "mon").lower()[:3]:
                    try:
                        import cig_profiles
                        out = cig_profiles.weekly(conn, "weekly")
                        print(f"Scheduler: CIG profiles: {out['listing_status']}; {len(out['results'])} files", flush=True)
                    except Exception as e:
                        conn.rollback()
                        print(f"Scheduler: CIG profile check failed: {e}", flush=True)
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
        # Images: a candidate scraped from the source page (never published without review),
        # and on posts the chosen image plus where it came from (candidate|manual|house).
        "ALTER TABLE collected_items ADD COLUMN IF NOT EXISTS image_url TEXT",
        "ALTER TABLE content_posts ADD COLUMN IF NOT EXISTS image_url TEXT",
        "ALTER TABLE content_posts ADD COLUMN IF NOT EXISTS image_source TEXT",
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
                '{"agencies":["transit agencies/authorities named (organizations only, not cities or regions), by their common public name"],"state":"2-letter US state code or empty",'
                f'"mode":["any of: {", ".join(MODES)}"],"programs":["any of: {", ".join(PROGRAMS)}"],'
                '"tags":["short free-form topic, project, or firm tags"]}\n'
                "Leave any array empty when nothing applies."),
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


def normalize_existing(conn):
    """Re-apply normalize_agencies() to every stored item and post (no model calls). Run after
    editing AGENCY_ALIASES."""
    changed = {}
    with conn.cursor() as cur:
        for table in ("collected_items", "content_posts"):
            cur.execute(f"SELECT id, agencies, state FROM {table} WHERE cardinality(agencies) > 0")
            n = 0
            for rid, agencies, state in cur.fetchall():
                new = normalize_agencies(agencies, state)
                if new != list(agencies):
                    cur.execute(f"UPDATE {table} SET agencies=%s WHERE id=%s", (new, rid))
                    n += 1
            changed[table] = n
        cur.execute("SELECT count(DISTINCT a) FROM collected_items, unnest(agencies) a")
        distinct = cur.fetchone()[0]
    conn.commit()
    print(f"Agencies normalized: {changed['collected_items']} items, {changed['content_posts']} posts updated; "
          f"{distinct} distinct agency names now.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", action="store_true")
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--migrate", action="store_true")
    ap.add_argument("--backfill", action="store_true",
                    help="one-off: add facets to existing items collected before facets existed")
    ap.add_argument("--normalize", action="store_true",
                    help="re-apply agency name normalization to stored items and posts (no model calls)")
    ap.add_argument("--schedule", action="store_true",
                    help="stay running and collect daily at COLLECT_AT, unless Auto-collect is off")
    ap.add_argument("--list-sources", action="store_true",
                    help="show what the next run will fetch (no fetching, no model calls)")
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
            added, updated, retired, kept = seed_sources(conn)
            print(f"Sources: {added} added, {updated} updated, {retired} retired, {kept} left as edited in "
                  f"the Sources tab ({len(ALL_SOURCES)} in the registry).")
        if a.list_sources:
            srcs = active_sources(conn)
            print(f"The next run fetches {len(srcs)} sources:")
            for sid, name, url in srcs:
                print(f"  {sid:4} {name}")
        if a.run:
            # Manual runs always go ahead; the Auto-collect toggle only governs the daily schedule.
            run(conn, a.limit)
        if a.backfill:
            backfill(conn, None if a.run else a.limit)
        if a.normalize:
            normalize_existing(conn)
        if not (a.seed or a.run or a.migrate or a.backfill or a.normalize or a.list_sources):
            print("Nothing to do. Use --migrate, --seed, --run, --backfill, --normalize and/or --schedule.")


if __name__ == "__main__":
    main()
