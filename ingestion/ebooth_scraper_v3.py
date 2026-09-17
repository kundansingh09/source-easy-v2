"""SEMICON exhibitor ingestion.

Stage 0: scrape the category taxonomy (level 1 -> level 2) -> data/categories.json
Stage 1: scrape the exhibitor directory (company name + real eBooth URL)
Stage 2: visit each profile for about / categories / HQ / website

Run:  python ingestion/ebooth_scraper_v3.py
"""

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
    "Europe expo": "europa2026", # <--- Fixed from 'europe2026'
    "India expo": "india2026",
}

FALLBACK_ABOUT = "Semiconductor technology and equipment supplier."
UNKNOWN_COUNTRY = "Unknown"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.5",
}

# Countries seen across SEMICON exhibitor lists. Used to pull a clean country
# out of free-text HQ strings like "Hsinchu, Taiwan" or "United Kingdom, United Kingdom".
KNOWN_COUNTRIES = {
    "taiwan": "Taiwan", "china": "China", "japan": "Japan", "korea": "South Korea",
    "south korea": "South Korea", "republic of korea": "South Korea",
    "singapore": "Singapore", "malaysia": "Malaysia", "thailand": "Thailand",
    "vietnam": "Vietnam", "philippines": "Philippines", "indonesia": "Indonesia",
    "india": "India", "hong kong": "Hong Kong", "macau": "Macau",
    "united states": "United States", "usa": "United States", "u.s.a.": "United States",
    "us": "United States", "canada": "Canada", "mexico": "Mexico", "brazil": "Brazil",
    "united kingdom": "United Kingdom", "uk": "United Kingdom", "england": "United Kingdom",
    "scotland": "United Kingdom", "ireland": "Ireland", "germany": "Germany",
    "france": "France", "netherlands": "Netherlands", "the netherlands": "Netherlands",
    "belgium": "Belgium", "switzerland": "Switzerland", "austria": "Austria",
    "italy": "Italy", "spain": "Spain", "portugal": "Portugal", "sweden": "Sweden",
    "norway": "Norway", "denmark": "Denmark", "finland": "Finland", "poland": "Poland",
    "czech republic": "Czech Republic", "czechia": "Czech Republic",
    "hungary": "Hungary", "romania": "Romania", "russia": "Russia",
    "israel": "Israel", "turkey": "Turkey", "australia": "Australia",
    "new zealand": "New Zealand", "south africa": "South Africa",
    "united arab emirates": "United Arab Emirates", "saudi arabia": "Saudi Arabia",
    "luxembourg": "Luxembourg", "slovenia": "Slovenia", "slovakia": "Slovakia",
    "estonia": "Estonia", "lithuania": "Lithuania", "latvia": "Latvia",
    "greece": "Greece", "ukraine": "Ukraine", "bulgaria": "Bulgaria",
}


# ---------------------------------------------------------------- utilities

def polite_sleep(lo=1.0, hi=2.0):
    time.sleep(random.uniform(lo, hi))


def clean_id(raw):
    """SEMI's hrefs contain padded whitespace, e.g. 'CatID=      203'."""
    if raw is None:
        return None
    digits = re.sub(r"\D", "", str(raw))
    return int(digits) if digits else None


def normalize_country(raw_location):
    """'Hsinchu, Taiwan' -> 'Taiwan'.  'United Kingdom, United Kingdom' -> 'United Kingdom'.

    Returns UNKNOWN_COUNTRY rather than None so the value is always
    hard-filterable (a null would make the record silently invisible
    to a country filter).
    """
    if not raw_location:
        return UNKNOWN_COUNTRY
    parts = [p.strip() for p in str(raw_location).split(",") if p.strip()]
    # Check right-to-left: country is conventionally last
    for part in reversed(parts):
        hit = KNOWN_COUNTRIES.get(part.lower())
        if hit:
            return hit
    for part in reversed(parts):
        low = part.lower()
        for key, val in KNOWN_COUNTRIES.items():
            if key in low:
                return val
    return UNKNOWN_COUNTRY


def dedupe_location(raw_location):
    """'United Kingdom, United Kingdom' -> 'United Kingdom' (for display)."""
    if not raw_location:
        return None
    parts = [p.strip() for p in str(raw_location).split(",") if p.strip()]
    return ", ".join(dict.fromkeys(parts)) or None


# ------------------------------------------------------- stage 0: taxonomy

def scrape_taxonomy(session, show_slug, out_path="data/categories.json"):
    """Build the master level1 -> level2 category tree from the exhibitor page.

    IMPORTANT: the leading display number is NOT the ID. On the live site
    '400 Components, Parts & Accessories' links to CatID=219. Always take the
    ID from the href, never from the label text.
    """
    url = f"https://expo.semi.org/{show_slug}/public/Exhibitors.aspx"
    resp = session.get(url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    l1, l2 = {}, {}
    for a in soup.find_all("a", href=True):
        href = a["href"]
        label = a.get_text(" ", strip=True)
        if not label:
            continue
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(href.replace(" ", "")).query)
        if "SubCatID" in qs:
            cid = clean_id(qs["SubCatID"][0])
            if cid:
                l2[cid] = label
        elif "CatID" in qs:
            cid = clean_id(qs["CatID"][0])
            if cid:
                l1[cid] = label

    taxonomy = {
        "level1": [{"id": k, "name": v} for k, v in sorted(l1.items())],
        "level2": [{"id": k, "name": v} for k, v in sorted(l2.items())],
    }
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(taxonomy, f, indent=4, ensure_ascii=False)
    print(f"Taxonomy: {len(l1)} level-1, {len(l2)} level-2 categories -> {out_path}")
    return taxonomy


# ------------------------------------------------------ stage 1: directory

def fetch_exhibitor_directory(session, show_slug):
    """One request gets every company name + real profile URL for a show."""
    url = f"https://expo.semi.org/{show_slug}/public/exhibitors.aspx"
    resp = session.get(url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    seen, exhibitors = set(), []
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

    match = re.search(r"(\d+)\s*Exhibitors", resp.text)
    if match:
        expected = int(match.group(1))
        status = "OK" if abs(len(exhibitors) - expected) < 5 else "MISMATCH - check pagination"
        print(f"[{show_slug}] parsed {len(exhibitors)} companies (page reports {expected} - {status})")
    else:
        print(f"[{show_slug}] parsed {len(exhibitors)} companies")
    return exhibitors


# -------------------------------------------------------- stage 2: profile

def extract_about(soup):
    for selector in ["[id*='Description' i]", ".exhibitor-description", ".co-description"]:
        node = soup.select_one(selector)
        if node:
            text = node.get_text(" ", strip=True)
            if len(text) > 40:
                return text

    # Fallback: window between the "Booth: ####" line and whichever comes
    # first - the "Categories" heading or the contact-form boilerplate.
    # Cutting at "Categories" matters: profiles with no Overview (common)
    # would otherwise capture the category list as their description, which
    # duplicates data already stored structurally and pollutes the embedding.
    full_text = soup.get_text("\n", strip=True)
    start = re.search(r"Booth:\s*\S+", full_text)
    if not start:
        return None

    end_candidates = [
        m.start() for m in re.finditer(r"^\s*Categories\s*$", full_text[start.end():], re.M | re.I)
    ]
    boiler = full_text.find("Type your information and click", start.end())
    ends = [e + start.end() for e in end_candidates]
    if boiler != -1:
        ends.append(boiler)
    if not ends:
        return None

    window = full_text[start.end():min(ends)]
    lines = [l.strip() for l in window.split("\n") if len(l.strip()) > 30]
    if lines:
        return " ".join(lines)
    return None


def extract_categories(soup):
    """Per-company hierarchical categories, IDs taken from hrefs (not labels).

    Parent-child nesting is recovered from DOCUMENT ORDER: on the profile page
    each level-1 heading is followed by its own level-2 items, so every
    SubCatID is attached to the most recently seen CatID. Returns flat lists
    (for filtering) plus a nested tree (for the UI and for display).
    """
    tree = []
    current = None
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "Exhibitors.aspx" not in href:
            continue
        label = a.get_text(" ", strip=True)
        if not label:
            continue
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(href.replace(" ", "")).query)

        if "SubCatID" in qs:
            cid = clean_id(qs["SubCatID"][0])
            if not cid:
                continue
            if current is None:
                # Subcategory with no preceding parent - keep it rather than
                # dropping data, under an explicit orphan bucket.
                current = {"l1_id": None, "l1_name": "Uncategorized", "children": []}
                tree.append(current)
            if cid not in [c["id"] for c in current["children"]]:
                current["children"].append({"id": cid, "name": label})

        elif "CatID" in qs:
            cid = clean_id(qs["CatID"][0])
            if not cid:
                continue
            existing = next((n for n in tree if n["l1_id"] == cid), None)
            if existing:
                current = existing
            else:
                current = {"l1_id": cid, "l1_name": label, "children": []}
                tree.append(current)

    l1_ids = [n["l1_id"] for n in tree if n["l1_id"] is not None]
    l1_names = [n["l1_name"] for n in tree if n["l1_id"] is not None]
    l2_ids, l2_names = [], []
    for n in tree:
        for c in n["children"]:
            if c["id"] not in l2_ids:
                l2_ids.append(c["id"])
                l2_names.append(c["name"])
    return l1_ids, l1_names, l2_ids, l2_names, tree


def extract_hq_and_website(soup, company_name):
    """HQ sits between the company-name heading and the 'Booth:' line.
    The website is rendered as its own plain-text URL, while its href is a
    SEMI redirector (Boothurl.aspx) - so read the link TEXT, not the href."""
    hq_raw, website = None, None

    for city_sel, country_sel in [
        ("[id*='lblCity' i]", "[id*='lblCountry' i]"),
        (".BoothContactCity", ".BoothContactCountry"),
    ]:
        city, country = soup.select_one(city_sel), soup.select_one(country_sel)
        parts = [c.get_text(strip=True) for c in (city, country) if c and c.get_text(strip=True)]
        if parts:
            hq_raw = ", ".join(dict.fromkeys(parts))
            break

    for a in soup.find_all("a", href=True):
        text = a.get_text(strip=True)
        if text.lower().startswith(("http://", "https://", "www.")):
            if "semi.org" in text.lower():
                continue
            website = text
            break

    if not hq_raw:
        full_text = soup.get_text("\n", strip=True)
        idx = full_text.find(company_name)
        if idx != -1:
            after = full_text[idx + len(company_name):]
            collected = []
            for line in [l.strip() for l in after.split("\n") if l.strip()]:
                low = line.lower()
                if low.startswith(("http", "www.")) or ".com" in low or ".tw" in low:
                    continue
                if low.startswith("booth:") or "sendemail" in low.replace(" ", ""):
                    break
                collected.append(line)
                if len(collected) >= 2:
                    break
            if collected:
                hq_raw = ", ".join(dict.fromkeys(collected))

    return dedupe_location(hq_raw), website


def fetch_profile(session, url, company_name, max_retries=2):
    for attempt in range(max_retries):
        try:
            resp = session.get(url, headers=HEADERS, timeout=20)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "html.parser")
            l1_ids, l1_names, l2_ids, l2_names, tree = extract_categories(soup)
            hq_raw, website = extract_hq_and_website(soup, company_name)
            return {
                "about": extract_about(soup) or FALLBACK_ABOUT,
                "hq_location": hq_raw,
                "hq_country": normalize_country(hq_raw),
                "website": website,
                "cat_l1_ids": l1_ids,
                "cat_l1_names": l1_names,
                "cat_l2_ids": l2_ids,
                "cat_l2_names": l2_names,
                "cat_tree": tree,
            }
        except Exception:
            if attempt == max_retries - 1:
                return {
                    "about": FALLBACK_ABOUT, "hq_location": None,
                    "hq_country": UNKNOWN_COUNTRY, "website": None,
                    "cat_l1_ids": [], "cat_l1_names": [],
                    "cat_l2_ids": [], "cat_l2_names": [], "cat_tree": [],
                }
            time.sleep(2)


# ------------------------------------------------------------------ driver

def run_ingestion(location="Taiwan expo", limit=None, output_path="data/semi_suppliers.json"):
    show_slug = SHOW_SLUGS.get(location)
    if not show_slug:
        print(f"No known show slug for '{location}'. Add it to SHOW_SLUGS first.")
        return

    os.makedirs("data", exist_ok=True)
    session = requests.Session()

    scrape_taxonomy(session, show_slug)
    polite_sleep()

    print(f"Fetching exhibitor directory for {location} ({show_slug})...")
    directory = fetch_exhibitor_directory(session, show_slug)
    if limit:
        directory = directory[:limit]

    # Resume: keep records that already have real content
    existing = {}
    if os.path.exists(output_path):
        try:
            with open(output_path) as f:
                for row in json.load(f):
                    existing[row["company_name"]] = row
        except Exception:
            pass

    results = []
    for i, row in enumerate(directory):
        prior = existing.get(row["company_name"])
        if prior and prior.get("about") not in (None, FALLBACK_ABOUT) and prior.get("cat_l1_ids"):
            results.append(prior)
            continue

        print(f"[{i + 1}/{len(directory)}] {row['company_name']}")
        details = fetch_profile(session, row["ebooth_url"], row["company_name"])
        results.append({
            "company_name": row["company_name"],
            "location": location,          # expo / sourcing-cluster tag
            "ebooth_url": row["ebooth_url"],
            **details,
        })

        if (i + 1) % 20 == 0:
            with open(output_path, "w") as f:
                json.dump(results, f, indent=4, ensure_ascii=False)
        polite_sleep()

    with open(output_path, "w") as f:
        json.dump(results, f, indent=4, ensure_ascii=False)

    real_about = sum(1 for r in results if r["about"] != FALLBACK_ABOUT)
    with_cats = sum(1 for r in results if r["cat_l1_ids"])
    with_hq = sum(1 for r in results if r["hq_country"] != UNKNOWN_COUNTRY)
    with_site = sum(1 for r in results if r.get("website"))
    n = len(results)
    print(f"\nDone. {n} profiles -> {output_path}")
    print(f"  real about text : {real_about}/{n}")
    print(f"  categories      : {with_cats}/{n}")
    print(f"  HQ country      : {with_hq}/{n}")
    print(f"  website         : {with_site}/{n}")
    if with_cats < n * 0.8:
        print("  WARNING: category coverage is low - inspect one profile's HTML "
              "and adjust extract_categories().")


if __name__ == "__main__":
    # Start small and eyeball the output before the full ~1286-company run
    run_ingestion(location="Taiwan expo")