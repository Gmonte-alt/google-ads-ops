"""
kw_target_lookup.py
====================
Looks up all keyword instances matching keyword text from a lookup Excel file,
across a specified date range. Useful for keyword removal research — finds
every instance of a keyword across all campaigns and match types in the account.

Match logic: keyword text only (normalized, case-insensitive).
Campaign, ad group, and match type from the lookup file are used as
reference information in the output but NOT as filter criteria.
All instances of the keyword text in the account are returned.

INPUT FILE FORMAT (Excel)
─────────────────────────
Columns required:
  Google Account ID  — platform_account_id (e.g. 8886502147)
  Keyword text       — keyword text with optional [] or "" notation
                       (notation is stripped; bare text is used for matching)

Additional columns (Campaign, Ad group) are included in output for reference.

OUTPUT
──────
Excel workbook with:
  Sheet 1 — All matched instances with performance data
  Sheet 2 — Keyword text lookup summary (unique texts × account)
  Sheet 3 — Unmatched (keyword texts with no instances in DB)

RUN
───
    conda activate paid-search
    python analysis/kw_target_lookup.py
    python analysis/kw_target_lookup.py --start 2026-03-25 --end 2026-04-23
    python analysis/kw_target_lookup.py --xlsx path/to/lookup.xlsx
"""

import sqlite3
import argparse
from pathlib import Path

import pandas as pd
import openpyxl
from openpyxl.styles import PatternFill, Font, Alignment
from openpyxl.utils import get_column_letter

DB_PATH      = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")
OUTPUT_DIR   = Path(r"C:\Projects\google-ads-ops\reports")
DEFAULT_XLSX = Path(r"C:\Projects\google-ads-ops\analysis\input\google-ads-kw-target-confie-lookup.xlsx")

START_DATE_DEFAULT = "2026-03-25"
END_DATE_DEFAULT   = "2026-04-23"

HEADER_FILL = "7030A0"
HEADER_FONT = "FFFFFF"


# ── Keyword text normalisation ─────────────────────────────────────────────────

def strip_match_notation(raw: str) -> str:
    """
    Strip Google Ads match type notation and return bare lowercase text.
      [keyword]  -> keyword
      "keyword"  -> keyword
      keyword    -> keyword
    """
    raw = (raw or "").strip()
    if raw.startswith("[") and raw.endswith("]"):
        return raw[1:-1].strip().lower()
    elif raw.startswith('"') and raw.endswith('"'):
        return raw[1:-1].strip().lower()
    return raw.lower()


# ── DB connection ──────────────────────────────────────────────────────────────

def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


# ── Load lookup list ──────────────────────────────────────────────────────────

def load_lookup(xlsx_path: Path) -> pd.DataFrame:
    wb   = openpyxl.load_workbook(xlsx_path, read_only=True, data_only=True)
    ws   = wb.active
    rows = list(ws.iter_rows(min_row=1, values_only=True))

    # Find header row
    header_idx = None
    for i, row in enumerate(rows):
        if row and any(str(c or "").strip().lower() in
                       ("google account id", "keyword text", "campaign")
                       for c in row):
            header_idx = i
            break

    if header_idx is None:
        raise ValueError("Could not find header row in lookup file.")

    headers = [str(c or "").strip() for c in rows[header_idx]]
    data    = [
        dict(zip(headers, row))
        for row in rows[header_idx + 1:]
        if any(v for v in row)
    ]

    df = pd.DataFrame(data)
    col_map = {
        "Google Account ID": "account_id_str",
        "Platform":          "platform",
        "Campaign":          "lookup_campaign",
        "Ad group":          "lookup_ad_group",
        "Keyword text":      "kw_raw",
    }
    df = df.rename(columns={k: v for k, v in col_map.items() if k in df.columns})
    df["account_id_str"] = df["account_id_str"].astype(str).str.strip()
    df["kw_raw"]         = df["kw_raw"].astype(str).str.strip()
    df["normalized_text"] = df["kw_raw"].apply(strip_match_notation)

    # Deduplicate to unique (account, normalized_text) pairs for matching
    unique_pairs = (df[["account_id_str", "normalized_text"]]
                    .drop_duplicates()
                    .reset_index(drop=True))

    print(f"  Loaded {len(df)} lookup rows → "
          f"{len(unique_pairs)} unique (account, keyword) pairs")
    return df, unique_pairs


# ── Resolve instances ─────────────────────────────────────────────────────────

def resolve_instances(conn, unique_pairs: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    For each (account, normalized_text) pair, return ALL matching
    keyword_instances regardless of campaign, ad group, or match type.
    """
    # Build account_id lookup
    acct_map = {}
    for a in conn.execute(
        "SELECT id, platform_account_id, account_name FROM accounts WHERE platform='google'"
    ).fetchall():
        acct_map[str(a["platform_account_id"])] = {
            "account_id":   a["id"],
            "account_name": a["account_name"],
        }

    results   = []
    unmatched = []

    for _, pair in unique_pairs.iterrows():
        pid   = str(pair["account_id_str"])
        ntext = str(pair["normalized_text"]).strip().lower()
        acct  = acct_map.get(pid)

        if not acct:
            unmatched.append({"account_id_str": pid, "normalized_text": ntext,
                               "reason": f"account {pid} not in DB"})
            continue

        account_id = acct["account_id"]

        ki_rows = conn.execute("""
            SELECT
                ki.id            AS instance_id,
                ki.criterion_id,
                ki.ad_group_id,
                ki.campaign_id,
                ki.keyword_text,
                ki.match_type,
                ki.campaign_name,
                ki.ad_group_name,
                ki.status        AS kw_status,
                ki_id.normalized_text
            FROM keyword_instances ki
            JOIN keyword_identities ki_id ON ki_id.id = ki.identity_id
            WHERE ki.account_id = ?
              AND LOWER(ki_id.normalized_text) = ?
        """, (account_id, ntext)).fetchall()

        if not ki_rows:
            unmatched.append({"account_id_str": pid, "normalized_text": ntext,
                               "reason": "no instances in keyword_instances"})
            continue

        for ki in ki_rows:
            results.append({
                "account_id_str":  pid,
                "account_name":    acct["account_name"],
                "account_id":      account_id,
                "instance_id":     ki["instance_id"],
                "criterion_id":    ki["criterion_id"],
                "campaign_id":     ki["campaign_id"],
                "campaign_name":   ki["campaign_name"],
                "ad_group_id":     ki["ad_group_id"],
                "ad_group_name":   ki["ad_group_name"],
                "keyword_text":    ki["keyword_text"],
                "match_type":      ki["match_type"],
                "kw_status":       ki["kw_status"],
                "normalized_text": ntext,
            })

    print(f"  Instances found : {len(results):,}")
    print(f"  Unmatched texts : {len(unmatched)}")
    if unmatched:
        for u in unmatched:
            print(f"    ✗ '{u['normalized_text']}' in acct {u['account_id_str']}: {u['reason']}")

    return (pd.DataFrame(results),
            pd.DataFrame(unmatched) if unmatched else pd.DataFrame())


# ── Fetch performance ─────────────────────────────────────────────────────────

def fetch_performance(conn, instance_ids: list, start: str, end: str) -> pd.DataFrame:
    if not instance_ids:
        return pd.DataFrame()

    all_rows = []
    chunk_size = 500
    for i in range(0, len(instance_ids), chunk_size):
        chunk = instance_ids[i:i + chunk_size]
        ph    = ",".join("?" * len(chunk))
        rows  = conn.execute(f"""
            SELECT
                kpd.instance_id,
                SUM(kpd.impressions)          AS impressions,
                SUM(kpd.clicks)               AS clicks,
                SUM(kpd.cost_micros) / 1e6    AS spend,
                SUM(kpd.primary_conv_count)   AS conversions,
                SUM(kpd.primary_conv_value)   AS revenue
            FROM keyword_performance_daily kpd
            WHERE kpd.instance_id IN ({ph})
              AND kpd.device      = 'ALL'
              AND kpd.geo_state   = 'ALL'
              AND kpd.report_date BETWEEN ? AND ?
            GROUP BY kpd.instance_id
        """, chunk + [start, end]).fetchall()
        all_rows.extend(rows)

    if not all_rows:
        return pd.DataFrame(columns=[
            "instance_id","impressions","clicks","spend","conversions","revenue"
        ])
    return pd.DataFrame([dict(r) for r in all_rows])


# ── Excel output ───────────────────────────────────────────────────────────────

def write_workbook(out_path: Path, results_df: pd.DataFrame,
                   summary_df: pd.DataFrame, unmatched_df: pd.DataFrame,
                   start: str, end: str):

    wb = openpyxl.Workbook()
    hfill = PatternFill("solid", fgColor=HEADER_FILL)
    hfont = Font(color=HEADER_FONT, bold=True)

    def write_sheet(ws, df, title=None):
        if title:
            ws.title = title
        if df is None or df.empty:
            ws.append(["No data"])
            return
        for ci, col in enumerate(df.columns, 1):
            cell = ws.cell(row=1, column=ci, value=col)
            cell.fill = hfill
            cell.font = hfont
            cell.alignment = Alignment(horizontal="center")
        for ri, row in enumerate(df.itertuples(index=False), 2):
            for ci, val in enumerate(row, 1):
                ws.cell(row=ri, column=ci, value=val)
        for ci, col in enumerate(df.columns, 1):
            max_len = max(
                len(str(col)),
                df.iloc[:, ci-1].astype(str).str.len().max() if len(df) > 0 else 0
            )
            ws.column_dimensions[get_column_letter(ci)].width = min(max_len + 2, 60)
        ws.freeze_panes = ws.cell(row=2, column=1)

    # Sheet 1 — all instances with performance
    ws1 = wb.active
    write_sheet(ws1, results_df,
                title=f"KW Instances {start} to {end}")

    # Sheet 2 — summary by keyword text
    ws2 = wb.create_sheet(title="Summary by Keyword")
    write_sheet(ws2, summary_df)

    # Sheet 3 — unmatched
    ws3 = wb.create_sheet(title="Unmatched Keywords")
    if not unmatched_df.empty:
        write_sheet(ws3, unmatched_df)
    else:
        ws3.append(["All keyword texts matched."])

    wb.save(out_path)
    print(f"\n  Saved: {out_path}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--xlsx",  default=str(DEFAULT_XLSX))
    p.add_argument("--start", default=START_DATE_DEFAULT)
    p.add_argument("--end",   default=END_DATE_DEFAULT)
    args = p.parse_args()

    start = args.start
    end   = args.end

    print("=" * 65)
    print("kw_target_lookup.py  [keyword-text match mode]")
    print(f"  Database : {DB_PATH}")
    print(f"  Lookup   : {Path(args.xlsx).name}")
    print(f"  Window   : {start} → {end}")
    print("=" * 65)

    conn = get_conn()

    print("\n[1] Loading lookup file...")
    lookup_df, unique_pairs = load_lookup(Path(args.xlsx))

    print("\n[2] Resolving instances (text-only match, all campaigns)...")
    instances_df, unmatched_df = resolve_instances(conn, unique_pairs)

    if instances_df.empty:
        print("  No instances found.")
        conn.close()
        return

    print(f"\n[3] Fetching performance ({start} → {end})...")
    iids    = instances_df["instance_id"].tolist()
    perf_df = fetch_performance(conn, iids, start, end)
    print(f"  Performance rows: {len(perf_df):,}")

    # Merge performance
    results_df = instances_df.merge(perf_df, on="instance_id", how="left")
    for col in ["impressions","clicks","spend","conversions","revenue"]:
        results_df[col] = results_df[col].fillna(0)

    # Derived metrics
    results_df["ctr"]  = (results_df["clicks"] / results_df["impressions"]
                          ).where(results_df["impressions"] > 0).round(4)
    results_df["cpc"]  = (results_df["spend"] / results_df["clicks"]
                          ).where(results_df["clicks"] > 0).round(2)
    results_df["cpa"]  = (results_df["spend"] / results_df["conversions"]
                          ).where(results_df["conversions"] > 0).round(2)
    results_df["roas"] = (results_df["revenue"] / results_df["spend"]
                          ).where(results_df["spend"] > 0).round(4)
    results_df["spend"]   = results_df["spend"].round(2)
    results_df["revenue"] = results_df["revenue"].round(2)

    # Output column order
    out_cols = [
        "account_id_str","account_name",
        "normalized_text","keyword_text","match_type","kw_status",
        "campaign_name","campaign_id",
        "ad_group_name","ad_group_id",
        "instance_id","criterion_id",
        "impressions","clicks","spend","conversions","revenue",
        "ctr","cpc","cpa","roas",
    ]
    results_df = results_df[[c for c in out_cols if c in results_df.columns]]

    # Summary by keyword text — spend, clicks, impressions, convs, revenue
    summary_df = (
        results_df.groupby(["account_id_str","account_name","normalized_text"],
                            as_index=False)
        .agg(
            total_instances=("instance_id","count"),
            enabled_instances=("kw_status", lambda x: (x=="ENABLED").sum()),
            impressions=("impressions","sum"),
            clicks=("clicks","sum"),
            spend=("spend","sum"),
            conversions=("conversions","sum"),
            revenue=("revenue","sum"),
        )
        .sort_values("spend", ascending=False)
        .reset_index(drop=True)
    )
    summary_df["spend"]   = summary_df["spend"].round(2)
    summary_df["revenue"] = summary_df["revenue"].round(2)

    # Terminal summary
    print(f"\n[4] Results:")
    print(f"  Total instances          : {len(results_df):,}")
    print(f"  ENABLED instances        : {(results_df['kw_status']=='ENABLED').sum():,}")
    print(f"  PAUSED instances         : {(results_df['kw_status']=='PAUSED').sum():,}")
    print(f"  With impressions         : {(results_df['impressions']>0).sum():,}")
    print(f"  Total spend              : ${results_df['spend'].sum():,.2f}")
    print(f"  Total clicks             : {results_df['clicks'].sum():,.0f}")
    print(f"  Total impressions        : {results_df['impressions'].sum():,.0f}")
    print(f"  Total conversions        : {results_df['conversions'].sum():,.1f}")
    print(f"  Total revenue            : ${results_df['revenue'].sum():,.2f}")

    print(f"\n  Top 5 by spend (keyword text × account):")
    for _, r in summary_df.head(5).iterrows():
        print(f"    '{r['normalized_text']}'  {r['account_id_str']}  "
              f"${r['spend']:,.2f}  {r['total_instances']} instances")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUTPUT_DIR / f"kw_target_lookup_{start}_to_{end}.xlsx"
    write_workbook(out_path, results_df, summary_df, unmatched_df, start, end)

    conn.close()
    print("Done.")


if __name__ == "__main__":
    main()
