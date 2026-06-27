"""
summarize_geo_tiers.py — post-processes geo_state_device_weekly_matrix.csv
into an RPL value-tier view (the presentable layer). No DB needed.

Groups states by BASELINE (first-half) RPL into High/Mid/Low tiers, carves
California out as its own line (structural low-RPL/high-volume outlier), and
aggregates each tier weekly so the trend is noise-clean. Emits:
  - tier weekly RPL table + CSV
  - movers table (material states ranked by RPL change)
  - chart: tier RPL trends (TOTAL only — no thin-sample device split)

    python summarize_geo_tiers.py --matrix geo_state_device_weekly_matrix.csv
"""
import argparse, csv
from collections import defaultdict
from pathlib import Path

HIGH, MIDLO = 75.0, 45.0           # tier cut points (baseline RPL)
CARVE_OUT = {"California"}          # structural outliers shown standalone
MAT_REV, MAT_CONVWK = 8000, 10     # movers materiality floor

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--matrix", default="geo_state_device_weekly_matrix.csv")
    ap.add_argument("--out-tiers", default="geo_tier_weekly_rpl.csv")
    ap.add_argument("--out-vol", default="geo_tier_weekly_vol.csv")
    ap.add_argument("--out-clicks", default="geo_tier_weekly_clicks.csv")
    ap.add_argument("--out-chart", default="geo_tier_trend.png")
    a = ap.parse_args()

    rows = list(csv.DictReader(open(a.matrix)))
    weeks = sorted({r["week_start"] for r in rows})
    half = len(weeks) // 2
    base_w, rec_w = weeks[:half], weeks[half:]

    T = defaultdict(dict)  # state -> week -> {rev,conv}
    for r in rows:
        if r["device_class"] != "TOTAL":
            continue
        T[r["bucket"]][r["week_start"]] = {"rev": float(r["revenue"]), "conv": float(r["conversions"]), "clk": float(r["clicks"])}

    def wsum(state, wks):
        rv = sum(T[state][w]["rev"] for w in wks if w in T[state])
        cv = sum(T[state][w]["conv"] for w in wks if w in T[state])
        return rv, cv

    states = [s for s in T if s != "All Other"]
    info = {}
    for s in states:
        rA, cA = wsum(s, base_w); rB, cB = wsum(s, rec_w)
        info[s] = {"rplA": rA / cA if cA else 0, "rplB": rB / cB if cB else 0,
                   "rev": rA + rB, "convwk": (cA + cB) / len(weeks),
                   "d": (rB / cB) / (rA / cA) - 1 if (cA and cB and rA) else 0}

    def tier_of(s):
        if s in CARVE_OUT: return s
        r = info[s]["rplA"]
        return "High (>=75)" if r >= HIGH else ("Mid (45-75)" if r >= MIDLO else "Low (<45)")

    # weekly tier aggregation (noise-clean)
    tiers = sorted(CARVE_OUT) + ["High (>=75)", "Mid (45-75)", "Low (<45)"]
    tier_week = {t: {} for t in tiers}
    tier_vol = {t: {} for t in tiers}
    tier_clk = {t: {} for t in tiers}
    for t in tiers:
        members = [s for s in states if tier_of(s) == t]
        for w in weeks:
            rv = sum(T[s][w]["rev"] for s in members if w in T[s])
            cv = sum(T[s][w]["conv"] for s in members if w in T[s])
            tier_week[t][w] = rv / cv if cv else None
            tier_vol[t][w] = cv / 7.0     # conversions per day (same band membership)
            ck = sum(T[s][w]["clk"] for s in members if w in T[s])
            tier_clk[t][w] = ck / 7.0     # clicks per day (same band membership)

    print("RPL by value-tier × week (TOTAL, aggregated — noise-clean):\n")
    hdr = "  {:<14}".format("tier") + "".join("{:>7}".format(w[5:]) for w in weeks)
    print(hdr); print("  " + "-" * (len(hdr) - 2))
    for t in tiers:
        line = "  {:<14}".format(t) + "".join(
            ("{:>7.0f}".format(tier_week[t][w]) if tier_week[t][w] is not None else "{:>7}".format("—"))
            for w in weeks)
        rA = sum(wsum(s, base_w)[0] for s in states if tier_of(s) == t)
        cA = sum(wsum(s, base_w)[1] for s in states if tier_of(s) == t)
        rB = sum(wsum(s, rec_w)[0] for s in states if tier_of(s) == t)
        cB = sum(wsum(s, rec_w)[1] for s in states if tier_of(s) == t)
        dd = ((rB / cB) / (rA / cA) - 1) * 100 if (cA and cB) else 0
        print(line + "   ({:+.0f}%)".format(dd))

    print("\nMovers (rev>=${:,} & >={} conv/wk), ranked by RPL change:".format(MAT_REV, MAT_CONVWK))
    mat = [s for s in states if info[s]["rev"] >= MAT_REV and info[s]["convwk"] >= MAT_CONVWK]
    for s in sorted(mat, key=lambda x: info[x]["d"]):
        i = info[s]; tag = "DECAY" if i["d"] <= -0.10 else ("GROW" if i["d"] >= 0.10 else "flat")
        print("  {:<15} {:6.1f} -> {:6.1f} ({:+5.1f}%)  rev ${:>8,.0f}  {:>5}".format(
            s, i["rplA"], i["rplB"], i["d"] * 100, i["rev"], tag))

    with Path(a.out_tiers).open("w", newline="") as f:
        wr = csv.writer(f); wr.writerow(["tier"] + weeks)
        for t in tiers:
            wr.writerow([t] + [round(tier_week[t][w], 2) if tier_week[t][w] is not None else "" for w in weeks])

    with Path(a.out_vol).open("w", newline="") as f:
        wr = csv.writer(f); wr.writerow(["tier"] + weeks)
        for t in tiers:
            wr.writerow([t] + [round(tier_vol[t][w], 1) if tier_vol[t][w] is not None else "" for w in weeks])

    with Path(a.out_clicks).open("w", newline="") as f:
        wr = csv.writer(f); wr.writerow(["tier"] + weeks)
        for t in tiers:
            wr.writerow([t] + [round(tier_clk[t][w], 1) if tier_clk[t][w] is not None else "" for w in weeks])

    try:
        import matplotlib; matplotlib.use("Agg"); import matplotlib.pyplot as plt
        xs = list(range(len(weeks)))
        colors = {"High (>=75)": "#c0504d", "Mid (45-75)": "#9bbb59",
                  "Low (<45)": "#4f81bd", "California": "#f0a030"}
        fig, ax = plt.subplots(figsize=(11, 6))
        for t in tiers:
            ys = [tier_week[t][w] for w in weeks]
            ax.plot(xs, ys, marker="o", lw=2.2, label=t, color=colors.get(t))
        ax.set_xticks(xs); ax.set_xticklabels([w[5:] for w in weeks], rotation=45, fontsize=8)
        ax.set_ylabel("RPL ($, native-conv basis)"); ax.grid(alpha=0.3)
        ax.set_title("Google Home — RPL by value-tier (high tier losing premium, mid tier rising)",
                     fontweight="bold")
        ax.legend(); fig.tight_layout(); fig.savefig(a.out_chart, dpi=130)
        print("\nchart -> {}".format(a.out_chart))
    except Exception as ex:
        print("(chart skipped: {})".format(ex))

if __name__ == "__main__":
    main()
