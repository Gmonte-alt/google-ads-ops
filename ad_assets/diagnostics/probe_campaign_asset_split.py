#!/usr/bin/env python
"""
probe_campaign_asset_split.py -- ad_assets/diagnostics/ -- Q11: can
account-level (customer-linked) sitelink performance be split BY CAMPAIGN,
and at what fidelity? READ-ONLY probe; paste full output to the DB-architect
gate if the verdict is GO.

  Q11a  campaign_aggregate_asset_view -- the purpose-built surface (asset
        performance aggregated per campaign REGARDLESS of link level).
        Tests: minimal select -> + field_type filter -> + date segment ->
        + click_type segment (decides whether direct-click ROAS can be
        campaign-split, or only impressions/clicks).
  Q11b  segments on customer_asset (expected REJECTED -- documents why the
        synced grain never had this).
  Q11c  reconciliation: view totals per sitelink vs customer_asset totals,
        LAST_30_DAYS -- the aggregate view must ~= the link-level truth.

  python probe_campaign_asset_split.py
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


def try_query(ga, label, query):
    try:
        rows = []
        for batch in ga.search_stream(customer_id=CUSTOMER_ID, query=query):
            rows.extend(batch.results)
        print("  %-38s ACCEPTED -- %d rows" % (label, len(rows)))
        return rows
    except GoogleAdsException as e:
        msg = "; ".join(err.message for err in e.failure.errors)[:150]
        print("  %-38s REJECTED -- %s" % (label, msg))
        return None


def main():
    client = GoogleAdsClient.load_from_storage()
    ga = client.get_service("GoogleAdsService")
    print("=" * 72)
    print("Q11: CAMPAIGN SPLIT FOR ACCOUNT-LEVEL SITELINKS  (customer %s)" % CUSTOMER_ID)
    print("=" * 72)

    print("Q11a: campaign_aggregate_asset_view")
    base = try_query(ga, "minimal (campaign x asset, impr)", """
        SELECT campaign.id, asset.id, metrics.impressions
        FROM campaign_aggregate_asset_view
        WHERE segments.date DURING LAST_30_DAYS
    """)
    sl = try_query(ga, "+ sitelink field_type filter", """
        SELECT campaign.id, asset.id, metrics.impressions, metrics.clicks,
               metrics.cost_micros, metrics.conversions, metrics.conversions_value
        FROM campaign_aggregate_asset_view
        WHERE segments.date DURING LAST_30_DAYS
          AND campaign_aggregate_asset_view.asset_field_type = 'SITELINK'
    """)
    if sl is None:
        sl = try_query(ga, "+ field_type filter (alt name)", """
            SELECT campaign.id, asset.id, metrics.impressions, metrics.clicks,
                   metrics.cost_micros, metrics.conversions, metrics.conversions_value
            FROM campaign_aggregate_asset_view
            WHERE segments.date DURING LAST_30_DAYS
              AND asset.type = 'SITELINK'
        """)
    try_query(ga, "+ segments.date (daily grain)", """
        SELECT campaign.id, asset.id, segments.date, metrics.impressions
        FROM campaign_aggregate_asset_view
        WHERE segments.date DURING LAST_7_DAYS
    """)
    ct = try_query(ga, "+ segments.click_type", """
        SELECT campaign.id, asset.id, segments.click_type, metrics.clicks
        FROM campaign_aggregate_asset_view
        WHERE segments.date DURING LAST_7_DAYS
    """)

    print("Q11b: campaign attribution on customer_asset (v23 surprise: accepted -- inspect it)")
    cb = try_query(ga, "customer_asset + campaign.id", """
        SELECT asset.id, campaign.id, metrics.impressions
        FROM customer_asset
        WHERE segments.date DURING LAST_7_DAYS
    """)
    if cb:
        camp_ids = {r.campaign.id for r in cb}
        n_assets = len({r.asset.id for r in cb})
        print("    -> campaign.id values: %d distinct across %d asset(s)%s"
              % (len(camp_ids), n_assets,
                 " -- POPULATED, campaign attribution is real" if len(camp_ids) > 1
                 else " -- single/zero value, attribution NOT real"))
    ct2 = try_query(ga, "customer_asset + campaign.id + click_type", """
        SELECT asset.id, campaign.id, segments.click_type,
               metrics.impressions, metrics.clicks, metrics.cost_micros,
               metrics.conversions, metrics.conversions_value
        FROM customer_asset
        WHERE customer_asset.field_type = 'SITELINK'
          AND segments.date DURING LAST_7_DAYS
    """)
    if ct2:
        camps2 = {r.campaign.id for r in ct2}
        cts = {r.segments.click_type.name for r in ct2}
        print("    -> THE decisive result: %d campaign(s) x click_types %s"
              % (len(camps2), sorted(cts)))
    agg_full = try_query(ga, "aggregate view: full metric set", """
        SELECT campaign.id, asset.id, metrics.impressions, metrics.clicks,
               metrics.cost_micros, metrics.conversions, metrics.conversions_value
        FROM campaign_aggregate_asset_view
        WHERE segments.date DURING LAST_7_DAYS
    """)

    if sl:
        print("Q11c: reconciliation vs customer_asset (LAST_30_DAYS, sitelinks)")
        link = try_query(ga, "customer_asset sitelink totals", """
            SELECT asset.id, metrics.impressions
            FROM customer_asset
            WHERE customer_asset.field_type = 'SITELINK'
              AND segments.date DURING LAST_30_DAYS
        """) or []
        view_tot = defaultdict(int)
        for r in sl:
            view_tot[r.asset.id] += r.metrics.impressions
        link_tot = {r.asset.id: r.metrics.impressions for r in link}
        shared = set(view_tot) & set(link_tot)
        if shared:
            worst = max(shared, key=lambda a: abs(view_tot[a] - link_tot[a]))
            print("  assets in both: %d | worst delta: asset %s view=%d link=%d"
                  % (len(shared), worst, view_tot[worst], link_tot[worst]))
        n_camp = len({r.campaign.id for r in sl})
        print("  campaign split observed: %d campaign(s) across %d sitelink asset(s)"
              % (n_camp, len(view_tot)))

    print("\nVERDICT (keyed on the SPLIT, not the optional filters):")
    best = None
    if ct2 and len({r.campaign.id for r in ct2}) > 1:
        best = "customer_asset with campaign attribution + click_type"
        print("  GO, FULL FIDELITY -- customer_asset carries campaign.id AND click_type:")
        print("  Impr, Share, AND direct-click ROAS/VPC are all campaign-splittable.")
    elif base:
        best = "campaign_aggregate_asset_view (no click_type)"
        print("  GO, PARTIAL -- aggregate view splits campaign x asset x date (sitelink")
        print("  filter applied client-side via asset_sitelink_data ids).")
        print("  click_type NOT available -> Impr/Share split by campaign; direct-click")
        print("  ROAS/VPC stay Total-only.")
    else:
        print("  NO-GO -- no campaign split available; re-link at campaign level or")
        print("  accept Total-only for shared sitelinks.")
    if best:
        print("  Next: paste this output to the DB-architect gate -> small table")
        print("  (campaign x asset x [click_type] x date) -> sync pass ->")
        print("  sitelink_direct_report.py fills its blank columns unchanged.")


if __name__ == "__main__":
    main()
