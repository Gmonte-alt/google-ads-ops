#!/usr/bin/env python
"""
geo_exclusion_analysis.py — state-level "what can we afford to cut" for
the late-Sept/Oct Google Home surge (account_id=1). TEMPORARY location
exclusions where we are demonstrably buying leads at negative GP.

Companion to surge_pause_analysis.py (keyword layer) — this is the next
layer. Exclusions don't reset Smart Bidding learning and reverse with
one checkbox; in-house validation: the ~8/12 T1 inventory exit and the
Life DMA-exclusion play.

SCOPE
  Patients: TIER1 (primary), TIER2 (rollback fired). TIER3/TIER4
  computed but report-only unless egregious (standing bleeder at
  <= -$50 GP/day gets surfaced regardless). TEXAS and FLORIDA are
  single-state campaigns — a "state cut" there is a campaign decision:
  excluded from candidacy, report-only. CALIFORNIA is single-state with
  sub-state surgery intent: the DB is state-grain ONLY (no city/DMA
  stored — architect hazard 1), so CA gets a report-only state row here
  plus an optional DMA section fed by a UI export (--ca-geo-csv; the
  exact export recipe is printed in the summary and below).

METHOD — two reads per state x campaign (level-bad vs surge-bad)
  1. SURGE WINDOW read: default 2026-09-26 -> latest complete geo day
     (--window-start/--window-end). Baseline = a pre-surge period that
     EXCLUDES the mid-Sept trough (default 2026-08-25..2026-09-15;
     --baseline-start/--baseline-end), composed same-day-class to the
     window's weekday/weekend mix (surge-charter convention). The
     pause-analysis finding that trough-anchored baselines overstate
     lift is why the baseline is pinned, not trailing.
  2. STANDING read: trailing 60 days ending at window end, EXCLUDING
     window days (DEVIATION from the charter's plain "trailing 60d",
     documented for ratification: including the window would let a
     heavy surge bleed drag the standing read negative and misclassify
     SURGE_ONLY bleeders as STANDING ones).
  Conversion lag (architect hazard 5): every window read ships two
  views — full window and window-minus-last-2-days; a class flip
  between views tags the row LAG_SENSITIVE (same discipline as the
  keyword surge charter: don't execute a LAG_SENSITIVE exclusion on
  the first read; let the morning re-run adjudicate).

CLASSIFICATION (volume gates first: window spend >= $150 AND window
conversions >= 3 for ANY cut verdict; below that WATCH regardless of
ROAS — prior model comparison showed 20-of-35 states flipping tiers
week-to-week at low volume)
  STANDING_BLEEDER    gates met, window GP/day < 0 AND standing
                      GP/day < 0 — strongest exclusion candidates
  SURGE_ONLY_BLEEDER  gates met, standing GP/day >= 0, window GP/day
                      <= -$15 and window ROAS < 0.90 — temporary
                      exclusion, re-enable at wave trough
  NO_BASELINE         baseline-period spend < $50 (state effectively
                      entered at/after the 7/29 re-tier or later) —
                      level read, not marginal; can still be a
                      candidate on level economics, marked explicitly
  WATCH / EARNING     otherwise
  REPORT_ONLY         TX/FL (campaign decision) and the CA state row
                      (surgery is sub-state)

THE "AFFORD" ACCOUNTING (the point)
  Per candidate: GP/day saved, conversions/day forfeited (with a
  +/-1.96*sqrt(n)/days range), implied GP per lead (negative = the $
  we are PAYING to own each lead), share of campaign surge delta,
  cumulative GP/day stopped down the ranked list. Account summary: if
  all candidates post, GP/day stopped vs leads/day lost, per campaign.
  Every candidate row carries the ACCOUNT-COVERAGE-LOSS flag: the 1:1
  state<->campaign structure means excluding a state from its campaign
  removes it from the ACCOUNT entirely.

RECONCILIATION vs prior evidence (ALIGNED / FLIPPED / NEW / RETIRED)
  Priors (Aug 60d margin map + marginal-state reads): T2 PA -$915,
  AZ -$537, IL -$253; T1 DE ($105 CPL / $22 RPL), NJ 0.45-0.48, MA
  0.66; earners T2 GA +$1,830 (but NEGATIVE incremental in both T2
  expansion episodes: -0.36/-0.33), NC +$1,222. Exception watch list:
  MA/PA/AZ/AL/WI/GA.

DATA RULES (architect-pinned)
  - Geo source resolved among campaign_performance_geo_daily_search /
    campaign_performance_geo_daily; account_id directly on the table.
  - DEVICE GRAMMAR IS INVERTED HERE: no device='ALL' rollup — SUM
    across devices, never filter to ALL (the exact opposite of
    kpd/cpd). The script probes device values and refuses to run the
    inverted grammar blindly: if an 'ALL' rollup unexpectedly exists
    it switches to filtering it and WARNS (grammar change -> architect).
  - geo_state holds FULL state names ('California'); geo_state IS NULL
    rows are real non-US/non-state serving — filtered from state grain,
    reported as one line, never bucketed into a state.
  - Metrics: cost_micros/1e6, conversions, conversion_value (native —
    no routed lanes at geo grain; matches the campaign-grain ruling).
    Ratios = summed numerators / summed denominators per cell.
  - Geo totals run BELOW campaign-daily totals by construction
    (Google's unattributed-location slice): the gap is reported beside
    cpd per campaign, never force-reconciled or scaled (hazard 2).
  - Served-geo != targeting config (hazard 3): rows measure where ads
    SERVED; the lever is targeting. Recommendations name state +
    campaign; exclude-vs-remove and presence-vs-interest mechanics are
    an execution note for Gilbert, not a DB claim.
  - State<->campaign uniqueness verified empirically (guard query);
    states served under >1 campaign are SURFACED, never averaged.
    Current tier membership is taken as input where supplied
    (--tier-ledger state,tier CSV from the geo-tiering thread's frozen
    ledger), else the served mapping is used and marked DERIVED.
  - DB read-only (mode=ro); campaigns by immutable id; yesterday
    complete (house lag ruling; the two-view is the mitigation); MST
    serving days.

CA DMA EXPORT RECIPE (for --ca-geo-csv)
  Google Ads UI -> Reports -> Predefined reports -> Geographic ->
  "Geographic report". Filter: Campaign = the CALIFORNIA campaign.
  Rows: Metro area (DMA region) [City also accepted], Day.
  Columns: Cost, Conversions, Conv. value.
  Date range: 2026-08-25 -> today. Download CSV, pass via --ca-geo-csv.
  The script splits it into the same window/baseline reads at DMA
  grain and applies the same gates.

OUTPUT (all framed TEMPORARY — change-log label GEO-EXCL-SURGE-20261002,
re-enable review at the wave trough; exclusions are NOT re-tiering: no
state moves between campaigns, ledger untouched)
  geo_exclusions_<end>.csv   ranked candidates + afford accounting
  geo_all_states_<end>.csv   every state x campaign row, all classes
  geo_ca_dma_<end>.csv       CA DMA section (if export supplied)
  geo_summary_<end>.txt      console summary + RATIFY block

NOTHING POSTS TO THE ACCOUNT FROM THIS SCRIPT. Window dates, volume
gates, and the final exclusion set go back to Gilbert for ratification
after the first real run.

USAGE
  python geo_exclusion_analysis.py
  python geo_exclusion_analysis.py --window-start 2026-09-26
  python geo_exclusion_analysis.py --ca-geo-csv ca_geographic_report.csv
  python geo_exclusion_analysis.py --tier-ledger tiers_v2.csv
"""

import argparse
import csv
import math
import re
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

DEFAULT_DB_PATH = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")
DEFAULT_OUT_DIR = Path(r"C:\Projects\google-ads-ops\analysis"
                       r"\profit-attribution-tracker\output\geo-exclusions")
ACCOUNT_ID = 1
EXCL_LABEL = "GEO-EXCL-SURGE-20261002"

# ── campaigns: immutable ids (NEVER resolve by name) ─────────────────────
PATIENTS = {"23496334752": "TIER1", "23530860942": "TIER2"}
REPORT_TIERS = {"23586637678": "TIER3", "23428389081": "TIER4"}
SINGLE_STATE = {"23394808297": "TEXAS", "23394808300": "FLORIDA",
                "23385188622": "CALIFORNIA"}
CAMPAIGNS = {**PATIENTS, **REPORT_TIERS, **SINGLE_STATE}
CAMP_ORDER = ["TIER1", "TIER2", "TIER3", "TIER4", "CALIFORNIA",
              "TEXAS", "FLORIDA"]
RETIER_DATE = date(2026, 7, 29)          # single-membership cutover
DATA_FLOOR = date(2026, 8, 1)            # Gilbert 10/2: pre-re-tier
                                         # data is a different
                                         # account structure — all
                                         # reads Aug/Sep+ ONLY

# ── thresholds (RATIFY before executing) ─────────────────────────────────
WINDOW_START = date(2026, 9, 26)
BASELINE_START = date(2026, 8, 25)       # pre-surge, excludes mid-Sept
BASELINE_END = date(2026, 9, 15)         # trough (pause-thread finding)
STANDING_DAYS = 60                       # trailing, EXCLUDING window
GATE_SPEND = 150.0                       # window spend gate
GATE_CONV = 3.0                          # window conversions gate
SOB_GP_DAY = -15.0                       # "strongly negative" window GP/d
SOB_ROAS = 0.90
NO_BASELINE_SPEND = 50.0                 # baseline-period spend floor
EGREGIOUS_GP_DAY = -50.0                 # T3/T4 surface-anyway bound
TRIM_DAYS = 2                            # lag view (hazard 5)
GUARD_SPEND = 10.0                       # multi-campaign state guard

# ── reconciliation priors (Aug 60d margin map + marginal reads) ──────────
PRIORS = {
    ("Pennsylvania", "TIER2"): ("BLEEDER", "60d GP -$915"),
    ("Arizona", "TIER2"): ("BLEEDER", "60d GP -$537; marginal -1.35"),
    ("Illinois", "TIER2"): ("BLEEDER", "60d GP -$253"),
    ("Delaware", "TIER1"): ("BLEEDER", "CPL $105 vs RPL $22; "
                                       "adjudicated never load-bearing"),
    ("New Jersey", "TIER1"): ("BLEEDER", "0.45-0.48 ROAS"),
    ("Massachusetts", "TIER1"): ("BLEEDER", "0.66 ROAS"),
    ("Georgia", "TIER2"): ("EARNER*", "+$1,830 at level BUT negative "
                                      "incremental in both T2 "
                                      "expansions (-0.36/-0.33)"),
    ("North Carolina", "TIER2"): ("EARNER", "+$1,222"),
}
WATCH_LIST = {"Massachusetts", "Pennsylvania", "Arizona", "Alabama",
              "Wisconsin", "Georgia"}

GEO_TABLE_CANDIDATES = ["campaign_performance_geo_daily_search",
                        "campaign_performance_geo_daily"]
CPD_TABLE_CANDIDATES = ["campaign_performance_daily_search",
                        "campaign_performance_daily"]
DATE_CANDIDATES = ["date", "report_date"]
REV_CANDIDATES = ["conversion_value", "conversions_value",
                  "primary_conv_value", "all_conv_value"]
CONV_CANDIDATES = ["conversions", "primary_conv_count", "all_conv_count"]


# ── shared helpers (house conventions) ───────────────────────────────────
def connect_ro(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def table_cols(conn, table):
    try:
        return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()


def pdate(s) -> date:
    return datetime.strptime(str(s)[:10], "%Y-%m-%d").date()


def is_weekend(d: date) -> bool:
    return d.weekday() >= 5


def daterange(d0: date, d1: date):
    d = d0
    while d <= d1:
        yield d
        d += timedelta(days=1)


def cid_s(v) -> str:
    s = str(v).strip() if v is not None else "?"
    return s[:-2] if s.endswith(".0") else s


def safe_div(a, b):
    return (a / b) if b else None


def fmt_money(v):
    return f"${v:,.0f}"


def class_counts(dates):
    return {False: sum(1 for d in dates if not is_weekend(d)),
            True: sum(1 for d in dates if is_weekend(d))}


# ── schema resolution ────────────────────────────────────────────────────
def resolve_schema(conn):
    s = {}
    for t in GEO_TABLE_CANDIDATES:
        cols = table_cols(conn, t)
        if cols:
            s["geo_table"] = t
            s["geo_date"] = next((c for c in DATE_CANDIDATES
                                  if c in cols), None)
            s["geo_rev"] = next((c for c in REV_CANDIDATES
                                 if c in cols), None)
            s["geo_conv"] = next((c for c in CONV_CANDIDATES
                                  if c in cols), None)
            s["geo_acct"] = "account_id" in cols
            break
    else:
        sys.exit("No geo table found among %s — architect thread."
                 % GEO_TABLE_CANDIDATES)
    for t in CPD_TABLE_CANDIDATES:
        cols = table_cols(conn, t)
        if cols:
            s["cpd_table"] = t
            s["cpd_date"] = next((c for c in DATE_CANDIDATES
                                  if c in cols), None)
            s["cpd_rev"] = next((c for c in REV_CANDIDATES
                                 if c in cols), None)
            s["cpd_acct"] = "account_id" in cols
            break
    else:
        s["cpd_table"] = None
    # DEVICE GRAMMAR PROBE (hazard: inverted vs kpd/cpd) — checklist item
    try:
        devs = [r[0] for r in conn.execute(
            f"SELECT DISTINCT device FROM {s['geo_table']} LIMIT 10")]
    except sqlite3.Error:
        devs = []
    s["geo_devices"] = devs
    s["geo_has_all"] = "ALL" in devs
    return s


def print_schema(s):
    print(f"  geo table: {s['geo_table']} (date={s['geo_date']}, "
          f"rev={s['geo_rev']}, conv={s['geo_conv']}, "
          f"account_id={'direct' if s['geo_acct'] else 'via join'})")
    if s["geo_has_all"]:
        print("  WARN: geo table UNEXPECTEDLY carries a device='ALL' "
              "rollup — grammar changed; FILTERING to ALL instead of "
              "summing. Flag to the architect thread.")
    else:
        print(f"  geo devices {s['geo_devices']} -> no ALL rollup; "
              "SUMMING across devices (inverted grammar confirmed)")


# ── fetchers ─────────────────────────────────────────────────────────────
def fetch_geo_days(conn, s, d0: date, d1: date):
    """{(cid, state): {date: [spend, rev, conv]}} over [d0, d1]; states
    are full names; NULL geo_state aggregated under key state=None."""
    dev_clause = "AND device = 'ALL'" if s["geo_has_all"] else ""
    acct_clause = "AND account_id = ?" if s["geo_acct"] else ""
    sql = f"""
        SELECT campaign_id AS cid, geo_state AS st,
               {s['geo_date']} AS d, SUM(cost_micros)/1e6 AS spend,
               SUM({s['geo_rev']}) AS rev, SUM({s['geo_conv']}) AS conv
        FROM {s['geo_table']}
        WHERE {s['geo_date']} >= ? AND {s['geo_date']} <= ?
          {dev_clause} {acct_clause}
        GROUP BY 1, 2, 3
    """
    params = [d0.isoformat(), d1.isoformat()]
    if s["geo_acct"]:
        params.append(ACCOUNT_ID)
    out = {}
    for r in conn.execute(sql, params):
        cid = cid_s(r["cid"])
        if cid not in CAMPAIGNS:
            continue
        key = (cid, r["st"])          # st may be None (non-state rows)
        out.setdefault(key, {})[pdate(r["d"])] = [
            r["spend"] or 0.0, r["rev"] or 0.0, r["conv"] or 0.0]
    return out


def fetch_cpd_totals(conn, s, dates):
    """{cid: [spend, rev]} over an explicit date set (gap report)."""
    if not s.get("cpd_table") or not dates:
        return {}
    acct = "AND account_id = ?" if s["cpd_acct"] else ""
    ph = ",".join("?" for _ in dates)
    sql = (f"SELECT campaign_id AS cid, SUM(cost_micros)/1e6 AS spend, "
           f"SUM({s['cpd_rev']}) AS rev FROM {s['cpd_table']} "
           f"WHERE device = 'ALL' {acct} AND {s['cpd_date']} IN ({ph}) "
           f"GROUP BY 1")
    params = ([ACCOUNT_ID] if s["cpd_acct"] else []) + \
        [d.isoformat() for d in dates]
    out = {}
    for r in conn.execute(sql, params):
        cid = cid_s(r["cid"])
        if cid in CAMPAIGNS:
            out[cid] = [r["spend"] or 0.0, r["rev"] or 0.0]
    return out


def first_spend_dates(conn, s):
    """{(cid, state): first date with spend} — NO_BASELINE detection."""
    dev_clause = "AND device = 'ALL'" if s["geo_has_all"] else ""
    acct_clause = "AND account_id = ?" if s["geo_acct"] else ""
    sql = f"""
        SELECT campaign_id AS cid, geo_state AS st,
               MIN({s['geo_date']}) AS d0
        FROM {s['geo_table']}
        WHERE cost_micros > 0 AND geo_state IS NOT NULL
          {dev_clause} {acct_clause}
        GROUP BY 1, 2
    """
    params = [ACCOUNT_ID] if s["geo_acct"] else []
    out = {}
    for r in conn.execute(sql, params):
        cid = cid_s(r["cid"])
        if cid in CAMPAIGNS:
            out[(cid, r["st"])] = pdate(r["d0"])
    return out


def load_tier_ledger(path):
    """Optional state,tier CSV from the geo-tiering thread's frozen
    ledger (hazard 4: membership is input, not derived)."""
    p = Path(path) if path else None
    if not p or not p.exists():
        return {}
    out = {}
    try:
        for r in csv.DictReader(open(p, encoding="utf-8-sig")):
            st = (r.get("state") or r.get("geo_state") or "").strip()
            tier = (r.get("tier") or r.get("campaign") or "").strip()
            if st and tier:
                out[st] = tier
    except Exception as e:
        print(f"  WARN: tier ledger unreadable ({e}) — served mapping "
              "marked DERIVED")
    return out


# ── composition ──────────────────────────────────────────────────────────
def compose(days: dict, dates):
    sp = rv = cv = 0.0
    for d in dates:
        v = days.get(d)
        if v:
            sp += v[0]
            rv += v[1]
            cv += v[2]
    return sp, rv, cv


def expected_spend(days: dict, bdates_by_class: dict, win_counts: dict):
    exp = 0.0
    for cls in (False, True):
        bd = bdates_by_class.get(cls, [])
        rate = (compose(days, bd)[0] / len(bd)) if bd else 0.0
        exp += rate * win_counts.get(cls, 0)
    return exp


def reads(days, win_dates, trim_dates, bdates_by_class, stand_dates):
    n_win = len(win_dates)
    counts = class_counts(win_dates)
    w_sp, w_rv, w_cv = compose(days, win_dates)
    t_sp, t_rv, t_cv = compose(days, trim_dates)
    s_sp, s_rv, s_cv = compose(days, stand_dates)
    b_sp = compose(days, bdates_by_class[False] + bdates_by_class[True])[0]
    exp = expected_spend(days, bdates_by_class, counts)
    n_stand = max(len(stand_dates), 1)
    return {
        "n_win": n_win,
        "w_sp": w_sp, "w_rv": w_rv, "w_cv": w_cv,
        "w_gp": w_rv - w_sp, "w_gp_day": (w_rv - w_sp) / n_win,
        "w_roas": safe_div(w_rv, w_sp), "w_rpl": safe_div(w_rv, w_cv),
        "w_cpl": safe_div(w_sp, w_cv), "w_sp_day": w_sp / n_win,
        "w_cv_day": w_cv / n_win,
        "cv_rng": 1.96 * math.sqrt(max(w_cv, 0.0)) / n_win,
        "d_sp_day": (w_sp - exp) / n_win,
        "t_gp_day": (t_rv - t_sp) / max(len(trim_dates), 1),
        "t_roas": safe_div(t_rv, t_sp), "t_cv": t_cv, "t_sp": t_sp,
        "b_sp": b_sp,
        "s_sp_day": s_sp / n_stand, "s_gp_day": (s_rv - s_sp) / n_stand,
        "s_roas": safe_div(s_rv, s_sp),
    }


# ── classification ───────────────────────────────────────────────────────
def geo_class(gates, w_sp, w_cv, w_gp_day, w_roas, s_gp_day,
              no_baseline):
    """(class, note) for one view. Gates are assessed ONCE on the full
    window and shared by both views (the trim view exists to read lag
    direction, not to re-test evidence volume); NO_BASELINE marks level
    reads; single-state/report scoping handled by the caller."""
    bleeding = w_gp_day < 0
    if not gates:
        if bleeding and w_sp >= GATE_SPEND and w_cv < 1:
            return "WATCH", ("ZERO_YIELD: >= gate spend with ~zero "
                             "conversions — charter gates say WATCH "
                             "(could be small-n luck); surfaced for "
                             "ratification, standing read is the "
                             "redemption check")
        if bleeding:
            return "WATCH", (f"below volume gates (${GATE_SPEND:.0f} & "
                             f"{GATE_CONV:.0f} conv) — noisy at this "
                             "volume, no cut verdict")
        return "EARNING" if w_gp_day >= 0 else "WATCH", ""
    if no_baseline:
        if bleeding and s_gp_day < 0:
            return "NO_BASELINE", ("LEVEL READ (thin pre-history, "
                                   "entered at/after 7/29 re-tier): "
                                   "negative GP both reads — candidate "
                                   "on level economics only")
        return "NO_BASELINE", "LEVEL READ — not bleeding on both reads"
    if bleeding and s_gp_day < 0:
        return "STANDING_BLEEDER", "negative GP/day in BOTH reads"
    if (s_gp_day >= 0 and w_gp_day <= SOB_GP_DAY
            and w_roas is not None and w_roas < SOB_ROAS):
        return "SURGE_ONLY_BLEEDER", ("good at rest, bleeding in "
                                      "expansion — temporary exclusion, "
                                      "re-enable at wave trough")
    if bleeding:
        return "WATCH", ("window-negative but standing-positive below "
                         "the SOB bound — not strong enough to cut")
    if w_roas is not None and w_roas < 1.0:
        return "WATCH", "window ROAS under break-even, GP/day >= 0"
    return "EARNING", ""


def reconcile(state, camp_label, cls):
    prior = PRIORS.get((state, camp_label))
    is_cut = cls in ("STANDING_BLEEDER", "SURGE_ONLY_BLEEDER",
                     "NO_BASELINE")
    if not prior:
        return ("NEW" if is_cut else ""), ""
    kind, note = prior
    if kind == "BLEEDER":
        return ("ALIGNED" if is_cut else "FLIPPED(recovered)"), note
    # prior earner
    return ("FLIPPED(degraded)" if is_cut else "ALIGNED"), note


# ── CA DMA section (UI export ingest) ────────────────────────────────────
def load_ca_csv(path):
    """Flexible-header parse of the UI geographic report. Returns
    {dma: {date: [spend, rev, conv]}} or None."""
    p = Path(path) if path else None
    if not p or not p.exists():
        return None

    def pick(fieldnames, cands):
        low = {f.lower().strip(): f for f in fieldnames}
        for c in cands:
            for k, orig in low.items():
                if c in k:
                    return orig
        return None

    def num(v):
        try:
            return float(re.sub(r"[^0-9.\-]", "", str(v)) or 0)
        except ValueError:
            return 0.0

    out = {}
    try:
        rows = list(csv.DictReader(open(p, encoding="utf-8-sig")))
        if not rows:
            return None
        fn = rows[0].keys()
        c_dma = pick(fn, ["metro", "dma", "city",
                          "matched location", "region"])
        c_day = pick(fn, ["day", "date"])
        c_cost = pick(fn, ["cost"])
        c_conv = pick(fn, ["conversions", "conv."])
        c_val = pick(fn, ["conv. value", "conv value", "value"])
        if not (c_dma and c_day and c_cost):
            print(f"  WARN: CA export columns not recognized "
                  f"({list(fn)}) — section skipped")
            return None
        for r in rows:
            dma = (r.get(c_dma) or "").strip()
            if not dma or dma.lower().startswith("total"):
                continue
            try:
                d = pdate(r.get(c_day))
            except ValueError:
                continue
            cell = out.setdefault(dma, {}).setdefault(d, [0.0, 0.0, 0.0])
            cell[0] += num(r.get(c_cost))
            cell[1] += num(r.get(c_val)) if c_val else 0.0
            cell[2] += num(r.get(c_conv)) if c_conv else 0.0
    except Exception as e:
        print(f"  WARN: CA export unreadable ({e}) — section skipped")
        return None
    return out


# ── output ───────────────────────────────────────────────────────────────
FIELDS = ["rank", "scope", "class", "lag_sensitive", "state", "campaign",
          "campaign_id", "tier_source", "win_spend_day", "win_roas",
          "win_roas_trim2", "win_rpl", "win_cpl", "win_gp_day",
          "stand_gp_day", "stand_roas", "delta_spend_day",
          "share_surge_delta", "conv_day_forfeit", "conv_day_range",
          "gp_per_lead", "gp_day_saved", "cum_gp_day_saved",
          "reconciliation", "prior_note", "watch_list",
          "coverage_loss", "excl_label", "note"]


def write_csv(path, rows, fields=FIELDS):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def parse_args():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    p.add_argument("--db", default=str(DEFAULT_DB_PATH))
    p.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    p.add_argument("--window-start", default=WINDOW_START.isoformat())
    p.add_argument("--window-end", default=None,
                   help="default: latest complete geo day")
    p.add_argument("--baseline-start",
                   default=BASELINE_START.isoformat())
    p.add_argument("--baseline-end", default=BASELINE_END.isoformat())
    p.add_argument("--standing-days", type=int, default=STANDING_DAYS)
    p.add_argument("--data-floor", default=DATA_FLOOR.isoformat(),
                   help="hard floor on ALL reads (post-re-tier "
                        "ruling); standing walk-back stops here")
    p.add_argument("--ca-geo-csv", default=None,
                   help="CALIFORNIA UI geographic report (DMA/city "
                        "grain) — see recipe in the docstring")
    p.add_argument("--tier-ledger", default=None,
                   help="optional state,tier CSV from the frozen "
                        "re-tier ledger (else served mapping, DERIVED)")
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

    # dates — HARD FLOOR: nothing before args.data_floor is read
    # (7/29 re-tier makes earlier data a different account)
    fetch_from = pdate(args.data_floor)
    geo_latest_row = conn.execute(
        f"SELECT MAX({s['geo_date']}) FROM {s['geo_table']}"
        + (" WHERE account_id = ?" if s["geo_acct"] else ""),
        ([ACCOUNT_ID] if s["geo_acct"] else [])).fetchone()
    latest = pdate(geo_latest_row[0]) if geo_latest_row[0] else \
        date.today() - timedelta(days=1)
    if args.window_end:
        latest = pdate(args.window_end)
    w0 = pdate(args.window_start)
    b0, b1 = pdate(args.baseline_start), pdate(args.baseline_end)
    win_dates = sorted(daterange(w0, latest))
    trim_dates = win_dates[:-TRIM_DAYS] if len(win_dates) > TRIM_DAYS \
        else win_dates[:1]
    bdates = {False: [d for d in daterange(b0, b1) if not is_weekend(d)],
              True: [d for d in daterange(b0, b1) if is_weekend(d)]}
    win_set = set(win_dates)
    stand_dates = []
    d = latest
    while len(stand_dates) < args.standing_days and d >= fetch_from:
        if d not in win_set:
            stand_dates.append(d)
        d -= timedelta(days=1)
    print(f"  window {w0} -> {latest} ({len(win_dates)}d) | baseline "
          f"{b0}..{b1} ({len(bdates[False])}wd/{len(bdates[True])}we) | "
          f"standing {len(stand_dates)}d ex-window | DATA FLOOR "
          f"{fetch_from} (post-re-tier only)")

    t = time.time()
    geo = fetch_geo_days(conn, s, fetch_from, latest)
    print(f"  geo fetch: {len(geo):,} state x campaign cells in "
          f"{time.time() - t:.1f}s")
    cpd_win = fetch_cpd_totals(conn, s, win_dates)
    conn.close()

    ledger = load_tier_ledger(args.tier_ledger)
    tier_src = "LEDGER" if ledger else "DERIVED(served)"

    # non-state (NULL geo_state) line + uniqueness guard
    null_sp = sum(compose(days, win_dates)[0]
                  for (cid, st), days in geo.items() if st is None)
    state_camps = {}
    for (cid, st), days in geo.items():
        if st is None:
            continue
        if compose(days, win_dates)[0] >= GUARD_SPEND:
            state_camps.setdefault(st, set()).add(CAMPAIGNS[cid])
    multi = {st: sorted(cs) for st, cs in state_camps.items()
             if len(cs) > 1}

    # per-campaign surge delta (geo-based, for share calc)
    camp_delta = {}
    for (cid, st), days in geo.items():
        if st is None:
            continue
        counts = class_counts(win_dates)
        d_day = (compose(days, win_dates)[0]
                 - expected_spend(days, bdates, counts)) / len(win_dates)
        camp_delta[cid] = camp_delta.get(cid, 0.0) + d_day

    # ── rows ─────────────────────────────────────────────────────────
    all_rows = []
    for (cid, st), days in sorted(geo.items(),
                                  key=lambda kv: (kv[0][0],
                                                  kv[0][1] or "")):
        if st is None:
            continue
        label = CAMPAIGNS[cid]
        r = reads(days, win_dates, trim_dates, bdates, stand_dates)
        if r["w_sp"] <= 0 and r["s_sp_day"] <= 0:
            continue
        # whole dataset is post-re-tier by construction (data
        # floor), so NO_BASELINE = thin baseline-period presence
        no_base = r["b_sp"] < NO_BASELINE_SPEND
        gates = (r["w_sp"] >= GATE_SPEND and r["w_cv"] >= GATE_CONV)
        cls, note = geo_class(gates, r["w_sp"], r["w_cv"],
                              r["w_gp_day"], r["w_roas"], r["s_gp_day"],
                              no_base)
        cls_t, _ = geo_class(gates, r["w_sp"], r["w_cv"], r["t_gp_day"],
                             r["t_roas"], r["s_gp_day"], no_base)
        lag = "Y" if cls != cls_t else ""
        scope = ("PATIENT" if cid in PATIENTS else
                 "REPORT_ONLY_TIER" if cid in REPORT_TIERS else
                 "REPORT_ONLY_SINGLE_STATE")
        if cid in SINGLE_STATE:
            cut = cls in ("STANDING_BLEEDER", "SURGE_ONLY_BLEEDER",
                          "NO_BASELINE")
            extra = ("state cut here = CAMPAIGN decision"
                     if label in ("TEXAS", "FLORIDA")
                     else "CA surgery is sub-state (see DMA section)")
            note = (f"{extra}; would-be class {cls}" if cut else extra)
            cls = "REPORT_ONLY"
        rec, prior_note = reconcile(st, label, cls)
        share = safe_div(r["d_sp_day"], camp_delta.get(cid)) \
            if camp_delta.get(cid, 0) > 0 else None
        all_rows.append({
            "scope": scope, "class": cls, "lag_sensitive": lag,
            "state": st, "campaign": label, "campaign_id": cid,
            "tier_source": (ledger.get(st, "?") if ledger else tier_src),
            "win_spend_day": round(r["w_sp_day"], 2),
            "win_roas": (round(r["w_roas"], 3)
                         if r["w_roas"] is not None else ""),
            "win_roas_trim2": (round(r["t_roas"], 3)
                               if r["t_roas"] is not None else ""),
            "win_rpl": (round(r["w_rpl"], 2)
                        if r["w_rpl"] is not None else ""),
            "win_cpl": (round(r["w_cpl"], 2)
                        if r["w_cpl"] is not None else ""),
            "win_gp_day": round(r["w_gp_day"], 2),
            "stand_gp_day": round(r["s_gp_day"], 2),
            "stand_roas": (round(r["s_roas"], 3)
                           if r["s_roas"] is not None else ""),
            "delta_spend_day": round(r["d_sp_day"], 2),
            "share_surge_delta": (round(share, 3)
                                  if share is not None else ""),
            "conv_day_forfeit": round(r["w_cv_day"], 2),
            "conv_day_range": (f"{max(r['w_cv_day'] - r['cv_rng'], 0):.2f}"
                               f"-{r['w_cv_day'] + r['cv_rng']:.2f}"),
            "gp_per_lead": (round(safe_div(r["w_gp"], r["w_cv"]), 2)
                            if r["w_cv"] else ""),
            "gp_day_saved": round(-r["w_gp_day"], 2),
            "reconciliation": rec, "prior_note": prior_note,
            "watch_list": "Y" if st in WATCH_LIST else "",
            "coverage_loss": "1:1 structure — exclusion removes state "
                             "from the ACCOUNT",
            "excl_label": EXCL_LABEL, "note": note,
        })

    cut_classes = ("STANDING_BLEEDER", "SURGE_ONLY_BLEEDER",
                   "NO_BASELINE")
    cls_rank = {c: i for i, c in enumerate(cut_classes)}
    candidates = sorted(
        [r for r in all_rows if r["class"] in cut_classes],
        key=lambda r: (cls_rank[r["class"]], -r["gp_day_saved"]))
    cum = 0.0
    for i, r in enumerate(candidates, 1):
        cum += r["gp_day_saved"]
        r["rank"] = i
        r["cum_gp_day_saved"] = round(cum, 2)
    egregious = [r for r in candidates
                 if r["scope"] == "REPORT_ONLY_TIER"
                 and r["class"] == "STANDING_BLEEDER"
                 and r["win_gp_day"] <= EGREGIOUS_GP_DAY]

    # CA DMA section
    ca_rows = []
    ca = load_ca_csv(args.ca_geo_csv)
    if ca:
        for dma, days in sorted(ca.items()):
            r = reads(days, win_dates, trim_dates, bdates, stand_dates)
            if r["w_sp"] <= 0:
                continue
            no_base = r["b_sp"] < NO_BASELINE_SPEND
            gates = (r["w_sp"] >= GATE_SPEND and r["w_cv"] >= GATE_CONV)
            cls, note = geo_class(gates, r["w_sp"], r["w_cv"],
                                  r["w_gp_day"], r["w_roas"],
                                  r["s_gp_day"], no_base)
            ca_rows.append({
                "rank": "", "scope": "CA_DMA", "class": cls,
                "lag_sensitive": "", "state": dma,
                "campaign": "CALIFORNIA",
                "campaign_id": "23385188622", "tier_source": "UI export",
                "win_spend_day": round(r["w_sp_day"], 2),
                "win_roas": (round(r["w_roas"], 3)
                             if r["w_roas"] is not None else ""),
                "win_roas_trim2": (round(r["t_roas"], 3)
                                   if r["t_roas"] is not None else ""),
                "win_rpl": "", "win_cpl": "",
                "win_gp_day": round(r["w_gp_day"], 2),
                "stand_gp_day": round(r["s_gp_day"], 2),
                "stand_roas": "", "delta_spend_day":
                    round(r["d_sp_day"], 2),
                "share_surge_delta": "",
                "conv_day_forfeit": round(r["w_cv_day"], 2),
                "conv_day_range": "", "gp_per_lead":
                    (round(safe_div(r["w_gp"], r["w_cv"]), 2)
                     if r["w_cv"] else ""),
                "gp_day_saved": round(-r["w_gp_day"], 2),
                "cum_gp_day_saved": "", "reconciliation": "",
                "prior_note": "", "watch_list": "",
                "coverage_loss": "sub-state exclusion inside the CA "
                                 "campaign", "excl_label": EXCL_LABEL,
                "note": note + (" [UI-export grain; lag view only "
                                "where dates allow]"),
            })
        ca_rows.sort(key=lambda r: -r["gp_day_saved"])

    # ── write + print ────────────────────────────────────────────────
    stamp = latest.isoformat()
    f_cand = out_dir / f"geo_exclusions_{stamp}.csv"
    f_all = out_dir / f"geo_all_states_{stamp}.csv"
    f_sum = out_dir / f"geo_summary_{stamp}.txt"
    write_csv(f_cand, candidates)
    write_csv(f_all, sorted(all_rows, key=lambda r: (
        CAMP_ORDER.index(r["campaign"]), -r["win_spend_day"])))
    files = [f_cand, f_all, f_sum]
    if ca_rows:
        f_ca = out_dir / f"geo_ca_dma_{stamp}.csv"
        write_csv(f_ca, ca_rows)
        files.insert(2, f_ca)

    text = summarize(all_rows, candidates, egregious, ca_rows, camp_delta,
                     cpd_win, geo, multi, null_sp, win_dates, bdates,
                     stand_dates, tier_src, args, latest, w0)
    f_sum.write_text(text, encoding="utf-8")
    print()
    print(text)
    print()
    for f in files:
        print(f"  wrote {f}")
    print(f"  total {time.time() - t0:.1f}s")


def summarize(all_rows, candidates, egregious, ca_rows, camp_delta,
              cpd_win, geo, multi, null_sp, win_dates, bdates,
              stand_dates, tier_src, args, latest, w0):
    L = []
    n_win = len(win_dates)
    L.append(f"GEO EXCLUSION ANALYSIS — Google Home (acct 1), window "
             f"{w0} -> {latest} ({n_win}d, MST serving days)")
    L.append(f"ALL RECOMMENDATIONS TEMPORARY — change-log label "
             f"{EXCL_LABEL}, re-enable review at the wave trough. "
             "Exclusions are NOT re-tiering (no state moves, ledger "
             "untouched). NOTHING POSTS from this script.")
    L.append("STATE GRAIN ONLY: the DB stores no city/DMA rows — "
             "within-CA surgery requires the UI export (recipe in "
             "script docstring)"
             + ("; CA section BUILT from supplied export." if ca_rows
                else "; no export supplied, CA state row report-only."))
    L.append(f"  baseline {args.baseline_start}..{args.baseline_end} "
             f"(pre-surge, mid-Sept trough excluded); standing "
             f"{len(stand_dates)}d EX-WINDOW (deviation, see RATIFY); "
             f"DATA FLOOR {args.data_floor} — pre-re-tier data excluded "
             f"from EVERY read (Gilbert 10/2); tier membership source: "
             f"{tier_src}")
    L.append("")

    # guard + gap
    if multi:
        L.append("STATE<->CAMPAIGN UNIQUENESS GUARD — states serving "
                 "under MULTIPLE campaigns in-window (surfaced, never "
                 "averaged):")
        for st, cs in sorted(multi.items()):
            L.append(f"  {st}: {', '.join(cs)}")
    else:
        L.append("STATE<->CAMPAIGN UNIQUENESS GUARD: clean — no state "
                 "served under multiple campaigns at >= "
                 f"${GUARD_SPEND:.0f} window spend.")
    L.append(f"Non-state serving (geo_state NULL): "
             f"{fmt_money(null_sp)} window spend — excluded from state "
             "grain, never bucketed into a state.")
    L.append("GEO vs CAMPAIGN-DAILY GAP (unattributed-location slice; "
             "reported, never reconciled):")
    for cid in sorted(cpd_win, key=lambda c:
                      CAMP_ORDER.index(CAMPAIGNS[c])):
        g_sp = sum(compose(days, win_dates)[0]
                   for (c2, st), days in geo.items() if c2 == cid)
        c_sp = cpd_win[cid][0]
        gap = (1 - g_sp / c_sp) if c_sp > 0 else 0
        L.append(f"  {CAMPAIGNS[cid]:<11} geo {fmt_money(g_sp)} vs cpd "
                 f"{fmt_money(c_sp)}  gap {gap:.1%}")
    L.append("")

    # candidates
    n_lag = sum(1 for r in candidates if r["lag_sensitive"])
    L.append(f"RANKED EXCLUSION CANDIDATES — {len(candidates)} rows "
             f"({sum(1 for r in candidates if r['class'] == 'STANDING_BLEEDER')} "
             f"STANDING, "
             f"{sum(1 for r in candidates if r['class'] == 'SURGE_ONLY_BLEEDER')} "
             f"SURGE_ONLY, "
             f"{sum(1 for r in candidates if r['class'] == 'NO_BASELINE')} "
             f"NO_BASELINE); {n_lag} LAG_SENSITIVE (hold for the "
             "morning re-run)")
    L.append("  EVERY exclusion removes the state from the ACCOUNT "
             "(1:1 structure). Served-geo measures damage; the lever "
             "is targeting — exclude-vs-remove / presence-vs-interest "
             "mechanics are an execution note for Gilbert.")
    L.append(f"  {'#':>3} {'class':<19} {'state':<15.15} {'camp':<7.7} "
             f"{'$/d':>6} {'ROAS':>5} {'trim':>5} {'60d GP/d':>8} "
             f"{'GP/d':>6} {'GP/lead':>7} {'conv/d':>6} {'save/d':>6} "
             f"{'cum':>6}  recon/flags")
    for r in candidates[:args.lines_cap]:
        flags = ",".join(x for x in (
            "LAG" if r["lag_sensitive"] else "",
            r["reconciliation"], "WL" if r["watch_list"] else "",
            r["scope"] if r["scope"] != "PATIENT" else "") if x)
        L.append(f"  {r['rank']:>3} {r['class']:<19} {r['state']:<15.15} "
                 f"{r['campaign']:<7.7} {r['win_spend_day']:>6,.0f} "
                 f"{(r['win_roas'] if r['win_roas'] != '' else 0):>5.2f} "
                 f"{(r['win_roas_trim2'] if r['win_roas_trim2'] != '' else 0):>5.2f} "
                 f"{r['stand_gp_day']:>8,.0f} {r['win_gp_day']:>6,.0f} "
                 f"{(r['gp_per_lead'] if r['gp_per_lead'] != '' else 0):>7,.0f} "
                 f"{r['conv_day_forfeit']:>6.1f} "
                 f"{r['gp_day_saved']:>6,.0f} "
                 f"{r['cum_gp_day_saved']:>6,.0f}  {flags}")
    if len(candidates) > args.lines_cap:
        L.append(f"  ... {len(candidates) - args.lines_cap} more in CSV")
    L.append("")

    # afford accounting per campaign
    L.append("THE AFFORD ACCOUNTING — if every candidate posts:")
    by_camp = {}
    for r in candidates:
        a = by_camp.setdefault(r["campaign"], [0.0, 0.0, 0])
        a[0] += r["gp_day_saved"]
        a[1] += r["conv_day_forfeit"]
        a[2] += 1
    for lbl in CAMP_ORDER:
        if lbl in by_camp:
            a = by_camp[lbl]
            delta = next((camp_delta[c] for c, l2 in CAMPAIGNS.items()
                          if l2 == lbl and c in camp_delta), 0.0)
            L.append(f"  {lbl:<11} {a[2]} exclusions -> GP/day stopped "
                     f"{fmt_money(a[0])}, leads/day forfeited "
                     f"{a[1]:.1f}; campaign surge Δ "
                     f"{fmt_money(delta)}/d")
    tot = [sum(a[0] for a in by_camp.values()),
           sum(a[1] for a in by_camp.values())]
    L.append(f"  ACCOUNT     GP/day stopped {fmt_money(tot[0])}, "
             f"leads/day forfeited {tot[1]:.1f} — every forfeited lead "
             "is one we are currently PAYING to acquire (GP/lead "
             "column)")
    if egregious:
        L.append("  EGREGIOUS (report-only tiers, standing bleeder <= "
                 f"{fmt_money(EGREGIOUS_GP_DAY)}/d — surfaced per "
                 "charter): " + ", ".join(
                     f"{r['state']}/{r['campaign']} "
                     f"({fmt_money(r['win_gp_day'])}/d)"
                     for r in egregious))
    L.append("")

    # single-state + CA
    L.append("SINGLE-STATE CAMPAIGNS (report-only):")
    for r in all_rows:
        if r["class"] == "REPORT_ONLY":
            L.append(f"  {r['campaign']:<11} {r['state']:<15.15} "
                     f"win ROAS "
                     f"{(r['win_roas'] if r['win_roas'] != '' else 0):.2f} "
                     f" GP/d {r['win_gp_day']:+,.0f}  60d GP/d "
                     f"{r['stand_gp_day']:+,.0f}  — {r['note']}")
    if ca_rows:
        L.append("")
        L.append(f"CALIFORNIA DMA SECTION (UI export, {len(ca_rows)} "
                 "DMAs) — top bleeders:")
        for r in [x for x in ca_rows if x["win_gp_day"] < 0][:10]:
            L.append(f"  {r['state']:<28.28} $/d "
                     f"{r['win_spend_day']:>6,.0f}  ROAS "
                     f"{(r['win_roas'] if r['win_roas'] != '' else 0):.2f}"
                     f"  GP/d {r['win_gp_day']:>+6,.0f}  {r['class']}")
    L.append("")

    # ratify
    L.append("RATIFY BEFORE EXECUTION (Gilbert):")
    L.append(f"  1. Window {w0} -> {latest}; baseline "
             f"{args.baseline_start}..{args.baseline_end}; standing "
             f"read computed EX-WINDOW (charter said plain trailing "
             "60d — deviation keeps SURGE_ONLY vs STANDING separation "
             f"honest; confirm or flip) and floored at "
             f"{args.data_floor}, so it holds up to ~56 post-re-tier "
             "days, not a full 60.")
    L.append(f"  2. Volume gates: >= ${GATE_SPEND:.0f} window spend AND "
             f">= {GATE_CONV:.0f} window conversions for any cut "
             f"verdict; SOB bound: window GP/day <= "
             f"{fmt_money(SOB_GP_DAY)} and ROAS < {SOB_ROAS}. NOTE the "
             "conversion gate means a ZERO-conversion state can never "
             "be cut as chartered — such rows are flagged ZERO_YIELD "
             "in WATCH; rule on them explicitly.")
    L.append("  3. The final exclusion set, row by row — LAG_SENSITIVE "
             "rows held for the morning re-run regardless.")
    L.append("  4. Execution mechanics per row (exclude vs remove "
             "targeting; presence vs presence-or-interest) — targeting "
             "lever, Gilbert's note, not a DB claim.")
    L.append("  Nothing posts to the account from this thread.")
    return "\n".join(L)


if __name__ == "__main__":
    main()
