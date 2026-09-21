#!/usr/bin/env python3
"""
Transit411 Command Center — the private home-base dashboard (LAN only).
Ties the live Ask NTD API and the Postgres store into one branded page.

  uvicorn command_center:app --host 0.0.0.0 --port 8080

Env:
  API_URL       (default http://api:8000)         -- the Ask NTD API service
  DATABASE_URL  (default postgresql://transit411:transit411@db:5432/transit411)
"""
import os
import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

try:
    import psycopg
except Exception:
    psycopg = None

API_URL = os.environ.get("API_URL", "http://api:8000")
DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://transit411:transit411@db:5432/transit411")

app = FastAPI(title="Transit411 Command Center")


@app.get("/api/status")
def status():
    out = {"api": {"ok": False}, "db": {"ok": False}}
    try:
        out["api"] = httpx.get(f"{API_URL}/health", timeout=5).json()
    except Exception as e:
        out["api"] = {"ok": False, "error": str(e)}
    if psycopg:
        try:
            with psycopg.connect(DATABASE_URL, connect_timeout=5) as c, c.cursor() as cur:
                cur.execute("SELECT count(*) FROM information_schema.tables WHERE table_schema='public'")
                out["db"] = {"ok": True, "tables": cur.fetchone()[0]}
        except Exception as e:
            out["db"] = {"ok": False, "error": str(e)}
    else:
        out["db"] = {"ok": False, "error": "psycopg not installed"}
    return out


class Ask(BaseModel):
    question: str


@app.post("/api/ask")
def ask(a: Ask):
    try:
        r = httpx.post(f"{API_URL}/ask", json={"question": a.question}, timeout=60)
    except httpx.HTTPError as e:
        raise HTTPException(502, f"API unreachable: {e}")
    if r.status_code != 200:
        raise HTTPException(r.status_code, r.text)
    return r.json()


@app.get("/api/query")
def query(q: str):
    try:
        r = httpx.get(f"{API_URL}/sql", params={"q": q}, timeout=30)
    except httpx.HTTPError as e:
        raise HTTPException(502, f"API unreachable: {e}")
    if r.status_code != 200:
        raise HTTPException(r.status_code, r.text)
    return r.json()


@app.get("/", response_class=HTMLResponse)
def home():
    return DASHBOARD


DASHBOARD = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Transit411 — Command Center</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Archivo:wght@600;700;800;900&family=Spectral:wght@400;500;600&family=JetBrains+Mono:wght@400;600&display=swap">
<style>
:root{--bg:#F2EEE4;--panel:#F7F4ED;--card:#FFF;--ink:#17140F;--muted:#6A6458;--line:#D8D2C4;--soft:#E7E1D4;--accent:#C0341F;--ok:#1F6B4A;--bar:#C0341F;--track:#EDE7D8}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font-family:'Spectral',Georgia,serif}
.disp{font-family:'Archivo',sans-serif}
header{background:var(--ink);color:var(--panel);padding:16px 24px;display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:12px}
.brand{font-family:'Archivo',sans-serif;font-weight:900;font-size:22px;letter-spacing:-1px}.brand span{color:var(--accent)}
.sub{font-family:'Archivo',sans-serif;font-size:10px;letter-spacing:2.5px;text-transform:uppercase;color:#A69F90}
.status{display:flex;gap:16px;font-family:'Archivo',sans-serif;font-size:12px}
.pill{display:flex;align-items:center;gap:7px}.dot{width:9px;height:9px;border-radius:50%;background:#8A8375}
.dot.up{background:#5FBF8F}.dot.down{background:#E8604B}
.tabs{display:flex;gap:2px;background:var(--panel);border-bottom:1px solid var(--line);padding:0 16px}
.tab{font-family:'Archivo',sans-serif;font-size:13px;font-weight:700;padding:13px 18px;border:none;background:transparent;color:var(--muted);cursor:pointer;border-bottom:3px solid transparent}
.tab.on{color:var(--ink);border-bottom-color:var(--accent)}
.wrap{max-width:980px;margin:0 auto;padding:24px 20px 60px}
.panel{display:none}.panel.on{display:block}
h2.disp{font-size:22px;font-weight:800;letter-spacing:-.4px;margin:0 0 4px}
p.lead{color:var(--muted);font-size:14px;margin:0 0 18px}
form{display:flex;gap:10px;margin-bottom:12px}
input[type=text]{flex-grow:1;padding:14px 15px;border:1px solid var(--line);background:var(--card);color:var(--ink);font-size:15px;font-family:'Spectral',serif;border-radius:9px}
button.go{font-family:'Archivo',sans-serif;font-weight:800;font-size:13px;text-transform:uppercase;letter-spacing:.5px;background:var(--accent);color:#fff;border:none;padding:0 26px;border-radius:9px;cursor:pointer}
.examples{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:18px}
.ex{font-family:'Archivo',sans-serif;font-size:12px;font-weight:600;padding:7px 12px;border:1px solid var(--line);background:transparent;color:var(--ink);border-radius:999px;cursor:pointer}
.ex:hover{border-color:var(--accent);color:var(--accent)}
.rcard{background:var(--card);border:1px solid var(--line);border-radius:12px;overflow:hidden;margin-top:6px}
.rh{background:var(--ink);color:var(--panel);padding:12px 18px;font-family:'Archivo',sans-serif;font-size:13px}
.chart{padding:12px 18px 6px}
.brow{display:flex;align-items:center;gap:10px;margin:6px 0}
.blabel{font-family:'Archivo',sans-serif;font-size:12px;width:240px;flex-shrink:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.btrack{flex-grow:1;background:var(--track);border-radius:4px;height:15px}.bfill{background:var(--bar);height:15px;border-radius:4px}
.bval{font-family:'JetBrains Mono',monospace;font-size:12px;font-weight:600;width:80px;text-align:right}
.twrap{padding:4px 18px 10px;overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:13px;font-family:'Archivo',sans-serif}
th{text-align:left;color:var(--muted);border-bottom:2px solid var(--line);padding:8px 12px 8px 0;white-space:nowrap}
td{padding:8px 12px 8px 0;border-bottom:1px solid var(--soft);white-space:nowrap}td.num{font-family:'JetBrains Mono',monospace}
details{margin:4px 18px 14px;border:1px solid var(--line);border-radius:8px;background:var(--panel)}
summary{cursor:pointer;padding:10px 13px;font-family:'Archivo',sans-serif;font-size:12px;font-weight:700;color:var(--muted)}
pre{margin:0;padding:0 13px 13px;font-family:'JetBrains Mono',monospace;font-size:12px;white-space:pre-wrap;color:var(--ink)}
.soon{padding:40px 24px;text-align:center;color:var(--muted);font-family:'Archivo',sans-serif;border:1px dashed var(--line);border-radius:12px}
.err{padding:16px 18px;color:var(--accent);font-family:'Archivo',sans-serif;font-size:14px}
.loading{padding:22px 18px;color:var(--muted);font-family:'Archivo',sans-serif}
</style></head><body>
<header>
  <div><div class="brand">TRANSIT<span>411</span></div><div class="sub">Command Center</div></div>
  <div class="status">
    <div class="pill"><span class="dot" id="apiDot"></span><span id="apiTxt">API…</span></div>
    <div class="pill"><span class="dot" id="dbDot"></span><span id="dbTxt">DB…</span></div>
  </div>
</header>
<div class="tabs">
  <button class="tab on" data-t="ask">Ask NTD</button>
  <button class="tab" data-t="collect">Collection</button>
  <button class="tab" data-t="sources">Sources</button>
  <button class="tab" data-t="publish">Publish</button>
</div>
<div class="wrap">
  <div class="panel on" id="p-ask">
    <h2 class="disp">Ask NTD</h2>
    <p class="lead">Plain-English questions over live NTD data — the query is generated, run read-only, and shown.</p>
    <form id="askForm"><input type="text" id="q" placeholder="e.g. cheapest heavy rail systems per rider" autocomplete="off"><button class="go" type="submit">Ask</button></form>
    <div class="examples" id="ex"></div>
    <div id="out"></div>
  </div>
  <div class="panel" id="p-collect"><div class="soon">Collection queue — phase 2b. Reads/writes the Postgres store; approvals publish to the site.</div></div>
  <div class="panel" id="p-sources"><div class="soon">Source registry — phase 2b. The watchlist that feeds the collection engine.</div></div>
  <div class="panel" id="p-publish"><div class="soon">Publishing & newsletter — phase 2b. Draft, schedule, and push approved items to the public site.</div></div>
</div>
<script>
const EX=["cheapest heavy rail systems per rider","compare light rail vs commuter rail cost per rider","highest ridership rail systems","most expensive bus systems per rider"];
const exWrap=document.getElementById("ex");
EX.forEach(t=>{const b=document.createElement("button");b.className="ex";b.textContent=t;b.onclick=()=>{document.getElementById("q").value=t;doAsk(t);};exWrap.appendChild(b);});

document.querySelectorAll(".tab").forEach(t=>t.onclick=()=>{
  document.querySelectorAll(".tab").forEach(x=>x.classList.toggle("on",x===t));
  document.querySelectorAll(".panel").forEach(p=>p.classList.toggle("on",p.id==="p-"+t.dataset.t));
});

async function refreshStatus(){
  try{
    const s=await (await fetch("/api/status")).json();
    setPill("api",s.api&&s.api.ok,s.api&&s.api.ok?("API · "+(s.api.rows!=null?s.api.rows.toLocaleString()+" rows":"ok")):"API down");
    setPill("db",s.db&&s.db.ok,s.db&&s.db.ok?("DB · "+(s.db.tables!=null?s.db.tables+" tables":"ok")):"DB down");
  }catch(e){setPill("api",false,"API down");setPill("db",false,"DB down");}
}
function setPill(k,up,txt){document.getElementById(k+"Dot").className="dot "+(up?"up":"down");document.getElementById(k+"Txt").textContent=txt;}

function isNum(v){return typeof v==="number"&&isFinite(v);}
function fmt(v){return isNum(v)?(Math.abs(v)>=1e9?"$"+(v/1e9).toFixed(2)+"B":Math.abs(v)>=1e6&&v%1===0?(v/1e6).toFixed(1)+"M":v.toLocaleString(undefined,{maximumFractionDigits:2})):v;}
function pickChartCol(cols,rows){ // last column that is numeric across rows = the metric asked about
  for(let i=cols.length-1;i>=0;i--){if(rows.every(r=>isNum(r[cols[i]])))return cols[i];}return null;}
function pickLabelCol(cols,rows){for(const c of cols){if(rows.some(r=>!isNum(r[c])))return c;}return cols[0];}

function render(res){
  const cols=res.columns||[],rows=res.rows||[];
  if(!rows.length){document.getElementById("out").innerHTML='<div class="rcard"><div class="err">No rows returned.</div></div>';return;}
  const metric=pickChartCol(cols,rows),lab=pickLabelCol(cols,rows);
  let chart="";
  if(metric){const max=Math.max(...rows.map(r=>r[metric]));chart='<div class="chart">'+rows.slice(0,15).map(r=>{
    const pct=Math.max(3,(r[metric]/max)*100);return '<div class="brow"><div class="blabel" title="'+r[lab]+'">'+r[lab]+'</div><div class="btrack"><div class="bfill" style="width:'+pct+'%"></div></div><div class="bval">'+fmt(r[metric])+'</div></div>';}).join("")+'</div>';}
  let th=cols.map(c=>"<th>"+c+"</th>").join("");
  let tb=rows.map(r=>"<tr>"+cols.map(c=>'<td class="'+(isNum(r[c])?"num":"")+'">'+fmt(r[c])+"</td>").join("")+"</tr>").join("");
  const head=res.question?("Q: "+res.question):"Result";
  const sql=res.sql?'<details><summary>View the query it ran ▾</summary><pre>'+res.sql.replace(/</g,"&lt;")+'</pre></details>':"";
  document.getElementById("out").innerHTML='<div class="rcard"><div class="rh">'+head+' · '+rows.length+' rows'+(metric?' · charting '+metric:'')+'</div>'+chart+'<div class="twrap"><table><thead><tr>'+th+'</tr></thead><tbody>'+tb+'</tbody></table></div>'+sql+'</div>';
}
async function doAsk(q){
  document.getElementById("out").innerHTML='<div class="rcard"><div class="loading">Reading “'+q+'” and running the query…</div></div>';
  try{
    const r=await fetch("/api/ask",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({question:q})});
    if(!r.ok){const t=await r.text();document.getElementById("out").innerHTML='<div class="rcard"><div class="err">'+t+'</div></div>';return;}
    render(await r.json());
  }catch(e){document.getElementById("out").innerHTML='<div class="rcard"><div class="err">Could not reach the API.</div></div>';}
}
document.getElementById("askForm").onsubmit=e=>{e.preventDefault();const v=document.getElementById("q").value.trim();if(v)doAsk(v);};
refreshStatus();setInterval(refreshStatus,15000);
</script></body></html>"""
