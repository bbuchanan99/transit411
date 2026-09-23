#!/usr/bin/env python3
"""
Picture candidates for a story - what the source page offers, so a human can choose before publishing.

We only ever collect URLs and read the page's own markup; nothing is copied or re-hosted. The
collector stores the publisher's own social image (og:image) when an item is queued; this module is
what the Publish tab calls when you want to see everything the article has, with enough detail
(kind, size, type) to judge which one is worth using.
"""
import os
import re
from urllib.parse import urljoin, urlparse

USER_AGENT = os.environ.get("COLLECT_USER_AGENT", "Transit411/1.0 (+https://transit411.net)")
MAX_HTML = 900_000          # enough for the article markup without pulling whole pages
MAX_CANDIDATES = 14
MIN_PIXELS = 200            # anything smaller than this on a side is an icon, not a picture

# Things that are never the story's picture.
JUNK = re.compile(r"(sprite|icon|favicon|logo|avatar|placeholder|spacer|pixel|tracking|1x1|blank|"
                  r"advert|doubleclick|googletag|gravatar|badge|button|share|social)", re.I)
META_PATTERNS = [
    ("og:image", r'<meta[^>]+property=["\']og:image(?::url)?["\'][^>]+content=["\']([^"\']+)["\']'),
    ("og:image", r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image(?::url)?["\']'),
    ("twitter:image", r'<meta[^>]+name=["\']twitter:image(?::src)?["\'][^>]+content=["\']([^"\']+)["\']'),
]


def _abs(base, u):
    try:
        found = urljoin(base, (u or "").strip())
        p = urlparse(found)
        return found if p.scheme in ("http", "https") and p.netloc and len(found) <= 600 else None
    except ValueError:
        return None


def _attr(tag, name):
    m = re.search(name + r'=["\']([^"\']*)["\']', tag, re.I)
    return m.group(1).strip() if m else ""


def _best_from_srcset(srcset):
    """The largest entry in a srcset (they are usually 'url 320w, url 640w, ...')."""
    best, best_w = None, -1
    for part in (srcset or "").split(","):
        bits = part.strip().split()
        if not bits:
            continue
        w = 0
        if len(bits) > 1 and bits[1].endswith("w"):
            try:
                w = int(bits[1][:-1])
            except ValueError:
                w = 0
        if w >= best_w:
            best, best_w = bits[0], w
    return best


def fetch_html(url, timeout=15):
    import requests
    with requests.get(url, timeout=timeout, stream=True, headers={
            "User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"}) as r:
        if r.status_code != 200 or "html" not in (r.headers.get("content-type") or ""):
            raise ValueError("that page returned HTTP %s" % r.status_code)
        return (r.raw.read(MAX_HTML, decode_content=True) or b"").decode("utf-8", errors="replace")


def candidates(url, html=None, probe=True, limit=MAX_CANDIDATES):
    """Every picture the article offers, best first: the publisher's own social image, then the
    pictures in the page itself. Each entry says where it came from and, when we can tell, how big
    it is - so an icon or a tracking pixel is obvious rather than a surprise after publishing."""
    html = html if html is not None else fetch_html(url)
    seen, out = set(), []

    def add(src, kind, alt="", w=0, h=0):
        u = _abs(url, src)
        if not u or u in seen or u.startswith("data:"):
            return
        if JUNK.search(u) and kind == "article":
            return
        if (w and w < MIN_PIXELS) or (h and h < MIN_PIXELS):
            return
        seen.add(u)
        out.append({"url": u, "kind": kind, "alt": (alt or "")[:200], "width": w or None, "height": h or None})

    for kind, pat in META_PATTERNS:
        m = re.search(pat, html, re.I)
        if m:
            add(m.group(1), kind)
    for tag in re.findall(r"<img\b[^>]*>", html, re.I)[:200]:
        src = _attr(tag, "src") or _attr(tag, "data-src") or _attr(tag, "data-lazy-src")
        srcset = _attr(tag, "srcset") or _attr(tag, "data-srcset")
        if srcset:
            src = _best_from_srcset(srcset) or src
        def num(name):
            v = _attr(tag, name)
            return int(v) if v.isdigit() else 0
        add(src, "article", _attr(tag, "alt"), num("width"), num("height"))
        if len(out) >= limit:
            break
    out = out[:limit]
    if probe:
        for c in out[:10]:
            c.update(probe_image(c["url"]))
    return out


def probe_image(url, timeout=8):
    """A HEAD request for type and size, so the picker can show what a picture actually is. Anything
    that fails just comes back unknown - it never blocks the choice."""
    import requests
    try:
        r = requests.head(url, timeout=timeout, allow_redirects=True, headers={"User-Agent": USER_AGENT})
        if r.status_code >= 400:
            r = requests.get(url, timeout=timeout, stream=True, headers={"User-Agent": USER_AGENT})
            r.close()
        ctype = (r.headers.get("content-type") or "").split(";")[0]
        length = r.headers.get("content-length")
        return {"ok": r.status_code < 400 and ctype.startswith("image/"),
                "content_type": ctype or None, "bytes": int(length) if (length or "").isdigit() else None}
    except Exception:
        return {"ok": None, "content_type": None, "bytes": None}


HOUSE_PILLARS = ["funding", "procurement", "people", "policy", "data", "news"]


def house_images(site):
    """Our own pillar graphics - always safe to publish, and the fallback when nothing else fits."""
    site = (site or "").rstrip("/")
    return [{"url": site + "/images/house/" + p + ".png", "kind": "house", "alt": p.title() + " - Transit411",
             "width": 1200, "height": 630, "ok": True, "content_type": "image/png", "bytes": None}
            for p in HOUSE_PILLARS]
