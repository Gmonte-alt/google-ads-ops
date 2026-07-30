#!/usr/bin/env python
"""
variant_inspector.py -- ad_assets/analysis/ -- expose what each RSA variant
actually IS: its headline lineup, how Google actually serves it, and how it
differs from the incumbent.

For each variant (fingerprint = normalized headline set):
  * where it runs + additive performance summary (from rsa_ad_performance_daily)
  * its headlines, each with SHOWN-ON%% -- the share of the ad's impressions
    that headline appeared on (valid within-ad relative read; shares sum to
    ~278%% across headlines because ~2.78 co-serve per impression)
  * a DIFF vs a baseline variant (default V1): [shared] / [UNIQUE] / lines
    the baseline has that this variant dropped
  * its descriptions (union across the variant's ads)

READ-ONLY. Requires variant_label to be tagged (variant_campaign_readout.py
--tag-from-csv); untagged ads group under their fingerprint.

  python variant_inspector.py --db "...\\paid_search.db"                     # all variants >= floor
  python variant_inspector.py --db "...\\paid_search.db" --variant V4        # one variant
  python variant_inspector.py --db "...\\paid_search.db" --variant V18 --vs V1
"""
import argparse
import os
import re
import sqlite3
import sys
from collections import defaultdict

ACCOUNT_ID = 1


def norm(text):
    return re.sub(r"\s+", " ", text).strip().lower()


def variant_expr():
    return "COALESCE(r.variant_label, r.fingerprint)"


def load_variants(conn, floor, only=None):
    rows = conn.execute(
        f"""SELECT {variant_expr()} AS v,
                   GROUP_CONCAT(DISTINCT r.campaign_id),
                   COUNT(DISTINCT r.ad_id),
                   SUM(p.clicks), SUM(p.conversions), SUM(p.conversions_value),
                   SUM(p.impressions)
            FROM rsa_ads r
            LEFT JOIN rsa_ad_performance_daily p
              ON p.account_id = r.account_id AND p.ad_id = r.ad_id AND p.device='ALL'
            WHERE r.account_id = ?
            GROUP BY v""", (ACCOUNT_ID,)).fetchall()
    out = {}
    for v, camps, n_ads, clicks, conv, value, impr in rows:
        if only and v != only:
            continue
        if not only and (clicks or 0) < floor:
            continue
        out[v] = {"campaigns": camps, "n_ads": n_ads, "clicks": clicks or 0,
                  "conv": conv or 0.0, "value": value or 0.0, "impr": impr or 0}
    return out


def load_lineup(conn, variant):
    """Headlines + serving weight for a variant. Serving weight = asset
    impressions / ad impressions, summed across the variant's ads."""
    ads = [r[0] for r in conn.execute(
        f"""SELECT r.ad_id FROM rsa_ads r
            WHERE r.account_id=? AND {variant_expr()} = ?""",
        (ACCOUNT_ID, variant)).fetchall()]
    if not ads:
        return [], [], 0
    marks = ",".join("?" * len(ads))

    ad_impr = conn.execute(
        f"""SELECT SUM(impressions) FROM rsa_ad_performance_daily
            WHERE account_id=? AND device='ALL' AND ad_id IN ({marks})""",
        [ACCOUNT_ID] + ads).fetchone()[0] or 0

    def lines(field_type):
        rows = conn.execute(
            f"""SELECT t.text, SUM(ap.impressions)
                FROM rsa_ad_assets c
                JOIN asset_text_data t
                  ON t.account_id=c.account_id AND t.asset_id=c.asset_id
                LEFT JOIN rsa_asset_performance_daily ap
                  ON ap.account_id=c.account_id AND ap.ad_id=c.ad_id
                 AND ap.asset_id=c.asset_id AND ap.field_type=c.field_type
                 AND ap.device='ALL'
                WHERE c.account_id=? AND c.field_type=? AND c.ad_id IN ({marks})
                GROUP BY t.text""",
            [ACCOUNT_ID, field_type] + ads).fetchall()
        agg = defaultdict(int)
        for text, impr in rows:
            agg[text] = max(agg[text], impr or 0)  # dedupe same text across ads
        return sorted(agg.items(), key=lambda kv: -kv[1]), ad_impr
    heads, _ = lines("HEADLINE")
    descs, _ = lines("DESCRIPTION")
    return heads, descs, ad_impr


def main():
    p = argparse.ArgumentParser(description="Expose RSA variant copy, serving, and diffs.")
    p.add_argument("--db", required=True)
    p.add_argument("--variant", default=None, help="Inspect one variant (e.g. V4)")
    p.add_argument("--vs", default="V1", help="Baseline for the diff (default V1)")
    p.add_argument("--floor", type=int, default=300,
                   help="Skip variants under this many total clicks (default 300)")
    p.add_argument("--out", action="store_true",
                   help="Also write the report to ad_assets\\exports\\"
                        "variant_anatomy_<variant>_<date>.txt (hand-off rule: "
                        "outputs go into files)")
    args = p.parse_args()

    if args.out:
        export_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                  "exports")
        os.makedirs(export_dir, exist_ok=True)
        out_path = os.path.join(export_dir, "variant_anatomy_%s_%s.txt"
                                % (args.variant or "all",
                                   __import__("datetime").date.today().isoformat()))

        class _Tee:
            def __init__(self, path, stdout):
                self._f = open(path, "w", encoding="utf-8")
                self._stdout = stdout

            def write(self, s):
                self._f.write(s)
                self._stdout.write(s)

            def flush(self):
                self._f.flush()
                self._stdout.flush()

        sys.stdout = _Tee(out_path, sys.stdout)
        print("[OUT] writing copy of this report to %s" % out_path)

    if not os.path.isfile(args.db):
        sys.exit("[ABORT] database not found: %s" % args.db)
    conn = sqlite3.connect("file:%s?mode=ro" % args.db.replace("\\", "/"), uri=True)

    variants = load_variants(conn, args.floor, only=args.variant)
    if not variants:
        sys.exit("No variants matched (check --variant spelling or lower --floor).")

    base_heads, _, _ = load_lineup(conn, args.vs)
    base_set = {norm(t) for t, _ in base_heads}

    for v in sorted(variants, key=lambda x: -variants[x]["clicks"]):
        info = variants[v]
        heads, descs, ad_impr = load_lineup(conn, v)
        cvr = 100.0 * info["conv"] / info["clicks"] if info["clicks"] else 0
        print("=" * 78)
        print("%s -- %d ad(s) | campaigns: %s" % (v, info["n_ads"], info["campaigns"]))
        print("    %s clicks | %.1f conv | CVR %.1f%% | $%.0f value"
              % (f"{info['clicks']:,}", info["conv"], cvr, info["value"]))
        this_set = {norm(t) for t, _ in heads}
        if v != args.vs and base_set:
            shared = len(this_set & base_set)
            unique = len(this_set - base_set)
            dropped = sorted(t for t, _ in base_heads if norm(t) not in this_set)
            print("    vs %s: %d shared / %d unique / %d of %s's lines dropped"
                  % (args.vs, shared, unique, len(dropped), args.vs))
        print("-" * 78)
        print("HEADLINES (shown-on %% of ad impressions; ~2.78x co-serve so column sums >100%%):")
        for text, impr in heads:
            share = 100.0 * impr / ad_impr if ad_impr else 0
            tag = ""
            if v != args.vs and base_set:
                tag = "[shared]" if norm(text) in base_set else "[UNIQUE]"
            dead = "  <-- never served" if impr == 0 and ad_impr else ""
            print("  %5.1f%%  %-62s %s%s" % (share, text[:62], tag, dead))
        if v != args.vs and base_set:
            dropped = [t for t, _ in base_heads if norm(t) not in this_set]
            if dropped:
                print("  DROPPED from %s:" % args.vs)
                for t in dropped:
                    print("          %s" % t[:70])
        print("DESCRIPTIONS:")
        for text, impr in descs:
            share = 100.0 * impr / ad_impr if ad_impr else 0
            print("  %5.1f%%  %s" % (share, text[:70]))
    print("=" * 78)


if __name__ == "__main__":
    main()
