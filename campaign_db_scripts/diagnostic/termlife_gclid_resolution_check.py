#!/usr/bin/env python
"""
termlife_gclid_resolution_check.py — Phase 1 follow-up to the residual probe.

The residual probe resolved 0/2155 clicks against keyword_instances because it keyed on
(ad_group_id, ad_group_criterion_id); the parsed click_view pairs are (ad_group_id,
criterion_id). This check settles, with data, everything the schema memo still needs:

  C1. ad_group_criterion_id FORMAT — raw keyword_instances rows for the ad groups seen in
      the probe, showing criterion_id vs ad_group_criterion_id side by side.
  C2. RESOLUTION RATE, correctly keyed — same three days (2026-10-15 / 09-22 / 08-03),
      resolution tested under three candidate keys:
        K1 (ad_group_id, criterion_id)
        K2 (ad_group_id, ad_group_criterion_id)            [probe's key — known 0%]
        K3 ad_group_criterion_id == "{ad_group_id}~{criterion_id}" composite
  C3. GEO JOIN COVERAGE — do the click_view geoTargetConstants ids exist in
      geo_target_constants? (decides whether geo columns are joinable or dead weight)
  C4. TYPE REFERENCE — PRAGMA on geo_target_constants, keyword_performance_daily
      (geo_target_constant_id type) and rsa_ads (ad_id type) for the DDL.

READ-ONLY: API reads only; DB opened ?mode=ro. ~3 API queries.

Usage (conda env paid-search):
    python termlife_gclid_resolution_check.py
"""

import re
import sqlite3
import sys
from collections import Counter

sys.stdout.reconfigure(encoding="utf-8")

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from google.ads.googleads.client import GoogleAdsClient  # noqa: E402
from google.ads.googleads.errors import GoogleAdsException  # noqa: E402

DB_PATH = r"C:\Projects\google-ads-ops\db\paid_search.db"
CUSTOMER_ID = "1671155176"
ACCOUNT_ID = 29
DAYS = ["2026-10-15", "2026-09-22", "2026-08-03"]  # same days as the residual probe
PROBE_AD_GROUPS = ["183626709335", "186544674655", "183626709575",
                   "196382159479", "185554641084"]
PROBE_CRITERIA = ["345888978444", "379744884443", "11630181",
                  "650577928312", "889940364893", "436628155346"]

KW_RES_RE = re.compile(r"customers/\d+/adGroupCriteria/(\d+)~(\d+)$")
GEO_RES_RE = re.compile(r"geoTargetConstants/(\d+)$")


def hr(title):
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


def table_cols(cur, table):
    try:
        cur.execute(f"PRAGMA table_info({table})")
        return [(r[1], r[2]) for r in cur.fetchall()]
    except sqlite3.Error as ex:
        print(f"  !! PRAGMA failed for {table}: {ex}")
        return []


def main():
    con = sqlite3.connect(rf"file:{DB_PATH}?mode=ro", uri=True)
    cur = con.cursor()

    # ------------------------------------------------------------------ C1: format
    hr("C1. ad_group_criterion_id FORMAT — raw rows for the probe's ad groups")
    ph = ",".join("?" * len(PROBE_AD_GROUPS))
    cur.execute(
        f"SELECT id, ad_group_id, criterion_id, ad_group_criterion_id, "
        f"keyword_text, match_type, status "
        f"FROM keyword_instances WHERE account_id=? AND ad_group_id IN ({ph}) "
        f"ORDER BY ad_group_id LIMIT 15", (ACCOUNT_ID, *PROBE_AD_GROUPS))
    rows = cur.fetchall()
    if not rows:
        print("  !! no keyword_instances rows for the probe ad groups — report this verbatim")
    for r in rows:
        print(f"  id={r[0]} ag={r[1]} crit={r[2]!r} ag_crit={r[3]!r} "
              f"text={r[4]!r} match={r[5]} status={r[6]}")
    # Also: any row anywhere whose criterion_id matches a probe criterion?
    ph2 = ",".join("?" * len(PROBE_CRITERIA))
    cur.execute(
        f"SELECT COUNT(*) FROM keyword_instances "
        f"WHERE account_id=? AND criterion_id IN ({ph2})", (ACCOUNT_ID, *PROBE_CRITERIA))
    print(f"\n  acct-29 rows whose criterion_id is one of the probe's 6 criteria: "
          f"{cur.fetchone()[0]}")
    cur.execute(
        "SELECT ad_group_criterion_id FROM keyword_instances "
        "WHERE account_id=? AND ad_group_criterion_id IS NOT NULL LIMIT 3", (ACCOUNT_ID,))
    print(f"  ad_group_criterion_id samples: {[r[0] for r in cur.fetchall()]}")

    # Build all three resolvers.
    cur.execute(
        "SELECT id, ad_group_id, criterion_id, ad_group_criterion_id "
        "FROM keyword_instances WHERE account_id=?", (ACCOUNT_ID,))
    k1, k2, k3 = {}, {}, {}
    for iid, agid, cid, agcid in cur.fetchall():
        agid = str(agid).strip() if agid is not None else None
        if agid is None:
            continue
        if cid is not None:
            k1.setdefault((agid, str(cid).strip()), []).append(iid)
        if agcid is not None:
            s = str(agcid).strip()
            k2.setdefault((agid, s), []).append(iid)
            k3.setdefault(s, []).append(iid)
    print(f"\n  resolver sizes: K1(ag,crit)={len(k1)} K2(ag,ag_crit)={len(k2)} "
          f"K3(composite)={len(k3)}")
    multi1 = sum(1 for v in k1.values() if len(v) > 1)
    if multi1:
        print(f"  !! K1 has {multi1} (ag,crit) keys mapping to >1 instance — "
              f"breaks the 1:1 assumption, report")

    # ------------------------------------------------------------------ C2 + C3: API days
    hr("C2/C3. RESOLUTION (3 keys) + GEO IDS — days " + ", ".join(DAYS))
    client = GoogleAdsClient.load_from_storage()
    ga = client.get_service("GoogleAdsService")
    fields = ["click_view.gclid", "click_view.keyword",
              "click_view.area_of_interest.most_specific",
              "click_view.location_of_presence.most_specific",
              "segments.date"]
    geo_aoi, geo_lop = set(), set()
    totals = Counter()
    for day in DAYS:
        q = (f"SELECT {', '.join(fields)} FROM click_view "
             f"WHERE segments.date = '{day}'")
        try:
            rows = []
            for batch in ga.search_stream(customer_id=CUSTOMER_ID, query=q):
                rows.extend(batch.results)
        except GoogleAdsException as ex:
            print(f"  {day}: QUERY FAILED — "
                  + "; ".join(e.message for e in ex.failure.errors))
            continue
        c = Counter()
        unresolved = []
        for r in rows:
            c["rows"] += 1
            m = KW_RES_RE.match(r.click_view.keyword or "")
            if not m:
                continue
            c["parsed"] += 1
            agid, cid = m.group(1), m.group(2)
            if (agid, cid) in k1:
                c["K1"] += 1
            elif len(unresolved) < 3:
                unresolved.append((agid, cid))
            if (agid, cid) in k2:
                c["K2"] += 1
            if f"{agid}~{cid}" in k3:
                c["K3"] += 1
            g = GEO_RES_RE.match(r.click_view.area_of_interest.most_specific or "")
            if g:
                geo_aoi.add(g.group(1))
            g = GEO_RES_RE.match(r.click_view.location_of_presence.most_specific or "")
            if g:
                geo_lop.add(g.group(1))
        totals.update(c)
        pct = lambda a: f"{100.0 * c[a] / c['parsed']:.1f}%" if c["parsed"] else "n/a"
        print(f"  {day}: rows={c['rows']} parsed={c['parsed']} | "
              f"K1(ag,crit)={c['K1']} ({pct('K1')}) | K2={c['K2']} ({pct('K2')}) | "
              f"K3(composite)={c['K3']} ({pct('K3')})")
        for agid, cid in unresolved:
            print(f"    K1-unresolved sample: ag={agid} crit={cid}")
    if totals["parsed"]:
        print(f"\n  >> OVERALL: parsed={totals['parsed']} "
              f"K1={totals['K1']} ({100.0*totals['K1']/totals['parsed']:.1f}%) "
              f"K2={totals['K2']} ({100.0*totals['K2']/totals['parsed']:.1f}%) "
              f"K3={totals['K3']} ({100.0*totals['K3']/totals['parsed']:.1f}%)")

    # Geo join coverage against geo_target_constants.
    print(f"\n  distinct geo ids seen: location_of_presence={len(geo_lop)}, "
          f"area_of_interest={len(geo_aoi)}")
    gtc_cols = table_cols(cur, "geo_target_constants")
    id_col = None
    for cand in ("geo_target_constant_id", "id", "criterion_id"):
        if cand in {n for n, _ in gtc_cols}:
            id_col = cand
            break
    if id_col:
        def coverage(ids, label):
            if not ids:
                print(f"  {label}: no ids to test")
                return
            found = 0
            idl = list(ids)
            for i in range(0, len(idl), 500):
                chunk = idl[i:i + 500]
                ph = ",".join("?" * len(chunk))
                # ids may be stored TEXT or INTEGER — CAST both sides to TEXT
                cur.execute(
                    f"SELECT COUNT(DISTINCT CAST({id_col} AS TEXT)) "
                    f"FROM geo_target_constants "
                    f"WHERE CAST({id_col} AS TEXT) IN ({ph})", chunk)
                found += cur.fetchone()[0]
            print(f"  {label}: {found}/{len(ids)} ids present in geo_target_constants "
                  f"({100.0 * found / len(ids):.1f}%)")
        coverage(geo_lop, "location_of_presence join coverage")
        coverage(geo_aoi, "area_of_interest join coverage")
    else:
        print("  !! geo_target_constants id column not identified — see C4 pragma")

    # ------------------------------------------------------------------ C4: types
    hr("C4. TYPE REFERENCE — geo_target_constants / kpd geo id / rsa_ads ad_id")
    for table in ("geo_target_constants", "rsa_ads"):
        cols = table_cols(cur, table)
        print(f"\n  {table}:" if cols else f"\n  {table}: NOT FOUND")
        for n, t in cols:
            print(f"    {n:<30} {t}")
    kcols = dict(table_cols(cur, "keyword_performance_daily"))
    print(f"\n  keyword_performance_daily geo columns: "
          f"geo_target_constant_id={kcols.get('geo_target_constant_id', 'ABSENT')!r}, "
          f"geo_state={kcols.get('geo_state', 'ABSENT')!r}")

    hr("DONE — paste this output back; the schema proposal memo follows from it.")
    con.close()


if __name__ == "__main__":
    main()
