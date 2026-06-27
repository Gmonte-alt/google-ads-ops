# USIA Home — Google Ads Editor build: settings checklist

Generated 2026-06-22 19:34. Build status for all entities: **Enabled**.

> **HOLD until greenlit.** Per USIA_Launch_Settings.md, stage this build but do not
> push it live until the launch is greenlit. Start date is set to **2026-06-24**.

The CSVs handle campaigns, ad groups, keywords, and locations. The items below are
the settings Google Ads Editor does **not** reliably set via CSV import — apply them
by hand (or in the UI) on the created shells.

## Import order (Editor → File → Import, or paste in "Make multiple changes")
1. `01_campaigns.csv` — 6 campaign shells (type, status, Manual CPC, budget, start date).
2. `02_ad_groups.csv` — 42 ad-group shells with Max CPC from usia_adgroup_maxcpc.csv.
3. `03_keywords.csv` — 571 positive keywords (confirm the column-mapping dialog).
4. `06_rsa_shells.csv` — one blank Responsive Search Ad per ad group (see "RSA shells").
5. `07_campaign_locations.csv` — 12 launch states x each campaign (criteria IDs).

## Bidding — Manual CPC now, portfolios in the UI after
Import every campaign on **Manual CPC** (already set in 01_campaigns.csv) with the
ad-group Max CPCs from `02_ad_groups.csv` — this clears Editor's missing-bid warning
and lets the account run if it goes live before the portfolios are attached. Then,
**in the Google Ads UI** (Shared library → Bid strategies), build two **portfolio
Maximize Clicks** strategies and attach them:
  - **USIA_MaxClicks_Conquest** (Maximize Clicks, max-CPC cap $13): USIA_HOME_BRAND_CARRIER
  - **USIA_MaxClicks_Generic** (Maximize Clicks, max-CPC cap $12): USIA_HOME_GEN, USIA_HOME_GEN_CALL, USIA_HOME_BRAND_GENERIC
  Caps are ~10% under breakeven for a new-account Quality-Score buffer. Conquest is
  isolated on its own portfolio + budget; bare-carrier rides the Generic portfolio.

## Daily budgets (opening settings — calibrate week 1 from search_budget_lost_is)
01_campaigns.csv carries these; confirm on import:
  - `USIA_HOME_BRAND_AFFINITY`: $—/day
  - `USIA_HOME_BRAND_AGGREGATOR`: $—/day
  - `USIA_HOME_BRAND_CARRIER`: $880/day
  - `USIA_HOME_BRAND_GENERIC`: $150/day
  - `USIA_HOME_GEN`: $820/day
  - `USIA_HOME_GEN_CALL`: $25/day
  The bare-carrier campaign (`USIA_HOME_BRAND_GENERIC`) must stay isolated and
  separately measurable — it coexists with the live Quote.com home account on these
  terms (watch for self-competition / CPC inflation).

## Locations
- Targeting = **Presence (people in or regularly in your targeted locations)**. Set this option per campaign in the UI/Editor
  location settings (the CSV carries the targets, not the presence-vs-interest toggle).
- Launch states (12): Illinois, Pennsylvania, Ohio, New Jersey, Tennessee, Missouri, Minnesota, Alabama, Wisconsin, Oklahoma, Connecticut, Louisiana.
- **Phase 2 — do NOT enable at launch:** CA, TX, GA, FL.
- State revenue differences: handle now with **per-state bid adjustments** (don't split
  campaigns); Phase 2 moves to tROAS-with-RPL-values inside the pooled campaigns.

## RSA shells (hand-off to the copy thread)
`06_rsa_shells.csv` has one RSA row per ad group with the full Editor schema
(Headline 1–15, Description 1–4, Path 1/2, Final URL), blank everywhere copy goes.
It is a **fill-in template**, not directly importable: Editor rejects an RSA with
fewer than **3 headlines, 2 descriptions, and a Final URL**. The copy thread fills
the blanks (from `USIA_AdGroup_Intent_Map.md`) and adds the Final URL, then imports.

## Shared negative lists (build in Shared Library, then apply)
The eight lists are **shared negative keyword lists**, not per-campaign one-offs.
- For each `negative_lists/NEG_*.txt`: Shared library → Negative keyword lists →
  create a list of the same name → paste the file's contents (already in match-type
  syntax: bare = broad, "quoted" = phrase). There is no CSV upload in the UI — paste.
- Apply each list to campaigns per **`04_negative_apply_matrix.csv`**.
- `05_negatives_campaign_level_FALLBACK.csv` is a QA view of that matrix.

Routing reminders (do **not** "fix" the gaps — they're deliberate):
- NEG_AUTO_VEHICLE → Bare-carrier only (Generic/Conquest hold home+auto bundle terms).
- NEG_BRAND_BUY → Generic only (routes our brand queries to the Conquest campaign).
- NEG_HOME_ROUTING → Bare-carrier only, broad (routes home-qualified brand queries to Conquest).
- NEG_BRAND_DNB → every campaign (universal restricted-brand block).

## Other campaign settings
- **Network:** Google Search only — **exclude** Search Partners and Display.
- **AI Max:** **OFF** at launch. When later enabled, regenerate the Bare-carrier
  campaign with `--ai-max` (exact-only, drops the redundant phrase twins).
- **Call-first assets:** enable Call Assets + call reporting on every campaign;
  "speak to an agent" is the differentiator. Count qualified calls as **primary** conversions.

## Phase 2 (post-launch — do not build now)
At ~30 conversions/campaign, pivot both portfolios from Maximize Clicks to **tROAS**,
feeding **RPL as conversion value** (parallel portfolios `USIA_tROAS_Conquest` /
`USIA_tROAS_Generic`), so Smart Bidding handles state-level economics inside the
pooled campaigns — no geo-split campaigns.
