"""Validate the API change-event layer end to end (synthetic)."""
import subprocess, sqlite3, json, sys, datetime
import campaign_db_scripts.change_history.gads_change_common as C
import pull_google_change_events_api as P

DB = "synthetic.db"
def run(cmd):
    r = subprocess.run([sys.executable] + cmd, capture_output=True, text=True)
    return r.stdout + r.stderr

# ---- build fixture + base pipeline ----
print(run(["make_synthetic.py"]).strip().splitlines()[-1])
run(["migrate_google_change_history.py", "--db", DB, "--cid", "888-650-2147", "--apply"])
run(["load_google_change_csv.py", "--db", DB, "--csv", "synthetic_change_history.csv", "--cid", "888-650-2147", "--apply"])

# ---- seed synthetic API ledger rows (what 18 would have pulled) ----
c = sqlite3.connect(DB)
def cj(d): return json.dumps(d)
api_rows = [
 (1,"ce1","2026-06-20 10:00:00","CAMPAIGN","UPDATE","customers/1/campaigns/g_fl","u","WEB",
   cj(["campaign.target_roas.target_roas"]), cj({"campaign.target_roas.target_roas":[1.12,1.10]})),
 (1,"ce2","2026-06-15 09:00:00","CAMPAIGN","UPDATE","customers/1/campaigns/g_tx","u","WEB",
   cj(["campaign.name"]), cj({"campaign.name":["HOME INSURANCE-TEXAS-NB-SEARCH-ALL-USA-STE",
                              "HOME INSURANCE-TIER THREE-NB-SEARCH-ALL-USA-STE"]})),
 (1,"ce3","2026-06-21 11:00:00","CAMPAIGN_BUDGET","UPDATE","customers/1/campaignBudgets/bud_ca","u","WEB",
   cj(["campaign_budget.amount_micros"]), cj({"campaign_budget.amount_micros":[1500000000,1800000000]})),
 (1,"ce4","2026-06-22 12:00:00","BIDDING_STRATEGY","UPDATE","customers/1/biddingStrategies/bs_tier12","u","WEB",
   cj(["bidding_strategy.target_roas.target_roas"]), cj({"bidding_strategy.target_roas.target_roas":[1.10,1.13]})),
 (1,"ce5","2026-06-23 13:00:00","AD_GROUP_CRITERION","UPDATE","customers/1/adGroupCriteria/111~222","u","WEB",
   cj(["ad_group_criterion.cpc_bid_micros"]), cj({"ad_group_criterion.cpc_bid_micros":[1000000,2000000]})),
 (1,"ce6","2026-01-15 09:01:05","CAMPAIGN","UPDATE","customers/1/campaigns/g_fl","u","WEB",
   cj(["campaign.target_roas.target_roas"]), cj({"campaign.target_roas.target_roas":[1.15,1.12]})),  # dup of CSV
]
c.executemany("INSERT INTO google_change_event_raw (account_id,resource_name,change_datetime,"
  "change_resource_type,resource_operation,change_resource_name,user_email,client_type,"
  "changed_fields,changes_json,loaded_at) VALUES (?,?,?,?,?,?,?,?,?,?,datetime('now'))", api_rows)
c.commit(); c.close()

# ---- run rename-aware 16 + dual-source 17 ----
print("\n===== 16 (rename-aware) =====")
print(run(["derive_campaign_name_history_google.py","--db",DB,"--cid","888-650-2147","--apply"]))
print("===== 17 (dual-source) dry-run =====")
print(run(["derive_campaign_change_events_google.py","--db",DB,"--cid","888-650-2147"]))
run(["derive_campaign_change_events_google.py","--db",DB,"--cid","888-650-2147","--apply"])

# ---- verify DB state ----
c = sqlite3.connect(DB); c.row_factory = sqlite3.Row
print("===== verification =====")
print("events by platform:", dict(c.execute("SELECT platform,COUNT(*) FROM campaign_change_events GROUP BY platform").fetchall()))
print("events by source/resolved_via (google):")
for r in c.execute("SELECT source,resolved_via,COUNT(*) n FROM campaign_change_events WHERE platform='google' GROUP BY source,resolved_via ORDER BY n DESC"):
    print("   %-12s %-26s %d" % (r["source"], r["resolved_via"], r["n"]))
print("portfolio fan-out (same day/values across 2 campaigns):")
for r in c.execute("SELECT campaign_id,change_date,old_value,new_value,resolved_via FROM campaign_change_events WHERE resolved_via='portfolio_strategy_fanout' ORDER BY campaign_id"):
    print("   ", dict(r))
print("name_history by platform:", dict(c.execute("SELECT platform,COUNT(*) FROM campaign_name_history GROUP BY platform").fetchall()))
print("g_tx intervals (renamed TEXAS->TIER THREE):")
for r in c.execute("SELECT campaign_name,valid_from,valid_to,source FROM campaign_name_history WHERE campaign_id='g_tx' ORDER BY valid_from"):
    print("   %-46s %s -> %-10s [%s]" % (r["campaign_name"], r["valid_from"], r["valid_to"] or "open", r["source"]))
c.close()

# ---- unit tests for the pure functions ----
print("\n===== unit tests =====")
def ok(name, cond): print(("PASS " if cond else "FAIL ")+name); assert cond, name

r = C.normalize_api_record("CAMPAIGN","customers/1/campaigns/g_fl",{"campaign.target_roas.target_roas":(1.12,1.10)})
ok("campaign tROAS -> event", r==[{"kind":"event","change_type":"TARGET_ROAS","scope":"campaign","scope_id":"g_fl","old":1.12,"new":1.10}])
r = C.normalize_api_record("CAMPAIGN","customers/1/campaigns/g_tx",{"campaign.name":("OLD","NEW")})
ok("campaign.name -> naming", r==[{"kind":"naming","campaign_id":"g_tx","old":"OLD","new":"NEW"}])
r = C.normalize_api_record("CAMPAIGN_BUDGET","customers/1/campaignBudgets/b",{"campaign_budget.amount_micros":(1500000000,1800000000)})
ok("budget micros -> dollars", r[0]["old"]==1500.0 and r[0]["new"]==1800.0 and r[0]["scope"]=="budget")
ok("ad-group-criterion not tracked", C.normalize_api_record("AD_GROUP_CRITERION","x",{"ad_group_criterion.cpc_bid_micros":(1,2)})==[])
ok("unknown resource type tolerated", C.normalize_api_record("999","x",{"foo":(1,2)})==[])

iv,w = C.reconstruct_name_intervals("TIER THREE","2026-01-02",[("2026-06-15","TEXAS","TIER THREE","api_rename")])
ok("rename -> 2 intervals", len(iv)==2 and iv[0]["valid_to"]=="2026-06-14" and iv[1]["valid_from"]=="2026-06-15" and iv[1]["valid_to"] is None and w is None)
iv,w = C.reconstruct_name_intervals("CURRENT","2026-01-01",[])
ok("no rename -> baseline open interval", len(iv)==1 and iv[0]["valid_to"] is None and iv[0]["source"]=="campaigns_seed")
iv,w = C.reconstruct_name_intervals("MISMATCH","2026-01-01",[("2026-03-01","A","B","api_rename")])
ok("uncaptured-rename warning", w is not None)
ok("dedup key rounds", C.event_dedup_key("c","2026-01-01","TARGET_ROAS",1.150000001,1.12)==C.event_dedup_key("c","2026-01-01","TARGET_ROAS",1.15,1.12))

# 18 extraction with a mock event + fake get_nested_attr
class _FM: paths=["campaign.target_roas.target_roas"]
class _Op:
    def __init__(s,n): s.name=n
class _Ev:
    resource_name="customers/1/changeEvents/z"; change_date_time="2026-06-20 10:00:00"
    change_resource_name="customers/1/campaigns/g_fl"; user_email="u"
    change_resource_type=_Op("CAMPAIGN"); resource_change_operation=_Op("UPDATE")
    client_type=_Op("GOOGLE_ADS_WEB_CLIENT"); changed_fields=_FM()
    old_resource="OLDRES"; new_resource="NEWRES"
def fake_getter(res, path): return {"OLDRES":1.12,"NEWRES":1.10}[res]
rec = P.extract_changes_from_event(_Ev(), fake_getter)
ok("18 extract: resource_type kept", rec["change_resource_type"]=="CAMPAIGN")
ok("18 extract: changed path old/new", rec["changes_json"]["campaign.target_roas.target_roas"]==(1.12,1.10))

# gap detection
c = sqlite3.connect(DB); c.row_factory=sqlite3.Row
today = datetime.date(2026,7,30)  # far future to force the gap on the seeded June rows
s,e,warn = P.compute_window(c, 1, today)
ok("gap detection warns when newest event > 30d old", warn is not None and "UNRECOVERABLE" in warn)
today2 = datetime.date(2026,6,25)  # within 30d of seeded events
s2,e2,warn2 = P.compute_window(c, 1, today2)
ok("no gap warning when recent", warn2 is None)
c.close()

import os
for f in (DB,"synthetic_change_history.csv"):
    if os.path.exists(f): os.remove(f)
print("\nALL CHECKS DONE")
