"""
analyze_keyword_themes.py — read-only: keyword-THEME GP/RPL/CPL, April vs recent.
Google Home account ONLY (account_id=1 — Google data; Microsoft is a different
account and is never touched here). Emits a campaign × ad_group × theme cut.

Themes (FIRST-MATCH, brand-specific first):
  geico              -> keyword contains "geico"
  farmers            -> keyword contains "farmers"
  all_other_carriers -> any other carrier brand (allstate, state farm, progressive,
                        liberty mutual, usaa, nationwide, travelers, american family,
                        erie, safeco, root, lemonade, hippo, aaa, mercury, hartford ...)
  generic            -> everything else (no carrier brand)

Granularity:
  - the three ISSUE campaigns (Tier One / Tier Four / California): one row per
    AD GROUP × theme × period (ad-group breakout).
  - every other campaign: one row per theme × period (campaign-level baseline),
    ad_group = "(campaign)".

Device: routed primary conv lives on device='ALL' only (the script prints a
runtime CHECK confirming this). All metrics are pulled at device='ALL'.

GAQL/SQLite truth notes (unchanged):
  - primary_conv_* on device='ALL' rows -> filter device='ALL'.
  - scope via ki.campaign_id (kpd.campaign_id is NULL on device rows).
  - compare by campaign_id, not name (June rename).

Usage:
    python analyze_keyword_themes.py
    python analyze_keyword_themes.py --a-start 2026-04-01 --a-end 2026-04-30 \
        --b-start 2026-05-18 --b-end 2026-06-07
"""
import argparse, csv, re, sqlite3, sys
from collections import defaultdict
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
DB = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")
ACCT = 1  # Google Home account — GOOGLE DATA ONLY. Microsoft is a different account_id.

ISSUE = {"Tier One", "Tier Four", "California"}          # ad-group granularity
TIER_NAMES = ["Tier One", "Tier Two", "Tier Three", "Tier Four", "California", "Texas", "Florida"]
THEME_ORDER = ["geico", "farmers", "all_other_carriers", "generic"]

# ── theme classifier: brand-specific first, then other carriers, then generic ──
GEICO   = re.compile(r"geico", re.I)
FARMERS = re.compile(r"farmers", re.I)
OTHER_CARRIERS = re.compile(
    r"allstate|state\s*farm|progressive|liberty\s*mutual|usaa|nationwide|travelers|"
    r"american\s*family|amfam|erie|safeco|the\s*general|root|lemonade|hippo|"
    r"aaa|csaa|wawanesa|bamboo|mercury|hartford", re.I)
def classify(text):
    t = text or ""
    if GEICO.search(t):   return "geico"
    if FARMERS.search(t): return "farmers"
    if OTHER_CARRIERS.search(t): return "all_other_carriers"
    return "generic"

def label(name):
    u = (name or "").upper()
    for k in TIER_NAMES:
        if k.upper() in u:
            return k
    return name


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--a-start", default="2026-04-01"); p.add_argument("--a-end", default="2026-04-30")
    p.add_argument("--b-start", default="2026-05-18"); p.add_argument("--b-end", default="2026-06-07")
    p.add_argument("--out", default="keyword_theme_cut.csv")
    return p.parse_args()


def device_check(conn, start, end):
    """Confirm whether routed primary conv exists on device<>'ALL' rows."""
    r = conn.execute(
        "SELECT COALESCE(SUM(CASE WHEN kpd.device<>'ALL' THEN kpd.primary_conv_count ELSE 0 END),0) nonall, "
        "       COALESCE(SUM(CASE WHEN kpd.device='ALL'  THEN kpd.primary_conv_count ELSE 0 END),0) allc "
        "FROM keyword_performance_daily kpd JOIN keyword_instances ki ON ki.id=kpd.instance_id "
        "WHERE ki.account_id=? AND kpd.report_date BETWEEN ? AND ?",
        (ACCT, start, end)).fetchone()
    return r["nonall"] or 0, r["allc"] or 0


def campaigns_with_data(conn, start, end):
    return conn.execute(
        "SELECT c.campaign_id, c.campaign_name, COALESCE(SUM(kpd.clicks),0) clicks "
        "FROM campaigns c "
        "JOIN keyword_instances ki ON ki.campaign_id=c.campaign_id AND ki.account_id=c.account_id "
        "JOIN keyword_performance_daily kpd ON kpd.instance_id=ki.id "
        "WHERE c.account_id=? AND kpd.device='ALL' AND kpd.report_date BETWEEN ? AND ? "
        "GROUP BY c.campaign_id, c.campaign_name HAVING SUM(kpd.clicks)>0 "
        "ORDER BY clicks DESC", (ACCT, start, end)).fetchall()


def pull(conn, cid, start, end):
    """Ad-group + keyword grain (device='ALL'). NOTE: assumes keyword_instances
    exposes ad_group_name. If your schema names it differently (e.g. ad_group_id,
    or a join to an ad_groups table), tell Claude and we adjust this one SELECT."""
    return conn.execute(
        "SELECT ki.ad_group_name AS ag, ki.keyword_text AS kw, "
        "       SUM(kpd.clicks) clicks, SUM(kpd.cost_micros)/1e6 cost, "
        "       SUM(kpd.primary_conv_count) conv, SUM(kpd.primary_conv_value) val "
        "FROM keyword_performance_daily kpd JOIN keyword_instances ki ON ki.id=kpd.instance_id "
        "WHERE ki.account_id=? AND ki.campaign_id=? AND kpd.device='ALL' "
        "  AND kpd.report_date BETWEEN ? AND ? "
        "GROUP BY ki.ad_group_name, ki.keyword_text", (ACCT, cid, start, end)).fetchall()


def agg(rows, by_adgroup):
    """-> {(ad_group_or_'(campaign)', theme): {clicks,cost,conv,val}}"""
    d = defaultdict(lambda: {"clicks": 0, "cost": 0.0, "conv": 0.0, "val": 0.0})
    for r in rows:
        key = (r["ag"] if by_adgroup else "(campaign)", classify(r["kw"]))
        t = d[key]
        t["clicks"] += r["clicks"] or 0; t["cost"] += r["cost"] or 0
        t["conv"] += r["conv"] or 0; t["val"] += r["val"] or 0
    return d


def main():
    a = parse_args()
    if not DB.exists():
        sys.exit(f"DB not found: {DB}")
    conn = sqlite3.connect(DB); conn.row_factory = sqlite3.Row

    print("=" * 80)
    print(f"  Google Home (account_id={ACCT}, GOOGLE DATA ONLY) — keyword THEME cut, A vs B")
    print(f"  A {a.a_start}..{a.a_end}   B {a.b_start}..{a.b_end}")
    print(f"  themes: {', '.join(THEME_ORDER)}   |   ad-group breakout for: {', '.join(sorted(ISSUE))}")
    print("=" * 80)

    nonall, allc = device_check(conn, a.a_start, a.b_end)
    verdict = ("CONFIRMED device='ALL'-only — value metrics cannot be split by device."
               if nonall == 0 else
               "NON-ZERO on non-ALL rows — a device value split IS possible; tell Claude.")
    print(f"  DEVICE CHECK: routed primary conv on device<>'ALL' = {nonall:,.0f}  "
          f"(device='ALL' = {allc:,.0f})  -> {verdict}\n")

    camps = campaigns_with_data(conn, a.a_start, a.b_end)
    out_rows = []
    for c in camps:
        lab = label(c["campaign_name"]); issue = lab in ISSUE
        A = agg(pull(conn, c["campaign_id"], a.a_start, a.a_end), issue)
        B = agg(pull(conn, c["campaign_id"], a.b_start, a.b_end), issue)
        # campaign-level theme totals for the printout (collapse ad groups)
        tot = defaultdict(lambda: {"A": {"val": 0, "cost": 0, "conv": 0}, "B": {"val": 0, "cost": 0, "conv": 0}})
        for store, per in ((A, "A"), (B, "B")):
            for (ag, th), v in store.items():
                tot[th][per]["val"] += v["val"]; tot[th][per]["cost"] += v["cost"]; tot[th][per]["conv"] += v["conv"]
        print(f"  ── {lab} (id {c['campaign_id']}){' [ad-group rows]' if issue else ''} — GP by theme (A->B) ──")
        print(f"     {'theme':18} {'A GP':>9} {'B GP':>9} {'GP Δ':>9} | {'A RPL':>6} {'B RPL':>6}")
        for th in THEME_ORDER:
            t = tot.get(th)
            if not t: continue
            gpa = t["A"]["val"] - t["A"]["cost"]; gpb = t["B"]["val"] - t["B"]["cost"]
            ra = t["A"]["val"]/t["A"]["conv"] if t["A"]["conv"] else 0
            rb = t["B"]["val"]/t["B"]["conv"] if t["B"]["conv"] else 0
            print(f"     {th:18} {gpa:9,.0f} {gpb:9,.0f} {gpb-gpa:9,.0f} | {ra:6.1f} {rb:6.1f}")
        # emit rows (ad-group grain for issue, campaign grain otherwise)
        keys = sorted(set(A) | set(B),
                      key=lambda k: (k[0], THEME_ORDER.index(k[1]) if k[1] in THEME_ORDER else 9))
        for (ag, th) in keys:
            for per, store in (("A", A), ("B", B)):
                v = store.get((ag, th))
                if not v: continue
                out_rows.append([lab, c["campaign_id"], ag, th, per,
                                 round(v["val"], 2), round(v["cost"], 2), round(v["conv"], 1), v["clicks"],
                                 round(v["val"] - v["cost"], 2),
                                 round(v["val"]/v["conv"], 2) if v["conv"] else "",
                                 round(v["cost"]/v["conv"], 2) if v["conv"] else ""])
    conn.close()

    with Path(a.out).open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["campaign", "campaign_id", "ad_group", "theme", "period",
                    "revenue", "cost", "conversions", "clicks", "GP", "RPL", "CPL"])
        w.writerows(out_rows)
    print(f"\n  CSV -> {Path(a.out).resolve()}  ({len(out_rows)} rows; campaign × ad_group × theme × period)")
    print("  Verify the DEVICE CHECK line and the geico/farmers/other/generic shares look sane,")
    print("  then run build_performance_review_workbook.py.")


if __name__ == "__main__":
    main()
