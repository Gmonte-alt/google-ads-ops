#!/usr/bin/env python
"""
surge_pause_analysis.py — TEMPORARY keyword/ad-group cuts for the
late-Sept 2026 spend surge on Google Home (account_id=1).

Which keywords (and ad groups) absorbed the surge spend at unprofitable
returns, such that TEMPORARILY pausing them stops the bleed without
touching what's earning. Pairs with the target-side moves already made
(T1 115%->125%, T2 117%->120%, 10/1). Patients: TIER1 + CALIFORNIA;
the other five campaigns are report-only this pass.

LINEAGE: window/baseline/classification conventions reused from
expansion_cut_list.py (gate-approved, Aug 2026) and carrier_tripwire.py
(CARRIER_PATTERNS verbatim). Access patterns per the DB architect's
surge addendum (pinned — no auto-discovery beyond the sanctioned
fallbacks below).

METHOD
  1. Surge window detected PER CAMPAIGN on daily spend
     (campaign_performance_daily_search, device='ALL') vs a trailing-30d
     same-day-class mean: elevated when spend >= 1.10x expected, with
     <= 2-day dips bridged; expected onset ~2026-09-26; hardcoded
     fallback 2026-09-26 -> latest complete day when detection is
     ambiguous. Baseline = trailing 14 same-day-class days per class
     immediately PRE-window, skipping any prior elevated days and the
     known August expansion (2026-08-16..20).
  2. Keyword x campaign (kpd day grain -> composed in Python), rolled
     to ad group x campaign. Same keyword text in different campaigns =
     separate decisions (house precedent).
  3. TWO ROAS VIEWS per row (architect hazard 1 — conversion lag):
     full window AND window-minus-last-2-days. Classified on the FULL
     view (Gilbert's inclusive ruling); rows whose action class flips
     between views are tagged LAG_SENSITIVE. Guidance printed: execute
     DEEP_CUT now, hold LAG_SENSITIVE PAUSE_TEMP for the next morning's
     re-run. revenue_snapshots.csv (the Aug falsification artifact), if
     present, calibrates restatement-by-age against today's settled DB
     values and is printed as context.
  4. AI MAX RESIDUAL (architect hazard 2): per campaign,
     AI_MAX_UNATTRIBUTED = cpd window dSpend/day − kpd window
     dSpend/day — the slice of the surge keyword pauses CANNOT stop
     (reachable only via targets/budgets, already moved, or the AI Max
     toggle — bundling=REQUIRED on all 7, not a-la-carte).
  5. Query grain (architect hazard 3): NEGATIVE_CANDIDATE for
     query-level bleeders WITHOUT a pausable keyword (covered queries
     annotated, not listed). Evidence floor $150 (> the keyword floor),
     section labeled "visible queries only" — sqp suppression means its
     totals DO NOT reconcile to keyword totals, by construction.

CLASSIFICATION (full-window view; thresholds ratify before execution)
  candidacy    window spend >= $100 (hard evidence floor) AND
               (window spend >= $150 OR window spend/day >= $30)
  DEEP_CUT     candidacy + window ROAS < 0.50 (zero-revenue rows count)
               -> execute first
  PAUSE_TEMP   candidacy + window ROAS < 0.85
  WATCH        window spend >= $100, ROAS 0.85..1.00
  EARNING      ROAS >= 1.00 — leave alone
  SURGE_ONLY_BLEEDER flag: baseline ROAS >= 1.2 but window ROAS < 0.85
               — good at rest, bad in expansion: the ideal TEMPORARY
               pause (kept inside PAUSE_TEMP, flagged; see RATIFY
               notes — the prompt's "baseline not strongly earning"
               clause is interpreted as this flag, not an exclusion).

PROTECTIONS (hard rules)
  - Home-intent terms (\b(home|homeowner|homeowners|house)\b) are NEVER
    in PAUSE_TEMP/DEEP_CUT; worst cases -> WATCH with a PROTECTED note.
  - Themed-generic pilot ad groups (name contains "home-generic",
    Gilbert's ruling 10/2 replacing an explicit id list) are EXCLUDED
    from cut candidacy entirely; their surge cost is reported in a
    separate PILOT section listing every excluded ad_group_id + name
    (eyeball for bystanders — architect's date-inference concern).
  - PURCHASE-intent queries never auto-negated (house rule); pause
    candidacy at keyword grain unaffected (TX Farmers-broad precedent).

OUTPUT (all framed TEMPORARY)
  surge_cuts_<end>.csv        ranked cut list w/ cumulative $/day
                              stopped, ALIGNED-vs-August column,
                              pause label + cut_date for next week's
                              tripwire (self-inflicted FADING/FALL-OUT
                              flags on cut rows — annotate, don't chase)
  surge_keepwatch_<end>.csv   WATCH + protected + covered queries
  surge_earners_<end>.csv     do-NOT-cut list
  surge_pilot_<end>.csv       pilot ad groups' surge cost (report only)
  surge_adgroups_<end>.csv    ad-group rollup (patients first)
  surge_summary_<end>.txt     console summary + RATIFY block

DATA RULES: DB read-only (mode=ro), never during a sync; device='ALL'
on kpd/cpd; kpd scoped through keyword_instances join (no account_id);
revenue = primary_conv_value, leads = primary_conv_count (routed
truth); ROAS = summed value / summed cost per cell, never averaged
dailies; campaigns resolved by immutable id, never name; campaign_id
normalized to str (TEXT/int mix in DB); yesterday treated complete
(house lag ruling — the two-view discipline is the mitigation, not an
exclusion); account runs on MST serving days — "latest" = max complete
date in cpd.

NOTHING IS EXECUTED BY THIS SCRIPT. First real run's window dates and
thresholds go back to Gilbert for ratification before any pause is made
in the account.

USAGE
  python surge_pause_analysis.py
  python surge_pause_analysis.py --window-start 2026-09-26 --window-end 2026-10-01
  python surge_pause_analysis.py --indexed-by          # sanctioned kpd hint if slow
  python surge_pause_analysis.py --august-cuts "C:\\...\\expansion_cuts_2026-08-20.csv"
  python surge_pause_analysis.py --snapshots revenue_snapshots.csv
"""

import argparse
import csv
import glob
import os
import re
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

DEFAULT_DB_PATH = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")
DEFAULT_OUT_DIR = Path(r"C:\Projects\google-ads-ops\analysis"
                       r"\profit-attribution-tracker\output\surge-pauses")
DEFAULT_AUG_DIR = Path(r"C:\Projects\google-ads-ops\analysis"
                       r"\profit-attribution-tracker\output\expansion-cuts")
ACCOUNT_ID = 1
BREAK_EVEN = 1.00

# ── campaigns: immutable ids (NEVER resolve by name) ─────────────────────
PATIENTS = {"23496334752": "TIER1", "23385188622": "CALIFORNIA"}
REPORT_ONLY = {"23394808297": "TEXAS", "23394808300": "FLORIDA",
               "23428389081": "TIER4", "23530860942": "TIER2",
               "23586637678": "TIER3"}
CAMPAIGNS = {**PATIENTS, **REPORT_ONLY}
CAMP_ORDER = ["TIER1", "CALIFORNIA", "TIER2", "TIER3", "TIER4",
              "TEXAS", "FLORIDA"]

# ── known prior expansion (excluded from baselines & trend history) ──────
AUG_EXPANSION = (date(2026, 8, 16), date(2026, 8, 20))

# ── account changes landing INSIDE the expected window (annotation only;
#    edit freely — context for reading later window days) ─────────────────
WINDOW_CHANGES = [
    (date(2026, 9, 29), "T2 budget $1,000 -> $1,300"),
    (date(2026, 9, 30), "EOD budget raises: TX $550->$715, "
                        "T3 $800->$925, T4 $500->$650"),
    (date(2026, 10, 1), "targets: T1 115%->125%, T2 117%->120%"),
]

# ── classification thresholds (RATIFY before executing) ──────────────────
SURGE_MULT = 1.10            # spend >= 1.10x trailing same-class mean
BRIDGE_DAYS = 2              # dips bridged inside a window
TREND_DAYS = 30              # trailing trend lookback
FALLBACK_START = date(2026, 9, 26)
MIN_EVIDENCE = 100.0         # hard floor: no cut-list row under this
PAUSE_SPEND = 150.0          # candidacy: window spend >= this ...
PAUSE_SPEND_DAY = 30.0       # ... OR window spend/day >= this
PAUSE_ROAS = 0.85
DEEP_ROAS = 0.50
STRONG_BASE = 1.20           # SURGE_ONLY_BLEEDER baseline bound
QUERY_FLOOR = 150.0          # query grain evidence floor (hazard 3)
TRIM_DAYS = 2                # lag view: window minus last N days
PAUSE_LABEL = "PAUSED-SURGE-20261001"

# ── carrier segments: verbatim from carrier_tripwire.py ──────────────────
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
OTHER_LABEL = "Other/generic"

# ── protections ──────────────────────────────────────────────────────────
# house product regex (architect-confirmed match) — protects ALL carriers'
# home-intent terms, not only Farmers (stricter than the August rule)
HOME_INTENT_RE = re.compile(r"\b(home|homeowner|homeowners|house)\b")
PILOT_NAME_PAT = "home-generic"      # Gilbert's ruling 10/2 (name contains)

# ── intent/junk taxonomy (verbatim from expansion_cut_list.py) ───────────
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
NAV_EXTRA_PATS = ["eagent", "e agent", "www", ".com", " com "]
CONQUEST_JUNK_PATS = ["zander", "kentucky farm bureau", "ky farm bureau",
                      "essent", "frickey", "farmers mutual"]

MATCH_TYPE_MAP = {2: "EXACT", 3: "PHRASE", 4: "BROAD",
                  "2": "EXACT", "3": "PHRASE", "4": "BROAD"}

REV_CANDIDATES = ["primary_conv_value", "conversion_value",
                  "conversions_value", "all_conv_value"]
CONV_CANDIDATES = ["primary_conv_count", "conversions", "all_conv_count"]
CPD_REV_CANDIDATES = ["conversion_value", "conversions_value",
                      "primary_conv_value", "all_conv_value"]
DATE_CANDIDATES = ["date", "report_date"]
SQP_TEXT_CANDIDATES = ["query_text", "search_term", "query", "term",
                       "search_query"]


# ── shared helpers (tripwire/August conventions) ─────────────────────────
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


def intent_of(q: str) -> str:
    q = f" {(q or '').lower()} "
    if SERVICE.search(q):
        return "SERVICE"
    if OTHER_PRODUCT.search(q):
        return "OTHER_PRODUCT"
    if AGENT.search(q):
        return "AGENT"
    if PURCHASE.search(q):
        return "PURCHASE"
    return ""


def junk_family(text: str) -> str:
    t = f" {(text or '').lower()} "
    if any(p in t for p in CONQUEST_JUNK_PATS):
        return "CONQUEST_JUNK"
    if any(p in t for p in NAV_EXTRA_PATS) or SERVICE.search(t):
        return "NAV_LEAK"
    return ""


def is_home_intent(text: str) -> bool:
    return bool(HOME_INTENT_RE.search((text or "").lower()))


def is_weekend(d: date) -> bool:
    return d.weekday() >= 5


def pdate(s) -> date:
    return datetime.strptime(str(s)[:10], "%Y-%m-%d").date()


def daterange(d0: date, d1: date):
    d = d0
    while d <= d1:
        yield d
        d += timedelta(days=1)


def roas_val(spend, rev):
    return (rev / spend) if spend and spend > 0 else None


def fmt_money(v):
    return f"${v:,.0f}"


def cid_s(v) -> str:
    """Normalize campaign_id to str — TEXT/int mix in DB (house 8/24)."""
    s = str(v).strip() if v is not None else "?"
    return s[:-2] if s.endswith(".0") else s


def normalize_target(raw):
    s = str(raw or "").strip().rstrip("%")
    try:
        v = float(s)
    except ValueError:
        return None
    return v / 100.0 if v > 10 else v


def mean(xs):
    xs = list(xs)
    return (sum(xs) / len(xs)) if xs else 0.0


# ── schema resolution (pinned sources + sanctioned fallbacks only) ───────
def resolve_schema(conn):
    s = {}
    kpd_cols = table_cols(conn, "keyword_performance_daily")
    s["kpd_rev"] = pick_col(conn, "keyword_performance_daily",
                            REV_CANDIDATES)
    s["kpd_conv"] = pick_col(conn, "keyword_performance_daily",
                             CONV_CANDIDATES)
    s["kpd_campaign_direct"] = "campaign_id" in kpd_cols   # architect: yes
    ki_cols = table_cols(conn, "keyword_instances")
    s["ki_match"] = "match_type" if "match_type" in ki_cols else None
    s["ki_campaign"] = "campaign_id" in ki_cols
    ag_cols = table_cols(conn, "ad_groups")
    # architect 9/2: ad_groups key = ad_group_id (+account_id), NOT id —
    # detect at runtime (v2 pattern)
    s["ag_key"] = next((c for c in ("ad_group_id", "id") if c in ag_cols),
                       None)
    s["ag_name"] = next((c for c in ("ad_group_name", "name")
                         if c in ag_cols), None)
    s["ag_acct"] = "account_id" in ag_cols
    # campaign grain
    for t in ("campaign_performance_daily_search",
              "campaign_performance_daily"):
        cols = table_cols(conn, t)
        if cols:
            s["cpd_table"] = t
            s["cpd_date"] = next((c for c in DATE_CANDIDATES
                                  if c in cols), None)
            s["cpd_rev"] = next((c for c in CPD_REV_CANDIDATES
                                 if c in cols), None)
            s["cpd_acct"] = "account_id" in cols
            break
    else:
        s["cpd_table"] = s["cpd_date"] = s["cpd_rev"] = None
        s["cpd_acct"] = False
    if s.get("cpd_table") == "campaign_performance_daily":
        print("  WARN: campaign_performance_daily_search view missing — "
              "falling back to base table (PMax rows may contaminate)")
    # query grain
    sqp_cols = table_cols(conn, "search_query_performance")
    s["sqp_date"] = next((c for c in ("report_date", "date")
                          if c in sqp_cols), None)
    s["sqp_rev"] = ("primary_conv_value" if "primary_conv_value"
                    in sqp_cols else None)
    s["sqp_conv"] = ("primary_conv_count" if "primary_conv_count"
                     in sqp_cols else None)
    s["sqp_instance"] = "instance_id" in sqp_cols
    s["sqp_mts"] = ("match_type_served" if "match_type_served"
                    in sqp_cols else None)
    s["sq_text"] = pick_col(conn, "search_queries", SQP_TEXT_CANDIDATES)
    # sqp device handling: EMPIRICAL probe (August convention) —
    # adjudicates the memo-vs-9/9-ruling discrepancy at runtime
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
    print("  schema: kpd rev=%s conv=%s campaign_direct=%s | ad_groups "
          "key=%s name=%s | cpd=%s date=%s rev=%s | sqp date=%s "
          "instance_id=%s"
          % (s["kpd_rev"], s["kpd_conv"], s["kpd_campaign_direct"],
             s["ag_key"], s["ag_name"], s["cpd_table"], s["cpd_date"],
             s["cpd_rev"], s["sqp_date"], s["sqp_instance"]))
    print("  sqp device probe: values %s -> %s" %
          (s["sqp_devices"],
           "ALL rollup present; filtering device='ALL'"
           if s["sqp_filter_all"]
           else "NO ALL rollup; aggregating across devices (9/9 ruling "
                "holds — architect memo's device='ALL' would zero the "
                "scan; flagged for the architect thread)"))


# ── campaign grain: daily spend/rev (window detection + AI Max truth) ────
def fetch_campaign_daily(conn, s, d0: date, d1: date):
    """{cid: {date: [spend, rev]}} over [d0, d1], device='ALL'."""
    rev_e = f"SUM({s['cpd_rev']})" if s["cpd_rev"] else "NULL"
    acct = "AND account_id = ? " if s["cpd_acct"] else ""
    sql = (f"SELECT campaign_id AS cid, {s['cpd_date']} AS d, "
           f"SUM(cost_micros)/1e6 AS spend, {rev_e} AS rev "
           f"FROM {s['cpd_table']} "
           f"WHERE device = 'ALL' {acct}"
           f"AND {s['cpd_date']} >= ? AND {s['cpd_date']} <= ? "
           f"GROUP BY 1, 2")
    params = ([ACCOUNT_ID] if s["cpd_acct"] else []) + \
        [d0.isoformat(), d1.isoformat()]
    out = {}
    for r in conn.execute(sql, params):
        cid = cid_s(r["cid"])
        if cid not in CAMPAIGNS:
            continue
        out.setdefault(cid, {})[pdate(r["d"])] = [r["spend"] or 0.0,
                                                  r["rev"] or 0.0]
    return out


def latest_complete_day(cpd_daily) -> date:
    """Max report date across the seven campaigns (house: yesterday is
    complete; account days are MST serving days)."""
    days = [d for per in cpd_daily.values() for d in per]
    return max(days) if days else date.today() - timedelta(days=1)


# ── window detection (per campaign) ──────────────────────────────────────
def excluded_days() -> set:
    return set(daterange(*AUG_EXPANSION))


def detect_window(daily: dict, latest: date, scan_from: date):
    """Walk scan_from..latest; a day is ELEVATED when spend >= SURGE_MULT
    x mean of same-day-class days in the trailing TREND_DAYS that are
    neither excluded nor already elevated. Returns (start, end, elevated,
    multiples) for the window ending nearest `latest` with <= BRIDGE_DAYS
    dips bridged, or None."""
    excl = excluded_days()
    elevated, mult = set(), {}
    for d in daterange(scan_from, latest):
        cls = is_weekend(d)
        hist = [daily[x][0] for x in
                daterange(d - timedelta(days=TREND_DAYS),
                          d - timedelta(days=1))
                if x in daily and is_weekend(x) == cls
                and x not in excl and x not in elevated]
        exp = mean(hist)
        sp = daily.get(d, [0.0, 0.0])[0]
        if exp > 0 and sp >= SURGE_MULT * exp:
            elevated.add(d)
            mult[d] = sp / exp
    if not elevated:
        return None
    end = max(elevated)
    if (latest - end).days > BRIDGE_DAYS:
        return None                      # surge already over / stale
    start, gap, d = end, 0, end - timedelta(days=1)
    while d >= scan_from:
        if d in elevated:
            start, gap = d, 0
        else:
            gap += 1
            if gap > BRIDGE_DAYS:
                break
        d -= timedelta(days=1)
    return start, latest, elevated, mult   # window runs through latest


def baseline_dates(daily: dict, before: date, n_per_class: int,
                   skip: set):
    """Trailing n_per_class same-day-class days per class ending the day
    before `before`, skipping `skip` (prior elevated + Aug expansion).
    Tripwire convention."""
    out = {True: [], False: []}
    d = before - timedelta(days=1)
    anchor = d
    while len(out[True]) < n_per_class or len(out[False]) < n_per_class:
        if d not in skip and d in daily:
            cls = is_weekend(d)
            if len(out[cls]) < n_per_class:
                out[cls].append(d)
        d -= timedelta(days=1)
        if (anchor - d).days > 200:
            break
    return out


# ── keyword grain: day-level cells, composed in Python ───────────────────
def fetch_keyword_days(conn, s, dates, indexed_by=False):
    """{(txt, mt, cid): {'ag': (agid, agname), 'days': {date: [spend,
    rev, conv, clicks]}}} — device='ALL', scoped through ki join."""
    if not dates:
        return {}
    rev_e = f"SUM(p.{s['kpd_rev']})" if s["kpd_rev"] else "NULL"
    conv_e = f"SUM(p.{s['kpd_conv']})" if s["kpd_conv"] else "NULL"
    match_e = f"ki.{s['ki_match']}" if s["ki_match"] else "NULL"
    # campaign resolution: p.campaign_id (architect-pinned) with
    # sanctioned fallbacks ki.campaign_id -> ag.campaign_id
    if s["kpd_campaign_direct"]:
        camp_e = "p.campaign_id"
    elif s["ki_campaign"]:
        camp_e = "ki.campaign_id"
    else:
        camp_e = "ag.campaign_id"
    ag_join = ""
    name_e = "NULL"
    if s["ag_key"]:
        acct = "AND ag.account_id = ki.account_id" if s["ag_acct"] else ""
        ag_join = (f"LEFT JOIN ad_groups ag ON ag.{s['ag_key']} = "
                   f"ki.ad_group_id {acct}")
        if s["ag_name"]:
            name_e = f"ag.{s['ag_name']}"
    hint = "INDEXED BY idx_kpd_dev_date" if indexed_by else ""
    ph = ",".join("?" for _ in dates)
    sql = f"""
        SELECT LOWER(ki.keyword_text) AS txt, {match_e} AS mt,
               {camp_e} AS cid, ki.ad_group_id AS agid,
               {name_e} AS agname, p.report_date AS d,
               SUM(p.cost_micros)/1e6 AS spend, {rev_e} AS rev,
               {conv_e} AS conv, SUM(p.clicks) AS clicks
        FROM keyword_performance_daily p {hint}
        JOIN keyword_instances ki
          ON ki.id = p.instance_id AND ki.account_id = ?
        {ag_join}
        WHERE p.device = 'ALL' AND p.report_date IN ({ph})
        GROUP BY 1, 2, 3, 4, 5, 6
    """
    out = {}
    for r in conn.execute(sql, [ACCOUNT_ID] +
                          [d.isoformat() for d in dates]):
        cid = cid_s(r["cid"])
        if cid not in CAMPAIGNS:
            continue
        mt = MATCH_TYPE_MAP.get(r["mt"], str(r["mt"] or "").upper())
        key = (r["txt"], mt, cid)
        cell = out.setdefault(key, {"ag": (r["agid"], r["agname"] or ""),
                                    "days": {}})
        day = cell["days"].setdefault(pdate(r["d"]), [0.0, 0.0, 0.0, 0.0])
        day[0] += r["spend"] or 0.0
        day[1] += r["rev"] or 0.0
        day[2] += r["conv"] or 0.0
        day[3] += r["clicks"] or 0.0
    return out


def fetch_instance_map(conn):
    """{instance_id: (txt, mt, cid)} for the covered-by-pause check."""
    out = {}
    try:
        for r in conn.execute(
                "SELECT id, LOWER(keyword_text) AS txt, match_type AS mt,"
                " campaign_id AS cid FROM keyword_instances "
                "WHERE account_id = ?", [ACCOUNT_ID]):
            mt = MATCH_TYPE_MAP.get(r["mt"], str(r["mt"] or "").upper())
            out[r["id"]] = (r["txt"], mt, cid_s(r["cid"]))
    except sqlite3.Error:
        pass
    return out


# ── query grain: day-level cells (hazard 3: suppression-heavy) ───────────
def fetch_query_days(conn, s, dates):
    """{(qtext, cid): {'days': {date: [spend, rev, conv, clicks]},
    'instances': set, 'mts': set}} — account-scoped via campaigns join,
    platform='google', device per empirical probe."""
    if not dates or not (s["sqp_date"] and s["sq_text"]):
        return {}
    rev_e = f"SUM(p.{s['sqp_rev']})" if s["sqp_rev"] else "NULL"
    conv_e = f"SUM(p.{s['sqp_conv']})" if s["sqp_conv"] else "NULL"
    inst_e = "p.instance_id" if s["sqp_instance"] else "NULL"
    mts_e = f"p.{s['sqp_mts']}" if s["sqp_mts"] else "NULL"
    dev_clause = (" AND p.device = 'ALL' " if s["sqp_filter_all"] else "")
    ph = ",".join("?" for _ in dates)
    sql = f"""
        SELECT LOWER(TRIM(sq.{s['sq_text']})) AS txt,
               p.campaign_id AS cid, {inst_e} AS inst, {mts_e} AS mts,
               p.{s['sqp_date']} AS d,
               SUM(p.cost_micros)/1e6 AS spend, {rev_e} AS rev,
               {conv_e} AS conv, SUM(p.clicks) AS clicks
        FROM search_query_performance p
        JOIN search_queries sq ON sq.id = p.query_id
        JOIN campaigns c ON c.campaign_id = p.campaign_id
          AND c.account_id = ?
        WHERE p.platform = 'google' {dev_clause}
          AND p.{s['sqp_date']} IN ({ph})
        GROUP BY 1, 2, 3, 4, 5
    """
    out = {}
    for r in conn.execute(sql, [ACCOUNT_ID] +
                          [d.isoformat() for d in dates]):
        cid = cid_s(r["cid"])
        if cid not in CAMPAIGNS:
            continue
        key = (r["txt"], cid)
        cell = out.setdefault(key, {"days": {}, "instances": set(),
                                    "mts": set()})
        day = cell["days"].setdefault(pdate(r["d"]), [0.0, 0.0, 0.0, 0.0])
        day[0] += r["spend"] or 0.0
        day[1] += r["rev"] or 0.0
        day[2] += r["conv"] or 0.0
        day[3] += r["clicks"] or 0.0
        if r["inst"] is not None:
            cell["instances"].add(r["inst"])
        if r["mts"]:
            cell["mts"].add(str(r["mts"]))
    return out


# ── targets (August pattern: latest TARGET_ROAS event on/before asof) ────
def campaign_targets(conn, asof: date, default_target: float):
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
                targets[cid_s(r["campaign_id"])] = t
    except sqlite3.Error:
        pass
    return targets


# ── composition: window / trim / baseline per key ────────────────────────
def compose(cell_days: dict, dates: list):
    sp = rv = cv = ck = 0.0
    for d in dates:
        v = cell_days.get(d)
        if v:
            sp += v[0]
            rv += v[1]
            cv += v[2]
            ck += v[3]
    return sp, rv, cv, ck


def class_counts(dates):
    return {False: sum(1 for d in dates if not is_weekend(d)),
            True: sum(1 for d in dates if is_weekend(d))}


def expected_spend(cell_days, bdates: dict, win_counts: dict):
    """Expected window spend composed from baseline same-class daily
    rates x the window's actual day-class mix (August convention —
    weekday/weekend mixing never manufactures a delta)."""
    exp = 0.0
    for cls in (False, True):
        days = bdates.get(cls, [])
        rate = (compose(cell_days, days)[0] / len(days)) if days else 0.0
        exp += rate * win_counts.get(cls, 0)
    return exp


# ── classification ───────────────────────────────────────────────────────
def kw_class(w_spend, w_roas, n_days, protected):
    """Full action-class ladder for one view. Returns (class, note)."""
    spend_day = (w_spend / n_days) if n_days else 0.0
    candidacy = (w_spend >= MIN_EVIDENCE and
                 (w_spend >= PAUSE_SPEND or spend_day >= PAUSE_SPEND_DAY))
    losing = (w_roas is not None and w_roas < PAUSE_ROAS) or w_roas is None
    deep = (w_roas is not None and w_roas < DEEP_ROAS) or w_roas is None
    if w_roas is not None and w_roas >= BREAK_EVEN:
        return "EARNING", ""
    if protected and candidacy and losing:
        return "WATCH", ("PROTECTED home-intent — never paused; "
                         "bid/copy/QS levers only")
    if candidacy and deep:
        return "DEEP_CUT", ""
    if candidacy and losing:
        return "PAUSE_TEMP", ""
    if w_spend >= MIN_EVIDENCE:
        if w_roas is not None and w_roas >= PAUSE_ROAS:
            return "WATCH", f"ROAS {w_roas:.2f} in 0.85–1.00 band"
        return "WATCH", "losing but below candidacy floors"
    return "LOW_EVIDENCE", f"window spend under ${MIN_EVIDENCE:.0f} floor"


def q_class(w_spend, w_roas, purchase, protected, covered, junk):
    losing = (w_roas is not None and w_roas < PAUSE_ROAS) or w_roas is None
    if w_spend < QUERY_FLOOR:
        return "LOW_EVIDENCE", f"under ${QUERY_FLOOR:.0f} query floor"
    if w_roas is not None and w_roas >= BREAK_EVEN:
        return "EARNING", ""
    if covered:
        return "WATCH", ("COVERED_BY_KEYWORD_PAUSE — maps to a keyword "
                         "already on the cut list; no separate negative")
    if protected and losing:
        return "WATCH", "PROTECTED home-intent — never negated"
    if purchase and not junk and losing:
        return "WATCH", ("PURCHASE intent — never auto-negated "
                         "(house rule; junk families override); "
                         "bid/copy lever")
    if losing:
        return "NEGATIVE_CANDIDATE", ""
    return "WATCH", f"ROAS {w_roas:.2f} in 0.85–1.00 band"


# ── August reconciliation ────────────────────────────────────────────────
def load_august_cuts(path_or_dir):
    """Returns (kw_keys, q_keys) from the August expansion_cuts CSV.
    kw key = (text, match_type, campaign_id); q key = (text,
    campaign_id) — architect's ALIGNED keying."""
    p = Path(path_or_dir) if path_or_dir else None
    f = None
    if p and p.is_file():
        f = p
    elif p and p.is_dir():
        hits = sorted(glob.glob(str(p / "expansion_cuts_*.csv")))
        f = Path(hits[-1]) if hits else None
    if not f or not f.exists():
        return set(), set(), None
    kw, q = set(), set()
    try:
        for r in csv.DictReader(open(f, encoding="utf-8")):
            txt = (r.get("text") or "").strip().lower()
            cid = cid_s(r.get("campaign_id"))
            if r.get("grain") == "keyword":
                kw.add((txt, (r.get("match_type") or "").upper(), cid))
            elif r.get("grain") == "query":
                q.add((txt, cid))
    except Exception as e:
        print(f"  WARN: August cuts unreadable ({e}) — ALIGNED blank")
        return set(), set(), None
    return kw, q, f


# ── hazard-1 calibration: revenue_snapshots.csv vs settled DB ────────────
def lag_calibration(conn, s, snap_path):
    """The Aug falsification artifact: account-day spend/revenue frozen
    at sync_date. Compare to today's settled values -> restatement by
    age. Source auto-picked (cpd vs kpd) by spend reconciliation."""
    p = Path(snap_path) if snap_path else None
    if not p or not p.exists():
        return None
    snaps = []
    try:
        for r in csv.DictReader(open(p, encoding="utf-8-sig")):
            snaps.append((pdate(r["report_date"]), pdate(r["sync_date"]),
                          float(r["spend"]), float(r["revenue"])))
    except Exception as e:
        print(f"  WARN: snapshots unreadable ({e}) — lag calibration "
              "skipped")
        return None
    if not snaps:
        return None
    d0 = min(x[0] for x in snaps)
    d1 = max(x[0] for x in snaps)
    # settled account-day totals from cpd AND kpd
    cpd = fetch_campaign_daily(conn, s, d0, d1)
    cpd_day = {}
    for per in cpd.values():
        for d, v in per.items():
            c = cpd_day.setdefault(d, [0.0, 0.0])
            c[0] += v[0]
            c[1] += v[1]
    kw = fetch_keyword_days(conn, s, list(daterange(d0, d1)))
    kpd_day = {}
    for cell in kw.values():
        for d, v in cell["days"].items():
            c = kpd_day.setdefault(d, [0.0, 0.0])
            c[0] += v[0]
            c[1] += v[1]
    # pick source by spend reconciliation across snapshot days
    def misfit(day_tot):
        errs = [abs(day_tot.get(rd, [0, 0])[0] - sp) / sp
                for rd, _, sp, _ in snaps if sp > 0]
        return mean(errs) if errs else 9e9
    src, day_tot = (("cpd", cpd_day)
                    if misfit(cpd_day) <= misfit(kpd_day)
                    else ("kpd", kpd_day))
    rows = []
    for rd, sd, sp, rv in sorted(snaps):
        now = day_tot.get(rd)
        if not now or rv <= 0:
            continue
        rows.append({"report_date": rd, "age_days": (sd - rd).days,
                     "rev_snap": rv, "rev_now": now[1],
                     "uplift": now[1] / rv - 1.0,
                     "spend_match": (abs(now[0] - sp) / sp
                                     if sp > 0 else None)})
    return {"source": src, "rows": rows} if rows else None


# ── output ───────────────────────────────────────────────────────────────
CUT_FIELDS = ["rank", "action", "lag_sensitive", "surge_only_bleeder",
              "text", "match_type", "ad_group", "ad_group_id", "campaign",
              "campaign_id", "segment", "intent", "junk_family",
              "win_spend", "win_rev", "win_roas", "win_roas_trim2",
              "base_spend", "base_roas", "delta_spend_day", "win_gp",
              "win_leads", "spend_day_stopped", "cum_spend_day_stopped",
              "aligned_august", "pause_label", "cut_date", "re_enable",
              "note"]
WATCH_FIELDS = [f for f in CUT_FIELDS if f not in
                ("rank", "spend_day_stopped", "cum_spend_day_stopped",
                 "pause_label", "cut_date", "re_enable")]
AG_FIELDS = ["campaign", "campaign_id", "ad_group", "ad_group_id",
             "pilot", "win_spend", "win_rev", "win_roas",
             "win_roas_trim2", "base_roas", "delta_spend_day", "win_gp",
             "n_cut_rows", "cut_spend_day"]
PILOT_FIELDS = ["campaign", "campaign_id", "ad_group", "ad_group_id",
                "win_spend", "win_rev", "win_roas", "win_gp",
                "delta_spend_day"]


def write_csv(path, rows, fields):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


# ── main ─────────────────────────────────────────────────────────────────
def parse_args():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    p.add_argument("--db", default=str(DEFAULT_DB_PATH))
    p.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    p.add_argument("--window-start", default=None,
                   help="override detected window start (YYYY-MM-DD, "
                        "all campaigns)")
    p.add_argument("--window-end", default=None,
                   help="override window end (default: latest complete "
                        "day in cpd)")
    p.add_argument("--baseline-days", type=int, default=14,
                   help="trailing same-day-class days per class")
    p.add_argument("--pilot-pattern", default=PILOT_NAME_PAT,
                   help="ad-group NAME substring excluded as the "
                        "themed-generic pilot (Gilbert 10/2)")
    p.add_argument("--august-cuts", default=str(DEFAULT_AUG_DIR),
                   help="August expansion_cuts CSV (file or dir; latest "
                        "picked from a dir)")
    p.add_argument("--snapshots", default="revenue_snapshots.csv",
                   help="Aug falsification snapshot CSV for the lag "
                        "calibration (skipped if absent)")
    p.add_argument("--default-target", type=float, default=1.0)
    p.add_argument("--indexed-by", action="store_true",
                   help="apply the sanctioned INDEXED BY "
                        "idx_kpd_dev_date hint (time it, don't "
                        "plan-read)")
    p.add_argument("--lines-cap", type=int, default=30)
    return p.parse_args()


def main():
    args = parse_args()
    db_path = Path(args.db)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    if not db_path.exists():
        sys.exit(f"DB not found: {db_path}")

    t0 = time.time()
    conn = connect_ro(db_path)
    s = resolve_schema(conn)
    print_schema(s)

    # ── stage 1: campaign dailies + window detection ─────────────────
    scan_fetch_from = date(2026, 7, 15)      # trend + Aug calibration room
    today_guess = date.today()
    cpd = fetch_campaign_daily(conn, s, scan_fetch_from, today_guess)
    if not cpd:
        sys.exit("No campaign rows found for the seven ids — check "
                 "account/view.")
    latest = latest_complete_day(cpd)
    if args.window_end:
        latest = pdate(args.window_end)
    print(f"  latest complete day (MST serving): {latest}")

    windows, excl_prior = {}, excluded_days()
    scan_from = date(2026, 9, 1)
    for cid, label in CAMPAIGNS.items():
        if args.window_start:
            w0 = pdate(args.window_start)
            windows[cid] = {"start": w0, "end": latest, "mode": "manual",
                            "elevated": set(daterange(w0, latest)),
                            "mult": {}}
            continue
        det = detect_window(cpd.get(cid, {}), latest, scan_from)
        if det:
            w0, w1, elev, mult = det
            mode = "detected"
        else:
            w0, w1, elev, mult = FALLBACK_START, latest, set(), {}
            mode = "FALLBACK (no clean detection)"
        windows[cid] = {"start": w0, "end": w1, "mode": mode,
                        "elevated": elev, "mult": mult}

    # per-campaign date sets
    win_dates, trim_dates, base_cls = {}, {}, {}
    all_dates = set()
    for cid, w in windows.items():
        dates = sorted(daterange(w["start"], w["end"]))
        win_dates[cid] = dates
        trim_dates[cid] = dates[:-TRIM_DAYS] if len(dates) > TRIM_DAYS \
            else dates[:1]
        skip = excl_prior | {d for d in w["elevated"] if d < w["start"]}
        base_cls[cid] = baseline_dates(cpd.get(cid, {}), w["start"],
                                       args.baseline_days, skip)
        all_dates.update(dates)
        for cls in (False, True):
            all_dates.update(base_cls[cid][cls])

    targets = campaign_targets(conn, latest, args.default_target)

    # ── stage 2: keyword + query day cells ───────────────────────────
    t = time.time()
    kw_cells = fetch_keyword_days(conn, s, sorted(all_dates),
                                  indexed_by=args.indexed_by)
    print(f"  kpd fetch: {len(kw_cells):,} keyword cells in "
          f"{time.time() - t:.1f}s"
          + (" (INDEXED BY idx_kpd_dev_date)" if args.indexed_by else ""))
    t = time.time()
    q_cells = fetch_query_days(conn, s, sorted(all_dates))
    print(f"  sqp fetch: {len(q_cells):,} query cells in "
          f"{time.time() - t:.1f}s")
    inst_map = fetch_instance_map(conn) if s["sqp_instance"] else {}

    # ── stage 3: hazard-1 calibration (optional) ─────────────────────
    calib = lag_calibration(conn, s, args.snapshots)
    conn.close()

    pilot_pat = (args.pilot_pattern or "").lower()

    # ── stage 4: keyword rows, two views ─────────────────────────────
    kw_rows, pilot_agg = [], {}
    for (txt, mt, cid), cell in kw_cells.items():
        dates = win_dates[cid]
        n_days = len(dates)
        counts = class_counts(dates)
        bdates = base_cls[cid]
        n_base = {c: len(bdates[c]) for c in (False, True)}
        w_sp, w_rv, w_cv, w_ck = compose(cell["days"], dates)
        if w_sp <= 0:
            continue
        agid, agname = cell["ag"]
        is_pilot = bool(pilot_pat) and pilot_pat in (agname or "").lower()
        b_sp, b_rv, _, _ = compose(cell["days"],
                                   bdates[False] + bdates[True])
        exp_sp = expected_spend(cell["days"], bdates, counts)
        d_day = (w_sp - exp_sp) / n_days if n_days else 0.0
        w_roas = roas_val(w_sp, w_rv)
        b_roas = roas_val(b_sp, b_rv)
        t_sp, t_rv, _, _ = compose(cell["days"], trim_dates[cid])
        t_roas = roas_val(t_sp, t_rv)

        if is_pilot:
            # pilot: report-only aggregation, NO candidacy
            k = (cid, agid, agname)
            a = pilot_agg.setdefault(k, [0.0, 0.0, 0.0, 0.0])
            a[0] += w_sp
            a[1] += w_rv
            a[2] += d_day
            a[3] += w_rv - w_sp
            continue

        protected = is_home_intent(txt)
        cls_full, note = kw_class(w_sp, w_roas, n_days, protected)
        cls_trim, _ = kw_class(t_sp, t_roas, len(trim_dates[cid]),
                               protected)
        lag_sens = "Y" if cls_full != cls_trim else ""
        bleeder = ("Y" if (b_roas is not None and b_roas >= STRONG_BASE
                           and w_roas is not None and w_roas < PAUSE_ROAS)
                   else "")
        kw_rows.append({
            "action": cls_full, "lag_sensitive": lag_sens,
            "surge_only_bleeder": bleeder, "text": txt, "match_type": mt,
            "ad_group": agname, "ad_group_id": agid,
            "campaign": CAMPAIGNS[cid], "campaign_id": cid,
            "segment": classify(txt), "intent": intent_of(txt),
            "junk_family": junk_family(txt),
            "win_spend": round(w_sp, 2), "win_rev": round(w_rv, 2),
            "win_roas": round(w_roas, 3) if w_roas is not None else "",
            "win_roas_trim2": (round(t_roas, 3) if t_roas is not None
                               else ""),
            "base_spend": round(b_sp, 2),
            "base_roas": round(b_roas, 3) if b_roas is not None else "",
            "delta_spend_day": round(d_day, 2),
            "win_gp": round(w_rv - w_sp, 2),
            "win_leads": round(w_cv, 1),
            "spend_day_stopped": round(w_sp / n_days, 2) if n_days else 0,
            "note": note, "grain": "keyword",
        })

    # ── stage 5: query rows (visible queries only) ───────────────────
    kw_cut_keys = {(r["text"], r["match_type"], r["campaign_id"])
                   for r in kw_rows
                   if r["action"] in ("DEEP_CUT", "PAUSE_TEMP")}
    q_rows = []
    for (txt, cid), cell in q_cells.items():
        dates = win_dates[cid]
        n_days = len(dates)
        counts = class_counts(dates)
        bdates = base_cls[cid]
        w_sp, w_rv, w_cv, _ = compose(cell["days"], dates)
        if w_sp < QUERY_FLOOR:                 # hazard-3 floor, early
            continue
        b_sp, b_rv, _, _ = compose(cell["days"],
                                   bdates[False] + bdates[True])
        exp_sp = expected_spend(cell["days"], bdates, counts)
        d_day = (w_sp - exp_sp) / n_days if n_days else 0.0
        w_roas = roas_val(w_sp, w_rv)
        b_roas = roas_val(b_sp, b_rv)
        t_sp, t_rv, _, _ = compose(cell["days"], trim_dates[cid])
        t_roas = roas_val(t_sp, t_rv)
        covered = any(inst_map.get(i) in kw_cut_keys
                      for i in cell["instances"])
        purchase = intent_of(txt) == "PURCHASE"
        protected = is_home_intent(txt)
        junk = junk_family(txt)
        cls_full, note = q_class(w_sp, w_roas, purchase, protected,
                                 covered, junk)
        cls_trim, _ = q_class(t_sp, t_roas, purchase, protected,
                              covered, junk)
        lag_sens = "Y" if cls_full != cls_trim else ""
        bleeder = ("Y" if (b_roas is not None and b_roas >= STRONG_BASE
                           and w_roas is not None and w_roas < PAUSE_ROAS)
                   else "")
        q_rows.append({
            "action": cls_full, "lag_sensitive": lag_sens,
            "surge_only_bleeder": bleeder, "text": txt,
            "match_type": "|".join(sorted(cell["mts"])) or "",
            "ad_group": "", "ad_group_id": "",
            "campaign": CAMPAIGNS[cid], "campaign_id": cid,
            "segment": classify(txt), "intent": intent_of(txt),
            "junk_family": junk,
            "win_spend": round(w_sp, 2), "win_rev": round(w_rv, 2),
            "win_roas": round(w_roas, 3) if w_roas is not None else "",
            "win_roas_trim2": (round(t_roas, 3) if t_roas is not None
                               else ""),
            "base_spend": round(b_sp, 2),
            "base_roas": round(b_roas, 3) if b_roas is not None else "",
            "delta_spend_day": round(d_day, 2),
            "win_gp": round(w_rv - w_sp, 2),
            "win_leads": round(w_cv, 1),
            "spend_day_stopped": round(w_sp / n_days, 2) if n_days else 0,
            "note": note, "grain": "query",
        })

    # ── stage 6: August reconciliation ───────────────────────────────
    aug_kw, aug_q, aug_file = load_august_cuts(args.august_cuts)
    for r in kw_rows:
        r["aligned_august"] = ("ALIGNED" if
                               (r["text"], r["match_type"],
                                r["campaign_id"]) in aug_kw else "")
    for r in q_rows:
        r["aligned_august"] = ("ALIGNED" if (r["text"], r["campaign_id"])
                               in aug_q else "")

    # ── stage 7: assemble outputs ────────────────────────────────────
    sev = {"DEEP_CUT": 0, "PAUSE_TEMP": 1, "NEGATIVE_CANDIDATE": 2}
    cuts = sorted([r for r in kw_rows + q_rows if r["action"] in sev],
                  key=lambda r: (sev[r["action"]],
                                 -r["spend_day_stopped"]))
    cum = 0.0
    for i, r in enumerate(cuts, 1):
        cum += r["spend_day_stopped"]
        r["rank"] = i
        r["cum_spend_day_stopped"] = round(cum, 2)
        r["pause_label"] = PAUSE_LABEL
        r["cut_date"] = latest.isoformat()
        r["re_enable"] = ("review at surge end; next week's tripwire "
                          "will show self-inflicted FADING/FALL-OUT on "
                          "this row — annotated, don't chase")
    watch = sorted([r for r in kw_rows + q_rows
                    if r["action"] == "WATCH"],
                   key=lambda r: -r["win_spend"])
    earners = sorted([r for r in kw_rows + q_rows
                      if r["action"] == "EARNING"],
                     key=lambda r: -r["win_spend"])

    # ad-group rollup (pilot flagged; patients first)
    ag_agg = {}
    for r in kw_rows:
        k = (r["campaign_id"], r["ad_group_id"], r["ad_group"])
        a = ag_agg.setdefault(k, {"sp": 0.0, "rv": 0.0, "tsp": 0.0,
                                  "trv": 0.0, "bsp": 0.0, "brv": 0.0,
                                  "dd": 0.0, "ncut": 0, "cutd": 0.0})
        a["sp"] += r["win_spend"]
        a["rv"] += r["win_rev"]
        a["bsp"] += r["base_spend"]
        a["brv"] += r["base_spend"] * (r["base_roas"] or 0) \
            if r["base_roas"] != "" else 0
        a["dd"] += r["delta_spend_day"]
        if r["action"] in ("DEEP_CUT", "PAUSE_TEMP"):
            a["ncut"] += 1
            a["cutd"] += r["spend_day_stopped"]
    ag_rows = []
    for (cid, agid, agname), a in ag_agg.items():
        ag_rows.append({
            "campaign": CAMPAIGNS[cid], "campaign_id": cid,
            "ad_group": agname, "ad_group_id": agid, "pilot": "",
            "win_spend": round(a["sp"], 2), "win_rev": round(a["rv"], 2),
            "win_roas": (round(a["rv"] / a["sp"], 3) if a["sp"] > 0
                         else ""),
            "win_roas_trim2": "",
            "base_roas": (round(a["brv"] / a["bsp"], 3) if a["bsp"] > 0
                          else ""),
            "delta_spend_day": round(a["dd"], 2),
            "win_gp": round(a["rv"] - a["sp"], 2),
            "n_cut_rows": a["ncut"], "cut_spend_day": round(a["cutd"], 2),
        })
    for (cid, agid, agname), a in pilot_agg.items():
        ag_rows.append({
            "campaign": CAMPAIGNS[cid], "campaign_id": cid,
            "ad_group": agname, "ad_group_id": agid, "pilot": "PILOT",
            "win_spend": round(a[0], 2), "win_rev": round(a[1], 2),
            "win_roas": round(a[1] / a[0], 3) if a[0] > 0 else "",
            "win_roas_trim2": "", "base_roas": "",
            "delta_spend_day": round(a[2], 2), "win_gp": round(a[3], 2),
            "n_cut_rows": 0, "cut_spend_day": 0.0,
        })
    order = {lbl: i for i, lbl in enumerate(CAMP_ORDER)}
    ag_rows.sort(key=lambda r: (order.get(r["campaign"], 9),
                                -r["win_spend"]))
    pilot_rows = [{
        "campaign": CAMPAIGNS[cid], "campaign_id": cid,
        "ad_group": agname, "ad_group_id": agid,
        "win_spend": round(a[0], 2), "win_rev": round(a[1], 2),
        "win_roas": round(a[1] / a[0], 3) if a[0] > 0 else "",
        "win_gp": round(a[3], 2), "delta_spend_day": round(a[2], 2),
    } for (cid, agid, agname), a in sorted(
        pilot_agg.items(), key=lambda kv: -kv[1][0])]

    # ── stage 8: campaign summary (AI Max residual beside coverage) ──
    camp_sum = {}
    for cid, label in CAMPAIGNS.items():
        dates = win_dates[cid]
        n_days = len(dates)
        counts = class_counts(dates)
        daily = cpd.get(cid, {})
        c_win = compose({d: v + [0, 0] for d, v in daily.items()}, dates)
        bdates = base_cls[cid]
        c_exp = expected_spend({d: v + [0, 0] for d, v in daily.items()},
                               bdates, counts)
        c_dday = (c_win[0] - c_exp) / n_days if n_days else 0.0
        kpd_win_sp = kpd_exp = 0.0
        for (t_, m_, c_), cell in kw_cells.items():
            if c_ != cid:
                continue
            kpd_win_sp += compose(cell["days"], dates)[0]
            kpd_exp += expected_spend(cell["days"], bdates, counts)
        k_dday = (kpd_win_sp - kpd_exp) / n_days if n_days else 0.0
        cut_day = sum(r["spend_day_stopped"] for r in cuts
                      if r["campaign_id"] == cid
                      and r["grain"] == "keyword")
        cut_delta = sum(max(0.0, r["delta_spend_day"]) for r in cuts
                        if r["campaign_id"] == cid
                        and r["grain"] == "keyword")
        pilot_day = sum(a[0] for (c_, _, _), a in pilot_agg.items()
                        if c_ == cid) / n_days if n_days else 0.0
        c_roas = roas_val(c_win[0], c_win[1])
        camp_sum[cid] = {
            "label": label, "window": windows[cid], "n_days": n_days,
            "win_spend": c_win[0], "win_roas": c_roas,
            "surge_dday": c_dday, "kpd_dday": k_dday,
            "aimax_dday": c_dday - k_dday, "cut_dday": cut_day,
            "cut_delta_day": cut_delta,
            "pilot_spend_day": pilot_day,
            "target": targets.get(cid, args.default_target),
        }

    # ── stage 9: write + print ───────────────────────────────────────
    stamp = latest.isoformat()
    f_cuts = out_dir / f"surge_cuts_{stamp}.csv"
    f_watch = out_dir / f"surge_keepwatch_{stamp}.csv"
    f_earn = out_dir / f"surge_earners_{stamp}.csv"
    f_pilot = out_dir / f"surge_pilot_{stamp}.csv"
    f_ag = out_dir / f"surge_adgroups_{stamp}.csv"
    f_sum = out_dir / f"surge_summary_{stamp}.txt"
    write_csv(f_cuts, cuts, CUT_FIELDS)
    write_csv(f_watch, watch, WATCH_FIELDS)
    write_csv(f_earn, earners, WATCH_FIELDS)
    write_csv(f_pilot, pilot_rows, PILOT_FIELDS)
    write_csv(f_ag, ag_rows, AG_FIELDS)

    text = summarize(cuts, watch, earners, pilot_rows, camp_sum, windows,
                     calib, aug_file, targets, args, latest)
    f_sum.write_text(text, encoding="utf-8")
    print()
    print(text)
    print()
    for f in (f_cuts, f_watch, f_earn, f_pilot, f_ag, f_sum):
        print(f"  wrote {f}")
    print(f"  total {time.time() - t0:.1f}s")


def summarize(cuts, watch, earners, pilot_rows, camp_sum, windows, calib,
              aug_file, targets, args, latest):
    L = []
    L.append(f"SURGE PAUSE ANALYSIS — Google Home (acct 1), run thru "
             f"{latest} (MST serving days)")
    L.append(f"ALL RECOMMENDATIONS TEMPORARY — label {PAUSE_LABEL}, "
             f"re-enable review at surge end. NOTHING EXECUTED by this "
             f"script.")
    L.append("")

    # windows + baselines (ratify)
    L.append("DETECTED WINDOWS (per campaign; vs trailing "
             f"{TREND_DAYS}d same-class trend, >= {SURGE_MULT:.2f}x, "
             f"<= {BRIDGE_DAYS}-day dips bridged; baseline = "
             f"{args.baseline_days}/class pre-window days, prior "
             "elevated + Aug 8/16-20 excluded)")
    for cid in sorted(camp_sum, key=lambda c:
                      CAMP_ORDER.index(camp_sum[c]["label"])):
        cs = camp_sum[cid]
        w = cs["window"]
        tag = "PATIENT" if cid in PATIENTS else "report-only"
        mults = sorted(w["mult"].values())
        pk = f", peak {max(mults):.2f}x" if mults else ""
        L.append(f"  {cs['label']:<11} {w['start']} -> {w['end']} "
                 f"({cs['n_days']}d, {w['mode']}{pk})  [{tag}]  "
                 f"tROAS target {cs['target']:.2f}")
    chg = [f"{d} {lbl}" for d, lbl in WINDOW_CHANGES]
    L.append("  account changes INSIDE window (read later days as "
             "new-regime): " + "; ".join(chg))
    L.append("")

    # hazard-1 calibration
    if calib:
        L.append(f"CONVERSION-LAG CALIBRATION (revenue_snapshots.csv vs "
                 f"settled DB; source auto-picked = {calib['source']}) — "
                 "restatement since snapshot age:")
        for r in calib["rows"]:
            sm = (f" (spend match {r['spend_match']:.1%} off)"
                  if r["spend_match"] and r["spend_match"] > 0.02 else "")
            L.append(f"  {r['report_date']}  age {r['age_days']:>2}d at "
                     f"snapshot: revenue has restated "
                     f"{r['uplift']:+.1%} since{sm}")
        young = [r["uplift"] for r in calib["rows"] if r["age_days"] <= 2]
        old = [r["uplift"] for r in calib["rows"] if r["age_days"] >= 5]
        if young:
            L.append(f"  -> days read at age <=2 restated "
                     f"{mean(young):+.1%} on avg (Aug evidence; upper "
                     "bound incl. later backfills) — this is what the "
                     "trim-2 view + LAG_SENSITIVE discipline guards")
        if old:
            L.append(f"  -> by age >=5 restatement was {mean(old):+.1%} "
                     "— mostly settled")
        L.append("")

    # cut list
    n_lag = sum(1 for r in cuts if r["lag_sensitive"])
    n_sob = sum(1 for r in cuts if r["surge_only_bleeder"])
    L.append(f"RANKED CUT LIST — {len(cuts)} rows "
             f"({sum(1 for r in cuts if r['action'] == 'DEEP_CUT')} "
             f"DEEP_CUT, "
             f"{sum(1 for r in cuts if r['action'] == 'PAUSE_TEMP')} "
             f"PAUSE_TEMP, "
             f"{sum(1 for r in cuts if r['action'] == 'NEGATIVE_CANDIDATE')} "
             f"NEGATIVE_CANDIDATE); {n_lag} LAG_SENSITIVE, {n_sob} "
             "SURGE_ONLY_BLEEDER")
    L.append("  EXECUTION GUIDANCE (architect hazard 1): execute "
             "DEEP_CUT now; HOLD any LAG_SENSITIVE PAUSE_TEMP for "
             "tomorrow morning's re-run — one day of settling converts "
             "many honestly.")
    L.append(f"  {'#':>3} {'action':<18} {'text':<32.32} "
             f"{'camp':<10.10} {'win$':>7} {'ROAS':>5} {'trim2':>5} "
             f"{'base':>5} {'Δ$/d':>6} {'stop$/d':>7} {'cum':>7}  flags")
    for r in cuts[:args.lines_cap]:
        flags = ",".join(x for x in (
            "LAG" if r["lag_sensitive"] else "",
            "SOB" if r["surge_only_bleeder"] else "",
            r["aligned_august"],
            r["segment"] if r["segment"] != OTHER_LABEL else "",
            r["junk_family"], r["grain"] if r["grain"] == "query"
            else "") if x)
        L.append(f"  {r['rank']:>3} {r['action']:<18} {r['text']:<32.32} "
                 f"{r['campaign']:<10.10} {r['win_spend']:>7,.0f} "
                 f"{(r['win_roas'] if r['win_roas'] != '' else 0):>5.2f} "
                 f"{(r['win_roas_trim2'] if r['win_roas_trim2'] != '' else 0):>5.2f} "
                 f"{(r['base_roas'] if r['base_roas'] != '' else 0):>5.2f} "
                 f"{r['delta_spend_day']:>+6,.0f} "
                 f"{r['spend_day_stopped']:>7,.0f} "
                 f"{r['cum_spend_day_stopped']:>7,.0f}  {flags}")
    if len(cuts) > args.lines_cap:
        L.append(f"  ... {len(cuts) - args.lines_cap} more in CSV")
    L.append("  NEGATIVE_CANDIDATE section is VISIBLE QUERIES ONLY "
             f"(sqp suppression; >= ${QUERY_FLOOR:.0f} floor) — its "
             "totals do NOT reconcile to keyword totals, by "
             "construction.")
    if aug_file:
        n_al = sum(1 for r in cuts if r["aligned_august"])
        L.append(f"  ALIGNED vs August ({Path(aug_file).name}): {n_al} "
                 "of the cut rows also appeared in the August list — "
                 "execute those with confidence.")
    else:
        L.append("  August cuts CSV not found — ALIGNED column blank "
                 "(--august-cuts).")
    L.append("")

    # campaign coverage (patients first) + AI Max residual
    L.append("PER-CAMPAIGN SURGE COVERAGE (hazard 2 — part of the bleed "
             "is structurally unpausable at keyword grain)")
    L.append(f"  {'campaign':<11} {'surge Δ$/d':>10} "
             f"{'cuts stop$/d':>12} {'cut Δ$/d':>8} {'addressed':>9} "
             f"{'AI-Max unattrib':>15} {'pilot $/d':>9} {'win ROAS':>8}")
    L.append("  (addressed = cut rows' INCREMENTAL Δ$/d vs the "
             "campaign's surge Δ$/d; 'cuts stop$/d' is the larger "
             "full spend the pauses halt)")
    for cid in sorted(camp_sum, key=lambda c:
                      CAMP_ORDER.index(camp_sum[c]["label"])):
        cs = camp_sum[cid]
        addr = (cs["cut_delta_day"] / cs["surge_dday"]
                if cs["surge_dday"] > 0 else 0.0)
        L.append(f"  {cs['label']:<11} {cs['surge_dday']:>+10,.0f} "
                 f"{cs['cut_dday']:>12,.0f} "
                 f"{cs['cut_delta_day']:>8,.0f} {addr:>9.0%} "
                 f"{cs['aimax_dday']:>+15,.0f} "
                 f"{cs['pilot_spend_day']:>9,.0f} "
                 f"{(cs['win_roas'] or 0):>8.2f}")
    L.append("  AI-Max-unattributed Δ$/d can only be stopped by "
             "target/budget moves (made 9/29-10/1) or the AI Max toggle "
             "— bundling=REQUIRED on all 7 campaigns, not a-la-carte.")
    L.append("")

    # pilot section
    L.append(f"THEMED-GENERIC PILOT (ad-group name contains "
             f"'{args.pilot_pattern}') — EXCLUDED from cut candidacy; "
             "surge cost report only. EYEBALL this list for bystanders "
             "(name-pattern ruling 10/2 replaced the explicit id list):")
    if pilot_rows:
        for r in pilot_rows:
            L.append(f"  {r['campaign']:<11} agid {r['ad_group_id']}  "
                     f"{r['ad_group']:<40.40} win {fmt_money(r['win_spend'])} "
                     f" ROAS {(r['win_roas'] or 0):.2f}  "
                     f"GP {r['win_gp']:+,.0f}  Δ$/d "
                     f"{r['delta_spend_day']:+,.0f}")
    else:
        L.append("  (no ad groups matched the pilot pattern — verify "
                 "the pattern if that's unexpected)")
    L.append("")

    # protected / watch / earners
    prot = [r for r in watch if "PROTECTED" in r["note"]]
    L.append(f"PROTECTED home-intent rows parked in WATCH: {len(prot)} "
             f"(worst cases listed in keepwatch CSV with notes)")
    L.append(f"WATCH: {len(watch)} rows | DO-NOT-CUT EARNING: "
             f"{len(earners)} rows (separate CSVs)")
    L.append("")

    # ratify block
    L.append("RATIFY BEFORE EXECUTION (Gilbert):")
    L.append("  1. Window dates per campaign above (esp. any FALLBACK "
             "rows) and the baseline spans.")
    L.append(f"  2. Thresholds: candidacy >= ${MIN_EVIDENCE:.0f} AND "
             f"(>= ${PAUSE_SPEND:.0f} or >= ${PAUSE_SPEND_DAY:.0f}/d); "
             f"PAUSE_TEMP < {PAUSE_ROAS}; DEEP_CUT < {DEEP_ROAS}; "
             f"query floor ${QUERY_FLOOR:.0f}.")
    L.append("  3. INTERPRETATION RULING NEEDED: rows with baseline "
             f"ROAS >= {STRONG_BASE} and window ROAS < {PAUSE_ROAS} are "
             "KEPT in PAUSE_TEMP and flagged SURGE_ONLY_BLEEDER (the "
             "prompt calls them the ideal temporary pauses, so the "
             "'baseline not strongly earning' clause was read as this "
             "flag, not an exclusion). Confirm or flip.")
    L.append("  4. Execution split: DEEP_CUT now; LAG_SENSITIVE "
             "PAUSE_TEMP held for tomorrow's re-run.")
    L.append("  5. tROAS targets read from change events as of window "
             "end: " + ", ".join(
                 f"{camp_sum[c]['label']}={camp_sum[c]['target']:.2f}"
                 for c in sorted(camp_sum, key=lambda c: CAMP_ORDER.index(
                     camp_sum[c]['label']))) +
             " — verify these picked up the 10/1 moves (T1 1.25, "
             "T2 1.20); if the change-events sync hasn't run, they'll "
             "be stale.")
    L.append("  Nothing is executed in the account until ratified.")
    return "\n".join(L)


if __name__ == "__main__":
    main()
