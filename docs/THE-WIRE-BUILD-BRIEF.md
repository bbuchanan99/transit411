# Build brief — Owned email: contacts, SES sending, and "The Wire" newsletter

Hand these to Claude Code. Architecture: **the master contact list lives in Postgres (we own it); Amazon SES (Essentials plan) is the sending pipe; the Command Center is the brain; webhooks (SES → SNS → our endpoint) carry bounces/complaints/unsubscribes back to keep the list clean.** Read `docs/MANUAL.md`. Build in phases; stop at each checkpoint. Commit/push per phase. **Step 0 every time: sync the repo — commit/push any uncommitted NAS work first and summarize.**

The newsletter is **"The Wire"** (publisher: Shepherd Labs). Its visual spec is the committed mock `the-wire-mock.html` (Dispatch styling: near-black `#17140F` + red `#C0341F`/`#EE6A54`, Archivo + Spectral). Match it.

---

## Phase 1 — Contact database + signup capture (no AWS needed; build this first)

1. **Schema — `contacts` table:** `id`, `email` (unique, lowercase-normalized), `name`, `status` (`pending`|`subscribed`|`unsubscribed`|`bounced`|`complained`), `source` (how they signed up), `tags text[]`, `confirm_token`, `unsub_token`, `created_at`, `confirmed_at`, `unsubscribed_at`, `last_event_at`. Treat `bounced`/`complained`/`unsubscribed` as suppressed — never emailed.
2. **Command Center "Contacts" tab:** counts by status; search; filter by status/tag; manual add; **CSV import** (dedupe on email); **CSV export**; edit tags; mark unsubscribed. This is the owned CRM asset.
3. **Public signup capture:** the site's newsletter form POSTs to a new **`POST /api/subscribe`** (validate + rate-limit; insert as `pending`). Add this single write endpoint to the read-only API allowlist (`readonly_api.py`) — it's the only public write, and it only creates a pending contact.
4. **Checkpoint:** show the Contacts tab working with a manual add + a CSV import before Phase 2.

---

## Phase 2 — SES sending + double opt-in + list hygiene

**Brian's AWS/DNS setup (do in parallel; you tell me the exact values):**
- Create an AWS account; open **SES** on the **Essentials** plan (no monthly fee, pay-per-email). Pick **one region** and standardize on it.
- Verify a sending **subdomain** `mail.transit411.net` (isolates newsletter reputation from the root). Add the **DKIM** CNAMEs + **SPF** + a **DMARC** record in Cloudflare DNS (you give me the records).
- Request **SES production access** (moves out of sandbox — short form; answer: opt-in transit newsletter with bounce/complaint handling + one-click unsubscribe).
- Create an **IAM user** with SES-send permission → keys for `.env`.
- Create **SNS topics** for bounce + complaint notifications → pointed at our webhook.

**Build:**
1. **Sending service** (`email_sender.py`): send via SES (boto3) from `news@mail.transit411.net`. AWS creds/region/from-address from `.env`. Throttle to SES rate limits.
2. **Double opt-in:** on signup, email a tokenized confirmation link → clicking sets `subscribed` + `confirmed_at`. Only `subscribed` contacts ever receive The Wire.
3. **Unsubscribe (self-hosted, bulletproof):** tokenized `GET /unsubscribe?t=...` → `unsubscribed`. Include **`List-Unsubscribe` + `List-Unsubscribe-Post` one-click headers (RFC 8058)** on every send (required by Gmail/Yahoo bulk rules).
4. **Feedback webhook:** `POST /api/email/sns` receiving SES→SNS **bounce** and **complaint** events (confirm the SNS subscription handshake; verify the message signature). Hard bounce → `bounced`; complaint → `complained`; both suppressed permanently.
5. **Deliverability:** DKIM/SPF/DMARC alignment, one-click unsubscribe, authoritative suppression list (never re-mail a bad address), gradual volume warm-up.
6. **Checkpoint:** demonstrate the full loop — subscribe → confirm → receive a test email → unsubscribe → a simulated bounce/complaint updates the contact.

---

## Phase 3 — "The Wire" template + newsletter module

1. **Email template (match `the-wire-mock.html`):** build it as **email-client-safe HTML** — table-based layout, inline styles, ~600px centered, web-safe fallbacks for Archivo/Spectral, alt text on images, and dark-mode-tolerant colors. Sections in order:
   - **Masthead:** TRANSIT411 · **THE WIRE** · tagline "Transit news, data & moves" · date + Issue No.
   - **The Lead** (top story: kicker, headline, optional lead image, 2-line take, source link).
   - **The Feed** (~3 items: pillar tag, headline, one-line why-it-matters, source link).
   - **By the Numbers** (the dark data block — one live stat from our own data, e.g. a CIG pipeline figure via `/api/cig`, with an "Ask it yourself →" link to Ask CIG/NTD). This is the differentiator; keep it every issue.
   - **On the Move** (2–3 people items).
   - **Open Procurements** (compact list; optional — include only when notable).
   - **Footer:** forward-to-a-colleague, subscribe link, one-click unsubscribe, "Transit411 is a product of Shepherd Labs, LLC."
2. **Newsletter module (Command Center "Newsletter" tab):**
   - **Auto-draft an issue from `content_posts`** for a chosen date range — pick a lead, group Feed items by pillar, pull the By-the-Numbers stat live, list recent People and Procurements. Fully **editable** before send.
   - **Preview** (renders the real template) → **send** to `subscribed` contacts (optionally by tag/segment) via SES, throttled.
   - **`issues` table:** log each issue (subject, sent_at, recipient count, segment). Capture per-recipient delivery/bounce from webhooks where feasible.
3. **Checkpoint:** send a real issue to a small test segment; confirm it renders correctly across Gmail + Outlook + Apple Mail.

---

## Images for posts (cross-cutting — needed by The Wire, article pages, and section lists)

1. **Schema:** add `image_url` and `image_source` (`candidate`|`manual`|`house`) to `content_posts`; add a candidate `image_url` to `collected_items`.
2. **Auto-candidate at collection:** when the collector fetches an article, also read the source page's **`og:image`** meta tag and store it as a *candidate* on the item (do not auto-publish it).
3. **Human gate in the Publish tab:** on approve/publish, show the candidate image — **keep it, paste a different URL, or choose a branded house graphic**. Never publish an image without this review.
4. **Branded pillar fallbacks (house graphics):** create a small set of on-brand images — one per pillar (Funding, Procurement, People, Policy, Data) in the Dispatch look — committed to the repo. Use as the fallback when there's no good `og:image` or the user prefers a safe/branded image. (Simple, distinctive, zero copyright risk.)
5. **Usage:** The Wire lead image, article pages, and section thumbnails use `image_url` with the pillar house-graphic as fallback. Keep images small and always linked to the source. **Never host a source's full-resolution photo as if it's ours** — thumbnails linking back only; prefer house graphics when unsure.

---

## Global constraints
- The Postgres contact list is the master; SES only sends. Never email a suppressed contact.
- Public surface stays minimal: only `/api/subscribe`, `/unsubscribe`, `/api/email/sns` are added (deliberately) beyond the existing read-only allowlist.
- Secrets in `.env` only. Match Dispatch styling. Commit/push per phase and report any AWS/DNS action needed from Brian.
