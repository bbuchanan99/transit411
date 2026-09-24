#!/usr/bin/env python3
"""
Transit411 read-only public API — the ONLY service the Cloudflare tunnel exposes.

It allowlists a handful of safe read/ask endpoints and forwards them to the Command
Center; every other path (publish, approve, delete, upload, collect, ...) returns 404,
so nothing that writes or mutates can be reached from the public internet.

The two "ask" endpoints each cost a model call on your Anthropic account, so they are limited
per visitor and per day, question size is capped, and they can be switched off entirely.

  uvicorn readonly_api:app --host 0.0.0.0 --port 8000
Env: UPSTREAM_URL (default http://command-center:8080)
     PUBLIC_ASK_ENABLED (default true), ASK_PER_IP_HOUR (20), ASK_PER_DAY (200), ASK_MAX_CHARS (500)
"""
import json
import os
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
import httpx
from fastapi import FastAPI, Request, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware

UPSTREAM = os.environ.get("UPSTREAM_URL", "http://command-center:8080")

# Spending limits for the model-backed endpoints (counts are in memory; a restart resets them).
ASK_PATHS = {"/api/ask", "/api/cig/ask"}
ASK_ENABLED = os.environ.get("PUBLIC_ASK_ENABLED", "true").lower() not in ("0", "false", "no", "off")
ASK_PER_IP_HOUR = int(os.environ.get("ASK_PER_IP_HOUR", "20"))
ASK_PER_DAY = int(os.environ.get("ASK_PER_DAY", "200"))
ASK_MAX_CHARS = int(os.environ.get("ASK_MAX_CHARS", "500"))
MAX_BODY = 16 * 1024
SNS_PATH = "/api/email/sns"
SNS_MAX_BODY = 512 * 1024
_ip_hits = defaultdict(deque)       # visitor ip -> timestamps of asks in the last hour
_day = {"date": None, "count": 0}   # asks today (UTC), across all visitors

# Newsletter signups: no model cost, but still rate-limited so the form can't be used to flood the
# contact list (or to probe who is on it - the API's reply is the same either way).
SUBSCRIBE_PATH = "/api/subscribe"
SUB_PER_IP_HOUR = int(os.environ.get("SUBSCRIBE_PER_IP_HOUR", "5"))
SUB_PER_DAY = int(os.environ.get("SUBSCRIBE_PER_DAY", "500"))
SUB_MAX_CHARS = 254
_sub_ip_hits = defaultdict(deque)
_sub_day = {"date": None, "count": 0}

# (method, exact path) pairs that are safe to expose publicly.
ALLOW = {
    ("GET", "/api/posts"),
    ("GET", "/api/cig"),
    ("GET", "/api/cig/history"),
    ("GET", "/api/cig/changes"),
    ("GET", "/api/cig/profile"),   # FTA's public project profile PDFs, only those in the committed lookup
    ("GET", "/api/agencies"),      # the agency reference table (names, NTD ids, links) the site builds from
    ("POST", "/api/ask"),
    ("POST", "/api/cig/ask"),
    ("POST", "/api/subscribe"),    # the ONLY public write: creates a pending newsletter contact
    ("GET", "/confirm"),           # double opt-in link from the confirmation email
    ("GET", "/unsubscribe"),       # one-click unsubscribe (human click)
    ("POST", "/unsubscribe"),      # one-click unsubscribe (RFC 8058, from the mail client)
    ("POST", "/api/email/sns"),    # SES bounce/complaint notifications (verified against SNS)
}

# Read-only paths with one path segment: GET /api/posts/<slug> (an article page's post). The slug is
# restricted so nothing else under /api/posts can be reached.
import re  # noqa: E402
ALLOW_PATTERNS = [("GET", re.compile(r"^/api/posts/[a-z0-9][a-z0-9-]{0,120}$"))]

app = FastAPI(title="Transit411 read-only public API")
# Only the Transit411 site (Pages preview + the domains) may call from a browser.
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"https://([a-z0-9-]+\.)?transit411\.(pages\.dev|net|com)",
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


@app.get("/health")
def health():
    return {"ok": True, "service": "readonly-api"}


def _visitor(request):
    # Only reachable through the tunnel (no published port), so Cloudflare's header is trustworthy.
    return request.headers.get("cf-connecting-ip") or (request.client.host if request.client else "unknown")


def _check_ask(request, body):
    """Enforce the ask limits and return a trimmed request body (question + at most 6 short turns)."""
    if not ASK_ENABLED:
        raise HTTPException(503, "Questions are turned off right now.")
    try:
        data = json.loads(body or b"{}")
        q = data.get("question")
    except (ValueError, AttributeError):
        raise HTTPException(400, "Send JSON with a 'question'.")
    if not isinstance(q, str) or not q.strip():
        raise HTTPException(400, "Send JSON with a 'question'.")
    if len(q) > ASK_MAX_CHARS:
        raise HTTPException(400, f"Please keep questions under {ASK_MAX_CHARS} characters.")
    hist = data.get("history") if isinstance(data.get("history"), list) else []
    hist = [{"question": str(t.get("question") or "")[:ASK_MAX_CHARS], "sql": str(t.get("sql") or "")[:4000]}
            for t in hist[-6:] if isinstance(t, dict)]
    now = time.time()
    today = datetime.now(timezone.utc).date()
    if _day["date"] != today:
        _day["date"], _day["count"] = today, 0
    if _day["count"] >= ASK_PER_DAY:
        raise HTTPException(429, "Today's question limit has been reached. Please try again tomorrow.",
                            headers={"Retry-After": "3600"})
    hits = _ip_hits[_visitor(request)]
    while hits and hits[0] < now - 3600:
        hits.popleft()
    if len(hits) >= ASK_PER_IP_HOUR:
        raise HTTPException(429, "You've asked a lot of questions this hour. Please try again a bit later.",
                            headers={"Retry-After": str(int(hits[0] + 3600 - now) + 1)})
    hits.append(now)
    _day["count"] += 1
    return json.dumps({"question": q.strip(), "history": hist}).encode()


def _check_subscribe(request, body):
    """Validate and rate-limit a signup, and pass on only email/name/source."""
    try:
        data = json.loads(body or b"{}")
        email = (data.get("email") or "").strip()
    except (ValueError, AttributeError):
        raise HTTPException(400, "Send JSON with an 'email'.")
    if not email or len(email) > SUB_MAX_CHARS or "@" not in email:
        raise HTTPException(400, "Please enter a valid email address.")
    now = time.time()
    today = datetime.now(timezone.utc).date()
    if _sub_day["date"] != today:
        _sub_day["date"], _sub_day["count"] = today, 0
    if _sub_day["count"] >= SUB_PER_DAY:
        raise HTTPException(429, "Too many signups right now. Please try again later.",
                            headers={"Retry-After": "3600"})
    hits = _sub_ip_hits[_visitor(request)]
    while hits and hits[0] < now - 3600:
        hits.popleft()
    if len(hits) >= SUB_PER_IP_HOUR:
        raise HTTPException(429, "You've tried that a few times already. Please try again later.",
                            headers={"Retry-After": str(int(hits[0] + 3600 - now) + 1)})
    hits.append(now)
    _sub_day["count"] += 1
    return json.dumps({"email": email, "name": str(data.get("name") or "")[:120],
                       "source": str(data.get("source") or "site")[:60]}).encode()


@app.api_route("/{path:path}", methods=["GET", "POST"])
async def proxy(path: str, request: Request):
    full = "/" + path
    if (request.method, full) not in ALLOW and not any(
            m == request.method and p.match(full) for m, p in ALLOW_PATTERNS):
        raise HTTPException(status_code=404, detail="Not found")
    body = await request.body()
    if len(body) > (SNS_MAX_BODY if full == SNS_PATH else MAX_BODY):
        raise HTTPException(413, "Request too large.")
    if full in ASK_PATHS:
        body = _check_ask(request, body)
    elif full == SUBSCRIBE_PATH:
        body = _check_subscribe(request, body)
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            up = await client.request(
                request.method, UPSTREAM + full,
                params=dict(request.query_params), content=body,
                headers={"content-type": "application/json" if full in ASK_PATHS or full == SUBSCRIBE_PATH
                         else request.headers.get("content-type", "application/json"),
                         # Tells the Command Center this came from the public site rather than the
                         # LAN, so usage can be counted separately. Internal only - it is set here,
                         # never read from the caller, so a visitor cannot claim to be either one.
                         "x-t411-surface": "public"},
            )
    except httpx.HTTPError:
        raise HTTPException(status_code=502, detail="Upstream unreachable")
    extra = {k: up.headers[k] for k in ("content-disposition",) if k in up.headers}
    return Response(content=up.content, status_code=up.status_code, headers=extra,
                    media_type=up.headers.get("content-type", "application/json"))
