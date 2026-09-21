#!/usr/bin/env python3
"""
Transit411 Command Center - the private home-base dashboard (LAN only).
  uvicorn command_center:app --host 0.0.0.0 --port 8080
Env: API_URL (default http://api:8000), DATABASE_URL (default postgresql://transit411:transit411@db:5432/transit411)
"""
import os
from typing import List, Optional
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


class Turn(BaseModel):
    question: Optional[str] = None
    sql: Optional[str] = None


class Ask(BaseModel):
    question: str
    history: Optional[List[Turn]] = None


@app.post("/api/ask")
def ask(a: Ask):
    payload = {"question": a.question, "history": [t.model_dump() for t in (a.history or [])]}
    try:
        r = httpx.post(f"{API_URL}/ask", json=payload, timeout=60)
    except httpx.HTTPError as e:
        raise HTTPException(502, f"API unreachable: {e}")
    if r.status_code != 200:
        raise HTTPException(r.status_code, r.text)
    return r.json()


def _db():
    if not psycopg:
        raise HTTPException(500, "psycopg not installed")
    return psycopg.connect(DATABASE_URL, connect_timeout=5)


@app.get("/api/collection")
def collection(status: str = "pending"):
    from collection import freshness
    items = []
    try:
        with _db() as c, c.cursor() as cur:
            cur.execute(
                "SELECT id, pillar, headline, summary, source_name, source_url, published, deadline, relevance, status "
                "FROM collected_items WHERE status=%s ORDER BY collected_at DESC LIMIT 200", (status,))
            names = [d[0] for d in cur.description]
            for row in cur.fetchall():
                it = dict(zip(names, row))
                st, sc = freshness(it.get("published"), it.get("deadline"))
                it["fresh_status"], it["fresh_score"] = st, sc
                it["published"] = it["published"].isoformat() if it.get("published") else None
                it["deadline"] = it["deadline"].isoformat() if it.get("deadline") else None
                items.append(it)
            cur.execute("SELECT status, count(*) FROM collected_items GROUP BY status")
            counts = {row[0]: row[1] for row in cur.fetchall()}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"DB error: {e}")
    items.sort(key=lambda x: x["fresh_score"], reverse=True)
    return {"items": items, "counts": counts}


@app.post("/api/collection/{item_id}/{action}")
def collection_action(item_id: int, action: str):
    mapping = {"approve": "approved", "skip": "skipped", "publish": "published", "reset": "pending"}
    if action not in mapping:
        raise HTTPException(400, "unknown action")
    try:
        with _db() as c, c.cursor() as cur:
            cur.execute("UPDATE collected_items SET status=%s WHERE id=%s", (mapping[action], item_id))
            c.commit()
    except Exception as e:
        raise HTTPException(502, f"DB error: {e}")
    return {"id": item_id, "status": mapping[action]}


@app.get("/", response_class=HTMLResponse)
def home():
    return DASHBOARD


DASHBOARD = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Transit411 - Command Center</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Archivo:wght@600;700;800;900&family=Spectral:wght@400;500;600&family=JetBrains+Mono:wght@400;600&display=swap">
<style>
:root{--bg:#F2EEE4;--panel:#F7F4ED;--card:#FFF;--ink:#17140F;--muted:#6A6458;--line:#D8D2C4;--soft:#E7E1D4;--accent:#C0341F;--ok:#1F6B4A;--bar:#C0341F;--track:#EDE7D8}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font-family:'Spectral',Georgia,serif}
.disp{font-family:'Archivo',sans-serif}
header{background:var(--ink);color:var(--panel);padding:16px 24px;display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:12px}
.brand{font-family:'Archivo',sans-serif;font-weight:900;font-size:22px;letter-spacing:-1px}.brand span{color:var(--accent)}
.sub{font-family:'Archivo',sans-serif;font-size:10px;letter-spacing:2.5px;text-transform:uppercase;color:#A69F90}
.status{display:flex;gap:16px;font-family:'Archivo',sans-serif;font-size:12px}
.pill{display:flex;align-items:center;gap:7px}.dot{width:9px;height:9px;border-radius:50%;background:#8A8375}.dot.up{background:#5FBF8F}.dot.down{background:#E8604B}
.tabs{display:flex;gap:2px;background:var(--panel);border-bottom:1px solid var(--line);padding:0 16px}
.tab{font-family:'Archivo',sans-serif;font-size:13px;font-weight:700;padding:13px 18px;border:none;background:transparent;color:var(--muted);cursor:pointer;border-bottom:3px solid transparent}
.tab.on{color:var(--ink);border-bottom-color:var(--accent)}
.wrap{max-width:980px;margin:0 auto;padding:24px 20px 60px}
.panel{display:none}.panel.on{display:block}
h2.disp{font-size:22px;font-weight:800;letter-spacing:-.4px;margin:0 0 4px}
p.lead{color:var(--muted);font-size:14px;margin:0 0 16px}
.askhead{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:10px}
.follow{font-family:'Archivo',sans-serif;font-size:12px;font-weight:700;color:var(--accent);display:none}
.follow.on{display:inline}
.newq{font-family:'Archivo',sans-serif;font-size:12px;font-weight:800;text-transform:uppercase;letter-spacing:.4px;background:transparent;border:1px solid var(--line);color:var(--muted);padding:8px 14px;border-radius:8px;cursor:pointer}
form{display:flex;gap:10px;margin-bottom:10px}
input[type=text]{flex-grow:1;padding:14px 15px;border:1px solid var(--line);background:var(--card);color:var(--ink);font-size:15px;font-family:'Spectral',serif;border-radius:9px}
button.go{font-family:'Archivo',sans-serif;font-weight:800;font-size:13px;text-transform:uppercase;letter-spacing:.5px;background:var(--accent);color:#fff;border:none;padding:0 26px;border-radius:9px;cursor:pointer}
.examples{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:16px}
.ex{font-family:'Archivo',sans-serif;font-size:12px;font-weight:600;padding:7px 12px;border:1px solid var(--line);background:transparent;color:var(--ink);border-radius:999px;cursor:pointer}.ex:hover{border-color:var(--accent);color:var(--accent)}
.rcard{background:var(--card);border:1px solid var(--line);border-radius:12px;overflow:hidden;margin-bottom:14px}
.rh{background:var(--ink);color:var(--panel);padding:11px 18px;font-family:'Archivo',sans-serif;font-size:13px}
.rh .fu{color:#EE6A54;font-size:10px;font-weight:800;letter-spacing:1px;text-transform:uppercase;margin-right:8px}
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
.loading{padding:20px 18px;color:var(--muted);font-family:'Archivo',sans-serif}
</style></head><body>
<header>
  <div><div class="brand">TRANSIT<span>411</span></div><div class="sub">Command Center</div></div>
  <div class="status">
    <div class="pill"><span class="dot" id="apiDot"></span><span id="apiTxt">API...</span></div>
    <div class="pill"><span class="dot" id="dbDot"></span><span id="dbTxt">DB...</span></div>
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
    <div class="askhead"><div><h2 class="disp">Ask NTD</h2><p class="lead">Ask, then keep asking - follow-ups like "what about Texas?" build on your last question. Hit New question to start fresh.</p></div></div>
    <form id="askForm"><input type="text" id="q" placeholder="Ask a question, then follow up..." autocomplete="off"><button class="go" type="submit">Ask</button></form>
    <div class="askhead"><div class="examples" id="ex"></div><div><span class="follow" id="follow">Following your thread</span> <button class="newq" id="newq" type="button">New question</button></div></div>
    <div id="out"></div>
  </div>
  <div class="panel" id="p-collect">
    <div class="askhead"><div><h2 class="disp">Collection queue</h2><p class="lead">Items the engine gathered, freshest first - approve what runs, skip the rest. Populate with the collector job.</p></div><button class="newq" id="cRefresh" type="button">Refresh</button></div>
    <div class="examples" id="cChips"></div>
    <div id="cOut"></div>
  </div>
  <div class="panel" id="p-sources"><div class="soon">Source registry - phase 2b. The watchlist that feeds the collection engine.</div></div>
  <div class="panel" id="p-publish"><div class="soon">Publishing & newsletter - phase 2b. Draft, schedule, push approved items to the public site.</div></div>
</div>
<script>
let thread=[];  // [{question, sql, columns, rows}]
const EX=["ridership trend by mode","cheapest heavy rail systems per rider","highest ridership rail systems","most expensive bus systems per rider"];
const exWrap=document.getElementById("ex");
EX.forEach(t=>{const b=document.createElement("button");b.className="ex";b.textContent=t;b.onclick=()=>{document.getElementById("q").value=t;doAsk(t);};exWrap.appendChild(b);});

document.querySelectorAll(".tab").forEach(t=>t.onclick=()=>{
  document.querySelectorAll(".tab").forEach(x=>x.classList.toggle("on",x===t));
  document.querySelectorAll(".panel").forEach(p=>p.classList.toggle("on",p.id==="p-"+t.dataset.t));
});
document.getElementById("newq").onclick=()=>{thread=[];document.getElementById("out").innerHTML="";document.getElementById("follow").classList.remove("on");document.getElementById("q").focus();};

async function refreshStatus(){
  try{const s=await (await fetch("/api/status")).json();
    setPill("api",s.api&&s.api.ok,s.api&&s.api.ok?("API "+(s.api.rows!=null?s.api.rows.toLocaleString()+" rows":"ok")):"API down");
    setPill("db",s.db&&s.db.ok,s.db&&s.db.ok?("DB "+(s.db.tables!=null?s.db.tables+" tables":"ok")):"DB down");
  }catch(e){setPill("api",false,"API down");setPill("db",false,"DB down");}
}
function setPill(k,up,txt){document.getElementById(k+"Dot").className="dot "+(up?"up":"down");document.getElementById(k+"Txt").textContent=txt;}

// Everything from the API, the model or the database is untrusted text: escape it before it
// goes into innerHTML (phase 2b will show headlines scraped from other sites).
function esc(v){return String(v??"").replace(/[&<>"']/g,ch=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[ch]));}
function isNum(v){return typeof v==="number"&&isFinite(v);}
// Format by what the column holds (same rules as the Ask NTD page), not by the size of the number.
function kind(c,rows){
  const n=c.toLowerCase(),vals=rows.map(r=>r[c]).filter(v=>v!==null&&v!==undefined);
  if(!vals.length||!vals.every(isNum))return "text";
  if(/(^|_)(year|yr)$|^year/.test(n))return "year";
  if(/(^|_)id$/.test(n))return "text";
  if(/percent|pct/.test(n))return "pct";
  if(/recovery|ratio/.test(n))return "ratio";
  if(/factor|cpi/.test(n))return "num";
  if(/cost|expense|opex|fare|dollar|spend|price|cpr|_real/.test(n))return "money";
  return "num";
}
function fmt(v,k){
  if(v===null||v===undefined)return "—";
  if(k==="text"||!isNum(v))return String(v);
  if(k==="year")return String(Math.round(v));
  if(k==="pct")return v.toFixed(1)+"%";
  if(k==="ratio")return (v*100).toFixed(1)+"%";
  const a=Math.abs(v);let s;
  if(a>=1e9)s=(a/1e9).toFixed(2)+"B";
  else if(a>=1e6)s=(a/1e6).toFixed(1)+"M";
  else if(Number.isInteger(v)||a>=1000)s=Math.round(a).toLocaleString();
  else s=a.toLocaleString(undefined,{minimumFractionDigits:k==="money"?2:0,maximumFractionDigits:2});
  return (v<0?"-":"")+(k==="money"?"$":"")+s;
}
function pickChartCol(cols,kinds){ // last numeric column (not a year or ID) = the metric asked about
  for(let i=cols.length-1;i>=0;i--){if(!["text","year"].includes(kinds[cols[i]]))return cols[i];}return null;}
function pickLabel(cols,rows,kinds){ // a readable name, never an ID; add mode/year when they tell rows apart
  const text=cols.filter(c=>kinds[c]==="text"&&!/(^|_)id$|^ntd_id$|mode_code/.test(c.toLowerCase()));
  const varies=c=>new Set(rows.map(r=>r[c])).size>1;
  const first=["agency","name","mode","state","city"].find(p=>text.includes(p))||text[0];
  const parts=first?[first]:[];
  if(first&&first!=="mode"&&text.includes("mode")&&varies("mode"))parts.push("mode");
  const yr=cols.find(c=>kinds[c]==="year");
  if(yr&&varies(yr))parts.push(yr);
  if(!parts.length)return null;
  return r=>parts.map(c=>fmt(r[c],kinds[c])).join(" · ");
}

function cardHtml(res,isFollow){
  const cols=res.columns||[],rows=res.rows||[];
  const tag=(isFollow?'<span class="fu">follow-up</span>':'')+'Q: '+esc(res.question);
  if(!rows.length)return '<div class="rcard"><div class="rh">'+tag+'</div><div class="err">No rows returned.</div></div>';
  const kinds=Object.fromEntries(cols.map(c=>[c,kind(c,rows)]));
  const metric=pickChartCol(cols,kinds),label=pickLabel(cols,rows,kinds);
  let chart="";
  const plotted=metric&&label?rows.filter(r=>isNum(r[metric])):[];
  if(plotted.length){const max=Math.max(...plotted.map(r=>Math.abs(r[metric])))||1;chart='<div class="chart">'+plotted.slice(0,15).map(r=>{
    const pct=Math.max(3,(Math.abs(r[metric])/max)*100),l=esc(label(r));return '<div class="brow"><div class="blabel" title="'+l+'">'+l+'</div><div class="btrack"><div class="bfill" style="width:'+pct+'%"></div></div><div class="bval">'+esc(fmt(r[metric],kinds[metric]))+'</div></div>';}).join("")+'</div>';}
  const th=cols.map(c=>"<th>"+esc(c)+"</th>").join("");
  const tb=rows.map(r=>"<tr>"+cols.map(c=>'<td class="'+(kinds[c]==="text"?"":"num")+'">'+esc(fmt(r[c],kinds[c]))+"</td>").join("")+"</tr>").join("");
  const sql=res.sql?'<details><summary>View the query it ran</summary><pre>'+esc(res.sql)+'</pre></details>':"";
  return '<div class="rcard"><div class="rh">'+tag+' &middot; '+rows.length+' rows'+(chart?' &middot; charting '+esc(metric):'')+'</div>'+chart+'<div class="twrap"><table><thead><tr>'+th+'</tr></thead><tbody>'+tb+'</tbody></table></div>'+sql+'</div>';
}
// The API's {"detail": ...} arrives wrapped once more by this server's proxy; unwrap to the message.
function errText(t){for(let i=0;i<2;i++){try{const d=JSON.parse(t).detail;t=typeof d==="string"?d:(d&&d.error)||JSON.stringify(d);}catch(_){break;}}return t;}
function renderThread(){document.getElementById("out").innerHTML=thread.map((r,i)=>cardHtml(r,i>0)).join("");
  document.getElementById("follow").classList.toggle("on",thread.length>0);
  window.scrollTo(0,document.body.scrollHeight);}

async function doAsk(q){
  const isFollow=thread.length>0;
  document.getElementById("out").insertAdjacentHTML("beforeend",'<div class="rcard" id="pending"><div class="loading">Reading "'+esc(q)+'"'+(isFollow?" (in context)":"")+' and running the query...</div></div>');
  window.scrollTo(0,document.body.scrollHeight);
  try{
    const r=await fetch("/api/ask",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({question:q,history:thread.map(t=>({question:t.question,sql:t.sql}))})});
    const pend=document.getElementById("pending"); if(pend)pend.remove();
    if(!r.ok){const t=await r.text();document.getElementById("out").insertAdjacentHTML("beforeend",'<div class="rcard"><div class="err">'+esc(errText(t))+'</div></div>');return;}
    const res=await r.json();
    thread.push({question:res.question||q,sql:res.sql,columns:res.columns,rows:res.rows});
    renderThread();
    document.getElementById("q").value="";
  }catch(e){const pend=document.getElementById("pending");if(pend)pend.remove();document.getElementById("out").insertAdjacentHTML("beforeend",'<div class="rcard"><div class="err">Could not reach the API.</div></div>');}
}
document.getElementById("askForm").onsubmit=e=>{e.preventDefault();const v=document.getElementById("q").value.trim();if(v)doAsk(v);};
// ---- Collection tab ----
let cFilter="pending";
const cChips=document.getElementById("cChips");
[["pending","Pending"],["approved","Approved"],["skipped","Skipped"],["published","Published"]].forEach(([k,lbl])=>{
  const b=document.createElement("button");b.className="ex";b.textContent=lbl;
  b.onclick=()=>{cFilter=k;document.querySelectorAll("#cChips .ex").forEach(x=>x.style.borderColor=(x===b?"var(--accent)":""));loadCollection();};
  if(k==="pending")b.style.borderColor="var(--accent)";cChips.appendChild(b);});
document.getElementById("cRefresh").onclick=loadCollection;
document.querySelector('.tab[data-t="collect"]').addEventListener("click",loadCollection);
const FCOLOR={Live:"#C0341F",Fresh:"#1F6B4A",Recent:"#1F6B7A",Aging:"#B07A1E",Stale:"#6A6458",Developing:"#3D5C8F",Expired:"#6A6458"};
function esc(s){return (s==null?"":String(s)).replace(/[&<>]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]));}
async function loadCollection(){
  const out=document.getElementById("cOut");
  out.innerHTML='<div class="rcard"><div class="loading">Loading the queue...</div></div>';
  try{
    const r=await fetch("/api/collection?status="+cFilter);
    if(!r.ok){out.innerHTML='<div class="rcard"><div class="err">'+esc(await r.text())+'</div></div>';return;}
    const d=await r.json();
    if(!d.items||!d.items.length){out.innerHTML='<div class="rcard"><div class="loading">Nothing '+cFilter+'. Populate with: docker compose run --rm collect --run</div></div>';return;}
    out.innerHTML=d.items.map(cCard).join("");
  }catch(e){out.innerHTML='<div class="rcard"><div class="err">Could not reach the queue.</div></div>';}
}
function cCard(it){
  const fc=FCOLOR[it.fresh_status]||"#6A6458";const acted=cFilter!=="pending";
  return '<div class="rcard" style="padding:16px 18px">'
    +'<div style="display:flex;gap:10px;align-items:center;margin-bottom:8px;font-family:Archivo,sans-serif;font-size:10px;font-weight:800;letter-spacing:1px;text-transform:uppercase">'
    +'<span style="color:var(--accent)">'+esc(it.pillar)+'</span>'
    +'<span style="color:'+fc+';border:1px solid '+fc+';border-radius:999px;padding:2px 8px">'+esc(it.fresh_status)+'</span>'
    +'<span style="color:var(--muted)">'+esc(it.relevance)+' relevance</span></div>'
    +'<div style="font-family:Archivo,sans-serif;font-weight:700;font-size:17px;line-height:1.3;margin-bottom:6px">'+esc(it.headline)+'</div>'
    +'<div style="font-size:14px;margin-bottom:10px">'+esc(it.summary)+'</div>'
    +'<div style="font-family:Archivo,sans-serif;font-size:12px;color:var(--muted);display:flex;gap:10px;flex-wrap:wrap;align-items:center">'
    +'<b style="color:var(--ink)">'+esc(it.source_name)+'</b>'+(it.published?'<span>'+esc(it.published)+'</span>':'')
    +(it.source_url?'<a href="'+esc(it.source_url)+'" target="_blank" rel="noopener">source</a>':'')+'</div>'
    +'<div style="display:flex;gap:8px;margin-top:12px">'
    +(acted?'<button class="ex" data-id="'+it.id+'" data-act="reset">Return to pending</button>'
           :'<button class="go" style="padding:9px 18px" data-id="'+it.id+'" data-act="approve">Approve</button><button class="ex" data-id="'+it.id+'" data-act="skip">Skip</button>')
    +'</div></div>';
}
async function cAct(id,action){try{await fetch("/api/collection/"+id+"/"+action,{method:"POST"});loadCollection();}catch(e){}}
document.getElementById("cOut").addEventListener("click",e=>{const b=e.target.closest("[data-act]");if(b)cAct(b.dataset.id,b.dataset.act);});
refreshStatus();setInterval(refreshStatus,15000);
</script></body></html>"""
