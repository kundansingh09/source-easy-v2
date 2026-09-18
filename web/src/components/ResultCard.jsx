import React, { useMemo, useState } from "react";
import { visibleCategories, allCategories } from "../categories.js";
import { parseOverview, relevanceLevel } from "../overview.js";

const FALLBACK_ABOUT = "Semiconductor technology and equipment supplier.";

// hq_location alone was being shown as-is, so a value like "San Jose, CA"
// (city/state only, no country) never displayed a country at all - the old
// logic only fell back to hq_country when hq_location was MISSING entirely,
// not when it was present but incomplete. Fix: always surface the country,
// appending it only if it isn't already present in the location string
// (avoids "Hsinchu, Taiwan, Taiwan" when the scraper already included it).
function formatHQ(hqLocation, hqCountry) {
  const country = hqCountry && hqCountry !== "Unknown" ? hqCountry : null;
  if (!hqLocation) return country;
  if (!country) return hqLocation;
  const already = hqLocation.toLowerCase().includes(country.toLowerCase());
  return already ? hqLocation : `${hqLocation}, ${country}`;
}

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

// The mentor's note was "if High is >20% of the distribution, maybe drop
// the relevance badge - it's not discriminating between suppliers." That
// distribution lives in report.txt from refine_overviews.py (the
// "semiconductor_relevance distribution" section), not in anything the
// frontend can see - so this is a manual, data-driven call, not something
// to guess at here. Flip this to false once you've checked it; left true
// (badge shown) until then so nothing silently disappears.
const SHOW_RELEVANCE_BADGE = true;

function InfoBadges({ parsed }) {
  const showRelevance = SHOW_RELEVANCE_BADGE && !!parsed.relevance;
  if (!parsed.booth && !parsed.valueChain && !showRelevance) return null;
  const level = relevanceLevel(parsed.relevance);
  return (
    <div className="badge-row">
      {parsed.booth && <span className="badge">Booth: {parsed.booth}</span>}
      {parsed.valueChain && <span className="badge">Segment: {parsed.valueChain}</span>}
      {showRelevance && (
        <span className={`badge badge-relevance${level ? ` is-${level}` : ""}`}>
          Relevance: {parsed.relevance}
        </span>
      )}
    </div>
  );
}

function TextGroup({ title, items }) {
  if (!items || items.length === 0) return null;
  return (
    <div className="overview-subsection">
      <div className="overview-subsection-label">{title}</div>
      <div className="overview-subsection-text">{items.join(", ")}.</div>
    </div>
  );
}

function ClampedText({ text, longThreshold = 320, open, onToggle }) {
  // open/onToggle come from the parent (OverviewSection) rather than owning
  // local state, so one "Read more" click reveals the capabilities/
  // expertise/end-markets sections at the same time as the full summary -
  // the mentor's ask was for all of it to be hidden together, not just the
  // summary paragraph on its own.
  //
  // longThreshold moved up from 180: the clamp is now 4 lines (was 2), so
  // the length at which a 4-line clamp actually visually truncates
  // anything is correspondingly higher - the old 180-char threshold was
  // tuned for a 2-line clamp and would show a "Read more" button on text
  // that no longer overflows 4 lines.
  const longEnough = text.length > longThreshold;
  return (
    <div>
      <div className={`overview${open || !longEnough ? "" : " overview-clamp"}`}>{text}</div>
      {longEnough && (
        <button className="overview-toggle" onClick={onToggle}>
          {open ? "Show less" : "Read more"}
        </button>
      )}
    </div>
  );
}

function OverviewSection({ about }) {
  const parsed = useMemo(() => parseOverview(about), [about]);
  const [open, setOpen] = useState(false);
  const toggle = () => setOpen((v) => !v);

  if (!parsed.structured) {
    // Legacy format - identical behavior to the original Overview component.
    if (!parsed.summary || parsed.summary === FALLBACK_ABOUT) {
      return <div className="overview overview-empty">No overview provided by this exhibitor.</div>;
    }
    return <ClampedText text={parsed.summary} open={open} onToggle={toggle} />;
  }

  const hasExtra = Boolean(
    parsed.capabilities.length || parsed.technicalExpertise.length
    || parsed.endMarkets.length || parsed.indiaPresence
  );
  // If the summary itself is short (won't clamp) but there's extra detail,
  // ClampedText renders no toggle of its own - so a toggle still needs to
  // show up somewhere, and this is it.
  const needsOwnToggle = hasExtra && parsed.summary.length <= 320;

  return (
    <div className="overview-structured">
      {parsed.summary && <ClampedText text={parsed.summary} open={open} onToggle={toggle} />}

      {needsOwnToggle && !open && (
        <button className="overview-toggle" onClick={toggle}>
          Read more (capabilities, expertise, end markets)
        </button>
      )}

      {hasExtra && open && (
        <>
          <TextGroup title="Key Capabilities" items={parsed.capabilities} />
          <TextGroup title="Technical Expertise" items={parsed.technicalExpertise} />
          <TextGroup title="End Markets" items={parsed.endMarkets} />
          {parsed.indiaPresence && (
            <div className="callout">
              <div className="callout-label">India Presence</div>
              <div className="callout-body">{parsed.indiaPresence}</div>
            </div>
          )}
        </>
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

function CardLinks({ item, sources }) {
  if (!item.website && sources.length <= 1) return null;
  return (
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
  );
}

export default function ResultCard({ item, filters }) {
  const hq = formatHQ(item.hq_location, item.hq_country);
  const locations = item.locations || [];
  const sources = item.sources || [];
  const parsed = useMemo(() => parseOverview(item.about), [item.about]);

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

      {/* Booth/Segment/Relevance, tags, and website/booth links all moved
          above the overview - they're the scannable, glanceable facts;
          the free-text overview is the heaviest read, so it goes last and
          collapsed. */}
      <InfoBadges parsed={parsed} />
      <CategoryTags item={item} filters={filters} />
      <CardLinks item={item} sources={sources} />

      <OverviewSection about={item.about} />
    </article>
  );
}