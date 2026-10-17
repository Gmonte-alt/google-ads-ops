"""
geico_dki_coverage.py -- Verifies the DKI question against the actual RSA asset data:
what share of Geico ad-group IMPRESSIONS each month went to ads carrying (a) a {KeyWord:...}
insertion headline, and (b) a literal 'geico' headline. Serving-weighted (multiple RSAs per
ad group -- coverage is measured by which ads actually served), and time-aware (an asset
counts on a date only if attached then, per rsa_ad_assets first_seen/removed dates).

Also reports:
  * the DKI default strings in use (the fallback text when the keyword doesn't fit), and
  * the keyword-length fallback exposure: share of the geico keyword cohort's impressions on
    keywords >30 chars (where headline DKI cannot render the keyword and falls back).

Sources: rsa_ads (ad->ad_group), rsa_ad_assets (ad->asset, field_type, lifecycle),
asset_text_data (text), rsa_ad_performance_daily (serving weights),
keyword_instances + keyword_performance_daily (fallback exposure).

    python geico_dki_coverage.py --db PATH --brand Geico
"""
import argparse
import collections
import re
import sqlite3

ACCOUNT_ID = 1
_DKI = re.compile(r"\{keyword:", re.I)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--brand", default="Geico")
    ap.add_argument("--account", type=int, default=ACCOUNT_ID)
    ap.add_argument("--csv", default=None)
    args = ap.parse_args()
    b = args.brand.lower()
    conn = sqlite3.connect(args.db)

    # 1) Geico ad-id set (id-set-first)
    ads = {}
    for ad_id, ag in conn.execute(
            "SELECT a.ad_id, g.ad_group_name FROM rsa_ads a "
            "JOIN ad_groups g ON g.ad_group_id = a.ad_group_id "
            "WHERE g.account_id=? AND lower(g.ad_group_name) LIKE ?",
            (args.account, "%" + b + "%")):
        ads[ad_id] = ag
    print("geico RSA ads: %d" % len(ads))
    if not ads:
        return
    conn.execute("CREATE TEMP TABLE _ads (ad_id INTEGER PRIMARY KEY)")
    conn.executemany("INSERT OR IGNORE INTO _ads VALUES (?)", [(i,) for i in ads])

    # 2) headline assets per ad, with text + lifecycle
    #    field_type naming can vary (HEADLINE / HEADLINE_1 ...); match loosely.
    ad_assets = collections.defaultdict(list)  # ad_id -> [(first,removed,is_dki,is_brand,text)]
    dki_defaults = collections.Counter()
    n_headline_assets = 0
    for ad_id, ft, first, removed, text in conn.execute(
            "SELECT ra.ad_id, ra.field_type, ra.first_seen_date, ra.removed_date, t.text "
            "FROM rsa_ad_assets ra JOIN _ads x ON x.ad_id = ra.ad_id "
            "JOIN asset_text_data t ON t.asset_id = ra.asset_id AND t.account_id = ra.account_id "
            "WHERE ra.account_id=? AND upper(ra.field_type) LIKE '%HEADLINE%'",
            (args.account,)):
        n_headline_assets += 1
        txt = text or ""
        is_dki = bool(_DKI.search(txt))
        if is_dki:
            m = re.search(r"\{keyword:\s*(.*?)\}", txt, re.I)
            if m:
                dki_defaults[m.group(1).strip()] += 1
        ad_assets[ad_id].append((first or "0000-00-00", removed or "9999-12-31",
                                 is_dki, b in txt.lower(), txt))
    print("headline asset links on geico ads: %d | ads with any headline data: %d"
          % (n_headline_assets, len(ad_assets)))
    firsts = [a[0] for lst in ad_assets.values() for a in lst if a[0] != "0000-00-00"]
    if firsts:
        print("asset-link first_seen range: %s .. %s  (if min is near the backfill date, "
              "lifecycle is CURRENT-COMPOSITION projected back -- read history cautiously)"
              % (min(firsts), max(firsts)))

    # 3) serving-weighted monthly coverage
    monthly = collections.defaultdict(lambda: {"impr": 0, "dki_impr": 0, "brand_impr": 0})
    q = ("SELECT p.ad_id, p.report_date, SUM(p.impressions) "
         "FROM rsa_ad_performance_daily p JOIN _ads x ON x.ad_id = p.ad_id "
         "WHERE p.account_id=? AND p.impressions>0 GROUP BY p.ad_id, p.report_date")
    for ad_id, d, impr in conn.execute(q, (args.account,)):
        m = (d or "")[:7]
        rec = monthly[m]
        rec["impr"] += impr
        has_dki = has_brand = False
        for first, removed, is_dki, is_brand, _ in ad_assets.get(ad_id, ()):
            if first <= d <= removed:
                has_dki = has_dki or is_dki
                has_brand = has_brand or is_brand
        if has_dki:
            rec["dki_impr"] += impr
        if has_brand:
            rec["brand_impr"] += impr

    print("\n%-8s %12s %10s %10s %10s %10s" % ("MONTH", "impr", "dki_impr", "dki_share",
                                               "brand_impr", "brand_shr"))
    rows = []
    for m in sorted(monthly):
        r = monthly[m]
        ds = round(r["dki_impr"] / r["impr"], 4) if r["impr"] else None
        bs = round(r["brand_impr"] / r["impr"], 4) if r["impr"] else None
        rows.append({"month": m, "impr": r["impr"], "dki_impr": r["dki_impr"], "dki_share": ds,
                     "brand_impr": r["brand_impr"], "brand_share": bs})
        print("%-8s %12d %10d %10s %10d %10s" % (m, r["impr"], r["dki_impr"], ds,
                                                 r["brand_impr"], bs))

    print("\n=== DKI default strings on geico ads (fallback text when keyword doesn't fit) ===")
    for txt, n in dki_defaults.most_common(10):
        print("  %4d  {KeyWord:%s}" % (n, txt))

    # 4) fallback exposure: geico keyword impressions on keywords too long to render (>30 chars)
    r = conn.execute(
        "SELECT SUM(p.impressions), "
        "       SUM(CASE WHEN length(ki.keyword_text) > 30 THEN p.impressions ELSE 0 END) "
        "FROM keyword_performance_daily p JOIN keyword_instances ki ON ki.id = p.instance_id "
        "WHERE ki.account_id=? AND p.device='ALL' AND lower(ki.keyword_text) LIKE ?",
        (args.account, "%" + b + "%")).fetchone()
    tot, long_ = r[0] or 0, r[1] or 0
    print("\n=== DKI fallback exposure (geico keyword cohort, all time) ===")
    print("  keyword impressions: %d | on keywords >30 chars (DKI cannot render): %d (%.1f%%)"
          % (tot, long_, 100.0 * long_ / tot if tot else 0))

    if args.csv and rows:
        import csv as _csv
        with open(args.csv, "w", newline="") as f:
            w = _csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader(); w.writerows(rows)
        print("[ok] wrote %s" % args.csv)
    conn.execute("DROP TABLE _ads")
    conn.close()


if __name__ == "__main__":
    main()
