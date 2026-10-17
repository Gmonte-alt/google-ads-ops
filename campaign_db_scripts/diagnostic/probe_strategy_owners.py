"""probe_strategy_owners.py -- read-only: which enabled campaigns run which portfolio
strategies, and who owns each (account vs MCC). The MCC-owned rows are the complete
list of live target levers whose change history is invisible at account level.

    python probe_strategy_owners.py --db C:\\Projects\\google-ads-ops\\db\\paid_search.db
"""
import argparse
import sqlite3

ap = argparse.ArgumentParser()
ap.add_argument("--db", required=True)
args = ap.parse_args()

conn = sqlite3.connect("file:%s?mode=ro" % args.db, uri=True)
conn.row_factory = sqlite3.Row
ccols = {r["name"] for r in conn.execute("PRAGMA table_info(campaigns)")}
status_col = "status" if "status" in ccols else "campaign_status"

for acct, label in [(1, "Google Home"), (29, "Term Life")]:
    print("=" * 78)
    print("account %s (%s): ENABLED campaigns -> portfolio strategy -> owner" % (acct, label))
    print("=" * 78)
    rows = conn.execute(
        "SELECT c.campaign_id, c.campaign_name, c.bidding_strategy_id, "
        "       b.strategy_name, b.owner, b.owner_customer_id "
        "FROM campaigns c "
        "LEFT JOIN bidding_strategies b ON b.account_id = c.account_id "
        "     AND CAST(b.bidding_strategy_id AS TEXT) = CAST(c.bidding_strategy_id AS TEXT) "
        "WHERE c.account_id = ? AND UPPER(c.%s) LIKE '%%ENABLED%%' "
        "ORDER BY (b.owner = 'mcc') DESC, c.campaign_name" % status_col, (acct,))
    n_mcc = 0
    for r in rows:
        if r["bidding_strategy_id"] is None:
            tag = "(campaign-level bidding, no portfolio)"
        elif r["strategy_name"] is None:
            tag = "strategy id %s -- UNKNOWN (not in bidding_strategies; re-run sync)" % r["bidding_strategy_id"]
        else:
            tag = "%s  [%s]" % (r["strategy_name"], r["owner"])
            if r["owner"] == "mcc":
                tag += "  <-- target history invisible at account level"
                n_mcc += 1
        print("  %-14s %-52s %s" % (r["campaign_id"], (r["campaign_name"] or "")[:52], tag))
    print("  -> %d enabled campaign(s) on MCC-owned strategies\n" % n_mcc)
conn.close()
