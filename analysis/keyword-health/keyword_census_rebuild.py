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
#### -------------------------------------------------------- BLOCK 4 -------------------------------------------------------- ####
#### ------------------------------------------------------------------------------------------------------------------------- ####
#### ------------------------------------------------------------------------------------------------------------------------- ####



# --- Block 4: full census pull, all charter campaigns -----------
# Same filters you validated in Blocks 1-3, now in one pass:
# FROM keyword_instances (zero-data rows kept) LEFT JOIN kpd
# (device='ALL', account scope via the join) LEFT JOIN kqd
# (QS never summed; impression-weighted per window).
# NEW TRANSFORMATIONS (both feed the verdict engine in Block 5):
#   segment    text-pattern carrier classification (tripwire's
#              patterns verbatim, so segments reconcile with the
#              daily monitoring)
#   protected  Farmers home-intent standing rule — never pause
CARRIER_PATTERNS = [
    ("Geico", ["geico"]), ("Farmers", ["farmers"]),
    ("State Farm", ["state farm", "statefarm"]),
    ("Travelers", ["travelers"]),
    ("Kin", ["kin insurance", " kin ", "kin home"]),
    ("AAA", ["aaa"]), ("Amica", ["amica"]),
    ("Hartford", ["hartford"]), ("Costco", ["costco"]),
]
HOME_TOKENS = ("home", "homeowner", "homeowners", "house")

def classify(kw):
    k = f" {kw.lower().strip()} "
    for label, pats in CARRIER_PATTERNS:
        for p in pats:
            if p in k:
                return label
    return "Other/generic"

def is_protected(kw):
    return classify(kw) == "Farmers" and \
        any(t in kw for t in HOME_TOKENS)

status_col = pick_col("keyword_instances",
                      ["status", "keyword_status", "kw_status"])
st_expr = f"ki.{status_col}" if status_col else "NULL"
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
        f"SUM(CASE WHEN {c} AND q.historical_quality_score IS NOT "
        f"NULL THEN p.{IMPR} ELSE 0 END) ri_{name}",
        f"SUM(CASE WHEN {c} AND q.historical_quality_score IS NOT "
        f"NULL THEN q.historical_quality_score * p.{IMPR} "
        f"ELSE 0 END) qi_{name}",
    ]
ids = list(CAMPAIGN_IDS.values())
q = (f"SELECT ki.id iid, ki.campaign_id cid, "
     f"ki.keyword_text kw_raw, ki.match_type mt, {st_expr} st, "
     f"{', '.join(terms)} "
     f"FROM keyword_instances ki "
     f"LEFT JOIN keyword_performance_daily p "
     f"ON p.instance_id = ki.id AND p.device = 'ALL' "
     f"AND p.report_date >= '{wins[-1][1].isoformat()}' "
     f"LEFT JOIN keyword_quality_daily q "
     f"ON q.instance_id = p.instance_id "
     f"AND q.report_date = p.report_date "
     f"WHERE ki.account_id = ? AND ki.campaign_id IN "
     f"({','.join('?' * len(ids))}) "
     f"GROUP BY 1, 2, 3, 4, 5")
rows = conn.execute(q, [ACCOUNT_ID] + ids).fetchall()
short_of = {c_: s for s, c_ in CAMPAIGN_IDS.items()}

FIELDS = ("im", "ck", "s", "cv", "r", "ri", "qi")
merged = {}
for r in rows:
    short = short_of[int(r["cid"])]
    key = (short, str(r["mt"] or "?").strip().upper(),
           (r["kw_raw"] or "").strip().lower())
    m = merged.setdefault(key, {"iids": set(), "sts": set(),
                                "inst_s90": [],
                                **{f"{f}_{n}": 0.0
                                   for n, _ in wins
                                   for f in FIELDS}})
    m["iids"].add(r["iid"])
    if r["st"] is not None:
        m["sts"].add(str(r["st"]).upper())
    # per-instance 90d economics kept for the Block-5
    # MIXED-PLACEMENT check (merge-disagreement flag)
    s90, r90 = r["s_90"] or 0, r["r_90"] or 0
    m["inst_s90"].append((s90, r90))
    for n, _ in wins:
        for fld in FIELDS:
            m[f"{fld}_{n}"] += r[f"{fld}_{n}"] or 0

d4 = OUT_DIR / "diag_4_census_full.csv"
with open(d4, "w", newline="", encoding="utf-8") as f:
    w = csv.writer(f)
    hdr = ["campaign", "keyword", "match_type", "n_instances",
           "statuses", "segment", "protected"]
    for n, _ in wins:
        hdr += [f"impr_{n}", f"ctr_{n}", f"iwqs_{n}",
                f"rated_share_{n}", f"cost_{n}", f"conv_{n}",
                f"value_{n}", f"roas_{n}"]
    w.writerow(hdr)
    for (short, mt, kw), m in sorted(merged.items(),
                                     key=lambda kv: -kv[1]["s_90"]):
        cells = [short, kw, mt, len(m["iids"]),
                 "/".join(sorted(m["sts"])), classify(kw),
                 is_protected(kw)]
        for n, _ in wins:
            im, ck = m[f"im_{n}"], m[f"ck_{n}"]
            s, cv, rv = m[f"s_{n}"], m[f"cv_{n}"], m[f"r_{n}"]
            ri, qi = m[f"ri_{n}"], m[f"qi_{n}"]
            cells += [f"{im:.0f}",
                      f"{ck/im:.4f}" if im else "",
                      f"{qi/ri:.2f}" if ri > 0 else "",
                      f"{ri/im:.3f}" if im else "",
                      f"{s:.2f}", f"{cv:.1f}", f"{rv:.2f}",
                      f"{rv/s:.3f}" if s else ""]
        w.writerow(cells)

# reconciliation console
print(f"\nBlock 4 — full census: {len(rows):,} instance rows -> "
      f"{len(merged):,} keyword x match x campaign rows")
print(f"{'campaign':11s} {'inst':>6s} {'merged':>7s} "
      f"{'zeroYTD':>8s} {'cost30':>12s}")
acct30 = 0.0
for short in sorted(CAMPAIGN_IDS):
    sel = [(k, m) for k, m in merged.items() if k[0] == short]
    inst = sum(len(m["iids"]) for _, m in sel)
    zero = sum(1 for _, m in sel if m["im_YTD"] == 0)
    c30 = sum(m["s_30"] for _, m in sel)
    acct30 += c30
    print(f"{short:11s} {inst:6d} {len(sel):7d} {zero:8d} "
          f"${c30:11,.2f}")
print(f"{'ACCOUNT':11s} {'':6s} {'':7s} {'':8s} ${acct30:11,.2f}"
      f"  <- must equal Diagnostic 1's campaign 30d costs summed")
print(f"\nWROTE: {d4}")
conn.close()



#### ------------------------------------------------------------------------------------------------------------------------- ####
#### ------------------------------------------------------------------------------------------------------------------------- ####
#### -------------------------------------------------------- BLOCK 5 -------------------------------------------------------- ####
#### ------------------------------------------------------------------------------------------------------------------------- ####
#### ------------------------------------------------------------------------------------------------------------------------- ####



# --- Block 5: verdict engine ------------------------------------
# Transparent rule cascade; every verdict carries its reason.
# Thresholds are constants so the meeting can argue about numbers,
# not code.
MIN_VERDICT_SPEND = 100.0   # 90d spend needed before a PAUSE verdict
BASE_SPEND = 500.0          # 90d spend separating BASE vs INCREMENTAL
BAD = 0.85                  # unhealthy line (house convention)
IMPROVING_DELTA = 0.15      # 30d must beat 90d by this to rescue
IMPROVING_FLOOR = 0.95      # ...or simply reach this in 30d
MIXED_MIN_INST_SPEND = 50.0 # instance must have this 90d spend to
                            # count toward placement disagreement
MIXED_SPREAD = 0.30         # instance ROAS spread that flags a row

def mixed_placement(m):
    """True when >=2 judgeable placements disagree materially —
    the case where the text-grain merge could mislead (your Block-2
    question, answered in code)."""
    inst = [(s, r / s) for s, r in m["inst_s90"]
            if s >= MIXED_MIN_INST_SPEND]
    if len(inst) < 2:
        return False, None
    roas = [x[1] for x in inst]
    spread = max(roas) - min(roas)
    return spread >= MIXED_SPREAD, spread

def verdict(short, mt, kw, m):
    prot = is_protected(kw)
    sts = m["sts"]
    enabled = (not sts) or any("ENAB" in s for s in sts)
    s90, r90v = m["s_90"], m["r_90"]
    roas90 = (r90v / s90) if s90 > 0 else None
    s30, r30v = m["s_30"], m["r_30"]
    roas30 = (r30v / s30) if s30 > 0 else None
    mixed, spread = mixed_placement(m)
    if not enabled:
        return "ALREADY-PAUSED", "instance status paused", mixed
    if prot:
        return "KEEP-PROTECTED", "Farmers home-intent standing rule", \
            mixed
    if m["im_90"] == 0:
        return "ZOMBIE", ("no impressions 90d — serving-eligibility "
                          "dead; NOT a pause (Oct-25 footprint "
                          "lesson)"), mixed
    if s90 < MIN_VERDICT_SPEND:
        if roas90 is not None and roas90 >= 1.0:
            return "KEEP-INCREMENTAL", \
                f"thin but earning (r90 {roas90:.2f}, ${s90:,.0f})", \
                mixed
        return "WATCH", f"thin (${s90:,.0f} 90d)", mixed
    improving = (roas30 is not None and
                 ((roas90 is not None and
                   roas30 - roas90 >= IMPROVING_DELTA)
                  or roas30 >= IMPROVING_FLOOR))
    if roas90 is not None and roas90 < BAD and \
            (roas30 is None or roas30 < BAD) and not improving:
        if mixed:
            return "WATCH", \
                (f"pause-shaped (r90 {roas90:.2f}) BUT placements "
                 f"disagree (spread {spread:.2f}) — resolve at "
                 "ad-group level before pausing"), mixed
        r30s = f"{roas30:.2f}" if roas30 is not None else "n/a"
        return "PAUSE", (f"r90 {roas90:.2f} / r30 {r30s} — bad both "
                         f"windows, not improving "
                         f"(${s90:,.0f} 90d)"), mixed
    if roas90 is not None and roas90 >= 1.0:
        if s90 >= BASE_SPEND:
            return "KEEP-BASE", \
                f"r90 {roas90:.2f} at ${s90:,.0f} 90d", mixed
        return "KEEP-INCREMENTAL", \
            f"r90 {roas90:.2f}, room below base bar", mixed
    if roas90 is not None and roas90 >= BAD:
        return "WATCH", f"borderline r90 {roas90:.2f}", mixed
    r30s = f"{roas30:.2f}" if roas30 is not None else "n/a"
    return "WATCH", \
        f"unhealthy r90 {roas90:.2f} but improving (r30 {r30s})", \
        mixed

results = []
for (short, mt, kw), m in merged.items():
    v, why, mixed = verdict(short, mt, kw, m)
    results.append({"campaign": short, "keyword": kw,
                    "match_type": mt,
                    "n_instances": len(m["iids"]),
                    "segment": classify(kw),
                    "protected": is_protected(kw),
                    "verdict": v, "mixed_placement": mixed,
                    "reason": why,
                    "spend_90": m["s_90"],
                    "roas_90": (m["r_90"] / m["s_90"])
                    if m["s_90"] > 0 else None,
                    "roas_30": (m["r_30"] / m["s_30"])
                    if m["s_30"] > 0 else None,
                    "spend_30_total": m["s_30"],
                    "spend_30_per_day": m["s_30"] / 30.0,
                    "gp_30_total": m["r_30"] - m["s_30"],
                    "gp_30_per_day": (m["r_30"] - m["s_30"]) / 30.0,
                    "impr_90": m["im_90"]})

d5 = OUT_DIR / "diag_5_verdicts.csv"
with open(d5, "w", newline="", encoding="utf-8") as f:
    w = csv.DictWriter(f, fieldnames=list(results[0].keys()))
    w.writeheader()
    w.writerows(sorted(results, key=lambda x: -x["spend_90"]))

ORDER = ["PAUSE", "WATCH", "KEEP-INCREMENTAL", "KEEP-BASE",
         "KEEP-PROTECTED", "ZOMBIE", "ALREADY-PAUSED"]
print("\nBlock 5 — verdicts "
      f"(PAUSE: <{BAD} in 90d AND 30d, not improving, >= "
      f"${MIN_VERDICT_SPEND:,.0f}/90d; BASE bar "
      f"${BASE_SPEND:,.0f}/90d):")
for short in sorted(CAMPAIGN_IDS) + ["ACCOUNT"]:
    sel = results if short == "ACCOUNT" else \
        [x for x in results if x["campaign"] == short]
    parts = []
    for v in ORDER:
        vs = [x for x in sel if x["verdict"] == v]
        if not vs:
            continue
        s = sum(x["spend_30_day"] for x in vs)
        parts.append(f"{v} {len(vs)}"
                     + (f" (${s:,.0f}/d)" if s >= 1 else ""))
    print(f"  {short:11s} " + " · ".join(parts))
pause = [x for x in results if x["verdict"] == "PAUSE"]
mixedrows = [x for x in results if x["mixed_placement"]]
print(f"\nHEADLINE: turn off {len(pause)} keywords "
      f"(${sum(x['spend_30_day'] for x in pause):,.0f}/d spend, "
      f"{sum(x['gp_30_day'] for x in pause):+,.0f}/d GP at 30d "
      f"rate) · MIXED-PLACEMENT flags: {len(mixedrows)} "
      f"({sum(1 for x in mixedrows if 'disagree' in x['reason'])} "
      "of them rescued from PAUSE)")
print("PAUSE list = candidates for the cut-list workflow — dated, "
      "tripwire-watched; not auto-execution.")
print(f"\nWROTE: {d5}")

