"""Merge the same company's records across multiple expo ingestion runs.

The same supplier legitimately shows up once per SEMICON show it exhibits at
- SEMI's directory is per-show, not global. Once you ingest more than one
  expo, "Advantest Co., Ltd." from Taiwan and "Advantest Co, Ltd" from Japan
  need to become one searchable record with two shows attached, not two
  separate hits competing for the same query.

Design stance, and why: the only automatic merge key is the company name
after conservative normalisation (legal-suffix stripping, punctuation and
whitespace only). No fuzzy/edit-distance matching, and no automatic merging
on shared website domain. Both would catch more true duplicates, but both
also silently merge distinct regional subsidiaries that should stay separate
- "ASML Korea" and "ASML Netherlands BV" can legitimately share asml.com and
a near-identical name while being different legal entities with different
HQs. An unmerged duplicate costs a repeated row a buyer can spot and ignore;
a bad merge costs a real company's distinct HQ or offering silently, which
is worse and harder to notice. Shared-website pairs that don't already match
on normalised name are surfaced in a review report instead of auto-merged -
see `dedupe()`'s second return value.
"""

import json
import re
import unicodedata
from collections import defaultdict
from urllib.parse import urlparse

FALLBACK_ABOUT = "Semiconductor technology and equipment supplier."
UNKNOWN_COUNTRY = "Unknown"

# Legal-entity suffixes to strip from the END of a normalised name, repeatedly
# (handles "XYZ Co., Ltd." -> strips "ltd" then "co"). Ordered roughly by
# frequency in the SEMI exhibitor lists seen so far (US/EU/JP/KR/CN/TW/IN).
# Deliberately does NOT include geographic words ("china", "korea", "europe",
# "(shanghai)", "asia pacific") - those distinguish real subsidiaries and
# stripping them is exactly the over-merge risk described above.
_LEGAL_SUFFIXES = [
    "incorporated", "corporation", "company", "limited", "co kg", "kabushiki kaisha",
    "sdn bhd", "pte ltd", "pvt ltd", "private limited", "s r l", "s p a", "s a",
    "b v", "n v", "a s", "a g", "gmbh co kg", "gmbh", "kk", "plc", "llp", "llc",
    "lp", "ag", "sa", "srl", "spa", "bv", "nv", "oy", "ab", "as", "kg",
    "inc", "corp", "co", "ltd",
]
# Longest-first so "co kg" strips before a lone trailing "co" would.
_LEGAL_SUFFIXES.sort(key=len, reverse=True)
_SUFFIX_RE = re.compile(
    r"(?:\s+(?:" + "|".join(re.escape(s) for s in _LEGAL_SUFFIXES) + r")\.?)+$"
)

# Generic multi-tenant domains that would make website-matching meaningless
# if they ever showed up in a "website" field (they shouldn't, but cheap to
# guard against a bad scrape).
_GENERIC_DOMAINS = {"gmail.com", "yahoo.com", "hotmail.com", "outlook.com",
                     "qq.com", "163.com", "126.com", "foxmail.com", "naver.com"}


def normalise_name(name):
    """Canonical merge key: lowercase, ASCII-normalise punctuation, strip
    legal-entity suffixes, collapse whitespace. Deterministic and reversible
    enough to explain - if two names normalise the same, a human looking at
    both should agree they're the same company on sight.
    """
    if not name:
        return ""
    # NFKC folds full-width CJK punctuation (，Ｌｔｄ．) to ASCII equivalents,
    # which shows up in the China/Japan/Korea listings.
    s = unicodedata.normalize("NFKC", name).lower()
    s = re.sub(r"[.,()\[\]{}'\"]", " ", s)
    s = re.sub(r"[&/]", " and ", s)
    s = re.sub(r"\s+", " ", s).strip()
    prev = None
    while prev != s:
        prev = s
        s = _SUFFIX_RE.sub("", s).strip()
    return s


def website_domain(url):
    """Bare registrable-ish domain for comparison, or None. Strips scheme,
    www, path, port. Not a full public-suffix-list implementation - good
    enough to compare "asml.com" to "www.asml.com/en" without pulling in a
    dependency for it.
    """
    if not url:
        return None
    url = url.strip()
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", url):
        url = "http://" + url
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return None
    host = host[4:] if host.startswith("www.") else host
    if not host or host in _GENERIC_DOMAINS:
        return None
    return host


def _better_about(a, b):
    a_real = bool(a) and a != FALLBACK_ABOUT
    b_real = bool(b) and b != FALLBACK_ABOUT
    if a_real and b_real:
        return a if len(a) >= len(b) else b
    return a if a_real else (b if b_real else (a or b or FALLBACK_ABOUT))


def _merge_two(base, other):
    """Fold `other` into `base` in place-ish (returns a new dict)."""
    merged = dict(base)

    merged["about"] = _better_about(base.get("about"), other.get("about"))

    # HQ: prefer a known one; if both are known and disagree, keep the first
    # but record the conflict rather than silently picking a side.
    b_hq, o_hq = base.get("hq_country") or UNKNOWN_COUNTRY, other.get("hq_country") or UNKNOWN_COUNTRY
    if b_hq == UNKNOWN_COUNTRY and o_hq != UNKNOWN_COUNTRY:
        merged["hq_country"] = o_hq
        merged["hq_location"] = other.get("hq_location")
    elif b_hq != UNKNOWN_COUNTRY and o_hq != UNKNOWN_COUNTRY and b_hq != o_hq:
        conflicts = set(merged.get("hq_country_conflict", [b_hq]))
        conflicts.add(o_hq)
        merged["hq_country_conflict"] = sorted(conflicts)

    merged["website"] = base.get("website") or other.get("website")

    # Category union, de-duplicated by id, order-preserving.
    def union_ids(key_ids, key_names):
        seen, ids, names = set(), [], []
        for src in (base, other):
            for i, n in zip(src.get(key_ids, []), src.get(key_names, [])):
                if i not in seen:
                    seen.add(i)
                    ids.append(i)
                    names.append(n)
        return ids, names

    merged["cat_l1_ids"], merged["cat_l1_names"] = union_ids("cat_l1_ids", "cat_l1_names")
    merged["cat_l2_ids"], merged["cat_l2_names"] = union_ids("cat_l2_ids", "cat_l2_names")

    seen_l1 = {n.get("l1_id") for n in merged.get("cat_tree", [])}
    tree = list(merged.get("cat_tree", []))
    for node in other.get("cat_tree", []):
        if node.get("l1_id") in seen_l1:
            existing = next(n for n in tree if n.get("l1_id") == node.get("l1_id"))
            have = {c["id"] for c in existing.get("children", [])}
            for c in node.get("children", []):
                if c["id"] not in have:
                    existing.setdefault("children", []).append(c)
                    have.add(c["id"])
        else:
            tree.append(node)
            seen_l1.add(node.get("l1_id"))
    merged["cat_tree"] = tree

    merged["locations"] = list(dict.fromkeys(base.get("locations", []) + other.get("locations", [])))
    merged["sources"] = base.get("sources", []) + other.get("sources", [])

    return merged


def dedupe(records):
    """records: list of per-show ingestion dicts, each with a single-string
    `location` (as produced by run_ingestion) or already-merged dicts with a
    `locations` list (idempotent - safe to re-run on already-deduped data).

    Returns (merged_records, review_candidates).
      merged_records: one dict per canonical company. `location` is replaced
        by `locations` (list of every expo it was seen at) and `sources`
        (list of {location, ebooth_url} for traceability back to the
        original listing).
      review_candidates: pairs that share a website domain but did NOT match
        on normalised name - surfaced for a human to decide, never merged
        automatically. Each entry is {domain, companies: [name, ...]}.
    """
    buckets = defaultdict(list)
    for r in records:
        r = dict(r)
        if "locations" not in r:
            loc = r.pop("location", None)
            r["locations"] = [loc] if loc else []
            r["sources"] = [{"location": loc, "ebooth_url": r.get("ebooth_url")}] if loc else []
        key = normalise_name(r.get("company_name", ""))
        buckets[key].append(r)

    merged = []
    for key, group in buckets.items():
        if not key:
            merged.extend(group)  # can't safely key an empty/garbage name - keep separate
            continue
        acc = group[0]
        for other in group[1:]:
            acc = _merge_two(acc, other)
        merged.append(acc)

    # Website-domain collisions across DIFFERENT normalised names -> report only.
    domain_to_names = defaultdict(set)
    for r in merged:
        dom = website_domain(r.get("website"))
        if dom:
            domain_to_names[dom].add(r.get("company_name", ""))
    review = [{"domain": dom, "companies": sorted(names)}
              for dom, names in domain_to_names.items() if len(names) > 1]

    merged.sort(key=lambda r: (r.get("company_name") or "").lower())
    return merged, review


def dedupe_files(input_paths, output_path, review_path=None):
    """Convenience wrapper: load N per-show JSON files, merge, write one
    combined `semi_suppliers.json` (the shape SourcingSearchEngine expects)
    plus an optional review report."""
    records = []
    for p in input_paths:
        with open(p) as f:
            records.extend(json.load(f))

    merged, review = dedupe(records)

    with open(output_path, "w") as f:
        json.dump(merged, f, indent=2, ensure_ascii=False)

    n_in, n_out = len(records), len(merged)
    print(f"dedupe: {n_in} source rows across {len(input_paths)} shows -> "
          f"{n_out} companies ({n_in - n_out} merged)")
    conflicts = [r for r in merged if r.get("hq_country_conflict")]
    if conflicts:
        print(f"  {len(conflicts)} companies have a conflicting HQ country "
              f"across shows - see 'hq_country_conflict' in the output")
    if review:
        print(f"  {len(review)} website-domain collisions need manual review"
              + (f" -> {review_path}" if review_path else ""))
        if review_path:
            with open(review_path, "w") as f:
                json.dump(review, f, indent=2, ensure_ascii=False)

    return merged, review

if __name__ == "__main__":
    import glob
    import os

    raw_dir = "data/raw"
    raw_files = sorted(glob.glob(os.path.join(raw_dir, "*.json")))

    if not raw_files:
        print("No raw JSON files found in data/raw/")
    else:
        out_path = "data/semi_suppliers.json"
        rev_path = "data/dedupe_review.json"
        os.makedirs("data", exist_ok=True)
        dedupe_files(raw_files, out_path, rev_path)