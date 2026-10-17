"""
termlife_disposition_ingest.py
==============================
Daily ingest for the SQLife Term disposition feed (see term_life_disposition_feed_notes.md).

Does three things, idempotently, in one pass:
  1. SWEEP  - find SQL_GenericDisposition_Term_Clicks_*.csv in the My Drive root
              (where Gmail's "Add all to Drive" lands them) and move each into
              <FEED_DIR>\YYYYMMDD\ based on the FILENAME timestamp (not today).
  2. LOAD   - for every dated folder whose batch_date is not yet in the load log:
              read cp1252, drop exact-duplicate rows, parse Tracking_Code ourselves,
              append to life_disposition_snapshot with batch_date + source_code.
  3. REPORT - per-batch summary: files, rows per stream, dupes dropped,
              never-before-seen stream codes flagged.

Safe to run any time, any number of times. Catches up multiple missed days.
Force a re-load of one batch:  python termlife_disposition_ingest.py --reload 20260924

Daily routine: open the SQLife email -> "Add all to Drive" -> run this script.
"""

import argparse
import csv
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------- CONFIG ----
DRIVE_ROOT = Path(r"G:\My Drive")                                  # Gmail's Add-to-Drive landing zone
FEED_DIR   = DRIVE_ROOT / "life-SQL-Generic-disposition"           # canonical archive: FEED_DIR\YYYYMMDD\*.csv
DB_PATH    = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")
ENC        = "cp1252"                                              # confirmed: Windows/SQL Server export

FILE_RE = re.compile(
    r"^SQL_GenericDisposition_Term_Clicks_([A-Z]{4})_(\d{12})(?:\s*\(\d+\))?\.csv$"
)

# Full 24-code roster from the partner email ("attached if available")
KNOWN_SEGMENTS = {"CA", "CB", "CC", "QA", "QJ", "SO", "SN", "UR"}
KNOWN_CHANNELS = {"M", "R", "T"}

# CSV header -> db column. Account_Id renamed sq_lead_id: it is SQLife's per-lead
# record id, NOT an ad account id (avoids collision with paid_search.db account_id).
COLMAP = [
    ("Day", "day"), ("month_year", "month_year"), ("Account_Id", "sq_lead_id"),
    ("Advert_Name", "advert_name"), ("Mailer_Name", "mailer_name"),
    ("EF_Affiliate", "ef_affiliate"), ("Advert_Source_Code", "advert_source_code"),
    ("Lead_Type", "lead_type"), ("Status", "status"), ("Lead_Count", "lead_count"),
    ("OP_Count", "op_count"), ("Contacts", "contacts"), ("Assigned_Ops", "assigned_ops"),
    ("Sqo", "sqo"), ("Cris", "cris"), ("Capped_Cri$", "capped_cri_dollars"),
    ("Policy_Type", "policy_type"), ("Proj_Inf_Prem", "proj_inf_prem"),
    ("Bound_Date", "bound_date"), ("FE_Policy_Count", "fe_policy_count"),
    ("FE_TotalLtr", "fe_total_ltr"), ("FE_Bound_Date", "fe_bound_date"),
    ("Mon_Rev", "mon_rev"), ("Average_Cost", "average_cost"), ("Lead_Tier", "lead_tier"),
    ("Age", "age"), ("Gender", "gender"), ("State", "state"),
    ("Existing_Ins", "existing_ins"), ("Desired_Ins$", "desired_ins_dollars"),
    ("Indv_Zipcode", "indv_zipcode"), ("Device_Type", "device_type"),
    ("Tracking_Code", "tracking_code"),
    # Partner's pre-parsed tracking tokens: broken on organic rows (URL colons).
    # Stored for fidelity as src_*; use the trk_* columns we parse instead.
    ("C", "src_c"), ("Ad", "src_ad"), ("Se", "src_se"), ("P", "src_p"),
    ("K", "src_k"), ("M", "src_m"),
    ("Transaction_Id", "transaction_id"), ("AdId", "ad_id"),
    ("SessionId", "session_id"), ("ANI", "ani"),
]
CSV_HEADERS = [c for c, _ in COLMAP]
DB_COLS     = [d for _, d in COLMAP]

INT_COLS  = {"lead_count", "op_count", "contacts", "assigned_ops", "sqo", "cris",
             "fe_policy_count", "age", "lead_tier"}
REAL_COLS = {"capped_cri_dollars", "proj_inf_prem", "fe_total_ltr", "mon_rev",
             "average_cost", "desired_ins_dollars"}

TRK_COLS = ["trk_channel", "trk_landing", "trk_se", "trk_click_id", "trk_k",
            "trk_gvtmisc", "trk_device", "trk_scode", "trk_utm_campaign"]

# Non-greedy between fixed key tokens: survives ':' inside values (e.g. https URLs),
# which is exactly what breaks the partner's own parser.
TRK_RE = re.compile(
    r"t:aff:c:(?P<c>.*?):ad:(?P<ad>.*?):se:(?P<se>.*?):p:(?P<p>.*?)"
    r":k:(?P<k>.*?):m:(?P<m>.*?):d:(?P<d>.*?):attr:"
)


# ---------------------------------------------------------------- helpers ---
def parse_tracking(tc: str) -> dict:
    out = dict.fromkeys(TRK_COLS)
    if not tc:
        return out
    m = TRK_RE.search(tc)
    if m:
        out.update(trk_channel=m["c"], trk_landing=m["ad"], trk_se=m["se"],
                   trk_click_id=m["p"], trk_k=m["k"], trk_gvtmisc=m["m"],
                   trk_device=m["d"])
    qs = tc.split("&", 1)[1] if "&" in tc else ""
    for pair in qs.split("&"):
        if pair.startswith("sCode="):
            out["trk_scode"] = pair[6:]
        elif pair.startswith("utm_campaign="):
            out["trk_utm_campaign"] = pair[13:]
    return out


def coerce(col: str, v: str):
    v = (v or "").strip()
    if v == "":
        return None
    if col in INT_COLS:
        try:
            return int(float(v))
        except ValueError:
            return v  # keep oddballs visible rather than nulling them
    if col in REAL_COLS:
        try:
            return float(v)
        except ValueError:
            return v
    return v


def ensure_schema(con: sqlite3.Connection):
    typed = []
    for c in DB_COLS:
        t = "INTEGER" if c in INT_COLS else "REAL" if c in REAL_COLS else "TEXT"
        typed.append(f"{c} {t}")
    con.execute(f"""
        CREATE TABLE IF NOT EXISTS life_disposition_snapshot (
            batch_date  TEXT NOT NULL,        -- YYYY-MM-DD of the feed batch
            source_code TEXT NOT NULL,        -- 4-letter code from the filename
            source_file TEXT NOT NULL,
            loaded_at   TEXT NOT NULL,
            {", ".join(typed)},
            {", ".join(f"{c} TEXT" for c in TRK_COLS)}
        )""")
    con.execute("""CREATE INDEX IF NOT EXISTS idx_lds_batch
                   ON life_disposition_snapshot(batch_date, source_code)""")
    con.execute("""CREATE INDEX IF NOT EXISTS idx_lds_lead
                   ON life_disposition_snapshot(sq_lead_id)""")
    con.execute("""CREATE INDEX IF NOT EXISTS idx_lds_txn
                   ON life_disposition_snapshot(transaction_id)""")
    con.execute("""
        CREATE TABLE IF NOT EXISTS life_disposition_load_log (
            batch_date   TEXT PRIMARY KEY,
            loaded_at    TEXT NOT NULL,
            n_files      INTEGER,
            rows_raw     INTEGER,
            rows_loaded  INTEGER,
            dupes_dropped INTEGER,
            files        TEXT
        )""")
    con.execute("""
        CREATE VIEW IF NOT EXISTS v_life_disposition_current AS
        SELECT * FROM life_disposition_snapshot
        WHERE batch_date = (SELECT MAX(batch_date) FROM life_disposition_snapshot)
        """)


# ------------------------------------------------------------------ sweep ---
def sweep() -> list[str]:
    """Move feed CSVs from the Drive root (and FEED_DIR top level) into dated folders."""
    notes = []
    FEED_DIR.mkdir(parents=True, exist_ok=True)
    candidates = list(DRIVE_ROOT.glob("SQL_GenericDisposition_Term_Clicks_*.csv")) \
               + list(FEED_DIR.glob("SQL_GenericDisposition_Term_Clicks_*.csv"))
    for f in candidates:
        m = FILE_RE.match(f.name)
        if not m:
            notes.append(f"  SKIP (name not recognized): {f.name}")
            continue
        code, ts = m.group(1), m.group(2)
        dest_dir = FEED_DIR / ts[:8]
        dest_dir.mkdir(exist_ok=True)
        canonical = f"SQL_GenericDisposition_Term_Clicks_{code}_{ts}.csv"
        dest = dest_dir / canonical
        if dest.exists():
            # duplicate capture (double-clicked Add-to-Drive) -> quarantine, don't delete
            dupdir = FEED_DIR / "_duplicates"
            dupdir.mkdir(exist_ok=True)
            f.rename(dupdir / f"{ts}_{f.name}")
            notes.append(f"  DUP  -> _duplicates\\: {f.name}")
        else:
            try:
                f.rename(dest)
                notes.append(f"  FILED -> {ts[:8]}\\{canonical}")
            except OSError as e:
                notes.append(f"  BUSY (still syncing? will retry next run): {f.name} [{e}]")
    return notes


# ------------------------------------------------------------------- load ---
def load_batch(con: sqlite3.Connection, folder: Path, reload_: bool):
    batch = folder.name                                   # YYYYMMDD
    batch_date = f"{batch[:4]}-{batch[4:6]}-{batch[6:]}"
    files = sorted(folder.glob("SQL_GenericDisposition_Term_Clicks_*.csv"))
    if not files:
        return None
    already = con.execute(
        "SELECT n_files FROM life_disposition_load_log WHERE batch_date=?",
        (batch_date,)).fetchone()
    if already and not reload_:
        if already[0] != len(files):
            print(f"  !! batch {batch_date} was loaded with {already[0]} files "
                  f"but folder now has {len(files)} — run --reload {batch}")
        return None

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    rows_raw = rows_loaded = dupes = 0
    per_stream, new_codes = {}, set()
    ins_cols = ["batch_date", "source_code", "source_file", "loaded_at"] + DB_COLS + TRK_COLS
    sql = (f"INSERT INTO life_disposition_snapshot ({','.join(ins_cols)}) "
           f"VALUES ({','.join('?' * len(ins_cols))})")

    con.execute("BEGIN")
    try:
        con.execute("DELETE FROM life_disposition_snapshot WHERE batch_date=?", (batch_date,))
        for f in files:
            code = FILE_RE.match(f.name).group(1)
            if code[:2] not in KNOWN_SEGMENTS or code[2] not in KNOWN_CHANNELS or code[3] != "Q":
                new_codes.add(code)
            with open(f, newline="", encoding=ENC) as fh:
                reader = csv.DictReader(fh)
                missing = [h for h in CSV_HEADERS if h not in (reader.fieldnames or [])]
                if missing:
                    raise RuntimeError(f"{f.name}: schema drift, missing {missing}")
                seen, batch_rows = set(), []
                for r in reader:
                    rows_raw += 1
                    key = tuple(r.get(h, "") for h in CSV_HEADERS)
                    if key in seen:                        # exact-duplicate row (export fanout)
                        dupes += 1
                        continue
                    seen.add(key)
                    vals = [coerce(d, r.get(h, "")) for h, d in COLMAP]
                    trk = parse_tracking(r.get("Tracking_Code", ""))
                    batch_rows.append([batch_date, code, f.name, now] + vals
                                      + [trk[c] for c in TRK_COLS])
                con.executemany(sql, batch_rows)
                rows_loaded += len(batch_rows)
                per_stream[code] = len(batch_rows)
        con.execute("""INSERT OR REPLACE INTO life_disposition_load_log
                       (batch_date, loaded_at, n_files, rows_raw, rows_loaded,
                        dupes_dropped, files)
                       VALUES (?,?,?,?,?,?,?)""",
                    (batch_date, now, len(files), rows_raw, rows_loaded, dupes,
                     ",".join(f.name for f in files)))
        con.commit()
    except Exception:
        con.rollback()
        raise
    return dict(batch_date=batch_date, n_files=len(files), rows_raw=rows_raw,
                rows_loaded=rows_loaded, dupes=dupes, per_stream=per_stream,
                new_codes=new_codes)


# ------------------------------------------------------------------- main ---
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reload", metavar="YYYYMMDD",
                    help="force delete+reload of one batch date")
    args = ap.parse_args()

    print("== SWEEP ==")
    notes = sweep()
    print("\n".join(notes) if notes else "  nothing to file")

    if not DB_PATH.exists():
        sys.exit(f"DB not found: {DB_PATH} — fix DB_PATH at top of script")
    con = sqlite3.connect(DB_PATH)
    con.execute("PRAGMA journal_mode=WAL")
    ensure_schema(con)

    print("\n== LOAD ==")
    loaded_any = False
    for folder in sorted(p for p in FEED_DIR.iterdir()
                         if p.is_dir() and re.fullmatch(r"\d{8}", p.name)):
        res = load_batch(con, folder, reload_=(args.reload == folder.name))
        if not res:
            continue
        loaded_any = True
        print(f"  batch {res['batch_date']}: {res['n_files']} files, "
              f"{res['rows_loaded']} rows loaded "
              f"({res['rows_raw']} raw, {res['dupes']} exact dupes dropped)")
        for code, n in sorted(res["per_stream"].items()):
            print(f"      {code}: {n}")
        if res["new_codes"]:
            print(f"  *** NEW STREAM CODE(S) NEVER SEEN BEFORE: "
                  f"{sorted(res['new_codes'])} — investigate & update notes ***")
    if not loaded_any:
        print("  no new batches (all dated folders already loaded)")

    latest = con.execute("SELECT MAX(batch_date) FROM life_disposition_snapshot").fetchone()[0]
    if latest:
        print(f"\n== CURRENT VIEW ==\n  v_life_disposition_current -> batch {latest}")
        for code, n, cri, rev in con.execute(
                """SELECT source_code, COUNT(*), COALESCE(SUM(cris),0),
                          ROUND(COALESCE(SUM(capped_cri_dollars),0),2)
                   FROM v_life_disposition_current GROUP BY 1 ORDER BY 1"""):
            print(f"      {code}: {n:5d} rows | CRIs {cri:4d} | ${rev:,.2f}")
    con.close()


if __name__ == "__main__":
    main()
