#!/usr/bin/env python3
"""
expansion_diagnostic.py — adjudicate the impression expansion (week of 8/17+)

Three independent tests, each answering: matching expansion vs demand growth?

  A. MATCH-TYPE SPLIT   - daily impressions by match type, last 14 days,
                          plus partial-week WoW growth per match type.
                          Expansion loads growth onto BROAD; demand lifts
                          EXACT proportionally.
  B. IS DIVERGENCE      - search_impression_share vs search_exact_match_is,
                          impression-weighted daily. Overall IS flat while
                          exact-match IS holds/rises under growing volume =
                          the eligible pool grew via non-exact matching.
  C. NEW-QUERY SHARE    - share of impressions from search terms never seen
                          in the trailing history, this week vs last week.
                          (Sync undercounts low-volume terms; the WoW *shift*
                          in share survives that. Absolute levels don't.)

Each module runs independently; if a table/column isn't found it explains
what it looked for and moves on. Usage:

  python expansion_diagnostic.py            # run all against DB_PATH
  python expansion_diagnostic.py --db PATH  # different db
  python expansion_diagnostic.py --tables   # list candidate tables and exit
"""

import argparse
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
DB_PATH = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")
ACCOUNT_ID = 1
OUTPUT_DIR = (Path(r"C:\Projects\google-ads-ops\analysis"
                   r"\profit-attribution-tracker\output") / "diagnostics")
LOOKBACK_DAYS = 15          # daily table window for modules A/B
HISTORY_WEEKS = 8           # "never seen" horizon for module C
ADG_TABLE = "ad_group_performance_daily"   # known-good from daily_pulse

DATE_CANDS = ["date", "report_date", "day"]
IMP_CANDS = ["impressions", "impr"]
COST_CANDS = ["cost", "cost_micros", "spend"]
TERM_CANDS = ["search_term", "query", "search_query", "term", "query_text"]
MATCH_CANDS = ["match_type", "keyword_match_type"]
INSTANCE_CANDS = ["instance_id", "keyword_instance_id"]


def cols(con, t):
    try:
        return [r[1] for r in con.execute(f"PRAGMA table_info({t})").fetchall()]
    except sqlite3.Error:
        return []


def tables(con):
    return [r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]


def first(cands, available):
    for c in cands:
        if c in available:
            return c
    return None


def money(col):
    return f"{col} / 1000000.0" if col.endswith("_micros") else col


def week_windows(latest):
    monday = latest - timedelta(days=latest.weekday())
    this_days = [monday + timedelta(days=i)
                 for i in range((latest - monday).days + 1)]
    last_days = [d - timedelta(days=7) for d in this_days]
    return this_days, last_days


def fmt_days(days):
    return f"{days[0].month}/{days[0].day}-{days[-1].month}/{days[-1].day}"


# ---------------------------------------------------------------------------
# Module A: match-type impression split
# ---------------------------------------------------------------------------
def load_valid(con, sql):
    """Load and validate: parseable dates, nonzero impressions."""
    df = pd.read_sql_query(sql, con)
    if df.empty or "date" not in df:
        return pd.DataFrame()
    df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.date
    df = df.dropna(subset=["date"])
    if df.empty or df.get("impressions", pd.Series(dtype=float)).sum() <= 0:
        return pd.DataFrame()
    return df


def kpd_datecol(con):
    return first(["report_date", "date"], cols(con, "keyword_performance_daily"))


def module_a(con, out):
    out.append("\n" + "=" * 74)
    out.append("A. MATCH-TYPE SPLIT OF IMPRESSION GROWTH")
    out.append("=" * 74)
    p = cols(con, "keyword_performance_daily")
    k = cols(con, "keyword_instances")
    if not p or not k:
        out.append("  SKIPPED: keyword_performance_daily / keyword_instances "
                   "not found — pinned source per DB architect.")
        return
    dcol = kpd_datecol(con)
    ccol = first(COST_CANDS, p) or "impressions"
    cexpr = money("p." + ccol) if ccol.endswith("_micros") else "p." + ccol
    if "device" not in p:
        out.append("  SKIPPED: keyword_performance_daily has no device column;"
                   " expected ALL-rollup filter per DB architect. Check schema.")
        return
    if "account_id" not in k:
        out.append("  SKIPPED: keyword_instances has no account_id; account "
                   "scoping must go through the keyword table per DB architect.")
        return
    min_date = (date.today() - timedelta(days=LOOKBACK_DAYS)).isoformat()
    sql = (f"SELECT p.{dcol} AS date, k.match_type AS match_type, "
           f"SUM(p.impressions) AS impressions, SUM({cexpr}) AS cost "
           f"FROM keyword_performance_daily p "
           f"JOIN keyword_instances k ON k.id = p.instance_id "
           f"WHERE k.account_id = {ACCOUNT_ID} AND p.device = 'ALL' "
           f"AND p.{dcol} >= '{min_date}' GROUP BY 1, 2")
    out.append("  source: keyword_performance_daily JOIN keyword_instances "
               f"(k.account_id={ACCOUNT_ID}, device='ALL') [pinned]")
    df = load_valid(con, sql)
    if df.empty:
        out.append("  SKIPPED: pinned query returned no valid rows in window.")
        return
    df["match_type"] = df["match_type"].astype(str).str.upper().str.strip()
    latest = df["date"].max()
    piv = df.pivot_table(index="date", columns="match_type",
                         values="impressions", aggfunc="sum").fillna(0)
    out.append("\n  daily impressions by match type:")
    out.append("  " + "date".ljust(12)
               + "".join(m.rjust(10) for m in piv.columns) + "TOTAL".rjust(10))
    for d, row in piv.iterrows():
        out.append("  " + str(d).ljust(12)
                   + "".join(f"{v:>10,.0f}" for v in row.values)
                   + f"{row.sum():>10,.0f}")
    this_days, last_days = week_windows(latest)
    cur = df[df["date"].isin(this_days)].groupby("match_type")[
        ["impressions", "cost"]].sum()
    prv = df[df["date"].isin(last_days)].groupby("match_type")[
        ["impressions", "cost"]].sum()
    comp = cur.join(prv, lsuffix="_cur", rsuffix="_prv", how="outer").fillna(0)
    tot_growth = comp["impressions_cur"].sum() - comp["impressions_prv"].sum()
    out.append(f"\n  WoW ({fmt_days(this_days)} vs {fmt_days(last_days)}, "
               f"same partial window):")
    out.append("  " + "match".ljust(10) + "impr prev".rjust(11)
               + "impr cur".rjust(11) + "growth".rjust(9)
               + "% of acct growth".rjust(18)
               + "cost prev".rjust(11) + "cost cur".rjust(11))
    for m, r in comp.sort_values("impressions_cur", ascending=False).iterrows():
        g = r["impressions_cur"] - r["impressions_prv"]
        shr = g / tot_growth if tot_growth else 0
        out.append("  " + str(m).ljust(10)
                   + f"{r['impressions_prv']:>11,.0f}{r['impressions_cur']:>11,.0f}"
                   + f"{g:>+9,.0f}{shr:>17.0%} "
                   + f"{r['cost_prv']:>11,.0f}{r['cost_cur']:>11,.0f}")
    out.append("\n  read: growth concentrated in BROAD (EXACT ~flat) = "
               "matching expansion; growth proportional across types = demand.")
# ---------------------------------------------------------------------------
# Module B: overall IS vs exact-match IS (censoring-aware)
# ---------------------------------------------------------------------------
def module_b(con, out):
    out.append("\n" + "=" * 74)
    out.append("B. IMPRESSION SHARE vs EXACT-MATCH IS (impression-weighted proxy)")
    out.append("=" * 74)
    c = cols(con, ADG_TABLE)
    need = {"search_impression_share", "search_exact_match_is"}
    if not need.issubset(c):
        out.append(f"  SKIPPED: {ADG_TABLE} lacks {need - set(c)}")
        return
    dcol = first(DATE_CANDS, c)
    icol = first(IMP_CANDS, c)
    min_date = (date.today() - timedelta(days=LOOKBACK_DAYS)).isoformat()
    dev = "AND device = 'ALL'" if "device" in c else ""
    df = pd.read_sql_query(
        f"SELECT {dcol} AS date, {icol} AS impressions, "
        f"search_impression_share AS overall_is, "
        f"search_exact_match_is AS exact_is "
        f"FROM {ADG_TABLE} WHERE account_id = {ACCOUNT_ID} "
        f"AND {dcol} >= '{min_date}' {dev}", con)
    df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.date
    df = df.dropna(subset=["date"])
    if df.empty:
        out.append("  SKIPPED: no rows with valid dates in window.")
        return
    for m in ["overall_is", "exact_is"]:
        df[m] = pd.to_numeric(df[m], errors="coerce").fillna(0)

    def wavg(d, col):
        u = d[d[col] > 0.1]          # <=0.1 is the API's "<10%" censor floor
        if u["impressions"].sum() == 0:
            return np.nan, 1.0
        cens = d[(d[col] > 0) & (d[col] <= 0.1)]["impressions"].sum()
        share_cens = cens / d["impressions"].sum() if d["impressions"].sum() else 0
        return ((u[col] * u["impressions"]).sum() / u["impressions"].sum(),
                share_cens)

    out.append("\n  " + "date".ljust(12) + "impr".rjust(9)
               + "overall IS".rjust(12) + "exact IS".rjust(10)
               + "  (censored share of imps: overall / exact)")
    for d, grp in df.groupby("date"):
        o, oc = wavg(grp, "overall_is")
        e, ec = wavg(grp, "exact_is")
        odisp = "<10%" if np.isnan(o) else f"{o:.1%}"
        edisp = "<10%" if np.isnan(e) else f"{e:.1%}"
        out.append("  " + str(d).ljust(12)
                   + f"{grp['impressions'].sum():>9,.0f}"
                   + f"{odisp:>12}{edisp:>10}   ({oc:.0%} / {ec:.0%})")
    out.append("\n  note: weighted by impressions, not eligible impressions — "
               "a directional proxy; don't reconcile levels against the UI.")
    out.append("\n  read: impressions up with overall IS flat while exact IS "
               "holds/rises = eligible pool grew via non-exact matching. "
               "Both IS metrics rising with impressions = winning share "
               "(bids). All flat with impressions up = demand.")


# ---------------------------------------------------------------------------
# Module C: never-before-seen query share, WoW
# ---------------------------------------------------------------------------
def module_c(con, out):
    out.append("\n" + "=" * 74)
    out.append("C. NEW-QUERY SHARE (never seen in trailing "
               f"{HISTORY_WEEKS} weeks)")
    out.append("=" * 74)
    p = cols(con, "search_query_performance")
    sq = cols(con, "search_queries")
    if not p or not sq:
        out.append("  SKIPPED: search_query_performance / search_queries not "
                   "found — pinned source per DB architect.")
        return
    dcol = first(["report_date", "date", "day"], p)
    vcol = first(IMP_CANDS, p) or ("clicks" if "clicks" in p else None)
    term = first(TERM_CANDS + ["query_text", "text"], sq)
    if not (dcol and vcol and term and "query_id" in p and "id" in sq):
        out.append(f"  SKIPPED: expected columns missing "
                   f"(date={dcol}, volume={vcol}, term={term}, "
                   f"query_id={'query_id' in p}).")
        return
    filt = []
    if "platform" in p:
        filt.append("p.platform = 'google'")
    if "device" in p:
        filt.append("p.device = 'ALL'")
    if "account_id" in p:
        filt.append(f"p.account_id = {ACCOUNT_ID}")
    where = (" AND " + " AND ".join(filt)) if filt else ""
    out.append(f"  source: search_query_performance JOIN search_queries "
               f"(volume: {vcol}"
               + (", " + ", ".join(filt) if filt else "") + ") [pinned]")
    use_fsd = "first_seen_date" in sq
    min_date = (date.today()
                - timedelta(days=HISTORY_WEEKS * 7 + 14)).isoformat()
    fsd_sel = ", sq.first_seen_date AS first_seen" if use_fsd else ""
    df = pd.read_sql_query(
        f"SELECT p.{dcol} AS date, sq.{term} AS term, "
        f"SUM(p.{vcol}) AS vol{fsd_sel} "
        f"FROM search_query_performance p "
        f"JOIN search_queries sq ON sq.id = p.query_id "
        f"WHERE p.{dcol} >= '{min_date}'{where} "
        f"GROUP BY p.{dcol}, sq.{term}"
        + (", sq.first_seen_date" if use_fsd else ""), con)
    if df.empty:
        out.append("  SKIPPED: query returned no rows.")
        return
    df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.date
    df = df.dropna(subset=["date"])
    if df.empty:
        out.append("  SKIPPED: no rows with valid dates in window.")
        return
    df["term"] = df["term"].astype(str).str.lower().str.strip()
    latest = df["date"].max()
    this_days, last_days = week_windows(latest)

    if use_fsd:
        df["first_seen"] = pd.to_datetime(df["first_seen"],
                                          errors="coerce").dt.date

        def new_share(days):
            wk = df[df["date"].isin(days)]
            grp = wk.groupby("term").agg(vol=("vol", "sum"),
                                         fs=("first_seen", "min"))
            tot = grp["vol"].sum()
            new = grp[grp["fs"] >= min(days)]
            return (new["vol"].sum() / tot if tot else np.nan, len(new),
                    len(grp), new["vol"].sort_values(ascending=False).head(15))
        method = ("first_seen_date (sync first-seen; 2024 backfill reset the "
                  "floor — valid for WoW novelty shift)")
    else:
        def new_share(days):
            hist_end = min(days) - timedelta(days=1)
            hist_start = hist_end - timedelta(days=HISTORY_WEEKS * 7 - 1)
            hist_terms = set(df[(df["date"] >= hist_start)
                                & (df["date"] <= hist_end)]["term"])
            wk = df[df["date"].isin(days)].groupby("term")["vol"].sum()
            tot = wk.sum()
            new = wk[~wk.index.isin(hist_terms)]
            return (new.sum() / tot if tot else np.nan, len(new), len(wk),
                    new.sort_values(ascending=False).head(15))
        method = f"trailing {HISTORY_WEEKS}-week scan"
    out.append(f"  novelty method: {method}")

    cur_share, cur_n, cur_tot, cur_top = new_share(this_days)
    prv_share, prv_n, prv_tot, _ = new_share(last_days)
    out.append(f"\n  last week ({fmt_days(last_days)}): {prv_share:.1%} of "
               f"volume from new terms ({prv_n:,} of {prv_tot:,} terms)")
    out.append(f"  this week ({fmt_days(this_days)}): {cur_share:.1%} of "
               f"volume from new terms ({cur_n:,} of {cur_tot:,} terms)"
               f"   -> shift {cur_share - prv_share:+.1%}pt")
    out.append("\n  top new terms this week by volume:")
    for term_, v in cur_top.items():
        out.append(f"    {v:>8,.0f}  {term_}")
    out.append("\n  caveat: sync omits below-threshold terms, so absolute "
               "shares are understated for both weeks; trust the WoW shift, "
               "not the level. A sharply higher new-term share = matching "
               "expansion, and the top-new-terms list is the negatives "
               "shortlist.")


# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db")
    ap.add_argument("--tables", action="store_true",
                    help="list tables with date+impressions or a term column")
    ap.add_argument("--no-save", action="store_true")
    args = ap.parse_args()
    db = Path(args.db) if args.db else DB_PATH
    if not db.exists():
        sys.exit(f"DB not found at {db}")
    con = sqlite3.connect(db)
    try:
        if args.tables:
            for t in tables(con):
                c = cols(con, t)
                tags = []
                if first(DATE_CANDS, c) and first(IMP_CANDS, c):
                    tags.append("perf-like")
                if first(MATCH_CANDS, c):
                    tags.append("has match_type")
                if first(TERM_CANDS, c):
                    tags.append("has search terms")
                if tags:
                    print(f"{t:<45} {', '.join(tags)}")
            return
        out = ["=" * 74,
               f"EXPANSION DIAGNOSTIC — account {ACCOUNT_ID} — run "
               f"{date.today().isoformat()}",
               "=" * 74]
        for mod in (module_a, module_b, module_c):
            try:
                mod(con, out)
            except Exception as e:
                out.append(f"\n  {mod.__name__} FAILED: {type(e).__name__}: {e}"
                           "\n  (other modules unaffected — paste this line "
                           "back to me)")
        report = "\n".join(out) + "\n"
        print(report)
        if not args.no_save:
            try:
                OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
                p = OUTPUT_DIR / f"expansion_diagnostic_{date.today().isoformat()}.txt"
                p.write_text(report, encoding="utf-8")
                print(f"saved -> {p}")
            except Exception as e:
                print(f"(could not save: {e})", file=sys.stderr)
    finally:
        con.close()


if __name__ == "__main__":
    main()
