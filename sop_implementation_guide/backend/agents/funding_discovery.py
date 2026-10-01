"""
FundingDiscoveryAgent — a live, user-driven funding extraction session.

    START → load_sources → clarify ⏸ → research ⏸ → extract ⏸ → geo_tie ⏸ → write_silver → END

The user brings the sources. The agent asks what it needs to know, reads what it was
given, proposes what it found, and recommends a geographic tie. The user reviews every
stage. Nothing reaches the silver layer until the last confirmation.

Design rule: the logic lives in the prompts below, not in Python branching. Every prompt
is a module constant so it can be read and edited without digging through code — the
previous implementation hid its sources, keywords and relevance rules in Python, which
is precisely what this replaces.

Scope is funding only.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional, TypedDict

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from backend.db.session import get_supabase
from backend.repositories.funding_programs import (
    DiscoverySessionRepository,
    FundingProgramRepository,
)
from backend.tools import grants_gov, web_source
from backend.tools.llm_extractor import structured_call

log = logging.getLogger(__name__)


# ── Prompts (edit these, not the nodes) ──────────────────────────────────────

KEYWORD_PROMPT = """A city planner stated this funding goal in their own words:

{goal}

Reduce it to a short search phrase for a government grants database, which matches on
keywords and has no understanding of sentences.

- Keep the subject matter and the kind of work. Drop the place name, the applicant, the
  verbs ("find", "looking for"), and any instruction to the extractor.
- Two to five words. Prefer the vocabulary a funding agency would use for this work over
  the planner's own phrasing.
- If the goal is already short and keyword-like, return it unchanged."""

CLARIFY_PROMPT = """You are helping a city planner build a funding catalogue for
specific city blocks.

THEIR GOAL: {goal}

SOURCES THEY SUPPLIED (first {n_chars} characters of each, as actually fetched):
{sources}

Ask only what you genuinely cannot determine from the text above and that would change
what you extract. Good questions are about scope and intent: which geography applies,
which kinds of project qualify, whether closed or upcoming rounds matter, who the
applicant will be. Do not ask for anything already stated in the goal or visible in the
sources.

Ask at most 4 questions. If the sources and goal are sufficient, return an empty list and
say so in `notes`."""

RESEARCH_PROMPT = """You are assessing whether a set of sources can support a funding
catalogue for a city planner.

THEIR GOAL: {goal}

THEIR ANSWERS TO YOUR QUESTIONS:
{answers}

SOURCES AS FETCHED:
{sources}

{search_block}

Report honestly:
- For each source, what funding information it actually contains, and what it does not.
  If a page is only a navigation index with no programme detail, say that plainly.
- How many distinct funding programmes you could extract, and what key fields would be
  missing.
- Additional official sources worth adding. Only suggest URLs you saw in the fetched text
  or in the search results above — do not invent plausible-looking government URLs.

Be direct about weak sources. Telling the planner a page is unusable is more useful than
extracting something thin from it."""

EXTRACT_PROMPT = """Extract funding programmes from the sources below, for this goal:

GOAL: {goal}

CONTEXT FROM THE PLANNER:
{answers}

SOURCES AS FETCHED:
{sources}

Rules that matter more than completeness:
- Extract only what the text supports. Leave a field null rather than guessing it. A null
  the planner fills in is fine; an invented dollar figure is not.
- `source_excerpt` must be a verbatim span copied from the source text — it is checked
  against the fetched text, and a programme whose excerpt does not match is flagged.
- `confidence` is your own estimate of how well the text supports the values.
- If a source contains no extractable programme, extract nothing from it. Returning an
  empty list is a valid, useful answer."""

GEO_PROMPT = """Recommend the geographic reach of each funding programme.

The planner works on individual city blocks in Bowie, Prince George's County, Maryland
(county FIPS 24033, state MD).

PROGRAMMES (as reviewed and edited by the planner):
{programs}

For each, recommend one `geo_scope`:
- "federal"        — available nationally
- "state:MD"       — Maryland only
- "county:24033"   — Prince George's County only
- "tract:NMTC"     — only in tracts carrying a designation (name it)
- "unknown"        — the source does not say

Base this on what the source said, not on what would be convenient. If a programme is
administered by a state agency, it is state-scoped even when the money is federal.
Explain each recommendation in one sentence so the planner can overrule it."""


# ── Response schemas ─────────────────────────────────────────────────────────

def _obj(props: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": props, "required": required,
            "additionalProperties": False}


KEYWORD_SCHEMA = _obj({
    "keyword": {"type": "string", "description": "Two to five words."},
}, ["keyword"])

CLARIFY_SCHEMA = _obj({
    "questions": {"type": "array", "items": _obj({
        "question": {"type": "string"},
        "why": {"type": "string", "description": "What this changes about the extraction."},
    }, ["question", "why"])},
    "notes": {"type": "string"},
}, ["questions", "notes"])

RESEARCH_SCHEMA = _obj({
    "source_assessments": {"type": "array", "items": _obj({
        "url": {"type": "string"},
        "contains": {"type": "string"},
        "missing": {"type": "string"},
        "usable": {"type": "boolean"},
    }, ["url", "contains", "missing", "usable"])},
    "expected_program_count": {"type": "integer"},
    "suggested_sources": {"type": "array", "items": _obj({
        "url": {"type": "string"},
        "why": {"type": "string"},
    }, ["url", "why"])},
    "summary": {"type": "string"},
}, ["source_assessments", "expected_program_count", "suggested_sources", "summary"])

# Every property carries a description, and they are not decoration: the model reads
# them during extraction, and `extraction_fields()` serves them to the console so the
# form can promise exactly what the agent will record. One definition, both jobs —
# so the promise cannot drift from the behaviour.
PROGRAM_SCHEMA = _obj({
    "program_name": {"type": "string",
                     "description": "The programme's name as the source writes it."},
    "program_type": {"type": ["string", "null"],
                     "description": "federal, state or local — whichever the source indicates."},
    "source_agency": {"type": ["string", "null"],
                      "description": "The body that administers the programme."},
    "award_amount_min": {"type": ["number", "null"],
                         "description": "Smallest award the source states, in dollars."},
    "award_amount_max": {"type": ["number", "null"],
                         "description": "Largest award the source states, in dollars."},
    "deadline": {"type": ["string", "null"],
                 "description": "The closing date the source states, as YYYY-MM-DD."},
    "eligibility_notes": {"type": ["string", "null"],
                          "description": "Who may apply and what the money may be spent "
                                         "on, in the source's own terms."},
    "application_url": {"type": ["string", "null"],
                        "description": "Where to apply, if the source links it."},
    "source_url": {"type": "string", "description": "The page this programme was read from."},
    "source_excerpt": {"type": "string", "description": "Verbatim span from the source."},
    "confidence": {"type": "number",
                   "description": "0-1. How well the source text supports these values."},
}, ["program_name", "program_type", "source_agency", "award_amount_min", "award_amount_max",
    "deadline", "eligibility_notes", "application_url", "source_url", "source_excerpt",
    "confidence"])


def extraction_fields() -> list[dict]:
    """
    The fields a session records per programme, for the console to show up front.

    Read off PROGRAM_SCHEMA rather than restated, so adding a field to the schema
    updates what the user was promised in the same commit.
    """
    return [
        {
            "field": name,
            "description": spec.get("description", ""),
            # A nullable field is one the agent is allowed to leave blank rather
            # than guess — that distinction is the point of showing this list.
            "optional": "null" in spec["type"] if isinstance(spec["type"], list) else False,
        }
        for name, spec in PROGRAM_SCHEMA["properties"].items()
    ]

EXTRACT_SCHEMA = _obj({
    "programs": {"type": "array", "items": PROGRAM_SCHEMA},
    "notes": {"type": "string"},
}, ["programs", "notes"])

GEO_SCHEMA = _obj({
    "recommendations": {"type": "array", "items": _obj({
        "program_name": {"type": "string"},
        "geo_scope": {"type": "string"},
        "reason": {"type": "string"},
    }, ["program_name", "geo_scope", "reason"])},
}, ["recommendations"])


# ── State ────────────────────────────────────────────────────────────────────

class DiscoveryState(TypedDict):
    thread_id: str
    goal: str
    search_keyword: str
    requested_sources: list[str]
    fetched: list[dict]
    fetch_failures: list[dict]
    questions: list[dict]
    answers: str
    research: dict
    search_result: dict
    drafts: list[dict]
    geo_recommendations: list[dict]
    written: list[dict]


# ── Helpers ──────────────────────────────────────────────────────────────────

PREVIEW_CHARS = 6000


def _render_sources(fetched: list[dict], limit: int = PREVIEW_CHARS) -> str:
    if not fetched:
        return "(no sources could be read)"
    return "\n\n".join(
        f"--- SOURCE: {s['final_url']} (fetched {s['fetched_at']}, {s['content_type']}) ---\n"
        f"{(s.get('text') or '')[:limit]}"
        for s in fetched
    )


def _search_keyword(goal: str) -> str:
    """
    Distil the goal into something a keyword index can match.

    The goal is prose aimed at the extractor — it names a place, an applicant and
    often an instruction, none of which a keyword database understands. Sending it
    raw meant the more carefully a planner wrote their goal, the worse their
    Grants.gov and web-search results got.

    Falls back to the goal on any failure. A weak search is a bad session; a failed
    session over a search term is a worse one.
    """
    try:
        keyword = (structured_call(
            KEYWORD_PROMPT.format(goal=goal), KEYWORD_SCHEMA, max_tokens=256,
        )["keyword"] or "").strip()
    except Exception as e:  # noqa: BLE001
        log.warning("keyword distillation failed, searching on the raw goal: %s", e)
        return goal
    if not keyword:
        return goal
    log.info("search keyword %r distilled from goal %r", keyword, goal[:80])
    return keyword


def _load_one(url: str, keyword: str) -> dict:
    """Fetch a source, routing through the Grants.gov adapter when applicable."""
    if grants_gov.handles(url):
        # Grants.gov needs POST, so a plain GET cannot read it. The search term still
        # derives from the user's goal, not from a keyword list in this repo — only
        # now it is the distilled form rather than the full sentence.
        text, hits = grants_gov.as_source_text(keyword)
        return {
            "url": url, "final_url": url, "status": 200,
            "content_type": "application/json (grants.gov adapter)",
            "text": text, "truncated": False,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "adapter": "grants_gov", "raw_hits": hits,
        }
    return web_source.fetch(url)


# ── Nodes ────────────────────────────────────────────────────────────────────

def load_sources(state: DiscoveryState) -> dict:
    keyword = _search_keyword(state["goal"])
    fetched, failures = [], []
    for url in state["requested_sources"]:
        try:
            fetched.append(_load_one(url, keyword))
        except Exception as e:  # noqa: BLE001 — report, never drop silently
            log.warning("source failed: %s (%s)", url, e)
            failures.append({"url": url, "error": str(e)})
    return {"fetched": fetched, "fetch_failures": failures, "search_keyword": keyword}


def clarify(state: DiscoveryState) -> dict:
    result = structured_call(
        CLARIFY_PROMPT.format(
            goal=state["goal"],
            n_chars=PREVIEW_CHARS,
            sources=_render_sources(state["fetched"]),
        ),
        CLARIFY_SCHEMA,
    )
    answers = interrupt({
        "stage": "clarify",
        "message": "The agent has questions before it extracts anything.",
        "questions": result["questions"],
        "notes": result["notes"],
        "sources_read": [
            {"url": s["final_url"], "chars": len(s.get("text") or ""), "adapter": s.get("adapter")}
            for s in state["fetched"]
        ],
        "fetch_failures": state["fetch_failures"],
        "search_keyword": state.get("search_keyword"),
        "expects": "Free text answering the questions. Send 'skip' to proceed without answering.",
    })
    return {"questions": result["questions"], "answers": answers or "skip"}


def research(state: DiscoveryState) -> dict:
    keyword = state.get("search_keyword") or state["goal"]
    search_result = web_source.search(
        f"{keyword} official government funding programme pages"
    )
    search_block = (
        f"WEB SEARCH RESULTS:\n{search_result['summary']}\n"
        + "\n".join(f"- {r.get('url')}" for r in search_result["results"] if r.get("url"))
        if search_result["available"]
        else f"WEB SEARCH: {search_result['summary']}"
    )

    result = structured_call(
        RESEARCH_PROMPT.format(
            goal=state["goal"],
            answers=state["answers"],
            sources=_render_sources(state["fetched"]),
            search_block=search_block,
        ),
        RESEARCH_SCHEMA,
    )

    decision = interrupt({
        "stage": "research",
        "message": "Here is what the sources actually contain. Adjust the source list before extraction.",
        "assessments": result["source_assessments"],
        "expected_program_count": result["expected_program_count"],
        "suggested_sources": result["suggested_sources"],
        "summary": result["summary"],
        "web_search_available": search_result["available"],
        "search_keyword": keyword,
        "current_sources": [s["final_url"] for s in state["fetched"]],
        "expects": "{'sources': [final list of URLs to extract from]} — or 'approved' to keep the current list.",
    })

    # The user may replace the source list entirely; re-fetch anything new.
    new_urls = decision.get("sources") if isinstance(decision, dict) else None
    if not new_urls:
        return {"research": result, "search_result": search_result}

    have = {s["final_url"]: s for s in state["fetched"]}
    have.update({s["url"]: s for s in state["fetched"]})
    fetched, failures = [], list(state["fetch_failures"])
    for url in new_urls:
        if url in have:
            fetched.append(have[url])
            continue
        try:
            fetched.append(_load_one(url, keyword))
        except Exception as e:  # noqa: BLE001
            failures.append({"url": url, "error": str(e)})
    return {"research": result, "search_result": search_result,
            "fetched": fetched, "fetch_failures": failures}


def extract(state: DiscoveryState) -> dict:
    result = structured_call(
        EXTRACT_PROMPT.format(
            goal=state["goal"],
            answers=state["answers"],
            sources=_render_sources(state["fetched"], limit=12000),
        ),
        EXTRACT_SCHEMA,
        max_tokens=8192,
    )

    # Verify every excerpt really appears in what we fetched. This is the line
    # between extraction and invention, so it is checked rather than trusted.
    drafts = []
    for p in result["programs"]:
        genuine = web_source.excerpt_is_genuine(p.get("source_excerpt"), state["fetched"])
        drafts.append({**p, "excerpt_verified": genuine})

    unverified = [d["program_name"] for d in drafts if not d["excerpt_verified"]]
    if unverified:
        log.warning("excerpt did not match fetched text for: %s", ", ".join(unverified))

    edited = interrupt({
        "stage": "extract",
        "message": "Review and correct each programme. Unverified excerpts could not be found "
                   "in the fetched text — treat those values as unconfirmed.",
        "programs": drafts,
        "unverified": unverified,
        "notes": result["notes"],
        "expects": "{'programs': [the kept, corrected list — omit the ones you reject]} "
                   "— or 'approved' to accept every programme as extracted.",
    })

    # An empty list is a real answer: it means every programme was rejected. Only a
    # response with no 'programs' key at all ('approved') falls back to the drafts.
    edits = edited.get("programs") if isinstance(edited, dict) else None
    final = edits if isinstance(edits, list) else drafts
    if len(final) < len(drafts):
        kept = {p.get("program_name") for p in final}
        log.info("reviewer rejected %d of %d programmes: %s",
                 len(drafts) - len(final), len(drafts),
                 ", ".join(d["program_name"] for d in drafts if d["program_name"] not in kept))
    return {"drafts": final}


def geo_tie(state: DiscoveryState) -> dict:
    if not state["drafts"]:
        return {"geo_recommendations": []}

    result = structured_call(
        GEO_PROMPT.format(programs=_programs_digest(state["drafts"])),
        GEO_SCHEMA,
    )
    by_name = {r["program_name"]: r for r in result["recommendations"]}

    confirmed = interrupt({
        "stage": "geo_tie",
        "message": "Confirm how far each programme reaches. This is the last step before "
                   "anything is written to the silver layer.",
        "recommendations": result["recommendations"],
        "programs": [
            {"program_name": d["program_name"],
             "recommended_geo_scope": (by_name.get(d["program_name"]) or {}).get("geo_scope", "unknown"),
             "reason": (by_name.get(d["program_name"]) or {}).get("reason", "")}
            for d in state["drafts"]
        ],
        "expects": "{'geo': {'<program_name>': '<geo_scope>'}} — or 'approved' to accept the recommendations.",
    })

    overrides = confirmed.get("geo", {}) if isinstance(confirmed, dict) else {}
    recs = []
    for d in state["drafts"]:
        name = d["program_name"]
        rec = by_name.get(name) or {}
        recs.append({
            "program_name": name,
            "geo_scope": overrides.get(name) or rec.get("geo_scope") or "unknown",
            "reason": rec.get("reason", ""),
            "overridden": name in overrides,
        })
    return {"geo_recommendations": recs}


def _session_sources(state: DiscoveryState) -> list[str]:
    """The sources extraction actually read — not necessarily the requested list,
    since the research stage lets the user replace it."""
    return [s["final_url"] for s in state["fetched"]]


def write_silver(state: DiscoveryState) -> dict:
    """Persist to funding_programs. Only reached after every stage was confirmed."""
    db = get_supabase()

    if not state["drafts"]:
        # Still record the session. A session that extracted nothing is a real
        # result worth keeping — it says those sources were tried and came up
        # empty, which is what stops someone re-running them next month.
        _record_session(db, state, [])
        return {"written": []}

    repo = FundingProgramRepository(db)
    geo = {r["program_name"]: r["geo_scope"] for r in state["geo_recommendations"]}
    written = []

    for d in state["drafts"]:
        name = d["program_name"]
        row = {
            "program_key": _program_key(name, d.get("source_url") or ""),
            "program_name": name,
            "program_type": d.get("program_type"),
            "source_agency": d.get("source_agency"),
            "award_amount_min": d.get("award_amount_min"),
            "award_amount_max": d.get("award_amount_max"),
            "deadline": d.get("deadline") or None,
            "eligibility_notes": d.get("eligibility_notes"),
            "application_url": d.get("application_url"),
            "requires_tract_designation": _designation_from_scope(geo.get(name)),
            "source_name": f"Extracted from {d.get('source_url')}",
            "source_url": d.get("source_url"),
            "source_vintage": datetime.now(timezone.utc).date().isoformat(),
            "is_extracted": True,
            "extraction_confidence": d.get("confidence"),
            "source_excerpt": d.get("source_excerpt"),
            "geo_scope": geo.get(name, "unknown"),
            "reviewed_by_user": True,
            "discovery_thread_id": state["thread_id"],
            "raw_data": {"excerpt_verified": d.get("excerpt_verified"),
                         "goal": state["goal"],
                         # Stamped on every program so a session's source list is
                         # recoverable from the catalogue alone, which is how
                         # sessions that predate discovery_sessions are browsed.
                         "session_sources": _session_sources(state)},
        }
        result = repo.upsert_program(row)
        if result:
            written.append({
                "program_key": row["program_key"],
                "program_name": name,
                "program_type": row["program_type"],
                "source_agency": row["source_agency"],
                "geo_scope": row["geo_scope"],
                "deadline": row["deadline"],
                "source_url": row["source_url"],
                "extraction_confidence": row["extraction_confidence"],
            })

    _record_session(db, state, written)
    return {"written": written}


def _record_session(db, state: DiscoveryState, written: list[dict]) -> None:
    """
    Append this session to the browsable history.

    Written last, and never allowed to fail the run: the programs are already in
    the silver layer at this point, and losing the history entry is a smaller
    loss than throwing away a session the user just spent four reviews on.
    """
    try:
        DiscoverySessionRepository(db).record({
            "thread_id": state["thread_id"],
            "goal": state["goal"],
            "sources": _session_sources(state),
            "requested_sources": state["requested_sources"],
            "programs": written,
            "program_count": len(written),
        })
    except Exception as e:  # noqa: BLE001
        log.warning("could not record discovery session %s: %s", state["thread_id"], e)


def _programs_digest(drafts: list[dict]) -> str:
    return "\n".join(
        f"- {d['program_name']} | agency: {d.get('source_agency')} | "
        f"type: {d.get('program_type')} | source: {d.get('source_url')}"
        for d in drafts
    )


def _program_key(name: str, source_url: str) -> str:
    """Stable key so re-extracting the same programme updates rather than duplicates."""
    slug = "-".join(("".join(c if c.isalnum() else " " for c in name.lower())).split())[:60]
    host = (source_url.split("//")[-1].split("/")[0] or "extracted").replace("www.", "")
    return f"{host}:{slug}" if slug else f"{host}:unnamed"


def _designation_from_scope(scope: Optional[str]) -> Optional[str]:
    """'tract:NMTC' means eligibility depends on a tract designation."""
    if scope and scope.startswith("tract:"):
        return scope.split(":", 1)[1] or None
    return None


# ── Graph ────────────────────────────────────────────────────────────────────

class FundingDiscoveryAgent:
    """Not a SpecialistAgent: this fills the catalogue, it does not score a block."""

    def __init__(self):
        self._checkpointer = MemorySaver()
        self._graph = self.build_graph()

    def build_graph(self):
        b = StateGraph(DiscoveryState)
        for name, fn in (
            ("load_sources", load_sources),
            ("clarify", clarify),
            ("research", research),
            ("extract", extract),
            ("geo_tie", geo_tie),
            ("write_silver", write_silver),
        ):
            b.add_node(name, fn)

        b.add_edge(START, "load_sources")
        b.add_edge("load_sources", "clarify")
        b.add_edge("clarify", "research")
        b.add_edge("research", "extract")
        b.add_edge("extract", "geo_tie")
        b.add_edge("geo_tie", "write_silver")
        b.add_edge("write_silver", END)
        return b.compile(checkpointer=self._checkpointer)

    def start(self, thread_id: str, goal: str, sources: list[str]) -> dict:
        state: DiscoveryState = {
            "thread_id": thread_id, "goal": goal, "search_keyword": "",
            "requested_sources": sources,
            "fetched": [], "fetch_failures": [], "questions": [], "answers": "",
            "research": {}, "search_result": {}, "drafts": [],
            "geo_recommendations": [], "written": [],
        }
        self._graph.invoke(state, config=self._cfg(thread_id))
        return self.current(thread_id)

    def respond(self, thread_id: str, value: Any) -> dict:
        self._graph.invoke(Command(resume=value), config=self._cfg(thread_id))
        return self.current(thread_id)

    def current(self, thread_id: str) -> dict:
        snap = self._graph.get_state(self._cfg(thread_id))
        if not snap or not snap.values:
            return {}

        # A pending interrupt carries the payload the UI should render.
        for task in snap.tasks or ():
            for itr in getattr(task, "interrupts", ()) or ():
                return {"thread_id": thread_id, "done": False, "stage_payload": itr.value}

        return {
            "thread_id": thread_id,
            "done": True,
            "stage_payload": None,
            "written": snap.values.get("written", []),
            "goal": snap.values.get("goal"),
        }

    @staticmethod
    def _cfg(thread_id: str) -> dict:
        return {"configurable": {"thread_id": thread_id}}
