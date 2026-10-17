"""
20_build_structure_era.py  -- account_structure_era ledger (SCD2).

Populates account_structure_era: one row per structural regime, boundaries DERIVED from the
change history (the week each campaign-name structure-suffix set changed in
google_change_csv_history_raw), NOT hand-typed guesses. Open interval (valid_to NULL) for the
current 2026 tier era.

Every change/perf row can then be stamped with the era in force at its date via
  date BETWEEN valid_from AND COALESCE(valid_to,'9999-12-31')
The critical field is troas_grain: 'ad_group' for all 2025 eras, 'campaign' for 2026 -- that's
what makes an intervention-count trend honest across the boundary (2025 ad-group tROAS sweeps
are NOT comparable to 2026 campaign-grain tROAS as raw counts).

channel_scope carries the Search-vs-PMax distinction forward (PMax test ran Feb-Oct 2025 as a
tagged concurrent sub-period; see pmax_note) so era-joined visuals can filter Search-only the
way the _search performance views do.

Boundaries evidenced by the weekly structure-suffix diagnostic:
  through 2025-02-02  SEARCH-MOBILE-USA-STE only (single mobile baseline)
  2025-02-03          +ALLDEVICE/DESKTOP/DESKTOP-GENERIC/MOBILE-GENERIC (device/generic build)
  2025-03-17          +SEARCH-MOBILE-CPA-STE (CPA bidding introduced)
  2025-05..2025-07    MOBILE-CPA + MOBILE stable (CPA duplex, ratio 2.0)
  2025-08             collapse to single SEARCH-MOBILE-USA-STE (~46 states, consolidation)
  2025-12-22          NB-SEARCH-ALL-USA-STE first appears (birth of the 2026 tier structure)

Usage:
    python 20_build_structure_era.py --db PATH            # dry-run (prints the ledger)
    python 20_build_structure_era.py --db PATH --apply
"""
import argparse
import sqlite3

TABLE = "account_structure_era"

# (era_key, valid_from, valid_to, campaign_grain, troas_grain, camps_per_state, channel_scope, notes)
ERAS = [
    ("2024_single_mobile", "2024-08-13", "2024-12-31", "state_single", "ad_group", "1",
     "search", "Same single-state SEARCH-MOBILE-USA-STE structure as early 2025, ad-group-grain "
     "tROAS, already in place at the start of change-history coverage. Evidence: 2024 grid rows "
     "(from 2024-08-13) are all SEARCH-MOBILE-USA-STE with ad-group tROAS. Coverage floor is "
     "Google's ~16-month UI retention, not a structural start; earlier 2024 predates the ledger."),
    ("2025_single_mobile", "2025-01-01", "2025-02-02", "state_single", "ad_group", "1",
     "search", "Baseline: one SEARCH-MOBILE-USA-STE campaign per state (50). Evidence: only "
     "MOBILE-USA suffix through wk 2025-01-27."),
    ("2025_device_generic_build", "2025-02-03", "2025-03-16", "state_variant", "ad_group", "3-4 (unstable)",
     "search", "Fragmented build: ALLDEVICE/DESKTOP/DESKTOP-GENERIC/MOBILE-GENERIC variants added "
     "alongside MOBILE. Evidence: suffix set jumps to 5 on wk 2025-02-03; ratio peaks ~3.94."),
    ("2025_cpa_introduced", "2025-03-17", "2025-04-30", "state_variant", "ad_group", "3 (settling)",
     "search", "SEARCH-MOBILE-CPA-STE introduced; device-generic splits retired. Evidence: CPA "
     "suffix first appears wk 2025-03-17; settles to ALLDEVICE+MOBILE-CPA+MOBILE by April."),
    ("2025_cpa_duplex", "2025-05-01", "2025-07-31", "state_duplex", "ad_group", "2.0",
     "search", "Stable two-campaigns-per-state (MOBILE-CPA + MOBILE). Evidence: ratio locks at "
     "2.0 May-Jul; suffix set is the CPA/MOBILE pair."),
    ("2025_consolidating_single", "2025-08-01", "2025-12-21", "state_single", "ad_group", "1 (~46 states)",
     "search", "Consolidation back to single SEARCH-MOBILE-USA-STE, ~46 states. Evidence: ratio "
     "collapses to 1.0 from wk 2025-08-04 onward."),
    ("2026_tier_structure", "2025-12-22", None, "tiered_campaign", "campaign", "tiered (RPL tiers)",
     "search+pmax", "Current era: NB-SEARCH-ALL / tier structure that becomes the 2026 account; "
     "tROAS managed at campaign/portfolio grain. Evidence: NB-SEARCH-ALL-USA-STE first appears "
     "wk 2025-12-22. PMax live in this era (new nonbrand PMax 2026-07)."),
]

PMAX_NOTE = ("PMax test campaign ran ~Feb-Oct 2025 CONCURRENT with the search eras above "
             "(paused, ~$188K spend); it is a channel overlay, not a structural era. Filter "
             "channel_scope / campaign_type for Search-only trends.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--account", type=int, default=1)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)

    print("=== account_structure_era to be written (account %d) ===" % args.account)
    print("%-28s %-11s %-11s %-16s %-10s %s" % (
        "era_key", "from", "to", "campaign_grain", "troas", "camps/state"))
    print("-" * 100)
    for e in ERAS:
        print("%-28s %-11s %-11s %-16s %-10s %s" % (
            e[0], e[1], e[2] or "(open)", e[3], e[4], e[5]))

    # contiguity check: each valid_from == prior valid_to + 1 day
    import datetime
    print("\n=== contiguity check ===")
    ok = True
    for i in range(1, len(ERAS)):
        prev_to = ERAS[i - 1][2]
        this_from = ERAS[i][1]
        expect = (datetime.date.fromisoformat(prev_to) + datetime.timedelta(days=1)).isoformat()
        status = "OK" if this_from == expect else "GAP/OVERLAP"
        if status != "OK":
            ok = False
        print("  %s -> %s : %s" % (ERAS[i - 1][0], ERAS[i][0], status))
    print("  overall:", "contiguous" if ok else "*** CHECK BOUNDARIES ***")

    if args.apply:
        conn.execute("DROP TABLE IF EXISTS %s" % TABLE)
        conn.execute(
            "CREATE TABLE %s ("
            " id INTEGER PRIMARY KEY, account_id INTEGER NOT NULL, era_key TEXT NOT NULL,"
            " valid_from TEXT NOT NULL, valid_to TEXT,"           # NULL = current
            " campaign_grain TEXT, troas_grain TEXT, camps_per_state TEXT,"
            " channel_scope TEXT, notes TEXT)" % TABLE)
        conn.executemany(
            "INSERT INTO %s (account_id, era_key, valid_from, valid_to, campaign_grain,"
            " troas_grain, camps_per_state, channel_scope, notes) VALUES (?,?,?,?,?,?,?,?,?)" % TABLE,
            [(args.account,) + e for e in ERAS])
        conn.execute("CREATE INDEX ix_%s_range ON %s (account_id, valid_from, valid_to)" % (TABLE, TABLE))
        # store the PMax overlay note as a row-less table comment via a meta row (era_key='_pmax_overlay')
        conn.execute(
            "INSERT INTO %s (account_id, era_key, valid_from, valid_to, channel_scope, notes) "
            "VALUES (?,?,?,?,?,?)" % TABLE,
            (args.account, "_pmax_overlay", "2025-02-01", "2025-10-31", "pmax", PMAX_NOTE))
        conn.commit()
        n = conn.execute("SELECT COUNT(*) FROM %s WHERE account_id=?" % TABLE, (args.account,)).fetchone()[0]
        print("\n[apply] wrote %d era rows (incl. 1 pmax overlay note) to %s" % (n, TABLE))
        print("[note] stamp any row with:  JOIN %s e ON <date> BETWEEN e.valid_from "
              "AND COALESCE(e.valid_to,'9999-12-31') AND e.era_key NOT LIKE '\\_%%' ESCAPE '\\'" % TABLE)
    else:
        print("\nDry run only. Re-run with --apply to write %s." % TABLE)
    conn.close()


if __name__ == "__main__":
    main()
