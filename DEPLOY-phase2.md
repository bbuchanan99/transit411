# Deploy brief — phase 2 (Command Center + Postgres/pgvector)

Paste to your Claude Code terminal on the PC. Phase 1 (the `api`) is already
running; this adds the database and the dashboard.

## New files in this drop
- `command_center.py` — the private dashboard (status + Ask NTD console, Dispatch-branded)
- `init.sql` — content schema (sources, collected_items, content_posts, subscribers) + pgvector
- updated `Dockerfile`, `docker-compose.yml`, `requirements.txt`

## Steps for Claude Code

1. Sync the updated project to the NAS:
   ```
   rsync -av ./transit411-data/ admin@<nas-ip>:/share/Container/transit411/app/
   ```

2. (Optional but recommended) set a real DB password in `.env` on the NAS:
   ```
   echo "DB_PASSWORD=$(openssl rand -hex 16)" >> /share/Container/transit411/app/.env
   ```

3. From the app folder on the NAS, rebuild and bring up the new services
   (the image changed, so rebuild; `api` restarts harmlessly):
   ```
   docker compose build
   docker compose up -d db
   docker compose up -d api command-center
   docker compose ps
   ```

4. Verify from a LAN browser:
   - Dashboard: `http://<nas-ip>:8080`  — both status pills should go green
     ("API · N rows", "DB · 4 tables").
   - Run a query in the Ask NTD box, e.g. "cheapest heavy rail systems per rider".
     The chart now plots the metric you asked about (the earlier chart bug is fixed).

## Notes
- Ports 8000 (API) and 8080 (Command Center) are LAN-only. Do not port-forward.
- The DB starts empty except for the schema; the collection/sources/publish tabs
  (phase 2b) fill it. `init.sql` runs only on first DB creation.
- If a pill stays red, `docker compose logs api` / `docker compose logs db` /
  `docker compose logs command-center` will show why — paste it back and I'll fix it.
