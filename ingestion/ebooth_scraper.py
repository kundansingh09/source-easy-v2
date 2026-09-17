import json
import os
import re
import time
import random
import urllib.parse

import requests
from bs4 import BeautifulSoup

# Only "taiwan2026" and "korea2026" are confirmed to exist at this URL pattern.
# The rest are guesses at the same pattern - verify before running at scale.
SHOW_SLUGS = {
    "Taiwan expo": "taiwan2026",
    "Korea expo": "korea2026",
    "China expo": "china2026",
    "Japan expo": "japan2026",
    "Europe expo": "europe2026",
    "India expo": "india2026",
}

FALLBACK_ABOUT = "Semiconductor technology and equipment supplier."

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.5",
}


def fetch_exhibitor_directory(session, show_slug):
    """Stage 1: one request gets every company name + real profile URL for a show."""
    url = f"https://expo.semi.org/{show_slug}/public/exhibitors.aspx"
    resp = session.get(url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    seen = set()
    exhibitors = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "eBooth.aspx" not in href or "IndexInList" not in href:
            continue
        name = a.get_text(strip=True)
        if not name or name in seen:
            continue
        seen.add(name)
        exhibitors.append(
            {"company_name": name, "ebooth_url": urllib.parse.urljoin(url, href)}
        )

    # Sanity check against the page's own "N Exhibitors" count, if present
    match = re.search(r"(\d+)\s*Exhibitors", resp.text)
    if match:
        expected = int(match.group(1))
        print(
            f"[{show_slug}] parsed {len(exhibitors)} companies "
            f"(page reports {expected} - {'OK' if abs(len(exhibitors) - expected) < 5 else 'MISMATCH, check for pagination'})"
        )
    else:
        print(f"[{show_slug}] parsed {len(exhibitors)} companies")

    return exhibitors


def extract_booth_description(soup):
    """Best-effort extraction of the profile's Overview text."""
    for selector in [
        "[id*='Description' i]",
        ".exhibitor-description",
        ".co-description",
    ]:
        node = soup.select_one(selector)
        if node:
            text = node.get_text(" ", strip=True)
            if len(text) > 40:
                return text

    # Fallback: text-window heuristic between "Booth: ####" and the
    # "Type your information and click..." boilerplate that appears on
    # every profile page, regardless of exact markup/class names.
    full_text = soup.get_text("\n", strip=True)
    start = re.search(r"Booth:\s*\S+", full_text)
    end_idx = full_text.find("Type your information and click")
    if start and end_idx != -1:
        window = full_text[start.end():end_idx]
        lines = [l.strip() for l in window.split("\n") if len(l.strip()) > 30]
        if lines:
            return " ".join(lines)

    return None


def extract_hq_location(soup, company_name):
    """Best-effort extraction of the company's real HQ city/country -
    NOT the expo location. Tries guessed ASP.NET label IDs first (unverified,
    cheap to try), then falls back to a text-position heuristic confirmed
    against live pages: the HQ text sits right after the <h1> company name
    and before the 'Booth: ####' line."""
    for city_sel, country_sel in [
        ("[id*='lblCity' i]", "[id*='lblCountry' i]"),
        (".BoothContactCity", ".BoothContactCountry"),
    ]:
        city = soup.select_one(city_sel)
        country = soup.select_one(country_sel)
        parts = [c.get_text(strip=True) for c in (city, country) if c and c.get_text(strip=True)]
        if parts:
            return ", ".join(dict.fromkeys(parts))  # dedupe while preserving order

    full_text = soup.get_text("\n", strip=True)
    idx = full_text.find(company_name)
    if idx == -1:
        return None
    after = full_text[idx + len(company_name):]
    lines = [l.strip() for l in after.split("\n") if l.strip()]
    location_parts = []
    for line in lines:
        low = line.lower()
        if low.startswith("http") or "www." in low or ".com" in low:
            continue
        if low.startswith("booth:") or "sendemail" in low.replace(" ", ""):
            break
        location_parts.append(line)
        if len(location_parts) >= 2:
            break
    return ", ".join(dict.fromkeys(location_parts)) if location_parts else None


def fetch_booth_details(session, url, company_name, max_retries=2):
    """Returns (about, hq_location)."""
    for attempt in range(max_retries):
        try:
            resp = session.get(url, headers=HEADERS, timeout=20)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "html.parser")
            about = extract_booth_description(soup) or FALLBACK_ABOUT
            hq_location = extract_hq_location(soup, company_name)
            return about, hq_location
        except Exception:
            if attempt == max_retries - 1:
                return FALLBACK_ABOUT, None
            time.sleep(2)
    return FALLBACK_ABOUT, None


def run_ingestion(location="Taiwan expo", limit=None):
    show_slug = SHOW_SLUGS.get(location)
    if not show_slug:
        print(f"No known show slug for '{location}'. Add it to SHOW_SLUGS first.")
        return

    output_path = "data/semi_suppliers.json"
    os.makedirs("data", exist_ok=True)

    # Resume support: keep anything already scraped with a real (non-fallback) about
    existing = {}
    if os.path.exists(output_path):
        with open(output_path, "r") as f:
            for row in json.load(f):
                existing[row["company_name"]] = row

    session = requests.Session()
    print(f"Fetching exhibitor directory for {location} ({show_slug})...")
    directory = fetch_exhibitor_directory(session, show_slug)
    if limit:
        directory = directory[:limit]

    results = []
    todo = [
        row for row in directory
        if existing.get(row["company_name"], {}).get("about") in (None, FALLBACK_ABOUT)
    ]
    print(f"{len(directory) - len(todo)} already have real descriptions, {len(todo)} to fetch")

    for i, row in enumerate(directory):
        if row["company_name"] in existing and existing[row["company_name"]].get("about") not in (None, FALLBACK_ABOUT):
            results.append(existing[row["company_name"]])
            continue

        print(f"[{i + 1}/{len(directory)}] {row['company_name']}")
        about, hq_location = fetch_booth_details(session, row["ebooth_url"], row["company_name"])
        results.append(
            {
                "company_name": row["company_name"],
                "location": location,  # expo/sourcing-cluster tag - keeps the app filter working
                "hq_location": hq_location,  # real company HQ, if found - may be None
                "about": about,
                "ebooth_url": row["ebooth_url"],
            }
        )

        # Checkpoint every 20 companies so a stopped run doesn't lose progress
        if (i + 1) % 20 == 0:
            with open(output_path, "w") as f:
                json.dump(results + list(directory[i + 1:]), f, indent=4)

        time.sleep(random.uniform(1.0, 2.0))  # be polite - this is a live show site

    with open(output_path, "w") as f:
        json.dump(results, f, indent=4)

    real = sum(1 for r in results if r["about"] != FALLBACK_ABOUT)
    print(f"\nDone. {len(results)} profiles saved to {output_path}")
    print(f"Real descriptions: {real}/{len(results)}")


if __name__ == "__main__":
    # Start small to spot-check quality before committing to the full ~1286-company run
    run_ingestion(location="Taiwan expo", limit=20)