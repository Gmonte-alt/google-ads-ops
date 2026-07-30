#!/usr/bin/env python
"""
brand_serve_split.py -- ad_assets/analysis/ -- served-face percentages for
chosen variants SPLIT BY BRAND vs NON-BRAND ad groups. The readout gating
the DKI phase-out variant: among the AUTHORED (non-DKI) lines in brand
groups, which earn any serve at all?

Classification (auditable -- every decision written to a CSV):
  * EXCLUDED: ad-group name matches the flagged patterns (geico / state
    farm / statefarm) -- limited-visibility territory, out per standing rule
  * BRAND: normalized name (lowercase, hyphens/spaces stripped) contains
    'nongeneric' -- catches BOTH account spellings ('Home-NonGeneric-Farmers'
    and 'Home-Non-Generic-Home_Group1')
  * forced NON-BRAND overrides (default: 'caregio') -- CaRegional is geo
    traffic despite its Non-Generic prefix; extend via --nonbrand-overrides
  * NON-BRAND: everything else (Generic, Fire, ...)

Scope: enabled ads only (ad_status='ENABLED'), device='ALL', since --from.
Shown-on% = SUM(asset impressions in class) / SUM(ad impressions in class)
-- the within-ad relative read aggregated across the variant's ads; valid
per the additivity rules (never exclusive credit; ~2.78 headlines co-serve,
so a variant's column sums to ~278%).

Outputs (ad_assets\\exports\\):
  brand_serve_split_<date>.csv          the serve table (the deliverable)
  brand_split_adgroup_classes_<date>.csv  per-ad-group classification audit

  python brand_serve_split.py --db "...\\paid_search.db"                # V1,V3 YTD
  python brand_serve_split.py --db "...\\paid_search.db" --variants V1,V3,V2
"""
import argparse
import csv
import datetime
import os
import re
import sqlite3
import sys
from collections import defaultdict

ACCOUNT_ID = 1
FLAG_PATTERNS = ["geico", "state farm", "statefarm"]
EXPORT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "exports")
NAME_TABLE_CANDIDATES = [
    ("ad_groups", "ad_group_id", "ad_group_name"),
    ("ad_groups", "ad_group_id", "name"),
    ("ad_groups", "id", "name"),
]


def find_names(conn):
    for table, idc, namec in NAME_TABLE_CANDIDATES:
        for scoped in (True, False):
            try:
                q = f"SELECT {idc}, {namec} FROM {table}"
                params = (ACCOUNT_ID,) if scoped else ()
                if scoped:
                    q += " WHERE account_id = ?"
                return {int(i): (n or "") for i, n in conn.execute(q, params).fetchall()}
            except sqlite3.Error:
                continue
    sys.exit("[ABORT] no ad-group name table found -- classification needs names.")


def classify(name, nonbrand_overrides):
    low = name.lower()
    if any(p in low for p in FLAG_PATTERNS):
        return "EXCLUDED"
    norm = re.sub(r"[-\s_]", "", low)
    if any(ov in norm for ov in nonbrand_overrides if ov):
        return "non-brand"
    return "brand" if "nongeneric" in norm else "non-brand"


def main():
    p = argparse.ArgumentParser(description="Brand vs non-brand served-face split (read-only).")
    p.add_argument("--db", required=True)
    p.add_argument("--variants", default="V1,V3",
                   help="Comma-separated variant labels or fingerprints (default V1,V3)")
    p.add_argument("--from", dest="date_from", default="2026-01-01", metavar="YYYY-MM-DD")
    p.add_argument("--nonbrand-overrides", default="caregio",
                   help="Comma-separated NORMALIZED substrings forced to non-brand "
                        "(default 'caregio' -- CaRegional is geo, not brand)")
    args = p.parse_args()

    if not os.path.isfile(args.db):
        sys.exit("[ABORT] database not found: %s" % args.db)
    os.makedirs(EXPORT_DIR, exist_ok=True)
    conn = sqlite3.connect("file:%s?mode=ro" % args.db.replace("\\", "/"), uri=True)
    names = find_names(conn)
    overrides = [re.sub(r"[-\s_]", "", s.strip().lower())
                 for s in args.nonbrand_overrides.split(",")]
    variants = [v.strip() for v in args.variants.split(",") if v.strip()]
    stamp = datetime.date.today().isoformat()

    # classification audit (only ad groups that actually host these variants' ads)
    ag_class = {}

    serve_rows = []      # variant, class, field_type, text, asset_impr, asset_clicks
    denom = {}           # (variant, class) -> ad impressions
    summary = []

    for variant in variants:
        ads = conn.execute(
            """SELECT ad_id, ad_group_id FROM rsa_ads
               WHERE account_id=? AND ad_status='ENABLED'
                 AND COALESCE(variant_label, fingerprint)=?""",
            (ACCOUNT_ID, variant)).fetchall()
        if not ads:
            print("[WARN] no enabled ads for %s -- skipped" % variant)
            continue
        class_ads = defaultdict(list)
        for ad_id, ag in ads:
            name = names.get(ag, "")
            cls = classify(name, overrides) if name else "UNKNOWN"
            ag_class[ag] = (name, cls)
            if cls in ("brand", "non-brand"):
                class_ads[cls].append(ad_id)

        for cls, ad_ids in sorted(class_ads.items()):
            marks = ",".join("?" * len(ad_ids))
            ad_impr = conn.execute(
                f"""SELECT SUM(impressions) FROM rsa_ad_performance_daily
                    WHERE account_id=? AND device='ALL' AND report_date>=?
                      AND ad_id IN ({marks})""",
                [ACCOUNT_ID, args.date_from] + ad_ids).fetchone()[0] or 0
            denom[(variant, cls)] = ad_impr

            rows = conn.execute(
                f"""SELECT t.text, a.field_type,
                           SUM(a.impressions), SUM(a.clicks)
                    FROM rsa_asset_performance_daily a
                    JOIN asset_text_data t
                      ON t.account_id=a.account_id AND t.asset_id=a.asset_id
                    WHERE a.account_id=? AND a.device='ALL' AND a.report_date>=?
                      AND a.ad_id IN ({marks})
                    GROUP BY t.text, a.field_type""",
                [ACCOUNT_ID, args.date_from] + ad_ids).fetchall()
            for text, ft, impr, clicks in rows:
                serve_rows.append((variant, cls, ft, text, impr or 0, clicks or 0))
            summary.append((variant, cls, len(ad_ids), ad_impr))

    conn.close()

    # ---- write the serve table ----
    out = os.path.join(EXPORT_DIR, "brand_serve_split_%s.csv" % stamp)
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["variant", "ad_group_class", "field_type", "text",
                    "asset_impressions", "class_ad_impressions", "shown_on_pct",
                    "asset_clicks"])
        for variant, cls, ft, text, impr, clicks in sorted(
                serve_rows, key=lambda r: (r[0], r[1], r[2], -r[4])):
            d = denom.get((variant, cls), 0) or 1
            w.writerow([variant, cls, ft, text, impr, denom.get((variant, cls), 0),
                        round(100.0 * impr / d, 1), clicks])

    audit = os.path.join(EXPORT_DIR, "brand_split_adgroup_classes_%s.csv" % stamp)
    with open(audit, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["ad_group_id", "ad_group_name", "class"])
        for ag, (name, cls) in sorted(ag_class.items(), key=lambda kv: kv[1][1]):
            w.writerow([ag, name, cls])

    # ---- console summary: headline serve, brand vs non-brand side by side ----
    print("\n[SCOPE] enabled ads, device=ALL, since %s; flagged groups excluded" % args.date_from)
    for variant, cls, n_ads, ad_impr in summary:
        print("  %-6s %-9s %3d ads, %s ad impressions" % (variant, cls, n_ads, f"{ad_impr:,}"))
    for variant in variants:
        pair = defaultdict(dict)  # text -> class -> pct
        for v, cls, ft, text, impr, clicks in serve_rows:
            if v == variant and ft == "HEADLINE":
                d = denom.get((v, cls), 0) or 1
                pair[text][cls] = 100.0 * impr / d
        if not pair:
            continue
        print("\n%s HEADLINES -- shown-on%% (brand | non-brand):" % variant)
        for text in sorted(pair, key=lambda t: -pair[t].get("brand", 0)):
            b = pair[text].get("brand")
            nb = pair[text].get("non-brand")
            print("  %6s | %6s   %s"
                  % ("%.1f" % b if b is not None else "--",
                     "%.1f" % nb if nb is not None else "--", text[:58]))
    print("\n[DONE] %s" % out)
    print("[DONE] %s (classification audit)" % audit)


if __name__ == "__main__":
    main()
