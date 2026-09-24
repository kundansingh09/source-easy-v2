import json
import csv
import os

# ----------------- CONFIGURATION -----------------
BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
INPUT_JSON = os.path.join(BASE_DIR, "data", "results (1)", "korea2026_with_homepages.json")
OUTPUT_CSV = os.path.join(BASE_DIR, "data", "Translate", "your_output_file.csv")

def json_to_minimal_csv(input_path, output_path):
    if not os.path.exists(input_path):
        print(f"Error: Input file not found at {input_path}")
        return

    headers = ["company_name", "about", "hq_location", "website_scrape"]
    print(f"Extracting {input_path} -> {output_path}...")

    # Using utf-8-sig to safely handle any hidden BOM characters
    with open(input_path, "r", encoding="utf-8-sig") as f:
        try:
            data = json.load(f)
        except json.JSONDecodeError:
            print(f"  [!] Error reading {input_path} - not valid JSON.")
            return

    if not data or not isinstance(data, list):
        print("  [!] Error: File is empty or not a JSON array.")
        return

    # Ensure the output directory exists in case you specify a new folder
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)

    # Write the minimal data using utf-8-sig for proper Excel Asian character display
    with open(output_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=headers)
        writer.writeheader()

        for row in data:
            writer.writerow({
                "company_name": row.get("company_name", ""),
                "about": row.get("about", ""),
                "hq_location": row.get("hq_location", ""),
                "website_scrape": row.get("website_scrape", "")
            })

    print("\nExtraction complete! Clean CSV has been generated.")

if __name__ == "__main__":
    json_to_minimal_csv(INPUT_JSON, OUTPUT_CSV)