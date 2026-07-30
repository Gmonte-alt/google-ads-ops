#!/usr/bin/env python
"""
sibling_comparison.py -- ad_assets/analysis/ -- the same-auction test:
compare a focal variant against the OTHER variants inside its own ad groups.

Why this exists: campaign-level readouts control for tier but not for ad-group
placement (a variant living in 6 handpicked ad groups vs an incumbent averaged
over ~70). Restricting to the focal variant's own ad groups pits it against
its literal siblings -- same keywords, same auctions, same landing pages,
rotation as the only allocator. Strongest available comparison short of a
controlled experiment.

Grain: rsa_ad_performance_daily (additive ad grain) -- valid to sum.
Significance: two-proportion z-test vs the focal variant (approximate --
Google conversions are fractional; treat |z| >= 2 as meaningful, not gospel).

READ-ONLY.

  python sibling_comparison.py --db "...\\paid_search.db"                     # focal V4, YTD
  python sibling_comparison.py --db "...\\paid_search.db" --variant V18 --from 2026-06-08
"""
import argparse
import math
import os
import sqlite3
import sys
from collections import defaultdict

ACCOUNT_ID = 1


def ztest(c1, n1, c2, n2):
    """Two-proportion z: focal (c1/n1) vs other (c2/n2). Approximate."""
    if not n1 or not n2:
        return None
    p1, p2 = c1 / n1, c2 / n2
    p = (c1 + c2) / (n1 + n2)
    se = math.sqrt(max(p * (1 - p), 1e-12) * (1 / n1 + 1 / n2))
    return (p1 - p2) / se if se else None


def main():
    p = argparse.ArgumentParser(description="Same-auction sibling comparison (additive grain).")
    p.add_argument("--db", required=True)
    p.add_argument("--variant", default="V4", help="Focal variant (default V4)")
    p.add_argument("--from", dest="date_from", default="2026-01-01", metavar="YYYY-MM-DD")
    p.add_argument("--to", dest="date_to", default=None, metavar="YYYY-MM-DD",
                   help="Window end (default: yesterday). Use for common-window "
                        "comparisons when siblings served different periods.")
    p.add_argument("--campaigns", default="",
                   help="Comma-separated campaign_ids: restrict the focal variant's "
                        "ad groups to these campaigns (e.g. the test campaigns)")
    p.add_argument("--floor", type=int, default=100,
                   help="Min clicks for a variant to appear in the overall table (default 100)")
    args = p.parse_args()

    if not os.path.isfile(args.db):
        sys.exit("[ABORT] database not found: %s" % args.db)
    conn = sqlite3.connect("file:%s?mode=ro" % args.db.replace("\\", "/"), uri=True)

    camp_filter = ""
    camp_params = []
    if args.campaigns.strip():
        ids = [int(c) for c in args.campaigns.split(",") if c.strip()]
        camp_filter = " AND campaign_id IN (%s)" % ",".join("?" * len(ids))
        camp_params = ids

    groups = [r[0] for r in conn.execute(
        f"""SELECT DISTINCT ad_group_id FROM rsa_ads
            WHERE account_id=? AND COALESCE(variant_label, fingerprint)=?{camp_filter}""",
        [ACCOUNT_ID, args.variant] + camp_params).fetchall()]
    if not groups:
        sys.exit("No ads found for variant %s -- check the label (tagged via "
                 "variant_campaign_readout.py --tag-from-csv?)." % args.variant)
    marks = ",".join("?" * len(groups))

    import datetime as _dt
    date_to = args.date_to or (_dt.date.today() - _dt.timedelta(days=1)).isoformat()

    rows = conn.execute(
        f"""SELECT COALESCE(r.variant_label, r.fingerprint) AS v,
                   r.ad_group_id, r.campaign_id,
                   SUM(p.clicks), SUM(p.conversions), SUM(p.conversions_value)
            FROM rsa_ad_performance_daily p
            JOIN rsa_ads r ON r.account_id=p.account_id AND r.ad_id=p.ad_id
            WHERE p.account_id=? AND p.device='ALL'
              AND p.report_date BETWEEN ? AND ?
              AND r.ad_group_id IN ({marks})
            GROUP BY v, r.ad_group_id, r.campaign_id""",
        [ACCOUNT_ID, args.date_from, date_to] + groups).fetchall()
    conn.close()
    if not rows:
        sys.exit("No performance rows in window for %s's ad groups." % args.variant)

    # ---- overall table across the focal variant's ad groups ----
    tot = defaultdict(lambda: [0, 0.0, 0.0])
    for v, ag, camp, clicks, conv, value in rows:
        tot[v][0] += clicks or 0
        tot[v][1] += conv or 0.0
        tot[v][2] += value or 0.0
    if args.variant not in tot:
        sys.exit("Focal variant has no perf rows in the window.")
    f_clicks, f_conv, _ = tot[args.variant]

    print("SAME-AUCTION COMPARISON -- within %s's %d ad group(s), %s..%s"
          % (args.variant, len(groups), args.date_from, date_to))
    print("(additive ad grain; z vs %s, |z|>=2 ~ meaningful)\n" % args.variant)
    print("%-12s %8s %9s %7s %8s %7s" % ("variant", "clicks", "conv", "CVR%", "VPC$", "z"))
    order = sorted(tot.items(), key=lambda kv: -(kv[1][1] / kv[1][0] if kv[1][0] else 0))
    for v, (clicks, conv, value) in order:
        if clicks < args.floor and v != args.variant:
            continue
        cvr = 100.0 * conv / clicks if clicks else 0
        vpc = value / clicks if clicks else 0
        if v == args.variant:
            z_s = "--"
        else:
            z = ztest(f_conv, f_clicks, conv, clicks)
            z_s = "%+.1f" % z if z is not None else "n/a"
        mark = " <-- focal" if v == args.variant else ""
        print("%-12s %8d %9.1f %7.1f %8.2f %7s%s" % (v, clicks, conv, cvr, vpc, z_s, mark))

    # ---- per-ad-group head-to-head (consistency check) ----
    print("\nPER AD GROUP (focal vs best-volume sibling in each):")
    print("%-14s %-14s %22s %22s" % ("ad_group", "campaign", args.variant, "top sibling"))
    per_ag = defaultdict(dict)
    ag_camp = {}
    for v, ag, camp, clicks, conv, value in rows:
        per_ag[ag][v] = (clicks or 0, conv or 0.0)
        ag_camp[ag] = camp
    focal_wins = 0
    counted = 0
    for ag in sorted(per_ag, key=lambda a: -(per_ag[a].get(args.variant, (0, 0))[0])):
        cell = per_ag[ag]
        if args.variant not in cell:
            continue
        fc, fv = cell[args.variant]
        sibs = {v: t for v, t in cell.items() if v != args.variant}
        if not sibs:
            print("%-14s %-14s %15d clk %4.1f%%   (no sibling with traffic)"
                  % (ag, ag_camp[ag], fc, 100.0 * fv / fc if fc else 0))
            continue
        sv = max(sibs, key=lambda v: sibs[v][0])
        sc, sconv = sibs[sv]
        f_cvr = 100.0 * fv / fc if fc else 0
        s_cvr = 100.0 * sconv / sc if sc else 0
        if fc >= 50 and sc >= 50:
            counted += 1
            focal_wins += 1 if f_cvr > s_cvr else 0
        print("%-14s %-14s %8d clk %5.1f%%   %s: %6d clk %5.1f%%"
              % (ag, ag_camp[ag], fc, f_cvr, sv, sc, s_cvr))
    if counted:
        print("\n%s leads its top sibling in %d of %d ad groups with >=50 clicks each side."
              % (args.variant, focal_wins, counted))
    print("\nRead: if the focal variant's edge holds HERE, it survives the ad-group "
          "placement confound. If it collapses, the campaign-level premium was placement.")


if __name__ == "__main__":
    main()
