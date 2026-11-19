"""
Zoning steps for the pipeline engine. None of them calls a model.

A parcel's zone is on the county's map, and whether a zone allows a use is
written in the ordinance's use tables, so these steps look both up rather than
ask a model ("determine, don't infer", backend/tools/zoning.py):

    from_table      start records from data on file: the study blocks, the
                    recommendation types, or the zoning data in force
    spatial_lookup  for each block, the values of one attribute of the county
                    GIS layers within a buffer of its centreline
    use_table       read the Principal Use Tables (§27-5101) from enCodePlus,
                    one record per use with a P / SE / X / A field per zone

They wrap the tools the Zoning tab's refresh uses (zoning_lookup,
use_table_extract), with the same safety rails: a layer with no polygons around
the blocks is refused, a use table whose columns moved is refused, and a block
whose lookup fails is marked failed and left blank, never stored as "no zone".

Importing this module registers the steps, the "arcgis" source kind and the
zoning reference tables with the engine (kinds.py imports it).
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

import httpx

from backend.pipelines import engine
from backend.pipelines.engine import PipelineError
from backend.tools import use_table_extract, zoning, zoning_lookup

BASE_TABLES = ("27-5101(c)", "27-5101(d)", "27-5101(e)")   # (f) is the overlay table: "overrides", not "allowed"
ZONES = {t: zones for t, zones in use_table_extract.TABLES.values()}
ALL_ZONES = [z for t in BASE_TABLES for z in ZONES[t]]
USE_VALUES = ["P", "SE", "X", "A"]
STATUS_VALUES = ["found", "no_zone", "failed"]
PAGE = 1000                  # PostgREST returns at most this many rows per request


def zone_field(code: str) -> str:
    """The field name for a zone's column: TAC-C -> zone_tac_c."""
    return "zone_" + re.sub(r"[^a-z0-9]+", "_", code.lower()).strip("_")


# ── Data on file ─────────────────────────────────────────────────────────────
#
# Read through the engine's refs(), like the SQL reference tables, so a step
# never talks to the database itself. rows(db) returns dicts keyed by column.

def _all(db, table: str, cols: str, order: str) -> list[dict]:
    out, start = [], 0
    while True:
        page = db.table(table).select(cols).order(order).range(start, start + PAGE - 1).execute().data or []
        out += page
        if len(page) < PAGE:
            return out
        start += PAGE


def _blocks(db) -> list[dict]:
    return [{"block_id": b["id"], "street_name": b.get("street_name")}
            for b in _all(db, "blocks", "id, street_name", "id")]


def _block_geometry(db) -> list[dict]:
    return _all(db, "blocks", "id, geometry_geojson", "id")


def _rec_types(db) -> list[dict]:
    by: dict[str, dict] = {}
    for r in _all(db, "block_recommendations", "block_id, rec_label, direction", "id"):
        label = (r.get("rec_label") or "").strip()
        if not label:
            continue
        e = by.setdefault(label, {"rec_label": label, "blocks": set(), "increase": 0, "decrease": 0})
        e["blocks"].add(r["block_id"])
        e["increase" if r.get("direction") != "Decrease" else "decrease"] += 1
    return [{**e, "blocks": len(e["blocks"])} for e in sorted(by.values(), key=lambda e: e["rec_label"].lower())]


def _readings(db) -> list[dict]:
    zoning.refresh_active(db)
    out = []
    for label, m in zoning.load_rules()["rec_uses"].items():
        out.append({"rec_label": label, "governed_by": m.get("governed_by") or "use_table",
                    "ordinance_uses": " | ".join(u["row"] for u in m.get("uses") or []) or None,
                    "mode": m.get("mode"), "confidence": m.get("confidence"), "note": m.get("note")})
    return out


def use_rows(doc: dict, tables: tuple[str, ...] | list[str] = BASE_TABLES) -> list[dict]:
    """A use table document as one row per use, merged across tables by name, with a column per zone."""
    rows: dict[str, dict] = {}
    for u in doc.get("uses") or []:
        if u["table"] not in tables:
            continue
        r = rows.setdefault(zoning._norm(u["use"]), {"use": u["use"], "use_group": u.get("group"),
                                                    "use_category": u.get("category"), "tables": [],
                                                    "standards": [], "zones": {}})
        r["tables"].append(u["table"])
        r["zones"].update(u["zones"])
        r["standards"] += [s["text"] for s in u.get("standards") or [] if s["text"] not in r["standards"]]
    amended = (doc.get("source") or {}).get("amended_through")
    return [{"use": r["use"], "use_group": r["use_group"], "use_category": r["use_category"],
             "tables": "; ".join(r["tables"]), "standards": "; ".join(r["standards"]) or None,
             "amended_through": amended, **{zone_field(z): v for z, v in r["zones"].items()}}
            for r in rows.values()]


def _uses(db) -> list[dict]:
    zoning.refresh_active(db)
    return use_rows(zoning.load_use_table())


USE_COLUMNS = {"use": "VARCHAR", "use_group": "VARCHAR", "use_category": "VARCHAR", "tables": "VARCHAR",
               "standards": "VARCHAR", "amended_through": "VARCHAR", **{zone_field(z): "VARCHAR" for z in ALL_ZONES}}

ON_FILE: dict[str, dict] = {
    "blocks": {
        "label": "Study blocks", "key": "block_id", "rows": _blocks,
        "description": "The study's street-segment blocks: one row per block.",
        "columns": {"block_id": "DOUBLE", "street_name": "VARCHAR"},
    },
    "recommendation_types": {
        "label": "Recommendation types", "key": "rec_label", "rows": _rec_types,
        "description": "Every SoP recommendation label on the blocks, with how many blocks carry it and how often "
                       "it is an increase or a decrease.",
        "columns": {"rec_label": "VARCHAR", "blocks": "DOUBLE", "increase": "DOUBLE", "decrease": "DOUBLE"},
    },
    "zoning_readings": {
        "label": "Recommendation readings in force", "key": "rec_label", "rows": _readings,
        "description": "Which ordinance use each recommendation type is read as, in the zoning data in force. "
                       "governed_by is use_table, not_a_land_use or unknown; ordinance_uses lists use-table rows, "
                       "' | '-separated (use names contain semicolons).",
        "columns": {"rec_label": "VARCHAR", "governed_by": "VARCHAR", "ordinance_uses": "VARCHAR", "mode": "VARCHAR",
                    "confidence": "VARCHAR", "note": "VARCHAR"},
    },
    "zoning_uses": {
        "label": "Use table in force", "key": "use", "rows": _uses,
        "description": "The Principal Use Tables in force (§27-5101 (c)-(e)), one row per use, with a column per zone "
                       "(zone_tac_c ...) holding P (by right), SE (special exception), X (prohibited) or A.",
        "columns": USE_COLUMNS,
    },
}

for _name, _t in ON_FILE.items():
    engine.REF_TABLES[_name] = {"description": _t["description"], "columns": _t["columns"], "rows": _t["rows"],
                                "kinds": ["zoning"]}
engine.REF_TABLES["block_geometry"] = {"description": "Block centrelines (GeoJSON), for spatial_lookup.",
                                       "columns": {"id": "DOUBLE", "geometry_geojson": "VARCHAR"},
                                       "rows": _block_geometry, "kinds": ["zoning"], "hidden": True}


# ── The "arcgis" source kind ─────────────────────────────────────────────────

def fetch_arcgis(src: dict) -> dict:
    """An ArcGIS REST service or layer, described as text: its layers (a service) or its fields (a layer)."""
    url = src["url"].rstrip("/")
    try:
        resp = httpx.get(url, params={"f": "json"}, timeout=40.0)
        resp.raise_for_status()
        body = resp.json()
    except (httpx.HTTPError, ValueError) as e:
        raise PipelineError(f"{url} could not be read: {e}") from e
    if "error" in body:
        raise PipelineError(f"{url}: {body['error']}")
    if "layers" in body:
        layers = {str(l["id"]): l.get("name") for l in body["layers"]}
        lines = [f"ArcGIS service: {body.get('mapName') or body.get('serviceDescription') or url}",
                 (body.get("description") or "").strip()[:500], "", "Layers (id: name, geometry):"]
        lines += [f"{l['id']}: {l.get('name')} ({(l.get('geometryType') or '').replace('esriGeometry', '').lower() or 'group'})"
                  for l in body["layers"]]
    else:
        layers = {"": body.get("name")}
        lines = [f"ArcGIS layer: {body.get('name')}", (body.get("description") or "").strip()[:500], "",
                 f"Geometry: {body.get('geometryType')}", "Fields:"]
        lines += [f"{f['name']} ({f.get('type', '').replace('esriFieldType', '')})" for f in body.get("fields") or []]
    now = datetime.now(timezone.utc).isoformat()
    return {"url": url, "final_url": url, "text": "\n".join(lines), "truncated": False,
            "content_type": "arcgis service" if "layers" in body else "arcgis layer", "fetched_at": now,
            "layers": layers}


def check_arcgis_url(url: str) -> str:
    url = (url or "").strip().rstrip("/")
    if not url.startswith("https://") or "/rest/services/" not in url or not re.search(r"/(MapServer|FeatureServer)(/\d+)?$", url):
        raise PipelineError("An ArcGIS source is a REST service or layer URL, e.g. "
                            "https://gisdata.pgplanning.org/arcgis/rest/services/Applications/ZoningCertificationLetter/MapServer")
    return url


engine.SOURCE_KINDS["arcgis"] = fetch_arcgis


# ── Steps ────────────────────────────────────────────────────────────────────

def _columns_map(given: Any, columns: list[str], fields: list[dict], what: str) -> dict[str, str]:
    """{field: column}: the given pairs, then every schema field named like a column. Raises PipelineError."""
    names = {f["name"] for f in fields}
    if given is not None and not isinstance(given, dict):
        raise PipelineError(f"{what}.fields must be an object {{field: column}}.")
    out = {}
    for field, col in (given or {}).items():
        if field not in names:
            raise PipelineError(f"No field {field!r} in the schema.")
        if col not in columns:
            raise PipelineError(f"{col!r} is not a column; they are {', '.join(columns[:12])}{' …' if len(columns) > 12 else ''}.")
        out[field] = col
    for f in fields:
        if f["name"] not in out and f["name"] in columns:
            out[f["name"]] = f["name"]
    if not out:
        raise PipelineError(f"No schema field matches a column ({', '.join(columns[:12])}{' …' if len(columns) > 12 else ''}): "
                            "name fields like the columns, or map them in fields.")
    return out


def _source(sources: list[dict] | None, sid: Any, kind: str, what: str) -> dict:
    src = next((s for s in sources or [] if s["id"] == sid), None)
    if not src:
        raise PipelineError(f"{what}.source_id must name a source ({kind}).")
    if src["kind"] != kind:
        raise PipelineError(f"{what} reads a{'n' if kind[0] in 'aeiou' else ''} {kind} source; {sid} is {src['kind']}.")
    return src


def _new(rid: str, src_url: str | None = None) -> dict:
    return {"id": rid, "values": {}, "prov": {}, "source_url": src_url}


# from_table

def _from_table_validate(c: dict, fields: list[dict], sources: list[dict] | None) -> dict:
    t = c.get("table")
    if t not in ON_FILE:
        raise PipelineError(f"from_table.table must be one of {', '.join(ON_FILE)}.")
    c["fields"] = _columns_map(c.get("fields"), list(ON_FILE[t]["columns"]), fields, "from_table")
    return c


def _from_table_io(step: dict, fields: list[dict]) -> dict:
    c = step.get("config") or {}
    return {"rows": "creates", "writes": list((c.get("fields") or {}).keys()), "reads": c.get("table")}


def _from_table_run(step: dict, records: list[dict], fields: list[dict], ctx: dict) -> tuple[list[dict], list[str]]:
    c = step["config"]
    t = ON_FILE[c["table"]]
    rows = ctx["refs"](c["table"])
    new = []
    for row in rows:
        key = re.sub(r"[^A-Za-z0-9]+", "-", str(row.get(t["key"]))).strip("-")[:60] or str(len(new) + 1)
        rec = _new(f"{c['table']}-{key}")
        for field, col in c["fields"].items():
            v, _ = engine._normalise(next(f for f in fields if f["name"] == field), row.get(col))
            engine._set(rec, field, v, "derived", step["id"], note=f"{t['label']}, column {col}")
        new.append(rec)
    return records + new, [f"{len(new)} row(s) from {t['label']}"]


# spatial_lookup

def _spatial_validate(c: dict, fields: list[dict], sources: list[dict] | None) -> dict:
    _source(sources, c.get("source_id"), "arcgis", "spatial_lookup")
    names = {f["name"]: f for f in fields}
    for k in ("writes", "block_field"):
        c[k] = c.get(k) or ("block_id" if k == "block_field" else None)
        if c[k] not in names:
            raise PipelineError(f"spatial_lookup.{k} must be a schema field ({c[k]!r} is not).")
    if not (c.get("field") or "").strip():
        raise PipelineError("spatial_lookup.field must name the layer attribute to read (e.g. CLASS), or * for a "
                            "readable name from whatever name field the layer has.")
    layers = c.get("layers") or []
    if not isinstance(layers, list) or not all(str(x).isdigit() for x in layers):
        raise PipelineError("spatial_lookup.layers must be a list of layer ids, e.g. [59].")
    c["layers"] = [int(x) for x in layers] or None
    try:
        c["buffer_m"] = float(c.get("buffer_m") or 25)
    except (TypeError, ValueError):
        raise PipelineError("spatial_lookup.buffer_m must be a number of metres.")
    if not 5 <= c["buffer_m"] <= 100:
        raise PipelineError("spatial_lookup.buffer_m must be between 5 and 100 metres.")
    if c.get("status_field"):
        f = names.get(c["status_field"])
        if not f:
            raise PipelineError(f"spatial_lookup.status_field {c['status_field']!r} is not a schema field.")
        if f["type"] == "enum" and not set(STATUS_VALUES) <= set(f.get("enum_values") or []):
            raise PipelineError(f"{f['name']} must allow {', '.join(STATUS_VALUES)}.")
    return c


def _spatial_io(step: dict, fields: list[dict]) -> dict:
    c = step.get("config") or {}
    return {"rows": "keeps", "writes": [w for w in (c.get("writes"), c.get("status_field")) if w],
            "reads": c.get("source_id")}


def _spatial_run(step: dict, records: list[dict], fields: list[dict], ctx: dict) -> tuple[list[dict], list[str]]:
    c = step["config"]
    src = next(s for s in ctx["sources"] if s["id"] == c["source_id"])
    doc = ctx["source_doc"](src["id"])
    base = src["url"].rstrip("/")
    is_layer = re.search(r"/\d+$", base) is not None
    layer_ids = [None] if is_layer else (c.get("layers") or [])
    if not layer_ids:
        raise PipelineError(f"{src['id']} is a service: name the layers to read in config.layers.")
    layers = [(base if lid is None else f"{base}/{lid}",
               (doc.get("layers") or {}).get("" if lid is None else str(lid)) or f"layer {lid}") for lid in layer_ids]
    labelled = len(layers) > 1
    geoms = {int(float(r["id"])): r["geometry_geojson"] for r in ctx["refs"]("block_geometry") if r.get("geometry_geojson")}
    status = c.get("status_field")
    when = datetime.now(timezone.utc).date().isoformat()
    names = ", ".join(n for _, n in layers)
    note = f"{names}, within {c['buffer_m']:g} m of the block centreline, looked up {when}"

    def block_of(rec):
        try:
            return int(float(rec["values"].get(c["block_field"])))
        except (TypeError, ValueError):
            return None

    wanted = [geoms[b] for b in (block_of(r) for r in records) if b in geoms]
    if status and wanted:
        # As the refresh does: a layer with no polygons around our blocks would make every block "no zone".
        for url, name in layers:
            if zoning_lookup.sanity_check(zoning_lookup.bbox(wanted), {"zoning_layer_url": url}) == 0:
                raise PipelineError(f"{name} has no polygons around these blocks, so every block would come back "
                                    "with no zone. Check the source and layer.")

    def look(rec):
        b = block_of(rec)
        if b not in geoms:
            raise zoning_lookup.ZoningLookupError(f"no geometry for block {rec['values'].get(c['block_field'])!r}")
        vals = []
        for url, name in layers:
            attrs = zoning_lookup.near(url, geoms[b], c["buffer_m"], "*" if c["field"] == "*" else c["field"])
            got = (zoning_lookup.overlay_values(attrs) if c["field"] == "*" and attrs
                   else sorted({str(a[c["field"]]) for a in attrs if a.get(c["field"]) not in (None, "")}))
            vals += [f"{name}: {v}" if labelled else v for v in got]
        return vals

    warnings, failed = [], []
    say = ctx.get("say")
    if say:
        say(f"Looking up {len(records)} block(s) on {names}")
    for rec, got in zip(records, engine._parallel(look, records)):
        if isinstance(got, Exception):
            failed.append(rec["id"])
            if status:
                engine._set(rec, status, "failed", "derived", step["id"], note=f"{names}: {got}")
            continue
        engine._set(rec, c["writes"], "; ".join(got) or None, "derived", step["id"], note=note,
                    source_url=layers[0][0])
        if status:
            engine._set(rec, status, "found" if got else "no_zone", "derived", step["id"], note=note)
    if failed:
        warnings.append(f"{len(failed)} lookup(s) failed and were left blank, not stored as none: "
                        f"{', '.join(failed[:8])}{' …' if len(failed) > 8 else ''}")
    return records, warnings


# use_table

_USE_TABLE_CACHE: dict[str, dict] = {}


def read_use_table(url: str, fresh: bool = False) -> dict:
    if fresh or url not in _USE_TABLE_CACHE:
        try:
            _USE_TABLE_CACHE[url] = use_table_extract.extract(url)
        except use_table_extract.UseTableError as e:
            raise PipelineError(str(e)) from e
    return _USE_TABLE_CACHE[url]


def _use_table_validate(c: dict, fields: list[dict], sources: list[dict] | None) -> dict:
    _source(sources, c.get("source_id"), "url", "use_table")
    tables = c.get("tables") or list(BASE_TABLES)
    if not isinstance(tables, list) or any(t not in BASE_TABLES for t in tables):
        raise PipelineError(f"use_table.tables must be a list of {', '.join(BASE_TABLES)} (the overlay table (f) "
                            "overrides base zones, so it is not read as permissions).")
    c["tables"] = [t for t in BASE_TABLES if t in tables]
    cols = list(USE_COLUMNS)
    c["fields"] = _columns_map(c.get("fields"), cols, fields, "use_table")
    if "use" not in c["fields"].values():
        raise PipelineError("use_table needs a field for the use's name (column `use`).")
    return c


def _use_table_io(step: dict, fields: list[dict]) -> dict:
    c = step.get("config") or {}
    return {"rows": "creates", "writes": list((c.get("fields") or {}).keys()), "reads": c.get("source_id")}


def _use_table_run(step: dict, records: list[dict], fields: list[dict], ctx: dict) -> tuple[list[dict], list[str]]:
    c = step["config"]
    src = next(s for s in ctx["sources"] if s["id"] == c["source_id"])
    if ctx.get("say"):
        ctx["say"](f"Reading the use tables from {src.get('label') or src['url']}")
    doc = read_use_table(src["url"], fresh=ctx.get("fresh", False))
    cite = doc["source"]["url"]
    by_name = {f["name"]: f for f in fields}
    new = []
    for n, row in enumerate(use_rows(doc, c["tables"]), 1):
        rec = _new(f"{src['id']}-{n}", cite)
        zones = [k for k in row if k.startswith("zone_")]
        rec["excerpt"] = f"{row['use']}: " + ", ".join(f"{k[5:].upper()} {row[k]}" for k in zones)
        rec["excerpt_verified"] = True
        for field, col in c["fields"].items():
            v, _ = engine._normalise(by_name[field], row.get(col))
            engine._set(rec, field, v, "extracted", step["id"],
                        note=f"Table {row['tables']}, current through {doc['source'].get('amended_through')}",
                        source_url=cite)
        new.append(rec)
    return records + new, [f"{len(new)} use(s) from {', '.join(c['tables'])}, current through "
                           f"{doc['source'].get('amended_through') or 'an unstated amendment'}"]


STEPS: dict[str, dict] = {
    "from_table": {
        "label": "From data on file",
        "model": False, "kinds": ["zoning"], "starts": True,
        "description": "Start records from data already on file, one per row: " + "; ".join(
            f"{k} ({v['description']} Columns: {', '.join(list(v['columns'])[:8])}{' …' if len(v['columns']) > 8 else ''})"
            for k, v in ON_FILE.items())
            + ". Fields named like a column are filled from it (marked derived); map others in fields.",
        "config": {"table": " | ".join(ON_FILE), "fields": "optional object {field: column}; fields named like a column map themselves"},
        "validate": _from_table_validate, "io": _from_table_io, "run": _from_table_run,
    },
    "spatial_lookup": {
        "label": "Map lookup",
        "model": False, "kinds": ["zoning"],
        "description": "For each block record, read one attribute of the county GIS polygons within buffer_m of the "
                       "block's centreline from an arcgis source (a layer, or a service with config.layers), and write "
                       "the values, '; '-separated, into a field (marked derived). With several layers each value is "
                       "prefixed by its layer's name. status_field, on the zones lookup, records found / no_zone / "
                       "failed: a failed lookup is left blank, never stored as no zone, and a layer with no polygons "
                       "around the blocks stops the run. E.g. zones: layer 59 field CLASS; prior zones: layer 65 CLASS; "
                       "cases: layers 26, 46, 29, 31 field CASE_NUM; overlays: layers 14, 22, 68, 23, 19, 20, 21 field *.",
        "config": {"source_id": "str: an arcgis source", "layers": "list of layer ids (for a service source)",
                   "field": "str: layer attribute, or * for each polygon's name", "writes": "str: schema field",
                   "block_field": "optional str, default block_id", "buffer_m": "optional number 5-100, default 25",
                   "status_field": "optional str: enum field taking found / no_zone / failed"},
        "validate": _spatial_validate, "io": _spatial_io, "run": _spatial_run,
    },
    "use_table": {
        "label": "Read use tables",
        "model": False, "kinds": ["zoning"], "starts": True,
        "description": "Read the Principal Use Tables (Zoning Ordinance §27-5101 (c)-(e)) from an enCodePlus url "
                       "source (doc-view.aspx?secid=649), one record per use merged across tables, with the zone "
                       "headers checked so no value lands under the wrong zone. Columns: use, use_group, use_category, "
                       "tables, standards, amended_through, and zone_<code> (zone_tac_c, zone_rsf_65 ...) holding P, SE, "
                       "X or A. Fields named like a column are filled (marked extracted, citing the table).",
        "config": {"source_id": "str: the enCodePlus url source", "tables": f"optional list of {', '.join(BASE_TABLES)}",
                   "fields": "optional object {field: column}"},
        "validate": _use_table_validate, "io": _use_table_io, "run": _use_table_run,
    },
}

engine.CATALOG.update(STEPS)
