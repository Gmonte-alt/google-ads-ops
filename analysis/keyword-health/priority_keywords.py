#!/usr/bin/env python
r"""
priority_keywords.py — Who earns reliably across the bidding cycles?
═══════════════════════════════════════════════════════════════════════
Judges every keyword on RELIABILITY ACROSS CYCLE PHASES rather than
level economics over calendar windows: which keywords delivered
consistent ROAS and lead volume in the CURRENT cycle AND the PREVIOUS
cycle. Those are the priority set — the swing-safe anchor inventory —
and the fading set are the regain targets, split into aggression vs
rebuild lanes by whether they still qualify for their auctions.

CYCLES: trough-to-trough waves from cycle_state_latest.csv (ACCOUNT
TROUGH rows = cycle authority). Current = latest trough -> data end
(OPEN — reads provisional). Previous = prior trough -> latest trough
minus one day. Override with --cycle-current / --cycle-previous
("YYYY-MM-DD:YYYY-MM-DD").

CONSISTENCY (weekly buckets — daily keyword ROAS is noise at <1
conv/day):
  presence   share of the cycle's weeks with >= 1 conversion
  band       share of active weeks (spend > 0) with weekly ROAS
             >= --band (0.85)
  vol CV     coefficient of variation of weekly conversions
QUALIFY (per cycle): pooled ROAS >= 1.00, presence >= --presence
(0.70), cycle spend >= --min-cycle-spend ($150).

CLASSES:
  PRIORITY        qualifies in BOTH cycles — the anchor set
  FADING          qualified previous cycle; current-cycle spend pace
                  < --fade-pace (30%) of previous or presence < 0.3
                  — the ceiling's victims, the regain targets
  SLIPPING        qualified previous; still active but failing
                  ROAS/consistency now
  EMERGING        qualifies current cycle only (watch for one more
                  cycle before trusting)
  NOISE           qualifies in neither

AGGRESSION vs REBUILD (the eligibility-vs-price split, per PRIORITY/
FADING row): impressions/week trajectory (current vs previous) +
last-14d impression-weighted QS:
  AGGRESSION   impr ratio < --impr-drop (0.70) AND qs14 >= --qs-floor
               (5) — still qualified, losing volume: the safest
               aggression in the account
  REBUILD-LANE impr ratio < --impr-drop AND (qs14 < floor or absent)
               — eligibility lost; aggression just pays more for the
               residue; needs the click-record rebuild
  HOLD         volume intact (ratio >= --impr-drop)

HONESTY (printed on outputs): the current cycle is OPEN — its
consistency reads firm up as it closes; cycle boundaries are ACCOUNT
authority while T1/T4 run ~10 days out of phase (their week buckets
straddle slightly); consistent keywords don't damp the controller's
oscillator — this list is where aggression doesn't FEED the whipsaw,
and the anchor set for structural protection. No pause verdicts here;
the census owns those.

DATA (house): kpd JOIN keyword_instances, device='ALL'; grain keyword
x MATCH TYPE x campaign; Leads = Conversions column; QS never summed;
mode=ro, never during a sync.

OUTPUT: priority_keywords_<date>.csv / .txt / .png

USAGE:
  python priority_keywords.py
  python priority_keywords.py --cycle-current 2026-08-02:2026-08-23 \
      --cycle-previous 2026-06-18:2026-08-01
"""

import argparse
import csv
import sqlite3
import statistics
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
    "CALIFORNIA": 23385188622, "TEXAS": 23394808297,
    "FLORIDA": 23394808300, "TIER4": 23428389081,
    "TIER1": 23496334752, "TIER2": 23530860942,
    "TIER3": 23586637678,
}
CARRIER_PATTERNS = [
    ("Geico", ["geico"]), ("Farmers", ["farmers"]),
    ("State Farm", ["state farm", "statefarm"]),
    ("Travelers", ["travelers"]),
    ("Kin", ["kin insurance", " kin ", "kin home"]),
    ("AAA", ["aaa"]), ("Amica", ["amica"]),
    ("Hartford", ["hartford"]), ("Costco", ["costco"]),
]
HOME_TOKENS = ("home", "homeowner", "homeowners", "house")
REV_CANDIDATES = ["conversion_value", "conversions_value", "conv_value",
                  "primary_conv_value", "total_conv_value",
                  "all_conv_value"]
CONV_CANDIDATES = ["conversions", "primary_conv_count", "conv_count",
                   "all_conversions"]
CLASS_COLORS = {"PRIORITY": "#2E7D32", "FADING": "#C55A11",
                "SLIPPING": "#BF8F00", "EMERGING": "#44709D",
                "NOISE": "#BFBFBF"}


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


def pdate(s):
    return datetime.strptime(str(s)[:10], "%Y-%m-%d").date()


def week_start(d):
    return d - timedelta(days=d.weekday())


def classify(kw):
    k = f" {kw.lower().strip()} "
    for label, pats in CARRIER_PATTERNS:
        for p in pats:
            if p in k:
                return label
    return "Other/generic"


def is_protected(kw):
    return classify(kw) == "Farmers" and \
        any(t in kw for t in HOME_TOKENS)


def cycles_from_state(path, data_end):
    p = Path(path)
    if not p.exists():
        return None
    troughs = []
    with open(p, newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            if (r.get("entity") or "").strip().upper()\
                    .startswith("ACCOUNT") and \
                    (r.get("record") or "").strip().upper() == \
                    "TROUGH":
                try:
                    troughs.append(pdate(r["date_start"]))
                except (KeyError, ValueError):
                    continue
    troughs = sorted(set(troughs))
    if len(troughs) < 2:
        return None
    cur_start, prev_start = troughs[-1], troughs[-2]
    return {"previous": (prev_start,
                         cur_start - timedelta(days=1)),
            "current": (cur_start, data_end)}


def parse_span(s):
    a, b = s.split(":")
    return (pdate(a), pdate(b))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db-path", default=str(DEFAULT_DB_PATH))
    ap.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    ap.add_argument("--cycle-state", default=str(DEFAULT_CYCLE_STATE))
    ap.add_argument("--cycle-current", default=None,
                    help='"YYYY-MM-DD:YYYY-MM-DD" override')
    ap.add_argument("--cycle-previous", default=None)
    ap.add_argument("--min-cycle-spend", type=float, default=150.0)
    ap.add_argument("--presence", type=float, default=0.70)
    ap.add_argument("--band", type=float, default=0.85)
    ap.add_argument("--fade-pace", type=float, default=0.30)
    ap.add_argument("--impr-drop", type=float, default=0.70)
    ap.add_argument("--qs-floor", type=float, default=5.0)
    ap.add_argument("--qs-days", type=int, default=14)
    ap.add_argument("--top-chart", type=int, default=10)
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
    has_kqd = pick_col(conn, "keyword_quality_daily",
                       ["historical_quality_score"]) is not None
    if not (rev_col and conv_col and impr_col):
        sys.exit("kpd needs revenue/conversions/impressions.")
    row = conn.execute(
        """SELECT MAX(p.report_date)
           FROM keyword_performance_daily p
           JOIN keyword_instances ki ON ki.id = p.instance_id
           WHERE ki.account_id = ?""", [ACCOUNT_ID]).fetchone()
    data_end = pdate(row[0])

    if args.cycle_current and args.cycle_previous:
        cyc = {"current": parse_span(args.cycle_current),
               "previous": parse_span(args.cycle_previous)}
        src = "CLI override"
    else:
        cyc = cycles_from_state(args.cycle_state, data_end)
        src = Path(args.cycle_state).name
        if not cyc:
            sys.exit("Need >= 2 ACCOUNT troughs in the cycle-state "
                     "file, or --cycle-current/--cycle-previous.")
    cur, prev = cyc["current"], cyc["previous"]
    cur_open = cur[1] >= data_end
    print(f"Cycles ({src}): PREVIOUS {prev[0]} -> {prev[1]} "
          f"({(prev[1] - prev[0]).days + 1}d) · CURRENT {cur[0]} -> "
          f"{cur[1]} ({(cur[1] - cur[0]).days + 1}d"
          + (", OPEN — provisional)" if cur_open else ")"))

    q = (f"SELECT ki.campaign_id cid, "
         f"LOWER(TRIM(ki.keyword_text)) kw, "
         f"UPPER(TRIM(ki.match_type)) mt, p.report_date d, "
         f"SUM(p.{impr_col}) im, SUM(p.cost_micros)/1e6 s, "
         f"SUM(p.{rev_col}) r, SUM(p.{conv_col}) cv "
         f"FROM keyword_performance_daily p "
         f"JOIN keyword_instances ki ON ki.id = p.instance_id "
         f"AND ki.account_id = ? "
         f"WHERE p.device = 'ALL' AND p.report_date BETWEEN ? AND ? "
         f"AND ki.campaign_id IN ({','.join('?' * len(ids))}) "
         f"GROUP BY 1, 2, 3, 4")
    rows = conn.execute(
        q, [ACCOUNT_ID, prev[0].isoformat(), cur[1].isoformat()]
        + ids).fetchall()

    qs14, qs_latest = {}, {}
    if has_kqd:
        qstart = (data_end - timedelta(days=args.qs_days - 1))\
            .isoformat()
        qq = (f"SELECT ki.campaign_id cid, "
              f"LOWER(TRIM(ki.keyword_text)) kw, "
              f"UPPER(TRIM(ki.match_type)) mt, "
              f"SUM(CASE WHEN q.historical_quality_score IS NOT NULL "
              f"THEN p.{impr_col} ELSE 0 END) ri, "
              f"SUM(CASE WHEN q.historical_quality_score IS NOT NULL "
              f"THEN q.historical_quality_score * p.{impr_col} "
              f"ELSE 0 END) qi "
              f"FROM keyword_performance_daily p "
              f"JOIN keyword_instances ki ON ki.id = p.instance_id "
              f"AND ki.account_id = ? "
              f"LEFT JOIN keyword_quality_daily q "
              f"ON q.instance_id = p.instance_id "
              f"AND q.report_date = p.report_date "
              f"WHERE p.device = 'ALL' AND p.report_date >= ? "
              f"AND ki.campaign_id IN ({','.join('?' * len(ids))}) "
              f"GROUP BY 1, 2, 3")
        for r in conn.execute(qq, [ACCOUNT_ID, qstart] + ids):
            short = short_of.get(int(r["cid"]))
            if short and (r["ri"] or 0) > 0:
                qs14[(short, r["mt"], r["kw"])] = r["qi"] / r["ri"]
        # fallback for dark keywords (zero impressions in the last
        # 14d -> no weighted QS by construction): latest snapshot on
        # record. "Still qualified" should read the last KNOWN QS,
        # not require current impressions.
        ql = (f"SELECT ki.campaign_id cid, "
              f"LOWER(TRIM(ki.keyword_text)) kw, "
              f"UPPER(TRIM(ki.match_type)) mt, "
              f"q.historical_quality_score qs FROM ("
              f"SELECT instance_id, historical_quality_score, "
              f"ROW_NUMBER() OVER (PARTITION BY instance_id "
              f"ORDER BY report_date DESC) rn "
              f"FROM keyword_quality_daily "
              f"WHERE report_date <= ?) q "
              f"JOIN keyword_instances ki ON ki.id = q.instance_id "
              f"WHERE q.rn = 1 AND ki.account_id = ? "
              f"AND ki.campaign_id IN "
              f"({','.join('?' * len(ids))})")
        acc = {}
        for r in conn.execute(
                ql, [data_end.isoformat(), ACCOUNT_ID] + ids):
            short = short_of.get(int(r["cid"]))
            if short and r["qs"] is not None:
                acc.setdefault((short, r["mt"], r["kw"]),
                               []).append(float(r["qs"]))
        qs_latest = {k: sum(v) / len(v) for k, v in acc.items()}
    conn.close()

    # weekly bucketing per keyword per cycle
    def cycle_weeks(span):
        w0, w1 = week_start(span[0]), week_start(span[1])
        out, w = [], w0
        while w <= w1:
            out.append(w)
            w += timedelta(days=7)
        return out
    weeks_of = {"previous": cycle_weeks(prev),
                "current": cycle_weeks(cur)}

    data = {}
    for r in rows:
        short = short_of.get(int(r["cid"]))
        if not short:
            continue
        d = pdate(r["d"])
        cycle = "current" if cur[0] <= d <= cur[1] else \
            ("previous" if prev[0] <= d <= prev[1] else None)
        if not cycle:
            continue
        key = (short, str(r["mt"] or "?"), r["kw"] or "")
        k = data.setdefault(key, {c: {} for c in
                                  ("previous", "current")})
        w = week_start(d)
        a = k[cycle].setdefault(w, [0.0, 0.0, 0.0, 0.0])
        a[0] += r["s"] or 0
        a[1] += r["r"] or 0
        a[2] += r["cv"] or 0
        a[3] += r["im"] or 0

    def cycle_metrics(wkmap, span, weeks):
        days = (span[1] - span[0]).days + 1
        s = sum(v[0] for v in wkmap.values())
        rv = sum(v[1] for v in wkmap.values())
        cv = sum(v[2] for v in wkmap.values())
        im = sum(v[3] for v in wkmap.values())
        nw = len(weeks)
        conv_w = [wkmap.get(w, [0, 0, 0, 0])[2] for w in weeks]
        active = [w for w in weeks
                  if wkmap.get(w, [0, 0, 0, 0])[0] > 0]
        band_ok = sum(1 for w in active
                      if wkmap[w][1] / wkmap[w][0] >= args.band)
        mean_c = sum(conv_w) / nw if nw else 0
        return {
            "spend": s, "rev": rv, "conv": cv, "impr": im,
            "roas": (rv / s) if s > 0 else None,
            "spend_pace": s / days, "impr_wk": im / nw if nw else 0,
            "presence": (sum(1 for c in conv_w if c > 0) / nw)
            if nw else 0,
            "band": (band_ok / len(active)) if active else None,
            "cv_conv": (statistics.pstdev(conv_w) / mean_c)
            if mean_c > 0 and nw > 1 else None,
        }

    def qualifies(m):
        return (m["roas"] is not None and m["roas"] >= 1.0
                and m["presence"] >= args.presence
                and m["spend"] >= args.min_cycle_spend)

    out = []
    for key, k in data.items():
        short, mt, kw = key
        P = cycle_metrics(k["previous"], prev, weeks_of["previous"])
        C = cycle_metrics(k["current"], cur, weeks_of["current"])
        qp, qc = qualifies(P), qualifies(C)
        pace_ratio = (C["spend_pace"] / P["spend_pace"]) \
            if P["spend_pace"] > 0 else None
        if qp and qc:
            cls = "PRIORITY"
        elif qp and ((pace_ratio is not None
                      and pace_ratio < args.fade_pace)
                     or C["presence"] < 0.3):
            cls = "FADING"
        elif qp:
            cls = "SLIPPING"
        elif qc:
            cls = "EMERGING"
        else:
            cls = "NOISE"
        impr_ratio = (C["impr_wk"] / P["impr_wk"]) \
            if P["impr_wk"] > 0 else None
        q14, q_src = qs14.get(key), "iw14"
        if q14 is None:
            q14 = qs_latest.get(key)
            q_src = "latest" if q14 is not None else ""
        lane = ""
        if cls in ("PRIORITY", "FADING"):
            if impr_ratio is not None and impr_ratio < args.impr_drop:
                lane = "AGGRESSION" if (q14 is not None
                                        and q14 >= args.qs_floor) \
                    else "REBUILD-LANE"
            else:
                lane = "HOLD"
        out.append({
            "campaign": short, "keyword": kw, "match_type": mt,
            "segment": classify(kw), "protected": is_protected(kw),
            "class": cls, "lane": lane,
            "qs14": q14, "qs_source": q_src,
            "impr_wk_prev": P["impr_wk"], "impr_wk_cur": C["impr_wk"],
            "impr_ratio": impr_ratio,
            "prev_spend": P["spend"], "prev_roas": P["roas"],
            "prev_presence": P["presence"], "prev_band": P["band"],
            "prev_cv_conv": P["cv_conv"], "prev_conv": P["conv"],
            "cur_spend": C["spend"], "cur_roas": C["roas"],
            "cur_presence": C["presence"], "cur_band": C["band"],
            "cur_cv_conv": C["cv_conv"], "cur_conv": C["conv"],
            "_wk_prev": k["previous"], "_wk_cur": k["current"],
        })

    stamp = data_end.isoformat()
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / f"priority_keywords_{stamp}.csv"
    fields = [f for f in out[0].keys() if not f.startswith("_")]
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(sorted(out, key=lambda x: -(x["prev_spend"]
                                                + x["cur_spend"])))

    # summary
    L = [f"PRIORITY KEYWORDS — cycles: PREVIOUS {prev[0]}->{prev[1]} "
         f"· CURRENT {cur[0]}->{cur[1]}"
         + (" (OPEN — provisional)" if cur_open else ""),
         f"Qualify per cycle: pooled ROAS >= 1.00, presence >= "
         f"{args.presence:.0%} of weeks, spend >= "
         f"${args.min_cycle_spend:,.0f}. AGGRESSION = still "
         f"qualified (qs14 >= {args.qs_floor:g}) but impressions/wk "
         f"< {args.impr_drop:.0%} of previous cycle. Account-trough "
         "authority; T1/T4 run ~10d out of phase (buckets straddle "
         "slightly). This list anchors aggression — it does not damp "
         "the oscillator.", ""]
    for cls in ("PRIORITY", "FADING", "SLIPPING", "EMERGING"):
        sel = sorted([x for x in out if x["class"] == cls],
                     key=lambda x: -(x["prev_conv"] + x["cur_conv"]))
        tot_c = sum(x["cur_conv"] + x["prev_conv"] for x in sel)
        L.append(f"{cls} — {len(sel)} keywords "
                 f"({tot_c:,.0f} conv across both cycles):")
        for x in sel[:12]:
            pr = f"{x['prev_roas']:.2f}" if x["prev_roas"] else "—"
            cr = f"{x['cur_roas']:.2f}" if x["cur_roas"] else "—"
            ir = f"{x['impr_ratio']:.2f}" if x["impr_ratio"] \
                is not None else "—"
            q = (f"{x['qs14']:.1f}"
                 + ("(last)" if x["qs_source"] == "latest" else "")) \
                if x["qs14"] is not None else "—"
            L.append(
                f"  {x['campaign']:11s} {x['keyword'][:32]:32s} "
                f"{x['match_type']:7s} prev {pr} "
                f"(pres {x['prev_presence']:.0%}) | cur {cr} "
                f"(pres {x['cur_presence']:.0%}) | impr x{ir} "
                f"qs {q}"
                + (f"  -> {x['lane']}" if x["lane"] else "")
                + ("  [PROT]" if x["protected"] else ""))
        L.append("")
    agg = [x for x in out if x["lane"] == "AGGRESSION"]
    reb = [x for x in out if x["lane"] == "REBUILD-LANE"]
    L.append(f"AGGRESSION LIST ({len(agg)}): "
             + ", ".join(f"{x['keyword'][:24]} "
                         f"[{x['campaign']}/{x['match_type'][:1]}]"
                         for x in sorted(
                             agg, key=lambda x: -x["prev_conv"])[:15]))
    L.append(f"REBUILD-LANE ({len(reb)}): "
             + ", ".join(f"{x['keyword'][:24]} "
                         f"[{x['campaign']}]"
                         for x in sorted(
                             reb, key=lambda x: -x["prev_conv"])[:15]))
    text = "\n".join(L)
    txt_path = out_dir / f"priority_keywords_{stamp}.txt"
    txt_path.write_text(text, encoding="utf-8")
    print("\n" + text)

    # chart: weekly conv lines for top priority + class mix
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(13, 4.8), dpi=150)
    prio = sorted([x for x in out if x["class"] == "PRIORITY"],
                  key=lambda x: -(x["prev_conv"] + x["cur_conv"]))
    all_weeks = weeks_of["previous"] + weeks_of["current"]
    cmap = plt.get_cmap("tab10")
    for i, x in enumerate(prio[:args.top_chart]):
        ys = [x["_wk_prev"].get(w, [0, 0, 0, 0])[2]
              + x["_wk_cur"].get(w, [0, 0, 0, 0])[2]
              for w in all_weeks]
        ax1.plot(all_weeks, ys, "-o", ms=3, lw=1.5,
                 color=cmap(i % 10),
                 label=f"{x['keyword'][:20]} [{x['campaign'][:4]}]")
    ax1.axvline(cur[0], color="#404040", ls="-.", lw=1.2)
    ax1.set_title(f"Top {min(args.top_chart, len(prio))} PRIORITY — "
                  "weekly conversions across both cycles "
                  "(vline = cycle boundary)", fontsize=9)
    ax1.legend(fontsize=5.5, ncol=2)
    ax1.tick_params(labelsize=7, rotation=25)
    ax1.grid(alpha=0.25)
    shorts = sorted(camp_ids)
    bot = {c: 0 for c in shorts}
    for cls, col in CLASS_COLORS.items():
        ns = [sum(1 for x in out if x["campaign"] == c
                  and x["class"] == cls) for c in shorts]
        if any(ns):
            ax2.bar(shorts, ns, bottom=[bot[c] for c in shorts],
                    color=col, label=cls, width=0.62)
            for c, n in zip(shorts, ns):
                bot[c] += n
    ax2.set_title("Class mix per campaign", fontsize=9)
    ax2.legend(fontsize=6.5)
    ax2.tick_params(labelsize=7.5)
    ax2.grid(axis="y", alpha=0.25)
    fig.suptitle(
        f"Priority keywords — consistency across cycles "
        f"({prev[0]} -> {cur[1]}"
        + (", current OPEN)" if cur_open else ")"), fontsize=10)
    fig.tight_layout(rect=(0, 0, 1, 0.92))
    png_path = out_dir / f"priority_keywords_{stamp}.png"
    fig.savefig(png_path)
    plt.close(fig)
    print(f"\nSaved: {csv_path}")
    print(f"Saved: {txt_path}")
    print(f"Saved: {png_path}")


if __name__ == "__main__":
    main()
