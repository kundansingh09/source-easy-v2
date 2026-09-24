import React, { useMemo, useState } from "react";
import { parseOverview, relevanceLevel } from "../overview.js";

const FALLBACK_ABOUT = "Semiconductor technology and equipment supplier.";

// hq_location alone was being shown as-is, so a value like "San Jose, CA"
// (city/state only, no country) never displayed a country at all - the old
// logic only fell back to hq_country when hq_location was MISSING entirely,
// not when it was present but incomplete. Fix: always surface the country,
// appending it only if it isn't already present in the location string
const KNOWN_COUNTRIES = new Set([
  "china", "japan", "taiwan", "south korea", "korea (south)", "germany", "united states",
  "usa", "u.s.a.", "us", "singapore", "netherlands", "united kingdom", "uk", "france",
  "switzerland", "austria", "italy", "hong kong", "malaysia", "poland", "canada",
  "belgium", "israel", "czech republic", "sweden", "finland", "ireland", "denmark",
  "spain", "australia", "india", "thailand", "vietnam", "philippines", "mexico",
  "brazil", "russia", "norway", "new zealand", "luxembourg", "liechtenstein"
]);

function normalizeCountry(c) {
  if (!c) return "";
  const lower = c.trim().toLowerCase();
  if (lower === "korea (south)" || lower === "korea, south" || lower === "republic of korea") {
    return "South Korea";
  }
  if (lower === "usa" || lower === "u.s.a." || lower === "us" || lower === "united states of america") {
    return "United States";
  }
  if (lower === "uk" || lower === "u.k.") {
    return "United Kingdom";
  }
  if (lower.includes("hong kong")) {
    return "Hong Kong";
  }
  if (lower.includes("taiwan")) {
    return "Taiwan";
  }
  if (lower.includes("p.r.china") || lower.includes("p.r. china")) {
    return "China";
  }
  return c.trim();
}

// Scraped directory data contains a mix of city/state only ("San Jose, CA"),
// local exhibitor branch locations ("Shanghai, China" for ASML), and country
// naming variations ("Korea (South)"). This standardizes aliases, avoids duplicate
// country mentions, and clearly attributes regional registrant branches when the
// local show office country differs from the parent corporate headquarters.
function formatHQ(hqLocation, hqCountry) {
  const country = hqCountry && hqCountry !== "Unknown" ? hqCountry.trim() : null;
  let loc = hqLocation ? hqLocation.trim() : null;

  if (loc && /^(#VALUE!|#N\/A|N\/A|UNKNOWN|-|NONE)$/i.test(loc)) {
    loc = null;
  }

  if (!loc) return country;
  if (!country) return loc;

  const normCountry = normalizeCountry(country);

  let cleanLoc = loc
    .replace(/\bKorea\s*\(South\)/gi, "South Korea")
    .replace(/\bP\.R\.China\b/gi, "China")
    .replace(/\bTaiwan,\s*China\b/gi, "Taiwan")
    .replace(/\bHong Kong,\s*China\b/gi, "Hong Kong");

  if (cleanLoc.toLowerCase().includes(normCountry.toLowerCase())) {
    return cleanLoc;
  }

  const parts = cleanLoc.split(",").map((p) => p.trim()).filter(Boolean);
  const lastPart = parts.length > 0 ? parts[parts.length - 1] : "";
  const normLast = normalizeCountry(lastPart).toLowerCase();

  if (normLast === normCountry.toLowerCase()) {
    return cleanLoc;
  }

  if (KNOWN_COUNTRIES.has(normLast)) {
    return `${normCountry} (Branch: ${cleanLoc})`;
  }

  return `${cleanLoc}, ${normCountry}`;
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

function CardLinks({ item, sources = [] }) {
  const links = [];
  const seenUrls = new Set();

  for (const s of sources) {
    const url = toSafeUrl(s.ebooth_url);
    if (url && !seenUrls.has(url)) {
      seenUrls.add(url);
      const label = s.location ? `${s.location} booth ↗` : "Expo booth ↗";
      links.push({ url, label });
    }
  }

  // Fallback to item.url if sources had no valid URLs
  const fallbackUrl = toSafeUrl(item.url);
  if (fallbackUrl && !seenUrls.has(fallbackUrl)) {
    seenUrls.add(fallbackUrl);
    links.push({ url: fallbackUrl, label: "Expo booth ↗" });
  }

  if (links.length === 0) return null;

  return (
    <div className="card-links">
      {links.map((link) => (
        <a key={link.url} href={link.url} target="_blank" rel="noopener noreferrer">
          {link.label}
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
      <CardLinks item={item} sources={sources} />

      <OverviewSection about={item.about} refined={item.refined} />
    </article>
  );
}