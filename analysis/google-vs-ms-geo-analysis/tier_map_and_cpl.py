"""
tier_map_and_cpl.py — addendum to cross_platform_state_rpl.py

A) Google CPL through the collapse: monthly CPL (cost/leads), overall + by
   device, search-only geo grain (same source/cuts as the main analysis).
B) State -> tier assignment maps, both platforms, derived from performance
   rows (dominant campaign per state by impressions), with a cross-platform
   contrast table. Time-aware: Google's RPL k=4 cutover was 7/29, so the map
   is printed for an EARLY window (March) and a CURRENT window (Aug 1 -> max)
   separately.

Runtime discovery (no assumptions):
  - Google geo table campaign dimension: campaign_id expected; joined to the
    campaigns table for names (name column resolved at runtime).
  - Microsoft geo_performance_daily may be campaign-blind. If no campaign
    column exists, falls back to listing acct-2 campaign names with parsed
    tier labels and states named in campaign names — and says plainly that
    MS state membership must come from targeting records (baseline YAML),
    not performance rows.

Usage:
  python tier_map_and_cpl.py --db "C:\\Projects\\google-ads-ops\\db\\paid_search.db"
"""

import argparse
import os
import re
import sqlite3
import sys
from collections import defaultdict

GOOGLE_TABLE = "campaign_performance_geo_daily_search"
MS_TABLE = "geo_performance_daily"
START_MONTH = "2026-03"

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

TIER_RE = re.compile(r"tier[\s_\-]*(\d)", re.IGNORECASE)


def die(msg):
    print(f"\n*** HARD FAIL: {msg}")
    sys.exit(1)


def table_columns(con, table):
    rows = con.execute(f"PRAGMA table_info({table})").fetchall()
    if not rows:
        die(f"table {table} not found")
    return [r[1] for r in rows]


def resolve(cols, candidates, table, what, required=True):
    for c in candidates:
        if c in cols:
            return c
    if required:
        die(f"{table}: no {what} among {candidates}; columns = {cols}")
    return None


def norm_state(v):
    if v is None:
        return None
    s = str(v).strip()
    if s in STATE_NAME_TO_CODE:
        return STATE_NAME_TO_CODE[s]
    if s.upper() in STATE_NAME_TO_CODE.values():
        return s.upper()
    if s.title() in STATE_NAME_TO_CODE:
        return STATE_NAME_TO_CODE[s.title()]
    return None


def classify_campaign(name):
    """Return a tier/single-state label parsed from a campaign name."""
    if not name:
        return "UNPARSED"
    m = TIER_RE.search(name)
    if m:
        return f"Tier {m.group(1)}"
    # single-state campaigns: full state name or standalone 2-letter code token
    for full, code in STATE_NAME_TO_CODE.items():
        if re.search(rf"\b{re.escape(full)}\b", name, re.IGNORECASE):
            return f"Single:{code}"
        if re.search(rf"(?<![A-Za-z]){code}(?![A-Za-z])", name):
            return f"Single:{code}"
    return "OTHER"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="paid_search.db")
    args = ap.parse_args()
    con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row

    g_cols = table_columns(con, GOOGLE_TABLE)
    m_cols = table_columns(con, MS_TABLE)
    c_cols = table_columns(con, "campaigns")

    g_date = resolve(g_cols, ["report_date", "date", "day"], GOOGLE_TABLE, "date")
    m_date = resolve(m_cols, ["report_date", "date", "day"], MS_TABLE, "date")
    g_cost = resolve(g_cols, ["cost_micros", "cost"], GOOGLE_TABLE, "cost")
    g_camp = resolve(g_cols, ["campaign_id"], GOOGLE_TABLE, "campaign_id",
                     required=False)
    m_camp = resolve(m_cols, ["campaign_id"], MS_TABLE, "campaign_id",
                     required=False)
    c_name = resolve(c_cols, ["campaign_name", "name"], "campaigns", "name")
    c_id = resolve(c_cols, ["campaign_id", "id"], "campaigns", "campaign_id")
    g_impr = resolve(g_cols, ["impressions", "impr"], GOOGLE_TABLE, "impressions",
                     required=False)
    m_impr = resolve(m_cols, ["impressions", "impr"], MS_TABLE, "impressions",
                     required=False)

    cost_div = 1e6 if g_cost == "cost_micros" else 1.0
    g_max = con.execute(
        f"SELECT MAX({g_date}) FROM {GOOGLE_TABLE} WHERE account_id=1").fetchone()[0]

    # ================================================================
    # A) GOOGLE CPL, monthly, overall + by device (search-only geo grain)
    # ================================================================
    print("=" * 78)
    print("A) GOOGLE CPL THROUGH THE COLLAPSE — geo grain, search-only "
          "(_search view), acct 1")
    print("=" * 78)
    rows = con.execute(
        f"SELECT SUBSTR({g_date},1,7) ym, device, SUM({g_cost}) cost, "
        f"SUM(conversions) leads FROM {GOOGLE_TABLE} "
        f"WHERE account_id=1 AND SUBSTR({g_date},1,7) >= ? GROUP BY 1,2",
        (START_MONTH,)).fetchall()
    agg = defaultdict(lambda: [0.0, 0.0])   # (ym, devclass) -> [cost, leads]
    for r in rows:
        dc = str(r["device"]).upper()
        agg[(r["ym"], dc)][0] += (r["cost"] or 0) / cost_div
        agg[(r["ym"], dc)][1] += r["leads"] or 0
        agg[(r["ym"], "ALLDEV")][0] += (r["cost"] or 0) / cost_div
        agg[(r["ym"], "ALLDEV")][1] += r["leads"] or 0
    months = sorted({ym for ym, _ in agg})
    print(f"\n{'month':10}{'cost':>11}{'leads':>9}{'CPL':>8} | "
          f"{'CPL desk':>9}{'CPL mob':>9}{'CPL tab':>9}")
    for ym in months:
        c, l = agg[(ym, "ALLDEV")]
        line = f"{ym:10}{c:11,.0f}{l:9,.0f}{c / l if l else 0:8.2f} |"
        for d in ("DESKTOP", "MOBILE", "TABLET"):
            dc, dl = agg.get((ym, d), (0, 0))
            line += f"{dc / dl if dl else 0:9.2f}"
        print(line + ("  (partial)" if ym == g_max[:7] else ""))

    # ================================================================
    # B) STATE -> TIER MAPS
    # ================================================================
    print("\n" + "=" * 78)
    print("B) STATE -> TIER ASSIGNMENT MAPS (dominant campaign per state "
          "by impressions)")
    print("=" * 78)

    camp_names = {}
    for r in con.execute(f"SELECT {c_id} cid, {c_name} nm, account_id "
                         f"FROM campaigns WHERE account_id IN (1,2)"):
        camp_names[(r["account_id"], str(r["cid"]))] = r["nm"]

    def build_map(table, date_col, camp_col, impr_col, acct, extra_where, lo, hi):
        """state -> (label, campaign_name, share) using dominant impressions."""
        if camp_col is None:
            return None
        weight = f"SUM({impr_col})" if impr_col else "SUM(conversions)"
        rows = con.execute(
            f"SELECT geo_state, {camp_col} cid, {weight} w FROM {table} "
            f"WHERE account_id={acct} {extra_where} "
            f"AND SUBSTR({date_col},1,7) BETWEEN ? AND ? "
            f"GROUP BY 1,2", (lo, hi)).fetchall()
        per_state = defaultdict(list)
        for r in rows:
            st = norm_state(r["geo_state"])
            if st is None:
                continue
            nm = camp_names.get((acct, str(r["cid"])), f"<id {r['cid']}>")
            per_state[st].append((r["w"] or 0, nm))
        out = {}
        for st, lst in per_state.items():
            lst.sort(reverse=True)
            tot = sum(w for w, _ in lst) or 1
            w, nm = lst[0]
            out[st] = (classify_campaign(nm), nm, w / tot)
        return out

    # Google: early window (March, pre-7/29-cutover structure) vs current (Aug)
    g_windows = [("March (pre-cutover GPL structure)", START_MONTH, START_MONTH),
                 ("August (post-7/29 RPL k=4 structure)", "2026-08", "2026-08")]
    g_maps = {}
    if g_camp:
        for label, lo, hi in g_windows:
            g_maps[label] = build_map(GOOGLE_TABLE, g_date, g_camp, g_impr,
                                      1, "", lo, hi)
    else:
        print(f"\n[G ] {GOOGLE_TABLE} has NO campaign column — Google map "
              f"not derivable from this table.")

    m_maps = {}
    if m_camp:
        for label, lo, hi in [
                ("March (pre-consolidation legacy state campaigns)",
                 START_MONTH, START_MONTH),
                ("August (post-consolidation tier structure)",
                 "2026-08", "2026-08")]:
            m_maps[label] = build_map(MS_TABLE, m_date, m_camp, m_impr, 2,
                                      "AND network != 'Audience'", lo, hi)
    else:
        print(f"\n[MS] {MS_TABLE} has NO campaign column — Microsoft state "
              f"membership is NOT derivable from performance rows here.")
        print("[MS] Fallback: acct-2 campaign names with parsed labels "
              "(membership itself lives in targeting records / "
              "microsoft_home_baseline.yaml):")
        for (acct, cid), nm in sorted(camp_names.items()):
            if acct != 2:
                continue
            lab = classify_campaign(nm)
            if lab != "OTHER" or True:
                print(f"   {lab:12} {nm}")

    # print maps
    for label, mp in g_maps.items():
        if not mp:
            continue
        print(f"\n[G ] Google state->tier, {label}:")
        by_tier = defaultdict(list)
        for st, (lab, nm, share) in sorted(mp.items()):
            by_tier[lab].append(f"{st}{'' if share > 0.8 else f'({share:.0%})'}")
        for lab in sorted(by_tier):
            print(f"   {lab:12} {', '.join(by_tier[lab])}")
        low = [(st, v) for st, v in mp.items() if v[2] <= 0.6]
        if low:
            print("   NOTE states with dominant-campaign share <=60% "
                  "(split serving): "
                  + ", ".join(f"{st}->{v[0]}({v[2]:.0%})" for st, v in low))

    for label, mp in m_maps.items():
        if not mp:
            continue
        print(f"\n[MS] Microsoft state->tier, {label}:")
        by_tier = defaultdict(list)
        for st, (lab, nm, share) in sorted(mp.items()):
            by_tier[lab].append(f"{st}{'' if share > 0.8 else f'({share:.0%})'}")
        for lab in sorted(by_tier):
            print(f"   {lab:12} {', '.join(by_tier[lab])}")

    # contrast table (only if both sides derivable) — CURRENT windows only
    cur_g = g_maps.get(g_windows[1][0]) if g_maps else None
    m_map = m_maps.get("August (post-consolidation tier structure)") if m_maps else None
    if cur_g and m_map:
        print("\n" + "=" * 78)
        print("CROSS-PLATFORM CONTRAST (Google current vs Microsoft; note "
              "different k — G k=4+singles, MS k=6-derived+singles)")
        print("=" * 78)
        print(f"{'state':6}{'Google':>14}{'Microsoft':>14}   note")
        for st in sorted(set(cur_g) | set(m_map)):
            gl = cur_g.get(st, ("-", "", 0))[0]
            ml = m_map.get(st, ("-", "", 0))[0]
            note = ""
            if gl.startswith("Single") and ml.startswith("Single"):
                note = "standalone on both"
            elif gl.startswith("Single") != ml.startswith("Single") and "-" not in (gl, ml):
                note = "standalone on one side only"
            print(f"{st:6}{gl:>14}{ml:>14}   {note}")

    print("\n[DONE]")


if __name__ == "__main__":
    main()
