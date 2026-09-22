# --------------------------------------------------------------------------
# KAGGLE — Multi-Show Homepage Scraper & Failure Analysis
# Combines sequential multi-file processing with byte-level extraction
# --------------------------------------------------------------------------

import subprocess, sys

# Ensure dependencies are available in the Kaggle kernel
subprocess.run([sys.executable, "-m", "pip", "install", "-q", "trafilatura", "beautifulsoup4"], check=True)

import json
import csv
import os
import re
import time
import requests
import trafilatura
from bs4 import BeautifulSoup
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ----------------- CONFIGURATION -----------------
import glob

# Automatically finds all JSON files no matter how Kaggle nested the folders
INPUT_FILES = sorted(glob.glob("/kaggle/input/**/*.json", recursive=True))

OUTPUT_DIR = "/kaggle/working"
os.makedirs(OUTPUT_DIR, exist_ok=True)

# Set to None to run all 5,000+ companies. 
TEST_LIMIT = None  
MAX_RETRIES = 2
TIMEOUT_SECS = 15

# Rotating User-Agents to prevent bot blocking
USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
]

SESSION = requests.Session()
SESSION.headers.update({
    "Accept-Language": "en-US,en;q=0.9,ja;q=0.8,zh-CN;q=0.8,zh-TW;q=0.8,zh;q=0.7,ko;q=0.7",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
})

# ----------------- EXTRACTION LOGIC -----------------

def bs_extract(raw_bytes):
    """Fallback extraction taking RAW BYTES to allow native charset detection."""
    soup = BeautifulSoup(raw_bytes, "html.parser")
    for tag in soup(["script", "style", "nav", "header", "footer", "aside", "noscript", "svg", "canvas", "form"]):
        tag.decompose()

    main = (soup.find("main") or
            soup.find("article") or
            soup.find(id=re.compile(r"content|main|wrap|page", re.I)) or
            soup.find(class_=re.compile(r"content|main|wrap|page", re.I)) or
            soup.body)

    if not main:
        return ""

    text = main.get_text(separator="\n", strip=True)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()

def looks_like_js_spa(raw_bytes):
    """Detects React/Vue shells that return empty HTML to plain HTTP requests."""
    soup = BeautifulSoup(raw_bytes, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    return len(soup.get_text(strip=True)) < 150

def scrape_homepage(url):
    """Returns (content, status, char_count). status is 'success' or an error reason."""
    if not url or url.strip() in ("", "null", "None"):
        return "", "no_url", 0

    url = url.strip()
    if not url.startswith("http"):
        url = "https://" + url  # Default to HTTPS

    last_error = None
    for attempt in range(MAX_RETRIES + 1):
        ua = USER_AGENTS[attempt % len(USER_AGENTS)]
        try:
            r = SESSION.get(
                url, timeout=TIMEOUT_SECS, allow_redirects=True,
                verify=False, headers={"User-Agent": ua},
            )
            r.raise_for_status()

            content_type = r.headers.get("Content-Type", "").lower()
            if "text/html" not in content_type and "xml" not in content_type and content_type != "":
                return "", f"non_html_content_type:{content_type.split(';')[0]}", 0

            raw_bytes = r.content

            # 1. Trafilatura
            text = trafilatura.extract(
                raw_bytes,
                include_comments=False,
                include_tables=True,
                no_fallback=False,
                favor_recall=True,
            )

            # 2. BeautifulSoup Fallback
            if not text or len(text) < 300:
                text = bs_extract(raw_bytes)

            if text:
                return text, "success", len(text)

            reason = "likely_js_rendered_spa" if looks_like_js_spa(raw_bytes) else "empty_after_extraction"
            return "", reason, 0

        except requests.exceptions.SSLError as e:
            last_error = f"ssl_error:{str(e)[:60]}"
        except requests.exceptions.Timeout:
            last_error = "timeout"
        except requests.exceptions.TooManyRedirects:
            last_error = "too_many_redirects"
            break
        except requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response is not None else "?"
            last_error = f"http_error:{status}"
            if status in (403, 401, 429):
                break  # Don't retry explicit WAF blocks
        except requests.exceptions.ConnectionError:
            last_error = "connection_error"
        except Exception as e:
            last_error = f"other:{type(e).__name__}"

        if attempt < MAX_RETRIES:
            time.sleep(1.5 * (attempt + 1))  # Backoff before retry

    return "", last_error or "unknown_failure", 0

# ----------------- MAIN PROCESSING PIPELINE -----------------

def run_pipeline():
    summary_metrics = []
    failure_audit = []

    print(f"Starting ingestion across {len(INPUT_FILES)} target files...\n")

    for file_idx, in_path in enumerate(INPUT_FILES, start=1):
        if not os.path.exists(in_path):
            print(f"[{file_idx}/{len(INPUT_FILES)}] Skipping {in_path}: File not found.")
            continue

        base_name = os.path.splitext(os.path.basename(in_path))[0]
        out_json = os.path.join(OUTPUT_DIR, f"{base_name}_with_homepages.json")

        print("=" * 60)
        print(f"[{file_idx}/{len(INPUT_FILES)}] Processing: {base_name}")
        print("=" * 60)

        with open(in_path, "r", encoding="utf-8") as f:
            records = json.load(f)

        if TEST_LIMIT is not None:
            records = records[:TEST_LIMIT]
            print(f"Running test mode: capped at {len(records)} records.")

        file_stats = {
            "file": base_name, "total": len(records), "success": 0,
            "failed": 0, "no_url": 0, "total_chars": 0, "errors": {}
        }

        for idx, r in enumerate(records, start=1):
            website = r.get("website")
            company_name = str(r.get("company_name", "Unknown"))
            
            content, status, char_count = scrape_homepage(website)
            r["website_scrape"] = content

            if status == "success":
                file_stats["success"] += 1
                file_stats["total_chars"] += char_count
                status_log = f"✅ Success ({char_count:,} chars)"
            elif status == "no_url":
                file_stats["no_url"] += 1
                status_log = "⚠️ No Website"
            else:
                file_stats["failed"] += 1
                file_stats["errors"][status] = file_stats["errors"].get(status, 0) + 1
                status_log = f"❌ {status}"
                
                failure_audit.append({
                    "show_file": base_name,
                    "company_name": company_name,
                    "website": website or "",
                    "failure_category": status
                })

            print(f"[{idx}/{len(records)}] {company_name[:32].ljust(32)} | {status_log}")
            time.sleep(0.4)

        with open(out_json, "w", encoding="utf-8") as f:
            json.dump(records, f, indent=4, ensure_ascii=False)
        print(f"\nSaved: {out_json}\n")

        total_attempted = file_stats["total"] - file_stats["no_url"]
        success_rate = (file_stats["success"] / total_attempted * 100) if total_attempted > 0 else 0
        avg_chars = (file_stats["total_chars"] / file_stats["success"]) if file_stats["success"] > 0 else 0

        summary_metrics.append({
            "Show": base_name,
            "Total Records": file_stats["total"],
            "Websites Present": total_attempted,
            "No Website": file_stats["no_url"],
            "Successful Scrapes": file_stats["success"],
            "Failed Scrapes": file_stats["failed"],
            "Success Rate (%)": round(success_rate, 2),
            "Avg Content Chars": int(avg_chars),
            "Error Breakdown": "; ".join([f"{k}: {v}" for k, v in file_stats["errors"].items()])
        })

    # ----------------- EXPORT ANALYSIS CSVS -----------------
    if summary_metrics:
        summary_csv = os.path.join(OUTPUT_DIR, "scraping_summary_overview.csv")
        with open(summary_csv, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=[
                "Show", "Total Records", "Websites Present", "No Website", 
                "Successful Scrapes", "Failed Scrapes", "Success Rate (%)", 
                "Avg Content Chars", "Error Breakdown"
            ])
            writer.writeheader()
            writer.writerows(summary_metrics)
        print(f"Summary Overview CSV : {summary_csv}")

    if failure_audit:
        failure_csv = os.path.join(OUTPUT_DIR, "scraping_failures_audit.csv")
        with open(failure_csv, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=["show_file", "company_name", "website", "failure_category"])
            writer.writeheader()
            writer.writerows(failure_audit)
        print(f"Detailed Failure CSV : {failure_csv}")

    print("\nAll tasks completed. Download files from the Kaggle Output tab.")

if __name__ == "__main__":
    run_pipeline()