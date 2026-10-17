#!/usr/bin/env python3
"""
daily_pulse.py — v1 daily performance readout, Google Home (account_id=1)

Answers two questions every morning:
  1. "What changed yesterday?"   -> latest day vs day-of-week baseline
     (median of the prior 4 same weekdays), account + campaign + carrier.
  2. "How is this week going?"   -> week-to-date (Mon..latest) vs the same
     partial window last week.

Data is used as-is (no offline-lag adjustment) per convention.
All grains derive from ONE daily ad-group-level pull so numbers reconcile.

Usage:
  python daily_pulse.py                # run against DB_PATH below
  python daily_pulse.py --demo         # synthetic data, verify output format
  python daily_pulse.py --csv file.csv # run from a CSV export instead of the DB
  python daily_pulse.py --date 2026-08-18   # pretend "latest day" is this date

CSV / query contract (one row per date x campaign x ad_group):
  date, campaign, ad_group, cost, clicks, leads, revenue
"""

import argparse
import re
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

# ----------------------------------------------------------------------------
# CONFIG — adjust these to your environment
# ----------------------------------------------------------------------------
DB_PATH = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")  # default; override with --db
ACCOUNT_ID = 1                             # Google Home

# Column auto-detection: load_db() introspects the three tables and picks the
# first matching name from each candidate list. *_micros columns are /1e6.
TABLES = {"perf": "ad_group_performance_daily", "adg": "ad_groups",
          "camp": "campaigns"}
CANDIDATES = {
    "date":      ["date", "report_date", "day", "segments_date"],
    "cost":      ["cost", "spend", "cost_micros", "total_cost"],
    "clicks":    ["clicks", "total_clicks"],
    "leads":     ["primary_conv_count", "primary_conversions", "primary_convs",
                  "primary_leads", "leads", "primary_conv"],
    "revenue":   ["primary_conv_value", "primary_conversion_value",
                  "primary_convs_value", "primary_value", "revenue"],
    "adg_name":  ["ad_group_name", "name", "adgroup_name"],
    "camp_name": ["campaign_name", "name"],
    "adg_fk":    ["ad_group_id", "adgroup_id"],
    "camp_fk":   ["campaign_id"],
    "acct":      ["account_id", "customer_id", "account"],
    "imps":      ["impressions", "impr"],
    "impr_share": ["search_impression_share", "impression_share"],
}

LOOKBACK_DAYS = 42          # history pulled; needs >= 4 prior same weekdays
BASELINE_WEEKS = 4          # median over the last N same weekdays
BASELINE_MIN_DATE = "2026-07-30"  # don't baseline across the 7/29 re-tier cutover
GP_FLAG_DOLLARS = 150       # |GP deviation| that makes the movers list
COST_FLAG_PCT = 0.25        # |cost deviation| pct that makes the movers list
MIN_BASELINE_COST = 25      # ignore cells that normally spend less than this/day
TOP_N_MOVERS = 8            # per grain

CHANGE_LOG_CSV = None       # optional: path to CSV with columns date,scope,change
                            # last 7 days of entries get printed with the report

# Where reports land: a daily-pulse subfolder inside the tracker's output dir.
# One dated .txt per run; latest.txt is always overwritten with the newest.
OUTPUT_DIR = Path(r"C:\Projects\google-ads-ops\analysis\profit-attribution-tracker\output") / "daily-pulse"

# Carrier extraction from ad group names (v1: name-pattern mapping).
CARRIER_PATTERNS = [
    ("Geico", r"geico"), ("Farmers", r"farmers"), ("Hartford", r"hartford"),
    ("Kin Insurance", r"\bkin\b"), ("Travelers", r"travelers"),
    ("Amica", r"amica"), ("AAA", r"\baaa\b"), ("State Farm", r"state.?farm"),
    ("Allstate", r"allstate"), ("Progressive", r"progressive"),
    ("American Family", r"american.?family|amfam"),
    ("Plymouth Rock", r"plymouth"), ("NerdWallet", r"nerdwallet"),
    ("Policygenius", r"policygenius"), ("Bamboo", r"bamboo"),
    ("Costco", r"costco"), ("American Home", r"american.?home"),
    ("Universal Property", r"universal"), ("Allied", r"allied"),
    ("21st Century", r"21st"), ("CSAA Insurance", r"csaa"),
    ("Wawanesa", r"wawanesa"), ("USAA", r"\busaa\b"),
    ("Liberty Mutual", r"liberty"), ("Nationwide", r"nationwide"),
    ("Lemonade", r"lemonade"), ("Hippo", r"hippo"),
]
CARRIER_RX = [(name, re.compile(pat, re.I)) for name, pat in CARRIER_PATTERNS]


def carrier_of(ad_group: str) -> str:
    for name, rx in CARRIER_RX:
        if rx.search(ad_group or ""):
            return name
    return "(non-carrier)"


def short_campaign(name: str) -> str:
    s = (name or "")
    s = re.sub(r"HOME INSURANCE[-_]?", "", s, flags=re.I)
    s = re.sub(r"STATE-GROUP_", "", s)
    s = re.sub(r"RPL\d+Plus_?", "", s)
    s = re.sub(r"_?COMBINED_TROAS_SEARCH_ALLDEVICE_USA-STE", "", s)
    s = re.sub(r"-NB-SEARCH-ALL-USA-STE", "", s)
    return s.strip("_- ") or name


# ----------------------------------------------------------------------------
# Loaders
# ----------------------------------------------------------------------------
def table_cols(con, table):
    return [r[1] for r in con.execute(f"PRAGMA table_info({table})").fetchall()]


def pick(cols, key, required=True, ctx=""):
    for cand in CANDIDATES[key]:
        if cand in cols:
            return cand
    if required:
        sys.exit(f"Could not find a '{key}' column in {ctx}.\n"
                 f"  tried: {CANDIDATES[key]}\n  available: {sorted(cols)}\n"
                 f"  -> add the right name to CANDIDATES['{key}'] at the top "
                 f"of the script, or run with --schema to inspect.")
    return None


def dump_schema():
    con = sqlite3.connect(DB_PATH)
    try:
        for t in TABLES.values():
            print(f"\n{t}:")
            for c in table_cols(con, t):
                print(f"  {c}")
    finally:
        con.close()


def load_db():
    min_date = (date.today() - timedelta(days=LOOKBACK_DAYS)).isoformat()
    con = sqlite3.connect(DB_PATH)
    try:
        p = table_cols(con, TABLES["perf"])
        col = {
            "date":    pick(p, "date", ctx=TABLES["perf"]),
            "cost":    pick(p, "cost", ctx=TABLES["perf"]),
            "clicks":  pick(p, "clicks", ctx=TABLES["perf"]),
            "leads":   pick(p, "leads", ctx=TABLES["perf"]),
            "revenue": pick(p, "revenue", ctx=TABLES["perf"]),
        }
        # denormalized perf table (names + account on the row) needs no joins
        denorm = all(k in p for k in ("ad_group_name", "campaign_name",
                                      "account_id"))
        # optional device rollup filter: if perf carries device splits + an
        # ALL rollup row, keep only ALL to avoid double counting
        device_filter = ""
        if "device" in p:
            has_all = con.execute(
                f"SELECT 1 FROM {TABLES['perf']} WHERE device='ALL' LIMIT 1"
            ).fetchone()
            if has_all:
                device_filter = "  AND agp.device = 'ALL'\n"
        cost_expr = (f"agp.{col['cost']} / 1000000.0"
                     if col["cost"].endswith("_micros") else f"agp.{col['cost']}")
        opt = ""
        imps_col = pick(p, "imps", required=False)
        is_col = pick(p, "impr_share", required=False)
        if imps_col:
            opt += f", agp.{imps_col} AS impressions"
        if is_col:
            opt += f", agp.{is_col} AS impr_share"
        core = (f"{cost_expr} AS cost, agp.{col['clicks']} AS clicks, "
                f"agp.{col['leads']} AS leads, agp.{col['revenue']} AS revenue"
                + opt + " ")
        if denorm:
            sql = (
                f"SELECT agp.{col['date']} AS date, "
                f"agp.campaign_name AS campaign, agp.ad_group_name AS ad_group, "
                + core +
                f"FROM {TABLES['perf']} agp "
                f"WHERE agp.account_id = :account_id "
                f"AND agp.{col['date']} >= :min_date\n" + device_filter
            )
        else:
            a = table_cols(con, TABLES["adg"])
            c = table_cols(con, TABLES["camp"])
            jc = {"adg_fk": pick(p, "adg_fk", ctx=TABLES["perf"]),
                  "adg_name": pick(a, "adg_name", ctx=TABLES["adg"]),
                  "camp_fk": pick(a, "camp_fk", ctx=TABLES["adg"]),
                  "camp_name": pick(c, "camp_name", ctx=TABLES["camp"]),
                  "acct": pick(c, "acct", ctx=TABLES["camp"])}
            sql = (
                f"SELECT agp.{col['date']} AS date, "
                f"c.{jc['camp_name']} AS campaign, "
                f"ag.{jc['adg_name']} AS ad_group, "
                + core +
                f"FROM {TABLES['perf']} agp "
                f"JOIN {TABLES['adg']} ag ON ag.id = agp.{jc['adg_fk']} "
                f"JOIN {TABLES['camp']} c ON c.id = ag.{jc['camp_fk']} "
                f"WHERE c.{jc['acct']} = :account_id "
                f"AND agp.{col['date']} >= :min_date\n" + device_filter
            )
        print(f"(columns detected: cost={col['cost']}, leads={col['leads']}, "
              f"revenue={col['revenue']}"
              + (", denormalized" if denorm else ", joined")
              + (", device=ALL rollup" if device_filter else "") + ")")
        df = pd.read_sql_query(sql, con, params={"account_id": ACCOUNT_ID,
                                                 "min_date": min_date})
    finally:
        con.close()
    if df.empty:
        sys.exit("Query ran but returned 0 rows — check ACCOUNT_ID and that "
                 f"{TABLES['perf']} has data in the last {LOOKBACK_DAYS} days.")
    return df


def load_csv(path):
    return pd.read_csv(path)


def load_demo():
    """Synthetic data so the report format can be verified end to end."""
    rng = np.random.default_rng(7)
    end = date(2026, 8, 18)
    days = [end - timedelta(days=i) for i in range(LOOKBACK_DAYS)][::-1]
    cells = [  # campaign, ad_group, base cost/day, cvr-ish lead rate per $
        ("Tier1", "Geico - Exact", 260, 0.055),
        ("Tier1", "Generic Home Insurance", 120, 0.03),
        ("Tier2", "Farmers - Broad", 190, 0.03),
        ("Tier2", "Hartford - Exact", 240, 0.05),
        ("Tier2", "Kin Insurance", 150, 0.05),
        ("Tier3", "Kin Insurance", 90, 0.025),
        ("TEXAS", "Farmers - Broad", 130, 0.045),
        ("TEXAS", "Kin Insurance", 160, 0.06),
        ("CALIFORNIA", "Bamboo CaRegion", 170, 0.05),
        ("CALIFORNIA", "Generic Home Insurance", 80, 0.03),
    ]
    dow_mult = {0: 1.15, 1: 1.1, 2: 1.05, 3: 1.0, 4: 0.95, 5: 0.75, 6: 0.7}
    rows = []
    for d in days:
        for camp, ag, base_cost, lead_rate in cells:
            m = dow_mult[d.weekday()] * rng.normal(1.0, 0.10)
            cost = max(5.0, base_cost * m)
            clicks = max(1, int(cost / rng.normal(7.0, 0.6)))
            lam = cost * lead_rate
            # inject anomalies on the final day: Geico CvR break, Farmers cost spike
            if d == end and "Geico" in ag:
                lam *= 0.35
            if d == end and camp == "Tier2" and "Farmers" in ag:
                cost *= 1.6
                clicks = int(clicks * 1.5)
            leads = rng.poisson(lam)
            revenue = leads * max(0.0, rng.normal(50, 9))
            rows.append([d.isoformat(), camp, ag, round(cost, 2), clicks,
                         int(leads), round(revenue, 2)])
    return pd.DataFrame(rows, columns=["date", "campaign", "ad_group",
                                       "cost", "clicks", "leads", "revenue"])


# ----------------------------------------------------------------------------
# Core
# ----------------------------------------------------------------------------
def prep(df):
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"]).dt.date
    for c in ["cost", "clicks", "leads", "revenue"]:
        df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0.0)
    for c in ["impressions", "impr_share"]:
        if c in df:
            df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0.0)
        else:
            df[c] = 0.0
    df["gp"] = df["revenue"] - df["cost"]
    df["carrier"] = df["ad_group"].map(carrier_of)
    df["campaign"] = df["campaign"].map(short_campaign)
    return df


def rollup(df, dims):
    g = df.groupby(dims + ["date"], as_index=False)[
        ["cost", "clicks", "leads", "revenue", "gp"]].sum()
    return g


def dow_baseline(g, dims, target):
    """Median of the prior BASELINE_WEEKS same weekdays for each cell."""
    base_dates = [target - timedelta(days=7 * i)
                  for i in range(1, BASELINE_WEEKS + 1)]
    floor = date.fromisoformat(BASELINE_MIN_DATE)
    base_dates = [d for d in base_dates if d >= floor]
    hist = g[g["date"].isin(base_dates)]
    if hist.empty:
        return None, 0
    med = hist.groupby(dims, as_index=False)[
        ["cost", "clicks", "leads", "revenue", "gp"]].median()
    return med, len(base_dates)


def fmt_md(d, dow=True):
    """Portable 'Tue 8/18' / '8/18' (Windows strftime has no %-m/%-d)."""
    s = f"{d.month}/{d.day}"
    return f"{d.strftime('%a')} {s}" if dow else s


def fmt_money(x):
    return f"-${abs(x):,.0f}" if x < 0 else f"${x:,.0f}"


def line(label, cur, exp=None, width=26):
    cpl_c = cur["cost"] / cur["leads"] if cur["leads"] else float("nan")
    s = (f"  {label:<{width}} cost {fmt_money(cur['cost']):>8}  "
         f"clicks {cur['clicks']:>5.0f}  leads {cur['leads']:>4.0f}  "
         f"CPL {('$%.0f' % cpl_c) if cur['leads'] else '  --':>5}  "
         f"rev {fmt_money(cur['revenue']):>8}  GP {fmt_money(cur['gp']):>8}")
    if exp is not None:
        s += (f"   | vs typical: cost {fmt_money(cur['cost']-exp['cost']):>7}"
              f"  leads {cur['leads']-exp['leads']:+.0f}"
              f"  GP {fmt_money(cur['gp']-exp['gp']):>7}")
    return s


def auction_stats(df, dates):
    """Auction-side rollup for a set of dates: imps, CPC, CTR, censoring-aware IS.
    Google reports IS below 10% as 0.0999 — treat <=0.1 as censored."""
    d = df[df["date"].isin(dates)]
    if d.empty:
        return None
    imps = d["impressions"].sum()
    clicks = d["clicks"].sum()
    cost = d["cost"].sum()
    cpc = cost / clicks if clicks else float("nan")
    ctr = clicks / imps if imps else float("nan")
    unc = d[d["impr_share"] > 0.1]
    cens_imps = d[(d["impr_share"] > 0) & (d["impr_share"] <= 0.1)]["impressions"].sum()
    if unc["impressions"].sum() > 0:
        is_w = (unc["impr_share"] * unc["impressions"]).sum() / unc["impressions"].sum()
        cshare = cens_imps / imps if imps else 0
        is_disp = f"{is_w:.1%}" + (f" ({cshare:.0%} of imps in <10% cells)"
                                   if cshare > 0.2 else "")
    elif cens_imps > 0:
        is_disp = "<10% (censored)"
    else:
        is_disp = "n/a"
    return {"imps": imps / len(dates), "cpc": cpc, "ctr": ctr, "is": is_disp}


def auction_line(df, target, base_dates, out, indent="  "):
    if df["impressions"].sum() == 0:
        return
    cur = auction_stats(df, [target])
    base = auction_stats(df, base_dates) if base_dates else None
    if cur is None:
        return
    s = (f"{indent}auction: impr {cur['imps']:,.0f}  CPC ${cur['cpc']:.2f}  "
         f"CTR {cur['ctr']:.1%}  IS {cur['is']}")
    if base:
        s += (f"   | vs typical: impr {cur['imps']/base['imps']-1:+.0%}  "
              f"CPC {cur['cpc']/base['cpc']-1:+.0%}  "
              f"CTR {(cur['ctr']-base['ctr'])*100:+.2f}pt")
    out.append(s)


def movers(g, dims, target, label, out):
    med, nbase = dow_baseline(g, dims, target)
    if med is None:
        out.append(f"  (no baseline history yet for {label})")
        return
    cur = g[g["date"] == target].groupby(dims, as_index=False)[
        ["cost", "clicks", "leads", "revenue", "gp"]].sum()
    m = cur.merge(med, on=dims, how="outer", suffixes=("", "_exp")).fillna(0.0)
    m = m[(m["cost_exp"] >= MIN_BASELINE_COST) | (m["cost"] >= MIN_BASELINE_COST)]
    m["gp_dev"] = m["gp"] - m["gp_exp"]
    m["cost_dev_pct"] = np.where(m["cost_exp"] > 0,
                                 m["cost"] / m["cost_exp"] - 1, np.nan)
    flag = m[(m["gp_dev"].abs() >= GP_FLAG_DOLLARS) |
             (m["cost_dev_pct"].abs() >= COST_FLAG_PCT)]
    flag = flag.reindex(flag["gp_dev"].abs().sort_values(ascending=False).index)
    out.append(f"\n{label} movers vs typical {target.strftime('%A')} "
               f"(baseline: median of last {nbase}) — "
               f"flag = |GP dev| >= ${GP_FLAG_DOLLARS} or |cost dev| >= "
               f"{COST_FLAG_PCT:.0%}:")
    if flag.empty:
        out.append("  none — normal day at this grain.")
        return
    for _, r in flag.head(TOP_N_MOVERS).iterrows():
        name = " / ".join(str(r[d]) for d in dims)
        why = []
        if abs(r["cost_dev_pct"]) >= COST_FLAG_PCT and not np.isnan(r["cost_dev_pct"]):
            why.append(f"cost {r['cost_dev_pct']:+.0%}")
        dl = r["leads"] - r["leads_exp"]
        if abs(dl) >= 1:
            why.append(f"leads {dl:+.0f} ({r['leads_exp']:.0f} typical)")
        out.append(f"  {name:<28} GP {fmt_money(r['gp']):>7} vs "
                   f"{fmt_money(r['gp_exp']):>7} typical  "
                   f"-> {fmt_money(r['gp_dev']):>7}"
                   + ("   [" + ", ".join(why) + "]" if why else ""))


def wtd(g_all, target, out, df=None):
    monday = target - timedelta(days=target.weekday())
    this_days = [monday + timedelta(days=i)
                 for i in range((target - monday).days + 1)]
    last_days = [d - timedelta(days=7) for d in this_days]
    cur = g_all[g_all["date"].isin(this_days)][
        ["cost", "clicks", "leads", "revenue", "gp"]].sum()
    prv = g_all[g_all["date"].isin(last_days)][
        ["cost", "clicks", "leads", "revenue", "gp"]].sum()
    span = (f"{fmt_md(this_days[0])}–{fmt_md(this_days[-1])}")
    out.append(f"\nWEEK TO DATE ({span}) vs same days last week:")
    out.append(line("this week", cur))
    out.append(line("last week", prv))
    d = cur - prv
    cpl_c = cur["cost"] / cur["leads"] if cur["leads"] else np.nan
    cpl_p = prv["cost"] / prv["leads"] if prv["leads"] else np.nan
    rpl_c = cur["revenue"] / cur["leads"] if cur["leads"] else np.nan
    rpl_p = prv["revenue"] / prv["leads"] if prv["leads"] else np.nan
    out.append(f"  {'delta':<26} cost {fmt_money(d['cost']):>8}  "
               f"clicks {d['clicks']:>+5.0f}  leads {d['leads']:>+4.0f}  "
               f"CPL {cpl_c - cpl_p:>+5.0f}  rev {fmt_money(d['revenue']):>8}"
               f"  GP {fmt_money(d['gp']):>8}"
               f"   (RPL {rpl_p:.0f}->{rpl_c:.0f})")
    if df is not None and df["impressions"].sum() > 0:
        a_cur = auction_stats(df, this_days)
        a_prv = auction_stats(df, last_days)
        if a_cur and a_prv:
            out.append(f"  {'auction':<26} impr/day {a_prv['imps']:,.0f}"
                       f" -> {a_cur['imps']:,.0f}"
                       f" ({a_cur['imps']/a_prv['imps']-1:+.0%})   "
                       f"CPC ${a_prv['cpc']:.2f} -> ${a_cur['cpc']:.2f}"
                       f" ({a_cur['cpc']/a_prv['cpc']-1:+.0%})   "
                       f"CTR {a_prv['ctr']:.1%} -> {a_cur['ctr']:.1%}   "
                       f"IS {a_prv['is']} -> {a_cur['is']}")


def change_log(out):
    if not CHANGE_LOG_CSV:
        return
    try:
        cl = pd.read_csv(CHANGE_LOG_CSV, parse_dates=["date"])
        recent = cl[cl["date"] >= pd.Timestamp(date.today() - timedelta(days=7))]
        if not recent.empty:
            out.append("\nCHANGES IN EFFECT (last 7 days):")
            for _, r in recent.sort_values("date").iterrows():
                out.append(f"  {r['date'].date()}  [{r.get('scope','')}] "
                           f"{r.get('change','')}")
    except Exception as e:  # change log is optional; never break the report
        out.append(f"\n(change log unreadable: {e})")


def run(df, as_of=None):
    df = prep(df)
    target = as_of or df["date"].max()
    out = []
    out.append("=" * 78)
    out.append(f"DAILY PULSE — Google Home — data through "
               f"{target.strftime('%A %Y-%m-%d')}")
    out.append("=" * 78)

    acct = rollup(df.assign(all="ACCOUNT"), ["all"])
    med, nbase = dow_baseline(acct, ["all"], target)
    cur = acct[acct["date"] == target][
        ["cost", "clicks", "leads", "revenue", "gp"]].sum()
    out.append(f"\nYESTERDAY ({fmt_md(target)}):")
    if med is not None:
        out.append(line("account", cur,
                        med[["cost", "clicks", "leads", "revenue", "gp"]].iloc[0]))
    else:
        out.append(line("account", cur))
    base_dates = [target - timedelta(days=7 * i)
                  for i in range(1, BASELINE_WEEKS + 1)
                  if target - timedelta(days=7 * i)
                  >= date.fromisoformat(BASELINE_MIN_DATE)]
    auction_line(df, target, base_dates, out)

    movers(rollup(df, ["campaign"]), ["campaign"], target, "CAMPAIGN", out)
    movers(rollup(df[df["carrier"] != "(non-carrier)"], ["carrier"]),
           ["carrier"], target, "CARRIER", out)

    wtd(acct, target, out, df=df)
    change_log(out)
    out.append("")
    return "\n".join(out), target


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--csv")
    ap.add_argument("--db", help="path to paid_search.db (default: DB_PATH in config)")
    ap.add_argument("--date", help="treat this date as the latest day (YYYY-MM-DD)")
    ap.add_argument("--no-save", action="store_true",
                    help="print only; don't write to OUTPUT_DIR")
    ap.add_argument("--schema", action="store_true",
                    help="print the columns of the three source tables and exit")
    args = ap.parse_args()
    global DB_PATH
    if args.db:
        DB_PATH = Path(args.db)
    if args.schema:
        if not Path(DB_PATH).exists():
            sys.exit(f"DB not found at {DB_PATH}")
        dump_schema()
        return
    if args.demo:
        df = load_demo()
    elif args.csv:
        df = load_csv(args.csv)
    else:
        if not Path(DB_PATH).exists():
            sys.exit(f"DB not found at {DB_PATH} — set DB_PATH/--db, or use --csv/--demo")
        df = load_db()
    as_of = date.fromisoformat(args.date) if args.date else None
    report, target = run(df, as_of)
    print(report)
    if not args.no_save:
        try:
            OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
            stamp = target.isoformat()
            path = OUTPUT_DIR / f"daily_pulse_{stamp}.txt"
            path.write_text(report, encoding="utf-8")
            (OUTPUT_DIR / "latest.txt").write_text(report, encoding="utf-8")
            print(f"saved -> {path}")
        except Exception as e:
            print(f"(could not save report: {e})", file=sys.stderr)


if __name__ == "__main__":
    main()
