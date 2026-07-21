#!/usr/bin/env python
"""
probe_asset_grain.py -- ad_assets/diagnostics/ -- READ-ONLY Google Ads API probe.

Answers the three grain questions that block the asset-DB schema commit (May design,
flagged "verify before committing the primary key"):

  Q1. Does asset performance support DEVICE segmentation?
  Q2. Is performance reported PER ASSOCIATION LEVEL (customer vs campaign vs ad group
      for the same asset), i.e. can the same asset_id appear at multiple levels with
      separate metrics?
  Q3. Are CONVERSION metrics populated at asset grain, or only impressions/clicks?

Method: queries campaign_asset (and ad_group_asset / customer_asset) for SITELINK assets
over the last 30 days, once without and once with segments.device, and inspects which
metric fields come back populated. Prints a verdict per question.

Auth mirrors 03/05/18: load_dotenv() -> GOOGLE_ADS_CONFIGURATION_FILE_PATH -> load_from_storage.
Read-only (search stream only). No DB writes.

    python probe_asset_grain.py            # uses HOME account from .env mapping
"""
import os
import sys
from collections import defaultdict

from dotenv import load_dotenv

load_dotenv()

try:
    from google.ads.googleads.client import GoogleAdsClient
    from google.ads.googleads.errors import GoogleAdsException
except ImportError:
    sys.exit("[ABORT] google-ads library not found -- run inside the paid-search conda env.")

# HOME account customer id (same id 03/05 use; 8886502147 per the 03 sync output)
CUSTOMER_ID = os.environ.get("PROBE_CUSTOMER_ID", "8886502147").replace("-", "")

DATE_RANGE = "LAST_30_DAYS"

BASE_METRICS = ("metrics.impressions, metrics.clicks, metrics.cost_micros, "
                "metrics.conversions, metrics.conversions_value, "
                "metrics.all_conversions, metrics.all_conversions_value")


def run_query(ga, query, label):
    print("\n" + "-" * 72)
    print("QUERY: %s" % label)
    print("-" * 72)
    rows = []
    try:
        stream = ga.search_stream(customer_id=CUSTOMER_ID, query=query)
        for batch in stream:
            for row in batch.results:
                rows.append(row)
    except GoogleAdsException as e:
        for err in e.failure.errors:
            print("  [API ERROR] %s" % err.message)
        return None
    print("  rows returned: %d" % len(rows))
    return rows


def main():
    client = GoogleAdsClient.load_from_storage()
    ga = client.get_service("GoogleAdsService")

    print("=" * 72)
    print("ASSET PERFORMANCE GRAIN PROBE  (customer %s, %s)" % (CUSTOMER_ID, DATE_RANGE))
    print("=" * 72)

    # ---- Q2/Q3: campaign-level association, no device ----
    q_campaign = f"""
        SELECT campaign.id, campaign.name,
               asset.id, asset.type, asset.sitelink_asset.link_text,
               {BASE_METRICS}
        FROM campaign_asset
        WHERE asset.type = 'SITELINK'
          AND segments.date DURING {DATE_RANGE}
    """
    rows_c = run_query(ga, q_campaign, "campaign_asset / SITELINK / no device segment")

    # ---- Q1: same query WITH device segment ----
    q_device = f"""
        SELECT campaign.id, asset.id, segments.device,
               {BASE_METRICS}
        FROM campaign_asset
        WHERE asset.type = 'SITELINK'
          AND segments.date DURING {DATE_RANGE}
    """
    rows_d = run_query(ga, q_device, "campaign_asset / SITELINK / WITH segments.device")

    # ---- Q2: customer-level association for the same assets ----
    q_customer = f"""
        SELECT asset.id, asset.type,
               {BASE_METRICS}
        FROM customer_asset
        WHERE asset.type = 'SITELINK'
          AND segments.date DURING {DATE_RANGE}
    """
    rows_cu = run_query(ga, q_customer, "customer_asset / SITELINK (account-level associations)")

    # ---- verdicts ----
    print("\n" + "=" * 72)
    print("VERDICTS")
    print("=" * 72)

    if rows_d is not None:
        devices = {str(r.segments.device) for r in rows_d} if rows_d else set()
        print("Q1 device segmentation: %s"
              % ("SUPPORTED -- devices seen: %s" % sorted(devices) if rows_d
                 else "query valid but zero rows -- inconclusive, widen date range"))
    else:
        print("Q1 device segmentation: NOT SUPPORTED at campaign_asset grain (API rejected segment)")

    if rows_c:
        # do any asset_ids appear at both campaign and customer level with metrics?
        c_assets = {r.asset.id for r in rows_c}
        cu_assets = {r.asset.id for r in (rows_cu or [])}
        overlap = c_assets & cu_assets
        print("Q2 association-level split: campaign-level assets=%d, customer-level assets=%d, "
              "overlap=%d %s"
              % (len(c_assets), len(cu_assets), len(overlap),
                 "-- SAME asset reports separately per level (store association_level in PK)"
                 if overlap else "-- no overlap seen; levels appear disjoint here"))

        # Q3: conversion population
        with_conv = sum(1 for r in rows_c if r.metrics.conversions > 0)
        with_clicks = sum(1 for r in rows_c if r.metrics.clicks > 0)
        print("Q3 conversions at asset grain: %d/%d rows have clicks; %d/%d have conversions>0 %s"
              % (with_clicks, len(rows_c), with_conv, len(rows_c),
                 "-- conversions POPULATED at asset grain" if with_conv
                 else "-- zero conversions everywhere: either attribution not reported at "
                      "asset grain, or genuinely no asset-attributed conversions; check a "
                      "high-volume window before concluding"))

        # sample
        print("\nsample rows (campaign_asset):")
        for r in rows_c[:5]:
            print("  camp %s | asset %s | '%s' | impr %s clicks %s conv %.1f val %.0f"
                  % (r.campaign.id, r.asset.id,
                     r.asset.sitelink_asset.link_text[:28],
                     r.metrics.impressions, r.metrics.clicks,
                     r.metrics.conversions, r.metrics.conversions_value))

    print("\nDone. Paste this full output to the DB architect thread -- the PK for "
          "asset_performance_daily follows from Q1/Q2, and the GP-join viability from Q3.")


if __name__ == "__main__":
    main()
