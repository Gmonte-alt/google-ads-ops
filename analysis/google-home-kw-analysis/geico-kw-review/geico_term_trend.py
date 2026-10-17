#!/usr/bin/env python3
"""Monthly funnel + economics trend for a search term (default: geico) from paid_search.db.

Sources search_query_performance (Search campaigns; PMax-served traffic is NOT in this table) joined to
search_queries for text, gated to Google Home (platform='google' AND campaigns.account_id=1).

Funnel mapping (printed in the banner; flag if wrong):
  zips  = secondary_conv_count   (zip submits)
  leads = primary_conv_count     (sold leads / primary conversions)
  click->zip = zips/clicks ; zip->lead = leads/zips ; click->lead = leads/clicks
Economics: CPC, CTR, CPL (cost/leads), ROAS (value/cost), GP (value-cost), GP/lead.
'all_conv per lead' = all_conv_count/leads (the conversions-per-lead read).

Outputs (to --out-dir):
  term_trend_monthly.csv   -- exact term, one row per month
  family_trend_monthly.csv -- all queries CONTAINING the term, one row per month
  family_variants.csv      -- per-variant totals for two comparison windows (prior vs current year)
Caveat: Google's search-term privacy thresholds can suppress low-volume rows at daily grain, so
low-volume months may undercount slightly; 'geico' exact is high-volume enough to be robust.

Run:
  python geico_term_trend.py --db C:\\Projects\\google-ads-ops\\db\\paid_search.db
  python geico_term_trend.py --db ... --term "state farm" --start 2025-01-01 --end 2026-08-10
"""
import argparse
import csv
import os
import sqlite3

ACCOUNT_ID = 1


def rate(n, d):
    return (n / d) if d else 0.0


def emit_rows(conn, where_sql, params, label):
    q = ("SELECT SUBSTR(p.report_date,1,7) AS ym, SUM(p.impressions) AS impr, SUM(p.clicks) AS clicks, "
         "SUM(p.cost_micros)/1000000.0 AS cost, SUM(p.secondary_conv_count) AS zips, "
         "SUM(p.primary_conv_count) AS leads, SUM(p.primary_conv_value) AS value, "
         "SUM(p.all_conv_count) AS all_conv "
         "FROM search_query_performance p "
         "JOIN search_queries sq ON sq.id = p.query_id "
         "JOIN campaigns c ON c.campaign_id = p.campaign_id AND c.account_id = ? "
         "WHERE p.platform='google' AND p.report_date >= ? AND p.report_date <= ? AND " + where_sql +
         " GROUP BY 1 ORDER BY 1")
    out = []
    for ym, impr, clicks, cost, zips, leads, value, all_conv in conn.execute(q, ([ACCOUNT_ID] + params)):
        impr = impr or 0; clicks = clicks or 0; cost = cost or 0.0
        zips = zips or 0.0; leads = leads or 0.0; value = value or 0.0; all_conv = all_conv or 0.0
        out.append(dict(month=ym, impressions=int(impr), clicks=int(clicks),
                        ctr=round(rate(clicks, impr), 4), cpc=round(rate(cost, clicks), 2),
                        cost=round(cost, 2), zips=round(zips, 1), leads=round(leads, 1),
                        click_to_zip=round(rate(zips, clicks), 4), zip_to_lead=round(rate(leads, zips), 4),
                        click_to_lead=round(rate(leads, clicks), 4),
                        all_conv_per_lead=round(rate(all_conv, leads), 2),
                        value=round(value, 2), roas=round(rate(value, cost), 2),
                        gp=round(value - cost, 2), gp_per_lead=round(rate(value - cost, leads), 2),
                        cpl=round(rate(cost, leads), 2)))
    return out


def write(path, rows):
    if not rows:
        print("  [WARN] no rows for %s" % path)
        return
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print("  wrote %s (%d rows)" % (path, len(rows)))


def main():
    ap = argparse.ArgumentParser(description="Monthly funnel/economics trend for a search term")
    ap.add_argument("--db", required=True)
    ap.add_argument("--term", default="geico")
    ap.add_argument("--start", default="2025-01-01")
    ap.add_argument("--end", default="2026-12-31")
    ap.add_argument("--out-dir", default="term_trend_output")
    a = ap.parse_args()
    term = a.term.strip().lower()
    conn = sqlite3.connect("file:%s?mode=ro" % a.db, uri=True)
    os.makedirs(a.out_dir, exist_ok=True)

    print("FUNNEL MAPPING: zips=secondary_conv_count, leads=primary_conv_count "
          "(click->zip=zips/clicks, zip->lead=leads/zips, click->lead=leads/clicks)")
    print("SOURCE: search_query_performance (Search only; PMax-served traffic not included)")
    maxd = conn.execute("SELECT MAX(report_date) FROM search_query_performance "
                        "WHERE platform='google'").fetchone()[0]
    print("data through: %s | window: %s..%s | term: %r\n" % (maxd, a.start, a.end, term))

    # 1) exact term
    exact = emit_rows(conn, "LOWER(TRIM(sq.query_text)) = ?", [a.start, a.end, term], "exact")
    write(os.path.join(a.out_dir, "term_trend_monthly.csv"), exact)

    # 2) family (queries containing the term)
    fam = emit_rows(conn, "LOWER(sq.query_text) LIKE ?", [a.start, a.end, "%" + term + "%"], "family")
    write(os.path.join(a.out_dir, "family_trend_monthly.csv"), fam)

    # 3) per-variant totals, split at the year boundary for a prior-vs-current compare
    split = "2026-01-01"
    q = ("SELECT LOWER(TRIM(sq.query_text)) AS t, "
         "SUM(CASE WHEN p.report_date <  ? THEN p.primary_conv_count ELSE 0 END) AS leads_prior, "
         "SUM(CASE WHEN p.report_date >= ? THEN p.primary_conv_count ELSE 0 END) AS leads_cur, "
         "SUM(CASE WHEN p.report_date <  ? THEN p.primary_conv_value - p.cost_micros/1000000.0 ELSE 0 END) AS gp_prior, "
         "SUM(CASE WHEN p.report_date >= ? THEN p.primary_conv_value - p.cost_micros/1000000.0 ELSE 0 END) AS gp_cur, "
         "SUM(p.clicks) AS clicks "
         "FROM search_query_performance p JOIN search_queries sq ON sq.id=p.query_id "
         "JOIN campaigns c ON c.campaign_id=p.campaign_id AND c.account_id=? "
         "WHERE p.platform='google' AND p.report_date >= ? AND p.report_date <= ? "
         "AND LOWER(sq.query_text) LIKE ? "
         "GROUP BY 1 HAVING SUM(p.clicks) >= 10 ORDER BY leads_prior DESC")
    var_rows = []
    for t, lp, lc, gpp, gpc, clicks in conn.execute(
            q, (split, split, split, split, ACCOUNT_ID, a.start, a.end, "%" + term + "%")):
        var_rows.append(dict(query=t, leads_prior=round(lp or 0, 1), leads_current=round(lc or 0, 1),
                             gp_prior=round(gpp or 0), gp_current=round(gpc or 0),
                             gp_change=round((gpc or 0) - (gpp or 0)), clicks_total=int(clicks or 0)))
    write(os.path.join(a.out_dir, "family_variants.csv"), var_rows)

    # console summary: exact-term trend at a glance
    if exact:
        print("\n%-8s %7s %7s %6s %6s %6s %6s %7s %7s %7s %7s %7s" % (
            "month", "impr", "clicks", "CTR", "CPC", "zips", "leads", "c->zip", "z->lead", "CPL", "ROAS", "GP"))
        for r in exact:
            print("%-8s %7d %7d %5.1f%% %6.2f %6.0f %6.0f %6.1f%% %6.1f%% %7.0f %6.2fx %7.0f" % (
                r["month"], r["impressions"], r["clicks"], r["ctr"] * 100, r["cpc"], r["zips"], r["leads"],
                r["click_to_zip"] * 100, r["zip_to_lead"] * 100, r["cpl"], r["roas"], r["gp"]))
    conn.close()
    print("\nwrote -> %s" % os.path.abspath(a.out_dir))


if __name__ == "__main__":
    main()
