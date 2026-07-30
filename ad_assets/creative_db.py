#!/usr/bin/env python
"""
creative_db.py -- ad_assets/ -- READ-ONLY query library over paid_search.db's
creative tables, importable by other subsystems' build scripts (first
consumer: the bulk-import thread's BCC-01 pause-row generator).

This module is the ad_assets subsystem's public query interface: other
scripts import THIS instead of writing their own SQL against ad_assets
tables. It opens the DB in read-only mode (mode=ro URI) -- it cannot write,
so importing it is safe by construction.

Usage from a build script:

    import sys
    sys.path.insert(0, r"C:\\Projects\\google-ads-ops\\ad_assets")
    from creative_db import CreativeDB

    db = CreativeDB(r"C:\\Projects\\google-ads-ops\\db\\paid_search.db")
    comp = db.ad_composition(1234567890)
    # {'headlines': ['...', ...], 'descriptions': ['...', ...]}
    all72 = db.ads_composition([id1, id2, ...])   # {ad_id: {...}}
    perf  = db.ad_performance([id1, id2], days=30)
    rsas  = db.enabled_rsas_in_ad_groups([ag1, ag2])
    name  = db.ad_group_name(ag1)

KNOWN LIMIT (state it in any consumer): the DB stores each ad's asset SET,
not slot ORDER -- RSA headline order isn't meaningful unpinned, and Google's
API doesn't expose a stable ordering. Compositions are returned
alphabetically for determinism. If Ads Editor matching for pause rows proves
ORDER-sensitive, fall back to the Editor's own export for the pause rows
(content from this module remains the verification source).

CLI fallback (emits the Option-A CSV without writing any import code):

    python creative_db.py --db "...\\paid_search.db" ^
        --composition-csv "...\\exports\\bcc01_pause_ranking_2026-07-23.csv"
    -> ad_assets\\exports\\incumbent_ad_content_<date>.csv
       (ad_id, field_type, n, text) for every ad_id in the input CSV
"""
import csv
import datetime
import os
import sqlite3

ACCOUNT_ID = 1

_NAME_TABLE_CANDIDATES = [
    ("ad_groups", "ad_group_id", "ad_group_name"),
    ("ad_groups", "ad_group_id", "name"),
    ("ad_groups", "id", "name"),
]


class CreativeDB:
    """Read-only accessor over the ad_assets tables in paid_search.db."""

    def __init__(self, db_path, account_id=ACCOUNT_ID):
        if not os.path.isfile(db_path):
            raise FileNotFoundError(db_path)
        self.account_id = account_id
        self._conn = sqlite3.connect(
            "file:%s?mode=ro" % db_path.replace("\\", "/"), uri=True)
        self._names = None

    # -- composition ---------------------------------------------------------
    def ad_composition(self, ad_id):
        """Current (non-removed) headlines and descriptions for one ad.
        String ids accepted and coerced."""
        ad_id = int(ad_id)
        rows = self._conn.execute(
            """SELECT c.field_type, t.text
               FROM rsa_ad_assets c
               JOIN asset_text_data t
                 ON t.account_id = c.account_id AND t.asset_id = c.asset_id
               WHERE c.account_id = ? AND c.ad_id = ? AND c.removed_date IS NULL
               ORDER BY c.field_type, t.text""",
            (self.account_id, ad_id)).fetchall()
        out = {"headlines": [], "descriptions": []}
        for ft, text in rows:
            key = "headlines" if ft == "HEADLINE" else (
                  "descriptions" if ft == "DESCRIPTION" else None)
            if key:
                out[key].append(text)
        return out

    def ads_composition(self, ad_ids):
        """{ad_id: composition} for many ads (one query, chunked).
        Keys are ALWAYS ints; string ids are accepted and coerced."""
        out = {}
        ids = [int(a) for a in ad_ids]
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            marks = ",".join("?" * len(chunk))
            rows = self._conn.execute(
                f"""SELECT c.ad_id, c.field_type, t.text
                    FROM rsa_ad_assets c
                    JOIN asset_text_data t
                      ON t.account_id = c.account_id AND t.asset_id = c.asset_id
                    WHERE c.account_id = ? AND c.removed_date IS NULL
                      AND c.ad_id IN ({marks})
                    ORDER BY c.ad_id, c.field_type, t.text""",
                [self.account_id] + chunk).fetchall()
            for ad_id, ft, text in rows:
                d = out.setdefault(ad_id, {"headlines": [], "descriptions": []})
                if ft == "HEADLINE":
                    d["headlines"].append(text)
                elif ft == "DESCRIPTION":
                    d["descriptions"].append(text)
        return out

    # -- inventory & identity ------------------------------------------------
    def enabled_rsas_in_ad_groups(self, ad_group_ids):
        """[(ad_id, ad_group_id, campaign_id, fingerprint, variant_label)] enabled only."""
        ids = list(ad_group_ids)
        marks = ",".join("?" * len(ids))
        return self._conn.execute(
            f"""SELECT ad_id, ad_group_id, campaign_id, fingerprint, variant_label
                FROM rsa_ads
                WHERE account_id = ? AND ad_status = 'ENABLED'
                  AND ad_group_id IN ({marks})
                ORDER BY ad_group_id, ad_id""",
            [self.account_id] + ids).fetchall()

    def fingerprint_of(self, ad_id):
        ad_id = int(ad_id)
        row = self._conn.execute(
            "SELECT fingerprint FROM rsa_ads WHERE account_id=? AND ad_id=?",
            (self.account_id, ad_id)).fetchone()
        return row[0] if row else None

    # -- performance (additive ad grain ONLY) --------------------------------
    def ad_performance(self, ad_ids, days=30):
        """{ad_id: {impressions, clicks, conversions, value}} over the window.
        Ad grain -- fully additive. (Per-asset tables are presence-conditioned
        and are deliberately NOT exposed here for consumer aggregation.)
        Keys are ALWAYS ints; string ids accepted and coerced."""
        date_from = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
        ids = [int(a) for a in ad_ids]
        marks = ",".join("?" * len(ids))
        rows = self._conn.execute(
            f"""SELECT ad_id, SUM(impressions), SUM(clicks),
                       SUM(conversions), SUM(conversions_value)
                FROM rsa_ad_performance_daily
                WHERE account_id = ? AND device = 'ALL' AND report_date >= ?
                  AND ad_id IN ({marks})
                GROUP BY ad_id""",
            [self.account_id, date_from] + ids).fetchall()
        return {ad: {"impressions": i or 0, "clicks": c or 0,
                     "conversions": cv or 0.0, "value": v or 0.0}
                for ad, i, c, cv, v in rows}

    # -- names ----------------------------------------------------------------
    def ad_group_name(self, ad_group_id):
        ad_group_id = int(ad_group_id)
        if self._names is None:
            self._names = {}
            for table, idc, namec in _NAME_TABLE_CANDIDATES:
                try:
                    self._names = {int(i): (n or "") for i, n in self._conn.execute(
                        f"SELECT {idc}, {namec} FROM {table}")}
                    break
                except sqlite3.Error:
                    continue
        return self._names.get(ad_group_id, "")

    def close(self):
        self._conn.close()


# ---------------------------------------------------------------------------
def _cli():
    import argparse
    p = argparse.ArgumentParser(description="CLI fallback: export ad compositions to CSV.")
    p.add_argument("--db", required=True)
    p.add_argument("--composition-csv", required=True,
                   help="Any CSV with an ad_id column (e.g. bcc01_pause_ranking_*.csv)")
    args = p.parse_args()

    with open(args.composition_csv, newline="", encoding="utf-8") as f:
        ad_ids = sorted({int(r["ad_id"]) for r in csv.DictReader(f)})
    db = CreativeDB(args.db)
    comps = db.ads_composition(ad_ids)
    db.close()

    export_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "exports")
    os.makedirs(export_dir, exist_ok=True)
    out = os.path.join(export_dir, "incumbent_ad_content_%s.csv"
                       % datetime.date.today().isoformat())
    missing = [a for a in ad_ids if a not in comps]
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["ad_id", "field_type", "n", "text"])
        for ad_id in ad_ids:
            comp = comps.get(ad_id, {"headlines": [], "descriptions": []})
            for i, h in enumerate(comp["headlines"], 1):
                w.writerow([ad_id, "HEADLINE", i, h])
            for i, d in enumerate(comp["descriptions"], 1):
                w.writerow([ad_id, "DESCRIPTION", i, d])
    print("[DONE] %d ad(s) -> %s" % (len(ad_ids) - len(missing), out))
    if missing:
        print("[CHECK] %d ad_id(s) with no composition rows (not in rsa tables?): %s"
              % (len(missing), missing[:10]))


if __name__ == "__main__":
    _cli()
