"""
load_microsoft_search_terms.py
------------------------------
Loads Microsoft Advertising Search Term (Daily) Report CSV exports into a
dedicated STAGING table `ms_search_terms` in paid_search.db.

WHY A STAGING TABLE (not search_query_performance):
  Microsoft has no API, so served-query data can only come from UI exports.
  Rather than write into the shared cross-platform `search_query_performance`
  (which is keyed on internal instance_id/query_id, is per-device with NO
  device='ALL' rollup, and would need its `search_queries` dimension
  populated), this lands the export faithfully in its own table. The Tier-B
  "already served / absorbed by live broad" check queries it directly,
  joining the export's native Keyword ID to keyword_instances at read time.
  (Final placement — staging vs promoting into sqp — is the open architect
  question; this keeps the raw truth intact either way.)

EXPORT FORMAT (confirmed from microsoft-home-sq-20260430.csv):
  UTF-8 w/ BOM, CRLF. 5 preamble lines + 1 blank, header on line 7, then a
  'Total' row and a '(c)2026 Microsoft' footer at the end (both dropped).
  24 columns incl native bracketed IDs, Network, per-device Date grain, and
  Bid vs Delivered match type (delivered carries close-variant annotations).

CONVENTIONS APPLIED:
  - IDs: strip brackets  '[531494260]' -> 531494260
  - normalized_search_term: 09-canonical = lowercase + whitespace-collapse
    ONLY (punctuation/apostrophes kept) — matches keyword_identities
  - delivered match type kept RAW + a collapsed base + is_close_variant flag
  - network kept raw + is_search_network flag (0 only when 'Audience')
  - metrics landed UNROUTED (Conversions / Revenue / All conversions /
    All revenue) — routing is a downstream/architect decision
  - per-device grain preserved; NO device='ALL' synthesis
  - idempotent per source file (re-loading a month replaces its rows)

USAGE:
  python load_microsoft_search_terms.py --backfill
  python load_microsoft_search_terms.py --file <path>
  python load_microsoft_search_terms.py --latest
  python load_microsoft_search_terms.py --reset            # wipe table first
  python load_microsoft_search_terms.py --backfill --dry-run
  (--db-path / --input-dir override the Windows defaults; handy for testing)
"""

import argparse
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

DEFAULT_DB = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")
DEFAULT_INPUT = Path(r"C:\Projects\google-ads-ops\kw_db_scripts\input\microsoft-sq")
FILE_GLOB = "microsoft-*-sq-*.csv"

COLUMN_MAP = {
    "Account number": "account_number",
    "Campaign name": "campaign_name",
    "Campaign ID": "campaign_id",
    "Ad group": "ad_group_name",
    "Ad group ID": "ad_group_id",
    "Search term": "search_term",
    "Keyword ID": "keyword_id",
    "Keyword": "keyword_text",
    "Ad ID": "ad_id",
    "Bid match type": "bid_match_type",
    "Delivered match type": "delivered_match_type",
    "Date": "report_date",
    "Device type": "device_raw",
    "Network": "network",
    "Impressions": "impressions",
    "Clicks": "clicks",
    "Spend": "spend_usd",
    "Conversions": "conversions",
    "Revenue": "revenue",
    "All conversions": "all_conversions",
    "All revenue": "all_revenue",
}
DEVICE_MAP = {"Computer": "DESKTOP", "Smartphone": "MOBILE", "Tablet": "TABLET"}

DDL = """
CREATE TABLE IF NOT EXISTS ms_search_terms (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    platform               TEXT NOT NULL DEFAULT 'microsoft',
    account_number         TEXT,
    campaign_id            INTEGER,
    campaign_name          TEXT,
    ad_group_id            INTEGER,
    ad_group_name          TEXT,
    keyword_id             INTEGER,     -- MS criterion id -> keyword_instances
    keyword_text           TEXT,
    ad_id                  INTEGER,
    search_term            TEXT NOT NULL,
    normalized_search_term TEXT,        -- 09-canonical (lowercase + ws-collapse)
    normalized_keyword     TEXT,
    bid_match_type         TEXT,        -- targeted
    delivered_match_type   TEXT,        -- served, RAW (incl 'close variant')
    delivered_base         TEXT,        -- EXACT / PHRASE / BROAD
    is_close_variant       INTEGER,     -- 1 if served as a close variant
    report_date            TEXT NOT NULL,
    device                 TEXT,        -- DESKTOP / MOBILE / TABLET
    network                TEXT,
    is_search_network      INTEGER,     -- 0 only when network = 'Audience'
    impressions            INTEGER,
    clicks                 INTEGER,
    spend_usd              REAL,
    conversions            REAL,        -- MS 'Conversions' column (unrouted)
    revenue                REAL,        -- MS 'Revenue' (pixel; usually 0)
    all_conversions        REAL,        -- MS 'All conversions'
    all_revenue            REAL,        -- MS 'All revenue' (offline-inclusive)
    source_file            TEXT,
    load_ts                TEXT
);
"""
INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_mst_norm      ON ms_search_terms(normalized_search_term)",
    "CREATE INDEX IF NOT EXISTS idx_mst_keyword   ON ms_search_terms(keyword_id)",
    "CREATE INDEX IF NOT EXISTS idx_mst_campaign  ON ms_search_terms(campaign_id)",
    "CREATE INDEX IF NOT EXISTS idx_mst_date      ON ms_search_terms(report_date)",
    "CREATE INDEX IF NOT EXISTS idx_mst_source    ON ms_search_terms(source_file)",
]

INSERT_COLS = [
    "platform", "account_number", "campaign_id", "campaign_name",
    "ad_group_id", "ad_group_name", "keyword_id", "keyword_text", "ad_id",
    "search_term", "normalized_search_term", "normalized_keyword",
    "bid_match_type", "delivered_match_type", "delivered_base",
    "is_close_variant", "report_date", "device", "network",
    "is_search_network", "impressions", "clicks", "spend_usd",
    "conversions", "revenue", "all_conversions", "all_revenue",
    "source_file", "load_ts",
]


def normalize(text: str) -> str:
    """09-canonical: lowercase + whitespace-collapse only (punctuation kept)."""
    return re.sub(r"\s+", " ", str(text).strip().lower())


def strip_brackets_int(v):
    s = re.sub(r"[\[\]]", "", str(v)).strip()
    return int(s) if s.isdigit() else None


def base_match(raw: str) -> str:
    u = str(raw).upper()
    return "EXACT" if "EXACT" in u else "PHRASE" if "PHRASE" in u else "BROAD"


def find_header_row(path: Path) -> int:
    with open(path, encoding="utf-8-sig") as fh:
        for i, line in enumerate(fh):
            if i > 20:
                break
            if '"Search term"' in line or '"Account name"' in line:
                return i
    raise ValueError(f"Header row not found in {path.name} — is this a MS search term report?")


def read_export(path: Path) -> pd.DataFrame:
    hdr = find_header_row(path)
    df = pd.read_csv(path, encoding="utf-8-sig", skiprows=hdr,
                     dtype=str, keep_default_na=False)
    # Drop Total row and (c) footer: real rows carry a bracketed Campaign ID.
    df = df[df["Campaign ID"].str.match(r"^\[\d+\]$", na=False)].copy()
    return df


def transform(df: pd.DataFrame, source_file: str) -> pd.DataFrame:
    df = df.rename(columns={k: v for k, v in COLUMN_MAP.items() if k in df.columns})
    for idc in ("campaign_id", "ad_group_id", "keyword_id", "ad_id"):
        if idc in df.columns:
            df[idc] = df[idc].map(strip_brackets_int)
    df["report_date"] = pd.to_datetime(df["report_date"], format="mixed").dt.strftime("%Y-%m-%d")
    df["device"] = df["device_raw"].map(DEVICE_MAP).fillna(df["device_raw"].str.upper())
    df["network"] = df["network"].astype(str).str.strip()
    df["is_search_network"] = (df["network"].str.lower() != "audience").astype(int)
    df["delivered_base"] = df["delivered_match_type"].map(base_match)
    df["is_close_variant"] = df["delivered_match_type"].str.contains(
        "close variant", case=False, na=False).astype(int)
    df["normalized_search_term"] = df["search_term"].map(normalize)
    df["normalized_keyword"] = df["keyword_text"].map(normalize)
    for c in ("impressions", "clicks"):
        df[c] = pd.to_numeric(df[c].str.replace(",", "", regex=False), errors="coerce").fillna(0).astype(int)
    for c in ("spend_usd", "conversions", "revenue", "all_conversions", "all_revenue"):
        df[c] = pd.to_numeric(df[c].str.replace(",", "", regex=False), errors="coerce").fillna(0.0)
    df = df[df["search_term"].str.strip().ne("")].copy()
    df["platform"] = "microsoft"
    df["source_file"] = source_file
    df["load_ts"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return df


def load_file(conn, path: Path, dry_run: bool) -> dict:
    raw = read_export(path)
    df = transform(raw, path.name)
    summary = {
        "file": path.name, "rows": len(df),
        "dates": f"{df['report_date'].min()} → {df['report_date'].max()}",
        "terms": df["normalized_search_term"].nunique(),
        "search_rows": int(df["is_search_network"].sum()),
        "audience_rows": int((~df["is_search_network"].astype(bool)).sum()),
        "clicks": int(df["clicks"].sum()), "spend": float(df["spend_usd"].sum()),
    }
    if dry_run:
        return summary
    cur = conn.cursor()
    cur.execute("DELETE FROM ms_search_terms WHERE source_file = ?", (path.name,))
    rows = [tuple(r[c] for c in INSERT_COLS) for _, r in df[INSERT_COLS].iterrows()]
    placeholders = ",".join("?" * len(INSERT_COLS))
    cur.executemany(
        f"INSERT INTO ms_search_terms ({','.join(INSERT_COLS)}) VALUES ({placeholders})", rows)
    conn.commit()
    return summary


def main():
    ap = argparse.ArgumentParser(description="Load MS search term exports into ms_search_terms staging table")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--file")
    g.add_argument("--latest", action="store_true")
    g.add_argument("--backfill", action="store_true")
    ap.add_argument("--db-path", default=str(DEFAULT_DB))
    ap.add_argument("--input-dir", default=str(DEFAULT_INPUT))
    ap.add_argument("--reset", action="store_true", help="wipe ms_search_terms before loading")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(DDL)
    for ix in INDEXES:
        conn.execute(ix)
    conn.commit()

    if args.reset and not args.dry_run:
        n = conn.execute("SELECT COUNT(*) FROM ms_search_terms").fetchone()[0]
        conn.execute("DELETE FROM ms_search_terms")
        conn.commit()
        print(f"reset: cleared {n:,} existing rows")

    input_dir = Path(args.input_dir)
    if args.file:
        files = [Path(args.file)]
    elif args.latest:
        files = sorted(input_dir.glob(FILE_GLOB))[-1:]
    else:
        files = sorted(input_dir.glob(FILE_GLOB))
    if not files:
        print("No files found."); return

    print(f"{'DRY RUN — ' if args.dry_run else ''}loading {len(files)} file(s) into "
          f"ms_search_terms{'' if args.dry_run else ' @ ' + args.db_path}\n")
    grand = 0
    for f in files:
        s = load_file(conn, f, args.dry_run)
        grand += s["rows"]
        print(f"  {s['file']}: {s['rows']:,} rows | {s['dates']} | "
              f"{s['terms']:,} distinct terms | search {s['search_rows']:,} / "
              f"audience {s['audience_rows']:,} | {s['clicks']:,} clicks | ${s['spend']:,.2f}")
    if not args.dry_run:
        tot = conn.execute("SELECT COUNT(*) FROM ms_search_terms").fetchone()[0]
        print(f"\nloaded {grand:,} rows this run; ms_search_terms now holds {tot:,} rows")
    conn.close()


if __name__ == "__main__":
    main()
