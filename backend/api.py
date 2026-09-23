"""HTTP API over the existing SourcingSearchEngine.

Deliberately thin. Every endpoint is a direct pass-through to a method that
already exists and is already tested (`facets()`, `search()`, `browse()`);
this module adds no ranking, filtering or faceting logic of its own. The
drill-down faceting rules, the >0-count guarantee, the zero-query browse
fork and the rerank blend all still live in backend/search_engine.py and
backend/reranker.py and behave identically to the Streamlit app.

The engine is built ONCE at process startup and held for the life of the
process. That is the whole reason this belongs on a long-lived host
(Render web service) rather than a serverless platform: `_init_collection`
downloads the ONNX model, embeds the entire catalogue and builds an
in-memory Qdrant index. Doing that per request is not viable, and on most
serverless runtimes there is no way to avoid it.
"""

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from backend.search_engine import SourcingSearchEngine

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DATA_PATH = os.path.join(BASE_DIR, "full-global-refined-hybrid.json")
DATA_PATH = os.environ.get("DATA_PATH", DEFAULT_DATA_PATH)
TAXONOMY_PATH = os.environ.get("TAXONOMY_PATH", os.path.join(BASE_DIR, "data", "categories.json"))
# Comma-separated list, e.g. "https://sourcing-web.onrender.com,http://localhost:5173"
ALLOWED_ORIGINS = [o.strip() for o in os.environ.get(
    "ALLOWED_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173").split(",") if o.strip()]

MAX_LIMIT = 50

state = {"engine": None, "error": None}


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Build at startup, not on first request, so the platform's health check
    # fails loudly on a broken index instead of the first user hitting a
    # multi-minute hang.
    try:
        print(f"Building index from {DATA_PATH} ...")
        engine = SourcingSearchEngine(DATA_PATH)
        state["engine"] = engine
        print(f"Index ready: {engine.count()} suppliers")
    except Exception as e:  # noqa: BLE001 - surfaced via /api/health
        state["error"] = str(e)
        print(f"Index build FAILED: {e}")
    yield


app = FastAPI(title="Semicon Sourcing API", version="1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


def get_engine():
    if state["engine"] is None:
        raise HTTPException(status_code=503,
                            detail=state["error"] or "Index still building, retry shortly.")
    return state["engine"]


@app.get("/api/health")
def health():
    """Render's health check target. Reports 'building' rather than failing
    outright during the startup window so a slow first boot doesn't get the
    deploy killed."""
    if state["error"]:
        return {"status": "error", "detail": state["error"], "count": 0}
    if state["engine"] is None:
        return {"status": "building", "count": 0}
    return {
        "status": "ok",
        "count": state["engine"].count(),
        "rerank_available": bool(os.environ.get("OPENAI_API_KEY")),
    }


@app.get("/api/taxonomy")
def taxonomy():
    """Id -> name maps, so the client can label an active filter chip without
    waiting on a facet response."""
    engine = get_engine()
    return {
        "l1": {str(k): v for k, v in engine.l1_names_by_id.items()},
        "l2": {str(k): v for k, v in engine.l2_names_by_id.items()},
    }


@app.get("/api/facets")
def facets(
    expo: str | None = None,
    country: list[str] = Query(default=[]),
    l1: list[int] = Query(default=[]),
    l2: list[int] = Query(default=[]),
):
    """Reachable filter values + counts for the current selection. Drill-down
    semantics and the 'never offer a zero-result option' guarantee come from
    the engine unchanged."""
    return get_engine().facets(
        expo=expo,
        hq_countries=country or None,
        cat_l1_ids=l1 or None,
        cat_l2_ids=l2 or None,
    )


@app.get("/api/search")
def search(
    q: str = "",
    expo: str | None = None,
    country: list[str] = Query(default=[]),
    l1: list[int] = Query(default=[]),
    l2: list[int] = Query(default=[]),
    limit: int = 10,
    offset: int = 0,
    rerank: bool = False,
    alpha: float = 0.4,
    fusion: str = "rrf",
):
    """Fork is the engine's, not ours: a non-empty q runs hybrid retrieval +
    fusion; an empty q with filters runs the deterministic alphabetical
    browse; an empty q with no filters returns nothing."""
    engine = get_engine()
    limit = max(1, min(MAX_LIMIT, limit))
    offset = max(0, offset)

    if rerank and not os.environ.get("OPENAI_API_KEY"):
        rerank = False  # degrade quietly rather than 500 on a missing key

    try:
        results, total = engine.search(
            query=q, expo=expo, hq_countries=country or None,
            cat_l1_ids=l1 or None, cat_l2_ids=l2 or None,
            limit=limit, offset=offset, rerank=rerank, alpha=alpha,
            fusion=fusion, with_total=True,
        )
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"Search failed: {e}") from e

    is_browse = not q.strip()
    return {
        "mode": "browse" if is_browse else "search",
        "query": q,
        "total": total,
        "limit": limit,
        "offset": offset,
        # Only browse mode paginates over a known total; ranked search
        # returns a top-N slice and has no meaningful "page 2".
        "paginated": is_browse,
        "reranked": bool(rerank),
        "results": results,
    }


class ExplainRequest(BaseModel):
    query: str | None = ""
    q: str | None = None
    candidates: list[dict] | None = None
    suppliers: list[dict] | None = None


@app.post("/api/explain")
def explain(req: ExplainRequest):
    """Generate on-demand 1-sentence explanations for candidate suppliers."""
    query = req.query or req.q or ""
    candidates = req.candidates or req.suppliers or []
    try:
        from backend.reranker import explain_results
    except ImportError:
        from reranker import explain_results

    try:
        return explain_results(query, candidates)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"Explanation failed: {e}") from e


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("backend.api:app", host="0.0.0.0",
                port=int(os.environ.get("PORT", 8000)), reload=True)
