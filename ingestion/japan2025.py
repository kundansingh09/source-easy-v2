#!/usr/bin/env python3
"""Exhibitor scraper for SEMI / a2z expo sites (built for SEMICON Japan 2025).

Flow
  1. Directory : {base}exhibitors.aspx?langID=2           -> every eBooth link (BoothID)
  2. Profile   : {base}eBooth.aspx?BoothID=<id>&langID=2  -> name, booth, location, website,
                 categories (ids + names), overview
  3. Language  : the site's own toggle() uses langID=2 for English and langID=1 for Japanese
                 (the old script asked for langID=1, i.e. Japanese). English mode returns
                 English category labels. Overviews an exhibitor only wrote in Japanese stay
                 Japanese, so ONLY those go through Google Translate (deep-translator),
                 cached on disk so nothing is ever translated twice.
  4. Output    : <out>/<show>.jsonl (checkpoint, resumable) -> <show>.json + <show>.csv

Usage
  pip install requests beautifulsoup4 deep-translator
  python semi_scraper.py japan2025 --limit 3 --dump-html debug   # smoke test first
  python semi_scraper.py japan2025                               # full run, resumable
"""
import argparse
import csv
import hashlib
import json
import random
import re
import sys
import time
import urllib.parse
import urllib.robotparser
from pathlib import Path

import requests
from bs4 import BeautifulSoup, Comment

LANG_EN, LANG_JA = 2, 1

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

CJK = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uff00-\uffef]")
KANA = re.compile(r"[\u3040-\u30ff]")
BOOTH_ID = re.compile(r"BoothID=(\d+)", re.I)
BOOTH_LABEL = re.compile(r"(?:Booth|小間番号)\s*[:：]?\s*([A-Za-z0-9][A-Za-z0-9\-]*)", re.I)
URLISH = re.compile(r"(?i)^(https?://|www\.)")
SOCIAL = ("linkedin.", "facebook.", "twitter.", "x.com", "youtube.", "instagram.")
INLINE = ["strong", "b", "em", "i", "u", "span", "font", "a", "small", "sup", "sub", "mark"]
TAB_LABELS = {"home", "products", "ホーム", "出展製品", "製品"}
END_LABELS = {"products", "出展製品", "categories", "カテゴリ", "カテゴリー"}
MIN_ABOUT = 8


# ------------------------------------------------------------------ http

class Client:
    def __init__(self, delay):
        self.s = requests.Session()
        self.s.headers.update(HEADERS)
        self.delay = delay

    def get(self, url, retries=3):
        last = None
        for attempt in range(retries):
            try:
                r = self.s.get(url, timeout=25)
                if r.status_code in (429, 500, 502, 503, 504):
                    raise requests.HTTPError(f"HTTP {r.status_code}")
                r.raise_for_status()
                return r
            except requests.RequestException as e:
                last = e
                time.sleep(2 ** attempt + random.random())
        raise last

    def pause(self):
        time.sleep(random.uniform(0.6 * self.delay, 1.6 * self.delay))


def robots_allows(client, base, paths):
    """Politeness check. Missing/unreadable robots.txt counts as allowed."""
    root = "/".join(base.split("/")[:3])
    try:
        r = client.s.get(root + "/robots.txt", timeout=15)
        if r.status_code != 200:
            return True
        rp = urllib.robotparser.RobotFileParser()
        rp.parse(r.text.splitlines())
        return all(rp.can_fetch(HEADERS["User-Agent"], base + p) for p in paths)
    except requests.RequestException:
        return True


# ------------------------------------------------------------- directory

def fetch_directory(client, base, lang):
    soup = BeautifulSoup(client.get(f"{base}exhibitors.aspx?langID={lang}").content, "html.parser")
    rows = {}
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "ebooth.aspx" not in href.lower() or "task=" in href.lower():
            continue
        m = BOOTH_ID.search(href)
        name = a.get_text(" ", strip=True)
        if m and name:
            rows.setdefault(m.group(1), name)  # dedupe by BoothID, not by name
    return [{"booth_id": k, "company_name": v} for k, v in rows.items()]


# ---------------------------------------------------------------- parsing

def english_half(label):
    """'205 装置、… / Nanotechnology Equipment' -> '205 Nanotechnology Equipment'.

    Only used when the page came back bilingual (Japanese mode). Picks the first ' / '
    where the right-hand side is free of Japanese; leaves pure-English labels untouched.
    """
    if not CJK.search(label):
        return label
    code = re.match(r"^(\d{3})\s", label)
    for m in re.finditer(r"\s*/\s*(?=[A-Za-z0-9])", label):
        left, right = label[:m.start()], label[m.end():].strip()
        if CJK.search(left) and right and not CJK.search(right):
            return f"{code.group(1)} {right}" if code else right
    return label


def extract_categories(soup):
    """Category tree with ids: [{l1_id, l1_name, children:[{id, name}]}]."""
    tree, current = [], None
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "exhibitors.aspx" not in href.lower():
            continue
        label = a.get_text(" ", strip=True)
        if not label:
            continue
        query = urllib.parse.urlparse(href.replace(" ", "")).query
        qs = {k.lower(): v for k, v in urllib.parse.parse_qs(query).items()}
        if "subcatid" in qs:
            cid = int(re.sub(r"\D", "", qs["subcatid"][0]) or 0)
            if not cid:
                continue
            if current is None:
                current = {"l1_id": None, "l1_name": "Uncategorized", "children": []}
                tree.append(current)
            if cid not in [c["id"] for c in current["children"]]:
                current["children"].append({"id": cid, "name": label})
        elif "catid" in qs:
            cid = int(re.sub(r"\D", "", qs["catid"][0]) or 0)
            if not cid:
                continue
            current = next((n for n in tree if n["l1_id"] == cid), None)
            if current is None:
                current = {"l1_id": cid, "l1_name": label, "children": []}
                tree.append(current)
    return tree


def extract_website(soup):
    """Link text carries the real URL; the href is a Boothurl.aspx redirector."""
    def usable(a):
        text = a.get_text(strip=True)
        low = text.lower()
        return (URLISH.match(text) and "semi.org" not in low and "a2zinc" not in low
                and not any(s in low for s in SOCIAL))

    links = [a for a in soup.find_all("a", href=True) if usable(a)]
    links.sort(key=lambda a: "boothurl.aspx" not in a["href"].lower())  # redirector links first
    if not links:
        return None
    site = links[0].get_text(strip=True)
    return site if site.lower().startswith("http") else "https://" + site


def extract_location(h1):
    """Best effort: the two text lines right under the company name (city/region, country)."""
    if h1 is None:
        return None, None
    lines = []
    for s in h1.find_all_next(string=True, limit=14):
        if isinstance(s, Comment) or h1 in s.parents:  # next_elements also walks into the h1 itself
            continue
        t = " ".join(str(s).split())
        if not t:
            continue
        if URLISH.match(t) or BOOTH_LABEL.search(t) or t.lower() in TAB_LABELS or t == "#":
            break
        lines.append(t)
        if len(lines) == 2:
            break
    if len(lines) == 2:
        return dedupe_location(lines[0]), lines[1]
    return (dedupe_location(lines[0]) if lines else None), None


def dedupe_location(raw):
    parts = [p.strip() for p in str(raw).split(",") if p.strip()]
    return ", ".join(dict.fromkeys(parts)) or None


def _lines(node):
    """Text lines of a node. Inline tags are unwrapped so sentences stay in one piece."""
    for tag in node.find_all(INLINE):
        tag.unwrap()
    node.smooth()
    out = []
    for s in node.find_all(string=True):
        if isinstance(s, Comment):
            continue
        t = " ".join(str(s).split())
        if t:
            out.append(t)
    return out


def _clean_overview(lines):
    i = 0
    while i < len(lines) and lines[i].lower() in TAB_LABELS | {"#"}:
        i += 1
    kept = []
    for l in lines[i:]:
        if l.lower() in END_LABELS:
            break
        kept.append(l)
    text = "\n".join(kept).strip()
    return text if len(text) >= MIN_ABOUT else None


def extract_overview(soup, name):
    """Overview text: tab pane #Home if present, else the text between the tabs and 'Products'."""
    pane = soup.find(id=re.compile(r"^home$", re.I))
    if pane is not None:
        text = _clean_overview(_lines(pane))
        if text:
            return text
    lines = _lines(soup)
    start = 0
    if name:
        norm_name = " ".join(name.split())
        start = next((i + 1 for i, l in enumerate(lines) if l == norm_name), 0)
    tab = next((i for i in range(start, len(lines)) if lines[i].lower() in ("home", "ホーム")), None)
    if tab is None:
        tab = next((i for i in range(start, len(lines)) if BOOTH_LABEL.search(lines[i])), start)
    return _clean_overview(lines[tab + 1:])


def parse_profile(content):
    soup = BeautifulSoup(content, "html.parser")
    for t in soup(["script", "style", "noscript"]):
        t.decompose()
    text = soup.get_text(" ", strip=True)
    h1 = next((h for h in soup.find_all("h1") if h.get_text(strip=True)), None)
    name = " ".join(h1.get_text(" ", strip=True).split()) if h1 else None
    booth = BOOTH_LABEL.search(text)
    city, country = extract_location(h1)
    website = extract_website(soup)
    tree = extract_categories(soup)
    about = extract_overview(soup, name)  # last: it rewrites the soup (unwraps inline tags)
    return {
        "profile_name": name,
        "booth": booth.group(1) if booth else None,
        "city_region": city,
        "country": country,
        "website": website,
        "tree": tree,
        "about": about,
        "page_lang": "ja" if re.search(r"小間番号|ホーム", text) else "en",
    }


# ------------------------------------------------------------- translation

def chunk_text(text, limit=800):
    """deep-translator sends a GET; Japanese chars URL-encode to 9 bytes each, so keep chunks small."""
    parts, cur = [], ""
    for seg in re.split(r"(?<=[。．！？!?\n])", text):
        if cur and len(cur) + len(seg) > limit:
            parts.append(cur)
            cur = ""
        while len(seg) > limit:
            parts.append(seg[:limit])
            seg = seg[limit:]
        cur += seg
    if cur:
        parts.append(cur)
    return [p for p in parts if p.strip()]


class Translator:
    """Google Translate via deep-translator, skip-if-English, cached on disk."""

    def __init__(self, cache_path, delay=0.4):
        try:
            from deep_translator import GoogleTranslator
        except ImportError:
            sys.exit("Missing dependency: pip install deep-translator")
        self.engines = {
            "ja": GoogleTranslator(source="ja", target="en"),
            "auto": GoogleTranslator(source="auto", target="en"),  # kana-less CJK, e.g. Chinese
        }
        self.path, self.delay, self.dirty = Path(cache_path), delay, 0
        self.cache = json.loads(self.path.read_text("utf-8")) if self.path.exists() else {}

    def _call(self, engine, part):
        for attempt in range(4):
            try:
                return engine.translate(part) or ""
            except Exception as e:  # network, throttling, bad payload
                print(f"    [!] translate retry {attempt + 1}: {type(e).__name__}", file=sys.stderr)
                time.sleep(2 ** attempt + random.random())
        return None

    def translate(self, text):
        """English text; original if it has no CJK; None if Google kept failing."""
        text = (text or "").strip()
        if not text or not CJK.search(text):
            return text
        key = hashlib.sha1(text.encode("utf-8")).hexdigest()
        if key in self.cache:
            return self.cache[key]
        engine = self.engines["ja" if KANA.search(text) else "auto"]
        pieces = []
        for part in chunk_text(text):
            res = self._call(engine, part)
            if res is None:
                return None
            pieces.append(res.strip())
            time.sleep(self.delay)
        result = "\n".join(pieces) if "\n" in text else " ".join(pieces)
        self.cache[key] = result
        self.dirty += 1
        if self.dirty >= 20:
            self.save()
        return result

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.cache, ensure_ascii=False), "utf-8")
        self.dirty = 0


# ------------------------------------------------------------ one company

def norm(s):
    return re.sub(r"[\W_]+", "", (s or "").casefold())


def names_match(a, b):
    a, b = norm(a), norm(b)
    return bool(a and b and (a == b or a in b or b in a))


def scrape_one(client, base, row, lang, tr, dump_dir):
    bid, dir_name = row["booth_id"], row["company_name"]
    urls = [f"{base}eBooth.aspx?BoothID={bid}&langID={lang}", f"{base}eBooth.aspx?BoothID={bid}"]
    warnings, chosen = [], None
    for url in urls:
        try:
            r = client.get(url)
        except requests.RequestException as e:
            warnings.append(f"fetch_error:{type(e).__name__}")
            continue
        m = BOOTH_ID.search(r.url)
        if m and m.group(1) != bid:  # the odd redirect-to-another-company case
            warnings.append(f"redirected_to_booth_{m.group(1)}")
            continue
        parsed = parse_profile(r.content)
        if chosen is None or (parsed["page_lang"] == "en" and chosen[0]["page_lang"] != "en"):
            chosen = (parsed, url, r)
        if parsed["page_lang"] == "en":
            break
        client.pause()
    if chosen is None:
        return None
    parsed, url, r = chosen

    if dump_dir:
        Path(dump_dir).mkdir(parents=True, exist_ok=True)
        (Path(dump_dir) / f"{bid}.html").write_bytes(r.content)
    if parsed["page_lang"] == "ja":
        warnings.append("page_not_english")
    pname = parsed["profile_name"]
    if pname and not CJK.search(pname) and not CJK.search(dir_name) and not names_match(pname, dir_name):
        warnings.append(f"name_mismatch:{pname}")

    # categories: English already in langID=2; split/translate only if the page was bilingual
    def label(s):
        s = english_half(s)
        return (tr.translate(s) or s) if CJK.search(s) else s

    tree = [{"l1_id": n["l1_id"], "l1_name": label(n["l1_name"]),
             "children": [{"id": c["id"], "name": label(c["name"])} for c in n["children"]]}
            for n in parsed["tree"]]
    l2 = {}
    for n in tree:
        for c in n["children"]:
            l2.setdefault(c["id"], c["name"])

    about = parsed["about"]
    about_en, translated = about, False
    if about and CJK.search(about):
        about_en = tr.translate(about)
        translated = about_en is not None
        if about_en is None:
            warnings.append("translation_failed")

    return {
        "booth_id": bid,
        "company_name": dir_name,
        "booth": parsed["booth"],
        "city_region": parsed["city_region"],
        "country": parsed["country"],
        "website": parsed["website"],
        "cat_l1_ids": [n["l1_id"] for n in tree if n["l1_id"] is not None],
        "cat_l1_names": [n["l1_name"] for n in tree if n["l1_id"] is not None],
        "cat_l2_ids": list(l2),
        "cat_l2_names": list(l2.values()),
        "cat_tree": tree,
        "about_original": about,
        "about_en": about_en,
        "about_translated": translated,
        "page_lang": parsed["page_lang"],
        "url": url,
        "warnings": warnings,
    }


# ------------------------------------------------------------------ output

CSV_COLUMNS = [
    ("booth_id", "Booth ID"), ("company_name", "Company"), ("booth", "Booth"),
    ("city_region", "City / Region"), ("country", "Country"), ("website", "Website"),
    ("cat_l1_names", "L1 Categories"), ("cat_l2_names", "L2 Categories"),
    ("about_en", "About (English)"), ("about_original", "About (Original)"),
    ("about_translated", "Machine translated"), ("page_lang", "Page language"),
    ("url", "URL"), ("warnings", "Warnings"),
]


def export(records, out_dir, slug):
    (out_dir / f"{slug}.json").write_text(json.dumps(records, indent=2, ensure_ascii=False), "utf-8")
    with open(out_dir / f"{slug}.csv", "w", newline="", encoding="utf-8-sig") as f:  # BOM: Excel-safe
        w = csv.writer(f)
        w.writerow([h for _, h in CSV_COLUMNS])
        for r in records:
            w.writerow([" | ".join(map(str, r[k])) if isinstance(r[k], list) else r[k]
                        for k, _ in CSV_COLUMNS])


def load_checkpoint(path):
    recs = {}
    if path.exists():
        for line in path.read_text("utf-8").splitlines():
            if line.strip():
                r = json.loads(line)
                recs[r["booth_id"]] = r
    return recs


def rewrite_checkpoint(path, recs):
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in recs.values()), "utf-8")


# ------------------------------------------------------------------ driver

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("show", help="show slug in the URL, e.g. japan2025")
    ap.add_argument("--limit", type=int, default=0, help="only the first N exhibitors (0 = all)")
    ap.add_argument("--lang", type=int, default=LANG_EN, help="langID (2 = English, 1 = Japanese)")
    ap.add_argument("--out", default="data/raw", help="output folder")
    ap.add_argument("--delay", type=float, default=1.0, help="base seconds between requests")
    ap.add_argument("--dump-html", metavar="DIR", help="save raw profile HTML here (debugging)")
    args = ap.parse_args()

    base = f"https://expo.semi.org/{args.show}/Public/"
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    client = Client(args.delay)
    tr = Translator(out_dir / "translation_cache.json")

    if not robots_allows(client, base, ["exhibitors.aspx", "eBooth.aspx"]):
        sys.exit("robots.txt disallows these paths - stopping.")

    directory = fetch_directory(client, base, args.lang)
    if not directory:
        sys.exit("No eBooth links found on the directory page - check the show slug / open it in a browser.")
    print(f"[{args.show}] directory: {len(directory)} exhibitors")
    if args.limit:
        directory = directory[:args.limit]

    ckpt = out_dir / f"{args.show}.jsonl"
    records = load_checkpoint(ckpt)
    todo = [r for r in directory if r["booth_id"] not in records]
    print(f"already done: {len(directory) - len(todo)}, to fetch: {len(todo)}")

    failed = []
    try:
        for i, row in enumerate(todo, 1):
            rec = scrape_one(client, base, row, args.lang, tr, args.dump_html)
            if rec is None:
                failed.append(row["booth_id"])
                print(f"[{i}/{len(todo)}] {row['company_name']}: FETCH FAILED (will retry next run)")
            else:
                records[row["booth_id"]] = rec
                with open(ckpt, "a", encoding="utf-8") as f:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                tag = "tr" if rec["about_translated"] else ("en" if rec["about_en"] else "none")
                print(f"[{i}/{len(todo)}] {row['company_name']} | page={rec['page_lang']} "
                      f"cats={len(rec['cat_l2_ids'])} about={tag} site={'y' if rec['website'] else 'n'}"
                      + (f" | {rec['warnings']}" if rec["warnings"] else ""))
            client.pause()
    except KeyboardInterrupt:
        print("\ninterrupted - exporting what we have (rerun to continue)")

    # retry translations that failed earlier (uses the original text already saved, no refetch)
    repaired = False
    for rec in records.values():
        if rec.get("about_original") and CJK.search(rec["about_original"]) and not rec.get("about_en"):
            new = tr.translate(rec["about_original"])
            if new:
                rec.update(about_en=new, about_translated=True,
                           warnings=[w for w in rec["warnings"] if w != "translation_failed"])
                repaired = True
    tr.save()
    if repaired:
        rewrite_checkpoint(ckpt, records)

    final = [records[r["booth_id"]] for r in directory if r["booth_id"] in records]
    export(final, out_dir, args.show)

    n = len(final)
    print(f"\nExported {n} records -> {out_dir / (args.show + '.json')} / .csv")
    print(f"  page still Japanese : {sum(r['page_lang'] == 'ja' for r in final)}")
    print(f"  machine-translated  : {sum(r['about_translated'] for r in final)}")
    print(f"  no overview         : {sum(not r['about_en'] for r in final)}")
    print(f"  no website          : {sum(not r['website'] for r in final)}")
    print(f"  no categories       : {sum(not r['cat_l2_ids'] for r in final)}")
    print(f"  with warnings       : {sum(bool(r['warnings']) for r in final)}")
    if failed:
        print(f"  fetch failures      : {len(failed)} (rerun to retry)")


if __name__ == "__main__":
    main()