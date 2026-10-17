#!/usr/bin/env python3
"""
rollout_plan.py -> rollout_plan.xlsx  (STAGE 1: gate, windows, K1, K2)

Execution ledger for the approved three-book restructure. PLANS changes,
executes nothing. Read-only DB. Consumes ONLY the frozen tag run:
  keyword_book_assignment_FROZEN_<YYYY-MM-DD>.csv
  first row:  # frozen: <date>, ruleset: 8/27+9/2a+9/2b+9/4-aggtail, row_count: N
Gate requires BOTH filename and metadata, and reconciles row_count.
--fixture bypasses the gate for test fixtures and stamps every sheet.

Windows (era-aware): REF = 2026-03-01..2026-07-28 (clean pre-cutover
regime, primary planning basis) | L90 = last 90 days (spans 7/29
cutover) | L30 = last 30 days (Aug anomaly month flagged). Expected-
ROAS math uses REF; L30 shown for recency; never L30 alone. Vendor
value understatement 10-13% noted once in headers; cells not inflated.

Conventions: leads = primary_conv_count, device='ALL' rows only, QS via
keyword_quality_daily LEFT JOIN (rated_share alongside), campaign_id is
the key. CACHED VALUES written (no live formulas) per charter.

Usage:
  python rollout_plan.py --db paid_search.db --assignment keyword_book_assignment_FROZEN_2026-09-05.csv
  python rollout_plan.py --db fx.db --assignment any.csv --fixture
"""
import argparse
import csv
import datetime as dt
import os
import re
import sqlite3
import sys
from collections import defaultdict

REF_START, REF_END = "2026-03-01", "2026-07-28"
FROZEN_NAME_RE = re.compile(
    r"keyword_book_assignment_FROZEN_\d{4}-\d{2}-\d{2}\.csv$")
RULESET_EXPECTED = "8/27+9/2a+9/2b+9/4-aggtail+9/8-prodintent"
CAMPAIGNS = {  # pinned inventory (confirmed 9/4); diffed vs fresh table
    "23394808297": "TEXAS", "23496334752": "TIER1", "23530860942": "TIER2",
    "23586637678": "TIER3", "23428389081": "TIER4",
    "23385188622": "CALIFORNIA", "23394808300": "FLORIDA",
}
# ANCHOR BASIS (9/7 re-pin): CAMPAIGN-TOTAL kpd leads (all books) —
# classification-free, reproduces structure_case Data_ClassMonthly row
# sums exactly (840/421/875 verified to the digit on the 9/3 DB).
# NOTE: structure_case's own anchors (783/358/781) are HOME-INTENT ONLY
# (BRAND-HOME+GENERIC), a different basis needing book classification.
ANCHORS = [("TEXAS", "2026-03", 840), ("TEXAS", "2026-04", 421),
           ("TIER2", "2026-03", 875)]
DNB_TOKENS = ["usaa", "liberty mutual", "safeco", "amica", "lemonade"]
# D1 RULED 9/7 (Gilbert): brand-home champion = TIER2. --champion overrides.
D1_CHAMPION = "TIER2"
# D2 RULED 9/7 (Gilbert): repurpose the CALIFORNIA campaign ID
# (23385188622) as the national flagship campaign.
D2_VERDICT = "REPURPOSE CALIFORNIA ID 23385188622 (ruled 9/7)"
# D5 RULED 9/7 (Gilbert): ALL campaigns launch at tROAS 1.10; ~2 weeks
# post-launch flagship tightens while both home-intent books loosen.
# Declare the 2-week step in S6's plan BEFORE baseline capture.
LAUNCH_TARGET = 1.10
# Seam vocabulary lifted VERBATIM from structure_case.py / kba (9/2+9/4
# rulings incl. collision-safe forms). B3 uses these; keep in sync with
# the tagger, never re-type.
# 9/8 R6: product-intent brand terms (condo/condominium/landlord/
# property w/ carrier-name carve-out) are BRAND-HOME. Flagship phrase
# negatives gain condo + landlord accordingly.
# "property insurance" (not bare "property") mirrors the tagger's
# P&C carve-out in negative form: blocks 'geico property insurance'
# queries while 'universal property and casualty' (FLAGSHIP by
# carve-out) is not self-blocked.
HOME_TOKENS = ("home", "homeowner", "homeowners", "house",
               "dwelling", "property insurance", "condo", "landlord")
CARRIER_NEG_TOKENS = [
    "geico", "farmers", "state farm", "statefarm", "travelers",
    "kin insurance", "kin home", "aaa", "amica", "hartford",
    "costco", "nationwide", "allstate", "progressive", "hippo",
    "bamboo", "tgs", "universal", "american home", "assurant",
    "metlife", "homesite", "the general", "mercury", "safeco",
    "liberty mutual", "lemonade", "usaa", "chubb", "erie",
    "auto-owners", "cincinnati", "openly", "branch",
    "21st century", "allied", "american family",
    "csaa", "high point", "hochheim", "homeowners of america",
    "njm", "palisades", "plymouth", "root home",
    "selective", "wawanesa",
    # 9/2 overrides
    "policygenius", "nerdwallet", "bankrate",
    "sterling home", "merrimack mutual", "dtric", "cig home",
    "preferred mutual", "ocean harbor", "elephant", "zephyr home",
    "cambridge mutual",
    # 9/4 aggregator tail (collision-safe forms)
    "zebra", "insurify", "smartfinancial", "quotewizard",
    "gabi home", "gabi insurance", "way.com", "everquote",
]
RULED_OUT_OF_SCOPE = {  # Gilbert 9/7: PMax excluded from this project
    "24052581480": "PMax — out-of-scope per 9/7 ruling",
}
BOOKS = ("GENERIC", "BRAND-HOME", "FLAGSHIP")


def die(m):
    print(f"*** HARD FAIL: {m}")
    sys.exit(1)


def log(m):
    print(m)


def load_tags(path, fixture):
    fn = os.path.basename(path)
    with open(path, newline="", encoding="utf-8-sig") as f:
        first = f.readline().strip()
        meta_ok = first.startswith("# frozen:")
        if not fixture:
            if not FROZEN_NAME_RE.search(fn):
                die(f"assignment file '{fn}' is not a FROZEN emission "
                    f"(need keyword_book_assignment_FROZEN_<date>.csv). "
                    f"This workbook refuses non-frozen tag sources.")
            if not meta_ok:
                die("frozen metadata row missing (expect '# frozen: "
                    "<date>, ruleset: ..., row_count: N' as first line).")
        meta = {}
        if meta_ok:
            for part in first.lstrip("# ").split(","):
                if ":" in part:
                    k, v = part.split(":", 1)
                    meta[k.strip()] = v.strip()
            body = f.read().splitlines()
        else:
            body = [first] + f.read().splitlines()
    rows = list(csv.DictReader(body))
    need = {"campaign", "keyword", "match_type", "book", "decision"}
    missing = need - set(rows[0].keys() if rows else [])
    if missing:
        die(f"tag file missing key columns: {sorted(missing)}")
    if meta_ok and "row_count" in meta:
        if int(meta["row_count"]) != len(rows):
            die(f"frozen metadata row_count {meta['row_count']} != "
                f"actual {len(rows)} — file truncated or edited.")
        log(f"[GATE] frozen metadata reconciled: {len(rows)} rows, "
            f"ruleset {meta.get('ruleset', '?')}")
        if meta.get("ruleset") and meta["ruleset"] != RULESET_EXPECTED:
            log(f"[WARN] ruleset '{meta['ruleset']}' != expected "
                f"'{RULESET_EXPECTED}' — verify with tagger thread.")
    if fixture:
        log("[FIXTURE MODE] frozen gate BYPASSED — output is a test "
            "artifact, not a plan.")
    return rows, meta


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--assignment", required=True)
    ap.add_argument("--fixture", action="store_true")
    ap.add_argument("--loser-clicks", type=float, default=100.0)
    ap.add_argument("--champion", default=None,
                    help="D1 brand-home champion shell (TIER1..TIER4); "
                         "default = argmax REF impressions, stamped "
                         "'pending D1 verdict'")
    ap.add_argument("--debug-anchor", action="store_true",
                    help="print top TX 2026-03 contributors for diffing "
                         "against structure_case Data_Keywords")
    a = ap.parse_args()
    here = os.path.dirname(os.path.abspath(__file__))
    outdir = os.path.join(here, "output")
    os.makedirs(outdir, exist_ok=True)

    tags, meta = load_tags(a.assignment, a.fixture)
    live_tags = [t for t in tags
                 if not (t["decision"] or "").startswith("EXCLUDE")]
    log(f"[TAGS] {len(tags)} rows, {len(live_tags)} live (non-EXCLUDE); "
        f"by book: " + ", ".join(
            f"{b}={sum(1 for t in live_tags if t['book'] == b)}"
            for b in BOOKS))

    con = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row

    def cols(t):
        return {r[1] for r in con.execute(f"PRAGMA table_info({t})")}

    kc = cols("campaigns")
    c_id = "campaign_id" if "campaign_id" in kc else "id"
    c_nm = "campaign_name" if "campaign_name" in kc else "name"
    c_st = ("status" if "status" in kc else
            ("campaign_status" if "campaign_status" in kc else None))
    fresh = {str(r[0]): (r[1], (r[2] if c_st else "?"))
             for r in con.execute(
                 f"SELECT {c_id}, {c_nm}"
                 + (f", {c_st}" if c_st else ", NULL")
                 + " FROM campaigns WHERE account_id=1")}
    inv_notes = []
    for cid, short in CAMPAIGNS.items():
        if cid not in fresh:
            inv_notes.append(f"MISSING from fresh export: {short} ({cid})")
    paused_unpinned = 0
    for cid, (nm, st) in fresh.items():
        if cid in CAMPAIGNS:
            continue
        stU = str(st).upper() if st else "?"
        if stU == "ENABLED" or stU == "?":
            if cid in RULED_OUT_OF_SCOPE:
                inv_notes.append(f"ENABLED unpinned campaign {cid} "
                                 f"'{nm}': {RULED_OUT_OF_SCOPE[cid]}")
            else:
                inv_notes.append(f"ENABLED unpinned campaign: {cid} "
                                 f"'{nm}' [{stU}] — needs a scope ruling "
                                 f"before cutover")
        elif stU == "PAUSED":
            paused_unpinned += 1
    if paused_unpinned:
        inv_notes.append(f"{paused_unpinned} PAUSED unpinned campaigns "
                         f"(legacy eras) — expected, no action")
    for n in inv_notes:
        log(f"[INVENTORY] {n}")
    if not inv_notes:
        log("[INVENTORY] fresh campaign table matches pinned IDs.")

    kpc = cols("keyword_performance_daily")
    kdate = "report_date" if "report_date" in kpc else "date"
    g_max = con.execute(
        f"SELECT MAX({kdate}) FROM keyword_performance_daily").fetchone()[0]
    if not g_max:
        die("no keyword performance data")
    dmax = dt.date.fromisoformat(g_max)
    L30 = ((dmax - dt.timedelta(days=29)).isoformat(), g_max)
    L90 = ((dmax - dt.timedelta(days=89)).isoformat(), g_max)
    REF = (REF_START, REF_END)
    log(f"[WINDOWS] REF {REF[0]}..{REF[1]} (planning basis) | "
        f"L90 {L90[0]}..{L90[1]} (spans 7/29 cutover) | "
        f"L30 {L30[0]}..{L30[1]} (Aug anomaly flagged)")

    kic = cols("keyword_instances")
    k_st = ("status" if "status" in kic else
            ("keyword_status" if "keyword_status" in kic else None))
    k_ag = "ad_group_id" if "ad_group_id" in kic else None
    ag_status = {}
    agc = cols("ad_groups") if con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND "
        "name='ad_groups'").fetchone() else set()
    a_st = ("status" if "status" in agc else
            ("ad_group_status" if "ad_group_status" in agc else None))
    a_id = "id" if "id" in agc else ("ad_group_id" if "ad_group_id"
                                     in agc else None)
    if a_st and a_id:
        for r in con.execute(f"SELECT {a_id}, {a_st} FROM ad_groups"):
            ag_status[r[0]] = str(r[1] or "?").upper()
    status_note = ("keyword+ad-group statuses resolved from DB"
                   if (k_st and a_st) else
                   "status columns not in this snapshot — resolve "
                   "statuses at build time (flagged)")
    log(f"[STATUS] {status_note}")
    inst = {}
    inst_status = {}
    sel = "SELECT id, campaign_id, keyword_text, match_type"
    sel += f", {k_st}" if k_st else ", NULL"
    sel += f", {k_ag}" if k_ag else ", NULL"
    for r in con.execute(sel + " FROM keyword_instances "
                               "WHERE account_id=1"):
        inst[r["id"]] = (str(r["campaign_id"]),
                         (r["keyword_text"] or "").lower().strip(),
                         (r["match_type"] or "").upper())
        kst = str(r[4] or "?").upper() if k_st else "?"
        ast = ag_status.get(r[5], "?") if k_ag else "?"
        inst_status[r["id"]] = (kst, ast)

    dev = "device" in kpc
    # ARCHITECT-RULED GRAIN GATES (mirrors structure_case): kpd at
    # device='ALL' must be ONE row per (instance, date). If the sync has
    # started writing split rows (geo or otherwise), summing inflates
    # every number — refuse and escalate rather than guess.
    if "geo_state" in kpc:
        gvals = [r[0] for r in con.execute(
            "SELECT DISTINCT geo_state FROM keyword_performance_daily "
            + ("WHERE device='ALL' " if dev else "") + "LIMIT 5")]
        log(f"[GRAIN] kpd DISTINCT geo_state = {gvals}")
        if not (len(gvals) == 1 and str(gvals[0]).upper() == "ALL"):
            die("kpd geo_state not constant 'ALL' — sync grain changed "
                "since the ruling. Ask the architect before rerunning; "
                "summing split rows silently inflates all numbers.")
    dup = con.execute(
        "SELECT instance_id, " + kdate + ", COUNT(*) c "
        "FROM keyword_performance_daily "
        + ("WHERE device='ALL' " if dev else "")
        + "GROUP BY 1,2 HAVING c > 1 LIMIT 5").fetchall()
    if dup:
        die(f"kpd has multiple rows per (instance, date) at device='ALL' "
            f"— e.g. {[(r[0], r[1], r[2]) for r in dup]}. Sync grain "
            f"changed; anchors will inflate. Ask the architect.")
    log("[GRAIN] kpd one-row-per-(instance,date) at device='ALL': PASS")

    perf = defaultdict(lambda: [0.0] * 5)   # (iid, win) -> im,ck,co,cv,rv
    anchor = defaultdict(float)
    EPRE = ("2026-01-01", "2026-07-28")
    EPOST = ("2026-07-29", g_max)
    Y26 = ("2026-01-01", g_max)
    wins = {"REF": REF, "L90": L90, "L30": L30,
            "EPRE": EPRE, "EPOST": EPOST, "Y26": Y26}
    q = (f"SELECT instance_id iid, {kdate} d, SUM(impressions) im, "
         f"SUM(clicks) ck, SUM(cost_micros)/1e6 co, "
         f"SUM(primary_conv_count) cv, SUM(primary_conv_value) rv "
         f"FROM keyword_performance_daily "
         + (f"WHERE device='ALL' " if dev else "")
         + "GROUP BY 1, 2")
    for r in con.execute(q):
        t = inst.get(r["iid"])
        if not t or t[0] not in CAMPAIGNS:
            continue
        d = r["d"]
        for w, (s, e) in wins.items():
            if s <= d <= e:
                p = perf[(r["iid"], w)]
                p[0] += r["im"] or 0
                p[1] += r["ck"] or 0
                p[2] += r["co"] or 0
                p[3] += r["cv"] or 0
                p[4] += r["rv"] or 0
        ym = d[:7]
        anchor[(CAMPAIGNS[t[0]], ym)] += r["cv"] or 0

    if a.debug_anchor:
        contrib = defaultdict(float)
        qd = (f"SELECT instance_id iid, SUM(primary_conv_count) cv "
              f"FROM keyword_performance_daily "
              + (f"WHERE device='ALL' AND " if dev else "WHERE ")
              + f"SUBSTR({kdate},1,7)='2026-03' GROUP BY 1")
        for r in con.execute(qd):
            t = inst.get(r["iid"])
            if t and t[0] in CAMPAIGNS and CAMPAIGNS[t[0]] == "TEXAS":
                contrib[(t[1], t[2])] += r["cv"] or 0
        top = sorted(contrib.items(), key=lambda kv: -kv[1])[:15]
        log(f"[DEBUG-ANCHOR] TX 2026-03 total {sum(contrib.values()):.0f} "
            f"across {len(contrib)} keywords; top 15:")
        for (kw, mt), v in top:
            log(f"    {v:7.1f}  {kw} [{mt}]")

    for short, ym, want in ANCHORS:
        got = anchor[(short, ym)]
        ok = abs(got - want) <= max(1.0, 0.02 * want)
        log(f"[ANCHOR] {short} {ym}: {got:.0f} vs {want} "
            f"{'PASS' if ok else 'FAIL'}")
        if not ok and not a.fixture:
            die("anchor reconciliation failed — wrong DB/scope.")

    qcols = cols("keyword_quality_daily")
    qs = defaultdict(lambda: [0.0, 0.0])    # (iid, win) -> qs*im, rated_im
    if qcols:
        qsq = (f"SELECT k.instance_id iid, k.{kdate} d, "
               f"q.historical_quality_score s, SUM(k.impressions) im "
               f"FROM keyword_performance_daily k "
               f"LEFT JOIN keyword_quality_daily q "
               f"ON q.instance_id = k.instance_id "
               f"AND q.report_date = k.{kdate} "
               + ("WHERE k.device='ALL' " if dev else "")
               + "GROUP BY 1, 2, 3")
        for r in con.execute(qsq):
            if r["s"] is None or not inst.get(r["iid"]):
                continue
            for w, (s, e) in wins.items():
                if s <= r["d"] <= e:
                    qs[(r["iid"], w)][0] += r["s"] * (r["im"] or 0)
                    qs[(r["iid"], w)][1] += r["im"] or 0

    # aggregate performance to (campaign_short, kw, match) x window
    agg = defaultdict(lambda: [0.0] * 5)
    qagg = defaultdict(lambda: [0.0, 0.0])
    kw_anywhere = defaultdict(set)          # (kw, match) -> campaign shorts
    for (iid, w), p in perf.items():
        cidp, kw, mt = inst[iid]
        short = CAMPAIGNS[cidp]
        k = (short, kw, mt, w)
        for i in range(5):
            agg[k][i] += p[i]
        if p[0] > 0:
            kw_anywhere[(kw, mt)].add(short)
        qv = qs.get((iid, w))
        if qv:
            qagg[k][0] += qv[0]
            qagg[k][1] += qv[1]

    import xlsxwriter
    out = os.path.join(outdir, "rollout_plan.xlsx")
    wb = xlsxwriter.Workbook(out)
    f_h = wb.add_format({"bold": True, "bg_color": "#1F3864",
                         "font_color": "white"})
    f_b = wb.add_format({"bold": True})
    f_warn = wb.add_format({"bold": True, "font_color": "#9C0006",
                            "bg_color": "#FFC7CE"})
    f_n = wb.add_format({"num_format": "#,##0"})
    f_c = wb.add_format({"num_format": "$#,##0"})
    f_r = wb.add_format({"num_format": "0.00"})
    f_p = wb.add_format({"num_format": "0%"})
    stamp = ("FIXTURE MODE — NOT A PLAN" if a.fixture else
             f"frozen tags: {os.path.basename(a.assignment)} "
             f"({meta.get('frozen', '?')})")

    def kw_sheet(name, book, extra_note=""):
        ws = wb.add_worksheet(name)
        ws.write(0, 0, f"[{stamp}]  {book} book — every non-EXCLUDE "
                       f"tagged keyword, all campaigns. REF window is the "
                       f"planning basis (Mar 1–Jul 28, pre-cutover "
                       f"regime); L30 = recency (Aug anomaly month, "
                       f"rated_share 0.87–0.89); L90 spans the 7/29 "
                       f"cutover (context only, columns collapsible). "
                       f"Vendor values understate ~10–13% (Sep vintage) — "
                       f"noted once, cells not inflated. " + extra_note,
                 f_b if not a.fixture else f_warn)
        hdr = ["campaign", "keyword", "match", "action", "protected",
               "REF_spend", "REF_leads", "REF_rev", "REF_ROAS", "REF_CPC",
               "REF_iwQS", "REF_rated_share",
               "L30_spend", "L30_ROAS", "L90_spend", "L90_ROAS",
               "in_TX", "DNB"]
        for c, h in enumerate(hdr):
            ws.write(3, c, h, f_h)
        ws.set_column(14, 15, None, None, {"level": 1, "collapsed": True})
        rows = [t for t in live_tags if t["book"] == book]

        def refspend(t):
            return agg.get((t["campaign"], t["keyword"].lower().strip(),
                            t["match_type"].upper(), "REF"),
                           [0] * 5)[2]
        rows.sort(key=lambda t: -refspend(t))
        x = 4
        camp_tot = defaultdict(lambda: [0.0] * 5)
        for t in rows:
            kwl = t["keyword"].lower().strip()
            mt = t["match_type"].upper()
            key3 = (t["campaign"], kwl, mt)
            pr = {w: agg.get(key3 + (w,), [0.0] * 5)
                  for w in ("REF", "L30", "L90")}
            qv = qagg.get(key3 + ("REF",), [0.0, 0.0])
            for i in range(5):
                camp_tot[t["campaign"]][i] += pr["REF"][i]
            ws.write_string(x, 0, t["campaign"])
            ws.write_string(x, 1, t["keyword"])
            ws.write_string(x, 2, mt)
            ws.write_string(x, 3, t.get("action") or "")
            ws.write_string(x, 4, t.get("protected") or "")
            im, ck, co, cv, rv = pr["REF"]
            ws.write_number(x, 5, co, f_c)
            ws.write_number(x, 6, cv, f_n)
            ws.write_number(x, 7, rv, f_c)
            if co:
                ws.write_number(x, 8, rv / co, f_r)
            if ck:
                ws.write_number(x, 9, co / ck, f_r)
            if qv[1]:
                ws.write_number(x, 10, qv[0] / qv[1], f_r)
            if im:
                ws.write_number(x, 11, min(qv[1] / im, 1.0), f_p)
            ws.write_number(x, 12, pr["L30"][2], f_c)
            if pr["L30"][2]:
                ws.write_number(x, 13, pr["L30"][4] / pr["L30"][2], f_r)
            ws.write_number(x, 14, pr["L90"][2], f_c)
            if pr["L90"][2]:
                ws.write_number(x, 15, pr["L90"][4] / pr["L90"][2], f_r)
            ws.write_string(
                x, 16, "KEEP-instance"
                if "TEXAS" in kw_anywhere.get((kwl, mt), set())
                else "ADD-to-champion")
            ws.write_string(x, 17, "DNB" if any(
                tok in kwl for tok in DNB_TOKENS) else "")
            x += 1
        ws.autofilter(3, 0, max(x - 1, 4), len(hdr) - 1)
        ws.freeze_panes(4, 3)
        # subtotal block
        sx = x + 2
        ws.write(sx, 0, f"{book} REF subtotals by campaign (the contrast "
                        f"table) + allocated-spend / expected-ROAS lines",
                 f_b)
        for c, h in enumerate(["campaign", "spend", "leads", "revenue",
                               "ROAS", "CPL"]):
            ws.write(sx + 1, c, h, f_h)
        tot = [0.0] * 5
        for i, (camp, v) in enumerate(sorted(camp_tot.items(),
                                             key=lambda kv: -kv[1][2])):
            ws.write_string(sx + 2 + i, 0, camp)
            ws.write_number(sx + 2 + i, 1, v[2], f_c)
            ws.write_number(sx + 2 + i, 2, v[3], f_n)
            ws.write_number(sx + 2 + i, 3, v[4], f_c)
            if v[2]:
                ws.write_number(sx + 2 + i, 4, v[4] / v[2], f_r)
            if v[3]:
                ws.write_number(sx + 2 + i, 5, v[2] / v[3], f_r)
            for j in range(5):
                tot[j] += v[j]
        ty = sx + 2 + len(camp_tot)
        ws.write_string(ty, 0, "ALLOCATED (all campaigns)")
        ws.write_number(ty, 1, tot[2], f_c)
        ws.write_number(ty, 2, tot[3], f_n)
        ws.write_number(ty, 3, tot[4], f_c)
        if tot[2]:
            ws.write_number(ty, 4, tot[4] / tot[2], f_r)
        ws.write(ty + 1, 0, "expected ROAS = REF-window trailing blend "
                            "above — a trailing blend, not a forecast; "
                            "S6's pre-registered reads govern.", f_b)
        return camp_tot

    kw_sheet("K1_TX_to_Generic", "GENERIC",
             "in_TX: KEEP-instance = (text,match) already live in TX; "
             "ADD-to-champion = union step adds it.")
    k2tot = kw_sheet("K2_Shell_to_BrandHome", "BRAND-HOME",
                     "D1 table below subtotals: champion = argmax REF "
                     "impressions unless ROAS/iwQS materially dissent.")

    # D1 decision table on K2 — placed BELOW the subtotal block:
    # rows = 4 header/title + tag rows + 2 gap + block title + header +
    # campaign rows + ALLOCATED + honesty line + 2 gap
    ws = wb.get_worksheet_by_name("K2_Shell_to_BrandHome")
    n_bh = sum(1 for t in live_tags if t["book"] == "BRAND-HOME")
    dx = 4 + n_bh + 2 + 2 + len(k2tot) + 2 + 3
    ws.write(dx, 0, "D1 — BRAND-HOME CHAMPION (Gilbert fills verdict):",
             f_b)
    for c, h in enumerate(["shell", "REF_impr", "REF_spend", "REF_leads",
                           "REF_ROAS", "REF_iwQS", "candidate"]):
        ws.write(dx + 1, c, h, f_h)
    shells = ["TIER1", "TIER2", "TIER3", "TIER4"]
    imprs = {}
    for i, sh in enumerate(shells):
        v = [0.0] * 5
        qv = [0.0, 0.0]
        for t in live_tags:
            if t["book"] != "BRAND-HOME" or t["campaign"] != sh:
                continue
            k3 = (sh, t["keyword"].lower().strip(),
                  t["match_type"].upper())
            p = agg.get(k3 + ("REF",), [0.0] * 5)
            for j in range(5):
                v[j] += p[j]
            q2 = qagg.get(k3 + ("REF",), [0.0, 0.0])
            qv[0] += q2[0]
            qv[1] += q2[1]
        imprs[sh] = v[0]
        ws.write_string(dx + 2 + i, 0, sh)
        ws.write_number(dx + 2 + i, 1, v[0], f_n)
        ws.write_number(dx + 2 + i, 2, v[2], f_c)
        ws.write_number(dx + 2 + i, 3, v[3], f_n)
        if v[2]:
            ws.write_number(dx + 2 + i, 4, v[4] / v[2], f_r)
        if qv[1]:
            ws.write_number(dx + 2 + i, 5, qv[0] / qv[1], f_r)
    champ = max(imprs, key=imprs.get) if imprs else ""
    for i, sh in enumerate(shells):
        ws.write_string(dx + 2 + i, 6,
                        "ARGMAX (candidate)" if sh == champ else "")
    ws.write(dx + 7, 0, "D1 VERDICT (fill in):", f_b)
    ws.write(dx + 7, 1, "")

    # ---- STAGE 2: P-sheets ----
    # campaign totals (ALL instances, tagged or not) for coverage math
    camp_win = defaultdict(lambda: [0.0] * 5)   # (short, win)
    for (iid, w), p in perf.items():
        short = CAMPAIGNS[inst[iid][0]]
        for i in range(5):
            camp_win[(short, w)][i] += p[i]
    tagged_keys = {(t["campaign"], t["keyword"].lower().strip(),
                    t["match_type"].upper())
                   for t in live_tags}
    # champion (D1): explicit arg or argmax
    champion = a.champion or D1_CHAMPION
    champ_note = ("--champion override" if a.champion
                  else "D1 RULED 9/7: TIER2")
    log(f"[D1] brand-home champion for planning: {champion} "
        f"({champ_note})")

    # P1_Retire_Shells
    wsP1 = wb.add_worksheet("P1_Retire_Shells")
    wsP1.write(0, 0, f"[{stamp}] PAUSE — non-champion shells. Champion = "
                     f"{champion} ({champ_note}). Coverage = share of the "
                     f"shell's REF leads carried forward by the plan "
                     f"(non-EXCLUDE tagged keywords: GENERIC->TX, "
                     f"BRAND-HOME->champion, FLAGSHIP->B1 build). "
                     f"Uncarried = EXCLUDEd + untagged-but-active "
                     f"keywords; inspect before pausing if coverage "
                     f"is low.",
               f_b if not a.fixture else f_warn)
    hdr1 = ["shell", "REF_spend", "REF_leads", "REF_ROAS", "L30_spend",
            "carried_REF_leads", "coverage", "untagged_REF_leads",
            "rename_to"]
    for c, h in enumerate(hdr1):
        wsP1.write(2, c, h, f_h)
    shells2 = [s for s in ("TIER1", "TIER2", "TIER3", "TIER4")
               if s != champion]
    carried = defaultdict(float)
    tagged_any = defaultdict(float)
    for t in live_tags:
        k3 = (t["campaign"], t["keyword"].lower().strip(),
              t["match_type"].upper())
        p = agg.get(k3 + ("REF",), [0.0] * 5)
        carried[t["campaign"]] += p[3]
    for (iid, w), p in perf.items():
        if w != "REF":
            continue
        cidp, kw, mt = inst[iid]
        short = CAMPAIGNS[cidp]
        if (short, kw, mt) in tagged_keys:
            tagged_any[short] += p[3]
    for i, sh in enumerate(shells2):
        tot = camp_win[(sh, "REF")]
        wsP1.write_string(3 + i, 0, sh)
        wsP1.write_number(3 + i, 1, tot[2], f_c)
        wsP1.write_number(3 + i, 2, tot[3], f_n)
        if tot[2]:
            wsP1.write_number(3 + i, 3, tot[4] / tot[2], f_r)
        wsP1.write_number(3 + i, 4, camp_win[(sh, "L30")][2], f_c)
        wsP1.write_number(3 + i, 5, carried[sh], f_n)
        if tot[3]:
            wsP1.write_number(3 + i, 6, carried[sh] / tot[3], f_p)
        wsP1.write_number(3 + i, 7, max(tot[3] - tagged_any[sh], 0.0),
                          f_n)
        wsP1.write_string(3 + i, 8, f"zz-RETIRED-{sh}")

    # P2_CAFL_HomeIntent
    wsP2 = wb.add_worksheet("P2_CAFL_HomeIntent")
    wsP2.write(0, 0, f"[{stamp}] PAUSE — CA/FL standalone home-intent "
                     f"keywords. covered_in_national = same (text, match) "
                     f"exists tagged in the receiving national book "
                     f"(GENERIC->TX book; BRAND-HOME->champion book "
                     f"{champion}). Uncovered rows are volume the "
                     f"national books do not yet carry — candidate ADDs.",
               f_b if not a.fixture else f_warn)
    hdr2 = ["campaign", "keyword", "match", "book", "REF_spend",
            "REF_leads", "REF_ROAS", "L30_spend", "covered_in_national"]
    for c, h in enumerate(hdr2):
        wsP2.write(2, c, h, f_h)
    gen_book_keys = {(t["keyword"].lower().strip(),
                      t["match_type"].upper())
                     for t in live_tags if t["book"] == "GENERIC"
                     and t["campaign"] not in ("CALIFORNIA", "FLORIDA")}
    bh_book_keys = {(t["keyword"].lower().strip(),
                     t["match_type"].upper())
                    for t in live_tags if t["book"] == "BRAND-HOME"
                    and t["campaign"] not in ("CALIFORNIA", "FLORIDA")}
    p2rows = [t for t in live_tags
              if t["campaign"] in ("CALIFORNIA", "FLORIDA")
              and t["book"] in ("GENERIC", "BRAND-HOME")]
    p2rows.sort(key=lambda t: -agg.get(
        (t["campaign"], t["keyword"].lower().strip(),
         t["match_type"].upper(), "REF"), [0] * 5)[2])
    x2 = 3
    cov_leads = uncov_leads = 0.0
    for t in p2rows:
        kwl = t["keyword"].lower().strip()
        mt = t["match_type"].upper()
        p = agg.get((t["campaign"], kwl, mt, "REF"), [0.0] * 5)
        l30 = agg.get((t["campaign"], kwl, mt, "L30"), [0.0] * 5)
        pool = (gen_book_keys if t["book"] == "GENERIC"
                else bh_book_keys)
        cov = (kwl, mt) in pool
        if cov:
            cov_leads += p[3]
        else:
            uncov_leads += p[3]
        wsP2.write_string(x2, 0, t["campaign"])
        wsP2.write_string(x2, 1, t["keyword"])
        wsP2.write_string(x2, 2, mt)
        wsP2.write_string(x2, 3, t["book"])
        wsP2.write_number(x2, 4, p[2], f_c)
        wsP2.write_number(x2, 5, p[3], f_n)
        if p[2]:
            wsP2.write_number(x2, 6, p[4] / p[2], f_r)
        wsP2.write_number(x2, 7, l30[2], f_c)
        wsP2.write_string(x2, 8, "YES" if cov else "NO — candidate ADD")
        x2 += 1
    wsP2.autofilter(2, 0, max(x2 - 1, 3), len(hdr2) - 1)
    wsP2.freeze_panes(3, 3)
    wsP2.write(x2 + 1, 0, f"coverage: {cov_leads:,.0f} REF leads covered "
                          f"in national books; {uncov_leads:,.0f} NOT "
                          f"covered ({uncov_leads / (cov_leads + uncov_leads):.0%} of CA/FL "
                          f"home-intent volume) — review NO rows before "
                          f"pause." if (cov_leads + uncov_leads) else
               "no CA/FL home-intent tags found", f_b)

    # P3_DNB_and_Losers
    wsP3 = wb.add_worksheet("P3_DNB_and_Losers")
    wsP3.write(0, 0, f"[{stamp}] DNB token families "
                     f"({', '.join(DNB_TOKENS)}): every live instance, "
                     f"2026 spend (should be ~$0 — flagged if not) + "
                     f"negative-list injection. LOSERS (mechanical rule, "
                     f"param row 2): >= {a.loser_clicks:.0f} clicks in "
                     f"2026 across BOTH eras, >=1 click in EACH era "
                     f"(pre/post 7/29 cutover), ZERO leads -> pause at "
                     f"cutover. List only; nothing discretionary.",
               f_b if not a.fixture else f_warn)
    wsP3.write(1, 0, "params:", f_b)
    wsP3.write_number(1, 1, a.loser_clicks)
    wsP3.write_string(1, 2, "min clicks (Y26, both eras summed)")
    for c, h in enumerate(["campaign", "keyword", "match", "token",
                           "spend_2026", "leads_2026", "kw_status",
                           "ag_status", "verdict"]):
        wsP3.write(3, c, h, f_h)
    x3 = 4
    dnb_active = defaultdict(bool)
    dnb_seen = defaultdict(set)
    for iid, (cidp, kw, mt) in inst.items():
        tok = next((tk for tk in DNB_TOKENS if tk in kw), None)
        if not tok:
            continue
        p = perf.get((iid, "Y26"))
        if p is None:
            continue
        short = CAMPAIGNS[cidp]
        kst, ast = inst_status.get(iid, ("?", "?"))
        active = not (kst.startswith("PAUS") or ast.startswith("PAUS"))
        if active and (kst != "?" or ast != "?"):
            dnb_active[tok] = True
        elif kst == "?" and ast == "?":
            dnb_active[tok] = dnb_active[tok] or (p[2] > 1.0)
        dnb_seen[tok].add(short)
        wsP3.write_string(x3, 0, short)
        wsP3.write_string(x3, 1, kw)
        wsP3.write_string(x3, 2, mt)
        wsP3.write_string(x3, 3, tok)
        wsP3.write_number(x3, 4, p[2], f_c)
        wsP3.write_number(x3, 5, p[3], f_n)
        wsP3.write_string(x3, 6, kst)
        wsP3.write_string(x3, 7, ast)
        wsP3.write_string(x3, 8,
                          "INCLUDE (active — 9/7 ruling: still bought)"
                          if active and (kst != "?" or ast != "?")
                          else ("DO NOT RE-INCLUDE (kw or ad group "
                                "paused — 9/7 ruling)"
                                if (kst != "?" or ast != "?")
                                else "resolve status at build"))
        x3 += 1
    wsP3.write(x3 + 1, 0, "PER-TOKEN VERDICT (9/7 ruling: still-bought "
                          "tokens go into the flagship build and are "
                          "NOT negatived; dead tokens are not "
                          "re-included):", f_b)
    for i, tk in enumerate(DNB_TOKENS):
        wsP3.write_string(x3 + 2 + i, 0, tk)
        alive = dnb_active.get(tk, False)
        wsP3.write_string(x3 + 2 + i, 1,
                          ("STILL BOUGHT — include in flagship build, "
                           "no negative" if alive else
                           "dead/inactive — do not re-include"))
        wsP3.write_string(x3 + 2 + i, 2, "seen in: " + (", ".join(
            sorted(dnb_seen.get(tk, []))) or "none"))
    xL = x3 + 2 + len(DNB_TOKENS) + 2
    wsP3.write(xL, 0, "LOSERS — pause at cutover (mechanical):", f_b)
    for c, h in enumerate(["campaign", "keyword", "match", "clicks_2026",
                           "clicks_preEra", "clicks_postEra",
                           "spend_2026", "leads_2026", "action"]):
        wsP3.write(xL + 1, c, h, f_h)
    xr = xL + 2
    n_losers = 0
    for iid, (cidp, kw, mt) in inst.items():
        y = perf.get((iid, "Y26"))
        if y is None or y[3] > 0:
            continue
        pre = perf.get((iid, "EPRE"), [0.0] * 5)
        post = perf.get((iid, "EPOST"), [0.0] * 5)
        if y[1] >= a.loser_clicks and pre[1] >= 1 and post[1] >= 1:
            wsP3.write_string(xr, 0, CAMPAIGNS[cidp])
            wsP3.write_string(xr, 1, kw)
            wsP3.write_string(xr, 2, mt)
            wsP3.write_number(xr, 3, y[1], f_n)
            wsP3.write_number(xr, 4, pre[1], f_n)
            wsP3.write_number(xr, 5, post[1], f_n)
            wsP3.write_number(xr, 6, y[2], f_c)
            wsP3.write_number(xr, 7, y[3], f_n)
            wsP3.write_string(xr, 8, "PAUSE AT CUTOVER")
            xr += 1
            n_losers += 1
    log(f"[P3] DNB live instances: {x3 - 4}; losers "
        f"(>= {a.loser_clicks:.0f} clicks, both eras, 0 leads): "
        f"{n_losers}")

    # ---- K1U/K2U: unique (keyword, match) rollups (9/7 request) ----
    def unique_sheet(name, book, dest):
        wsu = wb.add_worksheet(name)
        wsu.write(0, 0, f"[{stamp}] {book} — UNIQUE (keyword, match) "
                        f"per row, all holding campaigns grouped "
                        f"(B1-style view; K-sheets keep the per-campaign "
                        f"grain). dest_has_it: {dest} already holds the "
                        f"key.", f_b if not a.fixture else f_warn)
        hdru = ["keyword", "match", "source_campaigns", "n_sources",
                "REF_spend", "REF_leads", "REF_ROAS", "REF_CPC",
                "L30_spend", "L30_ROAS", "dest_has_it", "launch_action"]
        for c, h in enumerate(hdru):
            wsu.write(2, c, h, f_h)
        keys = {}
        for t in live_tags:
            if t["book"] != book:
                continue
            keys.setdefault((t["keyword"].lower().strip(),
                             t["match_type"].upper()),
                            set()).add(t["campaign"])
        rowsu = []
        for (kw, mt), camps in keys.items():
            tot = [0.0] * 5
            l30 = [0.0] * 5
            for cp in camps:
                p = agg.get((cp, kw, mt, "REF"), [0.0] * 5)
                q = agg.get((cp, kw, mt, "L30"), [0.0] * 5)
                for i in range(5):
                    tot[i] += p[i]
                    l30[i] += q[i]
            rowsu.append((kw, mt, camps, tot, l30))
        rowsu.sort(key=lambda r: -r[3][2])
        for i, (kw, mt, camps, tot, l30) in enumerate(rowsu):
            x = 3 + i
            wsu.write_string(x, 0, kw)
            wsu.write_string(x, 1, mt)
            wsu.write_string(x, 2, ",".join(sorted(camps)))
            wsu.write_number(x, 3, len(camps), f_n)
            wsu.write_number(x, 4, tot[2], f_c)
            wsu.write_number(x, 5, tot[3], f_n)
            if tot[2]:
                wsu.write_number(x, 6, tot[4] / tot[2], f_r)
            if tot[1]:
                wsu.write_number(x, 7, tot[2] / tot[1], f_r)
            wsu.write_number(x, 8, l30[2], f_c)
            if l30[2]:
                wsu.write_number(x, 9, l30[4] / l30[2], f_r)
            wsu.write_string(x, 10, "YES" if dest in camps else "no")
            l90i = sum(agg.get((cp, kw, mt, "L90"), [0.0] * 5)[0]
                       for cp in camps)
            wsu.write_string(x, 11, "KEEP" if dest in camps else
                             ("TRANSITION-ADD" if l90i > 0
                              else "DEFER (dormant)"))
        wsu.autofilter(2, 0, max(2 + len(rowsu), 3), len(hdru) - 1)
        wsu.freeze_panes(3, 2)
        return len(rowsu)

    nU1 = unique_sheet("K1U_Generic_Unique", "GENERIC", "TEXAS")

    # ---- K1T: generic ad-group theming (9/7 request: quote split) ----
    # 9/7 full taxonomy. Precedence: product segments outrank modifier
    # themes; QUOTE stays first per the ruled split. WAVES: 1 = launch
    # (existing volume); 2 = GKP-gated (add when the Keyword Planner
    # list populates them, post +28d read); 3 = PAYOUT-GATED (product
    # lines — verify buyer-feed RPL for condo / manufactured / 2nd-home
    # / bundle leads BEFORE building; unsellable leads get negatives,
    # not ad groups).
    THEME_ORDER = ["QUOTE", "CONDO", "MOBILE", "EXTENSION", "BUNDLE",
                   "PERIL", "COST", "COVERAGE", "HAZARD", "COMPARE",
                   "COMPANIES", "STATE", "CORE", "OTHER"]
    THEME_WAVE = {"QUOTE": 1, "PERIL": 1, "STATE": 1, "CORE": 1,
                  "OTHER": 1, "COST": 2, "COVERAGE": 2, "HAZARD": 2,
                  "COMPARE": 2, "COMPANIES": 2, "CONDO": 3, "MOBILE": 3,
                  "EXTENSION": 3, "BUNDLE": 3}
    THEME_TOKENS = {
        "QUOTE": ["quote", "quotes", "estimate"],
        "CONDO": ["condo", "condominium", "ho-6", "ho6"],
        "MOBILE": ["mobile home", "manufactured", "modular", "trailer"],
        "EXTENSION": ["second home", "2nd home", "vacation home",
                      "vacation property"],
        "BUNDLE": ["bundle", "bundling", "home and auto",
                   "auto and home"],
        "PERIL": ["fire", "flood", "wind", "hurricane", "earthquake",
                  "storm", "hail"],
        "COST": ["cheap", "affordable", "cost", "price", "low "],
        "COVERAGE": ["coverage", "covered", "what does"],
        "HAZARD": ["hazard"],
        "COMPARE": ["best", "compare", "comparison", "top ", "rated",
                    "review"],
        "COMPANIES": ["company", "companies", "provider", "carrier",
                      "agency", "agencies", "agent", "broker"],
    }
    CORE_FORMS = {"home insurance", "homeowners insurance",
                  "homeowner insurance", "house insurance",
                  "home owners insurance", "home insurance policy",
                  "homeowners insurance policy", "property insurance",
                  "dwelling insurance"}
    STATE_NAMES = [s.lower() for s in (
        "alabama alaska arizona arkansas california colorado connecticut "
        "delaware florida georgia hawaii idaho illinois indiana iowa "
        "kansas kentucky louisiana maine maryland massachusetts michigan "
        "minnesota mississippi missouri montana nebraska nevada ohio "
        "oklahoma oregon pennsylvania tennessee texas utah vermont "
        "virginia washington wisconsin wyoming".split())] + [
        "new hampshire", "new jersey", "new mexico", "new york",
        "north carolina", "north dakota", "rhode island",
        "south carolina", "south dakota", "west virginia"]

    def theme_of(kw):
        for th in THEME_ORDER:
            if th in THEME_TOKENS and any(
                    tk in kw for tk in THEME_TOKENS[th]):
                return th
        if any(sn in kw for sn in STATE_NAMES):
            return "STATE"
        if kw in CORE_FORMS:
            return "CORE"
        return "OTHER"

    wsT = wb.add_worksheet("K1T_AdGroup_Themes")
    wsT.write(0, 0, f"[{stamp}] GENERIC AD-GROUP BLUEPRINT (9/7: split "
                    f"quote-intent from core). Theme precedence: "
                    f"{' > '.join(THEME_ORDER)} — first matching token "
                    f"wins, so 'cheap home insurance quotes' themes "
                    f"QUOTE. STATE = state-modified generics (viable as "
                    f"keywords now that geography combines into one "
                    f"campaign). ROUTING: broad match leaks intent "
                    f"across ad groups — each theme's signature tokens "
                    f"must be PHRASE-NEGATIVED in every OTHER generic ad "
                    f"group (block below), or the split only renames "
                    f"rows. Query-DB sizing of the re-routed volume: "
                    f"pending (table name).",
              f_b if not a.fixture else f_warn)
    hdrT = ["keyword", "match", "theme", "proposed_ad_group",
            "source_campaigns", "REF_spend", "REF_leads", "REF_ROAS",
            "REF_CPC", "L30_spend"]
    for c, h in enumerate(hdrT):
        wsT.write(2, c, h, f_h)
    gk = {}
    for t in live_tags:
        if t["book"] != "GENERIC":
            continue
        gk.setdefault((t["keyword"].lower().strip(),
                       t["match_type"].upper()), set()).add(t["campaign"])
    trows = []
    for (kw, mt), camps in gk.items():
        tot = [0.0] * 5
        l30 = [0.0] * 5
        for cp in camps:
            p = agg.get((cp, kw, mt, "REF"), [0.0] * 5)
            q = agg.get((cp, kw, mt, "L30"), [0.0] * 5)
            for i in range(5):
                tot[i] += p[i]
                l30[i] += q[i]
        trows.append((theme_of(kw), kw, mt, camps, tot, l30))
    trows.sort(key=lambda r: (THEME_ORDER.index(r[0]), -r[4][2]))
    tsum = defaultdict(lambda: [0, 0.0, 0.0, 0.0])
    for i, (th, kw, mt, camps, tot, l30) in enumerate(trows):
        x = 3 + i
        wsT.write_string(x, 0, kw)
        wsT.write_string(x, 1, mt)
        wsT.write_string(x, 2, th)
        wsT.write_string(x, 3, f"Home-Generic-{th.title()}")
        wsT.write_string(x, 4, ",".join(sorted(camps)))
        wsT.write_number(x, 5, tot[2], f_c)
        wsT.write_number(x, 6, tot[3], f_n)
        if tot[2]:
            wsT.write_number(x, 7, tot[4] / tot[2], f_r)
        if tot[1]:
            wsT.write_number(x, 8, tot[2] / tot[1], f_r)
        wsT.write_number(x, 9, l30[2], f_c)
        s = tsum[th]
        s[0] += 1
        s[1] += tot[2]
        s[2] += tot[3]
        s[3] += tot[4]
    wsT.autofilter(2, 0, max(2 + len(trows), 3), len(hdrT) - 1)
    wsT.freeze_panes(3, 3)
    xs = 3 + len(trows) + 2
    wsT.write(xs, 0, "PROPOSED AD GROUPS (theme summary):", f_b)
    for c, h in enumerate(["ad_group", "wave", "n_keys", "REF_spend",
                           "REF_leads", "REF_ROAS", "REF_CPL"]):
        wsT.write(xs + 1, c, h, f_h)
    for i, th in enumerate(THEME_ORDER):
        s = tsum.get(th, [0, 0.0, 0.0, 0.0])
        wsT.write_string(xs + 2 + i, 0, f"Home-Generic-{th.title()}")
        wsT.write_string(xs + 2 + i, 1,
                         {1: "1 — LAUNCH", 2: "2 — GKP-gated",
                          3: "3 — PAYOUT-GATED"}[THEME_WAVE[th]])
        wsT.write_number(xs + 2 + i, 2, s[0], f_n)
        wsT.write_number(xs + 2 + i, 3, s[1], f_c)
        wsT.write_number(xs + 2 + i, 4, s[2], f_n)
        if s[1]:
            wsT.write_number(xs + 2 + i, 5, s[3] / s[1], f_r)
        if s[2]:
            wsT.write_number(xs + 2 + i, 6, s[1] / s[2], f_r)
    xr2 = xs + 2 + len(THEME_ORDER) + 1
    wsT.write(xr2, 0, "ROUTING NEGATIVES (ad-group level, PHRASE, "
                      "PRECEDENCE-AWARE): a theme's tokens are negatived "
                      "only in LOWER-precedence ad groups — e.g. 'quote' "
                      "is negatived in Price/Compare/Companies/State/"
                      "Core/Other but state names are NOT negatived in "
                      "Quote, so 'california home insurance quotes' "
                      "routes to Quote as precedence dictates:", f_b)
    for i, (th, toks) in enumerate(THEME_TOKENS.items()):
        wsT.write_string(xr2 + 1 + i, 0, f"Home-Generic-{th.title()}")
        wsT.write_string(xr2 + 1 + i, 1,
                         "negate elsewhere: " + ", ".join(
                             f'"{tk.strip()}"' for tk in toks))
    log(f"[K1T] generic themes: " + ", ".join(
        f"{th}={tsum[th][0]}" for th in THEME_ORDER if th in tsum))
    nU2 = unique_sheet("K2U_BrandHome_Unique", "BRAND-HOME", champion)
    log(f"[KU] unique rollups: {nU1} generic keys, {nU2} brand-home "
        f"keys")

    # ---- STAGE 3: B-sheets, M1 gate, A1 ----
    def book_keys(book, camps=None, exclude_camps=None):
        out = {}
        for t in live_tags:
            if t["book"] != book:
                continue
            if camps and t["campaign"] not in camps:
                continue
            if exclude_camps and t["campaign"] in exclude_camps:
                continue
            out.setdefault((t["keyword"].lower().strip(),
                            t["match_type"].upper()), []).append(
                t["campaign"])
        return out

    # asset-DB copy reference (approved amendment: graceful blank+flag)
    asset_theme = {}
    asset_flag = ""
    try:
        atc = cols("ad_assets") if con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='ad_assets'").fetchone() else set()
        if atc:
            pass  # theme lookup deferred to builder prompt; presence noted
        asset_flag = ("asset DB present — copy refs resolved at build "
                      "time" if atc else
                      "asset DB unreachable from this snapshot — copy "
                      "refs blank (flagged, per approved amendment)")
    except Exception:
        asset_flag = ("asset DB unreachable from this snapshot — copy "
                      "refs blank (flagged, per approved amendment)")

    # B1_National_Flagship
    wsB1 = wb.add_worksheet("B1_National_Flagship")
    wsB1.write(0, 0, f"[{stamp}] BUILD — national flagship campaign. "
                     f"Distinct (text, match) across ALL campaigns' "
                     f"FLAGSHIP tags = the build list. Geo = national "
                     f"minus CA/FL. {asset_flag}.",
               f_b if not a.fixture else f_warn)
    hdrB1 = ["keyword", "match", "source_campaigns", "n_sources",
             "REF_spend", "REF_leads", "REF_ROAS", "L30_spend",
             "L30_ROAS", "copy_ref", "launch_scope"]
    for c, h in enumerate(hdrB1):
        wsB1.write(2, c, h, f_h)
    b1 = book_keys("FLAGSHIP")
    b1rows = []
    for (kw, mt), camps in b1.items():
        tot = [0.0] * 5
        l30 = [0.0] * 5
        for cp in camps:
            p = agg.get((cp, kw, mt, "REF"), [0.0] * 5)
            q = agg.get((cp, kw, mt, "L30"), [0.0] * 5)
            for i in range(5):
                tot[i] += p[i]
                l30[i] += q[i]
        b1rows.append((kw, mt, camps, tot, l30))
    b1rows.sort(key=lambda r: -r[3][2])
    for i, (kw, mt, camps, tot, l30) in enumerate(b1rows):
        x = 3 + i
        wsB1.write_string(x, 0, kw)
        wsB1.write_string(x, 1, mt)
        wsB1.write_string(x, 2, ",".join(sorted(set(camps))))
        wsB1.write_number(x, 3, len(set(camps)), f_n)
        wsB1.write_number(x, 4, tot[2], f_c)
        wsB1.write_number(x, 5, tot[3], f_n)
        if tot[2]:
            wsB1.write_number(x, 6, tot[4] / tot[2], f_r)
        wsB1.write_number(x, 7, l30[2], f_c)
        if l30[2]:
            wsB1.write_number(x, 8, l30[4] / l30[2], f_r)
        wsB1.write_string(x, 9, asset_theme.get((kw, mt), ""))
        l90i = sum(agg.get((cp, kw, mt, "L90"), [0.0] * 5)[0]
                   for cp in set(camps))
        wsB1.write_string(x, 10, "LAUNCH" if l90i > 0
                          else "DEFER (dormant)")
    wsB1.autofilter(2, 0, max(2 + len(b1rows), 3), len(hdrB1) - 1)
    wsB1.freeze_panes(3, 2)

    # B2_CAFL_Flagship + D2
    wsB2 = wb.add_worksheet("B2_CAFL_Flagship")
    wsB2.write(0, 0, f"[{stamp}] BUILD + D2 — CA/FL flagship keywords "
                     f"and the standalone-flagship decision. D2 "
                     f"RECOMMENDATION: REPURPOSE the CA/FL campaign IDs "
                     f"(cite S1_Headline: an intact ledger does not "
                     f"rescue flagship — the tax is class-shaped, so new "
                     f"IDs buy nothing); portfolio target below.",
               f_b if not a.fixture else f_warn)
    hdrB2 = ["campaign", "keyword", "match", "REF_spend", "REF_leads",
             "REF_ROAS", "L30_spend", "L30_ROAS"]
    for c, h in enumerate(hdrB2):
        wsB2.write(2, c, h, f_h)
    b2rows = [t for t in live_tags if t["book"] == "FLAGSHIP"
              and t["campaign"] in ("CALIFORNIA", "FLORIDA")]
    b2rows.sort(key=lambda t: -agg.get(
        (t["campaign"], t["keyword"].lower().strip(),
         t["match_type"].upper(), "REF"), [0] * 5)[2])
    b2tot = [0.0] * 5
    xb = 3
    for t in b2rows:
        kwl = t["keyword"].lower().strip()
        mt = t["match_type"].upper()
        p = agg.get((t["campaign"], kwl, mt, "REF"), [0.0] * 5)
        l30 = agg.get((t["campaign"], kwl, mt, "L30"), [0.0] * 5)
        for i in range(5):
            b2tot[i] += p[i]
        wsB2.write_string(xb, 0, t["campaign"])
        wsB2.write_string(xb, 1, t["keyword"])
        wsB2.write_string(xb, 2, mt)
        wsB2.write_number(xb, 3, p[2], f_c)
        wsB2.write_number(xb, 4, p[3], f_n)
        if p[2]:
            wsB2.write_number(xb, 5, p[4] / p[2], f_r)
        wsB2.write_number(xb, 6, l30[2], f_c)
        if l30[2]:
            wsB2.write_number(xb, 7, l30[4] / l30[2], f_r)
        xb += 1
    wsB2.write(xb + 1, 0, "D2 DECISION BLOCK", f_b)
    wsB2.write(xb + 2, 0, "combined CA+FL flagship REF: "
                          f"${b2tot[2]:,.0f} spend, {b2tot[3]:,.0f} "
                          f"leads, realized ROAS "
                          f"{(b2tot[4] / b2tot[2]) if b2tot[2] else 0:.2f}")
    wsB2.write(xb + 3, 0, "current targets (VERIFY IN UI before "
                          "cutover): CA", f_b)
    wsB2.write_number(xb + 3, 1, 1.17, f_r)
    wsB2.write(xb + 3, 2, "FL", f_b)
    wsB2.write_number(xb + 3, 3, 1.12, f_r)
    ca_sp = sum(agg.get(("CALIFORNIA", t["keyword"].lower().strip(),
                         t["match_type"].upper(), "REF"), [0] * 5)[2]
                for t in b2rows if t["campaign"] == "CALIFORNIA")
    fl_sp = b2tot[2] - ca_sp
    blend = ((ca_sp * 1.17 + fl_sp * 1.12) / b2tot[2]
             if b2tot[2] else 0)
    wsB2.write(xb + 4, 0, "portfolio blended launch target "
                          "(REF-spend-weighted):", f_b)
    wsB2.write_number(xb + 4, 2, blend, f_r)
    wsB2.write(xb + 5, 0, f"D2 VERDICT: {D2_VERDICT}", f_b)

    # B3_Seam_Negatives
    wsB3 = wb.add_worksheet("B3_Seam_Negatives")
    wsB3.write(0, 0, f"[{stamp}] BUILD — campaign seams. Home books "
                     f"(TX generic + {champion} brand-home) get EXACT "
                     f"negatives = every distinct FLAGSHIP keyword text "
                     f"(exact blocks the bare query only — brand+home "
                     f"keywords keep serving). Flagship campaign gets "
                     f"PHRASE negatives = home tokens. Self-block check "
                     f"below MUST be 0.",
               f_b if not a.fixture else f_warn)
    flag_texts = sorted({kw for (kw, mt) in b1.keys()})
    wsB3.write(2, 0, f"LIST 1 — EXACT negatives for TX-generic AND "
                     f"{champion}-brand-home ({len(flag_texts)} terms):",
               f_b)
    for i, kw in enumerate(flag_texts):
        wsB3.write_string(3 + i, 0, f"[{kw}]")
    xn = 3 + len(flag_texts) + 1
    wsB3.write(xn, 0, f"LIST 2 — PHRASE negatives for the flagship "
                      f"campaign ({len(HOME_TOKENS)} tokens):", f_b)
    for i, tk in enumerate(HOME_TOKENS):
        wsB3.write_string(xn + 1 + i, 0, f'"{tk}"')
    xc = xn + 1 + len(HOME_TOKENS) + 1
    gen_keys = book_keys("GENERIC")
    bh_keys = book_keys("BRAND-HOME")
    viol = []
    for (kw, mt) in list(gen_keys) + list(bh_keys):
        if kw in b1 or (kw, "EXACT") in b1 or kw in flag_texts:
            viol.append(("home-book kw = flagship exact neg", kw))
    for (kw, mt) in b1.keys():
        if any(tk in kw for tk in HOME_TOKENS):
            viol.append(("flagship kw contains home token", kw))
    wsB3.write(xc, 0, f"SELF-BLOCK CHECK: {len(viol)} violations "
                      f"(MUST be 0 before build)",
               f_b if not viol else f_warn)
    for i, (why, kw) in enumerate(viol[:50]):
        wsB3.write_string(xc + 1 + i, 0, why)
        wsB3.write_string(xc + 1 + i, 1, kw)
    log(f"[B3] seam negatives: {len(flag_texts)} exact terms -> home "
        f"books; {len(HOME_TOKENS)} phrase tokens -> flagship; "
        f"self-block violations: {len(viol)}")

    # M1_Union_Coverage
    wsM1 = wb.add_worksheet("M1_Union_Coverage")
    wsM1.write(0, 0, f"[{stamp}] MOVE — union audit per book: distinct "
                     f"(text, match) x holding campaigns x "
                     f"destination-has-it -> ADD list. GENERIC dest = "
                     f"TEXAS; BRAND-HOME dest = {champion}; FLAGSHIP "
                     f"dest = B1 build (all rows are builds). "
                     f"Reconciliation gate at bottom.",
               f_b if not a.fixture else f_warn)
    for c, h in enumerate(["book", "keyword", "match", "held_by",
                           "dest_has_it", "REF_spend", "L90_impr",
                           "action"]):
        wsM1.write(2, c, h, f_h)
    xm = 3
    adds = {"GENERIC": 0, "BRAND-HOME": 0}
    defers = {"GENERIC": 0, "BRAND-HOME": 0}
    for book, dest in (("GENERIC", "TEXAS"), ("BRAND-HOME", champion)):
        for (kw, mt), camps in sorted(book_keys(book).items()):
            has = dest in camps
            ref_sp = sum(agg.get((cp, kw, mt, "REF"), [0.0] * 5)[2]
                         for cp in camps)
            l90i = sum(agg.get((cp, kw, mt, "L90"), [0.0] * 5)[0]
                       for cp in camps)
            if has:
                act = "KEEP"
            elif l90i > 0:
                act = f"TRANSITION-ADD to {dest}"
                adds[book] += 1
            else:
                act = "DEFER (dormant — expansion backlog)"
                defers[book] += 1
            wsM1.write_string(xm, 0, book)
            wsM1.write_string(xm, 1, kw)
            wsM1.write_string(xm, 2, mt)
            wsM1.write_string(xm, 3, ",".join(sorted(set(camps))))
            wsM1.write_string(xm, 4, "YES" if has else "no")
            wsM1.write_number(xm, 5, ref_sp, f_c)
            wsM1.write_number(xm, 6, l90i, f_n)
            wsM1.write_string(xm, 7, act)
            xm += 1
    wsM1.autofilter(2, 0, max(xm - 1, 3), 5)
    wsM1.freeze_panes(3, 3)
    # reconciliation gate: sheet-key counts vs tag-file distinct counts
    xg = xm + 2
    wsM1.write(xg, 0, "RECONCILIATION GATE (K1+K2+B1 distinct keys vs "
                      "frozen tag file):", f_b)
    gate_ok = True
    for i, (book, keys) in enumerate((("GENERIC", gen_keys),
                                      ("BRAND-HOME", bh_keys),
                                      ("FLAGSHIP", b1))):
        tag_n = len({(t["campaign"], t["keyword"].lower().strip(),
                      t["match_type"].upper())
                     for t in live_tags if t["book"] == book})
        sheet_n = sum(len(set(c)) for c in keys.values())
        ok = tag_n == sheet_n
        gate_ok &= ok
        wsM1.write_string(xg + 1 + i, 0, book)
        wsM1.write(xg + 1 + i, 1, f"tag rows {tag_n} vs sheet "
                                  f"{sheet_n}: "
                                  + ("PASS" if ok else "FAIL"),
                   f_b if ok else f_warn)
    log(f"[M1] union (9/7 transition-first gate, L90 impressions > 0): "
        f"generic {adds['GENERIC']} TRANSITION-ADDs / "
        f"{defers['GENERIC']} deferred; brand-home "
        f"{adds['BRAND-HOME']} TRANSITION-ADDs / "
        f"{defers['BRAND-HOME']} deferred; reconciliation "
        f"{'PASS' if gate_ok else 'FAIL'}")
    if not gate_ok and not a.fixture:
        die("M1 reconciliation failed — sheet keys != frozen tag file.")

    # A1_Budgets_Targets (D5)
    wsA1 = wb.add_worksheet("A1_Budgets_Targets")
    wsA1.write(0, 0, f"[{stamp}] D5 — budgets & launch targets. Trailing "
                     f"book spend share (REF + L30); PROPOSED split caps "
                     f"flagship at <= its trailing share. Launch tROAS "
                     f"per surviving controller = current effective "
                     f"(param cells — VERIFY IN UI at cutover; values "
                     f"from the 2026 change log, frozen since 6/25). S6 "
                     f"baseline capture (analysis thread) precedes "
                     f"cutover; reads +14/+28/+56d follow.",
               f_b if not a.fixture else f_warn)
    for c, h in enumerate(["book", "REF_spend", "REF_share", "L30_spend",
                           "L30_share", "proposed_share",
                           "launch_target"]):
        wsA1.write(2, c, h, f_h)
    bt = {}
    for book in BOOKS:
        rs = sum(agg.get((t["campaign"], t["keyword"].lower().strip(),
                          t["match_type"].upper(), "REF"), [0] * 5)[2]
                 for t in live_tags if t["book"] == book)
        ls = sum(agg.get((t["campaign"], t["keyword"].lower().strip(),
                          t["match_type"].upper(), "L30"), [0] * 5)[2]
                 for t in live_tags if t["book"] == book)
        bt[book] = (rs, ls)
    rtot = sum(v[0] for v in bt.values()) or 1
    ltot = sum(v[1] for v in bt.values()) or 1
    targets = {"GENERIC": ("D5 ruled 9/7: universal launch",
                           LAUNCH_TARGET),
               "BRAND-HOME": ("D5 ruled 9/7: universal launch",
                              LAUNCH_TARGET),
               "FLAGSHIP": ("D5 ruled 9/7: universal launch",
                            LAUNCH_TARGET)}
    for i, book in enumerate(BOOKS):
        rs, ls = bt[book]
        wsA1.write_string(3 + i, 0, book)
        wsA1.write_number(3 + i, 1, rs, f_c)
        wsA1.write_number(3 + i, 2, rs / rtot, f_p)
        wsA1.write_number(3 + i, 3, ls, f_c)
        wsA1.write_number(3 + i, 4, ls / ltot, f_p)
        prop = (min(rs / rtot, ls / ltot) if book == "FLAGSHIP"
                else None)
        if book == "FLAGSHIP":
            wsA1.write_number(3 + i, 5, prop, f_p)
        else:
            wsA1.write(3 + i, 5, "balance of budget")
        nm2, tv = targets[book]
        wsA1.write_number(3 + i, 6, tv, f_r)
        wsA1.write_string(3 + i, 7, nm2)
    wsA1.write(7, 0, f"D5 RULED 9/7: all three campaigns launch at "
                     f"tROAS {LAUNCH_TARGET:.2f}. PRE-DECLARED STEP "
                     f"(~+14d): flagship tightens; both home-intent "
                     f"books loosen — written into S6's plan BEFORE "
                     f"baseline capture so the +28/+56 reads stay "
                     f"clean. (B2 portfolio blend retained above as "
                     f"reference only.)", f_b)
    wsA1.write(8, 0, "S6 BASELINE (restate; capture BEFORE cutover, "
                     "analysis thread): generic CPC / generic CPL / "
                     "generic impression share of class / flagship CPL "
                     "/ blended CPL — read at cutover +14 / +28 / +56 "
                     "days; falsification clause as pre-registered.",
               f_b)

    # 00 stub
    ws0 = wb.add_worksheet("00_Summary")
    ws0.write(0, 0, f"[{stamp}] ROLLOUT PLAN — STAGE 1 BUILD (K1/K2 + "
                    f"gate + windows). Stages 2-3 add P/B/M/A sheets.",
              f_b if not a.fixture else f_warn)
    seq = ["1. tag FREEZE (tagger thread)", "2. fresh campaign export + "
           "inventory diff", "3. build rollout workbook (this)",
           "4. S6 BASELINE capture (analysis thread) — BEFORE cutover",
           "5. cutover", "6. change freeze window + S6 reads "
           "(+14/+28/+56d)"]
    ws0.write(2, 0, "SEQUENCING (status col B):", f_b)
    for i, s in enumerate(seq):
        ws0.write(3 + i, 0, s)
    ws0.write(10, 0, "DECISIONS — ALL RULED 9/7: D1 champion = TIER2 "
                     "| D2 = repurpose CALIFORNIA ID 23385188622 | D5 = "
                     "universal 1.10 launch target (+14d pre-declared "
                     "step: flagship tighter, home-intent looser) | DNB "
                     "= include still-bought active keywords, never "
                     "re-include paused kw/ad-group | PMax 24052581480 "
                     "out-of-scope.", f_b)
    ws0.write(16, 0, "LAUNCH PRINCIPLE (9/7, Gilbert): transition-first "
                     "— launch carries ONLY currently-serving keywords "
                     "(L90 activity gate); dormant tagged keys sit in "
                     "the expansion backlog, promoted on search-query "
                     "evidence post-S6 reads; net-new expansion after "
                     "that. Take off from traction.", f_b)
    ws0.write(14, 0, "STAGE 3 SHEETS: B1_National_Flagship (build "
                     "list) | B2_CAFL_Flagship (+D2) | B3_Seam_Negatives "
                     "(self-block check) | M1_Union_Coverage (gate) | "
                     "A1_Budgets_Targets (D5)", f_b)
    ws0.write(11, 0, "STAGE 2 SHEETS: P1_Retire_Shells (coverage + "
                     "renames) | P2_CAFL_HomeIntent (national-book "
                     "coverage) | P3_DNB_and_Losers (negatives + "
                     "mechanical pauses)", f_b)
    ws0.write(12, 0, f"WINDOWS: REF {REF[0]}..{REF[1]} | L90 "
                     f"{L90[0]}..{L90[1]} | L30 {L30[0]}..{L30[1]} | "
                     f"data through {g_max}")
    ws0.write(13, 0, "INVENTORY DIFF: " + ("; ".join(inv_notes)
                                           if inv_notes else
                                           "clean vs pinned IDs"))
    wb.close()
    wb_order_note = "sheet order note: 00 written last in Stage 1"
    log(f"[DONE] {out} ({wb_order_note})")


if __name__ == "__main__":
    main()
