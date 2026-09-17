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

function emptyResult(summary) {
  return {
    summary, booth: null, valueChain: null, relevance: null,
    capabilities: [], technicalExpertise: [], endMarkets: [],
    indiaPresence: null, structured: false,
  };
}

export function parseOverview(about) {
  const text = (about || "").trim();
  if (!text) return emptyResult("");

  const matches = [...text.matchAll(LABEL_RE)];
  if (matches.length === 0) return emptyResult(text); // legacy format, incl. FALLBACK_ABOUT itself

  const result = {
    summary: text.slice(0, matches[0].index).trim(),
    booth: null, valueChain: null, relevance: null,
    capabilities: [], technicalExpertise: [], endMarkets: [],
    indiaPresence: null, structured: true,
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

  return result;
}

export function relevanceLevel(value) {
  const v = (value || "").toLowerCase();
  return v === "high" || v === "medium" || v === "low" ? v : null;
}

export { FALLBACK_ABOUT };