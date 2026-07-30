#!/usr/bin/env python
"""
export_bcc01_pause_ranking.py -- ad_assets/analysis/ -- the bulk-import
thread's ranked pause input: EVERY enabled RSA in the BCC-01 target ad
groups, with performance over a lookback window and a within-group rank.

Grain: rsa_ad_performance_daily (additive ad grain -- the only valid grain
for ranking ads). Rank basis: conversions_value within the group (volume-
weighted, robust to thin cells); vpc/cvr/clicks included so a human can
override, plus a confidence tier (HIGH >=300 clicks / MED 100-299 / LOW
<100) -- LOW-confidence ranks are inventory, not verdicts.

is_dki_incumbent marks the compliance-mandated pauses (V1 fdb8e75afa /
V3 9265069966): whatever the wave order, THOSE must be paused by the end --
a DKI ad winning its group on performance does not exempt it.

Output: ad_assets\\exports\\bcc01_pause_ranking_<date>.csv
Also prints: RSAs-per-group distribution (verifies the 2-per-group
assumption), groups where the DKI incumbent is the BETTER performer, and
thin-data groups.

READ-ONLY.

  python export_bcc01_pause_ranking.py --db "...\\paid_search.db" ^
      --targets-csv "...\\exports\\bcc01_target_adgroups_2026-07-23.csv" --days 30
"""
import argparse
import csv
import datetime
import os
import sqlite3
import sys
from collections import defaultdict

ACCOUNT_ID = 1
DKI_FPS = {"fdb8e75afa": "V1", "9265069966": "V3"}
EXPORT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "exports")


def tier(clicks):
    return "HIGH" if clicks >= 300 else ("MEDIUM" if clicks >= 100 else "LOW")


def main():
    p = argparse.ArgumentParser(description="Ranked pause list for the BCC-01 phased load.")
    p.add_argument("--db", required=True)
    p.add_argument("--targets-csv", required=True)
    p.add_argument("--days", type=int, default=30)
    args = p.parse_args()
    for f in (args.db, args.targets_csv):
        if not os.path.isfile(f):
            sys.exit("[ABORT] not found: %s" % f)
    os.makedirs(EXPORT_DIR, exist_ok=True)

    with open(args.targets_csv, newline="", encoding="utf-8") as f:
        targets = {int(r["ad_group_id"]): (r.get("ad_group_name", ""),
                                           r.get("flagged_geico_sf", ""))
                   for r in csv.DictReader(f)}

    date_from = (datetime.date.today() - datetime.timedelta(days=args.days)).isoformat()
    conn = sqlite3.connect("file:%s?mode=ro" % args.db.replace("\\", "/"), uri=True)
    marks = ",".join("?" * len(targets))
    rows = conn.execute(
        f"""SELECT r.ad_id, r.ad_group_id, r.campaign_id, r.fingerprint,
                   COALESCE(r.variant_label, r.fingerprint) AS variant,
                   COALESCE(SUM(p.impressions),0), COALESCE(SUM(p.clicks),0),
                   COALESCE(SUM(p.conversions),0), COALESCE(SUM(p.conversions_value),0)
            FROM rsa_ads r
            LEFT JOIN rsa_ad_performance_daily p
              ON p.account_id=r.account_id AND p.ad_id=r.ad_id
             AND p.device='ALL' AND p.report_date >= ?
            WHERE r.account_id=? AND r.ad_status='ENABLED'
              AND r.ad_group_id IN ({marks})
            GROUP BY r.ad_id""",
        [date_from, ACCOUNT_ID] + list(targets)).fetchall()
    conn.close()

    by_group = defaultdict(list)
    for ad_id, ag, camp, fp, variant, impr, clicks, conv, value in rows:
        by_group[ag].append((ad_id, camp, fp, variant, impr, clicks, conv, value))

    stamp = datetime.date.today().isoformat()
    out = os.path.join(EXPORT_DIR, "bcc01_pause_ranking_%s.csv" % stamp)
    dki_wins, thin_groups = [], []
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["ad_group_id", "ad_group_name", "flagged_geico_sf", "campaign_id",
                    "ad_id", "variant", "fingerprint", "is_dki_incumbent",
                    "lookback_days", "impressions", "clicks", "conversions", "value",
                    "cvr_pct", "vpc", "rank_in_group_by_value", "confidence"])
        for ag in sorted(by_group):
            ads = sorted(by_group[ag], key=lambda a: -a[7])  # value desc
            if all(a[5] < 100 for a in ads):
                thin_groups.append(ag)
            if ads and ads[0][2] in DKI_FPS and len(ads) > 1:
                dki_wins.append((ag, ads[0][3]))
            for rank, (ad_id, camp, fp, variant, impr, clicks, conv, value) in enumerate(ads, 1):
                name, flagged = targets[ag]
                w.writerow([ag, name, flagged, camp, ad_id, variant, fp,
                            "YES" if fp in DKI_FPS else "no", args.days,
                            impr, clicks, round(conv, 1), round(value, 0),
                            round(100.0 * conv / clicks, 1) if clicks else "",
                            round(value / clicks, 2) if clicks else "",
                            rank, tier(clicks)])

    dist = defaultdict(int)
    for ads in by_group.values():
        dist[len(ads)] += 1
    print("[DONE] %d ad(s) across %d target group(s) -> %s"
          % (sum(len(v) for v in by_group.values()), len(by_group), out))
    print("[DIST] enabled RSAs per group: %s"
          % ", ".join("%d group(s) with %d RSA(s)" % (n, k) for k, n in sorted(dist.items())))
    missing = set(targets) - set(by_group)
    if missing:
        print("[CHECK] %d target group(s) with NO enabled RSA rows -- reconcile: %s"
              % (len(missing), sorted(missing)[:10]))
    if dki_wins:
        print("[NOTE] in %d group(s) the DKI incumbent is the BETTER performer by value "
              "(e.g. %s) -- it still must be paused by end state; only the wave "
              "order changes." % (len(dki_wins), dki_wins[:5]))
    if thin_groups:
        print("[NOTE] %d group(s) have <100 clicks on every ad in %dd -- ranks there "
              "are LOW confidence; widen --days or rank by clicks for those."
              % (len(thin_groups), args.days))


if __name__ == "__main__":
    main()
