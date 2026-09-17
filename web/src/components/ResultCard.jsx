import React, { useState } from "react";
import { visibleCategories, allCategories } from "../categories.js";

const FALLBACK_ABOUT = "Semiconductor technology and equipment supplier.";

function Scores({ item }) {
  // Reranked results carry a blended final score plus its two components;
  // plain hybrid results carry only the fused retrieval score. Showing the
  // breakdown rather than one opaque number is what makes a ranking
  // arguable to a buyer who has to justify the shortlist.
  if (item.final_score != null) {
    return (
      <div className="scorebox">
        <div className="score is-primary">
          <div className="score-val">{item.final_score.toFixed(2)}</div>
          <div className="score-key">Final</div>
        </div>
        {item.llm_score != null && (
          <div className="score">
            <div className="score-val">{item.llm_score.toFixed(2)}</div>
            <div className="score-key">Judge</div>
          </div>
        )}
        {item.retrieval_norm != null && (
          <div className="score">
            <div className="score-val">{item.retrieval_norm.toFixed(2)}</div>
            <div className="score-key">Search</div>
          </div>
        )}
      </div>
    );
  }
  if (item.score == null) return null; // browse mode ranks nothing
  return (
    <div className="scorebox">
      <div className="score">
        <div className="score-val">{Number(item.score).toFixed(3)}</div>
        <div className="score-key">Score</div>
      </div>
    </div>
  );
}

function Overview({ text }) {
  const [open, setOpen] = useState(false);
  if (!text || text === FALLBACK_ABOUT) {
    return <div className="overview" style={{ fontStyle: "italic", opacity: 0.7 }}>
      No overview provided by this exhibitor.
    </div>;
  }
  // Two-line clamp by default. The old collapsed-by-default expander hid
  // everything, so scanning a list meant clicking every card; a clamped
  // preview lets you skim and only expand what looks promising.
  const longEnough = text.length > 180;
  return (
    <div>
      <div className={`overview${open || !longEnough ? "" : " overview-clamp"}`}>{text}</div>
      {longEnough && (
        <button className="overview-toggle" onClick={() => setOpen((v) => !v)}>
          {open ? "Show less" : "Read more"}
        </button>
      )}
    </div>
  );
}

function CategoryTags({ item, filters }) {
  const [expanded, setExpanded] = useState(false);
  const opts = {
    selectedL1: filters.l1,
    selectedL2: filters.l2,
    query: filters.appliedQuery,
  };
  const { shown, hiddenCount, total, hasHits } = visibleCategories(item.cat_tree, opts, 6);
  if (!total) return null;

  const list = expanded ? allCategories(item.cat_tree, opts) : shown;
  const hitKeys = new Set(
    hasHits ? visibleCategories(item.cat_tree, opts, 999).shown.map((e) => e.key) : []
  );

  return (
    <div className="tags">
      {list.map((e) => (
        <span
          key={e.key}
          className={`tag${hitKeys.has(e.key) ? " is-hit" : ""}`}
          title={e.parent ? `${e.parent} › ${e.label}` : e.label}
        >
          {e.label}
        </span>
      ))}
      {!expanded && hiddenCount > 0 && (
        <button className="tag-more" onClick={() => setExpanded(true)}>
          +{hiddenCount} more
        </button>
      )}
      {expanded && (
        <button className="tag-more" onClick={() => setExpanded(false)}>
          Show fewer
        </button>
      )}
    </div>
  );
}

export default function ResultCard({ item, filters }) {
  const hq = item.hq_location || (item.hq_country !== "Unknown" ? item.hq_country : null);
  const locations = item.locations || [];
  const sources = item.sources || [];

  return (
    <article className="card">
      <div className="card-top">
        <div style={{ minWidth: 0 }}>
          <h3 className="card-name">
            {item.url && item.url !== "#" ? (
              <a href={item.url} target="_blank" rel="noopener noreferrer">{item.company_name}</a>
            ) : (
              item.company_name
            )}
          </h3>
          <div className="card-meta">
            {hq && <span><strong>HQ</strong> {hq}</span>}
            {locations.length > 0 && (
              <span>
                <strong>{locations.length > 1 ? "Shows" : "Show"}</strong>{" "}
                {locations.join(", ")}
              </span>
            )}
          </div>
        </div>
        <Scores item={item} />
      </div>

      {item.llm_reason && <p className="reason">{item.llm_reason}</p>}

      {item.hq_country_conflict?.length > 0 && (
        <div className="conflict">
          HQ country differs across this company's source listings (
          {item.hq_country_conflict.join(", ")}) — the value shown may not be authoritative.
        </div>
      )}

      <Overview text={item.about} />
      <CategoryTags item={item} filters={filters} />

      {(item.website || sources.length > 1) && (
        <div className="card-links">
          {item.website && (
            <a href={item.website} target="_blank" rel="noopener noreferrer">Website ↗</a>
          )}
          {sources.length > 1 &&
            sources
              .filter((s) => s.ebooth_url && s.ebooth_url !== "#")
              .map((s) => (
                <a key={s.location} href={s.ebooth_url} target="_blank" rel="noopener noreferrer">
                  {s.location} booth ↗
                </a>
              ))}
        </div>
      )}
    </article>
  );
}
