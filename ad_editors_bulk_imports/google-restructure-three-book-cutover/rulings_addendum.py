"""
rulings_addendum.py — Strategy rulings 5/8 + stopgap RSAs + budgets (files 17-20, 13b)

State-aware against the fresh export. Emits:
  17_generic_peril_rename_{date}.csv      Fire -> Peril (ruling 8)
  18_generic_keywords_addendum_{date}.csv Flood-Core enabled keys -> Peril
                                          + R3 one-word pause 'homeowner insurance' [Broad] (ruling 5)
  19_generic_flood_pause_{date}.csv       Flood-Core group paused (ruling 8)
  20_generic_stopgap_rsas_{date}.csv      Core's RSAs copied into Quote/State/Peril/Other,
                                          statuses preserved (stopgap ruling)
  13b_budgets_GATED_{date}.csv            GEN 1150 / BH 600 / CAFL 200 / NB 100 (A1 ruling)

Usage:
  python rulings_addendum.py --export <fresh_editor_export.csv> [--output-dir ./output_v2]
"""

import csv
import os
import argparse
from datetime import datetime

GEN = "HOME INSURANCE-GENERIC-HOME-INTENT-NB-SEARCH-ALL-USA-STE"
BH = "HOME INSURANCE_BRAND-HOME-INTENT_COMBINED_TROAS_SEARCH_ALLDEVICE_USA-STE"
CAFL = "HOME INSURANCE_FLAGSHIP_CAFL_TROAS_SEARCH_ALLDEVICE_USA-STE"
NB = "HOME INSURANCE_FLAGSHIP_NB_TROAS_SEARCH_ALLDEVICE_USA-STE"
L_MOVE, L_PAUSE, L_ADD = "restr-2026-09-move", "restr-2026-09-pause", "restr-2026-09-add"
BUDGETS = [(GEN, "1150.00"), (BH, "600.00"), (CAFL, "200.00"), (NB, "100.00")]



# ---- POSTED-NAME TRANSLATION (Editor import matches POSTED names) ----
# Local (pending-rename) name -> posted (live-account) name. Confirmed: TEXAS.
# TIER2RPL / CALIFORNIA presumed from section_e originals — CONFIRM IN WEB UI.
# NB Flagship intentionally unmapped: if it exists only locally (unposted
# creation), its local name IS its match key; if it exists posted under a
# different name, STOP and add it here.
POSTED_NAME_MAP = {
    "HOME INSURANCE-GENERIC-HOME-INTENT-NB-SEARCH-ALL-USA-STE":
        "HOME INSURANCE-TEXAS-NB-SEARCH-ALL-USA-STE",
    "HOME INSURANCE_BRAND-HOME-INTENT_COMBINED_TROAS_SEARCH_ALLDEVICE_USA-STE":
        "HOME INSURANCE_STATE-GROUP_TIER2RPL_COMBINED_TROAS_SEARCH_ALLDEVICE_USA-STE",
    "HOME INSURANCE_FLAGSHIP_CAFL_TROAS_SEARCH_ALLDEVICE_USA-STE":
        "HOME INSURANCE-CALIFORNIA-NB-SEARCH-ALL-USA-STE",
}

def to_posted(rows):
    """Translate Campaign / Campaign#Original values to posted names."""
    for r in rows:
        for col in ("Campaign", "Campaign#Original"):
            if col in r and r[col] in POSTED_NAME_MAP:
                r[col] = POSTED_NAME_MAP[r[col]]
    return rows

def wr(path, hdr, rows):
    with open(path, "w", encoding="utf-16-le", newline="") as f:
        w = csv.DictWriter(f, fieldnames=hdr, delimiter="\t")
        w.writeheader()
        w.writerows(to_posted(rows))
    print(f"  {os.path.basename(path)}: {len(rows)} rows")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--export", required=True)
    p.add_argument("--output-dir", default="./output_v2")
    a = p.parse_args()
    export = list(csv.DictReader(open(a.export, encoding="utf-16"), delimiter="\t"))
    ds = datetime.now().strftime("%Y-%m-%d")
    os.makedirs(a.output_dir, exist_ok=True)
    o = a.output_dir

    # 17 — Fire -> Peril
    assert any(r["Campaign"] == GEN and r.get("Ad Group") == "Home-Generic-Fire"
               for r in export), "GATE FAIL: Home-Generic-Fire not in export"
    wr(f"{o}/17_generic_peril_rename_{ds}.csv",
       ["Campaign", "Ad group#Original", "Ad group", "Labels"],
       [{"Campaign": GEN, "Ad group#Original": "Home-Generic-Fire",
         "Ad group": "Home-Generic-Peril", "Labels": L_MOVE}])

    # 18 — Flood keys -> Peril + R3 one-word pause
    KW = ["Campaign", "Ad group", "Keyword", "Keyword match type", "Keyword status", "Labels"]
    rows = []
    flood = [r for r in export if r["Campaign"] == GEN
             and r["Ad Group"] == "Home-Generic-Flood-Core"
             and r.get("Keyword", "").strip()
             and "Negative" not in r.get("Criterion Type", "")
             and r["Status"] == "Enabled"]
    assert flood, "GATE FAIL: no enabled Flood-Core keys found"
    for r in flood:
        rows.append({"Campaign": GEN, "Ad group": "Home-Generic-Peril",
                     "Keyword": r["Keyword"], "Keyword match type": r["Criterion Type"],
                     "Keyword status": "Enabled", "Labels": L_MOVE})
    rows.append({"Campaign": GEN, "Ad group": "Home-Generic-Home Insurance-Core",
                 "Keyword": "homeowner insurance", "Keyword match type": "Broad",
                 "Keyword status": "Paused", "Labels": L_PAUSE})
    wr(f"{o}/18_generic_keywords_addendum_{ds}.csv", KW, rows)

    # 19 — Flood-Core pause
    wr(f"{o}/19_generic_flood_pause_{ds}.csv",
       ["Campaign", "Ad group", "Ad group status", "Labels"],
       [{"Campaign": GEN, "Ad group": "Home-Generic-Flood-Core",
         "Ad group status": "Paused", "Labels": L_PAUSE}])

    # 20 — stopgap RSA copies
    RSA = (["Campaign", "Ad group", "Ad type", "Status", "Labels", "Final URL",
            "Path 1", "Path 2"] + [f"Headline {i}" for i in range(1, 16)]
           + [f"Description {i}" for i in range(1, 5)])
    core = [r for r in export if r["Campaign"] == GEN
            and r["Ad Group"] == "Home-Generic-Home Insurance-Core"
            and r.get("Ad type") == "Responsive search ad"]
    assert core, "GATE FAIL: no RSAs in Home-Generic-Home Insurance-Core"

    def clean(v):
        return "" if v.strip() in ("--", "-") else v

    rsa_rows = []
    for dest in ["Home-Generic-Quote", "Home-Generic-State",
                 "Home-Generic-Peril", "Home-Generic-Other"]:
        for r in core:
            row = {"Campaign": GEN, "Ad group": dest,
                   "Ad type": "Responsive search ad", "Status": r["Status"],
                   "Labels": (r["Labels"] + ";" + L_ADD).lstrip(";"),
                   "Final URL": r["Final URL"], "Path 1": r["Path 1"],
                   "Path 2": r["Path 2"]}
            for i in range(1, 16):
                row[f"Headline {i}"] = clean(r[f"Headline {i}"])
            for i in range(1, 5):
                row[f"Description {i}"] = clean(r[f"Description {i}"])
            rsa_rows.append(row)
    wr(f"{o}/20_generic_stopgap_rsas_{ds}.csv", RSA, rsa_rows)
    n_en = sum(1 for r in rsa_rows if r["Status"] == "Enabled")
    print(f"    ({len(core)} source RSAs x 4 groups; {n_en} enabled copies)")

    # 13b — budgets
    wr(f"{o}/13b_budgets_GATED_{ds}.csv",
       ["Campaign#Original", "Campaign", "Budget"],
       [{"Campaign#Original": c, "Campaign": c, "Budget": b} for c, b in BUDGETS])
    total = sum(float(b) for _, b in BUDGETS)
    flag = (200 + 100) / total * 100
    print(f"    (total ${total:.0f}/day, flagship share {flag:.2f}% vs ~14.7% cap)")


if __name__ == "__main__":
    main()
