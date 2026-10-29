"""
ms_negatives_and_wave2.py -- turn the month-1 evaluation into the two Editor
actions: (A) collapsed ad-group negatives with the BRAND terms called out via
the registry, and (B) the Phase-2 / wave-2 migration of the serving Quote and
Companies terms into their split-out groups. READ-ONLY against paid_search.db.

(A) NEGATIVES -- from the postlaunch *_negatives.csv candidates:
  BRAND rows      collapsed to ONE phrase negative per carrier, annotated from
                  brand_registry (bid_status / category) -> the "Brand Callout
                  (QW)" sheet is the handoff for Ben's QW carrier-negatives
                  worksheet; the import CSV for these is SEPARATE and posts
                  only AFTER the worksheet pass.
  CROSS_VERTICAL  collapsed to the observed vertical tokens (renters, life, ...)
                  -> phrase negatives, direct-apply.
  UNQUALIFIED     surgical EXACT negatives on queries clearing the spend/click
                  floor, in the ad group where observed; the long tail is
                  reported as a token-frequency readout, not imported.
  SCOPE: ad-group level ONLY (campaign-level would strangle the Non-Generic
  brand book sharing these campaigns). Brand + cross-vertical negatives expand
  across every (live campaign x observed generic group) pair that exists and
  is ENABLED, resolved from the DB.

(B) WAVE 2 -- reads the selection workbook's Migration sheet (label
  restr-2026-09-move), resolves each term's CURRENTLY-SERVING instances from
  keyword_current, and emits:
    _wave2_pauses.csv  the old instances, keyed by Id (verify-as-EDITS ritual!)
    _wave2_adds.csv    the term in its destination group across all six live
                       campaigns, at the match types it serves at today.
  Companies-theme destinations remap to --companies-dest (default
  Home-Generic-Other, since Companies was folded into Other at launch).

SEQUENCE: direct negatives -> QW worksheet pass -> brand negatives -> wave-2
pauses+adds -> log all under restr-2026-09.

    python ms_negatives_and_wave2.py --candidates-csv "output\\ms_expansion_postlaunch_2026-10-19_negatives.csv"
"""
import argparse
import csv
import os
import re
import sqlite3
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ms_expansion_coverage import normalize_09

ACCT = 2
CROSS_TOKENS = ["renters", "renter", "life", "medicare", "health", "pet",
                "business", "commercial"]
PLURAL_FOLD = {"renter": "renters"}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--candidates-csv", required=True,
                   help="the ms_expansion_postlaunch *_negatives.csv")
    p.add_argument("--db", default=r"C:\Projects\google-ads-ops\db\paid_search.db")
    p.add_argument("--selection-xlsx",
                   default=r"C:\Projects\google-ads-ops\analysis\ms-home-kw-expansion"
                           r"\output\ms_expansion_selection_2026-09-17.xlsx")
    p.add_argument("--companies-dest", default="Home-Generic-Other",
                   help="destination for Companies-theme migration rows (folded at launch)")
    p.add_argument("--min-unq-spend", type=float, default=10.0)
    p.add_argument("--min-unq-clicks", type=int, default=2)
    p.add_argument("--out-prefix", default=r"C:\Projects\google-ads-ops\analysis\ms-home-kw-expansion"
                                           r"\output\ms_negatives_wave2_%s" % date.today().isoformat())
    a = p.parse_args()

    conn = sqlite3.connect("file:%s?mode=ro" % a.db, uri=True)
    conn.row_factory = sqlite3.Row

    # ---- registry: brand_name -> (bid_status, category) ----
    reg = {}
    for r in conn.execute("SELECT brand_name, normalized_name, bid_status, brand_category "
                          "FROM brand_registry"):
        reg[(r["brand_name"] or "").strip().lower()] = \
            (r["normalized_name"], r["bid_status"] or "", r["brand_category"] or "")
    print("registry loaded: %d brands" % len(reg))

    # ---- candidates ----
    cands = list(csv.DictReader(open(a.candidates_csv, newline="", encoding="utf-8-sig")))
    for c in cands:
        c["clicks"] = int(float(c.get("clicks") or 0))
        c["spend"] = float(c.get("spend") or 0)
        rsn = c.get("reason") or ""
        c["cls"] = rsn.split(":")[0].strip()
        m = re.match(r"BRAND:\s*([^-]+?)\s*->", rsn)
        c["brand"] = m.group(1).strip() if m else ""
    print("candidates: %d rows (%s)" % (len(cands), a.candidates_csv))

    observed_groups = sorted({c["ad_group"] for c in cands if c.get("ad_group")})

    # ---- live generic-group scope from keyword_current. NOT ad_groups: that
    # table only refreshes on a STRUCTURE export/04 load, so it can miss groups
    # created since (the 36 launch groups, until the next structure refresh).
    # keyword_current refreshes with every keyword load and carries the parent
    # statuses. 'Home-Generic-%' excludes the Home-NonGeneric brand book, which
    # must never receive generic-protection negatives. ----
    generic_pairs = sorted({(r["campaign_name"], r["ad_group_name"]) for r in conn.execute(
        "SELECT DISTINCT campaign_name, ad_group_name FROM keyword_current "
        "WHERE account_id=? AND campaign_status='ENABLED' AND ad_group_status='ENABLED' "
        "AND ad_group_name LIKE 'Home-Generic-%'", (ACCT,))})
    obs_set = {g for g in observed_groups if g.startswith("Home-Generic-")}
    scope_pairs = [p for p in generic_pairs if p[1] in obs_set]
    skipped = sorted(set(observed_groups) - obs_set)
    missing = sorted(obs_set - {p[1] for p in generic_pairs})
    print("scope: %d live GENERIC (campaign x group) pairs (brand negatives span ALL of them); "
          "%d observed-group pairs (unqualified exacts)" % (len(generic_pairs), len(scope_pairs)))
    if skipped:
        print("  [note] NonGeneric/brand groups in candidates EXCLUDED from scope: %s" % ", ".join(skipped))
    if missing:
        print("  [warn] observed groups not live in keyword_current (check keyword load freshness): %s"
              % ", ".join(missing))

    # ---- (A1) BRAND collapse ----
    bagg = defaultdict(lambda: {"clicks": 0, "spend": 0.0, "q": [], "groups": set()})
    for c in cands:
        if c["cls"] != "BRAND" or not c["brand"]:
            continue
        b = bagg[c["brand"]]
        b["clicks"] += c["clicks"]; b["spend"] += c["spend"]
        b["q"].append((c["spend"], c["negative_keyword"]))
        b["groups"].add(c.get("ad_group") or "")
    brand_callout, brand_import = [], []
    for brand, b in sorted(bagg.items(), key=lambda kv: -kv[1]["spend"]):
        norm, bid, cat = reg.get(brand.lower(), (brand.lower(), "(not in registry)", ""))
        token = norm or brand.lower()
        exq = "; ".join(q for _, q in sorted(b["q"], reverse=True)[:3])
        brand_callout.append({"brand": brand, "negative_phrase": token,
                              "bid_status": bid, "category": cat,
                              "queries": len(b["q"]), "clicks": b["clicks"],
                              "spend": round(b["spend"], 2),
                              "observed_in": "; ".join(sorted(g for g in b["groups"] if g)),
                              "examples": exq,
                              "routing": "QW carrier-negatives worksheet FIRST; import after approval"})
        for camp, grp in generic_pairs:
            brand_import.append({"Type": "Ad Group Negative Keyword", "Campaign": camp,
                                 "Ad Group": grp, "Keyword": token, "Match Type": "Phrase"})
    print("\nBRAND CALLOUT (collapsed): %d carriers / $%.0f observed spend"
          % (len(brand_callout), sum(x["spend"] for x in brand_callout)))
    for x in brand_callout:
        print("  %-22s neg='%s'  status=%-14s $%-8s clicks=%-4d %s"
              % (x["brand"][:22], x["negative_phrase"], x["bid_status"],
                 f"{x['spend']:,.0f}", x["clicks"], x["observed_in"][:50]))

    # ---- (A2) CROSS_VERTICAL -> SHARED NEGATIVE KEYWORD LISTS (campaign-assoc).
    # Campaign level is SAFE for these: renters/life/medicare queries are unwanted
    # in every group of a home campaign, brand groups included -- the ad-group-only
    # rule protects the brand book from CARRIER negatives, not from these. ----
    seen_tok = defaultdict(lambda: [0, 0.0])
    for c in cands:
        if c["cls"] != "CROSS_VERTICAL":
            continue
        for t in CROSS_TOKENS:
            if re.search(r"\b%s\b" % t, c["negative_keyword"]):
                k = PLURAL_FOLD.get(t, t)
                seen_tok[k][0] += c["clicks"]; seen_tok[k][1] += c["spend"]
                break
    direct_rows, direct_import = [], []
    RENTERS_LIST = "Home - Renters Exclusion"
    CROSS_LIST = "Home - Cross-Vertical Exclusion"
    rcl, rsp = seen_tok.get("renters", [0, 0.0])
    list_items = [{"List": RENTERS_LIST, "Keyword": "renters", "Match Type": "Phrase",
                   "observed_clicks": rcl, "observed_spend": round(rsp, 2),
                   "note": "OWN list: DETACH from home campaigns when the renters campaigns launch"}]
    for tok in ("life", "medicare", "health", "pet", "business", "commercial"):
        cl, sp = seen_tok.get(tok, [0, 0.0])
        list_items.append({"List": CROSS_LIST, "Keyword": tok, "Match Type": "Phrase",
                           "observed_clicks": cl, "observed_spend": round(sp, 2),
                           "note": "observed this window" if (cl or sp) else "preventive"})
    live_camp_names = sorted({p[0] for p in generic_pairs})
    list_assoc = [{"Campaign": camp, "Negative Keyword List": lst}
                  for camp in live_camp_names for lst in (RENTERS_LIST, CROSS_LIST)]

    # ---- (A3) UNQUALIFIED surgical exacts + tail readout ----
    tail_tok = defaultdict(lambda: [0, 0.0])
    grp_camps = defaultdict(list)
    for camp, grp in scope_pairs:
        grp_camps[grp].append(camp)
    for c in sorted((c for c in cands if c["cls"] == "UNQUALIFIED"),
                    key=lambda x: -x["spend"]):
        if c["spend"] >= a.min_unq_spend or c["clicks"] >= a.min_unq_clicks:
            direct_rows.append({"kind": "UNQUALIFIED", "negative": c["negative_keyword"],
                                "match": "Exact", "scope": c.get("ad_group") or "(observed group)",
                                "clicks": c["clicks"], "spend": round(c["spend"], 2)})
            for camp in grp_camps.get(c.get("ad_group") or "", []):
                direct_import.append({"Type": "Ad Group Negative Keyword", "Campaign": camp,
                                      "Ad Group": c["ad_group"],
                                      "Keyword": c["negative_keyword"], "Match Type": "Exact"})
        else:
            for t in re.findall(r"[a-z]{4,}", c["negative_keyword"].lower()):
                tail_tok[t][0] += 1
                tail_tok[t][1] += c["spend"]
    n_unq = sum(1 for r in direct_rows if r["kind"] == "UNQUALIFIED")
    print("\nSHARED LISTS: 2 lists / %d items, associated to %d campaigns (campaign level -- "
          "safe for cross-vertical)" % (len(list_items), len(live_camp_names)))
    print("UNQUALIFIED surgical exacts: %d -> %d ad-group import rows" % (n_unq, len(direct_import)))
    print("unqualified TAIL (below floor; recurring tokens, review-only):")
    for t, (n, sp) in sorted(tail_tok.items(), key=lambda kv: -kv[1][1])[:12]:
        if n >= 3:
            print("    %-18s in %d queries ($%.0f)" % (t, n, sp))

    # ---- (B) WAVE-2 migration ----
    from openpyxl import load_workbook
    mig_terms = []
    if os.path.exists(a.selection_xlsx):
        wb_in = load_workbook(a.selection_xlsx, data_only=True, read_only=True)
        sheet = next((s for s in ("Migration (wave 2)", "Quote Migration (wave 2)")
                      if s in wb_in.sheetnames), None)
        if sheet:
            rows_it = list(wb_in[sheet].iter_rows(values_only=True))
            hdr_i = next(i for i, r in enumerate(rows_it) if r and r[0] == "keyword")
            hdr = rows_it[hdr_i]
            for r in rows_it[hdr_i + 1:]:
                d = dict(zip(hdr, r))
                if d.get("keyword"):
                    dest = d.get("move_to") or "Home-Generic-Quote"
                    if "Companies" in str(dest):
                        dest = a.companies_dest
                    mig_terms.append({"term": str(d["keyword"]).strip(), "dest": dest})
        wb_in.close()
    if not mig_terms:
        print("\nWAVE-2: no Migration sheet found in %s -- pass --selection-xlsx" % a.selection_xlsx)
    pauses, adds = [], []
    live_camps = [r[0] for r in conn.execute(
        "SELECT campaign_name FROM campaigns WHERE account_id=? AND status='ENABLED'", (ACCT,))]
    kc = {r[1] for r in conn.execute("PRAGMA table_info(keyword_current)")}
    idcol = "criterion_id" if "criterion_id" in kc else "ad_group_criterion_id"
    altcol = "ad_group_criterion_id" if idcol == "criterion_id" else "criterion_id"
    for m in mig_terms:
        n9 = normalize_09(m["term"])
        cur = [r for r in conn.execute(
            "SELECT * FROM keyword_current WHERE account_id=? AND is_serving=1", (ACCT,))
            if normalize_09(r["keyword_text"]) == n9]
        mts = sorted({(r["match_type"] or "").capitalize() for r in cur}) or ["Phrase"]
        for r in cur:
            pauses.append({"Type": "Keyword", "Id": r[idcol], "Alt Id": r[altcol],
                           "Campaign": r["campaign_name"], "Ad Group": r["ad_group_name"],
                           "Keyword": r["keyword_text"],
                           "Match Type": (r["match_type"] or "").capitalize(),
                           "Status": "Paused", "label": "restr-2026-09-move"})
        for camp in live_camps:
            for mt in mts:
                adds.append({"Campaign": camp, "Ad Group": m["dest"], "Keyword": m["term"],
                             "Match Type": mt, "Status": "Active",
                             "label": "restr-2026-09-move"})
    print("\nWAVE-2: %d migration terms -> %d old-instance PAUSES + %d ADDS into destinations"
          % (len(mig_terms), len(pauses), len(adds)))
    for m in mig_terms:
        print("    %-40s -> %s" % (m["term"], m["dest"]))
    conn.close()

    # ---- outputs ----
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.utils import get_column_letter
    HEADF = PatternFill("solid", fgColor="1F3864")
    WHITE = Font(name="Arial", bold=True, color="FFFFFF", size=10)
    BOLD = Font(name="Arial", bold=True, size=10)
    REG = Font(name="Arial", size=10)
    NOTE = Font(name="Arial", size=9, italic=True, color="595959")
    TITLE = Font(name="Arial", bold=True, size=13, color="1F3864")

    def sheet_from(wb, title, heading, cols, dicts, widths, note=None):
        ws = wb.create_sheet(title)
        ws["A1"] = heading; ws["A1"].font = TITLE
        ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=max(len(cols), 2))
        r0 = 3
        if note:
            ws["A2"] = note; ws["A2"].font = NOTE
            ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=max(len(cols), 2))
            r0 = 4
        for j, h in enumerate(cols, 1):
            c = ws.cell(r0, j, h); c.fill = HEADF; c.font = WHITE
            c.alignment = Alignment(horizontal="center", wrap_text=True)
        r = r0 + 1
        for d in dicts:
            for j, k in enumerate(cols, 1):
                ws.cell(r, j, d.get(k)).font = BOLD if j == 1 else REG
            r += 1
        for j, w in enumerate(widths, 1):
            ws.column_dimensions[get_column_letter(j)].width = w
        ws.freeze_panes = ws.cell(r0 + 1, 1).coordinate
        ws.auto_filter.ref = "A%d:%s%d" % (r0, get_column_letter(len(cols)), max(r - 1, r0))

    wb = Workbook(); wb.remove(wb.active)
    sheet_from(wb, "Brand Callout (QW)", "Carrier terms leaking into the generic groups -- QW worksheet handoff",
               ["brand", "negative_phrase", "bid_status", "category", "queries", "clicks",
                "spend", "observed_in", "examples", "routing"], brand_callout,
               [20, 22, 14, 16, 8, 8, 10, 44, 70, 42],
               note="bid_status from brand_registry (read-only). Route through Ben's QW "
                    "carrier-negatives worksheet FIRST; the brand import CSV posts only after.")
    sheet_from(wb, "Shared Lists", "Shared negative keyword lists + campaign associations (apply now)",
               ["List", "Keyword", "Match Type", "observed_clicks", "observed_spend", "note"],
               list_items, [30, 16, 10, 12, 12, 56],
               note="Create both lists in the Shared Library, add the items, associate to all %d "
                    "live campaigns (see _neg_list_associations.csv). Renters rides its OWN list "
                    "so it detaches cleanly when the renters campaigns launch." % len(live_camp_names))
    sheet_from(wb, "Unqualified Exacts", "Surgical exact negatives in the observed groups (apply now)",
               ["kind", "negative", "match", "scope", "clicks", "spend"], direct_rows,
               [16, 44, 8, 40, 8, 9],
               note="AD-GROUP level, observed group only. CARRIER negatives stay ad-group level too "
                    "(all generic groups) -- never campaign level, which would block the "
                    "Non-Generic brand book sharing these campaigns.")
    sheet_from(wb, "Wave2 Pauses", "Phase 2: pause the old serving instances (verify rows show as EDITS)",
               ["Type", "Id", "Alt Id", "Campaign", "Ad Group", "Keyword", "Match Type",
                "Status", "label"], pauses, [9, 18, 18, 44, 40, 30, 10, 8, 20],
               note="Import, confirm Editor shows EDITS not additions (if additions, re-key on "
                    "Alt Id), then post. Never post pauses and adds in the same push as negatives.")
    sheet_from(wb, "Wave2 Adds", "Phase 2: the migrated terms in their destination groups (all live campaigns)",
               ["Campaign", "Ad Group", "Keyword", "Match Type", "Status", "label"], adds,
               [44, 40, 30, 10, 8, 20])
    out_x = a.out_prefix + ".xlsx"
    os.makedirs(os.path.dirname(out_x) or ".", exist_ok=True)
    wb.save(out_x)

    def wcsv(path, cols, rows):
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
            w.writeheader(); w.writerows(rows)
        print("  wrote %s (%d rows)" % (path, len(rows)))

    print("\noutputs:")
    print("  wrote %s" % out_x)
    icols = ["Type", "Campaign", "Ad Group", "Keyword", "Match Type"]
    wcsv(a.out_prefix + "_neg_list_items.csv", ["List", "Keyword", "Match Type"], list_items)
    wcsv(a.out_prefix + "_neg_list_associations.csv", ["Campaign", "Negative Keyword List"], list_assoc)
    wcsv(a.out_prefix + "_negatives_direct.csv", icols, direct_import)
    wcsv(a.out_prefix + "_negatives_brand_AFTER_QW.csv", icols, brand_import)
    wcsv(a.out_prefix + "_wave2_pauses.csv",
         ["Type", "Id", "Campaign", "Ad Group", "Keyword", "Match Type", "Status"], pauses)
    wcsv(a.out_prefix + "_wave2_adds.csv",
         ["Campaign", "Ad Group", "Keyword", "Match Type", "Status"], adds)
    print("\nSEQUENCE: 1) create the 2 shared lists + items, associate to the live campaigns"
          "\n             (_neg_list_items / _neg_list_associations)"
          "\n          2) post _negatives_direct (unqualified exacts, ad-group level)"
          "\n          3) QW worksheet pass on the Brand Callout sheet"
          "\n          4) post _negatives_brand_AFTER_QW (ALL live generic groups, ad-group level)"
          "\n          5) wave2 pauses (verify EDITS) + adds   6) log under restr-2026-09."
          "\n          Import 2 test rows of each file type first and match the Type/List column"
          "\n          naming to your own Editor export if it differs.")


if __name__ == "__main__":
    main()
