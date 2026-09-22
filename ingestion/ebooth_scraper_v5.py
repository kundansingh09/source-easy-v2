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

# All six verified live on this URL pattern (expo.semi.org/{slug}/public/...,
# "Powered by a2z, Inc." / "Personify Corp.") by direct fetch, not guessed:
#   Taiwan expo -> taiwan2026  : exhibitors.aspx serves the real directory
#   Korea expo  -> korea2026   : same
#   China expo  -> china2026   : same, 1433 exhibitors, EN by default (add
#                                 ?langID=1 to exhibitors.aspx if it ever
#                                 defaults to Chinese for you - the anonymous
#                                 fetch used here returned English)
#   Japan expo  -> japan2026   : same, eBooth.aspx profile pages confirmed
#   Europe expo -> europa2026  : same slug as before (not "europe2026"), and
#                                 the eBooth profile pages ARE live - BUT see
#                                 the note below, this one needs a decision
#   India expo  -> india2026   : eBooth.aspx profile pages confirmed live too
#
# The decision worth knowing about, for Europe and India specifically:
# SEMI has ALSO stood up a newer, separate exhibitor portal for both shows
# (portal2026.semiconeuropa.org/exhibitors, portal2026.semiconindia.org/
# exhibitors - "Powered by A2Z Events", a different product from the classic
# a2z eBooth system these functions scrape). It's a different domain, a
# different page structure (company profiles at /co/{slug} instead of
# eBooth.aspx?IndexInList=..., categories rendered as a flat tagged string
# rather than the CatID/SubCatID href hierarchy this scraper depends on),
# and it looked richer for at least one profile spot-checked (ASML's "What
# We Do" text was long-form marketing copy, categories numbered 50 tags).
# It was NOT adopted here: the classic eBooth pages are confirmed live and
# populated for both shows RIGHT NOW with zero new code, while the new
# portal's category markup wasn't confirmed to carry the same L1/L2 ID
# hierarchy this pipeline's filtering depends on (no network access in this
# environment to inspect its raw HTML/hrefs and be sure). If a scrape of
# europa2026/india2026 through the pipeline below comes back thin - low
# category coverage or few real "about" hits, which run_ingestion already
# warns about - that is the signal to build a second scraper against the
# new portal rather than debug this one further.
SHOW_SLUGS = {
    "Korea expo": "korea2026",
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
    # Strategy 1: explicit description selectors (India/Taiwan classic layout)
    for selector in ["[id*='Description' i]", ".exhibitor-description", ".co-description"]:
        node = soup.select_one(selector)
        if node:
            text = node.get_text(" ", strip=True)
            if len(text) > 40:
                return text

    # Strategy 2: Korea layout — overview lives inside #content-container /
    # #ctl00_dvContent, after an "Overview" heading and before "Categories".
    for selector in ["#content-container", "#ctl00_dvContent"]:
        node = soup.select_one(selector)
        if not node:
            continue
        text = node.get_text("\n", strip=True)
        ov_match = re.search(r"^\s*Overview\s*$", text, re.M | re.I)
        if not ov_match:
            continue
        after_ov = text[ov_match.end():]
        stop = re.search(
            r"^\s*Categories\s*$|Type your information and click|Send Mail|Appointment",
            after_ov, re.M | re.I
        )
        window = after_ov[:stop.start()] if stop else after_ov
        lines = [l.strip() for l in window.split("\n") if len(l.strip()) > 20]
        if lines:
            return " ".join(lines)

    # Strategy 3: generic text-window fallback for all other layouts.
    # Enters the window AFTER an "Overview" heading (if present) so that
    # single-word section labels never bleed into the returned text.
    full_text = soup.get_text("\n", strip=True)
    start = re.search(r"Booth:\s*\S+", full_text)
    if not start:
        return None

    after_booth = full_text[start.end():]
    ov = re.search(r"^\s*Overview\s*$", after_booth, re.M | re.I)
    content_start = ov.end() if ov else 0

    stop = re.search(
        r"^\s*Categories\s*$|Type your information and click|Character Limit:|Send Mail|Appointment Date",
        after_booth[content_start:], re.M | re.I
    )
    window = after_booth[content_start: content_start + stop.start()] if stop else after_booth[content_start:]

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


# ------------------------------------------------------- multi-show driver

def run_all_shows(shows=None, limit=None, raw_dir="data/raw",
                   output_path="data/semi_suppliers.json",
                   review_path="data/dedupe_review.json"):
    """Ingest every show in `shows` (default: all of SHOW_SLUGS) to its own
    raw file under `raw_dir`, then merge them through dedupe.py into one
    `semi_suppliers.json` - the shape SourcingSearchEngine expects, now with
    `location` widened to `locations` (every expo a company was seen at) so
    a company exhibiting at both Taiwan and Europe is one searchable record,
    not two competing for the same query.

    Each show's raw file is kept and re-read on every run rather than held
    in memory across shows, for the same reason run_ingestion() itself
    writes incrementally: if show 4 of 6 dies on a network blip, shows 1-3
    are not re-scraped, and a plain re-run of this function picks up where
    it left off (run_ingestion's own resume logic keeps completed profiles
    inside each show's file; this loop just adds "was the show's file even
    finished" resume on top of that).
    """
    shows = shows or list(SHOW_SLUGS.keys())
    os.makedirs(raw_dir, exist_ok=True)

    raw_paths = []
    for show in shows:
        if show not in SHOW_SLUGS:
            print(f"Skipping '{show}': not in SHOW_SLUGS.")
            continue
        raw_path = os.path.join(raw_dir, f"{SHOW_SLUGS[show]}.json")
        print(f"\n=== {show} ({SHOW_SLUGS[show]}) -> {raw_path} ===")
        run_ingestion(location=show, limit=limit, output_path=raw_path)
        raw_paths.append(raw_path)

    if not raw_paths:
        print("No shows ingested, nothing to merge.")
        return

    print(f"\n=== merging {len(raw_paths)} shows ===")
    from dedupe import dedupe_files
    dedupe_files(raw_paths, output_path, review_path)


if __name__ == "__main__":
    # Start small and eyeball the output before the full multi-show run:
    #   run_ingestion(location="Taiwan expo", limit=20)
    # Full run across every confirmed show, merged and deduplicated:
    run_all_shows()