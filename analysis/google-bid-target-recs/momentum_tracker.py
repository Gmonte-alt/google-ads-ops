"""
Momentum Tracker — Google Home (daily)
═══════════════════════════════════════════════════════════════════════
Where is the account MOVING. For the account topline + each campaign:
spend/day, ROAS, and conversions/day as daily dots with 7d / 14d / 30d
rolling averages (ROAS rolled as ratio-of-sums), the campaign's tROAS
target as a reference line, and TARGET_ROAS change markers.

Momentum read (summary table + text): trailing 7d vs trailing 14d and
30d averages, all through the latest data date (8/20 ruling: recent
data treated as complete — no lag exclusion).
  ACCEL  = 7d above both 14d and 30d      (moving up, sustained)
  TURN+  = 7d above 14d, below 30d        (turning up off a low base)
  TURN-  = 7d below 14d, above 30d        (rolling over off a high)
  DECEL  = 7d below both                  (moving down, sustained)

Outputs (--out-dir ...\\momentum\\):
  momentum_summary_<date>.png   table: campaign x metric momentum
  momentum_<CAMPAIGN>_<date>.png  3-panel detail per campaign
  momentum_<date>.txt + latest.txt

Sources: campaign_performance_daily_search (device='ALL') JOIN
campaigns (account 1); targets/events from campaign_change_events.
DB read-only.

USAGE:
  python momentum_tracker.py
  python momentum_tracker.py --window-days 90 --campaign TIER2
"""

import argparse
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

DEFAULT_DB_PATH = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")
DEFAULT_OUT_DIR = Path(r"C:\Projects\google-ads-ops\analysis"
                       r"\profit-attribution-tracker\output\momentum")
ACCOUNT_ID = 1
MAS = [7, 14, 30]
MA_COLORS = {7: "#d62728", 14: "#2a78d6", 30: "#555555"}
REV_CANDIDATES = ["conversion_value", "conversions_value", "conv_value",
                  "primary_conv_value", "total_conv_value"]
CONV_CANDIDATES = ["conversions", "primary_conv_count", "conv_count",
                   "all_conversions"]
IMPR_CANDIDATES = ["impressions", "impr"]
CLICK_CANDIDATES = ["clicks", "click_count"]


def connect_ro(p):
    conn = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def pick_col(conn, table, candidates):
    cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    return next((c for c in candidates if c in cols), None)


def short_campaign(name):
    n = (name or "").replace("HOME INSURANCE", "").replace(
        "_STATE-GROUP_", "").replace("-NB-SEARCH-ALL-USA-STE", "").replace(
        "_COMBINED_TROAS_SEARCH_ALLDEVICE_USA-STE", "")
    return n.strip("-_ ") or name


def normalize_target(raw):
    s = str(raw or "").strip().rstrip("%")
    try:
        v = float(s)
    except ValueError:
        return None
    return v / 100.0 if v > 10 else v


def fetch(conn, start, end):
    rev = pick_col(conn, "campaign_performance_daily_search",
                   REV_CANDIDATES)
    cnv = pick_col(conn, "campaign_performance_daily_search",
                   CONV_CANDIDATES)
    imp = pick_col(conn, "campaign_performance_daily_search",
                   IMPR_CANDIDATES)
    clk = pick_col(conn, "campaign_performance_daily_search",
                   CLICK_CANDIDATES)
    rev_e = f"SUM(p.{rev})" if rev else "NULL"
    cnv_e = f"SUM(p.{cnv})" if cnv else "NULL"
    imp_e = f"SUM(p.{imp})" if imp else "NULL"
    clk_e = f"SUM(p.{clk})" if clk else "NULL"
    data, names = {}, {}
    sql = f"""
        SELECT p.campaign_id AS cid, c.campaign_name AS nm, p.date AS d,
               SUM(p.cost_micros)/1e6 AS cost, {rev_e} AS rev,
               {cnv_e} AS conv, {imp_e} AS impr, {clk_e} AS clicks
        FROM campaign_performance_daily_search p
        JOIN campaigns c ON c.campaign_id = p.campaign_id
        WHERE c.account_id=? AND p.device='ALL' AND p.date BETWEEN ? AND ?
        GROUP BY 1,2,3
    """
    for r in conn.execute(sql, [ACCOUNT_ID, start.isoformat(),
                                end.isoformat()]):
        cid = str(r["cid"])
        names[cid] = short_campaign(r["nm"])
        data.setdefault(cid, {})[str(r["d"])[:10]] = (
            r["cost"] or 0.0, r["rev"] or 0.0, r["conv"] or 0.0,
            r["impr"] or 0.0, r["clicks"] or 0.0)
    return data, names


def fetch_events(conn, cids):
    out = {c: [] for c in cids}
    try:
        ph = ",".join("?" for _ in cids)
        sql = f"""SELECT campaign_id, change_date, new_value
                  FROM campaign_change_events
                  WHERE platform='google' AND account_id=?
                    AND change_type='TARGET_ROAS'
                    AND campaign_id IN ({ph}) ORDER BY change_date"""
        for r in conn.execute(sql, [ACCOUNT_ID, *cids]):
            out[str(r["campaign_id"])].append(
                (str(r["change_date"])[:10],
                 normalize_target(r["new_value"])))
    except sqlite3.Error:
        pass
    return out


def roll(vals, dates, i, n, num_idx, den_idx=None):
    """Ratio-of-sums over trailing n days ending at index i."""
    lo = max(0, i - n + 1)
    num = sum(vals.get(dates[k].isoformat(), (0, 0, 0, 0, 0))[num_idx]
              for k in range(lo, i + 1))
    if den_idx is None:
        return num / (i - lo + 1)
    den = sum(vals.get(dates[k].isoformat(), (0, 0, 0, 0, 0))[den_idx]
              for k in range(lo, i + 1))
    return (num / den) if den > 0 else None


def series(vals, dates, num_idx, den_idx=None):
    daily, mas = [], {n: [] for n in MAS}
    for i, d in enumerate(dates):
        v = vals.get(d.isoformat())
        if den_idx is None:
            daily.append(v[num_idx] if v else 0.0)
        else:
            daily.append((v[num_idx] / v[den_idx])
                         if v and v[den_idx] > 0 else None)
        for n in MAS:
            mas[n].append(roll(vals, dates, i, n, num_idx, den_idx))
    return daily, mas


def momentum(mas):
    a7, a14, a30 = mas[7][-1], mas[14][-1], mas[30][-1]
    if None in (a7, a14, a30) or (a7 == a14 == a30 == 0):
        return None, "—"
    if a7 >= a14 >= a30 or (a7 >= a14 and a7 >= a30):
        tag = "ACCEL" if a7 >= a14 >= a30 else "TURN+"
    elif a7 < a14 and a7 < a30:
        tag = "DECEL"
    else:
        tag = "TURN-" if a7 < a14 else "TURN+"
    return (a7, a14, a30), tag


TAG_COLOR = {"ACCEL": "#0ca30c", "TURN+": "#639922", "TURN-": "#c98500",
             "DECEL": "#d03b3b", "—": "#888888"}


ECON_PANELS = [("Spend / day ($)", 0, None), ("ROAS", 1, 0),
               ("Conversions / day", 2, None),
               ("RPL ($/lead)", 1, 2), ("CPL ($/lead)", 0, 2)]
FUNNEL_PANELS = [("Impressions / day", 3, None),
                 ("Clicks / day", 4, None), ("CTR", 4, 3)]


def detail_chart(name, dates, vals, events, target, out_png,
                 panels=None, title_suffix="momentum"):
    panels = panels or ECON_PANELS
    fig, axes = plt.subplots(len(panels), 1,
                             figsize=(12, 3.1 * len(panels)),
                             sharex=True)
    if len(panels) == 1:
        axes = [axes]
    fig.suptitle(f"{name} — {title_suffix} (7/14/30d)", fontsize=13,
                 fontweight="bold", x=0.12, ha="left")
    for ax, (label, ni, di) in zip(axes, panels):
        daily, mas = series(vals, dates, ni, di)
        xs = dates
        ax.scatter(xs, [v if v is not None else float("nan")
                        for v in daily], s=10, color="#aaa", alpha=0.7,
                   label="daily")
        for n in MAS:
            ax.plot(xs, [v if v is not None else float("nan")
                         for v in mas[n]], lw=1.6 if n == 7 else 1.2,
                    color=MA_COLORS[n], label=f"{n}d avg",
                    alpha=0.95 if n == 7 else 0.8)
        if label == "ROAS" and target:
            ax.axhline(target, color="#333", lw=1, ls=":",
                       label=f"target {target:.2f}")
            ax.set_ylim(0, max(3.0, target * 2))
        if label == "CTR":
            ax.yaxis.set_major_formatter(
                plt.FuncFormatter(lambda v, _: f"{v:.1%}"))
        for ev_date, _ in events:
            try:
                ed = datetime.strptime(ev_date, "%Y-%m-%d").date()
                if dates[0] <= ed <= dates[-1]:
                    ax.axvline(ed, color="red", ls="--", lw=0.9,
                               alpha=0.6)
            except ValueError:
                pass
        ax.set_ylabel(label, fontsize=10)
        ax.grid(True, alpha=0.25)
        ax.tick_params(labelsize=9)
        if ax is axes[0]:
            ax.legend(fontsize=8, ncol=5, loc="upper left",
                      framealpha=0.85)
    fig.autofmt_xdate()
    fig.tight_layout(rect=[0, 0, 1, 0.97])
    fig.savefig(out_png, dpi=140, bbox_inches="tight")
    plt.close(fig)


def summary_chart(rows, last, out_png):
    n = len(rows)
    fig, ax = plt.subplots(figsize=(12, 1.4 + 0.5 * n))
    ax.axis("off")
    ax.set_xlim(0, 1)
    ax.set_ylim(0, n + 1.6)
    ax.text(0, n + 1.25, f"Momentum summary — {last}", fontsize=13,
            fontweight="bold")
    ax.text(0, n + 0.85, "trailing 7d level, vs 14d and 30d averages "
            "(all through latest date)", fontsize=8.5, color="#666")
    heads = [(0.00, ""), (0.16, "Spend/d 7d"), (0.25, "vs14"),
             (0.31, "vs30"), (0.37, "mom"),
             (0.46, "ROAS 7d"), (0.54, "vs14"), (0.60, "vs30"),
             (0.66, "mom"),
             (0.75, "Conv/d 7d"), (0.84, "vs14"), (0.90, "vs30"),
             (0.96, "mom")]
    for x, h in heads:
        ax.text(x, n + 0.4, h, fontsize=8.5, color="#888")
    for i, r in enumerate(rows):
        y = n - 1 - i + 0.15
        ax.axhline(n - 1 - i, color="#e5e5e5", lw=0.5)
        ax.text(0.00, y, r["name"], fontsize=9.5, fontweight="bold")
        for base_x, key in [(0.16, "spend"), (0.46, "roas"),
                            (0.75, "conv")]:
            m = r[key]
            if m[0] is None:
                ax.text(base_x, y, "—", fontsize=9, color="#999")
                continue
            (a7, a14, a30), tag = m
            fmt = (lambda v: f"{v:,.0f}") if key != "roas" else \
                (lambda v: f"{v:.2f}")
            dv = (lambda c, b: (f"{c/b-1:+.0%}" if b else "n/a")
                  if key != "roas" else f"{c-b:+.2f}")
            ax.text(base_x, y, fmt(a7), fontsize=9.5)
            ax.text(base_x + 0.09, y, dv(a7, a14), fontsize=9,
                    color="#0ca30c" if a7 >= a14 else "#d03b3b")
            ax.text(base_x + 0.15, y, dv(a7, a30), fontsize=9,
                    color="#0ca30c" if a7 >= a30 else "#d03b3b")
            ax.text(base_x + 0.21, y, tag, fontsize=8.5,
                    color=TAG_COLOR[tag], fontweight="bold")
    fig.savefig(out_png, dpi=150, bbox_inches="tight")
    plt.close(fig)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--db-path", default=str(DEFAULT_DB_PATH))
    p.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    p.add_argument("--window-days", type=int, default=90,
                   help="chart window (>=30 for the 30d average)")
    p.add_argument("--campaign", default=None,
                   help="substring filter — detail chart(s) only for "
                        "matching campaigns (summary always includes all)")
    args = p.parse_args()

    db = Path(args.db_path)
    if not db.exists():
        sys.exit(f"DB not found: {db}")
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    conn = connect_ro(db)
    row = conn.execute(
        """SELECT MAX(p.date) FROM campaign_performance_daily_search p
           JOIN campaigns c ON c.campaign_id=p.campaign_id
           WHERE c.account_id=?""", [ACCOUNT_ID]).fetchone()
    if not row or not row[0]:
        sys.exit("No campaign data found.")
    last = datetime.strptime(str(row[0])[:10], "%Y-%m-%d").date()
    start = last - timedelta(days=args.window_days + 30)
    data, names = fetch(conn, start, last)
    events = fetch_events(conn, list(data.keys()))
    conn.close()

    dates = [last - timedelta(days=k)
             for k in range(args.window_days - 1, -1, -1)]

    # account topline = sum across campaigns
    acct = {}
    for series_ in data.values():
        for d, (c, r, v, im, cl) in series_.items():
            a = acct.get(d, (0.0, 0.0, 0.0, 0.0, 0.0))
            acct[d] = (a[0] + c, a[1] + r, a[2] + v, a[3] + im,
                       a[4] + cl)

    entities = [("ACCOUNT", acct, [], None)]
    for cid, vals in sorted(data.items(),
                            key=lambda kv: -sum(x[0] for x in
                                                kv[1].values())):
        tgt = None
        evs = events.get(cid, [])
        if evs:
            tgt = evs[-1][1]
        entities.append((names[cid], vals, evs, tgt))

    stamp = last.isoformat()
    rows, lines = [], [f"MOMENTUM — {stamp} (7d vs 14d vs 30d, all "
                       f"through latest date)", ""]
    for name, vals, evs, tgt in entities:
        r = {"name": name}
        for key, ni, di in [("spend", 0, None), ("roas", 1, 0),
                            ("conv", 2, None), ("rpl", 1, 2),
                            ("cpl", 0, 2), ("impr", 3, None),
                            ("clicks", 4, None), ("ctr", 4, 3)]:
            _, mas = series(vals, dates, ni, di)
            r[key] = momentum(mas)
        rows.append(r)
        def cell(m, kind="num"):
            if m[0] is None:
                return "—"
            (a7, a14, a30), tag = m
            if kind == "ratio":
                return (f"{a7:.2f} ({a7-a14:+.2f}/14d, {a7-a30:+.2f}/30d)"
                        f" {tag}")
            if kind == "pct":
                return (f"{a7:.1%} ({a7-a14:+.1%}/14d, {a7-a30:+.1%}/30d)"
                        f" {tag}")
            p14 = f"{a7/a14-1:+.0%}" if a14 else "n/a"
            p30 = f"{a7/a30-1:+.0%}" if a30 else "n/a"
            return f"{a7:,.0f} ({p14}/14d, {p30}/30d) {tag}"
        lines.append(f"{name:14s} spend {cell(r['spend']):38s} "
                     f"roas {cell(r['roas'], 'ratio'):32s} "
                     f"conv {cell(r['conv'])}")
        lines.append(f"{'':14s} rpl   {cell(r['rpl']):38s} "
                     f"cpl  {cell(r['cpl']):32s} "
                     f"ctr  {cell(r['ctr'], 'pct')}")
        lines.append(f"{'':14s} impr  {cell(r['impr']):38s} "
                     f"clk  {cell(r['clicks'])}")
        if args.campaign and args.campaign.upper() not in name.upper() \
                and name != "ACCOUNT":
            continue
        safe = name.replace("/", "_").replace(" ", "_")
        detail_chart(name, dates, vals, evs, tgt,
                     out_dir / f"momentum_{safe}_{stamp}.png",
                     ECON_PANELS, "economics momentum")
        detail_chart(name, dates, vals, evs, tgt,
                     out_dir / f"funnel_{safe}_{stamp}.png",
                     FUNNEL_PANELS, "funnel momentum")

    summary_chart(rows, stamp, out_dir / f"momentum_summary_{stamp}.png")
    text = "\n".join(lines)
    print(text)
    (out_dir / f"momentum_{stamp}.txt").write_text(text, encoding="utf-8")
    (out_dir / "latest.txt").write_text(text, encoding="utf-8")
    print(f"\nSaved summary + {sum(1 for _ in entities)} detail charts "
          f"to {out_dir}")


if __name__ == "__main__":
    main()
