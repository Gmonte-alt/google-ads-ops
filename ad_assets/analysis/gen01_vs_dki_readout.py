#!/usr/bin/env python
"""
gen01_vs_dki_readout.py -- ad_assets/analysis/ -- early readout: the new
GEN-01 generic RSAs (launched 2026-09-23, DKI-free) vs the incumbent DKI
copy, in ad groups whose name contains 'Home-Generic'.

Reads (each answers a different question):
  1. Per-group composition -- which ads are live where, classified
     structurally: DKI (headline set contains {KeyWord) vs GEN-01
     (no DKI, first seen >= --new-since) vs LEGACY_NONDKI (no DKI, older).
  2. Cohort head-to-head IN-WINDOW -- serve share (Google's revealed
     preference under co-serving), CTR, CR, VPC at ad grain (additive).
  3. Weekly serve-share trend -- is GEN-01 gaining allocation?
  4. Incumbent BEFORE/AFTER -- the DKI cohort's own pre-period vs
     in-window (cannibalization vs complement).
  5. Fingerprint reconciliation -- distinct GEN-01 fingerprints found vs
     the 8 pre-registered provisionals.

NOT in this script: the vs-baseline read. The pinned GEN-01 baselines are
QUERY-grain (sqp) -- compare at that grain by rerunning
intent_theme_evidence.py --from 2026-09-23 against the pinned table.

EARLY-READ CAVEAT (printed in output): new ads are in learning-phase
exploration; the pre-declared verdict is per-theme VPC vs pinned baselines
at the +28d read (~2026-10-21). READ-ONLY.

  python gen01_vs_dki_readout.py --db "C:\\Projects\\google-ads-ops\\db\\paid_search.db"
"""
import argparse
import datetime
import sqlite3
import sys

ACCOUNT_ID = 1
EXPECTED_FPS = {"c97dbf7dc3": "Quote", "1301f56a90": "Cost", "e937fe75f5": "Compare",
                "388e3476a6": "Core", "5123fbe031": "State", "e96b6d1816": "Peril",
                "4740d4a5d3": "Flood", "bb4ee9d935": "Other", "f671113399": "Bundle"}
# Flood/Other are AS-BUILT hashes (10/6): one line added to each at build,
# re-verified against the canonical recipe; provisionals 16cfe489b2/38fe7d4e48 retired.


def metrics(conn, ad_ids, d1, d2):
    if not ad_ids:
        return (0, 0, 0, 0.0)
    marks = ",".join("?" * len(ad_ids))
    r = conn.execute(
        f"""SELECT COALESCE(SUM(impressions),0), COALESCE(SUM(clicks),0),
                   COALESCE(SUM(conversions),0), COALESCE(SUM(conversions_value),0)
            FROM rsa_ad_performance_daily
            WHERE account_id=? AND device='ALL' AND report_date BETWEEN ? AND ?
              AND ad_id IN ({marks})""",
        [ACCOUNT_ID, d1, d2] + list(ad_ids)).fetchone()
    return r


def line(label, m):
    impr, clicks, conv, val = m
    return ("%-28s impr %9s  clicks %7s  CTR %5.2f%%  CR %6.2f%%  VPC %6s"
            % (label, f"{impr:,}", f"{clicks:,}",
               100.0 * clicks / impr if impr else 0,
               100.0 * conv / clicks if clicks else 0,
               ("$%.2f" % (val / clicks)) if clicks else "--"))


def main():
    p = argparse.ArgumentParser(description="GEN-01 vs DKI early readout (read-only).")
    p.add_argument("--db", required=True)
    p.add_argument("--launch", default="2026-09-23", help="GEN-01 first-serve date")
    p.add_argument("--new-since", default="2026-09-20",
                   help="first_seen cutoff for classifying an ad as new")
    p.add_argument("--to", dest="date_to",
                   default=(datetime.date.today() - datetime.timedelta(days=1)).isoformat())
    p.add_argument("--pre-weeks", type=int, default=4, help="incumbent pre-period length")
    args = p.parse_args()
    conn = sqlite3.connect("file:%s?mode=ro" % args.db.replace("\\", "/"), uri=True)

    launch = datetime.date.fromisoformat(args.launch)
    pre1 = (launch - datetime.timedelta(weeks=args.pre_weeks)).isoformat()
    pre2 = (launch - datetime.timedelta(days=1)).isoformat()

    # scope: Home-Generic ad groups (id set once)
    groups = conn.execute(
        """SELECT ad_group_id, ad_group_name FROM ad_groups
           WHERE instr(lower(ad_group_name),'home-generic')
           ORDER BY ad_group_name""").fetchall()
    if not groups:
        sys.exit("[ABORT] no ad groups matching 'Home-Generic'.")
    ag_ids = [g[0] for g in groups]
    marks = ",".join("?" * len(ag_ids))

    # classify every enabled RSA in scope, structurally
    ads = conn.execute(
        f"""SELECT r.ad_id, r.ad_group_id, r.fingerprint,
                   COALESCE(r.variant_label,''), COALESCE(r.first_seen_date,''),
                   MAX(CASE WHEN instr(lower(t.text),'{{keyword') THEN 1 ELSE 0 END)
            FROM rsa_ads r
            JOIN rsa_ad_assets c ON c.account_id=r.account_id AND c.ad_id=r.ad_id
             AND c.field_type='HEADLINE' AND c.removed_date IS NULL
            JOIN asset_text_data t ON t.account_id=c.account_id AND t.asset_id=c.asset_id
            WHERE r.account_id=? AND r.ad_status='ENABLED' AND r.ad_group_id IN ({marks})
            GROUP BY r.ad_id""",
        [ACCOUNT_ID] + ag_ids).fetchall()
    if not ads:
        sys.exit("[ABORT] no enabled RSAs in scope -- are the 01/02 syncs current?")

    def classify(fp, first_seen, has_dki):
        if has_dki:
            return "DKI"
        if fp in EXPECTED_FPS or (first_seen and first_seen >= args.new_since):
            return "GEN01"
        return "LEGACY_NONDKI"

    by_class = {"DKI": [], "GEN01": [], "LEGACY_NONDKI": []}
    # id types differ across tables (SQLite affinity hides it in SQL; Python
    # dict lookups are strict) -- normalize to str on both sides
    gname = {str(g[0]): g[1] for g in groups}
    print("=" * 96)
    print("1) COMPOSITION -- enabled RSAs per Home-Generic ad group (%d groups in scope)"
          % len(groups))
    print("=" * 96)
    cur_g = None
    for ad_id, ag, fp, lbl, fs, dki in sorted(ads, key=lambda a: (gname[str(a[1])], a[5])):
        cls = classify(fp, fs, dki)
        by_class[cls].append(ad_id)
        if ag != cur_g:
            print("\n%s" % gname[str(ag)]); cur_g = ag
        print("  ad %-14s fp %-11s %-14s %-10s first_seen %s"
              % (ad_id, fp, cls, lbl or "(no label)", fs or "?"))

    print("\n" + "=" * 96)
    print("2) HEAD-TO-HEAD in window %s..%s (co-served: serve share = Google's preference)"
          % (args.launch, args.date_to))
    print("=" * 96)
    totals = {}
    for cls in ("GEN01", "DKI", "LEGACY_NONDKI"):
        totals[cls] = metrics(conn, by_class[cls], args.launch, args.date_to)
        if by_class[cls]:
            print(line("%s (%d ads)" % (cls, len(by_class[cls])), totals[cls]))
    denom = sum(t[0] for t in totals.values()) or 1
    print("serve share: GEN01 %.1f%% | DKI %.1f%% | legacy %.1f%%"
          % tuple(100.0 * totals[c][0] / denom for c in ("GEN01", "DKI", "LEGACY_NONDKI")))

    print("\n" + "=" * 96)
    print("3) WEEKLY SERVE-SHARE TREND (is GEN-01 gaining allocation?)")
    print("=" * 96)
    wk = launch
    end = datetime.date.fromisoformat(args.date_to)
    while wk <= end:
        w2 = min(wk + datetime.timedelta(days=6), end)
        g = metrics(conn, by_class["GEN01"], wk.isoformat(), w2.isoformat())
        d = metrics(conn, by_class["DKI"], wk.isoformat(), w2.isoformat())
        tot = g[0] + d[0]
        if tot:
            print("  %s..%s  GEN01 %5.1f%%  (GEN01 CR %5.2f%% | DKI CR %5.2f%%)"
                  % (wk, w2, 100.0 * g[0] / tot,
                     100.0 * g[2] / g[1] if g[1] else 0,
                     100.0 * d[2] / d[1] if d[1] else 0))
        wk = w2 + datetime.timedelta(days=1)

    print("\n" + "=" * 96)
    print("4) INCUMBENT DKI BEFORE/AFTER (same ads, pre %s..%s vs in-window)" % (pre1, pre2))
    print("=" * 96)
    print(line("DKI pre-period", metrics(conn, by_class["DKI"], pre1, pre2)))
    print(line("DKI in-window", totals["DKI"]))

    print("\n" + "=" * 96)
    print("5) FINGERPRINT RECONCILIATION (found vs pre-registered provisionals)")
    print("=" * 96)
    found = {fp for _, _, fp, _, _, dki in ads if not dki}
    for fp, theme in EXPECTED_FPS.items():
        print("  %-8s %-11s %s" % (theme, fp, "FOUND" if fp in found else "not found"))
    extra = found - set(EXPECTED_FPS)
    if extra:
        print("  non-DKI fingerprints NOT in the registration: %s" % sorted(extra))
        print("  -> strings changed at build, or legacy non-DKI ads; if GEN-01 ads,")
        print("     recompute + re-register before the +28d read cites fingerprints.")
    conn.close()

    print("\nCAVEATS: ~%d days of serving = learning-phase exploration; this is the"
          % ((end - launch).days + 1))
    print("EARLY read, not the verdict. Pre-declared judgment = per-theme VPC vs the")
    print("pinned baselines at +28d (~2026-10-21), at QUERY grain -- run")
    print("intent_theme_evidence.py --from %s and compare nonbrand rows to" % args.launch)
    print("gen01_baselines_pinned_2026-09-15.md. New themed groups have no in-group")
    print("DKI incumbent; their cross-group comparisons carry a different-keywords caveat.")


if __name__ == "__main__":
    main()
