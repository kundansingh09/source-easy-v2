import json
import csv
import os

# ----------------- CONFIGURATION -----------------
BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
JSON_INPUT = os.path.join(BASE_DIR, "data", "Translate", "japan2025_translate.json")      # Your original full JSON file
CSV_INPUT = os.path.join(BASE_DIR, "data", "Translate", "japan2025_translate.csv")            # Your Google Sheets translated CSV
JSON_OUTPUT = os.path.join(BASE_DIR, "data", "final", "japan2025_final_translated.json")   # The new output file

def merge_translations():
    if not os.path.exists(JSON_INPUT) or not os.path.exists(CSV_INPUT):
        print("Error: Input files not found. Please check your file names.")
        return

    # 1. Load the translated fields from the CSV into a dictionary keyed by company name
    translated_data = {}
    with open(CSV_INPUT, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            name = row.get("company_name", "").strip()
            if name:
                translated_data[name] = {
                    "about": row.get("about", ""),
                    "hq_location": row.get("hq_location", ""),
                    "website_scrape": row.get("website_scrape", "")
                }

    # 2. Load the original complete JSON
    # CHANGED: Using utf-8-sig here to safely ignore the BOM
    with open(JSON_INPUT, "r", encoding="utf-8-sig") as f:
        records = json.load(f)

    # 3. Inject the translated fields back into the JSON records
    updated_count = 0
    for r in records:
        name = r.get("company_name", "").strip()
        
        if name in translated_data:
            r["about"] = translated_data[name]["about"]
            r["hq_location"] = translated_data[name]["hq_location"]
            r["website_scrape"] = translated_data[name]["website_scrape"]
            updated_count += 1

    # 4. Save the final merged JSON
    with open(JSON_OUTPUT, "w", encoding="utf-8") as f:
        json.dump(records, f, indent=4, ensure_ascii=False)

    print(f"Merge complete!")
    print(f"Updated {updated_count}/{len(records)} records with translated text.")
    print(f"Saved final dataset to: {JSON_OUTPUT}")

if __name__ == "__main__":
    merge_translations()