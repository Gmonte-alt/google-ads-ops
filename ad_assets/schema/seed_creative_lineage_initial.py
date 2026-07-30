#!/usr/bin/env python
"""
seed_creative_lineage_initial.py -- ad_assets/schema/ -- the gate-approved
initial lineage seeding: BCC-01's three parent edges + the June family
(V11-V30, intent_fork from the V1 backbone per the V11 workbook), NOTHING
DEEPER -- pre-June parentage is not reliably reconstructable, and guessed
lineage is worse than absent lineage.

June edges are auto-derived from the DB (labels V11..V30 -> their
fingerprints; parent = V1's fingerprint), so nothing is hand-typed. Their
change_summary is generic-but-true; the strategy thread can enrich
individual summaries later with plain UPDATEs.

Idempotent (INSERT OR REPLACE on the PK). Dry-run default.

  python seed_creative_lineage_initial.py --db "...\\paid_search.db"
  python seed_creative_lineage_initial.py --db "...\\paid_search.db" --apply
"""
import argparse
import os
import re
import sqlite3
import sys

ACCOUNT_ID = 1
BCC01_FP = "e1fd7d1595"
BCC01_RECORD = "decisions/creative_decision_record_BCC-01.md"
JUNE_RECORD = "rsa-load-ready-copy_v11.xlsx (workbook source)"

BCC01_EDGES = [  # (parent_label, change_type, summary)
    ("V1", "synthesis",
     "serve-survivor core lines (zip action, $98 LOCATION, 2026 comparison) from V1"),
    ("V3", "synthesis",
     "brand-group lines (Prices In 2026, Instant Home Quote, LOCATION Best Rates) from V3"),
    ("V12", "compliance_rewrite",
     "location-insertion relevance pattern + geo/recency lines from V12; "
     "replaces DKI as the dynamic element"),
]


def label_fp(conn, label):
    fps = [r[0] for r in conn.execute(
        "SELECT DISTINCT fingerprint FROM rsa_ads WHERE account_id=? AND variant_label=?",
        (ACCOUNT_ID, label))]
    if len(fps) != 1:
        sys.exit("[ABORT] label %s maps to %d fingerprints -- run "
                 "verify_variant_labels.py first" % (label, len(fps)))
    return fps[0]


def main():
    p = argparse.ArgumentParser(description="Seed initial creative lineage (dry-run default).")
    p.add_argument("--db", required=True)
    p.add_argument("--apply", action="store_true")
    args = p.parse_args()
    if not os.path.isfile(args.db):
        sys.exit("[ABORT] database not found: %s" % args.db)
    conn = sqlite3.connect(args.db)

    edges = []  # (fingerprint, parent_fp, change_type, summary, authored_date, record)

    # BCC-01: three pre-registered edges
    for parent_label, ct, summary in BCC01_EDGES:
        edges.append((BCC01_FP, label_fp(conn, parent_label), ct, summary,
                      "2026-07-23", BCC01_RECORD))

    # June family: V11..V30 -> intent_fork from V1's backbone
    v1_fp = label_fp(conn, "V1")
    june = [(lbl, fp) for lbl, fp in conn.execute(
        """SELECT DISTINCT variant_label, fingerprint FROM rsa_ads
           WHERE account_id=? AND variant_label IS NOT NULL""", (ACCOUNT_ID,))
        if re.fullmatch(r"V(1[1-9]|2[0-9]|30)", lbl or "")]
    for lbl, fp in sorted(june):
        edges.append((fp, v1_fp, "intent_fork",
                      "June 2026 intent-test fork (%s) from incumbent backbone "
                      "per V11 workbook tier design" % lbl,
                      "2026-06-03", JUNE_RECORD))

    print("[SEED] %d edge(s) staged: 3 BCC-01 + %d June family" % (len(edges), len(edges) - 3))
    for fp, pf, ct, summary, dt, rec in edges:
        print("  %s <- %s  %-18s %s" % (fp, pf, ct, summary[:52]))

    if not args.apply:
        conn.close()
        print("\n[DRY RUN] nothing written. Re-run with --apply.")
        return

    conn.executemany(
        """INSERT OR REPLACE INTO creative_lineage
           (account_id, fingerprint, parent_fingerprint, change_type,
            change_summary, authored_date, decision_record_path, created_at)
           VALUES (?,?,?,?,?,?,?,datetime('now'))""",
        [(ACCOUNT_ID, fp, pf, ct, s, dt, rec) for fp, pf, ct, s, dt, rec in edges])
    conn.commit()
    n = conn.execute("SELECT COUNT(*) FROM creative_lineage WHERE account_id=?",
                     (ACCOUNT_ID,)).fetchone()[0]
    conn.close()
    print("\n[APPLY] done -- creative_lineage holds %d edge(s)." % n)


if __name__ == "__main__":
    main()
