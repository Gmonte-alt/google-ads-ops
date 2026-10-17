#!/usr/bin/env python
"""
headline_evidence_windows.py -- ad_assets/analysis/ -- the July top-headline
exercise (BCC-01 record 4 methodology), reproduced across FOUR windows in
one run: Last 30 / 60 / 90 days + YTD, for BOTH scopes (brand = NonGeneric
ad groups, Geico/SF excluded per convention; generic = complement).

Per headline x scope: shown-on impressions, shown-on share (vs the scope's
additive ad-grain denominator), CR, ad count, DKI flag, and the ads' labels
(GEN-01/BCC-01/V-family now visible since the label pass) -- each repeated
per window, so one row tells the line's whole trajectory.

Standing caveat (same as July): presence-conditioned under headline
co-serve; shown-on = Google's allocation preference, CR = face-conditioned
click rate. L30 is pure GEN-01-era serving; L90 straddles the 9/23 launch.

Output: exports\\headline_evidence_windows_<date>.csv + console top-20 per
scope by L30 shown-on. READ-ONLY.

  python headline_evidence_windows.py --db "C:\\Projects\\google-ads-ops\\db\\paid_search.db"
"""
import argparse
import csv
import datetime
import os
import sqlite3
import sys

ACCOUNT_ID = 1
EXPORT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "exports")


def scope_groups(conn, scope):
    test = ("instr(replace(lower(ad_group_name),'-',''),'nongeneric')"
            if scope == "brand" else
            "instr(replace(lower(ad_group_name),'-',''),'nongeneric')=0")
    flags = (" AND instr(lower(ad_group_name),'geico')=0"
             " AND instr(lower(ad_group_name),'state farm')=0") if scope == "brand" else ""
    return [r[0] for r in conn.execute(
        "SELECT ad_group_id FROM ad_groups WHERE " + test + flags)]


def main():
    p = argparse.ArgumentParser(description="Top-headline evidence across four windows.")
    p.add_argument("--db", required=True)
    p.add_argument("--to", dest="date_to",
                   default=(datetime.date.today() - datetime.timedelta(days=1)).isoformat())
    args = p.parse_args()
    if not os.path.isfile(args.db):
        sys.exit("[ABORT] database not found: %s" % args.db)
    os.makedirs(EXPORT_DIR, exist_ok=True)
    conn = sqlite3.connect("file:%s?mode=ro" % args.db.replace("\\", "/"), uri=True)

    end = datetime.date.fromisoformat(args.date_to)
    windows = [("L30", (end - datetime.timedelta(days=29)).isoformat()),
               ("L60", (end - datetime.timedelta(days=59)).isoformat()),
               ("L90", (end - datetime.timedelta(days=89)).isoformat()),
               ("YTD", end.replace(month=1, day=1).isoformat())]
    print("[WINDOWS] " + " | ".join("%s %s..%s" % (w, d1, args.date_to)
                                    for w, d1 in windows))

    rows_out = []
    for scope in ("brand", "generic"):
        ag_ids = scope_groups(conn, scope)
        if not ag_ids:
            print("[WARN] no %s-scope ad groups" % scope)
            continue
        marks = ",".join("?" * len(ag_ids))
        print("\n[SCOPE] %s: %d ad group(s)%s"
              % (scope.upper(), len(ag_ids),
                 " (Geico/SF excluded)" if scope == "brand" else ""))

        lines = {}  # norm -> dict
        for wname, d1 in windows:
            denom = conn.execute(
                f"""SELECT COALESCE(SUM(ap.impressions),0)
                    FROM rsa_ad_performance_daily ap
                    JOIN rsa_ads r ON r.account_id=ap.account_id AND r.ad_id=ap.ad_id
                    WHERE ap.account_id=? AND ap.device='ALL'
                      AND ap.report_date BETWEEN ? AND ?
                      AND r.ad_status='ENABLED' AND r.ad_group_id IN ({marks})""",
                [ACCOUNT_ID, d1, args.date_to] + ag_ids).fetchone()[0] or 1
            q = conn.execute(
                f"""SELECT LOWER(TRIM(t.text)), MIN(t.text),
                           COUNT(DISTINCT c.ad_id),
                           MAX(CASE WHEN instr(lower(t.text),'{{keyword') THEN 1 ELSE 0 END),
                           GROUP_CONCAT(DISTINCT r.variant_label),
                           COALESCE(SUM(ap.impressions),0), COALESCE(SUM(ap.clicks),0)
                    FROM asset_text_data t
                    JOIN rsa_ad_assets c ON c.account_id=t.account_id
                     AND c.asset_id=t.asset_id AND c.field_type='HEADLINE'
                     AND c.removed_date IS NULL
                    JOIN rsa_ads r ON r.account_id=c.account_id AND r.ad_id=c.ad_id
                    LEFT JOIN rsa_asset_performance_daily ap
                      ON ap.account_id=c.account_id AND ap.ad_id=c.ad_id
                     AND ap.asset_id=c.asset_id AND ap.field_type='HEADLINE'
                     AND ap.device='ALL' AND ap.report_date BETWEEN ? AND ?
                    WHERE t.account_id=? AND r.ad_status='ENABLED'
                      AND r.ad_group_id IN ({marks})
                    GROUP BY LOWER(TRIM(t.text))""",
                [d1, args.date_to, ACCOUNT_ID] + ag_ids).fetchall()
            for norm, text, n_ads, dki, labels, impr, clicks in q:
                rec = lines.setdefault(norm, {
                    "scope": scope, "line": text,
                    "is_dki": "YES" if dki else "no",
                    "labels": labels or "", "n_ads": n_ads})
                rec["n_ads"] = max(rec["n_ads"], n_ads)
                if labels:
                    rec["labels"] = labels
                rec["%s_shown_pct" % wname] = round(100.0 * impr / denom, 1)
                rec["%s_impr" % wname] = impr
                rec["%s_cr_pct" % wname] = round(100.0 * clicks / impr, 2) if impr else 0

        for rec in lines.values():
            for wname, _ in windows:
                rec.setdefault("%s_shown_pct" % wname, 0)
                rec.setdefault("%s_impr" % wname, 0)
                rec.setdefault("%s_cr_pct" % wname, 0)
        ordered = sorted(lines.values(), key=lambda r: -r["L30_impr"])
        rows_out.extend(ordered)

        print("%-42s %-14s %6s %6s | %6s %6s | %6s %6s" %
              ("line", "labels", "L30sh%", "L30CR", "L90sh%", "L90CR", "YTDsh%", "YTDCR"))
        for r in ordered[:20]:
            print("%-42s %-14s %6.1f %6.2f | %6.1f %6.2f | %6.1f %6.2f"
                  % (r["line"][:42], (r["labels"] or "-")[:14],
                     r["L30_shown_pct"], r["L30_cr_pct"],
                     r["L90_shown_pct"], r["L90_cr_pct"],
                     r["YTD_shown_pct"], r["YTD_cr_pct"]))

    conn.close()
    if not rows_out:
        sys.exit("[ABORT] nothing to export.")
    stamp = datetime.date.today().isoformat()
    path = os.path.join(EXPORT_DIR, "headline_evidence_windows_%s.csv" % stamp)
    fields = list(rows_out[0].keys())
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows_out)
    print("\n[DONE] %d line-rows (both scopes, all windows) -> %s" % (len(rows_out), path))
    print("Reading: L30 = pure GEN-01-era serving; L90 straddles the 9/23 launch;")
    print("a line whose L30 shown%% diverges from its YTD shown%% is a line whose role")
    print("CHANGED with the new creative. Presence-conditioned caveat applies throughout.")


if __name__ == "__main__":
    main()
