"""
dump_schema.py
==============
Prints the full CREATE TABLE statements for the core tables in paid_search.db.
Share this output with any new chat thread that needs to understand the schema
before writing import scripts or queries.

RUN
───
    conda activate paid-search
    python dump_schema.py
"""

import sqlite3
from pathlib import Path

DB_PATH = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")

TABLES = [
    "accounts",
    "campaigns",
    "ad_groups",
    "campaign_performance_daily",
    "keyword_instances",
    "keyword_identities",
    "keyword_performance_daily",
    "search_queries",
    "search_query_performance",
    "conversion_actions",
]

def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def main():
    conn   = get_conn()
    cursor = conn.cursor()

    print("=" * 68)
    print("paid_search.db — Full Schema Dump")
    print(f"Database: {DB_PATH}")
    print("=" * 68)

    # ── Table schemas ──────────────────────────────────────────────────────────
    print("\n── TABLE SCHEMAS ─────────────────────────────────────────────────\n")

    for table in TABLES:
        row = cursor.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
            (table,)
        ).fetchone()
        if row and row["sql"]:
            print(row["sql"].strip())
            print()
        else:
            print(f"-- TABLE NOT FOUND: {table}")
            print()

    # ── Index listing ──────────────────────────────────────────────────────────
    print("\n── INDEXES ───────────────────────────────────────────────────────\n")

    indexes = cursor.execute("""
        SELECT name, tbl_name, sql
        FROM sqlite_master
        WHERE type = 'index'
          AND sql IS NOT NULL
        ORDER BY tbl_name, name
    """).fetchall()

    current_table = None
    for idx in indexes:
        if idx["tbl_name"] != current_table:
            current_table = idx["tbl_name"]
            print(f"  -- {current_table}")
        print(f"  {idx['sql'].strip()};")
    print()

    # ── Row counts and account inventory ──────────────────────────────────────
    print("\n── ROW COUNTS ────────────────────────────────────────────────────\n")

    count_tables = [
        "accounts",
        "campaigns",
        "ad_groups",
        "campaign_performance_daily",
        "keyword_instances",
        "keyword_identities",
        "keyword_performance_daily",
        "search_queries",
        "search_query_performance",
        "conversion_actions",
        "negative_keywords",
        "geo_target_constants",
    ]

    for table in count_tables:
        try:
            n = cursor.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            print(f"  {table:<40} {n:>12,}")
        except Exception as e:
            print(f"  {table:<40} ERROR: {e}")

    # ── Accounts inventory ─────────────────────────────────────────────────────
    print("\n── ACCOUNTS TABLE ────────────────────────────────────────────────\n")

    accts = cursor.execute("""
        SELECT id, platform_account_id, platform, account_name,
               mcc_id, business_unit_id
        FROM accounts
        ORDER BY id
    """).fetchall()

    if accts:
        print(f"  {'id':>4}  {'platform':<12} {'platform_account_id':<22} "
              f"{'account_name':<25} {'mcc_id':<22} {'bu_id'}")
        print(f"  {'─'*4}  {'─'*12} {'─'*22} {'─'*25} {'─'*22} {'─'*6}")
        for a in accts:
            print(f"  {a['id']:>4}  "
                  f"{str(a['platform'] or ''):<12} "
                  f"{str(a['platform_account_id'] or ''):<22} "
                  f"{str(a['account_name'] or ''):<25} "
                  f"{str(a['mcc_id'] or ''):<22} "
                  f"{str(a['business_unit_id'] or '')}")
    else:
        print("  (no accounts found)")

    # ── account_id type check ──────────────────────────────────────────────────
    print("\n── ACCOUNT_ID TYPE CHECK ─────────────────────────────────────────\n")
    print("  Verifies TEXT vs INTEGER convention per table.")
    print()

    type_checks = [
        ("campaigns",                  "account_id"),
        ("ad_groups",                  "account_id"),
        ("campaign_performance_daily", "account_id"),
        ("keyword_instances",          "account_id"),
        ("keyword_performance_daily",  "instance_id"),  # no account_id here
        ("search_query_performance",   "instance_id"),
    ]

    for table, col in type_checks:
        try:
            row = cursor.execute(
                f"SELECT {col}, typeof({col}) AS t FROM {table} LIMIT 1"
            ).fetchone()
            if row:
                print(f"  {table:<40} {col}={row[col]}  type={row['t']}")
            else:
                print(f"  {table:<40} (empty — no rows to sample)")
        except Exception as e:
            print(f"  {table:<40} ERROR: {e}")

    print()
    print("=" * 68)
    print("Done.")
    conn.close()


if __name__ == "__main__":
    main()
