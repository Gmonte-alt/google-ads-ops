#!/usr/bin/env python
"""
export_bcc01_load_targets.py -- ad_assets/analysis/ -- generate the BCC-01
load's two companion CSVs from the DB:

  bcc01_target_adgroups_<date>.csv  brand ad groups (all six campaigns,
                                    GEICO/STATE FARM INCLUDED -- load scope,
                                    not measurement scope) where BCC-01 is
                                    created
  bcc01_pause_list_<date>.csv       enabled V1/V3 ads in those ad groups, to
                                    be PAUSED (never deleted)

Brand classification = the standing rule: normalized ad-group name contains
'nongeneric' (both account spellings), CaRegional forced non-brand. The one
difference from measurement readouts: flagged Geico/SF groups are IN (they
are the remediation's first targets); they carry a flagged=YES column so the
import thread sees which ones they are.

Sanity checks printed: targets without a pause candidate, pauses outside the
target list (either -> report back before loading).

READ-ONLY.

  python export_bcc01_load_targets.py --db "C:\\Projects\\google-ads-ops\\db\\paid_search.db"
"""
import argparse
import csv
import datetime
import os
import re
import sqlite3
import sys

ACCOUNT_ID = 1
INCUMBENT_FPS = {"fdb8e75afa": "V1", "9265069966": "V3"}
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
    p = argparse.ArgumentParser(description="Export BCC-01 load target + pause CSVs.")
    p.add_argument("--db", required=True)
    args = p.parse_args()
    if not os.path.isfile(args.db):
        sys.exit("[ABORT] database not found: %s" % args.db)
    os.makedirs(EXPORT_DIR, exist_ok=True)
    conn = sqlite3.connect("file:%s?mode=ro" % args.db.replace("\\", "/"), uri=True)
    names = find_names(conn)

    # enabled incumbent ads with their placements
    marks = ",".join("?" * len(INCUMBENT_FPS))
    ads = conn.execute(
        f"""SELECT ad_id, ad_group_id, campaign_id, fingerprint FROM rsa_ads
            WHERE account_id=? AND ad_status='ENABLED' AND fingerprint IN ({marks})""",
        [ACCOUNT_ID] + list(INCUMBENT_FPS)).fetchall()
    conn.close()

    targets = {}   # ad_group_id -> (name, campaign_id, flagged)
    pauses = []    # (ad_id, ad_group_id, name, campaign_id, variant)
    for ad_id, ag, camp, fp in ads:
        name = names.get(ag, "")
        if not name or not is_brand(name):
            continue
        flagged = any(pat in name.lower() for pat in FLAG_PATTERNS)
        targets[ag] = (name, camp, flagged)
        pauses.append((ad_id, ag, name, camp, INCUMBENT_FPS[fp]))

    stamp = datetime.date.today().isoformat()
    t_path = os.path.join(EXPORT_DIR, "bcc01_target_adgroups_%s.csv" % stamp)
    with open(t_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["ad_group_id", "ad_group_name", "campaign_id", "flagged_geico_sf"])
        for ag, (name, camp, flagged) in sorted(targets.items(), key=lambda kv: (kv[1][1], kv[1][0])):
            w.writerow([ag, name, camp, "YES" if flagged else "no"])

    p_path = os.path.join(EXPORT_DIR, "bcc01_pause_list_%s.csv" % stamp)
    with open(p_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["ad_id", "ad_group_id", "ad_group_name", "campaign_id", "variant"])
        for row in sorted(pauses, key=lambda r: (r[3], r[2], r[4])):
            w.writerow(row)

    # sanity checks
    ag_with_pause = {ag for _, ag, _, _, _ in pauses}
    empty_targets = set(targets) - ag_with_pause  # impossible by construction, kept as guard
    print("[DONE] %d target ad group(s) (%d flagged Geico/SF) -> %s"
          % (len(targets), sum(1 for v in targets.values() if v[2]), t_path))
    print("[DONE] %d ad(s) to pause (%s) -> %s"
          % (len(pauses),
             ", ".join("%s: %d" % (v, sum(1 for r in pauses if r[4] == v))
                       for v in sorted(set(r[4] for r in pauses))), p_path))
    if empty_targets:
        print("[CHECK] %d target(s) without a pause candidate -- reconcile before load: %s"
              % (len(empty_targets), sorted(empty_targets)))
    else:
        print("[CHECK] every target hosts >=1 incumbent to pause -- lists reconcile.")
    print("[NOTE]  scope check: expect V1-brand ~56 + V3-brand ~11 ads per the "
          "brand_serve_split populations, plus the flagged-group ads those "
          "readouts excluded. A materially different count = classification "
          "drift -- report back before loading.")


if __name__ == "__main__":
    main()
