"""Read-only: where (in time) did 'homeowners insurance for rental property' and 'insurance roof
replacement' convert, and how far back does account-1 sqp coverage actually go?
Run: python diag_lost_terms.py
"""
import sqlite3

DB = r"C:\Projects\google-ads-ops\db\paid_search.db"
c = sqlite3.connect("file:%s?mode=ro" % DB, uri=True)

TERMS = ["homeowners insurance for rental property", "insurance roof replacement"]

print("=== 1) do the terms exist in search_queries at all? ===")
for t in TERMS:
    rows = c.execute("SELECT id, query_text, first_seen_date, last_seen_date FROM search_queries "
                     "WHERE LOWER(TRIM(query_text)) = ?", (t,)).fetchall()
    print("  %r -> %d row(s)" % (t, len(rows)))
    for r in rows:
        print("     id=%s first_seen=%s last_seen=%s" % (r[0], r[2], r[3]))
    like = c.execute("SELECT COUNT(*) FROM search_queries WHERE query_text LIKE ?",
                     ("%" + t.split()[-2] + " " + t.split()[-1] + "%",)).fetchone()[0]
    print("     (LIKE-match on tail words: %d rows)" % like)

print("\n=== 2) monthly performance for each term, account 1, google ===")
for t in TERMS:
    print("  --- %s ---" % t)
    rows = c.execute(
        "SELECT SUBSTR(p.report_date,1,7) AS ym, SUM(p.primary_conv_count), "
        " SUM(p.primary_conv_value), SUM(p.cost_micros)/1000000.0 "
        "FROM search_query_performance p "
        "JOIN search_queries sq ON sq.id = p.query_id "
        "JOIN campaigns c2 ON c2.campaign_id = p.campaign_id AND c2.account_id = 1 "
        "WHERE p.platform='google' AND LOWER(TRIM(sq.query_text)) = ? "
        "GROUP BY 1 ORDER BY 1", (t,)).fetchall()
    if not rows:
        print("     NO ROWS for account 1 / google")
    for ym, conv, val, cost in rows:
        print("     %s  conv=%-8.1f value=%-9.0f cost=%-8.0f" % (ym, conv or 0, val or 0, cost or 0))

print("\n=== 2b) same terms WITHOUT the account gate (any campaign/account) ===")
for t in TERMS:
    rows = c.execute(
        "SELECT SUBSTR(p.report_date,1,7) AS ym, SUM(p.primary_conv_count) "
        "FROM search_query_performance p JOIN search_queries sq ON sq.id=p.query_id "
        "WHERE LOWER(TRIM(sq.query_text)) = ? GROUP BY 1 ORDER BY 1", (t,)).fetchall()
    tot = sum(r[1] or 0 for r in rows)
    print("  %r: months=%d total_conv=%.0f  %s" % (t, len(rows), tot,
          " ".join("%s:%.0f" % (r[0], r[1] or 0) for r in rows[:14])))

print("\n=== 3) account-1 sqp coverage by month (conv totals) ===")
for ym, n, conv in c.execute(
        "SELECT SUBSTR(p.report_date,1,7), COUNT(*), SUM(p.primary_conv_count) "
        "FROM search_query_performance p "
        "JOIN campaigns c2 ON c2.campaign_id = p.campaign_id AND c2.account_id = 1 "
        "WHERE p.platform='google' AND p.report_date >= '2025-01-01' "
        "GROUP BY 1 ORDER BY 1"):
    print("   %s  rows=%-8d conv=%.0f" % (ym, n, conv or 0))
c.close()
