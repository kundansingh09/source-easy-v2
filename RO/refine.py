#!/usr/bin/env python3
"""
Generate refined per-company semiconductor-intelligence JSON via the OpenAI API, from
two row-aligned CSVs:

    webscrape.csv   -- one column holds each company's website homepage text
    semicon.csv     -- one column holds the SEMICON directory profile text,
                       another holds the company name

Row i in webscrape.csv is assumed to describe the same company as row i in
semicon.csv (positional alignment, as specified) -- there is no join-by-name
step. A row-count mismatch is treated as fatal, not silently truncated,
since a silent misalignment would attribute one company's content to
another across the whole dataset.

Categories are not part of this schema and are not touched here (per the
decision to leave India category data empty for now).

Output
------
    out_llm/refined_overviews.json   -- full structured records, one per row
    out_llm/refined_overviews.csv    -- flattened, list fields '|'-joined
    out_llm/report.txt               -- success/parse-error/inference counts
    cache_llm/<sha1(name)>.json      -- per-company cache, makes reruns free
                                         and resumable after a crash

Usage
-----
    export OPENAI_API_KEY=sk-...
    pip install requests

    # Always do this first -- confirms column detection and row alignment
    # WITHOUT spending any API calls.
    python refine_overviews.py --website-csv webscrape.csv \\
        --semicon-csv semicon.csv --dry-run

    # Smoke test on a few rows before committing the full run
    python refine_overviews.py --website-csv webscrape.csv \\
        --semicon-csv semicon.csv --limit 10

    # Full run
    python refine_overviews.py --website-csv webscrape.csv \\
        --semicon-csv semicon.csv --concurrency 5

If your columns aren't auto-detected correctly (see the dry-run output),
pass them explicitly:
    --name-col "Company Name" --semicon-col "Semicon CONTENT" \\
    --website-col "Website CONTENT"
"""

from __future__ import annotations

import argparse
import csv
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

# --------------------------------------------------------------------------
# CONFIG
# --------------------------------------------------------------------------

API_URL = "https://api.openai.com/v1/chat/completions"
MODEL = "gpt-4o-mini"              # override with --model; try gpt-4o or gpt-5-mini too
MAX_OUTPUT_TOKENS = 1024
TEMPERATURE = 0.2
# Reasoning-family models (o1, o3, o4-mini, ...) don't accept a temperature
# override and use max_completion_tokens exclusively -- handled in call_openai().
REASONING_MODEL_PREFIXES = ("o1", "o3", "o4")

MAX_RETRIES = 4
RETRY_BACKOFF_BASE = 2.0           # seconds, doubles each retry
DEFAULT_CONCURRENCY = 5

# Cap how much raw content goes into one prompt -- a very long scraped page
# both costs more and dilutes the signal the model should focus on.
MAX_CONTENT_CHARS = 6000

OUT_DIR = "out_llm"
CACHE_DIR = "cache_llm"

REQUIRED_FIELDS = [
    "value_chain_position", "semiconductor_relevance", "capabilities",
    "end_markets", "india_angle", "summary", "technical_expertise",
]
LIST_FIELDS = {"capabilities", "end_markets", "technical_expertise"}

SYSTEM_PROMPT = """You are a semiconductor industry analyst with deep expertise across the full value chain — from upstream materials and equipment to design, fabrication, packaging, and test.

You will be given public information about a SEMICON exhibitor and must return ONLY a single JSON object matching the schema you are given — no markdown code fences, no preamble, no explanation before or after it."""

USER_PROMPT_TEMPLATE = """Given the following public information about a SEMICON exhibitor, extract structured intelligence about their role in the semiconductor ecosystem. Use the provided content as an anchoring source only if the content is relevant and representative of the company especially in semiconductor domain. Semicon content is derived from profile page in Semicon directory while website content is only the homepage scraped of root url (which could be detailed or generic based on website structure and group company profile or contain boilerplate content that's not relevant for profile). Where the content is generic, sparse, or non-technical, supplement with your own knowledge of this company's known semiconductor-related products, customers, or technologies — but clearly flag any such inference with "(inferred)" in the relevant field values.

COMPANY: {name}
Semicon CONTENT: {semicon_content}
Website CONTENT: {website_content}

Return a JSON object with exactly these fields:

{{
"value_chain_position": "<one of: Materials | Equipment | EDA/IP | Fab/Foundry | OSAT/Packaging | Test & Measurement | Design Services | Chemicals & Gases | Facility & Infrastructure | Distribution | Other>",
"semiconductor_relevance": "<High | Medium | Low (for many, be conservative) | None -- with a 1-liner reason only, how central is semiconductors to this company's business>",
"capabilities": ["<2-3 specific capabilities or product lines relevant to semiconductor manufacturing or design>"],
"end_markets": ["<target segments e.g. Logic, Memory, Power, RF/5G, Automotive, Advanced Packaging etc>"],
"india_angle": "<one sentence on their India-specific presence, manufacturing, or strategy if evident only, else null>",
"summary": "<2-3 sentence description of what this company does in the semiconductor value chain, written for a procurement or sourcing professional>",
"technical_expertise": ["<key technologies the company has demonstrated depth in -- not product names, but the underlying technical domains (e.g. 'atomic layer deposition', 'wafer-level packaging', 'RISC-V processor architecture', 'ultra-high vacuum systems'); note where the company shows actual strength at a global level, don't add generic entries for every company>"]
}}

Rules:
- Base everything strictly on the provided content. Do not hallucinate capabilities.
- If the content is too generic (e.g. a diversified MNC homepage with no semiconductor specifics), set semiconductor_relevance to Low or None and leave capabilities sparse.
- For capabilities, be specific: prefer "CMP slurry for sub-7nm nodes" over "semiconductor materials" but do not invent.
- If content is empty or irrelevant, return all fields as null except value_chain_position set to "Other"."""


# --------------------------------------------------------------------------
# DATA MODEL
# --------------------------------------------------------------------------

@dataclass
class CompanyInput:
    row_index: int
    name: str
    semicon_content: str
    website_content: str


@dataclass
class RefinedResult:
    row_index: int = 0
    name: str = ""
    value_chain_position: Optional[str] = None
    semiconductor_relevance: Optional[str] = None
    capabilities: list = field(default_factory=list)
    end_markets: list = field(default_factory=list)
    india_angle: Optional[str] = None
    summary: Optional[str] = None
    technical_expertise: list = field(default_factory=list)
    status: str = ""            # ok / parse-error / api-error / empty-input
    inferred_field_count: int = 0
    raw_response: str = ""      # kept only on parse-error, for debugging
    model: str = MODEL


# --------------------------------------------------------------------------
# HELPERS
# --------------------------------------------------------------------------

def log(msg: str) -> None:
    print(msg, flush=True)


def truncate(text: str, limit: int = MAX_CONTENT_CHARS) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0] + " ...[truncated]"


def cache_path(name: str, row_index: int, cache_dir: str) -> str:
    key = f"{row_index}:{name}"
    h = hashlib.sha1(key.encode("utf-8")).hexdigest()[:24]
    return os.path.join(cache_dir, f"{h}.json")


def strip_json_fences(text: str) -> str:
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
    return n


# --------------------------------------------------------------------------
# CSV LOADING (positional alignment, per instruction)
# --------------------------------------------------------------------------

def sniff_column(fieldnames: list[str], must_contain_all: list[str]) -> Optional[str]:
    for fn in fieldnames:
        low = fn.lower()
        if all(term in low for term in must_contain_all):
            return fn
    return None


def load_inputs(website_csv: str, semicon_csv: str,
                name_col: Optional[str], semicon_col: Optional[str],
                website_col: Optional[str]) -> list[CompanyInput]:
    with open(website_csv, "r", encoding="utf-8-sig", newline="") as fh:
        website_rows = list(csv.DictReader(fh))
    with open(semicon_csv, "r", encoding="utf-8-sig", newline="") as fh:
        semicon_rows = list(csv.DictReader(fh))

    if not website_rows or not semicon_rows:
        raise SystemExit("One of the input CSVs is empty.")

    website_fields = list(website_rows[0].keys())
    semicon_fields = list(semicon_rows[0].keys())

    resolved_website_col = website_col or sniff_column(website_fields, ["content"]) \
        or sniff_column(website_fields, ["website"])
    resolved_semicon_col = semicon_col or sniff_column(semicon_fields, ["content"]) \
        or sniff_column(semicon_fields, ["semicon"])
    resolved_name_col = name_col or sniff_column(semicon_fields, ["name"])

    missing = []
    if not resolved_website_col:
        missing.append(f"website content column not found in {website_csv} "
                       f"(columns: {website_fields}) -- pass --website-col")
    if not resolved_semicon_col:
        missing.append(f"semicon content column not found in {semicon_csv} "
                       f"(columns: {semicon_fields}) -- pass --semicon-col")
    if not resolved_name_col:
        missing.append(f"company name column not found in {semicon_csv} "
                       f"(columns: {semicon_fields}) -- pass --name-col")
    if missing:
        raise SystemExit("Column detection failed:\n  " + "\n  ".join(missing))

    log(f"[columns] name        <- {semicon_csv}::{resolved_name_col}")
    log(f"[columns] semicon     <- {semicon_csv}::{resolved_semicon_col}")
    log(f"[columns] website     <- {website_csv}::{resolved_website_col}")

    if len(website_rows) != len(semicon_rows):
        raise SystemExit(
            f"Row count mismatch: {website_csv} has {len(website_rows)} rows, "
            f"{semicon_csv} has {len(semicon_rows)} rows. Since alignment is "
            f"positional (row i <-> row i), a mismatch means at least one "
            f"company would be paired with the wrong content. Fix the CSVs "
            f"so they line up 1:1 before running this."
        )

    inputs = []
    for i, (wrow, srow) in enumerate(zip(website_rows, semicon_rows)):
        name = (srow.get(resolved_name_col) or "").strip()
        inputs.append(CompanyInput(
            row_index=i,
            name=name,
            semicon_content=(srow.get(resolved_semicon_col) or "").strip(),
            website_content=(wrow.get(resolved_website_col) or "").strip(),
        ))
    return inputs


# --------------------------------------------------------------------------
# LLM CALL
# --------------------------------------------------------------------------

class RateLimiter:
    """Simple shared spacing so concurrent workers don't all fire at once."""
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
                user_prompt: str, limiter: RateLimiter) -> tuple[Optional[str], str]:
    """Returns (response_text_or_None, status)."""
    is_reasoning_model = model.startswith(REASONING_MODEL_PREFIXES)

    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        "response_format": {"type": "json_object"},
        "max_completion_tokens": MAX_OUTPUT_TOKENS,
    }
    # Reasoning-family models (o1/o3/o4-mini) reject a temperature override --
    # they only support the default. Only set it for gpt-* chat models.
    if not is_reasoning_model:
        body["temperature"] = TEMPERATURE

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

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
            data = resp.json()
            try:
                text = data["choices"][0]["message"]["content"] or ""
            except (KeyError, IndexError):
                return None, "api-error-unexpected-shape"
            return text, "ok"

        if resp.status_code in (429, 500, 502, 503):
            if attempt == MAX_RETRIES:
                return None, f"api-error-{resp.status_code}"
            time.sleep(RETRY_BACKOFF_BASE ** attempt)
            continue

        # 4xx other than 429 -- not retryable (bad request, auth, etc.)
        return None, f"api-error-{resp.status_code}: {resp.text[:300]}"

    return None, "api-error-exhausted-retries"


def parse_response(raw_text: str) -> tuple[Optional[dict], str]:
    cleaned = strip_json_fences(raw_text)
    try:
        obj = json.loads(cleaned)
    except json.JSONDecodeError:
        # last resort: grab the outermost {...} block
        m = re.search(r"\{.*\}", cleaned, re.S)
        if not m:
            return None, "parse-error"
        try:
            obj = json.loads(m.group(0))
        except json.JSONDecodeError:
            return None, "parse-error"

    for f in REQUIRED_FIELDS:
        if f not in obj:
            obj[f] = [] if f in LIST_FIELDS else None
        elif f in LIST_FIELDS and not isinstance(obj[f], list):
            obj[f] = [obj[f]] if obj[f] else []
    return obj, "ok"


# --------------------------------------------------------------------------
# PER-COMPANY PIPELINE
# --------------------------------------------------------------------------

def process_company(ci: CompanyInput, session: requests.Session, api_key: str,
                    model: str, limiter: RateLimiter, cache_dir: str) -> RefinedResult:
    os.makedirs(cache_dir, exist_ok=True)
    cpath = cache_path(ci.name, ci.row_index, cache_dir)
    if os.path.exists(cpath):
        with open(cpath, "r", encoding="utf-8") as fh:
            d = json.load(fh)
        return RefinedResult(**d)

    result = RefinedResult(row_index=ci.row_index, name=ci.name, model=model)

    if not ci.semicon_content and not ci.website_content:
        result.status = "empty-input"
        result.value_chain_position = "Other"
        _save_cache(cpath, result)
        return result

    prompt = USER_PROMPT_TEMPLATE.format(
        name=ci.name or "(unknown)",
        semicon_content=truncate(ci.semicon_content) or "(none provided)",
        website_content=truncate(ci.website_content) or "(none provided)",
    )

    raw_text, call_status = call_openai(session, api_key, model, prompt, limiter)
    if raw_text is None:
        result.status = call_status
        _save_cache(cpath, result)
        return result

    parsed, parse_status = parse_response(raw_text)
    if parsed is None:
        result.status = "parse-error"
        result.raw_response = raw_text[:2000]
        _save_cache(cpath, result)
        return result

    result.value_chain_position = parsed.get("value_chain_position")
    result.semiconductor_relevance = parsed.get("semiconductor_relevance")
    result.capabilities = parsed.get("capabilities") or []
    result.end_markets = parsed.get("end_markets") or []
    result.india_angle = parsed.get("india_angle")
    result.summary = parsed.get("summary")
    result.technical_expertise = parsed.get("technical_expertise") or []
    result.status = "ok"
    result.inferred_field_count = count_inferred(parsed)

    _save_cache(cpath, result)
    return result


def _save_cache(path: str, result: RefinedResult) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(asdict(result), fh, ensure_ascii=False)


# --------------------------------------------------------------------------
# OUTPUT
# --------------------------------------------------------------------------

CSV_FIELDS = ["row_index", "name", "value_chain_position", "semiconductor_relevance",
              "capabilities", "end_markets", "india_angle", "summary",
              "technical_expertise", "status", "inferred_field_count", "model"]


def write_outputs(results: list[RefinedResult], out_dir: str) -> str:
    os.makedirs(out_dir, exist_ok=True)

    with open(os.path.join(out_dir, "refined_overviews.json"), "w", encoding="utf-8") as fh:
        json.dump([asdict(r) for r in results], fh, indent=2, ensure_ascii=False)

    with open(os.path.join(out_dir, "refined_overviews.csv"), "w",
              newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
        w.writeheader()
        for r in results:
            d = asdict(r)
            for lf in LIST_FIELDS:
                d[lf] = " | ".join(d[lf]) if d[lf] else ""
            w.writerow({k: d[k] for k in CSV_FIELDS})

    report = build_report(results)
    with open(os.path.join(out_dir, "report.txt"), "w", encoding="utf-8") as fh:
        fh.write(report)
    return report


def build_report(results: list[RefinedResult]) -> str:
    total = len(results)
    if not total:
        return "No rows processed.\n"

    status_counts: dict[str, int] = {}
    for r in results:
        status_counts[r.status] = status_counts.get(r.status, 0) + 1

    ok = [r for r in results if r.status == "ok"]
    relevance_counts: dict[str, int] = {}
    for r in ok:
        key = (r.semiconductor_relevance or "").split(" ")[0] or "unset"
        relevance_counts[key] = relevance_counts.get(key, 0) + 1

    inferred_total = sum(r.inferred_field_count for r in ok)
    rows_with_inference = sum(1 for r in ok if r.inferred_field_count > 0)
    empty_capabilities = sum(1 for r in ok if not r.capabilities)
    empty_summary = sum(1 for r in ok if not (r.summary or "").strip())

    lines = []
    lines.append("=" * 60)
    lines.append("REFINED OVERVIEW GENERATION -- REPORT")
    lines.append("=" * 60)
    lines.append(f"Total companies: {total}")
    lines.append("")
    lines.append("Status breakdown:")
    for k, v in sorted(status_counts.items()):
        lines.append(f"  {k:<22} {v}  ({v/total*100:.1f}%)")
    lines.append("")
    if ok:
        lines.append(f"Of {len(ok)} successful:")
        lines.append(f"  empty capabilities        {empty_capabilities} ({empty_capabilities/len(ok)*100:.1f}%)")
        lines.append(f"  empty summary              {empty_summary}")
        lines.append(f"  rows with any (inferred)   {rows_with_inference} ({rows_with_inference/len(ok)*100:.1f}%)")
        lines.append(f"  total (inferred) fields    {inferred_total}")
        lines.append("")
        lines.append("semiconductor_relevance distribution:")
        for k, v in sorted(relevance_counts.items(), key=lambda kv: -kv[1]):
            lines.append(f"  {k:<10} {v}  ({v/len(ok)*100:.1f}%)")
        lines.append("")

    errors = [r for r in results if r.status not in ("ok", "empty-input")]
    if errors:
        lines.append(f"Errors ({len(errors)}), first 20:")
        for r in errors[:20]:
            lines.append(f"  [{r.status}] {r.name}")
        lines.append("")

    lines.append("=" * 60)
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# MAIN
# --------------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Generate refined semiconductor-intelligence JSON via OpenAI")
    ap.add_argument("--website-csv", required=True)
    ap.add_argument("--semicon-csv", required=True)
    ap.add_argument("--name-col", default=None)
    ap.add_argument("--semicon-col", default=None)
    ap.add_argument("--website-col", default=None)
    ap.add_argument("--model", default=MODEL)
    ap.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    ap.add_argument("--limit", type=int, default=0,
                    help="take the first N rows (after --sample, if both given)")
    ap.add_argument("--sample", type=int, default=0,
                    help="randomly sample N rows across the full dataset instead "
                        "of taking the first N -- use this for spot-checks so you "
                        "see a representative mix, not just alphabetically-first "
                        "companies")
    ap.add_argument("--seed", type=int, default=42,
                    help="random seed for --sample, fixed by default so repeat "
                        "runs (e.g. comparing two models) sample the SAME rows")
    ap.add_argument("--run-tag", default=None,
                    help="namespaces cache/output dirs, e.g. --run-tag 4o-mini "
                        "writes to out_llm_4o-mini/ and cache_llm_4o-mini/. "
                        "Use a different tag per model when A/B testing so "
                        "runs don't share a cache or overwrite each other's "
                        "output.")
    ap.add_argument("--dry-run", action="store_true",
                    help="resolve columns + show one built prompt, no API calls")
    args = ap.parse_args(argv)

    inputs = load_inputs(args.website_csv, args.semicon_csv,
                         args.name_col, args.semicon_col, args.website_col)
    log(f"[load] {len(inputs)} row-aligned companies")

    tag = args.run_tag or re.sub(r"[^a-z0-9.\-]+", "-", args.model.lower())
    out_dir = f"{OUT_DIR}_{tag}"
    cache_dir = f"{CACHE_DIR}_{tag}"
    log(f"[run] model={args.model}  tag={tag}  out_dir={out_dir}  cache_dir={cache_dir}")

    if args.dry_run:
        sample = inputs[0]
        log("\n[dry-run] first row parsed as:")
        log(f"  name:            {sample.name!r}")
        log(f"  semicon_content: {sample.semicon_content[:150]!r}...")
        log(f"  website_content: {sample.website_content[:150]!r}...")
        log("\n[dry-run] prompt that WOULD be sent for row 0:\n")
        log(USER_PROMPT_TEMPLATE.format(
            name=sample.name or "(unknown)",
            semicon_content=truncate(sample.semicon_content) or "(none provided)",
            website_content=truncate(sample.website_content) or "(none provided)",
        ))
        log("\n[dry-run] no API calls made. Re-run without --dry-run once this looks right.")
        return 0

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise SystemExit("OPENAI_API_KEY is not set in the environment.")

    if args.sample:
        rng = random.Random(args.seed)
        inputs = rng.sample(inputs, min(args.sample, len(inputs)))
        inputs.sort(key=lambda ci: ci.row_index)  # keep report/log order stable
        log(f"[sample] randomly picked {len(inputs)} rows (seed={args.seed}) "
            f"-- use the same --seed to compare models on the SAME rows")

    if args.limit:
        inputs = inputs[:args.limit]

    session = requests.Session()
    limiter = RateLimiter(min_interval=1.0 / max(args.concurrency, 1))
    results: list[RefinedResult] = [None] * len(inputs)  # type: ignore

    log(f"[run] processing {len(inputs)} companies, concurrency={args.concurrency}")
    done = 0
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = {
            pool.submit(process_company, ci, session, api_key, args.model, limiter, cache_dir): idx
            for idx, ci in enumerate(inputs)
        }
        for fut in as_completed(futures):
            idx = futures[fut]
            try:
                results[idx] = fut.result()
            except Exception as exc:
                ci = inputs[idx]
                log(f"  ! unhandled failure for {ci.name}: {exc}")
                results[idx] = RefinedResult(row_index=ci.row_index, name=ci.name,
                                             status="unhandled-error")
            done += 1
            if done % 25 == 0 or done == len(inputs):
                log(f"  {done}/{len(inputs)}")

    report = write_outputs(results, out_dir)
    log("")
    log(report)
    log(f"Wrote {out_dir}/refined_overviews.json, .csv, report.txt")
    log(f"Per-company cache in {cache_dir}/ -- safe to re-run, cached rows are free")
    return 0


if __name__ == "__main__":
    sys.exit(main())