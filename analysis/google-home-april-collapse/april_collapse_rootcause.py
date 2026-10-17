#!/usr/bin/env python3
"""april_collapse_rootcause.py -- Analysis 4: the April exposure collapse + CTR/QS
decomposition (auction-health root-cause). EXPLANATION, NOT ACTION: attributes,
recommends nothing.

Merged investigation:
  WS1  What happened in April -- monthly Jan-Aug timeline per campaign & account,
       by query class, from BOTH sqp (query-grain truth, privacy-suppression floor)
       and kpd (complete, keyword-mediated). The two sources bracket the truth;
       the kpd-vs-sqp gap is itself an exhibit (gap widening in April = part of the
       "disappearance" is measurement). Eligible-impressions proxy (impr/IS, pinned
       method, labeled proxy) gives the demand-vs-share verdict. March TARGET_ROAS
       batches overlaid. Framing exhibit: "farmers home insurance" impressions,
       double-read (sqp + kpd).
  WS2  CTR fixed-mix counterfactual (same design as rpl_decomposition: hold class
       weights at the anchor month, recompute blended CTR; gap = mix, remainder =
       within-class), within-class CTR trends, and the QS layer strictly per the
       house recipe (see IRON RULES). Timing verdict: does auction-health
       degradation LEAD, COINCIDE WITH, or LAG the April volume collapse.

CLASS DEFINITIONS (ruled 8/24, printed at runtime; reconciliation proves
equivalence to Analysis 2's instrument):
  HOME_INTENT  text matches PRODUCT regex \\b(home|homeowner|homeowners|house)\\b,
               with or without a carrier brand (product token presence defines
               home-intent regardless of brand)
  FLAGSHIP     carrier-brand token present AND no product token; geo-variants
               ("farmers insurance texas") are still FLAGSHIP
  GENERIC      neither brand nor product token (insurance-intent by construction
               of the account's matched universe; no extra filter applied)
Brand detection: classification tables' brand column when a row exists (rebuilt
pre-run), token-match fallback against the brand vocabulary derived from those
tables for unclassified text. Product detection: ALWAYS the regex on raw text --
the regex IS the definition.

IRON RULES (encoded, same as geico_variant_qs.py / architect's canonical recipe):
  - QS source = keyword_quality_daily ONLY; kpd's own four QS-named columns are
    superseded and NULL -- never read them.
  - Join QS at DAILY grain on (instance_id, report_date); never latest-per-key.
  - iw_QS = SUM(historical_quality_score * impressions) / SUM(impressions) over
    rated rows, NEVER AVG. Unrated excluded; rated_share ships alongside EVERY
    iw_QS (rated coverage collapsed ~130K -> ~9K kw-days/mo: any QS move that
    arrives with a rated_share move is composition until proven otherwise).
  - match_type held as a dimension in the CPC-QS read.
  - device='ALL' on all performance queries; account scoping through joins
    (kpd has NO account_id -> keyword_instances; sqp is NOT account-scoped ->
    JOIN campaigns c ON c.campaign_id = p.campaign_id AND c.account_id = 1).
  - DB opened read-only (mode=ro); treat all data complete (house ruling 8/20,
    no lag exclusion).
  - search_queries.first_seen_date valid for novelty/staleness checks only
    (2024 backfill reset the floor).

GATES:
  1. Staleness gate: refuses (unless --force-stale) when queries/keywords first
     seen after the classification tables' last rebuild exceed --staleness-days.
     Rebuild both classification tables before the real run.
  2. Reconciliation gate: reproduces Analysis 2's anchors (TX home-intent leads
     Mar 783 / Apr 358; T2 Mar 781) within --recon-tol (default 10%). August
     anchors (~90 TX, 83 T2, flagship shares 57-68% tiers / ~31% TX) are
     INFORMATIONAL only -- August is a moving MTD window and those figures were
     read ~8/21-22. Charts are gated on the strict anchors passing
     (--force-recon to override).

KNOWN LIMITATION (stated up front, per charter): competitor IDENTITY lives in
Auction Insights, which is not in the DB. This analysis can establish WHETHER we
were outcompeted, not BY WHOM. If the share-loss verdict lands, the console
verdict block names the specific UI pulls (campaign, class, month range) needed
to identify the competitor.

Outputs (--out-dir):
  run_header.txt                 self-documenting header: definitions, resolved
                                 source map, gates, reconciliation results
  ws1_class_monthly_sqp.csv      month x campaign x class (sqp): impr, clicks,
                                 CTR, cost, CPC, leads, revenue
  ws1_class_monthly_kpd.csv      same grain from kpd (+ rated_share, iw_QS)
  ws1_sqp_kpd_gap.csv            month x campaign x class: kpd vs sqp impressions,
                                 suppressed share -- the bracket exhibit
  ws1_eligible_impressions.csv   month x campaign: impr, IS (impr-weighted),
                                 eligible = SUM(impr/IS) [PROXY], IS coverage
  ws1_farmers_home_exhibit.csv   monthly "farmers + product" impressions,
                                 sqp AND kpd, per campaign + account
  ws1_class_matchtype_served_sqp.csv  month x campaign x class x match_type_served:
                                 what match type captured each class over time
  ws1_target_events.csv          TARGET_ROAS change events (overlay source)
  ws2_ctr_decomposition.csv      month x campaign: actual vs fixed-mix CTR,
                                 mix vs within-class attribution
  ws2_qs_monthly_class.csv       month x scope(campaign|ACCOUNT) x class: iw_QS,
                                 rated_share, sub-component shares
  ws2_qs_cpc_matchtype.csv       month x class x match_type: iw_QS, rated_share,
                                 CPC (the CPC-QS read)
  ws2_timing_table.csv           account + TX monthly health series feeding the
                                 timing verdict
  charts/*.png                   gated on reconciliation PASS (or --force-recon)
  console                        gates, reconciliation, timing verdict block

Run order (architect's loop discipline):
  1. python april_collapse_rootcause.py --db ... --smoke     (seconds; schema/join shakeout,
     March strict anchors print)
  2. full run only after smoke passes:
     python april_collapse_rootcause.py --db C:\\Projects\\google-ads-ops\\db\\paid_search.db
  CSVs always precede charts; charts are gated on reconciliation PASS.

Reconciliation caveat: a small (~+1%) uniform upward offset vs the anchors is expected
restatement (GBRAIDS-class conversion backfills), not a definition mismatch -- the
tolerance absorbs it; reconcile primarily on March/April.
"""
import argparse, csv, os, re, sqlite3, sys
from collections import defaultdict

ACCOUNT_ID = 1
PRODUCT = re.compile(r"\b(home|homeowner|homeowners|house)\b")
HOME, FLAG, GEN = "HOME_INTENT", "FLAGSHIP", "GENERIC"
CLASSES = (HOME, FLAG, GEN)

# The 7 immutable core campaign ids (ids never change; names do). Everything else
# in account 1 (retired legacy shells etc.) rolls into OTHER for charts/verdicts
# but keeps its own rows in the CSVs -- Jan-Mar volume lives partly in retired ids.
CORE = {
    23385188622: "CA", 23394808297: "TX", 23394808300: "FL",
    23428389081: "T4", 23496334752: "T1", 23530860942: "T2", 23586637678: "T3",
}
TX_ID, T2_ID, T3_ID = 23394808297, 23530860942, 23586637678
TIER_IDS = (23496334752, 23530860942, 23586637678, 23428389081)

# Analysis 2 anchor numbers. strict anchors gate the charts; info anchors print
# only (August = moving MTD, read ~8/21-22).
RECON = [
    # (label, kind, strict?)
    ("TX home-intent leads 2026-03", ("leads", TX_ID, HOME, "2026-03", 783.0), True),
    ("TX home-intent leads 2026-04", ("leads", TX_ID, HOME, "2026-04", 358.0), True),
    ("T2 home-intent leads 2026-03", ("leads", T2_ID, HOME, "2026-03", 781.0), True),
    ("TX home-intent leads 2026-08 (~90, MTD @8/22)", ("leads", TX_ID, HOME, "2026-08", 90.0), False),
    ("T2 home-intent leads 2026-08 (83, MTD @8/22)", ("leads", T2_ID, HOME, "2026-08", 83.0), False),
    ("TX flagship share of leads 2026-08 (~31%)", ("flagshare", TX_ID, None, "2026-08", 0.31), False),
    ("T2 flagship share of leads 2026-08 (57-68% band)", ("flagshare", T2_ID, None, "2026-08", 0.62), False),
]

DEF_TEXT = (
    "HOME_INTENT = text matches %s (with or without brand; product token defines home-intent)\n"
    "FLAGSHIP    = carrier-brand token present AND no product token (geo-variants still flagship)\n"
    "GENERIC     = neither brand nor product token (insurance-intent by account construction)\n"
    "brand source = classification tables' brand column (rebuilt pre-run); token fallback for\n"
    "unclassified text uses the brand vocabulary derived from those same tables.\n"
    % PRODUCT.pattern
)


# ---------------------------------------------------------------- introspection
def cols(conn, table):
    try:
        return [r[1] for r in conn.execute("PRAGMA table_info(%s)" % table)]
    except sqlite3.Error:
        return []


def pick(available, candidates, what, table, required=True):
    low = {c.lower(): c for c in available}
    for cand in candidates:
        if cand.lower() in low:
            return low[cand.lower()]
    if required:
        sys.exit("FATAL: could not resolve %s on %s. Columns present: %s\n"
                 "Pin the correct column name in resolve_sources() and rerun."
                 % (what, table, ", ".join(available) or "(table missing)"))
    return None


def resolve_sources(conn):
    """Pin joins/filters per the house conventions; resolve only the column names
    memory doesn't pin. Everything resolved is printed in the header so the first
    real run documents its bindings for veto."""
    s = {}
    # required metric columns -- verify, don't guess silently
    for t, need in (("keyword_performance_daily",
                     ["instance_id", "report_date", "device", "impressions", "clicks", "cost_micros"]),
                    ("search_query_performance",
                     ["query_id", "campaign_id", "platform", "device", "report_date",
                      "impressions", "clicks", "cost_micros", "primary_conv_count",
                      "primary_conv_value"]),
                    ("keyword_quality_daily",
                     ["instance_id", "report_date", "historical_quality_score",
                      "hist_predicted_ctr", "hist_creative_quality", "hist_landing_page_quality"])):
        have = cols(conn, t)
        missing = [c for c in need if c not in have]
        if missing:
            sys.exit("FATAL: %s missing expected columns %s (has: %s)" % (t, missing, have))
    kpd = cols(conn, "keyword_performance_daily")
    s["kpd_leads"] = pick(kpd, ["primary_conv_count"], "kpd leads column", "keyword_performance_daily")
    s["kpd_rev"] = pick(kpd, ["primary_conv_value"], "kpd revenue column", "keyword_performance_daily", required=False)

    ki = cols(conn, "keyword_instances")
    s["ki_campaign"] = "campaign_id" if "campaign_id" in ki else None  # else via ad_groups (tripwire fallback)
    if not s["ki_campaign"] and "ad_group_id" not in ki:
        sys.exit("FATAL: keyword_instances has neither campaign_id nor ad_group_id (has: %s)" % ki)

    # impression share (ad_group_performance_daily)
    agp = cols(conn, "ad_group_performance_daily")
    s["agp_is"] = pick(agp, ["search_impression_share", "impression_share", "search_impr_share"],
                       "impression-share column", "ad_group_performance_daily")
    s["agp_adgroup"] = pick(agp, ["ad_group_id", "adgroup_id"], "ad_group key", "ad_group_performance_daily")
    s["agp_date"] = pick(agp, ["report_date", "date", "report_day", "stat_date", "day", "event_date"],
                         "date column", "ad_group_performance_daily")
    s["agp_device"] = pick(agp, ["device", "segment_device", "device_type"],
                           "device column", "ad_group_performance_daily", required=False)
    if s["agp_device"]:
        devs = [r[0] for r in conn.execute(
            "SELECT DISTINCT %s FROM ad_group_performance_daily LIMIT 25" % s["agp_device"])]
        if "ALL" not in devs:
            sys.exit("FATAL: ad_group_performance_daily.%s has no 'ALL' value (values: %s). "
                     "Pin the correct device filter for the eligible-impressions pull -- "
                     "filtering wrong here silently double-counts eligible impressions."
                     % (s["agp_device"], devs))
    ag_cols = cols(conn, "ad_groups")
    s["ag_key"] = pick(ag_cols, ["id", "ad_group_id"], "ad_groups primary key", "ad_groups")
    s["ag_campaign"] = pick(ag_cols, ["campaign_id"], "ad_groups campaign key", "ad_groups")
    # IS scale auto-detect (fraction vs percent)
    row = conn.execute("SELECT AVG(%s) FROM (SELECT %s FROM ad_group_performance_daily "
                       "WHERE %s IS NOT NULL AND %s > 0 LIMIT 5000)"
                       % ((s["agp_is"],) * 4)).fetchone()
    s["is_scale"] = 100.0 if (row and row[0] and row[0] > 1.5) else 1.0

    # change events
    cce = cols(conn, "campaign_change_events")
    s["cce_date"] = pick(cce, ["event_date", "change_date", "event_time", "changed_at",
                               "occurred_at", "report_date", "created_at", "date"],
                         "event date column", "campaign_change_events")
    s["cce_new"] = pick(cce, ["new_value", "new_target", "value_new"], "new_value column",
                        "campaign_change_events", required=False)

    # classification tables
    qc = cols(conn, "query_classification")
    s["qc_key"] = pick(qc, ["query_id", "sq_id", "id"], "query_classification join key", "query_classification")
    s["qc_brand"] = pick(qc, ["brand", "brand_name", "carrier", "carrier_brand"],
                         "query_classification brand column", "query_classification")
    s["qc_ts"] = pick(qc, ["classified_at", "built_at", "updated_at", "created_at"],
                      "query_classification timestamp", "query_classification", required=False)
    kc = cols(conn, "keyword_classification")
    s["kc_present"] = bool(kc)
    if kc:
        s["kc_key"] = pick(kc, ["instance_id", "keyword_instance_id", "keyword_id", "id"],
                           "keyword_classification join key", "keyword_classification", required=False)
        s["kc_text"] = pick(kc, ["keyword_text", "keyword"], "keyword_classification text",
                            "keyword_classification", required=False)
        s["kc_mt"] = pick(kc, ["match_type"], "keyword_classification match_type",
                          "keyword_classification", required=False)
        s["kc_brand"] = pick(kc, ["brand", "brand_name", "carrier", "carrier_brand"],
                             "keyword_classification brand column", "keyword_classification", required=False)
        s["kc_ts"] = pick(kc, ["classified_at", "built_at", "updated_at", "created_at"],
                          "keyword_classification timestamp", "keyword_classification", required=False)
    return s


# ------------------------------------------------------------------ classifier
class Classer(object):
    def __init__(self, brand_vocab):
        # token set + multiword phrases from the classification tables' own brands
        self.phrases, toks = [], set()
        for b in brand_vocab:
            b = (b or "").strip().lower()
            if not b:
                continue
            if " " in b:
                self.phrases.append(b)
            toks.add(b.split(" ")[0] if " " in b else b)
            toks.add(b.replace(" ", ""))
        self.tokens = toks
        self.stats = defaultdict(int)

    def has_brand_token(self, text):
        if any(p in text for p in self.phrases):
            return True
        return any(t in self.tokens for t in text.split())

    def classify(self, text, joined_brand):
        t = (text or "").lower().strip()
        if PRODUCT.search(t):
            return HOME  # product token defines home-intent regardless of brand
        branded = bool(joined_brand and str(joined_brand).strip())
        if not branded:
            branded = self.has_brand_token(t)
            if branded:
                self.stats["brand_by_token_fallback"] += 1
        else:
            self.stats["brand_by_table"] += 1
        return FLAG if branded else GEN


# ------------------------------------------------------------------ small utils
def d(v):
    return (v or 0)


def nid(x):
    """normalize campaign ids: the DB stores campaign_id as TEXT in places --
    '23394808297' and 23394808297 must be the same dict key (smoke-run bug 8/24)."""
    if isinstance(x, str) and x.strip().isdigit():
        return int(x.strip())
    return x


def rd(x, n=4):
    return round(x, n) if isinstance(x, float) else x


def campaign_names(conn):
    names = dict(CORE)
    try:
        for cid, nm in conn.execute(
                "SELECT campaign_id, campaign_name FROM campaigns WHERE account_id=?", (ACCOUNT_ID,)):
            names.setdefault(nid(cid), nm or str(cid))
    except sqlite3.Error:
        pass
    return names


def write_csv(path, header, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)
    print("wrote %s (%d rows)" % (path, len(rows)))


# ---------------------------------------------------------------------- gates
def staleness_gate(conn, s, args, hdr):
    """Refuse when placements/queries first seen after the classification rebuild
    would roll into GENERIC (the exact failure that bit the QS thread's q7)."""
    problems = []
    ts = None
    if s.get("qc_ts"):
        ts = conn.execute("SELECT MAX(%s) FROM query_classification" % s["qc_ts"]).fetchone()[0]
    fs = conn.execute("SELECT MAX(first_seen_date) FROM search_queries").fetchone()[0]
    n_unclassed = conn.execute(
        "SELECT COUNT(*) FROM search_queries sq LEFT JOIN query_classification qc "
        "ON qc.%s = sq.id WHERE qc.%s IS NULL AND sq.first_seen_date >= ?" % (s["qc_key"], s["qc_key"]),
        (args.start,)).fetchone()[0]
    hdr.append("staleness: query_classification last build=%s, MAX(first_seen)=%s, "
               "unclassified queries first seen >= %s: %d" % (ts, fs, args.start, n_unclassed))
    if ts and fs and str(fs)[:10] > str(ts)[:10]:
        gap = (int(str(fs)[:4]) * 372 + int(str(fs)[5:7]) * 31 + int(str(fs)[8:10])) - \
              (int(str(ts)[:4]) * 372 + int(str(ts)[5:7]) * 31 + int(str(ts)[8:10]))
        if gap > args.staleness_days:
            problems.append("query_classification is ~%dd stale vs newest query (%s > %s)"
                            % (gap, fs, ts))
    if s.get("kc_present") and s.get("kc_ts"):
        kts = conn.execute("SELECT MAX(%s) FROM keyword_classification" % s["kc_ts"]).fetchone()[0]
        hdr.append("staleness: keyword_classification last build=%s" % kts)
    if problems:
        for p in problems:
            print("STALENESS GATE: " + p)
        if not args.force_stale and not args.smoke:
            sys.exit("Refusing to run on a stale classification (rebuild both tables, "
                     "or --force-stale to override -- results will misfile new text into GENERIC).")
        print("  --force-stale set: continuing; interpret class mix moves with suspicion.")
    else:
        print("staleness gate: PASS")


def reconcile(kpd_agg, sqp_agg, names, tol, hdr):
    """Prove definition equivalence to Analysis 2 by reproducing its anchors.
    Evaluated at KEYWORD grain (kpd): the smoke run showed Analysis 2's anchor
    numbers reproduce at kpd (+0.3/+0.4%) and NOT at sqp (query-grain leads run
    ~70% low from privacy suppression) -- kpd is the instrument being matched.
    The sqp value prints alongside as living evidence of the suppression bracket."""
    def leads(agg, cid, cls, ym):
        return d(agg.get((ym, cid, cls), {}).get("leads"))

    def flagshare(cid, ym):
        tot = sum(leads(kpd_agg, cid, c, ym) for c in CLASSES)
        return (leads(kpd_agg, cid, FLAG, ym) / tot) if tot else 0.0

    ok = True
    months_present = set(k[0] for k in kpd_agg)
    print("\nRECONCILIATION vs Analysis 2 anchors, KPD GRAIN (tol +/-%.0f%% on strict rows):" % (tol * 100))
    caveat = ("NOTE: if a small (~+1%) uniform upward offset appears across every anchor, it is "
              "conversion-history restatement (GBRAIDS-class backfills), not a definition mismatch "
              "-- do not chase it; the tolerance absorbs it. Reconcile primarily on March/April; "
              "August is the softest number (partial month + moving MTD).")
    print(caveat)
    hdr.append("reconciliation:")
    hdr.append(caveat)
    for label, (kind, cid, cls, ym, expect), strict in RECON:
        if ym not in months_present:
            line = "  %-52s [skipped: %s outside run window]" % (label, ym)
            print(line); hdr.append(line)
            continue
        got = leads(kpd_agg, cid, cls, ym) if kind == "leads" else flagshare(cid, ym)
        dev = (got - expect) / expect if expect else 0.0
        if kind == "flagshare":
            line = "  %-52s expected %.0f%%  got %.1f%%" % (label, expect * 100, got * 100)
        else:
            line = ("  %-52s expected %6.0f  got %8.1f  (%+.1f%%)  [sqp: %.1f]"
                    % (label, expect, got, dev * 100, leads(sqp_agg, cid, cls, ym)))
        if strict:
            passed = abs(dev) <= tol
            ok &= passed
            line += "  [STRICT %s]" % ("PASS" if passed else "FAIL")
        else:
            line += "  [info: MTD moved since 8/22]"
        print(line)
        hdr.append(line)
    verdict = ("PASS -- reconstruction is the same instrument as Analysis 2; downstream comparable."
               if ok else
               "FAIL -- reconstruction diverges from Analysis 2's instrument. STOP AND RECONCILE "
               "the class definitions before trusting charts (charts gated off).")
    print("reconciliation verdict: " + verdict)
    hdr.append("reconciliation verdict: " + verdict)
    return ok


# ------------------------------------------------------------------------ main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--start", default="2026-01-01")
    ap.add_argument("--end", default="2026-12-31")
    ap.add_argument("--anchor", default="2026-03", help="fixed-mix baseline month (YYYY-MM)")
    ap.add_argument("--out-dir", default=r"C:\Projects\google-ads-ops\analysis\google-home-april-collapse\output")
    ap.add_argument("--recon-tol", type=float, default=0.10)
    ap.add_argument("--staleness-days", type=int, default=7)
    ap.add_argument("--force-stale", action="store_true")
    ap.add_argument("--force-recon", action="store_true", help="draw charts even on reconciliation FAIL")
    ap.add_argument("--no-charts", action="store_true")
    ap.add_argument("--smoke", action="store_true",
                    help="fast schema/join shakeout: anchor month only, no charts, "
                         "gates warn instead of refuse; March strict anchors still print")
    a = ap.parse_args()
    if a.smoke:
        a.start, a.end = a.anchor + "-01", a.anchor + "-31"
        a.no_charts = True
        print("SMOKE MODE: window %s..%s, charts off, gates warn-only -- run full only "
              "after this passes and the March anchors print sane." % (a.start, a.end))
    conn = sqlite3.connect("file:%s?mode=ro" % a.db, uri=True)
    os.makedirs(a.out_dir, exist_ok=True)
    hdr = ["april_collapse_rootcause.py -- run header (self-documenting)",
           "objective: explanation, not action -- attribute everything, recommend nothing",
           "", "CLASS DEFINITIONS:", DEF_TEXT,
           "KNOWN LIMITATION: competitor identity lives in Auction Insights (not in DB); this",
           "analysis establishes WHETHER we were outcompeted, not BY WHOM.", ""]

    s = resolve_sources(conn)
    hdr.append("RESOLVED SOURCE MAP (verify on first real run):")
    for k in sorted(s):
        hdr.append("  %s = %s" % (k, s[k]))
    hdr.append("  IS scale detected: %s" % ("percent/100" if s["is_scale"] == 100.0 else "fraction"))
    if not s.get("agp_device"):
        hdr.append("  NOTE: ad_group_performance_daily has no device column -- rows assumed "
                   "single-grain; verify eligible proxy against a known campaign IS in the UI once.")
    hdr.append("")

    staleness_gate(conn, s, a, hdr)
    names = campaign_names(conn)

    # brand vocabulary from the classification tables themselves
    vocab = set(r[0] for r in conn.execute(
        "SELECT DISTINCT %s FROM query_classification WHERE %s IS NOT NULL AND %s != ''"
        % ((s["qc_brand"],) * 3)))
    if s.get("kc_present") and s.get("kc_brand"):
        vocab |= set(r[0] for r in conn.execute(
            "SELECT DISTINCT %s FROM keyword_classification WHERE %s IS NOT NULL AND %s != ''"
            % ((s["kc_brand"],) * 3)))
    clf = Classer(vocab)
    hdr.append("brand vocabulary: %d distinct brands loaded from classification tables" % len(vocab))

    # keyword-side brand from keyword_classification when instance-joinable
    kc_brand = {}
    if s.get("kc_present") and s.get("kc_key") in ("instance_id", "keyword_instance_id") and s.get("kc_brand"):
        for iid, b in conn.execute("SELECT %s, %s FROM keyword_classification" % (s["kc_key"], s["kc_brand"])):
            if b:
                kc_brand[iid] = b
    hdr.append("keyword-side brand rows joinable from keyword_classification: %d" % len(kc_brand))

    farmers_home = lambda t: ("farmers" in t) and bool(PRODUCT.search(t))

    # ---------------- WS1 pull A: sqp (query-grain truth, suppression floor) ----
    # scoping per 8/23 convention: sqp is NOT account-scoped -> JOIN campaigns.
    sqp_agg = defaultdict(lambda: defaultdict(float))      # (ym, cid, class) -> sums
    sqp_mts = defaultdict(lambda: defaultdict(float))      # (ym, cid, class, match_type_served)
    farm_sqp = defaultdict(float)                          # (ym, cid) -> impressions
    qclass = {}
    q = ("SELECT SUBSTR(p.report_date,1,7) ym, p.campaign_id, p.query_id, sq.query_text, qc.%s, "
         " UPPER(COALESCE(p.match_type_served,'UNKNOWN')), "
         " SUM(p.impressions), SUM(p.clicks), SUM(p.cost_micros), SUM(p.primary_conv_count), "
         " SUM(COALESCE(p.primary_conv_value,0)) "
         " FROM search_query_performance p "
         " JOIN campaigns c ON c.campaign_id = p.campaign_id AND c.account_id = ? "
         " JOIN search_queries sq ON sq.id = p.query_id "
         " LEFT JOIN query_classification qc ON qc.%s = sq.id "
         " WHERE p.platform='google' AND p.device='ALL' AND p.report_date BETWEEN ? AND ? "
         " GROUP BY 1,2,3,6" % (s["qc_brand"], s["qc_key"]))
    for ym, cid, qid, text, brand, mts, im, ck, cm, lv, rv in conn.execute(q, (ACCOUNT_ID, a.start, a.end)):
        cid = nid(cid)
        cl = qclass.get(qid)
        if cl is None:
            cl = qclass[qid] = clf.classify(text, brand)
        x = sqp_agg[(ym, cid, cl)]
        x["impr"] += d(im); x["clicks"] += d(ck); x["cost"] += d(cm) / 1e6
        x["leads"] += d(lv); x["rev"] += d(rv)
        m = sqp_mts[(ym, cid, cl, mts)]
        m["impr"] += d(im); m["clicks"] += d(ck); m["cost"] += d(cm) / 1e6; m["leads"] += d(lv)
        if farmers_home((text or "").lower()):
            farm_sqp[(ym, cid)] += d(im)

    # ---------------- WS1 pull B + WS2 QS: kpd (complete, keyword-mediated) -----
    # single daily-grain pass serves the kpd trends, the QS layer, and match_type.
    ki_join = ("ki.campaign_id" if s["ki_campaign"] else "ag." + s["ag_campaign"])
    ag_join = ("" if s["ki_campaign"] else
               " JOIN ad_groups ag ON ag.%s = ki.ad_group_id " % s["ag_key"])
    kpd_agg = defaultdict(lambda: defaultdict(float))      # (ym, cid, class)
    qs_mt = defaultdict(lambda: defaultdict(float))        # (ym, class, match_type)
    farm_kpd = defaultdict(float)
    kclass = {}
    q = ("SELECT SUBSTR(p.report_date,1,7) ym, %s cid, p.instance_id, "
         " LOWER(ki.keyword_text), ki.match_type, "
         " SUM(p.impressions), SUM(p.clicks), SUM(p.cost_micros), SUM(p.%s), "
         " SUM(CASE WHEN q.historical_quality_score IS NOT NULL THEN p.impressions ELSE 0 END), "
         " SUM(CASE WHEN q.historical_quality_score IS NOT NULL THEN q.historical_quality_score*p.impressions END), "
         " SUM(CASE WHEN q.hist_predicted_ctr='ABOVE_AVERAGE' THEN p.impressions ELSE 0 END), "
         " SUM(CASE WHEN q.hist_predicted_ctr='BELOW_AVERAGE' THEN p.impressions ELSE 0 END), "
         " SUM(CASE WHEN q.hist_creative_quality='ABOVE_AVERAGE' THEN p.impressions ELSE 0 END), "
         " SUM(CASE WHEN q.hist_landing_page_quality='ABOVE_AVERAGE' THEN p.impressions ELSE 0 END) "
         " FROM keyword_performance_daily p "
         " JOIN keyword_instances ki ON ki.id = p.instance_id AND ki.account_id = ? %s"
         " LEFT JOIN keyword_quality_daily q ON q.instance_id = p.instance_id "
         "   AND q.report_date = p.report_date "
         " WHERE p.device='ALL' AND p.report_date BETWEEN ? AND ? "
         " GROUP BY 1,2,3" % (ki_join, s["kpd_leads"], ag_join))
    for (ym, cid, iid, text, mt, im, ck, cm, lv,
         ri, qsw, ca, cb, cra, lpa) in conn.execute(q, (ACCOUNT_ID, a.start, a.end)):
        cid = nid(cid)
        cl = kclass.get(iid)
        if cl is None:
            cl = kclass[iid] = clf.classify(text, kc_brand.get(iid))
        x = kpd_agg[(ym, cid, cl)]
        x["impr"] += d(im); x["clicks"] += d(ck); x["cost"] += d(cm) / 1e6; x["leads"] += d(lv)
        x["ri"] += d(ri); x["qsw"] += d(qsw); x["ca"] += d(ca); x["cb"] += d(cb)
        x["cra"] += d(cra); x["lpa"] += d(lpa)
        m = qs_mt[(ym, cl, (mt or "UNKNOWN").upper())]
        m["impr"] += d(im); m["clicks"] += d(ck); m["cost"] += d(cm) / 1e6
        m["ri"] += d(ri); m["qsw"] += d(qsw)
        if farmers_home(text or ""):
            farm_kpd[(ym, cid)] += d(im)

    # ---------------- WS1 pull C: eligible impressions proxy (campaign grain) ---
    # per architect: exact weight = eligible impressions = impressions / IS.
    # GRAIN LIMITATION (stated): ad groups mix flagship + home-intent keywords of a
    # carrier, so IS cannot be split by query class -- campaign/account level only.
    elig = defaultdict(lambda: defaultdict(float))         # (ym, cid)
    dev_f = ("p.%s='ALL' AND " % s["agp_device"]) if s["agp_device"] else ""
    q = ("SELECT SUBSTR(p.%s,1,7) ym, ag.%s, "
         " SUM(p.impressions), "
         " SUM(CASE WHEN p.%s > 0 THEN p.impressions END), "
         " SUM(CASE WHEN p.%s > 0 THEN p.impressions / (p.%s / %f) END) "
         " FROM ad_group_performance_daily p "
         " JOIN ad_groups ag ON ag.%s = p.%s "
         " JOIN campaigns c ON c.campaign_id = ag.%s AND c.account_id = ? "
         " WHERE %s p.%s BETWEEN ? AND ? GROUP BY 1,2"
         % (s["agp_date"], s["ag_campaign"], s["agp_is"], s["agp_is"], s["agp_is"], s["is_scale"],
            s["ag_key"], s["agp_adgroup"], s["ag_campaign"], dev_f, s["agp_date"]))
    for ym, cid, im, im_cov, el in conn.execute(q, (ACCOUNT_ID, a.start, a.end)):
        cid = nid(cid)
        x = elig[(ym, cid)]
        x["impr"] += d(im); x["impr_covered"] += d(im_cov); x["eligible"] += d(el)

    # ---------------- WS1 pull D: TARGET_ROAS events -----------------------------
    ev_rows = []
    try:
        q = ("SELECT e.%s, e.campaign_id, %s FROM campaign_change_events e "
             " JOIN campaigns c ON c.campaign_id = e.campaign_id AND c.account_id = ? "
             " WHERE e.change_type='TARGET_ROAS' AND LOWER(COALESCE(e.platform,'google'))='google' "
             " ORDER BY 1" % (s["cce_date"], ("e.%s" % s["cce_new"]) if s["cce_new"] else "NULL"))
        for dt, cid, nv in conn.execute(q, (ACCOUNT_ID,)):
            cid = nid(cid)
            ev_rows.append([str(dt)[:10], cid, names.get(cid, cid), nv])
    except sqlite3.Error as e:
        hdr.append("WARNING: change-events pull failed (%s) -- overlay omitted" % e)
    write_csv(os.path.join(a.out_dir, "ws1_target_events.csv"),
              ["date", "campaign_id", "campaign", "new_target"], ev_rows)

    # ================================ CSVs =======================================
    def class_rows(agg, qs_cols):
        rows = []
        months = sorted(set(k[0] for k in agg))
        # ACCOUNT rollup rows first-class alongside campaigns
        acct = defaultdict(lambda: defaultdict(float))
        for (ym, cid, cl), x in agg.items():
            for k, v in x.items():
                acct[(ym, cl)][k] += v
        def emit(ym, scope_id, scope, cl, x):
            im, ck = x["impr"], x["clicks"]
            if not im and not ck and not x["leads"]:
                return None
            r = [ym, scope_id, scope, cl, int(im), int(ck),
                 rd(ck / im if im else 0.0), rd(x["cost"], 2),
                 rd(x["cost"] / ck if ck else 0.0, 2), rd(x["leads"], 1), rd(x.get("rev", 0.0), 2)]
            if qs_cols:
                ri = x["ri"]
                r += [int(ri), rd(ri / im if im else 0.0, 3),
                      rd(x["qsw"] / ri, 2) if ri else "",
                      rd(x["ca"] / ri, 3) if ri else "", rd(x["cb"] / ri, 3) if ri else "",
                      rd(x["cra"] / ri, 3) if ri else "", rd(x["lpa"] / ri, 3) if ri else ""]
            return r
        for ym in months:
            for cl in CLASSES:
                if (ym, cl) in acct:
                    r = emit(ym, "", "ACCOUNT", cl, acct[(ym, cl)])
                    if r: rows.append(r)
        for (ym, cid, cl) in sorted(agg, key=lambda k: (k[0], str(k[1]), k[2])):
            r = emit(ym, cid, names.get(cid, str(cid)), cl, agg[(ym, cid, cl)])
            if r: rows.append(r)
        return rows, acct

    base = ["month", "campaign_id", "campaign", "class", "impressions", "clicks", "ctr",
            "cost", "cpc", "leads", "revenue"]
    sqp_rows, sqp_acct = class_rows(sqp_agg, qs_cols=False)
    write_csv(os.path.join(a.out_dir, "ws1_class_monthly_sqp.csv"), base, sqp_rows)
    qs_hdr = base + ["rated_impressions", "rated_share", "iw_qs", "pred_ctr_above_share",
                     "pred_ctr_below_share", "creative_above_share", "landing_page_above_share"]
    kpd_rows, kpd_acct = class_rows(kpd_agg, qs_cols=True)
    write_csv(os.path.join(a.out_dir, "ws1_class_monthly_kpd.csv"), qs_hdr, kpd_rows)

    # bracket exhibit: kpd vs sqp impressions (suppressed share) ------------------
    # IMPORTANT READ: only class='ALL' rows measure suppression cleanly. Per-class
    # rows mix suppression with the INSTRUMENT DIFFERENCE between query-class (what
    # the user typed) and keyword-class (what keyword caught it) -- e.g. the query
    # "farmers insurance" (flagship) matching keyword "farmers home insurance"
    # (home-intent). That cross-capture is itself informative, but it is not
    # suppression; don't quote per-class rows as suppression shares.
    gap_rows = []
    keys = sorted(set(list(sqp_agg.keys()) + list(kpd_agg.keys())),
                  key=lambda k: (k[0], str(k[1]), k[2]))
    camp_tot = defaultdict(lambda: [0.0, 0.0])   # (ym, cid) -> [kpd, sqp]
    for (ym, cid, cl) in keys:
        ki_ = kpd_agg.get((ym, cid, cl), {}).get("impr", 0.0)
        si_ = sqp_agg.get((ym, cid, cl), {}).get("impr", 0.0)
        camp_tot[(ym, cid)][0] += ki_; camp_tot[(ym, cid)][1] += si_
        gap_rows.append([ym, cid, names.get(cid, str(cid)), cl, int(ki_), int(si_),
                         rd(1.0 - si_ / ki_, 3) if ki_ else ""])
    for (ym, cid) in sorted(camp_tot, key=lambda k: (k[0], str(k[1]))):
        ki_, si_ = camp_tot[(ym, cid)]
        gap_rows.append([ym, cid, names.get(cid, str(cid)), "ALL (pure suppression)",
                         int(ki_), int(si_), rd(1.0 - si_ / ki_, 3) if ki_ else ""])
    for ym in sorted(set(k[0] for k in keys)):
        tk = ts = 0.0
        for cl in CLASSES:
            ki_ = kpd_acct.get((ym, cl), {}).get("impr", 0.0)
            si_ = sqp_acct.get((ym, cl), {}).get("impr", 0.0)
            tk += ki_; ts += si_
            gap_rows.append([ym, "", "ACCOUNT", cl, int(ki_), int(si_),
                             rd(1.0 - si_ / ki_, 3) if ki_ else ""])
        gap_rows.append([ym, "", "ACCOUNT", "ALL (pure suppression)", int(tk), int(ts),
                         rd(1.0 - ts / tk, 3) if tk else ""])
    hdr.append("gap exhibit note: only class='ALL' rows are pure suppression; per-class rows")
    hdr.append("mix suppression with the query-class vs keyword-class instrument difference")
    hdr.append("(flagship queries matching home-intent keywords is capture, not suppression).")
    write_csv(os.path.join(a.out_dir, "ws1_sqp_kpd_gap.csv"),
              ["month", "campaign_id", "campaign", "class", "kpd_impressions",
               "sqp_impressions", "suppressed_share_of_kpd"], gap_rows)

    # served match type per class over time (architect's exhibit: what match type
    # captured each class as the collapse unfolded). ACCOUNT rollup + campaign rows.
    mts_acct = defaultdict(lambda: defaultdict(float))
    for (ym, cid, cl, mts), x in sqp_mts.items():
        for k, v in x.items():
            mts_acct[(ym, cl, mts)][k] += v
    def mts_row(ym, cid, scope, cl, mts, x, cls_tot):
        im, ck = x["impr"], x["clicks"]
        return [ym, cid, scope, cl, mts, int(im), int(ck),
                rd(ck / im if im else 0.0), rd(x["cost"] / ck if ck else 0.0, 2),
                rd(x["leads"], 1), rd(im / cls_tot, 3) if cls_tot else ""]
    mts_rows = []
    for (ym, cl, mts) in sorted(mts_acct):
        tot = sum(v["impr"] for (y2, c2, m2), v in mts_acct.items() if y2 == ym and c2 == cl)
        mts_rows.append(mts_row(ym, "", "ACCOUNT", cl, mts, mts_acct[(ym, cl, mts)], tot))
    for (ym, cid, cl, mts) in sorted(sqp_mts, key=lambda k: (k[0], str(k[1]), k[2], k[3])):
        tot = sum(v["impr"] for (y2, c2, l2, m2), v in sqp_mts.items()
                  if y2 == ym and c2 == cid and l2 == cl)
        mts_rows.append(mts_row(ym, cid, names.get(cid, str(cid)), cl, mts,
                                sqp_mts[(ym, cid, cl, mts)], tot))
    write_csv(os.path.join(a.out_dir, "ws1_class_matchtype_served_sqp.csv"),
              ["month", "campaign_id", "campaign", "class", "match_type_served",
               "impressions", "clicks", "ctr", "cpc", "leads",
               "share_of_class_impr"], mts_rows)

    # eligible impressions proxy --------------------------------------------------
    el_rows = []
    for (ym, cid) in sorted(elig, key=lambda k: (k[0], str(k[1]))):
        x = elig[(ym, cid)]
        cov = x["impr_covered"] / x["impr"] if x["impr"] else 0.0
        iw_is = x["impr_covered"] / x["eligible"] if x["eligible"] else 0.0
        el_rows.append([ym, cid, names.get(cid, str(cid)), int(x["impr"]),
                        rd(cov, 3), rd(iw_is, 4), int(x["eligible"])])
    write_csv(os.path.join(a.out_dir, "ws1_eligible_impressions.csv"),
              ["month", "campaign_id", "campaign", "impressions", "is_coverage_share",
               "impr_weighted_IS", "eligible_impressions_PROXY"], el_rows)

    # farmers-home framing exhibit, double-read -----------------------------------
    fh_rows = []
    fh_months = sorted(set([k[0] for k in farm_sqp] + [k[0] for k in farm_kpd]))
    for ym in fh_months:
        cids = set([k[1] for k in farm_sqp if k[0] == ym] + [k[1] for k in farm_kpd if k[0] == ym])
        for cid in sorted(cids, key=str):
            fh_rows.append([ym, cid, names.get(cid, str(cid)),
                            int(farm_kpd.get((ym, cid), 0)), int(farm_sqp.get((ym, cid), 0))])
        fh_rows.append([ym, "", "ACCOUNT",
                        int(sum(v for k, v in farm_kpd.items() if k[0] == ym)),
                        int(sum(v for k, v in farm_sqp.items() if k[0] == ym))])
    write_csv(os.path.join(a.out_dir, "ws1_farmers_home_exhibit.csv"),
              ["month", "campaign_id", "campaign", "kpd_impressions_farmers_home",
               "sqp_impressions_farmers_home"], fh_rows)

    # =========================== WS2: CTR decomposition ==========================
    # fixed-mix counterfactual, kpd (complete source): hold class impression weights
    # at the anchor month, recompute blended CTR; gap = mix, remainder = within-class.
    def decomp_rows(agg, acct):
        rows = []
        scopes = sorted(set((k[1],) for k in agg), key=lambda t: str(t[0]))
        scope_iter = [("", "ACCOUNT", acct)] + [
            (cid, names.get(cid, str(cid)),
             {(ym, cl): agg[(ym, cid2, cl)] for (ym, cid2, cl) in agg if cid2 == cid})
            for (cid,) in scopes]
        for scope_id, scope, series in scope_iter:
            months = sorted(set(k[0] for k in series))
            if a.anchor not in months:
                continue
            aw = {cl: series.get((a.anchor, cl), {}).get("impr", 0.0) for cl in CLASSES}
            atot = sum(aw.values())
            if not atot:
                continue
            aw = {cl: w / atot for cl, w in aw.items()}
            base_ctr = (sum(series.get((a.anchor, cl), {}).get("clicks", 0.0) for cl in CLASSES) /
                        atot)
            for ym in months:
                im = sum(series.get((ym, cl), {}).get("impr", 0.0) for cl in CLASSES)
                ck = sum(series.get((ym, cl), {}).get("clicks", 0.0) for cl in CLASSES)
                if not im:
                    continue
                actual = ck / im
                # renormalize anchor weights over classes present this month
                present = [cl for cl in CLASSES if series.get((ym, cl), {}).get("impr", 0.0) > 0]
                wsum = sum(aw[cl] for cl in present) or 1.0
                fixed = sum(aw[cl] / wsum *
                            (series[(ym, cl)]["clicks"] / series[(ym, cl)]["impr"])
                            for cl in present)
                total = actual - base_ctr
                mix = actual - fixed
                within = fixed - base_ctr
                rows.append([ym, scope_id, scope, rd(actual), rd(fixed), rd(base_ctr),
                             rd(total), rd(mix), rd(within),
                             rd(mix / total, 3) if abs(total) > 1e-9 else "",
                             rd(within / total, 3) if abs(total) > 1e-9 else ""])
        return rows
    write_csv(os.path.join(a.out_dir, "ws2_ctr_decomposition.csv"),
              ["month", "campaign_id", "campaign", "blended_ctr_actual",
               "blended_ctr_fixedmix", "anchor_blended_ctr", "total_delta_vs_anchor",
               "mix_component", "withinclass_component", "mix_share_of_delta",
               "withinclass_share_of_delta"],
              decomp_rows(kpd_agg, kpd_acct))

    # QS monthly by class (ACCOUNT + campaign) -- already in kpd CSV; standalone view
    qs_rows = [r for r in kpd_rows]  # same columns; kept as its own file for the deck
    write_csv(os.path.join(a.out_dir, "ws2_qs_monthly_class.csv"), qs_hdr, qs_rows)

    # CPC-QS read with match_type held as a dimension ------------------------------
    mt_rows = []
    for (ym, cl, mt) in sorted(qs_mt, key=lambda k: (k[0], k[1], k[2])):
        x = qs_mt[(ym, cl, mt)]
        im, ck, ri = x["impr"], x["clicks"], x["ri"]
        mt_rows.append([ym, cl, mt, int(im), int(ri), rd(ri / im if im else 0.0, 3),
                        rd(x["qsw"] / ri, 2) if ri else "",
                        rd(x["cost"] / ck if ck else 0.0, 2), rd(ck / im if im else 0.0)])
    write_csv(os.path.join(a.out_dir, "ws2_qs_cpc_matchtype.csv"),
              ["month", "class", "match_type", "impressions", "rated_impressions",
               "rated_share", "iw_qs", "cpc", "ctr"], mt_rows)

    if not any(x.get("rev") for x in kpd_agg.values()):
        hdr.append("NOTE: kpd primary_conv_value sums to zero across the window -- keyword-grain")
        hdr.append("value appears unpopulated; irrelevant here (charter: volume and cost only).")

    # classifier provenance --------------------------------------------------------
    hdr.append("classifier provenance: %d queries classed (%d brand-by-table, %d brand-by-token-"
               "fallback), %d keywords classed" % (len(qclass), clf.stats["brand_by_table"],
                                                   clf.stats["brand_by_token_fallback"], len(kclass)))

    # =========================== reconciliation gate ==============================
    recon_ok = reconcile(kpd_agg, sqp_agg, names, a.recon_tol, hdr)
    if a.smoke:
        print("smoke mode: reconciliation shown for the in-window anchors only; "
              "full-run gate re-evaluates all strict anchors.")

    # =========================== timing verdict ===================================
    months = sorted(set(k[0] for k in kpd_acct))
    tt_rows = []
    def acct_series(cl):
        return {ym: kpd_acct.get((ym, cl), defaultdict(float)) for ym in months}
    H, F = acct_series(HOME), acct_series(FLAG)
    tx_h = {ym: kpd_agg.get((ym, TX_ID, HOME), defaultdict(float)) for ym in months}
    for ym in months:
        h, f, t = H[ym], F[ym], tx_h[ym]
        tot_i = sum(kpd_acct.get((ym, c), defaultdict(float))["impr"] for c in CLASSES)
        row = [ym,
               int(h["impr"]), int(sqp_acct.get((ym, HOME), defaultdict(float))["impr"]),
               rd(h["clicks"] / h["impr"]) if h["impr"] else "",
               rd(h["cost"] / h["clicks"], 2) if h["clicks"] else "",
               rd(h["qsw"] / h["ri"], 2) if h["ri"] else "",
               rd(h["ri"] / h["impr"], 3) if h["impr"] else "",
               rd(f["impr"] / tot_i, 3) if tot_i else "",
               int(t["impr"]), rd(t["clicks"] / t["impr"]) if t["impr"] else ""]
        tt_rows.append(row)
    write_csv(os.path.join(a.out_dir, "ws2_timing_table.csv"),
              ["month", "acct_HI_impr_kpd", "acct_HI_impr_sqp", "acct_HI_ctr",
               "acct_HI_cpc", "acct_HI_iw_qs", "acct_HI_rated_share",
               "flagship_share_of_impr", "TX_HI_impr", "TX_HI_ctr"], tt_rows)

    verdict = ["", "=" * 78, "TIMING VERDICT (data patterns; adjudication is yours)", "=" * 78]
    # collapse month = largest MoM drop in account home-intent kpd impressions
    hi_impr = {ym: H[ym]["impr"] for ym in months if H[ym]["impr"] > 0}
    # the trailing month is almost always partial (MTD): including it makes the
    # detector crown it 'collapse month' on mechanics alone (found 8/24). Detect
    # over full months only; report the exclusion.
    if len(hi_impr) > 2:
        last = sorted(hi_impr)[-1]
        hi_impr = {k: v for k, v in hi_impr.items() if k != last}
        print("timing: excluding trailing month %s from collapse detection (partial/MTD)" % last)
    collapse = None
    worst = 0.0
    ms = sorted(hi_impr)
    for i in range(1, len(ms)):
        prev, cur = hi_impr[ms[i - 1]], hi_impr[ms[i]]
        if prev > 0:
            drop = (cur - prev) / prev
            if drop < worst:
                worst, collapse = drop, ms[i]
    verdict.append("collapse month (max MoM drop, acct home-intent kpd impressions): %s (%.0f%%)"
                   % (collapse, worst * 100))

    def first_deterioration(vals, higher_is_better=True, eps=0.01, step=0.08):
        """onset = first month of sustained deterioration: either 2+ consecutive
        adverse MoM moves (each > eps relative, filters rounding noise) or a single
        adverse step > step relative (catches level shifts that then hold)."""
        mm = sorted(vals)
        def rel(m1, m2):
            base = vals[m1]
            return (vals[m2] - vals[m1]) / abs(base) if base else 0.0
        sign = -1.0 if higher_is_better else 1.0
        for i in range(1, len(mm)):
            r1 = rel(mm[i - 1], mm[i]) * sign  # >0 means adverse
            if r1 > step:
                return mm[i]
            if r1 > eps and i + 1 < len(mm) and rel(mm[i], mm[i + 1]) * sign > eps:
                return mm[i]
        return None

    checks = [
        ("home-intent iw_QS", {ym: (H[ym]["qsw"] / H[ym]["ri"]) for ym in months if H[ym]["ri"]}, True),
        ("home-intent CTR (within-class)", {ym: (H[ym]["clicks"] / H[ym]["impr"]) for ym in months if H[ym]["impr"]}, True),
        ("home-intent CPC", {ym: (H[ym]["cost"] / H[ym]["clicks"]) for ym in months if H[ym]["clicks"]}, False),
        ("home-intent rated_share (composition guard)", {ym: (H[ym]["ri"] / H[ym]["impr"]) for ym in months if H[ym]["impr"]}, True),
    ]
    for label, vals, hib in checks:
        onset = first_deterioration(vals, hib)
        rel = "n/a"
        if onset and collapse:
            rel = "LEADS April collapse" if onset < collapse else (
                "COINCIDES with collapse month" if onset == collapse else "LAGS the collapse")
        verdict.append("  %-46s sustained deterioration from: %s  -> %s" % (label, onset, rel))
    verdict += [
        "read: LEADS => internal Ad-Rank spiral mechanism implicated;",
        "      COINCIDES => triggered by the March target batches;",
        "      LAGS => external April event; CTR erosion is downstream composition.",
        "GUARD: any iw_QS move accompanied by a rated_share move in the same window is",
        "       composition until proven otherwise (rated coverage ~130K->~9K kw-days/mo).",
        "",
        "If the eligible-impressions read lands on SHARE LOSS (eligible flat, ours fell):",
        "  competitor identity requires Auction Insights UI pulls -- pull per affected",
        "  campaign (start TX + T2), segment: month, range %s -> %s; compare impression"
        % (a.anchor, months[-1] if months else "?"),
        "  share / overlap rate / outranking share for the top movers Mar vs Apr vs Aug.",
        "=" * 78]
    for line in verdict:
        print(line)
    hdr += verdict

    # ================================ charts ======================================
    if not a.no_charts and (recon_ok or a.force_recon):
        try:
            draw_charts(a, names, kpd_agg, kpd_acct, sqp_acct, elig, ev_rows,
                        farm_kpd, farm_sqp, months)
        except Exception as e:
            print("chart layer failed (CSVs unaffected): %r" % e)
    elif not a.no_charts:
        print("charts SKIPPED: reconciliation gate failed (--force-recon to override)")

    with open(os.path.join(a.out_dir, "run_header.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(str(x) for x in hdr) + "\n")
    print("wrote %s" % os.path.join(a.out_dir, "run_header.txt"))
    conn.close()


# ------------------------------------------------------------------ chart layer
def draw_charts(a, names, kpd_agg, kpd_acct, sqp_acct, elig, ev_rows,
                farm_kpd, farm_sqp, months):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    cdir = os.path.join(a.out_dir, "charts")
    os.makedirs(cdir, exist_ok=True)
    ev_by_month = defaultdict(list)
    for dt, cid, nm, nv in ev_rows:
        ev_by_month[str(dt)[:7]].append((nm, nv))

    def vlines(ax, mlist):
        for i, ym in enumerate(mlist):
            if ym in ev_by_month:
                ax.axvline(i, color="0.6", ls="--", lw=0.8)
                ax.text(i, ax.get_ylim()[1], "tROAS x%d" % len(ev_by_month[ym]),
                        rotation=90, va="top", fontsize=7, color="0.35")

    # 1) account: home-intent impressions, kpd vs sqp bracket + eligible proxy
    fig, ax = plt.subplots(figsize=(10, 5))
    hk = [kpd_acct.get((ym, HOME), {}).get("impr", 0) for ym in months]
    hs = [sqp_acct.get((ym, HOME), {}).get("impr", 0) for ym in months]
    ax.plot(months, hk, marker="o", label="home-intent impr (kpd, complete)")
    ax.plot(months, hs, marker="s", label="home-intent impr (sqp, suppression floor)")
    el_acct = [sum(v["eligible"] for k, v in elig.items() if k[0] == ym) for ym in months]
    ax2 = ax.twinx()
    ax2.plot(months, el_acct, color="tab:red", alpha=0.6, marker="^",
             label="eligible impr PROXY (all classes, campaign IS)")
    ax2.set_ylabel("eligible impressions (proxy)")
    ax.set_title("ACCOUNT: home-intent exposure, source bracket + eligible demand")
    ax.set_ylabel("impressions")
    vlines(ax, months)
    h1, l1 = ax.get_legend_handles_labels(); h2, l2 = ax2.get_legend_handles_labels()
    ax.legend(h1 + h2, l1 + l2, fontsize=8)
    fig.tight_layout(); fig.savefig(os.path.join(cdir, "acct_home_intent_bracket.png"), dpi=150)
    plt.close(fig)

    # 2) per core campaign: class impressions + eligible
    for cid, nm in CORE.items():
        mm = [ym for ym in months]
        if not any(kpd_agg.get((ym, cid, cl), {}).get("impr", 0) for ym in mm for cl in CLASSES):
            continue
        fig, ax = plt.subplots(figsize=(10, 5))
        for cl, mk in ((HOME, "o"), (FLAG, "s"), (GEN, "^")):
            ax.plot(mm, [kpd_agg.get((ym, cid, cl), {}).get("impr", 0) for ym in mm],
                    marker=mk, label=cl)
        ax2 = ax.twinx()
        ax2.plot(mm, [elig.get((ym, cid), {}).get("eligible", 0) for ym in mm],
                 color="tab:red", alpha=0.6, ls=":", label="eligible PROXY")
        ax.set_title("%s: impressions by class (kpd) vs eligible demand" % nm)
        vlines(ax, mm)
        h1, l1 = ax.get_legend_handles_labels(); h2, l2 = ax2.get_legend_handles_labels()
        ax.legend(h1 + h2, l1 + l2, fontsize=8)
        fig.tight_layout(); fig.savefig(os.path.join(cdir, "campaign_%s_class_vs_eligible.png" % nm), dpi=150)
        plt.close(fig)

    # 3) farmers-home exhibit
    fig, ax = plt.subplots(figsize=(10, 4.5))
    fk = [sum(v for k, v in farm_kpd.items() if k[0] == ym) for ym in months]
    fs = [sum(v for k, v in farm_sqp.items() if k[0] == ym) for ym in months]
    ax.plot(months, fk, marker="o", label="kpd (complete)")
    ax.plot(months, fs, marker="s", label="sqp (suppression floor)")
    ax.set_title('"farmers" + product-term impressions -- the framing exhibit, double-read')
    vlines(ax, months); ax.legend(fontsize=8)
    fig.tight_layout(); fig.savefig(os.path.join(cdir, "farmers_home_exhibit.png"), dpi=150)
    plt.close(fig)

    # 4) account iw_QS + rated_share by class (guard drawn on the same axes)
    fig, ax = plt.subplots(figsize=(10, 5))
    ax2 = ax.twinx()
    for cl, mk in ((HOME, "o"), (FLAG, "s")):
        qs = [(kpd_acct.get((ym, cl), {}).get("qsw", 0) / kpd_acct.get((ym, cl), {}).get("ri", 1))
              if kpd_acct.get((ym, cl), {}).get("ri", 0) else None for ym in months]
        rs = [(kpd_acct.get((ym, cl), {}).get("ri", 0) / kpd_acct.get((ym, cl), {}).get("impr", 1))
              if kpd_acct.get((ym, cl), {}).get("impr", 0) else None for ym in months]
        ax.plot(months, qs, marker=mk, label="iw_QS " + cl)
        ax2.plot(months, rs, marker=mk, ls=":", alpha=0.5, label="rated_share " + cl)
    ax.set_ylabel("iw_QS"); ax2.set_ylabel("rated_share (composition guard)")
    ax.set_title("ACCOUNT: iw_QS with rated_share guard, by class")
    vlines(ax, months)
    h1, l1 = ax.get_legend_handles_labels(); h2, l2 = ax2.get_legend_handles_labels()
    ax.legend(h1 + h2, l1 + l2, fontsize=8)
    fig.tight_layout(); fig.savefig(os.path.join(cdir, "acct_iwqs_rated_share.png"), dpi=150)
    plt.close(fig)
    print("charts written to %s" % cdir)


if __name__ == "__main__":
    main()
