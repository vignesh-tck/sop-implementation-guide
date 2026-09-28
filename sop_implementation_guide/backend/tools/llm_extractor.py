"""LLM tool for extracting structured funding eligibility from raw program data."""

import json
import logging
import httpx2 as httpx
from anthropic import Anthropic, APIConnectionError, AuthenticationError, APIStatusError
from backend.config import settings

log = logging.getLogger(__name__)

_client = None


def _get_client() -> Anthropic:
    global _client
    if _client is None:
        if not settings.anthropic_api_key:
            raise ValueError("ANTHROPIC_API_KEY is not set in .env")
        # On corporate networks with SSL inspection, set ANTHROPIC_SSL_VERIFY=false in .env
        http_client = httpx.Client(verify=settings.anthropic_ssl_verify)
        _client = Anthropic(api_key=settings.anthropic_api_key, http_client=http_client)
        if not settings.anthropic_ssl_verify:
            log.warning("SSL verification disabled — corporate network mode")
    return _client


def extract_funding_eligibility(
    block_context: dict,
    programs: list[dict],
) -> list[dict]:
    """
    Use Claude to assess funding program eligibility for a specific block.

    block_context: dict with tract_geoid, median_hh_income, sop_index_norm,
                   top_recommendations, ejscreen_data (optional)
    programs: list of program dicts from epa_brownfields.get_brownfields_programs_for_park()

    Returns the same list with added keys: relevance_score, eligibility_assessment
    """
    client = _get_client()

    income = block_context.get('median_hh_income')
    income_str = f"${income:,.0f}" if income is not None else "unknown"

    prompt = f"""You are a grant eligibility analyst for urban parks in Prince George's County, MD.

BLOCK CONTEXT:
- Census Tract: {block_context.get('tract_geoid')}
- Median HH Income: {income_str}
- SoP Walkability Score (normalized): {block_context.get('sop_index_norm', 'unknown')}/100
- Top Recommendations: {', '.join(block_context.get('top_recs', []))}
- Transit commuter share: {block_context.get('transit_share', 'unknown')}%

CDBG ELIGIBILITY PRE-CHECK: {block_context.get('cdbg_check', {})}

PROGRAMS TO ASSESS:
{json.dumps(programs, indent=2)}

For each program, assess:
1. relevance_score (0.0-1.0): How relevant is this program for a parks/green space project on this block?
2. eligibility_assessment: Brief assessment of whether this block likely qualifies and why.

Return a JSON array with the same programs, each augmented with:
- "relevance_score": float 0.0-1.0
- "eligibility_assessment": string (1-2 sentences)
- "recommended_action": string (e.g., "Apply", "Investigate further", "Unlikely — income too high")

Return ONLY valid JSON, no markdown fences."""

    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=2048,
            messages=[{"role": "user", "content": prompt}],
        )
    except AuthenticationError as e:
        raise ValueError(f"Anthropic API key is invalid or expired: {e}") from e
    except APIConnectionError as e:
        raise ConnectionError(f"Cannot reach Anthropic API — check network: {e}") from e
    except APIStatusError as e:
        log.error("Anthropic API error %s: %s", e.status_code, e.message)
        raise

    try:
        return json.loads(response.content[0].text)
    except (json.JSONDecodeError, IndexError, AttributeError) as e:
        log.warning("LLM returned non-JSON response (%s) — using defaults", e)
        return [
            {**p, "relevance_score": 0.5, "eligibility_assessment": "Manual review required"}
            for p in programs
        ]
