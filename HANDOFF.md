# HANDOFF — resume here

> Written Sep 28, 2026, mid-implementation. Read this before `START-HERE.md`, which is
> now partly stale (see "Corrections to older docs" at the end).
>
> **Midterm is Wed Oct 1.** Three days out.

---

## 1. ONE BLOCKER — do this first, it takes 2 minutes

The funding discovery agent works end-to-end **except the final database write**, because
new columns were added to `schema.sql` but not yet applied to Supabase.

The exact error:

```
postgrest.exceptions.APIError: Could not find the 'discovery_thread_id'
column of 'funding_programs' in the schema cache
```

**Fix:** open the Supabase SQL Editor and re-run
`sop_implementation_guide/backend/db/schema.sql` in full. Every statement is
`CREATE TABLE IF NOT EXISTS` / `ADD COLUMN IF NOT EXISTS`, so it is safe against the
loaded Bowie data. It adds these to `funding_programs`:

`is_extracted`, `extraction_confidence`, `source_excerpt`, `geo_scope`,
`reviewed_by_user`, `discovery_thread_id`

Then re-run the verification in section 4. Nothing else is known to be broken.

---

## 2. What was being built, and why

The user rejected the previous funding implementation as a **black box**: sources,
search keywords, relevance rules, and four programs' worth of facts were all hardcoded in
Python where no user could see or change them. Three of five hand-written URLs in that
catalog were dead within hours.

**The design the user wants** (their words, paraphrased):

> The end-user enters websites or APIs in a LIVE session. We extract information and
> present it. The user updates/fills and ties it to the geo location — we can recommend
> first. Then it moves to the silver layer. The funding agent has all the prompts and
> tools needed. Every step has user review. Keep it open-ended, don't code too much.
> Focus on funding only.

The approved plan is at `C:\Users\vigne\.claude\plans\golden-fluttering-platypus.md`.

**Design rule to preserve:** logic lives in the prompts and in user input, not in Python
branching. All prompts are module constants at the top of
`backend/agents/funding_discovery.py` so they can be read and edited without digging
through code. If a node starts growing keyword lists or scoring rules, the black box is
coming back.

---

## 3. What is verified working

Tested live against the real Maryland DNR page and the real Supabase instance.

| Stage | Status | Evidence |
|---|---|---|
| `load_sources` | ✅ | Fetched MD DNR page, reduced 6,250 chars of readable text |
| `clarify` ⏸ | ✅ | Asked 3 questions genuinely grounded in the page — caught that POS Local needs county authorisation, and that Acquisition/Development/Planning are separate tracks |
| `research` ⏸ | ✅ | Found programs nobody hand-typed: **Greenspace Equity Program** ($7M FY2027) and **Local Parks and Playgrounds Infrastructure**. Honestly marked search-result-only URLs `usable=false` because it had not actually read them |
| `extract` ⏸ | ✅ | 2 programs, both excerpts **verified** against fetched text, award amounts correctly left `NULL` rather than guessed, confidence 0.92–0.95 |
| `geo_tie` ⏸ | ✅ | Recommended `state:MD` for both, with reasons; override accepted |
| `write_silver` | ❌ | **Blocked on section 1 only** |

Also confirmed:
- **Nothing is written before the final confirmation.** `funding_programs` stayed at 10
  rows through stages 1–4. An abandoned session persists nothing.
- **The excerpt guard works.** `web_source.excerpt_is_genuine()` correctly flagged
  "Local Parks and Playgrounds Infrastructure Program" when its excerpt did not match the
  fetched text. That is the line between extraction and invention — keep it.
- **Web search is available**: `web_search_20250305` works with Haiku 4.5 (9 results).

---

## 4. Verification to run after unblocking

```bash
cd sop_implementation_guide
C:\Users\vigne\venvs\sop\Scripts\python.exe -m uvicorn backend.main:app --reload
```

Then a full session — this is the exact script that failed only at the last step:

1. `POST /funding/discovery` with
   `{"goal": "Find state and local funding to build a public garden on a residential block in Bowie, Maryland", "sources": ["https://dnr.maryland.gov/land/Pages/ProgramOpenSpace/home.aspx"]}`
2. `POST /funding/discovery/{thread_id}/respond` with `{"value": "City of Bowie via Prince George's County; development on existing city land."}`
3. `respond` with `{"value": "approved"}` → extraction drafts
4. `respond` with `{"value": {"programs": [...edited drafts...]}}` — **deliberately change
   a field** and confirm it survives to the DB
5. `respond` with `{"value": {"geo": {"<program_name>": "county:24033"}}}` → writes to silver

Assert afterwards: row has `is_extracted=true`, `reviewed_by_user=true`, a non-null
`source_excerpt`, the `discovery_thread_id`, and **your edited value** rather than the
model's original.

Remaining plan checks not yet done:
- Confirm no hand-typed program facts remain: `grep -rn "Program Open Space" backend/`
  should return nothing (it currently returns only prompt text in
  `funding_discovery.py`, which is fine — verify it is not data).
- Re-run the per-block flow on block 14 to confirm scoring still works against an
  extraction-filled catalog.

---

## 5. Files

**New this session**
- `backend/agents/funding_discovery.py` — the 5-stage LangGraph. Prompts at top.
- `backend/tools/web_source.py` — `fetch()`, `fetch_many()`, `search()`,
  `excerpt_is_genuine()`. Plain httpx, deliberately not a server-side web_fetch tool.
- `backend/api/discovery.py` — 3 generic endpoints (start / get stage / respond). The
  request body for `respond` is untyped on purpose; each stage states what it wants in
  its own `expects` field.

**Changed**
- `backend/tools/grants_gov.py` — reduced to a **protocol adapter only** (Grants.gov
  needs POST, so the generic GET fetcher cannot read it). All keyword lists, category
  filters and relevance ranking were deleted — that was the hidden logic.
- `backend/db/schema.sql` — extraction provenance columns (see section 1). `is_curated`
  is deprecated but intentionally **not dropped**, to keep the file non-destructive.
- `backend/models/funding.py` — `is_curated` → `is_extracted` + the new fields.
- `backend/agents/funding.py`, `frontend/dev-console/index.html` — same rename.
- `backend/tools/llm_extractor.py` — added reusable `structured_call(prompt, schema)`.
- `backend/main.py` — registers the discovery router.

**Deleted (do not resurrect)**
- `backend/tools/funding_catalog.py` — the 4 hand-typed programs
- `scripts/sync_funding_programs.py` — hardcoded-keyword Grants.gov sync
- `scripts/check_catalog_urls.py` — only existed to babysit hand-typed URLs

**Still to do**
- Discovery panel in `frontend/dev-console/index.html`. Should render whatever
  `stage_payload` returns generically (question list / source list / editable draft table)
  rather than a hand-built form per stage.

**Untouched and still working** — the per-block scoring path:
`FundingAgent`, `cdbg.py`, `tract_eligibility.py`, `orchestrator.py`.

---

## 6. Environment and gotchas that cost time

- **venv is outside the repo:** `C:\Users\vigne\venvs\sop`. Deliberate — the repo is in
  OneDrive, and OneDrive syncing `site-packages` causes file-lock failures during pip
  install. VS Code needs the interpreter set to it manually.
- **Always run from `sop_implementation_guide/`** — `config.py` loads `.env` relative to
  the working directory.
- **`anthropic` 1.9.0 removed `temperature`** from `messages.create()`. Pass it as
  `extra_body={"temperature": 0}`. Needed for reproducibility — without it the same block
  scored 70 then 95 across runs.
- **Port 8090 is blocked by Windows** (winerror 10013, reserved range) and something else
  answers on it with a 404. Use 8000.
- **`MemorySaver` is still the checkpointer** — for both agents. Any `--reload` destroys
  paused sessions. **This is the top live-demo risk.** Swapping to a Postgres
  checkpointer against Supabase is a contained change; `langgraph-checkpoint` 4.2.0 is
  already installed.
- Discovery sessions are in-memory only, so the `test-discovery-*` threads left over from
  testing need no cleanup.
- The IDE shows SQL errors in `schema.sql` — it parses PostgreSQL with a SQL Server
  dialect. `CREATE TABLE IF NOT EXISTS` is not T-SQL. False positives.

**Key data facts**
- NMTC layer: `NMTC_Qualified_Tracts_2025` FeatureServer, **layer id 124**, GEOID field
  `NMTC_Quali`. 122 qualified tracts in Prince George's County (24033).
- Of the 6 Bowie tracts, only **24033800520** is NMTC-qualified.
- **Demo contrast:** block 14 (Mitchellville Rd, $86,327) is CDBG *and* NMTC eligible;
  block 69 (12th St, $173,611) is neither. Funding sub-scores 70 vs 35.
- `funding_programs` currently holds **10 rows** — 6 genuinely sourced from Grants.gov
  (kept, full provenance) and 4 hand-typed ones that **should be deleted** per the plan
  but have not been yet. Deleting them leaves a compelling "watch silver fill from
  nothing" demo.

---

## 7. Corrections to older docs

`START-HERE.md` and `project-overview.md` are stale in ways that matter:

- They say "❌ Supabase project" — **wrong**, it exists, schema is applied, Bowie data is
  loaded.
- They claim *"First Data Source: EPA Brownfields — federal API"*. **No Brownfields API
  was ever called.** The module named `epa_brownfields.py` contained no Brownfields
  client and has been deleted; its one useful function is now `backend/tools/cdbg.py`.
- They say 43 blocks. The GeoJSON has **68**.
- EPA EJSCREEN (`ejscreen.epa.gov`) **no longer resolves** and its result was never used;
  that call has been removed.

---

## 8. Honest state of the midterm story

What can be demonstrated today: a live session where the user pastes a government URL,
the agent asks grounded clarifying questions, reports what the page actually contains,
extracts programs with verifiable excerpts, recommends a geographic tie, and the user
edits everything before it is saved. Plus per-block feasibility scoring driven by two
real determinations (CDBG income rule, NMTC tract designation) rather than LLM guesswork.

What cannot: zoning and policy agents (scaffolds only), and the Mapbox/React frontend
from `START-HERE.md` (does not exist — there is a plain-HTML dev console instead).

The accepted risk the user chose: replacing the Grants.gov sync means there is no
non-interactive fallback if a live session stumbles mid-demo. **Rehearse one full session
against the MD DNR URL before Wednesday.**
