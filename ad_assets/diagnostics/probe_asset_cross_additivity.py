#!/usr/bin/env python
"""
probe_asset_cross_additivity.py -- ad_assets/diagnostics/ -- READ-ONLY probe.

Prompted by the v1 sitelink-share failure (2026-07-17): summing SITELINKS
conversions ACROSS all sitelink assets produced 117-156% of total account
conversions -- impossible, therefore asset-grain metrics are duplicated
across co-serving assets. The earlier partition ruling (A1) proved
click_type additivity WITHIN one asset's rows only; this probe quantifies
what happens ACROSS assets, so the gate can codify the rule.

Method: same window, two sources of truth for "sitelink-click activity":
  A. ASSET GRAIN: SUM over all sitelink assets (customer_asset +
     campaign_asset) of clicks/conversions on click_type='SITELINKS' rows.
  B. CAMPAIGN REPORT: FROM campaign, segments.click_type='SITELINKS'
     totals -- a clean partition of real clicks/conversions.

If A >> B, per-asset rows duplicate the same events across every sitelink
shown on the impression; the ratio A/B estimates the average number of
sitelinks co-serving. Expected outcome: clicks may or may not duplicate,
conversions clearly do (that's what broke the share chart).

Proposed gate ruling if confirmed: DDL comment amendment on
asset_performance_daily -- "metrics are NOT additive across assets; any
account- or campaign-wide total comes from the campaign report's
click_type partition, never from summing asset rows. Asset rows support
per-asset and relative comparisons only."

    python probe_asset_cross_additivity.py           # LAST_30_DAYS
"""
import os
import sys
from dotenv import load_dotenv

load_dotenv()

try:
    from google.ads.googleads.client import GoogleAdsClient
except ImportError:
    sys.exit("[ABORT] google-ads library not found -- run inside the paid-search conda env.")

CUSTOMER_ID = os.environ.get("PROBE_CUSTOMER_ID", "8886502147").replace("-", "")
DATE_RANGE = "LAST_30_DAYS"


def gaql(ga, query):
    rows = []
    for batch in ga.search_stream(customer_id=CUSTOMER_ID, query=query):
        rows.extend(batch.results)
    return rows


def main():
    client = GoogleAdsClient.load_from_storage()
    ga = client.get_service("GoogleAdsService")

    print("=" * 72)
    print("CROSS-ASSET ADDITIVITY PROBE  (customer %s, %s)" % (CUSTOMER_ID, DATE_RANGE))
    print("=" * 72)

    # A: asset grain, summed across ALL sitelink assets, both live levels
    a_clicks = a_conv = 0.0
    for resource in ("customer_asset", "campaign_asset"):
        q = f"""
            SELECT asset.id, segments.click_type, metrics.clicks, metrics.conversions
            FROM {resource}
            WHERE asset.type = 'SITELINK'
              AND segments.date DURING {DATE_RANGE}
        """
        for r in gaql(ga, q):
            if r.segments.click_type.name == "SITELINKS":
                a_clicks += r.metrics.clicks
                a_conv += r.metrics.conversions

    # B: campaign report, clean click_type partition
    b_clicks = b_conv = t_clicks = t_conv = 0.0
    q = f"""
        SELECT segments.click_type, metrics.clicks, metrics.conversions
        FROM campaign
        WHERE segments.date DURING {DATE_RANGE}
    """
    for r in gaql(ga, q):
        t_clicks += r.metrics.clicks
        t_conv += r.metrics.conversions
        if r.segments.click_type.name == "SITELINKS":
            b_clicks += r.metrics.clicks
            b_conv += r.metrics.conversions

    print("\n%-38s %12s %12s" % ("", "clicks", "conversions"))
    print("%-38s %12.0f %12.1f" % ("A: asset-grain sum (all sitelinks)", a_clicks, a_conv))
    print("%-38s %12.0f %12.1f" % ("B: campaign-report SITELINKS", b_clicks, b_conv))
    print("%-38s %12.0f %12.1f" % ("account total (all click types)", t_clicks, t_conv))

    print("\nVERDICT:")
    for name, a, b in (("clicks", a_clicks, b_clicks), ("conversions", a_conv, b_conv)):
        if b == 0:
            print("  %s: campaign report shows zero -- widen window" % name)
        elif a / b > 1.1:
            print("  %s: DUPLICATED across assets -- ratio %.2fx (≈ avg sitelinks "
                  "co-serving). NEVER sum %s across asset rows." % (name, a / b, name))
        else:
            print("  %s: additive across assets within 10%% (ratio %.2fx) -- "
                  "per-asset attribution is exclusive for %s." % (name, a / b, name))

    print("\nTrue sitelink share of conversions (campaign report): %.1f%%"
          % (100.0 * b_conv / t_conv if t_conv else 0))
    print("Paste full output to the DB architect gate for the DDL-comment amendment.")


if __name__ == "__main__":
    main()
