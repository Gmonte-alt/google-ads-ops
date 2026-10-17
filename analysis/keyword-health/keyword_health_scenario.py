#!/usr/bin/env python
r"""
keyword_health_scenario.py v2 — Healthy-keyword conversions/day scenario
═══════════════════════════════════════════════════════════════════════
Question answered: if spend only flowed to keywords that are actually
healthy, what would expected conversions/day be — and what could the
freed budget earn if redeployed into measured-EXPANDABLE segments?

Companion to troas_target_gradient (target lever) and marginal_roas_map
(segment margins). MEASUREMENT/SCENARIO ONLY — no cuts recommended;
pause decisions stay with the cut-list workflow.

v2 (analyst review, 8/24) — all seven items:
  1. GRAIN FIX: keyword x MATCH TYPE x campaign (BROAD and EXACT of
     the same text are different auction objects; v1 merged them).
     n_instances column shows text duplicated across ad groups.
  2. PERF: integer GROUP BY (campaign, instance id) in SQL, text-level
     merge in Python; stage timers printed (query/merge/s3/chart/xlsx).
     The real fix is an index — request via the DB architect (see
     INDEX REQUEST below).
  3. Detail sheet capped (default 2000 rows by stability spend); the
     CSV always carries the full grain.
  4. FLOOR FRAMING: S1/S2 conv/day is a FLOOR and spend-saved an
     UPPER BOUND — the controller redeploys freed budget under tROAS;
     stated on the Summary sheet itself.
  5. S3 (DIRECTIONAL): S1's saved spend redeployed into EXPANDABLE
     segments at their pooled incremental ROAS from the marginal map,
     capacity-capped at each segment's observed absorption
     ($/day = mean over qualified episodes of dspend/win_days from the
     episodes CSV). Added conv/day = redeployed $ x inc-ROAS / segment
     RPL (recent-window, fallback campaign then account RPL).
     UNTESTED segments excluded and listed as probe candidates.
  6. PHASE-CONDITIONED flag: share of each keyword's stability spend
     that fell inside the cycle-state expansion windows; rows above
     --phase-share-flag (0.6) are flagged — dormant inventory judged
     mostly on expansion-phase economics, class provisional.
  7. PROTECTED COST: the protected-and-not-healthy subtotal (spend/day,
     GP/day, conv/day) shown on the Summary — the price of the
     exposure-recovery bet, stated rather than silently absorbed.

DESIGN (ratified 8/24):
  Health on the STABILITY window (post-cutover 2026-07-01 -> latest;
  min stability spend $100 else UNRATED): HEALTHY >= 1.00 ·
  BORDERLINE 0.85-1.00 · UNHEALTHY < 0.85. PROTECTED = Farmers
  home-intent (home/homeowner(s)/house token), kept in every scenario.
  Run-rate on trailing --recent-days (28). Conv = Leads = Conversions
  column; revenue as stored; GP = rev - spend (DB terms); break-even
  1.00; no lag exclusion (ruling 8/20).
  Scenarios: S0 current · S1 cut UNHEALTHY · S2 cut UNH+BORDERLINE
  (PROTECTED + UNRATED kept in all) · S3 = S1 + capacity-capped
  redeploy (directional).

INDEX REQUEST (route through the DB architect — shared infrastructure;
the access pattern is load-bearing across tripwire / cut list /
marginal map / this script):
  CREATE INDEX idx_kpd_dev_date
      ON keyword_performance_daily(device, report_date, instance_id);
  then ANALYZE. Pattern served: device='ALL' + report_date range +
  join on instance_id. Never create ad hoc from an analyst thread.

DATA RULES (house): kpd JOIN keyword_instances (account scope through
the join; kpd is Search-only), device='ALL'; campaign linkage =
ki.campaign_id else ad_groups fallback; revenue/conversions columns
auto-detected; DB mode=ro, never during a sync (a concurrent sync also
stalls readers — check before blaming the query).

OUTPUT: keyword_health_scenario_<date>.xlsx (Summary / Keyword Detail /
Notes) + keyword_health_detail_<date>.csv (full grain). Rerun after
cuts land or the next cycle closes.

USAGE:
  python keyword_health_scenario.py
  python keyword_health_scenario.py --min-spend 100 --recent-days 28
  python keyword_health_scenario.py --no-s3
  python keyword_health_scenario.py --map-csv <map.csv> --episodes-csv <eps.csv>
"""

import argparse
import csv
import io
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

try:
    from openpyxl import Workbook
    from openpyxl.drawing.image import Image as XLImage
    from openpyxl.styles import Alignment, Border, Font, PatternFill, \
        Side
    from openpyxl.utils import get_column_letter
except ImportError:
    sys.exit("openpyxl (with Pillow) required: "
             "pip install openpyxl pillow")

DEFAULT_DB_PATH = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")
DEFAULT_OUT_DIR = Path(r"C:\Projects\google-ads-ops\analysis"
                       r"\profit-attribution-tracker\output"
                       r"\keyword-health")
DEFAULT_MAP_DIR = Path(r"C:\Projects\google-ads-ops\analysis"
                       r"\profit-attribution-tracker\output"
                       r"\marginal-roas-map")
DEFAULT_CYCLE_STATE = Path(r"C:\Projects\google-ads-ops\analysis"
                           r"\profit-attribution-tracker\output"
                           r"\cycle-dashboard\cycle_state_latest.csv")
ACCOUNT_ID = 1
BREAK_EVEN = 1.00
ACCOUNT_LABEL = "ACCOUNT"

# Seven charter ids — verified via gradient v5.8 simulator cross-check.
CAMPAIGN_IDS = {
    "CALIFORNIA": 23385188622,
    "TEXAS":      23394808297,
    "FLORIDA":    23394808300,
    "TIER4":      23428389081,
    "TIER1":      23496334752,
    "TIER2":      23530860942,
    "TIER3":      23586637678,
}
CYCLE_ENTITY_SHORTS = ("TIER1", "TIER2", "TIER3", "TIER4",
                       "CALIFORNIA", "TEXAS", "FLORIDA")

# verbatim from carrier_tripwire.py (reconciles with daily monitoring)
CARRIER_PATTERNS = [
    ("Geico",        ["geico"]),
    ("Farmers",      ["farmers"]),
    ("State Farm",   ["state farm", "statefarm"]),
    ("Travelers",    ["travelers"]),
    ("Kin",          ["kin insurance", " kin ", "kin home"]),
    ("AAA",          ["aaa"]),
    ("Amica",        ["amica"]),
    ("Hartford",     ["hartford"]),
    ("Costco",       ["costco"]),
]
OTHER_LABEL = "Other/generic"
HOME_TOKENS = ("home", "homeowner", "homeowners", "house")

REV_CANDIDATES = ["conversion_value", "conversions_value", "conv_value",
                  "primary_conv_value", "total_conv_value",
                  "all_conv_value"]
CONV_CANDIDATES = ["conversions", "primary_conv_count", "conv_count",
                   "all_conversions"]

HEADER_FILL = PatternFill("solid", fgColor="1F3864")
HEADER_FONT = Font(bold=True, color="FFFFFF", size=10)
CAMP_FILL = PatternFill("solid", fgColor="D9E2F3")
CURR_FILL = PatternFill("solid", fgColor="E2EFDA")
NEG_FILL = PatternFill("solid", fgColor="F8CBAD")
WARN_FILL = PatternFill("solid", fgColor="FFF2CC")
S3_FILL = PatternFill("solid", fgColor="DEEBF7")
SOFT_FONT = Font(italic=True, color="808080", size=9)
THIN = Side(style="thin", color="BFBFBF")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
CLASS_FILL = {"HEALTHY": PatternFill("solid", fgColor="C6E0B4"),
              "BORDERLINE": PatternFill("solid", fgColor="FFF2CC"),
              "UNHEALTHY": PatternFill("solid", fgColor="F8CBAD"),
              "UNRATED": PatternFill("solid", fgColor="EDEDED")}


def connect_ro(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro",
                           uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def pick_col(conn, table, candidates):
    try:
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return None
    for c in candidates:
        if c in cols:
            return c
    return None


def has_col(conn, table, col):
    try:
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return False
    return col in cols


def pdate(s):
    return datetime.strptime(str(s)[:10], "%Y-%m-%d").date()


def classify(kw: str) -> str:
    k = f" {kw.lower().strip()} "
    for label, pats in CARRIER_PATTERNS:
        for p in pats:
            if p in k:
                return label
    return OTHER_LABEL


def is_protected(kw: str) -> bool:
    return classify(kw) == "Farmers" and \
        any(t in kw for t in HOME_TOKENS)


def health_class(roas, stab_spend, min_spend):
    if stab_spend < min_spend or roas is None:
        return "UNRATED"
    if roas >= BREAK_EVEN:
        return "HEALTHY"
    if roas >= 0.85:
        return "BORDERLINE"
    return "UNHEALTHY"


def fetch_targets(conn, cids):
    """Latest TARGET_ROAS new_value per campaign, platform='google' —
    the house-authoritative pattern (campaigns.target_roas is NULL for
    all 7; semantics per the target-gradient thread's ruling)."""
    out = {}
    if not has_col(conn, "campaign_change_events", "change_type"):
        return out
    val_col = pick_col(conn, "campaign_change_events",
                       ["new_value", "value"])
    if not val_col:
        print("  WARN: campaign_change_events has neither new_value "
              "nor value — Target column blank")
        return out
    time_col = pick_col(conn, "campaign_change_events",
                        ["event_time", "event_date", "change_datetime",
                         "created_at"]) or "rowid"
    plat = "AND platform = 'google'" if has_col(
        conn, "campaign_change_events", "platform") else ""
    try:
        q = (f"SELECT campaign_id, {val_col} v "
             f"FROM campaign_change_events "
             f"WHERE change_type = 'TARGET_ROAS' {plat} "
             f"AND campaign_id IN ({','.join('?' * len(cids))}) "
             f"ORDER BY {time_col}")
        for r in conn.execute(q, list(cids)):
            try:
                v = float(r["v"])
            except (TypeError, ValueError):
                continue
            out[int(r["campaign_id"])] = v / 100.0 if v > 10 else v
        print(f"  targets: campaign_change_events.{val_col} (latest "
              f"per campaign{', google only' if plat else ''}) — "
              f"{len(out)}/{len(cids)} resolved")
    except sqlite3.Error as e:
        print(f"  WARN: target fetch failed ({e}) — Target column "
              "blank")
    return out


def load_map_data(args):
    """{(short, segment): {"class", "inc"}} from the latest map CSV."""
    p = None
    if args.map_csv:
        p = Path(args.map_csv)
        if not p.exists():
            print(f"  WARN: --map-csv not found: {p}")
            return {}, None
    else:
        d = Path(args.map_dir)
        if d.exists():
            cands = sorted(d.glob("marginal_roas_map_*.csv"),
                           key=lambda x: x.stat().st_mtime,
                           reverse=True)
            p = cands[0] if cands else None
    if not p:
        return {}, None
    out = {}
    try:
        with open(p, newline="", encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                try:
                    inc = float(r.get("pooled_inc_roas") or "")
                except ValueError:
                    inc = None
                out[(r.get("campaign", ""), r.get("segment", ""))] = {
                    "class": r.get("class", ""), "inc": inc}
    except OSError as e:
        print(f"  WARN: could not read map CSV {p}: {e}")
        return {}, None
    return out, p


def load_episode_caps(args, map_csv_path):
    """{(short, segment): cap $/day} = mean over qualified episodes of
    dspend / win_days, from the episodes CSV."""
    p = None
    if args.episodes_csv:
        p = Path(args.episodes_csv)
        if not p.exists():
            print(f"  WARN: --episodes-csv not found: {p}")
            return {}, None
    elif map_csv_path:
        cand = map_csv_path.with_name(
            map_csv_path.name.replace("marginal_roas_map_",
                                      "episodes_"))
        if cand.exists():
            p = cand
    if not p:
        d = Path(args.map_dir)
        if d.exists():
            cands = sorted(d.glob("episodes_*.csv"),
                           key=lambda x: x.stat().st_mtime,
                           reverse=True)
            p = cands[0] if cands else None
    if not p:
        return {}, None
    vals = {}
    try:
        with open(p, newline="", encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                if str(r.get("qualified", "")).strip().lower() != \
                        "true":
                    continue
                try:
                    ds = float(r.get("dspend") or 0)
                    wd = float(r.get("win_days") or 0)
                except ValueError:
                    continue
                if wd <= 0 or ds <= 0:
                    continue
                vals.setdefault((r.get("campaign", ""),
                                 r.get("segment", "")), []).append(
                    ds / wd)
    except OSError as e:
        print(f"  WARN: could not read episodes CSV {p}: {e}")
        return {}, None
    return {k: sum(v) / len(v) for k, v in vals.items()}, p


def load_cycle_windows(path, camp_ids):
    """{cid: [(ws, we)]} expansion windows from the cycle-state file."""
    p = Path(path)
    if not p.exists():
        return {}

    def to_short(ent):
        e = (ent or "").strip().upper()
        for s in CYCLE_ENTITY_SHORTS:
            if e.startswith(s):
                return s
        return None

    out = {}
    try:
        with open(p, newline="", encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                if (r.get("record") or "").strip().upper() != "WINDOW":
                    continue
                short = to_short(r.get("entity"))
                cid = camp_ids.get(short) if short else None
                if not cid:
                    continue
                try:
                    out.setdefault(cid, []).append(
                        (pdate(r["date_start"]), pdate(r["date_end"])))
                except (KeyError, ValueError):
                    continue
    except OSError:
        return {}
    return out


def kw_campaign_join(conn):
    if has_col(conn, "keyword_instances", "campaign_id"):
        return "", "ki.campaign_id"
    return ("JOIN ad_groups ag ON ag.id = ki.ad_group_id",
            "ag.campaign_id")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--db-path", default=str(DEFAULT_DB_PATH))
    p.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    p.add_argument("--stability-start", default="2026-07-01")
    p.add_argument("--recent-days", type=int, default=28)
    p.add_argument("--min-spend", type=float, default=100.0)
    p.add_argument("--map", action="append",
                   help='override ids: --map "TIER2=123" (repeatable)')
    p.add_argument("--map-csv", default=None)
    p.add_argument("--episodes-csv", default=None)
    p.add_argument("--map-dir", default=str(DEFAULT_MAP_DIR))
    p.add_argument("--cycle-state", default=str(DEFAULT_CYCLE_STATE))
    p.add_argument("--phase-share-flag", type=float, default=0.6,
                   help="flag keywords with >= this share of stability "
                        "spend inside expansion windows as "
                        "phase-conditioned (class provisional)")
    p.add_argument("--no-s3", action="store_true",
                   help="skip the directional redeploy scenario")
    p.add_argument("--detail-rows", type=int, default=2000)
    p.add_argument("--target-span", type=float, default=0.12,
                   help="target-view ladder: current +/- this many "
                        "ROAS points")
    p.add_argument("--target-rungs", type=int, default=9)
    p.add_argument("--eps", action="append",
                   help='override target-view elasticity: --eps '
                        '"TIER2=2.5" (repeatable)')
    p.add_argument("--eps-default", type=float, default=2.4)
    p.add_argument("--no-target-view", action="store_true")
    p.add_argument("--qs-low", type=int, default=4,
                   help="QS bucket edge: <= this = LOW")
    p.add_argument("--qs-high", type=int, default=7,
                   help="QS bucket edge: >= this = HIGH")
    p.add_argument("--no-qs-view", action="store_true")
    p.add_argument("--indexed-by", default=None,
                   help="force an index on the hot kpd query, e.g. "
                        "idx_kpd_dev_date — per architect protocol, "
                        "use ONLY if the query timer stays slow after "
                        "the index (SQLite honors it as a hard hint)")
    args = p.parse_args()

    camp_ids = dict(CAMPAIGN_IDS)
    for m in args.map or []:
        k, v = m.split("=", 1)
        camp_ids[k.strip().upper()] = int(v.strip())

    db_path = Path(args.db_path)
    if not db_path.exists():
        sys.exit(f"DB not found: {db_path}")
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    conn = connect_ro(db_path)

    rev_col = pick_col(conn, "keyword_performance_daily",
                       REV_CANDIDATES)
    conv_col = pick_col(conn, "keyword_performance_daily",
                        CONV_CANDIDATES)
    if not rev_col or not conv_col:
        sys.exit(f"kpd columns missing (rev={rev_col}, "
                 f"conv={conv_col})")
    print(f"kpd columns: revenue={rev_col}, conversions={conv_col} "
          "(Leads = Conversions column)")

    row = conn.execute(
        """SELECT MAX(p.report_date) FROM keyword_performance_daily p
           JOIN keyword_instances ki ON ki.id = p.instance_id
           WHERE ki.account_id = ?""", [ACCOUNT_ID]).fetchone()
    if not row or not row[0]:
        sys.exit("No kpd data found.")
    data_end = pdate(row[0])
    stab_start = pdate(args.stability_start)
    recent_start = data_end - timedelta(days=args.recent_days - 1)
    print(f"Data through {data_end}; stability {stab_start}->"
          f"{data_end}; recent {recent_start}->{data_end} "
          f"({args.recent_days}d)")

    camp_table = "campaigns_search"
    try:
        conn.execute("SELECT 1 FROM campaigns_search LIMIT 1")
    except sqlite3.Error:
        camp_table = "campaigns"
    names = {}
    ids = list(camp_ids.values())
    for r in conn.execute(
            f"SELECT campaign_id, campaign_name FROM {camp_table} "
            f"WHERE campaign_id IN ({','.join('?' * len(ids))})", ids):
        names[int(r["campaign_id"])] = r["campaign_name"] or ""
    short_of = {cid: s for s, cid in camp_ids.items()}
    for s, cid in sorted(camp_ids.items()):
        print(f"  {s:11s} {cid}  "
              f"'{names.get(cid, 'NOT FOUND — verify id')}'")

    targets = fetch_targets(conn, ids)
    map_data, map_csv_path = load_map_data(args)
    if map_csv_path:
        print(f"  Segment marginal classes: {map_csv_path.name}")
    else:
        print("  NOTE: no marginal_roas_map CSV — segment context "
              "blank; S3 unavailable")
    cyc_windows = load_cycle_windows(args.cycle_state, camp_ids)
    if cyc_windows:
        nwin = sum(len(v) for v in cyc_windows.values())
        print(f"  Cycle windows for phase-conditioning: {nwin} from "
              f"{Path(args.cycle_state).name}")
    else:
        print("  NOTE: no cycle-state file — phase-conditioned column "
              "blank")

    # query: integer GROUP BY; text merge in Python (item 2b);
    # window-membership spend for phase-conditioning (item 6)
    t0 = time.perf_counter()
    join_extra, camp_expr = kw_campaign_join(conn)
    win_terms, win_params = [], []
    for cid, wins in cyc_windows.items():
        for ws_, we_ in wins:
            win_terms.append(f"({camp_expr} = ? AND p.report_date "
                             "BETWEEN ? AND ?)")
            win_params += [cid, ws_.isoformat(), we_.isoformat()]
    win_pred = "(" + " OR ".join(win_terms) + ")" if win_terms else \
        "(1=0)"
    ss, rs = stab_start.isoformat(), recent_start.isoformat()
    q = (f"SELECT {camp_expr} cid, ki.id iid, ki.keyword_text kw_raw, "
         f"ki.match_type mt, "
         f"SUM(CASE WHEN p.report_date >= ? THEN p.cost_micros "
         f"ELSE 0 END)/1e6 s_stab, "
         f"SUM(CASE WHEN p.report_date >= ? THEN p.{rev_col} "
         f"ELSE 0 END) r_stab, "
         f"SUM(CASE WHEN p.report_date >= ? AND {win_pred} "
         f"THEN p.cost_micros ELSE 0 END)/1e6 s_stab_win, "
         f"SUM(CASE WHEN p.report_date >= ? THEN p.cost_micros "
         f"ELSE 0 END)/1e6 s_rec, "
         f"SUM(CASE WHEN p.report_date >= ? THEN p.{rev_col} "
         f"ELSE 0 END) r_rec, "
         f"SUM(CASE WHEN p.report_date >= ? THEN p.{conv_col} "
         f"ELSE 0 END) c_rec "
         f"FROM keyword_performance_daily p "
         + (f"INDEXED BY {args.indexed_by} " if args.indexed_by
            else "")
         + f"JOIN keyword_instances ki ON ki.id = p.instance_id "
         f"AND ki.account_id = ? {join_extra} "
         f"WHERE p.device = 'ALL' AND p.report_date >= ? "
         f"AND {camp_expr} IN ({','.join('?' * len(ids))}) "
         f"GROUP BY 1, 2")
    params = [ss, ss, ss] + win_params + \
        [rs, rs, rs, ACCOUNT_ID, ss] + ids
    rows = conn.execute(q, params).fetchall()
    conn.close()
    t_query = time.perf_counter() - t0
    print(f"  instance rows: {len(rows):,}  [query {t_query:.1f}s]")

    # merge to keyword x match_type x campaign (item 1)
    t0 = time.perf_counter()
    nd = float(args.recent_days)
    merged = {}
    iid_key = {}
    for r in rows:
        cid = int(r["cid"])
        short = short_of.get(cid)
        if not short:
            continue
        kw = (r["kw_raw"] or "").strip().lower()
        mt = str(r["mt"] or "?").strip().upper()
        key = (short, cid, mt, kw)
        iid_key[int(r["iid"])] = key
        m = merged.setdefault(key, {
            "s_stab": 0.0, "r_stab": 0.0, "s_stab_win": 0.0,
            "s_rec": 0.0, "r_rec": 0.0, "c_rec": 0.0, "iids": set()})
        m["s_stab"] += r["s_stab"] or 0.0
        m["r_stab"] += r["r_stab"] or 0.0
        m["s_stab_win"] += r["s_stab_win"] or 0.0
        m["s_rec"] += r["s_rec"] or 0.0
        m["r_rec"] += r["r_rec"] or 0.0
        m["c_rec"] += r["c_rec"] or 0.0
        m["iids"].add(int(r["iid"]))

    # QS layer: impression-weighted per keyword over the stability
    # window (house recipe: kqd joined (instance_id, report_date),
    # weighted by kpd impressions, QS never summed, unrated excluded)
    qs_by_key = {}
    if not args.no_qs_view:
        connq = connect_ro(db_path)
        impr_col = pick_col(connq, "keyword_performance_daily",
                            ["impressions", "impr"])
        if impr_col and has_col(connq, "keyword_quality_daily",
                                "historical_quality_score"):
            qq = (f"SELECT p.instance_id iid, "
                  f"SUM(CASE WHEN q.historical_quality_score IS NOT "
                  f"NULL THEN p.{impr_col} ELSE 0 END) ri, "
                  f"SUM(p.{impr_col}) ti, "
                  f"SUM(CASE WHEN q.historical_quality_score IS NOT "
                  f"NULL THEN q.historical_quality_score * "
                  f"p.{impr_col} ELSE 0 END) qi "
                  f"FROM keyword_performance_daily p "
                  f"LEFT JOIN keyword_quality_daily q "
                  f"ON q.instance_id = p.instance_id "
                  f"AND q.report_date = p.report_date "
                  f"WHERE p.device = 'ALL' AND p.report_date >= ? "
                  f"GROUP BY 1")
            acc = {}
            for r in connq.execute(qq, [ss]):
                key = iid_key.get(int(r["iid"]))
                if not key:
                    continue
                a = acc.setdefault(key, [0.0, 0.0, 0.0])
                a[0] += r["ri"] or 0.0
                a[1] += r["ti"] or 0.0
                a[2] += r["qi"] or 0.0
            for key, (ri, ti, qi) in acc.items():
                iwqs = (qi / ri) if ri > 0 else None
                qs_by_key[key] = {
                    "iw_qs": iwqs,
                    "rated_share": (ri / ti) if ti > 0 else None}
            print(f"  QS layer: {sum(1 for v in qs_by_key.values() if v['iw_qs'] is not None):,} "
                  "rated keywords (impression-weighted, house recipe)")
        else:
            print("  NOTE: QS layer unavailable (missing impressions "
                  "column or keyword_quality_daily) — QS view skipped")
        connq.close()

    def qs_bucket(v):
        if v is None:
            return "No QS"
        if v <= args.qs_low:
            return f"QS<={args.qs_low}"
        if v < args.qs_high:
            return f"QS {args.qs_low + 1}-{args.qs_high - 1}"
        return f"QS {args.qs_high}+"

    kws = []
    for (short, cid, mt, kw), m in merged.items():
        roas_stab = (m["r_stab"] / m["s_stab"]) if m["s_stab"] > 0 \
            else None
        cls = health_class(roas_stab, m["s_stab"], args.min_spend)
        seg = classify(kw)
        info = map_data.get((short, seg)) or \
            (map_data.get((short, "Farmers excl. flagship"))
             if seg == "Farmers" else None) or \
            map_data.get((ACCOUNT_LABEL, seg)) or {}
        phase_share = (m["s_stab_win"] / m["s_stab"]) \
            if m["s_stab"] > 0 else None
        tgt = targets.get(cid)
        kws.append({
            "campaign": short, "keyword": kw, "match_type": mt,
            "n_instances": len(m["iids"]), "segment": seg,
            "protected": is_protected(kw), "class": cls,
            "phase_share": phase_share,
            "phase_conditioned": bool(
                phase_share is not None and cls != "UNRATED"
                and phase_share >= args.phase_share_flag),
            "stab_spend": m["s_stab"], "stab_rev": m["r_stab"],
            "stab_roas": roas_stab, "campaign_target": tgt,
            "vs_target": (roas_stab - tgt)
            if (roas_stab is not None and tgt) else None,
            "seg_marginal_class": info.get("class", ""),
            "rec_spend_day": m["s_rec"] / nd,
            "rec_rev_day": m["r_rec"] / nd,
            "rec_conv_day": m["c_rec"] / nd,
            "rec_gp_day": (m["r_rec"] - m["s_rec"]) / nd,
            "iw_qs": qs_by_key.get((short, cid, mt, kw),
                                   {}).get("iw_qs"),
            "qs_rated_share": qs_by_key.get((short, cid, mt, kw),
                                            {}).get("rated_share"),
            "qs_bucket": qs_bucket(
                qs_by_key.get((short, cid, mt, kw), {}).get("iw_qs")),
        })
    t_merge = time.perf_counter() - t0
    print(f"  keyword x match x campaign rows: {len(kws):,}  "
          f"[merge {t_merge:.1f}s]")

    def keep(k, scen):
        if scen == "S0":
            return True
        if k["protected"] or k["class"] == "UNRATED":
            return True
        if scen == "S1":
            return k["class"] != "UNHEALTHY"
        return k["class"] == "HEALTHY"          # S2

    def agg(sel):
        s = sum(k["rec_spend_day"] for k in sel)
        rv = sum(k["rec_rev_day"] for k in sel)
        cv = sum(k["rec_conv_day"] for k in sel)
        return {"spend": s, "rev": rv, "conv": cv, "gp": rv - s,
                "roas": (rv / s) if s > 0 else None}

    shorts = sorted(camp_ids)
    scen_rows = []
    base_by = {}
    for short in shorts + [ACCOUNT_LABEL]:
        sel_all = kws if short == ACCOUNT_LABEL else \
            [k for k in kws if k["campaign"] == short]
        base = agg(sel_all)
        base_by[short] = base
        for scen in ("S0", "S1", "S2"):
            sel = [k for k in sel_all if keep(k, scen)]
            a = agg(sel)
            scen_rows.append({
                "campaign": short, "scenario": scen, **a,
                "conv_retained": (a["conv"] / base["conv"])
                if base["conv"] > 0 else None,
                "spend_saved": base["spend"] - a["spend"],
                "gp_change": a["gp"] - base["gp"],
                "kws_cut": sum(1 for k in sel_all
                               if not keep(k, scen)),
                "note": ""})

    # S3: capacity-capped redeploy (item 5, directional)
    t0 = time.perf_counter()
    s3_alloc_lines, probe_lines = [], []
    ep_caps, ep_csv_path = ({}, None) if args.no_s3 else \
        load_episode_caps(args, map_csv_path)
    s3_available = bool(map_data and ep_caps and not args.no_s3)
    if s3_available and ep_csv_path:
        print(f"  S3 capacity from {ep_csv_path.name}")

    def pool_rpl(pool, min_conv):
        cv = sum(k["rec_conv_day"] for k in pool)
        rv = sum(k["rec_rev_day"] for k in pool)
        return (rv / cv) if cv >= min_conv else None

    def seg_rpl(short, seg):
        """(rpl, source). Segment RPL needs >= 0.5 conv/day of recent
        volume AND must sit within 0.5-2x campaign RPL — small-sample
        segment RPLs otherwise inflate/deflate S3 conv gains."""
        camp = pool_rpl([k for k in kws if k["campaign"] == short],
                        1.0)
        acct = pool_rpl(kws, 1.0)
        base = camp if camp else acct
        segp = pool_rpl([k for k in kws if k["campaign"] == short
                         and k["segment"] == seg], 0.5)
        if segp and base and 0.5 * base <= segp <= 2.0 * base:
            return segp, "seg"
        if base:
            return base, ("camp" if camp else "acct")
        return None, ""

    s3_by = {}
    if s3_available:
        for short in shorts:
            s1 = next(r for r in scen_rows if r["campaign"] == short
                      and r["scenario"] == "S1")
            saved = s1["spend_saved"]
            segs = []
            for (mshort, seg), info in map_data.items():
                if mshort != short or \
                        info.get("class") != "EXPANDABLE":
                    continue
                cap = ep_caps.get((mshort, seg))
                if cap and info.get("inc"):
                    segs.append((seg, info["inc"], cap))
            segs.sort(key=lambda x: -x[1])
            remaining, add_conv, add_gp, add_spend = \
                saved, 0.0, 0.0, 0.0
            for seg, inc, cap in segs:
                if remaining <= 0:
                    break
                alloc = min(cap, remaining)
                rpl, rpl_src = seg_rpl(short, seg)
                if not rpl:
                    continue
                add_spend += alloc
                add_conv += alloc * inc / rpl
                add_gp += alloc * (inc - 1.0)
                remaining -= alloc
                tag = "" if rpl_src == "seg" else f" ({rpl_src} RPL)"
                s3_alloc_lines.append(
                    f"{short}: ${alloc:,.0f}/d -> {seg} @ inc "
                    f"{inc:.2f} (cap ${cap:,.0f}/d, "
                    f"RPL ${rpl:,.0f}{tag}) = "
                    f"+{alloc * inc / rpl:.1f} conv/d")
            s3_by[short] = {"spend": add_spend, "conv": add_conv,
                            "gp": add_gp, "unredeployed": remaining,
                            "s1": s1}
            unt = sorted(seg for (msh, seg), info in map_data.items()
                         if msh == short
                         and info.get("class") == "UNTESTED")
            if unt:
                probe_lines.append(f"{short}: " + ", ".join(unt))

        acct = {"spend": 0.0, "conv": 0.0, "gp": 0.0,
                "unredeployed": 0.0}
        s3_rows = []
        for short in shorts:
            if short not in s3_by:
                continue
            part = s3_by[short]
            s1 = part["s1"]
            for kk in ("spend", "conv", "gp", "unredeployed"):
                acct[kk] += part[kk]
            base = base_by[short]
            conv = s1["conv"] + part["conv"]
            spend = s1["spend"] + part["spend"]
            gp = s1["gp"] + part["gp"]
            s3_rows.append({
                "campaign": short, "scenario": "S3*",
                "spend": spend, "rev": gp + spend, "conv": conv,
                "gp": gp,
                "roas": ((gp + spend) / spend) if spend > 0 else None,
                "conv_retained": (conv / base["conv"])
                if base["conv"] > 0 else None,
                "spend_saved": part["unredeployed"],
                "gp_change": gp - base["gp"],
                "kws_cut": s1["kws_cut"], "note": "directional"})
        s1a = next(r for r in scen_rows
                   if r["campaign"] == ACCOUNT_LABEL
                   and r["scenario"] == "S1")
        base = base_by[ACCOUNT_LABEL]
        conv = s1a["conv"] + acct["conv"]
        spend = s1a["spend"] + acct["spend"]
        gp = s1a["gp"] + acct["gp"]
        s3_rows.append({
            "campaign": ACCOUNT_LABEL, "scenario": "S3*",
            "spend": spend, "rev": gp + spend, "conv": conv, "gp": gp,
            "roas": ((gp + spend) / spend) if spend > 0 else None,
            "conv_retained": (conv / base["conv"])
            if base["conv"] > 0 else None,
            "spend_saved": acct["unredeployed"],
            "gp_change": gp - base["gp"],
            "kws_cut": s1a["kws_cut"], "note": "directional"})
        scen_rows += s3_rows
        order = {"S0": 0, "S1": 1, "S2": 2, "S3*": 3}
        seq = shorts + [ACCOUNT_LABEL]
        scen_rows.sort(key=lambda r: (seq.index(r["campaign"]),
                                      order[r["scenario"]]))
    t_s3 = time.perf_counter() - t0

    unrated_conv = sum(k["rec_conv_day"] for k in kws
                       if k["class"] == "UNRATED")
    total_conv = sum(k["rec_conv_day"] for k in kws)
    prot_bad = [k for k in kws if k["protected"]
                and k["class"] in ("UNHEALTHY", "BORDERLINE")]
    prot_bad_spend = sum(k["rec_spend_day"] for k in prot_bad)
    prot_bad_gp = sum(k["rec_gp_day"] for k in prot_bad)
    prot_bad_conv = sum(k["rec_conv_day"] for k in prot_bad)
    n_phase = sum(1 for k in kws if k["phase_conditioned"])

    # outputs
    stamp = data_end.isoformat()
    detail_csv = out_dir / f"keyword_health_detail_{stamp}.csv"
    fields = list(kws[0].keys()) if kws else []
    with open(detail_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(sorted(kws, key=lambda k: -k["stab_spend"]))

    t0 = time.perf_counter()
    fig, ax = plt.subplots(figsize=(9.6, 3.6), dpi=150)
    x = range(len(shorts))
    series = [("S0", "#1F3864"), ("S1", "#2a78d6"),
              ("S2", "#9DC3E6")] + \
        ([("S3*", "#0ca30c")] if s3_available else [])
    wbar = 0.8 / len(series)
    for i, (scen, col) in enumerate(series):
        vals = [next((r["conv"] for r in scen_rows
                      if r["campaign"] == s
                      and r["scenario"] == scen), 0.0)
                for s in shorts]
        ax.bar([xi + (i - (len(series) - 1) / 2) * wbar for xi in x],
               vals, width=wbar * 0.92, color=col,
               label={"S0": "S0 current",
                      "S1": "S1 floor (cut unh.)",
                      "S2": "S2 floor (cut unh.+bord.)",
                      "S3*": "S3* redeploy (directional)"}[scen])
    ax.set_xticks(list(x))
    ax.set_xticklabels(shorts, fontsize=8)
    ax.set_ylabel("conv/day (leads)", fontsize=8)
    ax.set_title("Expected conversions/day by scenario "
                 f"(recent {args.recent_days}d run-rate)", fontsize=10)
    ax.legend(fontsize=7)
    ax.grid(axis="y", alpha=0.3)
    buf = io.BytesIO()
    fig.tight_layout()
    fig.savefig(buf, format="png")
    plt.close(fig)
    buf.seek(0)
    t_chart = time.perf_counter() - t0

    # panel grid: conv/day + GP/day per scenario, per campaign
    import math as _math
    panel_entities = shorts + [ACCOUNT_LABEL]
    ncols = 2
    nrows = _math.ceil(len(panel_entities) / ncols)
    figp, axes = plt.subplots(nrows, ncols,
                              figsize=(11.5, 3.0 * nrows), dpi=150)
    axes = axes.flatten()
    scen_labels = {"S0": "S0\ncurrent", "S1": "S1\nfloor",
                   "S2": "S2\nfloor", "S3*": "S3*\ndirectional"}
    for ax, ent in zip(axes, panel_entities):
        ent_rows = [r for r in scen_rows if r["campaign"] == ent]
        xs = list(range(len(ent_rows)))
        conv = [r["conv"] for r in ent_rows]
        gp = [r["gp"] for r in ent_rows]
        ax.plot(xs, conv, "-o", color="#1F4E79", lw=2, ms=5,
                label="conv/day", zorder=3)
        for xi, v in zip(xs, conv):
            ax.annotate(f"{v:,.1f}", (xi, v), textcoords="offset "
                        "points", xytext=(0, 7), ha="center",
                        fontsize=7, color="#1F4E79",
                        annotation_clip=False)
        ax2 = ax.twinx()
        ax2.plot(xs, gp, "--s", color="#2E7D32", lw=1.6, ms=4,
                 label="GP/day", alpha=0.9)
        for xi, v in zip(xs, gp):
            ax2.annotate(f"{v:+,.0f}", (xi, v), textcoords="offset "
                         "points", xytext=(0, -11), ha="center",
                         fontsize=6.5, color="#2E7D32",
                         annotation_clip=False)
        if min(gp) < 0:
            ax2.axhline(0, color="#C00000", lw=0.8, ls=":",
                        alpha=0.7)
        ax.set_xticks(xs)
        ax.set_xticklabels([scen_labels.get(r["scenario"],
                                            r["scenario"])
                            for r in ent_rows], fontsize=7.5)
        s1r = next((r for r in ent_rows if r["scenario"] == "S1"),
                   None)
        sub = (f"  [{s1r['kws_cut']} kws cut at S1]"
               if s1r and s1r["kws_cut"] else "")
        ax.set_title(f"{ent}{sub}", fontsize=10, fontweight="bold")
        ax.set_ylabel("conv/day", fontsize=7.5, color="#1F4E79")
        ax2.set_ylabel("GP $/day", fontsize=7.5, color="#2E7D32")
        ax.tick_params(labelsize=7.5)
        ax2.tick_params(labelsize=7.5)
        ax.grid(axis="y", alpha=0.25)
        ax.margins(x=0.08, y=0.30)
    for ax in axes[len(panel_entities):]:
        ax.axis("off")
    figp.suptitle(
        "Keyword health scenarios — conv/day (blue, left) and GP/day "
        "(green, right) per scenario.\nS1/S2 = subtractive floors "
        "(no redeployment; spend-saved is an upper bound). S3* = "
        "directional capacity-capped redeploy. Not a forecast.",
        fontsize=10.5)
    figp.tight_layout(rect=(0, 0, 1, 0.94))
    panels_png = out_dir / f"keyword_health_panels_{stamp}.png"
    figp.savefig(panels_png)
    plt.close(figp)

    # ── S0-vs-S1 target view: conv/day + GP/day across candidate
    # tROAS targets, per campaign + ACCOUNT (delta-pts). Volume model
    # per the gradient thread: conv mult = (t/t0)^(-eps); portfolio
    # RPL constant; expected ROAS = realized x (t/t0); spend =
    # rev/ROAS. eps defaults = simulator-local values (gradient
    # thread, 8/13). DIRECTIONAL, not a forecast; S1 curve inherits
    # the floor caveat (real post-cut conv >= modeled).
    EPS_SIM = {"CALIFORNIA": 2.0, "FLORIDA": 2.6, "TEXAS": 3.1,
               "TIER1": 2.2, "TIER2": 2.1, "TIER3": 2.5, "TIER4": 2.5}
    for e in (args.eps or []):
        k, v = e.split("=", 1)
        EPS_SIM[k.strip().upper()] = float(v.strip())
    tv_png = None
    tv_lines = []
    tv_entities = [s for s in shorts if targets.get(camp_ids[s])
                   and base_by[s]["conv"] > 0]
    if tv_entities and not args.no_target_view:
        def curves(base, t0_, eps, ts):
            r0 = base["roas"] or 0
            out = []
            for t in ts:
                m = (t / t0_) ** (-eps)
                conv = base["conv"] * m
                rev = base["rev"] * m
                roas = r0 * (t / t0_)
                spend = rev / roas if roas > 0 else 0.0
                out.append((t, conv, rev - spend))
            return out

        ncols2 = 2
        nrows2 = _math.ceil((len(tv_entities) + 1) / ncols2)
        figt, axest = plt.subplots(nrows2, ncols2,
                                   figsize=(11.5, 3.0 * nrows2),
                                   dpi=150)
        axest = axest.flatten()
        acct_acc = {}
        for ax, ent in zip(axest, tv_entities):
            t0_ = targets[camp_ids[ent]]
            eps = EPS_SIM.get(ent, args.eps_default)
            span, n = args.target_span, args.target_rungs
            ts = [t0_ - span + i * (2 * span / (n - 1))
                  for i in range(n)]
            ts = [t for t in ts if t > 0.5]
            b0 = base_by[ent]
            s1r = next(r for r in scen_rows if r["campaign"] == ent
                       and r["scenario"] == "S1")
            b1 = {"conv": s1r["conv"], "rev": s1r["rev"],
                  "roas": s1r["roas"]}
            c0 = curves(b0, t0_, eps, ts)
            c1 = curves(b1, t0_, eps, ts)
            for i, t in enumerate(ts):
                d = round(t - t0_, 4)
                a = acct_acc.setdefault(i - (len(ts) - 1) // 2,
                                        [0.0, 0.0, 0.0, 0.0])
                a[0] += c0[i][1]
                a[1] += c0[i][2]
                a[2] += c1[i][1]
                a[3] += c1[i][2]
            ax.plot([c[0] for c in c0], [c[1] for c in c0], "-o",
                    color="#1F4E79", lw=2, ms=3.5, label="S0 conv/d")
            ax.plot([c[0] for c in c1], [c[1] for c in c1], "--o",
                    color="#6FA8DC", lw=1.8, ms=3.5,
                    label="S1 conv/d")
            ax2 = ax.twinx()
            ax2.plot([c[0] for c in c0], [c[2] for c in c0], "-s",
                     color="#2E7D32", lw=1.6, ms=3, alpha=0.9,
                     label="S0 GP/d")
            ax2.plot([c[0] for c in c1], [c[2] for c in c1], "--s",
                     color="#7CBF7C", lw=1.5, ms=3, alpha=0.9,
                     label="S1 GP/d")
            ax.axvline(t0_, color="#404040", ls="-.", lw=1)
            # volume-neutral target for the S1 portfolio
            if b1["conv"] > 0 and b0["conv"] > 0:
                tstar = t0_ * (b1["conv"] / b0["conv"]) ** (1 / eps)
                if ts[0] <= tstar <= ts[-1]:
                    gstar = curves(b1, t0_, eps, [tstar])[0][2]
                    ax.plot([tstar], [b0["conv"]], marker="*", ms=13,
                            color="#C55A11", zorder=5)
                    tv_lines.append(
                        f"{ent}: post-cut volume-neutral target "
                        f"~{tstar:.2f} (from {t0_:.2f}) -> S0 volume "
                        f"({b0['conv']:,.1f}/d) at GP "
                        f"${gstar:,.0f}/d vs S0 GP "
                        f"${b0['gp']:,.0f}/d")
            ax.set_title(f"{ent}  [t {t0_:.2f}, eps {eps:g}]",
                         fontsize=10, fontweight="bold")
            ax.set_ylabel("conv/day", fontsize=7.5, color="#1F4E79")
            ax2.set_ylabel("GP $/day", fontsize=7.5, color="#2E7D32")
            ax.tick_params(labelsize=7.5)
            ax2.tick_params(labelsize=7.5)
            ax.grid(axis="y", alpha=0.25)
            if min(c[2] for c in c1 + c0) < 0:
                ax2.axhline(0, color="#C00000", lw=0.8, ls=":")
            if ent == tv_entities[0]:
                h1, l1 = ax.get_legend_handles_labels()
                h2, l2 = ax2.get_legend_handles_labels()
                ax.legend(h1 + h2, l1 + l2, fontsize=6.5,
                          loc="upper right")
        # ACCOUNT panel in delta-points
        axA = axest[len(tv_entities)]
        ks = sorted(acct_acc)
        dx = [k * (2 * args.target_span / (args.target_rungs - 1))
              for k in ks]
        axA.plot(dx, [acct_acc[k][0] for k in ks], "-o",
                 color="#1F4E79", lw=2, ms=3.5)
        axA.plot(dx, [acct_acc[k][2] for k in ks], "--o",
                 color="#6FA8DC", lw=1.8, ms=3.5)
        axA2 = axA.twinx()
        axA2.plot(dx, [acct_acc[k][1] for k in ks], "-s",
                  color="#2E7D32", lw=1.6, ms=3)
        axA2.plot(dx, [acct_acc[k][3] for k in ks], "--s",
                  color="#7CBF7C", lw=1.5, ms=3)
        axA.axvline(0, color="#404040", ls="-.", lw=1)
        axA.set_title("ACCOUNT (delta target, pts)", fontsize=10,
                      fontweight="bold")
        axA.set_ylabel("conv/day", fontsize=7.5, color="#1F4E79")
        axA2.set_ylabel("GP $/day", fontsize=7.5, color="#2E7D32")
        axA.tick_params(labelsize=7.5)
        axA2.tick_params(labelsize=7.5)
        axA.grid(axis="y", alpha=0.25)
        for ax in axest[len(tv_entities) + 1:]:
            ax.axis("off")
        figt.suptitle(
            "Target view — conv/day (blue) and GP/day (green) across "
            "candidate tROAS targets:\ncurrent portfolio (S0, solid) "
            "vs post-S1-cut portfolio (dashed). star = post-cut "
            "target restoring S0 volume.\nModel: conv mult = "
            "(t/current)^(-eps), portfolio RPL constant, ROAS scales "
            "with target; eps = simulator-local. DIRECTIONAL, not a "
            "forecast.", fontsize=9.5)
        figt.tight_layout(rect=(0, 0, 1, 0.91))
        tv_png = out_dir / f"keyword_health_target_view_{stamp}.png"
        figt.savefig(tv_png)
        plt.close(figt)
    elif not args.no_target_view:
        print("  NOTE: target view skipped (no resolved targets)")

    # ── QS target view: GP/day (solid, left) + conv/day (faint
    # dashed, right) per QS bucket across candidate targets. Each
    # bucket = its own sub-portfolio (impression-weighted keyword QS
    # over the stability window -> bucket), same ladder model. Same
    # campaign eps applied to every bucket — differences come from
    # each bucket's realized economics, not differential elasticity
    # (in reality low-QS inventory likely responds MORE to
    # loosening: expansions concentrate there). DIRECTIONAL.
    qs_png = None
    qs_lines = []
    if qs_by_key and tv_entities and not args.no_qs_view:
        BUCKETS = [f"QS<={args.qs_low}",
                   f"QS {args.qs_low + 1}-{args.qs_high - 1}",
                   f"QS {args.qs_high}+", "No QS"]
        BCOLORS = {BUCKETS[0]: "#C00000", BUCKETS[1]: "#BF8F00",
                   BUCKETS[2]: "#2E7D32", BUCKETS[3]: "#808080"}
        nrows3 = _math.ceil((len(tv_entities) + 1) / 2)
        figq, axesq = plt.subplots(nrows3, 2,
                                   figsize=(11.5, 3.0 * nrows3),
                                   dpi=150)
        axesq = axesq.flatten()
        acct_q = {}
        for ax, ent in zip(axesq, tv_entities):
            t0_ = targets[camp_ids[ent]]
            eps = EPS_SIM.get(ent, args.eps_default)
            span, n = args.target_span, args.target_rungs
            ts = [t0_ - span + i * (2 * span / (n - 1))
                  for i in range(n)]
            ts = [t for t in ts if t > 0.5]
            ax2 = ax.twinx()
            stats = []
            for b in BUCKETS:
                sel = [k for k in kws if k["campaign"] == ent
                       and k["qs_bucket"] == b]
                base = agg(sel)
                if base["spend"] <= 0 or base["conv"] <= 0:
                    continue
                stats.append(f"{b}: ${base['spend']:,.0f}/d @ "
                             f"{base['roas']:.2f}")
                cc = curves(base, t0_, eps, ts)
                for i in range(len(ts)):
                    a = acct_q.setdefault(
                        (b, i - (len(ts) - 1) // 2), [0.0, 0.0])
                    a[0] += cc[i][1]
                    a[1] += cc[i][2]
                ax.plot([c[0] for c in cc], [c[2] for c in cc], "-",
                        color=BCOLORS[b], lw=2, label=b)
                ax2.plot([c[0] for c in cc], [c[1] for c in cc],
                         "--", color=BCOLORS[b], lw=1, alpha=0.45)
            ax.axvline(t0_, color="#404040", ls="-.", lw=1)
            ax.axhline(0, color="#C00000", lw=0.8, ls=":")
            ax.set_title(f"{ent}  [t {t0_:.2f}, eps {eps:g}]",
                         fontsize=10, fontweight="bold")
            ax.set_ylabel("GP $/day (solid)", fontsize=7.5)
            ax2.set_ylabel("conv/day (dashed)", fontsize=7.5)
            ax.tick_params(labelsize=7.5)
            ax2.tick_params(labelsize=7.5)
            ax.grid(axis="y", alpha=0.25)
            if ent == tv_entities[0]:
                ax.legend(fontsize=6.5, loc="best")
            qs_lines.append(f"{ent}: " + " | ".join(stats))
        axQ = axesq[len(tv_entities)]
        axQ2 = axQ.twinx()
        ks = sorted({k[1] for k in acct_q})
        dx = [k * (2 * args.target_span / (args.target_rungs - 1))
              for k in ks]
        for b in BUCKETS:
            gp = [acct_q.get((b, k), [0, 0])[1] for k in ks]
            cv = [acct_q.get((b, k), [0, 0])[0] for k in ks]
            if not any(gp) and not any(cv):
                continue
            axQ.plot(dx, gp, "-", color=BCOLORS[b], lw=2, label=b)
            axQ2.plot(dx, cv, "--", color=BCOLORS[b], lw=1,
                      alpha=0.45)
        axQ.axvline(0, color="#404040", ls="-.", lw=1)
        axQ.axhline(0, color="#C00000", lw=0.8, ls=":")
        axQ.set_title("ACCOUNT (delta target, pts)", fontsize=10,
                      fontweight="bold")
        axQ.set_ylabel("GP $/day (solid)", fontsize=7.5)
        axQ2.set_ylabel("conv/day (dashed)", fontsize=7.5)
        axQ.tick_params(labelsize=7.5)
        axQ2.tick_params(labelsize=7.5)
        axQ.grid(axis="y", alpha=0.25)
        for ax in axesq[len(tv_entities) + 1:]:
            ax.axis("off")
        unrated_sp = sum(k["rec_spend_day"] for k in kws
                         if k["qs_bucket"] == "No QS")
        tot_sp = sum(k["rec_spend_day"] for k in kws)
        figq.suptitle(
            "QS target view — GP/day (solid, left) and conv/day "
            "(dashed, right) across candidate tROAS targets, split "
            "by impression-weighted keyword QS bucket.\nSame eps "
            "per bucket (real low-QS response to loosening is "
            "likely STEEPER — expansions concentrate there). "
            f"No-QS spend: {(unrated_sp / tot_sp if tot_sp else 0):.0%}"
            " of recent. DIRECTIONAL, not a forecast.",
            fontsize=9.5)
        figq.tight_layout(rect=(0, 0, 1, 0.91))
        qs_png = out_dir / f"keyword_health_qs_view_{stamp}.png"
        figq.savefig(qs_png)
        plt.close(figq)

    t0 = time.perf_counter()
    wb = Workbook()
    ws = wb.active
    ws.title = "Summary"
    ws.sheet_view.showGridLines = False

    def put(ws_, r_, c_, v, fill=None, font=None, num=None,
            align=None):
        cell = ws_.cell(row=r_, column=c_, value=v)
        cell.border = BORDER
        if fill:
            cell.fill = fill
        if font:
            cell.font = font
        if num:
            cell.number_format = num
        if align:
            cell.alignment = Alignment(horizontal=align)
        return cell

    ws.cell(row=1, column=1,
            value="Keyword Health Scenario v2 — Google Home "
                  f"(account {ACCOUNT_ID}) — data through {stamp}"
            ).font = Font(bold=True, size=13)
    ws.cell(row=2, column=1, value=(
        f"Health on stability window {stab_start}→{data_end} "
        f"(keyword x MATCH TYPE; min ${args.min_spend:,.0f}); run-rate "
        f"trailing {args.recent_days}d; Leads = Conversions column. "
        "S1/S2 are FLOORS and spend-saved an UPPER BOUND — the "
        "controller redeploys freed budget under tROAS. S3* is "
        "DIRECTIONAL: S1 savings into measured-EXPANDABLE segments at "
        "pooled incremental ROAS, capacity-capped at observed $/day "
        "absorption. Measurement only — no cuts recommended here."
        )).font = SOFT_FONT

    hdr = ["Campaign", "Scenario", "Conv/day", "Spend/day", "GP/day",
           "ROAS", "vs S0 conv", "Spend saved/d", "KWs cut", "Note"]
    r0 = 4
    for c, h in enumerate(hdr, 1):
        put(ws, r0, c, h, HEADER_FILL, HEADER_FONT, align="center")
    r = r0 + 1
    for sr in scen_rows:
        first = sr["scenario"] == "S0"
        fill = CAMP_FILL if sr["campaign"] == ACCOUNT_LABEL else \
            (CURR_FILL if first else
             (S3_FILL if sr["scenario"] == "S3*" else None))
        put(ws, r, 1, sr["campaign"] if first else "", fill)
        put(ws, r, 2, sr["scenario"], fill)
        put(ws, r, 3, sr["conv"], fill, num="#,##0.0")
        put(ws, r, 4, sr["spend"], fill, num="$#,##0")
        put(ws, r, 5, sr["gp"],
            fill or (NEG_FILL if sr["gp"] < 0 else None),
            num="$#,##0")
        put(ws, r, 6, sr["roas"], fill, num="0.00")
        put(ws, r, 7, sr["conv_retained"], fill, num="0%")
        put(ws, r, 8, sr["spend_saved"], fill, num="$#,##0")
        put(ws, r, 9, sr["kws_cut"], fill, num="#,##0")
        put(ws, r, 10, sr["note"], fill,
            SOFT_FONT if sr["note"] else None)
        r += 1
    for c, cw in enumerate([12, 9, 10, 11, 11, 7, 11, 13, 8, 12], 1):
        ws.column_dimensions[get_column_letter(c)].width = cw

    img = XLImage(buf)
    img.anchor = f"A{r + 1}"
    ws.add_image(img)
    rr = r + 21
    info_lines = [
        "S1/S2 read: conv/day FLOOR if cut inventory simply stopped; "
        "spend saved is an UPPER bound (controller redeploys).",
        f"UNRATED tail: {unrated_conv:,.1f} conv/day "
        f"({(unrated_conv / total_conv if total_conv else 0):.0%} of "
        f"total) below the ${args.min_spend:,.0f} floor — kept in all "
        "scenarios.",
        "PROTECTED cost: protected-and-not-healthy rows run "
        f"${prot_bad_spend:,.0f}/d spend, {prot_bad_gp:+,.0f}/d GP, "
        f"{prot_bad_conv:,.1f} conv/d ({len(prot_bad)} rows) — the "
        "price of the exposure-recovery bet, kept in every scenario.",
        f"PHASE-CONDITIONED: {n_phase} rated rows have >= "
        f"{args.phase_share_flag:.0%} of stability spend inside "
        "expansion windows — dormant inventory judged on "
        "expansion-phase economics; class provisional (detail flag).",
    ]
    if s3_available:
        info_lines.append(
            "S3* allocation: " + (" | ".join(s3_alloc_lines)
                                  if s3_alloc_lines
                                  else "none (no capacity/savings)"))
        if probe_lines:
            info_lines.append(
                "UNTESTED (excluded from S3 — expansion candidates "
                "requiring a probe): " + " · ".join(probe_lines))
    else:
        info_lines.append(
            "S3 unavailable this run (needs marginal map + episodes "
            "CSVs" + (", or --no-s3 was set)." if args.no_s3
                      else ")."))
    for line in info_lines:
        ws.cell(row=rr, column=1, value=line).font = SOFT_FONT
        rr += 1

    wc = wb.create_sheet("Charts")
    wc.sheet_view.showGridLines = False
    imgp = XLImage(str(panels_png))
    imgp.anchor = "A1"
    wc.add_image(imgp)
    rr2 = int(3.0 * nrows * 150 / 20) + 6
    if tv_png:
        imgt = XLImage(str(tv_png))
        imgt.anchor = f"A{rr2}"
        wc.add_image(imgt)
        rr2 += int(3.0 * nrows2 * 150 / 20) + 4
        for line in tv_lines:
            wc.cell(row=rr2, column=1, value=line).font = SOFT_FONT
            rr2 += 1
        rr2 += 2
    if qs_png:
        imgq = XLImage(str(qs_png))
        imgq.anchor = f"A{rr2}"
        wc.add_image(imgq)
        rr2 += int(3.0 * nrows3 * 150 / 20) + 4
        for line in qs_lines:
            wc.cell(row=rr2, column=1, value=line).font = SOFT_FONT
            rr2 += 1

    wd = wb.create_sheet("Keyword Detail")
    wd.sheet_view.showGridLines = False
    dh = ["Campaign", "Keyword", "Match", "Inst", "Segment", "Class",
          "Prot", "PhaseCond", "Phase%", "QS", "QS bkt",
          "Stab spend", "Stab ROAS", "Target", "vs tgt",
          "Seg marginal", "Conv/d", "Spend/d", "GP/d"]
    for c, h in enumerate(dh, 1):
        put(wd, 1, c, h, HEADER_FILL, HEADER_FONT, align="center")
    detail = sorted(kws, key=lambda k: -k["stab_spend"])
    shown = detail[:args.detail_rows]
    for i, k in enumerate(shown, 2):
        put(wd, i, 1, k["campaign"])
        put(wd, i, 2, k["keyword"][:60])
        put(wd, i, 3, str(k["match_type"])[:8])
        put(wd, i, 4, k["n_instances"], num="0")
        put(wd, i, 5, k["segment"])
        put(wd, i, 6, k["class"], CLASS_FILL.get(k["class"]))
        put(wd, i, 7, "YES" if k["protected"] else "",
            WARN_FILL if k["protected"] else None)
        put(wd, i, 8, "PHASE" if k["phase_conditioned"] else "",
            WARN_FILL if k["phase_conditioned"] else None)
        put(wd, i, 9, k["phase_share"], num="0%")
        put(wd, i, 10, k.get("iw_qs"), num="0.0")
        put(wd, i, 11, k.get("qs_bucket", ""))
        put(wd, i, 12, k["stab_spend"], num="$#,##0")
        put(wd, i, 13, k["stab_roas"], num="0.00")
        put(wd, i, 14, k["campaign_target"], num="0.00")
        put(wd, i, 15, k["vs_target"], num="+0.00;-0.00")
        put(wd, i, 16, k["seg_marginal_class"])
        put(wd, i, 17, k["rec_conv_day"], num="#,##0.00")
        put(wd, i, 18, k["rec_spend_day"], num="$#,##0.0")
        put(wd, i, 19, k["rec_gp_day"],
            NEG_FILL if k["rec_gp_day"] < 0 else None,
            num="$#,##0.0")
    if len(detail) > len(shown):
        wd.cell(row=len(shown) + 3, column=1,
                value=f"… {len(detail) - len(shown):,} more rows in "
                      f"{detail_csv.name}").font = SOFT_FONT
    for c, cw in enumerate([11, 40, 8, 5, 13, 11, 5, 9, 7, 5, 8, 10,
                            9, 7, 8, 12, 8, 9, 9], 1):
        wd.column_dimensions[get_column_letter(c)].width = cw
    wd.freeze_panes = "A2"

    wn = wb.create_sheet("Notes")
    notes = [
        "Definitions & rules (v2)",
        "  Grain: keyword x MATCH TYPE x campaign — BROAD and EXACT "
        "of the same text are different auction objects (v1 merged "
        "them); Inst = ad-group placements merged per row.",
        f"  Health on stability window {stab_start} -> {data_end}, "
        f"min ${args.min_spend:,.0f}: HEALTHY >= 1.00 · BORDERLINE "
        "0.85-1.00 · UNHEALTHY < 0.85 · UNRATED below floor (kept "
        "everywhere).",
        "  PROTECTED = Farmers home-intent — kept in every scenario "
        "(standing rule); its cost is reported on the Summary.",
        f"  Run-rate trailing {args.recent_days}d ending {stamp}; "
        "Conv = Leads = Conversions column; revenue as stored; GP = "
        "rev - spend (DB terms); break-even 1.00; no lag exclusion "
        "(ruling 8/20).",
        "  S1/S2 SUBTRACTIVE FLOORS: no redeployment assumed; under "
        "campaign tROAS the controller redeploys, so conv/day is a "
        "floor and spend-saved an upper bound.",
        "  S3* DIRECTIONAL: S1 saved spend into EXPANDABLE segments "
        "(marginal map) at pooled incremental ROAS, filled in "
        "descending inc-ROAS, capacity-capped at each segment's "
        "observed absorption = mean over qualified episodes of "
        "dspend/win_days (episodes CSV). Added conv/day = alloc x "
        "inc-ROAS / segment RPL (recent window; fallback campaign, "
        "then account). UNTESTED excluded — listed as probe "
        "candidates. Every input is measured; the composition is "
        "still a model.",
        "  PHASE-CONDITIONED: share of stability spend inside "
        "cycle-state expansion windows >= "
        f"{args.phase_share_flag:.0%} — dormant inventory judged on "
        "expansion-phase economics; class provisional. The "
        "measurement window is a hypothesis.",
        "  QS VIEW: keyword-grain impression-weighted QS over the "
        "stability window -> bucket (differs from the cycle "
        "dashboard's daily-grain spend-by-bucket; both follow the "
        "canonical rules: QS never summed, weighted by kpd "
        "impressions, unrated excluded). Same eps applied to every "
        "bucket — real low-QS response to loosening is likely "
        "steeper, so low-bucket volume gains understate and GP "
        "losses understate on the loosen side.",
        "Caveats",
        "  Health is level ROAS under current targets/QS; rerun "
        "after cuts, target, or QS changes.",
        "  Targets = latest TARGET_ROAS new_value per campaign "
        "(platform='google') from campaign_change_events — the "
        "house-authoritative pattern; context columns only (no "
        "scenario math depends on them); cutover-day portfolio "
        "detachments may lack events — spot-check vs UI.",
        "  Pause decisions remain with expansion_cut_list.py; this "
        "workbook sizes consequences, it does not pick cuts.",
        "Performance",
        "  Stage timers print each run. Standing index request "
        "(route via DB architect — shared infra): CREATE INDEX "
        "idx_kpd_dev_date ON keyword_performance_daily(device, "
        "report_date, instance_id); then ANALYZE. Never run during a "
        "sync (concurrent writer stalls readers).",
    ]
    for i, line in enumerate(notes, 1):
        c = wn.cell(row=i, column=1, value=line)
        if not line.startswith("  "):
            c.font = Font(bold=True, size=11)
    wn.column_dimensions["A"].width = 112

    xlsx = out_dir / f"keyword_health_scenario_{stamp}.xlsx"
    wb.save(xlsx)
    t_xlsx = time.perf_counter() - t0

    print(f"\n[timers] query {t_query:.1f}s · merge {t_merge:.1f}s · "
          f"s3 {t_s3:.1f}s · chart {t_chart:.1f}s · "
          f"xlsx {t_xlsx:.1f}s")
    print("\nKEYWORD HEALTH SCENARIO — expected conv/day "
          "(S1/S2 = floors; S3* directional)")
    for short in shorts + [ACCOUNT_LABEL]:
        vals = {r["scenario"]: r for r in scen_rows
                if r["campaign"] == short}
        s0 = vals["S0"]
        if s0["conv"] <= 0 and s0["spend"] <= 0:
            print(f"  {short:11s} no recent data — verify id")
            continue

        def pct(v):
            return f"{v:.0%}" if v is not None else "—"

        line = f"  {short:11s} S0 {s0['conv']:7.1f}/d"
        for scen in ("S1", "S2", "S3*"):
            if scen not in vals:
                continue
            sr = vals[scen]
            line += (f"  {scen} {sr['conv']:7.1f} "
                     f"({pct(sr['conv_retained'])}, "
                     f"GP {sr['gp_change']:+,.0f}/d)")
        print(line)
    if tv_lines:
        print("\nTARGET VIEW — post-cut volume-neutral targets "
              "(directional):")
        for line in tv_lines:
            print("  " + line)
    print(f"\nSaved: {xlsx}")
    print(f"Saved: {panels_png}")
    if tv_png:
        print(f"Saved: {tv_png}")
    if qs_png:
        print(f"Saved: {qs_png}")
        print("\nQS BUCKET BASES (recent, per campaign):")
        for line in qs_lines:
            print("  " + line)
    print(f"Saved: {detail_csv}")


if __name__ == "__main__":
    main()
