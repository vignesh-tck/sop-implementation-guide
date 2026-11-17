"""
Extract Prince George's County's Principal Use Tables into backend/data.

Run from the sop_implementation_guide folder:
    python -m scripts.extract_use_table [url]

Writes backend/data/zoning_use_table.json, the seed (version 0) of the use
table. The app's Zoning tab can also re-extract from a URL the user gives and
store the result as a new version in the database after review; this script is
for refreshing the seed file in the repo. See backend/tools/use_table_extract.py.
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.tools.use_table_extract import CITE_URL, UseTableError, extract  # noqa: E402

OUT = ROOT / "backend" / "data" / "zoning_use_table.json"


def main() -> int:
    url = sys.argv[1] if len(sys.argv) > 1 else CITE_URL
    try:
        doc = extract(url)
    except UseTableError as e:
        print(e, file=sys.stderr)
        return 1
    doc["source"]["extracted_by"] = "scripts/extract_use_table.py"
    for label, n in doc["source"]["row_counts"].items():
        print(f"{label}: {n} uses")
    OUT.write_text(json.dumps(doc, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Wrote {len(doc['uses'])} rows to {OUT.relative_to(ROOT)} "
          f"(current through {doc['source']['amended_through']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
