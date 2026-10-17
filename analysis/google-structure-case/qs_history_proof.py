#!/usr/bin/env python
r"""
qs_history_proof.py — Anchor the slide claim: "impression-weighted QS
is at its lowest in account history."
═══════════════════════════════════════════════════════════════════════
Computes the monthly account-level impression-weighted QS series over
the full data horizon (ALL account-1 campaigns — legacy eras included,
because "account history" must mean the whole account), plus monthly
clicks/impressions, and emits:

  1. The slide sentence with numbers filled (current iw-QS, rank vs
     all complete months on record, the peak month/value, the data
     start date).
  2. The click-decline anchor (peak month -> latest, % decline,
     months-declining count).
  3. The Aug-landmine reconciliation sentence: the expansion-week
     iw-QS uptick vs the monthly record low — both true, one line.

HOUSE RECIPE: iw-QS = SUM(qs x impressions) / SUM(rated impressions),
joined (instance_id, report_date); QS never summed; unrated excluded;
rated_share printed for EVERY month and months below --rated-guard
(0.90) are flagged † — a claim about QS is only as good as the share
of impressions that carried a rating. device='ALL'; account scope via
the keyword_instances join. Partial current month shown but EXCLUDED
from the "lowest on record" ranking.

OUTPUT: qs_history_<date>.csv, .png (monthly iw-QS with the low
marked + monthly clicks), console proof block.

USAGE: python qs_history_proof.py
       python qs_history_proof.py --event 2026-08-16:2026-08-20
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
ACCOUNT_ID = 1
# documented ledger-reset treads (for the chart's context lines)
RESET_EVENTS = [("2024-07-15", "copy reset #1"),
                ("2024-11-01", "copy reset #2"),
                ("2025-07-28", "copy reset #3"),
                ("2026-01-01", "structural cutover")]


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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db-path", default=str(DEFAULT_DB_PATH))
    ap.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    ap.add_argument("--rated-guard", type=float, default=0.90)
    ap.add_argument("--event", default="2026-08-16:2026-08-20",
                    help="expansion event span for the "
                         "reconciliation sentence")
    args = ap.parse_args()

    conn = connect_ro(args.db_path)
    impr = pick_col(conn, "keyword_performance_daily",
                    ["impressions", "impr"])
    clk = pick_col(conn, "keyword_performance_daily", ["clicks"])
    if not (impr and clk):
        sys.exit("kpd needs impressions/clicks.")
    has_kqd = pick_col(conn, "keyword_quality_daily",
                       ["historical_quality_score"]) is not None
    if not has_kqd:
        sys.exit("keyword_quality_daily missing — no QS layer.")

    q = (f"SELECT SUBSTR(p.report_date, 1, 7) ym, "
         f"SUM(p.{impr}) im, SUM(p.{clk}) ck, "
         f"SUM(CASE WHEN q.historical_quality_score IS NOT NULL "
         f"THEN p.{impr} ELSE 0 END) ri, "
         f"SUM(CASE WHEN q.historical_quality_score IS NOT NULL "
         f"THEN q.historical_quality_score * p.{impr} ELSE 0 END) qi "
         f"FROM keyword_performance_daily p "
         f"JOIN keyword_instances ki ON ki.id = p.instance_id "
         f"AND ki.account_id = ? "
         f"LEFT JOIN keyword_quality_daily q "
         f"ON q.instance_id = p.instance_id "
         f"AND q.report_date = p.report_date "
         f"WHERE p.device = 'ALL' "
         f"GROUP BY 1 ORDER BY 1")
    months = []
    for r in conn.execute(q, [ACCOUNT_ID]):
        months.append({
            "ym": r["ym"], "im": r["im"] or 0, "ck": r["ck"] or 0,
            "ri": r["ri"] or 0,
            "iwqs": (r["qi"] / r["ri"]) if (r["ri"] or 0) > 0
            else None,
            "rated_share": (r["ri"] / r["im"]) if (r["im"] or 0) > 0
            else 0.0})
    row = conn.execute(
        f"""SELECT MAX(p.report_date) FROM keyword_performance_daily p
            JOIN keyword_instances ki ON ki.id = p.instance_id
            WHERE ki.account_id = ?""", [ACCOUNT_ID]).fetchone()
    data_end = datetime.strptime(str(row[0])[:10], "%Y-%m-%d").date()

    # event-week iw-QS for the reconciliation sentence
    ev = args.event.split(":")
    er = conn.execute(
        f"""SELECT SUM(CASE WHEN q.historical_quality_score IS NOT
            NULL THEN p.{impr} ELSE 0 END) ri,
            SUM(CASE WHEN q.historical_quality_score IS NOT NULL
            THEN q.historical_quality_score * p.{impr} ELSE 0 END) qi
            FROM keyword_performance_daily p
            JOIN keyword_instances ki ON ki.id = p.instance_id
            AND ki.account_id = ?
            LEFT JOIN keyword_quality_daily q
            ON q.instance_id = p.instance_id
            AND q.report_date = p.report_date
            WHERE p.device = 'ALL'
            AND p.report_date BETWEEN ? AND ?""",
        [ACCOUNT_ID, ev[0], ev[1]]).fetchone()
    conn.close()
    if not months:
        sys.exit("No data.")
    cur_partial = months[-1]["ym"] == data_end.strftime("%Y-%m")
    complete = [m for m in months[:-1] if m["iwqs"] is not None] \
        if cur_partial else [m for m in months if m["iwqs"] is not None]
    rated_ok = [m for m in complete
                if m["rated_share"] >= args.rated_guard]
    judged = complete  # rank on all complete months; guard flags ride
    low = min(judged, key=lambda m: m["iwqs"])
    peak = max(judged, key=lambda m: m["iwqs"])
    latest_full = judged[-1]
    rank_sorted = sorted(judged, key=lambda m: m["iwqs"])
    latest_rank = rank_sorted.index(latest_full) + 1
    ck_peak = max(judged, key=lambda m: m["ck"])
    decl = sum(1 for a, b in zip(judged[-13:-1], judged[-12:])
               if b["ck"] < a["ck"])
    ev_qs = (er["qi"] / er["ri"]) if er and (er["ri"] or 0) > 0 \
        else None

    def g(m):
        return "†" if m["rated_share"] < args.rated_guard else ""

    L = [f"QS HISTORY PROOF — account {ACCOUNT_ID}, ALL campaigns, "
         f"data {months[0]['ym']} -> {data_end} "
         f"({len(judged)} complete months"
         + (f"; current month partial, excluded from ranking)"
            if cur_partial else ")"),
         f"Recipe: iw-QS = SUM(qs x impr)/SUM(rated impr), unrated "
         f"excluded, rated_share printed; † = rated share < "
         f"{args.rated_guard:.0%} ({len(judged) - len(rated_ok)} of "
         f"{len(judged)} months flagged).", "",
         "SLIDE SENTENCES (numbers filled):",
         f"  1. \"Impression-weighted QS is at "
         f"{latest_full['iwqs']:.2f}{g(latest_full)} in "
         f"{latest_full['ym']} — "
         + (f"the lowest of all {len(judged)} months on record "
            if latest_rank == 1 else
            f"rank {latest_rank} lowest of {len(judged)} months "
            "(NOT the minimum — see below) ")
         + f"(data since {months[0]['ym']}); the account peaked at "
         f"{peak['iwqs']:.2f} in {peak['ym']}.\"",
         f"  2. \"Monthly clicks are down "
         f"{1 - latest_full['ck'] / ck_peak['ck']:.0%} from the "
         f"{ck_peak['ym']} peak ({ck_peak['ck']:,.0f} -> "
         f"{latest_full['ck']:,.0f}); clicks declined in {decl} of "
         f"the last 12 month-over-month steps.\"",
         "  3. Reconciliation (if the Aug-21 decomposition comes "
         "up): \"During the "
         f"{ev[0]}..{ev[1]} expansion the account's iw-QS ran "
         + (f"{ev_qs:.2f}" if ev_qs is not None else "n/a")
         + f" — a days-level uptick (the wave buys the best of an "
         f"already-degraded pool) inside a monthly series whose "
         f"{latest_full['ym']} value "
         f"({latest_full['iwqs']:.2f}) is "
         + ("the record low" if latest_rank == 1
            else f"rank {latest_rank} of {len(judged)}")
         + "; both are true at their own grain.\"", "",
         f"{'month':8s} {'iw-QS':>6s} {'rated%':>7s} "
         f"{'clicks':>10s} {'impressions':>12s}"]
    for m in months:
        tag = " (partial)" if (cur_partial and m is months[-1]) else ""
        L.append(f"{m['ym']:8s} "
                 + (f"{m['iwqs']:6.2f}" if m['iwqs'] is not None
                    else "     —")
                 + f"{g(m):1s}{'':1s}{m['rated_share']:6.0%} "
                 f"{m['ck']:10,.0f} {m['im']:12,.0f}{tag}")
    if latest_rank != 1:
        lower = [m["ym"] for m in rank_sorted[:latest_rank - 1]]
        L.append("")
        L.append(f"CAUTION: months below the latest full month: "
                 f"{', '.join(lower)} — if any are recent, the "
                 "claim may still hold as 'these months mark the "
                 "lowest'; check before the slide ships.")
    text = "\n".join(L)
    print(text)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = data_end.isoformat()
    csv_path = out_dir / f"qs_history_{stamp}.csv"
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["month", "iw_qs", "rated_share", "clicks",
                    "impressions", "partial"])
        for m in months:
            w.writerow([m["ym"],
                        f"{m['iwqs']:.3f}" if m["iwqs"] is not None
                        else "", f"{m['rated_share']:.3f}",
                        f"{m['ck']:.0f}", f"{m['im']:.0f}",
                        cur_partial and m is months[-1]])
    (out_dir / f"qs_history_{stamp}.txt").write_text(
        text, encoding="utf-8")

    xs = [datetime.strptime(m["ym"] + "-15", "%Y-%m-%d").date()
          for m in months]
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(11, 6.2), dpi=150,
                                   sharex=True)
    ys = [m["iwqs"] for m in months]
    ax1.plot(xs, ys, "-o", ms=3.5, lw=1.8, color="#1F3864")
    if cur_partial:
        ax1.plot(xs[-1], ys[-1], "o", ms=6, mfc="white",
                 mec="#1F3864")
    li = months.index(low)
    ax1.plot(xs[li], low["iwqs"], "v", ms=9, color="#C00000")
    ax1.annotate(f"low {low['iwqs']:.2f} ({low['ym']})",
                 (xs[li], low["iwqs"]),
                 textcoords="offset points", xytext=(8, -12),
                 fontsize=8, color="#C00000")
    for d, lbl in RESET_EVENTS:
        dd = datetime.strptime(d, "%Y-%m-%d").date()
        if xs[0] <= dd <= xs[-1]:
            ax1.axvline(dd, color="#808080", ls=":", lw=0.9)
            ax1.text(dd, ax1.get_ylim()[1], " " + lbl, fontsize=6,
                     color="#808080", va="top", rotation=90)
    for m, x in zip(months, xs):
        if m["rated_share"] < args.rated_guard:
            ax1.plot(x, m["iwqs"], "x", ms=7, color="#BF8F00")
    ax1.set_title("Account impression-weighted QS by month "
                  "(x = rated share below guard; hollow = partial "
                  "month)", fontsize=9.5)
    ax1.grid(alpha=0.25)
    ax2.bar(xs, [m["ck"] for m in months], width=22,
            color=["#BFBFBF" if (cur_partial and m is months[-1])
                   else "#2E5E8C" for m in months])
    ax2.set_title("Monthly clicks", fontsize=9.5)
    ax2.grid(axis="y", alpha=0.25)
    ax2.tick_params(labelsize=7.5, rotation=25)
    fig.suptitle(f"QS history proof — data through {stamp}",
                 fontsize=10.5)
    fig.tight_layout(rect=(0, 0, 1, 0.94))
    png_path = out_dir / f"qs_history_{stamp}.png"
    fig.savefig(png_path)
    plt.close(fig)
    print(f"\nSaved: {csv_path}")
    print(f"Saved: {png_path}")
    print(f"Saved: {out_dir / f'qs_history_{stamp}.txt'}")


if __name__ == "__main__":
    main()
