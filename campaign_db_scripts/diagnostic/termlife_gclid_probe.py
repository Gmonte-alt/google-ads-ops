#!/usr/bin/env python
"""
termlife_gclid_probe.py -- READ-ONLY API probe for the GCLID table build
(acct 29, customer 1671155176). Verifies externally-sourced claims about
click_view before any schema is designed: field availability, the one-day
query constraint, the actual lookback cliff, volume/day, keyword linkage
coverage, and a one-day clicks reconciliation vs the DB.

    python termlife_gclid_probe.py
"""
import datetime as dt
import sqlite3

from dotenv import load_dotenv
load_dotenv()
from google.ads.googleads.client import GoogleAdsClient
from google.ads.googleads.errors import GoogleAdsException

CUSTOMER_ID = "1671155176"
DB = r"C:\Projects\google-ads-ops\db\paid_search.db"

client = GoogleAdsClient.load_from_storage()
ga = client.get_service("GoogleAdsService")


def day(offset):
    return (dt.date.today() - dt.timedelta(days=offset)).isoformat()


def try_query(gaql, label):
    try:
        rows = []
        for batch in ga.search_stream(customer_id=CUSTOMER_ID, query=gaql):
            rows.extend(batch.results)
        return rows, None
    except GoogleAdsException as e:
        msg = "; ".join(err.message for err in e.failure.errors)[:160]
        return None, msg


# Field tiers: try rich first, fall back until something works.
TIERS = [
    ("rich", "SELECT click_view.gclid, click_view.ad_group_ad, "
             "click_view.keyword, click_view.keyword_info.text, "
             "click_view.keyword_info.match_type, campaign.id, ad_group.id, "
             "segments.date, segments.device, segments.click_type, "
             "segments.slot FROM click_view WHERE segments.date = '{d}'"),
    ("mid",  "SELECT click_view.gclid, click_view.keyword, campaign.id, "
             "ad_group.id, segments.date, segments.device "
             "FROM click_view WHERE segments.date = '{d}'"),
    ("min",  "SELECT click_view.gclid, campaign.id, segments.date "
             "FROM click_view WHERE segments.date = '{d}'"),
]

# Pick the most recent likely-serving day (skip Sunday; data lags ~a day).
probe_day = 2
while dt.date.fromisoformat(day(probe_day)).weekday() == 6:
    probe_day += 1
d0 = day(probe_day)

print("1) field-tier resolution on %s:" % d0)
working_tier = working_gaql = None
rows0 = []
for name, tmpl in TIERS:
    rows, err = try_query(tmpl.format(d=d0), name)
    if err is None:
        print("   tier '%s': OK -- %s rows" % (name, format(len(rows), ",")))
        working_tier, working_gaql, rows0 = name, tmpl, rows
        break
    print("   tier '%s': REJECTED -- %s" % (name, err))
if working_tier is None:
    raise SystemExit("   all tiers rejected -- paste this output to the architect.")

print("\n2) one-day constraint check (range query, expected to FAIL):")
_, err = try_query(
    "SELECT click_view.gclid FROM click_view WHERE segments.date BETWEEN "
    "'%s' AND '%s'" % (day(probe_day + 1), d0), "range")
print("   range query:", ("ACCEPTED (claim wrong!)" if err is None else "rejected -- " + err))

print("\n3) lookback cliff (single-day queries at increasing age):")
cliff = None
for off in (30, 60, 85, 89, 91, 95, 120):
    rows, err = try_query(working_gaql.format(d=day(off)), str(off))
    status = ("%s rows" % format(len(rows), ",")) if err is None else "FAIL: " + err
    print("   T-%-4d %s  %s" % (off, day(off), status))
    if err is not None and cliff is None:
        cliff = off
print("   -> observed cliff: %s" % ("~T-%d" % cliff if cliff else "none hit through T-120"))

print("\n4) day-0 sample shape (tier '%s', first 3 rows):" % working_tier)
for r in rows0[:3]:
    cv = r.click_view
    print("   gclid=%s.. camp=%s kw=%s" % (cv.gclid[:24], r.campaign.id,
          getattr(cv, "keyword", "") or "(none)"))
n = len(rows0)
n_gclid = len({r.click_view.gclid for r in rows0})
n_kw = sum(1 for r in rows0 if getattr(r.click_view, "keyword", ""))
print("   rows %s | distinct gclids %s | rows with keyword link %s (%.0f%%)"
      % (format(n, ","), format(n_gclid, ","), format(n_kw, ","),
         100.0 * n_kw / n if n else 0))

print("\n5) reconciliation vs DB clicks for %s:" % d0)
c = sqlite3.connect("file:%s?mode=ro" % DB, uri=True)
kpd = c.execute(
    "SELECT SUM(p.clicks) FROM keyword_performance_daily p "
    "JOIN keyword_instances k ON k.id=p.instance_id "
    "WHERE k.account_id=29 AND p.device='ALL' AND p.report_date=?", (d0,)).fetchone()[0]
cpd = c.execute(
    "SELECT SUM(clicks) FROM campaign_performance_daily "
    "WHERE account_id=29 AND device='ALL' AND date=?", (d0,)).fetchone()[0]
c.close()
print("   click_view rows: %s | kpd clicks: %s | cpd clicks: %s"
      % (format(n, ","), kpd, cpd))
print("\nPaste this whole output into the GCLID build thread alongside the charter.")
