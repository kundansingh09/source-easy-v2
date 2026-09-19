"""LLM reranking layer: a procurement judge blended with retrieval score.

Hybrid retrieval narrows ~1300 exhibitors to a shortlist; this module only
reorders that shortlist, so cost stays flat as the catalogue grows.

    final = alpha * normalised_retrieval + (1 - alpha) * llm_score

Two things about that formula are worth knowing before you tune it.

Normalisation window. The fused score out of Qdrant is an RRF sum (roughly
0.016-0.033 for two branches at k=60) with no absolute meaning - only the
ordering is meaningful. So it has to be normalised before it can be averaged
with anything. Min-max over the shortlist is the readable choice, but it
*manufactures* a full 0-1 spread every time: the best candidate scores 1.0
whether it is a perfect match or the least-bad of ten bad ones. That is
acceptable here because the LLM side carries the absolute-quality signal, but
it is why you should never read `retrieval_norm` as a quality measure.

Alpha is not the weight you think it is. Alpha weights the *scales*, not the
influence. Min-maxed RRF always spans exactly 1.0; LLM judges, left
unconstrained, cluster their scores in a narrow band (0.6-0.9 is typical), so
the LLM side contributes far less range than its nominal (1 - alpha) share.
Simulating this (compressed + 0.05-quantised judge scores, 4000 trials) put
realised LLM influence at ~52% when alpha=0.4 nominally assigns it 60%. The
fix is on the prompt side, not the arithmetic: the rubric below anchors each
score band to an observable condition and explicitly forbids clustering, which
restores the spread. `normalise="zscore"` is also available and equalises the
two sides by construction (it measured marginally better, +0.002 NDCG@5, which
is inside the noise), at the cost of scores that no longer read as 0-1.
"""

import json
import os
import time

from openai import OpenAI

RERANK_MODEL = os.environ.get("RERANK_MODEL", "gpt-4o-mini")
DEFAULT_ALPHA = 0.4
# Fraction of the shortlist the judge must actually score for the blend to
# mean anything. Below this we discard the rerank entirely - see the guard in
# rerank_with_llm for why partial coverage is worse than none.
MIN_JUDGE_COVERAGE = 0.6
FALLBACK_ABOUT = "Semiconductor technology and equipment supplier."

SYSTEM_PROMPT = """\
You are a senior sourcing engineer for a semiconductor fab. You qualify \
suppliers for an engineering procurement team. Your judgement decides which \
vendors get an RFQ, so a confident wrong answer costs more than an honest \
uncertain one.

You will receive a buyer's sourcing query and a numbered shortlist of \
exhibitor records retrieved from a SEMICON trade-show database. Each record \
has: an id, a company name, an HQ country, category tags from the show's own \
taxonomy, and the company's self-written overview (sometimes absent).

# What you are judging
Whether THIS company can plausibly supply what the buyer asked for. Not \
whether it is a good company, not whether it is famous, not whether it is \
adjacent to the right industry.

# Evidence rules
1. Score only what the record supports. A name that sounds relevant is not \
evidence. A category tag is weak evidence of capability; an overview that \
names the specific process step, equipment class, material, or specification \
is strong evidence.
2. Absence of an overview is not evidence of absence. A precise category match \
with no description outranks a vague description, but is outranked by a \
description that explicitly confirms the capability.
3. Distinguish tiers. A distributor, agent, or trading house for a product is \
a weaker match than the manufacturer of it when the buyer wants the product \
itself - but a legitimate match when the buyer wants supply rather than \
manufacture. Read the query to decide which is wanted.
4. Distinguish process adjacency. Front-end (litho, etch, deposition, CMP, \
metrology), back-end (assembly, packaging, test, burn-in), and facilities \
(UHP gas, chemicals, cleanroom, abatement) are different markets. A supplier \
strong in one is usually not a substitute in another.

# Geography
Apply this ONLY when the query expresses a geographic requirement (names a \
country or region, or says "local", "domestic", "nearshore", "avoid X"):
  - HQ satisfies the requirement -> no adjustment.
  - HQ is in the same region but the wrong country (query says Taiwan, HQ is \
Korea) -> cap relevance_score at 0.55 and say so in the reasoning.
  - HQ is on a different continent than required -> cap at 0.35.
  - HQ is "Unknown" -> cap at 0.60. Do not guess a location from the company \
name; unverified geography is a procurement risk, not a neutral fact.
When the query has no geographic requirement, HQ must not affect the score at \
all.

# Scoring rubric - use the full range
relevance_score is a float from 0.0 to 1.0. Anchor it to a condition, do not \
pick a comfortable middle number. Do not cluster scores; a shortlist where \
every candidate lands between 0.6 and 0.8 is a failed evaluation. If several \
candidates genuinely tie, it is correct to say so with identical scores - but \
this should be rare.
  0.90-1.00  Record explicitly names the requested capability, process step \
or product class. Would go on the RFQ list unread.
  0.70-0.89  Strong fit on category plus supporting description, but the exact \
specification in the query is not confirmed. Worth a qualification call.
  0.50-0.69  Right market segment, capability plausible but unevidenced; or a \
strong technical fit blocked by a geographic cap above.
  0.30-0.49  Adjacent segment. Would need to pivot or subcontract to serve \
this request.
  0.10-0.29  Semiconductor industry, wrong problem entirely.
  0.00-0.09  Not a supplier for this request in any reading.

If no candidate clears 0.5, return low scores across the board. Do not \
manufacture a plausible-looking ranking out of a bad shortlist - the buyer \
needs to know the search failed.

# Output contract
Return ONLY a JSON object, no prose, no markdown fences:
{"results": [{"id": <int, copied exactly from the record>, \
"relevance_score": <float 0.0-1.0>, \
"reasoning": "<=20 words, cite the specific evidence or the specific gap>"}]}
Include every id from the shortlist exactly once. Never invent an id.\
"""


# --------------------------------------------------------------- normalising

def _minmax(values):
    lo, hi = min(values), max(values)
    if hi - lo < 1e-12:
        # Every candidate fused to the same score - a real case when the
        # shortlist is one point long, or when a filter leaves a tied set.
        # 0.5 keeps the blend neutral instead of arbitrarily awarding 1.0.
        return [0.5] * len(values)
    return [(v - lo) / (hi - lo) for v in values]


def _zscore(values):
    n = len(values)
    mean = sum(values) / n
    var = sum((v - mean) ** 2 for v in values) / n
    sd = var ** 0.5
    if sd < 1e-12:
        return [0.0] * n
    return [(v - mean) / sd for v in values]


def normalise(values, method="minmax"):
    if not values:
        return []
    return _zscore(values) if method == "zscore" else _minmax(values)


# ----------------------------------------------------------------- prompting

def _format_candidates(candidates):
    lines = []
    for c in candidates:
        cats = ", ".join((c.get("categories_l2") or c.get("cat_l2_names") or c.get("categories_l1") or c.get("cat_l1_names") or [])[:6])
        about = (c.get("about") or "").strip()
        if not about or about == FALLBACK_ABOUT:
            about = "(no overview provided)"
        elif len(about) > 700:
            # Long marketing copy dilutes the judgement and inflates cost; the
            # opening lines carry the capability statement in practice.
            about = about[:700].rsplit(" ", 1)[0] + " ..."
        lines.append(
            f"id={c.get('id')}\n"
            f"  company: {c.get('company_name')}\n"
            f"  hq: {c.get('hq_country') or 'Unknown'}\n"
            f"  categories: {cats or 'none listed'}\n"
            f"  overview: {about}"
        )
    return "\n\n".join(lines)


def _parse(raw):
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        raw = raw[4:] if raw.lower().startswith("json") else raw
    data = json.loads(raw.strip())
    rows = data["results"] if isinstance(data, dict) else data
    out = {}
    for row in rows:
        try:
            score = float(row["relevance_score"])
        except (KeyError, TypeError, ValueError):
            continue
        out[int(row["id"])] = {
            "relevance_score": min(1.0, max(0.0, score)),
            "reasoning": str(row.get("reasoning", ""))[:200],
        }
    return out


# ------------------------------------------------------------------- rerank

def rerank_with_llm(query, hybrid_results, alpha=DEFAULT_ALPHA, top_n=None,
                    client=None, model=None, normalise_method="minmax",
                    temperature=0.0):
    """Blend the fused retrieval score with an LLM relevance judgement.

    Args:
        query: the buyer's raw search string.
        hybrid_results: list of dicts from SourcingSearchEngine.search(), each
            carrying at least `id` and `score` (the RRF/DBSF fused score).
        alpha: weight on the retrieval side. 0.4 default; see module docstring
            for why this number is load-bearing and how to pick it.
        top_n: truncate after reranking. None returns the whole shortlist.
        normalise_method: "minmax" (default, keeps final_score in 0-1) or
            "zscore" (scale-equalised, final_score unbounded).

    Returns the same dicts, reordered, each with `retrieval_norm`, `llm_score`,
    `llm_reason` and `final_score` added.

    Never raises. Any failure - missing API key, network error, malformed JSON,
    a judge that skips ids - degrades to the original hybrid ordering, because
    a rerank outage should cost relevance, not availability.
    """
    if not hybrid_results:
        return hybrid_results
    if len(hybrid_results) == 1:
        # Nothing to reorder, and min-max on a single value is meaningless.
        return hybrid_results[:top_n] if top_n else hybrid_results

    alpha = min(1.0, max(0.0, float(alpha)))
    client = client or OpenAI(timeout=8.0)  # reads OPENAI_API_KEY from env

    try:
        t0 = time.perf_counter()
        resp = client.chat.completions.create(
            model=model or RERANK_MODEL,
            temperature=temperature,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user",
                 "content": f"Buyer query: {query}\n\nShortlist:\n\n"
                            f"{_format_candidates(hybrid_results)}"},
            ],
        )
        llm_ms = (time.perf_counter() - t0) * 1000
        print(f"[DEBUG] OpenAI Rerank Time: {llm_ms:.2f} ms")
        
        judged = _parse(resp.choices[0].message.content)
        if not judged:
            raise ValueError("judge returned no usable scores")
        # Partial coverage is a partial outage. Filling the gaps with the mean
        # of what *was* scored looks harmless but quietly inverts the result:
        # if the judge scores one candidate 0.9 and skips the rest, every gap
        # is filled with 0.9 too, the LLM term goes flat, and retrieval alone
        # decides the order - with the one candidate the judge actually liked
        # given no advantage at all. Below this threshold the blend carries no
        # real signal, so return the honest hybrid ordering instead.
        coverage = len(judged) / len(hybrid_results)
        if coverage < MIN_JUDGE_COVERAGE:
            raise ValueError(
                f"judge scored only {len(judged)}/{len(hybrid_results)} candidates")
    except Exception as e:
        print(f"[rerank_with_llm] falling back to hybrid order - {e}")
        return hybrid_results[:top_n] if top_n else hybrid_results

    raw_scores = [c.get("score") or c.get("retrieval_score") or 0.0
                  for c in hybrid_results]
    normed = normalise(raw_scores, normalise_method)

    # Candidates the judge silently dropped keep their retrieval position
    # rather than being sent to the bottom by a 0.0 default - a parsing gap is
    # our problem, not evidence against the supplier.
    llm_raw = [judged.get(c["id"], {}).get("relevance_score") for c in hybrid_results]
    seen = [s for s in llm_raw if s is not None]
    fallback = sum(seen) / len(seen) if seen else 0.5

    for cand, rnorm, llm in zip(hybrid_results, normed, llm_raw):
        hit = judged.get(cand["id"])
        score = fallback if llm is None else llm
        cand["retrieval_norm"] = round(rnorm, 4)
        cand["llm_score"] = None if llm is None else round(llm, 3)
        cand["llm_reason"] = (hit or {}).get("reasoning") or ("not scored by judge"
                                                             if llm is None else "")
        cand["final_score"] = round(alpha * rnorm + (1 - alpha) * score, 4)

    ranked = sorted(hybrid_results, key=lambda c: c["final_score"], reverse=True)
    return ranked[:top_n] if top_n else ranked


# Backwards-compatible alias for the previous entry point.
def llm_rerank(query, candidates, top_n=None, client=None):
    return rerank_with_llm(query, candidates, top_n=top_n, client=client)


# ----------------------------------------------------------- on-demand explainer

EXPLAIN_SYSTEM_PROMPT = """\
You are a senior procurement judge for a semiconductor fab qualifying suppliers. \
You will receive a buyer's sourcing query and a shortlist of candidates. \
For EACH candidate in the provided shortlist, write a 1-sentence (<=20 words) \
explanation of why that candidate fits the query based on their capabilities, \
products, or categories.

Return ONLY a JSON object:
{"explanations": [{"id": <id, copied exactly from the record>, "reasoning": "<1-sentence explanation <=20 words>"}]}
Include every candidate id from the shortlist exactly once. Never invent an id.\
"""


def _parse_explanations(raw):
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        raw = raw[4:] if raw.lower().startswith("json") else raw
    try:
        data = json.loads(raw.strip())
    except Exception:
        return {}

    out = {}
    items = []
    if isinstance(data, dict):
        if "explanations" in data and isinstance(data["explanations"], list):
            items = data["explanations"]
        elif "results" in data and isinstance(data["results"], list):
            items = data["results"]
        elif "candidates" in data and isinstance(data["candidates"], list):
            items = data["candidates"]
        else:
            for k, v in data.items():
                if isinstance(v, str):
                    out[k] = v
                elif isinstance(v, dict) and "reasoning" in v:
                    out[k] = str(v["reasoning"])
    elif isinstance(data, list):
        items = data

    for item in items:
        if isinstance(item, dict) and "id" in item:
            reason = item.get("reasoning") or item.get("explanation") or ""
            out[item["id"]] = str(reason).strip()

    return out


def explain_results(query, candidates, client=None, model=None, temperature=0.0):
    """Generate 1-sentence explanations of why each candidate fits the query.

    Args:
        query: the buyer's sourcing query string.
        candidates: list of exhibitor dicts (each having an 'id').
        client: optional OpenAI client instance.
        model: optional LLM model name.
        temperature: sampling temperature (default 0.0).

    Returns:
        A dict mapping each candidate's id to its 1-sentence reasoning string.
    """
    if not candidates:
        return {}

    try:
        if not os.environ.get("OPENAI_API_KEY") and client is None:
            raise ValueError("OPENAI_API_KEY not configured")
        client = client or OpenAI(timeout=10.0)
        t0 = time.perf_counter()
        resp = client.chat.completions.create(
            model=model or RERANK_MODEL,
            temperature=temperature,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": EXPLAIN_SYSTEM_PROMPT},
                {"role": "user",
                 "content": f"Buyer query: {query}\n\nShortlist:\n\n"
                            f"{_format_candidates(candidates)}"},
            ],
        )
        llm_ms = (time.perf_counter() - t0) * 1000
        print(f"[DEBUG] OpenAI Explain Time: {llm_ms:.2f} ms")
        parsed = _parse_explanations(resp.choices[0].message.content)
    except Exception as e:
        print(f"[explain_results] judge call failed: {e}")
        parsed = {}

    out = {}
    for c in candidates:
        cid = c.get("id")
        if cid is None:
            continue
        reason = (
            parsed.get(cid)
            or parsed.get(str(cid))
            or (parsed.get(int(cid)) if isinstance(cid, str) and cid.isdigit() else None)
        )
        if not reason:
            reason = "Matches query criteria based on supplier profile and industry capabilities."
        out[cid] = str(reason).strip()

    return out