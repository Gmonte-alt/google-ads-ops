#!/usr/bin/env python
"""
probe_rsa_backfill_horizon.py -- ad_assets/diagnostics/ -- Q12 (Track B):
how far back can rsa_asset_performance_daily / rsa_ad_performance_daily be
backfilled, and is the GEICO COHORT visible at each depth?

For monthly checkpoints from Jan 2024 forward, queries one week of data with
NO campaign/ad-group status filters (historical stats for paused/removed
entities are the whole point -- the 2024 Geico SKAG groups are not enabled
today), at both grains:

  asset grain: ad_group_ad_asset_view  (feeds rsa_asset_performance_daily)
  ad grain:    ad_group_ad             (feeds rsa_ad_performance_daily)

and, per checkpoint, the same counts restricted to ad_group.name LIKE
'%Geico%' -- the cohort Track B's QS-vs-change overlay needs.

VERDICT: earliest checkpoint with data per grain; explicit YES/NO on whether
the Jul-2024 inflection is recoverable (data on BOTH sides of 2024-07) and
whether the Geico cohort is present there. Paste full output to the
change-history thread AND the DB-architect gate (the backfill will need a
widened-scope variant of 02_sync_rsa -- a documented deviation from the
enabled-only v1 scope ruling).

  python probe_rsa_backfill_horizon.py
"""
import os
import sys

from dotenv import load_dotenv

load_dotenv()

try:
    from google.ads.googleads.client import GoogleAdsClient
    from google.ads.googleads.errors import GoogleAdsException
except ImportError:
    sys.exit("[ABORT] google-ads library not found -- run inside the paid-search conda env.")

CUSTOMER_ID = os.environ.get("PROBE_CUSTOMER_ID", "8886502147").replace("-", "")

CHECKPOINTS = ["2024-01", "2024-04", "2024-06", "2024-07", "2024-08",
               "2024-10", "2025-01", "2025-04", "2025-07", "2025-10", "2026-01"]


def count(ga, query):
    try:
        n_rows, ads = 0, set()
        for batch in ga.search_stream(customer_id=CUSTOMER_ID, query=query):
            for r in batch.results:
                n_rows += 1
                ads.add(r.ad_group_ad.ad.id)
        return n_rows, len(ads), None
    except GoogleAdsException as e:
        return None, None, "; ".join(err.message for err in e.failure.errors)[:110]


def main():
    client = GoogleAdsClient.load_from_storage()
    ga = client.get_service("GoogleAdsService")
    print("=" * 78)
    print("Q12: RSA BACKFILL HORIZON + GEICO COHORT VISIBILITY  (customer %s)" % CUSTOMER_ID)
    print("    (one-week slices, NO status filters -- paused/removed scope included)")
    print("=" * 78)
    print("%-9s | %22s | %22s | %s" % ("month", "asset grain (rows/ads)",
                                       "ad grain (rows/ads)", "Geico ad-grain (rows/ads)"))
    print("-" * 78)

    first_asset = first_ad = first_geico = None
    results = {}
    for cp in CHECKPOINTS:
        d1, d2 = cp + "-01", cp + "-07"
        a_rows, a_ads, a_err = count(ga, f"""
            SELECT ad_group_ad.ad.id, segments.date, metrics.impressions
            FROM ad_group_ad_asset_view
            WHERE segments.date BETWEEN '{d1}' AND '{d2}'
              AND metrics.impressions > 0
        """)
        g_rows, g_ads, g_err = count(ga, f"""
            SELECT ad_group_ad.ad.id, segments.date, metrics.impressions
            FROM ad_group_ad
            WHERE segments.date BETWEEN '{d1}' AND '{d2}'
              AND metrics.impressions > 0
        """)
        geo_rows, geo_ads, geo_err = count(ga, f"""
            SELECT ad_group_ad.ad.id, ad_group.name, segments.date, metrics.impressions
            FROM ad_group_ad
            WHERE segments.date BETWEEN '{d1}' AND '{d2}'
              AND ad_group.name LIKE '%Geico%'
              AND metrics.impressions > 0
        """)
        results[cp] = (a_rows, g_rows, geo_rows)
        def fmt(rows, ads, err):
            if err:
                return "ERR: %s" % err[:18]
            return "%d / %d" % (rows, ads) if rows else "-- none --"
        print("%-9s | %22s | %22s | %s"
              % (cp, fmt(a_rows, a_ads, a_err), fmt(g_rows, g_ads, g_err),
                 fmt(geo_rows, geo_ads, geo_err)))
        if a_rows and not first_asset:
            first_asset = cp
        if g_rows and not first_ad:
            first_ad = cp
        if geo_rows and not first_geico:
            first_geico = cp

    print("\nVERDICT:")
    print("  asset grain reaches back to: %s" % (first_asset or "no data in range"))
    print("  ad grain reaches back to:    %s" % (first_ad or "no data in range"))
    print("  Geico cohort visible from:   %s" % (first_geico or "no data in range"))
    pre = results.get("2024-06", (0, 0, 0))
    post = results.get("2024-08", (0, 0, 0))
    jul_ok = bool((pre[1] or 0) and (post[1] or 0))
    geico_jul = bool((pre[2] or 0) and (post[2] or 0))
    print("  Jul-2024 inflection recoverable (ad grain both sides): %s"
          % ("YES" if jul_ok else "NO -- inflection stays inferred-not-proven; tell Ben"))
    print("  ...with the Geico cohort specifically: %s"
          % ("YES" if geico_jul else "NO -- cohort not visible across the inflection"))
    if first_ad:
        print("\n  Next: widened-scope backfill (no status filters, --from %s-01) through"
              % first_ad)
        print("  the DB-architect gate as a documented deviation from the enabled-only")
        print("  v1 scope; rsa_ads inventory must include the historical (paused/removed)")
        print("  ads so FK parents and ad_group_id joins exist for the whole window.")


if __name__ == "__main__":
    main()
