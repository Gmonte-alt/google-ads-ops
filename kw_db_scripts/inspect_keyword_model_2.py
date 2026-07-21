#!/usr/bin/env python
"""
inspect_keyword_model_2.py -- confirm ad_groups duplication + performance join integrity.
Read-only. Answers: (1) is ad_groups also dup'd? (2) do perf rows scatter across the
duplicate keyword_instances ids? (3) how many keywords are TRULY serving (dedup + hierarchy
ENABLED + recent impressions)?
    python inspect_keyword_model_2.py
"""
import sqlite3

DB = r"C:\Projects\google-ads-ops\db\paid_search.db"
A = 1

def main():
    c = sqlite3.connect("file:%s?mode=ro" % DB, uri=True).cursor()

    print("=== 1. ad_groups duplication ===")
    tot = c.execute("SELECT COUNT(*) FROM ad_groups WHERE account_id=?", (A,)).fetchone()[0]
    dis = c.execute("SELECT COUNT(DISTINCT ad_group_id) FROM ad_groups WHERE account_id=?", (A,)).fetchone()[0]
    print("  ad_groups rows: %s  distinct ad_group_id: %s  (avg %.1fx)"
          % (f"{tot:,}", f"{dis:,}", tot/dis if dis else 0))
    # of the DISTINCT ad groups, how many are enabled (dedup first)?
    en = c.execute("""SELECT COUNT(*) FROM (
                        SELECT ad_group_id, MAX(id) mid FROM ad_groups WHERE account_id=? GROUP BY ad_group_id
                      ) d JOIN ad_groups ag ON ag.id=d.mid WHERE ag.status='ENABLED'""", (A,)).fetchone()[0] \
         if 'id' in [r[1] for r in c.execute("PRAGMA table_info(ad_groups)")] else None
    print("  (enabled distinct ad groups, if id col present: %s)" % en)

    print("\n=== 2. campaigns duplication ===")
    tot = c.execute("SELECT COUNT(*) FROM campaigns WHERE account_id=?", (A,)).fetchone()[0]
    dis = c.execute("SELECT COUNT(DISTINCT campaign_id) FROM campaigns WHERE account_id=?", (A,)).fetchone()[0]
    print("  campaigns rows: %s  distinct campaign_id: %s  (avg %.1fx)"
          % (f"{tot:,}", f"{dis:,}", tot/dis if dis else 0))

    print("\n=== 3. performance join: do perf rows scatter across duplicate instance ids? ===")
    # pick the costco EXACT criterion, list its duplicate instance ids, see how perf distributes
    ids = [r[0] for r in c.execute(
        "SELECT id FROM keyword_instances WHERE account_id=? AND criterion_id='341287056186'", (A,))]
    print("  costco EXACT criterion has %d duplicate keyword_instances.id rows" % len(ids))
    if ids:
        qmarks = ",".join("?"*len(ids))
        perf = c.execute("SELECT COUNT(*), COUNT(DISTINCT instance_id) FROM keyword_performance_daily "
                         "WHERE instance_id IN (%s)" % qmarks, ids).fetchone()
        print("  perf rows referencing ANY of those ids: %s  across %s distinct instance_ids"
              % (f"{perf[0]:,}", perf[1]))
        print("  ^ if >1 distinct instance_id, performance is scattered across the duplicates")

    print("\n=== 4. TRULY serving keyword count (dedup + hierarchy ENABLED) ===")
    # dedup keyword_instances by criterion_id (max id), join enabled ad_groups + campaigns
    try:
        n = c.execute("""
          WITH ck AS (
            SELECT criterion_id, MAX(id) mid FROM keyword_instances WHERE account_id=? GROUP BY criterion_id
          ),
          agd AS (SELECT ad_group_id, MAX(id) mid FROM ad_groups WHERE account_id=? GROUP BY ad_group_id)
          SELECT COUNT(*) FROM ck
          JOIN keyword_instances ki ON ki.id=ck.mid
          JOIN agd ON agd.ad_group_id=ki.ad_group_id
          JOIN ad_groups ag ON ag.id=agd.mid AND ag.status='ENABLED'
          JOIN campaigns c2 ON c2.campaign_id=ki.campaign_id AND c2.account_id=? AND c2.status='ENABLED'
        """, (A, A, A)).fetchone()[0]
        print("  deduped keywords in ENABLED ad groups AND ENABLED campaigns: %s" % n)
    except sqlite3.OperationalError as e:
        print("  (query needs adjust: %s)" % e)

    print("\n=== 5. recent-serving via performance (ground truth) ===")
    r = c.execute("SELECT COUNT(DISTINCT instance_id) FROM keyword_performance_daily "
                  "WHERE report_date >= date('now','-14 day') AND impressions>0").fetchone()[0]
    print("  distinct instance_ids with impressions last 14d: %s" % f"{r:,}")
    print("\nDone.")

if __name__ == "__main__":
    main()
