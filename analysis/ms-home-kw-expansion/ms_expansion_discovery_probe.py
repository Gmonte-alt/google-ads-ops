"""
ms_expansion_discovery_probe.py — READ-ONLY discovery probe for the Microsoft Home
(account_id=2) keyword-expansion coverage check. Answers the five open facts before
any selection/coverage logic is built:

  (1) keyword_current / keyword_instances columns; does keyword_current pre-filter to
      live, or expose status? What are the actual status + match_type values on acct 2?
  (2) Parent-level status columns (ad_groups / campaigns) — needed because "biddable"
      requires ENABLED at keyword AND ad group AND campaign.
  (3) MS sqp coverage window + instance_id population (the Tier-B dedupe depends on it).
  (4) Any negatives table at all (expected: none -> overlay from exports).
  (5) Generic-book scoping: does the Non-Generic/NonGeneric naming token exist on the
      MS account, or does MS need a different generic/branded rule?

All column names are resolved at runtime (PRAGMA) — nothing is assumed, per the
introspect-don't-whitelist convention. Paste the full output back.

    python ms_expansion_discovery_probe.py
    python ms_expansion_discovery_probe.py --db C:\\path\\to\\paid_search.db
"""
import argparse
import sqlite3
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
ACCT = 2  # Microsoft Home


def cols(conn, table):
    try:
        return [r[1] for r in conn.execute("PRAGMA table_info(%s)" % table)]
    except sqlite3.OperationalError:
        return []


def pick(cands, present):
    return next((c for c in cands if c in present), None)


def section(title):
    print("\n" + "=" * 78)
    print("  " + title)
    print("=" * 78)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--db", default=r"C:\Projects\google-ads-ops\db\paid_search.db")
    a = p.parse_args()
    if not Path(a.db).exists():
        sys.exit("DB not found: %s" % a.db)
    conn = sqlite3.connect("file:%s?mode=ro" % a.db, uri=True)  # READ-ONLY

    # ------------------------------------------------------------------ (1)
    section("(1) keyword_current + keyword_instances — columns & acct-2 values")
    kc_cols = cols(conn, "keyword_current")
    ki_cols = cols(conn, "keyword_instances")
    print("  keyword_current columns : %s" % (", ".join(kc_cols) if kc_cols else "(view/table not found)"))
    print("  keyword_instances cols  : %s" % ", ".join(ki_cols))

    ki_status = pick(("status", "keyword_status", "criterion_status"), ki_cols)
    ki_match = pick(("match_type",), ki_cols)
    n_ki = conn.execute("SELECT COUNT(*) FROM keyword_instances WHERE account_id=?", (ACCT,)).fetchone()[0]
    print("\n  acct-2 keyword_instances rows: %s" % f"{n_ki:,}")
    if ki_status:
        print("  DISTINCT %s (acct 2):" % ki_status)
        for v, n in conn.execute(
                "SELECT %s, COUNT(*) FROM keyword_instances WHERE account_id=? "
                "GROUP BY 1 ORDER BY 2 DESC" % ki_status, (ACCT,)):
            print("      %-24r %s" % (v, f"{n:,}"))
    else:
        print("  ⚠ no status-like column on keyword_instances — ask where live/paused lives")
    if ki_match:
        print("  DISTINCT %s (acct 2):" % ki_match)
        for v, n in conn.execute(
                "SELECT %s, COUNT(*) FROM keyword_instances WHERE account_id=? "
                "GROUP BY 1 ORDER BY 2 DESC" % ki_match, (ACCT,)):
            print("      %-24r %s" % (v, f"{n:,}"))
    if kc_cols:
        kc_status = pick(("status", "keyword_status", "criterion_status"), kc_cols)
        has_acct = "account_id" in kc_cols
        if has_acct and kc_status:
            print("  DISTINCT keyword_current.%s (acct 2):" % kc_status)
            for v, n in conn.execute(
                    "SELECT %s, COUNT(*) FROM keyword_current WHERE account_id=? "
                    "GROUP BY 1 ORDER BY 2 DESC" % kc_status, (ACCT,)):
                print("      %-24r %s" % (v, f"{n:,}"))
            print("  -> if only ENABLED-ish values appear, the view pre-filters; if mixed, filter yourself")
        elif has_acct:
            n_kc = conn.execute("SELECT COUNT(*) FROM keyword_current WHERE account_id=?", (ACCT,)).fetchone()[0]
            print("  keyword_current acct-2 rows: %s (no status column — compare to ki count above:"
                  " smaller => pre-filtered)" % f"{n_kc:,}")

    # ------------------------------------------------------------------ (2)
    section("(2) Parent status — ad_groups / campaigns (acct 2)")
    for tbl in ("ad_groups", "campaigns"):
        tc = cols(conn, tbl)
        if not tc:
            print("  %s: table not found" % tbl)
            continue
        st = pick(("status", "%s_status" % tbl[:-1], "ad_group_status", "campaign_status"), tc)
        has_acct = "account_id" in tc
        print("  %s columns: %s" % (tbl, ", ".join(tc)))
        if st and has_acct:
            print("    DISTINCT %s.%s (acct 2):" % (tbl, st))
            for v, n in conn.execute(
                    "SELECT %s, COUNT(*) FROM %s WHERE account_id=? GROUP BY 1 ORDER BY 2 DESC"
                    % (st, tbl), (ACCT,)):
                print("        %-24r %s" % (v, f"{n:,}"))
        elif not st:
            print("    ⚠ no status-like column on %s" % tbl)

    # ------------------------------------------------------------------ (3)
    section("(3) MS sqp coverage window + instance_id population")
    sqp_cols = cols(conn, "search_query_performance")
    if "platform" not in sqp_cols:
        print("  ⚠ sqp has no platform column?? columns: %s" % ", ".join(sqp_cols))
    else:
        row = conn.execute(
            "SELECT MIN(report_date), MAX(report_date), COUNT(*), "
            "       SUM(CASE WHEN instance_id IS NULL THEN 1 ELSE 0 END) "
            "FROM search_query_performance WHERE platform='microsoft'").fetchone()
        lo, hi, n, nulls = row
        if n == 0:
            print("  0 rows for platform='microsoft' — check the platform value spelling:")
            for v, cnt in conn.execute(
                    "SELECT platform, COUNT(*) FROM search_query_performance GROUP BY 1"):
                print("      %-16r %s" % (v, f"{cnt:,}"))
        else:
            print("  coverage: %s -> %s   rows=%s   NULL instance_id rows=%s (%.1f%%)"
                  % (lo, hi, f"{n:,}", f"{nulls:,}", 100.0 * (nulls or 0) / n))
            print("  DISTINCT device values (MS rows) — expect NO 'ALL' rollup:")
            for v, cnt in conn.execute(
                    "SELECT device, COUNT(*) FROM search_query_performance "
                    "WHERE platform='microsoft' GROUP BY 1 ORDER BY 2 DESC"):
                print("      %-16r %s" % (v, f"{cnt:,}"))

    # ------------------------------------------------------------------ (4)
    section("(4) Negatives tables in paid_search.db")
    hits = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table','view') "
        "AND (lower(name) LIKE '%negative%' OR lower(name) LIKE '%neg_%')")]
    if hits:
        for t in hits:
            print("  FOUND: %s   columns: %s" % (t, ", ".join(cols(conn, t))))
    else:
        print("  none — confirmed: negatives must be overlaid from platform exports")
        print("  (USAA negative added ~9/7 is invisible to the DB; 'not served' on USAA-ish")
        print("   terms will be negative-blocked, not zero-demand)")

    # ------------------------------------------------------------------ (5)
    section("(5) Generic-book scoping — MS ad-group naming")
    ag_cols = cols(conn, "ad_groups")
    ag_name = pick(("name", "ad_group_name"), ag_cols)
    if ag_name and "account_id" in ag_cols:
        n_all = conn.execute("SELECT COUNT(*) FROM ad_groups WHERE account_id=?", (ACCT,)).fetchone()[0]
        n_tok = conn.execute(
            "SELECT COUNT(*) FROM ad_groups WHERE account_id=? AND ("
            "lower(%s) LIKE '%%non-generic%%' OR lower(%s) LIKE '%%nongeneric%%' "
            "OR lower(%s) LIKE '%%non generic%%')" % (ag_name, ag_name, ag_name),
            (ACCT,)).fetchone()[0]
        n_gen = conn.execute(
            "SELECT COUNT(*) FROM ad_groups WHERE account_id=? AND lower(%s) LIKE '%%generic%%'"
            % ag_name, (ACCT,)).fetchone()[0]
        print("  acct-2 ad groups: %s | containing 'generic': %s | containing Non-Generic variants: %s"
              % (f"{n_all:,}", f"{n_gen:,}", f"{n_tok:,}"))
        verdict = ("token PRESENT — Google rule may carry; verify branded groups all carry it"
                   if n_tok else
                   "token ABSENT — Google rule does NOT carry; need an MS-specific generic/branded rule")
        print("  -> %s" % verdict)
        print("\n  sample of DISTINCT acct-2 ad-group names (first 40, alphabetical):")
        for (nm,) in conn.execute(
                "SELECT DISTINCT %s FROM ad_groups WHERE account_id=? ORDER BY 1 LIMIT 40"
                % ag_name, (ACCT,)):
            print("      %s" % nm)
    else:
        print("  ⚠ could not resolve ad_groups name/account columns: %s" % ", ".join(ag_cols))

    conn.close()
    print("\nPaste this whole output back; it pins the five facts the coverage check needs.")


if __name__ == "__main__":
    main()
