"""
Which zones front a block, looked up from Prince George's County's GIS.

This is the zoning half of "determine, don't infer": a parcel's zone is on the
county's map, so we read it there rather than ask a model. The layers are the
ones behind the county's zoning certification letters
(Applications/ZoningCertificationLetter on gisdata.pgplanning.org). The current
zoning layer already reflects the 2024 Bowie-Mitchellville Sectional Map
Amendment: TAC-C did not exist here before 2024, and the layer shows it.

A block is a street segment, so "fronting" means any zoning polygon within
buffer_m metres of the centreline (25 m by default, about a lane plus a
sidewalk past the right-of-way). Besides the zone, each block also gets:
  - the prior (pre-April 2022) zone, which decides permitted uses in the legacy
    LCD zone (§27-4205(c)(3));
  - development case numbers (Basic Plans, CDPs, SDPs, special exceptions),
    which are what an SME pulls to answer an LCD question;
  - any overlay zones, which can add or remove uses (Table 27-5101(f)).

Only the sync script calls this. Agents and the API read the stored rows, so a
county server outage cannot break a demo. A failed query raises
ZoningLookupError, which the sync records as a failure; it is never stored as
"no zones".
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import httpx

SERVICE = (
    "https://gisdata.pgplanning.org/arcgis/rest/services"
    "/Applications/ZoningCertificationLetter/MapServer"
)
ZONING_LAYER = 59
PRIOR_ZONING_LAYER = 65


class ZoningLookupError(RuntimeError):
    """A county GIS layer could not be queried."""


def _paths(geometry: dict) -> list:
    if geometry.get("type") == "LineString":
        return [geometry["coordinates"]]
    if geometry.get("type") == "MultiLineString":
        return geometry["coordinates"]
    raise ValueError(f"Expected a LineString block geometry, got {geometry.get('type')}")


def _query(layer: int | str, params: dict, timeout: float = 40.0) -> list[dict]:
    try:
        resp = httpx.get(f"{SERVICE}/{layer}/query", params={**params, "f": "json"}, timeout=timeout)
        resp.raise_for_status()
        body = resp.json()
    except httpx.HTTPError as e:
        raise ZoningLookupError(f"layer {layer}: {e}") from e
    except ValueError as e:
        raise ZoningLookupError(f"layer {layer}: non-JSON response") from e
    # ArcGIS reports logical errors inside an HTTP 200 body.
    if "error" in body:
        raise ZoningLookupError(f"layer {layer}: {body['error']}")
    return [f.get("attributes", {}) for f in body.get("features", [])]


def _near(layer: int | str, geometry: dict, buffer_m: float, out_fields: str) -> list[dict]:
    geom = {"paths": _paths(geometry), "spatialReference": {"wkid": 4326}}
    return _query(layer, {
        "geometry": json.dumps(geom),
        "geometryType": "esriGeometryPolyline",
        "inSR": 4326,
        "spatialRel": "esriSpatialRelIntersects",
        "distance": buffer_m,
        "units": "esriSRUnit_Meter",
        "outFields": out_fields,
        "returnGeometry": "false",
    })


def _overlay_values(rows: list[dict]) -> list[str]:
    """A readable name for each overlay polygon, from whatever name field the layer has."""
    names = set()
    for a in rows:
        name = next(
            (str(v) for k, v in a.items()
             if v and k.upper() in ("NAME", "OVERLAY", "CLASS", "ZONE", "LABEL", "DISTRICT", "PLAN_NAME")),
            None,
        )
        names.add(name or "present")
    return sorted(names)


def lookup_block(geometry: dict, rules: dict) -> dict:
    """
    Zones, prior zones, development cases and overlays within buffer_m of a block.

    rules: backend/data/zoning_rules.json (for buffer_m and the layer lists).
    Raises ZoningLookupError if any layer fails, so a partial answer is never
    stored as a complete one.
    """
    buffer_m = rules.get("buffer_m", 25)
    case_layers = rules.get("case_layers", {})
    overlay_layers = rules.get("overlay_layers", {})

    jobs = {
        "zones": (ZONING_LAYER, "CLASS"),
        "prior": (PRIOR_ZONING_LAYER, "CLASS"),
        **{f"case:{lid}": (lid, "CASE_NUM") for lid in case_layers},
        **{f"overlay:{lid}": (lid, "*") for lid in overlay_layers},
    }
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {k: pool.submit(_near, layer, geometry, buffer_m, fields) for k, (layer, fields) in jobs.items()}
        results = {k: f.result() for k, f in futures.items()}

    zones = sorted({a["CLASS"] for a in results["zones"] if a.get("CLASS")})
    prior = sorted({a["CLASS"] for a in results["prior"] if a.get("CLASS")})
    cases = {
        case_layers[lid]: sorted({a["CASE_NUM"] for a in results[f"case:{lid}"] if a.get("CASE_NUM")})
        for lid in case_layers
    }
    overlays = [
        {"layer": overlay_layers[lid], "values": _overlay_values(results[f"overlay:{lid}"])}
        for lid in overlay_layers if results[f"overlay:{lid}"]
    ]
    return {
        "zones": zones,
        "prior_zones": prior,
        "cases": {k: v for k, v in cases.items() if v},
        "overlays": overlays,
        "overlays_checked": sorted(overlay_layers.values()),
        "buffer_m": buffer_m,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "layer_url": f"{SERVICE}/{ZONING_LAYER}",
    }


def sanity_check(bbox: tuple[float, float, float, float]) -> int:
    """
    Count zoning polygons in the study area before trusting any result.

    A zero here means the layer moved or changed schema, and every block would
    come back "no zone". Returns the polygon count inside bbox (lon/lat).
    """
    xmin, ymin, xmax, ymax = bbox
    try:
        resp = httpx.get(f"{SERVICE}/{ZONING_LAYER}/query", params={
            "geometry": f"{xmin},{ymin},{xmax},{ymax}",
            "geometryType": "esriGeometryEnvelope",
            "inSR": 4326,
            "spatialRel": "esriSpatialRelIntersects",
            "returnCountOnly": "true",
            "f": "json",
        }, timeout=40)
        resp.raise_for_status()
        body = resp.json()
    except (httpx.HTTPError, ValueError) as e:
        raise ZoningLookupError(f"count failed: {e}") from e
    if "error" in body:
        raise ZoningLookupError(f"ArcGIS error: {body['error']}")
    return int(body.get("count", 0))
