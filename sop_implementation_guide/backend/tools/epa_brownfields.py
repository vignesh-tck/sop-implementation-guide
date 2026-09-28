"""
Tools for querying EPA and HUD APIs used by FundingAgent.

EPA EJSCREEN: environmental justice scores by lat/lng — informs equity grant eligibility.
CDBG eligibility: rule-based check against HUD income thresholds using ACS data we have.
"""

import httpx
from typing import Optional


EJSCREEN_URL = "https://ejscreen.epa.gov/mapper/ejscreenRESTbroker.aspx"


def get_ejscreen_data(lat: float, lng: float, radius_miles: float = 0.5) -> dict:
    """
    Query EPA EJSCREEN for environmental justice indicators at a coordinate.
    Returns percentile scores for pollution burden, demographics, etc.
    """
    params = {
        "namestr": "",
        "geometry": f'{{"spatialReference":{{"wkid":4326}},"x":{lng},"y":{lat}}}',
        "distance": radius_miles,
        "unit": "9035",  # miles
        "areatype": "",
        "areaid": "",
        "f": "pjson",
    }
    try:
        resp = httpx.get(EJSCREEN_URL, params=params, timeout=15)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        return {"error": str(e), "source": "EPA EJSCREEN"}


def check_cdbg_eligibility(
    median_hh_income: Optional[float],
    area_median_income: float = 111_800.0,  # Prince George's County AMI 2024
) -> dict:
    """
    CDBG Low-to-Moderate Income (LMI) eligibility check.
    A census tract qualifies if median HH income <= 80% of Area Median Income.
    Prince George's County AMI 2024 ≈ $111,800 (HUD CPD Maps).
    """
    if median_hh_income is None:
        return {"eligible": None, "reason": "Income data unavailable"}

    threshold = area_median_income * 0.80
    eligible = median_hh_income <= threshold

    return {
        "program": "CDBG (Community Development Block Grant)",
        "eligible": eligible,
        "median_hh_income": median_hh_income,
        "lmi_threshold": round(threshold, 0),
        "area_median_income": area_median_income,
        "reason": (
            f"Tract income ${median_hh_income:,.0f} is {'below' if eligible else 'above'} "
            f"80% AMI threshold of ${threshold:,.0f}"
        ),
        "source": "HUD CDBG LMI eligibility rule",
    }


def get_brownfields_programs_for_park(tract_geoid: str) -> list[dict]:
    """
    Return a curated list of federal/state funding programs relevant to
    parks and green space in Prince George's County, MD.

    These are well-established programs — no API needed, eligibility is
    assessed using the block's ACS data + EJSCREEN scores.
    """
    programs = [
        {
            "program_name": "EPA Brownfields Assessment Grant",
            "program_type": "federal",
            "source_agency": "U.S. Environmental Protection Agency",
            "award_amount_min": 200_000,
            "award_amount_max": 500_000,
            "eligibility_notes": (
                "Available to communities for assessing brownfield sites for potential "
                "reuse as parks/green space. Requires documented brownfield site. "
                "Priority given to areas with environmental justice concerns."
            ),
            "application_url": "https://www.epa.gov/brownfields/brownfields-assessment-grants",
        },
        {
            "program_name": "EPA Brownfields Cleanup Grant",
            "program_type": "federal",
            "source_agency": "U.S. Environmental Protection Agency",
            "award_amount_min": 500_000,
            "award_amount_max": 500_000,
            "eligibility_notes": (
                "For cleanup of a specific brownfield site. Site must be owned by "
                "applicant or have site access agreement. Strong fit for converting "
                "contaminated parcels to park space."
            ),
            "application_url": "https://www.epa.gov/brownfields/brownfields-cleanup-grants",
        },
        {
            "program_name": "Land and Water Conservation Fund (LWCF) — State Formula",
            "program_type": "federal",
            "source_agency": "National Park Service / MD DNR",
            "award_amount_min": 50_000,
            "award_amount_max": 1_000_000,
            "eligibility_notes": (
                "50/50 match required. Administered by MD Dept of Natural Resources. "
                "Funds acquisition and development of public outdoor recreation areas. "
                "Parks and public gardens are eligible uses."
            ),
            "application_url": "https://dnr.maryland.gov/land/Pages/lwcf.aspx",
        },
        {
            "program_name": "Maryland Program Open Space (POS)",
            "program_type": "state",
            "source_agency": "Maryland Department of Natural Resources",
            "award_amount_min": 25_000,
            "award_amount_max": 500_000,
            "eligibility_notes": (
                "State grant for local governments to acquire, develop, and improve "
                "outdoor recreational areas. No match required for some categories. "
                "Prince George's County has active POS allocation history."
            ),
            "application_url": "https://dnr.maryland.gov/land/Pages/pos.aspx",
        },
        {
            "program_name": "New Markets Tax Credit (NMTC)",
            "program_type": "federal",
            "source_agency": "CDFI Fund / U.S. Treasury",
            "award_amount_min": None,
            "award_amount_max": None,
            "eligibility_notes": (
                "Available in Low-Income Community census tracts (LIC designation). "
                "Primarily for commercial/mixed-use with community benefit component. "
                "Can finance park-adjacent retail or community facilities."
            ),
            "application_url": "https://www.cdfifund.gov/programs-training/programs/new-markets-tax-credit",
        },
    ]
    return programs
