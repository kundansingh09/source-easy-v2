#!/usr/bin/env python3
"""
Convert SEMICON India raw data and GPT-4o refined intelligence
into the canonical supplier JSON schema for database ingestion and Qdrant.
"""

import csv
import json
import os
import re
from typing import Dict, Any, List, Optional

SEMICON_CSV = "semicon.csv"
REFINED_JSON = "out_llm_full/refined_overviews.json"  # Adjust directory tag if needed
OUTPUT_JSON = "india_suppliers.json"
EXPO_LABEL = "India expo"


def clean_str(val: Any) -> str:
    if val is None:
        return ""
    return str(val).strip()


def parse_hq(address: str) -> tuple[str, str]:
    """
    Extracts hq_location and hq_country from an address string.
    Falls back gracefully if address is sparse or unstructured.
    """
    addr = clean_str(address)
    if not addr:
        return "India", "India"

    # Normalize whitespace
    addr_clean = re.sub(r"\s+", " ", addr).strip()
    parts = [p.strip() for p in addr_clean.split(",") if p.strip()]

    if len(parts) >= 2:
        country = parts[-1]
        # Strip postal / zip codes from country segment
        country = re.sub(r"\b\d{5,6}\b", "", country).strip()
        location = ", ".join(parts[-2:])
        return location, country if country else "India"
    elif len(parts) == 1:
        return parts[0], parts[0]

    return "India", "India"


def build_about_overview(llm_entry: Dict[str, Any], fallback_semicon: str, booth: str = "") -> str:
    """
    Combines the GPT-4o structured output fields and booth info into a single,
    rich text block for dense vector embeddings and UI display.
    """
    if not llm_entry or llm_entry.get("status") != "ok":
        # Fallback text if LLM extraction failed or was empty
        base = fallback_semicon if fallback_semicon else "Semiconductor industry participant."
        if booth:
            return f"Booth: {booth}. {base}"
        return base

    summary = clean_str(llm_entry.get("summary"))
    capabilities = llm_entry.get("capabilities") or []
    tech_expertise = llm_entry.get("technical_expertise") or []
    end_markets = llm_entry.get("end_markets") or []
    value_chain = clean_str(llm_entry.get("value_chain_position"))
    relevance = clean_str(llm_entry.get("semiconductor_relevance"))
    india_angle = clean_str(llm_entry.get("india_angle"))

    sections = []

    if summary:
        sections.append(summary)

    if booth:
        sections.append(f"Booth: {booth}.")

    if value_chain and value_chain.lower() != "other":
        sections.append(f"Value Chain Role: {value_chain}.")
        
    if relevance and relevance.lower() not in ("none", "low", ""):
        sections.append(f"Semiconductor Relevance: {relevance}.")

    if capabilities:
        caps_str = ", ".join(capabilities)
        sections.append(f"Key Capabilities: {caps_str}.")

    if tech_expertise:
        tech_str = ", ".join(tech_expertise)
        sections.append(f"Technical Expertise: {tech_str}.")
        
    if end_markets:
        markets_str = ", ".join(end_markets)
        sections.append(f"End Markets: {markets_str}.")

    if india_angle and india_angle.lower() not in ("null", "none", ""):
        sections.append(f"India Presence: {india_angle}")

    full_overview = " ".join(sections).strip()
    return full_overview if full_overview else fallback_semicon


def main():
    if not os.path.exists(SEMICON_CSV):
        raise FileNotFoundError(f"Missing {SEMICON_CSV}")
    if not os.path.exists(REFINED_JSON):
        raise FileNotFoundError(f"Missing {REFINED_JSON}")

    print(f"Loading {SEMICON_CSV}...")
    with open(SEMICON_CSV, "r", encoding="utf-8-sig") as f:
        semicon_rows = list(csv.DictReader(f))

    print(f"Loading {REFINED_JSON}...")
    with open(REFINED_JSON, "r", encoding="utf-8") as f:
        llm_data = json.load(f)

    # Positional lookup mapping with fallback by normalized name
    llm_by_idx = {entry.get("row_index"): entry for entry in llm_data if entry.get("row_index") is not None}
    llm_by_name = {
        clean_str(entry.get("name")).lower(): entry
        for entry in llm_data
        if clean_str(entry.get("name"))
    }

    final_records: List[Dict[str, Any]] = []

    for idx, row in enumerate(semicon_rows):
        company_name = clean_str(row.get("name") or row.get("Company Name"))
        if not company_name:
            continue

        # Match LLM enrichment record
        llm_entry = llm_by_idx.get(idx)
        if not llm_entry:
            llm_entry = llm_by_name.get(company_name.lower(), {})

        # URL and website normalization
        ebooth = clean_str(row.get("url") or row.get("ebooth_url") or "")
        website = clean_str(row.get("website") or "")
        if website and not website.startswith(("http://", "https://")):
            website = "https://" + website

        # Location parsing
        raw_address = row.get("address") or ""
        hq_location, hq_country = parse_hq(raw_address)
        booth = clean_str(row.get("booth"))

        # Overview synthesis
        raw_semicon = clean_str(
            row.get("Semicon CONTENT") or row.get("overview2") or row.get("overview3")
        )
        about_text = build_about_overview(llm_entry, raw_semicon, booth)

        # Build final canonical object with strictly compliant schema
        record = {
            "company_name": company_name,
            "ebooth_url": ebooth,
            "about": about_text,
            "hq_location": hq_location,
            "hq_country": hq_country,
            "website": website,
            "cat_l1_ids": [],
            "cat_l1_names": [],
            "cat_l2_ids": [],
            "cat_l2_names": [],
            "cat_tree": [],
            "locations": [EXPO_LABEL],
            "sources": [
                {
                    "location": EXPO_LABEL,
                    "ebooth_url": ebooth
                }
            ]
        }
        final_records.append(record)

    with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
        json.dump(final_records, f, indent=2, ensure_ascii=False)

    print(f"\nDone! Exported {len(final_records)} records to {OUTPUT_JSON}")


if __name__ == "__main__":
    main()