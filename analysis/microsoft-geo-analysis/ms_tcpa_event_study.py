"""
ms_tcpa_event_study.py — Step 2 of the Microsoft Home tCPA Elasticity &
Lag-Profile Study. Read-only. Consumes Deliverable 0 (event_inventory.csv).

PURPOSE
  Pre-register how the geo re-tier's tCPA moves should behave, from this
  account's own raises: (1) how much click/lead volume a +1% tCPA buys,
  (2) the timing shape (CPC first, leads later). The rollout plan's
  Expected Behavior sheet reads the two CSVs this script writes.

SCOPE (inherited from the inventory; nothing re-decided here)
  - Events graded CLEAN or USABLE_FLAGGED only.
  - SEARCH NETWORK ONLY: campaign_performance_daily (search by design),
    device='ALL'. No audience table is read.
  - Windows exactly as the inventory set them (boundaries, truncation).

METHOD
  Baseline: every post day is compared to the mean of PRE days on the same
  weekday; weekdays missing from the pre window fall back to the pre mean.
  No smoothing in any computation (chart only).
  Lift   = Σ post actual ÷ Σ post DOW-matched baseline − 1  (clicks, leads,
           spend, revenue); CPC/CPL/ROAS are ratios of those sums.
  Elasticity = lift ÷ tCPA change.
  POOLED elasticity = Σ w·lift ÷ Σ w·Δtcpa, w = pre spend/day. This is a
  weighted slope, stable when single events are small (+5%) moves —
  averaging per-event ratios would let a 5% denominator blow up noise.
  Range = day-level bootstrap (resample pre and post days within each
  event, 2,000 reps), 10th–90th percentile.
  ROAS uses revenue-complete days only (latest day with account revenue,
  minus --revenue-lag-days).

OUTPUTS (--out-dir)
  elasticity_by_event.csv  one row per event + POOLED row (charter schema
                           plus ranges/ROAS/grade columns)
  pooled_summary.csv       pooled elasticities under several event sets
                           (all, search-only, without the 8/20 cluster,
                           leave-one-out) — the robustness read
  lag_profile.csv          day_rel −7..+21: cpc/clicks/leads index vs
                           DOW-matched pre baseline (=100), n_events
  lag_profile.png          same, 3-day smoothed for the eye
  study_summary.txt        headline numbers + derived timing scalars

USAGE
  python ms_tcpa_event_study.py
  python ms_tcpa_event_study.py --inventory path\\to\\event_inventory.csv
"""

import argparse
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

ACCOUNT_ID = 2
DEFAULT_DB_PATH = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")
DEFAULT_DIR = Path(r"C:\Projects\google-ads-ops\analysis\output\ms_tcpa_study")
STUDY_END = date(2026, 9, 17)
USABLE = ("CLEAN", "USABLE_FLAGGED")
N_BOOT = 2000
PCT_LO, PCT_HI = 10, 90
DAY_REL = range(-7, 22)
ADDITIVE = ["clicks", "leads", "spend", "revenue"]


# ═══════════════════════════════════════════════════════════════════
# DATA
# ═══════════════════════════════════════════════════════════════════

def load_inventory(path: Path) -> pd.DataFrame:
    inv = pd.read_csv(path)
    for c in ("change_date", "day0", "pre_start", "pre_end", "post_start", "post_end"):
        inv[c] = pd.to_datetime(inv[c]).dt.date
    inv = inv[inv["grade"].isin(USABLE)].copy()
    if inv.empty:
        sys.exit("ERROR: no CLEAN/USABLE_FLAGGED events in the inventory")
    inv["campaign_id"] = inv["campaign_id"].astype(int)
    return inv.reset_index(drop=True)


def fetch_daily(conn, ids, start, end) -> pd.DataFrame:
    cols = [r[1] for r in conn.execute("PRAGMA table_info(campaign_performance_daily)")]
    dcol = "date" if "date" in cols else "report_date"
    rcol = next((c for c in ("all_conversion_value", "conversion_value") if c in cols), None)
    if rcol is None:
        sys.exit(f"ERROR: no revenue column in campaign_performance_daily: {cols}")
    q = f"""
        SELECT cpd.campaign_id, cpd.{dcol} AS dt,
               SUM(cpd.cost_micros)/1e6 AS spend, SUM(cpd.clicks) AS clicks,
               SUM(cpd.conversions) AS leads, SUM(cpd.{rcol}) AS revenue
        FROM campaign_performance_daily cpd
        JOIN campaigns c ON c.campaign_id = cpd.campaign_id
        WHERE c.account_id = ? AND cpd.device = 'ALL'
          AND cpd.{dcol} BETWEEN ? AND ?
          AND CAST(cpd.campaign_id AS INTEGER) IN ({",".join("?" * len(ids))})
        GROUP BY 1, 2"""
    df = pd.read_sql(q, conn, params=(ACCOUNT_ID, start.isoformat(), end.isoformat(), *ids))
    df["campaign_id"] = pd.to_numeric(df["campaign_id"]).astype(int)
    df["dt"] = pd.to_datetime(df["dt"]).dt.date
    print(f"  Revenue column: {rcol} (search rows)")
    return df


def revenue_complete_through(conn, lag_days: int) -> date:
    row = conn.execute("""
        SELECT MAX(cpd.date) FROM campaign_performance_daily cpd
        JOIN campaigns c ON c.campaign_id = cpd.campaign_id
        WHERE c.account_id = ? AND cpd.device = 'ALL'
          AND COALESCE(cpd.all_conversion_value, 0) > 0""", (ACCOUNT_ID,)).fetchone()
    last = pd.to_datetime(row[0]).date() if row and row[0] else STUDY_END
    return last - timedelta(days=lag_days)


def series_for(daily, cid, start, end) -> pd.DataFrame:
    """Calendar-complete daily frame (missing days = 0 activity)."""
    idx = pd.date_range(start, end, freq="D").date
    s = daily[daily["campaign_id"] == cid].set_index("dt")[ADDITIVE]
    s = s.reindex(idx).fillna(0.0)
    s["dow"] = [d.weekday() for d in idx]
    return s


# ═══════════════════════════════════════════════════════════════════
# CORE ESTIMATOR
# ═══════════════════════════════════════════════════════════════════

def dow_baseline(pre: pd.DataFrame, dows) -> pd.DataFrame:
    """Expected value for each requested weekday from the pre window."""
    by = pre.groupby("dow")[ADDITIVE].mean()
    overall = pre[ADDITIVE].mean()
    return pd.DataFrame([by.loc[d] if d in by.index else overall for d in dows])


def event_lifts(pre: pd.DataFrame, post: pd.DataFrame, rev_ok: np.ndarray) -> dict:
    base = dow_baseline(pre, post["dow"].values)
    act = post[ADDITIVE].sum()
    exp = base.sum()
    out = {f"{m}_lift": (act[m] / exp[m] - 1) if exp[m] > 0 else np.nan
           for m in ("clicks", "leads", "spend")}
    out["cpc_change"] = ((act["spend"] / act["clicks"]) / (exp["spend"] / exp["clicks"]) - 1
                         if act["clicks"] > 0 and exp["clicks"] > 0 else np.nan)
    out["cpl_change"] = ((act["spend"] / act["leads"]) / (exp["spend"] / exp["leads"]) - 1
                         if act["leads"] > 0 and exp["leads"] > 0 else np.nan)
    # ROAS on revenue-complete post days only
    pr = post[rev_ok]
    if len(pr):
        b = dow_baseline(pre, pr["dow"].values).sum()
        a = pr[ADDITIVE].sum()
        out["post_roas"] = a["revenue"] / a["spend"] if a["spend"] > 0 else np.nan
        out["expected_roas"] = b["revenue"] / b["spend"] if b["spend"] > 0 else np.nan
    else:
        out["post_roas"] = out["expected_roas"] = np.nan
    out["post_expected_clicks_per_day"] = exp["clicks"] / len(post)
    out["post_expected_leads_per_day"] = exp["leads"] / len(post)
    return out


def pooled(lifts: dict, dtcpa: dict, w: dict, ids, metric) -> float:
    num = sum(w[i] * lifts[i][metric] for i in ids if np.isfinite(lifts[i][metric]))
    den = sum(w[i] * dtcpa[i] for i in ids if np.isfinite(lifts[i][metric]))
    return num / den if den else np.nan


# ═══════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════

def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db-path", default=str(DEFAULT_DB_PATH))
    p.add_argument("--inventory", default=str(DEFAULT_DIR / "event_inventory.csv"))
    p.add_argument("--out-dir", default=str(DEFAULT_DIR))
    p.add_argument("--revenue-lag-days", type=int, default=1)
    p.add_argument("--seed", type=int, default=7)
    a = p.parse_args()
    rng = np.random.default_rng(a.seed)
    out = Path(a.out_dir)
    out.mkdir(parents=True, exist_ok=True)

    inv = load_inventory(Path(a.inventory))
    conn = sqlite3.connect(a.db_path)
    lo = min(inv["pre_start"].min(), (inv["day0"] - timedelta(days=7)).min())
    daily = fetch_daily(conn, inv["campaign_id"].unique().tolist(), lo, STUDY_END)
    rev_through = revenue_complete_through(conn, a.revenue_lag_days)
    conn.close()

    print("═" * 70)
    print(f"MS HOME tCPA EVENT STUDY — {len(inv)} events, search network only")
    print(f"  ROAS uses revenue-complete days through {rev_through}")
    print("═" * 70)

    ev, lifts, dtcpa, w, pre_sets, post_sets, rev_masks = {}, {}, {}, {}, {}, {}, {}
    rows = []
    for _, e in inv.iterrows():
        eid, cid = e["event_id"], e["campaign_id"]
        pre = series_for(daily, cid, e["pre_start"], e["pre_end"])
        post = series_for(daily, cid, e["post_start"], e["post_end"])
        rev_ok = np.array([d <= rev_through for d in post.index])
        L = event_lifts(pre, post, rev_ok)
        ev[eid], lifts[eid], dtcpa[eid] = e, L, e["tcpa_change_pct"]
        w[eid] = pre["spend"].mean()
        pre_sets[eid], post_sets[eid], rev_masks[eid] = pre, post, rev_ok
        pre_tot, post_tot = pre[ADDITIVE].sum(), post[ADDITIVE].sum()
        rows.append({
            "event_id": eid, "grade": e["grade"], "regime": e["regime"],
            "event_date": e["change_date"], "day0": e["day0"],
            "campaign_id": cid, "campaign_key": e["campaign_key"], "campaign": e["campaign"],
            "tcpa_from": e["tcpa_from"], "tcpa_to": e["tcpa_to"],
            "tcpa_change_pct": e["tcpa_change_pct"],
            "pre_days": len(pre), "post_days": len(post),
            "pre_spend_per_day": pre_tot["spend"] / len(pre),
            "pre_clicks_per_day": pre_tot["clicks"] / len(pre),
            "post_clicks_per_day": post_tot["clicks"] / len(post),
            "post_expected_clicks_per_day": L["post_expected_clicks_per_day"],
            "click_lift_pct": L["clicks_lift"],
            "click_elasticity": L["clicks_lift"] / e["tcpa_change_pct"],
            "pre_leads_per_day": pre_tot["leads"] / len(pre),
            "post_leads_per_day": post_tot["leads"] / len(post),
            "post_expected_leads_per_day": L["post_expected_leads_per_day"],
            "lead_lift_pct": L["leads_lift"],
            "lead_elasticity": L["leads_lift"] / e["tcpa_change_pct"],
            "pre_cpc": pre_tot["spend"] / pre_tot["clicks"] if pre_tot["clicks"] else np.nan,
            "post_cpc": post_tot["spend"] / post_tot["clicks"] if post_tot["clicks"] else np.nan,
            "cpc_change_pct": L["cpc_change"], "cpl_change_pct": L["cpl_change"],
            "spend_lift_pct": L["spend_lift"],
            "expected_roas": L["expected_roas"], "post_roas": L["post_roas"],
            "confound_notes": "; ".join(x for x in (
                f"regime={e['regime']}",
                e["confound_flags"] if isinstance(e["confound_flags"], str) else "",
                e["window_notes"] if isinstance(e["window_notes"], str) else "") if x),
        })

    # ── Bootstrap ────────────────────────────────────────────────
    ids = list(ev)
    boot = {i: [] for i in ids}
    for _ in range(N_BOOT):
        for i in ids:
            pr, po = pre_sets[i], post_sets[i]
            pi = rng.integers(0, len(pr), len(pr))
            qi = rng.integers(0, len(po), len(po))
            boot[i].append(event_lifts(pr.iloc[pi], po.iloc[qi], rev_masks[i][qi]))

    def pooled_boot(sel, metric):
        vals = []
        for b in range(N_BOOT):
            num = den = 0.0
            for i in sel:
                v = boot[i][b][metric]
                if np.isfinite(v):
                    num += w[i] * v
                    den += w[i] * dtcpa[i]
            vals.append(num / den if den else np.nan)
        return np.nanpercentile(vals, [PCT_LO, PCT_HI])

    for r in rows:
        i = r["event_id"]
        for m, col in (("clicks_lift", "click"), ("leads_lift", "lead")):
            v = np.array([b[m] for b in boot[i]]) / dtcpa[i]
            r[f"{col}_elasticity_lo"], r[f"{col}_elasticity_hi"] = np.nanpercentile(v, [PCT_LO, PCT_HI])

    # ── Pooled variants ──────────────────────────────────────────
    cluster_820 = [i for i in ids if ev[i]["change_date"] == date(2026, 8, 20)]
    variants = [("ALL usable", ids),
                ("search-only", [i for i in ids if ev[i]["regime"] == "search_only"]),
                ("CLEAN only", [i for i in ids if ev[i]["grade"] == "CLEAN"]),
                ("8/20 cluster only", cluster_820),
                ("without 8/20 cluster", [i for i in ids if i not in cluster_820])]
    variants += [(f"leave out {i} ({ev[i]['campaign_key']} {ev[i]['change_date']})",
                  [j for j in ids if j != i]) for i in ids]
    prow = []
    for label, sel in variants:
        if not sel:
            continue
        rec = {"event_set": label, "n_events": len(sel),
               "events": " ".join(sel),
               "weighted_tcpa_change_pct": sum(w[i] * dtcpa[i] for i in sel) / sum(w[i] for i in sel)}
        for m, col in (("clicks_lift", "click"), ("leads_lift", "lead"),
                       ("cpc_change", "cpc"), ("spend_lift", "spend")):
            rec[f"{col}_elasticity"] = pooled(lifts, dtcpa, w, sel, m)
            rec[f"{col}_elasticity_lo"], rec[f"{col}_elasticity_hi"] = pooled_boot(sel, m)
        prow.append(rec)
    pooled_df = pd.DataFrame(prow)
    pooled_df.to_csv(out / "pooled_summary.csv", index=False)

    P = pooled_df.iloc[0]
    rows.append({"event_id": "POOLED", "grade": "spend-weighted slope", "regime": "all usable",
                 "tcpa_change_pct": P["weighted_tcpa_change_pct"],
                 "click_elasticity": P["click_elasticity"],
                 "click_elasticity_lo": P["click_elasticity_lo"],
                 "click_elasticity_hi": P["click_elasticity_hi"],
                 "lead_elasticity": P["lead_elasticity"],
                 "lead_elasticity_lo": P["lead_elasticity_lo"],
                 "lead_elasticity_hi": P["lead_elasticity_hi"],
                 "cpc_change_pct": P["cpc_elasticity"] * P["weighted_tcpa_change_pct"],
                 "confound_notes": f"n={len(ids)}; range = bootstrap P{PCT_LO}-P{PCT_HI}; "
                                   f"see pooled_summary.csv for robustness sets"})
    charter = ["event_date", "campaign_id", "campaign", "tcpa_from", "tcpa_to",
               "tcpa_change_pct", "pre_clicks_per_day", "post_clicks_per_day",
               "click_lift_pct", "click_elasticity", "pre_leads_per_day",
               "post_leads_per_day", "lead_lift_pct", "lead_elasticity", "pre_cpc",
               "post_cpc", "cpc_change_pct", "confound_notes"]
    extra = ["event_id", "grade", "regime", "campaign_key", "day0", "pre_days", "post_days",
             "pre_spend_per_day", "post_expected_clicks_per_day",
             "post_expected_leads_per_day", "click_elasticity_lo", "click_elasticity_hi",
             "lead_elasticity_lo", "lead_elasticity_hi", "cpl_change_pct",
             "spend_lift_pct", "expected_roas", "post_roas"]
    el = pd.DataFrame(rows)[charter + extra]
    el.to_csv(out / "elasticity_by_event.csv", index=False)

    # ── Lag profile ──────────────────────────────────────────────
    acc = {k: {m: [0.0, 0.0] for m in ("spend", "clicks", "leads")} for k in DAY_REL}
    n_ev = {k: 0 for k in DAY_REL}
    for i in ids:
        e = ev[i]
        pre = pre_sets[i]
        post_limit = e["post_end"] if e["post_end"] < e["day0"] + timedelta(days=16) \
            else min(e["day0"] + timedelta(days=21), STUDY_END)
        lo_d = max(e["pre_start"], e["day0"] - timedelta(days=7))
        s = series_for(daily, e["campaign_id"], lo_d, post_limit)
        base = dow_baseline(pre, s["dow"].values)
        base.index = s.index
        for d in s.index:
            k = (d - e["day0"]).days
            if k not in n_ev:
                continue
            n_ev[k] += 1
            for m in ("spend", "clicks", "leads"):
                acc[k][m][0] += s.at[d, m]
                acc[k][m][1] += base.at[d, m]
    lag = []
    for k in DAY_REL:
        a_, n = acc[k], n_ev[k]
        def idx(m):
            return 100 * a_[m][0] / a_[m][1] if a_[m][1] > 0 else np.nan
        cpc = (100 * (a_["spend"][0] / a_["clicks"][0]) / (a_["spend"][1] / a_["clicks"][1])
               if a_["clicks"][0] > 0 and a_["clicks"][1] > 0 else np.nan)
        lag.append({"day_rel": k, "cpc_index": cpc, "clicks_index": idx("clicks"),
                    "leads_index": idx("leads"), "n_events": n})
    lag = pd.DataFrame(lag)
    lag.to_csv(out / "lag_profile.csv", index=False)

    # ── Derived timing scalars (raw index, n_events >= 3) ────────
    ok = lag[(lag["n_events"] >= 3)]
    post_ok = ok[ok["day_rel"] >= 0]
    cpc_peak = int(post_ok.loc[post_ok["cpc_index"].idxmax(), "day_rel"]) if len(post_ok) else None
    li = post_ok.set_index("day_rel")["leads_index"]
    crossover = next((d for d in li.index
                      if all(li.get(d + j, np.nan) > 100 for j in range(3))), None)
    ci = post_ok.set_index("day_rel")["clicks_index"]
    plateau = None
    if len(ci) >= 8:
        tail = ci[ci.index >= ci.index.max() - 6].mean()
        roll = ci.rolling(3, min_periods=3).mean()
        plateau = next((d for d in roll.index
                        if np.isfinite(roll[d]) and (roll[roll.index >= d] - tail).abs().max() <= 10),
                       None)

    # ── Chart (3-day smoothing for display only) ─────────────────
    fig, ax = plt.subplots(figsize=(11, 5.5))
    for col, c, lbl in (("cpc_index", "#C0504D", "CPC"), ("clicks_index", "#4F81BD", "Clicks"),
                        ("leads_index", "#9BBB59", "Leads")):
        ax.plot(lag["day_rel"], lag[col], color=c, alpha=0.25, linewidth=1)
        ax.plot(lag["day_rel"], lag[col].rolling(3, center=True, min_periods=2).mean(),
                color=c, linewidth=2.2, label=f"{lbl} (3-day smoothed)")
    ax.axhline(100, color="black", linewidth=0.8)
    ax.axvspan(-1, 0, color="gray", alpha=0.15, label="change day (transition)")
    for (k, v), yf in zip(((cpc_peak, "CPC peak"), (crossover, "leads crossover"),
                           (plateau, "plateau")), (0.98, 0.70, 0.42)):
        if k is not None:
            ax.axvline(k, linestyle="--", color="gray", linewidth=1)
            ax.annotate(v, (k, yf), xycoords=("data", "axes fraction"), xytext=(3, 0),
                        textcoords="offset points", rotation=90, va="top", fontsize=8)
    ax2 = ax.twinx()
    ax2.bar(lag["day_rel"], lag["n_events"], color="gray", alpha=0.12)
    ax2.set_ylabel("events contributing", fontsize=8)
    ax2.set_ylim(0, max(lag["n_events"].max() * 4, 1))
    ax.set_xlabel("days from day 0 (first full day at new tCPA)")
    ax.set_ylabel("index vs same-weekday pre baseline (=100)")
    ax.set_title(f"MS Home tCPA raises — pooled lag profile ({len(ids)} events, search only)",
                 fontweight="bold", loc="left")
    ax.legend(loc="upper left", fontsize=8)
    ax.grid(alpha=0.3)
    fig.savefig(out / "lag_profile.png", dpi=110, bbox_inches="tight")
    plt.close(fig)

    # ── Summary ──────────────────────────────────────────────────
    def fmt(v, pct=False):
        return "n/a" if v is None or not np.isfinite(v) else (f"{v:+.0%}" if pct else f"{v:+.2f}")
    lines = [f"MS HOME tCPA EVENT STUDY — {len(ids)} usable raises (search network only)",
             f"Revenue-complete through {rev_through}", "",
             "PER EVENT (lift vs same-weekday baseline; elasticity = lift / tCPA change)"]
    for r in rows[:-1]:
        lines.append(f"  {r['event_id']} {r['campaign_key']:<3} {r['event_date']} tCPA "
                     f"{r['tcpa_change_pct']:+.1%} | clicks {fmt(r['click_lift_pct'], True)} "
                     f"leads {fmt(r['lead_lift_pct'], True)} CPC {fmt(r['cpc_change_pct'], True)} "
                     f"| lead elasticity {fmt(r['lead_elasticity'])} "
                     f"[{fmt(r['lead_elasticity_lo'])}, {fmt(r['lead_elasticity_hi'])}]")
    lines += ["", f"POOLED (spend-weighted slope; P{PCT_LO}-P{PCT_HI} bootstrap range)"]
    for _, r in pooled_df.iterrows():
        lines.append(f"  {r['event_set']:<48} n={r['n_events']}  clicks {fmt(r['click_elasticity'])} "
                     f"[{fmt(r['click_elasticity_lo'])}, {fmt(r['click_elasticity_hi'])}]  "
                     f"leads {fmt(r['lead_elasticity'])} "
                     f"[{fmt(r['lead_elasticity_lo'])}, {fmt(r['lead_elasticity_hi'])}]  "
                     f"CPC {fmt(r['cpc_elasticity'])}")
    lines += ["", "TIMING (pooled raw index, days with >=3 events)",
              f"  CPC peak day:         {cpc_peak}",
              f"  Leads crossover day:  {crossover}  (first of 3 straight days >100)",
              f"  Plateau day:          {plateau}  (clicks 3-day mean stays within ±10 pts of final week)",
              "", "Read: a 15% ladder step x pooled lead elasticity = expected lead lift for that step."]
    txt = "\n".join(lines)
    (out / "study_summary.txt").write_text(txt, encoding="utf-8")
    print(txt)
    print(f"\n  ✓ outputs in {out}")


if __name__ == "__main__":
    main()
