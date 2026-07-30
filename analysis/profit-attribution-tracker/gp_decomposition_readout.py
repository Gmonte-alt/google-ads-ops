#!/usr/bin/env python3
"""
gp_decomposition_readout.py
===========================
Profit Attribution Framework -- decomposition readout (Google, account 1).

One bridge engine, several lenses. For the latest complete period vs the
prior (ISO week or calendar month), decomposes dGP per group into FOUR
DRIVERS:

    GP = leads x (RPL - CPL)          RPL = revenue/lead, CPL = cost/lead
    VALUE       = dRPL x leads_prev          (what partners pay moved)
    EFFICIENCY  = -dCPL x leads_prev         (what we pay per lead moved)
    VOLUME      = dLeads x GP/lead_prev      (we bought more/less)
    INTERACTION = (dRPL - dCPL) x dLeads     (cross term)
    VALUE + EFFICIENCY + VOLUME + INTERACTION = dGP   (exact)

EFFICIENCY SUB-SPLIT (when clicks are available):
    CPL = CPC / CvR   (CPC = cost/clicks, CvR = leads/clicks)
    eff_cpc   = -(dCPC / CvR_prev) x leads_prev     (paying more per click)
    eff_cvr   = -CPC_prev x d(1/CvR) x leads_prev   (converting fewer clicks)
    eff_cross = remainder;  eff_cpc + eff_cvr + eff_cross = EFFICIENCY (exact)
    Different fixes: eff_cpc -> bidding/competition; eff_cvr -> query
    hygiene / landing page / mix.

Special cases (same as the waterfall): groups with zero leads in both
periods -> NO-LEAD SPEND; present only in current -> NEW; only in prior
-> EXITED.

LENSES
------
Keyword lens (perf -> keyword_instances -> keyword_category_map_current):
  1. Account aggregate
  2. keyword_category  (carrier_large / carrier_small / membership_affinity
                        / generic / aggregator_competitor / unmapped)
  3. keyword_category x device
  4. Campaign grid (campaign_id + name), sorted bleeders -> gainers
State lens (campaign_performance_geo_daily_search -- SIBLING view; Google never
crosses keyword x state, so these reconcile in total but do not join):
  5. State
  6. State x device

GRAIN: --grain week (default; latest complete ISO week vs prior) or
--grain month (latest complete calendar month vs prior complete month).
Note for month grain in Jul 2026: June->July straddles the re-tier dates
(6/25, 6/30, 7/9) and the 6/18 bid-down -- drivers blend event effects;
the readout prints the event dates as an annotation.

READ-ONLY (PRAGMA query_only=ON). CSVs written next to the script.

USAGE
-----
    python gp_decomposition_readout.py --db "C:\\Projects\\google-ads-ops\\db\\paid_search.db"
    python gp_decomposition_readout.py --db "..." --grain month
    python gp_decomposition_readout.py --db "..." --end-date 2026-07-12
"""

from __future__ import annotations

import argparse
import calendar
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
TOP_N_CAMPAIGNS = 15
TOP_N_STATES = 15
EVENT_DATES = ("bid-down 6/18", "Tier1 ease 6/25", "Tier2 6/30",
               "Tier3 consolidation 7/9 (5pm CST)")

GEO_TABLE = "campaign_performance_geo_daily_search"   # Google geo source
GEO_REV_COL = "conversion_value"   # empirically matched primary_conv_value
                                   # within 0.1% (June 2026 reconciliation)

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

DRIVERS = ["value", "efficiency", "volume", "interaction",
           "no_lead_spend", "new", "exited"]


def fail(msg):
    print("\n[FAIL] " + msg)
    sys.exit(1)


def norm_state(v):
    if v is None:
        return None
    s = str(v).strip().upper().split(",")[0].strip()
    if len(s) == 2 and s.isalpha():
        return s
    return NAME_TO_CODE.get(s)


# ---------------------------------------------------------------------------
# Period windows
# ---------------------------------------------------------------------------
def periods(grain: str, anchor: dt.date, window_days: int = 14):
    """Return (prev_start, prev_end, cur_start, cur_end, prev_label,
    cur_label) for the latest complete period on/before anchor."""
    if grain == "rolling":
        cur_end = anchor
        cur_start = cur_end - dt.timedelta(days=window_days - 1)
        prev_end = cur_start - dt.timedelta(days=1)
        prev_start = prev_end - dt.timedelta(days=window_days - 1)
        return prev_start, prev_end, cur_start, cur_end, \
            f"prior {window_days}d", f"last {window_days}d"
    if grain == "week":
        cur_end = anchor - dt.timedelta(days=(anchor.isoweekday() % 7))
        cur_start = cur_end - dt.timedelta(days=6)
        prev_end = cur_start - dt.timedelta(days=1)
        prev_start = prev_end - dt.timedelta(days=6)
        lab = lambda d: f"{d.isocalendar().year}-W{d.isocalendar().week:02d}"
        return prev_start, prev_end, cur_start, cur_end, \
            lab(cur_start - dt.timedelta(days=7)), lab(cur_start)
    # month: latest complete calendar month on/before anchor
    first_of_anchor = anchor.replace(day=1)
    cur_end = first_of_anchor - dt.timedelta(days=1)      # last complete
    cur_start = cur_end.replace(day=1)
    prev_end = cur_start - dt.timedelta(days=1)
    prev_start = prev_end.replace(day=1)
    lab = lambda d: d.strftime("%Y-%m")
    return prev_start, prev_end, cur_start, cur_end, \
        lab(prev_start), lab(cur_start)


# ---------------------------------------------------------------------------
# The bridge engine
# ---------------------------------------------------------------------------
def bridge(df: pd.DataFrame, keys: list[str]) -> pd.DataFrame:
    """df: columns keys + ['period'('prev'|'cur'), cost, revenue, leads]
    (+ optional 'clicks'). Returns per-group four-driver decomposition;
    drivers sum to delta_gp. With clicks, efficiency is sub-split into
    eff_cpc / eff_cvr / eff_cross (summing exactly to efficiency)."""
    has_clicks = "clicks" in df.columns
    metrics = ["cost", "revenue", "leads"] + (["clicks"] if has_clicks else [])
    g = df.groupby(keys + ["period"])[metrics].sum()
    g["gp"] = g["revenue"] - g["cost"]
    wide = g.unstack("period").fillna(0.0)
    # guarantee both period levels exist even if one period had zero rows
    for metric in tuple(metrics) + ("gp",):
        for per in ("prev", "cur"):
            if (metric, per) not in wide.columns:
                wide[(metric, per)] = 0.0
    if (wide[("cost", "prev")].sum() == 0 and
            wide[("revenue", "prev")].sum() == 0):
        print("[WARN] prior period has no data -- every group reads as "
              "NEW; check date coverage for the prior window.")
    rows = []
    for idx, r in wide.iterrows():
        key = idx if isinstance(idx, tuple) else (idx,)
        gp_p, gp_c = r[("gp", "prev")], r[("gp", "cur")]
        ld_p, ld_c = r[("leads", "prev")], r[("leads", "cur")]
        rv_p, rv_c = r[("revenue", "prev")], r[("revenue", "cur")]
        co_p, co_c = r[("cost", "prev")], r[("cost", "cur")]
        d = dict(zip(keys, key))
        d.update({k: 0.0 for k in DRIVERS})
        d.update({"gp_prev": gp_p, "gp_cur": gp_c, "delta_gp": gp_c - gp_p,
                  "leads_prev": ld_p, "leads_cur": ld_c,
                  "cost_prev": co_p, "cost_cur": co_c})
        in_p = (gp_p != 0) or (ld_p != 0) or (co_p != 0)
        in_c = (gp_c != 0) or (ld_c != 0) or (co_c != 0)
        if in_p and not in_c:
            d["exited"] = d["delta_gp"]
        elif in_c and not in_p:
            d["new"] = d["delta_gp"]
        elif ld_p > 0 and ld_c > 0:
            rpl_p, rpl_c = rv_p / ld_p, rv_c / ld_c
            cpl_p, cpl_c = co_p / ld_p, co_c / ld_c
            d["value"] = (rpl_c - rpl_p) * ld_p
            d["efficiency"] = -(cpl_c - cpl_p) * ld_p
            d["volume"] = (ld_c - ld_p) * (rpl_p - cpl_p)
            d["interaction"] = ((rpl_c - rpl_p) - (cpl_c - cpl_p)) * (ld_c - ld_p)
            d["rpl_prev"], d["rpl_cur"] = rpl_p, rpl_c
            d["cpl_prev"], d["cpl_cur"] = cpl_p, cpl_c
            if has_clicks:
                ck_p, ck_c = r[("clicks", "prev")], r[("clicks", "cur")]
                if ck_p > 0 and ck_c > 0:
                    cpc_p, cpc_c = co_p / ck_p, co_c / ck_c
                    icvr_p, icvr_c = ck_p / ld_p, ck_c / ld_c  # clicks/lead
                    d["eff_cpc"] = -(cpc_c - cpc_p) * icvr_p * ld_p
                    d["eff_cvr"] = -cpc_p * (icvr_c - icvr_p) * ld_p
                    d["eff_cross"] = d["efficiency"] - d["eff_cpc"] - d["eff_cvr"]
                    d["cpc_prev"], d["cpc_cur"] = cpc_p, cpc_c
                    d["cvr_prev"], d["cvr_cur"] = ld_p / ck_p, ld_c / ck_c
        else:
            d["no_lead_spend"] = d["delta_gp"]
        rows.append(d)
    out = pd.DataFrame(rows)
    if out.empty:
        return pd.DataFrame(columns=keys + DRIVERS +
                            ["gp_prev", "gp_cur", "delta_gp",
                             "leads_prev", "leads_cur",
                             "cost_prev", "cost_cur"])
    resid = (out["delta_gp"] - out[DRIVERS].sum(axis=1)).abs().max()
    if resid > 0.01:
        print(f"[WARN] bridge residual up to {resid:.4f} (should be ~0)")
    return out


def print_bridge(b: pd.DataFrame, keys: list[str], title: str,
                 top_n: int | None = None, sort_col: str = "delta_gp"):
    print("\n" + "=" * 92)
    print(title)
    print("=" * 92)
    sub_cols = [c for c in ("eff_cpc", "eff_cvr", "eff_cross")
                if c in b.columns]
    tot = b[DRIVERS + sub_cols + ["delta_gp", "gp_prev", "gp_cur"]].sum()
    comp = " | ".join(f"{k} {tot[k]:+,.0f}" for k in DRIVERS
                      if abs(tot[k]) > 0.5)
    print(f"TOTAL dGP {tot['delta_gp']:+,.0f}   "
          f"(GP {tot['gp_prev']:,.0f} -> {tot['gp_cur']:,.0f})")
    print(f"  drivers: {comp}")
    if sub_cols and abs(tot.get("efficiency", 0)) > 0.5:
        sub = " | ".join(f"{k.replace('eff_','')} {tot[k]:+,.0f}"
                         for k in sub_cols if abs(tot[k]) > 0.5)
        print(f"  efficiency split: {sub}   "
              f"(cpc = paying per click; cvr = converting clicks)")
    view = b.sort_values(sort_col)
    if top_n and len(view) > top_n:
        # bleeders first, then top gainers
        view = pd.concat([view.head(top_n // 2 + top_n % 2),
                          view.tail(top_n // 2)])
        print(f"  (showing worst {top_n//2 + top_n%2} and best "
              f"{top_n//2} of {len(b)} groups)")
    for _, r in view.iterrows():
        label = " x ".join(str(r[k]) for k in keys)
        parts = " | ".join(f"{k[:3]} {r[k]:+8,.0f}" for k in
                           ("value", "efficiency", "volume", "interaction")
                           if abs(r[k]) > 0.5)
        if "eff_cpc" in b.columns and abs(r.get("efficiency", 0)) > 0.5 \
                and pd.notna(r.get("eff_cpc")):
            parts += (f"  [eff: cpc {r['eff_cpc']:+,.0f} / "
                      f"cvr {r['eff_cvr']:+,.0f}]")
        special = "".join(f" [{k.upper()} {r[k]:+,.0f}]" for k in
                          ("no_lead_spend", "new", "exited")
                          if abs(r[k]) > 0.5)
        print(f"  {label:<48} dGP {r['delta_gp']:>+9,.0f}  "
              f"{parts}{special}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(
        description="GP decomposition readout (Google, read-only)")
    ap.add_argument("--db", required=True)
    ap.add_argument("--grain", choices=("week", "month", "rolling"),
                default="week")
    ap.add_argument("--window-days", type=int, default=14,
                    help="Rolling window length (used with --grain rolling; default 14)")
    ap.add_argument("--end-date", default=None,
                    help="YYYY-MM-DD anchor (default: yesterday)")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        fail(f"DB not found: {args.db}")
    conn = sqlite3.connect(args.db)
    conn.execute("PRAGMA query_only = ON")
    names = {n for (n,) in conn.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table','view')")}
    for req in ("keyword_performance_daily", "keyword_instances",
                "keyword_category_map_current", GEO_TABLE, "campaigns"):
        if req not in names:
            fail(f"'{req}' not found (category map applied? geo synced?)")

    anchor = (dt.date.fromisoformat(args.end_date) if args.end_date
              else dt.date.today() - dt.timedelta(days=1))
    p_s, p_e, c_s, c_e, lab_p, lab_c = periods(args.grain, anchor, args.window_days)
    print("=" * 92)
    print(f"GP DECOMPOSITION READOUT | grain={args.grain} | "
          f"{lab_p} ({p_s}..{p_e}) -> {lab_c} ({c_s}..{c_e})")
    print(f"Drivers: VALUE(dRPL) + EFFICIENCY(-dCPL) + VOLUME(dLeads) + "
          f"INTERACTION = dGP")
    if args.grain in ("month", "rolling"):
        print(f"[NOTE] Month grain straddles structural events: "
              f"{'; '.join(EVENT_DATES)} -- drivers blend event effects.")
    print("=" * 92)

    params = {"acct": ACCOUNT_ID, "ps": p_s.isoformat(), "pe": p_e.isoformat(),
              "cs": c_s.isoformat(), "ce": c_e.isoformat()}
    period_case = ("CASE WHEN {d} BETWEEN :ps AND :pe THEN 'prev' "
                   "ELSE 'cur' END")

    # ------------------ Keyword lens pull -----------------------------------
    map_cols = [r[1] for r in conn.execute(
        "PRAGMA table_info('keyword_category_map')")] or \
        [d[0] for d in (conn.execute(
            "SELECT * FROM keyword_category_map_current LIMIT 0"
        ).description or [])]
    has_mt = "match_type" in map_cols
    mt_sel = ", m.match_type" if has_mt else ", 'n/a' AS match_type"
    mt_grp = ", m.match_type" if has_mt else ""
    has_cb = "carrier_brand" in map_cols
    cb_sel = ", m.carrier_brand" if has_cb else ", NULL AS carrier_brand"
    cb_grp = ", m.carrier_brand" if has_cb else ""
    kpd_cols = [r[1] for r in conn.execute(
        "PRAGMA table_info('keyword_performance_daily')")]
    kw_clicks = ", SUM(perf.clicks) AS clicks" if "clicks" in kpd_cols else ""
    kw = pd.read_sql_query(f"""
        SELECT {period_case.format(d='perf.report_date')} AS period,
               m.keyword_category, m.carrier_size, m.state_tier
               {mt_sel} {cb_sel},
               perf.device,
               ki.campaign_id AS campaign_id,
               SUM(perf.cost_micros)/1e6    AS cost,
               SUM(perf.primary_conv_value) AS revenue,
               SUM(perf.primary_conv_count) AS leads
               {kw_clicks}
        FROM keyword_performance_daily perf
        JOIN keyword_instances ki ON ki.id = perf.instance_id
        JOIN keyword_category_map_current m
          ON  m.ad_group_criterion_id = ki.ad_group_criterion_id
          AND m.account_id            = ki.account_id
        WHERE ki.account_id = :acct
          AND perf.device != 'ALL'
          AND (perf.report_date BETWEEN :ps AND :pe
               OR perf.report_date BETWEEN :cs AND :ce)
        GROUP BY 1, m.keyword_category, m.carrier_size, m.state_tier
                 {mt_grp} {cb_grp}, perf.device, ki.campaign_id
    """, conn, params=params)
    if kw.empty:
        fail("Keyword lens returned zero rows for the two periods.")

    # names for the campaign grid
    camp_names = {str(r[0]): (r[1] or str(r[0])) for r in conn.execute(
        "SELECT campaign_id, campaign_name FROM campaigns "
        "WHERE account_id = ?", (ACCOUNT_ID,))} if \
        "campaign_name" in [r[1] for r in conn.execute(
            "PRAGMA table_info('campaigns')")] else {}

    # ------------------ State lens pull (sibling view) ----------------------
    geo = pd.read_sql_query(f"""
        SELECT {period_case.format(d='g.date')} AS period,
               g.geo_state, g.device,
               SUM(g.cost_micros)/1e6 AS cost,
               SUM(g.{GEO_REV_COL})   AS revenue,
               SUM(g.conversions)     AS leads,
               SUM(g.clicks)          AS clicks
        FROM {GEO_TABLE} g
        WHERE g.account_id = :acct
          AND (g.date BETWEEN :ps AND :pe OR g.date BETWEEN :cs AND :ce)
        GROUP BY 1, g.geo_state, g.device
    """, conn, params=params)
    conn.close()
    geo["state"] = geo["geo_state"].map(norm_state)
    geo = geo[geo["state"].notna()].copy()

    # ------------------ 1. Account aggregate --------------------------------
    kw["account"] = "Google Home (account 1)"
    print_bridge(bridge(kw, ["account"]), ["account"],
                 f"1. ACCOUNT AGGREGATE  ({lab_p} -> {lab_c}, keyword lens)")

    # ------------------ 2. Category -----------------------------------------
    b_cat = bridge(kw, ["keyword_category"])
    print_bridge(b_cat, ["keyword_category"],
                 "2. BY KEYWORD CATEGORY  (carrier sizing incl. membership)")

    # ------------------ 3. Category x device --------------------------------
    b_cd = bridge(kw, ["keyword_category", "device"])
    print_bridge(b_cd, ["keyword_category", "device"],
                 "3. CATEGORY x DEVICE", top_n=16)

    # ------------------ 3b/3c. Match type -----------------------------------
    if has_mt:
        b_mt = bridge(kw, ["match_type"])
        print_bridge(b_mt, ["match_type"],
                     "3b. BY MATCH TYPE  (post enum-fix; trustworthy)")
        b_cmt = bridge(kw, ["keyword_category", "match_type"])
        print_bridge(b_cmt, ["keyword_category", "match_type"],
                     "3c. CATEGORY x MATCH TYPE", top_n=16)
    else:
        b_mt = b_cmt = None
        print("\n[NOTE] match_type not in category map -- lenses 3b/3c "
              "skipped (re-apply the map build).")

    # ------------------ 3d-3f. Carrier lenses --------------------------------
    b_car = b_carm = b_carmd = None
    if has_cb:
        carr = kw[kw["carrier_brand"].notna()].copy()
        if not carr.empty:
            b_car = bridge(carr, ["carrier_brand"])
            print_bridge(b_car, ["carrier_brand"],
                         "3d. BY CARRIER  (carrier + membership terms only)",
                         top_n=20)
            if has_mt:
                b_carm = bridge(carr, ["carrier_brand", "match_type"])
                print_bridge(b_carm, ["carrier_brand", "match_type"],
                             "3e. CARRIER x MATCH TYPE", top_n=20)
                b_carmd = bridge(carr, ["carrier_brand", "match_type",
                                        "device"])
                print_bridge(b_carmd, ["carrier_brand", "match_type",
                                       "device"],
                             "3f. CARRIER x MATCH x DEVICE", top_n=20)
    else:
        print("\n[NOTE] carrier_brand not in category map -- carrier "
              "lenses skipped.")

    # ------------------ 4. Campaign grid ------------------------------------
    kw["campaign"] = kw["campaign_id"].astype(str).map(
        lambda c: camp_names.get(c, c))
    b_camp = bridge(kw, ["campaign"])
    print_bridge(b_camp, ["campaign"],
                 "4. CAMPAIGN GRID  (bleeders -> gainers)",
                 top_n=TOP_N_CAMPAIGNS)

    # ------------------ 5. State (sibling lens) ------------------------------
    geo_ok = not geo.empty and \
        (geo.groupby("period")["cost"].sum().reindex(
            ["prev", "cur"]).fillna(0) > 0).all()
    if not geo_ok:
        b_st = b_sd = None
        print("\n[NOTE] State lenses skipped: campaign_performance_geo_daily_search "
              "has no data for one/both windows (geo sync typically lags "
              "1-3 days; keyword lenses above are current).")
    if geo_ok:
        print("\n" + "-" * 92)
        print("STATE LENS -- sibling view on campaign-geo data. Reconciles "
              "with the keyword lens in total;\nGoogle provides no keyword x "
              "state cross, so lenses 5-6 do not join to lenses 1-4.")
        print("-" * 92)
        b_st = bridge(geo, ["state"])
        print_bridge(b_st, ["state"], "5. BY STATE", top_n=TOP_N_STATES)
        b_sd = bridge(geo, ["state", "device"])
        print_bridge(b_sd, ["state", "device"], "6. STATE x DEVICE",
                     top_n=16)

    # ------------------ Reconciliation + artifacts ---------------------------
    if b_st is not None:
        kw_tot = bridge(kw, ["account"]).iloc[0]
        st_tot = b_st[["gp_prev", "gp_cur", "delta_gp"]].sum()
        print("\n" + "=" * 92)
        print("LENS RECONCILIATION (keyword vs geo, same periods)")
        print("=" * 92)
        for lbl, a, b_ in (("GP prev", kw_tot["gp_prev"], st_tot["gp_prev"]),
                           ("GP cur", kw_tot["gp_cur"], st_tot["gp_cur"]),
                           ("dGP", kw_tot["delta_gp"], st_tot["delta_gp"])):
            gap = (abs(a - b_) / abs(a)) if a else 0
            print(f"  {lbl:<8} keyword {a:>11,.0f} | geo {b_:>11,.0f} | "
                  f"gap {gap:.1%}")
        print("  (small gaps are normal -- Google's geo report drops "
              "unattributable volume)")

    # ------------------ Carrier x Campaign pivot (current period) ------------
    piv = None
    if has_cb:
        cur_c = kw[(kw["period"] == "cur") & kw["carrier_brand"].notna()]
        if not cur_c.empty:
            cur_c = cur_c.copy()
            cur_c["campaign"] = cur_c["campaign_id"].astype(str).map(
                lambda c: camp_names.get(c, c))
            cur_c["gp"] = cur_c["revenue"] - cur_c["cost"]
            tot = (cur_c.groupby("carrier_brand")
                   [["cost", "revenue", "gp", "leads"]].sum()
                   .rename(columns={c: f"total_{c}" for c in
                                    ("cost", "revenue", "gp", "leads")}))
            gp_piv = cur_c.pivot_table(index="carrier_brand",
                                       columns="campaign", values="gp",
                                       aggfunc="sum")
            piv = tot.join(gp_piv).sort_values("total_gp", ascending=False)
            piv.index.name = "carrier_brand"
            print("\n" + "=" * 92)
            print(f"CARRIER PERFORMANCE x CAMPAIGN (GP, current period "
                  f"{lab_c}; totals = cost/revenue/gp/leads)")
            print("=" * 92)
            with pd.option_context("display.width", 250,
                                   "display.max_columns", 40):
                print(piv.head(20).round(0).fillna("").to_string())

    out_dir = os.path.dirname(os.path.abspath(__file__))
    stamp = (f"rolling{args.window_days}d_{c_e}" if args.grain == "rolling"
             else f"{args.grain}_{c_e}")
    frames = [("category", b_cat), ("category_device", b_cd),
              ("campaign", b_camp)]
    if b_st is not None:
        frames += [("state", b_st), ("state_device", b_sd)]
    if b_mt is not None:
        frames += [("match_type", b_mt), ("category_match", b_cmt)]
    if b_car is not None:
        frames += [("carrier", b_car)]
        if b_carm is not None:
            frames += [("carrier_match", b_carm),
                       ("carrier_match_device", b_carmd)]
    for name, frame in frames:
        frame.to_csv(os.path.join(
            out_dir, f"decomp_{name}_{stamp}.csv"), index=False)
    if piv is not None:
        piv.reset_index().to_csv(os.path.join(
            out_dir, f"carrier_campaign_pivot_{stamp}.csv"), index=False)
    print(f"\nArtifacts: decomp_<lens>_{stamp}.csv "
          f"({len(frames)} lens files"
          + (", + carrier_campaign_pivot" if piv is not None else "")
          + ") next to script.")
    print("Friday mapping: 'what went well' = top gainers by lens; 'what "
          "didn't' = bleeders; drivers name the mechanism.")


if __name__ == "__main__":
    main()
