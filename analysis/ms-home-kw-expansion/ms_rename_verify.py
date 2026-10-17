"""
ms_rename_verify.py
-------------------
Post-rename verification for Microsoft Home (account 2). Answers two
questions the account-wide name check could NOT answer, because the same
ad-group name exists in ~50 legacy state campaigns as well as the live
tier campaigns:

  PART A — Pre-rename names still inside TIER campaigns.
      Legacy state campaigns keeping the old names is expected (they were
      never in the rename scope). Tier campaigns are the live book, so a
      pre-rename name there is either a missed rename or an out-of-scope
      ad group. Prints ad_group_id, ad-group status, PARENT CAMPAIGN
      status, and keyword counts so it can be settled against the mapping.

  PART B — Name-ambiguity report for the import generator.
      For each generic ad-group name, how many distinct (campaign,
      ad_group_id) pairs it resolves to, and how many of those are
      actually SERVING (ad group ENABLED *and* parent campaign ENABLED).
      Any name with >1 serving target cannot be routed by name alone.

Read-only. Introspects column names, so it adapts if the schema differs.

USAGE:
    python ms_rename_verify.py
    python ms_rename_verify.py --db-path "C:\\Projects\\google-ads-ops\\db\\paid_search.db"
    python ms_rename_verify.py --tier-pattern TIER --generic-prefix Home-Generic-
"""

import argparse
import sqlite3
import sys

PRE_RENAME_NAMES = [
    "Home-Generic-Home Insurance-Cheapest-NA",
    "Home-Generic-Home Insurance-Core-NA",
    "Home-Generic-Home Insurance-Core-Up",
    "Home-Generic-Insurance-Cheapest-NA",
]


def cols(conn, table):
    try:
        return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
    except sqlite3.Error:
        return []


def pick(available, *candidates):
    for c in candidates:
        if c in available:
            return c
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db-path", default=r"C:\Projects\google-ads-ops\db\paid_search.db")
    ap.add_argument("--account-id", type=int, default=2)
    ap.add_argument("--tier-pattern", default="TIER",
                    help="substring identifying live tier campaigns (default: TIER)")
    ap.add_argument("--generic-prefix", default="Home-Generic-",
                    help="ad-group name prefix for the generic book")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db_path)
    ag_cols, c_cols, ki_cols = (cols(conn, "ad_groups"), cols(conn, "campaigns"),
                                cols(conn, "keyword_instances"))
    if not ag_cols or not c_cols:
        print("ERROR: ad_groups / campaigns not found in this DB.")
        return 2

    ag_name = pick(ag_cols, "ad_group_name", "name")
    ag_id = pick(ag_cols, "ad_group_id", "id")
    ag_status = pick(ag_cols, "status", "ad_group_status")
    ag_camp = pick(ag_cols, "campaign_id")
    c_id = pick(c_cols, "campaign_id", "id")
    c_name = pick(c_cols, "campaign_name", "name")
    c_status = pick(c_cols, "status", "campaign_status")
    acct_col_ag = pick(ag_cols, "account_id")
    acct_col_c = pick(c_cols, "account_id")

    missing = [n for n, v in [("ad_groups.name", ag_name), ("ad_groups.campaign_id", ag_camp),
                              ("campaigns.id", c_id), ("campaigns.name", c_name)] if not v]
    if missing:
        print("ERROR: could not resolve columns:", ", ".join(missing))
        print(" ad_groups:", ag_cols, "\n campaigns:", c_cols)
        return 2

    ag_scope = f"ag.{acct_col_ag} = {args.account_id}" if acct_col_ag else \
               f"c.{acct_col_c} = {args.account_id}"
    agst = f"ag.{ag_status}" if ag_status else "'?'"
    cst = f"c.{c_status}" if c_status else "'?'"

    # keyword counts per (campaign_name, ad_group_name) if available
    kw_by_pair = {}
    if ki_cols and pick(ki_cols, "ad_group_name") and pick(ki_cols, "campaign_name"):
        for cn, an, n in conn.execute(
                f"""SELECT campaign_name, ad_group_name, COUNT(*)
                    FROM keyword_instances WHERE account_id = ?
                    GROUP BY 1,2""", (args.account_id,)):
            kw_by_pair[(cn, an)] = n

    # ── PART A ────────────────────────────────────────────────────────
    ph = ",".join("?" * len(PRE_RENAME_NAMES))
    sql_a = f"""
        SELECT c.{c_name}, ag.{ag_name}, ag.{ag_id}, {agst}, {cst}
        FROM ad_groups ag
        JOIN campaigns c ON c.{c_id} = ag.{ag_camp}
        WHERE {ag_scope}
          AND UPPER(c.{c_name}) LIKE '%{args.tier_pattern.upper()}%'
          AND ag.{ag_name} IN ({ph})
        ORDER BY 1,2
    """
    rows = list(conn.execute(sql_a, PRE_RENAME_NAMES))
    print("=" * 100)
    print("PART A — pre-rename ad-group names inside TIER campaigns (the live book)")
    print("=" * 100)
    if not rows:
        print("  none — every pre-rename name lives only in legacy state campaigns. Nothing to fix.")
    else:
        print(f"  {'CAMPAIGN':<46} {'AD GROUP':<40} {'AG_ID':>16} {'AG':<9} {'CAMPAIGN':<9} {'KWS':>5}  SERVING")
        for cn, an, aid, ast, cs in rows:
            kws = kw_by_pair.get((cn, an), "-")
            serving = "YES" if str(ast).upper() == "ENABLED" and str(cs).upper() == "ENABLED" else "no"
            print(f"  {str(cn)[:46]:<46} {str(an)[:40]:<40} {str(aid):>16} "
                  f"{str(ast):<9} {str(cs):<9} {str(kws):>5}  {serving}")
        print("\n  -> SERVING=YES rows are live under a live campaign: check these against the")
        print("     rename mapping. SERVING=no means the parent campaign is paused (lower stakes).")

    # ── PART B ────────────────────────────────────────────────────────
    sql_b = f"""
        SELECT ag.{ag_name}, COUNT(*) AS targets,
               SUM(CASE WHEN UPPER({agst})='ENABLED' AND UPPER({cst})='ENABLED'
                        THEN 1 ELSE 0 END) AS serving
        FROM ad_groups ag
        JOIN campaigns c ON c.{c_id} = ag.{ag_camp}
        WHERE {ag_scope} AND ag.{ag_name} LIKE ?
        GROUP BY 1 ORDER BY serving DESC, targets DESC
    """
    rows_b = list(conn.execute(sql_b, (args.generic_prefix + "%",)))
    print()
    print("=" * 100)
    print("PART B — name ambiguity for the import generator (generic book)")
    print("=" * 100)
    print(f"  {'AD GROUP NAME':<52} {'TARGETS':>8} {'SERVING':>8}  VERDICT")
    amb = 0
    for an, targets, serving in rows_b:
        serving = serving or 0
        if serving > 1:
            verdict = "AMBIGUOUS — name alone cannot route"
            amb += 1
        elif serving == 1:
            verdict = "ok — exactly one serving target"
        else:
            verdict = "no serving target (all paused)"
        print(f"  {str(an)[:52]:<52} {targets:>8} {serving:>8}  {verdict}")
    print()
    print(f"  {amb} generic ad-group name(s) resolve to MORE THAN ONE serving target.")
    if amb:
        print("  -> The generator must scope by campaign (or join ad_group_id), and must check")
        print("     PARENT CAMPAIGN status — an ENABLED ad group under a paused campaign is not serving.")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
