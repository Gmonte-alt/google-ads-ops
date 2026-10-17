#!/usr/bin/env python
r"""
keyword_profile_table.py — Layer 1: the base / incremental / probe map
═══════════════════════════════════════════════════════════════════════
Assembles the one-page keyword profile the rebuild plan needs from the
instruments already on disk — no new measurement, one consistent data
vintage per run:

  BASE        what carries the P&L today and must be held/contained:
              flagship-carrier dependency (rev/GP shares), the top
              flagship rows, and the HEALTHY generic home-intent
              kernel — from keyword_health_detail_*.csv.
  INCREMENTAL where measured margin says growth comes from: EXPANDABLE
              segments with pooled incremental ROAS and observed $/day
              absorption caps (marginal_roas_map_*.csv +
              episodes_*.csv), funded by the S1-freed budget
              (recomputed from the health detail).
  PROBES      what needs a test before money: UNTESTED segments from
              the map, top biddable drowned re-add candidates and the
              PAUSED set from dormant_winners_*.csv, product-adjacent
              flags carried.

OUTPUT: keyword_profile_<date>.md (meeting one-pager) and
keyword_profile_<date>.xlsx (same content, one sheet). Sources and
data-through dates printed on both.

USAGE:
  python keyword_profile_table.py
  python keyword_profile_table.py --health-dir <dir> --map-dir <dir>
"""

import argparse
import csv
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

try:
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    from openpyxl.utils import get_column_letter
except ImportError:
    sys.exit("openpyxl required: pip install openpyxl")

DEFAULT_DB_PATH = Path(r"C:\Projects\google-ads-ops\db"
                       r"\paid_search.db")
DEFAULT_HEALTH_DIR = Path(r"C:\Projects\google-ads-ops\analysis"
                          r"\profit-attribution-tracker\output"
                          r"\keyword-health")
CAMPAIGN_IDS = {
    "CALIFORNIA": 23385188622,
    "TEXAS":      23394808297,
    "FLORIDA":    23394808300,
    "TIER4":      23428389081,
    "TIER1":      23496334752,
    "TIER2":      23530860942,
    "TIER3":      23586637678,
}
def is_branded_name(nm):
    """Branded = ad-group name contains any variant of 'Non-Generic'
    (Non-Generic, NonGeneric, non_generic, ...). Generic = everything
    else. Normalizes out separators so all spellings match."""
    norm = "".join(ch for ch in (nm or "").lower() if ch.isalnum())
    return "nongeneric" in norm
DEFAULT_MAP_DIR = Path(r"C:\Projects\google-ads-ops\analysis"
                       r"\profit-attribution-tracker\output"
                       r"\marginal-roas-map")
CARRIERS = {"Geico", "Farmers", "State Farm", "Travelers", "Kin",
            "AAA", "Amica", "Hartford", "Costco"}
HOME_TOKENS = ("home", "homeowner", "homeowners", "house")
HEADER_FILL = PatternFill("solid", fgColor="1F3864")
HEADER_FONT = Font(bold=True, color="FFFFFF", size=10)
SECTION_FONT = Font(bold=True, size=12)
SOFT_FONT = Font(italic=True, color="808080", size=9)


def newest(d, pattern):
    d = Path(d)
    if not d.exists():
        return None
    cands = sorted(d.glob(pattern), key=lambda x: x.stat().st_mtime,
                   reverse=True)
    return cands[0] if cands else None


def fnum(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def load_csv(p):
    with open(p, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--health-dir", default=str(DEFAULT_HEALTH_DIR))
    ap.add_argument("--map-dir", default=str(DEFAULT_MAP_DIR))
    ap.add_argument("--out-dir", default=str(DEFAULT_HEALTH_DIR))
    ap.add_argument("--health-csv", default=None)
    ap.add_argument("--map-csv", default=None)
    ap.add_argument("--episodes-csv", default=None)
    ap.add_argument("--dormant-csv", default=None)
    ap.add_argument("--top", type=int, default=8)
    ap.add_argument("--db-path", default=str(DEFAULT_DB_PATH))
    ap.add_argument("--qs-days", type=int, default=14,
                    help="window for keyword/ad-group QS columns")
    ap.add_argument("--map", action="append",
                    help='override ids: --map "TIER2=123"')
    args = ap.parse_args()

    hp = Path(args.health_csv) if args.health_csv else \
        newest(args.health_dir, "keyword_health_detail_*.csv")
    mp = Path(args.map_csv) if args.map_csv else \
        newest(args.map_dir, "marginal_roas_map_*.csv")
    ep = Path(args.episodes_csv) if args.episodes_csv else \
        newest(args.map_dir, "episodes_*.csv")
    dp = Path(args.dormant_csv) if args.dormant_csv else \
        newest(args.health_dir, "dormant_winners_*.csv")
    if not hp:
        sys.exit("keyword_health_detail_*.csv not found — run the "
                 "health scenario first.")
    print("Sources:")
    for label, p in (("health", hp), ("map", mp), ("episodes", ep),
                     ("dormant", dp)):
        print(f"  {label:9s} {p.name if p else 'MISSING'}")

    kws = load_csv(hp)
    for k in kws:
        for c in ("rec_spend_day", "rec_rev_day", "rec_conv_day",
                  "rec_gp_day", "stab_spend"):
            k[c] = fnum(k.get(c))
        k["protected"] = str(k.get("protected", "")).lower() == "true"
        k["home"] = any(t in (k.get("keyword") or "")
                        for t in HOME_TOKENS)
        k["carrier"] = k.get("segment") in CARRIERS
    stamp = hp.stem.replace("keyword_health_detail_", "")

    # ── DB enrichment: last-N-day keyword QS, dominant ad group +
    # its iwQS, and the authoritative branded/generic split
    # (ad groups containing 'Non-Generic-' = BRANDED) ──────────────
    camp_ids = dict(CAMPAIGN_IDS)
    for m in args.map or []:
        k, v = m.split("=", 1)
        camp_ids[k.strip().upper()] = int(v.strip())
    short_of = {cid: s for s, cid in camp_ids.items()}
    kw14, ag_of, class14 = {}, {}, {}
    n_disagree, d_disagree = 0, 0.0
    db = Path(args.db_path)
    if db.exists():
        conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro",
                               uri=True)
        conn.row_factory = sqlite3.Row

        def pcol(table, cands):
            try:
                cols = {r[1] for r in conn.execute(
                    f"PRAGMA table_info({table})")}
            except sqlite3.Error:
                return None
            for c in cands:
                if c in cols:
                    return c
            return None
        impr = pcol("keyword_performance_daily",
                    ["impressions", "impr"])
        rev = pcol("keyword_performance_daily",
                   ["conversion_value", "conversions_value",
                    "conv_value", "primary_conv_value",
                    "total_conv_value", "all_conv_value"])
        agn = pcol("ad_groups", ["ad_group_name", "name",
                                 "adgroup_name"])
        agk = pcol("ad_groups", ["id", "ad_group_id"])
        has_kqd = pcol("keyword_quality_daily",
                       ["historical_quality_score"]) is not None
        has_cid = pcol("keyword_instances",
                       ["campaign_id"]) is not None
        if impr and rev and agn and agk and has_kqd and has_cid:
            row = conn.execute(
                """SELECT MAX(p.report_date)
                   FROM keyword_performance_daily p
                   JOIN keyword_instances ki
                   ON ki.id = p.instance_id
                   WHERE ki.account_id = 1""").fetchone()
            dend = datetime.strptime(str(row[0])[:10],
                                     "%Y-%m-%d").date()
            dstart = dend - timedelta(days=args.qs_days - 1)
            ids = list(camp_ids.values())
            q = (f"SELECT ki.campaign_id cid, "
                 f"LOWER(TRIM(ki.keyword_text)) kw, "
                 f"UPPER(TRIM(ki.match_type)) mt, "
                 f"ki.ad_group_id agid, agx.{agn} agname, "
                 f"SUM(p.{impr}) im, "
                 f"SUM(p.cost_micros)/1e6 s, SUM(p.{rev}) r, "
                 f"SUM(CASE WHEN q.historical_quality_score IS NOT "
                 f"NULL THEN p.{impr} ELSE 0 END) ri, "
                 f"SUM(CASE WHEN q.historical_quality_score IS NOT "
                 f"NULL THEN q.historical_quality_score * p.{impr} "
                 f"ELSE 0 END) qi "
                 f"FROM keyword_performance_daily p "
                 f"JOIN keyword_instances ki "
                 f"ON ki.id = p.instance_id AND ki.account_id = 1 "
                 f"LEFT JOIN ad_groups agx "
                 f"ON agx.{agk} = ki.ad_group_id "
                 f"LEFT JOIN keyword_quality_daily q "
                 f"ON q.instance_id = p.instance_id "
                 f"AND q.report_date = p.report_date "
                 f"WHERE p.device = 'ALL' "
                 f"AND p.report_date BETWEEN ? AND ? "
                 f"AND ki.campaign_id IN "
                 f"({','.join('?' * len(ids))}) "
                 f"GROUP BY 1, 2, 3, 4, 5")
            kwacc, agacc = {}, {}
            for r in conn.execute(
                    q, [dstart.isoformat(), dend.isoformat()] + ids):
                short = short_of.get(int(r["cid"]))
                if not short:
                    continue
                key = (short, r["mt"] or "?", r["kw"] or "")
                a = kwacc.setdefault(key, {"ri": 0.0, "qi": 0.0,
                                           "ags": {}})
                a["ri"] += r["ri"] or 0
                a["qi"] += r["qi"] or 0
                nm = r["agname"] or "(no ad_groups row)"
                a["ags"][nm] = a["ags"].get(nm, 0) + (r["im"] or 0)
                g = agacc.setdefault(nm, {"ri": 0.0, "qi": 0.0,
                                          "s": 0.0, "r": 0.0})
                g["ri"] += r["ri"] or 0
                g["qi"] += r["qi"] or 0
                g["s"] += r["s"] or 0
                g["r"] += r["r"] or 0
            nd = float(args.qs_days)
            for nm, g in agacc.items():
                cls = "BRANDED" if is_branded_name(nm) else \
                    "GENERIC"
                c = class14.setdefault(cls, {"s": 0.0, "r": 0.0})
                c["s"] += g["s"] / nd
                c["r"] += g["r"] / nd
            for key, a in kwacc.items():
                kw14[key] = (a["qi"] / a["ri"]) if a["ri"] > 0                     else None
                dom = max(a["ags"], key=a["ags"].get)                     if a["ags"] else None
                if dom:
                    g = agacc[dom]
                    ag_of[key] = {
                        "name": dom,
                        "branded": is_branded_name(dom),
                        "qs": (g["qi"] / g["ri"]) if g["ri"] > 0
                        else None,
                        "multi": len(a["ags"]) > 1}
            for k in kws:
                key = (k.get("campaign"), k.get("match_type"),
                       k.get("keyword"))
                ai = ag_of.get(key)
                if ai and ai["branded"] != k["carrier"]:
                    n_disagree += 1
                    d_disagree += k["rec_spend_day"]
            print(f"  QS/ad-group layer: {len(kw14):,} keywords, "
                  f"{len(agacc):,} ad groups, last {args.qs_days}d "
                  f"({dstart}->{dend})")
        else:
            print("  NOTE: QS/ad-group enrichment unavailable "
                  "(missing columns) — QS columns blank")
        conn.close()
    else:
        print(f"  NOTE: DB not found at {db} — QS columns blank")

    def qsf(v):
        return f"{v:.1f}" if v is not None else "—"

    def kwrow(k):
        key = (k.get("campaign"), k.get("match_type"),
               k.get("keyword"))
        ai = ag_of.get(key) or {}
        agnm = (ai.get("name") or "—")[:26] +             ("*" if ai.get("multi") else "")
        return (f"| {k.get('keyword')} | {k.get('match_type')} | "
                f"{k.get('campaign')} | {agnm} | {k.get('class')} | "
                f"{qsf(kw14.get(key))} | {qsf(ai.get('qs'))} | "
                f"${k['rec_gp_day']:,.0f} | "
                f"${k['rec_spend_day']:,.0f} |")

    def is_branded_row(k):
        ai = ag_of.get((k.get("campaign"), k.get("match_type"),
                        k.get("keyword")))
        return ai["branded"] if ai else k["carrier"]

    tot_rev = sum(k["rec_rev_day"] for k in kws)
    tot_gp = sum(k["rec_gp_day"] for k in kws)
    tot_conv = sum(k["rec_conv_day"] for k in kws)
    car = [k for k in kws if k["carrier"]]
    car_rev = sum(k["rec_rev_day"] for k in car)
    car_gp = sum(k["rec_gp_day"] for k in car)
    top_flag = sorted(car, key=lambda k: -k["rec_gp_day"])[:args.top]
    kern = [k for k in kws if not k["carrier"] and k["home"]
            and k.get("class") == "HEALTHY"]
    kern_gp = sum(k["rec_gp_day"] for k in kern)
    gen_home = [k for k in kws if not k["carrier"] and k["home"]]
    unh = [k for k in kws if k.get("class") == "UNHEALTHY"
           and not k["protected"]]
    s1_freed = sum(k["rec_spend_day"] for k in unh)
    s1_gp = -sum(k["rec_gp_day"] for k in unh)
    prot_bad = [k for k in kws if k["protected"]
                and k.get("class") in ("UNHEALTHY", "BORDERLINE")]

    # incremental: map + caps
    inc_rows, unt_rows = [], []
    if mp:
        caps = {}
        if ep:
            tmp = {}
            for r in load_csv(ep):
                if str(r.get("qualified", "")).lower() != "true":
                    continue
                ds, wd = fnum(r.get("dspend")), fnum(r.get("win_days"))
                if ds > 0 and wd > 0:
                    tmp.setdefault((r.get("campaign"),
                                    r.get("segment")), []).append(
                        ds / wd)
            caps = {k: sum(v) / len(v) for k, v in tmp.items()}
        for r in load_csv(mp):
            key = (r.get("campaign"), r.get("segment"))
            if r.get("class") == "EXPANDABLE":
                inc_rows.append({
                    "campaign": r.get("campaign"),
                    "segment": r.get("segment"),
                    "inc": fnum(r.get("pooled_inc_roas")),
                    "dspend": fnum(r.get("dspend_absorbed")),
                    "eps": r.get("episodes_qualifying", ""),
                    "cap": caps.get(key)})
            elif r.get("class") == "UNTESTED" and \
                    r.get("campaign") != "ACCOUNT":
                unt_rows.append(key)
        inc_rows.sort(key=lambda x: -(x["inc"] or 0))

    # probes: dormant re-adds
    readds, paused, adjacent = [], [], []
    if dp:
        dw = load_csv(dp)
        for r in dw:
            if str(r.get("winner_2025", "")).lower() != "true":
                continue
            if str(r.get("biddable", "true")).lower() == "false":
                continue
            row = {"kw": r.get("keyword"), "mt": r.get("match_type"),
                   "gp25": fnum(r.get("gp_2025")),
                   "roas25": fnum(r.get("roas_2025")),
                   "status": r.get("status_now"),
                   "adj": str(r.get("product_adjacent",
                                    "")).lower() == "true"}
            if row["status"] == "PAUSED":
                paused.append(row)
            elif row["status"] == "MISSING":
                (adjacent if row["adj"] else readds).append(row)
        readds.sort(key=lambda x: -x["gp25"])
        adjacent.sort(key=lambda x: -x["gp25"])
        paused.sort(key=lambda x: -x["gp25"])

    # ── markdown one-pager ─────────────────────────────────────────
    L = [f"# Keyword profile — base / incremental / probes",
         f"**Data through {stamp}** · sources: {hp.name}"
         + (f" · {mp.name}" if mp else "")
         + (f" · {dp.name}" if dp else ""),
         "",
         "## BASE — carries the P&L today: hold and contain",
         "- Carrier terms: **"
         + (f"{car_rev / tot_rev:.0%} of revenue"
            if tot_rev > 0 else "n/a")
         + (f", {car_gp / tot_gp:.0%} of GP"
            if tot_gp > 0 and 0 <= car_gp / tot_gp <= 1.5
            else ", GP share n/m (small/negative total)")
         + f"** (${car_rev:,.0f}/d rev, ${car_gp:,.0f}/d GP of "
         f"account ${tot_rev:,.0f}/d / ${tot_gp:,.0f}/d; "
         f"{tot_conv:,.1f} conv/d).",
         f"- HEALTHY generic home-intent kernel: **{len(kern)} rows, "
         f"${kern_gp:,.0f}/d GP** (inside a "
         f"${sum(k['rec_rev_day'] for k in gen_home):,.0f}/d, "
         f"${sum(k['rec_gp_day'] for k in gen_home):,.0f}/d-GP "
         "generic home-intent mass) — the rebuild nucleus.",
         f"- Protected Farmers home-intent exposure bet: "
         f"{len(prot_bad)} not-healthy rows, "
         f"${sum(k['rec_spend_day'] for k in prot_bad):,.0f}/d spend, "
         f"{sum(k['rec_gp_day'] for k in prot_bad):+,.0f}/d GP — "
         "held by standing rule.",
         ]
    if class14:
        b = class14.get("BRANDED", {"s": 0, "r": 0})
        g = class14.get("GENERIC", {"s": 0, "r": 0})
        L.append(
            f"- Ad-group split (authoritative: any 'Non-Generic'/"
            f"'NonGeneric' variant = branded; last "
            f"{args.qs_days}d/day): BRANDED "
            f"${b['r']:,.0f}/d rev, ${b['r'] - b['s']:,.0f}/d GP · "
            f"GENERIC ${g['r']:,.0f}/d rev, "
            f"${g['r'] - g['s']:,.0f}/d GP.")
        if n_disagree:
            L.append(
                f"- Classifier check: {n_disagree} rows "
                f"(${d_disagree:,.0f}/d spend) where the text-based "
                "segment disagrees with the ad-group class — the "
                "ad-group class is authoritative for structure "
                "decisions.")
    QS_HDR = ("| Keyword | Match | Camp | Ad group | Class | "
              f"KW QS{args.qs_days}d | AG iwQS | GP/d | Spend/d |")
    QS_SEP = "|---|---|---|---|---|---:|---:|---:|---:|"
    top_brand = sorted([k for k in kws if is_branded_row(k)],
                       key=lambda k: -k["rec_gp_day"])[:args.top]
    top_gen = sorted([k for k in kws if not is_branded_row(k)],
                     key=lambda k: -k["rec_gp_day"])[:args.top]
    L += ["", "Top BRANDED rows (hold; containment targets):", "",
          QS_HDR, QS_SEP]
    for k in top_brand:
        L.append(kwrow(k))
    L += ["", "Top GENERIC rows (the rebuild book — keyword QS vs "
          "its ad group's iwQS shows who is dragging whom):", "",
          QS_HDR, QS_SEP]
    for k in top_gen:
        L.append(kwrow(k))
    if ag_of:
        L.append("")
        L.append(f"*QS columns: impression-weighted over the last "
                 f"{args.qs_days}d; '—' = no rated impressions "
                 "(QS never summed; unrated excluded). Ad group = "
                 "dominant by impressions; '*' = keyword spans "
                 "multiple ad groups.*")
    L += ["", "## INCREMENTAL — measured margin, capacity-capped",
          f"- Funding: S1 cleanup frees **${s1_freed:,.0f}/day** "
          f"({len(unh)} unprotected UNHEALTHY rows; removing them "
          f"adds ${s1_gp:,.0f}/d GP before redeploy).", ""]
    if inc_rows:
        L.append("| Campaign | Segment | Pooled inc ROAS | "
                 "Δspend absorbed | Cap $/day | Episodes |")
        L.append("|---|---|---:|---:|---:|---:|")
        for r in inc_rows:
            L.append(f"| {r['campaign']} | {r['segment']} | "
                     f"{r['inc']:.2f} | ${r['dspend']:,.0f} | "
                     + (f"${r['cap']:,.0f}" if r['cap'] else "—")
                     + f" | {r['eps']} |")
        L.append("")
        L.append("*Classes rest on 1–2 episodes until the wave-close "
                 "rerun; quote as provisional.*")
    else:
        L.append("*(map CSV missing — rerun marginal_roas_map.py)*")
    L += ["", "## PROBES — test before money"]
    if unt_rows:
        by_c = {}
        for c, s in unt_rows:
            by_c.setdefault(c, []).append(s)
        L.append("- UNTESTED segments (never absorbed a qualifying "
                 "expansion): "
                 + " · ".join(f"{c}: {', '.join(sorted(v))}"
                              for c, v in sorted(by_c.items())))
    if readds:
        L.append(f"- Top biddable MISSING re-adds (2025-era GP; "
                 "modification-grade): "
                 + ", ".join(f"{r['kw']} (${r['gp25']:,.0f} @ "
                             f"{r['roas25']:.2f})"
                             for r in readds[:args.top]))
    if paused:
        L.append("- PAUSED winners (toggle-grade after ledger check): "
                 + ", ".join(f"{r['kw']} (${r['gp25']:,.0f})"
                             for r in paused[:5]))
    if adjacent:
        L.append("- PRODUCT-ADJACENT (verify vertical fit first): "
                 + ", ".join(f"{r['kw']} (${r['gp25']:,.0f})"
                             for r in adjacent[:5]))
    L += ["", "*Era caveat: 2025 GP sizes what was lost, not what "
          "resurrection earns. Under campaign tROAS none of the "
          "incremental/probe rows can be force-fed — allocation "
          "control (negatives / ad-group structure / targets) is the "
          "enabling condition.*"]
    md = "\n".join(L)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    md_path = out_dir / f"keyword_profile_{stamp}.md"
    md_path.write_text(md, encoding="utf-8")
    print("\n" + md)

    # ── xlsx (one sheet, three blocks) ─────────────────────────────
    wb = Workbook()
    ws = wb.active
    ws.title = "Keyword Profile"
    ws.sheet_view.showGridLines = False
    r = 1
    ws.cell(row=r, column=1, value="Keyword profile — base / "
            f"incremental / probes — data through {stamp}"
            ).font = Font(bold=True, size=13)
    r += 2
    for line in L[3:]:
        if line.startswith("## "):
            r += 1
            ws.cell(row=r, column=1,
                    value=line[3:]).font = SECTION_FONT
            r += 1
        elif line.startswith("|") and "---" not in line:
            cells = [c.strip() for c in line.strip("|").split("|")]
            hdr = cells[0] in ("Keyword", "Campaign")
            for ci, v in enumerate(cells, 1):
                c = ws.cell(row=r, column=ci, value=v)
                if hdr:
                    c.fill = HEADER_FILL
                    c.font = HEADER_FONT
            r += 1
        elif line.startswith("*") and line.endswith("*"):
            ws.cell(row=r, column=1,
                    value=line.strip("*")).font = SOFT_FONT
            r += 1
        elif line.startswith("- ") or (line and not
                                       line.startswith("#")):
            ws.cell(row=r, column=1, value=line.lstrip("- "))
            r += 1
    for ci, w in enumerate([40, 8, 11, 27, 11, 9, 8, 9, 9], 1):
        ws.column_dimensions[get_column_letter(ci)].width = w
    xlsx_path = out_dir / f"keyword_profile_{stamp}.xlsx"
    wb.save(xlsx_path)
    print(f"\nSaved: {md_path}")
    print(f"Saved: {xlsx_path}")


if __name__ == "__main__":
    main()
