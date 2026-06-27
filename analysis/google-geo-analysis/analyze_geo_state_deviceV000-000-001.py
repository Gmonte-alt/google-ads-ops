"""
analyze_geo_state_device.py — read-only: account-level geo-state × device RPL
over the trailing 12 weeks, with shift-share (mix vs decay) decomposition.
============================================================================
Google Home (account_id=1) ONLY. Source: campaign_performance_geo_daily,
aggregated ACROSS campaign_id up to geo_state (account as a whole, NOT split
by campaign), MOBILE/DESKTOP + a summed TOTAL.

WHAT THIS ANSWERS
  "What shifted over time, by state?" and "did blended RPL move because
  specific states decayed, or because the conversion mix shifted toward
  cheaper states?" — the account-level analogue of the Tier One mix-vs-decay
  question, in geographic coordinates.

OUTPUT STRUCTURE (week-start columns, NOT 'Period A/B')
  T1  RPL by state × week (TOTAL device). States that drive the top
      <coverage> of 12-week revenue, each on its own row, sorted by total
      revenue; all remaining states collapsed into a single "All Other" row.
  T2  (CSV) full weekly matrix: state × device_class(TOTAL/MOBILE/DESKTOP)
      × week, with rev/conv/clicks/cost/RPL/CvR/CPC.
  T3  Account blended-RPL shift-share, BASELINE window vs RECENT window,
      decomposed per state into within-state RATE effect (decay) and
      MIX-shift effect (composition). Sums to the blended-RPL change.
  CHART (optional, if matplotlib present) small-multiple weekly RPL trend
      per top-revenue state with TOTAL/MOBILE/DESKTOP lines.

TRUST NOTE (read before quoting any number)
  This table carries Google NATIVE conversions, not routed pixel-primary.
  On the reconciliation weeks the native conv/value LEVELS run ~13-15% below
  kpd primary — so geo conversions, revenue, and CvR are NOT account truth.
  The RPL *ratio* reconciles far better (~2-3% on the tested week) because the
  under-coverage is roughly proportional in numerator and denominator. PF2
  re-checks this on TWO settled weeks; if the RPL delta stays small and stable,
  RPL shape/trends/comparisons are sound. Exact pixel-primary state×device RPL
  still requires a routed geographic_view + segments.device +
  segments.conversion_action API pull. This script reports RPL *shape*, and
  prints the recon RPL offset so the level caveat travels with the output.

READ-ONLY.

    python analyze_geo_state_device.py
    python analyze_geo_state_device.py --weeks 12 --coverage 0.90 \
        --baseline-weeks 6 --recent-weeks 2 --top-chart 9
"""

import argparse
import csv
import sqlite3
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
DB = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")
ACCT = 1
GEO = "campaign_performance_geo_daily"


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--weeks", type=int, default=12, help="trailing complete weeks")
    p.add_argument("--coverage", type=float, default=0.90,
                   help="cumulative revenue share to name individually (rest -> All Other)")
    p.add_argument("--baseline-weeks", type=int, default=6,
                   help="shift-share baseline = first N weeks of the window")
    p.add_argument("--recent-weeks", type=int, default=2,
                   help="shift-share recent = last N weeks of the window")
    p.add_argument("--recon", nargs="+",
                   default=["2025-05-05:2025-05-11", "2025-04-07:2025-04-13"],
                   help="settled reconciliation weeks, start:end")
    p.add_argument("--end-date", default=None, help="override last complete Sunday")
    p.add_argument("--top-chart", type=int, default=9, help="states in the chart")
    p.add_argument("--out-weekly", default="geo_state_weekly_rpl.csv")
    p.add_argument("--out-matrix", default="geo_state_device_weekly_matrix.csv")
    p.add_argument("--out-chart", default="geo_state_rpl_trend.png")
    return p.parse_args()


def monday(d):           return d - timedelta(days=d.weekday())
def rpl(v, c):           return round(v / c, 2) if c else None
def cvr(c, k):           return round(c / k, 4) if k else None
def cpc(co, k):          return round(co / k, 2) if k else None


def resolve_weeks(conn, end_date, n_weeks):
    if end_date:
        anchor = date.fromisoformat(end_date)
    else:
        mx = conn.execute(
            f"SELECT MAX(date) m FROM {GEO} WHERE account_id=?", (ACCT,)).fetchone()["m"]
        anchor = date.fromisoformat(mx)
    # last COMPLETE Sunday on or before anchor
    last_sun = anchor - timedelta(days=(anchor.weekday() + 1) % 7)
    if last_sun > anchor:
        last_sun -= timedelta(days=7)
    first_mon = last_sun - timedelta(days=7 * n_weeks - 1)
    weeks = [first_mon + timedelta(days=7 * i) for i in range(n_weeks)]  # Monday starts
    return first_mon, last_sun, weeks


def preflight(conn, recon_specs, first_mon, last_sun):
    print("\n  PF1 — device values (account_id=1):")
    devs = conn.execute(
        f"SELECT device, COUNT(*) n FROM {GEO} WHERE account_id=? GROUP BY device ORDER BY n DESC",
        (ACCT,)).fetchall()
    has_all = any(r["device"] == "ALL" for r in devs)
    for r in devs:
        print(f"      {str(r['device']):<14} {r['n']:>10,}")
    print(f"      => 'ALL' rollup present: {has_all}. "
          f"{'TOTAL = sum of device rows (no double-count).' if not has_all else 'WARNING: ALL row exists — script still sums non-ALL rows; verify.'}")

    print("\n  PF2 — native geo conversions vs kpd primary on SETTLED weeks "
          "(watch the RPL delta, not just levels):")
    for spec in recon_specs:
        s, e = spec.split(":")
        g = conn.execute(
            f"SELECT SUM(conversions) c, SUM(conversion_value) v FROM {GEO} "
            f"WHERE account_id=? AND device<>'ALL' AND date BETWEEN ? AND ?",
            (ACCT, s, e)).fetchone()
        k = conn.execute(
            "SELECT SUM(kpd.primary_conv_count) c, SUM(kpd.primary_conv_value) v "
            "FROM keyword_performance_daily kpd JOIN keyword_instances ki ON ki.id=kpd.instance_id "
            "WHERE ki.account_id=? AND kpd.device='ALL' AND kpd.report_date BETWEEN ? AND ?",
            (ACCT, s, e)).fetchone()
        gc, gv, kc, kv = g["c"] or 0, g["v"] or 0, k["c"] or 0, k["v"] or 0
        g_rpl = gv / gc if gc else 0
        k_rpl = kv / kc if kc else 0
        dconv = 100 * (gc - kc) / kc if kc else 0
        dval = 100 * (gv - kv) / kv if kv else 0
        drpl = 100 * (g_rpl - k_rpl) / k_rpl if k_rpl else 0
        verdict = "RPL SOUND" if abs(drpl) <= 5 else "RPL SHAPE-ONLY (level off)"
        print(f"      {s}..{e}: conv {dconv:+5.1f}%  value {dval:+5.1f}%  "
              f"RPL {g_rpl:6.2f} vs {k_rpl:6.2f} ({drpl:+4.1f}%)  => {verdict}")
    print("      Trust gate: quote RPL only if the RPL delta is small AND stable across both weeks.")

    print("\n  PF3 — NULL geo_state revenue share over the 12-week window:")
    tot = conn.execute(
        f"SELECT SUM(conversion_value) r FROM {GEO} WHERE account_id=? AND device<>'ALL' "
        f"AND date BETWEEN ? AND ?", (ACCT, str(first_mon), str(last_sun))).fetchone()["r"] or 0
    nul = conn.execute(
        f"SELECT SUM(conversion_value) r FROM {GEO} WHERE account_id=? AND device<>'ALL' "
        f"AND geo_state IS NULL AND date BETWEEN ? AND ?",
        (ACCT, str(first_mon), str(last_sun))).fetchone()["r"] or 0
    print(f"      NULL-state revenue ${nul:,.2f} of ${tot:,.2f} ({(100*nul/tot if tot else 0):.2f}%)")


def weekly_rollup(conn, first_mon, last_sun):
    """{week_monday: {state: {dev_class: {rev,conv,clicks,cost}}}} for TOTAL/MOBILE/DESKTOP."""
    rows = conn.execute(
        f"SELECT date, geo_state, device, "
        f"       SUM(conversion_value) rev, SUM(conversions) conv, "
        f"       SUM(clicks) clicks, SUM(cost_micros)/1e6 cost "
        f"FROM {GEO} WHERE account_id=? AND device<>'ALL' AND geo_state IS NOT NULL "
        f"AND date BETWEEN ? AND ? GROUP BY date, geo_state, device",
        (ACCT, str(first_mon), str(last_sun))).fetchall()
    W = defaultdict(lambda: defaultdict(lambda: defaultdict(
        lambda: {"rev": 0.0, "conv": 0.0, "clicks": 0, "cost": 0.0})))
    for r in rows:
        wk = monday(date.fromisoformat(r["date"]))
        st = r["geo_state"]
        for cls in ("TOTAL", r["device"]):
            if cls not in ("TOTAL", "MOBILE", "DESKTOP"):
                continue
            c = W[wk][st][cls]
            c["rev"] += r["rev"] or 0; c["conv"] += r["conv"] or 0
            c["clicks"] += r["clicks"] or 0; c["cost"] += r["cost"] or 0
    return W


def select_states(W, weeks, coverage):
    """Rank states by total 12-week TOTAL revenue; name to <coverage>, rest -> All Other."""
    rev = defaultdict(float)
    for wk in weeks:
        for st, dd in W.get(wk, {}).items():
            rev[st] += dd.get("TOTAL", {}).get("rev", 0.0)
    ranked = sorted(rev.items(), key=lambda kv: -kv[1])
    grand = sum(v for _, v in ranked) or 1.0
    named, cum = [], 0.0
    for st, v in ranked:
        named.append(st); cum += v
        if cum / grand >= coverage:
            break
    other = [st for st, _ in ranked if st not in set(named)]
    return named, other, rev, grand


def collapse(W, weeks, named, other):
    """Return {week: {bucket: {cls: agg}}} with 'All Other' as a synthetic bucket."""
    out = {}
    namedset = set(named)
    for wk in weeks:
        out[wk] = {}
        for st, dd in W.get(wk, {}).items():
            bucket = st if st in namedset else "All Other"
            b = out[wk].setdefault(bucket, defaultdict(
                lambda: {"rev": 0.0, "conv": 0.0, "clicks": 0, "cost": 0.0}))
            for cls, c in dd.items():
                t = b[cls]
                t["rev"] += c["rev"]; t["conv"] += c["conv"]
                t["clicks"] += c["clicks"]; t["cost"] += c["cost"]
    return out


def shift_share(C, weeks, base_n, recent_n):
    """Blended RPL: baseline (first base_n weeks) vs recent (last recent_n weeks).
       Decompose into within-bucket RATE effect and MIX-shift effect."""
    base_w = weeks[:base_n]
    rec_w = weeks[-recent_n:]

    def agg(window):
        d = defaultdict(lambda: {"rev": 0.0, "conv": 0.0})
        for wk in window:
            for b, cc in C.get(wk, {}).items():
                t = cc.get("TOTAL", {})
                d[b]["rev"] += t.get("rev", 0.0); d[b]["conv"] += t.get("conv", 0.0)
        return d

    A, B = agg(base_w), agg(rec_w)
    totA = sum(v["conv"] for v in A.values()) or 1.0
    totB = sum(v["conv"] for v in B.values()) or 1.0
    blendA = sum(v["rev"] for v in A.values()) / totA
    blendB = sum(v["rev"] for v in B.values()) / totB

    rows = []
    rate_sum = mix_sum = inter_sum = 0.0
    for b in sorted(set(A) | set(B)):
        shA = A[b]["conv"] / totA if b in A else 0
        shB = B[b]["conv"] / totB if b in B else 0
        rA = A[b]["rev"] / A[b]["conv"] if A.get(b, {}).get("conv") else 0
        rB = B[b]["rev"] / B[b]["conv"] if B.get(b, {}).get("conv") else 0
        re_, me_, it_ = shA * (rB - rA), rA * (shB - shA), (shB - shA) * (rB - rA)
        rate_sum += re_; mix_sum += me_; inter_sum += it_
        rows.append((b, rA, rB, shA, shB, re_, me_))
    return (base_w, rec_w, blendA, blendB, rate_sum, mix_sum, inter_sum,
            sorted(rows, key=lambda x: x[5]))  # sort by rate effect (decay first)


def print_state_week_table(C, weeks, named, rev):
    print("\n  T1 — RPL by state × week (TOTAL device). Sorted by 12-week revenue. "
          "Native-conv basis; read as shape.\n")
    wkhdr = "  {:<16}".format("state") + "".join("{:>9}".format(w.strftime("%m-%d")) for w in weeks) + "{:>11}".format("12w rev")
    print(wkhdr); print("  " + "-" * (len(wkhdr) - 2))
    order = named + ["All Other"]
    for b in order:
        vals = []
        for w in weeks:
            t = C.get(w, {}).get(b, {}).get("TOTAL")
            vals.append(rpl(t["rev"], t["conv"]) if t else None)
        rline = "  {:<16}".format(b) + "".join(
            ("{:>9.0f}".format(v) if v is not None else "{:>9}".format("—")) for v in vals)
        tot = rev.get(b, sum(rev.get(s, 0) for s in []))  # rev dict only has real states
        if b == "All Other":
            tot = sum(v for s, v in rev.items() if s not in set(named))
        rline += "{:>11}".format("${:,.0f}".format(tot))
        print(rline)


def write_weekly_total_csv(path, C, weeks, named):
    with Path(path).open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["bucket"] + [wk.strftime("%Y-%m-%d") for wk in weeks])
        for b in named + ["All Other"]:
            row = [b]
            for wk in weeks:
                t = C.get(wk, {}).get(b, {}).get("TOTAL")
                row.append(rpl(t["rev"], t["conv"]) if t else "")
            w.writerow(row)


def write_matrix_csv(path, C, weeks, named):
    with Path(path).open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["bucket", "device_class", "week_start",
                    "revenue", "conversions", "clicks", "cost", "RPL", "CvR", "CPC"])
        for b in named + ["All Other"]:
            for cls in ("TOTAL", "MOBILE", "DESKTOP"):
                for wk in weeks:
                    c = C.get(wk, {}).get(b, {}).get(cls)
                    if not c:
                        continue
                    w.writerow([b, cls, wk.strftime("%Y-%m-%d"),
                                round(c["rev"], 2), round(c["conv"], 1), c["clicks"],
                                round(c["cost"], 2), rpl(c["rev"], c["conv"]),
                                cvr(c["conv"], c["clicks"]), cpc(c["cost"], c["clicks"])])


def make_chart(path, C, weeks, named, top_n):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception as ex:
        print(f"\n  (chart skipped — matplotlib unavailable: {ex})")
        return
    states = named[:top_n]
    ncol = 3
    nrow = (len(states) + ncol - 1) // ncol
    fig, axes = plt.subplots(nrow, ncol, figsize=(4.2 * ncol, 2.8 * nrow), squeeze=False)
    xs = list(range(len(weeks)))
    xlabels = [w.strftime("%m-%d") for w in weeks]
    for i, st in enumerate(states):
        ax = axes[i // ncol][i % ncol]
        for cls, style in (("TOTAL", "-"), ("MOBILE", "--"), ("DESKTOP", ":")):
            ys = []
            for w in weeks:
                t = C.get(w, {}).get(st, {}).get(cls)
                ys.append(t["rev"] / t["conv"] if (t and t["conv"]) else None)
            ax.plot(xs, ys, style, marker="o", ms=2.5, label=cls, color="#6b9b4a" if cls == "TOTAL" else None)
        ax.set_title(st, fontsize=10, fontweight="bold")
        ax.set_xticks(xs[::2]); ax.set_xticklabels(xlabels[::2], fontsize=7, rotation=45)
        ax.tick_params(axis="y", labelsize=7); ax.grid(alpha=0.3)
        if i == 0:
            ax.legend(fontsize=7)
    for j in range(len(states), nrow * ncol):
        axes[j // ncol][j % ncol].axis("off")
    fig.suptitle("Weekly RPL by state (native-conv basis; shape) — TOTAL / MOBILE / DESKTOP",
                 fontsize=12, fontweight="bold")
    fig.tight_layout(rect=[0, 0, 1, 0.97])
    fig.savefig(path, dpi=130)
    print(f"\n  chart -> {Path(path).resolve()}")


def main():
    a = parse_args()
    if not DB.exists():
        sys.exit(f"DB not found: {DB}")
    conn = sqlite3.connect(DB); conn.row_factory = sqlite3.Row

    first_mon, last_sun, weeks = resolve_weeks(conn, a.end_date, a.weeks)
    print("=" * 78)
    print(f"  Google Home (account_id={ACCT}) — geo-state × device, last {a.weeks} weeks")
    print(f"  window {first_mon} .. {last_sun}   src: {GEO}   (account whole, not by campaign)")
    print("=" * 78)

    preflight(conn, a.recon, first_mon, last_sun)
    W = weekly_rollup(conn, first_mon, last_sun)
    conn.close()

    named, other, rev, grand = select_states(W, weeks, a.coverage)
    print(f"\n  States to {a.coverage*100:.0f}% of {a.weeks}-week revenue: "
          f"{len(named)} named + 'All Other' ({len(other)} states). Grand ${grand:,.0f}.")

    C = collapse(W, weeks, named, other)
    print_state_week_table(C, weeks, named, rev)

    bw, rw, blendA, blendB, rate, mix, inter, rows = shift_share(C, weeks, a.baseline_weeks, a.recent_weeks)
    print(f"\n  T3 — Account blended-RPL shift-share")
    print(f"      baseline {bw[0].strftime('%m-%d')}..{(bw[-1]+timedelta(days=6)).strftime('%m-%d')} "
          f"vs recent {rw[0].strftime('%m-%d')}..{(rw[-1]+timedelta(days=6)).strftime('%m-%d')}")
    print(f"      blended RPL {blendA:.2f} -> {blendB:.2f}  (Δ {blendB-blendA:+.2f})")
    print(f"        within-state RATE (decay) : {rate:+.2f}")
    print(f"        MIX-shift (composition)   : {mix:+.2f}")
    print(f"        interaction               : {inter:+.2f}")
    print(f"      top decay contributors (RPL_base -> RPL_recent, share shift):")
    for b, rA, rB, shA, shB, re_, me_ in rows[:8]:
        print(f"        {b:<16} {rA:6.1f} -> {rB:6.1f}   rate {re_:+5.2f}  mix {me_:+5.2f}  "
              f"(share {shA*100:4.1f}% -> {shB*100:4.1f}%)")

    write_weekly_total_csv(a.out_weekly, C, weeks, named)
    write_matrix_csv(a.out_matrix, C, weeks, named)
    print(f"\n  weekly TOTAL RPL  -> {Path(a.out_weekly).resolve()}")
    print(f"  full device matrix-> {Path(a.out_matrix).resolve()}")
    make_chart(a.out_chart, C, weeks, named, a.top_chart)

    print("\n  Reminder: native-conv basis. Levels (conv/rev/CvR) are ~13-15% under")
    print("  pixel-primary; RPL ratio reconciles ~2-3%. Quote RPL as shape, not level,")
    print("  unless PF2 RPL delta is small AND stable on both recon weeks.")


if __name__ == "__main__":
    main()
