# HANDOFF — resume here

> Updated Sep 29, 2026, after a dead-code pass. Read this before `START-HERE.md` and
> `project-overview.md`, both of which are stale (see section 8).
>
> **Midterm is Wed Oct 1.** Two days out.
>
> Everything in sections 1 and 3 was checked against the live Supabase instance on
> Sep 29. Claims inherited from the previous handoff that I did **not** re-verify are
> marked *(unverified)*.

---

## 1. THE MAIN FLOW — read this first

Two user-driven stages. Nothing runs unattended; a human confirms every write.

```
STAGE A — funding discovery          (fills the catalogue, not block-specific)
  user pastes goal + source URLs
    → load_sources   fetch the pages as raw text      [RAW]
    → clarify   ⏸    agent asks grounded questions
    → research  ⏸    what each source actually contains; adjust the source list
    → extract   ⏸    draft programmes, excerpts verified against fetched text
    → geo_tie   ⏸    recommend geographic reach, user overrides
    → write_silver   → funding_programs               [SILVER]

STAGE B — per-block analysis         (scores one block against the catalogue)
  user clicks a block in the dev console
    → POST /blocks/{id}/analyze      FundingAgent: catalogue × determinations
    → GET  /threads/{tid}/review ⏸   human review of the ranked signals
    → POST /threads/{tid}/approve    → funding_signals                [SILVER]
    → POST /threads/{tid}/gold       → block_implementation_profiles  [GOLD]
```

⏸ = LangGraph `interrupt()`. The session blocks until the user responds.

**The design rule that must survive:** logic lives in the prompts and in user input, not
in Python branching. Every discovery prompt is a module constant at the top of
`backend/agents/funding_discovery.py` so it can be read and edited without digging
through code. If a node starts growing keyword lists or scoring rules, the black box the
user rejected is coming back. Same for `grants_gov.py` — it is a *protocol adapter only*
(Grants.gov needs POST, so the generic GET fetcher cannot read it), never a privileged
source with opinions about relevance.

---

## 2. CURRENT BLOCKER — `tract_eligibility` is empty

Verified Sep 29. Live row counts:

| Table | Rows | |
|---|---:|---|
| `blocks` | 68 | ✅ |
| `block_recommendations` | 1506 | ✅ |
| `funding_programs` | 2 | ✅ both extracted + user-reviewed |
| `tract_eligibility` | **0** | ❌ **this is the blocker** |
| `funding_signals` | 2 | |
| `block_implementation_profiles` | 2 | |

`FundingAgent.gather_signals` reads per-tract designations from `tract_eligibility`. With
the table empty, every NMTC-gated programme resolves to **UNDETERMINED** instead of
CONFIRMED ELIGIBLE, so `_score_funding` never awards `points_confirmed_eligible` and the
block-14-vs-69 demo contrast collapses. The CDBG income rule still works — it is computed
from the block's own ACS data, not from this table.

**Fix (needs network, ~1 min):**

```bash
cd sop_implementation_guide
.venv/Scripts/python.exe -m scripts.sync_tract_eligibility
```

This is almost certainly the aftermath of `reset_and_reload.py` being run **without**
`--with-tracts` — that script wipes all eight tables and its own closing message tells you
to run the tract sync as step 2. Easy to miss. If you reset again, prefer
`--confirm --with-tracts`.

The schema blocker from the previous handoff is **resolved**: `funding_programs` now has
27 columns including `discovery_thread_id`, so `schema.sql` was re-run successfully.

---

## 3. What is verified working

**Checked Sep 29 by me:**
- App boots; **all 11 routes resolve** (`backend.main:app`).
- `schema.sql` is fully applied to Supabase — the extraction-provenance columns exist.
- **Stage A completed end-to-end at least twice.** `funding_programs` holds two rows that
  could only have come through a discovery session, both `is_extracted=true`,
  `reviewed_by_user=true`, `geo_scope=state:MD`:
  - `sustainablemaryland.com:sm-action-grants-program`
  - `mta.maryland.gov:statewide-transit-innovation-grant`
  
  Neither was ever hand-typed. The "watch the silver layer fill from nothing" demo
  **already happened** — the 4 legacy hand-typed rows and the 6 Grants.gov synced rows are
  gone, and the catalogue is now 100% user-reviewed extraction.
- Stage B has run: 2 `funding_signals` and 2 gold profiles exist.

**Inherited from the Sep 28 session** *(unverified — no live LLM session was run during
the cleanup)*:

| Stage | Evidence recorded Sep 28 |
|---|---|
| `load_sources` | Fetched MD DNR page, 6,250 chars of readable text |
| `clarify` ⏸ | 3 questions grounded in the page — caught that POS Local needs county authorisation |
| `research` ⏸ | Found **Greenspace Equity Program** ($7M FY2027) and **Local Parks and Playgrounds Infrastructure** — nobody hand-typed these. Honestly marked search-result-only URLs `usable=false` |
| `extract` ⏸ | Excerpts verified, award amounts correctly left `NULL` rather than guessed, confidence 0.92–0.95 |
| `geo_tie` ⏸ | Recommended `state:MD`, override accepted |

Two invariants worth protecting:
- **Nothing is written before the final confirmation.** An abandoned session persists
  nothing — `funding_programs` stays flat through stages 1–4.
- **The excerpt guard works.** `web_source.excerpt_is_genuine()` correctly flagged a
  programme whose excerpt did not match the fetched text. That is the line between
  extraction and invention. Keep it.

Web search is available: `web_search_20250305` works with Haiku 4.5.

---

## 4. How to run it

```bash
cd sop_implementation_guide
.venv/Scripts/python.exe -m uvicorn backend.main:app --reload --port 8000
```

Then open `frontend/dev-console/index.html` in a browser (plain file, no build step). It
has **two panels** — the block list/analysis flow, and the funding discovery panel added
in `e400d11`. The discovery panel renders whatever `stage_payload.stage` returns via a
single generic renderer, so adding or reordering a stage needs no frontend change.

**Smoke test for Stage A**, if you prefer the API directly:

1. `POST /funding/discovery` —
   `{"goal": "Find state and local funding to build a public garden on a residential block in Bowie, Maryland", "sources": ["https://dnr.maryland.gov/land/Pages/ProgramOpenSpace/home.aspx"]}`
2. `POST /funding/discovery/{thread_id}/respond` —
   `{"value": "City of Bowie via Prince George's County; development on existing city land."}`
3. `respond` `{"value": "approved"}` → extraction drafts
4. `respond` `{"value": {"programs": [...edited drafts...]}}` — **deliberately change a
   field** and confirm it survives to the DB
5. `respond` `{"value": {"geo": {"<program_name>": "county:24033"}}}` → writes to silver

Assert afterwards: the row has `is_extracted=true`, `reviewed_by_user=true`, a non-null
`source_excerpt`, the `discovery_thread_id`, and **your edited value** rather than the
model's original.

**Smoke test for Stage B:** run section 2's tract sync first, then `POST
/blocks/14/analyze` → `GET /threads/{tid}/review` → `approve` → `gold`. Block 14 should
come back CDBG- and NMTC-eligible.

---

## 5. Code map

```
backend/
  main.py                    FastAPI app; registers 3 routers
  config.py                  pydantic-settings; scoring weights + thresholds live here
  api/
    discovery.py             Stage A — 3 generic endpoints (start / get stage / respond).
                             The respond body is untyped on purpose: each stage declares
                             what it wants in its own `expects` field.
    blocks.py                Stage B — list / get / profile / analyze
    threads.py               Stage B — review / approve / gold
  agents/
    funding_discovery.py     Stage A graph. ALL PROMPTS AT TOP — edit those, not the nodes
    funding.py               Stage B graph. Two-stage: determinations (facts) then LLM
                             relevance ranking over them
    orchestrator.py          Fans out, scores, writes gold
    base.py                  SpecialistAgent ABC
    zoning.py / policy.py    Scaffolds. Return not_implemented. Post-midterm.
    deps.py                  lru_cache'd orchestrator — one checkpointer app-wide
  tools/
    web_source.py            fetch() / search() / excerpt_is_genuine(). Plain httpx
    grants_gov.py            Protocol adapter ONLY (POST). No keywords, no ranking
    llm_extractor.py         structured_call() + extract_funding_eligibility()
    cdbg.py                  Deterministic HUD LMI income rule
    tract_eligibility.py     NMTC lookup against the ArcGIS layer
  repositories/              All DB access. Agents never write SQL directly
  db/schema.sql              Re-runnable: every statement is IF NOT EXISTS
frontend/dev-console/        Single-file HTML console. No build step
scripts/
  load_bowie_data.py         GeoJSON + CSV → blocks, block_recommendations
  sync_tract_eligibility.py  NMTC designations → tract_eligibility  (see section 2)
  reset_and_reload.py        Wipe all 8 tables + reload. --confirm required
```

**Why the two agents differ:** `FundingDiscoveryAgent` is deliberately *not* a
`SpecialistAgent` — it fills the catalogue, it does not score a block. Different contract,
different base.

---

## 6. Dead code removed Sep 29 — do not resurrect

Earlier deletions (Sep 28):
- `backend/tools/funding_catalog.py` — the 4 hand-typed programmes
- `scripts/sync_funding_programs.py` — hardcoded-keyword Grants.gov sync
- `scripts/check_catalog_urls.py` — only existed to babysit hand-typed URLs

The Sep 29 pass removed **548 lines**. Nothing below had a single caller — each was
grep-verified before deletion:

- `backend/models/` — the whole package (6 files). Pydantic schemas that were never
  imported; every layer passes Supabase dicts instead. They had already drifted from
  `schema.sql` silently, precisely because nothing validated against them. If you want
  real validation, wire it into the API layer deliberately — don't restore these.
- `backend/repositories/{policy,zoning}.py` — repos for the scaffold agents. **The agents
  themselves were kept** as post-midterm markers; only the never-imported repos went.
- `scripts/clean_hand_typed_programs.py` — one-time migration, already run (its effect is
  visible in section 3: no hand-typed rows remain), and superseded by `reset_and_reload.py`.
- `grants_gov.{fetch_opportunity,opportunity_url,parse_date,_to_number,SOURCE_NAME}` —
  dead once `sync_funding_programs.py` went. Discovery needs only `handles()`, `search()`,
  `as_source_text()`, `clean_text()`.
- `web_source.fetch_many()` — discovery loops `_load_one()` itself so it can route
  Grants.gov URLs through the adapter.
- Unused repo methods: `BaseRepository.{list_all,upsert_many,delete}`,
  `FundingRepository.{get_pending_review,mark_reviewed}`,
  `FundingProgramRepository.{get_by_key,upsert_programs}`, `BlockRepository.get_by_tract`,
  and the whole `BlockRecommendationRepository` class.

**Left deliberately:** `funding_programs.is_curated`, `estimated_funding` and
`cfda_numbers` are now written by nothing, but dropping columns is a destructive migration
and `schema.sql` is meant to stay re-runnable. They are dead weight in the table, not in
the code.

---

## 7. Environment and gotchas that cost time

- **Two venvs exist and both work.** `sop_implementation_guide/.venv` (in-repo, what the
  commands in this doc use) and `C:\Users\vigne\venvs\sop` (external). The external one
  exists because the repo lives in OneDrive and OneDrive syncing `site-packages` causes
  file-lock failures during `pip install` — if you hit that, install into the external one.
  VS Code needs the interpreter set manually either way.
- **Always run from `sop_implementation_guide/`** — `config.py` loads `.env` relative to
  the working directory.
- **`MemorySaver` is still the checkpointer** for both agents. Any `--reload` destroys
  paused sessions, and `--reload` fires on every file edit. **This is the top live-demo
  risk.** Swapping to a Postgres checkpointer against Supabase is a contained change;
  `langgraph-checkpoint` 4.2.0 is already installed.
  - `OrchestratorAgent.resolve_block_id` already works around this for Stage B by falling
    back to `funding_signals.thread_id`. Stage A has no such fallback — an interrupted
    discovery session is simply lost.
- **`anthropic` 1.9.0 removed `temperature`** from `messages.create()`. Pass it as
  `extra_body={"temperature": 0}`. Needed for reproducibility — without it the same block
  scored 70 then 95 across runs. If the model is ever swapped to Opus 4.7+ or Sonnet 5,
  **remove it** — those reject sampling parameters with a 400.
- **Port 8090 is blocked by Windows** (winerror 10013, reserved range) and something else
  answers on it with a 404. Use 8000.
- **`reset_and_reload.py` leaves `tract_eligibility` empty** unless you pass
  `--with-tracts`. See section 2 — this has already bitten once.
- The IDE shows SQL errors in `schema.sql` — it parses PostgreSQL with a SQL Server
  dialect. `CREATE TABLE IF NOT EXISTS` is not T-SQL. False positives.

**Key data facts**
- NMTC layer: `NMTC_Qualified_Tracts_2025` FeatureServer, **layer id 124**, GEOID field
  `NMTC_Quali`. 122 qualified tracts in Prince George's County (24033).
- Of the 6 Bowie tracts, only **24033800520** is NMTC-qualified. *(unverified since
  `tract_eligibility` is currently empty — the sync in section 2 will re-establish it.)*
- **Demo contrast:** block 14 (Mitchellville Rd, $86,327) is CDBG *and* NMTC eligible;
  block 69 (12th St, $173,611) is neither. Funding sub-scores were 70 vs 35.
  *(Will not reproduce until section 2 is done.)*
- Scoring is tunable without code changes — see the weights and per-tier caps in
  `config.py`. `max_counted_per_tier = 2` exists because an uncapped sum hit the 100
  ceiling on almost every block and the sub-score stopped discriminating at all.

---

## 8. Corrections to older docs

`START-HERE.md` and `project-overview.md` are stale in ways that matter:

- They say "❌ Supabase project" — **wrong**, it exists, schema is applied, Bowie data is
  loaded (68 blocks, 1506 recommendations, verified Sep 29).
- They claim *"First Data Source: EPA Brownfields — federal API"*. **No Brownfields API
  was ever called.** The module named `epa_brownfields.py` contained no Brownfields client
  and has been deleted; its one useful function is now `backend/tools/cdbg.py`.
- They say 43 blocks. The GeoJSON has **68**.
- EPA EJSCREEN (`ejscreen.epa.gov`) **no longer resolves** and its result was never used;
  that call has been removed.
- They describe a Mapbox/React frontend. It does not exist — there is a single-file
  plain-HTML dev console instead, and it now includes the discovery panel.

---

## 9. Honest state of the midterm story

**What can be demonstrated today:** a live session where the user pastes a government URL,
the agent asks grounded clarifying questions, reports what the page actually contains,
extracts programmes with excerpts verified against the fetched text, recommends a
geographic tie, and the user edits everything before it is saved. Then per-block
feasibility scoring driven by real determinations (CDBG income rule, NMTC tract
designation) rather than LLM guesswork. The catalogue currently contains **only**
user-reviewed extractions — there is no hand-typed data left anywhere in the funding path.

**What cannot:** zoning and policy agents are scaffolds, so `zoning_score` and
`policy_score` are hardcoded `50.0` placeholders in `orchestrator.compute_gold` (the dev
console marks them with an asterisk). The gold layer is therefore 40% real and 60%
placeholder by weight — be upfront about that rather than letting the feasibility number
speak for itself.

**Before Wednesday, in order:**
1. Run the tract sync (section 2). Without it the strongest part of Stage B is inert.
2. Rehearse one full discovery session against the MD DNR URL. The accepted risk the user
   chose is that replacing the Grants.gov sync leaves **no non-interactive fallback** if a
   live session stumbles mid-demo.
3. Do not edit files while a demo session is paused — `--reload` will destroy it.
