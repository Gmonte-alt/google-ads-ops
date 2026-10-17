#!/usr/bin/env python
"""
06_sync_keyword_quality.py -- kw_db_scripts\ -- daily historical Quality Score
into its OWN table, decoupled from 03 (2026-08-12 decision: a QS-side API quirk
must never block the conversion sync -- the coupled v1 attempt aborted 03's
Pass 1a because the API forbids segments.ad_network_type alongside
historical_* metrics).

Table: keyword_quality_daily
  (instance_id, report_date) PK
  historical_quality_score   INTEGER   1-10; NULL = unrated
  hist_creative_quality      TEXT      ad-relevance bucket
  hist_landing_page_quality  TEXT
  hist_predicted_ctr         TEXT

Reading rules: QS is a keyword-day property -- never summed; impression-weighted
averages only (join kpd ALL rows for weights); NULL = unrated, excluded not zeroed.

    python 06_sync_keyword_quality.py --account home --refresh 15
    python 06_sync_keyword_quality.py --account home --backfill --start-date 2024-04-01 --end-date 2026-08-12
"""
import argparse
import datetime
import os
import sqlite3
import sys

from dotenv import load_dotenv
load_dotenv()

from google.ads.googleads.client import GoogleAdsClient
from google.ads.googleads.errors import GoogleAdsException

DB_DEFAULT = r"C:\Projects\google-ads-ops\db\paid_search.db"
ACCOUNTS = {"home": ("8886502147", 1),
            "term_life": ("1671155176", 29)}  # name -> (customer_id, account_id)

DDL = """
CREATE TABLE IF NOT EXISTS keyword_quality_daily (
    instance_id               INTEGER NOT NULL,
    report_date               TEXT    NOT NULL,
    historical_quality_score  INTEGER,
    hist_creative_quality     TEXT,
    hist_landing_page_quality TEXT,
    hist_predicted_ctr        TEXT,
    last_synced_at            TEXT,
    PRIMARY KEY (instance_id, report_date)
);
CREATE INDEX IF NOT EXISTS idx_kqd_date ON keyword_quality_daily (report_date);
"""


def qs_bucket(enum_val):
    try:
        name = enum_val.name
    except AttributeError:
        return None
    return None if name in ("UNSPECIFIED", "UNKNOWN") else name


def month_chunks(start, end):
    s = datetime.date.fromisoformat(start)
    e = datetime.date.fromisoformat(end)
    while s <= e:
        nxt = (s.replace(day=1) + datetime.timedelta(days=32)).replace(day=1)
        yield s.isoformat(), min(e, nxt - datetime.timedelta(days=1)).isoformat()
        s = nxt


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DB_DEFAULT)
    ap.add_argument("--account", default="home", choices=list(ACCOUNTS))
    ap.add_argument("--refresh", type=int, help="sync the last N days")
    ap.add_argument("--backfill", action="store_true")
    ap.add_argument("--start-date")
    ap.add_argument("--end-date")
    args = ap.parse_args()

    customer_id, account_id = ACCOUNTS[args.account]
    if args.backfill:
        if not (args.start_date and args.end_date):
            sys.exit("[ABORT] --backfill needs --start-date and --end-date")
        start, end = args.start_date, args.end_date
    else:
        days = args.refresh or 15
        end = datetime.date.today().isoformat()
        start = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()

    conn = sqlite3.connect(args.db)
    conn.executescript(DDL)
    # instance lookup: (criterion_id, ad_group_id) -> instance_id
    inst = {(r[0], r[1]): r[2] for r in conn.execute(
        "SELECT criterion_id, ad_group_id, id FROM keyword_instances WHERE account_id = ?",
        (account_id,))}
    print("=" * 65)
    print("06_sync_keyword_quality.py  account=%s  %s .. %s" % (args.account, start, end))
    print("  instances loaded: %s" % format(len(inst), ","))
    print("=" * 65)

    client = GoogleAdsClient.load_from_storage()
    ga = client.get_service("GoogleAdsService")
    now = datetime.datetime.now().isoformat(timespec="seconds")
    tot_rows = tot_written = tot_skipped = 0

    for cs, ce in month_chunks(start, end):
        print("[%s -> %s] " % (cs, ce), end="", flush=True)
        query = f"""
            SELECT
                ad_group_criterion.criterion_id,
                ad_group.id,
                segments.date,
                metrics.historical_quality_score,
                metrics.historical_creative_quality_score,
                metrics.historical_landing_page_quality_score,
                metrics.historical_search_predicted_ctr
            FROM keyword_view
            WHERE segments.date BETWEEN '{cs}' AND '{ce}'
              AND ad_group_criterion.type = 'KEYWORD'
              AND ad_group_criterion.status IN ('ENABLED', 'PAUSED')
              AND campaign.advertising_channel_type = 'SEARCH'
        """
        rows, written, skipped = [], 0, 0
        try:
            for batch in ga.search_stream(customer_id=customer_id, query=query):
                for row in batch.results:
                    rows.append((
                        str(row.ad_group_criterion.criterion_id),
                        str(row.ad_group.id),
                        row.segments.date,
                        row.metrics.historical_quality_score or None,
                        qs_bucket(row.metrics.historical_creative_quality_score),
                        qs_bucket(row.metrics.historical_landing_page_quality_score),
                        qs_bucket(row.metrics.historical_search_predicted_ctr),
                    ))
        except GoogleAdsException as ex:
            for err in ex.failure.errors:
                print("\n  [API ERROR]", err.message)
            conn.close()
            sys.exit(1)

        batch_sql = []
        for crit, agid, date_, hqs, cq, lp, ctr in rows:
            iid = inst.get((crit, agid))
            if iid is None:
                skipped += 1
                continue
            if hqs is None and cq is None and lp is None and ctr is None:
                continue  # fully unrated keyword-day: store nothing
            batch_sql.append((iid, date_, hqs, cq, lp, ctr, now))
            written += 1
        conn.executemany(
            """INSERT OR REPLACE INTO keyword_quality_daily
               (instance_id, report_date, historical_quality_score,
                hist_creative_quality, hist_landing_page_quality,
                hist_predicted_ctr, last_synced_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""", batch_sql)
        conn.commit()
        print("%s api rows | %s written | %s skipped (no instance)"
              % (format(len(rows), ","), format(written, ","), format(skipped, ",")))
        tot_rows += len(rows); tot_written += written; tot_skipped += skipped

    print("=" * 65)
    print("DONE. api rows %s | written %s | skipped %s"
          % (format(tot_rows, ","), format(tot_written, ","), format(tot_skipped, ",")))
    r = conn.execute("""SELECT COUNT(*), ROUND(AVG(historical_quality_score),2),
                        MIN(report_date), MAX(report_date)
                        FROM keyword_quality_daily""").fetchone()
    print("table now: %s rows | avg QS %s | %s .. %s" % (format(r[0], ","), r[1], r[2], r[3]))
    conn.close()


if __name__ == "__main__":
    main()
