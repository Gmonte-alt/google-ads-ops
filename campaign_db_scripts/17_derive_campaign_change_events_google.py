"""
17_derive_campaign_change_events_google.py  (Google change-history)

Pipeline step 17.
CADENCE: RECURRING -- run after 18 (API pull) to rebuild from the freshened ledger.

Derives the campaign-grain change timeline (TARGET_ROAS + DAILY_BUDGET) from BOTH
ledgers, deduped:
  * CSV ledger (google_change_csv_raw) -- the one-time YTD prose seed (pre-API
    history). Parsed via the prose catalog; campaign resolved by name against current
    campaigns, then (for names renamed/retired since the change was logged) against the
    pre-restructure snapshot, which preserves the immutable campaign_id under the old
    name (resolved_via=name_match_snapshot). Unmatched-either-way names are reported.
  * API ledger (google_change_event_raw) -- the going-forward weekly feed. Id-native;
    category keyed STRICTLY off change_resource_type (never off populated parents):
      CAMPAIGN + campaign.(target_roas|maximize_conversion_value.target_roas) -> TARGET_ROAS, resolved_via=campaign_native
      CAMPAIGN_BUDGET + amount_micros (/1e6 -> $)                              -> DAILY_BUDGET, budget_id -> campaign(s)
      BIDDING_STRATEGY + target_roas.target_roas                               -> TARGET_ROAS, bidding_strategy_id -> campaign(s)

Resolution wrinkles the API introduces (the CSV named the campaign in prose):
  * Budget -> campaign via campaigns.budget_id. 1:1 today (resolved_via=budget_native).
    A budget resolving to >1 campaign fans out (resolved_via=shared_budget_fanout)
    and fires a tripwire WARN -- dormant path, since no shared budgets exist today.
  * Portfolio strategy -> campaign(s) via campaigns.bidding_strategy_id. Genuinely
    1:many today; fans out one event per campaign (resolved_via=portfolio_strategy_fanout)
    so the measurement layer can group them as one root intervention. A 1:1 strategy
    resolves as portfolio_strategy.

Cross-source dedup (the chief dual-source correctness risk): the API is NOT a clean
superset of the UI, so the same change can appear in both. row_hash dedupes only
WITHIN a source; here we dedupe ACROSS sources on event_dedup_key (campaign_id,
change_date, change_type, old, new) with the API authoritative in overlap. Then the
same-day value-link collapse runs on the deduped candidates.

Other rules (unchanged from the CSV-only build):
  * ad-group-grain CSV rows skipped (deferred); 'Target ROAS is X%' / 'was X%' never emitted.
  * Portfolio-strategy creation -> TARGET_ROAS anchor with old_value=NULL (timeline
    start; excluded from before/after reads via old_value IS NOT NULL).
  * Budget stored in DOLLARS (cross-platform consistency); campaigns.daily_budget_micros
    is micros (dollars * 1e6 to join back).

Idempotent derive-from-scratch, scoped to platform='google' AND the resolved
account so Microsoft's rows are untouched. Backs up before mutating.

Usage:
    python derive_campaign_change_events_google.py --db PATH                 # dry-run
    python derive_campaign_change_events_google.py --db PATH --apply
"""

import argparse
import sys
import json
import collections
import gads_change_common as C

SOURCE = "google_ui_csv"
RESOLVED_VIA = "name_match_current"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--cid", default=C.DEFAULT_CID)
    ap.add_argument("--convention-version", default="v1-troas-budget-2026-06",
                    help="non-null tag stored in convention_version (live table requires it)")
    ap.add_argument("--rename-snapshot", default="campaigns_snapshot_pre_restructure_20260603",
                    help="pre-restructure snapshot used as a historical name->id fallback for CSV "
                         "rows whose campaign was renamed/retired since the change was logged")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    conn = C.connect(args.db, write=args.apply)
    try:
        acct = C.resolve_account_id(conn, args.cid)

        name2id = {r["campaign_name"]: r["campaign_id"]
                   for r in conn.execute("SELECT campaign_id, campaign_name FROM campaigns WHERE account_id=?", (acct,))}

        # Historical name->id fallback: campaigns renamed/retired since a CSV change was
        # logged won't match current names. The pre-restructure snapshot preserves the
        # immutable campaign_id under the OLD name, so we resolve id-natively instead of
        # guessing. (Confirmed live: TIER TWO->23496334752, TIER THREE->23530860942, both
        # renamed COMBINED shells -- current-name matching would swap them.)
        snap_name2id = {}
        snap = args.rename_snapshot
        if snap and C.table_exists(conn, snap):
            scols = {r["name"] for r in conn.execute("PRAGMA table_info(%s)" % snap)}
            if {"campaign_id", "campaign_name"}.issubset(scols):
                if "account_id" in scols:
                    rows_s = conn.execute("SELECT campaign_id, campaign_name FROM %s WHERE account_id=?" % snap, (acct,))
                else:
                    rows_s = conn.execute("SELECT campaign_id, campaign_name FROM %s" % snap)
                for r in rows_s:
                    snap_name2id.setdefault(r["campaign_name"], r["campaign_id"])
                print("[ok] snapshot fallback '%s' loaded (%d historical names)" % (snap, len(snap_name2id)))
            else:
                print("[WARN] snapshot '%s' lacks campaign_id/campaign_name; fallback disabled" % snap)
        elif snap:
            print("[WARN] snapshot '%s' not present; no historical name->id fallback" % snap)
        # budget_id / bidding_strategy_id -> campaign(s), for resolving API resources
        budget2camps = collections.defaultdict(list)
        strat2camps = collections.defaultdict(list)
        for r in conn.execute("SELECT campaign_id, budget_id, bidding_strategy_id FROM campaigns WHERE account_id=?", (acct,)):
            if r["budget_id"]:
                budget2camps[r["budget_id"]].append(r["campaign_id"])
            if r["bidding_strategy_id"]:
                strat2camps[r["bidding_strategy_id"]].append(r["campaign_id"])

        # Candidate events, cross-source deduped on (cid, day, type, old, new); API
        # is authoritative in overlap (id-native). Each value carries source + resolved_via.
        candidates = {}
        dedup_drops = 0

        def add(cid, day, ctype, old, new, source, resolved_via):
            nonlocal dedup_drops
            k = C.event_dedup_key(cid, day, ctype, old, new)
            cur = candidates.get(k)
            if cur is None:
                candidates[k] = {"cid": cid, "day": day, "type": ctype, "old": old, "new": new,
                                 "source": source, "resolved_via": resolved_via}
            elif source == "google_api" and cur["source"] != "google_api":
                candidates[k] = {"cid": cid, "day": day, "type": ctype, "old": old, "new": new,
                                 "source": source, "resolved_via": resolved_via}
                dedup_drops += 1   # API supersedes the CSV duplicate
            else:
                dedup_drops += 1   # same change already present

        # ---- source 1: CSV ledger (prose) ----
        csv_raw = conn.execute(
            "SELECT change_datetime, campaign_col, ad_group_col, changes_raw "
            "FROM google_change_csv_raw WHERE account_id=?", (acct,)).fetchall()
        skipped_adgroup = unresolved = csv_stmts = snap_stmts = 0
        unresolved_names = collections.Counter()
        for r in csv_raw:
            if (r["ad_group_col"] or "").strip():
                skipped_adgroup += 1
                continue
            events = C.parse_campaign_events(r["changes_raw"])
            if not events:
                continue
            name = r["campaign_col"] or ""
            cid = name2id.get(name)
            rv = "name_match_current"
            if cid is None:                      # renamed/retired -> historical snapshot id
                cid = snap_name2id.get(name)
                rv = "name_match_snapshot"
            if cid is None:
                unresolved += 1
                unresolved_names[name] += 1
                continue
            day = r["change_datetime"][:10]
            for ctype, old, new in events:
                add(cid, day, ctype, old, new, "google_ui_csv", rv)
                csv_stmts += 1
                if rv == "name_match_snapshot":
                    snap_stmts += 1

        # ---- source 2: API ledger (id-native) ----
        api_stmts = api_skipped = fanout_budget = fanout_strat = unresolved_res = 0
        if C.table_exists(conn, "google_change_event_raw"):
            for r in conn.execute(
                "SELECT change_datetime, change_resource_type, change_resource_name, changes_json "
                "FROM google_change_event_raw WHERE account_id=?", (acct,)):
                changes = {p: tuple(v) for p, v in json.loads(r["changes_json"]).items()}
                recs = C.normalize_api_record(r["change_resource_type"], r["change_resource_name"], changes)
                day = r["change_datetime"][:10]
                for rec in recs:
                    if rec["kind"] != "event":
                        continue  # naming -> handled by 16
                    scope, sid = rec["scope"], rec["scope_id"]
                    if scope == "campaign":
                        targets = [(sid, "campaign_native")]
                    elif scope == "budget":
                        cs = budget2camps.get(sid, [])
                        if len(cs) > 1:
                            fanout_budget += 1
                            print("[WARN] shared budget %s resolved to %d campaigns -- fan-out path activated "
                                  "(no shared budgets were expected today)" % (sid, len(cs)))
                            targets = [(c, "shared_budget_fanout") for c in cs]
                        else:
                            targets = [(c, "budget_native") for c in cs]
                    elif scope == "strategy":
                        cs = strat2camps.get(sid, [])
                        if len(cs) > 1:
                            fanout_strat += 1
                            targets = [(c, "portfolio_strategy_fanout") for c in cs]
                        else:
                            targets = [(c, "portfolio_strategy") for c in cs]
                    else:
                        targets = []
                    if not targets:
                        unresolved_res += 1
                        continue
                    for cid, rv in targets:
                        add(cid, day, rec["change_type"], rec["old"], rec["new"], "google_api", rv)
                        api_stmts += 1

        print("[ok] CSV: %d statements (%d via current names, %d via snapshot fallback; "
              "skipped %d ad-group rows, %d unresolved names) | "
              "API: %d statements (%d budget fan-outs, %d strategy fan-outs, %d unresolved resources)" % (
                  csv_stmts, csv_stmts - snap_stmts, snap_stmts, skipped_adgroup, unresolved,
                  api_stmts, fanout_budget, fanout_strat, unresolved_res))
        print("[ok] cross-source dedup removed %d duplicate candidate(s) (API authoritative)" % dedup_drops)
        if unresolved:
            print("[WARN] %d CSV statements had a campaign name not in current campaigns: %r" % (
                unresolved, dict(unresolved_names)))

        # collapse same-day walk-ups per (cid, day, type)
        groups = collections.defaultdict(list)
        for v in candidates.values():
            groups[(v["cid"], v["day"], v["type"])].append(v)

        out, ambiguous = [], 0
        derived_at = C.now_iso()
        for (cid, day, ctype), items in sorted(groups.items()):
            pairs = [(it["old"], it["new"]) for it in items]
            events, amb = C.collapse_same_day(pairs)
            if amb is not None:
                ambiguous += 1
                print("[WARN] ambiguous same-day chain skipped: %s %s %s starts=%s ends=%s" % (
                    cid, day, ctype, amb[0], amb[1]))
            api_items = [it for it in items if it["source"] == "google_api"]
            src = "google_api" if api_items else "google_ui_csv"
            rv = (api_items[0]["resolved_via"] if api_items else items[0]["resolved_via"])
            for old, new, cnt in events:
                out.append((C.PLATFORM, acct, cid, day, ctype, old, new, cnt,
                            rv, src, args.convention_version, derived_at))

        # report
        n_roas = sum(1 for e in out if e[4] == "TARGET_ROAS")
        n_bud = sum(1 for e in out if e[4] == "DAILY_BUDGET")
        n_anchor = sum(1 for e in out if e[5] is None)
        n_collapsed = sum(1 for e in out if e[7] > 1)
        n_api = sum(1 for e in out if e[9] == "google_api")
        print("[plan] %d net events: %d TARGET_ROAS (%d null-old anchors), %d DAILY_BUDGET | "
              "%d collapsed | %d from API, %d from CSV" % (
                  len(out), n_roas, n_anchor, n_bud, n_collapsed, n_api, len(out) - n_api))
        print("       distinct campaigns: %d | ambiguous chains skipped: %d" % (
            len(set(e[2] for e in out)), ambiguous))
        for e in sorted(out)[:10]:
            ov = "NULL" if e[5] is None else round(e[5], 4)
            print("       %s %-12s %s  %s -> %-7s (raw x%d) [%s/%s]" % (
                e[3], e[4], e[2], ov, round(e[6], 4), e[7], e[9], e[8]))

        CE_COLS = ["platform", "account_id", "campaign_id", "change_date", "change_type",
                   "old_value", "new_value", "raw_event_count", "resolved_via", "source",
                   "convention_version", "derived_at"]
        viol = C.check_rows_notnull(conn, "campaign_change_events", CE_COLS, out)
        if viol:
            for col, n in viol:
                msg = "not populated by this script" if n < 0 else "%d rows write NULL" % n
                print("[ABORT] live NOT NULL column campaign_change_events.%s: %s" % (col, msg))
            sys.exit(2)
        print("[ok] all rows satisfy live NOT NULL constraints (convention_version='%s')" % args.convention_version)

        if args.apply:
            bk = C.backup_table(conn, "campaign_change_events")
            print("[apply] backed up campaign_change_events -> %s" % bk)
            conn.execute("DELETE FROM campaign_change_events WHERE platform=? AND account_id=?",
                         (C.PLATFORM, acct))
            conn.executemany(
                "INSERT INTO campaign_change_events "
                "(platform, account_id, campaign_id, change_date, change_type, old_value, new_value, "
                " raw_event_count, resolved_via, source, convention_version, derived_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", out)
            conn.execute("COMMIT")
            print("[apply] wrote %d Google change events" % len(out))
            print("        before/after measurement should filter: old_value IS NOT NULL (drops timeline-start anchors)")
        else:
            print("\nDry run only. Re-run with --apply to write.")
    except Exception:
        if args.apply:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
