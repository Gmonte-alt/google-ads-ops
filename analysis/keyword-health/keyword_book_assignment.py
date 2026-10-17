#!/usr/bin/env python
r"""
keyword_book_assignment.py — Assign every keyword to its book for the
intent-split restructure, and say whether it MOVES or STAYS.
═══════════════════════════════════════════════════════════════════════
Three books (Gilbert's revamp, 8/31):
  FLAGSHIP    bare carrier-brand terms ("farmers insurance", "geico")
              -> own campaign(s), geo-tiered, damped (species-
              selective cap); MOVES by construction (QS ledgers 2-4,
              nothing to lose; fresh ledgers may re-rate)
  BRAND-HOME  carrier + home-intent ("kin home insurance") -> combined
              nationwide campaign; MOVES (policy flag) — mostly dark
              today, ledgers not producing; rows with a healthy
              latest QS are flagged LEDGER-LOSS so the cost is stated
  GENERIC     non-brand home-intent ("home insurance quotes") ->
              becomes the surviving campaign BY SUBTRACTION; STAYS
              (this is where the healthy 7-8 QS ledgers live — the
              December-reset lesson)

Assignment = text rules (carrier token + home token) cross-checked by
the ad-group signal (any 'Non-Generic' variant = branded). Text and
structure disagreeing -> REVIEW, never silently assigned.

INCLUSION (per row, each decision cites its instrument):
  census PAUSE            -> EXCLUDE (cut-list candidate)
  census ALREADY-PAUSED   -> EXCLUDE unless a biddable 2025 winner
                             (dormant PAUSED trio) -> READD
  non-biddable brand      -> EXCLUDE (usaa/liberty mutual/lemon/
                             safeco)
  census ZOMBIE           -> INCLUDE (zero cost; footprint lesson)
  everything else         -> INCLUDE
  dormant biddable MISSING winners -> READD as new adds (product-
                             adjacent flagged for a vertical check)

MATCH POLICY: BROAD rows tagged broad-review unless health is
measured (census KEEP-BASE, or priority PRIORITY/EMERGING).

GEO: FLAGSHIP kept per current campaign (tier = geo); the nationwide
books are DEDUPED across campaigns (same text x match collapses to
one row, source campaigns listed, metrics summed).

Negatives are NOT generated here (Gilbert has the list).

INPUTS (auto-found, newest): keyword_census_*.csv (verdicts),
priority_keywords_*.csv (class/lane), dormant_winners_*.csv
(re-adds), + a DB pass for dominant ad group / branded signal /
latest QS (skipped gracefully if unreachable).

OUTPUT: keyword_book_assignment_<date>.xlsx (Summary / FLAGSHIP /
BRAND-HOME / GENERIC / READDS / EXCLUDED / REVIEW) + master CSV.

USAGE:
  python keyword_book_assignment.py
  python keyword_book_assignment.py --brand-home-policy keep
"""

import argparse
import csv
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

try:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
except ImportError:
    sys.exit("openpyxl required: pip install openpyxl")

DEFAULT_DB_PATH = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")
DEFAULT_DIR = Path(r"C:\Projects\google-ads-ops\analysis"
                   r"\profit-attribution-tracker\output\keyword-health")
ACCOUNT_ID = 1
CAMPAIGN_IDS = {
    "CALIFORNIA": 23385188622, "TEXAS": 23394808297,
    "FLORIDA": 23394808300, "TIER4": 23428389081,
    "TIER1": 23496334752, "TIER2": 23530860942,
    "TIER3": 23586637678,
}
# carrier tokens: tripwire set + brands seen in the drowned pool and
# profile tables. Extend freely — unknown brands surface in REVIEW.
CARRIER_TOKENS = [
    "geico", "farmers", "state farm", "statefarm", "travelers",
    "kin insurance", " kin ", "kin home", "aaa", "amica", "hartford",
    "costco", "nationwide", "allstate", "progressive", "hippo",
    "bamboo", "tgs", "universal", "american home", "assurant",
    "metlife", "homesite", "the general", "mercury", "safeco",
    "liberty mutual", "lemonade", "lemon", "usaa", "chubb",
    "erie", "auto-owners", "cincinnati", "openly", "branch",
    # surfaced by the 8/27 real run's UNKNOWN-brand review (ad-group
    # said branded, text had no token) — confirm biddability
    "21st century", "allied", "american family", "century 21",
    "citizens", "citzen", "csaa", "high point", "hochheim",
    "homeowners of america", "farmer ", "liberty",
    # second review pass (8/27 keep-policy run) incl. deliberate
    # misspellings that are real targeted keywords
    "njm", "palisades", "plymouth", "root home", "root homeowners",
    "selective", "travellers", "universial", "univista", "wawanesa",
    # 9/2b ruling — regional carriers, boundary-tightened against
    # place-name collisions (lifted verbatim from structure_case.py
    # OVERRIDE_CARRIER_TOKENS)
    "sterling home", "merrimack mutual", "dtric", "cig home",
    "preferred mutual", "ocean harbor", "elephant", "zephyr home",
    "cambridge mutual",
]
# 'liberty' (not just 'liberty mutual') so 'liberty home insurance'
# is caught; extend with any brand Gilbert confirms non-biddable
NON_BIDDABLE = ["usaa", "liberty", "lemon", "safeco"]
# 9/2a + 9/4 rulings (FINAL): aggregators, the aggregator tail, and
# everquote all classify into the BRAND books (home token ->
# BRAND-HOME, else FLAGSHIP). Collision-safe forms per 9/4:
# 'way.com' literal (never bare 'way'); 'gabi home'/'gabi insurance'
# (never bare 'gabi').
BRAND_AGG_TOKENS = ["policygenius", "nerdwallet", "bankrate",
                    "zebra", "insurify", "smartfinancial",
                    "quotewizard", "gabi home", "gabi insurance",
                    "way.com", "everquote"]
HOME_TOKENS = ("home", "homeowner", "homeowners", "house",
               "dwelling")
# R6 (9/8 ruling): product-intent brand terms route to BRAND-HOME —
# home-adjacent product intent monetizes like brand+home and lives in
# the Brand-Home-Intent campaign. FLAGSHIP = bare brand navigational
# only.
PRODUCT_INTENT_TOKENS = ("condo", "condominium", "landlord")
# "property" counts as product intent EXCEPT inside a carrier name
# ("property and casualty" / "property & casualty").
PC_CARVEOUTS = ("property and casualty", "property & casualty")


def brand_home_intent(kw):
    """R6: does this brand keyword carry home/product intent?"""
    k = f" {kw.lower().strip()} "
    if any(t in k for t in HOME_TOKENS):
        return True
    if any(t in k for t in PRODUCT_INTENT_TOKENS):
        return True
    if "property" in k:
        stripped = k
        for c in PC_CARVEOUTS:
            stripped = stripped.replace(c, " ")
        if "property" in stripped:
            return True
    return False


# 9/8 strikes: typo keywords with negligible data — hard EXCLUDE
STRIKES = {("citzen home insurance quote", "EXACT"),
           ("universial property and casualty", "BROAD")}
ADJACENT_TOKENS = ("landlord", "renter", "renters", "condo", "flood",
                   "auto", "car ", " car", "umbrella", "business",
                   "commercial", "life ")
HEADER_FILL = PatternFill("solid", fgColor="1F3864")
HEADER_FONT = Font(bold=True, color="FFFFFF", size=9)
SOFT_FONT = Font(italic=True, color="808080", size=9)
BOOK_FILL = {"FLAGSHIP": PatternFill("solid", fgColor="D9E2F3"),
             "BRAND-HOME": PatternFill("solid", fgColor="E2EFDA"),
             "GENERIC": PatternFill("solid", fgColor="FFF2CC")}


def newest(d, pattern):
    d = Path(d)
    cands = sorted(d.glob(pattern), key=lambda x: x.stat().st_mtime,
                   reverse=True) if d.exists() else []
    return cands[0] if cands else None


def fnum(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def load_csv(p):
    with open(p, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def padded(kw):
    return f" {kw.lower().strip()} "


def has_any(kw, tokens):
    k = padded(kw)
    return any(t in k for t in tokens)


def is_branded_name(nm):
    norm = "".join(ch for ch in (nm or "").lower() if ch.isalnum())
    return "nongeneric" in norm


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=str(DEFAULT_DIR))
    ap.add_argument("--out-dir", default=str(DEFAULT_DIR))
    ap.add_argument("--db-path", default=str(DEFAULT_DB_PATH))
    ap.add_argument("--census-csv", default=None)
    ap.add_argument("--priority-csv", default=None)
    ap.add_argument("--dormant-csv", default=None)
    ap.add_argument("--brand-home-policy", choices=["move", "keep"],
                    default="move")
    ap.add_argument("--freeze", action="store_true",
                    help="emit keyword_book_assignment_FROZEN_<date>"
                         ".csv with the ruleset header line for the "
                         "rollout gate")
    ap.add_argument("--ledger-qs", type=float, default=7.0,
                    help="latest QS at/above this on a MOVING row "
                         "raises a LEDGER-LOSS flag")
    ap.add_argument("--map", action="append",
                    help='override ids: --map "TIER2=123"')
    args = ap.parse_args()

    camp_ids = dict(CAMPAIGN_IDS)
    for m in args.map or []:
        k, v = m.split("=", 1)
        camp_ids[k.strip().upper()] = int(v.strip())

    cp = Path(args.census_csv) if args.census_csv else \
        newest(args.dir, "keyword_census_*.csv")
    pp = Path(args.priority_csv) if args.priority_csv else \
        newest(args.dir, "priority_keywords_*.csv")
    dp = Path(args.dormant_csv) if args.dormant_csv else \
        newest(args.dir, "dormant_winners_*.csv")
    if not cp:
        sys.exit("keyword_census_*.csv not found — run the census.")
    print("Sources:")
    for label, p in (("census", cp), ("priority", pp),
                     ("dormant", dp)):
        print(f"  {label:9s} {p.name if p else 'MISSING'}")
    stamp = cp.stem.replace("keyword_census_", "")

    census = load_csv(cp)
    prio = {}
    if pp:
        for r in load_csv(pp):
            prio[(r["campaign"], r["match_type"].upper(),
                  r["keyword"].lower())] = r
    dormant = load_csv(dp) if dp else []

    # DB pass: dominant ad group + branded signal + latest QS
    ag_of, latest_qs = {}, {}
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
            return next((c for c in cands if c in cols), None)
        impr = pcol("keyword_performance_daily",
                    ["impressions", "impr"])
        agn = pcol("ad_groups", ["ad_group_name", "name"])
        agk = pcol("ad_groups", ["id", "ad_group_id"])
        has_kqd = pcol("keyword_quality_daily",
                       ["historical_quality_score"]) is not None
        short_of = {c: s for s, c in camp_ids.items()}
        ids = list(camp_ids.values())
        if impr and agn and agk:
            row = conn.execute(
                """SELECT MAX(p.report_date)
                   FROM keyword_performance_daily p
                   JOIN keyword_instances ki
                   ON ki.id = p.instance_id
                   WHERE ki.account_id = ?""", [ACCOUNT_ID]).fetchone()
            dend = datetime.strptime(str(row[0])[:10],
                                     "%Y-%m-%d").date()
            d90 = (dend - timedelta(days=89)).isoformat()
            q = (f"SELECT ki.campaign_id cid, "
                 f"LOWER(TRIM(ki.keyword_text)) kw, "
                 f"UPPER(TRIM(ki.match_type)) mt, agx.{agn} agname, "
                 f"SUM(p.{impr}) im "
                 f"FROM keyword_instances ki "
                 f"LEFT JOIN ad_groups agx "
                 f"ON agx.{agk} = ki.ad_group_id "
                 f"LEFT JOIN keyword_performance_daily p "
                 f"ON p.instance_id = ki.id AND p.device = 'ALL' "
                 f"AND p.report_date >= '{d90}' "
                 f"WHERE ki.account_id = ? AND ki.campaign_id IN "
                 f"({','.join('?' * len(ids))}) GROUP BY 1, 2, 3, 4")
            acc = {}
            for r in conn.execute(q, [ACCOUNT_ID] + ids):
                short = short_of.get(int(r["cid"]))
                if not short:
                    continue
                key = (short, r["mt"] or "?", r["kw"] or "")
                acc.setdefault(key, {})[r["agname"] or "?"] = \
                    r["im"] or 0
            for key, d in acc.items():
                dom = max(d, key=d.get)
                ag_of[key] = (dom, is_branded_name(dom))
        if has_kqd:
            ql = (f"SELECT ki.campaign_id cid, "
                  f"LOWER(TRIM(ki.keyword_text)) kw, "
                  f"UPPER(TRIM(ki.match_type)) mt, "
                  f"q.historical_quality_score qs FROM ("
                  f"SELECT instance_id, historical_quality_score, "
                  f"ROW_NUMBER() OVER (PARTITION BY instance_id "
                  f"ORDER BY report_date DESC) rn "
                  f"FROM keyword_quality_daily) q "
                  f"JOIN keyword_instances ki ON ki.id = q.instance_id "
                  f"WHERE q.rn = 1 AND ki.account_id = ? "
                  f"AND ki.campaign_id IN "
                  f"({','.join('?' * len(ids))})")
            tmp = {}
            for r in conn.execute(ql, [ACCOUNT_ID] + ids):
                short = short_of.get(int(r["cid"]))
                if short and r["qs"] is not None:
                    tmp.setdefault((short, r["mt"], r["kw"]),
                                   []).append(float(r["qs"]))
            latest_qs = {k: max(v) for k, v in tmp.items()}
        conn.close()
        print(f"  DB layer: {len(ag_of):,} ad-group signals, "
              f"{len(latest_qs):,} latest QS")
    else:
        print("  NOTE: DB unreachable — ad-group cross-check and "
              "LEDGER-LOSS flags unavailable")

    # ── assignment ──────────────────────────────────────────────────
    def assign_book(kw, ag_branded):
        carrier = has_any(kw, CARRIER_TOKENS)
        home = brand_home_intent(kw)
        if has_any(kw, BRAND_AGG_TOKENS):
            return ("BRAND-HOME" if home else "FLAGSHIP"), \
                "9/2a+9/4 ruling: aggregator/partner -> brand books", \
                False
        if carrier:
            return ("BRAND-HOME" if home else "FLAGSHIP"), "text", \
                False
        if ag_branded is True:
            # structure says branded, text found no known carrier
            return ("BRAND-HOME" if home else "FLAGSHIP"), \
                "brand inferred from Non-Generic ad group — " \
                "UNKNOWN brand token, extend CARRIER_TOKENS", True
        return "GENERIC", "text", False

    rows = []
    for r in census:
        kw = r["keyword"].lower()
        mt = r["match_type"].upper()
        camp = r["campaign"]
        key = (camp, mt, kw)
        agname, ag_branded = ag_of.get(key, (None, None))
        book, basis, review = assign_book(kw, ag_branded)
        pr = prio.get(key, {})
        verdict = r.get("verdict", "")
        adj = has_any(kw, ADJACENT_TOKENS)
        if (kw, mt) in STRIKES:
            decision, why = "EXCLUDE", "9/8 strike: typo keyword, " \
                "negligible data"
        elif has_any(kw, NON_BIDDABLE):
            decision, why = "EXCLUDE", "non-biddable brand"
        elif verdict == "PAUSE":
            decision, why = "EXCLUDE", f"census PAUSE — {r.get('reason','')[:50]}"
        elif verdict == "ALREADY-PAUSED":
            decision, why = "EXCLUDE", "already paused (dormant " \
                "ledger may READD it separately)"
        elif verdict == "ZOMBIE":
            decision, why = "INCLUDE", "zombie — zero cost; " \
                "footprint lesson (fresh ledger if book moves)"
        else:
            decision, why = "INCLUDE", f"census {verdict}"
        moves = (book == "FLAGSHIP") or \
            (book == "BRAND-HOME" and args.brand_home_policy == "move")
        lq = latest_qs.get(key)
        ledger_loss = bool(moves and lq is not None
                           and lq >= args.ledger_qs
                           and decision == "INCLUDE")
        broad_review = (mt == "BROAD" and decision == "INCLUDE"
                        and verdict != "KEEP-BASE"
                        and pr.get("class") not in ("PRIORITY",
                                                    "EMERGING"))
        rows.append({
            "campaign": camp, "keyword": kw, "match_type": mt,
            "book": book, "assign_basis": basis,
            "decision": decision, "decision_reason": why,
            "action": ("MOVE" if moves else "KEEP") if decision ==
            "INCLUDE" else "",
            "ledger_loss_flag": ledger_loss,
            "broad_review": broad_review,
            "review": review or adj,
            "review_note": (basis if review else "")
            + ("; product-adjacent — verify vertical" if adj else ""),
            "ad_group": agname or "",
            "ag_branded": ag_branded,
            "latest_qs": lq,
            "census_verdict": verdict,
            "priority_class": pr.get("class", ""),
            "priority_lane": pr.get("lane", ""),
            "protected": str(r.get("protected", "")).lower() == "true",
            "spend_90": fnum(r.get("spend_90")),
            "roas_90": r.get("roas_90", ""),
            "spend_30": fnum(r.get("spend_30")),
            "roas_30": r.get("roas_30", ""),
            "impr_30": fnum(r.get("impr_30")),
            "iwqs_30": r.get("iwqs_30", ""),
            "source": "census"})

    # dormant re-adds (biddable, MISSING or PAUSED winners)
    present = {(r["match_type"], r["keyword"]) for r in rows}
    for d in dormant:
        if str(d.get("winner_2025", "")).lower() != "true":
            continue
        if str(d.get("biddable", "true")).lower() == "false":
            continue
        st = d.get("status_now", "")
        # 9/7 DNB/status rule: no UNPAUSE action for any keyword
        # whose keyword or ad-group status is paused — inactive
        # keywords are not re-included anywhere. Only MISSING
        # winners come back, as fresh ADDs.
        if st != "MISSING":
            continue
        kw, mt = d["keyword"].lower(), d["match_type"].upper()
        if (kw, mt) in STRIKES:
            continue
        book, basis, review = assign_book(kw, None)
        adj = has_any(kw, ADJACENT_TOKENS)
        rows.append({
            "campaign": "(new)",
            "keyword": kw, "match_type": mt, "book": book,
            "assign_basis": basis, "decision": "READD",
            "decision_reason": f"dormant {st} 2025 winner "
            f"(${fnum(d.get('gp_2025')):,.0f} GP @ "
            f"{fnum(d.get('roas_2025')):.2f}, 2025-era)",
            "action": "ADD",
            "ledger_loss_flag": False,
            "broad_review": mt == "BROAD",
            "review": review or adj,
            "review_note": (basis if review else "")
            + ("; product-adjacent — verify vertical" if adj else ""),
            "ad_group": "", "ag_branded": None, "latest_qs": None,
            "census_verdict": "", "priority_class": "",
            "priority_lane": "", "protected": False,
            "spend_90": 0.0, "roas_90": "", "spend_30": 0.0,
            "roas_30": "", "impr_30": 0.0, "iwqs_30": "",
            "source": "dormant"})

    # nationwide dedupe for the combined books
    def dedupe(sel):
        out = {}
        for r in sel:
            k = (r["match_type"], r["keyword"])
            o = out.setdefault(k, dict(r, campaigns=set(),
                                       spend_90=0.0, spend_30=0.0,
                                       impr_30=0.0, lanes=set(),
                                       classes=set()))
            o["campaigns"].add(r["campaign"])
            o["spend_90"] += r["spend_90"]
            o["spend_30"] += r["spend_30"]
            o["impr_30"] += r["impr_30"]
            if r["priority_lane"]:
                o["lanes"].add(r["priority_lane"])
            if r["priority_class"]:
                o["classes"].add(r["priority_class"])
            o["ledger_loss_flag"] = o["ledger_loss_flag"] or \
                r["ledger_loss_flag"]
            o["review"] = o["review"] or r["review"]
        res = []
        for o in out.values():
            o["campaign"] = "/".join(sorted(o["campaigns"]))
            o["priority_lane"] = "/".join(sorted(o["lanes"]))
            o["priority_class"] = "/".join(sorted(o["classes"]))
            res.append(o)
        return sorted(res, key=lambda x: -x["spend_90"])

    inc = [r for r in rows if r["decision"] == "INCLUDE"]
    flagship = sorted([r for r in inc if r["book"] == "FLAGSHIP"],
                      key=lambda x: (x["campaign"], -x["spend_90"]))
    brand_home = dedupe([r for r in inc if r["book"] == "BRAND-HOME"])
    generic = dedupe([r for r in inc if r["book"] == "GENERIC"])
    readds = sorted([r for r in rows if r["decision"] == "READD"],
                    key=lambda x: (x["book"], x["keyword"]))
    excluded = sorted([r for r in rows if r["decision"] == "EXCLUDE"],
                      key=lambda x: -x["spend_90"])
    review = sorted([r for r in rows if r["review"]
                     or r["ledger_loss_flag"] or r["broad_review"]],
                    key=lambda x: -x["spend_90"])

    # master csv
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / f"keyword_book_assignment_{stamp}.csv"
    fields = [k for k in rows[0].keys()]
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)

    # summary
    L = [f"KEYWORD BOOK ASSIGNMENT — data through {stamp}",
         f"Policy: FLAGSHIP moves (construction, geo-tiered); "
         f"BRAND-HOME {args.brand_home_policy}s (nationwide, "
         f"deduped); GENERIC stays (subtraction — ledgers "
         f"preserved). LEDGER-LOSS = moving row with latest QS >= "
         f"{args.ledger_qs:g}.", ""]
    for book, sel in (("FLAGSHIP", flagship),
                      ("BRAND-HOME", brand_home),
                      ("GENERIC", generic)):
        s90 = sum(r["spend_90"] for r in sel)
        grain = "per-campaign rows" if book == "FLAGSHIP" else \
            "nationwide-deduped rows"
        L.append(f"{book:11s} {len(sel):4d} {grain}  ${s90:,.0f} 90d "
                 f"spend  action={'MOVE' if book == 'FLAGSHIP' or (book == 'BRAND-HOME' and args.brand_home_policy == 'move') else 'KEEP'}")
    ll = [r for r in rows if r["ledger_loss_flag"]]
    L.append(f"READD       {len(readds):4d} rows (dormant biddable "
             "MISSING winners, fresh ADDs; 9/7 rule: no unpauses — "
             "paused keywords/ad-groups stay out)")
    L.append(f"EXCLUDED    {len(excluded):4d} rows  "
             f"${sum(r['spend_90'] for r in excluded):,.0f} 90d spend "
             "(census PAUSE / already paused / non-biddable)")
    L.append(f"LEDGER-LOSS {len(ll):4d} moving campaign-rows with latest QS >= "
             f"{args.ledger_qs:g} (${sum(r['spend_90'] for r in ll):,.0f}"
             " 90d) — the price of construction on healthy ledgers; "
             "consider --brand-home-policy keep if large")
    unk = [r for r in rows if "UNKNOWN brand" in r["assign_basis"]]
    if unk:
        L.append(f"REVIEW: {len(unk)} rows branded by ad group but "
                 "unknown to CARRIER_TOKENS — extend the list: "
                 + ", ".join(sorted({r['keyword'][:22] for r in unk})
                             [:12]))
    L.append(f"REVIEW sheet: {len(review)} rows (unknown brands, "
             "aggregators, product-adjacent, broad-review, "
             "ledger-loss)")
    text = "\n".join(L)
    print("\n" + text)

    # workbook
    wb = Workbook()
    ws = wb.active
    ws.title = "Summary"
    ws.sheet_view.showGridLines = False
    for i, line in enumerate(L, 1):
        c = ws.cell(row=i, column=1, value=line)
        c.font = Font(bold=True, size=13) if i == 1 else \
            (SOFT_FONT if i == 2 else Font(size=10))
    ws.column_dimensions["A"].width = 120

    def sheet(name, sel, cols):
        w_ = wb.create_sheet(name)
        w_.sheet_view.showGridLines = False
        for ci, (h, _) in enumerate(cols, 1):
            c = w_.cell(row=1, column=ci, value=h)
            c.fill = HEADER_FILL
            c.font = HEADER_FONT
            c.alignment = Alignment(horizontal="center")
        for ri, r in enumerate(sel, 2):
            for ci, (_, k) in enumerate(cols, 1):
                v = r.get(k)
                if isinstance(v, bool):
                    v = "YES" if v else ""
                if isinstance(v, set):
                    v = "/".join(sorted(v))
                c = w_.cell(row=ri, column=ci, value=v)
                if k == "book" and v in BOOK_FILL:
                    c.fill = BOOK_FILL[v]
        for ci, (_, k) in enumerate(cols, 1):
            w_.column_dimensions[get_column_letter(ci)].width = \
                40 if k == "keyword" else \
                (30 if k in ("decision_reason", "review_note",
                             "ad_group") else 12)
        w_.freeze_panes = "B2"

    base_cols = [("Campaign(s)", "campaign"), ("Keyword", "keyword"),
                 ("Match", "match_type"), ("Book", "book"),
                 ("Action", "action"), ("Census", "census_verdict"),
                 ("Priority", "priority_class"),
                 ("Lane", "priority_lane"),
                 ("Latest QS", "latest_qs"), ("iwQS30", "iwqs_30"),
                 ("Spend90", "spend_90"), ("ROAS90", "roas_90"),
                 ("Spend30", "spend_30"), ("ROAS30", "roas_30"),
                 ("Impr30", "impr_30"), ("Ad group", "ad_group"),
                 ("LedgerLoss", "ledger_loss_flag"),
                 ("BroadReview", "broad_review"),
                 ("Prot", "protected"), ("Note", "review_note")]
    sheet("FLAGSHIP", flagship, base_cols)
    sheet("BRAND-HOME", brand_home, base_cols)
    sheet("GENERIC", generic, base_cols)
    sheet("READDS", readds,
          [("Book", "book"), ("Keyword", "keyword"),
           ("Match", "match_type"), ("Action", "action"),
           ("Why", "decision_reason"), ("Note", "review_note")])
    sheet("EXCLUDED", excluded,
          [("Campaign", "campaign"), ("Keyword", "keyword"),
           ("Match", "match_type"), ("Book", "book"),
           ("Why", "decision_reason"), ("Spend90", "spend_90"),
           ("ROAS90", "roas_90"), ("Prot", "protected")])
    sheet("REVIEW", review,
          [("Campaign", "campaign"), ("Keyword", "keyword"),
           ("Match", "match_type"), ("Book", "book"),
           ("Decision", "decision"), ("Action", "action"),
           ("LedgerLoss", "ledger_loss_flag"),
           ("BroadReview", "broad_review"),
           ("Latest QS", "latest_qs"), ("Spend90", "spend_90"),
           ("Ad group", "ad_group"), ("Note", "review_note"),
           ("Basis", "assign_basis")])
    xlsx_path = out_dir / f"keyword_book_assignment_{stamp}.xlsx"
    wb.save(xlsx_path)
    print(f"\nSaved: {xlsx_path}")
    print(f"Saved: {csv_path}")

    if args.freeze:
        RULESET = "8/27+9/2a+9/2b+9/4-aggtail+9/8-prodintent"
        fpath = out_dir / \
            f"keyword_book_assignment_FROZEN_{stamp}.csv"
        with open(fpath, "w", newline="", encoding="utf-8") as f:
            f.write(f"# frozen: {stamp}, ruleset: {RULESET}, "
                    f"row_count: {len(rows)}\n")
            w = csv.DictWriter(f, fieldnames=fields,
                               extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
        nonexc = {}
        for r in rows:
            if r["decision"] in ("INCLUDE", "READD"):
                nonexc[r["book"]] = nonexc.get(r["book"], 0) + 1
        print(f"Saved: {fpath}")
        print(f"FROZEN non-EXCLUDE tag counts (INCLUDE+READD "
              f"campaign-rows): "
              f"GENERIC {nonexc.get('GENERIC', 0)} / "
              f"BRAND-HOME {nonexc.get('BRAND-HOME', 0)} / "
              f"FLAGSHIP {nonexc.get('FLAGSHIP', 0)} "
              f"(row_count {len(rows)})")


if __name__ == "__main__":
    main()
