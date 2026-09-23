#!/usr/bin/env python3
"""
Transit411 newsletter: draft an issue from published posts, edit it, then send it through SES to
confirmed subscribers.

An issue is drafted from `content_posts` for a period, grouped by pillar, with every headline linking
to its own article page on the site (never straight to the source). Nothing is sent until someone
presses send in the Command Center, and only `subscribed` contacts that aren't suppressed receive it.

  status: draft -> sending -> sent (or failed)
"""
import os
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import wire_template

SITE = os.environ.get("PUBLIC_SITE_URL", "https://transit411.net").rstrip("/")
PILLAR_ORDER = ["Funding", "Procurement", "People", "Policy", "Data"]


def create_tables(conn):
    with conn.cursor() as cur:
        cur.execute("""CREATE TABLE IF NOT EXISTS issues (
            id BIGSERIAL PRIMARY KEY,
            issue_no INTEGER,
            subject TEXT NOT NULL,
            preheader TEXT,
            status TEXT NOT NULL DEFAULT 'draft',
            period_from DATE, period_to DATE,
            segment_tag TEXT,
            post_ids BIGINT[] NOT NULL DEFAULT '{}',
            html TEXT, text TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            sent_at TIMESTAMPTZ,
            recipients INTEGER DEFAULT 0, sent_count INTEGER DEFAULT 0, failed_count INTEGER DEFAULT 0,
            note TEXT)""")
        cur.execute("""CREATE TABLE IF NOT EXISTS issue_recipients (
            id BIGSERIAL PRIMARY KEY,
            issue_id BIGINT NOT NULL REFERENCES issues(id) ON DELETE CASCADE,
            email TEXT NOT NULL, status TEXT NOT NULL, message_id TEXT, detail TEXT,
            at TIMESTAMPTZ NOT NULL DEFAULT now())""")
        cur.execute("ALTER TABLE issues ADD COLUMN IF NOT EXISTS issue_no INTEGER")
        cur.execute("CREATE INDEX IF NOT EXISTS issue_recipients_issue ON issue_recipients (issue_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS issue_recipients_msg ON issue_recipients (message_id)")
    conn.commit()


def posts_for(conn, since, until):
    """Published posts in the period, newest first. image_url is read when the column exists (it is
    added by the images work); until then every item simply has no image."""
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM information_schema.columns WHERE table_name='content_posts' AND column_name='image_url'")
        has_image = bool(cur.fetchone())
        cur.execute("SELECT id, slug, pillar, title, body, source_name, state, publish_at"
                    + (", image_url" if has_image else "") + " FROM content_posts "
                    "WHERE status='published' AND publish_at >= %s AND publish_at < %s "
                    "ORDER BY publish_at DESC, id DESC", (since, until))
        out = []
        for row in cur.fetchall():
            pid, slug, pillar, title, body, source, state, at = row[:8]
            summary = (body or "").split("\n\nSource:")[0].strip()
            out.append({"id": pid, "slug": slug, "pillar": pillar or "News", "title": title,
                        "summary": summary, "source": source, "state": state, "publish_at": at,
                        "image_url": row[8] if has_image and len(row) > 8 else None})
    return out


def cig_stat(conn):
    """The By the Numbers block, straight from our own pipeline data (no model call)."""
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('cig_projects') IS NOT NULL")
            if not cur.fetchone()[0]:
                return None
            cur.execute("SELECT max(snapshot_date) FROM cig_projects")
            snap = cur.fetchone()[0]
            if not snap:
                return None
            cur.execute("SELECT count(*), coalesce(sum(cig_request_musd),0), "
                        "count(*) FILTER (WHERE phase='Eng') FROM cig_projects WHERE snapshot_date=%s", (snap,))
            n, total, eng = cur.fetchone()
    except Exception:
        return None
    if not n:
        return None
    billions = float(total) / 1000.0
    figure = ("$%.1fB" % billions) if billions >= 1 else ("$%.0fM" % float(total))
    caption = ("total CIG funding sought across %d active pipeline projects this month" % n
               + (" - %d of them now in Engineering." % eng if eng else "."))
    return {"figure": figure, "caption": caption, "cta": "Ask the pipeline yourself",
            "cta_url": SITE + "/ask-cig?q=" + quote("Which projects are closest to a funding grant agreement?")}


def assemble(conn, posts, issue_no, dateline, intro=None, lead_id=None):
    """Sort the period's posts into the running order The Wire uses: a lead, the feed, people moves and
    procurements. People and Procurement items get their own sections rather than crowding the feed."""
    by_id = {p["id"]: p for p in posts}
    def url(p):
        return SITE + "/article/" + (p["slug"] or "")
    def item(p):
        return {"id": p["id"], "title": p["title"], "summary": p["summary"], "pillar": p["pillar"],
                "source": p.get("source"), "state": p.get("state"), "url": url(p),
                "image_url": p.get("image_url"), "image_alt": p.get("image_alt")}
    people = [item(p) for p in posts if p["pillar"] == "People"]
    procure = [item(p) for p in posts if p["pillar"] == "Procurement"]
    rest = [p for p in posts if p["pillar"] not in ("People", "Procurement")] or posts
    lead_post = by_id.get(lead_id) or (rest[0] if rest else None)
    feed = [item(p) for p in rest if not lead_post or p["id"] != lead_post["id"]][:4]
    return {"date_label": dateline, "issue_no": issue_no, "site": SITE, "preheader": intro or "",
            "lead": item(lead_post) if lead_post else None, "feed": feed,
            "stat": cig_stat(conn), "moves": people[:3], "procurements": procure[:3]}


def draft(conn, since=None, until=None, tag=None, days=7, intro=None, lead_id=None):
    """Create a draft issue of The Wire from the posts published in a period (default: the last 7 days)."""
    create_tables(conn)
    until = until or datetime.now(timezone.utc).date() + timedelta(days=1)
    since = since or (until - timedelta(days=days + 1))
    posts = posts_for(conn, since, until)
    dateline = datetime.now(timezone.utc).strftime("%A, %b. %d, %Y").replace(" 0", " ")
    with conn.cursor() as cur:
        cur.execute("SELECT coalesce(max(issue_no), 0) + 1 FROM issues")
        issue_no = cur.fetchone()[0]
    data = assemble(conn, posts, issue_no, dateline, intro, lead_id)
    html, text = wire_template.render_html(data), wire_template.render_text(data)
    subject = default_subject(data)
    with conn.cursor() as cur:
        cur.execute("INSERT INTO issues (issue_no, subject, preheader, status, period_from, period_to, segment_tag, "
                    "post_ids, html, text) VALUES (%s,%s,%s,'draft',%s,%s,%s,%s,%s,%s) RETURNING id",
                    (issue_no, subject, intro, since, until, tag, [p["id"] for p in posts], html, text))
        iid = cur.fetchone()[0]
    conn.commit()
    return {"id": iid, "issue_no": issue_no, "posts": len(posts), "subject": subject,
            "period_from": str(since), "period_to": str(until),
            "sections": {"lead": bool(data["lead"]), "feed": len(data["feed"]), "moves": len(data["moves"]),
                         "procurements": len(data["procurements"]), "stat": bool(data["stat"])}}


def default_subject(data):
    lead = (data.get("lead") or {}).get("title")
    return ("The Wire: " + lead)[:120] if lead else "The Wire - Transit411"


def get(conn, issue_id):
    create_tables(conn)
    cols = ["id", "issue_no", "subject", "preheader", "status", "period_from", "period_to", "segment_tag", "post_ids",
            "html", "text", "created_at", "sent_at", "recipients", "sent_count", "failed_count", "note"]
    with conn.cursor() as cur:
        cur.execute("SELECT " + ", ".join(cols) + " FROM issues WHERE id=%s", (issue_id,))
        row = cur.fetchone()
    if not row:
        return None
    d = dict(zip(cols, row))
    for k in ("period_from", "period_to", "created_at", "sent_at"):
        d[k] = d[k].isoformat() if d[k] else None
    return d


def listing(conn, limit=30):
    create_tables(conn)
    with conn.cursor() as cur:
        cur.execute("SELECT id, coalesce(issue_no, id), subject, status, to_char(created_at AT TIME ZONE 'America/New_York','YYYY-MM-DD HH24:MI'), "
                    "to_char(sent_at AT TIME ZONE 'America/New_York','YYYY-MM-DD HH24:MI'), recipients, sent_count, "
                    "failed_count, cardinality(post_ids), segment_tag FROM issues ORDER BY id DESC LIMIT %s", (limit,))
        keys = ["id", "issue_no", "subject", "status", "created_at", "sent_at", "recipients", "sent_count", "failed_count",
                "posts", "segment_tag"]
        return [dict(zip(keys, r)) for r in cur.fetchall()]


def update(conn, issue_id, subject=None, html=None, text=None, preheader=None):
    """Edit a draft. A sent issue is never changed."""
    d = get(conn, issue_id)
    if not d:
        return None
    if d["status"] == "sent":
        raise ValueError("That issue has been sent; drafts can be edited, sent issues can't.")
    sets, params = [], []
    for col, val in (("subject", subject), ("html", html), ("text", text), ("preheader", preheader)):
        if val is not None:
            sets.append(col + "=%s")
            params.append(val)
    if sets:
        with conn.cursor() as cur:
            cur.execute("UPDATE issues SET " + ", ".join(sets) + " WHERE id=%s", params + [issue_id])
        conn.commit()
    return get(conn, issue_id)


def personalize(issue, unsub):
    """Fill in the recipient's own unsubscribe URL."""
    return (issue["html"] or "").replace("{unsub}", unsub), (issue["text"] or "").replace("{unsub}", unsub)


def send_test(conn, issue_id, to_email):
    """Send the issue to one address, marked as a test. Doesn't change the issue's status."""
    import contacts as cmod
    import email_sender as sender
    issue = get(conn, issue_id)
    if not issue:
        return None
    if cmod.is_suppressed(conn, to_email):
        raise ValueError("That address is on the do-not-email list.")
    with conn.cursor() as cur:
        cur.execute("SELECT unsub_token FROM contacts WHERE email=%s", (to_email,))
        row = cur.fetchone()
    unsub = sender.unsub_url(row[0]) if row and row[0] else sender.LINK_BASE + "/unsubscribe?t=test"
    html, text = personalize(issue, unsub)
    mid = sender.send(to_email, "[TEST] " + issue["subject"], text, html, unsub)
    sender.log_event(conn, to_email, "issue-test", detail="issue " + str(issue_id), message_id=mid)
    return mid


def send_issue(conn, issue_id, tag=None, limit=None):
    """Send to every confirmed subscriber (optionally one tag), throttled, skipping anyone suppressed.
    Each recipient's result is recorded in issue_recipients. Refuses to send an issue twice."""
    import contacts as cmod
    import email_sender as sender
    issue = get(conn, issue_id)
    if not issue:
        return None
    if issue["status"] in ("sent", "sending"):
        raise ValueError("That issue is already " + issue["status"] + ".")
    people = cmod.mailable(conn, tag or issue["segment_tag"], limit)
    if not people:
        raise ValueError("Nobody to send to: no confirmed subscribers" + (" with that tag." if tag else "."))
    with conn.cursor() as cur:
        cur.execute("UPDATE issues SET status='sending', recipients=%s WHERE id=%s", (len(people), issue_id))
    conn.commit()

    def build(r):
        return personalize(issue, sender.unsub_url(r["unsub_token"]))

    results = sender.send_throttled(conn, people, issue["subject"], build, log_type="issue")
    sent = sum(1 for r in results if r["status"] == "sent")
    failed = sum(1 for r in results if r["status"] == "failed")
    with conn.cursor() as cur:
        for r in results:
            cur.execute("INSERT INTO issue_recipients (issue_id, email, status, message_id, detail) "
                        "VALUES (%s,%s,%s,%s,%s)",
                        (issue_id, r["email"], r["status"], r.get("message_id"), r.get("detail")))
        cur.execute("UPDATE issues SET status=%s, sent_at=now(), sent_count=%s, failed_count=%s WHERE id=%s",
                    ("sent" if sent else "failed", sent, failed, issue_id))
    conn.commit()
    return {"issue_id": issue_id, "recipients": len(people), "sent": sent, "failed": failed,
            "skipped": sum(1 for r in results if r["status"] == "skipped"), "results": results[:50]}


def note_delivery_event(conn, message_id, status, detail=None):
    """A bounce/complaint arriving later updates that recipient's row (matched on the SES message id)."""
    if not message_id:
        return 0
    with conn.cursor() as cur:
        cur.execute("UPDATE issue_recipients SET status=%s, detail=coalesce(%s, detail) WHERE message_id=%s",
                    (status, detail, message_id))
        n = cur.rowcount
    conn.commit()
    return n
