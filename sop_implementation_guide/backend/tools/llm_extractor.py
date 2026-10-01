"""LLM tool for judging how well funding programs fit a block recommendation."""

import json
import logging

from anthropic import (
    Anthropic,
    APIConnectionError,
    AuthenticationError,
    APIStatusError,
    DefaultHttpxClient,
)
from backend.config import settings

log = logging.getLogger(__name__)

MODEL = "claude-haiku-4-5-20251001"

_client = None


def _get_client() -> Anthropic:
    global _client
    if _client is None:
        if not settings.anthropic_api_key:
            raise ValueError("ANTHROPIC_API_KEY is not set in .env")
        # On corporate networks with SSL inspection, set ANTHROPIC_SSL_VERIFY=false in .env.
        # DefaultHttpxClient preserves the SDK's own timeouts and connection limits.
        http_client = DefaultHttpxClient(verify=settings.anthropic_ssl_verify)
        _client = Anthropic(api_key=settings.anthropic_api_key, http_client=http_client)
        if not settings.anthropic_ssl_verify:
            log.warning("SSL verification disabled — corporate network mode")
    return _client


# Structured-output schema. This is what stops the model wrapping its answer in
# ```json fences (which broke json.loads at char 0) and what guarantees fit_score
# is a real number rather than null.
#
# The model returns ONLY its judgement, keyed by program_name. Award amounts and
# application URLs are merged back in locally — asking the model to echo facts we
# already hold wastes tokens and invites transcription errors.
FIT_SCHEMA = {
    "type": "object",
    "properties": {
        "fits": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "program_name": {
                        "type": "string",
                        "description": "Must exactly match a program_name from the input list.",
                    },
                    "fit_score": {
                        "type": "number",
                        "description": (
                            "0.0-1.0. How well this program could fund THIS ONE "
                            "recommendation. Never null — give a real estimate even "
                            "when the program's notes are thin."
                        ),
                    },
                    "narrative": {
                        "type": "string",
                        "description": (
                            "1-2 sentences naming the concrete connection between this "
                            "program and this recommendation, or the concrete reason "
                            "there isn't one. The reviewer reads this to decide."
                        ),
                    },
                    "recommended_action": {
                        "type": "string",
                        "description": 'e.g. "Apply", "Investigate further", "Skip — wrong region".',
                    },
                },
                "required": ["program_name", "fit_score", "narrative", "recommended_action"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["fits"],
    "additionalProperties": False,
}


def structured_call(prompt: str, schema: dict, max_tokens: int = 4096) -> dict:
    """
    Ask the model for JSON matching `schema`, and return it parsed.

    Shared by the discovery agent so each of its stages stays a few lines. Uses
    structured outputs, so markdown fences and shape drift are impossible — an
    earlier version of this pipeline lost days to exactly those two failures.

    Raises ValueError when the model declines, truncates, or returns something
    unparseable. Callers decide what to do; there is no silent default, because a
    silent default once made a broken integration look like working output.
    """
    client = _get_client()
    response = client.messages.create(
        model=MODEL,
        max_tokens=max_tokens,
        extra_body={"temperature": 0},
        output_config={"format": {"type": "json_schema", "schema": schema}},
        messages=[{"role": "user", "content": prompt}],
    )

    if response.stop_reason == "refusal":
        raise ValueError("The model declined this request.")
    if response.stop_reason == "max_tokens":
        raise ValueError(
            "Response hit max_tokens and is incomplete — try fewer sources at once."
        )

    try:
        text = next(b.text for b in response.content if b.type == "text")
        return json.loads(text)
    except (StopIteration, json.JSONDecodeError) as e:
        raise ValueError(f"Could not parse model response: {type(e).__name__}") from e


def _clamp_score(value) -> float:
    """Coerce a model-supplied score into 0.0-1.0.

    The DB has a CHECK (relevance_score BETWEEN 0 AND 1), so an out-of-range
    value would fail the insert rather than the request.
    """
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.5


def _fallback(programs: list[dict], recommendation: dict, reason: str) -> list[dict]:
    """Assessment-failed result for one recommendation.

    Flagged with assessment_failed so a flat wall of 0.5 scores is visible in the
    review payload instead of looking like real model output.
    """
    log.error(
        "Fit assessment failed for rec %s (%s) — returning defaults",
        recommendation.get("rec_label"), reason,
    )
    return [
        {
            **p,
            "rec_id": recommendation.get("id"),
            "rec_label": recommendation.get("rec_label"),
            "rec_dimension": recommendation.get("dimension"),
            "rec_direction": recommendation.get("direction"),
            "fit_score": 0.5,
            "narrative": f"Automated assessment unavailable ({reason}). Manual review required.",
            "recommended_action": "Investigate further",
            "assessment_failed": True,
        }
        for p in programs
    ]


def _program_digest(programs: list[dict]) -> list[dict]:
    """Only the fields the model needs to judge fit — not award amounts or URLs."""
    return [
        {
            "program_name": p.get("program_name"),
            "program_type": p.get("program_type"),
            "source_agency": p.get("source_agency"),
            "application_deadline": str(p.get("deadline")) if p.get("deadline") else None,
            "geo_scope": p.get("geo_scope"),
            "eligibility_notes": (p.get("eligibility_notes") or "")[:700],
        }
        for p in programs
    ]


def assess_recommendation_fit(
    block_context: dict,
    recommendation: dict,
    programs: list[dict],
) -> list[dict]:
    """
    Ask Claude how well each funding program could pay for ONE recommendation.

    block_context: dict with tract_geoid, median_hh_income, sop_index_norm,
                   transit_share
    recommendation: a block_recommendations row (id, rec_label, dimension,
                   direction, predicted_score_increase)
    programs: rows from the funding_programs table

    The recommendation is the unit of judgement, not the block. Scoring a program
    against a whole block's worth of improvements produced narratives too vague to
    act on — "supports community development" rather than "funds tree planting,
    which is what this recommendation is".

    Returns one row per program, highest fit first, each carrying the program's own
    fields plus rec_id, rec_label, fit_score, narrative and recommended_action.
    Every program gets a row: a low score is a judgement the reviewer can see and
    override, whereas an omission is indistinguishable from the model forgetting.

    On failure, returns every program at fit_score 0.5 with assessment_failed=True.
    """
    client = _get_client()

    income = block_context.get("median_hh_income")
    income_str = f"${income:,.0f}" if income is not None else "unknown"
    gain = recommendation.get("predicted_score_increase")

    # Direction is load-bearing, not decoration. Roughly 14% of recommendations are
    # 'Decrease' — "Surface parking lot / Decrease" means REMOVE the parking. Without
    # this line the model went looking for grants to build one.
    direction = recommendation.get("direction")
    if direction == "Decrease":
        intent = f"REMOVE or REDUCE: {recommendation.get('rec_label')}"
        intent_note = (
            "This block has too much of this feature. The work to fund is removal, "
            "reduction or conversion to something better — not building more of it."
        )
    else:
        intent = f"ADD or IMPROVE: {recommendation.get('rec_label')}"
        intent_note = "The work to fund is adding this feature, or improving what is there."

    prompt = f"""You are a grant analyst helping a municipality fund one specific
improvement on a city block in Prince George's County, Maryland.

BLOCK CONTEXT:
- Census Tract: {block_context.get('tract_geoid')}
- Median household income: {income_str}
- State of Place walkability score (normalized): {block_context.get('sop_index_norm', 'unknown')}/100
- Transit commuter share: {block_context.get('transit_share', 'unknown')}%

THE IMPROVEMENT TO FUND:
- {intent}
- {intent_note}
- Urban design dimension: {recommendation.get('dimension') or 'unspecified'}
- Predicted walkability gain if done: {f'+{gain}' if gain is not None else 'not estimated'}

FUNDING OPPORTUNITIES:
{json.dumps(_program_digest(programs), indent=2)}

Score how well each opportunity could fund THIS improvement — not the block in
general, and not the block's other needs.

Rules:
- Judge what the money can actually be spent on. A programme that funds exactly
  this kind of improvement scores high even if the block is unremarkable.
- Respect the direction above. A programme that funds building more of a feature
  the block needs less of is a poor fit, not a good one.
- Penalise geographic mismatch. A programme restricted to another region (for
  example a Great Lakes initiative) cannot fund work in Maryland however well the
  topic fits. Treat geo_scope as a hint, not as proof — reason from the notes too.
- The applicant is a city or county government, not an individual or a nonprofit.
- Where the notes are too thin to tell, say so in the narrative and score in the
  middle. Do not invent eligibility criteria that are not stated.
- In each narrative, name the specific connection or the specific mismatch. The
  reviewer will adjust your score, so give them the reason, not a summary.

Return one entry per opportunity, with program_name matching the input exactly."""

    try:
        response = client.messages.create(
            model=MODEL,
            # ~50 programmes × a two-sentence narrative each. Too low and the run
            # dies at the last few programmes rather than the first.
            max_tokens=8192,
            # Scoring, not writing. Without this the same block scored 70 then 95
            # across runs, because relevance values straddled a scoring tier
            # boundary — bad for a live demo and worse for evaluation.
            #
            # Sent via extra_body because anthropic 1.9.0 dropped `temperature` from
            # the create() signature; the API still honours it on Haiku 4.5. If this
            # model is ever swapped for Opus 4.7+ or Sonnet 5, remove it — those
            # reject sampling parameters with a 400.
            extra_body={"temperature": 0},
            output_config={"format": {"type": "json_schema", "schema": FIT_SCHEMA}},
            messages=[{"role": "user", "content": prompt}],
        )
    except AuthenticationError as e:
        raise ValueError(f"Anthropic API key is invalid or expired: {e}") from e
    except APIConnectionError as e:
        raise ConnectionError(f"Cannot reach Anthropic API — check network: {e}") from e
    except APIStatusError as e:
        log.error("Anthropic API error %s: %s", e.status_code, e.message)
        raise

    if response.stop_reason == "refusal":
        return _fallback(programs, recommendation, "model declined the request")
    if response.stop_reason == "max_tokens":
        return _fallback(programs, recommendation, "response truncated at max_tokens")

    try:
        text = next(b.text for b in response.content if b.type == "text")
        by_name = {f["program_name"]: f for f in json.loads(text)["fits"]}
    except (json.JSONDecodeError, KeyError, TypeError, StopIteration) as e:
        return _fallback(programs, recommendation, f"unparseable response: {type(e).__name__}")

    fits = []
    unmatched = []
    for p in programs:
        f = by_name.get(p.get("program_name"))
        # rec_dimension is spelled out rather than reusing the programme row's own
        # keys — `dimension` would collide with a funding_programs field of the
        # same name if one is ever added, and silently win or lose.
        base = {
            **p,
            "rec_id": recommendation.get("id"),
            "rec_label": recommendation.get("rec_label"),
            "rec_dimension": recommendation.get("dimension"),
            "rec_direction": recommendation.get("direction"),
        }
        if f is None:
            # Model dropped or renamed this programme — keep it, flagged, rather
            # than silently shrinking the candidate list.
            unmatched.append(p.get("program_name"))
            fits.append({
                **base,
                "fit_score": 0.5,
                "narrative": "Not assessed by model. Manual review required.",
                "recommended_action": "Investigate further",
                "assessment_failed": True,
            })
            continue
        fits.append({
            **base,
            "fit_score": _clamp_score(f.get("fit_score")),
            "narrative": f.get("narrative"),
            "recommended_action": f.get("recommended_action"),
        })

    if unmatched:
        log.warning(
            "Model returned no fit for rec %s: %s",
            recommendation.get("rec_label"), ", ".join(map(str, unmatched)),
        )

    # Strongest fit first — the reviewer reads top-down and the weak tail sinks.
    fits.sort(key=lambda f: f["fit_score"], reverse=True)
    return fits

