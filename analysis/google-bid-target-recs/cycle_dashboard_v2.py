"""
Cycle Dashboard v2 — ACCOUNT-grain cycle, floor bands, prediction
ledger — Google Home
═══════════════════════════════════════════════════════════════════════
Blend of cycle_dashboard.py (the cycle engine, chips, metric panels,
spiral/QS honesty guard, phase table, cycle_state CSV — inherited
verbatim where possible) and wave_calibration.py (pinned campaign
universe, event rail, structural-change discipline), plus four new
modules:

  GRAIN INVERSION   ACCOUNT is the primary cycle object. The account
      moves as one wave (measured: core campaigns phase-locked,
      perturbed ones ride the same wave shifted), and account-grain
      series are composition-invariant — they survive restructures
      with history intact. Campaigns are MEMBERS: phase + T+n +
      sync (corr of campaign oscillator vs account oscillator,
      trailing 60d) + PERTURBED tags; member counters are allowed
      to reset, the account counter is not.
  PHASE OVERLAY     each completed account cycle (trough→trough)
      drawn on a normalized 0..1 phase axis — leads/day scaled to
      its own cycle crest, CPC scaled to its cycle mean — with the
      current open cycle on top. This is where "does this cycle
      rhyme" is read.
  LEADS FLOOR BAND  projected trough floor for leads/day: each
      completed cycle's floor/crest ratio × the current cycle's
      crest-so-far. A BAND from observed ratios, never a point.
  PREDICTION LEDGER prediction_ledger.csv persists across runs.
      When the open account cycle is old enough to have a crest
      estimate, the run stamps ONE frozen prediction per kind
      (TROUGH_WINDOW, LEADS_FLOOR) anchored to the last trough;
      later runs never edit it. When the next trough prints, the
      run grades it (EARLY/IN_WINDOW/LATE; BELOW/IN/ABOVE band).
      Three cycles from now this is a track record, not a vibe.
  CPC TURN SIGNAL   CPC(7d) panel + per-cycle lead/lag diagnostic
      (CPC7 peak offset vs spend-oscillator peak; negative = CPC
      rolls over first). Tests the standing hypothesis: expansion
      lifts CPCs, contraction sheds them until ROAS recovers into
      the trough — if CPC consistently leads, it is a turn signal
      that fires before the oscillator.
  EVENT RAIL        dated structural events (regime moves, target
      batches, cutovers, mitigations, the coming restructure) drawn
      on the cycle panel so causes are stamped the day they happen,
      not reconstructed later.

RESTRUCTURE HANDLING (config below): when RESTRUCTURE_DATE is set,
it joins the event rail, member campaigns get PERTURBED tags for
PERTURB_DAYS, and new campaign ids are added to the account sum via
ADDED_CAMPAIGNS with their effective dates. The ACCOUNT series and
its trough history run straight through — that continuity is the
point of the grain inversion.

Conventions (house): DB read-only, never during a sync;
campaign_performance_daily_search device='ALL' JOIN campaigns
account 1; revenue/conversions/clicks columns auto-detected; leads =
platform conversions (NOT Looker — zip-submit definition changed
10/2); campaign universe pinned to the 7 charter ids
(--all-campaigns to override); targets = latest TARGET_ROAS event;
data treated complete (no lag exclusion — ruling 8/20); rolling
ROAS/CPC = ratio of sums; projections are WINDOWS/BANDS, never
points (~3 observed cycles).

OUTPUT (CSVs/txt before charts):
  cycle_state_<date>.csv (+ _latest)  — v1-compatible schema, plus
      CREST / FLOOR_RATIO / SYNC record types appended
  prediction_ledger.csv               — persistent, append+grade only
  cycle_phase_<date>.txt + latest.txt — console phase table
  cycle_v2_ACCOUNT_<date>.png         — chips, cycle+event rail,
      leads/ROAS/GP/CPC panels, spiral guard
  cycle_overlay_ACCOUNT_<date>.png    — phase-normalized overlays +
      floor band
  per-campaign PNGs via --campaign <substr> (v1-style)

USAGE:
  python cycle_dashboard_v2.py --smoke     # no charts; validation +
                                           # ledger actions printed
  python cycle_dashboard_v2.py
  python cycle_dashboard_v2.py --campaign TIER2 --window-days 180
"""

import argparse
import csv
import math
import shutil
import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.gridspec import GridSpec

DEFAULT_DB_PATH = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")
DEFAULT_OUT_DIR = Path(r"C:\Projects\google-ads-ops\analysis"
                       r"\profit-attribution-tracker\output"
                       r"\cycle-dashboard-v2")
ACCOUNT_ID = 1

# ── campaign universe (immutable ids — cardinal rule) ───────────────
PINNED_CAMPAIGNS = {
    "23385188622": "CA", "23394808297": "TX", "23394808300": "FL",
    "23428389081": "TIER4RPL", "23496334752": "TIER1RPL",
    "23530860942": "TIER2RPL", "23586637678": "TIER3RPL"}
# Post-restructure additions: id -> {"from": "YYYY-MM-DD", "label": x}
# The ACCOUNT sum includes an added id from its effective date.
ADDED_CAMPAIGNS = {}

# ── event rail (drawn on the cycle panel; dates are stamps, not
#    reconstructions) ────────────────────────────────────────────────
EVENT_RAIL = {
    "May target batch":          "2026-05-06",
    "partner bid-down":          "2026-06-18",
    "GPL T1 launch":             "2026-06-19",
    "GPL T2 launch":             "2026-06-30",
    "RPL k=4 cutover":           "2026-07-29",
    "T1 floor step":             "2026-08-12",
    "Google Smart Bidding upd.": "2026-08-17",
    "T2 budget raise":           "2026-09-29",
    "surge pauses":              "2026-10-01",
    "geo exclusions":            "2026-10-02",
}
# Set when the nationwide-Generic restructure executes; joins the
# rail and starts the PERTURB_DAYS clock for member tags.
RESTRUCTURE_DATE = None
RESTRUCTURE_LABEL = "RESTRUCTURE: nationwide Generic + geo tiers"
PERTURB_DAYS = 45
# Campaign-level perturbations (member tagged provisional for
# PERTURB_DAYS after): id -> [(date, label)]
PERTURBED_EVENTS = {
    "23496334752": [("2026-08-12", "floor step 115->~125")],
    "23530860942": [("2026-09-29", "budget raise")],
}
# Notes merged into predictions at stamp time, keyed by
# (entity, anchor-trough date).
PREDICTION_NOTES = {
    ("ACCOUNT", "2026-09-13"):
        "Gilbert expects a HIGHER floor than the ratio band (major "
        "lift this expansion); downside pressure named: surge pauses "
        "10/1 + geo exclusions 10/2 act into the contraction",
}

MIN_PRED_AGE_DAYS = 10        # open cycle must be this old to stamp
SYNC_LOOKBACK = 60            # member corr window vs account osc

REV_CANDIDATES = ["conversion_value", "conversions_value", "conv_value",
                  "primary_conv_value", "total_conv_value"]
CONV_CANDIDATES = ["conversions", "primary_conv_count", "conv_count",
                   "all_conversions"]
CLICK_CANDIDATES = ["clicks", "click_count"]
C_OSC, C_TROUGH = "#2a78d6", "#1a5aa8"
C_EVENT = "#7b52ab"
QS_COLORS = {"QS ≤ 4": "#d03b3b", "QS 5–6": "#c98500", "QS 7+": "#0ca30c"}
LEDGER_COLS = ["pred_id", "entity", "kind", "anchor_trough", "made_on",
               "window_start", "window_end", "band_lo", "band_hi",
               "central", "basis", "status", "actual_date",
               "actual_value", "verdict", "graded_on", "note"]


# ── inherited plumbing (cycle_dashboard.py, verbatim or near) ───────
def connect_ro(p):
    conn = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def pick_col(conn, table, cands):
    try:
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return None
    return next((c for c in cands if c in cols), None)


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


def fetch_campaigns(conn, start, end, header):
    tbl = "campaign_performance_daily_search"
    rev = pick_col(conn, tbl, REV_CANDIDATES)
    cnv = pick_col(conn, tbl, CONV_CANDIDATES)
    clk = pick_col(conn, tbl, CLICK_CANDIDATES)
    header.append(f"resolved columns: revenue={rev or 'NONE'}, "
                  f"conversions={cnv or 'NONE'}, clicks={clk or 'NONE'}")
    rev_e = f"SUM(p.{rev})" if rev else "NULL"
    cnv_e = f"SUM(p.{cnv})" if cnv else "NULL"
    clk_e = f"SUM(p.{clk})" if clk else "NULL"
    data, names = {}, {}
    sql = f"""SELECT p.campaign_id cid, c.campaign_name nm, p.date d,
                     SUM(p.cost_micros)/1e6 cost, {rev_e} rev,
                     {cnv_e} conv, {clk_e} clk
              FROM {tbl} p
              JOIN campaigns c ON c.campaign_id=p.campaign_id
              WHERE c.account_id=? AND p.device='ALL'
                AND p.date BETWEEN ? AND ? GROUP BY 1,2,3"""
    for r in conn.execute(sql, [ACCOUNT_ID, start.isoformat(),
                                end.isoformat()]):
        cid = str(r["cid"])
        names[cid] = short_campaign(r["nm"])
        data.setdefault(cid, {})[str(r["d"])[:10]] = (
            r["cost"] or 0.0, r["rev"] or 0.0, r["conv"] or 0.0,
            r["clk"] or 0.0)
    return data, names, clk is not None


def fetch_targets(conn, cids):
    out = {}
    if not cids:
        return out
    ph = ",".join("?" for _ in cids)
    try:
        for r in conn.execute(
            f"""SELECT campaign_id, new_value FROM campaign_change_events
                WHERE platform='google' AND account_id=?
                  AND change_type='TARGET_ROAS' AND campaign_id IN ({ph})
                ORDER BY change_date""", [ACCOUNT_ID, *cids]):
            v = normalize_target(r["new_value"])
            if v:
                out[str(r["campaign_id"])] = v
    except sqlite3.Error:
        pass
    return out


def kw_campaign_link(conn):
    ki = {r[1] for r in conn.execute("PRAGMA table_info(keyword_instances)")}
    if "campaign_id" in ki:
        return ("", "ki.campaign_id")
    if "ad_group_id" in ki:
        try:
            ag = {r[1] for r in conn.execute("PRAGMA table_info(ad_groups)")}
            key = "id" if "id" in ag else ("ad_group_id"
                                           if "ad_group_id" in ag else None)
            if key and "campaign_id" in ag:
                return (f"JOIN ad_groups ag ON ag.{key}=ki.ad_group_id",
                        "ag.campaign_id")
        except sqlite3.Error:
            pass
    return None


def fetch_qs_monthly(conn, cid, year_start):
    """Monthly spend by QS bucket + rated/total spend. cid=None ->
    account-wide. Inherited from cycle_dashboard.py."""
    if cid is None:
        try:
            sql = """
              SELECT SUBSTR(p.report_date,1,7) ym,
                     CASE WHEN q.historical_quality_score IS NULL
                          THEN 'unrated'
                          WHEN q.historical_quality_score<=4 THEN 'low'
                          WHEN q.historical_quality_score<=6 THEN 'mid'
                          ELSE 'high' END bucket,
                     SUM(p.cost_micros)/1e6 spend
              FROM keyword_performance_daily p
              JOIN keyword_instances ki
                ON ki.id=p.instance_id AND ki.account_id=?
              LEFT JOIN keyword_quality_daily q
                ON q.instance_id=p.instance_id
               AND q.report_date=p.report_date
              WHERE p.device='ALL' AND p.report_date>=?
              GROUP BY 1,2"""
            out = {}
            for r in conn.execute(sql, [ACCOUNT_ID, year_start]):
                out.setdefault(r["ym"], {})[r["bucket"]] = r["spend"] or 0.0
            return out or None
        except sqlite3.Error as e:
            print(f"  WARN: account QS panel unavailable: {e}")
            return None
    link = kw_campaign_link(conn)
    if link is None:
        print("  WARN: QS panel skipped — no keyword→campaign linkage")
        return None
    join_sql, camp_expr = link
    try:
        sql = f"""
          SELECT SUBSTR(p.report_date,1,7) ym,
                 CASE WHEN q.historical_quality_score IS NULL THEN 'unrated'
                      WHEN q.historical_quality_score<=4 THEN 'low'
                      WHEN q.historical_quality_score<=6 THEN 'mid'
                      ELSE 'high' END bucket,
                 SUM(p.cost_micros)/1e6 spend
          FROM keyword_performance_daily p
          JOIN keyword_instances ki
            ON ki.id=p.instance_id AND ki.account_id=?
          {join_sql}
          LEFT JOIN keyword_quality_daily q
            ON q.instance_id=p.instance_id
           AND q.report_date=p.report_date
          WHERE CAST({camp_expr} AS TEXT)=CAST(? AS TEXT)
            AND p.device='ALL' AND p.report_date>=?
          GROUP BY 1,2"""
        out = {}
        for r in conn.execute(sql, [ACCOUNT_ID, cid, year_start]):
            out.setdefault(r["ym"], {})[r["bucket"]] = r["spend"] or 0.0
        if not out:
            print(f"  WARN: QS panel empty for campaign {cid}")
            return None
        return out
    except sqlite3.Error as e:
        print(f"  WARN: QS panel unavailable for {cid}: {e}")
        return None


def ma(series_vals, i, n):
    lo = max(0, i - n + 1)
    seg = series_vals[lo:i + 1]
    return sum(seg) / len(seg)


def build_series(vals, dates):
    """Inherited, extended: + cpc7 (ratio of sums over 7d), clicks."""
    spend = [vals.get(d.isoformat(), (0, 0, 0, 0))[0] for d in dates]
    rev = [vals.get(d.isoformat(), (0, 0, 0, 0))[1] for d in dates]
    conv = [vals.get(d.isoformat(), (0, 0, 0, 0))[2] for d in dates]
    clk = [vals.get(d.isoformat(), (0, 0, 0, 0))[3] for d in dates]
    s7 = [ma(spend, i, 7) for i in range(len(spend))]
    s30 = [ma(spend, i, 30) for i in range(len(spend))]
    osc = [(a / b - 1) if b > 0 else 0.0 for a, b in zip(s7, s30)]
    roas7, cpc7 = [], []
    for i in range(len(dates)):
        lo = max(0, i - 6)
        c = sum(spend[lo:i + 1])
        v = sum(rev[lo:i + 1])
        k = sum(clk[lo:i + 1])
        roas7.append(v / c if c > 0 else None)
        cpc7.append(c / k if k > 0 else None)
    gp7 = [ma([r - c for r, c in zip(rev, spend)], i, 7)
           for i in range(len(spend))]
    conv7 = [ma(conv, i, 7) for i in range(len(conv))]
    return spend, osc, roas7, gp7, conv7, cpc7


def find_troughs(osc, warmup=30, min_sep=14, depth=-0.05):
    troughs = []
    for i in range(warmup, len(osc) - 3):
        lo = max(warmup, i - 7)
        hi = min(len(osc), i + 8)
        if osc[i] <= min(osc[lo:hi]) and osc[i] < depth:
            if not troughs or i - troughs[-1] >= min_sep:
                troughs.append(i)
            elif osc[i] < osc[troughs[-1]]:
                troughs[-1] = i
    return troughs


def windows_from_osc(dates, osc_daily, troughs, thresh, bridge=2):
    out = []
    if not troughs:
        return out
    bounds = list(zip(troughs, troughs[1:] + [None]))
    n = len(dates)

    def extend(idx, step, lo, hi):
        cur = idx
        while True:
            probe, gap, found = cur + step, 0, None
            while lo <= probe <= hi and gap <= bridge:
                if osc_daily[probe] > thresh:
                    found = probe
                    break
                gap += 1
                probe += step
            if found is None:
                return cur
            cur = found

    for t0, t1 in bounds:
        lo = t0 + 1
        hi = (t1 - 1) if t1 is not None else n - 1
        if lo > hi:
            continue
        peak = max(range(lo, hi + 1), key=lambda i: osc_daily[i])
        if osc_daily[peak] <= thresh:
            continue
        ws = extend(peak, -1, lo, hi)
        we = extend(peak, +1, lo, hi)
        out.append((dates[ws], dates[we], dates[peak],
                    osc_daily[peak], t1 is None and we >= n - 3))
    return out


def phase_of(osc, troughs):
    now, prev = osc[-1], osc[-8] if len(osc) >= 8 else osc[0]
    rising = now > prev
    if now > 0.10:
        return "EXPANSION" if rising else "CREST / ROLLING OVER"
    if now < -0.10:
        return "TROUGH / RECOVERY" if rising else "CONTRACTION"
    return "NEUTRAL (rising)" if rising else "NEUTRAL (falling)"


def momentum_tag(vals7, vals14, vals30):
    if None in (vals7, vals14, vals30):
        return "—"
    if vals7 >= vals14 >= vals30:
        return "ACCEL"
    if vals7 < vals14 and vals7 < vals30:
        return "DECEL"
    return "TURN+" if vals7 >= vals14 else "TURN-"


def pearson(a, b):
    n = len(a)
    if n < 10:
        return None
    mx, my = sum(a) / n, sum(b) / n
    ca = [x - mx for x in a]
    cb = [y - my for y in b]
    va = math.sqrt(sum(x * x for x in ca))
    vb = math.sqrt(sum(y * y for y in cb))
    if va == 0 or vb == 0:
        return None
    return sum(x * y for x, y in zip(ca, cb)) / (va * vb)


# ── new: cycle anatomy (completed cycles, ratios, crest timing) ─────
def cycle_anatomy(dates, osc, conv7, cpc7, troughs):
    """Per completed cycle (t0->t1): crest leads + offset, floor
    leads, floor/crest ratio, CPC-vs-osc peak lag (days; negative =
    CPC rolls over first). Plus the open cycle's crest-so-far."""
    cycles = []
    for t0, t1 in zip(troughs, troughs[1:]):
        seg = range(t0 + 1, t1 + 1)
        ci = max(seg, key=lambda i: conv7[i])
        crest, floor = conv7[ci], conv7[t1]
        osc_pk = max(range(t0 + 1, t1), key=lambda i: osc[i])
        cpc_lag = None
        cpc_seg = [(i, cpc7[i]) for i in range(t0 + 1, t1)
                   if cpc7[i] is not None]
        if len(cpc_seg) >= 5:
            cpc_pk = max(cpc_seg, key=lambda p: p[1])[0]
            cpc_lag = cpc_pk - osc_pk
        cycles.append({
            "t0": t0, "t1": t1, "start": dates[t0], "end": dates[t1],
            "len": t1 - t0, "crest_leads": crest,
            "crest_offset": ci - t0, "floor_leads": floor,
            "ratio": (floor / crest) if crest > 0 else None,
            "cpc_lag_d": cpc_lag})
    open_c = None
    if troughs:
        t0 = troughs[-1]
        if t0 < len(dates) - 1:
            seg = range(t0 + 1, len(dates))
            ci = max(seg, key=lambda i: conv7[i])
            open_c = {"t0": t0, "start": dates[t0],
                      "age": len(dates) - 1 - t0,
                      "crest_so_far": conv7[ci],
                      "crest_date": dates[ci],
                      "crest_offset": ci - t0}
    return cycles, open_c


def floor_projection(cycles, open_c):
    """Band = observed floor/crest ratios x current crest-so-far."""
    ratios = sorted(c["ratio"] for c in cycles if c["ratio"] is not None)
    if not ratios or not open_c or open_c["crest_so_far"] <= 0:
        return None
    crest = open_c["crest_so_far"]
    med = ratios[len(ratios) // 2]
    return {"central": med * crest, "lo": ratios[0] * crest,
            "hi": ratios[-1] * crest, "ratios": ratios,
            "crest": crest, "n": len(ratios)}


def trough_window(dates, troughs, periods):
    """Inherited projection logic (v1): window from median +/- observed
    spread, padded wide when n is small."""
    if not troughs or not periods:
        return None, None
    pad = 10 if len(periods) < 2 else 4
    lo = dates[troughs[-1]] + timedelta(days=max(min(periods) - (pad - 4),
                                                 7))
    hi = dates[troughs[-1]] + timedelta(days=max(periods) + pad)
    return lo, hi


# ── new: prediction ledger (append + grade; frozen once stamped) ────
def ledger_load(path):
    rows = []
    if path.exists():
        with open(path, encoding="utf-8", newline="") as f:
            rows = [dict(r) for r in csv.DictReader(f)]
    return rows


def ledger_save(path, rows):
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=LEDGER_COLS)
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in LEDGER_COLS})


def ledger_update(rows, entity, dates, troughs, periods, conv7,
                  cycles, open_c, stamp, phase, actions):
    """Stamp new frozen predictions for the open cycle; grade OPEN
    predictions whose anchor trough now has a successor.
    TROUGH_WINDOW stamps once the cycle is MIN_PRED_AGE old (it
    depends only on trough spacings). LEADS_FLOOR is gated on crest
    plausibility — stamped only when T+n has reached the historical
    median crest offset, or the phase has rolled over — because a
    floor band priced off a mid-rise crest-so-far is biased low."""
    key = {(r["entity"], r["anchor_trough"], r["kind"]) for r in rows}
    # 1) stamp
    if open_c and open_c["age"] >= MIN_PRED_AGE_DAYS and cycles:
        anchor = open_c["start"].isoformat()
        note = PREDICTION_NOTES.get((entity, anchor), "")
        lo_d, hi_d = trough_window(dates, troughs, periods)
        if lo_d and (entity, anchor, "TROUGH_WINDOW") not in key:
            rows.append({
                "pred_id": f"{entity}-{anchor}-TW", "entity": entity,
                "kind": "TROUGH_WINDOW", "anchor_trough": anchor,
                "made_on": stamp, "window_start": lo_d.isoformat(),
                "window_end": hi_d.isoformat(), "band_lo": "",
                "band_hi": "", "central": "",
                "basis": f"median±spread of {len(periods)} spacings "
                         f"({min(periods)}-{max(periods)}d)",
                "status": "OPEN", "note": note})
            actions.append(f"stamped TROUGH_WINDOW {entity} anchor "
                           f"{anchor}: {lo_d}..{hi_d}")
        offs = sorted(c["crest_offset"] for c in cycles)
        crest_gate = offs[len(offs) // 2]
        # TROUGH / RECOVERY is deliberately NOT in this set: the
        # oscillator sits there at the START of a new cycle too, and
        # a floor stamped at T+11 off a week-old crest is the exact
        # bias this gate exists to prevent.
        crest_in = (open_c["age"] >= crest_gate or phase in
                    ("CREST / ROLLING OVER", "CONTRACTION",
                     "NEUTRAL (falling)"))
        fp = floor_projection(cycles, open_c)
        if (fp and (entity, anchor, "LEADS_FLOOR") not in key
                and not crest_in):
            actions.append(
                f"LEADS_FLOOR deferred ({entity} T+{open_c['age']} < "
                f"crest gate T+{crest_gate} and still {phase}) — "
                "band priced off a mid-rise crest would be biased low")
        if (fp and crest_in
                and (entity, anchor, "LEADS_FLOOR") not in key):
            rows.append({
                "pred_id": f"{entity}-{anchor}-LF", "entity": entity,
                "kind": "LEADS_FLOOR", "anchor_trough": anchor,
                "made_on": stamp, "window_start": "", "window_end": "",
                "band_lo": f"{fp['lo']:.1f}",
                "band_hi": f"{fp['hi']:.1f}",
                "central": f"{fp['central']:.1f}",
                "basis": f"floor/crest ratios "
                         f"{'/'.join(f'{r:.2f}' for r in fp['ratios'])}"
                         f" x crest-so-far {fp['crest']:.1f} "
                         f"(leads7, {open_c['crest_date']})",
                "status": "OPEN", "note": note})
            actions.append(f"stamped LEADS_FLOOR {entity} anchor "
                           f"{anchor}: central {fp['central']:.0f}, "
                           f"band {fp['lo']:.0f}-{fp['hi']:.0f}")
    # 2) grade
    tr_dates = {dates[t].isoformat(): t for t in troughs}
    for r in rows:
        if r["entity"] != entity or r.get("status") != "OPEN":
            continue
        anchor = r["anchor_trough"]
        succ = [d for d in sorted(tr_dates) if d > anchor]
        if not succ:
            continue
        a_date = succ[0]
        t_idx = tr_dates[a_date]
        r["actual_date"] = a_date
        r["graded_on"] = stamp
        r["status"] = "GRADED"
        if r["kind"] == "TROUGH_WINDOW":
            if a_date < r["window_start"]:
                r["verdict"] = "EARLY"
            elif a_date > r["window_end"]:
                r["verdict"] = "LATE"
            else:
                r["verdict"] = "IN_WINDOW"
        else:  # LEADS_FLOOR
            actual = conv7[t_idx]
            r["actual_value"] = f"{actual:.1f}"
            lo, hi = float(r["band_lo"]), float(r["band_hi"])
            r["verdict"] = ("BELOW_BAND" if actual < lo else
                            ("ABOVE_BAND" if actual > hi else "IN_BAND"))
            fin = next((c for c in cycles
                        if c["end"].isoformat() == a_date), None)
            if fin:
                extra = (f"final cycle crest {fin['crest_leads']:.1f}, "
                         f"ratio {fin['ratio']:.2f}")
                r["note"] = (r.get("note", "") + "; " + extra
                             ).lstrip("; ")
        actions.append(f"graded {r['pred_id']}: {r['verdict']} "
                       f"(actual trough {a_date}"
                       + (f", leads7 {r['actual_value']}"
                          if r.get("actual_value") else "") + ")")
    return rows


# ── new: member perturbation + event helpers ────────────────────────
def perturbed_tag(cid, last):
    evs = list(PERTURBED_EVENTS.get(cid, []))
    if RESTRUCTURE_DATE:
        evs.append((RESTRUCTURE_DATE, "restructure"))
    hits = []
    for dstr, label in evs:
        d = datetime.strptime(dstr, "%Y-%m-%d").date()
        if 0 <= (last - d).days <= PERTURB_DAYS:
            hits.append(f"{label} {d.strftime('%m/%d')}")
    return ("PERTURBED(" + "; ".join(hits) + ")") if hits else ""


def rail_events(d0, d1):
    out = []
    for label, dstr in EVENT_RAIL.items():
        d = datetime.strptime(dstr, "%Y-%m-%d").date()
        if d0 <= d <= d1:
            out.append((d, label))
    if RESTRUCTURE_DATE:
        d = datetime.strptime(RESTRUCTURE_DATE, "%Y-%m-%d").date()
        if d0 <= d <= d1:
            out.append((d, RESTRUCTURE_LABEL))
    return sorted(out)


# ── charts ──────────────────────────────────────────────────────────
def render_main(name, dates, series, troughs, target, qs_monthly,
                fp, cycles, out_png):
    """v1 render inherited, extended: CPC panel, event rail, floor
    band marker on the leads panel."""
    spend, osc, roas7, gp7, conv7, cpc7 = series
    has_cpc = any(v is not None for v in cpc7)
    n_qs = 1 if qs_monthly else 0
    n_cpc = 1 if has_cpc else 0
    fig = plt.figure(figsize=(12, 12.5 + 1.25 * n_cpc + 1.6 * n_qs))
    gs = GridSpec(5 + n_cpc + n_qs, 1, figure=fig,
                  height_ratios=[0.65, 2.2, 1.25, 1.25, 1.25] +
                  ([1.25] * n_cpc) + ([1.9] if n_qs else []),
                  hspace=0.42)

    axc = fig.add_subplot(gs[0])
    axc.axis("off")
    last_t = troughs[-1] if troughs else None
    periods = [b - a for a, b in zip(troughs, troughs[1:])]
    med = sorted(periods)[len(periods) // 2] if periods else None
    tplus = (len(dates) - 1 - last_t) if last_t is not None else None
    lo_d, hi_d = trough_window(dates, troughs, periods)
    win = (f"{lo_d.strftime('%b %d')}–{hi_d.strftime('%b %d')}"
           if lo_d else "")
    cyc_label = (f"Median cycle: {med}d over {len(periods)} interval"
                 f"{'s' if len(periods) != 1 else ''} (range "
                 f"{min(periods)}–{max(periods)}d)"
                 if periods else "Cycle length: <2 troughs in window")
    if periods and len(periods) < 2:
        cyc_label = f"Cycle: {med}d (n=1 — treat as provisional)"
    chips = [f"Phase: {phase_of(osc, troughs)}",
             f"Day T+{tplus}" if tplus is not None else "Day —",
             f"Last trough: {dates[last_t].strftime('%b %d')}"
             if last_t is not None else "Last trough: —",
             cyc_label,
             f"Next trough window: {win} (±wide, n={len(periods)})"
             if win else ""]
    axc.text(0, 0.78, f"{name} — cycle & spiral v2 — {dates[-1]}",
             fontsize=13, fontweight="bold")
    axc.text(0, 0.30, "   ·   ".join(c for c in chips if c),
             fontsize=10, color="#444")
    if fp:
        axc.text(0, -0.12,
                 f"Leads floor projection: central "
                 f"~{fp['central']:.0f}/day, band {fp['lo']:.0f}–"
                 f"{fp['hi']:.0f} (ratios "
                 f"{'/'.join(f'{r:.2f}' for r in fp['ratios'])} × "
                 f"crest-so-far {fp['crest']:.0f}; n={fp['n']} — a "
                 f"band, not a point"
                 + ("; PRE-CREST provisional"
                    if fp.get("provisional") else "")
                 + ")", fontsize=10, color="#8a2d2d")

    ax1 = fig.add_subplot(gs[1])
    ax1.plot(dates, [o * 100 for o in osc], color=C_OSC, lw=1.8)
    ax1.fill_between(dates, 0, [o * 100 for o in osc], color=C_OSC,
                     alpha=0.08)
    ax1.axhline(0, color="#888", lw=0.8)
    for j, t in enumerate(troughs):
        ax1.axvline(dates[t], color=C_TROUGH, ls="--", lw=0.9,
                    alpha=0.6)
        lbl = f"▼ {dates[t].strftime('%b %d')}"
        if j > 0:
            lbl += f" (+{troughs[j] - troughs[j - 1]}d)"
        ax1.annotate(lbl, (dates[t], min(o * 100 for o in osc)),
                     fontsize=8.5, color=C_TROUGH,
                     xytext=(-16, -14), textcoords="offset points")
    if last_t is not None and periods:
        lo = dates[last_t] + timedelta(days=min(periods))
        hi = dates[last_t] + timedelta(days=max(periods) + 4)
        ax1.axvspan(max(lo, dates[0]),
                    min(hi, dates[-1] + timedelta(days=1)),
                    color="#898781", alpha=0.12)
    ymax = max(o * 100 for o in osc)
    for d, label in rail_events(dates[0], dates[-1]):
        ax1.axvline(d, color=C_EVENT, lw=1.0, alpha=0.55)
        ax1.text(d, ymax, label, rotation=90, fontsize=6.5,
                 va="top", ha="right", color=C_EVENT)
    ax1.set_ylabel("spend osc.\n7d ÷ 30d − 1 (%)", fontsize=9)
    ax1.grid(True, alpha=0.2)
    ax1.tick_params(labelsize=8)
    ax1.set_title("Cycle panel — ▼ troughs w/ day counts · shaded = "
                  "projected trough window · purple = event rail",
                  fontsize=9.5, loc="left", color="#666")

    panels = [("Leads / day (7d)", conv7, None),
              ("ROAS (7d) · dotted = target", roas7, target),
              ("GP / day ($, 7d)", gp7, None)]
    if has_cpc:
        panels.append(("CPC (7d) — hypothesis: rolls over before "
                       "spend at the crest", cpc7, None))
    for k, (title, ys, tgt) in enumerate(panels):
        ax = fig.add_subplot(gs[2 + k], sharex=ax1)
        ax.plot(dates, [y if y is not None else float("nan")
                        for y in ys], color="#555", lw=1.6)
        if tgt:
            ax.axhline(tgt, color="#333", ls=":", lw=1)
        if k == 0 and fp and lo_d:
            ax.axhspan(fp["lo"], fp["hi"], xmin=0.0, color="#8a2d2d",
                       alpha=0.10)
            ax.axhline(fp["central"], color="#8a2d2d", ls=":", lw=1.1)
        for t in troughs:
            ax.axvline(dates[t], color=C_TROUGH, ls="--", lw=0.9,
                       alpha=0.45)
        ax.set_title(title, fontsize=9.5, loc="left", color="#666")
        ax.grid(True, alpha=0.2)
        ax.tick_params(labelsize=8)

    if qs_monthly:
        axq = fig.add_subplot(gs[5 + n_cpc])
        yms = sorted(qs_monthly.keys())
        tot = [sum(qs_monthly[y].values()) or 1 for y in yms]
        low = [qs_monthly[y].get("low", 0) / t for y, t in zip(yms, tot)]
        mid = [qs_monthly[y].get("mid", 0) / t for y, t in zip(yms, tot)]
        high = [qs_monthly[y].get("high", 0) / t
                for y, t in zip(yms, tot)]
        unr = [qs_monthly[y].get("unrated", 0) / t
               for y, t in zip(yms, tot)]
        rated = [1 - u for u in unr]
        rl = [l / r if r > 0 else 0 for l, r in zip(low, rated)]
        rm = [m / r if r > 0 else 0 for m, r in zip(mid, rated)]
        rh = [h / r if r > 0 else 0 for h, r in zip(high, rated)]
        x = range(len(yms))
        axq.bar(x, rl, color=QS_COLORS["QS ≤ 4"], label="QS ≤ 4",
                width=0.7)
        axq.bar(x, rm, bottom=rl, color=QS_COLORS["QS 5–6"],
                label="QS 5–6", width=0.7)
        axq.bar(x, rh, bottom=[a + b for a, b in zip(rl, rm)],
                color=QS_COLORS["QS 7+"], label="QS 7+", width=0.7)
        axq.plot(x, rated, color="#333", ls="--", lw=1.5, marker="o",
                 ms=3, label="rated share of spend")
        axq.set_xticks(list(x))
        axq.set_xticklabels(yms, fontsize=8)
        axq.set_ylim(0, 1.05)
        axq.set_title("Spiral panel — spend share by QS bucket (within "
                      "RATED spend) · dashed = rated share (honesty "
                      "guard: falling rated share = shares partly "
                      "compositional)", fontsize=9.5, loc="left",
                      color="#666")
        axq.legend(fontsize=8, ncol=4, loc="upper right",
                   framealpha=0.85)
        axq.grid(True, axis="y", alpha=0.2)
        axq.tick_params(labelsize=8)

    fig.autofmt_xdate()
    for lab in ax1.get_xticklabels():
        lab.set_visible(True)
        lab.set_rotation(30)
        lab.set_horizontalalignment("right")
    ax1.tick_params(labelbottom=True)
    fig.savefig(out_png, dpi=140, bbox_inches="tight")
    plt.close(fig)


def render_overlay(name, dates, conv7, cpc7, troughs, cycles, open_c,
                   fp, out_png):
    """Phase-normalized cycle overlay: completed cycles 0..1,
    leads/crest (left) and CPC/cycle-mean (right); current open
    cycle bold, floor band shaded."""
    fig, (axl, axr) = plt.subplots(1, 2, figsize=(13, 5.2))
    fig.suptitle(f"{name} — phase-normalized cycles (x = trough→"
                 "trough; current cycle x = days ÷ median cycle)",
                 fontsize=12, fontweight="bold", x=0.07, ha="left")
    greys = ["#9db8d6", "#6f94c4", "#436fa8"]
    periods = [c["len"] for c in cycles]
    medp = sorted(periods)[len(periods) // 2] if periods else None
    for k, c in enumerate(cycles):
        xs = [(i - c["t0"]) / c["len"]
              for i in range(c["t0"], c["t1"] + 1)]
        yl = [conv7[i] / c["crest_leads"] if c["crest_leads"] > 0
              else None for i in range(c["t0"], c["t1"] + 1)]
        col = greys[k % len(greys)]
        axl.plot(xs, yl, color=col, lw=1.4,
                 label=f"{c['start'].strftime('%b%d')}→"
                       f"{c['end'].strftime('%b%d')} "
                       f"(floor {c['ratio']:.2f})")
        seg = [(i, cpc7[i]) for i in range(c["t0"], c["t1"] + 1)
               if cpc7[i] is not None]
        if len(seg) >= 5:
            mean_c = sum(v for _, v in seg) / len(seg)
            axr.plot([(i - c["t0"]) / c["len"] for i, _ in seg],
                     [v / mean_c for _, v in seg], color=col, lw=1.4,
                     label=(f"{c['start'].strftime('%b%d')} cycle"
                            + (f" (CPC lag {c['cpc_lag_d']:+d}d)"
                               if c["cpc_lag_d"] is not None else "")))
    if open_c and medp:
        t0 = open_c["t0"]
        xs = [(i - t0) / medp for i in range(t0, len(dates))]
        crest = open_c["crest_so_far"] or 1
        axl.plot(xs, [conv7[i] / crest for i in range(t0, len(dates))],
                 color="#c23b3b", lw=2.3,
                 label=f"CURRENT (T+{open_c['age']}, ÷ crest-so-far)")
        axl.plot([xs[-1]], [conv7[-1] / crest], marker="o", ms=7,
                 color="#c23b3b")
        seg = [(i, cpc7[i]) for i in range(t0, len(dates))
               if cpc7[i] is not None]
        if len(seg) >= 5:
            mean_c = sum(v for _, v in seg) / len(seg)
            axr.plot([(i - t0) / medp for i, _ in seg],
                     [v / mean_c for _, v in seg], color="#c23b3b",
                     lw=2.3, label="CURRENT")
    if fp and fp["crest"] > 0:
        axl.axhspan(fp["lo"] / fp["crest"], fp["hi"] / fp["crest"],
                    color="#8a2d2d", alpha=0.10,
                    label="projected floor band")
    axl.set_title("Leads/day (7d) ÷ cycle crest", fontsize=10,
                  loc="left", color="#666")
    axr.set_title("CPC (7d) ÷ cycle mean — negative lag = CPC rolls "
                  "over first", fontsize=10, loc="left", color="#666")
    for ax in (axl, axr):
        ax.set_xlabel("cycle phase (0 = trough, 1 = next trough)")
        ax.grid(True, alpha=0.25)
        ax.legend(fontsize=7.5, loc="lower left")
        ax.set_xlim(0, max(1.35, (open_c["age"] / medp + 0.05)
                           if (open_c and medp) else 1.35))
    fig.tight_layout(rect=[0, 0, 1, 0.93])
    fig.savefig(out_png, dpi=140, bbox_inches="tight")
    plt.close(fig)


# ── main ────────────────────────────────────────────────────────────
def main():
    p = argparse.ArgumentParser()
    p.add_argument("--db-path", default=str(DEFAULT_DB_PATH))
    p.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    p.add_argument("--window-days", type=int, default=180,
                   help="longer than v1's 120 so several completed "
                        "cycles feed the overlay/ratios")
    p.add_argument("--campaign", default=None,
                   help="substring: also render v1-style per-campaign "
                        "dashboards for matches")
    p.add_argument("--all-campaigns", action="store_true",
                   help="disable the 7-id charter pin")
    p.add_argument("--qs-from", default="2026-01-01")
    p.add_argument("--window-osc-thresh", type=float, default=0.10)
    p.add_argument("--smoke", action="store_true",
                   help="no charts; validation + ledger actions")
    args = p.parse_args()

    db = Path(args.db_path)
    if not db.exists():
        sys.exit(f"DB not found: {db}")
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    header = [f"cycle_dashboard_v2 run {datetime.now().isoformat()}",
              f"db={db}"]
    conn = connect_ro(db)
    row = conn.execute(
        """SELECT MAX(p.date) FROM campaign_performance_daily_search p
           JOIN campaigns c ON c.campaign_id=p.campaign_id
           WHERE c.account_id=?""", [ACCOUNT_ID]).fetchone()
    if not row or not row[0]:
        sys.exit("No campaign data found.")
    last = datetime.strptime(str(row[0])[:10], "%Y-%m-%d").date()
    start = last - timedelta(days=args.window_days + 30)
    data, names, has_clicks = fetch_campaigns(conn, start, last, header)
    if not has_clicks:
        header.append("WARN: no clicks column — CPC panel/diagnostic "
                      "skipped")
    if not args.all_campaigns:
        universe = dict(PINNED_CAMPAIGNS)
        for cid, cfg in ADDED_CAMPAIGNS.items():
            universe[cid] = cfg.get("label", cid)
        dropped = [c for c in data if c not in universe]
        data = {c: v for c, v in data.items() if c in universe}
        header.append(f"campaign universe PINNED to {len(data)} ids "
                      f"({len(dropped)} legacy/other dropped; "
                      "--all-campaigns to include)")
        for cid, lbl in PINNED_CAMPAIGNS.items():
            if cid not in data:
                header.append(f"WARN: pinned {lbl} ({cid}) has no rows")
    targets = fetch_targets(conn, list(data.keys()))
    dates = [last - timedelta(days=k)
             for k in range(args.window_days - 1, -1, -1)]
    stamp = last.isoformat()

    # ACCOUNT series: sum of universe, respecting ADDED from-dates
    acct = {}
    for cid, series_ in data.items():
        from_d = ADDED_CAMPAIGNS.get(cid, {}).get("from", "0000-00-00")
        for d, v in series_.items():
            if d < from_d:
                continue
            a = acct.get(d, (0.0, 0.0, 0.0, 0.0))
            acct[d] = tuple(x + y for x, y in zip(a, v))
    a_series = build_series(acct, dates)
    a_spend, a_osc, a_roas7, a_gp7, a_conv7, a_cpc7 = a_series
    a_troughs = find_troughs(a_osc)
    a_periods = [b - a for a, b in zip(a_troughs, a_troughs[1:])]
    cycles, open_c = cycle_anatomy(dates, a_osc, a_conv7, a_cpc7,
                                   a_troughs)
    fp = floor_projection(cycles, open_c)
    if fp and cycles and open_c:
        offs = sorted(c["crest_offset"] for c in cycles)
        gate = offs[len(offs) // 2]
        fp["provisional"] = (open_c["age"] < gate and
                             phase_of(a_osc, a_troughs) not in
                             ("CREST / ROLLING OVER", "CONTRACTION",
                              "NEUTRAL (falling)"))
    med = (sorted(a_periods)[len(a_periods) // 2] if a_periods
           else None)
    lo_d, hi_d = trough_window(dates, a_troughs, a_periods)

    # prediction ledger (stamp + grade) — before any chart
    ledger_path = out_dir / "prediction_ledger.csv"
    ledger = ledger_load(ledger_path)
    actions = []
    ledger = ledger_update(ledger, "ACCOUNT", dates, a_troughs,
                           a_periods, a_conv7, cycles, open_c, stamp,
                           phase_of(a_osc, a_troughs), actions)
    ledger_save(ledger_path, ledger)
    n_open = sum(1 for r in ledger if r.get("status") == "OPEN")
    n_graded = sum(1 for r in ledger if r.get("status") == "GRADED")

    # console
    lines = [f"CYCLE DASHBOARD v2 — ACCOUNT-grain — {stamp}",
             "(oscillator = 7d spend ÷ 30d avg − 1; projections are "
             "windows/bands, few observed cycles)", ""]
    tplus = ((len(dates) - 1 - a_troughs[-1]) if a_troughs else None)
    lines.append(
        f"ACCOUNT  {phase_of(a_osc, a_troughs):22s} "
        f"T+{tplus if tplus is not None else '—'}   "
        f"cycle {str(med) + 'd' if med else '—'} "
        f"({min(a_periods)}-{max(a_periods)}d, n={len(a_periods)})   "
        f"next trough {lo_d.strftime('%m/%d') if lo_d else '—'}-"
        f"{hi_d.strftime('%m/%d') if hi_d else '—'}"
        if a_periods else f"ACCOUNT  {phase_of(a_osc, a_troughs)}")
    if cycles:
        lines.append("  completed cycles: " + "; ".join(
            f"{c['start'].strftime('%m/%d')}→"
            f"{c['end'].strftime('%m/%d')} ({c['len']}d, crest T+"
            f"{c['crest_offset']}, floor/crest {c['ratio']:.2f}"
            + (f", CPC lag {c['cpc_lag_d']:+d}d"
               if c["cpc_lag_d"] is not None else "") + ")"
            for c in cycles))
    if open_c:
        lines.append(f"  current cycle: T+{open_c['age']}, "
                     f"crest-so-far leads7={open_c['crest_so_far']:.0f}"
                     f" on {open_c['crest_date']} "
                     f"(T+{open_c['crest_offset']})")
    if fp:
        lines.append(f"  LEADS FLOOR projection: central "
                     f"~{fp['central']:.0f}/day, band {fp['lo']:.0f}-"
                     f"{fp['hi']:.0f} (ratios "
                     f"{'/'.join(f'{r:.2f}' for r in fp['ratios'])}; "
                     "a band, not a point"
                     + ("; PRE-CREST — provisional, crest still "
                        "growing" if fp.get("provisional") else "")
                     + ")")
    cpc_lags = [c["cpc_lag_d"] for c in cycles
                if c["cpc_lag_d"] is not None]
    turn = (f"  turn signals: spend "
            f"{momentum_tag(ma(a_spend, len(a_spend)-1, 7), ma(a_spend, len(a_spend)-1, 14), ma(a_spend, len(a_spend)-1, 30))}"
            f" · ROAS7 {a_roas7[-1]:.2f}"
            + (f" · CPC7 {a_cpc7[-1]:.2f}" if a_cpc7[-1] else ""))
    if cpc_lags:
        lag_med = sorted(cpc_lags)[len(cpc_lags) // 2]
        turn += (f" · CPC-vs-osc peak lag median {lag_med:+d}d "
                 + ("(CPC LEADS the turn)" if lag_med < 0 else
                    "(CPC does not lead)"))
    lines.append(turn)
    lines.append(f"  LEDGER: {n_open} OPEN, {n_graded} graded "
                 f"({ledger_path.name})")
    for a in actions:
        lines.append(f"    ledger: {a}")
    lines.append("")
    lines.append(f"MEMBERS (sync = corr w/ ACCOUNT osc, trailing "
                 f"{SYNC_LOOKBACK}d; member counters may reset — the "
                 "account counter does not)")

    state_rows = [("ACCOUNT", [])]
    ent_rows = state_rows[0][1]
    ent_rows.append(["ACCOUNT", "PHASE", stamp, "",
                     phase_of(a_osc, a_troughs),
                     f"T+{tplus}" if tplus is not None else ""])
    for t in a_troughs:
        ent_rows.append(["ACCOUNT", "TROUGH", dates[t].isoformat(),
                         dates[t].isoformat(), f"{a_osc[t]:.4f}", ""])
    for c in cycles:
        ent_rows.append(["ACCOUNT", "CREST",
                         (dates[c["t0"] + c["crest_offset"]]
                          ).isoformat(), "",
                         f"{c['crest_leads']:.1f}",
                         f"leads7; cycle {c['start']}→{c['end']}"])
        ent_rows.append(["ACCOUNT", "FLOOR_RATIO", c["end"].isoformat(),
                         "", f"{c['ratio']:.4f}",
                         f"floor {c['floor_leads']:.1f} / crest "
                         f"{c['crest_leads']:.1f}"])
    if med:
        ent_rows.append(["ACCOUNT", "CYCLE_MEDIAN", "", "", str(med),
                         f"n={len(a_periods)}"])
    if lo_d:
        ent_rows.append(["ACCOUNT", "NEXT_WINDOW", lo_d.isoformat(),
                         hi_d.isoformat(), "", f"n={len(a_periods)}"])
    s30 = [ma(a_spend, i, 30) for i in range(len(a_spend))]
    osc_daily = [(s / b - 1) if b > 0 else 0.0
                 for s, b in zip(a_spend, s30)]
    for ws_d, we_d, pk_d, pk_o, open_f in windows_from_osc(
            dates, osc_daily, a_troughs, args.window_osc_thresh):
        ent_rows.append(["ACCOUNT", "WINDOW", ws_d.isoformat(),
                         we_d.isoformat(), f"{pk_o:.4f}",
                         ("OPEN; " if open_f else "")
                         + f"peak={pk_d.isoformat()}; daily-grain"])

    # members
    member_series = {}
    for cid, vals in sorted(data.items(),
                            key=lambda kv: -sum(x[0] for x in
                                                kv[1].values())):
        name = names[cid]
        series = build_series(vals, dates)
        member_series[cid] = series
        spend, osc, roas7, gp7, conv7, cpc7 = series
        troughs = find_troughs(osc)
        periods = [b - a for a, b in zip(troughs, troughs[1:])]
        tp = (len(dates) - 1 - troughs[-1]) if troughs else None
        m_med = sorted(periods)[len(periods) // 2] if periods else None
        m_lo, m_hi = trough_window(dates, troughs, periods)
        win = (f"{m_lo.strftime('%m/%d')}-{m_hi.strftime('%m/%d')}"
               if m_lo else "")
        tail = min(SYNC_LOOKBACK, len(dates) - 30)
        sync = pearson(osc[-tail:], a_osc[-tail:])
        ptag = perturbed_tag(cid, last)
        sp_tag = momentum_tag(ma(spend, len(spend) - 1, 7),
                              ma(spend, len(spend) - 1, 14),
                              ma(spend, len(spend) - 1, 30))
        lines.append(
            f"{phase_of(osc, troughs):22s} {name:10s} "
            f"T+{tp if tp is not None else '—':<4} "
            f"cycle {str(m_med) + 'd' if m_med else '—':6s} "
            f"next {win:12s} "
            f"sync {f'{sync:+.2f}' if sync is not None else '—':6s} "
            f"spend {sp_tag:6s} {ptag}")
        m_rows = [[name, "PHASE", stamp, "", phase_of(osc, troughs),
                   f"T+{tp}" if tp is not None else ""]]
        for t in troughs:
            m_rows.append([name, "TROUGH", dates[t].isoformat(),
                           dates[t].isoformat(), f"{osc[t]:.4f}", ""])
        if m_med:
            m_rows.append([name, "CYCLE_MEDIAN", "", "", str(m_med),
                           f"n={len(periods)}"])
        if m_lo:
            m_rows.append([name, "NEXT_WINDOW", m_lo.isoformat(),
                           m_hi.isoformat(), "", f"n={len(periods)}"])
        if sync is not None:
            m_rows.append([name, "SYNC", stamp, "", f"{sync:.4f}",
                           f"corr vs ACCOUNT osc, {tail}d"])
        if ptag:
            m_rows.append([name, "PERTURBED", stamp, "", "", ptag])
        state_rows.append((name, m_rows))

    # outputs: txt + CSVs first, charts after
    text = "\n".join(lines)
    print(text)
    (out_dir / f"cycle_phase_{stamp}.txt").write_text(text,
                                                      encoding="utf-8")
    (out_dir / "latest.txt").write_text(text, encoding="utf-8")
    cs_path = out_dir / f"cycle_state_{stamp}.csv"
    with open(cs_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["entity", "record", "date_start", "date_end",
                    "value", "note"])
        for _, rows_ in state_rows:
            for r in rows_:
                w.writerow(r)
    shutil.copy(cs_path, out_dir / "cycle_state_latest.csv")
    (out_dir / "run_header.txt").write_text("\n".join(header),
                                            encoding="utf-8")
    print(f"\nSaved cycle state: {cs_path} (+ cycle_state_latest.csv)")
    print(f"Ledger: {ledger_path} ({n_open} OPEN / {n_graded} graded)")

    if args.smoke:
        print("\n--smoke: charts skipped")
        print(f"  account troughs: "
              f"{[dates[t].isoformat() for t in a_troughs]}")
        print(f"  completed cycles: {len(cycles)}; open cycle: "
              f"{'T+' + str(open_c['age']) if open_c else 'none'}")
        for a in actions:
            print(f"  ledger action: {a}")
        if not actions:
            print("  ledger action: none (already stamped / nothing "
                  "to grade)")
        conn.close()
        return

    qs = fetch_qs_monthly(conn, None, args.qs_from)
    render_main("ACCOUNT", dates, a_series, a_troughs, None, qs, fp,
                cycles, out_dir / f"cycle_v2_ACCOUNT_{stamp}.png")
    if cycles:
        render_overlay("ACCOUNT", dates, a_conv7, a_cpc7, a_troughs,
                       cycles, open_c, fp,
                       out_dir / f"cycle_overlay_ACCOUNT_{stamp}.png")
    if args.campaign:
        for cid, series in member_series.items():
            name = names[cid]
            if args.campaign.upper() not in name.upper():
                continue
            troughs = find_troughs(series[1])
            m_cycles, m_open = cycle_anatomy(dates, series[1],
                                             series[4], series[5],
                                             troughs)
            qs_c = fetch_qs_monthly(conn, cid, args.qs_from)
            safe = name.replace("/", "_").replace(" ", "_")
            render_main(name, dates, series, troughs,
                        targets.get(cid), qs_c, None, m_cycles,
                        out_dir / f"cycle_v2_{safe}_{stamp}.png")
    conn.close()
    print("Charts saved.")


if __name__ == "__main__":
    main()
