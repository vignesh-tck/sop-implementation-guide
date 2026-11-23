"""
An implementation plan as a graph of steps: who does each one, what it waits
on, and roughly how long the whole thing takes.

A step carries:
  id            stable within its plan ("s1", "s2" ...), so moving or removing a
                step never breaks another step's reference to it
  title         what has to happen
  how           how it is done, in a sentence or two
  actor         the primary owner, as a kind: agent (this system: the zoning
                lookup or the funding agent), city, builder (property owner,
                developer or contractor), other (state, county, boards, anyone else)
  owner         the primary owner exactly, in words ("Code enforcement", "MD SHA")
  secondary_owner  who else answers for it, e.g. the SME who reviews an agent's output
  time          how long, in words ("1–3 mo", "Program deadline")
  depends_on    ids of the steps it waits on
  depends_mode  'all' (waits for every one) or 'any' (the first one done)
  condition     set when the step only happens in some cases ("Code violation");
                drawn as a labelled branch

Steps saved before these fields existed have a free-text `waits_on` instead
("After 2 and 4", "If voluntary"). upgrade_steps reads it once, the same way
every time, so old rows still draw.
"""

import re

ACTORS = ("agent", "city", "builder", "other")

# Earliest keyword in the owner text wins, so "Road owner" (other) beats the
# "owner" inside it, and "City or owner" reads as the City.
_ACTOR_WORDS = {
    "other": ("road owner", "md sha", "sha", "county", "m-ncppc", "board", "examiner",
              "permitting", "dnr", "agency", "state", "utility"),
    "builder": ("owner", "developer", "contractor", "builder"),
    "city": ("city", "planning staff", "staff", "code enforcement", "economic development",
             "public works", "council", "procurement", "finance"),
}

_LIVE_AGENT_SOURCES = ("zoning", "funding")

# How a step's diff is described, field by field.
DIFF_FIELDS = (
    ("title", "Activity"), ("how", "Description"), ("after", "Depends on"), ("condition", "Only if"),
    ("actor", "Primary owner"), ("owner", "Primary owner, exactly"), ("secondary_owner", "Secondary owner"),
    ("time", "Duration"),
)

ACTOR_LABEL = {"agent": "Agent", "city": "City", "builder": "Builder", "other": "Other"}


def infer_actor(step: dict) -> str:
    """A best guess at who does a step, for steps saved without an actor."""
    title = (step.get("title") or "").strip().lower()
    if step.get("filled_by") == "zoning":
        return "agent"
    if step.get("filled_by") == "funding" and title.startswith("find"):
        return "agent"
    owner = (step.get("owner") or "").lower()
    best, best_at = "city", None
    for actor, words in _ACTOR_WORDS.items():
        for w in words:
            at = owner.find(w)
            if at >= 0 and (best_at is None or at < best_at):
                best, best_at = actor, at
    return best


def _legacy_depends(text: str, i: int, steps: list[dict]) -> tuple[list[int], str, str | None]:
    """
    Step numbers (1-based), mode and condition read from an old waits_on text.

    "After 2 and 4" -> [2, 4] all; "After 3 or 5" -> [3, 5] any; "After 4–6" ->
    [4, 5, 6]; "Alongside 2–4" -> whatever step 2 waits on; "If voluntary" ->
    the step before, as a branch labelled "Voluntary". Anything else waits on
    the step before.
    """
    text = (text or "").strip()
    prev = [i] if i > 0 else []
    if not text:
        return prev, "all", None
    low = text.lower()
    if low.startswith("if "):
        cond = text[3:].strip()
        return prev, "all", cond[:1].upper() + cond[1:]
    nums: list[int] = []
    for a, b in re.findall(r"(\d+)\s*(?:[–-]\s*(\d+))?", text):
        a = int(a)
        nums += list(range(a, int(b) + 1)) if b else [a]
    if low.startswith("alongside") and nums:
        first = nums[0] - 1
        if 0 <= first < len(steps):
            return _legacy_depends(steps[first].get("waits_on") or "", first, steps)[0], "all", None
    if low.startswith("after") and nums:
        return nums, ("any" if " or " in low else "all"), None
    return prev, "all", None


def upgrade_steps(steps: list[dict]) -> list[dict]:
    """
    Give every step an id, an actor and structured dependencies.

    Steps that already have them are only tidied: references to ids that no
    longer exist, or to the step itself, are dropped.
    """
    out = [dict(s) for s in steps or []]
    used = {s["id"] for s in out if isinstance(s.get("id"), str) and s["id"].strip()}
    k = 0
    for s in out:
        if not (isinstance(s.get("id"), str) and s["id"].strip()):
            k += 1
            while f"s{k}" in used:
                k += 1
            s["id"] = f"s{k}"
            used.add(s["id"])
    id_at = [s["id"] for s in out]
    known = set(id_at)
    for i, s in enumerate(out):
        if "depends_on" not in s:
            nums, mode, cond = _legacy_depends(s.get("waits_on") or "", i, out)
            s["depends_on"] = [id_at[n - 1] for n in nums if 1 <= n <= len(out) and n - 1 != i]
            s["depends_mode"] = mode
            if cond and not s.get("condition"):
                s["condition"] = cond
        deps = []
        for d in s.get("depends_on") or []:
            if d in known and d != s["id"] and d not in deps:
                deps.append(d)
        s["depends_on"] = deps
        if s.get("depends_mode") not in ("all", "any"):
            s["depends_mode"] = "all"
        if s.get("actor") not in ACTORS:
            s["actor"] = infer_actor(s)
    # Only once every step is read: "Alongside 2–4" reads step 2's text.
    for s in out:
        s.pop("waits_on", None)
    return out


def find_cycle(steps: list[dict]) -> list[str] | None:
    """Titles of steps that wait on each other in a loop, or None."""
    deps = {s["id"]: list(s.get("depends_on") or []) for s in steps}
    left = {i: len(d) for i, d in deps.items()}
    users: dict[str, list[str]] = {}
    for i, d in deps.items():
        for x in d:
            users.setdefault(x, []).append(i)
    ready = [i for i, n in left.items() if n == 0]
    while ready:
        i = ready.pop()
        for u in users.get(i, []):
            left[u] -= 1
            if left[u] == 0:
                ready.append(u)
    stuck = [i for i, n in left.items() if n > 0]
    if not stuck:
        return None
    titles = {s["id"]: s.get("title") or s["id"] for s in steps}
    return [titles[i] for i in stuck]


# ── Time ─────────────────────────────────────────────────────────────────────

_UNIT_WEEKS = {"day": 1 / 7, "wk": 1, "week": 1, "mo": 4.345, "month": 4.345, "yr": 52, "year": 52}
_RANGE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:[–-]\s*(\d+(?:\.\d+)?))?\s*(day|wk|week|mo|month|yr|year)s?\b", re.I)


def parse_time(text: str | None) -> tuple[float, float] | None:
    """
    A step's time as (fewest, most) weeks, or None when it has no fixed length
    ("Program deadline", "Varies", "At handover").

    Several ranges in one text ("Special exception 4–9 mo; rezoning 9–18 mo")
    span from the smallest to the largest. "Up to 12 mo" starts at zero.
    """
    text = (text or "").strip()
    if not text:
        return None
    lo = hi = None
    for a, b, unit in _RANGE.findall(text):
        w = _UNIT_WEEKS[unit.lower()]
        a_w, b_w = float(a) * w, float(b or a) * w
        lo = a_w if lo is None else min(lo, a_w)
        hi = b_w if hi is None else max(hi, b_w)
    low = text.lower()
    for word, rng in (("days", (0.2, 1.0)), ("weeks", (1.0, 4.0))):
        if re.search(rf"\b{word}\b", low):
            lo = rng[0] if lo is None else min(lo, rng[0])
            hi = rng[1] if hi is None else max(hi, rng[1])
    if lo is None:
        return None
    if "up to" in low:
        lo = 0.0
    return round(lo, 2), round(hi, 2)


def estimate(steps: list[dict]) -> dict:
    """
    A rough range for the whole plan, in weeks, along its longest chain.

    fastest: shortest times, and steps that only happen in some cases are
    skipped. slowest: longest times with every step. A step waiting on 'any'
    starts when the first of them is done. Steps with no fixed time count as
    zero and are listed in `untimed`, so the range is never presented as more
    certain than it is.
    """
    by_id = {s["id"]: s for s in steps}
    num = {s["id"]: i for i, s in enumerate(steps, start=1)}
    times = {s["id"]: parse_time(s.get("time")) for s in steps}
    if find_cycle(steps):
        return {"fastest_weeks": None, "slowest_weeks": None, "untimed": []}

    def finish(which: str) -> float:
        done: dict[str, float] = {}

        def end(i: str) -> float:
            if i in done:
                return done[i]
            s = by_id[i]
            deps = s.get("depends_on") or []
            ends = [end(d) for d in deps]
            start = (min(ends) if s.get("depends_mode") == "any" else max(ends)) if ends else 0.0
            t = times[i]
            if which == "fastest":
                dur = 0.0 if (s.get("condition") or not t) else t[0]
            else:
                dur = t[1] if t else 0.0
            done[i] = start + dur
            return done[i]

        return max((end(i) for i in by_id), default=0.0)

    return {
        "fastest_weeks": round(finish("fastest"), 1),
        "slowest_weeks": round(finish("slowest"), 1),
        "untimed": [num[i] for i, t in times.items() if t is None],
    }


# ── Diff ─────────────────────────────────────────────────────────────────────

def after_text(step: dict, num: dict[str, int]) -> str:
    deps = sorted(num[d] for d in step.get("depends_on") or [] if d in num)
    if not deps:
        return "Start"
    joiner = " or " if step.get("depends_mode") == "any" else ", "
    return "After " + joiner.join(str(n) for n in deps)


def _links(step: dict) -> tuple:
    deps = sorted(step.get("depends_on") or [])
    return tuple(deps), (step.get("depends_mode") == "any" and len(deps) > 1)


def _field(step: dict, key: str, num: dict[str, int]) -> str:
    if key == "after":
        return after_text(step, num)
    if key == "actor":
        return ACTOR_LABEL.get(step.get("actor"), step.get("actor") or "")
    return (step.get(key) or "").strip()


def diff_steps(base: list[dict], new: list[dict]) -> dict:
    """
    What changed from one version of a plan's steps to the next, by step id.

    removed entries keep the whole step, so a removed step can still be drawn
    (faded) where it used to be.
    """
    base_num = {s["id"]: i for i, s in enumerate(base, start=1)}
    new_num = {s["id"]: i for i, s in enumerate(new, start=1)}
    base_by = {s["id"]: s for s in base}
    added, changed = [], []
    for s in new:
        old = base_by.get(s["id"])
        if old is None:
            added.append({"id": s["id"], "n": new_num[s["id"]], "title": s.get("title") or "", "actor": s.get("actor")})
            continue
        fields = []
        for key, label in DIFF_FIELDS:
            before, after = _field(old, key, base_num), _field(s, key, new_num)
            # Links compare by id: a step inserted earlier renumbers the rest
            # without changing what any of them waits on.
            same = (_links(old) == _links(s)) if key == "after" else before == after
            if not same:
                fields.append({"field": key, "label": label, "before": before, "after": after})
        if fields:
            changed.append({"id": s["id"], "n": new_num[s["id"]], "title": s.get("title") or "", "fields": fields})
    removed = [{**s, "n": base_num[s["id"]]} for s in base if s["id"] not in new_num]
    return {
        "added": added, "changed": changed, "removed": removed,
        "count": len(added) + len(changed) + len(removed),
    }
