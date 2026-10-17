"""
tROAS Guardrail Monitor — Google Home Insurance
═══════════════════════════════════════════════════════════════════════
10-second morning check for post-target-change overshoot. For every
campaign with a TARGET_ROAS change in the last --window days (found
automatically in campaign_change_events — the changes auto-log, so day
counts anchor on exact timestamps, not memory), evaluates the banded
defense protocol on LAG-MATURE data only and prints one status line:

  GRACE  — inside the first --grace-days post-change; monitor only
  OK     — tracking within expectations
  AMBER  — watch: spend >1.5x expected, or mature 5d ROAS >0.10 below
           expected. Verify vs the UI lag-adjusted view; recheck 48h.
  RED    — step back ONE rung: mature 7d ROAS below the campaign RED
           line (= expected ROAS − 0.15, floored 0.93, capped 1.05)
           for 5+ mature days past grace. Never snap back fully — a
           full revert is itself a 15%+ move that restarts learning.
  BLACK  — circuit breaker: mature 3d ROAS < 0.90 (true break-even),
           OR true-econ GP/day negative 5 consecutive mature days, OR
           spend >2x expected with ROAS < 1.0. BUDGET CAP FIRST (stops
           the bleed without resetting bid learning), then revert.

Expectations are derived from the campaign's own pre-change baseline
(14d before the event): expected ROAS = (baseline ROAS / old target) x
new target; expected spend = baseline spend x volume multiplier
(simulator-interpolated where available, else (new/old)^-2.5).

Data rules: campaign_performance_daily_search, device='ALL', revenue =
conversion_value, summed numerators; lag-mature = excludes trailing
--lag-days (default 3); DB opened read-only. Rationale for the dwell
times: tROAS overshoot usually self-corrects (June 2026 controller
correction being the house case study) — reacting mid-correction
whipsaws the controller.

USAGE:
  python troas_guardrail_monitor.py
  python troas_guardrail_monitor.py --window 28 --lag-days 4
  python troas_guardrail_monitor.py --correction-pct 13
"""

import argparse
import math
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

DEFAULT_DB_PATH = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")
ACCOUNT_ID = 1

ENABLED_CAMPAIGN_IDS = [
    "23385188622",   # CALIFORNIA-NB
    "23394808297",   # TEXAS-NB
    "23394808300",   # FLORIDA-NB
    "23428389081",   # TIER4RPL (legacy shell re-enabled as T4 at cutover)
    "23496334752",   # TIER1RPL
    "23530860942",   # TIER2RPL
    "23586637678",   # TIER3RPL
]

# simulator spend curves (Aug 2-8 2026 UI screenshots) — used only for
# the expected-spend multiplier; keyed by NAME token.
SIM_SPEND = {
    "CALIFORNIA": [(4.68, 9.95), (3.51, 60.04), (2.34, 180.42),
                   (1.75, 455.24), (1.46, 832.17), (1.29, 1436.79),
                   (1.17, 2115.90), (1.05, 2800.91), (0.88, 4546.47),
                   (0.58, 14469.43)],
    "FLORIDA": [(4.48, 1.02), (3.36, 3.62), (2.24, 17.06), (1.68, 37.86),
                (1.40, 77.42), (1.23, 114.94), (1.12, 197.43),
                (1.01, 242.22), (0.84, 419.64), (0.56, 1873.51)],
    "TEXAS": [(4.48, 11.94), (3.36, 43.15), (2.24, 122.49),
              (1.68, 349.69), (1.40, 706.20), (1.23, 1243.94),
              (1.12, 2155.02), (1.01, 3100.54), (0.84, 5136.85),
              (0.56, 18425.09)],
    "TIER1RPL": [(1.34, 1534.03), (1.22, 2182.31), (1.15, 2832.98),
                 (1.07, 3455.72), (1.02, 4087.53), (0.96, 4816.27),
                 (0.93, 5303.58), (0.85, 6173.06), (0.84, 7385.96),
                 (0.79, 9065.58)],
    "TIER2RPL": [(4.68, 37.42), (3.51, 160.31), (2.34, 374.50),
                 (1.75, 923.31), (1.46, 1665.68), (1.29, 2943.03),
                 (1.17, 4437.28), (1.05, 5796.68), (0.88, 9302.44),
                 (0.58, 31461.71)],
    "TIER3RPL": [(1.41, 719.76), (1.25, 1054.01), (1.17, 1364.37),
                 (1.07, 1852.42), (1.01, 2262.53), (0.98, 2678.78),
                 (0.91, 3012.48), (0.90, 3440.42), (0.86, 4020.88),
                 (0.81, 4942.58)],
    "TIER4RPL": [(4.68, 5.20), (3.51, 21.03), (2.34, 60.20),
                 (1.75, 184.31), (1.46, 325.01), (1.29, 544.70),
                 (1.17, 862.48), (1.05, 1178.34), (0.88, 1926.64),
                 (0.58, 7530.52)],
}


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


def spend_mult(name, t_old, t_new):
    token = next((k for k in SIM_SPEND if k in name.upper()), None)
    if token:
        pts = sorted(SIM_SPEND[token])

        def interp(t):
            for (t1, s1), (t2, s2) in zip(pts, pts[1:]):
                if t1 <= t <= t2:
                    f = (math.log(t) - math.log(t1)) / (math.log(t2) - math.log(t1))
                    return math.exp(math.log(s1) + f * (math.log(s2) - math.log(s1)))
            return None
        a, b = interp(t_old), interp(t_new)
        if a and b:
            return b / a
    return (t_new / t_old) ** (-2.5)


def daily_series(conn, cid, start, end):
    rows = conn.execute(
        """SELECT date, SUM(cost_micros)/1e6 AS cost,
                  SUM(conversions) AS conv, SUM(conversion_value) AS rev
           FROM campaign_performance_daily_search
           WHERE campaign_id = ? AND device = 'ALL'
             AND date BETWEEN ? AND ?
           GROUP BY date ORDER BY date""",
        [cid, start.isoformat(), end.isoformat()]).fetchall()
    return [(datetime.strptime(str(r["date"])[:10], "%Y-%m-%d").date(),
             r["cost"] or 0, r["conv"] or 0, r["rev"] or 0) for r in rows]


def window_roas(series, days):
    tail = series[-days:] if len(series) >= 1 else []
    cost = sum(x[1] for x in tail)
    rev = sum(x[3] for x in tail)
    return (rev / cost if cost else None), cost / max(len(tail), 1)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--db-path", default=str(DEFAULT_DB_PATH))
    p.add_argument("--window", type=int, default=21,
                   help="look back this many days for TARGET_ROAS moves")
    p.add_argument("--lag-days", type=int, default=0,
                   help="default 0 per 8/20 ruling: recent data treated as complete (lag-restatement unproven); set 3 if the snapshot test ever demonstrates the lag")
    p.add_argument("--grace-days", type=int, default=4)
    p.add_argument("--correction-pct", type=float, default=11.5)
    args = p.parse_args()

    db_path = Path(args.db_path)
    if not db_path.exists():
        sys.exit(f"DB not found: {db_path}")
    conn = connect_ro(db_path)
    corr = 1 + args.correction_pct / 100.0

    ph = ",".join("?" for _ in ENABLED_CAMPAIGN_IDS)
    max_row = conn.execute(
        f"""SELECT MAX(date) FROM campaign_performance_daily_search
            WHERE campaign_id IN ({ph}) AND device='ALL'""",
        ENABLED_CAMPAIGN_IDS).fetchone()
    max_date = datetime.strptime(max_row[0][:10], "%Y-%m-%d").date()
    mature_end = max_date - timedelta(days=args.lag_days)
    today = datetime.now().date()
    cutoff = (today - timedelta(days=args.window)).isoformat()

    events = conn.execute(
        f"""SELECT e.campaign_id, c.campaign_name, e.change_date,
                   e.old_value, e.new_value
            FROM campaign_change_events e
            JOIN campaigns c ON c.campaign_id = e.campaign_id
            WHERE e.platform='google' AND e.account_id=?
              AND e.change_type='TARGET_ROAS'
              AND e.campaign_id IN ({ph}) AND e.change_date >= ?
            ORDER BY e.change_date""",
        [ACCOUNT_ID, *ENABLED_CAMPAIGN_IDS, cutoff]).fetchall()

    latest = {}
    for e in events:
        latest[str(e["campaign_id"])] = e
    if not latest:
        print(f"No TARGET_ROAS moves in the last {args.window} days — "
              f"nothing to monitor.")
        return

    print(f"Guardrail monitor — data through {max_date} "
          f"(mature through {mature_end}, lag {args.lag_days}d)\n")
    print(f"{'STATUS':7s} {'campaign':28s} {'moved':>10s} {'day':>4s} "
          f"{'5d ROAS':>8s} {'7d ROAS':>8s} {'spend/d':>9s} "
          f"{'exp ROAS':>8s} {'exp sp/d':>9s}  reason")

    for cid, e in latest.items():
        name = e["campaign_name"]
        short = name.replace("HOME INSURANCE", "").replace(
            "_STATE-GROUP_", "").replace("-NB-SEARCH-ALL-USA-STE", ""
            ).replace("_COMBINED_TROAS_SEARCH_ALLDEVICE_USA-STE", ""
            ).strip("-_ ")[:28]
        ev_date = datetime.strptime(str(e["change_date"])[:10],
                                    "%Y-%m-%d").date()
        t_old = normalize_target(e["old_value"])
        t_new = normalize_target(e["new_value"])
        if not (t_old and t_new):
            print(f"{'?':7s} {short:28s}  unparseable event values")
            continue
        days_since = (today - ev_date).days

        base = daily_series(conn, cid, ev_date - timedelta(days=14),
                            ev_date - timedelta(days=1))
        b_cost = sum(x[1] for x in base)
        b_rev = sum(x[3] for x in base)
        b_roas = b_rev / b_cost if b_cost else None
        b_spend_day = b_cost / max(len(base), 1)
        exp_roas = (b_roas / t_old) * t_new if b_roas else None
        exp_spend = b_spend_day * spend_mult(name, t_old, t_new)

        post_all = daily_series(conn, cid, ev_date + timedelta(days=1),
                                max_date)
        post_mat = [x for x in post_all if x[0] <= mature_end]
        spend_day_now = (sum(x[1] for x in post_all[-3:]) /
                         max(len(post_all[-3:]), 1)) if post_all else 0
        roas5, _ = window_roas(post_mat, 5)
        roas7, _ = window_roas(post_mat, 7)
        roas3, _ = window_roas(post_mat, 3)
        gp_neg_run = 0
        for x in reversed(post_mat):
            if x[3] * corr - x[1] < 0:
                gp_neg_run += 1
            else:
                break

        red_line = min(1.05, max(0.93, (exp_roas or 1.05) - 0.15))
        status, reason = "OK", "tracking within expectations"
        catastrophic = (spend_day_now > 2 * b_spend_day and roas3 is not None
                        and roas3 < 0.90)
        if days_since <= args.grace_days and not catastrophic:
            status, reason = "GRACE", (f"day {days_since} of "
                                       f"{args.grace_days} — monitor only")
        else:
            if roas3 is not None and roas3 < 0.90:
                status, reason = "BLACK", (f"mature 3d ROAS {roas3:.2f} < "
                                           f"0.90 true break-even — budget "
                                           f"cap first, then revert")
            elif gp_neg_run >= 5:
                status, reason = "BLACK", (f"true-econ GP/day negative "
                                           f"{gp_neg_run} consecutive "
                                           f"mature days — budget cap "
                                           f"first, then revert")
            elif (exp_spend and spend_day_now > 2 * exp_spend
                  and roas7 is not None and roas7 < 1.0):
                status, reason = "BLACK", (f"spend {spend_day_now:,.0f}/d > "
                                           f"2x expected with 7d ROAS "
                                           f"{roas7:.2f} < 1.0 — budget "
                                           f"cap first, then revert")
            elif (roas7 is not None and roas7 < red_line
                  and len(post_mat) >= args.grace_days + 5):
                status, reason = "RED", (f"mature 7d ROAS {roas7:.2f} < "
                                         f"RED line {red_line:.2f} — "
                                         f"step back ONE rung")
            elif exp_spend and spend_day_now > 1.5 * exp_spend:
                status, reason = "AMBER", (f"spend {spend_day_now:,.0f}/d > "
                                           f"1.5x expected "
                                           f"{exp_spend:,.0f} — verify vs "
                                           f"UI, recheck 48h")
            elif (roas5 is not None and exp_roas
                  and roas5 < exp_roas - 0.10):
                status, reason = "AMBER", (f"mature 5d ROAS {roas5:.2f} > "
                                           f"0.10 below expected "
                                           f"{exp_roas:.2f} — verify vs "
                                           f"UI, recheck 48h")

        f5 = f"{roas5:.2f}" if roas5 is not None else "—"
        f7 = f"{roas7:.2f}" if roas7 is not None else "—"
        print(f"{status:7s} {short:28s} {t_old:.2f}→{t_new:.2f} "
              f"{days_since:>4d} {f5:>8s} {f7:>8s} {spend_day_now:>9,.0f} "
              f"{exp_roas or 0:>8.2f} {exp_spend:>9,.0f}  {reason}")

    conn.close()


if __name__ == "__main__":
    main()
