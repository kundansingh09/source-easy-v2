import json
import csv
from openai import OpenAI
from concurrent.futures import ThreadPoolExecutor

client = OpenAI()
JSON_PATH = "india_suppliers.json"
CSV_PATH = "semicon.csv"

# 1. Load the raw, uncorrupted addresses from the original CSV
raw_addresses = {}
with open(CSV_PATH, "r", encoding="utf-8-sig") as f:
    reader = csv.DictReader(f)
    for row in reader:
        comp_name = (row.get("name") or row.get("Company Name") or "").strip()
        addr = (row.get("address") or "").strip()
        if comp_name:
            raw_addresses[comp_name] = addr

# 2. Load the JSON that needs fixing
with open(JSON_PATH, "r", encoding="utf-8") as f:
    suppliers = json.load(f)

def process_supplier(supplier):
    comp_name = supplier["company_name"]
    
    # Grab the pristine raw address directly from the CSV
    raw_address = raw_addresses.get(comp_name, "")
    
    if not raw_address:
        supplier["hq_location"] = "India"
        supplier["hq_country"] = "India"
        return supplier
        
    prompt = f"""
    Extract the clean City, State/Province (if applicable), and Country from this raw address string: "{raw_address}"
    
    RULES:
    1. Format strictly as JSON: {{"location": "City, State", "country": "Clean Country Name"}}
    2. If no state is present, just use "City" for the location.
    3. Translate any Japanese/Chinese/Korean text to English.
    4. Remove zip codes, websites, building names, and weird zone codes (like 33-TN or 27-MH).
    5. If the address is completely invalid, output location: "India", country: "India".
    """
    
    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            temperature=0
        )
        result = json.loads(response.choices[0].message.content)
        
        supplier["hq_location"] = result.get("location", "India")
        supplier["hq_country"] = result.get("country", "India")
        print(f"✅ Fixed: {comp_name} -> {supplier['hq_location']} | {supplier['hq_country']}")
        
    except Exception as e:
        print(f"❌ Failed on {comp_name}")
        
    return supplier

print("Extracting fresh locations from raw CSV using gpt-4o-mini...")

# Process 10 at a time to finish in seconds
with ThreadPoolExecutor(max_workers=10) as executor:
    cleaned_suppliers = list(executor.map(process_supplier, suppliers))

# Overwrite the JSON file with the perfect locations
with open(JSON_PATH, "w", encoding="utf-8") as f:
    json.dump(cleaned_suppliers, f, indent=2, ensure_ascii=False)

print("\nDone! Run your merge script and force_re-index.py to push to Qdrant.")