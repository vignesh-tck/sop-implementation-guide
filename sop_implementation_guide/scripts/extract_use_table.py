"""
Extract Prince George's County's Principal Use Tables into backend/data.

Run from the sop_implementation_guide folder:
    python -m scripts.extract_use_table

Downloads Section 27-5101 of the Zoning Ordinance from the County's official
code host (enCodePlus) and writes backend/data/zoning_use_table.json: for every
principal use, whether each zone permits it by right (P), only by special
exception (SE), or prohibits it (X), plus links to the use-specific standards.

The table is extracted, never typed by hand, so it can be re-run when the
Council amends the ordinance. The amendment the text is current through is
read from the section's own editor's notes and stored with the data.

Safety rail: each table's zone header is checked against the columns we expect.
If enCodePlus reorders a column, the script stops rather than silently filing
every value under the wrong zone.
"""

import json
import re
import sys
from datetime import date
from html import unescape
from html.parser import HTMLParser
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "backend" / "data" / "zoning_use_table.json"

BASE = "https://online.encodeplus.com/regs/princegeorgescounty-md/"
FETCH_URL = BASE + "doc-view.aspx?secid=649"
CITE_URL = BASE + "doc-viewer.aspx#secid-649"

# Column order of each table, as the header rows print it. Checked on every run.
TABLES = {
    "5101-1": ("27-5101(c)", ["ROS", "AG", "AR", "RE", "RR", "RSF-95", "RSF-65", "RSF-A",
                              "RMF-12", "RMF-20", "RMF-48"]),
    "5101-2": ("27-5101(d)", ["CN", "CS", "CGO", "IE", "IH", "NAC", "TAC-C", "TAC-E",
                              "LTO-C", "LTO-E", "RTO-L-C", "RTO-L-E", "RTO-H-C", "RTO-H-E", "RMH"]),
    "5101-3": ("27-5101(e)", ["R-PD", "NAC-PD", "TAC-PD", "LTO-PD", "RTO-PD", "MU-PD", "IE-PD"]),
    "5101-4": ("27-5101(f)", ["RCO", "LDO", "IDO", "APA-1", "APA-2", "APA-3S", "APA-3M", "APA-4",
                              "APA-5", "APA-6", "APZ", "CZ", "HINA"]),
}

# The header row that names the zones, per table, with "Core"/"Edge" expanded.
# (NAC spans two header rows in table (d), so it is absent from the second row.)
EXPECTED_HEADER = {
    "5101-1": ["ROS", "AG", "AR", "RE", "RR", "RSF-95", "RSF-65", "RSF-A", "RMF-12", "RMF-20", "RMF-48"],
    "5101-2": ["CN", "CS", "CGO", "IE", "IH", "Core", "Edge", "Core", "Edge", "Core", "Edge",
               "Core", "Edge", "RMH"],
    "5101-3": ["R-PD", "NAC-PD", "TAC-PD", "LTO-PD", "RTO-PD", "MU-PD", "IE-PD"],
    "5101-4": ["RCO", "LDO", "IDO", "APA-1", "APA-2", "APA-3S", "APA-3M", "APA-4", "APA-5",
               "APA-6", "APZ", "CZ", "HINA"],
}


def clean(text: str) -> str:
    # enCodePlus uses U+2010 hyphens in zone names ("RSF‐95").
    return re.sub(r"\s+", " ", unescape(text)).replace("‐", "-").strip()


class TableParser(HTMLParser):
    """Collect each <table id=...> as rows of cells; a cell keeps its text, colspan and links."""

    def __init__(self):
        super().__init__()
        self.tables: dict[str, list[list[dict]]] = {}
        self._table_id: str | None = None
        self._depth = 0
        self._row: list[dict] | None = None
        self._cell: dict | None = None
        self._link: dict | None = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "table":
            self._depth += 1
            if self._depth == 1 and a.get("id") in TABLES:
                self._table_id = a["id"]
                self.tables[self._table_id] = []
        elif self._table_id is None:
            return
        elif tag == "tr":
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = {"text": "", "span": int(a.get("colspan") or 1), "links": []}
        elif tag == "a" and self._cell is not None and a.get("href"):
            self._link = {"href": a["href"], "text": ""}
        elif tag == "br" and self._cell is not None:
            self._cell["text"] += " "

    def handle_endtag(self, tag):
        if tag == "table":
            if self._depth == 1:
                self._table_id = None
            self._depth -= 1
        elif self._table_id is None:
            return
        elif tag == "a" and self._link is not None and self._cell is not None:
            self._cell["links"].append(self._link)
            self._link = None
        elif tag in ("td", "th") and self._cell is not None and self._row is not None:
            self._cell["text"] = clean(self._cell["text"])
            self._row.append(self._cell)
            self._cell = None
        elif tag == "tr" and self._row is not None:
            self.tables[self._table_id].append(self._row)
            self._row = None

    def handle_data(self, data):
        if self._cell is not None:
            self._cell["text"] += data
        if self._link is not None:
            self._link["text"] += data


def absolute(href: str) -> str:
    return href if href.startswith("http") else BASE + href.lstrip("/")


def parse_table(table_id: str, rows: list[list[dict]]) -> list[dict]:
    label, zones = TABLES[table_id]
    n = len(zones)

    header = next((r for r in rows if [c["text"] for c in r][:2] == EXPECTED_HEADER[table_id][:2]), None)
    if header is None or [c["text"] for c in header] != EXPECTED_HEADER[table_id]:
        got = [c["text"] for c in header] if header else None
        raise SystemExit(f"Table {label}: zone header changed. Expected {EXPECTED_HEADER[table_id]}, got {got}.")

    uses = []
    group = category = None
    for r in rows:
        if len(r) == 1:
            # A full-width row is either a title, the P/SE/X key, or a group heading.
            text = r[0]["text"]
            if not text.startswith(("Table 27-5101", "P =", "A =", "A blank")):
                group = text
            continue
        cells = list(r)
        widths = [c["span"] for c in cells]
        # Data rows: [category?] use(span 2) value*n [standards?]; the standards
        # cell is sometimes shared across rows (rowspan) and so missing.
        if len(cells) == n + 3:
            category = cells[0]["text"]
            cells = cells[1:]
        elif len(cells) == n + 1:
            cells = cells + [{"text": "", "span": 1, "links": []}]
        if len(cells) != n + 2 or widths[-1] != 1:
            continue
        values = [c["text"] for c in cells[1:1 + n]]
        if not all(v in ("P", "SE", "X", "A", "SE*", "P*", "") for v in values):
            continue  # a header row
        std = cells[-1]
        uses.append({
            "use": cells[0]["text"],
            "group": group,
            "category": category,
            "table": label,
            "zones": dict(zip(zones, values)),
            "standards_text": std["text"],
            "standards": [
                {"text": clean(l["text"]), "url": absolute(l["href"])}
                for l in std["links"] if clean(l["text"])
            ],
        })
    return uses


def amended_through(html: str) -> str | None:
    """The last bill in the section's editor's-note history, e.g. 'CB-070-2026'."""
    bills = re.findall(r"\b(C[BR]-\d{3}-\d{4})\b", html)
    return bills[-1] if bills else None


def main() -> int:
    resp = httpx.get(FETCH_URL, timeout=60, headers={"User-Agent": "Mozilla/5.0"}, follow_redirects=True)
    resp.raise_for_status()
    html = resp.text
    if "27-5101. Principal Use Tables" not in html:
        print("The page did not contain Section 27-5101; refusing to write.", file=sys.stderr)
        return 1

    parser = TableParser()
    parser.feed(html)
    missing = sorted(set(TABLES) - set(parser.tables))
    if missing:
        print(f"Tables not found on the page: {missing}", file=sys.stderr)
        return 1

    uses = []
    for table_id, rows in parser.tables.items():
        parsed = parse_table(table_id, rows)
        print(f"{TABLES[table_id][0]}: {len(parsed)} uses")
        uses.extend(parsed)

    doc = {
        "source": {
            "name": "Prince George's County Zoning Ordinance, Section 27-5101 Principal Use Tables",
            "url": CITE_URL,
            "standards_url": BASE + "doc-viewer.aspx#secid-650",
            "special_exception_url": BASE + "doc-viewer.aspx#secid-608",
            "amended_through": amended_through(html),
            "extracted": date.today().isoformat(),
            "extracted_by": "scripts/extract_use_table.py",
        },
        "key": {
            "P": "Permitted by right, subject to the ordinance's other regulations.",
            "SE": "Permitted only on approval of a special exception (Section 27-3604).",
            "X": "Prohibited.",
        },
        "unlisted_rule": "All uses not listed are prohibited. (Section 27-5101(a)(1))",
        "tables": {label: {"zones": zones} for label, zones in TABLES.values()},
        "uses": uses,
    }
    OUT.write_text(json.dumps(doc, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Wrote {len(uses)} rows to {OUT.relative_to(ROOT)} (current through {doc['source']['amended_through']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
