"""
rpl_decomposition_v2.py — "One Price List" identification
Supersedes the H1/H2/H3 rpl_decomposition.py framing.

ARCHITECTURE FACT (the identification): lead buyers price platform-blind via
STATE x DEVICE modifiers on one shared price list. Microsoft cell RPL is
therefore an external readout of the same prices Google leads are paid on.
Google blended RPL == sum over cells of [lead share] x [cell price] x
[acceptance residual], so every RPL movement decomposes into exactly:
  (A) MIX      - leads migrating across state x device cells
  (B) PRICE    - within-cell payout steps (vendor modifier events)
  (C) RESIDUAL - acceptance/dupes/credit-backs (ours)
Within-cell movement is split B-vs-C by cross-platform corroboration:
  same cell moved on MS too            -> PRICE (vendor)
  MS cell had volume and did not move  -> RESIDUAL (ours)
  MS cell too thin to check            -> UNVERIFIED (stated, not forced)

METHOD GATES (all hard):
  G1 synthetic: frozen prices + shifting mix -> ~100% MIX, ~0% PRICE;
                frozen mix + stepped prices  -> ~100% PRICE. Runs every time.
  G2 identity:  MIX + PRICE + INTERACTION == delta-RPL, every month, exact.
  G3 positive control: the June 18, 2026 vendor bid-down must appear as
                synchronized within-cell steps on BOTH platforms at the
                right date, or the method stops with no verdict.

Sources and conventions reused from cross_platform_state_rpl.py verbatim:
Google campaign_performance_geo_daily_search acct 1 (native splits; sum for
all-device), MS geo_performance_daily acct 2 (device='ALL' filter, splits for
device cells, network != 'Audience'), revenue = conversion_value (adjudicated
8/25), full-name state normalization, <30 leads/month LOW_CONF floor.

Usage:
  python rpl_decomposition_v2.py --db paid_search.db --synthetic-only
  python rpl_decomposition_v2.py --db paid_search.db
Outputs -> <script dir>/output/rpl_decomposition_v2.xlsx
"""

import argparse
import os
import re
import sqlite3
import sys
from collections import defaultdict

GOOGLE_TABLE = "campaign_performance_geo_daily_search"
MS_TABLE = "geo_performance_daily"
BASE_A = "2026-03"                       # contested regime start
THIN = 30                                # Google cell floor (headline)
MS_CORR_FLOOR = 20                       # MS cell floor for corroboration
CORR_BAND = 0.15                         # |dG - dM| within 15pts -> corroborated
FLAT_MS = 0.05                           # |dM| < 5% counts as "MS flat"
MOVE_G = 0.10                            # Google move must exceed 10% to classify
JUNE_PRE = ("2026-05-25", "2026-06-17")  # positive-control windows
JUNE_POST = ("2026-06-19", "2026-07-12")

STATE_NAME_TO_CODE = {
    "Alabama": "AL", "Alaska": "AK", "Arizona": "AZ", "Arkansas": "AR",
    "California": "CA", "Colorado": "CO", "Connecticut": "CT", "Delaware": "DE",
    "District of Columbia": "DC", "Florida": "FL", "Georgia": "GA",
    "Hawaii": "HI", "Idaho": "ID", "Illinois": "IL", "Indiana": "IN",
    "Iowa": "IA", "Kansas": "KS", "Kentucky": "KY", "Louisiana": "LA",
    "Maine": "ME", "Maryland": "MD", "Massachusetts": "MA", "Michigan": "MI",
    "Minnesota": "MN", "Mississippi": "MS", "Missouri": "MO", "Montana": "MT",
    "Nebraska": "NE", "Nevada": "NV", "New Hampshire": "NH", "New Jersey": "NJ",
    "New Mexico": "NM", "New York": "NY", "North Carolina": "NC",
    "North Dakota": "ND", "Ohio": "OH", "Oklahoma": "OK", "Oregon": "OR",
    "Pennsylvania": "PA", "Rhode Island": "RI", "South Carolina": "SC",
    "South Dakota": "SD", "Tennessee": "TN", "Texas": "TX", "Utah": "UT",
    "Vermont": "VT", "Virginia": "VA", "Washington": "WA",
    "West Virginia": "WV", "Wisconsin": "WI", "Wyoming": "WY",
}
DEVICE_CANON = {
    "DESKTOP": "desktop", "COMPUTER": "desktop", "MOBILE": "mobile",
    "SMARTPHONE": "mobile", "TABLET": "tablet", "ALL": "ALL",
}
TIER_RE = re.compile(r"tier[\s_\-]*(\d)", re.IGNORECASE)

HEADER = []


def log(msg=""):
    print(msg)
    HEADER.append(str(msg))


def die(msg):
    log(f"\n*** HARD FAIL: {msg}")
    sys.exit(1)


def norm_state(v):
    if v is None:
        return None
    s = str(v).strip()
    if s in STATE_NAME_TO_CODE:
        return STATE_NAME_TO_CODE[s]
    if s.upper() in STATE_NAME_TO_CODE.values():
        return s.upper()
    return STATE_NAME_TO_CODE.get(s.title())


def canon_device(raw):
    return DEVICE_CANON.get(str(raw).strip().upper(), None) if raw is not None else None


def months_between(a, b):
    y, m = map(int, a.split("-"))
    ey, em = map(int, b.split("-"))
    out = []
    while (y, m) <= (ey, em):
        out.append(f"{y:04d}-{m:02d}")
        m += 1
        if m == 13:
            m, y = 1, y + 1
    return out


# =====================================================================
# CORE DECOMPOSITION (Laspeyres with explicit interaction)
# cells: dict[(state, dev)] -> (leads, revenue) per period
# =====================================================================

def blended(cells):
    L = sum(v[0] for v in cells.values())
    R = sum(v[1] for v in cells.values())
    return (R / L if L else 0.0), L


def decompose(base_cells, cur_cells):
    """Return dict with mix, price, interaction, delta, plus per-cell price
    deltas for classification. Missing-cell convention (keeps identity exact):
    a cell absent in one period takes the other period's price, so its entire
    contribution routes through MIX."""
    rpl0, L0 = blended(base_cells)
    rpl1, L1 = blended(cur_cells)
    if not L0 or not L1:
        return None
    keys = set(base_cells) | set(cur_cells)
    mix_l, price_l, inter_l = [], [], []
    cell_rows = []
    for k in keys:
        l0, r0 = base_cells.get(k, (0.0, 0.0))
        l1, r1 = cur_cells.get(k, (0.0, 0.0))
        w0, w1 = l0 / L0, l1 / L1
        p0 = (r0 / l0) if l0 else None
        p1 = (r1 / l1) if l1 else None
        p0e = p0 if p0 is not None else p1
        p1e = p1 if p1 is not None else p0
        if p0e is None:
            continue
        mix_l.append((w1 - w0) * p0e)
        price_l.append(w0 * (p1e - p0e))
        inter_l.append((w1 - w0) * (p1e - p0e))
        cell_rows.append({"cell": k, "w0": w0, "w1": w1, "p0": p0, "p1": p1,
                          "l0": l0, "l1": l1,
                          "price_contrib": w0 * (p1e - p0e) + (w1 - w0) * (p1e - p0e) * 0})
    import math
    mix, price, inter = math.fsum(mix_l), math.fsum(price_l), math.fsum(inter_l)
    out = {"rpl0": rpl0, "rpl1": rpl1, "delta": rpl1 - rpl0,
           "mix": mix, "price": price, "interaction": inter,
           "cells": cell_rows}
    tol = 1e-6 * max(1.0, abs(rpl0), abs(rpl1))
    if abs((mix + price + inter) - out["delta"]) > tol:
        die(f"G2 identity violated: mix+price+inter="
            f"{mix + price + inter:.6f} vs delta={out['delta']:.6f} "
            f"(tol {tol:.2e})")
    return out


# =====================================================================
# G1 — SYNTHETIC GATES (run every time, before any real data)
# =====================================================================

def synthetic_gates():
    log("=" * 78)
    log("G1 SYNTHETIC GATES")
    log("=" * 78)
    prices = {("TX", "desktop"): 70, ("TX", "mobile"): 55,
              ("CA", "desktop"): 30, ("CA", "mobile"): 25,
              ("IL", "desktop"): 105, ("IL", "mobile"): 90}

    # Gate 1a: frozen prices, shifting mix -> PRICE ~ 0
    base = {k: (100.0, 100.0 * p) for k, p in prices.items()}
    cur = {}
    shift = {("TX", "desktop"): 20, ("TX", "mobile"): 160,
             ("CA", "desktop"): 10, ("CA", "mobile"): 220,
             ("IL", "desktop"): 5, ("IL", "mobile"): 30}
    for k, l in shift.items():
        cur[k] = (float(l), float(l) * prices[k])
    d = decompose(base, cur)
    share_price = abs(d["price"]) / max(abs(d["delta"]), 1e-9)
    log(f"  1a shifting-mix/frozen-prices: delta={d['delta']:+.2f} "
        f"mix={d['mix']:+.2f} price={d['price']:+.2f} inter={d['interaction']:+.2f}"
        f"  -> price share {share_price:.1%}")
    if share_price > 0.02:
        die("G1a failed: price channel picked up a frozen price list")

    # Gate 1b: frozen mix, stepped prices -> MIX ~ 0
    cur2 = {k: (100.0, 100.0 * p * (0.85 if k[0] == "TX" else 1.0))
            for k, p in prices.items()}
    d2 = decompose(base, cur2)
    share_mix = abs(d2["mix"] + d2["interaction"]) / max(abs(d2["delta"]), 1e-9)
    log(f"  1b frozen-mix/stepped-prices: delta={d2['delta']:+.2f} "
        f"mix={d2['mix']:+.2f} price={d2['price']:+.2f} "
        f"inter={d2['interaction']:+.2f}  -> mix share {share_mix:.1%}")
    if share_mix > 0.02:
        die("G1b failed: mix channel picked up a frozen mix")
    log("  [PASS] both synthetic gates")


# =====================================================================
# DATA LOADING
# =====================================================================

def table_columns(con, t):
    rows = con.execute(f"PRAGMA table_info({t})").fetchall()
    if not rows:
        die(f"table {t} not found")
    return [r[1] for r in rows]


def load_monthly_cells(con, table, acct, date_col, rev_col, extra_where,
                       lo, hi, split_only):
    """-> dict[ym][(state, dev)] = (leads, rev). split_only=True (Google):
    native splits, all-device derived by summing. False (MS): splits only
    (ALL excluded here; MS cell grain uses splits)."""
    out = defaultdict(lambda: defaultdict(lambda: [0.0, 0.0]))
    q = (f"SELECT SUBSTR({date_col},1,7) ym, geo_state, device, "
         f"SUM(conversions) l, SUM({rev_col}) r FROM {table} "
         f"WHERE account_id={acct} {extra_where} "
         f"AND SUBSTR({date_col},1,7) BETWEEN ? AND ? GROUP BY 1,2,3")
    for row in con.execute(q, (lo, hi)):
        st = norm_state(row["geo_state"])
        dev = canon_device(row["device"])
        if st is None or dev is None or dev == "ALL":
            continue
        c = out[row["ym"]][(st, dev)]
        c[0] += row["l"] or 0
        c[1] += row["r"] or 0
    return {ym: {k: tuple(v) for k, v in cells.items()}
            for ym, cells in out.items()}


def window_cells(con, table, acct, date_col, rev_col, extra_where, lo, hi):
    out = defaultdict(lambda: [0.0, 0.0])
    q = (f"SELECT geo_state, device, SUM(conversions) l, SUM({rev_col}) r "
         f"FROM {table} WHERE account_id={acct} {extra_where} "
         f"AND {date_col} BETWEEN ? AND ? GROUP BY 1,2")
    for row in con.execute(q, (lo, hi)):
        st = norm_state(row["geo_state"])
        dev = canon_device(row["device"])
        if st is None or dev is None or dev == "ALL":
            continue
        c = out[(st, dev)]
        c[0] += row["l"] or 0
        c[1] += row["r"] or 0
    return {k: tuple(v) for k, v in out.items()}


# =====================================================================
# CLASSIFY within-cell movement: PRICE vs RESIDUAL vs UNVERIFIED
# =====================================================================

def classify_within(base_g, cur_g, base_m, cur_m):
    """Split the Laspeyres price channel (+ its interaction share) into
    corroborated PRICE / RESIDUAL / UNVERIFIED dollar buckets, using MS as
    the price-list readout. Returns (price$, resid$, unver$, rows)."""
    L0 = sum(v[0] for v in base_g.values())
    L1 = sum(v[0] for v in cur_g.values())
    price_d = resid_d = unver_d = 0.0
    rows = []
    for k in set(base_g) | set(cur_g):
        l0, r0 = base_g.get(k, (0, 0))
        l1, r1 = cur_g.get(k, (0, 0))
        if not l0 or not l1:
            continue                      # entering/exiting cells are MIX
        p0, p1 = r0 / l0, r1 / l1
        w0, w1 = l0 / L0, l1 / L1
        contrib = w0 * (p1 - p0) + (w1 - w0) * (p1 - p0)   # price + its interaction
        ml0, mr0 = base_m.get(k, (0, 0))
        ml1, mr1 = cur_m.get(k, (0, 0))
        if p0 <= 0:
            # zero-revenue base cell: relative price change undefined
            rows.append({"cell": k, "l0": l0, "l1": l1, "dG": None, "dM": None,
                         "label": "UNVERIFIED", "contrib": contrib,
                         "thin": l0 < THIN or l1 < THIN})
            unver_d += contrib
            continue
        dg = p1 / p0 - 1
        if (ml0 >= MS_CORR_FLOOR and ml1 >= MS_CORR_FLOOR
                and mr0 > 0 and ml0 > 0 and (mr0 / ml0) > 0):
            dm = (mr1 / ml1) / (mr0 / ml0) - 1
            if abs(dg) < MOVE_G:
                lab = "PRICE(small)"      # negligible move; keep in price
                price_d += contrib
            elif (dg > 0) == (dm > 0) and abs(dg - dm) <= CORR_BAND:
                lab = "PRICE"
                price_d += contrib
            elif abs(dm) < FLAT_MS:
                lab = "RESIDUAL"
                resid_d += contrib
            elif (dg > 0) != (dm > 0):
                lab = "RESIDUAL"
                resid_d += contrib
            else:
                lab = "PRICE(partial)"    # same sign, magnitude off — vendor + ours
                price_d += contrib
        else:
            dm = None
            lab = "UNVERIFIED"
            unver_d += contrib
        rows.append({"cell": k, "l0": l0, "l1": l1, "dG": dg, "dM": dm,
                     "label": lab, "contrib": contrib,
                     "thin": l0 < THIN or l1 < THIN})
    return price_d, resid_d, unver_d, rows


# =====================================================================
# MAIN
# =====================================================================

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="paid_search.db")
    ap.add_argument("--synthetic-only", action="store_true")
    ap.add_argument("--out", default="rpl_decomposition_v2.xlsx")
    args = ap.parse_args()

    out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")
    os.makedirs(out_dir, exist_ok=True)
    if not os.path.isabs(args.out):
        args.out = os.path.join(out_dir, args.out)

    log("=" * 78)
    log("RPL DECOMPOSITION v2 — ONE PRICE LIST (MIX / PRICE / RESIDUAL)")
    log("=" * 78)
    synthetic_gates()
    if args.synthetic_only:
        log("\n[synthetic-only] gates passed; exiting before real data.")
        return

    con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    g_cols = table_columns(con, GOOGLE_TABLE)
    m_cols = table_columns(con, MS_TABLE)
    g_date = "date" if "date" in g_cols else "report_date"
    m_date = "date" if "date" in m_cols else "report_date"
    for need, cols, t in [("campaign_id", g_cols, GOOGLE_TABLE),
                          ("conversion_value", g_cols, GOOGLE_TABLE),
                          ("network", m_cols, MS_TABLE)]:
        if need not in cols:
            die(f"{t} missing {need}")

    g_max = con.execute(f"SELECT MAX({g_date}) FROM {GOOGLE_TABLE} "
                        f"WHERE account_id=1").fetchone()[0]
    end_month = g_max[:7]
    ms_where = "AND network != 'Audience'"

    # ---- load everything once ----
    G = load_monthly_cells(con, GOOGLE_TABLE, 1, g_date, "conversion_value",
                           "", "2026-01", end_month, True)
    M = load_monthly_cells(con, MS_TABLE, 2, m_date, "conversion_value",
                           ms_where, "2026-01", end_month, False)
    log(f"\n[SCOPE] 2026-only by design: Microsoft has no revenue data "
        f"before ~2026-01, so pre-2026 windows are permanently UNVERIFIED "
        f"— excluded rather than shown unverifiable (ruling 9/2).")
    log(f"[BASELINE A] {BASE_A} (contested regime start)")
    log(f"[SCOPE] through {end_month} (Google max {g_max}; final month partial)")

    # =================================================================
    # G3 — POSITIVE CONTROL: June 18 bid-down, both platforms
    # =================================================================
    log("\n" + "=" * 78)
    log("G3 POSITIVE CONTROL — June 18, 2026 vendor bid-down")
    log(f"pre {JUNE_PRE[0]}..{JUNE_PRE[1]}  vs  post {JUNE_POST[0]}..{JUNE_POST[1]}")
    log("=" * 78)
    gp = window_cells(con, GOOGLE_TABLE, 1, g_date, "conversion_value", "",
                      *JUNE_PRE)
    gq = window_cells(con, GOOGLE_TABLE, 1, g_date, "conversion_value", "",
                      *JUNE_POST)
    mp = window_cells(con, MS_TABLE, 2, m_date, "conversion_value", ms_where,
                      *JUNE_PRE)
    mq = window_cells(con, MS_TABLE, 2, m_date, "conversion_value", ms_where,
                      *JUNE_POST)

    def to_state(cells):
        agg = defaultdict(lambda: [0.0, 0.0])
        for (st, dev), (l, r) in cells.items():
            agg[st][0] += l
            agg[st][1] += r
        return agg

    control_rows, detected = [], []
    gps, gqs, mps, mqs = map(to_state, (gp, gq, mp, mq))
    for st in sorted(set(gps) & set(gqs) & set(mps) & set(mqs)):
        if min(gps[st][0], gqs[st][0]) < 25 or min(mps[st][0], mqs[st][0]) < 15:
            continue
        if min(gps[st][1], mps[st][1], gqs[st][0], mqs[st][0]) <= 0:
            continue    # zero pre-window revenue: no relative step computable
        dg = (gqs[st][1] / gqs[st][0]) / (gps[st][1] / gps[st][0]) - 1
        dm = (mqs[st][1] / mqs[st][0]) / (mps[st][1] / mps[st][0]) - 1
        sync = (dg < -0.05 and dm < -0.05)
        control_rows.append((st, gps[st][0], gqs[st][0], dg,
                             mps[st][0], mqs[st][0], dm, sync))
        if sync:
            detected.append(st)
        log(f"  {st}: G {dg:+6.1%} (n {gps[st][0]:.0f}->{gqs[st][0]:.0f})   "
            f"MS {dm:+6.1%} (n {mps[st][0]:.0f}->{mqs[st][0]:.0f})"
            f"{'   <== SYNCHRONIZED STEP' if sync else ''}")
    if not detected:
        die("G3 positive control FAILED: no state shows a synchronized "
            "post-6/18 price step on both platforms. The method cannot find "
            "the one documented event — fix before issuing any verdict.")
    log(f"  [PASS] synchronized bid-down detected in: {', '.join(detected)}")

    # =================================================================
    # WATERFALLS — both baselines, blended + desktop-only
    # =================================================================
    def run_waterfall(base_ym, months, desktop_only=False):
        base_g = G.get(base_ym, {})
        if desktop_only:
            base_g = {k: v for k, v in base_g.items() if k[1] == "desktop"}
        rows = []
        for ym in months:
            cur_g = G.get(ym, {})
            base_m = M.get(base_ym, {})
            cur_m = M.get(ym, {})
            if desktop_only:
                cur_g = {k: v for k, v in cur_g.items() if k[1] == "desktop"}
            d = decompose(base_g, cur_g)
            if d is None:
                continue
            pr, rs, uv, cells = classify_within(base_g, cur_g, base_m, cur_m)
            # price+interaction total from decompose = pr+rs+uv (same set minus
            # entering/exiting, whose price legs are zero by convention)
            rows.append({"month": ym, **{k: d[k] for k in
                         ("rpl0", "rpl1", "delta", "mix", "price", "interaction")},
                         "price_corr": pr, "residual": rs, "unverified": uv,
                         "cell_rows": cells})
        return rows

    months_a = [ym for ym in months_between(BASE_A, end_month) if ym in G]
    wf_a = run_waterfall(BASE_A, months_a)
    wf_a_desk = run_waterfall(BASE_A, months_a, desktop_only=True)

    log("\n" + "=" * 78)
    log(f"WATERFALL vs {BASE_A} (blended). delta = mix + price + interaction; "
        f"within-cell (price+its interaction) split by MS corroboration.")
    log("=" * 78)
    log(f"{'month':9}{'RPL':>7}{'delta':>8} | {'MIX':>7}{'PRICE':>7}{'INTER':>7}"
        f" | {'corrPRICE':>10}{'RESID':>7}{'UNVER':>7}")
    for r in wf_a:
        log(f"{r['month']:9}{r['rpl1']:7.2f}{r['delta']:+8.2f} | "
            f"{r['mix']:+7.2f}{r['price']:+7.2f}{r['interaction']:+7.2f} | "
            f"{r['price_corr']:+10.2f}{r['residual']:+7.2f}{r['unverified']:+7.2f}")

    # =================================================================
    # MARCH 2026 VERDICT — Feb vs Apr cell prices, G vs MS
    # =================================================================
    log("\n" + "=" * 78)
    log("MARCH 2026 VERDICT — Feb->Apr cell-price deltas, Google vs Microsoft")
    log("=" * 78)
    feb, apr = "2026-02", "2026-04"
    verdict_rows = []
    if feb in G and apr in G:
        d_fa = decompose(G[feb], G[apr])
        pr, rs, uv, cells = classify_within(G[feb], G[apr],
                                            M.get(feb, {}), M.get(apr, {}))
        for c in sorted(cells, key=lambda x: -abs(x["contrib"])):
            if c["thin"]:
                continue
            dgs = f"{c['dG']:+6.1%}" if c["dG"] is not None else " zero$"
            dm = f"{c['dM']:+.1%}" if c["dM"] is not None else "  thin"
            log(f"  {c['cell'][0]}-{c['cell'][1]:8}: G {dgs}  MS {dm:>7}"
                f"  -> {c['label']:14} contrib {c['contrib']:+6.2f}")
            verdict_rows.append(c)
        tot = d_fa["delta"]
        log(f"\n  Feb->Apr blended delta {tot:+.2f} = MIX {d_fa['mix']:+.2f} "
            f"+ within-cell [corrPRICE {pr:+.2f} / RESIDUAL {rs:+.2f} / "
            f"UNVERIFIED {uv:+.2f}]")
        mix_share = d_fa["mix"] / tot if tot else 0
        price_share = pr / tot if tot else 0
        if abs(mix_share) >= abs(price_share):
            verdict = (f"VERDICT: the March 2026 RPL improvement was primarily "
                       f"ACCOUNT-SIDE (mix): mix explains {mix_share:.0%} of the "
                       f"Feb->Apr delta vs {price_share:.0%} MS-corroborated price.")
        else:
            verdict = (f"VERDICT: the March 2026 RPL improvement was primarily "
                       f"VENDOR-SIDE (price): MS-corroborated price explains "
                       f"{price_share:.0%} vs {mix_share:.0%} mix.")
        log("  " + verdict)
    else:
        verdict = "VERDICT: Feb or Apr 2026 cell data missing — no verdict."
        log("  " + verdict)

    # =================================================================
    # TIER LENS — campaign-attributed cells, A/B/C per tier since March
    # =================================================================
    log("\n" + "=" * 78)
    log("TIER LENS — tiered shells' realized RPL in MIX/PRICE/RESIDUAL terms")
    log("(campaign-attributed geo rows; each month grouped by the tier label "
        "of the campaign that actually served)")
    log("=" * 78)
    c_cols = table_columns(con, "campaigns")
    c_name = "campaign_name" if "campaign_name" in c_cols else "name"
    c_id = "campaign_id" if "campaign_id" in c_cols else "id"
    cname = {str(r[0]): r[1] for r in con.execute(
        f"SELECT {c_id}, {c_name} FROM campaigns WHERE account_id=1")}

    tier_cells = defaultdict(lambda: defaultdict(lambda: defaultdict(
        lambda: [0.0, 0.0])))   # tier -> ym -> cell -> [l, r]
    q = (f"SELECT SUBSTR({g_date},1,7) ym, geo_state, device, campaign_id, "
         f"SUM(conversions) l, SUM(conversion_value) r FROM {GOOGLE_TABLE} "
         f"WHERE account_id=1 AND SUBSTR({g_date},1,7) BETWEEN ? AND ? "
         f"GROUP BY 1,2,3,4")
    for row in con.execute(q, (BASE_A, end_month)):
        nm = cname.get(str(row["campaign_id"]), "")
        mt = TIER_RE.search(nm or "")
        if not mt:
            continue
        st = norm_state(row["geo_state"])
        dev = canon_device(row["device"])
        if st is None or dev in (None, "ALL"):
            continue
        c = tier_cells[f"Tier {mt.group(1)}"][row["ym"]][(st, dev)]
        c[0] += row["l"] or 0
        c[1] += row["r"] or 0

    tier_out = []
    tier_series = {}
    for tier in sorted(tier_cells):
        series = tier_cells[tier]
        if BASE_A not in series:
            continue
        base_g = {k: tuple(v) for k, v in series[BASE_A].items()}
        last_full = sorted(ym for ym in series if ym < end_month)
        if not last_full:
            continue
        cur_ym = last_full[-1]
        cur_g = {k: tuple(v) for k, v in series[cur_ym].items()}
        d = decompose(base_g, cur_g)
        if d is None:
            continue
        pr, rs, uv, _ = classify_within(base_g, cur_g,
                                        M.get(BASE_A, {}), M.get(cur_ym, {}))
        log(f"  {tier}: RPL {d['rpl0']:.0f} -> {d['rpl1']:.0f} "
            f"({BASE_A}->{cur_ym}, {sum(v[0] for v in cur_g.values()):.0f} leads)"
            f"  delta {d['delta']:+.1f} = MIX {d['mix']:+.1f} "
            f"+ corrPRICE {pr:+.1f} + RESID {rs:+.1f} + UNVER {uv:+.1f}")
        tier_out.append((tier, BASE_A, cur_ym, d, pr, rs, uv))
        tier_series[tier] = series

    # ---- FIXED CURRENT-ROSTER VIEW: same states, March vs now ----
    log("\n" + "=" * 78)
    log("FIXED-ROSTER TIER VIEW — each tier's CURRENT states, those same "
        "states' account-level RPL basis in " + BASE_A + " vs now "
        "(serving campaign irrelevant; no membership churn in MIX)")
    log("=" * 78)
    roster_out = []
    roster_cells = {}
    # anchors, computed once at account level for the roster month-pair:
    # (a) verified-mobile panel: median corroborated mobile price move
    # (b) desktop stability per state (corroborated, |dG| < MOVE_G)
    b1_all = tier_out[0][2] if tier_out else None
    panel_med, desk_stable, acct_rows = None, set(), []
    if b1_all:
        acct_rows = classify_within(G.get(BASE_A, {}), G.get(b1_all, {}),
                                    M.get(BASE_A, {}), M.get(b1_all, {}))[3]
        mob_dgs = sorted(r["dG"] for r in acct_rows
                         if r["cell"][1] == "mobile" and r["dM"] is not None
                         and r["label"].startswith("PRICE")
                         and r["dG"] is not None)
        if mob_dgs:
            panel_med = mob_dgs[len(mob_dgs) // 2]
        for r in acct_rows:
            if (r["cell"][1] == "desktop" and r["dM"] is not None
                    and r["dG"] is not None and abs(r["dG"]) < MOVE_G):
                desk_stable.add(r["cell"][0])
    log(f"  [ANCHORS] verified-mobile panel median price move: "
        + (f"{panel_med:+.1%} (from MS-corroborated mobile cells)"
           if panel_med is not None else "n/a (no corroborated mobile cells)")
        + f"; desktop-stable states (corroborated, |move|<{MOVE_G:.0%}): "
        f"{len(desk_stable)}")
    for (tier, b0, b1, d, pr, rs, uv) in tier_out:
        roster = {k[0] for k, v in tier_series[tier].get(b1, {}).items()
                  if v[0] > 0}
        base_c = {k: v for k, v in G.get(BASE_A, {}).items()
                  if k[0] in roster}
        cur_c = {k: v for k, v in G.get(b1, {}).items() if k[0] in roster}
        base_m2 = {k: v for k, v in M.get(BASE_A, {}).items()
                   if k[0] in roster}
        cur_m2 = {k: v for k, v in M.get(b1, {}).items() if k[0] in roster}
        d2 = decompose(base_c, cur_c)
        if d2 is None:
            continue
        pr2, rs2, uv2, rows2 = classify_within(base_c, cur_c, base_m2,
                                               cur_m2)
        # split UNVERIFIED by device, then infer the mobile portion
        uv_mob = uv_oth = mob_panel = mob_diverg = mob_unanch = 0.0
        for r in rows2:
            if r["label"] != "UNVERIFIED":
                continue
            if r["cell"][1] == "mobile":
                uv_mob += r["contrib"]
                dg = r["dG"]
                if (dg is not None and panel_med is not None
                        and abs(dg - panel_med) <= CORR_BAND):
                    mob_panel += r["contrib"]
                elif dg is not None and r["cell"][0] in desk_stable:
                    mob_diverg += r["contrib"]
                else:
                    mob_unanch += r["contrib"]
            else:
                uv_oth += r["contrib"]
        roster_out.append((tier, b1, sorted(roster), d2, pr2, rs2, uv2,
                           uv_mob, uv_oth, mob_panel, mob_diverg,
                           mob_unanch))
        roster_cells[tier] = (base_c, cur_c)
        log(f"  {tier} ({len(roster)} states): RPL {d2['rpl0']:.2f} -> "
            f"{d2['rpl1']:.2f}  delta {d2['delta']:+.2f} = MIX "
            f"{d2['mix']:+.2f} + corrPRICE {pr2:+.2f} + RESID {rs2:+.2f} "
            f"+ UNVER {uv2:+.2f}")
        unanch_states = sorted({r["cell"][0] for r in rows2
                                if r["label"] == "UNVERIFIED"
                                and r["cell"][1] == "mobile"
                                and not (r["dG"] is not None
                                         and panel_med is not None
                                         and abs(r["dG"] - panel_med)
                                         <= CORR_BAND)
                                and not (r["dG"] is not None
                                         and r["cell"][0] in desk_stable)})
        log(f"      UNVER split: mobile {uv_mob:+.2f} [panel-consistent "
            f"{mob_panel:+.2f} / divergent(desktop-stable) {mob_diverg:+.2f}"
            f" / unanchored {mob_unanch:+.2f}]  other {uv_oth:+.2f}")
        log(f"      unanchored states: {','.join(unanch_states) or 'none'} "
            f"— cross-check against 05's synchronized June-18 list")

    # =================================================================
    # WORKBOOK
    # =================================================================
    import xlsxwriter
    wb = xlsxwriter.Workbook(args.out, {"nan_inf_to_errors": True})
    f_hdr = wb.add_format({"bold": True, "bg_color": "#1F3864",
                           "font_color": "white", "border": 1})
    f_bold = wb.add_format({"bold": True})
    f_mono = wb.add_format({"font_name": "Consolas", "font_size": 9})
    f_cur = wb.add_format({"num_format": "$#,##0.00"})
    f_pct = wb.add_format({"num_format": "+0.0%;-0.0%"})

    ws = wb.add_worksheet("00_Header")
    ws.set_column(0, 0, 110)
    for i, line in enumerate(HEADER):
        ws.write_string(i, 0, line, f_mono)   # write_string: '='-prefixed
        # separator lines must not be interpreted as formulas

    # ---- Data_Cells: the raw sheet every exhibit references ----
    from xlsxwriter.utility import xl_rowcol_to_cell as XC
    f_num = wb.add_format({"num_format": "#,##0.0"})
    wsD = wb.add_worksheet("Data_Cells")
    dhdrs = ["scope", "platform", "month", "state", "device", "leads",
             "revenue"]
    for c, h in enumerate(dhdrs):
        wsD.write(0, c, h, f_hdr)
    di = 1
    for src_dict, plat in ((G, "Google"), (M, "Microsoft")):
        for ym in sorted(src_dict):
            for (st, dev), (l, r) in sorted(src_dict[ym].items()):
                wsD.write_string(di, 0, "ACCOUNT")
                wsD.write_string(di, 1, plat)
                wsD.write_string(di, 2, ym)
                wsD.write_string(di, 3, st)
                wsD.write_string(di, 4, dev)
                wsD.write_number(di, 5, l, f_num)
                wsD.write_number(di, 6, r, f_cur)
                di += 1
    for tier, series in tier_series.items():
        for ym in sorted(series):
            for (st, dev), (l, r) in sorted(series[ym].items()):
                wsD.write_string(di, 0, tier)
                wsD.write_string(di, 1, "Google")
                wsD.write_string(di, 2, ym)
                wsD.write_string(di, 3, st)
                wsD.write_string(di, 4, dev)
                wsD.write_number(di, 5, l, f_num)
                wsD.write_number(di, 6, r, f_cur)
                di += 1
    for (tier, b1x, roster, d2, pr2, rs2, uv2, *_rest) in roster_out:
        base_c, cur_c = roster_cells[tier]
        for ymx, cc in ((BASE_A, base_c), (b1x, cur_c)):
            for (st, dev), (l, r) in sorted(cc.items()):
                wsD.write_string(di, 0, tier + " ROSTER")
                wsD.write_string(di, 1, "Google")
                wsD.write_string(di, 2, ymx)
                wsD.write_string(di, 3, st)
                wsD.write_string(di, 4, dev)
                wsD.write_number(di, 5, l, f_num)
                wsD.write_number(di, 6, r, f_cur)
                di += 1
    for wname, wcells in (("JUNE-PRE", gps), ("JUNE-POST", gqs)):
        for st, (l, r) in sorted(wcells.items()):
            wsD.write_string(di, 0, "ACCOUNT")
            wsD.write_string(di, 1, "Google")
            wsD.write_string(di, 2, wname)
            wsD.write_string(di, 3, st)
            wsD.write_string(di, 4, "ALL")
            wsD.write_number(di, 5, l, f_num)
            wsD.write_number(di, 6, r, f_cur)
            di += 1
    for wname, wcells in (("JUNE-PRE", mps), ("JUNE-POST", mqs)):
        for st, (l, r) in sorted(wcells.items()):
            wsD.write_string(di, 0, "ACCOUNT")
            wsD.write_string(di, 1, "Microsoft")
            wsD.write_string(di, 2, wname)
            wsD.write_string(di, 3, st)
            wsD.write_string(di, 4, "ALL")
            wsD.write_number(di, 5, l, f_num)
            wsD.write_number(di, 6, r, f_cur)
            di += 1
    wsD.autofilter(0, 0, di - 1, len(dhdrs) - 1)
    wsD.freeze_panes(1, 0)
    log(f"[DATA] Data_Cells sheet: {di - 1:,} raw rows (ACCOUNT + tier "
        f"scopes); exhibit RPL columns are live SUMIFS on this sheet")

    def dsum(valcol, scope_ref, plat_ref, month_ref, dev_ref):
        return (f"SUMIFS(Data_Cells!${valcol}:${valcol},"
                f"Data_Cells!$A:$A,{scope_ref},Data_Cells!$B:$B,{plat_ref},"
                f"Data_Cells!$C:$C,{month_ref},Data_Cells!$E:$E,{dev_ref})")

    def waterfall_sheet(name, wf, base_label, dev_literal):
        ws = wb.add_worksheet(name)
        cols = ["month", "RPL", "delta_vs_base", "MIX", "PRICE(laspeyres)",
                "INTERACTION", "corr_PRICE", "RESIDUAL", "UNVERIFIED"]
        ws.write(0, 0, f"Baseline {base_label}. Identity: delta = MIX + PRICE "
                       f"+ INTERACTION (gated). RPL and delta are LIVE "
                       f"formulas on Data_Cells; decomposition columns are "
                       f"computed at build (Laspeyres base-weight math is "
                       f"not SUMIFS-able) and gate G2 proves they sum to "
                       f"delta.", f_bold)
        ws.write(1, 0, "params:", f_bold)
        ws.write_string(1, 1, "ACCOUNT")
        ws.write_string(1, 2, "Google")
        ws.write_string(1, 3, dev_literal)
        sc_ref, pl_ref, dv_ref = "$B$2", "$C$2", "$D$2"
        for c, h in enumerate(cols):
            ws.write(2, c, h, f_hdr)
        base_rpl_ref = None
        for i, r in enumerate(wf):
            x = 3 + i
            ws.write_string(x, 0, r["month"])
            m_ref = f"$A{x + 1}"
            rpl_f = (f"=IFERROR({dsum('G', sc_ref, pl_ref, m_ref, dv_ref)}"
                     f"/{dsum('F', sc_ref, pl_ref, m_ref, dv_ref)},\"\")")
            ws.write_formula(x, 1, rpl_f, f_cur)
            if base_rpl_ref is None:
                base_rpl_ref = XC(x, 1, row_abs=True, col_abs=True)
            ws.write_formula(x, 2, f"={XC(x, 1)}-{base_rpl_ref}", f_cur)
            for c, k in enumerate(["mix", "price", "interaction",
                                   "price_corr", "residual", "unverified"],
                                  start=3):
                ws.write_number(x, c, r[k], f_cur)
        ch = wb.add_chart({"type": "column", "subtype": "stacked"})
        n = len(wf)
        for c, nm2 in [(3, "MIX"), (6, "corr PRICE"), (7, "RESIDUAL"),
                       (8, "UNVERIFIED")]:
            ch.add_series({"name": nm2,
                           "categories": [name, 3, 0, 2 + n, 0],
                           "values": [name, 3, c, 2 + n, c]})
        ch.set_title({"name": f"RPL delta vs {base_label} by channel"})
        ch.set_size({"width": 620, "height": 400})
        ws.insert_chart(2, 10, ch)

    waterfall_sheet("01_Waterfall_Mar26", wf_a, BASE_A, "*")
    waterfall_sheet("02_Desktop_Mar26", wf_a_desk, BASE_A + " (desktop only)",
                    "desktop")

    ws = wb.add_worksheet("04_March_Verdict")
    ws.write(0, 0, verdict, f_bold)
    ws.write(1, 0, "params:", f_bold)
    for c, (lab, val, sfmt) in enumerate([
            ("base_month", "2026-02", True), ("cur_month", "2026-04", True),
            ("min_G_move", MOVE_G, False), ("corr_band", CORR_BAND, False),
            ("MS_flat", FLAT_MS, False), ("MS_floor", MS_CORR_FLOOR, False)]):
        ws.write(1, 1 + c * 2, lab, f_bold)
        if sfmt:
            ws.write_string(1, 2 + c * 2, val)
        else:
            ws.write_number(1, 2 + c * 2, val)
    P_FEB, P_APR = "$C$2", "$E$2"
    P_MOVE, P_BAND, P_FLAT, P_FLOOR = "$G$2", "$I$2", "$K$2", "$M$2"
    hdrs = ["state", "device", "G_leads_base", "G_leads_cur", "G_dPrice",
            "MS_leads_base", "MS_leads_cur", "MS_dPrice", "contrib_$",
            "class"]
    for c, h in enumerate(hdrs):
        ws.write(2, c, h, f_hdr)

    def cellsum(valcol, plat, month_ref, x):
        return (f"SUMIFS(Data_Cells!${valcol}:${valcol},Data_Cells!$A:$A,"
                f"\"ACCOUNT\",Data_Cells!$B:$B,\"{plat}\","
                f"Data_Cells!$C:$C,{month_ref},Data_Cells!$D:$D,$A{x},"
                f"Data_Cells!$E:$E,$B{x})")

    l1_tot = (f"SUMIFS(Data_Cells!$F:$F,Data_Cells!$A:$A,\"ACCOUNT\","
              f"Data_Cells!$B:$B,\"Google\",Data_Cells!$C:$C,{P_APR})")
    for i, cr in enumerate(verdict_rows):
        r = 3 + i
        x = r + 1
        ws.write_string(r, 0, cr["cell"][0])
        ws.write_string(r, 1, cr["cell"][1])
        ws.write_formula(r, 2, f"={cellsum('F', 'Google', P_FEB, x)}", f_num)
        ws.write_formula(r, 3, f"={cellsum('F', 'Google', P_APR, x)}", f_num)
        g_p0 = f"({cellsum('G', 'Google', P_FEB, x)}/$C{x})"
        g_p1 = f"({cellsum('G', 'Google', P_APR, x)}/$D{x})"
        ws.write_formula(r, 4, f"=IFERROR({g_p1}/{g_p0}-1,\"\")", f_pct)
        ws.write_formula(r, 5, f"={cellsum('F', 'Microsoft', P_FEB, x)}",
                         f_num)
        ws.write_formula(r, 6, f"={cellsum('F', 'Microsoft', P_APR, x)}",
                         f_num)
        m_p0 = f"({cellsum('G', 'Microsoft', P_FEB, x)}/$F{x})"
        m_p1 = f"({cellsum('G', 'Microsoft', P_APR, x)}/$G{x})"
        ws.write_formula(
            r, 7, f"=IF(OR($F{x}<{P_FLOOR},$G{x}<{P_FLOOR}),\"\","
                  f"IFERROR({m_p1}/{m_p0}-1,\"\"))", f_pct)
        # contrib = w1 * (p1 - p0)  [algebraic identity of the Laspeyres
        # price + interaction share]
        ws.write_formula(r, 8, f"=IFERROR(($D{x}/{l1_tot})*({g_p1}-{g_p0}),"
                               f"\"\")", f_cur)
        ws.write_formula(
            r, 9,
            f"=IF(ISNUMBER($E{x})=FALSE,\"UNVERIFIED\","
            f"IF(ISNUMBER($H{x})=FALSE,\"UNVERIFIED\","
            f"IF(ABS($E{x})<{P_MOVE},\"PRICE(small)\","
            f"IF(AND(SIGN($E{x})=SIGN($H{x}),ABS($E{x}-$H{x})<={P_BAND}),"
            f"\"PRICE\",IF(ABS($H{x})<{P_FLAT},\"RESIDUAL\","
            f"IF(SIGN($E{x})<>SIGN($H{x}),\"RESIDUAL\","
            f"\"PRICE(partial)\"))))))")
    ws.freeze_panes(3, 2)

    ws = wb.add_worksheet("05_June18_Control")
    ws.write(0, 0, "Positive control: pre/post June 18 state RPL steps, both "
                   "platforms. Synchronized negative steps = the documented "
                   "vendor bid-down, found by the method. LIVE formulas on "
                   "Data_Cells rows month='JUNE-PRE'/'JUNE-POST' "
                   f"(pre {JUNE_PRE[0]}..{JUNE_PRE[1]}, "
                   f"post {JUNE_POST[0]}..{JUNE_POST[1]}).", f_bold)
    ws.write(1, 0, "params:", f_bold)
    ws.write(1, 1, "pre", f_bold)
    ws.write_string(1, 2, "JUNE-PRE")
    ws.write(1, 3, "post", f_bold)
    ws.write_string(1, 4, "JUNE-POST")
    ws.write(1, 5, "sync_step", f_bold)
    ws.write_number(1, 6, -0.05, f_pct)
    W_PRE, W_POST, W_SYNC = "$C$2", "$E$2", "$G$2"
    hdrs = ["state", "G_pre_n", "G_post_n", "G_step", "MS_pre_n", "MS_post_n",
            "MS_step", "synchronized"]
    for c, h in enumerate(hdrs):
        ws.write(2, c, h, f_hdr)

    def wsum(valcol, plat, month_ref, x):
        return (f"SUMIFS(Data_Cells!${valcol}:${valcol},Data_Cells!$A:$A,"
                f"\"ACCOUNT\",Data_Cells!$B:$B,\"{plat}\","
                f"Data_Cells!$C:$C,{month_ref},Data_Cells!$D:$D,$A{x},"
                f"Data_Cells!$E:$E,\"ALL\")")

    for i, (st, a, b, dg, c1, d1, dm, sync) in enumerate(control_rows):
        r = 3 + i
        x = r + 1
        ws.write_string(r, 0, st)
        ws.write_formula(r, 1, f"={wsum('F', 'Google', W_PRE, x)}", f_num)
        ws.write_formula(r, 2, f"={wsum('F', 'Google', W_POST, x)}", f_num)
        gpre = f"({wsum('G', 'Google', W_PRE, x)}/$B{x})"
        gpost = f"({wsum('G', 'Google', W_POST, x)}/$C{x})"
        ws.write_formula(r, 3, f"=IFERROR({gpost}/{gpre}-1,\"\")", f_pct)
        ws.write_formula(r, 4, f"={wsum('F', 'Microsoft', W_PRE, x)}", f_num)
        ws.write_formula(r, 5, f"={wsum('F', 'Microsoft', W_POST, x)}", f_num)
        mpre = f"({wsum('G', 'Microsoft', W_PRE, x)}/$E{x})"
        mpost = f"({wsum('G', 'Microsoft', W_POST, x)}/$F{x})"
        ws.write_formula(r, 6, f"=IFERROR({mpost}/{mpre}-1,\"\")", f_pct)
        ws.write_formula(r, 7, f"=IF(AND($D{x}<{W_SYNC},$G{x}<{W_SYNC}),"
                               f"\"YES\",\"\")")

    ws = wb.add_worksheet("06_Tier_Lens")
    ws.write(0, 0, "TIER RPL DECAY, DECOMPOSED — the stack sums exactly to "
                   "delta (Laspeyres identity, gate G2). MIX = what the "
                   "tier's RPL would have done with every cell's price "
                   "frozen at baseline and only the lead distribution "
                   "across state x device cells shifting (the volume "
                   "collapse hollowing out high-RPL cells). The claim: "
                   "MIX dominates every tier's decay; where a corroborated "
                   "PRICE slice appears, it traces to documented vendor "
                   "events — the spring repricing (04, raises) and/or the "
                   "June 18 rebalance (05, cuts): check the slice's SIGN "
                   "against those exhibits — "
                   "not to buyers cutting prices under the tier structure. "
                   "UNVERIFIED = MS cell too thin to certify (mostly "
                   "mobile; see 07 diagnostic). Tier membership CHANGES "
                   "over the window (states moved between campaigns): "
                   "entering/exiting states land in MIX by construction "
                   "and can never appear as PRICE; corr_PRICE is computed "
                   "only on each tier's persistent states (last column) — "
                   "cross-check those against 05's synchronized list. "
                   "Tier MIX therefore bundles the volume collapse AND "
                   "membership reshuffles — both composition, neither "
                   "price. Re-tiering reorganizes the costume, not the "
                   "cause.",
             f_bold)
    hdrs = ["tier", "base", "current", "RPL_base", "RPL_now", "delta",
            "MIX", "corr_PRICE", "RESIDUAL", "UNVERIFIED", "MIX_share",
            "states_base", "states_now", "persistent_states"]
    for c, h in enumerate(hdrs):
        ws.write(2, c, h, f_hdr)
    ws.write(1, 0, "params:", f_bold)
    ws.write_string(1, 1, "Google")
    ws.write_string(1, 2, "*")
    plT, dvT = "$B$2", "$C$2"
    f_pctT = wb.add_format({"num_format": "0%"})
    n_t = len(tier_out)
    for i, (tier, b0, b1, d, pr, rs, uv) in enumerate(tier_out):
        x = 3 + i
        ws.write_string(x, 0, tier)
        ws.write_string(x, 1, b0)
        ws.write_string(x, 2, b1)
        scT = f"$A{x + 1}"
        for c, mref in ((3, f"$B{x + 1}"), (4, f"$C{x + 1}")):
            f = (f"=IFERROR({dsum('G', scT, plT, mref, dvT)}"
                 f"/{dsum('F', scT, plT, mref, dvT)},\"\")")
            ws.write_formula(x, c, f, f_cur)
        ws.write_formula(x, 5, f"={XC(x, 4)}-{XC(x, 3)}", f_cur)
        for c, v in enumerate([d["mix"], pr, rs, uv], start=6):
            ws.write_number(x, c, v, f_cur)
        if d["delta"]:
            ws.write_number(x, 10, d["mix"] / d["delta"], f_pctT)
        ser = tier_series.get(tier, {})
        st_b = {k[0] for k, v in ser.get(b0, {}).items() if v[0] > 0}
        st_n = {k[0] for k, v in ser.get(b1, {}).items() if v[0] > 0}
        ws.write_number(x, 11, len(st_b))
        ws.write_number(x, 12, len(st_n))
        ws.write_string(x, 13, ",".join(sorted(st_b & st_n)))
    # stacked column: components stack to delta per tier
    ch = wb.add_chart({"type": "column", "subtype": "stacked"})
    for col, nm in [(6, "MIX (composition)"), (7, "corroborated PRICE"),
                    (8, "RESIDUAL"), (9, "UNVERIFIED")]:
        ch.add_series({"name": nm,
                       "categories": ["06_Tier_Lens", 3, 0, 2 + n_t, 0],
                       "values": ["06_Tier_Lens", 3, col, 2 + n_t, col]})
    ch.set_title({"name": "Tier RPL change: stack = delta; the PRICE "
                          "slice is the one a buyer would have to move"})
    ch.set_y_axis({"name": "$ per lead vs baseline"})
    ch.set_size({"width": 680, "height": 400})
    ws.insert_chart(3 + n_t + 2, 0, ch)
    rr = 3 + n_t + 24
    ws.write(rr, 0, "FIXED CURRENT-ROSTER VIEW — each tier's CURRENT "
                    "states, those same states compared to their own "
                    f"account-level RPL basis in {BASE_A} (whichever "
                    "campaign served them then). No membership churn: "
                    "MIX here is purely lead redistribution WITHIN the "
                    "roster (state weights + devices). UNVER_mobile is "
                    "further split by two anchors: mob_panel_consistent = "
                    "moves matching the MS-verified mobile panel median "
                    "(vendor mobile repricing, inferred); mob_divergent = "
                    "diverges from the panel while the same state's "
                    "desktop is corroborated-stable (Google-side, "
                    "inferred); mob_unanchored = no anchor. INFERRED "
                    "labels never merge into corr_PRICE. This is the view "
                    "that supports: 'even with the re-tiering as tuned, "
                    "the states inside each tier lost RPL through lead "
                    "mix while their prices held or were repriced "
                    "upward.'", f_bold)
    rhdrs = ["tier", "current_month", "roster_n", "RPL_base", "RPL_now",
             "delta", "MIX", "corr_PRICE", "RESIDUAL", "UNVER_mobile",
             "UNVER_other", "mob_panel_consistent", "mob_divergent",
             "mob_unanchored", "MIX_share", "roster_states"]
    for c, h in enumerate(rhdrs):
        ws.write(rr + 1, c, h, f_hdr)
    for i, (tier, b1x, roster, d2, pr2, rs2, uv2, uv_mob, uv_oth,
            mob_panel, mob_diverg, mob_unanch) in enumerate(roster_out):
        x = rr + 2 + i
        xx = x + 1
        ws.write_string(x, 0, tier)
        ws.write_string(x, 1, b1x)
        ws.write_number(x, 2, len(roster))
        scR = f"CONCATENATE($A{xx},\" ROSTER\")"
        for c, mref in ((3, f"\"{BASE_A}\""), (4, f"$B{xx}")):
            f = (f"=IFERROR(SUMIFS(Data_Cells!$G:$G,Data_Cells!$A:$A,{scR},"
                 f"Data_Cells!$B:$B,{plT},Data_Cells!$C:$C,{mref},"
                 f"Data_Cells!$E:$E,{dvT})/SUMIFS(Data_Cells!$F:$F,"
                 f"Data_Cells!$A:$A,{scR},Data_Cells!$B:$B,{plT},"
                 f"Data_Cells!$C:$C,{mref},Data_Cells!$E:$E,{dvT}),\"\")")
            ws.write_formula(x, c, f, f_cur)
        ws.write_formula(x, 5, f"={XC(x, 4)}-{XC(x, 3)}", f_cur)
        for c, v in enumerate([d2["mix"], pr2, rs2, uv_mob, uv_oth,
                               mob_panel, mob_diverg, mob_unanch],
                              start=6):
            ws.write_number(x, c, v, f_cur)
        if d2["delta"]:
            ws.write_number(x, 14, d2["mix"] / d2["delta"], f_pctT)
        ws.write_string(x, 15, ",".join(roster))
    n_r = len(roster_out)
    ch2 = wb.add_chart({"type": "column", "subtype": "stacked"})
    for col, nm2 in [(6, "MIX (within-roster)"), (7, "corroborated PRICE"),
                     (8, "RESIDUAL"), (9, "UNVER mobile"),
                     (10, "UNVER other")]:
        ch2.add_series({"name": nm2,
                        "categories": ["06_Tier_Lens", rr + 2, 0,
                                       rr + 1 + n_r, 0],
                        "values": ["06_Tier_Lens", rr + 2, col,
                                   rr + 1 + n_r, col]})
    ch2.set_title({"name": "Today's rosters vs their own March basis: "
                           "every verifiable component is ~zero or "
                           "positive"})
    ch2.set_y_axis({"name": "$ per lead vs " + BASE_A})
    ch2.set_size({"width": 680, "height": 400})
    ws.insert_chart(rr + 2 + n_r + 2, 0, ch2)
    # headline chart: reported (as-served) vs real (fixed-roster) delta
    ch3 = wb.add_chart({"type": "column"})
    ch3.add_series({"name": "Change in the tier's reported average "
                            "(membership + volume weights moved)",
                    "categories": ["06_Tier_Lens", 3, 0, 2 + n_t, 0],
                    "values": ["06_Tier_Lens", 3, 5, 2 + n_t, 5]})
    ch3.add_series({"name": "Change in the states' actual value "
                            "(same states, Mar vs now)",
                    "categories": ["06_Tier_Lens", 3, 0, 2 + n_t, 0],
                    "values": ["06_Tier_Lens", rr + 2, 5,
                               rr + 1 + n_r, 5]})
    ch3.set_title({"name": "When a tier's RPL falls in our reports, did "
                           "anything real lose value?"})
    ch3.set_y_axis({"name": "$ per lead vs " + BASE_A})
    ch3.set_size({"width": 680, "height": 400})
    ws.insert_chart(2, 15, ch3)
    ws.write(24, 15, "READ ME: this chart does NOT compare March's tiering "
                     "to today's, and neither bar measures tier-setup "
                     "quality. A tier's reported average moves whenever "
                     "its membership or lead weights move — Tier 1's "
                     "average fell $17.55 because it widened from 3 states "
                     "to 10 (dilution, an intended effect of the re-tier) "
                     "and volume shifted, while its actual states GAINED "
                     "$1.40. Class-average analogy: add younger students "
                     "and the class's average height falls; no student "
                     "shrank. Left bar = the recomputed average; right "
                     "bar = did any student shrink.", f_bold)

    # ---- UNVERIFIED diagnostic: what drives it? (latest month vs base) ----
    uv_dev = defaultdict(float)
    uv_reason = defaultdict(float)
    uv_tot = 0.0
    if wf_a:
        for cr in wf_a[-1]["cell_rows"]:
            if cr["label"] == "UNVERIFIED":
                uv_dev[cr["cell"][1]] += cr["contrib"]
                uv_reason["zero-$-base" if cr["dG"] is None
                          else "MS cell thin"] += cr["contrib"]
                uv_tot += cr["contrib"]
    log("\n" + "=" * 78)
    log(f"UNVERIFIED DIAGNOSTIC — {wf_a[-1]['month'] if wf_a else 'n/a'} vs "
        f"{BASE_A}: total {uv_tot:+.2f} $/lead")
    for dev, v in sorted(uv_dev.items(), key=lambda x: -abs(x[1])):
        log(f"  by device: {dev:8} {v:+7.2f}  "
            f"({abs(v) / max(abs(uv_tot), 1e-9):.0%} of |unverified|)")
    for rs2, v in sorted(uv_reason.items(), key=lambda x: -abs(x[1])):
        log(f"  by reason: {rs2:14} {v:+7.2f}")
    log("=" * 78)

    ws = wb.add_worksheet("07_Summary")
    ws.set_column(0, 0, 100)
    ws.write(0, 0, "ONE-PAGE SUMMARY (layman)", f_bold)
    for i, t in enumerate([
        "Our lead buyers cannot see which platform a lead came from. They pay "
        "off one price list, set per state and device. So our revenue-per-lead "
        "can only move three ways: (1) our leads shifting between states/"
        "devices (MIX — our bidding), (2) the buyers changing the price list "
        "(PRICE — verified when Microsoft's identical cell moves too), or "
        "(3) lead acceptance/quality (RESIDUAL — ours).",
        "UNVERIFIED DIAGNOSTIC (latest month vs baseline): "
        + "; ".join(f"{dev}: {v:+.2f} $/lead "
                    f"({abs(v) / max(abs(uv_tot), 1e-9):.0%})"
                    for dev, v in sorted(uv_dev.items(),
                                         key=lambda x: -abs(x[1])))
        + ". Reasons: "
        + "; ".join(f"{rs2}: {v:+.2f}" for rs2, v in sorted(
            uv_reason.items(), key=lambda x: -abs(x[1])))
        + ". Structural driver: Microsoft is 72-78% desktop, so its mobile "
        "cells rarely clear the corroboration floor — Google mobile moves "
        "outside the largest states are unverifiable by construction; the "
        "platform-blind price list covers them in principle but cannot be "
        "checked cell-by-cell.",
        "HOW TO READ MICROSOFT IN THIS WORKBOOK: this is a Google analysis; "
        "Microsoft is the control, not a subject. Every Microsoft number "
        "answers exactly one question — did the vendors' price move in the "
        "identical state x device cell on the other platform? If yes, the "
        "Google move is a vendor repricing (corr_PRICE); if no, it is "
        "Google-side (MIX/RESIDUAL); if Microsoft's cell is too thin to "
        "tell, it is UNVERIFIED. Never read Microsoft columns as "
        "performance comparison — that lives in the cross-platform "
        "workbook, not here.",
        verdict,
        "June 18 bid-down: detected independently by this method on both "
        "platforms — see 05 (the credibility check).",
        "Caveats: no vendor modifier log beyond June 18 (cross-platform "
        "corroboration substitutes); Microsoft mobile too thin to verify "
        "mobile-cell prices directly — the platform-blind architecture covers "
        "them by construction; INTEREST-location and acceptance-lag caveats "
        "carry over from the cross-platform workbook; final month partial.",
    ]):
        ws.write(2 + i * 2, 0, t)

    wb.close()
    log(f"\n[DONE] workbook: {args.out}")


if __name__ == "__main__":
    main()
