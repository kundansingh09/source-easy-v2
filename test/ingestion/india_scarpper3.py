#!/usr/bin/env python3
"""
Stage-2-only scraper: visits every portal_url in an existing exhibitors CSV
and pulls exactly three fields off the company's own portal page:

    name       -- as the page itself displays it (may differ slightly from
                  the exhibitors.aspx spelling -- both are kept, see output)
    website    -- the company's OWN external site, as they listed it
    overview   -- their profile / about text, if present

Why this is a separate script from the discovery scraper: that one already
has portal_url for every row (guessed or confirmed) -- this one's only job
is to test each URL and extract cleanly, so you can re-run it on its own
without re-doing hall discovery every time.

--- Bug fixed vs. the previous run ---
The CSV you're feeding this in has a `website` column full of junk like
`https://Boothurl.aspx?BoothID=696000`. That happened because the old
website-extraction blindly prepended "https://" to ANY href, including a
relative in-page link ("Boothurl.aspx?..."), which is never a company
website. This script:
  1. never invents a scheme for a relative href -- if it's not already
     absolute, it's resolved against the actual page URL, and even then
     only kept if the resulting host is a real external domain
  2. explicitly rejects anything containing .aspx / Boothurl / eBooth /
     portal2026.semiconindia.org / expo.semi.org -- those are the show's
     own infrastructure, never the exhibitor's site
  3. prefers a link that sits near the text "website" / "visit website" /
     a globe icon, before falling back to "first plausible external link"

Usage
-----
    pip install requests beautifulsoup4 lxml

    python portal_visit.py exhibitors.csv                # full run
    python portal_visit.py exhibitors.csv --limit 20      # smoke test
    python portal_visit.py exhibitors.csv --debug-dump    # save raw HTML
    python portal_visit.py exhibitors.csv --retest-only   # skip rows whose
                                                            # portal_status
                                                            # was already "ok"
                                                            # in a prior run
Output
------
    out2/portal_results.csv
    out2/portal_results.json
    out2/report.txt
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
from dataclasses import dataclass, asdict
from typing import Optional
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# --------------------------------------------------------------------------

REQUEST_DELAY = 1.5
TIMEOUT = 30
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
)

OUT_DIR = "out2"
CACHE_DIR = "cache_portal"
DEBUG_DIR = "debug_portal"

# Never accept these as "the company's own website" -- they're the show's
# own infrastructure or generic social/media hosts, not a company profile.
BLOCKED_HOST_FRAGMENTS = [
    "semi.org", "semiconindia.org", "portal2026", "expo.semi.org",
    "facebook.com", "twitter.com", "x.com", "linkedin.com", "instagram.com",
    "youtube.com", "wechat.com", "weibo.com", "wa.me", "whatsapp.com",
    "maps.google", "goo.gl", "google.com/maps",
]
# Filename/path fragments that mean "this is show navigation, not a site"
BLOCKED_PATH_FRAGMENTS = [
    ".aspx", "boothurl", "ebooth", "eventmap", "javascript:", "mailto:",
    "tel:", "#",
]

WEBSITE_LABEL_RE = re.compile(r"\b(visit\s+website|website|company\s+website|www\b)", re.I)
OVERVIEW_LABEL_RE = re.compile(r"\b(overview|about\s+us|about|company\s+profile|profile)\b", re.I)


# --------------------------------------------------------------------------

@dataclass
class PortalResult:
    input_name: str = ""
    portal_url: str = ""
    http_status: str = ""          # ok / 404 / blocked / error / cached-empty
    page_name: str = ""
    website: str = ""
    overview: str = ""
    name_matches_input: str = ""   # yes/no/n-a, quick eyeball signal


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
    return re.sub(r"\s+", " ", s).strip()


def soup_of(html: str) -> BeautifulSoup:
    try:
        return BeautifulSoup(html, "lxml")
    except Exception:
        return BeautifulSoup(html, "html.parser")


class Fetcher:
    def __init__(self, delay=REQUEST_DELAY, debug=False):
        self.delay = delay
        self.debug = debug
        self.last_request = 0.0
        self.live = 0
        self.cached = 0
        self.failed = 0

        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        })
        retry = Retry(total=3, backoff_factor=1.5,
                       status_forcelist=[429, 500, 502, 503, 504],
                       allowed_methods=["GET"])
        self.session.mount("https://", HTTPAdapter(max_retries=retry))
        self.session.mount("http://", HTTPAdapter(max_retries=retry))

        os.makedirs(CACHE_DIR, exist_ok=True)
        if debug:
            os.makedirs(DEBUG_DIR, exist_ok=True)

    def get(self, url: str, label: str = "") -> tuple[Optional[str], str]:
        """Returns (html_or_None, status_label)."""
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
        if resp.status_code == 403:
            self.failed += 1
            return None, "blocked"
        if resp.status_code != 200:
            self.failed += 1
            return None, f"http-{resp.status_code}"

        html = resp.text
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(html)
        if self.debug and label:
            with open(os.path.join(DEBUG_DIR, f"{label}.html"), "w", encoding="utf-8") as fh:
                fh.write(html)
        return html, "ok"


# --------------------------------------------------------------------------
# EXTRACTION
# --------------------------------------------------------------------------

def is_blocked_url(url: str) -> bool:
    low = url.lower()
    if any(frag in low for frag in BLOCKED_PATH_FRAGMENTS):
        return True
    host = urlparse(url).netloc.lower()
    return any(frag in host for frag in BLOCKED_HOST_FRAGMENTS)


def resolve_and_validate(href: str, page_url: str) -> str:
    """
    The core fix: only ever return an absolute, external, non-infrastructure
    URL. A bare relative path (no scheme, no netloc after resolving against
    the page) is NEVER promoted into a fake https:// URL.
    """
    if not href:
        return ""
    href = href.strip()
    if is_blocked_url(href):
        return ""

    absolute = urljoin(page_url, href)
    parsed = urlparse(absolute)

    if parsed.scheme not in ("http", "https"):
        return ""
    if not parsed.netloc or "." not in parsed.netloc:
        return ""
    if is_blocked_url(absolute):
        return ""
    return absolute.rstrip("/")


def extract_name(soup: BeautifulSoup, page_url: str) -> str:
    for sel in ("h1", "h2"):
        tag = soup.find(sel)
        if tag:
            t = clean(tag.get_text(" "))
            if t and len(t) < 200:
                return t
    if soup.title:
        t = clean(soup.title.get_text(" "))
        # strip common " | Site Name" / " - Site Name" suffixes
        t = re.split(r"\s*[\|\u2013\-]\s*(semicon|portal)", t, flags=re.I)[0]
        return clean(t)
    return ""


def extract_website(soup: BeautifulSoup, page_url: str) -> str:
    # Pass 1: a link whose visible text or nearby label says "website"
    for a in soup.find_all("a", href=True):
        label_text = clean(a.get_text(" "))
        nearby = ""
        prev = a.find_previous(string=True)
        if prev:
            nearby = clean(str(prev))
        if WEBSITE_LABEL_RE.search(label_text) or WEBSITE_LABEL_RE.search(nearby):
            cand = resolve_and_validate(a["href"], page_url)
            if cand:
                return cand

    # Pass 2: an icon-only link (class mentions "web"/"globe"/"link")
    for a in soup.find_all("a", href=True):
        cls = " ".join(a.get("class", []))
        if re.search(r"web(site)?|globe|external-?link", cls, re.I):
            cand = resolve_and_validate(a["href"], page_url)
            if cand:
                return cand

    # Pass 3: first external, non-infrastructure absolute link on the page
    # (last resort -- least reliable, so it only fires if passes 1-2 found nothing)
    for a in soup.find_all("a", href=True):
        cand = resolve_and_validate(a["href"], page_url)
        if cand:
            return cand

    return ""


def extract_overview(soup: BeautifulSoup, page_url: str) -> str:
    node = soup.find(attrs={"class": re.compile(
        r"overview|about|company-profile|profile-text|description", re.I)})
    if node:
        t = clean(node.get_text(" "))
        if len(t) > 30:
            return t[:4000]

    # a heading literally saying "Overview" / "About Us", take the next block
    for tag in soup.find_all(["h2", "h3", "h4", "strong", "b"]):
        if OVERVIEW_LABEL_RE.search(clean(tag.get_text(" "))):
            nxt = tag.find_next(["p", "div"])
            if nxt:
                t = clean(nxt.get_text(" "))
                if len(t) > 30:
                    return t[:4000]

    meta = soup.find("meta", attrs={"name": "description"}) or \
        soup.find("meta", attrs={"property": "og:description"})
    if meta and meta.get("content"):
        t = clean(meta["content"])
        if len(t) > 30:
            return t

    paras = [clean(p.get_text(" ")) for p in soup.find_all("p")]
    paras = [p for p in paras if len(p) > 80]
    if paras:
        return max(paras, key=len)[:4000]

    return ""


def parse_portal_page(html: str, page_url: str, input_name: str) -> PortalResult:
    soup = soup_of(html)
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()

    r = PortalResult(input_name=input_name, portal_url=page_url, http_status="ok")
    r.page_name = extract_name(soup, page_url)
    r.website = extract_website(soup, page_url)
    r.overview = extract_overview(soup, page_url)

    if r.page_name:
        r.name_matches_input = "yes" if normalize_name(r.page_name) == normalize_name(input_name) else "no"
    else:
        r.name_matches_input = "n/a"
    return r


# --------------------------------------------------------------------------
# MAIN
# --------------------------------------------------------------------------

def load_rows(csv_path: str) -> list[dict]:
    with open(csv_path, "r", encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def write_outputs(results: list[PortalResult]) -> str:
    os.makedirs(OUT_DIR, exist_ok=True)
    fields = list(PortalResult.__dataclass_fields__.keys())

    with open(os.path.join(OUT_DIR, "portal_results.csv"), "w",
              newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        for r in results:
            w.writerow(asdict(r))

    with open(os.path.join(OUT_DIR, "portal_results.json"), "w", encoding="utf-8") as fh:
        json.dump([asdict(r) for r in results], fh, indent=2, ensure_ascii=False)

    report = build_report(results)
    with open(os.path.join(OUT_DIR, "report.txt"), "w", encoding="utf-8") as fh:
        fh.write(report)
    return report


def build_report(results: list[PortalResult]) -> str:
    total = len(results)
    if not total:
        return "No rows processed.\n"

    status_counts: dict[str, int] = {}
    for r in results:
        status_counts[r.http_status] = status_counts.get(r.http_status, 0) + 1

    empty_name = sum(1 for r in results if not r.page_name.strip())
    empty_website = sum(1 for r in results if not r.website.strip())
    empty_overview = sum(1 for r in results if not r.overview.strip())
    all_three_empty = sum(1 for r in results
                          if not r.page_name.strip() and not r.website.strip()
                          and not r.overview.strip())
    name_mismatch = sum(1 for r in results if r.name_matches_input == "no")

    lines = []
    lines.append("=" * 62)
    lines.append("PORTAL PAGE VISIT -- COMPLETENESS REPORT")
    lines.append("=" * 62)
    lines.append(f"Total rows processed: {total}")
    lines.append("")
    lines.append("Page fetch status:")
    for k, v in sorted(status_counts.items()):
        lines.append(f"  {k:<14} {v}  ({v/total*100:.1f}%)")
    lines.append("")
    lines.append(f"{'FIELD':<12}{'FILLED':>8}{'EMPTY':>8}{'% FILLED':>10}")
    lines.append("-" * 62)
    for label, empty in [("name", empty_name), ("website", empty_website),
                          ("overview", empty_overview)]:
        filled = total - empty
        lines.append(f"{label:<12}{filled:>8}{empty:>8}{filled/total*100:>9.1f}%")
    lines.append("-" * 62)
    lines.append(f"Rows with ALL THREE fields empty: {all_three_empty}")
    lines.append(f"Page name differs from CSV input name: {name_mismatch}")
    lines.append("")

    no_website = [r.input_name for r in results
                  if r.http_status == "ok" and not r.website.strip()][:30]
    if no_website:
        lines.append(f"Fetched OK but no website found ({empty_website} total), first 30:")
        for n in no_website:
            lines.append(f"  - {n}")
        lines.append("")

    dead = [r.input_name for r in results if r.http_status not in ("ok",)][:30]
    if dead:
        lines.append(f"Portal page did not resolve ({total - status_counts.get('ok', 0)} total), first 30:")
        for n in dead:
            lines.append(f"  - {n}")
        lines.append("")

    lines.append("=" * 62)
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Visit portal pages, extract name/website/overview")
    ap.add_argument("csv_path", help="exhibitors.csv with a portal_url column")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--delay", type=float, default=REQUEST_DELAY)
    ap.add_argument("--debug-dump", action="store_true")
    ap.add_argument("--retest-only", action="store_true",
                    help="skip rows whose CSV portal_status was already 'ok'")
    ap.add_argument("--url-column", default="portal_url")
    ap.add_argument("--name-column", default="name")
    args = ap.parse_args(argv)

    rows = load_rows(args.csv_path)
    if args.retest_only and "portal_status" in (rows[0].keys() if rows else []):
        before = len(rows)
        rows = [r for r in rows if r.get("portal_status") != "ok"]
        log(f"[filter] --retest-only: {before} -> {len(rows)} rows (dropped already-ok)")

    if args.limit:
        rows = rows[:args.limit]

    fetcher = Fetcher(delay=args.delay, debug=args.debug_dump)
    results: list[PortalResult] = []

    log(f"[run] visiting {len(rows)} portal pages")
    for i, row in enumerate(rows, 1):
        name = clean(row.get(args.name_column, ""))
        url = clean(row.get(args.url_column, ""))

        if not url:
            results.append(PortalResult(input_name=name, portal_url="",
                                        http_status="no-url-in-csv"))
            continue

        html, status = fetcher.get(url, label=f"row{i}")
        if not html:
            results.append(PortalResult(input_name=name, portal_url=url,
                                        http_status=status))
        else:
            results.append(parse_portal_page(html, url, name))

        if i % 25 == 0 or i == len(rows):
            log(f"  {i}/{len(rows)}  (live={fetcher.live} cached={fetcher.cached} failed={fetcher.failed})")

    report = write_outputs(results)
    log("")
    log(report)
    log(f"Wrote {OUT_DIR}/portal_results.csv, .json and report.txt")
    return 0


if __name__ == "__main__":
    sys.exit(main())