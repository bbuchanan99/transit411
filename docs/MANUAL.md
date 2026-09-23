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
- **`build_db.py`** — pulls the National Transit Database (Socrata dataset `npsm-38gk`, service + operating expenses by mode, report years 2015 onward) into a **DuckDB** file (`ntd.duckdb`) with three tables: `ntd_service` (latest year, one row per agency+mode), `ntd_history` (every year, with fares, passenger miles, revenue miles/hours and derived ratios; dollar columns both raw and CPI-U inflation-adjusted as `*_real`), and `cpi` (the adjustment factors). Add each new year's CPI-U average to `CPI_U` in `build_db.py`.
- **`query.py`** — read-only SQL guardrails, a detailed schema description (tables, raw vs. inflation-adjusted columns, an acronym → NTD ID map for ~45 large systems, worked examples), and `nl_to_sql()` (conversation-aware: follow-ups modify the previous query).
- **`serve.py`** — the NTD API service (`/health`, `/sql`, `/ask`) plus a plain-English question page at `/`. Ask NTD's answers show the generated SQL for trust.

### 3.2 Collection engine
- **`collection.py`** — the content collector. Sources include RSS feeds **and Google News keyword searches**. Sites that refuse automated requests (FTA, Railway Age, APTA, Eno) are marked `Manual` and skipped rather than worked around; the keyword searches surface their coverage through Google News instead. Low-relevance items are stored as `filtered` so they're never re-triaged, and agency names are normalized to one standard public name (`AGENCY_ALIASES`; re-apply with `--normalize`). For each new item it calls the model to produce a pillar, rewritten headline, paraphrased summary, relevance, and metadata facets (agencies, mode, programs, state, tags); computes a freshness score; and writes it to Postgres as `pending`.
- **Freshness model** — three clocks: news decays fast (~3-day half-life), deadline-driven items stay **Live** until their date then **Expire**, developing stories decay slowly.
- **Sources tab** (Command Center only) edits the `sources` table: feeds and Google News keyword searches, with pillar, type, method, trust and notes.
  - **Controls:** add, edit, delete, switch sources on and off, and **Test** a feed, which fetches it once with no model call and nothing queued.
  - **Health:** each run records every source's last fetch, last success or error (with the failure streak), and its in-feed, new and queued counts, next to the items it has produced.
  - **When changes apply:** the collector reads the table fresh each run (enabled RSS sources that aren't deleted), so changes apply to the next run. `collect --list-sources` shows what that run will fetch.
  - **Agency searches:** `agencies.py --searches` creates one daily Google News search per search-enabled agency (type `Agency`, query `"<agency>" transit when:2d`), shown as its own group in the tab. They are created **switched off** (`--activate` to switch on), so no run happens on searches nobody has reviewed. On as of 2026-09-22: MBTA, LA Metro, WMATA, CTA, MTA (New York), SEPTA, BART, Sound Transit — the next run fetches 20 sources, up from 12. Measured volume: all 17 candidate agencies returned 125 entries over a 2-day window (~60–70 new links/day, one classify call each); the 8 enabled are roughly 30/day.
  - **The registry in code:** `SOURCES` / `SEARCH_QUERIES` in `collection.py` still seed the table, but `--seed` leaves alone any source edited, added or deleted in the tab. A deleted registry source is kept as a marker so it isn't re-added.
  - **Renames:** renaming a source relabels its collected items, so its counts carry over.
- Runs on demand (`--run`) or on a daily schedule (the `scheduler` service; the Command Center's **Auto-collect** switch turns the daily run off to save model tokens), and can `--seed` sources, `--migrate` schema, `--backfill` facets, and `--normalize` agency names.

### 3.3 Content pipeline (collect → review → publish)
- **`collected_items`** (Postgres) — the review queue. The Command Center's **Collection** tab shows pending items with freshness; you approve/skip.
- **`content_posts`** (Postgres) — published posts (what the public site reads). The **Publish** tab turns approved items into posts (paraphrased summary + source link — copyright-safe).
- **Metadata facets** — posts carry `agencies`, `mode`, `programs`, `tags`, `state` (Postgres arrays, GIN-indexed) so the site can filter by section and cross-cut.
- **Article pages** — every published post has its own page at `/article/<slug>`, pre-rendered at build from `GET /api/posts` (`getStaticPaths`). It carries the pillar, headline, date, **Transit411's paraphrase only**, the metadata chips, a prominent link out to the source, and SEO tags (canonical, Open Graph, Twitter). Every headline on the site links here; the source link-out lives on the page (lists keep a small "source" link). `GET /api/posts/{slug}` returns one post.
  - **Explore module** (built at build time; no model calls, no client-side fetching): related coverage (internal links scored by shared agency/program/pillar/state), the story's agency in the CIG pipeline (matched through `agencies.json` aliases), "go deeper" links to `/cig?agency=…` and to Ask NTD / Ask CIG with a prefilled `?q=`, and reference links from the agency table. Each layer appears only when it has content.
  - **Ask pages** fill the box from `?q=` but never run it — the reader presses Ask, so no model call happens without them.
- **Content lifecycle** — nothing falls off the site by itself. A published post stays published (and keeps its `/article/<slug>` URL) until it is unpublished by hand; a *featured* placement expires at `featured_until`, which drops the flag, not the post.
  - **Homepage** is a 12-slot shop window: a lead, 3 in "Also this week", 8 in the wire grid. Older stories simply move out of those slots.
  - **Section pages paginate** at 20 a page (`/news`, `/news/2`, …), each pre-rendered with prev/next links, so the archive stays browsable and crawlable however large it grows.
  - **`/api/posts` is paged** (`limit`, max 500, and `offset`; the reply carries `total` and `has_more`). The site walks every page at build time — without that, only the newest 200 posts would exist and older article pages would silently stop being generated, turning live URLs into 404s.
- **Images** — every post can carry a picture, and nothing from a source is published without review.
  - **Candidate:** when the collector queues an item it reads the source page's own `og:image` (or `twitter:image`) and stores the **URL only** on `collected_items.image_url`. A page that blocks us, or has no tag, simply yields nothing.
  - **The gate:** the Publish tab shows the candidate with three ways out — *Publish with this image*, *Publish with house graphic*, or *Paste an image URL*. `content_posts.image_url` + `image_source` (`candidate`|`manual`|`house`) record the choice. "Publish all" publishes without pictures, so bulk publishing can never push a source's photo live.
  - **House graphics:** `site/public/images/house/{funding,procurement,people,policy,data,news}.png` (1200×630, Dispatch look), generated by `tools/make_house_images.py` — ours, so no copyright question. They are the fallback wherever a post has no chosen picture.
  - **Where they show:** the article page hero (linked to the source when it is the source's own picture, with an "Image: {source}" credit), section-list thumbnails, The Wire's lead slot, and `og:image` on article pages. We never re-host a source's photo — the URL is referenced and always links back.
- **Agency reference table** — `reference/agencies.json` (65 agencies: aliases, state, `ntd_id`, `cig_sponsor`, website/newsroom/procurement, `search_enabled`), synced to Postgres by `agencies.py --sync` and served read-only at `GET /api/agencies`. `match()` resolves a post tag or CIG sponsor to a row (e.g. LACMTA → LA Metro). Websites came from known domains and are **not** fetch-verified; blanks are simply omitted by the site.
- **Featured / paid People** — `featured`, `featured_until`, `sponsor` columns + a feature endpoint, for paid highlight placements.

### 3.4 CIG pipeline + Ask CIG
- **`cig.py`** — parses the monthly **FTA CIG Dashboard PDF** (by column position; validated against the real dashboard) into `cig_projects`. **Versioned by `snapshot_date`** — every month is kept, so phase advances, rating changes, and cost drift are recoverable. Full milestone dates captured (PD entry, NEPA, Engineering, LONP, rating dates, estimated grant).
  - **Load a month (usual way):** on the Command Center's **Grants** tab, paste the dashboard PDF's link from transit.dot.gov/CIG into **Load from link**, or download it and use **Upload dashboard**. transit.dot.gov blocks automated access to its /CIG page (so `--latest` fails with HTTP 403), but the PDF files themselves can usually be fetched by link. Not always: on 2026-09-22 the same PDF link was served once and then refused. When Load from link says 403, download the PDF and upload it.
  - Command line: `--url <pdf>` (a specific/archived month), `--file <pdf>`. A PDF that parses to fewer than 20 projects (a layout change — e.g. dashboards before mid-2026) is refused and existing data is left unchanged.
  - Every load or refusal is logged in `cig_loads`, and each loaded PDF is kept in `DATA_DIR/cig`.
  - **Mode comes only from FTA — never guessed.** The dashboard has no mode column. A project is **BRT** when the dashboard's *Length of Exclusive BRT* column has a value (a number or TBD; N/A = not BRT). Otherwise its mode is the **Proposed Project** field of its FTA project profile PDF. Otherwise, or when the two disagree (e.g. Interstate Bridge Replacement, light rail plus BRT), it's **NULL, shown as "Unspecified"**. `mode_source` records which applied, and `profile_file` links the profile.
  - **Profiles:** FTA's profile pages block automated access, so download the profile PDFs in a browser (Current CIG Projects / annual report). Then run `docker compose run --rm -v <folder>:/in cig --profiles /in --out /in/cig_modes.json --remode`. It reads the PDFs, keeps copies in `DATA_DIR/cig/profiles`, writes the lookup and re-applies modes to every stored snapshot. Copy `cig_modes.json` into the repo's `reference/` folder (it's committed and reviewable), then rebuild the images so the served lookup matches. Project Development profiles are prose only (no Proposed Project field), so those projects stay Unspecified unless the dashboard's BRT column says BRT. Name matching is within the same state: exact normalized name, then a unique word-containment match, then a strict fuzzy match.
  - Coverage (2026-09-11): 40 of 47 sourced (31 from the BRT column, 9 from profiles), 7 Unspecified, and 46 of 47 linked to a profile (not Green Line BRT).
- **Profile archive (`cig_profiles.py`)** keeps every version of each project's FTA profile, with change tracking, the same way the dashboard is kept month by month.
  - **Limits:** transit.dot.gov blocks automated access (Akamai 403) to the Current CIG Projects page and to each project's profile page, and we don't work around it. So links and PDFs come from your browser.
  - **Profile links:** save the Current CIG Projects page (Ctrl+S, "Webpage, HTML only") and load it with **Load projects page** on the Grants tab. You can also drop it in the inbox, or run `cig-profiles --listing FILE`. That fills `cig_profile_pages.profile_url` (one row per project). On 2026-09-22: 51 listed, 46 of 47 current projects matched; Green Line BRT isn't on FTA's page.
  - **Profile PDFs:** use **Upload profiles** (several at once), or drop them in the NAS folder `data/cig_profiles/inbox`. Each PDF is matched to a project by its title and state. One that doesn't match goes to `inbox/unmatched`; open that project and use **Upload a newer version**.
  - **Versions** (`cig_profile_versions`): the PDF text is normalized and hashed (sha256). A new version is stored only when the text differs from every stored version of that project, so re-uploads and duplicates store nothing. A revision is marked `changed`, linked to `prev_version_id`, and diffed against it. Files are in `DATA_DIR/cig_profiles/<state-project>/<captured_at>.pdf`.
  - **Baseline:** 46 versions, seeded from the PDFs kept by `cig.py --profiles`.
  - **Weekly check:** the scheduler runs it on `CIG_PROFILES_DAY` (default Monday) at `COLLECT_AT`, and **Check now** runs it on demand. It processes the inbox and makes one request for FTA's listing page, logged in `cig_profile_runs`. Today that request gets a 403 and nothing else is requested. If FTA ever serves the page, profiles are fetched politely, and a PDF whose link matches the latest archived one isn't downloaded again.
  - **What to download:** every distinct copy of the listing page is kept in `cig_profile_listings`, with its changes vs. the copy before: new projects, removed ones, stage changes and re-pointed links.
    - **PDF addresses:** `cig_profile_pages.pdf_url` records the PDF each project page links to.
    - **The list:** the Grants tab's **To download** list (and `cig-profiles --to-download`) shows current projects with a listing change, a PDF link not yet archived, or no archived version.
  - **Browser-assisted refresh (Claude in Chrome, with you present):**
    1. Claude opens the Current CIG Projects page in your Chrome and compares its table with the stored copy.
    2. It reads each project page's PDF link, one page every 4 seconds, and records them with `cig-profiles --pdf-links FILE`.
    3. It downloads only the PDFs on the To download list, with your OK, and uploads them.
    - **First run (2026-09-22):** the live page matched the saved copy, and all 46 project pages linked exactly the archived PDFs, so nothing needed downloading.
  - **Grants tab:** each project shows **Profile versions**, with capture date, FTA's date, the PDF and **Show changes** (the diff). A **profile updated** badge marks projects with a revision. This is Command Center only; the public API doesn't expose the archive.
- **API** — `/api/cig` (latest snapshot, filterable; rows include `mode_source`, `profile_file`, `profile_url`, `profile_versions`, `profile_changed_at`), `/api/cig/history` (a project's month-by-month trajectory), `/api/cig/changes` (what changed between two snapshots), `/api/cig/loads` (load history, with the kept PDFs), `/api/cig/profile?file=` (a project's FTA profile PDF; only files listed in the lookup). An expanded project row links **FTA project profile (PDF)**.
- **Ask CIG** — `/api/cig/ask`: plain-English → read-only SQL over `cig_projects` (its own Command Center tab). Queries run as the restricted `cig_reader` database role (can read `cig_projects` only), in a read-only transaction with a 5-second limit; the prompt lists the loaded snapshot dates.
- The **Grants** tab shows the pipeline summary (with a stale warning after 45 days), a **Dashboard files** list, the project table (clicking a project expands its milestones and snapshot history), and below it a **What changed** panel (latest vs. previous month or any earlier one: new/dropped projects, phase moves, rating, grant-date, cost and CIG-request changes). The public `/cig` page uses the same order: table first, then What changed.

### 3.5 Command Center (private dashboard)
- **`command_center.py`** — the internal home base, served on the NAS LAN at **`http://<nas-ip>:8088`** (8088 because QNAP's admin UI owns 8080).
- Tabs: **Ask NTD**, **Collection**, **Sources**, **Publish**, **Grants**, **Ask CIG**. Plus a live status bar (API rows, DB tables).
- This is where you approve, publish, feature, run collectors, upload CIG dashboards, and QA. **It is never exposed publicly.**

### 3.6 Shared component bundle
- **`static/t411.js`** + **`static/t411.css`** — framework-free UI components (`renderCigTable` with expandable milestone/timeline rows, `renderCigMilestones`, `renderCigTimeline`, `renderCigChanges`, `openCigProject`) that use the host page's CSS variables. All values are HTML-escaped (quotes included), since data comes from outside sources. **Loaded by both the Command Center and the public site**, so views are built once and used everywhere.

### 3.7 Public site (Astro)
- **`/site`** — an **Astro** static site deploying to **Cloudflare Pages**. Design is **"Dispatch"** (bold editorial newspaper: near-black + red, Archivo + Spectral).
- Pages: homepage (`index.astro`), section pages (`[section].astro` → News/Funding/Procurement/People/Policy), a **Data hub** (`data.astro`).
- **`src/lib/content.js`** — central content source. At **build time** it fetches published posts from `${PUBLIC_API_BASE}/api/posts` and the CIG summary from `/api/cig` (`PUBLIC_API_BASE` defaults to `https://api.transit411.net`; see `site/.env.example`). The homepage shows the lead story, "Also this week" and the wire, plus live CIG figures; section pages filter by pillar. Headlines link to the original source (no article pages yet). If the API is down or slow (8 s timeout) the build still succeeds and pages show a "no stories yet" state.
- **Static output:** newly published posts appear on the site after the **next Pages build** (a push, a manual redeploy, or a Pages deploy hook). `site/.nvmrc` pins Node 22 for the build.
- Lives at **`transit411.pages.dev`** (domain stays dark until launch).

### 3.8 Read-only public API + Cloudflare Tunnel
- **`readonly_api.py`** — the ONLY service the tunnel exposes. It **allowlists** exactly the safe endpoints (`GET /api/posts`, `GET /api/cig`, `GET /api/cig/history`, `GET /api/cig/changes`, `GET /api/cig/profile`, `POST /api/ask`, `POST /api/cig/ask`) and forwards them to the Command Center; **everything else returns 404.** CORS restricts browser calls to the Transit411 site (`transit411.pages.dev`, `transit411.net`, `transit411.com` and their subdomains).
  - The two ask endpoints each cost an Anthropic call, so they are **rate-limited**: `ASK_PER_IP_HOUR` per visitor (default 20, by Cloudflare's visitor-IP header), `ASK_PER_DAY` in total (default 200), questions up to `ASK_MAX_CHARS` (500), requests up to 16 KB. `PUBLIC_ASK_ENABLED=false` turns them off. Counts are in memory and reset when the container restarts.
- **`cloudflared`** — the tunnel container. Dials out to Cloudflare (no open router ports). Public hostname **`api.transit411.net`** → `http://readonly-api:8000`. It sits on its own `public` Docker network with `readonly-api` only, so a misconfigured hostname in the Cloudflare dashboard can't reach the Command Center, the database or the NTD API. The token is passed as the `TUNNEL_TOKEN` environment variable (not on the command line).
- **Data path:** internet → Cloudflare → tunnel → `readonly-api` (6 safe endpoints) → Command Center. No path from the public to any write/admin action.

---

## 4. Data stores

**Postgres** (`db` service, `pgvector/pgvector:pg16`), database `transit411`:
- `sources` — the collector's watchlist (feeds + keyword searches).
- `collected_items` — the review queue (pending/approved/skipped/published/filtered) with facets + embedding column.
- `content_posts` — published posts the site reads (title, body, pillar, facets, featured/sponsor, `item_id` link).
- `cig_projects` — the CIG pipeline, versioned by `snapshot_date`, with milestone dates.
- `cig_loads` — every CIG dashboard load or refusal (source, name/link, projects, kept PDF path).
- `cig_profile_pages` — each project's profile page on FTA's site (`profile_url`), from the saved listing page.
- `cig_profile_versions` — every distinct version of each project's profile PDF: text hash, file, `changed`, `prev_version_id`, diff.
- `cig_profile_runs` — each weekly/manual profile check and upload batch.
- `subscribers` — newsletter list (schema present).
- `app_settings` — small key/values (the auto-collect toggle, the scheduler's next/last run).

**DuckDB** — `ntd.duckdb` (file on a NAS volume): `ntd_service` (latest year), `ntd_history` (2015 onward, raw and inflation-adjusted), and `cpi`, behind Ask NTD.

**Files** — kept CIG dashboard PDFs in `DATA_DIR/cig`.

---

## 5. Docker services (docker-compose.yml)

| Service | Type | Purpose |
|---|---|---|
| `db` | always-on | Postgres + pgvector (content store) |
| `api` | always-on | NTD API (`serve.py`) — Ask NTD and its question page, port 8000 (LAN) |
| `command-center` | always-on | Private dashboard, port **8088** (LAN) |
| `readonly-api` | always-on | Public-safe API proxy (internal only; tunnel target) |
| `cloudflared` | always-on | Cloudflare Tunnel → `api.transit411.net` |
| `scheduler` | always-on | Daily collector run |
| `ingest` | on-demand | `build_db.py` — (re)build the NTD DuckDB |
| `collect` | on-demand | `collection.py` — run the collector / seed / migrate / backfill / normalize |
| `cig` | on-demand | `cig.py` — load a CIG dashboard snapshot from the command line |
| `cig-profiles` | on-demand | `cig_profiles.py` — profile archive: `--listing FILE`, `--ingest PDF...`, `--seed`, `--weekly`, `--rediff` |

On-demand services use the `tools` profile: run with `docker compose run --rm <service> <args>`. Note that plain `docker compose build` skips them — also run `docker compose --profile tools build ingest collect cig cig-profiles` after code changes.

**Naming:** the compose file sets `name: transit411`, so every container, network and image is prefixed (`transit411-<service>-1`). Give new services plain names; the prefix is automatic.

---

## 6. Runbook — common operations

All run on the NAS (via Claude Code or SSH), from the app folder `/share/Container/transit411/app`. In SSH sessions `docker` isn't on the PATH; run `export PATH=/share/CACHEDEV2_DATA/.qpkg/container-station/bin:$PATH` first.

**Bring the stack up / update after a code change:**
```
# (the NAS isn't a git checkout: copy the changed files into the app folder first — see §7)
docker compose build
docker compose --profile tools build ingest collect cig cig-profiles
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

**Load a CIG dashboard snapshot (monthly):** open transit.dot.gov/CIG in a browser, copy the dashboard PDF's link, and paste it into **Load from link** on the Command Center's Grants tab (or download it and use **Upload dashboard**). Command-line equivalents:
```
docker compose run --rm cig --url <pdf-url>     # a specific/archived month (backfill history)
# cig --latest fails: transit.dot.gov returns HTTP 403 to automated requests for the /CIG page
```

**Everyday content work:** open `http://<nas-ip>:8088` → Collection (search/filter, approve) → Publish. Use the header's **Auto-collect** switch to pause the daily collector.

**Bring up the tunnel / public API:**
```
docker compose up -d readonly-api cloudflared
docker compose logs cloudflared          # expect "Registered tunnel connection"
curl https://api.transit411.net/health   # expect {"ok":true,"service":"readonly-api"}
```

**Verify the security boundary:**
```
curl -X POST https://api.transit411.net/api/publish/1     # must return 404 (a real Command Center route)
curl https://api.transit411.net/api/collection            # must return 404
curl https://api.transit411.net/api/cig                   # must return 200
```

---

## 7. Deploy workflow

- **Code:** built here (chat) → delivered as a **git patch** → Claude Code applies (`git apply <patch>`), commits, pushes.
- **Repo private?** No — public, so Cloudflare Pages can build it and the chat can read it. Secrets stay out via `.gitignore`.
- **NAS deploy:** the NAS app folder is not a git checkout (no git there). Claude Code copies the changed files from the local repo to `/share/Container/transit411/app` with `scp` (rsync isn't available on the PC), then runs `docker compose build && docker compose up -d <services>` (plus `--profile tools build` for on-demand services). Patches are reviewed before applying; ones written against older code are merged by hand rather than force-applied.
- **Site deploy:** every push to the repo triggers **Cloudflare Pages** to rebuild `/site`.
  - Pages build settings: **Root directory** `site`, **Build command** `npm install && npm run build`, **Output directory** `dist`.
- **Line endings:** a `.gitattributes` pins LF to avoid cross-platform churn.

---

## 8. Configuration (.env on the NAS — never committed)

| Var | Purpose |
|---|---|
| `ANTHROPIC_API_KEY` | Model calls (collection classify, Ask NTD/CIG SQL generation) |
| `ASK_NTD_MODEL` | Model for NL→SQL. Code default `claude-haiku-4-5`; **set to `claude-sonnet-5`** on the NAS (more reliable on multi-step questions) |
| `COLLECT_MODEL` | Model for collector triage (default `claude-haiku-4-5`) |
| `DB_PASSWORD` | Postgres password (`openssl rand -hex 16`) |
| `DATA_DIR` | NAS path for data volumes (DuckDB, Postgres, kept CIG PDFs) |
| `COLLECT_AT` / `COLLECT_TZ` | Daily collector schedule (defaults `06:00`, `America/New_York`) |
| `CLOUDFLARE_TUNNEL_TOKEN` | Cloudflare Tunnel token |
| `CF_PAGES_DEPLOY_HOOK` | Cloudflare Pages deploy hook URL (secret). Publishing/unpublishing rebuilds the site ~1 minute later; the Publish tab also has **Rebuild site now** |
| `PUBLIC_ASK_ENABLED` | Public ask endpoints on/off (default `true`) |
| `ASK_PER_IP_HOUR` / `ASK_PER_DAY` / `ASK_MAX_CHARS` | Public ask limits (defaults 20 / 200 / 500) |

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
- Public Astro site on Cloudflare Pages (homepage + sections + data hub), **reading live posts and CIG figures from the API at build time**. ✅
- Read-only public API + Cloudflare Tunnel, secured by allowlist; `api.transit411.net` live. ✅
- Public site's **Ask NTD** and **Ask CIG** pages, plus **CIG Pipeline** (`/cig`: phase filter, sortable table, milestones, history, What changed). ✅
- CIG **mode from FTA sources only** (BRT column + profile), "Unspecified" otherwise; each project linked to its FTA profile PDF and page. ✅
- CIG **profile archive**: versioned profiles with text diffs, listing snapshots, a To download list, and the browser-assisted refresh. ✅
- **Sortable CIG table** (every column; blanks last; third click restores the default order), on the Grants tab and `/cig`. ✅

- **Mode filter** on `/cig` (chips with counts) and the Grants tab (a dropdown), combinable with the phase filter. "Unspecified" is its own option; `/api/cig?mode=Unspecified` returns projects with no FTA-stated mode, and the summary carries `by_mode`. ✅
- **Sources tab**: the collector's registry as a control panel (add/edit/delete/enable feeds and keyword searches, Test, per-source health); changes apply to the next run. ✅
- **Publish all** on the Publish tab: shown when 2+ items are ready. After a confirm with the count, it publishes exactly the items on screen (anything approved after the page loaded waits) in one transaction via `POST /api/publish-all`, with a single site rebuild. ✅

**Pending / next:**
- Refresh the profile PDFs as projects enter Engineering, since their profiles then state a mode (fewer Unspecified).
- **Owned email programme** (contacts live in our Postgres; Amazon SES is only the sending pipe). Phase 1 is done: the `contacts` + `email_suppressions` tables, the Contacts tab, and the public `POST /api/subscribe` (pending contacts only, no sending).
  - **Phase 2 — SES sending, double opt-in, feedback loop:** `email_sender.py` (boto3, throttled batches), a confirmation email that flips a contact to `subscribed`, a self-hosted tokenised `/unsubscribe` plus `List-Unsubscribe` and `List-Unsubscribe-Post` one-click headers (RFC 8058), and `POST /api/email/sns` for SES→SNS bounces and complaints (handshake confirmed, message verified) feeding the suppression list. Warm the sending volume up gradually.
  - **Phase 3 — Newsletter module (built):** the Command Center's **Newsletter** tab. *Draft issue* builds one from `content_posts` for a period (7/14/30 days), grouped by pillar, every headline linking to its article page on the site, with an optional intro line. The draft is editable (subject and HTML body), previewable at `/api/newsletter/issues/{id}/preview`, and can be sent as a `[TEST]` to one address. *Send to subscribers* goes only to `subscribed` contacts that aren't suppressed, throttled at the SES rate, requires `confirm=true`, refuses to send an issue twice, and records every recipient in `issue_recipients` — a later bounce or complaint is matched back to that row by SES message id. `{unsub}` in the body becomes each recipient's own unsubscribe link.
  - **AWS/DNS setup, as of 2026-09-22:** ✅ SES identity `mail.transit411.net` verified; ✅ custom MAIL FROM `bounce.mail.transit411.net` (MX `feedback-smtp.us-east-1.amazonses.com`, SPF `v=spf1 include:amazonses.com ~all`) — records live, SES status still pending its own recheck (harmless: sending falls back to the default MAIL FROM); ✅ DMARC `v=DMARC1; p=none; rua=mailto:admin@transit411.net; fo=1` at `_dmarc`; ✅ IAM user `transit411-ses-sender` (send-only) + keys in `.env`; ✅ SNS topic `transit411-ses-feedback` subscribed to `POST /api/email/sns` (handshake auto-confirmed) and set as SES's bounce/complaint destination; ⬜ SES production access (requested, in review — until granted, sending only reaches SES-verified addresses).
  - **Two gotchas worth remembering:** the IAM policy must list the **configuration set** ARN as well as the identity ARN when `SES_CONFIGURATION_SET` is set, or every send fails `AccessDenied`; and `SNS_TOPIC_ARN` in `.env` must be the real ARN — a wrong value silently 403s every genuine notification.
  - **Feedback loop proven (2026-09-22):** test sends to `bounce@simulator.amazonses.com` and `complaint@simulator.amazonses.com` came back through SNS within ~5s and set those contacts to `bounced` / `complained`, suppressed both, and emptied the mailable list. Test rows deleted afterwards.
    - **`PUBLIC_SITE_URL` must point at a live site.** The Wire's links and its lead image are absolute, and `transit411.net` is still dark, so it is set to `https://transit411.pages.dev` until the root domain is pointed at Pages — change it there and then, or every link in a sent issue 404s.
  - **Careful with `.env` edits:** the file had no trailing newline, so an appended line once merged into `SNS_TOPIC_ARN` and silently broke the feedback webhook. Always check `grep -n` afterwards.
- **Region:** us-east-1. **Root domain mail stays on GoDaddy** (`smtp.secureserver.net`) — never touch the root MX.
- Facet filtering on the section pages, so an article's agency/program chips can link to a filtered view (they're labels today).
- Backfill more CIG monthly PDFs to enrich history/timelines (loaded so far: 2026-07-10, 2026-08-07, 2026-09-11). Dashboards from before mid-2026 use a different layout and need a second set of column positions in `cig.py`.

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

- The **read-only API allowlist** is the public boundary — only 7 read/ask endpoints are reachable (the 6 above plus `GET /api/cig/profile`, which serves only the FTA profile PDFs in the committed lookup); all writes and the profile archive are 404. Keep it tight if adding public endpoints.
- The **public ask endpoints spend money** (an Anthropic call each): keep the per-visitor and daily limits on, and set `PUBLIC_ASK_ENABLED=false` if usage looks wrong. Also set a spending limit in the Anthropic Console.
- The **tunnel is network-isolated**: `cloudflared` can reach only `readonly-api`, even if a dashboard hostname is misconfigured.
- **Secrets live only in `.env`** on the NAS (git-ignored). Never commit keys or tokens, and don't paste them into chat.
- **Regenerate the Cloudflare Tunnel token** if it's ever exposed (Zero Trust → Tunnels → refresh token).
- The **Command Center is LAN/Tailscale only and has no login** — never expose it through the tunnel.
- Model-facing SQL is **read-only**: Ask NTD opens DuckDB read-only behind SELECT/WITH-only guards; Ask CIG runs a single checked SELECT in a read-only Postgres transaction as the `cig_reader` role, which can read `cig_projects` and nothing else (not `subscribers`), with a 5-second timeout.
- Everything the pages show from feeds, the model or the database is **HTML-escaped**, and only `http(s)` links are rendered.

---

*End of manual. Keep this in the repo (e.g. `docs/MANUAL.md`) so it travels with the project and updates as the system grows.*
