# BUILDER PROMPT v2 — Corrections Pass (supersedes v1 where they conflict)
**Basis:** frozen edition `ruleset: 8/27+9/2a+9/2b+9/4-aggtail+9/8-prodintent` (row_count 2157; books 393/913/529) · `rollout_plan.xlsx` 9/9 run, all gates PASS, B3 = 104 exact + 8 phrase, 0 self-block · Gilbert's 15 rulings (9/8) · account state per the 9/11 Ads Editor audit.
**Campaign names as they now exist:** `…GENERIC-HOME-INTENT…` (ex-TEXAS) · `…BRAND-HOME-INTENT_COMBINED…` (ex-TIER2) · `…FLAGSHIP_CAFL…` (ex-CALIFORNIA, CA+FL geo) · `…FLAGSHIP_NB…` (national, staged-paused) · FL/T1/T3/T4 paused donors.

## LAW 0 — RENAME > REUSE > CREATE
Never create an ad group where an existing one holds the keys and their RSA history — rename it to convention instead (renames reset nothing; new groups mean new RSAs through ad review and fresh serving ledgers). Creation is sanctioned only where listed below. **LAW 0b — LABELS:** every touched entity gets exactly one of `restr-2026-09-pause` / `restr-2026-09-add` / `restr-2026-09-move`. No unlabeled changes; rollback = filter label, re-enable.

## A. GENERIC-HOME-INTENT campaign (generic book only — everything else pauses)
1. **Quote extraction (R1/R4):** move the 4 quote keys out of `Home-Generic-Home Insurance-Core` and `home insurance estimate` out of `Home-Generic-Cost-Core` → all into `Home-Generic-Quote` (add there with label; pause originals with label).
2. **(R2)** `texas home insurance` (both match types) Core → `Home-Generic-State`.
3. **(R3 — validated 9/9, texts as stored):** Pause `Home Owners Insurance` [BROAD] and `home owner insurance` [BROAD] (labels). Validation (variant_query_check.py, DIAG2): zero reportable query rows for either since 2026-03-01 — 73,090 historical rows, all pre-March — while the survivors carried the full stream; these are dead keywords, consolidation already occurred via same-meaning matching. Note: "homeowner insurance" (one word) does not exist as a standalone keyword — no action. **Consolidation review complete (core_overlap_check.py, 9/9): `home insurance` [BROAD] and `homeowners insurance` [BROAD] are KEEP-BOTH** — shared-query concentration 63–65% (below the 70% redundancy bar), ~288 REF leads across two ~5,000-query exclusive tails; complementary routing doors, not duplicates. Core final launch roster: 2 broads + 2 exacts, variants paused.
4. **Book purge:** pause ALL `Home-NonGeneric-*` / `Home-Non-Generic-*` ad groups still enabled here (Farmers, Geico, Costco, Hartford, Kin, State Farm, American Family, Group1/3/4/6, Home_New, and any others) — group-level pause, labeled. Their keys' homes are BH/flagship.
5. **Shells:** `Home-Generic-Compare` and `Home-Generic-Cost-Core` are wave-2; either pause them (fold their ~4 keys into Other/Core) or leave paused-empty — Gilbert's call, default = pause + fold.
6. **Verify negatives:** campaign-level EXACT list = the **104 flagship terms** (B3 List 1, current edition); ad-group routing negatives precedence-aware per K1T (a theme's tokens negatived only in lower-precedence groups).

## B. BRAND-HOME-INTENT campaign
1. **Renames (R8/R11/R12, keep RSAs):** `Home_Group2` → `Home-NonGeneric-American Insurance-Core` · `Home_Group5` → `Home-NonGeneric-Allied Insurance-Core` · `Home_Group6` → `Home-NonGeneric-21st Century Insurance-Core`.
2. **(R11)** Split `allied trust` keys into a new `Home-NonGeneric-Allied Trust Insurance-Core` group (sanctioned creation).
3. **Geico consolidation (R14):** two enabled Geico groups exist. Move any keys unique to the new `Geico Insurance-Core` into the historic `Geico-Core`, pause the new group, rename historic → `Home-NonGeneric-Geico Insurance-Core`.
4. **NerdWallet (R13):** move its keys from the created `NerdWallet-Core` group into `Home_New`; pause `NerdWallet-Core`.
5. **(R9)** Ensure `norcal aaa` + all aaa home/homeowners keys sit in `Home_Group1`; `aaa renters insurance` pauses with the Renters groups. **(R10)** `Home_Group4` retained as-is (plymouth rock + universal cohabit).
6. **R6 migrations IN (the reconstructed list — authoritative, do not re-derive):** ensure enabled here, in their carrier groups: `costco condo insurance` [EXACT] · `farmers condo` [PHRASE] · `geico condo insurance` [EXACT+BROAD] · `geico landlord insurance` [EXACT+BROAD] · `geico property insurance` [EXACT+BROAD] · `universal condo insurance` [EXACT+BROAD] · `universal property` [PHRASE] · `costco property insurance` [BROAD]. (Un-pause `costco condo insurance` here — audit found it paused backwards.)
7. **Book purge:** pause the enabled generic groups (`Home-Generic-*`) and the stray `Farmers Insurance-Flagship` group.
8. **Verify negatives:** campaign-level EXACT = the same 104 flagship terms.

## C. FLAGSHIP_CAFL campaign
1. **Duplicate collapse (R5/R14/R15):** for the seven carriers with both `-Core` and builder-created `-Flagship` groups enabled (Farmers, Geico, Costco, Hartford, Kin, Travelers, American Family): pause the created `-Flagship` duplicates; keep the historic `-Core` groups; rename them `-Core` → `-Flagship`. Also pause the created `Universal Insurance-Flagship` group (its only key migrated to BH).
2. **R6 migrations OUT:** pause here (labels): geico condo/landlord/property, costco condo, universal condo (in Group4 too), and `safeco landlord insurance` (EXCLUDE-book, non-biddable — pause wherever enabled).
3. **Book purge:** pause the enabled generic groups (`Home-Generic-*`) and the brand-home groups (Group1/3/4/5/6, Home_New, Home_CaRegion) — their books live in TX/BH.
4. **Verify negatives:** campaign-level PHRASE = the **8 tokens** (home, homeowner, homeowners, house, dwelling, **property insurance**, condo, landlord — "property insurance", never bare "property", per the P&C carve-out).

## D. FLAGSHIP_NB (national) — already clean
18 bare-brand keys, 9 carrier groups, paused pending cutover. Confirm no condo/landlord/property key present; apply the same 8 phrase negatives; geo stays national-minus-CA/FL. Enable at cutover.

## E. Account-wide
1. Do-not-build texts regardless of book: `citzen home insurance quote` (any match), `universial property and casualty` (any match), `home` [PHRASE], `zombie home insurance` (9/8b batch pending; enforced here now).
2. Donor campaigns stay paused; `zz-RETIRED-*` renames if not yet applied.
3. Settings unchanged from v1: all three live campaigns at tROAS 1.10 (D5); budgets per A1; +14d target step pre-declared in S6.

## GATES
**Gate A (after A–D):** TX holds generic-only enabled groups; BH gained the 12 migrant keys + renames done; CAFL has exactly one enabled group per carrier, all `-Flagship`-suffixed; zero condo/landlord/property keys enabled in either flagship campaign; negative counts 104/104/8/8. **Gate B (before cutover):** S6 baseline pinned; every change this pass carries a `restr-2026-09-*` label; spot-check "geico condo insurance" serves only from BH's Geico group and is blocked in both flagship campaigns by the condo phrase negative.
