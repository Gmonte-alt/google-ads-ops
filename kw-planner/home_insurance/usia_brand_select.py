#!/usr/bin/env python
"""
usia_brand_select.py -- build the USIA Home BRAND (conquest) campaigns.

Selects biddable, home-insurance-focused brand terms from demand_research.db and groups
them into conquest ad groups. Mirrors usia_keyword_select.py's engine (TF-IDF + sklearn
cosine dedup, canonical = highest volume, <=25 cap, exact/phrase) but groups by BRAND
instead of theme.

Eligibility: vertical=home, on-vertical, brand-tagged, biddable (dnb_flag=0 AND
brand_bid_status in approved/conditional), and NOT an adjacent vertical (home-warranty/
title brands like American Home Shield are dropped even though brand-tagged).

Structure (per Gilbert):
  - Carriers that individually clear the floor (>=2000/mo) -> their own ad group.
  - Thinner carriers -> consolidated into CARRIER_OTHER so the combined volume clears it.
  - AFFINITY (AAA / Costco / AARP / Sam's Club) -> their own ad group, separate from carriers.
  - AGGREGATOR (NerdWallet / Policygenius / Bankrate ...) -> their own ad group.
Campaigns: USIA_HOME_BRAND_CARRIER / _AFFINITY / _AGGREGATOR.

NOTE: run AFTER retag_demand_research.py --apply, or the brand tags are stale/sparse.

    python usia_brand_select.py --out usia_home_brand.xlsx
"""
import argparse
import csv
import re
import sqlite3
from collections import defaultdict

from usia_keyword_select import (cluster_theme, clean_keyword, ADJACENT_RE,
                                 STATE_RE, US_ABBR, dedup_key,
                                 AUTO_EXCLUDE_RE, HOME_TOKEN_RE)

AFFINITY_KEYS = ("aaa", "costco", "aarp", "sam's club", "sams club", "sam s club")

# Same-company brand strings the registry carries separately -> collapse to one ad group.
# Universal Insurance Holdings operates as Universal Property & Casualty (the home insurer),
# so both tags fold into one. (Durable home for this is the registry; this is the interim.)
BRAND_ALIASES = {
    "universal insurance": "Universal Property",
    "universal property": "Universal Property",
    "american bankers": "Assurant",       # American Bankers is Assurant's flood subsidiary
}

# Health / medicare carriers are NOT home insurers -- they leak in on "catastrophic insurance"
# and "house calls / home visit / at home" terms that the vertical classifier mis-tags as home.
# Hard-deny them from the home conquest workbook. (Durable fix: DNB/recategorize in the registry.)
HEALTH_DENY_RE = re.compile(
    r"\b(unitedhealthcare|united healthcare|uhc|bcbs|blue cross|blue shield|anthem|cigna|"
    r"humana|aetna|kaiser|wellcare|molina|centene|oscar health)\b")

# Carriers the registry currently mis-categorizes as aggregator -- force CARRIER regardless of
# the category field, until the registry/retag propagates the correct category. Hanover is a
# carrier; Assurant (incl. its American Bankers flood sub) is a carrier.
CARRIER_OVERRIDE = ("hanover", "assurant")

# Service / navigate-to-the-carrier terms are not new-business intent and stay excluded
# (someone searching "geico claims phone number" wants Geico's service line). But a BARE
# "[carrier] home insurance phone number" -- no service qualifier -- is a call-first
# opportunity (they want to talk to a human about home insurance), so it is NOT excluded
# and flows into the carrier's conquest ad group. The service qualifiers below still catch
# phone terms when paired (e.g. "...claims phone number" matches on "claims").
BRAND_NAVIGATE_RE = re.compile(
    r"\b(claims?|customer service|customer support|billing|home office|corporate office|"
    r"mailing address|fax|log ?in|sign ?in|pay (my )?bill|policy number|cancel|file a claim)\b")


# Local/county farm mutuals (e.g. "Pendleton County Farmers Fire Insurance Company") false-match
# the "Farmers" brand token but are unrelated small mutuals -- not conquest targets. Interim
# guard; durable fix is registry-side (DNB or tag as their own restricted brand).
BRAND_MISTAG_RE = re.compile(r"\b(county farmers|farmers mutual|farmers cooperative|farmers co-op)\b")


def is_adjacent(kw):
    return any(rx.search(kw) for _, rx in ADJACENT_RE)


def geoloop_date(db):
    conn = sqlite3.connect(db)
    d = conn.execute("""SELECT pull_date FROM kwp_ideas WHERE vertical='home'
                        GROUP BY pull_date ORDER BY COUNT(DISTINCT geo_constant_id) DESC LIMIT 1""").fetchone()
    conn.close()
    return d[0] if d else None


def load_brand_candidates(db, pull_dates=None, exclude_geo=True):
    """Biddable brand candidates. Unions across ALL pulls by default (MAX volume per
    keyword) so a later seed batch (peril) augments rather than replaces -- scoping to a
    single pull_date is what left every carrier with only brand+peril terms and none of
    the standard 'geico home insurance' conquest terms. Pass a list to restrict."""
    conn = sqlite3.connect(db)
    q = """
        SELECT keyword_text, MAX(avg_monthly_searches), brand, brand_bid_status, brand_category
        FROM kwp_ideas
        WHERE vertical='home' AND geo_name='United States' AND vertical_match='on_vertical'
          AND brand != '' AND dnb_flag=0 AND brand_bid_status IN ('approved','conditional')
          AND avg_monthly_searches > 0"""
    params = []
    if pull_dates:
        q += " AND pull_date IN (%s)" % ",".join("?" * len(pull_dates))
        params = list(pull_dates)
    q += " GROUP BY keyword_text"
    raw = conn.execute(q, params).fetchall()
    conn.close()
    out = []
    for kw, vol, brand, status, cat in raw:
        ck = clean_keyword(kw)
        if not ck or is_adjacent(ck):     # drop warranty/title/etc. even if brand-tagged
            continue
        if HEALTH_DENY_RE.search(ck) or HEALTH_DENY_RE.search((brand or "").lower()):
            continue                       # health/medicare carriers are not home insurers
        if BRAND_MISTAG_RE.search(ck):
            continue                       # county/mutual farm insurers mis-tagged as Farmers
        if AUTO_EXCLUDE_RE.search(ck) and not HOME_TOKEN_RE.search(ck):
            continue                       # pure auto brand term; home+auto bundles are kept
        if exclude_geo and (STATE_RE.search(ck) or (set(ck.split()) & US_ABBR)):
            continue                       # drop geo-modified (handled by location targeting)
        if BRAND_NAVIGATE_RE.search(ck):
            continue                       # drop service/navigate-to-carrier terms
        out.append((ck, int(vol or 0), brand, status or "", (cat or "")))
    return out


def segment_of(brand, cat):
    bl = (brand or "").lower()
    if any(k in bl for k in AFFINITY_KEYS):
        return "AFFINITY"
    if any(c in bl for c in CARRIER_OVERRIDE):     # mis-categorized carriers -> force CARRIER
        return "CARRIER:" + brand
    if "aggregator" in (cat or "").lower():
        return "AGGREGATOR"
    return "CARRIER:" + brand            # one bucket per carrier


def _cluster_rows(items, sim):
    """Cluster items -> canonical rows (all, sorted by volume desc)."""
    # collapse word-order + home-synonym dupes first (keep highest-volume surface + its brand)
    best = {}
    for kw, vol, brand in items:
        k = dedup_key(kw)
        if k not in best or (vol or 0) > best[k][1]:
            best[k] = (kw, vol, brand)
    items = list(best.values())
    kws = [k for k, _, _ in items]
    vols = [v for _, v, _ in items]
    brand_of = {k: b for k, _, b in items}
    clusters = cluster_theme(kws, vols, sim)
    rows = [{"keyword": kws[c[0]], "volume": vols[c[0]],
             "brand": brand_of[kws[c[0]]],
             "members": "; ".join(kws[m] for m in c)} for c in clusters]
    rows.sort(key=lambda r: -r["volume"])
    return rows


def build(db, out, sim, cap, exact_cutoff, floor, pull_dates=None, exclude_geo=True, other_cap=None):
    gl = geoloop_date(db)
    cands = load_brand_candidates(db, pull_dates, exclude_geo)

    # bucket by segment
    seg = defaultdict(list)              # segment key -> list[(kw, vol, brand)]
    for kw, vol, brand, status, cat in cands:
        brand = BRAND_ALIASES.get((brand or "").lower(), brand)   # merge same-company tags
        seg[segment_of(brand, cat)].append((kw, vol, brand))

    # carriers: decide own-group vs consolidate on POST-DEDUP/cap volume (consistent w/ floor)
    adgroups = {}                        # ad_group -> (campaign, kept_rows)
    other_items = []
    for key, items in seg.items():
        if not key.startswith("CARRIER:"):
            continue
        name = key.split(":", 1)[1]
        kept = _cluster_rows(items, sim)[:cap]
        if sum(r["volume"] for r in kept) >= floor:
            adgroups[name] = ("USIA_HOME_BRAND_CARRIER", kept)
        else:
            other_items.extend(items)    # too thin alone -> consolidate raw, re-cluster together
    if other_items:
        # CARRIER_OTHER intentionally NOT held to the per-group cap -- it's the catch-all for
        # thin carriers, and we want to capture as many real carrier terms as possible here.
        # other_cap=None -> uncapped; pass --other-cap N to bound it.
        adgroups["CARRIER_OTHER"] = ("USIA_HOME_BRAND_CARRIER", _cluster_rows(other_items, sim)[:other_cap])
    if seg.get("AFFINITY"):
        adgroups["AFFINITY"] = ("USIA_HOME_BRAND_AFFINITY", _cluster_rows(seg["AFFINITY"], sim)[:cap])
    if seg.get("AGGREGATOR"):
        adgroups["AGGREGATOR"] = ("USIA_HOME_BRAND_AGGREGATOR", _cluster_rows(seg["AGGREGATOR"], sim)[:cap])

    # match type + summary
    selected, summary = {}, []
    for ag, (camp, kept) in adgroups.items():
        for i, r in enumerate(kept):
            r["match_type"] = "exact" if (r["volume"] >= exact_cutoff or i == 0) else "phrase"
            r["ad_group"] = ag
            r["campaign"] = camp
        selected[ag] = kept
        kv = sum(r["volume"] for r in kept)
        nb = len(set(r["brand"] for r in kept))
        ne = sum(1 for r in kept if r["match_type"] == "exact")
        summary.append((camp, ag, nb, sum(1 for _ in kept), len(kept), kv, ne, len(kept) - ne,
                        "OK" if kv >= floor else "THIN"))

    summary.sort(key=lambda s: (s[0], -s[5]))
    write_outputs(out, selected, summary, gl, sim, cap, exact_cutoff, floor)
    return selected, summary


def write_outputs(out, selected, summary, gl, sim, cap, exact_cutoff, floor):
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

    ws = wb.create_sheet("Brand Ad Group Summary")
    ws["A1"] = "USIA Home BRAND (conquest) -- Ad Group Summary"; ws["A1"].font = TITLE
    ws.merge_cells("A1:I1")
    ws["A2"] = ("Source: demand_research.db (home, on-vertical, biddable brands only -- approved/conditional, "
                "not DNB, not adjacent). Pull %s. Carriers >=%s/mo get own ad group; thinner -> CARRIER_OTHER. "
                "TF-IDF+cosine>=%.2f dedup; cap %d; exact if vol>=%s or top-of-group." %
                (gl, f"{floor:,}", sim, cap, f"{exact_cutoff:,}"))
    ws["A2"].font = NOTE; ws.merge_cells("A2:I2")
    hdr(ws, 4, ["Campaign", "Ad Group", "# Brands", "Raw kw", "Selected", "Volume",
                "Exact", "Phrase", "Floor"])
    r = 5
    for camp, ag, nb, raw_n, nsel, vol, ne, nph, flag in summary:
        ws.cell(r, 1, camp).font = REG
        ws.cell(r, 2, ag).font = BOLD
        ws.cell(r, 3, nb).font = REG
        ws.cell(r, 4, raw_n).font = REG
        ws.cell(r, 5, nsel).font = REG
        ws.cell(r, 6, vol).font = REG; ws.cell(r, 6).number_format = "#,##0"
        ws.cell(r, 7, ne).font = REG
        ws.cell(r, 8, nph).font = REG
        c9 = ws.cell(r, 9, flag)
        c9.font = Font(name="Arial", size=10, color="C00000" if flag == "THIN" else "1F7A1F")
        r += 1
    for col, w in {"A": 26, "B": 18, "C": 9, "D": 8, "E": 9, "F": 13, "G": 8, "H": 8, "I": 8}.items():
        ws.column_dimensions[col].width = w
    ws.freeze_panes = "A5"

    ws = wb.create_sheet("Brand Keywords")
    ws["A1"] = "Brand Keywords -- by ad group"; ws["A1"].font = TITLE
    ws.merge_cells("A1:G1")
    ws["A2"] = ("One row per canonical brand keyword. Consolidated ad groups (CARRIER_OTHER, AFFINITY, "
                "AGGREGATOR) span multiple brands -- see Brand column. All terms biddable & home-focused.")
    ws["A2"].font = NOTE; ws.merge_cells("A2:G2")
    hdr(ws, 4, ["Campaign", "Ad Group", "Brand", "Keyword (canonical)", "Match Type", "Volume", "Cluster Members"])
    r = 5
    for camp, ag, *_ in summary:
        for row in selected.get(ag, []):
            ws.cell(r, 1, row["campaign"]).font = REG
            ws.cell(r, 2, row["ad_group"]).font = BOLD
            ws.cell(r, 3, row["brand"]).font = REG
            ws.cell(r, 4, row["keyword"]).font = REG
            mt = ws.cell(r, 5, row["match_type"])
            mt.font = Font(name="Arial", size=10, color="1F3864" if row["match_type"] == "exact" else "000000")
            ws.cell(r, 6, row["volume"]).font = REG; ws.cell(r, 6).number_format = "#,##0"
            ws.cell(r, 7, row["members"]).font = NOTE
            ws.cell(r, 7).alignment = Alignment(wrap_text=True, vertical="top")
            r += 1
    for col, w in {"A": 26, "B": 16, "C": 16, "D": 38, "E": 11, "F": 11, "G": 60}.items():
        ws.column_dimensions[col].width = w
    ws.freeze_panes = "A5"
    wb.save(out)

    csv_path = out.rsplit(".", 1)[0] + ".csv"
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["campaign", "ad_group", "brand", "keyword", "match_type", "volume", "cluster_members"])
        for camp, ag, *_ in summary:
            for row in selected.get(ag, []):
                w.writerow([row["campaign"], row["ad_group"], row["brand"], row["keyword"],
                            row["match_type"], row["volume"], row["members"]])
    print("wrote %s and %s" % (out, csv_path))


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--db", default=r"C:\Projects\google-ads-ops\db\demand_research.db")
    p.add_argument("--out", default="usia_home_brand.xlsx")
    p.add_argument("--sim", type=float, default=0.5)
    p.add_argument("--cap", type=int, default=25)
    p.add_argument("--exact-cutoff", type=int, default=10000)
    p.add_argument("--floor", type=int, default=2000,
                   help="carrier own-ad-group threshold AND consolidation target (default 2000)")
    p.add_argument("--pull-dates", nargs="*", default=None,
                   help="restrict to these pull_date(s); default unions ALL pulls so brand+peril augments the standard conquest terms.")
    p.add_argument("--keep-geo", action="store_true",
                   help="keep state/abbr geo-modified brand terms (default: exclude, since location targeting handles geo).")
    p.add_argument("--other-cap", type=int, default=None,
                   help="row cap for CARRIER_OTHER only (default: uncapped, to capture as many thin-carrier terms as possible).")
    a = p.parse_args()
    _, summary = build(a.db, a.out, a.sim, a.cap, a.exact_cutoff, a.floor,
                       pull_dates=a.pull_dates, exclude_geo=not a.keep_geo, other_cap=a.other_cap)
    print("\nbrand ad groups:")
    cur = None
    for camp, ag, nb, raw_n, nsel, vol, ne, nph, flag in summary:
        if camp != cur:
            print("  [%s]" % camp); cur = camp
        print("    %-16s brands=%-3d kept=%-3d vol=%-9s exact/phrase=%d/%d  %s"
              % (ag, nb, nsel, f"{vol:,}", ne, nph, flag))


if __name__ == "__main__":
    main()
