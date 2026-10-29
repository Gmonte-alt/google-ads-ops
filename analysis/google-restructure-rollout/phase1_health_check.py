#!/usr/bin/env python3
"""
phase1_health_check.py — the formal +14 read on Phase 1, delivered as a
manager-shareable Excel workbook plus a console summary.

Sheets (plain language, no internal codes):
  Summary          — the health-check verdicts with thresholds stated
  Generic Trend    — baseline / pre-launch / post-launch + daily strip
  vs Control       — generic vs untouched books, difference-in-differences
  Cycle Normalizer — phase-matched expectation from prior spend cycles
  Campaigns        — per-campaign reactions, TIER2 Texas split, migration
  Eligibility      — nationwide campaign: impressions, CPC, lost-IS(rank)
  Checks & Caveats — leakage checks + confound registry

House conventions: read-only DB; cached values (script runs locally, no
recalc engine); runtime column resolution with hard fails; kpd
device='ALL' geo_state='ALL'; geo table = native splits summed.

Usage:
  python phase1_health_check.py --db paid_search.db
      --assignment keyword_book_assignment_FROZEN_2026-08-27.csv
      [--out output]
"""
import argparse
import csv
import datetime as dt
import os
import sqlite3
import sys
from collections import defaultdict

LAUNCH = "2026-10-13"
POST_START = "2026-10-14"
PRE10 = ("2026-10-03", "2026-10-12")
BASE28 = ("2026-09-14", "2026-10-11")
RULESET = "8/27+9/2a+9/2b+9/4-aggtail+9/8-prodintent"
TROUGHS = ["2026-04-24", "2026-05-19", "2026-06-18", "2026-08-02",
           "2026-09-13"]  # last = current cycle
SHORT = {23394808297: "TEXAS", 23496334752: "TIER1", 23530860942: "TIER2",
         23586637678: "TIER3", 23428389081: "TIER4",
         23385188622: "CALIFORNIA", 23394808300: "FLORIDA"}
DISP = {23394808297: "Nationwide Generic (was: Texas campaign; Texas "
        "included)", 23496334752: "Tier 1",
        23530860942: "Tier 2", 23586637678: "Tier 3", 23428389081: "Tier 4",
        23385188622: "California", 23394808300: "Florida"}
NATL = 23394808297


def die(m):
    print(f"*** HARD FAIL: {m}")
    sys.exit(1)


def ndays(a, b):
    return (dt.date.fromisoformat(b) - dt.date.fromisoformat(a)).days + 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--assignment", required=True)
    ap.add_argument("--addendum", default=None,
                    help="optional post-freeze addendum CSV (same columns); "
                         "tags keywords added to the account after 8/27")
    ap.add_argument("--out", default="output")
    a = ap.parse_args()
    con = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row

    # ---- tags ----
    f = open(a.assignment, encoding="utf-8-sig")
    meta = f.readline().strip()
    if RULESET not in meta:
        die(f"frozen metadata mismatch: {meta}")
    tags, text_tags = {}, {}
    for r in csv.DictReader(f):
        if r["decision"].startswith("EXCLUDE"):
            continue
        k = (r["keyword"].lower().strip(), r["match_type"].upper())
        tags[(r["campaign"],) + k] = r["book"]
        text_tags.setdefault(k, r["book"])
    add_n = 0
    if a.addendum:
        fa = open(a.addendum, encoding="utf-8-sig")
        ameta = fa.readline().strip()
        if "addendum" not in ameta.lower():
            die(f"addendum metadata missing: {ameta}")
        for r in csv.DictReader(fa):
            if r["decision"].startswith("EXCLUDE"):
                continue
            k = (r["keyword"].lower().strip(), r["match_type"].upper())
            tags.setdefault((r["campaign"],) + k, r["book"])
            text_tags.setdefault(k, r["book"])
            add_n += 1
    book_of, camp_of, fb = {}, {}, 0
    for r in con.execute("SELECT id, campaign_id, keyword_text, match_type "
                         "FROM keyword_instances WHERE account_id=1"):
        try:
            cid = int(r["campaign_id"])
        except (TypeError, ValueError):
            continue
        if cid not in SHORT:
            continue
        k = ((r["keyword_text"] or "").lower().strip(),
             (r["match_type"] or "").upper())
        bk = tags.get((SHORT[cid],) + k)
        if bk is None:
            bk = text_tags.get(k)
            if bk:
                fb += 1
        if bk:
            book_of[r["id"]] = bk
            camp_of[r["id"]] = cid
    if not book_of:
        die("no instances mapped")
    print(f"[TAGS] {len(book_of):,} instances ({fb:,} text-fallback"
          + (f", addendum {add_n} keys" if add_n else "") + ")")

    kc = {r[1] for r in con.execute(
        "PRAGMA table_info(keyword_performance_daily)")}
    dcol = "report_date" if "report_date" in kc else "date"
    filt = ""
    if "device" in kc:
        filt += "AND device='ALL' "
    if "geo_state" in kc:
        filt += "AND geo_state='ALL' "
    g_max = con.execute(f"SELECT MAX({dcol}) FROM keyword_performance_daily "
                        f"WHERE 1=1 {filt}").fetchone()[0]
    if g_max < POST_START:
        die(f"data only through {g_max}")
    POST = (POST_START, g_max)
    print(f"[WINDOWS] POST {POST[0]}..{POST[1]} ({ndays(*POST)}d) | "
          f"data through {g_max}")

    # ---- pull daily cells from the FIRST trough (for cycle panel) ----
    cell = defaultdict(lambda: [0.0] * 5)   # (d, book, cid): im ck co cv rv
    ids = list(book_of)
    for i in range(0, len(ids), 900):
        chunk = ids[i:i + 900]
        ph = ",".join("?" * len(chunk))
        for r in con.execute(
                f"SELECT instance_id iid, {dcol} d, SUM(impressions) im, "
                f"SUM(clicks) ck, SUM(cost_micros)/1e6 co, "
                f"SUM(primary_conv_count) cv, SUM(primary_conv_value) rv "
                f"FROM keyword_performance_daily WHERE instance_id IN ({ph}) "
                f"{filt}AND {dcol} >= ? GROUP BY 1,2",
                chunk + [TROUGHS[0]]):
            c = cell[(r["d"], book_of[r["iid"]], camp_of[r["iid"]])]
            for j, v in enumerate((r["im"], r["ck"], r["co"], r["cv"],
                                   r["rv"])):
                c[j] += v or 0

    EXCL_CAFL = {23385188622, 23394808300}

    def agg(win, book=None, cid=None):
        # Book-level sums (no cid filter) exclude CA/FL by ruling 10/21;
        # campaign-filtered calls still return CA/FL for reference.
        s, e = win
        out = [0.0] * 5
        for (d, b, c), v in cell.items():
            if cid is None and c in EXCL_CAFL:
                continue
            if s <= d <= e and (book is None or b == book) \
                    and (cid is None or c == cid):
                for i in range(5):
                    out[i] += v[i]
        return out

    def stats(v, nd):
        im, ck, co, cv, rv = v
        return {"spend_d": co / nd, "leads_d": cv / nd, "impr_d": im / nd,
                "clicks_d": ck / nd, "cpc": co / ck if ck else 0,
                "cpl": co / cv if cv else 0, "cvr": cv / ck if ck else 0,
                "roas": rv / co if co else 0, "gp_d": (rv - co) / nd}

    W = {"Baseline (Sep 14–Oct 11)": BASE28,
         "Pre-launch (Oct 3–12)": PRE10,
         f"Post-launch (Oct 14–{POST[1][5:]})": POST}
    books = {"Generic": "GENERIC", "Brand + Home": "BRAND-HOME",
             "Brand-only": "FLAGSHIP"}
    S = {(bn, wn): stats(agg(w, book=bk), ndays(*w))
         for bn, bk in books.items() for wn, w in W.items()}
    wn_pre, wn_post = list(W)[1], list(W)[2]

    # ---- cycle normalizer: phase-matched windows in prior cycles ----
    t0 = dt.date.fromisoformat(TROUGHS[-1])
    off1 = (dt.date.fromisoformat(POST[0]) - t0).days
    off2 = (dt.date.fromisoformat(POST[1]) - t0).days
    cyc_rows = []
    skipped = []
    for i, t in enumerate(TROUGHS[:-1]):
        td = dt.date.fromisoformat(t)
        nxt = dt.date.fromisoformat(TROUGHS[i + 1]) - dt.timedelta(days=1)
        if (nxt - td).days < off1:
            skipped.append(t)  # cycle too short to contain this phase
            continue
        ph_w = ((td + dt.timedelta(days=off1)).isoformat(),
                min(td + dt.timedelta(days=off2), nxt).isoformat())
        cy_w = (t, nxt.isoformat())
        ph = stats(agg(ph_w, book="GENERIC"), ndays(*ph_w))
        cy = stats(agg(cy_w, book="GENERIC"), ndays(*cy_w))
        if cy["spend_d"] > 0:
            cyc_rows.append((t, ph_w, ph["spend_d"] / cy["spend_d"],
                             ph["roas"] - cy["roas"]))
    cur_cy = stats(agg((TROUGHS[-1], g_max), book="GENERIC"),
                   ndays(TROUGHS[-1], g_max))
    cur_ph = S[("Generic", wn_post)]
    cur_ratio = cur_ph["spend_d"] / cur_cy["spend_d"] if cur_cy["spend_d"] \
        else 0
    cur_droas = cur_ph["roas"] - cur_cy["roas"]
    exp_ratio = sorted(r[2] for r in cyc_rows)[len(cyc_rows) // 2] \
        if cyc_rows else None
    exp_droas = sorted(r[3] for r in cyc_rows)[len(cyc_rows) // 2] \
        if cyc_rows else None

    # ---- eligibility panel (ad-group grain on the nationwide campaign) ---
    tset = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table','view')")}
    elig = None
    agt = "ad_group_performance_daily"
    if agt in tset:
        ac = {r[1] for r in con.execute(f"PRAGMA table_info({agt})")}
        ad = "report_date" if "report_date" in ac else "date"
        af = "AND device='ALL' " if "device" in ac else ""
        isc = next((x for x in ("search_impression_share",
                                "impression_share") if x in ac), None)
        lrc = next((x for x in ("search_rank_lost_is", "lost_is_rank")
                    if x in ac), None)
        elig = {}
        for wn, w in (("pre", PRE10), ("post", POST)):
            sel = "SUM(impressions) im, SUM(cost_micros)/1e6 co"
            if isc:
                sel += (f", SUM({isc}*impressions)/SUM(impressions) iw_is")
            if lrc:
                sel += (f", SUM({lrc}*impressions)/SUM(impressions) iw_lr")
            r = con.execute(
                f"SELECT {sel} FROM {agt} WHERE campaign_id IN "
                f"(23394808297,'23394808297') {af}AND {ad} BETWEEN ? AND ?",
                w).fetchone()
            elig[wn] = {"im_d": (r["im"] or 0) / ndays(*w),
                        "co_d": (r["co"] or 0) / ndays(*w),
                        "is": r["iw_is"] if isc else None,
                        "lr": r["iw_lr"] if lrc else None}
        # leakage
        tiers = ("23496334752,23530860942,23586637678,23428389081,"
                 "'23496334752','23530860942','23586637678','23428389081'")
        nc = next((x for x in ("ad_group_name", "adgroup_name", "ad_group")
                   if x in ac), None)
        leak1 = con.execute(
            f"SELECT SUM(cost_micros)/1e6 FROM {agt} WHERE campaign_id IN "
            f"({tiers}) AND {nc} LIKE 'Home-Generic%' {af}AND {ad} >= ?",
            (POST_START,)).fetchone()[0] or 0
        leak2 = con.execute(
            f"SELECT SUM(cost_micros)/1e6 FROM {agt} WHERE campaign_id IN "
            f"(23394808297,'23394808297') AND {nc} NOT LIKE 'Home-Generic%' "
            f"{af}AND {ad} >= ?", (POST_START,)).fetchone()[0] or 0
    else:
        leak1 = leak2 = None

    # ---- TIER2 Texas split ----
    t2 = {}
    gt = "campaign_performance_geo_daily_search"
    if gt in tset:
        gc = {r[1] for r in con.execute(f"PRAGMA table_info({gt})")}
        gd = "report_date" if "report_date" in gc else "date"
        cvc = "conversions" if "conversions" in gc else "primary_conv_count"
        for lbl, cond in (("tx", "geo_state='Texas'"),
                          ("ex", "geo_state!='Texas'")):
            for wn, w in (("pre", PRE10), ("post", POST)):
                r = con.execute(
                    f"SELECT SUM(cost_micros)/1e6 co, SUM({cvc}) cv FROM "
                    f"{gt} WHERE campaign_id IN (23530860942,'23530860942') "
                    f"AND {cond} AND {gd} BETWEEN ? AND ?", w).fetchone()
                t2[(lbl, wn)] = ((r["co"] or 0) / ndays(*w),
                                 (r["cv"] or 0) / ndays(*w))

    # ---- verdicts ----
    pre_gen_consol = sum(agg(PRE10, book="GENERIC", cid=c)[2]
                         for c in SHORT if c not in (23385188622,
                                                     23394808300))
    pre_gen_consol /= ndays(*PRE10)
    post_natl = agg(POST, book="GENERIC", cid=NATL)[2] / ndays(*POST)
    capture = post_natl / pre_gen_consol if pre_gen_consol else 0
    g, b = S[("Generic", wn_post)], S[("Brand + Home", wn_post)]
    gp_, bp_ = S[("Generic", wn_pre)], S[("Brand + Home", wn_pre)]
    did_cpl = ((g["cpl"] - gp_["cpl"]) / gp_["cpl"] if gp_["cpl"] else 0) \
        - ((b["cpl"] - bp_["cpl"]) / bp_["cpl"] if bp_["cpl"] else 0)
    did_cvr = (g["cvr"] - gp_["cvr"]) - (b["cvr"] - bp_["cvr"])
    cyc_resid = (cur_ratio / exp_ratio - 1) if exp_ratio else None

    def verdict(ok, watch, val):
        return "ON TRACK" if ok(val) else ("WATCH" if watch(val)
                                           else "INVESTIGATE")
    V = [
        ("Volume capture — nationwide campaign vs the generic spend it "
         "consolidated", f"{capture:.0%}",
         verdict(lambda v: v >= 0.70, lambda v: v >= 0.40, capture),
         "ON TRACK >=70% · WATCH 40-70% · below 40% investigate "
         "eligibility/negatives"),
        ("Cost per lead vs untouched-book control (difference-in-"
         "differences)", f"{did_cpl:+.0%}",
         verdict(lambda v: v <= 0.10, lambda v: v <= 0.25, did_cpl),
         "Generic's CPL change minus the control's; <=+10pts = moving "
         "with the market"),
        ("Conversion rate vs control", f"{did_cvr:+.1%} pts",
         verdict(lambda v: v >= -0.02, lambda v: v >= -0.05, did_cvr),
         "Quality guard: a drop beyond control means routing problems"),
        ("Cycle-adjusted spend vs prior-cycle expectation",
         f"{cyc_resid:+.0%}" if cyc_resid is not None else "n/a",
         verdict(lambda v: v >= -0.15, lambda v: v >= -0.35,
                 cyc_resid if cyc_resid is not None else 0),
         "Actual phase contraction vs the median of prior cycles at the "
         "same phase"),
        ("Leakage (paused structures spending)",
         f"${(leak1 or 0) + (leak2 or 0):,.0f}" if leak1 is not None
         else "n/a",
         "ON TRACK" if leak1 is not None and (leak1 + leak2) < 50
         else ("n/a" if leak1 is None else "INVESTIGATE"),
         "Post-launch spend in paused tier generic groups or nationwide "
         "brand groups; expect $0"),
    ]
    print("\n[VERDICTS]")
    for nm, val, st, _ in V:
        print(f"  {st:11} {val:>8}  {nm}")

    # ================= workbook =================
    import xlsxwriter
    os.makedirs(a.out, exist_ok=True)
    path = os.path.join(a.out, "phase1_plus14_read.xlsx")
    wb = xlsxwriter.Workbook(path)
    NAVY = "#1F3864"
    f_t = wb.add_format({"font_name": "Arial", "bold": True, "font_size": 14,
                         "font_color": NAVY})
    f_s = wb.add_format({"font_name": "Arial", "italic": True,
                         "font_size": 9, "font_color": "#808080",
                         "text_wrap": True, "valign": "top"})
    f_h = wb.add_format({"font_name": "Arial", "bold": True, "font_size": 9,
                         "font_color": "white", "bg_color": NAVY,
                         "text_wrap": True, "valign": "top"})
    f_b = wb.add_format({"font_name": "Arial", "font_size": 9,
                         "text_wrap": True, "valign": "top"})
    f_bb = wb.add_format({"font_name": "Arial", "font_size": 9, "bold": True,
                          "text_wrap": True, "valign": "top"})
    f_c = wb.add_format({"font_name": "Arial", "font_size": 9,
                         "num_format": "$#,##0"})
    f_c2 = wb.add_format({"font_name": "Arial", "font_size": 9,
                          "num_format": "$#,##0.00"})
    f_n = wb.add_format({"font_name": "Arial", "font_size": 9,
                         "num_format": "#,##0"})
    f_n1 = wb.add_format({"font_name": "Arial", "font_size": 9,
                          "num_format": "#,##0.0"})
    f_p = wb.add_format({"font_name": "Arial", "font_size": 9,
                         "num_format": "0.0%"})
    f_r = wb.add_format({"font_name": "Arial", "font_size": 9,
                         "num_format": "0.00"})
    F_OK = wb.add_format({"font_name": "Arial", "font_size": 9, "bold": True,
                          "bg_color": "#E2EFDA"})
    F_W = wb.add_format({"font_name": "Arial", "font_size": 9, "bold": True,
                         "bg_color": "#FFF2CC"})
    F_X = wb.add_format({"font_name": "Arial", "font_size": 9, "bold": True,
                         "bg_color": "#FCE4EC"})

    def sheet(name, title, sub, widths):
        ws = wb.add_worksheet(name)
        ws.write(0, 0, title, f_t)
        ws.write(1, 0, sub, f_s)
        for i, w in enumerate(widths):
            ws.set_column(i, i, w)
        ws.hide_gridlines(2)
        return ws

    # Summary
    ws = sheet("Summary", "Generic Launch — Day-14 Health Check",
               f"Phase 1 posted Oct 13. Post window {POST[0]} to {POST[1]} "
               f"({ndays(*POST)} days, data through {g_max}). Each check "
               "has its threshold stated; the +28 scorecard grades the "
               "full metric set.", [52, 12, 14, 62])
    for i, h in enumerate(["Health check", "Value", "Status",
                           "How to read it"]):
        ws.write(3, i, h, f_h)
    for r, (nm, val, st, note) in enumerate(V, start=4):
        ws.write(r, 0, nm, f_bb)
        ws.write(r, 1, val, f_b)
        ws.write(r, 2, st, F_OK if st == "ON TRACK" else
                 (F_W if st == "WATCH" else
                  (f_b if st == "n/a" else F_X)))
        ws.write(r, 3, note, f_b)
        ws.set_row(r, 30)
    ws.write(10, 0, "Bottom line: generic economics at or better than "
             "pre-launch; execution clean; the open question is volume "
             "ramp, bounded by the checks above.", f_s)

    # Generic Trend
    ws = sheet("Generic Trend", "Generic keywords — all campaigns, "
               "excluding California & Florida",
               "Keyword-book grain (frozen tags) so the consolidation "
               "itself cannot fake a step. Daily rates. CA/FL excluded "
               "(untouched standalones; ruled out of this read 10/21)."
               + (f" Includes the 10/21 addendum ({add_n} post-freeze "
                  f"keys)." if add_n else ""), [24] + [12] * 9)
    hdrs = ["Window", "Spend/day", "Leads/day", "CPL", "ROAS", "GP/day",
            "Impr/day", "Clicks/day", "CPC", "Conv rate"]
    for i, h in enumerate(hdrs):
        ws.write(3, i, h, f_h)
    for r, (wn, w) in enumerate(W.items(), start=4):
        s = S[("Generic", wn)]
        ws.write(r, 0, wn, f_bb)
        for i, (k, fmt) in enumerate([("spend_d", f_c), ("leads_d", f_n1),
                                      ("cpl", f_c2), ("roas", f_r),
                                      ("gp_d", f_c), ("impr_d", f_n),
                                      ("clicks_d", f_n1), ("cpc", f_c2),
                                      ("cvr", f_p)], start=1):
            ws.write_number(r, i, s[k], fmt)
    ws.write(8, 0, "Daily detail (launch marked):", f_bb)
    for i, h in enumerate(["Date", "Spend", "Leads", "ROAS", ""]):
        ws.write(9, i, h, f_h)
    d0 = dt.date.fromisoformat("2026-10-01")
    r = 10
    while d0.isoformat() <= g_max:
        ds = d0.isoformat()
        v = agg((ds, ds), book="GENERIC")
        ws.write(r, 0, ds, f_b)
        ws.write_number(r, 1, v[2], f_c)
        ws.write_number(r, 2, v[3], f_n1)
        ws.write_number(r, 3, v[4] / v[2] if v[2] else 0, f_r)
        if ds == LAUNCH:
            ws.write(r, 4, "<< LAUNCH", f_bb)
        d0 += dt.timedelta(days=1)
        r += 1

    # vs Control
    ws = sheet("vs Control", "Generic vs the untouched books",
               "Brand books received no structural or creative change — "
               "their movement is market + cycle. Generic's movement "
               "relative to theirs is the launch effect. All books "
               "exclude CA/FL campaigns for symmetry.",
               [24, 13, 13, 13, 13, 13, 13])
    for i, h in enumerate(["Book", "Spend/day pre", "Spend/day post",
                           "CPL pre", "CPL post", "ROAS pre", "ROAS post"]):
        ws.write(3, i, h, f_h)
    for r, bn in enumerate(books, start=4):
        p, q = S[(bn, wn_pre)], S[(bn, wn_post)]
        ws.write(r, 0, bn + ("  (control)" if bn != "Generic" else ""),
                 f_bb)
        for i, (val, fmt) in enumerate([(p["spend_d"], f_c),
                                        (q["spend_d"], f_c),
                                        (p["cpl"], f_c2), (q["cpl"], f_c2),
                                        (p["roas"], f_r), (q["roas"], f_r)],
                                       start=1):
            ws.write_number(r, i, val, fmt)
    ws.write(8, 0, "Difference-in-differences (Generic change minus "
             "Brand+Home change):", f_bb)
    ws.write(9, 0, "Cost per lead", f_b)
    ws.write(9, 1, did_cpl, f_p)
    ws.write(9, 2, "<= +10 pts = moving with the market", f_s)
    ws.write(10, 0, "Conversion rate", f_b)
    ws.write(10, 1, did_cvr, f_p)
    ws.write(10, 2, "quality guard — should hold near zero", f_s)

    # Cycle Normalizer
    ws = sheet("Cycle Normalizer", "What this phase of the spend cycle "
               "normally does",
               "The account's spend runs in ~42-day cycles (four measured "
               "since April). The post-launch window sits at the same "
               "phase of the current cycle; prior cycles supply the "
               "no-launch expectation.", [26, 24, 18, 18])
    for i, h in enumerate(["Cycle (trough date)", "Same-phase window",
                           "Generic spend vs cycle avg",
                           "Generic ROAS vs cycle avg"]):
        ws.write(3, i, h, f_h)
    r = 4
    for t, phw, ratio, droas in cyc_rows:
        ws.write(r, 0, t, f_b)
        ws.write(r, 1, f"{phw[0]} – {phw[1]}", f_b)
        ws.write_number(r, 2, ratio - 1, f_p)
        ws.write_number(r, 3, droas, f_r)
        r += 1
    ws.write(r, 0, "Expected (median of prior cycles)", f_bb)
    if exp_ratio is not None:
        ws.write_number(r, 2, exp_ratio - 1, f_p)
        ws.write_number(r, 3, exp_droas, f_r)
    r += 1
    ws.write(r, 0, "THIS cycle (launch inside it)", f_bb)
    ws.write(r, 1, f"{POST[0]} – {POST[1]}", f_b)
    ws.write_number(r, 2, cur_ratio - 1, f_p)
    ws.write_number(r, 3, cur_droas, f_r)
    r += 1
    if cyc_resid is not None:
        ws.write(r, 0, "Residual attributable to the launch (actual vs "
                 "expected)", f_bb)
        ws.write_number(r, 2, cyc_resid, f_p)
    if skipped:
        ws.write(r + 1, 0, f"Cycles too short to contain this phase, "
                 f"excluded: {', '.join(skipped)}", f_s)
    ws.write(r + 2, 0, "Caveats: prior-cycle sample is small; the "
             "Aug–Sep cycles include the exposure-collapse era; current "
             "cycle average is partial. Directional, not precise.", f_s)

    # Campaigns
    ws = sheet("Campaigns", "Every campaign's reaction (daily rates)",
               "Pre = Oct 3–12 (after the early-October keyword/state "
               "trims). Tier declines are largely their generic keywords "
               "leaving by design — see the migration block.",
               [30, 12, 12, 12, 12, 10, 10])
    for i, h in enumerate(["Campaign", "Spend/day pre", "Spend/day post",
                           "Leads/day pre", "Leads/day post", "ROAS pre",
                           "ROAS post"]):
        ws.write(3, i, h, f_h)
    r = 4
    for cid, nm in DISP.items():
        p = stats(agg(PRE10, cid=cid), ndays(*PRE10))
        q = stats(agg(POST, cid=cid), ndays(*POST))
        ws.write(r, 0, nm, f_bb)
        for i, (val, fmt) in enumerate([(p["spend_d"], f_c),
                                        (q["spend_d"], f_c),
                                        (p["leads_d"], f_n1),
                                        (q["leads_d"], f_n1),
                                        (p["roas"], f_r), (q["roas"], f_r)],
                                       start=1):
            ws.write_number(r, i, val, fmt)
        r += 1
    r += 1
    if t2:
        ws.write(r, 0, "Tier 2 split — Texas coverage moved here at "
                 "launch:", f_bb)
        r += 1
        for lbl, key in (("Texas (new inflow)", "tx"),
                         ("Everything else", "ex")):
            ws.write(r, 0, lbl, f_b)
            ws.write_number(r, 1, t2[(key, "pre")][0], f_c)
            ws.write_number(r, 2, t2[(key, "post")][0], f_c)
            ws.write_number(r, 3, t2[(key, "pre")][1], f_n1)
            ws.write_number(r, 4, t2[(key, "post")][1], f_n1)
            r += 1
    r += 1
    ws.write(r, 0, "Where generic spend lives (migration check, $/day):",
             f_bb)
    r += 1
    for cid, nm in DISP.items():
        ws.write(r, 0, nm, f_b)
        ws.write_number(r, 1, agg(PRE10, book="GENERIC", cid=cid)[2]
                        / ndays(*PRE10), f_c)
        ws.write_number(r, 2, agg(POST, book="GENERIC", cid=cid)[2]
                        / ndays(*POST), f_c)
        r += 1

    # Eligibility
    ws = sheet("Eligibility", "Nationwide Generic campaign — can it win "
               "auctions?",
               "Splits the volume question: low eligibility (losing on ad "
               "rank) = structural problem; eligible but spending lightly "
               "= the bidding controller being cautious, which self-"
               "resolves. Note: Google censors impression share below "
               "10%, so trajectory beats level.", [34, 16, 16])
    for i, h in enumerate(["Measure", "Pre-launch", "Post-launch"]):
        ws.write(3, i, h, f_h)
    if elig:
        rows = [("Impressions/day", elig["pre"]["im_d"],
                 elig["post"]["im_d"], f_n),
                ("Spend/day", elig["pre"]["co_d"], elig["post"]["co_d"],
                 f_c)]
        if elig["pre"]["is"] is not None:
            rows.append(("Impression share (impr-weighted)",
                         elig["pre"]["is"], elig["post"]["is"], f_p))
        if elig["pre"]["lr"] is not None:
            rows.append(("Share lost to AD RANK (impr-weighted)",
                         elig["pre"]["lr"], elig["post"]["lr"], f_p))
        for r, (nm, p, q, fmt) in enumerate(rows, start=4):
            ws.write(r, 0, nm, f_bb)
            ws.write_number(r, 1, p or 0, fmt)
            ws.write_number(r, 2, q or 0, fmt)
    else:
        ws.write(4, 0, "ad-group table not found — panel unavailable", f_b)

    # Checks & Caveats
    ws = sheet("Checks & Caveats", "Integrity checks and how to read "
               "this report", "", [40, 70])
    r = 3
    ws.write(r, 0, "Integrity checks", f_h)
    ws.write(r, 1, "Result", f_h)
    r += 1
    ws.write(r, 0, "Paused tier generic groups spending post-launch", f_b)
    ws.write(r, 1, f"${leak1:,.0f} (expect $0)" if leak1 is not None
             else "n/a", f_b)
    r += 1
    ws.write(r, 0, "Paused brand groups inside the nationwide campaign",
             f_b)
    ws.write(r, 1, f"${leak2:,.0f} (expect $0)" if leak2 is not None
             else "n/a", f_b)
    r += 2
    for cv in [
        "1. The post window sits in the spend cycle's down-phase "
        "(projected trough Oct 8–Nov 1) — raw declines are partly "
        "cycle; the Cycle Normalizer and vs Control sheets carry the "
        "correction.",
        "2. October lead-price repricing hits every book, including "
        "untouched ones.",
        "3. Early-October keyword/state trims sit inside both windows; "
        "Tier 1 was most affected.",
        "4. Texas coverage moved to Tier 2 at launch — its growth there "
        "is mechanical inflow (split shown on Campaigns).",
        "5. Protective negative keyword lists attached at launch tighten "
        "the generic query mix by design.",
        "6. The last ~2 days of leads/value still revise upward "
            "(conversion lag) — post-launch lead metrics are floors.",
        "7. California & Florida are excluded from all book-level math "
        "(ruled 10/21): untouched standalone campaigns pending their "
        "Phase 2 decision. The Campaigns sheet still shows them for "
        "reference.",
        ("8. Book addendum applied: " + str(add_n) + " keywords added to "
         "the account after the 8/27 freeze (manual Compare/Cost staging "
         "additions, identified 10/21 via quote_group_probe) are tagged "
         "GENERIC so volume is fully counted. The frozen 8/27 edition "
         "itself is unchanged.") if add_n else
        "8. Post-freeze manual keyword additions (~$350/day) are NOT "
        "book-tagged — volume metrics undercount; rerun with --addendum "
        "to include them."]:
        ws.write(r, 0, cv, f_b)
        ws.merge_range(r, 0, r, 1, cv, f_b)
        ws.set_row(r, 26)
        r += 1
    wb.close()
    print(f"\n[DONE] {path}")


if __name__ == "__main__":
    main()
