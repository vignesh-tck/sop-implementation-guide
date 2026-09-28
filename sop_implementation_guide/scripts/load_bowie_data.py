"""
Load Bowie data into Supabase.

Run from the sop_implementation_guide folder:
    python -m scripts.load_bowie_data

Loads:
  1. bowie_features_scores_tracts_acs.geojson  → blocks table
  2. Final_Bowie_OrderedBlockwiseRecs_20240314 Overall.csv → block_recommendations table

The data files live one folder up (in the project root).
"""

import json
import os
import sys
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

load_dotenv()

# Add project root to path so backend imports work
sys.path.insert(0, str(Path(__file__).parent.parent))

from backend.db.session import get_supabase

DATA_DIR = Path(__file__).parent.parent.parent  # one level up from sop_implementation_guide
GEOJSON_PATH = DATA_DIR / "bowie_features_scores_tracts_acs.geojson"
CSV_PATH = DATA_DIR / "Final_Bowie_OrderedBlockwiseRecs_20240314 Overall.csv"


# ── Field groups from GeoJSON properties ──────────────────────────────────────

SCORE_MAP = {
    "sopindex7": "sop_index",
    "sop7norm": "sop_index_norm",
    "form3": "form_score",
    "dens2": "density_score",
    "prox3": "proximity_score",
    "conn6": "connectivity_score",
    "parks2": "parks_score",
    "peds4": "pedestrian_score",
    "safe": "safety_score",
    "traffic5": "traffic_score",
    "aesttot3": "aesthetics_score",
}

ACS_MAP = {
    "tract_geoid": "tract_geoid",
    "tract_name": "tract_name",
    "total_pop": "total_pop",
    "median_hh_income": "median_hh_income",
    "median_home_value": "median_home_value",
    "commuters_total": "commuters_total",
    "commuters_transit": "commuters_transit",
    "renter_no_vehicle": "renter_no_vehicle",
}

# GeoJSON stores these as floats (e.g. 2004.0); schema declares them INTEGER
ACS_INTEGER_FIELDS = {"total_pop", "commuters_total", "commuters_transit", "renter_no_vehicle"}


def _normalize_direction(val) -> str | None:
    """Normalize '(Best) Increase' / '(Best) Decrease' → 'Increase' / 'Decrease'."""
    if val is None or (isinstance(val, float)):
        return None
    s = str(val).strip()
    if "Increase" in s:
        return "Increase"
    if "Decrease" in s:
        return "Decrease"
    return None


def _to_int(val):
    """Cast a float-like value to int, returning None if null."""
    if val is None:
        return None
    try:
        return int(float(val))
    except (TypeError, ValueError):
        return None

SKIP_KEYS = {"Unnamed: 0", " ", "adultuse"}


def extract_features(props: dict, suffix: str) -> dict:
    """Pull all _x or _y features from a properties dict."""
    return {
        k[: -len(suffix)]: v
        for k, v in props.items()
        if k.endswith(suffix) and k not in SKIP_KEYS
    }


def load_geojson(db) -> int:
    print(f"Loading {GEOJSON_PATH} ...")
    with open(GEOJSON_PATH, encoding="utf-8") as f:
        gj = json.load(f)

    rows = []
    for feat in gj["features"]:
        p = feat["properties"]

        row: dict = {
            "id": p["sgmntid"],
            "street_name": p.get("name_2") or p.get("StreetName", ""),
            "intersection_a": p.get("intersc1") or p.get("IntersectionA"),
            "intersection_b": p.get("intersc2") or p.get("IntersectionB"),
            "neighborhood": p.get("neighborhood", "Bowie Town Center"),
        }

        for src, dst in SCORE_MAP.items():
            row[dst] = p.get(src)

        for src, dst in ACS_MAP.items():
            val = p.get(src)
            row[dst] = _to_int(val) if dst in ACS_INTEGER_FIELDS else val

        row["features_current"] = extract_features(p, "_x")
        row["features_recommended"] = extract_features(p, "_y")
        row["geometry_geojson"] = feat["geometry"]

        rows.append(row)

    result = db.table("blocks").upsert(rows, on_conflict="id").execute()
    count = len(result.data) if result.data else 0
    print(f"  ✓ {count} blocks loaded")
    return count


def load_csv(db) -> int:
    print(f"Loading {CSV_PATH} ...")
    df = pd.read_csv(CSV_PATH, skiprows=2, header=0, low_memory=False)

    # Keep only rows that have a numeric block number
    df = df[pd.to_numeric(df.iloc[:, 1], errors="coerce").notna()].copy()
    df.columns = [str(c) for c in df.columns]

    # All 10 SoP dimensions with their column positions (rec, score, direction).
    # Cols 6–72: first 3 dimensions (no named header — inferred from CSV structure).
    # Cols 75+:  remaining 7 dimensions (have named headers in CSV).
    dim_col_sets = {
        "Personal Safety": [
            (6, 7, 8), (9, 10, 11), (12, 13, 14),
        ],
        "Traffic Safety": [
            (17, 18, 19), (20, 21, 22), (23, 24, 25), (26, 27, 28), (29, 30, 31),
            (32, 33, 34), (35, 36, 37), (38, 39, 40), (41, 42, 43), (44, 45, 46),
        ],
        "Pedestrian & Bike Amenities": [
            (49, 50, 51), (52, 53, 54), (55, 56, 57), (58, 59, 60),
            (61, 62, 63), (64, 65, 66), (67, 68, 69), (70, 71, 72),
        ],
        "Aesthetics": [
            (75, 76, 77), (78, 79, 80), (81, 82, 83), (84, 85, 86),
            (87, 88, 89), (90, 91, 92), (93, 94, 95), (96, 97, 98),
        ],
        "Proximity": [
            (101, 102, 103), (104, 105, 106), (107, 108, 109),
            (110, 111, 112), (113, 114, 115),
        ],
        "Recreational Facilities": [
            (118, 119, 120), (121, 122, 123),
        ],
        "Parks & Public Spaces": [
            (126, 127, 128), (129, 130, 131), (132, 133, 134),
        ],
        "Form": [
            (137, 138, 139), (140, 141, 142), (143, 144, 145),
            (146, 147, 148), (149, 150, 151),
        ],
        "Density": [
            (154, 155, 156), (157, 158, 159),
        ],
        "Connectivity": [
            (162, 163, 164),
        ],
    }

    cols = list(df.columns)
    rows = []

    for _, row in df.iterrows():
        block_id = int(pd.to_numeric(row.iloc[1]))

        for dim, col_sets in dim_col_sets.items():
            for rank, (rc, sc, dc) in enumerate(col_sets, start=1):
                if rc >= len(cols):
                    continue
                label = row.iloc[rc]
                score = row.iloc[sc] if sc < len(cols) else None
                direction = row.iloc[dc] if dc < len(cols) else None
                if pd.notna(label) and str(label).strip():
                    rows.append({
                        "block_id": block_id,
                        "rec_rank": rank,
                        "rec_label": str(label).strip(),
                        "dimension": dim,
                        "predicted_score_increase": float(score) if pd.notna(score) else None,
                        "direction": _normalize_direction(direction),
                    })

    if not rows:
        print("  ⚠ No recommendation rows parsed — check CSV column indices")
        return 0

    # Upsert in chunks of 500
    total = 0
    for i in range(0, len(rows), 500):
        chunk = rows[i : i + 500]
        result = (
            db.table("block_recommendations")
            .upsert(chunk, on_conflict="block_id,rec_rank,dimension")
            .execute()
        )
        total += len(result.data) if result.data else 0

    print(f"  ✓ {total} recommendation rows loaded")
    return total


def main():
    db = get_supabase()
    load_geojson(db)
    load_csv(db)
    print("\nDone. Run the FastAPI server: uvicorn backend.main:app --reload")


if __name__ == "__main__":
    main()
