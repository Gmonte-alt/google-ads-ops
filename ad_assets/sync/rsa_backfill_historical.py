#!/usr/bin/env python
"""
rsa_backfill_historical.py -- ad_assets/sync/ -- ONE-TIME widened-scope
backfill of rsa_ad_performance_daily (+ rsa_ads inventory) back to the
probed horizon (Q12: data reaches 2024-04; Jul-2024 inflection recoverable
with the Geico cohort). GATE ITEM: this deliberately deviates from the
weekly sync's enabled-only v1 scope -- NO campaign/ad-group status filters,
because the 2024 history lives in ad groups that are paused/removed today.

DESIGN (per gate memo):
  * AD GRAIN ONLY, device='ALL' pass only (~1.6-1.7M rows est. from probe).
    Asset grain deliberately NOT backfilled (~10M rows, no consumer).
  * COVERAGE TIER (gate 2026-08-12, document wherever this table is consumed):
    rsa_ad_performance_daily device-split rows exist 2026+ only (weekly
    sync); historical 2024-2025 rows are device='ALL' ONLY. A device cut on
    RSA history returns silence by design, not by bug. (Also add this note
    as a comment on the table in 02_create_rsa_tables.sql in the repo.)
  * CONVERSION DEFINITIONS: conversions / conversions_value are
    Google-native, so 2024 rows reflect 2024's goal configuration (the
    EverQuote era). Serve-shape and volume are era-safe; any CROSS-ERA
    conversion comparison carries the partner-transition caveat that
    footnotes everything else spanning it.
  * Month-chunked, committed per chunk -- resumable (upserts are idempotent;
    re-running a month is safe).
  * Inventory derived FROM the perf window itself: each month's perf pull
    collects ad_ids; definitions (headlines -> fingerprint, ad_group_id,
    campaign_id, status) are fetched for ids not yet in rsa_ads and upserted
    PARENTS-FIRST. Historical ads land unlabeled (they print as fingerprints
    in readouts -- correct, they are pre-label generations).
  * FINGERPRINT CAVEAT (documented): the API exposes an ad's CURRENT
    definition only. For ads edited after their serving era, the fingerprint
    reflects post-edit copy. Most SKAG-era ads were paused, not edited;
    treat historical fingerprints as best-available identity, not gospel.
  * Weekly routine untouched: tombstone sweeps are scoped to each run's own
    inventory; enabled-only tools filter on ad_status and are unaffected.

  python rsa_backfill_historical.py --db "...\\paid_search.db" --from 2024-04-01 --to 2025-12-31
  python rsa_backfill_historical.py --db "...\\paid_search.db" --from 2024-04-01 --to 2025-12-31 --apply
"""
import argparse
import datetime
import hashlib
import os
import re
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


def gaql(ga, query):
    rows = []
    for batch in ga.search_stream(customer_id=CUSTOMER_ID, query=query):
        rows.extend(batch.results)
    return rows


def fingerprint(headline_texts):
    norm = sorted(re.sub(r"\s+", " ", h).strip().lower() for h in headline_texts)
    return hashlib.md5("||".join(norm).encode("utf-8")).hexdigest()[:10]


def month_chunks(date_from, date_to):
    d = datetime.date.fromisoformat(date_from)
    end = datetime.date.fromisoformat(date_to)
    while d <= end:
        nxt = (d.replace(day=28) + datetime.timedelta(days=4)).replace(day=1)
        yield d.isoformat(), min(nxt - datetime.timedelta(days=1), end).isoformat()
        d = nxt


def fetch_definitions(ga, conn, new_ids, run_date):
    """Upsert rsa_ads rows for ads not yet known (parents-first)."""
    ids = sorted(new_ids)
    n = 0
    for i in range(0, len(ids), 1000):
        chunk = ids[i:i + 1000]
        rows = gaql(ga, f"""
            SELECT campaign.id, ad_group.id, ad_group_ad.ad.id, ad_group_ad.status,
                   ad_group_ad.ad.responsive_search_ad.headlines
            FROM ad_group_ad
            WHERE ad_group_ad.ad.id IN ({",".join(str(x) for x in chunk)})
        """)
        for r in rows:
            fp = fingerprint([h.text for h in r.ad_group_ad.ad.responsive_search_ad.headlines])
            conn.execute(
                """INSERT INTO rsa_ads (account_id, ad_id, ad_group_id, campaign_id,
                       ad_status, fingerprint, first_seen_date, last_seen_date,
                       last_synced_at)
                   VALUES (?,?,?,?,?,?,?,?,datetime('now'))
                   ON CONFLICT(account_id, ad_id) DO NOTHING""",
                (ACCOUNT_ID, r.ad_group_ad.ad.id, r.ad_group.id, r.campaign.id,
                 r.ad_group_ad.status.name, fp, run_date, run_date))
            n += 1
    return n


def main():
    p = argparse.ArgumentParser(description="One-time widened-scope RSA ad-grain backfill.")
    p.add_argument("--db", required=True)
    p.add_argument("--from", dest="date_from", required=True, metavar="YYYY-MM-DD")
    p.add_argument("--to", dest="date_to", required=True, metavar="YYYY-MM-DD")
    p.add_argument("--apply", action="store_true",
                   help="Write to the DB (default: dry-run -- prints per-month row "
                        "counts from the API without writing)")
    args = p.parse_args()
    if not os.path.isfile(args.db):
        sys.exit("[ABORT] database not found: %s" % args.db)
    datetime.date.fromisoformat(args.date_from)
    datetime.date.fromisoformat(args.date_to)
    run_date = datetime.date.today().isoformat()

    client = GoogleAdsClient.load_from_storage()
    ga = client.get_service("GoogleAdsService")
    conn = sqlite3.connect(args.db)
    conn.execute("PRAGMA foreign_keys = ON")
    known = {r[0] for r in conn.execute(
        "SELECT ad_id FROM rsa_ads WHERE account_id=?", (ACCOUNT_ID,))}
    print("=" * 72)
    print("HISTORICAL RSA BACKFILL (widened scope, ad grain, device=ALL)")
    print("window %s..%s | %s | known ads: %d"
          % (args.date_from, args.date_to,
             "APPLY" if args.apply else "DRY RUN", len(known)))
    print("=" * 72)

    tot_rows = tot_ads = 0
    for m_from, m_to in month_chunks(args.date_from, args.date_to):
        try:
            rows = gaql(ga, f"""
                SELECT ad_group_ad.ad.id, segments.date,
                       metrics.impressions, metrics.clicks, metrics.cost_micros,
                       metrics.conversions, metrics.conversions_value
                FROM ad_group_ad
                WHERE ad_group_ad.ad.type = 'RESPONSIVE_SEARCH_AD'
                  AND segments.date BETWEEN '{m_from}' AND '{m_to}'
                  AND metrics.impressions > 0
            """)
        except GoogleAdsException as e:
            conn.close()
            sys.exit("[ABORT] API error in %s (chunks before it are committed; "
                     "re-run with --from %s): %s" % (m_from, m_from, e))
        new_ids = {r.ad_group_ad.ad.id for r in rows} - known
        print("  %s  perf rows: %7d | new ads: %5d %s"
              % (m_from[:7], len(rows), len(new_ids), "" if args.apply else "(dry)"))
        tot_rows += len(rows)
        if not args.apply:
            continue
        n_def = fetch_definitions(ga, conn, new_ids, run_date)   # parents first
        known |= new_ids
        tot_ads += n_def
        for r in rows:
            conn.execute(
                """INSERT INTO rsa_ad_performance_daily (account_id, ad_id, device,
                       report_date, impressions, clicks, cost_micros,
                       conversions, conversions_value, last_synced_at)
                   VALUES (?,?,'ALL',?,?,?,?,?,?,datetime('now'))
                   ON CONFLICT(account_id, ad_id, device, report_date)
                   DO UPDATE SET impressions=excluded.impressions,
                       clicks=excluded.clicks, cost_micros=excluded.cost_micros,
                       conversions=excluded.conversions,
                       conversions_value=excluded.conversions_value,
                       last_synced_at=excluded.last_synced_at""",
                (ACCOUNT_ID, r.ad_group_ad.ad.id, str(r.segments.date),
                 r.metrics.impressions, r.metrics.clicks, r.metrics.cost_micros,
                 r.metrics.conversions, r.metrics.conversions_value))
        conn.commit()   # per-month commit -- resumable

    if args.apply:
        print("\n[ACCEPT] run check_geico_acceptance.py for the acceptance evidence")
        print("         (fast indexed version; the embedded query was retired after")
        print("         it proved pathologically slow on the full-size DB).")
    conn.close()
    print("[RESULT] %s: %s rows%s across window."
          % ("written" if args.apply else "would write", f"{tot_rows:,}",
             (", %d new rsa_ads" % tot_ads) if args.apply else ""))
    if not args.apply:
        print("Re-run with --apply after the gate ruling.")


if __name__ == "__main__":
    main()
