"""
tROAS Target Gradient v3 — Google Home Insurance
═══════════════════════════════════════════════════════════════════════
Per-campaign target-change gradient with an ELASTICITY MODEL (v3): each
rung now carries modeled conversions, spend, and GP — not just expected
ROAS — so the volume/efficiency trade is visible: raising the target
delivers FEWER conversions at higher ROAS; lowering it buys volume at
lower ROAS.

MODEL (two moving parts, both directional):
  1. ROAS response:  expected ROAS(t) = delivery_ratio × t
     (post-freeze delivery ratio realized/current held constant).
  2. Volume response: volume_mult(t) = (t / current) ^ (-ε)
     applied to post-freeze spend/day and conv/day. ε > 0 means higher
     targets shed volume. Modeled revenue/day = spend/day × mult ×
     expected ROAS; modeled GP/day = revenue/day − spend/day (DB terms).

ELASTICITY SOURCE per campaign (v4 — event study):
  The v3 weekly ln(conv)~ln(ROAS) fit failed on real data (r²≈0,
  fits rejected): week-to-week realized-ROAS variation is mostly
  market movement, where volume and ROAS co-move, which attenuates or
  flips the slope. v4 instead uses the NATURAL EXPERIMENTS in
  campaign_change_events: each historical TARGET_ROAS step with
  |Δln target| ≥ --min-move-pct is measured as
      ε_event = −Δln(conv/day) / Δln(target)
  comparing --pre-days before the change to --post-days after it
  (skipping 2 ramp days; the post window is truncated at the next
  TARGET_ROAS/DAILY_BUDGET event on the campaign and dropped if <5
  usable days or if it extends into the lag-soft zone).
  Pooling: per-campaign weighted median (weights = |Δln target|);
  fallback chain campaign events → account-pooled events → --elasticity
  default (4.0, anchored on the May tightening ~197→70 conv/day and
  the June controller correction, clicks −41% on ~15% ROAS shift —
  both imply ε≈3.5-4.5). Every event's implied ε is listed in Notes.
  Still directional: events carry seasonality/market confounds. The
  Google tROAS simulator (Layer 3, UI, after the first clean week) is
  the arbiter for rung-vs-rung curvature.

The GP/day column is the payoff: with ε > 1 the modeled GP curve has an
interior maximum — the rung marked ★ maximizes modeled GP in true
economics (revenue corrected by (1+correction) for the decision, DB
numbers never restated in the data columns).

TARGET RETRIEVAL (unchanged from v2): latest TARGET_ROAS event per
campaign_id in campaign_change_events (snapshot campaigns.target_roas
NULL for these ids; fallback only). NOTE: cutover-day detachments from
portfolio strategies may not emit TARGET_ROAS events — spot-check
resolved targets against the UI once.

DATA RULES (unchanged): campaign_performance_daily_search device='ALL',
revenue = native conversion_value, summed numerators;
campaign_performance_geo_daily_search device rows summed; no keyword or
base tables; DB opened mode=ro.

USAGE:
  python troas_target_gradient_v4.py
  python troas_target_gradient_v4.py --elasticity 4.0 --pre-days 14 --post-days 14
  python troas_target_gradient_v4.py --no-fit          # force default ε
  python troas_target_gradient_v4.py --span-points 4 --step-points 1

OUTPUT: troas_target_gradient_<YYYY-MM-DD>.xlsx
  Charts / Gradient / State Decomp / Notes
"""

import argparse
import io
import math
import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

try:
    from openpyxl import Workbook
    from openpyxl.drawing.image import Image as XLImage
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter
except ImportError:
    sys.exit("openpyxl (with Pillow) is required: pip install openpyxl pillow")

DEFAULT_DB_PATH = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")
ACCOUNT_ID = 1

# NOTE (8/13): comments corrected — V2's list carried pre-cutover
# names. True DB names verified via simulator last-week cross-check:
ENABLED_CAMPAIGN_IDS = [
    "23385188622",   # CALIFORNIA-NB
    "23394808297",   # TEXAS-NB
    "23394808300",   # FLORIDA-NB
    "23428389081",   # TIER4RPL (legacy shell re-enabled as T4 at cutover)
    "23496334752",   # TIER1RPL
    "23530860942",   # TIER2RPL
    "23586637678",   # TIER3RPL
]

EPS_MIN, EPS_MAX = 0.3, 8.0
FIT_MIN_WEEKS, FIT_MIN_R2 = 8, 0.10

HEADER_FILL = PatternFill("solid", fgColor="1F3864")
HEADER_FONT = Font(bold=True, color="FFFFFF", size=10)
CAMP_FILL = PatternFill("solid", fgColor="D9E2F3")
CURR_FILL = PatternFill("solid", fgColor="E2EFDA")
BEN_FILL = PatternFill("solid", fgColor="FFF2CC")
NEG_FILL = PatternFill("solid", fgColor="F8CBAD")
STAR_FILL = PatternFill("solid", fgColor="C6E0B4")     # GP-max rung
SOFT_FONT = Font(italic=True, color="808080", size=9)
THIN = Side(style="thin", color="BFBFBF")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)



# ─────────────────────────────────────────────────────────────────────
# Google Ads tROAS bid simulator — transcribed from UI screenshots,
# simulation period Aug 2-8, 2026 (weekly figures). Layer 3 evidence.
# points: (target_ratio, cost_wk, conv_wk, conv_value_wk, addl_budget_day)
# ─────────────────────────────────────────────────────────────────────
SIMULATOR_PERIOD = "Aug 2-8, 2026"
SIMULATOR_DATA = {
    "CALIFORNIA": {  # current 117%
        "current": 1.17,
        "last_week": (2067.63, 106, 2938.20),
        "points": [
            (4.68, 9.95, 4, 65.77, None),
            (3.51, 60.04, 10, 230.73, None),
            (2.34, 180.42, 23, 532.72, None),
            (1.75, 455.24, 40, 1002.29, None),
            (1.46, 832.17, 59, 1544.24, None),
            (1.29, 1436.79, 85, 2288.41, None),
            (1.17, 2115.90, 109, 3024.36, None),
            (1.05, 2800.91, 129, 3659.67, None),
            (0.88, 4546.47, 176, 5123.22, None),
            (0.58, 14469.43, 367, 11257.94, 590.79),
        ],
    },
    "FLORIDA": {  # current 112%
        "current": 1.12,
        "last_week": (192.26, 9, 193.85),
        "points": [
            (4.48, 1.02, 0, 3.73, None),
            (3.36, 3.62, 1, 9.18, None),
            (2.24, 17.06, 2, 33.65, None),
            (1.68, 37.86, 3, 58.33, None),
            (1.40, 77.42, 5, 98.94, None),
            (1.23, 114.94, 6, 132.61, None),
            (1.12, 197.43, 9, 200.49, None),
            (1.01, 242.22, 10, 228.44, None),
            (0.84, 419.64, 15, 338.09, None),
            (0.56, 1873.51, 41, 1044.89, None),
        ],
    },
    "TEXAS": {  # current 112%
        "current": 1.12,
        "last_week": (2081.63, 44, 2493.24),
        "points": [
            (4.48, 11.94, 2, 51.98, None),
            (3.36, 43.15, 4, 141.40, None),
            (2.24, 122.49, 8, 300.52, None),
            (1.68, 349.69, 14, 649.93, None),
            (1.40, 706.20, 22, 1102.99, None),
            (1.23, 1243.94, 32, 1699.70, None),
            (1.12, 2155.02, 46, 2598.65, None),
            (1.01, 3100.54, 59, 3403.42, None),
            (0.84, 5136.85, 82, 4877.91, None),
            (0.56, 18425.09, 185, 11851.30, 717.00),
        ],
    },
    "TIER1RPL": {  # current 115%
        "current": 1.15,
        "last_week": (2774.66, 42, 2225.70),
        "points": [
            (1.34, 1534.03, 28, 1385.43, None),
            (1.22, 2182.31, 36, 1856.16, None),
            (1.15, 2832.98, 43, 2288.45, None),
            (1.07, 3455.72, 48, 2668.72, None),
            (1.02, 4087.53, 53, 3023.45, None),
            (0.96, 4816.27, 59, 3401.33, None),
            (0.93, 5303.58, 63, 3642.63, 43.20),
            (0.85, 6173.06, 70, 4073.17, 195.78),
            (0.84, 7385.96, 80, 4673.76, 451.04),
            (0.79, 9065.58, 92, 5451.54, 828.08),
        ],
    },
    "TIER2RPL": {  # current 117%
        "current": 1.17,
        "last_week": (4293.71, 91, 5179.75),
        "points": [
            (4.68, 37.42, 3, 107.79, None),
            (3.51, 160.31, 10, 446.25, None),
            (2.34, 374.50, 18, 840.69, None),
            (1.75, 923.31, 34, 1645.83, None),
            (1.46, 1665.68, 49, 2552.77, None),
            (1.29, 2943.03, 72, 3956.90, None),
            (1.17, 4437.28, 94, 5386.50, None),
            (1.05, 5796.68, 110, 6473.08, None),
            (0.88, 9302.44, 150, 8984.81, 196.80),
            (0.58, 31461.71, 330, 21094.64, 4224.05),
        ],
    },
    "TIER3RPL": {  # current 117% (events said 1.13 — STALE)
        "current": 1.17,
        "last_week": (1335.47, 34, 1663.77),
        "points": [
            (1.41, 719.76, 23, 1063.09, None),
            (1.25, 1054.01, 29, 1396.42, None),
            (1.17, 1364.37, 35, 1705.93, None),
            (1.07, 1852.42, 43, 2169.48, None),
            (1.01, 2262.53, 49, 2532.55, None),
            (0.98, 2678.78, 55, 2899.31, None),
            (0.91, 3012.48, 59, 3157.89, None),
            (0.90, 3440.42, 65, 3482.49, None),
            (0.86, 4020.88, 72, 3899.16, None),
            (0.81, 4942.58, 83, 4527.59, None),
        ],
    },
    "TIER4RPL": {  # current 117% (events said 1.12 — STALE)
        "current": 1.17,
        "last_week": (846.40, 32, 862.97),
        "points": [
            (4.68, 5.20, 2, 20.39, None),
            (3.51, 21.03, 3, 55.62, None),
            (2.34, 60.20, 6, 120.10, None),
            (1.75, 184.31, 12, 277.92, None),
            (1.46, 325.01, 18, 419.62, None),
            (1.29, 544.70, 24, 617.75, None),
            (1.17, 862.48, 33, 885.96, None),
            (1.05, 1178.34, 40, 1111.86, None),
            (0.88, 1926.64, 55, 1580.48, None),
            (0.58, 7530.52, 126, 4285.20, None),
        ],
    },
}


def sim_for(camp):
    """Simulator entry matched by NAME token (screenshot titles are the
    authority; id-keying caused a tier rotation when V2's stale id
    comments were trusted — 8/13 incident)."""
    name = camp["name"].upper()
    for token, data in SIMULATOR_DATA.items():
        if token in name:
            return data
    return None



def _sim_interp(points, t):
    """Log-linear interpolation of (cost, conv, value) at target t
    within the simulator's plotted range. Returns None outside it."""
    pts = sorted((p[0], p[1], p[2], p[3]) for p in points
                 if p[1] > 0 and p[2] > 0 and p[3] > 0)
    for (t1, c1, v1, x1), (t2, c2, v2, x2) in zip(pts, pts[1:]):
        if t1 <= t <= t2:
            f = (math.log(t) - math.log(t1)) / (math.log(t2) - math.log(t1))
            g = lambda a, b: math.exp(math.log(a) + f * (math.log(b) - math.log(a)))
            return g(c1, c2), g(v1, v2), g(x1, x2)
    return None


def compute_sim_options(sim, current, corr_pct, tol_pct):
    """Two options off the simulator curve in TRUE economics
    (value x (1+corr) - cost):
      A 'chase GP'    — target maximizing true GP within the plotted range
      B 'chase volume'— lowest target keeping true GP within tol% of
                        current
    Range-edge results are flagged (no extrapolation beyond Google's
    plotted points)."""
    c = 1 + corr_pct / 100.0
    pts = [p for p in sim["points"] if p[1] > 0 and p[2] > 0 and p[3] > 0]
    lo = min(p[0] for p in pts)
    hi = max(p[0] for p in pts)
    base = _sim_interp(pts, current)
    if not base:
        return None
    b_cost, b_conv, b_val = base
    gp0 = b_val * c - b_cost
    grid, t = [], lo
    while t <= hi + 1e-9:
        r = _sim_interp(pts, t)
        if r:
            grid.append((t, r[0], r[1], r[2], r[3] * 0 if False else r[2]))
        t += 0.005
    # (t, cost, conv, value)
    grid = [(t, r[0], r[1], r[2]) for t, r in
            [(g[0], _sim_interp(pts, g[0])) for g in grid]]
    def gp(row):
        return row[3] * c - row[1]
    best = max(grid, key=gp)
    opt_a = {"target": best[0], "dconv": best[2] / b_conv - 1,
             "dgp": (gp(best) / gp0 - 1) if gp0 > 0 else None,
             "gp_wk": gp(best), "edge": abs(best[0] - hi) < 0.006 or
             abs(best[0] - lo) < 0.006}
    opt_b = None
    if gp0 > 0:
        floor_gp = gp0 * (1 - tol_pct / 100.0)
        ok = [row for row in grid if gp(row) >= floor_gp]
        if ok:
            low = min(ok, key=lambda r: r[0])
            opt_b = {"target": low[0], "dconv": low[2] / b_conv - 1,
                     "dgp": gp(low) / gp0 - 1, "gp_wk": gp(low),
                     "edge": abs(low[0] - lo) < 0.006}
    return {"gp0_wk": gp0, "a": opt_a, "b": opt_b, "range": (lo, hi)}


def sim_local_elasticity(points, current):
    """Arc elasticity from the two points bracketing the current
    target (conversions basis)."""
    pts = sorted(points, key=lambda p: p[0])
    below = [p for p in pts if p[0] < current and p[2] > 0]
    above = [p for p in pts if p[0] > current and p[2] > 0]
    if not below or not above:
        return None
    lo, hi = below[-1], above[0]
    if lo[2] <= 0 or hi[2] <= 0 or lo[0] <= 0 or hi[0] <= 0:
        return None
    return -math.log(hi[2] / lo[2]) / math.log(hi[0] / lo[0])


# ─────────────────────────────────────────────────────────────────────
# Data access
# ─────────────────────────────────────────────────────────────────────

def connect_ro(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def normalize_target(raw):
    if raw is None:
        return None
    s = str(raw).strip().rstrip(",").rstrip('"').strip()
    if not s:
        return None
    if s.endswith("%"):
        try:
            return float(s.rstrip("%")) / 100.0
        except ValueError:
            return None
    try:
        v = float(s)
    except ValueError:
        return None
    return v / 100.0 if v > 10 else v


def fetch_campaigns(conn):
    ph = ",".join("?" for _ in ENABLED_CAMPAIGN_IDS)
    sql = f"""
        SELECT campaign_id, campaign_name, target_roas
        FROM campaigns
        WHERE account_id = ? AND campaign_id IN ({ph})
          AND status = 'ENABLED'
        ORDER BY campaign_name
    """
    rows = conn.execute(sql, [ACCOUNT_ID, *ENABLED_CAMPAIGN_IDS]).fetchall()
    return [{
        "campaign_id": str(r["campaign_id"]),
        "name": r["campaign_name"],
        "snapshot_target": normalize_target(r["target_roas"]),
    } for r in rows]


def fetch_latest_targets(conn, campaigns):
    ids = [c["campaign_id"] for c in campaigns]
    ph = ",".join("?" for _ in ids)
    sql = f"""
        SELECT e.campaign_id, e.change_date, e.new_value
        FROM campaign_change_events e
        WHERE e.platform = 'google' AND e.account_id = ?
          AND e.change_type = 'TARGET_ROAS' AND e.campaign_id IN ({ph})
        ORDER BY e.change_date
    """
    latest = {}
    try:
        for r in conn.execute(sql, [ACCOUNT_ID, *ids]):
            v = normalize_target(r["new_value"])
            if v is not None:
                latest[str(r["campaign_id"])] = (v, str(r["change_date"])[:10])
    except sqlite3.Error as exc:
        print(f"  WARN: could not read campaign_change_events: {exc}")
    for c in campaigns:
        if c["campaign_id"] in latest:
            c["target"], evt_date = latest[c["campaign_id"]]
            c["target_source"] = f"event {evt_date}"
        elif c["snapshot_target"] is not None:
            c["target"] = c["snapshot_target"]
            c["target_source"] = "snapshot (no TARGET_ROAS events)"
        else:
            c["target"] = None
            c["target_source"] = "UNRESOLVED — supply manually"
    return campaigns


def apply_simulator_targets(campaigns):
    """UI simulator screenshots are ground truth for the live setting;
    the change-event layer is known to miss cutover-day portfolio
    detachments. Override where they disagree, loudly."""
    for c in campaigns:
        sim = sim_for(c)
        if not sim:
            continue
        sim_t = sim["current"]
        if c["target"] is None or abs(c["target"] - sim_t) > 1e-9:
            old = f"{c['target']:.2f}" if c["target"] is not None else "—"
            print(f"  TARGET OVERRIDE {short_name(c['name'])}: "
                  f"{old} ({c['target_source']}) → {sim_t:.2f} "
                  f"[UI simulator {SIMULATOR_PERIOD}]")
            c["target"] = sim_t
            c["target_source"] = f"UI simulator {SIMULATOR_PERIOD}"
        else:
            c["target_source"] += f"; UI-confirmed {SIMULATOR_PERIOD}"
    return campaigns


def max_data_date(conn):
    row = conn.execute(
        """SELECT MAX(p.date) FROM campaign_performance_daily_search p
           JOIN campaigns c ON c.campaign_id = p.campaign_id
           WHERE c.account_id = ?""", [ACCOUNT_ID]).fetchone()
    return datetime.strptime(row[0][:10], "%Y-%m-%d").date()


def campaign_perf(conn, ids, start, end):
    ph = ",".join("?" for _ in ids)
    sql = f"""
        SELECT campaign_id,
               SUM(cost_micros) / 1e6 AS cost,
               SUM(conversions)       AS conv,
               SUM(conversion_value)  AS revenue
        FROM campaign_performance_daily_search
        WHERE campaign_id IN ({ph}) AND device = 'ALL'
          AND date BETWEEN ? AND ?
        GROUP BY campaign_id
    """
    rows = conn.execute(sql, [*ids, start.isoformat(), end.isoformat()])
    return {str(r["campaign_id"]): dict(r) for r in rows}


def state_perf(conn, ids, start, end):
    ph = ",".join("?" for _ in ids)
    sql = f"""
        SELECT campaign_id, geo_state,
               SUM(cost_micros) / 1e6 AS cost,
               SUM(conversions)       AS conv,
               SUM(conversion_value)  AS revenue
        FROM campaign_performance_geo_daily_search
        WHERE campaign_id IN ({ph}) AND date BETWEEN ? AND ?
        GROUP BY campaign_id, geo_state
    """
    rows = conn.execute(sql, [*ids, start.isoformat(), end.isoformat()])
    out = {}
    for r in rows:
        out.setdefault(str(r["campaign_id"]), []).append(dict(r))
    return out


def weekly_history(conn, ids, end, weeks):
    """Weekly (Mon-Sun) cost/conv/revenue per campaign for the fit."""
    # snap end back to the last Sunday on/before end
    last_sun = end - timedelta(days=(end.weekday() + 1) % 7)
    start = last_sun - timedelta(days=7 * weeks - 1)
    ph = ",".join("?" for _ in ids)
    sql = f"""
        SELECT campaign_id, date,
               SUM(cost_micros) / 1e6 AS cost,
               SUM(conversions)       AS conv,
               SUM(conversion_value)  AS revenue
        FROM campaign_performance_daily_search
        WHERE campaign_id IN ({ph}) AND device = 'ALL'
          AND date BETWEEN ? AND ?
        GROUP BY campaign_id, date
    """
    rows = conn.execute(sql, [*ids, start.isoformat(), last_sun.isoformat()])
    weekly = {}
    for r in rows:
        d = datetime.strptime(str(r["date"])[:10], "%Y-%m-%d").date()
        wk = d - timedelta(days=d.weekday())        # Monday key
        agg = weekly.setdefault(str(r["campaign_id"]), {}).setdefault(
            wk, {"cost": 0.0, "conv": 0.0, "revenue": 0.0})
        agg["cost"] += r["cost"] or 0
        agg["conv"] += r["conv"] or 0
        agg["revenue"] += r["revenue"] or 0
    return weekly


# ─────────────────────────────────────────────────────────────────────
# Elasticity fit
# ─────────────────────────────────────────────────────────────────────

def fit_elasticity(weeks_dict, default_eps):
    """ln(conv) ~ a + b*ln(roas) per weekly point; eps = -b.
    Returns (eps, source_label)."""
    pts = []
    for agg in weeks_dict.values():
        if agg["cost"] > 0 and agg["conv"] > 0 and agg["revenue"] > 0:
            roas = agg["revenue"] / agg["cost"]
            if roas > 0:
                pts.append((math.log(roas), math.log(agg["conv"])))
    n = len(pts)
    if n < FIT_MIN_WEEKS:
        return default_eps, f"default ε={default_eps:.2f} (fit n={n}<{FIT_MIN_WEEKS})"
    mx = sum(p[0] for p in pts) / n
    my = sum(p[1] for p in pts) / n
    sxx = sum((p[0] - mx) ** 2 for p in pts)
    sxy = sum((p[0] - mx) * (p[1] - my) for p in pts)
    syy = sum((p[1] - my) ** 2 for p in pts)
    if sxx <= 0 or syy <= 0:
        return default_eps, f"default ε={default_eps:.2f} (degenerate fit)"
    b = sxy / sxx
    r2 = (sxy * sxy) / (sxx * syy)
    if b >= 0 or r2 < FIT_MIN_R2:
        return default_eps, (f"default ε={default_eps:.2f} "
                             f"(fit rejected: slope={b:+.2f}, r²={r2:.2f}, n={n})")
    eps = min(max(-b, EPS_MIN), EPS_MAX)
    return eps, f"fit ε={eps:.2f} (r²={r2:.2f}, n={n})"


def fetch_all_change_events(conn, ids):
    """All TARGET_ROAS + DAILY_BUDGET events per campaign, ordered."""
    ph = ",".join("?" for _ in ids)
    sql = f"""
        SELECT campaign_id, change_type, change_date, old_value, new_value
        FROM campaign_change_events
        WHERE platform = 'google' AND account_id = ?
          AND change_type IN ('TARGET_ROAS', 'DAILY_BUDGET')
          AND campaign_id IN ({ph})
        ORDER BY campaign_id, change_date
    """
    out = {}
    try:
        for r in conn.execute(sql, [ACCOUNT_ID, *ids]):
            out.setdefault(str(r["campaign_id"]), []).append({
                "type": r["change_type"],
                "date": datetime.strptime(str(r["change_date"])[:10],
                                          "%Y-%m-%d").date(),
                "old": r["old_value"], "new": r["new_value"],
            })
    except sqlite3.Error as exc:
        print(f"  WARN: could not read campaign_change_events: {exc}")
    return out


def daily_conv_series(conn, ids, start, end):
    """conv/day per campaign per date (device='ALL')."""
    ph = ",".join("?" for _ in ids)
    sql = f"""
        SELECT campaign_id, date, SUM(conversions) AS conv
        FROM campaign_performance_daily_search
        WHERE campaign_id IN ({ph}) AND device = 'ALL'
          AND date BETWEEN ? AND ?
        GROUP BY campaign_id, date
    """
    out = {}
    for r in conn.execute(sql, [*ids, start.isoformat(), end.isoformat()]):
        d = datetime.strptime(str(r["date"])[:10], "%Y-%m-%d").date()
        out.setdefault(str(r["campaign_id"]), {})[d] = r["conv"] or 0
    return out


def _mean_conv(series, start, end):
    days = [(start + timedelta(days=i)) for i in range((end - start).days + 1)]
    vals = [series.get(d) for d in days]
    vals = [v for v in vals if v is not None]
    if len(vals) < 5:
        return None, len(vals)
    return sum(vals) / len(vals), len(vals)


def event_study_elasticities(conn, ids, args, hard_end):
    """Per-event implied ε from TARGET_ROAS steps.
    Returns (per_campaign_events, all_events)."""
    events = fetch_all_change_events(conn, ids)
    if not events:
        return {}, []
    earliest = min(e["date"] for evs in events.values() for e in evs)
    series = daily_conv_series(
        conn, ids, earliest - timedelta(days=args.pre_days + 2), hard_end)
    per_camp, pooled = {}, []
    ramp = 2
    for cid, evs in events.items():
        s = series.get(cid, {})
        troas_evs = [e for e in evs if e["type"] == "TARGET_ROAS"]
        for e in troas_evs:
            old_t = normalize_target(e["old"])
            new_t = normalize_target(e["new"])
            if not old_t or not new_t:
                continue
            dln_t = math.log(new_t / old_t)
            if abs(dln_t) < args.min_move_pct / 100.0:
                continue
            pre_start = e["date"] - timedelta(days=args.pre_days)
            pre_end = e["date"] - timedelta(days=1)
            post_start = e["date"] + timedelta(days=ramp)
            post_end = e["date"] + timedelta(days=ramp + args.post_days - 1)
            # truncate post window at the next event on this campaign
            nxt = [x["date"] for x in evs if x["date"] > e["date"]]
            if nxt:
                post_end = min(post_end, min(nxt) - timedelta(days=1))
            post_end = min(post_end, hard_end)
            pre, n_pre = _mean_conv(s, pre_start, pre_end)
            post, n_post = _mean_conv(s, post_start, post_end)
            if not pre or not post or pre <= 0 or post <= 0:
                continue
            eps = -math.log(post / pre) / dln_t
            rec = {"campaign_id": cid, "date": e["date"],
                   "old": old_t, "new": new_t, "dln_t": dln_t,
                   "pre": pre, "post": post, "eps": eps,
                   "n_pre": n_pre, "n_post": n_post}
            per_camp.setdefault(cid, []).append(rec)
            pooled.append(rec)
    return per_camp, pooled


def _weighted_median(recs):
    recs = sorted(recs, key=lambda r: r["eps"])
    tot = sum(abs(r["dln_t"]) for r in recs)
    acc = 0.0
    for r in recs:
        acc += abs(r["dln_t"])
        if acc >= tot / 2:
            return r["eps"]
    return recs[-1]["eps"]


def _usable(recs):
    """Events kept for pooling: positive implied ε within the sane
    band. Negative ε (volume rose on a tighter target) and |ε| beyond
    EPS_MAX (e.g. structure changes landing on the same day, like the
    7/29 cutover) are confounded — listed in Notes but excluded here."""
    return [r for r in recs if 0 < r["eps"] <= EPS_MAX]


def pick_elasticity(cid, per_camp, pooled, default_eps):
    """Fallback chain: campaign events -> account-pooled -> default."""
    own = _usable(per_camp.get(cid, []))
    if own:
        eps = min(max(_weighted_median(own), EPS_MIN), EPS_MAX)
        n = len(own)
        return eps, f"events ε={eps:.2f} ({n} event{'s' if n != 1 else ''})"
    account = _usable(pooled)
    if account:
        eps = min(max(_weighted_median(account), EPS_MIN), EPS_MAX)
        n = len(account)
        return eps, f"acct-pooled ε={eps:.2f} ({n} event{'s' if n != 1 else ''})"
    return default_eps, f"default ε={default_eps:.2f} (no usable events)"


# ─────────────────────────────────────────────────────────────────────
# Gradient math
# ─────────────────────────────────────────────────────────────────────

def safe_div(a, b):
    return a / b if b else None


def build_ladder(camp, perf, args, db_breakeven, n_days, eps, eps_label):
    cost = perf.get("cost", 0) or 0
    revenue = perf.get("revenue", 0) or 0
    conv = perf.get("conv", 0) or 0
    realized = safe_div(revenue, cost)
    target = camp["target"]
    ratio = safe_div(realized, target) if (realized and target) else None
    gp = revenue - cost
    spend_day, conv_day = cost / n_days, conv / n_days

    rungs = []
    if target is not None and ratio:
        n_steps = int(round(args.span_points / args.step_points))
        for k in range(-n_steps, n_steps + 1):
            pts = k * args.step_points
            t = target + pts / 100.0
            exp_roas = ratio * t
            mult = (t / target) ** (-eps)
            e_spend = spend_day * mult
            e_conv = conv_day * mult
            e_rev = e_spend * exp_roas
            e_gp_db = e_rev - e_spend
            e_gp_true = e_rev * (1 + args.correction_pct / 100.0) - e_spend
            rungs.append({
                "points": pts, "target": t, "expected_roas": exp_roas,
                "e_spend_day": e_spend, "e_conv_day": e_conv,
                "e_gp_day_db": e_gp_db, "e_gp_day_true": e_gp_true,
                "below_breakeven": exp_roas < db_breakeven,
                "is_current": k == 0,
                "is_ben": abs(pts + args.ben_points) < 1e-9,
                "is_gp_max": False,
            })
        best = max(rungs, key=lambda r: r["e_gp_day_true"])
        best["is_gp_max"] = True
        best["gp_max_edge"] = best is rungs[0] or best is rungs[-1]
    return {"cost": cost, "revenue": revenue, "conv": conv, "gp": gp,
            "realized": realized, "ratio": ratio, "rungs": rungs,
            "spend_day": spend_day, "conv_day": conv_day,
            "eps": eps, "eps_label": eps_label}


def short_name_by_id(campaigns, cid):
    for c in campaigns:
        if c["campaign_id"] == cid:
            return short_name(c["name"])
    return cid


def short_name(name):
    n = name.replace("HOME INSURANCE", "").replace("_STATE-GROUP_", "")
    n = n.replace("-NB-SEARCH-ALL-USA-STE", "").replace(
        "_COMBINED_TROAS_SEARCH_ALLDEVICE_USA-STE", "")
    return n.strip("-_ ") or name


# ─────────────────────────────────────────────────────────────────────
# Chart — small multiples: ROAS line (left axis) + GP/day (right axis)
# ─────────────────────────────────────────────────────────────────────

def build_gradient_chart_png(campaigns, ladders, args, db_breakeven):
    plotable = [c for c in campaigns if ladders[c["campaign_id"]]["rungs"]]
    if not plotable:
        return None
    n = len(plotable)
    ncols = 2
    nrows = (n + ncols - 1) // ncols
    fig, axes = plt.subplots(nrows, ncols, figsize=(11.5, 3.3 * nrows))
    axes = axes.flatten() if n > 1 else [axes]

    for ax, camp in zip(axes, plotable):
        lad = ladders[camp["campaign_id"]]
        xs = [r["target"] for r in lad["rungs"]]
        roas = [r["expected_roas"] for r in lad["rungs"]]
        gp = [r["e_gp_day_db"] for r in lad["rungs"]]
        conv = [r["e_conv_day"] for r in lad["rungs"]]
        target = camp["target"]
        ben_t = target - args.ben_points / 100.0
        gp_max = next(r for r in lad["rungs"] if r["is_gp_max"])

        ax.axhspan(min(min(roas), db_breakeven) - 0.02, db_breakeven,
                   color="#F8CBAD", alpha=0.4, zorder=0)
        ax.axhline(db_breakeven, color="#C0504D", linestyle="--",
                   linewidth=1.1, label=f"break-even (DB {db_breakeven:.2f})")
        ax.plot(xs, roas, marker="o", markersize=4, color="#1F4E79",
                linewidth=1.7, label="expected ROAS", zorder=3)
        ax.axvline(target, color="#404040", linestyle="-.", linewidth=1.1,
                   label=f"current {target:.2f}")
        ax.axvline(ben_t, color="#BF8F00", linestyle=":", linewidth=1.5,
                   label=f"Ben -{args.ben_points:g}pt")
        ax.set_ylabel("expected ROAS (DB)", fontsize=7.5, color="#1F4E79")
        ax.tick_params(labelsize=7)
        ax.grid(True, alpha=0.22)

        ax2 = ax.twinx()
        ax2.plot(xs, gp, marker="s", markersize=3.5, color="#2E7D32",
                 linewidth=1.6, linestyle="--", label="modeled GP/day")
        ax2.plot(xs, conv, marker="^", markersize=3.5, color="#7F7F7F",
                 linewidth=1.2, linestyle=":", label="modeled conv/day")
        ax2.plot([gp_max["target"]], [gp_max["e_gp_day_db"]], marker="*",
                 markersize=13, color="#2E7D32", zorder=5,
                 label=(f"GP-max {gp_max['points']:+g}pt"
                        + (" (edge)" if gp_max.get("gp_max_edge") else "")))
        ax2.set_ylabel("GP $/day (DB) · conv/day", fontsize=7.5,
                       color="#2E7D32")
        ax2.tick_params(labelsize=7)

        h1, l1 = ax.get_legend_handles_labels()
        h2, l2 = ax2.get_legend_handles_labels()
        ax.legend(h1 + h2, l1 + l2, fontsize=5.6, loc="upper left",
                  framealpha=0.85)
        ax.set_title(f"{short_name(camp['name'])}   [{lad['eps_label']}]",
                     fontsize=8.5, fontweight="bold")
        ax.set_xlabel("candidate tROAS target", fontsize=7.5)

    for ax in axes[len(plotable):]:
        ax.set_visible(False)
    fig.suptitle(
        f"tROAS gradient ±{args.span_points:g}pts — volume model: "
        f"mult=(t/current)^(-ε); higher target ⇒ fewer conversions. "
        f"Directional, not a forecast.", fontsize=9.5)
    fig.tight_layout(rect=[0, 0, 1, 0.965])
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150)
    plt.close(fig)
    buf.seek(0)
    return buf




def write_simulator_sheet(ws, campaigns, ladders, args, db_breakeven,
                          options=None):
    options = options or {}
    ws.title = "Simulator"
    ws.sheet_view.showGridLines = False
    headers = ["Campaign", "Sim target", "Δ vs current", "Conv/wk (sim)",
               "Spend/wk (sim)", "Conv value/wk (sim)", "Sim ROAS",
               "vs break-even", "Sim GP/wk (DB)",
               "Sim GP/wk (true econ)", "Addl budget/day",
               "Arc ε to next lower pt",
               "Model conv/wk @ same target", "Model − sim (conv, %)",
               "Path"]
    ws.append([f"Google Ads tROAS bid simulator — period "
               f"{SIMULATOR_PERIOD} — vs gradient model. Layer 3 "
               f"evidence: directional, single-week auction "
               f"counterfactual (holds query mix, competitors, budgets "
               f"fixed; excludes learning-phase transients)."])
    ws.cell(row=1, column=1).font = Font(bold=True, size=11)
    ws.append(["Arc ε between adjacent sim points = "
               "−Δln(conv)/Δln(target). GP shown in BOTH bases: DB "
               "terms and true econ (value ×(1+correction) − cost) — "
               "the Path A/B flags are computed on TRUE ECON, and the "
               "green-highlighted true-econ GP cell marks each "
               "campaign's peak (the flags' anchor). 'Model conv/wk' "
               "applies the Gradient ladder's ε to the same target — "
               "its divergence from sim conv IS the "
               "events-vs-simulator disagreement."])
    ws.cell(row=2, column=1).font = SOFT_FONT
    ws.append([])
    ws.append(headers)
    style_header(ws, 4, len(headers))

    r = 5
    for camp in campaigns:
        sim = sim_for(camp)
        if not sim:
            continue
        lad = ladders[camp["campaign_id"]]
        current = camp["target"]
        eps_model = lad["eps"]
        conv_day0 = lad["conv_day"]
        pts = sorted(sim["points"], key=lambda p: -p[0])
        local_eps = sim_local_elasticity(sim["points"], current)
        opt = options.get(camp["campaign_id"])
        corr = 1 + args.correction_pct / 100.0
        gp_true_max = max((p[3] * corr - p[1]) for p in pts if p[1] > 0)
        def nearest_flag(t):
            out = []
            for label, o in (("A", opt["a"] if opt else None),
                             ("B", opt.get("b") if opt else None)):
                if not o:
                    continue
                near = min(pts, key=lambda p: abs(math.log(p[0] / o["target"])))
                if abs(near[0] - t) < 1e-9:
                    out.append(f"◀{label} ({o['target']:.2f})")
            return " ".join(out)
        # header row for the campaign with last-week actuals
        lw = sim["last_week"]
        ws.append([f"{camp['name']}   —   sim current {current:.2f} · "
                   f"local ε(sim)={local_eps:.2f} vs ladder "
                   f"ε={eps_model:.2f} · last wk actual: "
                   f"{lw[1]} conv / {lw[0]:,.0f} spend / {lw[2]:,.0f} val"])
        ws.cell(row=r, column=1).fill = CAMP_FILL
        ws.cell(row=r, column=1).font = Font(bold=True, size=9)
        r += 1
        for i, (t, cost, conv, val, addl) in enumerate(pts):
            roas = val / cost if cost else None
            arc = None
            if i + 1 < len(pts):
                t2, _, conv2, _, _ = pts[i + 1]
                if conv > 0 and conv2 > 0 and t2 > 0:
                    arc = -math.log(conv / conv2) / math.log(t / t2)
            model_conv_wk = None
            delta_pct = None
            if conv_day0 and current:
                model_conv_wk = conv_day0 * 7 * (t / current) ** (-eps_model)
                if conv:
                    delta_pct = (model_conv_wk - conv) / conv
            is_cur = abs(t - current) < 1e-9
            ws.append(["",
                       t, (t - current) * 100,
                       conv, cost, val, roas,
                       ("BELOW" if (roas or 0) < db_breakeven else "above"),
                       (val - cost) if cost else None,
                       (val * (1 + args.correction_pct / 100.0) - cost)
                       if cost else None,
                       addl,
                       arc,
                       model_conv_wk, delta_pct, nearest_flag(t)])
            for c in range(2, len(headers) + 1):
                cell = ws.cell(row=r, column=c)
                cell.border = BORDER
                if c in (2, 7, 12):
                    cell.number_format = "0.00"
                elif c == 3:
                    cell.number_format = "+0;-0"
                elif c in (4, 5, 6, 9, 10, 11, 13):
                    cell.number_format = "#,##0"
                elif c == 14:
                    cell.number_format = "+0%;-0%"
            pf = ws.cell(row=r, column=15)
            if pf.value:
                pf.fill = STAR_FILL if "◀A" in str(pf.value) else BEN_FILL
                pf.font = Font(bold=True, size=9)
            gt_cell = ws.cell(row=r, column=10)
            if (gt_cell.value is not None
                    and abs(gt_cell.value - gp_true_max) < 0.01):
                gt_cell.fill = STAR_FILL
                gt_cell.font = Font(bold=True, size=9)
            if is_cur:
                for c in range(2, 9):
                    ws.cell(row=r, column=c).fill = CURR_FILL
            if roas is not None and roas < db_breakeven:
                ws.cell(row=r, column=7).fill = NEG_FILL
                ws.cell(row=r, column=8).fill = NEG_FILL
            r += 1
        r += 1
        ws.append([])

    widths = [46, 10, 11, 12, 12, 15, 9, 11, 12, 13, 12, 14, 16, 14, 12]
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w


def build_sim_compare_chart_png(campaigns, ladders):
    plot = [c for c in campaigns if sim_for(c)
            and ladders[c["campaign_id"]]["rungs"]]
    if not plot:
        return None
    ncols = 2
    nrows = (len(plot) + ncols - 1) // ncols
    fig, axes = plt.subplots(nrows, ncols, figsize=(11.5, 3.2 * nrows))
    axes = axes.flatten() if len(plot) > 1 else [axes]
    for ax, camp in zip(axes, plot):
        sim = sim_for(camp)
        lad = ladders[camp["campaign_id"]]
        current = camp["target"]
        pts = sorted([p for p in sim["points"] if p[0] <= 2.0],
                     key=lambda p: p[0])
        sx = [p[0] for p in pts]
        sy = [p[2] / 7.0 for p in pts]
        mx = [current * (0.70 + 0.02 * i) for i in range(31)]
        my = [lad["conv_day"] * (t / current) ** (-lad["eps"]) for t in mx]
        ax.plot(sx, sy, marker="o", markersize=5, color="#C55A11",
                linewidth=1.8, label="simulator (conv/day)")
        ax.plot(mx, my, color="#1F4E79", linewidth=1.8, linestyle="--",
                label=f"gradient model (ε={lad['eps']:.1f})")
        ax.axvline(current, color="#404040", linestyle="-.", linewidth=1.1,
                   label=f"current {current:.2f}")
        ax.set_title(short_name(camp["name"]), fontsize=9,
                     fontweight="bold")
        ax.set_xlabel("tROAS target", fontsize=7.5)
        ax.set_ylabel("conv/day", fontsize=7.5)
        ax.tick_params(labelsize=7)
        ax.grid(True, alpha=0.22)
        ax.legend(fontsize=6, loc="upper right", framealpha=0.85)
    for ax in axes[len(plot):]:
        ax.set_visible(False)
    fig.suptitle(f"Volume response: simulator ({SIMULATOR_PERIOD}, points "
                 f"≤2.0 shown) vs gradient model — the flatter simulator "
                 f"curve near current is Google's 'bigger moves needed' "
                 f"claim", fontsize=9.5)
    fig.tight_layout(rect=[0, 0, 1, 0.96])
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150)
    plt.close(fig)
    buf.seek(0)
    return buf



# ─────────────────────────────────────────────────────────────────────
# Recommendations — the one-page table (Sheet 1). Keyed by NAME token.
# These are analysis conclusions (8/13, events-vs-simulator two-run
# bracket); edit here as decisions evolve. Live numbers (current
# target, realized, GP) are filled from the run at build time.
# ─────────────────────────────────────────────────────────────────────
RECOMMENDATIONS = {
    # guidance text per campaign; Option A/B numbers are computed live
    # from the simulator curves (true economics) at build time.
    "TIER2RPL": {
        "guidance": "WAVE 1 mover either path. Both instruments agree; "
                    "biggest tier by spend, healthiest cushion. Its "
                    "post-change read calibrates ε for the Wave-2 moves.",
        "rollback": "Banded protocol — see Guardrails sheet",
    },
    "TEXAS": {
        "guidance": "WAVE 1 mover on the volume path (instruments "
                    "agree, modest move). On the GP path: hold — sim "
                    "true-econ GP already peaks at current.",
        "rollback": "Banded protocol — see Guardrails sheet",
    },
    "CALIFORNIA": {
        "guidance": "WAVE 2 (after ε calibrates on T2/TX read). Volume "
                    "is cheap here — sim true-econ GP peaks BELOW "
                    "current — but the Option B floor is a big move: "
                    "stage in Layer-4 steps (e.g. 1.17→1.05→…), never "
                    "one jump.",
        "rollback": "Banded protocol — see Guardrails sheet",
    },
    "TIER3RPL": {
        "guidance": "WAVE 2. Rare dominant option: chasing GP IS a "
                    "volume move (sim true-econ peak sits below "
                    "current). Option B floor is deep — stage in steps.",
        "rollback": "Banded protocol — see Guardrails sheet",
    },
    "TIER4RPL": {
        "guidance": "GP path only — the one campaign whose sim GP peak "
                    "is clearly ABOVE current in both bases; loosening "
                    "fails even a 5%% tolerance almost immediately. "
                    "Exclude from any volume package.",
        "rollback": "Revert to 1.17 if 7d conv/day falls >40% vs "
                    "post-freeze baseline",
    },
    "TIER1RPL": {
        "guidance": "NEITHER path via target: sim curve is GP-negative "
                    "(true econ) across nearly its whole range — "
                    "least-bad is the range top, barely break-even. "
                    "State surgery (NJ/MA per State Decomp) is the "
                    "lever; re-simulate after dispositions.",
        "rollback": "n/a (no target change recommended)",
    },
    "FLORIDA": {
        "guidance": "Immaterial spend (~$33/day) — numbers shown for "
                    "completeness; not worth a change slot on either "
                    "path.",
        "rollback": "n/a",
    },
}


def write_recommendation_sheet(ws, campaigns, ladders, args,
                               db_breakeven, win_start, win_end):
    ws.title = "Recommendation"
    ws.sheet_view.showGridLines = False
    tol = args.gp_tolerance
    headers = ["Campaign", "Current target", "Post-freeze realized",
               "Sim GP/wk (true econ)",
               "A: Chase GP — target", "A: Δ conv", "A: Δ GP (true)",
               f"B: Chase volume (≤{tol:g}% GP give) — target",
               "B: Δ conv", "B: Δ GP (true)",
               "Guidance / sequencing", "Rollback trigger"]
    ws.append([f"tROAS target options — Google Home — TWO PATHS from the "
               f"Google bid-simulator curves ({SIMULATOR_PERIOD}) in TRUE "
               f"economics (value ×(1+{args.correction_pct:g}%) − cost): "
               f"A = target at the GP-optimal point; B = lowest target "
               f"holding GP within {tol:g}% of current."])
    ws.cell(row=1, column=1).font = Font(bold=True, size=11)
    ws.append(["Options interpolated within Google's plotted range only "
               "('range edge' = optimum may lie beyond the plotted "
               "curve). Single-week counterfactual — directional. "
               "Moves >1 Layer-4 step (15-20% relative) must be staged; "
               "batch each wave same-day, ≥1 week between waves, "
               f"realized window {win_start} → {win_end}."])
    ws.cell(row=2, column=1).font = SOFT_FONT
    ws.append([])
    ws.append(headers)
    style_header(ws, 4, len(headers))

    order_key = ["TIER2RPL", "TEXAS", "CALIFORNIA", "TIER3RPL",
                 "TIER4RPL", "TIER1RPL", "FLORIDA"]
    def rank(c):
        u = c["name"].upper()
        for i, tok in enumerate(order_key):
            if tok in u:
                return i
        return 99
    r = 5
    for camp in sorted(campaigns, key=rank):
        rec = None
        for token, v in RECOMMENDATIONS.items():
            if token in camp["name"].upper():
                rec = v
                break
        lad = ladders[camp["campaign_id"]]
        sim = sim_for(camp)
        opts = (compute_sim_options(sim, camp["target"],
                                    args.correction_pct, tol)
                if sim and camp["target"] else None)
        def fmt_t(o):
            if not o:
                return "n/a"
            s = f"{o['target']:.2f}"
            if o.get("edge"):
                s += " (range edge)"
            rel = o["target"] / camp["target"] - 1
            if abs(rel) > 0.20:
                s += " — STAGE"
            return s
        a = opts["a"] if opts else None
        b = opts["b"] if opts else None
        ws.append([
            camp["name"],
            camp["target"],
            lad["realized"],
            opts["gp0_wk"] if opts else None,
            fmt_t(a),
            a["dconv"] if a else None,
            a["dgp"] if (a and a["dgp"] is not None) else None,
            fmt_t(b) if b else "n/a (GP-negative at current)"
            if opts and opts["gp0_wk"] <= 0 else fmt_t(b),
            b["dconv"] if b else None,
            b["dgp"] if b else None,
            rec["guidance"] if rec else "",
            rec["rollback"] if rec else "",
        ])
        for c in range(1, len(headers) + 1):
            cell = ws.cell(row=r, column=c)
            cell.border = BORDER
            cell.alignment = Alignment(wrap_text=True, vertical="top")
            if c in (2, 3):
                cell.number_format = "0.00"
            elif c == 4:
                cell.number_format = "#,##0"
            elif c in (6, 7, 9, 10):
                cell.number_format = "+0.0%;-0.0%"
        ws.cell(row=r, column=1).font = Font(bold=True, size=9)
        # tint the two option blocks
        for c in (5, 6, 7):
            ws.cell(row=r, column=c).fill = STAR_FILL
        for c in (8, 9, 10):
            ws.cell(row=r, column=c).fill = BEN_FILL
        if opts and opts["gp0_wk"] <= 0:
            ws.cell(row=r, column=4).fill = NEG_FILL
        r += 1
    widths = [40, 9, 11, 12, 17, 9, 10, 20, 9, 10, 46, 34]
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = "A5"



GUARDRAIL_PROTOCOL = [
    ("GRACE (days 1-4)", "Monitor only — learning-period noise expected. "
     "Act only if catastrophic: spend >2x baseline AND lag-mature ROAS "
     "< 0.90 (true break-even)."),
    ("AMBER — watch", "Spend/day > 1.5x EXPECTED post-change spend "
     "(expected = baseline x sim volume multiplier — growth per plan is "
     "not overshoot), OR lag-mature rolling-5d ROAS > 0.10 below "
     "expected (= delivery ratio x new target). Action: verify vs UI "
     "lag-adjusted view, recheck in 48h, change nothing."),
    ("RED — step back", "Lag-mature rolling-7d ROAS below the "
     "campaign's RED line (= expected ROAS - 0.15, floored 0.93, capped "
     "1.05) sustained 5+ days past grace. Action: revert ONE rung, not "
     "to the original target (a full snap-back is itself a 15%+ move "
     "that restarts learning)."),
    ("BLACK — circuit breaker", "Lag-mature ROAS < 0.90 for 3 days, OR "
     "true-econ GP/day negative 5 consecutive days, OR spend >2x "
     "expected with ROAS < 1.0. Action: BUDGET CAP FIRST (stops the "
     "bleed without resetting bid learning), then revert."),
]


def write_guardrails_sheet(ws, campaigns, ladders, args):
    ws.title = "Guardrails"
    ws.sheet_view.showGridLines = False
    ws.append(["tROAS overshoot defense — banded protocol. Evaluate on "
               "LAG-MATURE data only (exclude trailing "
               f"{args.lag_days} days); anchor day counts on the logged "
               "TARGET_ROAS event timestamp. Daily check: "
               "troas_guardrail_monitor.py."])
    ws.cell(row=1, column=1).font = Font(bold=True, size=11)
    r = 2
    for band, rule in GUARDRAIL_PROTOCOL:
        ws.append([band, rule])
        ws.cell(row=r, column=1).font = Font(bold=True, size=9)
        ws.cell(row=r, column=2).alignment = Alignment(wrap_text=True,
                                                       vertical="top")
        r += 1
    ws.append([])
    r += 1
    headers = ["Campaign", "If moved to (Option B)", "Expected ROAS",
               "Expected spend/day", "AMBER spend/day >",
               "AMBER 5d ROAS <", "RED 7d ROAS <", "BLACK ROAS <"]
    ws.append(headers)
    style_header(ws, r, len(headers))
    r += 1
    n_days = None
    for camp in campaigns:
        lad = ladders[camp["campaign_id"]]
        sim = sim_for(camp)
        if not (sim and camp["target"] and lad["ratio"]):
            continue
        opts = compute_sim_options(sim, camp["target"],
                                   args.correction_pct, args.gp_tolerance)
        move = opts.get("b") if opts else None
        if not move:
            continue
        t_new = move["target"]
        exp_roas = lad["ratio"] * t_new
        base = _sim_interp(sim["points"], camp["target"])
        new = _sim_interp(sim["points"], t_new)
        spend_mult = (new[0] / base[0]) if (base and new) else \
            (t_new / camp["target"]) ** (-2.5)
        exp_spend = lad["spend_day"] * spend_mult
        red = min(1.05, max(0.93, exp_roas - 0.15))
        ws.append([camp["name"], f"{t_new:.2f}", exp_roas, exp_spend,
                   1.5 * exp_spend, exp_roas - 0.10, red, 0.90])
        for c in range(2, len(headers) + 1):
            cell = ws.cell(row=r, column=c)
            cell.border = BORDER
            if c in (3, 6, 7, 8):
                cell.number_format = "0.00"
            elif c in (4, 5):
                cell.number_format = "#,##0"
        ws.cell(row=r, column=1).font = Font(bold=True, size=9)
        r += 1
    ws.column_dimensions["A"].width = 46
    for col, w in zip("BCDEFGH", [16, 12, 14, 14, 13, 12, 11]):
        ws.column_dimensions[col].width = w
    ws.column_dimensions["B"].width = 16


# ─────────────────────────────────────────────────────────────────────
# Workbook
# ─────────────────────────────────────────────────────────────────────

def style_header(ws, row, cols):
    for c in range(1, cols + 1):
        cell = ws.cell(row=row, column=c)
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.border = BORDER
        cell.alignment = Alignment(horizontal="center")


def write_gradient_sheet(ws, campaigns, ladders, args, db_breakeven,
                         win_start, win_end, options=None):
    options = options or {}
    ws.title = "Gradient"
    ws.sheet_view.showGridLines = False
    headers = ["Campaign", "Rung (pts)", "Target",
               "Expected ROAS", "vs break-even",
               "Modeled conv/day", "Modeled spend/day",
               "Modeled GP/day (DB)", "Modeled GP/day (true econ)",
               "Modeled conv/wk", "Modeled spend/wk",
               "Modeled conv value/wk", "Modeled GP/wk (DB)",
               "GP-max", "Path", "Elasticity source", "Target source",
               "Actual: realized", "Actual: conv (win)",
               "Actual: spend (win)", "Actual: GP (win)",
               "Rollback trigger"]
    ws.append([f"tROAS Target Gradient — Google Home — window {win_start} → "
               f"{win_end} (trailing {args.lag_days}d lag-soft excluded) — "
               f"PROVISIONAL until a full clean post-freeze week"])
    ws.cell(row=1, column=1).font = Font(bold=True, size=11)
    ws.append([f"Volume model: mult = (t/current)^(-ε). Modeled columns are "
               f"directional; weekly = daily × 7; value/wk = spend/wk × "
               f"expected ROAS. Break-even (DB) = "
               f"1/(1+{args.correction_pct:g}%) = {db_breakeven:.3f}. "
               f"★ = modeled GP-max rung (true econ). Path column flags "
               f"the Recommendation sheet's options: ◀A = chase-GP "
               f"target, ◀B = chase-volume floor; 'A@x.xx↓/↑' on the "
               f"first row = that option lies beyond the ±"
               f"{args.span_points:g}pt ladder. Actual columns are the "
               f"window at the CURRENT target — identical on every rung "
               f"by construction."])
    ws.cell(row=2, column=1).font = SOFT_FONT
    ws.append([])
    ws.append(headers)
    style_header(ws, 4, len(headers))

    half_step = args.step_points / 200.0
    r = 5
    for camp in campaigns:
        lad = ladders[camp["campaign_id"]]
        opt = options.get(camp["campaign_id"])
        a = opt["a"] if opt else None
        b = opt.get("b") if opt else None
        if not lad["rungs"]:
            ws.append([camp["name"], "—", None, None, "target unresolved"]
                      + [None] * 8 + ["", "", "", camp["target_source"],
                      lad["realized"], lad["conv"], lad["cost"],
                      lad["gp"], ""])
            ws.cell(row=r, column=1).fill = CAMP_FILL
            r += 2
            ws.append([])
            continue
        # options that fall outside the ladder get noted on row 1
        rung_ts = [x["target"] for x in lad["rungs"]]
        def outside(o):
            return o and not any(abs(t - o["target"]) <= half_step
                                 for t in rung_ts)
        notes = []
        if outside(a):
            arrow = "↓" if a["target"] < min(rung_ts) else "↑"
            notes.append(f"A@{a['target']:.2f}{arrow}")
        if b and outside(b):
            arrow = "↓" if b["target"] < min(rung_ts) else "↑"
            notes.append(f"B@{b['target']:.2f}{arrow}")
        first = True
        for rung in lad["rungs"]:
            rollback = ""
            if first and lad["realized"]:
                rollback = "Banded protocol — see Guardrails sheet"
            flags = []
            if a and abs(rung["target"] - a["target"]) <= half_step:
                flags.append("◀A")
            if b and abs(rung["target"] - b["target"]) <= half_step:
                flags.append("◀B")
            path_cell = " ".join(flags)
            if first and notes:
                path_cell = (path_cell + " " if path_cell else "") + \
                    " ".join(notes)
            conv_wk = rung["e_conv_day"] * 7
            spend_wk = rung["e_spend_day"] * 7
            value_wk = rung["e_spend_day"] * rung["expected_roas"] * 7
            gp_wk = rung["e_gp_day_db"] * 7
            ws.append([
                camp["name"] if first else "",
                f"{rung['points']:+g}" + (" (current)" if rung["is_current"]
                                          else " (Ben)" if rung["is_ben"] else ""),
                rung["target"],
                rung["expected_roas"],
                "BELOW" if rung["below_breakeven"] else "above",
                rung["e_conv_day"],
                rung["e_spend_day"],
                rung["e_gp_day_db"],
                rung["e_gp_day_true"],
                conv_wk, spend_wk, value_wk, gp_wk,
                ("★ edge→" if rung.get("gp_max_edge") else "★")
                if rung["is_gp_max"] else "",
                path_cell,
                lad["eps_label"] if first else "",
                camp["target_source"] if first else "",
                lad["realized"],
                lad["conv"],
                lad["cost"],
                lad["gp"],
                rollback,
            ])
            for c in range(1, len(headers) + 1):
                cell = ws.cell(row=r, column=c)
                cell.border = BORDER
                if c in (3, 4, 18):
                    cell.number_format = "0.00"
                elif c in (6, 10):
                    cell.number_format = "0.0"
                elif c in (7, 8, 9, 11, 12, 13, 19, 20, 21):
                    cell.number_format = "#,##0"
            if first:
                ws.cell(row=r, column=1).fill = CAMP_FILL
                ws.cell(row=r, column=1).font = Font(bold=True, size=9)
            fill = (CURR_FILL if rung["is_current"]
                    else BEN_FILL if rung["is_ben"] else None)
            if fill:
                for c in range(2, 14):
                    ws.cell(row=r, column=c).fill = fill
            if rung["is_gp_max"]:
                ws.cell(row=r, column=14).fill = STAR_FILL
                ws.cell(row=r, column=9).fill = STAR_FILL
            if flags:
                ws.cell(row=r, column=15).fill = (
                    STAR_FILL if "◀A" in flags else BEN_FILL)
                ws.cell(row=r, column=15).font = Font(bold=True, size=9)
            if rung["below_breakeven"]:
                ws.cell(row=r, column=4).fill = NEG_FILL
                ws.cell(row=r, column=5).fill = NEG_FILL
            first = False
            r += 1
        r += 1
        ws.append([])

    widths = [46, 12, 8, 10, 11, 11, 12, 13, 13, 10, 11, 12, 11, 8, 12,
              26, 20, 11, 11, 12, 11, 34]
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w


def write_state_sheet(ws, campaigns, states):
    ws.title = "State Decomp"
    ws.sheet_view.showGridLines = False
    headers = ["Campaign", "State", "Spend", "Spend share", "Conv",
               "Revenue (DB $)", "Realized ROAS", "GP (DB $)"]
    ws.append(headers)
    style_header(ws, 1, len(headers))
    r = 2
    for camp in campaigns:
        rows = states.get(camp["campaign_id"], [])
        total_cost = sum(x["cost"] or 0 for x in rows)
        rows.sort(key=lambda x: (x["revenue"] or 0) - (x["cost"] or 0))
        first = True
        for x in rows:
            cost, rev = x["cost"] or 0, x["revenue"] or 0
            gp = rev - cost
            ws.append([camp["name"] if first else "", x["geo_state"], cost,
                       safe_div(cost, total_cost), x["conv"], rev,
                       safe_div(rev, cost), gp])
            for c in range(1, len(headers) + 1):
                cell = ws.cell(row=r, column=c)
                cell.border = BORDER
                if c in (3, 5, 6, 8):
                    cell.number_format = "#,##0"
                elif c == 4:
                    cell.number_format = "0.0%"
                elif c == 7:
                    cell.number_format = "0.00"
            if first:
                ws.cell(row=r, column=1).fill = CAMP_FILL
                ws.cell(row=r, column=1).font = Font(bold=True, size=9)
            if gp < 0:
                ws.cell(row=r, column=8).fill = NEG_FILL
            first = False
            r += 1
        r += 1
        ws.append([])
    for i, w in enumerate([46, 8, 10, 11, 8, 12, 12, 10], start=1):
        ws.column_dimensions[get_column_letter(i)].width = w


def write_notes_sheet(ws, args, win_start, win_end, soft_read,
                      db_breakeven, campaigns, ladders, pooled_ev=None):
    ws.title = "Notes"
    lines = [
        ("Run", datetime.now().strftime("%Y-%m-%d %H:%M")),
        ("Realized window", f"{win_start} → {win_end} (trailing "
                            f"{args.lag_days}d excluded as lag-soft)"),
        ("Provisional flag", "numbers provisional until a full clean "
                             "Mon–Sun post-freeze week exists"),
        ("Target authority", "campaign_change_events latest TARGET_ROAS "
                             "per campaign_id; snapshot fallback. "
                             "Cutover-day portfolio detachments may not "
                             "emit events — spot-check vs UI once."),
        ("Revenue basis", "campaign_performance_daily_search."
                          "conversion_value, device='ALL' (ruled basis)"),
        ("State basis", "campaign_performance_geo_daily_search, device "
                        "rows summed (no ALL rollup)"),
        ("ROAS response", "expected ROAS = delivery ratio × candidate "
                          "target (post-freeze ratio held constant)"),
        ("Volume model", f"mult = (t/current)^(-ε); ε per campaign from "
                         f"the TARGET_ROAS event study (−Δln(conv/day)/"
                         f"Δln(target), pre {args.pre_days}d vs post "
                         f"{args.post_days}d skipping 2 ramp days, post "
                         f"window truncated at the next event; weighted "
                         f"median, clamped {EPS_MIN}–{EPS_MAX}); "
                         f"fallback campaign→account-pooled→default "
                         f"ε={args.elasticity:g}. Events carry seasonal/"
                         f"market confounds — DIRECTIONAL ONLY; the UI "
                         f"simulator (Layer 3) is the arbiter; reconcile "
                         f"vs elasticity_regression_google.py."),
        ("GP-max ★", f"rung maximizing modeled GP/day in true economics "
                     f"(revenue × (1+{args.correction_pct:g}%) − spend); "
                     f"displayed GP/day column stays in DB terms. "
                     f"'★ edge→' means the maximum sits at the ladder "
                     f"boundary — the modeled optimum lies beyond the "
                     f"±span, so widen --span-points or treat as 'keep "
                     f"moving in this direction in scheduled steps'"),
        ("Correction", f"{args.correction_pct:g}% applied to break-even "
                       f"threshold (1/(1+c) = {db_breakeven:.3f}) and the "
                       f"GP-max decision only; DB realized never restated"),
        ("Schedule (Layer 4)", "one move per campaign, ≥1 week apart, "
                               "batched same-day; TARGET_ROAS events "
                               "auto-land in change history"),
        ("Soft trailing window", soft_read),
        ("Simulator (Layer 3)", f"UI tROAS simulator, period "
                                f"{SIMULATOR_PERIOD}, transcribed from "
                                f"screenshots 8/12. Single-week auction "
                                f"counterfactual: holds query mix, "
                                f"competitors and budgets fixed, excludes "
                                f"learning-phase transients and feedback "
                                f"— a likely FLOOR on responsiveness. "
                                f"Event-study ε (May/June episodes) is a "
                                f"likely CEILING (coincident controller/"
                                f"market moves attributed to the target "
                                f"change). Local sim ε near current runs "
                                f"~1.4-2.3 vs event-study ~4; plan "
                                f"multi-week moves between the two, "
                                f"validate with the first clean "
                                f"post-change read."),
        ("Target overrides", "UI-simulator currents are authority where "
                             "available (disable with --no-sim-targets); "
                             "change-event targets were stale for T3/T4 "
                             "(portfolio-detachment events missing). "
                             "8/13 fix: simulator data keyed by campaign "
                             "NAME (screenshot titles), after the stale "
                             "V2 id-comment mapping rotated the four "
                             "tier assignments in the 8/13 morning run."),
    ]
    for c in campaigns:
        lad = ladders[c["campaign_id"]]
        t = f"{c['target']:.2f}" if c["target"] is not None else "—"
        lines.append((f"{short_name(c['name'])}",
                      f"target {t} [{c['target_source']}] · {lad['eps_label']}"))
    for r in sorted(pooled_ev or [], key=lambda x: (x["campaign_id"], x["date"])):
        lines.append((f"Event {r['date']}",
                      f"{short_name_by_id(campaigns, r['campaign_id'])}: "
                      f"{r['old']:.2f}→{r['new']:.2f}, conv/day "
                      f"{r['pre']:.1f}→{r['post']:.1f}, implied ε="
                      f"{r['eps']:+.2f} (pre n={r['n_pre']}, post "
                      f"n={r['n_post']})"
                      + ("" if 0 < r["eps"] <= EPS_MAX else
                         " — CONFOUNDED (outside 0–%g), excluded from "
                         "pooling" % EPS_MAX)))
    for k, v in lines:
        ws.append([k, v])
        ws.cell(row=ws.max_row, column=1).font = Font(bold=True, size=9)
    ws.column_dimensions["A"].width = 26
    ws.column_dimensions["B"].width = 115
    for row in ws.iter_rows():
        row[1].alignment = Alignment(wrap_text=True, vertical="top")


# ─────────────────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--db-path", default=str(DEFAULT_DB_PATH))
    p.add_argument("--window-start", default="2026-07-30")
    p.add_argument("--lag-days", type=int, default=3)
    p.add_argument("--span-points", type=float, default=4.0)
    p.add_argument("--step-points", type=float, default=1.0)
    p.add_argument("--ben-points", type=float, default=3.0)
    p.add_argument("--correction-pct", type=float, default=11.5)
    p.add_argument("--gp-tolerance", type=float, default=5.0,
                   help="Option B: max %% true-econ GP give-up when "
                        "chasing volume")
    p.add_argument("--elasticity", type=float, default=4.0,
                   help="fallback ε when no usable events exist "
                        "(default anchored on the May/June episodes)")
    p.add_argument("--pre-days", type=int, default=14)
    p.add_argument("--post-days", type=int, default=14)
    p.add_argument("--min-move-pct", type=float, default=1.0,
                   help="minimum |relative target move| for an event "
                        "to count, in percent")
    p.add_argument("--no-fit", action="store_true",
                   help="skip event study; use --elasticity everywhere")
    p.add_argument("--no-sim-targets", action="store_true",
                   help="do not override targets with UI-simulator "
                        "currents")
    p.add_argument("--eps-source", choices=["events", "simulator"],
                   default="events",
                   help="elasticity for the Gradient ladder: event "
                        "study (default) or simulator local arc ε")
    p.add_argument("--output", default=None)
    args = p.parse_args()

    db_path = Path(args.db_path)
    if not db_path.exists():
        sys.exit(f"DB not found: {db_path}")
    conn = connect_ro(db_path)

    db_breakeven = 1.0 / (1.0 + args.correction_pct / 100.0)

    campaigns = fetch_campaigns(conn)
    if not campaigns:
        sys.exit("No campaigns resolved — check ENABLED_CAMPAIGN_IDS.")
    fetch_latest_targets(conn, campaigns)
    if not args.no_sim_targets:
        apply_simulator_targets(campaigns)

    max_date = max_data_date(conn)
    win_start = datetime.strptime(args.window_start, "%Y-%m-%d").date()
    win_end = max_date - timedelta(days=args.lag_days)
    if win_end <= win_start:
        sys.exit(f"Window empty: {win_start} → {win_end}")
    n_days = (win_end - win_start).days + 1
    print(f"Realized window: {win_start} → {win_end} ({n_days}d; max data "
          f"date {max_date}, trailing {args.lag_days}d soft)")

    ids = [c["campaign_id"] for c in campaigns]
    perf = campaign_perf(conn, ids, win_start, win_end)
    states = state_perf(conn, ids, win_start, win_end)
    if args.no_fit:
        per_camp_ev, pooled_ev = {}, []
    else:
        per_camp_ev, pooled_ev = event_study_elasticities(
            conn, ids, args, win_end)
        if pooled_ev:
            print(f"\nEvent study — {len(pooled_ev)} usable TARGET_ROAS "
                  f"events:")
            for r in sorted(pooled_ev, key=lambda x: x["date"]):
                print(f"  {r['date']}  {short_name_by_id(campaigns, r['campaign_id']):12s} "
                      f"{r['old']:.2f}→{r['new']:.2f}  conv/day "
                      f"{r['pre']:.1f}→{r['post']:.1f}  implied ε="
                      f"{r['eps']:+.2f}  (pre n={r['n_pre']}, post "
                      f"n={r['n_post']})")
        else:
            print("\nEvent study: no usable TARGET_ROAS events — all "
                  "campaigns will use the default ε.")
    soft = campaign_perf(conn, ids, win_end + timedelta(days=1), max_date)
    soft_cost = sum(v["cost"] or 0 for v in soft.values())
    soft_rev = sum(v["revenue"] or 0 for v in soft.values())
    soft_read = (f"{win_end + timedelta(days=1)} → {max_date}: spend "
                 f"{soft_cost:,.0f}, revenue {soft_rev:,.0f}, ROAS "
                 f"{safe_div(soft_rev, soft_cost) or 0:.2f} (SOFT)")

    ladders = {}
    print(f"\nGradient (break-even {db_breakeven:.3f}):")
    for camp in campaigns:
        if args.no_fit:
            eps, eps_label = args.elasticity, f"default ε={args.elasticity:.2f} (--no-fit)"
        elif args.eps_source == "simulator":
            sim = sim_for(camp)
            se = sim_local_elasticity(sim["points"], camp["target"]) if sim else None
            if se and se > 0:
                eps = min(max(se, EPS_MIN), EPS_MAX)
                eps_label = f"simulator local ε={eps:.2f} ({SIMULATOR_PERIOD})"
            else:
                eps, eps_label = pick_elasticity(
                    camp["campaign_id"], per_camp_ev, pooled_ev,
                    args.elasticity)
        else:
            eps, eps_label = pick_elasticity(
                camp["campaign_id"], per_camp_ev, pooled_ev,
                args.elasticity)
        lad = build_ladder(camp, perf.get(camp["campaign_id"], {}),
                           args, db_breakeven, n_days, eps, eps_label)
        ladders[camp["campaign_id"]] = lad
        rz = f"{lad['realized']:.2f}" if lad["realized"] else "—"
        gp_max = next((r for r in lad["rungs"] if r["is_gp_max"]), None)
        gm = (f"GP-max {gp_max['points']:+g}pt"
              + (" (EDGE — optimum beyond ladder span)"
                 if gp_max.get("gp_max_edge") else "")) if gp_max else "no ladder"
        print(f"  {short_name(camp['name']):12s} target "
              f"{camp['target'] if camp['target'] else 0:.2f} "
              f"[{camp['target_source']}] realized {rz}  {lad['eps_label']}"
              f"  {gm}")

    options = {}
    for camp in campaigns:
        sim = sim_for(camp)
        if sim and camp["target"]:
            options[camp["campaign_id"]] = compute_sim_options(
                sim, camp["target"], args.correction_pct,
                args.gp_tolerance)

    wb = Workbook()
    write_recommendation_sheet(wb.active, campaigns, ladders, args,
                               db_breakeven, win_start, win_end)
    charts_ws = wb.create_sheet()
    charts_ws.title = "Charts"
    charts_ws.sheet_view.showGridLines = False
    png = build_gradient_chart_png(campaigns, ladders, args, db_breakeven)
    if png:
        charts_ws.add_image(XLImage(png), "A1")
    else:
        charts_ws["A1"] = "No plottable campaigns (targets unresolved)."

    write_gradient_sheet(wb.create_sheet(), campaigns, ladders, args,
                         db_breakeven, win_start, win_end, options)
    sim_ws = wb.create_sheet()
    write_simulator_sheet(sim_ws, campaigns, ladders, args, db_breakeven,
                          options)
    cmp_png = build_sim_compare_chart_png(campaigns, ladders)
    if cmp_png:
        anchor_row = sim_ws.max_row + 2
        sim_ws.add_image(XLImage(cmp_png), f"A{anchor_row}")
    write_guardrails_sheet(wb.create_sheet(), campaigns, ladders, args)
    write_state_sheet(wb.create_sheet(), campaigns, states)
    write_notes_sheet(wb.create_sheet(), args, win_start, win_end,
                      soft_read, db_breakeven, campaigns, ladders,
                      pooled_ev)

    out = args.output or f"troas_target_gradient_{date.today().isoformat()}.xlsx"
    wb.save(out)
    conn.close()
    print(f"\nSaved: {out}")


if __name__ == "__main__":
    main()
