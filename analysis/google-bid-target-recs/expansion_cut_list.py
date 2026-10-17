#!/usr/bin/env python
"""
expansion_cut_list.py — Expansion-terms drill-down: what did Google buy
at the margin during a tROAS expansion window, ranked as cut candidates.

Compares a breach window (default 2026-08-16 -> 2026-08-20) against a
trailing same-day-class baseline (default 14 same-class days per class,
ending the day before the window — the carrier_tripwire convention).
Weekday days compare to a weekday baseline, weekend days to a weekend
baseline; expected spend is composed from the window's actual day-class
mix, so weekday/weekend mixing never manufactures a delta.

TWO GRAINS (pinned sources; column names verified against
carrier_tripwire.py v4 and carrier_query_analysis.py):
  1. Search-query grain (negatives): query x campaign from
     search_query_performance p JOIN search_queries sq ON sq.id=p.query_id
     JOIN campaigns c ON c.campaign_id=p.campaign_id AND c.account_id=1
     WHERE p.platform='google'. Columns: report_date, cost_micros,
     impressions, clicks, secondary_conv_count (zips),
     primary_conv_count (leads), primary_conv_value (revenue).
     Device handling auto-detected (carrier_query_analysis pattern):
     filter device='ALL' when a rollup exists, else no filter.
     sq.first_seen_date flags never-before-seen queries (WoW novelty
     only — the 2024 backfill reset the floor).
  2. Keyword grain (pauses): keyword x campaign from
     keyword_performance_daily p JOIN keyword_instances ki
     ON ki.id=p.instance_id AND ki.account_id=1, device='ALL'
     (kpd has NO account_id — scope through the join). Campaign linkage
     = the tripwire's kw_campaign_column fallback: ki.campaign_id if
     present, else ad_groups ag ON ag.id = ki.ad_group_id.

CLASSIFICATION (reconciles with daily monitoring + negatives workflow):
  - Carrier segment: the tripwire's CARRIER_PATTERNS (9 named carriers,
    first-hit top-down; unmatched = Other/generic).
  - Farmers split: FLAGSHIP vs HOME_INTENT (home/homeowner/house token).
    HOME_INTENT is PROTECTED — never recommended for negative or pause;
    it is the inventory we've been losing exposure on.
  - Intent: carrier_query_analysis's taxonomy, verbatim regexes —
    SERVICE, OTHER_PRODUCT, AGENT, PURCHASE, BRAND_BARE relative to the
    row's own carrier; OTHER_CARRIER via the brand registry export when
    a non-tripwire brand appears; GENERIC otherwise.
  - Junk families from the AI Max term reviews: NAV_LEAK extras beyond
    SERVICE (eagent, www, .com) and CONQUEST_JUNK (zander, kentucky
    farm bureau, essent, frickey, farmers mutual). Flagged, but the
    ACTION is decided per-campaign on that campaign's own economics
    ("zander insurance" earns in TX and loses in T2).

ACTIONS:
  Query grain -> negative. Cut when window ROAS < 1.00 (or zero
  revenue) on >= --cost-floor window spend in that campaign, EXCEPT
  PURCHASE intent (never auto-negated — bid/copy lever; house rule) and
  protected Farmers home-intent. suggested_scope =
  campaign_or_shared_list when the same text is a cut in >= 3
  campaigns, else ad_group (carrier_query_analysis convention).
  Keyword grain -> pause. Same economics; PURCHASE does NOT protect a
  keyword from a pause recommendation (pausing is the bid-side lever —
  TX Farmers-broad precedent), but the purchase flag is carried so you
  can adjudicate. Protected home-intent excluded at both grains.
  Ranked by breach-window spend at ROAS < 1.00; break-even = 1.00;
  revenue = platform conversion value as stored; NO conversion-lag
  exclusion (house ruling 8/20; matches tripwire --lag-days 0 default).
  Earning rows (ROAS >= campaign target) are the separate do-NOT-cut
  list; second_look=FAMILY_EARNING marks rows whose whole carrier
  family expanded profitably in-window (reconcile with the tripwire's
  earning segments before executing).

Per-campaign tROAS targets: latest change_type='TARGET_ROAS' in
campaign_change_events on/before window end (value/100 when > 10);
--default-target covers campaigns with no event.

DATA RULES: DB read-only (mode=ro); never during a sync. Mobile is a
cross-cutting FLAG (mobile_share, mobile_concentrated), not a cuttable
unit. Privacy suppression: sqp undercounts sub-threshold terms — the
summary reports sqp-vs-kpd spend coverage; keyword grain backstops.

USAGE:
  python expansion_cut_list.py
  python expansion_cut_list.py --window-start 2026-08-16 --window-end 2026-08-20
  python expansion_cut_list.py --baseline-days 14 --cost-floor 50
  python expansion_cut_list.py --default-target 1.0 --mobile-threshold 0.6
"""

import argparse
import csv
import os
import re
import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

DEFAULT_DB_PATH = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")
DEFAULT_OUT_DIR = Path(r"C:\Projects\google-ads-ops\analysis"
                       r"\profit-attribution-tracker\output\expansion-cuts")
DEFAULT_BRAND_CSV = r"C:\Projects\google-ads-ops\brand_registry_export.csv"
ACCOUNT_ID = 1
BREAK_EVEN = 1.00

# ── carrier segments: verbatim from carrier_tripwire.py ──────────────────
# order = display preference for ties; matching is first-hit top-down
CARRIER_PATTERNS = [
    ("Geico",        ["geico"]),
    ("Farmers",      ["farmers"]),
    ("State Farm",   ["state farm", "statefarm"]),
    ("Travelers",    ["travelers"]),
    ("Kin",          ["kin insurance", " kin ", "kin home"]),
    ("AAA",          ["aaa"]),
    ("Amica",        ["amica"]),
    ("Hartford",     ["hartford"]),
    ("Costco",       ["costco"]),
]
CARRIER_TOKEN = {           # first pattern = the carrier's core token
    label: pats[0] for label, pats in CARRIER_PATTERNS}
OTHER_LABEL = "Other/generic"

# ── column candidates (kpd order = tripwire's; sqp = verified cols) ──────
REV_CANDIDATES = ["conversion_value", "conversions_value", "conv_value",
                  "primary_conv_value", "total_conv_value",
                  "all_conv_value"]
CONV_CANDIDATES = ["conversions", "primary_conv_count", "conv_count",
                   "all_conversions"]
SQP_REV_CANDIDATES = ["primary_conv_value"] + REV_CANDIDATES
SQP_CONV_CANDIDATES = ["primary_conv_count"] + CONV_CANDIDATES
SQP_DATE_CANDIDATES = ["report_date", "date"]
SQP_TEXT_CANDIDATES = ["query_text", "search_term", "query", "term",
                       "search_query"]

# ── intent taxonomy: verbatim regexes from carrier_query_analysis.py ─────
SERVICE = re.compile(
    r"\b(login|log in|sign in|claim|claims|phone|number|customer service"
    r"|pay|payment|bill|billing|cancel|cancellation|roadside|id card|app"
    r"|careers|jobs|address)\b")
OTHER_PRODUCT = re.compile(
    r"\b(auto|car|vehicle|life|renters|rental|pet|business|commercial"
    r"|flood|umbrella|motorcycle|boat|rv)\b")
AGENT = re.compile(
    r"\b(agent|agents|agency|agencies|broker|near me|office|local)\b")
PURCHASE = re.compile(
    r"\b(quote|quotes|rate|rates|cost|price|pricing|cheap|compare"
    r"|comparison|discount|estimate|coverage|policy|insurance)\b")

# ── junk families (AI Max term reviews / TX residual waste) ──────────────
NAV_EXTRA_PATS = ["eagent", "e agent", "www", ".com", " com "]
CONQUEST_JUNK_PATS = ["zander", "kentucky farm bureau", "ky farm bureau",
                      "essent", "frickey", "farmers mutual"]
HOME_INTENT_PATS = ["home", "homeowner", "house"]

MATCH_TYPE_MAP = {2: "EXACT", 3: "PHRASE", 4: "BROAD",
                  "2": "EXACT", "3": "PHRASE", "4": "BROAD"}


# ── shared helpers (tripwire conventions) ────────────────────────────────
def connect_ro(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def table_cols(conn, table):
    try:
        return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()


def pick_col(conn, table, candidates):
    cols = table_cols(conn, table)
    return next((c for c in candidates if c in cols), None)


def classify(kw: str) -> str:
    k = f" {(kw or '').lower().strip()} "
    for label, pats in CARRIER_PATTERNS:
        for p in pats:
            if p in k:
                return label
    return OTHER_LABEL


def farmers_split(text: str, seg: str) -> str:
    if seg != "Farmers":
        return ""
    t = f" {(text or '').lower()} "
    return ("HOME_INTENT" if any(p in t for p in HOME_INTENT_PATS)
            else "FLAGSHIP")


def load_brands(brand_csv):
    """carrier_query_analysis.load_brands, minus the term exclusion —
    tripwire carriers are excluded at intent() time instead."""
    toks = set()
    if brand_csv and os.path.exists(brand_csv):
        try:
            for r in csv.DictReader(open(brand_csv, encoding="utf-8-sig")):
                v = re.sub(r"[^a-z0-9 ]", "",
                           (r.get("normalized_name")
                            or r.get("brand_name") or "").lower()).strip()
                if len(v) >= 4:
                    toks.add(v)
        except Exception as e:
            print(f"  WARN: brand registry unreadable ({e}) — "
                  "OTHER_CARRIER flag limited to hardcoded junk families")
    return toks


def intent(q: str, seg: str, other_brands) -> str:
    """carrier_query_analysis.intent() adapted to multi-carrier scope:
    the row's own carrier segment plays the role of --term."""
    q = f" {(q or '').lower()} "
    if seg == OTHER_LABEL:
        for b in other_brands:
            if b and b in q and classify(b) == OTHER_LABEL:
                return "OTHER_CARRIER"
        if SERVICE.search(q):
            return "SERVICE"
        if OTHER_PRODUCT.search(q):
            return "OTHER_PRODUCT"
        if AGENT.search(q):
            return "AGENT"
        if PURCHASE.search(q):
            return "PURCHASE"
        return "GENERIC"
    if SERVICE.search(q):
        return "SERVICE"
    if OTHER_PRODUCT.search(q):
        return "OTHER_PRODUCT"
    if AGENT.search(q):
        return "AGENT"
    if PURCHASE.search(q):
        return "PURCHASE"
    return "BRAND_BARE"


def junk_family(text: str) -> str:
    t = f" {(text or '').lower()} "
    if any(p in t for p in CONQUEST_JUNK_PATS):
        return "CONQUEST_JUNK"
    if any(p in t for p in NAV_EXTRA_PATS) or SERVICE.search(t):
        return "NAV_LEAK"
    return ""


def is_weekend(d: date) -> bool:
    return d.weekday() >= 5


def short_campaign(name):
    n = (name or "").replace("HOME INSURANCE", "").replace(
        "_STATE-GROUP_", "").replace("-NB-SEARCH-ALL-USA-STE", "").replace(
        "_COMBINED_TROAS_SEARCH_ALLDEVICE_USA-STE", "")
    return n.strip("-_ ") or name


def normalize_target(raw):
    s = str(raw or "").strip().rstrip("%")
    try:
        v = float(s)
    except ValueError:
        return None
    return v / 100.0 if v > 10 else v


def daterange(d0: date, d1: date):
    d = d0
    while d <= d1:
        yield d
        d += timedelta(days=1)


def baseline_dates(baseline_end: date, n_per_class: int):
    """Trailing n_per_class same-day-class days per class, ending
    baseline_end (carrier_tripwire baseline_for convention)."""
    out = {True: [], False: []}          # is_weekend -> [dates]
    d = baseline_end
    while len(out[True]) < n_per_class or len(out[False]) < n_per_class:
        cls = is_weekend(d)
        if len(out[cls]) < n_per_class:
            out[cls].append(d)
        d -= timedelta(days=1)
        if (baseline_end - d).days > 200:          # safety
            break
    return out


def fmt_money(v):
    return f"${v:,.0f}"


def roas_val(spend, rev):
    return (rev / spend) if spend and spend > 0 else None


# ── schema resolution ────────────────────────────────────────────────────
def resolve_schema(conn):
    s = {}
    # keyword grain
    s["kpd_rev"] = pick_col(conn, "keyword_performance_daily",
                            REV_CANDIDATES)
    s["kpd_conv"] = pick_col(conn, "keyword_performance_daily",
                             CONV_CANDIDATES)
    s["kpd_clicks"] = ("clicks" if "clicks" in
                       table_cols(conn, "keyword_performance_daily")
                       else None)
    ki_cols = table_cols(conn, "keyword_instances")
    s["ki_match"] = "match_type" if "match_type" in ki_cols else None
    # campaign linkage: the tripwire's kw_campaign_column fallback
    ag_cols = table_cols(conn, "ad_groups")
    if "campaign_id" in ki_cols:
        s["ki_campaign_via"] = "direct"
        s["ag_key"] = None
    elif "ad_group_id" in ki_cols and "campaign_id" in ag_cols:
        s["ki_campaign_via"] = "ad_group"
        s["ag_key"] = next((c for c in ("id", "ad_group_id")
                            if c in ag_cols), None)
    else:
        s["ki_campaign_via"] = None
        s["ag_key"] = None
    # query grain (columns verified against carrier_query_analysis.py)
    sqp_cols = table_cols(conn, "search_query_performance")
    s["sqp_date"] = next((c for c in SQP_DATE_CANDIDATES
                          if c in sqp_cols), None)
    s["sqp_rev"] = pick_col(conn, "search_query_performance",
                            SQP_REV_CANDIDATES)
    s["sqp_conv"] = pick_col(conn, "search_query_performance",
                             SQP_CONV_CANDIDATES)
    s["sqp_zips"] = ("secondary_conv_count" if "secondary_conv_count"
                     in sqp_cols else None)
    s["sqp_clicks"] = "clicks" if "clicks" in sqp_cols else None
    if "campaign_id" in sqp_cols:
        s["sqp_campaign_via"] = "direct"
    elif ("ad_group_id" in sqp_cols and "campaign_id" in ag_cols):
        s["sqp_campaign_via"] = "ad_group"
        s.setdefault("ag_key", next((c for c in ("id", "ad_group_id")
                                     if c in ag_cols), None))
    else:
        s["sqp_campaign_via"] = None
    s["sq_text"] = pick_col(conn, "search_queries", SQP_TEXT_CANDIDATES)
    s["sq_first_seen"] = ("first_seen_date" if "first_seen_date" in
                          table_cols(conn, "search_queries") else None)
    # sqp device handling: carrier_query_analysis.device_clause pattern
    try:
        devs = [r[0] for r in conn.execute(
            "SELECT DISTINCT device FROM search_query_performance "
            "LIMIT 10")]
    except sqlite3.Error:
        devs = []
    s["sqp_devices"] = devs
    s["sqp_filter_all"] = bool(devs and "ALL" in devs and len(devs) > 1)
    return s


def print_schema(s):
    print("  schema: kpd rev=%s conv=%s | ki->campaign via %s(ag.%s) | "
          "sqp date=%s rev=%s conv=%s zips=%s campaign_via=%s | "
          "sq text=%s first_seen=%s"
          % (s["kpd_rev"], s["kpd_conv"], s["ki_campaign_via"],
             s["ag_key"], s["sqp_date"], s["sqp_rev"], s["sqp_conv"],
             s["sqp_zips"], s["sqp_campaign_via"], s["sq_text"],
             s["sq_first_seen"]))
    print("  sqp device values %s -> %s" %
          (s["sqp_devices"],
           "filtering device='ALL' (authoritative rows)"
           if s["sqp_filter_all"] else "no ALL rollup; no device filter"))


def campaign_names(conn):
    names = {}
    try:
        for r in conn.execute(
                "SELECT campaign_id, campaign_name FROM campaigns "
                "WHERE account_id = ?", [ACCOUNT_ID]):
            names[str(r["campaign_id"])] = short_campaign(
                r["campaign_name"])
    except sqlite3.Error:
        pass
    return names


def campaign_targets(conn, asof: date, default_target: float):
    """Latest change_type='TARGET_ROAS' per campaign on/before asof
    (carrier_tripwire fetch pattern). Missing -> default_target."""
    targets = {}
    cols = table_cols(conn, "campaign_change_events")
    if not cols:
        return targets
    type_col = next((c for c in ("change_type", "field", "field_name",
                                 "event_type", "change_field",
                                 "attribute") if c in cols), None)
    val_col = next((c for c in ("new_value", "value") if c in cols), None)
    date_col = next((c for c in ("change_date", "date", "event_date")
                     if c in cols), None)
    if not (type_col and val_col and date_col):
        return targets
    where = [f"{date_col} <= ?", f"UPPER({type_col}) LIKE '%TARGET_ROAS%'"]
    params = [asof.isoformat()]
    if "platform" in cols:
        where.append("platform = 'google'")
    if "account_id" in cols:
        where.append("account_id = ?")
        params.append(ACCOUNT_ID)
    sql = (f"SELECT campaign_id, {val_col} AS v, {date_col} AS d "
           f"FROM campaign_change_events WHERE {' AND '.join(where)} "
           f"ORDER BY {date_col}")
    try:
        for r in conn.execute(sql, params):
            t = normalize_target(r["v"])
            if t:
                targets[str(r["campaign_id"])] = t   # later rows win
    except sqlite3.Error:
        pass
    return targets


# ── fetchers ─────────────────────────────────────────────────────────────
def _agg(rows_iter, key_fn):
    """Aggregate [spend, rev, conv, clicks, zips] rows into dict[key]."""
    out = {}
    for r in rows_iter:
        k = key_fn(r)
        cur = out.setdefault(k, [0.0, 0.0, 0.0, 0.0, 0.0])
        cur[0] += r["spend"] or 0.0
        cur[1] += r["rev"] or 0.0
        cur[2] += r["conv"] or 0.0
        cur[3] += r["clicks"] or 0.0
        cur[4] += (r["zips"] if "zips" in r.keys() else 0) or 0.0
    return out


def fetch_keyword_grain(conn, s, dates, device="ALL", by_class=False):
    """keyword x campaign aggregates over an explicit date set.
    Key (kw_text, match, campaign_id)[, is_weekend]."""
    rev_e = f"SUM(p.{s['kpd_rev']})" if s["kpd_rev"] else "NULL"
    conv_e = f"SUM(p.{s['kpd_conv']})" if s["kpd_conv"] else "NULL"
    clk_e = f"SUM(p.{s['kpd_clicks']})" if s["kpd_clicks"] else "NULL"
    match_e = (f"UPPER(ki.{s['ki_match']})" if s["ki_match"] else "NULL")
    if s["ki_campaign_via"] == "direct":
        camp_e, join_ag = "ki.campaign_id", ""
    elif s["ki_campaign_via"] == "ad_group":
        camp_e = "ag.campaign_id"
        join_ag = f"LEFT JOIN ad_groups ag ON ag.{s['ag_key']} = ki.ad_group_id"
    else:
        camp_e, join_ag = "NULL", ""
    ph = ",".join("?" for _ in dates)
    sql = f"""
        SELECT LOWER(ki.keyword_text) AS txt, {match_e} AS mt,
               {camp_e} AS cid, p.report_date AS d,
               SUM(p.cost_micros)/1e6 AS spend, {rev_e} AS rev,
               {conv_e} AS conv, {clk_e} AS clicks, NULL AS zips
        FROM keyword_performance_daily p
        JOIN keyword_instances ki
          ON ki.id = p.instance_id AND ki.account_id = ?
        {join_ag}
        WHERE p.device = ? AND p.report_date IN ({ph})
        GROUP BY 1, 2, 3, 4
    """
    params = [ACCOUNT_ID, device] + [d.isoformat() for d in dates]

    def key(r):
        mt = MATCH_TYPE_MAP.get(r["mt"], str(r["mt"] or ""))
        base = (r["txt"], mt, str(r["cid"] or "?"))
        if by_class:
            dd = datetime.strptime(str(r["d"])[:10], "%Y-%m-%d").date()
            return base + (is_weekend(dd),)
        return base

    return _agg(conn.execute(sql, params), key)


def fetch_query_grain(conn, s, dates, device=None, by_class=False):
    """query x campaign aggregates, ACCOUNT-SCOPED via campaigns join
    (sqp spans other Google verticals). device=None -> auto per schema:
    'ALL' filter when a rollup exists, else unfiltered; device='MOBILE'
    -> mobile rows for the share flag. Returns (agg, first_seen)."""
    if not s["sqp_date"] or not s["sq_text"] or not s["sqp_campaign_via"]:
        return {}, {}
    rev_e = f"SUM(p.{s['sqp_rev']})" if s["sqp_rev"] else "NULL"
    conv_e = f"SUM(p.{s['sqp_conv']})" if s["sqp_conv"] else "NULL"
    clk_e = f"SUM(p.{s['sqp_clicks']})" if s["sqp_clicks"] else "NULL"
    zip_e = f"SUM(p.{s['sqp_zips']})" if s["sqp_zips"] else "NULL"
    fs_e = (f"sq.{s['sq_first_seen']}" if s["sq_first_seen"] else "NULL")
    if s["sqp_campaign_via"] == "direct":
        camp_e, join_link = "p.campaign_id", ""
    else:
        camp_e = "ag.campaign_id"
        join_link = (f"JOIN ad_groups ag ON ag.{s['ag_key']} = "
                     f"p.ad_group_id")
    if device is None:
        dev_clause = (" AND p.device = 'ALL' " if s["sqp_filter_all"]
                      else "")
        dev_params = []
    else:
        dev_clause = " AND p.device = ? "
        dev_params = [device]
    ph = ",".join("?" for _ in dates)
    sql = f"""
        SELECT LOWER(TRIM(sq.{s['sq_text']})) AS txt, {camp_e} AS cid,
               {fs_e} AS first_seen, p.{s['sqp_date']} AS d,
               SUM(p.cost_micros)/1e6 AS spend, {rev_e} AS rev,
               {conv_e} AS conv, {clk_e} AS clicks, {zip_e} AS zips
        FROM search_query_performance p
        JOIN search_queries sq ON sq.id = p.query_id
        {join_link}
        JOIN campaigns c ON c.campaign_id = {camp_e}
          AND c.account_id = ?
        WHERE p.platform = 'google' {dev_clause}
          AND p.{s['sqp_date']} IN ({ph})
        GROUP BY 1, 2, 3, 4
    """
    params = [ACCOUNT_ID] + dev_params + [d.isoformat() for d in dates]
    first_seen = {}
    rows = []
    for r in conn.execute(sql, params):
        rows.append(r)
        if r["first_seen"]:
            first_seen[r["txt"]] = str(r["first_seen"])[:10]

    def key(r):
        base = (r["txt"], "", str(r["cid"] or "?"))
        if by_class:
            dd = datetime.strptime(str(r["d"])[:10], "%Y-%m-%d").date()
            return base + (is_weekend(dd),)
        return base

    return _agg(rows, key), first_seen


# ── row assembly ─────────────────────────────────────────────────────────
def build_rows(window_all, window_mob, base_by_class, first_seen, grain,
               args, win_class_counts, base_class_days, targets,
               camp_names, family_roas, other_brands):
    n_win_days = sum(win_class_counts.values())
    rows = []
    for key, w in window_all.items():
        txt, mt, cid = key
        w_spend, w_rev, w_conv, w_clk, w_zip = w
        if w_spend < args.min_window_spend:
            continue
        exp_spend = base_spend = base_rev = 0.0
        for cls in (False, True):
            b = base_by_class.get(key + (cls,))
            n = base_class_days[cls]
            rate = (b[0] / n) if (b and n) else 0.0
            exp_spend += rate * win_class_counts[cls]
            if b:
                base_spend += b[0]
                base_rev += b[1]
        d_spend_day = (w_spend - exp_spend) / n_win_days
        w_roas = roas_val(w_spend, w_rev)
        b_roas = roas_val(base_spend, base_rev)
        mob = window_mob.get(key)
        mob_share = (mob[0] / w_spend) if (mob and w_spend > 0) else 0.0

        seg = classify(txt)
        fsplit = farmers_split(txt, seg)
        it = intent(txt, seg, other_brands)
        junk = junk_family(txt)
        target = targets.get(cid, args.default_target)

        novelty = ""
        if grain == "query":
            fs = first_seen.get(txt)
            if fs and fs >= args.window_start:
                novelty = "NEW"
        if not novelty:
            base_rate = base_spend / max(sum(base_class_days.values()), 1)
            if (base_rate < 0.10 * (w_spend / n_win_days)
                    and w_spend >= args.cost_floor):
                novelty = "NEWLY_SCALED"

        # ── action ──
        cut_verb = "negative" if grain == "query" else "pause"
        second_look = ""
        fam_r = family_roas.get(seg)
        if fam_r is not None and fam_r >= target:
            second_look = "FAMILY_EARNING"
        losing = (w_roas is not None and w_roas < BREAK_EVEN)
        zero_rev = (w_roas is None and w_conv < 1)
        if w_roas is not None and w_roas >= target:
            action, reason = "earner", (
                f"window ROAS {w_roas:.2f} >= target {target:.2f}")
        elif fsplit == "HOME_INTENT":
            action, reason = "keep-watch", (
                "PROTECTED: Farmers home-intent — never block; "
                "bid/copy/QS levers only")
        elif (losing or zero_rev) and w_spend >= args.cost_floor:
            if grain == "query" and it == "PURCHASE" and not junk:
                action, reason = "keep-watch", (
                    f"ROAS {w_roas:.2f} < 1.00 but PURCHASE intent — "
                    "never auto-negated; bid/copy lever")
            else:
                why = f"{it}, " if it not in ("BRAND_BARE", "GENERIC") \
                    else ""
                r_txt = ("zero revenue" if w_roas is None
                         else f"ROAS {w_roas:.2f} < 1.00")
                action = cut_verb
                reason = (f"{why}{r_txt} on {fmt_money(w_spend)} "
                          "this campaign")
        elif losing or zero_rev:
            action, reason = "keep-watch", (
                f"{'zero revenue' if w_roas is None else f'ROAS {w_roas:.2f} < 1.00'}"
                f" but window spend below ${args.cost_floor:.0f} floor")
        else:
            action, reason = "keep-watch", (
                f"ROAS {w_roas:.2f} between break-even and target "
                f"{target:.2f}")
        if junk and action == "keep-watch" and (w_roas or 0) >= BREAK_EVEN:
            reason += (f" [junk family {junk} but earning HERE — "
                       "per-campaign rule]")

        rows.append({
            "grain": grain,
            "text": txt,
            "match_type": mt,
            "campaign": camp_names.get(cid, cid),
            "campaign_id": cid,
            "segment": seg,
            "farmers_split": fsplit,
            "intent": it,
            "junk_family": junk,
            "novelty": novelty,
            "win_spend": round(w_spend, 2),
            "win_rev": round(w_rev, 2),
            "win_gp": round(w_rev - w_spend, 2),
            "win_leads": round(w_conv, 1),
            "win_zips": round(w_zip, 1),
            "win_clicks": int(w_clk),
            "win_roas": round(w_roas, 3) if w_roas is not None else "",
            "base_spend": round(base_spend, 2),
            "base_gp": round(base_rev - base_spend, 2),
            "base_roas": round(b_roas, 3) if b_roas is not None else "",
            "delta_spend_day": round(d_spend_day, 2),
            "mobile_share": round(mob_share, 2),
            "mobile_concentrated": ("Y" if mob_share >=
                                    args.mobile_threshold else ""),
            "campaign_target": target,
            "second_look": second_look,
            "suggested_scope": "",
            "action": action,
            "reason": reason,
        })
    return rows


def family_window_roas(window_all):
    tot = {}
    for (txt, _mt, _cid), v in window_all.items():
        seg = classify(txt)
        cur = tot.setdefault(seg, [0.0, 0.0])
        cur[0] += v[0]
        cur[1] += v[1]
    return {seg: (rv / sp if sp > 0 else None)
            for seg, (sp, rv) in tot.items()}


def annotate_scope(rows):
    """suggested_scope for query-grain cuts: campaign_or_shared_list
    when the same text is a cut in >= 3 campaigns, else ad_group
    (carrier_query_analysis convention)."""
    counts = {}
    for r in rows:
        if r["grain"] == "query" and r["action"] == "negative":
            counts[r["text"]] = counts.get(r["text"], 0) + 1
    for r in rows:
        if r["grain"] == "query" and r["action"] == "negative":
            n = counts[r["text"]]
            r["suggested_scope"] = ("campaign_or_shared_list"
                                    if n >= 3 else "ad_group")


# ── output ───────────────────────────────────────────────────────────────
FIELDS = ["grain", "text", "match_type", "campaign", "campaign_id",
          "segment", "farmers_split", "intent", "junk_family", "novelty",
          "win_spend", "win_rev", "win_gp", "win_leads", "win_zips",
          "win_clicks", "win_roas", "base_spend", "base_gp", "base_roas",
          "delta_spend_day", "mobile_share", "mobile_concentrated",
          "campaign_target", "second_look", "suggested_scope", "action",
          "reason"]


def write_csv(path, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)


def summarize(cuts, watch, earners, args, coverage, lines_cap=25):
    L = []
    L.append(f"EXPANSION CUT LIST — window {args.window_start} -> "
             f"{args.window_end} vs {args.baseline_days}d same-day-class "
             f"baseline ending {args.baseline_end}")
    L.append(f"break-even = {BREAK_EVEN:.2f}; revenue = platform "
             "conversion value as stored; NO lag exclusion (house 8/20)")
    if coverage is not None:
        L.append(f"query-grain coverage: sqp captures {coverage:.0%} of "
                 "kpd window spend (privacy-suppressed tail is the "
                 "remainder — keyword grain backstops it)")
    L.append("")
    L.append(f"CUT CANDIDATES (ranked by window spend at ROAS < "
             f"{BREAK_EVEN:.2f}) — {len(cuts)} rows")
    L.append(f"  {'action':9} {'term/keyword':34.34} {'campaign':12.12} "
             f"{'win$':>8} {'ROAS':>5} {'GP':>7} {'Δ$/d':>7} {'mob':>4}"
             "  flags")
    for r in cuts[:lines_cap]:
        flags = ",".join(x for x in (
            r["segment"] if r["segment"] != OTHER_LABEL else "generic",
            r["farmers_split"], r["intent"], r["junk_family"],
            r["novelty"], "MOB" if r["mobile_concentrated"] else "",
            r["second_look"], r["suggested_scope"]) if x)
        L.append(f"  {r['action']:9} {r['text']:34.34} "
                 f"{r['campaign']:12.12} {r['win_spend']:>8,.0f} "
                 f"{(r['win_roas'] if r['win_roas'] != '' else 0):>5.2f} "
                 f"{r['win_gp']:>7,.0f} {r['delta_spend_day']:>+7,.0f} "
                 f"{r['mobile_share']:>4.0%}  {flags}")
    if len(cuts) > lines_cap:
        L.append(f"  ... {len(cuts) - lines_cap} more in CSV")
    L.append("")
    L.append(f"DO NOT CUT — earning expansion rows (ROAS >= campaign "
             f"target) — {len(earners)} rows")
    for r in earners[:lines_cap]:
        L.append(f"  {'earner':9} {r['text']:34.34} "
                 f"{r['campaign']:12.12} {r['win_spend']:>8,.0f} "
                 f"{r['win_roas']:>5.2f} {r['win_gp']:>7,.0f} "
                 f"{r['delta_spend_day']:>+7,.0f} "
                 f"{r['mobile_share']:>4.0%}  {r['segment']}")
    if len(earners) > lines_cap:
        L.append(f"  ... {len(earners) - lines_cap} more in CSV")
    L.append("")
    prot = [r for r in watch if "PROTECTED" in r["reason"]]
    L.append(f"KEEP-WATCH: {len(watch)} rows ({len(prot)} protected "
             "Farmers home-intent) — full detail in CSV")
    L.append("")
    L.append("Reminders: recommendations are PER-CAMPAIGN (same term can "
             "earn elsewhere); mobile is a flag, not a cuttable unit; "
             "second_look=FAMILY_EARNING rows reconcile against the "
             "tripwire's earning segments before executing; PURCHASE "
             "protects queries from negatives, NOT keywords from pause "
             "candidacy (bid-side lever) — adjudicate those by hand.")
    return "\n".join(L)


# ── main ─────────────────────────────────────────────────────────────────
def main():
    p = argparse.ArgumentParser()
    p.add_argument("--db-path", default=str(DEFAULT_DB_PATH))
    p.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    p.add_argument("--brand-csv", default=DEFAULT_BRAND_CSV)
    p.add_argument("--window-start", default="2026-08-16")
    p.add_argument("--window-end", default="2026-08-20")
    p.add_argument("--baseline-end", default=None,
                   help="default = day before window start")
    p.add_argument("--baseline-days", type=int, default=14,
                   help="trailing same-day-class days per class")
    p.add_argument("--cost-floor", type=float, default=50.0,
                   help="min window spend ($, per campaign) to recommend "
                        "a cut")
    p.add_argument("--min-window-spend", type=float, default=25.0,
                   help="rows below this window spend are dropped")
    p.add_argument("--default-target", type=float, default=1.00,
                   help="ROAS target for campaigns with no TARGET_ROAS "
                        "event on record")
    p.add_argument("--mobile-threshold", type=float, default=0.60)
    args = p.parse_args()

    db_path = Path(args.db_path)
    if not db_path.exists():
        sys.exit(f"DB not found: {db_path}")
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    win_start = datetime.strptime(args.window_start, "%Y-%m-%d").date()
    win_end = datetime.strptime(args.window_end, "%Y-%m-%d").date()
    base_end = (datetime.strptime(args.baseline_end, "%Y-%m-%d").date()
                if args.baseline_end else win_start - timedelta(days=1))
    args.baseline_end = base_end.isoformat()

    win_dates = list(daterange(win_start, win_end))
    win_class_counts = {False: sum(1 for d in win_dates
                                   if not is_weekend(d)),
                        True: sum(1 for d in win_dates if is_weekend(d))}
    bdates = baseline_dates(base_end, args.baseline_days)
    base_class_days = {False: len(bdates[False]), True: len(bdates[True])}
    all_base_dates = sorted(bdates[False] + bdates[True])

    conn = connect_ro(db_path)
    s = resolve_schema(conn)
    print_schema(s)
    for req, msg in [(s["kpd_rev"], "keyword_performance_daily revenue"),
                     (s["sqp_rev"], "search_query_performance revenue")]:
        if not req:
            print(f"  WARN: no {msg} column found — ROAS blank at that "
                  "grain")
    other_brands = load_brands(args.brand_csv)
    print(f"  brand registry: {len(other_brands)} tokens loaded" if
          other_brands else
          "  brand registry: not loaded — OTHER_CARRIER limited to "
          "hardcoded junk families")
    camp_names = campaign_names(conn)
    targets = campaign_targets(conn, win_end, args.default_target)
    if targets:
        shown = {camp_names.get(k, k): v for k, v in targets.items()}
        print(f"  campaign tROAS targets (change events): {shown}")
    else:
        print(f"  WARN: no TARGET_ROAS events found — using "
              f"--default-target {args.default_target:.2f} everywhere")

    kw_win = fetch_keyword_grain(conn, s, win_dates, "ALL")
    kw_win_mob = fetch_keyword_grain(conn, s, win_dates, "MOBILE")
    kw_base = fetch_keyword_grain(conn, s, all_base_dates, "ALL",
                                  by_class=True)
    q_win, first_seen = fetch_query_grain(conn, s, win_dates)
    q_win_mob, _ = fetch_query_grain(conn, s, win_dates, device="MOBILE")
    q_base, _ = fetch_query_grain(conn, s, all_base_dates,
                                  by_class=True)
    conn.close()

    kpd_total = sum(v[0] for v in kw_win.values())
    sqp_total = sum(v[0] for v in q_win.values())
    coverage = (sqp_total / kpd_total) if kpd_total > 0 else None

    fam_roas = family_window_roas(kw_win)   # reconcile w/ tripwire segs

    rows = []
    rows += build_rows(q_win, q_win_mob, q_base, first_seen, "query",
                       args, win_class_counts, base_class_days, targets,
                       camp_names, fam_roas, other_brands)
    rows += build_rows(kw_win, kw_win_mob, kw_base, {}, "keyword",
                       args, win_class_counts, base_class_days, targets,
                       camp_names, fam_roas, other_brands)
    annotate_scope(rows)

    cuts = sorted([r for r in rows if r["action"] in ("negative",
                                                      "pause")],
                  key=lambda r: -r["win_spend"])
    earners = sorted([r for r in rows if r["action"] == "earner"],
                     key=lambda r: -r["win_spend"])
    watch = sorted([r for r in rows if r["action"] == "keep-watch"],
                   key=lambda r: -r["win_spend"])

    stamp = win_end.isoformat()
    f_cuts = out_dir / f"expansion_cuts_{stamp}.csv"
    f_earn = out_dir / f"expansion_earners_{stamp}.csv"
    f_watch = out_dir / f"expansion_keepwatch_{stamp}.csv"
    f_sum = out_dir / f"expansion_cut_summary_{stamp}.txt"
    write_csv(f_cuts, cuts)
    write_csv(f_earn, earners)
    write_csv(f_watch, watch)
    text = summarize(cuts, watch, earners, args, coverage)
    f_sum.write_text(text, encoding="utf-8")
    print()
    print(text)
    print()
    for f in (f_cuts, f_earn, f_watch, f_sum):
        print(f"  wrote {f}")


if __name__ == "__main__":
    main()
