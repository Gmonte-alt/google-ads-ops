"""Build synthetic fixtures to validate the Google change-history pipeline."""
import csv, sqlite3, os

DB = "synthetic.db"
CSVF = "synthetic_change_history.csv"
NB = "\u202f"  # narrow no-break space, as in the real export

if os.path.exists(DB): os.remove(DB)
c = sqlite3.connect(DB)
c.executescript("""
CREATE TABLE accounts (id INTEGER PRIMARY KEY, account_name TEXT NOT NULL,
  platform_account_id TEXT NOT NULL, platform TEXT NOT NULL DEFAULT 'google',
  mcc_id TEXT, business_unit_id INTEGER);
CREATE TABLE campaigns (campaign_id TEXT PRIMARY KEY, account_id INTEGER NOT NULL,
  campaign_name TEXT NOT NULL, status TEXT NOT NULL, target_roas REAL,
  daily_budget_micros INTEGER, first_seen_date TEXT, last_synced_at TEXT NOT NULL);
-- a pre-existing Microsoft row in the shared tables, to prove Google derive doesn't touch it
-- shared tables: REPLICATE THE LIVE (MS-built) NOT NULL constraints exactly,
-- so the fixture reproduces real constraint behavior (valid_from / new_value /
-- convention_version are NOT NULL on the live DB; valid_to / old_value are not).
CREATE TABLE campaign_name_history (id INTEGER PRIMARY KEY, platform TEXT NOT NULL,
  account_id INTEGER NOT NULL, campaign_id TEXT NOT NULL, campaign_name TEXT NOT NULL,
  valid_from TEXT NOT NULL, valid_to TEXT, source TEXT NOT NULL, convention_version TEXT NOT NULL,
  UNIQUE(platform, campaign_id, valid_from));
CREATE TABLE campaign_change_events (id INTEGER PRIMARY KEY, platform TEXT NOT NULL,
  account_id INTEGER NOT NULL, campaign_id TEXT NOT NULL, change_date TEXT NOT NULL,
  change_type TEXT NOT NULL, old_value REAL, new_value REAL NOT NULL, raw_event_count INTEGER NOT NULL,
  resolved_via TEXT NOT NULL, source TEXT NOT NULL, convention_version TEXT NOT NULL, derived_at TEXT NOT NULL);
""")
# account: platform_account_id stored UNDASHED, to prove dashed-input variant matching works
c.execute("INSERT INTO accounts (account_name, platform_account_id, platform) VALUES ('Home Insurance','8886502147','google')")
gid = c.execute("SELECT id FROM accounts").fetchone()[0]
ms_acct = 2
camps = [
 ("g_fl","HOME INSURANCE-FLORIDA-NB-SEARCH-ALL-USA-STE",1.12,"2026-01-02"),
 ("g_t1","HOME INSURANCE-TIER ONE-NB-SEARCH-ALL-USA-STE",1.10,"2026-01-03"),
 ("g_t2","HOME INSURANCE-TIER TWO-NB-SEARCH-ALL-USA-STE",1.15,"2026-01-03"),
 ("g_ca","HOME INSURANCE-CALIFORNIA-NB-SEARCH-ALL-USA-STE",1.20,"2026-01-02"),
 ("g_tx","HOME INSURANCE-TEXAS-NB-SEARCH-ALL-USA-STE",1.00,"2026-01-02"),
 ("g_ut","HOME INSURANCE-UTAH-NB-SEARCH-ALL-USA-STE",1.05,None),  # null first_seen -> floor guard
]
for cid,name,roas,fsd in camps:
    c.execute("INSERT INTO campaigns VALUES (?,?,?,?,?,?,?,?)",
              (cid,gid,name,"ENABLED",roas,None,fsd,"2026-06-28"))
# pre-existing MS rows in shared tables (must survive the Google derive)
c.execute("INSERT INTO campaign_name_history (platform,account_id,campaign_id,campaign_name,valid_from,valid_to,source,convention_version) "
          "VALUES ('microsoft',?,'531494261','...Tier2RPL65Plus...','2026-06-02',NULL,'override','v1-names-2026-06')",(ms_acct,))
c.execute("INSERT INTO campaign_change_events (platform,account_id,campaign_id,change_date,change_type,old_value,new_value,raw_event_count,resolved_via,source,convention_version,derived_at) "
          "VALUES ('microsoft',?,'531494261','2026-06-02','TARGET_CPA',45.0,52.0,1,'name_match_current','ms_grid','v1-tcpa-budget-2026-06','2026-06-26 00:00:00')",(ms_acct,))
c.commit(); c.close()

def dt(s):  # 'Jan 07, 2026, 10:15:03 AM' with U+202F before AM/PM
    return s.replace(" AM"," "+chr(0x202f)+"AM").replace(" PM"," "+chr(0x202f)+"PM")

FL="HOME INSURANCE-FLORIDA-NB-SEARCH-ALL-USA-STE"
T1="HOME INSURANCE-TIER ONE-NB-SEARCH-ALL-USA-STE"
T2="HOME INSURANCE-TIER TWO-NB-SEARCH-ALL-USA-STE"
CA="HOME INSURANCE-CALIFORNIA-NB-SEARCH-ALL-USA-STE"

rows = [
 # date, user, campaign, ad_group, changes
 (dt("Jan 15, 2026, 9:01:05 AM"),"g@q.com",FL,"", "Campaign changed\nTarget ROAS changed from 115% to 112%"),          # INCLUDE roas 1.15->1.12
 (dt("Jan 16, 2026, 9:02:00 AM"),"g@q.com",T2,"", '"Home Insurance - Tier 2 - Combined": Target ROAS changed from 1.12 to 1.15'),  # INCLUDE roas 1.12->1.15
 (dt("Jan 10, 2026, 8:00:00 AM"),"g@q.com",T1,"", 'Campaign changed\nCampaign moved from bid strategy named "Home Tier B" to "Home Insurance - Tier 1 - Combined"\n"Home Insurance - Tier 1 - Combined": Target ROAS portfolio strategy created\nTarget ROAS: 1.1'),  # INCLUDE anchor NULL->1.1
 (dt("Jan 18, 2026, 1:00:00 PM"),"g@q.com",T2,"Home-NonGeneric-Liberty Mutual","1 ad group target ROAS increased\nTarget ROAS changed from 130% to 150%"),  # EXCLUDE ad-group grain
 (dt("Jan 19, 2026, 2:00:00 PM"),"g@q.com",T1,"", "Label added\nLabel \"watch\" added\nTarget ROAS is 110%"),            # EXCLUDE state snapshot
 (dt("Jan 20, 2026, 3:00:00 PM"),"g@q.com",FL,"", FL+": Budget amount changed from $1,900.00 to $2,200.00\n1 budget amount increased"),  # INCLUDE budget 1900->2200
 # same-day non-monotonic walk-up on T1 2026-01-07 (3 rows) -> collapse 500->300 raw x3
 (dt("Jan 07, 2026, 10:01:00 AM"),"g@q.com",T1,"", T1+": Budget amount changed from $500.00 to $333.00"),
 (dt("Jan 07, 2026, 10:05:00 AM"),"g@q.com",T1,"", T1+": Budget amount changed from $333.00 to $250.00"),
 (dt("Jan 07, 2026, 10:09:00 AM"),"g@q.com",T1,"", T1+": Budget amount changed from $250.00 to $300.00"),
 # same-day walk-up on CA 2026-01-12 (2 rows) -> collapse 1000->1500 raw x2
 (dt("Jan 12, 2026, 11:00:00 AM"),"g@q.com",CA,"", CA+": Budget amount changed from $1,000.00 to $1,250.00"),
 (dt("Jan 12, 2026, 11:30:00 AM"),"g@q.com",CA,"", CA+": Budget amount changed from $1,250.00 to $1,500.00"),
 # budget RENAME (not an amount change) -> EXCLUDE
 (dt("Jan 22, 2026, 4:00:00 PM"),"g@q.com",T2,"", 'Budget changed\nBudget name changed from "OLD BUDGET" to "NEW BUDGET"'),
 # creative noise -> EXCLUDE
 (dt("Jan 23, 2026, 5:00:00 PM"),"g@q.com",FL,"Home-Generic-Core","1 responsive search ad created\nHeadline is \"Compare Home Rates (Asset ID: 123)\""),
 # unresolved campaign name -> WARN, not written
 (dt("Jan 24, 2026, 6:00:00 PM"),"g@q.com","HOME INSURANCE-NEVADA-NB-SEARCH-ALL-USA-STE","", "Campaign changed\nTarget ROAS changed from 120% to 118%"),
]

with open(CSVF,"w",encoding="utf-8",newline="") as fh:
    fh.write("Change history report\n")
    fh.write('"January 1, 2026 - June 28, 2026"\n')
    w=csv.writer(fh)
    w.writerow(["Date & time","User","Campaign","Ad group","Changes"])
    for r in rows: w.writerow(r)

print("built", DB, "and", CSVF, "| google account id =", gid)
print("U+202F present in csv:", "\u202f".encode() in open(CSVF,'rb').read())
