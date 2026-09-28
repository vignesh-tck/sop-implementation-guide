"""
Per-census-tract funding designations, looked up from published GIS datasets.

This is the "funding GIS dataset" half of the sponsor's framing: whether a block
can use a given program is often a *spatial designation*, not a judgement call.
NMTC is the first one implemented — a tract either carries the Low-Income
Community designation or it does not, and that is a fact we can cite rather than
ask an LLM to guess.

Blocks already carry tract_geoid (pre-joined in the source GeoJSON), so these are
attribute queries against an 11-digit GEOID. No PostGIS spatial join is needed
yet; when zoning arrives and we need parcel-level overlays, that changes.

Designation semantics, which matter:
  True   -> tract is designated
  False  -> tract is confirmed NOT designated (queried successfully, no match)
  None   -> lookup failed; we do not know

None must never be collapsed to False. Recording "not eligible" because a server
timed out would silently suppress real funding.
"""

from __future__ import annotations

import logging
from typing import Optional

import httpx

log = logging.getLogger(__name__)

# Verified Sep 2026: 35,167 features nationally, 122 in Prince George's County
# (24033). Field NMTC_Quali holds the 11-digit tract GEOID. Layer id is 124.
#
# Note on provenance: this is a community-republished layer, not a CDFI Fund
# primary source. We record source_url and vintage on every determination so the
# claim is auditable, and cross-checking against the official CDFI Fund LIC list
# is tracked as follow-up work.
NMTC_LAYER = (
    "https://services6.arcgis.com/BAJNi3EgCdtQ1BCG/arcgis/rest/services"
    "/NMTC_Qualified_Tracts_2025/FeatureServer/124"
)
NMTC_GEOID_FIELD = "NMTC_Quali"

NMTC_SOURCE = {
    "source_name": "NMTC Qualified Tracts 2025 (ArcGIS FeatureServer)",
    "source_url": NMTC_LAYER,
    "source_vintage": "2025",
}


class EligibilityLookupError(RuntimeError):
    """The designation dataset could not be queried."""


def _query(layer: str, where: str, out_fields: str = "*", timeout: float = 40.0) -> list[dict]:
    params = {
        "where": where,
        "outFields": out_fields,
        "returnGeometry": "false",
        "f": "json",
    }
    try:
        resp = httpx.get(f"{layer}/query", params=params, timeout=timeout)
        resp.raise_for_status()
        body = resp.json()
    except httpx.HTTPError as e:
        raise EligibilityLookupError(f"query failed: {e}") from e
    except ValueError as e:
        raise EligibilityLookupError(f"non-JSON response: {e}") from e

    # ArcGIS reports logical errors inside an HTTP 200 body.
    if "error" in body:
        raise EligibilityLookupError(f"ArcGIS error: {body['error']}")
    return [f.get("attributes", {}) for f in body.get("features", [])]


def _count(layer: str, where: str, timeout: float = 40.0) -> int:
    """Count matches without transferring rows."""
    params = {"where": where, "returnCountOnly": "true", "f": "json"}
    try:
        resp = httpx.get(f"{layer}/query", params=params, timeout=timeout)
        resp.raise_for_status()
        body = resp.json()
    except httpx.HTTPError as e:
        raise EligibilityLookupError(f"count failed: {e}") from e
    except ValueError as e:
        raise EligibilityLookupError(f"non-JSON response: {e}") from e

    if "error" in body:
        raise EligibilityLookupError(f"ArcGIS error: {body['error']}")
    return int(body.get("count", 0))


def lookup_nmtc(tract_geoid: str) -> dict:
    """
    Determine NMTC Low-Income Community designation for one census tract.

    Returns a dict ready to persist into tract_eligibility:
      {program_key, tract_geoid, eligible, basis, source_*, raw_data}

    Raises EligibilityLookupError on transport failure so the caller can record
    eligible=None rather than a false negative.
    """
    geoid = str(tract_geoid).strip()
    if not geoid:
        raise ValueError("tract_geoid is required")

    # The layer contains only qualified tracts, so presence *is* the designation.
    rows = _query(NMTC_LAYER, f"{NMTC_GEOID_FIELD}='{geoid}'")
    eligible = len(rows) > 0

    if eligible:
        basis = (
            f"Census tract {geoid} is present in the NMTC qualified-tract layer "
            f"(2025 vintage), so it carries the Low-Income Community designation."
        )
    else:
        basis = (
            f"Census tract {geoid} is absent from the NMTC qualified-tract layer "
            f"(2025 vintage). The layer lists only qualified tracts, so absence "
            f"means the tract is not NMTC-designated."
        )

    return {
        "program_key": "NMTC",
        "tract_geoid": geoid,
        "eligible": eligible,
        "basis": basis,
        **NMTC_SOURCE,
        "raw_data": {"matched_features": rows[:1]},
    }


def sanity_check_layer(county_prefix: str = "24033") -> int:
    """
    Confirm the layer actually covers our study area before trusting a negative.

    If a whole county returns zero qualified tracts, a 'not designated' verdict is
    more likely a schema or vintage mismatch than a real answer. Returns the count
    of qualified tracts whose GEOID starts with county_prefix.
    """
    return _count(NMTC_LAYER, f"{NMTC_GEOID_FIELD} LIKE '{county_prefix}%'")


# Registry so sync scripts and future designations (Energy Communities, USDA/SBA
# loan areas) can be added without touching call sites.
DESIGNATION_LOOKUPS = {
    "NMTC": lookup_nmtc,
}
