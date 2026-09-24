import json
import os
import re

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
LANG1_FILE = os.path.join(BASE_DIR, "test", "china2026_lang1.json")  # English (Base)
LANG2_FILE = os.path.join(BASE_DIR, "test", "china2026_lang2.json")  # Chinese (Supplemental)
OUTPUT_FILE = os.path.join(BASE_DIR, "test", "china2026_merged.json")
REPORT_FILE = os.path.join(BASE_DIR, "test", "china2026_merge_report.txt")

def load_json(filepath):
    if not os.path.exists(filepath):
        print(f"File not found: {filepath}")
        return []
    with open(filepath, "r", encoding="utf-8") as f:
        return json.load(f)

def extract_booth_id(url):
    """Extracts the unique numeric BoothID from the eBooth URL."""
    if not url:
        return None
    match = re.search(r"BoothID=(\d+)", str(url), re.IGNORECASE)
    return match.group(1) if match else None

def is_empty(val):
    """Checks if a field is null, empty string, or whitespace-only."""
    if val is None:
        return True
    if isinstance(val, str) and not val.strip():
        return True
    if isinstance(val, list) and len(val) == 0:
        return True
    return False

def merge_passes():
    lang1 = load_json(LANG1_FILE)
    lang2 = load_json(LANG2_FILE)

    # Index lang2 by Booth ID (primary), with company_name as fallback
    lang2_by_booth = {}
    lang2_by_name = {}

    for r in lang2:
        bid = extract_booth_id(r.get("ebooth_url"))
        if bid:
            lang2_by_booth[bid] = r
        name = r.get("company_name")
        if name:
            lang2_by_name[name.strip()] = r

    merged = []
    matched_count = 0
    backfilled_about = []
    backfilled_website = []
    backfilled_cats = []

    for r1 in lang1:
        bid = extract_booth_id(r1.get("ebooth_url"))
        name = (r1.get("company_name") or "").strip()

        # Match by BoothID first; fallback to company name
        r2 = lang2_by_booth.get(bid) or lang2_by_name.get(name)

        if r2:
            matched_count += 1

            # Backfill 'about'
            if is_empty(r1.get("about")) and not is_empty(r2.get("about")):
                r1["about"] = r2["about"]
                backfilled_about.append((name, bid))

            # Backfill 'website'
            if is_empty(r1.get("website")) and not is_empty(r2.get("website")):
                r1["website"] = r2["website"]
                backfilled_website.append((name, bid))

            # Backfill categories
            if is_empty(r1.get("cat_l1_ids")) and not is_empty(r2.get("cat_l1_ids")):
                r1["cat_l1_ids"] = r2.get("cat_l1_ids", [])
                r1["cat_l1_names"] = r2.get("cat_l1_names", [])
                r1["cat_l2_ids"] = r2.get("cat_l2_ids", [])
                r1["cat_l2_names"] = r2.get("cat_l2_names", [])
                r1["cat_tree"] = r2.get("cat_tree", [])
                backfilled_cats.append((name, bid))

            # lang1 hq_location is strictly preserved

        merged.append(r1)

    # 1. Write the merged JSON
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(merged, f, indent=4, ensure_ascii=False)

    # 2. Write the Audit Report
    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        f.write("=========================================\n")
        f.write(" CHINA 2026 MERGE REPORT (BOOTH-ID MATCH)\n")
        f.write("=========================================\n\n")
        f.write(f"Total Base Records (lang1) : {len(merged)}\n")
        f.write(f"Supp Records Indexed (lang2): {len(lang2)}\n")
        f.write(f"Successfully Matched Pairs : {matched_count}/{len(merged)}\n\n")

        f.write("--- SUMMARY OF GAINS ---\n")
        f.write(f"Overviews (About) backfilled : {len(backfilled_about)}\n")
        f.write(f"Categories backfilled        : {len(backfilled_cats)}\n")
        f.write(f"Websites backfilled          : {len(backfilled_website)}\n\n")

        if backfilled_about:
            f.write("--- OVERVIEWS BACKFILLED FROM CHINESE PASS ---\n")
            for name, bid in backfilled_about:
                f.write(f" - [BoothID {bid}] {name}\n")
            f.write("\n")

        if backfilled_cats:
            f.write("--- CATEGORIES BACKFILLED FROM CHINESE PASS ---\n")
            for name, bid in backfilled_cats:
                f.write(f" - [BoothID {bid}] {name}\n")
            f.write("\n")

        if backfilled_website:
            f.write("--- WEBSITES BACKFILLED FROM CHINESE PASS ---\n")
            for name, bid in backfilled_website:
                f.write(f" - [BoothID {bid}] {name}\n")
            f.write("\n")

    print(f"Merge complete: {len(merged)} records saved to {OUTPUT_FILE}")
    print(f"Matched {matched_count}/{len(merged)} companies by Booth ID.")
    print(f"Gains: +{len(backfilled_about)} abouts | +{len(backfilled_cats)} category trees | +{len(backfilled_website)} websites")
    print(f"Report written to: {REPORT_FILE}")

if __name__ == "__main__":
    merge_passes()