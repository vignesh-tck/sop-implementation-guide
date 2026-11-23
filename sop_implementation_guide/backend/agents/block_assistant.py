"""
BlockAssistant: the app's main assistant, "Ask about this block" on the Blocks tab.

A starting point for the SME who works a block: questions about the block, its
recommendations, their plans and durations, and how any of it came to be (the
zoning finding and ordinance behind an outcome, the funding matches and their
sources, the playbooks, the dataset versions and the pipelines that built
them), or, with scope "all", about all the blocks at once. It reads this app's
data through the tools in backend/agents/assistant_tools.py and changes nothing.

Every turn ends with the `answer` tool: a one-line headline, a short body, at
most one visual, what the answer assumes, next actions, what to show on the
map, and the references it cites. The visual names ids only; the numbers drawn
(durations, scores, block lists) are filled in here from the same data the
tools read, so a chart can't disagree with the data. The sources shown are the
tools the turn actually called, and a reference can only be one the tools
returned, for the same reason.

The chat is held by the browser and sent with each turn as plain text (each
question and the answer's headline and body), not the raw tool exchange: a
follow-up re-reads whatever it needs, which keeps requests small.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Callable

from supabase import Client

from backend.agents.assistant_tools import DIMENSIONS, PROGRESS, TOOLS, Data, Tools, _num, rec_row, timeline
from backend.agents.pipeline_builder import _create
from backend.tools.llm_extractor import _get_client

log = logging.getLogger(__name__)

MODEL = "claude-opus-5-5"
MAX_TOOL_ROUNDS = 10
HISTORY_TURNS = 12
CITE = re.compile(r"\[(R\d+(?:\s*,\s*R\d+)*)\]")

_IDS = {"type": "array", "items": {"type": "integer"}}

ANSWER_TOOL = {
    "name": "answer",
    "description": "Give your answer. Call this exactly once, last, after reading what you need.",
    "input_schema": {"type": "object", "properties": {
        "headline": {"type": "string", "description": "The direct answer in one sentence, under 16 words."},
        "body": {"type": "string", "description": "Two or three short sentences, or a few '- ' bullets, that support "
                                                  "the headline. **bold** allowed. Cite as [R3] after the claim. "
                                                  "Under 120 words."},
        "visual": {"type": "object", "description": "At most one chart or list; the app draws it from the data.",
                   "properties": {
                       "type": {"type": "string", "enum": ["timeline", "scores", "recommendations", "blocks"]},
                       "rec_ids": _IDS, "block_ids": _IDS,
                       "arrangement": {"type": "string", "enum": ["together", "one_after_another", "phases"]},
                       "phases": {"type": "array", "items": _IDS},
                       "phase_names": {"type": "array", "items": {"type": "string"}}},
                   "required": ["type"]},
        "assumptions": {"type": "array", "items": {"type": "string"},
                        "description": "What the answer assumes or doesn't know yet, one short line each."},
        "references": {"type": "array", "items": {"type": "string"},
                       "description": "Ids of the references (R1, R2 …) from the tool results that back the answer."},
        "actions": {"type": "array", "description": "Two or three next steps, as buttons.", "items": {
            "type": "object", "properties": {
                "label": {"type": "string", "description": "Button text, under 5 words."},
                "type": {"type": "string", "enum": ["ask", "open_recommendation", "run_funding", "run_zoning",
                                                    "highlight_blocks", "open_block"]},
                "question": {"type": "string", "description": "For ask: the follow-up question it sends."},
                "rec_id": {"type": "integer"}, "block_ids": _IDS},
            "required": ["label", "type"]}},
        "map": {"type": "object", "description": "What to show on the map.", "properties": {
            "rec_ids": {**_IDS, "description": "The block's recommendations this answer is about (labelled on its street)."},
            "block_ids": {**_IDS, "description": "Blocks to highlight (across-block answers)."}}}},
        "required": ["headline", "body"]},
}

SYSTEM = """You are the main assistant in a planning app for the City of Bowie, Maryland (Prince George's County).
The app turns State of Place (SoP) recommendations into implementation plans for Bowie's {n_blocks} street blocks.
You answer the SME (subject matter expert) who works a block. It is a starting point for their work: be useful,
quick and honest.

How the app's data is built. Use this to explain where anything comes from; read the specifics with the tools.
- Blocks, scores and recommendations come from State of Place's data, not from this app. Each block has a SoP
  score (0-100), nine dimension scores, a street survey of ~125 features, and recommendations ("Arcades",
  "Crosswalks 2") with a predicted score increase.
- Pipelines (Pipeline development tab): a planner builds each one in a chat with a pipeline agent: a goal, the fields
  to extract, sources (web pages, PDFs, the county GIS map service, app tables), and steps from a fixed catalogue
  (fetch, model extraction, spatial lookup, use-table reading, SQL …) that the planner approves one at a time.
  Loading a run writes the working data and snapshots it as a numbered, immutable dataset version: Funding vN,
  Zoning vN, or a general table (gp_<name>). Tools: pipelines, datasets, general_table.
- Funding: a funding pipeline's version is the program catalogue. A Block Funding run matches programs to each of a
  block's recommendations against one Funding version; the SME keeps or drops each match and names an amount, and
  approving pins the run. Tools: funding_detail, funding_programs.
- Zoning: the zoning pipeline publishes block zones (county GIS lookups within a buffer of each block), the
  ordinance's use permissions (P / SE / X per zone, from the Zoning Ordinance use tables on enCodePlus) and the
  readings (which ordinance uses each recommendation type is taken to be). A Block Zoning run makes one cited
  finding per recommendation against one Zoning version; findings with questions wait for the SME, who accepts,
  sets the outcome with a note, or leaves it open; approving pins the run. Plans use approved runs only.
  Tools: zoning_finding, zoning_reading.
- Playbooks (Playbooks tab): the standard steps for one kind of change; each recommendation type maps to one.
  Planners edit them (with a playbook assistant) and save versions. Durations and owners are a strawman until an SME
  reviews them. A block's plan for a recommendation starts as its playbook; an SME edits a draft and approves it, and
  every draft and approval is kept. Tools: playbook, plan, plan_history.

How to answer:
- Read before you answer: use the tools; never state a number, duration, status, source or step the tools didn't
  give you. For durations, sequencing or a first plan, call `timeline`. For "why" a zoning outcome or a funding
  match, call zoning_finding or funding_detail; for "where does this come from", follow it back through datasets and
  pipelines.
- Cite: tool results carry reference ids (R1, R2 …) on the documents behind them (ordinance sections, GIS layers,
  program pages, pipeline sources, playbook step sources). Put [R3] right after the claim it supports, and list the
  ids you cite in `references`. Never cite an id a tool didn't give you; a claim with no reference is your reading,
  so say so.
- Finish by calling `answer` once. The headline is the direct answer, like "About 20 months to 4 years if all five
  start together." The body backs it up briefly; name recommendations by their label.
- Pick the visual that helps: `timeline` for durations, sequencing or a first plan; `scores` for why a block scores
  what it does; `recommendations` for a set of the block's recommendations; `blocks` for a list of blocks.
  The app draws it from the data, so only give ids.
- `assumptions`: always say what the answer rests on or lacks, e.g. "Funding hasn't been run, so waits for funding
  aren't included", or that a zoning answer is a preview, not SME-approved. If the question depends on funding or
  zoning that isn't approved, say so and offer a run_funding or run_zoning action.
- `actions`: two or three next steps the SME would click, e.g. "Sequence them instead", "Leave out Bars/Night Clubs"
  (type ask, with the full question), or open_recommendation for a plan to open.
- `map`: on a block, the recommendations your answer is about (labelled on its street); across blocks, the block ids.
- "The top N" means the N recommendations with the largest predicted score increase on the block.
- Recommendations overlap when they share a playbook, touch the same feature or the same crossing (e.g. Crosswalks 2
  and Zebra Striping 2 are both at intersection 2), or need the same permit or agency; they conflict when one
  removes what the other needs. Say which, from the data, and mark judgement as such.
- A first (initial) plan: phases of the block's recommendations, using `timeline` with arrangement "phases": quick,
  low-dependency street work first, things that share steps or a crossing together, long-lead items (private
  development, land use, anything needing a special exception) started early. Name each phase in phase_names. Say
  it's a starting point: each recommendation's own plan is still edited and approved on its page.
- If you need the SME's judgement, give what you found first, then one specific question.
- Be concrete about Bowie, Prince George's County and Maryland agencies; write "(confirm)" after any agency, permit
  or body the tools didn't name.
- Plain words, no headings, no preamble. Never say something was saved or changed: you can't change anything."""


# ── The answer, filled in from the data ──────────────────────────────────────

def _references(raw: dict, tools: Tools, texts: list[str]) -> tuple[list[dict], Callable[[str], str]]:
    """The references the answer cites, numbered in order of first use, and a function that renumbers [R3] → [1]."""
    known = {r["id"]: r for r in tools.refs}
    order: list[str] = []
    for t in texts:
        for group in CITE.findall(t or ""):
            for rid in re.split(r"\s*,\s*", group):
                if rid in known and rid not in order:
                    order.append(rid)
    for rid in raw.get("references") or []:
        if isinstance(rid, str) and rid in known and rid not in order:
            order.append(rid)
    num = {rid: i for i, rid in enumerate(order, start=1)}

    def renumber(text: str) -> str:
        def one(m: re.Match) -> str:
            ns = [str(num[r]) for r in re.split(r"\s*,\s*", m.group(1)) if r in num]
            return f"[{', '.join(ns)}]" if ns else ""
        out = CITE.sub(one, text or "")
        # A citation dropped for being unknown leaves "claim ." behind.
        return re.sub(r"[ \t]+([.,;:)])", r"\1", re.sub(r"[ \t]{2,}", " ", out)).strip()

    refs = [{"n": num[rid], "label": known[rid]["label"], "url": known[rid]["url"], "detail": known[rid]["detail"]}
            for rid in order]
    return refs, renumber


def _finish(raw: dict, data: Data, tools: Tools, block_id: int | None) -> dict:
    """The model's answer with every number it shows read from the data, and anything that doesn't resolve dropped."""
    on_block = {r["rec_id"]: r for r in data.recs(block_id)} if block_id else {}
    known = {b["id"]: b for b in data.blocks()}
    headline, body = (raw.get("headline") or "").strip(), (raw.get("body") or "").strip()
    assumptions = [a.strip() for a in raw.get("assumptions") or [] if isinstance(a, str) and a.strip()][:5]
    refs, renumber = _references(raw, tools, [headline, body, *assumptions])
    ans: dict[str, Any] = {"headline": renumber(headline), "body": renumber(body),
                           "assumptions": [renumber(a) for a in assumptions], "references": refs}

    v = raw.get("visual") or {}
    rec_ids = [i for i in v.get("rec_ids") or [] if i in on_block]
    try:
        if v.get("type") == "timeline" and block_id and (rec_ids or v.get("phases")):
            phases = [[i for i in p if i in on_block] for p in v.get("phases") or []]
            t = timeline(data, block_id, rec_ids or [i for p in phases for i in p], v.get("arrangement") or "together",
                         phases)
            if t["bars"]:
                names = [n for n in v.get("phase_names") or [] if isinstance(n, str)]
                ans["visual"] = {"type": "timeline", **t, "phase_names": names}
                tools._source("durations", "Playbook durations",
                              "Each plan's estimate along its longest chain of steps (approved plan, else playbook default)")
        elif v.get("type") == "scores" and block_id:
            b, avg = data.block(block_id), data.averages()
            ans["visual"] = {"type": "scores", "sop": _num(b.get("sop_index_norm")), "sop_average": avg["sop_index_norm"],
                             "rows": [{"dimension": n, "score": _num(b.get(c)), "average": avg[c]} for c, n in DIMENSIONS]}
        elif v.get("type") == "recommendations" and rec_ids:
            ans["visual"] = {"type": "recommendations", "rows": [
                {"rec_id": i, "label": on_block[i]["rec_label"], "dimension": on_block[i].get("dimension"),
                 "direction": on_block[i].get("direction"), "gain": on_block[i].get("predicted_score_increase"),
                 "duration": rec_row(on_block[i])["duration"]} for i in rec_ids]}
        elif v.get("type") == "blocks":
            ids = [i for i in v.get("block_ids") or [] if i in known]
            if ids:
                ans["visual"] = {"type": "blocks", "rows": [
                    {"block_id": i, "street": known[i].get("street_name"), "sop": _num(known[i].get("sop_index_norm"))}
                    for i in ids]}
    except ValueError as e:   # an id the data doesn't hold: answer without the visual
        log.warning("block assistant visual dropped: %s", e)
    ans["sources"] = [{"label": s["label"], "detail": s["detail"]} for s in tools.sources]

    actions = []
    for a in raw.get("actions") or []:
        t, label = a.get("type"), (a.get("label") or "").strip()
        if not label:
            continue
        if t == "ask" and (a.get("question") or "").strip():
            actions.append({"type": "ask", "label": label, "question": a["question"].strip()})
        elif t == "open_recommendation" and a.get("rec_id") in on_block:
            actions.append({"type": t, "label": label, "rec_id": a["rec_id"]})
        elif t in ("run_funding", "run_zoning") and block_id:
            actions.append({"type": t, "label": label})
        elif t == "highlight_blocks" and [i for i in a.get("block_ids") or [] if i in known]:
            actions.append({"type": t, "label": label, "block_ids": [i for i in a["block_ids"] if i in known]})
        elif t == "open_block" and len(a.get("block_ids") or []) == 1 and a["block_ids"][0] in known:
            actions.append({"type": t, "label": label, "block_ids": a["block_ids"]})
    ans["actions"] = actions[:4]

    m = raw.get("map") or {}
    ans["map"] = {
        "block_id": block_id,
        "recs": [{"rec_id": i, "label": on_block[i]["rec_label"]} for i in m.get("rec_ids") or [] if i in on_block][:6],
        "block_ids": [i for i in m.get("block_ids") or [] if i in known],
    }
    if not ans["headline"]:
        ans["headline"], ans["body"] = ans["body"], ""
    return ans


# ── The turn ─────────────────────────────────────────────────────────────────

def _context_text(data: Data, scope: str, block_id: int | None, rec_ids: list[int]) -> str:
    if scope == "all" or not block_id:
        lines = [f"Scope: all {len(data.blocks())} blocks."]
        if block_id:
            b = data.block(block_id)
            lines.append(f"The SME has block {block_id} ({b.get('street_name')}) open.")
        return "\n".join(lines)
    b = data.block(block_id)
    lines = [f"Scope: block {block_id}, {b.get('street_name')} "
             f"({b.get('intersection_a') or '?'} → {b.get('intersection_b') or '?'})."]
    for rid in rec_ids:
        try:
            lines.append(f"About recommendation {rid}: {data.rec(block_id, rid)['rec_label']}.")
        except ValueError:
            pass
    return "\n".join(lines)


def _history(history: list[dict] | None) -> list[dict]:
    """Earlier turns as alternating user / assistant text, starting with a user turn and ending with an answer."""
    messages: list[dict] = []
    for h in (history or [])[-HISTORY_TURNS:]:
        role, text = h.get("role"), (h.get("text") or "").strip()
        if role not in ("user", "assistant") or not text:
            continue
        if messages and messages[-1]["role"] == role:
            messages[-1]["content"] += "\n\n" + text
        else:
            messages.append({"role": role, "content": text})
    if messages and messages[0]["role"] == "assistant":
        messages.pop(0)
    if messages and messages[-1]["role"] == "user":
        messages.pop()
    return messages


def ask(db: Client, question: str, scope: str = "block", block_id: int | None = None,
        rec_ids: list[int] | None = None, history: list[dict] | None = None,
        progress: Callable[[str], None] | None = None) -> dict:
    """One question and its answer. history: [{role: user | assistant, text}], oldest first."""
    data = Data(db)
    tools = Tools(data)
    scope = "all" if scope == "all" or not block_id else "block"
    if block_id:
        data.block(block_id)

    messages = _history(history)
    messages.append({"role": "user", "content": [
        {"type": "text", "text": f"<context>\n{_context_text(data, scope, block_id, rec_ids or [])}\n</context>"},
        {"type": "text", "text": question.strip()}]})

    client = _get_client()
    system = [{"type": "text", "text": SYSTEM.format(n_blocks=len(data.blocks()))}]
    final: dict | None = None
    text_reply = ""
    for _ in range(MAX_TOOL_ROUNDS):
        resp = _create(client, model=MODEL, max_tokens=8000, system=system, tools=[*TOOLS, ANSWER_TOOL],
                       messages=messages, cache_control={"type": "ephemeral"}, output_config={"effort": "medium"})
        content = [b.model_dump(mode="json", exclude_none=True) for b in resp.content]
        messages.append({"role": "assistant", "content": content})
        text_reply = "\n\n".join(b["text"] for b in content if b.get("type") == "text" and b.get("text")) or text_reply
        uses = [b for b in content if b.get("type") == "tool_use"]
        answer_use = next((b for b in uses if b["name"] == "answer"), None)
        if answer_use:
            final = answer_use.get("input") or {}
            break
        if resp.stop_reason == "refusal":
            return {"headline": "The model declined to answer this.", "body": "", "sources": [], "references": [],
                    "actions": [], "assumptions": [], "map": {"block_id": block_id, "recs": [], "block_ids": []}}
        if resp.stop_reason != "tool_use" or not uses:
            break
        results = []
        for b in uses:
            args = b.get("input") or {}
            if progress and b["name"] in PROGRESS:
                progress(PROGRESS[b["name"]].format(block_id=args.get("block_id", "")))
            result, is_error = tools.run(b["name"], args)
            results.append({"type": "tool_result", "tool_use_id": b["id"], "content": json.dumps(result, default=str),
                            **({"is_error": True} if is_error else {})})
        messages.append({"role": "user", "content": results})

    if final is None:
        # Ended in plain text, or ran out of rounds: show what it said.
        first, _, rest = (text_reply or "I couldn't finish that answer. Try asking it more narrowly.").partition("\n")
        final = {"headline": first.strip(), "body": rest.strip()}
    if progress:
        progress("Writing the answer")
    return _finish(final, data, tools, block_id)
