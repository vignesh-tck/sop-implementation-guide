"""
Rule-based CDBG eligibility, computed from the block's own ACS data.

Formerly epa_brownfields.py, which was a misleading name: that module never called
an EPA Brownfields API. It contained three things — a hardcoded program list (now
replaced by funding_programs, synced from Grants.gov), a call to EPA EJSCREEN
(whose host no longer resolves, and whose result was never passed to the LLM), and
this function, which was the only part doing real per-block work.

This is a deterministic rule against a published HUD threshold, not an inference.
That distinction is the point: the LLM ranks and explains, it does not decide
whether a block qualifies.
"""

from typing import Optional

# Prince George's County, MD area median income. HUD publishes income limits
# annually, so this is a dated constant, not a permanent truth — it is surfaced in
# the determination's provenance so the vintage is visible downstream.
PRINCE_GEORGES_AMI_2024 = 111_800.0

# CDBG low/moderate-income area-benefit threshold: 80% of area median income.
LMI_THRESHOLD_RATIO = 0.80

SOURCE = "HUD CDBG low/moderate-income area-benefit rule"


def check_cdbg_eligibility(
    median_hh_income: Optional[float],
    area_median_income: float = PRINCE_GEORGES_AMI_2024,
) -> dict:
    """
    CDBG Low-to-Moderate Income (LMI) eligibility check.

    An area-benefit activity generally requires the service area's median household
    income to sit at or below 80% of area median income.

    Returns eligible=None when income is unknown — distinct from False. A missing
    input must not read as a determination of ineligibility.
    """
    if median_hh_income is None:
        return {
            "program": "CDBG (Community Development Block Grant)",
            "eligible": None,
            "reason": "Median household income unavailable for this block's tract.",
            "area_median_income": area_median_income,
            "source": SOURCE,
        }

    threshold = area_median_income * LMI_THRESHOLD_RATIO
    eligible = median_hh_income <= threshold

    return {
        "program": "CDBG (Community Development Block Grant)",
        "eligible": eligible,
        "median_hh_income": median_hh_income,
        "lmi_threshold": round(threshold, 0),
        "area_median_income": area_median_income,
        "reason": (
            f"Tract median household income ${median_hh_income:,.0f} is "
            f"{'at or below' if eligible else 'above'} the 80% AMI threshold of "
            f"${threshold:,.0f} (area median ${area_median_income:,.0f})."
        ),
        "source": SOURCE,
    }
