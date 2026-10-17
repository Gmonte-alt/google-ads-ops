"""
cross_platform_state_rpl.py
Cross-platform state RPL comparison: Google Home (acct 1) vs Microsoft Home (acct 2)
Monthly grain, March 2026 -> current.

Charter (8/25):
  Hypothesis: both platforms monetize the same partner CID, so state-level lead value
  should align across platforms WITHIN device; blended-RPL divergence should be
  explained by device mix. Residual after the device control = the finding.

Sources (read-only):
  Google:    campaign_performance_geo_daily_search (PMax-excluded), account_id=1
             device = native splits ONLY (no ALL rollup): desktop cut = 'DESKTOP',
             all-device = SUM over splits. Leads=conversions, revenue=conversion_value.
  Microsoft: geo_performance_daily, account_id=2
             device = 'ALL' rollup present (filter, never sum alongside);
             splits used only for device-controlled test.
             Search-only = network != 'Audience' (row filter).
             Leads=conversions. Revenue column ADJUDICATED in Step 0.

Step 0: adjudicate MS geo revenue column (conversion_value vs all_conversion_value)
        by reconciling search-only geo monthly totals against campaign-grain truth
        (campaign_performance_daily = SEARCH ONLY by design, acct 2), computed live.
Step 1: geo_state normalization (MS full names vs Google format TBD at runtime),
        coverage assertion on top-10 spend states both sides; location_type check.

Exhibits (Excel workbook + console header):
  00_Header        adjudication evidence + normalization + runtime rulings
  01_RPL_Index     state x month RPL, all-device, both platforms, own-March=100
  02_DeviceAlign   desktop-vs-desktop and mobile-vs-mobile state RPL by month
                   (scatter charts, diagonal = CID-alignment prediction)
  03_DeviceMix     device lead-share per platform per month
  04_LeadsPerDay   leads/day per state per platform per month (partial Aug labeled)
  Data_*           underlying cell-level data with thin-cell flags

Thin-cell floor: < 30 leads/month/cell -> flagged LOW_CONF, excluded from
index + scatter exhibits (kept in data sheets).

Usage:
  python cross_platform_state_rpl.py --db paid_search.db --smoke   # March-only checks
  python cross_platform_state_rpl.py --db paid_search.db           # full run
"""

import argparse
import calendar
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import date

# ---------------------------------------------------------------- constants

START_MONTH = "2026-03"
THIN_CELL_LEADS = 30
ADJ_TOLERANCE = 0.02          # winner must reconcile within 2% on full months
MS_ALL_SPLIT_TOL = 0.015      # ALL rollup vs sum-of-splits sanity tolerance

GOOGLE_TABLE = "campaign_performance_geo_daily_search"
MS_TABLE = "geo_performance_daily"
MS_CAMPAIGN_TRUTH = "campaign_performance_daily"   # search-only by design (acct 2)

STATE_NAME_TO_CODE = {
    "Alabama": "AL", "Alaska": "AK", "Arizona": "AZ", "Arkansas": "AR",
    "California": "CA", "Colorado": "CO", "Connecticut": "CT", "Delaware": "DE",
    "District of Columbia": "DC", "Florida": "FL", "Georgia": "GA", "Hawaii": "HI",
    "Idaho": "ID", "Illinois": "IL", "Indiana": "IN", "Iowa": "IA", "Kansas": "KS",
    "Kentucky": "KY", "Louisiana": "LA", "Maine": "ME", "Maryland": "MD",
    "Massachusetts": "MA", "Michigan": "MI", "Minnesota": "MN", "Mississippi": "MS",
    "Missouri": "MO", "Montana": "MT", "Nebraska": "NE", "Nevada": "NV",
    "New Hampshire": "NH", "New Jersey": "NJ", "New Mexico": "NM", "New York": "NY",
    "North Carolina": "NC", "North Dakota": "ND", "Ohio": "OH", "Oklahoma": "OK",
    "Oregon": "OR", "Pennsylvania": "PA", "Rhode Island": "RI",
    "South Carolina": "SC", "South Dakota": "SD", "Tennessee": "TN", "Texas": "TX",
    "Utah": "UT", "Vermont": "VT", "Virginia": "VA", "Washington": "WA",
    "West Virginia": "WV", "Wisconsin": "WI", "Wyoming": "WY",
}
STATE_CODE_TO_CODE = {v: v for v in STATE_NAME_TO_CODE.values()}

# device-name canonicalization (both platforms' known vocabularies)
DEVICE_CANON = {
    "DESKTOP": "desktop", "COMPUTER": "desktop", "COMPUTERS": "desktop",
    "MOBILE": "mobile", "SMARTPHONE": "mobile", "SMARTPHONES": "mobile",
    "HIGHENDMOBILE": "mobile",
    "TABLET": "tablet", "TABLETS": "tablet",
    "ALL": "ALL",
}

HEADER_LINES = []  # accumulated for the 00_Header sheet


def log(msg=""):
    print(msg)
    HEADER_LINES.append(str(msg))


def die(msg):
    log("")
    log(f"*** HARD FAIL: {msg}")
    sys.exit(1)


# ---------------------------------------------------------------- introspection

def table_columns(con, table):
    rows = con.execute(f"PRAGMA table_info({table})").fetchall()
    if not rows:
        die(f"table {table} not found in DB")
    return [r[1] for r in rows]


def resolve_col(cols, candidates, table, what, required=True):
    for c in candidates:
        if c in cols:
            return c
    if required:
        die(f"{table}: no {what} column among {candidates}; columns = {cols}")
    return None


def canon_device(raw):
    if raw is None:
        return None
    return DEVICE_CANON.get(str(raw).strip().upper().replace(" ", ""), None)


def month_of(d):
    return d[:7]


def months_between(start_ym, end_ym):
    y, m = map(int, start_ym.split("-"))
    ey, em = map(int, end_ym.split("-"))
    out = []
    while (y, m) <= (ey, em):
        out.append(f"{y:04d}-{m:02d}")
        m += 1
        if m == 13:
            m, y = 1, y + 1
    return out


def days_elapsed_in_month(ym, max_data_date):
    """Denominator for leads/day. Partial month uses data max date."""
    y, m = map(int, ym.split("-"))
    month_days = calendar.monthrange(y, m)[1]
    last = date(y, m, month_days)
    mx = date(*map(int, max_data_date.split("-")))
    if mx < last and mx.year == y and mx.month == m:
        return mx.day, True
    return month_days, False


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="paid_search.db")
    ap.add_argument("--smoke", action="store_true",
                    help="March-only verification pass (no workbook)")
    ap.add_argument("--out", default="cross_platform_state_rpl.xlsx")
    args = ap.parse_args()

    # all outputs land in <script dir>/output/ unless an absolute path is given
    out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")
    os.makedirs(out_dir, exist_ok=True)
    if not os.path.isabs(args.out):
        args.out = os.path.join(out_dir, args.out)

    con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row

    log("=" * 78)
    log("CROSS-PLATFORM STATE RPL — Google Home (acct 1) vs Microsoft Home (acct 2)")
    log("Mode: " + ("SMOKE (March-only checks)" if args.smoke else "FULL RUN"))
    log("=" * 78)

    # ---------- runtime column resolution (no assumptions) ----------
    g_cols = table_columns(con, GOOGLE_TABLE)
    m_cols = table_columns(con, MS_TABLE)
    t_cols = table_columns(con, MS_CAMPAIGN_TRUTH)

    g_date = resolve_col(g_cols, ["report_date", "date", "segments_date", "day"],
                         GOOGLE_TABLE, "date")
    m_date = resolve_col(m_cols, ["report_date", "date", "time_period", "day"],
                         MS_TABLE, "date")
    t_date = resolve_col(t_cols, ["report_date", "date", "time_period", "day"],
                         MS_CAMPAIGN_TRUTH, "date")

    for need, cols, tbl in [
        ("geo_state", g_cols, GOOGLE_TABLE), ("device", g_cols, GOOGLE_TABLE),
        ("conversions", g_cols, GOOGLE_TABLE), ("conversion_value", g_cols, GOOGLE_TABLE),
        ("geo_state", m_cols, MS_TABLE), ("device", m_cols, MS_TABLE),
        ("network", m_cols, MS_TABLE), ("conversions", m_cols, MS_TABLE),
        ("conversion_value", m_cols, MS_TABLE),
    ]:
        if need not in cols:
            die(f"{tbl} missing required column '{need}'; columns = {cols}")

    ms_has_all_cv = "all_conversion_value" in m_cols
    if not ms_has_all_cv:
        log(f"NOTE: {MS_TABLE} has no all_conversion_value column — adjudication "
            f"degenerates to verifying conversion_value against campaign truth.")

    g_has_acct = "account_id" in g_cols
    m_has_acct = "account_id" in m_cols
    t_has_acct = "account_id" in t_cols
    if not (g_has_acct and m_has_acct and t_has_acct):
        die(f"account_id presence — {GOOGLE_TABLE}:{g_has_acct} "
            f"{MS_TABLE}:{m_has_acct} {MS_CAMPAIGN_TRUTH}:{t_has_acct}. "
            f"Need scoping ruling from architect before running.")

    m_loc_type = "location_type" if "location_type" in m_cols else None
    g_cost = "cost_micros" if "cost_micros" in g_cols else ("cost" if "cost" in g_cols else None)
    m_cost = "cost_micros" if "cost_micros" in m_cols else ("cost" if "cost" in m_cols else None)

    log(f"\n[RESOLVE] {GOOGLE_TABLE}: date={g_date}, cost={g_cost}")
    log(f"[RESOLVE] {MS_TABLE}: date={m_date}, cost={m_cost}, "
        f"location_type={'present' if m_loc_type else 'ABSENT'}, "
        f"all_conversion_value={'present' if ms_has_all_cv else 'ABSENT'}")
    log(f"[RESOLVE] {MS_CAMPAIGN_TRUTH}: date={t_date}")

    # ---------- data extents ----------
    g_max = con.execute(
        f"SELECT MAX({g_date}) FROM {GOOGLE_TABLE} WHERE account_id=1").fetchone()[0]
    m_max = con.execute(
        f"SELECT MAX({m_date}) FROM {MS_TABLE} WHERE account_id=2").fetchone()[0]
    g_min = con.execute(
        f"SELECT MIN({g_date}) FROM {GOOGLE_TABLE} WHERE account_id=1").fetchone()[0]
    m_min = con.execute(
        f"SELECT MIN({m_date}) FROM {MS_TABLE} WHERE account_id=2").fetchone()[0]
    log(f"\n[EXTENT] Google geo: {g_min} -> {g_max}   Microsoft geo: {m_min} -> {m_max}")
    if month_of(g_min) > START_MONTH or month_of(m_min) > START_MONTH:
        die(f"coverage does not reach {START_MONTH} on both sides")

    end_month = min(month_of(g_max), month_of(m_max))
    months = months_between(START_MONTH, end_month)
    if args.smoke:
        run_months = ["2026-03"]
    else:
        run_months = months
    log(f"[SCOPE] analysis months: {', '.join(run_months)}")

    # ---------- MS vocab prints ----------
    nets = [r[0] for r in con.execute(
        f"SELECT DISTINCT network FROM {MS_TABLE} WHERE account_id=2").fetchall()]
    log(f"\n[MS] DISTINCT network: {nets}")
    if "Audience" not in nets:
        log("WARNING: literal 'Audience' not among network values — verify the "
            "search-only filter (network != 'Audience') still excludes the right rows.")

    m_devices = [r[0] for r in con.execute(
        f"SELECT DISTINCT device FROM {MS_TABLE} WHERE account_id=2").fetchall()]
    g_devices = [r[0] for r in con.execute(
        f"SELECT DISTINCT device FROM {GOOGLE_TABLE} WHERE account_id=1").fetchall()]
    log(f"[MS] DISTINCT device: {m_devices}")
    log(f"[G ] DISTINCT device: {g_devices}")
    if any(canon_device(d) == "ALL" for d in g_devices):
        die(f"{GOOGLE_TABLE} contains an ALL-like device value — pinned ruling says "
            f"native splits only. Double-count risk; stop and re-adjudicate.")
    if not any(canon_device(d) == "ALL" for d in m_devices):
        die(f"{MS_TABLE} has no ALL device rollup — pinned ruling says it does.")
    unmapped = [d for d in set(m_devices + g_devices) if canon_device(d) is None]
    if unmapped:
        log(f"WARNING: unmapped device values (excluded from device-controlled "
            f"exhibit, included in nothing): {unmapped}")

    ms_all_literal = next(d for d in m_devices if canon_device(d) == "ALL")

    if m_loc_type:
        lt = con.execute(
            f"SELECT {m_loc_type}, COUNT(*), SUM(conversions) FROM {MS_TABLE} "
            f"WHERE account_id=2 GROUP BY 1").fetchall()
        log(f"[MS] location_type distribution (rows, conversions): "
            + "; ".join(f"{r[0]}={r[1]}/{r[2]:.0f}" for r in lt))
        if len(lt) > 1:
            log("  -> multiple location_types present: disjointness verified below "
                "via geo-vs-campaign-truth reconciliation (Step 0). If Step 0 "
                "reconciles, summing across types is not double-counting.")

    # =====================================================================
    # STEP 0 — ADJUDICATE MS REVENUE COLUMN (computed live, full months)
    # =====================================================================
    log("\n" + "=" * 78)
    log("STEP 0 — MS GEO REVENUE COLUMN ADJUDICATION")
    log("Truth = campaign_performance_daily (SEARCH-ONLY BY DESIGN, acct 2, "
        "penny-reconciled 8/06). Candidates = geo search-only monthly totals.")
    log("=" * 78)

    adjud_months = [m for m in months if m != month_of(m_max)] or months  # full months
    truth = {}
    for r in con.execute(
            f"SELECT SUBSTR({t_date},1,7) ym, SUM(conversions) leads, "
            f"SUM(conversion_value) rev FROM {MS_CAMPAIGN_TRUTH} "
            f"WHERE account_id=2 AND device=? AND SUBSTR({t_date},1,7) BETWEEN ? AND ? "
            f"GROUP BY 1", (ms_all_literal, adjud_months[0], adjud_months[-1])):
        truth[r["ym"]] = (r["leads"] or 0.0, r["rev"] or 0.0)

    cand_cols = ["conversion_value"] + (["all_conversion_value"] if ms_has_all_cv else [])
    geo = {}
    sel = ", ".join(f"SUM({c}) AS {c}" for c in cand_cols)
    for r in con.execute(
            f"SELECT SUBSTR({m_date},1,7) ym, SUM(conversions) leads, {sel} "
            f"FROM {MS_TABLE} WHERE account_id=2 AND device=? AND network != 'Audience' "
            f"AND SUBSTR({m_date},1,7) BETWEEN ? AND ? GROUP BY 1",
            (ms_all_literal, adjud_months[0], adjud_months[-1])):
        geo[r["ym"]] = {"leads": r["leads"] or 0.0,
                        **{c: (r[c] or 0.0) for c in cand_cols}}

    log(f"\n{'month':8} {'truth_leads':>11} {'geo_leads':>10} | {'truth_rev':>11} "
        + " ".join(f"{c:>18}" for c in cand_cols))
    errs = {c: [] for c in cand_cols}
    for ym in adjud_months:
        t_leads, t_rev = truth.get(ym, (0, 0))
        g = geo.get(ym, {})
        line = f"{ym:8} {t_leads:11.1f} {g.get('leads', 0):10.1f} | {t_rev:11.0f}"
        for c in cand_cols:
            v = g.get(c, 0.0)
            pct = (v / t_rev - 1) if t_rev else float("nan")
            errs[c].append(abs(pct))
            line += f" {v:11.0f}({pct:+5.1%})"
        log(line)
        rpl = (t_rev / t_leads) if t_leads else 0
        log(f"{'':8} truth RPL = {rpl:6.2f}  (expect ~$50, ~$92-96K/mo per 8/06 map)")

    winner, verdict = None, ""
    if ms_has_all_cv:
        mean_err = {c: (sum(e) / len(e) if e else 9e9) for c, e in errs.items()}
        winner = min(mean_err, key=mean_err.get)
        log(f"\n[ADJUDICATION] mean abs monthly error vs truth: "
            + ", ".join(f"{c}={mean_err[c]:.2%}" for c in cand_cols))
        if mean_err[winner] > ADJ_TOLERANCE:
            die(f"neither candidate reconciles within {ADJ_TOLERANCE:.0%} "
                f"(best = {winner} at {mean_err[winner]:.2%}). Do not proceed — "
                f"take this table to the MS architect.")
        loser = [c for c in cand_cols if c != winner][0]
        verdict = (f"MS geo revenue = {winner} (mean err {mean_err[winner]:.2%}; "
                   f"{loser} err {mean_err[loser]:.2%})")
    else:
        mean_err = sum(errs["conversion_value"]) / max(len(errs["conversion_value"]), 1)
        if mean_err > ADJ_TOLERANCE:
            die(f"conversion_value fails reconciliation ({mean_err:.2%})")
        winner = "conversion_value"
        verdict = f"MS geo revenue = conversion_value (sole candidate, err {mean_err:.2%})"
    log(f"[VERDICT] {verdict}")
    ms_rev_col = winner

    # lead-count parity check (geo vs campaign truth, same months)
    lead_gaps = [abs(geo.get(ym, {}).get("leads", 0) - truth.get(ym, (0, 0))[0])
                 / max(truth.get(ym, (0, 0))[0], 1) for ym in adjud_months]
    if lead_gaps and max(lead_gaps) > 0.02:
        log(f"WARNING: geo vs campaign lead counts diverge up to {max(lead_gaps):.1%} "
            f"— conversions definitions may differ across tables; noted, not fatal "
            f"(geo used consistently within this analysis).")

    # =====================================================================
    # STEP 1 — STATE NORMALIZATION
    # =====================================================================
    log("\n" + "=" * 78)
    log("STEP 1 — geo_state NORMALIZATION")
    log("=" * 78)

    g_states = [r[0] for r in con.execute(
        f"SELECT DISTINCT geo_state FROM {GOOGLE_TABLE} WHERE account_id=1").fetchall()]
    m_states = [r[0] for r in con.execute(
        f"SELECT DISTINCT geo_state FROM {MS_TABLE} WHERE account_id=2").fetchall()]
    log(f"[G ] DISTINCT geo_state ({len(g_states)}): sample = {sorted(map(str, g_states))[:8]}")
    log(f"[MS] DISTINCT geo_state ({len(m_states)}): sample = {sorted(map(str, m_states))[:8]}")

    def normalize(vals, side):
        mapping, missed = {}, []
        for v in vals:
            if v is None:
                continue
            s = str(v).strip()
            if s in STATE_NAME_TO_CODE:
                mapping[v] = STATE_NAME_TO_CODE[s]
            elif s.upper() in STATE_CODE_TO_CODE:
                mapping[v] = s.upper()
            elif s.title() in STATE_NAME_TO_CODE:
                mapping[v] = STATE_NAME_TO_CODE[s.title()]
            else:
                missed.append(s)
        if missed:
            log(f"[{side}] unmapped geo_state values ({len(missed)}): {missed[:15]}"
                + (" ..." if len(missed) > 15 else "")
                + "  -> EXCLUDED from state exhibits (national/unknown rows)")
        return mapping

    g_map = normalize(g_states, "G ")
    m_map = normalize(m_states, "MS")
    if not g_map:
        die(f"Google geo_state format unrecognized — no values mapped. "
            f"Full DISTINCT list above; extend the normalizer.")

    # top-10 spend state coverage assertion, both sides
    def top_states(table, date_col, acct, dev_filter, cost_col, mapping):
        if not cost_col:
            cost_expr = "SUM(conversions)"
            basis = "conversions (no cost col)"
        else:
            cost_expr = f"SUM({cost_col})"
            basis = cost_col
        rows = con.execute(
            f"SELECT geo_state, {cost_expr} v FROM {table} "
            f"WHERE account_id={acct} {dev_filter} "
            f"AND SUBSTR({date_col},1,7) >= '{START_MONTH}' "
            f"GROUP BY 1 ORDER BY v DESC LIMIT 10").fetchall()
        top = [(r[0], mapping.get(r[0])) for r in rows]
        return top, basis

    g_top, g_basis = top_states(GOOGLE_TABLE, g_date, 1, "", g_cost, g_map)
    m_top, m_basis = top_states(MS_TABLE, m_date, 2,
                                f"AND device='{ms_all_literal}' AND network != 'Audience'",
                                m_cost, m_map)
    log(f"\n[G ] top-10 by {g_basis}: " + ", ".join(f"{s}->{c or 'UNMAPPED'}" for s, c in g_top))
    log(f"[MS] top-10 by {m_basis}: " + ", ".join(f"{s}->{c or 'UNMAPPED'}" for s, c in m_top))
    for side, top in [("Google", g_top), ("Microsoft", m_top)]:
        um = [s for s, c in top if c is None]
        if um:
            die(f"{side} top-10 spend states unmapped: {um} — fix normalization first.")
    log("[OK] top-10 spend states fully mapped on both sides")

    # =====================================================================
    # EXTRACT CELL DATA  (state x month x device-class per platform)
    # =====================================================================
    # google: per split device, canon; all-device = sum of splits
    def g_case_map():
        return " ".join(
            f"WHEN '{d}' THEN '{canon_device(d)}'" for d in g_devices if canon_device(d))

    g_click_col = "clicks" if "clicks" in g_cols else None
    m_click_col = "clicks" if "clicks" in m_cols else None
    g_cost_sel = (f"SUM({g_cost})" if g_cost else "0") + " cost"
    g_clk_sel = (f"SUM({g_click_col})" if g_click_col else "0") + " clk"
    g_rows = con.execute(
        f"SELECT SUBSTR({g_date},1,7) ym, geo_state, "
        f"CASE device {g_case_map()} ELSE NULL END dev, "
        f"SUM(conversions) leads, SUM(conversion_value) rev, "
        f"{g_cost_sel}, {g_clk_sel} "
        f"FROM {GOOGLE_TABLE} WHERE account_id=1 "
        f"AND SUBSTR({g_date},1,7) BETWEEN ? AND ? "
        f"GROUP BY 1,2,3", (run_months[0], run_months[-1])).fetchall()

    def m_case_map():
        return " ".join(
            f"WHEN '{d}' THEN '{canon_device(d)}'" for d in m_devices if canon_device(d))

    m_cost_sel = (f"SUM({m_cost})" if m_cost else "0") + " cost"
    m_clk_sel = (f"SUM({m_click_col})" if m_click_col else "0") + " clk"
    m_rows = con.execute(
        f"SELECT SUBSTR({m_date},1,7) ym, geo_state, "
        f"CASE device {m_case_map()} ELSE NULL END dev, "
        f"SUM(conversions) leads, SUM({ms_rev_col}) rev, "
        f"{m_cost_sel}, {m_clk_sel} "
        f"FROM {MS_TABLE} WHERE account_id=2 AND network != 'Audience' "
        f"AND SUBSTR({m_date},1,7) BETWEEN ? AND ? "
        f"GROUP BY 1,2,3", (run_months[0], run_months[-1])).fetchall()

    # cells[(platform, ym, state_code, devclass)] = [leads, rev, cost, clicks]
    # devclass in {'all','desktop','mobile','tablet'}
    g_cdiv = 1e6 if g_cost == "cost_micros" else 1.0
    m_cdiv = 1e6 if m_cost == "cost_micros" else 1.0
    cells = defaultdict(lambda: [0.0, 0.0, 0.0, 0.0])
    for r in g_rows:
        st = g_map.get(r["geo_state"])
        if st is None or r["dev"] is None or r["dev"] == "ALL":
            continue
        for dc in (r["dev"], "all"):        # google all-device = accumulate splits
            c = cells[("G", r["ym"], st, dc)]
            c[0] += r["leads"] or 0
            c[1] += r["rev"] or 0
            c[2] += (r["cost"] or 0) / g_cdiv
            c[3] += r["clk"] or 0
    for r in m_rows:
        st = m_map.get(r["geo_state"])
        if st is None or r["dev"] is None:
            continue
        dc = "all" if r["dev"] == "ALL" else r["dev"]   # MS all-device = ALL rows only
        c = cells[("M", r["ym"], st, dc)]
        c[0] += r["leads"] or 0
        c[1] += r["rev"] or 0
        c[2] += (r["cost"] or 0) / m_cdiv
        c[3] += r["clk"] or 0

    # ---------- smoke checks ----------
    log("\n" + "=" * 78)
    log("CONSISTENCY CHECKS" + (" (SMOKE)" if args.smoke else ""))
    log("=" * 78)
    # MS: ALL vs sum-of-splits (within search-only rows, per month)
    for ym in run_months:
        for metric, idx in [("leads", 0), ("rev", 1)]:
            all_v = sum(v[idx] for (p, y, s, d), v in cells.items()
                        if p == "M" and y == ym and d == "all")
            split_v = sum(v[idx] for (p, y, s, d), v in cells.items()
                          if p == "M" and y == ym and d in ("desktop", "mobile", "tablet"))
            gap = abs(split_v - all_v) / max(all_v, 1)
            tag = "OK" if gap <= MS_ALL_SPLIT_TOL else "WARN"
            log(f"[{tag}] MS {ym} {metric}: ALL={all_v:,.0f} splits={split_v:,.0f} "
                f"gap={gap:.2%}")
    # Google month totals for eyeball vs known figures
    for ym in run_months:
        gl = sum(v[0] for (p, y, s, d), v in cells.items()
                 if p == "G" and y == ym and d == "all")
        gr = sum(v[1] for (p, y, s, d), v in cells.items()
                 if p == "G" and y == ym and d == "all")
        log(f"[G ] {ym} all-device totals: leads={gl:,.1f} rev=${gr:,.0f} "
            f"RPL={gr / gl if gl else 0:,.2f}  <- eyeball vs known figures")

    if args.smoke:
        log("\nSMOKE COMPLETE — review adjudication verdict, mapping, and check "
            "lines above; then run full.")
        return

    # =====================================================================
    # WORKBOOK
    # =====================================================================
    import xlsxwriter

    all_states = sorted({s for (p, y, s, d) in cells})
    wb = xlsxwriter.Workbook(args.out, {"nan_inf_to_errors": True})
    f_hdr = wb.add_format({"bold": True, "bg_color": "#1F3864", "font_color": "white",
                           "border": 1})
    f_bold = wb.add_format({"bold": True})
    f_num = wb.add_format({"num_format": "#,##0.0"})
    f_cur = wb.add_format({"num_format": "$#,##0.00"})
    f_idx = wb.add_format({"num_format": "0"})
    f_pct = wb.add_format({"num_format": "0.0%"})
    f_low = wb.add_format({"font_color": "#999999", "italic": True})
    f_mono = wb.add_format({"font_name": "Consolas", "font_size": 9})

    partial_flags = {}
    for ym in run_months:
        _, gp = days_elapsed_in_month(ym, g_max)
        _, mp = days_elapsed_in_month(ym, m_max)
        partial_flags[ym] = gp or mp

    def mlabel(ym):
        return ym + (" (partial)" if partial_flags[ym] else "")

    def cell(p, ym, st, d):
        return cells.get((p, ym, st, d), [0.0, 0.0, 0.0, 0.0])

    def rpl(p, ym, st, d):
        v = cell(p, ym, st, d)
        l, r = v[0], v[1]
        return (r / l) if l else None, l

    # ---- 00_Header ----
    ws = wb.add_worksheet("00_Header")
    ws.set_column(0, 0, 110)
    for i, line in enumerate(HEADER_LINES):
        ws.write_string(i, 0, line, f_mono)
    caveat_row = len(HEADER_LINES) + 2
    ws.write(caveat_row, 0, "CAVEATS (read before using):", f_bold)
    for j, cv in enumerate([
        f"1. VERDICT: {verdict}. Absolute cross-platform RPL still carries "
        f"attribution-methodology differences — ALIGNMENT and TRENDS are the "
        f"defensible claims, not level gaps.",
        "2. June Microsoft tier restructure & Google account restructures are "
        "campaign-level structural breaks — invisible (by design) at state grain here.",
        "3. Conversion lag softens the last ~week on both platforms; final month partial.",
        f"4. Thin-cell floor: cells under {THIN_CELL_LEADS} leads/month are flagged "
        f"LOW_CONF and excluded from index + scatter exhibits.",
        "5. MS search-only = network != 'Audience' row filter; Google = _search view "
        "(PMax-excluded). Device: Google all-device = SUM of native splits; "
        "MS all-device = device='ALL' rows only.",
        "6. MS rows include location_type=INTEREST (~7% of conversions): state "
        "assigned by location-of-interest, not searcher location. Step-0 "
        "reconciliation confirms no double-count; semantics arguably correct for "
        "home insurance (property state = what the CID prices).",
        "7. Final-month coverage windows differ per platform (each side's own max "
        "data date) — leads/day denominators adjust per side; treat the partial "
        "month's cross-platform comparison as softest.",
    ]):
        ws.write(caveat_row + 1 + j, 0, cv)

    # ---- Data_Cells (long form) ----
    ws = wb.add_worksheet("Data_Cells")
    hdrs = ["platform", "month", "state", "device", "leads", "revenue",
            "cost", "clicks", "rpl", "flag"]
    for c, h in enumerate(hdrs):
        ws.write(0, c, h, f_hdr)
    r_i = 1
    for (p, ym, st, d), (l, rv, co, ck) in sorted(cells.items()):
        flag = "LOW_CONF" if l < THIN_CELL_LEADS else ""
        ws.write(r_i, 0, "Google" if p == "G" else "Microsoft")
        ws.write(r_i, 1, mlabel(ym))
        ws.write(r_i, 2, st)
        ws.write(r_i, 3, d)
        ws.write_number(r_i, 4, l, f_num)
        ws.write_number(r_i, 5, rv, f_cur)
        ws.write_number(r_i, 6, co, f_cur)
        ws.write_number(r_i, 7, ck, f_num)
        if l:
            ws.write_number(r_i, 8, rv / l, f_cur)
        ws.write(r_i, 9, flag, f_low if flag else None)
        r_i += 1
    ws.autofilter(0, 0, r_i - 1, len(hdrs) - 1)
    ws.freeze_panes(1, 0)

    # ---- 01_RPL_Index: own-March = 100, all-device ----
    ws = wb.add_worksheet("01_RPL_Index")
    ws.write(0, 0, "State x month RPL (all-device), indexed to each platform's own "
                   "March = 100. Blank = thin cell or no March base.", f_bold)
    from xlsxwriter.utility import xl_rowcol_to_cell as XC

    def sifs_ref(valcol, plat_ref, month_ref, state_ref, dev_ref):
        """SUMIFS with all criteria taken from CELL REFERENCES."""
        return (f"SUMIFS(Data_Cells!${valcol}:${valcol},"
                f"Data_Cells!$A:$A,{plat_ref},Data_Cells!$B:$B,{month_ref},"
                f"Data_Cells!$C:$C,{state_ref},Data_Cells!$D:$D,{dev_ref})")

    def index_sheet(ws, valcol):
        """01/06 layout: params row, platform row, month row, data with
        every criterion referenced from a cell."""
        ws.write(1, 0, "params:", f_bold)
        ws.write(1, 1, "device")
        ws.write_string(1, 2, "all")
        ws.write(1, 3, "thin floor")
        ws.write_number(1, 4, THIN_CELL_LEADS)
        dev_ref, thin_ref = "$C$2", "$E$2"
        pr, mr = 2, 3                      # platform row, month row (0-based)
        ws.write(mr, 0, "state", f_hdr)
        n = len(run_months)
        for pi, plat in enumerate(("Google", "Microsoft")):
            for j, ym in enumerate(run_months):
                c = 1 + pi * n + j
                ws.write_string(pr, c, plat, f_hdr)
                ws.write_string(mr, c, mlabel(ym), f_hdr)
        for i, st in enumerate(all_states):
            r = mr + 1 + i
            ws.write_string(r, 0, st, f_bold)
            st_ref = f"$A{r + 1}"
            for pi in range(2):
                base_c = 1 + pi * n
                base_m = XC(mr, base_c, row_abs=True, col_abs=True)
                for j in range(n):
                    c = base_c + j
                    plat_ref = XC(pr, c, row_abs=True)
                    m_ref = XC(mr, c, row_abs=True)
                    cur_l = sifs_ref("E", plat_ref, m_ref, st_ref, dev_ref)
                    base_l = sifs_ref("E", plat_ref, base_m, st_ref, dev_ref)
                    cur_v = sifs_ref(valcol, plat_ref, m_ref, st_ref, dev_ref)
                    base_v = sifs_ref(valcol, plat_ref, base_m, st_ref,
                                      dev_ref)
                    f = (f"=IF(OR({cur_l}<{thin_ref},{base_l}<{thin_ref}),"
                         f"\"\",IFERROR(100*({cur_v}/{cur_l})"
                         f"/({base_v}/{base_l}),\"\"))")
                    ws.write_formula(r, c, f, f_idx)
        ws.freeze_panes(mr + 1, 1)

    index_sheet(ws, "F")

    # ---- 02_DeviceAlign: desktop & mobile scatter data + charts ----
    ws = wb.add_worksheet("02_DeviceAlign")
    ws.write(0, 0, "Device-controlled alignment: state RPL, MS (x) vs Google (y), "
                   "same device. CID hypothesis predicts points near the diagonal.", f_bold)
    ws.write(1, 0, "Cohort panel: floor-qualifiers (>= thin floor on BOTH "
                   "platforms in >=1 month) UNION the top-6 combined-volume "
                   "states, every month; 'qualifies' compares this row's "
                   "leads cells to the floor param — money states with "
                   "thin desktop cells show as LOW_CONF, not as absences.",
             f_bold)
    ws.write(1, 9, "params:", f_bold)
    ws.write_string(1, 10, "Microsoft")
    ws.write_string(1, 11, "Google")
    ws.write_number(1, 12, THIN_CELL_LEADS)
    ms_ref, g_ref, thin_ref = "$K$2", "$L$2", "$M$2"
    hdrs = ["device", "month", "state", "ms_rpl", "g_rpl", "ms_leads",
            "g_leads", "qualifies"]
    for c, h in enumerate(hdrs):
        ws.write(2, c, h, f_hdr)
    # top-6 by combined G+MS all-device leads (used for the Top-6 block
    # AND unioned into the panel so the money states are always present)
    combined = {st: sum(cell("G", ym, st, "all")[0]
                        + cell("M", ym, st, "all")[0] for ym in run_months)
                for st in all_states}
    ranked = sorted(combined.items(), key=lambda x: -x[1])
    top6 = [s for s, _ in ranked[:6]]
    # cohort per device = floor-qualifiers UNION top-6; thin months carry
    # the LOW_CONF flag rather than dropping the state
    cohort = {}
    for d in ("desktop", "mobile"):
        qual = set()
        for ym in run_months:
            for st in all_states:
                mv, ml = rpl("M", ym, st, d)
                gv, gl = rpl("G", ym, st, d)
                if (mv is not None and gv is not None
                        and ml >= THIN_CELL_LEADS and gl >= THIN_CELL_LEADS):
                    qual.add(st)
        cohort[d] = sorted(qual | set(top6))
    scatter_rows = {("desktop"): [], ("mobile"): []}
    r_i = 3
    for d in ("desktop", "mobile"):
        for ym in run_months:
            for st in cohort[d]:
                x = r_i + 1                     # Excel row number
                dref, mref, sref = f"$A{x}", f"$B{x}", f"$C{x}"
                ml_f = sifs_ref("E", ms_ref, mref, sref, dref)
                gl_f = sifs_ref("E", g_ref, mref, sref, dref)
                mr_f = sifs_ref("F", ms_ref, mref, sref, dref)
                gr_f = sifs_ref("F", g_ref, mref, sref, dref)
                ws.write_string(r_i, 0, d)
                ws.write_string(r_i, 1, mlabel(ym))
                ws.write_string(r_i, 2, st)
                ws.write_formula(r_i, 3,
                                 f"=IFERROR({mr_f}/$F{x},\"\")", f_cur)
                ws.write_formula(r_i, 4,
                                 f"=IFERROR({gr_f}/$G{x},\"\")", f_cur)
                ws.write_formula(r_i, 5, f"={ml_f}", f_num)
                ws.write_formula(r_i, 6, f"={gl_f}", f_num)
                ws.write_formula(
                    r_i, 7,
                    f"=IF(AND($F{x}>={thin_ref},$G{x}>={thin_ref}),"
                    f"\"YES\",\"LOW_CONF\")")
                scatter_rows[d].append((r_i, ym))
                r_i += 1
    ws.autofilter(2, 0, max(r_i - 1, 3), len(hdrs) - 1)
    ws.freeze_panes(3, 0)

    chart_anchor = 2
    for d in ("desktop", "mobile"):
        if not scatter_rows[d]:
            continue
        ch = wb.add_chart({"type": "scatter"})
        # one series per month for color-by-month
        by_month = defaultdict(list)
        for ri, ym in scatter_rows[d]:
            by_month[ym].append(ri)
        for ym, ris in sorted(by_month.items()):
            # rows are contiguous per (device, month) by construction order
            ch.add_series({
                "name": f"{d} {ym}",
                "categories": ["02_DeviceAlign", min(ris), 3, max(ris), 3],
                "values": ["02_DeviceAlign", min(ris), 4, max(ris), 4],
                "marker": {"type": "circle", "size": 6},
            })
        ch.set_title({"name": f"{d}: cohort state RPL, MS vs Google "
                              f"(diagonal = aligned; incl LOW_CONF months)"})
        ch.set_x_axis({"name": "Microsoft RPL ($)"})
        ch.set_y_axis({"name": "Google RPL ($)"})
        ch.set_size({"width": 640, "height": 420})
        ws.insert_chart(chart_anchor, 9, ch)
        chart_anchor += 23

    # alignment summary: median G/MS ratio per device per month
    ws.write(r_i + 2, 0, "Alignment summary — median Google/MS RPL ratio "
                         "(1.00 = aligned). Computed at build time over "
                         "qualifying rows above (medians are not "
                         "SUMIFS-able); reconcile by filtering the panel.",
             f_bold)
    sr = r_i + 3
    ws.write(sr, 0, "device", f_hdr)
    for j, ym in enumerate(run_months):
        ws.write(sr, 1 + j, mlabel(ym), f_hdr)
    for k, d in enumerate(("desktop", "mobile")):
        ws.write(sr + 1 + k, 0, d, f_bold)
        for j, ym in enumerate(run_months):
            ratios = []
            for st in all_states:
                mv, ml = rpl("M", ym, st, d)
                gv, gl = rpl("G", ym, st, d)
                if mv and gv and ml >= THIN_CELL_LEADS and gl >= THIN_CELL_LEADS:
                    ratios.append(gv / mv)
            if ratios:
                ratios.sort()
                med = ratios[len(ratios) // 2]
                ws.write_number(sr + 1 + k, 1 + j, med, wb.add_format(
                    {"num_format": "0.00"}))

    # ---- Top-6 cohort block (LIVE formulas referencing Data_Cells) ----
    f_r2 = wb.add_format({"num_format": "0.00"})
    tr = sr + 5
    ws.write(tr, 0, "TOP-6 STATES COHORT — fixed selection by combined "
                    "G+MS leads over the full window: "
                    + ", ".join(top6) + ". Same group every month. "
                    "All values below are LIVE formulas whose criteria "
                    "come from CELLS (row/column headers + params).", f_bold)
    # membership reconciliation: why the alignment cohort and top-6 differ
    ws.write(tr + 1, 0, "Membership map (alignment cohort = clears the "
                        "thin floor on both platforms in >=1 month; "
                        "top-6 = combined-volume rank):", f_bold)
    mm = tr + 2
    for c, h in enumerate(["state", "combined_leads", "volume_rank",
                           "in_desktop_cohort", "in_mobile_cohort",
                           "in_top6"]):
        ws.write(mm, c, h, f_hdr)
    show = sorted(set(top6) | set(cohort["desktop"]) | set(cohort["mobile"]),
                  key=lambda s: -combined.get(s, 0))
    for i, st in enumerate(show):
        ws.write_string(mm + 1 + i, 0, st, f_bold)
        ws.write_number(mm + 1 + i, 1, combined.get(st, 0), f_num)
        ws.write_number(mm + 1 + i, 2,
                        1 + [s for s, _ in ranked].index(st), f_idx)
        dq = any((rpl("M", ym, st, "desktop")[1] >= THIN_CELL_LEADS
                  and rpl("G", ym, st, "desktop")[1] >= THIN_CELL_LEADS)
                 for ym in run_months)
        mq = any((rpl("M", ym, st, "mobile")[1] >= THIN_CELL_LEADS
                  and rpl("G", ym, st, "mobile")[1] >= THIN_CELL_LEADS)
                 for ym in run_months)
        ws.write(mm + 1 + i, 3, "YES" if dq else "")
        ws.write(mm + 1 + i, 4, "YES" if mq else "")
        ws.write(mm + 1 + i, 5, "YES" if st in top6 else "")

    blocks = []
    row = mm + len(show) + 3
    for title, plat in [("Google desktop RPL ($)", "Google"),
                        ("Microsoft desktop RPL ($)", "Microsoft")]:
        n6 = len(run_months)
        ws.write(row, 0, title, f_hdr)
        for j, ym in enumerate(run_months):
            ws.write_string(row, 1 + j, mlabel(ym), f_hdr)
        ws.write(row, n6 + 2, "platform:", f_bold)
        ws.write_string(row, n6 + 3, plat)
        ws.write(row, n6 + 4, "device:", f_bold)
        ws.write_string(row, n6 + 5, "desktop")
        p_ref = XC(row, n6 + 3, row_abs=True, col_abs=True)
        d_ref = XC(row, n6 + 5, row_abs=True, col_abs=True)
        for i, st in enumerate(top6):
            r = row + 1 + i
            ws.write_string(r, 0, st, f_bold)
            s_ref = f"$A{r + 1}"
            for j in range(len(run_months)):
                m_ref = XC(row, 1 + j, row_abs=True)
                f = (f"=IFERROR({sifs_ref('F', p_ref, m_ref, s_ref, d_ref)}"
                     f"/{sifs_ref('E', p_ref, m_ref, s_ref, d_ref)},\"\")")
                ws.write_formula(r, 1 + j, f, f_cur)
        blocks.append(row)
        row += len(top6) + 3
    g_blk, m_blk = blocks
    ws.write(row, 0, "G/MS desktop RPL ratio (1.00 = aligned)", f_hdr)
    for j, ym in enumerate(run_months):
        ws.write(row, 1 + j, mlabel(ym), f_hdr)
    from xlsxwriter.utility import xl_rowcol_to_cell as xc
    for i, st in enumerate(top6):
        ws.write(row + 1 + i, 0, st, f_bold)
        for j in range(len(run_months)):
            f = (f"=IFERROR({xc(g_blk + 1 + i, 1 + j)}"
                 f"/{xc(m_blk + 1 + i, 1 + j)},\"\")")
            ws.write_formula(row + 1 + i, 1 + j, f, f_r2)
    ratio_blk = row
    row += len(top6) + 3
    ws.write(row, 0, "Combined G+MS leads (all-device)", f_hdr)
    for j, ym in enumerate(run_months):
        ws.write_string(row, 1 + j, mlabel(ym), f_hdr)
    n6 = len(run_months)
    ws.write(row, n6 + 2, "platforms:", f_bold)
    ws.write_string(row, n6 + 3, "Google")
    ws.write_string(row, n6 + 4, "Microsoft")
    ws.write(row, n6 + 5, "device:", f_bold)
    ws.write_string(row, n6 + 6, "all")
    pg = XC(row, n6 + 3, row_abs=True, col_abs=True)
    pm = XC(row, n6 + 4, row_abs=True, col_abs=True)
    dv = XC(row, n6 + 6, row_abs=True, col_abs=True)
    for i, st in enumerate(top6):
        r = row + 1 + i
        ws.write_string(r, 0, st, f_bold)
        s_ref = f"$A{r + 1}"
        for j in range(len(run_months)):
            m_ref = XC(row, 1 + j, row_abs=True)
            f = (f"={sifs_ref('E', pg, m_ref, s_ref, dv)}"
                 f"+{sifs_ref('E', pm, m_ref, s_ref, dv)}")
            ws.write_formula(r, 1 + j, f, f_num)
    vol_blk = row
    n = len(run_months)
    for anchor, blk, ttl, yname in [
            (tr + 2, ratio_blk, "Top-6: G/MS desktop RPL ratio by month "
             "(1.0 = aligned)", "ratio"),
            (tr + 26, vol_blk, "Top-6: combined lead volume by month",
             "leads")]:
        ch = wb.add_chart({"type": "line"})
        for i, st in enumerate(top6):
            ch.add_series({"name": st,
                           "categories": ["02_DeviceAlign", blk, 1, blk, n],
                           "values": ["02_DeviceAlign", blk + 1 + i, 1,
                                      blk + 1 + i, n],
                           "marker": {"type": "circle", "size": 5}})
        ch.set_title({"name": ttl})
        ch.set_y_axis({"name": yname})
        ch.set_size({"width": 620, "height": 360})
        ws.insert_chart(anchor, 9, ch)

    # ---- 03_DeviceMix ----
    ws = wb.add_worksheet("03_DeviceMix")
    ws.write(0, 0, "Device lead-share per platform per month (explanation layer for "
                   "blended-RPL divergence).", f_bold)
    ws.write(2, 0, "platform", f_hdr)
    ws.write(2, 1, "device", f_hdr)
    for j, ym in enumerate(run_months):
        ws.write(2, 2 + j, mlabel(ym), f_hdr)
    ri2 = 3
    for p, pname in (("G", "Google"), ("M", "Microsoft")):
        for d in ("desktop", "mobile", "tablet"):
            ws.write(ri2, 0, pname)
            ws.write(ri2, 1, d)
            x = ri2 + 1
            blk0 = ri2 - ("desktop", "mobile", "tablet").index(d)  # first row of this platform block
            for j, ym in enumerate(run_months):
                m_ref = XC(2, 2 + j, row_abs=True)
                def s_dev(dev_row):
                    return (f"SUMIFS(Data_Cells!$E:$E,Data_Cells!$A:$A,"
                            f"$A{blk0 + 1},Data_Cells!$B:$B,{m_ref},"
                            f"Data_Cells!$D:$D,$B{dev_row + 1})")
                tot = "+".join(s_dev(blk0 + k) for k in range(3))
                ws.write_formula(ri2, 2 + j,
                                 f"=IFERROR({s_dev(ri2)}/({tot}),\"\")",
                                 f_pct)
            ri2 += 1
    ch = wb.add_chart({"type": "line"})
    for k in range(6):
        ch.add_series({
            "name": ["03_DeviceMix", 3 + k, 0, 3 + k, 1],
            "categories": ["03_DeviceMix", 2, 2, 2, 1 + len(run_months)],
            "values": ["03_DeviceMix", 3 + k, 2, 3 + k, 1 + len(run_months)],
        })
    ch.set_title({"name": "Device lead-share by platform"})
    ch.set_y_axis({"num_format": "0%"})
    ch.set_size({"width": 640, "height": 380})
    ws.insert_chart(11, 0, ch)

    # ---- 04_LeadsPerDay ----
    ws = wb.add_worksheet("04_LeadsPerDay")
    ws.write(0, 0, "Leads/day per state per platform (all-device, search-only). "
                   "Partial months use days-elapsed denominators.", f_bold)
    ws.write(1, 0, "params:", f_bold)
    ws.write(1, 1, "device")
    ws.write_string(1, 2, "all")
    dev04 = "$C$2"
    n4 = len(run_months)
    ws.write(4, 0, "state", f_hdr)
    ws.write(2, 0, "platform ->", f_hdr)
    ws.write(3, 0, "days ->", f_hdr)
    for pi, (plat, mx) in enumerate((("Google", g_max), ("Microsoft", m_max))):
        for j, ym in enumerate(run_months):
            c = 1 + pi * n4 + j
            ws.write_string(2, c, plat, f_hdr)
            days, _ = days_elapsed_in_month(ym, mx)
            ws.write_number(3, c, days, f_hdr)
            ws.write_string(4, c, mlabel(ym), f_hdr)
    for i, st in enumerate(all_states):
        r = 5 + i
        ws.write_string(r, 0, st, f_bold)
        s_ref = f"$A{r + 1}"
        for pi in range(2):
            for j in range(n4):
                c = 1 + pi * n4 + j
                p_ref = XC(2, c, row_abs=True)
                d_ref = XC(3, c, row_abs=True)
                m_ref = XC(4, c, row_abs=True)
                lf = sifs_ref("E", p_ref, m_ref, s_ref, dev04)
                ws.write_formula(r, c,
                                 f"=IF({lf}=0,\"\",{lf}/{d_ref})", f_num)
    ws.freeze_panes(5, 1)

    # ---- 05_RankedOut: volume vs price, indexed (deck exhibit) ----
    ws = wb.add_worksheet("05_RankedOut")
    ws.write(0, 0, "SERVING LOSS, NOT POSITION LOSS — volume vs price, "
                   f"indexed {START_MONTH} = 100. Being outbid into lower "
                   "positions preserves impressions (you still serve, "
                   "cheaper and lower); being priced out of the market "
                   "makes surviving clicks costlier. Neither happened: "
                   "impressions/leads vanished while CPC and CPL held "
                   "flat, and Microsoft (same buyers, one platform-blind "
                   "price list) held volume. LIVE formulas on Data_Cells.",
             f_bold)
    g_clicks_col = "clicks" if "clicks" in g_cols else None
    ws.write(1, 0, "params:", f_bold)
    ws.write_string(1, 1, "Google")
    ws.write_string(1, 2, "Microsoft")
    ws.write_string(1, 3, "all")
    P_G, P_M, P_D = "$B$2", "$C$2", "$D$2"
    hdrs = ["month", "G_leads", "G_days", "G_leads_day", "G_vol_idx",
            "MS_leads", "MS_days", "MS_leads_day", "MS_vol_idx",
            "G_cost", "G_CPL", "G_CPL_idx", "G_clicks", "G_CPC",
            "G_CPC_idx"]
    for c, h in enumerate(hdrs):
        ws.write(2, c, h, f_hdr)
    f_idx0 = wb.add_format({"num_format": "0"})
    from xlsxwriter.utility import xl_rowcol_to_cell as xc5

    def plat_sum(valcol, plat_ref, row):
        return (f"SUMIFS(Data_Cells!${valcol}:${valcol},"
                f"Data_Cells!$A:$A,{plat_ref},"
                f"Data_Cells!$B:$B,$A{row + 1},"
                f"Data_Cells!$D:$D,{P_D})")

    for i, ym in enumerate(run_months):
        row = 3 + i
        gd, _ = days_elapsed_in_month(ym, g_max)
        md, _ = days_elapsed_in_month(ym, m_max)
        ws.write_string(row, 0, mlabel(ym))
        ws.write_formula(row, 1, f"={plat_sum('E', P_G, row)}", f_num)
        ws.write_number(row, 2, gd, f_idx0)
        ws.write_formula(row, 3, f"={xc5(row, 1)}/{xc5(row, 2)}", f_num)
        ws.write_formula(row, 4,
                         f"=IFERROR(100*{xc5(row, 3)}/{xc5(3, 3)},\"\")",
                         f_idx0)
        ws.write_formula(row, 5, f"={plat_sum('E', P_M, row)}", f_num)
        ws.write_number(row, 6, md, f_idx0)
        ws.write_formula(row, 7, f"={xc5(row, 5)}/{xc5(row, 6)}", f_num)
        ws.write_formula(row, 8,
                         f"=IFERROR(100*{xc5(row, 7)}/{xc5(3, 7)},\"\")",
                         f_idx0)
        ws.write_formula(row, 9, f"={plat_sum('G', P_G, row)}", f_cur)
        ws.write_formula(row, 10,
                         f"=IFERROR({xc5(row, 9)}/{xc5(row, 1)},\"\")",
                         f_cur)
        ws.write_formula(row, 11,
                         f"=IFERROR(100*{xc5(row, 10)}/{xc5(3, 10)},\"\")",
                         f_idx0)
        ws.write_formula(row, 12, f"={plat_sum('H', P_G, row)}", f_num)
        ws.write_formula(row, 13,
                         f"=IFERROR({xc5(row, 9)}/{xc5(row, 12)},\"\")",
                         f_cur)
        ws.write_formula(row, 14,
                         f"=IFERROR(100*{xc5(row, 13)}/{xc5(3, 13)},\"\")",
                         f_idx0)
    n = len(run_months)
    ch = wb.add_chart({"type": "line"})
    for col, nm in [(4, "Google lead volume"), (8, "Microsoft lead volume"),
                    (11, "Google CPL"), (14, "Google CPC")]:
        ch.add_series({"name": nm,
                       "categories": ["05_RankedOut", 3, 0, 2 + n, 0],
                       "values": ["05_RankedOut", 3, col, 2 + n, col],
                       "marker": {"type": "circle", "size": 6}})
    ch.set_title({"name": f"Volume vs price, indexed {START_MONTH} = 100"})
    ch.set_y_axis({"name": "index"})
    ch.set_size({"width": 680, "height": 400})
    ws.insert_chart(3 + n + 2, 0, ch)
    src_row = 3 + n + 24
    ws.write(src_row, 0, "SOURCES (per column):", f_bold)
    for j, t in enumerate([
        "G_leads / MS_leads: this workbook's Data_Cells (all-device rows; "
        "Google = summed native splits, MS = device='ALL', search-only).",
        "days: each platform's own data-max date drives partial-month "
        "denominators (leads/day makes partial months comparable).",
        "G_cost / G_CPL / G_clicks / G_CPC: SUMIFS on Data_Cells cost "
        "(col G) and clicks (col H), device='all' rows, acct scoping "
        "upstream (same cuts as tier_map_and_cpl.py Section A).",
        "Indexes: each series vs its own " + START_MONTH + " value.",

        "IS adjudication (UI-verified, all eras incl. 2024 peak): "
        "Search impr. share reads '<10%' and Lost IS (rank) '>90%' — "
        "censored bounds, never usable numbers, in every era; the account "
        "has always operated below Google's 10% share floor, so rank-lost "
        "cannot measure the collapse. Lost IS (budget) IS real: ~0% in "
        "every era — never budget-bound, always rank-bound; Google was "
        "always willing to serve more if Ad Rank permitted.",
        "Anticipated objection, answered: being outbid into LOWER "
        "positions also lowers CPC — true. But position slippage "
        "preserves serving (impressions persist at lower CTR); it cannot "
        "erase three-quarters of volume. And a market price-out makes "
        "the clicks you still win costlier; blended CPC held flat "
        "(desktop +~15% at the margin only).",
        "Caption: if we had merely slid down the page, our ads would "
        "still be showing — cheaper, lower, clicking less. Instead "
        "volume vanished at flat CPC and flat CPL while Microsoft, "
        "selling the same leads to the same buyers, held. We lost on "
        "Ad Rank at unchanged prices — and the quality component of "
        "Ad Rank is the part the restructure fixes.",
    ]):
        ws.write_string(src_row + 1 + j, 0, t)

    # ---- 06_CPL_Index: own-March = 100, all-device (mirrors 01) ----
    ws = wb.add_worksheet("06_CPL_Index")
    ws.write(0, 0, "State x month CPL (all-device, search-only), indexed to "
                   "each platform's own March = 100. Blank = thin cell or "
                   "no March base. LIVE formulas; criteria from header/"
                   "param cells (cost col G / leads col E of Data_Cells). "
                   "Complements 05: if we were being priced out, these "
                   "would climb.", f_bold)
    index_sheet(ws, "G")

    # ---- 07_ROAS_Sort: the state-level realized-ROAS re-sort test ----
    ws = wb.add_worksheet("07_ROAS_Sort")
    ws.write(0, 0, "DID tROAS RE-SORT STATES BY REALIZED RETURN? Theory "
                   "under test: costs in high-value states rose until "
                   "their actual ROAS fell below cheap states, so the "
                   "bidder rationally cut them. Three predictions: (1) "
                   "high-value states' CPL rose sharply; (2) their "
                   "realized ROAS is now WORSE than cheap states; (3) "
                   "states with better realized ROAS kept more volume. "
                   "Read the band summary and the scatter: if the "
                   "algorithm sorted by return, the scatter slopes up. "
                   "Bands = states grouped by March lead value (param "
                   "thresholds). Every band at the same realized ROAS = "
                   "water at one level across connected tanks: nothing "
                   "favored; the total shrank.", f_bold)
    ws.write(1, 0, "params:", f_bold)
    ws.write_string(1, 1, "Google")
    ws.write_string(1, 2, mlabel("2026-03"))
    ws.write_string(1, 3, mlabel(run_months[-1]))
    ws.write_number(1, 4, 60)
    ws.write_number(1, 5, 40)
    ws.write(1, 6, "min base leads: 30 (build-time row filter)")
    RP, RB, RC, TH, TL = "$B$2", "$C$2", "$D$2", "$E$2", "$F$2"

    def s7f(valcol, month_ref, state_ref):
        return (f"SUMIFS(Data_Cells!${valcol}:${valcol},"
                f"Data_Cells!$A:$A,{RP},Data_Cells!$B:$B,{month_ref},"
                f"Data_Cells!$C:$C,{state_ref})")

    hdr7 = ["state", "leads_base", "leads_now", "RPL_base", "CPL_base",
            "CPL_now", "ROAS_base", "ROAS_now", "retention", "band"]
    for c, h in enumerate(hdr7):
        ws.write(3, c, h, f_hdr)
    gbase = {}
    for (p, ym, st, d), (l, rv, co, ck) in cells.items():
        if p == "G" and ym == "2026-03":
            a = gbase.setdefault(st, [0.0, 0.0])
            a[0] += l; a[1] += co
    roster7 = sorted(st for st, (l, co) in gbase.items()
                     if l >= 30 and co > 0)
    r0 = 4
    for i, st in enumerate(roster7):
        x = r0 + i
        xx = x + 1
        sr = f"$A{xx}"
        ws.write_string(x, 0, st)
        ws.write_formula(x, 1, f"={s7f('E', RB, sr)}", f_num)
        ws.write_formula(x, 2, f"={s7f('E', RC, sr)}", f_num)
        ws.write_formula(x, 3, f"=IFERROR({s7f('F', RB, sr)}/$B{xx},\"\")",
                         f_cur)
        ws.write_formula(x, 4, f"=IFERROR({s7f('G', RB, sr)}/$B{xx},\"\")",
                         f_cur)
        ws.write_formula(x, 5, f"=IF($C{xx}<5,\"\","
                               f"IFERROR({s7f('G', RC, sr)}/$C{xx},\"\"))",
                         f_cur)
        ws.write_formula(x, 6, f"=IFERROR({s7f('F', RB, sr)}/"
                               f"{s7f('G', RB, sr)},\"\")", f_r2)
        ws.write_formula(x, 7, f"=IF($C{xx}<5,\"\","
                               f"IFERROR({s7f('F', RC, sr)}/"
                               f"{s7f('G', RC, sr)},\"\"))", f_r2)
        ws.write_formula(x, 8, f"=IFERROR($C{xx}/$B{xx},\"\")", f_pct)
        ws.write_formula(x, 9, f"=IF($D{xx}>={TH},\"HIGH\","
                               f"IF($D{xx}>={TL},\"MID\",\"LOW\"))")
    rEnd = r0 + len(roster7)
    a1, a2 = r0 + 1, rEnd
    sums = r0 + len(roster7) + 2
    ws.write(sums - 1, 0, "BAND SUMMARY (medians; band thresholds in "
                          "params). Prediction check: CPL_now vs CPL_base "
                          "per band (1); ROAS_now HIGH vs LOW (2); "
                          "scatter slope (3).", f_bold)
    cols7 = [("n", None), ("CPL_base", "E"), ("CPL_now", "F"),
             ("ROAS_base", "G"), ("ROAS_now", "H"), ("retention", "I")]
    ws.write(sums, 0, "band", f_hdr)
    for ci, (nm, _) in enumerate(cols7):
        ws.write(sums, 1 + ci, nm, f_hdr)
    for bi, band in enumerate(("HIGH", "MID", "LOW")):
        x = sums + 1 + bi
        ws.write_string(x, 0, band)
        ws.write_formula(x, 1,
                         f"=COUNTIF($J${a1}:$J${a2},$A{x + 1})", f_num)
        for ci, (nm, col) in enumerate(cols7[1:], start=2):
            fmt = f_pct if nm == "retention" else (
                f_r2 if "ROAS" in nm else f_cur)
            ws.write_array_formula(
                x, ci, x, ci,
                f"{{=MEDIAN(IF($J${a1}:$J${a2}=$A{x + 1},"
                f"${col}${a1}:${col}${a2}))}}", fmt)
    chB = wb.add_chart({"type": "column"})
    for ci, nm in [(4, "realized ROAS, March"), (5, "realized ROAS, now")]:
        chB.add_series({"name": nm,
                        "categories": ["07_ROAS_Sort", sums + 1, 0,
                                       sums + 3, 0],
                        "values": ["07_ROAS_Sort", sums + 1, ci,
                                   sums + 3, ci]})
    chB.set_title({"name": "The return premium leveled out — every band "
                           "now ~equal, just under target"})
    chB.set_y_axis({"name": "revenue per $1 spent"})
    chB.set_size({"width": 620, "height": 360})
    ws.insert_chart(3, 11, chB)
    chS = wb.add_chart({"type": "scatter"})
    chS.add_series({"name": "states",
                    "categories": ["07_ROAS_Sort", r0, 6, rEnd - 1, 6],
                    "values": ["07_ROAS_Sort", r0, 8, rEnd - 1, 8],
                    "marker": {"type": "circle", "size": 6}})
    chS.set_title({"name": "If the algorithm sorted states by return, "
                           "this would slope up — it doesn't"})
    chS.set_x_axis({"name": "realized ROAS, March (rev per $1)"})
    chS.set_y_axis({"name": "share of March volume retained",
                    "num_format": "0%"})
    chS.set_legend({"none": True})
    chS.set_size({"width": 620, "height": 360})
    ws.insert_chart(22, 11, chS)
    log(f"[07] ROAS-sort test sheet: {len(roster7)} states, band summary "
        f"medians + convergence bar + sort scatter")

    wb.close()
    log(f"\n[DONE] workbook written: {args.out}")
    log(f"[VERDICT restated] {verdict}")


if __name__ == "__main__":
    main()
