#!/usr/bin/env python3
"""
SEMICON India 2026 -- eBooth profile scraper.

This ports the extraction logic from ebooth_scraper_v4.py (confirmed working
live on Taiwan/Korea/Japan/China) over to India, with two changes:

  1. DIRECTORY: India's exhibitors.aspx doesn't reliably return the full
     cross-hall list on its own (established earlier in this project), so
     directory discovery walks EventMap.aspx per hall (mapid=633, 634, ...
     confirmed; more probed automatically) instead of Taiwan's single
     exhibitors.aspx call. If you already have a CSV with ebooth_url filled
     in (you do -- exhibitors.csv), pass --from-csv and this skips discovery
     entirely and just re-visits those URLs, which is faster and is the
     recommended first run.

  2. CATEGORIES: skipped entirely, per instruction. extract_categories() is
     not ported over.

Everything else -- the booth regex, the "read link TEXT not href for the
website" fix, the HQ-from-lblCity/lblCountry logic, the about-text window
that stops at the Categories heading -- is carried over unchanged, because
it's already proven against this same a2z/eBooth page family.

--- Why the old website column was garbage ---
Old code treated ANY href as a possible website and prepended "https://" to
it blindly, so a relative in-page link like "Boothurl.aspx?BoothID=..."
became "https://Boothurl.aspx?...", which is nonsense -- that's SEMI's own
redirector, not a company site. The fix (same one this Taiwan script already
uses): the real website is rendered as visible LINK TEXT ("www.acme.com"),
while the href behind it is the redirector. Read a.get_text(), not a["href"].

Usage
-----
    pip install requests beautifulsoup4 lxml

    # Recommended first run: re-visit the ebooth_url your existing CSV
    # already has, fixing website/address/overview extraction.
    python india_ebooth_scraper.py --from-csv exhibitors.csv --limit 20

    # Full run from the existing CSV:
    python india_ebooth_scraper.py --from-csv exhibitors.csv

    # Discovery from scratch (only if you don't trust the CSV's directory):
    python india_ebooth_scraper.py --discover --limit 20

Output
------
    out3/exhibitors_final.csv
    out3/exhibitors_final.json
    out3/report.txt
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import sys
import time
import unicodedata
import urllib.parse
from dataclasses import dataclass, asdict, field
from typing import Optional

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# --------------------------------------------------------------------------
# CONFIG
# --------------------------------------------------------------------------

SHOW_SLUG = "india2026"
EVENT_ID = "32838"
EXPO_BASE = f"https://expo.semi.org/{SHOW_SLUG}/Public/"

# Confirmed halls (633, 634); extend/override with --halls.
HALL_MAP_IDS = [633, 634]
HALL_PROBE_RANGE = range(628, 650)  # used only with --probe-halls

REQUEST_DELAY = 1.3
TIMEOUT = 20
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.5",
}

OUT_DIR = "out3"
CACHE_DIR = "cache_ebooth"

LISTING_PATTERNS = [
    r"^\s*$",
    r"^(all|view all|show all|back to|return to)\b",
    r"^(product|technology|market|category|segment)\s+(categories|listing|listings|index)",
    r"^(sponsor|sponsors|media partner|partners|supporting organi[sz]ation)s?\b",
    r"^(pavilion|country pavilion|zone|theatre|theater|stage|lounge|cafe|registration)\b",
    r"^(exhibitor list|exhibitor directory|floor ?plan|hall \d+)\b",
    r"^\d+\s*$",
]
LISTING_RE = [re.compile(p, re.I) for p in LISTING_PATTERNS]

STANDALONE_BOOTH_RE = re.compile(r"^\s*([A-Z]{0,3}[-\s]?\d{1,5}[A-Z]?)\s*$")
BOOTH_LINE_RE = re.compile(r"Booth:\s*(\S+)", re.I)


# --------------------------------------------------------------------------
# DATA MODEL
# --------------------------------------------------------------------------

@dataclass
class Exhibitor:
    name: str = ""
    booth: str = ""
    website: str = ""
    address: str = ""
    overview: str = ""
    ebooth_url: str = ""
    halls: list = field(default_factory=list)
    fetch_status: str = ""   # ok / 404 / error / no-url


# --------------------------------------------------------------------------
# HELPERS
# --------------------------------------------------------------------------

def log(msg: str) -> None:
    print(msg, flush=True)


def clean(text: Optional[str]) -> str:
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text).replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def normalize_name(name: str) -> str:
    s = clean(name).lower()
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    s = re.sub(
        r"\b(pvt|private|ltd|limited|llp|inc|incorporated|co|corp|corporation|"
        r"gmbh|bv|nv|sa|ag|kk|plc|llc|group|india|technologies|technology)\b",
        " ", s)
    return re.sub(r"\s+", " ", s).strip()


def is_listing(name: str) -> bool:
    n = clean(name)
    if len(n) < 2:
        return True
    return any(rx.match(n) for rx in LISTING_RE)


def clean_id(raw):
    if raw is None:
        return None
    digits = re.sub(r"\D", "", str(raw))
    return int(digits) if digits else None


# --------------------------------------------------------------------------
# HTTP with on-disk cache (safe to re-run / resume)
# --------------------------------------------------------------------------

class Fetcher:
    def __init__(self, delay=REQUEST_DELAY):
        self.delay = delay
        self.last_request = 0.0
        self.live = 0
        self.cached = 0
        self.failed = 0
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        retry = Retry(total=3, backoff_factor=1.5,
                       status_forcelist=[429, 500, 502, 503, 504],
                       allowed_methods=["GET"])
        self.session.mount("https://", HTTPAdapter(max_retries=retry))
        self.session.mount("http://", HTTPAdapter(max_retries=retry))
        os.makedirs(CACHE_DIR, exist_ok=True)

    def get(self, url: str) -> tuple[Optional[str], str]:
        path = os.path.join(CACHE_DIR, hashlib.sha1(url.encode()).hexdigest()[:20] + ".html")
        if os.path.exists(path):
            self.cached += 1
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                content = fh.read()
            return (content or None), ("ok" if content else "cached-empty")

        wait = self.delay - (time.time() - self.last_request)
        if wait > 0:
            time.sleep(wait)
        try:
            resp = self.session.get(url, timeout=TIMEOUT)
            self.last_request = time.time()
            self.live += 1
        except requests.RequestException as exc:
            self.failed += 1
            log(f"  ! request failed {url} :: {exc}")
            return None, "error"

        if resp.status_code == 404:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("")
            return None, "404"
        if resp.status_code != 200:
            self.failed += 1
            return None, f"http-{resp.status_code}"

        with open(path, "w", encoding="utf-8") as fh:
            fh.write(resp.text)
        return resp.text, "ok"


# --------------------------------------------------------------------------
# STAGE 1 -- DIRECTORY (only used with --discover)
# --------------------------------------------------------------------------

def extract_directory_rows(html: str, page_url: str, hall: Optional[int] = None) -> list[Exhibitor]:
    """Same eBooth.aspx?IndexInList= anchor pattern Taiwan's directory uses."""
    soup = BeautifulSoup(html, "html.parser")
    seen, rows = set(), []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "eBooth.aspx" not in href or "IndexInList" not in href:
            continue
        name = clean(a.get_text(" "))
        if not name or is_listing(name):
            continue
        abs_url = urllib.parse.urljoin(page_url, href)
        if abs_url in seen:
            continue
        seen.add(abs_url)
        ex = Exhibitor(name=name, ebooth_url=abs_url)
        if hall is not None:
            ex.halls = [hall]
        rows.append(ex)
    return rows


def discover(fetcher: Fetcher, halls: list[int]) -> list[Exhibitor]:
    # Try the single-list page first (works for Taiwan/Korea/Japan/China).
    url = f"{EXPO_BASE}exhibitors.aspx?ID={EVENT_ID}&shAvailable=1"
    log(f"[discover] trying {url}")
    html, status = fetcher.get(url)
    if html:
        rows = extract_directory_rows(html, url)
        if len(rows) >= 20:
            log(f"[discover] exhibitors.aspx returned {len(rows)} -- using this as the full list")
            return rows
        log(f"[discover] exhibitors.aspx only gave {len(rows)}, falling back to per-hall EventMap")

    merged: dict[str, Exhibitor] = {}
    for mid in halls:
        map_url = f"{EXPO_BASE}EventMap.aspx?ID={EVENT_ID}&shAvailable=1&mapid={mid}"
        log(f"[discover] hall mapid={mid}")
        html, status = fetcher.get(map_url)
        if not html:
            log(f"[discover]   mapid={mid}: {status}")
            continue
        rows = extract_directory_rows(html, map_url, hall=mid)
        log(f"[discover]   mapid={mid}: {len(rows)} rows")
        for ex in rows:
            k = normalize_name(ex.name)
            if not k:
                continue
            if k in merged:
                if mid not in merged[k].halls:
                    merged[k].halls.append(mid)
            else:
                merged[k] = ex
    return list(merged.values())


def probe_halls(fetcher: Fetcher) -> list[int]:
    good = []
    for mid in HALL_PROBE_RANGE:
        url = f"{EXPO_BASE}EventMap.aspx?ID={EVENT_ID}&shAvailable=1&mapid={mid}"
        html, status = fetcher.get(url)
        if not html:
            continue
        rows = extract_directory_rows(html, url, hall=mid)
        if len(rows) >= 5:
            good.append(mid)
            log(f"[probe] mapid={mid}: {len(rows)} rows -- keeping")
    return good


# --------------------------------------------------------------------------
# STAGE 2 -- PROFILE (ported from ebooth_scraper_v4.py, categories dropped)
# --------------------------------------------------------------------------

def extract_booth(soup: BeautifulSoup, full_text: str) -> str:
    for sel in ("[id*='lblBooth' i]", ".BoothNumber", "[id*='Booth' i]"):
        node = soup.select_one(sel)
        if node:
            t = clean(node.get_text(" "))
            if STANDALONE_BOOTH_RE.match(t):
                return t.replace(" ", "")
    m = BOOTH_LINE_RE.search(full_text)
    if m:
        return clean(m.group(1))
    return ""


def extract_hq_and_website(soup: BeautifulSoup, company_name: str):
    """
    Ported unchanged from the Taiwan script (already proven against this
    page family): HQ from lblCity/lblCountry pair, website from anchor TEXT
    (never the href -- the href is a Boothurl.aspx redirector, not the
    company's real site).
    """
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
            if "semi.org" in text.lower() or "semiconindia.org" in text.lower():
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
                if low.startswith(("http", "www.")) or ".com" in low or ".in" in low:
                    continue
                if low.startswith("booth:") or "sendemail" in low.replace(" ", ""):
                    break
                collected.append(line)
                if len(collected) >= 2:
                    break
            if collected:
                hq_raw = ", ".join(dict.fromkeys(collected))

    if hq_raw:
        parts = [p.strip() for p in hq_raw.split(",") if p.strip()]
        hq_raw = ", ".join(dict.fromkeys(parts))

    return hq_raw, website


def extract_about(soup: BeautifulSoup) -> str:
    for selector in ["[id*='Description' i]", ".exhibitor-description", ".co-description"]:
        node = soup.select_one(selector)
        if node:
            text = clean(node.get_text(" "))
            if len(text) > 40:
                return text

    # Fallback: window between "Booth: ####" and the first of Categories /
    # contact-form boilerplate. Categories itself is not extracted (dropped
    # per instruction) -- it's only used here as a stop marker so the about
    # text doesn't swallow the category list.
    full_text = soup.get_text("\n", strip=True)
    start = BOOTH_LINE_RE.search(full_text)
    if not start:
        return ""

    end_candidates = [
        m.start() for m in re.finditer(r"^\s*Categories\s*$", full_text[start.end():], re.M | re.I)
    ]
    boiler = full_text.find("Type your information and click", start.end())
    ends = [e + start.end() for e in end_candidates]
    if boiler != -1:
        ends.append(boiler)
    if not ends:
        return ""

    window = full_text[start.end():min(ends)]
    lines = [l.strip() for l in window.split("\n") if len(l.strip()) > 30]
    return " ".join(lines) if lines else ""


def fetch_profile(fetcher: Fetcher, ex: Exhibitor) -> None:
    if not ex.ebooth_url:
        ex.fetch_status = "no-url"
        return
    html, status = fetcher.get(ex.ebooth_url)
    ex.fetch_status = status
    if not html:
        return

    soup = BeautifulSoup(html, "html.parser")
    full_text = soup.get_text("\n", strip=True)

    booth = extract_booth(soup, full_text)
    if booth:
        ex.booth = booth

    hq_raw, website = extract_hq_and_website(soup, ex.name)
    if hq_raw and not ex.address:
        ex.address = hq_raw
    if website:
        ex.website = website

    about = extract_about(soup)
    if about:
        ex.overview = about


# --------------------------------------------------------------------------
# LOAD FROM EXISTING CSV
# --------------------------------------------------------------------------

def load_from_csv(path: str) -> list[Exhibitor]:
    rows = []
    with open(path, "r", encoding="utf-8-sig", newline="") as fh:
        for r in csv.DictReader(fh):
            ex = Exhibitor(
                name=clean(r.get("name", "")),
                booth=clean(r.get("booth", "")),
                address=clean(r.get("address", "")),
                ebooth_url=clean(r.get("ebooth_url", "")),
            )
            halls = clean(r.get("halls", ""))
            if halls:
                ex.halls = [int(h) for h in halls.split("|") if h.strip().isdigit()]
            rows.append(ex)
    return rows


# --------------------------------------------------------------------------
# OUTPUT
# --------------------------------------------------------------------------

CSV_FIELDS = ["name", "booth", "website", "address", "overview",
              "halls", "ebooth_url", "fetch_status"]


def write_outputs(rows: list[Exhibitor]) -> str:
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(OUT_DIR, "exhibitors_final.csv"), "w",
              newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
        w.writeheader()
        for ex in rows:
            d = asdict(ex)
            d["halls"] = "|".join(str(h) for h in ex.halls)
            w.writerow({k: d[k] for k in CSV_FIELDS})

    with open(os.path.join(OUT_DIR, "exhibitors_final.json"), "w", encoding="utf-8") as fh:
        json.dump([asdict(e) for e in rows], fh, indent=2, ensure_ascii=False)

    report = build_report(rows)
    with open(os.path.join(OUT_DIR, "report.txt"), "w", encoding="utf-8") as fh:
        fh.write(report)
    return report


def build_report(rows: list[Exhibitor]) -> str:
    total = len(rows)
    if not total:
        return "No rows.\n"

    tracked = ["name", "booth", "website", "address", "overview"]
    missing = {f: sum(1 for e in rows if not getattr(e, f).strip()) for f in tracked}

    status_counts: dict[str, int] = {}
    for e in rows:
        status_counts[e.fetch_status or "unknown"] = status_counts.get(e.fetch_status or "unknown", 0) + 1

    lines = []
    lines.append("=" * 60)
    lines.append(f"SEMICON India 2026 -- eBooth profile report")
    lines.append("=" * 60)
    lines.append(f"Total companies: {total}")
    lines.append("")
    lines.append("Fetch status:")
    for k, v in sorted(status_counts.items()):
        lines.append(f"  {k:<14} {v}  ({v/total*100:.1f}%)")
    lines.append("")
    lines.append(f"{'FIELD':<10}{'FILLED':>8}{'EMPTY':>8}{'% FILLED':>10}")
    lines.append("-" * 60)
    for f in tracked:
        filled = total - missing[f]
        star = "  <- key" if f in ("name", "booth", "website") else ""
        lines.append(f"{f:<10}{filled:>8}{missing[f]:>8}{filled/total*100:>9.1f}%{star}")
    lines.append("-" * 60)

    core = sum(1 for e in rows if e.name.strip() and e.booth.strip() and e.website.strip())
    lines.append(f"Complete on name+booth+website: {core} / {total} ({core/total*100:.1f}%)")
    lines.append("")

    no_site = [e.name for e in rows if not e.website.strip()][:30]
    if no_site:
        lines.append(f"Missing website ({missing['website']} total), first 30:")
        for n in no_site:
            lines.append(f"  - {n}")
        lines.append("")

    lines.append("=" * 60)
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# MAIN
# --------------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="SEMICON India 2026 eBooth profile scraper")
    ap.add_argument("--from-csv", help="existing exhibitors.csv to re-visit (recommended)")
    ap.add_argument("--discover", action="store_true",
                    help="discover directory from scratch instead of --from-csv")
    ap.add_argument("--halls", type=int, nargs="*", default=None)
    ap.add_argument("--probe-halls", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--delay", type=float, default=REQUEST_DELAY)
    args = ap.parse_args(argv)

    if not args.from_csv and not args.discover:
        ap.error("pass either --from-csv exhibitors.csv or --discover")

    fetcher = Fetcher(delay=args.delay)

    if args.from_csv:
        log(f"[load] reading {args.from_csv}")
        rows = load_from_csv(args.from_csv)
        log(f"[load] {len(rows)} companies loaded")
    else:
        halls = args.halls if args.halls else (
            probe_halls(fetcher) if args.probe_halls else HALL_MAP_IDS)
        rows = discover(fetcher, halls)
        log(f"[discover] {len(rows)} unique companies")

    if not rows:
        log("No companies to process.")
        return 1

    if args.limit:
        rows = rows[:args.limit]

    log(f"[profile] visiting {len(rows)} eBooth pages")
    for i, ex in enumerate(rows, 1):
        try:
            fetch_profile(fetcher, ex)
        except Exception as exc:
            log(f"  ! failed {ex.name}: {exc}")
            ex.fetch_status = "error"
        if i % 25 == 0 or i == len(rows):
            log(f"  {i}/{len(rows)}  live={fetcher.live} cached={fetcher.cached} failed={fetcher.failed}")

    report = write_outputs(rows)
    log("")
    log(report)
    log(f"Wrote {OUT_DIR}/exhibitors_final.csv, .json, report.txt")
    return 0


if __name__ == "__main__":
    sys.exit(main())