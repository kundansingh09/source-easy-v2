// Parses supplier.about into structured fields when it carries the labeled
// enrichment format ("Booth: X. Value Chain Role: Y. ..."), and falls back
// to treating the whole string as a plain summary when it doesn't - which
// covers every legacy record (China/Taiwan/Korea/Japan/Europe expos, and
// the FALLBACK_ABOUT placeholder itself).
//
// Detection is presence-based, not position-based: it finds every label
// wherever it appears and slices the text between consecutive matches, so
// it doesn't assume a fixed field order or that every field is present.
// That matters because this looks like LLM-generated enrichment (the "Key
// Capabilities:" / "Technical Expertise:" phrasing is a template, not
// scraped prose), and a template can drop a field or reorder across runs
// without the parser breaking.
//
// Known limitation, not fully solvable from the frontend: list fields
// (capabilities, technicalExpertise, endMarkets) are split on ", " with no
// deeper parsing. If a single item's own description legitimately contains
// a comma, it will be split into two items. Cheap to notice by eye in the
// UI if it happens; not worth a heavier parser until real data shows it's
// a real problem.

const FALLBACK_ABOUT = "Semiconductor technology and equipment supplier.";

const LABEL_DEFS = [
  { key: "booth", label: "Booth" },
  { key: "valueChain", label: "Value Chain Role" },
  { key: "relevance", label: "Semiconductor Relevance" },
  { key: "capabilities", label: "Key Capabilities", list: true },
  { key: "technicalExpertise", label: "Technical Expertise", list: true },
  { key: "endMarkets", label: "End Markets", list: true },
  { key: "indiaPresence", label: "India Presence" },
];

// Longest-label-first in the alternation so a shorter label can never
// shadow a longer one that starts with the same words (defensive - none of
// the current labels actually collide, but cheap to keep safe as more get
// added later).
const ALTERNATION = LABEL_DEFS.map((d) => d.label)
  .sort((a, b) => b.length - a.length)
  .join("|");
const LABEL_RE = new RegExp(`\\b(${ALTERNATION}):\\s*`, "gi");

// Booth/Role/Relevance are meant to be short codes or single words, not
// sentences. Without a bound, a label with nothing recognized after it
// (commonly: "Booth:" is the ONLY label present in a thin record) captures
// everything to the end of the string - including a whole unrelated
// trailing sentence - because the slicing rule is "up to the next matched
// label, or end of string if there is none". A 40-character value with a
// mid-string ". Capital" is not a booth code, it's a swallowed sentence.
function looksLikeShortValue(value, maxLen) {
  if (!value) return false;
  if (value.length > maxLen) return false;
  // A period followed by whitespace and a capital letter mid-value means a
  // new sentence started inside what should have been one short field -
  // the parser only ever trims a single TRAILING period, so an internal
  // one like this is always a sign of over-capture, never legitimate.
  if (/\.\s+[A-Z]/.test(value)) return false;
  return true;
}

function emptyResult(summary) {
  return {
    summary, booth: null, valueChain: null, relevance: null,
    capabilities: [], technicalExpertise: [], endMarkets: [],
    regionalPresence: null, indiaPresence: null, structured: false,
  };
}

export function parseOverview(about, refined) {
  // If refined object is provided and non-empty, use structured fields directly
  if (refined && typeof refined === "object" && Object.keys(refined).length > 0) {
    const summary = (refined.summary || about || "").trim();
    const capabilities = Array.isArray(refined.capabilities)
      ? refined.capabilities
      : (refined.capabilities ? [refined.capabilities] : []);
    const technicalExpertise = Array.isArray(refined.technical_expertise)
      ? refined.technical_expertise
      : (refined.technical_expertise ? [refined.technical_expertise] : []);
    const endMarkets = Array.isArray(refined.end_markets)
      ? refined.end_markets
      : (refined.end_markets ? [refined.end_markets] : []);
    const valueChain = refined.value_chain_position || null;
    const relevance = refined.semiconductor_relevance || null;

    let regionalPresence = null;
    if (typeof refined.regional_presence === "string") {
      regionalPresence = refined.regional_presence.trim();
    } else if (refined.regional_presence && typeof refined.regional_presence === "object") {
      const parts = Object.entries(refined.regional_presence)
        .map(([k, v]) => (v ? `${k}: ${v}` : k));
      if (parts.length > 0) regionalPresence = parts.join("; ");
    }
    const indiaPresence = refined.india_presence || regionalPresence || null;

    const hasStructured = Boolean(
      valueChain || relevance || capabilities.length || technicalExpertise.length || endMarkets.length || regionalPresence || indiaPresence
    );

    return {
      summary,
      booth: null,
      valueChain,
      relevance,
      capabilities,
      technicalExpertise,
      endMarkets,
      regionalPresence,
      indiaPresence,
      structured: hasStructured,
    };
  }

  const text = (about || "").trim();
  if (!text) return emptyResult("");

  const matches = [...text.matchAll(LABEL_RE)];
  if (matches.length === 0) return emptyResult(text); // legacy format, incl. FALLBACK_ABOUT itself

  const result = {
    summary: text.slice(0, matches[0].index).trim(),
    booth: null, valueChain: null, relevance: null,
    capabilities: [], technicalExpertise: [], endMarkets: [],
    regionalPresence: null, indiaPresence: null, structured: true,
  };

  for (let i = 0; i < matches.length; i++) {
    const labelText = matches[i][1];
    const def = LABEL_DEFS.find((d) => d.label.toLowerCase() === labelText.toLowerCase());
    if (!def) continue;

    const start = matches[i].index + matches[i][0].length;
    const end = i + 1 < matches.length ? matches[i + 1].index : text.length;
    // Trailing period trimmed only when it's the very last character of the
    // field, so it closes out the field's own sentence without eating a
    // period that's part of an abbreviation earlier in the value.
    const value = text.slice(start, end).trim().replace(/\.\s*$/, "");
    if (!value) continue;

    result[def.key] = def.list
      ? value.split(",").map((s) => s.trim()).filter(Boolean)
      : value;
  }

  // Reject swallowed values on the three short fields rather than display
  // them as-is - a corrupted "Booth" badge is worse than no badge.
  if (result.booth && !looksLikeShortValue(result.booth, 20)) result.booth = null;
  if (result.valueChain && !looksLikeShortValue(result.valueChain, 40)) result.valueChain = null;
  if (result.relevance && !looksLikeShortValue(result.relevance, 20)) result.relevance = null;

  // Same failure mode is theoretically possible on any field that ends up
  // being the LAST matched label with nothing recognized after it - not
  // observed on these in real data yet (only booth, so far), but a
  // generous cap costs nothing and prevents a silent repeat. indiaPresence
  // gets a looser cap since it's meant to be an actual paragraph.
  const capList = (items, max) => (items.join(", ").length > max ? [] : items);
  result.capabilities = capList(result.capabilities, 600);
  result.technicalExpertise = capList(result.technicalExpertise, 600);
  result.endMarkets = capList(result.endMarkets, 600);
  if (result.indiaPresence && result.indiaPresence.length > 900) result.indiaPresence = null;

  // A label technically matched, but if every field it produced got
  // rejected (or every list field ended up empty), there's nothing
  // structured left worth a special layout - fall back to the ENTIRE
  // original text as a plain summary, not the truncated pre-label
  // "summary" computed above, since that would silently drop the part
  // that came after the (now-rejected) label.
  const hasContent = Boolean(
    result.booth || result.valueChain || result.relevance
    || result.capabilities.length || result.technicalExpertise.length
    || result.endMarkets.length || result.indiaPresence
  );
  if (!hasContent) return emptyResult(text);

  return result;
}

export function relevanceLevel(value) {
  const v = (value || "").toLowerCase().trim();
  if (v.startsWith("high")) return "high";
  if (v.startsWith("medium")) return "medium";
  if (v.startsWith("low")) return "low";
  return null;
}

export { FALLBACK_ABOUT };