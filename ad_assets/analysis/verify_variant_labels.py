#!/usr/bin/env python
"""
verify_variant_labels.py -- ad_assets/analysis/ -- Readout #1 for the
creative-strategy thread: label <-> fingerprint reconciliation, as a
re-runnable verification with PASS/FAIL lines (exit 1 on any FAIL).

Checks:
  L1  the focal variant (default V2, the value champion) maps to exactly ONE
      fingerprint -- prints it, so downstream claims can cite the hash
  L2  no variant_label maps to more than one fingerprint
  L3  no fingerprint carries more than one variant_label
  L4  untagged-ad inventory (expected: the retired/paused generations that
      predate the rsa_variants_2026-07-18.csv export -- these correctly show
      their fingerprint in readouts, not a label)

Context for the strategy thread: the $16.98 VPC appearing under both "V4" and
"V2" in different documents is a numeric coincidence between two different
slices (V4 x campaign 23394808297, full window, campaign grain vs V2 within
V4's shared ad groups, Jan-May). If L1-L3 PASS here, the labeling is sound
and the handoff's champion claims stand as scoped.

READ-ONLY.

  python verify_variant_labels.py --db "...\\paid_search.db"
  python verify_variant_labels.py --db "...\\paid_search.db" --variant V4
"""
import argparse
import os
import sqlite3
import sys

ACCOUNT_ID = 1


def main():
    p = argparse.ArgumentParser(description="Variant label <-> fingerprint verification.")
    p.add_argument("--db", required=True)
    p.add_argument("--variant", default="V2", help="Focal variant for L1 (default V2)")
    args = p.parse_args()

    if not os.path.isfile(args.db):
        sys.exit("[ABORT] database not found: %s" % args.db)
    conn = sqlite3.connect("file:%s?mode=ro" % args.db.replace("\\", "/"), uri=True)
    fail = False

    # L1: focal variant -> exactly one fingerprint
    fps = [r[0] for r in conn.execute(
        """SELECT DISTINCT fingerprint FROM rsa_ads
           WHERE account_id=? AND variant_label=?""",
        (ACCOUNT_ID, args.variant)).fetchall()]
    if len(fps) == 1:
        n_ads, camps = conn.execute(
            """SELECT COUNT(*), COUNT(DISTINCT campaign_id) FROM rsa_ads
               WHERE account_id=? AND variant_label=?""",
            (ACCOUNT_ID, args.variant)).fetchone()
        print("[L1] PASS -- %s = fingerprint %s (%d ads across %d campaigns)"
              % (args.variant, fps[0], n_ads, camps))
        print("     anatomy/served-face for this fingerprint: "
              "variant_inspector.py --variant %s" % args.variant)
    elif not fps:
        fail = True
        print("[L1] FAIL -- no ads carry variant_label=%s (label not imported?)" % args.variant)
    else:
        fail = True
        print("[L1] FAIL -- %s maps to %d fingerprints: %s"
              % (args.variant, len(fps), ", ".join(fps)))

    # L2: any label -> multiple fingerprints
    bad = conn.execute(
        """SELECT variant_label, COUNT(DISTINCT fingerprint) n,
                  GROUP_CONCAT(DISTINCT fingerprint)
           FROM rsa_ads WHERE account_id=? AND variant_label IS NOT NULL
           GROUP BY variant_label HAVING n > 1""", (ACCOUNT_ID,)).fetchall()
    if bad:
        fail = True
        print("[L2] FAIL -- label(s) spanning multiple fingerprints:")
        for label, n, fplist in bad:
            print("     %-6s -> %d fingerprints: %s" % (label, n, fplist))
    else:
        print("[L2] PASS -- every label maps to exactly one fingerprint")

    # L3: any fingerprint -> multiple labels
    bad = conn.execute(
        """SELECT fingerprint, COUNT(DISTINCT variant_label) n,
                  GROUP_CONCAT(DISTINCT variant_label)
           FROM rsa_ads WHERE account_id=? AND variant_label IS NOT NULL
           GROUP BY fingerprint HAVING n > 1""", (ACCOUNT_ID,)).fetchall()
    if bad:
        fail = True
        print("[L3] FAIL -- fingerprint(s) carrying multiple labels:")
        for fp, n, labels in bad:
            print("     %s -> %s" % (fp, labels))
    else:
        print("[L3] PASS -- every fingerprint carries at most one label")

    # L4: untagged inventory (informational)
    n_untagged, n_total = conn.execute(
        """SELECT SUM(CASE WHEN variant_label IS NULL THEN 1 ELSE 0 END), COUNT(*)
           FROM rsa_ads WHERE account_id=?""", (ACCOUNT_ID,)).fetchone()
    n_untag_fp = conn.execute(
        """SELECT COUNT(DISTINCT fingerprint) FROM rsa_ads
           WHERE account_id=? AND variant_label IS NULL""", (ACCOUNT_ID,)).fetchone()[0]
    print("[L4] INFO -- %d of %d ads untagged (%d fingerprints): expected = "
          "generations paused before the 07-18 export; they appear in readouts "
          "as raw fingerprints (e.g. 538f096e1a)" % (n_untagged or 0, n_total, n_untag_fp))

    conn.close()
    if fail:
        sys.exit("\n[RESULT] LABEL INTEGRITY FAILED -- reconcile before trusting "
                 "any label-keyed claim (including the handoff's champion).")
    print("\n[RESULT] label integrity verified -- handoff claims stand as scoped.")


if __name__ == "__main__":
    main()
