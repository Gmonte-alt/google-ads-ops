#!/usr/bin/env python
"""
usia_launch_shell.py -- generate the USIA Home (Google Ads Search) launch shell.

Turns the demand/winnability/profitability analysis into a buildable campaign
skeleton: geo tiers, campaign structure, ad groups + seed keywords, negatives,
and the launch recommendation. Single vertical (home); renters excluded per the
self-serve Quote.com LP decision.

Geo tiers derive from per-state demand (demand_research.db geo-loop) x realized
Home RPL/ROAS (geo_state_rpl_overlay.csv). Texas is placed in T1 by judgment
(volume anchor) despite break-even realized ROAS under Home's current bidding.

    python usia_launch_shell.py --out usia_home_launch_shell.xlsx
"""

import argparse
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side

# ---- geo tiers: (state, demand_index, rpl, roas) ----
T1 = [("Texas", 315320, 62, 1.03), ("Illinois", 130950, 78, 1.28),
      ("Pennsylvania", 103660, 77, 1.25), ("Ohio", 95170, 80, 1.27),
      ("New Jersey", 89840, 97, 1.37)]
T2 = [("California", 300440, 25, 1.14), ("Georgia", 117240, 56, 1.16),
      ("North Carolina", 104890, 36, 1.20), ("Tennessee", 61110, 74, 1.19),
      ("Missouri", 58530, 76, 1.21), ("Minnesota", 54000, 73, 1.26),
      ("Alabama", 51010, 60, 1.40), ("Wisconsin", 48500, 74, 1.30),
      ("Oklahoma", 43050, 67, 1.20), ("Connecticut", 42850, 77, 1.21),
      ("Louisiana", 41720, 34, 1.32), ("Oregon", 40750, 89, 1.54),
      ("Kentucky", 39600, 64, 1.32), ("Kansas", 37170, 77, 1.20),
      ("Nevada", 35290, 38, 1.20), ("Arkansas", 32660, 60, 1.28),
      ("Utah", 30250, 78, 1.34), ("Idaho", 23050, 87, 1.38),
      ("West Virginia", 22980, 53, 1.18), ("Delaware", 18040, 90, 1.49)]
T3 = [("Florida", 217680, 23, 1.01), ("New York", 169900, 34, 1.03),
      ("Arizona", 71130, 54, 1.07), ("Colorado", 70310, 84, 1.07),
      ("Massachusetts", 60360, 92, 1.08), ("Maryland", 59550, 64, 1.11),
      ("South Carolina", 56940, 40, 1.02), ("New Mexico", 26270, 69, 1.08),
      ("Rhode Island", 20290, 60, 1.13)]
EXCLUDE_NEG = ["Virginia (0.88)", "Michigan (0.99)", "Washington (0.82)", "Indiana (0.79)",
               "Iowa (0.91)", "Nebraska (0.89)", "New Hampshire (0.75)", "Mississippi (0.68)",
               "Hawaii (0.67)", "Wyoming (0.63)", "South Dakota (0.55)"]
EXCLUDE_THIN = ["Maine", "Montana", "District of Columbia", "North Dakota", "Alaska", "Vermont"]

ROLE = {"T1": "Primary launch -- anchor + profitable core",
        "T2": "Profitable expansion (ROAS >= ~1.15)",
        "T3": "Volume / break-even test (capped budgets)"}

# ---- ad groups + real seed keywords (home, on-vertical, generic) ----
AD_GROUPS = {
    "QUOTE": ["home insurance quote", "homeowners insurance quote", "house insurance quote",
              "home insurance quotation", "homeowners insurance quotes"],
    "COMPARE": ["best home insurance companies", "home insurance companies",
                "compare home insurance", "top home insurance companies", "best homeowners insurance"],
    "PRICE": ["cheap home insurance", "affordable home insurance", "cheapest homeowners insurance",
              "inexpensive home insurance", "cheap homeowners insurance"],
    "COVERAGE": ["home insurance coverage", "homeowners insurance policy", "dwelling coverage",
                 "personal property coverage", "home insurance deductible"],
}
CALL_GROUPS = {
    "NEAR_ME": ["home insurance near me", "homeowners insurance near me", "house insurance near me"],
    "AGENT_BROKER": ["home insurance agent", "home insurance broker", "home insurance agency",
                     "home insurance agents near me", "local home insurance agent"],
}

NEGATIVES = [
    ("Cross-vertical", "renters insurance; renter insurance; apartment insurance; tenant insurance; "
     "auto insurance; car insurance; vehicle insurance; commercial insurance; business insurance; "
     "life insurance; pet insurance; travel insurance; boat insurance; flood insurance only", "phrase",
     "Account shared list"),
    ("Existing-policyholder / navigational", "login; log in; sign in; claim; claims; file a claim; "
     "payment; pay bill; pay my bill; customer service; cancel policy; declaration page", "phrase",
     "Account shared list"),
    ("Carrier brands (conquest DEFERRED)", "geico; state farm; allstate; progressive; usaa; "
     "liberty mutual; farmers; nationwide; travelers; american family; lemonade; erie; amica", "phrase",
     "Generic + Call campaigns (remove the approved set when conquest launches in Phase 2)"),
    ("Job / info / non-commercial", "jobs; careers; agent salary; license; exam; definition; "
     "meaning; wiki; reddit", "phrase", "Account shared list"),
]

CAMPAIGNS = [
    ("USIA_HOME_GEN_T1", "T1", T1, 45, "Maximize Conversions",
     "5 states. Lead budget. Quote/Compare/Price/Coverage ad groups."),
    ("USIA_HOME_GEN_CALL", "T1+T2", None, 20, "Maximize Conversions",
     "USIA differentiator. Call ads + call extensions, business hours. Near-me / agent-broker ad groups."),
    ("USIA_HOME_GEN_T2", "T2", T2, 25, "Maximize Conversions",
     "20 states. Profitable expansion. Same ad groups as T1."),
    ("USIA_HOME_GEN_T3", "T3", T3, 10, "Maximize Conversions (capped)",
     "9 states. Break-even test, tight daily caps; promote to T2 only if it clears target."),
]
# Phase-2 brand/conquest family -- separate from generic. Approved carriers only.
CONQUEST = [
    ("USIA_HOME_CONQ_APPROVED", "T1+T2", "Phase 2 (own budget)", "Target CPA (separate)",
     "APPROVED carriers only (Travelers, Nationwide, American Family, + Auto-Owners/Cincinnati once added). "
     "DNB carriers (Geico, State Farm, Allstate, USAA, Progressive, Liberty Mutual, Lemonade...) NEVER bid. "
     "Own budget, bidding, and conquest landing pages -- intent differs from generic."),
]

# generic vs brand competitive levels (home, national, on-vertical; demand_research.db, KWP proxy)
COMPETITION = [
    ("Generic", 995, 2539200, 37, 43.32, 452.54, "MED 42% / LOW 42% / HIGH 12%"),
    ("Brand -- biddable (approved/conditional)", 164, 596780, 42, 30.80, 110.00, "MED 53% / LOW 35% / HIGH 10%"),
    ("Brand -- DNB (restricted)", 74, 434170, 35, 30.53, 866.18, "LOW 51% / MED 44% / HIGH 4%"),
]

# ---- styles ----
HEAD = PatternFill("solid", fgColor="1F3864")
SUB = PatternFill("solid", fgColor="D6DCE4")
T1F = PatternFill("solid", fgColor="E2EFDA")
T3F = PatternFill("solid", fgColor="FCE4D6")
WHITE = Font(name="Arial", bold=True, color="FFFFFF", size=10)
BOLD = Font(name="Arial", bold=True, size=10)
REG = Font(name="Arial", size=10)
NOTE = Font(name="Arial", size=9, italic=True, color="595959")
TITLE = Font(name="Arial", bold=True, size=14, color="1F3864")
THIN = Side(style="thin", color="BFBFBF")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)


def hdr(ws, row, headers):
    for j, h in enumerate(headers, 1):
        c = ws.cell(row, j, h)
        c.fill = HEAD; c.font = WHITE
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        c.border = BORDER


def setw(ws, w):
    for col, width in w.items():
        ws.column_dimensions[col].width = width


def sheet_plan(wb):
    ws = wb.create_sheet("Launch Plan")
    ws["A1"] = "USIA Home -- Google Ads Search Launch Recommendation"
    ws["A1"].font = TITLE
    ws.merge_cells("A1:B1")
    rows = [
        ("SCOPE", ""),
        ("Vertical", "Home insurance only. Renters excluded -- routed to a self-serve Quote.com LP (renters contact-intent ~8.5k/mo; price-led, no call demand)."),
        ("Channel", "Google Ads Search, new account (USIA, Google account #6). Call/agent-assisted positioning."),
        ("Positioning", "Comparison layer + call-assisted. The contact wedge (~140k/mo biddable) is the differentiator, not a volume engine (~2-3% of demand)."),
        ("", ""),
        ("GEO STRATEGY", ""),
        ("Tiering basis", "Per-state demand x realized Home RPL/ROAS. RANK on profitability, not raw volume -- the auction prices value into CPC, so high RPL != high margin."),
        ("Tier 1 (5 states)", "Texas (volume anchor), New Jersey, Illinois, Ohio, Pennsylvania -- the profitable core + the #1 demand market."),
        ("Tier 2 (20 states)", "Profitable expansion (ROAS >= ~1.15), led by California for scale; many small high-ROAS states."),
        ("Tier 3 (9 states)", "Break-even volume test (FL, NY, etc.) at capped budgets -- prove before scaling."),
        ("Excluded (17)", "Negative-margin states (VA, MI, WA, IN, MS, HI...) and thin/no-signal states. Not launched."),
        ("", ""),
        ("ACCOUNT ARCHITECTURE -- PRIMARY SPLIT: GENERIC vs BRAND", ""),
        ("Why split", "Generic and brand are different auctions -- different intent, conversion, and economics. KWP shows competition is comparable (generic CPC is even higher), so the split is for intent, bidding-signal hygiene, separate budgets, and DNB compliance, not competition level. See 'Generic vs Brand' sheet."),
        ("Generic family (launch)", "Geo-tier campaigns (T1/T2/T3) + the Call campaign. All non-brand. This is the entire launch."),
        ("Brand/Conquest family (Phase 2)", "Separate campaigns, approved carriers only, own budget + bidding + LPs. DNB carriers never bid. Geo-tier nests inside the generic family; brand is its own family."),
        ("", ""),
        ("CAMPAIGN ARCHITECTURE", ""),
        ("Structure", "One Search campaign per tier (geo = the tier's states) + a dedicated Call campaign across T1+T2. Ad groups by intent theme."),
        ("Ad groups", "Quote (dominant), Compare, Price, Coverage in each tier campaign; Near-Me and Agent/Broker in the Call campaign."),
        ("Match types", "Launch on Phrase + Exact for control with no history; add Broad once Smart Bidding has data."),
        ("Carrier conquest", "DEFERRED to Phase 2. 42% of branded demand is DNB; conquest is expensive and competitive -- prove generic + call first. Carrier brands run as negatives at launch."),
        ("", ""),
        ("BID STRATEGY (phased -- new account, zero history)", ""),
        ("Phase 0 (wk 1-3)", "Conversion tracking/pixel MUST be live first. Launch on Maximize Conversions to seed data (Manual CPC only if tracking is cold). Conservative budgets."),
        ("Phase 1 (>= ~15-30 conv)", "Move to Target CPA at a target derived from RPL x target margin. Expand to Tier 2."),
        ("Phase 2 (value data reliable)", "Move to Target ROAS; add Broad match; open Tier 3 test; launch carrier conquest (approved carriers only)."),
        ("", ""),
        ("BUDGET", ""),
        ("Split", "T1 45% / Call 20% / T2 25% / T3 10%. Weighted to the profitable core; T3 capped."),
        ("", ""),
        ("CAVEATS", ""),
        ("RPL/ROAS is a proxy", "Realized under HOME's bidding (PF2 gap >5%, native-conversions), not USIA's call/agent economics. Treat tiers as a strong prior; re-tier after ~4-6 weeks of USIA data."),
        ("Demand is a relative index", "KWP buckets close variants; absolutes overcount unique searches. Rankings/shares hold; totals don't."),
        ("Hardening still open", "Routed geographic_view pull (proxy->exact RPL) and a YoY growth read sharpen the case before heavy spend."),
    ]
    r = 3
    for k, v in rows:
        is_sec = k.isupper() and k
        ws.cell(r, 1, k).font = BOLD if (is_sec or v == "" and k) else BOLD if k and not v else REG
        if is_sec:
            ws.cell(r, 1).fill = SUB
            ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=2)
        else:
            c = ws.cell(r, 2, v); c.font = REG; c.alignment = Alignment(wrap_text=True, vertical="top")
            ws.cell(r, 1).font = BOLD
        r += 1
    setw(ws, {"A": 32, "B": 95})


def sheet_tiers(wb):
    ws = wb.create_sheet("Geo Tiers")
    ws["A1"] = "Geo Tiers -- launch targeting"
    ws["A1"].font = TITLE
    ws.merge_cells("A1:F1")
    ws["A2"] = ("Demand = relative index (demand_research.db geo-loop). RPL/ROAS = realized Home, "
                "2026-03-09..06-07, native basis (proxy). Texas placed in T1 by judgment (volume anchor).")
    ws["A2"].font = NOTE
    ws.merge_cells("A2:F2")
    hdr(ws, 4, ["Tier", "State", "Demand (index)", "RPL ($/lead)", "ROAS", "Role"])
    r = 5
    for tier, states, fill in (("T1", T1, T1F), ("T2", T2, None), ("T3", T3, T3F)):
        for s, dem, rpl, roas in states:
            ws.cell(r, 1, tier).font = BOLD
            ws.cell(r, 2, s).font = REG
            ws.cell(r, 3, dem).font = REG; ws.cell(r, 3).number_format = "#,##0"
            ws.cell(r, 4, rpl).font = REG; ws.cell(r, 4).number_format = "$#,##0"
            c5 = ws.cell(r, 5, roas); c5.number_format = "0.00"
            c5.font = Font(name="Arial", size=10, color="C00000" if roas < 1.05 else "000000")
            ws.cell(r, 6, ROLE[tier]).font = NOTE
            if fill:
                for col in range(1, 7):
                    ws.cell(r, col).fill = fill
            r += 1
    r += 1
    ws.cell(r, 1, "EXCLUDED at launch").font = BOLD
    ws.cell(r, 1).fill = SUB; ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=6)
    r += 1
    ws.cell(r, 1, "Negative margin").font = BOLD
    ws.cell(r, 2, "; ".join(EXCLUDE_NEG)).font = REG
    ws.merge_cells(start_row=r, start_column=2, end_row=r, end_column=6)
    r += 1
    ws.cell(r, 1, "Thin / no RPL signal").font = BOLD
    ws.cell(r, 2, "; ".join(EXCLUDE_THIN)).font = REG
    ws.merge_cells(start_row=r, start_column=2, end_row=r, end_column=6)
    setw(ws, {"A": 8, "B": 18, "C": 14, "D": 12, "E": 8, "F": 40})
    ws.freeze_panes = "A5"


def sheet_campaigns(wb):
    ws = wb.create_sheet("Campaign Shell")
    ws["A1"] = "Campaign Shell -- Google Ads Search"
    ws["A1"].font = TITLE
    ws.merge_cells("A1:G1")
    ws["A2"] = ("PRIMARY SPLIT = Generic vs Brand. Generic family launches (geo-tiered); the Brand/Conquest "
                "family is a separate Phase-2 structure (approved carriers only). Geo-tier nests inside generic.")
    ws["A2"].font = NOTE
    ws.merge_cells("A2:G2")
    hdr(ws, 4, ["Campaign", "Tier", "States", "Network / Settings", "Budget %",
                "Launch Bid Strategy", "Notes"])
    r = 5
    ws.cell(r, 1, "GENERIC FAMILY (launch)").font = BOLD
    ws.cell(r, 1).fill = SUB; ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=7)
    r += 1
    first = r
    for name, tier, states, pct, bid, notes in CAMPAIGNS:
        ns = "%d states" % len(states) if states else "T1+T2 geos"
        ws.cell(r, 1, name).font = BOLD
        ws.cell(r, 2, tier).font = REG
        ws.cell(r, 3, ns).font = REG
        ws.cell(r, 4, "Search only; no Display/Search Partners at launch; location = people IN target; "
                "ad rotation = optimize").font = NOTE
        ws.cell(r, 5).value = pct / 100.0; ws.cell(r, 5).number_format = "0%"; ws.cell(r, 5).font = REG
        ws.cell(r, 6, bid).font = REG
        ws.cell(r, 7, notes).font = NOTE
        for col in range(1, 8):
            ws.cell(r, col).alignment = Alignment(wrap_text=True, vertical="top")
        r += 1
    ws.cell(r, 1, "Generic launch total").font = BOLD
    ws.cell(r, 5, "=SUM(E%d:E%d)" % (first, r - 1)).font = BOLD
    ws.cell(r, 5).number_format = "0%"
    r += 2
    ws.cell(r, 1, "BRAND / CONQUEST FAMILY (Phase 2 -- separate budget & bidding)").font = BOLD
    ws.cell(r, 1).fill = T3F; ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=7)
    r += 1
    for name, geo, budget, bid, notes in CONQUEST:
        ws.cell(r, 1, name).font = BOLD
        ws.cell(r, 2, geo).font = REG
        ws.cell(r, 3, "approved carriers").font = REG
        ws.cell(r, 4, "Search only; conquest LPs; separate from generic for intent + bidding hygiene + DNB control").font = NOTE
        ws.cell(r, 5, budget).font = REG
        ws.cell(r, 6, bid).font = REG
        ws.cell(r, 7, notes).font = NOTE
        for col in range(1, 8):
            ws.cell(r, col).alignment = Alignment(wrap_text=True, vertical="top")
        r += 1
    setw(ws, {"A": 24, "B": 8, "C": 14, "D": 34, "E": 12, "F": 22, "G": 46})
    ws.freeze_panes = "A5"


def sheet_competition(wb):
    ws = wb.create_sheet("Generic vs Brand")
    ws["A1"] = "Competitive Levels -- Generic vs Brand"
    ws["A1"].font = TITLE
    ws.merge_cells("A1:G1")
    ws["A2"] = ("Home, national, on-vertical (demand_research.db; KWP proxy -- live auction-insights is the "
                "authoritative read once running). Competition is COMPARABLE across buckets and generic CPC is "
                "higher on the median -- so the split is for intent / bidding hygiene / DNB control, not competition level.")
    ws["A2"].font = NOTE
    ws.merge_cells("A2:G2")
    hdr(ws, 4, ["Bucket", "Keywords", "Volume", "Avg Comp. Index", "Median CPC", "Max CPC", "Competition mix"])
    for i, (b, n, vol, ci, med, mx, mix) in enumerate(COMPETITION):
        r = 5 + i
        ws.cell(r, 1, b).font = BOLD if i == 0 else REG
        ws.cell(r, 2, n).font = REG
        ws.cell(r, 3, vol).font = REG; ws.cell(r, 3).number_format = "#,##0"
        ws.cell(r, 4, ci).font = REG
        ws.cell(r, 5, med).font = REG; ws.cell(r, 5).number_format = "$#,##0.00"
        ws.cell(r, 6, mx).font = REG; ws.cell(r, 6).number_format = "$#,##0.00"
        ws.cell(r, 7, mix).font = REG
    r = 9
    ws.cell(r, 1, "Implication").font = BOLD
    ws.cell(r, 2, "Split generic from brand/conquest as the primary axis -- but because the auctions differ in "
            "intent and economics (and DNB must stay a clean negative boundary), not because brand is more "
            "competitive. At launch USIA is generic-only; brand/conquest is Phase 2 (approved carriers).").font = REG
    ws.merge_cells("A%d:G%d" % (r, r))
    ws.cell(r, 2).alignment = Alignment(wrap_text=True, vertical="top")
    ws.row_dimensions[r].height = 42
    setw(ws, {"A": 36, "B": 10, "C": 12, "D": 16, "E": 12, "F": 11, "G": 24})


def sheet_adgroups(wb):
    ws = wb.create_sheet("Ad Groups & Keywords")
    ws["A1"] = "Ad Groups & Seed Keywords"
    ws["A1"].font = TITLE
    ws.merge_cells("A1:E1")
    ws["A2"] = ("Seeds are real home on-vertical generic terms (demand_research.db). Launch match types: "
                "Phrase + Exact. Replicate the four themed ad groups across T1/T2/T3 campaigns.")
    ws["A2"].font = NOTE
    ws.merge_cells("A2:E2")
    hdr(ws, 4, ["Campaign scope", "Ad Group", "Match Types", "Seed Keywords", "Notes"])
    r = 5

    def block(scope, groups, note_fn):
        nonlocal r
        for ag, kws in groups.items():
            ws.cell(r, 1, scope).font = REG
            ws.cell(r, 2, ag).font = BOLD
            ws.cell(r, 3, "Phrase + Exact").font = REG
            ws.cell(r, 4, "; ".join(kws)).font = REG
            ws.cell(r, 4).alignment = Alignment(wrap_text=True, vertical="top")
            ws.cell(r, 5, note_fn(ag)).font = NOTE
            r += 1

    block("T1 / T2 / T3 (each)", AD_GROUPS,
          lambda ag: "Quote = dominant intent; lead RSA + budget here." if ag == "QUOTE" else
          ("Price-led copy; watch CPL." if ag == "PRICE" else ""))
    r += 1
    ws.cell(r, 1, "USIA_HOME_SRCH_CALL").font = BOLD
    ws.cell(r, 1).fill = SUB; ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=5)
    r += 1
    block("CALL", CALL_GROUPS,
          lambda ag: "Call ads + call extensions; route to agents during business hours.")
    setw(ws, {"A": 22, "B": 16, "C": 16, "D": 64, "E": 34})
    ws.freeze_panes = "A5"


def sheet_negatives(wb):
    ws = wb.create_sheet("Negatives")
    ws["A1"] = "Negative Keywords"
    ws["A1"].font = TITLE
    ws.merge_cells("A1:D1")
    hdr(ws, 3, ["Category", "Negative Terms", "Match", "Scope"])
    r = 4
    for cat, terms, match, scope in NEGATIVES:
        ws.cell(r, 1, cat).font = BOLD
        ws.cell(r, 2, terms).font = REG
        ws.cell(r, 2).alignment = Alignment(wrap_text=True, vertical="top")
        ws.cell(r, 3, match).font = REG
        ws.cell(r, 4, scope).font = NOTE
        ws.cell(r, 4).alignment = Alignment(wrap_text=True, vertical="top")
        r += 1
    setw(ws, {"A": 30, "B": 70, "C": 10, "D": 40})


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--out", default="usia_home_launch_shell.xlsx")
    args = p.parse_args()
    wb = Workbook(); wb.remove(wb.active)
    sheet_plan(wb)
    sheet_tiers(wb)
    sheet_competition(wb)
    sheet_campaigns(wb)
    sheet_adgroups(wb)
    sheet_negatives(wb)
    wb.save(args.out)
    print("wrote %s" % args.out)


if __name__ == "__main__":
    main()
