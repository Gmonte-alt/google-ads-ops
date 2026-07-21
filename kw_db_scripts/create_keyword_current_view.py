#!/usr/bin/env python
"""
create_keyword_current_view.py  --  Keyword remediation Step 3.

Creates the canonical `keyword_current` view: one row per keyword PLACEMENT
(ad_group_criterion_id = '{ad_group_id}~{criterion_id}'), joined to its ad group and campaign
for hierarchy status, exposing a `hierarchy_enabled` flag (campaign ENABLED AND ad group ENABLED).

This is the single interface every consumer (edit-file generator, covering-check, per-keyword
analyses) should read instead of the raw keyword_instances table. It does NOT collapse by
criterion_id -- a keyword legitimately has many placements across ad groups; each is one row,
addressable by its immutable ad_group_criterion_id edit key.

SAFE: a view is non-destructive (no data mutation). Idempotent (DROP VIEW IF EXISTS + CREATE).
Requires explicit --db. Verifies the view after creation.

Serving semantics:
  - hierarchy_enabled = 1  <=> campaign.status='ENABLED' AND ad_group.status='ENABLED'
  - A placement is TRULY serving when hierarchy_enabled=1 AND keyword status='ENABLED'.
    (Consumers apply the keyword-status filter themselves depending on need.)

Usage:
    python create_keyword_current_view.py --db PATH            # dry-run (prints DDL + preview)
    python create_keyword_current_view.py --db PATH --apply    # create the view
"""
import argparse
import os
import sqlite3
import sys

VIEW = "keyword_current"

DDL = """
CREATE VIEW keyword_current AS
SELECT
    ki.account_id,
    ki.ad_group_criterion_id,               -- '{ad_group_id}~{criterion_id}' : immutable edit key
    ki.criterion_id,                         -- shared criterion (NOT unique across ad groups)
    ki.keyword_text,
    ki.match_type,
    ki.status                AS keyword_status,
    ki.campaign_id,
    c.campaign_name,
    c.status                 AS campaign_status,
    ki.ad_group_id,
    ag.ad_group_name,
    ag.status                AS ad_group_status,
    CASE WHEN c.status = 'ENABLED' AND ag.status = 'ENABLED'
         THEN 1 ELSE 0 END   AS hierarchy_enabled,
    -- truly serving = live keyword in a live ad group in a live campaign
    CASE WHEN c.status = 'ENABLED' AND ag.status = 'ENABLED' AND ki.status = 'ENABLED'
         THEN 1 ELSE 0 END   AS is_serving,
    ki.cpc_bid_micros,
    ki.quality_score,
    ki.identity_id,
    ki.last_seen_date,
    ki.platform
FROM keyword_instances ki
JOIN ad_groups ag ON ag.ad_group_id = ki.ad_group_id AND ag.account_id = ki.account_id
JOIN campaigns  c ON c.campaign_id  = ki.campaign_id  AND c.account_id  = ki.account_id
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True, help="explicit path to the DB (no default)")
    ap.add_argument("--apply", action="store_true", help="create the view (default: dry-run)")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        sys.exit("[ABORT] DB does not exist: %s" % args.db)
    size = os.path.getsize(args.db)
    print("opening DB: %s  (%d bytes)" % (os.path.abspath(args.db), size))

    conn = sqlite3.connect(args.db)

    # pre-flight: required columns exist
    ki_cols = [r[1] for r in conn.execute("PRAGMA table_info(keyword_instances)")]
    for req in ("ad_group_criterion_id", "criterion_id", "match_type", "status",
                "campaign_id", "ad_group_id"):
        if req not in ki_cols:
            sys.exit("[ABORT] keyword_instances missing required column '%s' "
                     "(run Step 1 migration first)." % req)
    for t in ("campaigns", "ad_groups"):
        cols = [r[1] for r in conn.execute("PRAGMA table_info(%s)" % t)]
        if "status" not in cols:
            sys.exit("[ABORT] %s has no status column." % t)
    print("pre-flight OK: keyword_instances has ad_group_criterion_id; campaigns/ad_groups have status.")

    print("\n--- DDL to be applied ---")
    print(DDL.strip())

    if not args.apply:
        print("\n[dry-run] view NOT created. Re-run with --apply.")
        conn.close()
        return

    # create (idempotent)
    print("\n[apply] dropping any existing view and creating %s ..." % VIEW)
    conn.execute("DROP VIEW IF EXISTS %s" % VIEW)
    conn.execute(DDL)
    conn.commit()

    # verify: view exists and returns sane numbers
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='view' AND name=?", (VIEW,)
    ).fetchone()
    if not exists:
        sys.exit("[ABORT] view creation did not take.")

    total = conn.execute("SELECT COUNT(*) FROM %s" % VIEW).fetchone()[0]
    serving = conn.execute("SELECT COUNT(*) FROM %s WHERE is_serving=1" % VIEW).fetchone()[0]
    hier = conn.execute("SELECT COUNT(*) FROM %s WHERE hierarchy_enabled=1" % VIEW).fetchone()[0]
    # per account
    print("\n[verify] view '%s' created." % VIEW)
    print("  total placement rows: %s" % f"{total:,}")
    print("  hierarchy_enabled=1 : %s" % f"{hier:,}")
    print("  is_serving=1        : %s" % f"{serving:,}")
    print("\n  by account (account_id | total | serving):")
    for r in conn.execute(
        "SELECT account_id, COUNT(*), SUM(is_serving) FROM %s GROUP BY account_id ORDER BY 2 DESC" % VIEW
    ):
        print("   %5s | %10s | %s" % (r[0], f"{r[1]:,}", f"{(r[2] or 0):,}"))

    # sanity spot-check: the costco keyword's serving placements
    print("\n  spot-check (costco condo insurance, account 1, serving placements):")
    for r in conn.execute(
        "SELECT ad_group_criterion_id, match_type, ad_group_name FROM %s "
        "WHERE account_id=1 AND keyword_text='costco condo insurance' AND is_serving=1 LIMIT 5" % VIEW
    ):
        print("    %s | %s | %s" % (r[0], r[1], r[2]))

    conn.close()
    print("\nDone. Consumers should now read '%s' (filter is_serving=1 for live keywords) "
          "instead of the raw keyword_instances table." % VIEW)


if __name__ == "__main__":
    main()
