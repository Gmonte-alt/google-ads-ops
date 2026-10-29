"""
phase2_builder.py — Three-Book Restructure Phase 2 (builder items 4-7)

State-aware against the fresh posted-state export (2026-10-28). Emits the
Phase-2 import set with restr-2026-10-* labels. Decision-gated files are
suffixed _GATED and hold until Gilbert's CA/FL + BH-geo verdicts.

READY (import once strategy signs off on the staged export):
  p2_01_shared_lists_{d}.csv        BARE_BRAND_EXACT (104 B3 exacts) +
                                    HOME_ROUTING (8 phrase tokens) — create + members
  p2_02_list_attachments_{d}.csv    BARE_BRAND_EXACT -> GENERIC + TIER2(BH);
                                    HOME_ROUTING -> NB only (CA/FL gated on decision 1)
  p2_03_nb_geo_trim_{d}.csv         NB 48 -> 45 (remove MN/NM/AR by criterion ID)
  p2_04_bh_transition_adds_{d}.csv  BH-book keys enabled only in T1/T3/T4 -> TIER2
                                    same-named groups (structural diff vs this export)
  p2_05_bare_brand_prune_{d}.csv    pause enabled EXACT keys in GENERIC/TIER2 whose
                                    text is on B3 List 1 (self-block clearance —
                                    emitted only if violations exist; the list
                                    attachment is UNSAFE until these pause)

GATED (emitted, do not import until verdicts):
  p2_10_bh_geo_45st_GATED_{d}.csv   TIER2 -> 45-state national (= Generic footprint)
  p2_11_bh_geo_48st_GATED_{d}.csv   TIER2 -> 48-state national (MN/NM/AR kept)
  p2_12_tier_pauses_GATED_{d}.csv   pause T1/T3/T4 (at post, with the atomic set)
  p2_13_nb_enable_GATED_{d}.csv     enable NB (tROAS/budget = manual UI step, values TBD)

BLOCKED (needs shared-library-inclusive export): Progressive stray-row removals
(flood/water/fema/pronto/progress/everquote) + 'true' -> 'true insurance' —
this export carries list names/attachments but zero member keywords.

Usage:
  python phase2_builder.py --export <fresh_posted_export.csv>
                           --plan rollout_plan.xlsx [--output-dir .\\output_p2]
"""

import csv
import os
import argparse
from collections import defaultdict
from datetime import datetime

GEN = "HOME INSURANCE-GENERIC-HOME-INTENT-NB-SEARCH-ALL-USA-STE"
T1 = "HOME INSURANCE_STATE-GROUP_TIER1RPL_COMBINED_TROAS_SEARCH_ALLDEVICE_USA-STE"
T2 = "HOME INSURANCE_STATE-GROUP_TIER2RPL_COMBINED_TROAS_SEARCH_ALLDEVICE_USA-STE"
T3 = "HOME INSURANCE_STATE-GROUP_TIER3RPL_COMBINED_TROAS_SEARCH_ALLDEVICE_USA-STE"
T4 = "HOME INSURANCE_STATE-GROUP_TIER4RPL_COMBINED_TROAS_SEARCH_ALLDEVICE_USA-STE"
NB = "HOME INSURANCE_FLAGSHIP_NB_TROAS_SEARCH_ALLDEVICE_USA-STE"
CA = "HOME INSURANCE-CALIFORNIA-NB-SEARCH-ALL-USA-STE"
FL = "HOME INSURANCE-FLORIDA-NB-SEARCH-ALL-USA-STE"

L_ADD, L_PAUSE = "restr-2026-10-add", "restr-2026-10-pause"
LIST_BARE = "BARE_BRAND_EXACT"
LIST_ROUT = "HOME_ROUTING"
PHRASE_TOKENS = ["home", "homeowner", "homeowners", "house", "dwelling",
                 "property insurance", "condo", "landlord"]
TRIM_STATES = {"Minnesota, United States", "New Mexico, United States",
               "Arkansas, United States"}


def wr(path, hdr, rows_):
    with open(path, "w", encoding="utf-16-le", newline="") as f:
        w = csv.DictWriter(f, fieldnames=hdr, delimiter="\t")
        w.writeheader()
        w.writerows(rows_)
    print(f"  {os.path.basename(path)}: {len(rows_)} rows")


def load_b3(plan):
    from openpyxl import load_workbook
    wb = load_workbook(plan, data_only=True, read_only=True)
    terms, in1 = [], False
    for row in wb["B3_Seam_Negatives"].iter_rows(values_only=True):
        v = row[0]
        if v is None:
            continue
        s = str(v).strip()
        if s.startswith("LIST 1"):
            in1 = True
            continue
        if s.startswith("LIST 2"):
            in1 = False
        if in1 and s.startswith("[") and s.endswith("]"):
            terms.append(s[1:-1])
    assert len(terms) == 104, f"B3 List 1 = {len(terms)}, expected 104"
    return terms


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--export", required=True)
    p.add_argument("--plan", required=True)
    p.add_argument("--output-dir", default="./output_p2")
    a = p.parse_args()
    d = datetime.now().strftime("%Y-%m-%d")
    o = a.output_dir
    os.makedirs(o, exist_ok=True)

    rows = list(csv.DictReader(open(a.export, encoding="utf-16"), delimiter="\t"))
    b3 = load_b3(a.plan)
    b3l = {t.lower() for t in b3}

    # --- gates: campaigns present, names as posted ---
    camps = {r["Campaign"] for r in rows if r.get("Budget")}
    for c in [GEN, T1, T2, T3, T4, NB, CA, FL]:
        assert c in camps, f"GATE FAIL: campaign missing from export: {c}"

    ag_status = {(r["Campaign"], r["Ad Group"]): r["Ad Group Status"]
                 for r in rows if r.get("Ad Group") and r.get("Ad Group Status")}

    def enabled_keys(campaign):
        out = {}
        for r in rows:
            k = r.get("Keyword", "").strip()
            if (r["Campaign"] != campaign or not k
                    or "Negative" in r.get("Criterion Type", "")
                    or r.get("Shared set name")):
                continue
            if r.get("Status") != "Enabled":
                continue
            if ag_status.get((campaign, r["Ad Group"])) != "Enabled":
                continue
            out[(k.lower(), r["Criterion Type"])] = (r["Keyword"], r["Ad Group"])
        return out

    print("=" * 70)
    print(f"PHASE 2 BUILDER — basis: {os.path.basename(a.export)}")
    print("=" * 70)

    # === p2_01: shared lists (create + members) ===
    SS = ["Shared set name", "Shared set type", "Keyword", "Criterion Type", "Status"]
    ss_rows = [{"Shared set name": LIST_BARE,
                "Shared set type": "Shared negative keyword list",
                "Keyword": "", "Criterion Type": "", "Status": "Enabled"}]
    ss_rows += [{"Shared set name": LIST_BARE,
                 "Shared set type": "Shared negative keyword list",
                 "Keyword": t, "Criterion Type": "Negative Exact",
                 "Status": "Enabled"} for t in b3]
    ss_rows += [{"Shared set name": LIST_ROUT,
                 "Shared set type": "Shared negative keyword list",
                 "Keyword": "", "Criterion Type": "", "Status": "Enabled"}]
    ss_rows += [{"Shared set name": LIST_ROUT,
                 "Shared set type": "Shared negative keyword list",
                 "Keyword": t, "Criterion Type": "Negative Phrase",
                 "Status": "Enabled"} for t in PHRASE_TOKENS]
    existing_lists = {r["Shared set name"] for r in rows if r.get("Shared set name")}
    for nm in (LIST_BARE, LIST_ROUT):
        assert nm not in existing_lists, f"GATE FAIL: list already exists: {nm}"
    wr(f"{o}/p2_01_shared_lists_{d}.csv", SS, ss_rows)
    print(f"    ({LIST_BARE}: 104 Negative Exact | {LIST_ROUT}: 8 Negative Phrase)")

    # === p2_05: bare-brand self-block clearance (BEFORE attachment) ===
    KW = ["Campaign", "Ad group", "Keyword", "Keyword match type",
          "Keyword status", "Labels"]
    prune = []
    for camp, nm in [(GEN, "GENERIC"), (T2, "TIER2/BH")]:
        for (kl, m), (ktext, grp) in sorted(enabled_keys(camp).items()):
            if m == "Exact" and kl in b3l:
                prune.append({"Campaign": camp, "Ad group": grp, "Keyword": ktext,
                              "Keyword match type": "Exact",
                              "Keyword status": "Paused", "Labels": L_PAUSE})
    wr(f"{o}/p2_05_bare_brand_prune_{d}.csv", KW, prune)
    print(f"    SELF-BLOCK CLEARANCE: {len(prune)} enabled B3 exacts in GENERIC/TIER2"
          + (" — import BEFORE (or with) the attachment file" if prune
             else " — none found, attachment is clean"))
    for r in prune:
        print(f"      PAUSE [{r['Campaign'][:28]}...] {r['Ad group'][:40]} :: {r['Keyword']}")

    # === p2_02: list attachments ===
    AT = ["Campaign", "Shared set name", "Shared set type", "Status"]
    att = [{"Campaign": c, "Shared set name": LIST_BARE,
            "Shared set type": "Shared negative keyword list", "Status": "Enabled"}
           for c in (GEN, T2)]
    att += [{"Campaign": NB, "Shared set name": LIST_ROUT,
             "Shared set type": "Shared negative keyword list", "Status": "Enabled"}]
    wr(f"{o}/p2_02_list_attachments_{d}.csv", AT, att)
    print(f"    ({LIST_BARE} -> GENERIC + TIER2/BH; {LIST_ROUT} -> NB ONLY — "
          "CA/FL attachment GATED on decision 1: both still run generic+BH books)")

    # === p2_03: NB geo trim 48 -> 45 ===
    GEO = ["Campaign", "ID", "Location", "Status"]
    trim = []
    for r in rows:
        if (r["Campaign"] == NB and r.get("Location") in TRIM_STATES
                and r.get("Location type") == "State"
                and not r.get("Criterion Type")):
            trim.append({"Campaign": NB, "ID": r["ID"], "Location": r["Location"],
                         "Status": "Removed"})
    assert len(trim) == 3, f"GATE FAIL: expected 3 NB trim rows, found {len(trim)}"
    wr(f"{o}/p2_03_nb_geo_trim_{d}.csv", GEO, trim)
    print("    (NB 48 -> 45, matching Generic; removals by criterion ID)")

    # === p2_04: BH transition adds (structural diff, book-filtered) ===
    t2k = enabled_keys(T2)
    pool = {}
    for camp in (T1, T3, T4):
        for key, (ktext, grp) in enabled_keys(camp).items():
            if grp.lower().startswith("home-generic"):
                continue
            pool.setdefault(key, (ktext, grp))
    adds, skipped = [], []
    for (kl, m), (ktext, grp) in sorted(pool.items()):
        if (kl, m) in t2k:
            continue
        if kl in b3l:  # flagship book — its home is NB, not BH
            skipped.append((ktext, m, "FLAGSHIP(B3)"))
            continue
        assert (T2, grp) in ag_status, f"GATE FAIL: T2 lacks group {grp}"
        adds.append({"Campaign": T2, "Ad group": grp, "Keyword": ktext,
                     "Keyword match type": m, "Keyword status": "Enabled",
                     "Labels": L_ADD})
    wr(f"{o}/p2_04_bh_transition_adds_{d}.csv", KW, adds)
    print(f"    ({len(adds)} BH-book adds; {len(skipped)} flagship-book skipped: {skipped})")
    print("    L90 NOTE: all candidates are enabled in live spending campaigns; "
          "formal L90 spend check = strategy's compare-script audit on the staged export")

    # === GATED files ===
    print("\n-- GATED (hold for verdicts) --")
    # BH geo variants: add-state rows reusing NB's criterion IDs (universal geo ids)
    nb_states = {r["Location"]: r["ID"] for r in rows
                 if r["Campaign"] == NB and r.get("Location type") == "State"
                 and not r.get("Criterion Type")}
    t2_states = {r["Location"] for r in rows
                 if r["Campaign"] == T2 and r.get("Location type") == "State"
                 and not r.get("Criterion Type")}
    v45 = [{"Campaign": T2, "ID": i, "Location": s, "Status": "Enabled"}
           for s, i in sorted(nb_states.items())
           if s not in t2_states and s not in TRIM_STATES]
    v48 = [{"Campaign": T2, "ID": i, "Location": s, "Status": "Enabled"}
           for s, i in sorted(nb_states.items()) if s not in t2_states]
    wr(f"{o}/p2_10_bh_geo_45st_GATED_{d}.csv", GEO, v45)
    wr(f"{o}/p2_11_bh_geo_48st_GATED_{d}.csv", GEO, v48)
    print(f"    (TIER2 currently {len(t2_states)} states; variant A adds {len(v45)} "
          f"-> 45-state footprint, variant B adds {len(v48)} -> 48; import EXACTLY ONE)")
    CP = ["Campaign#Original", "Campaign", "Campaign Status"]
    wr(f"{o}/p2_12_tier_pauses_GATED_{d}.csv", CP,
       [{"Campaign#Original": c, "Campaign": c, "Campaign Status": "Paused"}
        for c in (T1, T3, T4)])
    wr(f"{o}/p2_13_nb_enable_GATED_{d}.csv", CP,
       [{"Campaign#Original": NB, "Campaign": NB, "Campaign Status": "Enabled"}])
    print("    (NB pre-enable manuals still owed: tCPA$40.30->tROAS + $1 budget -> "
          "real dollars; values = open Phase-2 ruling)")

    print("\nBLOCKED-INPUT: Progressive stray removals + 'true'->'true insurance' — "
          "need a shared-library-inclusive export (this one has 0 member keywords).")
    print("\nIMPORT ORDER: 01 -> 05 -> 02 -> 03 -> 04 -> [verdict-gated 10|11, 12, 13] "
          "-> stage -> export staged state -> strategy audit -> ONE atomic post.")


if __name__ == "__main__":
    main()
