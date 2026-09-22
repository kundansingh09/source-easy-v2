#!/usr/bin/env python3
"""
Refine global_deduped.json in place with LLM-extracted semiconductor intelligence.

Each record in the input JSON has inline content (about, website_scrape, snippet).
This script adds a `refined` sub-object to each record and writes the result back
to a new file (or the same file if --in-place is passed), leaving all other fields
(locations, sources, cat_tree, etc.) exactly as they are.

Architecture differences from the old CSV-based refine.py
----------------------------------------------------------
- No positional alignment: content travels with each record, not in parallel CSVs.
- Output is merged back into the source JSON, not a separate out_llm/ directory.
- Category IDs are never sent to the LLM (not meaningful to it). Category NAMES
  are included as a short hint (capped at 8 entries), since "Die Bonding Equipment"
  tells the model more than any free-text summary for thin records.
- india_angle is broadened to regional_presence: a dict of country/region -> one
  sentence. More useful for a 6-show global dataset than a hardcoded India field.
- Cache key is (company_name + content hash), so a record whose content was updated
  (e.g. website_scrape added) is re-processed, not served stale from cache.

Usage
-----
    export OPENAI_API_KEY=sk-...

    # Always dry-run first - confirms field detection with no API cost
    python refine.py --input global_deduped.json --dry-run

    # Smoke test on 10 records
    python refine.py --input global_deduped.json --limit 10

    # Full run, output to global_refined.json
    python refine.py --input global_deduped.json --output global_refined.json

    # Full run, overwrite in place
    python refine.py --input global_deduped.json --in-place

    # Resume after a crash - already-cached records are free
    python refine.py --input global_deduped.json --output global_refined.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, asdict
from typing import Optional

import requests

# ─────────────────────────────────────────────────────────── config

API_URL   = "https://api.openai.com/v1/chat/completions"
MODEL           = "gpt-4o-mini"   # used when about >= ABOUT_SPARSE_THRESHOLD
SPARSE_MODEL    = "gpt-4o"        # used when about < ABOUT_SPARSE_THRESHOLD (sparse input)
MAX_TOKENS = 1024
TEMPERATURE = 0.2
REASONING_PREFIXES = ("o1", "o3", "o4")

MAX_RETRIES         = 4
RETRY_BACKOFF_BASE  = 2.0
DEFAULT_CONCURRENCY = 5
CACHE_DIR           = "cache_refine_v3"

# Routing threshold: records with about < ABOUT_SPARSE_THRESHOLD chars are "sparse" —
# mini first compresses all raw content into a focused 200-word summary, then 4o
# receives that clean summary and enriches it with its own domain knowledge.
# Records with rich about go directly to mini (single-stage, cheaper).
ABOUT_SPARSE_THRESHOLD = 500

ABOUT_MAX_CHARS        = 2000
WEBSITE_MAX_CHARS      = 1500
SNIPPET_MAX_CHARS      = 800
CAT_NAMES_MAX          = 8     # max category names sent as context hint

REQUIRED_FIELDS = [
    "value_chain_position", "semiconductor_relevance", "summary",
    "capabilities", "technical_expertise", "end_markets", "regional_presence",
]
LIST_FIELDS = {"capabilities", "technical_expertise", "end_markets"}

# ─────────────────────────────────────────────────────────── prompts

SYSTEM_PROMPT = """\
You are a semiconductor industry analyst with deep expertise across the full
value chain — from upstream materials and equipment to design, fabrication,
packaging, and test.

Return ONLY a single JSON object. No markdown fences, no preamble, no text
before or after the JSON.\
"""

# Stage-1 prompt (mini): compress raw content into a tight semiconductor-focused summary.
# Used only for sparse records (about < ABOUT_SPARSE_THRESHOLD).
SUMMARISE_PROMPT_TEMPLATE = """\
You are a semiconductor industry analyst. Summarise the following raw content about \
a SEMICON exhibitor into a concise 150-200 word paragraph focused exclusively on what \
the company makes or does in the semiconductor value chain. Discard generic marketing \
language, boilerplate, navigation text, and anything unrelated to semiconductors. \
Preserve specific product names, process technologies, materials, and market segments.

COMPANY: {name}
CATEGORY HINTS: {category_hints}
SEMICON CONTENT: {about}
WEBSITE CONTENT: {website_scrape}
SNIPPET: {snippet}

Return plain text only — no JSON, no bullet points, no headers.\
"""

USER_PROMPT_TEMPLATE = """\
Extract structured semiconductor-industry intelligence about this SEMICON exhibitor.

SOURCE HIERARCHY (trust in this order):
1. Semicon directory content  — authoritative for semiconductor scope
2. Website scrape             — may be generic homepage, use with caution
3. Snippet                    — brief excerpt, fill gaps only

COMPANY: {name}
CATEGORY HINTS: {category_hints}
SEMICON CONTENT: {about}
WEBSITE CONTENT: {website_scrape}
SNIPPET: {snippet}

INSTRUCTIONS:
- Use provided content as your primary source. Supplement with your own knowledge
  ONLY when content is sparse or generic, and flag every such addition with
  "(inferred)" in the field value.
- Do not hallucinate capabilities. If the company is not meaningfully a
  semiconductor supplier, say so clearly in semiconductor_relevance.
- For capabilities: be specific. "CMP slurry optimised for sub-7nm back-end
  dielectric" is useful. "Semiconductor materials" is not.
- For technical_expertise: name underlying technical DOMAINS, not product names.
  E.g. "thermocompression bonding", "ultra-high vacuum systems", "RISC-V
  microarchitecture". Only list domains where this company has genuine depth;
  do not pad with generic entries.

Return a JSON object with exactly these fields:

{{
  "value_chain_position": "<one of: Materials | Equipment | EDA/IP | Fab/Foundry | OSAT/Packaging | Test & Measurement | Design Services | Chemicals & Gases | Facility & Infrastructure | Distribution | Other>",
  "semiconductor_relevance": "<High | Medium | Low | None> — one sentence explaining why",
  "summary": "<2-3 sentences describing what this company does in the semiconductor value chain, written for a procurement or sourcing professional>",
  "capabilities": ["<2-4 specific capabilities or product lines>"],
  "technical_expertise": ["<key underlying technical domains where the company has genuine depth>"],
  "end_markets": ["<target segments, e.g. Logic, Memory, Power, RF/5G, Automotive, Advanced Packaging, HPC, MEMS>"],
  "regional_presence": {{
    "<country or region name>": "<one sentence on presence, manufacturing, or strategy there>",
    ...
  }}
}}

Rules for regional_presence:
- Include a country/region only when the content explicitly mentions it in the
  context of manufacturing, sales offices, key customers, or strategic investment.
- Do not infer presence from company name alone (e.g. "ASML Korea" does not
  automatically mean Korea has a manufacturing presence — it may just be a sales
  entity).
- Typical entries: "India", "Taiwan", "Japan", "Korea", "China", "Europe",
  "United States". Use the same level of specificity the content uses.
- If no regional specifics are mentioned anywhere, return an empty object {{}}.
- Flag any entry derived from your own knowledge, not the content, with "(inferred)".

If content is empty or irrelevant across all fields, return all fields as null
except value_chain_position set to "Other" and regional_presence as {{}}.
"""


# ─────────────────────────────────────────────────────────── data model

@dataclass
class CompanyInput:
    record_index: int
    company_name: str
    about: str
    website_scrape: str
    snippet: str
    category_names: list[str]
    content_hash: str    # sha1 of all content; cache busts when content changes
    use_two_stage: bool = False  # True → mini summarise then 4o enrich


@dataclass
class RefinedResult:
    record_index: int = 0
    company_name: str = ""
    value_chain_position: Optional[str] = None
    semiconductor_relevance: Optional[str] = None
    summary: Optional[str] = None
    capabilities: list = field(default_factory=list)
    technical_expertise: list = field(default_factory=list)
    end_markets: list = field(default_factory=list)
    regional_presence: dict = field(default_factory=dict)
    status: str = ""
    inferred_field_count: int = 0
    raw_response: str = ""
    model: str = MODEL


# ─────────────────────────────────────────────────────────── helpers

def log(msg: str) -> None:
    print(msg, flush=True)


def truncate(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0] + " ...[truncated]"


def content_hash(parts: list[str]) -> str:
    joined = "\n".join(p or "" for p in parts)
    return hashlib.sha1(joined.encode("utf-8")).hexdigest()[:20]


def cache_path(name: str, chash: str) -> str:
    key = f"{name}:{chash}"
    h = hashlib.sha1(key.encode("utf-8")).hexdigest()[:24]
    return os.path.join(CACHE_DIR, f"{h}.json")


def strip_fences(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


def count_inferred(record: dict) -> int:
    n = 0
    for v in record.values():
        if isinstance(v, str) and "(inferred)" in v.lower():
            n += 1
        elif isinstance(v, list):
            n += sum(1 for item in v if isinstance(item, str) and "(inferred)" in item.lower())
        elif isinstance(v, dict):
            n += sum(1 for item in v.values() if isinstance(item, str) and "(inferred)" in item.lower())
    return n


# ─────────────────────────────────────────────────────────── loading

FALLBACK_ABOUT = "Semiconductor technology and equipment supplier."


def build_inputs(records: list[dict]) -> list[CompanyInput]:
    inputs = []
    for i, r in enumerate(records):
        # Skip records that already have a valid refined block (resume logic).
        # Delete the cache file manually to force a re-process.
        name = r.get("company_name") or (r.get("_all_names") or [None])[0] or f"record_{i}"

        about_raw = (r.get("about") or "").strip()
        about     = "" if about_raw == FALLBACK_ABOUT else about_raw
        snippet   = (r.get("snippet") or "").strip()
        ws        = (r.get("website_scrape") or "").strip()

        # Routing: sparse about → two-stage (mini summarise → 4o enrich)
        #          rich about   → single-stage mini directly (website_scrape skipped)
        use_two_stage = len(about) < ABOUT_SPARSE_THRESHOLD
        if not use_two_stage:
            ws = ""  # about is rich enough; skip noisy homepage content

        cat_names = list(dict.fromkeys(
            (r.get("cat_l1_names") or []) + (r.get("cat_l2_names") or [])
        ))[:CAT_NAMES_MAX]

        chash = content_hash([about, ws, snippet])
        inputs.append(CompanyInput(
            record_index=i,
            company_name=name,
            about=about,
            website_scrape=ws,
            snippet=snippet,
            category_names=cat_names,
            content_hash=chash,
            use_two_stage=use_two_stage,
        ))
    return inputs


# ─────────────────────────────────────────────────────────── LLM

class RateLimiter:
    def __init__(self, min_interval: float):
        self.min_interval = min_interval
        self.lock = threading.Lock()
        self.last = 0.0

    def wait(self):
        with self.lock:
            now = time.time()
            wait = self.min_interval - (now - self.last)
            if wait > 0:
                time.sleep(wait)
            self.last = time.time()


def call_openai(session: requests.Session, api_key: str, model: str,
                prompt: str, limiter: RateLimiter) -> tuple[Optional[str], str]:
    is_reasoning = model.startswith(REASONING_PREFIXES)
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "response_format": {"type": "json_object"},
        "max_completion_tokens": MAX_TOKENS,
    }
    if not is_reasoning:
        body["temperature"] = TEMPERATURE

    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    for attempt in range(1, MAX_RETRIES + 1):
        limiter.wait()
        try:
            resp = session.post(API_URL, headers=headers, json=body, timeout=60)
        except requests.RequestException as exc:
            if attempt == MAX_RETRIES:
                return None, f"network-error: {exc}"
            time.sleep(RETRY_BACKOFF_BASE ** attempt)
            continue

        if resp.status_code == 200:
            try:
                text = resp.json()["choices"][0]["message"]["content"] or ""
            except (KeyError, IndexError):
                return None, "api-error-unexpected-shape"
            return text, "ok"

        if resp.status_code in (429, 500, 502, 503):
            if attempt == MAX_RETRIES:
                return None, f"api-error-{resp.status_code}"
            time.sleep(RETRY_BACKOFF_BASE ** attempt)
            continue

        return None, f"api-error-{resp.status_code}: {resp.text[:300]}"

    return None, "api-error-exhausted-retries"


def parse_response(raw: str) -> tuple[Optional[dict], str]:
    cleaned = strip_fences(raw)
    try:
        obj = json.loads(cleaned)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", cleaned, re.S)
        if not m:
            return None, "parse-error"
        try:
            obj = json.loads(m.group(0))
        except json.JSONDecodeError:
            return None, "parse-error"

    for f in REQUIRED_FIELDS:
        if f not in obj:
            if f in LIST_FIELDS:
                obj[f] = []
            elif f == "regional_presence":
                obj[f] = {}
            else:
                obj[f] = None
        elif f in LIST_FIELDS and not isinstance(obj[f], list):
            obj[f] = [obj[f]] if obj[f] else []
        elif f == "regional_presence" and not isinstance(obj[f], dict):
            obj[f] = {}
    return obj, "ok"


# ─────────────────────────────────────────────────────────── per-company

def build_summarise_prompt(ci: CompanyInput) -> str:
    cat_hint = ", ".join(ci.category_names) if ci.category_names else "(none)"
    return SUMMARISE_PROMPT_TEMPLATE.format(
        name=ci.company_name or "(unknown)",
        category_hints=cat_hint,
        about=truncate(ci.about, ABOUT_MAX_CHARS) or "(none provided)",
        website_scrape=truncate(ci.website_scrape, WEBSITE_MAX_CHARS) or "(none provided)",
        snippet=truncate(ci.snippet, SNIPPET_MAX_CHARS) or "(none provided)",
    )


def build_prompt(ci: CompanyInput, override_about: str = "") -> str:
    cat_hint = ", ".join(ci.category_names) if ci.category_names else "(none)"
    about_text = override_about or truncate(ci.about, ABOUT_MAX_CHARS) or "(none provided)"
    return USER_PROMPT_TEMPLATE.format(
        name=ci.company_name or "(unknown)",
        category_hints=cat_hint,
        about=about_text,
        website_scrape="(none provided)" if override_about else (truncate(ci.website_scrape, WEBSITE_MAX_CHARS) or "(none provided)"),
        snippet=truncate(ci.snippet, SNIPPET_MAX_CHARS) or "(none provided)",
    )


def process(ci: CompanyInput, session: requests.Session, api_key: str,
            model: str, limiter: RateLimiter) -> RefinedResult:
    os.makedirs(CACHE_DIR, exist_ok=True)
    cpath = cache_path(ci.company_name, ci.content_hash)

    # NEW:
    if os.path.exists(cpath):
        with open(cpath, encoding="utf-8") as f:
            cached_data = json.load(f)
            if cached_data.get("status") in ("ok", "empty-input"):
                return RefinedResult(**cached_data)

    result = RefinedResult(record_index=ci.record_index,
                           company_name=ci.company_name, model=model)

    if not ci.about and not ci.website_scrape and not ci.snippet:
        result.status = "empty-input"
        result.value_chain_position = "Other"
        _cache(cpath, result)
        return result

    # Two-stage path: mini compresses raw content → 4o enriches from clean summary
    if ci.use_two_stage:
        summary_raw, sum_status = call_openai(
            session, api_key, MODEL, build_summarise_prompt(ci), limiter
        )
        if summary_raw is None:
            result.status = f"summarise-failed:{sum_status}"
            _cache(cpath, result)
            return result
        compressed_summary = summary_raw.strip()
        enrich_prompt = build_prompt(ci, override_about=compressed_summary)
        raw, call_status = call_openai(session, api_key, SPARSE_MODEL, enrich_prompt, limiter)
        result.model = f"{MODEL}+{SPARSE_MODEL}"
    else:
        raw, call_status = call_openai(session, api_key, MODEL, build_prompt(ci), limiter)

    if raw is None:
        result.status = call_status
        _cache(cpath, result)
        return result

    parsed, _ = parse_response(raw)
    if parsed is None:
        result.status = "parse-error"
        result.raw_response = raw[:2000]
        _cache(cpath, result)
        return result

    result.value_chain_position  = parsed.get("value_chain_position")
    result.semiconductor_relevance = parsed.get("semiconductor_relevance")
    result.summary               = parsed.get("summary")
    result.capabilities          = parsed.get("capabilities") or []
    result.technical_expertise   = parsed.get("technical_expertise") or []
    result.end_markets           = parsed.get("end_markets") or []
    result.regional_presence     = parsed.get("regional_presence") or {}
    result.status                = "ok"
    result.inferred_field_count  = count_inferred(parsed)
    _cache(cpath, result)
    return result


def _cache(path: str, r: RefinedResult) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(asdict(r), f, ensure_ascii=False)


# ─────────────────────────────────────────────────────────── merge + write

def merge_into_records(records: list[dict], results: list[RefinedResult]) -> list[dict]:
    """Write LLM output back into the source record as a `refined` sub-object.
    All original fields (locations, sources, cat_tree, cat_l1_ids, etc.) are
    preserved unchanged. Only `refined` is added/replaced.
    """
    out = []
    for r, res in zip(records, results):
        record = dict(r)
        refined = {
            "value_chain_position"  : res.value_chain_position,
            "semiconductor_relevance": res.semiconductor_relevance,
            "summary"               : res.summary,
            "capabilities"          : res.capabilities,
            "technical_expertise"   : res.technical_expertise,
            "end_markets"           : res.end_markets,
            "regional_presence"     : res.regional_presence,
            "_status"               : res.status,
            "_model"                : res.model,
            "_inferred_fields"      : res.inferred_field_count,
        }
        if res.raw_response:
            refined["_raw_response"] = res.raw_response
        record["refined"] = refined
        out.append(record)
    return out


def build_report(results: list[RefinedResult]) -> str:
    total = len(results)
    if not total:
        return "No records processed.\n"

    status_c: dict[str, int] = {}
    for r in results:
        status_c[r.status] = status_c.get(r.status, 0) + 1

    ok = [r for r in results if r.status == "ok"]
    rel_c: dict[str, int] = {}
    for r in ok:
        tokens = (r.semiconductor_relevance or "").split()
        key = tokens[0] if tokens else "unset"
        rel_c[key] = rel_c.get(key, 0) + 1

    lines = ["=" * 60, "REFINE RUN REPORT", "=" * 60,
             f"Total records : {total}", ""]
    lines.append("Status:")
    for k, v in sorted(status_c.items()):
        lines.append(f"  {k:<28} {v:5d}  ({v/total*100:.1f}%)")
    if ok:
        lines += ["", f"Of {len(ok)} successful:"]
        lines.append(f"  empty capabilities       {sum(1 for r in ok if not r.capabilities)}")
        lines.append(f"  empty summary            {sum(1 for r in ok if not (r.summary or '').strip())}")
        inferred = sum(r.inferred_field_count for r in ok)
        lines.append(f"  total (inferred) fields  {inferred}")
        has_regional = sum(1 for r in ok if r.regional_presence)
        lines.append(f"  with regional_presence   {has_regional}")
        lines += ["", "semiconductor_relevance:"]
        for k, v in sorted(rel_c.items(), key=lambda kv: -kv[1]):
            lines.append(f"  {k:<10} {v:5d}  ({v/len(ok)*100:.1f}%)")
    lines.append("=" * 60)
    return "\n".join(lines) + "\n"


# ─────────────────────────────────────────────────────────── main

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Refine global_deduped.json with LLM intelligence")
    ap.add_argument("--input",  required=True,  help="global_deduped.json (or any V3 output)")
    ap.add_argument("--output", default=None,   help="output path; defaults to <input stem>_refined.json")
    ap.add_argument("--in-place", action="store_true", help="write back to the same file")
    ap.add_argument("--model",  default=MODEL)
    ap.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    ap.add_argument("--limit",  type=int, default=0, help="first N records only")
    ap.add_argument("--sample", type=int, default=0, help="random N records (for spot-checks)")
    ap.add_argument("--seed",   type=int, default=42)
    ap.add_argument("--dry-run", action="store_true",
                    help="print the prompt for the first record; no API calls")
    ap.add_argument("--skip-already-refined", action="store_true", default=True,
                    help="skip records that already have a `refined` block (default: on)")
    ap.add_argument("--force", action="store_true",
                    help="re-process even records that already have a `refined` block")
    args = ap.parse_args(argv)

    with open(args.input, encoding="utf-8") as f:
        records: list[dict] = json.load(f)
    log(f"[load] {len(records)} records from {args.input}")

    inputs = build_inputs(records)

    if args.dry_run:
        ci = inputs[0]
        log(f"\n[dry-run] record 0: {ci.company_name!r}")
        log(f"  about:          {ci.about[:100]!r}...")
        log(f"  website_scrape: {ci.website_scrape[:100]!r}...")
        log(f"  snippet:        {ci.snippet[:100]!r}...")
        log(f"  categories:     {ci.category_names}")
        log(f"\n[dry-run] prompt:\n{build_prompt(ci)}")
        log("\n[dry-run] no API calls made.")
        return 0

    if not args.force and args.skip_already_refined:
        already = sum(1 for r in records if r.get("refined", {}).get("_status") == "ok")
        if already:
            log(f"[skip] {already} records already have a refined block — "
                f"skipping (pass --force to re-process)")

    if args.sample:
        rng = random.Random(args.seed)
        inputs = rng.sample(inputs, min(args.sample, len(inputs)))
        inputs.sort(key=lambda ci: ci.record_index)
    if args.limit:
        inputs = inputs[:args.limit]

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise SystemExit("OPENAI_API_KEY is not set.")

    session = requests.Session()
    limiter = RateLimiter(1.0 / max(args.concurrency, 1))
    results_map: dict[int, RefinedResult] = {}

    log(f"[run] {len(inputs)} records, model={args.model}, concurrency={args.concurrency}")
    done = 0
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = {
            pool.submit(process, ci, session, api_key, args.model, limiter): ci.record_index
            for ci in inputs
        }
        for fut in as_completed(futures):
            idx = futures[fut]
            try:
                results_map[idx] = fut.result()
            except Exception as exc:
                ci = inputs[next(i for i, ci in enumerate(inputs) if ci.record_index == idx)]
                log(f"  ! unhandled failure for {ci.company_name}: {exc}")
                results_map[idx] = RefinedResult(record_index=idx,
                                                 company_name=ci.company_name,
                                                 status="unhandled-error")
            done += 1
            if done % 50 == 0 or done == len(inputs):
                log(f"  {done}/{len(inputs)}")

    # Merge refined results back into the full record list, preserving any
    # records not processed in this run (--limit / --sample / --skip) unchanged.
    results_ordered = [results_map.get(i) for i in range(len(records))]
    out_records = []
    for r, res in zip(records, results_ordered):
        if res is None:
            out_records.append(r)          # not processed this run — keep as-is
        else:
            rec = dict(r)
            rec["refined"] = {
                "value_chain_position"   : res.value_chain_position,
                "semiconductor_relevance": res.semiconductor_relevance,
                "summary"                : res.summary,
                "capabilities"           : res.capabilities,
                "technical_expertise"    : res.technical_expertise,
                "end_markets"            : res.end_markets,
                "regional_presence"      : res.regional_presence,
                "_status"                : res.status,
                "_model"                 : res.model,
                "_inferred_fields"       : res.inferred_field_count,
                **({"_raw_response": res.raw_response} if res.raw_response else {}),
            }
            out_records.append(rec)

    out_path = args.input if args.in_place else (
        args.output or re.sub(r"\.json$", "_refined.json", args.input))
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out_records, f, indent=2, ensure_ascii=False)

    report = build_report([r for r in results_map.values()])
    log("\n" + report)
    log(f"[done] {out_path}")
    log(f"[cache] {CACHE_DIR}/ — re-runs are free for unchanged records")
    return 0


if __name__ == "__main__":
    sys.exit(main())