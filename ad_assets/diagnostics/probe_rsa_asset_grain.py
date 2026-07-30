#!/usr/bin/env python
"""
probe_rsa_asset_grain.py -- ad_assets/diagnostics/ -- READ-ONLY Google Ads API probe.

Phase 2 (RSA layer) grain probe. External sources claim ad_group_ad_asset_view
exposes ONLY metrics.impressions for RSA text assets (clicks/conversions are
UI-modeled and API-blank). House rule: verify against ground truth before the
schema is ruled. Three questions:

  Q7. METRIC AVAILABILITY -- which metric fields are (a) selectable and
      (b) actually populated on ad_group_ad_asset_view for RSAs?
      Tries the full metric set first; on API rejection retries
      impressions-only and reports which fields were refused vs empty.
  Q8. LABEL LANDSCAPE -- performance_label distribution across all enabled
      RSA assets (by field type), pinning usage, and dead weight (assets
      with zero impressions in 30d). This is the pruning signal inventory
      for the new testing regime.
  Q9. COMBINATION CONCENTRATION -- from ad_group_ad_asset_combination_view:
      how many distinct combinations serve per ad, and what share of
      impressions the top 3 combos take (for the highest-volume ad).
      If a couple of combos dominate, a 15-headline RSA is effectively
      2-3 fixed ads chosen by Google -- the measurability argument for
      lean themed RSAs, quantified.

Schema implications for the gate (pending these verdicts):
  * rsa asset metrics table likely stores impressions ONLY + label
  * performance_label is a SNAPSHOT that changes over time -- propose a
    weekly label-history table so LEARNING->GOOD/LOW trajectories are
    trackable (labels are the only performance signal Google gives here)

    python probe_rsa_asset_grain.py            # LAST_30_DAYS
"""
import os
import sys
from collections import Counter, defaultdict

from dotenv import load_dotenv

load_dotenv()

try:
    from google.ads.googleads.client import GoogleAdsClient
    from google.ads.googleads.errors import GoogleAdsException
except ImportError:
    sys.exit("[ABORT] google-ads library not found -- run inside the paid-search conda env.")

CUSTOMER_ID = os.environ.get("PROBE_CUSTOMER_ID", "8886502147").replace("-", "")
DATE_RANGE = "LAST_30_DAYS"

FULL_METRICS = ("metrics.impressions, metrics.clicks, metrics.cost_micros, "
                "metrics.conversions, metrics.conversions_value")


def gaql(ga, query):
    rows = []
    for batch in ga.search_stream(customer_id=CUSTOMER_ID, query=query):
        rows.extend(batch.results)
    return rows


def main():
    client = GoogleAdsClient.load_from_storage()
    ga = client.get_service("GoogleAdsService")

    print("=" * 72)
    print("RSA ASSET GRAIN PROBE  (customer %s, %s)" % (CUSTOMER_ID, DATE_RANGE))
    print("=" * 72)

    # ---- Q7: metric availability ----
    base_select = """
        SELECT ad_group_ad.ad.id, asset.id, asset.text_asset.text,
               ad_group_ad_asset_view.field_type,
               ad_group_ad_asset_view.performance_label,
               ad_group_ad_asset_view.pinned_field,
               {metrics}
        FROM ad_group_ad_asset_view
        WHERE ad_group_ad.ad.type = 'RESPONSIVE_SEARCH_AD'
          AND ad_group_ad.status = 'ENABLED'
          AND ad_group.status = 'ENABLED'
          AND campaign.status = 'ENABLED'
          AND segments.date DURING {dr}
    """
    rows, refused = None, []
    try:
        rows = gaql(ga, base_select.format(metrics=FULL_METRICS, dr=DATE_RANGE))
        print("\nQ7: full metric set SELECTABLE (%d rows)" % len(rows))
    except GoogleAdsException as e:
        refused = [err.message for err in e.failure.errors]
        print("\nQ7: full metric set REJECTED:")
        for m in refused:
            print("    [API] %s" % m)
        rows = gaql(ga, base_select.format(metrics="metrics.impressions", dr=DATE_RANGE))
        print("    retry impressions-only: %d rows" % len(rows))

    if not rows:
        sys.exit("[ABORT] zero rows -- widen date range or check ad statuses.")

    has_full = not refused
    if has_full:
        pop = {
            "impressions": sum(1 for r in rows if r.metrics.impressions > 0),
            "clicks": sum(1 for r in rows if r.metrics.clicks > 0),
            "conversions": sum(1 for r in rows if r.metrics.conversions > 0),
            "cost": sum(1 for r in rows if r.metrics.cost_micros > 0),
        }
        print("Q7 population check (rows with value > 0, of %d):" % len(rows))
        for k, v in pop.items():
            print("    %-12s %6d %s" % (k, v,
                  "" if v else "<-- selectable but NEVER populated (UI-only metric)"))
    else:
        print("Q7 VERDICT: API refuses click/conversion metrics at RSA asset grain "
              "-- impressions + performance_label are the whole universe.")

    # ---- Q10: device segment gate check (Phase 2 Decision 1) ----
    print("\nQ10: segments.device on ad_group_ad_asset_view (gate check):")
    try:
        dev_rows = gaql(ga, f"""
            SELECT ad_group_ad.ad.id, asset.id, segments.device, metrics.impressions
            FROM ad_group_ad_asset_view
            WHERE ad_group_ad.ad.type = 'RESPONSIVE_SEARCH_AD'
              AND ad_group_ad.status = 'ENABLED'
              AND ad_group.status = 'ENABLED'
              AND campaign.status = 'ENABLED'
              AND segments.date DURING {DATE_RANGE}
        """)
        devices = sorted({r.segments.device.name for r in dev_rows})
        print("    ACCEPTED -- %d rows, devices seen: %s" % (len(dev_rows), devices))
        print("    VERDICT: device-segmented sync pass is GO; PK commits as drafted.")
    except GoogleAdsException as e:
        for err in e.failure.errors:
            print("    [API] %s" % err.message)
        print("    VERDICT: REJECTED -- device column stays 'ALL'-only, PK unchanged, "
              "sync runs unsegmented pass only.")

    # ---- Q8: label landscape ----
    print("\nQ8: performance_label landscape (enabled RSA assets, %s):" % DATE_RANGE)
    label_ft = Counter()
    pinned = Counter()
    impr_by_asset = defaultdict(int)
    text_by_asset = {}
    for r in rows:
        ft = r.ad_group_ad_asset_view.field_type.name
        label_ft[(ft, r.ad_group_ad_asset_view.performance_label.name)] += 1
        pf = r.ad_group_ad_asset_view.pinned_field.name
        if pf and pf != "UNSPECIFIED":
            pinned[pf] += 1
        impr_by_asset[(r.ad_group_ad.ad.id, r.asset.id)] += r.metrics.impressions
        text_by_asset[r.asset.id] = r.asset.text_asset.text

    fts = sorted({ft for ft, _ in label_ft})
    labels = sorted({lb for _, lb in label_ft})
    print("    %-14s" % "" + "".join("%10s" % lb for lb in labels))
    for ft in fts:
        print("    %-14s" % ft + "".join("%10d" % label_ft.get((ft, lb), 0) for lb in labels))
    dead = sum(1 for v in impr_by_asset.values() if v == 0)
    print("    pinned assets: %s" % (dict(pinned) or "none"))
    print("    dead weight: %d of %d (ad, asset) slots with ZERO impressions in 30d"
          % (dead, len(impr_by_asset)))

    # ---- Q9: combination concentration ----
    print("\nQ9: combination concentration:")
    try:
        combo_rows = gaql(ga, f"""
            SELECT ad_group_ad.ad.id,
                   ad_group_ad_asset_combination_view.served_assets,
                   metrics.impressions
            FROM ad_group_ad_asset_combination_view
            WHERE ad_group_ad.status = 'ENABLED'
              AND ad_group.status = 'ENABLED'
              AND campaign.status = 'ENABLED'
              AND segments.date DURING {DATE_RANGE}
        """)
    except GoogleAdsException as e:
        combo_rows = None
        for err in e.failure.errors:
            print("    [API] combination view rejected: %s" % err.message)

    if combo_rows:
        per_ad = defaultdict(list)   # ad_id -> [(combo_key, impressions)]
        for r in combo_rows:
            key = tuple(sorted(a.asset.split("/")[-1]
                               for a in r.ad_group_ad_asset_combination_view.served_assets))
            per_ad[r.ad_group_ad.ad.id].append((key, r.metrics.impressions))
        n_combos = [len({k for k, _ in v}) for v in per_ad.values()]
        print("    ads with combination data: %d | distinct combos per ad: "
              "min %d / median %d / max %d"
              % (len(per_ad), min(n_combos),
                 sorted(n_combos)[len(n_combos) // 2], max(n_combos)))
        # top-volume ad deep dive
        top_ad = max(per_ad, key=lambda a: sum(i for _, i in per_ad[a]))
        combos = defaultdict(int)
        for k, i in per_ad[top_ad]:
            combos[k] += i
        total = sum(combos.values()) or 1
        top3 = sorted(combos.values(), reverse=True)[:3]
        print("    highest-volume ad %s: %d distinct combos, top-3 combos = %.1f%% "
              "of its impressions" % (top_ad, len(combos), 100.0 * sum(top3) / total))
        best = max(combos, key=combos.get)
        print("    its #1 combo (%.1f%%):" % (100.0 * combos[best] / total))
        for aid in best:
            print("      - %s" % text_by_asset.get(int(aid), "asset %s" % aid)[:60])

    print("\nDone. Paste full output to the DB architect gate -- the Phase 2 schema "
          "(asset_text_data / rsa_ad_assets / label history) follows from Q7, and "
          "Q9 quantifies the lean-RSA measurability argument for the testing regime.")


if __name__ == "__main__":
    main()
