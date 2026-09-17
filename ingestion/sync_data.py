import os
import shutil
import subprocess

OLD_RAW_DIR = "../source-easy/data/raw"
NEW_RAW_DIR = "data/raw"

def sync_and_build():
    print("1. Fetching raw data from the old folder...")
    
    # Ensure the target directory exists
    os.makedirs(NEW_RAW_DIR, exist_ok=True)
    
    # Safely copy every JSON file from the old raw directory
    copied_count = 0
    if os.path.exists(OLD_RAW_DIR):
        for filename in os.listdir(OLD_RAW_DIR):
            if filename.endswith(".json"):
                src = os.path.join(OLD_RAW_DIR, filename)
                dst = os.path.join(NEW_RAW_DIR, filename)
                shutil.copy2(src, dst)
                print(f"  -> Copied {filename}")
                copied_count += 1
    else:
        print(f"Error: Could not find old directory at {OLD_RAW_DIR}")
        return

    print(f"\nSuccessfully copied {copied_count} files.")
    
    # Run the deduplication script to merge them into semi_suppliers.json
    print("\n2. Running deduplication on the fetched data...")
    try:
        subprocess.run(["python", "ingestion/dedupe.py"], check=True)
        print("\nData sync and deduplication complete! Your dataset is ready.")
    except subprocess.CalledProcessError as e:
        print(f"\nError running dedupe.py: {e}")

if __name__ == "__main__":
    sync_and_build()