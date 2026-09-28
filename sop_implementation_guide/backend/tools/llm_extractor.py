"""LLM tool for extracting structured funding eligibility from raw program data."""

import json
import logging
from typing import Optional

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
# ```json fences (which broke json.loads at char 0) and what guarantees
# relevance_score is a real number rather than null.
#
# The model returns ONLY its assessment, keyed by program_name. Award amounts and
# application URLs are merged back in locally — asking the model to echo facts we
# already hold wastes tokens and invites transcription errors.
ASSESSMENT_SCHEMA = {
    "type": "object",
    "properties": {
        "assessments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "program_name": {
                        "type": "string",
                        "description": "Must exactly match a program_name from the input list.",
                    },
                    "relevance_score": {
                        "type": "number",
                        "description": (
                            "0.0-1.0. How relevant this program is for a parks/green "
                            "space project on this block. Never null — use a real "
                            "estimate even when information is incomplete."
                        ),
                    },
                    "eligibility_assessment": {
                        "type": "string",
                        "description": "1-2 sentences on whether this block likely qualifies, and why.",
                    },
                    "recommended_action": {
                        "type": "string",
                        "description": 'e.g. "Apply", "Investigate further", "Unlikely — income too high".',
                    },
                },
                "required": [
                    "program_name",
                    "relevance_score",
                    "eligibility_assessment",
                    "recommended_action",
                ],
                "additionalProperties": False,
            },
        }
    },
    "required": ["assessments"],
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


def _clamp_relevance(value) -> float:
    """Coerce a model-supplied relevance score into 0.0-1.0.

    The DB has a CHECK (relevance_score BETWEEN 0 AND 1), so an out-of-range
    value would fail the insert rather than the request.
    """
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.5


def _fallback(programs: list[dict], reason: str) -> list[dict]:
    """Assessment-failed result.

    Flagged with assessment_failed so a flat wall of 0.5 scores is visible in the
    review payload instead of looking like real model output.
    """
    log.error("Funding eligibility assessment failed (%s) — returning defaults", reason)
    return [
        {
            **p,
            "relevance_score": 0.5,
            "eligibility_assessment": f"Automated assessment unavailable ({reason}). Manual review required.",
            "recommended_action": "Investigate further",
            "assessment_failed": True,
        }
        for p in programs
    ]


def _designation_status(program: dict, determinations: dict) -> tuple[Optional[bool], Optional[str], str]:
    """
    Resolve a program's hard eligibility test into (confirmed, basis, label).

    `confirmed` is tri-state: True eligible, False confirmed ineligible, None when
    the program has no designation test or the test could not be evaluated.
    """
    key = program.get("requires_tract_designation")
    if not key:
        return None, None, "no location-based eligibility test"

    d = (determinations or {}).get(key)
    if not d:
        return None, None, f"{key}: UNDETERMINED (no determination on record)"

    eligible, basis = d.get("eligible"), d.get("basis")
    if eligible is True:
        return True, basis, f"{key}: CONFIRMED ELIGIBLE — {basis}"
    if eligible is False:
        return False, basis, f"{key}: NOT ELIGIBLE — {basis}"
    return None, basis, f"{key}: UNDETERMINED — {basis or 'lookup failed'}"


def extract_funding_eligibility(
    block_context: dict,
    programs: list[dict],
    determinations: Optional[dict] = None,
) -> list[dict]:
    """
    Use Claude to rank funding programs for a specific block.

    block_context: dict with tract_geoid, median_hh_income, sop_index_norm,
                   top_recs, transit_share
    programs: rows from the funding_programs table
    determinations: designation_key -> verdict, computed deterministically by the
                   agent (CDBG income rule, NMTC tract designation). These are
                   passed to the model as *established facts*, not questions — an
                   earlier version had the model guessing NMTC status from income.

    Returns the same programs, each augmented with relevance_score,
    eligibility_assessment, recommended_action, eligibility_confirmed and
    determination_basis. On failure, returns every program with relevance_score 0.5
    and assessment_failed=True.
    """
    client = _get_client()

    income = block_context.get("median_hh_income")
    income_str = f"${income:,.0f}" if income is not None else "unknown"

    # Resolve each program's hard eligibility test up front, so the model is told
    # the verdict rather than asked to infer it.
    status_by_name: dict[str, tuple[Optional[bool], Optional[str], str]] = {
        p.get("program_name"): _designation_status(p, determinations) for p in programs
    }

    # Only the fields the model needs to judge relevance — not award amounts or URLs.
    program_digest = [
        {
            "program_name": p.get("program_name"),
            "program_type": p.get("program_type"),
            "source_agency": p.get("source_agency"),
            "application_deadline": str(p.get("deadline")) if p.get("deadline") else None,
            "eligibility_notes": (p.get("eligibility_notes") or "")[:700],
            "verified_eligibility": status_by_name[p.get("program_name")][2],
        }
        for p in programs
    ]

    prompt = f"""You are a grant analyst helping a municipality fund built-environment
improvements on a specific city block in Prince George's County, Maryland.

BLOCK CONTEXT:
- Census Tract: {block_context.get('tract_geoid')}
- Median household income: {income_str}
- State of Place walkability score (normalized): {block_context.get('sop_index_norm', 'unknown')}/100
- Recommended improvements for this block: {', '.join(block_context.get('top_recs', [])) or 'none recorded'}
- Transit commuter share: {block_context.get('transit_share', 'unknown')}%

VERIFIED ELIGIBILITY DETERMINATIONS (authoritative — treat as established fact):
{json.dumps(determinations or {}, indent=2, default=str)}

PROGRAMS TO ASSESS (each carries its own verified_eligibility):
{json.dumps(program_digest, indent=2)}

Score each program's relevance for THIS block's recommended improvements.

Rules:
- Do not second-guess verified_eligibility. Where it says NOT ELIGIBLE, score
  relevance at or below 0.1 and say the block does not qualify. Where it says
  CONFIRMED ELIGIBLE, treat qualification as settled and judge only topical fit.
- Penalise geographic mismatch. A program restricted to another region (for example
  a Great Lakes initiative) is not relevant to Maryland however well the topic fits.
- Weigh whether the program funds what this block actually needs.
- The applicant is a city or county government, not an individual.

Return one entry per program, with program_name matching the input exactly."""

    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=2048,
            # Scoring, not writing. Without this the same block scored 70 then 95
            # across runs, because relevance values straddled a scoring tier
            # boundary — bad for a live demo and worse for evaluation.
            #
            # Sent via extra_body because anthropic 1.9.0 dropped `temperature` from
            # the create() signature; the API still honours it on Haiku 4.5. If this
            # model is ever swapped for Opus 4.7+ or Sonnet 5, remove it — those
            # reject sampling parameters with a 400.
            extra_body={"temperature": 0},
            output_config={"format": {"type": "json_schema", "schema": ASSESSMENT_SCHEMA}},
            messages=[{"role": "user", "content": prompt}],
        )
    except AuthenticationError as e:
        raise ValueError(f"Anthropic API key is invalid or expired: {e}") from e
    except APIConnectionError as e:
        raise ConnectionError(f"Cannot reach Anthropic API — check network: {e}") from e
    except APIStatusError as e:
        log.error("Anthropic API error %s: %s", e.status_code, e.message)
        raise

    # The deterministic verdict is attached regardless of what the model returns —
    # it comes from a rule or a dataset, so an LLM failure must not lose it.
    def with_determination(row: dict, program: dict) -> dict:
        confirmed, basis, _ = status_by_name[program.get("program_name")]
        return {**row, "eligibility_confirmed": confirmed, "determination_basis": basis}

    if response.stop_reason == "refusal":
        return [with_determination(r, r) for r in _fallback(programs, "model declined the request")]
    if response.stop_reason == "max_tokens":
        return [with_determination(r, r) for r in _fallback(programs, "response truncated at max_tokens")]

    try:
        text = next(b.text for b in response.content if b.type == "text")
        by_name = {
            a["program_name"]: a for a in json.loads(text)["assessments"]
        }
    except (json.JSONDecodeError, KeyError, TypeError, StopIteration) as e:
        return [
            with_determination(r, r)
            for r in _fallback(programs, f"unparseable response: {type(e).__name__}")
        ]

    assessed = []
    unmatched = []
    for p in programs:
        a = by_name.get(p.get("program_name"))
        if a is None:
            # Model dropped or renamed this program — keep it, flagged, rather
            # than silently shrinking the candidate list.
            unmatched.append(p.get("program_name"))
            assessed.append(
                with_determination(
                    {
                        **p,
                        "relevance_score": 0.5,
                        "eligibility_assessment": "Not assessed by model. Manual review required.",
                        "recommended_action": "Investigate further",
                        "assessment_failed": True,
                    },
                    p,
                )
            )
            continue
        assessed.append(
            with_determination(
                {
                    **p,
                    "relevance_score": _clamp_relevance(a.get("relevance_score")),
                    "eligibility_assessment": a.get("eligibility_assessment"),
                    "recommended_action": a.get("recommended_action"),
                },
                p,
            )
        )

    if unmatched:
        log.warning("Model returned no assessment for: %s", ", ".join(map(str, unmatched)))

    return assessed
