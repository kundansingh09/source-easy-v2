import React, { useState } from "react";

/** One facet dimension: checkbox list with counts, capped with a show-more. */
function FacetList({ label, options, selected, onToggle, idKey = "value", initial = 6 }) {
  const [expanded, setExpanded] = useState(false);
  const sel = new Set(selected.map(String));

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
  const f = facets || { expos: [], countries: [], total: 0 };

  const toggleIn = (key, value) =>
    setFilters((prev) => {
      const list = prev[key] || [];
      const has = list.some((v) => String(v) === String(value));
      return {
        ...prev,
        [key]: has ? list.filter((v) => String(v) !== String(value)) : [...list, value],
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