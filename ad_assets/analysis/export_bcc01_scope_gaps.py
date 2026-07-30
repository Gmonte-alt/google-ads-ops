#!/usr/bin/env python
"""
export_bcc01_scope_gaps.py -- ad_assets/analysis/ -- census for the BCC-01
scope ruling: EVERY brand ad group (name rule, Geico/SF INCLUDED) that hosts
at least one ENABLED RSA, with its incumbent creative(s), whether any
enabled ad's copy carries DKI syntax, and whether the group is covered by
the current load (bcc01_target_adgroups CSV).

The gap this answers: the target list was derived from "hosts enabled V1/V3"
(record §6's pause authorization), so brand groups with OTHER incumbents --
Geico-Core (V4), Costco (V5), etc. -- fell out entirely, creates included.
V4 still CARRIES DKI lines (~2.5% served, but the flag is about copy), so
the flagged Geico territory is not remediated by the load as scoped. This
census is the factual basis for the strategy thread's §6 amendment decision.

Output: ad_assets\\exports\\bcc01_scope_gaps_<date>.csv
READ-ONLY.

  python export_bcc01_scope_gaps.py --db "...\\paid_search.db" ^
      --targets-csv "...\\exports\\bcc01_target_adgroups_2026-07-23.csv"
"""
import argparse
import csv
import datetime
import os
import re
import sqlite3
import sys
from collections import defaultdict

ACCOUNT_ID = 1
FLAG_PATTERNS = ["geico", "state farm", "statefarm"]
NONBRAND_OVERRIDES = ["caregio"]
EXPORT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "exports")
NAME_TABLE_CANDIDATES = [
    ("ad_groups", "ad_group_id", "ad_group_name"),
    ("ad_groups", "ad_group_id", "name"),
    ("ad_groups", "id", "name"),
]


def find_names(conn):
    for table, idc, namec in NAME_TABLE_CANDIDATES:
        for scoped in (True, False):
            try:
                q = f"SELECT {idc}, {namec} FROM {table}"
                params = (ACCOUNT_ID,) if scoped else ()
                if scoped:
                    q += " WHERE account_id = ?"
                return {int(i): (n or "") for i, n in conn.execute(q, params).fetchall()}
            except sqlite3.Error:
                continue
    sys.exit("[ABORT] no ad-group name table found.")


def is_brand(name):
    norm = re.sub(r"[-\s_]", "", name.lower())
    if any(ov in norm for ov in NONBRAND_OVERRIDES):
        return False
    return "nongeneric" in norm


def main():
    p = argparse.ArgumentParser(description="Brand ad-group scope census for BCC-01.")
    p.add_argument("--db", required=True)
    p.add_argument("--targets-csv", required=True)
    args = p.parse_args()
    for f in (args.db, args.targets_csv):
        if not os.path.isfile(f):
            sys.exit("[ABORT] not found: %s" % f)
    os.makedirs(EXPORT_DIR, exist_ok=True)

    with open(args.targets_csv, newline="", encoding="utf-8") as f:
        in_targets = {int(r["ad_group_id"]) for r in csv.DictReader(f)}

    conn = sqlite3.connect("file:%s?mode=ro" % args.db.replace("\\", "/"), uri=True)
    names = find_names(conn)

    # every enabled RSA with a DKI-in-copy marker
    rows = conn.execute(
        """SELECT r.ad_group_id, r.campaign_id, r.ad_id,
                  COALESCE(r.variant_label, r.fingerprint) AS variant,
                  EXISTS (SELECT 1 FROM rsa_ad_assets c
                          JOIN asset_text_data t
                            ON t.account_id=c.account_id AND t.asset_id=c.asset_id
                          WHERE c.account_id=r.account_id AND c.ad_id=r.ad_id
                            AND c.removed_date IS NULL
                            AND LOWER(t.text) LIKE '%{keyword%') AS carries_dki
           FROM rsa_ads r
           WHERE r.account_id=? AND r.ad_status='ENABLED'""",
        (ACCOUNT_ID,)).fetchall()
    conn.close()

    groups = defaultdict(lambda: {"camp": None, "ads": [], "dki": False})
    for ag, camp, ad_id, variant, dki in rows:
        name = names.get(ag, "")
        if not name or not is_brand(name):
            continue
        g = groups[ag]
        g["camp"] = camp
        g["ads"].append("%s%s" % (variant, "*DKI" if dki else ""))
        g["dki"] = g["dki"] or bool(dki)

    stamp = datetime.date.today().isoformat()
    out = os.path.join(EXPORT_DIR, "bcc01_scope_gaps_%s.csv" % stamp)
    n_gap = n_gap_dki = 0
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["ad_group_id", "ad_group_name", "campaign_id", "flagged_geico_sf",
                    "enabled_ads", "any_ad_carries_dki", "in_current_load", "verdict"])
        for ag in sorted(groups, key=lambda a: (names[a], groups[a]["camp"] or 0)):
            g = groups[ag]
            name = names[ag]
            flagged = any(p in name.lower() for p in FLAG_PATTERNS)
            covered = ag in in_targets
            if covered:
                verdict = "covered by load"
            elif g["dki"]:
                verdict = "GAP: DKI copy live, not in load"
                n_gap += 1
                n_gap_dki += 1
            else:
                verdict = "gap: no DKI (compliant incumbent), not in load"
                n_gap += 1
            w.writerow([ag, name, g["camp"], "YES" if flagged else "no",
                        "; ".join(sorted(g["ads"])), "YES" if g["dki"] else "no",
                        "yes" if covered else "NO", verdict])

    total = len(groups)
    print("[DONE] %d brand ad group(s) with enabled RSAs -> %s" % (total, out))
    print("[SCOPE] covered by current load: %d | gaps: %d (of which %d carry live "
          "DKI copy)" % (total - n_gap, n_gap, n_gap_dki))
    gap_rows = [(ag, names[ag], groups[ag]["ads"]) for ag in groups
                if ag not in in_targets and groups[ag]["dki"]]
    if gap_rows:
        print("[GAPS WITH LIVE DKI COPY] (the strategy thread's amendment decides these):")
        for ag, name, ads in sorted(gap_rows, key=lambda r: r[1])[:20]:
            print("   %-14d %-48s %s" % (ag, name[:48], "; ".join(sorted(ads))))


if __name__ == "__main__":
    main()
