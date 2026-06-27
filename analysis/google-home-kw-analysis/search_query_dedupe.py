#!/usr/bin/env python3
"""
search_query_dedupe.py — Tier B dedupe for the Home Insurance re-add list.

For each biddable keyword TEXT that went dark in 2026 (lost_biddable_READD from
the YoY analysis), ask from search-term data: is that query already being served
in 2026, and specifically by a BROAD/PHRASE keyword? If broad already covers the
intent, re-adding the exact term is redundant for coverage (a bid/QS control
decision, not recovery). If it isn't served, re-adding is net-new recovery.

Verdict: NOT_SERVED (re-add) / COVERED_BY_BROAD (skip-or-control) /
COVERED_BY_EXACT (already served by another exact kw).

Correctness (per DB architect):
- broad-vs-exact = keyword_instances.match_type (STRUCTURAL), never
  search_query_performance.match_type_served (behavioral).
- query text in search_queries (join via query_id), match on normalized_text.
- sqp has no account_id; scope via keyword_instances.account_id.
- sqp rows are per (query,keyword,day,match_type_served); we SUM and join 1:1
  on instance_id/query_id, so no fan-out — never count rows.
Read-only.
"""
import argparse, glob, os, sqlite3, sys
from datetime import date, timedelta
try:
    from openpyxl import Workbook, load_workbook
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.utils import get_column_letter
except ImportError:
    sys.exit("openpyxl is required:  pip install openpyxl")

CONFIG = {
    "sqp_table": "search_query_performance", "sqp_query_col": "query_id",
    "sqp_instance_col": "instance_id", "sqp_date_col": "report_date",
    "sqp_device_col": "device", "sqp_impr_col": "impressions",
    "sqp_clicks_col": "clicks", "sqp_pconv_count_col": "primary_conv_count",
    "sqp_pconv_value_col": "primary_conv_value",
    "sq_table": "search_queries", "sq_id_col": "id",   # sqp.query_id -> here; flip to "query_id" if validation fails
    "sq_norm_col": "normalized_text", "sq_text_col": "query_text",
    "ki_table": "keyword_instances", "ki_id_col": "id",
    "ki_account_col": "account_id", "ki_match_col": "match_type",
    "ki_text_col": "keyword_text",
}
ACCOUNT_ID = 1
DEFAULT_VERDICTS = ("lost_biddable_READD",)
SERVED_IMPR_FLOOR = 30
SERVED_CONV_FLOOR = 1.0
INSPECT_TABLES = ["search_query_performance", "search_queries", "keyword_instances"]


def derive_window(args):
    yest = date.today() - timedelta(days=1)
    return (args.window_start or date(yest.year, 4, 1).isoformat(),
            args.window_end or yest.isoformat())


def inspect_schema(conn):
    cur = conn.cursor()
    for t in INSPECT_TABLES:
        print(f"\n===== {t} =====")
        try:
            for r in cur.execute(f"PRAGMA table_info({t})").fetchall():
                print(f"  {r[1]:<26} {r[2]:<10} pk={r[5]}")
        except sqlite3.Error as ex:
            print(f"  (could not read: {ex})")


def validate_joins(conn, c, win):
    cur = conn.cursor()
    for tkey, cols in (
        ("sqp_table", ["sqp_query_col", "sqp_instance_col", "sqp_date_col",
                       "sqp_device_col", "sqp_impr_col", "sqp_pconv_count_col"]),
        ("sq_table", ["sq_id_col", "sq_norm_col"]),
        ("ki_table", ["ki_id_col", "ki_account_col", "ki_match_col", "ki_text_col"]),
    ):
        present = {r[1] for r in cur.execute(f"PRAGMA table_info({c[tkey]})").fetchall()}
        for ck in cols:
            if c[ck] not in present:
                sys.exit(f"SCHEMA ERROR: {c[tkey]}.{c[ck]} missing. Run --inspect-schema. "
                         f"Present: {sorted(present)}")
    n = cur.execute(f"""
        SELECT COUNT(*) FROM {c['sqp_table']} sqp
        JOIN {c['ki_table']} ki ON ki.{c['ki_id_col']} = sqp.{c['sqp_instance_col']}
        JOIN {c['sq_table']} sq ON sq.{c['sq_id_col']} = sqp.{c['sqp_query_col']}
        WHERE sqp.{c['sqp_device_col']}='ALL' AND ki.{c['ki_account_col']}=?
          AND sqp.{c['sqp_date_col']}>=? AND sqp.{c['sqp_date_col']}<=?
    """, [ACCOUNT_ID, win[0], win[1]]).fetchone()[0]
    if n == 0:
        sys.exit(f"JOIN/COVERAGE ERROR: 0 rows for account {ACCOUNT_ID} in {win[0]}..{win[1]}. "
                 f"Either sqp.query_id does not reference {c['sq_table']}.{c['sq_id_col']} "
                 f"(try sq_id_col='query_id') or the window isn't backfilled.")
    lo, hi = cur.execute(f"SELECT MIN({c['sqp_date_col']}), MAX({c['sqp_date_col']}) "
                         f"FROM {c['sqp_table']}").fetchone()
    print(f"sqp coverage: {lo} -> {hi}   |   join OK ({n:,} rows in window)")


def per_term_sql(c):
    bp = "('BROAD','PHRASE')"
    return f"""
        SELECT
          CASE WHEN ki.{c['ki_match_col']} IN {bp} THEN 'bp'
               WHEN ki.{c['ki_match_col']}='EXACT' THEN 'ex' ELSE 'oth' END AS bucket,
          CASE WHEN lower(ki.{c['ki_text_col']}) LIKE '%' || ? || '%' THEN 1 ELSE 0 END AS on_term,
          SUM(CASE WHEN sq.{c['sq_norm_col']}=? THEN sqp.{c['sqp_impr_col']} ELSE 0 END) AS e_impr,
          SUM(CASE WHEN sq.{c['sq_norm_col']}=? THEN sqp.{c['sqp_pconv_count_col']} ELSE 0 END) AS e_conv,
          SUM(sqp.{c['sqp_impr_col']}) AS c_impr,
          SUM(sqp.{c['sqp_pconv_count_col']}) AS c_conv,
          SUM(sqp.{c['sqp_pconv_value_col']}) AS c_rev
        FROM {c['sqp_table']} sqp
        JOIN {c['ki_table']} ki ON ki.{c['ki_id_col']} = sqp.{c['sqp_instance_col']}
        JOIN {c['sq_table']} sq ON sq.{c['sq_id_col']} = sqp.{c['sqp_query_col']}
        WHERE sqp.{c['sqp_device_col']}='ALL' AND ki.{c['ki_account_col']}=?
          AND sqp.{c['sqp_date_col']}>=? AND sqp.{c['sqp_date_col']}<=?
          AND sq.{c['sq_norm_col']} LIKE '%' || ? || '%'
        GROUP BY bucket, on_term
    """


def dedupe_term(conn, c, term, win):
    # NOTE: param order must match per_term_sql's placeholders:
    # 1) on_term LIKE, 2) e_impr =, 3) e_conv =, 4) account, 5) date>=, 6) date<=, 7) contains LIKE
    on_impr = off_impr = 0
    on_conv = off_conv = 0.0
    et_impr = 0
    et_conv = 0.0
    bp_off = ex_off = 0
    for bucket, on_term, ei, ec, ci, cc, cr in conn.cursor().execute(
            per_term_sql(c),
            [term, term, term, ACCOUNT_ID, win[0], win[1], term]).fetchall():
        ci = ci or 0; cc = cc or 0.0; ei = ei or 0; ec = ec or 0.0
        et_impr += ei; et_conv += ec
        if on_term:
            on_impr += ci; on_conv += cc
        else:
            off_impr += ci; off_conv += cc
            if bucket == 'bp':
                bp_off += ci
            elif bucket == 'ex':
                ex_off += ci
    total_impr = on_impr + off_impr
    total_conv = on_conv + off_conv

    if total_impr < SERVED_IMPR_FLOOR:
        v, rec = "NOT_SERVED", "RE-ADD (net-new recovery)"
    elif on_impr >= SERVED_IMPR_FLOOR:
        v, rec = "COVERED_ON_TERM", "an active keyword on this term is already serving; skip"
    else:
        v, rec = "ABSORBED_BY_OTHER_KW", "served only by other keywords' text; RE-ADD for control/relevance"
    return {"verdict": v, "recommendation": rec,
            "served_total_impr": total_impr, "served_total_conv": round(total_conv, 1),
            "served_on_term_impr": on_impr, "served_on_term_conv": round(on_conv, 1),
            "served_off_term_impr": off_impr, "served_off_term_conv": round(off_conv, 1),
            "off_term_broad_impr": bp_off, "off_term_exact_impr": ex_off,
            "exact_text_impr": et_impr, "exact_text_conv": round(et_conv, 1)}


def find_latest_workbook(out_dir):
    cands = glob.glob(os.path.join(out_dir, "keyword_yoy_analysis_*.xlsx"))
    return max(cands, key=os.path.getmtime) if cands else None


def load_candidates(path, verdicts):
    wb = load_workbook(path, data_only=True, read_only=True)
    if "Text_Netting" not in wb.sheetnames:
        sys.exit(f"{path} has no Text_Netting sheet — run yoy_keyword_analysis.py first.")

    def sd(name):
        rows = list(wb[name].iter_rows(values_only=True))
        return [dict(zip(rows[0], r)) for r in rows[1:]]
    bstat = {}
    if "Brand_Netting" in wb.sheetnames:
        for b in sd("Brand_Netting"):
            mb = b.get("modifier_brand")
            if mb:
                bstat[str(mb).lower().strip()] = b.get("brand_status")
    out = []
    for r in sd("Text_Netting"):
        if r.get("verdict") in verdicts:
            mb = r.get("modifier_brand")
            out.append({"keyword_text": r.get("keyword_text"), "modifier_brand": mb,
                        "biddability": r.get("biddability"), "conv_2025": r.get("conv_2025"),
                        "brand_status": bstat.get(str(mb).lower().strip()) if mb else None})
    return out


HEADER_FILL = PatternFill("solid", fgColor="1F3864")
HEADER_FONT = Font(bold=True, color="FFFFFF", size=10)


def write_workbook(path, rows):
    wb = Workbook(); ws = wb.active; ws.title = "TierB_Dedupe"
    cols = ["keyword_text", "modifier_brand", "biddability", "conv_2025",
            "verdict", "recommendation",
            "served_total_impr", "served_total_conv",
            "served_on_term_impr", "served_on_term_conv",
            "served_off_term_impr", "served_off_term_conv",
            "off_term_broad_impr", "off_term_exact_impr",
            "exact_text_impr", "exact_text_conv"]
    ws.append(cols)
    for ci in range(1, len(cols) + 1):
        cell = ws.cell(row=1, column=ci); cell.fill = HEADER_FILL; cell.font = HEADER_FONT
        cell.alignment = Alignment(horizontal="center", wrap_text=True)
    for r in rows:
        ws.append([r.get(k) for k in cols])
    ws.freeze_panes = "A2"
    for ci, col in enumerate(cols, 1):
        ws.column_dimensions[get_column_letter(ci)].width = min(max(len(col) + 2, 12), 40)
    ws.auto_filter.ref = f"A1:{get_column_letter(len(cols))}{len(rows)+1}"
    wb.save(path)


def print_summary(rows, win):
    order = ["NOT_SERVED", "ABSORBED_BY_OTHER_KW", "COVERED_ON_TERM"]
    actions = {"NOT_SERVED": "RE-ADD (net-new)",
               "ABSORBED_BY_OTHER_KW": "RE-ADD (control/relevance)",
               "COVERED_ON_TERM": "skip — keyword on term already serving"}
    buckets = {}
    for r in rows:
        b = buckets.setdefault(r["verdict"], {"n": 0, "c25": 0.0})
        b["n"] += 1; b["c25"] += r.get("conv_2025") or 0.0
    print("\n" + "=" * 78)
    print(f"TIER B DEDUPE — search-term coverage in {win[0]} .. {win[1]}")
    print("=" * 78)
    print(f"{'verdict':<20}{'terms':>7}{'2025 conv behind':>20}   action")
    for v in order + [k for k in buckets if k not in order]:
        if v in buckets:
            b = buckets[v]
            print(f"{v:<20}{b['n']:>7}{b['c25']:>20,.0f}   {actions.get(v,'')}")
    print("=" * 78)


def main():
    ap = argparse.ArgumentParser(description="Tier B search-term dedupe.")
    ap.add_argument("--db-path", default=r"C:\Projects\google-ads-ops\db\paid_search.db")
    ap.add_argument("--out-dir", default=r"C:\Projects\google-ads-ops\analysis\output")
    ap.add_argument("--workbook")
    ap.add_argument("--verdicts", default=",".join(DEFAULT_VERDICTS))
    ap.add_argument("--window-start")
    ap.add_argument("--window-end")
    ap.add_argument("--inspect-schema", action="store_true")
    args = ap.parse_args()

    conn = sqlite3.connect(f"file:{args.db_path}?mode=ro", uri=True)
    c = CONFIG
    if args.inspect_schema:
        inspect_schema(conn); return

    win = derive_window(args)
    print(f"Window: {win[0]} -> {win[1]}")
    validate_joins(conn, c, win)

    wb_path = args.workbook or find_latest_workbook(args.out_dir)
    if not wb_path or not os.path.exists(wb_path):
        sys.exit("No YoY workbook found. Pass --workbook or run yoy_keyword_analysis.py.")
    print(f"Candidates from: {wb_path}")
    verdicts = tuple(v.strip() for v in args.verdicts.split(","))
    cands = load_candidates(wb_path, verdicts)
    print(f"Deduping {len(cands)} candidate terms (verdicts: {', '.join(verdicts)})\n")

    rows = []
    for cand in cands:
        if not cand["keyword_text"]:
            continue
        rows.append({**cand, **dedupe_term(conn, c, cand["keyword_text"], win)})
    readd = {"NOT_SERVED", "ABSORBED_BY_OTHER_KW"}
    rows.sort(key=lambda r: (r["verdict"] not in readd, -(r.get("conv_2025") or 0)))

    print_summary(rows, win)
    os.makedirs(args.out_dir, exist_ok=True)
    out = os.path.join(args.out_dir, f"tierB_dedupe_{date.today().isoformat()}.xlsx")
    write_workbook(out, rows)
    print(f"\nWROTE {out}  ({len(rows)} terms)")
    conn.close()


if __name__ == "__main__":
    main()