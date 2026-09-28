# START HERE — GA Tech OMSA Practicum · State of Place
> Briefing document for resuming work in a new chat session.
> Last updated: Sep 12, 2026

---

## Who I Am
- GA Tech OMSA student, final practicum project
- Working with **one teammate** (split TBD: backend/agents vs frontend)
- Using Claude Code as AI assistance — comfortable building properly with guidance
- On a **work machine (Windows 11)** — Docker is not available
- Have personal LLM API credits

---

## The Project in One Paragraph
**State of Place (SoP)** is a company that scores every city block across 150 built environment features and generates ranked recommendations for urban improvement (e.g. "add a park, fix the sidewalk, add crosswalks"). They can tell cities *what* to do but not *how to get it done*. My practicum project builds the **implementation layer** — a system that takes SoP's block-level recommendations and enriches them with real-world signals: what funding exists, what zoning allows, what policies support or block it. The output is a feasibility score + ranked actions per block, queryable in natural language.

---

## Key Files in This Project Folder
| File | What it contains |
|---|---|
| `START-HERE.md` | This file — read first |
| `project-overview.md` | Full project context, data analysis, timelines, decisions log |
| `solution-architecture.html` | Full solution architecture doc — open in browser. Has 11 sections including data pipeline, agent design, code patterns, tech stack decisions |
| `Bowie-Case Study.pdf` | SoP success story for Bowie, MD (image-based PDF — not extractable) |
| `SoP 150 Feature List.csv` | All 150 features SoP measures on each block |
| `Final_Bowie_OrderedBlockwiseRecs_20240314 Overall.csv` | Full Bowie recommendation engine output |
| `recommendation_summary/` | Three XLSX files: 50pct, 75pct, 90pct priority tiers for Bowie |
| `Project_notes_sponsor.txt` | Sponsor (Mariela) notes on project direction — read this |

---

## The Data We Have
SoP gave us **Bowie, MD** as the working dataset:
- **43 blocks** in Bowie Town Center neighborhood
- Each block has: street name, intersections, list of recommended feature changes, current SoP score, projected score after changes
- Average score lift: **+43 points** (e.g. 35.2 → 87.5)
- Top recommendations across all blocks:
  1. Public Garden (39/43 blocks) ← **anchor rec type**
  2. Playing/Sport Field (38/43)
  3. Arcades/covered walkways (37/43)
  4. Vertical mixed-use buildings (23/43)
  5. Crosswalks (17/43)

**Important distinction:** The 150 feature list is SoP's *observation* vocabulary (what they measure). The recommendation labels (e.g. "Public Garden", "Sidewalks with shading") are a *different, more human-readable* vocabulary used in the output. My system works with the recommendation labels, not the 150 codes.

---

## What's Been Decided

### Focus & Framing
- **All 3 focus areas** (Funding, Zoning, Plans/Policy) — not just one. The sponsor confirmed the buckets are fluid; start with one and cross over as needed.
- **Anchor recommendation type: Public Garden / Parks** — appears on 39/43 blocks, touches all 3 buckets, richest implementation signal
- **Approach: Recommendation-first** — take a rec ("add a park to Block 1"), find what funding/zoning/policy applies

### Architecture (see solution-architecture.html for full detail)
**Medallion data pipeline:**
```
Source Data → Bronze (raw) → Silver (cleaned + geo-tagged) → Gold (implementation profiles)
```

**Key tables:**
- `recommendations_raw` / `recommendations_clean` — Bowie block data
- `funding_signals` — cleaned funding opportunities (EPA, CDBG, LWCF, NMTC, MD POS)
- `zoning_signals` — parcel zoning normalized, park/mixed-use flags
- `policy_signals` — LLM-extracted signals from policy docs
- `geo_lookup` — THE junction table: spatial join of block geometry → all geographic boundaries (census tract, funding zones, zoning districts)
- `block_implementation_profile` — gold layer: feasibility score + actions + narrative per block

**Agents (LangGraph):**
- `OrchestratorAgent` — fans out to 3 specialists, aggregates, computes score, human approval, writes gold
- `FundingAgent` — searches funding programs, extracts eligibility, writes `funding_signals`
- `ZoningAgent` — GIS parcel lookup, classifies barriers, writes `zoning_signals`
- `PolicyAgent` — searches/fetches policy docs, LLM extraction, writes `policy_signals`
- `FeasibilityScorer` — pure compute node, no interrupt

**Human-in-the-loop pattern (LangGraph):**
Every agent: search → extract → **⏸ interrupt for user review** → apply feedback → write to DB

**Feasibility score:** `0.40 × funding + 0.35 × zoning + 0.25 × policy` (weights configurable)

### Tech Stack
| Layer | Choice |
|---|---|
| Language | Python |
| Agent framework | **LangGraph** (not PydanticAI — needed for interrupt/checkpointing) |
| Backend | FastAPI |
| Database | **Supabase** (PostgreSQL + PostGIS + pgvector) — cloud hosted, free tier, shared between teammates |
| Vector store | pgvector on Supabase (no separate Chroma needed) |
| Frontend | React + Vercel (free) |
| Backend hosting | Railway (~$7/month, only needed from Oct onward for demos) |
| Local dev | Native Python, no Docker — Supabase eliminates local DB need |
| Collaboration | GitHub private repo |
| LLM API | Personal credits (covered) |

### Code Design Patterns
LangGraph = workflow engine (state, interrupts, checkpointing)
Inside each LangGraph node, use:
1. **Repository pattern** — agents never write SQL directly, each table has a Repository class
2. **Abstract Base Class** — all specialist agents implement `SpecialistAgent` ABC: `run()`, `get_pending_review()`, `apply_feedback()`
3. **Dependency injection** — tools (web search, GIS, LLM) injected into agents via FastAPI `Depends()`
4. **Pydantic models** — every table has a corresponding model; the shared contract between agents
5. **Upsert/idempotency** — all writes use `INSERT ON CONFLICT DO UPDATE`; agents are safe to re-run

### First Data Source
**EPA Brownfields** — federal API, spatially tagged, directly relevant to park recs (contaminated land → green space). Start here.

---

## Build Strategy (IMPORTANT)

> Build the **full framework properly first**, then implement only FundingAgent end-to-end for the Oct 1 midterm. ZoningAgent and PolicyAgent are scaffolded (empty implementations) but the framework is plug-and-play — they slot in without structural changes.

**This is not over-engineering. It means:**
- Midterm demo: full system running with one live agent
- Post-midterm: just fill in the other two agents inside the existing framework

### Timeline
| Dates | Work |
|---|---|
| Sep 13–18 (DISCOVER) | GitHub repo, Supabase schema, project structure, base classes, Pydantic models |
| Sep 18–28 (PROTOTYPE V1) | FundingAgent end-to-end, FastAPI endpoints, basic React, EPA Brownfields live |
| Sep 28–Oct 1 | Polish, test on 3–5 Bowie blocks, midterm deck |
| Oct 2–23 (BUILD+TEST) | ZoningAgent → PolicyAgent → full gold layer → expand data sources |
| Oct 26–Nov 13 (REFINE) | Full frontend, NL interaction, scalability, source attribution |
| Nov 16–26 (FINALIZE) | Evaluation, final demo, final report |

---

## Project Structure (to be created)
```
sop-implementation-engine/
├── backend/
│   ├── agents/
│   │   ├── base.py          ← SpecialistAgent ABC (shared contract)
│   │   ├── orchestrator.py
│   │   ├── funding.py       ← implement first
│   │   ├── zoning.py        ← scaffold only
│   │   └── policy.py        ← scaffold only
│   ├── repositories/
│   │   ├── base.py
│   │   ├── funding.py
│   │   ├── zoning.py
│   │   ├── policy.py
│   │   └── gold.py
│   ├── models/              ← Pydantic models, agree upfront
│   │   ├── recommendations.py
│   │   ├── funding.py
│   │   ├── zoning.py
│   │   ├── policy.py
│   │   └── gold.py
│   ├── tools/
│   │   ├── web_search.py
│   │   ├── gis.py
│   │   └── llm_extractor.py
│   ├── api/
│   │   ├── blocks.py
│   │   └── threads.py
│   ├── db/
│   │   ├── session.py
│   │   └── migrations/      ← Alembic
│   └── main.py
└── frontend/
    └── src/
```

---

## Open Questions (need answers before/during build)
1. **Geography confirmation** — Bowie for prototype, or Philadelphia? Ask Mariela: *"Should our prototype target Bowie since we have the rec data, or Philadelphia? Or Bowie to prototype then scale to Philly?"*
2. **Node.js on work machine** — `node --version` to check. If blocked, React dev happens on home machine.
3. **Team split** — confirm with teammate: who owns backend/agents vs frontend?
4. **Feasibility score weights** — 0.40/0.35/0.25 is a starting assumption, validate once data is flowing

---

## Sponsor Contacts
- **Mariela Alfonzo**, Founder/CEO — mariela@stateofplace.co (main contact)
- **Evan Taylor**, CXO (secondary)
- Weekly team meetings + weekly student check-ins

---

## Frontend Design Reference

The frontend should **match the SoP application's visual style and structure** — we are building an extension of their product, not a standalone tool.

### SoP App UI Pattern (from screenshot)
```
┌─────────────────────────────────────────────────────────────┐
│  EXPLORE  │  ASSESS  │  PLAN  │  ACTIONS  │  DASHBOARD      │  ← top nav, dark bg, orange active tab
├───────────┬─────────────────────────────────────────────────┤
│  filter   │  breadcrumb: Index | date | shapes | 320 Blocks │  ← filter bar
├───────────┼─────────────────────────────────────────────────┤
│           │                                                  │
│  TIMING   │                                                  │
│  ─────    │         MAPBOX MAP                               │
│  SCORING  │    street segments: red (low) → teal (high)     │
│  ─────    │                                                  │
│  IRIS     │    [48.2 donut]          [legend: low → high]   │
│  SENSOR   │                                                  │
│  ─────    │                                                  │
│  OUTCOME  │                                                  │
│  DATA     │                                                  │
└───────────┴─────────────────────────────────────────────────┘
```

### Color Palette (from SoP app)
| Token | Value | Use |
|---|---|---|
| Background | `#1B2535` | App bg, sidebar |
| Surface | `#1E2D3E` | Sidebar panels |
| Orange accent | `#F07B3A` | Active tab, highlights |
| Teal (high score) | `#2EC4B6` | Map high-value segments |
| Red (low score) | `#E05C5C` | Map low-value segments |
| Text | `#FFFFFF` / `#A0B8CC` | Primary / muted |
| Border | `#2A3F55` | Panel dividers |

### Map Library
**Mapbox GL JS** via `react-map-gl` — confirmed from SoP app logo. Street segments colored by score.

### Key React Components to Build
| Component | Description |
|---|---|
| `TopNav` | Tabs: EXPLORE / ASSESS / PLAN / ACTIONS / DASHBOARD |
| `FilterBar` | Breadcrumb chips (Index, date range, shapes, block count) |
| `LeftSidebar` | Collapsible sections — our additions: IMPLEMENTATION SIGNALS, FEASIBILITY |
| `MapView` | Mapbox map, street segments colored by feasibility score |
| `ScoreWidget` | Donut chart overlay on map (show feasibility score, not just SoP score) |
| `BlockDetailPanel` | Slides in when block selected — funding opps, zoning flags, policy signals, actions |
| `ChatPanel` | NL query input + response — "What can we build in 12 months?" |

### Our Additions to the SoP Pattern
When a block is selected on the map, the left sidebar switches to show:
- Feasibility score (donut, styled like SoP's score widget)
- Funding opportunities (program name, amount, deadline)
- Zoning signals (current zone, barriers, variance required)
- Policy signals (supporting/blocking policies with excerpts)
- Ranked next steps / actions
- Chat input for follow-up questions

---

## What to Do Tomorrow (Sep 13)
1. `node --version` — check if Node.js is available on work machine
2. Create GitHub private repo — invite teammate
3. Create Supabase project — share connection string with teammate
4. Set up Python virtual environment
5. Create the project folder structure above
6. Start with `models/` — lock Pydantic schemas before any agent code
7. Then `agents/base.py` — the SpecialistAgent ABC

**The models/ folder is the first thing both teammates need to agree on. Everything else builds from there.**
