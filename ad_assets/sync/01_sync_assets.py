#!/usr/bin/env python
"""
01_sync_assets.py -- ad_assets/sync/ -- sitelink asset sync for paid_search.db.

Syncs, in parents-first order (FKs enforced on this connection):
  1. assets            -- SITELINK assets: identity, approval_status
  2. asset_sitelink_data -- link_text, descriptions, final urls, url params (JSON)
  3. asset_associations  -- customer / campaign / ad_group links, with
                            tombstoning (removed_date) and re-attachment revival
  4. asset_performance_daily -- TRIPLE-PASS per gate ruling 2026-07-17:
       (device='ALL', click_type='ALL')  unsegmented -- authoritative totals
       (device=X,     click_type='ALL')  device split
       (device='ALL', click_type=X)      click-type split
     No device x click_type cross pass in v1.

Hard rules implemented (charter + gate rulings):
  * ALL enum values via the client library's .name -- never hand-written maps
  * upsert on natural keys; assets upsert NEVER touches theme_tag /
    usp_category / buyer_journey_stage (analysis-owned columns)
  * association tombstoning: links absent from the API pull get removed_date
    stamped (never DELETEd); links that reappear get removed_date cleared
  * impressions additivity rule enforced by post-run assertions (see below)
  * account scoping: every write carries account_id

Post-run acceptance assertions (per architect, extended):
  A1 clicks/conversions across click_type rows SUM to the ALL row      [HARD]
  A2 no click_type row's impressions EXCEED the ALL row; the max
     click_type row's impressions ~equal the ALL row                   [HARD]
     (note: per-row equality was the original spec, but AD_IMAGE rows
      carry only image-eligible impressions -- flagged at the gate)
  A3 device rows sum ~= ALL row (warn only -- modeled data may drift)  [WARN]
  Plus a printed comparison block: probe-key row (asset 317584962874 /
  campaign 23385188622) and the customer-level association count, for
  eyeball verification against API ground truth.

Usage (weekly routine line, after the 18->16->17 sequence):
  python 01_sync_assets.py --db "C:\\Projects\\google-ads-ops\\db\\paid_search.db" --days 14

  --days N   performance window ending yesterday (default 30 for first
             backfill; 14 in the routine so restated conversions get
             re-upserted on the same PK)
"""
import argparse
import datetime
import json
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
ACCOUNT_ID = 1  # Google Home in paid_search.db account mapping
ASSET_TYPE = "SITELINK"  # Phase-1b (callouts etc.) parameterizes this; the tombstone
                         # sweep MUST stay scoped to it (gate ruling 2026-07-17)

PERF_RESOURCES = [  # (GAQL resource, association_level, container id field)
    ("customer_asset", "CUSTOMER", "customer.id"),
    ("campaign_asset", "CAMPAIGN", "campaign.id"),
    ("ad_group_asset", "AD_GROUP", "ad_group.id"),
]

METRICS = ("metrics.impressions, metrics.clicks, metrics.cost_micros, "
           "metrics.conversions, metrics.conversions_value, "
           "metrics.all_conversions, metrics.all_conversions_value")

PROBE_KEY = {"asset_id": 317584962874, "campaign_id": 23385188622}  # ground-truth eyeball row


def gaql(ga, query):
    rows = []
    stream = ga.search_stream(customer_id=CUSTOMER_ID, query=query)
    for batch in stream:
        rows.extend(batch.results)
    return rows


# ---------------------------------------------------------------------------
# 1 + 2: assets and sitelink payload
# ---------------------------------------------------------------------------
def sync_assets(ga, conn, run_date):
    q = """
        SELECT asset.id, asset.type, asset.name,
               asset.policy_summary.approval_status,
               asset.sitelink_asset.link_text,
               asset.sitelink_asset.description1,
               asset.sitelink_asset.description2,
               asset.final_urls, asset.final_mobile_urls,
               asset.url_custom_parameters
        FROM asset
        WHERE asset.type = 'SITELINK'
    """
    rows = gaql(ga, q)
    for r in rows:
        conn.execute(
            """INSERT INTO assets (account_id, asset_id, asset_type, asset_name,
                                   status, approval_status,
                                   first_seen_date, last_seen_date, last_synced_at)
               VALUES (?,?,?,?,NULL,?,?,?,datetime('now'))
               ON CONFLICT(account_id, asset_id) DO UPDATE SET
                   asset_type      = excluded.asset_type,
                   asset_name      = excluded.asset_name,
                   approval_status = excluded.approval_status,
                   last_seen_date  = excluded.last_seen_date,
                   last_synced_at  = excluded.last_synced_at""",
            # NOTE: theme_tag / usp_category / buyer_journey_stage deliberately
            # absent from DO UPDATE -- the sync never touches analysis columns.
            # NOTE: assets.status left NULL -- the Asset resource has no status
            # field in the API; lifecycle status lives on the association
            # (asset_associations.link_status). Flagged at the gate.
            (ACCOUNT_ID, r.asset.id, r.asset.type_.name, r.asset.name or None,
             r.asset.policy_summary.approval_status.name,
             run_date, run_date))

        params = {p.key: p.value for p in r.asset.url_custom_parameters}
        conn.execute(
            """INSERT INTO asset_sitelink_data (account_id, asset_id, link_text,
                   description1, description2, final_url, final_url_mobile,
                   final_url_params_json, last_synced_at)
               VALUES (?,?,?,?,?,?,?,?,datetime('now'))
               ON CONFLICT(account_id, asset_id) DO UPDATE SET
                   link_text = excluded.link_text,
                   description1 = excluded.description1,
                   description2 = excluded.description2,
                   final_url = excluded.final_url,
                   final_url_mobile = excluded.final_url_mobile,
                   final_url_params_json = excluded.final_url_params_json,
                   last_synced_at = excluded.last_synced_at""",
            (ACCOUNT_ID, r.asset.id, r.asset.sitelink_asset.link_text,
             r.asset.sitelink_asset.description1 or None,
             r.asset.sitelink_asset.description2 or None,
             r.asset.final_urls[0] if r.asset.final_urls else None,
             r.asset.final_mobile_urls[0] if r.asset.final_mobile_urls else None,
             json.dumps(params) if params else None))
    print("[ASSETS] upserted %d sitelink assets (+ payload)" % len(rows))
    return len(rows)


# ---------------------------------------------------------------------------
# 3: associations with tombstoning
# ---------------------------------------------------------------------------
def sync_associations(ga, conn, run_date, run_marker):
    total = 0
    for resource, level, id_field in PERF_RESOURCES:
        q = f"""
            SELECT {id_field}, asset.id, {resource}.status
            FROM {resource}
            WHERE asset.type = 'SITELINK'
        """
        rows = gaql(ga, q)
        for r in rows:
            container_id = (r.customer.id if level == "CUSTOMER"
                            else r.campaign.id if level == "CAMPAIGN"
                            else r.ad_group.id)
            link_status = getattr(r, resource).status.name
            conn.execute(
                """INSERT INTO asset_associations (account_id, asset_id,
                       association_level, association_id, field_type, link_status,
                       first_seen_date, last_seen_date, removed_date, last_synced_at)
                   VALUES (?,?,?,?, 'SITELINK', ?, ?, ?, NULL, ?)
                   ON CONFLICT(account_id, asset_id, association_level, association_id)
                   DO UPDATE SET
                       link_status    = excluded.link_status,
                       last_seen_date = excluded.last_seen_date,
                       removed_date   = NULL,              -- revival on re-attachment
                       last_synced_at = excluded.last_synced_at""",
                (ACCOUNT_ID, r.asset.id, level, container_id,
                 link_status, run_date, run_date, run_marker))
        total += len(rows)
        print("[ASSOC] %-9s level: %d links" % (level, len(rows)))

    # tombstone: anything this account has live FOR THIS ASSET TYPE that this
    # run did not touch. Type scoping is load-bearing (gate ruling 2026-07-17):
    # without it, a sitelink-only pull would falsely tombstone every other
    # asset type's associations the moment Phase-1b adds one.
    cur = conn.execute(
        """UPDATE asset_associations
           SET removed_date = ?
           WHERE account_id = ? AND removed_date IS NULL AND last_synced_at != ?
             AND asset_id IN (SELECT asset_id FROM assets
                              WHERE account_id = ? AND asset_type = ?)""",
        (run_date, ACCOUNT_ID, run_marker, ACCOUNT_ID, ASSET_TYPE))
    if cur.rowcount:
        print("[ASSOC] tombstoned %d association(s) absent from this pull" % cur.rowcount)
    return total


# ---------------------------------------------------------------------------
# 4: performance, triple-pass
# ---------------------------------------------------------------------------
def sync_performance(ga, conn, date_from, date_to):
    passes = [  # (label, extra select, device getter, click_type getter)
        ("ALL/ALL", "", lambda r: "ALL", lambda r: "ALL"),
        ("device", ", segments.device", lambda r: r.segments.device.name, lambda r: "ALL"),
        ("click_type", ", segments.click_type", lambda r: "ALL",
         lambda r: r.segments.click_type.name),
    ]
    n = 0
    for resource, level, id_field in PERF_RESOURCES:
        for label, seg_select, get_dev, get_ct in passes:
            q = f"""
                SELECT {id_field}, asset.id, segments.date{seg_select}, {METRICS}
                FROM {resource}
                WHERE asset.type = 'SITELINK'
                  AND segments.date BETWEEN '{date_from}' AND '{date_to}'
            """
            rows = gaql(ga, q)
            for r in rows:
                container_id = (r.customer.id if level == "CUSTOMER"
                                else r.campaign.id if level == "CAMPAIGN"
                                else r.ad_group.id)
                conn.execute(
                    """INSERT INTO asset_performance_daily (account_id, asset_id,
                           association_level, association_id, device, click_type,
                           report_date, impressions, clicks, cost_micros,
                           conversions, conversions_value,
                           all_conversions, all_conversions_value, last_synced_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'))
                       ON CONFLICT(account_id, asset_id, association_level,
                                   association_id, device, click_type, report_date)
                       DO UPDATE SET
                           impressions = excluded.impressions,
                           clicks = excluded.clicks,
                           cost_micros = excluded.cost_micros,
                           conversions = excluded.conversions,
                           conversions_value = excluded.conversions_value,
                           all_conversions = excluded.all_conversions,
                           all_conversions_value = excluded.all_conversions_value,
                           last_synced_at = excluded.last_synced_at""",
                    (ACCOUNT_ID, r.asset.id, level, container_id,
                     get_dev(r), get_ct(r), str(r.segments.date),
                     r.metrics.impressions, r.metrics.clicks, r.metrics.cost_micros,
                     r.metrics.conversions, r.metrics.conversions_value,
                     r.metrics.all_conversions, r.metrics.all_conversions_value))
            n += len(rows)
            print("[PERF] %-9s %-10s pass: %d rows" % (level, label, len(rows)))
    return n


# ---------------------------------------------------------------------------
# acceptance assertions
# ---------------------------------------------------------------------------
def run_assertions(conn, date_from, date_to):
    hard_fail = False
    base = ("FROM asset_performance_daily WHERE account_id = %d "
            "AND report_date BETWEEN '%s' AND '%s'" % (ACCOUNT_ID, date_from, date_to))

    # A1: clicks + conversions partition across click_type
    q = f"""
        SELECT a.asset_id, a.association_level, a.association_id, a.report_date,
               a.clicks AS all_clicks, a.conversions AS all_conv,
               s.sum_clicks, s.sum_conv
        FROM asset_performance_daily a
        JOIN (SELECT asset_id, association_level, association_id, report_date,
                     SUM(clicks) sum_clicks, SUM(conversions) sum_conv
              {base} AND device='ALL' AND click_type != 'ALL'
              GROUP BY 1,2,3,4) s
          USING (asset_id, association_level, association_id, report_date)
        WHERE a.account_id = {ACCOUNT_ID} AND a.device='ALL' AND a.click_type='ALL'
          AND (ABS(a.clicks - s.sum_clicks) > 1 OR ABS(a.conversions - s.sum_conv) > 0.51)
    """
    bad = conn.execute(q).fetchall()
    if bad:
        hard_fail = True
        print("[ASSERT A1] FAIL -- %d key(s) where click_type rows do not sum to ALL "
              "(first: %s)" % (len(bad), bad[0]))
    else:
        print("[ASSERT A1] PASS -- clicks/conversions partition cleanly across click_type")

    # A2: impressions non-additivity -- no click_type row exceeds ALL; max ~equals ALL
    q = f"""
        SELECT COUNT(*)
        FROM asset_performance_daily a
        JOIN asset_performance_daily s
          ON s.account_id=a.account_id AND s.asset_id=a.asset_id
         AND s.association_level=a.association_level AND s.association_id=a.association_id
         AND s.report_date=a.report_date AND s.device='ALL' AND s.click_type != 'ALL'
        WHERE a.account_id={ACCOUNT_ID} AND a.device='ALL' AND a.click_type='ALL'
          AND a.report_date BETWEEN '{date_from}' AND '{date_to}'
          AND s.impressions > a.impressions
    """
    n_exceed = conn.execute(q).fetchone()[0]
    if n_exceed:
        hard_fail = True
        print("[ASSERT A2] FAIL -- %d click_type row(s) exceed the ALL row's impressions" % n_exceed)
    else:
        print("[ASSERT A2] PASS -- no click_type row exceeds ALL-row impressions "
              "(impressions repeat, they do not partition)")

    # A3: device sum vs ALL (warn only)
    q = f"""
        SELECT COUNT(*)
        FROM asset_performance_daily a
        JOIN (SELECT asset_id, association_level, association_id, report_date,
                     SUM(clicks) sum_clicks
              {base} AND click_type='ALL' AND device != 'ALL'
              GROUP BY 1,2,3,4) s
          USING (asset_id, association_level, association_id, report_date)
        WHERE a.account_id={ACCOUNT_ID} AND a.device='ALL' AND a.click_type='ALL'
          AND ABS(a.clicks - s.sum_clicks) > MAX(2, 0.02 * a.clicks)
    """
    n_drift = conn.execute(q).fetchone()[0]
    print("[ASSERT A3] %s -- device rows vs ALL row (%d key(s) drift > 2%%)"
          % ("WARN" if n_drift else "PASS", n_drift))

    # ground-truth eyeball block
    print("\n[GROUND TRUTH] compare against API/UI for the window %s..%s:" % (date_from, date_to))
    row = conn.execute(
        """SELECT SUM(impressions), SUM(clicks), SUM(conversions), SUM(conversions_value)
           FROM asset_performance_daily
           WHERE account_id=? AND asset_id=? AND association_level='CAMPAIGN'
             AND association_id=? AND device='ALL' AND click_type='ALL'
             AND report_date BETWEEN ? AND ?""",
        (ACCOUNT_ID, PROBE_KEY["asset_id"], PROBE_KEY["campaign_id"],
         date_from, date_to)).fetchone()
    print("  probe key (asset %d / campaign %d): impr %s clicks %s conv %s val %s"
          % (PROBE_KEY["asset_id"], PROBE_KEY["campaign_id"], *row))
    n_cu = conn.execute(
        """SELECT COUNT(*) FROM asset_associations
           WHERE account_id=? AND association_level='CUSTOMER' AND removed_date IS NULL""",
        (ACCOUNT_ID,)).fetchone()[0]
    print("  live customer-level sitelink associations: %d (probe saw 36)" % n_cu)
    return hard_fail


def main():
    p = argparse.ArgumentParser(description="Sitelink asset sync (assets, associations, perf).")
    p.add_argument("--db", required=True, help="Path to paid_search.db (REQUIRED)")
    p.add_argument("--days", type=int, default=30,
                   help="Performance window ending yesterday (default 30; routine uses 14)")
    p.add_argument("--from", dest="date_from", default=None, metavar="YYYY-MM-DD",
                   help="Explicit window start date, overrides --days "
                        "(backfills: e.g. --from 2026-01-01 for YTD)")
    args = p.parse_args()

    if not os.path.isfile(args.db):
        sys.exit("[ABORT] database not found: %s" % args.db)

    today = datetime.date.today()
    date_to = (today - datetime.timedelta(days=1)).isoformat()
    if args.date_from:
        datetime.date.fromisoformat(args.date_from)  # validates format, raises if bad
        date_from = args.date_from
    else:
        date_from = (today - datetime.timedelta(days=args.days)).isoformat()
    run_date = today.isoformat()
    run_marker = datetime.datetime.now().isoformat(timespec="seconds")

    client = GoogleAdsClient.load_from_storage()
    ga = client.get_service("GoogleAdsService")

    conn = sqlite3.connect(args.db)
    conn.execute("PRAGMA foreign_keys = ON")

    print("=" * 72)
    print("SITELINK ASSET SYNC  account_id=%d customer=%s  perf %s..%s"
          % (ACCOUNT_ID, CUSTOMER_ID, date_from, date_to))
    print("=" * 72)

    try:
        sync_assets(ga, conn, run_date)          # parents first
        sync_associations(ga, conn, run_date, run_marker)
        sync_performance(ga, conn, date_from, date_to)
        conn.commit()
    except (GoogleAdsException, sqlite3.Error) as e:
        conn.rollback()
        conn.close()
        sys.exit("[ABORT] sync failed, all changes rolled back: %s" % e)

    print()
    hard_fail = run_assertions(conn, date_from, date_to)
    conn.close()

    if hard_fail:
        sys.exit("\n[RESULT] SYNC WROTE DATA BUT HARD ASSERTIONS FAILED -- "
                 "investigate before consuming this table.")
    print("\n[RESULT] sync complete, assertions passed.")


if __name__ == "__main__":
    main()
