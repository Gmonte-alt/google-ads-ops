#!/usr/bin/env python3
"""
generic_spend_rate.py — daily spend rate of ALL generic ad groups
combined (ad groups named 'Home-Generic*'), EXCLUDING the California
and Florida campaigns, over trailing 30/14/7 days. Informs the
Generic campaign's daily budget at Phase 1 post.

Read-only. Resolves tables at runtime per house conventions.
Usage: python generic_spend_rate.py --db C:\\Projects\\google-ads-ops\\db\\paid_search.db
"""
import argparse
import datetime as dt
import sqlite3
import sys
from collections import defaultdict


def die(m):
    print(f"*** HARD FAIL: {m}")
    sys.exit(1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    a = ap.parse_args()
    con = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    tables = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}

    # campaign scope: account 1, the five non-CA/FL serving campaigns
    camp = {}
    for r in con.execute("SELECT campaign_id, campaign_name FROM "
                         "campaigns WHERE account_id=1"):
        n = (r["campaign_name"] or "").upper()
        if "CALIFORNIA" in n or "FLORIDA" in n:
            continue
        if any(s in n for s in ("TEXAS", "GENERIC-HOME-INTENT",
                                "TIER1", "TIER2", "TIER3", "TIER4")):
            camp[r["campaign_id"]] = r["campaign_name"]
    if not camp:
        die("no in-scope campaigns resolved from campaigns table")
    print(f"[SCOPE] {len(camp)} campaigns (CA/FL excluded):")
    for cid, n in camp.items():
        print(f"   {cid}  {n[:70]}")

    # source: prefer an ad-group-grain daily table; else kpd + instances
    def cols(t):
        return {r[1] for r in con.execute(f"PRAGMA table_info({t})")}
    src = None
    for t in ("ad_group_performance_daily", "adgroup_performance_daily",
              "ad_group_performance"):
        if t in tables:
            c = cols(t)
            name_col = next((x for x in ("ad_group_name", "adgroup_name",
                                         "ad_group") if x in c), None)
            date_col = next((x for x in ("report_date", "date")
                             if x in c), None)
            if name_col and date_col and "cost_micros" in c:
                src = ("AG", t, name_col, date_col, c)
                break
    if src is None:
        # fallback: keyword grain joined to instances carrying ad group
        kc = cols("keyword_instances") if "keyword_instances" in tables \
            else set()
        agcol = next((x for x in ("ad_group_name", "adgroup_name",
                                  "ad_group") if x in kc), None)
        if "keyword_performance_daily" in tables and agcol:
            c = cols("keyword_performance_daily")
            date_col = next(x for x in ("report_date", "date") if x in c)
            src = ("KPD", "keyword_performance_daily", agcol, date_col, c)
        else:
            like = sorted(t for t in tables if "group" in t.lower()
                          or "adgroup" in t.lower())
            die(f"no ad-group-grain source found; ad-group-ish tables: "
                f"{like}; keyword_instances cols: {sorted(kc)} — paste "
                f"this to the strategy thread.")
    kind, t, name_col, date_col, c = src
    filt = ""
    if "device" in c:
        filt += "AND device='ALL' "
    if "geo_state" in c:
        filt += "AND geo_state='ALL' "
    if "platform" in c:
        filt += "AND LOWER(platform)='google' "
    print(f"[SOURCE] {t} ({kind} grain) filters: {filt.strip() or 'none'}")

    g_max = con.execute(
        f"SELECT MAX({date_col}) FROM {t} WHERE 1=1 {filt}").fetchone()[0]
    d_max = dt.date.fromisoformat(g_max)
    start30 = (d_max - dt.timedelta(days=29)).isoformat()
    ph = ",".join("?" * len(camp))

    daily = defaultdict(float)
    by_camp = defaultdict(float)
    if kind == "AG":
        q = (f"SELECT {date_col} d, campaign_id cid, SUM(cost_micros)/1e6 co "
             f"FROM {t} WHERE campaign_id IN ({ph}) {filt}"
             f"AND {name_col} LIKE 'Home-Generic%' "
             f"AND {date_col} >= ? GROUP BY 1,2")
        rows = con.execute(q, list(camp) + [start30])
    else:
        q = (f"SELECT p.{date_col} d, ki.campaign_id cid, "
             f"SUM(p.cost_micros)/1e6 co "
             f"FROM {t} p JOIN keyword_instances ki ON ki.id=p.instance_id "
             f"WHERE ki.account_id=1 AND ki.campaign_id IN ({ph}) "
             + filt.replace("AND device", "AND p.device")
                   .replace("AND geo_state", "AND p.geo_state")
                   .replace("AND LOWER(platform)", "AND LOWER(p.platform)") +
             f"AND ki.{name_col} LIKE 'Home-Generic%' "
             f"AND p.{date_col} >= ? GROUP BY 1,2")
        rows = con.execute(q, list(camp) + [start30])
    for r in rows:
        daily[r["d"]] += r["co"] or 0
        by_camp[r["cid"]] += r["co"] or 0

    if not daily:
        die("zero generic-group spend rows in the last 30 days — check "
            "the ad-group name pattern and source grain.")

    print(f"\n[WINDOWS] data through {g_max}")
    for n in (30, 14, 7):
        s = (d_max - dt.timedelta(days=n - 1)).isoformat()
        tot = sum(v for d, v in daily.items() if d >= s)
        print(f"  last {n:>2}d: total ${tot:,.0f}  ->  "
              f"${tot / n:,.0f}/day")
    print(f"\n[BY CAMPAIGN] generic-group spend, last 30d "
          f"(all of this consolidates into the nationwide campaign):")
    for cid, v in sorted(by_camp.items(), key=lambda kv: -kv[1]):
        print(f"   ${v:>8,.0f}  {camp[cid][:64]}")
    print(f"\n[DAILY] last 14 days (watch the 10/1-10/2 surge-action "
          f"dates and any ramp since):")
    for i in range(13, -1, -1):
        d = (d_max - dt.timedelta(days=i)).isoformat()
        bar = "#" * int(daily.get(d, 0) / 25)
        print(f"   {d}  ${daily.get(d, 0):>7,.0f}  {bar}")


if __name__ == "__main__":
    main()
