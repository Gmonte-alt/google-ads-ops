#!/usr/bin/env python
"""
02_sync_rsa.py -- ad_assets/sync/ -- RSA layer sync for paid_search.db.

Syncs, in parents-first order (FKs enforced on this connection):
  1. assets (asset_type='TEXT')       -- identity rows for headline/description assets
  2. asset_text_data                  -- the text payload
  3. rsa_ads                          -- one row per RSA, with the FINGERPRINT
  4. rsa_ad_assets                    -- composition (which assets in which ad),
                                         pinned_field + performance_label snapshot,
                                         tombstone/revival semantics
  5. rsa_asset_performance_daily      -- DUAL-PASS per Q10 verdict (GO):
                                           (device='ALL')  unsegmented, authoritative
                                           (device=X)      device split
                                         NO click_type at this grain (gate ruling).
  6. rsa_ad_performance_daily         -- AD GRAIN (addendum, gate 2026-07-22):
                                         the additive, exclusively-attributed
                                         grain -- variant x campaign analysis
                                         runs HERE, never on asset rows.
                                         Same window, same transaction as the
                                         asset pull, dual-pass device.

FINGERPRINT RECIPE (authoritative, per 02_create_rsa_tables.sql):
  headlines only -> lowercase -> strip + collapse internal whitespace ->
  sort -> join '||' -> md5 -> first 10 hex chars.

Hard rules: enum .name everywhere; upserts never touch analysis-owned columns
(variant_label, theme_tag, asset_subtype); tombstone sweep scoped to ads seen
in this run's enabled inventory (paused-ad-group compositions are never
falsely tombstoned); full rollback on any API/DB error.

SCOPE v1: enabled campaigns + enabled ad groups, ALL ad statuses within them
(paused incumbents from pause-and-recreate loads keep their history and FK
parents). Ads inside paused ad groups are out of scope -- documented gap.
An ad group paused between runs rejoins scope on re-enable; its composition
history simply has a gap for the paused interval (visible in last_seen_date).
That gap is real history, not data loss.

Post-run assertions (gate Decision 4, amended 2026-07-22 to be persistable --
pure SQL across the two stored perf tables, checkable by anyone, any time):
  A4 [HARD]  cross-table invariant: every asset row's clicks/conversions
             <= its ad's row in rsa_ad_performance_daily, per
             (ad, device='ALL', report_date)
  A5 [PRINT] presence-conditioned duplication ratios from the stored tables:
             sum(asset rows) / sum(ad rows), per field_type
  A6 [WARN]  device rows sum ~= ALL rows per (ad, asset, date)

Usage:
  python 02_sync_rsa.py --db "C:\\Projects\\google-ads-ops\\db\\paid_search.db" --from 2026-01-01   # first backfill
  python 02_sync_rsa.py --db "C:\\Projects\\google-ads-ops\\db\\paid_search.db" --days 14           # weekly routine
"""
import argparse
import datetime
import hashlib
import os
import re
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

ENABLED_SCOPE = ("ad_group_ad.ad.type = 'RESPONSIVE_SEARCH_AD' "
                 "AND ad_group.status = 'ENABLED' AND campaign.status = 'ENABLED'")


def gaql(ga, query):
    rows = []
    for batch in ga.search_stream(customer_id=CUSTOMER_ID, query=query):
        rows.extend(batch.results)
    return rows


def fingerprint(headline_texts):
    """THE recipe -- must match 02_create_rsa_tables.sql exactly."""
    norm = sorted(re.sub(r"\s+", " ", h).strip().lower() for h in headline_texts)
    return hashlib.md5("||".join(norm).encode("utf-8")).hexdigest()[:10]


# ---------------------------------------------------------------------------
def sync_ads(ga, conn, run_date):
    q = f"""
        SELECT campaign.id, ad_group.id, ad_group_ad.ad.id, ad_group_ad.status,
               ad_group_ad.ad.responsive_search_ad.headlines
        FROM ad_group_ad
        WHERE {ENABLED_SCOPE}
    """
    rows = gaql(ga, q)
    ad_ids = set()
    for r in rows:
        fp = fingerprint([h.text for h in r.ad_group_ad.ad.responsive_search_ad.headlines])
        conn.execute(
            """INSERT INTO rsa_ads (account_id, ad_id, ad_group_id, campaign_id,
                                    ad_status, fingerprint,
                                    first_seen_date, last_seen_date, last_synced_at)
               VALUES (?,?,?,?,?,?,?,?,datetime('now'))
               ON CONFLICT(account_id, ad_id) DO UPDATE SET
                   ad_group_id    = excluded.ad_group_id,
                   campaign_id    = excluded.campaign_id,
                   ad_status      = excluded.ad_status,
                   fingerprint    = excluded.fingerprint,
                   last_seen_date = excluded.last_seen_date,
                   last_synced_at = excluded.last_synced_at""",
            # variant_label / theme_tag deliberately absent -- analysis-owned
            (ACCOUNT_ID, r.ad_group_ad.ad.id, r.ad_group.id, r.campaign.id,
             r.ad_group_ad.status.name, fp, run_date, run_date))
        ad_ids.add(r.ad_group_ad.ad.id)
    n_fp = conn.execute(
        "SELECT COUNT(DISTINCT fingerprint) FROM rsa_ads WHERE account_id=?",
        (ACCOUNT_ID,)).fetchone()[0]
    print("[ADS] upserted %d RSAs -> %d distinct fingerprints" % (len(rows), n_fp))
    return ad_ids


def sync_composition(ga, conn, run_date, run_marker, ad_ids):
    q = f"""
        SELECT ad_group_ad.ad.id, asset.id, asset.text_asset.text,
               ad_group_ad_asset_view.field_type,
               ad_group_ad_asset_view.pinned_field,
               ad_group_ad_asset_view.performance_label
        FROM ad_group_ad_asset_view
        WHERE {ENABLED_SCOPE}
    """
    rows = gaql(ga, q)
    for r in rows:
        v = r.ad_group_ad_asset_view
        conn.execute(
            """INSERT INTO assets (account_id, asset_id, asset_type, asset_name,
                                   status, approval_status,
                                   first_seen_date, last_seen_date, last_synced_at)
               VALUES (?,?,'TEXT',NULL,NULL,NULL,?,?,datetime('now'))
               ON CONFLICT(account_id, asset_id) DO UPDATE SET
                   last_seen_date = excluded.last_seen_date,
                   last_synced_at = excluded.last_synced_at""",
            (ACCOUNT_ID, r.asset.id, run_date, run_date))
        conn.execute(
            """INSERT INTO asset_text_data (account_id, asset_id, text, last_synced_at)
               VALUES (?,?,?,datetime('now'))
               ON CONFLICT(account_id, asset_id) DO UPDATE SET
                   text = excluded.text,
                   last_synced_at = excluded.last_synced_at""",
            # asset_subtype deliberately absent -- analysis-owned
            (ACCOUNT_ID, r.asset.id, r.asset.text_asset.text))
        pinned = v.pinned_field.name
        conn.execute(
            """INSERT INTO rsa_ad_assets (account_id, ad_id, asset_id, field_type,
                       pinned_field, performance_label,
                       first_seen_date, last_seen_date, removed_date, last_synced_at)
               VALUES (?,?,?,?,?,?,?,?,NULL,?)
               ON CONFLICT(account_id, ad_id, asset_id, field_type) DO UPDATE SET
                   pinned_field      = excluded.pinned_field,
                   performance_label = excluded.performance_label,
                   last_seen_date    = excluded.last_seen_date,
                   removed_date      = NULL,
                   last_synced_at    = excluded.last_synced_at""",
            (ACCOUNT_ID, r.ad_group_ad.ad.id, r.asset.id, v.field_type.name,
             None if pinned in ("UNSPECIFIED", "UNKNOWN") else pinned,
             v.performance_label.name, run_date, run_date, run_marker))

    # tombstone: composition rows for ads IN THIS RUN'S SCOPE that the pull
    # didn't touch. Never sweeps ads outside the enabled inventory.
    if ad_ids:
        marks = ",".join("?" * len(ad_ids))
        cur = conn.execute(
            f"""UPDATE rsa_ad_assets SET removed_date = ?
                WHERE account_id = ? AND removed_date IS NULL
                  AND last_synced_at != ? AND ad_id IN ({marks})""",
            [run_date, ACCOUNT_ID, run_marker] + list(ad_ids))
        if cur.rowcount:
            print("[COMP] tombstoned %d composition row(s)" % cur.rowcount)
    print("[COMP] upserted %d composition rows" % len(rows))


def sync_performance(ga, conn, date_from, date_to):
    passes = [("ALL", "", lambda r: "ALL"),
              ("device", ", segments.device", lambda r: r.segments.device.name)]
    n = 0
    for label, seg, get_dev in passes:
        q = f"""
            SELECT ad_group_ad.ad.id, asset.id,
                   ad_group_ad_asset_view.field_type, segments.date{seg},
                   metrics.impressions, metrics.clicks, metrics.cost_micros,
                   metrics.conversions, metrics.conversions_value
            FROM ad_group_ad_asset_view
            WHERE {ENABLED_SCOPE}
              AND segments.date BETWEEN '{date_from}' AND '{date_to}'
        """
        rows = gaql(ga, q)
        for r in rows:
            conn.execute(
                """INSERT INTO rsa_asset_performance_daily (account_id, ad_id,
                       asset_id, field_type, device, report_date,
                       impressions, clicks, cost_micros, conversions,
                       conversions_value, last_synced_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,datetime('now'))
                   ON CONFLICT(account_id, ad_id, asset_id, field_type, device, report_date)
                   DO UPDATE SET
                       impressions = excluded.impressions,
                       clicks = excluded.clicks,
                       cost_micros = excluded.cost_micros,
                       conversions = excluded.conversions,
                       conversions_value = excluded.conversions_value,
                       last_synced_at = excluded.last_synced_at""",
                (ACCOUNT_ID, r.ad_group_ad.ad.id, r.asset.id,
                 r.ad_group_ad_asset_view.field_type.name, get_dev(r),
                 str(r.segments.date), r.metrics.impressions, r.metrics.clicks,
                 r.metrics.cost_micros, r.metrics.conversions,
                 r.metrics.conversions_value))
        n += len(rows)
        print("[PERF] %s pass: %d rows" % (label, len(rows)))
    return n


# ---------------------------------------------------------------------------
def sync_ad_performance(ga, conn, date_from, date_to):
    """AD GRAIN (additive) -- same window, same transaction as the asset pull."""
    passes = [("ALL", "", lambda r: "ALL"),
              ("device", ", segments.device", lambda r: r.segments.device.name)]
    n = 0
    for label, seg, get_dev in passes:
        q = f"""
            SELECT ad_group_ad.ad.id, segments.date{seg},
                   metrics.impressions, metrics.clicks, metrics.cost_micros,
                   metrics.conversions, metrics.conversions_value
            FROM ad_group_ad
            WHERE {ENABLED_SCOPE}
              AND segments.date BETWEEN '{date_from}' AND '{date_to}'
        """
        rows = gaql(ga, q)
        for r in rows:
            conn.execute(
                """INSERT INTO rsa_ad_performance_daily (account_id, ad_id, device,
                       report_date, impressions, clicks, cost_micros,
                       conversions, conversions_value, last_synced_at)
                   VALUES (?,?,?,?,?,?,?,?,?,datetime('now'))
                   ON CONFLICT(account_id, ad_id, device, report_date)
                   DO UPDATE SET
                       impressions = excluded.impressions,
                       clicks = excluded.clicks,
                       cost_micros = excluded.cost_micros,
                       conversions = excluded.conversions,
                       conversions_value = excluded.conversions_value,
                       last_synced_at = excluded.last_synced_at""",
                (ACCOUNT_ID, r.ad_group_ad.ad.id, get_dev(r), str(r.segments.date),
                 r.metrics.impressions, r.metrics.clicks, r.metrics.cost_micros,
                 r.metrics.conversions, r.metrics.conversions_value))
        n += len(rows)
        print("[PERF] ad-grain %s pass: %d rows" % (label, len(rows)))
    return n


# ---------------------------------------------------------------------------
def run_assertions(conn, date_from, date_to):
    """Pure SQL across the two stored perf tables (gate 2026-07-22) --
    persistable and re-runnable by anyone, any time, without the API."""
    # A4: cross-table invariant per (ad, device='ALL', report_date)
    exceed = conn.execute(
        """SELECT COUNT(*)
           FROM rsa_asset_performance_daily a
           JOIN rsa_ad_performance_daily ad
             ON ad.account_id = a.account_id AND ad.ad_id = a.ad_id
            AND ad.device = 'ALL' AND ad.report_date = a.report_date
           WHERE a.account_id = ? AND a.device = 'ALL'
             AND a.report_date BETWEEN ? AND ?
             AND (a.clicks > ad.clicks + 1 OR a.conversions > ad.conversions + 0.51)""",
        (ACCOUNT_ID, date_from, date_to)).fetchone()[0]
    if exceed:
        print("[ASSERT A4] FAIL -- %d asset row(s) exceed their ad's stored totals" % exceed)
    else:
        print("[ASSERT A4] PASS -- cross-table invariant holds: every asset row "
              "<= its ad's rsa_ad_performance_daily row")

    # A5: duplication ratios from the stored tables (ads present in both)
    for ft in ("HEADLINE", "DESCRIPTION"):
        s = conn.execute(
            """SELECT SUM(clicks), SUM(conversions)
               FROM rsa_asset_performance_daily
               WHERE account_id=? AND device='ALL' AND field_type=?
                 AND report_date BETWEEN ? AND ?""",
            (ACCOUNT_ID, ft, date_from, date_to)).fetchone()
        t = conn.execute(
            """SELECT SUM(ad.clicks), SUM(ad.conversions)
               FROM rsa_ad_performance_daily ad
               WHERE ad.account_id=? AND ad.device='ALL'
                 AND ad.report_date BETWEEN ? AND ?
                 AND EXISTS (SELECT 1 FROM rsa_asset_performance_daily x
                             WHERE x.account_id=ad.account_id AND x.ad_id=ad.ad_id
                               AND x.field_type=? AND x.device='ALL'
                               AND x.report_date=ad.report_date)""",
            (ACCOUNT_ID, date_from, date_to, ft)).fetchone()
        t_clicks, t_conv = (t[0] or 1), (t[1] or 1)
        print("[ASSERT A5] %-11s duplication: clicks %.2fx, conversions %.2fx"
              % (ft, (s[0] or 0) / t_clicks, (s[1] or 0) / t_conv))

    # A6: device sum vs ALL (warn)
    n_drift = conn.execute(
        """SELECT COUNT(*)
           FROM rsa_asset_performance_daily a
           JOIN (SELECT ad_id, asset_id, field_type, report_date,
                        SUM(clicks) s FROM rsa_asset_performance_daily
                 WHERE account_id=? AND device != 'ALL'
                   AND report_date BETWEEN ? AND ?
                 GROUP BY 1,2,3,4) d
             USING (ad_id, asset_id, field_type, report_date)
           WHERE a.account_id=? AND a.device='ALL'
             AND a.report_date BETWEEN ? AND ?
             AND ABS(a.clicks - d.s) > MAX(2, 0.02 * a.clicks)""",
        (ACCOUNT_ID, date_from, date_to, ACCOUNT_ID, date_from, date_to)).fetchone()[0]
    print("[ASSERT A6] %s -- device rows vs ALL (%d key(s) drift > 2%%)"
          % ("WARN" if n_drift else "PASS", n_drift))
    return bool(exceed)


def main():
    p = argparse.ArgumentParser(description="RSA layer sync (Phase 2).")
    p.add_argument("--db", required=True, help="Path to paid_search.db (REQUIRED)")
    p.add_argument("--days", type=int, default=30)
    p.add_argument("--from", dest="date_from", default=None, metavar="YYYY-MM-DD",
                   help="Explicit window start, overrides --days (e.g. 2026-01-01)")
    args = p.parse_args()

    if not os.path.isfile(args.db):
        sys.exit("[ABORT] database not found: %s" % args.db)

    today = datetime.date.today()
    date_to = (today - datetime.timedelta(days=1)).isoformat()
    if args.date_from:
        datetime.date.fromisoformat(args.date_from)
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
    print("RSA LAYER SYNC  account_id=%d customer=%s  perf %s..%s"
          % (ACCOUNT_ID, CUSTOMER_ID, date_from, date_to))
    print("=" * 72)

    try:
        ad_ids = sync_ads(ga, conn, run_date)                 # parents first
        sync_composition(ga, conn, run_date, run_marker, ad_ids)
        sync_performance(ga, conn, date_from, date_to)
        sync_ad_performance(ga, conn, date_from, date_to)     # additive grain
        conn.commit()
    except (GoogleAdsException, sqlite3.Error) as e:
        conn.rollback()
        conn.close()
        sys.exit("[ABORT] sync failed, all changes rolled back: %s" % e)

    print()
    try:
        hard_fail = run_assertions(conn, date_from, date_to)
    finally:
        conn.close()

    if hard_fail:
        sys.exit("\n[RESULT] SYNC WROTE DATA BUT A4 FAILED -- investigate before consuming.")
    print("\n[RESULT] RSA sync complete, assertions passed.")


if __name__ == "__main__":
    main()
