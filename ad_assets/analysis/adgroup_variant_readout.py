#!/usr/bin/env python
"""
adgroup_variant_readout.py -- ad_assets/analysis/ -- variant x AD GROUP
readout on the additive ad grain: the empirical input for the strategy
thread's intent-routing table (ad group -> intent -> variant to deploy).

Per strategy-thread request 2026-07-23:
  * grain: variant x ad_group x campaign, all six campaigns, since --from
  * cells below the 300-click floor are REPORTED, not hidden, with a
    confidence tier: HIGH (>=300 clicks), MEDIUM (100-299), LOW (<100).
    Treat MEDIUM as directional, LOW as inventory-only.
  * Geico / State Farm ad groups EXCLUDED (limited-visibility flags).
    Exclusion works by ad-group NAME when a name table exists in the DB
    (auto-detected), plus explicit --exclude-adgroups ids. Everything
    excluded is printed, so the cut is auditable.
  * optional --class-csv <file> with columns ad_group_id,query_class merges
    a dominant-query-class column (the DB's rsa tables don't carry query
    class natively; supply the mapping or name the DB table and the
    measurement thread wires the join).

Output: CSV in ad_assets\\exports\\ + a printed summary of HIGH cells.
READ-ONLY.

  python adgroup_variant_readout.py --db "...\\paid_search.db"
  python adgroup_variant_readout.py --db "...\\paid_search.db" --class-csv groups.csv --exclude-adgroups 111,222
"""
import argparse
import csv
import datetime
import os
import sqlite3
import sys

ACCOUNT_ID = 1
DEFAULT_EXCLUDE_PATTERNS = ["geico", "state farm", "statefarm"]
EXPORT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "exports")

# candidate (table, id column, name column) triplets for ad-group names in
# the wider paid_search.db -- first one that works wins
NAME_TABLE_CANDIDATES = [
    ("ad_groups", "ad_group_id", "ad_group_name"),
    ("ad_groups", "ad_group_id", "name"),
    ("ad_groups", "id", "name"),
    ("adgroups", "ad_group_id", "ad_group_name"),
    ("ad_group_history", "ad_group_id", "ad_group_name"),
]


def find_names(conn):
    for table, idc, namec in NAME_TABLE_CANDIDATES:
        try:
            rows = conn.execute(
                f"SELECT {idc}, {namec} FROM {table} WHERE account_id = ?",
                (ACCOUNT_ID,)).fetchall()
            print("[NAMES] using %s.%s (%d rows)" % (table, namec, len(rows)))
            return {int(i): (n or "") for i, n in rows}
        except sqlite3.Error:
            try:  # retry without account_id scoping if the table lacks it
                rows = conn.execute(f"SELECT {idc}, {namec} FROM {table}").fetchall()
                print("[NAMES] using %s.%s (%d rows, unscoped)" % (table, namec, len(rows)))
                return {int(i): (n or "") for i, n in rows}
            except sqlite3.Error:
                continue
    print("[NAMES] no ad-group name table found -- name-based exclusion "
          "unavailable; use --exclude-adgroups with explicit ids.")
    return {}


def tier(clicks):
    return "HIGH" if clicks >= 300 else ("MEDIUM" if clicks >= 100 else "LOW")


def main():
    p = argparse.ArgumentParser(description="Variant x ad group readout (additive grain).")
    p.add_argument("--db", required=True)
    p.add_argument("--from", dest="date_from", default="2026-01-01", metavar="YYYY-MM-DD")
    p.add_argument("--exclude-name-like", default=",".join(DEFAULT_EXCLUDE_PATTERNS),
                   help="Comma-separated name substrings to exclude (default geico,state farm,statefarm)")
    p.add_argument("--exclude-adgroups", default="",
                   help="Comma-separated ad_group_ids to exclude explicitly")
    p.add_argument("--class-csv", default=None,
                   help="Optional CSV with columns ad_group_id,query_class")
    args = p.parse_args()

    if not os.path.isfile(args.db):
        sys.exit("[ABORT] database not found: %s" % args.db)
    os.makedirs(EXPORT_DIR, exist_ok=True)
    conn = sqlite3.connect("file:%s?mode=ro" % args.db.replace("\\", "/"), uri=True)

    names = find_names(conn)
    patterns = [s.strip().lower() for s in args.exclude_name_like.split(",") if s.strip()]
    excluded = {int(x) for x in args.exclude_adgroups.split(",") if x.strip()}
    for ag, name in names.items():
        if any(pat in name.lower() for pat in patterns):
            excluded.add(ag)

    classes = {}
    if args.class_csv:
        with open(args.class_csv, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                classes[int(row["ad_group_id"])] = row.get("query_class", "")
        print("[CLASS] loaded query classes for %d ad groups" % len(classes))

    rows = conn.execute(
        """SELECT COALESCE(r.variant_label, r.fingerprint) AS variant,
                  r.ad_group_id, r.campaign_id,
                  SUM(p.clicks), SUM(p.conversions), SUM(p.conversions_value)
           FROM rsa_ad_performance_daily p
           JOIN rsa_ads r ON r.account_id = p.account_id AND r.ad_id = p.ad_id
           WHERE p.account_id = ? AND p.device = 'ALL' AND p.report_date >= ?
           GROUP BY variant, r.ad_group_id, r.campaign_id
           HAVING SUM(p.clicks) > 0
           ORDER BY r.ad_group_id, SUM(p.conversions_value) DESC""",
        (ACCOUNT_ID, args.date_from)).fetchall()
    conn.close()

    if excluded:
        n_before = len(rows)
        rows = [r for r in rows if r[1] not in excluded]
        cut = sorted(excluded)
        print("[EXCLUDE] %d ad group(s) excluded (%d cells removed): %s"
              % (len(cut), n_before - len(rows),
                 ", ".join("%d%s" % (ag, " '%s'" % names[ag] if ag in names else "")
                           for ag in cut)))

    stamp = datetime.date.today().isoformat()
    out_path = os.path.join(EXPORT_DIR, "adgroup_variant_readout_%s.csv" % stamp)
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["variant", "ad_group_id", "ad_group_name", "campaign_id",
                    "query_class", "clicks", "conversions", "value",
                    "cvr_pct", "vpc", "confidence"])
        for v, ag, camp, clicks, conv, value in rows:
            clicks = clicks or 0
            conv = conv or 0.0
            value = value or 0.0
            w.writerow([v, ag, names.get(ag, ""), camp, classes.get(ag, ""),
                        clicks, round(conv, 1), round(value, 0),
                        round(100.0 * conv / clicks, 1) if clicks else "",
                        round(value / clicks, 2) if clicks else "",
                        tier(clicks)])

    high = [(v, ag, camp, c, cv, val) for v, ag, camp, c, cv, val in rows if (c or 0) >= 300]
    print("\n[SUMMARY] %d cells written (%d HIGH / %d MEDIUM / %d LOW) -> %s"
          % (len(rows), len(high),
             sum(1 for r in rows if 100 <= (r[3] or 0) < 300),
             sum(1 for r in rows if (r[3] or 0) < 100), out_path))
    print("\nHIGH-confidence cells (decision-grade), by VPC:")
    print("%-12s %-14s %-28s %8s %7s %7s" % ("variant", "ad_group", "name", "clicks", "CVR%", "VPC$"))
    for v, ag, camp, c, cv, val in sorted(high, key=lambda r: -((r[5] or 0) / r[3])):
        print("%-12s %-14s %-28s %8d %7.1f %7.2f"
              % (v, ag, names.get(ag, "")[:28], c, 100.0 * cv / c, val / c))


if __name__ == "__main__":
    main()
