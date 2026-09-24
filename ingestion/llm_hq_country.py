import json
import os
from openai import OpenAI

# Automatically reads the OPENAI_API_KEY environment variable you exported
client = OpenAI()

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
INPUT_JSON = os.path.join(BASE_DIR, "data", "Translate", "japan2025_final_translated.json")
OUTPUT_JSON = os.path.join(BASE_DIR, "data", "final", "japan2025_standardized.json")
BATCH_SIZE = 50

def extract_countries_batched(locations_batch):
    """
    Sends a batch of location strings to gpt-4o-mini and returns a mapping.
    """
    prompt = f"""
    Given this list of raw headquarters location strings from corporate profiles, identify the primary standardized country for each.
    If the location is 'Hong Kong' or 'Taiwan', return 'Hong Kong' or 'Taiwan' as the country entity.
    If undetermined or empty, return null.

    Locations:
    {json.dumps(locations_batch, ensure_ascii=False)}

    Return ONLY a JSON object mapping each raw string to its standardized country name:
    {{
      "Kaohsiung City, Taiwan": "Taiwan",
      "Hong Kong": "Hong Kong",
      "Wonju-si, Gangwon-state, Korea (South)": "South Korea",
      "京都市, 京都府, Japan": "Japan"
    }}
    """

    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": "You are a precise geographic entity extraction engine. Output strictly valid JSON."},
            {"role": "user", "content": prompt}
        ],
        response_format={"type": "json_object"},
        temperature=0.0
    )
    return json.loads(response.choices[0].message.content)

def add_hq_country():
    # Read with utf-8-sig to handle hidden BOM characters
    with open(INPUT_JSON, "r", encoding="utf-8-sig") as f:
        records = json.load(f)

    # 1. Gather all unique non-empty hq_location values
    unique_locations = list({
        r.get("hq_location").strip()
        for r in records 
        if r.get("hq_location") and str(r.get("hq_location")).strip()
    })

    print(f"Total records: {len(records)}")
    print(f"Unique locations to resolve: {len(unique_locations)}")

    # 2. Batch-resolve locations with gpt-4o-mini
    location_to_country = {}
    for i in range(0, len(unique_locations), BATCH_SIZE):
        batch = unique_locations[i:i + BATCH_SIZE]
        print(f"Resolving batch {i // BATCH_SIZE + 1} ({len(batch)} locations)...")
        resolved_batch = extract_countries_batched(batch)
        location_to_country.update(resolved_batch)

    # 3. Inject only the hq_country tag
    for r in records:
        loc = (r.get("hq_location") or "").strip()
        r["hq_country"] = location_to_country.get(loc, None)

    # 4. Save standardized dataset ensuring native characters are kept intact
    with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
        json.dump(records, f, indent=4, ensure_ascii=False)

    print(f"\nCompleted! Standardized file saved to: {OUTPUT_JSON}")

if __name__ == "__main__":
    add_hq_country()