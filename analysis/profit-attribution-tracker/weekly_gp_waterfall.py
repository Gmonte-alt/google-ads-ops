#!/usr/bin/env python3
"""
weekly_gp_waterfall.py
======================
Profit Attribution Framework v1.2 -- Phase 3 flagship. The weekly GP
waterfall: GP by profit cell (keyword_category x state_tier x device) at
ISO-week grain, with a week-over-week bridge decomposed into RATE / VOLUME /
INTERACTION so the Friday narrative can say *why* GP moved.

DUMB-CONSUMER CONTRACT
----------------------
This script classifies NOTHING. It reads keyword_category_map_current (the
framework artifact) and keyword_performance_daily (via keyword_instances on
instance_id). If a number looks wrong, the fix belongs in the framework
artifacts or loaders -- never here.

DEVICE HANDLING
---------------
The cell includes device, so cell math uses DEVICE-SPLIT rows
(device != 'ALL'); mixing with 'ALL' rollups would double count. Each week's
split total is RECONCILED against the 'ALL' rollup and the gap reported;
a gap > RECON_TOLERANCE means device splits are incomplete.

WOW BRIDGE MATH (per cell, latest complete week t vs t-1)
---------------------------------------------------------
  gpl         = GP / leads (only when leads > 0)
  RATE        = (gpl_t - gpl_{t-1}) * leads_{t-1}
  VOLUME      = (leads_t - leads_{t-1}) * gpl_{t-1}
  INTERACTION = (gpl_t - gpl_{t-1}) * (leads_t - leads_{t-1})
  Zero leads in both weeks -> NO-LEAD SPEND. Present only in t -> NEW CELL.
  Present only in t-1 -> EXITED CELL.
Because primary_conv_value is uploaded actuals (partner economics included),
RPL changes surface as RATE -- a big negative RATE week is the cue to ask
the partner-economics question.

READ-ONLY (PRAGMA query_only=ON). Outputs two CSVs next to the script:
gp_by_cell_weekly_<end>.csv (dashboard input) and wow_bridge_<end>.csv.

USAGE
-----
    python weekly_gp_waterfall.py --db "C:\\Projects\\google-ads-ops\\db\\paid_search.db"
    python weekly_gp_waterfall.py --db "..." --weeks 12 --end-date 2026-07-12
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import sqlite3
import sys

import pandas as pd

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

ACCOUNT_ID = 1
DEFAULT_WEEKS = 8              # complete ISO weeks in the panel
RECON_TOLERANCE = 0.02         # 2% device-split vs ALL reconciliation gap
TOP_MOVERS = 12

PERF_TABLE = "keyword_performance_daily"
KI_TABLE = "keyword_instances"
MAP_VIEW = "keyword_category_map_current"

CELL_COLS = ["keyword_category", "state_tier", "device"]


def fail(msg: str, code: int = 1) -> None:
    print("\n[FAIL] " + msg)
    sys.exit(code)


def last_complete_sunday(end_date: dt.date) -> dt.date:
    """Most recent Sunday on or before end_date (ISO weeks end Sunday)."""
    return end_date - dt.timedelta(days=(end_date.isoweekday() % 7))


def main() -> None:
    ap = argparse.ArgumentParser(description="Weekly GP waterfall (read-only)")
    ap.add_argument("--db", required=True, help="Path to paid_search.db")
    ap.add_argument("--weeks", type=int, default=DEFAULT_WEEKS,
                    help=f"Complete ISO weeks in panel (default {DEFAULT_WEEKS})")
    ap.add_argument("--end-date", default=None,
                    help="YYYY-MM-DD; panel ends at the last complete ISO "
                         "week on/before this date (default: yesterday)")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        fail(f"DB not found at: {args.db}")
    conn = sqlite3.connect(args.db)
    conn.execute("PRAGMA query_only = ON")

    # --- Preflight ----------------------------------------------------------
    names = {n for (n,) in conn.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table','view')")}
    for req in (PERF_TABLE, KI_TABLE, MAP_VIEW):
        if req not in names:
            fail(f"'{req}' not found. If it's {MAP_VIEW}, run "
                 f"build_keyword_category_map.py --apply first.")
    n_map = conn.execute(
        f"SELECT COUNT(*) FROM {MAP_VIEW} WHERE account_id = ?",
        (ACCOUNT_ID,)).fetchone()[0]
    if n_map == 0:
        fail(f"{MAP_VIEW} has zero rows for account {ACCOUNT_ID} -- "
             f"apply the category map build first.")
    print(f"[OK] {MAP_VIEW}: {n_map:,} classified placement(s)")

    # --- Window: N complete ISO weeks ---------------------------------------
    raw_end = (dt.date.fromisoformat(args.end_date) if args.end_date
               else dt.date.today() - dt.timedelta(days=1))
    end = last_complete_sunday(raw_end)
    start = end - dt.timedelta(weeks=args.weeks) + dt.timedelta(days=1)
    print(f"Panel: {start} .. {end}  ({args.weeks} complete ISO weeks)")
    print(f"Account: {ACCOUNT_ID} | cell = category x tier x device\n")

    params = {"acct": ACCOUNT_ID, "start": start.isoformat(),
              "end": end.isoformat()}

    # --- Cell-grain pull (device-split rows only) ----------------------------
    sql = f"""
        SELECT
            perf.report_date                AS report_date,
            m.keyword_category              AS keyword_category,
            m.state_tier                    AS state_tier,
            perf.device                     AS device,
            SUM(perf.cost_micros) / 1e6     AS cost,
            SUM(perf.primary_conv_value)    AS revenue,
            SUM(perf.primary_conv_count)    AS leads
        FROM {PERF_TABLE} perf
        JOIN {KI_TABLE} ki
          ON ki.id = perf.instance_id
        JOIN {MAP_VIEW} m
          ON  m.ad_group_criterion_id = ki.ad_group_criterion_id
          AND m.account_id            = ki.account_id
        WHERE ki.account_id = :acct
          AND perf.device  != 'ALL'
          AND perf.report_date BETWEEN :start AND :end
        GROUP BY perf.report_date, m.keyword_category, m.state_tier,
                 perf.device
    """
    df = pd.read_sql_query(sql, conn, params=params)
    if df.empty:
        fail("Zero device-split rows in window. If the perf table only "
             "carries device='ALL' rollups, the device axis has no data -- "
             "flag to DB architect; interim: switch the pull to "
             "device='ALL' and treat device as a constant axis.")

    # --- Reconciliation vs 'ALL' rollups -------------------------------------
    recon = pd.read_sql_query(f"""
        SELECT perf.report_date AS report_date,
               SUM(perf.cost_micros)/1e6     AS cost_all,
               SUM(perf.primary_conv_value)  AS revenue_all,
               SUM(perf.primary_conv_count)  AS leads_all
        FROM {PERF_TABLE} perf
        JOIN {KI_TABLE} ki ON ki.id = perf.instance_id
        WHERE ki.account_id = :acct
          AND perf.device = 'ALL'
          AND perf.report_date BETWEEN :start AND :end
        GROUP BY perf.report_date
    """, conn, params=params)
    conn.close()

    df["report_date"] = pd.to_datetime(df["report_date"])
    iso = df["report_date"].dt.isocalendar()
    df["week"] = (iso["year"].astype(str) + "-W"
                  + iso["week"].astype(int).astype(str).str.zfill(2))

    if not recon.empty:
        recon["report_date"] = pd.to_datetime(recon["report_date"])
        r_iso = recon["report_date"].dt.isocalendar()
        recon["week"] = (r_iso["year"].astype(str) + "-W"
                         + r_iso["week"].astype(int).astype(str).str.zfill(2))
        split_w = df.groupby("week")[["cost", "revenue"]].sum()
        all_w = recon.groupby("week")[["cost_all", "revenue_all"]].sum()
        rc = split_w.join(all_w, how="outer").fillna(0.0)
        rc["gap_cost"] = (rc["cost"] - rc["cost_all"]).abs() / rc[
            "cost_all"].replace(0, pd.NA)
        worst = rc["gap_cost"].max()
        print("RECONCILIATION (device-split vs ALL rollup, weekly cost):")
        for wk, row in rc.iterrows():
            flag = ("  <-- GAP" if pd.notna(row["gap_cost"])
                    and row["gap_cost"] > RECON_TOLERANCE else "")
            gap = (f"{row['gap_cost']:.2%}" if pd.notna(row["gap_cost"])
                   else "n/a")
            print(f"  {wk}: split {row['cost']:>12,.0f} | ALL "
                  f"{row['cost_all']:>12,.0f} | gap {gap}{flag}")
        if pd.notna(worst) and worst > RECON_TOLERANCE:
            print(f"[WARN] Worst weekly gap {worst:.2%} exceeds "
                  f"{RECON_TOLERANCE:.0%} -- device splits may be "
                  f"incomplete; read device-level cells with caution.")
        print()

    # --- Weekly cell panel -----------------------------------------------------
    panel = (df.groupby(["week"] + CELL_COLS, as_index=False)
               [["cost", "revenue", "leads"]].sum())
    panel["gp"] = panel["revenue"] - panel["cost"]
    panel["gp_per_lead"] = panel["gp"] / panel["leads"].where(panel["leads"] > 0)

    weeks = sorted(panel["week"].unique())
    if len(weeks) < 2:
        fail("Fewer than two complete weeks in panel -- widen --weeks or "
             "check data coverage.")

    # --- Account trend -----------------------------------------------------------
    trend = panel.groupby("week")[["cost", "revenue", "leads", "gp"]].sum()
    print("=" * 78)
    print("WEEKLY ACCOUNT TREND (device-split basis)")
    print("=" * 78)
    for wk, row in trend.iterrows():
        gpl = row["gp"] / row["leads"] if row["leads"] else float("nan")
        print(f"  {wk}: GP {row['gp']:>11,.0f} | leads {row['leads']:>8,.0f}"
              f" | GP/lead {gpl:>7,.2f} | cost {row['cost']:>11,.0f}"
              f" | GP/day {row['gp']/7:>9,.0f}")

    # --- Unmapped visibility --------------------------------------------------------
    t_prev, t_cur = weeks[-2], weeks[-1]
    cur_week = panel[panel["week"] == t_cur]
    unm = cur_week[cur_week["keyword_category"] == "unmapped"]
    tot_gp_cur = cur_week["gp"].sum()
    print(f"\nUNMAPPED visibility ({t_cur}): GP {unm['gp'].sum():,.0f} "
          f"on cost {unm['cost'].sum():,.0f}"
          + (f" ({unm['gp'].sum()/tot_gp_cur:.1%} of GP)" if tot_gp_cur else "")
          + " -- if material, assign sizes / extend the map.")

    # --- WoW bridge -------------------------------------------------------------------
    a = panel[panel["week"] == t_prev].set_index(CELL_COLS)[["gp", "leads", "cost"]]
    b = panel[panel["week"] == t_cur].set_index(CELL_COLS)[["gp", "leads", "cost"]]
    cells = a.join(b, how="outer", lsuffix="_p", rsuffix="_c").fillna(0.0)

    rows = []
    for cell, r in cells.iterrows():
        gp_p, gp_c = r["gp_p"], r["gp_c"]
        ld_p, ld_c = r["leads_p"], r["leads_c"]
        d_gp = gp_c - gp_p
        rate = volume = inter = nolead = new = exited = 0.0
        in_p = (gp_p != 0) or (ld_p != 0) or (r["cost_p"] != 0)
        in_c = (gp_c != 0) or (ld_c != 0) or (r["cost_c"] != 0)
        if in_p and not in_c:
            exited = d_gp
        elif in_c and not in_p:
            new = d_gp
        elif ld_p > 0 and ld_c > 0:
            gpl_p, gpl_c = gp_p / ld_p, gp_c / ld_c
            rate = (gpl_c - gpl_p) * ld_p
            volume = (ld_c - ld_p) * gpl_p
            inter = (gpl_c - gpl_p) * (ld_c - ld_p)
        else:
            nolead = d_gp
        rows.append(dict(zip(CELL_COLS, cell)) | {
            "gp_prev": gp_p, "gp_cur": gp_c, "delta_gp": d_gp,
            "rate": rate, "volume": volume, "interaction": inter,
            "no_lead_spend": nolead, "new_cell": new, "exited_cell": exited})
    bridge = pd.DataFrame(rows).sort_values("delta_gp")

    comp_cols = ["rate", "volume", "interaction", "no_lead_spend",
                 "new_cell", "exited_cell"]
    totals = bridge[comp_cols + ["delta_gp"]].sum()

    print("\n" + "=" * 78)
    print(f"WOW BRIDGE  {t_prev} -> {t_cur}   "
          f"(account dGP {totals['delta_gp']:+,.0f})")
    print("=" * 78)
    for comp in comp_cols:
        if abs(totals[comp]) > 0.5:
            print(f"  {comp:<14} {totals[comp]:>+12,.0f}")
    resid = totals["delta_gp"] - sum(totals[c] for c in comp_cols)
    if abs(resid) > 1:
        print(f"  [WARN] bridge residual {resid:+,.2f} (should be ~0)")

    print("\n  by category:")
    for cat, row in (bridge.groupby("keyword_category")
                     [["delta_gp", "rate", "volume"]].sum()
                     .sort_values("delta_gp").iterrows()):
        print(f"    {cat:<22} dGP {row['delta_gp']:>+10,.0f}  "
              f"(rate {row['rate']:>+9,.0f} | vol {row['volume']:>+9,.0f})")
    print("\n  by tier:")
    for tier, row in (bridge.groupby("state_tier")
                      [["delta_gp", "rate", "volume"]].sum()
                      .sort_values("delta_gp").iterrows()):
        print(f"    {tier:<22} dGP {row['delta_gp']:>+10,.0f}  "
              f"(rate {row['rate']:>+9,.0f} | vol {row['volume']:>+9,.0f})")

    print(f"\n  top {TOP_MOVERS} mover cells:")
    movers = bridge.reindex(
        bridge["delta_gp"].abs().sort_values(ascending=False).index
    ).head(TOP_MOVERS)
    for _, r in movers.iterrows():
        cell = f"{r['keyword_category']} x {r['state_tier']} x {r['device']}"
        print(f"    {cell:<52} dGP {r['delta_gp']:>+9,.0f}  "
              f"(rate {r['rate']:>+8,.0f} | vol {r['volume']:>+8,.0f})")

    # --- Bleeding cells (v1: negative GP latest week; boundary comes in v1.3) ---
    bleed = cur_week[(cur_week["gp"] < 0)].sort_values("gp")
    if len(bleed):
        print(f"\n  BLEEDING cells in {t_cur} (GP < 0): {len(bleed)}, "
              f"total {bleed['gp'].sum():,.0f} on cost "
              f"{bleed['cost'].sum():,.0f}")
        for _, r in bleed.head(10).iterrows():
            print(f"    {r['keyword_category']} x {r['state_tier']} x "
                  f"{r['device']}: GP {r['gp']:,.0f} (cost {r['cost']:,.0f})")

    # --- Artifacts ------------------------------------------------------------------
    out_dir = os.path.dirname(os.path.abspath(__file__))
    panel_path = os.path.join(out_dir, f"gp_by_cell_weekly_{end}.csv")
    bridge_path = os.path.join(out_dir, f"wow_bridge_{end}.csv")
    panel.to_csv(panel_path, index=False)
    bridge.to_csv(bridge_path, index=False)
    print(f"\nArtifacts:\n  {panel_path}\n  {bridge_path}")
    print("\nThese two CSVs are the framework's consumer contract: the "
          "Friday narrative reads them this week; the dashboard reads the "
          "same files later.")


if __name__ == "__main__":
    main()