-- Run this once after Terraform creates the Cloud SQL instance:
--   psql "$(gcloud secrets versions access latest --secret=arxivlens-dev-db-url)" \
--        -f infra/sql/init.sql

-- 1. Extensions
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- 2. Papers table (one row per paper)
CREATE TABLE IF NOT EXISTS papers (
  arxiv_id          TEXT PRIMARY KEY,
  title             TEXT NOT NULL,
  authors           TEXT[],
  primary_category  TEXT,
  published         DATE,
  abstract          TEXT,
  metadata          JSONB,
  ingested_at       TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS papers_published_idx ON papers (published DESC);
CREATE INDEX IF NOT EXISTS papers_category_idx  ON papers (primary_category);

-- 3. Chunks table — text, figures, tables
CREATE TABLE IF NOT EXISTS chunks (
  chunk_id          TEXT PRIMARY KEY,
  arxiv_id          TEXT NOT NULL REFERENCES papers(arxiv_id) ON DELETE CASCADE,
  modality          TEXT NOT NULL CHECK (modality IN ('text', 'figure', 'table')),
  section           TEXT,
  content           TEXT NOT NULL,
  content_tsv       TSVECTOR GENERATED ALWAYS AS (to_tsvector('english', content)) STORED,
  embedding         VECTOR(768),
  metadata          JSONB,
  content_hash      TEXT NOT NULL,
  -- For figures only:
  image_uri         TEXT,
  -- For tables only:
  table_headers     TEXT[],
  table_first_rows  JSONB,
  created_at        TIMESTAMPTZ DEFAULT NOW()
);

-- BM25-style sparse retrieval
CREATE INDEX IF NOT EXISTS chunks_tsv_idx ON chunks USING GIN (content_tsv);

-- Metadata filtering
CREATE INDEX IF NOT EXISTS chunks_metadata_idx ON chunks USING GIN (metadata);
CREATE INDEX IF NOT EXISTS chunks_arxiv_idx    ON chunks (arxiv_id);
CREATE INDEX IF NOT EXISTS chunks_modality_idx ON chunks (modality);

-- HNSW index for fast approximate nearest-neighbor search.
-- Build this AFTER bulk-loading data; it's faster.
-- CREATE INDEX chunks_embedding_hnsw_idx ON chunks
--   USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64);

-- 4. Eval runs
CREATE TABLE IF NOT EXISTS eval_runs (
  run_id          TEXT PRIMARY KEY,
  git_sha         TEXT,
  created_at      TIMESTAMPTZ DEFAULT NOW(),
  model           TEXT,
  config          JSONB,
  metrics         JSONB,
  notes           TEXT
);

-- 5. Query log (for debugging + online eval)
CREATE TABLE IF NOT EXISTS queries (
  query_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  query_text      TEXT NOT NULL,
  filters         JSONB,
  retrieved_ids   TEXT[],
  answer          TEXT,
  faithfulness    REAL,
  latency_ms      INTEGER,
  model           TEXT,
  created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS queries_created_idx ON queries (created_at DESC);
