"""Topic stock photography from Unsplash and Pexels.

For the stories that are not about one agency - a funding fight, a policy change, a procurement
round - where a logo makes no sense and the house graphic is the only thing left.

Both services are free to use and both REQUIRE a credit. That credit is stored with the asset, on
every row, and shown in the review screen; a stock photo whose attribution we have lost is a
licence breach waiting to happen, so it cannot be stored at all.

A MISSING KEY IS NOT AN ERROR. If UNSPLASH_KEY or PEXELS_KEY is absent the integration is built
and simply skipped, loudly, in the report. Neither key blocks a run, and neither blocks the other.

  python stock.py --fetch                 # every topic in the vocabulary
  python stock.py --fetch --topic bus     # one topic
"""

import os
import time

import image_library as L

UA = os.environ.get("COLLECT_USER_AGENT", "Transit411/1.0 (+https://transit411.net)")
PER_QUERY = 4            # a handful per search term; a human has to look at every one
PAUSE = 0.4

UNSPLASH_LICENSE = "Unsplash License (free to use, credit appreciated and stored here)"
PEXELS_LICENSE = "Pexels License (free to use, credit required by our own policy)"


def _key(name):
    return (os.environ.get(name) or "").strip()


def unsplash(query, per_page=PER_QUERY):
    """-> list of assets, or raises RuntimeError with a readable reason."""
    key = _key("UNSPLASH_KEY")
    if not key:
        raise RuntimeError("UNSPLASH_KEY is not set")
    import requests
    r = requests.get("https://api.unsplash.com/search/photos",
                     params={"query": query, "per_page": per_page, "orientation": "landscape"},
                     headers={"Authorization": "Client-ID " + key, "User-Agent": UA,
                              "Accept-Version": "v1"}, timeout=25)
    r.raise_for_status()
    out = []
    for p in (r.json().get("results") or []):
        user = p.get("user") or {}
        who = user.get("name") or user.get("username") or "Unknown photographer"
        out.append({
            "source": "unsplash",
            "source_url": (p.get("links") or {}).get("html") or ("https://unsplash.com/photos/" + str(p.get("id"))),
            "url": (p.get("urls") or {}).get("regular"),
            "width": p.get("width"), "height": p.get("height"),
            "license": UNSPLASH_LICENSE,
            "attribution": "Photo by %s on Unsplash" % who,
            "notes": (p.get("alt_description") or "")[:200],
        })
    return out


def pexels(query, per_page=PER_QUERY):
    key = _key("PEXELS_KEY")
    if not key:
        raise RuntimeError("PEXELS_KEY is not set")
    import requests
    r = requests.get("https://api.pexels.com/v1/search",
                     params={"query": query, "per_page": per_page, "orientation": "landscape"},
                     headers={"Authorization": key, "User-Agent": UA}, timeout=25)
    r.raise_for_status()
    out = []
    for p in (r.json().get("photos") or []):
        who = p.get("photographer") or "Unknown photographer"
        out.append({
            "source": "pexels",
            "source_url": p.get("url") or ("https://www.pexels.com/photo/%s/" % p.get("id")),
            "url": (p.get("src") or {}).get("large"),
            "width": p.get("width"), "height": p.get("height"),
            "license": PEXELS_LICENSE,
            "attribution": "Photo by %s on Pexels" % who,
            "notes": (p.get("alt") or "")[:200],
        })
    return out


PROVIDERS = (("unsplash", unsplash), ("pexels", pexels))


def fetch(conn, only_topic=None, per_query=PER_QUERY):
    """Populate stock candidates for every topic in the vocabulary. Returns a report."""
    with conn.cursor() as cur:
        L.schema(cur)
    conn.commit()

    topics = {k: v for k, v in L.TOPICS.items() if not only_topic or k == only_topic}
    report = {"added": 0, "updated": 0, "skipped_providers": [], "errors": [], "by_topic": {}}

    available = []
    for name, fn in PROVIDERS:
        try:
            fn("transit", per_page=1)
            available.append((name, fn))
        except RuntimeError as e:
            # A missing key is expected and must not stop the run, or stop the other provider.
            report["skipped_providers"].append({"provider": name, "why": str(e)})
            print("  skipping %s: %s" % (name, e), flush=True)
        except Exception as e:
            report["skipped_providers"].append({"provider": name, "why": "%s: %s" % (type(e).__name__, e)})
            print("  skipping %s: %s: %s" % (name, type(e).__name__, e), flush=True)
    if not available:
        print("  no stock provider available - set UNSPLASH_KEY and/or PEXELS_KEY in .env", flush=True)
        return report

    for tag, queries in topics.items():
        got = 0
        for q in queries:
            for name, fn in available:
                try:
                    assets = fn(q, per_page=per_query)
                except Exception as e:
                    report["errors"].append({"topic": tag, "query": q, "provider": name,
                                             "why": "%s: %s" % (type(e).__name__, e)})
                    continue
                time.sleep(PAUSE)
                for a in assets:
                    if not a.get("url"):
                        continue
                    a.update(kind="stock", agency=None, topic_tags=[tag])
                    try:
                        with conn.cursor() as cur:
                            _id, what = L.add_candidate(cur, a)
                        conn.commit()
                        report[what] += 1
                        got += 1
                    except L.MissingProvenance as e:
                        # Should be unreachable - both providers give us everything - but if a
                        # response ever lacks a credit, we drop the photo rather than the credit.
                        report["errors"].append({"topic": tag, "query": q, "provider": name,
                                                 "why": str(e)})
        report["by_topic"][tag] = got
        print("  %-14s %d candidate(s)" % (tag, got), flush=True)
    return report


def main():
    import argparse
    import psycopg
    ap = argparse.ArgumentParser(description="Topic stock candidates from Unsplash and Pexels.")
    ap.add_argument("--fetch", action="store_true")
    ap.add_argument("--topic")
    ap.add_argument("--per-query", type=int, default=PER_QUERY)
    a = ap.parse_args()
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        raise SystemExit("DATABASE_URL is not set")
    with psycopg.connect(dsn) as conn:
        if a.fetch:
            rep = fetch(conn, only_topic=a.topic, per_query=a.per_query)
            print("\nadded %d | updated %d" % (rep["added"], rep["updated"]))
            if rep["skipped_providers"]:
                print("skipped:", rep["skipped_providers"])
        else:
            with conn.cursor() as cur:
                L.schema(cur)
                conn.commit()
                print(L.stats(cur))


if __name__ == "__main__":
    main()
