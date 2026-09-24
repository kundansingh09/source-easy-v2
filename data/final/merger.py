import json
import os
import glob
import sys

# ----------------- CONFIGURATION -----------------
# OPTION 1: Set an explicit directory path where all your JSON files are stored
BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
INPUT_DIR = os.path.join(BASE_DIR, "data", "final")

# OPTION 2: If you want to explicitly name specific files, put them here.
# (Leave empty [] if you want it to scan INPUT_DIR automatically)
EXPLICIT_FILES = [
    # os.path.join(BASE_DIR, "data", "Translate", "china2026_standardized.json"),
    # os.path.join(BASE_DIR, "data", "Translate", "japan2025_final_translated.json"),
    # os.path.join(BASE_DIR, "data", "Translate", "taiwan2026.json"),
    # os.path.join(BASE_DIR, "data", "Translate", "europa2025_with_homepages.json"),
    # os.path.join(BASE_DIR, "data", "Translate", "india_suppliers_with_homepages.json"),
]

OUTPUT_FILE = os.path.join(BASE_DIR, "data", "global_merged_dataset-3.json")

# Enforce the strict Taiwan schema
TAIWAN_SCHEMA = {
    "company_name": "",
    "location": "",
    "ebooth_url": "",
    "about": "",
    "hq_location": "",
    "hq_country": "",
    "website": "",
    "cat_l1_ids": [],
    "cat_l1_names": [],
    "cat_l2_ids": [],
    "cat_l2_names": [],
    "cat_tree": [],
    "website_scrape": ""
}

def get_target_files():
    """Resolves whether to use explicit paths or a folder path."""
    if EXPLICIT_FILES:
        return [f for f in EXPLICIT_FILES if os.path.isfile(f)]
    
    if os.path.isdir(INPUT_DIR):
        # Finds all JSON files inside INPUT_DIR
        return sorted(glob.glob(os.path.join(INPUT_DIR, "*.json")))
    
    return []

def normalize_and_merge():
    target_files = get_target_files()

    # Prevent reading output file if it sits in the same directory
    abs_output = os.path.abspath(OUTPUT_FILE)
    target_files = [f for f in target_files if os.path.abspath(f) != abs_output]

    if not target_files:
        print(f"Error: No JSON files found in {INPUT_DIR} or EXPLICIT_FILES.")
        sys.exit(1)

    print(f"Found {len(target_files)} files to merge:")
    for f in target_files:
        print(f" -> {f}")
    print()

    merged_data = []
    analysis_report = {}

    for file_path in target_files:
        filename = os.path.basename(file_path)
        with open(file_path, "r", encoding="utf-8-sig") as f:
            try:
                records = json.load(f)
            except json.JSONDecodeError:
                print(f"[!] Error decoding {filename}. Skipping.")
                continue

        analysis_report[filename] = {
            "total_records": len(records),
            "dropped_fields": set(),
            "missing_fields": set()
        }

        for record in records:
            # 1. Flatten India's nested arrays to match Taiwan's flat strings
            if "ebooth_url" not in record and "sources" in record and isinstance(record["sources"], list) and len(record["sources"]) > 0:
                record["ebooth_url"] = record["sources"][0].get("ebooth_url", "")

            if "location" not in record and "locations" in record and isinstance(record["locations"], list) and len(record["locations"]) > 0:
                record["location"] = record["locations"][0]

            # 2. Track schema discrepancies
            record_keys = set(record.keys())
            schema_keys = set(TAIWAN_SCHEMA.keys())

            dropped = record_keys - schema_keys
            missing = schema_keys - record_keys

            analysis_report[filename]["dropped_fields"].update(dropped)
            analysis_report[filename]["missing_fields"].update(missing)

            # 3. Standardize keys and values
            standardized_record = {}
            for key, default_val in TAIWAN_SCHEMA.items():
                val = record.get(key)
                if val is None:
                    # Provide an empty list copy or default string
                    standardized_record[key] = list(default_val) if isinstance(default_val, list) else default_val
                else:
                    standardized_record[key] = val

            merged_data.append(standardized_record)

    # 4. Save output
    os.makedirs(os.path.dirname(abs_output), exist_ok=True)
    with open(abs_output, "w", encoding="utf-8") as f:
        json.dump(merged_data, f, indent=4, ensure_ascii=False)

    # 5. Summary Printout
    print("=" * 60)
    print("MERGE ANALYSIS REPORT")
    print("=" * 60)
    print(f"Total merged records: {len(merged_data)}")
    print(f"Output saved to: {abs_output}\n")

    for filename, stats in analysis_report.items():
        print(f"File: {filename} ({stats['total_records']} records)")
        dropped_str = ", ".join(sorted(stats["dropped_fields"])) if stats["dropped_fields"] else "None"
        missing_str = ", ".join(sorted(stats["missing_fields"])) if stats["missing_fields"] else "None"
        print(f"  [-] Extra fields dropped: {dropped_str}")
        print(f"  [!] Missing fields patched: {missing_str}")
        print("-" * 60)

if __name__ == "__main__":
    normalize_and_merge()