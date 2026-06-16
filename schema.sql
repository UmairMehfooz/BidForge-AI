-- ==============================================================================
-- BidForge AI — Supabase Database Schema
-- Run this in your Supabase SQL Editor to create the required tables.
-- ==============================================================================

-- 1. Workspaces Table
CREATE TABLE workspaces (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'created',
    rfp_file_path TEXT,
    sector TEXT,       -- inferred during /parse; drives sector win-rate in scoring
    issuer_type TEXT,  -- inferred during /parse ('govt'/'private'); drives hybrid matching
    warning TEXT,      -- non-fatal pipeline warnings (truncation, failed chunks)
    competitor_presence TEXT DEFAULT 'unknown',  -- manual input: unknown/low/medium/high (Fix 7)
    pipeline_timings JSONB,  -- per-stage wall-clock seconds, e.g. {"parse": 41.2, "total": 95.3} (Fix 9)
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- MIGRATION for existing databases (run once in the Supabase SQL editor):
-- ALTER TABLE workspaces ADD COLUMN IF NOT EXISTS sector TEXT;
-- ALTER TABLE workspaces ADD COLUMN IF NOT EXISTS issuer_type TEXT;
-- ALTER TABLE workspaces ADD COLUMN IF NOT EXISTS warning TEXT;
-- ALTER TABLE workspaces ADD COLUMN IF NOT EXISTS competitor_presence TEXT DEFAULT 'unknown';
-- ALTER TABLE workspaces ADD COLUMN IF NOT EXISTS pipeline_timings JSONB;

-- 2. Requirements Table
CREATE TABLE requirements (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    section_ref TEXT,
    requirement TEXT NOT NULL,
    type TEXT NOT NULL,
    deadline TEXT,
    budget_ref TEXT,
    extraction_source TEXT,  -- 'llm' | 'ner' | 'both' (Fix 4)
    taxonomy_id TEXT,        -- evaluation-criteria taxonomy entry (Fix 8)
    taxonomy_name TEXT,
    note TEXT,                        -- bid manager's short note on this requirement
    is_done BOOLEAN DEFAULT FALSE,    -- bid manager's done/not-done mark
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- MIGRATION for existing databases (run once in the Supabase SQL editor):
-- ALTER TABLE requirements ADD COLUMN IF NOT EXISTS extraction_source TEXT;
-- ALTER TABLE requirements ADD COLUMN IF NOT EXISTS taxonomy_id TEXT;
-- ALTER TABLE requirements ADD COLUMN IF NOT EXISTS taxonomy_name TEXT;
-- ALTER TABLE requirements ADD COLUMN IF NOT EXISTS note TEXT;
-- ALTER TABLE requirements ADD COLUMN IF NOT EXISTS is_done BOOLEAN DEFAULT FALSE;

-- 3. Compliance Items Table
CREATE TABLE compliance_items (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    requirement_id UUID NOT NULL REFERENCES requirements(id) ON DELETE CASCADE,
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    status TEXT NOT NULL,
    matched_capability_id TEXT,
    confidence FLOAT NOT NULL DEFAULT 0.0,
    gap_note TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 4. Proposal Sections Table
CREATE TABLE proposal_sections (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    requirement_id UUID REFERENCES requirements(id) ON DELETE SET NULL,
    section_title TEXT,
    section_ref TEXT NOT NULL,
    ai_draft TEXT,
    edited_draft TEXT,
    status TEXT NOT NULL DEFAULT 'draft',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- MIGRATION for existing databases (run once in the Supabase SQL editor):
-- ALTER TABLE proposal_sections ADD COLUMN IF NOT EXISTS requirement_id UUID;
-- ALTER TABLE proposal_sections ADD COLUMN IF NOT EXISTS section_title TEXT;

-- 5. Bid Scores Table (renamed from win_scores — Fix 12).
-- Columns now match what the scoring engine actually writes; the original
-- (score, decision, reasons) shape silently rejected every insert.
CREATE TABLE bid_scores (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    compliance_rate FLOAT,
    domain_match FLOAT,
    budget_alignment FLOAT,
    past_win_rate FLOAT,
    capability_depth FLOAT,
    overall_score FLOAT NOT NULL,
    decision TEXT NOT NULL,
    score_breakdown JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- MIGRATION for existing databases (run once in the Supabase SQL editor):
-- ALTER TABLE win_scores RENAME TO bid_scores;
-- ALTER TABLE bid_scores DROP CONSTRAINT IF EXISTS win_scores_workspace_id_key;
-- ALTER TABLE bid_scores ALTER COLUMN score DROP NOT NULL;
-- ALTER TABLE bid_scores ADD COLUMN IF NOT EXISTS compliance_rate FLOAT;
-- ALTER TABLE bid_scores ADD COLUMN IF NOT EXISTS domain_match FLOAT;
-- ALTER TABLE bid_scores ADD COLUMN IF NOT EXISTS budget_alignment FLOAT;
-- ALTER TABLE bid_scores ADD COLUMN IF NOT EXISTS past_win_rate FLOAT;
-- ALTER TABLE bid_scores ADD COLUMN IF NOT EXISTS capability_depth FLOAT;
-- ALTER TABLE bid_scores ADD COLUMN IF NOT EXISTS overall_score FLOAT;
-- ALTER TABLE bid_scores ADD COLUMN IF NOT EXISTS score_breakdown JSONB;
-- (legacy 'score' and 'reasons' columns can stay; new code ignores them)
