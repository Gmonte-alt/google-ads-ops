#!/usr/bin/env python

#### ------------------------------------------------------------------------------------------------------------------------- ####
#### ------------------------------------------------------------------------------------------------------------------------- ####
#### -------------------------------------------------------- BLOCK 1 -------------------------------------------------------- ####
#### ------------------------------------------------------------------------------------------------------------------------- ####
#### ------------------------------------------------------------------------------------------------------------------------- ####

"""keyword_census_rebuild.py — Stage 1: retrieval + window dates.
Diagnostic: campaign x window totals to reconcile against the UI."""

import argparse
import csv
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

DB_PATH = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")
OUT_DIR = Path(r"C:\Projects\google-ads-ops\analysis"
               r"\keyword-health\output\keyword-health")
ACCOUNT_ID = 1
CAMPAIGN_IDS = {
    "CALIFORNIA": 23385188622, "TEXAS": 23394808297,
    "FLORIDA": 23394808300, "TIER4": 23428389081,
    "TIER1": 23496334752, "TIER2": 23530860942,
    "TIER3": 23586637678,
}
WINDOWS = [7, 14, 30, 60, 90]   # + YTD added at runtime

# --- connection: read-only URI so an analyst run can never write ---
conn = sqlite3.connect(f"file:{DB_PATH.as_posix()}?mode=ro", uri=True)
conn.row_factory = sqlite3.Row

# --- column introspection: we never hardcode revenue/conversion
# column names; we detect them so the script survives schema drift ---
def pick_col(table, candidates):
    cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    for c in candidates:
        if c in cols:
            return c
    return None

REV = pick_col("keyword_performance_daily",
               ["conversion_value", "primary_conv_value"])
CONV = pick_col("keyword_performance_daily",
                ["conversions", "primary_conv_count"])
IMPR = pick_col("keyword_performance_daily", ["impressions", "impr"])
CLK = pick_col("keyword_performance_daily", ["clicks"])
print(f"columns -> revenue={REV}  conversions={CONV}  "
      f"impressions={IMPR}  clicks={CLK}")

# --- the data horizon: windows end at the LAST SYNCED DAY, not
# 'today'. This is the #1 source of UI mismatch, so we print it. ---
row = conn.execute(
    """SELECT MAX(p.report_date)
       FROM keyword_performance_daily p
       JOIN keyword_instances ki ON ki.id = p.instance_id
       WHERE ki.account_id = ?""", [ACCOUNT_ID]).fetchone()
DATA_END = datetime.strptime(str(row[0])[:10], "%Y-%m-%d").date()
wins = [(str(w), DATA_END - timedelta(days=w - 1)) for w in WINDOWS]
wins.append(("YTD", DATA_END.replace(month=1, day=1)))
print(f"\nDATA THROUGH: {DATA_END}   (set the UI to CUSTOM date "
      "ranges below — do NOT use the UI's 'Last 7 days' presets, "
      "they end at today, ours end at the last synced day)")
for name, start in wins:
    print(f"  window {name:>3}: {start}  ->  {DATA_END}  (inclusive)")

# --- Diagnostic 1: campaign x window totals ---------------------
# FILTERS, stated: (1) ki.account_id=1 scopes to Google Home via the
# instances join (kpd itself has no account column); (2) device='ALL'
# uses the pre-aggregated device row so device splits aren't double-
# counted; (3) charter campaign ids only; (4) report_date >= window
# start (inclusive both ends). kpd is SEARCH KEYWORD grain — compare
# against the UI's Search keywords totals, not campaign overview.
# ids = list(CAMPAIGN_IDS.values())
# terms = []
# for name, start in wins:
#     c = f"p.report_date >= '{start.isoformat()}'"
#     terms += [
#         f"SUM(CASE WHEN {c} THEN p.{IMPR} ELSE 0 END) im_{name}",
#         f"SUM(CASE WHEN {c} THEN p.{CLK} ELSE 0 END) ck_{name}",
#         f"SUM(CASE WHEN {c} THEN p.cost_micros ELSE 0 END)/1e6 "
#         f"s_{name}",
#         f"SUM(CASE WHEN {c} THEN p.{CONV} ELSE 0 END) cv_{name}",
#         f"SUM(CASE WHEN {c} THEN p.{REV} ELSE 0 END) r_{name}",
#     ]
# q = (f"SELECT ki.campaign_id cid, {', '.join(terms)} "
#      f"FROM keyword_performance_daily p "
#      f"JOIN keyword_instances ki ON ki.id = p.instance_id "
#      f"AND ki.account_id = ? "
#      f"WHERE p.device = 'ALL' "
#      f"AND p.report_date >= '{wins[-1][1].isoformat()}' "
#      f"AND ki.campaign_id IN ({','.join('?' * len(ids))}) "
#      f"GROUP BY 1")
# short_of = {cid: s for s, cid in CAMPAIGN_IDS.items()}
# OUT_DIR.mkdir(parents=True, exist_ok=True)
# diag = OUT_DIR / "diag_1_campaign_window_totals.csv"
# with open(diag, "w", newline="", encoding="utf-8") as f:
#     w = csv.writer(f)
#     w.writerow(["campaign", "window", "start_date", "end_date",
#                 "impressions", "clicks", "ctr", "cost",
#                 "conversions", "conv_value", "roas"])
#     for r in conn.execute(q, [ACCOUNT_ID] + ids):
#         camp = short_of[int(r["cid"])]
#         for name, start in wins:
#             im, ck = r[f"im_{name}"] or 0, r[f"ck_{name}"] or 0
#             s, cv = r[f"s_{name}"] or 0, r[f"cv_{name}"] or 0
#             rv = r[f"r_{name}"] or 0
#             w.writerow([camp, name, start, DATA_END,
#                         f"{im:.0f}", f"{ck:.0f}",
#                         f"{ck/im:.4f}" if im else "",
#                         f"{s:.2f}", f"{cv:.1f}", f"{rv:.2f}",
#                         f"{rv/s:.3f}" if s else ""])
# print(f"\nWROTE: {diag}")
# print("Reconcile in the UI: Campaigns view -> filter to the 7 "
#       "campaigns -> segment 'Search keywords' totals per campaign, "
#       "custom date range per window above.")
# conn.close()



#### ------------------------------------------------------------------------------------------------------------------------- ####
#### ------------------------------------------------------------------------------------------------------------------------- ####
#### -------------------------------------------------------- BLOCK 2 -------------------------------------------------------- ####
#### ------------------------------------------------------------------------------------------------------------------------- ####
#### ------------------------------------------------------------------------------------------------------------------------- ####



# --- Diagnostic 2: keyword grain, one campaign ------------------
# Grain (a) INSTANCE  = one row per keyword criterion per ad group
#   -> compares 1:1 against the UI Search keywords table.
# Grain (b) MERGED    = keyword text x match type (lowercased,
#   trimmed) -> the census grain; 'n_instances' counts merged
#   placements.
# NEW FILTER BEHAVIOR vs Block 1: we start FROM keyword_instances
# and LEFT JOIN performance, so enabled-but-never-served keywords
# appear with zeros. Everything else is unchanged: account scope via
# the instances join, device='ALL', inclusive dates ending DATA_END.
RECON_CAMPAIGN = "TIER2"        # <- edit to reconcile another one

cid = CAMPAIGN_IDS[RECON_CAMPAIGN]
status_col = pick_col("keyword_instances",
                      ["status", "keyword_status", "kw_status"])
print(f"\nDiagnostic 2 on {RECON_CAMPAIGN} ({cid}); "
      f"keyword status column: {status_col or 'NONE'}")

terms = []
for name, start in wins:
    c = f"p.report_date >= '{start.isoformat()}'"
    terms += [
        f"SUM(CASE WHEN {c} THEN p.{IMPR} ELSE 0 END) im_{name}",
        f"SUM(CASE WHEN {c} THEN p.{CLK} ELSE 0 END) ck_{name}",
        f"SUM(CASE WHEN {c} THEN p.cost_micros ELSE 0 END)/1e6 "
        f"s_{name}",
        f"SUM(CASE WHEN {c} THEN p.{CONV} ELSE 0 END) cv_{name}",
        f"SUM(CASE WHEN {c} THEN p.{REV} ELSE 0 END) r_{name}",
    ]
st_expr = f"ki.{status_col}" if status_col else "NULL"
q = (f"SELECT ki.id iid, ki.ad_group_id agid, "
     f"ki.keyword_text kw_raw, ki.match_type mt, {st_expr} st, "
     f"{', '.join(terms)} "
     f"FROM keyword_instances ki "
     f"LEFT JOIN keyword_performance_daily p "
     f"ON p.instance_id = ki.id AND p.device = 'ALL' "
     f"AND p.report_date >= '{wins[-1][1].isoformat()}' "
     f"WHERE ki.account_id = ? AND ki.campaign_id = ? "
     f"GROUP BY 1, 2, 3, 4, 5")
rows = conn.execute(q, [ACCOUNT_ID, cid]).fetchall()

METRIC_HDR = []
for name, _ in wins:
    METRIC_HDR += [f"impr_{name}", f"clicks_{name}", f"ctr_{name}",
                   f"cost_{name}", f"conv_{name}", f"value_{name}",
                   f"roas_{name}"]

def metric_cells(src):
    cells = []
    for name, _ in wins:
        im = src[f"im_{name}"] or 0
        ck = src[f"ck_{name}"] or 0
        s = src[f"s_{name}"] or 0
        cv = src[f"cv_{name}"] or 0
        rv = src[f"r_{name}"] or 0
        cells += [f"{im:.0f}", f"{ck:.0f}",
                  f"{ck/im:.4f}" if im else "",
                  f"{s:.2f}", f"{cv:.1f}", f"{rv:.2f}",
                  f"{rv/s:.3f}" if s else ""]
    return cells

# (a) instance grain — raw keyword text preserved for UI matching
d2a = OUT_DIR / "diag_2_keyword_instances.csv"
with open(d2a, "w", newline="", encoding="utf-8") as f:
    w = csv.writer(f)
    w.writerow(["instance_id", "ad_group_id", "keyword_text",
                "match_type", "status"] + METRIC_HDR)
    for r in sorted(rows, key=lambda x: -(x["s_30"] or 0)):
        w.writerow([r["iid"], r["agid"], r["kw_raw"], r["mt"],
                    r["st"]] + metric_cells(r))

# (b) merged grain — the census transformation, shown explicitly
merged = {}
for r in rows:
    key = (str(r["mt"] or "?").strip().upper(),
           (r["kw_raw"] or "").strip().lower())
    m = merged.setdefault(key, {"iids": set(), "sts": set(),
                                **{f"{f}_{n}": 0.0
                                   for n, _ in wins
                                   for f in ("im", "ck", "s",
                                             "cv", "r")}})
    m["iids"].add(r["iid"])
    if r["st"] is not None:
        m["sts"].add(str(r["st"]).upper())
    for n, _ in wins:
        for fld in ("im", "ck", "s", "cv", "r"):
            m[f"{fld}_{n}"] += r[f"{fld}_{n}"] or 0

d2b = OUT_DIR / "diag_2b_keyword_merged.csv"
with open(d2b, "w", newline="", encoding="utf-8") as f:
    w = csv.writer(f)
    w.writerow(["keyword", "match_type", "n_instances", "statuses"]
               + METRIC_HDR)
    for (mt, kw), m in sorted(merged.items(),
                              key=lambda kv: -kv[1]["s_30"]):
        w.writerow([kw, mt, len(m["iids"]),
                    "/".join(sorted(m["sts"]))] + metric_cells(m))

# console: the reconciliation numbers you'll check first
n_zero = sum(1 for r in rows if (r["im_YTD"] or 0) == 0)
tot30 = sum(r["s_30"] or 0 for r in rows)
print(f"  instance rows: {len(rows):,}  (zero-impression YTD: "
      f"{n_zero})")
print(f"  merged keyword x match rows: {len(merged):,}")
print(f"  30d cost total: ${tot30:,.2f}  <- must equal Diagnostic "
      f"1's {RECON_CAMPAIGN} 30d cost")
print(f"  top 10 by 30d cost:")
for r in sorted(rows, key=lambda x: -(x['s_30'] or 0))[:10]:
    print(f"    {r['kw_raw'][:38]:38s} {str(r['mt']):8s} "
          f"${r['s_30'] or 0:9,.2f}  impr {r['im_30'] or 0:8,.0f}")
print(f"\nWROTE: {d2a}")
print(f"WROTE: {d2b}")



#### ------------------------------------------------------------------------------------------------------------------------- ####
#### ------------------------------------------------------------------------------------------------------------------------- ####
#### -------------------------------------------------------- BLOCK 3 -------------------------------------------------------- ####
#### ------------------------------------------------------------------------------------------------------------------------- ####
#### ------------------------------------------------------------------------------------------------------------------------- ####


# --- Diagnostic 3: QS layer (same campaign) ---------------------
# Two different QS numbers, on purpose:
#   latest_qs : the most recent daily snapshot per instance — this
#               is the number that should MATCH the UI's Quality
#               Score column (the UI shows current QS).
#   iwqs_<w>  : impression-weighted QS over each window — the
#               census's analytical number. House rules: QS is NEVER
#               summed, weighting uses OUR kpd impressions, unrated
#               days are excluded, and rated_share exposes coverage.
q3_terms = []
for name, start in wins:
    c = f"p.report_date >= '{start.isoformat()}'"
    q3_terms += [
        f"SUM(CASE WHEN {c} THEN p.{IMPR} ELSE 0 END) ti_{name}",
        f"SUM(CASE WHEN {c} AND q.historical_quality_score IS NOT "
        f"NULL THEN p.{IMPR} ELSE 0 END) ri_{name}",
        f"SUM(CASE WHEN {c} AND q.historical_quality_score IS NOT "
        f"NULL THEN q.historical_quality_score * p.{IMPR} "
        f"ELSE 0 END) qi_{name}",
    ]
q3 = (f"SELECT ki.id iid, ki.keyword_text kw_raw, ki.match_type mt, "
      f"{', '.join(q3_terms)} "
      f"FROM keyword_instances ki "
      f"LEFT JOIN keyword_performance_daily p "
      f"ON p.instance_id = ki.id AND p.device = 'ALL' "
      f"AND p.report_date >= '{wins[-1][1].isoformat()}' "
      f"LEFT JOIN keyword_quality_daily q "
      f"ON q.instance_id = p.instance_id "
      f"AND q.report_date = p.report_date "
      f"WHERE ki.account_id = ? AND ki.campaign_id = ? "
      f"GROUP BY 1, 2, 3")
q3_rows = conn.execute(q3, [ACCOUNT_ID, cid]).fetchall()

# latest snapshot per instance (the UI-comparable number)
latest = {}
for r in conn.execute(
        """SELECT instance_id, historical_quality_score FROM (
             SELECT instance_id, historical_quality_score,
                    ROW_NUMBER() OVER (PARTITION BY instance_id
                        ORDER BY report_date DESC) rn
             FROM keyword_quality_daily
             WHERE report_date <= ?) WHERE rn = 1""",
        [DATA_END.isoformat()]):
    latest[int(r["instance_id"])] = r["historical_quality_score"]

qs_merged = {}
for r in q3_rows:
    key = (str(r["mt"] or "?").strip().upper(),
           (r["kw_raw"] or "").strip().lower())
    m = qs_merged.setdefault(key, {"latest": set(),
                                   **{f"{f}_{n}": 0.0
                                      for n, _ in wins
                                      for f in ("ti", "ri", "qi")}})
    if latest.get(r["iid"]) is not None:
        m["latest"].add(int(latest[r["iid"]]))
    for n, _ in wins:
        for fld in ("ti", "ri", "qi"):
            m[f"{fld}_{n}"] += r[f"{fld}_{n}"] or 0

d3 = OUT_DIR / "diag_3_qs.csv"
with open(d3, "w", newline="", encoding="utf-8") as f:
    w = csv.writer(f)
    hdr = ["keyword", "match_type", "latest_qs"]
    for n, _ in wins:
        hdr += [f"iwqs_{n}", f"rated_share_{n}"]
    w.writerow(hdr)
    for (mt, kw), m in sorted(qs_merged.items(),
                              key=lambda kv: -kv[1]["ti_30"]):
        cells = [kw, mt,
                 "/".join(str(v) for v in sorted(m["latest"]))
                 or ""]
        for n, _ in wins:
            ri, qi, ti = m[f"ri_{n}"], m[f"qi_{n}"], m[f"ti_{n}"]
            cells += [f"{qi/ri:.2f}" if ri > 0 else "",
                      f"{ri/ti:.3f}" if ti > 0 else ""]
        w.writerow(cells)

# console: campaign-level coverage + spot rows
print(f"\nDiagnostic 3 — QS on {RECON_CAMPAIGN}:")
for n, _ in wins:
    ti = sum(m[f"ti_{n}"] for m in qs_merged.values())
    ri = sum(m[f"ri_{n}"] for m in qs_merged.values())
    qi = sum(m[f"qi_{n}"] for m in qs_merged.values())
    print(f"  {n:>3}: campaign iwQS "
          f"{(qi/ri):.2f}" if ri > 0 else f"  {n:>3}: no rated",
          f" | rated coverage {(ri/ti):.0%}" if ti > 0 else "")
print("  top 10 by 30d impressions:")
for (mt, kw), m in sorted(qs_merged.items(),
                          key=lambda kv: -kv[1]["ti_30"])[:10]:
    ri, qi = m["ri_30"], m["qi_30"]
    print(f"    {kw[:36]:36s} {mt:8s} latest="
          f"{'/'.join(str(v) for v in sorted(m['latest'])) or '—'}"
          f"  iwqs30={qi/ri:.2f}" if ri > 0 else
          f"    {kw[:36]:36s} {mt:8s} latest="
          f"{'/'.join(str(v) for v in sorted(m['latest'])) or '—'}"
          f"  iwqs30=—")
print(f"\nWROTE: {d3}")

#### -------------------------------------------------------- END BLOCK 3 -------------------------------------------------------- ####


conn.close()