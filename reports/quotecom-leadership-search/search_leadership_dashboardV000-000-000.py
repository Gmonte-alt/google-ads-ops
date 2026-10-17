#!/usr/bin/env python3
"""
search_leadership_dashboard.py — Quote.com Search Leadership Dashboard
(Home vertical, Google account 1).

Generates the leadership campaign report from paid_search.db, matching
the Search_Leadership_Dashboard-home-only.xlsx template.

SOURCING (architect ruling, 2026-09-02): this is a CAMPAIGN-grain
leadership artifact, so leads/revenue/cost come from
campaign_performance_daily_search NATIVE columns — conversions,
conversion_value (singular), cost_micros/1e6 — at device='ALL'. These
matched the UI to the digit in the July three-way reconciliation. kpd
routed-primary is deliberately NOT used here (it runs ~1% light from
the AI Max attribution drip and is the keyword-analysis stack, not the
campaign-reporting stack). Never mix the two in one metric.

ROW SET (architect ruling, 2026-09-04): driven by performance-in-window,
never by status — DISTINCT campaigns with impressions>0 OR cost>0 at
device='ALL' in the trailing-30 window, then annotated from
campaigns_search. A campaign paused mid-window keeps its spend visible,
rendered with a PAUSED marker (column F) and greyed name. The same
served-roster drives every panel so descriptive and metric columns can
never disagree. status and ai_max_enabled are current snapshots, not
historical states.

LAYOUT (per Gilbert):
  A  Campaign Name        (greyed when PAUSED)
  B  Ad Groups            — ad groups that SERVED in trailing 30
                            (ad_group_performance_daily, impressions>0)
  C  Conv/Mo              — trailing-30 leads (cpd_search conversions;
                            placeholder definition until clarified)
  D  Has AI Max (y/n)     — campaigns_search.ai_max_enabled: Y / N / ?
                            (? = NULL, created between sync runs)
  E  LP Version           — from dashboard_config.csv (manual; skeleton
                            auto-generated on first run)
  F  (open) — 'PAUSED' marker only on paused campaigns, else empty
  G  Current tROAS      — latest change-history target + start date
  H  Previous tROAS     — the target it changed from + its start date
                          (campaign_change_events, rename-aware,
                          anchor-aware; snapshot fallback '(current)')
  I  Actual ROAS, Last 30 Days        J..O  Last Week … 6 Weeks Ago
  P  GP Earned,  Last 30 Days         Q..V  Last Week … 6 Weeks Ago

Weeks are the six most recent COMPLETE Mon-Sun weeks at the anchor;
the 30-day windows end at the anchor itself. Date-range labels are
computed each run. ROAS = conversion_value / cost; GP =
conversion_value − cost. Zero-cost periods render blank ROAS.

FOOTNOTES rendered on-sheet: conversion lag (most recent 2-3 days
restate upward — windows stay UI-aligned by design) and PMax exclusion
(the _search views exclude PERFORMANCE_MAX by architecture; the live
PMax campaign is named).

USAGE
    python search_leadership_dashboard.py
    python search_leadership_dashboard.py --anchor-date 2026-08-23
    --db-path / --account-id / --output-dir / --config
"""

import argparse
import csv
import sqlite3
from datetime import date, timedelta
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

DEFAULT_DB_PATH = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")
# Script home: C:\Projects\google-ads-ops\reports\quotecom-leadership-search\
# Defaults are anchored to the script's own directory so the report and
# config land there no matter which folder the script is launched from.
SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_OUTPUT_DIR = SCRIPT_DIR / "output"
DEFAULT_CONFIG = SCRIPT_DIR / "dashboard_config.csv"
DEFAULT_ACCOUNT_ID = 1
N_WEEKS = 6
PMAX_NOTE_CAMPAIGN = "PMax (PERFORMANCE_MAX) campaigns"

MONEY_FMT = '"$"#,##0'
ROAS_FMT = "0.00"
CONV_FMT = "#,##0"

TITLE_FONT = Font(bold=True, size=14)
PANEL_FONT = Font(bold=True, size=11)
HEADER_FILL = PatternFill("solid", fgColor="1F4E79")
HEADER_FONT = Font(bold=True, color="FFFFFF", size=10)
SUB_FILL = PatternFill("solid", fgColor="2E74B5")
BODY_FONT = Font(size=10)
PAUSED_NAME_FONT = Font(size=10, color="909090", italic=True)
PAUSED_MARK_FONT = Font(size=10, color="C00000", bold=True)
TOTAL_FONT = Font(bold=True, size=10)
TOTAL_FILL = PatternFill("solid", fgColor="DDEBF7")
NOTE_FONT = Font(italic=True, size=9, color="666666")
THIN = Side(style="thin", color="D9D9D9")
BORDER = Border(top=THIN, bottom=THIN, left=THIN, right=THIN)

ROAS_TOTAL_COL = 9    # I
GP_TOTAL_COL = 16     # P
HEADER_ROW_PANEL = 4
HEADER_ROW_REL = 5
HEADER_ROW_DATES = 6
FIRST_DATA_ROW = 7


def q1(conn, sql, params=()):
    return conn.execute(sql, params).fetchall()


def table_columns(conn, table):
    try:
        cur = conn.execute(f"SELECT * FROM {table} LIMIT 0")
    except sqlite3.Error as exc:
        raise SystemExit(f"ERROR: cannot read {table}: {exc}")
    return [d[0] for d in cur.description]


def build_windows(anchor: date):
    """Trailing-30 + six most recent complete Mon-Sun weeks."""
    t30 = (anchor - timedelta(days=29), anchor)
    last_sun = anchor - timedelta(days=(anchor.weekday() + 1) % 7)
    weeks = []
    for k in range(N_WEEKS):
        sun = last_sun - timedelta(weeks=k)
        weeks.append((sun - timedelta(days=6), sun))
    return t30, weeks


def _pick(cols, *candidates):
    return next((c for c in candidates if c in cols), None)


def _mdY(dstr) -> str:
    d = date.fromisoformat(str(dstr)[:10])
    return f"{d.month}/{d.day}/{str(d.year)[2:]}"


def fetch_troas_targets(conn, camp_ids, account_id, snapshot):
    """Columns G/H: current and previous target ROAS with start dates.

    Reads the derived change-history model (campaign_change_events,
    rename-aware by campaign_id — same source as
    troas_changes_export.py). Per campaign:
      G = new_value of the LATEST tROAS event, 'since' that event's date
      H = old_value of that same event, 'since' the date of the event
          that established it (the immediately preceding event)
    Timeline-start anchors (old_value NULL) are the model's opening
    markers, not real changes: a start date inherited from an anchor is
    rendered 'since ≤ date' — the target has been in effect since AT
    LEAST tracking start. A campaign whose only row is the anchor gets
    G with '≤' and an empty H (no known prior target).

    Fallback + cross-check against campaigns_search.target_roas (the
    verified daily snapshot): campaigns with no events display the
    snapshot as 'current' with no date; where the event-derived current
    disagrees with the snapshot by > 0.005, the sheet keeps the dated
    event value and a console warning lists the mismatch (the events
    model may lag the daily snapshot by one sync).

    Returns ({campaign_id: (g_text, h_text)}, [warnings]).
    """
    out = {cid: ("", "") for cid in camp_ids}
    warnings = []

    cols = [r[1] for r in conn.execute(
        "PRAGMA table_info(campaign_change_events)")]
    events = {}
    if cols:
        c_date = _pick(cols, "change_date", "event_date", "date")
        c_type = _pick(cols, "change_type", "attribute", "type")
        c_old = _pick(cols, "old_value", "old")
        c_new = _pick(cols, "new_value", "new")
        c_acct = _pick(cols, "account_id")
        if all([c_date, c_type, c_old, c_new]):
            ph = ",".join("?" * len(camp_ids))
            acct_clause = f" AND {c_acct} = ?" if c_acct else ""
            params = [*camp_ids] + ([account_id] if c_acct else [])
            rows = conn.execute(
                f"SELECT campaign_id, {c_date}, {c_old}, {c_new} "
                f"FROM campaign_change_events "
                f"WHERE upper({c_type}) LIKE '%ROAS%' "
                f"AND campaign_id IN ({ph}){acct_clause} "
                f"ORDER BY campaign_id, {c_date}",
                params).fetchall()
            for cid, d, old, new in rows:
                events.setdefault(cid, []).append((str(d), old, new))
        else:
            warnings.append(f"campaign_change_events has unexpected "
                            f"schema ({cols}) — tROAS columns fall back "
                            f"to snapshot only")
    else:
        warnings.append("campaign_change_events not found (run 17 "
                        "first) — tROAS columns fall back to the "
                        "campaigns_search.target_roas snapshot, undated")

    for cid in camp_ids:
        evs = events.get(cid, [])
        snap = snapshot.get(cid)
        if not evs:
            if snap:
                out[cid] = (f"{float(snap):g} (current)", "")
            continue
        latest = evs[-1]
        d_latest, old, new = latest
        if old is None:
            # Only/latest row is the timeline-start anchor.
            out[cid] = (f"{float(new):g} (since ≤ {_mdY(d_latest)})", "")
        else:
            g_text = f"{float(new):g} (since {_mdY(d_latest)})"
            # Previous target started at the preceding event's date.
            prev = evs[-2] if len(evs) >= 2 else None
            if prev is not None:
                approx = "≤ " if prev[1] is None else ""
                h_text = f"{float(old):g} (since {approx}{_mdY(prev[0])})"
            else:
                h_text = f"{float(old):g}"
            out[cid] = (g_text, h_text)
        if snap is not None:
            try:
                if abs(float(snap) - float(latest[2])) > 0.005:
                    warnings.append(
                        f"campaign {cid}: change-history current tROAS "
                        f"{latest[2]} != snapshot target_roas {snap} — "
                        f"events model may lag the daily sync; sheet "
                        f"shows the dated event value")
            except (TypeError, ValueError):
                pass
    return out, warnings


def rng_label(a: date, b: date) -> str:
    return f"{a.month}/{a.day} - {b.month}/{b.day}"


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db-path", default=str(DEFAULT_DB_PATH))
    p.add_argument("--account-id", type=int, default=DEFAULT_ACCOUNT_ID)
    p.add_argument("--anchor-date", default=None,
                   help="YYYY-MM-DD; default = MAX(date) in cpd_search")
    p.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    p.add_argument("--config", default=str(DEFAULT_CONFIG),
                   help="campaign_id,lp_version CSV (skeleton generated "
                        "on first run)")
    args = p.parse_args()

    db = Path(args.db_path)
    if not db.exists():
        raise SystemExit(f"ERROR: DB not found at {db}")
    conn = sqlite3.connect(db)

    # ── Verify the verified schemas are actually there ─────────────
    cpd_cols = table_columns(conn, "campaign_performance_daily_search")
    for col in ("date", "device", "impressions", "cost_micros",
                "conversions", "conversion_value"):
        if col not in cpd_cols:
            raise SystemExit(
                f"ERROR: campaign_performance_daily_search lacks '{col}' "
                f"(columns: {cpd_cols}). The leadership sourcing ruling "
                f"expects native conversions/conversion_value here — "
                f"check with the DB architect.")
    ca_cols = table_columns(conn, "campaigns_search")
    has_ai = "ai_max_enabled" in ca_cols
    has_bundling = "ai_max_bundling" in ca_cols
    if not has_ai:
        print("  ⚠ campaigns_search.ai_max_enabled not found — Has AI Max "
              "column will render '?' throughout (is the AI Max sync line "
              "running?)")

    row = conn.execute(
        "SELECT MAX(date) FROM campaign_performance_daily_search "
        "WHERE account_id = ? AND device = 'ALL'", (args.account_id,)
    ).fetchone()
    if not row or not row[0]:
        raise SystemExit("ERROR: no cpd_search rows for this account")
    anchor = (date.fromisoformat(args.anchor_date) if args.anchor_date
              else date.fromisoformat(str(row[0])[:10]))
    t30, weeks = build_windows(anchor)
    windows = [("Last 30 Days", *t30)] + [
        ("Last Week" if k == 0 else f"{k + 1} Weeks Ago", a, b)
        for k, (a, b) in enumerate(weeks)]
    print(f"Anchor {anchor} | 30d {t30[0]} → {t30[1]} | "
          f"weeks {weeks[-1][0]} → {weeks[0][1]}")

    # ── Served roster (drives every panel; never status-filtered) ──
    ai_sel = ("CASE ca.ai_max_enabled WHEN 1 THEN 'Y' WHEN 0 THEN 'N' "
              "ELSE '?' END" if has_ai else "'?'")
    bundling_sel = "ca.ai_max_bundling" if has_bundling else "NULL"
    troas_sel = ("ca.target_roas" if "target_roas" in ca_cols else "NULL")
    roster = q1(conn, f"""
        SELECT ca.campaign_id, ca.campaign_name, ca.status,
               {ai_sel} AS has_ai_max, {bundling_sel} AS ai_max_bundling,
               {troas_sel} AS target_roas
        FROM (
            SELECT DISTINCT campaign_id
            FROM campaign_performance_daily_search
            WHERE account_id = ? AND device = 'ALL'
              AND date BETWEEN ? AND ?
              AND (impressions > 0 OR cost_micros > 0)
        ) AS served
        JOIN campaigns_search ca
          ON ca.campaign_id = served.campaign_id AND ca.account_id = ?
        ORDER BY ca.status, ca.campaign_name
    """, (args.account_id, t30[0].isoformat(), t30[1].isoformat(),
          args.account_id))
    if not roster:
        raise SystemExit("ERROR: served roster is empty for the window")
    print(f"Roster: {len(roster)} campaigns "
          f"({sum(1 for r in roster if r[2] != 'ENABLED')} not ENABLED)")

    camp_ids = [r[0] for r in roster]
    troas_snapshot = {r[0]: r[5] for r in roster if r[5] is not None}
    ph = ",".join("?" * len(camp_ids))

    # ── Ad groups that served in trailing 30 ───────────────────────
    agp_cols = table_columns(conn, "ad_group_performance_daily")
    ag_dev = "AND device = 'ALL'" if "device" in agp_cols else ""
    ag_date = "date" if "date" in agp_cols else "report_date"
    ag_rows = q1(conn, f"""
        SELECT campaign_id, COUNT(DISTINCT ad_group_id)
        FROM ad_group_performance_daily
        WHERE campaign_id IN ({ph}) {ag_dev}
          AND {ag_date} BETWEEN ? AND ? AND impressions > 0
        GROUP BY campaign_id
    """, (*camp_ids, t30[0].isoformat(), t30[1].isoformat()))
    ad_groups = dict(ag_rows)

    # ── Per-window metrics (native cpd_search columns) ─────────────
    metrics = {}  # (campaign_id, window_idx) -> (cost, conv, rev)
    acct = {}     # window_idx -> [cost, conv, rev]
    for wi, (_lbl, a, b) in enumerate(windows):
        rows = q1(conn, f"""
            SELECT campaign_id,
                   SUM(cost_micros) / 1.0e6,
                   SUM(conversions),
                   SUM(conversion_value)
            FROM campaign_performance_daily_search
            WHERE account_id = ? AND device = 'ALL'
              AND campaign_id IN ({ph})
              AND date BETWEEN ? AND ?
            GROUP BY campaign_id
        """, (args.account_id, *camp_ids, a.isoformat(), b.isoformat()))
        acct[wi] = [0.0, 0.0, 0.0]
        for cid, cost, conv, rev in rows:
            metrics[(cid, wi)] = (cost or 0.0, conv or 0.0, rev or 0.0)
            acct[wi][0] += cost or 0.0
            acct[wi][1] += conv or 0.0
            acct[wi][2] += rev or 0.0

    # ── tROAS current/previous targets (cols G/H) ──────────────────
    troas_gh, troas_warnings = fetch_troas_targets(
        conn, camp_ids, args.account_id, troas_snapshot)
    for w in troas_warnings:
        print(f"  ⚠ {w}")

    # ── LP-version config (manual column E) ────────────────────────
    cfg_path = Path(args.config)
    lp = {}
    if cfg_path.exists():
        with open(cfg_path, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                try:
                    lp[int(r["campaign_id"])] = (r.get("lp_version") or "").strip()
                except (KeyError, ValueError):
                    continue
        print(f"Config: {cfg_path} — LP versions for "
              f"{sum(1 for v in lp.values() if v)} campaigns")
    else:
        with open(cfg_path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["campaign_id", "campaign_name", "lp_version"])
            for cid, name, *_ in roster:
                w.writerow([cid, name, ""])
        print(f"Config: {cfg_path} not found — skeleton generated with "
              f"{len(roster)} campaigns. Fill lp_version and re-run; "
              f"column E is blank this run.")

    # ══ Workbook ═══════════════════════════════════════════════════
    wb = Workbook()
    ws = wb.active
    ws.title = "Home"

    ws.cell(row=1, column=1,
            value="Quote.com — Search Leadership Dashboard (Home)"
            ).font = TITLE_FONT
    ws.cell(row=2, column=1, value=(
        f"Generated {date.today()} | data through {anchor} | "
        f"campaign-grain native metrics (matches Google Ads UI)"
        )).font = NOTE_FONT

    # Panel titles
    for col, title in ((ROAS_TOTAL_COL, "Actual ROAS"),
                       (GP_TOTAL_COL, "Gross Profit Earned")):
        c = ws.cell(row=HEADER_ROW_PANEL, column=col, value=title)
        c.font = PANEL_FONT
    ws.cell(row=HEADER_ROW_PANEL, column=1, value="Quote.com").font = PANEL_FONT

    # Relative + date-range header rows for both panels
    for base in (ROAS_TOTAL_COL, GP_TOTAL_COL):
        for wi, (lbl, a, b) in enumerate(windows):
            c1 = ws.cell(row=HEADER_ROW_REL, column=base + wi, value=lbl)
            c1.fill = HEADER_FILL
            c1.font = HEADER_FONT
            c1.alignment = Alignment(horizontal="center")
            c2 = ws.cell(row=HEADER_ROW_DATES, column=base + wi,
                         value=("Total: " if wi == 0 else "") + rng_label(a, b))
            c2.fill = SUB_FILL
            c2.font = HEADER_FONT
            c2.alignment = Alignment(horizontal="center")

    left_headers = ["Campaign Name", "Ad Groups", "Conv/Mo",
                    "Has AI Max (y/n)", "LP Version Currently Running",
                    "", "Current tROAS", "Previous tROAS"]  # F open (PAUSED marker only)
    for j, h in enumerate(left_headers, 1):
        c = ws.cell(row=HEADER_ROW_DATES, column=j, value=h)
        if h:
            c.fill = HEADER_FILL
            c.font = HEADER_FONT
            c.alignment = Alignment(horizontal="center", wrap_text=True)

    # ── Campaign rows ──────────────────────────────────────────────
    r = FIRST_DATA_ROW
    for cid, name, status, ai_yn, bundling, _troas_snap in roster:
        paused = status != "ENABLED"
        name_cell = ws.cell(row=r, column=1, value=name)
        name_cell.font = PAUSED_NAME_FONT if paused else BODY_FONT

        ws.cell(row=r, column=2, value=ad_groups.get(cid, 0)
                ).number_format = "#,##0"
        c30 = metrics.get((cid, 0), (0.0, 0.0, 0.0))
        ws.cell(row=r, column=3, value=round(c30[1])
                ).number_format = CONV_FMT
        ws.cell(row=r, column=4, value=ai_yn).alignment = \
            Alignment(horizontal="center")
        ws.cell(row=r, column=5, value=lp.get(cid, ""))
        if paused:
            m = ws.cell(row=r, column=6, value="PAUSED")
            m.font = PAUSED_MARK_FONT
            m.alignment = Alignment(horizontal="center")
        g_text, h_text = troas_gh.get(cid, ("", ""))
        for col, txt in ((7, g_text), (8, h_text)):
            c = ws.cell(row=r, column=col, value=txt or None)
            c.font = BODY_FONT
            c.alignment = Alignment(horizontal="center")

        for base, kind in ((ROAS_TOTAL_COL, "roas"), (GP_TOTAL_COL, "gp")):
            for wi in range(len(windows)):
                cost, _conv, rev = metrics.get((cid, wi), (0.0, 0.0, 0.0))
                if kind == "roas":
                    val = (rev / cost) if cost > 0 else None
                    fmt = ROAS_FMT
                else:
                    val = rev - cost if (cost or rev) else None
                    fmt = MONEY_FMT
                cell = ws.cell(row=r, column=base + wi, value=val)
                cell.number_format = fmt
                cell.font = BODY_FONT
                cell.border = BORDER
        for j in range(1, 9):
            ws.cell(row=r, column=j).border = BORDER
        r += 1

    # ── Account total row ──────────────────────────────────────────
    ws.cell(row=r, column=1, value="ACCOUNT TOTAL").font = TOTAL_FONT
    ws.cell(row=r, column=1).fill = TOTAL_FILL
    ws.cell(row=r, column=3, value=round(acct[0][1])).number_format = CONV_FMT
    ws.cell(row=r, column=3).font = TOTAL_FONT
    ws.cell(row=r, column=3).fill = TOTAL_FILL
    for base, kind in ((ROAS_TOTAL_COL, "roas"), (GP_TOTAL_COL, "gp")):
        for wi in range(len(windows)):
            cost, _conv, rev = acct[wi]
            val = ((rev / cost) if cost > 0 else None) if kind == "roas" \
                else (rev - cost if (cost or rev) else None)
            cell = ws.cell(row=r, column=base + wi, value=val)
            cell.number_format = ROAS_FMT if kind == "roas" else MONEY_FMT
            cell.font = TOTAL_FONT
            cell.fill = TOTAL_FILL
            cell.border = BORDER

    # ── Footnotes ──────────────────────────────────────────────────
    fr = r + 2
    ws.cell(row=fr, column=1, value=(
        "Notes: (1) Conversions continue to land for 2-3 days after the "
        "click — the most recent days restate upward; windows are kept "
        "UI-aligned by design. (2) " + PMAX_NOTE_CAMPAIGN + " are excluded "
        "by the account's _search reporting architecture. (3) Conv/Mo = "
        "trailing-30-day leads (placeholder definition pending "
        "clarification). (4) Metrics are campaign-grain native columns "
        "and match the Google Ads UI; PAUSED rows served during the "
        "window and are annotated with current status. (5) Current/Previous "
        "tROAS come from the change-history model with target start "
        "dates; '≤ date' means in effect since at least tracking start "
        "(the model's timeline opens there); '(current)' means snapshot "
        "value with no change history."
        )).font = NOTE_FONT

    # widths + freeze
    widths = {1: 46, 2: 9, 3: 8, 4: 13, 5: 24, 6: 9, 7: 19, 8: 19}
    for c, w in widths.items():
        ws.column_dimensions[get_column_letter(c)].width = w
    for c in range(ROAS_TOTAL_COL, GP_TOTAL_COL + len(windows)):
        ws.column_dimensions[get_column_letter(c)].width = 11
    ws.freeze_panes = f"B{FIRST_DATA_ROW}"

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"Search_Leadership_Dashboard_home_{anchor.isoformat()}.xlsx"
    wb.save(out)
    print(f"\n  ✓ Wrote {out}")

    # console recap
    cost, conv, rev = acct[0]
    print(f"Last 30: cost ${cost:,.0f} | leads {conv:,.0f} | "
          f"rev ${rev:,.0f} | ROAS {rev / cost if cost else 0:.2f} | "
          f"GP ${rev - cost:,.0f}")
    conn.close()


if __name__ == "__main__":
    main()
