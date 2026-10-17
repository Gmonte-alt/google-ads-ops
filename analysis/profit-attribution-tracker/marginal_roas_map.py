#!/usr/bin/env python
r"""
marginal_roas_map.py — Marginal ROAS map: which parts of the Google Home
structure absorb incremental spend at >=1.0 incremental ROAS, and which
parts structurally drag expansions down.

MEASUREMENT ONLY. This script recommends no cuts, caps, or target
changes. It measures what the bidding controller's own expansion
episodes earned at the margin, segment by segment. Interpretation note
carried into every output: this is the machine's CHOSEN margin — what
incremental dollars earned as currently bought, under current targets
and QS — not a structure-invariant curve. Numbers should be expected to
shift after future cuts or QS work; that is precisely why this is built
as a rerunnable before/after instrument (rerun after each cycle closes).

ESTIMATOR (primary) — arc-incremental ROAS per segment per episode:
    inc_ROAS = (rev_window - rev_expected) / (spend_window - spend_expected)
  where the expansion window is detected per campaign (below) and the
  expected values are composed from the window's actual day-class mix
  (weekday/weekend) using per-class baseline means — the
  carrier_tripwire / expansion_cut_list convention, so weekday/weekend
  mixing never manufactures a delta. Pooled estimate across episodes =
  ratio of sums of deltas (house rule: ROAS always ratio-of-sums).

ESTIMATOR (cross-check) — per-segment log-log elasticity on post-cutover
  daily data: ln(rev+1) ~ ln(spend), days with spend >= $20;
  marginal_ROAS ~= b * average_ROAS over the fit days. Reported beside
  the arc estimate with a DISAGREE flag when both exist and differ by
  > 0.25; the two are never averaged.

PROPOSED CONSTRUCTIONS (ratify or edit; all CLI-tunable)
────────────────────────────────────────────────────────
1. Expansion window (peak-centered above-baseline stretch):
   Within each trough pair (t0, t1), baseline = per-day-class mean of
   campaign daily spend over the trailing --baseline-days (14) same-class
   days ending ON t0. Find the peak spend day in (t0, t1); the window is
   the maximal contiguous run of days containing the peak where daily
   spend >= --expansion-mult (1.25) x that day's class baseline. Episodes
   whose window starts before --cutover (2026-07-01) are dropped
   (Feb–Mar era = demand surges per Analysis 4/5 — would bias the map
   upward). The open interval after a campaign's last trough is searched
   the same way through the latest data date; if its window reaches the
   data edge it is flagged OPEN (still running) — include/exclude via
   --exclude-open (default: included, flagged).
2. Segment baselines: anchored at the same t0, same convention, per
   segment (and per state for the companion). Baseline ROAS reported =
   expected_rev / expected_spend over the window's day-class mix.
3. Minimum spend-delta threshold (segment-episode qualifies for the
   pooled estimate only if):
       dspend >= --min-delta-abs ($250 over the window)  AND
       dspend >= --min-delta-rel (15%) x expected window spend
   Below-threshold cells are marked BELOW-THRESHOLD and excluded from
   pooling; a segment with zero qualifying episodes = UNTESTED ("no
   evidence", not "bad") — the UNTESTED list is its own deliverable.
   Segments that CONTRACTED during a campaign expansion are marked
   NO-EXPANSION (also non-qualifying).
4. T1 contamination: episodes whose window intersects the ~8/12 target
   step ±(3 pre / 14 post) days (post-step ringing ~12d per wave
   calibration) are flagged CONTAMINATED, excluded from pooling, and
   reported separately.

CLASSES (pooled across qualifying episodes):
   EXPANDABLE  pooled >= 1.00 and consistent
   DRAG        pooled <  0.85 and consistent
   MIXED       pooled in [0.85, 1.00) or inconsistent
   UNTESTED    zero qualifying episodes
   Inconsistent = at least one qualifying episode >= 1.00 AND at least
   one < 0.85. Per-episode range always shown.

GRAIN:
   Primary: carrier segment x campaign — CARRIER_PATTERNS verbatim from
   carrier_tripwire.py (9 carriers + Other/generic, first-hit top-down)
   so results reconcile with daily monitoring. The two Farmers flagship
   broad keywords (TIER2, TIER3) are broken out as their own rows and
   EXCLUDED from those campaigns' Farmers rows (labelled "Farmers excl.
   flagship") to avoid double counting; auto-detected as the top-spend
   post-cutover BROAD Farmers keyword without a home/homeowner/house
   token, overridable via --flagship "TIER2=farmers insurance". The
   ACCOUNT pseudo-campaign row aggregates the resolved campaigns at base
   segment grain (flagship folded back into Farmers there, so the
   account row stays canonical vs the tripwire).
   Mobile: cross-cutting FLAG only (window mobile spend-weight per
   segment), never a segmentation axis (prior analysis: mobile doesn't
   discriminate winners from losers at term level).
   Companion: state grain for TIER2 and TIER1 from the geo table —
   devices SUMMED (no ALL rollup there; if an ALL rollup is detected the
   script uses it instead and says so).

EPISODE POOL — trough dates from the cycle instrument (per campaign;
post-cutover controller-echo expansions only):
   ACCOUNT     Jun 18, Aug 02
   TIER2       Jun 18, Aug 12
   TEXAS       May 23, Jun 12, Jul 29
   TIER3       May 23, Jun 19, Jul 07, Aug 03
   TIER1       Jun 24, Jul 30, Aug 14   (8/12 step -> contamination rule)
   CALIFORNIA  Jun 12, Jul 29
If the currently-running expansion crests before a rerun, the open
episode closes on its own and enters the pool automatically.

DATA RULES (house):
   DB read-only (mode=ro); never during a sync. Segment spend/revenue:
   keyword_performance_daily p JOIN keyword_instances ki ON
   ki.id = p.instance_id AND ki.account_id = 1 (kpd has NO account_id —
   scope through the join; kpd is structurally Search-only). Carrier
   rows device='ALL'; mobile weight from device='MOBILE'. Campaign
   linkage = ki.campaign_id if present, else ad_groups fallback
   (tripwire convention). Campaign daily totals from
   campaign_performance_daily_search (device='ALL'); kpd-sum fallback
   with a warning if the view is absent. Revenue column auto-detected
   (REV_CANDIDATES). Data treated as complete — no conversion-lag
   exclusion (house ruling 8/20). Break-even = 1.00.

CAMPAIGN RESOLUTION: paste the charter campaign ids into CAMPAIGN_IDS
below (same ids wave_calibration.py pins) or pass --map "TIER2=123..."
(repeatable). Name-hint fallback exists but prints a loud warning —
resolve by immutable campaign_id, never by mutable name (cardinal rule).

OUTPUTS (to --out-dir):
   marginal_roas_map_{stamp}.csv     the map (segment x campaign)
   episodes_{stamp}.csv              full segment x episode audit grain
   state_companion_{stamp}.csv       TIER2/TIER1 state grain
   marginal_roas_map_{stamp}.txt     readable summary (+ latest.txt):
       proposals, episode inventory, ranked map, UNTESTED list,
       cut-list reconciliation, closing paragraph with the computed
       "where can we grow / what fraction went below 0.85" numbers.

USAGE:
   python marginal_roas_map.py
   python marginal_roas_map.py --map "TIER2=<real_campaign_id>" (repeatable)
   python marginal_roas_map.py --expansion-mult 1.25 --min-delta-abs 250
   python marginal_roas_map.py --cutlist-csv path\to\cuts_keywords.csv
"""

import argparse
import csv
import math
import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

DEFAULT_DB_PATH = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")
DEFAULT_OUT_DIR = Path(r"C:\Projects\google-ads-ops\analysis"
                       r"\profit-attribution-tracker\output"
                       r"\marginal-roas-map")
DEFAULT_CUTLIST_DIR = Path(r"C:\Projects\google-ads-ops\analysis"
                           r"\profit-attribution-tracker\output"
                           r"\expansion-cuts")
ACCOUNT_ID = 1
BREAK_EVEN = 1.00
ACCOUNT_LABEL = "ACCOUNT"

# Paste the charter campaign ids here (same ids wave_calibration.py pins).
# Short name -> campaign_id (int). Leave empty to use --map / name hints.
CAMPAIGN_IDS = {
    # "TIER1": 0, "TIER2": 0, "TIER3": 0, "TIER4": 0,
    # "TEXAS": 0, "CALIFORNIA": 0,
}

# order = display preference for ties; matching is first-hit top-down
# (verbatim from carrier_tripwire.py)
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
BASE_SEGMENTS = [label for label, _ in CARRIER_PATTERNS] + [OTHER_LABEL]

REV_CANDIDATES = ["conversion_value", "conversions_value", "conv_value",
                  "primary_conv_value", "total_conv_value",
                  "all_conv_value"]
SPEND_CANDIDATES = ["cost_micros", "spend", "cost"]
DATE_CANDIDATES = ["report_date", "date"]
STATE_CANDIDATES = ["geo_state", "state", "state_code", "region",
                    "geo_region"]
GEO_TABLES = ["campaign_performance_geo_daily_search",
              "campaign_performance_geo_daily"]
CAMP_DAILY_TABLES = ["campaign_performance_daily_search",
                     "campaign_performance_daily"]
CAMP_TABLES = ["campaigns_search", "campaigns"]

HOME_TOKENS = ("home", "homeowner", "homeowners", "house")

# Trough dates from the cycle instrument (per campaign)
TROUGHS = {
    ACCOUNT_LABEL: ["2026-06-18", "2026-08-02"],
    "TIER2":       ["2026-06-18", "2026-08-12"],
    "TEXAS":       ["2026-05-23", "2026-06-12", "2026-07-29"],
    "TIER3":       ["2026-05-23", "2026-06-19", "2026-07-07",
                    "2026-08-03"],
    "TIER1":       ["2026-06-24", "2026-07-30", "2026-08-14"],
    "CALIFORNIA":  ["2026-06-12", "2026-07-29"],
}
# campaign -> [(event_date, label, days_before, days_after)]
CONTAMINATION = {
    "TIER1": [("2026-08-12", "tROAS floor step ~8/12", 3, 14)],
}
NAME_HINTS = {
    "TIER1": ["tier 1", "tier1", "t1"],
    "TIER2": ["tier 2", "tier2", "t2"],
    "TIER3": ["tier 3", "tier3", "t3"],
    "TIER4": ["tier 4", "tier4", "t4"],
    "TEXAS": ["texas", " tx"],
    "CALIFORNIA": ["california", " ca "],
}
# states called out in the summary if present (known CPL bleeders)
WATCH_STATES = {"PA", "AZ", "IL", "DE", "PENNSYLVANIA", "ARIZONA",
                "ILLINOIS", "DELAWARE"}

WD, WE = "WD", "WE"


# ── plumbing ─────────────────────────────────────────────────────────────

def connect_ro(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def table_exists(conn, name):
    r = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name = ? "
        "AND type IN ('table','view')", [name]).fetchone()
    return bool(r)


def pick_table(conn, candidates):
    for t in candidates:
        if table_exists(conn, t):
            return t
    return None


def pick_col(conn, table, candidates):
    try:
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return None
    for c in candidates:
        if c in cols:
            return c
    return None


def has_col(conn, table, col):
    try:
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return False
    return col in cols


def pdate(s):
    return datetime.strptime(str(s)[:10], "%Y-%m-%d").date()


def day_class(d: date) -> str:
    return WD if d.weekday() < 5 else WE


def fnum(v, fmt="{:.2f}", dash="—"):
    return fmt.format(v) if v is not None else dash


def classify(kw: str) -> str:
    k = f" {kw.lower().strip()} "
    for label, pats in CARRIER_PATTERNS:
        for p in pats:
            if p in k:
                return label
    return OTHER_LABEL


# ── campaign resolution ─────────────────────────────────────────────────

def resolve_campaigns(conn, args, wanted):
    """Returns {short_name: campaign_id}. ACCOUNT is a pseudo campaign."""
    pinned = dict(CAMPAIGN_IDS)
    for m in args.map or []:
        if "=" not in m:
            sys.exit(f"--map expects NAME=campaign_id, got: {m}")
        k, v = m.split("=", 1)
        pinned[k.strip().upper()] = int(v.strip())

    camp_table = pick_table(conn, CAMP_TABLES)
    if not camp_table:
        sys.exit("No campaigns table/view found.")
    rows = conn.execute(
        f"SELECT campaign_id, campaign_name FROM {camp_table} "
        f"WHERE account_id = ?", [ACCOUNT_ID]).fetchall()
    names = {int(r["campaign_id"]): (r["campaign_name"] or "")
             for r in rows}

    resolved, fell_back = {}, []
    for short in sorted(wanted):
        if short in pinned and pinned[short]:
            cid = int(pinned[short])
            resolved[short] = cid
            if cid not in names:
                print(f"  WARN: pinned id {cid} for {short} not found in "
                      f"{camp_table} (account {ACCOUNT_ID}) — keeping it, "
                      "but verify")
            continue
        hints = NAME_HINTS.get(short, [short.lower()])
        matches = [cid for cid, nm in names.items()
                   if any(h in f" {nm.lower()} " for h in hints)]
        if not matches:
            print(f"  WARN: no campaign matched {short} by name — its "
                  "episodes will be skipped. Pin with --map.")
            continue
        if len(matches) > 1:
            # tiebreak by recent spend
            best = pick_by_recent_spend(conn, matches)
            print(f"  WARN: {len(matches)} campaigns matched {short} by "
                  f"name; picked {best} ('{names[best]}') by recent "
                  "spend. Pin with --map to be safe.")
            matches = [best]
        resolved[short] = matches[0]
        fell_back.append(short)
    if fell_back:
        print("  WARN: name-hint fallback used for: "
              + ", ".join(fell_back)
              + " — cardinal rule is to resolve by immutable "
              "campaign_id; paste the charter ids into CAMPAIGN_IDS or "
              "pass --map.")
    print("  Campaign map: " + ", ".join(
        f"{s}={cid} ('{names.get(cid, '?')}')"
        for s, cid in sorted(resolved.items())))
    return resolved, names


def pick_by_recent_spend(conn, cids):
    t = pick_table(conn, CAMP_DAILY_TABLES)
    if not t:
        return cids[0]
    spend_col = pick_col(conn, t, SPEND_CANDIDATES) or "cost_micros"
    date_col = pick_col(conn, t, DATE_CANDIDATES) or "report_date"
    q = (f"SELECT campaign_id, SUM({spend_col}) s FROM {t} "
         f"WHERE campaign_id IN ({','.join('?' * len(cids))}) "
         f"AND {date_col} >= date('now','-60 day') "
         "GROUP BY campaign_id ORDER BY s DESC")
    try:
        r = conn.execute(q, cids).fetchone()
        return int(r["campaign_id"]) if r else cids[0]
    except sqlite3.Error:
        return cids[0]


# ── campaign daily totals (for window detection) ────────────────────────

def fetch_campaign_totals(conn, camp_map, start, end):
    """{short: {date: spend}} plus ACCOUNT = sum of resolved campaigns."""
    t = pick_table(conn, CAMP_DAILY_TABLES)
    out = {s: {} for s in camp_map}
    if t:
        date_col = pick_col(conn, t, DATE_CANDIDATES) or "report_date"
        spend_col = pick_col(conn, t, SPEND_CANDIDATES) or "cost_micros"
        dev_filter = "AND device = 'ALL'" if has_col(conn, t, "device") \
            else ""
        micros = spend_col == "cost_micros"
        ids = list(camp_map.values())
        q = (f"SELECT campaign_id, {date_col} d, SUM({spend_col}) s "
             f"FROM {t} WHERE campaign_id IN "
             f"({','.join('?' * len(ids))}) "
             f"AND {date_col} BETWEEN ? AND ? {dev_filter} "
             f"GROUP BY campaign_id, {date_col}")
        rev_lookup = {cid: s for s, cid in camp_map.items()}
        for r in conn.execute(q, ids + [start.isoformat(),
                                        end.isoformat()]):
            short = rev_lookup.get(int(r["campaign_id"]))
            if short:
                v = (r["s"] or 0) / (1e6 if micros else 1)
                out[short][pdate(r["d"])] = v
    else:
        print("  WARN: no campaign daily table found — deriving campaign "
              "totals from kpd sums (keyword-covered spend only).")
    acct = {}
    dead = []
    recent_floor = end - timedelta(days=30)
    for short, series in out.items():
        recent = sum(v for d, v in series.items() if d >= recent_floor)
        if recent <= 0:
            dead.append(short)
            continue
        for d, v in series.items():
            acct[d] = acct.get(d, 0.0) + v
    if dead:
        print("  WARN: NO SPEND in the last 30 days for: "
              + ", ".join(sorted(dead))
              + " — wrong/placeholder campaign id? These campaigns are "
              "EXCLUDED from the ACCOUNT total; their own episodes "
              "will all skip. Fix the ids and re-run before trusting "
              "ACCOUNT rows.")
    out[ACCOUNT_LABEL] = acct
    return out


# ── baseline + window machinery ─────────────────────────────────────────

def class_baseline(series, anchor, n_per_class, exclude=frozenset()):
    """Mean of the trailing n same-day-class values ending ON anchor,
    skipping dates in `exclude` (previously detected expansion windows —
    so a baseline anchored at a trough that follows a nearby peak is
    not inflated by that peak). Returns ({class: mean}, {class: n})."""
    vals = {WD: [], WE: []}
    d = anchor
    guard = 0
    while (len(vals[WD]) < n_per_class or len(vals[WE]) < n_per_class) \
            and guard < n_per_class * 7 + 60:
        if d not in exclude:
            c = day_class(d)
            if len(vals[c]) < n_per_class:
                vals[c].append(series.get(d, 0.0))
        d -= timedelta(days=1)
        guard += 1
    means = {c: (sum(v) / len(v) if v else 0.0) for c, v in vals.items()}
    counts = {c: len(v) for c, v in vals.items()}
    return means, counts


def detect_window(series, t0, search_end, base, mult, smooth_days=3,
                  dip_days=1):
    """Maximal run containing the peak where SMOOTHED spend >= mult x
    class baseline, within (t0, search_end]. Detection runs on a
    centered smooth_days-day mean (raw daily noise otherwise snaps
    multi-week runs into fragments); runs may bridge up to dip_days
    consecutive below-threshold days. Measurement downstream always
    uses raw daily values over the returned window."""
    days = []
    d = t0 + timedelta(days=1)
    while d <= search_end:
        days.append(d)
        d += timedelta(days=1)
    if not days:
        return None
    half = max(smooth_days // 2, 0)

    def sm(x):
        vals = [series.get(x + timedelta(days=k), 0.0)
                for k in range(-half, half + 1)]
        return sum(vals) / len(vals)

    def qual(x):
        return sm(x) >= mult * base.get(day_class(x), 0.0) and \
            sm(x) > 0

    peak = max(days, key=sm)
    if not qual(peak):
        return None
    ws = we = peak
    # extend left, bridging up to dip_days non-qualifying days
    while True:
        step, bridged = None, 0
        probe = ws - timedelta(days=1)
        while probe >= days[0] and bridged <= dip_days:
            if qual(probe):
                step = probe
                break
            bridged += 1
            probe -= timedelta(days=1)
        if step is None:
            break
        ws = step
    while True:
        step, bridged = None, 0
        probe = we + timedelta(days=1)
        while probe <= days[-1] and bridged <= dip_days:
            if qual(probe):
                step = probe
                break
            bridged += 1
            probe += timedelta(days=1)
        if step is None:
            break
        we = step
    return ws, we


def build_episodes(short, totals, data_end, args, troughs=None,
                   file_windows=None):
    """List of episode dicts for one campaign. troughs: explicit list
    of dates (cycle-state authority) or None -> hardcoded TROUGHS.
    file_windows: the cycle-state WINDOW rows for this campaign, used
    only as a cross-check — a DIVERGENCE note is attached when the
    detected window differs from the file's by > --divergence-days."""
    eps = []
    if troughs is None:
        troughs = [pdate(s) for s in TROUGHS[short]]
    cutover = pdate(args.cutover)
    bounds = list(zip(troughs, troughs[1:] + [None]))
    prior_window_dates = set()
    for i, (t0, t1) in enumerate(bounds):
        is_open_interval = t1 is None
        search_end = (t1 - timedelta(days=1)) if t1 else data_end
        base, counts = class_baseline(totals, t0, args.baseline_days,
                                      prior_window_dates)
        win = detect_window(totals, t0, search_end, base,
                            args.expansion_mult, args.smooth_days,
                            args.dip_days)
        ep = {"campaign": short, "t0": t0, "t1": t1,
              "base": base, "base_counts": counts,
              "window": win, "open": False, "contaminated": None,
              "skip": None,
              "exclude": frozenset(prior_window_dates)}
        if win is None:
            ep["skip"] = "no above-threshold expansion between troughs"
            eps.append(ep)
            continue
        ws, we = win
        d = ws
        while d <= we:
            prior_window_dates.add(d)
            d += timedelta(days=1)
        if we < cutover:
            ep["skip"] = (f"window {ws}→{we} ends pre-cutover "
                          "(demand-surge era excluded)")
            eps.append(ep)
            continue
        if ws < cutover:
            ep["note"] = f"clipped at cutover (detected start {ws})"
            ws = cutover
            ep["window"] = (ws, we)
        if is_open_interval and we >= data_end - timedelta(
                days=args.edge_guard):
            ep["open"] = True
        for ev_date, label, pre, post in CONTAMINATION.get(short, []):
            ev = pdate(ev_date)
            lo, hi = ev - timedelta(days=pre), ev + timedelta(days=post)
            if ws <= hi and we >= lo:
                ep["contaminated"] = label
        if file_windows:
            upper = t1 or (data_end + timedelta(days=1))
            fcands = [w for w in file_windows if t0 < w[0] < upper]
            if not fcands:
                ep["note"] = ((ep.get("note", "") + "; ")
                              if ep.get("note") else "") +                     "DIVERGENCE: no WINDOW row in cycle-state here"
            else:
                fws, fwe = fcands[0][0], min(fcands[0][1], data_end)
                if max(abs((fws - ws).days), abs((fwe - we).days)) >                         args.divergence_days:
                    ep["note"] = ((ep.get("note", "") + "; ")
                                  if ep.get("note") else "") +                         f"DIVERGENCE: cycle-state says {fws}→{fwe}"
        eps.append(ep)
    return eps


CYCLE_ENTITY_SHORTS = ("TIER1", "TIER2", "TIER3", "TIER4",
                       "CALIFORNIA", "TEXAS", "FLORIDA")


def load_cycle_state(path):
    """Parse the cycle dashboard export (long format: entity, record,
    date_start, date_end, value, note). Returns {"path", "entities":
    {short: {"troughs": [date], "windows": [(ws, we, open, note)]}}}
    or None on any failure (caller falls back to internal detection)."""
    p = Path(path)
    if not p.exists():
        return None

    def to_short(ent):
        e = (ent or "").strip().upper()
        if e == "ACCOUNT":
            return ACCOUNT_LABEL
        for s in CYCLE_ENTITY_SHORTS:
            if e.startswith(s):
                return s
        return None

    ents = {}
    try:
        with open(p, newline="", encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                short = to_short(r.get("entity"))
                if not short:
                    continue
                rec = (r.get("record") or "").strip().upper()
                d = ents.setdefault(short, {"troughs": [], "windows": []})
                if rec == "TROUGH":
                    d["troughs"].append(pdate(r["date_start"]))
                elif rec == "WINDOW":
                    note = (r.get("note") or "").strip()
                    d["windows"].append(
                        (pdate(r["date_start"]), pdate(r["date_end"]),
                         note.upper().startswith("OPEN"), note))
    except (OSError, KeyError, ValueError, TypeError) as e:
        print(f"  WARN: could not parse cycle-state file {p}: {e} — "
              "falling back to internal detection.")
        return None
    ents = {s: d for s, d in ents.items() if d["troughs"]}
    if not ents:
        return None
    for d in ents.values():
        d["troughs"].sort()
        d["windows"].sort()
    return {"path": p, "entities": ents}


def build_episodes_from_state(short, state, totals, data_end, args):
    """Episodes with troughs AND windows taken from the cycle-state
    file (the cycle authority). Downstream policy — cutover skip/clip,
    contamination, OPEN flag, thresholds — is unchanged: the file says
    WHERE the expansions were; this script still decides what
    qualifies for pooling. The internal detector runs per interval as
    a cross-check and a DIVERGENCE note is attached when its bounds
    differ from the file by > --divergence-days."""
    eps = []
    troughs = state["troughs"]
    windows = sorted(state["windows"])
    cutover = pdate(args.cutover)
    bounds = list(zip(troughs, troughs[1:] + [None]))
    for t0, t1 in bounds:
        upper = t1 or (data_end + timedelta(days=1))
        cands = [w for w in windows if t0 < w[0] < upper]
        exclude = set()
        ep = {"campaign": short, "t0": t0, "t1": t1,
              "window": None, "open": False, "contaminated": None,
              "skip": None, "exclude": frozenset(),
              "source": "cycle-state"}
        if not cands:
            ep["skip"] = "no WINDOW row in cycle-state for this interval"
            eps.append(ep)
            continue
        ws, we, opn, wnote = max(cands, key=lambda w: (w[1] - w[0]).days)
        notes = []
        if len(cands) > 1:
            notes.append(f"{len(cands)} WINDOW rows in interval; "
                         "widest used")
        we = min(we, data_end)
        for ows, owe, _o, _n in windows:
            if ows < ws:
                d = ows
                while d <= min(owe, ws - timedelta(days=1)):
                    exclude.add(d)
                    d += timedelta(days=1)
        ep["exclude"] = frozenset(exclude)
        if we < cutover:
            ep["window"] = (ws, we)
            ep["skip"] = (f"window {ws}→{we} ends pre-cutover "
                          "(demand-surge era excluded)")
            eps.append(ep)
            continue
        if ws < cutover:
            notes.append(f"clipped at cutover (file start {ws})")
            ws = cutover
        ep["window"] = (ws, we)
        ep["open"] = opn or we >= data_end - timedelta(
            days=args.edge_guard)
        for ev_date, label, pre, post in CONTAMINATION.get(short, []):
            ev = pdate(ev_date)
            lo, hi = ev - timedelta(days=pre), ev + timedelta(days=post)
            if ws <= hi and we >= lo:
                ep["contaminated"] = label
        # cross-check: what would the internal detector have said?
        if totals:
            base, _c = class_baseline(totals, t0, args.baseline_days,
                                      ep["exclude"])
            own = detect_window(totals, t0,
                                (t1 - timedelta(days=1)) if t1
                                else data_end,
                                base, args.expansion_mult,
                                args.smooth_days, args.dip_days)
            if own is None:
                notes.append("DIVERGENCE: internal detector found no "
                             "window here")
            else:
                d0 = abs((own[0] - ws).days)
                d1 = abs((own[1] - we).days)
                if max(d0, d1) > args.divergence_days:
                    notes.append(f"DIVERGENCE: detector says "
                                 f"{own[0]}→{own[1]}")
        if notes:
            ep["note"] = "; ".join(notes)
        eps.append(ep)
    return eps


def expected_totals(series_pair, base_spend, base_rev, ws, we):
    """(spend_w, rev_w, spend_exp, rev_exp) for one segment series over
    the window, expectations composed from the window's day-class mix."""
    sw = rw = se = re_ = 0.0
    d = ws
    while d <= we:
        c = day_class(d)
        s, r = series_pair.get(d, (0.0, 0.0))
        sw += s
        rw += r
        se += base_spend.get(c, 0.0)
        re_ += base_rev.get(c, 0.0)
        d += timedelta(days=1)
    return sw, rw, se, re_


def seg_class_baseline(series_pair, anchor, n, exclude=frozenset()):
    vals_s = {WD: [], WE: []}
    vals_r = {WD: [], WE: []}
    d = anchor
    guard = 0
    while (len(vals_s[WD]) < n or len(vals_s[WE]) < n) and \
            guard < n * 7 + 60:
        if d not in exclude:
            c = day_class(d)
            if len(vals_s[c]) < n:
                s, r = series_pair.get(d, (0.0, 0.0))
                vals_s[c].append(s)
                vals_r[c].append(r)
        d -= timedelta(days=1)
        guard += 1
    bs = {c: (sum(v) / len(v) if v else 0.0) for c, v in vals_s.items()}
    br = {c: (sum(v) / len(v) if v else 0.0) for c, v in vals_r.items()}
    return bs, br


# ── segment daily fetch ─────────────────────────────────────────────────

def kw_campaign_join(conn):
    """(join_sql, campaign_expr) — tripwire fallback convention."""
    if has_col(conn, "keyword_instances", "campaign_id"):
        return "", "ki.campaign_id"
    return ("JOIN ad_groups ag ON ag.id = ki.ad_group_id",
            "ag.campaign_id")


def detect_flagships(conn, camp_map, rev_col, args):
    """{(campaign_id, kw_text_lower): label} for the two Farmers
    flagship broad keywords (TIER2, TIER3)."""
    overrides = {}
    for f in args.flagship or []:
        if "=" not in f:
            sys.exit(f"--flagship expects NAME=keyword text, got: {f}")
        k, v = f.split("=", 1)
        overrides[k.strip().upper()] = v.strip().lower()
    join_extra, camp_expr = kw_campaign_join(conn)
    flag = {}
    for short in ("TIER2", "TIER3"):
        cid = camp_map.get(short)
        if not cid:
            continue
        if short in overrides:
            kw = overrides[short]
            flag[(cid, kw)] = f"Farmers flagship [{short}]"
            print(f"  Flagship ({short}): '{kw}' (override)")
            continue
        q = (f"SELECT LOWER(TRIM(ki.keyword_text)) kw, ki.match_type mt, "
             f"SUM(p.cost_micros)/1e6 s "
             f"FROM keyword_performance_daily p "
             f"JOIN keyword_instances ki ON ki.id = p.instance_id "
             f"AND ki.account_id = ? {join_extra} "
             f"WHERE p.device = 'ALL' AND p.report_date >= ? "
             f"AND {camp_expr} = ? "
             f"GROUP BY 1, 2 ORDER BY s DESC")
        rows = conn.execute(q, [ACCOUNT_ID, args.cutover, cid]).fetchall()
        best = None
        for r in rows:
            kw = r["kw"] or ""
            if classify(kw) != "Farmers":
                continue
            if any(t in kw for t in HOME_TOKENS):
                continue  # protected home-intent is not the flagship
            mt = str(r["mt"] or "").upper()
            if "BROAD" in mt or mt in ("4", "2"):
                best = (kw, r["s"])
                break
            if best is None:
                best = (kw, r["s"])  # fallback: top non-home Farmers kw
        if best:
            flag[(cid, best[0])] = f"Farmers flagship [{short}]"
            print(f"  Flagship ({short}): '{best[0]}' "
                  f"(post-cutover spend ${best[1]:,.0f}) — auto-detected; "
                  "override with --flagship if wrong")
        else:
            print(f"  WARN: no Farmers flagship candidate found in "
                  f"{short}")
    return flag


def fetch_segment_daily(conn, camp_map, start, end, args):
    """Returns:
       data[(short, segment)][device][date] = (spend, rev)
       where device in ('ALL', 'MOBILE'); short includes ACCOUNT.
       Flagship keywords carry their own segment label in their home
       campaign; the campaign's Farmers row excludes them ('Farmers
       excl. flagship'); ACCOUNT folds flagship back into Farmers."""
    rev_col = pick_col(conn, "keyword_performance_daily", REV_CANDIDATES)
    if not rev_col:
        sys.exit("No revenue column found on keyword_performance_daily "
                 f"(looked for: {', '.join(REV_CANDIDATES)})")
    print(f"  kpd revenue column: {rev_col}")
    flagships = detect_flagships(conn, camp_map, rev_col, args)
    join_extra, camp_expr = kw_campaign_join(conn)
    ids = list(camp_map.values())
    q = (f"SELECT {camp_expr} cid, p.report_date d, p.device dev, "
         f"LOWER(TRIM(ki.keyword_text)) kw, "
         f"SUM(p.cost_micros)/1e6 s, SUM(p.{rev_col}) r "
         f"FROM keyword_performance_daily p "
         f"JOIN keyword_instances ki ON ki.id = p.instance_id "
         f"AND ki.account_id = ? {join_extra} "
         f"WHERE p.device IN ('ALL','MOBILE') "
         f"AND p.report_date BETWEEN ? AND ? "
         f"AND {camp_expr} IN ({','.join('?' * len(ids))}) "
         f"GROUP BY 1, 2, 3, 4")
    short_of = {cid: s for s, cid in camp_map.items()}
    data = {}
    seg_cache = {}

    def add(key, dev, d, s, r):
        data.setdefault(key, {"ALL": {}, "MOBILE": {}})
        cur = data[key][dev].get(d, (0.0, 0.0))
        data[key][dev][d] = (cur[0] + s, cur[1] + r)

    n = 0
    for row in conn.execute(q, [ACCOUNT_ID, start.isoformat(),
                                end.isoformat()] + ids):
        n += 1
        cid = int(row["cid"])
        short = short_of[cid]
        kw = row["kw"] or ""
        if kw not in seg_cache:
            seg_cache[kw] = classify(kw)
        seg = seg_cache[kw]
        d = pdate(row["d"])
        dev = row["dev"]
        s = row["s"] or 0.0
        r = row["r"] or 0.0
        flabel = flagships.get((cid, kw))
        if flabel:
            add((short, flabel), dev, d, s, r)
            add((short, "Farmers excl. flagship"), dev, d, 0.0, 0.0)
            add((ACCOUNT_LABEL, "Farmers"), dev, d, s, r)
        else:
            label = seg
            if seg == "Farmers" and any(
                    c == cid for (c, _k) in flagships):
                label = "Farmers excl. flagship"
            add((short, label), dev, d, s, r)
            add((ACCOUNT_LABEL, seg), dev, d, s, r)
    print(f"  kpd rows aggregated: {n:,}")
    return data, flagships


# ── elasticity cross-check ──────────────────────────────────────────────

def elasticity(series_pair, start, end, min_spend=20.0):
    xs, ys, tot_s, tot_r = [], [], 0.0, 0.0
    d = start
    while d <= end:
        s, r = series_pair.get(d, (0.0, 0.0))
        if s >= min_spend:
            xs.append(math.log(s))
            ys.append(math.log(r + 1.0))
            tot_s += s
            tot_r += r
        d += timedelta(days=1)
    n = len(xs)
    if n < 20 or tot_s <= 0:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    if sxx <= 0:
        return None
    b = sxy / sxx
    syy = sum((y - my) ** 2 for y in ys)
    r2 = (sxy ** 2) / (sxx * syy) if syy > 0 else 0.0
    avg_roas = tot_r / tot_s
    return {"b": b, "r2": r2, "n": n, "avg_roas": avg_roas,
            "marg": b * avg_roas,
            "reliable": (n >= 20 and r2 >= 0.10)}


# ── pooling + classification ────────────────────────────────────────────

def classify_pooled(pooled, ep_vals):
    if pooled is None:
        return "UNTESTED"
    hi = any(v >= 1.00 for v in ep_vals)
    lo = any(v < 0.85 for v in ep_vals)
    inconsistent = hi and lo and len(ep_vals) > 1
    if inconsistent:
        return "MIXED"
    if pooled >= 1.00:
        return "EXPANDABLE"
    if pooled < 0.85:
        return "DRAG"
    return "MIXED"


def consistency_note(ep_vals):
    if not ep_vals:
        return "no qualifying episodes"
    if len(ep_vals) == 1:
        return "single episode"
    rng = max(ep_vals) - min(ep_vals)
    tag = "consistent" if rng <= 0.35 else "wide"
    return f"{len(ep_vals)} eps, range {rng:.2f} ({tag})"


# ── state companion ─────────────────────────────────────────────────────

def state_companion(conn, camp_map, episodes_by_camp, data_end, args):
    t = pick_table(conn, GEO_TABLES)
    if not t:
        print("  WARN: no geo table found — state companion skipped.")
        return [], "no geo table found"
    date_col = pick_col(conn, t, DATE_CANDIDATES)
    state_col = pick_col(conn, t, STATE_CANDIDATES)
    spend_col = pick_col(conn, t, SPEND_CANDIDATES)
    rev_col = pick_col(conn, t, REV_CANDIDATES)
    if not all([date_col, state_col, spend_col, rev_col]):
        msg = (f"geo table {t} missing needed columns "
               f"(date={date_col}, state={state_col}, spend={spend_col}, "
               f"rev={rev_col}) — state companion skipped")
        print("  WARN: " + msg)
        return [], msg
    micros = spend_col == "cost_micros"
    dev_note = "devices summed (no device column)"
    dev_filter = ""
    if has_col(conn, t, "device"):
        vals = [r[0] for r in conn.execute(
            f"SELECT DISTINCT device FROM {t} LIMIT 50")]
        if "ALL" in vals:
            dev_filter = "AND device = 'ALL'"
            dev_note = ("device='ALL' rollup detected and used "
                        "(brief expected none — verify)")
        else:
            dev_note = "devices summed (no ALL rollup, as expected)"
    rows_out = []
    for short in ("TIER2", "TIER1"):
        cid = camp_map.get(short)
        if not cid:
            continue
        q = (f"SELECT {date_col} d, {state_col} st, "
             f"SUM({spend_col}) s, SUM({rev_col}) r "
             f"FROM {t} WHERE campaign_id = ? "
             f"AND {date_col} BETWEEN ? AND ? {dev_filter} "
             f"GROUP BY 1, 2")
        series = {}
        eps_here = episodes_by_camp.get(short, [])
        if not eps_here:
            continue
        start = min(ep["t0"] for ep in eps_here) - timedelta(days=45)
        for r in conn.execute(q, [cid, start.isoformat(),
                                  data_end.isoformat()]):
            st = str(r["st"] or "?").strip()
            d = pdate(r["d"])
            s = (r["s"] or 0.0) / (1e6 if micros else 1.0)
            series.setdefault(st, {})
            cur = series[st].get(d, (0.0, 0.0))
            series[st][d] = (cur[0] + s, cur[1] + (r["r"] or 0.0))
        for ep in episodes_by_camp.get(short, []):
            if ep["skip"] or ep["contaminated"] or not ep["window"]:
                continue
            ws, we = ep["window"]
            for st, sp in series.items():
                bs, br = seg_class_baseline(sp, ep["t0"],
                                            args.baseline_days,
                                            ep["exclude"])
                sw, rw, se, re_ = expected_totals(sp, bs, br, ws, we)
                ds, dr = sw - se, rw - re_
                qual = (ds >= args.min_delta_abs
                        and ds >= args.min_delta_rel * max(se, 1e-9)) \
                    if se > 0 else (ds >= args.min_delta_abs)
                inc = (dr / ds) if abs(ds) > 1e-9 else None
                no_base = se < 20.0
                rows_out.append({
                    "campaign": short, "state": st,
                    "episode": f"{ep['t0']}→{ep['t1'] or 'open'}",
                    "win_start": ws, "win_end": we,
                    "window_spend": sw, "expected_spend": se,
                    "dspend": ds, "window_rev": rw, "expected_rev": re_,
                    "drev": dr, "inc_roas": inc,
                    "baseline_roas": (re_ / se) if se > 0 else None,
                    "qualified": bool(qual and ds > 0),
                    "open": ep["open"],
                    "baseline_note": ("NO PRE-WINDOW BASELINE — state "
                                      "likely new to campaign (geo "
                                      "re-tier); incremental read = "
                                      "level read, not marginal")
                    if no_base else ""})
    return rows_out, f"source={t}; {dev_note}"


# ── cut-list reconciliation ─────────────────────────────────────────────

def load_cutlist(args):
    p = None
    if args.cutlist_csv:
        p = Path(args.cutlist_csv)
        if not p.exists():
            print(f"  WARN: --cutlist-csv not found: {p}")
            return None, None
    else:
        d = Path(args.cutlist_dir)
        if d.exists():
            cands = sorted(
                [x for x in d.glob("*.csv")
                 if "cut" in x.name.lower()
                 and ("kw" in x.name.lower()
                      or "keyword" in x.name.lower()
                      or "pause" in x.name.lower())],
                key=lambda x: x.stat().st_mtime, reverse=True)
            if not cands:
                cands = sorted(d.glob("*cut*.csv"),
                               key=lambda x: x.stat().st_mtime,
                               reverse=True)
            p = cands[0] if cands else None
    if not p:
        return None, None
    try:
        with open(p, newline="", encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
        return rows, p
    except OSError as e:
        print(f"  WARN: could not read cut list {p}: {e}")
        return None, None


def col_of(row, candidates):
    keys = {k.lower().strip(): k for k in row.keys()}
    for c in candidates:
        if c in keys:
            return keys[c]
    return None


def reconcile_cutlist(cut_rows, cut_path, seg_class, camp_map, names,
                      flagships):
    """For each pause candidate: attach its segment's marginal class."""
    if not cut_rows:
        return ["Cut-list reconciliation: no cut-list CSV found — "
                "run expansion_cut_list.py first or pass --cutlist-csv. "
                "Skipped."]
    r0 = cut_rows[0]
    kw_c = col_of(r0, ["text", "keyword_text", "keyword", "kw",
                       "query_text", "query"])
    camp_c = col_of(r0, ["campaign", "campaign_name"])
    cid_c = col_of(r0, ["campaign_id"])
    grain_c = col_of(r0, ["grain"])
    action_c = col_of(r0, ["action"])
    roas_c = col_of(r0, ["win_roas", "window_roas", "roas",
                         "level_roas"])
    spend_c = col_of(r0, ["win_spend", "window_spend", "spend",
                          "window_cost", "cost"])
    if not kw_c:
        return [f"Cut-list reconciliation: {cut_path.name} has no "
                "recognizable keyword column — skipped."]
    id_by_name = {}
    for short, cid in camp_map.items():
        id_by_name[str(cid)] = short
        nm = names.get(cid, "").lower()
        if nm:
            id_by_name[nm] = short
    lines = [f"Cut-list reconciliation (vs {cut_path.name}, "
             f"{len(cut_rows)} candidates):",
             "  level-bad + segment DRAG      -> aligned (cut logic and "
             "marginal logic agree)",
             "  level-bad + segment EXPAND/MIX-> reorder candidate: "
             "level losses may be idiosyncratic to the term, not the "
             "segment's margin",
             "  level-bad + segment UNTESTED  -> no marginal evidence "
             "either way", ""]
    tally = {}
    detail = []
    for r in cut_rows:
        kw = (r.get(kw_c) or "").strip().lower()
        if not kw:
            continue
        camp_raw = (r.get(camp_c) or "").strip() if camp_c else ""
        short = None
        if cid_c and (r.get(cid_c) or "").strip():
            short = id_by_name.get((r.get(cid_c) or "").strip())
        if not short:
            short = id_by_name.get(camp_raw.lower()) or \
                id_by_name.get(camp_raw)
        if not short:
            for s2, hints in NAME_HINTS.items():
                if any(h in f" {camp_raw.lower()} " for h in hints):
                    short = s2
                    break
        seg = classify(kw)
        cid = camp_map.get(short)
        flabel = flagships.get((cid, kw)) if cid else None
        if flabel:
            seg = flabel
        cls = seg_class.get((short, seg)) or \
            (seg_class.get((short, "Farmers excl. flagship"))
             if seg == "Farmers" else None) or \
            seg_class.get((ACCOUNT_LABEL, classify(kw))) or "UNTESTED"
        key = ("aligned" if cls == "DRAG"
               else "reorder" if cls in ("EXPANDABLE", "MIXED")
               else "no-evidence")
        tally[key] = tally.get(key, 0) + 1
        try:
            spend_v = float(r.get(spend_c) or 0) if spend_c else 0.0
        except ValueError:
            spend_v = 0.0
        try:
            roas_v = float(r.get(roas_c)) if roas_c and r.get(roas_c) \
                not in (None, "", "—") else None
        except ValueError:
            roas_v = None
        gr = ((r.get(grain_c) or "?")[:1].lower() if grain_c else "?")
        act = ((r.get(action_c) or "").strip() if action_c else "")
        detail.append((spend_v, kw, camp_raw or "?", roas_v, short or "?",
                       seg, cls, key, gr, act))
    detail.sort(reverse=True)
    for spend_v, kw, camp_raw, roas_v, short, seg, cls, key, gr, act in \
            detail[:25]:
        segtag = seg if "[" in seg else f"{seg} [{short}]"
        lines.append(f"  {key.upper():11s} [{gr}] {kw[:32]:32s} "
                     f"{camp_raw[:14]:14s} ${spend_v:>7,.0f} lvlROAS "
                     f"{fnum(roas_v)}  seg {segtag} -> {cls}"
                     + (f"  ({act})" if act else ""))
    if len(detail) > 25:
        lines.append(f"  ... {len(detail) - 25} more in the CSVs")
    lines.append("  Tally: " + ", ".join(
        f"{k}={v}" for k, v in sorted(tally.items())))
    lines.append(
        "  Read: 'aligned' rows are level-bad in level-bad segments — "
        "cutting them and expecting marginal relief is coherent. "
        "'Reorder' rows are level-bad terms inside segments whose "
        "margin held >=0.85 — different objects: pausing them cleans "
        "level ROAS but the segment's expansion behavior wasn't the "
        "problem.")
    return lines


# ── decomposition charts ────────────────────────────────────────────────

def inc_color(v):
    if v is None:
        return "#808080"
    if v >= 1.0:
        return "#2E7D32"
    if v >= 0.85:
        return "#BF8F00"
    return "#C00000"


def build_decomposition_chart(ep_rows, out_dir, stamp):
    """Per campaign-episode: baseline block (expected spend/day at
    baseline ROAS) + incremental block (dspend/day at incremental
    ROAS) -> blended. Renders 'strong baseline, weak margin'
    directly: green-left + red-right bars are spend expanding into
    strength at a diluting margin."""
    agg = {}
    for r in ep_rows:
        key = (r["campaign"], r["episode"], str(r["win_start"]),
               r["win_days"], bool(r["contaminated"]), r["open"])
        a = agg.setdefault(key, [0.0, 0.0, 0.0, 0.0])
        a[0] += r["window_spend"]
        a[1] += r["expected_spend"]
        a[2] += r["window_rev"]
        a[3] += r["expected_rev"]
    camps = sorted({k[0] for k in agg}, key=lambda c:
                   (c != ACCOUNT_LABEL, c))
    ncols = 2
    nrows = math.ceil(len(camps) / ncols)
    fig, axes = plt.subplots(nrows, ncols,
                             figsize=(12.5, 2.6 * nrows), dpi=150)
    axes = axes.flatten()
    for ax, camp in zip(axes, camps):
        eps = sorted([k for k in agg if k[0] == camp],
                     key=lambda k: k[2])
        ylabels = []
        for yi, key in enumerate(eps):
            ws, es, wr, er = agg[key]
            days = max(key[3], 1)
            base_d, win_d = es / days, ws / days
            d_d = win_d - base_d
            base_roas = (er / es) if es > 0 else None
            inc = ((wr - er) / (ws - es)) if abs(ws - es) > 1 else None
            blend = (wr / ws) if ws > 0 else None
            hat = "///" if key[4] else None
            ax.barh(yi, base_d, color="#44709D", height=0.55,
                    hatch=hat, edgecolor="white")
            if d_d > 0:
                ax.barh(yi, d_d, left=base_d, color=inc_color(inc),
                        height=0.55, hatch=hat, edgecolor="white")
            if base_roas is not None and base_d > 0:
                ax.text(base_d / 2, yi, f"{base_roas:.2f}",
                        ha="center", va="center", fontsize=7,
                        color="white", fontweight="bold")
            if inc is not None and d_d > 0:
                ax.text(base_d + d_d / 2, yi, f"+@{inc:.2f}",
                        ha="center", va="center", fontsize=7,
                        color="white", fontweight="bold")
            tag = f"  blend {blend:.2f}" if blend is not None else ""
            if key[4]:
                tag += " (contam.)"
            elif key[5]:
                tag += " (open)"
            ax.text(win_d, yi, tag, ha="left", va="center",
                    fontsize=6.5, color="#404040")
            lbl = key[2][5:] + f" ({key[3]}d)"
            ylabels.append(lbl)
        ax.set_yticks(range(len(eps)))
        ax.set_yticklabels(ylabels, fontsize=7)
        ax.invert_yaxis()
        ax.set_title(camp, fontsize=10, fontweight="bold")
        ax.set_xlabel("spend $/day (kpd-covered)", fontsize=7)
        ax.tick_params(labelsize=7)
        ax.grid(axis="x", alpha=0.25)
        ax.margins(x=0.18)
    for ax in axes[len(camps):]:
        ax.axis("off")
    fig.suptitle(
        "Average vs marginal — each expansion episode: baseline "
        "block (expected spend/day at baseline ROAS, blue) + "
        "incremental block (added spend/day at incremental "
        "ROAS).\nBlue-into-red bars = spend expanding into a strong "
        "baseline at a diluting margin — the blended number hides "
        "the split.", fontsize=10)
    fig.tight_layout(rect=(0, 0, 1, 0.93))
    p = out_dir / f"marginal_decomposition_{stamp}.png"
    fig.savefig(p)
    plt.close(fig)
    return p


def build_dumbbell_chart(map_rows, out_dir, stamp):
    """Per campaign: segment dumbbells baseline ROAS -> pooled
    incremental ROAS (marker sized by dspend absorbed)."""
    camps = sorted({r["campaign"] for r in map_rows
                    if r["class"] != "UNTESTED"},
                   key=lambda c: (c != ACCOUNT_LABEL, c))
    ncols = 2
    nrows = math.ceil(len(camps) / ncols)
    fig, axes = plt.subplots(nrows, ncols,
                             figsize=(12.5, 2.9 * nrows), dpi=150)
    axes = axes.flatten()
    for ax, camp in zip(axes, camps):
        rows = sorted([r for r in map_rows if r["campaign"] == camp
                       and r["class"] != "UNTESTED"
                       and r["baseline_roas"] is not None
                       and r["pooled_inc_roas"] is not None],
                      key=lambda r: -r["dspend_absorbed"])[:10]
        for yi, r in enumerate(rows):
            b, m = r["baseline_roas"], r["pooled_inc_roas"]
            ax.plot([b, m], [yi, yi], color="#808080", lw=1.2,
                    zorder=1)
            ax.scatter([b], [yi], s=28, facecolors="white",
                       edgecolors="#44709D", linewidths=1.6, zorder=2)
            ax.scatter([m], [yi],
                       s=max(30, min(r["dspend_absorbed"] / 6, 220)),
                       color=inc_color(m), zorder=3)
        ax.axvline(1.0, color="#404040", lw=1)
        ax.axvline(0.85, color="#404040", lw=0.8, ls=":")
        ax.set_yticks(range(len(rows)))
        ax.set_yticklabels(
            [f"{r['segment'][:22]} (${r['dspend_absorbed']:,.0f})"
             for r in rows], fontsize=7)
        ax.invert_yaxis()
        ax.set_title(camp, fontsize=10, fontweight="bold")
        ax.set_xlabel("ROAS", fontsize=7)
        ax.tick_params(labelsize=7)
        ax.grid(axis="x", alpha=0.25)
    for ax in axes[len(camps):]:
        ax.axis("off")
    fig.suptitle(
        "Segment baseline -> marginal (open circle = baseline ROAS; "
        "filled dot = pooled incremental ROAS on the added dollars, "
        "sized by qualifying dspend absorbed).\nLeftward lines "
        "crossing 1.0 or 0.85 = segments whose expansion dollars "
        "earned far below their baseline.", fontsize=10)
    fig.tight_layout(rect=(0, 0, 1, 0.92))
    p = out_dir / f"marginal_dumbbells_{stamp}.png"
    fig.savefig(p)
    plt.close(fig)
    return p


# ── main assembly ───────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--db-path", default=str(DEFAULT_DB_PATH))
    p.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    p.add_argument("--baseline-days", type=int, default=14,
                   help="trailing same-day-class days per class in every "
                        "baseline (tripwire convention)")
    p.add_argument("--expansion-mult", type=float, default=1.25,
                   help="window day qualifies when spend >= mult x "
                        "class baseline (PROPOSED)")
    p.add_argument("--min-delta-abs", type=float, default=250.0,
                   help="min window spend delta ($) for a segment-"
                        "episode to qualify (PROPOSED)")
    p.add_argument("--min-delta-rel", type=float, default=0.15,
                   help="min spend delta as fraction of expected window "
                        "spend (PROPOSED)")
    p.add_argument("--cutover", default="2026-07-01",
                   help="episodes with windows before this date are "
                        "excluded (demand-surge era)")
    p.add_argument("--smooth-days", type=int, default=3,
                   help="centered smoothing window for expansion "
                        "detection (1 = raw daily; measurement always "
                        "uses raw days)")
    p.add_argument("--dip-days", type=int, default=1,
                   help="max consecutive below-threshold days a "
                        "detected run may bridge")
    p.add_argument("--edge-guard", type=int, default=2,
                   help="window reaching within N days of the data edge "
                        "= still-running (OPEN)")
    p.add_argument("--exclude-open", action="store_true",
                   help="exclude OPEN (still-running) episodes from "
                        "pooling")
    p.add_argument("--map", action="append",
                   help='pin campaigns: --map "TIER2=123456789" '
                        "(repeatable)")
    p.add_argument("--flagship", action="append",
                   help='pin flagship kw: --flagship "TIER2=farmers '
                        'insurance" (repeatable)')
    p.add_argument("--cycle-state",
                   default=str(Path(
                       r"C:\Projects\google-ads-ops\analysis"
                       r"\profit-attribution-tracker\output"
                       r"\cycle-dashboard\cycle_state_latest.csv")),
                   help="cycle dashboard export (TROUGH/WINDOW rows). "
                        "When present it is the cycle AUTHORITY: "
                        "troughs and expansion windows are read from "
                        "it and the internal detector runs only as a "
                        "cross-check. Absent/unreadable -> internal "
                        "detection fallback with hardcoded TROUGHS.")
    p.add_argument("--window-source", choices=["file", "detect"],
                   default="detect",
                   help="with a cycle-state file present: 'detect' "
                        "(DEFAULT, ratified 8/24) = HYBRID — troughs "
                        "from the file (cycle authority), windows "
                        "detected on daily spend within them "
                        "(estimator authority), file WINDOW rows "
                        "consumed as cross-check with DIVERGENCE "
                        "flags; 'file' = full adoption of the file's "
                        "WINDOW rows.")
    p.add_argument("--divergence-days", type=int, default=5,
                   help="flag when the internal detector's window "
                        "bounds differ from the cycle-state file by "
                        "more than this many days on either edge")
    p.add_argument("--cutlist-csv", default=None,
                   help="explicit path to the cut-list keyword CSV")
    p.add_argument("--cutlist-dir", default=str(DEFAULT_CUTLIST_DIR),
                   help="dir to auto-find the newest cut-list CSV in")
    p.add_argument("--elast-min-spend", type=float, default=20.0)
    args = p.parse_args()

    db_path = Path(args.db_path)
    if not db_path.exists():
        sys.exit(f"DB not found: {db_path}")
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    conn = connect_ro(db_path)

    cycle = load_cycle_state(args.cycle_state)
    if cycle:
        print(f"Cycle authority: {cycle['path'].name} — troughs and "
              "windows adopted from the cycle dashboard; internal "
              "detector demoted to cross-check.")
    else:
        print("Cycle-state file not found/usable — internal detection "
              f"with hardcoded TROUGHS (looked at {args.cycle_state}).")
    wanted = {s for s in TROUGHS if s != ACCOUNT_LABEL}
    if cycle:
        wanted |= {s for s in cycle["entities"] if s != ACCOUNT_LABEL}
    print("Resolving campaigns...")
    camp_map, names = resolve_campaigns(conn, args, wanted)
    if not camp_map:
        sys.exit("No campaigns resolved — pin with --map.")

    row = conn.execute(
        """SELECT MAX(p.report_date) FROM keyword_performance_daily p
           JOIN keyword_instances ki ON ki.id = p.instance_id
           WHERE ki.account_id = ?""", [ACCOUNT_ID]).fetchone()
    if not row or not row[0]:
        sys.exit("No kpd data found.")
    data_end = pdate(row[0])
    all_troughs = [pdate(x) for v in TROUGHS.values() for x in v]
    if cycle:
        for d in cycle["entities"].values():
            all_troughs.extend(d["troughs"])
    start = min(all_troughs) - timedelta(days=45)
    print(f"Data through {data_end}; history from {start}.")

    print("Fetching campaign daily totals...")
    totals = fetch_campaign_totals(conn, camp_map, start, data_end)

    print("Building episodes...")
    episodes_by_camp = {}
    shorts = [ACCOUNT_LABEL] + sorted(camp_map)
    for short in shorts:
        if cycle and short in cycle["entities"]:
            if args.window_source == "detect":
                eps = build_episodes(
                    short, totals.get(short, {}), data_end, args,
                    troughs=cycle["entities"][short]["troughs"],
                    file_windows=cycle["entities"][short]["windows"])
            else:
                eps = build_episodes_from_state(
                    short, cycle["entities"][short],
                    totals.get(short, {}), data_end, args)
        elif short in TROUGHS:
            eps = build_episodes(short, totals.get(short, {}),
                                 data_end, args)
        else:
            continue
        episodes_by_camp[short] = eps
        for ep in eps:
            tag = (f"SKIP ({ep['skip']})" if ep["skip"]
                   else "CONTAMINATED (" + ep["contaminated"] + ")"
                   if ep["contaminated"]
                   else "OPEN (still running)" if ep["open"] else "ok")
            if ep.get("note"):
                tag += f" [{ep['note']}]" 
            w = (f"{ep['window'][0]}→{ep['window'][1]}"
                 if ep["window"] else "—")
            t1s = str(ep['t1'] or 'open')
            print(f"  {short:11s} {ep['t0']}→{t1s:10s} "
                  f"window {w:24s} {tag}")

    print("Fetching segment daily (kpd)...")
    seg_daily, flagships = fetch_segment_daily(conn, camp_map, start,
                                               data_end, args)

    # per-segment, per-episode metrics
    ep_rows = []
    for (short, seg), devs in sorted(seg_daily.items()):
        pair = devs["ALL"]
        mob = devs["MOBILE"]
        for ep in episodes_by_camp.get(short, []):
            if ep["skip"] or not ep["window"]:
                continue
            ws, we = ep["window"]
            bs, br = seg_class_baseline(pair, ep["t0"],
                                        args.baseline_days,
                                        ep["exclude"])
            sw, rw, se, re_ = expected_totals(pair, bs, br, ws, we)
            ds, dr = sw - se, rw - re_
            msw = 0.0
            d = ws
            while d <= we:
                msw += mob.get(d, (0.0, 0.0))[0]
                d += timedelta(days=1)
            inc = (dr / ds) if abs(ds) > 1e-9 else None
            if ep["contaminated"]:
                status, qualified = "CONTAMINATED", False
            elif ds <= 0:
                status, qualified = "NO-EXPANSION", False
            elif ds < args.min_delta_abs or \
                    (se > 0 and ds < args.min_delta_rel * se):
                status, qualified = "BELOW-THRESHOLD", False
            elif ep["open"] and args.exclude_open:
                status, qualified = "OPEN-EXCLUDED", False
            else:
                status, qualified = ("OPEN" if ep["open"] else "OK"), \
                    True
            ep_rows.append({
                "campaign": short, "segment": seg,
                "episode": f"{ep['t0']}→{ep['t1'] or 'open'}",
                "win_start": ws, "win_end": we,
                "win_days": (we - ws).days + 1,
                "window_spend": sw, "expected_spend": se, "dspend": ds,
                "window_rev": rw, "expected_rev": re_, "drev": dr,
                "inc_roas": inc,
                "baseline_roas": (re_ / se) if se > 0 else None,
                "mobile_weight": (msw / sw) if sw > 0 else None,
                "status": status, "qualified": qualified,
                "open": ep["open"],
                "contaminated": ep["contaminated"] or ""})

    # pool per (campaign, segment)
    map_rows = []
    seg_class = {}
    by_key = {}
    for r in ep_rows:
        by_key.setdefault((r["campaign"], r["segment"]), []).append(r)
    for (short, seg), rows in sorted(by_key.items()):
        qual = [r for r in rows if r["qualified"]]
        tds = sum(r["dspend"] for r in qual)
        tdr = sum(r["drev"] for r in qual)
        pooled = (tdr / tds) if tds > 0 else None
        ep_vals = [r["inc_roas"] for r in qual
                   if r["inc_roas"] is not None]
        cls = classify_pooled(pooled, ep_vals)
        # untested nuance: contaminated-only evidence stays UNTESTED but
        # noted
        note = consistency_note(ep_vals)
        flagged = [r for r in rows if r["status"] == "CONTAMINATED"
                   and r["inc_roas"] is not None]
        if flagged:
            note += ("; contaminated eps (excluded): "
                     + ", ".join(f"{r['inc_roas']:.2f}"
                                 for r in flagged))
        base_se = sum(r["expected_spend"] for r in qual)
        base_re = sum(r["expected_rev"] for r in qual)
        w_spend = sum(r["window_spend"] for r in qual)
        w_mob = sum((r["mobile_weight"] or 0) * r["window_spend"]
                    for r in qual)
        el = elasticity(seg_daily[(short, seg)]["ALL"],
                        pdate(args.cutover), data_end,
                        args.elast_min_spend)
        el_flag = ""
        if el and el["reliable"] and pooled is not None:
            if abs(el["marg"] - pooled) > 0.25:
                el_flag = "DISAGREE"
        elif el and not el["reliable"]:
            el_flag = "weak fit"
        map_rows.append({
            "campaign": short, "segment": seg, "class": cls,
            "baseline_roas": (base_re / base_se) if base_se > 0
            else None,
            "pooled_inc_roas": pooled,
            "episodes_qualifying": len(qual),
            "episode_detail": "; ".join(
                f"{r['win_start']}:{fnum(r['inc_roas'])}"
                f"@${r['dspend']:,.0f}"
                + ("(open)" if r["open"] else "")
                for r in qual) or "—",
            "dspend_absorbed": tds if qual else 0.0,
            "mobile_weight": (w_mob / w_spend) if w_spend > 0 else None,
            "elast_b": el["b"] if el else None,
            "elast_marg_roas": el["marg"] if el else None,
            "elast_n": el["n"] if el else None,
            "elast_r2": el["r2"] if el else None,
            "elast_flag": el_flag,
            "consistency": note,
            "nonqual_statuses": ",".join(sorted(
                {r["status"] for r in rows if not r["qualified"]}))
            or ""})
        seg_class[(short, seg)] = cls

    print("State companion (TIER2/TIER1)...")
    state_rows, state_note = state_companion(conn, camp_map,
                                             episodes_by_camp, data_end,
                                             args)
    cut_rows, cut_path = load_cutlist(args)
    conn.close()

    recon = reconcile_cutlist(cut_rows, cut_path, seg_class, camp_map,
                              names, flagships)

    # ── write outputs ───────────────────────────────────────────────────
    if not map_rows or not ep_rows:
        sys.exit("No segment-episode rows produced — check campaign "
                 "map, trough dates, and cutover setting.")
    stamp = data_end.isoformat()
    map_csv = out_dir / f"marginal_roas_map_{stamp}.csv"
    with open(map_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(map_rows[0].keys()))
        w.writeheader()
        w.writerows(map_rows)
    ep_csv = out_dir / f"episodes_{stamp}.csv"
    with open(ep_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(ep_rows[0].keys()))
        w.writeheader()
        w.writerows(ep_rows)
    st_csv = out_dir / f"state_companion_{stamp}.csv"
    if state_rows:
        with open(st_csv, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(state_rows[0].keys()))
            w.writeheader()
            w.writerows(state_rows)

    # ── summary ────────────────────────────────────────────────────────
    L = []
    L.append(f"MARGINAL ROAS MAP — Google Home (account {ACCOUNT_ID}) — "
             f"data through {stamp}")
    L.append("Measurement only: no cuts, caps, or target changes "
             "recommended here.")
    L.append("")
    L.append("PROPOSED CONSTRUCTIONS IN EFFECT (ratify or re-run with "
             "flags):")
    if cycle and args.window_source == "detect":
        L.append(f"  HYBRID (default, ratified 8/24): troughs ADOPTED "
                 f"from {cycle['path'].name} (cycle authority); "
                 "windows detected on daily spend >= "
                 f"{args.expansion_mult:g}x class baseline within them "
                 "(estimator authority); file WINDOW rows = "
                 f"cross-check (DIVERGENCE > {args.divergence_days}d "
                 "flagged)")
    elif cycle:
        L.append(f"  windows/troughs: ADOPTED from {cycle['path'].name} "
                 "(cycle dashboard = cycle authority); internal "
                 f"detector runs as cross-check (flags divergence > "
                 f"{args.divergence_days}d)")
    else:
        L.append(f"  window: internal detection — contiguous days >= "
                 f"{args.expansion_mult:g}x same-day-class baseline "
                 f"({args.baseline_days} class-days ending on the "
                 "trough), peak-centered; troughs hardcoded")
    L.append(f"  baselines: {args.baseline_days} same-day-class days "
             "ending on the trough, prior expansion windows excluded")
    L.append(f"  qualify: dspend >= ${args.min_delta_abs:,.0f} and >= "
             f"{args.min_delta_rel:.0%} of expected window spend")
    L.append(f"  pool: post-cutover ({args.cutover}) controller-echo "
             "episodes only; OPEN episodes "
             + ("EXCLUDED" if args.exclude_open else
                "included and flagged"))
    L.append("")
    L.append("EPISODE INVENTORY:")
    for short, eps in episodes_by_camp.items():
        for ep in eps:
            w = (f"{ep['window'][0]}→{ep['window'][1]}" if ep["window"]
                 else "—")
            tag = (f"skip: {ep['skip']}" if ep["skip"]
                   else f"CONTAMINATED: {ep['contaminated']}"
                   if ep["contaminated"]
                   else "OPEN" if ep["open"] else "ok")
            if ep.get("note"):
                tag += f" [{ep['note']}]" 
            t1s = str(ep['t1'] or 'open')
            L.append(f"  {short:11s} troughs {ep['t0']} → "
                     f"{t1s:10s}  window {w:25s} {tag}")
    L.append("")
    L.append("THE MAP — ranked by expansion spend absorbed "
             "(qualifying episodes):")
    hdr = (f"  {'CLASS':11s} {'CAMPAIGN':11s} {'SEGMENT':26s} "
           f"{'baseROAS':>8s} {'incROAS':>8s} {'dSpend':>10s} "
           f"{'eps':>3s} {'mobile':>6s} {'elast':>7s}  consistency")
    L.append(hdr)
    for r in sorted(map_rows, key=lambda x: -x["dspend_absorbed"]):
        if r["class"] == "UNTESTED":
            continue
        L.append(
            f"  {r['class']:11s} {r['campaign']:11s} "
            f"{r['segment'][:26]:26s} "
            f"{fnum(r['baseline_roas']):>8s} "
            f"{fnum(r['pooled_inc_roas']):>8s} "
            f"${r['dspend_absorbed']:>9,.0f} "
            f"{r['episodes_qualifying']:>3d} "
            f"{fnum(r['mobile_weight'], '{:.0%}'):>6s} "
            f"{fnum(r['elast_marg_roas']):>7s}"
            f"{('!' if r['elast_flag'] == 'DISAGREE' else ' ')} "
            f"{r['consistency']}")
        L.append(f"      episodes: {r['episode_detail']}")
    L.append("")
    L.append("UNTESTED (no qualifying expansion — 'no evidence', not "
             "'bad'; only a deliberate probe can generate evidence "
             "here):")
    unt = [r for r in map_rows if r["class"] == "UNTESTED"]
    for r in sorted(unt, key=lambda x: (x["campaign"], x["segment"])):
        L.append(f"  {r['campaign']:11s} {r['segment'][:30]:30s} "
                 f"({r['nonqual_statuses'] or 'no episodes'})"
                 + (f"  [{r['consistency']}]"
                    if "contaminated" in r["consistency"] else ""))
    L.append("")
    L.append(f"STATE COMPANION — TIER2/TIER1 ({state_note}):")
    if state_rows:
        n_nb = sum(1 for r in state_rows if r.get("baseline_note"))
        if n_nb:
            L.append(f"  NOTE: {n_nb} state-episode rows have no "
                     "pre-window baseline (state likely entered the "
                     "campaign at the geo re-tier) — for those rows "
                     "incROAS is a LEVEL read, not marginal; marked "
                     "*no-baseline below and in the CSV.")
        qual_st = [r for r in state_rows if r["qualified"]]
        qual_st.sort(key=lambda r: -r["dspend"])
        L.append(f"  {'CAMPAIGN':9s} {'STATE':14s} {'episode':24s} "
                 f"{'dSpend':>10s} {'incROAS':>8s} {'baseROAS':>8s}")
        for r in qual_st[:30]:
            up = r["state"].upper().strip()
            watch = " <-- watch state" if up in WATCH_STATES else ""
            nb = " *no-baseline (re-tier?)" if r.get("baseline_note") \
                else ""
            L.append(f"  {r['campaign']:9s} {r['state'][:14]:14s} "
                     f"{r['episode']:24s} ${r['dspend']:>9,.0f} "
                     f"{fnum(r['inc_roas']):>8s} "
                     f"{fnum(r['baseline_roas']):>8s}"
                     + ("(open)" if r["open"] else "") + watch + nb)
        if len(qual_st) > 30:
            L.append(f"  ... {len(qual_st) - 30} more in "
                     f"{st_csv.name}")
    else:
        L.append("  (none)")
    L.append("")
    L.extend(recon)
    L.append("")

    # closing paragraph with computed numbers
    qual_eps = [r for r in ep_rows if r["qualified"]
                and r["campaign"] != ACCOUNT_LABEL]
    tot_ds = sum(r["dspend"] for r in qual_eps)
    drag_ds = sum(r["dspend"] for r in qual_eps
                  if r["inc_roas"] is not None and r["inc_roas"] < 0.85)
    exp_rows = [r for r in map_rows
                if r["class"] == "EXPANDABLE"
                and r["campaign"] != ACCOUNT_LABEL]
    exp_names = ", ".join(
        (r['segment'] if "[" in r['segment']
         else f"{r['segment']} [{r['campaign']}]")
        + (f" ({fnum(r['pooled_inc_roas'])} on "
           f"${r['dspend_absorbed']:,.0f})")
        for r in sorted(
                              exp_rows,
                              key=lambda x: -x["dspend_absorbed"]))
    exp_ds = sum(r["dspend_absorbed"] for r in exp_rows)
    frac = (drag_ds / tot_ds) if tot_ds > 0 else 0.0
    L.append("ANSWER PARAGRAPH:")
    L.append(
        f"  Across post-cutover controller-echo expansions, the "
        f"structure absorbed ${tot_ds:,.0f} of qualifying incremental "
        f"spend (campaign grain, segment-episodes above threshold). "
        f"Segments classed EXPANDABLE — where the marginal dollar "
        f"earned >= {BREAK_EVEN:.2f} consistently — took "
        f"${exp_ds:,.0f} of it: {exp_names or 'none identified'}. "
        f"${drag_ds:,.0f} ({frac:.0%}) of qualifying expansion spend "
        f"went to segment-episodes running below 0.85 incremental "
        f"ROAS. Caveat carried from the charter: this is the machine's "
        f"chosen margin under current targets and QS, not a "
        f"structure-invariant curve — rerun this map after any cuts, "
        f"QS work, or the next completed cycle before re-reading it. "
        f"UNTESTED segments ({len(unt)}) have no expansion evidence "
        f"in the pool; only a deliberate probe can generate it.")
    decomp_png = build_decomposition_chart(ep_rows, out_dir, stamp)
    dumb_png = build_dumbbell_chart(map_rows, out_dir, stamp)
    L.append("")
    L.append(f"CHARTS: {decomp_png.name} (episode average-vs-marginal "
             f"decomposition) · {dumb_png.name} (segment baseline->"
             "marginal dumbbells)")
    text = "\n".join(L)
    print("\n" + text)
    txt_path = out_dir / f"marginal_roas_map_{stamp}.txt"
    txt_path.write_text(text, encoding="utf-8")
    (out_dir / "latest.txt").write_text(text, encoding="utf-8")
    print(f"\nSaved: {map_csv}")
    print(f"Saved: {decomp_png}")
    print(f"Saved: {dumb_png}")
    print(f"Saved: {ep_csv}")
    if state_rows:
        print(f"Saved: {st_csv}")
    print(f"Saved: {txt_path}")


if __name__ == "__main__":
    main()
