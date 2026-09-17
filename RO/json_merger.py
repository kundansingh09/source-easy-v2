import json

# Adjust paths to match your local master database file
MASTER_JSON = "/Users/kundansingh/source-easy-v2/data/semi_suppliers.json" 
INDIA_JSON = "/Users/kundansingh/source-easy-v2/RO/india_suppliers.json"
OUTPUT_JSON = "/Users/kundansingh/source-easy-v2/data/final_combined_suppliers.json"

def main():
    with open(MASTER_JSON, "r", encoding="utf-8") as f:
        master_data = json.load(f)
        
    with open(INDIA_JSON, "r", encoding="utf-8") as f:
        india_data = json.load(f)

    # Dictionary to deduplicate by normalized name
    merged_db = {}
    
    # 1. Load existing master records
    for record in master_data:
        name_key = record["company_name"].strip().lower()
        merged_db[name_key] = record

    # 2. Merge or append India records
    for record in india_data:
        name_key = record["company_name"].strip().lower()
        
        if name_key in merged_db:
            # Company exists! Merge the expos and sources.
            existing = merged_db[name_key]
            
            for loc in record.get("locations", []):
                if loc not in existing.get("locations", []):
                    existing["locations"].append(loc)
                    
            # Combine the eBooth source links
            existing["sources"].extend(record.get("sources", []))
            
        else:
            # New company! Add them to the master database.
            merged_db[name_key] = record

    # 3. Export the final canonical database
    final_list = list(merged_db.values())
    
    with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
        json.dump(final_list, f, indent=2, ensure_ascii=False)
        
    print(f"Merged database created with {len(final_list)} unique suppliers.")

if __name__ == "__main__":
    main()