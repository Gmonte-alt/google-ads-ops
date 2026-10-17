# BUILDER PROMPT — Google Home Three-Book Restructure Cutover
**Source of truth:** `rollout_plan.xlsx` (frozen run, `keyword_book_assignment_FROZEN_2026-08-27.csv`, ruleset `8/27+9/2a+9/2b+9/4-aggtail`, all gates PASS 9/7-9/8). This document executes that workbook; where they conflict, the workbook wins.
**Rulings in force:** D1 champion = TIER2 · D2 = repurpose CALIFORNIA 23385188622 as national flagship · D5 = all campaigns launch tROAS 1.10 · DNB = active still-bought keys ride, paused kw/ad-group never re-included · PMax 24052581480 out-of-scope · Launch principle = transition-first, L90-gated · Struck rows (9/8): `citzen home insurance quote` [EXACT], `universial property and casualty` [BROAD].
**Scope: 216 keywords flying · 75 build actions (21 generic adds − 0, 29 brand-home adds, 25 flagship keys) · everything else KEEP-in-place or pause.**

---
## PHASE 0 — Preconditions (all must be true before Phase 1)
- [ ] S6 plan text carries both pre-declarations: (1) ~+14d target step — flagship tightens, both home-intent books loosen; (2) Wave-2 ad groups added no earlier than the +28d read.
- [ ] S6 **baseline captured and pinned** by the analysis thread (generic CPC / generic CPL / generic impressions / flagship CPL / blended CPL). No account change of any kind before this is pinned.
- [ ] Fresh campaign export diffed clean (last run: only ruled PMax + 111 legacy paused).
- [ ] Change-log ready to receive same-day entries.

## PHASE 1 — Build destinations (no pauses in this phase)
### 1A. TEXAS 23394808297 — Generic campaign
1. Create 5 ad groups: `Home-Generic-Quote`, `Home-Generic-Peril`, `Home-Generic-State`, `Home-Generic-Core`, `Home-Generic-Other`.
2. Add the **21 transition keywords** (both match types where listed; theme = launch ad group):
   - **Core:** home insurance [EXACT] · homeowners insurance [BROAD] · homeowners insurance [EXACT]
   - **Quote:** homeowners insurance quote [EXACT]
   - **Peril:** fire insurance [BROAD+EXACT] · fire insurance california [BROAD+EXACT] · fire insurance in california [EXACT] · california home insurance in high fire [EXACT] · home insurance in high fire risk areas [EXACT] · flood insurance [BROAD] · florida flood insurance [BROAD]
   - **State:** california home insurance [BROAD+EXACT] · homeowners insurance california [EXACT] · colorado home insurance [BROAD] · florida home insurance [BROAD] · utah home insurance [BROAD]
   - **Other:** home insurance calculator by zip code [PHRASE] · best home insurance [PHRASE] *(COMPARE, wave-parked)*
3. **D7 — existing 47 TX generic keys (open, rule before cutover):** *Recommended:* move them into the themed groups now (per-key mapping = K1T column `proposed_ad_group`), pausing their old instances — generic is the reset-tolerant class (S1: rated 0% below-average even in rebuilt shells), Gilbert wants the quote split, and routing negatives only work cleanly on a themed structure. *Fallback:* leave the 47 in place, themed groups hold only the 21 adds, re-theme post-S6. **One-word ruling needed: MOVE or LEAVE.**
4. Ad-group routing negatives (PHRASE, precedence-aware — a theme's tokens negatived only in LOWER-precedence groups, per K1T routing block): "quote"/"quotes"/"estimate" into Peril/State/Core/Other; peril tokens into State/Core/Other; state names into Core/Other.
5. Campaign-level EXACT negatives: all **107 flagship terms** from `B3_Seam_Negatives` List 1, verbatim.
6. Pause TX keywords not in the generic book (they are flagship/brand-home rows now living elsewhere) — defer to Phase 2 if preferred, same day regardless.

### 1B. TIER2 23530860942 — Brand-Home campaign
1. Add the **29 transition keywords** (list = M1 `TRANSITION-ADD to TIER2` minus the 2 struck rows) into matching carrier ad groups; create a carrier group only where none exists (hochheim prairie, plymouth rock, wawanesa, csaa, mercury, bamboo, american home, homeowners of america, nerdwallet as needed).
2. Existing 95 TIER2 brand-home keys: **untouched** (KEEP-instance by design).
3. Campaign-level EXACT negatives: the same 107 flagship terms (B3 List 1).
4. Pause TIER2 keywords not in the brand-home book — Phase 2 acceptable.

### 1C. CALIFORNIA 23385188622 — repurpose as National Flagship
1. Rename per convention (suggested: `HOME INSURANCE_FLAGSHIP_NB_TROAS_SEARCH_ALLDEVICE_USA-STE`).
2. Geo: national **minus CA/FL**? **No — D2 repurpose supersedes the charter's geo note only if ruled; default = national ALL states.** Confirm geo with Gilbert in one word: NATIONAL-ALL or NATIONAL-MINUS-CA/FL. *(Recommendation: NATIONAL-ALL — the vendor price list is state-blind on the buy side and seams are by keyword class, not geography.)*
3. Create carrier-family ad groups and place the **25 launch keys** (B1 `launch_scope=LAUNCH`): Farmers (2) · Geico incl. condo/landlord (3) · Travelers (4) · Kin (4) · Hartford (4) · Costco incl. condo (3) · American Family (3) · Universal condo (2).
4. Campaign-level PHRASE negatives: the **6 home tokens** from B3 List 2 ("home", "homeowner", "homeowners", "house", "dwelling", "property").
5. Pause every legacy CALIFORNIA keyword/ad group not in the flagship launch list. Honor every P3 `DO NOT RE-INCLUDE` verdict; the 116 DEFER flagship keys are **not built**.

**GATE 1 (before Phase 2):** TX holds 68 generic keys across 5 groups (D7=MOVE) or 21 adds (D7=LEAVE) · TIER2 gained exactly 29 · CALIFORNIA holds exactly 25 across ~8 groups · negatives counts 107/107/6 · spot-check: "geico home insurance" serves nowhere as a keyword except TIER2's geico group, and `[geico]` exact-negative present in TX + TIER2.

## PHASE 2 — Pause donors (same day, after Gate 1)
- FLORIDA 23394808300, TIER1 23496334752, TIER3 23586637678, TIER4 23428389081: **campaign pause**, rename `zz-RETIRED-<name>`.
- Any remaining in-place pauses from 1A.6/1B.4 not yet done.
- Nothing is deleted. Deferred keys (118 generic / 123 brand-home / 116 flagship) stay enumerated in the workbook as the expansion backlog — promoted only on search-query evidence, post-S6 reads.

## PHASE 3 — Settings (after Phase 2)
- Targets: **1.10 tROAS on all three campaigns** (D5).
- Budgets: per `A1_Budgets_Targets` — flagship capped at ≤ its trailing share (lower of REF/L30), balance to TX + TIER2 (Gilbert sets dollars).
- Verify in UI that no other bidding/geo settings carried over stale on CALIFORNIA (device modifiers, audiences, old geo lists).

## PHASE 4 — Log and freeze
- Change-log entries for every action, same day; cutover date recorded — the S6 clock starts here.
- Freeze window: no discretionary changes. Pre-declared exceptions only: +14d target step; +28d earliest Wave-2 ad groups.
- Reads at +14 (step only), +28, +56 per S6. Falsification clause stands as pre-registered.

**Open one-word rulings before cutover day: D7 (MOVE / LEAVE) and 1C geo (NATIONAL-ALL / MINUS-CA/FL).**
