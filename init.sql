-- Transit411 content store (phase 2 foundation). Runs once on first DB start.
CREATE EXTENSION IF NOT EXISTS vector;

-- Feeds the collection engine watches.
CREATE TABLE IF NOT EXISTS sources (
  id          BIGSERIAL PRIMARY KEY,
  name        TEXT NOT NULL,
  url         TEXT NOT NULL,
  pillar      TEXT,              -- Funding | Procurement | People | Policy | Data
  type        TEXT,              -- Official | Trade | Association | Aggregator | Agency | Data
  method      TEXT,              -- RSS | API | Scrape | Search | Manual
  trust       TEXT DEFAULT 'Med',
  notes       TEXT,
  added_at    TIMESTAMPTZ DEFAULT now()
);

-- Items the engine collects, awaiting review.
CREATE TABLE IF NOT EXISTS collected_items (
  id          BIGSERIAL PRIMARY KEY,
  pillar      TEXT,
  headline    TEXT NOT NULL,
  summary     TEXT,
  source_name TEXT,
  source_url  TEXT,
  published   DATE,
  deadline    DATE,              -- for procurements / ballot measures (freshness engine)
  relevance   TEXT DEFAULT 'med',
  status      TEXT DEFAULT 'pending',  -- pending | approved | skipped | published | filtered (auto: low relevance)
  agencies    TEXT[],
  mode        TEXT[],
  programs    TEXT[],
  tags        TEXT[],
  state       TEXT,
  embedding   VECTOR(1536),      -- for semantic content search
  collected_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS collected_status_idx ON collected_items(status);

-- Published posts (the public site reads these).
CREATE TABLE IF NOT EXISTS content_posts (
  id           BIGSERIAL PRIMARY KEY,
  slug         TEXT UNIQUE,
  pillar       TEXT,
  title        TEXT NOT NULL,
  body         TEXT,
  status       TEXT DEFAULT 'draft',    -- draft | scheduled | published
  publish_at   TIMESTAMPTZ,
  item_id      BIGINT,                  -- the collected_items row it was published from
  source_name  TEXT,
  source_url   TEXT,
  agencies     TEXT[],
  mode         TEXT[],
  programs     TEXT[],
  tags         TEXT[],
  state        TEXT,
  featured     BOOLEAN DEFAULT false,
  featured_until TIMESTAMPTZ,
  sponsor      TEXT,
  source_type  TEXT DEFAULT 'collected',
  created_at   TIMESTAMPTZ DEFAULT now()
);

-- Small shared settings (e.g. the Command Center's Auto-collect toggle). collection.py also
-- creates this if missing, since init.sql only runs when the database is first created.
CREATE TABLE IF NOT EXISTS app_settings (
  key          TEXT PRIMARY KEY,
  value        JSONB NOT NULL,
  updated_at   TIMESTAMPTZ DEFAULT now()
);

-- Newsletter subscribers.
CREATE TABLE IF NOT EXISTS subscribers (
  id           BIGSERIAL PRIMARY KEY,
  email        TEXT UNIQUE NOT NULL,
  confirmed    BOOLEAN DEFAULT false,
  created_at   TIMESTAMPTZ DEFAULT now()
);

-- FTA Capital Investment Grants pipeline (loaded by cig.py from the monthly dashboard PDF).
CREATE TABLE IF NOT EXISTS cig_projects (
  id BIGSERIAL PRIMARY KEY, snapshot_date DATE, project_name TEXT, sponsor TEXT,
  city TEXT, state TEXT, mode TEXT, phase TEXT, length_mi TEXT, stations TEXT,
  cost_musd NUMERIC, cost_raw TEXT, cig_request_musd NUMERIC, cig_request_raw TEXT,
  cig_share TEXT, rating TEXT, noncig_status TEXT, est_grant TEXT, nepa TEXT,
  pd_entry TEXT, eng_entry TEXT, fetched_at TIMESTAMPTZ DEFAULT now()
);
