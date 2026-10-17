#!/usr/bin/env python
"""
pull_geico_asset_history.py -- ad_assets/diagnostics/ -- the cohort-scoped
SERVING-GRAIN pull (evidence grain 4), answering both open questions in one
run over the widened window 2024-04-01 .. 2025-12-31:

  Q-DKI:     which DKI assets served on Geico-cohort ads, through when
             (immutable-text proof of "DKI SERVED", per date)
  Q-REWRITE: the Jul-2025 rewrite as-served -- last serving date of the old
             flagship ('Save Time & Money In Minutes'), first serving date
             of the new one ('$98 {LOCATION(State)} Home Insurance'), per
             ad group, dated to the day

WHY THIS GRAIN: text assets are IMMUTABLE (editing an RSA swaps asset links,
never rewrites an asset), so [asset served on date D] + [asset text] is
proof of served copy on D -- immune to the post-edit contamination that
caps fingerprint evidence. NOTE: proves SERVED, not merely-present; never-
served assets leave no rows here.

DB is read-only (cohort ids via ad_groups -> rsa_ads, ids-only joins).
Output (ad_assets\\exports\\):
  geico_asset_serving_daily_<date>.csv   ad_id, ad_group_id, asset_id,
                                         report_date, impressions, clicks
  geico_asset_texts_<date>.csv           asset_id, text, is_dki,
                                         is_flagship_old, is_flagship_new
Console: verdict blocks for both questions.

  python pull_geico_asset_history.py --from 2024-04-01 --to 2025-12-31
"""
import argparse
import csv
import datetime
import os
import sqlite3
import sys
from collections import defaultdict

from dotenv import load_dotenv

load_dotenv()

try:
    from google.ads.googleads.client import GoogleAdsClient
except ImportError:
    sys.exit("[ABORT] google-ads library not found -- run inside the paid-search conda env.")

CUSTOMER_ID = os.environ.get("PROBE_CUSTOMER_ID", "8886502147").replace("-", "")
ACCOUNT_ID = 1
DB = r"C:\Projects\google-ads-ops\db\paid_search.db"
EXPORT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "exports")
FLAGSHIP_OLD = "save time & money in minutes"
FLAGSHIP_NEW_TOKENS = ("$98", "{location")


def chunks(seq, n=900):
    seq = list(seq)
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def main():
    p = argparse.ArgumentParser(description="Geico-cohort asset serving history (read-only).")
    p.add_argument("--from", dest="date_from", default="2024-04-01")
    p.add_argument("--to", dest="date_to", default="2025-12-31")
    args = p.parse_args()
    os.makedirs(EXPORT_DIR, exist_ok=True)

    conn = sqlite3.connect("file:%s?mode=ro" % DB.replace("\\", "/"), uri=True)
    geico_ags = [r[0] for r in conn.execute(
        "SELECT ad_group_id FROM ad_groups WHERE instr(lower(ad_group_name),'geico')")]
    ad_to_group = {}
    for ch in chunks(geico_ags):
        marks = ",".join("?" * len(ch))
        for ad_id, ag in conn.execute(
                f"SELECT ad_id, ad_group_id FROM rsa_ads WHERE account_id=? "
                f"AND ad_group_id IN ({marks})", [ACCOUNT_ID] + ch):
            ad_to_group[ad_id] = ag
    conn.close()
    print("[COHORT] %d Geico ad group ids -> %d RSA ads" % (len(geico_ags), len(ad_to_group)))

    client = GoogleAdsClient.load_from_storage()
    ga = client.get_service("GoogleAdsService")
    stamp = datetime.date.today().isoformat()
    serving_path = os.path.join(EXPORT_DIR, "geico_asset_serving_daily_%s.csv" % stamp)
    n_rows = 0
    asset_ids = set()
    # per-asset serving envelopes for the verdicts
    first_last = {}  # asset_id -> [first_date, last_date, impressions]
    with open(serving_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["ad_id", "ad_group_id", "asset_id", "report_date",
                    "impressions", "clicks"])
        for ch in chunks(sorted(ad_to_group)):
            for batch in ga.search_stream(customer_id=CUSTOMER_ID, query=f"""
                SELECT ad_group_ad.ad.id, asset.id, segments.date,
                       metrics.impressions, metrics.clicks
                FROM ad_group_ad_asset_view
                WHERE segments.date BETWEEN '{args.date_from}' AND '{args.date_to}'
                  AND ad_group_ad.ad.id IN ({",".join(str(i) for i in ch)})
                  AND metrics.impressions > 0
            """):
                for r in batch.results:
                    ad = r.ad_group_ad.ad.id
                    aid = r.asset.id
                    d = str(r.segments.date)
                    w.writerow([ad, ad_to_group[ad], aid, d,
                                r.metrics.impressions, r.metrics.clicks])
                    n_rows += 1
                    asset_ids.add(aid)
                    fl = first_last.setdefault(aid, [d, d, 0])
                    fl[0], fl[1] = min(fl[0], d), max(fl[1], d)
                    fl[2] += r.metrics.impressions
    print("[PULL] %s serving rows, %d distinct assets -> %s"
          % (f"{n_rows:,}", len(asset_ids), serving_path))

    texts = {}
    for ch in chunks(sorted(asset_ids), 500):
        for batch in ga.search_stream(customer_id=CUSTOMER_ID, query=f"""
            SELECT asset.id, asset.text_asset.text FROM asset
            WHERE asset.id IN ({",".join(str(i) for i in ch)})
        """):
            for r in batch.results:
                texts[r.asset.id] = r.asset.text_asset.text or ""
    text_path = os.path.join(EXPORT_DIR, "geico_asset_texts_%s.csv" % stamp)
    with open(text_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["asset_id", "text", "is_dki", "is_flagship_old", "is_flagship_new"])
        for aid in sorted(asset_ids):
            t = texts.get(aid, "")
            tl = t.lower()
            w.writerow([aid, t, "YES" if "{keyword" in tl else "no",
                        "YES" if FLAGSHIP_OLD in tl else "no",
                        "YES" if all(tok in tl for tok in FLAGSHIP_NEW_TOKENS) else "no"])
    print("[TEXTS] %d asset texts -> %s" % (len(texts), text_path))

    def classify(aid):
        tl = texts.get(aid, "").lower()
        if "{keyword" in tl:
            return "DKI"
        if FLAGSHIP_OLD in tl:
            return "OLD_FLAGSHIP"
        if all(tok in tl for tok in FLAGSHIP_NEW_TOKENS):
            return "NEW_FLAGSHIP"
        return None

    groups = defaultdict(list)
    for aid, (d1, d2, impr) in first_last.items():
        c = classify(aid)
        if c:
            groups[c].append((aid, d1, d2, impr))
    print("\n[Q-DKI] DKI assets that SERVED on the Geico cohort:")
    dki = sorted(groups.get("DKI", []), key=lambda x: x[2])
    if dki:
        print("  %d asset(s); serving span %s .. %s (latest DKI serving date = "
              "the proven end of served DKI)" % (len(dki), min(d for _, d, _, _ in dki),
                                                 max(d2 for _, _, d2, _ in dki)))
        for aid, d1, d2, impr in dki[-5:]:
            print("    asset %-14d %s..%s  %s impr  %r"
                  % (aid, d1, d2, f"{impr:,}", texts.get(aid, "")[:44]))
    else:
        print("  none served in window")
    print("\n[Q-REWRITE] Jul-2025 rewrite as-served:")
    for label, key in (("old flagship last served", "OLD_FLAGSHIP"),
                       ("new flagship first served", "NEW_FLAGSHIP")):
        rows = groups.get(key, [])
        if not rows:
            print("  %-26s -- no serving rows in window" % label)
        elif key == "OLD_FLAGSHIP":
            print("  %-26s %s  (%d asset(s))"
                  % (label, max(d2 for _, _, d2, _ in rows), len(rows)))
        else:
            print("  %-26s %s  (%d asset(s))"
                  % (label, min(d1 for _, d1, _, _ in rows), len(rows)))
    print("\nLedger corroboration test: old-flagship last-served ~Jul-22/27-2025 and")
    print("new-flagship first-served ~Jul-23/28-2025 = the rewrite proven AS-SERVED,")
    print("dated. Send both CSVs + this console block to the change-history thread.")


if __name__ == "__main__":
    main()
