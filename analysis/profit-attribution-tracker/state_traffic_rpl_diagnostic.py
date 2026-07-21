#!/usr/bin/env python3
"""
state_traffic_rpl_diagnostic.py  (v3 -- geo_performance_daily, observed tROAS)
==============================================================================
Answers: for the named states, how much of the traffic decline does RPL
explain -- and how much is the tROAS / tier mechanism?

V3.1 (architect correction 2026-07-14): geo_performance_daily is the
MICROSOFT geo table (account 2 only). Google's geo source is
campaign_performance_geo_daily -- account 1, 2025-01-01 -> current. This
table has no that-day bidding columns (Microsoft-only), so epochs stay
date-based; tROAS(obs) simply won't print. Original v3 notes: runs on the
Google geo table, which
carries account_id (scope directly -- exception to the perf-table rule),
geo_state natively, device, and the that-day bidding state including
target_roas_that_day -- so each epoch reports the OBSERVED tROAS in force,
not an inference from calendar dates.

RUNTIME GUARDS (architect's three double-counting checks, automated):
  - location_type: if multiple attribution bases exist, one is chosen
    (prefers PRESENCE/PHYSICAL; override with --location-type) -- mixing
    bases double-counts.
  - device: if an 'ALL' rollup coexists with split rows, splits are used
    and 'ALL' excluded. Never both in one SUM.
  - network: same rule; network is then rolled up deliberately (the Google
    cell has no network axis).

REVENUE COLUMN -- EMPIRICALLY SELECTED AT RUNTIME:
  The account convention says all_conversion_value; the frozen GP formula's
  truth is primary_conv_value (offline upload = the primary action), which
  may match conversion_value instead. The script sums June 2026 for
  keyword-table primary value vs both geo columns and uses whichever is
  closer, printing all three so the choice is auditable. GP here stays
  consistent with the framework's GP.

EPOCHS: PRE (.. 2026-06-17) | BIDDOWN (2026-06-18..07-07) | CONSOL (07-08..)
Per state: daily-rate leads/cost/RPL/GP + observed tROAS per epoch;
PRE->CONSOL %d(leads) vs %d(RPL) and the UNEXPLAINED GAP; CPC trend; state x
device split. Reading: GAP ~0 -> RPL explains it. Big negative GAP + flat
CPC -> tROAS bid-back / tier demotion. GAP negative + CPC up -> competitive
pressure (the Texas signature).

READ-ONLY (PRAGMA query_only=ON).

USAGE
-----
    python state_traffic_rpl_diagnostic.py --db "C:\\Projects\\google-ads-ops\\db\\paid_search.db"
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
except Exception:
    pass

ACCOUNT_ID = 1
DEFAULT_STATES = "MA,CO,AZ,GA,IL,MI,NV,TX,VA"
BIDDOWN_DATE = dt.date(2026, 6, 18)
CONSOL_DATE = dt.date(2026, 7, 8)
RECON_TOLERANCE = 0.10
REV_CHECK_START, REV_CHECK_END = "2026-06-01", "2026-06-30"

GEO_TABLE = "campaign_performance_geo_daily"   # Google geo source (account 1); geo_performance_daily is Microsoft-only
G = {
    "account_id": "account_id",
    "campaign_id": "campaign_id",
    "date": "date",
    "state": "geo_state",
    "device": "device",
    "cost_micros": "cost_micros",
    "leads": "conversions",
    "rev_all": "all_conversion_value",
    "rev_primary": "conversion_value",
    "troas": "target_roas_that_day",
}
OPTIONAL_DIMS = ("network", "location_type")

TIER1 = {"UT", "OH", "AL", "MN", "WI", "OR", "DE"}
TIER2 = {"AZ", "GA", "NJ", "NC", "PA", "MD", "IL", "KS", "MO", "NM"}

NAME_TO_CODE = {
    "ALABAMA": "AL", "ALASKA": "AK", "ARIZONA": "AZ", "ARKANSAS": "AR",
    "CALIFORNIA": "CA", "COLORADO": "CO", "CONNECTICUT": "CT",
    "DELAWARE": "DE", "DISTRICT OF COLUMBIA": "DC", "FLORIDA": "FL",
    "GEORGIA": "GA", "HAWAII": "HI", "IDAHO": "ID", "ILLINOIS": "IL",
    "INDIANA": "IN", "IOWA": "IA", "KANSAS": "KS", "KENTUCKY": "KY",
    "LOUISIANA": "LA", "LOUISANA": "LA", "MAINE": "ME", "MARYLAND": "MD",
    "MASSACHUSETTS": "MA", "MICHIGAN": "MI", "MINNESOTA": "MN",
    "MISSISSIPPI": "MS", "MISSOURI": "MO", "MONTANA": "MT",
    "NEBRASKA": "NE", "NEVADA": "NV", "NEW HAMPSHIRE": "NH",
    "NEW JERSEY": "NJ", "NEW MEXICO": "NM", "NEW YORK": "NY",
    "NORTH CAROLINA": "NC", "NORTH DAKOTA": "ND", "OHIO": "OH",
    "OKLAHOMA": "OK", "OREGON": "OR", "PENNSYLVANIA": "PA",
    "RHODE ISLAND": "RI", "SOUTH CAROLINA": "SC", "SOUTH DAKOTA": "SD",
    "TENNESSEE": "TN", "TEXAS": "TX", "UTAH": "UT", "VERMONT": "VT",
    "VIRGINIA": "VA", "WASHINGTON": "WA", "WEST VIRGINIA": "WV",
    "WISCONSIN": "WI", "WYOMING": "WY",
}


def norm_state(v):
    if v is None:
        return None
    s = str(v).strip().upper().split(",")[0].strip()
    if len(s) == 2 and s.isalpha():
        return s
    return NAME_TO_CODE.get(s)


def tier_of(code):
    return ("tier1" if code in TIER1 else
            "tier2" if code in TIER2 else "tier3_consolidated")


def fail(msg):
    print("\n[FAIL] " + msg)
    sys.exit(1)


def pct(new, old):
    if old in (0, None) or pd.isna(old):
        return None
    return (new - old) / abs(old)


def fmt_pct(x):
    return "   n/a" if x is None or (isinstance(x, float) and pd.isna(x)) \
        else f"{x:+6.0%}"


def main():
    ap = argparse.ArgumentParser(
        description="State traffic-vs-RPL diagnostic (geo grain, v3)")
    ap.add_argument("--db", required=True)
    ap.add_argument("--states", default=DEFAULT_STATES)
    ap.add_argument("--weeks", type=int, default=12)
    ap.add_argument("--end-date", default=None)
    ap.add_argument("--location-type", default=None,
                    help="Override location_type filter value")
    args = ap.parse_args()

    want = [s.strip().upper() for s in args.states.split(",") if s.strip()]
    if not os.path.exists(args.db):
        fail(f"DB not found: {args.db}")
    conn = sqlite3.connect(args.db)
    conn.execute("PRAGMA query_only = ON")

    cols = [r[1] for r in conn.execute(f"PRAGMA table_info('{GEO_TABLE}')")]
    if not cols:
        fail(f"{GEO_TABLE} not found.")
    missing = [v for k, v in G.items() if v not in cols and k != "troas"]
    if missing:
        fail(f"{GEO_TABLE} missing {missing}. Actual columns: {cols}")
    has_troas = G["troas"] in cols
    clicks_c = next((c for c in ("clicks", "click_count") if c in cols), None)

    # ---------------- Guards: dimension rollup checks -----------------------
    print("=" * 78)
    print("DIMENSION GUARDS")
    print("=" * 78)
    filters = []

    devs = [str(r[0]) for r in conn.execute(
        f"SELECT DISTINCT {G['device']} FROM {GEO_TABLE} "
        f"WHERE {G['account_id']} = ?", (ACCOUNT_ID,))]
    dev_has_all = any(d.upper() == "ALL" for d in devs)
    dev_splits = [d for d in devs if d.upper() != "ALL"]
    device_mode = len(dev_splits) > 0
    if dev_has_all and device_mode:
        filters.append(f"UPPER(g.{G['device']}) != 'ALL'")
        print(f"  device: values={devs} -> 'ALL' rollup coexists with "
              f"splits; using splits, excluding 'ALL'")
    elif device_mode:
        print(f"  device: values={devs} -> splits only; no filter needed")
    else:
        print(f"  device: values={devs} -> no split rows; state grain only")

    for dim in OPTIONAL_DIMS:
        if dim not in cols:
            print(f"  {dim}: column not present")
            continue
        vals = [str(r[0]) for r in conn.execute(
            f"SELECT DISTINCT {dim} FROM {GEO_TABLE} "
            f"WHERE {G['account_id']} = ?", (ACCOUNT_ID,))]
        if dim == "location_type" and len(vals) > 1:
            choice = args.location_type or next(
                (v for v in vals if "PRESENCE" in v.upper()
                 or "PHYSICAL" in v.upper()), vals[0])
            filters.append(f"g.{dim} = '{choice}'")
            print(f"  {dim}: values={vals} -> multiple attribution bases; "
                  f"using '{choice}' (override with --location-type)")
        elif any(v.upper() in ("ALL", "MIXED") for v in vals) and len(vals) > 1:
            filters.append(f"UPPER(g.{dim}) NOT IN ('ALL','MIXED')")
            print(f"  {dim}: values={vals} -> rollup coexists with splits; "
                  f"excluding rollup, summing splits")
        else:
            print(f"  {dim}: values={vals} -> single basis; summed "
                  f"(rolled up deliberately)")
    guard_sql = ("AND " + " AND ".join(filters)) if filters else ""

    # ---------------- Revenue column: empirical selection --------------------
    print("\n" + "=" * 78)
    print("REVENUE COLUMN SELECTION (June 2026 reconciliation)")
    print("=" * 78)
    kpd = conn.execute("""
        SELECT SUM(p.primary_conv_value)
        FROM keyword_performance_daily p
        JOIN keyword_instances ki ON ki.id = p.instance_id
        WHERE ki.account_id = ? AND p.device = 'ALL'
          AND p.report_date BETWEEN ? AND ?""",
        (ACCOUNT_ID, REV_CHECK_START, REV_CHECK_END)).fetchone()[0] or 0.0
    geo_sums = {}
    for label, col in (("conversion_value", G["rev_primary"]),
                       ("all_conversion_value", G["rev_all"])):
        geo_sums[label] = conn.execute(f"""
            SELECT SUM(g.{col}) FROM {GEO_TABLE} g
            WHERE g.{G['account_id']} = ? {guard_sql}
              AND g.{G['date']} BETWEEN ? AND ?""",
            (ACCOUNT_ID, REV_CHECK_START, REV_CHECK_END)).fetchone()[0] or 0.0
    print(f"  keyword primary_conv_value : {kpd:>12,.0f}   (framework truth)")
    for label, v in geo_sums.items():
        diff = abs(v - kpd) / kpd if kpd else float("inf")
        print(f"  geo {label:<22}: {v:>12,.0f}   (diff {diff:.1%})")
    rev_label = min(geo_sums, key=lambda k: abs(geo_sums[k] - kpd))
    rev_col = G["rev_primary"] if rev_label == "conversion_value" else G["rev_all"]
    diff = abs(geo_sums[rev_label] - kpd) / kpd if kpd else 0
    print(f"  -> using '{rev_col}' (closest to framework GP basis"
          f"{'' if diff <= RECON_TOLERANCE else ' -- GAP LARGER THAN EXPECTED, flag to DB architect'})")

    # ---------------- Window + pull ------------------------------------------
    raw_end = (dt.date.fromisoformat(args.end_date) if args.end_date
               else dt.date.today() - dt.timedelta(days=1))
    end = raw_end - dt.timedelta(days=(raw_end.isoweekday() % 7))
    start = end - dt.timedelta(weeks=args.weeks) + dt.timedelta(days=1)
    print(f"\nWindow: {start} .. {end} | states: {', '.join(want)}\n")

    troas_sel = (f", SUM(g.{G['troas']} * g.{G['cost_micros']}) AS troas_wt, "
                 f"SUM(CASE WHEN g.{G['troas']} IS NOT NULL THEN "
                 f"g.{G['cost_micros']} ELSE 0 END) AS troas_cost"
                 if has_troas else "")
    clicks_sel = f", SUM(g.{clicks_c}) AS clicks" if clicks_c else ""
    dev_sel = f", g.{G['device']} AS device" if device_mode else ""
    dev_grp = f", g.{G['device']}" if device_mode else ""

    df = pd.read_sql_query(f"""
        SELECT g.{G['date']} AS report_date,
               g.{G['state']} AS geo_raw
               {dev_sel},
               SUM(g.{G['cost_micros']})/1e6 AS cost,
               SUM(g.{rev_col})              AS revenue,
               SUM(g.{G['leads']})           AS leads
               {clicks_sel} {troas_sel}
        FROM {GEO_TABLE} g
        WHERE g.{G['account_id']} = :acct {guard_sql}
          AND g.{G['date']} BETWEEN :start AND :end
        GROUP BY g.{G['date']}, g.{G['state']} {dev_grp}
    """, conn, params={"acct": ACCOUNT_ID, "start": start.isoformat(),
                       "end": end.isoformat()})
    conn.close()
    if df.empty:
        fail("Zero rows in window from geo_performance_daily.")

    df["state"] = df["geo_raw"].map(norm_state)
    unrec = sorted({str(v) for v in
                    df.loc[df['state'].isna(), 'geo_raw'].dropna().unique()})
    if unrec:
        print(f"[WARN] Unrecognized geo values (excluded): {unrec[:10]}")
    present = sorted(df["state"].dropna().unique())
    df = df[df["state"].isin(want)].copy()
    if df.empty:
        fail(f"No rows for requested states. Present: {present[:60]}")

    d = pd.to_datetime(df["report_date"]).dt.date
    df["epoch"] = "PRE"
    df.loc[d >= BIDDOWN_DATE, "epoch"] = "BIDDOWN"
    df.loc[d >= CONSOL_DATE, "epoch"] = "CONSOL"
    epoch_days = {"PRE": max((BIDDOWN_DATE - start).days, 1),
                  "BIDDOWN": max((CONSOL_DATE - BIDDOWN_DATE).days, 1),
                  "CONSOL": max((end - CONSOL_DATE).days + 1, 1)}

    agg = ["cost", "revenue", "leads"]
    if clicks_c:
        agg.append("clicks")
    if has_troas:
        agg += ["troas_wt", "troas_cost"]

    print("=" * 96)
    print("PER-STATE: daily-rate by epoch  (RPL = revenue/lead; GAP = "
          "%dLeads - %dRPL, PRE -> CONSOL)")
    print("=" * 96)
    out_rows = []
    for st in want:
        sub = df[df["state"] == st]
        if sub.empty:
            print(f"\n{st} [{tier_of(st)}]: NO DATA in window")
            continue
        e = (sub.groupby("epoch")[agg].sum()
             .reindex(["PRE", "BIDDOWN", "CONSOL"]).fillna(0.0))
        e["days"] = [epoch_days[i] for i in e.index]
        e["leads_d"] = e["leads"] / e["days"]
        e["cost_d"] = e["cost"] / e["days"]
        e["rpl"] = e["revenue"] / e["leads"].where(e["leads"] > 0)
        e["gp_d"] = (e["revenue"] - e["cost"]) / e["days"]
        if clicks_c:
            e["cpc"] = e["cost"] / e["clicks"].where(e["clicks"] > 0)
            e["clicks_d"] = e["clicks"] / e["days"]
        if has_troas:
            e["troas"] = e["troas_wt"] / e["troas_cost"].where(
                e["troas_cost"] > 0)

        pre, con = e.loc["PRE"], e.loc["CONSOL"]
        d_leads = pct(con["leads_d"], pre["leads_d"])
        d_rpl = pct(con["rpl"], pre["rpl"])
        gap = (d_leads - d_rpl if d_leads is not None and d_rpl is not None
               and not pd.isna(d_rpl) else None)
        d_cpc = pct(con.get("cpc"), pre.get("cpc")) if clicks_c else None

        print(f"\n{st}  [{tier_of(st)}]")
        for ep in ("PRE", "BIDDOWN", "CONSOL"):
            r = e.loc[ep]
            rpl_s = f"{r['rpl']:6.2f}" if pd.notna(r["rpl"]) else "   n/a"
            xtra = ""
            if clicks_c and pd.notna(r.get("cpc")):
                xtra += f" | CPC {r['cpc']:5.2f}"
            if has_troas and pd.notna(r.get("troas")):
                xtra += f" | tROAS(obs) {r['troas']:4.2f}"
            print(f"  {ep:<8} leads/day {r['leads_d']:6.2f} | RPL {rpl_s} | "
                  f"cost/day {r['cost_d']:8.2f} | GP/day "
                  f"{r['gp_d']:+8.2f}{xtra}")
        line = (f"  PRE->CONSOL: leads {fmt_pct(d_leads)} | RPL "
                f"{fmt_pct(d_rpl)} | GAP {fmt_pct(gap)}")
        if clicks_c:
            line += f" | CPC {fmt_pct(d_cpc)}"
        print(line)

        if device_mode:
            for dev in sorted(sub["device"].dropna().unique()):
                sd = (sub[sub["device"] == dev].groupby("epoch")
                      [["cost", "revenue", "leads"]].sum()
                      .reindex(["PRE", "CONSOL"]).fillna(0.0))
                lp = sd.loc["PRE", "leads"] / epoch_days["PRE"]
                lc = sd.loc["CONSOL", "leads"] / epoch_days["CONSOL"]
                rp = (sd.loc["PRE", "revenue"] / sd.loc["PRE", "leads"]
                      if sd.loc["PRE", "leads"] else None)
                rc = (sd.loc["CONSOL", "revenue"] / sd.loc["CONSOL", "leads"]
                      if sd.loc["CONSOL", "leads"] else None)
                print(f"    {str(dev):<8} leads/day {lp:6.2f} -> {lc:6.2f} "
                      f"({fmt_pct(pct(lc, lp))}) | RPL "
                      f"{(f'{rp:6.2f}' if rp else '  n/a')} -> "
                      f"{(f'{rc:6.2f}' if rc else '  n/a')}")

        row = {"state": st, "tier": tier_of(st),
               "leads_day_pre": pre["leads_d"],
               "leads_day_consol": con["leads_d"],
               "rpl_pre": pre["rpl"], "rpl_consol": con["rpl"],
               "gp_day_pre": pre["gp_d"], "gp_day_consol": con["gp_d"],
               "pct_leads": d_leads, "pct_rpl": d_rpl,
               "unexplained_gap": gap,
               "pct_cpc": d_cpc if clicks_c else None}
        if has_troas:
            row["troas_pre"] = pre.get("troas")
            row["troas_consol"] = con.get("troas")
        out_rows.append(row)

    out = pd.DataFrame(out_rows)
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        f"state_traffic_rpl_{end}.csv")
    out.to_csv(path, index=False)
    print(f"\nSummary CSV: {path}")
    print("\nReading guide: GAP ~0 -> RPL explains the decline. Large "
          "negative GAP + flat CPC -> tROAS bid-back / tier demotion "
          "(check tier tag + observed tROAS). GAP negative + CPC up -> "
          "competitive pressure (the Texas signature).")


if __name__ == "__main__":
    main()
