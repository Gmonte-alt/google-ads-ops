#!/usr/bin/env python
"""
01c_sync_this_asset.py -- ad_assets/sync/ -- per-asset INTERACTION metrics
(the UI's "This extension" segment; API segments.asset_interaction_target),
stored as click_type='THIS_ASSET' rows in the existing tables (gate
2026-09-03, no DDL -- click_type is unconstrained TEXT):

  asset_performance_daily        (CUSTOMER level, association_id=1)
  asset_campaign_performance_daily (campaign-attributed)

RULES (gate memo): THIS_ASSET rows overlap the click-type partition -- never
sum them with other click_type rows (A1 in 01_sync_assets.py excludes them);
their impressions are presence-duplicates -- consume clicks/cost/
conversions/value ONLY.

Routine: weekly, 4th line, same --days 14. First run: --from 2026-01-01.

  python 01c_sync_this_asset.py --db "...\\paid_search.db" --from 2026-01-01
  python 01c_sync_this_asset.py --db "...\\paid_search.db" --days 14
"""
import argparse
import datetime
import os
import sqlite3
import sys
from collections import defaultdict

from dotenv import load_dotenv

load_dotenv()

try:
    from google.ads.googleads.client import GoogleAdsClient
    from google.ads.googleads.errors import GoogleAdsException
except ImportError:
    sys.exit("[ABORT] google-ads library not found -- run inside the paid-search conda env.")

CUSTOMER_ID = os.environ.get("PROBE_CUSTOMER_ID", "8886502147").replace("-", "")
ACCOUNT_ID = 1

UP_LINK = """INSERT INTO asset_performance_daily (account_id, asset_id,
                 association_level, association_id, device, click_type, report_date,
                 impressions, clicks, cost_micros, conversions, conversions_value,
                 all_conversions, all_conversions_value, last_synced_at)
             VALUES (?,?,'CUSTOMER',1,'ALL','THIS_ASSET',?,?,?,?,?,?,0,0,datetime('now'))
             ON CONFLICT(account_id, asset_id, association_level, association_id,
                         device, click_type, report_date)
             DO UPDATE SET impressions=excluded.impressions, clicks=excluded.clicks,
                 cost_micros=excluded.cost_micros, conversions=excluded.conversions,
                 conversions_value=excluded.conversions_value,
                 last_synced_at=excluded.last_synced_at"""

UP_CAMP = """INSERT INTO asset_campaign_performance_daily (account_id, asset_id,
                 campaign_id, device, click_type, report_date, impressions, clicks,
                 cost_micros, conversions, conversions_value, last_synced_at)
             VALUES (?,?,?,'ALL','THIS_ASSET',?,?,?,?,?,?,datetime('now'))
             ON CONFLICT(account_id, asset_id, campaign_id, device, click_type, report_date)
             DO UPDATE SET impressions=excluded.impressions, clicks=excluded.clicks,
                 cost_micros=excluded.cost_micros, conversions=excluded.conversions,
                 conversions_value=excluded.conversions_value,
                 last_synced_at=excluded.last_synced_at"""


def main():
    p = argparse.ArgumentParser(description="This-asset interaction sync (THIS_ASSET rows).")
    p.add_argument("--db", required=True)
    p.add_argument("--days", type=int, default=30)
    p.add_argument("--from", dest="date_from", default=None, metavar="YYYY-MM-DD")
    args = p.parse_args()
    if not os.path.isfile(args.db):
        sys.exit("[ABORT] database not found: %s" % args.db)
    today = datetime.date.today()
    date_to = (today - datetime.timedelta(days=1)).isoformat()
    date_from = args.date_from or (today - datetime.timedelta(days=args.days)).isoformat()
    datetime.date.fromisoformat(date_from)

    client = GoogleAdsClient.load_from_storage()
    ga = client.get_service("GoogleAdsService")
    conn = sqlite3.connect(args.db)
    conn.execute("PRAGMA foreign_keys = ON")
    print("=" * 72)
    print("THIS-ASSET INTERACTION SYNC  account_id=%d  %s..%s" % (ACCOUNT_ID, date_from, date_to))
    print("=" * 72)

    # one query, campaign-attributed; link-grain rows derived by summing campaigns
    # (campaign attribution ~= customer totals per A7's standing tolerance)
    try:
        rows = []
        for batch in ga.search_stream(customer_id=CUSTOMER_ID, query=f"""
            SELECT asset.id, campaign.id, segments.date,
                   segments.asset_interaction_target.interaction_on_this_asset,
                   metrics.impressions, metrics.clicks, metrics.cost_micros,
                   metrics.conversions, metrics.conversions_value
            FROM customer_asset
            WHERE customer_asset.field_type = 'SITELINK'
              AND segments.date BETWEEN '{date_from}' AND '{date_to}'
        """):
            rows.extend(batch.results)
    except GoogleAdsException as e:
        conn.close()
        sys.exit("[ABORT] API error, nothing written: %s" % e)

    camp_rows = {}
    link_rows = defaultdict(lambda: [0, 0, 0, 0.0, 0.0])
    for r in rows:
        if not r.segments.asset_interaction_target.interaction_on_this_asset:
            continue
        key = (r.asset.id, r.campaign.id, str(r.segments.date))
        cell = camp_rows.setdefault(key, [0, 0, 0, 0.0, 0.0])
        cell[0] += r.metrics.impressions
        cell[1] += r.metrics.clicks
        cell[2] += r.metrics.cost_micros
        cell[3] += r.metrics.conversions
        cell[4] += r.metrics.conversions_value
        lk = link_rows[(r.asset.id, str(r.segments.date))]
        for i, v in enumerate((r.metrics.impressions, r.metrics.clicks,
                               r.metrics.cost_micros, r.metrics.conversions,
                               r.metrics.conversions_value)):
            lk[i] += v

    try:
        for (aid, camp, d), v in camp_rows.items():
            conn.execute(UP_CAMP, (ACCOUNT_ID, aid, camp, d, v[0], v[1], v[2], v[3], v[4]))
        for (aid, d), v in link_rows.items():
            conn.execute(UP_LINK, (ACCOUNT_ID, aid, d, v[0], v[1], v[2], v[3], v[4]))
        conn.commit()
    except sqlite3.Error as e:
        conn.rollback()
        conn.close()
        sys.exit("[ABORT] DB write failed, rolled back: %s" % e)
    print("[PERF] THIS_ASSET campaign-split rows: %d | link-grain rows: %d"
          % (len(camp_rows), len(link_rows)))

    # acceptance: the this-asset slice cannot exceed its SITELINKS-type whole
    n_bad = conn.execute(
        """SELECT COUNT(*) FROM (
             SELECT t.asset_id, t.report_date
             FROM asset_performance_daily t
             JOIN asset_performance_daily s
               ON s.account_id=t.account_id AND s.asset_id=t.asset_id
              AND s.association_level=t.association_level
              AND s.association_id=t.association_id AND s.device='ALL'
              AND s.click_type='SITELINKS' AND s.report_date=t.report_date
             WHERE t.account_id=? AND t.click_type='THIS_ASSET' AND t.device='ALL'
               AND t.report_date BETWEEN ? AND ?
               AND t.clicks > s.clicks)""",
        (ACCOUNT_ID, date_from, date_to)).fetchone()[0]
    print("[ASSERT A8] %s -- this-asset clicks <= SITELINKS-type clicks per (asset, date)"
          " (%d violation(s))" % ("PASS" if n_bad == 0 else "FAIL", n_bad))
    conn.close()
    print("\n[RESULT] this-asset sync complete.%s"
          % ("" if n_bad == 0 else " INVESTIGATE A8 before trusting new rows."))


if __name__ == "__main__":
    main()
