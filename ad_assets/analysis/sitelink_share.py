#!/usr/bin/env python
"""
sitelink_share.py -- ad_assets/analysis/ -- what share of account traffic
(clicks) and revenue comes from sitelinks DIRECTLY, on the corrected
THIS_ASSET data (clicks on the sitelink itself; additive across sitelinks).

Numerator:   asset_performance_daily, CUSTOMER level, click_type='THIS_ASSET'
Denominator: campaign table (auto-detected columns), all campaigns incl. PMax
Windows:     Last 30 days + YTD. READ-ONLY.

  python sitelink_share.py --db "C:\\Projects\\google-ads-ops\\db\\paid_search.db"
"""
import argparse
import datetime
import sqlite3
import sys

ACCOUNT_ID = 1


def main():
    p = argparse.ArgumentParser(description="Sitelink direct share of account (read-only).")
    p.add_argument("--db", required=True)
    p.add_argument("--rev30", type=float, default=None,
                   help="Account revenue for the last-30-days window (from UI/Looker) -- "
                        "overrides the fallback denominator")
    p.add_argument("--revytd", type=float, default=None,
                   help="Account revenue YTD (from UI/Looker)")
    args = p.parse_args()
    conn = sqlite3.connect("file:%s?mode=ro" % args.db.replace("\\", "/"), uri=True)

    import re as _re
    def table_cols(t):
        return {r[1] for r in conn.execute("PRAGMA table_info(%s)" % t)}
    def find_rev_col(colset):
        # generous pattern match instead of a name whitelist: anything that
        # looks like conversion value / revenue (prefer non-"all_" columns)
        cands = [c for c in colset
                 if _re.search(r"(conv.*val|val.*conv|revenue|conv_value)", c, _re.I)]
        cands.sort(key=lambda c: (c.lower().startswith("all"), len(c)))
        return cands[0] if cands else None
    cols = table_cols("campaign_performance_daily")
    rev_col = find_rev_col(cols)
    rev_table = "campaign_performance_daily"
    rev_datec = "date"
    # revenue may live in a SIBLING campaign-level table rather than as a
    # column on the perf table -- scan for one before falling back to grains
    if not rev_col:
        camp_tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE '%campaign%'")]
        for t in camp_tables:
            tc = table_cols(t)
            rc = find_rev_col(tc)
            datec = next((c for c in ("date", "report_date", "segment_date") if c in tc), None)
            if rc and datec and "campaign_id" in tc:
                rev_col, rev_table, rev_datec = rc, t, datec
                break
    # revenue-denominator fallback ladder when the campaign table lacks value
    rev_fallback = None
    if not rev_col:
        for t, extra in (("ad_group_performance_daily", ""),
                         ("rsa_ad_performance_daily", " AND device='ALL'")):
            tc = table_cols(t)
            if "conversions_value" in tc:
                datec = next((c for c in ("report_date", "date") if c in tc), None)
                if datec:
                    rev_fallback = (t, datec, extra)
                    break
    cost_col = next((c for c in ("cost_micros", "cost", "spend") if c in cols), None)
    if "clicks" not in cols:
        sys.exit("[ABORT] campaign_performance_daily has no clicks column -- "
                 "columns present: %s" % sorted(cols))
    cost_expr = ("SUM(%s)/1e6" % cost_col) if cost_col == "cost_micros" else (
        "SUM(%s)" % cost_col if cost_col else "NULL")
    rev_expr = "SUM(%s)" % rev_col if rev_col else "NULL"
    if rev_col:
        print("[SOURCES] revenue denominator: %s.%s" % (rev_table, rev_col))

    today = datetime.date.today()
    yday = (today - datetime.timedelta(days=1)).isoformat()
    windows = [("Last 30 days", (today - datetime.timedelta(days=30)).isoformat(), yday, args.rev30),
               ("YTD", today.replace(month=1, day=1).isoformat(), yday, args.revytd)]

    print("=" * 76)
    print("SITELINK DIRECT SHARE OF ACCOUNT  (this-asset clicks; revenue = conv value)")
    print("=" * 76)
    for label, d1, d2, rev_override in windows:
        num = conn.execute(
            """SELECT COALESCE(SUM(clicks),0), COALESCE(SUM(conversions),0),
                      COALESCE(SUM(conversions_value),0), COALESCE(SUM(cost_micros),0)/1e6
               FROM asset_performance_daily
               WHERE account_id=? AND association_level='CUSTOMER' AND device='ALL'
                 AND click_type='THIS_ASSET' AND report_date BETWEEN ? AND ?""",
            (ACCOUNT_ID, d1, d2)).fetchone()
        den = conn.execute(
            f"""SELECT COALESCE(SUM(clicks),0), NULL, {cost_expr}
                FROM campaign_performance_daily
                WHERE account_id=? AND date BETWEEN ? AND ?""",
            (ACCOUNT_ID, d1, d2)).fetchone()
        acct_rev_val = None
        if rev_col:
            acct_rev_val = conn.execute(
                f"""SELECT COALESCE(SUM({rev_col}),0) FROM {rev_table}
                    WHERE account_id=? AND {rev_datec} BETWEEN ? AND ?""",
                (ACCOUNT_ID, d1, d2)).fetchone()[0]
        den = (den[0], acct_rev_val, den[2])
        sl_clicks, sl_conv, sl_val, sl_cost = num
        acct_clicks, acct_rev, acct_cost = den
        print("\n%s  (%s .. %s)" % (label, d1, d2))
        print("  sitelink direct clicks : %10s  of %12s account clicks  = %5.1f%%"
              % (f"{sl_clicks:,}", f"{acct_clicks:,}",
                 100.0 * sl_clicks / acct_clicks if acct_clicks else 0))
        if rev_override:
            print("  sitelink direct revenue: %10s  of %12s account revenue = %5.1f%%"
                  % (f"${sl_val:,.0f}", f"${rev_override:,.0f}",
                     100.0 * sl_val / rev_override))
            print("        (denominator user-provided from UI/Looker -- authoritative)")
            if acct_cost:
                print("        implied account ROAS: %.2f" % (rev_override / acct_cost))
        elif acct_rev is not None:
            print("  sitelink direct revenue: %10s  of %12s account revenue = %5.1f%%"
                  % (f"${sl_val:,.0f}", f"${acct_rev:,.0f}",
                     100.0 * sl_val / acct_rev if acct_rev else 0))
        elif rev_fallback:
            t, datec, extra = rev_fallback
            acct_rev = conn.execute(
                f"""SELECT COALESCE(SUM(conversions_value),0) FROM {t}
                    WHERE account_id=? AND {datec} BETWEEN ? AND ?{extra}""",
                (ACCOUNT_ID, d1, d2)).fetchone()[0]
            print("  sitelink direct revenue: %10s  of %12s account revenue = %5.1f%%"
                  % (f"${sl_val:,.0f}", f"${acct_rev:,.0f}",
                     100.0 * sl_val / acct_rev if acct_rev else 0))
            print("        (revenue denominator from %s -- see scope note below)" % t)
            if acct_cost and acct_rev and acct_rev / acct_cost < 0.8:
                print("        [SUSPECT] implied account ROAS %.2f is implausibly low -- "
                      "denominator likely incomplete (enabled-scope backfill, coverage"
                      % (acct_rev / acct_cost))
                print("        holes, PMax exclusion). Re-run with --rev30/--revytd from "
                      "the UI for the true share.")
        else:
            print("  sitelink direct revenue: $%s  (no revenue column found in "
                  "any candidate table -- share not computable)" % f"{sl_val:,.0f}")
        if acct_clicks and sl_clicks:
            print("  avg CPC                : sitelink $%.2f vs account $%.2f"
                  % (sl_cost / sl_clicks, (acct_cost or 0) / acct_clicks))
        if acct_cost is not None:
            print("  sitelink direct cost   : %10s  of %12s account cost    = %5.1f%%"
                  % (f"${sl_cost:,.0f}", f"${acct_cost:,.0f}",
                     100.0 * sl_cost / acct_cost if acct_cost else 0))
    conn.close()
    print("\nNOTE: 'direct' = clicks ON the sitelink (This-extension). Sitelinks also")
    print("lift main-ad CTR; that indirect contribution is real but not in these")
    print("shares. Click/cost denominators include ALL campaigns (PMax included).")
    print("A fallback revenue denominator (ad-group or RSA grain) may exclude")
    print("PMax / non-RSA revenue -- slightly understated denominator, slightly")
    print("overstated revenue share; the note above says which table was used.")


if __name__ == "__main__":
    main()
