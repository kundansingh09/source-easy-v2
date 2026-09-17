"""Strip low-value sentences from overview/about text before it goes into
the dense embedding - specifically trade-show logistics filler ("located at
booth R7705", "we look forward to meeting you") that carries zero product or
capability signal but dilutes it in a short text_chunk.

Sentence-level, not word-level. Turning prose into a keyword list would be
out-of-distribution for a sentence-transformer (all-MiniLM-L6-v2 is trained
on natural sentence pairs, not keyword salad) and is more likely to blur the
embedding than sharpen it. Dropping whole low-value sentences keeps the
survivors grammatical, which is what the model actually wants.

No fixed target length. Nothing here tries to normalise overview length
across companies - a tight, all-signal overview should come out unchanged,
and only identified boilerplate gets removed from a padded one. Test this
as an explicit property below (`test_does_not_touch_pure_signal`), because
it's the thing most likely to regress silently if a pattern gets too broad
later.

This only changes what gets EMBEDDED. The raw `about` string stays in the
payload untouched for display - see the comment in _build_text_chunk.
"""

import re

FALLBACK_ABOUT = "Semiconductor technology and equipment supplier."

# Sentence-level patterns for trade-show logistics filler. Deliberately
# structural (booth/stand + a number, "look forward to meeting", "visit us
# at") rather than tied to one show's exact wording, since the same
# boilerplate shape repeats across Taiwan/Korea/China/Japan with the show
# name and year substituted, and Europe says "stand" where the others say
# "booth". Each pattern is meant to match on ~ the whole sentence it
# appears in, not a fragment inside an otherwise informative one.
_BOILERPLATE_PATTERNS = [
    # "At SEMICON TAIWAN 2025, X is located at booth R7705."
    r"\bis\s+located\s+at\s+(booth|stand)\b",
    r"\blocated\s+at\s+(booth|stand)\s*[:#]?\s*[a-z]?\d",
    # "We look forward to meeting you at SEMICON Taiwan 2025!"
    r"\bwe\s+look\s+forward\s+to\s+meeting\s+you\b",
    r"\blook\s+forward\s+to\s+(seeing|welcoming)\s+you\b",
    # "Visit us at booth X", "Come see us at stand Y", "Please visit our booth"
    r"\b(visit|come\s+(and\s+)?(visit|see))\s+us\s+at\s+(booth|stand)\b",
    r"\bplease\s+visit\s+our\s+(booth|stand)\b",
    r"\bsee\s+you\s+at\s+semicon\b",
    r"\bmeet\s+us\s+at\s+(booth|stand)\b",
    # Bare "Booth: R7705" / "Stand No. 4A-120" fragments some scrapes leave
    # as their own sentence after HTML->text conversion.
    r"^\s*(booth|stand)\s*(no\.?|number|#)?\s*[:\-]?\s*[a-z]?[\d\-]+\s*$",
]
_BOILERPLATE_RE = [re.compile(p, re.IGNORECASE) for p in _BOILERPLATE_PATTERNS]

# A sentence naming the show/year is only boilerplate in combination with a
# booth/CTA cue (above) - "SEMICON Taiwan" alone can appear in a genuinely
# informative sentence ("we introduced our latest scanner at SEMICON Taiwan
# 2024"), so show-name mentions are NOT matched on their own.

# If cleaning would remove EVERYTHING (a same-real-world edge case: an about
# field that is nothing but a booth announcement with no product content at
# all), fall back to the original text rather than embedding nothing - a
# company with zero embedded content becomes unfindable by anything except
# its name and category tags, which is a worse failure mode than leaving
# one noisy sentence in.
_MIN_KEEP_CHARS = 20


def split_sentences(text):
    """Lightweight sentence splitter for marketing prose. Not
    abbreviation-aware NLP-grade tokenisation - splits on ./!/? followed by
    whitespace and a capital letter, or end of string, which is good enough
    for exhibitor bios and errs toward under-splitting (merging two
    sentences) rather than over-splitting, since an under-split boilerplate
    sentence just fails to match a pattern (safe) while an over-split one
    risks a false match on a fragment (unsafe)."""
    text = (text or "").strip()
    if not text:
        return []
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9])", text)
    return [p.strip() for p in parts if p.strip()]


def is_boilerplate(sentence):
    return any(p.search(sentence) for p in _BOILERPLATE_RE)


def clean_overview(text):
    """Returns {"cleaned": str, "removed": [str], "kept": [str],
    "fallback_used": bool}.

    `cleaned` is what should be embedded. `removed` is exposed for
    auditing - see audit_corpus() below - so a bad pattern is easy to spot
    across real data before it silently degrades embeddings at scale.
    """
    if not text or text == FALLBACK_ABOUT:
        return {"cleaned": text or "", "removed": [], "kept": [text] if text else [],
               "fallback_used": False}

    sentences = split_sentences(text)
    kept, removed = [], []
    for s in sentences:
        (removed if is_boilerplate(s) else kept).append(s)

    cleaned = " ".join(kept)
    fallback_used = len(cleaned) < _MIN_KEEP_CHARS
    if fallback_used:
        # Stripped down to (near) nothing - trust the safety net over the
        # classifier for this one record rather than embed an empty string.
        cleaned = text

    return {"cleaned": cleaned, "removed": removed, "kept": kept, "fallback_used": fallback_used}


def audit_corpus(items, about_key="about", sample=30, show_unchanged=False):
    """Run clean_overview over a real dataset and print what got removed,
    so a pattern that's too broad (or too narrow) is visible before it's
    trusted. Meant to be run once against the real semi_suppliers.json
    after ingestion:

        python -c "
        import json
        from backend.text_clean import audit_corpus
        audit_corpus(json.load(open('data/semi_suppliers.json')))"
    """
    touched = []
    for item in items:
        about = item.get(about_key, "")
        if not about or about == FALLBACK_ABOUT:
            continue
        result = clean_overview(about)
        if result["removed"] or (show_unchanged and result["cleaned"] == about):
            touched.append((item.get("company_name", "?"), result))

    print(f"{len(touched)}/{len(items)} companies had at least one sentence flagged\n")
    for name, r in touched[:sample]:
        flag = " [FALLBACK: cleaning would have emptied this, kept original]" if r["fallback_used"] else ""
        print(f"--- {name}{flag} ---")
        for s in r["removed"]:
            print(f"  - removed: {s}")
        print()
    if len(touched) > sample:
        print(f"... and {len(touched) - sample} more (increase `sample` to see them)")