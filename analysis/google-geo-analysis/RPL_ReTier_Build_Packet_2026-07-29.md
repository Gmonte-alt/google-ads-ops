# Google Home — RPL k=4 Re-Tier: Ads Editor Build Packet
Generated 2026-07-29 from `ads_editor_*_2026-07-29_pre-cutover.csv` (fresh, incl. paused) diffed
against the frozen intended map (`google_geo_model_validation_rpl_C2..._k_4_excludeTX-CA-FL.xlsx`,
State Assignments + Below Threshold placements). Membership ledger:
`commitments\google_home_rpl_k4_committed_2026-07-29.yaml`.

**Scope: 26 state placements across 4 shells, single cutover. CA/TX/FL untouched by this packet.**

---
## Pre-flight findings (resolve BEFORE building)

**F1 — BID STRATEGY MISCONFIGURATION (live) — RESOLVED VALUES.** The Tier1RPL80Plus shell
(23496334752) is attached to portfolio **"Home Insurance - Tier 2 - Combined" @ 115%** — a leftover
from the July shell repurposing (this campaign was the old Tier Two shell; it kept its old
portfolio when renamed). Tier2/Tier3 shells run campaign-level 117%. ACTION at cutover: detach the
T1 shell to **campaign-level tROAS 115%** (same-number detach — no effective target change). Note
for the target owner: the premium shell has therefore been running LOOSER (115%) than T2/T3 (117%);
under the draft floors (T1 = 1.25) T1 is the only shell whose floor REQUIRES tightening steps
post-freeze — with Massachusetts inside, which is exception-watch item #1.

**F2 — Legacy shell portfolio — RESOLVED VALUE.** zz-RETIRED shell (23428389081) is attached to
portfolio **"Home Insurance - Tier 1 - Combined" @ 117%**. Milder than feared (117% = current
T2/T3 level, not a premium-era number), but still wrong for the T4 cohort at launch: the reset
sequence detaches FIRST and sets the ratified T4 launch target before enabling.

**F3 — Resolved / confirmed.** New Mexico dual-targeting: RESOLVED (single enabled campaign).
New Hampshire is live in the T2 shell and lands in the intended map via below-threshold placement.
Current live targets for reference: CA 117% / TX 112% / FL 112% / T2 117% / T3 117%.

---
## Change set (by campaign_id — names are being changed in this same build)

### Shell 23496334752 → becomes TIER ONE (currently "Tier1RPL80Plus")
- REMOVE location targets: Alabama, Delaware, Ohio, Oregon, Utah, Wisconsin
- ADD location targets: Illinois, Kansas, Massachusetts, New Hampshire, New Jersey, New Mexico
- Bid strategy: detach from "Tier 2 - Combined" portfolio → campaign-level tROAS at recorded
  effective target (F1). Launch target = current effective (floors come later, stepped).

### Shell 23530860942 → becomes TIER TWO (currently "Tier2RPL70Plus")
- REMOVE: Illinois, Kansas, Maryland, New Hampshire, New Jersey, New Mexico, Oklahoma, Pennsylvania
- ADD: Colorado, Kentucky, Ohio, Utah
- Bid strategy: unchanged (campaign-level 117%).

### Shell 23586637678 → becomes TIER THREE (currently "Tier3RPL50Plus")
- REMOVE: Colorado, Hawaii, Kentucky, Louisiana, Massachusetts, Mississippi, Nevada, New York,
  Rhode Island, South Carolina, South Dakota, Wyoming
- ADD: Alabama, Maryland, Oklahoma, Oregon, Pennsylvania, Wisconsin
- Bid strategy: unchanged (campaign-level 117%).

### Shell 23428389081 → RE-ENABLED as TIER FOUR (currently "zz-RETIRED-...TIER ONE-LEGACY", Paused)
STRICT SEQUENCE — never enable-then-fix:
1. DETACH from "Tier 1 - Combined" portfolio (@117%) → campaign-level tROAS (F2).
2. SET launch tROAS **112%** (matching TX/FL posture; pending final ratification) and budget appropriate to ~$130/33d-equivalent cohort (see plan workbook).
3. STRIP stale location targets: Connecticut, Colorado, Virginia.
   KEEP (already correct for T4): Hawaii, Louisiana, Nevada, New York, South Carolina.
4. ADD: Delaware, Mississippi, Rhode Island, South Dakota, Wyoming.
   (Net T4 roster = HI, LA, NV, NY, SC, DE, MS, RI, SD, WY — 10 states.)
5. AUDIT contents inherited from its legacy life: 226 keywords, 48 ads, 224 location-related rows.
   Align keywords/negatives to the current library (V11-era); pause/remove anything CT/CO/VA- or
   premium-angle-specific. Ad copy must be geo-generic.
6. RENAME (table below). 7. ENABLE. Verify status + target + roster same-day.

### Rename table (same build; campaign_id is the analysis key throughout)
| campaign_id | new name |
|---|---|
| 23496334752 | HOME INSURANCE_STATE-GROUP_TIER1_COMBINED_TROAS_SEARCH_ALLDEVICE_USA-STE |
| 23530860942 | HOME INSURANCE_STATE-GROUP_TIER2_COMBINED_TROAS_SEARCH_ALLDEVICE_USA-STE |
| 23586637678 | HOME INSURANCE_STATE-GROUP_TIER3_COMBINED_TROAS_SEARCH_ALLDEVICE_USA-STE |
| 23428389081 | HOME INSURANCE_STATE-GROUP_TIER4_COMBINED_TROAS_SEARCH_ALLDEVICE_USA-STE |
(Old names encode retired RPL band definitions; adjust convention to taste — no band numbers in names.)

---
## Sequencing & acceptance
1. All changes staged in one Editor change set; post in one publish, early in the day.
2. Targets DO NOT move at cutover except the two portfolio detaches (at current effective
   numbers) and the T4 launch target. Post-cutover landscape: T1 115% / T2 117% / T3 117% /
   T4 112%. Against the draft floors (1.25/1.14/1.05/1.00): T1 is BELOW its floor (mandatory
   tightening steps post-freeze); T2/T3 sit ABOVE theirs (loosening toward floors is the
   volume-recovery lever, optional); T4 launches above its no-loss floor. Floor stepping begins
   after the 2-week freeze.
3. Same day: pull `ads_editor_locations_<date>_post-build.csv` (incl. paused), reshape to the tiers
   schema, run the model with `--reference-locations` against it.
   **ACCEPTANCE: Ledger Comparison reads 100% SAME (45 rows: 42 tiered + 3 standalone... T4 makes
   it 4 shells; expected tiered = 42 states across 4 shells). Any MOVED row = build defect; fix
   before end of day.**
4. Freeze: 2 weeks, no target changes, exception screen watch-only.

## Prompt for the builder thread
> Attached: `RPL_ReTier_Build_Packet_2026-07-29.md`, `ads_editor_full_2026-07-29_pre-cutover.csv`,
> `ads_editor_locations_2026-07-29_pre-cutover.csv`.
> Build this change set in Google Ads Editor exactly as specified, keyed by campaign_id.
> Re-verify current state against the attached fresh export before staging (do not trust any other
> source). Resolve findings F1/F2 per the packet. The legacy shell follows its strict sequence —
> detach portfolio and set the launch target BEFORE enabling. Stage everything as ONE change set,
> list every staged change for review before posting, and do not post until the pre-flight items
> are confirmed complete. After posting, export locations (incl. paused) for the verification run.
