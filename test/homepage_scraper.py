import json
import re
import time
import requests
import trafilatura
from bs4 import BeautifulSoup
import urllib3

# Suppress warnings for websites with expired/invalid SSL certificates
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ----------------- CONFIGURATION -----------------
INPUT_FILE  = "/Users/kundansingh/source-easy-v2/test/japan2025_en_merged.json"  # Change this to your input file
OUTPUT_FILE = "japan2025_with_homepages.json"
FAILURES_FILE = "homepage_scrape_failures.csv"

# Set to 10 for testing; set to None to run all records
TEST_LIMIT  = 10

MAX_RETRIES = 2
TIMEOUT_SECS = 15

# Two user agents to rotate through on retry — some sites block a single
# repeated UA as a bot signature.
USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
]

SESSION = requests.Session()
SESSION.headers.update({
    "Accept-Language": "en-US,en;q=0.9,ja;q=0.8,zh;q=0.7",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
})

# ----------------- EXTRACTION LOGIC -----------------

def bs_extract(raw_bytes):
    """Fallback extraction for corporate homepages that lack standard article tags.
    Takes RAW BYTES (not pre-decoded text) so BeautifulSoup's own encoding
    detection (UnicodeDammit) can correctly handle Shift_JIS / EUC-JP / UTF-8
    pages regardless of what (or whether) the HTTP header declared."""
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
    """Heuristic: a page that is almost entirely a script-loader shell
    (e.g. React/Vue/Angular apps) will have very little text outside
    <script> tags. Used only to label WHY an extraction came back empty,
    not to change behavior — a plain requests.get() can't execute JS
    regardless, so this just makes the failure reason accurate."""
    soup = BeautifulSoup(raw_bytes, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    visible_text = soup.get_text(strip=True)
    return len(visible_text) < 150


def scrape_homepage(url):
    """Fetches and extracts the full homepage content.
    Returns (content, failure_reason). failure_reason is None on success."""
    if not url.startswith("http"):
        url = "https://" + url  # https default — most JP corporate sites enforce it anyway

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
                return "", f"non_html_content_type:{content_type.split(';')[0]}"

            raw_bytes = r.content  # RAW BYTES — never r.text. This is the actual fix:
            # requests defaults to ISO-8859-1 when a server's Content-Type header
            # omits a charset (very common on older JP sites), which silently
            # mangles Shift_JIS / EUC-JP / UTF-8 bodies into mojibake. Feeding
            # raw bytes to trafilatura / BeautifulSoup lets THEM sniff the real
            # encoding from the <meta charset> tag or byte patterns instead.

            # 1. Try Trafilatura first (favor recall to grab as much as possible)
            text = trafilatura.extract(
                raw_bytes,
                include_comments=False,
                include_tables=True,
                no_fallback=False,
                favor_recall=True,
            )

            # 2. If Trafilatura fails or thinks it's not an article (<300 chars), use BeautifulSoup
            if not text or len(text) < 300:
                text = bs_extract(raw_bytes)

            if text:
                return text, None

            reason = "likely_js_rendered_spa" if looks_like_js_spa(raw_bytes) else "empty_after_extraction"
            return "", reason

        except requests.exceptions.SSLError as e:
            last_error = f"ssl_error:{str(e)[:60]}"
        except requests.exceptions.Timeout:
            last_error = "timeout"
        except requests.exceptions.TooManyRedirects:
            last_error = "too_many_redirects"
            break  # retrying won't help
        except requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response is not None else "?"
            last_error = f"http_error:{status}"
            if status in (403, 401, 429):
                break  # a fresh UA on retry rarely fixes an explicit block; don't waste a retry
        except requests.exceptions.ConnectionError:
            last_error = "connection_error"
        except Exception as e:
            last_error = f"other:{type(e).__name__}"

        if attempt < MAX_RETRIES:
            time.sleep(1.5 * (attempt + 1))  # backoff before retry

    return "", last_error or "unknown_failure"

# ----------------- EXECUTION & REPORTING -----------------

def run():
    print(f"Loading {INPUT_FILE}...")
    with open(INPUT_FILE, "r", encoding="utf-8") as f:
        records = json.load(f)

    if TEST_LIMIT is not None:
        records = records[:TEST_LIMIT]
        print(f"Test mode active: processing only first {len(records)} records.\n")

    metrics = {"total": len(records), "success": 0, "failed": 0, "no_url": 0}
    failure_log = []          # (company_name, url, reason)
    failure_reason_counts = {}

    for i, r in enumerate(records):
        website = r.get("website")
        if website:
            website = str(website).strip()

        if website and website != "null":
            content, reason = scrape_homepage(website)

            if content:
                status = f"✅ Success ({len(content)} chars)"
                metrics["success"] += 1
            else:
                status = f"❌ Failed ({reason})"
                metrics["failed"] += 1
                failure_log.append((r.get("company_name", ""), website, reason))
                failure_reason_counts[reason] = failure_reason_counts.get(reason, 0) + 1

            time.sleep(0.5)  # polite delay
        else:
            content = ""
            status = "⚠️ No Website Provided"
            metrics["no_url"] += 1

        r["website_scrape"] = content

        company_name = str(r.get("company_name", "Unknown"))[:35]
        print(f"[{i+1}/{metrics['total']}] {company_name.ljust(35)} | {status}")

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(records, f, indent=4, ensure_ascii=False)

    if failure_log:
        import csv
        with open(FAILURES_FILE, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["Company Name", "Website", "Failure Reason"])
            writer.writerows(failure_log)

    print("\n" + "=" * 45)
    print(" SCRAPING TASK SUMMARY REPORT")
    print("=" * 45)
    print(f" Total Records Processed : {metrics['total']}")
    print(f" Successfully Scraped    : {metrics['success']}")
    print(f" Failed / Blocked        : {metrics['failed']}")
    print(f" No Website Provided     : {metrics['no_url']}")
    if failure_reason_counts:
        print("\n Failure reason breakdown:")
        for reason, count in sorted(failure_reason_counts.items(), key=lambda x: -x[1]):
            print(f"   {reason:<30} {count}")
    print("=" * 45)
    print(f" Output saved to: {OUTPUT_FILE}")
    if failure_log:
        print(f" Failure details: {FAILURES_FILE}")

if __name__ == "__main__":
    run()