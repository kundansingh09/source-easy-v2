import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "./api.js";
import FilterPanel from "./components/FilterPanel.jsx";
import ResultCard from "./components/ResultCard.jsx";

const PAGE_SIZE = 10;

const CLEARED = {
  expo: "All",
  countries: [],
  l1: [],
  l2: [],
  // alpha stays user-tunable (retrieval-vs-judge weight); whether rerank
  // RUNS AT ALL is no longer a manual choice - see runSearch. Mentor's ask:
  // "don't need to select in filters", it should just happen for a real
  // search query and never for filter-only browsing.
  alpha: 0.4,
  // The query that produced the CURRENT results, as distinct from whatever
  // is sitting in the input box. Search now runs on an explicit submit, so
  // these two genuinely diverge while the user is typing, and category
  // highlighting must follow the applied query or tags would flicker
  // against a query that was never run.
  appliedQuery: "",
};

// How long to wait after the last filter/query change before actually
// firing a search. Filter clicks used to trigger a call immediately and
// unconditionally - harmless when a query search was a cheap hybrid
// lookup, but now that reranking runs automatically for every text query,
// three quick filter clicks while a query is active would fire three
// separate paid LLM calls, with only the last one's result kept. This
// collapses a burst of rapid changes into one actual call.
const SEARCH_DEBOUNCE_MS = 400;

// The default trade show the app opens with
const DEFAULT_EXPO = "India expo";

// The state the app loads with on first visit
const INITIAL_FILTERS = { ...CLEARED, expo: DEFAULT_EXPO };

export default function App() {
  const [draft, setDraft] = useState("");
  const [filters, setFilters] = useState(INITIAL_FILTERS);
  const [page, setPage] = useState(0);

  const [health, setHealth] = useState(null);
  const [facets, setFacets] = useState(null);
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [drawerOpen, setDrawerOpen] = useState(false);

  const reqId = useRef(0);
  const debounceRef = useRef(null);

  useEffect(() => {
    api.health().then(setHealth).catch((e) => setError(e.message));
  }, []);

  const activeCount =
    (filters.expo !== "All" ? 1 : 0) +
    filters.countries.length + filters.l1.length + filters.l2.length;
  const hasFilters = activeCount > 0;

  // Facets track the filter selection only - never the draft text - so the
  // sidebar doesn't churn on every keystroke.
  useEffect(() => {
    if (!health || health.status !== "ok") return;
    api.facets(filters).then(setFacets).catch(() => setFacets(null));
  }, [health, filters.expo, filters.countries, filters.l1, filters.l2]);

  const runSearch = useCallback(async (f, pageIndex) => {
    const isBrowse = !f.appliedQuery.trim();
    // commented to show results even when no filters are applied, so that the user can browse the catalogue without a query
    // if (!f.appliedQuery.trim() && !(
    //   f.expo !== "All" || f.countries.length || f.l1.length || f.l2.length
    // )) {
    //   setData(null);
    //   return;
    // }
    const id = ++reqId.current;
    setLoading(true);
    setError(null);
    try {
      const res = await api.search({
        query: f.appliedQuery,
        expo: f.expo === "All" ? null : f.expo,
        countries: f.countries, l1: f.l1, l2: f.l2,
        limit: isBrowse ? PAGE_SIZE : 10,
        offset: isBrowse ? pageIndex * PAGE_SIZE : 0,
        // Rerank automatically for any real query; never for filter-only
        // browsing (there's nothing to rerank - browse is sorted
        // alphabetically, not ranked, and always has been). The server
        // degrades this to false on its own if no key is configured, so
        // this is safe to send unconditionally.
        rerank: !isBrowse,
        alpha: f.alpha, fusion: "rrf",
      });
      // Drop responses that a newer request has already superseded, so a
      // slow rerank call can't overwrite fresher results.
      if (id === reqId.current) setData(res);
    } catch (e) {
      if (id === reqId.current) { setError(e.message); setData(null); }
    } finally {
      if (id === reqId.current) setLoading(false);
    }
  }, []);

  // Debounced: any change here (filter click, applied query, alpha) waits
  // SEARCH_DEBOUNCE_MS of quiet before actually firing. A burst of changes
  // within that window collapses to one call - the abuse case this exists
  // for is a user (or a script) clicking several filters in quick
  // succession while a query is active, which would otherwise fire one
  // full rerank per click.
  useEffect(() => {
    if (!health || health.status !== "ok") return;
    if (debounceRef.current) clearTimeout(debounceRef.current);
    debounceRef.current = setTimeout(() => {
      setPage(0);
      runSearch(filters, 0);
    }, SEARCH_DEBOUNCE_MS);
    return () => clearTimeout(debounceRef.current);
  }, [health, filters.expo, filters.countries, filters.l1, filters.l2,
      filters.alpha, filters.appliedQuery]);

  const submit = (e) => {
    e?.preventDefault();
    setDrawerOpen(false);
    setFilters((p) => ({ ...p, appliedQuery: draft }));
  };

  const goPage = (next) => {
    setPage(next);
    runSearch(filters, next);
    window.scrollTo({ top: 0, behavior: "smooth" });
  };

  const clearAll = () => {
    setDraft("");
    setPage(0);
    setFilters({ ...CLEARED });
  };

  const taxonomyLabel = useMemo(() => {
    const map = new Map();
    (facets?.l1 || []).forEach((o) => map.set(`l1-${o.id}`, o.label));
    (facets?.l2 || []).forEach((o) => map.set(`l2-${o.id}`, o.label));
    return map;
  }, [facets]);

  const chips = [];
  if (filters.expo !== "All") {
    chips.push({ key: "expo", label: filters.expo,
                 clear: () => setFilters((p) => ({ ...p, expo: "All" })) });
  }
  filters.countries.forEach((c) =>
    chips.push({ key: `c-${c}`, label: c,
      clear: () => setFilters((p) => ({ ...p, countries: p.countries.filter((x) => x !== c) })) }));
  filters.l1.forEach((id) =>
    chips.push({ key: `l1-${id}`, label: taxonomyLabel.get(`l1-${id}`) || `Category ${id}`,
      clear: () => setFilters((p) => ({ ...p, l1: p.l1.filter((x) => x !== id), l2: [] })) }));
  filters.l2.forEach((id) =>
    chips.push({ key: `l2-${id}`, label: taxonomyLabel.get(`l2-${id}`) || `Subcategory ${id}`,
      clear: () => setFilters((p) => ({ ...p, l2: p.l2.filter((x) => x !== id) })) }));

  const results = data?.results || [];
  const isBrowse = data?.mode === "browse";
  const pages = isBrowse ? Math.ceil((data?.total || 0) / PAGE_SIZE) : 1;

  // Rerank runs automatically for any real query (see runSearch), so
  // "loading a search with a query in the box" now specifically means
  // "waiting on the LLM judge", not just a fast DB lookup - the button
  // should say so rather than a generic "Searching…".
  const isProcessingLLM = loading && !!filters.appliedQuery.trim();
  const searchButtonLabel = isProcessingLLM
    ? "Processing"
    : loading ? "Searching…" : "Search";

  return (
    <>
      <header className="header">
        <div className="header-inner">
          <div className="brand">
            <span className="brand-mark">SEMICON India 2026</span>
            <span className="brand-sub">
              {health?.status === "ok" ? `${health.count.toLocaleString()} suppliers` : ""}
            </span>
          </div>

          <form className="searchbar" onSubmit={submit}>
            <div className="search-input-wrap">
              <input
                className="search-input"
                type="search"
                value={draft}
                onChange={(e) => setDraft(e.target.value)}
                placeholder="UHP nitrogen gas, wafer defect inspection, burn-in test…"
                aria-label="Search supplier pool"
              />
              {draft && (
                <button type="button" className="search-clear" aria-label="Clear search"
                        onClick={() => setDraft("")}>✕</button>
              )}
            </div>
            <button type="submit" className="btn btn-primary" disabled={loading}>
              {isProcessingLLM && <span className="spinner" aria-hidden="true" />}
              {searchButtonLabel}
            </button>
            <button type="button" className="btn btn-filters"
                    onClick={() => setDrawerOpen(true)}>
              Filters
              {activeCount > 0 && <span className="filter-count-badge">{activeCount}</span>}
            </button>
          </form>
        </div>
      </header>

      <div className="layout">
        <FilterPanel
          facets={facets}
          filters={filters}
          setFilters={setFilters}
          onClearAll={clearAll}
          open={drawerOpen}
          onClose={() => setDrawerOpen(false)}
          rerankAvailable={!!health?.rerank_available}
          totalIndexed={health?.count ?? 0}
        />

        <main>
          {chips.length > 0 && (
            <div className="chips">
              {chips.map((c) => (
                <span key={c.key} className="chip">
                  {c.label}
                  <button onClick={c.clear} aria-label={`Remove ${c.label} filter`}>✕</button>
                </span>
              ))}
              <button className="btn btn-ghost btn-sm" onClick={clearAll}>Clear all</button>
            </div>
          )}

          {health && health.status === "building" && (
            <div className="state">
              <div className="state-title">Building the index…</div>
              <div className="state-body">
                The server embeds the whole catalogue at startup. This takes a minute or
                two on a cold instance — refresh shortly.
              </div>
            </div>
          )}

          {error && (
            <div className="state is-error">
              <div className="state-title">Something went wrong</div>
              <div className="state-body">{error}</div>
            </div>
          )}

          {loading && !data && (
            <>{[0, 1, 2, 3].map((i) => <div key={i} className="skeleton" />)}</>
          )}

          {!loading && !error && !data && health?.status === "ok" && (
            <div className="state">
              <div className="state-title">Search the supplier pool</div>
              <div className="state-body">
                Enter a sourcing query and press Search, or pick a filter to browse the
                catalogue without a query.
              </div>
            </div>
          )}

          {data && results.length === 0 && !loading && (
            <div className="state">
              <div className="state-title">No suppliers matched</div>
              <div className="state-body">
                Try removing a filter, or searching a broader term.
              </div>
            </div>
          )}

          {data && results.length > 0 && (
            <>
              <div className="results-head">
                <div className="results-count">
                  {isBrowse
                    ? `${data.total.toLocaleString()} suppliers`
                    : `Top ${results.length} matches`}
                  {isBrowse && data.total > PAGE_SIZE &&
                    ` · showing ${data.offset + 1}–${data.offset + results.length}`}
                </div>
                <div className="results-note">
                  {isBrowse
                    ? "Filter-only browse — sorted alphabetically, not ranked."
                    : data.reranked
                      ? `Reranked by procurement judge · α=${filters.alpha.toFixed(2)}`
                      : "Hybrid search — dense semantics + BM25, fused by RRF."}
                </div>
              </div>

              {results.map((item) => (
                <ResultCard key={item.id} item={item} filters={filters} />
              ))}

              {isBrowse && pages > 1 && (
                <div className="pager">
                  <button className="btn btn-sm" disabled={page === 0 || loading}
                          onClick={() => goPage(page - 1)}>← Previous</button>
                  <span className="pager-pos">Page {page + 1} of {pages}</span>
                  <button className="btn btn-sm" disabled={page >= pages - 1 || loading}
                          onClick={() => goPage(page + 1)}>Next →</button>
                </div>
              )}
            </>
          )}
        </main>
      </div>
    </>
  );
}