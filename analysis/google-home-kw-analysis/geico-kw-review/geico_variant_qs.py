#!/usr/bin/env python3
"""Geico QS by keyword VARIANT: is the segment iw-QS story driven by 'geico insurance'
rather than bare 'geico'?

Buckets (keyword_text x match class):
  GEICO|EXACT, GEICO|BROAD_PHRASE            -- bare brand
  GEICO_INS|EXACT, GEICO_INS|BROAD_PHRASE    -- 'geico insurance'
  HOME                                        -- geico + home/homeowner/house product terms
  OTHER                                       -- remaining geico keywords (conquest tails etc.)

Outputs (--out-dir):
  geico_variant_monthly.csv  per month x bucket: impressions, rated share, iw-QS,
                             pred_ctr above/below shares, creative above share,
                             + bucket's share of ALL rated geico impressions that month
  console: weighting summary for --recent window (who carries the segment average)

Iron rules: QS never summed (iw only, weights = kpd ALL impressions); unrated excluded,
rated_share reported; monthly grain. Same joins as qs_analysis.py.

Run:
  python geico_variant_qs.py --db C:\\Projects\\google-ads-ops\\db\\paid_search.db
"""
import argparse, csv, os, re, sqlite3
from collections import defaultdict

ACCOUNT_ID = 1
PRODUCT = re.compile(r"\b(home|homeowner|homeowners|house)\b")


def bucket(kw, mt):
    k = kw.lower().strip()
    m = "EXACT" if (mt or "").upper() == "EXACT" else "BROAD_PHRASE"
    if PRODUCT.search(k):
        return "HOME"
    if k == "geico":
        return "GEICO|" + m
    if k == "geico insurance":
        return "GEICO_INS|" + m
    return "OTHER"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--recent", default="2025-08-01", help="start of window for the weighting summary")
    ap.add_argument("--out-dir", default=r"C:\Projects\google-ads-ops\analysis\google-home-kw-analysis\geico-kw-review\output")
    a = ap.parse_args()
    conn = sqlite3.connect("file:%s?mode=ro" % a.db, uri=True)
    os.makedirs(a.out_dir, exist_ok=True)

    q = ("SELECT SUBSTR(p.report_date,1,7) ym, LOWER(ki.keyword_text) kw, ki.match_type mt, "
         " SUM(p.impressions) impr, "
         " SUM(CASE WHEN q.historical_quality_score IS NOT NULL THEN p.impressions ELSE 0 END) ri, "
         " SUM(CASE WHEN q.historical_quality_score IS NOT NULL THEN q.historical_quality_score*p.impressions END) qsw, "
         " SUM(CASE WHEN q.hist_predicted_ctr='ABOVE_AVERAGE' THEN p.impressions ELSE 0 END) ctr_a, "
         " SUM(CASE WHEN q.hist_predicted_ctr='BELOW_AVERAGE' THEN p.impressions ELSE 0 END) ctr_b, "
         " SUM(CASE WHEN q.hist_creative_quality='ABOVE_AVERAGE' THEN p.impressions ELSE 0 END) cr_a "
         " FROM keyword_performance_daily p "
         " JOIN keyword_instances ki ON ki.id = p.instance_id AND ki.account_id = ? "
         " LEFT JOIN keyword_quality_daily q ON q.instance_id = p.instance_id AND q.report_date = p.report_date "
         " WHERE p.device = 'ALL' AND LOWER(ki.keyword_text) LIKE '%geico%' "
         " GROUP BY 1,2,3")
    agg = defaultdict(lambda: dict(impr=0, ri=0, qsw=0.0, ca=0, cb=0, cra=0))
    for ym, kw, mt, impr, ri, qsw, ca, cb, cra in conn.execute(q, (ACCOUNT_ID,)):
        x = agg[(ym, bucket(kw, mt))]
        x["impr"] += impr or 0; x["ri"] += ri or 0; x["qsw"] += qsw or 0
        x["ca"] += ca or 0; x["cb"] += cb or 0; x["cra"] += cra or 0

    month_ri = defaultdict(int)
    for (ym, b), x in agg.items():
        month_ri[ym] += x["ri"]

    rows = []
    for (ym, b), x in sorted(agg.items()):
        ri = x["ri"]
        rows.append([ym, b, x["impr"], ri,
                     round(ri / x["impr"], 3) if x["impr"] else 0,
                     round(x["qsw"] / ri, 2) if ri else "",
                     round(x["ca"] / ri, 3) if ri else "", round(x["cb"] / ri, 3) if ri else "",
                     round(x["cra"] / ri, 3) if ri else "",
                     round(ri / month_ri[ym], 3) if month_ri[ym] else 0])
    path = os.path.join(a.out_dir, "geico_variant_monthly.csv")
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["month", "bucket", "impressions", "rated_impressions", "rated_share", "iw_qs",
                    "pred_ctr_above_share", "pred_ctr_below_share", "creative_above_share",
                    "share_of_geico_rated_impr"])
        w.writerows(rows)
    print("wrote %s (%d rows)" % (path, len(rows)))

    # weighting summary: who carries the segment average in the recent window
    tot = defaultdict(lambda: dict(ri=0, qsw=0.0))
    for (ym, b), x in agg.items():
        if ym >= a.recent[:7]:
            tot[b]["ri"] += x["ri"]; tot[b]["qsw"] += x["qsw"]
    all_ri = sum(t["ri"] for t in tot.values())
    print("\nweighting summary since %s (share of rated geico impressions | iw-QS):" % a.recent)
    for b, t in sorted(tot.items(), key=lambda kv: -kv[1]["ri"]):
        print("  %-22s %6.1f%%   iw-QS %s" % (
            b, 100.0 * t["ri"] / all_ri if all_ri else 0,
            ("%.2f" % (t["qsw"] / t["ri"])) if t["ri"] else "unrated"))
    seg = sum(t["qsw"] for t in tot.values()) / all_ri if all_ri else 0
    print("  %-22s %6s     iw-QS %.2f" % ("SEGMENT (all geico)", "100%", seg))
    conn.close()


if __name__ == "__main__":
    main()
