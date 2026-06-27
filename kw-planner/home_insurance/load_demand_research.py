#!/usr/bin/env python
"""
USIA Step 1 -- load KWP dry-run CSVs into demand_research.db.

This is the "graduation" from dry-run capture to committed storage. It is a
SEPARATE database from paid_search.db (no account_id, demand-grain not
performance-grain), built to ATTACH-join later: keyword_text uses the same
normal form as the 07-11 tagger, and geo_constant_id matches
paid_search.db.geo_target_constants.

MODEL (three tables + an audit table)
  kwp_ideas            one row per (vertical, keyword_text, geo, pull_date).
                       Metrics + classification (vertical_match, brand, dnb).
                       vertical is IDENTITY (verticals are separate datasets);
                       theme/seed are NOT here -- they are provenance.
  kwp_idea_provenance  one row per surfacing path (theme, seed_method, seed_term,
                       seed_url). A keyword surfaced by generic AND high_intent
                       is one idea with two provenance rows.
  idea_monthly_volumes the trailing-12-month array, exploded long (idea_id, ym).
  load_runs            audit: which CSV was loaded when, and how many rows.

Idempotent: idea_id is a deterministic hash of the identity key, so re-loading
the same CSV upserts in place rather than duplicating.

USAGE
  # load one file
  python load_demand_research.py path\\to\\usia_dryrun_home_generic_keyword_bundled_2840.csv

  # load everything in the output dir
  python load_demand_research.py --glob "C:\\Projects\\google-ads-ops\\kw-planner\\home_insurance\\output\\usia_dryrun_*.csv"

  # custom db location
  python load_demand_research.py --db C:\\Projects\\google-ads-ops\\db\\demand_research.db <csv> ...
"""

import argparse
import csv
import glob as globmod
import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone

DEFAULT_DB = r"C:\Projects\google-ads-ops\db\demand_research.db"

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS kwp_ideas (
    idea_id            TEXT PRIMARY KEY,         -- sha1(vertical|keyword|geo|pull_date)
    vertical           TEXT NOT NULL,
    keyword_text       TEXT NOT NULL,           -- normalized (matches 07-11 normal form)
    keyword_text_raw   TEXT,
    geo_constant_id    TEXT NOT NULL,
    geo_name           TEXT,
    pull_date          TEXT NOT NULL,
    avg_monthly_searches INTEGER,
    competition        TEXT,
    competition_index  INTEGER,
    low_cpc_usd        REAL,
    high_cpc_usd       REAL,
    vertical_match     TEXT,                    -- on_vertical / neutral / mixed / cross_vertical
    detected_verticals TEXT,
    would_exclude      INTEGER,                 -- 0/1
    brand              TEXT,
    brand_bid_status   TEXT,                    -- restricted / conditional / approved / ''
    brand_category     TEXT,
    dnb_flag           INTEGER,                 -- 0/1
    brand_source       TEXT,
    UNIQUE (vertical, keyword_text, geo_constant_id, pull_date)
);

CREATE TABLE IF NOT EXISTS kwp_idea_provenance (
    idea_id     TEXT NOT NULL REFERENCES kwp_ideas(idea_id) ON DELETE CASCADE,
    theme       TEXT,
    seed_method TEXT,
    seed_term   TEXT,
    seed_url    TEXT,
    pull_date   TEXT,
    UNIQUE (idea_id, theme, seed_method, seed_term, seed_url)
);

CREATE TABLE IF NOT EXISTS idea_monthly_volumes (
    idea_id  TEXT NOT NULL REFERENCES kwp_ideas(idea_id) ON DELETE CASCADE,
    year_month TEXT NOT NULL,                   -- 'YYYY-MM'
    searches INTEGER,
    UNIQUE (idea_id, year_month)
);

CREATE TABLE IF NOT EXISTS load_runs (
    run_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    csv_path   TEXT,
    loaded_at  TEXT,
    rows_read  INTEGER,
    ideas_upserted INTEGER,
    provenance_upserted INTEGER,
    monthly_upserted INTEGER
);

CREATE INDEX IF NOT EXISTS ix_ideas_vert_geo  ON kwp_ideas(vertical, geo_constant_id);
CREATE INDEX IF NOT EXISTS ix_ideas_keyword   ON kwp_ideas(keyword_text);
CREATE INDEX IF NOT EXISTS ix_ideas_brand     ON kwp_ideas(brand);
CREATE INDEX IF NOT EXISTS ix_ideas_vmatch    ON kwp_ideas(vertical_match);
CREATE INDEX IF NOT EXISTS ix_ideas_dnb       ON kwp_ideas(dnb_flag);
CREATE INDEX IF NOT EXISTS ix_prov_idea       ON kwp_idea_provenance(idea_id);
CREATE INDEX IF NOT EXISTS ix_prov_theme      ON kwp_idea_provenance(theme);
CREATE INDEX IF NOT EXISTS ix_monthly_idea    ON idea_monthly_volumes(idea_id);
CREATE INDEX IF NOT EXISTS ix_monthly_ym      ON idea_monthly_volumes(year_month);
"""


def ensure_schema(conn):
    conn.executescript(SCHEMA)
    conn.commit()


def to_int(v, default=0):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def to_real(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def to_bool(v):
    return 1 if str(v).strip().lower() in ("1", "true", "yes") else 0


def idea_id_for(vertical, keyword_text, geo, pull_date):
    key = "|".join([vertical or "", keyword_text or "", geo or "", pull_date or ""])
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


def load_csv(conn, path):
    with open(path, "r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    cur = conn.cursor()
    ideas = prov = monthly = 0

    for r in rows:
        g = r.get  # old-schema CSVs lack brand/seed_url columns -> default ''
        vertical = g("vertical", "")
        keyword = g("keyword_text", "")
        geo = g("geo_constant_id", "")
        pull_date = g("pull_date", "")
        if not keyword or not vertical:
            continue
        iid = idea_id_for(vertical, keyword, geo, pull_date)

        cur.execute("""
            INSERT INTO kwp_ideas (
                idea_id, vertical, keyword_text, keyword_text_raw, geo_constant_id,
                geo_name, pull_date, avg_monthly_searches, competition, competition_index,
                low_cpc_usd, high_cpc_usd, vertical_match, detected_verticals, would_exclude,
                brand, brand_bid_status, brand_category, dnb_flag, brand_source
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(idea_id) DO UPDATE SET
                vertical=excluded.vertical, keyword_text=excluded.keyword_text,
                keyword_text_raw=excluded.keyword_text_raw, geo_constant_id=excluded.geo_constant_id,
                geo_name=excluded.geo_name, pull_date=excluded.pull_date,
                avg_monthly_searches=excluded.avg_monthly_searches, competition=excluded.competition,
                competition_index=excluded.competition_index, low_cpc_usd=excluded.low_cpc_usd,
                high_cpc_usd=excluded.high_cpc_usd, vertical_match=excluded.vertical_match,
                detected_verticals=excluded.detected_verticals, would_exclude=excluded.would_exclude,
                brand=excluded.brand, brand_bid_status=excluded.brand_bid_status,
                brand_category=excluded.brand_category, dnb_flag=excluded.dnb_flag,
                brand_source=excluded.brand_source
        """, (
            iid, vertical, keyword, g("keyword_text_raw", ""), geo,
            g("geo_name", ""), pull_date, to_int(g("avg_monthly_searches")),
            g("competition", ""), to_int(g("competition_index")),
            to_real(g("low_cpc_usd")), to_real(g("high_cpc_usd")),
            g("vertical_match", ""), g("detected_verticals", ""), to_bool(g("would_exclude")),
            g("brand", ""), g("brand_bid_status", ""), g("brand_category", ""),
            to_bool(g("dnb_flag")), g("brand_source", ""),
        ))
        ideas += 1

        cur.execute("""
            INSERT OR IGNORE INTO kwp_idea_provenance
                (idea_id, theme, seed_method, seed_term, seed_url, pull_date)
            VALUES (?,?,?,?,?,?)
        """, (iid, g("theme", ""), g("seed_method", ""), g("seed_term", ""),
              g("seed_url", ""), pull_date))
        prov += cur.rowcount if cur.rowcount > 0 else 0

        raw = g("monthly_volumes_json", "") or "[]"
        try:
            for mv in json.loads(raw):
                ym = mv.get("ym")
                if not ym:
                    continue
                cur.execute("""
                    INSERT OR REPLACE INTO idea_monthly_volumes (idea_id, year_month, searches)
                    VALUES (?,?,?)
                """, (iid, ym, to_int(mv.get("searches"))))
                monthly += 1
        except (json.JSONDecodeError, TypeError):
            pass

    cur.execute("""
        INSERT INTO load_runs (csv_path, loaded_at, rows_read, ideas_upserted,
                               provenance_upserted, monthly_upserted)
        VALUES (?,?,?,?,?,?)
    """, (os.path.abspath(path), datetime.now(timezone.utc).isoformat(timespec="seconds"),
          len(rows), ideas, prov, monthly))
    conn.commit()
    return len(rows), ideas, prov, monthly


def summarize(conn):
    cur = conn.cursor()
    print("\n" + "=" * 64)
    print("demand_research.db -- current totals")
    print("=" * 64)
    for tbl in ("kwp_ideas", "kwp_idea_provenance", "idea_monthly_volumes"):
        n = cur.execute("SELECT COUNT(*) FROM %s" % tbl).fetchone()[0]
        print("  %-22s %d rows" % (tbl, n))
    print("\n  kwp_ideas by vertical:")
    for v, n in cur.execute("SELECT vertical, COUNT(*) FROM kwp_ideas GROUP BY vertical ORDER BY 2 DESC"):
        print("      %-10s %d" % (v, n))
    print("\n  provenance by theme:")
    for t, n in cur.execute("SELECT theme, COUNT(*) FROM kwp_idea_provenance GROUP BY theme ORDER BY 2 DESC"):
        print("      %-14s %d" % (t or "(none)", n))
    print("=" * 64)


def main():
    p = argparse.ArgumentParser(description="Load USIA KWP dry-run CSVs into demand_research.db.")
    p.add_argument("csvs", nargs="*", help="CSV file paths to load.")
    p.add_argument("--glob", help="glob pattern for CSVs to load (e.g. output/usia_dryrun_*.csv).")
    p.add_argument("--db", default=DEFAULT_DB, help="path to demand_research.db.")
    args = p.parse_args()

    paths = list(args.csvs)
    if args.glob:
        paths += globmod.glob(args.glob)
    paths = sorted(set(os.path.abspath(x) for x in paths))
    if not paths:
        p.error("no CSVs given (pass file paths and/or --glob).")

    os.makedirs(os.path.dirname(args.db) or ".", exist_ok=True)
    conn = sqlite3.connect(args.db)
    conn.execute("PRAGMA busy_timeout=15000;")
    ensure_schema(conn)

    print("DB: %s" % args.db)
    for path in paths:
        if not os.path.exists(path):
            print("  [skip] not found: %s" % path)
            continue
        rr, i, pr, mo = load_csv(conn, path)
        print("  loaded %-60s rows=%d ideas=%d prov=%d monthly=%d"
              % (os.path.basename(path), rr, i, pr, mo))

    summarize(conn)
    conn.close()


if __name__ == "__main__":
    main()
