#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
termlife_p1_cr12.py  (v1)
=========================
P1 — CR12 weekly YTD deep dive, Term Life (account_id=29).
Campaign: LIFE INSURANCE-CR12-SEARCH-ALLDEVICE-USA-STE (22899566624).
READ-ONLY. Console output only (workbook assembled at final deliverable).

Sections:
  V0  Verification: cpd.conversions vs kpd routed (monthly) + scale gates
  W1  CR12 weekly master series (cpd): volume, spend, CPC, routed leads,
      IS family (eligible-weighted), budget & tROAS that-day, event markers
  W2  Censoring disclosure (rows at Google's 0.0999 / 0.9001 bounds)
  W3  Google Trends weekly overlay + correlations (full / pre-contamination)
  W4  kpd weekly: ethos vs rest within CR12 + impression-weighted QS
  W5  sqp weekly (CR12, ethos keywords): match_type_served mix + tail share
  W6  LAG_SENSITIVE dual view (full vs minus-last-2-days)

Conventions: device='ALL'; Mon-Sat weeks (no Sundays by schedule); routed
leads = primary action (cpd.conversions verified == kpd primary in V0);
summed-numerator ratios; IS weekly = SUM(impr)/SUM(eligible) with
eligible = impr/IS per day; censored bound rows included in weighted
aggregates and disclosed, never quoted as point values.
"""

import os
import sqlite3
import pandas as pd
import numpy as np

DB_PATH = r"C:\Projects\google-ads-ops\db\paid_search.db"
ACCOUNT_ID = 29
CR12_ID = 22899566624
WINDOW_START = "2025-08-01"        # clean routed history
CONTAM_START = "2026-09-29"        # schedule-cut incident (sibling thread)

# Google Trends weekly export (two columns: 'ethos life','ethos insurance').
# EDIT THIS PATH to where the CSV lives on your machine:
TRENDS_CSV = r"C:\Projects\google-ads-ops\analysis\google-term-life-policy-kpi-ytd\input\time_series_US_20251008-1436_20261008-1436-weekly.csv"

EVENTS = {
    "2026-05-25": "IS-drop start (post-Memorial-Day)",
    "2026-08-14": "Limited-by-budget flag",
    "2026-08-17": "Google bid/pacing update + Tier budget cuts",
    "2026-09-10": "Budget raises begin (CR3 9/10, CR12 9/11)",
    "2026-09-18": "tROAS cuts CR3/CR45; CR12 5200->5900 9/21",
    "2026-09-29": "SCHEDULE-CUT INCIDENT (contaminated ->)",
}

pd.set_option("display.width", 240)
pd.set_option("display.max_columns", 60)
pd.set_option("display.max_rows", 400)
pd.set_option("display.float_format", lambda x: f"{x:,.2f}")


def connect():
    return sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)


def hr(t):
    print("\n" + "=" * 110)
    print(t)
    print("=" * 110)


def week_start(s):
    """Monday of each date's week (dates are ISO strings)."""
    d = pd.to_datetime(s)
    return (d - pd.to_timedelta((d.dt.dayofweek) % 7, unit="D")).dt.date


def event_label(wk):
    d0 = pd.Timestamp(wk)
    labs = [v for k, v in EVENTS.items()
            if d0 <= pd.Timestamp(k) <= d0 + pd.Timedelta(days=5)]
    return "; ".join(labs)


def cols(con, table):
    return [r[1] for r in con.execute(f"PRAGMA table_info({table})").fetchall()]


def main():
    con = connect()

    # ------------------------------------------------------------------ V0
    hr("V0  VERIFICATION & GATES")
    v = pd.read_sql_query("""
        SELECT SUBSTR(date,1,7) AS month,
               SUM(conversions) AS cpd_conv,
               SUM(conversion_value) AS cpd_val
        FROM campaign_performance_daily
        WHERE account_id=? AND device='ALL' AND date>=?
        GROUP BY 1
    """, con, params=(ACCOUNT_ID, WINDOW_START))
    k = pd.read_sql_query("""
        SELECT SUBSTR(p.report_date,1,7) AS month,
               SUM(p.primary_conv_count) AS kpd_leads,
               SUM(p.primary_conv_value) AS kpd_val
        FROM keyword_performance_daily p
        JOIN keyword_instances ki ON ki.id=p.instance_id
        WHERE ki.account_id=? AND p.device='ALL' AND p.report_date>=?
        GROUP BY 1
    """, con, params=(ACCOUNT_ID, WINDOW_START))
    vm = v.merge(k, on="month", how="outer").fillna(0)
    vm["conv_diff"] = vm["cpd_conv"] - vm["kpd_leads"]
    vm["conv_diff_%"] = 100 * vm["conv_diff"] / vm["cpd_conv"].replace(0, np.nan)
    print("  cpd.conversions vs kpd routed leads, account-wide monthly")
    print("  (if |diff%| <= ~2% every month, cpd conversions are usable as "
          "routed at campaign grain; PMAX-era months may diverge — note it):")
    print(vm.round(2).to_string(index=False))

    mx = con.execute(
        "SELECT MAX(date) FROM campaign_performance_daily "
        "WHERE account_id=? AND campaign_id=?",
        (ACCOUNT_ID, CR12_ID)).fetchone()[0]
    print(f"\n  CR12 max cpd date: {mx}  (trailing ~3 days provisional; "
          f"~7-day conversion restatement)")
    tot26 = con.execute(
        "SELECT SUM(cost_micros)/1e6 FROM campaign_performance_daily "
        "WHERE account_id=? AND campaign_id=? AND device='ALL' "
        "AND date>='2026-01-01'", (ACCOUNT_ID, CR12_ID)).fetchone()[0]
    print(f"  CR12 2026 spend: ${tot26:,.0f}   (gate: $518,485 from P0)")

    # ------------------------------------------------------------------ W1
    hr("W1  CR12 WEEKLY MASTER SERIES (cpd, device=ALL)")
    d = pd.read_sql_query("""
        SELECT date, impressions, clicks, cost_micros/1e6 AS spend,
               conversions AS leads, conversion_value AS lead_value,
               search_impression_share AS is_share,
               search_budget_lost_is AS bl,
               search_rank_lost_is AS rl,
               search_exact_match_is AS em,
               budget_micros_that_day/1e6 AS daily_budget,
               target_roas_that_day AS troas
        FROM campaign_performance_daily
        WHERE account_id=? AND campaign_id=? AND device='ALL' AND date>=?
        ORDER BY date
    """, con, params=(ACCOUNT_ID, CR12_ID, WINDOW_START))
    d["week"] = week_start(d["date"])

    ok = d["is_share"].notna() & (d["is_share"] > 0)
    d["eligible"] = np.where(ok, d["impressions"] / d["is_share"], np.nan)
    d["impr_rated"] = np.where(ok, d["impressions"], np.nan)
    d["bl_elig"] = d["bl"] * d["eligible"]
    d["rl_elig"] = d["rl"] * d["eligible"]
    d["em_impr"] = d["em"] * d["impressions"]
    d["cens_share"] = (d["is_share"] == 0.0999).astype(int)
    d["cens_lost"] = ((d["bl"] == 0.9001) | (d["rl"] == 0.9001)).astype(int)

    w = d.groupby("week").agg(
        days=("date", "nunique"),
        impressions=("impressions", "sum"),
        clicks=("clicks", "sum"),
        spend=("spend", "sum"),
        leads=("leads", "sum"),
        lead_value=("lead_value", "sum"),
        impr_rated=("impr_rated", "sum"),
        eligible=("eligible", "sum"),
        bl_elig=("bl_elig", "sum"),
        rl_elig=("rl_elig", "sum"),
        em_impr=("em_impr", "sum"),
        avg_daily_budget=("daily_budget", "mean"),
        troas=("troas", "last"),
        cens_share_days=("cens_share", "sum"),
        cens_lost_days=("cens_lost", "sum"),
    ).reset_index()

    w["cpc"] = w["spend"] / w["clicks"]
    w["val_per_lead"] = w["lead_value"] / w["leads"]
    w["IS_%"] = 100 * w["impr_rated"] / w["eligible"]
    w["budget_lost_%"] = 100 * w["bl_elig"] / w["eligible"]
    w["rank_lost_%"] = 100 * w["rl_elig"] / w["eligible"]
    w["exact_IS_%"] = 100 * w["em_impr"] / w["impressions"]
    w["event"] = w["week"].map(event_label)

    # Budget & tROAS reconstructed from change history. The cpd *_that_day
    # columns proved snapshot-like on this account (budget constant at the
    # current value, troas NULL) — flagged to the architect; the change-event
    # step series is the real lever timeline.
    ev = pd.read_sql_query(
        "SELECT change_date, change_type, old_value, new_value "
        "FROM campaign_change_events "
        "WHERE account_id=? AND campaign_id=? ORDER BY change_date",
        con, params=(ACCOUNT_ID, CR12_ID))

    def step_series(kind):
        e = ev[ev["change_type"] == kind]
        if not len(e):
            return None
        idx = pd.date_range(WINDOW_START, mx, freq="D")
        s = pd.Series(np.nan, index=idx)
        s.iloc[0] = e.iloc[0]["old_value"]
        for _, r in e.iterrows():
            ts = pd.Timestamp(r["change_date"])
            if ts < idx[0]:
                s.iloc[0] = r["new_value"]
            elif ts <= idx[-1]:
                s.loc[ts] = r["new_value"]
        return s.ffill()

    def weekly_last(s):
        wkkey = (s.index - pd.to_timedelta(s.index.dayofweek % 7,
                                           unit="D")).date
        return s.groupby(wkkey).last()

    bser = step_series("DAILY_BUDGET")
    tser = step_series("TARGET_ROAS")
    if bser is not None:
        w["budget_hist"] = w["week"].map(weekly_last(bser))
    if tser is not None:
        w["troas_hist"] = w["week"].map(weekly_last(tser))

    lever_cols = [c for c in ["budget_hist", "troas_hist"] if c in w.columns]
    cols1 = (["week", "days", "impressions", "eligible", "IS_%",
              "budget_lost_%", "rank_lost_%", "exact_IS_%"]
             + lever_cols + ["event"])
    print("\n  (a) Exposure & auction health (budget_hist/troas_hist = "
          "end-of-week values from campaign_change_events; cpd *_that_day "
          "columns are snapshot-suspect on this account and not shown):")
    print(w[cols1].round(2).to_string(index=False))

    cols2 = ["week", "clicks", "spend", "cpc", "leads", "val_per_lead",
             "lead_value", "event"]
    print("\n  (b) Spend & routed outcomes (value/lead = volume-mix proxy "
          "ONLY, never lead quality):")
    print(w[cols2].round(2).to_string(index=False))

    # ------------------------------------------------------------------ W2
    hr("W2  CENSORING DISCLOSURE (per-row bounds: share=0.0999, "
       "lost=0.9001)")
    nd = len(d)
    print(f"  CR12 days in window: {nd}; days with IS present: "
          f"{int(ok.sum())} ({100*ok.mean():.1f}%)")
    print(f"  days at share bound 0.0999: {int(d['cens_share'].sum())}")
    print(f"  days at a lost bound 0.9001: {int(d['cens_lost'].sum())}")
    cw = w[(w["cens_share_days"] > 0) | (w["cens_lost_days"] > 0)]
    print("  weeks containing bound rows (included in weighted aggregates, "
          "disclosed here):")
    print(cw[["week", "days", "cens_share_days", "cens_lost_days"]]
          .to_string(index=False) if len(cw) else "    (none)")

    # ------------------------------------------------------------------ W3
    hr("W3  GOOGLE TRENDS OVERLAY + CORRELATIONS (weekly export)")
    if not os.path.exists(TRENDS_CSV):
        print(f"  !! Trends CSV not found at:\n     {TRENDS_CSV}\n"
              "  Edit TRENDS_CSV at the top of the script and re-run; "
              "all other sections are unaffected.")
        tw = None
    else:
        tw = pd.read_csv(TRENDS_CSV)
        tw.columns = [c.strip().lower() for c in tw.columns]
        tcol = [c for c in tw.columns if c.startswith("time")][0]
        vals = [c for c in tw.columns if c != tcol]
        tw["demand"] = tw[vals].mean(axis=1)
        # Trends weeks start Sunday; ad weeks start Monday -> shift +1 day
        tw["week"] = (pd.to_datetime(tw[tcol])
                      + pd.Timedelta(days=1)).dt.date
        tw = tw[["week", "demand"]]
        mg = w.merge(tw, on="week", how="inner").sort_values("week")
        mg["contam"] = pd.to_datetime(mg["week"]) >= pd.Timestamp("2026-09-28")

        print("\n  Indexed co-movement (first 4 overlapping weeks = 100):")
        ix = mg.head(4)
        for c in ["demand", "impressions", "eligible", "cpc",
                  "budget_lost_%", "leads"]:
            base = ix[c].mean()
            mg[f"{c}_ix"] = 100 * mg[c] / base if base else np.nan
        show = ["week", "demand_ix", "impressions_ix", "eligible_ix",
                "cpc_ix", "budget_lost_%_ix", "leads_ix", "event"]
        print(mg[show].round(1).to_string(index=False))

        def corr_block(frame, label):
            print(f"\n  Correlations vs demand — {label} "
                  f"(n={len(frame)} weeks):")
            out = []
            for c in ["impressions", "eligible", "cpc", "budget_lost_%",
                      "rank_lost_%", "IS_%", "clicks", "leads"]:
                out.append({
                    "metric": c,
                    "pearson": frame["demand"].corr(frame[c]),
                    "spearman": frame["demand"].corr(frame[c],
                                                     method="spearman"),
                })
            print(pd.DataFrame(out).round(3).to_string(index=False))

        corr_block(mg, "FULL overlap window")
        corr_block(mg[~mg["contam"]], "EXCLUDING contaminated weeks "
                                      "(>= 2026-09-28)")
        m26 = mg[(pd.to_datetime(mg["week"]) >= "2026-01-01")
                 & (~mg["contam"])]
        corr_block(m26, "2026 pre-contamination only")

        print("\n  H2 denominator test: if eligible_ix rises with demand_ix "
              "while impressions_ix lags and budget_lost_% rises, exposure "
              "loss is a growing-denominator artifact, not position loss.")

    # ------------------------------------------------------------------ W4
    hr("W4  kpd WEEKLY — ethos vs rest within CR12 + iw_QS")
    kd = pd.read_sql_query("""
        SELECT p.report_date AS date,
               CASE WHEN LOWER(ki.keyword_text) LIKE '%ethos%'
                    THEN 'ethos' ELSE 'rest' END AS grp,
               SUM(p.impressions) AS impressions,
               SUM(p.clicks) AS clicks,
               SUM(p.cost_micros)/1e6 AS spend,
               SUM(p.primary_conv_count) AS leads,
               SUM(p.primary_conv_value) AS lead_value,
               SUM(CASE WHEN p.historical_quality_score IS NOT NULL
                        THEN p.historical_quality_score * p.impressions
                   END) AS qs_x_impr,
               SUM(CASE WHEN p.historical_quality_score IS NOT NULL
                        THEN p.impressions END) AS impr_rated
        FROM keyword_performance_daily p
        JOIN keyword_instances ki ON ki.id=p.instance_id
        WHERE ki.account_id=? AND p.campaign_id=? AND p.device='ALL'
          AND p.report_date>=?
        GROUP BY 1,2
    """, con, params=(ACCOUNT_ID, CR12_ID, WINDOW_START))
    kd["week"] = week_start(kd["date"])
    for c in ["qs_x_impr", "impr_rated"]:
        kd[c] = pd.to_numeric(kd[c], errors="coerce")
    kw = kd.groupby(["week", "grp"], as_index=False).agg(
        impressions=("impressions", "sum"), clicks=("clicks", "sum"),
        spend=("spend", "sum"), leads=("leads", "sum"),
        lead_value=("lead_value", "sum"),
        qs_x_impr=("qs_x_impr", "sum"), impr_rated=("impr_rated", "sum"))
    kw["cpc"] = kw["spend"] / kw["clicks"]

    if kw["impr_rated"].fillna(0).sum() > 0:
        kw["iw_QS"] = kw["qs_x_impr"] / kw["impr_rated"]
        kw["rated_share_%"] = 100 * kw["impr_rated"] / kw["impressions"]
    else:
        print("  [note] kpd.historical_quality_score unpopulated for acct "
              "29 — using keyword_quality_daily per the access charter.")
        kw["iw_QS"] = np.nan
        kw["rated_share_%"] = np.nan
        try:
            if con.execute("SELECT 1 FROM sqlite_master WHERE name="
                           "'keyword_quality_daily'").fetchone():
                qc = cols(con, "keyword_quality_daily")
                print(f"  keyword_quality_daily columns: {qc}")
                datec = "report_date" if "report_date" in qc else "date"
                qsc = next((c for c in ["quality_score", "qs",
                                        "historical_quality_score"]
                            if c in qc), None)
                devp = "AND q.device='ALL'" if "device" in qc else ""
                if qsc and "impressions" in qc:
                    qsql = f"""
                        SELECT q.{datec} AS date,
                               CASE WHEN LOWER(ki.keyword_text)
                                    LIKE '%ethos%' THEN 'ethos'
                                    ELSE 'rest' END AS grp,
                               SUM(q.{qsc} * q.impressions) AS qxi,
                               SUM(q.impressions) AS impr_rated
                        FROM keyword_quality_daily q
                        JOIN keyword_instances ki ON ki.id=q.instance_id
                        WHERE ki.account_id={ACCOUNT_ID}
                          AND ki.campaign_id={CR12_ID}
                          AND q.{datec}>='{WINDOW_START}'
                          AND q.{qsc} IS NOT NULL {devp}
                        GROUP BY 1,2"""
                elif qsc:
                    qsql = f"""
                        SELECT q.{datec} AS date,
                               CASE WHEN LOWER(ki.keyword_text)
                                    LIKE '%ethos%' THEN 'ethos'
                                    ELSE 'rest' END AS grp,
                               SUM(q.{qsc} * p.impressions) AS qxi,
                               SUM(p.impressions) AS impr_rated
                        FROM keyword_quality_daily q
                        JOIN keyword_instances ki ON ki.id=q.instance_id
                        JOIN keyword_performance_daily p
                             ON p.instance_id=q.instance_id
                            AND p.report_date=q.{datec}
                            AND p.device='ALL'
                        WHERE ki.account_id={ACCOUNT_ID}
                          AND ki.campaign_id={CR12_ID}
                          AND q.{datec}>='{WINDOW_START}'
                          AND q.{qsc} IS NOT NULL {devp}
                        GROUP BY 1,2"""
                else:
                    qsql = None
                    print("  !! no QS column resolved in "
                          "keyword_quality_daily — escalate to architect.")
                if qsql:
                    qd = pd.read_sql_query(qsql, con)
                    qd["week"] = week_start(qd["date"])
                    qw = qd.groupby(["week", "grp"], as_index=False).agg(
                        qxi=("qxi", "sum"),
                        impr_rated_q=("impr_rated", "sum"))
                    qw["iw_QS_q"] = qw["qxi"] / qw["impr_rated_q"]
                    kw = kw.drop(columns=["iw_QS", "rated_share_%"]).merge(
                        qw[["week", "grp", "iw_QS_q", "impr_rated_q"]],
                        on=["week", "grp"], how="left")
                    kw = kw.rename(columns={"iw_QS_q": "iw_QS"})
                    kw["rated_share_%"] = (100 * kw["impr_rated_q"]
                                           / kw["impressions"])
            else:
                print("  !! keyword_quality_daily not found — QS columns "
                      "will print NaN; escalate to architect.")
        except Exception as exc:
            print(f"  !! QS fallback failed ({exc}) — QS columns print "
                  f"NaN; escalate to architect. Other sections unaffected.")
            if "iw_QS" not in kw.columns:
                kw["iw_QS"] = np.nan
            if "rated_share_%" not in kw.columns:
                kw["rated_share_%"] = np.nan

    for g in ["ethos", "rest"]:
        sub = kw[kw["grp"] == g][["week", "impressions", "clicks", "spend",
                                  "cpc", "leads", "lead_value", "iw_QS",
                                  "rated_share_%"]]
        print(f"\n  CR12 / {g.upper()} weekly:")
        print(sub.round(2).to_string(index=False))

    # ------------------------------------------------------------------ W5
    hr("W5  sqp WEEKLY — CR12 ethos keywords: match_type_served mix + tail")
    print("  CAVEAT: weekly sqp suppression is worse than monthly — shares "
          "are directional, absolute counts are lower bounds.")
    sq = pd.read_sql_query("""
        SELECT p.report_date AS date, p.query_id,
               p.match_type_served,
               SUM(p.impressions) AS impressions,
               SUM(p.clicks) AS clicks,
               SUM(p.primary_conv_count) AS leads
        FROM search_query_performance p
        JOIN keyword_instances ki ON ki.id=p.instance_id
                                  AND ki.account_id=?
        WHERE p.campaign_id=? AND p.platform='google' AND p.device='ALL'
          AND p.report_date>=?
          AND LOWER(ki.keyword_text) LIKE '%ethos%'
        GROUP BY 1,2,3
    """, con, params=(ACCOUNT_ID, CR12_ID, WINDOW_START))
    sq["week"] = week_start(sq["date"])

    mt = (sq.groupby(["week", "match_type_served"])["impressions"].sum()
            .unstack(fill_value=0))
    mt_share = (100 * mt.div(mt.sum(axis=1), axis=0)).round(1)
    print("\n  match_type_served — share of ethos impressions by week (%):")
    print(mt_share.to_string())

    rows = []
    for wk, g in sq.groupby("week"):
        q = g.groupby("query_id")["impressions"].sum() \
             .sort_values(ascending=False)
        ti = q.sum()
        rows.append({
            "week": wk, "distinct_queries": len(q),
            "top1_%": 100 * q.iloc[0] / ti if ti else np.nan,
            "top10_%": 100 * q.head(10).sum() / ti if ti else np.nan,
            "tail_impr_%": (100 * q.iloc[10:].sum() / ti
                            if len(q) > 10 else 0.0),
            "sqp_impr": ti,
        })
    tail = pd.DataFrame(rows)
    tail["event"] = tail["week"].map(event_label)
    print("\n  Ethos query tail by week:")
    print(tail.round(1).to_string(index=False))

    # ------------------------------------------------------------------ W6
    hr("W6  LAG_SENSITIVE DUAL VIEW — last 4 weeks, full vs "
       "minus-last-2-days")
    mxd = pd.Timestamp(mx)
    cut = (mxd - pd.Timedelta(days=2)).date().isoformat()
    last4 = sorted(w["week"].unique())[-4:]
    full = w[w["week"].isin(last4)][["week", "impressions", "clicks",
                                     "spend", "leads", "val_per_lead",
                                     "IS_%", "budget_lost_%"]]
    print(f"\n  FULL (through {mx}):")
    print(full.round(2).to_string(index=False))
    d2 = d[d["date"] <= cut].copy()
    w2 = d2.groupby("week").agg(
        impressions=("impressions", "sum"), clicks=("clicks", "sum"),
        spend=("spend", "sum"), leads=("leads", "sum"),
        lead_value=("lead_value", "sum"),
        impr_rated=("impr_rated", "sum"), eligible=("eligible", "sum"),
        bl_elig=("bl_elig", "sum")).reset_index()
    w2["val_per_lead"] = w2["lead_value"] / w2["leads"]
    w2["IS_%"] = 100 * w2["impr_rated"] / w2["eligible"]
    w2["budget_lost_%"] = 100 * w2["bl_elig"] / w2["eligible"]
    w2 = w2[w2["week"].isin(last4)][["week", "impressions", "clicks",
                                     "spend", "leads", "val_per_lead",
                                     "IS_%", "budget_lost_%"]]
    print(f"\n  MINUS-LAST-2-DAYS (through {cut})  [LAG_SENSITIVE]:")
    print(w2.round(2).to_string(index=False))
    print("\n  Conversions restate upward ~7 days; treat the freshest two "
          "weeks as provisional in both views.")

    print("\nDone. Paste full output back to the analysis thread.")
    con.close()


if __name__ == "__main__":
    main()
