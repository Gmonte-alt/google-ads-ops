#!/usr/bin/env python3
"""Quality Score x Performance analysis, Google Home (account_id=1), per the QS charter.

Sources: keyword_quality_daily (QS; row exists only for RATED keyword-days) LEFT-JOINED FROM
keyword_performance_daily (device='ALL' rows only; kpd's own QS-named columns are NULL -- unused),
scoped via keyword_instances.account_id=1, cut via keyword_classification (ad_group_criterion_id).

Iron rules implemented:
  - QS is NEVER summed; every aggregate is the impression-weighted average (weights = kpd ALL impressions)
  - unrated days are excluded from averages, never coerced to zero; rated share is reported alongside
  - ratios from summed numerators; monthly grain

Outputs (--out-dir):
  q1_matchtype_monthly.csv   EXACT vs BROAD/PHRASE: iw-QS + component ABOVE_AVERAGE shares + rated share
  q2_drift_decomposition.csv era A vs era B overall + common-instance set -> composition vs within verdict
  q3_qs_band_economics.csv   CPC / CvR / CPL by QS band (click floor)
  q4_lp_fixlist.csv          BELOW_AVERAGE landing-page placements with material recent spend (+brand/group)
  q5_statefarm_profile.csv   QS profile of surviving State Farm placements
  q6_geico_qs_monthly.csv    geico keywords: monthly iw-QS + predicted-CTR bucket shares (cliff tie-in)

Run:
  python qs_analysis.py --db C:\\Projects\\google-ads-ops\\db\\paid_search.db
"""
import argparse
import csv
import os
import sqlite3

ACCOUNT_ID = 1
DEFAULT_OUT = r"C:\Projects\google-ads-ops\analysis\google-home-kw-analysis\geico-kw-review\output"
FNAME_SUFFIX = ""  # set from --term / --exclude-term so scoped runs are self-describing


def _slug(t):
    return t.strip().lower().replace(" ", "-")


def out_path(out, base, extra=""):
    """qN_name.csv -> qN_name__term-geico[__q2excl-x].csv when scoping params are set."""
    stem, dot, ext = base.rpartition(".")
    return os.path.join(out, "%s%s%s.%s" % (stem, FNAME_SUFFIX, extra, ext))


def check_schema(conn):
    need = {
        "keyword_quality_daily": ["instance_id", "report_date", "historical_quality_score",
                                  "hist_creative_quality", "hist_landing_page_quality", "hist_predicted_ctr"],
        "keyword_performance_daily": ["instance_id", "report_date", "device", "impressions", "clicks",
                                      "cost_micros", "primary_conv_count", "primary_conv_value"],
        "keyword_instances": ["id", "account_id", "keyword_text", "match_type", "ad_group_id", "criterion_id", "ad_group_criterion_id"],
        "keyword_classification": ["account_id", "ad_group_criterion_id", "brand_name", "query_group", "category"],
    }
    for t, cols in need.items():
        have = [r[1] for r in conn.execute("PRAGMA table_info(%s)" % t)]
        if not have:
            raise SystemExit("Missing table %r" % t)
        miss = [c for c in cols if c not in have]
        if miss:
            raise SystemExit("Table %s missing %s. Has: %s" % (t, miss, ", ".join(have)))
    print("SCHEMA OK: kqd + kpd (device=ALL) + ki + classification")
    # Governance rule (DB architect): classification tables are point-in-time rebuild artifacts.
    # If any keyword instance is newer than the last classification rebuild, segment cuts (q4/q7)
    # put those placements in GENERIC/blank -> warn loudly before anyone reads them.
    ki_cols = [r[1] for r in conn.execute("PRAGMA table_info(keyword_instances)")]
    kc_cols = [r[1] for r in conn.execute("PRAGMA table_info(keyword_classification)")]
    if "first_seen_date" in ki_cols and "classified_at" in kc_cols:
        mx_ki = conn.execute("SELECT MAX(first_seen_date) FROM keyword_instances WHERE account_id=%d"
                             % ACCOUNT_ID).fetchone()[0]
        mx_kc = conn.execute("SELECT MAX(classified_at) FROM keyword_classification").fetchone()[0]
        if mx_ki and mx_kc and str(mx_ki)[:10] > str(mx_kc)[:10]:
            print("  [STALENESS WARNING] keyword_instances has placements newer (%s) than the last "
                  "classification rebuild (%s) -> q4/q7 segment cuts will misfile them as GENERIC/blank. "
                  "Rebuild keyword_classification first." % (mx_ki, mx_kc))
        else:
            print("  classification freshness OK (instances<=%s, classified_at=%s)" % (mx_ki, mx_kc))
    else:
        print("  (staleness check skipped: first_seen_date/classified_at not both present)")


BASE_FROM = (
    " FROM keyword_performance_daily p "
    " JOIN keyword_instances ki ON ki.id = p.instance_id AND ki.account_id = %d "
    " LEFT JOIN keyword_quality_daily q ON q.instance_id = p.instance_id AND q.report_date = p.report_date "
    " WHERE p.device = 'ALL' " % ACCOUNT_ID)


def set_term_scope(term):
    """Scope EVERY question to keywords containing the term (e.g. 'geico')."""
    global BASE_FROM
    if term:
        safe = term.strip().lower().replace("'", "")
        BASE_FROM += " AND LOWER(ki.keyword_text) LIKE '%" + safe + "%' "


def write(path, header, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)
    print("  wrote %s (%d rows)" % (os.path.basename(path), len(rows)))


def q1_matchtype_monthly(conn, out):
    q = ("SELECT SUBSTR(p.report_date,1,7) ym, "
         " CASE WHEN UPPER(ki.match_type)='EXACT' THEN 'EXACT' ELSE 'BROAD_PHRASE' END mt, "
         " SUM(p.impressions) impr, "
         " SUM(CASE WHEN q.historical_quality_score IS NOT NULL THEN p.impressions ELSE 0 END) rated_impr, "
         " SUM(CASE WHEN q.historical_quality_score IS NOT NULL THEN q.historical_quality_score*p.impressions END) qs_w, "
         " SUM(CASE WHEN q.hist_creative_quality='ABOVE_AVERAGE' THEN p.impressions ELSE 0 END) cr_above, "
         " SUM(CASE WHEN q.hist_landing_page_quality='ABOVE_AVERAGE' THEN p.impressions ELSE 0 END) lp_above, "
         " SUM(CASE WHEN q.hist_predicted_ctr='ABOVE_AVERAGE' THEN p.impressions ELSE 0 END) ctr_above "
         + BASE_FROM +
         " GROUP BY 1,2 ORDER BY 1,2")
    rows = []
    for ym, mt, impr, ri, qsw, cr, lp, ct in conn.execute(q):
        impr = impr or 0; ri = ri or 0
        rows.append([ym, mt, impr, ri,
                     round(ri / impr, 4) if impr else 0,
                     round(qsw / ri, 2) if ri else "",
                     round(cr / ri, 4) if ri else "", round(lp / ri, 4) if ri else "",
                     round(ct / ri, 4) if ri else ""])
    write(out_path(out, "q1_matchtype_monthly.csv"),
          ["month", "match_class", "impressions", "rated_impressions", "rated_share",
           "iw_qs", "creative_above_share", "lp_above_share", "pred_ctr_above_share"], rows)


def q2_drift(conn, out, a0, a1, b0, b1, exclude_term=""):
    def era(w0, w1):
        cond = " AND p.report_date >= '%s' AND p.report_date <= '%s' " % (w0, w1)
        # IDENTITY grain (keyword_text + match_type), not instance_id: the restructure replaced every
        # placement (zero instance overlap across eras), so "same keyword, new placement" is the only
        # grain that yields a real within-keyword read across the boundary.
        q = ("SELECT LOWER(TRIM(ki.keyword_text)) || '~' || UPPER(ki.match_type) ident, "
             " SUM(p.impressions) impr, "
             " SUM(CASE WHEN q.historical_quality_score IS NOT NULL THEN p.impressions ELSE 0 END) ri, "
             " SUM(q.historical_quality_score*p.impressions) qsw " + BASE_FROM + cond +
             " GROUP BY 1 HAVING ri > 0")
        d = {}
        for ident, impr, ri, qsw in conn.execute(q):
            d[ident] = (ri, qsw)
        return d

    A, B = era(a0, a1), era(b0, b1)
    excl_note = ""
    if exclude_term:
        ex = exclude_term.strip().lower()
        nA, nB = len(A), len(B)
        A = {k: v for k, v in A.items() if ex not in k}
        B = {k: v for k, v in B.items() if ex not in k}
        excl_note = " [excluding %r: dropped %d/%d era-A and %d/%d era-B identities]" % (
            exclude_term, nA - len(A), nA, nB - len(B), nB)
        print("  Q2 exclusion:%s" % excl_note)
    common = set(A) & set(B)

    def iwqs(d, keys=None):
        ks = keys if keys is not None else d.keys()
        ri = sum(d[k][0] for k in ks)
        qw = sum(d[k][1] for k in ks)
        return (qw / ri if ri else 0), ri

    qa, ra = iwqs(A); qb, rb = iwqs(B)
    qa_c, _ = iwqs(A, common); qb_c, _ = iwqs(B, common)
    overall = qb - qa
    within = qb_c - qa_c
    composition = overall - within
    if abs(overall) < 0.1:
        verdict = "NO MATERIAL DRIFT (overall change < 0.1)"
    elif within <= composition:
        verdict = "WITHIN-KEYWORD DECLINE (account-health problem)"
    else:
        verdict = "COMPOSITION (mix of what's rated changed; benign)"
    rows = [
        ["era_A (%s..%s) overall iw-QS" % (a0, a1), round(qa, 3), "rated identities: %d" % len(A)],
        ["era_B (%s..%s) overall iw-QS" % (b0, b1), round(qb, 3), "rated identities: %d" % len(B)],
        ["overall change", round(overall, 3), ""],
        ["common identity set (text+match)", len(common), "same keyword rated in BOTH eras (any placement)"],
        ["common-set iw-QS era A", round(qa_c, 3), ""],
        ["common-set iw-QS era B", round(qb_c, 3), ""],
        ["within-keyword change", round(within, 3), "same placements, era A -> B"],
        ["composition effect", round(composition, 3), "entry/exit of rated population"],
        ["verdict (dominant driver)", verdict, ""],
    ]
    extra = ("__q2excl-" + _slug(exclude_term)) if exclude_term else ""
    write(out_path(out, "q2_drift_decomposition.csv", extra), ["metric", "value", "note"], rows)
    print("  Q2: overall %.2f -> %.2f | within=%.2f composition=%.2f -> %s"
          % (qa, qb, within, composition, verdict))


def q3_bands(conn, out, click_floor):
    q = ("SELECT q.historical_quality_score qs, SUM(p.impressions) impr, SUM(p.clicks) clicks, "
         " SUM(p.cost_micros)/1000000.0 cost, SUM(p.primary_conv_count) conv, SUM(p.primary_conv_value) val "
         + BASE_FROM + " AND q.historical_quality_score IS NOT NULL GROUP BY 1 HAVING SUM(p.clicks) >= %d "
         " ORDER BY 1" % click_floor)
    rows = []
    for qs, impr, clicks, cost, conv, val in conn.execute(q):
        rows.append([qs, impr, clicks, round(cost), round(conv, 1),
                     round(cost / clicks, 2) if clicks else 0,
                     round(conv / clicks, 4) if clicks else 0,
                     round(cost / conv, 2) if conv else "",
                     round(val / cost, 2) if cost else "",
                     round(val - cost)])
    write(out_path(out, "q3_qs_band_economics.csv"),
          ["qs", "impressions", "clicks", "cost", "leads", "CPC", "CvR_click_to_lead", "CPL", "ROAS", "GP"], rows)


def q4_lp_fixlist(conn, out, days, spend_floor):
    q = ("SELECT ki.keyword_text, ki.match_type, ki.ad_group_criterion_id agc, "
         " SUM(p.cost_micros)/1000000.0 cost, SUM(p.primary_conv_count) conv, SUM(p.primary_conv_value) val, "
         " SUM(CASE WHEN q.hist_landing_page_quality='BELOW_AVERAGE' THEN p.impressions ELSE 0 END) lp_below_impr, "
         " SUM(CASE WHEN q.historical_quality_score IS NOT NULL THEN p.impressions ELSE 0 END) rated_impr, "
         " SUM(CASE WHEN q.historical_quality_score IS NOT NULL THEN q.historical_quality_score*p.impressions END) qs_w "
         + BASE_FROM +
         " AND p.report_date >= DATE((SELECT MAX(report_date) FROM keyword_performance_daily), '-%d day') "
         " GROUP BY 1,2,3 HAVING cost >= %f AND rated_impr > 0" % (days, spend_floor))
    rows = []
    for text, mt, agc, cost, conv, val, lpb, ri, qsw in conn.execute(q):
        if not ri or (lpb / ri) < 0.5:
            continue  # majority of rated impressions BELOW_AVERAGE on landing page
        cls = conn.execute("SELECT brand_name, query_group, category FROM keyword_classification "
                           "WHERE account_id = ? AND ad_group_criterion_id = ? LIMIT 1",
                           (ACCOUNT_ID, agc)).fetchone()
        brand, group, cat = (cls or ("", "", ""))
        rows.append([text, mt, brand or "", group or "", cat or "", round(cost), round(conv, 1),
                     round(val - cost), round(qsw / ri, 2), round(lpb / ri, 3)])
    rows.sort(key=lambda r: -r[5])
    write(out_path(out, "q4_lp_fixlist.csv"),
          ["keyword", "match_type", "brand", "query_group", "category", "cost_%dd" % days,
           "leads", "GP", "iw_qs", "lp_below_share"], rows)


def q5_placements(conn, out, days, like_term):
    q = ("SELECT ki.keyword_text, ki.match_type, "
         " SUM(p.impressions) impr, SUM(p.cost_micros)/1000000.0 cost, SUM(p.primary_conv_count) conv, "
         " SUM(CASE WHEN q.historical_quality_score IS NOT NULL THEN p.impressions ELSE 0 END) ri, "
         " SUM(q.historical_quality_score*p.impressions) qsw, "
         " SUM(CASE WHEN q.hist_creative_quality='BELOW_AVERAGE' THEN p.impressions ELSE 0 END) cr_below, "
         " SUM(CASE WHEN q.hist_landing_page_quality='BELOW_AVERAGE' THEN p.impressions ELSE 0 END) lp_below, "
         " SUM(CASE WHEN q.hist_predicted_ctr='BELOW_AVERAGE' THEN p.impressions ELSE 0 END) ctr_below "
         + BASE_FROM +
         " AND LOWER(ki.keyword_text) LIKE '%" + like_term + "%' "
         " AND p.report_date >= DATE((SELECT MAX(report_date) FROM keyword_performance_daily), '-" + str(days) + " day') "
         " GROUP BY 1,2 ORDER BY impr DESC")
    rows = []
    for text, mt, impr, cost, conv, ri, qsw, crb, lpb, ctb in conn.execute(q):
        rows.append([text, mt, impr, round(cost or 0), round(conv or 0, 1),
                     round((ri or 0) / impr, 3) if impr else 0,
                     round(qsw / ri, 2) if ri else "",
                     round(crb / ri, 3) if ri else "", round(lpb / ri, 3) if ri else "",
                     round(ctb / ri, 3) if ri else ""])
    write(out_path(out, "q5_placement_profile.csv"),
          ["keyword", "match_type", "impr_%dd" % days, "cost", "leads", "rated_share", "iw_qs",
           "creative_below_share", "lp_below_share", "pred_ctr_below_share"], rows)


def q6_geico_monthly(conn, out):
    q = ("SELECT SUBSTR(p.report_date,1,7) ym, SUM(p.impressions) impr, "
         " SUM(CASE WHEN q.historical_quality_score IS NOT NULL THEN p.impressions ELSE 0 END) ri, "
         " SUM(q.historical_quality_score*p.impressions) qsw, "
         " SUM(CASE WHEN q.hist_predicted_ctr='ABOVE_AVERAGE' THEN p.impressions ELSE 0 END) ctr_above, "
         " SUM(CASE WHEN q.hist_predicted_ctr='BELOW_AVERAGE' THEN p.impressions ELSE 0 END) ctr_below "
         + BASE_FROM + " AND LOWER(ki.keyword_text) LIKE '%geico%' GROUP BY 1 ORDER BY 1")
    rows = []
    for ym, impr, ri, qsw, ca, cb in conn.execute(q):
        rows.append([ym, impr or 0, ri or 0,
                     round((ri or 0) / impr, 3) if impr else 0,
                     round(qsw / ri, 2) if ri else "",
                     round(ca / ri, 3) if ri else "", round(cb / ri, 3) if ri else ""])
    write(out_path(out, "q6_geico_qs_monthly.csv"),
          ["month", "impressions", "rated_impressions", "rated_share", "iw_qs",
           "pred_ctr_above_share", "pred_ctr_below_share"], rows)




def q7_segment_monthly(conn, out):
    """Monthly iw-QS by segment: each carrier brand (via keyword_classification.brand_name) vs GENERIC
    (no brand). Answers 'is the QS drop account-wide or concentrated?' Segments under 1,000 rated
    impressions in a month are still emitted (judge thinness by rated_impr)."""
    q = ("SELECT SUBSTR(p.report_date,1,7) ym, "
         " COALESCE(NULLIF(kc.brand_name,''),'GENERIC') seg, "
         " SUM(p.impressions) impr, "
         " SUM(CASE WHEN q.historical_quality_score IS NOT NULL THEN p.impressions ELSE 0 END) ri, "
         " SUM(q.historical_quality_score*p.impressions) qsw, "
         " SUM(CASE WHEN q.hist_creative_quality='BELOW_AVERAGE' THEN p.impressions ELSE 0 END) cr_below, "
         " SUM(CASE WHEN q.hist_predicted_ctr='BELOW_AVERAGE' THEN p.impressions ELSE 0 END) ctr_below "
         + BASE_FROM.replace(" WHERE ",
             " LEFT JOIN keyword_classification kc "
             " ON kc.account_id = ki.account_id "
             " AND kc.ad_group_criterion_id = ki.ad_group_criterion_id WHERE ") +
         " GROUP BY 1,2 ORDER BY 1,2")
    # house rule: verify the plan probes kc via its PK (composite leading column supplied), never a scan
    plan_lines = [r[3] for r in conn.execute("EXPLAIN QUERY PLAN " + q)]
    kc_line = next((l for l in plan_lines if " kc " in l or l.endswith(" kc") or "kc USING" in l), "")
    if (not kc_line) or "SCAN kc" in kc_line or "AUTOMATIC" in kc_line.upper():
        print("  [PLAN WARNING] q7 kc join is not an index probe -> expect a long scan. kc plan: %r"
              % (kc_line or "kc not found in plan"))
    else:
        print("  q7 plan OK: %s" % kc_line)
    rows = []
    for ym, seg, impr, ri, qsw, crb, ctb in conn.execute(q):
        rows.append([ym, seg, impr or 0, ri or 0,
                     round((ri or 0)/impr, 3) if impr else 0,
                     round(qsw/ri, 2) if ri else "",
                     round(crb/ri, 3) if ri else "", round(ctb/ri, 3) if ri else ""])
    write(out_path(out, "q7_segment_qs_monthly.csv"),
          ["month", "segment", "impressions", "rated_impressions", "rated_share",
           "iw_qs", "creative_below_share", "pred_ctr_below_share"], rows)


def main():
    ap = argparse.ArgumentParser(description="QS x performance analysis per charter")
    ap.add_argument("--db", required=True)
    ap.add_argument("--out-dir", default=DEFAULT_OUT)
    ap.add_argument("--exclude-term", default="", help="Q2 only: drop identities containing this term from the drift decomposition")
    ap.add_argument("--era-a-start", default="2024-04-01")
    ap.add_argument("--era-a-end", default="2024-12-31")
    ap.add_argument("--era-b-start", default="2026-02-01")
    ap.add_argument("--era-b-end", default="2026-08-31")
    ap.add_argument("--click-floor", type=int, default=500)
    ap.add_argument("--lp-days", type=int, default=90)
    ap.add_argument("--lp-spend-floor", type=float, default=100.0)
    ap.add_argument("--term", default="", help="scope ALL questions to keywords containing this term")
    a = ap.parse_args()
    conn = sqlite3.connect("file:%s?mode=ro" % a.db, uri=True)
    check_schema(conn)
    set_term_scope(a.term)
    global FNAME_SUFFIX
    if a.term:
        FNAME_SUFFIX = "__term-" + _slug(a.term)
        print("TERM SCOPE: all questions restricted to keywords containing %r "
              "(output files suffixed '%s')" % (a.term, FNAME_SUFFIX))
    os.makedirs(a.out_dir, exist_ok=True)
    print("Q1 match-type monthly...");   q1_matchtype_monthly(conn, a.out_dir)
    print("Q2 drift decomposition...");  q2_drift(conn, a.out_dir, a.era_a_start, a.era_a_end,
                                                  a.era_b_start, a.era_b_end, a.exclude_term)
    print("Q3 QS-band economics...");    q3_bands(conn, a.out_dir, a.click_floor)
    print("Q4 landing-page fix list...");q4_lp_fixlist(conn, a.out_dir, a.lp_days, a.lp_spend_floor)
    print("Q5 placement profile...");    q5_placements(conn, a.out_dir, a.lp_days, (a.term or "state farm"))
    print("Q6 geico QS trend...");       q6_geico_monthly(conn, a.out_dir)
    print("Q7 segment QS monthly...");   q7_segment_monthly(conn, a.out_dir)
    conn.close()
    print("wrote -> %s" % os.path.abspath(a.out_dir))


if __name__ == "__main__":
    main()
