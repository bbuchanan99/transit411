# Deploy brief — paste this into your Claude Code terminal (the one on your PC)

This is written for the Claude Code session running on your PC on 192.168.1.0,
which *can* reach the QNAP. It builds the phase-1 engine (real NTD data + Ask NTD
API) as containers in Container Station.

## What you're deploying
- `ingest` — one-shot container that pulls real NTD data and builds `ntd.duckdb`
- `api` — always-on read-only Ask NTD API on port 8000 (LAN only for now)

## Prerequisites (confirm or set up)
- SSH is enabled on the QNAP (Control Panel → Telnet/SSH), and you can `ssh admin@<nas-ip>`.
- Container Station is installed, which provides Docker + Compose on the NAS.
- The NAS has outbound internet (it needs to reach data.transportation.gov).
- You have an Anthropic API key for the natural-language endpoint (optional to start;
  raw SQL works without it).

## Steps for Claude Code to run

1. Copy this project folder to the NAS (adjust the IP/user and path):
   ```
   rsync -av ./transit411-data/ admin@<nas-ip>:/share/Container/transit411/app/
   ```

2. SSH in and go to the app folder:
   ```
   ssh admin@<nas-ip>
   cd /share/Container/transit411/app
   ```

3. Create the env file and the data directory:
   ```
   cp .env.example .env
   #  edit .env: set ANTHROPIC_API_KEY and confirm DATA_DIR
   mkdir -p /share/Container/transit411/data
   ```

4. Build the database from **real** NTD data (this is the validation step):
   ```
   docker compose run --rm ingest
   ```
   Expect output like "Normalized to N agency-mode rows across M modes" and a
   per-mode summary. If column detection complains, it prints the columns it saw —
   tell me and I'll adjust `build_db.py` (the DOT file layout occasionally shifts).

5. Start the API:
   ```
   docker compose up -d api
   ```

6. Verify from any browser on your LAN:
   - `http://<nas-ip>:8000/health`  → `{"ok": true, "rows": ...}`
   - `http://<nas-ip>:8000/sql?q=SELECT agency, mode, ROUND(cost_per_rider,2) cpr FROM ntd_service WHERE mode='Heavy Rail' ORDER BY cpr LIMIT 10`

## Notes
- Port 8000 is exposed on the LAN only. Do **not** port-forward it. When we go
  public, a Cloudflare Tunnel points the cloud site at this API with no open ports.
- Refresh the data anytime with `docker compose run --rm ingest`.
- Phase 2 adds a Postgres+pgvector service (content, subscribers, watchlists) and
  the Command Center container — after this engine is confirmed working on the NAS.
