# Transit411 — Operations Manual

*A working reference for the whole system: what it is, how the pieces fit, where they run, and how to operate them. Written as of the current build.*

---

## 1. What Transit411 is

Transit411 is an independent transit-industry intelligence product with three revenue-oriented pillars:

- **Editorial** — news across Funding, Procurement, People, and Policy (the "wire" and a weekly newsletter).
- **Data tools** — plain-English search and dashboards over public transit data:
  - **Ask NTD** — natural-language querying of the National Transit Database (ridership, cost per rider, financials).
  - **CIG Pipeline** + **Ask CIG** — the FTA Capital Investment Grants pipeline (who's seeking funding, where they stand, what changed), searchable in plain English.
- **Marketplace (planned)** — paid job/procurement postings and paid "featured" People placements.

The guiding principle: **the public site is a thin reader of an engine that already exists.** All data and logic live behind an API; the website just displays it.

---

## 2. Architecture — the three planes

```
  YOUR PC (Claude Code)          QNAP NAS (private engine)            CLOUDFLARE (public)
  build · deploy · control  ->   Docker containers on the LAN    ->   Pages site + Tunnel
```

| Plane | Where | Role |
|---|---|---|
| **Control** | Your PC, via **Claude Code** | Writes code, commits, pushes to GitHub. Not a server. |
| **Private engine** | **QNAP NAS**, Container Station (Docker) | Postgres, NTD data, collectors, the Command Center, the read-only API, the tunnel. Holds all data and API keys. On the private LAN (192.168.1.x) + Tailscale. |
| **Public face** | **Cloudflare** | Pages hosts the static site; a Tunnel exposes a read-only slice of the API. The public only ever touches Cloudflare. |

**Golden rule:** public touches it → Cloudflare; your data/keys/pipeline → NAS; builds and controls it all → your PC.

**Repo:** `github.com/bbuchanan99/transit411` (public). It is the single source of truth. Claude Code commits/pushes; the NAS deploys from it; Cloudflare Pages builds the site from it.

---

## 3. Components

### 3.1 NTD data engine + Ask NTD
- **`build_db.py`** — pulls the National Transit Database (Socrata dataset `npsm-38gk`, service + operating expenses by mode) into a **DuckDB** file (`ntd.duckdb`), one row per agency+mode (+year if present), computing cost-per-rider.
- **`query.py`** — read-only SQL guardrails, dynamic schema introspection, and `nl_to_sql()` (conversation-aware: follow-ups modify the previous query).
- **`serve.py`** — the NTD API service (`/health`, `/sql`, `/ask`). Ask NTD's answers show the generated SQL for trust.

### 3.2 Collection engine
- **`collection.py`** — the content collector. Sources include RSS feeds **and Google News keyword searches** (which route around sites that block bots). For each new item it calls the model to produce a pillar, rewritten headline, paraphrased summary, relevance, and metadata facets (agencies, mode, programs, state, tags); computes a freshness score; and writes it to Postgres as `pending`.
- **Freshness model** — three clocks: news decays fast (~3-day half-life), deadline-driven items stay **Live** until their date then **Expire**, developing stories decay slowly.
- Runs on demand (`--run`) or on a daily schedule (the `scheduler` service), and can `--seed` sources, `--migrate` schema, and `--backfill` facets.

### 3.3 Content pipeline (collect → review → publish)
- **`collected_items`** (Postgres) — the review queue. The Command Center's **Collection** tab shows pending items with freshness; you approve/skip.
- **`content_posts`** (Postgres) — published posts (what the public site reads). The **Publish** tab turns approved items into posts (paraphrased summary + source link — copyright-safe).
- **Metadata facets** — posts carry `agencies`, `mode`, `programs`, `tags`, `state` (Postgres arrays, GIN-indexed) so the site can filter by section and cross-cut.
- **Featured / paid People** — `featured`, `featured_until`, `sponsor` columns + a feature endpoint, for paid highlight placements.

### 3.4 CIG pipeline + Ask CIG
- **`cig.py`** — parses the monthly **FTA CIG Dashboard PDF** (by column position; validated against the real dashboard) into `cig_projects`. **Versioned by `snapshot_date`** — every month is kept, so phase advances, rating changes, and cost drift are recoverable. Full milestone dates captured (PD entry, NEPA, Engineering, LONP, rating dates, estimated grant).
  - Load a month: `--latest` (newest), `--url <pdf>` (a specific/archived month, for backfill), or `--file <pdf>`.
- **API** — `/api/cig` (latest snapshot, filterable), `/api/cig/history` (a project's month-by-month trajectory), plus change-detection and load-history endpoints.
- **Ask CIG** — `/api/cig/ask`: plain-English → read-only SQL over `cig_projects` (its own Command Center tab).
- The **Grants** tab renders the pipeline; clicking a project expands its milestones and snapshot history.

### 3.5 Command Center (private dashboard)
- **`command_center.py`** — the internal home base, served on the NAS LAN at **`http://<nas-ip>:8088`** (8088 because QNAP's admin UI owns 8080).
- Tabs: **Ask NTD**, **Collection**, **Sources**, **Publish**, **Grants**, **Ask CIG**. Plus a live status bar (API rows, DB tables).
- This is where you approve, publish, feature, run collectors, upload CIG dashboards, and QA. **It is never exposed publicly.**

### 3.6 Shared component bundle
- **`static/t411.js`** + **`static/t411.css`** — framework-free UI components (`renderCigTable` with expandable milestone/timeline rows, `renderCigMilestones`, `renderCigTimeline`) that use the host page's CSS variables. **Loaded by both the Command Center and the public site**, so views are built once and used everywhere.

### 3.7 Public site (Astro)
- **`/site`** — an **Astro** static site deploying to **Cloudflare Pages**. Design is **"Dispatch"** (bold editorial newspaper: near-black + red, Archivo + Spectral).
- Pages: homepage (`index.astro`), section pages (`[section].astro` → News/Funding/Procurement/People/Policy), a **Data hub** (`data.astro`).
- **`src/lib/content.js`** — central content source. Currently returns **sample content** so the site builds before the API is wired; swapping to live data (`${PUBLIC_API_BASE}/api/posts`) is a one-function change (the live version is written in a comment there).
- Lives at **`transit411.pages.dev`** (domain stays dark until launch).

### 3.8 Read-only public API + Cloudflare Tunnel
- **`readonly_api.py`** — the ONLY service the tunnel exposes. It **allowlists** exactly the safe endpoints (`GET /api/posts`, `GET /api/cig`, `GET /api/cig/history`, `POST /api/ask`, `POST /api/cig/ask`) and forwards them to the Command Center; **everything else returns 404.** CORS restricts browser calls to the Transit411 site (`*.transit411.pages.dev`, `transit411.net`, `transit411.com`).
- **`cloudflared`** — the tunnel container. Dials out to Cloudflare (no open router ports). Public hostname **`api.transit411.net`** → `http://readonly-api:8000`.
- **Data path:** internet → Cloudflare → tunnel → `readonly-api` (5 safe endpoints) → Command Center. No path from the public to any write/admin action.

---

## 4. Data stores

**Postgres** (`db` service, `pgvector/pgvector:pg16`), database `transit411`:
- `sources` — the collector's watchlist (feeds + keyword searches).
- `collected_items` — the review queue (pending/approved/skipped/published/filtered) with facets + embedding column.
- `content_posts` — published posts the site reads (title, body, pillar, facets, featured/sponsor, `item_id` link).
- `cig_projects` — the CIG pipeline, versioned by `snapshot_date`, with milestone dates.
- `subscribers` — newsletter list (schema present).
- `app_settings` — small key/values (e.g., the auto-collect toggle).

**DuckDB** — `ntd.duckdb` (file on a NAS volume): `ntd_service` table for the NTD data behind Ask NTD.

---

## 5. Docker services (docker-compose.yml)

| Service | Type | Purpose |
|---|---|---|
| `db` | always-on | Postgres + pgvector (content store) |
| `api` | always-on | NTD API (`serve.py`) — Ask NTD, port 8000 (LAN) |
| `command-center` | always-on | Private dashboard, port **8088** (LAN) |
| `readonly-api` | always-on | Public-safe API proxy (internal only; tunnel target) |
| `cloudflared` | always-on | Cloudflare Tunnel → `api.transit411.net` |
| `scheduler` | always-on | Daily collector run |
| `ingest` | on-demand | `build_db.py` — (re)build the NTD DuckDB |
| `collect` | on-demand | `collection.py` — run the collector / seed / migrate |
| `cig` | on-demand | `cig.py` — load a CIG dashboard snapshot |

On-demand services use the `tools` profile: run with `docker compose run --rm <service> <args>`.

---

## 6. Runbook — common operations

All run on the NAS (via Claude Code or SSH), from the app folder.

**Bring the stack up / update after a pull:**
```
git pull
docker compose build
docker compose up -d
```

**Refresh NTD data:**
```
docker compose run --rm ingest        # rebuilds ntd.duckdb from data.transportation.gov
```

**Run the collector (populate the review queue):**
```
docker compose run --rm collect --seed          # once: load sources
docker compose run --rm collect --run --limit 8 # fetch feeds/searches
```

**Load a CIG dashboard snapshot:**
```
docker compose run --rm cig --latest            # newest monthly dashboard
docker compose run --rm cig --url <pdf-url>     # a specific/archived month (backfill history)
```

**Everyday content work:** open `http://<nas-ip>:8088` → Collection (approve) → Publish.

**Bring up the tunnel / public API:**
```
docker compose up -d readonly-api cloudflared
docker compose logs cloudflared          # expect "Registered tunnel connection"
curl https://api.transit411.net/health   # expect {"ok":true,"service":"readonly-api"}
```

**Verify the security boundary:**
```
curl https://api.transit411.net/api/publish/1/approve   # must return 404
```

---

## 7. Deploy workflow

- **Code:** built here (chat) → delivered as a **git patch** → Claude Code applies (`git apply <patch>`), commits, pushes.
- **Repo private?** No — public, so Cloudflare Pages can build it and the chat can read it. Secrets stay out via `.gitignore`.
- **NAS deploy:** `git pull` on the NAS, then `docker compose build && up -d`.
- **Site deploy:** every push to the repo triggers **Cloudflare Pages** to rebuild `/site`.
  - Pages build settings: **Root directory** `site`, **Build command** `npm install && npm run build`, **Output directory** `dist`.
- **Line endings:** a `.gitattributes` pins LF to avoid cross-platform churn.

---

## 8. Configuration (.env on the NAS — never committed)

| Var | Purpose |
|---|---|
| `ANTHROPIC_API_KEY` | Model calls (collection classify, Ask NTD/CIG SQL generation) |
| `ASK_NTD_MODEL` | Model for NL→SQL (default `claude-haiku-4-5`) |
| `DB_PASSWORD` | Postgres password (`openssl rand -hex 16`) |
| `DATA_DIR` | NAS path for data volumes (DuckDB, Postgres) |
| `COLLECT_AT` / `COLLECT_TZ` | Daily collector schedule |
| `CLOUDFLARE_TUNNEL_TOKEN` | Cloudflare Tunnel token |

The public site uses `PUBLIC_API_BASE` (e.g. `https://api.transit411.net`) — set in the site build when wiring live data.

---

## 9. Networking & endpoints

- **Command Center (private):** `http://<nas-ip>:8088` (LAN / Tailscale only).
- **NTD API (private):** `http://<nas-ip>:8000`.
- **Public API:** `https://api.transit411.net` (tunnel → read-only API).
- **Public site (dev):** `https://transit411.pages.dev`.
- **Domain:** `transit411.net` — Active in Cloudflare, **root kept dark** until launch (a leftover GoDaddy `A` record / email CNAMEs remain; don't delete the email/MX records).
- **Tailscale:** private admin access to the NAS from anywhere.

---

## 10. Current status

**Working:**
- NTD engine + Ask NTD (with conversational follow-ups). ✅
- Collection engine (RSS + Google News keyword search), review queue, freshness scoring. ✅
- Content pipeline: collect → approve → publish, with facets + featured mechanic. ✅
- CIG pipeline: versioned snapshots, milestones, history, change detection, load history; Grants tab; Ask CIG. ✅
- Command Center dashboard (all tabs). ✅
- Shared component bundle. ✅
- Public Astro site on Cloudflare Pages (homepage + sections + data hub, **sample content**). ✅
- Read-only public API + Cloudflare Tunnel, secured by allowlist; `api.transit411.net` live. ✅

**Pending / next:**
- Wire the site to **live data** (`PUBLIC_API_BASE=https://api.transit411.net`): real published posts on homepage/sections; build the CIG pipeline page and Ask NTD/CIG pages using the shared bundle.
- Individual article pages; About; newsletter capture wired to an ESP (Beehiiv).
- Backfill more CIG monthly PDFs to enrich history/timelines.

---

## 11. Roadmap (logged product to-dos)

- **Branded output templates** — consistent chart/table/export styling and shareable "data cards"; the basis for the Ask NTD/CIG revenue product.
- **Ask NTD revenue subsite** — `ask.transit411.*` with freemium tiers, API/bulk data access, branded reports.
- **"Ask ___" brand family** — Ask NTD, Ask Transit, room to expand.
- **Marketplace** — paid job/procurement postings; paid featured People placements.
- **Newsletter** — the retention engine (weekly intelligence).
- **Launch** — point `transit411.net` root → Pages, drop any access gate, go public.

---

## 12. Security notes

- The **read-only API allowlist** is the public boundary — only 5 GET/ask endpoints are reachable; all writes are 404. Keep it tight if adding public endpoints.
- **Secrets live only in `.env`** on the NAS (git-ignored). Never commit keys or tokens.
- **Regenerate the Cloudflare Tunnel token** if it's ever exposed (Zero Trust → Tunnels → refresh token).
- The **Command Center is LAN/Tailscale only** — never expose it through the tunnel.
- Model-facing SQL (Ask NTD/CIG) is **read-only** (SELECT-only guards + read-only transaction).

---

*End of manual. Keep this in the repo (e.g. `docs/MANUAL.md`) so it travels with the project and updates as the system grows.*
