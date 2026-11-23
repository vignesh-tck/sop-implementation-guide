"""
PlaybookAgent: the assistant beside the Playbooks tab's canvas.

Three ways in, one result. Whatever the assistant does comes back as a target:
the playbooks as it would have them, plus a one-line reason for each thing it
touched. The browser compares the target with the planner's draft and shows
every difference as a separate suggestion (add this step, change that
duration, link these two steps) to accept or reject one at a time. Nothing is
saved here; an accepted suggestion becomes part of the draft, which the
planner saves as a playbook version like any other edit.

- run_turn: the chat. The planner asks for a change ("owners who can't afford
  repairs get stuck after step 2") or, in "answer" mode, just a question. The
  agent edits a copy of the playbooks with tools.
- assist: one quick action on one step (rewrite its description, estimate its
  duration, suggest owners), on a faster model, for the step panel.
- draft_playbook: a whole new playbook from a purpose, optional source files
  and a playbook to model it on, streamed a step at a time so the planner sees
  the outline grow.

Every step the assistant writes carries a `source`: the file and page, the
playbook step it was modelled on, or "general practice (confirm)". It stays on
the step once accepted and is cited on every block's plan.

The chat is held by the browser and sent with each turn, raw (tool calls and
results included, earlier turns' thinking left out, as in pipeline_builder).
"""

from __future__ import annotations

import copy
import json
import logging
import re
from io import BytesIO
from typing import Any, Callable

import httpx
import pypdf

from backend.agents.pipeline_builder import _create
from backend.tools import web_source
from backend.tools.llm_extractor import _get_client, structured_call
from backend.tools.plan_graph import ACTORS, find_cycle, upgrade_steps

log = logging.getLogger(__name__)

MODEL = "claude-opus-5-5"
FAST_MODEL = "claude-sonnet-5-5"
MAX_TOOL_ROUNDS = 10
STATE_TAG = "playbooks_state"
THINKING = ("thinking", "redacted_thinking")
FILE_CHARS = 30_000

STEP_KEYS = ("title", "how", "actor", "owner", "secondary_owner", "time", "depends_on", "depends_mode",
             "condition", "filled_by", "source")

ACTOR_TEXT = ("agent = this app's zoning lookup or funding agent; city = City of Bowie staff, council or contractors "
              "it hires; builder = the property owner, developer or their contractor; other = anyone else: the state, "
              "the county, M-NCPPC, a land bank, boards")

STEP_GUIDE = """Each step has:
- title: the activity, short (under ~80 characters), starting with a verb.
- how: the activity description, one or two plain sentences on how it gets done.
- depends_on: ids of the steps it waits for ("s1", "s3"); depends_mode "all" (default) or "any" (whichever
  finishes first). A step with no dependencies starts the plan. A step that follows alternative branches
  (only one of them happens, e.g. owner permission or land bank) must use depends_mode "any".
- condition: set only when the step happens in some cases ("Code violation", "Owner can't afford repairs"); it
  draws as a labelled branch. Leave it empty for steps that always happen.
- actor: the primary owner's kind, one of agent, city, builder, other ({actors}).
- owner: the primary owner exactly ("Code enforcement", "MD SHA", "Developer").
- secondary_owner: who else answers for it. Every step whose actor is "agent" needs one; normally
  "SME reviews the agent's output".
- time: potential duration as the app writes it: "1–2 wk", "1–3 mo", "Days", "Program deadline", "At handover".
  Use an en dash in ranges.
- filled_by: what supplies the step's content on a block's plan. One of:
{sources}
  Use "zoning" or "funding" only for the steps those agents actually answer (looking up or checking zoning;
  finding funding). Anything else is "template" unless one of the not-yet-built lookups fits.
- source: where this step comes from, short: a file and page ("Vacant_Lot_SOP.pdf p. 4"), the playbook step it
  is modelled on ("Parks playbook, step 1"), or "General practice (confirm)". Always set it on steps you write."""

SYSTEM = """You are the playbook assistant in a planning app for the City of Bowie, Maryland (Prince George's
County), used to implement State of Place block recommendations (e.g. "Public Garden", "Crosswalks 1", "Blank walls").

A playbook is the standard sequence of steps a city follows to get one kind of change done. Each of the 65
recommendation types maps to one playbook; every block's implementation plan for that recommendation starts from
it, and an SME (subject matter expert) then reviews and approves the plan per block. The planner sees the
playbooks as a canvas (a flowchart of steps) beside this chat.

{step_guide}

How to work:
- The current playbooks are in <{tag}> on each message, with the playbook the planner has open
  ("selected") and the recommendation types no playbook covers yet ("unmapped"). Steps are numbered in list
  order; refer to them by number and title when you talk to the planner, and use ids in tool calls. "@step 3"
  in a message means step 3 of the selected playbook.
- {mode}
- Keep the graph sound: every step after the first waits on something, branches carry a condition, and the
  last steps lead to the change actually being built, installed or running.
- Be concrete about Bowie, Prince George's County and Maryland: who issues which permit (the county's
  permitting office, DPIE; MD SHA on state roads; M-NCPPC for parks and planning), which board hears what.
  When you are not sure of an agency, a body or a duration, write "(confirm)" after it rather than guessing
  silently. Durations are a strawman for the SME to review.
- Attached files appear in <file> tags with [p. N] page markers; cite them by name and page in `source`.
- If a request is ambiguous, say what you found first, then ask one specific question.
- Talk to the planner directly ("you"). Keep replies short and plain. No headings."""

PROPOSE_MODE = """Make changes with the tools. Each change you make appears to the planner as its own suggestion,
  with your `reason`, which they accept or reject separately, so change only what the request needs: prefer
  add_step and update_step over set_steps for an existing playbook, keep existing steps' ids, and don't rewrite
  steps you aren't changing. Give every tool call a short `reason` (why, in under 12 words). Then say in a few
  lines what you suggested and why. Never say something is saved or applied."""

ANSWER_MODE = """You are in "just answer" mode: answer the question from the playbooks; you cannot change
  anything now. If a change would help, say what you would change, and that they can switch to "Propose changes"."""

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
    "source": {"type": "string", "description": "Where this step comes from (file and page, playbook step, or general practice)."},
}
_REASON = {"reason": {"type": "string", "description": "Why, in under 12 words. Shown to the planner with the suggestion."}}
_STEP_LIST = {"type": "array", "items": {"type": "object", "properties": {"id": {"type": "string"}, **_STEP_PROPS},
                                         "required": ["title"]}}

TOOLS = [
    {"name": "add_playbook",
     "description": "Create a new playbook. Give its steps here (ids s1, s2 ... referenced in depends_on) or add them after.",
     "input_schema": {"type": "object", "properties": {
         "name": {"type": "string"},
         "who": {"type": "string", "description": "Who does the work, in a few words."},
         "branch": {"type": "string", "description": "The deciding question that sets the path, in a sentence."},
         "steps": _STEP_LIST, **_REASON,
     }, "required": ["name", "reason"]}},
    {"name": "update_playbook",
     "description": "Rename a playbook or change who does the work or its deciding question.",
     "input_schema": {"type": "object", "properties": {
         "playbook": {"type": "string", "description": "The playbook key."},
         "name": {"type": "string"}, "who": {"type": "string"}, "branch": {"type": "string"}, **_REASON,
     }, "required": ["playbook", "reason"]}},
    {"name": "remove_playbook",
     "description": "Delete a playbook. Its recommendation types become unmapped unless you map them elsewhere.",
     "input_schema": {"type": "object", "properties": {"playbook": {"type": "string"}, **_REASON},
                      "required": ["playbook", "reason"]}},
    {"name": "set_steps",
     "description": "Replace all of a playbook's steps at once, only for a real restructure. Keep existing steps' ids.",
     "input_schema": {"type": "object", "properties": {"playbook": {"type": "string"}, "steps": _STEP_LIST, **_REASON},
                      "required": ["playbook", "steps", "reason"]}},
    {"name": "add_step",
     "description": "Add one step. It goes after `after` (a step id) in the list and, unless depends_on is given, waits on it.",
     "input_schema": {"type": "object", "properties": {
         "playbook": {"type": "string"}, "after": {"type": "string"}, **_STEP_PROPS, **_REASON,
     }, "required": ["playbook", "title", "reason"]}},
    {"name": "update_step",
     "description": "Change some fields of one step, including what it depends on. Only the fields given change; "
                    "send condition \"\" to clear it.",
     "input_schema": {"type": "object", "properties": {
         "playbook": {"type": "string"}, "step_id": {"type": "string"}, **_STEP_PROPS, **_REASON,
     }, "required": ["playbook", "step_id", "reason"]}},
    {"name": "remove_step",
     "description": "Remove one step. Steps that waited on it then wait on what it waited on.",
     "input_schema": {"type": "object", "properties": {
         "playbook": {"type": "string"}, "step_id": {"type": "string"}, **_REASON,
     }, "required": ["playbook", "step_id", "reason"]}},
    {"name": "map_recommendation_types",
     "description": "Move recommendation types (exact labels) into a playbook, or out of every playbook with playbook \"\".",
     "input_schema": {"type": "object", "properties": {
         "playbook": {"type": "string"}, "labels": {"type": "array", "items": {"type": "string"}}, **_REASON,
     }, "required": ["playbook", "labels", "reason"]}},
]

TOOL_LABELS = {
    "add_playbook": "suggested a playbook", "update_playbook": "suggested a playbook change",
    "remove_playbook": "suggested removing a playbook", "set_steps": "suggested new steps",
    "add_step": "suggested a step", "update_step": "suggested a step change", "remove_step": "suggested removing a step",
    "map_recommendation_types": "suggested moving recommendation types",
}


class ToolError(Exception):
    pass


def _slug(name: str, taken: list[str]) -> str:
    base = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "playbook"
    key, n = base, 2
    while key in taken:
        key, n = f"{base}_{n}", n + 1
    return key


def _next_id(taken: set[str]) -> str:
    nums = [int(m.group(1)) for i in taken if (m := re.fullmatch(r"s(\d+)", i))]
    return f"s{max(nums, default=0) + 1}"


class Work:
    """The planner's playbooks as the assistant edits them: a copy, checked after every change."""

    def __init__(self, doc: dict, sources: dict, labels: list[str]):
        self.doc = copy.deepcopy(doc)
        self.doc.setdefault("pathways", [])
        self.doc.setdefault("rec_types", {})
        self.sources = sources
        self.labels = set(labels) | set(self.doc["rec_types"])
        # Why each thing was touched, keyed as the browser keys its suggestions:
        # "playbook:<key>", "step:<key>:<id>", "rec:<label>".
        self.reasons: dict[str, str] = {}

    def _why(self, key: str, reason: str | None) -> None:
        if reason and reason.strip():
            self.reasons[key] = reason.strip()[:200]

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
            for k in ("condition", "source", "secondary_owner"):
                if not s.get(k):
                    s.pop(k, None)
            if s.get("depends_mode") == "all":
                s.pop("depends_mode", None)
        loop = find_cycle(steps)
        if loop:
            more = f" and {len(loop) - 4} more" if len(loop) > 4 else ""
            raise ToolError(f"That would make steps wait on each other in a loop (involving {'; '.join(loop[:4])}{more}).")
        p["steps"] = steps
        return [[s["id"], s["title"]] for s in steps]

    def _steps_from(self, raw_steps: list[dict], old: list[dict]) -> list[dict]:
        """Steps as given, keeping the ids of existing steps and giving new ones fresh ids."""
        taken = {s["id"] for s in old}
        given = [str(r.get("id") or "").strip() for r in raw_steps]
        rename: dict[str, str] = {}
        steps, seen = [], set()
        for raw, sid in zip(raw_steps, given):
            s = self._fields(raw)
            if not sid or sid in seen:
                new = _next_id(taken | seen | set(given))
                if sid:
                    rename[sid] = new
                sid = new
            s["id"] = sid
            seen.add(sid)
            steps.append(s)
        for s in steps:
            s["depends_on"] = [rename.get(d, d) for d in s.get("depends_on") or []]
        return steps

    # ── tools ──

    def add_playbook(self, name: str, reason: str = "", who: str = "", branch: str = "", steps: list | None = None) -> dict:
        name = (name or "").strip()
        if not name:
            raise ToolError("A playbook needs a name.")
        key = _slug(name, [p["key"] for p in self.doc["pathways"]])
        p = {"key": key, "name": name, "who": (who or "").strip(), "branch": (branch or "").strip(), "steps": []}
        self.doc["pathways"].append(p)
        self._why(f"playbook:{key}", reason)
        out = {"playbook": key}
        if steps:
            out["steps"] = self._commit(p, self._steps_from(steps, []))
        return out

    def update_playbook(self, playbook: str, reason: str = "", **fields) -> dict:
        p = self.playbook(playbook)
        for k in ("name", "who", "branch"):
            if fields.get(k) is not None:
                p[k] = str(fields[k]).strip()
        self._why(f"playbook:{playbook}", reason)
        return {"playbook": playbook, "name": p["name"]}

    def remove_playbook(self, playbook: str, reason: str = "") -> dict:
        self.playbook(playbook)
        self.doc["pathways"] = [p for p in self.doc["pathways"] if p["key"] != playbook]
        freed = [l for l, k in self.doc["rec_types"].items() if k == playbook]
        for l in freed:
            del self.doc["rec_types"][l]
        self._why(f"playbook:{playbook}", reason)
        return {"removed": playbook, "unmapped": freed}

    def set_steps(self, playbook: str, steps: list, reason: str = "") -> dict:
        p = self.playbook(playbook)
        out = self._commit(p, self._steps_from(steps or [], p["steps"]))
        self._why(f"playbook:{playbook}", reason)
        return {"steps": out}

    def add_step(self, playbook: str, reason: str = "", after: str | None = None, **raw) -> dict:
        p = self.playbook(playbook)
        steps = [dict(s) for s in p["steps"]]
        ids = {s["id"] for s in steps}
        if after and after not in ids:
            raise ToolError(f"No step '{after}' in {playbook}.")
        s = self._fields(raw)
        s["id"] = _next_id(ids)
        at = next(i for i, x in enumerate(steps) if x["id"] == after) + 1 if after else len(steps)
        if "depends_on" not in s:
            prev = after or (steps[-1]["id"] if steps else None)
            s["depends_on"] = [prev] if prev else []
        steps.insert(at, s)
        self._commit(p, steps)
        self._why(f"step:{playbook}:{s['id']}", reason)
        return {"added": s["id"], "steps": [[x["id"], x["title"]] for x in p["steps"]]}

    def update_step(self, playbook: str, step_id: str, reason: str = "", **raw) -> dict:
        p = self.playbook(playbook)
        steps = [dict(s) for s in p["steps"]]
        s = next((x for x in steps if x["id"] == step_id), None)
        if not s:
            raise ToolError(f"No step '{step_id}' in {playbook}.")
        s.update(self._fields(raw))
        self._commit(p, steps)
        self._why(f"step:{playbook}:{step_id}", reason)
        return {"updated": step_id}

    def remove_step(self, playbook: str, step_id: str, reason: str = "") -> dict:
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
        self._why(f"step:{playbook}:{step_id}", reason)
        return {"removed": step_id}

    def map_recommendation_types(self, playbook: str, labels: list, reason: str = "") -> dict:
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
            self._why(f"rec:{l}", reason)
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


# ── Files ────────────────────────────────────────────────────────────────────

def read_file(url: str, name: str) -> str:
    """A file's text for the model, PDFs with [p. N] markers so steps can cite the page."""
    if (url or "").lower().split("?")[0].endswith(".pdf"):
        resp = httpx.get(url, timeout=30, follow_redirects=True, headers=web_source.UA)
        resp.raise_for_status()
        reader = pypdf.PdfReader(BytesIO(resp.content))
        text = "\n".join(f"[p. {i}]\n{page.extract_text() or ''}" for i, page in enumerate(reader.pages, start=1))
    else:
        text = web_source.fetch(url, max_text=FILE_CHARS)["text"]
    return text[:FILE_CHARS]


def _files_block(files: list[dict]) -> list[dict]:
    blocks = []
    for f in files or []:
        name = f.get("name") or f.get("filename") or "file"
        try:
            text = read_file(f["url"], name)
            blocks.append({"type": "text", "text": f'<file name="{name}">\n{text}\n</file>'})
        except Exception as e:  # noqa: BLE001 — the model is told, not left guessing
            blocks.append({"type": "text", "text": f'<file name="{name}">Could not be read: {e}</file>'})
    return blocks


# ── The chat turn ────────────────────────────────────────────────────────────

def _snapshot(doc: dict, selected: str | None, labels: list[str]) -> dict:
    keep = ("id", "title", "how", "actor", "owner", "secondary_owner", "time", "depends_on", "depends_mode",
            "condition", "filled_by", "source")
    pathways = []
    for p in doc.get("pathways") or []:
        pathways.append({
            "key": p["key"], "name": p.get("name"), "who": p.get("who"), "branch": p.get("branch"),
            "recommendation_types": sorted(l for l, k in (doc.get("rec_types") or {}).items() if k == p["key"]),
            "steps": [{"n": i, **{k: s[k] for k in keep if s.get(k) not in (None, "", [])}}
                      for i, s in enumerate(p.get("steps") or [], start=1)],
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
    detail = a.get("reason") or a.get("title") or a.get("name") or a.get("step_id") or ", ".join(a.get("labels") or []) or ""
    return {"id": block.get("id"), "label": TOOL_LABELS.get(block.get("name"), block.get("name")),
            "detail": str(detail)[:120], "error": error}


def _system(sources: dict, mode: str) -> str:
    guide = STEP_GUIDE.format(
        actors=ACTOR_TEXT,
        sources="\n".join(f"  - {k}: {v.get('label')}{'' if v.get('live') else ' (not built yet; the SME fills it in)'}"
                          for k, v in sources.items()))
    return SYSTEM.format(step_guide=guide, tag=STATE_TAG, mode=ANSWER_MODE if mode == "answer" else PROPOSE_MODE)


def run_turn(messages: list[dict], text: str, working: dict, selected: str | None, sources: dict,
             labels: list[str], mode: str = "propose", files: list[dict] | None = None) -> dict:
    """
    One planner message and everything the assistant does in reply.

    working is the draft with the suggestions still pending applied, so a
    follow-up builds on them. Returns the conversation to send next time, what
    to show in the chat, the target (the playbooks as the assistant would have
    them) and the reasons for what it touched.
    """
    work = Work(working, sources, labels)
    history = _without_thinking(messages or [])
    user = [{"type": "text", "text": text}, *_files_block(files or []),
            {"type": "text", "text": f"<{STATE_TAG}>\n{json.dumps(_snapshot(work.doc, selected, labels))}\n</{STATE_TAG}>"}]
    history.append({"role": "user", "content": user})
    shown: list[dict] = []

    client = _get_client()
    system = [{"type": "text", "text": _system(sources, mode)}]
    for _ in range(MAX_TOOL_ROUNDS):
        # Tools stay defined in "answer" mode (earlier turns used them); the model just can't call any.
        resp = _create(client, model=MODEL, max_tokens=16000, system=system, tools=TOOLS,
                       **({"tool_choice": {"type": "none"}} if mode == "answer" else {}), messages=history,
                       cache_control={"type": "ephemeral"}, output_config={"effort": "medium"})
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
        shown.append({"kind": "event", "level": "warn", "text": "The assistant stopped after many changes in one turn. Reply to continue."})

    # A turn cut short can leave tool calls unanswered, which the API rejects next time.
    last = history[-1]
    if last["role"] == "assistant":
        pending = [b for b in last["content"] if isinstance(b, dict) and b.get("type") == "tool_use"]
        if pending:
            history.append({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": b["id"], "is_error": True, "content": "Not run: the turn ended."}
                for b in pending]})

    return {"messages": _without_thinking(history), "display": shown, "target": work.doc, "reasons": work.reasons}


# ── Quick actions on one step ────────────────────────────────────────────────

ASSIST = {
    "rewrite": {
        "ask": "Rewrite this step's description (`how`) so a planner new to Bowie could do it: name the data source or "
               "office, and what is handed to whom at the end. One or two plain sentences, no longer than needed.",
        "schema": {"how": {"type": "string"}, "note": {"type": "string", "description": "What the rewrite adds, under 10 words."}},
    },
    "estimate": {
        "ask": "Estimate this step's potential duration in the app's format (\"1–2 wk\", \"1–3 mo\"). Base it on similar "
               "steps in the other playbooks where there are any, and say which in the note.",
        "schema": {"time": {"type": "string"}, "note": {"type": "string", "description": "Why, under 15 words."}},
    },
    "owners": {
        "ask": "Suggest this step's primary owner (actor kind and who exactly) and a secondary owner who reviews or "
               "answers for it alongside them.",
        "schema": {"actor": {"type": "string", "enum": list(ACTORS)}, "owner": {"type": "string"},
                   "secondary_owner": {"type": "string"}, "note": {"type": "string", "description": "Why, under 15 words."}},
    },
}
VARIANTS = {"shorter": "Make it noticeably shorter than the current suggestion.",
            "plainer": "Use plainer words than the current suggestion: no jargon or acronyms without saying what they are.",
            "again": "Give a different suggestion from the current one."}


def assist(action: str, doc: dict, playbook: str, step_id: str, variant: str | None = None,
           previous: str | None = None) -> dict:
    """One quick suggestion for one field group of one step, on the faster model."""
    spec = ASSIST.get(action)
    if not spec:
        raise ValueError(f"Unknown action {action}")
    p = next((x for x in doc.get("pathways") or [] if x["key"] == playbook), None)
    step = next((s for s in (p or {}).get("steps") or [] if s.get("id") == step_id), None)
    if not step:
        raise ValueError("That step is not in the playbook.")
    others = [{"playbook": x.get("name"), "steps": [{k: s.get(k) for k in ("title", "actor", "owner", "time")}
                                                   for s in x.get("steps") or []]}
              for x in doc.get("pathways") or [] if x["key"] != playbook]
    prompt = (
        "You help a city planner in Bowie, Maryland (Prince George's County) write playbooks: the standard steps to get "
        f"a kind of street change done. Owner kinds: {ACTOR_TEXT}.\n\n"
        f"Playbook: {p.get('name')} — {p.get('branch') or ''}\n"
        f"Its steps: {json.dumps([{k: s.get(k) for k in ('id', 'title', 'how', 'owner', 'time')} for s in p['steps']])}\n\n"
        f"The step: {json.dumps(step)}\n\n"
        f"Other playbooks, for comparison: {json.dumps(others)}\n\n"
        f"{spec['ask']} Write \"(confirm)\" after anything you are unsure of."
        + (f"\n\nThe current suggestion is: {previous}\n{VARIANTS.get(variant, '')}" if variant and previous else "")
    )
    schema = {"type": "object", "properties": spec["schema"], "required": list(spec["schema"]), "additionalProperties": False}
    return structured_call(prompt, schema, max_tokens=2000, model=FAST_MODEL, effort="low")


# ── Do a playbook's recommendation types belong in it? ───────────────────────

def check_fit(doc: dict, playbook: str, labels: list[str]) -> dict:
    """
    For each recommendation type routed to one playbook: does its steps' work
    fit it, or would another playbook serve it better? Also which unmapped
    types belong here. One call on the faster model; the browser turns every
    move into a suggestion to accept or reject.
    """
    rec_types = doc.get("rec_types") or {}
    pathways = doc.get("pathways") or []
    p = next((x for x in pathways if x["key"] == playbook), None)
    if not p:
        raise ValueError("That playbook is not in the draft.")
    mine = sorted(l for l, k in rec_types.items() if k == playbook)
    unmapped = sorted(l for l in labels if l not in rec_types)
    if not mine and not unmapped:
        return {"types": [], "add": []}

    def outline(x: dict) -> dict:
        return {"key": x["key"], "name": x.get("name"), "who": x.get("who"), "deciding_question": x.get("branch"),
                "steps": [{k: s.get(k) for k in ("title", "how", "owner") if s.get(k)} for s in x.get("steps") or []]}

    others = [{**outline(x), "recommendation_types": sorted(l for l, k in rec_types.items() if k == x["key"])}
              for x in pathways if x["key"] != playbook]
    keys = [x["key"] for x in pathways if x["key"] != playbook]
    prompt = (
        "You help a city planner in Bowie, Maryland (Prince George's County) route State of Place block "
        "recommendation types to playbooks: the standard steps a city follows to get one kind of change done. Every "
        "block's plan for a recommendation starts from its playbook, so a type belongs where the steps actually get "
        "that change done (the right permits, owners and approvals).\n\n"
        f"The playbook: {json.dumps(outline(p))}\n"
        f"Recommendation types routed to it: {json.dumps(mine)}\n\n"
        f"The other playbooks: {json.dumps(others)}\n\n"
        f"Recommendation types no playbook covers yet: {json.dumps(unmapped)}\n\n"
        "1. For every type routed to this playbook, say whether it fits. Mark it as not fitting only when another "
        "playbook clearly serves it better; then set move_to to that playbook's key. A type that fits gets "
        "move_to \"\".\n"
        "2. List any uncovered types whose work this playbook's steps would get done (often none).\n"
        "Each reason is one plain sentence under 15 words naming the step or permit that decides it, "
        "e.g. \"No step here covers sign permits.\""
    )
    schema = {
        "type": "object", "additionalProperties": False, "required": ["types", "add"],
        "properties": {
            "types": {"type": "array", "items": {
                "type": "object", "additionalProperties": False, "required": ["label", "fits", "move_to", "reason"],
                "properties": {"label": {"type": "string", "enum": mine or [""]}, "fits": {"type": "boolean"},
                               "move_to": {"type": "string", "enum": ["", *keys]}, "reason": {"type": "string"}}}},
            "add": {"type": "array", "items": {
                "type": "object", "additionalProperties": False, "required": ["label", "reason"],
                "properties": {"label": {"type": "string", "enum": unmapped or [""]}, "reason": {"type": "string"}}}},
        },
    }
    out = structured_call(prompt, schema, max_tokens=4000, model=FAST_MODEL, effort="low")
    types = [t for t in out.get("types") or [] if t["label"] in mine]
    for t in types:
        if t["fits"] or t["move_to"] not in keys:
            t["fits"], t["move_to"] = True, ""
    add = [a for a in out.get("add") or [] if a["label"] in unmapped]
    return {"types": types, "add": add}


# ── Drafting a whole playbook ────────────────────────────────────────────────

DRAFT_PROMPT = """You are drafting a new playbook for the City of Bowie, Maryland (Prince George's County): the
standard sequence of steps a city follows to get one kind of change done. A planner and an SME will review every
step you write.

What this playbook is for: {purpose}

{step_guide}

{model_on}{files_note}
Recommendation types it should cover (chosen by the planner): {chosen}
Recommendation types that exist (the only ones you may suggest): {labels}

Write the playbook as JSON Lines, one JSON object per line, nothing else (no prose, no code fences):
- Line 1: {{"name": ..., "who": ..., "branch": ..., "estimated_steps": <number>, "suggested_rec_types": [up to 3 more
  exact labels from the list that fit, or []]}}
- Then one line per step, in order: {{"id": "s1", "title": ..., "how": ..., "actor": ..., "owner": ...,
  "secondary_owner": ..., "time": ..., "depends_on": [...], "condition": ..., "filled_by": ..., "source": ...}}
Use ids s1, s2 ... in order. Give branches a condition. Typically 6 to 12 steps. Cite a source on every step."""


def draft_playbook(purpose: str, files: list[dict], chosen: list[str], model_on: dict | None, doc: dict,
                   sources: dict, labels: list[str], on_progress: Callable[[dict], None],
                   stopped: Callable[[], bool]) -> dict:
    """
    A new playbook, streamed: on_progress gets {meta, steps} each time a step is
    complete, so the planner watches the outline grow. Returns the same, plus the
    playbook as it would be added (key, steps checked for loops and bad links).
    """
    guide = STEP_GUIDE.format(
        actors=ACTOR_TEXT,
        sources="\n".join(f"  - {k}: {v.get('label')}" for k, v in sources.items()))
    model_note = ""
    if model_on:
        model_note = (f"Model it on the \"{model_on.get('name')}\" playbook: reuse its owners, step style and typical "
                      f"durations where they fit, and cite it (\"{model_on.get('name')} playbook, step N\").\n"
                      f"{json.dumps(model_on.get('steps') or [])}\n\n")
    content = [{"type": "text", "text": DRAFT_PROMPT.format(
        purpose=purpose, step_guide=guide, model_on=model_note,
        files_note="Source files are attached below; cite them by name and page.\n" if files else "",
        chosen=", ".join(chosen) or "none yet", labels=", ".join(sorted(labels)))}, *_files_block(files)]

    meta: dict = {}
    steps: list[dict] = []
    buf = ""

    def take(line: str) -> None:
        line = line.strip().strip("`")
        if not line.startswith("{"):
            return
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            return
        if not meta and "name" in obj and "title" not in obj:
            meta.update(obj)
        elif "title" in obj:
            steps.append(obj)
        on_progress({"meta": dict(meta), "steps": list(steps)})

    client = _get_client()
    with client.messages.stream(model=MODEL, max_tokens=16000, messages=[{"role": "user", "content": content}],
                                output_config={"effort": "medium"}) as stream:
        for chunk in stream.text_stream:
            buf += chunk
            while "\n" in buf:
                line, buf = buf.split("\n", 1)
                take(line)
            if stopped():
                break
    take(buf)

    work = Work(doc, sources, labels)
    name = meta.get("name") or "New playbook"
    try:
        out = work.add_playbook(name=name, who=meta.get("who") or "", branch=meta.get("branch") or "",
                                steps=steps, reason="Drafted by the assistant")
    except ToolError:
        # A stopped draft can end mid-graph: keep the steps, drop links to steps never written.
        ids = {s.get("id") for s in steps}
        for s in steps:
            s["depends_on"] = [d for d in s.get("depends_on") or [] if d in ids]
        work = Work(doc, sources, labels)
        out = work.add_playbook(name=name, who=meta.get("who") or "", branch=meta.get("branch") or "",
                                steps=steps, reason="Drafted by the assistant")
    key = out["playbook"]
    playbook = next(p for p in work.doc["pathways"] if p["key"] == key)
    suggested = [l for l in meta.get("suggested_rec_types") or [] if l in set(labels) and l not in chosen][:3]
    return {"meta": meta, "steps": steps, "playbook": playbook, "suggested_rec_types": suggested}
