#!/usr/bin/env python
"""
probe_interaction_target.py -- ad_assets/diagnostics/ -- Q13: does the API
expose the UI's "This extension vs. Other" segmentation (per-asset
interaction) on our sync surfaces? If YES, the syncs can store TRUE
this-asset clicks and the workbook's CTR/ROAS will reconcile with the UI.

Tests segments.asset_interaction_target on customer_asset (campaign-split
source) and on the link-grain surfaces, then reconciles one asset against
the UI export's numbers (Rates For Seniors, CA, Aug 4 - Sep 2:
this-extension clicks 525 / cost $2,651 / value $2,885).

  python probe_interaction_target.py
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

def try_query(ga, label, query):
    try:
        rows = []
        for batch in ga.search_stream(customer_id=CUSTOMER_ID, query=query):
            rows.extend(batch.results)
        print("  %-46s ACCEPTED -- %d rows" % (label, len(rows)))
        return rows
    except GoogleAdsException as e:
        print("  %-46s REJECTED -- %s"
              % (label, "; ".join(er.message for er in e.failure.errors)[:100]))
        return None

def main():
    client = GoogleAdsClient.load_from_storage()
    ga = client.get_service("GoogleAdsService")
    print("=" * 74)
    print("Q13: asset_interaction_target (the UI's 'This extension' segment)")
    print("=" * 74)
    rows = try_query(ga, "customer_asset + interaction_target", """
        SELECT asset.id, campaign.id,
               segments.asset_interaction_target.asset,
               segments.asset_interaction_target.interaction_on_this_asset,
               metrics.clicks, metrics.cost_micros, metrics.conversions_value
        FROM customer_asset
        WHERE customer_asset.field_type = 'SITELINK'
          AND segments.date BETWEEN '2026-08-04' AND '2026-09-02'
    """)
    if rows:
        # reconcile the sample asset against the UI export
        agg = {}
        for r in rows:
            key = (r.asset.id, r.campaign.id,
                   r.segments.asset_interaction_target.interaction_on_this_asset)
            a = agg.setdefault(key, [0, 0, 0.0])
            a[0] += r.metrics.clicks
            a[1] += r.metrics.cost_micros
            a[2] += r.metrics.conversions_value
        ca = [k for k in agg if k[1] == 23385188622 and k[2]]
        ca.sort(key=lambda k: -agg[k][0])
        print("  top CA this-asset rows (expect Rates For Seniors ~525 clicks / $2,651 / $2,885):")
        for k in ca[:3]:
            c, cost, val = agg[k]
            print("    asset %-14d clicks %5d  cost $%s  value $%s"
                  % (k[0], c, f"{cost/1e6:,.0f}", f"{val:,.0f}"))
        print("\nVERDICT: GO -- add interaction-filtered passes to 01/01b (gate memo:")
        print("new click_type-style rows storing this-asset metrics; A1 exclusion noted).")
    else:
        print("\nVERDICT: segment not available on customer_asset in this version --")
        print("workbook keeps the corrected 'direct' definition; UI export remains")
        print("the per-asset-interaction source when that precision is needed.")

if __name__ == "__main__":
    main()
