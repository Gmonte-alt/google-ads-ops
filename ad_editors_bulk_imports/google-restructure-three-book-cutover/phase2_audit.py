"""
phase2_audit.py — Phase 2 staged-state / post-launch audit

Run against the STAGED export (before the atomic post) and again against the
post-launch export. Verdicts adapt: tier pauses + NB enable are required only
when --post is passed (the staged export shows them staged; Editor exports
pending state, so both runs should pass identically if everything is staged).

Usage:
  python phase2_audit.py --export <staged_or_post_export.csv> [--post]
"""

import csv
import argparse
from collections import defaultdict

GEN = "HOME INSURANCE-GENERIC-HOME-INTENT-NB-SEARCH-ALL-USA-STE"
T1 = "HOME INSURANCE_STATE-GROUP_TIER1RPL_COMBINED_TROAS_SEARCH_ALLDEVICE_USA-STE"
T2 = "HOME INSURANCE_STATE-GROUP_TIER2RPL_COMBINED_TROAS_SEARCH_ALLDEVICE_USA-STE"
T3 = "HOME INSURANCE_STATE-GROUP_TIER3RPL_COMBINED_TROAS_SEARCH_ALLDEVICE_USA-STE"
T4 = "HOME INSURANCE_STATE-GROUP_TIER4RPL_COMBINED_TROAS_SEARCH_ALLDEVICE_USA-STE"
NB = "HOME INSURANCE_FLAGSHIP_NB_TROAS_SEARCH_ALLDEVICE_USA-STE"
CA = "HOME INSURANCE-CALIFORNIA-NB-SEARCH-ALL-USA-STE"
FL = "HOME INSURANCE-FLORIDA-NB-SEARCH-ALL-USA-STE"
LIST_BARE, LIST_ROUT = "BARE_BRAND_EXACT", "HOME_ROUTING"
TRIM = {"Minnesota, United States", "New Mexico, United States", "Arkansas, United States"}
NB_ADDS7 = [("farmers insurance", "Exact"), ("kin insurance", "Exact"),
            ("kin insurance reviews", "Exact"), ("travelers", "Exact"),
            ("travelers insurance", "Exact"), ("hartford insurance", "Exact"),
            ("american family insurance", "Exact")]
ADDS9 = [("farmers homeowners insurance", "Broad"),
         ("hartford home insurance colorado", "Broad"),
         ("hartford home insurance colorado", "Exact"),
         ("hartford home insurance new jersey", "Broad"),
         ("hartford home insurance new jersey", "Exact"),
         ("state farm home insurance colorado", "Broad"),
         ("state farm home insurance new jersey", "Broad"),
         ("travelers home insurance colorado", "Broad"),
         ("travelers home insurance new jersey", "Broad")]

res = []


def chk(name, ok, detail=""):
    res.append(ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--export", required=True)
    p.add_argument("--post", action="store_true")
    a = p.parse_args()
    rows = list(csv.DictReader(open(a.export, encoding="utf-16"), delimiter="\t"))

    camp_status = {r["Campaign"]: r["Campaign Status"] for r in rows if r.get("Budget")}
    camp_row = {r["Campaign"]: r for r in rows if r.get("Budget")}
    ag = {(r["Campaign"], r["Ad Group"]): r["Ad Group Status"]
          for r in rows if r.get("Ad Group") and r.get("Ad Group Status")}
    geo = defaultdict(set)
    for r in rows:
        if r.get("Location") and r.get("Location type") == "State" and not r.get("Criterion Type"):
            geo[r["Campaign"]].add(r["Location"])
    lists = {r["Shared set name"] for r in rows if r.get("Shared set name")}
    attach = defaultdict(set)
    for r in rows:
        if r.get("Shared set name") and r.get("Campaign"):
            attach[r["Shared set name"]].add(r["Campaign"])

    def kw(campaign):
        out = {}
        for r in rows:
            k = r.get("Keyword", "").strip()
            if (r["Campaign"] != campaign or not k or r.get("Shared set name")
                    or "Negative" in r.get("Criterion Type", "")):
                continue
            key = (k.lower(), r["Criterion Type"])
            enabled = (r.get("Status") == "Enabled"
                       and ag.get((campaign, r["Ad Group"])) == "Enabled")
            out.setdefault(key, []).append((enabled, r["Ad Group"]))
        return out

    print("=" * 70)
    print(f"PHASE 2 AUDIT — {'POST-LAUNCH' if a.post else 'STAGED'} — {a.export}")
    print("=" * 70)

    print("\n-- ITEM 4: BARE_BRAND_EXACT --")
    chk("list exists", LIST_BARE in lists)
    chk("attached to GENERIC + TIER2/BH",
        {GEN, T2} <= attach.get(LIST_BARE, set()),
        f"attached: {len(attach.get(LIST_BARE, set()))} campaigns")
    chk("NOT attached to flagships (bare-brand is their traffic)",
        not ({NB, CA, FL} & attach.get(LIST_BARE, set())))
    print("    NOTE: member count (104) not verifiable from a standard export — "
          "confirm in Shared Library UI or a shared-set-inclusive export")

    print("\n-- ITEM 5: NB GEO --")
    chk("NB = 45 states", len(geo[NB]) == 45, f"{len(geo[NB])}")
    chk("MN/NM/AR absent from NB", not (TRIM & geo[NB]), f"present: {TRIM & geo[NB]}")
    chk("NB footprint == GENERIC footprint", geo[NB] == geo[GEN])

    print("\n-- ITEM 6: BH TRANSITION ADDS --")
    t2k = kw(T2)
    missing = [(k, m) for (k, m) in ADDS9
               if not any(en for en, _ in t2k.get((k, m), []))]
    chk("9 transition adds enabled in TIER2", not missing, f"missing: {missing}")
    # miss-detector (compare-script technique): any enabled tier brand key w/o T2 placement
    t2texts = {key for key, insts in t2k.items() if any(en for en, _ in insts)}
    misses = []
    for c in (T1, T3, T4):
        for (k, m), insts in kw(c).items():
            if any(en for en, _ in insts) and (k, m) not in t2texts:
                grp = insts[0][1]
                if not grp.lower().startswith("home-generic"):
                    misses.append((k, m))
    chk("zero enabled tier brand keys without a TIER2 placement", not misses,
        f"{sorted(set(misses))[:6]}")

    print("\n-- ITEM 3: NB EXACT ADDS (analyst ruling 10/28) --")
    nbk = kw(NB)
    nb_missing = [(k, m) for (k, m) in NB_ADDS7
                  if not any(en for en, _ in nbk.get((k, m), []))]
    chk("7 ruled Exacts enabled in NB", not nb_missing, f"missing: {nb_missing}")
    deferred = [("costco insurance", "Exact"), ("the hartford", "Exact"),
                ("kin insurance quote", "Exact"), ("travelers insurance company", "Exact"),
                ("the hartford insurance company", "Exact"), ("american family", "Exact"),
                ("geico", "Exact")]
    created = [(k, m) for (k, m) in deferred if nbk.get((k, m))]
    chk("DEFER keys never created in NB", not created, f"created: {created}")

    print("\n-- ITEM 7: NEGATIVE MATRIX --")
    chk("HOME_ROUTING exists", LIST_ROUT in lists)
    chk("HOME_ROUTING -> NB", NB in attach.get(LIST_ROUT, set()))
    chk("HOME_ROUTING NOT on GENERIC/TIER2 (would block their books)",
        not ({GEN, T2} & attach.get(LIST_ROUT, set())))
    cafl_rout = {CA, FL} & attach.get(LIST_ROUT, set())
    print(f"    HOME_ROUTING on CA/FL: {sorted(c[:30] for c in cafl_rout) or 'no (decision-1 gated)'}")
    print("    Progressive strays + 'true' edit: BLOCKED-INPUT (shared-library export needed)")

    print("\n-- SELF-BLOCK CLEARANCE --")
    viol = []
    for c in (GEN, T2):
        for (k, m), insts in kw(c).items():
            if m == "Exact" and any(en for en, _ in insts):
                viol.append((c[:20], k))  # checked against B3 below if available
    # lightweight: the 13 known texts
    known = {"american family", "american family insurance", "costco insurance",
             "farmers insurance", "hartford insurance", "kin insurance",
             "kin insurance quote", "kin insurance reviews", "the hartford",
             "the hartford insurance company", "travelers", "travelers insurance",
             "travelers insurance company"}
    live = [k for c, k in viol if k in known]
    chk("13 bare-brand exacts paused (p2_05)", not live, f"still enabled: {live}")

    print("\n-- SEAM SPOT-CHECKS --")
    for c, nm in [(GEN, "GENERIC"), (T2, "TIER2/BH")]:
        d = kw(c)
        bare = any(en for en, _ in d.get(("geico", "Exact"), []))
        chk(f"'geico' [Exact] not enabled in {nm}", not bare)
    ghi_gen = any(en for en, _ in kw(GEN).get(("geico home insurance", "Broad"), [])) \
        or any(en for en, _ in kw(GEN).get(("geico home insurance", "Exact"), []))
    chk("'geico home insurance' not enabled in GENERIC", not ghi_gen)
    ghi_t2 = kw(T2).get(("geico home insurance", "Broad"), []) + \
        kw(T2).get(("geico home insurance", "Exact"), [])
    in_geico_grp = all("geico" in g.lower() for en, g in ghi_t2 if en) and \
        any(en for en, _ in ghi_t2)
    chk("'geico home insurance' enabled in TIER2, Geico group only", in_geico_grp,
        f"{[(g) for en, g in ghi_t2 if en]}")

    print("\n-- CAMPAIGN LEVEL --")
    for c, nm in [(T1, "T1"), (T3, "T3"), (T4, "T4")]:
        want = "Paused"
        ok = camp_status.get(c) == want
        chk(f"{nm} {want}{'' if a.post else ' (staged)'}", ok, camp_status.get(c))
    nbr = camp_row.get(NB, {})
    chk("NB Enabled" + ("" if a.post else " (staged)"), camp_status.get(NB) == "Enabled",
        camp_status.get(NB))
    chk("NB tROAS + real budget set",
        nbr.get("Bid Strategy Type") == "Target ROAS" and nbr.get("Budget") not in ("", "1.00"),
        f"strat={nbr.get('Bid Strategy Type')} budget={nbr.get('Budget')}")
    bh_geo = len(geo[T2])
    chk("TIER2/BH geo = 45 or 48 (per verdict)", bh_geo in (45, 48), f"{bh_geo} states")

    print("\n-- LABELS --")
    lab = sum(1 for r in rows if "restr-2026-10" in r.get("Labels", ""))
    chk("restr-2026-10-* labels present (>=25 rows)", lab >= 25, f"{lab}")

    fails = res.count(False)
    print("\n" + "=" * 70)
    print(f"VERDICT: {res.count(True)}/{len(res)} PASS — "
          + ("CLEAR" if not fails else f"{fails} FAILURE(S) — HOLD"))


if __name__ == "__main__":
    main()
