#!/usr/bin/env python3
"""Quote-first search-term GP analysis, sourced from paid_search.db (no UI export needed).

Ranks Quote's OWN search queries by realized GP over a date window, tags brand vs non-brand and intent,
flags usia_targeted as a COLUMN (the USIA lens preserved, not a gate), and -- with --prior-start/--prior-end
-- finds LOST VOLUME: queries that converted in the prior window but are gone/declined in the current one.

Reads the search-query performance table (auto-resolves name: search_query_performance_daily / sqp /
search_term_performance_daily / similar) using the account conventions:
  cost = cost_micros/1e6 ; conversions = primary_conv_count ; value = primary_conv_value
  GP   = primary_conv_value - cost  (realized, not the UI report's GP column)

Freshness note: the sqp sync trails ~6 days. Irrelevant for YoY comparisons; for "current period" reads,
know the last few days are empty. The script prints the max date it found so the window is explicit.

Usage:
  # YoY lost-volume: Jan-Jul 2025 vs Jan-Jul 2026 (matched windows -> fair decline read)
  python quote_search_term_gp_db.py --db C:\\Projects\\google-ads-ops\\db\\paid_search.db ^
      --start 2026-01-01 --end 2026-07-31 --prior-start 2025-01-01 --prior-end 2025-07-31 ^
      --brand-csv brand_registry_export.csv --usia-csv kwp_ideas_export_v2.csv
  # single-period ranking only: omit --prior-*
"""
import argparse
import csv
import os
import re
import sqlite3

ACCOUNT_ID = 1
GENERIC_STOP = {"insurance", "home", "house", "homeowners", "quote", "quotes", "cost", "cheap", "best",
                "compare", "online", "company", "policy", "coverage", "near", "the", "and", "for",
                "auto", "car", "life", "health", "property", "owner", "owners", "rate", "rates"}

# schema pinned to search_query_performance + search_queries (see resolve)


def resolve(conn):
    """Fixed to the confirmed schema: search_query_performance (facts: query_id, report_date, platform,
    cost_micros, primary_conv_*) joined to search_queries (id -> query_text). Gated to Google Home via
    platform='google' AND campaigns.account_id=1 -- the DB holds multiple Google accounts (Home=1,
    USIA=6), so platform alone is not enough. Verifies tables/columns and fails loudly if schema moved."""
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view')")]
    for t in ("search_query_performance", "search_queries", "campaigns"):
        if t not in tables:
            raise SystemExit("Expected table %r not found. Tables: %s" % (t, ", ".join(sorted(tables))))
    perf = [r[1] for r in conn.execute("PRAGMA table_info(search_query_performance)")]
    qc = [r[1] for r in conn.execute("PRAGMA table_info(search_queries)")]
    need_perf = ["query_id", "report_date", "platform", "cost_micros",
                 "primary_conv_count", "primary_conv_value", "campaign_id"]
    missing = [c for c in need_perf if c not in perf] + [c for c in ("id", "query_text") if c not in qc]
    if missing:
        raise SystemExit("Missing columns %s (schema changed?). perf=%s search_queries=%s"
                         % (missing, perf, qc))
    print("SCHEMA RESOLVED: search_query_performance.query_id -> search_queries.id (query_text); "
          "date=report_date; gate=platform='google' + campaigns.account_id=%d" % ACCOUNT_ID)
    return {}


def window(conn, s, start, end):
    q = ("SELECT LOWER(TRIM(sq.query_text)) AS t, SUM(p.cost_micros)/1000000.0 AS cost, "
         "SUM(p.primary_conv_count) AS conv, SUM(p.primary_conv_value) AS val "
         "FROM search_query_performance p "
         "JOIN search_queries sq ON sq.id = p.query_id "
         "JOIN campaigns c ON c.campaign_id = p.campaign_id AND c.account_id = ? "
         "WHERE p.platform = 'google' AND p.report_date >= ? AND p.report_date <= ? "
         "GROUP BY LOWER(TRIM(sq.query_text))")
    out = {}
    for t, cost, conv, val in conn.execute(q, (ACCOUNT_ID, start, end)):
        if not t or t.startswith("total:"):
            continue
        out[t] = dict(cost=cost or 0.0, conv=conv or 0.0, val=val or 0.0, gp=(val or 0.0) - (cost or 0.0))
    return out


def build_brand_matcher(brand_csv):
    toks = set()
    if brand_csv and os.path.exists(brand_csv):
        for r in csv.DictReader(open(brand_csv, encoding="utf-8-sig")):
            for col in ("normalized_name", "brand_name"):
                v = re.sub(r"[^a-z0-9 ]", "", (r.get(col) or "").strip().lower()).strip()
                if len(v) >= 4 and v not in GENERIC_STOP:
                    toks.add(v)
    toks |= {"geico", "usaa", "farmers", "liberty mutual", "state farm", "statefarm", "progressive",
             "allstate", "nationwide", "travelers", "kin", "lemonade", "hippo", "amica", "erie", "safeco",
             "hartford", "american family", "amfam", "aaa", "chubb", "assurant", "homesite", "mercury",
             "tgs", "alfa", "country financial", "pure", "qbe", "the general", "matic", "insurify",
             "everquote", "safeu", "penn national", "smart financial", "farm bureau", "plymouth rock"}
    toks = {t for t in toks if t not in GENERIC_STOP}
    rx = re.compile(r"\b(" + "|".join(sorted((re.escape(t) for t in toks), key=len, reverse=True)) + r")\b")
    return lambda x: bool(rx.search(x))


def load_usia(usia_csv):
    if not usia_csv or not os.path.exists(usia_csv):
        return set()
    return {(r.get("keyword_text") or "").strip().lower()
            for r in csv.DictReader(open(usia_csv, encoding="utf-8-sig"))} - {""}


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
    ap = argparse.ArgumentParser(description="Quote-first search-term GP from paid_search.db + YoY lost volume")
    ap.add_argument("--db", required=True)
    ap.add_argument("--start", required=True, help="current window start YYYY-MM-DD")
    ap.add_argument("--end", required=True, help="current window end YYYY-MM-DD")
    ap.add_argument("--prior-start", help="prior window start (enables lost-volume)")
    ap.add_argument("--prior-end", help="prior window end")
    ap.add_argument("--brand-csv")
    ap.add_argument("--usia-csv")
    ap.add_argument("--out-dir", default="quote_search_term_output")
    ap.add_argument("--min-prior-conv", type=float, default=10.0)
    ap.add_argument("--decline-frac", type=float, default=0.30)
    a = ap.parse_args()

    conn = sqlite3.connect("file:%s?mode=ro" % a.db, uri=True)
    s = resolve(conn)
    maxd = conn.execute("SELECT MAX(report_date) FROM search_query_performance "
                        "WHERE platform='google'").fetchone()[0]
    print("data through: %s" % maxd)

    is_brand = build_brand_matcher(a.brand_csv)
    usia = load_usia(a.usia_csv)
    cur = window(conn, s, a.start, a.end)
    os.makedirs(a.out_dir, exist_ok=True)

    ranked = sorted(cur.items(), key=lambda kv: -kv[1]["gp"])
    with open(os.path.join(a.out_dir, "search_term_gp_ranked.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Search term", "brand", "intent", "usia_targeted", "conv", "cost", "value", "GP", "ROAS"])
        for t, x in ranked:
            w.writerow([t, "brand" if is_brand(t) else "nonbrand", intent(t),
                        "yes" if t in usia else "no", round(x["conv"], 1), round(x["cost"]),
                        round(x["val"]), round(x["gp"]),
                        round(x["val"] / x["cost"], 2) if x["cost"] else 0])
    nb_gp = sum(x["gp"] for t, x in cur.items() if not is_brand(t))
    print("current window %s..%s: %d terms | non-brand GP %.0f | wrote search_term_gp_ranked.csv"
          % (a.start, a.end, len(cur), nb_gp))

    if a.prior_start and a.prior_end:
        prior = window(conn, s, a.prior_start, a.prior_end)
        lost = []
        for t, p in prior.items():
            if p["conv"] < a.min_prior_conv:
                continue
            c = cur.get(t, dict(conv=0.0, gp=0.0))
            if c["conv"] <= p["conv"] * a.decline_frac:
                lost.append((t, "brand" if is_brand(t) else "nonbrand", intent(t),
                             "yes" if t in usia else "no",
                             "fully_lost" if c["conv"] < 1 else "declined",
                             p["conv"], p["gp"], c["conv"], c["gp"], p["gp"] - c["gp"]))
        lost.sort(key=lambda r: -r[9])
        with open(os.path.join(a.out_dir, "lost_volume.csv"), "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["Search term", "brand", "intent", "usia_targeted", "status",
                        "prior_conv", "prior_GP", "current_conv", "current_GP", "GP_lost"])
            for r in lost:
                w.writerow([r[0], r[1], r[2], r[3], r[4], round(r[5], 1), round(r[6]),
                            round(r[7], 1), round(r[8]), round(r[9])])
        fully = [r for r in lost if r[4] == "fully_lost"]
        print("prior %s..%s: lost/declined %d (%d fully lost) | reclaimable GP %.0f | wrote lost_volume.csv"
              % (a.prior_start, a.prior_end, len(lost), len(fully), sum(r[9] for r in lost)))
        print("\ntop 15 reclaimable (by GP lost):")
        for r in lost[:15]:
            print("  %-46s %-9s %-14s prior_conv=%-6.0f GP_lost=%-7.0f %s"
                  % (r[0][:46], r[1], r[2], r[5], r[9], r[4]))
    conn.close()
    print("\nwrote -> %s" % os.path.abspath(a.out_dir))


if __name__ == "__main__":
    main()
