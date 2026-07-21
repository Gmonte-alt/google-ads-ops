#!/usr/bin/env python
"""
probe_asset_click_type.py -- ad_assets/diagnostics/ -- READ-ONLY Google Ads API probe.

Follow-up to probe_asset_grain.py, prompted by Gilbert's flag (2026-07-17): asset
report metrics count activity on impressions where the asset was SHOWN -- a "click"
on a sitelink row can be a click on the headline, not the sitelink. Before trusting
any asset-level ROAS (and before the asset_performance_daily PK is applied), we need
to know whether the API splits this out at asset grain.

Questions:
  Q4. Is segments.click_type selectable FROM campaign_asset / customer_asset?
  Q5. Which click_type values actually appear for SITELINK assets, and what share
      of clicks/conversions land on the sitelink itself vs the rest of the ad?
  Q6. Do the click_type-segmented rows sum back to the unsegmented totals
      (i.e. is it a clean partition we can store alongside device='ALL' style)?

If Q4 is YES: click_type joins device as a PK dimension in asset_performance_daily
(with click_type='ALL' for unsegmented passes) -- gate that ruling with the DB
architect BEFORE apply_asset_schema.py runs with --apply.

Enum rule: all enum values printed via .name (never raw ints, never hand maps).

    python probe_asset_click_type.py           # HOME account per .env, LAST_30_DAYS
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

CUSTOMER_ID = os.environ.get("PROBE_CUSTOMER_ID", "8886502147").replace("-", "")
DATE_RANGE = "LAST_30_DAYS"

BASE_METRICS = ("metrics.impressions, metrics.clicks, metrics.cost_micros, "
                "metrics.conversions, metrics.conversions_value")


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


def summarize_by_click_type(rows, label):
    agg = defaultdict(lambda: {"impr": 0, "clicks": 0, "conv": 0.0, "value": 0.0})
    for r in rows:
        ct = r.segments.click_type.name          # enum .name -- house rule
        agg[ct]["impr"] += r.metrics.impressions
        agg[ct]["clicks"] += r.metrics.clicks
        agg[ct]["conv"] += r.metrics.conversions
        agg[ct]["value"] += r.metrics.conversions_value
    total_clicks = sum(v["clicks"] for v in agg.values()) or 1
    total_conv = sum(v["conv"] for v in agg.values()) or 1
    print("\n  %s -- split by click_type:" % label)
    for ct, v in sorted(agg.items(), key=lambda kv: -kv[1]["clicks"]):
        print("    %-28s impr %8d  clicks %6d (%5.1f%%)  conv %8.1f (%5.1f%%)  val %9.0f"
              % (ct, v["impr"], v["clicks"], 100.0 * v["clicks"] / total_clicks,
                 v["conv"], 100.0 * v["conv"] / total_conv, v["value"]))
    return agg


def main():
    client = GoogleAdsClient.load_from_storage()
    ga = client.get_service("GoogleAdsService")

    print("=" * 72)
    print("ASSET CLICK_TYPE PROBE  (customer %s, %s)" % (CUSTOMER_ID, DATE_RANGE))
    print("=" * 72)

    # ---- baseline: unsegmented campaign_asset totals (for the Q6 sum check) ----
    q_base = f"""
        SELECT campaign.id, asset.id, {BASE_METRICS}
        FROM campaign_asset
        WHERE asset.type = 'SITELINK'
          AND segments.date DURING {DATE_RANGE}
    """
    rows_base = run_query(ga, q_base, "campaign_asset / SITELINK / unsegmented (baseline)")

    # ---- Q4/Q5: campaign_asset WITH segments.click_type ----
    q_ct = f"""
        SELECT campaign.id, asset.id, segments.click_type, {BASE_METRICS}
        FROM campaign_asset
        WHERE asset.type = 'SITELINK'
          AND segments.date DURING {DATE_RANGE}
    """
    rows_ct = run_query(ga, q_ct, "campaign_asset / SITELINK / WITH segments.click_type")

    # ---- same on customer_asset (the 36-asset population) ----
    q_cu = f"""
        SELECT asset.id, segments.click_type, {BASE_METRICS}
        FROM customer_asset
        WHERE asset.type = 'SITELINK'
          AND segments.date DURING {DATE_RANGE}
    """
    rows_cu = run_query(ga, q_cu, "customer_asset / SITELINK / WITH segments.click_type")

    # ---- verdicts ----
    print("\n" + "=" * 72)
    print("VERDICTS")
    print("=" * 72)

    if rows_ct is None:
        print("Q4 click_type at asset grain: NOT SUPPORTED on campaign_asset "
              "(API rejected segment) -- PK stays as ruled; click-attribution "
              "must come from another view if it exists at all.")
    else:
        print("Q4 click_type at asset grain: SUPPORTED on campaign_asset.")
        summarize_by_click_type(rows_ct, "campaign_asset")

        if rows_base:
            base_clicks = sum(r.metrics.clicks for r in rows_base)
            base_conv = sum(r.metrics.conversions for r in rows_base)
            ct_clicks = sum(r.metrics.clicks for r in rows_ct)
            ct_conv = sum(r.metrics.conversions for r in rows_ct)
            print("\nQ6 partition check: unsegmented clicks=%d vs click_type-summed=%d; "
                  "conv %.1f vs %.1f %s"
                  % (base_clicks, ct_clicks, base_conv, ct_conv,
                     "-- CLEAN partition (store like device: 'ALL' + segmented passes)"
                     if abs(base_clicks - ct_clicks) <= max(2, 0.01 * base_clicks)
                     else "-- MISMATCH: segmented rows do not sum to totals; treat "
                          "click_type rows as a separate lens, keep 'ALL' authoritative"))

    if rows_cu is not None:
        print("\ncustomer_asset (account-level sitelinks):")
        if rows_cu:
            summarize_by_click_type(rows_cu, "customer_asset")
        else:
            print("  query valid, zero rows -- widen date range before concluding")
    else:
        print("\ncustomer_asset: click_type segment REJECTED at account-association level.")

    print("\nDone. Paste full output to the DB architect gate. If Q4=SUPPORTED, the "
          "proposed ruling is: add click_type to the asset_performance_daily PK "
          "(default 'ALL'), sync passes: (ALL,ALL), (device,ALL), (ALL,click_type). "
          "Decide BEFORE apply_asset_schema.py --apply.")


if __name__ == "__main__":
    main()
