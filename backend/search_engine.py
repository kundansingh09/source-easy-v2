"""Hybrid supplier search over the SEMICON exhibitor catalogue.

Three things changed relative to the previous version:

  1. FACETING (Task 1) - the engine now exposes `facets()`, which returns the
     filter values that are still reachable given what the user has already
     selected, each with a count. Selecting "Taiwan" narrows the category
     lists to categories that Taiwanese suppliers actually have. Dead-end
     combinations become unselectable rather than merely disappointing.

  2. ZERO-QUERY BROWSE (Task 2) - `search()` forks. With a query it runs the
     dense+sparse prefetch and fuses. With an empty query and at least one
     filter it skips the embedding models entirely and uses `scroll()` with a
     `Filter`, returning every match sorted by company name.

  3. RERANK PLUMBING (Task 3) - the hybrid stage now hands a wider shortlist
     to `backend.reranker.rerank_with_llm`, which blends the fused retrieval
     score with an LLM judgement. The blend lives in the reranker module; this
     module only decides how deep a shortlist to hand over.

Also fixed: `from reranker import llm_rerank` was an un-packaged import. It
resolved when you ran `python backend/search_engine.py` (because Python puts
the script's own directory on sys.path) and raised ModuleNotFoundError under
Streamlit, where the project root is on sys.path instead. Since the frontend
never passed rerank=True, the break was invisible.
"""

import json
import os
import threading
import time
from collections import Counter

# Ensure FASTEMBED_CACHE_PATH points to a writable directory.
# On cloud platforms without a mounted volume, /var/cache is read-only for non-root users.
fastembed_cache = os.environ.get("FASTEMBED_CACHE_PATH", "/tmp/fastembed_cache")
try:
    os.makedirs(fastembed_cache, exist_ok=True)
    test_file = os.path.join(fastembed_cache, ".write_test")
    with open(test_file, "w") as f:
        f.write("ok")
    os.remove(test_file)
except (PermissionError, OSError):
    fastembed_cache = "/tmp/fastembed_cache"
    os.makedirs(fastembed_cache, exist_ok=True)
os.environ["FASTEMBED_CACHE_PATH"] = fastembed_cache

from qdrant_client import QdrantClient, models
try:
    from backend.text_clean import clean_overview
except ImportError:  # `python backend/search_engine.py` run directly, not as a package
    from text_clean import clean_overview

DENSE_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
SPARSE_MODEL = "Qdrant/bm25"
FALLBACK_ABOUT = "Semiconductor technology and equipment supplier."
UNKNOWN_COUNTRY = "Unknown"

# Fields the facet index needs. Kept narrow on purpose: this is scrolled in
# full at startup and held in RAM.
_FACET_FIELDS = ["company_name", "location", "hq_country",
                 "cat_l1_ids", "cat_l2_ids", "cat_tree"]


# --------------------------------------------------------------------------
# Facet matching. Pure functions over plain dicts so they can be unit-tested
# without a running Qdrant, and so the semantics are readable in one place.
# --------------------------------------------------------------------------

def _row_matches(row, expo=None, hq_countries=None, cat_l1_ids=None, cat_l2_ids=None):
    """Mirror of `build_filter` in Python. MUST stay semantically identical to
    it - if the two ever disagree, the UI will offer a filter combination that
    Qdrant then returns nothing for, which is exactly the bug we are fixing.
    """
    if expo and expo != "All" and expo not in row["expos"]:
        return False
    if hq_countries and row["country"] not in hq_countries:
        return False
    # L2 is more specific, so it supersedes L1 when both are present - same
    # precedence as build_filter's if/elif.
    if cat_l2_ids:
        if not (row["l2"] & {int(i) for i in cat_l2_ids}):
            return False
    elif cat_l1_ids:
        if not (row["l1"] & {int(i) for i in cat_l1_ids}):
            return False
    return True


BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DATA_PATH = os.path.join(BASE_DIR, "full-global-refined-hybrid.json")


class SourcingSearchEngine:
    def __init__(self, data_path=DEFAULT_DATA_PATH, taxonomy_path=None):
        if data_path and not os.path.isabs(data_path):
            data_path = os.path.join(BASE_DIR, data_path)
        if taxonomy_path and not os.path.isabs(taxonomy_path):
            taxonomy_path = os.path.join(BASE_DIR, taxonomy_path)
        self.data_path = data_path
        self.taxonomy_path = taxonomy_path

        # Connect to Cloud if credentials exist, otherwise use local RAM
        qdrant_url = os.environ.get("QDRANT_URL")
        qdrant_key = os.environ.get("QDRANT_API_KEY")
        if qdrant_url and qdrant_key:
            if qdrant_url.startswith("https://") and qdrant_url.endswith(":6333"):
                qdrant_url = qdrant_url[:-5]
            port = 443 if qdrant_url.startswith("https://") else 6333
            self.client = QdrantClient(url=qdrant_url, port=port, api_key=qdrant_key, timeout=60)
        else:
            self.client = QdrantClient(":memory:")
            
        self.collection_name = "semicon_suppliers"
        # Precautionary rather than a confirmed fix: 25 threads x 60 concurrent
        # searches produced 0 errors either way, and the lock costs nothing at
        # this scale. Left in place because Streamlit spawns a thread per
        # interaction and FastEmbed's batch dict is not documented as re-entrant.
        self.lock = threading.Lock()
        self.clean_overview = clean_overview
        self.l1_to_l2 = {}
        self.l1_names_by_id = {}
        self.l2_names_by_id = {}
        self._facet_rows = []
        self._refined_lookup = self._load_refined_lookup()
        self._init_collection()

    # ------------------------------------------------------------- loading

    def _load_refined_lookup(self):
        """Build an in-memory lookup of company_name/id -> refined dict from local file.
        Guarantees that even if Qdrant Cloud hasn't been re-indexed with 'refined' payloads yet,
        all search/browse results immediately carry the full refined intelligence."""
        lookup = {}
        if not self.data_path or not os.path.exists(self.data_path):
            return lookup
        try:
            with open(self.data_path, "r", encoding="utf-8") as f:
                suppliers = json.load(f)
            for idx, item in enumerate(suppliers):
                ref = item.get("refined")
                if ref:
                    name = (item.get("company_name") or "").strip().lower()
                    if name:
                        lookup[name] = ref
                    lookup[idx + 1] = ref
        except Exception as e:
            print(f"Warning: could not load refined lookup from {self.data_path}: {e}")
        return lookup

    def _build_text_chunk(self, item):
        """Text that gets embedded. Dense MiniLM and sparse BM25 index
        company name, refined summary, capabilities, technical expertise,
        role/segment, HQ, categories, and about.
        """
        parts = [item.get("company_name", "")]

        refined = item.get("refined") or {}
        if not refined and hasattr(self, "_refined_lookup"):
            key = (item.get("company_name") or "").strip().lower()
            refined = self._refined_lookup.get(key) or {}

        summary = (refined.get("summary") or "").strip()
        about = (item.get("about") or "").strip()
        has_real_about = bool(about) and about != FALLBACK_ABOUT

        if summary:
            parts.append(f"About: {summary}")
        elif has_real_about:
            embed_about = clean_overview(about)["cleaned"] if self.clean_overview else about
            parts.append(f"About: {embed_about}")

        role = refined.get("value_chain_position")
        if role and role != "Other":
            parts.append(f"Segment: {role}")

        caps = refined.get("capabilities") or []
        if caps:
            parts.append("Capabilities: " + "; ".join(caps[:6]))

        tech = refined.get("technical_expertise") or []
        if tech:
            parts.append("Expertise: " + "; ".join(tech[:4]))

        hq = item.get("hq_location") or item.get("hq_country")
        if hq and hq != UNKNOWN_COUNTRY:
            parts.append(f"HQ: {hq}")

        cats = list(dict.fromkeys(
            list(item.get("cat_l1_names", [])) + list(item.get("cat_l2_names", []))))
        if cats:
            parts.append("Categories: " + "; ".join(cats))

        if not summary and not has_real_about and about:
            parts.append(about)  # keep the fallback string as a weak tail signal

        return " | ".join(p for p in parts if p)

    def _init_collection(self):
            # --- CLOUD SKIP: if Qdrant already has the data, just build facets
            # and return -- no local file needed. This must come BEFORE the
            # file-existence check below, otherwise a missing/renamed JSON
            # silently exits and leaves _facet_rows empty, making count() return
            # 0 and the API appear broken even though Qdrant is fine.
            if self.client.collection_exists(self.collection_name):
                if self.client.get_collection(self.collection_name).points_count > 0:
                    print("Data already in Qdrant Cloud. Skipping heavy upload.")
                    self._build_facet_index()
                    return

            # Only reach here if Qdrant is empty / collection missing.
            # Local file is required to seed Qdrant -- fail clearly if absent.
            if not os.path.exists(self.data_path):
                print(f"WARNING: {self.data_path} not found and Qdrant has no data. "
                      f"Run force_re-index.py locally to seed Qdrant before deploying.")
                return
            with open(self.data_path) as f:
                suppliers = json.load(f)
            if not suppliers:
                return

            # Real L1 -> L2 map, built from the nested cat_tree captured at scrape
            # time (document order on the profile page).
            for item in suppliers:
                for node in item.get("cat_tree", []) or []:
                    l1_id = node.get("l1_id")
                    if l1_id is None:
                        continue
                    self.l1_names_by_id.setdefault(l1_id, node.get("l1_name", str(l1_id)))
                    bucket = self.l1_to_l2.setdefault(l1_id, {})
                    for child in node.get("children", []) or []:
                        bucket.setdefault(child["id"], child["name"])
                        self.l2_names_by_id.setdefault(child["id"], child["name"])

            print("Seeding Qdrant from local file...")

            self.client.create_collection(
                collection_name=self.collection_name,
                vectors_config={
                    "dense": models.VectorParams(
                        size=self.client.get_embedding_size(DENSE_MODEL),
                        distance=models.Distance.COSINE,
                    )
                },
                sparse_vectors_config={"sparse": models.SparseVectorParams()},
            )

            points = []
            for idx, item in enumerate(suppliers):
                text_chunk = self._build_text_chunk(item)
                points.append(
                    models.PointStruct(
                        id=idx + 1,
                        vector={
                            "dense": models.Document(text=text_chunk, model=DENSE_MODEL),
                            "sparse": models.Document(text=text_chunk, model=SPARSE_MODEL),
                        },
                        payload={
                            "company_name": item.get("company_name", "Unknown"),
                            # Post-dedupe data carries `locations` (every expo the
                            # company was seen at); pre-dedupe single-show data
                            # still carries the old single-string `location`.
                            # Normalising to a list here means every downstream
                            # consumer - filtering, faceting, display - only
                            # needs to handle one shape.
                            "location": (item.get("locations")
                                        or ([item["location"]] if item.get("location") else [])),
                            "hq_location": item.get("hq_location"),           # display
                            "hq_country": item.get("hq_country", UNKNOWN_COUNTRY),
                            "about": item.get("about", ""),
                            "refined": item.get("refined") or {},
                            "website": item.get("website"),
                            "ebooth_url": item.get("ebooth_url", "#"),  # primary/first-seen link
                            "sources": item.get("sources", []),  # {location, ebooth_url} per expo
                            "hq_country_conflict": item.get("hq_country_conflict", []),
                            "cat_l1_ids": item.get("cat_l1_ids", []),
                            "cat_l1_names": item.get("cat_l1_names", []),
                            "cat_l2_ids": item.get("cat_l2_ids", []),
                            "cat_l2_names": item.get("cat_l2_names", []),
                            "cat_tree": item.get("cat_tree", []),
                        },
                    )
                )

            self.client.upload_points(collection_name=self.collection_name, points=points)

            for field, schema in [
                ("location", models.PayloadSchemaType.KEYWORD),
                ("hq_country", models.PayloadSchemaType.KEYWORD),
                ("company_name", models.PayloadSchemaType.KEYWORD),
                ("cat_l1_ids", models.PayloadSchemaType.INTEGER),
                ("cat_l2_ids", models.PayloadSchemaType.INTEGER),
            ]:
                try:
                    self.client.create_payload_index(self.collection_name, field,
                                                    field_schema=schema)
                except Exception:
                    pass

            self._build_facet_index()

    # ------------------------------------------------------------- faceting

    def _build_facet_index(self):
        """One unfiltered scroll at startup into a compact in-RAM table.

        Why not `client.facet()`: facet() answers one field at a time, so a
        four-dimension sidebar costs four round trips *per rerun*, and Streamlit
        reruns on every keystroke. It also cannot express "count categories
        under the currently selected country but ignore the category filter
        itself", which is what drill-down faceting needs. At ~1.3k exhibitors
        the whole catalogue is a few hundred KB of ints and strings, so
        counting in Python is exact, instant, and dependency-free.

        Switch to `client.facet()` per dimension (server mode, indexed fields)
        once this outgrows roughly 100k points, or the moment ingestion becomes
        live rather than batch - this table is a snapshot.
        """
        rows, offset = [], None
        l1_names_by_id = {}
        l1_to_l2 = {}
        l2_names_by_id = {}
        while True:
            batch, offset = self.client.scroll(
                collection_name=self.collection_name,
                limit=1000, offset=offset,
                with_payload=_FACET_FIELDS, with_vectors=False,
            )
            for pt in batch:
                p = pt.payload or {}
                for node in (p.get("cat_tree") or []):
                    l1_id = node.get("l1_id")
                    if l1_id is None:
                        continue
                    l1_names_by_id.setdefault(l1_id, node.get("l1_name", str(l1_id)))
                    bucket = l1_to_l2.setdefault(l1_id, {})
                    for child in (node.get("children") or []):
                        bucket.setdefault(child["id"], child["name"])
                        l2_names_by_id.setdefault(child["id"], child["name"])

                rows.append({
                    "id": pt.id,
                    "name": p.get("company_name") or "",
                    # p.get("location") is normalised to a list at load time
                    # in _init_collection (see the comment there) - a company
                    # deduped across shows carries every expo it exhibits at,
                    # so it's discoverable and filterable under any of them.
                    "expos": p.get("location") or ["Unknown"],
                    "country": p.get("hq_country") or UNKNOWN_COUNTRY,
                    "l1": {int(i) for i in (p.get("cat_l1_ids") or [])},
                    "l2": {int(i) for i in (p.get("cat_l2_ids") or [])},
                })
            if offset is None:
                break
        self._facet_rows = rows
        if l1_names_by_id:
            self.l1_names_by_id = l1_names_by_id
            self.l1_to_l2 = l1_to_l2
            self.l2_names_by_id = l2_names_by_id

    def facets(self, expo=None, hq_countries=None, cat_l1_ids=None, cat_l2_ids=None):
        """Reachable filter values + counts, given the current selection.

        Drill-down semantics, the same convention Amazon and every mature
        faceted UI uses: each dimension is counted with every *other* active
        filter applied but not its own. Counting a dimension against its own
        selection would collapse it to the single value already chosen, and the
        user could never add a second country without first clearing the first.

        The hierarchy is the one exception - L2 counts do respect the L1
        selection, because an L2 option that is not a child of the chosen L1 is
        not a widening of the choice, it is a different branch entirely.

        Returns {"expos": [...], "countries": [...], "l1": [...], "l2": [...]},
        each a list of {"value"/"id", "label", "count"} sorted by count desc,
        and "total" for the current full selection.
        """
        rows = self._facet_rows

        def counted(key_fn, **skip):
            active = dict(expo=expo, hq_countries=hq_countries,
                          cat_l1_ids=cat_l1_ids, cat_l2_ids=cat_l2_ids)
            active.update(skip)
            c = Counter()
            for r in rows:
                if _row_matches(r, **active):
                    for k in key_fn(r):
                        c[k] += 1
            return c

        expo_counts = counted(lambda r: r["expos"], expo=None)
        country_counts = counted(lambda r: [r["country"]], hq_countries=None)
        l1_counts = counted(lambda r: r["l1"], cat_l1_ids=None, cat_l2_ids=None)

        # L2: honour the L1 selection, ignore the L2 selection, and only surface
        # subcategories that actually belong to the selected parents.
        allowed_l2 = None
        if cat_l1_ids:
            allowed_l2 = set()
            for l1 in cat_l1_ids:
                allowed_l2 |= set(self.l1_to_l2.get(int(l1), {}).keys())
        l2_counts = counted(
            lambda r: (r["l2"] & allowed_l2) if allowed_l2 is not None else r["l2"],
            cat_l2_ids=None,
        )

        total = sum(1 for r in rows if _row_matches(
            r, expo, hq_countries, cat_l1_ids, cat_l2_ids))

        def as_values(counter):
            return [{"value": v, "label": v, "count": n}
                    for v, n in sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))
                    if n > 0]

        def as_cats(counter, names):
            return [{"id": i, "label": names.get(i, str(i)), "count": n}
                    for i, n in sorted(counter.items(),
                                       key=lambda kv: (-kv[1], names.get(kv[0], "")))
                    if n > 0]

        return {
            "expos": as_values(expo_counts),
            "countries": as_values(country_counts),
            "l1": as_cats(l1_counts, self.l1_names_by_id),
            "l2": as_cats(l2_counts, self.l2_names_by_id),
            "total": total,
        }

    # ----------------------------------------------------------- filtering

    @staticmethod
    def build_filter(expo=None, hq_countries=None, cat_l1_ids=None, cat_l2_ids=None):
        """Hard metadata filter. Conditions are ANDed; values inside one
        condition are ORed (MatchAny).

        Hierarchy comes free: each point stores its own level-1 ancestors, so
        filtering on cat_l1_ids returns the whole branch including children.
        Passing cat_l2_ids narrows to specific leaves.
        """
        must = []
        if expo and expo != "All":
            # `location` is a list per point post-dedupe (every expo a
            # company was seen at). Qdrant's MatchValue against a
            # keyword-indexed array field matches if the value is anywhere
            # in the array, so this reads as "company exhibits at `expo`",
            # not "company's only expo is `expo`" - no change needed from
            # the single-value version, just documenting the assumption.
            must.append(models.FieldCondition(
                key="location", match=models.MatchValue(value=expo)))
        if hq_countries:
            must.append(models.FieldCondition(
                key="hq_country", match=models.MatchAny(any=list(hq_countries))))
        if cat_l2_ids:
            must.append(models.FieldCondition(
                key="cat_l2_ids", match=models.MatchAny(any=[int(i) for i in cat_l2_ids])))
        elif cat_l1_ids:
            must.append(models.FieldCondition(
                key="cat_l1_ids", match=models.MatchAny(any=[int(i) for i in cat_l1_ids])))
        return models.Filter(must=must) if must else None

    def _shape(self, point_id, payload, score=None):
        company_name = payload.get("company_name")
        company_key = (company_name or "").strip().lower()
        refined = (
            payload.get("refined")
            or getattr(self, "_refined_lookup", {}).get(company_key)
            or getattr(self, "_refined_lookup", {}).get(point_id)
            or {}
        )
        return {
            "id": point_id,
            "company_name": company_name,
            "locations": payload.get("location") or [],  # every expo, post-dedupe
            "hq_location": payload.get("hq_location"),
            "hq_country": payload.get("hq_country"),
            "about": payload.get("about"),
            "refined": refined,
            "website": payload.get("website"),
            "url": payload.get("ebooth_url"),
            "sources": payload.get("sources", []),  # per-expo booth link, when deduped
            "hq_country_conflict": payload.get("hq_country_conflict", []),
            "categories_l1": payload.get("cat_l1_names", []),
            "categories_l2": payload.get("cat_l2_names", []),
            "cat_tree": payload.get("cat_tree", []),
            "score": round(score, 4) if score is not None else None,
            "retrieval_score": round(score, 6) if score is not None else None,
        }

    # -------------------------------------------------------------- browse

    def browse(self, expo=None, hq_countries=None, cat_l1_ids=None, cat_l2_ids=None,
               limit=25, offset=0):
        """Zero-query mode. No embeddings, no fusion, no ranking - just the set
        of companies that satisfy the filters, alphabetised.

        Returns (rows, total). Sorting happens in Python rather than via
        scroll's `order_by` so that it works identically in local mode and does
        not depend on a keyword index existing; at catalogue scale the sort is
        trivial and the determinism is worth more than the microseconds.
        """
        query_filter = self.build_filter(expo, hq_countries, cat_l1_ids, cat_l2_ids)

        collected, page = [], None
        while True:
            batch, page = self.client.scroll(
                collection_name=self.collection_name,
                scroll_filter=query_filter,
                limit=1000, offset=page,
                with_payload=True, with_vectors=False,
            )
            collected.extend(batch)
            if page is None:
                break

        collected.sort(key=lambda pt: ((pt.payload or {}).get("company_name") or "").lower())
        total = len(collected)
        window = collected[offset:offset + limit] if limit else collected[offset:]
        return [self._shape(pt.id, pt.payload or {}) for pt in window], total

    # -------------------------------------------------------------- search

    def _hybrid_query(self, query, query_filter, candidates, fetch_n, fusion_method):
        with self.lock:
            return self.client.query_points(
                collection_name=self.collection_name,
                prefetch=[
                    models.Prefetch(
                        query=models.Document(text=query, model=DENSE_MODEL),
                        using="dense", limit=candidates, filter=query_filter,
                    ),
                    models.Prefetch(
                        query=models.Document(text=query, model=SPARSE_MODEL),
                        using="sparse", limit=candidates, filter=query_filter,
                    ),
                ],
                query=models.FusionQuery(fusion=fusion_method),
                limit=fetch_n,
            )

    def search(self, query: str, expo=None, hq_countries=None,
               cat_l1_ids=None, cat_l2_ids=None, limit: int = 5,
               candidates: int = 50, fusion: str = "rrf",
               rerank: bool = False, alpha: float = 0.4,
               offset: int = 0, with_total: bool = False):
        """Fork: query present -> hybrid retrieval; query empty -> filter browse.

        `rerank_depth` is deliberately separate from `candidates`. Prefetch
        pulls `candidates` (50) per branch because vector recall is cheap;
        the LLM sees only `rerank_depth` (20) because judgement is not. The old
        code reranked all 50, which was ~2.5x the tokens for candidates that
        fusion had already ranked out of contention.
        """
        query = (query or "").strip()

        # Dynamically scale rerank depth to 2x the display limit (minimum 20)
        rerank_depth = max(limit * 2, 20)

        # ---- Task 2: zero-query fork -------------------------------------
        if not query:
            # commenting out the "no filters, no results" logic to allow browsing without a query
            # has_filters = bool(
            #     (expo and expo != "All") or hq_countries or cat_l1_ids or cat_l2_ids)
            # if not has_filters:
            #     return ([], 0) if with_total else []
            rows, total = self.browse(expo, hq_countries, cat_l1_ids, cat_l2_ids,
                                      limit=limit, offset=offset)
            return (rows, total) if with_total else rows

        # ---- hybrid path --------------------------------------------------
        query_filter = self.build_filter(expo, hq_countries, cat_l1_ids, cat_l2_ids)
        fusion_method = models.Fusion.DBSF if fusion.lower() == "dbsf" else models.Fusion.RRF
        fetch_n = max(limit, rerank_depth) if rerank else limit

        # The filter MUST sit inside each prefetch branch. A top-level
        # query_filter is ignored when the outer query is a FusionQuery -
        # verified empirically. Inside the branches it constrains the vector
        # search itself, so you get `limit` matching results rather than
        # top-k-then-discard (which silently under-returns).
        t0 = time.perf_counter()
        response = self._hybrid_query(query, query_filter, candidates,
                                      fetch_n, fusion_method)
        qdrant_ms = (time.perf_counter() - t0) * 1000
        print(f"[DEBUG] Qdrant Search Time: {qdrant_ms:.2f} ms")

        results = [self._shape(r.id, r.payload, r.score) for r in response.points]

        if rerank and results:
            try:
                from backend.reranker import rerank_with_llm
            except ImportError:          # direct `python backend/search_engine.py`
                from reranker import rerank_with_llm
            results = rerank_with_llm(query, results, alpha=alpha, top_n=limit)
        else:
            results = results[:limit]

        return (results, len(results)) if with_total else results

    # ----------------------------------------------------------- UI helpers

    def level1_categories(self):
        """Master taxonomy, restricted to level-1 categories some ingested
        company actually carries."""
        return [{"id": k, "name": v}
                for k, v in sorted(self.l1_names_by_id.items(), key=lambda kv: kv[1])]

    def level2_names_for_l1(self, l1_id):
        """[(id, name), ...] of subcategories belonging to this parent."""
        return sorted(self.l1_to_l2.get(int(l1_id), {}).items(), key=lambda kv: kv[1])

    def available_countries(self):
        return [f["value"] for f in self.facets()["countries"]]

    def count(self):
        return len(self._facet_rows)


if __name__ == "__main__":
    engine = SourcingSearchEngine()
    print(f"{engine.count()} suppliers indexed")
    for row in engine.search("wafer defect inspection", limit=10):
        print(f"  {row['score']:<8} {row['hq_country']:<16} {row['company_name']}")