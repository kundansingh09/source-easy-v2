#!/usr/bin/env python3
import json
import os

# Adjust this path if your folder is named something else (like just 'out_llm')
FILE_PATH = "out_llm_full/refined_overviews.json"

def main():
    if not os.path.exists(FILE_PATH):
        print(f"Error: Could not find {FILE_PATH}")
        return

    # Load the existing data
    with open(FILE_PATH, 'r', encoding='utf-8') as f:
        data = json.load(f)

    # Strip the 'model' key from every record
    count = 0
    for record in data:
        if "model" in record:
            del record["model"]
            count += 1

    # Overwrite the file with the cleaned data
    with open(FILE_PATH, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    print(f"Success! Removed 'model' key from {count} records in {FILE_PATH}")

if __name__ == "__main__":
    main()