"""
geico_qs_overlay.py -- The four-source Geico QS causal timeline (2024-08 -> today), monthly.

Puts on ONE picture, aligned by month, the evidence for the QS erosion story:
  1. QS series      -- iw_QS + pred_ctr_below_share + pred_ctr_above_share (from qs_cohort_monthly,
                       the architect-gated math) with rated_share plotted as the COMPOSITION GUARD.
  2. Change events  -- Geico change blocks by category/month (keyword_status pauses, rsa_new/change,
                       tROAS, keyword_add) from google_change_csv_history_raw via the categorizer.
  3. RSA inventory  -- distinct serving Geico ads per month (rsa_ad_performance_daily x rsa_ads),
                       the creative-wave signal (Aug-2024, Nov-2024).
  4. Era bands      -- account_structure_era shading the structural regimes.

COHORT: keyword-text 'geico' for QS + change events (matches qs_analysis.py); RSA is ad-grain so
it uses the Geico ad_group id-set. That cohort seam is labeled on the chart, not hidden.

Reads rated_share alongside iw_QS so a QS move on collapsing coverage reads as composition-until-
proven (here rated_share stays ~0.9-1.0, so the collapse is real -- the chart shows that).

    python geico_qs_overlay.py --db PATH --brand Geico --out ./output
"""
import argparse
import collections
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
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def month_floor(dt_str):
    return (dt_str or "")[:7] + "-01"


def load_qs(conn, brand, account):
    import qs_cohort_monthly as Q
    rows = Q.monthly_qs(conn, brand, account)
    for r in rows:
        r["m"] = pd.Timestamp(r["month"] + "-01")
    return rows


def load_change_events(conn, brand, account):
    """Monthly Geico change-block counts by category, keyword-text cohort where possible;
    for ad-group-attributed changes we also keep ad_group_col contains geico. A block counts
    if geico is in the ad_group OR in the change text (mirrors geico_change_export)."""
    if CHANGE_HISTORY_DIR not in sys.path:
        sys.path.insert(0, CHANGE_HISTORY_DIR)
    import change_taxonomy as T
    b = brand.lower()
    # SQL-prefilter to Geico-relevant rows only (geico in the ad group OR anywhere in the change
    # text). This avoids running the categorizer over the whole 1.46M-row table -- only a few
    # thousand rows match, so the Python categorize loop stays small and the run is fast.
    like = "%" + b + "%"
    q = ("SELECT change_datetime, ad_group_col, changes_raw FROM google_change_csv_history_raw "
         "WHERE account_id=? AND (lower(ad_group_col) LIKE ? OR lower(changes_raw) LIKE ?)")
    monthly = collections.defaultdict(lambda: collections.Counter())
    for dt, ag, raw in conn.execute(q, (account, like, like)):
        m = pd.Timestamp((dt or "")[:7] + "-01")
        ag_is_geico = b in (ag or "").lower()
        for block, rec in zip(T.split_blocks(raw), T.categorize_cell(raw)):
            # count a block only if the ad group is geico OR geico appears in that block
            if ag_is_geico or b in block.lower():
                monthly[m][rec["category"]] += 1
    return monthly


def load_rsa_inventory(conn, brand, account):
    """Distinct serving Geico ads per month from rsa_ad_performance_daily joined to rsa_ads,
    scoped to Geico ad groups via ad_groups. Serving = impressions>0."""
    # geico ad_group id-set
    have_ag = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ad_groups'").fetchone()
    have_ra = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='rsa_ads'").fetchone()
    have_rp = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='rsa_ad_performance_daily'").fetchone()
    if not (have_ag and have_ra and have_rp):
        return {}
    out = {}
    try:
        # Resolve the Geico ad-id set FIRST (small), then filter the 1.67M-row perf table to it
        # via a temp table. Avoids a nested scan of the whole perf table with a name LIKE.
        ad_ids = [r[0] for r in conn.execute(
            "SELECT a.ad_id FROM rsa_ads a JOIN ad_groups g ON g.ad_group_id = a.ad_group_id "
            "WHERE g.account_id=? AND lower(g.ad_group_name) LIKE ?",
            (account, "%" + brand.lower() + "%"))]
        if not ad_ids:
            return out
        conn.execute("CREATE TEMP TABLE IF NOT EXISTS _geico_adids (ad_id INTEGER PRIMARY KEY)")
        conn.execute("DELETE FROM _geico_adids")
        conn.executemany("INSERT OR IGNORE INTO _geico_adids VALUES (?)", [(i,) for i in ad_ids])
        sql = ("""
            SELECT substr(p.report_date,1,7) AS ym, COUNT(DISTINCT p.ad_id) AS serving_ads
            FROM rsa_ad_performance_daily p
            JOIN _geico_adids gi ON gi.ad_id = p.ad_id
            WHERE p.account_id=? AND p.impressions > 0
            GROUP BY ym ORDER BY ym
        """)
        for ym, n in conn.execute(sql, (account,)):
            out[pd.Timestamp(ym + "-01")] = n
        conn.execute("DROP TABLE _geico_adids")
    except Exception as exc:
        print("  (rsa inventory unavailable: %s)" % exc)
    return out


def load_eras(conn, account):
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='account_structure_era'").fetchone():
        return []
    return [(k, pd.Timestamp(vf), pd.Timestamp(vt or "2100-01-01")) for k, vf, vt in conn.execute(
        "SELECT era_key, valid_from, valid_to FROM account_structure_era "
        "WHERE account_id=? AND era_key NOT LIKE '\\_%' ESCAPE '\\' ORDER BY valid_from", (account,))]


ERA_COLORS = ["#F6F6F6", "#EAF2F8", "#FEF9E7", "#F5EEF8", "#EAFAF1", "#FDEDEC", "#EBF5FB"]
# the change categories worth showing as stacked bars (the causal levers)
EVENT_CATS = ["keyword_status", "rsa_new", "rsa_change", "rsa_paused",
              "bid_target_adgroup", "keyword_add", "negative_keyword", "adgroup_status"]
EVENT_COLORS = {"keyword_status": "#C0392B", "rsa_new": "#27AE60", "rsa_change": "#16A085",
                "rsa_paused": "#95A5A6", "bid_target_adgroup": "#2980B9", "keyword_add": "#8E44AD",
                "negative_keyword": "#D35400", "adgroup_status": "#F39C12"}


def build(qs, events, rsa, eras, brand, start, end, out):
    fig, axes = plt.subplots(5, 1, figsize=(15, 15), sharex=True,
                             gridspec_kw={"height_ratios": [2.4, 2.0, 1.6, 2.0, 1.4]})
    span0, span1 = pd.Timestamp(start), pd.Timestamp(end)
    months = pd.date_range(span0, span1, freq="MS")

    def shade(ax):
        for i, (k, vf, vt) in enumerate(eras):
            ax.axvspan(vf, min(vt, span1), color=ERA_COLORS[i % len(ERA_COLORS)], alpha=0.55, zorder=0)

    qm = {r["m"]: r for r in qs}
    xs = [r["m"] for r in qs]

    # Panel 1: iw_QS
    ax = axes[0]; shade(ax)
    ax.plot(xs, [r["iw_QS"] for r in qs], "-o", color="#1F4E79", lw=2, ms=3)
    ax.set_ylabel("iw-QS", fontsize=10); ax.grid(True, alpha=0.25); ax.set_ylim(0, 10)
    ax.set_title("%s QS causal timeline — %s to %s   (cohort: keyword-text '%s'; RSA panel is ad-group-scoped)"
                 % (brand, start, end, brand.lower()), fontsize=13, fontweight="bold", loc="left")

    # Panel 2: predicted-CTR bucket shares (below vs above) + rated_share as composition guard
    ax = axes[1]; shade(ax)
    ax.plot(xs, [r["pred_ctr_below_share"] for r in qs], "-o", color="#C0392B", lw=2, ms=3, label="pred-CTR BELOW share")
    ax.plot(xs, [r["pred_ctr_above_share"] for r in qs], "-o", color="#27AE60", lw=2, ms=3, label="pred-CTR ABOVE share")
    ax.set_ylabel("pred-CTR share", fontsize=10); ax.set_ylim(0, 1.02); ax.grid(True, alpha=0.25)
    ax.legend(loc="center left", fontsize=8, framealpha=0.9)

    # Panel 3: rated_share (composition guard) — its own panel so the story is explicit
    ax = axes[2]; shade(ax)
    ax.plot(xs, [r["rated_share"] for r in qs], "-o", color="#7F8C8D", lw=1.8, ms=3)
    ax.axhline(0.9, color="#BDC3C7", ls="--", lw=1)
    ax.set_ylabel("rated_share\n(coverage guard)", fontsize=9); ax.set_ylim(0, 1.05); ax.grid(True, alpha=0.25)
    ax.text(0.005, 0.08, "high & flat => QS moves are real, not composition",
            transform=ax.transAxes, fontsize=8, color="#555")

    # Panel 4: change events, stacked bars by category
    ax = axes[3]; shade(ax)
    bottom = np.zeros(len(months))
    for cat in EVENT_CATS:
        vals = np.array([events.get(m, {}).get(cat, 0) for m in months], float)
        if vals.sum() == 0:
            continue
        ax.bar(months, vals, bottom=bottom, width=22, color=EVENT_COLORS.get(cat, "#888"),
               label=cat, alpha=0.9)
        bottom += vals
    ax.set_ylabel("Geico change\nblocks / month", fontsize=9); ax.grid(True, alpha=0.25)
    ax.legend(loc="upper right", fontsize=7, ncol=2, framealpha=0.9)

    # Panel 5: RSA serving inventory
    ax = axes[4]; shade(ax)
    if rsa:
        rx = sorted(rsa.keys())
        ax.plot(rx, [rsa[m] for m in rx], "-o", color="#6C3483", lw=2, ms=3)
    ax.set_ylabel("serving Geico\nRSAs / month", fontsize=9); ax.grid(True, alpha=0.25)

    for ax in axes:
        ax.set_xlim(span0, span1)
    axes[-1].xaxis.set_major_locator(mdates.MonthLocator(interval=1))
    axes[-1].xaxis.set_major_formatter(mdates.DateFormatter("%b %y"))
    plt.setp(axes[-1].xaxis.get_majorticklabels(), rotation=60, ha="right", fontsize=8)
    fig.tight_layout()
    path = os.path.join(out, "%s_qs_overlay.png" % brand.lower())
    fig.savefig(path, dpi=95, bbox_inches="tight")
    plt.close(fig)
    return path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--brand", default="Geico")
    ap.add_argument("--account", type=int, default=ACCOUNT_ID)
    ap.add_argument("--start", default="2024-08-01")
    ap.add_argument("--end", default=None)
    ap.add_argument("--out", default=".")
    args = ap.parse_args()
    conn = sqlite3.connect(args.db)
    end = args.end or conn.execute(
        "SELECT MAX(report_date) FROM keyword_performance_daily").fetchone()[0][:10]
    print("window %s .. %s" % (args.start, end))
    qs = load_qs(conn, args.brand, args.account)
    qs = [r for r in qs if args.start <= r["month"] + "-01" <= end]
    print("QS months: %d" % len(qs))
    events = load_change_events(conn, args.brand, args.account)
    print("change-event months: %d" % len(events))
    rsa = load_rsa_inventory(conn, args.brand, args.account)
    print("RSA-inventory months: %d" % len(rsa))
    eras = load_eras(conn, args.account)
    print("eras: %d" % len(eras))
    os.makedirs(args.out, exist_ok=True)
    path = build(qs, events, rsa, eras, args.brand, args.start, end, args.out)
    print("[ok] wrote %s" % path)
    conn.close()


if __name__ == "__main__":
    main()
