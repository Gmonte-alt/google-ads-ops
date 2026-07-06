#!/usr/bin/env python
"""
13_persist_target_theme.py -- persist self-owned categorization onto search_queries.

Adds three NEW columns and populates them, leaving ALL of 08's columns (theme, theme_confidence,
modifier_*, brand_category, intent_*, tag_*) UNTOUCHED. 08 is fenced off as an audit target, not a
dependency; this pass is the trusted parallel tagging the parity join reads from.

  target_theme         -- categorize(query_text): ADJACENT:* | BUNDLE | HOMEOWNERS_EXT | <12 themes> | CORE
  target_theme_source  -- provenance/version stamp (default TAG_VERSION), for the audit trail
  target_group         -- V3 query-group brand-structure cut: 'BRAND' | 'NONBRAND' (cleaned registry,
                          two-pronged sentinel guard), kept parallel so brand/non-brand stay separable

Idempotent (INSERT OR REPLACE semantics via UPDATE keyed on search_queries.id; re-runnable, and the
guarded ADD COLUMN is a no-op once present). Dry-run by default; --apply to write.

    python 13_persist_target_theme.py --db paid_search.db                 # dry-run: counts only
    python 13_persist_target_theme.py --db paid_search.db --apply         # write
    python 13_persist_target_theme.py --db paid_search.db --apply --brand-db paid_search.db
"""
import argparse
import os
import sqlite3

from parity_common import categorize, make_brand_tagger, brand_of

TAG_VERSION = "target_theme.v1(assign_theme+ADJACENT; collapse-cover)"

NEW_COLUMNS = [
    ("target_theme", "TEXT"),
    ("target_theme_source", "TEXT"),
    ("target_group", "TEXT"),
]


def ensure_columns(conn, apply):
    """Schema-guarded ADD COLUMN: add only what's absent, and ONLY under --apply (dry-run touches
    nothing). Returns (missing, added)."""
    have = {r[1] for r in conn.execute("PRAGMA table_info(search_queries)")}
    missing = [name for name, _ in NEW_COLUMNS if name not in have]
    added = []
    if apply:
        for name, decl in NEW_COLUMNS:
            if name not in have:
                conn.execute("ALTER TABLE search_queries ADD COLUMN %s %s" % (name, decl))
                added.append(name)
    return missing, added


def _report(theme_counts, group_counts, total, label):
    core = theme_counts.get("CORE", 0)
    print("\ntarget_theme distribution%s:" % label)
    for theme in sorted(theme_counts, key=lambda t: -theme_counts[t]):
        print("  %-24s %7d  (%.1f%%)" % (theme, theme_counts[theme],
                                         100 * theme_counts[theme] / total if total else 0))
    print("target_group:  BRAND=%d  NONBRAND=%d" % (group_counts["BRAND"], group_counts["NONBRAND"]))
    print("CORE catch-all rate%s: %.1f%%%s" % (
        label, 100 * core / total if total else 0,
        "  <-- watch this; high => assign_theme patterns need widening for messy query text"
        if not label else ""))


def _table_exists(conn, name):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                        (name,)).fetchone() is not None


def scoped_query_ids(conn, account_id, days):
    """query_ids account `account_id` actually realized -- exactly Set 3's scope: sqp joined to
    keyword_instances (sqp has no account_id), device='ALL', trailing `days`-day window.
    Returns None (not an exception) if the required tables are absent -- e.g. when pointed at a
    DB that isn't the live paid_search.db."""
    for t in ("search_query_performance", "keyword_instances"):
        if not _table_exists(conn, t):
            print("\n[scope] '%s' not found in this DB -- scoped diagnostic skipped. "
                  "Are you pointed at the live paid_search.db?" % t)
            return None
    rows = conn.execute(
        "SELECT DISTINCT p.query_id FROM search_query_performance p "
        "JOIN keyword_instances ki ON ki.id = p.instance_id "
        "WHERE ki.account_id = ? AND p.device = 'ALL' AND p.report_date >= date('now', ?)",
        (account_id, "-%d day" % days)).fetchall()
    return {r[0] for r in rows}


def run(db, brand_db, apply, scope_account=None, days=90, force_small=False):
    abspath = os.path.abspath(db)
    size = os.path.getsize(db) if os.path.exists(db) else -1
    print("opening DB: %s  (%s)" % (abspath, ("%d bytes" % size) if size >= 0 else "DOES NOT EXIST"))
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")

    missing, added = ensure_columns(conn, apply)
    if apply:
        print("columns checked; added: %s" % (added or "none (all present)"))
    else:
        print("columns checked; would add under --apply: %s" % (missing or "none (all present)"))

    tagger = make_brand_tagger(brand_db or db)

    rows = conn.execute("SELECT id, query_text FROM search_queries").fetchall()
    print("scanning %d search_queries rows" % len(rows))
    if len(rows) < 1000:
        msg = ("search_queries has only %d rows -- far smaller than the live account (~327k). "
               "This may not be the live paid_search.db." % len(rows))
        if apply and not force_small:
            raise SystemExit(
                "[ABORT] %s\n"
                "        Refusing to --apply to a suspiciously small DB. If this is intentional "
                "(e.g. a test fixture), re-run with --force-small." % msg)
        print("  [WARN] %s" % msg)

    updates = []
    per_qid = {}  # qid -> (theme, group)
    theme_counts, group_counts = {}, {"BRAND": 0, "NONBRAND": 0}
    for qid, qtext in rows:
        theme = categorize(qtext or "")
        brand = brand_of(tagger, qtext or "")[0]
        group = "BRAND" if brand else "NONBRAND"
        updates.append((theme, TAG_VERSION, group, qid))
        per_qid[qid] = (theme, group)
        theme_counts[theme] = theme_counts.get(theme, 0) + 1
        group_counts[group] += 1

    _report(theme_counts, group_counts, len(updates), "")

    if scope_account is not None:
        sids = scoped_query_ids(conn, scope_account, days)
        if sids is not None:
            s_theme, s_group = {}, {"BRAND": 0, "NONBRAND": 0}
            for qid in sids:
                if qid in per_qid:
                    th, gr = per_qid[qid]
                    s_theme[th] = s_theme.get(th, 0) + 1
                    s_group[gr] += 1
            n = sum(s_theme.values())
            print("\n" + "=" * 70)
            print("SCOPED to account_id=%d realized demand (device='ALL', trailing %dd): %d queries"
                  % (scope_account, days, n))
            _report(s_theme, s_group, n, " [account %d]" % scope_account)
            print("=" * 70)
            print("^ THIS CORE rate governs the analysis. >45%% => widen assign_theme before the join.")

    if not apply:
        print("\n[dry-run] no rows written. Re-run with --apply to persist.")
        conn.close()
        return

    conn.executemany(
        "UPDATE search_queries SET target_theme=?, target_theme_source=?, target_group=? WHERE id=?",
        updates,
    )
    conn.commit()
    print("\n[apply] wrote target_theme/target_theme_source/target_group for %d rows." % len(updates))
    conn.close()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--db", required=True,
                   help="explicit path to the target DB (no default -- must be named)")
    p.add_argument("--brand-db", default=None, help="registry DB for BrandTagger (default: --db)")
    p.add_argument("--apply", action="store_true", help="write (default: dry-run)")
    p.add_argument("--force-small", action="store_true",
                   help="permit --apply against a <1000-row DB (test fixtures only)")
    p.add_argument("--scope-account", type=int, default=None,
                   help="report-only: also print the theme distribution restricted to this "
                        "account's realized demand (the CORE rate that governs the analysis)")
    p.add_argument("--days", type=int, default=90, help="trailing window for --scope-account")
    a = p.parse_args()
    run(a.db, a.brand_db, a.apply, a.scope_account, a.days, a.force_small)


if __name__ == "__main__":
    main()
