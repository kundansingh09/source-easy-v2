// Decides which of a company's categories are worth showing on its card.
//
// The problem this fixes: a supplier can carry 50+ category tags (ASML's
// SEMICON Europa profile literally lists 50). Rendering all of them turns
// every result card into a wall of grey text, buries the one tag that
// explains why the result is here, and makes the list impossible to scan.
//
// The rule: show categories that are the REASON this result matched -
// either explicitly selected as a filter, or overlapping the search query -
// and hide the rest behind a "+N more" toggle. Nothing is deleted; it's a
// disclosure change, not a data change.
//
// Pure functions with no React imports so they can be unit-tested directly
// under node (see web/src/categories.test.js).

// Words that carry no discriminating signal in a category name. Kept small
// and domain-aware on purpose: "equipment", "systems" and "technology"
// appear in a large share of SEMI category names, so matching on them would
// mark nearly every tag a hit and defeat the whole point.
const STOPWORDS = new Set([
  "the", "and", "for", "with", "from", "that", "this", "our", "your", "are",
  "can", "has", "have", "who", "what", "which", "where", "need", "want",
  "find", "looking", "supplier", "suppliers", "vendor", "vendors", "company",
  "companies", "manufacturer", "manufacturers", "maker", "makers",
  "equipment", "systems", "system", "technology", "technologies",
  "solutions", "solution", "services", "service", "products", "product",
  "semiconductor", "semiconductors", "based", "near", "local", "domestic",
  "avoid", "best", "good", "top", "new", "used",
]);

export function queryTokens(query) {
  if (!query) return [];
  return [
    ...new Set(
      query
        .toLowerCase()
        .split(/[^a-z0-9]+/)
        .filter((t) => t.length >= 3 && !STOPWORDS.has(t))
    ),
  ];
}

function nameMatches(name, tokens) {
  if (!tokens.length) return false;
  const lower = (name || "").toLowerCase();
  // Substring rather than whole-word so "litho" hits "Lithography" and
  // "packag" hits "Packaging" - users type stems, and SEMI's category
  // names are noun phrases that rarely collide misleadingly.
  return tokens.some((t) => lower.includes(t));
}

/**
 * Flatten a company's cat_tree into a display list, marking which entries
 * are "hits" (the reason it matched) and which are merely carried.
 *
 * @param {Array}  catTree    payload cat_tree: [{l1_id, l1_name, children:[{id,name}]}]
 * @param {Object} opts
 * @param {number[]} opts.selectedL1
 * @param {number[]} opts.selectedL2
 * @param {string}   opts.query
 * @returns {{hits: Array, rest: Array, total: number}}
 *   hits/rest entries are {key, label, parent} - parent is the L1 name when
 *   the entry is a child, null when the entry is a top-level category.
 */
export function splitCategories(catTree, { selectedL1 = [], selectedL2 = [], query = "" } = {}) {
  const tree = Array.isArray(catTree) ? catTree : [];
  const l1Set = new Set(selectedL1.map(Number));
  const l2Set = new Set(selectedL2.map(Number));
  const tokens = queryTokens(query);

  const hits = [];
  const rest = [];

  for (const branch of tree) {
    const l1Id = Number(branch?.l1_id);
    const l1Name = branch?.l1_name || "";
    const children = Array.isArray(branch?.children) ? branch.children : [];

    // A branch with no children still needs to be representable, otherwise
    // companies tagged only at level 1 would show nothing at all.
    if (!children.length) {
      const entry = { key: `l1-${l1Id}`, label: l1Name, parent: null };
      const isHit =
        l1Set.has(l1Id) ||
        (!l1Set.size && !l2Set.size && nameMatches(l1Name, tokens));
      (isHit ? hits : rest).push(entry);
      continue;
    }

    for (const child of children) {
      const cId = Number(child?.id);
      const cName = child?.name || "";
      const entry = { key: `l2-${l1Id}-${cId}`, label: cName, parent: l1Name };

      let isHit;
      if (l2Set.size) {
        // An explicit L2 selection is the most specific thing the user
        // said; only those exact leaves count as the reason.
        isHit = l2Set.has(cId);
      } else if (l1Set.size) {
        // L1 selected: every child under a selected parent is in scope.
        isHit = l1Set.has(l1Id);
      } else {
        // No category filter - fall back to query overlap, checking the
        // parent name too so "front-end" surfaces its children.
        isHit = nameMatches(cName, tokens) || nameMatches(l1Name, tokens);
      }

      (isHit ? hits : rest).push(entry);
    }
  }

  // De-duplicate: the same leaf can legitimately appear under two parents.
  const seen = new Set();
  const dedupe = (list) =>
    list.filter((e) => {
      const k = `${e.parent}|${e.label}`;
      if (seen.has(k)) return false;
      seen.add(k);
      return true;
    });

  const outHits = dedupe(hits);
  const outRest = dedupe(rest);
  return { hits: outHits, rest: outRest, total: outHits.length + outRest.length };
}

/**
 * What to render by default. Returns at most `cap` entries, preferring hits.
 * When nothing matched (pure browse with no category filter) we still show a
 * couple of tags so the card isn't featureless - just not all 50.
 */
export function visibleCategories(catTree, opts = {}, cap = 6) {
  const { hits, rest, total } = splitCategories(catTree, opts);
  const shown = hits.length ? hits.slice(0, cap) : rest.slice(0, Math.min(cap, 3));
  const hiddenCount = total - shown.length;
  return { shown, hiddenCount, total, hasHits: hits.length > 0 };
}

/** Full flat list, hits first, for the expanded "+N more" state. */
export function allCategories(catTree, opts = {}) {
  const { hits, rest } = splitCategories(catTree, opts);
  return [...hits, ...rest];
}
