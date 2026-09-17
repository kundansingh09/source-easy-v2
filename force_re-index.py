#!/usr/bin/env python3
"""
Force local re-embedding and upload of all suppliers to Qdrant Cloud.
"""

import os
import json
from qdrant_client import QdrantClient
from dotenv import load_dotenv

load_dotenv()

DATA_PATH = "data/final_combined_suppliers.json"
COLLECTION_NAME = "semicon_suppliers"

def main():
    if not os.path.exists(DATA_PATH):
        print(f"Error: {DATA_PATH} not found.")
        return

    with open(DATA_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    print(f"Found {len(data)} suppliers in {DATA_PATH}.")

    # 1. Connect to Qdrant Cloud and delete the old collection if present
    qdrant_url = os.getenv("QDRANT_URL")
    qdrant_api_key = os.getenv("QDRANT_API_KEY")

    if qdrant_url:
        client = QdrantClient(url=qdrant_url, api_key=qdrant_api_key)
        if client.collection_exists(COLLECTION_NAME):
            print(f"Deleting collection '{COLLECTION_NAME}' from Qdrant Cloud...")
            client.delete_collection(COLLECTION_NAME)
            print("Collection deleted.")

    # 2. Instantiating SourcingSearchEngine automatically builds vectors and uploads
    print("\nInitializing Search Engine (generating embeddings and uploading)...")
    from backend.search_engine import SourcingSearchEngine
    engine = SourcingSearchEngine(data_path=DATA_PATH)

    # 3. Verify total points uploaded
    count = engine.client.get_collection(engine.collection_name).points_count
    print(f"\nSuccess! Qdrant Cloud now contains {count} indexed suppliers.")

if __name__ == "__main__":
    main()