#!/usr/bin/env python
"""
rsa_sitelink_report.py (v2) -- ad_assets/analysis/ -- READ-ONLY analysis report.

v2 corrections (2026-07-17, after v1 review with Gilbert):
  * TEST DEFINITION IS EXPLICIT, not auto-detected. The intent test = the
    CA and Tier2 campaigns (launched week of Jun 1-8). A variant is a TEST
    VARIANT if it runs ONLY inside test campaigns; everything else is
    incumbent. Override with --test-campaigns if the test changes.
  * The creative comparison is scoped to AD GROUPS THAT CONTAIN A TEST
    VARIANT (2-3 ad groups per test campaign kept incumbent-only creative
    due to keyword groupings -- they are excluded automatically). Within
    those ad groups, test ads and incumbent ads share the same auctions,
    so CVR test-vs-incumbent is a fair creative read.
  * SITELINK SHARE IS REBUILT on the campaign report's click_type
    partition (FROM campaign, segments.click_type) -- real clicks and
    conversions partition cleanly there and are additive account-wide.
    v1 summed conversions ACROSS assets from asset_performance_daily,
    which double-counts (multiple sitelinks serve per impression) and
    produced impossible >100% shares. Asset-grain metrics must NEVER be
    summed across assets -- see probe_asset_cross_additivity.py and the
    pending gate ruling.

Outputs (ad_assets\\exports\\): report HTML + variants / weekly / test /
share CSVs, date-stamped.

Usage:
  python rsa_sitelink_report.py                       # YTD default
  python rsa_sitelink_report.py --from 2026-03-01
  python rsa_sitelink_report.py --test-campaigns 111,222
"""
import argparse
import base64
import csv
import datetime
import hashlib
import io
import os
import sys
from collections import defaultdict

from dotenv import load_dotenv

load_dotenv()

try:
    from google.ads.googleads.client import GoogleAdsClient
except ImportError:
    sys.exit("[ABORT] google-ads library not found -- run inside the paid-search conda env.")

try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    HAVE_MPL = True
except ImportError:
    HAVE_MPL = False

CUSTOMER_ID = os.environ.get("PROBE_CUSTOMER_ID", "8886502147").replace("-", "")
EXPORT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "exports")

DEFAULT_TEST_CAMPAIGNS = {23385188622: "CA", 23530860942: "Tier2"}
TOP_VARIANTS_CHARTED = 8  # rest grouped as 'other' to keep the chart readable


def gaql(ga, query):
    rows = []
    for batch in ga.search_stream(customer_id=CUSTOMER_ID, query=query):
        rows.extend(batch.results)
    return rows


def week_of(date_str):
    d = datetime.date.fromisoformat(str(date_str))
    return (d - datetime.timedelta(days=d.weekday())).isoformat()


def vnum(label):  # numeric sort for V1..V32
    return int(label[1:])


# ---------------------------------------------------------------------------
def fetch_rsa_inventory(ga):
    q = """
        SELECT campaign.id, campaign.name, ad_group.id,
               ad_group_ad.ad.id,
               ad_group_ad.ad.responsive_search_ad.headlines
        FROM ad_group_ad
        WHERE ad_group_ad.ad.type = 'RESPONSIVE_SEARCH_AD'
          AND ad_group_ad.status = 'ENABLED'
          AND ad_group.status = 'ENABLED'
          AND campaign.status = 'ENABLED'
    """
    ads = {}
    for r in gaql(ga, q):
        headlines = tuple(sorted(h.text for h in
                                 r.ad_group_ad.ad.responsive_search_ad.headlines))
        fp = hashlib.md5("||".join(headlines).encode("utf-8")).hexdigest()[:10]
        ads[r.ad_group_ad.ad.id] = {
            "campaign_id": r.campaign.id, "campaign_name": r.campaign.name,
            "ad_group_id": r.ad_group.id, "fingerprint": fp, "headlines": headlines,
        }
    # label variants, ordered by prevalence
    counts = defaultdict(int)
    for a in ads.values():
        counts[a["fingerprint"]] += 1
    labels = {fp: "V%d" % (i + 1)
              for i, fp in enumerate(sorted(counts, key=lambda f: -counts[f]))}
    for a in ads.values():
        a["variant"] = labels[a["fingerprint"]]
    return ads


def classify_test(ads, test_campaigns):
    """A variant is TEST if every ad carrying it lives in a test campaign."""
    variant_camps = defaultdict(set)
    for a in ads.values():
        variant_camps[a["variant"]].add(a["campaign_id"])
    test_variants = {v for v, camps in variant_camps.items()
                     if camps and camps <= set(test_campaigns)}
    test_ad_groups = {a["ad_group_id"] for a in ads.values()
                      if a["variant"] in test_variants}
    for a in ads.values():
        a["is_test_variant"] = a["variant"] in test_variants
    return test_variants, test_ad_groups


# ---------------------------------------------------------------------------
def chart_png(build_fn, wide=False):
    if not HAVE_MPL:
        return None
    fig, ax = plt.subplots(figsize=(10 if wide else 9, 4))
    build_fn(ax)
    ax.grid(alpha=0.25)
    fig.autofmt_xdate(rotation=45)
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=110)
    plt.close(fig)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def img_tag(b64):
    if b64 is None:
        return "<p><i>(matplotlib not installed -- see CSV exports)</i></p>"
    return '<img src="data:image/png;base64,%s" style="max-width:100%%"/>' % b64


# ---------------------------------------------------------------------------
def main():
    p = argparse.ArgumentParser(description="RSA variant + sitelink share report v2 (read-only).")
    p.add_argument("--from", dest="date_from", default="2026-01-01", metavar="YYYY-MM-DD")
    p.add_argument("--test-campaigns", default="",
                   help="comma-separated campaign ids (default: CA + Tier2)")
    args = p.parse_args()
    os.makedirs(EXPORT_DIR, exist_ok=True)

    if args.test_campaigns.strip():
        test_campaigns = {int(c): str(c) for c in args.test_campaigns.split(",") if c.strip()}
    else:
        test_campaigns = dict(DEFAULT_TEST_CAMPAIGNS)

    date_to = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
    provisional_wk = week_of((datetime.date.today() - datetime.timedelta(days=8)).isoformat())
    stamp = datetime.date.today().isoformat()

    client = GoogleAdsClient.load_from_storage()
    ga = client.get_service("GoogleAdsService")

    print("[1/4] RSA inventory + test classification...")
    ads = fetch_rsa_inventory(ga)
    test_variants, test_ad_groups = classify_test(ads, test_campaigns)
    n_variants = len({a["variant"] for a in ads.values()})
    print("      %d RSAs -> %d variants; %d test variants in %d test ad groups "
          "(campaigns: %s)"
          % (len(ads), n_variants, len(test_variants), len(test_ad_groups),
             ", ".join(test_campaigns.values())))

    print("[2/4] RSA weekly metrics...")
    q = f"""
        SELECT campaign.id, ad_group.id, ad_group_ad.ad.id, segments.week,
               metrics.impressions, metrics.clicks, metrics.conversions,
               metrics.conversions_value
        FROM ad_group_ad
        WHERE ad_group_ad.ad.type = 'RESPONSIVE_SEARCH_AD'
          AND segments.date BETWEEN '{args.date_from}' AND '{date_to}'
    """
    var_weekly = defaultdict(lambda: defaultdict(float))
    # (campaign_label, test|incumbent) -> week -> [conv, clicks, value]
    cmp_weekly = defaultdict(lambda: defaultdict(lambda: [0.0, 0, 0.0]))
    for r in gaql(ga, q):
        info = ads.get(r.ad_group_ad.ad.id)
        if info is None:
            continue  # ads paused/removed since inventory pull
        wk = week_of(r.segments.week)
        var_weekly[info["variant"]][wk] += r.metrics.conversions
        # creative comparison: only inside test campaigns, only in ad groups
        # that actually contain a test variant (the 2-3 unchanged ad groups
        # per campaign fall out here automatically)
        if r.campaign.id in test_campaigns and r.ad_group.id in test_ad_groups:
            bucket = (test_campaigns[r.campaign.id],
                      "test" if info["is_test_variant"] else "incumbent")
            cmp_weekly[bucket][wk][0] += r.metrics.conversions
            cmp_weekly[bucket][wk][1] += r.metrics.clicks
            cmp_weekly[bucket][wk][2] += r.metrics.conversions_value

    print("[3/4] sitelink share (campaign-report click_type partition)...")
    q = f"""
        SELECT segments.week, segments.click_type,
               metrics.clicks, metrics.conversions
        FROM campaign
        WHERE segments.date BETWEEN '{args.date_from}' AND '{date_to}'
    """
    week_total = defaultdict(float)
    week_sitelink = defaultdict(float)
    for r in gaql(ga, q):
        wk = week_of(r.segments.week)
        week_total[wk] += r.metrics.conversions
        if r.segments.click_type.name == "SITELINKS":
            week_sitelink[wk] += r.metrics.conversions
    weeks = sorted(week_total)
    share = {wk: (100.0 * week_sitelink[wk] / week_total[wk]) if week_total[wk] else 0.0
             for wk in weeks}
    full = [wk for wk in weeks if wk < provisional_wk]
    half = len(full) // 2
    share_early = sum(share[w] for w in full[:half]) / max(1, half)
    share_late = sum(share[w] for w in full[half:]) / max(1, len(full) - half)

    print("[4/4] writing report...")
    all_variants = sorted(var_weekly, key=vnum)
    top = sorted(var_weekly, key=lambda v: -sum(var_weekly[v].values()))[:TOP_VARIANTS_CHARTED]

    def c_variants(ax):
        for v in sorted(top, key=vnum):
            style = "--" if v in test_variants else "-"
            ax.plot(weeks, [var_weekly[v].get(w, 0) for w in weeks],
                    style, marker=".", label=v + (" (test)" if v in test_variants else ""))
        other = [sum(var_weekly[v].get(w, 0) for v in all_variants if v not in top)
                 for w in weeks]
        ax.plot(weeks, other, ":", color="gray", label="other (%d)" % (len(all_variants) - len(top)))
        ax.axvline(provisional_wk, ls="--", lw=0.8, color="gray")
        ax.set_title("Weekly conversions by RSA variant (top %d; dashed = test creative)"
                     % TOP_VARIANTS_CHARTED)
        ax.legend(fontsize=7, ncol=2)

    def c_test(ax):
        colors = {"CA": "tab:orange", "Tier2": "tab:red"}
        for (camp, kind), wkdata in sorted(cmp_weekly.items()):
            cvr = [100.0 * wkdata[w][0] / wkdata[w][1] if wkdata.get(w, [0, 0, 0])[1] else None
                   for w in weeks]
            ax.plot(weeks, cvr, "-" if kind == "incumbent" else "--", marker=".",
                    color=colors.get(camp, "tab:blue"),
                    alpha=1.0 if kind == "test" else 0.5,
                    label="%s %s" % (camp, kind))
        ax.axvline(provisional_wk, ls="--", lw=0.8, color="gray")
        ax.set_title("Intent test: CVR%, test vs incumbent creative, "
                     "SAME ad groups only (dashed = test)")
        ax.set_ylabel("CVR %")
        ax.legend(fontsize=8)

    def c_share(ax):
        ax.plot(weeks, [share[w] for w in weeks], marker=".", color="tab:green")
        ax.axvline(provisional_wk, ls="--", lw=0.8, color="gray")
        ax.set_title("Sitelink-click conversions as %% of total (campaign-report "
                     "click_type; early %.1f%% vs recent %.1f%%)" % (share_early, share_late))
        ax.set_ylabel("%")

    variant_defs, seen = [], set()
    for a in sorted(ads.values(), key=lambda a: vnum(a["variant"])):
        if a["variant"] in seen:
            continue
        seen.add(a["variant"])
        variant_defs.append(
            "<tr%s><td><b>%s</b>%s</td><td style='font-size:11px'>%s ...</td></tr>"
            % (' style="background:#fff3f3"' if a["is_test_variant"] else "",
               a["variant"], " (test)" if a["is_test_variant"] else "",
               " | ".join(a["headlines"][:4])))

    html = f"""<!doctype html><html><head><meta charset="utf-8">
<title>RSA + Sitelink report v2 {stamp}</title>
<style>body{{font-family:Segoe UI,Arial,sans-serif;max-width:960px;margin:24px auto;color:#222}}
table{{border-collapse:collapse;width:100%}}td,th{{border:1px solid #ddd;padding:4px 8px;text-align:left}}
h2{{border-bottom:2px solid #eee;padding-bottom:4px}}</style></head><body>
<h1>RSA variants &amp; sitelink share (v2) -- {args.date_from} to {date_to}</h1>
<p><b>Test definition (confirmed):</b> intent test = {', '.join(test_campaigns.values())}
campaigns; test variants = creative running only in those campaigns
({len(test_variants)} variants, red in the table). Comparison restricted to the
{len(test_ad_groups)} ad groups that contain test creative. Trailing week provisional.</p>
<h2>1. Conversions by variant</h2>{img_tag(chart_png(c_variants, wide=True))}
<h3>Variant definitions</h3><table><tr><th>Variant</th><th>Sample headlines</th></tr>
{''.join(variant_defs)}</table>
<h2>2. Intent test -- test vs incumbent creative, same ad groups</h2>
{img_tag(chart_png(c_test))}
<h2>3. Sitelink share of conversions (corrected)</h2>{img_tag(chart_png(c_share))}
<p>Computed from the campaign report's click_type partition (additive account-wide).
v1's asset-table method double-counted across co-serving assets and is retired.</p>
</body></html>"""

    html_path = os.path.join(EXPORT_DIR, "rsa_sitelink_report_%s.html" % stamp)
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html)

    with open(os.path.join(EXPORT_DIR, "rsa_variants_%s.csv" % stamp), "w",
              newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["variant", "is_test_variant", "fingerprint", "ad_id", "campaign_id",
                    "campaign_name", "ad_group_id", "headlines"])
        for ad_id, a in sorted(ads.items(), key=lambda kv: vnum(kv[1]["variant"])):
            w.writerow([a["variant"], a["is_test_variant"], a["fingerprint"], ad_id,
                        a["campaign_id"], a["campaign_name"], a["ad_group_id"],
                        " | ".join(a["headlines"])])
    with open(os.path.join(EXPORT_DIR, "rsa_weekly_%s.csv" % stamp), "w",
              newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["week"] + all_variants)
        for wk in weeks:
            w.writerow([wk] + [round(var_weekly[v].get(wk, 0), 2) for v in all_variants])
    with open(os.path.join(EXPORT_DIR, "test_comparison_%s.csv" % stamp), "w",
              newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["week", "campaign", "bucket", "conversions", "clicks", "conv_value", "cvr_pct"])
        for (camp, kind), wkdata in sorted(cmp_weekly.items()):
            for wk in weeks:
                conv, clicks, val = wkdata.get(wk, [0.0, 0, 0.0])
                w.writerow([wk, camp, kind, round(conv, 2), clicks, round(val, 2),
                            round(100.0 * conv / clicks, 2) if clicks else ""])
    with open(os.path.join(EXPORT_DIR, "sitelink_share_weekly_%s.csv" % stamp), "w",
              newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["week", "sitelink_conversions", "total_conversions", "share_pct"])
        for wk in weeks:
            w.writerow([wk, round(week_sitelink[wk], 1), round(week_total[wk], 1),
                        round(share[wk], 2)])

    print("\n[DONE] %s" % html_path)
    print("       sitelink share (corrected): early %.1f%% -> recent %.1f%%"
          % (share_early, share_late))


if __name__ == "__main__":
    main()
