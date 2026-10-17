#!/usr/bin/env python
"""
generic_consolidation_budget.py — GENERIC CONSOLIDATION BUDGET ANALYSIS (pre-launch, read-only)

Sizes the opening daily budget for the nationwide Home-Generic campaign (TEXAS id
23394808297 repurposed) and reports trailing 7/14/30-day ROAS for the consolidating
cohort: all `Home-Generic-*` ad groups in the five source campaigns.

READ-ONLY. No account changes. Window choices, the confirmed cohort roster, and the
final budget number go back to Gilbert for ratification. The name-matched roster is
printed FIRST and must be confirmed against the rebuild thread's cohort tracker
before the cohort figures are treated as authoritative.

Pinned conventions (DB architect, 2026-10):
  - paid_search.db opened mode=ro
  - ad_group_performance_daily (agp): account_id=1, device='ALL'; names from
    ad_groups (key = ad_group_id + account_id); revenue = primary_conv_value,
    leads = primary_conv_count; ROAS = summed value / summed cost per cell
  - campaign_performance_geo_daily: account_id=1, NO 'ALL' device rollup -> SUM
    device splits; geo_state IS NOT NULL, full state names; date column = 'date'
  - campaigns resolved by immutable campaign_id only
  - AI Max residual: ad-group-grain sums run ~1% under campaign truth (structural)
  - Lost-IS: account sits below Google's 10% IS reporting floor — rank/budget-lost
    figures are censored and are NOT cited here

Usage:
  python generic_consolidation_budget.py [--db PATH] [--asof YYYY-MM-DD]
      [--rec-uplift 1.20] [--stretch-uplift 1.50] [--outdir output]
"""

import argparse
import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

# ----------------------------------------------------------------------------- pins
DB_DEFAULT = r"C:\Projects\google-ads-ops\db\paid_search.db"
ACCOUNT_ID = 1

SOURCE_CAMPAIGNS = {
    23394808297: "TEXAS->Home-Generic",
    23496334752: "TIER1",
    23530860942: "TIER2",
    23586637678: "TIER3",
    23428389081: "TIER4",
}
CARVEOUT_CAMPAIGNS = {
    23385188622: "CALIFORNIA (stays)",
    23394808300: "FLORIDA (stays)",
}
NAME_PATTERN = "Home-Generic-%"

# CA/FL groups aren't moving; MN/NM/AR were removed from targeting 10/2 and the
# nationwide geo list launches without them (45 states).
EXCLUDED_STATES = ["California", "Florida", "Minnesota", "New Mexico", "Arkansas"]

WINDOWS = [7, 14, 30]
LAG_TRIM_DAYS = 2          # conversions restate upward ~7d; trim view drops last 2 days
LAG_SENSITIVE_PCT = 0.10   # |ROAS_full - ROAS_trim| / ROAS_trim above this => LAG_SENSITIVE
PILOT_FIRST_SEEN = "2026-09-20"  # 9/23-24 themed pilot groups => SHORT_HISTORY annotation

# State-mix sensitivity window: 30d ending pre-surge/pre-exclusion, because the Oct
# geo exclusions (GEO-EXCL-SURGE-20261002) and surge pauses distort the recent mix.
MIX_SENS_END = "2026-09-25"

pd.set_option("display.width", 220)
pd.set_option("display.max_columns", 60)
pd.set_option("display.max_rows", 400)
pd.set_option("display.float_format", lambda v: f"{v:,.2f}")


def hr(title):
    print("\n" + "=" * 100)
    print(title)
    print("=" * 100)


def abort(msg):
    print(f"\nABORT: {msg}")
    print("Schema surprise / convention conflict -> route to the DB architect thread before shipping.")
    sys.exit(1)


def table_cols(con, table):
    try:
        return [r[1] for r in con.execute(f"PRAGMA table_info({table})").fetchall()]
    except sqlite3.Error:
        return []


def pick(cols, candidates, what, table):
    for c in candidates:
        if c in cols:
            return c
    abort(f"could not resolve {what} on {table}; candidates {candidates}, have {cols}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DB_DEFAULT)
    ap.add_argument("--asof", default=None, help="window end date (default: yesterday, local)")
    ap.add_argument("--rec-uplift", type=float, default=1.20)
    ap.add_argument("--stretch-uplift", type=float, default=1.50)
    ap.add_argument("--outdir", default="output")
    args = ap.parse_args()

    asof = (datetime.strptime(args.asof, "%Y-%m-%d").date() if args.asof
            else date.today() - timedelta(days=1))
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    stem = outdir / f"generic_consolidation_budget_{asof.isoformat()}"

    con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    src_ids = tuple(SOURCE_CAMPAIGNS)
    ph = ",".join("?" * len(src_ids))

    hr(f"GENERIC CONSOLIDATION BUDGET ANALYSIS — read-only — windows end {asof} "
       f"(account is MST; confirm {asof} is a complete serving day)")
    print("Source campaigns (immutable ids):")
    for cid, nm in SOURCE_CAMPAIGNS.items():
        print(f"  {cid}  {nm}")
    print("Carved out (not consolidating):", ", ".join(f"{k} {v}" for k, v in CARVEOUT_CAMPAIGNS.items()))

    # ------------------------------------------------------------------ schema resolve
    agp_cols = table_cols(con, "ad_group_performance_daily")
    if not agp_cols:
        abort("ad_group_performance_daily not found")
    agp_date = pick(agp_cols, ["report_date", "date"], "date column", "ad_group_performance_daily")
    agp_cost = pick(agp_cols, ["cost", "spend", "cost_micros"], "cost column", "ad_group_performance_daily")
    for need in ("account_id", "device", "ad_group_id", "primary_conv_value", "primary_conv_count"):
        if need not in agp_cols:
            abort(f"ad_group_performance_daily missing required column {need}")
    agp_campaign = "campaign_id" if "campaign_id" in agp_cols else None
    cost_expr_agp = f"p.{agp_cost}/1000000.0" if agp_cost == "cost_micros" else f"p.{agp_cost}"

    ag_cols = table_cols(con, "ad_groups")
    ag_name = pick(ag_cols, ["ad_group_name", "name"], "name column", "ad_groups")
    ag_status = next((c for c in ("status", "ad_group_status") if c in ag_cols), None)
    if "campaign_id" not in ag_cols or "ad_group_id" not in ag_cols or "account_id" not in ag_cols:
        abort(f"ad_groups missing campaign_id/ad_group_id/account_id; have {ag_cols}")

    geo_table = "campaign_performance_geo_daily"
    geo_cols = table_cols(con, geo_table)
    if not geo_cols:
        abort(f"{geo_table} not found")
    geo_date = pick(geo_cols, ["date", "report_date"], "date column", geo_table)
    geo_cost = pick(geo_cols, ["cost", "spend", "cost_micros"], "cost column", geo_table)
    cost_expr_geo = f"{geo_cost}/1000000.0" if geo_cost == "cost_micros" else geo_cost
    if "geo_state" not in geo_cols or "account_id" not in geo_cols or "campaign_id" not in geo_cols:
        abort(f"{geo_table} missing geo_state/account_id/campaign_id; have {geo_cols}")

    # grain gate: agp must carry an 'ALL' device rollup
    devs = [r[0] for r in con.execute(
        f"SELECT DISTINCT device FROM ad_group_performance_daily WHERE account_id=? LIMIT 20",
        (ACCOUNT_ID,)).fetchall()]
    if "ALL" not in devs:
        abort(f"agp device values {devs} contain no 'ALL' rollup — convention violated")

    # ------------------------------------------------------------- STEP 0: cohort roster
    hr("STEP 0 — NAME-MATCHED COHORT ROSTER (candidate set — CONFIRM WITH GILBERT / cohort "
       "tracker BEFORE treating as the cohort)")
    roster = pd.read_sql_query(
        f"""
        SELECT CAST(ag.campaign_id AS INTEGER) AS campaign_id,
               ag.ad_group_id,
               ag.{ag_name} AS ad_group_name
               {f', ag.{ag_status} AS status' if ag_status else ''}
        FROM ad_groups ag
        WHERE ag.account_id = ?
          AND CAST(ag.campaign_id AS INTEGER) IN ({ph})
          AND ag.{ag_name} LIKE ?
        """, con, params=(ACCOUNT_ID, *src_ids, NAME_PATTERN))
    if roster.empty:
        abort("name match returned zero ad groups — pattern or campaign scope wrong")
    roster["campaign"] = roster["campaign_id"].map(SOURCE_CAMPAIGNS)

    # tripwires: matches outside scope
    stray = con.execute(
        f"""SELECT COUNT(*) FROM ad_groups ag
            WHERE ag.account_id=? AND ag.{ag_name} LIKE ?
              AND CAST(ag.campaign_id AS INTEGER) NOT IN ({ph})""",
        (ACCOUNT_ID, NAME_PATTERN, *src_ids)).fetchone()[0]
    cafl = con.execute(
        f"""SELECT COUNT(*) FROM ad_groups ag
            WHERE ag.account_id=? AND ag.{ag_name} LIKE ?
              AND CAST(ag.campaign_id AS INTEGER) IN (?,?)""",
        (ACCOUNT_ID, NAME_PATTERN, *CARVEOUT_CAMPAIGNS)).fetchone()[0]
    ci_extra = con.execute(
        f"""SELECT COUNT(*) FROM ad_groups ag
            WHERE ag.account_id=? AND LOWER(ag.{ag_name}) LIKE 'home-generic-%'
              AND ag.{ag_name} NOT LIKE ? AND CAST(ag.campaign_id AS INTEGER) IN ({ph})""",
        (ACCOUNT_ID, NAME_PATTERN, *src_ids)).fetchone()[0]
    print(f"Tripwires: pattern matches outside the 5 source campaigns = {stray} "
          f"(of which in CA/FL = {cafl}, correctly excluded — not moving); "
          f"case-variant matches missed by exact-case LIKE inside scope = {ci_extra}")

    # per-group trailing figures + first-active date (pilot annotation)
    ids_ph = ",".join("?" * len(roster))
    gid = roster["ad_group_id"].tolist()
    d30, d7 = asof - timedelta(days=29), asof - timedelta(days=6)
    per_group = pd.read_sql_query(
        f"""
        SELECT p.ad_group_id,
               SUM(CASE WHEN p.{agp_date} >= ? THEN {cost_expr_agp} ELSE 0 END)            AS spend_30d,
               SUM(CASE WHEN p.{agp_date} >= ? THEN {cost_expr_agp} ELSE 0 END)            AS spend_7d,
               SUM(CASE WHEN p.{agp_date} >= ? THEN p.primary_conv_count ELSE 0 END)       AS leads_30d,
               SUM(CASE WHEN p.{agp_date} >= ? THEN p.primary_conv_value ELSE 0 END)       AS rev_30d,
               MIN(CASE WHEN p.impressions > 0 THEN p.{agp_date} END)                      AS first_active
        FROM ad_group_performance_daily p
        WHERE p.account_id = ? AND p.device = 'ALL'
          AND p.{agp_date} <= ? AND p.ad_group_id IN ({ids_ph})
        GROUP BY p.ad_group_id
        """, con,
        params=(d30.isoformat(), d7.isoformat(), d30.isoformat(), d30.isoformat(),
                ACCOUNT_ID, asof.isoformat(), *gid))
    roster = roster.merge(per_group, on="ad_group_id", how="left").fillna(
        {"spend_30d": 0, "spend_7d": 0, "leads_30d": 0, "rev_30d": 0})
    roster["roas_30d"] = roster["rev_30d"] / roster["spend_30d"].replace(0, pd.NA)
    roster["note"] = ""
    roster.loc[roster["first_active"].fillna("9999") >= PILOT_FIRST_SEEN, "note"] = \
        "SHORT_HISTORY (9/23-24 pilot — graduating member)"
    roster = roster.sort_values(["spend_30d"], ascending=False)
    cols_show = ["campaign", "ad_group_name"] + (["status"] if ag_status else []) + \
                ["spend_7d", "spend_30d", "leads_30d", "rev_30d", "roas_30d", "first_active", "note"]
    print(roster[cols_show].to_string(index=False))
    print(f"\nCandidate groups: {len(roster)}   |   30d cohort spend ${roster.spend_30d.sum():,.0f}"
          f"   |   >>> CONFIRM this roster against the rebuild thread's cohort tracker <<<")
    roster.to_csv(f"{stem}_roster.csv", index=False)

    # -------------------------------------------------- STEP 1+2: trailing windows x views
    hr("STEP 1/2 — COHORT TRAILING WINDOWS (FULL vs TRIM2 = same window minus last "
       f"{LAG_TRIM_DAYS} days; conversions restate upward ~7d)")
    print("Window context: 7d sits ENTIRELY inside the late-Sept surge (onset ~9/26, targets "
          "moved 10/1–10/2); 14d mostly inside it; 30d mixes regimes. No single window is "
          "'the' answer — all three ship side by side.")

    def window_agg(start, end, label, view):
        q = pd.read_sql_query(
            f"""
            SELECT CAST(ag.campaign_id AS INTEGER) AS campaign_id,
                   SUM({cost_expr_agp})          AS spend,
                   SUM(p.primary_conv_value)     AS revenue,
                   SUM(p.primary_conv_count)     AS leads
            FROM ad_group_performance_daily p
            JOIN ad_groups ag ON ag.ad_group_id = p.ad_group_id AND ag.account_id = ?
            WHERE p.account_id = ? AND p.device = 'ALL'
              AND p.{agp_date} BETWEEN ? AND ?
              AND p.ad_group_id IN ({ids_ph})
            GROUP BY 1
            """, con, params=(ACCOUNT_ID, ACCOUNT_ID, start.isoformat(), end.isoformat(), *gid))
        q["campaign"] = q["campaign_id"].map(SOURCE_CAMPAIGNS)
        tot = pd.DataFrame([{"campaign_id": 0, "campaign": "TOTAL COHORT",
                             "spend": q.spend.sum(), "revenue": q.revenue.sum(),
                             "leads": q.leads.sum()}])
        q = pd.concat([tot, q.sort_values("spend", ascending=False)], ignore_index=True)
        ndays = (end - start).days + 1
        q["window"], q["view"], q["days"] = label, view, ndays
        q["spend_per_day"] = q.spend / ndays
        q["roas"] = q.revenue / q.spend.replace(0, pd.NA)
        q["cpl"] = q.spend / q.leads.replace(0, pd.NA)
        return q

    frames = []
    for w in WINDOWS:
        start = asof - timedelta(days=w - 1)
        frames.append(window_agg(start, asof, f"{w}d", "FULL"))
        frames.append(window_agg(start, asof - timedelta(days=LAG_TRIM_DAYS), f"{w}d", "TRIM2"))
    win = pd.concat(frames, ignore_index=True)

    # LAG_SENSITIVE flag per (window, campaign)
    piv = win.pivot_table(index=["window", "campaign"], columns="view", values="roas")
    piv["lag_flag"] = ""
    sens = (piv["FULL"] - piv["TRIM2"]).abs() / piv["TRIM2"].replace(0, pd.NA) > LAG_SENSITIVE_PCT
    piv.loc[sens.fillna(False), "lag_flag"] = "LAG_SENSITIVE"
    win = win.merge(piv["lag_flag"].reset_index(), on=["window", "campaign"], how="left")

    for w in WINDOWS:
        sub = win[win.window == f"{w}d"]
        print(f"\n--- {w}-day window ---")
        print(sub[["view", "campaign", "days", "spend", "spend_per_day", "revenue",
                   "leads", "roas", "cpl", "lag_flag"]].to_string(index=False))
    win.to_csv(f"{stem}_windows.csv", index=False)
    print("\nAI MAX RESIDUAL (noted once): cohort sums at ad-group grain run ~1% under campaign "
          "truth (wider during expansions) — structural; do NOT reverse-engineer the budget to "
          "close that gap.")

    # ------------------------------------------------- STEP 3: state-mix approximation
    hr("STEP 3 — STATE-MIX APPROXIMATION (declared approximation — geo grain is campaign×state "
       "only; NO ad-group×geo exists in the DB)")
    print("Method: each source campaign's state spend mix is applied to that campaign's cohort "
          "spend; the CA/FL/MN/NM/AR portions are subtracted. Per-state figures here are NOT "
          "measured ad-group geography — do not quote them as such.")

    def state_mix(end_d, tag):
        start_d = end_d - timedelta(days=29)
        g = pd.read_sql_query(
            f"""
            SELECT CAST(campaign_id AS INTEGER) AS campaign_id, geo_state,
                   SUM({cost_expr_geo}) AS spend
            FROM {geo_table}
            WHERE account_id = ? AND geo_state IS NOT NULL
              AND CAST(campaign_id AS INTEGER) IN ({ph})
              AND {geo_date} BETWEEN ? AND ?
            GROUP BY 1, 2
            """, con, params=(ACCOUNT_ID, *src_ids, start_d.isoformat(), end_d.isoformat()))
        if g.empty:
            abort(f"geo pull empty for {tag} window {start_d}..{end_d}")
        g["total"] = g.groupby("campaign_id")["spend"].transform("sum")
        g["share"] = g.spend / g.total
        excl = (g[g.geo_state.isin(EXCLUDED_STATES)]
                .groupby("campaign_id")["share"].sum()
                .reindex(src_ids).fillna(0.0))
        fac = (1 - excl).rename("serving_factor_45")
        out = pd.concat([excl.rename("excluded_state_share"), fac], axis=1)
        out.index = [SOURCE_CAMPAIGNS[i] for i in out.index]
        print(f"\n[{tag}] 30d geo window {start_d}..{end_d} — per-campaign excluded-state share "
              f"(CA+FL+MN+NM+AR) and 45-state serving factor:")
        print(out.to_string())
        return fac.to_dict(), g

    fac_now, g_now = state_mix(asof, "CURRENT (includes Oct surge exclusions — mix distorted)")
    fac_pre, _ = state_mix(datetime.strptime(MIX_SENS_END, "%Y-%m-%d").date(),
                           "PRE-SURGE sensitivity (ends 9/25, before exclusions/pauses)")
    g_now.to_csv(f"{stem}_statemix.csv", index=False)

    def spd45(window_label, view, fac):
        sub = win[(win.window == window_label) & (win.view == view) & (win.campaign_id != 0)]
        return sum(r.spend_per_day * fac.get(r.campaign_id, 1.0)
                   for r in sub.itertuples())

    hr("45-STATE COHORT SPEND/DAY (cohort spend × per-campaign serving factor)")
    rows = []
    for w in WINDOWS:
        rows.append({"window": f"{w}d",
                     "spd_45_current_mix": spd45(f"{w}d", "FULL", fac_now),
                     "spd_45_presurge_mix": spd45(f"{w}d", "FULL", fac_pre)})
    spd = pd.DataFrame(rows)
    print(spd.to_string(index=False))

    # ------------------------------------------------- STEP 4: budget construction
    hr("STEP 4 — BUDGET RECOMMENDATION (floor / recommended / stretch) — FOR RATIFICATION")
    floor_30 = spd.loc[spd.window == "30d"].iloc[0]
    floor_7 = spd.loc[spd.window == "7d"].iloc[0]
    floor_14 = spd.loc[spd.window == "14d"].iloc[0]
    # 30d two-view, pre-surge mix = the honest "at rest" anchor (architect note 3)
    anchor = floor_30["spd_45_presurge_mix"]
    rec = anchor * args.rec_uplift
    stretch = anchor * args.stretch_uplift
    print(f"Floor candidates (45-state cohort spend/day):")
    print(f"  7d  current/pre-surge mix: ${floor_7.spd_45_current_mix:,.0f} / ${floor_7.spd_45_presurge_mix:,.0f}   (surge-depressed — context only)")
    print(f"  14d current/pre-surge mix: ${floor_14.spd_45_current_mix:,.0f} / ${floor_14.spd_45_presurge_mix:,.0f}")
    print(f"  30d current/pre-surge mix: ${floor_30.spd_45_current_mix:,.0f} / ${floor_30.spd_45_presurge_mix:,.0f}   <- anchor")
    print(f"\n  FLOOR       ${anchor:,.0f}/day   = 30d 45-state cohort spend/day (pre-surge mix)")
    print(f"  RECOMMENDED ${rec:,.0f}/day   = floor × {args.rec_uplift:.2f}")
    print(f"  STRETCH     ${stretch:,.0f}/day   = floor × {args.stretch_uplift:.2f}")
    print("\nReasoning per tier (uplift multipliers are PARAMETERS, not measurements — ratify):")
    print("  (a) Trailing spend is CAPPED demand: five of seven source campaigns carry the")
    print("      limited-by-budget flag. Lost-IS CANNOT quantify the gap — this account sits")
    print("      below Google's 10% IS reporting floor; rank/budget-lost figures are censored.")
    print("  (b) Consolidation typically unlocks spend as Smart Bidding re-explores; the")
    print("      repurposed TEXAS id also carries TX-only bidding history into a nationwide")
    print("      context -> expect 1-2 relearning weeks; conservative early budget is prudent.")
    print("  Overdelivery exposure (Google may spend ~2x daily budget on a given day):")
    for lbl, v in (("floor", anchor), ("recommended", rec), ("stretch", stretch)):
        print(f"      {lbl:<12} up to ~${v*2:,.0f} on a single day")
    pd.DataFrame([{"floor": anchor, "recommended": rec, "stretch": stretch,
                   "rec_uplift": args.rec_uplift, "stretch_uplift": args.stretch_uplift,
                   "anchor_basis": "30d, 45-state, pre-surge mix"}]).to_csv(
        f"{stem}_budget.csv", index=False)

    hr("DONE — read-only; nothing changed in the account")
    print("Back to Gilbert for ratification: (1) cohort roster vs the rebuild-thread tracker,")
    print("(2) window choice / treatment of surge-depressed reads, (3) the final budget number.")
    print(f"CSVs written with prefix: {stem}_*.csv")
    con.close()


if __name__ == "__main__":
    main()
