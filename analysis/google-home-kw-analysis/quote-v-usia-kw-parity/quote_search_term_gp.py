#!/usr/bin/env python3
"""Quote-first search-term GP analysis. UN-ANCHORS from USIA: ranks Quote's OWN search terms by realized
GP, tags brand vs non-brand and intent, and flags which terms USIA also targets -- as a COLUMN, not a
filter -- so the USIA lens is preserved without letting it define the opportunity space.

With a PRIOR-period export it also finds LOST VOLUME: terms Quote converted well on last year but isn't
capturing now. That's reclaimable demand -- the fastest way to rebuild account volume is to win back what
you already proved converts, rather than prospecting net-new.

Usage:
  # single-period ranking:
  python quote_search_term_gp.py --current 2026_search_terms.csv --brand-csv brand_registry_export.csv
  # year-over-year (what did we lose?):
  python quote_search_term_gp.py --current 2026_search_terms.csv --prior 2025_search_terms.csv \\
      --brand-csv brand_registry_export.csv --usia-csv kwp_ideas_export_v2.csv

Handles Google's report quirks: encoding (utf-8 or utf-16), the 2-row preamble, the 'total:' subtotal
rows, and oversized fields. Reads the report's GP column directly when present.

WINDOW NOTE: for a fair 'lost' read, compare comparable windows (e.g. Jan-Jul 2025 vs Jan-Jul 2026). A
full prior year vs a partial current year will over-flag declines just from the shorter window -- so the
output separates FULLY-LOST terms (strong prior, ~zero current -- window-independent) from DECLINED ones.
"""
import argparse
import csv
import io
import os
import re

csv.field_size_limit(10 ** 7)

GENERIC_STOP = {"insurance", "home", "house", "homeowners", "quote", "quotes", "cost", "cheap", "best",
                "compare", "online", "company", "policy", "coverage", "near", "the", "and", "for",
                "auto", "car", "life", "health", "property", "owner", "owners", "rate", "rates"}


def read_report(path):
    """Parse a Google search-terms/keyword report -> list of dict rows (subtotal + blank rows dropped)."""
    data = open(path, "rb").read()
    txt = None
    for enc in ("utf-8-sig", "utf-8", "utf-16"):
        try:
            t = data.decode(enc)
            txt = t
            break
        except Exception:
            continue
    if txt is None:
        txt = data.decode("utf-8", errors="replace")
    lines = txt.splitlines()
    hi = next((i for i, l in enumerate(lines[:20])
               if ("Search term" in l or "Search keyword" in l or "Keyword" in l)
               and ("Cost" in l or "Clicks" in l or "Conversions" in l)), None)
    if hi is None:
        hi = next((i for i, l in enumerate(lines[:20]) if "Search term" in l or "Keyword" in l), 0)
    delim = "\t" if lines[hi].count("\t") > lines[hi].count(",") else ","
    rd = csv.DictReader(io.StringIO("\n".join(lines[hi:])), delimiter=delim)
    out = []
    for r in rd:
        st = (r.get("Search term") or r.get("Search keyword") or r.get("Keyword") or "").strip()
        if not st or st.lower().startswith("total:"):
            continue
        out.append(r)
    return out


def num(v):
    try:
        return float(str(v).replace(",", "").replace("$", "").replace("%", "") or 0)
    except Exception:
        return 0.0


def aggregate(rows):
    from collections import defaultdict
    cols = list(rows[0].keys()) if rows else []
    stcol = next((c for c in cols if c.strip() in ("Search term", "Search keyword", "Keyword")), "Search term")
    gpcol = next((c for c in cols if c.strip() == "GP"), None)
    vcol = next((c for c in cols if c.strip() in ("Conv. value", "Conv value", "Conversion value", "All conv. value")), None)
    ccol = next((c for c in cols if c.strip() == "Conversions"), None)
    costcol = next((c for c in cols if c.strip() == "Cost"), None)
    agg = defaultdict(lambda: dict(conv=0.0, cost=0.0, val=0.0, gp=0.0))
    for r in rows:
        t = (r.get(stcol) or "").strip().lower()
        if not t:
            continue
        a = agg[t]
        a["conv"] += num(r.get(ccol)); a["cost"] += num(r.get(costcol)); a["val"] += num(r.get(vcol))
        a["gp"] += num(r.get(gpcol)) if gpcol else (num(r.get(vcol)) - num(r.get(costcol)))
    return dict(agg)


def build_brand_matcher(brand_csv):
    toks = set()
    if brand_csv and os.path.exists(brand_csv):
        for r in csv.DictReader(open(brand_csv, encoding="utf-8-sig")):
            for col in ("normalized_name", "brand_name"):
                v = (r.get(col) or "").strip().lower()
                v = re.sub(r"[^a-z0-9 ]", "", v).strip()
                if len(v) >= 4 and v not in GENERIC_STOP:
                    toks.add(v)
    # supplement with common carriers/aggregators that may be thin in the registry
    toks |= {"geico", "usaa", "farmers", "liberty mutual", "state farm", "statefarm", "progressive",
             "allstate", "nationwide", "travelers", "kin", "lemonade", "hippo", "amica", "erie", "safeco",
             "hartford", "american family", "amfam", "aaa", "chubb", "assurant", "homesite", "mercury",
             "metlife", "esurance", "foremost", "openly", "branch", "citizens", "heritage", "tower hill",
             "centauri", "trumbull", "wellington", "njm", "tgs", "alfa", "country financial", "pure",
             "qbe", "the general", "matic", "insurify", "everquote", "safeu", "penn national", "smart financial",
             "farm bureau", "national general", "bristol west", "auto owners", "auto-owners", "plymouth rock"}
    toks = {t for t in toks if t not in GENERIC_STOP}
    if not toks:
        return lambda s: False
    rx = re.compile(r"\b(" + "|".join(sorted((re.escape(t) for t in toks), key=len, reverse=True)) + r")\b")
    return lambda s: bool(rx.search(s))


def load_usia(usia_csv):
    if not usia_csv or not os.path.exists(usia_csv):
        return set()
    s = set()
    for r in csv.DictReader(open(usia_csv, encoding="utf-8-sig")):
        k = (r.get("keyword_text") or "").strip().lower()
        if k:
            s.add(k)
    return s


INTENT = [
    ("LANDLORD_RENTAL", re.compile(r"\b(rental propert|rental home|landlord|rent out|investment propert|buy to let|airbnb|vacation rental|second home)\b")),
    ("ROOF_CLAIMS", re.compile(r"\b(roof|water damage|storm damage|hail|wind damage|does insurance cover|covered by insurance|file a claim|filing a claim)\b")),
    ("RENTERS", re.compile(r"\b(renters?|tenant)\b")),
    ("CONDO", re.compile(r"\b(condo|townhome|townhouse)\b")),
    ("MOBILE_MANUF", re.compile(r"\b(mobile home|manufactured home|trailer|modular home)\b")),
    ("COST_PRICE", re.compile(r"\b(cost|price|pricing|how much|cheap|affordable|estimate|calculator|average)\b")),
    ("QUOTE", re.compile(r"\bquotes?\b")),
    ("COMPARE", re.compile(r"\b(compare|comparison|best|top|vs|versus|review|reviews|rating)\b")),
    ("NEAR_ME", re.compile(r"\b(near me|nearby|local|in my area)\b")),
    ("COVERAGE", re.compile(r"\b(coverage|dwelling|liability|replacement cost|personal property|deductible|umbrella)\b")),
    ("AGENT_BROKER", re.compile(r"\b(agent|broker|agency|provider)\b")),
    ("GENERIC_HOME", re.compile(r"\b(home insurance|homeowners insurance|house insurance|home owners|property insurance|hazard insurance|dwelling insurance)\b")),
]


def intent(t):
    for name, rx in INTENT:
        if rx.search(t):
            return name
    return "OTHER"


def main():
    ap = argparse.ArgumentParser(description="Quote-first search-term GP ranking + lost-volume YoY")
    ap.add_argument("--current", required=True, help="current-period search-terms export")
    ap.add_argument("--prior", help="prior-period export (enables lost-volume analysis)")
    ap.add_argument("--brand-csv", help="brand_registry_export.csv (for brand/non-brand tagging)")
    ap.add_argument("--usia-csv", help="kwp_ideas_export_v2.csv (adds a usia_targeted flag column)")
    ap.add_argument("--out-dir", default="quote_search_term_output")
    ap.add_argument("--min-prior-conv", type=float, default=10.0, help="min prior conversions to flag a lost term")
    ap.add_argument("--decline-frac", type=float, default=0.30, help="current<=prior*frac => declined")
    a = ap.parse_args()

    is_brand = build_brand_matcher(a.brand_csv)
    usia = load_usia(a.usia_csv)
    cur = aggregate(read_report(a.current))
    os.makedirs(a.out_dir, exist_ok=True)

    def tag(t):
        return ("brand" if is_brand(t) else "nonbrand", intent(t), ("yes" if t in usia else "no"))

    # 1) current-period ranking
    ranked = sorted(cur.items(), key=lambda kv: -kv[1]["gp"])
    with open(os.path.join(a.out_dir, "search_term_gp_ranked.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Search term", "brand", "intent", "usia_targeted", "conv", "cost", "value", "GP", "ROAS"])
        for t, x in ranked:
            b, i, u = tag(t)
            roas = x["val"] / x["cost"] if x["cost"] else 0
            w.writerow([t, b, i, u, round(x["conv"], 1), round(x["cost"]), round(x["val"]), round(x["gp"]), round(roas, 2)])
    nb_gp = sum(x["gp"] for t, x in cur.items() if not is_brand(t))
    print("current terms: %d | non-brand GP: %.0f | wrote search_term_gp_ranked.csv" % (len(cur), nb_gp))

    # 2) lost-volume (year-over-year)
    if a.prior:
        prior = aggregate(read_report(a.prior))
        lost = []
        for t, p in prior.items():
            if p["conv"] < a.min_prior_conv:
                continue
            c = cur.get(t, dict(conv=0.0, gp=0.0, cost=0.0, val=0.0))
            if c["conv"] <= p["conv"] * a.decline_frac:  # gone or sharply declined
                status = "fully_lost" if c["conv"] < 1 else "declined"
                b, i, u = tag(t)
                lost.append((t, b, i, u, status, p["conv"], p["gp"], c["conv"], c["gp"], p["gp"] - c["gp"]))
        lost.sort(key=lambda r: -r[9])  # by GP lost
        with open(os.path.join(a.out_dir, "lost_volume.csv"), "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["Search term", "brand", "intent", "usia_targeted", "status",
                        "prior_conv", "prior_GP", "current_conv", "current_GP", "GP_lost"])
            for r in lost:
                w.writerow([r[0], r[1], r[2], r[3], r[4], round(r[5], 1), round(r[6]),
                            round(r[7], 1), round(r[8]), round(r[9])])
        tot_lost = sum(r[9] for r in lost)
        fully = [r for r in lost if r[4] == "fully_lost"]
        print("lost/declined terms: %d (%d fully lost) | reclaimable GP: %.0f | wrote lost_volume.csv"
              % (len(lost), len(fully), tot_lost))
        print("\ntop 15 reclaimable (by GP lost):")
        for r in lost[:15]:
            print("  %-46s %-9s %-9s prior_conv=%-6.0f GP_lost=%-7.0f %s"
                  % (r[0][:46], r[1], r[2], r[5], r[9], r[4]))
    print("\nwrote -> %s" % os.path.abspath(a.out_dir))


if __name__ == "__main__":
    main()
