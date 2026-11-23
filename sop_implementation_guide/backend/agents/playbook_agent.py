"""
PlaybookAgent: the chat beside the Playbooks tab's canvas.

The planner asks for a change ("add a playbook for bike share", "an SME should
review step 4", "split the permit step") and the agent makes it on a copy of
the playbooks the planner has open, with tools that add, change and remove
playbooks and steps and move recommendation types between them. It never
saves anything: the copy comes back as a proposal, drawn on the canvas with
its changes, which the planner adds to their draft (or discards) and then
saves as a playbook version like any other edit.

The conversation is held by the browser and sent with each turn, raw (tool
calls and results included, earlier turns' thinking left out, as in
pipeline_builder), so there is nothing to store here. Each user turn carries a
snapshot of the playbooks as they stand, so the agent sees the planner's own
edits between turns.
"""

from __future__ import annotations

import copy
import json
import logging
import re
from typing import Any

from backend.agents.pipeline_builder import _create
from backend.tools.llm_extractor import _get_client
from backend.tools.plan_graph import ACTORS, diff_steps, find_cycle, upgrade_steps

log = logging.getLogger(__name__)

MODEL = "claude-opus-5-5"
MAX_TOOL_ROUNDS = 10
STATE_TAG = "playbooks_state"
THINKING = ("thinking", "redacted_thinking")

STEP_KEYS = ("title", "how", "actor", "owner", "secondary_owner", "time", "depends_on", "depends_mode",
             "condition", "filled_by")

SYSTEM = """You are the playbook agent in a planning app for the City of Bowie, Maryland (Prince George's County),
used to implement State of Place block recommendations (e.g. "Public Garden", "Crosswalks 1", "Blank walls").

A playbook is the standard sequence of steps a city follows to get one kind of change done. Each of the 65
recommendation types maps to one playbook; every block's implementation plan for that recommendation starts from
it, and an SME (subject matter expert) then reviews and approves the plan per block. The planner sees the
playbooks as a canvas (a flowchart of steps) beside this chat.

Each step has:
- title: the activity, short (under ~80 characters), starting with a verb.
- how: the activity description, one or two plain sentences on how it gets done.
- depends_on: ids of the steps it waits for ("s1", "s3"); depends_mode "all" (default) or "any" (whichever
  finishes first). A step with no dependencies starts the plan.
- condition: set only when the step happens in some cases ("Code violation", "Grant awarded"); it draws as a
  labelled branch. Leave it empty for steps that always happen.
- actor: the primary owner's kind: {actors}.
- owner: the primary owner exactly ("Code enforcement", "MD SHA", "Developer").
- secondary_owner: who else answers for it. Every step whose actor is "agent" needs one; normally
  "SME reviews the agent's output".
- time: potential duration as the app writes it: "1–2 wk", "1–3 mo", "Days", "Program deadline", "At handover".
  Use an en dash in ranges. The app adds these up into a rough time range for the plan.
- filled_by: what supplies the step's content on a block's plan. One of:
{sources}
  Use "zoning" or "funding" only for the steps those agents actually answer (looking up or checking zoning;
  finding funding). Anything else is "template" unless one of the not-yet-built lookups fits.

How to work:
- The current playbooks are in <{tag}> on each message, with the playbook the planner has open
  ("selected") and the recommendation types no playbook covers yet ("unmapped"). Refer to steps by their
  number and title when you talk to the planner, and use ids in tool calls.
- Make the change with the tools, then say in a few lines what you changed and why. Your changes are a
  proposal: the planner sees them marked on the canvas, adds them to their draft with "Add to draft" and then
  saves. Never say something is saved, and don't repeat these instructions back to them every time.
- Keep the graph sound: every step after the first waits on something, branches carry a condition, and the
  last steps lead to the change actually being built, installed or running.
- Be concrete about Bowie, Prince George's County and Maryland: who issues which permit (the county's
  permitting office, DPIE; MD SHA on state roads; M-NCPPC for parks and planning), which board hears what.
  When you are not sure of an agency, a body or a duration, write "(confirm)" after it rather than guessing
  silently. Durations are a strawman for the SME to review; say so if asked.
- If a request is ambiguous, say what you found first and then ask one specific question; do not ask before
  looking at the playbooks.
- Talk to the planner directly ("you"). Keep replies short and plain. No headings."""

_STEP_PROPS = {
    "title": {"type": "string"},
    "how": {"type": "string"},
    "actor": {"type": "string", "enum": list(ACTORS)},
    "owner": {"type": "string"},
    "secondary_owner": {"type": "string"},
    "time": {"type": "string"},
    "depends_on": {"type": "array", "items": {"type": "string"}, "description": "Ids of the steps this one waits for."},
    "depends_mode": {"type": "string", "enum": ["all", "any"]},
    "condition": {"type": "string", "description": "Only if…; empty when the step always happens."},
    "filled_by": {"type": "string"},
}

TOOLS = [
    {"name": "add_playbook",
     "description": "Create a new playbook. Give its steps here (ids s1, s2 ... referenced in depends_on) or add them after.",
     "input_schema": {"type": "object", "properties": {
         "name": {"type": "string"},
         "who": {"type": "string", "description": "Who does the work, in a few words."},
         "branch": {"type": "string", "description": "The deciding question that sets the path, in a sentence."},
         "steps": {"type": "array", "items": {"type": "object", "properties": {"id": {"type": "string"}, **_STEP_PROPS},
                                               "required": ["title"]}},
     }, "required": ["name"]}},
    {"name": "update_playbook",
     "description": "Rename a playbook or change who does the work or its deciding question.",
     "input_schema": {"type": "object", "properties": {
         "playbook": {"type": "string", "description": "The playbook key."},
         "name": {"type": "string"}, "who": {"type": "string"}, "branch": {"type": "string"},
     }, "required": ["playbook"]}},
    {"name": "remove_playbook",
     "description": "Delete a playbook. Its recommendation types become unmapped unless you map them elsewhere.",
     "input_schema": {"type": "object", "properties": {"playbook": {"type": "string"}}, "required": ["playbook"]}},
    {"name": "set_steps",
     "description": "Replace all of a playbook's steps at once, for a restructure. Ids s1, s2 ... in depends_on.",
     "input_schema": {"type": "object", "properties": {
         "playbook": {"type": "string"},
         "steps": {"type": "array", "items": {"type": "object", "properties": {"id": {"type": "string"}, **_STEP_PROPS},
                                               "required": ["title"]}},
     }, "required": ["playbook", "steps"]}},
    {"name": "add_step",
     "description": "Add one step. It goes after `after` (a step id) in the list and, unless depends_on is given, waits on it.",
     "input_schema": {"type": "object", "properties": {
         "playbook": {"type": "string"}, "after": {"type": "string"}, **_STEP_PROPS,
     }, "required": ["playbook", "title"]}},
    {"name": "update_step",
     "description": "Change some fields of one step. Only the fields given change; send condition \"\" to clear it.",
     "input_schema": {"type": "object", "properties": {
         "playbook": {"type": "string"}, "step_id": {"type": "string"}, **_STEP_PROPS,
     }, "required": ["playbook", "step_id"]}},
    {"name": "remove_step",
     "description": "Remove one step. Steps that waited on it then wait on what it waited on.",
     "input_schema": {"type": "object", "properties": {
         "playbook": {"type": "string"}, "step_id": {"type": "string"},
     }, "required": ["playbook", "step_id"]}},
    {"name": "map_recommendation_types",
     "description": "Move recommendation types (exact labels) into a playbook, or out of every playbook with playbook \"\".",
     "input_schema": {"type": "object", "properties": {
         "playbook": {"type": "string"},
         "labels": {"type": "array", "items": {"type": "string"}},
     }, "required": ["playbook", "labels"]}},
]

TOOL_LABELS = {
    "add_playbook": "added a playbook", "update_playbook": "changed a playbook", "remove_playbook": "removed a playbook",
    "set_steps": "rewrote the steps", "add_step": "added a step", "update_step": "changed a step",
    "remove_step": "removed a step", "map_recommendation_types": "moved recommendation types",
}


class ToolError(Exception):
    pass


def _slug(name: str, taken: list[str]) -> str:
    base = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "playbook"
    key, n = base, 2
    while key in taken:
        key, n = f"{base}_{n}", n + 1
    return key


class Work:
    """The planner's playbooks as the agent edits them: a copy, checked after every change."""

    def __init__(self, doc: dict, sources: dict, labels: list[str]):
        self.doc = copy.deepcopy(doc)
        self.doc.setdefault("pathways", [])
        self.doc.setdefault("rec_types", {})
        self.sources = sources
        self.labels = set(labels) | set(self.doc["rec_types"])

    def playbook(self, key: str) -> dict:
        p = next((p for p in self.doc["pathways"] if p["key"] == key), None)
        if not p:
            raise ToolError(f"No playbook '{key}'. Keys: {', '.join(x['key'] for x in self.doc['pathways'])}.")
        return p

    def _fields(self, raw: dict) -> dict:
        out = {}
        for k in STEP_KEYS:
            if k not in raw:
                continue
            v = raw[k]
            if k == "depends_on":
                if not isinstance(v, list):
                    raise ToolError("depends_on must be a list of step ids.")
                out[k] = [str(x) for x in v]
                continue
            v = "" if v is None else str(v).strip()
            if k == "actor" and v not in ACTORS:
                raise ToolError(f"actor must be one of {', '.join(ACTORS)}.")
            if k == "filled_by" and v and v not in self.sources:
                raise ToolError(f"filled_by must be one of {', '.join(self.sources)}.")
            if k == "depends_mode" and v not in ("all", "any"):
                raise ToolError("depends_mode must be 'all' or 'any'.")
            out[k] = v
        return out

    def _commit(self, p: dict, steps: list[dict]) -> list[list[str]]:
        known = {s["id"] for s in steps if s.get("id")}
        for s in steps:
            bad = [d for d in s.get("depends_on") or [] if d not in known]
            if bad:
                raise ToolError(f"Step '{s.get('title')}' waits on unknown step id(s) {', '.join(bad)}.")
        steps = upgrade_steps(steps)
        for s in steps:
            if not (s.get("title") or "").strip():
                raise ToolError("Every step needs a title.")
            s.setdefault("filled_by", "template")
            if not s.get("condition"):
                s.pop("condition", None)
            if s.get("depends_mode") == "all":
                s.pop("depends_mode", None)
        loop = find_cycle(steps)
        if loop:
            more = f" and {len(loop) - 4} more" if len(loop) > 4 else ""
            raise ToolError(f"That would make steps wait on each other in a loop (involving {'; '.join(loop[:4])}{more}).")
        p["steps"] = steps
        return [[s["id"], s["title"]] for s in steps]

    def _steps_from(self, raw_steps: list[dict]) -> list[dict]:
        steps, seen = [], set()
        for i, raw in enumerate(raw_steps, start=1):
            s = self._fields(raw)
            sid = str(raw.get("id") or "").strip()
            s["id"] = sid if sid and sid not in seen else f"s{i}"
            seen.add(s["id"])
            steps.append(s)
        return steps

    # ── tools ──

    def add_playbook(self, name: str, who: str = "", branch: str = "", steps: list | None = None) -> dict:
        name = (name or "").strip()
        if not name:
            raise ToolError("A playbook needs a name.")
        key = _slug(name, [p["key"] for p in self.doc["pathways"]])
        p = {"key": key, "name": name, "who": (who or "").strip(), "branch": (branch or "").strip(), "steps": []}
        self.doc["pathways"].append(p)
        out = {"playbook": key}
        if steps:
            out["steps"] = self._commit(p, self._steps_from(steps))
        return out

    def update_playbook(self, playbook: str, **fields) -> dict:
        p = self.playbook(playbook)
        for k in ("name", "who", "branch"):
            if fields.get(k) is not None:
                p[k] = str(fields[k]).strip()
        return {"playbook": playbook, "name": p["name"]}

    def remove_playbook(self, playbook: str) -> dict:
        self.playbook(playbook)
        self.doc["pathways"] = [p for p in self.doc["pathways"] if p["key"] != playbook]
        freed = [l for l, k in self.doc["rec_types"].items() if k == playbook]
        for l in freed:
            del self.doc["rec_types"][l]
        return {"removed": playbook, "unmapped": freed}

    def set_steps(self, playbook: str, steps: list) -> dict:
        p = self.playbook(playbook)
        return {"steps": self._commit(p, self._steps_from(steps or []))}

    def add_step(self, playbook: str, after: str | None = None, **raw) -> dict:
        p = self.playbook(playbook)
        steps = [dict(s) for s in p["steps"]]
        ids = {s["id"] for s in steps}
        if after and after not in ids:
            raise ToolError(f"No step '{after}' in {playbook}.")
        s = self._fields(raw)
        k = len(steps) + 1
        while f"s{k}" in ids:
            k += 1
        s["id"] = f"s{k}"
        at = next(i for i, x in enumerate(steps) if x["id"] == after) + 1 if after else len(steps)
        if "depends_on" not in s:
            prev = after or (steps[-1]["id"] if steps else None)
            s["depends_on"] = [prev] if prev else []
        steps.insert(at, s)
        self._commit(p, steps)
        return {"added": s["id"], "steps": [[x["id"], x["title"]] for x in p["steps"]]}

    def update_step(self, playbook: str, step_id: str, **raw) -> dict:
        p = self.playbook(playbook)
        steps = [dict(s) for s in p["steps"]]
        s = next((x for x in steps if x["id"] == step_id), None)
        if not s:
            raise ToolError(f"No step '{step_id}' in {playbook}.")
        s.update(self._fields(raw))
        self._commit(p, steps)
        return {"updated": step_id}

    def remove_step(self, playbook: str, step_id: str) -> dict:
        p = self.playbook(playbook)
        gone = next((x for x in p["steps"] if x["id"] == step_id), None)
        if not gone:
            raise ToolError(f"No step '{step_id}' in {playbook}.")
        steps = []
        for x in p["steps"]:
            if x["id"] == step_id:
                continue
            x = dict(x)
            if step_id in (x.get("depends_on") or []):
                x["depends_on"] = list(dict.fromkeys(
                    [d for d in x["depends_on"] if d != step_id] + list(gone.get("depends_on") or [])))
            steps.append(x)
        self._commit(p, steps)
        return {"removed": step_id}

    def map_recommendation_types(self, playbook: str, labels: list) -> dict:
        if playbook:
            self.playbook(playbook)
        unknown = [l for l in labels if l not in self.labels]
        if unknown:
            raise ToolError(f"Unknown recommendation type(s): {', '.join(unknown)}. Use the exact labels.")
        for l in labels:
            if playbook:
                self.doc["rec_types"][l] = playbook
            else:
                self.doc["rec_types"].pop(l, None)
        return {"mapped": labels, "to": playbook or None}

    def run(self, name: str, args: dict) -> tuple[Any, bool]:
        try:
            fn = getattr(self, name, None)
            if name not in TOOL_LABELS or fn is None:
                return {"error": f"Unknown tool {name}"}, True
            return fn(**args), False
        except ToolError as e:
            return {"error": str(e)}, True
        except TypeError as e:
            return {"error": f"Bad arguments: {e}"}, True


# ── What the proposal changes ────────────────────────────────────────────────

def proposal_changes(before: dict, after: dict) -> list[dict]:
    """One line per change, for the proposal card: {op: + ~ - →, playbook, text}."""
    lines = []
    old = {p["key"]: p for p in before.get("pathways") or []}
    new = {p["key"]: p for p in after.get("pathways") or []}
    for key, p in new.items():
        name = p.get("name") or key
        if key not in old:
            lines.append({"op": "+", "playbook": key, "text": f"New playbook “{name}” ({len(p.get('steps') or [])} steps)"})
            continue
        o = old[key]
        fields = [label for f, label in (("name", "name"), ("who", "who does the work"), ("branch", "deciding question"))
                  if (o.get(f) or "") != (p.get(f) or "")]
        if fields:
            lines.append({"op": "~", "playbook": key, "text": f"“{name}”: changed {', '.join(fields)}"})
        d = diff_steps(upgrade_steps(o.get("steps") or []), upgrade_steps(p.get("steps") or []))
        for a in d["added"]:
            lines.append({"op": "+", "playbook": key, "text": f"“{name}” step {a['n']}: {a['title']}"})
        for c in d["changed"]:
            lines.append({"op": "~", "playbook": key,
                          "text": f"“{name}” step {c['n']}: {c['title']} ({', '.join(f['label'] for f in c['fields'])})"})
        for r in d["removed"]:
            lines.append({"op": "-", "playbook": key, "text": f"“{name}”: removed step “{r.get('title')}”"})
    for key, p in old.items():
        if key not in new:
            lines.append({"op": "-", "playbook": key, "text": f"Removed playbook “{p.get('name') or key}”"})
    names = {**{k: p.get("name") or k for k, p in old.items()}, **{k: p.get("name") or k for k, p in new.items()}}
    ot, nt = before.get("rec_types") or {}, after.get("rec_types") or {}
    for label in sorted(set(ot) | set(nt)):
        if ot.get(label) != nt.get(label):
            to = f"to “{names.get(nt[label], nt[label])}”" if nt.get(label) else "out of every playbook"
            lines.append({"op": "→", "playbook": nt.get(label) or ot.get(label), "text": f"“{label}” moved {to}"})
    return lines


# ── The turn ─────────────────────────────────────────────────────────────────

def _snapshot(doc: dict, selected: str | None, labels: list[str]) -> dict:
    keep = ("id", "title", "how", "actor", "owner", "secondary_owner", "time", "depends_on", "depends_mode",
            "condition", "filled_by")
    pathways = []
    for p in doc.get("pathways") or []:
        pathways.append({
            "key": p["key"], "name": p.get("name"), "who": p.get("who"), "branch": p.get("branch"),
            "recommendation_types": sorted(l for l, k in (doc.get("rec_types") or {}).items() if k == p["key"]),
            "steps": [{k: s[k] for k in keep if s.get(k) not in (None, "", [])} for s in p.get("steps") or []],
        })
    return {"selected": selected, "playbooks": pathways,
            "unmapped": sorted(l for l in labels if l not in (doc.get("rec_types") or {}))}


def _without_thinking(messages: list[dict]) -> list[dict]:
    out = []
    for m in messages:
        content = m["content"]
        if m["role"] == "assistant" and isinstance(content, list):
            content = [b for b in content if not (isinstance(b, dict) and b.get("type") in THINKING)]
            if not content:
                content = [{"type": "text", "text": "(no reply)"}]
        out.append({"role": m["role"], "content": content})
    return out


def _chip(block: dict, error: str | None) -> dict:
    a = block.get("input") or {}
    detail = a.get("title") or a.get("name") or a.get("step_id") or ", ".join(a.get("labels") or []) or a.get("playbook") or ""
    return {"id": block.get("id"), "label": TOOL_LABELS.get(block.get("name"), block.get("name")),
            "detail": str(detail)[:120], "error": error}


def run_turn(messages: list[dict], text: str, draft: dict, proposal: dict | None, selected: str | None,
             sources: dict, labels: list[str]) -> dict:
    """
    One planner message and everything the agent does in reply.

    The agent works on the pending proposal if there is one (so a follow-up
    builds on it), else on the planner's draft. Returns the conversation to
    send next time, what to show in the chat, and the proposal (None when the
    playbooks end up as the draft already has them).
    """
    work = Work(proposal or draft, sources, labels)
    system = SYSTEM.format(
        actors="; ".join(f"{k}" for k in ACTORS) + " (agent = this app's zoning lookup or funding agent; city = City "
               "of Bowie staff, council or contractors it hires; builder = the property owner, developer or their "
               "contractor; other = anyone else: the state, the county, M-NCPPC, boards)",
        sources="\n".join(f"  - {k}: {v.get('label')}{'' if v.get('live') else ' (not built yet; the SME fills it in)'}"
                          for k, v in sources.items()),
        tag=STATE_TAG,
    )
    history = _without_thinking(messages or [])
    user = [{"type": "text", "text": text},
            {"type": "text", "text": f"<{STATE_TAG}>\n{json.dumps(_snapshot(work.doc, selected, labels))}\n</{STATE_TAG}>"}]
    history.append({"role": "user", "content": user})
    shown: list[dict] = [{"kind": "user", "text": text}]

    client = _get_client()
    for _ in range(MAX_TOOL_ROUNDS):
        resp = _create(client, model=MODEL, max_tokens=16000, system=[{"type": "text", "text": system}],
                       tools=TOOLS, messages=history, cache_control={"type": "ephemeral"},
                       output_config={"effort": "medium"})
        content = [b.model_dump(mode="json", exclude_none=True) for b in resp.content]
        history.append({"role": "assistant", "content": content})
        uses = [b for b in content if b.get("type") == "tool_use"]
        results, errors = [], {}
        for b in uses:
            result, is_error = work.run(b["name"], b.get("input") or {})
            if is_error:
                errors[b["id"]] = result.get("error")
            results.append({"type": "tool_result", "tool_use_id": b["id"], "content": json.dumps(result, default=str),
                            **({"is_error": True} if is_error else {})})
        reply = "\n\n".join(b["text"] for b in content if b.get("type") == "text" and b.get("text"))
        if reply or uses:
            shown.append({"kind": "assistant", "text": reply, "tools": [_chip(b, errors.get(b["id"])) for b in uses]})
        if resp.stop_reason == "refusal":
            shown.append({"kind": "event", "level": "warn", "text": "The model declined to continue this request."})
            break
        if resp.stop_reason == "max_tokens":
            shown.append({"kind": "event", "level": "warn", "text": "The reply was cut off (too long). Ask it to continue."})
            break
        if resp.stop_reason != "tool_use":
            break
        history.append({"role": "user", "content": results})
    else:
        shown.append({"kind": "event", "level": "warn", "text": "The agent stopped after many changes in one turn. Reply to continue."})

    # A turn cut short can leave tool calls unanswered, which the API rejects next time.
    last = history[-1]
    if last["role"] == "assistant":
        pending = [b for b in last["content"] if isinstance(b, dict) and b.get("type") == "tool_use"]
        if pending:
            history.append({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": b["id"], "is_error": True, "content": "Not run: the turn ended."}
                for b in pending]})

    changes = proposal_changes(draft, work.doc)
    return {
        "messages": _without_thinking(history),
        "display": shown,
        "proposal": {"doc": work.doc, "changes": changes} if changes else None,
    }
