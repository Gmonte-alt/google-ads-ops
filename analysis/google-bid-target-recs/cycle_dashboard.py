"""
Cycle & Spiral Dashboard — Google Home (daily, per campaign)
═══════════════════════════════════════════════════════════════════════
Cycle-analysis view of each campaign's tROAS oscillation ("one cycle"
= the ~4-5 week wave), campaign grain because targets are campaign
grain.

Per campaign, one PNG:
  CHIPS   phase · day T+n since last trough · last trough date ·
          median cycle length (range) · projected next-trough WINDOW
  CYCLE   spend oscillator = 7d spend ÷ trailing 30d avg − 1, troughs
          marked (▼) with trough-to-trough day counts, projected
          window shaded. Spend is the oscillator source (zero-lag).
  METRICS leads/day, ROAS(7d) vs target, GP/day — trough verticals
          carried through on aligned axes (tests the hypothesis:
          troughs = leads lowest / ROAS highest; a trough where ROAS
          does NOT crest is itself a signal)
  SPIRAL  monthly spend share by QS bucket (≤4 / 5-6 / 7+) with the
          RATED-SHARE line as a mandatory honesty guard (rated
          coverage is thin; migration + falling rated share =
          partly measurement, and the panel says so itself)
  FOOTER  momentum tags (7v14 / 7v30) for spend, ROAS, leads

Plus a console/txt phase table across campaigns (the 2-second read).

Conventions: DB read-only; campaign_performance_daily_search
device='ALL' JOIN campaigns account 1; revenue/conversions columns
auto-detected; targets = latest TARGET_ROAS event; all data treated
as complete (no lag exclusion — house ruling 8/20); rolling ROAS =
ratio of sums. QS: keyword_quality_daily joined at DAILY grain via
keyword_instances→ad_groups for campaign scope; iw over rated rows.
Next-trough projections are WINDOWS (median ± observed spread),
never dates — ~3 observed cycles cannot support point forecasts.

USAGE:
  python cycle_dashboard.py
  python cycle_dashboard.py --campaign TIER2 --window-days 120
"""

import argparse
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.gridspec import GridSpec

DEFAULT_DB_PATH = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")
DEFAULT_OUT_DIR = Path(r"C:\Projects\google-ads-ops\analysis"
                       r"\profit-attribution-tracker\output\cycle-dashboard")
ACCOUNT_ID = 1
REV_CANDIDATES = ["conversion_value", "conversions_value", "conv_value",
                  "primary_conv_value", "total_conv_value"]
CONV_CANDIDATES = ["conversions", "primary_conv_count", "conv_count",
                   "all_conversions"]
C_OSC, C_TROUGH = "#2a78d6", "#1a5aa8"
QS_COLORS = {"QS ≤ 4": "#d03b3b", "QS 5–6": "#c98500", "QS 7+": "#0ca30c"}


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


def fetch_campaigns(conn, start, end):
    rev = pick_col(conn, "campaign_performance_daily_search", REV_CANDIDATES)
    cnv = pick_col(conn, "campaign_performance_daily_search", CONV_CANDIDATES)
    rev_e = f"SUM(p.{rev})" if rev else "NULL"
    cnv_e = f"SUM(p.{cnv})" if cnv else "NULL"
    data, names = {}, {}
    sql = f"""SELECT p.campaign_id cid, c.campaign_name nm, p.date d,
                     SUM(p.cost_micros)/1e6 cost, {rev_e} rev, {cnv_e} conv
              FROM campaign_performance_daily_search p
              JOIN campaigns c ON c.campaign_id=p.campaign_id
              WHERE c.account_id=? AND p.device='ALL'
                AND p.date BETWEEN ? AND ? GROUP BY 1,2,3"""
    for r in conn.execute(sql, [ACCOUNT_ID, start.isoformat(),
                                end.isoformat()]):
        cid = str(r["cid"])
        names[cid] = short_campaign(r["nm"])
        data.setdefault(cid, {})[str(r["d"])[:10]] = (
            r["cost"] or 0.0, r["rev"] or 0.0, r["conv"] or 0.0)
    return data, names


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
    """How keyword_instances reaches campaign_id. Returns (join_sql,
    campaign_expr) or None. Mirrors the tripwire's introspection."""
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
    account-wide (no campaign filter)."""
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
        print(f"  WARN: QS panel skipped — no keyword→campaign linkage "
              f"found (keyword_instances lacks campaign_id/ad_group_id "
              f"path)")
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
            print(f"  WARN: QS panel empty for campaign {cid} — the "
                  f"keyword→campaign join matched no rows (check "
                  f"linkage/types); panel omitted")
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
    spend = [vals.get(d.isoformat(), (0, 0, 0))[0] for d in dates]
    rev = [vals.get(d.isoformat(), (0, 0, 0))[1] for d in dates]
    conv = [vals.get(d.isoformat(), (0, 0, 0))[2] for d in dates]
    s7 = [ma(spend, i, 7) for i in range(len(spend))]
    s30 = [ma(spend, i, 30) for i in range(len(spend))]
    osc = [(a / b - 1) if b > 0 else 0.0 for a, b in zip(s7, s30)]
    roas7 = []
    for i in range(len(dates)):
        lo = max(0, i - 6)
        c = sum(spend[lo:i + 1]); v = sum(rev[lo:i + 1])
        roas7.append(v / c if c > 0 else None)
    gp7 = [ma([r - c for r, c in zip(rev, spend)], i, 7)
           for i in range(len(spend))]
    conv7 = [ma(conv, i, 7) for i in range(len(conv))]
    return spend, osc, roas7, gp7, conv7


def find_troughs(osc, warmup=30, min_sep=14, depth=-0.05):
    troughs = []
    for i in range(warmup, len(osc) - 3):
        lo = max(warmup, i - 7); hi = min(len(osc), i + 8)
        if osc[i] <= min(osc[lo:hi]) and osc[i] < depth:
            if not troughs or i - troughs[-1] >= min_sep:
                troughs.append(i)
            elif osc[i] < osc[troughs[-1]]:
                troughs[-1] = i
    return troughs


def windows_from_osc(dates, osc_daily, troughs, thresh, bridge=2):
    """Expansion windows on the DAILY-grain oscillator (daily spend /
    30d trend - 1) — NOT the 7d-smoothed cycle oscillator, whose
    trailing crossings shift windows several days late (measured
    cost: surge ramp days dropped). One window per trough interval
    (and the open interval): the maximal run above thresh containing
    the interval's daily peak, bridging up to `bridge` consecutive
    sub-threshold days (weekend dips). Returns
    [(start, end, peak_date, peak_osc, open_flag)]."""
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


def render(name, dates, series, troughs, target, qs_monthly, out_png):
    spend, osc, roas7, gp7, conv7 = series
    n_qs = 1 if qs_monthly else 0
    fig = plt.figure(figsize=(12, 12.5 + 1.6 * n_qs))
    gs = GridSpec(5 + n_qs, 1, figure=fig,
                  height_ratios=[0.65, 2.2, 1.25, 1.25, 1.25] +
                  ([1.9] if n_qs else []), hspace=0.42)

    # chips
    axc = fig.add_subplot(gs[0]); axc.axis("off")
    last_t = troughs[-1] if troughs else None
    periods = [b - a for a, b in zip(troughs, troughs[1:])]
    med = sorted(periods)[len(periods) // 2] if periods else None
    tplus = (len(dates) - 1 - last_t) if last_t is not None else None
    win = ""
    if last_t is not None and periods:
        pad = 10 if len(periods) < 2 else 4
        lo = dates[last_t] + timedelta(days=max(min(periods) - (pad - 4),
                                                7))
        hi = dates[last_t] + timedelta(days=max(periods) + pad)
        win = f"{lo.strftime('%b %d')}–{hi.strftime('%b %d')}"
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
    axc.text(0, 0.75, f"{name} — cycle & spiral — {dates[-1]}",
             fontsize=13, fontweight="bold")
    axc.text(0, 0.12, "   ·   ".join(c for c in chips if c), fontsize=10,
             color="#444")

    # cycle panel
    ax1 = fig.add_subplot(gs[1])
    ax1.plot(dates, [o * 100 for o in osc], color=C_OSC, lw=1.8)
    ax1.fill_between(dates, 0, [o * 100 for o in osc],
                     color=C_OSC, alpha=0.08)
    ax1.axhline(0, color="#888", lw=0.8)
    for j, t in enumerate(troughs):
        ax1.axvline(dates[t], color=C_TROUGH, ls="--", lw=0.9, alpha=0.6)
        lbl = f"▼ {dates[t].strftime('%b %d')}"
        if j > 0:
            lbl += f" (+{troughs[j] - troughs[j-1]}d)"
        ax1.annotate(lbl, (dates[t], min(o * 100 for o in osc)),
                     fontsize=8.5, color=C_TROUGH,
                     xytext=(-16, -14), textcoords="offset points")
    if last_t is not None and periods:
        lo = dates[last_t] + timedelta(days=min(periods))
        hi = dates[last_t] + timedelta(days=max(periods) + 4)
        ax1.axvspan(max(lo, dates[0]), min(hi, dates[-1] +
                    timedelta(days=1)), color="#898781", alpha=0.12)
    ax1.set_ylabel("spend osc.\n7d ÷ 30d − 1 (%)", fontsize=9)
    ax1.grid(True, alpha=0.2)
    ax1.tick_params(labelsize=8)
    ax1.set_title("Cycle panel — ▼ troughs w/ day counts · shaded = "
                  "projected trough window", fontsize=9.5, loc="left",
                  color="#666")

    # metric panels with trough verticals
    panels = [("Leads / day (7d)", conv7, None),
              ("ROAS (7d) · dotted = target", roas7, target),
              ("GP / day ($, 7d)", gp7, None)]
    for k, (title, ys, tgt) in enumerate(panels):
        ax = fig.add_subplot(gs[2 + k], sharex=ax1)
        ax.plot(dates, [y if y is not None else float("nan") for y in ys],
                color="#555", lw=1.6)
        if tgt:
            ax.axhline(tgt, color="#333", ls=":", lw=1)
        for t in troughs:
            ax.axvline(dates[t], color=C_TROUGH, ls="--", lw=0.9,
                       alpha=0.45)
        ax.set_title(title, fontsize=9.5, loc="left", color="#666")
        ax.grid(True, alpha=0.2)
        ax.tick_params(labelsize=8)

    # QS migration panel
    if qs_monthly:
        axq = fig.add_subplot(gs[5])
        yms = sorted(qs_monthly.keys())
        tot = [sum(qs_monthly[y].values()) or 1 for y in yms]
        low = [qs_monthly[y].get("low", 0) / t for y, t in zip(yms, tot)]
        mid = [qs_monthly[y].get("mid", 0) / t for y, t in zip(yms, tot)]
        high = [qs_monthly[y].get("high", 0) / t for y, t in zip(yms, tot)]
        unr = [qs_monthly[y].get("unrated", 0) / t
               for y, t in zip(yms, tot)]
        rated = [1 - u for u in unr]
        # shares within RATED spend (so unrated growth can't fake migration)
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
    # date axis on the cycle panel too (autofmt hides non-bottom labels)
    for lab in ax1.get_xticklabels():
        lab.set_visible(True)
        lab.set_rotation(30)
        lab.set_horizontalalignment("right")
    ax1.tick_params(labelbottom=True)
    fig.savefig(out_png, dpi=140, bbox_inches="tight")
    plt.close(fig)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--db-path", default=str(DEFAULT_DB_PATH))
    p.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    p.add_argument("--window-days", type=int, default=120)
    p.add_argument("--campaign", default=None)
    p.add_argument("--qs-from", default="2026-01-01")
    p.add_argument("--window-osc-thresh", type=float, default=0.10,
                   help="exported expansion windows = contiguous "
                        "DAILY-grain oscillator (spend/30d-1) runs "
                        "above this; daily grain is noisier than the "
                        "cycle oscillator, hence 0.10 default")
    args = p.parse_args()

    db = Path(args.db_path)
    if not db.exists():
        sys.exit(f"DB not found: {db}")
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    conn = connect_ro(db)
    row = conn.execute(
        """SELECT MAX(p.date) FROM campaign_performance_daily_search p
           JOIN campaigns c ON c.campaign_id=p.campaign_id
           WHERE c.account_id=?""", [ACCOUNT_ID]).fetchone()
    last = datetime.strptime(str(row[0])[:10], "%Y-%m-%d").date()
    start = last - timedelta(days=args.window_days + 30)
    data, names = fetch_campaigns(conn, start, last)
    targets = fetch_targets(conn, list(data.keys()))

    dates = [last - timedelta(days=k)
             for k in range(args.window_days - 1, -1, -1)]
    stamp = last.isoformat()
    state_rows = []
    lines = [f"CYCLE PHASE TABLE — {stamp}",
             f"(oscillator = 7d spend ÷ 30d avg − 1; projections are "
             f"windows, ~3 observed cycles only)", ""]
    acct = {}
    for series_ in data.values():
        for d, (c, r, v) in series_.items():
            a = acct.get(d, (0.0, 0.0, 0.0))
            acct[d] = (a[0] + c, a[1] + r, a[2] + v)
    entities = [(None, "ACCOUNT", acct)] + [
        (cid, names[cid], vals) for cid, vals in
        sorted(data.items(), key=lambda kv: -sum(x[0] for x in
                                                 kv[1].values()))]
    for cid, name, vals in entities:
        series = build_series(vals, dates)
        spend, osc, roas7, gp7, conv7 = series
        troughs = find_troughs(osc)
        periods = [b - a for a, b in zip(troughs, troughs[1:])]
        tplus = (len(dates) - 1 - troughs[-1]) if troughs else None
        med = sorted(periods)[len(periods) // 2] if periods else None
        win = ""
        if troughs and periods:
            pad = 10 if len(periods) < 2 else 4
            lo = dates[troughs[-1]] + timedelta(
                days=max(min(periods) - (pad - 4), 7))
            hi = dates[troughs[-1]] + timedelta(days=max(periods) + pad)
            win = f"{lo.strftime('%m/%d')}-{hi.strftime('%m/%d')}"
        ent_rows = [[name, "PHASE", dates[-1].isoformat(), "",
                     phase_of(osc, troughs),
                     f"T+{tplus}" if tplus is not None else ""]]
        for t in troughs:
            ent_rows.append([name, "TROUGH", dates[t].isoformat(),
                             dates[t].isoformat(), f"{osc[t]:.4f}", ""])
        s30 = [ma(spend, i, 30) for i in range(len(spend))]
        osc_daily = [(s / b - 1) if b > 0 else 0.0
                     for s, b in zip(spend, s30)]
        for ws_d, we_d, pk_d, pk_o, open_f in windows_from_osc(
                dates, osc_daily, troughs, args.window_osc_thresh):
            ent_rows.append([name, "WINDOW", ws_d.isoformat(),
                             we_d.isoformat(), f"{pk_o:.4f}",
                             ("OPEN; " if open_f else "")
                             + f"peak={pk_d.isoformat()}; daily-grain"])
        if med:
            ent_rows.append([name, "CYCLE_MEDIAN", "", "", str(med),
                             f"n={len(periods)}"])
        if win:
            ent_rows.append([name, "NEXT_WINDOW",
                             (dates[troughs[-1]] + timedelta(
                                 days=max(min(periods) - 6, 7))
                              ).isoformat() if periods else "",
                             (dates[troughs[-1]] + timedelta(
                                 days=max(periods) + (10 if len(
                                     periods) < 2 else 4))
                              ).isoformat() if periods else "",
                             "", f"n={len(periods)}"])
        state_rows.append((name, ent_rows))
        sp_tag = momentum_tag(ma(spend, len(spend) - 1, 7),
                              ma(spend, len(spend) - 1, 14),
                              ma(spend, len(spend) - 1, 30))
        lines.append(
            f"{phase_of(osc, troughs):22s} {name:14s} "
            f"T+{tplus if tplus is not None else '—':<4} "
            f"cycle {str(med) + 'd' if med else '—':6s} "
            f"next {win:12s} spend {sp_tag}")
        if args.campaign and args.campaign.upper() not in name.upper():
            continue
        qs = fetch_qs_monthly(conn, cid, args.qs_from)
        safe = name.replace("/", "_").replace(" ", "_")
        render(name, dates, series, troughs,
               targets.get(cid) if cid else None, qs,
               out_dir / f"cycle_{safe}_{stamp}.png")
    conn.close()

    text = "\n".join(lines)
    print(text)
    (out_dir / f"cycle_phase_{stamp}.txt").write_text(text,
                                                     encoding="utf-8")
    # machine-readable cycle state (consumed by marginal_roas_map etc.)
    import csv as _csv
    cs_path = out_dir / f"cycle_state_{stamp}.csv"
    with open(cs_path, "w", newline="", encoding="utf-8") as f:
        w = _csv.writer(f)
        w.writerow(["entity", "record", "date_start", "date_end",
                    "value", "note"])
        for name, rows_ in state_rows:
            for r in rows_:
                w.writerow(r)
    import shutil
    shutil.copy(cs_path, out_dir / "cycle_state_latest.csv")
    print(f"Saved cycle state: {cs_path} (+ cycle_state_latest.csv)")
    (out_dir / "latest.txt").write_text(text, encoding="utf-8")
    print(f"\nSaved dashboards to {out_dir}")


if __name__ == "__main__":
    main()
