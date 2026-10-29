#!/usr/bin/env python
"""
termlife_gclid_residual_probe.py — Phase 1 residual probe for the GCLID click ledger build.

Charter: GCLID Click Ledger (Term Life, acct 29). The heavy probing (field set, one-day
constraint, 90-day cliff, click-exactness, keyword linkage on 2026-10-20) is DONE — this
script covers only the residual questions before the schema proposal memo:

  S0. Sibling id types   — PRAGMA table_info on keyword_instances, campaign_performance_daily,
                           search_query_performance (+ ad_groups) so the DDL matches, not guesses.
  S1. Geo / page_number  — tier-fallback test of click_view.page_number,
                           click_view.area_of_interest.most_specific,
                           click_view.location_of_presence.most_specific on one recent
                           non-Sunday day: accepted? fill rate? worth a column?
  S2. Keyword linkage    — kw-linked % on 3 more days (spread across the 90-day window),
                           parse rates for the keyword/ad resource names, and the
                           (ad_group_id, criterion_id) -> keyword_instances resolution rate
                           (read-only DB join). Per-day cpd/kpd click cross-checks included.
  S3. Script numbering   — lists numbered scripts in campaign_db_scripts so Phase 2 picks
                           a free number (charter: don't collide with change-history 18).

READ-ONLY EVERYWHERE: Google Ads API reads only (no mutations); SQLite opened with
?mode=ro. No DDL, no writes, no backups needed.

Usage (conda env paid-search):
    python termlife_gclid_residual_probe.py
    python termlife_gclid_residual_probe.py --days 2026-10-15 2026-09-22 2026-08-03
    python termlife_gclid_residual_probe.py --geo-day 2026-10-17
"""

import argparse
import datetime as dt
import os
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
SCRIPTS_DIR = r"C:\Projects\google-ads-ops\campaign_db_scripts"
CUSTOMER_ID = "1671155176"
ACCOUNT_ID = 29

# Fields already verified accepted (probe 2026-10-22) — the base set.
BASE_FIELDS = [
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
]

# Residual candidates under test.
RESIDUAL_FIELDS = [
    "click_view.page_number",
    "click_view.area_of_interest.most_specific",
    "click_view.location_of_presence.most_specific",
]

KW_RES_RE = re.compile(r"customers/\d+/adGroupCriteria/(\d+)~(\d+)$")
AD_RES_RE = re.compile(r"customers/\d+/adGroupAds/(\d+)~(\d+)$")


# ----------------------------------------------------------------------------- helpers

def hr(title):
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


def non_sunday_back(d):
    """Step backwards off a Sunday (acct 29 runs a no-Sunday schedule)."""
    while d.weekday() == 6:
        d -= dt.timedelta(days=1)
    return d


def default_days(today):
    """Three linkage days spread across the window: ~T-7, ~T-30, ~T-80 (never Sunday,
    T-80 stays well inside the hard 90-day cliff)."""
    return [non_sunday_back(today - dt.timedelta(days=n)) for n in (7, 30, 80)]


def enum_name(v):
    try:
        return v.name
    except AttributeError:
        return str(v)


def run_gaql(ga_service, query):
    """Run a GAQL query; return (rows:list, error:str|None). Never raises."""
    try:
        rows = []
        for batch in ga_service.search_stream(customer_id=CUSTOMER_ID, query=query):
            rows.extend(batch.results)
        return rows, None
    except GoogleAdsException as ex:
        msgs = "; ".join(e.message for e in ex.failure.errors) or str(ex)
        return None, msgs
    except Exception as ex:  # transient transport errors etc. — report, don't crash
        return None, f"{type(ex).__name__}: {ex}"


def table_cols(cur, table):
    """PRAGMA table_info -> list of (name, type, notnull, pk). Empty list if absent."""
    try:
        cur.execute(f"PRAGMA table_info({table})")
        return [(r[1], r[2], r[3], r[5]) for r in cur.fetchall()]
    except sqlite3.Error as ex:
        print(f"  !! PRAGMA failed for {table}: {ex}")
        return []


# ----------------------------------------------------------------------------- sections

def section_siblings(cur):
    hr("S0. SIBLING SCHEMAS — id/type reference for the DDL (match, don't guess)")
    for table in ("keyword_instances", "campaign_performance_daily",
                  "search_query_performance", "ad_groups"):
        cols = table_cols(cur, table)
        if not cols:
            print(f"\n  {table}: NOT FOUND or unreadable")
            continue
        print(f"\n  {table} ({len(cols)} columns):")
        for name, ctype, notnull, pk in cols:
            flags = []
            if pk:
                flags.append("PK")
            if notnull:
                flags.append("NOT NULL")
            print(f"    {name:<28} {ctype:<10} {' '.join(flags)}")
    # Pull out the id columns the memo cares about, explicitly.
    print("\n  >> ID-TYPE DIGEST (for the memo):")
    for table in ("keyword_instances", "campaign_performance_daily",
                  "search_query_performance", "ad_groups"):
        cols = {n: t for n, t, _, _ in table_cols(cur, table)}
        hits = {k: v for k, v in cols.items()
                if any(tok in k.lower() for tok in
                       ("campaign_id", "ad_group_id", "criterion", "instance_id",
                        "ad_id", "account_id", "query_id"))}
        if hits:
            print(f"    {table}: " + ", ".join(f"{k}={v or '?'}" for k, v in sorted(hits.items())))


def section_geo(ga_service, geo_day):
    hr(f"S1. RESIDUAL FIELDS — tier-fallback probe on {geo_day} "
       f"({geo_day.strftime('%A')})")
    day = geo_day.isoformat()

    def q(fields):
        return (f"SELECT {', '.join(fields)} FROM click_view "
                f"WHERE segments.date = '{day}'")

    # Tier 1: base-lite + all three residuals in one query.
    lite = ["click_view.gclid", "segments.date"]
    rows, err = run_gaql(ga_service, q(lite + RESIDUAL_FIELDS))
    accepted = {}
    if err is None:
        print(f"  Combined query ACCEPTED ({len(rows)} rows) — all three residual fields valid.")
        accepted = {f: rows for f in RESIDUAL_FIELDS}
    else:
        print(f"  Combined query REJECTED: {err}")
        print("  Falling back to per-field attempts:")
        for f in RESIDUAL_FIELDS:
            frows, ferr = run_gaql(ga_service, q(lite + [f]))
            if ferr is None:
                print(f"    {f}: ACCEPTED ({len(frows)} rows)")
                accepted[f] = frows
            else:
                print(f"    {f}: REJECTED — {ferr}")

    # Fill rates / cardinality for whatever survived.
    for f, frows in accepted.items():
        attr = f.split("click_view.", 1)[1]
        vals = []
        for r in frows:
            node = r.click_view
            for part in attr.split("."):
                node = getattr(node, part, None)
                if node is None:
                    break
            vals.append(node)
        if attr == "page_number":
            nonzero = [v for v in vals if v]
            print(f"\n  {f}: {len(nonzero)}/{len(vals)} rows nonzero; "
                  f"distinct values: {sorted(set(vals))[:10]}")
        else:
            nonempty = [v for v in vals if v]
            c = Counter(nonempty)
            print(f"\n  {f}: {len(nonempty)}/{len(vals)} rows populated; "
                  f"{len(c)} distinct; top 5: {c.most_common(5)}")
    if not accepted:
        print("\n  No residual field accepted — memo verdict: drop geo/page columns.")
    print("\n  >> Memo question this answers: geo-of-click columns earn their place only if")
    print("     accepted AND populated AND cheap; campaign-grain geo already exists elsewhere.")


def load_ki_resolver(cur):
    """Build (ad_group_id, criterion_id) -> [instance_id,...] for acct 29.
    Introspects keyword_instances rather than assuming column names."""
    cols = {n for n, _, _, _ in table_cols(cur, "keyword_instances")}
    crit_col = None
    for cand in ("ad_group_criterion_id", "criterion_id"):
        if cand in cols:
            crit_col = cand
            break
    if not crit_col or "ad_group_id" not in cols:
        print(f"  !! keyword_instances lacks expected keys (cols seen: {sorted(cols)}) — "
              f"resolution test skipped; flag for the architect memo.")
        return None, None
    scope = "WHERE account_id = ?" if "account_id" in cols else ""
    sql = f"SELECT id, ad_group_id, {crit_col} FROM keyword_instances {scope}"
    cur.execute(sql, (ACCOUNT_ID,) if scope else ())
    resolver = {}
    n = 0
    for iid, agid, cid in cur.fetchall():
        if agid is None or cid is None:
            continue
        # normalize: ids may be TEXT in parts of the DB — digit-string -> canonical str
        key = (str(agid).strip(), str(cid).strip())
        resolver.setdefault(key, []).append(iid)
        n += 1
    print(f"  keyword_instances resolver loaded: {n} acct-{ACCOUNT_ID} rows, "
          f"{len(resolver)} distinct (ad_group_id, {crit_col}) keys "
          f"[{sum(1 for v in resolver.values() if len(v) > 1)} keys map to >1 instance]")
    return resolver, crit_col


def day_cross_checks(cur, day):
    """cpd + kpd clicks for the day (reconciliation only). Introspects column names."""
    out = {}
    # cpd
    cols = {n for n, _, _, _ in table_cols(cur, "campaign_performance_daily")}
    date_col = "date" if "date" in cols else ("report_date" if "report_date" in cols else None)
    if date_col and "clicks" in cols:
        if "account_id" in cols:
            sql = (f"SELECT COALESCE(SUM(clicks),0) FROM campaign_performance_daily "
                   f"WHERE account_id=? AND {date_col}=?")
            params = (ACCOUNT_ID, day)
            if "device" in cols:
                sql += " AND device='ALL'"
        else:
            sql = (f"SELECT COALESCE(SUM(p.clicks),0) FROM campaign_performance_daily p "
                   f"JOIN campaigns c ON c.campaign_id = p.campaign_id "
                   f"WHERE c.account_id=? AND p.{date_col}=?")
            params = (ACCOUNT_ID, day)
            if "device" in cols:
                sql += " AND p.device='ALL'"
        try:
            cur.execute(sql, params)
            out["cpd_clicks"] = cur.fetchone()[0]
        except sqlite3.Error as ex:
            out["cpd_clicks"] = f"ERR {ex}"
    # kpd via ki join (kpd has NO account_id; device='ALL' rollup)
    kcols = {n for n, _, _, _ in table_cols(cur, "keyword_performance_daily")}
    kdate = "report_date" if "report_date" in kcols else ("date" if "date" in kcols else None)
    if kdate and "clicks" in kcols:
        try:
            cur.execute(
                f"SELECT COALESCE(SUM(p.clicks),0) FROM keyword_performance_daily p "
                f"JOIN keyword_instances k ON k.id = p.instance_id "
                f"WHERE k.account_id=? AND p.device='ALL' AND p.{kdate}=?",
                (ACCOUNT_ID, day))
            out["kpd_clicks"] = cur.fetchone()[0]
        except sqlite3.Error as ex:
            out["kpd_clicks"] = f"ERR {ex}"
    return out


def section_linkage(ga_service, cur, days):
    hr("S2. KEYWORD LINKAGE + DB RESOLUTION — "
       + ", ".join(f"{d} ({d.strftime('%a')})" for d in days))
    resolver, crit_col = load_ki_resolver(cur)

    summary = []
    for d in days:
        day = d.isoformat()
        print(f"\n  --- {day} ({d.strftime('%A')}) ---")
        query = (f"SELECT {', '.join(BASE_FIELDS)} FROM click_view "
                 f"WHERE segments.date = '{day}'")
        rows, err = run_gaql(ga_service, query)
        if err is not None:
            print(f"  QUERY REJECTED/FAILED: {err}")
            summary.append((day, "FAIL", err))
            continue

        n = len(rows)
        gclids = set()
        kw_present = kw_parsed = resolved = multi = 0
        ad_present = ad_parsed = 0
        unresolved_samples = []
        devices, ctypes, slots = Counter(), Counter(), Counter()

        for r in rows:
            gclids.add(r.click_view.gclid)
            devices[enum_name(r.segments.device)] += 1
            ctypes[enum_name(r.segments.click_type)] += 1
            slots[enum_name(r.segments.slot)] += 1

            kw = r.click_view.keyword
            if kw:
                kw_present += 1
                m = KW_RES_RE.match(kw)
                if m:
                    kw_parsed += 1
                    key = (m.group(1), m.group(2))
                    if resolver is not None:
                        hits = resolver.get(key, [])
                        if hits:
                            resolved += 1
                            if len(hits) > 1:
                                multi += 1
                        elif len(unresolved_samples) < 5:
                            unresolved_samples.append(
                                (key, r.click_view.keyword_info.text,
                                 enum_name(r.click_view.keyword_info.match_type)))
            ad = r.click_view.ad_group_ad
            if ad:
                ad_present += 1
                if AD_RES_RE.match(ad):
                    ad_parsed += 1

        pct = lambda a, b: f"{(100.0 * a / b):.1f}%" if b else "n/a"
        print(f"  rows: {n} | distinct gclids: {len(gclids)}"
              f"{'' if len(gclids) == n else '  << DUPES: ' + str(n - len(gclids))}")
        print(f"  keyword resource present: {kw_present}/{n} ({pct(kw_present, n)}) | "
              f"parse-ok: {kw_parsed}/{kw_present} ({pct(kw_parsed, kw_present)})")
        print(f"  ad resource present: {ad_present}/{n} ({pct(ad_present, n)}) | "
              f"parse-ok: {ad_parsed}/{ad_present} ({pct(ad_parsed, ad_present)})")
        if resolver is not None:
            print(f"  resolved to keyword_instances (via ad_group_id+{crit_col}): "
                  f"{resolved}/{kw_parsed} ({pct(resolved, kw_parsed)})"
                  f"{'' if not multi else f'  [{multi} keys hit >1 instance — 1:1 check]'}")
            for key, text, mt in unresolved_samples:
                print(f"    unresolved sample: ag/crit={key} text={text!r} match={mt}")
        print(f"  device: {dict(devices)}")
        print(f"  click_type: {dict(ctypes)}")
        print(f"  slot: {dict(slots)}")

        checks = day_cross_checks(cur, day)
        cpd = checks.get("cpd_clicks", "n/a")
        kpd = checks.get("kpd_clicks", "n/a")
        tie = "TIE" if (cpd == n == kpd) else ("cpd-TIE" if cpd == n else "DELTA")
        print(f"  cross-check: click_view rows {n} vs cpd clicks {cpd} vs kpd clicks {kpd}"
              f"  -> {tie}")
        summary.append((day, "OK",
                        f"rows={n} kw-linked={pct(kw_present, n)} "
                        f"resolved={pct(resolved, kw_parsed)} cpd={cpd} kpd={kpd}"))
    return summary


def section_numbering():
    hr("S3. SCRIPT NUMBERING — free prefix for the Phase 2 sync script")
    try:
        names = sorted(os.listdir(SCRIPTS_DIR))
    except OSError as ex:
        print(f"  !! cannot list {SCRIPTS_DIR}: {ex}")
        return
    numbered = {}
    for nm in names:
        m = re.match(r"^(\d{2})[a-z]?_", nm)
        if m:
            numbered.setdefault(int(m.group(1)), []).append(nm)
    for num in sorted(numbered):
        print(f"  {num:02d}: " + ", ".join(numbered[num]))
    taken = set(numbered)
    nxt = next(i for i in range(17, 100) if i not in taken)
    print(f"\n  >> lowest free number >= 17: {nxt:02d}  "
          f"(charter proposes 17_sync_gclid_clicks.py; 18 is change-history)")


# ----------------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description="Phase 1 residual probe (read-only)")
    ap.add_argument("--days", nargs="+", metavar="YYYY-MM-DD",
                    help="override the 3 linkage test days")
    ap.add_argument("--geo-day", metavar="YYYY-MM-DD",
                    help="override the residual-fields test day")
    args = ap.parse_args()

    today = dt.date.today()
    if args.days:
        days = [dt.date.fromisoformat(d) for d in args.days]
    else:
        days = default_days(today)
    geo_day = (dt.date.fromisoformat(args.geo_day) if args.geo_day
               else non_sunday_back(today - dt.timedelta(days=3)))

    print("termlife_gclid_residual_probe.py — READ-ONLY (API reads + DB mode=ro)")
    print(f"run date: {today} | customer: {CUSTOMER_ID} | account_id: {ACCOUNT_ID}")
    print(f"geo-day: {geo_day} | linkage days: {', '.join(d.isoformat() for d in days)}")
    for d in days + [geo_day]:
        if d.weekday() == 6:
            print(f"  !! {d} is a Sunday (no-Sunday schedule) — expect 0 rows")
        if (today - d).days > 89:
            print(f"  !! {d} is beyond the 90-day cliff — the API will reject it")

    client = GoogleAdsClient.load_from_storage()
    ga_service = client.get_service("GoogleAdsService")
    con = sqlite3.connect(rf"file:{DB_PATH}?mode=ro", uri=True)
    cur = con.cursor()

    section_siblings(cur)
    section_geo(ga_service, geo_day)
    summary = section_linkage(ga_service, cur, days)
    section_numbering()

    hr("SUMMARY (paste this block + anything surprising back to the build thread)")
    for day, status, detail in summary:
        print(f"  {day}  {status}  {detail}")
    print("\n  Next step: schema proposal memo (DDL + rationale + open questions) built")
    print("  from this output, for the DB-architect thread. No DDL before ratification.")

    con.close()


if __name__ == "__main__":
    main()
