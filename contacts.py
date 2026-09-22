#!/usr/bin/env python3
"""
Transit411 contact database - the master list we own (Postgres). SES will only be the sending pipe.

Statuses: pending (signed up, not confirmed), subscribed (confirmed - the only ones that get mail),
unsubscribed, bounced, complained. The last three are suppressed: they are also written to
email_suppressions, and mailable() reads that table, so a bad address can never be mailed again even
if a later import re-adds it.

  python contacts.py --import file.csv --source "import"   # add/update from CSV (dedupes on email)
  python contacts.py --export out.csv
  python contacts.py --counts
"""
import argparse
import csv
import io
import os
import re
import secrets

STATUSES = ["pending", "subscribed", "unsubscribed", "bounced", "complained"]
SUPPRESSED = ("unsubscribed", "bounced", "complained")
EMAIL_RE = re.compile(r"^[^@\s,;<>]{1,64}@[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$")
MAX_EMAIL = 254


def normalize(email):
    """Lowercased and trimmed, or None if it isn't a plausible address. Angle brackets and a display
    name ("Jane <jane@x.org>") are stripped, since CSV exports often carry them."""
    e = (email or "").strip()
    m = re.search(r"<([^>]+)>", e)
    if m:
        e = m.group(1).strip()
    e = e.strip("\"' ").lower()
    return e if len(e) <= MAX_EMAIL and EMAIL_RE.match(e) else None


def token():
    return secrets.token_urlsafe(32)


def create_tables(conn):
    with conn.cursor() as cur:
        cur.execute("""CREATE TABLE IF NOT EXISTS contacts (
            id BIGSERIAL PRIMARY KEY,
            email TEXT NOT NULL,
            name TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            source TEXT,
            tags TEXT[] NOT NULL DEFAULT '{}',
            confirm_token TEXT,
            unsub_token TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            confirmed_at TIMESTAMPTZ,
            unsubscribed_at TIMESTAMPTZ,
            last_event_at TIMESTAMPTZ,
            last_event TEXT,
            notes TEXT)""")
        cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS contacts_email_uniq ON contacts (email)")
        cur.execute("CREATE INDEX IF NOT EXISTS contacts_status_idx ON contacts (status)")
        cur.execute("CREATE INDEX IF NOT EXISTS contacts_tags_idx ON contacts USING GIN (tags)")
        # Authoritative do-not-email list: survives edits, imports and re-signups.
        cur.execute("""CREATE TABLE IF NOT EXISTS email_suppressions (
            email TEXT PRIMARY KEY, reason TEXT NOT NULL, detail TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT now())""")
    conn.commit()


def suppress(conn, email, reason, detail=None):
    """Add an address to the do-not-email list (idempotent; the first reason is kept)."""
    with conn.cursor() as cur:
        cur.execute("INSERT INTO email_suppressions (email, reason, detail) VALUES (%s,%s,%s) "
                    "ON CONFLICT (email) DO NOTHING", (email, reason, detail))
    conn.commit()


def is_suppressed(conn, email):
    with conn.cursor() as cur:
        cur.execute("SELECT reason FROM email_suppressions WHERE email=%s", (email,))
        row = cur.fetchone()
    return row[0] if row else None


def set_status(conn, contact_id, status, event=None, detail=None):
    """Change a contact's status, stamping the matching timestamp and suppressing where needed."""
    if status not in STATUSES:
        raise ValueError(f"status must be one of {', '.join(STATUSES)}")
    with conn.cursor() as cur:
        cur.execute("SELECT email FROM contacts WHERE id=%s", (contact_id,))
        row = cur.fetchone()
        if not row:
            return None
        cur.execute("UPDATE contacts SET status=%s, last_event_at=now(), last_event=%s, "
                    "confirmed_at = CASE WHEN %s='subscribed' THEN coalesce(confirmed_at, now()) ELSE confirmed_at END, "
                    "unsubscribed_at = CASE WHEN %s='unsubscribed' THEN coalesce(unsubscribed_at, now()) ELSE unsubscribed_at END "
                    "WHERE id=%s", (status, event or status, status, status, contact_id))
    conn.commit()
    if status in SUPPRESSED:
        suppress(conn, row[0], status, detail)
    return row[0]


def upsert(conn, email, name=None, source=None, tags=None, status="pending"):
    """Add a contact, or update an existing one without ever resurrecting a suppressed address or
    downgrading someone already subscribed. Returns (row_id, what) where what is
    'added' | 'updated' | 'suppressed' | 'invalid'."""
    e = normalize(email)
    if not e:
        return None, "invalid"
    create_tables(conn)
    with conn.cursor() as cur:
        cur.execute("SELECT id, status FROM contacts WHERE email=%s", (e,))
        row = cur.fetchone()
        supp = is_suppressed(conn, e)
        if row:
            cur.execute("UPDATE contacts SET name=coalesce(nullif(%s,''), name), "
                        "source=coalesce(source, %s), tags=(SELECT array(SELECT DISTINCT unnest(tags || %s::text[]))) "
                        "WHERE id=%s", (name, source, tags or [], row[0]))
            conn.commit()
            return row[0], "suppressed" if (supp or row[1] in SUPPRESSED) else "updated"
        if supp:
            return None, "suppressed"   # never re-add someone who bounced, complained or opted out
        cur.execute("INSERT INTO contacts (email, name, status, source, tags, confirm_token, unsub_token) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                    (e, name or None, status, source, tags or [], token(), token()))
        cid = cur.fetchone()[0]
    conn.commit()
    return cid, "added"


def import_csv(conn, text, source="csv import", default_tags=None):
    """Load a CSV. Any column named email/e-mail/email address is the address; name/full name and
    tags (comma or semicolon separated) are used when present; a file with no header is read as
    one address per line. Dedupes on email."""
    create_tables(conn)
    rows, result = [], {"added": 0, "updated": 0, "suppressed": 0, "invalid": 0, "duplicate_in_file": 0}
    sample = text[:4096]
    has_header = bool(re.search(r"(?im)^[^\n]*\b(e-?mail|email address)\b", sample))
    if has_header:
        for r in csv.DictReader(io.StringIO(text)):
            keys = {(k or "").strip().lower(): (v or "").strip() for k, v in r.items()}
            email = next((keys[k] for k in ("email", "e-mail", "email address", "emailaddress") if keys.get(k)), "")
            name = next((keys[k] for k in ("name", "full name", "fullname", "contact") if keys.get(k)), "")
            tags = [t.strip() for t in re.split(r"[;,]", keys.get("tags", "")) if t.strip()]
            rows.append((email, name, tags))
    else:
        for line in io.StringIO(text):
            parts = [p.strip() for p in line.split(",")]
            if parts and parts[0]:
                rows.append((parts[0], parts[1] if len(parts) > 1 else "", []))
    seen = set()
    for email, name, tags in rows:
        e = normalize(email)
        if e and e in seen:
            result["duplicate_in_file"] += 1
            continue
        if e:
            seen.add(e)
        _, what = upsert(conn, email, name, source, (tags or []) + list(default_tags or []))
        result[what] = result.get(what, 0) + 1
    result["rows"] = len(rows)
    return result


def export_csv(conn, status=None):
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["email", "name", "status", "source", "tags", "created_at", "confirmed_at", "unsubscribed_at"])
    with conn.cursor() as cur:
        cur.execute("SELECT email, name, status, source, tags, created_at, confirmed_at, unsubscribed_at FROM contacts"
                    + (" WHERE status=%s" if status else "") + " ORDER BY created_at", (status,) if status else ())
        for r in cur.fetchall():
            w.writerow([r[0], r[1] or "", r[2], r[3] or "", ";".join(r[4] or []),
                        *[(x.isoformat() if x else "") for x in r[5:]]])
    return out.getvalue()


def counts(conn):
    create_tables(conn)
    with conn.cursor() as cur:
        cur.execute("SELECT status, count(*) FROM contacts GROUP BY status")
        by_status = {s: 0 for s in STATUSES}
        by_status.update({s: n for s, n in cur.fetchall()})
        cur.execute("SELECT count(*) FROM email_suppressions")
        supp = cur.fetchone()[0]
    return {"by_status": by_status, "total": sum(by_status.values()), "suppressed": supp,
            "mailable": by_status.get("subscribed", 0)}


def mailable(conn, tag=None, limit=None):
    """Who may receive a send: subscribed, and not on the suppression list. Phase 2 sends read this."""
    with conn.cursor() as cur:
        cur.execute("SELECT c.id, c.email, c.name, c.unsub_token FROM contacts c "
                    "LEFT JOIN email_suppressions s ON s.email = c.email "
                    "WHERE c.status='subscribed' AND s.email IS NULL"
                    + (" AND %s = ANY(c.tags)" if tag else "") + " ORDER BY c.id"
                    + (" LIMIT %s" % int(limit) if limit else ""), (tag,) if tag else ())
        return [dict(zip(("id", "email", "name", "unsub_token"), r)) for r in cur.fetchall()]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--import", dest="imp", metavar="CSV")
    ap.add_argument("--export", metavar="CSV")
    ap.add_argument("--source", default="command line")
    ap.add_argument("--counts", action="store_true")
    a = ap.parse_args()
    import psycopg
    dsn = os.environ.get("DATABASE_URL", "postgresql://transit411:transit411@db:5432/transit411")
    with psycopg.connect(dsn) as conn:
        create_tables(conn)
        if a.imp:
            with open(a.imp, encoding="utf-8-sig") as fh:
                print("Import:", import_csv(conn, fh.read(), a.source))
        if a.export:
            with open(a.export, "w", encoding="utf-8", newline="") as fh:
                fh.write(export_csv(conn))
            print("Exported to", a.export)
        if a.counts or not (a.imp or a.export):
            print("Contacts:", counts(conn))


if __name__ == "__main__":
    main()
