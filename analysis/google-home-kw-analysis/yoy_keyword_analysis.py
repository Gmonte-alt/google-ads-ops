#!/usr/bin/env python3
"""
yoy_keyword_analysis.py

Year-over-year keyword-IDENTITY performance analysis for the Google Home
Insurance account (account_id = 1) in paid_search.db.

Compares matched calendar windows (default: Apr 1 -> yesterday, both years) at
the keyword_identity level -- NOT keyword_instance and NOT campaign level --
because the 2025->2026 per-state->tier restructure makes campaign grouping the
wrong unit of comparison. Keyword text/match_type/platform survived the
restructure; campaigns did not.

Read-only. No INSERT/UPDATE. Connects to a local SQLite DB.

Workflow:
    1.  python yoy_keyword_analysis.py --inspect-schema
        -> prints PRAGMA table_info for the key tables, then exits.
           Use this to confirm the join columns in CONFIG below before a real run.
    2.  python yoy_keyword_analysis.py
        -> reconciles spend, prints the top-20-by-2025-spend validation table,
           and PAUSES for confirmation before building the workbook.
    3.  python yoy_keyword_analysis.py --skip-validation
        -> runs end to end without the pause.

Author: keyword analyst thread
"""

import argparse
import re
import sqlite3
import sys
from datetime import date, timedelta

try:
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter
except ImportError:
    sys.exit("openpyxl is required:  pip install openpyxl")


# ---------------------------------------------------------------------------
# CONFIG -- join/column names live here so they can be corrected in ONE place
# after `--inspect-schema`. These are the *assumed* mappings; verify them.
# ---------------------------------------------------------------------------
CONFIG = {
    # keyword_performance_daily (kpd) -- CONFIRMED via --inspect-schema
    "kpd_table":            "keyword_performance_daily",
    "kpd_instance_col":     "instance_id",     # FK -> keyword_instances.id  (THE join)
    "kpd_campaign_col":     "campaign_id",      # TEXT -> campaigns.campaign_id (recon only)
    "kpd_date_col":         "report_date",
    "kpd_device_col":       "device",
    "kpd_impr_col":         "impressions",
    "kpd_clicks_col":       "clicks",
    "kpd_cost_col":         "cost_micros",
    "kpd_pconv_count_col":  "primary_conv_count",
    "kpd_pconv_value_col":  "primary_conv_value",
    # NOTE: kpd has NO account_id column. Aggregation scopes via ki.account_id
    # (we join ki anyway); the pre-join recon total scopes via campaigns.

    # campaigns -- CONFIRMED: PK is campaign_id (TEXT)
    "campaigns_table":       "campaigns",
    "campaigns_id_col":      "campaign_id",
    "campaigns_account_col": "account_id",

    # keyword_instances (ki) -- CONFIRMED
    "ki_table":             "keyword_instances",
    "ki_id_col":            "id",              # PK that kpd.instance_id points to
    "ki_account_col":       "account_id",      # scope Google Home = 1
    "ki_identity_col":      "identity_id",     # FK -> keyword_identities.id
    "ki_status_col":        "status",
    "ki_lastseen_col":      "last_seen_date",
    "ki_firstseen_col":     "first_seen_date",

    # keyword_identities (kid) -- CONFIRMED: NO platform column (has `vertical`)
    "kid_table":            "keyword_identities",
    "kid_id_col":           "id",
    "kid_text_col":         "normalized_text",
    "kid_match_col":        "match_type",
    "kid_brandcat_col":     "brand_category",
    "kid_intenttype_col":   "intent_type",
    "kid_intentaction_col": "intent_action",
    "kid_modbrand_col":     "modifier_brand",

    # campaign_performance_daily_search (recon only) -- CONFIRMED: date col is `date`,
    # has account_id directly.
    "cpd_table":            "campaign_performance_daily_search",
    "cpd_date_col":         "date",
    "cpd_device_col":       "device",
    "cpd_cost_col":         "cost_micros",
    "cpd_account_col":      "account_id",
}

ACCOUNT_ID = 1
ACTIVE_RECENCY_DAYS = 14          # ki must be seen within this many days to count as active
# --- Reconciliation gates ---------------------------------------------------
# JOIN-LOSS is the integrity check that actually governs the identity analysis:
# kpd spend that fails to join to keyword_instances is silently dropped.
JOIN_LOSS_WARN_PCT = 0.5          # auto-run orphan diagnostic if join-loss exceeds this
#
# kpd-vs-cpd is NOT an equality checksum. Keyword spend (kpd) is a SUBSET of
# campaign spend (cpd); the gap = non-keyword spend (DSA dynamic targets, PMax,
# audience/network expansion, search partners). Expected relationship: cpd >= kpd.
RECON_GAP_WARN_PCT = 0.5          # cpd-minus-kpd gap above this -> warn + per-campaign diagnostic
RECON_GAP_FAIL_PCT = 15.0         # gap this large suggests we're comparing the wrong universes -> abort
RECON_SIGN_TOL_PCT = 0.5          # kpd exceeding cpd by more than this is impossible for a subset -> abort

# Default per-identity inclusion thresholds (applied to the *2025* window for
# the YoY table; applied to the *2026* window for the New Keywords list).
DEFAULT_MIN_IMPRESSIONS = 1000
DEFAULT_MIN_SPEND = 100.0
DEFAULT_MIN_CONVERSIONS = 1

# "Disappeared" = meaningful 2025 + zero 2026 impressions
DISAPPEARED_MIN_SPEND = 500.0
DISAPPEARED_MIN_CONV = 5
DISAPPEARED_MIN_REVENUE = 1000.0

# "Faded" = still enabled, >=50% drop in impr OR conv, and 2025 spend >= 500
FADED_DROP_PCT = 0.50
FADED_MIN_2025_SPEND = 500.0

INSPECT_TABLES = [
    "keyword_identities", "keyword_instances", "keyword_performance_daily",
    "campaign_performance_daily_search", "campaigns", "ad_groups", "accounts",
]


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------
def safe_div(num, den):
    if den in (None, 0, 0.0):
        return None
    if num is None:
        return None
    return num / den


def pct_change(old, new):
    """Returns (abs_delta, pct_delta). pct is None when base is 0/None."""
    o = old or 0.0
    n = new or 0.0
    abs_d = n - o
    if o == 0:
        return abs_d, None        # undefined / "new from zero"
    return abs_d, (n - o) / o


def fmt_pct(p):
    return None if p is None else round(p * 100.0, 1)


def derive_windows(args):
    """Resolve the four window dates. Defaults: Apr 1 -> yesterday, matched."""
    yest = date.today() - timedelta(days=1)

    def parse(s):
        y, m, d = (int(x) for x in s.split("-"))
        return date(y, m, d)

    if args.window_2026_start:
        s26 = parse(args.window_2026_start)
    else:
        s26 = date(yest.year, 4, 1)
    if args.window_2026_end:
        e26 = parse(args.window_2026_end)
    else:
        e26 = yest
    if args.window_2025_start:
        s25 = parse(args.window_2025_start)
    else:
        s25 = date(s26.year - 1, 4, 1)
    if args.window_2025_end:
        e25 = parse(args.window_2025_end)
    else:
        # mirror 2026 end into prior year; guard Feb 29
        try:
            e25 = date(e26.year - 1, e26.month, e26.day)
        except ValueError:
            e25 = date(e26.year - 1, e26.month, e26.day - 1)

    return (s25.isoformat(), e25.isoformat(),
            s26.isoformat(), e26.isoformat())


# ---------------------------------------------------------------------------
# schema inspection
# ---------------------------------------------------------------------------
def inspect_schema(conn):
    cur = conn.cursor()
    for t in INSPECT_TABLES:
        print(f"\n===== {t} =====")
        try:
            for row in cur.execute(f"PRAGMA table_info({t})").fetchall():
                # cid, name, type, notnull, dflt_value, pk
                print(f"  {row[1]:<28} {row[2]:<12} pk={row[5]}")
        except sqlite3.Error as e:
            print(f"  (could not read: {e})")
    print("\nPaste the above back to the analyst to confirm the CONFIG joins.")


# ---------------------------------------------------------------------------
# SQL building
# ---------------------------------------------------------------------------
def identity_agg_sql(c):
    """Per keyword_identity aggregation for a single window.
    Join: kpd.instance_id -> keyword_instances.id ; scope via ki.account_id."""
    return f"""
        SELECT
            ki.{c['ki_identity_col']}                       AS identity_id,
            SUM(kpd.{c['kpd_impr_col']})                    AS impressions,
            SUM(kpd.{c['kpd_clicks_col']})                  AS clicks,
            SUM(kpd.{c['kpd_cost_col']}) / 1000000.0        AS spend,
            SUM(kpd.{c['kpd_pconv_count_col']})             AS conv,
            SUM(kpd.{c['kpd_pconv_value_col']})             AS revenue
        FROM {c['kpd_table']} kpd
        JOIN {c['ki_table']} ki
              ON ki.{c['ki_id_col']} = kpd.{c['kpd_instance_col']}
        WHERE kpd.{c['kpd_device_col']} = 'ALL'
          AND kpd.{c['kpd_date_col']} >= ?
          AND kpd.{c['kpd_date_col']} <= ?
          AND ki.{c['ki_account_col']} = ?
        GROUP BY ki.{c['ki_identity_col']}
    """


def kpd_total_spend_sql(c):
    """Total kpd spend for the account/window BEFORE the instance join.
    Scoped via campaigns so it does not depend on the instance FK."""
    return f"""
        SELECT SUM(kpd.{c['kpd_cost_col']}) / 1000000.0
        FROM {c['kpd_table']} kpd
        JOIN {c['campaigns_table']} camp
              ON camp.{c['campaigns_id_col']} = kpd.{c['kpd_campaign_col']}
        WHERE kpd.{c['kpd_device_col']} = 'ALL'
          AND kpd.{c['kpd_date_col']} >= ?
          AND kpd.{c['kpd_date_col']} <= ?
          AND camp.{c['campaigns_account_col']} = ?
    """


def identity_joined_spend_sql(c):
    """kpd spend that survives the instance join (i.e. enters the analysis)."""
    return f"""
        SELECT SUM(kpd.{c['kpd_cost_col']}) / 1000000.0
        FROM {c['kpd_table']} kpd
        JOIN {c['ki_table']} ki
              ON ki.{c['ki_id_col']} = kpd.{c['kpd_instance_col']}
        WHERE kpd.{c['kpd_device_col']} = 'ALL'
          AND kpd.{c['kpd_date_col']} >= ?
          AND kpd.{c['kpd_date_col']} <= ?
          AND ki.{c['ki_account_col']} = ?
    """


def cpd_total_spend_sql(c):
    """campaign_performance_daily_search spend for the same scope/window.
    cpd carries account_id directly; no campaigns join needed."""
    return f"""
        SELECT SUM(cpd.{c['cpd_cost_col']}) / 1000000.0
        FROM {c['cpd_table']} cpd
        WHERE cpd.{c['cpd_device_col']} = 'ALL'
          AND cpd.{c['cpd_date_col']} >= ?
          AND cpd.{c['cpd_date_col']} <= ?
          AND cpd.{c['cpd_account_col']} = ?
    """


def identity_meta_sql(c):
    """All Home identities + tags + active-status flag, scoped via ki.account_id.
    keyword_identities has no platform column; Home scope comes from instances."""
    return f"""
        SELECT
            kid.{c['kid_id_col']}            AS identity_id,
            kid.{c['kid_text_col']}          AS keyword_text,
            kid.{c['kid_match_col']}         AS match_type,
            kid.{c['kid_brandcat_col']}      AS brand_category,
            kid.{c['kid_intenttype_col']}    AS intent_type,
            kid.{c['kid_intentaction_col']}  AS intent_action,
            kid.{c['kid_modbrand_col']}      AS modifier_brand,
            MAX(CASE
                    WHEN ki.{c['ki_status_col']} = 'ENABLED'
                     AND ki.{c['ki_lastseen_col']} >= date('now', '-{ACTIVE_RECENCY_DAYS} days')
                    THEN 1 ELSE 0
                END)                          AS currently_active,
            MAX(ki.{c['ki_lastseen_col']})    AS last_seen_date
        FROM {c['kid_table']} kid
        JOIN {c['ki_table']} ki
              ON ki.{c['ki_identity_col']} = kid.{c['kid_id_col']}
             AND ki.{c['ki_account_col']} = ?
        GROUP BY kid.{c['kid_id_col']}
    """


# ---------------------------------------------------------------------------
# data loading
# ---------------------------------------------------------------------------
def load_agg(conn, sql, params):
    cur = conn.cursor()
    out = {}
    for r in cur.execute(sql, params).fetchall():
        out[r[0]] = {
            "impressions": r[1] or 0,
            "clicks": r[2] or 0,
            "spend": r[3] or 0.0,
            "conv": r[4] or 0.0,
            "revenue": r[5] or 0.0,
        }
    return out


def load_meta(conn, c):
    cur = conn.cursor()
    out = {}
    for r in cur.execute(identity_meta_sql(c), [ACCOUNT_ID]).fetchall():
        out[r[0]] = {
            "keyword_text": r[1],
            "match_type": r[2],
            "brand_category": r[3] or "(untagged)",
            "intent_type": r[4] or "(untagged)",
            "intent_action": r[5] or "(untagged)",
            "modifier_brand": r[6],
            "currently_active": bool(r[7]),
            "last_seen_date": r[8],
        }
    return out


def scalar(conn, sql, params):
    v = conn.cursor().execute(sql, params).fetchone()[0]
    return v or 0.0


def normalize_brand(s):
    """Mirror brand_registry's normalize(): lowercase, strip, collapse whitespace.
    Must match 07_seed_brand_registry.py exactly so modifier_brand joins cleanly."""
    if not s:
        return None
    return re.sub(r"\s+", " ", str(s).lower().strip())


def load_brand_registry(conn):
    """Load brand_registry as {normalized_name: {bid_status, brand_category,
    restriction_reason, restriction_source, verticals}}. Returns ({}, False) if
    the table is absent (older DB) so the analysis degrades gracefully."""
    try:
        rows = conn.cursor().execute("""
            SELECT normalized_name, bid_status, brand_category,
                   restriction_reason, restriction_source, verticals
            FROM brand_registry
        """).fetchall()
    except sqlite3.Error:
        return {}, False
    reg = {}
    for nn, bid, cat, reason, src, verts in rows:
        if nn:
            reg[nn] = {
                "bid_status": bid,
                "brand_category": cat,
                "restriction_reason": reason,
                "restriction_source": src,
                "verticals": verts,
            }
    return reg, True


# Maps brand_registry.bid_status -> the actionable biddability bucket the
# architect wants on the Disappeared sheet.
BIDDABILITY_BY_STATUS = {
    "restricted":  "non_biddable",      # Google/trademark enforcement — unrecoverable
    "conditional": "conditional",       # biddable now, QW may pull at any time
    "approved":    "biddable",          # cleared — a real candidate to re-add if cut
}

# Operational override (Gilbert, 2026-06): brands we cannot bid on that the
# brand_registry has marked 'approved' in error. ADDITIVE to the registry's own
# restrictions (USAA, Liberty Mutual, Safeco, Homesite, Progressive, Allstate,
# etc. remain restricted via BRAND_DATA). Matched as a token within the
# normalized modifier_brand. This is a stopgap until the registry is corrected.
NON_BIDDABLE_OVERRIDE = ("lemonade", "tgs")


def annotate_biddability(row, registry, registry_present):
    """Attach bid_status / biddability / restriction_reason to a row by looking
    up its modifier_brand against the registry. Honest about misses: a NULL tag
    and an untagged-brand miss are distinct from a real 'biddable' answer."""
    mb = normalize_brand(row.get("modifier_brand"))
    # Operational override first — applies regardless of registry availability.
    if mb and any(tok in mb for tok in NON_BIDDABLE_OVERRIDE):
        row["bid_status"] = "restricted"
        row["biddability"] = "non_biddable"
        row["restriction_reason"] = "operational override (not on registry no-bid list)"
        return
    if not registry_present:
        row["bid_status"] = None
        row["biddability"] = "registry_unavailable"
        row["restriction_reason"] = None
        return
    if mb is None:
        row["bid_status"] = None
        row["biddability"] = "no_brand_tag"
        row["restriction_reason"] = None
        return
    hit = registry.get(mb)
    if hit is None:
        row["bid_status"] = None
        row["biddability"] = "brand_not_in_registry"
        row["restriction_reason"] = None
        return
    row["bid_status"] = hit["bid_status"]
    row["biddability"] = BIDDABILITY_BY_STATUS.get(hit["bid_status"], hit["bid_status"])
    row["restriction_reason"] = hit["restriction_reason"]


# ---------------------------------------------------------------------------
# row assembly
# ---------------------------------------------------------------------------
def metric_block(d):
    spend = d["spend"]
    clicks = d["clicks"]
    conv = d["conv"]
    rev = d["revenue"]
    return {
        "impressions": d["impressions"],
        "clicks": clicks,
        "spend": round(spend, 2),
        "conv": round(conv, 2),
        "revenue": round(rev, 2),
        "cpc": _r(safe_div(spend, clicks)),
        "cvr": _r(safe_div(conv, clicks), 4),
        "rpv": _r(safe_div(rev, clicks)),
        "cpa": _r(safe_div(spend, conv)),
        "roas": _r(safe_div(rev, spend), 3),
    }


def _r(v, nd=2):
    return None if v is None else round(v, nd)


def classify(meta, m25, m26, instances_stale):
    """status_class for the per-identity table.

    Observation-first: a keyword that delivered impressions in 2026 IS serving,
    regardless of what the keyword_instances flag says. Only fall back to the
    instance flag for keywords with zero 2026 delivery -- and only trust that
    flag when the instances table is actually fresh. When the structure sync is
    stale, we cannot tell a deliberate cut from 'enabled but dark', so we report
    the observation ('no_2026_delivery') instead of asserting a cause."""
    impr26 = m26["impressions"]
    if impr26 > 0:
        # demonstrably serving -> never 'removed'; stable vs underperforming on deltas
        _, conv_pct = pct_change(m25["conv"], m26["conv"])
        _, spend_pct = pct_change(m25["spend"], m26["spend"])
        if (conv_pct is not None and conv_pct <= -FADED_DROP_PCT) or \
           (spend_pct is not None and spend_pct <= -FADED_DROP_PCT):
            return "still_present_underperforming"
        return "active_stable"
    # zero 2026 delivery
    if instances_stale:
        return "no_2026_delivery"        # cause unknowable until structure sync is refreshed
    if meta["currently_active"]:
        return "active_no_impressions"   # enabled but Google serving nothing
    return "intentionally_removed"


def build_rows(meta, agg25, agg26, instances_stale):
    """One row per identity that appears in either window."""
    ids = set(agg25) | set(agg26)
    rows = []
    for iid in ids:
        m = meta.get(iid, {
            "keyword_text": f"(identity {iid} - no meta)",
            "match_type": None,
            "brand_category": "(untagged)", "intent_type": "(untagged)",
            "intent_action": "(untagged)", "modifier_brand": None,
            "currently_active": False, "last_seen_date": None,
        })
        d25 = agg25.get(iid, _zero())
        d26 = agg26.get(iid, _zero())
        b25 = metric_block(d25)
        b26 = metric_block(d26)

        row = {
            "identity_id": iid,
            "keyword_text": m["keyword_text"],
            "match_type": m["match_type"],
            "brand_category": m["brand_category"],
            "modifier_brand": m["modifier_brand"],
            "intent_type": m["intent_type"],
            "intent_action": m["intent_action"],
            "currently_active": m["currently_active"],
            "last_seen_date": m["last_seen_date"],
        }
        for k, v in b25.items():
            row[f"{k}_2025"] = v
        for k, v in b26.items():
            row[f"{k}_2026"] = v
        # deltas on the volume + efficiency metrics
        for k in ("impressions", "clicks", "spend", "conv", "revenue",
                  "cpc", "rpv", "roas", "cpa"):
            ad, pd = pct_change(b25[k], b26[k])
            row[f"{k}_delta"] = _r(ad, 2)
            row[f"{k}_delta_pct"] = fmt_pct(pd)
        row["status_class"] = classify(m, d25, d26, instances_stale)
        rows.append(row)
    return rows


def _zero():
    return {"impressions": 0, "clicks": 0, "spend": 0.0, "conv": 0.0, "revenue": 0.0}


# ---------------------------------------------------------------------------
# excel writing
# ---------------------------------------------------------------------------
HEADER_FILL = PatternFill("solid", fgColor="1F3864")
HEADER_FONT = Font(bold=True, color="FFFFFF", size=10)
THIN = Side(style="thin", color="D9D9D9")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)


def write_sheet(wb, title, columns, rows, freeze="A2"):
    ws = wb.create_sheet(title[:31])
    ws.append(columns)
    for ci, _ in enumerate(columns, 1):
        cell = ws.cell(row=1, column=ci)
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        cell.border = BORDER
    for r in rows:
        ws.append([r.get(c) for c in columns])
    ws.freeze_panes = freeze
    # rough column widths
    for ci, col in enumerate(columns, 1):
        width = min(max(len(col) + 2, 10), 40)
        ws.column_dimensions[get_column_letter(ci)].width = width
    ws.auto_filter.ref = f"A1:{get_column_letter(len(columns))}{len(rows)+1}"
    return ws


def write_readme(wb, win, recon, counts):
    ws = wb.create_sheet("README")
    ws.column_dimensions["A"].width = 110
    lines = [
        ("Google Home Insurance — YoY Keyword-Identity Analysis", True),
        (f"Generated: {date.today().isoformat()}", False),
        ("", False),
        ("WINDOWS (matched calendar spans)", True),
        (f"  2025: {win[0]} to {win[1]}", False),
        (f"  2026: {win[2]} to {win[3]}", False),
        ("  Note: spec literal said '...to 2025-05-(today-1)'. Because this was run in June,",
         False),
        ("  windows resolve dynamically to 'Apr 1 -> yesterday' mirrored across years.",
         False),
        ("", False),
        ("RECONCILIATION (2025 window, account_id=1, device='ALL')", True),
        (f"  kpd total spend                : ${recon['kpd']:,.2f}", False),
        (f"  campaign_perf_daily total spend: ${recon['cpd']:,.2f}", False),
        (f"  identity-joined spend          : ${recon['joined']:,.2f}", False),
        (f"  cpd-minus-kpd gap              : {recon['gap_pct']:+.4f}%  "
         f"(${recon['cpd'] - recon['kpd']:,.2f}) = non-keyword spend (DSA/PMax/network)",
         False),
        (f"  spend lost to instance join    : {recon['join_loss_pct']:.4f}%  "
         f"(this is the integrity check that governs identity totals)", False),
        ("", False),
        ("COUNTS", True),
        (f"  identities w/ 2025 data : {counts['n25']}", False),
        (f"  identities w/ 2026 data : {counts['n26']}", False),
        (f"  disappeared             : {counts['disappeared']}", False),
        (f"  faded                   : {counts['faded']}", False),
        (f"  new                     : {counts['new']}", False),
        ("", False),
        ("METHODOLOGY", True),
        ("  Unit of comparison is keyword_identity (normalized_text + match_type + platform),",
         False),
        ("  NOT keyword_instance and NOT campaign. No campaign filter is applied.", False),
        ("  Routed conversions only (primary_conv_count / primary_conv_value).", False),
        ("  device='ALL' rows read directly (pre-aggregated).", False),
        ("", False),
        ("CONFOUNDS — read before sharing", True),
        ("  1. 2025->2026 per-state -> tier restructure changed per-state budget allocation;",
         False),
        ("     keyword shifts may reflect budget reallocation, not demand.", False),
        ("  2. A keyword-reduction exercise ran during the restructure. 'Disappeared' keywords",
         False),
        ("     have THREE causes, split by the 'biddability' column (from brand_registry):", False),
        ("       - non_biddable  : Google/trademark enforcement (e.g. USAA, Liberty Mutual) —", False),
        ("                         NOT cut by us, unrecoverable regardless of account state.", False),
        ("       - biddable + no delivery : candidate we could re-add if we choose.", False),
        ("       - enabled_but_dark / no_2026_delivery : diagnostic, needs fresh instances.", False),
        ("     Biddability coverage depends on keyword_identities.modifier_brand being tagged;", False),
        ("     'no_brand_tag' / 'brand_not_in_registry' rows are unresolved, not biddable.", False),
        ("  3. Macro insurance demand may have shifted YoY — not controlled here.", False),
        ("  4. Conversion attribution settles over time; recent 2026 dates undercount revenue",
         False),
        ("     relative to fully-settled 2025 dates.", False),
        ("  5. Windows are matched calendar spans (this version), not 7mo vs 1.5mo.", False),
        ("  6. Directional, not causal. Use to generate hypotheses, not to assign cause.", False),
        ("", False),
        ("EXACT/BROAD ATTRIBUTION (read before using Text_Netting)", True),
        ("  In 2026 the same text runs on EXACT and BROAD in the same campaign+ad group.", False),
        ("  The Google Ads API credits those conversions to the EXACT keyword; the UI credits", False),
        ("  BROAD. This analysis uses API data, so 2026 match-type splits are an attribution", False),
        ("  artifact, not serving credit. 2025 was EXACT-only (clean). The Text_Netting sheet", False),
        ("  sums across match types per text, which is INVARIANT to this swap — read the text", False),
        ("  totals there, and treat the per-match-type 2026 columns (suffixed _apiAttr) as", False),
        ("  reference only.", False),
        ("  LIMITATION: text-level netting catches same-text EXACT->BROAD migration. It does", False),
        ("  NOT catch a long-tail EXACT term's queries being absorbed by a shorter BROAD", False),
        ("  keyword (e.g. 'geico home insurance' queries served by a 'home insurance' BROAD", False),
        ("  keyword) — that re-attributes to a different text and needs search-term analysis.", False),
    ]
    for i, (text, bold) in enumerate(lines, 1):
        cell = ws.cell(row=i, column=1, value=text)
        if bold:
            cell.font = Font(bold=True, size=11, color="1F3864")
    return ws


def write_summary(wb, totals25, totals26):
    ws = wb.create_sheet("Summary")
    cols = ["Metric", "2025", "2026", "Delta", "Delta %"]
    ws.append(cols)
    for ci in range(1, len(cols) + 1):
        c = ws.cell(row=1, column=ci)
        c.fill = HEADER_FILL
        c.font = HEADER_FONT
    metrics = [
        ("Spend ($)", totals25["spend"], totals26["spend"]),
        ("Primary conversions", totals25["conv"], totals26["conv"]),
        ("Revenue ($)", totals25["revenue"], totals26["revenue"]),
        ("Impressions", totals25["impressions"], totals26["impressions"]),
        ("Clicks", totals25["clicks"], totals26["clicks"]),
        ("ROAS", safe_div(totals25["revenue"], totals25["spend"]),
         safe_div(totals26["revenue"], totals26["spend"])),
        ("CPA ($)", safe_div(totals25["spend"], totals25["conv"]),
         safe_div(totals26["spend"], totals26["conv"])),
    ]
    for name, v25, v26 in metrics:
        ad, pd = pct_change(v25, v26)
        ws.append([name, _r(v25, 3), _r(v26, 3), _r(ad, 3), fmt_pct(pd)])
    for ci in range(1, len(cols) + 1):
        ws.column_dimensions[get_column_letter(ci)].width = 22
    ws.freeze_panes = "A2"
    return ws


def aggregate_by(rows, key):
    buckets = {}
    for r in rows:
        k = r.get(key) or "(untagged)"
        b = buckets.setdefault(k, {"spend_2025": 0.0, "spend_2026": 0.0,
                                   "conv_2025": 0.0, "conv_2026": 0.0,
                                   "revenue_2025": 0.0, "revenue_2026": 0.0,
                                   "n": 0})
        b["n"] += 1
        for m in ("spend", "conv", "revenue"):
            b[f"{m}_2025"] += r.get(f"{m}_2025") or 0.0
            b[f"{m}_2026"] += r.get(f"{m}_2026") or 0.0
    out = []
    for k, b in sorted(buckets.items(), key=lambda kv: -kv[1]["spend_2025"]):
        row = {key: k, "n_identities": b["n"]}
        for m in ("spend", "conv", "revenue"):
            row[f"{m}_2025"] = round(b[f"{m}_2025"], 2)
            row[f"{m}_2026"] = round(b[f"{m}_2026"], 2)
            ad, pd = pct_change(b[f"{m}_2025"], b[f"{m}_2026"])
            row[f"{m}_delta"] = round(ad, 2)
            row[f"{m}_delta_pct"] = fmt_pct(pd)
        out.append(row)
    return out


def aggregate_by_pair(rows, k1, k2):
    buckets = {}
    for r in rows:
        key = (r.get(k1) or "(untagged)", r.get(k2) or "(untagged)")
        b = buckets.setdefault(key, {"spend_2025": 0.0, "spend_2026": 0.0,
                                     "conv_2025": 0.0, "conv_2026": 0.0,
                                     "revenue_2025": 0.0, "revenue_2026": 0.0,
                                     "n": 0})
        b["n"] += 1
        for m in ("spend", "conv", "revenue"):
            b[f"{m}_2025"] += r.get(f"{m}_2025") or 0.0
            b[f"{m}_2026"] += r.get(f"{m}_2026") or 0.0
    out = []
    for (a, c), b in sorted(buckets.items(), key=lambda kv: -kv[1]["spend_2025"]):
        row = {k1: a, k2: c, "n_identities": b["n"]}
        for m in ("spend", "conv", "revenue"):
            row[f"{m}_2025"] = round(b[f"{m}_2025"], 2)
            row[f"{m}_2026"] = round(b[f"{m}_2026"], 2)
            ad, pd = pct_change(b[f"{m}_2025"], b[f"{m}_2026"])
            row[f"{m}_delta"] = round(ad, 2)
            row[f"{m}_delta_pct"] = fmt_pct(pd)
        out.append(row)
    return out


# ---------------------------------------------------------------------------
# EXACT -> BROAD netting (text level, invariant to the API attribution swap)
# ---------------------------------------------------------------------------
# IMPORTANT ATTRIBUTION NOTE:
# When the same text runs on BOTH exact and broad match in the same
# campaign+ad group (the 2026 structure), the Google Ads *API* credits the
# conversions to the EXACT keyword; the *UI* credits BROAD. This analysis uses
# API data. So 2026 match-type-level conversion splits are an attribution
# artifact, NOT a record of which match type served. 2025 was EXACT-only, so
# 2025 is clean. We therefore net at the normalized_text level (summed across
# match types), which is invariant to the swap: the text total is the same no
# matter which match type the API credits.

NETTING_MIN_2025_CONV = 5.0
NETTING_MIN_2025_SPEND = 500.0
RETENTION_THRESHOLD = 0.50      # 2026/2025 text-conv ratio at/above this = retained


def build_text_netting(rows_all):
    """One row per normalized_text: 2025 vs 2026 totals summed across match
    types, plus a verdict on whether the term retained volume and, where it
    didn't, whether the gap is recoverable (biddable) or structural."""
    by_text = {}
    UNRESOLVED = (None, "no_brand_tag", "brand_not_in_registry", "registry_unavailable")
    for r in rows_all:
        t = r.get("keyword_text")
        if not t:
            continue
        e = by_text.setdefault(t, {
            "biddability": r.get("biddability"), "bid_status": r.get("bid_status"),
            "modifier_brand": r.get("modifier_brand"),
            "spend_2025": 0.0, "conv_2025": 0.0, "rev_2025": 0.0,
            "spend_2026": 0.0, "conv_2026": 0.0, "rev_2026": 0.0,
            "conv26_by_mt": {},
        })
        # If one match type of this text resolved a brand and another didn't,
        # keep the resolved answer.
        if e["biddability"] in UNRESOLVED and r.get("biddability") not in UNRESOLVED:
            e["biddability"] = r.get("biddability")
            e["bid_status"] = r.get("bid_status")
        e["spend_2025"] += r.get("spend_2025") or 0.0
        e["conv_2025"] += r.get("conv_2025") or 0.0
        e["rev_2025"] += r.get("revenue_2025") or 0.0
        e["spend_2026"] += r.get("spend_2026") or 0.0
        e["conv_2026"] += r.get("conv_2026") or 0.0
        e["rev_2026"] += r.get("revenue_2026") or 0.0
        mt = r.get("match_type") or "?"
        e["conv26_by_mt"][mt] = e["conv26_by_mt"].get(mt, 0.0) + (r.get("conv_2026") or 0.0)

    out = []
    for t, e in by_text.items():
        if e["conv_2025"] < NETTING_MIN_2025_CONV and e["spend_2025"] < NETTING_MIN_2025_SPEND:
            continue
        c25, c26 = e["conv_2025"], e["conv_2026"]
        ratio = (c26 / c25) if c25 else None
        bdb = e["biddability"]
        if c26 < 1.0:                          # whole text effectively dark in 2026
            if bdb == "non_biddable":
                verdict = "lost_non_biddable"
            elif bdb == "biddable":
                verdict = "lost_biddable_READD"
            elif bdb == "conditional":
                verdict = "lost_conditional"
            else:
                verdict = "lost_brand_unresolved"
        elif ratio is not None and ratio >= RETENTION_THRESHOLD:
            verdict = "retained_or_recaptured"
        else:
            verdict = "partial_retention"
        ad, pd = pct_change(c25, c26)
        broad26 = e["conv26_by_mt"].get("BROAD", 0.0)
        out.append({
            "keyword_text": t,
            "modifier_brand": e["modifier_brand"],
            "biddability": bdb,
            "bid_status": e["bid_status"],
            "verdict": verdict,
            "conv_2025": round(c25, 1),
            "conv_2026": round(c26, 1),
            "conv_delta": round(ad, 1),
            "conv_delta_pct": fmt_pct(pd),
            "spend_2025": round(e["spend_2025"], 2),
            "spend_2026": round(e["spend_2026"], 2),
            "rev_2025": round(e["rev_2025"], 2),
            "rev_2026": round(e["rev_2026"], 2),
            # 2026 match-type split shown FOR REFERENCE ONLY — API attribution
            # artifact, not serving credit. The text totals above are the truth.
            "conv26_exact_apiAttr": round(e["conv26_by_mt"].get("EXACT", 0.0), 1),
            "conv26_broad_apiAttr": round(broad26, 1),
            "conv26_phrase_apiAttr": round(e["conv26_by_mt"].get("PHRASE", 0.0), 1),
            "broad_share_2026_apiAttr": fmt_pct((broad26 / c26) if c26 else None),
        })
    out.sort(key=lambda r: r["conv_2025"], reverse=True)
    return out


def print_netting_summary(netting):
    """Roll the netting verdicts up so the recoverable gap is one glance away."""
    buckets = {}
    for r in netting:
        b = buckets.setdefault(r["verdict"], {"n": 0, "c25": 0.0, "c26": 0.0, "s25": 0.0})
        b["n"] += 1
        b["c25"] += r["conv_2025"]
        b["c26"] += r["conv_2026"]
        b["s25"] += r["spend_2025"]
    print("\n" + "=" * 96)
    print("TEXT-LEVEL NETTING (summed across match types — invariant to EXACT/BROAD API swap)")
    print("=" * 96)
    print(f"{'verdict':<26}{'texts':>7}{'2025 conv':>12}{'2026 conv':>12}{'2025 spend':>14}")
    order = ["retained_or_recaptured", "partial_retention", "lost_biddable_READD",
             "lost_conditional", "lost_non_biddable", "lost_brand_unresolved"]
    for v in order + [k for k in buckets if k not in order]:
        if v not in buckets:
            continue
        b = buckets[v]
        print(f"{v:<26}{b['n']:>7}{b['c25']:>12,.0f}{b['c26']:>12,.0f}{b['s25']:>14,.0f}")
    tot25 = sum(b["c25"] for b in buckets.values())
    tot26 = sum(b["c26"] for b in buckets.values())
    print("-" * 96)
    print(f"{'TOTAL (netted texts)':<26}{sum(b['n'] for b in buckets.values()):>7}"
          f"{tot25:>12,.0f}{tot26:>12,.0f}")
    print("\nRead: 'retained_or_recaptured' + 'partial_retention' = volume the TERM kept")
    print("      (incl. EXACT->BROAD migration). 'lost_biddable_READD' = genuine recoverable")
    print("      gap. 'lost_non_biddable' = structural (policy/trademark), not a lever.")
    print("=" * 96)


# ---------------------------------------------------------------------------
# Brand-level netting — answers "which carriers are gone entirely in 2026?"
# ---------------------------------------------------------------------------
BRAND_MIN_2025_CONV = 30.0


def build_brand_netting(rows_all):
    """Roll YoY up to modifier_brand (across all texts AND match types), using
    the real brand tag on every identity. A brand 'fully_dark_2026' has zero
    2026 delivery anywhere — the cleanest re-add target (no existing coverage to
    be redundant with). 'exact_gone_broad_only' means the brand survived via a
    different match type (re-adding the dark text would be redundant)."""
    UNRESOLVED = (None, "no_brand_tag", "brand_not_in_registry", "registry_unavailable")
    by_brand = {}
    for r in rows_all:
        mb = r.get("modifier_brand")
        if not mb:
            continue
        key = normalize_brand(mb)
        e = by_brand.setdefault(key, {
            "modifier_brand": mb, "biddability": r.get("biddability"),
            "bid_status": r.get("bid_status"),
            "conv_2025": 0.0, "conv_2026": 0.0, "spend_2025": 0.0, "spend_2026": 0.0,
            "c26_exact": 0.0, "c26_broad": 0.0, "c26_phrase": 0.0,
        })
        if e["biddability"] in UNRESOLVED and r.get("biddability") not in UNRESOLVED:
            e["biddability"] = r.get("biddability")
            e["bid_status"] = r.get("bid_status")
        e["conv_2025"] += r.get("conv_2025") or 0.0
        e["conv_2026"] += r.get("conv_2026") or 0.0
        e["spend_2025"] += r.get("spend_2025") or 0.0
        e["spend_2026"] += r.get("spend_2026") or 0.0
        mt = r.get("match_type") or "?"
        c26 = r.get("conv_2026") or 0.0
        if mt == "EXACT":
            e["c26_exact"] += c26
        elif mt == "BROAD":
            e["c26_broad"] += c26
        elif mt == "PHRASE":
            e["c26_phrase"] += c26

    out = []
    for key, e in by_brand.items():
        c25, c26 = e["conv_2025"], e["conv_2026"]
        if c25 < BRAND_MIN_2025_CONV:
            continue
        if c26 < 1.0:
            status = "fully_dark_2026"
        elif e["c26_exact"] < 1.0:
            status = "exact_gone_broad_only"
        else:
            status = "present"
        ad, pd = pct_change(c25, c26)
        out.append({
            "modifier_brand": e["modifier_brand"],
            "biddability": e["biddability"],
            "bid_status": e["bid_status"],
            "brand_status": status,
            "conv_2025": round(c25, 1),
            "conv_2026": round(c26, 1),
            "conv_delta_pct": fmt_pct(pd),
            "spend_2025": round(e["spend_2025"], 2),
            "spend_2026": round(e["spend_2026"], 2),
            "conv26_exact_apiAttr": round(e["c26_exact"], 1),
            "conv26_broad_apiAttr": round(e["c26_broad"], 1),
            "conv26_phrase_apiAttr": round(e["c26_phrase"], 1),
        })
    out.sort(key=lambda r: r["conv_2025"], reverse=True)
    return out


def print_brand_netting_summary(brand_netting):
    """Surface the re-add targets: biddable brands fully dark in 2026."""
    dark_biddable = [b for b in brand_netting
                     if b["brand_status"] == "fully_dark_2026"
                     and b["biddability"] == "biddable"]
    dark_nonbid = [b for b in brand_netting
                   if b["brand_status"] == "fully_dark_2026"
                   and b["biddability"] == "non_biddable"]
    print("\n" + "=" * 88)
    print("BRAND-LEVEL: carriers FULLY DARK in 2026 (zero delivery, any match type)")
    print("=" * 88)
    print(f"{'brand':<24}{'biddability':<14}{'2025 conv':>11}{'2025 spend':>13}")
    print("  -- biddable (clean re-add: no existing coverage) --")
    for b in dark_biddable:
        print(f"  {b['modifier_brand'][:20]:<22}{b['biddability']:<14}"
              f"{b['conv_2025']:>11,.0f}{b['spend_2025']:>13,.0f}")
    if dark_nonbid:
        print("  -- non-biddable (do NOT re-add) --")
        for b in dark_nonbid:
            print(f"  {b['modifier_brand'][:20]:<22}{b['biddability']:<14}"
                  f"{b['conv_2025']:>11,.0f}{b['spend_2025']:>13,.0f}")
    rec_c = sum(b["conv_2025"] for b in dark_biddable)
    rec_s = sum(b["spend_2025"] for b in dark_biddable)
    print("-" * 88)
    print(f"  Clean biddable re-add pool: {len(dark_biddable)} brands, "
          f"{rec_c:,.0f} conv (2025), ${rec_s:,.0f} spend (2025)")
    print("=" * 88)


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------
def print_validation_table(rows):
    """Top 20 identities by 2025 spend, printed to stdout."""
    top = sorted(rows, key=lambda r: r.get("spend_2025") or 0, reverse=True)[:20]
    print("\n" + "=" * 120)
    print("VALIDATION — top 20 keyword identities by 2025 spend")
    print("=" * 120)
    hdr = (f"{'keyword_text':<34}{'mt':<7}{'spd25':>10}{'spd26':>10}"
           f"{'cv25':>7}{'cv26':>7}{'rev25':>10}{'rev26':>10}"
           f"{'roas25':>8}{'roas26':>8}  status")
    print(hdr)
    print("-" * 120)
    for r in top:
        kt = (r["keyword_text"] or "")[:32]
        print(f"{kt:<34}{str(r['match_type'] or ''):<7}"
              f"{(r['spend_2025'] or 0):>10,.0f}{(r['spend_2026'] or 0):>10,.0f}"
              f"{(r['conv_2025'] or 0):>7,.0f}{(r['conv_2026'] or 0):>7,.0f}"
              f"{(r['revenue_2025'] or 0):>10,.0f}{(r['revenue_2026'] or 0):>10,.0f}"
              f"{(r['roas_2025'] if r['roas_2025'] is not None else 0):>8.2f}"
              f"{(r['roas_2026'] if r['roas_2026'] is not None else 0):>8.2f}"
              f"  {r['status_class']}")
    print("=" * 120)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def orphan_diagnostic_sql(c):
    """kpd rows whose instance_id has no matching keyword_instances row,
    bucketed by month, with spend. Account-scoped via campaigns (the instance
    join is exactly what's broken, so we cannot scope through it here)."""
    return f"""
        SELECT substr(kpd.{c['kpd_date_col']}, 1, 7)        AS ym,
               COUNT(*)                                     AS orphan_rows,
               SUM(kpd.{c['kpd_cost_col']}) / 1000000.0     AS orphan_spend,
               SUM(CASE WHEN kpd.{c['kpd_instance_col']} IS NULL THEN 1 ELSE 0 END)
                                                            AS null_instance_rows
        FROM {c['kpd_table']} kpd
        JOIN {c['campaigns_table']} camp
              ON camp.{c['campaigns_id_col']} = kpd.{c['kpd_campaign_col']}
        LEFT JOIN {c['ki_table']} ki
              ON ki.{c['ki_id_col']} = kpd.{c['kpd_instance_col']}
        WHERE kpd.{c['kpd_device_col']} = 'ALL'
          AND kpd.{c['kpd_date_col']} >= ?
          AND kpd.{c['kpd_date_col']} <= ?
          AND camp.{c['campaigns_account_col']} = ?
          AND ki.{c['ki_id_col']} IS NULL
        GROUP BY ym
        ORDER BY ym
    """


def run_orphan_diagnostic(conn, c, start, end):
    """Print orphaned-instance_id rows by month so a concentrated time window
    (one-time hygiene issue) is distinguishable from a steady leak."""
    print("\n" + "-" * 72)
    print("JOIN-LOSS DIAGNOSTIC — kpd rows with no matching keyword_instances")
    print("(bucketed by month; concentration in one window => one-time hygiene)")
    print("-" * 72)
    print(f"{'month':<10}{'orphan_rows':>14}{'orphan_spend':>16}{'null_inst':>12}")
    rows = conn.cursor().execute(orphan_diagnostic_sql(c), [start, end, ACCOUNT_ID]).fetchall()
    if not rows:
        print("  (no orphaned rows found)")
    else:
        for ym, n, spend, nulls in rows:
            print(f"{ym:<10}{n:>14,}{(spend or 0):>16,.2f}{(nulls or 0):>12,}")
    print("-" * 72)


def campaign_recon_cpd_sql(c):
    return f"""
        SELECT cpd.campaign_id, SUM(cpd.{c['cpd_cost_col']}) / 1000000.0
        FROM {c['cpd_table']} cpd
        WHERE cpd.{c['cpd_device_col']} = 'ALL'
          AND cpd.{c['cpd_date_col']} >= ?
          AND cpd.{c['cpd_date_col']} <= ?
          AND cpd.{c['cpd_account_col']} = ?
        GROUP BY cpd.campaign_id
    """


def campaign_recon_kpd_sql(c):
    return f"""
        SELECT kpd.{c['kpd_campaign_col']}, SUM(kpd.{c['kpd_cost_col']}) / 1000000.0
        FROM {c['kpd_table']} kpd
        JOIN {c['campaigns_table']} camp
              ON camp.{c['campaigns_id_col']} = kpd.{c['kpd_campaign_col']}
        WHERE kpd.{c['kpd_device_col']} = 'ALL'
          AND kpd.{c['kpd_date_col']} >= ?
          AND kpd.{c['kpd_date_col']} <= ?
          AND camp.{c['campaigns_account_col']} = ?
        GROUP BY kpd.{c['kpd_campaign_col']}
    """


def campaign_meta_sql(c):
    return f"""
        SELECT {c['campaigns_id_col']}, campaign_name, status,
               campaign_type, advertising_channel_sub_type
        FROM {c['campaigns_table']}
        WHERE {c['campaigns_account_col']} = ?
    """


def run_campaign_recon_diagnostic(conn, c, start, end, top_n=30):
    """Localize the cpd-minus-kpd gap by campaign so non-keyword spend (DSA,
    PMax, network) is visible. Campaigns where cpd >> kpd carry the gap; their
    channel sub-type usually names the cause."""
    cpd = {r[0]: (r[1] or 0.0) for r in
           conn.cursor().execute(campaign_recon_cpd_sql(c), [start, end, ACCOUNT_ID])}
    kpd = {r[0]: (r[1] or 0.0) for r in
           conn.cursor().execute(campaign_recon_kpd_sql(c), [start, end, ACCOUNT_ID])}
    meta = {r[0]: (r[1], r[2], r[3], r[4]) for r in
            conn.cursor().execute(campaign_meta_sql(c), [ACCOUNT_ID])}

    rows = []
    for cid in set(cpd) | set(kpd):
        cv, kv = cpd.get(cid, 0.0), kpd.get(cid, 0.0)
        name, status, ctype, sub = meta.get(cid, (f"(campaign {cid})", "?", "?", "?"))
        rows.append((name or "(unnamed)", status, sub or ctype or "?", cv, kv, cv - kv))
    rows.sort(key=lambda r: -abs(r[5]))

    print("\n" + "-" * 104)
    print(f"KPD-vs-CPD GAP DIAGNOSTIC by campaign  [{start} -> {end}]  "
          f"(gap = cpd - kpd = non-keyword spend)")
    print("-" * 104)
    print(f"{'campaign':<40}{'status':<10}{'channel':<16}"
          f"{'cpd':>12}{'kpd':>12}{'gap':>12}")
    for name, status, sub, cv, kv, gap in rows[:top_n]:
        print(f"{name[:38]:<40}{(status or '?'):<10}{sub[:14]:<16}"
              f"{cv:>12,.2f}{kv:>12,.2f}{gap:>12,.2f}")
    if len(rows) > top_n:
        rest = sum(r[5] for r in rows[top_n:])
        print(f"{'... (' + str(len(rows)-top_n) + ' more campaigns)':<40}"
              f"{'':<10}{'':<16}{'':>12}{'':>12}{rest:>12,.2f}")
    tot_gap = sum(r[5] for r in rows)
    print("-" * 104)
    print(f"{'TOTAL GAP':<78}{tot_gap:>12,.2f}")
    print("Read: gap concentrated in DSA/PMax/dynamic campaigns => benign non-keyword spend.")
    print("      gap spread evenly across keyword campaigns => search-partner/expansion spend.")
    print("-" * 104)


def status_diagnostic_sql(c):
    """Distinct status strings + last_seen recency, scoped to the Home account.
    Reveals whether currently_active fails on the status string or stale dates."""
    return f"""
        SELECT ki.{c['ki_status_col']}                       AS status,
               COUNT(*)                                      AS n,
               MIN(ki.{c['ki_lastseen_col']})                AS min_last_seen,
               MAX(ki.{c['ki_lastseen_col']})                AS max_last_seen,
               SUM(CASE WHEN ki.{c['ki_lastseen_col']} >= date('now', '-{ACTIVE_RECENCY_DAYS} days')
                        THEN 1 ELSE 0 END)                    AS recent_n
        FROM {c['ki_table']} ki
        WHERE ki.{c['ki_account_col']} = ?
        GROUP BY ki.{c['ki_status_col']}
        ORDER BY n DESC
    """


def status_sample_sql(c):
    """Instance-level dump for a few known-serving terms, so we can see the exact
    status string and last_seen_date on the instance that's actually delivering."""
    return f"""
        SELECT kid.{c['kid_text_col']}      AS keyword_text,
               kid.{c['kid_match_col']}      AS match_type,
               ki.{c['ki_status_col']}       AS status,
               ki.{c['ki_lastseen_col']}     AS last_seen_date,
               ki.campaign_name              AS campaign_name
        FROM {c['ki_table']} ki
        JOIN {c['kid_table']} kid
              ON kid.{c['kid_id_col']} = ki.{c['ki_identity_col']}
        WHERE ki.{c['ki_account_col']} = ?
          AND kid.{c['kid_text_col']} IN ('farmers insurance', 'home insurance', 'geico',
                                          'homeowners insurance')
        ORDER BY kid.{c['kid_text_col']}, ki.{c['ki_lastseen_col']} DESC
    """


def run_status_diagnostic(conn, c):
    cur = conn.cursor()
    print("\n" + "=" * 88)
    print(f"STATUS DIAGNOSTIC — keyword_instances, account {ACCOUNT_ID}  "
          f"(today = {date.today().isoformat()}, recency window = {ACTIVE_RECENCY_DAYS}d)")
    print("=" * 88)
    print(f"{'status':<16}{'n':>10}{'min_last_seen':>16}{'max_last_seen':>16}{'recent_n':>12}")
    for status, n, mn, mx, rec in cur.execute(status_diagnostic_sql(c), [ACCOUNT_ID]).fetchall():
        print(f"{str(status):<16}{n:>10,}{str(mn):>16}{str(mx):>16}{rec:>12,}")
    print("\nInterpretation:")
    print(f"  - If no row has status exactly 'ENABLED' -> the status string is the bug.")
    print(f"  - If 'ENABLED' exists but recent_n = 0 -> last_seen_date is stale "
          f"(instances sync lagging > {ACTIVE_RECENCY_DAYS}d); widen window or fix the sync.")
    print("-" * 88)
    print("Sample instances for known-serving terms:")
    print(f"{'keyword_text':<24}{'mt':<7}{'status':<14}{'last_seen':<14}campaign")
    rows = cur.execute(status_sample_sql(c), [ACCOUNT_ID]).fetchall()
    if not rows:
        print("  (no instances found for sample terms — check kid.normalized_text values)")
    for kt, mt, st, ls, cn in rows:
        print(f"{(kt or '')[:22]:<24}{str(mt or ''):<7}{str(st or ''):<14}"
              f"{str(ls or ''):<14}{(cn or '')[:36]}")
    print("=" * 88)


def main():
    ap = argparse.ArgumentParser(description="YoY keyword-identity analysis (Google Home).")
    ap.add_argument("--db-path", default=r"C:\Projects\google-ads-ops\db\paid_search.db")
    ap.add_argument("--out-dir", default=r"C:\Projects\google-ads-ops\analysis\output")
    ap.add_argument("--window-2025-start")
    ap.add_argument("--window-2025-end")
    ap.add_argument("--window-2026-start")
    ap.add_argument("--window-2026-end")
    ap.add_argument("--min-impressions", type=int, default=DEFAULT_MIN_IMPRESSIONS)
    ap.add_argument("--min-spend", type=float, default=DEFAULT_MIN_SPEND)
    ap.add_argument("--min-conversions", type=float, default=DEFAULT_MIN_CONVERSIONS)
    ap.add_argument("--inspect-schema", action="store_true",
                    help="Print table_info for key tables and exit.")
    ap.add_argument("--diagnose-status", action="store_true",
                    help="Print keyword_instances status/last_seen distribution and exit. "
                         "Use to debug a universally-False currently_active flag.")
    ap.add_argument("--skip-validation", action="store_true",
                    help="Skip the top-20 confirmation pause.")
    ap.add_argument("--recon-tolerance", type=float, default=RECON_GAP_WARN_PCT,
                    help="cpd-minus-kpd gap %% above which to warn + run the per-campaign "
                         "diagnostic (default %(default)s). Does not abort on its own.")
    args = ap.parse_args()

    conn = sqlite3.connect(f"file:{args.db_path}?mode=ro", uri=True)
    c = CONFIG

    if args.inspect_schema:
        inspect_schema(conn)
        return

    if args.diagnose_status:
        run_status_diagnostic(conn, c)
        return

    s25, e25, s26, e26 = derive_windows(args)
    print(f"2025 window: {s25} -> {e25}")
    print(f"2026 window: {s26} -> {e26}")

    # ---- reconciliation (2025 window) --------------------------------------
    p25 = [s25, e25, ACCOUNT_ID]
    kpd_spend = scalar(conn, kpd_total_spend_sql(c), p25)
    cpd_spend = scalar(conn, cpd_total_spend_sql(c), [s25, e25, ACCOUNT_ID])
    joined_spend = scalar(conn, identity_joined_spend_sql(c), p25)

    join_loss_pct = (kpd_spend - joined_spend) / kpd_spend * 100 if kpd_spend else 0.0
    gap_pct = (cpd_spend - kpd_spend) / cpd_spend * 100 if cpd_spend else 0.0  # signed: + means cpd>kpd
    print(f"\nRECON  kpd=${kpd_spend:,.2f}  cpd=${cpd_spend:,.2f}  "
          f"joined=${joined_spend:,.2f}")
    print(f"RECON  cpd-minus-kpd gap (non-keyword spend) = {gap_pct:+.4f}%  "
          f"(${cpd_spend - kpd_spend:,.2f})")
    print(f"RECON  spend lost to instance join = {join_loss_pct:.4f}%")

    # (1) JOIN-LOSS: the check that governs identity-level integrity.
    if join_loss_pct > JOIN_LOSS_WARN_PCT:
        print(f"\n*** JOIN-LOSS {join_loss_pct:.4f}% EXCEEDS {JOIN_LOSS_WARN_PCT}% "
              f"WARN THRESHOLD ***")
        print("    Identity-level totals are undercounting. Investigate before trusting.")
        run_orphan_diagnostic(conn, c, s25, e25)
        run_orphan_diagnostic(conn, c, s26, e26)

    # (2) kpd-vs-cpd: a SUBSET sanity check (cpd >= kpd; gap = non-keyword spend).
    #     Hard-fail only on a sign violation (kpd > cpd, impossible for a subset)
    #     or an implausibly large gap (probably comparing the wrong universes).
    #     A plausible gap just warns and runs the per-campaign diagnostic.
    if kpd_spend > cpd_spend * (1 + RECON_SIGN_TOL_PCT / 100.0):
        print(f"\n*** SIGN VIOLATION: kpd (${kpd_spend:,.2f}) exceeds cpd "
              f"(${cpd_spend:,.2f}). Keyword spend cannot exceed campaign spend — "
              f"this indicates double-counting or a scope error.")
        run_campaign_recon_diagnostic(conn, c, s25, e25)
        sys.exit("Aborting on reconciliation sign violation.")

    if gap_pct > args.recon_tolerance:
        print(f"\n*** cpd-minus-kpd gap {gap_pct:.4f}% exceeds warn threshold "
              f"{args.recon_tolerance}%. Localizing before proceeding. ***")
        run_campaign_recon_diagnostic(conn, c, s25, e25)
        if gap_pct > RECON_GAP_FAIL_PCT:
            sys.exit(f"Aborting: gap {gap_pct:.2f}% exceeds {RECON_GAP_FAIL_PCT}% — "
                     f"too large to be ordinary non-keyword spend. Check the table scopes.")
        print(f"\nGap is in the plausible non-keyword-spend band (<= {RECON_GAP_FAIL_PCT}%). "
              f"Confirm the breakdown above looks like DSA/PMax/network, then approve at "
              f"the top-20 gate below.")

    # ---- load aggregates + meta --------------------------------------------
    agg25 = load_agg(conn, identity_agg_sql(c), [s25, e25, ACCOUNT_ID])
    agg26 = load_agg(conn, identity_agg_sql(c), [s26, e26, ACCOUNT_ID])
    meta = load_meta(conn, c)

    # Is the keyword_instances table fresh enough to trust its status flag?
    seen_dates = [m["last_seen_date"] for m in meta.values() if m["last_seen_date"]]
    max_seen = max(seen_dates) if seen_dates else None
    cutoff = (date.today() - timedelta(days=ACTIVE_RECENCY_DAYS)).isoformat()
    instances_stale = (max_seen is None) or (max_seen < cutoff)
    if instances_stale:
        print(f"\n*** keyword_instances IS STALE: newest last_seen_date = {max_seen} "
              f"(recency cutoff {cutoff}). ***")
        print("    Structure sync has not run recently. status_class will report "
              "'no_2026_delivery' for zero-delivery keywords rather than guess at")
        print("    intentional removal. Delivery-based metrics are unaffected; the "
              "'was it cut?' question needs a fresh instances sync to answer.")

    rows_all = build_rows(meta, agg25, agg26, instances_stale)

    # ---- biddability axis (from brand_registry, the authoritative source) ---
    registry, registry_present = load_brand_registry(conn)
    if not registry_present:
        print("\n*** brand_registry table not found — biddability will show "
              "'registry_unavailable'. Run 07_seed_brand_registry.py. ***")
    for r in rows_all:
        annotate_biddability(r, registry, registry_present)

    # threshold filter for the per-identity YoY sheet (applied to 2025)
    def meets_2025(r):
        return ((r["impressions_2025"] or 0) >= args.min_impressions
                or (r["spend_2025"] or 0) >= args.min_spend
                or (r["conv_2025"] or 0) >= args.min_conversions)

    yoy_rows = [r for r in rows_all if meets_2025(r)]
    yoy_rows.sort(key=lambda r: r.get("spend_2025") or 0, reverse=True)

    # ---- validation gate ----------------------------------------------------
    print_validation_table(yoy_rows)
    if not args.skip_validation:
        ans = input("\nProceed to full Excel build? [y/N]: ").strip().lower()
        if ans != "y":
            print("Stopped before build. Re-run with --skip-validation to auto-proceed.")
            return

    # ---- derived lists ------------------------------------------------------
    disappeared = []
    for r in rows_all:
        meaningful = ((r["spend_2025"] or 0) >= DISAPPEARED_MIN_SPEND
                      or (r["conv_2025"] or 0) >= DISAPPEARED_MIN_CONV
                      or (r["revenue_2025"] or 0) >= DISAPPEARED_MIN_REVENUE)
        if meaningful and (r["impressions_2026"] or 0) == 0:
            r2 = dict(r)
            # Three distinct causes for zero 2026 delivery, in priority order:
            #  1. non-biddable -> Google/trademark enforcement, NOT our doing,
            #     unrecoverable regardless of account state.
            #  2. enabled-but-dark (per a FRESH instances flag) -> diagnostic.
            #  3. otherwise -> cut/paused/demand, undetermined while instances
            #     are stale. Never asserted as a deliberate cut.
            if r["biddability"] == "non_biddable":
                r2["removal_flag"] = "non_biddable (policy/trademark — unrecoverable)"
            elif r["currently_active"]:
                r2["removal_flag"] = "enabled_but_dark_per_instances"
            else:
                r2["removal_flag"] = "no_2026_delivery (cut/paused/demand — see last_seen)"
            disappeared.append(r2)
    disappeared.sort(key=lambda r: r.get("spend_2025") or 0, reverse=True)

    # Biddability breakdown of the disappeared bucket -> separates the
    # unrecoverable from the re-addable, with the lost 2025 spend behind each.
    bid_breakdown = {}
    for r in disappeared:
        b = r["biddability"]
        agg = bid_breakdown.setdefault(b, {"n": 0, "spend25": 0.0, "conv25": 0.0})
        agg["n"] += 1
        agg["spend25"] += r["spend_2025"] or 0.0
        agg["conv25"] += r["conv_2025"] or 0.0
    print("\nDISAPPEARED bucket by biddability (lost 2025 spend behind each):")
    for b, agg in sorted(bid_breakdown.items(), key=lambda kv: -kv[1]["spend25"]):
        print(f"  {b:<26} {agg['n']:>4} kw  ${agg['spend25']:>12,.0f}  "
              f"{agg['conv25']:>7,.0f} conv")

    faded = []
    for r in rows_all:
        if (r["impressions_2026"] or 0) == 0:   # must have served in 2026 to be "faded"
            continue
        if (r["spend_2025"] or 0) < FADED_MIN_2025_SPEND:
            continue
        impr_drop = r.get("impressions_delta_pct")
        conv_drop = r.get("conv_delta_pct")
        big_drop = ((impr_drop is not None and impr_drop <= -FADED_DROP_PCT * 100)
                    or (conv_drop is not None and conv_drop <= -FADED_DROP_PCT * 100))
        if big_drop:
            faded.append(r)
    faded.sort(key=lambda r: r.get("spend_2025") or 0, reverse=True)

    new_rows = []
    for r in rows_all:
        if (r["impressions_2025"] or 0) == 0:
            if ((r["impressions_2026"] or 0) >= args.min_impressions
                    or (r["spend_2026"] or 0) >= args.min_spend
                    or (r["conv_2026"] or 0) >= args.min_conversions):
                new_rows.append(r)
    new_rows.sort(key=lambda r: r.get("spend_2026") or 0, reverse=True)

    # ---- EXACT->BROAD netting (text level) ----------------------------------
    netting = build_text_netting(rows_all)
    print_netting_summary(netting)

    # ---- brand-level netting (which carriers are gone entirely) -------------
    brand_netting = build_brand_netting(rows_all)
    print_brand_netting_summary(brand_netting)

    # ---- totals -------------------------------------------------------------
    def totals(agg):
        t = _zero()
        for d in agg.values():
            for k in t:
                t[k] += d[k]
        return t
    t25, t26 = totals(agg25), totals(agg26)

    # ---- build workbook -----------------------------------------------------
    wb = Workbook()
    wb.remove(wb.active)

    write_readme(wb, (s25, e25, s26, e26),
                 {"kpd": kpd_spend, "cpd": cpd_spend, "joined": joined_spend,
                  "gap_pct": gap_pct, "join_loss_pct": join_loss_pct},
                 {"n25": len(agg25), "n26": len(agg26),
                  "disappeared": len(disappeared), "faded": len(faded),
                  "new": len(new_rows)})
    write_summary(wb, t25, t26)

    yoy_cols = (["identity_id", "keyword_text", "match_type", "brand_category",
                 "modifier_brand", "biddability", "bid_status",
                 "intent_type", "intent_action", "currently_active",
                 "status_class", "last_seen_date"]
                + [f"{m}_2025" for m in ("impressions", "clicks", "spend", "conv",
                                         "revenue", "cpc", "cvr", "rpv", "cpa", "roas")]
                + [f"{m}_2026" for m in ("impressions", "clicks", "spend", "conv",
                                         "revenue", "cpc", "cvr", "rpv", "cpa", "roas")]
                + [f"{m}_delta" for m in ("impressions", "clicks", "spend", "conv",
                                          "revenue", "cpc", "rpv", "roas", "cpa")]
                + [f"{m}_delta_pct" for m in ("impressions", "clicks", "spend", "conv",
                                              "revenue", "cpc", "rpv", "roas", "cpa")])
    write_sheet(wb, "Per_Keyword_YoY", yoy_cols, yoy_rows)

    roll_cols = lambda key: ([key, "n_identities"]
                             + [f"{m}_2025" for m in ("spend", "conv", "revenue")]
                             + [f"{m}_2026" for m in ("spend", "conv", "revenue")]
                             + [f"{m}_delta" for m in ("spend", "conv", "revenue")]
                             + [f"{m}_delta_pct" for m in ("spend", "conv", "revenue")])

    write_sheet(wb, "Rollup_BrandCategory", roll_cols("brand_category"),
                aggregate_by(yoy_rows, "brand_category"))
    write_sheet(wb, "Rollup_IntentType", roll_cols("intent_type"),
                aggregate_by(yoy_rows, "intent_type"))
    write_sheet(wb, "Rollup_IntentAction", roll_cols("intent_action"),
                aggregate_by(yoy_rows, "intent_action"))
    write_sheet(wb, "Rollup_MatchType", roll_cols("match_type"),
                aggregate_by(yoy_rows, "match_type"))

    pair_cols = (["brand_category", "match_type", "n_identities"]
                 + [f"{m}_2025" for m in ("spend", "conv", "revenue")]
                 + [f"{m}_2026" for m in ("spend", "conv", "revenue")]
                 + [f"{m}_delta" for m in ("spend", "conv", "revenue")]
                 + [f"{m}_delta_pct" for m in ("spend", "conv", "revenue")])
    write_sheet(wb, "Rollup_Brand_x_Match", pair_cols,
                aggregate_by_pair(yoy_rows, "brand_category", "match_type"))

    disc_cols = ["identity_id", "keyword_text", "match_type", "brand_category",
                 "modifier_brand", "biddability", "bid_status", "restriction_reason",
                 "intent_type", "spend_2025", "conv_2025", "revenue_2025",
                 "roas_2025", "removal_flag", "last_seen_date"]
    write_sheet(wb, "Disappeared", disc_cols, disappeared)

    faded_cols = (["identity_id", "keyword_text", "match_type", "brand_category",
                   "intent_type", "spend_2025", "conv_2025", "revenue_2025", "roas_2025",
                   "impressions_2026", "spend_2026", "conv_2026", "revenue_2026",
                   "roas_2026", "impressions_delta_pct", "conv_delta_pct",
                   "spend_delta_pct", "last_seen_date"])
    write_sheet(wb, "Faded", faded_cols, faded)

    new_cols = ["identity_id", "keyword_text", "match_type", "brand_category",
                "modifier_brand", "biddability", "bid_status",
                "intent_type", "impressions_2026", "clicks_2026", "spend_2026",
                "conv_2026", "revenue_2026", "roas_2026", "currently_active"]
    write_sheet(wb, "New_Keywords", new_cols, new_rows)

    net_cols = ["keyword_text", "modifier_brand", "biddability", "bid_status", "verdict",
                "conv_2025", "conv_2026", "conv_delta", "conv_delta_pct",
                "spend_2025", "spend_2026", "rev_2025", "rev_2026",
                "conv26_exact_apiAttr", "conv26_broad_apiAttr",
                "conv26_phrase_apiAttr", "broad_share_2026_apiAttr"]
    write_sheet(wb, "Text_Netting", net_cols, netting)

    brand_cols = ["modifier_brand", "biddability", "bid_status", "brand_status",
                  "conv_2025", "conv_2026", "conv_delta_pct", "spend_2025", "spend_2026",
                  "conv26_exact_apiAttr", "conv26_broad_apiAttr", "conv26_phrase_apiAttr"]
    write_sheet(wb, "Brand_Netting", brand_cols, brand_netting)

    # ---- save ---------------------------------------------------------------
    import os
    os.makedirs(args.out_dir, exist_ok=True)
    out_path = os.path.join(args.out_dir,
                            f"keyword_yoy_analysis_{date.today().isoformat()}.xlsx")
    wb.save(out_path)

    print(f"\nWROTE {out_path}")
    print(f"  Per_Keyword_YoY : {len(yoy_rows)} rows")
    print(f"  Disappeared     : {len(disappeared)} rows")
    print(f"  Faded           : {len(faded)} rows")
    print(f"  New_Keywords    : {len(new_rows)} rows")
    print(f"  Text_Netting    : {len(netting)} rows")
    print(f"  Brand_Netting   : {len(brand_netting)} rows")
    print(f"  2025 spend ${t25['spend']:,.0f} | 2026 spend ${t26['spend']:,.0f}")
    print(f"  2025 conv {t25['conv']:,.0f} | 2026 conv {t26['conv']:,.0f}")
    print(f"  2025 rev ${t25['revenue']:,.0f} | 2026 rev ${t26['revenue']:,.0f}")

    conn.close()


if __name__ == "__main__":
    main()
