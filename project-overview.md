# GA Tech OMSA Practicum — State of Place (Fall 2026)

## What's the Ask?

**Company:** State of Place (SoP) — a platform that scores urban environments and generates recommendations for city improvement.

**Core Problem:** SoP can tell cities *what* to do, but not *how to actually get it done*. The practicum is about building the "implementation layer."

**Problem Statement:**
> "How do we turn messy public data into structured implementation signals that help our recommendation engine answer not just 'what should cities do?' but 'how do they actually get it done?'"

---

## Primary Objectives

1. Design methods to ingest and structure implementation-related information from diverse public data sources.
2. Develop AI workflows that translate this information into machine-readable opportunities and constraints.
3. Integrate those signals into the engine to prioritize and contextualize recommendations and generate implementation actions.

## Secondary Objectives

1. Explore alternative AI architectures (agents, RAG, MCP, knowledge graphs, etc.).
2. Develop approaches that are scalable, explainable, and suitable for integration into a production platform.
3. Prototype natural-language interaction with contextualized recommendations and implementation actions.

---

## Three Focus Areas (Choose One)

| Area | Description |
|---|---|
| **Zoning & Entitlements** | What's allowed, what permits are needed |
| **Funding & Financing** | What grants/programs exist, eligibility, deadlines |
| **Plans & Policies** | City plans, capital improvement programs, policy initiatives |

Teams are independent. Different technical approaches are encouraged. Multiple teams may work in the same area.

---

## What the End-of-Semester Demo Should Do

- Retrieve real public implementation data
- Convert it into structured recommendation signals
- Connect those signals to relevant SoP data/recommendations
- Use them to inform recommendation priority, feasibility, effort, or timing
- Generate concrete implementation actions
- Allow users to query the resulting information in natural language
- Demonstrate technical accuracy and scalability

---

## Technical Approaches to Consider

- NLP / LLM extraction
- RAG / semantic retrieval
- Agents & MCP / tool calling
- Knowledge graphs
- Geospatial processing / spatial joins
- Classification + ranking
- Recommendation-system methods
- Natural-language interfaces
- Provenance / confidence scoring

---

## Available Data

| SoP Data | Public Data |
|---|---|
| 2M+ street-level images | Planning documents |
| ~8,000 Philadelphia blocks | Zoning codes |
| 150 built environment features | Capital Improvement Plans |
| Recommendation engine outputs | Funding opportunities |
| Other outcome data | Permitting information |
| APIs | Policy initiatives |
| | GIS / Open Data across 5 USDOT partner cities |

---

## Key Timelines

| Phase | Dates | Deliverable |
|---|---|---|
| **DEFINE** | Aug 24 – Sep 4 | Choose focus area, form teams, define technical question |
| **DISCOVER** | Sep 7 – Sep 18 | Verify data, baseline ingestion/extraction, define schema |
| **PROTOTYPE V1** | Sep 21 – Oct 1 | End-to-end proof of concept + **midterm deck/report due Oct 1** |
| **BUILD + TEST** | Oct 2 – Oct 23 | Improve methods, connect to SoP recs, test priority/effort/timing |
| **REFINE / PRODUCTIZE** | Oct 26 – Nov 13 | Add NL interaction, attribution, scalability, polish demo |
| **FINALIZE** | Nov 16 – Nov 26 | Evaluation, docs, **final report + demo due Nov 26** |

> **Today: Sep 12** — currently in DISCOVER phase. Midterm due Oct 1.

---

## Team Structure

- Weekly team meetings
- Weekly student check-ins
- Ad hoc technical working sessions
- Direct collaboration with SoP product & engineering
- Opportunity to observe design-thinking workshops with five cities

**Main Contact:** Mariela Alfonzo, Founder/CEO — mariela@stateofplace.co  
**Secondary Contact:** Evan Taylor, CXO

---

## Immediate Next Steps (by Week 2)

- [ ] Explore the three focus areas
- [ ] Review starting data inventory
- [ ] Think about technical approach/interests
- [ ] Rank focus area preferences
- [ ] Form teams
- [ ] Define scope
- [ ] Finalize by Week 2

---

---

## Sponsor Guidance (from Project_notes_sponsor.txt)

Mariela shared concrete direction on how to think about the project:

### Framing the User Story
The right mental model is **recommendation-first**: start with what SoP already recommends (e.g., "add a park to this block") and then ask — what are the barriers and incentives to actually implementing that?

For a park recommendation, you'd want to know:
- Are there zoning laws/ordinances that make it harder to create one?
- Are there EPA policies incentivizing brownfield remediation (polluted sites that could become parks)?
- Are there local ordinances tied to public health that could help lobby for a park?
- Are there local, regional, state, and/or federal grants that could fund it?

### The Three Buckets are Fluid
> "What may be more feasible is to choose a core area and then cross over into the other two buckets as needed to address your use case."

You don't need to stay rigidly in one lane. Pick one as a core anchor, then pull from the others as the recommendation type demands.

### Concrete Example Project Idea
A tool that:
1. Parses Federal/State/Local passed bills
2. Cross-references them with economic GIS data (Energy Communities, New Markets Tax Credits, USDA SBA Loan areas, census tracts, city/state limits)
3. Creates a "funding GIS dataset" matched with a "policy dataset"

This could be split into two components:
- **NLP/agentic workflow** — ingests unstructured government data (updated monthly/yearly)
- **MCP/data storage layer** — stores and serves structured policies/regulations

### Useful ArcGIS Open Datasets (mentioned by sponsor)
- https://www.arcgis.com/home/item.html?id=37d43f0f030d4db99a05f468f46d9a1b
- https://experience.arcgis.com/experience/e78fa7dabc604f36b8307e3e1cc13d52

---

## Understanding the Existing SoP Data

### SoP 150 Feature List
SoP scores every city block across **150 built environment features**, each with categorical values. Examples:
- `BikeLane` — Bicycle lanes present (No/Yes)
- `Benches` — Benches or chairs (None/Few/Some/a lot)
- `BlnkWall` — Blank walls (None/Few/Some/a lot)
- `BuffStTr` — Street Trees as buffers (No/One side/Both sides)
- `PubGard` — Public Garden presence
- `Crosswalks`, `ParkingLot`, `MixedUse`, etc.

These features feed into a composite **SoP Index Score** (0–100).

### Bowie, MD — Real Recommendation Data

The Bowie dataset is the working example of what SoP actually produces. Key fields:

| Field | Description |
|---|---|
| `sgmntid` | Street segment / block ID |
| `neighborhood` | Neighborhood name |
| `StreetName` | Street |
| `IntersectionA / B` | Block boundaries |
| `Features to be changed` | Specific recommendations (e.g., "Add Public Garden, remove Surface parking lot") |
| `SoPIndex Before Score` | Current score (e.g., 35.2) |
| `SoPIndex New Score (Projected)` | Projected score after implementing recs (e.g., 87.5) |

**Example:** Block on Harbour Way scores 35.2 → projected 87.5 with 10 changes including adding a Public Garden, removing surface parking, adding crosswalks, and adding vertical mixed-use buildings.

### Recommendation Summary Files (3 Tiers)

Three files represent different **ambition/priority thresholds** — how aggressively to pursue improvements:

| File | Blocks | Feature Changes | Avg Score Before | Avg Score After | Avg Lift |
|---|---|---|---|---|---|
| `50pct` | 13 | 105 | 44.7 | 59.3 | +14.6 |
| `75pct` | 27 | 213 | 44.7 | 74.5 | +29.8 |
| `90pct` | 43 | 306 | 44.7 | 87.9 | **+43.2** |

All blocks are in **Bowie Town Center** neighborhood. The 50pct file is a subset of 75pct, which is a subset of 90pct — they stack from highest-impact to broadest coverage.

Score lift ranges from **+18 to +55 points**, with a median of +45. This means SoP's recommendations are substantial — the implementation layer needs to reflect that ambition.

### Top Recommended Feature Changes (across 43 blocks)

| Rank | Feature | Count | Implementation Complexity |
|---|---|---|---|
| 1 | Public Garden | 39x | Medium — land, funding, EPA brownfield grants possible |
| 2 | Playing or Sport Field | 38x | Medium — land, parks funding (LWCF, CDBG) |
| 3 | Arcades (covered walkways) | 37x | Low-Medium — streetscape grants, RAISE/CMAQ |
| 4 | Vertical Mixed-Use Buildings | 23x | High — requires zoning change |
| 5 | Angled/On-street Parking | 21x | Low — road design, permit |
| 6 | General Building Maintenance | 18x | Low — code enforcement, CDBG |
| 7 | Movie Theater | 18x | High — private development, incentives |
| 8 | Crosswalks | 17x | Low — traffic engineering, federal safety grants |
| 9 | Surface Parking Lot (conversion) | 14x | Medium — planning approval, EPA grants |
| 10 | Condo/Apartment Housing | 14x | High — zoning, housing programs |

**Key insight:** The top 3 recommendations (Public Garden, Sport Field, Arcades) appear on nearly every block and are all addressable through **public funding programs** — this is the sweet spot for the Funding & Financing focus area.

This is the **input data** your system needs to enrich with implementation signals (can we actually do this? Is there funding? What are the zoning constraints?).

---

## Proposed Focus Area & Technical Approach

> **Recommended focus: Funding & Financing** (with crossover into Plans & Policies as needed)

**Why:** The sponsor explicitly called out funding as the most concrete and actionable layer. Grant programs, tax credits, and federal designations (NMTC, Energy Communities, USDA loans) are spatially bounded and well-structured — ideal for GIS cross-referencing and NLP extraction.

### Proposed Architecture

```
PUBLIC DATA SOURCES                  PROCESSING LAYER                  SoP INTEGRATION
─────────────────                    ────────────────                  ─────────────────
Federal bills / legislation    →     NLP / LLM extraction         →   Match to block recs
State grant programs           →     Structured schema output      →   Tag: opportunity /
Local capital improvement plans →    Spatial join (GIS)           →     constraint /
ArcGIS open datasets           →     RAG for querying             →     priority / timing
Zoning codes (as needed)       →     Agent for updates            →   Natural language Q&A
```

### Key Questions to Answer
1. For a given SoP recommendation (e.g., "add a park to block X"), what funding opportunities exist?
2. Are there zoning barriers that would block it?
3. What is the realistic timeline and effort level?
4. Can a user ask "what can we implement in the next year with available funding?" and get a real answer?

---

## Firm Next Steps Plan

### This Week (DISCOVER phase, by Sep 18)
- [ ] Confirm focus area choice: **Funding & Financing** (with Policy crossover)
- [ ] Access and explore the 5 USDOT partner city datasets (Philadelphia + 4 others)
- [ ] Download the ArcGIS datasets linked by sponsor (Energy Communities, NMTC, USDA SBA Loan areas)
- [ ] Define the structured output schema: what does an "implementation signal" look like? (fields: type, source, eligibility, geography, deadline, confidence)
- [ ] Set up a gold-standard evaluation approach: pick 2–3 Bowie blocks and manually identify what funding/policies apply

### PROTOTYPE V1 (by Oct 1 — Midterm)
- [ ] Build ingestion pipeline for at least 1–2 public data sources (e.g., one federal grant program, one ArcGIS dataset)
- [ ] NLP extraction: parse unstructured funding documents into structured signals
- [ ] Spatial join: link funding opportunities to Bowie block segments
- [ ] Connect signals to Bowie recommendation data
- [ ] Basic natural-language interface: "What funding exists for block X?"
- [ ] Midterm deck + report

### BUILD + TEST (Oct 2–23)
- [ ] Expand to more data sources and geographies
- [ ] Refine signal schema (priority, effort, timing, cost)
- [ ] Test how implementation signals change recommendation ranking
- [ ] Accuracy/reliability evaluation against gold standard

### REFINE / PRODUCTIZE (Oct 26–Nov 13)
- [ ] Add source attribution and confidence scoring
- [ ] Scalability testing
- [ ] Polish NL interaction layer
- [ ] End-to-end demo prep

### FINALIZE (Nov 16–26)
- [ ] Final evaluation
- [ ] Documentation and production recommendations
- [ ] Final demo + report

---

## Decisions & Notes

- Sep 12: Reviewed kickoff PDF, Bowie case study data, and sponsor notes.
- Sep 12: Confirmed all 3 focus areas (fluid buckets, not one only). Anchor rec type: Public Garden / Parks.
- Sep 12: Full solution architecture designed — see `solution-architecture.html`
- Sep 12: Tech stack locked — Python + FastAPI + LangGraph + React + Supabase + pgvector + Vercel + Railway
- Sep 12: Build strategy — full framework first, FundingAgent end-to-end for Oct 1 midterm, others scaffold in
- Sep 12: First data source — EPA Brownfields API
- Sep 12: Code design patterns — Repository, ABC, Pydantic, Dependency Injection

## Current Status (Sep 12)

**Phase:** DISCOVER (ends Sep 18) → PROTOTYPE V1 due Oct 1

**Done:**
- ✅ Understood project ask and SoP data
- ✅ Full architecture designed and documented (solution-architecture.html)
- ✅ Tech stack locked
- ✅ Build strategy agreed

**Not started yet:**
- ❌ GitHub repo
- ❌ Supabase project
- ❌ Any code
- ❌ Teammate coordination

**Starting tomorrow (Sep 13):**
1. Check Node.js: `node --version`
2. Create GitHub private repo
3. Create Supabase project
4. Set up Python venv + project structure
5. Start with `models/` — lock Pydantic schemas first
6. Then `agents/base.py` — SpecialistAgent ABC

---

## Open Questions

### Q1: Target Geography — Bowie (MD) or Philadelphia (PA)?
> **Status: Needs sponsor confirmation**

The kickoff PDF lists Philadelphia (~8,000 blocks) as the primary SoP dataset, but the team was given Bowie, MD recommendation CSVs. These are different states with different zoning codes, funding programs, and policies.

**Possible interpretations:**
- Bowie data = format reference only; Philadelphia is the actual build target
- Bowie = prototype geography; Philadelphia = scale-up target
- Both are valid; team's choice

**Draft message to Mariela:**
> "Quick clarification on geography — the kickoff deck references Philadelphia as the primary dataset (~8,000 blocks), but we were also given the Bowie recommendation CSVs. Should our prototype target Philadelphia, or should we use Bowie since we have the rec data already? Or would you recommend using Bowie to prototype the approach and then scale to Philly?"

