#!/usr/bin/env python
"""
01b_sync_asset_campaign_split.py -- ad_assets/sync/ -- campaign-split
performance for account-linked SITELINK assets, from customer_asset with
campaign attribution (Q11 probe: GO, full fidelity).

Two passes, same window, same transaction (Phase-1 pattern):
  (click_type='ALL')  no click_type segment -- authoritative impressions
  (click_type=X)      segments.click_type   -- the partition BY CLICK KIND.
                      'SITELINKS' rows = sitelink-KIND clicks during this
                      asset's impressions, INCLUDING co-served neighbors'
                      clicks (~1.5x). NOT per-asset direct credit -- that is
                      click_type='THIS_ASSET' rows, written by
                      01c_sync_this_asset.py (interaction segment, matches
                      the UI's 'This extension'). Corrected 2026-09-03.

Assertion A7 [WARN]: per (asset, date), campaign-attributed 'ALL' clicks
summed across campaigns ~= the customer-level row in asset_performance_daily
(tolerance 2% or 2 clicks) -- attribution can shade totals slightly; drift
beyond tolerance is worth a look, not an abort.

Routine: weekly, after 01_sync_assets.py, same --days 14.

  python 01b_sync_asset_campaign_split.py --db "...\\paid_search.db" --from 2026-01-01   # backfill
  python 01b_sync_asset_campaign_split.py --db "...\\paid_search.db" --days 14           # weekly
"""
import argparse
import datetime
import os
import sqlite3
import sys

from dotenv import load_dotenv

load_dotenv()

try:
    from google.ads.googleads.client import GoogleAdsClient
    from google.ads.googleads.errors import GoogleAdsException
except ImportError:
    sys.exit("[ABORT] google-ads library not found -- run inside the paid-search conda env.")

CUSTOMER_ID = os.environ.get("PROBE_CUSTOMER_ID", "8886502147").replace("-", "")
ACCOUNT_ID = 1

UPSERT = """INSERT INTO asset_campaign_performance_daily (account_id, asset_id,
                campaign_id, device, click_type, report_date, impressions,
                clicks, cost_micros, conversions, conversions_value, last_synced_at)
            VALUES (?,?,?,'ALL',?,?,?,?,?,?,?,datetime('now'))
            ON CONFLICT(account_id, asset_id, campaign_id, device, click_type, report_date)
            DO UPDATE SET impressions=excluded.impressions, clicks=excluded.clicks,
                cost_micros=excluded.cost_micros, conversions=excluded.conversions,
                conversions_value=excluded.conversions_value,
                last_synced_at=excluded.last_synced_at"""


def gaql(ga, query):
    rows = []
    for batch in ga.search_stream(customer_id=CUSTOMER_ID, query=query):
        rows.extend(batch.results)
    return rows


def main():
    p = argparse.ArgumentParser(description="Campaign-split sitelink perf sync.")
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
    print("ASSET CAMPAIGN-SPLIT SYNC  account_id=%d  %s..%s" % (ACCOUNT_ID, date_from, date_to))
    print("=" * 72)

    passes = [("ALL", "", lambda r: "ALL"),
              ("click_type", ", segments.click_type", lambda r: r.segments.click_type.name)]
    try:
        for label, seg, get_ct in passes:
            q = f"""
                SELECT asset.id, campaign.id, segments.date{seg},
                       metrics.impressions, metrics.clicks, metrics.cost_micros,
                       metrics.conversions, metrics.conversions_value
                FROM customer_asset
                WHERE customer_asset.field_type = 'SITELINK'
                  AND segments.date BETWEEN '{date_from}' AND '{date_to}'
            """
            rows = gaql(ga, q)
            for r in rows:
                conn.execute(UPSERT, (ACCOUNT_ID, r.asset.id, r.campaign.id,
                                      get_ct(r), str(r.segments.date),
                                      r.metrics.impressions, r.metrics.clicks,
                                      r.metrics.cost_micros, r.metrics.conversions,
                                      r.metrics.conversions_value))
            print("[PERF] campaign-split %s pass: %d rows" % (label, len(rows)))
        conn.commit()
    except (GoogleAdsException, sqlite3.Error) as e:
        conn.rollback()
        conn.close()
        sys.exit("[ABORT] sync failed, all changes rolled back: %s" % e)

    # A7 [WARN]: campaign sums vs customer-level link grain
    n_drift = conn.execute(
        """SELECT COUNT(*) FROM (
             SELECT c.asset_id, c.report_date, SUM(c.clicks) s, MAX(a.clicks) t
             FROM asset_campaign_performance_daily c
             JOIN asset_performance_daily a
               ON a.account_id=c.account_id AND a.asset_id=c.asset_id
              AND a.association_level='CUSTOMER' AND a.device='ALL'
              AND a.click_type='ALL' AND a.report_date=c.report_date
             WHERE c.account_id=? AND c.device='ALL' AND c.click_type='ALL'
               AND c.report_date BETWEEN ? AND ?
             GROUP BY c.asset_id, c.report_date
             HAVING ABS(s - t) > MAX(2, 0.02 * t))""",
        (ACCOUNT_ID, date_from, date_to)).fetchone()[0]
    print("[ASSERT A7] %s -- campaign sums vs customer-level rows (%d key(s) drift > 2%%)"
          % ("WARN" if n_drift else "PASS", n_drift))
    conn.close()
    print("\n[RESULT] campaign-split sync complete.")


if __name__ == "__main__":
    main()
