-- =============================================================
-- SoP Implementation Engine — Supabase Schema
-- Run this in the Supabase SQL Editor (Dashboard > SQL Editor)
-- =============================================================

-- ---------------------------------------------------------------
-- SILVER LAYER
-- ---------------------------------------------------------------

-- Core block table (loaded from bowie_features_scores_tracts_acs.geojson)
CREATE TABLE IF NOT EXISTS blocks (
    id              BIGINT PRIMARY KEY,     -- = sgmntid from GeoJSON
    street_name     TEXT NOT NULL,
    intersection_a  TEXT,
    intersection_b  TEXT,
    neighborhood    TEXT DEFAULT 'Bowie Town Center',

    -- SoP current scores
    sop_index       NUMERIC,
    sop_index_norm  NUMERIC,
    form_score      NUMERIC,
    density_score   NUMERIC,
    proximity_score NUMERIC,
    connectivity_score NUMERIC,
    parks_score     NUMERIC,
    pedestrian_score NUMERIC,
    safety_score    NUMERIC,
    traffic_score   NUMERIC,
    aesthetics_score NUMERIC,

    -- Census tract + ACS demographics (pre-joined in GeoJSON)
    tract_geoid     TEXT,
    tract_name      TEXT,
    total_pop       INTEGER,
    median_hh_income NUMERIC,
    median_home_value NUMERIC,
    commuters_total  INTEGER,
    commuters_transit INTEGER,
    renter_no_vehicle INTEGER,

    -- All 125 SoP features — current observed state (_x)
    features_current     JSONB,
    -- All 125 SoP features — recommended target state (_y)
    features_recommended JSONB,

    -- GeoJSON LineString geometry (upgrade to PostGIS GEOMETRY later for ZoningAgent)
    geometry_geojson JSONB,

    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Ranked recommendations per block (loaded from OrderedBlockwiseRecs CSV)
CREATE TABLE IF NOT EXISTS block_recommendations (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    block_id    BIGINT REFERENCES blocks(id) ON DELETE CASCADE,
    rec_rank    INTEGER,
    rec_label   TEXT NOT NULL,      -- e.g. "Public Garden"
    dimension   TEXT,               -- e.g. "Parks & Public Spaces"
    predicted_score_increase NUMERIC,
    direction   TEXT CHECK (direction IN ('Increase', 'Decrease')),
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (block_id, rec_rank, dimension)
);

-- ---------------------------------------------------------------
-- SIGNAL TABLES (written by specialist agents)
-- ---------------------------------------------------------------

CREATE TABLE IF NOT EXISTS funding_signals (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    block_id        BIGINT REFERENCES blocks(id) ON DELETE CASCADE,
    program_name    TEXT NOT NULL,
    program_type    TEXT,           -- 'federal' | 'state' | 'local'
    source_agency   TEXT,
    award_amount_min NUMERIC,
    award_amount_max NUMERIC,
    deadline        DATE,
    eligibility_notes TEXT,
    relevance_score NUMERIC CHECK (relevance_score BETWEEN 0 AND 1),
    application_url TEXT,
    raw_data        JSONB,
    thread_id       TEXT,
    reviewed        BOOLEAN DEFAULT FALSE,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (block_id, program_name)  -- idempotency: safe to re-run agent
);

CREATE TABLE IF NOT EXISTS zoning_signals (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    block_id        BIGINT REFERENCES blocks(id) ON DELETE CASCADE,
    parcel_id       TEXT,
    current_zone    TEXT,
    zone_description TEXT,
    allows_parks    BOOLEAN,
    allows_mixed_use BOOLEAN,
    variance_required BOOLEAN,
    barrier_notes   TEXT,
    raw_data        JSONB,
    thread_id       TEXT,
    reviewed        BOOLEAN DEFAULT FALSE,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (block_id, parcel_id)
);

CREATE TABLE IF NOT EXISTS policy_signals (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    block_id        BIGINT REFERENCES blocks(id) ON DELETE CASCADE,
    policy_name     TEXT NOT NULL,
    policy_type     TEXT,           -- 'plan' | 'ordinance' | 'program'
    stance          TEXT CHECK (stance IN ('supporting', 'blocking', 'neutral')),
    excerpt         TEXT,
    source_url      TEXT,
    relevance_score NUMERIC CHECK (relevance_score BETWEEN 0 AND 1),
    raw_data        JSONB,
    thread_id       TEXT,
    reviewed        BOOLEAN DEFAULT FALSE,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (block_id, policy_name)
);

-- ---------------------------------------------------------------
-- GOLD LAYER (computed by Orchestrator after all signals are reviewed)
-- ---------------------------------------------------------------

CREATE TABLE IF NOT EXISTS block_implementation_profiles (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    block_id        BIGINT REFERENCES blocks(id) ON DELETE CASCADE UNIQUE,
    feasibility_score  NUMERIC CHECK (feasibility_score BETWEEN 0 AND 100),
    funding_score      NUMERIC,
    zoning_score       NUMERIC,
    policy_score       NUMERIC,
    top_actions        JSONB,   -- ordered list of action strings
    narrative          TEXT,
    funding_signal_ids JSONB,   -- [1, 2, 3]
    zoning_signal_ids  JSONB,
    policy_signal_ids  JSONB,
    thread_id          TEXT,
    computed_at        TIMESTAMPTZ DEFAULT NOW()
);

-- ---------------------------------------------------------------
-- INDEXES
-- ---------------------------------------------------------------

CREATE INDEX IF NOT EXISTS idx_blocks_tract_geoid   ON blocks(tract_geoid);
CREATE INDEX IF NOT EXISTS idx_blocks_neighborhood  ON blocks(neighborhood);
CREATE INDEX IF NOT EXISTS idx_block_recs_block_id  ON block_recommendations(block_id);
CREATE INDEX IF NOT EXISTS idx_funding_block_id     ON funding_signals(block_id);
CREATE INDEX IF NOT EXISTS idx_funding_reviewed     ON funding_signals(reviewed);
CREATE INDEX IF NOT EXISTS idx_zoning_block_id      ON zoning_signals(block_id);
CREATE INDEX IF NOT EXISTS idx_policy_block_id      ON policy_signals(block_id);
CREATE INDEX IF NOT EXISTS idx_profiles_block_id    ON block_implementation_profiles(block_id);

-- =============================================================
-- FUNDING DATA SOURCES  (added Sep 2026)
--
-- Everything below is additive and idempotent, so this whole file stays safe
-- to re-run against a database that already holds the Bowie data.
--
-- Why these tables exist: funding programs used to be a hardcoded Python list
-- and eligibility was inferred by the LLM. Programs are now synced from
-- Grants.gov and eligibility is a per-tract determination from a published
-- dataset. Agents read these tables — never a live third-party API — so a slow
-- or unavailable upstream cannot break a demo.
-- =============================================================

-- ---------------------------------------------------------------
-- SILVER: funding program catalog (synced, not hand-written)
-- ---------------------------------------------------------------

CREATE TABLE IF NOT EXISTS funding_programs (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    -- Stable synthetic key, e.g. 'grants.gov:362715' or 'md-pos'.
    program_key     TEXT UNIQUE NOT NULL,
    program_name    TEXT NOT NULL,
    program_type    TEXT,            -- 'federal' | 'state' | 'local'
    source_agency   TEXT,

    award_amount_min  NUMERIC,       -- Grants.gov awardFloor
    award_amount_max  NUMERIC,       -- Grants.gov awardCeiling
    estimated_funding NUMERIC,
    deadline          DATE,          -- Grants.gov closeDate / responseDate
    eligibility_notes TEXT,
    application_url   TEXT,
    cfda_numbers      JSONB,

    -- When set (e.g. 'NMTC'), a block only qualifies if tract_eligibility
    -- confirms that designation for its census tract.
    requires_tract_designation TEXT,

    -- TRUE = hand-maintained entry (state/local programs absent from
    -- Grants.gov). Keeps curated rows honestly distinguishable from synced ones.
    is_curated      BOOLEAN DEFAULT FALSE,

    -- Provenance: answers "where did this come from?" per row.
    source_name     TEXT NOT NULL,
    source_url      TEXT,
    source_vintage  TEXT,
    fetched_at      TIMESTAMPTZ DEFAULT NOW(),

    raw_data        JSONB,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

-- ---------------------------------------------------------------
-- SILVER: per-tract funding designations
--
-- The minimal form of the geo_lookup junction table. Keyed on census tract
-- rather than block geometry because blocks already carry tract_geoid, so no
-- PostGIS spatial join is needed yet.
-- ---------------------------------------------------------------

CREATE TABLE IF NOT EXISTS tract_eligibility (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tract_geoid     TEXT NOT NULL,
    program_key     TEXT NOT NULL,   -- designation key, e.g. 'NMTC'
    -- NULL means "could not determine" — distinct from FALSE ("determined not
    -- eligible"). A lookup failure must never be recorded as ineligible.
    eligible        BOOLEAN,
    basis           TEXT,            -- human-readable reason for the verdict

    source_name     TEXT,
    source_url      TEXT,
    source_vintage  TEXT,
    fetched_at      TIMESTAMPTZ DEFAULT NOW(),

    raw_data        JSONB,
    UNIQUE (tract_geoid, program_key)
);

-- ---------------------------------------------------------------
-- Provenance on the signals the agent writes
-- ---------------------------------------------------------------

ALTER TABLE funding_signals
    ADD COLUMN IF NOT EXISTS source_name           TEXT,
    ADD COLUMN IF NOT EXISTS source_url            TEXT,
    ADD COLUMN IF NOT EXISTS source_vintage        TEXT,
    ADD COLUMN IF NOT EXISTS fetched_at            TIMESTAMPTZ,
    -- TRUE/FALSE from a deterministic rule or dataset lookup; NULL when the
    -- program carries no hard eligibility test.
    ADD COLUMN IF NOT EXISTS eligibility_confirmed BOOLEAN,
    ADD COLUMN IF NOT EXISTS determination_basis   TEXT;

CREATE INDEX IF NOT EXISTS idx_funding_programs_key   ON funding_programs(program_key);
CREATE INDEX IF NOT EXISTS idx_funding_programs_type  ON funding_programs(program_type);
CREATE INDEX IF NOT EXISTS idx_tract_elig_tract       ON tract_eligibility(tract_geoid);
CREATE INDEX IF NOT EXISTS idx_tract_elig_program     ON tract_eligibility(program_key);

-- ---------------------------------------------------------------
-- LIVE EXTRACTION PROVENANCE  (added Sep 2026)
--
-- Programs now enter the silver layer through a user-driven discovery session:
-- the user supplies the source, the agent extracts a draft, the user reviews and
-- edits it, and only then is it written. These columns record how a row got here
-- and how much to trust it.
--
-- is_curated is DEPRECATED — it marked hand-written entries, which no longer
-- exist. The column is left in place rather than dropped so this file stays
-- non-destructive; nothing writes it any more.
-- ---------------------------------------------------------------

ALTER TABLE funding_programs
    -- TRUE when the row came from a live extraction session.
    ADD COLUMN IF NOT EXISTS is_extracted          BOOLEAN DEFAULT FALSE,
    -- Model's own confidence, 0-1. Low confidence is not an error — it is a
    -- prompt to the reviewer to check the excerpt.
    ADD COLUMN IF NOT EXISTS extraction_confidence NUMERIC,
    -- The verbatim span the values were read from. Makes a claim checkable
    -- against the live page instead of taken on trust.
    ADD COLUMN IF NOT EXISTS source_excerpt        TEXT,
    -- Geographic reach the program applies to, confirmed by the user:
    -- 'federal' | 'state:MD' | 'county:24033' | 'tract:NMTC' etc.
    ADD COLUMN IF NOT EXISTS geo_scope             TEXT,
    -- FALSE would mean it reached the table without human review; the discovery
    -- flow cannot produce that, so it doubles as an invariant check.
    ADD COLUMN IF NOT EXISTS reviewed_by_user      BOOLEAN DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS discovery_thread_id   TEXT;

CREATE INDEX IF NOT EXISTS idx_funding_programs_thread ON funding_programs(discovery_thread_id);


-- ---------------------------------------------------------------
-- PER-RECOMMENDATION FUNDING FIT  (added Sep 2026)
--
-- A funding signal used to be "this program suits this block". The unit is now
-- "this program could fund this specific recommendation", because that is the
-- question a planner actually asks and the only one a narrative can answer
-- concretely. One block with five recommendations produces five independent
-- sets of fits, each approved on its own.
--
-- This is the one DESTRUCTIVE statement in this file: the old
-- UNIQUE (block_id, program_name) has to go, or two recommendations that both
-- match the same program would collide and the second write would overwrite the
-- first. Rows written before this migration carry rec_id NULL.
-- ---------------------------------------------------------------

ALTER TABLE funding_signals
    ADD COLUMN IF NOT EXISTS rec_id BIGINT
        REFERENCES block_recommendations(id) ON DELETE CASCADE,
    -- The rec_label as it stood when assessed. Denormalised on purpose: the
    -- narrative below refers to it, so the row stays readable even if the
    -- recommendation is later re-ranked or relabelled.
    ADD COLUMN IF NOT EXISTS rec_label TEXT;

ALTER TABLE funding_signals
    DROP CONSTRAINT IF EXISTS funding_signals_block_id_program_name_key;

-- Guarded because ADD CONSTRAINT has no IF NOT EXISTS in Postgres, and this
-- file is meant to be re-runnable. Also skipped once the per-analysis key
-- below exists: that key replaces this one, and re-adding this one would fail
-- as soon as two analyses of a block hold the same match.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname IN ('funding_signals_block_rec_program_key',
                          'funding_signals_analysis_rec_program_key')
    ) THEN
        ALTER TABLE funding_signals
            ADD CONSTRAINT funding_signals_block_rec_program_key
            UNIQUE (block_id, rec_id, program_name);
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_funding_signals_rec ON funding_signals(rec_id);


-- ---------------------------------------------------------------
-- DISCOVERY SESSION HISTORY  (added Sep 2026)
--
-- One append-only row per completed discovery session, so past sessions stay
-- browsable and re-runnable. The agent's checkpointer is in-memory and only
-- holds sessions that are still paused, so without this a finished session
-- left no record of itself beyond the programs it wrote.
--
-- Why not just group funding_programs by discovery_thread_id: program_key is
-- the upsert conflict target, so re-running a session over the same sources
-- moves those rows onto the newer thread_id and the older session appears to
-- have written nothing. A session's history must not be rewritten by a later
-- one. `programs` holds the snapshot of what THIS session wrote, at the values
-- it wrote them; the live catalogue is still the place to read current values.
--
-- Sessions that ran before this table existed are recovered by grouping
-- funding_programs instead — see FundingProgramRepository.sessions_from_catalogue.
-- ---------------------------------------------------------------

CREATE TABLE IF NOT EXISTS discovery_sessions (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    thread_id       TEXT UNIQUE NOT NULL,
    goal            TEXT NOT NULL,
    -- The sources extraction actually read, which is not necessarily the list
    -- the session started with: the research stage lets the user replace it.
    sources         JSONB,
    requested_sources JSONB,
    -- Snapshot of the programs written, one object per program.
    programs        JSONB,
    program_count   INTEGER DEFAULT 0,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_discovery_sessions_created ON discovery_sessions(created_at DESC);


-- ---------------------------------------------------------------
-- IMPLEMENTATION PLANS  (added Nov 2026)
--
-- The steps to get one recommendation done, as a reviewer (an SME) saved them.
-- A plan starts from its pathway template in backend/data/pathways.json; a row
-- appears here only once someone saves an edited or finalised version.
--
-- Funding is deliberately NOT stored in `steps`. It is attached on read from
-- funding_signals, so money approved after a plan was finalised still shows.
-- template_version records which template the reviewer started from, so a
-- plan saved against an older template stays recognisable as such.
-- ---------------------------------------------------------------

CREATE TABLE IF NOT EXISTS implementation_plans (
    id               BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    block_id         BIGINT REFERENCES blocks(id) ON DELETE CASCADE,
    rec_id           BIGINT REFERENCES block_recommendations(id) ON DELETE CASCADE,
    rec_label        TEXT,
    pathway_key      TEXT NOT NULL,
    template_version TEXT,
    -- [{id, title, how, actor, owner, time, depends_on, depends_mode, condition,
    --   filled_by, edited}], in order (older rows: {title, owner, waits_on, time ...})
    steps            JSONB NOT NULL,
    status           TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'final')),
    reviewed_by      TEXT,
    notes            TEXT,
    finalized_at     TIMESTAMPTZ,
    created_at       TIMESTAMPTZ DEFAULT NOW(),
    updated_at       TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (block_id, rec_id)
);

CREATE INDEX IF NOT EXISTS idx_plans_block_id ON implementation_plans(block_id);


-- ---------------------------------------------------------------
-- IMPLEMENTATION PLAN REVISIONS  (added Nov 22 2026)
--
-- The audit trail of a plan. An SME's edits are saved as a draft; nothing
-- else reads a draft until someone approves it, and only then is
-- implementation_plans (what the block page reads) updated.
-- Every draft saved, approval and discarded draft is one append-only row here,
-- with the steps as they stood and what changed against the approved version
-- (or the playbook, before the first approval).
--
-- Run this section once in the Supabase SQL editor. It is safe to run again.
-- ---------------------------------------------------------------

CREATE TABLE IF NOT EXISTS implementation_plan_revisions (
    id               BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    block_id         BIGINT REFERENCES blocks(id) ON DELETE CASCADE,
    rec_id           BIGINT REFERENCES block_recommendations(id) ON DELETE CASCADE,
    rec_label        TEXT,
    -- 1, 2, 3 ... per (block, recommendation).
    revision         INT NOT NULL,
    action           TEXT NOT NULL CHECK (action IN ('draft', 'approved', 'discarded')),
    -- [{id, title, how, actor, owner, time, depends_on, depends_mode, condition,
    --   filled_by, edited}], in order. NULL on a discarded draft.
    steps            JSONB,
    -- {added, changed, removed, count} against the approved steps at the time.
    changes          JSONB,
    -- The approved revision a draft started from; NULL = the playbook.
    base_revision    INT,
    pathway_key      TEXT,
    template_version TEXT,
    made_by          TEXT NOT NULL,
    notes            TEXT,
    created_at       TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (block_id, rec_id, revision)
);

CREATE INDEX IF NOT EXISTS idx_plan_revisions_rec ON implementation_plan_revisions(block_id, rec_id, revision DESC);

-- The approved revision now in implementation_plans.
ALTER TABLE implementation_plans ADD COLUMN IF NOT EXISTS revision INT;


-- ---------------------------------------------------------------
-- PATHWAY TEMPLATES  (added Nov 2026)
--
-- The pathway templates as edited in the app's Playbooks tab. Every save is a
-- new row; the highest version is the one in force, and older rows are the
-- history a reviewer can restore from. With no rows (or no table), the app
-- falls back to backend/data/pathways.json, which is version v0.
--
-- `doc` holds {status, pathways, rec_types}. The filled_by sources are not
-- stored: they describe what the code can automate, so they always come from
-- the file.
-- ---------------------------------------------------------------

CREATE TABLE IF NOT EXISTS pathway_templates (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    version     INT NOT NULL UNIQUE,
    doc         JSONB NOT NULL,
    updated_by  TEXT NOT NULL,
    note        TEXT,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

-- =============================================================
-- ZONING DATA VERSIONS AND REFRESH RUNS  (added Nov 2026)
--
-- The Zoning tab can re-extract the use table and re-look-up block zones from
-- sources the user names. A refresh never changes anything on its own: it runs
-- in the background into zoning_refresh_runs, the user reviews the diff, and
-- only "apply" writes a new zoning_data_versions row (and the blocks' rows in
-- zoning_signals). The hosted server's disk is wiped on redeploy, so the data
-- in force lives here, not in a file.
--
-- With no versions (or no tables), the app uses backend/data/zoning_use_table.json
-- and the default GIS settings, which together are version 0.
-- =============================================================

-- The zoning data in force: the highest version wins. Insert-only, so every
-- earlier version stays restorable.
CREATE TABLE IF NOT EXISTS zoning_data_versions (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    version     INT NOT NULL UNIQUE,
    use_table   JSONB NOT NULL,     -- same shape as zoning_use_table.json
    settings    JSONB NOT NULL,     -- GIS layers, zone field, buffer
    applied_by  TEXT NOT NULL,
    note        TEXT,
    run_id      BIGINT,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

-- One row per refresh the user starts. status: running -> ready (diff awaits
-- review) -> applied | discarded, or failed.
CREATE TABLE IF NOT EXISTS zoning_refresh_runs (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    status        TEXT NOT NULL DEFAULT 'running'
                  CHECK (status IN ('running', 'ready', 'failed', 'applied', 'discarded')),
    inputs        JSONB NOT NULL,
    requested_by  TEXT NOT NULL,
    note          TEXT,
    progress      JSONB,            -- {done, total, step}
    diff          JSONB,            -- what the refresh would change
    proposed      JSONB,            -- the extracted use table and block lookups
    error         TEXT,
    reviewed_by   TEXT,
    applied_version INT,
    created_at    TIMESTAMPTZ DEFAULT NOW(),
    finished_at   TIMESTAMPTZ,
    reviewed_at   TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_zoning_refresh_runs_created ON zoning_refresh_runs(created_at DESC);

-- Added Nov 19 2026: a zoning pipeline publishes the recommendation readings
-- (and the rest of zoning_rules.json) with the use table, so the rules in
-- force can come from a version rather than only the committed file. NULL =
-- the version uses backend/data/zoning_rules.json.
ALTER TABLE zoning_data_versions ADD COLUMN IF NOT EXISTS rules JSONB;


-- =============================================================
-- DATASET VERSIONS AND BLOCK ANALYSES  (added Nov 2026)
--
-- Every approved write to the funding catalogue or the zoning data (a
-- discovery session, a zoning refresh, the zoning sync script, and later a
-- pipeline run) snapshots the whole dataset as a new, numbered, immutable
-- version: Funding v1, v2 ... and Zoning v1, v2 ... A version may carry a name.
--
-- A block analysis (Block Funding or Block Zoning) records which version it
-- ran against, and can be re-run against any version at any time. Each block
-- has at most one pinned analysis per kind; plans, signals and the gold layer
-- read the pinned one, so a newer dataset version never silently changes a
-- block until someone re-runs it.
--
-- The working tables (funding_programs, zoning_signals, zoning_data_versions)
-- are unchanged and stay the data the Zoning tab and discovery work on.
-- =============================================================

CREATE TABLE IF NOT EXISTS dataset_versions (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    kind          TEXT NOT NULL CHECK (kind IN ('funding', 'zoning', 'general')),
    version       INT NOT NULL,
    name          TEXT,                -- optional, e.g. "Nov 18 MD + federal"
    note          TEXT,
    -- What produced it: {type: 'seed' | 'discovery' | 'zoning_refresh' |
    -- 'zoning_sync' | 'pipeline', ...ids}
    source        JSONB,
    record_count  INT DEFAULT 0,
    summary       JSONB,               -- counts for list views
    created_by    TEXT,
    created_at    TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (kind, version)
);

-- The snapshot itself. Funding: one 'program' row per funding_programs row.
-- Zoning: one 'use_table', one 'rules', one 'settings', and one 'block_zone'
-- row per zoning_signals row.
CREATE TABLE IF NOT EXISTS dataset_records (
    id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    dataset_version_id  BIGINT NOT NULL REFERENCES dataset_versions(id) ON DELETE CASCADE,
    record_type         TEXT NOT NULL,
    record_key          TEXT NOT NULL,
    data                JSONB NOT NULL,
    UNIQUE (dataset_version_id, record_type, record_key)
);

CREATE INDEX IF NOT EXISTS idx_dataset_records_version ON dataset_records(dataset_version_id, record_type);

-- One row per Block Funding / Block Zoning run. status: running -> in_review
-- (funding, awaiting approval) -> complete, or failed / discarded.
CREATE TABLE IF NOT EXISTS block_analyses (
    id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    block_id            BIGINT NOT NULL REFERENCES blocks(id) ON DELETE CASCADE,
    kind                TEXT NOT NULL CHECK (kind IN ('funding', 'zoning')),
    dataset_version_id  BIGINT REFERENCES dataset_versions(id),
    dataset_version     INT,           -- denormalised for display
    name                TEXT,
    note                TEXT,
    status              TEXT NOT NULL DEFAULT 'running'
                        CHECK (status IN ('running', 'in_review', 'complete', 'failed', 'discarded')),
    thread_id           TEXT,
    result              JSONB,
    error               TEXT,
    pinned              BOOLEAN NOT NULL DEFAULT FALSE,
    created_by          TEXT,
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    completed_at        TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_block_analyses_block ON block_analyses(block_id, kind, created_at DESC);
-- At most one pinned analysis per block and kind.
CREATE UNIQUE INDEX IF NOT EXISTS uq_block_analyses_pinned ON block_analyses(block_id, kind) WHERE pinned;

-- Funding signals belong to the analysis that wrote them, so re-running a
-- block keeps the earlier run's signals instead of overwriting them.
ALTER TABLE funding_signals
    ADD COLUMN IF NOT EXISTS analysis_id BIGINT REFERENCES block_analyses(id) ON DELETE CASCADE;

CREATE INDEX IF NOT EXISTS idx_funding_signals_analysis ON funding_signals(analysis_id);

-- DESTRUCTIVE (constraint only, no data): the per-(block, rec, program) key
-- would stop two analyses of one block from both holding the same match.
ALTER TABLE funding_signals
    DROP CONSTRAINT IF EXISTS funding_signals_block_rec_program_key;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'funding_signals_analysis_rec_program_key'
    ) THEN
        ALTER TABLE funding_signals
            ADD CONSTRAINT funding_signals_analysis_rec_program_key
            UNIQUE (analysis_id, rec_id, program_name);
    END IF;
END $$;

-- Which analyses a gold profile was computed from.
ALTER TABLE block_implementation_profiles
    ADD COLUMN IF NOT EXISTS funding_analysis_id BIGINT,
    ADD COLUMN IF NOT EXISTS zoning_analysis_id  BIGINT;


-- =============================================================
-- PIPELINES  (added Nov 2026)
--
-- Chat-built data pipelines (Pipeline Development tab). The user states the
-- fields to extract and supplies sources; the agent proposes steps from a
-- fixed catalog (backend/pipelines/engine.py); the user approves each one.
-- A pipeline can be re-run on demand. Loading a run writes the working table
-- (funding_programs for a funding pipeline) and snapshots it as the next
-- dataset version, so block analyses can be tied to it.
-- =============================================================

CREATE TABLE IF NOT EXISTS pipelines (
    id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    kind                TEXT NOT NULL CHECK (kind IN ('funding', 'zoning', 'general')),
    name                TEXT NOT NULL,
    -- {schema: {status, fields: [...]}, sources: [...], steps: [...]}
    definition          JSONB NOT NULL DEFAULT '{}'::jsonb,
    -- Bumped when a changed definition is loaded: "def v3".
    definition_version  INT NOT NULL DEFAULT 0,
    -- Last run's outputs per step, source fetch results, LLM step cache.
    state               JSONB NOT NULL DEFAULT '{}'::jsonb,
    -- 'turn' or 'run' while the agent or a run is working; NULL when idle.
    busy                TEXT,
    busy_since          TIMESTAMPTZ,
    created_by          TEXT,
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    updated_at          TIMESTAMPTZ DEFAULT NOW()
);

-- The chat. role 'user' / 'assistant' rows hold the exact API content blocks
-- (sent back to the model unchanged); 'event' rows are app notices shown in
-- the chat only (e.g. "You approved step 2").
CREATE TABLE IF NOT EXISTS pipeline_messages (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    pipeline_id  BIGINT NOT NULL REFERENCES pipelines(id) ON DELETE CASCADE,
    role         TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'event')),
    content      JSONB NOT NULL,
    created_at   TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_pipeline_messages_pipeline ON pipeline_messages(pipeline_id, id);

-- One row per Load: what was written and which dataset version it became.
CREATE TABLE IF NOT EXISTS pipeline_runs (
    id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    pipeline_id         BIGINT NOT NULL REFERENCES pipelines(id) ON DELETE CASCADE,
    definition_version  INT NOT NULL,
    definition          JSONB NOT NULL,
    name                TEXT,
    records             JSONB,
    coverage            JSONB,
    diff                JSONB,
    dataset_version_id  BIGINT REFERENCES dataset_versions(id),
    dataset_version     INT,
    created_by          TEXT,
    created_at          TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_pipeline_runs_pipeline ON pipeline_runs(pipeline_id, id DESC);


-- =============================================================
-- GENERAL PIPELINES  (added Nov 2026)
--
-- A general pipeline has a free schema and writes each output into a table of
-- its own, named gp_<name>, never into the app's tables. It writes only
-- through gp_publish below, which runs every output of one publish in one
-- transaction: it creates the table (or adds the columns a new field needs),
-- optionally deletes this pipeline's earlier rows (mode replace), then
-- inserts the rows, updating any with the same _key (mode merge).
--
-- Run this whole section once in the Supabase SQL editor. It is safe to run
-- again.
-- =============================================================

-- Allow the 'general' kind on databases created before it existed. Any CHECK
-- on kind is replaced, whatever it was named.
DO $$
DECLARE c record;
BEGIN
    FOR c IN SELECT conrelid::regclass AS tbl, conname FROM pg_constraint
             WHERE contype = 'c'
               AND conrelid IN ('pipelines'::regclass, 'dataset_versions'::regclass)
               AND pg_get_constraintdef(oid) LIKE '%kind%'
    LOOP
        EXECUTE format('ALTER TABLE %s DROP CONSTRAINT %I', c.tbl, c.conname);
    END LOOP;
END $$;
ALTER TABLE pipelines
    ADD CONSTRAINT pipelines_kind_check CHECK (kind IN ('funding', 'zoning', 'general'));
ALTER TABLE dataset_versions
    ADD CONSTRAINT dataset_versions_kind_check CHECK (kind IN ('funding', 'zoning', 'general'));

-- p_writes: [{table, replace, columns: [{name, type}], rows: [{column: value}]}].
-- Every row carries the same keys, including _key, _pipeline_id, _record_id,
-- _source_url, _evidence and _published_at.
-- Returns one {table, created, added_columns, deleted, written} per write.
CREATE OR REPLACE FUNCTION gp_publish(p_writes JSONB, p_pipeline_id BIGINT)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    w JSONB;
    col JSONB;
    t TEXT;
    cols TEXT;
    sets TEXT;
    created BOOLEAN;
    added TEXT[];
    deleted INT;
    written INT;
    result JSONB := '[]'::jsonb;
BEGIN
    FOR w IN SELECT * FROM jsonb_array_elements(p_writes) LOOP
        t := w->>'table';
        IF t IS NULL OR t !~ '^gp_[a-z][a-z0-9_]{0,50}$' THEN
            RAISE EXCEPTION 'General pipelines write only to tables named gp_<lower_snake_case>, not %', t;
        END IF;

        created := to_regclass(format('public.%I', t)) IS NULL;
        IF created THEN
            EXECUTE format('CREATE TABLE public.%I (
                _key          TEXT PRIMARY KEY,
                _pipeline_id  BIGINT,
                _record_id    TEXT,
                _source_url   TEXT,
                _evidence     JSONB,
                _published_at TIMESTAMPTZ DEFAULT NOW())', t);
            -- Read and written by the backend (service role) only, not with the public key.
            EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY', t);
        END IF;

        added := '{}';
        FOR col IN SELECT * FROM jsonb_array_elements(COALESCE(w->'columns', '[]'::jsonb)) LOOP
            IF (col->>'name') !~ '^[a-z][a-z0-9_]{0,40}$' THEN
                RAISE EXCEPTION 'Column name % must be lower_snake_case', col->>'name';
            END IF;
            IF (col->>'type') NOT IN ('text', 'double precision', 'date', 'boolean') THEN
                RAISE EXCEPTION 'Column type % is not one a general pipeline writes', col->>'type';
            END IF;
            IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                           WHERE table_schema = 'public' AND table_name = t AND column_name = col->>'name') THEN
                EXECUTE format('ALTER TABLE public.%I ADD COLUMN %I %s', t, col->>'name', col->>'type');
                added := added || (col->>'name');
            END IF;
        END LOOP;

        deleted := 0;
        IF COALESCE((w->>'replace')::boolean, FALSE) THEN
            EXECUTE format('DELETE FROM public.%I WHERE _pipeline_id = $1', t) USING p_pipeline_id;
            GET DIAGNOSTICS deleted = ROW_COUNT;
        END IF;

        written := 0;
        IF jsonb_array_length(COALESCE(w->'rows', '[]'::jsonb)) > 0 THEN
            SELECT string_agg(quote_ident(k), ', '),
                   string_agg(format('%I = EXCLUDED.%I', k, k), ', ') FILTER (WHERE k <> '_key')
              INTO cols, sets
              FROM jsonb_object_keys(w->'rows'->0) AS k;
            EXECUTE format('INSERT INTO public.%I (%s) SELECT %s FROM jsonb_populate_recordset(NULL::public.%I, $1) '
                           'ON CONFLICT (_key) DO UPDATE SET %s', t, cols, cols, t, sets)
                USING w->'rows';
            GET DIAGNOSTICS written = ROW_COUNT;
        END IF;

        result := result || jsonb_build_object('table', t, 'created', created, 'added_columns', to_jsonb(added),
                                               'deleted', deleted, 'written', written);
    END LOOP;
    -- So the API sees a new table or column at once.
    NOTIFY pgrst, 'reload schema';
    RETURN result;
END;
$$;

-- The function creates tables, so only the backend's service role may call it.
REVOKE ALL ON FUNCTION gp_publish(JSONB, BIGINT) FROM PUBLIC;
REVOKE ALL ON FUNCTION gp_publish(JSONB, BIGINT) FROM anon, authenticated;
GRANT EXECUTE ON FUNCTION gp_publish(JSONB, BIGINT) TO service_role;
