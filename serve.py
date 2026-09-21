#!/usr/bin/env python3
"""
Minimal read-only Ask NTD API — run on your NAS network to test end to end.

  pip install -r requirements.txt
  uvicorn serve:app --host 0.0.0.0 --port 8000

  GET  /health                       -> {ok, rows}
  GET  /sql?q=SELECT ...             -> {sql, columns, rows}
  POST /ask   {"question": "..."}    -> {question, sql, columns, rows}   (needs ANTHROPIC_API_KEY)

NOT hardened for the public internet. Before exposing this beyond your LAN,
add authentication, rate limiting, and HTTPS, and keep the API key server-side.
Recommended: keep this API + the database internal; put the public website on
managed hosting and have it call this over a secure tunnel.
"""
import os
import anthropic
import duckdb
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

from query import run_sql, nl_to_sql  # same read-only guardrails as the CLI

DB = os.environ.get("NTD_DB", "ntd.duckdb")
app = FastAPI(title="Transit411 — Ask NTD")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


def _con():
    return duckdb.connect(DB, read_only=True)


def _result(df):
    # NULLs arrive as NaN/NA, which JSON can't encode; send them as null instead of failing the request.
    df = df.astype(object).where(df.notna(), None)
    return {"columns": list(df.columns), "rows": df.to_dict(orient="records"), "count": int(len(df))}


def _anthropic_detail(e):
    """A short, readable reason for a failed Anthropic call (never includes the API key)."""
    if isinstance(e, anthropic.APIStatusError):
        body = e.body if isinstance(e.body, dict) else {}
        msg = (body.get("error") or {}).get("message") or e.message
        return f"Anthropic API error ({e.status_code}): {msg}"
    if isinstance(e, anthropic.APIConnectionError):
        return "Could not reach the Anthropic API from the server. Check the NAS's internet connection."
    return f"Anthropic API error: {e}"


@app.get("/", include_in_schema=False)
def home():
    # The plain-English question page; /docs stays available for developers.
    return FileResponse(os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "index.html"))


@app.get("/health")
def health():
    try:
        con = _con()
        n = con.execute("SELECT COUNT(*) FROM ntd_service").fetchone()[0]
        h, y0, y1 = con.execute("SELECT COUNT(*), MIN(report_year), MAX(report_year) FROM ntd_history").fetchone()
        con.close()
        return {"ok": True, "rows": int(n), "history_rows": int(h), "years": [y0, y1]}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/sql")
def sql(q: str):
    con = _con()
    try:
        df = run_sql(con, q)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except duckdb.Error as e:
        raise HTTPException(status_code=400, detail=f"SQL error: {e}")
    finally:
        con.close()
    return {"sql": q, **_result(df)}


class Ask(BaseModel):
    question: str


@app.post("/ask")
def ask(a: Ask):
    try:
        generated = nl_to_sql(a.question)
    except anthropic.AnthropicError as e:
        # 502: the upstream model service failed, not this API or the question.
        raise HTTPException(status_code=502, detail=_anthropic_detail(e))
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    if not generated:
        raise HTTPException(status_code=400, detail="Set ANTHROPIC_API_KEY (or wire a local model) for natural language.")
    con = _con()
    try:
        df = run_sql(con, generated)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"Generated query was not read-only; refused. ({e})")
    except duckdb.Error as e:
        raise HTTPException(status_code=422, detail={"error": f"Generated SQL failed: {e}", "sql": generated})
    finally:
        con.close()
    return {"question": a.question, "sql": generated, **_result(df)}
