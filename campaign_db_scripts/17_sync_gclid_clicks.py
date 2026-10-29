#!/usr/bin/env python
"""
17_sync_gclid_clicks.py — GCLID click ledger sync (click_view -> gclid_clicks).

GATE: do NOT run with --apply until the DB architect ratifies the schema proposal memo.
If the ratified DDL differs from the memo, patch DDL_* below before --init.

One row per Google click (gclid PK), with keyword / ad group / campaign / ad / device /
click-grain geo attribution, for acct 29 (Term Life) now, multi-account ready. Companion
gclid_sync_log records every day attempted — 0-row Sundays are COVERED days, not gaps.

Hard API facts this script is built around (probe-verified 2026-10-22):
  * click_view requires a one-day filter — one GAQL query per day, never a range
  * 90-day lookback cliff, hard-enforced — days older than T-89 are skipped loudly
  * keyword/ad resource names parse to (ad_group_id, criterion_id) / (ad_group_id, ad_id)
  * resolution to keyword_instances via composite ad_group_criterion_id = "ag~crit" (100%)
  * device is PER-CLICK here — no 'ALL' rollup exists at this grain (sum, never filter)

Design rules: a click row is NEVER dropped (unresolved keyword -> NULLs, counted);
upsert on gclid (re-pull idempotent); no FK constraints; dry-run by default; --init
takes a DB backup before DDL.

Usage (conda env paid-search):
    python 17_sync_gclid_clicks.py --init                      # dry-run: print DDL
    python 17_sync_gclid_clicks.py --init --apply              # backup DB, create tables
    python 17_sync_gclid_clicks.py --backfill                  # dry-run the T-89..T-1 span
    python 17_sync_gclid_clicks.py --backfill --apply          # Phase 2: backfill to cliff
    python 17_sync_gclid_clicks.py --apply                     # daily: re-pull T-3..T-1
    python 17_sync_gclid_clicks.py --start 2026-09-01 --end 2026-09-07 --apply
    python 17_sync_gclid_clicks.py --acceptance                # read-only acceptance checks
"""

import argparse
import datetime as dt
import re
import sqlite3
import sys
import time
from collections import Counter
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from google.ads.googleads.client import GoogleAdsClient  # noqa: E402
from google.ads.googleads.errors import GoogleAdsException  # noqa: E402

DB_PATH = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")
BACKUP_DIR = Path(r"C:\Projects\google-ads-ops\db\backups")

# account label -> (customer_id, account_id). Multi-account ready; acct 29 only for now.
ACCOUNTS = {
    "term_life": ("1671155176", 29),
}

CLIFF_DAYS = 89          # last safely pullable day = today - 89 (T-91 hard-rejected)
DAILY_WINDOW = 3         # daily mode re-pulls T-1..T-3 (late-attribution absorption)
STALE_WARN_DAYS = 3      # gap detector: warn if newest logged day older than this
CLIFF_ALERT_DAYS = 80    # gap detector: ALERT on unlogged days at/past T-80

KW_RES_RE = re.compile(r"customers/\d+/adGroupCriteria/(\d+)~(\d+)$")
AD_RES_RE = re.compile(r"customers/\d+/adGroupAds/(\d+)~(\d+)$")
GEO_RES_RE = re.compile(r"geoTargetConstants/(\d+)$")

GAQL_FIELDS = [
    "click_view.gclid",
    "click_view.ad_group_ad",
    "click_view.keyword",
    "click_view.keyword_info.text",
    "click_view.keyword_info.match_type",
    "campaign.id",
    "ad_group.id",
    "segments.date",
    "segments.device",
    "segments.click_type",
    "segments.slot",
    "click_view.area_of_interest.most_specific",
    "click_view.location_of_presence.most_specific",
]

DDL_GCLID_CLICKS = """\
-- gclid_clicks: accumulating click-grain ledger, one row per gclid (click_view).
-- !! DEVICE SEMANTICS INVERTED vs house norm: device here is PER-CLICK (MOBILE/DESKTOP/
-- !! TABLET). There is NO 'ALL' rollup at this grain. SUM to aggregate; NEVER filter
-- !! device='ALL' (returns zero rows). Opposite of kpd/cpd.
-- !! ACCUMULATES, NEVER DELETES: Google restates click_view after the fact (invalid-click
-- !! scrubbing). Upsert re-pulls add/update rows but never remove a gclid Google later
-- !! scrubbed, so ledger counts >= restated click_view/cpd on older days is EXPECTED
-- !! DRIFT, not corruption — a lead's click must stay findable even if the click was
-- !! later disqualified for billing.
CREATE TABLE IF NOT EXISTS gclid_clicks (
    gclid                   TEXT PRIMARY KEY,
    account_id              INTEGER NOT NULL,
    click_date              TEXT    NOT NULL,
    campaign_id             TEXT    NOT NULL,
    ad_group_id             TEXT,
    criterion_id            TEXT,
    instance_id             INTEGER,
    keyword_text            TEXT,
    match_type              TEXT,
    ad_id                   INTEGER,
    device                  TEXT    NOT NULL,
    click_type              TEXT,
    slot                    TEXT,
    location_of_presence_id INTEGER,
    area_of_interest_id     INTEGER,
    synced_at               TEXT    NOT NULL
)"""

DDL_SYNC_LOG = """\
-- gclid_sync_log: per-day coverage ledger. rows_pulled=0 (Sundays) = COVERED, not a gap.
CREATE TABLE IF NOT EXISTS gclid_sync_log (
    account_id  INTEGER NOT NULL,
    click_date  TEXT    NOT NULL,
    rows_pulled INTEGER NOT NULL,
    kw_linked   INTEGER NOT NULL,
    resolved    INTEGER NOT NULL,
    synced_at   TEXT    NOT NULL,
    PRIMARY KEY (account_id, click_date)
)"""

DDL_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_gclid_clicks_acct_date ON gclid_clicks(account_id, click_date)",
    "CREATE INDEX IF NOT EXISTS idx_gclid_clicks_instance  ON gclid_clicks(instance_id)",
]

UPSERT_CLICK = """\
INSERT INTO gclid_clicks (gclid, account_id, click_date, campaign_id, ad_group_id,
    criterion_id, instance_id, keyword_text, match_type, ad_id, device, click_type,
    slot, location_of_presence_id, area_of_interest_id, synced_at)
VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
ON CONFLICT(gclid) DO UPDATE SET
    account_id=excluded.account_id, click_date=excluded.click_date,
    campaign_id=excluded.campaign_id, ad_group_id=excluded.ad_group_id,
    criterion_id=excluded.criterion_id, instance_id=excluded.instance_id,
    keyword_text=excluded.keyword_text, match_type=excluded.match_type,
    ad_id=excluded.ad_id, device=excluded.device, click_type=excluded.click_type,
    slot=excluded.slot, location_of_presence_id=excluded.location_of_presence_id,
    area_of_interest_id=excluded.area_of_interest_id, synced_at=excluded.synced_at"""

UPSERT_LOG = """\
INSERT INTO gclid_sync_log (account_id, click_date, rows_pulled, kw_linked, resolved, synced_at)
VALUES (?,?,?,?,?,?)
ON CONFLICT(account_id, click_date) DO UPDATE SET
    rows_pulled=excluded.rows_pulled, kw_linked=excluded.kw_linked,
    resolved=excluded.resolved, synced_at=excluded.synced_at"""


# ----------------------------------------------------------------------------- utilities

def now_iso():
    return dt.datetime.now().isoformat(timespec="seconds")


def enum_name(v):
    try:
        return v.name
    except AttributeError:
        return str(v)


def backup_db():
    """File backup before any DDL — sqlite backup API (WAL-safe point-in-time copy)."""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = BACKUP_DIR / f"paid_search_pre_gclid_{stamp}.db"
    print(f"  backing up DB -> {dest} (this is an ~11.5GB copy; allow a few minutes)")
    src = sqlite3.connect(str(DB_PATH))
    dst = sqlite3.connect(str(dest))
    with dst:
        src.backup(dst)
    src.close()
    dst.close()
    print(f"  backup complete: {dest.stat().st_size / 1e9:.2f} GB")
    return dest


def connect(apply_mode):
    if apply_mode:
        con = sqlite3.connect(str(DB_PATH))
    else:
        con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    return con


def daterange(start, end):
    d = start
    while d <= end:
        yield d
        d += dt.timedelta(days=1)


def pull_day(ga_service, customer_id, day, retries=2):
    """One GAQL query for one day. Returns (rows, error). Retries transient failures."""
    query = (f"SELECT {', '.join(GAQL_FIELDS)} FROM click_view "
             f"WHERE segments.date = '{day.isoformat()}'")
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            rows = []
            for batch in ga_service.search_stream(customer_id=customer_id, query=query):
                rows.extend(batch.results)
            return rows, None
        except GoogleAdsException as ex:
            last_err = "; ".join(e.message for e in ex.failure.errors) or str(ex)
            # permanent errors (too-old date, bad field) won't heal — don't retry
            if "older than 90 days" in last_err or "must have a filter" in last_err:
                return None, last_err
        except Exception as ex:
            last_err = f"{type(ex).__name__}: {ex}"
        if attempt < retries:
            print(f"    transient failure (attempt {attempt}): {last_err} — retrying in 10s")
            time.sleep(10)
    return None, last_err


def load_resolver(cur, account_id):
    """composite ad_group_criterion_id ('ag~crit') -> instance id, scoped to account."""
    cur.execute(
        "SELECT ad_group_criterion_id, id FROM keyword_instances "
        "WHERE account_id=? AND ad_group_criterion_id IS NOT NULL", (account_id,))
    resolver = {}
    dupes = 0
    for key, iid in cur.fetchall():
        k = str(key).strip()
        if k in resolver:
            dupes += 1
        resolver[k] = iid
    print(f"  resolver: {len(resolver)} composite keys for account_id={account_id}"
          + (f"  !! {dupes} duplicate keys (1:1 assumption violated — investigate)"
             if dupes else ""))
    return resolver


def transform(rows, account_id, resolver, synced_at):
    """API rows -> upsert tuples. NEVER drops a click row; counts linkage instead."""
    out = []
    kw_linked = resolved = 0
    for r in rows:
        cv = r.click_view
        ad_group_id = criterion_id = keyword_text = match_type = None
        instance_id = ad_id = None
        kw = cv.keyword or ""
        m = KW_RES_RE.match(kw)
        if m:
            kw_linked += 1
            ad_group_id, criterion_id = m.group(1), m.group(2)
            keyword_text = cv.keyword_info.text or None
            match_type = enum_name(cv.keyword_info.match_type)
            iid = resolver.get(f"{ad_group_id}~{criterion_id}")
            if iid is not None:
                instance_id = iid
                resolved += 1
        ma = AD_RES_RE.match(cv.ad_group_ad or "")
        if ma:
            if ad_group_id is None:
                ad_group_id = ma.group(1)
            ad_id = int(ma.group(2))
        if ad_group_id is None and r.ad_group.id:
            ad_group_id = str(r.ad_group.id)
        g = GEO_RES_RE.match(cv.location_of_presence.most_specific or "")
        lop = int(g.group(1)) if g else None
        g = GEO_RES_RE.match(cv.area_of_interest.most_specific or "")
        aoi = int(g.group(1)) if g else None
        out.append((
            cv.gclid, account_id, r.segments.date, str(r.campaign.id),
            ad_group_id, criterion_id, instance_id, keyword_text, match_type,
            ad_id, enum_name(r.segments.device), enum_name(r.segments.click_type),
            enum_name(r.segments.slot), lop, aoi, synced_at,
        ))
    return out, kw_linked, resolved


# ----------------------------------------------------------------------------- gap check

def gap_check(cur, account_id, today):
    """Coverage audit on gclid_sync_log. Absence of a log row = gap; 0-row days covered."""
    print(f"\n  --- gap check (account_id={account_id}) ---")
    try:
        cur.execute("SELECT click_date FROM gclid_sync_log WHERE account_id=?", (account_id,))
    except sqlite3.OperationalError:
        print("  gclid_sync_log does not exist yet — run --init first")
        return
    logged = {r[0] for r in cur.fetchall()}
    if not logged:
        print("  no days logged yet")
        return
    newest = max(logged)
    age = (today - dt.date.fromisoformat(newest)).days
    if age > STALE_WARN_DAYS:
        print(f"  !! WARN: newest logged day {newest} is {age} days old "
              f"(> {STALE_WARN_DAYS}) — daily sync has stalled")
    else:
        print(f"  newest logged day: {newest} (age {age}d) — OK")
    window_start = today - dt.timedelta(days=CLIFF_DAYS)
    missing = [d for d in daterange(max(window_start, dt.date.fromisoformat(min(logged))),
                                    today - dt.timedelta(days=1))
               if d.isoformat() not in logged]
    if not missing:
        print("  no gaps inside the 90-day window — all days logged (Sundays incl.)")
        return
    urgent = [d for d in missing if (today - d).days >= CLIFF_ALERT_DAYS]
    print(f"  !! {len(missing)} unlogged day(s) in window: "
          + ", ".join(d.isoformat() for d in missing[:10])
          + (" ..." if len(missing) > 10 else ""))
    if urgent:
        print(f"  !! ALERT: {len(urgent)} unlogged day(s) at/past T-{CLIFF_ALERT_DAYS} — "
              f"about to fall off the cliff UNPULLED: "
              + ", ".join(d.isoformat() for d in urgent))


# ----------------------------------------------------------------------------- acceptance

def acceptance(cur, account_id, today):
    """Read-only acceptance checks per the ratified memo. Prints a paste-ready block."""
    print("\n" + "=" * 78)
    print("ACCEPTANCE CHECKS (memo section 5)")
    print("=" * 78)
    # 1) last 7 non-Sunday days vs cpd
    days, d = [], today - dt.timedelta(days=1)
    while len(days) < 7:
        if d.weekday() != 6:
            days.append(d)
        d -= dt.timedelta(days=1)
    print("\n  1) click ledger vs cpd clicks (device='ALL'), last 7 non-Sunday days:")
    worst = 0.0
    for day in sorted(days):
        iso = day.isoformat()
        cur.execute("SELECT COUNT(*) FROM gclid_clicks WHERE account_id=? AND click_date=?",
                    (account_id, iso))
        gc = cur.fetchone()[0]
        cur.execute("SELECT COALESCE(SUM(clicks),0) FROM campaign_performance_daily "
                    "WHERE account_id=? AND date=? AND device='ALL'", (account_id, iso))
        cpd = cur.fetchone()[0]
        delta = gc - cpd
        pct = (100.0 * delta / cpd) if cpd else 0.0
        worst = max(worst, abs(pct))
        flag = "TIE" if delta == 0 else f"delta {delta:+d} ({pct:+.2f}%)"
        print(f"     {iso}: ledger {gc} vs cpd {cpd} -> {flag}")
    print(f"     worst |delta|: {worst:.2f}%  (pass: exact or <=1% documented; "
          f"ledger >= cpd drift on older days is expected — scrubbed gclids persist)")
    # 2) duplicate gclids
    cur.execute("SELECT COUNT(*) - COUNT(DISTINCT gclid) FROM gclid_clicks "
                "WHERE account_id=?", (account_id,))
    print(f"\n  2) duplicate gclids: {cur.fetchone()[0]}  (pass: 0; PK-enforced)")
    # 3) log coverage over the loaded span
    cur.execute("SELECT MIN(click_date), MAX(click_date) FROM gclid_sync_log "
                "WHERE account_id=?", (account_id,))
    lo, hi = cur.fetchone()
    if lo:
        span = [d for d in daterange(dt.date.fromisoformat(lo), dt.date.fromisoformat(hi))]
        cur.execute("SELECT COUNT(*) FROM gclid_sync_log WHERE account_id=?", (account_id,))
        n = cur.fetchone()[0]
        print(f"\n  3) sync-log coverage: {n}/{len(span)} days logged over {lo}..{hi}  "
              f"(pass: every day incl. 0-row Sundays)")
        cur.execute("SELECT COUNT(*) FROM gclid_sync_log "
                    "WHERE account_id=? AND rows_pulled=0", (account_id,))
        print(f"     0-row days logged (Sundays expected): {cur.fetchone()[0]}")
    else:
        print("\n  3) sync-log empty — nothing loaded yet")
    # 4) resolution rate full span
    cur.execute("SELECT COUNT(*), SUM(criterion_id IS NOT NULL), "
                "SUM(instance_id IS NOT NULL) FROM gclid_clicks WHERE account_id=?",
                (account_id,))
    n, kw, res = cur.fetchone()
    if n:
        print(f"\n  4) full-span rates: rows={n} | kw-linked {100.0*(kw or 0)/n:.2f}% | "
              f"resolved {100.0*(res or 0)/n:.2f}%  (expected ~100%)")
    else:
        print("\n  4) gclid_clicks empty — nothing loaded yet")
    print("\n  (paste this block into the architect-thread results memo)")


# ----------------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description="GCLID click ledger sync (dry-run by default)")
    ap.add_argument("--account", default="term_life", choices=sorted(ACCOUNTS))
    ap.add_argument("--apply", action="store_true", help="write to the DB (default: dry-run)")
    ap.add_argument("--init", action="store_true", help="create tables (backup first)")
    ap.add_argument("--backfill", action="store_true",
                    help="span = T-89 .. yesterday (Phase 2 backfill-to-cliff)")
    ap.add_argument("--start", metavar="YYYY-MM-DD")
    ap.add_argument("--end", metavar="YYYY-MM-DD")
    ap.add_argument("--acceptance", action="store_true",
                    help="read-only acceptance checks, then exit")
    args = ap.parse_args()

    today = dt.date.today()
    customer_id, account_id = ACCOUNTS[args.account]
    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"17_sync_gclid_clicks.py | {mode} | account={args.account} "
          f"(customer {customer_id}, account_id {account_id}) | run date {today}")

    if args.acceptance:
        con = connect(apply_mode=False)
        acceptance(con.cursor(), account_id, today)
        con.close()
        return

    # ------------------------------------------------------------------ init / DDL
    if args.init:
        print("\n--- INIT (DDL) ---")
        for stmt in [DDL_GCLID_CLICKS, DDL_SYNC_LOG] + DDL_INDEXES:
            print(stmt + ";\n")
        if not args.apply:
            print("  dry-run: no backup taken, no DDL executed. Re-run with --apply.")
        else:
            backup_db()
            con = connect(apply_mode=True)
            with con:
                for stmt in [DDL_GCLID_CLICKS, DDL_SYNC_LOG] + DDL_INDEXES:
                    con.execute(stmt)
            con.close()
            print("  tables + indexes created.")
        if not (args.backfill or args.start):
            return

    # ------------------------------------------------------------------ span
    cliff = today - dt.timedelta(days=CLIFF_DAYS)
    yesterday = today - dt.timedelta(days=1)
    if args.backfill:
        start, end = cliff, yesterday
    elif args.start:
        start = dt.date.fromisoformat(args.start)
        end = dt.date.fromisoformat(args.end) if args.end else start
    else:
        start, end = today - dt.timedelta(days=DAILY_WINDOW), yesterday  # daily T-3..T-1

    if end > yesterday:
        print(f"  end clamped {end} -> {yesterday} (fresh days incomplete; today never pulled)")
        end = yesterday
    skipped_cliff = []
    if start < cliff:
        skipped_cliff = [d.isoformat() for d in daterange(start, cliff - dt.timedelta(days=1))]
        print(f"  !! {len(skipped_cliff)} day(s) older than T-{CLIFF_DAYS} skipped — "
              f"beyond the hard API cliff, PERMANENTLY unpullable: "
              f"{skipped_cliff[0]}..{skipped_cliff[-1]}")
        start = cliff
    if start > end:
        print("  nothing to pull in span.")
        return
    n_days = (end - start).days + 1
    print(f"  span: {start} .. {end} ({n_days} day(s), one API query each)")

    # ------------------------------------------------------------------ pull loop
    client = GoogleAdsClient.load_from_storage()
    ga_service = client.get_service("GoogleAdsService")
    con = connect(args.apply)
    cur = con.cursor()
    resolver = load_resolver(cur, account_id)

    summary, failures = [], []
    print(f"\n  {'date':<12}{'dow':<5}{'rows':>6}{'kw%':>8}{'res%':>8}{'upserted':>10}")
    for day in daterange(start, end):
        rows, err = pull_day(ga_service, customer_id, day)
        if err is not None:
            print(f"  {day.isoformat():<12}{day.strftime('%a'):<5}  FAILED: {err}")
            failures.append((day.isoformat(), err))
            continue  # NO log row for a failed day — stays a visible gap, never faked
        synced_at = now_iso()
        tuples, kw_linked, resolved = transform(rows, account_id, resolver, synced_at)
        n = len(tuples)
        kwp = f"{100.0*kw_linked/n:.1f}%" if n else "-"
        rsp = f"{100.0*resolved/n:.1f}%" if n else "-"
        note = "  (Sunday, covered)" if (n == 0 and day.weekday() == 6) else ""
        if args.apply:
            with con:
                con.executemany(UPSERT_CLICK, tuples)
                con.execute(UPSERT_LOG, (account_id, day.isoformat(), n,
                                         kw_linked, resolved, synced_at))
            print(f"  {day.isoformat():<12}{day.strftime('%a'):<5}{n:>6}{kwp:>8}{rsp:>8}"
                  f"{n:>10}{note}")
        else:
            print(f"  {day.isoformat():<12}{day.strftime('%a'):<5}{n:>6}{kwp:>8}{rsp:>8}"
                  f"{'(dry)':>10}{note}")
        summary.append((day.isoformat(), n, kw_linked, resolved))

    # ------------------------------------------------------------------ end-of-run summary
    tot = Counter()
    for _, n, kw, res in summary:
        tot["rows"] += n
        tot["kw"] += kw
        tot["res"] += res
    print(f"\n  TOTALS: days ok={len(summary)} failed={len(failures)} | rows={tot['rows']} | "
          f"kw-linked={100.0*tot['kw']/tot['rows']:.1f}% | "
          f"resolved={100.0*tot['res']/tot['rows']:.1f}%"
          if tot["rows"] else
          f"\n  TOTALS: days ok={len(summary)} failed={len(failures)} | rows=0")
    for day, err in failures:
        print(f"  !! FAILED {day}: {err}  (re-run: --start {day} --end {day} --apply)")
    if skipped_cliff:
        print(f"  !! reminder: {len(skipped_cliff)} pre-cliff day(s) permanently lost")

    # Per-day delta vs cpd — the baseline drift record (architect addendum 10/22).
    # Expected: ledger >= restated cpd on older days; the ledger never deletes gclids
    # Google later scrubbed. Drift is expected, not corruption.
    if summary:
        print("\n  per-day delta vs cpd clicks (device='ALL') — baseline drift record:")
        src_label = "ledger" if args.apply else "pulled"
        print(f"  {'date':<12}{src_label:>8}{'cpd':>8}{'delta':>8}")
        for day, n, _, _ in summary:
            if args.apply:
                cur.execute("SELECT COUNT(*) FROM gclid_clicks "
                            "WHERE account_id=? AND click_date=?", (account_id, day))
                src = cur.fetchone()[0]
            else:
                src = n
            cur.execute("SELECT COALESCE(SUM(clicks),0) FROM campaign_performance_daily "
                        "WHERE account_id=? AND date=? AND device='ALL'", (account_id, day))
            cpd = cur.fetchone()[0]
            print(f"  {day:<12}{src:>8}{cpd:>8}{src - cpd:>+8d}")

    if args.apply:
        gap_check(cur, account_id, today)
    else:
        print("\n  dry-run complete — nothing written. Add --apply to load.")
    con.close()


if __name__ == "__main__":
    main()
