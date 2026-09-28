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
