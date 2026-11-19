"""
The pipeline's step graph.

Each step names its inputs: the steps whose records it receives. A step with
several inputs merges them (their records, one after another); an Extract
step's inputs are records that come before the ones it reads from its
sources. The steps listed in output_from feed the final output. A step's
output goes to one place, so the graph is a set of branches that merge on
the way to the output: there is no splitting.

Definitions saved before steps had inputs are read as the chain they were:
each step takes the one before it, and the last feeds the output. A graph
that is still such a chain is stored and hashed exactly as before.
"""

from __future__ import annotations


class GraphError(ValueError):
    """The steps do not form a graph that can run (a loop, an unknown input)."""


def normalise(definition: dict) -> dict:
    """{"steps": [step + "inputs"], "output_from": [...]} for any definition, old or new. Does not mutate."""
    steps = definition.get("steps") or []
    # A pipeline with several outputs is always stored wired (see outputs.py).
    explicit = "output_from" in definition or "outputs" in definition or any("inputs" in s for s in steps)
    out = []
    for i, s in enumerate(steps):
        ins = list(s.get("inputs") or []) if explicit else ([steps[i - 1]["id"]] if i else [])
        out.append({**s, "inputs": ins})
    output_from = list(definition.get("output_from") or []) if explicit else ([steps[-1]["id"]] if steps else [])
    return {"steps": out, "output_from": output_from}


def is_chain(g: dict) -> bool:
    """True when the graph is the plain chain an old definition would mean (each step after the previous)."""
    steps = g["steps"]
    if any(s["inputs"] != ([steps[i - 1]["id"]] if i else []) for i, s in enumerate(steps)):
        return False
    return g["output_from"] == ([steps[-1]["id"]] if steps else [])


def consumers(g: dict) -> dict[str, list[str]]:
    """Where each step's output goes: step ids, and "output"."""
    used: dict[str, list[str]] = {s["id"]: [] for s in g["steps"]}
    for s in g["steps"]:
        for i in s["inputs"]:
            if i in used:
                used[i].append(s["id"])
    for i in g["output_from"]:
        if i in used:
            used[i].append("output")
    return used


def order(g: dict) -> list[str]:
    """The steps in an order they can run in (inputs first), keeping the listed order where free. Raises GraphError."""
    ids = [s["id"] for s in g["steps"]]
    known = set(ids)
    for s in g["steps"]:
        bad = [i for i in s["inputs"] if i not in known]
        if bad:
            raise GraphError(f"{s['id']} takes input from {', '.join(bad)}, which {'is' if len(bad) == 1 else 'are'} not a step.")
    waiting = {s["id"]: set(s["inputs"]) for s in g["steps"]}
    done: list[str] = []
    while len(done) < len(ids):
        ready = next((i for i in ids if i not in done and not (waiting[i] - set(done))), None)
        if ready is None:
            loop = [i for i in ids if i not in done]
            raise GraphError(f"The wires between {', '.join(loop)} form a loop: a step cannot take its own output.")
        done.append(ready)
    return done


def ancestors(g: dict, step_id: str) -> set[str]:
    """Every step whose records reach step_id."""
    inputs = {s["id"]: s["inputs"] for s in g["steps"]}
    seen: set[str] = set()
    todo = list(inputs.get(step_id, []))
    while todo:
        i = todo.pop()
        if i not in seen and i in inputs:
            seen.add(i)
            todo.extend(inputs[i])
    return seen


def stored(g: dict) -> dict:
    """What to save on the definition: inputs and output_from only when the graph is not a plain chain."""
    steps = [{k: v for k, v in s.items() if k != "inputs"} for s in g["steps"]]
    if is_chain(g):
        return {"steps": steps}
    return {"steps": [{**s, "inputs": list(x["inputs"])} for s, x in zip(steps, g["steps"])],
            "output_from": list(g["output_from"])}
