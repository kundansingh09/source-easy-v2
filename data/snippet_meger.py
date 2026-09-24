import json
import csv
import re

import os

# ----------------- CONFIGURATION -----------------
BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
JSON_INPUT  = os.path.join(BASE_DIR, "data", "dedupe", "global_deduped-3.json")
CSV_INPUT   = os.path.join(BASE_DIR, "data", "raw", "snippets.csv")
JSON_OUTPUT = os.path.join(BASE_DIR, "data", "dedupe", "global_deduped_with_snippets-3.json")

def normalise(s):
    """Uppercase + collapse whitespace — handles 'VON ARDENNE GmbH' vs
    'VON ARDENNE GMBH', extra spaces, etc."""
    return re.sub(r'\s+', ' ', s.upper().strip())

def get_snippet(row):
    """The CSV has 72 rows where the snippet text landed in the 'website'
    column (the row is shifted left by one). Detect and recover those."""
    website = row.get('website', '').strip()
    snippet = row.get('snippet', '').strip()
    if website and not website.startswith('http'):
        return website   # shifted row: text is in the website column
    return snippet       # normal row

def merge():
    # 1. Load CSV into a lookup: normalised_company -> snippet text
    csv_lookup = {}
    with open(CSV_INPUT, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        print(f"CSV columns: {reader.fieldnames}")

        company_col = next(
            (c for c in (reader.fieldnames or []) if c.strip().lower() == "company"),
            None
        )
        if not company_col:
            print("ERROR: no 'company' column found in CSV.")
            print(f"Found: {reader.fieldnames}")
            return

        for row in reader:
            company = row.get(company_col, "").strip()
            if not company:
                continue
            snip = get_snippet(row)
            csv_lookup[normalise(company)] = snip

    print(f"CSV unique companies loaded : {len(csv_lookup)}")

    # 2. Load JSON
    with open(JSON_INPUT, "r", encoding="utf-8") as f:
        records = json.load(f)
    print(f"JSON records loaded        : {len(records)}")

    # 3. Inject snippet field
    matched_with_text = 0
    matched_empty     = 0
    unmatched         = 0

    for r in records:
        key = normalise(r.get("company_name", ""))
        if key in csv_lookup:
            snippet_val = csv_lookup[key]
            r["snippet"] = snippet_val
            if snippet_val.strip():
                matched_with_text += 1
            else:
                matched_empty += 1
        else:
            r["snippet"] = ""
            unmatched += 1

    # 4. Save
    with open(JSON_OUTPUT, "w", encoding="utf-8") as f:
        json.dump(records, f, indent=2, ensure_ascii=False)

    print("=" * 55)
    print(" MERGE COMPLETE")
    print("=" * 55)
    print(f"Total JSON records            : {len(records)}")
    print(f"Matched + have snippet text   : {matched_with_text}")
    print(f"Matched + snippet was empty   : {matched_empty}  (website scrape failed for those)")
    print(f"Not in CSV at all             : {unmatched}  (snippet = '')")
    print(f"")
    print(f"Note: CSV had {len(csv_lookup)} companies total.")
    print(f"      {len(csv_lookup) - matched_with_text - matched_empty} CSV companies")
    print(f"      don't exist in the JSON yet (different pipeline / not deduped in).")
    print(f"")
    print(f"Output saved to: {JSON_OUTPUT}")

if __name__ == "__main__":
    merge()