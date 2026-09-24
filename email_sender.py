#!/usr/bin/env python3
"""
Transit411 email sending (Amazon SES) - the pipe only. The list itself lives in Postgres
(contacts.py), and nothing here ever mails an address that is suppressed there.

Every message carries the one-click unsubscribe headers Gmail and Yahoo require of bulk senders
(RFC 8058: List-Unsubscribe with an https URL, plus List-Unsubscribe-Post), and each recipient's
URL carries their own token.

Config (.env on the NAS; never committed):
  AWS_REGION, AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY   IAM user limited to ses:SendRawEmail
  SES_FROM            e.g. "Transit411 <news@mail.transit411.net>"
  SES_REPLY_TO        optional, e.g. hello@transit411.net
  SES_CONFIGURATION_SET  optional, if you create one for event publishing
  EMAIL_LINK_BASE     where confirm/unsubscribe links point (default https://api.transit411.net)
  SES_MAX_PER_SECOND  send rate (default 1 - the SES sandbox limit; raise after production access)
  SNS_TOPIC_ARN       optional, if set only that topic's notifications are accepted
"""
import os
import re
import time
from email.message import EmailMessage
from email.utils import formataddr, parseaddr

import contacts as cmod

REGION = os.environ.get("AWS_REGION", "us-east-1")
FROM = os.environ.get("SES_FROM", "")
REPLY_TO = os.environ.get("SES_REPLY_TO", "")
CONFIG_SET = os.environ.get("SES_CONFIGURATION_SET", "")
LINK_BASE = os.environ.get("EMAIL_LINK_BASE", "https://api.transit411.net").rstrip("/")
MAX_PER_SECOND = float(os.environ.get("SES_MAX_PER_SECOND", "1"))
SNS_TOPIC_ARN = os.environ.get("SNS_TOPIC_ARN", "")
SITE = os.environ.get("PUBLIC_SITE_URL", "https://transit411.net").rstrip("/")


class NotConfigured(RuntimeError):
    pass


def configured():
    return bool(FROM and os.environ.get("AWS_ACCESS_KEY_ID") and os.environ.get("AWS_SECRET_ACCESS_KEY"))


def _client():
    if not configured():
        raise NotConfigured("SES isn't set up yet: add AWS_REGION, AWS_ACCESS_KEY_ID, "
                            "AWS_SECRET_ACCESS_KEY and SES_FROM to .env on the NAS.")
    import boto3
    return boto3.client("ses", region_name=REGION)


def confirm_url(t):
    return LINK_BASE + "/confirm?t=" + str(t)


def unsub_url(t):
    return LINK_BASE + "/unsubscribe?t=" + str(t)


def _message(to_email, to_name, subject, text, html, unsub):
    """A MIME message with the one-click unsubscribe headers on every send."""
    m = EmailMessage()
    m["From"] = FROM
    m["To"] = formataddr((to_name or "", to_email))
    m["Subject"] = subject
    if REPLY_TO:
        m["Reply-To"] = REPLY_TO
    if unsub:
        m["List-Unsubscribe"] = "<" + unsub + ">"
        m["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
    m["List-Id"] = "Transit411 <news." + (parseaddr(FROM)[1].split("@")[-1] or "transit411.net") + ">"
    m.set_content(text)
    if html:
        m.add_alternative(html, subtype="html")
    return m


def send(to_email, subject, text, html=None, unsub=None, to_name=None):
    """Send one message through SES. Returns the SES message id."""
    ses = _client()
    msg = _message(to_email, to_name, subject, text, html, unsub)
    kwargs = {"Source": FROM, "Destinations": [to_email], "RawMessage": {"Data": msg.as_bytes()}}
    if CONFIG_SET:
        kwargs["ConfigurationSetName"] = CONFIG_SET
    return ses.send_raw_email(**kwargs)["MessageId"]


def send_throttled(conn, recipients, subject, build, log_type="send", per_second=None):
    """Send to many, at the SES rate limit, skipping anyone suppressed at the moment of sending.
    `build(recipient)` returns (text, html). Returns per-recipient results."""
    rate = per_second or MAX_PER_SECOND
    gap = 1.0 / rate if rate > 0 else 0
    out = []
    for r in recipients:
        if cmod.is_suppressed(conn, r["email"]):
            out.append({"email": r["email"], "status": "skipped", "detail": "suppressed"})
            continue
        text, html = build(r)
        started = time.monotonic()
        try:
            mid = send(r["email"], subject, text, html, unsub_url(r["unsub_token"]), r.get("name"))
            log_event(conn, r["email"], log_type, message_id=mid)
            out.append({"email": r["email"], "status": "sent", "message_id": mid})
        except Exception as e:                       # one bad address must not stop the run
            detail = (type(e).__name__ + ": " + str(e))[:300]
            log_event(conn, r["email"], "error", detail=detail)
            out.append({"email": r["email"], "status": "failed", "detail": detail})
        wait = gap - (time.monotonic() - started)
        if wait > 0:
            time.sleep(wait)
    return out


# ---- what we send -------------------------------------------------------------------------------
CONFIRM_HTML = """<!doctype html><html><body style="margin:0;background:#F2EEE4;padding:24px;font-family:Georgia,serif;color:#17140F">
<div style="max-width:560px;margin:0 auto;background:#fff;border:1px solid #D8D2C4;padding:28px">
<div style="font-family:Arial,sans-serif;font-weight:800;font-size:22px;letter-spacing:-.5px">TRANSIT<span style="color:#C0341F">411</span></div>
<h1 style="font-family:Arial,sans-serif;font-size:20px;margin:18px 0 10px">Confirm your subscription</h1>
<p style="font-size:16px;line-height:1.6">One click and you are on the list for <strong>Transit411 Intelligence</strong> &mdash; transit funding, procurement, people and policy.</p>
<p style="margin:22px 0"><a href="{link}" style="background:#17140F;color:#fff;font-family:Arial,sans-serif;font-weight:700;font-size:15px;padding:13px 22px;text-decoration:none;display:inline-block">Confirm subscription</a></p>
<p style="font-size:13px;color:#6A6458;line-height:1.6">If the button does not work, paste this into your browser:<br><span style="word-break:break-all">{link}</span></p>
<p style="font-size:13px;color:#6A6458">Did not sign up? Ignore this email and nothing happens.</p>
</div></body></html>"""


def confirmation_email(contact):
    link = confirm_url(contact["confirm_token"])
    text = ("Thanks for signing up for Transit411 Intelligence.\n\n"
            "Please confirm your subscription:\n" + link + "\n\n"
            "If you didn't sign up, ignore this message - you won't hear from us again.\n\n"
            "Transit411 - " + SITE + "\n")
    return text, CONFIRM_HTML.replace("{link}", link)


def send_confirmation(conn, contact):
    """Double opt-in: the only email a pending contact ever gets."""
    if cmod.is_suppressed(conn, contact["email"]):
        raise ValueError("That address is suppressed; it can't be mailed.")
    text, html = confirmation_email(contact)
    mid = send(contact["email"], "Confirm your Transit411 subscription", text, html,
               unsub_url(contact["unsub_token"]), contact.get("name"))
    log_event(conn, contact["email"], "confirmation", message_id=mid)
    return mid


def send_test(conn, to_email):
    """A one-off check that SES, DKIM and the headers are working. When the address is on the list the
    message carries that contact's real unsubscribe headers, so the test shows exactly what a
    subscriber sees - including the unsubscribe control mail clients render from them."""
    with conn.cursor() as cur:
        cur.execute("SELECT unsub_token, name FROM contacts WHERE email=%s", (to_email,))
        row = cur.fetchone()
    unsub = unsub_url(row[0]) if row and row[0] else None
    text = ("This is a Transit411 test message.\n\nIf you're reading it, SES sending, DKIM signing and "
            "the unsubscribe headers are all working.\n")
    html = ("<p>This is a <strong>Transit411</strong> test message.</p><p>If you're reading it, SES sending, "
            "DKIM signing and the unsubscribe headers are all working.</p>")
    if unsub:
        text += "\nUnsubscribe: " + unsub + "\n"
        html += '<p style="font-size:12px;color:#6A6458"><a href="' + unsub + '">Unsubscribe</a></p>'
    mid = send(to_email, "Transit411 test message", text, html, unsub, row[1] if row else None)
    log_event(conn, to_email, "test", message_id=mid)
    return mid


# ---- event log ----------------------------------------------------------------------------------
def create_tables(conn):
    with conn.cursor() as cur:
        cur.execute("""CREATE TABLE IF NOT EXISTS email_events (
            id BIGSERIAL PRIMARY KEY, email TEXT, type TEXT NOT NULL, detail TEXT, message_id TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now())""")
        cur.execute("CREATE INDEX IF NOT EXISTS email_events_email_idx ON email_events (email, created_at DESC)")
    conn.commit()


def log_event(conn, email, type_, detail=None, message_id=None):
    create_tables(conn)
    with conn.cursor() as cur:
        cur.execute("INSERT INTO email_events (email, type, detail, message_id) VALUES (%s,%s,%s,%s)",
                    (email, type_, detail, message_id))
    conn.commit()


# ---- SES -> SNS feedback (bounces and complaints) ------------------------------------------------
SNS_CERT_HOST = re.compile(r"^sns\.[a-z0-9-]+\.amazonaws\.com$")
SIG_FIELDS = {
    "Notification": ["Message", "MessageId", "Subject", "Timestamp", "TopicArn", "Type"],
    "SubscriptionConfirmation": ["Message", "MessageId", "SubscribeURL", "Timestamp", "Token", "TopicArn", "Type"],
    "UnsubscribeConfirmation": ["Message", "MessageId", "SubscribeURL", "Timestamp", "Token", "TopicArn", "Type"],
}
_certs = {}


def verify_sns(msg):
    """True only if this really came from SNS: the signing certificate is fetched from an
    amazonaws.com host over https and must verify the message's own canonical string."""
    import base64
    from urllib.parse import urlparse
    import requests
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    from cryptography.x509 import load_pem_x509_certificate

    if SNS_TOPIC_ARN and msg.get("TopicArn") != SNS_TOPIC_ARN:
        return False
    fields = SIG_FIELDS.get(msg.get("Type"))
    if not fields or not msg.get("Signature"):
        return False
    url = msg.get("SigningCertURL") or msg.get("SigningCertUrl") or ""
    u = urlparse(url)
    if u.scheme != "https" or not SNS_CERT_HOST.match(u.hostname or ""):
        return False
    try:
        pem = _certs.get(url)
        if pem is None:
            r = requests.get(url, timeout=10)
            r.raise_for_status()
            pem = r.content
            _certs[url] = pem
        canonical = "".join(f + "\n" + str(msg[f]) + "\n" for f in fields if f in msg).encode()
        algo = hashes.SHA256() if str(msg.get("SignatureVersion")) == "2" else hashes.SHA1()
        load_pem_x509_certificate(pem).public_key().verify(
            base64.b64decode(msg["Signature"]), canonical, padding.PKCS1v15(), algo)
        return True
    except Exception:   # unreachable cert, wrong cert, bad signature, bad base64 - all mean no
        return False


def _mark(conn, email, status, detail):
    """Suppress the address for good, and update its contact row if we have one."""
    cmod.suppress(conn, email, status, detail)
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM contacts WHERE email=%s", (email,))
        row = cur.fetchone()
    if row:
        cmod.set_status(conn, row[0], status, event=status, detail=detail)



def _note_issue(message_id, conn, status, detail):
    """If the message came from a newsletter issue, mark that recipient row too."""
    try:
        import newsletter
        newsletter.note_delivery_event(conn, message_id, status, detail)
    except Exception:
        pass      # feedback handling must never fail because of the issue log

def handle_sns(conn, msg):
    """Process a verified SNS message. Hard bounces and complaints suppress the address for good."""
    import json
    import requests
    kind = msg.get("Type")
    if kind == "SubscriptionConfirmation":
        requests.get(msg["SubscribeURL"], timeout=10)      # completes the handshake
        log_event(conn, None, "sns-subscribed", detail=msg.get("TopicArn"))
        return {"handled": "subscription confirmed"}
    if kind == "UnsubscribeConfirmation":
        log_event(conn, None, "sns-unsubscribed", detail=msg.get("TopicArn"))
        return {"handled": "topic unsubscribed"}
    if kind != "Notification":
        return {"handled": "ignored", "type": kind}
    body = json.loads(msg.get("Message") or "{}")
    orig_id = (body.get("mail") or {}).get("messageId")
    what = body.get("notificationType") or body.get("eventType")
    done = []
    if what == "Bounce":
        b = body.get("bounce", {})
        hard = b.get("bounceType") == "Permanent"
        for r in b.get("bouncedRecipients", []):
            email = cmod.normalize(r.get("emailAddress"))
            if not email:
                continue
            detail = (str(b.get("bounceType")) + "/" + str(b.get("bounceSubType")) + ": "
                      + str(r.get("diagnosticCode") or ""))[:300]
            log_event(conn, email, "bounce", detail=detail)
            _note_issue(orig_id, conn, "bounced", detail)
            if hard:
                _mark(conn, email, "bounced", detail)
            done.append({"email": email, "bounce": b.get("bounceType"), "suppressed": hard})
    elif what == "Complaint":
        c = body.get("complaint", {})
        for r in c.get("complainedRecipients", []):
            email = cmod.normalize(r.get("emailAddress"))
            if not email:
                continue
            detail = ("complaint: " + str(c.get("complaintFeedbackType") or "unknown"))[:300]
            log_event(conn, email, "complaint", detail=detail)
            _note_issue(orig_id, conn, "complained", detail)
            _mark(conn, email, "complained", detail)
            done.append({"email": email, "complaint": c.get("complaintFeedbackType"), "suppressed": True})
    elif what == "Delivery":
        for email in body.get("delivery", {}).get("recipients", []):
            log_event(conn, cmod.normalize(email), "delivery")
        done.append({"delivered": len(body.get("delivery", {}).get("recipients", []))})
    return {"handled": what, "recipients": done}
