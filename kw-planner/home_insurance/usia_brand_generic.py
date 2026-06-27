#!/usr/bin/env python
"""
usia_brand_generic.py -- build the USIA Home bare-carrier GENERIC campaign.

Seeds a separate USIA_HOME_BRAND_GENERIC campaign from the EXISTING account's bare-term
performance (brand_term_performance_bare_terms.csv), not the KWP pull -- so the carrier list
and keyword forms are grounded in what actually converts. This is the cross-vertical brand
play (bare "geico" / "travelers insurance"), fenced off from the home-qualified conquest
campaign and carried on a "speak to an agent" / call-first message.

What it does:
  - Aggregates the bare-term CSV by carrier; drops lead-buyer aggregators and affinity brands
    (explicit DROP set -- the search-query brand_category tags are unreliable for these).
  - Keeps carriers clearing --min-spend AND --min-roas; flags thin (ROAS < 1.15) ones.
  - Per keeper carrier emits PHRASE keywords: the insurance-qualified form ("farmers insurance")
    always, plus the bare token ("geico") only when the pure-brand query itself cleared
    --min-bare-spend in the data (so distinctive brands that earned bare-token volume get it,
    dictionary-word carriers don't unless they proved out). One ad group per carrier.
  - Emits campaign-level negatives: home routing (home/homeowners/homeowner/house -> sends
    home-qualified brand queries to the conquest campaign) + service/navigational (login/claims/
    billing/etc.). Auto is deliberately NOT negated -- the cross-vertical reach is the point.
  - Writes an Editor-ready workbook (keywords + negatives + carrier-selection rationale +
    setup notes) and CSV mirrors.

    python usia_brand_generic.py --perf-csv brand_term_performance_bare_terms.csv
    python usia_brand_generic.py --perf-csv ...csv --min-spend 1000 --min-roas 1.0 --min-bare-spend 500
"""
import argparse
import csv
import re
from collections import defaultdict

CAMPAIGN = "USIA_HOME_BRAND_GENERIC"

# Lead-buyer aggregators (excluded as customers, per the standing convention) + affinity brands
# (handled in the AFFINITY campaign, not carrier-generic). The search_queries.brand_category
# tags miss these, so we drop by name. Extend with --drop.
DROP_BRANDS = {
    # aggregators / lead-gen / marketplaces
    "everquote", "smartfinancial", "quotewizard", "policygenius", "policy genius", "nerdwallet",
    "matic", "matic insurance", "otto", "otto insurance", "insurify", "bankrate", "the zebra",
    "zebra", "gabi", "savemaxhomes", "truequote", "homequote explorer", "2go insurance", "2go",
    "cfpnet", "insurance.com", "covertree",
    # affinity (own campaign)
    "costco", "aaa", "aarp", "sam's club", "sams club", "sam s club",
}

HOME_NEGS = ["home", "homeowners", "homeowner", "house", "houses"]
SERVICE_NEGS = ["login", "log in", "sign in", "claims", "claim", "file a claim",
                "customer service", "customer support", "billing", "pay bill", "pay my bill",
                "policy number", "cancel", "mailing address", "fax"]


def _despace(s):
    return (s or "").replace(" ", "")


def read_perf(path):
    """carrier -> aggregates (incl. bare-token spend); plus term_cost keyed by DESPACED term, so
    one-word spellings ('statefarm') count toward the two-word brand form ('state farm')."""
    agg = defaultdict(lambda: {"cost": 0.0, "clicks": 0, "conv": 0.0, "value": 0.0, "bare": 0.0})
    term_cost = defaultdict(float)
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            car = (row["carrier"] or "").strip().lower()
            term = (row["search_term"] or "").strip().lower()
            cost = float(row["cost"] or 0); clicks = int(float(row["clicks"] or 0))
            conv = float(row["conv"] or 0); value = float(row["value"] or 0)
            if not car:
                continue
            a = agg[car]
            a["cost"] += cost; a["clicks"] += clicks; a["conv"] += conv; a["value"] += value
            term_cost[_despace(term)] += cost
            if _despace(term) == _despace(car):     # the pure-brand query (any spacing)
                a["bare"] += cost
    return agg, term_cost


def select_carriers(agg, min_spend, min_roas, drop):
    keepers, dropped = [], []
    for car, a in agg.items():
        roas = a["value"] / a["cost"] if a["cost"] else 0
        cpa = a["cost"] / a["conv"] if a["conv"] else 0
        cpc = a["cost"] / a["clicks"] if a["clicks"] else 0
        rec = {"carrier": car, "cost": a["cost"], "clicks": a["clicks"], "conv": a["conv"],
               "value": a["value"], "roas": roas, "cpa": cpa, "cpc": cpc, "bare": a["bare"]}
        if car in drop:
            rec["reason"] = "drop: aggregator/affinity"; dropped.append(rec); continue
        if a["cost"] < min_spend:
            rec["reason"] = "drop: spend < %g" % min_spend; dropped.append(rec); continue
        if roas < min_roas:
            rec["reason"] = "drop: ROAS %.2f < %g" % (roas, min_roas); dropped.append(rec); continue
        rec["reason"] = "keep" + (" (THIN ROAS)" if roas < 1.15 else "")
        keepers.append(rec)
    keepers.sort(key=lambda r: -r["cost"])
    dropped.sort(key=lambda r: -r["cost"])
    return keepers, dropped


def gen_keywords(keepers, term_cost, min_bare_spend, exact_min, ai_max=False):
    """Match-type logic:
      - bare token ('geico','farmers') -> EXACT (tight; phrase on a bare brand pulls junk), when
        the pure-brand query (any spacing) cleared --min-bare-spend.
      - '[carrier] insurance' form:
          launch (ai_max=False): PHRASE always (catches the tail), + EXACT on proven heads
            (form despaced spend >= --exact-min) for priority.
          ai_max=True: EXACT only -- AI Max treats keywords as broad and expands to the tail, so
            a same-text phrase twin is redundant and just blurs reporting. Exact holds priority.
    """
    rows, seen = [], set()                     # dedup on (keyword, match_type)
    def add(ag, kw, mt, carrier):
        if (kw, mt) in seen:
            return
        seen.add((kw, mt))
        rows.append({"campaign": CAMPAIGN, "ad_group": ag, "keyword": kw,
                     "match_type": mt, "carrier": carrier})
    for r in keepers:
        c = r["carrier"].strip().lower()
        ag = r["carrier"].title()
        ins_form = c if c.endswith("insurance") else c + " insurance"
        if ai_max:
            add(ag, ins_form, "exact", c)                    # AI Max expands; exact holds priority
        else:
            add(ag, ins_form, "phrase", c)                   # tail discovery
            if term_cost.get(_despace(ins_form), 0) >= exact_min:
                add(ag, ins_form, "exact", c)
        if not c.endswith("insurance") and r["bare"] >= min_bare_spend:
            add(ag, c, "exact", c)                           # bare token, tight
    return rows


def write_outputs(out, keepers, dropped, kw_rows, min_spend, min_roas, min_bare_spend):
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment
    HEAD = PatternFill("solid", fgColor="1F3864")
    WHITE = Font(name="Arial", bold=True, color="FFFFFF", size=10)
    BOLD = Font(name="Arial", bold=True, size=10)
    REG = Font(name="Arial", size=10)
    NOTE = Font(name="Arial", size=9, italic=True, color="595959")
    TITLE = Font(name="Arial", bold=True, size=13, color="1F3864")

    def hdr(ws, row, hs):
        for j, h in enumerate(hs, 1):
            c = ws.cell(row, j, h); c.fill = HEAD; c.font = WHITE
            c.alignment = Alignment(horizontal="center", wrap_text=True)

    wb = Workbook(); wb.remove(wb.active)

    # Setup notes
    ws = wb.create_sheet("Setup")
    ws["A1"] = "USIA_HOME_BRAND_GENERIC -- setup"; ws["A1"].font = TITLE; ws.merge_cells("A1:B1")
    notes = [
        ("Campaign", CAMPAIGN),
        ("Type / network", "Search only -- exclude Search Partners & Display for a clean brand test"),
        ("Match type", "Bare token ('geico') EXACT (tight); '[carrier] insurance' PHRASE for the "
                       "tail, + EXACT on proven high-volume heads for priority"),
        ("Budget", "ISOLATED daily budget, separate from conquest -- this is a measurable coexist test"),
        ("Bidding", "USIA is a new (blank-pixel) account: start Manual CPC using the per-carrier CPC in "
                    "'Carrier Selection'; move to tCPA once ~30-50 conversions bank"),
        ("Message", "Call-first / 'speak to an agent' RSAs; enable call assets (the differentiator)"),
        ("Negatives", "Home routing + service/navigational applied at campaign level (see Negatives tab). "
                      "Auto is intentionally NOT negated -- cross-vertical reach is the point."),
        ("Seeded from", "existing-account bare-term performance; keep if spend>=%g and ROAS>=%g; bare token "
                        "added only if the pure-brand query cleared $%g" % (min_spend, min_roas, min_bare_spend)),
        ("Note", "Runs alongside the existing home account on these terms -- watch for self-competition / "
                 "CPC inflation in the shared auctions."),
    ]
    r = 3
    for k, v in notes:
        ws.cell(r, 1, k).font = BOLD
        c = ws.cell(r, 2, v); c.font = REG; c.alignment = Alignment(wrap_text=True, vertical="top")
        r += 1
    ws.column_dimensions["A"].width = 16; ws.column_dimensions["B"].width = 95

    # Keywords (Editor-ready)
    ws = wb.create_sheet("Keywords")
    ws["A1"] = "Keywords -- import-ready (phrase)"; ws["A1"].font = TITLE; ws.merge_cells("A1:D1")
    hdr(ws, 3, ["Campaign", "Ad Group", "Keyword", "Match Type"])
    r = 4
    for k in kw_rows:
        ws.cell(r, 1, k["campaign"]).font = REG
        ws.cell(r, 2, k["ad_group"]).font = BOLD
        ws.cell(r, 3, k["keyword"]).font = REG
        ws.cell(r, 4, k["match_type"]).font = REG
        r += 1
    for col, w in {"A": 26, "B": 22, "C": 34, "D": 12}.items():
        ws.column_dimensions[col].width = w
    ws.freeze_panes = "A4"

    # Negatives (campaign-level)
    ws = wb.create_sheet("Negatives")
    ws["A1"] = "Campaign negative keywords"; ws["A1"].font = TITLE; ws.merge_cells("A1:D1")
    ws["A2"] = ("Home set routes home-qualified brand queries to USIA_HOME_BRAND_CARRIER (conquest). "
                "Service set drops navigational/service traffic. Add at campaign level.")
    ws["A2"].font = NOTE; ws.merge_cells("A2:D2")
    hdr(ws, 4, ["Campaign", "Negative Keyword", "Match Type", "Purpose"])
    r = 5
    for neg in HOME_NEGS:
        ws.cell(r, 1, CAMPAIGN).font = REG; ws.cell(r, 2, neg).font = REG
        ws.cell(r, 3, "broad").font = REG; ws.cell(r, 4, "route home-qualified to conquest").font = NOTE
        r += 1
    for neg in SERVICE_NEGS:
        ws.cell(r, 1, CAMPAIGN).font = REG; ws.cell(r, 2, neg).font = REG
        ws.cell(r, 3, "phrase").font = REG; ws.cell(r, 4, "drop service/navigational").font = NOTE
        r += 1
    for col, w in {"A": 26, "B": 22, "C": 12, "D": 36}.items():
        ws.column_dimensions[col].width = w

    # Carrier selection rationale
    ws = wb.create_sheet("Carrier Selection")
    ws["A1"] = "Carrier selection -- from existing-account bare-term performance"; ws["A1"].font = TITLE
    ws.merge_cells("A1:I1")
    hdr(ws, 3, ["Carrier", "Decision", "Cost", "Clicks", "Conv", "ROAS", "CPA", "CPC", "Bare $"])
    r = 4
    for rec in keepers + dropped:
        ws.cell(r, 1, rec["carrier"]).font = BOLD if rec["reason"].startswith("keep") else REG
        dc = ws.cell(r, 2, rec["reason"])
        dc.font = Font(name="Arial", size=10,
                       color="1F7A1F" if rec["reason"].startswith("keep") and "THIN" not in rec["reason"]
                       else ("C07A00" if "THIN" in rec["reason"] else "C00000"))
        ws.cell(r, 3, round(rec["cost"], 0)).font = REG; ws.cell(r, 3).number_format = "#,##0"
        ws.cell(r, 4, rec["clicks"]).font = REG
        ws.cell(r, 5, round(rec["conv"], 1)).font = REG
        ws.cell(r, 6, round(rec["roas"], 2)).font = REG
        ws.cell(r, 7, round(rec["cpa"], 2)).font = REG
        ws.cell(r, 8, round(rec["cpc"], 2)).font = REG
        ws.cell(r, 9, round(rec["bare"], 0)).font = REG; ws.cell(r, 9).number_format = "#,##0"
        r += 1
    for col, w in {"A": 22, "B": 22, "C": 11, "D": 8, "E": 8, "F": 8, "G": 9, "H": 8, "I": 10}.items():
        ws.column_dimensions[col].width = w
    ws.freeze_panes = "A4"
    wb.save(out)

    base = out.rsplit(".", 1)[0]
    with open(base + "_keywords.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f); w.writerow(["campaign", "ad_group", "keyword", "match_type"])
        for k in kw_rows:
            w.writerow([k["campaign"], k["ad_group"], k["keyword"], k["match_type"]])
    with open(base + "_negatives.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f); w.writerow(["campaign", "negative_keyword", "match_type"])
        for neg in HOME_NEGS:
            w.writerow([CAMPAIGN, neg, "broad"])
        for neg in SERVICE_NEGS:
            w.writerow([CAMPAIGN, neg, "phrase"])
    print("wrote %s, %s, %s" % (out, base + "_keywords.csv", base + "_negatives.csv"))


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--perf-csv", default="brand_term_performance_bare_terms.csv")
    p.add_argument("--out", default="usia_home_brand_generic.xlsx")
    p.add_argument("--min-spend", type=float, default=1000, help="carrier inclusion: min 90d bare spend")
    p.add_argument("--min-roas", type=float, default=1.0, help="carrier inclusion: min bare ROAS")
    p.add_argument("--min-bare-spend", type=float, default=500,
                   help="add the bare token (e.g. 'geico') only if the pure-brand query cleared this")
    p.add_argument("--exact-min", type=float, default=2000,
                   help="add EXACT on a '[carrier] insurance' form only if that exact query cleared this")
    p.add_argument("--ai-max", action="store_true",
                   help="emit the AI-Max-era structure: exact-only forms, no redundant phrase twin")
    p.add_argument("--drop", nargs="*", default=None, help="extra brand names to drop")
    a = p.parse_args()
    drop = set(DROP_BRANDS) | {d.lower() for d in (a.drop or [])}
    agg, term_cost = read_perf(a.perf_csv)
    keepers, dropped = select_carriers(agg, a.min_spend, a.min_roas, drop)
    kw_rows = gen_keywords(keepers, term_cost, a.min_bare_spend, a.exact_min, a.ai_max)
    write_outputs(a.out, keepers, dropped, kw_rows, a.min_spend, a.min_roas, a.min_bare_spend)
    n_exact = sum(1 for k in kw_rows if k["match_type"] == "exact")
    print("\nUSIA_HOME_BRAND_GENERIC%s: %d carriers kept, %d keywords (%d exact / %d phrase), %d campaign negatives"
          % (" [AI MAX]" if a.ai_max else "", len(keepers), len(kw_rows), n_exact,
             len(kw_rows) - n_exact, len(HOME_NEGS) + len(SERVICE_NEGS)))
    print("kept carriers (cost / ROAS):")
    for r in keepers:
        print("  %-20s $%-9s ROAS=%.2f  bare=%s%s"
              % (r["carrier"], f"{r['cost']:,.0f}", r["roas"],
                 "yes" if r["bare"] >= a.min_bare_spend else "no",
                 "  [THIN]" if r["roas"] < 1.15 else ""))


if __name__ == "__main__":
    main()
