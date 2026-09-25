"""Agency logos from Wikimedia, with their licence carried along.

Why Wikimedia and nowhere else: it publishes licence terms in a queryable API, and it expects
automated readers. An agency's own website does neither - it is behind bot protection as often as
not, and the logo sits there with no stated terms at all. We do not scrape agency sites.

A LICENCE WARNING THAT MATTERS. Most US transit agency logos are NOT freely licensed. They are
uploaded to English Wikipedia under a non-free "fair use" rationale, which covers Wikipedia's own
use of them and does not extend to ours. A few are {{PD-textlogo}} - plain enough wordmarks to sit
below the threshold of originality - and those are genuinely free.

So this fetcher does not judge. It records the licence verbatim as Wikimedia states it, and flags
every asset as free / non-free / unclear so the reviewer sees the problem before approving
anything. Trademark law is a separate question again, and also the reviewer's. That is the entire
reason nothing here auto-publishes.

  python logos.py --fetch            # all known agencies
  python logos.py --fetch --limit 5  # a few, for a dry run
  python logos.py --coverage
"""

import json
import os
import sys
import time
import urllib.parse
import urllib.request

import re

import image_library as L

# Titles that sound like an agency rather than a dictionary word.
TRANSITY = re.compile(r"transit|transport|railway|rail|metro|bus|authority|agency|commuter|"
                      r"subway|tramway|council|district", re.I)

# Wikimedia asks automated readers to identify themselves and give a contact. This is that.
UA = os.environ.get("COLLECT_USER_AGENT", "Transit411/1.0 (+https://transit411.net)")
WP_API = "https://en.wikipedia.org/w/api.php"
COMMONS_API = "https://commons.wikimedia.org/w/api.php"
PAUSE = 1.0             # seconds between calls. 0.35 across 65 agencies earned an HTTP 429.
MATCH_AT = 0.6          # below this the article is probably a different organisation

# Licence strings that mean we may actually use the file. Anything else needs a human to look.
FREE_HINTS = ("public domain", "pd-", "cc0", "cc by", "cc-by", "attribution", "gfdl")
NONFREE_HINTS = ("non-free", "nonfree", "fair use", "fairuse", "copyright", "trademark")


class RateLimited(RuntimeError):
    """Wikimedia asked us to slow down. NOT the same as an agency having no article."""


_last_call = [0.0]


def _get(api, params, tries=4):
    """One Wikimedia call, paced and backed off.

    Wikimedia rate-limits anonymous clients, and a burst across 65 agencies earns a 429 within
    seconds. Requests are spaced, 429/503 is honoured with its Retry-After, and a rate limit is
    raised as RateLimited rather than folded in with "no article found" - reporting throttling as
    a coverage miss would make the coverage numbers a fiction.
    """
    import requests
    for attempt in range(tries):
        wait = PAUSE - (time.time() - _last_call[0])
        if wait > 0:
            time.sleep(wait)
        _last_call[0] = time.time()
        r = requests.get(api, params=dict(params, format="json", formatversion="2"),
                         headers={"User-Agent": UA, "Accept": "application/json"}, timeout=25)
        if r.status_code in (429, 503):
            retry_after = r.headers.get("Retry-After")
            back = float(retry_after) if (retry_after or "").isdigit() else (2.0 * (2 ** attempt))
            if attempt == tries - 1:
                raise RateLimited("HTTP %s after %d tries" % (r.status_code, tries))
            print("     (%s - waiting %.0fs)" % (r.status_code, back), flush=True)
            time.sleep(min(back, 60))
            continue
        r.raise_for_status()
        return r.json()
    raise RateLimited("gave up after %d tries" % tries)


def find_article(names):
    """The Wikipedia article that is actually about this agency, or None.

    Searched by each known name in turn and matched back against ALL of them, so an alias finds
    the article and the full name confirms it.
    """
    # Longest name first. Searching an acronym first found the Wikipedia page "CTA" for the
    # Chicago Transit Authority and, for BART, the article on Bart Simpson - both scoring a
    # perfect 1.00, because a bare acronym matches itself exactly.
    ordered = sorted({n for n in names if n}, key=len, reverse=True)
    for name in ordered[:3]:
        try:
            d = _get(WP_API, {"action": "query", "list": "search", "srsearch": name, "srlimit": 5})
        except RateLimited:
            raise
        except Exception:
            continue
        scored = []
        for hit in (d.get("query", {}).get("search") or []):
            title = hit.get("title") or ""
            # "List of ... yards", "... Police", "... (disambiguation)" are about the agency
            # without being the agency.
            if title.lower().startswith(("list of", "history of", "timeline of")):
                continue
            if "(disambiguation)" in title.lower():
                continue
            # "PATH (rail system)" is the PATH article; the parenthetical is Wikipedia's
            # disambiguator, not part of the name. Comparing with it attached scored the real
            # article at 0.50 and let the generic article "Path" win on an exact acronym match.
            bare = re.sub(r"\s*\([^)]*\)\s*$", "", title).strip()
            scored.append((max(L.best_score(names, title), L.best_score(names, bare)), title))
        if not scored:
            continue
        top = max(sc for sc, _ in scored)
        if top < MATCH_AT:
            continue
        tied = [t for sc, t in scored if sc == top]
        # Among equal matches prefer one that sounds like transit - "PATH (rail system)" over
        # "Path", "Chicago Transit Authority" over "CTA" - then the plainest title, so
        # "...Transportation Authority" beats "...Transportation Authority Police".
        transit = [t for t in tied if TRANSITY.search(t)]
        for cand in sorted(transit or tied, key=len):
            # "Metropolitan Council", "Madison Metro" and "Ride On" are DISAMBIGUATION pages, not
            # agency articles - they carry no logo and matched perfectly. Filtering on the
            # "(disambiguation)" suffix missed them, because they do not have one.
            if is_disambiguation(cand):
                continue
            return {"title": cand, "score": round(top, 2), "found_by": name}
    return None


# Transit articles are full of route markers - BSicon_*, "Line 1 ... icon", "SEPTA B1 icon". They
# match every naive "is this a logo?" test and are never the agency's logo.
ROUTE_ICON = ("bsicon", "_icon", "icon-", "icon_", "line_", "route_", "_line", "diagram",
              "map", "pictogram", "arrow")
# Wikipedia's own furniture. "Wiktionary-logo-en-v2.svg" contains "logo" and was picked as the
# Chicago Transit Authority's logo until this list grew.
CHROME = ("commons-logo", "wikimedia", "wiktionary", "wikisource", "wikiquote", "wikibooks",
          "wikinews", "wikiversity", "wikidata", "wikispecies", "wikivoyage", "wikipedia-logo",
          "edit-", "ambox", "question_book", "symbol_", "folder_", "padlock", "wiki_letter",
          "red_pencil", "increase", "decrease", "flag_of", "star_", "portal")


def _usable(filename):
    f = (filename or "").lower()
    return not any(w in f for w in CHROME) and not any(w in f for w in ROUTE_ICON)


def is_disambiguation(title):
    """A disambiguation page lists other articles. It is never the agency."""
    try:
        d = _get(WP_API, {"action": "query", "titles": title, "prop": "pageprops"})
    except Exception:
        return False
    pages = d.get("query", {}).get("pages") or []
    if not pages:
        return False
    return "disambiguation" in (pages[0].get("pageprops") or {})


def _looks_like_logo(filename):
    f = (filename or "").lower()
    if not _usable(f):
        return -1
    if any(w in f for w in ("logo", "wordmark", "roundel", "emblem", "seal")):
        return 2
    return 1 if f.endswith(".svg") else 0


INFOBOX_LOGO = None      # compiled on first use, below


def infobox_logo(title):
    """The infobox's own logo= field, read from the article wikitext.

    This is the only reliable source. pageimages returns the article's LEAD image, which on most
    transit articles is a photograph of a bus or a station - so asking for it produced
    "LA Metro ElDorado Axess (cropped).jpg" as LA Metro's logo. The logo lives in a separate
    infobox parameter, and this reads exactly that.
    """
    global INFOBOX_LOGO
    if INFOBOX_LOGO is None:
        import re
        INFOBOX_LOGO = re.compile(
            r"\|\s*(logo|logo_image|image_logo|agency_logo|image)\s*=\s*"
            r"(?:\[\[)?(?:File:|Image:)?\s*([^" + chr(10) + r"|}\]]+?)\s*(?:\||\]\]|"
            + chr(10) + r"|\})", re.I)
    try:
        d = _get(WP_API, {"action": "parse", "page": title, "prop": "wikitext"})
    except Exception:
        return None
    text = ((d.get("parse") or {}).get("wikitext") or "")
    if not isinstance(text, str):
        text = text.get("*", "") if isinstance(text, dict) else ""
    for m in INFOBOX_LOGO.finditer(text):
        field = (m.group(1) or "").lower()
        name = (m.group(2) or "").split("{{")[0].split("|")[0].strip()
        low = name.lower()
        if not _usable(name):
            continue
        if field.startswith("logo") or field.endswith("logo"):
            if low.endswith((".svg", ".png", ".jpg", ".jpeg", ".gif")):
                return "File:" + name
        elif low.endswith(".svg"):
            # A plain `image =` field, but a VECTOR: MBTA's infobox says "image = MBTA.svg" and
            # that is the logo. A photograph in the same field would be .jpg, and is not.
            return "File:" + name
    return None


def article_logo_file(title):
    """A file that is plausibly the agency's LOGO, or None.

    Deliberately returns None rather than a photograph. A bus picture stored as an agency logo
    would be worse than the house graphic it is meant to replace, and would make the coverage
    numbers a lie.
    """
    got = infobox_logo(title)
    if got:
        return got
    try:
        d = _get(WP_API, {"action": "query", "titles": title, "prop": "images", "imlimit": "80"})
    except Exception:
        return None
    pages = d.get("query", {}).get("pages") or []
    if not pages:
        return None
    files = [i.get("title") for i in (pages[0].get("images") or []) if i.get("title")]
    named = [f for f in files if _usable(f) and _looks_like_logo(f) >= 2]
    return min(named, key=len) if named else None


def file_details(file_title):
    """Size, direct URL, licence and attribution for a File:, from Commons or Wikipedia.

    Files live on Commons when freely licensed and on Wikipedia itself when non-free, so both are
    asked - and which one answered is itself a signal about the licence.
    """
    for api, where in ((COMMONS_API, "commons"), (WP_API, "wikipedia")):
        try:
            d = _get(api, {"action": "query", "titles": file_title, "prop": "imageinfo",
                           "iiprop": "url|size|extmetadata"})
        except Exception:
            continue
        pages = d.get("query", {}).get("pages") or []
        if not pages or pages[0].get("missing"):
            continue
        info = (pages[0].get("imageinfo") or [{}])[0]
        if not info.get("url"):
            continue
        meta = info.get("extmetadata") or {}

        def m(key):
            v = (meta.get(key) or {}).get("value")
            return _strip_html(v) if v else ""

        licence = m("LicenseShortName") or m("UsageTerms") or m("License")
        artist = m("Artist") or m("Credit") or m("Attribution")
        return {
            "file": file_title,
            "url": info.get("url"),
            "descriptionurl": info.get("descriptionurl") or "",
            "width": info.get("width"), "height": info.get("height"),
            "license": licence,
            "license_url": m("LicenseUrl"),
            "artist": artist,
            "hosted_on": where,
        }
    return None


def _strip_html(s):
    import re
    s = re.sub(r"(?is)<[^>]+>", " ", str(s or ""))
    for a, b in (("&amp;", "&"), ("&nbsp;", " "), ("&quot;", '"'), ("&#039;", "'"), ("&lt;", "<"), ("&gt;", ">")):
        s = s.replace(a, b)
    return " ".join(s.split())[:300]


def licence_class(details):
    """free | non-free | unclear. The reviewer sees this before they see an Approve button."""
    text = " ".join([details.get("license") or "", details.get("artist") or ""]).lower()
    host = details.get("hosted_on")
    if any(h in text for h in NONFREE_HINTS) and not any(h in text for h in ("cc by", "cc-by", "public domain")):
        return "non-free"
    if any(h in text for h in FREE_HINTS):
        return "free"
    # A file on Commons is free by Commons policy even when we cannot parse the tag; one that
    # exists only on Wikipedia usually is not.
    return "free" if host == "commons" else "unclear"


def asset_for(agency_name, names):
    """Find one logo candidate for an agency. Returns the asset dict, or a dict with 'miss'."""
    art = find_article(names)
    if not art:
        return {"miss": "no matching Wikipedia article"}
    fil = article_logo_file(art["title"])
    if not fil:
        return {"miss": "article has no usable image", "article": art["title"]}
    det = file_details(fil)
    if not det:
        return {"miss": "could not read file details", "article": art["title"], "file": fil}

    klass = licence_class(det)
    licence = det["license"] or ("Commons file, licence tag unread" if det["hosted_on"] == "commons"
                                 else "Not stated - treat as all rights reserved")
    attribution = det["artist"] or ("Wikimedia Commons" if det["hosted_on"] == "commons"
                                    else "English Wikipedia")
    note = {
        "free": "Licence looks free. Check it still requires the credit shown.",
        "non-free": "NON-FREE: uploaded under a fair-use rationale that covers Wikipedia, not us. "
                    "Do not approve unless you have another right to use this logo.",
        "unclear": "Licence could not be classified. Read the file page before approving.",
    }[klass]
    return {
        "kind": "logo",
        "agency": agency_name,
        "topic_tags": [],
        "url": det["url"],
        "width": det.get("width"), "height": det.get("height"),
        "source": "wikimedia",
        "source_url": det.get("descriptionurl") or ("https://commons.wikimedia.org/wiki/" + urllib.parse.quote(fil)),
        "license": licence,
        "attribution": attribution,
        "notes": "%s | matched %s (%.2f) via %s | %s" % (
            klass, art["title"], art["score"], art["found_by"], note),
        "_class": klass,
    }


def all_agencies(conn):
    """Every agency we might want a logo for: the reference table plus CIG sponsors."""
    import agencies as A
    out = {}
    for a in A.all_agencies(conn):
        names = [a.get("name")] + list(a.get("aliases") or [])
        if a.get("cig_sponsor"):
            names.append(a["cig_sponsor"])
        out[a["name"]] = [n for n in names if n]
    with conn.cursor() as cur:
        cur.execute("SELECT DISTINCT sponsor FROM cig_projects WHERE sponsor IS NOT NULL AND sponsor <> ''")
        for (sp,) in cur.fetchall():
            if not any(L.name_score(sp, n) >= 0.9 for names in out.values() for n in names):
                out.setdefault(sp, [sp])
    return out


def fetch(conn, limit=None, only=None):
    """Populate logo candidates. Returns a report; writes nothing but candidates."""
    with conn.cursor() as cur:
        L.schema(cur)
    conn.commit()
    targets = all_agencies(conn)
    if only:
        targets = {k: v for k, v in targets.items() if k.lower() == only.lower()}
    names = sorted(targets)
    if limit:
        names = names[:limit]

    report = {"checked": 0, "added": 0, "updated": 0, "misses": [], "by_class": {}}
    for name in names:
        report["checked"] += 1
        try:
            asset = asset_for(name, targets[name])
        except RateLimited as e:
            # Stop rather than mark the rest of the list as "no logo found".
            report["stopped_early"] = "rate limited at %s (%s)" % (name, e)
            print("  !! rate limited at %s - stopping so the rest are not recorded as misses" % name,
                  flush=True)
            break
        except Exception as e:
            report["misses"].append({"agency": name, "why": "%s: %s" % (type(e).__name__, e)})
            continue
        if asset.get("miss"):
            report["misses"].append({"agency": name, "why": asset["miss"]})
            print("  -- %-46s %s" % (name[:46], asset["miss"]), flush=True)
            continue
        klass = asset.pop("_class")
        report["by_class"][klass] = report["by_class"].get(klass, 0) + 1
        with conn.cursor() as cur:
            _id, what = L.add_candidate(cur, asset)
        conn.commit()
        report[what] += 1
        print("  %-8s %-46s %-9s %s" % (what, name[:46], klass, (asset["license"] or "")[:40]), flush=True)
    return report


def main():
    import argparse
    import psycopg
    ap = argparse.ArgumentParser(description="Agency logo candidates from Wikimedia.")
    ap.add_argument("--fetch", action="store_true")
    ap.add_argument("--coverage", action="store_true")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--agency")
    a = ap.parse_args()
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        raise SystemExit("DATABASE_URL is not set")
    with psycopg.connect(dsn) as conn:
        if a.fetch:
            rep = fetch(conn, limit=a.limit, only=a.agency)
            print("\nchecked %d | added %d | updated %d | no candidate %d"
                  % (rep["checked"], rep["added"], rep["updated"], len(rep["misses"])))
            print("by licence class:", rep["by_class"])
        if a.coverage or not a.fetch:
            with conn.cursor() as cur:
                L.schema(cur)
                conn.commit()
                cov = L.coverage(cur)
            print("agencies %d | approved logo %d | awaiting review %d | nothing %d"
                  % (cov["total"], cov["with_approved"], cov["awaiting_review"], cov["none"]))


if __name__ == "__main__":
    main()
