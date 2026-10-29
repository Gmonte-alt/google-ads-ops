#!/usr/bin/env python3
"""
phase1_adgroup_keyword_compare.py — pre/post comparison by ad group and
keyword for the Phase 1 generic consolidation. Diagnostic companion to
phase1_health_check.py (which stays the graded instrument).

Windows match the health check: PRE = Oct 3-12, POST = Oct 14-data max.
CA/FL excluded throughout (ruled 10/21).

Sheets (output/phase1_adgroup_keyword_compare.xlsx):
  Ad Groups — nationwide campaign ad groups at AD-GROUP grain (= the UI,
              keywordless spend included), pre vs post, plus the AI-Max/
              keywordless wedge row and the paused tier donor groups'
              pre-launch generic spend for reference.
  Keywords  — every keyword (text + match type) at CONSOLIDATION grain:
              pre summed across ALL non-CA/FL campaigns (column shows
              where it lived), post in the nationwide campaign. Book tag
              shown (GENERIC / addendum / other / untagged).
  Movers    — top spend gainers and decliners, and worst GP swings.

Usage: python phase1_adgroup_keyword_compare.py --db paid_search.db
       --assignment keyword_book_assignment_FROZEN_2026-08-27.csv
       [--addendum keyword_book_addendum_2026-10-21.csv] [--out output]
"""
import argparse
import csv
import os
import sqlite3
import sys
from collections import defaultdict

PRE = ("2026-10-03", "2026-10-12")
POST_START = "2026-10-14"
RULESET = "8/27+9/2a+9/2b+9/4-aggtail+9/8-prodintent"
SHORT = {23394808297: "NATL", 23496334752: "T1", 23530860942: "T2",
         23586637678: "T3", 23428389081: "T4"}
TAGCAMP = {23394808297: "TEXAS", 23496334752: "TIER1",
           23530860942: "TIER2", 23586637678: "TIER3",
           23428389081: "TIER4"}
NATL = 23394808297
TIER_IDS = [c for c in SHORT if c != NATL]


def die(m):
    print(f"*** HARD FAIL: {m}")
    sys.exit(1)


def ndays(a, b):
    import datetime as dt
    return (dt.date.fromisoformat(b) - dt.date.fromisoformat(a)).days + 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--assignment", required=True)
    ap.add_argument("--addendum", default=None)
    ap.add_argument("--out", default="output")
    a = ap.parse_args()
    con = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row

    # ---- tags ----
    f = open(a.assignment, encoding="utf-8-sig")
    if RULESET not in f.readline():
        die("frozen metadata mismatch")
    tags, text_tags = {}, {}
    for r in csv.DictReader(f):
        if r["decision"].startswith("EXCLUDE"):
            continue
        k = (r["keyword"].lower().strip(), r["match_type"].upper())
        tags[(r["campaign"],) + k] = r["book"]
        text_tags.setdefault(k, r["book"])
    add_keys = set()
    if a.addendum:
        fa = open(a.addendum, encoding="utf-8-sig")
        if "addendum" not in fa.readline().lower():
            die("addendum metadata missing")
        for r in csv.DictReader(fa):
            if r["decision"].startswith("EXCLUDE"):
                continue
            k = (r["keyword"].lower().strip(), r["match_type"].upper())
            tags.setdefault((r["campaign"],) + k, r["book"])
            text_tags.setdefault(k, r["book"])
            add_keys.add(k)

    # ---- instances in the five consolidation campaigns ----
    kic = {r[1] for r in con.execute("PRAGMA table_info(keyword_instances)")}
    agcol = next((x for x in ("ad_group_name", "adgroup_name", "ad_group")
                  if x in kic), None)
    inst = {}
    for r in con.execute(
            "SELECT id, campaign_id, keyword_text, match_type"
            + (f", {agcol}" if agcol else "") +
            " FROM keyword_instances WHERE account_id=1"):
        try:
            cid = int(r["campaign_id"])
        except (TypeError, ValueError):
            continue
        if cid not in SHORT:
            continue
        kw = (r["keyword_text"] or "").lower().strip()
        mt = (r["match_type"] or "").upper()
        inst[r["id"]] = (cid, kw, mt, r[agcol] if agcol else "")
    if not inst:
        die("no instances mapped")
    print(f"[SCOPE] {len(inst):,} instances across "
          f"{len(set(v[0] for v in inst.values()))} campaigns"
          + ("" if agcol else "  (no ad-group column on instances)"))

    def book_of(cid, kw, mt):
        bk = tags.get((TAGCAMP[cid], kw, mt)) or text_tags.get((kw, mt))
        if bk is None:
            return "UNTAGGED"
        if (kw, mt) in add_keys:
            return bk + " (addendum)"
        return bk

    # ---- keyword grain ----
    kc = {r[1] for r in con.execute(
        "PRAGMA table_info(keyword_performance_daily)")}
    dcol = "report_date" if "report_date" in kc else "date"
    filt = ""
    if "device" in kc:
        filt += "AND device='ALL' "
    if "geo_state" in kc:
        filt += "AND geo_state='ALL' "
    g_max = con.execute(f"SELECT MAX({dcol}) FROM keyword_performance_daily"
                        f" WHERE 1=1 {filt}").fetchone()[0]
    POST = (POST_START, g_max)
    nd_pre, nd_post = ndays(*PRE), ndays(*POST)
    print(f"[WINDOWS] PRE {PRE[0]}..{PRE[1]} ({nd_pre}d) | "
          f"POST {POST[0]}..{POST[1]} ({nd_post}d)")

    # kw -> {win: [co, cv, rv]}, plus pre campaign set and post ad group
    kwagg = defaultdict(lambda: {"pre": [0.0] * 3, "post": [0.0] * 3,
                                 "pre_camps": set(), "ag": ""})
    # natl per-keyword totals (for the per-group wedge)
    natl_kw_by_ag = defaultdict(lambda: {"pre": [0.0] * 3,
                                         "post": [0.0] * 3})
    ids = list(inst)
    for i in range(0, len(ids), 900):
        chunk = ids[i:i + 900]
        ph = ",".join("?" * len(chunk))
        for r in con.execute(
                f"SELECT instance_id iid, {dcol} d, "
                f"SUM(cost_micros)/1e6 co, SUM(primary_conv_count) cv, "
                f"SUM(primary_conv_value) rv "
                f"FROM keyword_performance_daily "
                f"WHERE instance_id IN ({ph}) {filt}AND {dcol} >= ? "
                f"GROUP BY 1,2", chunk + [PRE[0]]):
            cid, kw, mt, ag = inst[r["iid"]]
            win = ("pre" if PRE[0] <= r["d"] <= PRE[1] else
                   "post" if r["d"] >= POST[0] else None)
            if win is None:
                continue
            e = kwagg[(kw, mt)]
            for j, v in enumerate((r["co"], r["cv"], r["rv"])):
                e[win][j] += v or 0
            if win == "pre":
                e["pre_camps"].add(SHORT[cid])
            if cid == NATL:
                if win == "post":
                    e["ag"] = ag
                g = natl_kw_by_ag[ag]
                for j, v in enumerate((r["co"], r["cv"], r["rv"])):
                    g[win][j] += v or 0

    # ---- ad-group grain (nationwide + tier donors) ----
    tset = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table','view')")}
    agt = "ad_group_performance_daily"
    if agt not in tset:
        die("ad_group_performance_daily not found")
    ac = {r[1] for r in con.execute(f"PRAGMA table_info({agt})")}
    ad = "report_date" if "report_date" in ac else "date"
    af = "AND device='ALL' " if "device" in ac else ""
    nc = next(x for x in ("ad_group_name", "adgroup_name", "ad_group")
              if x in ac)
    cvc = next((x for x in ("primary_conv_count", "conversions")
                if x in ac), None)
    rvc = next((x for x in ("primary_conv_value", "conversion_value",
                            "conversions_value") if x in ac), None)
    has_ck = "clicks" in ac
    sel = "SUM(cost_micros)/1e6 co, SUM(impressions) im"
    if cvc and rvc:
        sel += f", SUM({cvc}) cv, SUM({rvc}) rv"
    if has_ck:
        sel += ", SUM(clicks) ck"

    def agp_by_group(cids, w):
        ph = ",".join(f"{c},'{c}'" for c in cids)
        out = {}
        for r in con.execute(
                f"SELECT {nc} nm, {sel} FROM {agt} WHERE campaign_id IN "
                f"({ph}) {af}AND {ad} BETWEEN ? AND ? GROUP BY 1", w):
            out[r["nm"]] = [r["co"] or 0, (r["cv"] or 0) if cvc else 0,
                            (r["rv"] or 0) if rvc else 0,
                            r["im"] or 0,
                            (r["ck"] or 0) if has_ck else 0]
        return out

    natl_pre = agp_by_group([NATL], PRE)
    natl_post = agp_by_group([NATL], POST)
    tier_pre = agp_by_group(TIER_IDS, PRE)
    tier_post = agp_by_group(TIER_IDS, POST)

    # ================= workbook =================
    import xlsxwriter
    os.makedirs(a.out, exist_ok=True)
    path = os.path.join(a.out, "phase1_adgroup_keyword_compare.xlsx")
    wb = xlsxwriter.Workbook(path)
    NAVY = "#1F3864"
    f_t = wb.add_format({"font_name": "Arial", "bold": True,
                         "font_size": 14, "font_color": NAVY})
    f_s = wb.add_format({"font_name": "Arial", "italic": True,
                         "font_size": 9, "font_color": "#808080",
                         "text_wrap": True, "valign": "top"})
    f_h = wb.add_format({"font_name": "Arial", "bold": True, "font_size": 9,
                         "font_color": "white", "bg_color": NAVY,
                         "text_wrap": True, "valign": "top"})
    f_b = wb.add_format({"font_name": "Arial", "font_size": 9})
    f_bb = wb.add_format({"font_name": "Arial", "font_size": 9,
                          "bold": True})
    f_c = wb.add_format({"font_name": "Arial", "font_size": 9,
                         "num_format": "$#,##0"})
    f_c2 = wb.add_format({"font_name": "Arial", "font_size": 9,
                          "num_format": "$#,##0.00"})
    f_n1 = wb.add_format({"font_name": "Arial", "font_size": 9,
                          "num_format": "#,##0.0"})
    f_r = wb.add_format({"font_name": "Arial", "font_size": 9,
                         "num_format": "0.00"})

    def sheet(name, title, sub, widths):
        ws = wb.add_worksheet(name)
        ws.write(0, 0, title, f_t)
        ws.write(1, 0, sub, f_s)
        for i, w in enumerate(widths):
            ws.set_column(i, i, w)
        ws.hide_gridlines(2)
        ws.freeze_panes(4, 0)
        return ws

    def rates(v, nd):
        co, cv, rv = v[0], v[1], v[2]
        return (co / nd, cv / nd, co / cv if cv else 0,
                rv / co if co else 0)

    # ---- Ad Groups sheet ----
    ws = sheet("Ad Groups", "Nationwide campaign — ad groups, pre vs post",
               f"Ad-group grain (= the UI; includes spend served without "
               f"a keyword). Pre {PRE[0]}–{PRE[1]}, post {POST[0]}–"
               f"{POST[1]}, daily rates. Donor block: tier generic groups "
               f"paused at launch. CA/FL excluded.",
               [38, 11, 11, 10, 10, 11, 11, 9, 9])
    hdrs = ["Ad group", "Spend/d pre", "Spend/d post", "Leads/d pre",
            "Leads/d post", "CPL pre", "CPL post", "ROAS pre", "ROAS post"]
    for i, h in enumerate(hdrs):
        ws.write(3, i, h, f_h)
    names = sorted(set(natl_pre) | set(natl_post),
                   key=lambda n: -(natl_post.get(n, [0])[0]))
    r = 4
    for nm in names:
        p = natl_pre.get(nm, [0, 0, 0, 0])
        q = natl_post.get(nm, [0, 0, 0, 0])
        if p[0] < 1 and q[0] < 1:
            continue
        pr, qr = rates(p, nd_pre), rates(q, nd_post)
        ws.write(r, 0, nm, f_bb)
        for i, (val, fmt) in enumerate(
                [(pr[0], f_c), (qr[0], f_c), (pr[1], f_n1), (qr[1], f_n1),
                 (pr[2], f_c2), (qr[2], f_c2), (pr[3], f_r), (qr[3], f_r)],
                start=1):
            ws.write_number(r, i, val, fmt)
        r += 1
    # wedge row: campaign agp total minus keyword-grain total
    tot_pre_ag = [sum(v[i] for v in natl_pre.values()) for i in range(3)]
    tot_post_ag = [sum(v[i] for v in natl_post.values()) for i in range(3)]
    kw_pre = [0.0] * 3
    kw_post = [0.0] * 3
    for g in natl_kw_by_ag.values():
        for i in range(3):
            kw_pre[i] += g["pre"][i]
            kw_post[i] += g["post"][i]
    r += 1
    ws.write(r, 0, "Spend without keyword attribution (AI Max etc.)",
             f_bb)
    ws.write_number(r, 1, (tot_pre_ag[0] - kw_pre[0]) / nd_pre, f_c)
    ws.write_number(r, 2, (tot_post_ag[0] - kw_post[0]) / nd_post, f_c)
    r += 2
    ws.write(r, 0, "Donor reference — tier generic groups (paused at "
             "launch):", f_bb)
    r += 1
    for nm in sorted(tier_pre, key=lambda n: -tier_pre[n][0]):
        if not nm.startswith("Home-Generic"):
            continue
        p = tier_pre.get(nm, [0, 0, 0, 0])
        q = tier_post.get(nm, [0, 0, 0, 0])
        if p[0] < 1 and q[0] < 1:
            continue
        pr, qr = rates(p, nd_pre), rates(q, nd_post)
        ws.write(r, 0, nm, f_b)
        for i, (val, fmt) in enumerate(
                [(pr[0], f_c), (qr[0], f_c), (pr[1], f_n1), (qr[1], f_n1),
                 (pr[2], f_c2), (qr[2], f_c2), (pr[3], f_r), (qr[3], f_r)],
                start=1):
            ws.write_number(r, i, val, fmt)
        r += 1

    # ---- Theme Capture sheet ----
    def theme_of(nm):
        if not nm.startswith("Home-Generic-"):
            return None
        t = nm[len("Home-Generic-"):]
        if t.endswith("-Core"):
            t = t[:-5]
        return {"Home Insurance": "Core", "Fire": "Peril"}.get(t, t)

    th = defaultdict(lambda: {"pre": [0.0] * 5, "post": [0.0] * 5})
    for src, win in ((natl_pre, "pre"), (tier_pre, "pre"),
                     (natl_post, "post"), (tier_post, "post")):
        for nm, v in src.items():
            t = theme_of(nm)
            if t is None:
                continue
            for i in range(5):
                th[t][win][i] += v[i]
    ws = sheet("Theme Capture", "Generic spend by theme — consolidation "
               "capture",
               f"Pre = the theme's generic ad groups across all five "
               f"campaigns ({PRE[0]}–{PRE[1]}); post = the nationwide "
               f"campaign ({POST[0]}–{POST[1]}). Daily rates, ad-group "
               f"grain. Capture = post spend as a share of pre. CA/FL "
               f"excluded.",
               [16, 11, 11, 9, 10, 10, 10, 10, 9, 9, 9, 9])
    hdrs = ["Theme", "Spend/d pre", "Spend/d post", "Capture",
            "Leads/d pre", "Leads/d post", "CPL pre", "CPL post",
            "CPC pre", "CPC post", "ROAS pre", "ROAS post"]
    for i, h in enumerate(hdrs):
        ws.write(3, i, h, f_h)
    f_p = wb.add_format({"font_name": "Arial", "font_size": 9,
                         "num_format": "0%"})
    r = 4
    tot = {"pre": [0.0] * 5, "post": [0.0] * 5}
    for t in sorted(th, key=lambda t: -th[t]["pre"][0]):
        p, q = th[t]["pre"], th[t]["post"]
        if p[0] < 1 and q[0] < 1:
            continue
        for i in range(5):
            tot["pre"][i] += p[i]
            tot["post"][i] += q[i]
        ws.write(r, 0, t, f_bb)
        vals = [(p[0] / nd_pre, f_c), (q[0] / nd_post, f_c),
                (q[0] / nd_post / (p[0] / nd_pre) if p[0] else 0, f_p),
                (p[1] / nd_pre, f_n1), (q[1] / nd_post, f_n1),
                (p[0] / p[1] if p[1] else 0, f_c2),
                (q[0] / q[1] if q[1] else 0, f_c2),
                (p[0] / p[4] if p[4] else 0, f_c2),
                (q[0] / q[4] if q[4] else 0, f_c2),
                (p[2] / p[0] if p[0] else 0, f_r),
                (q[2] / q[0] if q[0] else 0, f_r)]
        for i, (val, fmt) in enumerate(vals, start=1):
            ws.write_number(r, i, val, fmt)
        r += 1
    p, q = tot["pre"], tot["post"]
    ws.write(r, 0, "ALL GENERIC", f_bb)
    vals = [(p[0] / nd_pre, f_c), (q[0] / nd_post, f_c),
            (q[0] / nd_post / (p[0] / nd_pre) if p[0] else 0, f_p),
            (p[1] / nd_pre, f_n1), (q[1] / nd_post, f_n1),
            (p[0] / p[1] if p[1] else 0, f_c2),
            (q[0] / q[1] if q[1] else 0, f_c2),
            (p[0] / p[4] if p[4] else 0, f_c2),
            (q[0] / q[4] if q[4] else 0, f_c2),
            (p[2] / p[0] if p[0] else 0, f_r),
            (q[2] / q[0] if q[0] else 0, f_r)]
    for i, (val, fmt) in enumerate(vals, start=1):
        ws.write_number(r, i, val, fmt)
    if not has_ck:
        ws.write(r + 2, 0, "CPC columns unavailable: no clicks column on "
                 "the ad-group table.", f_s)

    # ---- Keywords sheet ----
    ws = sheet("Keywords", "Keywords — consolidation grain, pre vs post",
               "Pre = the keyword's spend across ALL five consolidation "
               "campaigns (column shows where it lived); post = the "
               "nationwide campaign. Keyword grain. Sorted by post spend. "
               "CA/FL excluded.",
               [34, 8, 20, 13, 14, 11, 11, 10, 10, 9, 9, 9, 9])
    hdrs = ["Keyword", "Match", "Book", "Pre lived in", "Post ad group",
            "Spend/d pre", "Spend/d post", "Leads/d pre", "Leads/d post",
            "CPL pre", "CPL post", "ROAS pre", "ROAS post"]
    for i, h in enumerate(hdrs):
        ws.write(3, i, h, f_h)
    rows = []
    for (kw, mt), e in kwagg.items():
        if e["pre"][0] < 1 and e["post"][0] < 1:
            continue
        rows.append((kw, mt, e))
    rows.sort(key=lambda t: (-t[2]["post"][0], -t[2]["pre"][0]))
    r = 4
    for kw, mt, e in rows:
        pr, qr = rates(e["pre"], nd_pre), rates(e["post"], nd_post)
        bk = book_of(NATL, kw, mt)
        ws.write(r, 0, kw, f_b)
        ws.write(r, 1, mt[:1] + mt[1:].lower(), f_b)
        ws.write(r, 2, bk, f_b)
        ws.write(r, 3, "+".join(sorted(e["pre_camps"])) or "—", f_b)
        ws.write(r, 4, e["ag"] or "—", f_b)
        for i, (val, fmt) in enumerate(
                [(pr[0], f_c), (qr[0], f_c), (pr[1], f_n1), (qr[1], f_n1),
                 (pr[2], f_c2), (qr[2], f_c2), (pr[3], f_r), (qr[3], f_r)],
                start=5):
            ws.write_number(r, i, val, fmt)
        r += 1

    # ---- Movers sheet ----
    ws = sheet("Movers", "Biggest movers, pre vs post (daily rates)",
               "Spend deltas at consolidation grain; GP swing = "
               "(value−cost)/day change. A keyword 'losing' spend here "
               "often shows the controller reallocating, not a defect — "
               "read with the ROAS columns on Keywords.",
               [34, 8, 12, 12, 12])
    for i, h in enumerate(["Keyword", "Match", "Spend/d pre",
                           "Spend/d post", "GP/d change"]):
        ws.write(3, i, h, f_h)

    def gp_d(e):
        return ((e["post"][2] - e["post"][0]) / nd_post
                - (e["pre"][2] - e["pre"][0]) / nd_pre)

    def spd(e):
        return e["post"][0] / nd_post - e["pre"][0] / nd_pre

    r = 4
    for title, sel_rows in (
            ("Top spend gainers", sorted(rows, key=lambda t:
                                         -spd(t[2]))[:10]),
            ("Top spend decliners", sorted(rows, key=lambda t:
                                           spd(t[2]))[:10]),
            ("Worst GP swings", sorted(rows, key=lambda t:
                                       gp_d(t[2]))[:10])):
        ws.write(r, 0, title, f_bb)
        r += 1
        for kw, mt, e in sel_rows:
            ws.write(r, 0, kw, f_b)
            ws.write(r, 1, mt[:1] + mt[1:].lower(), f_b)
            ws.write_number(r, 2, e["pre"][0] / nd_pre, f_c)
            ws.write_number(r, 3, e["post"][0] / nd_post, f_c)
            ws.write_number(r, 4, gp_d(e), f_c)
            r += 1
        r += 1

    # ---- Quality Score sheet (house canon: keyword_quality_daily) ----
    # Architect ruling: QS is NEVER read off keyword_performance_daily
    # (its historical_* columns are dead by design). QS lives in its own
    # table, rated-rows-only semantics.
    qt = "keyword_quality_daily" if "keyword_quality_daily" in tset \
        else None
    if qt is None:
        for t in sorted(tset):
            if t == "keyword_performance_daily":
                continue
            cols = {c[1].lower() for c in
                    con.execute(f"PRAGMA table_info({t})")}
            if any("quality" in c or c == "qs" for c in cols) \
                    and "instance_id" in cols \
                    and ("report_date" in cols or "date" in cols):
                qt = t
                break
    ws = sheet("Quality Score", "Quality score — impression-weighted, "
               "rated rows only",
               "House canon: iw-QS = SUM(QS x impressions)/SUM("
               "impressions) over rated keyword-days; a missing day "
               "means Google didn't rate it, not missing data. Rated "
               "share = rated impressions / all impressions — read it "
               "beside every QS. Caveat: rated coverage collapsed with "
               "consolidation (~130K to ~9K rated keyword-days/month), "
               "so trends across that boundary are partly composition; "
               "weight keywords rated in BOTH windows.",
               [34, 8, 26, 8, 8, 8, 11, 11])
    hdrs = ["Keyword", "Match", "Ad group", "QS pre", "QS post",
            "Change", "Rated% pre", "Rated% post"]
    for i, h in enumerate(hdrs):
        ws.write(3, i, h, f_h)
    if qt is None:
        ws.write(4, 0, "No QS table found in the database.", f_b)
        print("[QS] no QS table in DB")
    else:
        qc = {c[1].lower(): c[1] for c in
              con.execute(f"PRAGMA table_info({qt})")}
        qcol = next((qc[c] for c in qc
                     if "quality" in c or c == "qs"), None)
        qdt = qc.get("report_date") or qc.get("date")
        qim = qc.get("impressions")
        natl_ids = [i for i, v in inst.items() if v[0] == NATL]
        # total impressions per instance from kpd (for rated share)
        tot_im = defaultdict(lambda: [0.0, 0.0])
        has_kpd_im = "impressions" in kc
        qagg = defaultdict(lambda: [0.0] * 6)  # wq_pre, im_pre, wq_post,
        #                                         im_post, n_pre, n_post
        for i in range(0, len(natl_ids), 900):
            chunk = natl_ids[i:i + 900]
            ph = ",".join("?" * len(chunk))
            if has_kpd_im:
                for r in con.execute(
                        f"SELECT instance_id iid, "
                        f"SUM(CASE WHEN {dcol} BETWEEN ? AND ? THEN "
                        f"impressions ELSE 0 END) ip, "
                        f"SUM(CASE WHEN {dcol} >= ? THEN impressions "
                        f"ELSE 0 END) iq "
                        f"FROM keyword_performance_daily "
                        f"WHERE instance_id IN ({ph}) {filt}GROUP BY 1",
                        [*PRE, POST[0]] + chunk):
                    if r["iid"] is not None:
                        tot_im[r["iid"]] = [r["ip"] or 0, r["iq"] or 0]
            w = f"*{qim}" if qim else ""
            d = qim if qim else "1"
            for r in con.execute(
                    f"SELECT instance_id iid, "
                    f"SUM(CASE WHEN {qdt} BETWEEN ? AND ? THEN "
                    f"{qcol}{w} END) wp, "
                    f"SUM(CASE WHEN {qdt} BETWEEN ? AND ? THEN {d} "
                    f"END) dp, "
                    f"SUM(CASE WHEN {qdt} >= ? THEN {qcol}{w} END) wq, "
                    f"SUM(CASE WHEN {qdt} >= ? THEN {d} END) dq "
                    f"FROM {qt} WHERE instance_id IN ({ph}) "
                    f"AND {qcol} > 0 GROUP BY 1",
                    [*PRE, *PRE, POST[0], POST[0]] + chunk):
                cid, kw, mt, ag = inst[r["iid"]]
                g = qagg[(kw, mt, ag)]
                g[0] += r["wp"] or 0
                g[1] += r["dp"] or 0
                g[2] += r["wq"] or 0
                g[3] += r["dq"] or 0
                ti = tot_im.get(r["iid"], [0, 0])
                g[4] += ti[0]
                g[5] += ti[1]
        qrows = []
        for (kw, mt, ag), g in qagg.items():
            qp = g[0] / g[1] if g[1] else None
            qq = g[2] / g[3] if g[3] else None
            rsp = g[1] / g[4] if (qim and g[4]) else None
            rsq = g[3] / g[5] if (qim and g[5]) else None
            qrows.append((kw, mt, ag, qp, qq, rsp, rsq))
        qrows.sort(key=lambda q: ((q[4] - q[3]) if (q[3] and q[4])
                                  else 99))
        f_pq = wb.add_format({"font_name": "Arial", "font_size": 9,
                              "num_format": "0%"})
        r = 4
        # account-level summary first
        sw = [sum(g[i] for g in qagg.values()) for i in range(6)]
        ws.write(r, 0, "NATIONWIDE CAMPAIGN (all rated keywords)", f_bb)
        if sw[1]:
            ws.write_number(r, 3, sw[0] / sw[1], f_n1)
        if sw[3]:
            ws.write_number(r, 4, sw[2] / sw[3], f_n1)
        if sw[1] and sw[3]:
            ws.write_number(r, 5, sw[2] / sw[3] - sw[0] / sw[1], f_n1)
        if qim and sw[4]:
            ws.write_number(r, 6, sw[1] / sw[4], f_pq)
        if qim and sw[5]:
            ws.write_number(r, 7, sw[3] / sw[5], f_pq)
        r += 2
        both = 0
        for kw, mt, ag, qp, qq, rsp, rsq in qrows:
            ws.write(r, 0, kw, f_b)
            ws.write(r, 1, mt[:1] + mt[1:].lower(), f_b)
            ws.write(r, 2, ag or "—", f_b)
            if qp is not None:
                ws.write_number(r, 3, qp, f_n1)
            if qq is not None:
                ws.write_number(r, 4, qq, f_n1)
            if qp is not None and qq is not None:
                ws.write_number(r, 5, qq - qp, f_n1)
                both += 1
            if rsp is not None:
                ws.write_number(r, 6, rsp, f_pq)
            if rsq is not None:
                ws.write_number(r, 7, rsq, f_pq)
            r += 1
        iwp = f"{sw[0]/sw[1]:.1f}" if sw[1] else "n/a"
        iwq = f"{sw[2]/sw[3]:.1f}" if sw[3] else "n/a"
        print(f"[QS] {qt}.{qcol} — {len(qrows)} keywords rated "
              f"({both} in both windows) | campaign iw-QS pre {iwp} "
              f"-> post {iwq}")

    wb.close()
    print(f"[KEYWORDS] {len(rows):,} with spend in either window")
    print(f"[DONE] {path}")


if __name__ == "__main__":
    main()
