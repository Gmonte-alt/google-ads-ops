"""
structure_case.py — Structure Case: CTR/eligibility decomposition by
keyword class (the prescription evidence for the three-book restructure).

CLAIM UNDER TEST: the account's loss of ability to compete for clicks is
(a) keyword-class-shaped and (b) transmitted/amplified by mixing classes in
shared campaigns — not geography-shaped.

CLASS TAGS: read from the newest keyword_book_assignment_*.csv (the actual
migration artifact; join key = LOWER(TRIM(text)) x UPPER(match)). Historical
rows absent from the CSV (killed keywords, 2025 legacy campaigns) classify
via the tagger's own token rules (lifted verbatim), flagged fallback with
share printed. If no assignment CSV is found, the whole run is fallback-
tagged and says so; the frozen tag run supersedes.

LEDGER STATUS: INTACT = TEXAS 23394808297; RESET = TIER1-4; CA/FL context;
pre-cutover campaigns = LEGACY (2025 era).

CONVENTIONS (pinned): kpd JOIN keyword_instances (kpd has no account_id),
device='ALL', date col resolved at runtime; leads = conversions; kpd revenue
column resolved at runtime with printed evidence; iw_QS = SUM(qs*impr)/
SUM(impr) joined (instance_id, report_date), rated_share ALWAYS alongside,
guard fires < 0.90; pCTR-below = rated-impression share in BELOW bucket;
agp for IS (impression-weighted proxy, labeled), eligible = impr/IS.

GATES: synthetic shift-share gate every run (frozen class CTRs + shifting
mix -> ~100% MIX); Laspeyres identity fsum + relative tolerance; smoke
anchors = A2/A4 kpd-grain anchors (TX HI Mar 783 / Apr 358, T2 Mar 781,
+/-10%) where HI = BRAND-HOME + GENERIC books.

Usage:
  python structure_case.py --db paid_search.db --smoke
  python structure_case.py --db paid_search.db [--assignment path.csv]
Outputs -> <script dir>/output/structure_case.xlsx
"""

import argparse
import csv
import glob
import math
import os
import sqlite3
import sys
import time
from collections import defaultdict

ACCOUNT_ID = 1
CAMPAIGN_IDS = {
    "CALIFORNIA": 23385188622, "TEXAS": 23394808297,
    "FLORIDA": 23394808300, "TIER4": 23428389081,
    "TIER1": 23496334752, "TIER2": 23530860942,
    "TIER3": 23586637678,
}
LEDGER = {"TEXAS": "INTACT", "TIER1": "RESET", "TIER2": "RESET",
          "TIER3": "RESET", "TIER4": "RESET",
          "CALIFORNIA": "SINGLE-OTHER", "FLORIDA": "SINGLE-OTHER"}
RESET_EVENTS = [("2024-07-15", "copy reset #1 (~Jul-24)"),
                ("2024-11-01", "copy reset #2 (~Nov-24)"),
                ("2025-07-28", "copy reset #3 (7/28/25)"),
                ("2026-01-01", "structural cutover (Dec-25/Jan-26, approx)"),
                ("2026-07-29", "7/29 re-tier")]
ANCHORS = [("TEXAS", "2026-03", 783.0), ("TEXAS", "2026-04", 358.0),
           ("TIER2", "2026-03", 781.0)]
ANCHOR_TOL = 0.10
RATED_GUARD = 0.90
START = "2025-01"

# ---- token rules lifted VERBATIM from keyword_book_assignment.py ----
CARRIER_TOKENS = [
    "geico", "farmers", "state farm", "statefarm", "travelers",
    "kin insurance", " kin ", "kin home", "aaa", "amica", "hartford",
    "costco", "nationwide", "allstate", "progressive", "hippo",
    "bamboo", "tgs", "universal", "american home", "assurant",
    "metlife", "homesite", "the general", "mercury", "safeco",
    "liberty mutual", "lemonade", "lemon", "usaa", "chubb",
    "erie", "auto-owners", "cincinnati", "openly", "branch",
    "21st century", "allied", "american family", "century 21",
    "citizens", "citzen", "csaa", "high point", "hochheim",
    "homeowners of america", "farmer ", "liberty",
    "njm", "palisades", "plymouth", "root home", "root homeowners",
    "selective", "travellers", "universial", "univista", "wawanesa",
]
PARTNER_TOKENS = ["everquote"]
AGGREGATOR_TOKENS = ["policygenius", "nerdwallet", "zebra", "insurify",
                     "smartfinancial", "bankrate", "quotewizard", "gabi",
                     "way.com"]
HOME_TOKENS = ("home", "homeowner", "homeowners", "house",
               "dwelling", "property")
# 9/2 Gilbert ruling: these aggregators reclassify to the BRAND books
# (home token -> BRAND-HOME, else FLAGSHIP), overriding both the 8/27
# assignment CSV and the fallback rule. DIVERGES from the 8/27 tag run —
# relay the same change to keyword_book_assignment.py before freeze.
OVERRIDE_BRAND_TOKENS = ["policygenius", "nerdwallet", "bankrate"]
# 9/2 ruling #2: regional carriers missing from the tagger's vocabulary
# (fallback misread them as non-brand -> GENERIC). Destination in
# keyword_book_assignment.py = CARRIER_TOKENS. Tokens are boundary-
# tightened against place-name collisions: Sterling Heights MI,
# Cambridge MA, Merrimack NH, Zephyrhills FL, and 'cig' inside Cigna.
OVERRIDE_CARRIER_TOKENS = [
    "sterling home", "merrimack mutual", "dtric", "cig home",
    "preferred mutual", "ocean harbor", "elephant", "zephyr home",
    "cambridge mutual",
]
OVERRIDE_ALL = OVERRIDE_BRAND_TOKENS + OVERRIDE_CARRIER_TOKENS
# carrier families for the S8 spotlight: all keywords containing the
# token, split by book (bare brand -> FLAGSHIP vs brand+home -> BRAND-HOME)
CARRIER_SPOTLIGHT = ["geico", "farmers"]

HEADER = []


def log(msg=""):
    print(msg)
    HEADER.append(str(msg))


def die(msg):
    log(f"\n*** HARD FAIL: {msg}")
    sys.exit(1)


def has_any(kw, tokens):
    return any(t in kw for t in tokens)


def fallback_book(kw):
    """Tagger's assign_book text logic minus the ad-group signal."""
    if has_any(kw, PARTNER_TOKENS) or has_any(kw, AGGREGATOR_TOKENS):
        return "GENERIC"
    if has_any(kw, CARRIER_TOKENS):
        return "BRAND-HOME" if has_any(kw, HOME_TOKENS) else "FLAGSHIP"
    return "GENERIC"


def months_between(a, b):
    y, m = map(int, a.split("-"))
    ey, em = map(int, b.split("-"))
    out = []
    while (y, m) <= (ey, em):
        out.append(f"{y:04d}-{m:02d}")
        m += 1
        if m == 13:
            m, y = 1, y + 1
    return out


# ------------------------------------------------------------------
# Laspeyres shift-share on CTR (weights = impression shares, value = CTR)
# ------------------------------------------------------------------

def blended_ctr(cells):
    I = sum(v[0] for v in cells.values())
    C = sum(v[1] for v in cells.values())
    return (C / I if I else 0.0), I


def decompose_ctr(base, cur):
    ctr0, I0 = blended_ctr(base)
    ctr1, I1 = blended_ctr(cur)
    if not I0 or not I1:
        return None
    mix_l, within_l, inter_l = [], [], []
    for k in set(base) | set(cur):
        i0, c0 = base.get(k, (0.0, 0.0))
        i1, c1 = cur.get(k, (0.0, 0.0))
        w0, w1 = i0 / I0, i1 / I1
        p0 = (c0 / i0) if i0 else None
        p1 = (c1 / i1) if i1 else None
        p0e = p0 if p0 is not None else p1
        p1e = p1 if p1 is not None else p0
        if p0e is None:
            continue
        mix_l.append((w1 - w0) * p0e)
        within_l.append(w0 * (p1e - p0e))
        inter_l.append((w1 - w0) * (p1e - p0e))
    mix, within, inter = (math.fsum(mix_l), math.fsum(within_l),
                          math.fsum(inter_l))
    out = {"ctr0": ctr0, "ctr1": ctr1, "delta": ctr1 - ctr0,
           "mix": mix, "within": within, "interaction": inter}
    tol = 1e-9 * max(1.0, abs(ctr0), abs(ctr1)) + 1e-12
    if abs((mix + within + inter) - out["delta"]) > max(tol, 1e-9):
        die(f"identity violated: {mix + within + inter:.9f} vs "
            f"{out['delta']:.9f}")
    return out


def synthetic_gate():
    log("=" * 78)
    log("SYNTHETIC GATE — frozen class CTRs + shifting mix -> ~100% MIX")
    log("=" * 78)
    ctrs = {"FLAGSHIP": 0.05, "BRAND-HOME": 0.09, "GENERIC": 0.12}
    base = {k: (100000.0, 100000.0 * p) for k, p in ctrs.items()}
    cur = {"FLAGSHIP": (250000.0, 250000.0 * ctrs["FLAGSHIP"]),
           "BRAND-HOME": (60000.0, 60000.0 * ctrs["BRAND-HOME"]),
           "GENERIC": (40000.0, 40000.0 * ctrs["GENERIC"])}
    d = decompose_ctr(base, cur)
    share = abs(d["within"]) / max(abs(d["delta"]), 1e-12)
    log(f"  delta={d['delta']:+.4f} mix={d['mix']:+.4f} "
        f"within={d['within']:+.4f} inter={d['interaction']:+.4f} "
        f"-> within share {share:.1%}")
    if share > 0.02:
        die("synthetic gate failed: WITHIN picked up frozen CTRs")
    log("  [PASS]")


# ------------------------------------------------------------------

def table_columns(con, t):
    rows = con.execute(f"PRAGMA table_info({t})").fetchall()
    return [r[1] for r in rows] if rows else None


def resolve(cols, cands, table, what, required=True):
    for c in cands:
        if c in cols:
            return c
    if required:
        die(f"{table}: no {what} among {cands}; columns={cols}")
    return None


def load_assignment(path_arg, script_dir):
    cands = []
    if path_arg:
        cands = [path_arg]
    else:
        for pat in [os.path.join(script_dir, "keyword_book_assignment_*.csv"),
                    r"C:\Projects\google-ads-ops\analysis"
                    r"\profit-attribution-tracker\output\keyword-health"
                    r"\keyword_book_assignment_*.csv",
                    os.path.join(script_dir, "..", "*", "output",
                                 "keyword-health",
                                 "keyword_book_assignment_*.csv")]:
            cands += sorted(glob.glob(pat))
    if not cands:
        return {}, None
    path = sorted(cands)[-1]
    book_of = {}
    conflicts = 0
    with open(path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            kw = (r.get("keyword") or "").strip().lower()
            mt = (r.get("match_type") or "").strip().upper()
            bk = (r.get("book") or "").strip()
            if not kw or bk not in ("FLAGSHIP", "BRAND-HOME", "GENERIC"):
                continue
            key = (kw, mt)
            if key in book_of and book_of[key] != bk:
                conflicts += 1
            else:
                book_of[key] = bk
    return book_of, (path, conflicts)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="paid_search.db")
    ap.add_argument("--assignment", default=None,
                    help="keyword_book_assignment_*.csv (auto-found if omitted)")
    ap.add_argument("--smoke", action="store_true")
    ap.add_argument("--out", default="structure_case.xlsx")
    args = ap.parse_args()

    script_dir = os.path.dirname(os.path.abspath(__file__))
    out_dir = os.path.join(script_dir, "output")
    os.makedirs(out_dir, exist_ok=True)
    if not os.path.isabs(args.out):
        args.out = os.path.join(out_dir, args.out)

    log("=" * 78)
    log("STRUCTURE CASE — CTR/ELIGIBILITY DECOMPOSITION BY KEYWORD CLASS")
    log("Mode: " + ("SMOKE" if args.smoke else "FULL RUN"))
    log("=" * 78)
    synthetic_gate()

    con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row

    # ---- column resolution ----
    kpd_cols = table_columns(con, "keyword_performance_daily") or die(
        "keyword_performance_daily missing")
    ki_cols = table_columns(con, "keyword_instances")
    kqd_cols = table_columns(con, "keyword_quality_daily")
    agp_cols = table_columns(con, "ad_group_performance_daily")
    ag_cols = table_columns(con, "ad_groups")

    kpd_date = resolve(kpd_cols, ["report_date", "date"], "kpd", "date")
    kpd_impr = resolve(kpd_cols, ["impressions", "impr"], "kpd", "impressions")
    kpd_clicks = resolve(kpd_cols, ["clicks", "click"], "kpd", "clicks")
    kpd_cost = resolve(kpd_cols, ["cost_micros", "cost"], "kpd", "cost")
    # ARCHITECT RULING (9/1): leads = primary_conv_count paired with
    # primary_conv_value (routed-lead measure: QW Offline + EQ + GBRAIDS).
    # NEVER all_conv_count (monitor-mode/excluded actions included).
    # primary_conv_value = all_conv_value only EMPIRICALLY under the current
    # routing map (exact since 7/30) — safe for 2026 windows, not invariant.
    kpd_leads = resolve(kpd_cols, ["primary_conv_count", "conversions"],
                        "kpd", "leads (ruling: primary_conv_count)")
    v = con.execute(
        f"SELECT SUM(p.{kpd_leads}) FROM keyword_performance_daily p "
        f"JOIN keyword_instances k ON k.id=p.instance_id "
        f"WHERE k.account_id={ACCOUNT_ID} AND p.device='ALL' "
        f"AND SUBSTR(p.{kpd_date},1,7)='2026-03'").fetchone()[0]
    log(f"[LEADS] ruling-pinned kpd.{kpd_leads}; Mar-2026 sum = "
        f"{v or 0:,.1f} (anchor gate verifies vs A2/A4 figures below)")
    # geo_state grain gate (ruling: constant 'ALL' under current sync)
    if "geo_state" in kpd_cols:
        gvals = [r[0] for r in con.execute(
            "SELECT DISTINCT geo_state FROM keyword_performance_daily "
            "WHERE device='ALL' LIMIT 5")]
        log(f"[GRAIN] kpd DISTINCT geo_state (device='ALL') = {gvals}")
        if gvals and set(gvals) != {"ALL"}:
            die("kpd geo_state is not constant 'ALL' — the sync now writes "
                "state splits; per architect ruling, stop and send this "
                "back through the gate (summing would change grain).")
    kpd_rev = None
    for c in ["primary_conv_value", "conversion_value", "all_conv_value"]:
        if c in kpd_cols:
            v = con.execute(
                f"SELECT SUM(p.{c}) FROM keyword_performance_daily p "
                f"JOIN keyword_instances k ON k.id=p.instance_id "
                f"WHERE k.account_id={ACCOUNT_ID} AND p.device='ALL' "
                f"AND SUBSTR(p.{kpd_date},1,7)='2026-03'").fetchone()[0]
            log(f"[REV] kpd.{c} Mar-2026 sum = {v or 0:,.0f}")
            if v and v > 0 and kpd_rev is None:
                kpd_rev = c
    if kpd_rev is None:
        log("WARNING: no populated kpd revenue column — ROAS omitted from S1")
    log(f"[RESOLVE] kpd date={kpd_date}, revenue={kpd_rev}")

    kqd_ok = kqd_cols and "historical_quality_score" in kqd_cols
    kqd_pctr = resolve(kqd_cols or [], ["hist_predicted_ctr"],
                       "kqd", "pred-CTR bucket", required=False) if kqd_ok else None
    if kqd_pctr:
        vals = [r[0] for r in con.execute(
            f"SELECT DISTINCT {kqd_pctr} FROM keyword_quality_daily "
            f"LIMIT 20")]
        log(f"[KQD] pred-CTR bucket values: {vals}")

    agp_ok = bool(agp_cols)
    if agp_ok:
        agp_date = resolve(agp_cols, ["date", "report_date"], "agp", "date")
        agp_is = resolve(agp_cols, ["search_impression_share",
                                    "impression_share", "search_is"],
                         "agp", "impression share", required=False)
        agp_lost = resolve(agp_cols, ["search_rank_lost_impression_share",
                                      "rank_lost_impression_share",
                                      "search_rank_lost_is"],
                           "agp", "lost-IS(rank)", required=False)
        ag_key = resolve(ag_cols, ["id", "ad_group_id"], "ad_groups", "key")
        log(f"[RESOLVE] agp date={agp_date}, IS={agp_is}, "
            f"lost_rank={agp_lost or 'ABSENT (reported as such)'}")
    else:
        agp_date = agp_is = agp_lost = ag_key = None
        log("[RESOLVE] ad_group_performance_daily ABSENT — S3 IS panel skipped")

    # ---- assignment tags ----
    book_of, meta = load_assignment(args.assignment, script_dir)
    if meta:
        log(f"\n[TAGS] assignment file: {meta[0]} ({len(book_of):,} "
            f"text x match tags, {meta[1]} conflicts)")
        log("[TAGS] note: if the final tag run is not yet frozen, the frozen "
            "run supersedes these exhibits.")
    else:
        log("\n[TAGS] NO keyword_book_assignment_*.csv FOUND — entire run "
            "uses fallback token rules; the frozen tag run supersedes.")

    # ---- instance classification ----
    t0 = time.time()
    inst = {}
    camp_short = {v: k for k, v in CAMPAIGN_IDS.items()}
    fb_count = ov_count = tot = 0
    for r in con.execute(
            "SELECT id, campaign_id, LOWER(TRIM(keyword_text)) kw, "
            "UPPER(TRIM(match_type)) mt FROM keyword_instances "
            f"WHERE account_id={ACCOUNT_ID}"):
        tot += 1
        kw = r["kw"] or ""
        bk = book_of.get((kw, r["mt"]))
        srcs = "assignment-csv"
        if bk is None:
            bk = fallback_book(kw)
            srcs = "fallback-rule"
            fb_count += 1
        if has_any(kw, OVERRIDE_ALL):
            bk = "BRAND-HOME" if has_any(kw, HOME_TOKENS) else "FLAGSHIP"
            srcs = "brand-override(9/2)"
            ov_count += 1
        cid = r["campaign_id"]
        short = camp_short.get(int(cid) if cid is not None else -1)
        ledger = LEDGER.get(short, "LEGACY")
        inst[r["id"]] = (bk, short or "LEGACY", ledger, srcs, kw, r["mt"] or "")
    log(f"[TAGS] {tot:,} instances classified; fallback-tagged "
        f"{fb_count:,} ({fb_count / max(tot, 1):.0%}) — legacy/killed rows "
        f"outside the assignment file  [{time.time() - t0:.1f}s]")
    log(f"[OVERRIDE] 9/2 rulings: {ov_count:,} instances reclassified to "
        f"BRAND books (home token -> BRAND-HOME, else FLAGSHIP). "
        f"Ruling #1 aggregators: {OVERRIDE_BRAND_TOKENS}. Ruling #2 "
        f"regional carriers (collision-tightened tokens vs Sterling "
        f"Heights/Cambridge MA/Merrimack NH/Zephyrhills/Cigna): "
        f"{OVERRIDE_CARRIER_TOKENS}. Diverges from the 8/27 assignment "
        f"run — relay both to keyword_book_assignment.py (aggregators as "
        f"override policy; carriers into CARRIER_TOKENS). Remaining "
        f"aggregator tokens (zebra, insurify, smartfinancial, quotewizard, "
        f"gabi, way.com, everquote) stay GENERIC pending ruling.")

    # ---- kpd monthly per instance ----
    t0 = time.time()
    rev_sel = f", SUM(p.{kpd_rev}) rev" if kpd_rev else ", 0 rev"
    kmax = con.execute(
        f"SELECT MAX(p.{kpd_date}) FROM keyword_performance_daily p "
        f"JOIN keyword_instances k ON k.id=p.instance_id "
        f"WHERE k.account_id={ACCOUNT_ID}").fetchone()[0]
    end_month = kmax[:7]
    # cell[(ym, book, ledger, short)] = [impr, clicks, cost, leads, rev]
    cell = defaultdict(lambda: [0.0] * 5)
    kw_tot = defaultdict(lambda: [0.0] * 5)   # per-instance 2026 totals (audit sheet)
    kw_month = defaultdict(float)             # (spotlight kw, ym) -> impressions
    q = (f"SELECT SUBSTR(p.{kpd_date},1,7) ym, p.instance_id iid, "
         f"SUM(p.{kpd_impr}) im, SUM(p.{kpd_clicks}) ck, "
         f"SUM(p.{kpd_cost}) cm, SUM(p.{kpd_leads}) cv {rev_sel} "
         f"FROM keyword_performance_daily p "
         f"WHERE p.device='ALL' AND SUBSTR(p.{kpd_date},1,7) >= ? "
         f"GROUP BY 1,2")
    n = 0
    for r in con.execute(q, (START,)):
        tag = inst.get(r["iid"])
        if tag is None:
            continue
        bk, short, ledger, _, _, _ = tag
        c = cell[(r["ym"], bk, ledger, short)]
        c[0] += r["im"] or 0
        c[1] += r["ck"] or 0
        c[2] += (r["cm"] or 0) / (1e6 if kpd_cost == "cost_micros" else 1.0)
        c[3] += r["cv"] or 0
        c[4] += r["rev"] or 0
        if r["ym"] >= "2026-01":
            kwtxt, bk0 = tag[4], tag[0]
            if bk0 in ("FLAGSHIP", "BRAND-HOME"):
                for tok in CARRIER_SPOTLIGHT:
                    if tok in kwtxt:
                        kw_month[(tok, bk0, r["ym"])] += r["im"] or 0
            kk = kw_tot[r["iid"]]
            kk[0] += r["im"] or 0
            kk[1] += r["ck"] or 0
            kk[2] += (r["cm"] or 0) / (1e6 if kpd_cost == "cost_micros" else 1.0)
            kk[3] += r["cv"] or 0
            kk[4] += r["rev"] or 0
        n += 1
    log(f"[KPD] {n:,} instance-months aggregated into {len(cell):,} cells "
        f"(thru {kmax})  [{time.time() - t0:.1f}s]")
    log(f"[WINDOW] account-wide series: {START}-01 -> {kmax}; class x ledger "
        f"exhibits (S1/charts/audit): 2026-01-01 -> {kmax}; final month "
        f"partial.")

    # ---- QS layer per (ym, book, ledger) ----
    qs = defaultdict(lambda: [0.0, 0.0, 0.0, 0.0, 0.0])
    # [qs*impr, rated_impr, below_impr, above_impr, total_impr]
    if kqd_ok:
        t0 = time.time()
        below_expr = (f"SUM(CASE WHEN UPPER(q.{kqd_pctr}) LIKE '%BELOW%' "
                      f"THEN p.{kpd_impr} ELSE 0 END)") if kqd_pctr else "0"
        above_expr = (f"SUM(CASE WHEN UPPER(q.{kqd_pctr}) LIKE '%ABOVE%' "
                      f"THEN p.{kpd_impr} ELSE 0 END)") if kqd_pctr else "0"
        qq = (f"SELECT SUBSTR(p.{kpd_date},1,7) ym, p.instance_id iid, "
              f"SUM(q.historical_quality_score * p.{kpd_impr}) qi, "
              f"SUM(p.{kpd_impr}) ri, {below_expr} bi, {above_expr} ai "
              f"FROM keyword_performance_daily p "
              f"JOIN keyword_quality_daily q "
              f"ON q.instance_id = p.instance_id "
              f"AND q.report_date = p.{kpd_date} "
              f"WHERE p.device='ALL' AND SUBSTR(p.{kpd_date},1,7) >= ? "
              f"GROUP BY 1,2")
        for r in con.execute(qq, (START,)):
            tag = inst.get(r["iid"])
            if tag is None:
                continue
            bk, short, ledger = tag[0], tag[1], tag[2]
            a = qs[(r["ym"], bk, ledger)]
            a[0] += r["qi"] or 0
            a[1] += r["ri"] or 0
            a[2] += r["bi"] or 0
            a[3] += r["ai"] or 0
        # total impressions for rated_share denominators
        for (ym, bk, ledger, short), v in cell.items():
            qs[(ym, bk, ledger)][4] += v[0]
        log(f"[KQD] QS layer joined at daily grain  [{time.time() - t0:.1f}s]")

    months = [m for m in months_between(START, end_month)]

    def agg(ym, bk=None, ledger=None, short=None):
        out = [0.0] * 5
        for (y, b, lg, s), v in cell.items():
            if y != ym:
                continue
            if bk and b != bk:
                continue
            if ledger and lg != ledger:
                continue
            if short and s != short:
                continue
            for i in range(5):
                out[i] += v[i]
        return out

    def qsv(ym, bk=None, ledger=None):
        a = [0.0] * 5
        for (y, b, lg), v in qs.items():
            if y != ym:
                continue
            if bk and b != bk:
                continue
            if ledger and lg != ledger:
                continue
            for i in range(5):
                a[i] += v[i]
        iwqs = a[0] / a[1] if a[1] else None
        rated = a[1] / a[4] if a[4] else None
        below = a[2] / a[1] if a[1] else None
        above = a[3] / a[1] if a[1] else None
        # raw components: rated_impr, below_impr, above_impr
        return iwqs, rated, below, above, a[1], a[2], a[3]

    # ---- ANCHOR RECONCILIATION (A2/A4, kpd grain) ----
    log("\n" + "=" * 78)
    log("ANCHOR RECONCILIATION — book(BRAND-HOME)+book(GENERIC) leads vs "
        "Analysis-2/4 home-intent anchors (kpd grain, +/-10%)")
    log("=" * 78)
    anchors_ok = True
    for short, ym, target in ANCHORS:
        got = (agg(ym, "BRAND-HOME", None, short)[3]
               + agg(ym, "GENERIC", None, short)[3])
        pct = got / target - 1
        ok = abs(pct) <= ANCHOR_TOL
        anchors_ok &= ok
        log(f"  {short} {ym}: {got:,.1f} vs anchor {target:,.0f} "
            f"({pct:+.1%}) {'PASS' if ok else 'FAIL'}")
    if not anchors_ok:
        die("anchor reconciliation failed — book classes do not reproduce "
            "the ruled home-intent definition; fix tags before exhibits.")

    # ---- rated-share guard survey ----
    if kqd_ok:
        for ym in months[-3:]:
            rated = qsv(ym)[1]
            if rated is not None and rated < RATED_GUARD:
                log(f"[GUARD] {ym} account rated_share {rated:.2f} < "
                    f"{RATED_GUARD} — QS/CTR reads for this month are "
                    f"composition-suspect (A4 Aug collapse pattern)")

    if args.smoke:
        log("\nSMOKE COMPLETE — synthetic gate, column resolution, tags, "
            "anchors, guards above. Run full when clean.")
        return

    # =================================================================
    # S1 — THE 2x2 (class x ledger), monthly 2026
    # =================================================================
    log("\n" + "=" * 78)
    log("S1 — CLASS x LEDGER 2x2 (2026, INTACT=TX vs RESET=tiers)")
    log("=" * 78)
    s1_rows = []
    m2026 = [m for m in months if m >= "2026-01"]
    for bk in ("FLAGSHIP", "BRAND-HOME", "GENERIC"):
        for ledger in ("INTACT", "RESET", "SINGLE-OTHER"):
            for ym in m2026:
                im, ck, cost, leads, rev = agg(ym, bk, ledger)
                if im < 100:
                    continue
                (iwqs, rated, below, above, rated_i, below_i, above_i) = (
                    qsv(ym, bk, ledger) if kqd_ok else (None,) * 7)
                s1_rows.append({
                    "book": bk, "ledger": ledger, "month": ym,
                    "impr": im, "ctr": ck / im if im else None,
                    "cpc": cost / ck if ck else None,
                    "iwqs": iwqs, "rated": rated, "below": below,
                    "above": above, "rated_i": rated_i,
                    "below_i": below_i, "above_i": above_i,
                    "roas": (rev / cost) if (cost and kpd_rev) else None,
                    "leads": leads,
                    "cpl": cost / leads if leads else None})
    # interaction statement, latest full month
    last_full = [m for m in m2026 if m < end_month][-1]

    def pick(bk, lg, ym):
        for r in s1_rows:
            if r["book"] == bk and r["ledger"] == lg and r["month"] == ym:
                return r
        return None

    hi_i = [pick(b, "INTACT", last_full) for b in ("BRAND-HOME", "GENERIC")]
    hi_r = [pick(b, "RESET", last_full) for b in ("BRAND-HOME", "GENERIC")]
    fl_i, fl_r = pick("FLAGSHIP", "INTACT", last_full), pick(
        "FLAGSHIP", "RESET", last_full)

    def cmb(rows):
        rows = [r for r in rows if r]
        im = sum(r["impr"] for r in rows)
        ck = sum(r["ctr"] * r["impr"] for r in rows)
        return (ck / im if im else None), im

    hi_ctr_i, _ = cmb(hi_i)
    hi_ctr_r, _ = cmb(hi_r)
    log(f"\n[{last_full}] CTR — HOME-INTENT: INTACT "
        f"{hi_ctr_i:.1%}" if hi_ctr_i else "n/a")
    interaction_txt = []
    if hi_ctr_i and hi_ctr_r and fl_i and fl_r:
        inter = (hi_ctr_i - hi_ctr_r) - (fl_i["ctr"] - fl_r["ctr"])
        interaction_txt.append(
            f"CTR interaction ({last_full}): home-intent INTACT-minus-RESET "
            f"= {hi_ctr_i - hi_ctr_r:+.1%}; flagship INTACT-minus-RESET = "
            f"{fl_i['ctr'] - fl_r['ctr']:+.1%}; interaction = {inter:+.1%} "
            f"(positive = the ledger penalty falls on home-intent, the "
            f"charter's prediction)")
        log("  " + interaction_txt[-1])
    for r in [x for x in s1_rows if x["month"] == last_full]:
        log(f"  {r['book']:11}{r['ledger']:13} impr {r['impr']:>10,.0f}  "
            f"CTR {r['ctr']:.1%}  CPC {r['cpc'] or 0:6.2f}  "
            f"iwQS {r['iwqs'] or 0:4.1f} (rated {r['rated'] or 0:.2f}, "
            f"below {r['below'] or 0:.0%})  "
            + (f"ROAS {r['roas']:.2f}" if r["roas"] else ""))

    # =================================================================
    # S2 — CTR SHIFT-SHARE (account-wide + per campaign)
    # =================================================================
    log("\n" + "=" * 78)
    log("S2 — CTR SHIFT-SHARE (Laspeyres; cells = book x ledger-group "
        "account-wide, book per campaign)")
    log("=" * 78)

    def cells_for(ym, short=None):
        out = {}
        for (y, b, lg, s), v in cell.items():
            if y != ym:
                continue
            if short and s != short:
                continue
            k = b if short else (b, lg)
            c = out.setdefault(k, [0.0, 0.0])
            c[0] += v[0]
            c[1] += v[1]
        return {k: tuple(v) for k, v in out.items()}

    s2 = {}
    for base_ym, label in [("2025-01", "vs 2025-01"), ("2026-03", "vs 2026-03")]:
        base = cells_for(base_ym)
        rows = []
        for ym in months:
            if ym < base_ym:
                continue
            d = decompose_ctr(base, cells_for(ym))
            if d:
                rows.append({"month": ym, **d})
        s2[label] = rows
        log(f"\nACCOUNT {label}:  (delta = MIX + WITHIN + INTER)")
        log(f"{'month':9}{'CTR':>7} {'delta':>8} {'MIX':>8} {'WITHIN':>8} "
            f"{'INTER':>8}")
        for r in rows:
            log(f"{r['month']:9}{r['ctr1']:7.1%} {r['delta']:+8.2%} "
                f"{r['mix']:+8.2%} {r['within']:+8.2%} "
                f"{r['interaction']:+8.2%}")

    s2_camp = {}
    for short in ("TEXAS", "TIER1", "TIER2", "TIER3", "TIER4"):
        base = cells_for("2026-03", short)
        rows = []
        for ym in [m for m in months if m >= "2026-03"]:
            d = decompose_ctr(base, cells_for(ym, short))
            if d:
                rows.append({"month": ym, **d})
        s2_camp[short] = rows
        if rows:
            r = rows[-1]
            log(f"\n{short} vs 2026-03, latest ({r['month']}): "
                f"delta {r['delta']:+.2%} = MIX {r['mix']:+.2%} + WITHIN "
                f"{r['within']:+.2%} + INTER {r['interaction']:+.2%}")

    # =================================================================
    # S3 — ELIGIBILITY PANEL
    # =================================================================
    log("\n" + "=" * 78)
    log("S3 — ELIGIBILITY: home-intent impressions, IS (iw proxy), "
        "lost-IS(rank), CPC/CPL — 'ranked out, not priced out'")
    log("=" * 78)
    s3 = defaultdict(dict)
    if agp_ok and agp_is:
        lost_sel = (f", SUM(a.{agp_lost} * a.impressions) li"
                    if agp_lost else ", NULL li")
        dev_where = "AND a.device='ALL'" if "device" in agp_cols else ""
        qq = (f"SELECT SUBSTR(a.{agp_date},1,7) ym, g.campaign_id cid, "
              f"SUM(a.impressions) im, SUM(a.{agp_is} * a.impressions) si "
              f"{lost_sel} FROM ad_group_performance_daily a "
              f"JOIN ad_groups g ON g.{ag_key} = a.ad_group_id "
              f"WHERE SUBSTR(a.{agp_date},1,7) >= ? {dev_where} "
              f"GROUP BY 1,2")
        try:
            for r in con.execute(qq, (START,)):
                short = camp_short.get(int(r["cid"]) if r["cid"] else -1)
                if not short:
                    continue
                im = r["im"] or 0
                if not im:
                    continue
                s3[short][r["ym"]] = {
                    "is": (r["si"] or 0) / im,
                    "lost_rank": ((r["li"] or 0) / im) if agp_lost else None,
                    "eligible": im / ((r["si"] or 0) / im)
                    if (r["si"] or 0) > 0 else None}
        except sqlite3.Error as e:
            log(f"[S3] agp pull failed ({e}) — IS panel omitted; "
                f"impressions/CPC/CPL still shown")
    for short in ("TEXAS", "TIER1", "TIER2", "TIER3", "TIER4"):
        line = f"  {short:7}"
        for ym in [m for m in months if m >= "2026-01"]:
            hi = [agg(ym, b, None, short) for b in ("BRAND-HOME", "GENERIC")]
            im = sum(h[0] for h in hi)
            isv = s3[short].get(ym, {}).get("is")
            line += (f" {ym[-2:]}:{im / 1000:.0f}K"
                     + (f"/{isv:.0%}" if isv is not None else ""))
        log(line + "   (HI impr / campaign IS proxy)")

    # =================================================================
    # WORKBOOK
    # =================================================================
    import xlsxwriter
    wb = xlsxwriter.Workbook(args.out, {"nan_inf_to_errors": True})
    f_hdr = wb.add_format({"bold": True, "bg_color": "#1F3864",
                           "font_color": "white", "border": 1})
    f_bold = wb.add_format({"bold": True})
    f_mono = wb.add_format({"font_name": "Consolas", "font_size": 9})
    f_pct = wb.add_format({"num_format": "0.0%"})
    f_num = wb.add_format({"num_format": "#,##0"})
    f_cur = wb.add_format({"num_format": "$#,##0.00"})
    f_d1 = wb.add_format({"num_format": "0.0"})

    ws = wb.add_worksheet("00_Header")
    ws.set_column(0, 0, 110)
    for i, line in enumerate(HEADER):
        ws.write_string(i, 0, line, f_mono)   # write_string: '='-prefixed
        # separator lines must not be interpreted as formulas

    ws = wb.add_worksheet("S1_2x2")
    ws.write(0, 0, "pCTR columns are Google's own daily hist_predicted_ctr "
                   "label (ABOVE/AVERAGE/BELOW_AVERAGE) from "
                   "keyword_quality_daily, impression-weighted: pCTR_below = "
                   "below_impr / rated_impr. Raw components exposed in the "
                   "last four columns.", f_bold)
    ws.write_string(1, 0, f"Data window: 2026-01-01 -> {kmax} "
                    f"(source: keyword_performance_daily + "
                    f"keyword_quality_daily; final month partial)", f_bold)
    hdrs = ["book", "ledger", "month", "impressions", "CTR", "CPC",
            "iw_QS", "rated_share", "pCTR_below", "ROAS", "leads", "CPL",
            "rated_impr", "below_impr", "above_impr", "pCTR_above"]
    for c, h in enumerate(hdrs):
        ws.write(2, c, h, f_hdr)
    for i, r in enumerate(s1_rows):
        ws.write(3 + i, 0, r["book"])
        ws.write(3 + i, 1, r["ledger"])
        ws.write(3 + i, 2, r["month"])
        ws.write_number(3 + i, 3, r["impr"], f_num)
        for c, k, fmt in [(4, "ctr", f_pct), (5, "cpc", f_cur),
                          (6, "iwqs", f_d1), (7, "rated", f_pct),
                          (8, "below", f_pct), (9, "roas", f_d1),
                          (10, "leads", f_num), (11, "cpl", f_cur),
                          (12, "rated_i", f_num), (13, "below_i", f_num),
                          (14, "above_i", f_num), (15, "above", f_pct)]:
            if r.get(k) is not None:
                ws.write_number(3 + i, c, r[k], fmt)
    ws.autofilter(2, 0, len(s1_rows) + 2, len(hdrs) - 1)
    ws.freeze_panes(3, 0)
    for j, t in enumerate(interaction_txt):
        ws.write(len(s1_rows) + 5 + j, 0, t, f_bold)

    # ---- S1_Charts: monthly pivots + line charts per metric ----
    wsc = wb.add_worksheet("S1_Charts")
    wsc.write(0, 0, "Time series behind S1 — one line per book x ledger "
                    "(INTACT=TX, RESET=Tier1-4). Data table below each "
                    "chart block.", f_bold)
    wsc.write_string(1, 0, f"Data window: 2026-01-01 -> {kmax} "
                     f"(final month partial)", f_bold)
    series_keys = [("FLAGSHIP", "INTACT"), ("FLAGSHIP", "RESET"),
                   ("BRAND-HOME", "INTACT"), ("BRAND-HOME", "RESET"),
                   ("GENERIC", "INTACT"), ("GENERIC", "RESET")]
    chart_months = sorted({r["month"] for r in s1_rows})
    lut = {(r["book"], r["ledger"], r["month"]): r for r in s1_rows}
    block_row = 2
    for metric, mfmt, title in [("ctr", f_pct, "CTR by class x ledger"),
                                ("iwqs", f_d1, "iw_QS by class x ledger"),
                                ("below", f_pct,
                                 "pCTR-below share by class x ledger"),
                                ("impr", f_num,
                                 "Impressions by class x ledger")]:
        wsc.write(block_row, 0, "month", f_hdr)
        for c, (bk, lg) in enumerate(series_keys):
            wsc.write(block_row, 1 + c, f"{bk} {lg}", f_hdr)
        for ri, ym in enumerate(chart_months):
            wsc.write(block_row + 1 + ri, 0, ym)
            for c, (bk, lg) in enumerate(series_keys):
                r = lut.get((bk, lg, ym))
                if r and r.get(metric) is not None:
                    wsc.write_number(block_row + 1 + ri, 1 + c,
                                     r[metric], mfmt)
        ch = wb.add_chart({"type": "line"})
        for c in range(len(series_keys)):
            ch.add_series({
                "name": ["S1_Charts", block_row, 1 + c],
                "categories": ["S1_Charts", block_row + 1, 0,
                               block_row + len(chart_months), 0],
                "values": ["S1_Charts", block_row + 1, 1 + c,
                           block_row + len(chart_months), 1 + c],
                "marker": {"type": "circle", "size": 5}})
        ch.set_title({"name": title})
        if metric in ("ctr", "below"):
            ch.set_y_axis({"num_format": "0%"})
        ch.set_size({"width": 700, "height": 340})
        wsc.insert_chart(block_row, 9, ch)
        block_row += len(chart_months) + 20

    # ---- S1_Headline: the deck visual — same-campaign class divergence ----
    wsh = wb.add_worksheet("S1_Headline")
    wsh.write(0, 0, "THE DECK VISUAL — same Texas campaign, two keyword "
                    "classes, Google's own verdict. This is Google's daily "
                    "expected-CTR label (keyword_quality_daily."
                    "hist_predicted_ctr), impression-weighted: % of rated "
                    "impressions served under a BELOW_AVERAGE label. We "
                    "compute nothing — same campaign, same ledger, same "
                    "geography, same months; the only variable is the "
                    "keyword class.", f_bold)
    wsh.write(1, 0, "Footnote to say aloud: flagship's RAW CTR is healthy "
                    "all year (right column) — it is graded on the brand "
                    "owner's curve, so the penalty is invisible in normal "
                    "reporting. Generic has held 0% below-average since "
                    "April even in the intact shell; the RESET series on "
                    "S1_Charts shows the same class shape in the rebuilt "
                    "tiers (class-shaped, ledger-independent).", f_bold)
    hh = ["month", "FLAGSHIP pCTR-below", "GENERIC pCTR-below",
          "FLAGSHIP raw CTR", "GENERIC raw CTR"]
    for c, h in enumerate(hh):
        wsh.write(3, c, h, f_hdr)
    for ri, ym in enumerate(chart_months):
        wsh.write_string(4 + ri, 0, ym)
        fl = lut.get(("FLAGSHIP", "INTACT", ym))
        gn = lut.get(("GENERIC", "INTACT", ym))
        for c, r0, k, fmt in [(1, fl, "below", f_pct), (2, gn, "below",
                                                        f_pct),
                              (3, fl, "ctr", f_pct), (4, gn, "ctr",
                                                      f_pct)]:
            if r0 and r0.get(k) is not None:
                wsh.write_number(4 + ri, c, r0[k], fmt)
    nm_ = len(chart_months)
    chh = wb.add_chart({"type": "line"})
    for col, nm2 in [(1, "FLAGSHIP (carrier-brand terms)"),
                     (2, "GENERIC (home-intent terms)")]:
        chh.add_series({"name": nm2,
                        "categories": ["S1_Headline", 4, 0, 3 + nm_, 0],
                        "values": ["S1_Headline", 4, col, 3 + nm_, col],
                        "marker": {"type": "circle", "size": 7},
                        "line": {"width": 2.5}})
    chh.set_title({"name": "Same Texas campaign, two keyword classes — % "
                           "of impressions Google rates below-average "
                           "expected CTR"})
    chh.set_y_axis({"num_format": "0%", "min": 0, "max": 1})
    chh.set_size({"width": 760, "height": 440})
    wsh.insert_chart(3, 6, chh)

    # ---- S8_Spotlight: same carrier, two intent forms ----
    ws8 = wb.add_worksheet("S8_Spotlight")
    ws8.write(0, 0, "SAME CARRIER, TWO INTENT FORMS — monthly impressions "
                    "indexed to March = 100. For each carrier name, ALL "
                    "keywords containing it, split by book: bare-brand "
                    "forms ('geico') = FLAGSHIP vs brand+home forms "
                    "('geico home insurance') = BRAND-HOME. Same brand, "
                    "same advertiser demand, same consumer — the only "
                    "difference is the intent form of the query, which is "
                    "exactly the book boundary. If the bare form loses "
                    "serving harder than the home form within the SAME "
                    "carrier, the loss is intent-form-shaped.", f_bold)
    series8 = [(tok, bk) for tok in CARRIER_SPOTLIGHT
               for bk in ("FLAGSHIP", "BRAND-HOME")]
    n_s8 = len(series8)
    hdr8 = (["month"]
            + [f"{t} {b}" for t, b in series8]
            + [f"{t} {b} idx" for t, b in series8])
    for c, h in enumerate(hdr8):
        ws8.write(2, c, h, f_hdr)
    base8 = {(t, b): kw_month.get((t, b, "2026-03"), 0.0)
             for t, b in series8}
    for ri, ym in enumerate(sorted({m for m in chart_months})):
        x = 3 + ri
        ws8.write_string(x, 0, ym)
        for c, (t, b) in enumerate(series8):
            v = kw_month.get((t, b, ym), 0.0)
            ws8.write_number(x, 1 + c, v, f_num)
            bb = base8.get((t, b))
            if bb:
                ws8.write_number(x, 1 + n_s8 + c, 100 * v / bb, f_d1)
    nmn = len(chart_months)
    for ti, tok in enumerate(CARRIER_SPOTLIGHT):
        ch8 = wb.add_chart({"type": "line"})
        for c, (t, b) in enumerate(series8):
            if t != tok:
                continue
            ch8.add_series({
                "name": f"{t} — {'bare brand (FLAGSHIP)' if b == 'FLAGSHIP' else 'brand+home (BRAND-HOME)'}",
                "categories": ["S8_Spotlight", 3, 0, 2 + nmn, 0],
                "values": ["S8_Spotlight", 3, 1 + n_s8 + c,
                           2 + nmn, 1 + n_s8 + c],
                "marker": {"type": "circle", "size": 6},
                "line": {"width": 2.5}})
        ch8.set_title({"name": f"'{tok}' keywords: bare-brand vs "
                               f"brand+home impressions, Mar = 100"})
        ch8.set_y_axis({"name": "impressions index"})
        ch8.set_size({"width": 640, "height": 380})
        ws8.insert_chart(3 + ti * 21, 11, ch8)

    # ---- S9_tROAS_Check: the "did the algorithm retreat?" rebuttal ----
    ws9 = wb.add_worksheet("S9_tROAS_Check")
    ws9.write(0, 0, "OBJECTION CHECK — 'if CVR fell, tROAS lowered bids to "
                    "protect the target, so the retreat was the "
                    "algorithm's.' Test: HOME-INTENT (GENERIC + BRAND-HOME) "
                    "clicks vs CVR vs CPC, indexed March = 100. A "
                    "target-protecting bid-down requires a large CVR "
                    "decline and shows up as falling CPC. Read the three "
                    "lines: if clicks collapse while CVR and CPC hold, "
                    "bids were essentially held and value inputs were "
                    "roughly flat — the volume left through the QUALITY "
                    "side of Ad Rank (see S1_Headline), not the bid side.",
             f_bold)
    ws9.write(1, 0, "Standing facts (UI-verified by DB architect, all "
                    "eras incl. 2024 peak): Search impr. share reads "
                    "'<10%' and Lost IS (rank) '>90%' — both censored "
                    "bounds, never usable numbers, in EVERY era; Lost IS "
                    "(budget) is real and ~0% in every era — the account "
                    "has never been budget-bound, always rank-bound: "
                    "Google was always willing to serve more if Ad Rank "
                    "permitted.", f_bold)
    ws9.write(2, 0, "params:", f_bold)
    ws9.write_string(2, 1, "GENERIC")
    ws9.write_string(2, 2, "BRAND-HOME")
    ws9.write_string(2, 3, "2026-03")
    B1, B2v, BM = "$B$3", "$C$3", "$D$3"
    hdr9 = ["month", "HI_clicks", "HI_leads", "HI_cost", "HI_CVR",
            "HI_CPC", "clicks_idx", "CVR_idx", "CPC_idx"]
    for c, h in enumerate(hdr9):
        ws9.write(4, c, h, f_hdr)

    def s9(valcol, month_ref, book_ref):
        return (f"SUMIFS(Data_ClassMonthly!${valcol}:${valcol},"
                f"Data_ClassMonthly!$B:$B,{month_ref},"
                f"Data_ClassMonthly!$C:$C,{book_ref})")

    def s9hi(valcol, month_ref):
        return f"({s9(valcol, month_ref, B1)}+{s9(valcol, month_ref, B2v)})"

    months9 = sorted({m for m in chart_months})
    for ri, ym in enumerate(months9):
        x = 5 + ri
        xx = x + 1
        ws9.write_string(x, 0, ym)
        mr = f"$A{xx}"
        ws9.write_formula(x, 1, f"={s9hi('F', mr)}", f_num)
        ws9.write_formula(x, 2, f"={s9hi('H', mr)}", f_num)
        ws9.write_formula(x, 3, f"={s9hi('G', mr)}", f_cur)
        ws9.write_formula(x, 4, f"=IFERROR($C{xx}/$B{xx},\"\")", f_pct)
        ws9.write_formula(x, 5, f"=IFERROR($D{xx}/$B{xx},\"\")", f_cur)
        ws9.write_formula(x, 6, f"=IFERROR(100*$B{xx}/{s9hi('F', BM)},"
                                f"\"\")", f_d1)
        ws9.write_formula(x, 7, f"=IFERROR(100*($C{xx}/$B{xx})/"
                                f"({s9hi('H', BM)}/{s9hi('F', BM)}),\"\")",
                          f_d1)
        ws9.write_formula(x, 8, f"=IFERROR(100*($D{xx}/$B{xx})/"
                                f"({s9hi('G', BM)}/{s9hi('F', BM)}),\"\")",
                          f_d1)
    n9 = len(months9)
    ch9 = wb.add_chart({"type": "line"})
    for col, nm9 in [(6, "HI clicks (volume)"), (7, "HI CVR (click->lead)"),
                     (8, "HI CPC (bid proxy)")]:
        ch9.add_series({"name": nm9,
                        "categories": ["S9_tROAS_Check", 5, 0, 4 + n9, 0],
                        "values": ["S9_tROAS_Check", 5, col, 4 + n9, col],
                        "marker": {"type": "circle", "size": 6},
                        "line": {"width": 2.5}})
    ch9.set_title({"name": "Home-intent: clicks vs CVR vs CPC, "
                           "March = 100 — the volume left; the bid and "
                           "value inputs didn't"})
    ch9.set_y_axis({"name": "index (Mar=100)"})
    ch9.set_size({"width": 700, "height": 420})
    ws9.insert_chart(4, 10, ch9)

    def s2_sheet(name, rows_by_label):
        ws = wb.add_worksheet(name)
        ws.write_string(0, 0, f"Data window: {START}-01 -> {kmax} for the "
                        f"2025-01 baseline; 2026-03-01 -> {kmax} for the "
                        f"2026-03 baseline (final month partial)", f_bold)
        row = 2
        for label, rows in rows_by_label.items():
            ws.write(row, 0, str(label), f_bold)
            row += 1
            for c, h in enumerate(["month", "CTR", "delta", "MIX",
                                   "WITHIN", "INTER"]):
                ws.write(row, c, h, f_hdr)
            row += 1
            first = row
            for r in rows:
                ws.write(row, 0, r["month"])
                for c, k in enumerate(["ctr1", "delta", "mix", "within",
                                       "interaction"], start=1):
                    ws.write_number(row, c, r[k], f_pct)
                row += 1
            if rows and name == "S2_Account":
                ch = wb.add_chart({"type": "column", "subtype": "stacked"})
                for c, nm in [(3, "MIX"), (4, "WITHIN"), (5, "INTER")]:
                    ch.add_series({
                        "name": nm,
                        "categories": [name, first, 0, row - 1, 0],
                        "values": [name, first, c, row - 1, c]})
                ch.set_title({"name": f"CTR delta {label} by channel"})
                ch.set_size({"width": 560, "height": 320})
                ws.insert_chart(first - 2, 8, ch)
            row += 2
    s2_sheet("S2_Account", s2)
    s2_sheet("S2_PerCampaign", s2_camp)

    ws = wb.add_worksheet("S3_Eligibility")
    ws.write(0, 0, "Eligibility panel — home-intent impressions, campaign "
                   "IS proxy, CPC/CPL.", f_bold)
    ws.write_string(1, 0, f"Data window: {START}-01 -> {kmax} "
                    f"(final month partial)", f_bold)
    hdrs = ["campaign", "month", "HI_impressions", "campaign_IS_proxy",
            "lost_IS_rank", "eligible_proxy", "HI_CPC", "HI_CPL"]
    for c, h in enumerate(hdrs):
        ws.write(2, c, h, f_hdr)
    ri = 3
    for short in ("TEXAS", "TIER1", "TIER2", "TIER3", "TIER4"):
        for ym in [m for m in months if m >= "2025-01"]:
            hi = [agg(ym, b, None, short) for b in ("BRAND-HOME", "GENERIC")]
            im = sum(h[0] for h in hi)
            ck = sum(h[1] for h in hi)
            cost = sum(h[2] for h in hi)
            leads = sum(h[3] for h in hi)
            if im < 100:
                continue
            d = s3[short].get(ym, {})
            ws.write(ri, 0, short)
            ws.write(ri, 1, ym)
            ws.write_number(ri, 2, im, f_num)
            if d.get("is") is not None:
                ws.write_number(ri, 3, d["is"], f_pct)
            if d.get("lost_rank") is not None:
                ws.write_number(ri, 4, d["lost_rank"], f_pct)
            if d.get("eligible"):
                ws.write_number(ri, 5, d["eligible"], f_num)
            if ck:
                ws.write_number(ri, 6, cost / ck, f_cur)
            if leads:
                ws.write_number(ri, 7, cost / leads, f_cur)
            ri += 1
    ws.autofilter(2, 0, ri - 1, len(hdrs) - 1)
    ws.freeze_panes(3, 0)
    ws.write(ri + 2, 0, "Reset overlays: " + "; ".join(
        f"{d} = {t}" for d, t in RESET_EVENTS), f_bold)
    ws.write(ri + 3, 0, "IS is an impression-weighted PROXY (exact weight = "
             "eligible impressions); IS floor <10% censoring caveat applies "
             "(A4).", f_bold)

    ws = wb.add_worksheet("S4_Cost_Table")
    ws.write(0, 0, "WHAT MAINTAINING GEOGRAPHY COSTS AND WHAT IT NO LONGER "
                   "BUYS (assembled from verified analyses — no new math)",
             f_bold)
    ws.write_string(1, 0, f"Prepared from data through {kmax}", f_bold)
    rows4 = [
        ("Tier RPL erosion is composition, not value", "Tier 1 RPL -$17.55 "
         "Mar->Jul, MIX -$15.98 (91%); Tier 3 -$17.90, MIX -$14.01 (78%); "
         "vendor price contribution $0.00", "rpl_decomposition_v2 / 06_Tier_Lens"),
        ("Tier RPL bands compressed", "Mar tier spread $78-$43 ($35 wide) -> "
         "Jul $70-$41 ($29) with T1 no longer top — the tiers no longer "
         "rank-order value", "rpl_decomposition_v2 / 06_Tier_Lens"),
        ("Boundary fragility", "20 of 35 states shifted tiers in one "
         "clean-week re-read (7/22) — T3/T4 boundary is noise at current "
         "volumes", "geo re-tier thread"),
        ("Per-controller signal density", "~51.6 conv/day account across 7 "
         "campaign controllers (~7.4/day each); 3 books => ~17/day per "
         "controller (2.3x)", "keyword_health_scenario S0 (8/24)"),
        ("Structural resets tax the QS ledger", "Dec-25/Jan-26 cutover: "
         "home-intent iw_QS stepped 6.17->5.31 at cutover; ~2x Feb-Mar CPC "
         "re-entry toll; copy resets #2/#3 cost -43%/-41% CTR troughs",
         "Analysis 4 (staircase, confirmed)"),
        ("Geography does not shape the click loss", "S2 above: the CTR "
         "decline decomposes on CLASS cells; geo-cell RPL stayed "
         "cross-platform aligned (corr +0.97) while volume collapsed "
         "everywhere including never-touched TX (-75%)",
         "this workbook + cross_platform_state_rpl"),
        ("The keyword structure is already leaking authority", "AI Max "
         "matches ~1% of spend/leads OUTSIDE keyword attribution (steady "
         "daily drip, ~2% on T2; spiked $40-55/day in the 8/17 expansion "
         "week) — Google is increasingly serving around the structure the "
         "account maintains; a structure argued from keyword grain "
         "inherits this blind spot, and its growth is itself evidence",
         "AI Max attribution gap (8/27) + architect ruling 9/1"),
    ]
    for c, h in enumerate(["cost / finding", "evidence", "source"]):
        ws.write(2, c, h, f_hdr)
    for i, (a, b, c) in enumerate(rows4):
        ws.write(3 + i, 0, a, f_bold)
        ws.write(3 + i, 1, b)
        ws.write(3 + i, 2, c)
    ws.set_column(0, 0, 38)
    ws.set_column(1, 1, 80)
    ws.set_column(2, 2, 40)

    ws = wb.add_worksheet("S5_Mechanism_Map")
    ws.write(0, 0, "MECHANISM -> DESIGN FEATURE (each finding paired to the "
                   "book-map element that addresses it)", f_bold)
    ws.write_string(1, 0, f"Prepared from data through {kmax}", f_bold)
    rows5 = [
        ("Flagship pCTR-below/QS collapse is account-wide (both ledger "
         "states) and prices every shared campaign's Ad Rank",
         "FLAGSHIP isolated in its own geo-tiered campaigns with a "
         "species-selective damping cap — the low-QS franchise stops "
         "setting the shared shells' auction terms"),
        ("Home-intent ledgers are the account's healthiest asset and reset "
         "events are what destroyed value (December lesson)",
         "GENERIC becomes the surviving campaign BY SUBTRACTION — ids, "
         "history, and QS ledgers preserved; nothing re-enters auctions "
         "from zero"),
        ("BRAND-HOME rows are mostly dark with non-producing ledgers",
         "Combined nationwide BRAND-HOME campaign — nothing to lose; "
         "LEDGER-LOSS rows (healthy QS that must move) counted and stated "
         "up front"),
        ("Tier boundaries are noise and tier RPL no longer rank-orders "
         "value (band compression + 20/35 shifts)",
         "Geography demoted from campaign boundary to a tier layer inside "
         "FLAGSHIP only; home-intent runs nationwide where the CID prices "
         "state x device anyway (platform-blind price list)"),
        ("Seven controllers at ~7 conv/day starve smart bidding",
         "Three controllers at ~17 conv/day each — above signal starvation "
         "without loosening targets"),
        ("Structural migration is a QS-capital event with a measurable toll",
         "Migration moves ONLY flagship into new ids (ledgers 2-4, nothing "
         "to lose; fresh ledgers may re-rate); generic/carrier-home "
         "repurpose existing ids — the plan minimizes ledger destruction "
         "by construction"),
    ]
    for c, h in enumerate(["mechanism (finding)", "design feature (book map)"]):
        ws.write(2, c, h, f_hdr)
    for i, (a, b) in enumerate(rows5):
        ws.write(3 + i, 0, a)
        ws.write(3 + i, 1, b)
    ws.set_column(0, 0, 70)
    ws.set_column(1, 1, 70)

    ws = wb.add_worksheet("S6_Success_Metrics")
    ws.write(0, 0, "PRE-REGISTERED POST-CUTOVER SUCCESS METRICS "
                   "(committed before migration; read dates fixed; these "
                   "are the numbers that would FALSIFY the case)", f_bold)
    ws.write_string(1, 0, f"Prepared from data through {kmax}", f_bold)
    rows6 = [
        ("Baseline", "Trailing 28 days ending the day before cutover, per "
         "book x campaign: CTR, iw_QS + rated_share, campaign IS proxy, "
         "CPC, home-intent impressions/day. Frozen in the cutover note."),
        ("Read dates", "Cutover +14d (early read, directional only), +28d "
         "(first scored read), +56d (verdict read). No other dates count."),
        ("GENERIC book (ledgers preserved)", "iw_QS must NOT step down "
         ">0.3 at cutover (the December failure mode); CTR within 1pt of "
         "baseline by +28d; HI impressions/day >= baseline by +56d."),
        ("FLAGSHIP book (fresh ledgers)", "EXPECTED to open low "
         "(pCTR-below high) — success = monotone re-rating by +56d and "
         "shared-shell (GENERIC) iw_QS/CTR unharmed, proving the "
         "quarantine works."),
        ("Account", "Home-intent search IS proxy off the <10% floor OR "
         "lost-IS(rank) declining by +56d; CPC not up >15% vs baseline "
         "(the toll bound)."),
        ("Falsification", "If GENERIC iw_QS steps down like December, or "
         "HI impressions/day is below baseline at +56d with flagship "
         "damped, the structure case is wrong and we say so."),
    ]
    for i, (a, b) in enumerate(rows6):
        ws.write(2 + i, 0, a, f_bold)
        ws.write(2 + i, 1, b)
    ws.set_column(0, 0, 34)
    ws.set_column(1, 1, 100)

    # ---- Data_ClassMonthly + S7_Dose: flagship dose vs generic response ----
    wsc2 = wb.add_worksheet("Data_ClassMonthly")
    for c, h in enumerate(["campaign", "month", "book", "ledger",
                           "impressions", "clicks", "cost", "leads"]):
        wsc2.write(0, c, h, f_hdr)
    ri2 = 1
    for (ym, bk, ledger, short), v in sorted(cell.items()):
        if ym < "2026-01":
            continue
        wsc2.write_string(ri2, 0, short)
        wsc2.write_string(ri2, 1, ym)
        wsc2.write_string(ri2, 2, bk)
        wsc2.write_string(ri2, 3, ledger)
        wsc2.write_number(ri2, 4, v[0], f_num)
        wsc2.write_number(ri2, 5, v[1], f_num)
        wsc2.write_number(ri2, 6, v[2], f_cur)
        wsc2.write_number(ri2, 7, v[3], f_num)
        ri2 += 1
    wsc2.autofilter(0, 0, ri2 - 1, 7)
    wsc2.freeze_panes(1, 0)

    ws7 = wb.add_worksheet("S7_Dose")
    ws7.write(0, 0, "DOSE-RESPONSE (observational): does a higher FLAGSHIP "
                    "share of a campaign's spend coincide with worse "
                    "GENERIC outcomes in that same campaign-month? "
                    "Transmission predicts: negative slope on generic "
                    "volume, positive on generic CPC/CPL. A flat slope "
                    "WEAKENS the coupling claim — this exhibit is "
                    "falsifiable in both directions. CAVEAT (reverse "
                    "causality): flagship SHARE rises mechanically when "
                    "generic falls for any reason — read the "
                    "flag_cost_idx dose alongside share. This supports, "
                    "and never replaces, the S6 experiment.", f_bold)
    ws7.write(1, 0, "params:", f_bold)
    ws7.write_string(1, 1, "FLAGSHIP")
    ws7.write_string(1, 2, "GENERIC")
    ws7.write_string(1, 3, "2026-03")
    PF, PG, PBm = "$B$2", "$C$2", "$D$2"
    hdr7 = ["campaign", "month", "flag_cost", "campaign_cost",
            "flag_share", "flag_cost_idx", "gen_impr", "gen_impr_idx",
            "gen_CPC", "gen_CPL"]
    for c, h in enumerate(hdr7):
        ws7.write(3, c, h, f_hdr)

    def s7(valcol, camp_ref, month_ref, book_ref=None):
        f = (f"SUMIFS(Data_ClassMonthly!${valcol}:${valcol},"
             f"Data_ClassMonthly!$A:$A,{camp_ref},"
             f"Data_ClassMonthly!$B:$B,{month_ref}")
        if book_ref:
            f += f",Data_ClassMonthly!$C:$C,{book_ref}"
        return f + ")"

    pairs = sorted({(short, ym) for (ym, bk, ledger, short) in cell
                    if ym >= "2026-01" and short not in ("LEGACY",)})
    r7 = 4
    for short, ym in pairs:
        tot_cost = sum(v[2] for (y, b, lg, s), v in cell.items()
                       if y == ym and s == short)
        gen_impr = sum(v[0] for (y, b, lg, s), v in cell.items()
                       if y == ym and s == short and b == "GENERIC")
        if tot_cost <= 0 or gen_impr <= 0:
            continue
        x = r7 + 1
        cr, mr = f"$A{x}", f"$B{x}"
        ws7.write_string(r7, 0, short)
        ws7.write_string(r7, 1, ym)
        ws7.write_formula(r7, 2, f"={s7('G', cr, mr, PF)}", f_cur)
        ws7.write_formula(r7, 3, f"={s7('G', cr, mr)}", f_cur)
        ws7.write_formula(r7, 4, f"=IFERROR($C{x}/$D{x},\"\")", f_pct)
        ws7.write_formula(r7, 5, f"=IFERROR(100*$C{x}/"
                                 f"{s7('G', cr, PBm, PF)},\"\")", f_d1)
        ws7.write_formula(r7, 6, f"={s7('E', cr, mr, PG)}", f_num)
        ws7.write_formula(r7, 7, f"=IFERROR(100*$G{x}/"
                                 f"{s7('E', cr, PBm, PG)},\"\")", f_d1)
        ws7.write_formula(r7, 8, f"=IFERROR({s7('G', cr, mr, PG)}/"
                                 f"{s7('F', cr, mr, PG)},\"\")", f_cur)
        ws7.write_formula(r7, 9, f"=IFERROR({s7('G', cr, mr, PG)}/"
                                 f"{s7('H', cr, mr, PG)},\"\")", f_cur)
        r7 += 1
    n7 = r7 - 4
    cr0, cr1 = 5, r7
    f_corr = wb.add_format({"num_format": "+0.00", "bold": True})
    ws7.write(r7 + 1, 0, "CORREL(flag_share, gen_impr_idx):", f_bold)
    ws7.write_formula(r7 + 1, 2, f"=CORREL(E{cr0}:E{cr1},H{cr0}:H{cr1})",
                      f_corr)
    ws7.write(r7 + 2, 0, "CORREL(flag_share, gen_CPC):", f_bold)
    ws7.write_formula(r7 + 2, 2, f"=CORREL(E{cr0}:E{cr1},I{cr0}:I{cr1})",
                      f_corr)
    ws7.write(r7 + 3, 0, "CORREL(flag_cost_idx, gen_impr_idx):", f_bold)
    ws7.write_formula(r7 + 3, 2, f"=CORREL(F{cr0}:F{cr1},H{cr0}:H{cr1})",
                      f_corr)
    for anchor2, ycol, ttl, yname in [
            (3, 7, "Flagship spend share (x) vs generic volume index (y)",
             "generic impressions, own Mar=100"),
            (26, 8, "Flagship spend share (x) vs generic CPC (y)",
             "generic CPC ($)")]:
        chs = wb.add_chart({"type": "scatter"})
        chs.add_series({"name": "campaign-months",
                        "categories": ["S7_Dose", 4, 4, r7 - 1, 4],
                        "values": ["S7_Dose", 4, ycol, r7 - 1, ycol],
                        "marker": {"type": "circle", "size": 6}})
        chs.set_title({"name": ttl})
        chs.set_x_axis({"name": "flagship share of campaign spend",
                        "num_format": "0%"})
        chs.set_y_axis({"name": yname})
        chs.set_legend({"none": True})
        chs.set_size({"width": 620, "height": 360})
        ws7.insert_chart(anchor2, 11, chs)
    log(f"[S7] dose-response table: {n7} campaign-months; correlations "
        f"live in-sheet (CORREL cells under the table)")

    ws = wb.add_worksheet("Data_Keywords")
    ws.write(0, 0, "CLASSIFICATION AUDIT — every keyword with 2026 activity, "
                   "its book, and HOW it was tagged. Book rules: FLAGSHIP = "
                   "carrier-brand token, no home token; BRAND-HOME = carrier "
                   "token + home token; GENERIC = no carrier token. "
                   "tag_source: assignment-csv / fallback-rule / "
                   "brand-override(9/2). Challenge any row: the class "
                   "exhibits are just sums of these. "
                   f"Data window: 2026-01-01 -> {kmax} "
                   f"(final month partial).", f_bold)
    audit = defaultdict(lambda: [0.0] * 5)
    for iid, tot in kw_tot.items():
        if tot[0] <= 0:
            continue
        bk, short, ledger, srcs, kw, mt = inst[iid]
        key = (kw, mt, short, ledger, bk, srcs)
        a = audit[key]
        for i in range(5):
            a[i] += tot[i]
    # ---- summary block (frozen with the header): total + per-book ----
    book_sum = defaultdict(lambda: [0.0] * 5)
    for (kw, mt, short, ledger, bk, srcs), a in audit.items():
        for i in range(5):
            book_sum[bk][i] += a[i]
            book_sum["TOTAL"][i] += a[i]
    for c, h in enumerate(["book", "revenue_2026", "cost_2026", "ROAS",
                           "leads_2026", "CPL"]):
        ws.write(2, c, h, f_hdr)
    for j, bk in enumerate(["TOTAL", "FLAGSHIP", "BRAND-HOME", "GENERIC"]):
        s = book_sum[bk]
        ws.write(3 + j, 0, bk, f_bold)
        ws.write_number(3 + j, 1, s[4], f_cur)
        ws.write_number(3 + j, 2, s[2], f_cur)
        if s[2]:
            ws.write_number(3 + j, 3, s[4] / s[2], f_d1)
        if s[3] is not None:
            ws.write_number(3 + j, 4, s[3], f_num)
        if s[3]:
            ws.write_number(3 + j, 5, s[2] / s[3], f_cur)
    hdrs = ["keyword", "match", "campaign", "ledger", "book", "tag_source",
            "impr_2026", "clicks_2026", "CTR", "cost_2026", "leads_2026",
            "revenue_2026", "CPC", "CPL"]
    for c, h in enumerate(hdrs):
        ws.write(8, c, h, f_hdr)
    ri = 9
    for key, a in sorted(audit.items(), key=lambda x: -x[1][0]):
        kw, mt, short, ledger, bk, srcs = key
        ws.write_string(ri, 0, kw)
        ws.write(ri, 1, mt)
        ws.write(ri, 2, short)
        ws.write(ri, 3, ledger)
        ws.write(ri, 4, bk)
        ws.write(ri, 5, srcs)
        ws.write_number(ri, 6, a[0], f_num)
        ws.write_number(ri, 7, a[1], f_num)
        if a[0]:
            ws.write_number(ri, 8, a[1] / a[0], f_pct)
        ws.write_number(ri, 9, a[2], f_cur)
        ws.write_number(ri, 10, a[3], f_num)
        ws.write_number(ri, 11, a[4], f_cur)
        if a[1]:
            ws.write_number(ri, 12, a[2] / a[1], f_cur)
        if a[3]:
            ws.write_number(ri, 13, a[2] / a[3], f_cur)
        ri += 1
    ws.autofilter(8, 0, ri - 1, len(hdrs) - 1)
    ws.freeze_panes(9, 1)
    log(f"[AUDIT] Data_Keywords sheet: {ri - 9:,} keyword rows with 2026 "
        f"activity (sortable/filterable — the class exhibits are sums of "
        f"these rows)")

    wb.close()
    log(f"\n[DONE] workbook: {args.out}")


if __name__ == "__main__":
    main()
