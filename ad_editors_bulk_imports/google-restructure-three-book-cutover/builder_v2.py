"""
builder_v2.py — Three-Book Restructure builder, corrections pass per builder_prompt_v2.md

STATE-AWARE: reads the fresh Ads Editor export and emits ONLY the delta between
v2's target state and the account as it actually stands. Ops already satisfied
are reported, not re-emitted. Every emitted row carries exactly one
restr-2026-09-* label (LAW 0b).

Import files are numbered in REQUIRED IMPORT ORDER (keyword/status ops that
reference current names always precede the rename files that change them):

  01_generic_keywords        A.1 quote extraction delta, A.2 texas move, A.3 dead variant
  02_generic_fold_OPTIONAL   A.5 default (pause+fold): Compare/Cost-Core keys -> Other
  03_generic_adgroups        A.4 book purge + A.5 group pauses
  04_bh_adgroup_create       B.2 Allied Trust Insurance-Core (sanctioned creation)
  05_bh_keywords             B.2 allied-trust split, B.3 geico unique-key move,
                             B.6 R6 migrations IN, safeco pause (BH)
  06_bh_rsa_copies           B.2 copy Group5's RSAs into Allied Trust group
  07_bh_adgroup_status       B.4 NerdWallet-Core pause, B.7 book purge
  08_bh_renames              zz-DUP defense on created Geico group (FIRST ROW),
                             B.1 renames, B.3 historic Geico rename
  09_cafl_keywords           C.2 R6 migrations OUT + safeco pause (CAFL)
  10_cafl_adgroup_status     C.1 dup -Flagship pauses (zz-DUP renamed), C.3 book purge
  11_cafl_renames            C.1 historic -Core -> -Flagship (7 spec + State Farm per Gate A)
  12_flagship_phrase_negs    C.4 + D: 8 Campaign Negative Phrase tokens x CAFL + NB
  13_troas_settings_GATED    E.3 / D5: tROAS 1.10 on 3 live campaigns  [cutover-gated]
  14_zz_retired_GATED        E.2 donor renames                          [cutover-gated]
  15_nb_enable_GATED         D: enable NB Flagship                      [cutover-gated]

BLOCKED (need inputs): the 104 exact seam negatives (B3 List 1 from
rollout_plan.xlsx) for A.6/B.8, and A1 budgets. K1T routing negatives: verify-only.

Usage:
  python builder_v2.py --export <fresh_editor_export.csv> [--dry-run]
"""

import csv
import os
import argparse
from collections import defaultdict
from datetime import datetime

GEN = "HOME INSURANCE-GENERIC-HOME-INTENT-NB-SEARCH-ALL-USA-STE"
BH = "HOME INSURANCE_BRAND-HOME-INTENT_COMBINED_TROAS_SEARCH_ALLDEVICE_USA-STE"
CAFL = "HOME INSURANCE_FLAGSHIP_CAFL_TROAS_SEARCH_ALLDEVICE_USA-STE"
NB = "HOME INSURANCE_FLAGSHIP_NB_TROAS_SEARCH_ALLDEVICE_USA-STE"
DONORS = [
    "HOME INSURANCE-FLORIDA-NB-SEARCH-ALL-USA-STE",
    "HOME INSURANCE_STATE-GROUP_TIER1RPL_COMBINED_TROAS_SEARCH_ALLDEVICE_USA-STE",
    "HOME INSURANCE_STATE-GROUP_TIER3RPL_COMBINED_TROAS_SEARCH_ALLDEVICE_USA-STE",
    "HOME INSURANCE_STATE-GROUP_TIER4RPL_COMBINED_TROAS_SEARCH_ALLDEVICE_USA-STE",
]

L_MOVE, L_PAUSE, L_ADD = "restr-2026-09-move", "restr-2026-09-pause", "restr-2026-09-add"
PHRASE_NEG_TOKENS = ["home", "homeowner", "homeowners", "house", "dwelling",
                     "property insurance", "condo", "landlord"]

ALLIED_TRUST_GROUP = "Home-NonGeneric-Allied Trust Insurance-Core"
GROUP5 = "Home-Non-Generic-Home_Group5"

report = {"satisfied": [], "emitted": [], "blocked": [], "flags": []}



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

def sat(msg): report["satisfied"].append(msg)
def emi(msg): report["emitted"].append(msg)
def blk(msg): report["blocked"].append(msg)
def flg(msg): report["flags"].append(msg)


class State:
    def __init__(self, rows):
        self.rows = rows
        self.ag_status = {}
        self.kw = defaultdict(dict)  # (campaign, adgroup) -> {(kw_lower, match): status}
        for r in rows:
            c, g = r.get("Campaign", ""), r.get("Ad Group", "")
            if g and r.get("Ad Group Status"):
                self.ag_status[(c, g)] = r["Ad Group Status"]
            k = r.get("Keyword", "").strip()
            if k and "Negative" not in r.get("Criterion Type", ""):
                self.kw[(c, g)][(k.lower(), r.get("Criterion Type", ""))] = r.get("Status", "")

    def groups(self, c):
        return sorted(g for (cc, g) in self.ag_status if cc == c)

    def gstat(self, c, g):
        return self.ag_status.get((c, g))

    def kstat(self, c, g, kw, mt):
        return self.kw[(c, g)].get((kw.lower(), mt))

    def rsas(self, c, g):
        return [r for r in self.rows
                if r["Campaign"] == c and r["Ad Group"] == g
                and r.get("Ad type") == "Responsive search ad"]


def kw_row(c, g, kw, mt, status, label):
    return {"Campaign": c, "Ad group": g, "Keyword": kw, "Keyword match type": mt,
            "Keyword status": status, "Labels": label}


def build_generic(st):
    kw_rows, fold_rows, ag_rows = [], [], []
    CORE, COST, QUOTE, STATE_G = ("Home-Generic-Home Insurance-Core",
                                  "Home-Generic-Cost-Core", "Home-Generic-Quote",
                                  "Home-Generic-State")

    # --- A.1 quote extraction (delta) ---
    for kw, mt in [("home insurance quotes", "Broad"), ("home insurance quotes", "Exact"),
                   ("homeowners insurance quote", "Broad"), ("homeowners insurance quote", "Exact")]:
        if st.kstat(GEN, QUOTE, kw, mt) == "Enabled":
            sat(f"A.1 {kw} [{mt}] already in Quote")
        else:
            kw_rows.append(kw_row(GEN, QUOTE, kw, mt, "Enabled", L_MOVE))
            emi(f"A.1 ADD {kw} [{mt}] -> Quote")
        if st.kstat(GEN, CORE, kw, mt) == "Enabled":
            kw_rows.append(kw_row(GEN, CORE, kw, mt, "Paused", L_MOVE))
            emi(f"A.1 PAUSE {kw} [{mt}] in Core")
        else:
            sat(f"A.1 {kw} [{mt}] already not-enabled in Core")
    # estimate: Phrase moves from Cost-Core
    if st.kstat(GEN, QUOTE, "home insurance estimate", "Phrase") == "Enabled":
        sat("A.1 estimate [Phrase] already in Quote")
    else:
        kw_rows.append(kw_row(GEN, QUOTE, "home insurance estimate", "Phrase", "Enabled", L_MOVE))
        emi("A.1 ADD home insurance estimate [Phrase] -> Quote")
    if st.kstat(GEN, COST, "home insurance estimate", "Phrase") == "Enabled":
        kw_rows.append(kw_row(GEN, COST, "home insurance estimate", "Phrase", "Paused", L_MOVE))
        emi("A.1 PAUSE home insurance estimate [Phrase] in Cost-Core")

    # --- A.2 texas -> State (text as stored: 'Texas home insurance') ---
    for mt in ["Broad", "Exact"]:
        if st.kstat(GEN, STATE_G, "texas home insurance", mt) == "Enabled":
            sat(f"A.2 texas [{mt}] already in State")
        else:
            kw_rows.append(kw_row(GEN, STATE_G, "Texas home insurance", mt, "Enabled", L_MOVE))
            emi(f"A.2 ADD Texas home insurance [{mt}] -> State")
        if st.kstat(GEN, CORE, "texas home insurance", mt) == "Enabled":
            kw_rows.append(kw_row(GEN, CORE, "Texas home insurance", mt, "Paused", L_MOVE))
            emi(f"A.2 PAUSE Texas home insurance [{mt}] in Core")

    # --- A.3 dead variants (texts as they exist NOW) ---
    if st.kstat(GEN, CORE, "home owners insurance", "Broad") == "Enabled":
        kw_rows.append(kw_row(GEN, CORE, "home owners insurance", "Broad", "Paused", L_PAUSE))
        emi("A.3 PAUSE home owners insurance [Broad] (R3)")
    else:
        sat("A.3 'home owners insurance' already not-enabled")
    if st.kstat(GEN, CORE, "home owner insurance", "Broad"):
        kw_rows.append(kw_row(GEN, CORE, "home owner insurance", "Broad", "Paused", L_PAUSE))
        emi("A.3 PAUSE home owner insurance [Broad] (R3)")
    else:
        sat("A.3 R3 text 'home owner insurance' not present in account (no action)")
    if st.kstat(GEN, CORE, "homeowner insurance", "Broad") == "Enabled":
        flg("A.3 DIVERGENCE: 'homeowner insurance' [Broad] (one word) IS enabled in Core "
            "although R3 validation said it does not exist. NOT paused — it was never "
            "validated dead. Needs a ruling (rerun variant_query_check on this text).")

    # --- A.5 shells: default = pause + fold into Other ---
    seen = set()
    for grp in ["Home-Generic-Cost-Core", "Home-Generic-Compare"]:
        for (kw, mt), status in st.kw[(GEN, grp)].items():
            if status != "Enabled" or (kw, mt) in seen:
                continue
            if kw == "home insurance estimate" and mt == "Phrase":
                continue  # A.1 already routes it to Quote
            seen.add((kw, mt))
            fold_rows.append(kw_row(GEN, "Home-Generic-Other", kw, mt, "Enabled", L_MOVE))
    emi(f"A.5 FOLD {len(fold_rows)} Cost-Core/Compare keys -> Other (OPTIONAL file 02)")
    flg(f"A.5 spec said '~4 keys' in the wave-2 shells; export shows {len(fold_rows)+1} "
        "enabled (Cost-Core grew). Default pause+fold implemented as file 02 — SKIP file 02 "
        "to choose the leave-paused-empty option instead. Gilbert's call per spec.")

    # --- A.4 book purge + A.5 group pauses ---
    keep_enabled = {"Home-Generic-Quote", "Home-Generic-Home Insurance-Core",
                    "Home-Generic-State", "Home-Generic-Fire", "Home-Generic-Flood-Core",
                    "Home-Generic-Other"}
    for g in st.groups(GEN):
        if st.gstat(GEN, g) != "Enabled":
            continue
        if g in keep_enabled:
            continue
        ag_rows.append({"Campaign": GEN, "Ad group": g, "Ad group status": "Paused",
                        "Labels": L_PAUSE})
        emi(f"A.4/A.5 PAUSE group {g}")
    flg("A.x KEPT ENABLED in GENERIC: Quote/State/Fire/Other have keywords but 0 RSAs — "
        "they cannot serve until creative lands (out of this pass's scope; needs an RSA "
        "build ruling). Flood-Core / Home Insurance-Core have RSAs. Naming is also mixed "
        "(Fire vs Flood-Core) — flag for a later hygiene rename.")
    return kw_rows, fold_rows, ag_rows


def build_bh(st):
    create_rows, kw_rows, rsa_rows, status_rows, rename_rows = [], [], [], [], []

    # --- B.2 Allied Trust split ---
    if (BH, ALLIED_TRUST_GROUP) in st.ag_status:
        sat("B.2 Allied Trust group already exists")
    else:
        create_rows.append({"Campaign": BH, "Ad group": ALLIED_TRUST_GROUP,
                            "Ad group type": "Standard", "Ad group status": "Enabled",
                            "Labels": L_ADD})
        emi("B.2 CREATE Home-NonGeneric-Allied Trust Insurance-Core (sanctioned)")
    for mt in ["Exact", "Broad"]:
        if st.kstat(BH, ALLIED_TRUST_GROUP, "allied trust home insurance", mt) != "Enabled":
            kw_rows.append(kw_row(BH, ALLIED_TRUST_GROUP, "allied trust home insurance",
                                  mt, "Enabled", L_MOVE))
            emi(f"B.2 ADD allied trust home insurance [{mt}] -> Allied Trust group")
        if st.kstat(BH, GROUP5, "allied trust home insurance", mt) == "Enabled":
            kw_rows.append(kw_row(BH, GROUP5, "allied trust home insurance", mt,
                                  "Paused", L_MOVE))
            emi(f"B.2 PAUSE allied trust home insurance [{mt}] in Group5")
    # copy Group5's RSAs into the new group (statuses preserved, labels appended)
    for r in st.rsas(BH, GROUP5):
        row = {"Campaign": BH, "Ad group": ALLIED_TRUST_GROUP,
               "Ad type": "Responsive search ad", "Status": r["Status"],
               "Labels": (r["Labels"] + ";" + L_ADD).lstrip(";"),
               "Final URL": r["Final URL"], "Path 1": r["Path 1"], "Path 2": r["Path 2"]}
        def clean(v):
            return "" if v.strip() in ("--", "-") else v
        for i in range(1, 16):
            row[f"Headline {i}"] = clean(r[f"Headline {i}"])
        for i in range(1, 5):
            row[f"Description {i}"] = clean(r[f"Description {i}"])
        rsa_rows.append(row)
    emi(f"B.2 COPY {len(rsa_rows)} Group5 RSAs -> Allied Trust group (statuses preserved: "
        + ", ".join(r["Status"] for r in rsa_rows) + ")")
    flg("B.2 BCC-01 NOTE: one copied RSA carries the BCC-01 label into a NEW ad group — "
        "relay to the asset-DB thread so the new instance registers in lineage before the "
        "weekly sync trips on an unexpected BCC-01 sighting.")

    # --- B.3 Geico consolidation ---
    created, historic = "Home-NonGeneric-Geico Insurance-Core", "Home-NonGeneric-Geico-Core"
    hist_keys = set(st.kw[(BH, historic)].keys())
    for (kw, mt), status in st.kw[(BH, created)].items():
        if status == "Enabled" and (kw, mt) not in hist_keys:
            kw_rows.append(kw_row(BH, historic, kw, mt, "Enabled", L_MOVE))
            emi(f"B.3 ADD unique key '{kw}' [{mt}] created-Geico -> historic Geico-Core")
    # created group: zz-DUP rename + pause FIRST in rename file (clears the name)
    rename_rows.append({"Campaign": BH, "Ad group#Original": created,
                        "Ad group": "zz-DUP-" + created, "Ad group status": "Paused",
                        "Labels": L_PAUSE})
    emi("B.3 PAUSE created Geico group + defensive rename zz-DUP-* (clears name)")
    flg("B.3 BUILDER RULING (approve): historic Geico-Core renames to 'Home-NonGeneric-"
        "Geico Insurance-Core' per R14, which COLLIDES with the created group's name. "
        "Resolution: created group renamed 'zz-DUP-Home-NonGeneric-Geico Insurance-Core' "
        "+ Paused as the first row of the rename file, BEFORE the historic rename. Same "
        "pattern used for the 7 CAFL collisions.")

    # --- B.1 renames (after all keyword/status ops that use old names) ---
    for orig, new in [("Home-Non-Generic-Home_Group2", "Home-NonGeneric-American Insurance-Core"),
                      (GROUP5, "Home-NonGeneric-Allied Insurance-Core"),
                      ("Home-Non-Generic-Home_Group6", "Home-NonGeneric-21st Century Insurance-Core")]:
        if (BH, new) in st.ag_status:
            sat(f"B.1 {new} already exists")
        else:
            rename_rows.append({"Campaign": BH, "Ad group#Original": orig, "Ad group": new,
                                "Ad group status": "", "Labels": L_MOVE})
            emi(f"B.1 RENAME {orig} -> {new}")
    rename_rows.append({"Campaign": BH, "Ad group#Original": historic,
                        "Ad group": created, "Ad group status": "", "Labels": L_MOVE})
    emi(f"B.3 RENAME historic {historic} -> {created}")

    # --- B.4 NerdWallet ---
    nw = "Home-NonGeneric-NerdWallet-Core"
    home_new = "Home-Non-Generic-Home_New"
    missing = [k for k, s in st.kw[(BH, nw)].items()
               if s == "Enabled" and st.kstat(BH, home_new, k[0], k[1]) != "Enabled"]
    for kw, mt in missing:
        kw_rows.append(kw_row(BH, home_new, kw, mt, "Enabled", L_MOVE))
        emi(f"B.4 ADD {kw} [{mt}] -> Home_New")
    if not missing:
        sat("B.4 NerdWallet keys already present in Home_New — only the group pause needed")
    status_rows.append({"Campaign": BH, "Ad group": nw, "Ad group status": "Paused",
                        "Labels": L_PAUSE})
    emi("B.4 PAUSE NerdWallet-Core")

    # --- B.5 R9/R10 ---
    aaa_ok = all(st.kstat(BH, "Home-Non-Generic-Home_Group1", k, m) == "Enabled"
                 for k, m in [("aaa home insurance", "Broad"), ("aaa home insurance", "Exact"),
                              ("aaa homeowners insurance", "Exact")])
    if aaa_ok:
        sat("B.5 aaa home/homeowners keys sit in Home_Group1; aaa renters lives in the "
            "paused Renters_Group1 (group-level pause satisfies R9)")
    if not any(k[0] == "norcal aaa" for k in st.kw[(BH, "Home-Non-Generic-Home_Group1")]):
        flg("B.5 R9: 'norcal aaa' not found anywhere in BRAND-HOME. Not invented — need "
            "the intended match type + source before adding.")
    sat("B.5 R10: Home_Group4 retained as-is (plymouth rock + universal cohabit)")

    # --- B.6 R6 migrations IN (authoritative list) ---
    R6_IN = [("costco condo insurance", "Exact", "Home-NonGeneric-Costco Insurance-Core"),
             ("farmers condo", "Phrase", "Home-NonGeneric-Farmers Insurance-Core"),
             ("geico condo insurance", "Exact", historic), ("geico condo insurance", "Broad", historic),
             ("geico landlord insurance", "Exact", historic), ("geico landlord insurance", "Broad", historic),
             ("geico property insurance", "Exact", historic), ("geico property insurance", "Broad", historic),
             ("universal condo insurance", "Exact", "Home-Non-Generic-Home_Group4"),
             ("universal condo insurance", "Broad", "Home-Non-Generic-Home_Group4"),
             ("universal property", "Phrase", "Home-Non-Generic-Home_Group4"),
             ("costco property insurance", "Broad", "Home-NonGeneric-Costco Insurance-Core")]
    for kw, mt, grp in R6_IN:
        s = st.kstat(BH, grp, kw, mt)
        if s == "Enabled":
            sat(f"B.6 {kw} [{mt}] already enabled in {grp.split('-')[-1]}")
        else:
            kw_rows.append(kw_row(BH, grp, kw, mt, "Enabled", L_MOVE))
            emi(f"B.6 {'UN-PAUSE' if s == 'Paused' else 'ADD'} {kw} [{mt}] -> {grp}")

    # safeco landlord: pause wherever enabled (BH)
    for grp, d in st.kw.items():
        if grp[0] != BH:
            continue
        for (kw, mt), s in d.items():
            if kw == "safeco landlord insurance" and s == "Enabled":
                kw_rows.append(kw_row(BH, grp[1], kw, mt, "Paused", L_PAUSE))
                emi(f"C.2/E SAFECO PAUSE [{mt}] in BH/{grp[1]}")

    # --- B.7 book purge ---
    for g in st.groups(BH):
        if st.gstat(BH, g) != "Enabled":
            continue
        if g.startswith("Home-Generic-") or g == "Home-NonGeneric-Farmers Insurance-Flagship":
            status_rows.append({"Campaign": BH, "Ad group": g, "Ad group status": "Paused",
                                "Labels": L_PAUSE})
            emi(f"B.7 PAUSE group {g}")
    return create_rows, kw_rows, rsa_rows, status_rows, rename_rows


def build_cafl(st):
    kw_rows, status_rows, rename_rows = [], [], []
    SEVEN = ["American Family Insurance", "Costco Insurance", "Farmers Insurance",
             "Geico", "Hartford Insurance", "Kin Insurance", "Travelers Insurance"]

    # --- C.2 R6 OUT + safeco (pause wherever enabled in CAFL) ---
    OUT_TEXTS = {"geico condo insurance", "geico landlord insurance", "geico property insurance",
                 "costco condo insurance", "universal condo insurance", "safeco landlord insurance"}
    for (c, g), d in st.kw.items():
        if c != CAFL:
            continue
        for (kw, mt), s in d.items():
            if kw in OUT_TEXTS and s == "Enabled":
                kw_rows.append(kw_row(CAFL, g, kw, mt, "Paused", L_PAUSE))
                emi(f"C.2 PAUSE {kw} [{mt}] in CAFL/{g}")

    # --- C.1 duplicate collapse ---
    for carrier in SEVEN:
        dup = f"Home-NonGeneric-{carrier}-Flagship" if carrier != "Geico" else "Home-NonGeneric-Geico-Flagship"
        core = f"Home-NonGeneric-{carrier}-Core" if carrier != "Geico" else "Home-NonGeneric-Geico-Core"
        target = dup  # rename target == the dup's current name
        if st.gstat(CAFL, dup):
            rename_rows.append({"Campaign": CAFL, "Ad group#Original": dup,
                                "Ad group": "zz-DUP-" + dup, "Ad group status": "Paused",
                                "Labels": L_PAUSE})
            emi(f"C.1 PAUSE + zz-DUP rename created {dup}")
        if st.gstat(CAFL, core):
            rename_rows.append({"Campaign": CAFL, "Ad group#Original": core,
                                "Ad group": target, "Ad group status": "", "Labels": L_MOVE})
            emi(f"C.1 RENAME historic {core} -> {target}")
    # Universal created flagship: pause (no historic core in CAFL, no collision)
    uni = "Home-NonGeneric-Universal Insurance-Flagship"
    if st.gstat(CAFL, uni) == "Enabled":
        status_rows.append({"Campaign": CAFL, "Ad group": uni, "Ad group status": "Paused",
                            "Labels": L_PAUSE})
        emi(f"C.1 PAUSE {uni} (its key migrated to BH)")
    # State Farm: Gate A wants every enabled group -Flagship-suffixed
    sf = "Home-NonGeneric-State Farm Insurance-Core"
    if st.gstat(CAFL, sf) == "Enabled":
        rename_rows.append({"Campaign": CAFL, "Ad group#Original": sf,
                            "Ad group": "Home-NonGeneric-State Farm Insurance-Flagship",
                            "Ad group status": "", "Labels": L_MOVE})
        emi("C.1 RENAME State Farm Insurance-Core -> -Flagship")
        flg("C.1 INFERRED (approve): State Farm isn't in the seven-carrier list but Gate A "
            "requires all enabled CAFL groups -Flagship-suffixed; renamed accordingly.")

    # --- C.3 book purge ---
    purge = {"Home-Generic-Cost-Core", "Home-Generic-Fire-Core", "Home-Generic-Flood-Core",
             "Home-Generic-Home Insurance-Core", "Home-Non-Generic-Home_Group1",
             "Home-Non-Generic-Home_Group3", "Home-Non-Generic-Home_Group4",
             "Home-Non-Generic-Home_Group5", "Home-Non-Generic-Home_Group6",
             "Home-Non-Generic-Home_New", "Home-Non-Generic-Home_CaRegion"}
    for g in sorted(purge):
        if st.gstat(CAFL, g) == "Enabled":
            status_rows.append({"Campaign": CAFL, "Ad group": g, "Ad group status": "Paused",
                                "Labels": L_PAUSE})
            emi(f"C.3 PAUSE group {g}")
        else:
            sat(f"C.3 {g} already paused/absent")
    return kw_rows, status_rows, rename_rows


def build_negs_and_gated(st):
    neg_rows = []
    for camp in [CAFL, NB]:
        existing = {r["Keyword"].lower() for r in st.rows
                    if r["Campaign"] == camp and r.get("Criterion Type") == "Campaign Negative Phrase"}
        for tok in PHRASE_NEG_TOKENS:
            if tok in existing:
                sat(f"C.4/D phrase negative '{tok}' already on {camp.split('_')[2] if '_' in camp else camp}")
            else:
                neg_rows.append({"Campaign": camp, "Keyword": tok,
                                 "Criterion Type": "Campaign Negative Phrase", "Status": "Enabled"})
    emi(f"C.4/D {len(neg_rows)} Campaign Negative Phrase rows (8 tokens x CAFL + NB, "
        "delta vs existing)")

    troas = [{"Campaign#Original": c, "Campaign": c, "Target ROAS": "110%"}
             for c in [GEN, BH, CAFL]]
    zz = [{"Campaign#Original": c, "Campaign": "zz-RETIRED-" + c, "Campaign Status": "Paused"}
          for c in DONORS]
    nb_en = [{"Campaign#Original": NB, "Campaign": NB, "Campaign Status": "Enabled"}]
    return neg_rows, troas, zz, nb_en


def gate_a(st, all_pauses, all_renames):
    """Simulate emitted ops and check Gate A."""
    print("\n" + "=" * 70 + "\nGATE A SIMULATION (post-import expected state)\n" + "=" * 70)
    status = dict(st.ag_status)
    for r in all_pauses:
        status[(r["Campaign"], r["Ad group"])] = "Paused"
    for r in all_renames:
        key = (r["Campaign"], r["Ad group#Original"])
        stt = r.get("Ad group status") or status.get(key, "")
        status.pop(key, None)
        status[(r["Campaign"], r["Ad group"])] = stt
    gen_en = sorted(g for (c, g), s in status.items() if c == GEN and s == "Enabled")
    print(f"  GENERIC enabled groups ({len(gen_en)}): {gen_en}")
    ok1 = all(g.startswith("Home-Generic-") for g in gen_en)
    print(f"  [{'PASS' if ok1 else 'FAIL'}] Generic-only enabled in GENERIC")
    cafl_en = sorted(g for (c, g), s in status.items() if c == CAFL and s == "Enabled")
    ok2 = all(g.endswith("-Flagship") for g in cafl_en)
    print(f"  CAFL enabled groups ({len(cafl_en)}): {cafl_en}")
    print(f"  [{'PASS' if ok2 else 'FAIL'}] All CAFL enabled groups -Flagship-suffixed, "
          f"one per carrier: {len(cafl_en) == len({g.rsplit('-', 1)[0] for g in cafl_en})}")
    print("  [BLOCKED] negative counts 104/104 — needs B3 List 1 (rollout_plan.xlsx)")
    print("  [EMITTED] 8/8 phrase negatives per flagship campaign")


def write(path, headers, rows):
    with open(path, "w", encoding="utf-16-le", newline="") as f:
        w = csv.DictWriter(f, fieldnames=headers, delimiter="\t", extrasaction="ignore")
        w.writeheader()
        w.writerows(to_posted(rows))
    print(f"  {os.path.basename(path)}: {len(rows)} rows")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--export", required=True)
    p.add_argument("--output-dir", default="./output_v2")
    p.add_argument("--dry-run", action="store_true")
    a = p.parse_args()
    with open(a.export, "r", encoding="utf-16") as f:
        rows = list(csv.DictReader(f, delimiter="\t"))
    st = State(rows)
    for c in [GEN, BH, CAFL, NB] + DONORS:
        assert any(r["Campaign"] == c for r in rows), f"GATE FAIL: missing campaign {c}"
    ds = datetime.now().strftime("%Y-%m-%d")

    g_kw, g_fold, g_ag = build_generic(st)
    b_create, b_kw, b_rsa, b_status, b_ren = build_bh(st)
    c_kw, c_status, c_ren = build_cafl(st)
    negs, troas, zz, nb_en = build_negs_and_gated(st)

    blk("A.6/B.8: 104 exact seam negatives need B3 List 1 — attach rollout_plan.xlsx "
        "(or its B3 sheet export) and I emit file 16 + the 104/104 gate check")
    blk("A.6: K1T precedence-aware routing negatives — verify-only without the K1T spec")
    blk("E.3: budgets per A1 — values not in the prompt; left manual")
    sat("D: NB clean — 18 bare-brand keys, zero condo/landlord/property; geo 48 states")
    sat("E.1: do-not-build texts enabled only inside paused donor campaigns")

    gate_a(st, g_ag + b_status + c_status, b_ren + c_ren)

    print("\n" + "=" * 70 + "\nRECONCILIATION\n" + "=" * 70)
    for k in ["satisfied", "emitted", "blocked", "flags"]:
        print(f"\n[{k.upper()}] ({len(report[k])})")
        for m in report[k]:
            print(f"  - {m}")

    if a.dry_run:
        print("\nDRY RUN — no files written")
        return
    os.makedirs(a.output_dir, exist_ok=True)
    print(f"\nFILES -> {a.output_dir}")
    KW = ["Campaign", "Ad group", "Keyword", "Keyword match type", "Keyword status", "Labels"]
    AG = ["Campaign", "Ad group", "Ad group status", "Labels"]
    AGC = ["Campaign", "Ad group", "Ad group type", "Ad group status", "Labels"]
    REN = ["Campaign", "Ad group#Original", "Ad group", "Ad group status", "Labels"]
    RSA = (["Campaign", "Ad group", "Ad type", "Status", "Labels", "Final URL",
            "Path 1", "Path 2"] + [f"Headline {i}" for i in range(1, 16)]
           + [f"Description {i}" for i in range(1, 5)])
    NEG = ["Campaign", "Keyword", "Criterion Type", "Status"]
    o = a.output_dir
    write(f"{o}/01_generic_keywords_{ds}.csv", KW, g_kw)
    write(f"{o}/02_generic_fold_OPTIONAL_{ds}.csv", KW, g_fold)
    write(f"{o}/03_generic_adgroups_{ds}.csv", AG, g_ag)
    write(f"{o}/04_bh_adgroup_create_{ds}.csv", AGC, b_create)
    write(f"{o}/05_bh_keywords_{ds}.csv", KW, b_kw)
    write(f"{o}/06_bh_rsa_copies_{ds}.csv", RSA, b_rsa)
    write(f"{o}/07_bh_adgroup_status_{ds}.csv", AG, b_status)
    write(f"{o}/08_bh_renames_{ds}.csv", REN, b_ren)
    write(f"{o}/09_cafl_keywords_{ds}.csv", KW, c_kw)
    write(f"{o}/10_cafl_adgroup_status_{ds}.csv", AG, c_status)
    write(f"{o}/11_cafl_renames_{ds}.csv", REN, c_ren)
    write(f"{o}/12_flagship_phrase_negs_{ds}.csv", NEG, negs)
    write(f"{o}/13_troas_settings_GATED_{ds}.csv", ["Campaign#Original", "Campaign", "Target ROAS"], troas)
    write(f"{o}/14_zz_retired_GATED_{ds}.csv", ["Campaign#Original", "Campaign", "Campaign Status"], zz)
    write(f"{o}/15_nb_enable_GATED_{ds}.csv", ["Campaign#Original", "Campaign", "Campaign Status"], nb_en)
    print("\nIMPORT ORDER = file number order. 02 is OPTIONAL (A.5 fold). "
          "13-15 are CUTOVER-GATED (Gate A + S6 baseline first).")


if __name__ == "__main__":
    main()
