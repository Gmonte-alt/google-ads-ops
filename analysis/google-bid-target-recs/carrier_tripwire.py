"""
Carrier Spend Tripwire — Google Home (daily)
═══════════════════════════════════════════════════════════════════════
Pre-emptive GP defense: watches daily SPEND (the zero-lag leading
indicator) in each carrier segment against that segment's own recent
baseline, and flags expansions before GP can report the damage.

One PNG per day: a status table (all segments, worst first, with a 28-day
sparkline per row) + a detail chart for each RED segment. Plus a
daily_pulse-style text block (dated + latest.txt) for Slack.

SEGMENTS (v1 — pattern-based, per keep-it-simple ruling):
  Nine named carriers matched on keyword_text substrings; everything
  unmatched = "Other/generic". Mobile is a cross-cutting row (all
  keywords, device='MOBILE'). The generic-vs-aggregator split from the
  classification tables is the v1.1 upgrade, not this version.

EARNING COLUMN (v4): every row carries ROAS — revenue/spend over the
trailing --mroas-window (7) days through the latest data date.
RULING (8/20): yesterday's data is treated as COMPLETE, including the
last 3 days of ROAS — the conversion-lag restatement claim is
unproven, so no lag exclusion by default (--lag-days 0). If the lag
is ever demonstrated (see note below), set --lag-days back to 3.
Campaign rows additionally show the
GAP vs the campaign's tROAS target (latest TARGET_ROAS event in
campaign_change_events) plus a gap trend sparkline — the pendulum
driver: sustained positive gap precedes expansion, gap collapsing
precedes the swing-back. Read: RED + mROAS above target = expansion
is EARNING (hold, cap as insurance); RED + mROAS below ~1.0 =
DILUTING (cap today).

DOWNSIDE TRIPWIRE (v3): symmetric lower bands catch drop-offs —
  FADING   = yesterday < 0.67x baseline (day 1)    -> watch
  FALL-OUT = 2 consecutive days < 0.67x, OR any day < 0.5x
             -> diagnose: auction exit (IS/eligible impressions) vs
             demand; caps are not the lever on the way down.
  A drop within 10 days of an expansion streak is tagged
  "(post-expansion)" — the pendulum swinging back, expected; an
  untagged fall-out came out of nowhere and is the scarier kind.

STATUS RULES (defaults; all CLI-tunable):
  baseline(day d)  = mean spend of the trailing 14 days of the SAME
                     day-class (weekday vs weekend) — kills the
                     Monday-vs-weekend-mean false positive
  AMBER            = yesterday > 1.5x baseline (day 1)     -> watch
  RED              = 2 consecutive days > 1.5x, OR any day > 2.0x
                     -> cap budgets on affected campaigns that morning
  Quiet segments   (baseline < $5/day, e.g. State Farm) alert only when
                     spend APPEARS (> --quiet-floor $25/day) — for
                     'recoverable' segments that row is a recovery
                     tracker, not a drain tripwire.

DATA (pinned sources):
  keyword_performance_daily p JOIN keyword_instances k ON
  k.id = p.instance_id AND k.account_id = 1  (kpd has no account_id —
  scope through the join; kpd is structurally Search-only).
  Carrier rows: device='ALL'. Mobile row: device='MOBILE'.
  Spend = cost_micros/1e6, summed. Spend does not restate — no lag
  exclusion needed. DB opened read-only; never during a sync.

CAMPAIGN VIEW (v2): a second PNG with the same grammar at campaign
grain — 7 campaign rows from campaign_performance_daily_search
(device='ALL'), same status rules. Each RED campaign's detail chart
carries a segment-contribution bar: which carrier segments drove that
campaign's breach delta (breach-window spend/day vs baseline, per
segment, via kpd->instances->campaign linkage). That is the
collateral-damage read: whether a campaign-level cap would hit one
expanding segment or throttle healthy neighbors.

LAG FALSIFICATION TEST: every run appends account-wide spend/revenue
per report_date (last 10 dates) to revenue_snapshots.csv in --out-dir,
keyed by (report_date, sync_date). Once a report_date has been seen on
2+ sync dates, the run prints a LAG CHECK comparing its revenue as
first seen vs now. ~10 days of runs settles the restatement question
permanently: growth => lag is real (restore --lag-days 3 everywhere);
flat => the claim dies.

USAGE:
  python carrier_tripwire.py
  python carrier_tripwire.py --amber-mult 1.5 --red-mult 2.0 --red-streak 2
  python carrier_tripwire.py --baseline-days 14 --trend-days 28
"""

import argparse
import os
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.gridspec import GridSpec

DEFAULT_DB_PATH = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")
DEFAULT_OUT_DIR = Path(r"C:\Projects\google-ads-ops\analysis"
                       r"\profit-attribution-tracker\output\carrier-tripwire")
ACCOUNT_ID = 1

# order = display preference for ties; matching is first-hit top-down
CARRIER_PATTERNS = [
    ("Geico",        ["geico"]),
    ("Farmers",      ["farmers"]),
    ("State Farm",   ["state farm", "statefarm"]),
    ("Travelers",    ["travelers"]),
    ("Kin",          ["kin insurance", " kin ", "kin home"]),
    ("AAA",          ["aaa"]),
    ("Amica",        ["amica"]),
    ("Hartford",     ["hartford"]),
    ("Costco",       ["costco"]),
]
OTHER_LABEL = "Other/generic"
MOBILE_LABEL = "Mobile (all kws)"

C_LINE, C_BASE = "#2a78d6", "#c3c2b7"
C_OK, C_AMBER, C_RED = "#0ca30c", "#c98500", "#d03b3b"
STATUS_ORDER = {"RED": 0, "FALLOUT": 0, "AMBER": 1, "FADING": 1,
                "APPEARING": 1, "OK": 2, "QUIET": 3}
C_FALL, C_FADE = "#185FA5", "#534AB7"


def connect_ro(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


REV_CANDIDATES = ["conversion_value", "conversions_value", "conv_value",
                  "primary_conv_value", "total_conv_value",
                  "all_conv_value"]


def pick_col(conn, table, candidates):
    try:
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return None
    for c in candidates:
        if c in cols:
            return c
    return None


def classify(kw: str) -> str:
    k = f" {kw.lower().strip()} "
    for label, pats in CARRIER_PATTERNS:
        for p in pats:
            if p in k:
                return label
    return OTHER_LABEL


def fetch_daily(conn, start, end):
    """Returns ({segment: {date: spend}}, {date: mobile_spend})."""
    seg = {label: {} for label, _ in CARRIER_PATTERNS}
    seg[OTHER_LABEL] = {}
    seg_rev = {label: {} for label, _ in CARRIER_PATTERNS}
    seg_rev[OTHER_LABEL] = {}
    rev_col = pick_col(conn, "keyword_performance_daily", REV_CANDIDATES)
    rev_expr = f"SUM(p.{rev_col})" if rev_col else "NULL"
    if not rev_col:
        print("  WARN: no revenue column found on keyword_performance_"
              "daily — segment mROAS will show as '—' "
              f"(looked for: {', '.join(REV_CANDIDATES)})")
    sql = f"""
        SELECT p.report_date AS d, LOWER(ki.keyword_text) AS kw,
               SUM(p.cost_micros) / 1e6 AS spend,
               {rev_expr} AS rev
        FROM keyword_performance_daily p
        JOIN keyword_instances ki
          ON ki.id = p.instance_id AND ki.account_id = ?
        WHERE p.device = 'ALL' AND p.report_date BETWEEN ? AND ?
        GROUP BY 1, 2
    """
    for r in conn.execute(sql, [ACCOUNT_ID, start.isoformat(),
                                end.isoformat()]):
        d = str(r["d"])[:10]
        s = classify(r["kw"])
        seg[s][d] = seg[s].get(d, 0.0) + (r["spend"] or 0.0)
        if r["rev"] is not None:
            seg_rev[s][d] = seg_rev[s].get(d, 0.0) + r["rev"]

    mobile, mobile_rev = {}, {}
    sql_m = f"""
        SELECT p.report_date AS d, SUM(p.cost_micros) / 1e6 AS spend,
               {rev_expr} AS rev
        FROM keyword_performance_daily p
        JOIN keyword_instances ki
          ON ki.id = p.instance_id AND ki.account_id = ?
        WHERE p.device = 'MOBILE' AND p.report_date BETWEEN ? AND ?
        GROUP BY 1
    """
    for r in conn.execute(sql_m, [ACCOUNT_ID, start.isoformat(),
                                  end.isoformat()]):
        mobile[str(r["d"])[:10]] = r["spend"] or 0.0
        if r["rev"] is not None:
            mobile_rev[str(r["d"])[:10]] = r["rev"]
    return seg, seg_rev, mobile, mobile_rev


def fetch_campaign_daily(conn, start, end):
    """Campaign-grain spend/day (device='ALL'), scoped to account."""
    out, rev = {}, {}
    rev_col = pick_col(conn, "campaign_performance_daily_search",
                       REV_CANDIDATES)
    rev_expr = f"SUM(p.{rev_col})" if rev_col else "NULL"
    if not rev_col:
        print("  WARN: no revenue column found on campaign_performance_"
              "daily_search — campaign mROAS/gap will show as '—'")
    sql = f"""
        SELECT p.campaign_id, c.campaign_name, p.date AS d,
               SUM(p.cost_micros) / 1e6 AS spend,
               {rev_expr} AS rev
        FROM campaign_performance_daily_search p
        JOIN campaigns c ON c.campaign_id = p.campaign_id
        WHERE c.account_id = ? AND p.device = 'ALL'
          AND p.date BETWEEN ? AND ?
        GROUP BY 1, 2, 3
    """
    names = {}
    for r in conn.execute(sql, [ACCOUNT_ID, start.isoformat(),
                                end.isoformat()]):
        cid = str(r["campaign_id"])
        names[cid] = r["campaign_name"]
        out.setdefault(cid, {})[str(r["d"])[:10]] = r["spend"] or 0.0
        if r["rev"] is not None:
            rev.setdefault(cid, {})[str(r["d"])[:10]] = r["rev"]
    return out, rev, names


def kw_campaign_column(conn):
    """Find how keyword_instances links to campaigns. Returns SQL
    expression for campaign_id or None."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(keyword_instances)")}
    if "campaign_id" in cols:
        return "ki.campaign_id"
    if "ad_group_id" in cols:
        try:
            agc = {r[1] for r in conn.execute("PRAGMA table_info(ad_groups)")}
            if "campaign_id" in agc:
                return "AG_JOIN"
        except sqlite3.Error:
            pass
    return None


def fetch_seg_by_campaign(conn, start, end):
    """{campaign_id: {segment: {date: spend}}} for contribution bars.
    Returns None if no keyword->campaign linkage exists."""
    link = kw_campaign_column(conn)
    if link is None:
        return None
    if link == "AG_JOIN":
        sql = """
            SELECT ag.campaign_id AS cid, LOWER(ki.keyword_text) AS kw,
                   p.report_date AS d, SUM(p.cost_micros)/1e6 AS spend
            FROM keyword_performance_daily p
            JOIN keyword_instances ki
              ON ki.id = p.instance_id AND ki.account_id = ?
            JOIN ad_groups ag ON ag.id = ki.ad_group_id
            WHERE p.device = 'ALL' AND p.report_date BETWEEN ? AND ?
            GROUP BY 1, 2, 3
        """
    else:
        sql = f"""
            SELECT {link} AS cid, LOWER(ki.keyword_text) AS kw,
                   p.report_date AS d, SUM(p.cost_micros)/1e6 AS spend
            FROM keyword_performance_daily p
            JOIN keyword_instances ki
              ON ki.id = p.instance_id AND ki.account_id = ?
            WHERE p.device = 'ALL' AND p.report_date BETWEEN ? AND ?
            GROUP BY 1, 2, 3
        """
    out = {}
    try:
        for r in conn.execute(sql, [ACCOUNT_ID, start.isoformat(),
                                    end.isoformat()]):
            cid = str(r["cid"])
            seg = classify(r["kw"])
            d = str(r["d"])[:10]
            out.setdefault(cid, {}).setdefault(seg, {})
            out[cid][seg][d] = out[cid][seg].get(d, 0.0) + (r["spend"] or 0.0)
    except sqlite3.Error as exc:
        print(f"  WARN: keyword->campaign linkage query failed: {exc}")
        return None
    return out


def fetch_targets(conn, cids):
    """Latest TARGET_ROAS per campaign from campaign_change_events."""
    if not cids:
        return {}
    ph = ",".join("?" for _ in cids)
    out = {}
    try:
        sql = f"""
            SELECT campaign_id, change_date, new_value
            FROM campaign_change_events
            WHERE platform='google' AND account_id=?
              AND change_type='TARGET_ROAS' AND campaign_id IN ({ph})
            ORDER BY change_date
        """
        for r in conn.execute(sql, [ACCOUNT_ID, *cids]):
            raw = str(r["new_value"]).strip().rstrip("%")
            try:
                v = float(raw)
                out[str(r["campaign_id"])] = v / 100.0 if v > 10 else v
            except ValueError:
                pass
    except sqlite3.Error:
        pass
    return out


def mature_roas(spend_s, rev_s, dates, mature_end, window):
    """Sum(rev)/sum(spend) over the last `window` dates <= mature_end."""
    ds = [d for d in dates if d <= mature_end][-window:]
    if not any(d.isoformat() in rev_s for d in ds):
        return None
    cost = sum(spend_s.get(d.isoformat(), 0.0) for d in ds)
    rev = sum(rev_s.get(d.isoformat(), 0.0) for d in ds)
    return (rev / cost) if cost > 0 else None


def gap_series(spend_s, rev_s, dates, mature_end, window, target):
    """Rolling mature ROAS − target for each date <= mature_end."""
    out = []
    for i, d in enumerate(dates):
        if d > mature_end:
            break
        ds = [x for x in dates[:i + 1]][-window:]
        cost = sum(spend_s.get(x.isoformat(), 0.0) for x in ds)
        rev = sum(rev_s.get(x.isoformat(), 0.0) for x in ds)
        out.append((d, (rev / cost - target) if cost > 0 else None))
    return out


def short_campaign(name):
    n = (name or "").replace("HOME INSURANCE", "").replace(
        "_STATE-GROUP_", "").replace("-NB-SEARCH-ALL-USA-STE", "").replace(
        "_COMBINED_TROAS_SEARCH_ALLDEVICE_USA-STE", "")
    return n.strip("-_ ") or name


def contribution(series_by_seg, dates, streak, baseline_days, args):
    """Per-segment Δ spend/day: mean(streak window) − mean(baseline of
    same day-classes). Returns sorted [(segment, delta)]."""
    if not series_by_seg or streak < 1:
        return []
    out = []
    for seg, series in series_by_seg.items():
        idxs = range(len(dates) - streak, len(dates))
        cur = sum(series.get(dates[i].isoformat(), 0.0)
                  for i in idxs) / streak
        base = sum(baseline_for(series, dates, i, baseline_days)
                   for i in idxs) / streak
        d = cur - base
        if abs(d) >= 1:
            out.append((seg, d))
    out.sort(key=lambda x: -x[1])
    return out


def is_weekend(d):
    return d.weekday() >= 5


def baseline_for(series, dates, idx, n_days):
    """Mean of trailing n_days values of the same day-class (weekday vs
    weekend) before dates[idx]."""
    target_cls = is_weekend(dates[idx])
    vals = []
    j = idx - 1
    while j >= 0 and len(vals) < n_days:
        if is_weekend(dates[j]) == target_cls:
            vals.append(series.get(dates[j].isoformat(), 0.0))
        j -= 1
    return (sum(vals) / len(vals)) if vals else 0.0


def evaluate(series, dates, args):
    """Status for the last date. Returns dict with spend, baseline,
    delta, streak, status."""
    idx = len(dates) - 1
    y_spend = series.get(dates[idx].isoformat(), 0.0)
    base = baseline_for(series, dates, idx, args.baseline_days)

    if base < args.quiet_base:
        if y_spend >= args.quiet_floor:
            return dict(spend=y_spend, base=base, delta=None, streak=0,
                        status="APPEARING", post_expansion=False)
        return dict(spend=y_spend, base=base, delta=None, streak=0,
                    status="QUIET", post_expansion=False)

    streak = 0
    for k in range(idx, -1, -1):
        b = baseline_for(series, dates, k, args.baseline_days)
        if b >= args.quiet_base and \
                series.get(dates[k].isoformat(), 0.0) > args.amber_mult * b:
            streak += 1
        else:
            break

    low_streak = 0
    for k in range(idx, -1, -1):
        b = baseline_for(series, dates, k, args.baseline_days)
        if b >= args.quiet_base and \
                series.get(dates[k].isoformat(), 0.0) < args.fade_mult * b:
            low_streak += 1
        else:
            break

    # was there an expansion streak in the recent past? (pendulum tag)
    post_expansion = False
    if low_streak:
        for k in range(max(0, idx - low_streak - 10), idx - low_streak + 1):
            b = baseline_for(series, dates, k, args.baseline_days)
            if b >= args.quiet_base and \
                    series.get(dates[k].isoformat(), 0.0) > args.amber_mult * b:
                post_expansion = True
                break

    delta = y_spend / base - 1
    if (streak >= args.red_streak) or (y_spend > args.red_mult * base):
        status = "RED"
    elif (low_streak >= args.fallout_streak) or \
            (base >= args.quiet_base and y_spend < args.fallout_mult * base):
        status = "FALLOUT"
    elif streak >= 1:
        status = "AMBER"
    elif low_streak >= 1:
        status = "FADING"
    else:
        status = "OK"
    return dict(spend=y_spend, base=base, delta=delta,
                streak=streak or low_streak, status=status,
                post_expansion=post_expansion)


def status_color(s):
    return {"RED": C_RED, "AMBER": C_AMBER, "APPEARING": C_AMBER,
            "FALLOUT": C_FALL, "FADING": C_FADE,
            "OK": C_OK, "QUIET": "#898781"}[s]


def status_text(s, streak, post_exp=False):
    t = {"RED": "cap budgets", "AMBER": "watch",
         "FALLOUT": "fall-out: diagnose", "FADING": "fading",
         "APPEARING": "spend appearing", "OK": "in band",
         "QUIET": "quiet"}[s]
    if streak:
        t += f" (d{streak})"
    if post_exp and s in ("FALLOUT", "FADING"):
        t += " post-exp"
    return t


def render(rows, dates, args, out_png, title="Carrier spend tripwire",
           contrib=None):
    LAG0 = args.lag_days == 0
    reds = [r for r in rows if r["ev"]["status"] in ("RED", "FALLOUT")]
    n_detail = min(len(reds), 4)
    fig_h = 1.0 + 0.42 * len(rows) + 2.6 * n_detail
    fig = plt.figure(figsize=(11, fig_h))
    gs = GridSpec(2 + n_detail, 1, figure=fig,
                  height_ratios=[0.5, 0.42 * len(rows)] + [2.4] * n_detail,
                  hspace=0.45)

    ax_t = fig.add_subplot(gs[0])
    ax_t.axis("off")
    ax_t.text(0, 0.8, f"{title} — {dates[-1]}",
              fontsize=14, fontweight="bold")
    ax_t.text(0, 0.15, f"spend/day vs own {args.baseline_days}d same-"
              f"day-class baseline · up: amber >{args.amber_mult:g}x, red "
              f"{args.red_streak}d or >{args.red_mult:g}x · down: fading "
              f"<{args.fade_mult:g}x, fall-out {args.fallout_streak}d or "
              f"<{args.fallout_mult:g}x", fontsize=9, color="#666")

    ax = fig.add_subplot(gs[1])
    ax.axis("off")
    ax.set_xlim(0, 1)
    ax.set_ylim(0, len(rows) + 1)
    has_gap = any("gap" in r for r in rows)
    if has_gap:
        cols = [(0.00, "Campaign"), (0.17, "Yday $"), (0.25, "Base $"),
                (0.32, "Δ"), (0.38, "d"), (0.41, "28d trend"),
                (0.56, "ROAS" if LAG0 else "mROAS"),
                (0.63, "vs tgt"), (0.69, "gap trend"),
                (0.84, "Status")]
    else:
        cols = [(0.00, "Segment"), (0.20, "Yday $"), (0.29, "Base $"),
                (0.37, "Δ"), (0.44, "d"), (0.48, "28d trend"),
                (0.71, "ROAS(7d)" if LAG0 else "mROAS(7d)"),
                (0.84, "Status")]
    for x, h in cols:
        ax.text(x, len(rows) + 0.6, h, fontsize=9, color="#888")
    for i, r in enumerate(rows):
        y = len(rows) - 1 - i + 0.15
        ev = r["ev"]
        c1 = cols[1][0]; c2 = cols[2][0]; c3 = cols[3][0]
        c4 = cols[4][0]; c5 = cols[5][0]
        tw = 0.13 if has_gap else 0.20
        ax.text(0.00, y, r["label"], fontsize=9.5, fontweight="bold")
        ax.text(c1, y, f"${ev['spend']:,.0f}", fontsize=9.5)
        ax.text(c2, y, f"${ev['base']:,.0f}", fontsize=9.5, color="#666")
        ax.text(c3, y, (f"{ev['delta']:+.0%}" if ev["delta"] is not None
                        else "—"), fontsize=9.5,
                color=status_color(ev["status"])
                if ev["status"] not in ("OK", "QUIET") else "#666")
        ax.text(c4, y, str(ev["streak"]) if ev["streak"] else "",
                fontsize=9.5, color="#666")
        xs = [c5 + tw * k / max(len(dates) - 1, 1)
              for k in range(len(dates))]
        vals = [r["series"].get(d.isoformat(), 0.0) for d in dates]
        vmax = max(vals) or 1.0
        ys = [y - 0.05 + 0.30 * v / vmax for v in vals]
        ax.plot(xs, ys, lw=1.2, color=status_color(ev["status"])
                if ev["status"] != "OK" else "#898781")
        mr = r.get("mroas")
        mr_x = cols[6][0]
        if mr is not None:
            tgt = r.get("target")
            if tgt:
                mc = C_OK if mr >= tgt else (C_RED if mr < 1.0 else C_AMBER)
            else:
                mc = C_OK if mr >= 1.0 else C_RED
            ax.text(mr_x, y, f"{mr:.2f}", fontsize=9.5, color=mc,
                    fontweight="bold")
        else:
            ax.text(mr_x, y, "—", fontsize=9.5, color="#999")
        if has_gap:
            g = r.get("gap")
            ax.text(cols[7][0], y,
                    (f"{g:+.2f}" if g is not None else "—"), fontsize=9.5,
                    color=(C_OK if (g or 0) >= 0 else C_RED))
            gs_pts = [(d, v) for d, v in (r.get("gap_series") or [])
                      if v is not None]
            if len(gs_pts) > 2:
                gx0 = cols[8][0]
                gvals = [v for _, v in gs_pts]
                gmax = max(abs(min(gvals)), abs(max(gvals)), 0.05)
                gxs = [gx0 + 0.12 * k / max(len(gs_pts) - 1, 1)
                       for k in range(len(gs_pts))]
                gys = [y + 0.10 + 0.14 * v / gmax for v in gvals]
                ax.plot([gx0, gx0 + 0.12], [y + 0.10, y + 0.10],
                        lw=0.6, color="#bbb")
                ax.plot(gxs, gys, lw=1.1,
                        color=C_OK if gvals[-1] >= 0 else C_RED)
        ax.text(cols[-1][0], y, status_text(ev["status"], ev["streak"],
                                            ev.get("post_expansion")),
                fontsize=9, color=status_color(ev["status"]),
                bbox=dict(boxstyle="round,pad=0.25", fc="white",
                          ec=status_color(ev["status"]), lw=0.8))
        ax.axhline(len(rows) - 1 - i, color="#ddd", lw=0.5)

    for j, r in enumerate(reds[:n_detail]):
        cbars = (contrib or {}).get(r.get("cid"), []) if contrib else []
        if cbars:
            sub = gs[2 + j].subgridspec(1, 2, width_ratios=[2.2, 1],
                                        wspace=0.25)
            axd = fig.add_subplot(sub[0])
            axc = fig.add_subplot(sub[1])
            top = cbars[:6]
            labels = [s for s, _ in top][::-1]
            vals = [v for _, v in top][::-1]
            colors = [C_RED if v > 0 else C_OK for v in vals]
            axc.barh(labels, vals, color=colors, height=0.6)
            axc.set_title("Δ spend/day by segment (streak vs base)",
                          fontsize=8.5, loc="left", color="#444")
            axc.tick_params(labelsize=8)
            axc.axvline(0, color="#888", lw=0.8)
            axc.grid(True, axis="x", alpha=0.2)
        else:
            axd = fig.add_subplot(gs[2 + j])
        vals = [r["series"].get(d.isoformat(), 0.0) for d in dates]
        bases = [baseline_for(r["series"], dates, k, args.baseline_days)
                 for k in range(len(dates))]
        band = [b * args.amber_mult for b in bases]
        axd.plot(dates, vals, color=C_LINE, lw=1.8, label="spend/day")
        axd.plot(dates, bases, color=C_BASE, ls="--", lw=1.2,
                 label="baseline")
        lo_band = [b * args.fade_mult for b in bases]
        axd.fill_between(dates, lo_band, band, color="#898781", alpha=0.10,
                         label=f"{args.fade_mult:g}–{args.amber_mult:g}x band")
        breach_x = [d for d, v, b in zip(dates, vals, band) if v > b > 0]
        breach_y = [v for v, b in zip(vals, band) if v > b > 0]
        axd.scatter(breach_x, breach_y, color=C_RED, s=22, zorder=5,
                    label="over")
        low_x = [d for d, v, b in zip(dates, vals, lo_band)
                 if b > 0 and v < b]
        low_y = [v for v, b in zip(vals, lo_band) if b > 0 and v < b]
        if low_x:
            axd.scatter(low_x, low_y, color=C_FALL, s=22, zorder=5,
                        marker="v", label="under")
        axd.set_title(f"{r['label']} — spend/day", fontsize=10,
                      loc="left", color="#444")
        axd.tick_params(labelsize=8)
        axd.grid(True, alpha=0.2)
        axd.legend(fontsize=7, loc="upper left", framealpha=0.85)

    fig.savefig(out_png, dpi=150, bbox_inches="tight")
    plt.close(fig)


def snapshot_and_check(conn, out_dir, last):
    """Append today's view of recent report_dates; report restatement."""
    import csv
    path = out_dir / "revenue_snapshots.csv"
    today = datetime.now().date().isoformat()
    rows = conn.execute(
        """SELECT p.date AS d, SUM(p.cost_micros)/1e6 AS spend,
                  SUM(p.conversion_value) AS rev
           FROM campaign_performance_daily_search p
           JOIN campaigns c ON c.campaign_id = p.campaign_id
           WHERE c.account_id = ? AND p.device = 'ALL' AND p.date >= ?
           GROUP BY 1 ORDER BY 1""",
        [ACCOUNT_ID,
         (last - timedelta(days=9)).isoformat()]).fetchall()
    existing = []
    if path.exists():
        with open(path, newline="", encoding="utf-8") as f:
            existing = list(csv.DictReader(f))
    seen_today = {r["report_date"] for r in existing
                  if r["sync_date"] == today}
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if not existing:
            w.writerow(["report_date", "sync_date", "spend", "revenue"])
        for r in rows:
            d = str(r["d"])[:10]
            if d not in seen_today:
                w.writerow([d, today, f"{r['spend'] or 0:.2f}",
                            f"{r['rev'] or 0:.2f}"])
    # restatement report
    hist = {}
    for r in existing:
        hist.setdefault(r["report_date"], []).append(
            (r["sync_date"], float(r["revenue"])))
    checks = []
    for r in rows:
        d = str(r["d"])[:10]
        h = sorted(hist.get(d, []))
        if h and h[0][0] != today:
            first_sync, first_rev = h[0]
            now_rev = r["rev"] or 0
            if first_rev > 0:
                chg = now_rev / first_rev - 1
                age = (datetime.strptime(today, "%Y-%m-%d")
                       - datetime.strptime(first_sync, "%Y-%m-%d")).days
                checks.append((d, first_sync, age, first_rev, now_rev, chg))
    if checks:
        print("\nLAG CHECK (revenue by report_date: first-seen vs now):")
        for d, fs, age, fr, nr, chg in checks:
            flag = "  <-- RESTATED" if abs(chg) >= 0.03 else ""
            print(f"  {d}: ${fr:,.0f} (seen {fs}) -> ${nr:,.0f} now "
                  f"({chg:+.1%} over {age}d){flag}")
        moved = [c for c in checks if abs(c[5]) >= 0.03]
        if moved:
            print("  => restatement detected on "
                  f"{len(moved)}/{len(checks)} dates — lag claim gaining "
                  "support; consider --lag-days 3")
        else:
            print("  => no material restatement so far — recent data "
                  "behaving as complete")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--db-path", default=str(DEFAULT_DB_PATH))
    p.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    p.add_argument("--baseline-days", type=int, default=14,
                   help="trailing same-day-class days in the baseline")
    p.add_argument("--trend-days", type=int, default=28)
    p.add_argument("--amber-mult", type=float, default=1.5)
    p.add_argument("--red-mult", type=float, default=2.0)
    p.add_argument("--red-streak", type=int, default=2)
    p.add_argument("--lag-days", type=int, default=0,
                   help="trailing days excluded from ROAS. Default 0 "
                        "per 8/20 ruling: recent data treated as "
                        "complete; the lag-restatement claim is "
                        "unproven")
    p.add_argument("--mroas-window", type=int, default=7,
                   help="mature-ROAS window length in days")
    p.add_argument("--fade-mult", type=float, default=0.67,
                   help="fading: spend below this x baseline")
    p.add_argument("--fallout-mult", type=float, default=0.5,
                   help="fall-out: any day below this x baseline")
    p.add_argument("--fallout-streak", type=int, default=2,
                   help="consecutive fading days that escalate to "
                        "fall-out")
    p.add_argument("--quiet-base", type=float, default=5.0,
                   help="baseline $/day below which a segment is 'quiet'")
    p.add_argument("--quiet-floor", type=float, default=25.0,
                   help="spend $/day at which a quiet segment flags "
                        "'appearing'")
    args = p.parse_args()

    db_path = Path(args.db_path)
    if not db_path.exists():
        sys.exit(f"DB not found: {db_path}")
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    conn = connect_ro(db_path)

    row = conn.execute(
        """SELECT MAX(p.report_date) FROM keyword_performance_daily p
           JOIN keyword_instances ki ON ki.id = p.instance_id
           WHERE ki.account_id = ?""", [ACCOUNT_ID]).fetchone()
    if not row or not row[0]:
        sys.exit("No kpd data found.")
    last = datetime.strptime(str(row[0])[:10], "%Y-%m-%d").date()
    # extra history so every trend day has a full same-class baseline
    start = last - timedelta(days=args.trend_days
                             + args.baseline_days * 2 + 14)
    snapshot_and_check(conn, out_dir, last)
    seg, seg_rev, mobile, mobile_rev = fetch_daily(conn, start, last)
    camp_daily, camp_rev, camp_names = fetch_campaign_daily(conn, start,
                                                            last)
    seg_by_camp = fetch_seg_by_campaign(conn, start, last)
    targets = fetch_targets(conn, list(camp_daily.keys()))
    conn.close()
    mature_end = last - timedelta(days=args.lag_days)

    dates = [last - timedelta(days=k)
             for k in range(args.trend_days - 1, -1, -1)]

    rows = []
    for label, _ in CARRIER_PATTERNS:
        rows.append({"label": label, "series": seg[label],
                     "ev": evaluate(seg[label], dates, args),
                     "mroas": mature_roas(seg[label], seg_rev[label],
                                          dates, mature_end,
                                          args.mroas_window)})
    rows.append({"label": OTHER_LABEL, "series": seg[OTHER_LABEL],
                 "ev": evaluate(seg[OTHER_LABEL], dates, args),
                 "mroas": mature_roas(seg[OTHER_LABEL],
                                      seg_rev[OTHER_LABEL], dates,
                                      mature_end, args.mroas_window)})
    rows.append({"label": MOBILE_LABEL, "series": mobile,
                 "ev": evaluate(mobile, dates, args),
                 "mroas": mature_roas(mobile, mobile_rev, dates,
                                      mature_end, args.mroas_window)})
    rows.sort(key=lambda r: (STATUS_ORDER[r["ev"]["status"]],
                             -r["ev"]["spend"]))

    # campaign-grain rows
    camp_rows = []
    for cid, series in camp_daily.items():
        ev = evaluate(series, dates, args)
        tgt = targets.get(cid)
        mr = mature_roas(series, camp_rev.get(cid, {}), dates,
                         mature_end, args.mroas_window)
        camp_rows.append({"label": short_campaign(camp_names.get(cid, cid)),
                          "cid": cid, "series": series, "ev": ev,
                          "mroas": mr, "target": tgt,
                          "gap": (mr - tgt) if (mr is not None and tgt)
                          else None,
                          "gap_series": gap_series(
                              series, camp_rev.get(cid, {}), dates,
                              mature_end, args.mroas_window, tgt)
                          if tgt else None})
    camp_rows.sort(key=lambda r: (STATUS_ORDER[r["ev"]["status"]],
                                  -r["ev"]["spend"]))
    contrib = {}
    if seg_by_camp:
        for r in camp_rows:
            if r["ev"]["status"] == "RED":
                contrib[r["cid"]] = contribution(
                    seg_by_camp.get(r["cid"]), dates,
                    max(r["ev"]["streak"], 1), args.baseline_days, args)

    stamp = last.isoformat()
    out_png = out_dir / f"carrier_tripwire_{stamp}.png"
    render(rows, dates, args, out_png)
    out_png_c = out_dir / f"campaign_tripwire_{stamp}.png"
    render(camp_rows, dates, args, out_png_c,
           title="Campaign spend tripwire", contrib=contrib)

    lines = [f"CARRIER SPEND TRIPWIRE — {stamp}",
             f"(vs {args.baseline_days}d same-day-class baseline; up: "
             f"amber >{args.amber_mult:g}x, red {args.red_streak}d or "
             f">{args.red_mult:g}x; down: fading <{args.fade_mult:g}x, "
             f"fall-out {args.fallout_streak}d or <{args.fallout_mult:g}x; "
             + (f"ROAS = trailing {args.mroas_window}d thru {mature_end})"
                if args.lag_days == 0 else
                f"mROAS = {args.mroas_window}d mature ROAS thru "
                f"{mature_end})"), ""]
    for r in rows:
        ev = r["ev"]
        d = f"{ev['delta']:+.0%}" if ev["delta"] is not None else "  — "
        mr = r.get("mroas")
        lines.append(f"{status_text(ev['status'], ev['streak'], ev.get('post_expansion')).upper():20s}"
                     f" {r['label']:16s} ${ev['spend']:>8,.0f}  base "
                     f"${ev['base']:>8,.0f}  {d}  ROAS "
                     f"{mr:.2f}" if mr is not None else
                     f"{status_text(ev['status'], ev['streak'], ev.get('post_expansion')).upper():20s}"
                     f" {r['label']:16s} ${ev['spend']:>8,.0f}  base "
                     f"${ev['base']:>8,.0f}  {d}  ROAS —")
    lines += ["", f"CAMPAIGN VIEW — {stamp}"]
    for r in camp_rows:
        ev = r["ev"]
        d = f"{ev['delta']:+.0%}" if ev["delta"] is not None else "  — "
        mr, g = r.get("mroas"), r.get("gap")
        extra = (f"  ROAS {mr:.2f}" if mr is not None else "  ROAS —")
        extra += (f" ({'+' if g >= 0 else ''}{g:.2f} vs tgt)"
                  if g is not None else "")
        lines.append(f"{status_text(ev['status'], ev['streak'], ev.get('post_expansion')).upper():20s}"
                     f" {r['label']:14s} ${ev['spend']:>8,.0f}  base "
                     f"${ev['base']:>8,.0f}  {d}{extra}")
        for seg_name, dv in (contrib.get(r["cid"]) or [])[:4]:
            lines.append(f"                   └ {seg_name:14s} "
                         f"{'+' if dv >= 0 else ''}{dv:,.0f}/day")
    text = "\n".join(lines)
    print(text)
    (out_dir / f"carrier_tripwire_{stamp}.txt").write_text(
        text, encoding="utf-8")
    (out_dir / "latest.txt").write_text(text, encoding="utf-8")
    print(f"\nSaved: {out_png}")
    print(f"Saved: {out_png_c}")


if __name__ == "__main__":
    main()
