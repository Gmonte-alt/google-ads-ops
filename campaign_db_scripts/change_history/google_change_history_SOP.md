# Google Change-History Pipeline — Weekly SOP & Recovery Runbook

**Scope:** the Google Home change-history pipeline (`14`–`18` + `gads_change_common.py`) feeding the
shared `campaign_name_history` and `campaign_change_events` tables in `paid_search.db`.
**Owner:** Gilbert Montemayor · **Account:** Google Home, CID 888-650-2147 (`accounts.id=1`)
**Health-check counts (last known-good, 2026-07-06):** events `{microsoft:52, google:59}` · intervals `{microsoft:54, google:118}`

---

## 0. Cardinal rules — read first

These four rules were each bought with a real failure this cycle. They are not style preferences.

**Rule 1 — Resolve campaigns by immutable `campaign_id`, never by mutable name.**
Campaign names drift (renames, re-tiering); ids do not. The whole 42→59 recovery hinged on this. Concretely,
the tier shells were renamed and the labels *rhyme misleadingly*:

| campaign_id (truth) | name when the change was logged | name now |
|---|---|---|
| `23496334752` | HOME INSURANCE-**TIER TWO** | Tier**1**RPL80Plus… |
| `23530860942` | HOME INSURANCE-**TIER THREE** | Tier**2**RPL70Plus… |

A name-based guess maps "Tier2RPL70Plus" back to "TIER TWO" — **swapping Tier Two and Tier Three** and
silently misattributing every one of their tROAS/budget changes. Resolving by id from the snapshot got it
right. When in doubt, trust the id, distrust the name.

**Rule 2 — Snapshot before you restructure.**
Take a `campaigns_snapshot_pre_restructure_<YYYYMMDD>` (campaign_id + campaign_name, all accounts) **before
any rename, re-tier, or retirement.** That snapshot is the historical name→id map, and it has now been the
recovery key twice — Microsoft renames and the Google tier ids. `17`'s `--rename-snapshot` fallback consumes
it. No snapshot before a restructure = no clean way to attribute pre-rename history afterward.

**Rule 3 — Never let a script or harness touch the live DB by accident.**
The clobber that started this was an unsafe harness. Guards that must hold for anything touching this DB:
- **No default DB path.** Every pipeline script requires an explicit `--db` (they do: `required=True`). A
  script that *defaults* to the live DB is a footgun — never add one.
- **Validation/synthetic harnesses write to a throwaway temp DB only.** `make_synthetic.py` /
  `validate_api_layer.py` hardcode `synthetic.db` and never reference `paid_search.db` or any `C:\` path.
  Keep it that way; a harness must never accept or default to the live path.
- **Backup before mutate; scoped deletes; no DROP.** `16`/`17` snapshot the table (`_backup_<ts>`) before a
  delete, delete only `WHERE platform='google' AND account_id=?`, and no script contains `DROP TABLE`. If
  tables ever vanish, it came from *outside* this pipeline.
- **Recommended guard (not yet implemented):** a small-DB tripwire — abort if the target `--db` is
  implausibly small (the live DB is ~9.5 GB), catching a wrong/truncated file before any write.

**Rule 4 — Back up `paid_search.db` to the T9 after every `--apply` run.**
The rebuilt state lives in exactly one place until you copy it. That single-copy window is what the clobber
exploited. Backup is part of the run, not an afterthought.

---

## 1. Environment (every session)

Run from the **project root** so `.env` loads (sets `GOOGLE_ADS_CONFIGURATION_FILE_PATH` →
`C:\ProgramData\google-ads-credentials\google-ads.yaml`, like syncs `03`/`05`).

```bat
conda activate paid-search
cd C:\Projects\google-ads-ops

set DB=C:\Projects\google-ads-ops\db\paid_search.db
set SC=C:\Projects\google-ads-ops\campaign_db_scripts
set YTD=C:\Projects\google-ads-ops\campaign_db_scripts\input\google-changehist\google-home-changehist-grid-20260629.csv
```
Every script is **dry-run by default**; mutations require `--apply`. `--cid` defaults to the Home CID. Read
the dry-run, then apply.

---

## 2. Weekly run (normal operation)

The going-forward feed is the API layer — **no migration, no CSV, in a normal week.**

```bat
python %SC%\18_pull_google_change_events_api.py --db %DB%            :: dry-run: reads [preview] by resource type
python %SC%\18_pull_google_change_events_api.py --db %DB% --apply
python %SC%\16_derive_campaign_name_history_google.py --db %DB%
python %SC%\16_derive_campaign_name_history_google.py --db %DB% --apply
python %SC%\17_derive_campaign_change_events_google.py --db %DB%
python %SC%\17_derive_campaign_change_events_google.py --db %DB% --apply
```
After a **rename week**, look in `18`'s preview for `CAMPAIGN … campaign.name` rows — those drive `16`'s
interval splits and are the main thing to confirm landed.

**Verify (health check, after every apply):**
```bat
python -c "import sqlite3;c=sqlite3.connect(r'%DB%');print('events',dict(c.execute(\"select platform,count(*) from campaign_change_events group by platform\").fetchall()));print('intervals',dict(c.execute(\"select platform,count(*) from campaign_name_history group by platform\").fetchall()))"
```
Microsoft must stay `52 / 54` (the derives are Google-scoped). Google grows as changes accumulate. **Then
back up to the T9 (Rule 4).**

---

## 3. Periodic backstop — the UI grid (monthly, or after portfolio/geo changes)

`change_event` does **not** reliably capture **portfolio (Shared-Library) tROAS** changes or **geo/state-tier
moves** — the UI export is the only valued source for those. Run `15` with a fresh export to fold them in;
`17` is dual-source and dedupes the overlap (API authoritative).

```bat
:: export a fresh Change History CSV from the Google Ads UI, then:
python %SC%\15_load_google_change_csv.py --db %DB% --csv PATH\TO\fresh-export.csv --apply
python %SC%\16_derive_campaign_name_history_google.py --db %DB% --apply
python %SC%\17_derive_campaign_change_events_google.py --db %DB% --apply
```
Do this **right after any portfolio-strategy tROAS change** — the API will miss it (Phase 2 will automate a
snapshot-diff; until then the UI grid is the mechanism).

---

## 4. The two-source model (why renames only break one path)

- **CSV ledger (`google_change_csv_raw`)** — the historical seed, **name-keyed** (`campaign_col`, no id).
  Resolves a change to a campaign by matching the CSV name to a campaign. **This is the path renames break:**
  a renamed campaign's old CSV name no longer matches current campaigns.
- **API ledger (`google_change_event_raw`)** — the going-forward feed, **id-native** (`resource_name` embeds
  the campaign_id). **Immune to renames** — ids don't drift.

So renamed/retired campaigns only drop out of the **CSV** path. `17` bridges this with the
**`--rename-snapshot` fallback**: any CSV name not in current campaigns is looked up in the pre-restructure
snapshot by immutable id (tagged `resolved_via='name_match_snapshot'`, auditable). This is Rule 1 + Rule 2
operationalized — and it's the fix that recovered the 17 tier statements.

---

## 5. Recovery / rebuild (tables missing, clobbered, or renamed-campaign drops)

**Detection:**
```bat
python -c "import sqlite3,os,datetime;p=r'%DB%';print('db modified:',datetime.datetime.fromtimestamp(os.path.getmtime(p)));c=sqlite3.connect(p);tabs=[r[0] for r in c.execute(\"select name from sqlite_master where type='table'\")];[print(t,'exists:',t in tabs) for t in ('google_change_csv_raw','google_change_event_raw')];[print(t,dict(c.execute('select platform,count(*) from '+t+' group by platform').fetchall())) for t in ('campaign_name_history','campaign_change_events') if t in tabs]"
```
Missing raw ledgers, or the derived tables showing **only** `microsoft` rows → rebuild below. **The pipeline
cannot cause this** (Rule 3: no DROP, scoped deletes, backup-first); a missing table means a restore/revert or
an external process. Confirm via the `db modified` timestamp before rebuilding.

**Rebuild (Google-scoped, additive — Microsoft untouched):**
```bat
python %SC%\14_migrate_google_change_history.py --db %DB% --apply          :: recreate both google_change_*_raw ledgers
python %SC%\15_load_google_change_csv.py --db %DB% --csv %YTD% --apply     :: reload the 566-row YTD seed
python %SC%\16_derive_campaign_name_history_google.py --db %DB% --apply    :: -> google: 118 intervals
python %SC%\17_derive_campaign_change_events_google.py --db %DB% --apply   :: -> google: 59 events (snapshot fallback resolves renamed shells)
```
Then the §2 verify — target `events {…:59}`, `intervals {…:118}` — and back up (Rule 4).

**Renamed-campaign scenario (the 42→59 case):** if `17` reports `unresolved names` for campaigns you *know*
exist under new names (e.g. TIER TWO/THREE), that's rename drift, not data loss — the statements are still in
`google_change_csv_raw`. `17`'s `--rename-snapshot` fallback resolves them by id from
`campaigns_snapshot_pre_restructure_20260603`. Confirm with:
```bat
python -c "import sqlite3;c=sqlite3.connect(r'%DB%');print('via snapshot',c.execute(\"select count(*) from campaign_change_events where resolved_via='name_match_snapshot'\").fetchone()[0])"
```
Nothing is ever permanently lost — the DB is *derived*; source of truth is the CSV/UI exports on disk.

---

## 6. Troubleshooting

**Tables/rows missing, or only `microsoft` rows** → §5. Verify cause (restore vs. external drop) via file
mtime; the pipeline never drops tables.

**`17` drops statements / `unresolved names` for renamed campaigns** → rename drift. Ensure `17` runs with
`--rename-snapshot` pointed at a snapshot that predates the rename and contains the old names. Resolve by **id
from the snapshot**, never by hand-mapping names (Rule 1 — a name-based mapping swapped Tier Two/Three).

**`no such column` on a shared table after a restore** → schema divergence. The restored `campaign_rename_overrides`
uses `old_name/new_name/rename_date/note`; `16` expects `historical_name/valid_to`. `16` now guards this
(`PRAGMA table_info` check) and skips the overrides source with a `[WARN]` — harmless while empty. Reconcile
the shared schema when convenient (known-open item).

**`START_DATE_TOO_OLD` from `18`** → the API rejects a start older than 30 days; date-only start = midnight, so
exactly-30 is already over. `18` requests 28 days (`MAX_LOOKBACK_DAYS`). If it reappears, confirm that constant
wasn't raised.

**`FileNotFoundError` on the yaml / config not loading** → not in project root, or `.env` unread. `cd` to the
project root first. Yaml at `C:\ProgramData\google-ads-credentials\google-ads.yaml` via `.env`; `18` calls
`load_dotenv()`. `--google-ads-yaml PATH` overrides only for different credentials.

**A portfolio tROAS change isn't in `campaign_change_events`** → known API gap (Shared-Library strategies).
Run the §3 UI-grid backstop. Phase 2 (snapshot-diff) will automate it.

**Geo/state-tier moves aren't tracked** → by design (Phase 2). Only tROAS, budget, names are tracked.

**Microsoft counts changed after a Google run** → should never happen (Google-scoped deletes). If they did,
restore from the `_backup_<ts>` the derive created, stop, and investigate.

---

## 7. What the pipeline captures

| Change type | Weekly API (`18`) | UI backstop (`15`) | Notes |
|---|---|---|---|
| Campaign-level tROAS (`campaign.target_roas`) | ✅ | ✅ | reliable via API |
| Daily budget (`campaign_budget.amount_micros`) | ✅ | ✅ | stored in dollars |
| Campaign name / rename (`campaign.name`) | ✅ | ✅ | drives `16` SCD2 splits |
| **Portfolio tROAS** (`bidding_strategy.target_roas`) | ❌ | ✅ | API gap → UI backstop (Phase 2: snapshot-diff) |
| **Geo / state-tier moves** (location criteria) | ❌ | ❌ | Phase 2 (`CAMPAIGN_CRITERION`) |
| Ad-group / keyword / status | ❌ (skipped) | ❌ (skipped) | out of scope by design |

---

## 8. Known-open items (deferred, not hidden)

1. **`16` historical-name intervals for renamed shells** — `campaign_name_history` currently shows only the
   *current* names for the two renamed tier shells (missing the old TIER TWO/THREE interval). Events are whole;
   only historical-name joins are thin. Fix: seed the rename interval from the snapshot with a date-aligned
   boundary.
2. **`campaign_rename_overrides` schema reconciliation** — shared table's live schema
   (`old_name/new_name/rename_date/note`) ≠ what `16` expects (`historical_name/valid_to`). Guarded for now.
3. **Phase 2 portfolio-tROAS snapshot-diff** — automate `bidding_strategy` config snapshots + daily diff, since
   `change_event` structurally omits portfolio changes. Also Phase 2: geo/`CAMPAIGN_CRITERION` tracking.

---

## Appendix A — Incident history (why these rules exist)

Written down while fresh, as rationale — not blame.

- **The clobber (last week):** a *separate* analysis thread dropped `paid_search.db` tables. The change-history
  scripts cannot do this (no DROP, scoped deletes) — the risk lives in *other* code touching the DB, which is
  why Rule 3's guards apply to anything that opens this file.
- **The restore:** the T9 backup loaded predated both the change-history work *and* the June tier rename. So the
  rebuild had to recreate the Google tables **and** contend with renamed campaigns.
- **The name-drift discovery:** `17` dropped 17 statements for TIER TWO/THREE — not lost, just unattributable,
  because those shells had been renamed and the CSV carried the *old* names. A proposed name-based id mapping
  turned out **swapped** (would have misattributed Tier Two↔Tier Three). The `campaigns_snapshot_pre_restructure_20260603`
  snapshot resolved it by immutable id → 59 events, no misattribution. Hence Rules 1 and 2.
- **The lesson:** ids are truth, names drift, snapshot before you restructure, and back up the moment you have a
  good state. Every rule above traces to a specific thing that went wrong when it wasn't followed.

---

## Weekly checklist (tl;dr)

1. `cd C:\Projects\google-ads-ops`, set env vars (§1).
2. `18` dry-run → read `[preview]` → `18 --apply`.
3. `16` dry-run → apply. `17` dry-run → apply.
4. Verify: Microsoft stays `52 / 54`; Google grows. (§2)
5. **Monthly / after portfolio or geo changes:** UI-grid backstop (§3).
6. **Back up `paid_search.db` to the T9** (Rule 4).
