import React, { useEffect, useState } from "react";

const STAGES = [
  {
    id: "vectors",
    label: "Embedding & Vectors",
    desc: "Generating dense & BM25 sparse query vectors...",
  },
  {
    id: "retrieval",
    label: "Catalog Match",
    desc: "Executing hybrid vector + keyword search across suppliers...",
  },
  {
    id: "judge",
    label: "AI Procurement Judge",
    desc: "Evaluating technical capabilities & scoring RFQ fit with LLM...",
  },
];

export function ProcessingIndicator({ query, isReranking = true }) {
  const [stageIndex, setStageIndex] = useState(0);

  useEffect(() => {
    setStageIndex(0);
    if (!isReranking) return;

    const t1 = setTimeout(() => setStageIndex(1), 700);
    const t2 = setTimeout(() => setStageIndex(2), 1600);

    return () => {
      clearTimeout(t1);
      clearTimeout(t2);
    };
  }, [query, isReranking]);

  const activeStage = isReranking ? STAGES[stageIndex] : {
    id: "browse",
    label: "Filtering Catalogue",
    desc: "Retrieving suppliers matching active criteria...",
  };

  return (
    <div className="processing-banner" role="status" aria-live="polite">
      <div className="processing-glow-line" />
      <div className="processing-content">
        <div className="processing-header">
          <div className="processing-icon-wrap">
            <span className="processing-pulse-ring" />
            <svg
              className="processing-icon"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="2"
              strokeLinecap="round"
              strokeLinejoin="round"
            >
              <rect x="2" y="2" width="20" height="20" rx="5" ry="5" />
              <path d="M16 11.37A4 4 0 1 1 12.63 8 4 4 0 0 1 16 11.37z" />
              <line x1="17.5" y1="6.5" x2="17.51" y2="6.5" />
            </svg>
          </div>
          <div className="processing-info">
            <div className="processing-title">
              {isReranking ? (
                <>AI Sourcing Search: <span className="processing-query">"{query}"</span></>
              ) : (
                "Updating Catalog View..."
              )}
            </div>
            <div className="processing-desc">{activeStage.desc}</div>
          </div>
        </div>

        {isReranking && (
          <div className="processing-steps">
            {STAGES.map((step, idx) => {
              const isDone = idx < stageIndex;
              const isCurrent = idx === stageIndex;
              return (
                <div
                  key={step.id}
                  className={`processing-step ${isCurrent ? "is-current" : ""} ${isDone ? "is-done" : ""}`}
                >
                  <span className="processing-step-dot" />
                  <span className="processing-step-text">{step.label}</span>
                </div>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}

export function ResultCardSkeleton() {
  return (
    <div className="card skeleton-card" aria-hidden="true">
      <div className="skeleton-card-top">
        <div className="skeleton-col">
          <div className="skeleton-bar skeleton-title" />
          <div className="skeleton-bar skeleton-subtitle" />
        </div>
        <div className="skeleton-scorebox" />
      </div>
      <div className="skeleton-badges">
        <div className="skeleton-badge" />
        <div className="skeleton-badge" />
        <div className="skeleton-badge skeleton-badge-wide" />
      </div>
      <div className="skeleton-tags">
        <div className="skeleton-tag" />
        <div className="skeleton-tag skeleton-tag-wide" />
        <div className="skeleton-tag" />
      </div>
      <div className="skeleton-lines">
        <div className="skeleton-bar skeleton-line" />
        <div className="skeleton-bar skeleton-line skeleton-line-short" />
      </div>
    </div>
  );
}
