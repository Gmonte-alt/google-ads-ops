"""
gate_a_audit.py — Gate A audit on the ACTUAL posted account (Stage 2 of record)

Run against a FRESH Ads Editor export pulled AFTER Stage 1 posts.
  --stage pre-cutover   (default) validates the Stage 1 end state:
                        exact negatives expected ABSENT, NB still Paused
  --stage post-cutover  validates the Stage 3 end state:
                        104/104 exacts, NB Enabled, donors zz-RETIRED

Usage:
  python gate_a_audit.py --export <post_stage1_export.csv> [--stage pre-cutover]
"""

import csv
import argparse
from collections import defaultdict

GEN = "HOME INSURANCE-GENERIC-HOME-INTENT-NB-SEARCH-ALL-USA-STE"
BH = "HOME INSURANCE_BRAND-HOME-INTENT_COMBINED_TROAS_SEARCH_ALLDEVICE_USA-STE"
CAFL = "HOME INSURANCE_FLAGSHIP_CAFL_TROAS_SEARCH_ALLDEVICE_USA-STE"
NB = "HOME INSURANCE_FLAGSHIP_NB_TROAS_SEARCH_ALLDEVICE_USA-STE"
DONORS = ["HOME INSURANCE-FLORIDA-NB-SEARCH-ALL-USA-STE",
          "HOME INSURANCE_STATE-GROUP_TIER1RPL_COMBINED_TROAS_SEARCH_ALLDEVICE_USA-STE",
          "HOME INSURANCE_STATE-GROUP_TIER3RPL_COMBINED_TROAS_SEARCH_ALLDEVICE_USA-STE",
          "HOME INSURANCE_STATE-GROUP_TIER4RPL_COMBINED_TROAS_SEARCH_ALLDEVICE_USA-STE"]
TOKENS = {"home", "homeowner", "homeowners", "house", "dwelling",
          "property insurance", "condo", "landlord"}
GEN_ENABLED = {"Home-Generic-Quote", "Home-Generic-Home Insurance-Core",
               "Home-Generic-State", "Home-Generic-Peril", "Home-Generic-Other"}
CAFL_ENABLED = {f"Home-NonGeneric-{c}-Flagship" for c in
                ["American Family Insurance", "Costco Insurance", "Farmers Insurance",
                 "Hartford Insurance", "Kin Insurance", "Travelers Insurance",
                 "State Farm Insurance"]} | {"Home-NonGeneric-Geico-Flagship"}
R6_IN = [("costco condo insurance", "Exact"), ("farmers condo", "Phrase"),
         ("geico condo insurance", "Exact"), ("geico condo insurance", "Broad"),
         ("geico landlord insurance", "Exact"), ("geico landlord insurance", "Broad"),
         ("geico property insurance", "Exact"), ("geico property insurance", "Broad"),
         ("universal condo insurance", "Exact"), ("universal condo insurance", "Broad"),
         ("universal property", "Phrase"), ("costco property insurance", "Broad")]
PRUNE_16A = ["american family", "costco insurance", "travelers", "the hartford",
             "kin insurance"]  # representative texts; count check is authoritative

results = []


def chk(section, name, ok, detail=""):
    results.append((section, name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--export", required=True)
    p.add_argument("--stage", choices=["pre-cutover", "post-cutover"],
                   default="pre-cutover")
    a = p.parse_args()
    rows = list(csv.DictReader(open(a.export, encoding="utf-16"), delimiter="\t"))
    post = a.stage == "post-cutover"

    ag = {}
    kw = defaultdict(dict)
    rsas = defaultdict(list)
    camp = {}
    cneg = defaultdict(lambda: defaultdict(set))
    for r in rows:
        c, g = r.get("Campaign", ""), r.get("Ad Group", "")
        if g and r.get("Ad Group Status"):
            ag[(c, g)] = r["Ad Group Status"]
        k = r.get("Keyword", "").strip()
        ct = r.get("Criterion Type", "")
        if k and ct.startswith("Campaign Negative"):
            cneg[c][ct.replace("Campaign Negative ", "")].add(k.lower())
        elif k and "Negative" not in ct:
            kw[(c, g)][(k.lower(), ct)] = r.get("Status", "")
        if r.get("Ad type") == "Responsive search ad":
            rsas[(c, g)].append(r)
        if r.get("Budget"):
            camp[c] = r

    def enabled_groups(c):
        return {g for (cc, g), s in ag.items() if cc == c and s == "Enabled"}

    def kstat(c, g, t, m):
        return kw[(c, g)].get((t.lower(), m))

    print("=" * 70)
    print(f"GATE A AUDIT — {a.stage.upper()} — {a.export}")
    print("=" * 70)

    print("\n-- CAMPAIGNS --")
    for c in [GEN, BH, CAFL]:
        chk("camp", f"{c.split('_')[0][:30]}... Enabled",
            camp.get(c, {}).get("Campaign Status") == "Enabled")
    nb_status = camp.get(NB, {}).get("Campaign Status")
    chk("camp", f"NB {'Enabled' if post else 'Paused (pre-cutover)'}",
        nb_status == ("Enabled" if post else "Paused"), f"is {nb_status}")
    nbr = camp.get(NB, {})
    chk("camp", "NB bid strategy tROAS 110% + real budget",
        nbr.get("Bid Strategy Type") == "Target ROAS" and nbr.get("Budget") not in ("", "1.00"),
        f"strat='{nbr.get('Bid Strategy Type')}' tROAS='{nbr.get('Target ROAS')}' "
        f"budget='{nbr.get('Budget')}'")
    if post:
        for d in DONORS:
            zz = "zz-RETIRED-" + d
            chk("camp", f"donor retired: {d[:35]}...", zz in camp and d not in camp)

    print("\n-- GENERIC --")
    en = enabled_groups(GEN)
    chk("gen", "enabled groups == Quote/Core/State/Peril/Other", en == GEN_ENABLED,
        f"actual: {sorted(en)}")
    noads = [g for g in en if not any(r["Status"] == "Enabled" for r in rsas[(GEN, g)])]
    chk("gen", "every enabled group has an enabled RSA", not noads, f"gaps: {noads}")
    chk("gen", "quote keys enabled in Quote",
        all(kstat(GEN, "Home-Generic-Quote", t, m) == "Enabled"
            for t, m in [("home insurance quotes", "Exact"), ("homeowners insurance quote", "Exact"),
                         ("home insurance estimate", "Phrase")]))
    chk("gen", "quote/texas sources paused in Core",
        all(kstat(GEN, "Home-Generic-Home Insurance-Core", t, m) == "Paused"
            for t, m in [("home insurance quotes", "Exact"), ("home insurance quotes", "Broad"),
                         ("homeowners insurance quote", "Exact"), ("homeowners insurance quote", "Broad"),
                         ("texas home insurance", "Exact"), ("texas home insurance", "Broad")]))
    chk("gen", "R3 pauses (home owners insurance + homeowner insurance)",
        kstat(GEN, "Home-Generic-Home Insurance-Core", "home owners insurance", "Broad") == "Paused"
        and kstat(GEN, "Home-Generic-Home Insurance-Core", "homeowner insurance", "Broad") == "Paused")
    chk("gen", "texas keys enabled in State",
        all(kstat(GEN, "Home-Generic-State", "texas home insurance", m) == "Enabled"
            for m in ["Exact", "Broad"]))
    chk("gen", "Peril holds Flood keys (flood insurance Exact+Broad)",
        all(kstat(GEN, "Home-Generic-Peril", "flood insurance", m) == "Enabled"
            for m in ["Exact", "Broad"]))

    print("\n-- BRAND-HOME --")
    en = enabled_groups(BH)
    at = "Home-NonGeneric-Allied Trust Insurance-Core"
    chk("bh", "Allied Trust group enabled", at in en)
    chk("bh", "allied trust keys enabled there + paused in Allied Insurance-Core",
        all(kstat(BH, at, "allied trust home insurance", m) == "Enabled" for m in ["Exact", "Broad"])
        and all(kstat(BH, "Home-NonGeneric-Allied Insurance-Core", "allied trust home insurance", m)
                in ("Paused", None) for m in ["Exact", "Broad"]))
    n_at = rsas[(BH, at)]
    chk("bh", "Allied Trust has 4 RSAs (2 Enabled)",
        len(n_at) == 4 and sum(r["Status"] == "Enabled" for r in n_at) == 2,
        f"{len(n_at)} RSAs, {sum(r['Status'] == 'Enabled' for r in n_at)} enabled")
    chk("bh", "BCC-01-labeled RSA present in Allied Trust",
        any("BCC-01" in r.get("Labels", "") for r in n_at))
    for old, new in [("Home-Non-Generic-Home_Group2", "Home-NonGeneric-American Insurance-Core"),
                     ("Home-Non-Generic-Home_Group5", "Home-NonGeneric-Allied Insurance-Core"),
                     ("Home-Non-Generic-Home_Group6", "Home-NonGeneric-21st Century Insurance-Core")]:
        chk("bh", f"rename landed: ...{new.split('-')[-2]}",
            (BH, new) in ag and (BH, old) not in ag)
    chk("bh", "Geico: historic renamed, zz-DUP paused",
        (BH, "Home-NonGeneric-Geico Insurance-Core") in ag
        and ag.get((BH, "zz-DUP-Home-NonGeneric-Geico Insurance-Core")) == "Paused")
    grp_of = {("costco condo insurance"): "Home-NonGeneric-Costco Insurance-Core",
              ("costco property insurance"): "Home-NonGeneric-Costco Insurance-Core",
              ("farmers condo"): "Home-NonGeneric-Farmers Insurance-Core",
              ("universal condo insurance"): "Home-Non-Generic-Home_Group4",
              ("universal property"): "Home-Non-Generic-Home_Group4"}
    missing = [(t, m) for t, m in R6_IN
               if kstat(BH, grp_of.get(t, "Home-NonGeneric-Geico Insurance-Core"), t, m) != "Enabled"]
    chk("bh", "R6 IN: all 12 migrant keys enabled", not missing, f"missing: {missing}")
    pruned = sum(1 for (c, g), d in kw.items() if c == BH
                 for (t, m), s in d.items()
                 if m == "Exact" and s == "Paused" and t in
                 {"american family", "american family insurance", "costco insurance",
                  "travelers", "travelers insurance", "travelers insurance company",
                  "the hartford", "the hartford insurance company", "hartford insurance",
                  "kin insurance", "kin insurance quote", "kin insurance reviews"})
    chk("bh", "16a prune: 12 flagship-book exacts paused", pruned == 12, f"found {pruned}")
    stray = [g for g in en if g.startswith("Home-Generic-") or g.endswith("Farmers Insurance-Flagship")]
    chk("bh", "B.7 purge: no generic/stray-flagship groups enabled", not stray, f"{stray}")

    print("\n-- CAFL --")
    en = enabled_groups(CAFL)
    chk("cafl", "enabled groups == 8 carriers, all -Flagship", en == CAFL_ENABLED,
        f"actual: {sorted(en)}")
    zz = [g for (c, g), s in ag.items() if c == CAFL and g.startswith("zz-DUP-")]
    chk("cafl", "7 zz-DUP groups present + Paused",
        len(zz) == 7 and all(ag[(CAFL, g)] == "Paused" for g in zz), f"found {len(zz)}")
    out_live = [(g, t, m) for (c, g), d in kw.items() if c == CAFL
                for (t, m), s in d.items() if s == "Enabled" and ag.get((c, g)) == "Enabled"
                and any(x in t for x in ["condo", "landlord", "property insurance"])]
    chk("cafl", "zero condo/landlord/property-insurance keys live", not out_live, f"{out_live}")

    print("\n-- NEGATIVES --")
    for c, nm in [(CAFL, "CAFL"), (NB, "NB")]:
        got = cneg[c].get("Phrase", set())
        chk("neg", f"{nm} phrase negatives 8/8 (tokens exact)", got == TOKENS,
            f"missing: {TOKENS - got}, extra: {got - TOKENS - {'fema'} }")
    for c, nm in [(GEN, "GENERIC"), (BH, "BRAND-HOME")]:
        got = len(cneg[c].get("Exact", set()))
        want = 104 if post else 0
        chk("neg", f"{nm} exact negatives {'104/104' if post else 'absent pre-cutover'}",
            got == want or (not post and got <= 1), f"count {got}")

    print("\n-- LABELS --")
    lab = sum(1 for r in rows if "restr-2026-09" in r.get("Labels", ""))
    chk("lab", "restr-2026-09-* labels present at scale (>150 rows)", lab > 150, f"{lab} rows")

    fails = [x for x in results if not x[2]]
    print("\n" + "=" * 70)
    print(f"VERDICT: {len(results) - len(fails)}/{len(results)} PASS — "
          + ("GATE A CLEAR" + (" — POST-CUTOVER STATE VERIFIED" if post
                               else ": PROCEED TO CUTOVER MORNING") if not fails
             else f"{len(fails)} FAILURE(S) — HOLD"))
    for s, n, ok, d in fails:
        print(f"  FAIL [{s}] {n} — {d}")


if __name__ == "__main__":
    main()
