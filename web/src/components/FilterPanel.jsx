import React, { useState } from "react";

/** One facet dimension: checkbox list with counts, capped with a show-more. */
function FacetList({ label, options, selected, onToggle, idKey = "value", initial = 6 }) {
  const [expanded, setExpanded] = useState(false);
  const sel = new Set(selected.map(String));

  // Selected values are pinned to the top so they never hide under a
  // collapsed "show more" - otherwise a user can't see (or undo) a filter
  // they just applied.
  const ordered = [
    ...options.filter((o) => sel.has(String(o[idKey]))),
    ...options.filter((o) => !sel.has(String(o[idKey]))),
  ];
  const shown = expanded ? ordered : ordered.slice(0, initial);
  const hidden = ordered.length - shown.length;

  return (
    <div className="facet-group">
      <div className="facet-label">{label}</div>
      {options.length === 0 ? (
        <div className="facet-empty">None available for this selection</div>
      ) : (
        <>
          <div className="facet-list">
            {shown.map((o) => {
              const id = o[idKey];
              const checked = sel.has(String(id));
              return (
                <label key={id} className={`facet-option${checked ? " is-checked" : ""}`}>
                  <input type="checkbox" checked={checked} onChange={() => onToggle(id)} />
                  <span className="facet-name" title={o.label}>{o.label}</span>
                  <span className="facet-num">{o.count}</span>
                </label>
              );
            })}
          </div>
          {hidden > 0 && (
            <button className="facet-more" onClick={() => setExpanded(true)}>
              Show {hidden} more
            </button>
          )}
          {expanded && ordered.length > initial && (
            <button className="facet-more" onClick={() => setExpanded(false)}>
              Show less
            </button>
          )}
        </>
      )}
    </div>
  );
}

export default function FilterPanel({
  facets, filters, setFilters, onClearAll, open, onClose,
  rerankAvailable, totalIndexed,
}) {
  const f = facets || { expos: [], countries: [], l1: [], l2: [], total: 0 };

  const toggleIn = (key, value) =>
    setFilters((prev) => {
      const list = prev[key];
      const has = list.some((v) => String(v) === String(value));
      return {
        ...prev,
        [key]: has ? list.filter((v) => String(v) !== String(value)) : [...list, value],
        // Changing L1 can orphan an L2 pick from a different branch. The
        // engine lets L2 override L1, so a stale child would silently widen
        // results back out of the chosen parent - drop them instead.
        ...(key === "l1" ? { l2: [] } : {}),
      };
    });

  return (
    <>
      {open && <div className="drawer-backdrop" onClick={onClose} />}
      <aside className={`sidebar${open ? " is-open" : ""}`} aria-label="Filters">
        <div className="sidebar-head">
          <div className="sidebar-title">Filters</div>
          <div style={{ display: "flex", gap: 4 }}>
            <button className="btn btn-ghost btn-sm" onClick={onClearAll}>Clear all</button>
            <button className="btn btn-ghost btn-sm sidebar-close" onClick={onClose}
                    aria-label="Close filters">✕</button>
          </div>
        </div>

        <div className="facet-group">
          <div className="facet-label">Trade show</div>
          <select
            className="select"
            value={filters.expo || "All"}
            onChange={(e) => {
              const newExpo = e.target.value;
              setFilters((p) => ({
                ...p,
                expo: newExpo,
                // Category/Subcategory are hidden while India expo is
                // selected (see below) - clear them on switching IN so a
                // filter picked earlier doesn't keep narrowing results
                // invisibly, with no control left to see or remove it.
                ...(newExpo === "India expo" ? { l1: [], l2: [] } : {}),
              }));
            }}
          >
            <option value="All">All shows ({totalIndexed})</option>
            {f.expos.map((o) => (
              <option key={o.value} value={o.value}>{o.label} ({o.count})</option>
            ))}
          </select>
          <div className="hint">Where the company exhibits — not where it's based.</div>
        </div>

        <FacetList
          label="Supplier HQ country"
          options={f.countries}
          selected={filters.countries}
          onToggle={(v) => toggleIn("countries", v)}
        />

        {filters.expo !== "India expo" && (
          <>
            <FacetList
              label="Category"
              options={f.l1}
              selected={filters.l1}
              onToggle={(v) => toggleIn("l1", v)}
              idKey="id"
            />

            <FacetList
              label="Subcategory"
              options={f.l2}
              selected={filters.l2}
              onToggle={(v) => toggleIn("l2", v)}
              idKey="id"
              initial={8}
            />
          </>
        )}

        <div className="facet-group">
          <div className="facet-label">Ranking</div>
          {rerankAvailable ? (
            <div className="hint" style={{ marginTop: 0 }}>
              LLM reranking runs automatically for text searches — no need to
              turn it on. It never runs for filter-only browsing.
            </div>
          ) : (
            <div className="hint" style={{ marginTop: 0 }}>
              LLM reranking is unavailable — the server has no OPENAI_API_KEY
              set. Searches use hybrid retrieval only.
            </div>
          )}
        </div>
      </aside>
    </>
  );
}