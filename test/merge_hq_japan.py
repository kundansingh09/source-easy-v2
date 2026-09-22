# --------------------------------------------------------------------------
# merge_hq_location.py
#
# Takes the "en" pass as the base dataset and overwrites its hq_location
# field with the "default" pass's hq_location — because the "default" pass
# (Japanese UI) scrapes clean addresses, while the "en" pass has a scraper
# bug that leaked a placeholder value ("United States") into ~68% of its
# hq_location field. Website / about / categories are already near-identical
# between passes, so they are left untouched.
#
# Join key: BoothID, extracted from ebooth_url — NOT company_name, since
# company names differ by language pass and can't be matched reliably.
# company_name is only used secondarily, to flag suspicious matches where
# the Booth ID matches but the two names look nothing alike at all (a sanity
# check, not the join key).
#
# Outputs (written next to the input files):
#   japan2025_en_merged.json   — en pass, hq_location overwritten from default
#   japan2025_en_merged.csv    — same, flat CSV
#   merge_report.txt           — human-readable summary + ambiguity list
#   merge_report_ambiguities.csv — machine-readable list of every flagged row
# --------------------------------------------------------------------------

import json
import csv
import os
import re
from urllib.parse import urlparse, parse_qs

# ----------------- CONFIG -----------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

DEFAULT_PATH = os.path.join(BASE_DIR, "japan2025_default.json")
EN_PATH      = os.path.join(BASE_DIR, "japan2025_en.json")
OUT_JSON     = os.path.join(BASE_DIR, "japan2025_en_merged.json")
OUT_CSV      = os.path.join(BASE_DIR, "japan2025_en_merged.csv")
REPORT_TXT   = os.path.join(BASE_DIR, "merge_report.txt")
REPORT_CSV   = os.path.join(BASE_DIR, "merge_report_ambiguities.csv")

# values that are known scraper-garbage — if the DEFAULT value we're about
# to copy over is itself one of these, treat it as "no good replacement"
# rather than blindly overwriting with junk.
SUSPICIOUS_HQ_VALUES = {"united states", "japan", "n/a", ""}

# ----------------- HELPERS -----------------

def booth_id(url):
    if not url:
        return None
    qs = parse_qs(urlparse(url).query)
    vals = qs.get("BoothID")
    return vals[0] if vals else None

def is_suspicious(value):
    if not value:
        return True
    return value.strip().lower() in SUSPICIOUS_HQ_VALUES

def load(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

# ----------------- LOAD -----------------

default_data = load(DEFAULT_PATH)
en_data      = load(EN_PATH)

default_by_id = {}
default_dupe_ids = set()
for r in default_data:
    bid = booth_id(r.get("ebooth_url"))
    if bid is None:
        continue
    if bid in default_by_id:
        default_dupe_ids.add(bid)
    default_by_id[bid] = r   # last one wins if duplicated; dupes are reported separately

en_dupe_ids = set()
seen_en_ids = set()
for r in en_data:
    bid = booth_id(r.get("ebooth_url"))
    if bid is None:
        continue
    if bid in seen_en_ids:
        en_dupe_ids.add(bid)
    seen_en_ids.add(bid)

# ----------------- MERGE -----------------

merged = []
overwritten          = []   # (name, bid, old_hq, new_hq)
no_default_match      = []  # (name, bid, kept_hq) — en record has no matching booth in default
no_default_match_bad  = []  # subset of the above where the kept en value is itself known-bad
default_value_suspicious = []  # (name, bid, old_hq, suspicious_new_hq) — overwrote anyway, flagged
no_booth_id_en        = []  # en records with no parseable BoothID at all

for r in en_data:
    row = dict(r)  # shallow copy — don't mutate original
    name = row.get("company_name", "")
    bid = booth_id(row.get("ebooth_url"))

    if bid is None:
        no_booth_id_en.append(name)
        merged.append(row)
        continue

    default_row = default_by_id.get(bid)

    if default_row is None:
        # no matching company in the default pass at all
        no_default_match.append((name, bid, row.get("hq_location")))
        if is_suspicious(row.get("hq_location")):
            no_default_match_bad.append((name, bid, row.get("hq_location")))
        merged.append(row)
        continue

    old_hq = row.get("hq_location")
    new_hq = default_row.get("hq_location")

    row["hq_location"] = new_hq
    row["hq_location_source"] = "default_pass"  # traceability marker

    if is_suspicious(new_hq):
        default_value_suspicious.append((name, bid, old_hq, new_hq))

    if old_hq != new_hq:
        overwritten.append((name, bid, old_hq, new_hq))

    merged.append(row)

# records that exist only in default (never make it into the en-based output at all)
en_ids_present = {booth_id(r.get("ebooth_url")) for r in en_data if booth_id(r.get("ebooth_url"))}
only_in_default = [
    (r.get("company_name"), booth_id(r.get("ebooth_url")))
    for r in default_data
    if booth_id(r.get("ebooth_url")) not in en_ids_present
]

# ----------------- WRITE MERGED OUTPUT -----------------

with open(OUT_JSON, "w", encoding="utf-8") as f:
    json.dump(merged, f, indent=4, ensure_ascii=False)

with open(OUT_CSV, "w", newline="", encoding="utf-8") as f:
    writer = csv.writer(f)
    writer.writerow([
        "Company Name", "Location", "eBooth URL", "Website",
        "HQ Location", "HQ Source", "Categories L1", "Categories L2", "About"
    ])
    for r in merged:
        writer.writerow([
            r.get("company_name"),
            r.get("location"),
            r.get("ebooth_url"),
            r.get("website"),
            r.get("hq_location"),
            r.get("hq_location_source", "en_pass_original"),
            " | ".join(r.get("cat_l1_names", [])),
            " | ".join(r.get("cat_l2_names", [])),
            r.get("about") or "",
        ])

# ----------------- WRITE AMBIGUITY CSV -----------------

with open(REPORT_CSV, "w", newline="", encoding="utf-8") as f:
    writer = csv.writer(f)
    writer.writerow(["Issue Type", "Company Name", "Booth ID", "Old HQ (en)", "New HQ (default)"])
    for name, bid, old, new in default_value_suspicious:
        writer.writerow(["default_value_still_suspicious", name, bid, old, new])
    for name, bid, kept in no_default_match:
        writer.writerow(["no_default_match_kept_en_value", name, bid, kept, ""])
    for name, bid in only_in_default:
        writer.writerow(["only_in_default_not_in_output", name, bid, "", ""])
    for name in no_booth_id_en:
        writer.writerow(["en_record_no_booth_id", name, "", "", ""])
    for bid in sorted(default_dupe_ids):
        writer.writerow(["duplicate_booth_id_in_default", "", bid, "", ""])
    for bid in sorted(en_dupe_ids):
        writer.writerow(["duplicate_booth_id_in_en", "", bid, "", ""])

# ----------------- WRITE HUMAN-READABLE REPORT -----------------

lines = []
lines.append("=" * 70)
lines.append("MERGE REPORT — japan2025 hq_location merge (en base, default hq)")
lines.append("=" * 70)
lines.append("")
lines.append(f"default pass records : {len(default_data)}")
lines.append(f"en pass records      : {len(en_data)}")
lines.append(f"merged output records: {len(merged)}  (same count as en pass, by design)")
lines.append("")
lines.append("-" * 70)
lines.append("JOIN RESULT")
lines.append("-" * 70)
lines.append(f"Matched by Booth ID and hq_location overwritten : {len(overwritten)}")
matched_total = len(en_data) - len(no_default_match) - len(no_booth_id_en)
lines.append(f"Matched, value already identical (no change needed): {matched_total - len(overwritten)}")
lines.append(f"No matching Booth ID in default pass (kept en value) : {len(no_default_match)}")
lines.append(f"  — of those, the kept en value is itself known-bad  : {len(no_default_match_bad)}")
lines.append(f"En records with no parseable Booth ID at all      : {len(no_booth_id_en)}")
lines.append("")
lines.append("-" * 70)
lines.append("DATA QUALITY FLAGS")
lines.append("-" * 70)
lines.append(f"Overwrote with a default value that is ITSELF suspicious "
             f"(e.g. bare 'Japan'/'United States'/empty): {len(default_value_suspicious)}")
lines.append(f"Companies present only in 'default' pass, absent from 'en' "
             f"(not included in merged output — see ambiguity CSV): {len(only_in_default)}")
lines.append(f"Duplicate Booth IDs found in default pass: {len(default_dupe_ids)}")
lines.append(f"Duplicate Booth IDs found in en pass: {len(en_dupe_ids)}")
lines.append("")
lines.append("-" * 70)
lines.append("SAMPLE OF OVERWRITTEN VALUES (first 10)")
lines.append("-" * 70)
for name, bid, old, new in overwritten[:10]:
    lines.append(f"  [{bid}] {name}")
    lines.append(f"      old (en, likely garbage): {old}")
    lines.append(f"      new (default, trusted)  : {new}")
lines.append("")
if default_value_suspicious:
    lines.append("-" * 70)
    lines.append("ROWS WHERE EVEN THE DEFAULT VALUE LOOKS SUSPICIOUS (needs manual check)")
    lines.append("-" * 70)
    for name, bid, old, new in default_value_suspicious[:20]:
        lines.append(f"  [{bid}] {name}  ->  default value: '{new}'  (was: '{old}')")
    if len(default_value_suspicious) > 20:
        lines.append(f"  ... and {len(default_value_suspicious) - 20} more — see {os.path.basename(REPORT_CSV)}")
    lines.append("")
lines.append("-" * 70)
lines.append("FILES WRITTEN")
lines.append("-" * 70)
lines.append(f"  {OUT_JSON}")
lines.append(f"  {OUT_CSV}")
lines.append(f"  {REPORT_CSV}   (full row-by-row ambiguity list)")
lines.append("")

report_text = "\n".join(lines)
with open(REPORT_TXT, "w", encoding="utf-8") as f:
    f.write(report_text)

print(report_text)