"""
ms_expansion_postlaunch.py -- month-1 evaluation of the 9/18 MS Home generic
expansion. READ-ONLY. Produces the keep/kill + negatives + wave-2 evidence pack.

WINDOWS
  launch pollution 9/18..9/21 (syndicated partners took 57-83% of clicks) is
  EXCLUDED from all decision metrics; clean window defaults to 9/22..10/18.
  Oct-12 revenue is adjusted in-memory by --adjust-factor (default 2.0 per the
  observed one-day RPL dip); every topline prints RAW and ADJUSTED. Use
  --adjust-factor 0 to EXCLUDE 10/12 from rate metrics instead; 1.0 = no touch.

READS
  cohort   = keyword_instances first seen at launch (first_seen_date >= --launch-date)
             in the six live campaigns -- catches the new groups AND the adds
             into existing Core groups
  perf     = keyword_performance_daily (device-ALL aware), revenue columns introspected
  book     = incumbent acct-2 generic instances (same window) as the benchmark
  queries  = sqp (platform='microsoft') if rows exist in-window, else ms_search_terms

DECISION RULES (book-relative; floors keep noise from masquerading as verdicts)
  PAUSE_CANDIDATE  0 leads AND spend >= pause_mult x book CPL        (default 2.0x)
  REVIEW_CPL       >=2 leads AND CPL > 1.75x book CPL
  PROMOTE_EXACT    phrase, >=10 leads, CPL <= 1.1x book  (the 'exact is earned' rule)
  SCALE_WATCH      >=3 leads AND ROAS >= book ROAS
  LEARNING         everything else -- no action at month 1

ALSO
  query review -> Editor-ready negative candidates (brand via brand_tagger -> route
  through the QW carrier-negatives worksheet; cross-vertical/unqualified -> direct),
  expansion-term capture before/after (brand-broad bleed validation), wave-2
  GO/HOLD gate on the Quote + Companies groups.

    python ms_expansion_postlaunch.py
    python ms_expansion_postlaunch.py --inspect-schema
"""
import argparse
import csv
import os
import re
import sqlite3
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ms_expansion_coverage import normalize_09, normalize_loose, load_tagger

ACCT = 2
CROSS_RE = re.compile(r"\b(life|renters?|business|commercial|medicare|health|pet|auto only|car only)\b")
HOME_RE = re.compile(r"\b(home|homes|house|houses|homeowner|homeowners|property|condo|dwelling|hazard|flood|fire|mobile|manufactured)\b")
NEW_THEMES = ("Home-Generic-Quote", "Home-Generic-Compare", "Home-Generic-Cost-Core",
              "Home-Generic-Companies", "Home-Generic-Other",
              "Home-Generic-Home Auto Bundle-Core", "Home-Generic-Home Auto Bundle-Quote")


def cols_of(conn, t):
    try:
        return [r[1] for r in conn.execute("PRAGMA table_info(%s)" % t)]
    except sqlite3.OperationalError:
        return []


def pick(cands, present, what, fatal=True):
    c = next((x for x in cands if x in present), None)
    if c is None and fatal:
        sys.exit("SCHEMA ERROR: none of %s found for %s. Present: %s" % (cands, what, present))
    return c


def fmt(x, money=False):
    if x is None:
        return ""
    return ("$%s" % f"{x:,.2f}") if money else f"{x:,.0f}"


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--db", default=r"C:\Projects\google-ads-ops\db\paid_search.db")
    p.add_argument("--launch-date", default="2026-09-17")
    p.add_argument("--clean-start", default="2026-09-22")
    p.add_argument("--end", default="2026-10-18")
    p.add_argument("--adjust-date", default="2026-10-12")
    p.add_argument("--adjust-factor", type=float, default=2.0,
                   help="multiply that date's revenue by this (2.0 default; 0 = exclude the day; 1 = no touch)")
    p.add_argument("--pollution", choices=["auto", "network", "fence"], default="auto",
                   help="launch-day handling: 'network' filters syndicated-partner ROWS out and "
                        "keeps the launch days (needs a network column in kpd); 'fence' excludes "
                        "launch-date..clean-start by DATE; 'auto' = network when the column exists, "
                        "else fence")
    p.add_argument("--pause-mult", type=float, default=2.0)
    p.add_argument("--inspect-schema", action="store_true")
    p.add_argument("--out", default=r"C:\Projects\google-ads-ops\analysis\ms-home-kw-expansion"
                                    r"\output\ms_expansion_postlaunch_%s.xlsx" % date.today().isoformat())
    a = p.parse_args()

    conn = sqlite3.connect("file:%s?mode=ro" % a.db, uri=True)
    conn.row_factory = sqlite3.Row
    kpd_c = cols_of(conn, "keyword_performance_daily")
    ki_c = cols_of(conn, "keyword_instances")
    if a.inspect_schema:
        print("kpd:", kpd_c); print("ki:", ki_c)
        print("ms_search_terms:", cols_of(conn, "ms_search_terms"))
        print("sqp:", cols_of(conn, "search_query_performance"))
        return
    impr = pick(["impressions", "impr"], kpd_c, "impressions")
    clicks = pick(["clicks"], kpd_c, "clicks")
    cost = pick(["cost_micros", "cost"], kpd_c, "cost")
    leads = pick(["primary_conv_count", "conversions"], kpd_c, "leads")
    rev = pick(["primary_conv_value", "conv_value", "revenue"], kpd_c, "revenue")
    dt = pick(["report_date", "date"], kpd_c, "date")
    dev = pick(["device"], kpd_c, "device")
    fs = pick(["first_seen_date", "first_seen"], ki_c, "first_seen")
    cost_div = 1e6 if cost == "cost_micros" else 1.0
    devs = {(r[0] or "").upper() for r in conn.execute(
        "SELECT DISTINCT %s FROM keyword_performance_daily LIMIT 200" % dev)}
    dev_sql = " AND kpd.%s='ALL'" % dev if "ALL" in devs else ""

    # ---- pollution handling: network filter (preferred) or date fence (fallback) ----
    NET_CANDS = ["network", "ad_network", "ad_network_type", "network_type",
                 "top_level_network", "ad_distribution", "network_name"]
    SYND_RE = re.compile(r"synd|partner", re.I)
    ROLL_RE = re.compile(r"^(all|total)$", re.I)
    net = next((c for c in NET_CANDS if c in kpd_c), None)
    net_sql, net_active, synd_vals = "", False, []
    if a.pollution in ("auto", "network") and net:
        vals = [r[0] for r in conn.execute(
            "SELECT DISTINCT %s FROM keyword_performance_daily" % net) if r[0]]
        synd_vals = [v for v in vals if SYND_RE.search(v)]
        kept = [v for v in vals if not SYND_RE.search(v) and not ROLL_RE.match(v.strip())]
        if synd_vals and kept:
            qvals = ", ".join("'%s'" % v.replace("'", "''") for v in kept)
            net_sql = " AND kpd.%s IN (%s)" % (net, qvals)
            net_active = True
            print("network filter ON (column %s): keeping %s | excluding %s" % (net, kept, synd_vals))
        else:
            print("network column '%s' present but values unusable (%s) -> date fence instead."
                  % (net, vals))
    if not net_active:
        if a.pollution == "network":
            sys.exit("ABORT: --pollution network requested but kpd has no usable network column "
                     "(candidates: %s). Re-export the keyword report WITH Network segmentation and "
                     "reload via 04, or run --pollution fence." % NET_CANDS)
        print("DATE FENCE active: %s..%s excluded from decisions. To include launch-day O&O data "
              "instead, re-export the keyword report with Network segmentation + reload via 04."
              % (a.launch_date, a.clean_start))
    dstart = a.launch_date if net_active else a.clean_start

    live = {r["campaign_id"]: r["campaign_name"] for r in conn.execute(
        "SELECT campaign_id, campaign_name FROM campaigns WHERE account_id=? AND status='ENABLED'", (ACCT,))}
    ph_c = ",".join("?" * len(live))

    cohort = conn.execute(
        "SELECT ki.id, ki.keyword_text, ki.match_type, ki.status, ki.ad_group_name, "
        "ki.campaign_id FROM keyword_instances ki WHERE ki.account_id=? AND ki.%s>=? "
        "AND ki.campaign_id IN (%s)" % (fs, ph_c),
        [ACCT, a.launch_date] + list(live)).fetchall()
    if not cohort:
        sys.exit("No launch-cohort instances found (first_seen >= %s in live campaigns). "
                 "Check --launch-date against the actual sync dates." % a.launch_date)
    cohort_ids = [r["id"] for r in cohort]
    meta = {r["id"]: r for r in cohort}
    kw_texts = {normalize_09(r["keyword_text"]) for r in cohort}
    print("launch cohort: %d instances / %d distinct keywords / %d ad groups (first_seen >= %s)"
          % (len(cohort), len(kw_texts),
             len({(r["campaign_id"], r["ad_group_name"]) for r in cohort}), a.launch_date))

    def pull(ids_sql, params, where_extra=""):
        q = ("SELECT kpd.instance_id iid, kpd.%s d, SUM(kpd.%s) impr, SUM(kpd.%s) clicks, "
             "SUM(kpd.%s)/%f cost, SUM(kpd.%s) leads, SUM(kpd.%s) rev "
             "FROM keyword_performance_daily kpd JOIN keyword_instances ki ON ki.id=kpd.instance_id "
             "WHERE ki.account_id=%d AND kpd.%s>=? AND kpd.%s<=?%s%s %s GROUP BY 1,2"
             % (dt, impr, clicks, cost, cost_div, leads, rev, ACCT, dt, dt, dev_sql, net_sql, ids_sql))
        return conn.execute(q, params).fetchall()

    ph_i = ",".join("?" * len(cohort_ids))
    rows = pull("AND kpd.instance_id IN (%s)" % ph_i,
                [dstart, a.end] + cohort_ids)
    # incumbent generic book benchmark (exclude cohort instances), SAME window + filter
    book_rows = pull("AND ki.campaign_id IN (%s) AND kpd.instance_id NOT IN (%s) "
                     "AND lower(ki.ad_group_name) NOT LIKE '%%non-generic%%'"
                     % (ph_c, ph_i),
                     [dstart, a.end] + list(live) + cohort_ids)
    # the excluded slice, cohort only (context, never decisions)
    pol = [0, 0, 0.0, 0.0, 0.0]
    if net_active:
        qvals = ", ".join("'%s'" % v.replace("'", "''") for v in synd_vals)
        q = ("SELECT SUM(kpd.%s), SUM(kpd.%s), SUM(kpd.%s)/%f, SUM(kpd.%s), SUM(kpd.%s) "
             "FROM keyword_performance_daily kpd JOIN keyword_instances ki ON ki.id=kpd.instance_id "
             "WHERE ki.account_id=%d AND kpd.%s>=? AND kpd.%s<=?%s AND kpd.%s IN (%s) "
             "AND kpd.instance_id IN (%s)"
             % (impr, clicks, cost, cost_div, leads, rev, ACCT, dt, dt, dev_sql, net, qvals, ph_i))
        r0 = conn.execute(q, [a.launch_date, a.end] + cohort_ids).fetchone()
        pol = [r0[0] or 0, r0[1] or 0, r0[2] or 0, r0[3] or 0, r0[4] or 0]
    else:
        pollute = pull("AND kpd.instance_id IN (%s)" % ph_i,
                       [a.launch_date, a.clean_start] + cohort_ids)
        for r in pollute:
            if r["d"] < a.clean_start:
                pol[0] += r["impr"] or 0; pol[1] += r["clicks"] or 0
                pol[2] += r["cost"] or 0; pol[3] += r["leads"] or 0; pol[4] += r["rev"] or 0

    def adjust(rws):
        raw_rev = sum(r["rev"] or 0 for r in rws)
        out = []
        for r in rws:
            rr = dict(r)
            if rr["d"] == a.adjust_date:
                if a.adjust_factor == 0:
                    continue
                rr["rev"] = (rr["rev"] or 0) * a.adjust_factor
            out.append(rr)
        return out, raw_rev, sum(r["rev"] or 0 for r in out)

    rows, raw_rev_c, adj_rev_c = adjust(rows)
    book_rows, raw_rev_b, adj_rev_b = adjust(book_rows)
    note_adj = ("Oct-12 revenue x%.1f (in-memory)" % a.adjust_factor if a.adjust_factor not in (0, 1)
                else ("Oct-12 EXCLUDED from metrics" if a.adjust_factor == 0 else "no Oct-12 adjustment"))
    win_note = ("NETWORK-FILTERED: syndicated rows excluded, launch days included" if net_active
                else "DATE-FENCED: %s..%s excluded" % (a.launch_date, a.clean_start))
    print("window %s..%s | %s | %s" % (dstart, a.end, win_note, note_adj))
    print("cohort rev raw $%.0f -> used $%.0f | book rev raw $%.0f -> used $%.0f"
          % (raw_rev_c, adj_rev_c, raw_rev_b, adj_rev_b))

    def agg(rws, keyfn):
        d = defaultdict(lambda: [0, 0, 0.0, 0.0, 0.0])
        for r in rws:
            k = keyfn(r)
            if k is None:
                continue
            t = d[k]
            t[0] += r["impr"] or 0; t[1] += r["clicks"] or 0; t[2] += r["cost"] or 0
            t[3] += r["leads"] or 0; t[4] += r["rev"] or 0
        return d

    def rates(t):
        im, cl, co, ld, rv = t
        return {"impr": im, "clicks": cl, "cost": co, "leads": ld, "rev": rv,
                "CTR%": 100 * cl / im if im else 0, "CPC": co / cl if cl else 0,
                "CvR%": 100 * ld / cl if cl else 0, "CPL": co / ld if ld else None,
                "RPL": rv / ld if ld else None, "ROAS%": 100 * rv / co if co else 0}

    book = rates([sum(x) for x in zip(*agg(book_rows, lambda r: "b").values())] or [0, 0, 0, 0, 0])
    print("\nBENCHMARK incumbent generic book: spend %s | leads %s | CPL %s | CvR %.1f%% | ROAS %.0f%%"
          % (fmt(book["cost"], 1), fmt(book["leads"]), fmt(book["CPL"], 1) or "n/a",
             book["CvR%"], book["ROAS%"]))

    # ---- group scorecard ----
    def gname(iid):
        return meta[iid]["ad_group_name"]
    g_agg = agg(rows, lambda r: (gname(r["iid"]), live.get(meta[r["iid"]]["campaign_id"], "?")))
    roll = agg(rows, lambda r: gname(r["iid"]))
    score, score_detail = [], []
    for g in sorted(roll, key=lambda x: -roll[x][2]):
        m = rates(roll[g])
        score.append(dict(ad_group=g, **{k: round(v, 2) if isinstance(v, float) else v
                                         for k, v in m.items()},
                          CPL_vs_book=round(m["CPL"] / book["CPL"], 2) if m["CPL"] and book["CPL"] else "",
                          ROAS_vs_book=round(m["ROAS%"] / book["ROAS%"], 2) if book["ROAS%"] and m["ROAS%"] else ""))
    for (g, c), t in sorted(g_agg.items()):
        m = rates(t)
        score_detail.append(dict(ad_group=g, campaign=c[:44],
                                 **{k: round(v, 2) if isinstance(v, float) else v for k, v in m.items()}))
    print("\nNEW-GROUP SCORECARD (clean window, rolled across campaigns):")
    print("  %-40s %9s %7s %6s %8s %8s %7s" % ("ad group", "spend", "clicks", "leads", "CPL", "ROAS%", "vs-book"))
    for s in score:
        print("  %-40s %9s %7s %6s %8s %7.0f %7s"
              % (s["ad_group"][:40], fmt(s["cost"], 1), fmt(s["clicks"]), fmt(s["leads"]),
                 fmt(s["CPL"], 1) or "--", s["ROAS%"], s["CPL_vs_book"] or "--"))

    # ---- keyword decisions ----
    k_agg = agg(rows, lambda r: (gname(r["iid"]), meta[r["iid"]]["keyword_text"],
                                 meta[r["iid"]]["match_type"]))
    kw_rows = []
    counts = defaultdict(int)
    for (g, kw, mt), t in sorted(k_agg.items(), key=lambda kv: -kv[1][2]):
        m = rates(t)
        dec = "LEARNING"
        why = "insufficient signal at month 1"
        if book["CPL"]:
            if m["leads"] == 0 and m["cost"] >= a.pause_mult * book["CPL"]:
                dec, why = "PAUSE_CANDIDATE", "0 leads on %.1fx book-CPL spend" % (m["cost"] / book["CPL"])
            elif m["leads"] >= 2 and m["CPL"] and m["CPL"] > 1.75 * book["CPL"]:
                dec, why = "REVIEW_CPL", "CPL %.2fx book on %d leads" % (m["CPL"] / book["CPL"], m["leads"])
            elif mt.lower() == "phrase" and m["leads"] >= 10 and m["CPL"] and m["CPL"] <= 1.1 * book["CPL"]:
                dec, why = "PROMOTE_EXACT", "earned exact: %d leads at %.2fx book CPL" % (m["leads"], m["CPL"] / book["CPL"])
            elif m["leads"] >= 3 and m["ROAS%"] >= book["ROAS%"]:
                dec, why = "SCALE_WATCH", "ROAS at/above book on %d leads" % m["leads"]
        counts[dec] += 1
        kw_rows.append(dict(ad_group=g, keyword=kw, match_type=mt, decision=dec, why=why,
                            **{k: round(v, 2) if isinstance(v, float) else v for k, v in m.items()}))
    print("\nKEYWORD DECISIONS: " + " | ".join("%s %d" % (k, v) for k, v in sorted(counts.items())))
    for r in kw_rows:
        if r["decision"] not in ("LEARNING",):
            print("  %-16s %-34s %-7s %-38s spend %s leads %s"
                  % (r["decision"], r["keyword"][:34], r["match_type"], r["why"][:38],
                     fmt(r["cost"], 1), fmt(r["leads"])))

    # ---- query review (negative candidates) + term capture ----
    q_src, q_rows, q_note = None, [], ""
    mst_c = cols_of(conn, "ms_search_terms")
    sqp_c = cols_of(conn, "search_query_performance")
    if mst_c:
        q_src = "ms_search_terms"     # preferred: carries network + normalized text
    elif sqp_c and conn.execute("SELECT 1 FROM search_query_performance WHERE platform='microsoft' "
                                "LIMIT 1").fetchone():
        q_src = "sqp"
    tagger, _ = load_tagger(None, a.db)
    neg_rows, capture = [], []
    if q_src == "ms_search_terms":
        term = pick(["normalized_search_term", "search_term"], mst_c, "term")
        qkw = pick(["normalized_keyword", "keyword"], mst_c, "kw")
        qdt = pick(["report_date", "month", "date"], mst_c, "date")
        qim = pick(["impressions"], mst_c, "impr")
        qcl = pick(["clicks"], mst_c, "clicks")
        qsp = pick(["spend_usd", "spend", "cost", "cost_micros"], mst_c, "spend", fatal=False)
        qag = pick(["ad_group_name", "ad_group"], mst_c, "ad group", fatal=False)
        qnet = next((c for c in NET_CANDS if c in mst_c), None)
        mst_net = a.pollution in ("auto", "network") and qnet is not None
        qstart = a.launch_date if mst_net else a.clean_start
        sel = [term, qkw, qdt, qim, qcl]
        i_sp = len(sel) if qsp else None
        if qsp:
            sel.append(qsp)
        i_ag = len(sel) if qag else None
        if qag:
            sel.append(qag)
        i_nt = len(sel) if mst_net else None
        if mst_net:
            sel.append(qnet)
        post = defaultdict(lambda: [0, 0, 0.0, set()])
        pre_abs, post_abs = defaultdict(lambda: defaultdict(int)), defaultdict(lambda: defaultdict(int))
        for r in conn.execute("SELECT %s FROM ms_search_terms" % ", ".join(sel)):
            if i_nt is not None and r[i_nt] and SYND_RE.search(str(r[i_nt])):
                continue
            q, k, d = normalize_09(r[0] or ""), normalize_09(r[1] or ""), str(r[2])[:10]
            im, cl = r[3] or 0, r[4] or 0
            sp = (r[i_sp] or 0) / (1e6 if qsp == "cost_micros" else 1) if i_sp is not None else 0
            ag = r[i_ag] if i_ag is not None else ""
            if q in kw_texts:
                if d >= qstart:
                    post_abs[q][k] += cl or im
                elif d < a.launch_date:
                    pre_abs[q][k] += cl or im
            if d >= qstart and k in kw_texts:
                t = post[(q, ag if qag else "")]
                t[0] += im; t[1] += cl; t[2] += sp; t[3].add(k)
        q_note = ("ms_search_terms, matched by normalized text; spend %s; %s"
                  % ("included" if qsp else "NOT in table",
                     "network-filtered, query window from %s" % qstart if mst_net
                     else "no network column -> query window from %s (date-fenced)" % qstart))
        for (q, ag), t in sorted(post.items(), key=lambda kv: -(kv[1][2] or kv[1][1])):
            brand = ""
            if tagger:
                brand, _, _, _, _ = tagger.tag(q)
            cls = ("BRAND" if brand else
                   "CROSS_VERTICAL" if CROSS_RE.search(" %s " % q) else
                   "UNQUALIFIED" if not HOME_RE.search(" %s " % q) else "GENERIC_OK")
            row = dict(query=q, ad_group=ag, cls=cls, brand=brand, impr=t[0], clicks=t[1],
                       spend=round(t[2], 2), via="; ".join(sorted(t[3]))[:60])
            q_rows.append(row)
            if cls != "GENERIC_OK" and (t[1] >= 1 or t[0] >= 20):
                neg_rows.append(dict(level="Ad Group" if ag else "Campaign (all six)",
                                     ad_group=ag, negative_keyword=q, match="Phrase",
                                     reason=cls + (": %s -> route via QW carrier-negatives worksheet" % brand
                                                   if cls == "BRAND" else ""),
                                     clicks=t[1], spend=row["spend"]))
        for q in sorted(kw_texts):
            pre_k = sorted(pre_abs.get(q, {}).items(), key=lambda kv: -kv[1])[:2]
            post_k = sorted(post_abs.get(q, {}).items(), key=lambda kv: -kv[1])[:2]
            if pre_k or post_k:
                capture.append(dict(term=q,
                                    pre_absorbers="; ".join(k for k, _ in pre_k),
                                    post_absorbers="; ".join(k for k, _ in post_k),
                                    now_self="YES" if any(k == q for k, _ in post_k) else ""))
        self_n = sum(1 for c in capture if c["now_self"])
        print("\nQUERY REVIEW (%s): %d distinct queries in cohort groups | negative candidates: %d"
              % (q_note, len(q_rows), len(neg_rows)))
        print("TERM CAPTURE (brand-bleed validation): %d/%d expansion terms now caught by their own "
              "keyword post-launch (vs brand/other broads before)" % (self_n, len(capture)))
    else:
        q_note = ("sqp platform='microsoft'" if q_src == "sqp" else "NO query source found")
        print("\nQUERY REVIEW: source=%s -- paste --inspect-schema output and I'll wire the sqp path."
              % q_note)

    # ---- wave-2 gate ----
    def grp_leads(prefix):
        return sum(roll[g][3] for g in roll if g.startswith(prefix)), \
               sum(roll[g][2] for g in roll if g.startswith(prefix))
    for nm in ("Home-Generic-Quote", "Home-Generic-Companies"):
        ld, sp = grp_leads(nm)
        cpl = sp / ld if ld else None
        ok = ld >= 10 and (cpl is None or (book["CPL"] and cpl <= 1.5 * book["CPL"]))
        print("WAVE-2 GATE %-26s leads=%s CPL=%s -> %s"
              % (nm, fmt(ld), fmt(cpl, 1) or "--",
                 "GO (migrate serving terms)" if ok else "HOLD (insufficient data or CPL out of band)"))
    lbl = ("EXCLUDED SYNDICATED SLICE %s..%s" % (dstart, a.end)) if net_active \
          else ("POLLUTED LAUNCH DAYS %s..%s (date-fenced)" % (a.launch_date, a.clean_start))
    print("%s -- context only, never in decisions: spend %s, %s clicks, %s leads"
          % (lbl, fmt(pol[2], 1), fmt(pol[1]), fmt(pol[3])))
    if net_active:
        print("sensitivity: re-run with --pollution fence to drop %s..%s by date instead; "
              "decisions should not flip between the two reads."
              % (a.launch_date, a.clean_start))

    # ---- workbook + CSVs ----
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.utils import get_column_letter
    HEADF = PatternFill("solid", fgColor="1F3864")
    WHITE = Font(name="Arial", bold=True, color="FFFFFF", size=10)
    BOLD = Font(name="Arial", bold=True, size=10)
    REG = Font(name="Arial", size=10)
    NOTE = Font(name="Arial", size=9, italic=True, color="595959")
    TITLE = Font(name="Arial", bold=True, size=13, color="1F3864")

    def sheet_from(wb, title, heading, cols, dicts, widths, note=None):
        ws = wb.create_sheet(title)
        ws["A1"] = heading; ws["A1"].font = TITLE
        ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=max(len(cols), 2))
        r0 = 3
        if note:
            ws["A2"] = note; ws["A2"].font = NOTE
            ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=max(len(cols), 2))
            r0 = 4
        for j, h in enumerate(cols, 1):
            c = ws.cell(r0, j, h); c.fill = HEADF; c.font = WHITE
            c.alignment = Alignment(horizontal="center", wrap_text=True)
        r = r0 + 1
        for d in dicts:
            for j, k in enumerate(cols, 1):
                ws.cell(r, j, d.get(k)).font = BOLD if j == 1 else REG
            r += 1
        for j, w in enumerate(widths, 1):
            ws.column_dimensions[get_column_letter(j)].width = w
        ws.freeze_panes = ws.cell(r0 + 1, 1).coordinate
        ws.auto_filter.ref = "A%d:%s%d" % (r0, get_column_letter(len(cols)), max(r - 1, r0))

    wb = Workbook(); wb.remove(wb.active)
    mcols = ["impr", "clicks", "cost", "leads", "rev", "CTR%", "CPC", "CvR%", "CPL", "RPL", "ROAS%"]
    sheet_from(wb, "Group Scorecard", "New groups, %s..%s (%s)" % (dstart, a.end, note_adj),
               ["ad_group"] + mcols + ["CPL_vs_book", "ROAS_vs_book"], score,
               [40] + [8] * 11 + [10, 10],
               note="Benchmark = incumbent generic book, same window + same filter: CPL %s, "
                    "CvR %.1f%%, ROAS %.0f%%. %s."
                    % (fmt(book["CPL"], 1), book["CvR%"], book["ROAS%"], win_note))
    sheet_from(wb, "By Campaign", "Group x campaign detail", ["ad_group", "campaign"] + mcols,
               score_detail, [40, 44] + [8] * 11)
    sheet_from(wb, "Keyword Decisions", "Per keyword, book-relative rules (floors prevent noise verdicts)",
               ["decision", "ad_group", "keyword", "match_type", "why"] + mcols, kw_rows,
               [16, 38, 32, 9, 40] + [8] * 11,
               note="PROMOTE_EXACT = the launch policy's earned-exact rule. PAUSE_CANDIDATE needs your "
                    "sign-off -- write decision/kill_reason columns and I'll cut the Editor file.")
    if q_rows:
        sheet_from(wb, "Query Review", "Queries hitting cohort keywords, clean window (%s)" % q_note,
                   ["cls", "query", "brand", "ad_group", "impr", "clicks", "spend", "via"],
                   q_rows[:800], [14, 40, 16, 36, 8, 8, 9, 60])
        sheet_from(wb, "Negative Candidates", "Editor-ready candidates (BRAND rows -> QW worksheet process)",
                   ["level", "ad_group", "negative_keyword", "match", "reason", "clicks", "spend"],
                   neg_rows, [20, 36, 38, 8, 52, 8, 9])
        sheet_from(wb, "Term Capture", "Expansion terms: who caught them pre vs post launch",
                   ["term", "pre_absorbers", "post_absorbers", "now_self"], capture,
                   [34, 52, 52, 9],
                   note="now_self=YES means the term's own keyword now absorbs it -- the brand-broad "
                        "bleed fix working. This is the validation metric for Ben.")
    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    wb.save(a.out)
    base = os.path.splitext(a.out)[0]
    with open(base + "_negatives.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["level", "ad_group", "negative_keyword", "match",
                                          "reason", "clicks", "spend"])
        w.writeheader(); w.writerows(neg_rows)
    with open(base + "_keyword_decisions.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["decision", "ad_group", "keyword", "match_type", "why"] + mcols)
        w.writeheader(); w.writerows(kw_rows)
    print("\nwrote %s (+_negatives.csv, +_keyword_decisions.csv)" % a.out)
    conn.close()


if __name__ == "__main__":
    main()
