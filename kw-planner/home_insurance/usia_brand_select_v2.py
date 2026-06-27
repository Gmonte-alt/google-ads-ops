#!/usr/bin/env python
"""
usia_brand_select_v2.py -- USIA Home BRAND (conquest) campaigns, v2 selection logic.

Same v2 engine as usia_keyword_select_v2.py (intent-signature coverage heads + covered-demand
floor + match typing), adapted for conquest. v1 (usia_brand_select.py) is preserved for diffing.

v2 changes vs v1:
  1) HEAD-FINDING is INTENT-SIGNATURE COVERAGE per brand, not "TF-IDF canonical = highest
     volume + 25 cap". Within a brand, the signature strips the brand's OWN tokens as well as
     the universal base {home, insurance}, so the distinguishing signature is the MODIFIER:
     "geico home insurance" -> {} (the bare-brand anchor); "geico home insurance quote" ->
     {quote}; "geico home and auto insurance" -> {auto}. Heads consolidate by modifier; the
     bid surface stays a real branded query.

  2) FLOOR on SUMMED COVERED DEMAND (--floor), applied after consolidation. A modifier earns
     its own phrase head only if its covered demand clears the floor; otherwise it folds toward
     the bare-brand anchor (shedding its lowest-coverage token). Phrase-correct: a modifier whose
     surface contains "home/homeowners insurance" ("geico home insurance quote") can ride under
     the bare-brand head; a bundle ("geico home and auto insurance") has no such phrase and keeps
     its own (THIN-flagged) head rather than mis-folding.

  3) MATCH TYPE is brand-appropriate (the conquest call): the bare-brand head is EXACT at ANY
     volume -- the searcher named the competitor, so intent is already precise -- plus anything
     over --exact-cutoff. Everything else is PHRASE. Each exact head gets 1-2 phrase COMPANIONS
     (highest-volume modifier superset + a call-intent superset like "...phone number"), so the
     exact's modifier tail (which exact won't match) is still covered. Differs from the generic
     pipeline, where exact is reserved for high-demand core because phrase does discovery.

  Routing and exclusions are v1 verbatim: per-brand grouping (carrier clears --group-floor ->
  own ad group, else CARRIER_OTHER), AFFINITY / AGGREGATOR segments, and load_brand_candidates'
  biddability/adjacency/health/navigate/geo screens. TF-IDF clustering is dropped (per-brand
  pools are small; the intent signature is the head finder).

    python usia_brand_select_v2.py --out usia_home_brand_v2.xlsx
    python usia_brand_select_v2.py --floor 300 --exact-cutoff 10000 --group-floor 2000

Requires usia_brand_select.py and usia_keyword_select_v2.py in the same folder.
"""
import argparse
import csv
from collections import defaultdict

from usia_brand_select import (
    load_brand_candidates, geoloop_date, segment_of, BRAND_ALIASES,
)
from usia_keyword_select import normalize_text
from usia_keyword_select_v2 import HOME_INS_PHRASE_RE, CALL_TOKENS

BASE_TOKENS = frozenset({"home", "insurance"})


def brand_tokens(brand):
    """Normalized tokens of the brand name -- stripped from the signature so the modifier
    is what distinguishes heads within a brand group (e.g. 'state farm' -> {state, farm})."""
    return frozenset(normalize_text(brand).split()) if brand else frozenset()


def intent_sig(kw, btok):
    """Normalized tokens minus the universal base AND this brand's own tokens -> the modifier
    intent. Empty == the bare '[brand] home insurance' anchor."""
    return frozenset(set(normalize_text(kw).split()) - BASE_TOKENS - btok)


def _head_text(sig, ms, btok):
    """Clean canonical branded surface for a head: prefer an 'insurance'-bearing (else
    multiword) member whose signature matches, by individual volume."""
    exact = [m for m in ms if intent_sig(m["kw"], btok) == sig]
    pool = exact or ms
    clean = [m for m in pool if "insurance" in m["kw"].split()]
    if not clean:
        clean = [m for m in pool if len(m["kw"].split()) >= 2]
    cand = clean or pool
    return max(cand, key=lambda m: (m["vol"], -len(m["kw"].split())))["kw"]


def select_heads_brand(pool, floor, btok):
    """pool: [(kw, vol, brand)] for ONE brand. Intent-signature coverage heads with the floor
    on summed covered demand. Sub-floor modifiers fold toward the bare-brand anchor (if their
    surface contains 'home insurance', so the bare-brand phrase would cover them); bundles and
    other non-coverable modifiers keep their own THIN head."""
    members = [{"kw": kw, "vol": vol, "sig": intent_sig(kw, btok),
                "core": bool(HOME_INS_PHRASE_RE.search(kw))} for kw, vol, _ in pool]
    while True:
        tokcov = defaultdict(int)
        for m in members:
            for t in m["sig"]:
                tokcov[t] += m["vol"]
        dem = defaultdict(int)
        for m in members:
            dem[m["sig"]] += m["vol"]
        moved = False
        for m in members:
            s = m["sig"]
            if len(s) >= 2 and dem[s] < floor:
                drop = min(s, key=lambda t: (tokcov[t], t))
                m["sig"] = frozenset(s - {drop})
                moved = True
        if not moved:
            break
    dem = defaultdict(int)
    for m in members:
        dem[m["sig"]] += m["vol"]
    for m in members:
        if m["sig"] and dem[m["sig"]] < floor and m["core"]:
            m["sig"] = frozenset()       # absorb into the bare-brand anchor head
    groups = defaultdict(list)
    for m in members:
        groups[m["sig"]].append(m)
    heads = []
    for sig, ms in groups.items():
        ms.sort(key=lambda m: -m["vol"])
        vol = sum(m["vol"] for m in ms)
        heads.append({
            "keyword": _head_text(sig, ms, btok),
            "sig": sig, "volume": vol, "n_members": len(ms),
            "members": "; ".join(m["kw"] for m in ms),
            "member_list": [(m["kw"], m["vol"]) for m in ms],
            "thin": vol < floor,
        })
    heads.sort(key=lambda h: -h["volume"])
    return heads


def pick_companions(head, btok):
    """Up to two phrase companions for an exact head: highest-volume modifier superset plus a
    call-intent superset if present. Unlike the generic pipeline, the bare-brand head (empty
    signature) DOES get companions -- per brand its supersets are just that brand's modifier
    tail (clean), so they recover the tail the exact bare-brand keyword won't match."""
    hs = head["sig"]
    cands = [(kw, vol) for kw, vol in head["member_list"] if intent_sig(kw, btok) > hs]
    if not cands:
        return []
    comps = []
    top = max(cands, key=lambda x: x[1])
    comps.append({"keyword": top[0], "volume": top[1], "call": bool(intent_sig(top[0], btok) & CALL_TOKENS)})
    calls = [(kw, vol) for kw, vol in cands if intent_sig(kw, btok) & CALL_TOKENS]
    if calls:
        tc = max(calls, key=lambda x: x[1])
        if tc[0] != top[0]:
            comps.append({"keyword": tc[0], "volume": tc[1], "call": True})
    return comps


def apply_match_types(heads, exact_cutoff, other=False):
    """Bare-brand head exact at any volume (conquest intent is precise); other heads exact only
    over the cutoff; everything else phrase. EXCEPTION -- the thin CARRIER_OTHER catch-all
    (other=True): the bare-brand head is PHRASE, since a sub-floor exact just trips Low-Search-
    Volume and a phrase catches the carrier's variant tail in one keyword (all a thin carrier
    warrants). Companions attach to exact heads only; a phrase head reaches its own tail."""
    for h in heads:
        btok = brand_tokens(h["brand"])
        bare = not h["sig"]
        if other:
            h["match_type"] = "exact" if (not bare and h["volume"] >= exact_cutoff) else "phrase"
        else:
            h["match_type"] = "exact" if (bare or h["volume"] >= exact_cutoff) else "phrase"
        h["companions"] = pick_companions(h, btok) if h["match_type"] == "exact" else []


def build(db, out, exact_cutoff, floor, group_floor, pull_dates=None, exclude_geo=True, other_cap=None):
    gl = geoloop_date(db)
    cands = load_brand_candidates(db, pull_dates, exclude_geo)

    # bucket rows by (segment, brand); head-find per brand so thin carriers never cross-merge
    by_brand = defaultdict(list)
    for kw, vol, brand, status, cat in cands:
        brand = BRAND_ALIASES.get((brand or "").lower(), brand)
        by_brand[(segment_of(brand, cat), brand)].append((kw, vol, brand))

    brand_heads = {}
    for (seg, brand), items in by_brand.items():
        brand_heads[(seg, brand)] = select_heads_brand(items, floor, brand_tokens(brand))

    # assign brands to ad groups (carrier own-group vs CARRIER_OTHER; affinity/aggregator segments)
    adgroups = {}
    other = []
    for (seg, brand), heads in brand_heads.items():
        vol = sum(h["volume"] for h in heads)
        if seg == "AFFINITY":
            ag, camp = "AFFINITY", "USIA_HOME_BRAND_AFFINITY"
        elif seg == "AGGREGATOR":
            ag, camp = "AGGREGATOR", "USIA_HOME_BRAND_AGGREGATOR"
        else:                                # CARRIER:<brand>
            if vol >= group_floor:
                ag, camp = brand, "USIA_HOME_BRAND_CARRIER"
            else:
                other.append((brand, heads))
                continue
        adgroups.setdefault(ag, (camp, []))
        for h in heads:
            h["ad_group"], h["campaign"], h["brand"] = ag, camp, brand
            adgroups[ag][1].append(h)

    if other:
        rows = []
        for brand, heads in other:
            for h in heads:
                h["ad_group"], h["campaign"], h["brand"] = "CARRIER_OTHER", "USIA_HOME_BRAND_CARRIER", brand
                rows.append(h)
        rows.sort(key=lambda h: -h["volume"])
        if other_cap:
            rows = rows[:other_cap]
        adgroups["CARRIER_OTHER"] = ("USIA_HOME_BRAND_CARRIER", rows)

    selected, summary = {}, []
    for ag, (camp, heads) in adgroups.items():
        heads.sort(key=lambda h: -h["volume"])
        apply_match_types(heads, exact_cutoff, other=(ag == "CARRIER_OTHER"))
        selected[ag] = heads
        vol = sum(h["volume"] for h in heads)
        n_brands = len(set(h["brand"] for h in heads))
        n_exact = sum(1 for h in heads if h["match_type"] == "exact")
        n_comp = sum(len(h["companions"]) for h in heads)
        n_thin = sum(1 for h in heads if h["thin"])
        flag = "OK" if vol >= group_floor else "THIN"
        summary.append((camp, ag, n_brands, len(heads), n_exact, n_comp, vol, n_thin, flag))

    summary.sort(key=lambda s: (s[0], -s[6]))
    write_outputs(out, selected, summary, gl, exact_cutoff, floor, group_floor)
    return selected, summary


def write_outputs(out, selected, summary, gl, exact_cutoff, floor, group_floor):
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    HEAD = PatternFill("solid", fgColor="1F3864")
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

    wb = Workbook(); wb.remove(wb.active)

    ws = wb.create_sheet("Brand Group Summary")
    ws["A1"] = "USIA Home BRAND -- Conquest Selection Summary (v2)"; ws["A1"].font = TITLE
    ws.merge_cells("A1:H1")
    ws["A2"] = ("Biddable, home-focused brand terms (on-vertical, brand-tagged, not DNB, not adjacent). "
                "Pull %s. v2: intent-signature heads per brand; floor %s on summed covered demand; bare-brand "
                "head EXACT at any volume (+ over %s); modifier tail PHRASE + 1-2 companions. Carriers >= %s/mo "
                "get own ad group, thinner -> CARRIER_OTHER." %
                (gl, f"{floor:,}", f"{exact_cutoff:,}", f"{group_floor:,}"))
    ws["A2"].font = NOTE; ws.merge_cells("A2:H2")
    hdr(ws, 4, ["Campaign", "Ad Group", "# Brands", "Heads", "Exact", "Companions", "Volume", "Floor"])
    r = 5
    for camp, ag, nb, nh, ne, nc, vol, nt, flag in summary:
        ws.cell(r, 1, camp).font = REG
        ws.cell(r, 2, ag).font = BOLD
        ws.cell(r, 3, nb).font = REG
        ws.cell(r, 4, nh).font = REG
        ws.cell(r, 5, "%d / %d ph" % (ne, nh - ne)).font = REG
        ws.cell(r, 6, nc).font = REG
        ws.cell(r, 7, vol).font = REG; ws.cell(r, 7).number_format = "#,##0"
        c8 = ws.cell(r, 8, "%s (%d thin)" % (flag, nt))
        c8.font = Font(name="Arial", size=10, color="C00000" if flag == "THIN" else "1F7A1F")
        r += 1
    for col, w in {"A": 26, "B": 18, "C": 9, "D": 8, "E": 12, "F": 12, "G": 13, "H": 13}.items():
        ws.column_dimensions[col].width = w
    ws.freeze_panes = "A5"

    ws = wb.create_sheet("Brand Keywords")
    ws["A1"] = "Brand Keywords -- heads (exact bare-brand / phrase modifiers) + companions"; ws["A1"].font = TITLE
    ws.merge_cells("A1:H1")
    ws["A2"] = ("One row per HEAD, plus indented phrase COMPANION rows under each exact head (the modifier "
                "tail the exact bare-brand keyword won't match). Consolidated groups (CARRIER_OTHER, AFFINITY, "
                "AGGREGATOR) span brands -- see Brand column.")
    ws["A2"].font = NOTE; ws.merge_cells("A2:H2")
    hdr(ws, 4, ["Campaign", "Ad Group", "Brand", "Keyword", "Match Type", "Volume", "Role", "Covered Members"])
    r = 5
    for camp, ag, *_ in summary:
        for row in selected.get(ag, []):
            ws.cell(r, 1, row["campaign"]).font = REG
            ws.cell(r, 2, row["ad_group"]).font = BOLD
            ws.cell(r, 3, row["brand"]).font = REG
            kwc = ws.cell(r, 4, row["keyword"])
            kwc.font = Font(name="Arial", size=10, color="C00000" if row["thin"] else "000000")
            mt = ws.cell(r, 5, row["match_type"])
            mt.font = Font(name="Arial", size=10, bold=row["match_type"] == "exact",
                           color="1F3864" if row["match_type"] == "exact" else "000000")
            ws.cell(r, 6, row["volume"]).font = REG; ws.cell(r, 6).number_format = "#,##0"
            ws.cell(r, 7, "head").font = REG
            ws.cell(r, 8, row["members"]).font = NOTE
            ws.cell(r, 8).alignment = Alignment(wrap_text=True, vertical="top")
            r += 1
            for comp in row["companions"]:
                ws.cell(r, 1, row["campaign"]).font = NOTE
                ws.cell(r, 2, row["ad_group"]).font = NOTE
                ws.cell(r, 3, row["brand"]).font = NOTE
                ws.cell(r, 4, "   \u21b3 " + comp["keyword"]).font = REG
                ws.cell(r, 5, "phrase").font = REG
                ws.cell(r, 6, comp["volume"]).font = REG; ws.cell(r, 6).number_format = "#,##0"
                ws.cell(r, 7, "companion (call)" if comp["call"] else "companion").font = NOTE
                ws.cell(r, 8, "qualifies: " + row["keyword"]).font = NOTE
                r += 1
    for col, w in {"A": 26, "B": 16, "C": 16, "D": 40, "E": 11, "F": 11, "G": 16, "H": 56}.items():
        ws.column_dimensions[col].width = w
    ws.freeze_panes = "A5"
    wb.save(out)

    csv_path = out.rsplit(".", 1)[0] + ".csv"
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["campaign", "ad_group", "brand", "keyword", "match_type", "volume",
                    "role", "qualifies_head", "covered_members", "thin"])
        for camp, ag, *_ in summary:
            for row in selected.get(ag, []):
                w.writerow([row["campaign"], row["ad_group"], row["brand"], row["keyword"],
                            row["match_type"], row["volume"], "head", "", row["members"], int(row["thin"])])
                for comp in row["companions"]:
                    w.writerow([row["campaign"], row["ad_group"], row["brand"], comp["keyword"], "phrase",
                                comp["volume"], "companion_call" if comp["call"] else "companion",
                                row["keyword"], "", 0])
    print("wrote %s and %s" % (out, csv_path))


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--db", default=r"C:\Projects\google-ads-ops\db\demand_research.db")
    p.add_argument("--out", default="usia_home_brand_v2.xlsx")
    p.add_argument("--exact-cutoff", type=int, default=10000,
                   help="modifier head volume >= this -> exact (bare-brand head is always exact)")
    p.add_argument("--floor", type=int, default=300,
                   help="min SUMMED covered demand for a MODIFIER to earn its own phrase head (default 300)")
    p.add_argument("--group-floor", type=int, default=2000,
                   help="carrier own-ad-group threshold; thinner carriers -> CARRIER_OTHER (default 2000)")
    p.add_argument("--pull-dates", nargs="*", default=None,
                   help="restrict to these pull_date(s); default unions ALL pulls.")
    p.add_argument("--keep-geo", action="store_true",
                   help="keep state/abbr geo-modified brand terms (default: exclude).")
    p.add_argument("--other-cap", type=int, default=None,
                   help="row cap for CARRIER_OTHER only (default: uncapped).")
    a = p.parse_args()
    _, summary = build(a.db, a.out, a.exact_cutoff, a.floor, a.group_floor,
                       pull_dates=a.pull_dates, exclude_geo=not a.keep_geo, other_cap=a.other_cap)
    print("\nbrand ad groups (v2 intent-signature heads; bare-brand exact, modifiers phrase + companions):")
    cur = None
    for camp, ag, nb, nh, ne, nc, vol, nt, flag in summary:
        if camp != cur:
            print("  [%s]" % camp); cur = camp
        print("    %-18s brands=%-3d heads=%-3d exact=%-2d comp=%-2d vol=%-9s thin=%-2d  %s"
              % (ag, nb, nh, ne, nc, f"{vol:,}", nt, flag))


if __name__ == "__main__":
    main()
