"""
geico_cohort_timeline.py -- Brand-cohort performance timeline with a classified change-history
event track, for the Geico ad-group cohort across ALL campaigns, 2025-01-01 -> today.

WHY this is its own script (not a fork of the tROAS visualizer): the unit is a BRAND COHORT
(ad groups named *Geico*), not a campaign; the grain is ad-group; the event model is a 3-lane
track classified by what the change targeted; and it covers every metric, not just ROAS.

SOURCE CONTRACT (DB-architect ruling 2026-08-12, primary_conv_* REPAIRED same day):
  * All metrics from ad_group_performance_daily, device='ALL', account_id=1.
  * Revenue = conversion_value (verified). Leads = primary_conv_count / primary_conv_value
    (repaired; verified vs kpd truth to <0.1%). native `conversions` is Zip-inclusive -> NOT leads.
  * Cohort = ad_group_name LIKE '%Geico%'; multiple ids per name is the cohort (sum freely).
  * Segment: 'renters' if name contains 'renter' else 'home' (renters line ends at 2025-12-22
    restructure -- shown as its own series, not merged, so the renters-pause reads as itself).
  * QA: one kpd cross-check kept as a standing assertion, not a data dependency.

EVENT TRACK -- 3 lanes, classified by target (from the change categorizer + ad_group_col):
  * geico        -- change hit a Geico ad group (ad_group_col contains 'geico')  [causal]
  * other_adgroup-- change to a non-Geico ad group in these campaigns            [context]
  * campaign     -- ad_group_col empty => whole-campaign change (budget/status/tROAS/geo) [context]
  2025 events from google_change_csv_2025_raw; 2026 from google_change_csv_raw.

Era bands from account_structure_era shade the structural regimes (so a level-shift at the
2025-12-22 tier restructure isn't misread as a Geico performance change).

Usage:
    python geico_cohort_timeline.py --db PATH                     # -> PNG(s) in --out
    python geico_cohort_timeline.py --db PATH --brand Geico --out ./output
"""
import argparse
import collections
import datetime
import io
import os
import sqlite3
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import numpy as np
import pandas as pd

CHANGE_HISTORY_DIR = os.environ.get("CHANGE_HISTORY_DIR",
                                    r"C:\Projects\google-ads-ops\campaign_db_scripts\change_history")
ACCOUNT_ID = 1
ROLL = 7  # rolling-average window (days)


# ───────────────────────── cohort performance ─────────────────────────
def load_cohort_daily(conn, brand, account=ACCOUNT_ID):
    """Daily Geico-cohort metrics across all campaigns, segmented home/renters.
    All from ad_group_performance_daily, device='ALL'. Ratios computed later from sums."""
    sql = ("SELECT date, ad_group_name, "
           "       SUM(impressions) impr, SUM(clicks) clicks, SUM(cost_micros) cost_micros, "
           "       SUM(conversion_value) revenue, SUM(primary_conv_count) leads, "
           "       SUM(primary_conv_value) lead_value, "
           "       SUM(impressions*search_impression_share) sis_num, SUM(impressions) sis_den "
           "FROM ad_group_performance_daily "
           "WHERE account_id=? AND device='ALL' AND lower(ad_group_name) LIKE ? "
           "GROUP BY date, ad_group_name")
    rows = conn.execute(sql, (account, "%" + brand.lower() + "%")).fetchall()
    recs = []
    for (d, name, impr, clk, cost_micros, rev, leads, lead_val, sis_num, sis_den) in rows:
        seg = "renters" if "renter" in (name or "").lower() else "home"
        recs.append({"date": d, "segment": seg,
                     "impr": impr or 0, "clicks": clk or 0,
                     "cost": (cost_micros or 0) / 1e6, "revenue": rev or 0.0,
                     "leads": leads or 0, "lead_value": lead_val or 0.0,
                     "sis_num": sis_num or 0.0, "sis_den": sis_den or 0})
    if not recs:
        return pd.DataFrame()
    df = pd.DataFrame(recs)
    df["date"] = pd.to_datetime(df["date"])
    return df


def rollup(df, segments):
    """Sum the cohort to daily totals for the chosen segment(s); derive ratio metrics from sums."""
    d = df[df["segment"].isin(segments)] if segments else df
    g = d.groupby("date", as_index=False).sum(numeric_only=True)
    g = g.sort_values("date").reset_index(drop=True)
    # ratios from summed numerator/denominator (never averaged daily ratios)
    g["ctr"] = np.where(g["impr"] > 0, g["clicks"] / g["impr"], np.nan)
    g["cvr"] = np.where(g["clicks"] > 0, g["leads"] / g["clicks"], np.nan)
    g["roas"] = np.where(g["cost"] > 0, g["revenue"] / g["cost"], np.nan)
    g["cpa"] = np.where(g["leads"] > 0, g["cost"] / g["leads"], np.nan)
    g["sis"] = np.where(g["sis_den"] > 0, g["sis_num"] / g["sis_den"], np.nan)
    return g


# ───────────────────────── event track ─────────────────────────
def _import_taxonomy():
    if CHANGE_HISTORY_DIR not in sys.path:
        sys.path.insert(0, CHANGE_HISTORY_DIR)
    import change_taxonomy as T
    return T


def load_events(conn, brand, start, end, account=ACCOUNT_ID):
    """Classified change events across both eras. Returns per-(date,lane) counts + the
    Geico-lane individual events (for annotated markers)."""
    T = _import_taxonomy()
    b = brand.lower()
    lane_daily = collections.defaultdict(lambda: collections.Counter())  # date -> lane -> count
    geico_events = []  # individual causal events

    def classify_lane(ag_col):
        agl = (ag_col or "").lower()
        if not agl.strip():
            return "campaign"
        return "geico" if b in agl else "other_adgroup"

    def ingest(table, datecol):
        q = ("SELECT %s, campaign_col, ad_group_col, changes_raw FROM %s "
             "WHERE account_id=? AND substr(%s,1,10) BETWEEN ? AND ?"
             % (datecol, table, datecol))
        for dt, camp, ag, raw in conn.execute(q, (account, start, end)):
            d = (dt or "")[:10]
            lane = classify_lane(ag)
            recs = T.categorize_cell(raw)
            lane_daily[d][lane] += len(recs) if recs else 1
            if lane == "geico":
                cats = ",".join(sorted({r["category"] for r in recs})) if recs else "change"
                geico_events.append({"date": d, "campaign": camp or "", "cats": cats})

    for tbl in ("google_change_csv_2025_raw", "google_change_csv_raw"):
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (tbl,)).fetchone():
            ingest(tbl, "change_datetime")
    return lane_daily, geico_events


def load_eras(conn, account=ACCOUNT_ID):
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='account_structure_era'").fetchone():
        return []
    out = []
    for era_key, vf, vt in conn.execute(
            "SELECT era_key, valid_from, valid_to FROM account_structure_era "
            "WHERE account_id=? AND era_key NOT LIKE '\\_%' ESCAPE '\\' ORDER BY valid_from", (account,)):
        out.append((era_key, vf, vt or "2100-01-01"))
    return out


# ───────────────────────── render ─────────────────────────
METRICS = [
    ("impr", "Impressions", False), ("clicks", "Clicks", False),
    ("ctr", "CTR", True), ("cost", "Cost ($)", False),
    ("leads", "Leads (primary conv)", False), ("cvr", "CvR (lead/click)", True),
    ("roas", "ROAS", False), ("cpa", "CPA ($)", False),
    ("sis", "Search Impr. Share", True),
]
ERA_COLORS = ["#EAF2F8", "#FEF9E7", "#F5EEF8", "#EAFAF1", "#FDEDEC", "#EBF5FB"]
LANE_STYLE = {"geico": ("#C0392B", "-"), "other_adgroup": ("#7F8C8D", ":"), "campaign": ("#2874A6", "--")}


def build_timeline_png(g, lane_daily, geico_events, eras, title, start, end):
    n = len(METRICS)
    fig, axes = plt.subplots(n + 1, 1, figsize=(15, 2.1 * (n + 1)), sharex=True,
                             gridspec_kw={"height_ratios": [2.2] * n + [1.0]})
    x = g["date"]
    span0, span1 = pd.Timestamp(start), pd.Timestamp(end)

    def shade_eras(ax):
        for i, (ek, vf, vt) in enumerate(eras):
            ax.axvspan(pd.Timestamp(vf), min(pd.Timestamp(vt), span1),
                       color=ERA_COLORS[i % len(ERA_COLORS)], alpha=0.5, zorder=0)

    def mark_geico(ax):
        for ev in geico_events:
            ax.axvline(pd.Timestamp(ev["date"]), color="#C0392B", ls="--", lw=0.8, alpha=0.45, zorder=1)

    for ax, (col, label, is_ratio) in zip(axes[:n], METRICS):
        shade_eras(ax)
        mark_geico(ax)
        y = g[col].astype(float)
        ax.scatter(x, y, s=8, color="gray", alpha=0.35, zorder=2)
        ax.plot(x, y.rolling(ROLL, min_periods=3).mean(), color="#1F4E79", lw=1.8, zorder=3)
        ax.set_ylabel(label, fontsize=9)
        ax.grid(True, alpha=0.25)
        if is_ratio and y.notna().any():
            ax.set_ylim(0, np.nanpercentile(y, 98) * 1.15 or 1)

    # event-density strip (3 lanes)
    axe = axes[n]
    shade_eras(axe)
    dts = sorted(lane_daily.keys())
    if dts:
        idx = pd.to_datetime(dts)
        for lane, (color, _) in LANE_STYLE.items():
            counts = [lane_daily[d].get(lane, 0) for d in dts]
            axe.bar(idx, counts, width=1.0, color=color, alpha=0.55, label=lane)
    axe.set_ylabel("changes/day", fontsize=9)
    axe.legend(loc="upper left", fontsize=7, ncol=3, framealpha=0.9)
    axe.grid(True, alpha=0.25)

    for ax in axes:
        ax.set_xlim(span0, span1)
    axes[-1].xaxis.set_major_locator(mdates.MonthLocator())
    axes[-1].xaxis.set_major_formatter(mdates.DateFormatter("%b %Y"))
    plt.setp(axes[-1].xaxis.get_majorticklabels(), rotation=30, ha="right")
    axes[0].set_title(title, fontsize=13, fontweight="bold", loc="left")
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=95, bbox_inches="tight")
    plt.close(fig)
    return buf.getvalue()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--brand", default="Geico")
    ap.add_argument("--out", default=".")
    ap.add_argument("--start", default="2025-01-01")
    ap.add_argument("--end", default=None)
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    end = args.end or conn.execute(
        "SELECT MAX(date) FROM ad_group_performance_daily WHERE account_id=?", (ACCOUNT_ID,)).fetchone()[0]
    print("window %s .. %s" % (args.start, end))

    df = load_cohort_daily(conn, args.brand)
    if df.empty:
        print("no cohort rows for brand %r" % args.brand); return
    df = df[(df["date"] >= pd.Timestamp(args.start)) & (df["date"] <= pd.Timestamp(end))]
    print("cohort daily rows: %d | segments: %s" % (len(df), sorted(df["segment"].unique())))

    lane_daily, geico_events = load_events(conn, args.brand, args.start, end)
    eras = load_eras(conn)
    print("geico-lane change events: %d | eras: %d" % (len(geico_events), len(eras)))

    os.makedirs(args.out, exist_ok=True)
    views = [("TOTAL", ["home", "renters"]), ("HOME", ["home"]), ("RENTERS", ["renters"])]
    for vname, segs in views:
        sub = df[df["segment"].isin(segs)]
        if sub.empty:
            continue
        g = rollup(df, segs)
        title = "%s cohort — %s   |   %s to %s" % (args.brand, vname, args.start, end)
        png = build_timeline_png(g, lane_daily, geico_events, eras, title, args.start, end)
        path = os.path.join(args.out, "%s_cohort_%s.png" % (args.brand.lower(), vname.lower()))
        with open(path, "wb") as f:
            f.write(png)
        print("  wrote %s (%d bytes)" % (path, len(png)))
    conn.close()


if __name__ == "__main__":
    main()
