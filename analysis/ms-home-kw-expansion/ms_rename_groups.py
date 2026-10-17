"""
ms_rename_groups.py -- generates the Editor import CSVs for the ad-group naming
standardization (step 1 of the runbook). READ-ONLY against paid_search.db.

Emits TWO files, imported and posted IN ORDER (retire first frees the canonical
names before the actives claim them -- never combine into one import):

  01_retire_<date>.csv        dead namesakes + legacy Quote groups in the SIX LIVE
                              campaigns -> "zzRETIRE-" prefix. Scope: any group
                              (any status) whose name collides with an upcoming
                              target name, plus Home-Generic-Quote / -Quote-NA --
                              EXCLUDING each campaign's designated active core.
  02_canonicalize_<date>.csv  the six designated active cores (Gilbert's mapping)
                              -> "Home-Generic-Home Insurance-Core".

Renames are identified by ad-group Id, so Editor updates the entity in place
(same id, same history). Rows carry Type / Id / Campaign / Ad Group (new name);
Status is deliberately omitted so nothing but the name changes.

SANITY RITUAL: after importing each file into Editor, spot-check 2-3 rows show as
EDITS to existing groups (not additions) before posting -- if Editor shows them
as new ad groups, the Id column didn't match and STOP.

    python ms_rename_groups.py            # prints the plan + writes both CSVs
"""
import csv
import os
import sqlite3
import sys
from datetime import date

sys.stdout.reconfigure(encoding="utf-8")
ACCT = 2
CANON_CORE = "Home-Generic-Home Insurance-Core"
RETIRE_PREFIX = "zzRETIRE-"

# campaign token -> current name of the DESIGNATED ACTIVE core (Gilbert's mapping)
ACTIVE_CORE = {
    "Tier1":      "Home-Generic-Home Insurance-Cheapest-NA",
    "Tier2":      "Home-Generic-Home Insurance-Core-NA",
    "Tier3":      "Home-Generic-Home Insurance-Core-Up",
    "Tier5":      "Home-Generic-Insurance-Cheapest-NA",
    "CALIFORNIA": "Home-Generic-Home Insurance-Cheapest",
    "FLORIDA":    "Home-Generic-Home Insurance-Core-NA",
}
# names the expansion will create/target -- any existing group carrying one of
# these (or the legacy Quote pair) in a live campaign collides and gets retired
TARGET_NAMES = {
    CANON_CORE, "Home-Generic-Quote", "Home-Generic-Quote-NA",
    "Home-Generic-Compare", "Home-Generic-Cost-Core", "Home-Generic-Companies",
    "Home-Generic-Other", "Home-Generic-Home Auto Bundle-Core",
    "Home-Generic-Home Auto Bundle-Quote",
}


def main():
    db = sys.argv[1] if len(sys.argv) > 1 else r"C:\Projects\google-ads-ops\db\paid_search.db"
    outdir = r"C:\Projects\google-ads-ops\analysis\ms-home-kw-expansion\output"
    conn = sqlite3.connect("file:%s?mode=ro" % db, uri=True)
    conn.row_factory = sqlite3.Row

    camps = conn.execute(
        "SELECT campaign_id, campaign_name FROM campaigns "
        "WHERE account_id=? AND status='ENABLED'", (ACCT,)).fetchall()
    tok_of = {}
    for c in camps:
        for tok in ACTIVE_CORE:
            if tok.upper() in (c["campaign_name"] or "").upper():
                tok_of[c["campaign_id"]] = (tok, c["campaign_name"])
    if len(tok_of) != len(ACTIVE_CORE):
        sys.exit("ABORT: matched %d of %d live campaigns to the mapping -- check tokens.\n"
                 "Live: %s" % (len(tok_of), len(ACTIVE_CORE),
                               [c["campaign_name"] for c in camps]))

    retire, canon, warnings = [], [], []
    for cid, (tok, cname) in sorted(tok_of.items(), key=lambda kv: kv[1][0]):
        groups = conn.execute(
            "SELECT ad_group_id, ad_group_name, status FROM ad_groups "
            "WHERE account_id=? AND campaign_id=?", (ACCT, cid)).fetchall()
        by_name = {g["ad_group_name"]: g for g in groups}
        des_name = ACTIVE_CORE[tok]
        des = by_name.get(des_name)
        if des is None:
            sys.exit("ABORT: designated active core '%s' not found in %s.\n"
                     "Groups present: %s" % (des_name, cname,
                                             sorted(by_name)[:40]))
        if des["status"] != "ENABLED":
            warnings.append("designated core '%s' in %s has status=%s (expected ENABLED)"
                            % (des_name, tok, des["status"]))
        for g in groups:
            if g["ad_group_id"] == des["ad_group_id"]:
                continue
            if g["ad_group_name"] in TARGET_NAMES:
                if g["status"] == "ENABLED":
                    warnings.append("retiring an ENABLED group: '%s' in %s -- confirm intended"
                                    % (g["ad_group_name"], tok))
                retire.append({"Type": "Ad Group", "Id": g["ad_group_id"],
                               "Campaign": cname,
                               "Ad Group": RETIRE_PREFIX + g["ad_group_name"],
                               "_was": g["ad_group_name"], "_tok": tok,
                               "_status": g["status"]})
        canon.append({"Type": "Ad Group", "Id": des["ad_group_id"], "Campaign": cname,
                      "Ad Group": CANON_CORE, "_was": des_name, "_tok": tok,
                      "_status": des["status"]})
    conn.close()

    print("PLAN -- file 01 (retire, import+post FIRST): %d renames" % len(retire))
    for r in retire:
        print("  %-10s %-42s -> %s   [%s]" % (r["_tok"], r["_was"], r["Ad Group"], r["_status"]))
    print("\nPLAN -- file 02 (canonicalize, import+post SECOND): %d renames" % len(canon))
    for r in canon:
        print("  %-10s %-42s -> %s   [%s]" % (r["_tok"], r["_was"], r["Ad Group"], r["_status"]))
    if warnings:
        print("\nWARNINGS:")
        for w in warnings:
            print("  ! %s" % w)

    os.makedirs(outdir, exist_ok=True)
    cols = ["Type", "Id", "Campaign", "Ad Group"]
    for name, rows in (("01_retire", retire), ("02_canonicalize", canon)):
        path = os.path.join(outdir, "ms_rename_%s_%s.csv" % (name, date.today().isoformat()))
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
            w.writeheader(); w.writerows(rows)
        print("\nwrote %s (%d rows)" % (path, len(rows)))
    print("\nSEQUENCE: import 01 -> verify rows show as EDITS (not additions) -> post -> "
          "import 02 -> verify -> post. Then: fresh structure export -> 04 loader -> "
          "verification SQL -> regenerate the keyword import.")
    print("Record the old->new mapping above in the restr-2026-09 change-log entry.")


if __name__ == "__main__":
    main()
