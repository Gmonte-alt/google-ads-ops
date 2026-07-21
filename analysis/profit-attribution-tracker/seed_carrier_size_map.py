#!/usr/bin/env python3
"""
seed_carrier_size_map.py
========================
Profit Attribution Framework v1.2 -- creates and seeds `carrier_size_map`,
the standalone SCD2 home for the carrier-size axis (framework section 4.2,
approved 2026-07-14; DB architect decision section 8 Q4: standalone table,
NOT a brand_registry column, because 07_seed_brand_registry.py re-seeds with
INSERT OR REPLACE and would clobber a manual column).

WHAT IT DOES
------------
1. Tripwire: refuses to run against a suspiciously small DB (wrong-file guard).
2. Creates `carrier_size_map` if absent (schema below, per DB architect).
3. Resolves each of the 23 approved brand names to its `normalized_name`
   via brand_registry -- FAILS LOUDLY on any that don't resolve (no guessing
   at normalization).
4. Idempotent append-only seed: inserts a current row (end_date NULL) per
   carrier unless one already exists. Existing current rows are NEVER
   modified; a conflicting current assignment is reported, not overwritten
   (re-assignment is a deliberate separate act, not a seed side effect).
5. DRY-RUN BY DEFAULT: prints the full plan. Writes only with --apply.

SCHEMA (DB architect, 2026-07-14)
---------------------------------
    CREATE TABLE IF NOT EXISTS carrier_size_map (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        normalized_name TEXT NOT NULL,
        carrier_size    TEXT NOT NULL CHECK
                        (carrier_size IN ('large','small','membership_affinity')),
        effective_date  TEXT NOT NULL,
        end_date        TEXT,
        note            TEXT,
        UNIQUE(normalized_name, effective_date)
    );

USAGE
-----
    python seed_carrier_size_map.py --db "C:\\Projects\\google-ads-ops\\db\\paid_search.db"           # dry run
    python seed_carrier_size_map.py --db "C:\\...\\paid_search.db" --apply   # write
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import sqlite3
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

MIN_DB_BYTES = 1_000_000_000   # ~1 GB tripwire; paid_search.db is ~9.8 GB

# Approved mapping -- framework v1.1 section 4.2, approved 2026-07-14.
# Keys are brand_registry.brand_name values; resolved to normalized_name at
# runtime from the registry itself.
APPROVED = {
    # membership / affinity -- AAA-branded demand in one bucket
    "Costco": "membership_affinity",
    "AAA": "membership_affinity",
    "CSAA Insurance": "membership_affinity",   # AAA affiliate
    # true DTC nationals
    "Farmers": "large",
    "Hartford": "large",
    "Geico": "large",
    "Travelers": "large",
    "American Family": "large",
    "State Farm": "large",
    "Amica": "large",
    "21st Century": "large",                   # Farmers brand
    # regional / specialist / insurtech
    "Kin Insurance": "small",
    "Lemonade": "small",
    "Bamboo": "small",
    "Universal Property": "small",
    "Mercury": "small",
    "Wawanesa": "small",
    "Hochheim Prairie": "small",
    "Allied Trust": "small",
    "Homeowners of America": "small",
    "Allied": "small",
    "Plymouth Rock": "small",
    "American Home": "small",
}

DDL = """
CREATE TABLE IF NOT EXISTS carrier_size_map (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    normalized_name TEXT NOT NULL,
    carrier_size    TEXT NOT NULL CHECK
                    (carrier_size IN ('large','small','membership_affinity')),
    effective_date  TEXT NOT NULL,
    end_date        TEXT,
    note            TEXT,
    UNIQUE(normalized_name, effective_date)
);
"""

SEED_NOTE = "seed: framework v1.1 4.2 approved 2026-07-14"


def fail(msg: str, code: int = 1) -> None:
    print("\n[FAIL] " + msg)
    sys.exit(code)


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Create + seed carrier_size_map (dry-run by default)")
    ap.add_argument("--db", required=True, help="Path to paid_search.db")
    ap.add_argument("--apply", action="store_true",
                    help="Actually write (default is dry-run)")
    ap.add_argument("--effective-date", default=None,
                    help="YYYY-MM-DD for seed rows (default: today)")
    ap.add_argument("--force-small-db", action="store_true",
                    help="Bypass the small-DB tripwire (testing only)")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        fail(f"DB not found at: {args.db}")
    size = os.path.getsize(args.db)
    if size < MIN_DB_BYTES and not args.force_small_db:
        fail(f"DB is only {size/1e9:.2f} GB (< {MIN_DB_BYTES/1e9:.0f} GB "
             f"tripwire) -- is this the right file? Use --force-small-db "
             f"only if you are sure.")

    eff = args.effective_date or dt.date.today().isoformat()
    mode = "APPLY" if args.apply else "DRY-RUN (no writes; use --apply)"
    print("=" * 74)
    print(f"seed_carrier_size_map | mode: {mode} | effective_date: {eff}")
    print(f"db: {args.db} ({size/1e9:.2f} GB)")
    print("=" * 74)

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    # -- Resolve approved brand_names -> normalized_name via the registry ---
    reg = {row["brand_name"]: row["normalized_name"] for row in conn.execute(
        "SELECT brand_name, normalized_name FROM brand_registry")}
    unresolved = [b for b in APPROVED if b not in reg]
    if unresolved:
        # help the operator see near-misses before failing
        print("[FAIL-DETAIL] brand_name values in brand_registry that "
              "resemble the unresolved names:")
        for b in unresolved:
            tok = b.split()[0].lower()
            near = [k for k in reg if tok in k.lower()]
            print(f"  '{b}' -> candidates: {near or 'none'}")
        fail(f"{len(unresolved)} approved brand names not found in "
             f"brand_registry: {unresolved}. Fix APPROVED keys to match "
             f"registry brand_name exactly (or re-seed the registry first).")
    plan = {reg[b]: (b, sz) for b, sz in APPROVED.items()}
    print(f"[OK] All {len(APPROVED)} approved brands resolved to "
          f"normalized_name.\n")

    # -- Existing state ------------------------------------------------------
    table_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='carrier_size_map'").fetchone() is not None
    current = {}
    if table_exists:
        current = {row["normalized_name"]: row["carrier_size"]
                   for row in conn.execute(
                       "SELECT normalized_name, carrier_size FROM "
                       "carrier_size_map WHERE end_date IS NULL")}
        print(f"Table exists with {len(current)} current assignment(s).")
    else:
        print("Table does not exist -- will be created.")

    to_insert, ok_skip, conflicts = [], [], []
    for norm, (brand, sz) in sorted(plan.items()):
        if norm in current:
            (ok_skip if current[norm] == sz else conflicts).append(
                (brand, norm, current[norm], sz))
        else:
            to_insert.append((brand, norm, sz))

    print(f"\nPLAN: insert {len(to_insert)} | already-correct "
          f"{len(ok_skip)} | conflicts {len(conflicts)}")
    for brand, norm, sz in to_insert:
        print(f"  + {brand:<24} ({norm})  ->  {sz}")
    for brand, norm, cur, want in conflicts:
        print(f"  ! {brand:<24} current='{cur}' vs approved='{want}' -- "
              f"NOT modified; resolve deliberately (close current row, "
              f"insert new version).")

    if not args.apply:
        conn.close()
        print("\nDry-run complete. Re-run with --apply to write.")
        return

    # -- Write ---------------------------------------------------------------
    try:
        with conn:                      # single transaction
            conn.execute(DDL)
            conn.executemany(
                "INSERT INTO carrier_size_map "
                "(normalized_name, carrier_size, effective_date, end_date, "
                "note) VALUES (?, ?, ?, NULL, ?)",
                [(norm, sz, eff, SEED_NOTE) for _, norm, sz in to_insert])
    except sqlite3.Error as e:
        fail(f"Write failed, transaction rolled back: {e}")

    # -- Verify --------------------------------------------------------------
    rows = conn.execute(
        "SELECT carrier_size, COUNT(*) n FROM carrier_size_map "
        "WHERE end_date IS NULL GROUP BY carrier_size "
        "ORDER BY carrier_size").fetchall()
    total = sum(r["n"] for r in rows)
    print(f"\n[OK] Wrote {len(to_insert)} row(s). Current assignments now: "
          f"{total}")
    for r in rows:
        print(f"  {r['carrier_size']:<20} {r['n']}")
    if total != len(APPROVED) and not conflicts:
        print(f"[WARN] Expected {len(APPROVED)} current assignments, "
              f"found {total} -- table may contain assignments beyond "
              f"the seed (fine if deliberate).")
    conn.close()
    print("\nDone. carrier_size_map is live; build_keyword_category_map "
          "reads it via LEFT JOIN (missing -> unmapped).")


if __name__ == "__main__":
    main()
