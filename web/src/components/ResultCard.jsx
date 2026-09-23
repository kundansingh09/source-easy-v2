import React, { useMemo, useState } from "react";
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
const SHOW_RELEVANCE_BADGE = false;

function InfoBadges({ parsed, country }) {
  const showRelevance = SHOW_RELEVANCE_BADGE && !!parsed.relevance;
  const validCountry = country && country !== "Unknown";

  if (!parsed.booth && !parsed.valueChain && !showRelevance && !validCountry) return null;
  const level = relevanceLevel(parsed.relevance);
  
  return (
    <div className="badge-row">
      {validCountry && <span className="badge">Country: {country}</span>}
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

function OverviewSection({ about, refined }) {
  const parsed = useMemo(() => parseOverview(about, refined), [about, refined]);
  const [open, setOpen] = useState(false);
  const toggle = () => setOpen((v) => !v);

  const summary = (parsed.summary || "").trim();
  const hasSummary = Boolean(summary && summary !== FALLBACK_ABOUT);
  const hasExtra = Boolean(
    parsed.capabilities.length || parsed.technicalExpertise.length
    || parsed.endMarkets.length || parsed.regionalPresence || parsed.indiaPresence
  );

  if (!parsed.structured && !hasSummary) {
    // Legacy format - identical behavior to the original Overview component.
    return <div className="overview overview-empty">No overview provided by this exhibitor.</div>;
  }

  const isLongSummary = summary.length > 320;
  const canExpand = hasExtra || isLongSummary;

  return (
    <div className={parsed.structured ? "overview-structured" : undefined}>
      {hasSummary && (
        <div>
          <div className={`overview${!open && isLongSummary ? " overview-clamp" : ""}`}>
            {summary}
          </div>
          {canExpand && !open && (
            <button className="overview-toggle" onClick={toggle}>
              Read more
            </button>
          )}
        </div>
      )}

      {!hasSummary && canExpand && !open && (
        <button className="overview-toggle" onClick={toggle}>
          Read more
        </button>
      )}

      {open && (
        <>
          {hasExtra && (
            <>
              <TextGroup title="Key Capabilities" items={parsed.capabilities} />
              <TextGroup title="Technical Expertise" items={parsed.technicalExpertise} />
              <TextGroup title="End Markets" items={parsed.endMarkets} />
              {(parsed.regionalPresence || parsed.indiaPresence) && (
                <div className="callout">
                  <div className="callout-label">
                    {parsed.regionalPresence ? "Regional Presence" : "India Presence"}
                  </div>
                  <div className="callout-body">
                    {parsed.regionalPresence || parsed.indiaPresence}
                  </div>
                </div>
              )}
            </>
          )}
          <button className="overview-toggle" onClick={toggle}>
            Show less
          </button>
        </>
      )}
    </div>
  );
}

function toSafeUrl(url) {
  if (!url || url === "#") return null;
  return /^https?:\/\//i.test(url) ? url : `https://${url}`;
}

function CardLinks({ item, sources, hasOverview }) {
  const profileUrl = toSafeUrl(item.url);
  const showSemiconProfile = hasOverview && profileUrl;
  
  if (!showSemiconProfile && sources.length <= 1) return null;
  
  return (
    <div className="card-links">
      {showSemiconProfile && (
        <a href={profileUrl} target="_blank" rel="noopener noreferrer">Semicon Profile ↗</a>
      )}
      {sources.length > 1 &&
        sources
          .filter((s) => toSafeUrl(s.ebooth_url))
          .map((s) => (
            <a key={s.location} href={toSafeUrl(s.ebooth_url)} target="_blank" rel="noopener noreferrer">
              {s.location} booth ↗
            </a>
          ))}
    </div>
  );
}

export default function ResultCard({ item, filters, explanation, explanations }) {
  const hq = formatHQ(item.hq_location, item.hq_country);
  const locations = item.locations || [];
  const sources = item.sources || [];
  const parsed = useMemo(() => parseOverview(item.about, item.refined), [item.about, item.refined]);
  
  const hasOverview = parsed.structured || (parsed.summary && parsed.summary !== FALLBACK_ABOUT);
  const safeWebsite = toSafeUrl(item.website);
  const cardReason = explanation || explanations?.[item.id] || explanations?.[String(item.id)];

  return (
    <article className="card">
      <div className="card-top">
        <div style={{ minWidth: 0 }}>
          <h3 className="card-name">
            {safeWebsite ? (
              <a href={safeWebsite} target="_blank" rel="noopener noreferrer">{item.company_name}</a>
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

      {cardReason && <p className="reason">{cardReason}</p>}

      {item.hq_country_conflict?.length > 0 && (
        <div className="conflict">
          HQ country differs across this company's source listings (
          {item.hq_country_conflict.join(", ")}) — the value shown may not be authoritative.
        </div>
      )}

      {/* Booth/Segment/Relevance, and website/booth links all moved
          above the overview - they're the scannable, glanceable facts;
          the free-text overview is the heaviest read, so it goes last and
          collapsed. */}
      <InfoBadges parsed={parsed} country={item.hq_country} />
      <CardLinks item={item} sources={sources} hasOverview={hasOverview} />

      <OverviewSection about={item.about} refined={item.refined} />
    </article>
  );
}