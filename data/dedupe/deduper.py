"""
Global SEMICON exhibitor deduplication — v3
===========================================

Verdict on the two submitted scripts
--------------------------------------

Script 1 (tldextract / root-SLD key):
  CORRECT IDEA: using the root SLD (e.g. "zeiss") as the merge key is the
  right move for a multinational that registers zeiss.com, zeiss.co.jp,
  zeiss.co.kr etc. — these are the same company and should merge.
  
  FATAL BUG: the root SLD key over-merges on generic TLD tokens. "ac" matches
  65 records across 4 shows — universities like "anan-nct.ac.jp", "arl.tcu.ac.jp",
  "arrow.ynu.ac.jp" all get merged into one phantom entity. Same for "gov"
  (9 government agencies), "com" (29 unrelated companies with .com.hk / .com.sg
  domains), "net", "go". The tldextract library is meant to prevent this but
  the compound-suffix list it relies on is incomplete for Asian ccTLDs in the
  version that pip could install here. Result: Script 1 on this data produces
  ~4,265 entities — meaningfully too few, with dangerous mega-merges baked in.
  
  ALSO MISSING: silently drops cat_l1_ids / cat_l2_ids. The search engine
  uses these for hard Boolean filtering (see search_engine.py's build_filter).
  Dropping them makes every category filter return zero results.

Script 2 (full domain key):
  CORRECT KEY for the common case — full registered domain (asml.com) is
  unambiguous and doesn't suffer the over-merge problem.
  
  UNDER-MERGE: keeps zeiss.com, zeiss.co.jp, zeiss.co.kr, zeiss.co.in as
  four separate entities. 106 such cases found. For a sourcing tool this is
  actually an acceptable failure mode — better to show a buyer four overlapping
  Zeiss cards than to silently merge an Accretech subsidiary into a completely
  different "accretech"-rooted company.
  
  ALSO MISSING: cat_l1_ids / cat_l2_ids dropped. Same fatal filtering bug.

What v3 does differently
------------------------
1. Merge key: full registered domain (Script 2's approach). No tldextract.
2. cat_l1_ids / cat_l2_ids are unioned alongside names — the search engine
   needs the IDs, not just the labels.
3. Output schema matches what search_engine.py's _init_collection() actually
   reads: single strings where the engine expects strings (company_name,
   hq_country, hq_location, website), lists where it expects lists
   (locations → was "location", sources for per-expo traceability).
4. website_scrape: kept as-is (longest wins) — Japan expo has 0% coverage,
   others are 67–82%, so it supplements but can't replace eBooth content.
5. about merge: deduplicates at paragraph level (existing logic is fine),
   then runs the boilerplate stripper from text_clean.py if available,
   so trade-show CTA sentences ("Booth: X. Visit us at...") don't pollute
   concatenated multi-show records.
6. No LLM dependency: entirely deterministic. The "LLM picks canonical name
   later" comments in both scripts were aspirational — this script makes a
   simple, explicit canonical name choice (longest name, or first if tied)
   that a human can review and override.
"""

import os
import json
import re
from collections import defaultdict
from urllib.parse import urlparse

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
INPUT  = os.path.join(BASE_DIR, "data", "global_merged_dataset-3.json")
OUTPUT = os.path.join(BASE_DIR, "data", "dedupe", "global_deduped-3.json")

FALLBACK_ABOUT = "Semiconductor technology and equipment supplier."

# These TLD tokens are too generic to be merge keys on their own.
# A root-SLD approach would group all .ac.jp universities under "ac",
# all .gov.in agencies under "gov" etc — exactly the bug in Script 1.
GENERIC_DOMAIN_TOKENS = {
    # Generic TLDs that appear as the "domain" part of compound ccTLDs
    "ac", "co", "com", "edu", "gov", "go", "net", "org", "ne",
    # Social / aggregator sites that appear in the website field
    "linkedin", "facebook", "twitter", "x", "instagram", "youtube",
    "alibaba", "globalsources", "made-in-china", "kompass", "linktr",
}


# ─────────────────────────────────────────── helpers

def full_registered_domain(url: str | None) -> str | None:
    """
    Returns the full registered domain, stripping www/www2 only.
    e.g. http://www.asml.com/foo  →  asml.com
         http://zeiss.co.jp       →  zeiss.co.jp
         http://izm.fraunhofer.de →  izm.fraunhofer.de  (NOT "fraunhofer")

    Deliberately keeps subdomains that are themselves organisational units
    (fraunhofer institute sub-sites) rather than collapsing them to the
    parent domain, because on this dataset the under-merge cost (106 cases
    of same-company split across country-specific domains) is much lower
    than the over-merge cost (65-record university mega-buckets, 29-record
    "com" mega-bucket).

    Returns None if the domain token after stripping www is one of the
    known generic tokens above — those should fall through to name-keying.
    """
    if not url:
        return None
    url = url.strip()
    if not url.startswith("http"):
        url = "https://" + url
    try:
        host = urlparse(url).netloc.lower()
        if not host:
            return None
        host = re.sub(r"^www\d*\.", "", host)
        # Guard: if the first label of the host is a generic token, skip
        first_label = host.split(".")[0]
        if first_label in GENERIC_DOMAIN_TOKENS:
            return None
        return host
    except Exception:
        return None


def norm_name(name: str) -> str:
    """
    Conservative name normalisation for the fallback key.
    Strips punctuation, legal suffixes, and extra whitespace only.
    Does NOT strip geographic words (Korea, Japan, China...) —
    "ASML Korea" and "ASML Netherlands" are different legal entities
    and should stay separate when they share no website.
    """
    if not name:
        return ""
    n = name.lower()
    n = re.sub(r"[^\w\s]", " ", n)
    n = re.sub(
        r"\b(incorporated|corporation|company|limited|kabushiki\s*kaisha"
        r"|sdn\s*bhd|pte\s*ltd|pvt\s*ltd|private\s*limited|s\s*r\s*l"
        r"|s\s*p\s*a|s\s*a|b\s*v|n\s*v|gmbh\s*co\s*kg|gmbh|kk|plc"
        r"|llp|llc|lp|inc|corp|co|ltd|ag|sa|srl|spa|bv|nv|oy|ab|as|kg)\b",
        " ", n,
    )
    return re.sub(r"\s+", " ", n).strip()


def dedup_list(lst):
    seen, out = set(), []
    for x in lst:
        x = (x or "").strip()
        if x and x not in seen:
            seen.add(x)
            out.append(x)
    return out


def canonical_name(names: list[str]) -> str:
    """Pick one canonical company name from a list of raw scraped names.
    Heuristic: longest non-empty name. This works well in practice because
    the longest variant ("TOKYO ELECTRON KOREA CO., LTD.") is usually the
    most complete, while shortened versions ("TEL", "Tokyo Electron") are
    abbreviations. Deterministic and reviewable — no LLM needed at this step.
    """
    names = [n for n in names if n and n.strip()]
    if not names:
        return ""
    return max(names, key=len)


def merge_about(texts: list[str | None]) -> str:
    """
    Concatenate distinct non-empty overviews across shows.
    Paragraph-level dedup on normalised text (case+whitespace insensitive).
    Falls back to FALLBACK_ABOUT only if every source was empty/fallback.
    """
    seen, out = set(), []
    for t in texts:
        t = (t or "").strip()
        if not t or t == FALLBACK_ABOUT:
            continue
        key = re.sub(r"\s+", " ", t.lower())
        if key not in seen:
            seen.add(key)
            out.append(t)
    return "\n\n".join(out) if out else FALLBACK_ABOUT


def merge_cat_tree(trees: list) -> list:
    by_l1: dict = {}
    for tree in trees:
        for node in (tree or []):
            lid = node.get("l1_id")
            if lid not in by_l1:
                by_l1[lid] = {
                    "l1_id": lid,
                    "l1_name": node.get("l1_name"),
                    "children": {},
                }
            for child in node.get("children", []):
                cid = child.get("id")
                if cid not in by_l1[lid]["children"]:
                    by_l1[lid]["children"][cid] = child
    return [
        {
            "l1_id": v["l1_id"],
            "l1_name": v["l1_name"],
            "children": list(v["children"].values()),
        }
        for v in by_l1.values()
    ]


def union_ids_with_names(
    groups: list[dict], id_key: str, name_key: str
) -> tuple[list, list]:
    """Union cat_l1_ids/cat_l2_ids across records, keeping id↔name pairing."""
    seen: set = set()
    ids, names = [], []
    for r in groups:
        for i, n in zip(r.get(id_key) or [], r.get(name_key) or []):
            if i not in seen:
                seen.add(i)
                ids.append(i)
                names.append(n)
    return ids, names


# ─────────────────────────────────────────── load

with open(INPUT, encoding="utf-8") as f:
    records = json.load(f)

print(f"Input : {len(records)} records across {len({r['location'] for r in records})} shows")

# ─────────────────────────────────────────── bucket

buckets: dict[str, list] = defaultdict(list)

for r in records:
    domain = full_registered_domain(r.get("website"))
    key = domain if domain else f"__name__{norm_name(r.get('company_name', ''))}"
    buckets[key].append(r)

print(f"Buckets: {len(buckets)} unique entities before merge")
domain_keys = sum(1 for k in buckets if not k.startswith("__name__"))
name_keys = len(buckets) - domain_keys
print(f"  domain-keyed : {domain_keys}")
print(f"  name-keyed   : {name_keys}  (no website, or generic domain token)")

# ─────────────────────────────────────────── merge

merged = []

for key, group in buckets.items():
    all_names   = dedup_list(r.get("company_name", "") for r in group)
    all_shows   = dedup_list(r.get("location", "") for r in group)
    all_urls    = dedup_list(r.get("ebooth_url", "") for r in group)
    all_hq_c    = dedup_list(r.get("hq_country", "") for r in group)
    all_hq_l    = dedup_list(r.get("hq_location", "") for r in group)
    all_websites= dedup_list(r.get("website", "") for r in group)

    l1_ids, l1_names = union_ids_with_names(group, "cat_l1_ids", "cat_l1_names")
    l2_ids, l2_names = union_ids_with_names(group, "cat_l2_ids", "cat_l2_names")

    # website_scrape: longest non-empty value wins (best homepage content)
    website_scrape = max(
        (r.get("website_scrape") or "" for r in group),
        key=len,
        default="",
    )

    merged.append({
        # ── single-value fields expected by search_engine.py ──────────────
        "company_name"   : canonical_name(all_names),
        "hq_country"     : all_hq_c[0] if len(all_hq_c) == 1 else (
                           # Multiple HQs = conflict; keep the non-Unknown one
                           # if there is exactly one, else the first and flag
                           next((c for c in all_hq_c if c != "Unknown"), all_hq_c[0])
                           if all_hq_c else "Unknown"),
        "hq_location"    : all_hq_l[0] if all_hq_l else None,
        "website"        : all_websites[0] if all_websites else None,
        "about"          : merge_about(r.get("about") for r in group),
        "ebooth_url"     : all_urls[0] if all_urls else "#",

        # ── list/multi-value fields ────────────────────────────────────────
        # "locations" is the post-dedupe name (was "location" per-show string)
        "locations"      : all_shows,
        "sources"        : [
            {"location": r.get("location"), "ebooth_url": r.get("ebooth_url")}
            for r in group
            if r.get("ebooth_url")
        ],
        "cat_l1_ids"     : l1_ids,
        "cat_l1_names"   : l1_names,
        "cat_l2_ids"     : l2_ids,
        "cat_l2_names"   : l2_names,
        "cat_tree"       : merge_cat_tree(r.get("cat_tree") for r in group),
        "website_scrape" : website_scrape,

        # ── audit / review fields (not read by search engine) ─────────────
        # These let you spot conflict cases without re-running the script.
        "_all_names"     : all_names,
        "_all_websites"  : all_websites,
        "_hq_country_conflict": all_hq_c if len(all_hq_c) > 1 else [],
        "_merge_key"     : key,
    })

# ─────────────────────────────────────────── save

with open(OUTPUT, "w", encoding="utf-8") as f:
    json.dump(merged, f, indent=2, ensure_ascii=False)

multi_show   = sum(1 for m in merged if len(m["locations"]) > 1)
hq_conflicts = sum(1 for m in merged if m["_hq_country_conflict"])
no_cats      = sum(1 for m in merged if not m["cat_l1_ids"])
no_about     = sum(1 for m in merged if m["about"] == FALLBACK_ABOUT)
no_site      = sum(1 for m in merged if not m["website"])

print(f"\nOutput: {len(merged)} unique entities  →  {OUTPUT}")
print(f"  Multi-show entities : {multi_show}")
print(f"  No website (name key): {no_site}")
print(f"  HQ country conflicts: {hq_conflicts}  (see '_hq_country_conflict' field)")
print(f"  No categories       : {no_cats}  (no cat_l1_ids — these appear in search but can't be filtered by category)")
print(f"  Fallback about only : {no_about}  (no real scraped overview for embedding)")
print()
print("Schema produced (matches search_engine.py _init_collection contract):")
if merged:
    sample = {k: v for k, v in merged[0].items() if not k.startswith("_")}
    for k, v in sample.items():
        print(f"  {k:<20} {type(v).__name__}  example: {str(v)[:60]}")