"""
Which zones front a block, looked up from Prince George's County's GIS.

This is the zoning half of "determine, don't infer": a parcel's zone is on the
county's map, so we read it there rather than ask a model. The default layers
are the ones behind the county's zoning certification letters
(Applications/ZoningCertificationLetter on gisdata.pgplanning.org). The current
zoning layer already reflects the 2024 Bowie-Mitchellville Sectional Map
Amendment: TAC-C did not exist here before 2024, and the layer shows it.

The layers are settings, not constants: the Zoning tab can point a refresh at a
different zoning layer (default_settings() lists what can be set). Every
user-supplied layer is checked with describe_layer() before any block is run
against it.

A block is a street segment, so "fronting" means any zoning polygon within
buffer_m metres of the centreline (25 m by default, about a lane plus a
sidewalk past the right-of-way). Besides the zone, each block also gets:
  - the prior (pre-April 2022) zone, which decides permitted uses in the legacy
    LCD zone (§27-4205(c)(3));
  - development case numbers (Basic Plans, CDPs, SDPs, special exceptions),
    which are what an SME pulls to answer an LCD question;
  - any overlay zones, which can add or remove uses (Table 27-5101(f)).

Only the sync script and an app refresh call this. Findings, plans and scores
read the stored rows, so a county server outage cannot break a demo. A failed
query raises ZoningLookupError, which is recorded as a failure; it is never
stored as "no zones".
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx

SERVICE = (
    "https://gisdata.pgplanning.org/arcgis/rest/services"
    "/Applications/ZoningCertificationLetter/MapServer"
)


class ZoningLookupError(RuntimeError):
    """A county GIS layer could not be queried, or is not usable as given."""


def default_settings(rules: dict) -> dict:
    """The GIS settings used when no refresh has changed them (data version 0)."""
    return {
        "zoning_layer_url": f"{SERVICE}/59",
        "zone_field": "CLASS",
        "prior_layer_url": f"{SERVICE}/65",
        "prior_field": "CLASS",
        "context_service_url": SERVICE,   # case-number and overlay layers live here
        "buffer_m": rules.get("buffer_m", 25),
    }


def _paths(geometry: dict) -> list:
    if geometry.get("type") == "LineString":
        return [geometry["coordinates"]]
    if geometry.get("type") == "MultiLineString":
        return geometry["coordinates"]
    raise ValueError(f"Expected a LineString block geometry, got {geometry.get('type')}")


def _get(url: str, params: dict, timeout: float = 40.0) -> dict:
    try:
        resp = httpx.get(url, params={**params, "f": "json"}, timeout=timeout)
        resp.raise_for_status()
        body = resp.json()
    except httpx.HTTPError as e:
        raise ZoningLookupError(f"{url}: {e}") from e
    except ValueError as e:
        raise ZoningLookupError(f"{url}: non-JSON response") from e
    # ArcGIS reports logical errors inside an HTTP 200 body.
    if "error" in body:
        raise ZoningLookupError(f"{url}: {body['error']}")
    return body


def _near(layer_url: str, geometry: dict, buffer_m: float, out_fields: str) -> list[dict]:
    geom = {"paths": _paths(geometry), "spatialReference": {"wkid": 4326}}
    body = _get(f"{layer_url}/query", {
        "geometry": json.dumps(geom),
        "geometryType": "esriGeometryPolyline",
        "inSR": 4326,
        "spatialRel": "esriSpatialRelIntersects",
        "distance": buffer_m,
        "units": "esriSRUnit_Meter",
        "outFields": out_fields,
        "returnGeometry": "false",
    })
    return [f.get("attributes", {}) for f in body.get("features", [])]


def describe_layer(layer_url: str, field: str) -> dict:
    """
    Check that a layer URL is a queryable polygon layer with the given field.

    Run on every user-supplied layer before a refresh looks up any block, so a
    typo fails in seconds with a reason instead of 68 failed lookups.
    """
    parsed = urlparse(layer_url or "")
    if parsed.scheme != "https" or "/rest/services/" not in parsed.path:
        raise ZoningLookupError(
            "Expected an ArcGIS REST layer URL, e.g. https://…/rest/services/…/MapServer/59"
        )
    body = _get(layer_url, {})
    if body.get("type") not in ("Feature Layer", None) or "query" not in (body.get("capabilities") or "").lower():
        raise ZoningLookupError(f"{layer_url} is not a queryable feature layer.")
    if body.get("geometryType") != "esriGeometryPolygon":
        raise ZoningLookupError(f"{layer_url} has {body.get('geometryType')} geometry; zoning needs polygons.")
    fields = [f["name"] for f in body.get("fields") or []]
    if field not in fields:
        raise ZoningLookupError(f"{layer_url} has no field {field!r}. Fields: {', '.join(fields)}")
    return {"name": body.get("name"), "description": (body.get("description") or "")[:300], "fields": fields}


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


def lookup_block(geometry: dict, rules: dict, settings: dict | None = None) -> dict:
    """
    Zones, prior zones, development cases and overlays within buffer_m of a block.

    rules: backend/data/zoning_rules.json (for the case and overlay layer lists).
    settings: GIS settings (default_settings() if omitted).
    Raises ZoningLookupError if any layer fails, so a partial answer is never
    stored as a complete one.
    """
    s = {**default_settings(rules), **(settings or {})}
    buffer_m = s["buffer_m"]
    ctx = s["context_service_url"]
    case_layers = rules.get("case_layers", {})
    overlay_layers = rules.get("overlay_layers", {})

    jobs = {
        "zones": (s["zoning_layer_url"], s["zone_field"]),
        "prior": (s["prior_layer_url"], s["prior_field"]),
        **{f"case:{lid}": (f"{ctx}/{lid}", "CASE_NUM") for lid in case_layers},
        **{f"overlay:{lid}": (f"{ctx}/{lid}", "*") for lid in overlay_layers},
    }
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {k: pool.submit(_near, url, geometry, buffer_m, field) for k, (url, field) in jobs.items()}
        results = {k: f.result() for k, f in futures.items()}

    zones = sorted({str(a[s["zone_field"]]) for a in results["zones"] if a.get(s["zone_field"])})
    prior = sorted({str(a[s["prior_field"]]) for a in results["prior"] if a.get(s["prior_field"])})
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
        "layer_url": s["zoning_layer_url"],
        "prior_layer_url": s["prior_layer_url"],
    }


def sanity_check(bbox: tuple[float, float, float, float], settings: dict | None = None, rules: dict | None = None) -> int:
    """
    Count zoning polygons in the study area before trusting any result.

    A zero here means the layer moved, changed schema or covers somewhere else,
    and every block would come back "no zone". Returns the polygon count inside
    bbox (lon/lat).
    """
    s = {**default_settings(rules or {}), **(settings or {})}
    xmin, ymin, xmax, ymax = bbox
    body = _get(f"{s['zoning_layer_url']}/query", {
        "geometry": f"{xmin},{ymin},{xmax},{ymax}",
        "geometryType": "esriGeometryEnvelope",
        "inSR": 4326,
        "spatialRel": "esriSpatialRelIntersects",
        "returnCountOnly": "true",
    })
    return int(body.get("count", 0))


def bbox(geometries: list[dict]) -> tuple[float, float, float, float]:
    pts = [p for g in geometries for path in _paths(g) for p in path]
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return min(xs), min(ys), max(xs), max(ys)


def near(layer_url: str, geometry: dict, buffer_m: float, out_fields: str) -> list[dict]:
    """The attributes of a layer's features within buffer_m of a block centreline. Raises ZoningLookupError."""
    return _near(layer_url, geometry, buffer_m, out_fields)


def overlay_values(rows: list[dict]) -> list[str]:
    """A readable name for each polygon, from whatever name field the layer has."""
    return _overlay_values(rows)
