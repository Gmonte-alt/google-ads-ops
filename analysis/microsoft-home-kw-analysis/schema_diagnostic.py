"""Read-only schema diagnostic. Answers the three open questions before reworking the analysis:
   (1) where does the network (Audience vs search) split live, and at what grain?
   (2) is keyword_performance_daily also split by geo_state in a way that would double-count?
   (3) does ad_groups carry a status column for the paused-ad-group exclusion?
Safe: opens read-only, only SELECT/PRAGMA."""
import sqlite3, sys

DB = r"C:\Projects\google-ads-ops\db\paid_search.db"
con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
q = con.execute

tables = sorted(r[0] for r in q("SELECT name FROM sqlite_master WHERE type='table'"))
print("=== TABLES ===")
print(tables)

print("\n=== TABLES CARRYING A 'network' COLUMN (any grain) ===")
for t in tables:
    try:
        cols = [r[1] for r in q(f"PRAGMA table_info({t})")]
    except sqlite3.Error:
        continue
    if any("network" in c.lower() for c in cols):
        print(f"{t}: {cols}")

print("\n=== keyword_performance_daily grain profile (account 2) ===")
def acct2_join():
    return ("keyword_performance_daily kpd JOIN keyword_instances ki "
            "ON ki.id = kpd.instance_id WHERE ki.account_id = 2")
try:
    print("distinct device:", q("SELECT DISTINCT device FROM keyword_performance_daily").fetchall())
    print("distinct geo_state (first 25):",
          q("SELECT DISTINCT geo_state FROM keyword_performance_daily LIMIT 25").fetchall())
    print("geo_state NULLs present?:",
          q("SELECT COUNT(*) FROM keyword_performance_daily WHERE geo_state IS NULL").fetchone()[0], "rows")
    # Is a device='ALL' slice further split by geo_state? (rows per instance/date, distinct geos)
    print("device='ALL' slice -> rows per (instance,date) and distinct geo_states:")
    for r in q("""SELECT kpd.instance_id, kpd.report_date, COUNT(*) rows_, COUNT(DISTINCT kpd.geo_state) geos
                  FROM keyword_performance_daily kpd JOIN keyword_instances ki ON ki.id = kpd.instance_id
                  WHERE ki.account_id = 2 AND kpd.device='ALL'
                  GROUP BY kpd.instance_id, kpd.report_date ORDER BY rows_ DESC LIMIT 5"""):
        print("  ", r)
    # Is there a geo rollup row (geo_state = 'ALL' / '' / 0) coexisting with per-state rows?
    print("geo rollup sentinels present?:",
          q("""SELECT geo_state, COUNT(*) FROM keyword_performance_daily
               WHERE geo_state IN ('ALL','All','','0','TOTAL') GROUP BY geo_state""").fetchall())
except sqlite3.Error as e:
    print("kpd profile error:", e)

print("\n=== ad_groups (status for paused exclusion) ===")
try:
    agc = [r[1] for r in q("PRAGMA table_info(ad_groups)")]
    print("ad_groups columns:", agc if agc else "NO ad_groups TABLE")
    if agc and "status" in agc:
        print("distinct ad_groups.status:", q("SELECT DISTINCT status FROM ad_groups").fetchall())
except sqlite3.Error as e:
    print("ad_groups error:", e)

print("\n=== keyword_instances status / match_type values (account 2) ===")
try:
    print("status:", q("SELECT DISTINCT status FROM keyword_instances WHERE account_id=2").fetchall())
    print("match_type:", q("SELECT DISTINCT match_type FROM keyword_instances WHERE account_id=2").fetchall())
    print("distinct ad_group_name sample:",
          q("SELECT DISTINCT ad_group_name FROM keyword_instances WHERE account_id=2 LIMIT 15").fetchall())
except sqlite3.Error as e:
    print("ki error:", e)

# If a network table exists at campaign grain, show what keys it exposes for a possible carrier map
print("\n=== IF a *_network_* table exists: its columns + sample grain keys ===")
for t in tables:
    if "network" in t.lower():
        try:
            cols = [r[1] for r in q(f"PRAGMA table_info({t})")]
            print(f"{t}: {cols}")
        except sqlite3.Error as e:
            print(t, "err", e)

con.close()
