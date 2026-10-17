"""
qs_cohort_monthly.py -- canonical monthly QS aggregates for a cohort (Geico), computed
EXACTLY per the DB-architect gate-set so this thread and the QS-analysis thread reconcile.

DRAFT FOR ARCHITECT GATE REVIEW. Definitions (do not vary):
  rated_share          = SUM(impr on kw-days WITH a kqd row) / SUM(all impr)      [LEFT JOIN kpd->kqd]
  iw_QS                = SUM(historical_quality_score * impr) / SUM(impr)          [rated rows only]
  pred_ctr_below_share = SUM(impr where hist_predicted_ctr='BELOW_AVERAGE') / SUM(rated impr)

Join recipe (id-set-first):
  Geico ad_group id-set  := ad_groups.ad_group_id where lower(ad_group_name) LIKE '%geico%', acct 1
  cohort instance-set    := keyword_instances.instance_id where ad_group_id IN id-set, ki.account_id=1
  weights                := keyword_performance_daily, device='ALL', instance_id IN instance-set
  ratings (LEFT JOIN)    := keyword_quality_daily on (instance_id, report_date)
  grain                  := MONTH (daily QS moves slowly, daily cells go thin)

Rules: QS never SUMmed; NULL/absent kqd row = UNRATED (excluded from rated aggregates, but its
impressions COUNT in the rated_share denominator). Plot rated_share beside iw_QS (composition guard).

    python qs_cohort_monthly.py --db PATH --brand Geico            # prints monthly table + CSV
"""
import argparse
import csv
import sqlite3


def monthly_qs(conn, brand, account):
    """Monthly canonical QS aggregates for the keyword-text cohort (matches qs_analysis.py):
    cohort = LOWER(ki.keyword_text) LIKE '%brand%'. Join recipe mirrors the working script:
      keyword_performance_daily p  (device='ALL')
      JOIN keyword_instances ki ON ki.id = p.instance_id AND ki.account_id=?
      LEFT JOIN keyword_quality_daily q ON q.instance_id=p.instance_id AND q.report_date=p.report_date
    rated = q.historical_quality_score IS NOT NULL (the LEFT-JOIN miss = unrated)."""
    sql = ("""
        SELECT substr(p.report_date,1,7) AS ym,
               SUM(p.impressions) AS all_impr,
               SUM(CASE WHEN q.historical_quality_score IS NOT NULL THEN p.impressions ELSE 0 END) AS rated_impr,
               SUM(CASE WHEN q.historical_quality_score IS NOT NULL
                        THEN q.historical_quality_score*p.impressions ELSE 0 END) AS qs_x_impr,
               SUM(CASE WHEN q.hist_predicted_ctr='BELOW_AVERAGE' THEN p.impressions ELSE 0 END) AS below_impr,
               SUM(CASE WHEN q.hist_predicted_ctr='ABOVE_AVERAGE' THEN p.impressions ELSE 0 END) AS above_impr
        FROM keyword_performance_daily p
        JOIN keyword_instances ki ON ki.id = p.instance_id AND ki.account_id = ?
        LEFT JOIN keyword_quality_daily q
               ON q.instance_id = p.instance_id AND q.report_date = p.report_date
        WHERE p.device='ALL' AND LOWER(ki.keyword_text) LIKE ?
        GROUP BY ym ORDER BY ym
    """)
    rows = []
    for ym, all_impr, rated_impr, qs_x_impr, below_impr, above_impr in conn.execute(
            sql, (account, "%" + brand.lower() + "%")):
        all_impr = all_impr or 0; rated_impr = rated_impr or 0
        rows.append({
            "month": ym, "all_impr": all_impr, "rated_impr": rated_impr,
            "rated_share": round(rated_impr / all_impr, 4) if all_impr else None,
            "iw_QS": round(qs_x_impr / rated_impr, 3) if rated_impr else None,
            "pred_ctr_below_share": round(below_impr / rated_impr, 4) if rated_impr else None,
            "pred_ctr_above_share": round(above_impr / rated_impr, 4) if rated_impr else None,
        })
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--brand", default="Geico")
    ap.add_argument("--account", type=int, default=1)
    ap.add_argument("--csv", default=None)
    args = ap.parse_args()
    conn = sqlite3.connect(args.db)
    rows = monthly_qs(conn, args.brand, args.account)
    print("cohort = keywords containing %r (keyword-text match, per qs_analysis.py)" % args.brand)
    print("%-8s %10s %10s %10s %7s %8s %8s" % ("MONTH","all_impr","rated_imp","rated_shr","iw_QS","below","above"))
    for r in rows:
        print("%-8s %10d %10d %10s %7s %8s %8s" % (
            r["month"], r["all_impr"], r["rated_impr"], r["rated_share"],
            r["iw_QS"], r["pred_ctr_below_share"], r["pred_ctr_above_share"]))
    if args.csv:
        with open(args.csv, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader(); w.writerows(rows)
        print("[ok] wrote %s" % args.csv)
    conn.close()


if __name__ == "__main__":
    main()
