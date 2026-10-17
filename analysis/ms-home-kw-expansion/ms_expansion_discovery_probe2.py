"""
ms_expansion_discovery_probe2.py — READ-ONLY follow-up probe after probe 1 findings.

Probe 1 established: keyword_current has hierarchy_enabled/is_serving; acct 2 is 96.8%%
BROAD; only 6/50 campaigns ENABLED; negative_keywords EXISTS; naming taxonomy is
Home-{Generic|Non-Generic}-{Product}-{Intent}[-suffix] with parsed_* columns.

This probe answers the four follow-ups:
  (A) LIVE-SERVING footprint — how much of the 59.6K "ENABLED" book actually serves
      after the three-level rollup, and its match-type mix. Tests the hypothesis that
      the planner's ~0%% impr share reflects a small live footprint, not a losing one.
  (B) negative_keywords contents — acct-2 coverage, level values, freshness
      (is the ~9/7 USAA negative in there?), and the USAA rows themselves.
  (C) Parsed taxonomy on the acct-2 GENERIC book — distinct parsed_theme /
      parsed_match_tier / parsed_intent_tier values and whether they decode the
      -NA / -Up suffixes, plus the live generic ad-group roster w/ keyword counts.
  (D) The 6 ENABLED campaigns — names + parsed fields, so we know which campaigns
      the expansion would actually land in.

READ-ONLY. Paste the full output back.

    python ms_expansion_discovery_probe2.py
"""
import argparse
import sqlite3
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
ACCT = 2  # Microsoft Home


def section(title):
    print("\n" + "=" * 78)
    print("  " + title)
    print("=" * 78)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--db", default=r"C:\Projects\google-ads-ops\db\paid_search.db")
    a = p.parse_args()
    if not Path(a.db).exists():
        sys.exit("DB not found: %s" % a.db)
    conn = sqlite3.connect("file:%s?mode=ro" % a.db, uri=True)
    conn.row_factory = sqlite3.Row

    # ------------------------------------------------------------------ (A)
    section("(A) LIVE-SERVING footprint (keyword_current, acct 2)")
    print("  keyword_status x hierarchy_enabled x is_serving x match_type:")
    for r in conn.execute(
            "SELECT keyword_status, hierarchy_enabled, is_serving, match_type, COUNT(*) n "
            "FROM keyword_current WHERE account_id=? "
            "GROUP BY 1,2,3,4 ORDER BY n DESC", (ACCT,)):
        print("      %-9s hier=%-4s serving=%-4s %-7s %s"
              % (r["keyword_status"], r["hierarchy_enabled"], r["is_serving"],
                 r["match_type"], f"{r['n']:,}"))
    print("\n  BOTTOM LINE — actually-serving keywords by match_type:")
    for r in conn.execute(
            "SELECT match_type, COUNT(*) n, COUNT(DISTINCT keyword_text) texts "
            "FROM keyword_current WHERE account_id=? AND is_serving=1 "
            "GROUP BY 1 ORDER BY n DESC", (ACCT,)):
        print("      %-7s instances=%-9s distinct texts=%s"
              % (r["match_type"], f"{r['n']:,}", f"{r['texts']:,}"))
    tot = conn.execute("SELECT COUNT(*) FROM keyword_current WHERE account_id=? AND is_serving=1",
                       (ACCT,)).fetchone()[0]
    print("      TOTAL serving instances: %s  (vs 59,565 keyword-ENABLED)" % f"{tot:,}")

    # ------------------------------------------------------------------ (B)
    section("(B) negative_keywords — coverage, levels, freshness, USAA")
    print("  rows by account_id x platform:")
    for r in conn.execute(
            "SELECT account_id, platform, COUNT(*) n, MIN(first_seen_date) lo, "
            "MAX(last_seen_date) hi FROM negative_keywords GROUP BY 1,2 ORDER BY n DESC"):
        print("      acct=%-4s platform=%-10s rows=%-8s first_seen>=%s  last_seen<=%s"
              % (r["account_id"], r["platform"], f"{r['n']:,}", r["lo"], r["hi"]))
    print("\n  acct-2 rows by level x match_type:")
    for r in conn.execute(
            "SELECT level, match_type, COUNT(*) n FROM negative_keywords "
            "WHERE account_id=? GROUP BY 1,2 ORDER BY n DESC", (ACCT,)):
        print("      level=%-12s match=%-10s %s" % (r["level"], r["match_type"], f"{r['n']:,}"))
    print("\n  USAA negatives on acct 2 (the ~9/7 add — freshness check):")
    rows = conn.execute(
        "SELECT keyword_text, match_type, level, campaign_name, first_seen_date, last_seen_date "
        "FROM negative_keywords WHERE account_id=? AND lower(keyword_text) LIKE '%usaa%'",
        (ACCT,)).fetchall()
    if rows:
        for r in rows:
            print("      %-24r %-8s %-10s %-30s seen %s -> %s"
                  % (r["keyword_text"], r["match_type"], r["level"],
                     (r["campaign_name"] or "")[:30], r["first_seen_date"], r["last_seen_date"]))
    else:
        print("      NONE — table is stale w.r.t. the 9/7 USAA add; overlay from exports still required")

    # ------------------------------------------------------------------ (C)
    section("(C) Parsed taxonomy — acct-2 GENERIC book")
    for col in ("parsed_theme", "parsed_match_tier", "parsed_intent_tier"):
        print("  DISTINCT %s (generic ad groups):" % col)
        for r in conn.execute(
                "SELECT %s v, COUNT(*) n FROM ad_groups WHERE account_id=? "
                "AND lower(ad_group_name) NOT LIKE '%%non-generic%%' "
                "AND lower(ad_group_name) NOT LIKE '%%nongeneric%%' "
                "AND lower(ad_group_name) NOT LIKE '%%non generic%%' "
                "GROUP BY 1 ORDER BY n DESC LIMIT 25" % col, (ACCT,)):
            print("      %-32r %s" % (r["v"], f"{r['n']:,}"))
    print("\n  LIVE generic ad groups (ENABLED under an ENABLED campaign) w/ serving kw counts:")
    for r in conn.execute(
            "SELECT ag.ad_group_name, c.campaign_name, COUNT(kc.criterion_id) kws "
            "FROM ad_groups ag "
            "JOIN campaigns c ON c.campaign_id=ag.campaign_id AND c.account_id=ag.account_id "
            "LEFT JOIN keyword_current kc ON kc.ad_group_id=ag.ad_group_id "
            "     AND kc.account_id=ag.account_id AND kc.is_serving=1 "
            "WHERE ag.account_id=? AND ag.status='ENABLED' AND c.status='ENABLED' "
            "AND lower(ag.ad_group_name) NOT LIKE '%non-generic%' "
            "AND lower(ag.ad_group_name) NOT LIKE '%nongeneric%' "
            "AND lower(ag.ad_group_name) NOT LIKE '%non generic%' "
            "GROUP BY 1,2 ORDER BY kws DESC", (ACCT,)):
        print("      %-52s %-28s kws=%s"
              % (r["ad_group_name"][:52], r["campaign_name"][:28], f"{r['kws']:,}"))

    # ------------------------------------------------------------------ (D)
    section("(D) The ENABLED campaigns (acct 2)")
    for r in conn.execute(
            "SELECT campaign_name, campaign_type, parsed_product_type, parsed_geo_state, "
            "parsed_match_tier, parsed_intent_tier, daily_budget_micros/1e6 budget, "
            "bidding_strategy_type FROM campaigns "
            "WHERE account_id=? AND status='ENABLED' ORDER BY campaign_name", (ACCT,)):
        print("      %-40s type=%-10s prod=%-8s geo=%-14s match=%-8s intent=%-8s "
              "budget=%.0f bid=%s"
              % (r["campaign_name"][:40], r["campaign_type"], r["parsed_product_type"],
                 r["parsed_geo_state"], r["parsed_match_tier"], r["parsed_intent_tier"],
                 r["budget"] or 0, r["bidding_strategy_type"]))

    conn.close()
    print("\nPaste this whole output back — it completes discovery: the live footprint,")
    print("the negatives source of truth, and the taxonomy the expansion must slot into.")


if __name__ == "__main__":
    main()
