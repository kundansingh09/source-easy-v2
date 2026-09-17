#!/usr/bin/env python3
"""
SEMICON India 2026 exhibitor scraper.

Two-stage design:

  STAGE 1  DISCOVERY  -> company name + booth number
           Preferred:  exhibitors.aspx  (single cross-hall list; the pattern that
                       works for the Taiwan/Korea/Japan shows)
           Fallback:   EventMap.aspx?...&mapid=NNN  per hall, then merge + dedupe.
           Both emit the identical `eBooth.aspx?IndexInList=` link pattern, so the
           same extractor handles either source.

  STAGE 2  ENRICHMENT -> address, website, overview
           Each company's portal page at
           https://portal2026.semiconindia.org/co/<slug>
           Slug is taken from a real href when the page gives us one, and only
           derived from the company name as a fallback.

Priority fields are NAME, WEBSITE, BOOTH. The run always ends with a completeness
report counting how many records are missing each field.

Usage
-----
    pip install requests beautifulsoup4 lxml

    python semicon_scraper.py                       # full run
    python semicon_scraper.py --probe-halls         # find every valid mapid first
    python semicon_scraper.py --limit 25            # smoke test
    python semicon_scraper.py --debug-dump          # save raw HTML for selector fixing
    python semicon_scraper.py --no-portal           # stage 1 only (fast, name+booth)

Output
------
    out/exhibitors.csv     flat table
    out/exhibitors.json    full records
    out/report.txt         completeness counters
    cache/                 every fetched page (delete to force re-fetch)
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
from dataclasses import dataclass, field, asdict
from typing import Iterable, Optional
from urllib.parse import urljoin, urlparse, parse_qs

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# --------------------------------------------------------------------------
# CONFIG  -- the only block you should normally need to touch
# --------------------------------------------------------------------------

SHOW_SLUG = "india2026"          # NOTE: Europe's real slug is `europa2026`, not `europe2026`
EVENT_ID = "32838"
EXPO_BASE = f"https://expo.semi.org/{SHOW_SLUG}/Public/"
PORTAL_BASE = "https://portal2026.semiconindia.org/co/"

# Confirmed halls. 633 and 634 were verified as two distinct halls.
# If you believe there is a 643, leave it here -- invalid IDs are dropped
# automatically rather than crashing the run.
MAP_IDS = [633, 634]

# --probe-halls scans this range and keeps whatever returns a real booth list.
HALL_PROBE_RANGE = range(628, 650)

REQUEST_DELAY = 1.2              # seconds between live requests, be polite
TIMEOUT = 30
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
)

OUT_DIR = "out"
CACHE_DIR = "cache"
DEBUG_DIR = "debug"

# Rows that are category listings / sponsor blocks / nav furniture rather than
# real exhibitors. Matched case-insensitively against the extracted name.
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

# Hosts that are never a company's own website (social, the show itself, etc.)
NON_WEBSITE_HOSTS = {
    "facebook.com", "www.facebook.com", "twitter.com", "x.com", "www.x.com",
    "linkedin.com", "www.linkedin.com", "instagram.com", "www.instagram.com",
    "youtube.com", "www.youtube.com", "wechat.com", "weibo.com",
    "semi.org", "expo.semi.org", "portal2026.semiconindia.org",
    "semiconindia.org", "www.semiconindia.org", "maps.google.com",
    "goo.gl", "google.com", "www.google.com",
}

BOOTH_RE = re.compile(
    r"\b(?:booth|stand|stall)\s*(?:no\.?|number|#|:)?\s*([A-Z]{0,3}[-\s]?\d{1,5}[A-Z]?(?:\s*[/,&]\s*[A-Z]{0,3}[-\s]?\d{1,5}[A-Z]?)*)",
    re.I,
)
STANDALONE_BOOTH_RE = re.compile(r"^\s*([A-Z]{0,3}[-\s]?\d{1,5}[A-Z]?)\s*$")
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
PHONE_RE = re.compile(r"(?:\+?\d[\d\s().-]{7,}\d)")


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
    country: str = ""
    email: str = ""
    phone: str = ""
    halls: list = field(default_factory=list)      # which mapid(s) it appeared in
    ebooth_url: str = ""
    portal_url: str = ""
    portal_status: str = ""                        # ok / 404 / slug-guessed / skipped
    source: str = ""                               # exhibitors.aspx | eventmap

    def key(self) -> str:
        return normalize_name(self.name)


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

class Fetcher:
    def __init__(self, delay=REQUEST_DELAY, debug=False):
        self.delay = delay
        self.debug = debug
        self.last_request = 0.0
        self.live_count = 0
        self.cache_count = 0
        self.fail_count = 0

        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        })
        retry = Retry(
            total=3, backoff_factor=1.5,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET"],
        )
        self.session.mount("https://", HTTPAdapter(max_retries=retry))
        self.session.mount("http://", HTTPAdapter(max_retries=retry))

        os.makedirs(CACHE_DIR, exist_ok=True)
        if debug:
            os.makedirs(DEBUG_DIR, exist_ok=True)

    def _cache_path(self, url: str) -> str:
        h = hashlib.sha1(url.encode("utf-8")).hexdigest()[:20]
        return os.path.join(CACHE_DIR, f"{h}.html")

    def get(self, url: str, label: str = "") -> Optional[str]:
        path = self._cache_path(url)
        if os.path.exists(path):
            self.cache_count += 1
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                return fh.read()

        wait = self.delay - (time.time() - self.last_request)
        if wait > 0:
            time.sleep(wait)

        try:
            resp = self.session.get(url, timeout=TIMEOUT)
            self.last_request = time.time()
            self.live_count += 1
        except requests.RequestException as exc:
            self.fail_count += 1
            log(f"  ! request failed {url} :: {exc}")
            return None

        if resp.status_code == 404:
            # Cache 404s as empty so a re-run doesn't re-hammer dead slugs.
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("")
            return None
        if resp.status_code != 200:
            self.fail_count += 1
            log(f"  ! HTTP {resp.status_code} {url}")
            return None

        html = resp.text
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(html)
        if self.debug and label:
            with open(os.path.join(DEBUG_DIR, f"{label}.html"), "w",
                      encoding="utf-8") as fh:
                fh.write(html)
        return html


# --------------------------------------------------------------------------
# HELPERS
# --------------------------------------------------------------------------

def log(msg: str) -> None:
    print(msg, flush=True)


def soup_of(html: str) -> BeautifulSoup:
    try:
        return BeautifulSoup(html, "lxml")
    except Exception:
        return BeautifulSoup(html, "html.parser")


def clean(text: Optional[str]) -> str:
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text)
    text = text.replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def normalize_name(name: str) -> str:
    """Dedupe key: strip punctuation and common corporate suffixes."""
    s = clean(name).lower()
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    s = re.sub(
        r"\b(pvt|private|ltd|limited|llp|inc|incorporated|co|corp|corporation|"
        r"gmbh|bv|nv|sa|ag|kk|plc|llc|group|india|technologies|technology)\b",
        " ", s)
    return re.sub(r"\s+", " ", s).strip()


def slugify(name: str) -> str:
    """
    Match the portal's observed slug format exactly, confirmed against a real
    exhibitors.aspx export:

        3D Glass Solutions                     -> 3d-glass-solutions
        AC&C Technology Pte Ltd                -> ac&c-technology-pte-ltd
        Accurex Solutions Pvt. Ltd             -> accurex-solutions-pvt.-ltd
        ACM Research Inc.                      -> acm-research-inc.
        Advance Panels & Switchgears P Ltd     -> advance-panels-&-switchgears-p-ltd

    Rule: lowercase, collapse whitespace runs to a single '-', and otherwise
    leave punctuation alone (crucially: '&' and '.' are KEPT, not stripped
    or converted to '-'). Only characters that are neither alnum, whitespace,
    nor one of & . - are dropped outright (commas, parentheses, apostrophes,
    slashes -- no confirmed example yet, so these are removed rather than
    guessed at).
    """
    s = unicodedata.normalize("NFKD", clean(name))
    s = s.encode("ascii", "ignore").decode("ascii").lower()
    s = re.sub(r"\s+", "-", s)
    s = re.sub(r"[^a-z0-9&.\-]", "", s)
    s = re.sub(r"-{2,}", "-", s)
    return s.strip("-")


def is_listing(name: str) -> bool:
    """True for category/sponsor/nav rows that are not real exhibitors."""
    n = clean(name)
    if len(n) < 2:
        return True
    return any(rx.match(n) for rx in LISTING_RE)


def looks_like_company_website(url: str) -> bool:
    if not url or not url.lower().startswith(("http://", "https://")):
        return False
    host = (urlparse(url).netloc or "").lower()
    if not host or host in NON_WEBSITE_HOSTS:
        return False
    return host.lstrip("www.") not in {h.lstrip("www.") for h in NON_WEBSITE_HOSTS}


def tidy_website(url: str) -> str:
    url = clean(url)
    if not url:
        return ""
    if not url.lower().startswith(("http://", "https://")):
        url = "https://" + url.lstrip("/")
    return url.rstrip("/")


def extract_booth(text: str) -> str:
    m = BOOTH_RE.search(text or "")
    if m:
        return clean(m.group(1)).replace(" ", "")
    return ""


# --------------------------------------------------------------------------
# STAGE 1a -- extract exhibitor rows from any page carrying eBooth links
# --------------------------------------------------------------------------

def extract_exhibitor_links(html: str, page_url: str, source: str,
                            hall: Optional[int] = None) -> list[Exhibitor]:
    """
    Works on both exhibitors.aspx and EventMap.aspx because both emit
    `eBooth.aspx?IndexInList=...` anchors. Name comes from the anchor text;
    booth number is hunted for in the anchor, then its row, then its parent.
    """
    soup = soup_of(html)
    found: list[Exhibitor] = []
    seen_hrefs: set[str] = set()

    anchors = soup.find_all(
        "a", href=lambda h: h and "ebooth.aspx" in h.lower()
    )
    # Some EventMap variants link straight to the portal instead.
    anchors += soup.find_all(
        "a", href=lambda h: h and "/co/" in h and "portal" in h.lower()
    )

    for a in anchors:
        href = a.get("href", "")
        abs_url = urljoin(page_url, href)
        if abs_url in seen_hrefs:
            continue
        seen_hrefs.add(abs_url)

        name = clean(a.get_text(" "))
        if not name:
            name = clean(a.get("title") or a.get("aria-label") or "")
        if is_listing(name):
            continue

        ex = Exhibitor(name=name, source=source)
        if hall is not None:
            ex.halls = [hall]

        if "ebooth.aspx" in abs_url.lower():
            ex.ebooth_url = abs_url
        else:
            ex.portal_url = abs_url

        # --- booth number, widening the search outward ---
        booth = ""
        row = a.find_parent("tr")
        container = row or a.find_parent(["li", "div", "td", "article"]) or a.parent

        # 1. a sibling cell that is purely a booth-looking token
        if row:
            for cell in row.find_all(["td", "th"]):
                txt = clean(cell.get_text(" "))
                if txt and txt != name and STANDALONE_BOOTH_RE.match(txt):
                    booth = txt.replace(" ", "")
                    break
        # 2. an element whose class/id mentions booth
        if not booth and container:
            node = container.find(
                attrs={"class": re.compile(r"booth|stand", re.I)}
            ) or container.find(attrs={"id": re.compile(r"booth|stand", re.I)})
            if node:
                t = clean(node.get_text(" "))
                booth = extract_booth(t) or (t if STANDALONE_BOOTH_RE.match(t) else "")
        # 3. "Booth: 1234" anywhere in the surrounding block
        if not booth and container:
            booth = extract_booth(container.get_text(" "))
        # 4. the URL sometimes carries it
        if not booth:
            qs = parse_qs(urlparse(abs_url).query)
            for k in ("BoothID", "boothid", "Booth"):
                if k in qs and STANDALONE_BOOTH_RE.match(qs[k][0] or ""):
                    booth = qs[k][0]
                    break

        ex.booth = clean(booth)
        found.append(ex)

    return found


def extract_exhibitor_table_rows(html: str, page_url: str, source: str,
                                  hall: Optional[int] = None) -> list[Exhibitor]:
    """
    Row-level pass over exhibitors.aspx-style tables, used alongside
    extract_exhibitor_links() specifically to catch rows like:

        ADVANCED SYSTEM IN PACKAGE TECHNOLOGIES   760   (no link at all)

    which the anchor scan above silently misses because there's no <a> to
    find. Any exhibitor recovered here that also appears in the anchor pass
    gets folded together at merge() time via the normalized-name key, so
    running both passes is safe and just fills gaps.
    """
    soup = soup_of(html)
    found: list[Exhibitor] = []

    for row in soup.find_all("tr"):
        cells = row.find_all(["td", "th"])
        if len(cells) < 2:
            continue

        cell_texts = [clean(c.get_text(" ")) for c in cells]
        # header row guard
        if any(t.lower() in ("company name", "booth", "booth#", "booth no",
                              "booth no.", "website") for t in cell_texts):
            continue

        booth = ""
        name = ""
        url = ""

        a = row.find("a", href=True)
        if a:
            href = urljoin(page_url, a["href"])
            if "/co/" in href or "ebooth.aspx" in href.lower():
                url = href

        for t in cell_texts:
            if not t:
                continue
            if not booth and STANDALONE_BOOTH_RE.match(t) and t != name:
                booth = t.replace(" ", "")
                continue
            if t.lower().startswith(("http://", "https://")):
                continue
            if not name and not STANDALONE_BOOTH_RE.match(t):
                name = t

        if not name or is_listing(name):
            continue

        ex = Exhibitor(name=name, booth=booth, source=source)
        if hall is not None:
            ex.halls = [hall]
        if url:
            if "ebooth.aspx" in url.lower():
                ex.ebooth_url = url
            else:
                ex.portal_url = url
                ex.portal_status = "ok"  # link was present in the source table
        else:
            ex.portal_status = "no-portal-listed"  # confirmed: some rows carry no link at all
        found.append(ex)

    return found


# --------------------------------------------------------------------------
# STAGE 1b -- discovery sources
# --------------------------------------------------------------------------

def try_exhibitors_page(fetcher: Fetcher) -> list[Exhibitor]:
    """The preferred single cross-hall list. Returns [] if this show lacks it."""
    candidates = [
        f"{EXPO_BASE}exhibitors.aspx?ID={EVENT_ID}&shAvailable=1",
        f"{EXPO_BASE}Exhibitors.aspx?ID={EVENT_ID}",
        f"{EXPO_BASE}exhibitors.aspx",
    ]
    for url in candidates:
        log(f"[discovery] trying {url}")
        html = fetcher.get(url, label="exhibitors")
        if not html:
            continue
        rows = extract_exhibitor_links(html, url, source="exhibitors.aspx")
        rows += extract_exhibitor_table_rows(html, url, source="exhibitors.aspx")
        if len(rows) >= 20:          # a real list, not an empty shell
            log(f"[discovery] exhibitors.aspx returned {len(rows)} raw rows "
                f"(anchor + table pass, pre-dedupe)")
            return rows
        log(f"[discovery]   only {len(rows)} entries, not treating as complete")
    return []


def scrape_eventmap(fetcher: Fetcher, mapid: int) -> list[Exhibitor]:
    url = (f"{EXPO_BASE}EventMap.aspx?ID={EVENT_ID}"
           f"&shAvailable=1&mapid={mapid}")
    log(f"[discovery] hall map mapid={mapid}")
    html = fetcher.get(url, label=f"eventmap_{mapid}")
    if not html:
        return []
    rows = extract_exhibitor_links(html, url, source="eventmap", hall=mapid)
    rows += extract_exhibitor_table_rows(html, url, source="eventmap", hall=mapid)
    log(f"[discovery]   {len(rows)} raw rows in mapid={mapid} (pre-dedupe)")
    return rows


def probe_halls(fetcher: Fetcher) -> list[int]:
    """Walk a range of mapids and keep any that yield a real booth list."""
    good = []
    log(f"[probe] scanning mapid {HALL_PROBE_RANGE.start}-{HALL_PROBE_RANGE.stop - 1}")
    for mid in HALL_PROBE_RANGE:
        rows = scrape_eventmap(fetcher, mid)
        if len(rows) >= 5:
            good.append(mid)
    log(f"[probe] valid hall map IDs: {good}")
    return good


# --------------------------------------------------------------------------
# STAGE 2 -- enrichment
# --------------------------------------------------------------------------

def parse_ebooth(html: str, page_url: str, ex: Exhibitor) -> None:
    """The intermediate SEMI booth page: often has booth no. + a portal link."""
    soup = soup_of(html)
    text = clean(soup.get_text(" "))

    if not ex.booth:
        ex.booth = extract_booth(text)

    if not ex.portal_url:
        a = soup.find("a", href=lambda h: h and "/co/" in h)
        if a:
            ex.portal_url = urljoin(page_url, a["href"])

    if not ex.website:
        for a in soup.find_all("a", href=True):
            href = tidy_website(a["href"])
            if looks_like_company_website(href):
                ex.website = href
                break


def parse_portal(html: str, page_url: str, ex: Exhibitor) -> None:
    """Company detail page at portal2026.semiconindia.org/co/<slug>."""
    soup = soup_of(html)

    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()

    # --- name (prefer the portal's canonical spelling) ---
    h = soup.find(["h1", "h2"])
    if h:
        portal_name = clean(h.get_text(" "))
        if portal_name and not is_listing(portal_name):
            if not ex.name or len(portal_name) > len(ex.name):
                ex.name = portal_name

    # --- website ---
    if not ex.website:
        node = soup.find(attrs={"class": re.compile(r"website|url|weblink", re.I)})
        if node:
            a = node.find("a", href=True)
            cand = tidy_website(a["href"] if a else node.get_text(" "))
            if looks_like_company_website(cand):
                ex.website = cand
    if not ex.website:
        for a in soup.find_all("a", href=True):
            cand = tidy_website(a["href"])
            if looks_like_company_website(cand):
                ex.website = cand
                break

    full_text = soup.get_text("\n")

    # --- booth ---
    if not ex.booth:
        ex.booth = extract_booth(full_text)

    # --- address ---
    if not ex.address:
        node = (soup.find("address")
                or soup.find(attrs={"class": re.compile(r"address|location|contact-info", re.I)}))
        if node:
            ex.address = clean(node.get_text(" "))
    if not ex.address:
        m = re.search(r"Address\s*:?\s*\n?(.{10,300})", full_text, re.I)
        if m:
            ex.address = clean(m.group(1).split("\n")[0])

    # --- overview / description ---
    if not ex.overview:
        node = soup.find(attrs={"class": re.compile(
            r"overview|description|about|company-profile|profile-text", re.I)})
        if node:
            ex.overview = clean(node.get_text(" "))
    if not ex.overview:
        m = soup.find("meta", attrs={"name": "description"}) or \
            soup.find("meta", attrs={"property": "og:description"})
        if m and m.get("content"):
            ex.overview = clean(m["content"])
    if not ex.overview:
        paras = [clean(p.get_text(" ")) for p in soup.find_all("p")]
        paras = [p for p in paras if len(p) > 80]
        if paras:
            ex.overview = max(paras, key=len)
    if len(ex.overview) > 4000:
        ex.overview = ex.overview[:4000] + "..."

    # --- contact extras ---
    if not ex.email:
        m = EMAIL_RE.search(full_text)
        if m:
            ex.email = m.group(0)
    if not ex.phone:
        m = PHONE_RE.search(full_text)
        if m:
            ex.phone = clean(m.group(0))

    # --- country, best effort from the tail of the address ---
    if not ex.country and ex.address:
        tail = ex.address.split(",")[-1].strip()
        if 2 <= len(tail) <= 30 and not any(ch.isdigit() for ch in tail):
            ex.country = tail


def enrich(fetcher: Fetcher, ex: Exhibitor) -> None:
    if ex.ebooth_url:
        html = fetcher.get(ex.ebooth_url)
        if html:
            parse_ebooth(html, ex.ebooth_url, ex)

    # exhibitors.aspx explicitly had no <a> for this row (confirmed real case,
    # e.g. "ADVANCED SYSTEM IN PACKAGE TECHNOLOGIES" / booth 760). Don't burn
    # requests guessing a slug for a page that was never linked in the first
    # place -- leave website/overview empty and let the report surface it.
    if ex.portal_status == "no-portal-listed" and not ex.portal_url:
        return

    guessed = False
    if not ex.portal_url:
        ex.portal_url = PORTAL_BASE + slugify(ex.name)
        guessed = True

    html = fetcher.get(ex.portal_url)
    if html:
        parse_portal(html, ex.portal_url, ex)
        ex.portal_status = "slug-guessed" if guessed else "ok"
        return

    # Slug guess missed -- try a couple of common variants before giving up.
    if guessed:
        base = slugify(ex.name)
        variants = [
            re.sub(r"-(pvt|private)-(ltd|limited)$", "", base),
            re.sub(r"-(ltd|limited|inc|llp|llc|gmbh)$", "", base),
            base.replace("-and-", "-"),
        ]
        for v in dict.fromkeys(variants):
            if not v or v == base:
                continue
            url = PORTAL_BASE + v
            html = fetcher.get(url)
            if html:
                ex.portal_url = url
                parse_portal(html, url, ex)
                ex.portal_status = "slug-guessed"
                return

    ex.portal_status = "404"


# --------------------------------------------------------------------------
# MERGE
# --------------------------------------------------------------------------

def merge(groups: Iterable[list[Exhibitor]]) -> list[Exhibitor]:
    merged: dict[str, Exhibitor] = {}
    dropped_listings = 0
    dupes = 0

    for group in groups:
        for ex in group:
            if is_listing(ex.name):
                dropped_listings += 1
                continue
            k = ex.key()
            if not k:
                dropped_listings += 1
                continue
            if k not in merged:
                merged[k] = ex
                continue
            dupes += 1
            cur = merged[k]
            # same company in two halls -> keep both hall ids and both booths
            for hall in ex.halls:
                if hall not in cur.halls:
                    cur.halls.append(hall)
            if ex.booth and ex.booth not in cur.booth:
                cur.booth = f"{cur.booth} / {ex.booth}" if cur.booth else ex.booth
            for fld in ("website", "address", "overview", "portal_url",
                        "ebooth_url", "email", "phone", "country"):
                if not getattr(cur, fld) and getattr(ex, fld):
                    setattr(cur, fld, getattr(ex, fld))

    log(f"[merge] {len(merged)} unique | {dupes} duplicate rows folded "
        f"| {dropped_listings} listing/non-company rows skipped")
    return sorted(merged.values(), key=lambda e: e.name.lower())


# --------------------------------------------------------------------------
# OUTPUT + COUNTERS
# --------------------------------------------------------------------------

CSV_FIELDS = ["name", "booth", "website", "address", "overview", "country",
              "email", "phone", "halls", "portal_url", "portal_status",
              "ebooth_url", "source"]


def write_outputs(rows: list[Exhibitor]) -> str:
    os.makedirs(OUT_DIR, exist_ok=True)

    with open(os.path.join(OUT_DIR, "exhibitors.csv"), "w",
              newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=CSV_FIELDS, extrasaction="ignore")
        w.writeheader()
        for ex in rows:
            d = asdict(ex)
            d["halls"] = "|".join(str(h) for h in ex.halls)
            w.writerow(d)

    with open(os.path.join(OUT_DIR, "exhibitors.json"), "w", encoding="utf-8") as fh:
        json.dump([asdict(e) for e in rows], fh, indent=2, ensure_ascii=False)

    report = build_report(rows)
    with open(os.path.join(OUT_DIR, "report.txt"), "w", encoding="utf-8") as fh:
        fh.write(report)
    return report


def build_report(rows: list[Exhibitor]) -> str:
    total = len(rows)
    if not total:
        return "No exhibitors collected.\n"

    tracked = ["name", "booth", "website", "address", "overview",
               "country", "email", "phone"]
    missing = {f: sum(1 for e in rows if not getattr(e, f).strip()) for f in tracked}

    lines = []
    lines.append("=" * 62)
    lines.append(f"SEMICON {SHOW_SLUG} -- EXHIBITOR COMPLETENESS REPORT")
    lines.append("=" * 62)
    lines.append(f"Total unique exhibitors: {total}")
    lines.append("")
    lines.append(f"{'FIELD':<12}{'FILLED':>8}{'EMPTY':>8}{'% FILLED':>10}")
    lines.append("-" * 62)
    for f in tracked:
        filled = total - missing[f]
        star = "  <- key" if f in ("name", "booth", "website") else ""
        lines.append(f"{f:<12}{filled:>8}{missing[f]:>8}{filled / total * 100:>9.1f}%{star}")
    lines.append("-" * 62)

    core = sum(1 for e in rows if e.name.strip() and e.booth.strip() and e.website.strip())
    lines.append(f"Complete on all 3 key fields (name+booth+website): "
                 f"{core} / {total}  ({core / total * 100:.1f}%)")
    lines.append("")

    by_hall: dict[str, int] = {}
    for e in rows:
        k = "|".join(str(h) for h in e.halls) or "unknown"
        by_hall[k] = by_hall.get(k, 0) + 1
    lines.append("By hall (mapid):")
    for k, v in sorted(by_hall.items()):
        lines.append(f"  {k:<14} {v}")
    lines.append("")

    by_status: dict[str, int] = {}
    for e in rows:
        by_status[e.portal_status or "skipped"] = by_status.get(e.portal_status or "skipped", 0) + 1
    lines.append("Portal page resolution:")
    for k, v in sorted(by_status.items()):
        lines.append(f"  {k:<14} {v}")
    lines.append("")

    no_site = [e.name for e in rows if not e.website.strip()][:25]
    if no_site:
        lines.append(f"Missing website ({missing['website']} total), first 25:")
        for n in no_site:
            lines.append(f"  - {n}")
        lines.append("")

    no_booth = [e.name for e in rows if not e.booth.strip()][:25]
    if no_booth:
        lines.append(f"Missing booth ({missing['booth']} total), first 25:")
        for n in no_booth:
            lines.append(f"  - {n}")
        lines.append("")

    # Guards against the kind of collision seen in the source data itself,
    # e.g. two unrelated companies resolving to the same portal_url.
    by_url: dict[str, list[str]] = {}
    for e in rows:
        if e.portal_url:
            by_url.setdefault(e.portal_url, []).append(e.name)
    collisions = {u: names for u, names in by_url.items() if len(names) > 1}
    if collisions:
        lines.append(f"Possible portal_url collisions ({len(collisions)}) -- "
                      f"check these manually:")
        for u, names in list(collisions.items())[:15]:
            lines.append(f"  {u}")
            for n in names:
                lines.append(f"      - {n}")
        lines.append("")

    no_link = sum(1 for e in rows if e.portal_status == "no-portal-listed")
    if no_link:
        lines.append(f"Rows with no portal link in the source at all: {no_link} "
                      f"(website/overview will be empty for these by design)")
        lines.append("")

    lines.append("=" * 62)
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# MAIN
# --------------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="SEMICON India 2026 exhibitor scraper")
    ap.add_argument("--halls", type=int, nargs="*", default=None,
                    help=f"hall map IDs (default {MAP_IDS})")
    ap.add_argument("--probe-halls", action="store_true",
                    help="scan a range of mapids and use every valid one")
    ap.add_argument("--skip-exhibitors-page", action="store_true",
                    help="go straight to per-hall maps")
    ap.add_argument("--no-portal", action="store_true",
                    help="stage 1 only: name + booth, no enrichment")
    ap.add_argument("--limit", type=int, default=0, help="enrich only N records")
    ap.add_argument("--delay", type=float, default=REQUEST_DELAY)
    ap.add_argument("--debug-dump", action="store_true",
                    help="save raw HTML of discovery pages to debug/")
    args = ap.parse_args(argv)

    fetcher = Fetcher(delay=args.delay, debug=args.debug_dump)
    groups: list[list[Exhibitor]] = []

    # ---- STAGE 1 ----
    if not args.skip_exhibitors_page:
        rows = try_exhibitors_page(fetcher)
        if rows:
            groups.append(rows)

    if not groups:
        log("[discovery] falling back to per-hall EventMap scraping")
        halls = args.halls if args.halls else (
            probe_halls(fetcher) if args.probe_halls else MAP_IDS)
        for mid in halls:
            rows = scrape_eventmap(fetcher, mid)
            if rows:
                groups.append(rows)
    elif args.probe_halls or args.halls:
        # Cross-check: make sure the flat list isn't missing a hall.
        halls = args.halls if args.halls else probe_halls(fetcher)
        for mid in halls:
            rows = scrape_eventmap(fetcher, mid)
            if rows:
                groups.append(rows)

    exhibitors = merge(groups)
    if not exhibitors:
        log("No exhibitors found. Run with --debug-dump and inspect debug/*.html "
            "-- the markup has probably changed or the list is JS-rendered.")
        return 1

    # ---- STAGE 2 ----
    if not args.no_portal:
        targets = exhibitors[:args.limit] if args.limit else exhibitors
        log(f"[enrich] fetching detail for {len(targets)} companies")
        for i, ex in enumerate(targets, 1):
            try:
                enrich(fetcher, ex)
            except Exception as exc:
                log(f"  ! enrich failed for {ex.name}: {exc}")
                ex.portal_status = "error"
            if i % 25 == 0 or i == len(targets):
                log(f"  {i}/{len(targets)}")
    else:
        for ex in exhibitors:
            ex.portal_status = "skipped"

    report = write_outputs(exhibitors)
    log("")
    log(report)
    log(f"HTTP: {fetcher.live_count} live, {fetcher.cache_count} cached, "
        f"{fetcher.fail_count} failed")
    log(f"Wrote {OUT_DIR}/exhibitors.csv, .json and report.txt")
    return 0


if __name__ == "__main__":
    sys.exit(main())