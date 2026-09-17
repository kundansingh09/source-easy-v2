// Run: node web/src/categories.test.js
// Plain ESM, no test framework - these are pure functions and the assertions
// are the point, not the harness.

import assert from "node:assert";
import { queryTokens, splitCategories, visibleCategories, allCategories } from "./categories.js";

const TREE = [
  {
    l1_id: 10,
    l1_name: "Front-End Equipment",
    children: [
      { id: 101, name: "Lithography" },
      { id: 102, name: "Etch" },
      { id: 103, name: "CMP" },
    ],
  },
  {
    l1_id: 20,
    l1_name: "Back-End / Packaging",
    children: [
      { id: 201, name: "Die Attach" },
      { id: 202, name: "Burn-in Test" },
    ],
  },
  { l1_id: 30, l1_name: "Facilities & UHP", children: [] },
];

// --- tokenisation ---------------------------------------------------------
assert.deepStrictEqual(queryTokens(""), []);
assert.deepStrictEqual(queryTokens("the and for"), [], "stopwords only -> no tokens");
// domain stopwords must not become tokens, or every tag would match
assert.ok(!queryTokens("semiconductor equipment supplier").includes("equipment"));
assert.ok(!queryTokens("semiconductor equipment supplier").includes("semiconductor"));
assert.deepStrictEqual(queryTokens("wafer defect inspection"), ["wafer", "defect", "inspection"]);
assert.deepStrictEqual(queryTokens("CMP  slurry!!"), ["cmp", "slurry"]);
console.log("queryTokens: OK");

// --- L2 selection wins over everything -----------------------------------
let r = splitCategories(TREE, { selectedL2: [102], selectedL1: [10], query: "lithography" });
assert.deepStrictEqual(r.hits.map((h) => h.label), ["Etch"],
  "explicit L2 selection must be the only hit, overriding L1 and query");
assert.strictEqual(r.total, 6, "nothing is dropped, only reclassified");
console.log("L2 selection overrides L1 + query: OK");

// --- L1 selection marks that branch's children ---------------------------
r = splitCategories(TREE, { selectedL1: [20] });
assert.deepStrictEqual(r.hits.map((h) => h.label).sort(), ["Burn-in Test", "Die Attach"]);
assert.ok(r.rest.some((e) => e.label === "Lithography"), "other branches become 'rest', not gone");
console.log("L1 selection marks its children: OK");

// --- query matching when no category filter ------------------------------
r = splitCategories(TREE, { query: "lithography scanner" });
assert.deepStrictEqual(r.hits.map((h) => h.label), ["Lithography"]);
// stem matching: users type "litho", the tag says "Lithography"
r = splitCategories(TREE, { query: "litho tooling" });
assert.deepStrictEqual(r.hits.map((h) => h.label), ["Lithography"]);
// parent-name match surfaces the branch's children
r = splitCategories(TREE, { query: "packaging" });
assert.deepStrictEqual(r.hits.map((h) => h.label).sort(), ["Burn-in Test", "Die Attach"]);
console.log("query matching (incl. stems and parent names): OK");

// --- childless L1 branch is still representable ---------------------------
r = splitCategories(TREE, { query: "facilities" });
assert.deepStrictEqual(r.hits.map((h) => h.label), ["Facilities & UHP"],
  "a company tagged only at level 1 must not render as empty");
r = splitCategories(TREE, { selectedL1: [30] });
assert.deepStrictEqual(r.hits.map((h) => h.label), ["Facilities & UHP"]);
console.log("childless L1 branch handled: OK");

// --- no filter, no query -> nothing is a 'hit' ----------------------------
r = splitCategories(TREE, {});
assert.strictEqual(r.hits.length, 0);
assert.strictEqual(r.rest.length, 6);
console.log("browse with no query/filter -> no false hits: OK");

// --- de-duplication across parents ---------------------------------------
const DUPE = [
  { l1_id: 10, l1_name: "A", children: [{ id: 1, name: "Shared" }] },
  { l1_id: 20, l1_name: "A", children: [{ id: 1, name: "Shared" }] },
];
r = splitCategories(DUPE, {});
assert.strictEqual(r.total, 1, "same leaf under the same parent name shown once");
console.log("de-duplication: OK");

// --- visibleCategories capping -------------------------------------------
const BIG = [{
  l1_id: 1, l1_name: "Big",
  children: Array.from({ length: 50 }, (_, i) => ({ id: i, name: `Cat ${i}` })),
}];
let v = visibleCategories(BIG, {}, 6);
assert.strictEqual(v.shown.length, 3, "no hits -> show a small teaser, not 50");
assert.strictEqual(v.hiddenCount, 47);
assert.strictEqual(v.hasHits, false);

v = visibleCategories(BIG, { query: "Cat" }, 6);
assert.strictEqual(v.shown.length, 6, "many hits -> capped at cap");
assert.strictEqual(v.hiddenCount, 44);
assert.strictEqual(v.hasHits, true);
assert.strictEqual(v.shown.length + v.hiddenCount, v.total, "counts must reconcile");
console.log("visibleCategories capping: OK");

// --- expanded view keeps everything, hits first ---------------------------
const all = allCategories(TREE, { selectedL1: [20] });
assert.strictEqual(all.length, 6, "expanded view loses nothing");
assert.deepStrictEqual(all.slice(0, 2).map((e) => e.label).sort(),
  ["Burn-in Test", "Die Attach"], "hits sort first");
console.log("allCategories: OK");

// --- defensive: malformed payloads ---------------------------------------
assert.strictEqual(splitCategories(null, {}).total, 0);
assert.strictEqual(splitCategories(undefined, { query: "x" }).total, 0);
assert.strictEqual(splitCategories([{ l1_id: 1 }], {}).total, 1, "missing children key tolerated");
assert.strictEqual(visibleCategories([], {}).total, 0);
console.log("malformed payload tolerance: OK");

console.log("\nall category tests passed");
