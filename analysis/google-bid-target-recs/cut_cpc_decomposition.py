#!/usr/bin/env python
r"""
cut_cpc_decomposition.py — Why are the cut candidates unhealthy?
═══════════════════════════════════════════════════════════════════════
Gilbert's hypothesis: the bidder pays up as it matures into its cycle,
so some keywords are unhealthy because their CLICKS GET EXPENSIVE
during expansion phases — not because their conversions are bad.
ROAS = (value per click) / CPC, so this script splits every cut
candidate's post-cutover days into IN-WINDOW (inside the cycle-state
expansion windows) vs OUT-OF-WINDOW and compares CPC, value-per-click,
and ROAS across the phases.

VERDICTS (per keyword x match type x campaign):
  PRICE-DRIVEN       out-of-window ROAS >= 0.95 AND in-window CPC >=
                     --cpc-ratio (1.2x) out-of-window CPC — the
                     keyword is ~fine at calm-phase prices; expansion
                     pricing is the illness. Curve-damping target
                     (caps/negatives at phase), not evidence the term
                     itself is bad.
  CONVERSION-DRIVEN  ROAS < 0.85 in BOTH phases — bad at any price;
                     pause logic applies with confidence.
  MIXED              everything between (price inflates AND value is
                     weak, or borderline phases).
  IN-WINDOW-ONLY     insufficient out-of-window clicks — dormant
                     inventory that only exists in expansions (the
                     flagship-BROAD pattern); phase comparison
                     impossible, itself diagnostic.
  OUT-ONLY           insufficient in-window clicks — the cycle is not
                     this keyword's story.
  INSUFFICIENT       too few clicks in both phases (< --min-clicks).

POOLED HEADLINE: of the analyzed classes' recent $/day, the share in
each verdict — the split that turns the S1 "broom" into two tools and
quantifies the elevated-CPC spiral link at keyword grain.

SCOPE: reads the keyword_health detail CSV (auto-finds newest) for
classes/flags; analyzes classes in --classes (default
UNHEALTHY,BORDERLINE; add HEALTHY for a control read). PROTECTED rows
are analyzed and flagged, never labeled "pause with confidence".

HONESTY NOTES: phase = position in the WAVE (daily grain via
cycle-state expansion windows), not intraday pacing — kpd is daily, so
intraday bid maturation is invisible in-DB. CPC ratios need clicks;
rows under --min-clicks per phase get the reduced verdicts above. The
windows come from the daily-grain cycle-state export; rerun after the
dashboard reruns.

DATA RULES (house): kpd JOIN keyword_instances (account scope through
join), device='ALL', clicks/revenue/conversions columns auto-detected,
mode=ro, never during a sync.

OUTPUT (to --out-dir, default the keyword-health folder):
  cut_cpc_decomposition_<date>.csv    full metrics per row
  cut_cpc_decomposition_<date>.png    verdict $/day by campaign +
                                      CPC-ratio vs calm-ROAS scatter
  cut_cpc_decomposition_<date>.txt    summary + pooled headline

USAGE:
  python cut_cpc_decomposition.py
  python cut_cpc_decomposition.py --classes UNHEALTHY
  python cut_cpc_decomposition.py --min-clicks 30 --cpc-ratio 1.2
"""

import argparse
import csv
import sqlite3
import sys
from datetime import datetime
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
CYCLE_ENTITY_SHORTS = ("TIER1", "TIER2", "TIER3", "TIER4",
                       "CALIFORNIA", "TEXAS", "FLORIDA")
REV_CANDIDATES = ["conversion_value", "conversions_value", "conv_value",
                  "primary_conv_value", "total_conv_value",
                  "all_conv_value"]
CONV_CANDIDATES = ["conversions", "primary_conv_count", "conv_count",
                   "all_conversions"]
CLICK_CANDIDATES = ["clicks", "click_count"]

VERDICT_COLORS = {"PRICE-DRIVEN": "#C55A11",
                  "CONVERSION-DRIVEN": "#C00000",
                  "MIXED": "#BF8F00",
                  "IN-WINDOW-ONLY": "#7030A0",
                  "OUT-ONLY": "#808080",
                  "INSUFFICIENT": "#BFBFBF"}


def connect_ro(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro",
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


def kw_campaign_join(conn):
    if has_col(conn, "keyword_instances", "campaign_id"):
        return "", "ki.campaign_id"
    return ("JOIN ad_groups ag ON ag.id = ki.ad_group_id",
            "ag.campaign_id")


def load_detail(args):
    p = None
    if args.detail_csv:
        p = Path(args.detail_csv)
        if not p.exists():
            sys.exit(f"--detail-csv not found: {p}")
    else:
        d = Path(args.out_dir)
        cands = sorted(d.glob("keyword_health_detail_*.csv"),
                       key=lambda x: x.stat().st_mtime, reverse=True)
        if not cands:
            sys.exit(f"No keyword_health_detail_*.csv in {d} — run "
                     "keyword_health_scenario.py first.")
        p = cands[0]
    rows = {}
    with open(p, newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            key = (r["campaign"], r["match_type"].upper(),
                   r["keyword"].lower())
            rows[key] = {
                "class": r["class"],
                "protected": r["protected"].strip().lower() == "true",
                "phase_conditioned":
                    r["phase_conditioned"].strip().lower() == "true",
                "stab_roas": float(r["stab_roas"])
                if r["stab_roas"] else None,
                "rec_spend_day": float(r["rec_spend_day"] or 0),
                "seg_marginal_class": r.get("seg_marginal_class", "")}
    return rows, p


def load_cycle_windows(path, camp_ids):
    p = Path(path)
    if not p.exists():
        sys.exit(f"cycle-state file not found: {p} — the phase split "
                 "needs it (run the cycle dashboard).")

    def to_short(ent):
        e = (ent or "").strip().upper()
        for s in CYCLE_ENTITY_SHORTS:
            if e.startswith(s):
                return s
        return None

    out = {}
    with open(p, newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            if (r.get("record") or "").strip().upper() != "WINDOW":
                continue
            short = to_short(r.get("entity"))
            cid = camp_ids.get(short) if short else None
            if not cid:
                continue
            try:
                out.setdefault(cid, []).append(
                    (pdate(r["date_start"]), pdate(r["date_end"])))
            except (KeyError, ValueError):
                continue
    if not out:
        sys.exit("No WINDOW rows parsed from cycle-state file.")
    return out


def verdict(m, args):
    ci, co = m["clicks_in"], m["clicks_out"]
    if ci < args.min_clicks and co < args.min_clicks:
        return "INSUFFICIENT"
    if co < args.min_clicks:
        return "IN-WINDOW-ONLY"
    if ci < args.min_clicks:
        return "OUT-ONLY"
    cpc_r = (m["cpc_in"] / m["cpc_out"]) if m["cpc_out"] else None
    if m["roas_out"] is not None and m["roas_out"] >= 0.95 and \
            cpc_r is not None and cpc_r >= args.cpc_ratio:
        return "PRICE-DRIVEN"
    if m["roas_in"] is not None and m["roas_in"] < 0.85 and \
            m["roas_out"] is not None and m["roas_out"] < 0.85:
        return "CONVERSION-DRIVEN"
    return "MIXED"


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--db-path", default=str(DEFAULT_DB_PATH))
    p.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    p.add_argument("--detail-csv", default=None)
    p.add_argument("--cycle-state", default=str(DEFAULT_CYCLE_STATE))
    p.add_argument("--stability-start", default="2026-07-01")
    p.add_argument("--classes", default="UNHEALTHY,BORDERLINE",
                   help="comma list of health classes to analyze")
    p.add_argument("--min-clicks", type=float, default=30.0,
                   help="min clicks per phase for a full verdict")
    p.add_argument("--cpc-ratio", type=float, default=1.2,
                   help="in/out CPC ratio threshold for PRICE-DRIVEN")
    p.add_argument("--map", action="append",
                   help='override ids: --map "TIER2=123"')
    args = p.parse_args()

    camp_ids = dict(CAMPAIGN_IDS)
    for m in args.map or []:
        k, v = m.split("=", 1)
        camp_ids[k.strip().upper()] = int(v.strip())
    short_of = {cid: s for s, cid in camp_ids.items()}
    classes = {c.strip().upper() for c in args.classes.split(",")}

    detail, detail_path = load_detail(args)
    targets = {k: v for k, v in detail.items()
               if v["class"] in classes}
    print(f"Detail: {detail_path.name} — analyzing "
          f"{len(targets):,} rows in classes "
          f"{sorted(classes)}")
    cyc = load_cycle_windows(args.cycle_state, camp_ids)
    nwin = sum(len(v) for v in cyc.values())
    print(f"Cycle windows: {nwin} from "
          f"{Path(args.cycle_state).name}")

    conn = connect_ro(Path(args.db_path))
    rev_col = pick_col(conn, "keyword_performance_daily",
                       REV_CANDIDATES)
    conv_col = pick_col(conn, "keyword_performance_daily",
                        CONV_CANDIDATES)
    click_col = pick_col(conn, "keyword_performance_daily",
                         CLICK_CANDIDATES)
    if not click_col:
        sys.exit("No clicks column on keyword_performance_daily "
                 f"(looked for {CLICK_CANDIDATES}) — CPC needs it.")
    print(f"kpd columns: clicks={click_col}, revenue={rev_col}, "
          f"conversions={conv_col}")

    join_extra, camp_expr = kw_campaign_join(conn)
    ids = list(camp_ids.values())
    win_terms, win_params = [], []
    for cid, wins in cyc.items():
        for ws, we in wins:
            win_terms.append(f"({camp_expr} = ? AND p.report_date "
                             "BETWEEN ? AND ?)")
            win_params += [cid, ws.isoformat(), we.isoformat()]
    win_pred = "(" + " OR ".join(win_terms) + ")"
    q = (f"SELECT {camp_expr} cid, LOWER(TRIM(ki.keyword_text)) kw, "
         f"UPPER(TRIM(ki.match_type)) mt, "
         f"CASE WHEN {win_pred} THEN 1 ELSE 0 END inwin, "
         f"SUM(p.cost_micros)/1e6 s, SUM(p.{click_col}) c, "
         f"SUM(p.{rev_col}) r, SUM(p.{conv_col}) cv "
         f"FROM keyword_performance_daily p "
         f"JOIN keyword_instances ki ON ki.id = p.instance_id "
         f"AND ki.account_id = ? {join_extra} "
         f"WHERE p.device = 'ALL' AND p.report_date >= ? "
         f"AND {camp_expr} IN ({','.join('?' * len(ids))}) "
         f"GROUP BY 1, 2, 3, 4")
    rows = conn.execute(
        q, win_params + [ACCOUNT_ID, args.stability_start] + ids
    ).fetchall()
    data_end = conn.execute(
        """SELECT MAX(p.report_date) FROM keyword_performance_daily p
           JOIN keyword_instances ki ON ki.id = p.instance_id
           WHERE ki.account_id = ?""", [ACCOUNT_ID]).fetchone()[0]
    conn.close()
    stamp = str(data_end)[:10]

    agg = {}
    for r in rows:
        short = short_of.get(int(r["cid"]))
        if not short:
            continue
        key = (short, str(r["mt"] or "?"), r["kw"] or "")
        if key not in targets:
            continue
        a = agg.setdefault(key, {"in": [0.0] * 4, "out": [0.0] * 4})
        side = "in" if r["inwin"] else "out"
        a[side][0] += r["s"] or 0.0
        a[side][1] += r["c"] or 0.0
        a[side][2] += r["r"] or 0.0
        a[side][3] += r["cv"] or 0.0

    out_rows = []
    for key, a in agg.items():
        short, mt, kw = key
        si, ci, ri, cvi = a["in"]
        so, co, ro, cvo = a["out"]
        m = {"clicks_in": ci, "clicks_out": co,
             "cpc_in": (si / ci) if ci > 0 else None,
             "cpc_out": (so / co) if co > 0 else None,
             "vpc_in": (ri / ci) if ci > 0 else None,
             "vpc_out": (ro / co) if co > 0 else None,
             "roas_in": (ri / si) if si > 0 else None,
             "roas_out": (ro / so) if so > 0 else None}
        v = verdict(m, args)
        d = targets[key]
        out_rows.append({
            "campaign": short, "keyword": kw, "match_type": mt,
            "class": d["class"], "protected": d["protected"],
            "phase_conditioned": d["phase_conditioned"],
            "seg_marginal_class": d["seg_marginal_class"],
            "rec_spend_day": d["rec_spend_day"],
            "stab_roas": d["stab_roas"],
            "spend_in": si, "spend_out": so,
            "clicks_in": ci, "clicks_out": co,
            "cpc_in": m["cpc_in"], "cpc_out": m["cpc_out"],
            "cpc_ratio": (m["cpc_in"] / m["cpc_out"])
            if (m["cpc_in"] and m["cpc_out"]) else None,
            "vpc_in": m["vpc_in"], "vpc_out": m["vpc_out"],
            "roas_in": m["roas_in"], "roas_out": m["roas_out"],
            "conv_in": cvi, "conv_out": cvo,
            "verdict": v})
    missing = [k for k in targets if k not in agg]
    if missing:
        print(f"  NOTE: {len(missing)} target rows had no kpd data in "
              "the window (renamed/paused?)")

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / f"cut_cpc_decomposition_{stamp}.csv"
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(out_rows[0].keys()))
        w.writeheader()
        w.writerows(sorted(out_rows,
                           key=lambda x: -x["rec_spend_day"]))

    # pooled headline (recent $/day by verdict), unhealthy separately
    def pool(rows_, label):
        tot = sum(x["rec_spend_day"] for x in rows_)
        lines = [f"{label}: ${tot:,.0f}/d recent spend across "
                 f"{len(rows_)} rows"]
        for v in VERDICT_COLORS:
            sel = [x for x in rows_ if x["verdict"] == v]
            if not sel:
                continue
            sp = sum(x["rec_spend_day"] for x in sel)
            lines.append(
                f"  {v:18s} {len(sel):3d} rows  ${sp:,.0f}/d "
                f"({(sp / tot if tot else 0):.0%})")
        # pooled verdict for the click-starved tail: the rows can't be
        # judged singly, but the MASS can
        ins = [x for x in rows_ if x["verdict"] == "INSUFFICIENT"]
        si = sum(x["spend_in"] for x in ins)
        so = sum(x["spend_out"] for x in ins)
        ci = sum(x["clicks_in"] for x in ins)
        co = sum(x["clicks_out"] for x in ins)
        ri = sum((x["roas_in"] or 0) * x["spend_in"] for x in ins)
        ro = sum((x["roas_out"] or 0) * x["spend_out"] for x in ins)
        if ins and ci > 0 and co > 0 and si > 0 and so > 0:
            lines.append(
                f"  INSUFFICIENT POOLED as one mass: ROAS "
                f"{ri / si:.2f} in / {ro / so:.2f} out, CPC "
                f"${si / ci:.2f} in / ${so / co:.2f} out (ratio "
                f"{(si / ci) / (so / co):.2f}) -> the tail's "
                "aggregate verdict")
        return lines

    L = [f"CUT CPC DECOMPOSITION — data through {stamp}",
         f"phase = cycle-state expansion windows ({nwin}); "
         f"stability window {args.stability_start}->{stamp}; "
         f"PRICE-DRIVEN = calm-phase ROAS >= 0.95 AND in-window CPC "
         f">= {args.cpc_ratio:g}x calm CPC; full verdicts need >= "
         f"{args.min_clicks:g} clicks/phase.",
         "NOTE: phase = position in the WAVE (daily grain); intraday "
         "bid pacing is invisible in-DB.", ""]
    unh = [x for x in out_rows if x["class"] == "UNHEALTHY"]
    bord = [x for x in out_rows if x["class"] == "BORDERLINE"]
    if unh:
        L += pool(unh, "UNHEALTHY (the S1 set)")
        L.append("")
    if bord:
        L += pool(bord, "BORDERLINE (the S2 layer)")
        L.append("")
    top_price = sorted([x for x in out_rows
                        if x["verdict"] == "PRICE-DRIVEN"],
                       key=lambda x: -x["rec_spend_day"])[:12]
    if top_price:
        L.append("Top PRICE-DRIVEN (curve-damping targets, not "
                 "term-quality evidence):")
        for x in top_price:
            L.append(
                f"  {x['campaign']:10s} {x['keyword'][:32]:32s} "
                f"{x['match_type']:7s} calmROAS "
                f"{x['roas_out']:.2f}  CPC ${x['cpc_out']:.2f}->"
                f"${x['cpc_in']:.2f} ({x['cpc_ratio']:.2f}x)  "
                f"${x['rec_spend_day']:.0f}/d"
                + ("  [PROT]" if x["protected"] else ""))
        L.append("")
    top_conv = sorted([x for x in out_rows
                       if x["verdict"] == "CONVERSION-DRIVEN"],
                      key=lambda x: -x["rec_spend_day"])[:12]
    if top_conv:
        L.append("Top CONVERSION-DRIVEN (bad at any price):")
        for x in top_conv:
            L.append(
                f"  {x['campaign']:10s} {x['keyword'][:32]:32s} "
                f"{x['match_type']:7s} ROAS in/out "
                f"{x['roas_in']:.2f}/{x['roas_out']:.2f}  "
                f"${x['rec_spend_day']:.0f}/d"
                + ("  [PROT — never auto-pause]" if x["protected"]
                   else ""))
        L.append("")
    dorm = [x for x in out_rows if x["verdict"] == "IN-WINDOW-ONLY"]
    if dorm:
        L.append(f"IN-WINDOW-ONLY (dormant, expansion-born — "
                 f"{len(dorm)} rows, "
                 f"${sum(x['rec_spend_day'] for x in dorm):,.0f}/d): "
                 + ", ".join(f"{x['keyword'][:24]} "
                             f"[{x['campaign']}/{x['match_type'][:1]}]"
                             for x in sorted(
                                 dorm,
                                 key=lambda x: -x["rec_spend_day"]
                             )[:8]))
    text = "\n".join(L)
    txt_path = out_dir / f"cut_cpc_decomposition_{stamp}.txt"
    txt_path.write_text(text, encoding="utf-8")
    print("\n" + text)

    # chart: verdict $/day by campaign + scatter
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(12.5, 4.4), dpi=150)
    camps = sorted({x["campaign"] for x in out_rows})
    bottoms = {c: 0.0 for c in camps}
    for v, col in VERDICT_COLORS.items():
        vals = []
        for c in camps:
            sp = sum(x["rec_spend_day"] for x in out_rows
                     if x["campaign"] == c and x["verdict"] == v)
            vals.append(sp)
        if not any(vals):
            continue
        ax1.bar(camps, vals, bottom=[bottoms[c] for c in camps],
                color=col, label=v, width=0.62)
        for c, sp in zip(camps, vals):
            bottoms[c] += sp
    ax1.set_ylabel("recent $/day (analyzed classes)", fontsize=8)
    ax1.set_title("Why the cut candidates are unhealthy — "
                  "$/day by verdict", fontsize=9.5)
    ax1.tick_params(labelsize=7.5)
    ax1.legend(fontsize=6.5)
    ax1.grid(axis="y", alpha=0.25)
    pts = [x for x in out_rows if x["cpc_ratio"] is not None
           and x["roas_out"] is not None]
    for v, col in VERDICT_COLORS.items():
        sel = [x for x in pts if x["verdict"] == v]
        if not sel:
            continue
        ax2.scatter([x["roas_out"] for x in sel],
                    [x["cpc_ratio"] for x in sel],
                    s=[max(x["rec_spend_day"] * 6, 12) for x in sel],
                    color=col, alpha=0.75, label=v, edgecolors="none")
    ax2.axvline(0.95, color="#404040", ls=":", lw=1)
    ax2.axhline(args.cpc_ratio, color="#404040", ls=":", lw=1)
    ax2.set_xlabel("calm-phase (out-of-window) ROAS", fontsize=8)
    ax2.set_ylabel("CPC ratio (in-window / calm)", fontsize=8)
    ax2.set_title("Price vs quality — size = recent $/day "
                  "(top-right quadrant = PRICE-DRIVEN zone)",
                  fontsize=9.5)
    ax2.tick_params(labelsize=7.5)
    ax2.grid(alpha=0.25)
    fig.tight_layout()
    png_path = out_dir / f"cut_cpc_decomposition_{stamp}.png"
    fig.savefig(png_path)
    plt.close(fig)
    print(f"\nSaved: {csv_path}")
    print(f"Saved: {png_path}")
    print(f"Saved: {txt_path}")


if __name__ == "__main__":
    main()
