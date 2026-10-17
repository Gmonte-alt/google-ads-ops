"""
Wave Calibration — Google Home (Analysis 5)
═══════════════════════════════════════════════════════════════════════
Metrology and policy, not diagnosis. NO ACTIONS, NO TARGET RECOMMENDA-
TIONS. Measures the tROAS expansion/contraction oscillation and prices
the standing controls from 2026 history:

  D1  Wave measurement (per campaign, current regime): detrend daily
      spend and ROAS against their own rolling baselines; extract
      amplitude (peak-to-trough, ROAS pts + spend % of baseline),
      period (peak spacing + autocorrelation), current phase, and
      cross-campaign phase alignment. Ranges, not points — the current
      regime holds only ~2-3 observed cycles.
  D2  Pendulum transfer function (weekly, full 2026, regime-flagged):
      campaign-week mean (realized ROAS - target) vs following week's
      and week+2's dlog(spend). Weeks containing or immediately
      following our own change events (ANY change type, + seeded
      manual events for changes the API never logged) are EXCLUDED —
      the claim under test is autonomous controller response. 8/16-20
      anchor week marked. A weak fit is reported as a weak fit.
  D3  Surge-budget backtest: daily caps at 1.2x/1.3x/1.5x of a
      trailing 14d same-day-class (weekday/weekend) baseline, FROZEN
      at streak start (the tripwire v1.1 fix — an unfrozen baseline
      chases the breach upward and under-clips). Clipped days valued
      at their own realized ROAS. Per-campaign x cap-level table with
      episodes itemized + sensitivity view. Backtest savings are an
      UPPER BOUND — capping alters controller behavior.
  D4  (a) Target-trough validation (NOT recommendations): current
      target vs 1.00 + measured half-amplitude (and +0.05 margin) —
      flags campaigns whose natural trough dips below break-even by
      construction. (b) Standing KPIs backfilled monthly for 2026:
      time-in-band (% days with 7d rolling ROAS within +/-0.10 of
      target-in-effect) and amplitude-per-cycle.

REGIMES (structural breaks must not be read as oscillation):
  PRE_MAY       2026-01-01 .. 2026-05-05
  MAY_JUN       2026-05-06 .. 2026-06-30   (May cliff, 5/6 target
                batch, 6/18 partner bid-down, 6/19+6/30 GPL launches)
  POST_CUTOVER  2026-07-01 .. now          (current-policy regime)
  Detrending and cycle extraction NEVER cross a regime boundary
  (rolling baselines reset at each boundary).
  KNOWN CONFLICT, ruled with eyes open: the 7/29 RPL k=4 cutover
  (20/35 states moved) sits INSIDE the current regime. Handling:
  (1) 7/29 marked as an intra-regime structural event and its week
  (+ the following week) excluded from D2 for all campaigns;
  (2) T1's ~8/12 floor step seeded as a MANUAL event (portfolio
  detachments / freeze-lift steps may be absent from the API);
  (3) every current-policy headline also reported for the STRICT
  window 7/30 .. now (--strict-start) as a sensitivity.

DATA (house conventions):
  campaign_performance_daily_search (device='ALL') JOIN campaigns
  (account 1); revenue = platform conversion_value (auto-detected per
  REV_CANDIDATES); targets = TARGET_ROAS events in
  campaign_change_events, "117%"-style values normalized; resolve by
  immutable campaign_id; rolling ROAS is ALWAYS ratio-of-sums; data
  treated as complete — NO conversion-lag exclusion (ruling 8/20);
  break-even = 1.00. DB read-only (mode=ro); never during a sync.

OUTPUT (CSVs always written before any chart renders):
  d1_wave_cycles.csv / d1_wave_measurement.csv / d1_sync.csv
  d2_pendulum_weekly.csv / d2_pendulum_fits.csv
  d3_backtest_days.csv / d3_backtest_episodes.csv / d3_backtest_summary.csv
  d4_target_trough_validation.csv / d4_kpi_monthly.csv
  wave_overlay_<CAMPAIGN>_<date>.png, pendulum_scatter_<date>.png,
  backtest_summary_<date>.png, kpi_time_in_band_<date>.png
  wave_calibration_<date>.txt + latest.txt (Slack-pasteable)
  run_header.txt (resolved sources, config, exclusions, warnings)

USAGE:
  python wave_calibration.py --smoke      # full compute, CSVs +
                                          # header + validation prints,
                                          # no charts
  python wave_calibration.py              # full run
  python wave_calibration.py --caps 1.2 1.25 1.3 1.5
"""

import argparse
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
                       r"\profit-attribution-tracker\output\wave-calibration")
ACCOUNT_ID = 1
BREAK_EVEN = 1.00

# ── Regime + event config (edit here if the ruling changes) ─────────
REGIMES = [
    ("PRE_MAY",      date(2026, 1, 1),  date(2026, 5, 5)),
    ("MAY_JUN",      date(2026, 5, 6),  date(2026, 6, 30)),
    ("POST_CUTOVER", date(2026, 7, 1),  None),   # None = latest data
]
CURRENT_REGIME = "POST_CUTOVER"
STRICT_START = date(2026, 7, 30)     # day after RPL k=4 cutover

# Structural events: marked on charts; their week (+ next week) is
# excluded from D2 for ALL campaigns. label -> date
STRUCTURAL_EVENTS = {
    "May target batch":        date(2026, 5, 6),
    "partner bid-down":        date(2026, 6, 18),
    "GPL T1 launch":           date(2026, 6, 19),
    "GPL T2 launch":           date(2026, 6, 30),
    "RPL k=4 cutover (20/35)": date(2026, 7, 29),
}
# Changes the change-events API is known/suspected to have missed
# (portfolio detachments never logged; 8/12 T1 floor step unconfirmed).
# campaign_id -> [(date, label)]
MANUAL_EVENTS = {
    "23496334752": [(date(2026, 8, 12), "T1 floor step 115->~125 "
                     "(freeze-lift; API logging unconfirmed)")],
}
ANCHOR_WEEK = date(2026, 8, 10)      # gap week feeding the 8/16-20
                                     # zero-change expansion

# Charter universe — resolve by immutable campaign_id (cardinal rule).
# Legacy shells and old state singles in account 1 still carry rows;
# --all-campaigns overrides for exploration only.
PINNED_CAMPAIGNS = {
    "23385188622": "CA", "23394808297": "TX", "23394808300": "FL",
    "23428389081": "TIER4RPL", "23496334752": "TIER1RPL",
    "23530860942": "TIER2RPL", "23586637678": "TIER3RPL"}

MAS = [7, 14, 30]
MA_COLORS = {7: "#d62728", 14: "#2a78d6", 30: "#555555"}
REV_CANDIDATES = ["conversion_value", "conversions_value", "conv_value",
                  "primary_conv_value", "total_conv_value"]
CONV_CANDIDATES = ["conversions", "primary_conv_count", "conv_count",
                   "all_conversions"]
CAMP_COLORS = ["#2a78d6", "#d62728", "#0ca30c", "#c98500", "#7b52ab",
               "#0aa3a3", "#b0568c", "#666666"]


# ── shared plumbing (per momentum_tracker / carrier_tripwire) ───────
def connect_ro(p):
    conn = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def pick_col(conn, table, candidates):
    try:
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return None
    return next((c for c in candidates if c in cols), None)


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


def d2s(d):
    return d.isoformat()


def parse_date(s):
    return datetime.strptime(str(s)[:10], "%Y-%m-%d").date()


def regime_of(d):
    for name, s, e in REGIMES:
        if d >= s and (e is None or d <= e):
            return name
    return "PRE_2026"


def regime_bounds(name, last):
    for n, s, e in REGIMES:
        if n == name:
            return s, (e or last)
    raise KeyError(name)


# ── data layer ──────────────────────────────────────────────────────
def fetch(conn, start, end, header):
    tbl = "campaign_performance_daily_search"
    rev = pick_col(conn, tbl, REV_CANDIDATES)
    cnv = pick_col(conn, tbl, CONV_CANDIDATES)
    if not rev:
        sys.exit(f"No revenue column on {tbl} "
                 f"(looked for {', '.join(REV_CANDIDATES)})")
    header.append(f"resolved columns: revenue={rev}, "
                  f"conversions={cnv or 'NONE'}")
    cnv_e = f"SUM(p.{cnv})" if cnv else "NULL"
    data, names = {}, {}
    sql = f"""
        SELECT p.campaign_id AS cid, c.campaign_name AS nm, p.date AS d,
               SUM(p.cost_micros)/1e6 AS cost, SUM(p.{rev}) AS rev,
               {cnv_e} AS conv
        FROM {tbl} p
        JOIN campaigns c ON c.campaign_id = p.campaign_id
        WHERE c.account_id=? AND p.device='ALL' AND p.date BETWEEN ? AND ?
        GROUP BY 1,2,3
    """
    for r in conn.execute(sql, [ACCOUNT_ID, d2s(start), d2s(end)]):
        cid = str(r["cid"])
        names[cid] = short_campaign(r["nm"])
        data.setdefault(cid, {})[parse_date(r["d"])] = (
            r["cost"] or 0.0, r["rev"] or 0.0, r["conv"] or 0.0)
    return data, names


def fetch_events(conn, cids, header):
    """target events (TARGET_ROAS only) + all-change event dates."""
    targets, all_events = {c: [] for c in cids}, {c: set() for c in cids}
    ph = ",".join("?" for _ in cids)
    try:
        sql = f"""SELECT campaign_id, change_date, change_type, new_value
                  FROM campaign_change_events
                  WHERE platform='google' AND account_id=?
                    AND campaign_id IN ({ph}) ORDER BY change_date"""
        for r in conn.execute(sql, [ACCOUNT_ID, *cids]):
            cid = str(r["campaign_id"])
            d = parse_date(r["change_date"])
            all_events[cid].add(d)
            if r["change_type"] == "TARGET_ROAS":
                t = normalize_target(r["new_value"])
                if t is not None:
                    targets[cid].append((d, t))
    except sqlite3.Error as e:
        header.append(f"WARN: campaign_change_events read failed ({e}) "
                      "— targets unresolved, D2/D4 degraded")
    for cid, evs in MANUAL_EVENTS.items():
        if cid in all_events:
            for d, label in evs:
                all_events[cid].add(d)
                header.append(f"manual event seeded: campaign {cid} "
                              f"{d} — {label}")
    return targets, all_events


def target_at(targets_cid, d):
    t = None
    for ed, v in targets_cid:
        if ed <= d:
            t = v
        else:
            break
    return t


# ── rolling helpers (ratio-of-sums, regime-bounded) ─────────────────
def roll_within(vals, d, n, num_i, den_i, r_start, min_days=None):
    """Trailing-n ratio-of-sums ending at d, never crossing r_start.
    Returns None until at least min_days (default n) of window fit
    inside the regime."""
    lo = d - timedelta(days=n - 1)
    if lo < r_start:
        lo = r_start
    span = (d - lo).days + 1
    if span < (min_days or n):
        return None
    num = den = 0.0
    k = lo
    while k <= d:
        v = vals.get(k)
        if v:
            num += v[num_i]
            if den_i is not None:
                den += v[den_i]
        k += timedelta(days=1)
    if den_i is None:
        return num / span
    return (num / den) if den > 0 else None


# ── D1: wave measurement ────────────────────────────────────────────
def detrended_series(vals, r_start, r_end):
    """Per day: spend detrend (7d/30d - 1) and ROAS detrend
    (7d - 30d), both ratio-of-sums, regime-bounded. 30d baseline
    accepts >=14 in-regime days so young regimes still measure."""
    out = {}
    d = r_start
    while d <= r_end:
        s7 = roll_within(vals, d, 7, 0, None, r_start)
        s30 = roll_within(vals, d, 30, 0, None, r_start, min_days=14)
        r7 = roll_within(vals, d, 7, 1, 0, r_start)
        r30 = roll_within(vals, d, 30, 1, 0, r_start, min_days=14)
        sd = (s7 / s30 - 1.0) if (s7 is not None and s30) else None
        rd = (r7 - r30) if (r7 is not None and r30 is not None) else None
        out[d] = (sd, rd, s7, s30, r7, r30)
        d += timedelta(days=1)
    return out


def extract_extrema(xs, ys, min_amp):
    """Alternating peaks/troughs from a series via sign-segment
    extremes, then prune adjacent swings smaller than min_amp."""
    pts = [(x, y) for x, y in zip(xs, ys) if y is not None]
    if len(pts) < 10:
        return []
    ext = []          # (date, value, 'P'|'T')
    seg = [pts[0]]
    sign = 1 if pts[0][1] >= 0 else -1
    for x, y in pts[1:]:
        s = 1 if y >= 0 else -1
        if s == sign:
            seg.append((x, y))
        else:
            f = max if sign > 0 else min
            xd, yv = f(seg, key=lambda p: p[1])
            ext.append((xd, yv, "P" if sign > 0 else "T"))
            seg, sign = [(x, y)], s
    f = max if sign > 0 else min
    xd, yv = f(seg, key=lambda p: p[1])
    ext.append((xd, yv, "P" if sign > 0 else "T"))
    # prune small adjacent swings (removes the pair, keeps alternation)
    changed = True
    while changed and len(ext) >= 2:
        changed = False
        best_i, best_a = None, None
        for i in range(len(ext) - 1):
            a = abs(ext[i][1] - ext[i + 1][1])
            if a < min_amp and (best_a is None or a < best_a):
                best_i, best_a = i, a
        if best_i is not None:
            if best_i == 0:
                del ext[0]
            elif best_i == len(ext) - 2:
                del ext[-1]
            else:
                del ext[best_i:best_i + 2]
            changed = True
    return ext


def autocorr_period(xs, ys, lo=14, hi=49):
    pts = [(x, y) for x, y in zip(xs, ys) if y is not None]
    if len(pts) < lo + 14:
        return None
    ys2 = [y for _, y in pts]
    m = sum(ys2) / len(ys2)
    c = [y - m for y in ys2]
    var = sum(v * v for v in c)
    if var <= 0:
        return None
    best_lag, best_r = None, -2.0
    for lag in range(lo, min(hi, len(c) - 7) + 1):
        r = sum(c[i] * c[i - lag] for i in range(lag, len(c))) / var
        if r > best_r:
            best_lag, best_r = lag, r
    return (best_lag, best_r) if best_lag else None


def pearson(a, b):
    n = len(a)
    if n < 10:
        return None
    ma, mb = sum(a) / n, sum(b) / n
    ca = [x - ma for x in a]
    cb = [x - mb for x in b]
    va = math.sqrt(sum(x * x for x in ca))
    vb = math.sqrt(sum(x * x for x in cb))
    if va == 0 or vb == 0:
        return None
    return sum(x * y for x, y in zip(ca, cb)) / (va * vb)


def best_xcorr_lag(sa, sb, dates, max_lag=10):
    """Lag (days) of max cross-correlation, b shifted vs a."""
    best_lag, best_r = 0, None
    for lag in range(-max_lag, max_lag + 1):
        xs, ys = [], []
        for d in dates:
            va = sa.get(d)
            vb = sb.get(d + timedelta(days=lag))
            if va is not None and vb is not None:
                xs.append(va)
                ys.append(vb)
        r = pearson(xs, ys)
        if r is not None and (best_r is None or r > best_r):
            best_lag, best_r = lag, r
    return best_lag, best_r


def measure_waves(data, names, last, args):
    """Cycle table + per-campaign measurement per regime."""
    cyc_rows, meas, det_store = [], {}, {}
    for cid, vals in data.items():
        for rname, rs, re_ in REGIMES:
            r_end = re_ or last
            if r_end < rs:
                continue
            det = detrended_series(vals, rs, r_end)
            if rname == CURRENT_REGIME:
                det_store[cid] = det
            xs = sorted(det)
            sd = [det[d][0] for d in xs]
            rd = [det[d][1] for d in xs]
            ext_s = extract_extrema(xs, sd, args.min_amp_spend)
            ext_r = extract_extrema(xs, rd, args.min_amp_roas)
            # trailing extrema within edge_guard days of the series
            # end are provisional (segment not closed) — drop from
            # amplitude/period stats
            guard = r_end - timedelta(days=args.edge_guard)
            prov = any(e[0] > guard for e in ext_s)
            ext_s = [e for e in ext_s if e[0] <= guard]
            ext_r = [e for e in ext_r if e[0] <= guard]
            # cycles: peak->trough swings on spend detrend
            swings_s, periods = [], []
            peaks = [e for e in ext_s if e[2] == "P"]
            for i in range(len(ext_s) - 1):
                a, b = ext_s[i], ext_s[i + 1]
                if a[2] == "P" and b[2] == "T":
                    swings_s.append((a[0], b[0], a[1] - b[1]))
            for i in range(len(peaks) - 1):
                periods.append((peaks[i + 1][0] - peaks[i][0]).days)
            swings_r = [abs(ext_r[i][1] - ext_r[i + 1][1])
                        for i in range(len(ext_r) - 1)]
            for pk, tr, amp in swings_s:
                cyc_rows.append({"campaign": names[cid], "cid": cid,
                                 "regime": rname, "peak_date": pk,
                                 "trough_date": tr,
                                 "spend_amp_pct": amp})
            acf = autocorr_period(xs, sd)
            meas[(cid, rname)] = {
                "ext_s": ext_s, "ext_r": ext_r, "periods": periods,
                "swings_s": [a for _, _, a in swings_s],
                "swings_r": swings_r, "acf": acf, "det": det,
                "provisional_tail": prov}
    # current-phase + summary rows (current regime)
    sum_rows = []
    for cid in data:
        m = meas.get((cid, CURRENT_REGIME))
        if not m:
            continue
        all_periods = sorted(
            p for (c2, rn), mm in meas.items() if c2 == cid
            for p in mm["periods"])
        det = m["det"]
        peaks = [e for e in m["ext_s"] if e[2] == "P"]
        last_peak = peaks[-1][0] if peaks else None
        per = sorted(m["periods"])
        med_p = per[len(per) // 2] if per else None
        phase_p = med_p or (all_periods[len(all_periods) // 2]
                            if all_periods else None)
        s7 = det[last][2] if last in det else None
        s14 = roll_within(data[cid], last, 14, 0, None,
                          regime_bounds(CURRENT_REGIME, last)[0])
        phase_state = ("expanding" if (s7 and s14 and s7 > s14)
                       else "contracting")
        sw_s, sw_r = sorted(m["swings_s"]), sorted(m["swings_r"])

        def rng(v, fmt):
            if not v:
                return ("", "", "")
            mid = v[len(v) // 2]
            return (fmt(mid), fmt(v[0]), fmt(v[-1]))
        sum_rows.append({
            "campaign": names[cid], "cid": cid,
            "n_cycles": len(m["swings_s"]),
            "spend_amp_med_pct": rng(sw_s, lambda x: f"{x:.0%}")[0],
            "spend_amp_range": "-".join(
                filter(None, rng(sw_s, lambda x: f"{x:.0%}")[1:])),
            "roas_amp_med_pts": rng(sw_r, lambda x: f"{x:.2f}")[0],
            "roas_amp_range": "-".join(
                filter(None, rng(sw_r, lambda x: f"{x:.2f}")[1:])),
            "period_med_d": med_p or "",
            "period_range_d": (f"{per[0]}-{per[-1]}" if per else ""),
            "period_2026_med_d": (all_periods[len(all_periods) // 2]
                                  if all_periods else ""),
            "period_2026_range_d": (f"{all_periods[0]}-"
                                    f"{all_periods[-1]}"
                                    if all_periods else ""),
            "period_2026_n": len(all_periods),
            "acf_period_d": m["acf"][0] if m["acf"] else "",
            "acf_r": f"{m['acf'][1]:.2f}" if m["acf"] else "",
            "last_peak": last_peak or "",
            "days_since_peak": ((last - last_peak).days
                                if last_peak else ""),
            "phase_frac": (f"{(last - last_peak).days / phase_p:.2f}"
                           if (last_peak and phase_p) else ""),
            "phase_state": phase_state + (
                " (provisional extremum at edge)"
                if m.get("provisional_tail") else ""),
            "half_amp_roas_pts": (sw_r[len(sw_r) // 2] / 2.0
                                  if sw_r else None)})
    # cross-campaign sync on current-regime spend detrend
    rs, re_ = regime_bounds(CURRENT_REGIME, last)
    dts = [rs + timedelta(days=k) for k in range((re_ - rs).days + 1)]
    sync_rows, corrs, lags = [], [], []
    cids = [c for c in det_store
            if sum(1 for d in dts
                   if det_store[c].get(d, (None,))[0] is not None) >= 30]
    for i in range(len(cids)):
        for j in range(i + 1, len(cids)):
            sa = {d: det_store[cids[i]][d][0] for d in dts
                  if det_store[cids[i]].get(d, (None,))[0] is not None}
            sb = {d: det_store[cids[j]][d][0] for d in dts
                  if det_store[cids[j]].get(d, (None,))[0] is not None}
            common = sorted(set(sa) & set(sb))
            r0 = pearson([sa[d] for d in common],
                         [sb[d] for d in common])
            blag, br = best_xcorr_lag(sa, sb, common)
            if r0 is not None:
                corrs.append(r0)
                lags.append(abs(blag))
                sync_rows.append({
                    "a": names[cids[i]], "b": names[cids[j]],
                    "corr_lag0": f"{r0:.2f}",
                    "best_lag_d": blag,
                    "corr_best": f"{br:.2f}" if br else ""})
    sync_summary = {
        "mean_corr_lag0": (sum(corrs) / len(corrs)) if corrs else None,
        "mean_abs_best_lag_d": (sum(lags) / len(lags)) if lags else None,
        "pairs": len(corrs)}
    return cyc_rows, sum_rows, sync_rows, sync_summary, meas, det_store


# ── D2: pendulum transfer function ──────────────────────────────────
def monday_of(d):
    return d - timedelta(days=d.weekday())


def weekly_rows(data, names, targets, all_events, first, last, args):
    """Campaign-week samples: gap -> dlog spend at lag1/lag2, with
    event-week exclusion (any change type + manual + structural)."""
    struct_weeks = set()
    for _, ed in STRUCTURAL_EVENTS.items():
        struct_weeks.add(monday_of(ed))
        struct_weeks.add(monday_of(ed) + timedelta(days=7))
    rows = []
    w0 = monday_of(first)
    if w0 < first:
        w0 += timedelta(days=7)
    for cid, vals in data.items():
        excl = set()
        for ed in all_events.get(cid, ()):
            excl.add(monday_of(ed))
            excl.add(monday_of(ed) + timedelta(days=7))
        excl |= struct_weeks
        weeks = {}
        w = w0
        while w + timedelta(days=6) <= last:
            cost = rev = 0.0
            for k in range(7):
                v = vals.get(w + timedelta(days=k))
                if v:
                    cost += v[0]
                    rev += v[1]
            weeks[w] = (cost, rev)
            w += timedelta(days=7)
        for w, (cost, rev) in weeks.items():
            w1, w2 = w + timedelta(days=7), w + timedelta(days=14)
            if cost < args.min_week_spend:
                continue
            tgt = target_at(targets.get(cid, []), w)
            if tgt is None:
                continue
            roas = rev / cost
            gap = roas - tgt
            row = {"campaign": names[cid], "cid": cid, "week": w,
                   "regime": regime_of(w), "spend": cost,
                   "roas": roas, "target": tgt, "gap": gap,
                   "dlog_lag1": None, "dlog_lag2": None,
                   "excluded": (w in excl),
                   "anchor": (w == ANCHOR_WEEK)}
            if w1 in weeks and weeks[w1][0] >= args.min_week_spend:
                row["dlog_lag1"] = math.log(weeks[w1][0] / cost)
                row["lag1_ok"] = (w not in excl) and (w1 not in excl)
            else:
                row["lag1_ok"] = False
            if (w2 in weeks and w1 in weeks
                    and weeks[w2][0] >= args.min_week_spend
                    and weeks[w1][0] >= args.min_week_spend):
                row["dlog_lag2"] = math.log(weeks[w2][0] / weeks[w1][0])
                row["lag2_ok"] = ((w not in excl) and (w1 not in excl)
                                  and (w2 not in excl))
            else:
                row["lag2_ok"] = False
            rows.append(row)
    return rows


def ols(xy):
    n = len(xy)
    if n < 4:
        return None
    mx = sum(x for x, _ in xy) / n
    my = sum(y for _, y in xy) / n
    sxx = sum((x - mx) ** 2 for x, _ in xy)
    sxy = sum((x - mx) * (y - my) for x, y in xy)
    syy = sum((y - my) ** 2 for _, y in xy)
    if sxx == 0:
        return None
    b = sxy / sxx
    a = my - b * mx
    sse = sum((y - (a + b * x)) ** 2 for x, y in xy)
    r2 = (sxy * sxy / (sxx * syy)) if syy > 0 else 0.0
    se = math.sqrt(sse / (n - 2) / sxx) if n > 2 and sxx > 0 else None
    return {"slope": b, "intercept": a, "r2": r2, "n": n,
            "slope_se": se}


def pendulum_fits(rows, names):
    """Pooled + per-campaign fits, full-year and current regime."""
    fits = []
    scopes = [("2026_all", lambda r: True),
              ("current_regime",
               lambda r: r["regime"] == CURRENT_REGIME)]
    for lag_key, ok_key in [("dlog_lag1", "lag1_ok"),
                            ("dlog_lag2", "lag2_ok")]:
        for scope, pred in scopes:
            pooled = [(r["gap"], r[lag_key]) for r in rows
                      if r[ok_key] and r[lag_key] is not None
                      and pred(r)]
            f = ols(pooled)
            fits.append({"scope": scope, "lag": lag_key,
                         "campaign": "POOLED", **(f or {})})
            for cid in sorted({r["cid"] for r in rows}):
                xy = [(r["gap"], r[lag_key]) for r in rows
                      if r["cid"] == cid and r[ok_key]
                      and r[lag_key] is not None and pred(r)]
                f = ols(xy)
                fits.append({"scope": scope, "lag": lag_key,
                             "campaign": names[cid], **(f or {})})
    return fits


# ── D3: surge-budget backtest ───────────────────────────────────────
def day_class(d):
    return "WE" if d.weekday() >= 5 else "WD"


def trailing_baseline(vals, d, n_days):
    """Mean spend of trailing n same-day-class days before d.
    Returns (baseline, earliest_day_used); the caller enforces that
    the window stays inside the day's regime."""
    got, tot = 0, 0.0
    k = d - timedelta(days=1)
    scanned = 0
    cls = day_class(d)
    earliest = None
    while got < n_days and scanned < n_days * 4:
        if day_class(k) == cls:
            v = vals.get(k)
            tot += v[0] if v else 0.0
            got += 1
            earliest = k
        k -= timedelta(days=1)
        scanned += 1
    if got != n_days:
        return None, None
    return tot / got, earliest


def backtest(data, names, first, last, args):
    day_rows, ep_rows, summary = [], [], {}
    released_ct = 0

    def flush(ep, released=False):
        if ep:
            if released:
                ep["released"] = True
            ep_rows.append(ep)
        return None

    for cid, vals in data.items():
        for mult in args.caps:
            frozen, in_streak, streak_len = None, False, 0
            ep, clipped_days = None, []
            prev_regime = None
            d = first
            while d <= last:
                reg = regime_of(d)
                if reg != prev_regime:
                    # never carry streak state across a boundary
                    ep = flush(ep)
                    in_streak, frozen, streak_len = False, None, 0
                    prev_regime = reg
                v = vals.get(d, (0.0, 0.0, 0.0))
                spend, rev = v[0], v[1]
                base_live, earliest = trailing_baseline(
                    vals, d, args.baseline_days)
                rs = (regime_bounds(reg, last)[0]
                      if reg != "PRE_2026" else None)
                if (base_live is None or base_live < args.min_base
                        or rs is None or earliest < rs):
                    # baseline crosses the regime boundary or is too
                    # thin — a surge read is meaningless on this day
                    ep = flush(ep)
                    in_streak, frozen, streak_len = False, None, 0
                    d += timedelta(days=1)
                    continue
                base = frozen if (in_streak and frozen is not None
                                  and args.freeze_baseline) else base_live
                cap = mult * base
                clip = max(0.0, spend - cap)
                if clip > 0 and not in_streak:
                    in_streak, frozen, streak_len = True, base_live, 0
                    base = frozen if args.freeze_baseline else base_live
                    cap = mult * base
                    clip = max(0.0, spend - cap)
                elif clip <= 0 and in_streak:
                    in_streak, frozen, streak_len = False, None, 0
                if in_streak:
                    streak_len += 1
                    if streak_len > args.max_streak_days:
                        # LEVEL SHIFT, not a surge: release — close
                        # the episode, re-level, stop clipping
                        ep = flush(ep, released=True)
                        released_ct += 1
                        in_streak, frozen, streak_len = False, None, 0
                        clip = 0.0
                if clip > 0:
                    roas_d = (rev / spend) if spend > 0 else 0.0
                    rev_forgone = clip * roas_d
                    gp_net = clip - rev_forgone   # >0 saves GP
                    day_rows.append({
                        "campaign": names[cid], "cid": cid,
                        "date": d, "regime": reg,
                        "cap_mult": mult, "spend": spend,
                        "baseline": base, "cap": cap,
                        "clipped_spend": clip,
                        "day_roas": roas_d,
                        "rev_forgone": rev_forgone,
                        "gp_net_saved": gp_net})
                    if ep is None:
                        ep = {"campaign": names[cid], "cid": cid,
                              "cap_mult": mult, "start": d, "end": d,
                              "days": 0, "clipped": 0.0,
                              "rev_forgone": 0.0, "gp_net": 0.0,
                              "regime": reg, "released": False}
                    ep["end"] = d
                    ep["days"] += 1
                    ep["clipped"] += clip
                    ep["rev_forgone"] += rev_forgone
                    ep["gp_net"] += gp_net
                    clipped_days = []
                else:
                    clipped_days.append(d)
                    if ep and len(clipped_days) > args.episode_gap:
                        ep = flush(ep)
                d += timedelta(days=1)
            ep = flush(ep)
    for r in day_rows:
        for scope in ["2026_all"] + (
                ["current_regime"] if r["regime"] == CURRENT_REGIME
                else []):
            k = (r["campaign"], r["cap_mult"], scope)
            s = summary.setdefault(k, {"days": 0, "clipped": 0.0,
                                       "rev_forgone": 0.0,
                                       "gp_net": 0.0})
            s["days"] += 1
            s["clipped"] += r["clipped_spend"]
            s["rev_forgone"] += r["rev_forgone"]
            s["gp_net"] += r["gp_net_saved"]
    return day_rows, ep_rows, summary, released_ct


# ── D4: target-trough validation + standing KPIs ────────────────────
def trough_validation(sum_rows, targets, names):
    rows = []
    for r in sum_rows:
        cid = r["cid"]
        tgt = None
        tl = targets.get(cid, [])
        if tl:
            tgt = tl[-1][1]
        ha = r["half_amp_roas_pts"]
        if tgt is None or ha is None:
            rows.append({"campaign": r["campaign"], "cid": cid,
                         "current_target": tgt or "",
                         "half_amp_roas_pts": (f"{ha:.2f}" if ha
                                               else ""),
                         "n_cycles": r["n_cycles"],
                         "natural_trough": "", "required_target": "",
                         "required_w_margin": "", "flag": "n/a"})
            continue
        trough = tgt - ha
        req = BREAK_EVEN + ha
        reqm = BREAK_EVEN + ha + 0.05
        flag = ("TROUGH<BE" if trough < BREAK_EVEN else
                ("<BE+margin" if trough < BREAK_EVEN + 0.05 else "ok"))
        rows.append({"campaign": r["campaign"], "cid": cid,
                     "current_target": f"{tgt:.2f}",
                     "half_amp_roas_pts": f"{ha:.2f}",
                     "n_cycles": r["n_cycles"],
                     "natural_trough": f"{trough:.2f}",
                     "required_target": f"{req:.2f}",
                     "required_w_margin": f"{reqm:.2f}",
                     "flag": flag})
    return rows


def kpi_monthly(data, names, targets, cyc_rows, first, last, args):
    rows = []
    for cid, vals in data.items():
        month = date(first.year, first.month, 1)
        while month <= last:
            nm = (date(month.year + 1, 1, 1) if month.month == 12
                  else date(month.year, month.month + 1, 1))
            m_end = min(nm - timedelta(days=1), last)
            in_band = days = 0
            for k in range((m_end - month).days + 1):
                d = month + timedelta(days=k)
                if regime_of(d) == "PRE_2026":
                    continue
                rs = regime_bounds(regime_of(d), last)[0]
                r7 = roll_within(vals, d, 7, 1, 0, rs)
                tgt = target_at(targets.get(cid, []), d)
                if r7 is None or tgt is None:
                    continue
                days += 1
                if abs(r7 - tgt) <= args.band:
                    in_band += 1
            cycles = [c for c in cyc_rows if c["cid"] == cid
                      and c["peak_date"].year == month.year
                      and c["peak_date"].month == month.month]
            amp = (sum(c["spend_amp_pct"] for c in cycles)
                   / len(cycles)) if cycles else None
            rows.append({
                "campaign": names[cid], "cid": cid,
                "month": month.strftime("%Y-%m"),
                "regime": regime_of(min(m_end, max(month, first))),
                "days_measured": days,
                "time_in_band_pct": (f"{in_band / days:.0%}"
                                     if days else ""),
                "cycles_peaking": len(cycles),
                "mean_cycle_spend_amp_pct": (f"{amp:.0%}" if amp
                                             is not None else "")})
            month = nm
    return rows


# ── CSV writer (CSVs always land before any chart renders) ──────────
def write_csv(path, rows, cols):
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(",".join(cols) + "\n")
        for r in rows:
            out = []
            for c in cols:
                v = r.get(c, "")
                v = "" if v is None else v
                if isinstance(v, float):
                    v = f"{v:.4f}"
                v = str(v)
                if "," in v or '"' in v:
                    v = '"' + v.replace('"', '""') + '"'
                out.append(v)
            f.write(",".join(out) + "\n")


# ── charts ──────────────────────────────────────────────────────────
REGIME_SHADE = {"PRE_MAY": "#f2f2f2", "MAY_JUN": "#fdf3e3",
                "POST_CUTOVER": "#eaf3ea"}


def wave_overlay(name, vals, meas_by_regime, targets_cid, last,
                 out_png):
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(14, 7.5),
                                   sharex=True)
    fig.suptitle(f"{name} — wave overlay 2026 (regime-bounded "
                 "7d/30d, peaks/troughs marked)", fontsize=13,
                 fontweight="bold", x=0.07, ha="left")
    first = REGIMES[0][1]
    xs = [first + timedelta(days=k)
          for k in range((last - first).days + 1)]
    spend = [vals.get(d, (0, 0, 0))[0] for d in xs]
    ax1.scatter(xs, spend, s=8, color="#aaa", alpha=0.6, label="daily")
    for rname, rs, re_ in REGIMES:
        r_end = re_ or last
        for ax in (ax1, ax2):
            ax.axvspan(rs, r_end + timedelta(days=1),
                       color=REGIME_SHADE.get(rname, "#fff"),
                       alpha=0.5, zorder=0)
        m = meas_by_regime.get(rname)
        if not m:
            continue
        det = m["det"]
        dd = sorted(det)
        ax1.plot(dd, [det[d][2] for d in dd], lw=1.6,
                 color=MA_COLORS[7],
                 label="7d" if rname == "PRE_MAY" else None)
        ax1.plot(dd, [det[d][3] for d in dd], lw=1.2,
                 color=MA_COLORS[30],
                 label="30d" if rname == "PRE_MAY" else None)
        ax2.plot(dd, [det[d][4] for d in dd], lw=1.6,
                 color=MA_COLORS[7])
        ax2.plot(dd, [det[d][5] for d in dd], lw=1.2,
                 color=MA_COLORS[30])
        for xd, yv, t in m["ext_s"]:
            s7v = det[xd][2]
            ax1.plot([xd], [s7v], marker="^" if t == "P" else "v",
                     ms=8, color="#0ca30c" if t == "P" else "#d03b3b",
                     zorder=5)
    # target step
    if targets_cid:
        tx, ty = [], []
        t_prev = None
        for d in xs:
            t = target_at(targets_cid, d)
            if t is not None:
                tx.append(d)
                ty.append(t)
        if tx:
            ax2.step(tx, ty, where="post", color="#333", lw=1.1,
                     ls=":", label="target")
        for ed, _ in targets_cid:
            if first <= ed <= last:
                for ax in (ax1, ax2):
                    ax.axvline(ed, color="red", ls="--", lw=0.8,
                               alpha=0.6)
    for label, ed in STRUCTURAL_EVENTS.items():
        if first <= ed <= last:
            for ax in (ax1, ax2):
                ax.axvline(ed, color="#7b52ab", ls="-", lw=1.0,
                           alpha=0.55)
            ax1.text(ed, ax1.get_ylim()[1] * 0.98, label, rotation=90,
                     fontsize=6.5, va="top", ha="right",
                     color="#7b52ab")
    ax1.set_ylabel("Spend / day ($)")
    ax2.set_ylabel("ROAS")
    ax2.axhline(BREAK_EVEN, color="#888", lw=0.8, ls="--")
    ax2.set_ylim(0, 3.0)
    for ax in (ax1, ax2):
        ax.grid(True, alpha=0.25)
    ax1.legend(fontsize=8, ncol=4, loc="upper left", framealpha=0.85)
    fig.autofmt_xdate()
    fig.tight_layout(rect=[0, 0, 1, 0.96])
    fig.savefig(out_png, dpi=140, bbox_inches="tight")
    plt.close(fig)


def pendulum_chart(rows, fits, names, out_png):
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(14, 6))
    fig.suptitle("Pendulum transfer function — weekly gap "
                 "(ROAS − target) vs next week's Δlog(spend); "
                 "event weeks excluded", fontsize=12,
                 fontweight="bold", x=0.07, ha="left")
    cids = sorted({r["cid"] for r in rows})
    cmap = {c: CAMP_COLORS[i % len(CAMP_COLORS)]
            for i, c in enumerate(cids)}
    for ax, lag_key, ok_key, ttl in [
            (ax1, "dlog_lag1", "lag1_ok", "lag 1 week"),
            (ax2, "dlog_lag2", "lag2_ok", "lag 2 weeks")]:
        for r in rows:
            if r[lag_key] is None:
                continue
            included = r[ok_key]
            ax.scatter([r["gap"]], [r[lag_key]],
                       s=90 if r["anchor"] else 22,
                       color=cmap[r["cid"]],
                       alpha=0.85 if included else 0.18,
                       marker="*" if r["anchor"] else "o",
                       edgecolors="#333" if r["anchor"] else "none",
                       zorder=5 if r["anchor"] else 3)
        f = next((f for f in fits if f["scope"] == "current_regime"
                  and f["lag"] == lag_key
                  and f["campaign"] == "POOLED" and f.get("slope")
                  is not None), None)
        if f:
            gaps = [r["gap"] for r in rows if r[ok_key]
                    and r[lag_key] is not None]
            if gaps:
                x0, x1 = min(gaps), max(gaps)
                ax.plot([x0, x1],
                        [f["intercept"] + f["slope"] * x0,
                         f["intercept"] + f["slope"] * x1],
                        color="#333", lw=1.4,
                        label=(f"pooled current-regime: slope "
                               f"{f['slope']:.2f} r2 {f['r2']:.2f} "
                               f"n {f['n']}"))
                ax.legend(fontsize=8, loc="upper left")
        ax.axhline(0, color="#999", lw=0.7)
        ax.axvline(0, color="#999", lw=0.7)
        ax.set_title(ttl + "  (faded = excluded week; ★ = 8/16-20 "
                     "anchor)", fontsize=9)
        ax.set_xlabel("week gap: realized ROAS − target")
        ax.set_ylabel("Δlog spend")
        ax.grid(True, alpha=0.25)
    handles = [plt.Line2D([], [], marker="o", ls="", color=cmap[c],
                          label=names[c]) for c in cids]
    fig.legend(handles=handles, fontsize=8, ncol=len(cids),
               loc="lower center", frameon=False)
    fig.tight_layout(rect=[0, 0.05, 1, 0.94])
    fig.savefig(out_png, dpi=140, bbox_inches="tight")
    plt.close(fig)


def backtest_table_chart(summary, caps, out_png, scope="2026_all"):
    camps = sorted({k[0] for k in summary if k[2] == scope})
    n = len(camps)
    fig, ax = plt.subplots(figsize=(13, 1.6 + 0.5 * n))
    ax.axis("off")
    ax.set_xlim(0, 1)
    ax.set_ylim(0, n + 2)
    ax.text(0, n + 1.6, f"Surge-budget backtest — {scope} "
            "(clipped days valued at own realized ROAS; UPPER-BOUND "
            "estimate — capping alters controller behavior)",
            fontsize=11, fontweight="bold")
    xw = 0.78 / max(1, len(caps))
    ax.text(0.0, n + 0.8, "campaign", fontsize=9, color="#888")
    for i, m in enumerate(caps):
        ax.text(0.20 + i * xw, n + 0.8,
                f"{m:g}x  clip / rev / netGP", fontsize=9,
                color="#888")
    for r_i, camp in enumerate(camps):
        y = n - 1 - r_i + 0.2
        ax.axhline(n - 1 - r_i, color="#e5e5e5", lw=0.5)
        ax.text(0.0, y, camp, fontsize=9.5, fontweight="bold")
        for i, m in enumerate(caps):
            s = summary.get((camp, m, scope))
            if not s:
                ax.text(0.20 + i * xw, y, "—", fontsize=9,
                        color="#999")
                continue
            col = "#0ca30c" if s["gp_net"] >= 0 else "#d03b3b"
            ax.text(0.20 + i * xw, y,
                    f"${s['clipped']:,.0f} / ${s['rev_forgone']:,.0f}"
                    f" / ${s['gp_net']:+,.0f}", fontsize=9, color=col)
    fig.savefig(out_png, dpi=150, bbox_inches="tight")
    plt.close(fig)


def kpi_chart(kpi_rows, out_png):
    camps = sorted({r["campaign"] for r in kpi_rows})
    months = sorted({r["month"] for r in kpi_rows})
    fig, ax = plt.subplots(figsize=(13, 5))
    for i, camp in enumerate(camps):
        xs, ys = [], []
        for m in months:
            r = next((r for r in kpi_rows if r["campaign"] == camp
                      and r["month"] == m), None)
            if r and r["time_in_band_pct"]:
                xs.append(m)
                ys.append(float(r["time_in_band_pct"].rstrip("%"))
                          / 100)
        ax.plot(xs, ys, marker="o", ms=4, lw=1.3,
                color=CAMP_COLORS[i % len(CAMP_COLORS)], label=camp)
    ax.set_title("Standing KPI — time-in-band (% days, 7d ROAS "
                 "within ±0.10 of target) — monthly 2026 backfill = "
                 "the baseline future quarters are judged against",
                 fontsize=11, fontweight="bold", loc="left")
    ax.yaxis.set_major_formatter(
        plt.FuncFormatter(lambda v, _: f"{v:.0%}"))
    ax.set_ylim(0, 1)
    ax.grid(True, alpha=0.25)
    ax.legend(fontsize=8, ncol=4)
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(out_png, dpi=140, bbox_inches="tight")
    plt.close(fig)


# ── console summary (Slack-pasteable) ───────────────────────────────
def console_summary(sum_rows, sync, fits, summary, trough_rows,
                    kpi_rows, caps, last, args, warnings):
    L = [f"WAVE CALIBRATION — {last} "
         f"(current regime {CURRENT_REGIME} "
         f"{regime_bounds(CURRENT_REGIME, last)[0]} -> {last}; "
         f"~2-3 cycles observed — ranges, not points)", ""]
    L.append("D1 WAVES (per campaign, current regime; amp = "
             "peak-to-trough)")
    for r in sorted(sum_rows, key=lambda r: r["campaign"]):
        per = (f"{r['period_med_d']}d ({r['period_range_d']})"
               if r["period_med_d"] else "n/a (<2 peaks in regime)")
        p26 = (f"; 2026 regime-bounded spacings "
               f"{r['period_2026_med_d']}d "
               f"({r['period_2026_range_d']}, n="
               f"{r['period_2026_n']})"
               if r.get("period_2026_med_d") else "")
        acf = (f", acf {r['acf_period_d']}d r={r['acf_r']}"
               if r["acf_period_d"] else "")
        L.append(f"  {r['campaign']:12s} period {per}{acf}; spend amp "
                 f"{r['spend_amp_med_pct'] or 'n/a'} "
                 f"({r['spend_amp_range']}); ROAS amp "
                 f"{r['roas_amp_med_pts'] or 'n/a'} pts "
                 f"({r['roas_amp_range']}); phase: "
                 f"{r['days_since_peak']}d past peak "
                 f"(frac {r['phase_frac'] or '—'}), "
                 f"{r['phase_state']}; n={r['n_cycles']} cycles"
                 + p26)
    if sync["mean_corr_lag0"] is not None:
        L.append(f"  SYNC: mean pairwise corr (lag 0) "
                 f"{sync['mean_corr_lag0']:.2f} across "
                 f"{sync['pairs']} pairs; mean |best lag| "
                 f"{sync['mean_abs_best_lag_d']:.1f}d "
                 f"(0 = in phase)")
    L.append("")
    L.append("D2 PENDULUM (gap -> next-week dlog spend; own-change "
             "weeks excluded)")
    for scope in ["current_regime", "2026_all"]:
        for lag in ["dlog_lag1", "dlog_lag2"]:
            f = next((f for f in fits if f["scope"] == scope
                      and f["lag"] == lag
                      and f["campaign"] == "POOLED"), None)
            if not f or f.get("slope") is None:
                L.append(f"  {scope} {lag}: insufficient sample")
                continue
            se = f.get("slope_se")
            ci = (f" (±{1.96 * se:.2f})" if se else "")
            verdict = ("WEAK — too noisy to calibrate at weekly grain"
                       if f["r2"] < args.weak_r2 else "usable")
            L.append(f"  {scope:14s} {lag}: slope {f['slope']:+.2f}"
                     f"{ci} dlog-spend per ROAS pt of gap, "
                     f"r2 {f['r2']:.2f}, n {f['n']} -> {verdict}")
    L.append("  (per-campaign slopes in d2_pendulum_fits.csv; "
             "★ anchor = 8/16-20 zero-change expansion)")
    L.append("")
    L.append("D3 SURGE-BUDGET BACKTEST (net GP = clipped spend − "
             "revenue forgone at realized ROAS; UPPER BOUND)")
    for scope in ["2026_all", "current_regime"]:
        camps = sorted({k[0] for k in summary if k[2] == scope})
        if not camps:
            continue
        L.append(f"  [{scope}]")
        tot = {m: [0.0, 0.0, 0.0] for m in caps}
        for camp in camps:
            parts = []
            for m in caps:
                s = summary.get((camp, m, scope))
                if s:
                    parts.append(f"{m:g}x net ${s['gp_net']:+,.0f}")
                    tot[m][0] += s["clipped"]
                    tot[m][1] += s["rev_forgone"]
                    tot[m][2] += s["gp_net"]
                else:
                    parts.append(f"{m:g}x —")
            L.append(f"    {camp:12s} " + "  ".join(parts))
        L.append("    " + "ACCOUNT      " + "  ".join(
            f"{m:g}x clip ${tot[m][0]:,.0f} net ${tot[m][2]:+,.0f}"
            for m in caps))
    L.append("")
    L.append("D4a TARGET-TROUGH VALIDATION (construction check, NOT "
             "recommendations)")
    for r in trough_rows:
        L.append(f"  {r['campaign']:12s} target "
                 f"{r['current_target'] or '—'}  half-amp "
                 f"{r['half_amp_roas_pts'] or '—'}  natural trough "
                 f"{r['natural_trough'] or '—'}  needs>= "
                 f"{r['required_target'] or '—'} "
                 f"(+margin {r['required_w_margin'] or '—'})  "
                 f"[{r['flag']}]")
    L.append("")
    latest_m = max((r["month"] for r in kpi_rows), default=None)
    if latest_m:
        L.append(f"D4b STANDING KPIs — {latest_m} (full 2026 backfill "
                 "in d4_kpi_monthly.csv)")
        for r in [r for r in kpi_rows if r["month"] == latest_m]:
            L.append(f"  {r['campaign']:12s} time-in-band "
                     f"{r['time_in_band_pct'] or '—'} "
                     f"({r['days_measured']}d); cycles peaking "
                     f"{r['cycles_peaking']}, mean amp "
                     f"{r['mean_cycle_spend_amp_pct'] or '—'}")
    if warnings:
        L += ["", "WARNINGS:"] + [f"  ! {w}" for w in warnings]
    L += ["", "Caveats: current regime holds ~2-3 cycles — every "
          "estimate is a range; backtest is an upper bound (capping "
          "alters controller behavior); no target changes "
          "recommended, no actions taken."]
    return "\n".join(L)


# ── main ────────────────────────────────────────────────────────────
def main():
    global REGIMES
    p = argparse.ArgumentParser()
    p.add_argument("--db-path", default=str(DEFAULT_DB_PATH))
    p.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    p.add_argument("--caps", nargs="+", type=float,
                   default=[1.2, 1.3, 1.5])
    p.add_argument("--baseline-days", type=int, default=14,
                   help="same-day-class days in the surge baseline "
                        "(tripwire convention)")
    p.add_argument("--no-freeze-baseline", dest="freeze_baseline",
                   action="store_false",
                   help="sensitivity: let the baseline chase breach "
                        "days (tripwire v1 behavior)")
    p.add_argument("--episode-gap", type=int, default=1,
                   help="quiet days tolerated inside one episode")
    p.add_argument("--min-base", type=float, default=25.0,
                   help="skip days whose baseline is below this $")
    p.add_argument("--min-week-spend", type=float, default=50.0)
    p.add_argument("--min-amp-spend", type=float, default=0.05,
                   help="prune spend-detrend swings smaller than this")
    p.add_argument("--min-amp-roas", type=float, default=0.04,
                   help="prune ROAS-detrend swings smaller than this")
    p.add_argument("--band", type=float, default=0.10,
                   help="time-in-band half-width (ROAS pts)")
    p.add_argument("--weak-r2", type=float, default=0.20)
    p.add_argument("--strict-start", default=STRICT_START.isoformat(),
                   help="strict current-policy sensitivity window "
                        "start (post-RPL-cutover)")
    p.add_argument("--all-campaigns", action="store_true",
                   help="disable the 7-campaign charter pin")
    p.add_argument("--max-streak-days", type=int, default=21,
                   help="a breach running this long is a LEVEL SHIFT, "
                        "not a surge: episode released, baseline "
                        "re-levels")
    p.add_argument("--edge-guard", type=int, default=3,
                   help="trailing extrema within this many days of "
                        "series end are provisional — excluded from "
                        "amplitude/period stats")
    p.add_argument("--campaign", default=None,
                   help="substring filter for detail charts")
    p.add_argument("--smoke", action="store_true",
                   help="full compute, CSVs + header + validation "
                        "prints, no charts")
    args = p.parse_args()
    args.freeze_baseline = getattr(args, "freeze_baseline", True)

    db = Path(args.db_path)
    if not db.exists():
        sys.exit(f"DB not found: {db}")
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    header, warnings = [], []
    header.append(f"wave_calibration run {datetime.now().isoformat()}")
    header.append(f"db={db}  regimes=" + "; ".join(
        f"{n} {s}->{e or 'latest'}" for n, s, e in REGIMES))
    header.append("structural events (weeks excluded from D2 for "
                  "ALL campaigns): " + "; ".join(
                      f"{lbl} {d}" for lbl, d
                      in STRUCTURAL_EVENTS.items()))
    header.append(f"strict sensitivity window start: "
                  f"{args.strict_start}")
    header.append(f"lag ruling: NONE (8/20 house ruling — data "
                  "treated complete)")
    header.append(f"surge baseline: trailing {args.baseline_days}d "
                  "same-day-class (weekday/weekend), "
                  + ("FROZEN at streak start (v1.1 fix)"
                     if args.freeze_baseline else
                     "UNFROZEN (sensitivity mode)"))

    conn = connect_ro(db)
    row = conn.execute(
        """SELECT MAX(p.date) FROM campaign_performance_daily_search p
           JOIN campaigns c ON c.campaign_id=p.campaign_id
           WHERE c.account_id=?""", [ACCOUNT_ID]).fetchone()
    if not row or not row[0]:
        sys.exit("No campaign data found.")
    last = parse_date(row[0])
    first = REGIMES[0][1]
    fetch_start = first - timedelta(days=70)  # baseline warm-up
    data, names = fetch(conn, fetch_start, last, header)
    if not args.all_campaigns:
        dropped = [c for c in data if c not in PINNED_CAMPAIGNS]
        data = {c: v for c, v in data.items() if c in PINNED_CAMPAIGNS}
        header.append(f"campaign universe PINNED to {len(data)} charter"
                      f" ids ({len(dropped)} legacy/other campaigns "
                      "dropped; --all-campaigns to include)")
        for pid, lbl in PINNED_CAMPAIGNS.items():
            if pid not in data:
                warnings.append(f"pinned campaign {lbl} ({pid}) has NO "
                                "rows in window — check view/state")
    targets, all_events = fetch_events(conn, list(data.keys()), header)
    conn.close()
    header.append(f"data: {len(data)} campaigns, "
                  f"{fetch_start} -> {last}")
    for cid in sorted(data, key=lambda c: names[c]):
        tl = targets.get(cid, [])
        cur = tl[-1] if tl else None
        header.append(f"  {names[cid]:14s} id={cid}  target events="
                      f"{len(tl)}  current="
                      + (f"{cur[1]:.2f} (since {cur[0]})" if cur
                         else "UNRESOLVED")
                      + f"  all-change dates={len(all_events[cid])}")
        if not tl:
            warnings.append(f"{names[cid]}: no TARGET_ROAS events — "
                            "excluded from D2/D4; check event layer "
                            "(portfolio detachments never logged)")

    # ── compute (current regime as configured) ──
    (cyc_rows, sum_rows, sync_rows, sync, meas,
     _) = measure_waves(data, names, last, args)
    d2_rows = weekly_rows(data, names, targets, all_events, first,
                          last, args)
    fits = pendulum_fits(d2_rows, names)
    day_rows, ep_rows, summary, released_ct = backtest(
        data, names, first, last, args)
    header.append(f"backtest: baselines regime-bounded; "
                  f"{released_ct} level-shift releases at "
                  f">{args.max_streak_days}d streaks (episodes "
                  "flagged 'released' in d3_backtest_episodes.csv)")
    trough_rows = trough_validation(
        [r for r in sum_rows], targets, names)
    kpi_rows = kpi_monthly(data, names, targets, cyc_rows, first,
                           last, args)

    # ── strict-window sensitivity (7/30 -> now) ──
    strict = parse_date(args.strict_start)
    saved = REGIMES
    REGIMES = [r for r in REGIMES if r[0] != CURRENT_REGIME] + [
        (CURRENT_REGIME, strict, None)]
    try:
        _, strict_sum, _, strict_sync, _, _ = measure_waves(
            data, names, last, args)
    finally:
        REGIMES = saved
    for r in strict_sum:
        r["window"] = f"strict {strict}->"
    for r in sum_rows:
        r["window"] = f"{regime_bounds(CURRENT_REGIME, last)[0]}->"
    header.append("strict-window D1 sensitivity appended to "
                  "d1_wave_measurement.csv (window column)")
    if strict_sync["mean_corr_lag0"] is not None and \
            sync["mean_corr_lag0"] is not None:
        header.append(f"sync corr: regime "
                      f"{sync['mean_corr_lag0']:.2f} vs strict "
                      f"{strict_sync['mean_corr_lag0']:.2f}")

    # ── CSVs first, always ──
    stamp = last.isoformat()
    write_csv(out_dir / "d1_wave_cycles.csv", cyc_rows,
              ["campaign", "cid", "regime", "peak_date",
               "trough_date", "spend_amp_pct"])
    write_csv(out_dir / "d1_wave_measurement.csv",
              sum_rows + strict_sum,
              ["campaign", "cid", "window", "n_cycles",
               "spend_amp_med_pct", "spend_amp_range",
               "roas_amp_med_pts", "roas_amp_range", "period_med_d",
               "period_range_d", "period_2026_med_d",
               "period_2026_range_d", "period_2026_n",
               "acf_period_d", "acf_r", "last_peak",
               "days_since_peak", "phase_frac", "phase_state"])
    write_csv(out_dir / "d1_sync.csv", sync_rows,
              ["a", "b", "corr_lag0", "best_lag_d", "corr_best"])
    write_csv(out_dir / "d2_pendulum_weekly.csv", d2_rows,
              ["campaign", "cid", "week", "regime", "spend", "roas",
               "target", "gap", "dlog_lag1", "lag1_ok", "dlog_lag2",
               "lag2_ok", "excluded", "anchor"])
    write_csv(out_dir / "d2_pendulum_fits.csv",
              [f for f in fits if f.get("slope") is not None],
              ["scope", "lag", "campaign", "slope", "slope_se",
               "intercept", "r2", "n"])
    write_csv(out_dir / "d3_backtest_days.csv", day_rows,
              ["campaign", "cid", "date", "regime", "cap_mult",
               "spend", "baseline", "cap", "clipped_spend",
               "day_roas", "rev_forgone", "gp_net_saved"])
    write_csv(out_dir / "d3_backtest_episodes.csv", ep_rows,
              ["campaign", "cid", "cap_mult", "regime", "start",
               "end", "days", "clipped", "rev_forgone", "gp_net",
               "released"])
    sm_rows = [{"campaign": k[0], "cap_mult": k[1], "scope": k[2],
                **v} for k, v in sorted(summary.items())]
    write_csv(out_dir / "d3_backtest_summary.csv", sm_rows,
              ["campaign", "cap_mult", "scope", "days", "clipped",
               "rev_forgone", "gp_net"])
    write_csv(out_dir / "d4_target_trough_validation.csv", trough_rows,
              ["campaign", "cid", "current_target",
               "half_amp_roas_pts", "n_cycles", "natural_trough",
               "required_target", "required_w_margin", "flag"])
    write_csv(out_dir / "d4_kpi_monthly.csv", kpi_rows,
              ["campaign", "cid", "month", "regime", "days_measured",
               "time_in_band_pct", "cycles_peaking",
               "mean_cycle_spend_amp_pct"])

    text = console_summary(sum_rows, sync, fits, summary, trough_rows,
                           kpi_rows, args.caps, last, args, warnings)
    print(text)
    (out_dir / f"wave_calibration_{stamp}.txt").write_text(
        text, encoding="utf-8")
    (out_dir / "latest.txt").write_text(text, encoding="utf-8")
    (out_dir / "run_header.txt").write_text(
        "\n".join(header + ["", "WARNINGS:"] + warnings),
        encoding="utf-8")
    print(f"\nCSVs + run_header.txt saved to {out_dir}")

    if args.smoke:
        print("\n--smoke: validation only, charts skipped")
        print(f"  d2 samples lag1 (included): "
              f"{sum(1 for r in d2_rows if r['lag1_ok'])} of "
              f"{sum(1 for r in d2_rows if r['dlog_lag1'] is not None)}"
              " with data")
        print(f"  d3 clipped day-rows: {len(day_rows)}, "
              f"episodes: {len(ep_rows)}")
        print(f"  d1 cycles (all regimes): {len(cyc_rows)}")
        return

    # ── charts (after CSVs) ──
    for cid, vals in data.items():
        if args.campaign and \
                args.campaign.upper() not in names[cid].upper():
            continue
        mbr = {rn: meas.get((cid, rn))
               for rn, _, _ in REGIMES if meas.get((cid, rn))}
        safe = names[cid].replace("/", "_").replace(" ", "_")
        wave_overlay(names[cid], vals, mbr, targets.get(cid, []),
                     last, out_dir / f"wave_overlay_{safe}_{stamp}.png")
    pendulum_chart(d2_rows, fits, names,
                   out_dir / f"pendulum_scatter_{stamp}.png")
    backtest_table_chart(summary, args.caps,
                         out_dir / f"backtest_summary_{stamp}.png",
                         "2026_all")
    backtest_table_chart(summary, args.caps,
                         out_dir /
                         f"backtest_summary_current_{stamp}.png",
                         "current_regime")
    kpi_chart(kpi_rows, out_dir / f"kpi_time_in_band_{stamp}.png")
    print("Charts saved.")


if __name__ == "__main__":
    main()
