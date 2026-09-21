#!/usr/bin/env python3
"""
Transit411 read-only public API — the ONLY service the Cloudflare tunnel exposes.

It allowlists a handful of safe read/ask endpoints and forwards them to the Command
Center; every other path (publish, approve, delete, upload, collect, ...) returns 404,
so nothing that writes or mutates can be reached from the public internet.

  uvicorn readonly_api:app --host 0.0.0.0 --port 8000
Env: UPSTREAM_URL (default http://command-center:8080)
"""
import os
import httpx
from fastapi import FastAPI, Request, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware

UPSTREAM = os.environ.get("UPSTREAM_URL", "http://command-center:8080")

# (method, exact path) pairs that are safe to expose publicly.
ALLOW = {
    ("GET", "/api/posts"),
    ("GET", "/api/cig"),
    ("GET", "/api/cig/history"),
    ("POST", "/api/ask"),
    ("POST", "/api/cig/ask"),
}

app = FastAPI(title="Transit411 read-only public API")
# Only the Transit411 site (Pages preview + the domain) may call from a browser.
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"https://([a-z0-9-]+\.)?transit411\.(pages\.dev|com)",
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


@app.get("/health")
def health():
    return {"ok": True, "service": "readonly-api"}


@app.api_route("/{path:path}", methods=["GET", "POST"])
async def proxy(path: str, request: Request):
    full = "/" + path
    if (request.method, full) not in ALLOW:
        raise HTTPException(status_code=404, detail="Not found")
    body = await request.body()
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            up = await client.request(
                request.method, UPSTREAM + full,
                params=dict(request.query_params), content=body,
                headers={"content-type": request.headers.get("content-type", "application/json")},
            )
    except httpx.HTTPError:
        raise HTTPException(status_code=502, detail="Upstream unreachable")
    return Response(content=up.content, status_code=up.status_code,
                    media_type=up.headers.get("content-type", "application/json"))
