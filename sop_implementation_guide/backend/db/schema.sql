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
-- file is meant to be re-runnable.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'funding_signals_block_rec_program_key'
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
