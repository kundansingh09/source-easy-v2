import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "./api.js";
import FilterPanel from "./components/FilterPanel.jsx";
import ResultCard from "./components/ResultCard.jsx";
import { ProcessingIndicator, ResultCardSkeleton } from "./components/ProcessingIndicator.jsx";

const PAGE_SIZE = 10;

const CLEARED = {
  expo: "All",
  countries: [],
  alpha: 0.4,
  appliedQuery: "",
};

const SEARCH_DEBOUNCE_MS = 400;
const DEFAULT_EXPO = "All";
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
  const [isExplaining, setIsExplaining] = useState(false);
  const [explanations, setExplanations] = useState({});

  const reqId = useRef(0);
  const debounceRef = useRef(null);
  const skipNextDebounceRef = useRef(false);

  useEffect(() => {
    api.health().then(setHealth).catch((e) => setError(e.message));
  }, []);

  const activeCount =
    (filters.expo !== "All" ? 1 : 0) + filters.countries.length;
  const hasFilters = activeCount > 0;

  useEffect(() => {
    if (!health || health.status !== "ok") return;
    api.facets({ expo: filters.expo, countries: filters.countries })
      .then(setFacets)
      .catch(() => setFacets(null));
  }, [health, filters.expo, filters.countries]);

  const runSearch = useCallback(async (f, pageIndex) => {
    const isBrowse = !f.appliedQuery.trim();
    const id = ++reqId.current;
    setLoading(true);
    setError(null);
    setExplanations({});
    setIsExplaining(false);
    try {
      const res = await api.search({
        query: f.appliedQuery,
        expo: f.expo === "All" ? null : f.expo,
        countries: f.countries,
        limit: isBrowse ? PAGE_SIZE : 10,
        offset: isBrowse ? pageIndex * PAGE_SIZE : 0,
        rerank: !isBrowse,
        alpha: f.alpha,
        fusion: "rrf",
      });
      if (id === reqId.current) setData(res);
    } catch (e) {
      if (id === reqId.current) { setError(e.message); setData(null); }
    } finally {
      if (id === reqId.current) setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (!health || health.status !== "ok") return;
    if (skipNextDebounceRef.current) {
      skipNextDebounceRef.current = false;
      return;
    }
    if (debounceRef.current) clearTimeout(debounceRef.current);
    debounceRef.current = setTimeout(() => {
      setPage(0);
      runSearch(filters, 0);
    }, SEARCH_DEBOUNCE_MS);
    return () => clearTimeout(debounceRef.current);
  }, [health, filters.expo, filters.countries, filters.alpha, filters.appliedQuery]);

  const submit = (e) => {
    e?.preventDefault();
    setDrawerOpen(false);
    if (debounceRef.current) clearTimeout(debounceRef.current);
    skipNextDebounceRef.current = true;
    setPage(0);
    if (draft === filters.appliedQuery) {
      runSearch(filters, 0);
      return;
    }
    const updated = { ...filters, appliedQuery: draft };
    setFilters(updated);
    runSearch(updated, 0);
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
    setExplanations({});
    setIsExplaining(false);
  };

  const handleExplain = async () => {
    if (isExplaining || !results || results.length === 0) return;
    setIsExplaining(true);
    try {
      const query = filters.appliedQuery || data?.query || "";
      const res = await api.explain(query, results);
      setExplanations(res || {});
    } catch (e) {
      console.error("Failed to explain rankings:", e);
    } finally {
      setIsExplaining(false);
    }
  };

  const chips = [];
  if (filters.expo !== "All") {
    chips.push({ key: "expo", label: filters.expo,
                 clear: () => setFilters((p) => ({ ...p, expo: "All" })) });
  }
  filters.countries.forEach((c) =>
    chips.push({ key: `c-${c}`, label: c,
      clear: () => setFilters((p) => ({ ...p, countries: p.countries.filter((x) => x !== c) })) }));

  const results = data?.results || [];
  const isBrowse = data?.mode === "browse";
  const pages = isBrowse ? Math.ceil((data?.total || 0) / PAGE_SIZE) : 1;
  const hasExplanations = Object.keys(explanations).length > 0;

  const isProcessingLLM = loading && !!filters.appliedQuery.trim();
  const searchButtonLabel = isProcessingLLM
    ? "Processing"
    : loading ? "Searching…" : "Search";

  return (
    <>
      <header className="header">
        {loading && <div className="top-scanner" aria-hidden="true" />}
        <div className="header-inner">
          <div className="brand">
            <span className="brand-mark">SEMICON Global Sourcing</span>
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

          {isProcessingLLM && (
            <ProcessingIndicator query={filters.appliedQuery} isReranking={true} />
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

          {loading && (isProcessingLLM || !data) && (
            <div aria-label="Loading suppliers" aria-busy="true">
              {[0, 1, 2].map((i) => (
                <ResultCardSkeleton key={i} />
              ))}
            </div>
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

          {data && results.length > 0 && (!loading || !isProcessingLLM) && (
            <div className={loading ? "results-dimmed" : ""}>
              <div className="results-head">
                <div className="results-count">
                  {isBrowse
                    ? `${data.total.toLocaleString()} suppliers`
                    : `Top ${results.length} matches`}
                  {isBrowse && data.total > PAGE_SIZE &&
                    ` · showing ${data.offset + 1}–${data.offset + results.length}`}
                </div>
                <div className="results-explain-row">
                  <button
                    type="button"
                    className="btn btn-sm btn-explain"
                    onClick={handleExplain}
                    disabled={isExplaining || hasExplanations}
                  >
                    Explain Rankings
                  </button>
                  {isExplaining && (
                    <span className="explaining-inline">
                      <span className="spinner spinner-dark" aria-hidden="true" />
                      Explaining the ranks...
                    </span>
                  )}
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
                <ResultCard
                  key={item.id}
                  item={item}
                  filters={filters}
                  explanation={explanations[item.id] || explanations[String(item.id)]}
                  explanations={explanations}
                />
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
            </div>
          )}
        </main>
      </div>
    </>
  );
}