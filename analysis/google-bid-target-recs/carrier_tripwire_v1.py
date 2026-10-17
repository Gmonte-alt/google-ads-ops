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
    sql = """
        SELECT p.report_date AS d, LOWER(ki.keyword_text) AS kw,
               SUM(p.cost_micros) / 1e6 AS spend
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

    mobile = {}
    sql_m = """
        SELECT p.report_date AS d, SUM(p.cost_micros) / 1e6 AS spend
        FROM keyword_performance_daily p
        JOIN keyword_instances ki
          ON ki.id = p.instance_id AND ki.account_id = ?
        WHERE p.device = 'MOBILE' AND p.report_date BETWEEN ? AND ?
        GROUP BY 1
    """
    for r in conn.execute(sql_m, [ACCOUNT_ID, start.isoformat(),
                                  end.isoformat()]):
        mobile[str(r["d"])[:10]] = r["spend"] or 0.0
    return seg, mobile


def fetch_campaign_daily(conn, start, end):
    """Campaign-grain spend/day (device='ALL'), scoped to account."""
    out = {}
    sql = """
        SELECT p.campaign_id, c.campaign_name, p.date AS d,
               SUM(p.cost_micros) / 1e6 AS spend
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
    return out, names


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
    cols = [(0.00, "Segment"), (0.24, "Yday $"), (0.34, "Base $"),
            (0.44, "Δ"), (0.52, "Days"), (0.58, "28d trend"),
            (0.86, "Status")]
    for x, h in cols:
        ax.text(x, len(rows) + 0.6, h, fontsize=9, color="#888")
    for i, r in enumerate(rows):
        y = len(rows) - 1 - i + 0.15
        ev = r["ev"]
        ax.text(0.00, y, r["label"], fontsize=10, fontweight="bold")
        ax.text(0.24, y, f"${ev['spend']:,.0f}", fontsize=10)
        ax.text(0.34, y, f"${ev['base']:,.0f}", fontsize=10, color="#666")
        ax.text(0.44, y, (f"{ev['delta']:+.0%}" if ev["delta"] is not None
                          else "—"), fontsize=10,
                color=status_color(ev["status"])
                if ev["status"] not in ("OK", "QUIET") else "#666")
        ax.text(0.52, y, str(ev["streak"]) if ev["streak"] else "",
                fontsize=10, color="#666")
        # sparkline
        xs = [0.58 + 0.24 * k / max(len(dates) - 1, 1)
              for k in range(len(dates))]
        vals = [r["series"].get(d.isoformat(), 0.0) for d in dates]
        vmax = max(vals) or 1.0
        ys = [y - 0.05 + 0.30 * v / vmax for v in vals]
        ax.plot(xs, ys, lw=1.2, color=status_color(ev["status"])
                if ev["status"] != "OK" else "#898781")
        ax.text(0.86, y, status_text(ev["status"], ev["streak"],
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
    seg, mobile = fetch_daily(conn, start, last)
    camp_daily, camp_names = fetch_campaign_daily(conn, start, last)
    seg_by_camp = fetch_seg_by_campaign(conn, start, last)
    conn.close()

    dates = [last - timedelta(days=k)
             for k in range(args.trend_days - 1, -1, -1)]

    rows = []
    for label, _ in CARRIER_PATTERNS:
        rows.append({"label": label, "series": seg[label],
                     "ev": evaluate(seg[label], dates, args)})
    rows.append({"label": OTHER_LABEL, "series": seg[OTHER_LABEL],
                 "ev": evaluate(seg[OTHER_LABEL], dates, args)})
    rows.append({"label": MOBILE_LABEL, "series": mobile,
                 "ev": evaluate(mobile, dates, args)})
    rows.sort(key=lambda r: (STATUS_ORDER[r["ev"]["status"]],
                             -r["ev"]["spend"]))

    # campaign-grain rows
    camp_rows = []
    for cid, series in camp_daily.items():
        ev = evaluate(series, dates, args)
        camp_rows.append({"label": short_campaign(camp_names.get(cid, cid)),
                          "cid": cid, "series": series, "ev": ev})
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
             f"fall-out {args.fallout_streak}d or <{args.fallout_mult:g}x)",
             ""]
    for r in rows:
        ev = r["ev"]
        d = f"{ev['delta']:+.0%}" if ev["delta"] is not None else "  — "
        lines.append(f"{status_text(ev['status'], ev['streak'], ev.get('post_expansion')).upper():20s}"
                     f" {r['label']:16s} ${ev['spend']:>8,.0f}  base "
                     f"${ev['base']:>8,.0f}  {d}")
    lines += ["", f"CAMPAIGN VIEW — {stamp}"]
    for r in camp_rows:
        ev = r["ev"]
        d = f"{ev['delta']:+.0%}" if ev["delta"] is not None else "  — "
        lines.append(f"{status_text(ev['status'], ev['streak'], ev.get('post_expansion')).upper():20s}"
                     f" {r['label']:14s} ${ev['spend']:>8,.0f}  base "
                     f"${ev['base']:>8,.0f}  {d}")
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
