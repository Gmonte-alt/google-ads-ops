"""
diag_check_match_type_labels.py
================================
Checks whether keyword_instances.match_type labels are correct by
cross-referencing criterion_id against known BROAD and EXACT criterion IDs
for the fire insurance keyword in the Home Insurance account.

From the diagnostic data:
  criterion_id = 19000971  -> EXACT match keyword
  criterion_id = 96324694  -> BROAD match keyword

If any instance has a match_type that contradicts its criterion_id,
the label was written incorrectly by the keyword structure sync and
needs to be corrected.

Also checks keyword_identities to see if the mismatch originates there.

RUN
───
    conda activate paid-search
    python analysis/diag_check_match_type_labels.py
"""

import sqlite3
from pathlib import Path

DB_PATH = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")

# Known criterion_id → expected match type mapping
# Derived from cross-referencing DB data with Google Ads UI screenshot
KNOWN_CRITERION_MATCH = {
    "19000971": "EXACT",
    "96324694": "BROAD",
}

# The five instances in the current 7 enabled NB-SEARCH campaigns
TARGET_IIDS = [420369, 420370, 420740, 420741, 53512]


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def main():
    conn = get_conn()

    print("=" * 72)
    print("diag_check_match_type_labels.py")
    print(f"  Database: {DB_PATH}")
    print("=" * 72)

    # ── 1. Check the five NB-SEARCH fire insurance instances ──────────────
    print("\n── 1. match_type labels on NB-SEARCH fire insurance instances ────────\n")
    print(f"  {'iid':>8}  {'criterion_id':>14}  {'ki match':>10}  "
          f"{'expected':>10}  {'status':>10}  {'campaign':>40}")
    print(f"  {'─'*8}  {'─'*14}  {'─'*10}  {'─'*10}  {'─'*10}  {'─'*40}")

    mismatches = []
    for iid in TARGET_IIDS:
        r = conn.execute("""
            SELECT ki.id, ki.match_type, ki.criterion_id,
                   ki.campaign_name, ki.ad_group_name, ki.status,
                   ki_id.match_type AS identity_match_type,
                   ki_id.normalized_text
            FROM keyword_instances ki
            JOIN keyword_identities ki_id ON ki_id.id = ki.identity_id
            WHERE ki.id = ?
        """, (iid,)).fetchone()

        if not r:
            print(f"  {iid:>8}  NOT FOUND")
            continue

        crit = str(r['criterion_id'])
        expected = KNOWN_CRITERION_MATCH.get(crit, "UNKNOWN")
        actual   = r['match_type']
        ok       = actual == expected
        status   = "✓ OK" if ok else "✗ WRONG"
        camp     = str(r['campaign_name'] or '').replace("HOME INSURANCE-","").replace("-NB-SEARCH-ALL-USA-STE","")

        print(f"  {iid:>8}  {crit:>14}  {actual:>10}  {expected:>10}  {status:>10}  {camp:>40}")

        if not ok:
            mismatches.append({
                'iid': iid,
                'criterion_id': crit,
                'actual_match': actual,
                'expected_match': expected,
                'identity_match': r['identity_match_type'],
                'campaign': camp,
                'ad_group': r['ad_group_name'],
            })

    # ── 2. Check keyword_identities for the same criterion_ids ────────────
    print("\n── 2. keyword_identities linked to these instances ──────────────────\n")
    print(f"  {'iid':>8}  {'identity_id':>12}  {'normalized_text':>30}  "
          f"{'ki_id.match_type':>18}  {'ki.match_type':>14}")
    print(f"  {'─'*8}  {'─'*12}  {'─'*30}  {'─'*18}  {'─'*14}")

    for iid in TARGET_IIDS:
        r = conn.execute("""
            SELECT ki.id, ki.identity_id, ki.match_type AS ki_match,
                   ki_id.normalized_text, ki_id.match_type AS kid_match
            FROM keyword_instances ki
            JOIN keyword_identities ki_id ON ki_id.id = ki.identity_id
            WHERE ki.id = ?
        """, (iid,)).fetchone()
        if r:
            print(f"  {r['id']:>8}  {r['identity_id']:>12}  "
                  f"{str(r['normalized_text']):>30}  "
                  f"{str(r['kid_match']):>18}  "
                  f"{str(r['ki_match']):>14}")

    # ── 3. Verdict ─────────────────────────────────────────────────────────
    print("\n── 3. VERDICT ───────────────────────────────────────────────────────\n")

    if not mismatches:
        print("  ✓ All match_type labels are correct.")
        print("    The performance attribution issue is purely an API reporting behavior.")
    else:
        print(f"  ✗ {len(mismatches)} instance(s) have incorrect match_type labels:\n")
        for m in mismatches:
            print(f"    iid={m['iid']}  criterion_id={m['criterion_id']}")
            print(f"      keyword_instances.match_type = '{m['actual_match']}' (WRONG)")
            print(f"      Expected based on criterion_id: '{m['expected_match']}'")
            print(f"      keyword_identities.match_type = '{m['identity_match']}'")
            print(f"      Campaign: {m['campaign']}")
            print(f"      Ad Group: {m['ad_group']}")
            print()

        print("  These rows need to be corrected in keyword_instances.")
        print("  The mismatch originates in 02_sync_keyword_structure.py —")
        print("  match_type was written from keyword_identities without")
        print("  verifying against the API's criterion-level match type.")
        print()
        print("  To generate the correction SQL, run:")
        print("  python analysis/diag_check_match_type_labels.py --fix-sql")

    # ── 4. Optional: print correction SQL ─────────────────────────────────
    import sys
    if "--fix-sql" in sys.argv and mismatches:
        print("\n── 4. CORRECTION SQL (review before running) ────────────────────────\n")
        print("  -- Run in DB Browser for SQLite or via sqlite3 CLI")
        print("  -- Verify instance IDs match your account before executing\n")
        for m in mismatches:
            print(f"  UPDATE keyword_instances")
            print(f"    SET match_type = '{m['expected_match']}'")
            print(f"    WHERE id = {m['iid']};")
            print()

        print("  -- After updating, also update keyword_identities if the identity")
        print("  -- match_type is also wrong (check section 2 above).")

    conn.close()
    print("Done.")


if __name__ == "__main__":
    main()
