#!/usr/bin/env python
r"""
adgroup_cohort_tracker.py — Are the new themed generic ad groups building?
═══════════════════════════════════════════════════════════════════════
Evidence instrument for the rebuild plan: the Jul-14 2026 reorganization
created tighter-themed generic ad groups (Home-Generic-Home Insurance-
Core, Home-Generic-Fire-Core, Home-Generic-Cost-Core, ...). This tracks
each matching ad group's WEEKLY impressions, CTR, impression-weighted
QS, spend, and ROAS — before/after the cohort date — so "it seems to be
driving more impressions" becomes a dated chart.

COHORTS:
  NEW     first activity on/after --cohort-start (default 2026-07-14)
  LEGACY  matching ad groups active before the cohort date (the
          comparison set; their post-date series shows what
          reorganization did to the donors)

DATA (house rules): primary source = kpd JOIN keyword_instances
(device='ALL'; Search keyword grain) rolled up to ad group via
ki.ad_group_id — schema-known and consistent with every other
instrument. QS = keyword_quality_daily impression-weighted within the
ad group per week (never summed; unrated excluded; rated share shown).
Ad-group names from ad_groups (key column introspected). Weekly grain
on purpose — thin-data noise is the enemy (the data-density criterion).

HONESTY NOTES (printed on outputs):
  · A reorganization MOVES keywords: group-level series conflate
    migration with growth. Keyword-followed views live in the health
    workbook; this instrument answers the ad-group question only.
  · ~6 weeks of life is EARLY — QS ledgers move slowly; verdicts are
    directional.
  · Expansion windows (cycle-state) are shaded on the charts —
    in-window lifts are wave behavior, not necessarily theme success.

OUTPUT (to --out-dir): adgroup_cohort_<date>.csv (weekly grain),
.png (4-panel: impressions / CTR / iw-QS / ROAS), .txt (ramp verdicts).

USAGE:
  python adgroup_cohort_tracker.py
  python adgroup_cohort_tracker.py --pattern "home-generic"
  python adgroup_cohort_tracker.py --cohort-start 2026-07-14 --since 2026-05-01
"""

import argparse
import csv
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

DEFAULT_DB_PATH = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")
DEFAULT_OUT_DIR = Path(r"C:\Projects\google-ads-ops\analysis"
                       r"\profit-attribution-tracker\output"
                       r"\keyword-health")
DEFAULT_CYCLE_STATE = Path(r"C:\Projects\google-ads-ops\analysis"
                           r"\profit-attribution-tracker\output"
                           r"\cycle-dashboard\cycle_state_latest.csv")
ACCOUNT_ID = 1
CAMPAIGN_IDS = {
    "CALIFORNIA": 23385188622,
    "TEXAS":      23394808297,
    "FLORIDA":    23394808300,
    "TIER4":      23428389081,
    "TIER1":      23496334752,
    "TIER2":      23530860942,
    "TIER3":      23586637678,
}
REV_CANDIDATES = ["conversion_value", "conversions_value", "conv_value",
                  "primary_conv_value", "total_conv_value",
                  "all_conv_value"]
CONV_CANDIDATES = ["conversions", "primary_conv_count", "conv_count",
                   "all_conversions"]


def connect_ro(p):
    conn = sqlite3.connect(f"file:{Path(p).as_posix()}?mode=ro",
                           uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def pick_col(conn, table, candidates):
    try:
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return None
    for c in candidates:
        if c in cols:
            return c
    return None


def has_col(conn, table, col):
    try:
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return False
    return col in cols


def pdate(s):
    return datetime.strptime(str(s)[:10], "%Y-%m-%d").date()


def week_start(d):
    return d - timedelta(days=d.weekday())


def load_windows(path):
    p = Path(path)
    if not p.exists():
        return []
    out = []
    try:
        with open(p, newline="", encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                if (r.get("record") or "").strip().upper() != "WINDOW":
                    continue
                if not (r.get("entity") or "").strip().upper()\
                        .startswith("ACCOUNT"):
                    continue
                try:
                    out.append((pdate(r["date_start"]),
                                pdate(r["date_end"])))
                except (KeyError, ValueError):
                    continue
    except OSError:
        return []
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db-path", default=str(DEFAULT_DB_PATH))
    ap.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    ap.add_argument("--pattern", default="home-generic",
                    help="ad-group-name LIKE filter "
                         "(case-insensitive substring)")
    ap.add_argument("--cohort-start", default="2026-07-14",
                    help="groups first active on/after this date = "
                         "NEW cohort")
    ap.add_argument("--since", default="2026-05-01",
                    help="history window start for the series")
    ap.add_argument("--min-week-impr", type=float, default=50.0,
                    help="weeks below this impression floor are drawn "
                         "faded (thin-data guard)")
    ap.add_argument("--cycle-state", default=str(DEFAULT_CYCLE_STATE))
    ap.add_argument("--map", action="append",
                    help='override ids: --map "TIER2=123"')
    args = ap.parse_args()

    camp_ids = dict(CAMPAIGN_IDS)
    for m in args.map or []:
        k, v = m.split("=", 1)
        camp_ids[k.strip().upper()] = int(v.strip())
    ids = list(camp_ids.values())
    short_of = {cid: s for s, cid in camp_ids.items()}

    conn = connect_ro(args.db_path)
    rev_col = pick_col(conn, "keyword_performance_daily",
                       REV_CANDIDATES)
    conv_col = pick_col(conn, "keyword_performance_daily",
                        CONV_CANDIDATES)
    impr_col = pick_col(conn, "keyword_performance_daily",
                        ["impressions", "impr"])
    click_col = pick_col(conn, "keyword_performance_daily",
                         ["clicks", "click_count"])
    ag_name_col = pick_col(conn, "ad_groups",
                           ["ad_group_name", "name", "adgroup_name"])
    ag_key_col = pick_col(conn, "ad_groups", ["id", "ad_group_id"])
    if not (impr_col and click_col):
        sys.exit("kpd needs impressions+clicks columns for CTR.")
    if not (ag_name_col and ag_key_col):
        sys.exit("ad_groups needs a name and key column.")
    if not has_col(conn, "keyword_instances", "ad_group_id"):
        sys.exit("keyword_instances.ad_group_id missing.")
    has_kqd = has_col(conn, "keyword_quality_daily",
                      "historical_quality_score")
    print(f"kpd: impr={impr_col} clicks={click_col} rev={rev_col} "
          f"conv={conv_col}; ad_groups: {ag_key_col}/{ag_name_col}; "
          f"QS layer: {'yes' if has_kqd else 'NO'}")

    camp_expr = "ki.campaign_id" if has_col(
        conn, "keyword_instances", "campaign_id") else "ag.campaign_id"
    join_extra = "" if camp_expr == "ki.campaign_id" else \
        f"JOIN ad_groups ag ON ag.{ag_key_col} = ki.ad_group_id "

    q = (f"SELECT ki.ad_group_id agid, "
         f"agx.{ag_name_col} agname, {camp_expr} cid, "
         f"p.report_date d, "
         f"SUM(p.{impr_col}) impr, SUM(p.{click_col}) clk, "
         f"SUM(p.cost_micros)/1e6 s, SUM(p.{rev_col}) r"
         + (f", SUM(p.{conv_col}) cv" if conv_col else ", 0 cv")
         + (f", SUM(CASE WHEN q.historical_quality_score IS NOT NULL "
            f"THEN p.{impr_col} ELSE 0 END) ri, "
            f"SUM(CASE WHEN q.historical_quality_score IS NOT NULL "
            f"THEN q.historical_quality_score * p.{impr_col} "
            f"ELSE 0 END) qi "
            if has_kqd else ", 0 ri, 0 qi ")
         + f"FROM keyword_performance_daily p "
         f"JOIN keyword_instances ki ON ki.id = p.instance_id "
         f"AND ki.account_id = ? {join_extra}"
         f"JOIN ad_groups agx ON agx.{ag_key_col} = ki.ad_group_id "
         + ("LEFT JOIN keyword_quality_daily q "
            "ON q.instance_id = p.instance_id "
            "AND q.report_date = p.report_date "
            if has_kqd else "")
         + f"WHERE p.device = 'ALL' AND p.report_date >= ? "
         f"AND LOWER(agx.{ag_name_col}) LIKE ? "
         f"AND {camp_expr} IN ({','.join('?' * len(ids))}) "
         f"GROUP BY 1, 2, 3, 4")
    pat = f"%{args.pattern.strip().lower()}%"
    rows = conn.execute(q, [ACCOUNT_ID, args.since, pat] + ids)\
        .fetchall()
    data_end = conn.execute(
        """SELECT MAX(p.report_date) FROM keyword_performance_daily p
           JOIN keyword_instances ki ON ki.id = p.instance_id
           WHERE ki.account_id = ?""", [ACCOUNT_ID]).fetchone()[0]
    conn.close()
    stamp = str(data_end)[:10]
    if not rows:
        sys.exit(f"No ad groups matching '%{args.pattern}%' with "
                 f"data since {args.since}. Check --pattern against "
                 "real ad-group names.")

    cohort_start = pdate(args.cohort_start)
    # weekly rollup per ad group
    groups = {}
    for r in rows:
        gid = int(r["agid"])
        g = groups.setdefault(gid, {
            "name": r["agname"], "camp": short_of.get(int(r["cid"]),
                                                      str(r["cid"])),
            "first": None, "weeks": {}})
        d = pdate(r["d"])
        if (r["impr"] or 0) > 0 or (r["s"] or 0) > 0:
            if g["first"] is None or d < g["first"]:
                g["first"] = d
        w = week_start(d)
        a = g["weeks"].setdefault(w, [0.0] * 6)
        a[0] += r["impr"] or 0
        a[1] += r["clk"] or 0
        a[2] += r["s"] or 0
        a[3] += r["r"] or 0
        a[4] += r["ri"] or 0
        a[5] += r["qi"] or 0
    for g in groups.values():
        g["cohort"] = "NEW" if (g["first"] and
                                g["first"] >= cohort_start) else \
            "LEGACY"
    n_new = sum(1 for g in groups.values() if g["cohort"] == "NEW")
    print(f"  matched {len(groups)} ad groups "
          f"({n_new} NEW since {cohort_start}, "
          f"{len(groups) - n_new} LEGACY)")

    windows = load_windows(args.cycle_state)

    # CSV (weekly grain)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / f"adgroup_cohort_{stamp}.csv"
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["ad_group_id", "ad_group_name", "campaign",
                    "cohort", "first_activity", "week", "impressions",
                    "clicks", "ctr", "spend", "revenue", "roas",
                    "iw_qs", "rated_share"])
        for gid, g in sorted(groups.items()):
            for wk in sorted(g["weeks"]):
                im, ck, s, r, ri, qi = g["weeks"][wk]
                w.writerow([gid, g["name"], g["camp"], g["cohort"],
                            g["first"], wk, f"{im:.0f}", f"{ck:.0f}",
                            f"{(ck / im):.4f}" if im else "",
                            f"{s:.2f}", f"{r:.2f}",
                            f"{(r / s):.3f}" if s > 0 else "",
                            f"{(qi / ri):.2f}" if ri > 0 else "",
                            f"{(ri / im):.3f}" if im else ""])

    # verdicts: first 2 full weeks vs last 2 full weeks (per group)
    last_full = week_start(pdate(stamp)) - timedelta(days=7)
    L = [f"AD-GROUP COHORT TRACKER — pattern '%{args.pattern}%' — "
         f"data through {stamp}",
         f"Cohort date {cohort_start} · weekly grain since "
         f"{args.since} · partial current week excluded from "
         "verdicts.",
         "HONESTY: reorganization moves keywords — group series "
         "conflate migration with growth (keyword-followed views: "
         "health workbook). ~6 weeks is EARLY; QS ledgers move "
         "slowly; in-window lifts are wave behavior (shaded on "
         "chart). Verdicts are directional.", ""]
    for gid, g in sorted(groups.items(),
                         key=lambda kv: (kv[1]["cohort"] != "NEW",
                                         kv[1]["name"] or "")):
        wks = sorted(w for w in g["weeks"] if w <= last_full)
        if len(wks) < 2:
            L.append(f"{g['name']} [{g['camp']}] {g['cohort']}: "
                     "insufficient full weeks")
            continue
        head = wks[:2]
        tail = wks[-2:]

        def pool(sel):
            t = [0.0] * 6
            for w_ in sel:
                for i in range(6):
                    t[i] += g["weeks"][w_][i]
            im, ck, s, r, ri, qi = t
            return {"impr": im / len(sel), "ctr": (ck / im)
                    if im else None, "roas": (r / s) if s > 0
                    else None, "qs": (qi / ri) if ri > 0 else None,
                    "spend": s / len(sel)}
        a, b = pool(head), pool(tail)

        def fmt(v, p):
            return p.format(v) if v is not None else "—"
        L.append(
            f"{g['name']} [{g['camp']}] {g['cohort']} "
            f"(first {g['first']}):")
        L.append(
            f"  impressions/wk {a['impr']:,.0f} -> {b['impr']:,.0f} "
            f"({((b['impr'] / a['impr'] - 1) if a['impr'] else 0):+.0%})"
            f" · CTR {fmt(a['ctr'], '{:.1%}')} -> "
            f"{fmt(b['ctr'], '{:.1%}')}"
            f" · iwQS {fmt(a['qs'], '{:.1f}')} -> "
            f"{fmt(b['qs'], '{:.1f}')}"
            f" · ROAS {fmt(a['roas'], '{:.2f}')} -> "
            f"{fmt(b['roas'], '{:.2f}')}"
            f" · spend/wk ${b['spend']:,.0f}")
    text = "\n".join(L)
    txt_path = out_dir / f"adgroup_cohort_{stamp}.txt"
    txt_path.write_text(text, encoding="utf-8")
    print("\n" + text)

    # chart: 4 panels, lines per ad group, cohort vline, windows shaded
    metrics = [("impressions/wk", 0, None), ("CTR", None, "ctr"),
               ("impression-weighted QS", None, "qs"),
               ("ROAS", None, "roas")]
    fig, axes = plt.subplots(2, 2, figsize=(12.5, 7.2), dpi=150)
    axes = axes.flatten()
    cmap = plt.get_cmap("tab10")
    order = sorted(groups.items(),
                   key=lambda kv: (kv[1]["cohort"] != "NEW",
                                   kv[1]["name"] or ""))
    for mi, (label, _, kind) in enumerate(metrics):
        ax = axes[mi]
        for gi, (gid, g) in enumerate(order):
            wks = sorted(g["weeks"])
            xs, ys = [], []
            for w_ in wks:
                im, ck, s, r, ri, qi = g["weeks"][w_]
                if kind is None:
                    v = im
                elif kind == "ctr":
                    v = (ck / im) if im >= args.min_week_impr else None
                elif kind == "qs":
                    v = (qi / ri) if ri > 0 else None
                else:
                    v = (r / s) if s > 0 and \
                        im >= args.min_week_impr else None
                if v is not None:
                    xs.append(w_)
                    ys.append(v)
            if not xs:
                continue
            new = g["cohort"] == "NEW"
            nm = (g["name"] or str(gid))
            ax.plot(xs, ys, "-o" if new else "--s",
                    color=cmap(gi % 10), lw=2 if new else 1.2,
                    ms=3.5 if new else 2.5,
                    alpha=1.0 if new else 0.55,
                    label=f"{nm[:30]} [{g['camp']}]"
                    + ("" if new else " (legacy)"))
        for ws_, we_ in windows:
            ax.axvspan(ws_, we_, color="#F4B183", alpha=0.15, lw=0)
        ax.axvline(cohort_start, color="#404040", ls="-.", lw=1)
        if kind == "roas":
            ax.axhline(1.0, color="#C00000", ls=":", lw=0.9)
        ax.set_title(label, fontsize=9.5, fontweight="bold")
        ax.tick_params(labelsize=7, rotation=25)
        ax.grid(alpha=0.25)
        if mi == 0:
            ax.legend(fontsize=6, loc="upper left")
    fig.suptitle(
        f"Themed generic ad groups — weekly build since "
        f"{args.since} (solid = NEW cohort since {cohort_start}; "
        "dashed = legacy; shaded = account expansion windows; "
        "thin weeks blanked).\nEARLY and directional — group series "
        "conflate keyword migration with growth.", fontsize=10)
    fig.tight_layout(rect=(0, 0, 1, 0.92))
    png_path = out_dir / f"adgroup_cohort_{stamp}.png"
    fig.savefig(png_path)
    plt.close(fig)
    print(f"\nSaved: {csv_path}")
    print(f"Saved: {png_path}")
    print(f"Saved: {txt_path}")


if __name__ == "__main__":
    main()
