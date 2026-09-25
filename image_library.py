"""The image and logo library: where a picture comes from, and whether we may use it.

Articles fall back to a generic house graphic when nobody chose a picture. This holds the two
things that would usually be better - the agency's own logo for an agency story, and a relevant
topic photo otherwise - so that fallback has something real to reach for.

PROVENANCE IS THE POINT, not a field we happen to store. Every row must name its source, the exact
page it came from, its licence and the credit that licence requires. That is enforced by a CHECK
constraint, not by the callers remembering: an asset with no provenance cannot physically be
inserted. If we cannot say where a picture came from and on what terms, we do not keep it.

NOTHING HERE IS PUBLISHED BY ITSELF. Everything lands as `candidate`. A logo is a trademark and a
stock photo is a licence agreement; both are judgement calls that stay with a human, in the
Command Center's review screen.

We never scrape an agency's own website - bot-walls and unclear licensing. Logos come from
Wikimedia Commons, which publishes its licence terms in a queryable API.
"""

import os
import re

KINDS = ("logo", "stock")
SOURCES = ("wikimedia", "unsplash", "pexels", "manual")
STATUSES = ("candidate", "approved", "rejected")

# Provenance fields. Every one is required, for every asset, from every source.
PROVENANCE = ("source", "source_url", "license", "attribution")


class MissingProvenance(ValueError):
    """Raised before we ever reach the database, so the caller gets a useful message."""


def schema(cur):
    """Idempotent. The CHECK constraint is the real guarantee here."""
    cur.execute("""CREATE TABLE IF NOT EXISTS image_library (
        id           BIGSERIAL PRIMARY KEY,
        kind         TEXT NOT NULL,
        agency       TEXT,
        topic_tags   TEXT[] NOT NULL DEFAULT '{}',
        file_path    TEXT,
        url          TEXT,
        width        INT,
        height       INT,
        source       TEXT NOT NULL,
        source_url   TEXT NOT NULL,
        license      TEXT NOT NULL,
        attribution  TEXT NOT NULL,
        fetched_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
        status       TEXT NOT NULL DEFAULT 'candidate',
        notes        TEXT,
        reviewed_at  TIMESTAMPTZ,
        created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT image_library_kind_ck CHECK (kind IN ('logo','stock')),
        CONSTRAINT image_library_status_ck CHECK (status IN ('candidate','approved','rejected')),
        -- No asset enters the library without full provenance. Enforced here so it cannot be
        -- forgotten by a future caller, a migration or a hand-written INSERT.
        CONSTRAINT image_library_provenance_ck CHECK (
            btrim(coalesce(source, '')) <> '' AND
            btrim(coalesce(source_url, '')) <> '' AND
            btrim(coalesce(license, '')) <> '' AND
            btrim(coalesce(attribution, '')) <> ''),
        CONSTRAINT image_library_has_image_ck CHECK (
            btrim(coalesce(url, '')) <> '' OR btrim(coalesce(file_path, '')) <> ''))""")
    # One row per asset per agency: re-running a fetch updates rather than duplicates.
    cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS image_library_asset_uniq "
                "ON image_library (kind, lower(coalesce(agency, '')), source_url)")
    cur.execute("CREATE INDEX IF NOT EXISTS image_library_status_idx ON image_library (status, kind)")
    cur.execute("CREATE INDEX IF NOT EXISTS image_library_agency_idx ON image_library (lower(coalesce(agency, '')))")
    cur.execute("CREATE INDEX IF NOT EXISTS image_library_tags_idx ON image_library USING GIN (topic_tags)")
    # Which approved logo an agency uses. Set only when a human approves one.
    cur.execute("ALTER TABLE agencies ADD COLUMN IF NOT EXISTS logo_image_id BIGINT")
    # ...and it must stop pointing at an image that no longer exists. Without this, deleting a
    # row left the agency claiming an approved logo that had gone, so coverage counted a logo
    # nothing could load. ON DELETE SET NULL makes the database keep the two in step.
    cur.execute("UPDATE agencies SET logo_image_id = NULL WHERE logo_image_id IS NOT NULL "
                "AND logo_image_id NOT IN (SELECT id FROM image_library)")
    cur.execute("""DO $$ BEGIN
        IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'agencies_logo_image_fk') THEN
            ALTER TABLE agencies ADD CONSTRAINT agencies_logo_image_fk
                FOREIGN KEY (logo_image_id) REFERENCES image_library(id) ON DELETE SET NULL;
        END IF;
    END $$;""")


def check_provenance(asset):
    """Refuse an incomplete asset up front, with a message naming what is missing."""
    missing = [f for f in PROVENANCE if not str(asset.get(f) or "").strip()]
    if missing:
        raise MissingProvenance(
            "refusing to store an image without " + ", ".join(missing)
            + " (" + (asset.get("source_url") or asset.get("url") or "no url") + ")")
    if asset.get("source") not in SOURCES:
        raise MissingProvenance("unknown source %r; expected one of %s"
                                % (asset.get("source"), ", ".join(SOURCES)))
    if asset.get("kind") not in KINDS:
        raise MissingProvenance("unknown kind %r; expected one of %s"
                                % (asset.get("kind"), ", ".join(KINDS)))
    return True


def add_candidate(cur, asset):
    """Store one asset as a candidate. Returns (id, 'added'|'updated').

    An asset we already hold keeps its review decision - re-running a fetch must never quietly
    un-approve or un-reject what someone has already judged.
    """
    check_provenance(asset)
    cur.execute(
        """INSERT INTO image_library
             (kind, agency, topic_tags, file_path, url, width, height,
              source, source_url, license, attribution, notes)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
           ON CONFLICT (kind, lower(coalesce(agency, '')), source_url) DO UPDATE SET
             topic_tags = EXCLUDED.topic_tags,
             url = EXCLUDED.url, file_path = EXCLUDED.file_path,
             width = EXCLUDED.width, height = EXCLUDED.height,
             license = EXCLUDED.license, attribution = EXCLUDED.attribution,
             notes = EXCLUDED.notes
           RETURNING id, (xmax = 0) AS inserted""",
        (asset["kind"], asset.get("agency"), asset.get("topic_tags") or [],
         asset.get("file_path"), asset.get("url"), asset.get("width"), asset.get("height"),
         asset["source"], asset["source_url"], asset["license"], asset["attribution"],
         asset.get("notes")))
    rid, inserted = cur.fetchone()
    return rid, ("added" if inserted else "updated")


def supersede_candidates(cur, agency, keep_id):
    """Retire earlier CANDIDATE logos for an agency when a later fetch finds a different file.

    An improved fetcher finds a better file, but the old guess stays: the unique index is on the
    file URL, so a different file is a new row. That left city and county seals - Seal of Atlanta
    for MARTA, MKESeal for Milwaukee - sitting in the queue next to the real logo, both marked
    free, both one click from being approved.

    Only rows still awaiting review are touched. A human's approve or reject is never undone by a
    re-run; that is the whole contract of this table.
    """
    if not agency or not keep_id:
        return 0
    cur.execute(
        """UPDATE image_library
              SET status='rejected', reviewed_at=now(),
                  notes = coalesce(notes,'') || ' | superseded by a later fetch (id %s)'
            WHERE kind='logo' AND status='candidate'
              AND lower(agency)=lower(%%s) AND id <> %%s""" % int(keep_id),
        (agency, keep_id))
    return cur.rowcount


def set_status(cur, image_id, status, approve_for_agency=True):
    """Approve or reject one asset. Approving a logo points its agency at it."""
    if status not in STATUSES:
        raise ValueError("unknown status " + str(status))
    cur.execute("UPDATE image_library SET status=%s, reviewed_at=now() WHERE id=%s "
                "RETURNING kind, agency", (status, image_id))
    row = cur.fetchone()
    if not row:
        return None
    kind, agency = row
    if kind == "logo" and agency:
        if status == "approved" and approve_for_agency:
            cur.execute("UPDATE agencies SET logo_image_id=%s WHERE lower(name)=lower(%s)",
                        (image_id, agency))
        elif status != "approved":
            # A logo that is no longer approved must stop being the agency's logo.
            cur.execute("UPDATE agencies SET logo_image_id=NULL WHERE logo_image_id=%s", (image_id,))
    return {"id": image_id, "kind": kind, "agency": agency, "status": status}


import os as _os

# An uploaded file has no public URL of its own, so one is derived: the read-only API serves it at
# /api/images/file/<id>, and that is what the static site can actually load.
PUBLIC_API = (_os.environ.get("PUBLIC_API_BASE")
              or _os.environ.get("EMAIL_LINK_BASE")
              or "https://api.transit411.net").rstrip("/")


def public_url(row):
    """The URL a reader's browser can fetch, whether the asset is hosted elsewhere or uploaded."""
    if row.get("url"):
        return row["url"]
    if row.get("file_path") and row.get("id"):
        return "%s/api/images/file/%s" % (PUBLIC_API, row["id"])
    return None


def approved_logo(cur, agency_name):
    """The approved logo for an agency, or None. Only ever returns an approved row."""
    if not agency_name:
        return None
    cur.execute(
        """SELECT i.id, i.url, i.file_path, i.width, i.height, i.license, i.attribution, i.source_url
             FROM image_library i
            WHERE i.kind='logo' AND i.status='approved' AND lower(i.agency)=lower(%s)
            ORDER BY i.reviewed_at DESC NULLS LAST, i.id DESC LIMIT 1""", (agency_name,))
    row = cur.fetchone()
    if not row:
        return None
    out = dict(zip(("id", "url", "file_path", "width", "height", "license", "attribution",
                    "source_url"), row))
    out["url"] = public_url(out)
    return out


def approved_stock(cur, tags, limit=5):
    """Approved stock images matching any of these topic tags, best overlap first."""
    tags = [t for t in (tags or []) if t]
    if not tags:
        return []
    cur.execute(
        """SELECT id, url, file_path, width, height, license, attribution, source_url, topic_tags,
                  cardinality(ARRAY(SELECT unnest(topic_tags) INTERSECT SELECT unnest(%s::text[]))) AS overlap
             FROM image_library
            WHERE kind='stock' AND status='approved'
              AND topic_tags && %s::text[]
            ORDER BY overlap DESC, id DESC LIMIT %s""", (tags, tags, limit))
    cols = ("id", "url", "file_path", "width", "height", "license", "attribution", "source_url",
            "topic_tags", "overlap")
    out = [dict(zip(cols, r)) for r in cur.fetchall()]
    for r in out:
        r["url"] = public_url(r)
    return out


def coverage(cur):
    """Which agencies have an approved logo, a candidate waiting, or nothing at all."""
    cur.execute("""
        SELECT a.name, a.state, a.logo_image_id IS NOT NULL AS approved,
               (SELECT count(*) FROM image_library i
                 WHERE i.kind='logo' AND lower(i.agency)=lower(a.name) AND i.status='candidate') AS candidates,
               (SELECT count(*) FROM image_library i
                 WHERE i.kind='logo' AND lower(i.agency)=lower(a.name) AND i.status='rejected') AS rejected
          FROM agencies a ORDER BY a.name""")
    rows = [dict(zip(("agency", "state", "approved", "candidates", "rejected"), r))
            for r in cur.fetchall()]
    return {
        "agencies": rows,
        "total": len(rows),
        "with_approved": sum(1 for r in rows if r["approved"]),
        "awaiting_review": sum(1 for r in rows if not r["approved"] and r["candidates"]),
        "none": sum(1 for r in rows if not r["approved"] and not r["candidates"]),
    }


def stats(cur):
    cur.execute("SELECT kind, status, count(*) FROM image_library GROUP BY 1,2 ORDER BY 1,2")
    out = {}
    for kind, status, n in cur.fetchall():
        out.setdefault(kind, {})[status] = n
    cur.execute("SELECT source, count(*) FROM image_library GROUP BY 1 ORDER BY 2 DESC")
    return {"by_kind": out, "by_source": [{"source": s, "count": n} for s, n in cur.fetchall()]}


# ---- topic tags ---------------------------------------------------------------------------
# Deliberately a small, fixed vocabulary. Stock images are searched and matched on these, so an
# open-ended tag set would mean a library nothing ever matches.

TOPICS = {
    "bus": ["bus", "city bus", "transit bus", "brt"],
    "light-rail": ["light rail", "tram", "streetcar"],
    "heavy-rail": ["subway", "metro train", "heavy rail"],
    "commuter-rail": ["commuter train", "regional rail"],
    "ferry": ["passenger ferry", "harbor ferry"],
    "station": ["transit station", "train platform", "bus terminal"],
    "construction": ["transit construction", "rail construction", "infrastructure construction"],
    "funding": ["government building", "capitol", "public finance"],
    "policy": ["legislature", "city hall", "public meeting"],
    "people": ["business portrait", "office meeting", "professional headshot"],
    "procurement": ["blueprint", "engineering plans", "contract signing"],
    "maintenance": ["rail maintenance", "bus depot", "vehicle maintenance"],
}


def topics_for(text, mode=None, pillar=None):
    """Topic tags for an article, from its words plus the mode/pillar we already tagged it with."""
    t = (text or "").lower()
    tags = []
    for tag, words in (
        ("bus", ("bus", "brt", "bus rapid")),
        ("light-rail", ("light rail", "streetcar", "tram")),
        ("heavy-rail", ("subway", "heavy rail", "metro rail")),
        ("commuter-rail", ("commuter rail", "regional rail", "commuter train")),
        ("ferry", ("ferry",)),
        ("station", ("station", "terminal", "platform")),
        ("construction", ("construction", "groundbreak", "build", "extension")),
        ("funding", ("grant", "funding", "budget", "appropriat", "million", "billion")),
        ("policy", ("policy", "legislat", "reauthoriz", "rule", "congress")),
        ("people", ("appoint", "named", "ceo", "general manager", "director", "hire")),
        ("procurement", ("rfp", "contract", "solicitation", "award", "bid", "design-build")),
        ("maintenance", ("maintenance", "overhaul", "state of good repair")),
    ):
        if any(w in t for w in words):
            tags.append(tag)
    for m in (mode or []):
        key = {"Bus": "bus", "BRT": "bus", "Light Rail": "light-rail", "Heavy Rail": "heavy-rail",
               "Commuter Rail": "commuter-rail", "Streetcar": "light-rail", "Ferry": "ferry"}.get(m)
        if key and key not in tags:
            tags.append(key)
    p = {"Funding": "funding", "Policy": "policy", "People": "people",
         "Procurement": "procurement"}.get(pillar or "")
    if p and p not in tags:
        tags.append(p)
    return tags


# ---- name matching ------------------------------------------------------------------------

_DROP = re.compile(r"\b(the|of|and|inc|authority|agency|district|department|transit|transportation|"
                   r"metropolitan|regional|county|city|public|system|services|service)\b")


def name_key(name):
    """A loose key for fuzzy-matching an agency name against a search result title."""
    s = (name or "").lower()
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    s = _DROP.sub(" ", s)
    return " ".join(s.split())


def acronym(s):
    """MBTA from "Massachusetts Bay Transportation Authority". Transit runs on these."""
    words = [w for w in re.sub(r"[^A-Za-z0-9\s]", " ", s or "").split() if w]
    return "".join(w[0] for w in words).lower()


def _compact(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def name_score(a, b):
    """0..1 similarity of two agency-ish names.

    Word overlap alone scores "MBTA" against "Massachusetts Bay Transportation Authority" at zero,
    which is the single most common shape of the problem here - so an acronym that matches the
    other side's initials counts as a match.
    """
    ca, cb = _compact(a), _compact(b)
    if not ca or not cb:
        return 0.0
    if ca == cb:
        return 1.0
    if ca == acronym(b) or cb == acronym(a):
        return 1.0
    ta, tb = set(name_key(a).split()), set(name_key(b).split())
    if not ta or not tb:
        return 0.0
    # One shared word is not a match. The alias "City of Madison" reduces to {madison}, which
    # scored a perfect 1.00 against "Madison County Transit" - a different agency, in a different
    # state. Anything resting on a single common word needs the names themselves to agree, which
    # the exact and acronym tests above already cover.
    if len(ta & tb) < 2:
        return 0.0
    # Symmetric, not containment. Containment scored "Massachusetts Bay Transportation Authority"
    # against "Massachusetts Department of Transportation" at 1.00, because the distinctive words
    # left after stripping boilerplate were {massachusetts, bay} and {massachusetts} - one side
    # fully contained in the other, and a different agency entirely.
    return len(ta & tb) / len(ta | tb)


def best_score(names, candidate):
    """The best match between a candidate title and any of an agency's known names/aliases.

    The reference table already curates aliases for exactly this; using them is far more reliable
    than trying to make one string comparison clever enough.
    """
    return max((name_score(n, candidate) for n in names if n), default=0.0)
