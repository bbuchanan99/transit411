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

SITE = os.environ.get("PUBLIC_SITE_URL", "https://transit411.net").rstrip("/")
PILLAR_ORDER = ["Funding", "Procurement", "People", "Policy", "Data"]


def create_tables(conn):
    with conn.cursor() as cur:
        cur.execute("""CREATE TABLE IF NOT EXISTS issues (
            id BIGSERIAL PRIMARY KEY,
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
        cur.execute("CREATE INDEX IF NOT EXISTS issue_recipients_issue ON issue_recipients (issue_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS issue_recipients_msg ON issue_recipients (message_id)")
    conn.commit()


def posts_for(conn, since, until):
    """Published posts in the period, newest first."""
    with conn.cursor() as cur:
        cur.execute("SELECT id, slug, pillar, title, body, source_name, publish_at FROM content_posts "
                    "WHERE status='published' AND publish_at >= %s AND publish_at < %s "
                    "ORDER BY publish_at DESC, id DESC", (since, until))
        out = []
        for pid, slug, pillar, title, body, source, at in cur.fetchall():
            summary = (body or "").split("\n\nSource:")[0].strip()
            out.append({"id": pid, "slug": slug, "pillar": pillar or "News", "title": title,
                        "summary": summary, "source": source, "publish_at": at})
    return out


def esc(s):
    return (str(s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;"))


def group_by_pillar(posts):
    groups = {}
    for p in posts:
        groups.setdefault(p["pillar"], []).append(p)
    ordered = [(k, groups[k]) for k in PILLAR_ORDER if k in groups]
    return ordered + [(k, v) for k, v in sorted(groups.items()) if k not in PILLAR_ORDER]


HEAD = """<!doctype html><html><body style="margin:0;background:#F2EEE4;padding:24px 12px;font-family:Georgia,serif;color:#17140F">
<div style="max-width:640px;margin:0 auto;background:#fff;border:1px solid #D8D2C4">
<div style="padding:26px 28px 18px;border-bottom:3px solid #17140F">
<div style="font-family:Arial,sans-serif;font-weight:800;font-size:26px;letter-spacing:-1px">TRANSIT<span style="color:#C0341F">411</span></div>
<div style="font-family:Arial,sans-serif;font-size:11px;letter-spacing:1.5px;text-transform:uppercase;color:#6A6458;margin-top:6px">{dateline}</div>
</div>
<div style="padding:22px 28px 8px">"""

FOOT = """</div>
<div style="padding:18px 28px 26px;border-top:1px solid #D8D2C4;font-family:Arial,sans-serif;font-size:12px;color:#6A6458">
<p style="margin:0 0 8px">You're receiving this because you subscribed at transit411.net.</p>
<p style="margin:0"><a href="{unsub}" style="color:#6A6458">Unsubscribe</a> &middot; <a href="{site}" style="color:#6A6458">Transit411</a></p>
</div></div></body></html>"""


def render(posts, dateline, intro=None):
    """(html, text) for an issue. {unsub} is filled in per recipient at send time."""
    html = [HEAD.replace("{dateline}", esc(dateline))]
    text = ["TRANSIT411 - " + dateline, ""]
    if intro:
        html.append('<p style="font-size:17px;line-height:1.6;margin:0 0 20px">' + esc(intro) + "</p>")
        text += [intro, ""]
    for pillar, items in group_by_pillar(posts):
        html.append('<div style="font-family:Arial,sans-serif;font-size:11px;font-weight:800;letter-spacing:1px;'
                    'text-transform:uppercase;color:#C0341F;border-bottom:2px solid #17140F;padding-bottom:6px;'
                    'margin:22px 0 14px">' + esc(pillar) + "</div>")
        text += [pillar.upper(), "-" * len(pillar)]
        for p in items:
            url = SITE + "/article/" + (p["slug"] or "")
            html.append('<div style="margin:0 0 18px">'
                        '<a href="' + esc(url) + '" style="font-family:Arial,sans-serif;font-weight:700;font-size:18px;'
                        'line-height:1.3;color:#17140F;text-decoration:none">' + esc(p["title"]) + "</a>"
                        + ('<div style="font-size:15px;line-height:1.55;color:#3B3730;margin-top:6px">'
                           + esc(p["summary"]) + "</div>" if p["summary"] else "")
                        + ('<div style="font-family:Arial,sans-serif;font-size:12px;color:#6A6458;margin-top:6px">'
                           + esc(p["source"] or "") + "</div>" if p.get("source") else "")
                        + "</div>")
            text += [p["title"], (p["summary"] or "").strip(), url, ""]
        text.append("")
    html.append(FOOT.replace("{site}", SITE))
    text += ["--", "You're receiving this because you subscribed at transit411.net.", "Unsubscribe: {unsub}"]
    return "".join(html), "\n".join(text)


def default_subject(posts, dateline):
    lead = posts[0]["title"] if posts else "Transit411 Weekly Intelligence"
    return ("Transit411: " + lead)[:120]


def draft(conn, since=None, until=None, tag=None, days=7, intro=None):
    """Create a draft issue from the posts published in a period (default: the last 7 days)."""
    create_tables(conn)
    until = until or datetime.now(timezone.utc).date() + timedelta(days=1)
    since = since or (until - timedelta(days=days + 1))
    posts = posts_for(conn, since, until)
    dateline = datetime.now(timezone.utc).strftime("%B %-d, %Y") if os.name != "nt" else \
        datetime.now(timezone.utc).strftime("%B %d, %Y")
    html, text = render(posts, dateline, intro)
    subject = default_subject(posts, dateline)
    with conn.cursor() as cur:
        cur.execute("INSERT INTO issues (subject, status, period_from, period_to, segment_tag, post_ids, html, text) "
                    "VALUES (%s,'draft',%s,%s,%s,%s,%s,%s) RETURNING id",
                    (subject, since, until, tag, [p["id"] for p in posts], html, text))
        iid = cur.fetchone()[0]
    conn.commit()
    return {"id": iid, "posts": len(posts), "subject": subject, "period_from": str(since), "period_to": str(until)}


def get(conn, issue_id):
    create_tables(conn)
    cols = ["id", "subject", "preheader", "status", "period_from", "period_to", "segment_tag", "post_ids",
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
        cur.execute("SELECT id, subject, status, to_char(created_at AT TIME ZONE 'America/New_York','YYYY-MM-DD HH24:MI'), "
                    "to_char(sent_at AT TIME ZONE 'America/New_York','YYYY-MM-DD HH24:MI'), recipients, sent_count, "
                    "failed_count, cardinality(post_ids), segment_tag FROM issues ORDER BY id DESC LIMIT %s", (limit,))
        keys = ["id", "subject", "status", "created_at", "sent_at", "recipients", "sent_count", "failed_count",
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
