#!/usr/bin/env python
"""
tier_flag_prediction.py — will the limited-by-budget flag clear on the tier campaigns
after their Home-Generic ad groups pause? (pre-launch, read-only)

Logic per campaign, per window: campaign TOTAL spend/day (campaign_performance_daily,
campaign truth) minus the Home-Generic cohort's spend/day (ad_group_performance_daily)
= the RESIDUAL NonGeneric run-rate that stays behind. Residual vs the campaign's
unchanged daily budget -> naive post-change utilization -> flag prediction.

The prediction is deliberately labeled NAIVE: it assumes NonGeneric demand holds at
its trailing rate. In practice Smart Bidding re-explores into freed budget, so
"CLEARS" is the optimistic bound; "PERSISTS" is robust.

Fill CURRENT_DAILY_BUDGETS below from the UI before running (TEXAS pre-filled at
$715). Run with --asof 2026-10-05 to stay consistent with the budget analysis run.

READ-ONLY. No account changes.
"""

import argparse
import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

DB_DEFAULT = r"C:\Projects\google-ads-ops\db\paid_search.db"
ACCOUNT_ID = 1

SOURCE_CAMPAIGNS = {
    23394808297: "TEXAS->Home-Generic",
    23496334752: "TIER1",
    23530860942: "TIER2",
    23586637678: "TIER3",
    23428389081: "TIER4",
}

# <<< EDIT ME: current daily budgets from the UI ($/day). None = prediction skipped,
# residuals still printed so you can eyeball against the UI number.
CURRENT_DAILY_BUDGETS = {
    23394808297: 715.0,   # TEXAS (confirmed by Gilbert 10/6)
    23496334752: None,    # TIER1
    23530860942: None,    # TIER2
    23586637678: None,    # TIER3
    23428389081: None,    # TIER4
}

NAME_PATTERN = "Home-Generic-%"
WINDOWS = [7, 14, 30]
LAST_N = 2  # post-mitigation slice (pauses 10/1, exclusions 10/2, target moves 10/1-2)

# naive post-change utilization (residual / budget) thresholds
TH_CLEAR, TH_BORDER = 0.85, 1.00

pd.set_option("display.width", 220)
pd.set_option("display.float_format", lambda v: f"{v:,.2f}")


def hr(t):
    print("\n" + "=" * 100 + f"\n{t}\n" + "=" * 100)


def abort(msg):
    print(f"\nABORT: {msg}\nSchema surprise -> DB architect thread before shipping.")
    sys.exit(1)


def cols_of(con, t):
    try:
        return [r[1] for r in con.execute(f"PRAGMA table_info({t})").fetchall()]
    except sqlite3.Error:
        return []


def pick(cols, cands, what, t):
    for c in cands:
        if c in cols:
            return c
    abort(f"could not resolve {what} on {t}; candidates {cands}, have {cols}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DB_DEFAULT)
    ap.add_argument("--asof", default=None)
    ap.add_argument("--outdir", default="output")
    args = ap.parse_args()
    asof = (datetime.strptime(args.asof, "%Y-%m-%d").date() if args.asof
            else date.today() - timedelta(days=1))
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    ids = tuple(SOURCE_CAMPAIGNS)
    ph = ",".join("?" * len(ids))

    # ---- resolve campaign_performance_daily (campaign truth; Search-only by design,
    #      and scoped to the 5 immutable Search campaign ids regardless)
    cpd = "campaign_performance_daily"
    cpd_cols = cols_of(con, cpd)
    if not cpd_cols:
        abort(f"{cpd} not found")
    cpd_date = pick(cpd_cols, ["date", "report_date"], "date column", cpd)
    cpd_cost = pick(cpd_cols, ["cost", "spend", "cost_micros"], "cost column", cpd)
    cpd_cost_expr = f"{cpd_cost}/1000000.0" if cpd_cost == "cost_micros" else cpd_cost
    cpd_dev = "device" in cpd_cols
    if cpd_dev:
        devs = [r[0] for r in con.execute(
            f"SELECT DISTINCT device FROM {cpd} WHERE CAST(campaign_id AS INTEGER) IN ({ph}) LIMIT 20",
            ids).fetchall()]
        if "ALL" not in devs:
            abort(f"{cpd} has a device column but no 'ALL' rollup (values {devs})")

    # ---- resolve agp (cohort side — same sourcing as the budget analysis)
    agp_cols = cols_of(con, "ad_group_performance_daily")
    agp_date = pick(agp_cols, ["report_date", "date"], "date column", "ad_group_performance_daily")
    agp_cost = pick(agp_cols, ["cost", "spend", "cost_micros"], "cost column", "ad_group_performance_daily")
    agp_cost_expr = f"p.{agp_cost}/1000000.0" if agp_cost == "cost_micros" else f"p.{agp_cost}"
    ag_cols = cols_of(con, "ad_groups")
    ag_name = pick(ag_cols, ["ad_group_name", "name"], "name column", "ad_groups")

    def spend_per_day(start, end):
        nd = (end - start).days + 1
        tot = pd.read_sql_query(
            f"""SELECT CAST(campaign_id AS INTEGER) AS campaign_id,
                       SUM({cpd_cost_expr}) AS total_spend
                FROM {cpd}
                WHERE CAST(campaign_id AS INTEGER) IN ({ph})
                  {"AND device='ALL'" if cpd_dev else ""}
                  AND {cpd_date} BETWEEN ? AND ?
                GROUP BY 1""",
            con, params=(*ids, start.isoformat(), end.isoformat()))
        coh = pd.read_sql_query(
            f"""SELECT CAST(ag.campaign_id AS INTEGER) AS campaign_id,
                       SUM({agp_cost_expr}) AS cohort_spend
                FROM ad_group_performance_daily p
                JOIN ad_groups ag ON ag.ad_group_id = p.ad_group_id AND ag.account_id = ?
                WHERE p.account_id = ? AND p.device = 'ALL'
                  AND ag.{ag_name} LIKE ?
                  AND CAST(ag.campaign_id AS INTEGER) IN ({ph})
                  AND p.{agp_date} BETWEEN ? AND ?
                GROUP BY 1""",
            con, params=(ACCOUNT_ID, ACCOUNT_ID, NAME_PATTERN, *ids,
                         start.isoformat(), end.isoformat()))
        m = tot.merge(coh, on="campaign_id", how="outer").fillna(0.0)
        m["total_spd"] = m.total_spend / nd
        m["cohort_spd"] = m.cohort_spend / nd
        m["residual_spd"] = m.total_spd - m.cohort_spd
        m["generic_share"] = m.cohort_spd / m.total_spd.replace(0, pd.NA)
        return m[["campaign_id", "total_spd", "cohort_spd", "residual_spd", "generic_share"]]

    frames = []
    for w in WINDOWS:
        f = spend_per_day(asof - timedelta(days=w - 1), asof)
        f["window"] = f"{w}d"
        frames.append(f)
    f = spend_per_day(asof - timedelta(days=LAST_N - 1), asof)
    f["window"] = f"LAST{LAST_N} (post-mitigation)"
    frames.append(f)
    out = pd.concat(frames, ignore_index=True)
    out["campaign"] = out.campaign_id.map(SOURCE_CAMPAIGNS)
    out["budget"] = out.campaign_id.map(CURRENT_DAILY_BUDGETS)
    out["util_now"] = out.total_spd / out.budget
    out["util_post"] = out.residual_spd / out.budget

    def verdict(r):
        if pd.isna(r.budget):
            return "SET BUDGET IN SCRIPT"
        if r.campaign_id == 23394808297:
            return "n/a — becomes the generic campaign"
        u = r.util_post
        if u >= TH_BORDER:
            return "PERSISTS (residual >= budget)"
        if u >= TH_CLEAR:
            return "BORDERLINE"
        return "CLEARS initially (naive) — expect re-expansion"

    out["flag_prediction"] = out.apply(verdict, axis=1)

    hr(f"PER-CAMPAIGN FLAG PREDICTION — windows end {asof} (read-only)")
    print("residual = campaign total minus Home-Generic cohort = what the unchanged budget\n"
        "must cover after the pauses. Sanity gate: generic_share must sit in (0,1); a value\n"
        ">1 means cohort grain exceeds campaign truth -> stop and route to the architect.\n")
    for w in out.window.unique():
        sub = out[out.window == w].sort_values("total_spd", ascending=False)
        print(f"--- {w} ---")
        print(sub[["campaign", "total_spd", "cohort_spd", "residual_spd", "generic_share",
                   "budget", "util_now", "util_post", "flag_prediction"]].to_string(index=False))
        print()
        bad = sub[(sub.generic_share > 1.0)]
        if not bad.empty:
            abort(f"generic_share > 1 on {bad.campaign.tolist()} in window {w}")

    print("Reading guide:")
    print(f"  util_post < {TH_CLEAR:.2f}  -> CLEARS initially; Smart Bidding re-explores brand into")
    print("                      freed budget, so expect the flag to creep back within days-weeks")
    print(f"  {TH_CLEAR:.2f}-{TH_BORDER:.2f}        -> BORDERLINE (flag flickers)")
    print(f"  >= {TH_BORDER:.2f}           -> PERSISTS")
    print("  TEXAS row: its residual = NonGeneric spend riding inside the repurposed campaign.")
    print("  If that residual is materially > 0, those groups' demand ALSO lives inside the new")
    print("  Home-Generic budget unless they pause/move — confirm their disposition in phase 1.")
    print("  AI Max residual (~1%) inflates generic_share denominator-side; immaterial here.")

    path = outdir / f"tier_flag_prediction_{asof.isoformat()}.csv"
    out.to_csv(path, index=False)
    print(f"\nCSV: {path}")
    con.close()


if __name__ == "__main__":
    main()
