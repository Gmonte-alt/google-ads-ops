#!/usr/bin/env python
"""
reconcile_fingerprint.py -- ad_assets/analysis/ -- THE ACCEPTANCE TEST for
the creative lineage system (architect ruling 2026-07-23): after an authored
creative loads and the weekly sync runs, the SYNCED fingerprint must equal
the PRE-REGISTERED one. Equality proves the canonical recipe is stable
end-to-end (authoring-side computation = sync-side computation). Divergence
means the recipe drifted or a headline was edited between record and load --
and every pre-load lineage edge is suspect until explained.

Checks (PASS/FAIL, exit 1 on failure):
  F1  the expected fingerprint exists in rsa_ads (post-sync)
  F2  its ads carry the expected headline count (default 15)
  F3  label application: sets variant_label on the fingerprint's ads
      (with --apply), or verifies it if already set
Drift diagnostics on F1 failure: prints unlabeled fingerprints that appeared
since the given date with their headline counts -- the edited-creative
candidates.

  python reconcile_fingerprint.py --db "...\\paid_search.db" --expect e1fd7d1595 --label BCC-01
  python reconcile_fingerprint.py --db "...\\paid_search.db" --expect e1fd7d1595 --label BCC-01 --apply
"""
import argparse
import os
import sqlite3
import sys

ACCOUNT_ID = 1


def main():
    p = argparse.ArgumentParser(description="Post-load fingerprint reconciliation (acceptance test).")
    p.add_argument("--db", required=True)
    p.add_argument("--expect", required=True, help="Pre-registered fingerprint (e.g. e1fd7d1595)")
    p.add_argument("--label", required=True, help="Label to apply/verify (e.g. BCC-01)")
    p.add_argument("--headlines", type=int, default=15,
               help="expected headline count (15 = BCC-01 default)")
    p.add_argument("--since", default="2026-07-23",
                   help="For drift diagnostics: new-fingerprint search window start")
    p.add_argument("--apply", action="store_true", help="Write the label (default: verify only)")
    args = p.parse_args()

    if not os.path.isfile(args.db):
        sys.exit("[ABORT] database not found: %s" % args.db)
    conn = sqlite3.connect(args.db)
    fail = False

    ads = conn.execute(
        """SELECT ad_id, variant_label, ad_status FROM rsa_ads
           WHERE account_id=? AND fingerprint=?""",
        (ACCOUNT_ID, args.expect)).fetchall()

    if not ads:
        fail = True
        print("[F1] FAIL -- fingerprint %s not found in rsa_ads. Either the load "
              "hasn't synced yet, or THE RECIPE DRIFTED." % args.expect)
        cands = conn.execute(
            """SELECT r.fingerprint, COUNT(DISTINCT r.ad_id),
                      (SELECT COUNT(*) FROM rsa_ad_assets c
                       WHERE c.account_id=r.account_id AND c.ad_id=MIN(r.ad_id)
                         AND c.field_type='HEADLINE' AND c.removed_date IS NULL)
               FROM rsa_ads r
               WHERE r.account_id=? AND r.variant_label IS NULL
                 AND r.first_seen_date >= ?
               GROUP BY r.fingerprint""",
            (ACCOUNT_ID, args.since)).fetchall()
        if cands:
            print("     drift candidates (unlabeled fingerprints first seen since %s):" % args.since)
            for fp, n_ads, n_hl in cands:
                print("       %s  (%d ad(s), %s headlines)%s"
                      % (fp, n_ads, n_hl,
                         "  <-- headline count matches expectation" if n_hl == args.headlines else ""))
            print("     if one of these is the loaded creative, a headline changed between "
                  "record and load: diff it (variant_inspector.py --variant <fp> ) and "
                  "either fix the load or re-register the record + lineage edges.")
        else:
            print("     no new unlabeled fingerprints since %s -- likely the sync simply "
                  "hasn't run since the load." % args.since)
    else:
        print("[F1] PASS -- %s present: %d ad(s) (%d enabled)"
              % (args.expect, len(ads), sum(1 for _, _, s in ads if s == "ENABLED")))

        n_hl = conn.execute(
            """SELECT COUNT(*) FROM rsa_ad_assets
               WHERE account_id=? AND ad_id=? AND field_type='HEADLINE'
                 AND removed_date IS NULL""",
            (ACCOUNT_ID, ads[0][0])).fetchone()[0]
        if n_hl == args.headlines:
            print("[F2] PASS -- %d headlines as recorded" % n_hl)
        else:
            fail = True
            print("[F2] FAIL -- %d headlines, record says %d" % (n_hl, args.headlines))

        labels = {lbl for _, lbl, _ in ads if lbl}
        if labels == {args.label}:
            print("[F3] PASS -- label %s already applied" % args.label)
        elif labels:
            fail = True
            print("[F3] FAIL -- fingerprint carries other label(s): %s" % labels)
        elif args.apply:
            conn.execute(
                "UPDATE rsa_ads SET variant_label=? WHERE account_id=? AND fingerprint=?",
                (args.label, ACCOUNT_ID, args.expect))
            conn.commit()
            print("[F3] PASS -- label %s applied to %d ad(s)" % (args.label, len(ads)))
        else:
            print("[F3] PENDING -- unlabeled; re-run with --apply to set %s" % args.label)

    conn.close()
    if fail:
        sys.exit("\n[RESULT] RECONCILIATION FAILED -- do not trust pre-load lineage "
                 "edges for this creative until explained.")
    print("\n[RESULT] reconciliation %s -- recipe stable end-to-end."
          % ("passed" if ads else "pending (awaiting first post-load sync)"))


if __name__ == "__main__":
    main()
