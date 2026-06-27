"""
migrate_phase2_collisions.py  — resolve the 38 cross-brand collisions, then
tighten brand_registry_keywords to a global UNIQUE(normalized, match_type).
==============================================================================
Executes the confirmed Phase 2 decisions:

  BRAND MERGES (keep_id <- drop_id): reassign the dropped brand's keywords to the
  canonical (dropping any that would collide with the canonical's own), repoint
  any parent_brand references, optionally force the canonical's bid_status, then
  delete the duplicate brand row.
        174 CSAA Insurance        <- 51  CSAA
        132 Blue Cross Blue Shield<- 133 BCBS
        125 UnitedHealthcare      <- 126 UHC
        98  Matic                 <- 111 Matic Insurance
        109 Grange Insurance      <- 95  Grange Mutual
        24  Kin Insurance (->approved) <- 72 Kin
        40  Humana                <- 142 Humana Gold Plus  ('gold plus' = product modifier)

  KEYWORD FIXES (per colliding term, keep ONE brand's row, drop the rest):
        {248,74} keep 74  (drop ACE — substring trap on 21 terms)
        {98,74}  keep 74  (drop Matic — 'thematic progression' substring trap)
        {148,74} keep 148 (Globe Life)
        {180,74} keep 180 (Baja Auto Insurance)
        {181,74} keep 181 (USAgencies)
        {1,134}  DROP ENTIRELY ('kaiser permanente jd power1-800-progressive')

  THEN: verify 0 cross-brand collisions remain and add the global
  UNIQUE INDEX(normalized, match_type) — a term can map to only one brand.

SAFETY: dry-run default (pass --apply). Backs up BOTH tables first
(brand_registry / brand_registry_keywords -> *_prephase2_<date>). Verifies
expected brand names by id before touching anything; aborts if ids drifted.
Validated on synthetic before live use.

    python migrate_phase2_collisions.py            # dry-run preview
    python migrate_phase2_collisions.py --apply
"""

import argparse
import sqlite3
import sys
from datetime import date
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
DB = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")
STAMP = f"{date.today():%Y%m%d}"

# (keep_id, drop_id, force_bid_status_or_None)
MERGES = [
    (174, 51, None),
    (132, 133, None),
    (125, 126, None),
    (98, 111, None),
    (109, 95, None),
    (24, 72, "approved"),
    (40, 142, None),
]
# expected names by id — safety guard against id drift
EXPECTED = {
    174: "CSAA Insurance", 51: "CSAA",
    132: "Blue Cross Blue Shield", 133: "BCBS",
    125: "UnitedHealthcare", 126: "UHC",
    98: "Matic", 111: "Matic Insurance",
    109: "Grange Insurance", 95: "Grange Mutual",
    24: "Kin Insurance", 72: "Kin",
    40: "Humana", 142: "Humana Gold Plus",
    248: "ACE", 74: "Unattributed Negative Keyword",
    148: "Globe Life", 180: "Baja Auto Insurance",
    181: "USAgencies", 1: "Progressive", 134: "Kaiser Permanente",
}
# non-merge collision resolutions: frozenset(brand_ids) -> keeper_id (None = drop all)
RESOLUTIONS = {
    frozenset({248, 74}): 74,
    frozenset({98, 74}): 74,
    frozenset({148, 74}): 148,
    frozenset({180, 74}): 180,
    frozenset({181, 74}): 181,
    frozenset({1, 134}): None,
}


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--apply", action="store_true", help="Perform it. Else dry-run.")
    return p.parse_args()


def collisions(conn):
    out = []
    for c in conn.execute("""
        SELECT normalized, match_type
        FROM brand_registry_keywords
        GROUP BY normalized, match_type
        HAVING COUNT(DISTINCT brand_id) > 1"""):
        bids = [r["brand_id"] for r in conn.execute(
            "SELECT DISTINCT brand_id FROM brand_registry_keywords WHERE normalized=? AND match_type=?",
            (c["normalized"], c["match_type"]))]
        out.append((c["normalized"], c["match_type"], bids))
    return out


def main():
    a = parse_args()
    if not DB.exists():
        sys.exit(f"DB not found: {DB}")
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 15000")

    print("=" * 70)
    print(f"  Phase 2 collision migration{'   (DRY RUN)' if not a.apply else '   [APPLY]'}")
    print("=" * 70)

    # ── safety: verify ids map to expected names ──
    drift = []
    for bid, name in EXPECTED.items():
        row = conn.execute("SELECT brand_name FROM brand_registry WHERE id=?", (bid,)).fetchone()
        if not row or row["brand_name"] != name:
            drift.append((bid, name, row["brand_name"] if row else "(missing)"))
    if drift:
        print("  ABORT: brand-id/name drift — ids don't match the triage snapshot:")
        for bid, exp, got in drift:
            print(f"    id={bid} expected '{exp}' got '{got}'")
        sys.exit(1)
    print(f"  id/name guard: OK ({len(EXPECTED)} ids verified)")

    before = collisions(conn)
    print(f"  collisions before: {len(before)} (term,match_type) groups")

    if not a.apply:
        print("\n  DRY RUN — planned actions:")
        for keep, drop, force in MERGES:
            kn = EXPECTED[keep]; dn = EXPECTED[drop]
            dropc = conn.execute("SELECT COUNT(*) c FROM brand_registry_keywords WHERE brand_id=?", (drop,)).fetchone()["c"]
            clash = conn.execute("""SELECT COUNT(*) c FROM brand_registry_keywords d
                WHERE d.brand_id=? AND EXISTS (SELECT 1 FROM brand_registry_keywords k
                WHERE k.brand_id=? AND k.normalized=d.normalized AND k.match_type=d.match_type)""",
                (drop, keep)).fetchone()["c"]
            fs = f", force bid_status={force}" if force else ""
            print(f"    MERGE {dn}->{kn}: reassign {dropc-clash} kw, drop {clash} colliding, delete brand{fs}")
        for norm, mt, bids in before:
            key = frozenset(bids)
            if key in RESOLUTIONS:
                keeper = RESOLUTIONS[key]
                act = "DROP ALL" if keeper is None else f"keep {EXPECTED.get(keeper, keeper)}"
                print(f"    KW   '{norm}'[{mt or 'blank'}]: {act}")
        print("\n  Re-run with --apply.")
        conn.close()
        return

    # ── APPLY ──
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("ROLLBACK")
    except sqlite3.OperationalError as e:
        sys.exit(f"  [LOCKED] {e}")
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.execute("PRAGMA foreign_keys = OFF")

    try:
        conn.execute("BEGIN")
        # backups
        for t in ("brand_registry", "brand_registry_keywords"):
            conn.execute(f"DROP TABLE IF EXISTS {t}_prephase2_{STAMP}")
            conn.execute(f"CREATE TABLE {t}_prephase2_{STAMP} AS SELECT * FROM {t}")

        # 1. merges
        for keep, drop, force in MERGES:
            kn, dn = EXPECTED[keep], EXPECTED[drop]
            # drop the dup's keyword rows that would collide on the canonical
            conn.execute("""DELETE FROM brand_registry_keywords
                WHERE brand_id=? AND EXISTS (SELECT 1 FROM brand_registry_keywords k
                    WHERE k.brand_id=? AND k.normalized=brand_registry_keywords.normalized
                      AND k.match_type=brand_registry_keywords.match_type)""", (drop, keep))
            # reassign the rest to canonical
            conn.execute("UPDATE brand_registry_keywords SET brand_id=? WHERE brand_id=?", (keep, drop))
            # repoint any parent_brand references from the dropped name to the canonical name
            conn.execute("UPDATE brand_registry SET parent_brand=? WHERE parent_brand=?", (kn, dn))
            # optional bid_status force on canonical
            if force:
                conn.execute("UPDATE brand_registry SET bid_status=? WHERE id=?", (force, keep))
            # delete the duplicate brand row
            conn.execute("DELETE FROM brand_registry WHERE id=?", (drop,))

        # 2. keyword fixes on remaining collisions
        for norm, mt, bids in collisions(conn):
            key = frozenset(bids)
            if key not in RESOLUTIONS:
                conn.execute("ROLLBACK")
                sys.exit(f"  ABORT: unhandled collision {norm!r}[{mt}] brands={bids}. No changes committed.")
            keeper = RESOLUTIONS[key]
            if keeper is None:
                conn.execute("DELETE FROM brand_registry_keywords WHERE normalized=? AND match_type=?", (norm, mt))
            else:
                conn.execute("DELETE FROM brand_registry_keywords WHERE normalized=? AND match_type=? AND brand_id<>?",
                             (norm, mt, keeper))

        # 3. verify zero collisions
        left = collisions(conn)
        if left:
            conn.execute("ROLLBACK")
            sys.exit(f"  ABORT: {len(left)} collisions remain after resolution. No changes committed.")

        # 4. tighten to global UNIQUE(normalized, match_type)
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_brk_norm_mt_unique "
                     "ON brand_registry_keywords(normalized, match_type)")

        # integrity: no LIVE-table keyword row may reference a missing brand.
        # (A global PRAGMA foreign_key_check would false-positive on backup tables
        #  that retain their FK and point at the brands we just deleted.)
        orphans = conn.execute("""
            SELECT COUNT(*) c FROM brand_registry_keywords k
            LEFT JOIN brand_registry b ON b.id = k.brand_id
            WHERE b.id IS NULL""").fetchone()["c"]
        if orphans:
            conn.execute("ROLLBACK")
            sys.exit(f"  ABORT: {orphans} orphaned keyword rows in live table. No changes committed.")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.execute("PRAGMA foreign_keys = ON")

    brands = conn.execute("SELECT COUNT(*) c FROM brand_registry").fetchone()["c"]
    kw = conn.execute("SELECT COUNT(*) c FROM brand_registry_keywords").fetchone()["c"]
    print(f"\n  MIGRATION COMPLETE")
    print(f"    brand_registry: {brands} brands (7 duplicates removed)")
    print(f"    brand_registry_keywords: {kw} rows")
    print(f"    collisions remaining: 0")
    print(f"    global UNIQUE(normalized, match_type) enforced")
    print(f"    backups: brand_registry_prephase2_{STAMP}, brand_registry_keywords_prephase2_{STAMP}")
    conn.close()


if __name__ == "__main__":
    main()
