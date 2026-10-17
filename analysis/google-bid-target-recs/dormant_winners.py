#!/usr/bin/env python
r"""
dormant_winners.py — Which 2025 earners has the flagship concentration
drowned, and what would resurrection candidates look like?
═══════════════════════════════════════════════════════════════════════
Gilbert's hypothesis: historically good keywords are being drowned out
by high-impression flagship terms under campaign tROAS. This script
tests it by joining a FULL-2025 baseline against the current book —
by KEYWORD TEXT across the Dec-2025 restructure (2025 lived in
different campaigns/instances, so text is the only stable key; the
QS-cohort precedent).

MEASUREMENT ONLY. Output is a ranked ledger, not a buy list; under
campaign tROAS a re-enabled keyword cannot be force-fed — allocation
changes need negatives / ad-group structure / ad-group targets.

PER TEXT (x match type), baseline era (default 2025-01-01..2025-12-21,
pre-restructure): spend, revenue, conv, ROAS, GP/day. WINNER = GP > 0
and ROAS >= 1.0 in its own era, above --min-baseline-spend ($200).

CURRENT STATUS per text:
  MISSING     no instance in the seven charter campaigns
  PAUSED      instances exist, none enabled (needs a status column;
              'unknown' if absent)
  STARVED     enabled, recent spend/day < --starved-frac (20%) of its
              2025 run-rate
  REDUCED     20-70% of 2025 run-rate
  ALIVE       >= 70%
Joined context from the newest keyword_health_detail CSV when the text
exists there: health class, QS bucket, protected flag, segment
marginal class.

FLAGS: HOME-INTENT (home/homeowner(s)/house token), PRODUCT-ADJACENT
(landlord/renters/condo/flood/auto/umbrella/business token — verify
vertical fit before any resurrection), carrier segment.

HEADLINE: the DROWNED POOL = sum of 2025 GP/day now sitting in
MISSING + PAUSED + STARVED winners, split by segment and flags.

ERA HONESTY (printed on every output): 2025 revenue is the
EverQuote-era basis under a different account structure with ad-group
tROAS — cross-era ROAS is not apples-to-apples, RPLs differ, demand
and the competitive set have moved. "2025 GP/day" is what these terms
earned THEN, a sizing of what was lost — NOT a forecast of what
resurrection earns now.

DATA RULES (house): kpd JOIN keyword_instances ki (account scope via
join), device='ALL'; baseline spans ALL account-1 instances (old
structure included); current presence = instances in the charter
campaign ids; mode=ro; never during a sync.

OUTPUT (to --out-dir): dormant_winners_<date>.csv / .txt / .png

USAGE:
  python dormant_winners.py
  python dormant_winners.py --baseline-start 2025-01-01 --baseline-end 2025-12-21
  python dormant_winners.py --min-baseline-spend 200 --starved-frac 0.2
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

CAMPAIGN_IDS = {
    "CALIFORNIA": 23385188622,
    "TEXAS":      23394808297,
    "FLORIDA":    23394808300,
    "TIER4":      23428389081,
    "TIER1":      23496334752,
    "TIER2":      23530860942,
    "TIER3":      23586637678,
}
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
HOME_TOKENS = ("home", "homeowner", "homeowners", "house")
ADJACENT_TOKENS = ("landlord", "renter", "renters", "condo", "flood",
                   "auto", "car ", " car", "umbrella", "business",
                   "commercial", "life ")
# brands no longer biddable for us (Gilbert, 8/25) — permanently
# gone, excluded from the recoverable pool, reported separately
NON_BIDDABLE_DEFAULT = "usaa,liberty mutual,lemon,safeco"
REV_CANDIDATES = ["conversion_value", "conversions_value", "conv_value",
                  "primary_conv_value", "total_conv_value",
                  "all_conv_value"]
CONV_CANDIDATES = ["conversions", "primary_conv_count", "conv_count",
                   "all_conversions"]
STATUS_CANDIDATES = ["status", "keyword_status", "kw_status", "state"]

STATUS_COLORS = {"MISSING": "#C00000", "PAUSED": "#C55A11",
                 "STARVED": "#BF8F00", "REDUCED": "#7F7F00",
                 "ALIVE": "#2E7D32"}


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


def classify(kw):
    k = f" {kw.lower().strip()} "
    for label, pats in CARRIER_PATTERNS:
        for p in pats:
            if p in k:
                return label
    return OTHER_LABEL


def is_home(kw):
    return any(t in kw for t in HOME_TOKENS)


def is_adjacent(kw):
    k = f" {kw} "
    return any(t in k for t in ADJACENT_TOKENS)


def kw_campaign_join(conn):
    if has_col(conn, "keyword_instances", "campaign_id"):
        return "", "ki.campaign_id"
    return ("JOIN ad_groups ag ON ag.id = ki.ad_group_id",
            "ag.campaign_id")


def load_detail(out_dir, path=None):
    p = Path(path) if path else None
    if not p:
        cands = sorted(Path(out_dir).glob("keyword_health_detail_*.csv"),
                       key=lambda x: x.stat().st_mtime, reverse=True)
        p = cands[0] if cands else None
    if not p or not p.exists():
        return {}, None
    out = {}
    with open(p, newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            key = (r["match_type"].upper(), r["keyword"].lower())
            d = out.setdefault(key, {"cls": set(), "qs": set(),
                                     "prot": False, "seg_m": set()})
            d["cls"].add(r["class"])
            if r.get("qs_bucket"):
                d["qs"].add(r["qs_bucket"])
            if r["protected"].strip().lower() == "true":
                d["prot"] = True
            if r.get("seg_marginal_class"):
                d["seg_m"].add(r["seg_marginal_class"])
    return out, p


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db-path", default=str(DEFAULT_DB_PATH))
    ap.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    ap.add_argument("--baseline-start", default="2025-01-01")
    ap.add_argument("--baseline-end", default="2025-12-21",
                    help="pre-restructure by default")
    ap.add_argument("--recent-days", type=int, default=28)
    ap.add_argument("--min-baseline-spend", type=float, default=200.0)
    ap.add_argument("--starved-frac", type=float, default=0.20)
    ap.add_argument("--alive-frac", type=float, default=0.70)
    ap.add_argument("--detail-csv", default=None)
    ap.add_argument("--exclude-brands",
                    default=NON_BIDDABLE_DEFAULT,
                    help="comma list of tokens for brands no longer "
                         "biddable — their 2025 winners are reported "
                         "as NON-BIDDABLE and excluded from the "
                         "recoverable/drowned pool. 'lemon' also "
                         "catches 'lemonade'. Pass '' to disable.")
    ap.add_argument("--exclude-adgroup-pattern", default="renter",
                    help="exclude baseline-era ad groups whose name "
                         "matches (LIKE, case-insensitive) — the 2025 "
                         "structure carried vertical in ad-group "
                         "names, and renters inventory exited BY "
                         "DESIGN at the restructure (not drowned). "
                         "Pass '' to disable.")
    ap.add_argument("--map", action="append")
    args = ap.parse_args()

    camp_ids = dict(CAMPAIGN_IDS)
    for m in args.map or []:
        k, v = m.split("=", 1)
        camp_ids[k.strip().upper()] = int(v.strip())
    ids = list(camp_ids.values())

    conn = connect_ro(args.db_path)
    rev_col = pick_col(conn, "keyword_performance_daily",
                       REV_CANDIDATES)
    conv_col = pick_col(conn, "keyword_performance_daily",
                        CONV_CANDIDATES)
    if not rev_col:
        sys.exit("No revenue column on kpd.")
    join_extra, camp_expr = kw_campaign_join(conn)
    status_col = pick_col(conn, "keyword_instances", STATUS_CANDIDATES)
    print(f"kpd: revenue={rev_col}, conversions={conv_col}; "
          "ki status column: "
          + (status_col or "NONE (paused vs enabled unknowable)"))

    row = conn.execute(
        """SELECT MAX(p.report_date) FROM keyword_performance_daily p
           JOIN keyword_instances ki ON ki.id = p.instance_id
           WHERE ki.account_id = ?""", [ACCOUNT_ID]).fetchone()
    data_end = str(row[0])[:10]
    recent_start = (datetime.strptime(data_end, "%Y-%m-%d")
                    - timedelta(days=args.recent_days - 1)
                    ).strftime("%Y-%m-%d")
    b0, b1 = args.baseline_start, args.baseline_end
    bdays = (datetime.strptime(b1, "%Y-%m-%d")
             - datetime.strptime(b0, "%Y-%m-%d")).days + 1
    print(f"Baseline {b0}->{b1} ({bdays}d, ALL account-{ACCOUNT_ID} "
          f"instances); current = recent {args.recent_days}d ending "
          f"{data_end}, charter campaigns only")

    # baseline era: text x match grain across the WHOLE account,
    # minus deliberately-exited verticals (ad-group-name filter)
    ag_name_col = pick_col(conn, "ad_groups",
                           ["ad_group_name", "name", "adgroup_name"])
    ag_key_col = pick_col(conn, "ad_groups", ["id", "ad_group_id"])
    excl_join, excl_where, excl_params = "", "", []
    pat = (args.exclude_adgroup_pattern or "").strip()
    if pat and ag_name_col and ag_key_col and \
            has_col(conn, "keyword_instances", "ad_group_id"):
        excl_join = (f"LEFT JOIN ad_groups agx "
                     f"ON agx.{ag_key_col} = ki.ad_group_id ")
        excl_where = (f"AND (agx.{ag_name_col} IS NULL OR "
                      f"LOWER(agx.{ag_name_col}) NOT LIKE ?) ")
        excl_params = [f"%{pat.lower()}%"]
        print(f"  baseline excludes ad groups matching '%{pat}%' "
              "(deliberately-exited vertical, not drowned)")
    elif pat:
        print("  NOTE: ad-group-name exclusion unavailable "
              "(no usable name/key column on ad_groups) — renters-era "
              "inventory may inflate the drowned pool; text-token "
              "PRODUCT-ADJACENT flag is the only guard")
    qb = (f"SELECT LOWER(TRIM(ki.keyword_text)) kw, "
          f"UPPER(TRIM(ki.match_type)) mt, "
          f"SUM(p.cost_micros)/1e6 s, SUM(p.{rev_col}) r"
          + (f", SUM(p.{conv_col}) cv" if conv_col else ", 0 cv")
          + f" FROM keyword_performance_daily p "
          f"JOIN keyword_instances ki ON ki.id = p.instance_id "
          f"AND ki.account_id = ? {join_extra} {excl_join}"
          f"WHERE p.device = 'ALL' AND p.report_date BETWEEN ? AND ? "
          f"{excl_where}GROUP BY 1, 2")
    base = {}
    for r in conn.execute(qb, [ACCOUNT_ID, b0, b1] + excl_params):
        base[(r["mt"] or "?", r["kw"] or "")] = {
            "s": r["s"] or 0.0, "r": r["r"] or 0.0,
            "cv": r["cv"] or 0.0}
    print(f"  baseline texts: {len(base):,}")

    # current presence + recent spend in charter campaigns
    qc = (f"SELECT LOWER(TRIM(ki.keyword_text)) kw, "
          f"UPPER(TRIM(ki.match_type)) mt, "
          f"COUNT(DISTINCT ki.id) n_inst, "
          + (f"SUM(CASE WHEN UPPER(COALESCE(ki.{status_col},'')) "
             f"LIKE '%ENAB%' THEN 1 ELSE 0 END) n_enab, "
             if status_col else "NULL n_enab, ")
          + f"SUM(CASE WHEN p.report_date >= ? AND p.device = 'ALL' "
          f"THEN p.cost_micros ELSE 0 END)/1e6 s_rec, "
          f"SUM(CASE WHEN p.report_date >= ? AND p.device = 'ALL' "
          f"THEN p.{rev_col} ELSE 0 END) r_rec "
          f"FROM keyword_instances ki "
          f"LEFT JOIN keyword_performance_daily p "
          f"ON p.instance_id = ki.id {join_extra.replace('JOIN', 'LEFT JOIN') if join_extra else ''} "
          f"WHERE ki.account_id = ? AND {camp_expr} IN "
          f"({','.join('?' * len(ids))}) GROUP BY 1, 2")
    cur = {}
    for r in conn.execute(qc, [recent_start, recent_start,
                               ACCOUNT_ID] + ids):
        cur[(r["mt"] or "?", r["kw"] or "")] = {
            "n_inst": r["n_inst"] or 0,
            "n_enab": r["n_enab"],
            "s_rec": r["s_rec"] or 0.0, "r_rec": r["r_rec"] or 0.0}
    conn.close()
    print(f"  current charter texts: {len(cur):,}")

    detail, detail_path = load_detail(args.out_dir, args.detail_csv)
    if detail_path:
        print(f"  health context joined from {detail_path.name}")

    nb_tokens = [t.strip().lower() for t in
                 (args.exclude_brands or "").split(",") if t.strip()]

    def non_biddable(kw):
        k = f" {kw} "
        return any(t in k for t in nb_tokens)

    nd = float(args.recent_days)
    out_rows = []
    for (mt, kw), b in base.items():
        if b["s"] < args.min_baseline_spend:
            continue
        gp25 = b["r"] - b["s"]
        roas25 = (b["r"] / b["s"]) if b["s"] > 0 else None
        winner = gp25 > 0 and (roas25 or 0) >= 1.0
        c = cur.get((mt, kw))
        base_spd = b["s"] / bdays
        if not c or c["n_inst"] == 0:
            status = "MISSING"
            rec_spd = 0.0
            ratio = 0.0
        else:
            rec_spd = c["s_rec"] / nd
            ratio = (rec_spd / base_spd) if base_spd > 0 else None
            if c["n_enab"] is not None and c["n_enab"] == 0:
                status = "PAUSED"
            elif ratio is not None and ratio < args.starved_frac:
                status = "STARVED"
            elif ratio is not None and ratio < args.alive_frac:
                status = "REDUCED"
            else:
                status = "ALIVE"
        if non_biddable(kw):
            status = "NON-BIDDABLE"
        d = detail.get((mt, kw), {})
        out_rows.append({
            "keyword": kw, "match_type": mt,
            "biddable": not non_biddable(kw),
            "segment": classify(kw),
            "home_intent": is_home(kw),
            "product_adjacent": is_adjacent(kw),
            "winner_2025": winner,
            "spend_2025": b["s"], "rev_2025": b["r"],
            "roas_2025": roas25, "gp_2025": gp25,
            "gp_2025_day": gp25 / bdays,
            "spend_2025_day": base_spd,
            "conv_2025": b["cv"],
            "status_now": status,
            "rec_spend_day": rec_spd,
            "spend_ratio": ratio,
            "health_class_now": "/".join(sorted(d.get("cls", []))) or "",
            "qs_bucket_now": "/".join(sorted(d.get("qs", []))) or "",
            "protected": d.get("prot", False),
            "seg_marginal_class": "/".join(sorted(d.get("seg_m", [])))
            or ""})

    if not out_rows:
        sys.exit("No baseline texts above the spend floor — check "
                 "--baseline-start/--baseline-end against kpd history "
                 "coverage.")
    stamp = data_end
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / f"dormant_winners_{stamp}.csv"
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(out_rows[0].keys()))
        w.writeheader()
        w.writerows(sorted(out_rows, key=lambda x: -x["gp_2025"]))

    winners = [x for x in out_rows if x["winner_2025"]]
    nb = [x for x in winners if not x["biddable"]]
    winners = [x for x in winners if x["biddable"]]
    drowned = [x for x in winners
               if x["status_now"] in ("MISSING", "PAUSED", "STARVED")]
    L = [f"DORMANT WINNERS — 2025 baseline vs current — data through "
         f"{stamp}",
         f"Baseline {b0}->{b1}; winner = 2025 GP>0 & ROAS>=1.0 & "
         f"spend >= ${args.min_baseline_spend:,.0f}; starved < "
         f"{args.starved_frac:.0%} of 2025 run-rate.",
         "ERA CAVEAT: 2025 = EverQuote-era revenue, pre-restructure "
         "structure, ad-group tROAS. 2025 GP sizes what was LOST, it "
         "does not forecast what resurrection earns now.",
         "STRUCTURAL CAVEAT: under campaign tROAS a re-enabled "
         "keyword cannot be force-fed; allocation changes need "
         "negatives / ad-group structure / ad-group targets.",
         (f"FILTER: baseline excludes ad groups matching "
          f"'%{(args.exclude_adgroup_pattern or '').strip()}%' "
          "(exited-by-design vertical)."
          if (args.exclude_adgroup_pattern or "").strip()
          else "FILTER: none — renters-era inventory may inflate "
          "the pool."), ""]
    if nb:
        L.append(f"NON-BIDDABLE (excluded from the recoverable pool "
                 f"— {', '.join(sorted(set(nb_tokens)))}): "
                 f"{len(nb)} 2025 winners, "
                 f"${sum(x['gp_2025_day'] for x in nb):,.0f}/day of "
                 "2025 GP — permanently gone, not drowned.")
        L.append("")
    tot_gp = sum(x["gp_2025_day"] for x in drowned)
    L.append(f"THE DROWNED POOL (biddable only): {len(drowned)} 2025 "
             f"winners now MISSING/PAUSED/STARVED, worth "
             f"${tot_gp:,.0f}/day of 2025-era GP "
             f"(of {len(winners)} winners, "
             f"${sum(x['gp_2025_day'] for x in winners):,.0f}/day "
             "total).")
    for st in ("MISSING", "PAUSED", "STARVED", "REDUCED", "ALIVE"):
        sel = [x for x in winners if x["status_now"] == st]
        if sel:
            L.append(f"  {st:8s} {len(sel):4d} winners  "
                     f"${sum(x['gp_2025_day'] for x in sel):,.0f}/day "
                     "of 2025 GP")
    L.append("")
    by_seg = {}
    for x in drowned:
        k = x["segment"] + (" (home-intent)" if x["home_intent"]
                            else "")
        by_seg[k] = by_seg.get(k, 0.0) + x["gp_2025_day"]
    L.append("Drowned pool by segment:")
    for k, v in sorted(by_seg.items(), key=lambda kv: -kv[1]):
        L.append(f"  {k:28s} ${v:,.0f}/day")
    L.append("")
    L.append("TOP 25 DROWNED WINNERS (by 2025 GP):")
    for x in sorted(drowned, key=lambda x: -x["gp_2025"])[:25]:
        flags = "".join(["H" if x["home_intent"] else "",
                         "A" if x["product_adjacent"] else "",
                         "P" if x["protected"] else ""])
        L.append(
            f"  {x['status_now']:8s} {x['keyword'][:34]:34s} "
            f"{x['match_type']:7s} 2025: ${x['spend_2025']:>8,.0f} @ "
            f"{x['roas_2025']:.2f} GP ${x['gp_2025']:>8,.0f}  now "
            f"${x['rec_spend_day']:>5,.0f}/d "
            f"[{x['qs_bucket_now'] or '—'}]"
            + (f" {{{flags}}}" if flags else ""))
    adj = [x for x in drowned if x["product_adjacent"]]
    if adj:
        L.append("")
        L.append(f"PRODUCT-ADJACENT drowned winners ({len(adj)} — "
                 "verify vertical fit before resurrecting): "
                 + ", ".join(f"{x['keyword'][:28]}"
                             f" (${x['gp_2025']:,.0f})"
                             for x in sorted(
                                 adj, key=lambda x: -x["gp_2025"]
                             )[:10]))
    text = "\n".join(L)
    txt_path = out_dir / f"dormant_winners_{stamp}.txt"
    txt_path.write_text(text, encoding="utf-8")
    print("\n" + text)

    # chart: scatter + drowned-pool bar
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(12.5, 4.6), dpi=150)
    for st, col in STATUS_COLORS.items():
        sel = [x for x in winners if x["status_now"] == st]
        if not sel:
            continue
        ax1.scatter([max(x["gp_2025_day"], 0.02) for x in sel],
                    [(x["spend_ratio"] if x["spend_ratio"] is not None
                      else 0.0) for x in sel],
                    s=26, color=col, alpha=0.75, label=st,
                    edgecolors="none")
    ax1.set_xscale("log")
    ax1.axhline(args.starved_frac, color="#404040", ls=":", lw=1)
    ax1.axhline(1.0, color="#404040", ls="--", lw=0.8, alpha=0.6)
    ax1.set_xlabel("2025 GP/day (log)", fontsize=8)
    ax1.set_ylabel("current spend / 2025 run-rate", fontsize=8)
    ax1.set_title("2025 winners: what they earned vs how alive they "
                  "are now", fontsize=9.5)
    ax1.legend(fontsize=6.5)
    ax1.tick_params(labelsize=7.5)
    ax1.grid(alpha=0.25)
    segs = sorted(by_seg, key=lambda k: -by_seg[k])[:10]
    ax2.barh(range(len(segs)), [by_seg[s] for s in segs],
             color="#C55A11")
    ax2.set_yticks(range(len(segs)))
    ax2.set_yticklabels(segs, fontsize=7.5)
    ax2.invert_yaxis()
    ax2.set_xlabel("2025 GP/day now drowned (missing/paused/starved)",
                   fontsize=8)
    ax2.set_title("The drowned pool by segment (2025-era dollars)",
                  fontsize=9.5)
    ax2.tick_params(labelsize=7.5)
    ax2.grid(axis="x", alpha=0.25)
    fig.suptitle("Dormant winners — 2025 earners vs the current book "
                 "(text-grain across the restructure). 2025 GP sizes "
                 "the loss, not the recovery.", fontsize=10)
    fig.tight_layout(rect=(0, 0, 1, 0.92))
    png_path = out_dir / f"dormant_winners_{stamp}.png"
    fig.savefig(png_path)
    plt.close(fig)
    print(f"\nSaved: {csv_path}")
    print(f"Saved: {png_path}")
    print(f"Saved: {txt_path}")


if __name__ == "__main__":
    main()
