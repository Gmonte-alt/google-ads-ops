#!/usr/bin/env python3
"""
search_leadership_dashboard.py — Quote.com Search Leadership Dashboard.

Emits TWO workbooks per run — Home (account_id=1) and Term Life
(account_id=29) — same template, same structure, parameterized by a
single account registry (ACCOUNTS below; --account home|term-life|both).

SOURCING — differs by account, per architect rulings:
  * HOME (2026-09-02 ruling): campaign-grain NATIVE columns from
    campaign_performance_daily_search — conversions, conversion_value
    (singular), cost_micros/1e6 at device='ALL'. UI-exact (July
    three-way reconciliation).
  * TERM LIFE (2026-09-25 extension): native `conversions` != leads on
    Life (it blends offline leads + Zip Submit clicks + calls), so
    leads/revenue are ROUTED — primary_conv_count / primary_conv_value
    from keyword_performance_daily scoped via keyword_instances
    (ki.account_id=29), device='ALL'; cost stays campaign-grain. Routed
    = SQ Life Hourly Offline Conversions, the account's optimization
    target, tracking the UI Conversions column within ~1.1% in the
    hourly era. Methods note on-sheet; no caveat banner (per Gilbert's
    correction). Never mix native and routed inside one metric.

TERM-LIFE facts this report respects (architect briefing 2026-09-25):
  routed history begins 2025-06 (clean 2025-08) — irrelevant here since
  windows are trailing-30 + 6 weeks; no Sundays by ad schedule (no
  per-day denominators in this report, Mon-Sun sums unaffected);
  ai_max flags are Home-scoped → Life renders '—', never N; parsed_*
  and classification tables unused; campaign_change_events is
  Home-scoped and Google's API only reaches ~30 days back → Life's
  Previous-tROAS (H) renders '—' until the extended pipeline
  accumulates events, then is picked up automatically; Life G reads
  the campaigns_search.target_roas snapshot (verified maintained daily
  by campaign 02) — a NULL target on an ENABLED Life campaign triggers
  an escalate warning, never improvisation.

SCALE GATES (Life only, architect-endorsed): trailing-30 spend, routed
leads, ROAS, and enabled-campaign count are checked against the
briefing's expected ranges; any failure prints a DO-NOT-SEND /
escalate banner (the workbook is still written for diagnosis). The
first Life generation goes to the architect for gate review before
leadership sees it.

ROW SET (architect ruling, 2026-09-04): driven by performance-in-window,
never by status — DISTINCT campaigns with impressions>0 OR cost>0 at
device='ALL' in the trailing-30 window, then annotated from
campaigns_search. Paused-mid-window campaigns keep their spend, with a
PAUSED marker (col F) and greyed name. The same served-roster drives
every panel.

LAYOUT (identical for both accounts):
  A  Campaign Name        (greyed when PAUSED)
  B  Ad Groups            — ad groups that SERVED in trailing 30
  C  Conv/Mo              — trailing-30 leads (Home: native,
                            placeholder definition; Life: routed,
                            confirmed)
  D  Has AI Max (y/n)     — Y / N; NULL renders '?' on Home
                            (between-syncs), '—' on Life (flag sync is
                            Home-scoped)
  E  LP Version           — per-account config CSV
                            (dashboard_config_home.csv /
                            dashboard_config_life.csv; legacy
                            dashboard_config.csv migrates to the Home
                            file; skeletons generated on first run)
  F  (open) — 'PAUSED' marker only, else empty
  G  Current tROAS        — change-history latest target + start date;
                            snapshot '(current)' fallback
  H  Previous tROAS       — prior target + its start date; '—' on Life
                            until change history covers acct 29
  I  Actual ROAS, Last 30 Days        J..O  Last Week … 6 Weeks Ago
  P  GP Earned,  Last 30 Days         Q..V  Last Week … 6 Weeks Ago

Weeks are the six most recent COMPLETE Mon-Sun weeks at the anchor
(anchor = per-account MAX(date)); 30-day windows end at the anchor.
ROAS = revenue / cost; GP = revenue − cost; zero-cost periods blank.

USAGE
    python search_leadership_dashboard.py                # both accounts
    python search_leadership_dashboard.py --account home
    python search_leadership_dashboard.py --account term-life
    --db-path / --anchor-date / --output-dir
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
LEGACY_CONFIG = SCRIPT_DIR / "dashboard_config.csv"
N_WEEKS = 6

# Account registry — the ONLY place account identity lives. Every query
# takes its scoping from here; filenames are stamped with `label`.
ACCOUNTS = {
    "home": {
        "account_id": 1,
        "label": "home",
        "display": "Home",
        "lead_source": "native",   # cpd_search conversions/conversion_value
        "week_closes_dow": 6,      # Mon-Sun weeks (Mon=0..Sun=6)
        "week_note": "",
        "ai_null": "?",            # NULL = created between AI Max syncs
        "config_name": "dashboard_config_home.csv",
        "h_placeholder": "",
        "scale_gates": None,
        "pmax_note": ("PMax (PERFORMANCE_MAX) campaigns are excluded by "
                      "the account's _search reporting architecture."),
        "methods_note": ("Leads/revenue/cost are campaign-grain native "
                         "columns and match the Google Ads UI."),
        "convmo_note": ("Conv/Mo = trailing-30-day leads (placeholder "
                        "definition pending clarification)."),
    },
    "term-life": {
        "account_id": 29,
        "label": "term-life",
        "display": "Term Life",
        "lead_source": "routed",   # kpd primary_conv_* via keyword_instances
        "week_closes_dow": 5,      # Mon-SAT weeks: the ad schedule serves
                                   # no Sundays, so a week is complete at
                                   # end of Saturday
        "week_note": (" Weeks display Mon-Sat: the account's ad schedule "
                      "serves no Sundays, so a week is complete at end of "
                      "Saturday."),
        "ai_null": "—",            # AI Max flag sync is Home-scoped
        "config_name": "dashboard_config_life.csv",
        "h_placeholder": "—",      # no dated event → '—', never a proxy date
        "scale_gates": {           # architect briefing, trailing-30 vs ~monthly
            "spend": (80_000, 180_000),
            "leads": (1_400, 3_300),
            "roas": (0.90, 1.45),
            "enabled_campaigns": (3, 8),
        },
        "pmax_note": ("The account's 3 paused PMax campaigns are "
                      "excluded automatically by the _search reporting "
                      "architecture."),
        "methods_note": ("Leads = SQ Life Hourly Offline Conversions "
                         "(routed primary conversions) — the account's "
                         "optimization target; revenue is the routed "
                         "conversion value; cost is campaign-grain. To "
                         "reconcile against a UI export, segment "
                         "Conversions by action and match that line."),
        "convmo_note": "Conv/Mo = trailing-30-day routed leads.",
    },
}

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


def build_windows(anchor: date, close_dow: int = 6):
    """Trailing-30 + six most recent complete weeks.

    close_dow is the weekday (Mon=0..Sun=6) on which the account's
    serving week CLOSES: 6 (Sunday) for Home, 5 (Saturday) for Term
    Life, whose ad schedule serves no Sundays. A week is complete once
    its last SERVED day has passed — judging completeness by Sunday on
    a no-Sundays account would leave "Last Week" perpetually one week
    stale, because the anchor (MAX data date) can never land on a
    Sunday there. Weeks still start Monday; the range simply ends on
    the close day, so Life weeks render Mon-Sat.
    """
    t30 = (anchor - timedelta(days=29), anchor)
    last_close = anchor - timedelta(days=(anchor.weekday() - close_dow) % 7)
    weeks = []
    for k in range(N_WEEKS):
        close = last_close - timedelta(weeks=k)
        weeks.append((close - timedelta(days=close_dow), close))
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


def fetch_routed_leads(conn, camp_ids, account_id, a, b, warnings):
    """Life leads/revenue: routed primary conversions from kpd, scoped
    via keyword_instances (ki.account_id) per the broadcast convention,
    device='ALL'. Returns {campaign_id: (conv, rev)}."""
    kpd_cols = table_columns(conn, "keyword_performance_daily")
    d_col = _pick(kpd_cols, "report_date", "date")
    ph = ",".join("?" * len(camp_ids))
    if "instance_id" in kpd_cols:
        sql = (f"SELECT kpd.campaign_id, SUM(kpd.primary_conv_count), "
               f"SUM(kpd.primary_conv_value) "
               f"FROM keyword_performance_daily kpd "
               f"JOIN keyword_instances ki ON ki.id = kpd.instance_id "
               f"WHERE ki.account_id = ? AND kpd.device = 'ALL' "
               f"AND kpd.campaign_id IN ({ph}) "
               f"AND kpd.{d_col} BETWEEN ? AND ? "
               f"GROUP BY kpd.campaign_id")
    else:
        warnings.append("keyword_performance_daily has no instance_id — "
                        "routed leads scoped via campaigns join instead "
                        "of keyword_instances (flag to architect)")
        sql = (f"SELECT kpd.campaign_id, SUM(kpd.primary_conv_count), "
               f"SUM(kpd.primary_conv_value) "
               f"FROM keyword_performance_daily kpd "
               f"JOIN campaigns c ON c.campaign_id = kpd.campaign_id "
               f"WHERE c.account_id = ? AND kpd.device = 'ALL' "
               f"AND kpd.campaign_id IN ({ph}) "
               f"AND kpd.{d_col} BETWEEN ? AND ? "
               f"GROUP BY kpd.campaign_id")
    rows = q1(conn, sql, (account_id, *camp_ids,
                          a.isoformat(), b.isoformat()))
    return {cid: (conv or 0.0, rev or 0.0) for cid, conv, rev in rows}


def run_account(conn, key, cfg, args, ca_cols, has_ai, has_bundling):
    """Generate one account's workbook. Returns the output path."""
    account_id = cfg["account_id"]
    print(f"\n══ {cfg['display']} (account_id={account_id}) ══")

    row = conn.execute(
        "SELECT MAX(date) FROM campaign_performance_daily_search "
        "WHERE account_id = ? AND device = 'ALL'", (account_id,)
    ).fetchone()
    if not row or not row[0]:
        raise SystemExit(f"ERROR: no cpd_search rows for account "
                         f"{account_id} ({cfg['display']})")
    anchor = (date.fromisoformat(args.anchor_date) if args.anchor_date
              else date.fromisoformat(str(row[0])[:10]))
    t30, weeks = build_windows(anchor, cfg.get("week_closes_dow", 6))
    windows = [("Last 30 Days", *t30)] + [
        ("Last Week" if k == 0 else f"{k + 1} Weeks Ago", a, b)
        for k, (a, b) in enumerate(weeks)]
    print(f"Anchor {anchor} | 30d {t30[0]} → {t30[1]} | "
          f"weeks {weeks[-1][0]} → {weeks[0][1]}")

    # ── Served roster (drives every panel; never status-filtered) ──
    null_tok = cfg["ai_null"]
    ai_sel = (f"CASE ca.ai_max_enabled WHEN 1 THEN 'Y' WHEN 0 THEN 'N' "
              f"ELSE '{null_tok}' END" if has_ai else f"'{null_tok}'")
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
    """, (account_id, t30[0].isoformat(), t30[1].isoformat(), account_id))
    if not roster:
        raise SystemExit(f"ERROR: served roster is empty for "
                         f"{cfg['display']}")
    n_enabled = sum(1 for r in roster if r[2] == "ENABLED")
    print(f"Roster: {len(roster)} campaigns "
          f"({len(roster) - n_enabled} not ENABLED)")

    camp_ids = [r[0] for r in roster]
    troas_snapshot = {r[0]: r[5] for r in roster if r[5] is not None}
    ph = ",".join("?" * len(camp_ids))

    # ── Ad groups that served in trailing 30 ───────────────────────
    agp_cols = table_columns(conn, "ad_group_performance_daily")
    ag_dev = "AND device = 'ALL'" if "device" in agp_cols else ""
    ag_date = "date" if "date" in agp_cols else "report_date"
    ad_groups = dict(q1(conn, f"""
        SELECT campaign_id, COUNT(DISTINCT ad_group_id)
        FROM ad_group_performance_daily
        WHERE campaign_id IN ({ph}) {ag_dev}
          AND {ag_date} BETWEEN ? AND ? AND impressions > 0
        GROUP BY campaign_id
    """, (*camp_ids, t30[0].isoformat(), t30[1].isoformat())))

    # ── Per-window metrics ─────────────────────────────────────────
    # cost always campaign-grain; leads/revenue by cfg["lead_source"].
    routed_warnings = []
    metrics = {}  # (campaign_id, window_idx) -> (cost, conv, rev)
    acct = {}     # window_idx -> [cost, conv, rev]
    for wi, (_lbl, a, b) in enumerate(windows):
        cost_rows = q1(conn, f"""
            SELECT campaign_id, SUM(cost_micros) / 1.0e6,
                   SUM(conversions), SUM(conversion_value)
            FROM campaign_performance_daily_search
            WHERE account_id = ? AND device = 'ALL'
              AND campaign_id IN ({ph})
              AND date BETWEEN ? AND ?
            GROUP BY campaign_id
        """, (account_id, *camp_ids, a.isoformat(), b.isoformat()))
        if cfg["lead_source"] == "routed":
            routed = fetch_routed_leads(conn, camp_ids, account_id,
                                        a, b, routed_warnings)
        acct[wi] = [0.0, 0.0, 0.0]
        for cid, cost, n_conv, n_rev in cost_rows:
            if cfg["lead_source"] == "native":
                conv, rev = n_conv or 0.0, n_rev or 0.0
            else:
                conv, rev = routed.get(cid, (0.0, 0.0))
            metrics[(cid, wi)] = (cost or 0.0, conv, rev)
            acct[wi][0] += cost or 0.0
            acct[wi][1] += conv
            acct[wi][2] += rev
    for w in dict.fromkeys(routed_warnings):
        print(f"  ⚠ {w}")

    # ── tROAS current/previous targets (cols G/H) ──────────────────
    troas_gh, troas_warnings = fetch_troas_targets(
        conn, camp_ids, account_id, troas_snapshot)
    if cfg["h_placeholder"]:
        troas_gh = {cid: (g, h or cfg["h_placeholder"])
                    for cid, (g, h) in troas_gh.items()}
    for w in troas_warnings:
        print(f"  ⚠ {w}")
    for cid, name, status, *_ in roster:
        if status == "ENABLED" and not troas_gh.get(cid, ("", ""))[0]:
            print(f"  ⚠ ESCALATE: ENABLED campaign {cid} ({name[:40]}) "
                  f"has no tROAS target from change history OR snapshot "
                  f"— send back to the DB architect before improvising.")

    # ── LP-version config (manual column E) ────────────────────────
    cfg_path = SCRIPT_DIR / cfg["config_name"]
    if (key == "home" and not cfg_path.exists() and LEGACY_CONFIG.exists()):
        cfg_path.write_text(LEGACY_CONFIG.read_text(encoding="utf-8"),
                            encoding="utf-8")
        print(f"Config: migrated legacy {LEGACY_CONFIG.name} → "
              f"{cfg_path.name}")
    lp = {}
    if cfg_path.exists():
        with open(cfg_path, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                try:
                    lp[int(r["campaign_id"])] = (r.get("lp_version") or "").strip()
                except (KeyError, ValueError):
                    continue
        print(f"Config: {cfg_path.name} — LP versions for "
              f"{sum(1 for v in lp.values() if v)} campaigns")
    else:
        with open(cfg_path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["campaign_id", "campaign_name", "lp_version"])
            for cid, name, *_ in roster:
                w.writerow([cid, name, ""])
        print(f"Config: {cfg_path.name} not found — skeleton generated "
              f"with {len(roster)} campaigns; fill lp_version and re-run.")

    # ══ Workbook ═══════════════════════════════════════════════════
    wb = Workbook()
    ws = wb.active
    ws.title = cfg["display"].replace(" ", "_")

    ws.cell(row=1, column=1,
            value=f"Quote.com — Search Leadership Dashboard "
                  f"({cfg['display']})").font = TITLE_FONT
    src_line = ("campaign-grain native metrics (matches Google Ads UI)"
                if cfg["lead_source"] == "native"
                else "routed leads/revenue (SQ Life Hourly Offline "
                     "Conversions); campaign-grain cost")
    ws.cell(row=2, column=1, value=(
        f"Generated {date.today()} | data through {anchor} | {src_line}"
        )).font = NOTE_FONT

    for col, title in ((ROAS_TOTAL_COL, "Actual ROAS"),
                       (GP_TOTAL_COL, "Gross Profit Earned")):
        ws.cell(row=HEADER_ROW_PANEL, column=col, value=title).font = PANEL_FONT
    ws.cell(row=HEADER_ROW_PANEL, column=1, value="Quote.com").font = PANEL_FONT

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
                    "", "Current tROAS", "Previous tROAS"]
    for j, h in enumerate(left_headers, 1):
        c = ws.cell(row=HEADER_ROW_DATES, column=j, value=h)
        if h:
            c.fill = HEADER_FILL
            c.font = HEADER_FONT
            c.alignment = Alignment(horizontal="center", wrap_text=True)

    r = FIRST_DATA_ROW
    for cid, name, status, ai_yn, bundling, _snap in roster:
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
        g_text, h_text = troas_gh.get(cid, ("", cfg["h_placeholder"]))
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

    # ── Footnotes (per account) ────────────────────────────────────
    troas_note = ("Current/Previous tROAS come from the change-history "
                  "model with target start dates; '≤ date' = in effect "
                  "since at least tracking start; '(current)' = snapshot "
                  "value with no change history"
                  + ("; '—' = no dated change event recorded for this "
                     "campaign (dates are as fresh as the weekly "
                     "grid/API history cadence)."
                     if cfg["h_placeholder"] else "."))
    ws.cell(row=r + 2, column=1, value=(
        "Notes: (1) Conversions continue to land for 2-3 days after the "
        "click — the most recent days restate upward; windows are kept "
        "UI-aligned by design." + cfg.get("week_note", "") + " (2) " + cfg["pmax_note"] + " (3) "
        + cfg["convmo_note"] + " (4) " + cfg["methods_note"]
        + " PAUSED rows served during the window and are annotated with "
        "current status. (5) " + troas_note)).font = NOTE_FONT

    widths = {1: 46, 2: 9, 3: 8, 4: 13, 5: 24, 6: 9, 7: 19, 8: 19}
    for c, w in widths.items():
        ws.column_dimensions[get_column_letter(c)].width = w
    for c in range(ROAS_TOTAL_COL, GP_TOTAL_COL + len(windows)):
        ws.column_dimensions[get_column_letter(c)].width = 11
    ws.freeze_panes = f"B{FIRST_DATA_ROW}"

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / (f"Search_Leadership_Dashboard_{cfg['label']}_"
                     f"{anchor.isoformat()}.xlsx")
    wb.save(out)
    print(f"  ✓ Wrote {out}")

    cost, conv, rev = acct[0]
    roas30 = rev / cost if cost else 0.0
    print(f"Last 30: cost ${cost:,.0f} | leads {conv:,.0f} | "
          f"rev ${rev:,.0f} | ROAS {roas30:.2f} | GP ${rev - cost:,.0f}")

    # ── Scale gates (Life) ─────────────────────────────────────────
    gates = cfg.get("scale_gates")
    if gates:
        checks = [("trailing-30 spend", cost, gates["spend"]),
                  ("trailing-30 routed leads", conv, gates["leads"]),
                  ("trailing-30 ROAS", roas30, gates["roas"]),
                  ("enabled campaigns", n_enabled,
                   gates["enabled_campaigns"])]
        failures = []
        for label, val, (lo, hi) in checks:
            ok = lo <= val <= hi
            print(f"  [scale gate] {label}: "
                  f"{val:,.2f} vs [{lo:g}, {hi:g}] "
                  f"{'OK' if ok else 'FAIL'}")
            if not ok:
                failures.append(label)
        if failures:
            print("  " + "!" * 62)
            print(f"  !! DO NOT SEND — {cfg['display']} toplines outside "
                  f"expected ranges ({', '.join(failures)}).")
            print("  !! Stop and escalate to the DB architect thread; "
                  "the workbook was written for diagnosis only.")
            print("  " + "!" * 62)
    return out


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db-path", default=str(DEFAULT_DB_PATH))
    p.add_argument("--account", choices=[*ACCOUNTS, "both"], default="both",
                   help="Which workbook(s) to emit (default: both)")
    p.add_argument("--anchor-date", default=None,
                   help="YYYY-MM-DD; default = per-account MAX(date)")
    p.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    args = p.parse_args()

    db = Path(args.db_path)
    if not db.exists():
        raise SystemExit(f"ERROR: DB not found at {db}")
    conn = sqlite3.connect(db)

    cpd_cols = table_columns(conn, "campaign_performance_daily_search")
    for col in ("date", "device", "impressions", "cost_micros",
                "conversions", "conversion_value"):
        if col not in cpd_cols:
            raise SystemExit(
                f"ERROR: campaign_performance_daily_search lacks '{col}' "
                f"(columns: {cpd_cols}) — check with the DB architect.")
    ca_cols = table_columns(conn, "campaigns_search")
    has_ai = "ai_max_enabled" in ca_cols
    has_bundling = "ai_max_bundling" in ca_cols
    if not has_ai:
        print("  ⚠ campaigns_search.ai_max_enabled not found — Has AI Max "
              "will render its null token throughout (is the AI Max sync "
              "line running?)")

    keys = list(ACCOUNTS) if args.account == "both" else [args.account]
    for key in keys:
        run_account(conn, key, ACCOUNTS[key], args,
                    ca_cols, has_ai, has_bundling)
    conn.close()


if __name__ == "__main__":
    main()
