#!/usr/bin/env python
"""
flagship_line_evidence.py -- ad_assets/analysis/ -- BCC-01-methodology line
evidence table for the flagship pinned-RSA composition (FLG family). For every
headline serving on ENABLED ads in BRAND (NonGeneric) ad groups: shown-on
impressions, share of scope, click rate, ad/fingerprint spread, DKI flag, and
BCC-01 membership -- so pin groups and the slot-3 pool are chosen on serve
allocation + click evidence, not taste.

Methodology (matches the BCC-01 decision record):
  * scope = enabled ads in ad groups whose normalized name contains
    'nongeneric' (both spellings), Geico/State Farm groups EXCLUDED from
    comparison by default (--include-flagged to widen; flag-territory data
    answers remediation questions, not creative ones)
  * metrics are PRESENCE-CONDITIONED (headline co-serve; see A5 ratio) --
    shown-on %% is Google's serve allocation (revealed preference), CR is
    face-conditioned click rate. Neither attributes causal credit per line.
  * shown-on share denominator = SUM of the scope ads' ad-grain impressions
    (rsa_ad_performance_daily -- additive), per the record's convention.

Output: exports\\flagship_line_evidence_<date>.csv + top-25 console table.
READ-ONLY.

  python flagship_line_evidence.py --db "C:\\Projects\\google-ads-ops\\db\\paid_search.db"
  python flagship_line_evidence.py --db "..." --from 2026-06-01
"""
import argparse
import csv
import datetime
import os
import sqlite3
import sys

ACCOUNT_ID = 1
BCC01_FP = "e1fd7d1595"
EXPORT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "exports")


def main():
    p = argparse.ArgumentParser(description="Line evidence for flagship pinned composition.")
    p.add_argument("--db", required=True)
    p.add_argument("--from", dest="date_from", default="2026-01-01", metavar="YYYY-MM-DD")
    p.add_argument("--to", dest="date_to",
                   default=(datetime.date.today() - datetime.timedelta(days=1)).isoformat())
    p.add_argument("--include-flagged", action="store_true",
                   help="Include Geico/State Farm groups (default: excluded)")
    p.add_argument("--scope", choices=["brand", "generic"], default="brand",
                   help="brand = NonGeneric ad groups (default); generic = the "
                        "complement (non-brand ad groups -- generic-intent traffic)")
    args = p.parse_args()
    if not os.path.isfile(args.db):
        sys.exit("[ABORT] database not found: %s" % args.db)
    os.makedirs(EXPORT_DIR, exist_ok=True)
    conn = sqlite3.connect("file:%s?mode=ro" % args.db.replace("\\", "/"), uri=True)

    # brand-scope ad groups by id (names build the id set ONCE; joins on ids)
    flag_clause = "" if args.include_flagged else \
        " AND instr(lower(ad_group_name),'geico')=0" \
        " AND instr(lower(ad_group_name),'state farm')=0"
    scope_test = ("instr(replace(lower(ad_group_name),'-',''),'nongeneric')"
                  if args.scope == "brand" else
                  "instr(replace(lower(ad_group_name),'-',''),'nongeneric')=0")
    ag_ids = [r[0] for r in conn.execute(
        "SELECT ad_group_id FROM ad_groups WHERE " + scope_test + flag_clause)]
    if not ag_ids:
        sys.exit("[ABORT] no %s-scope ad groups found." % args.scope)
    marks = ",".join("?" * len(ag_ids))
    print("[SCOPE] %s: %d ad group(s)%s | window %s..%s"
          % (args.scope.upper(), len(ag_ids),
             "" if args.include_flagged else " (Geico/SF excluded)",
             args.date_from, args.date_to))

    # scope denominator: additive ad-grain impressions of the scope's enabled ads
    denom = conn.execute(
        f"""SELECT COALESCE(SUM(ap.impressions),0)
            FROM rsa_ad_performance_daily ap
            JOIN rsa_ads r ON r.account_id=ap.account_id AND r.ad_id=ap.ad_id
            WHERE ap.account_id=? AND ap.device='ALL'
              AND ap.report_date BETWEEN ? AND ?
              AND r.ad_status='ENABLED' AND r.ad_group_id IN ({marks})""",
        [ACCOUNT_ID, args.date_from, args.date_to] + ag_ids).fetchone()[0] or 1

    bcc01_assets = {r[0] for r in conn.execute(
        """SELECT DISTINCT c.asset_id FROM rsa_ad_assets c
           JOIN rsa_ads r ON r.account_id=c.account_id AND r.ad_id=c.ad_id
           WHERE r.account_id=? AND r.fingerprint=? AND c.field_type='HEADLINE'""",
        (ACCOUNT_ID, BCC01_FP))}

    rows = conn.execute(
        f"""SELECT LOWER(TRIM(t.text)), MIN(t.text), c.asset_id,
                   COUNT(DISTINCT c.ad_id), COUNT(DISTINCT r.fingerprint),
                   COALESCE(SUM(ap.impressions),0), COALESCE(SUM(ap.clicks),0),
                   COALESCE(SUM(ap.conversions),0), COALESCE(SUM(ap.conversions_value),0)
            FROM asset_text_data t
            JOIN rsa_ad_assets c ON c.account_id=t.account_id AND c.asset_id=t.asset_id
             AND c.field_type='HEADLINE' AND c.removed_date IS NULL
            JOIN rsa_ads r ON r.account_id=c.account_id AND r.ad_id=c.ad_id
            LEFT JOIN rsa_asset_performance_daily ap
              ON ap.account_id=c.account_id AND ap.ad_id=c.ad_id
             AND ap.asset_id=c.asset_id AND ap.field_type='HEADLINE'
             AND ap.device='ALL' AND ap.report_date BETWEEN ? AND ?
            WHERE t.account_id=? AND r.ad_status='ENABLED'
              AND r.ad_group_id IN ({marks})
            GROUP BY LOWER(TRIM(t.text))""",
        [args.date_from, args.date_to, ACCOUNT_ID] + ag_ids).fetchall()
    conn.close()

    out = []
    for norm, text, aid, n_ads, n_fps, impr, clicks, conv, val in rows:
        tl = text.lower()
        out.append({
            "line": text,
            "is_dki": "YES" if "{keyword" in tl else "no",
            "in_bcc01": "YES" if aid in bcc01_assets else "no",
            "n_ads": n_ads, "n_fingerprints": n_fps,
            "impr_shown_on": impr,
            "shown_on_share_pct": round(100.0 * impr / denom, 1),
            "clicks": clicks,
            "cr_pct": round(100.0 * clicks / impr, 2) if impr else 0,
            "conversions": conv,
            "conv_value": round(val, 0),
            "len_chars": len(text),
        })
    out.sort(key=lambda r: -r["impr_shown_on"])

    stamp = datetime.date.today().isoformat()
    path = os.path.join(EXPORT_DIR, "%s_line_evidence_%s.csv"
                        % ("flagship" if args.scope == "brand" else "generic", stamp))
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(out[0].keys()))
        w.writeheader()
        w.writerows(out)
    print("[DONE] %d unique headline(s) -> %s" % (len(out), path))
    print("\nTop 25 by serve allocation (shown-on impressions):")
    print("%-44s %4s %6s %7s %7s %6s %5s" % ("line", "DKI", "BCC01", "shown%", "CR%", "convs", "len"))
    for r in out[:25]:
        print("%-44s %4s %6s %6.1f%% %6.2f%% %6.0f %5d"
              % (r["line"][:44], r["is_dki"], r["in_bcc01"],
                 r["shown_on_share_pct"], r["cr_pct"], r["conversions"], r["len_chars"]))
    print("\nCaveat on every number: presence-conditioned under headline co-serve;")
    print("shown-on = Google's allocation preference, CR = face-conditioned. Pin-group")
    print("candidates: high shown-on + high CR + <=30 chars; slot-3 pool: proven serve,")
    print("nothing compliance-critical (position 3 does not always render).")


if __name__ == "__main__":
    main()
