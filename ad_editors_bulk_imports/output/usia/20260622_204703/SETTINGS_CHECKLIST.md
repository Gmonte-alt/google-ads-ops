# USIA Home — Google Ads Editor build: settings checklist

Generated 2026-06-22 20:47. Build status for all entities: **Enabled**.

> **HOLD until greenlit.** Per USIA_Launch_Settings.md, stage this build but do not
> push it live until the launch is greenlit. Start date is set to **2026-06-24**.

The CSVs handle campaigns, ad groups, keywords, and locations. The items below are
the settings Google Ads Editor does **not** reliably set via CSV import — apply them
by hand (or in the UI) on the created shells.

## Import order (Editor → File → Import, or paste in "Make multiple changes")
1. `01_campaigns.csv` — 4 campaign shells (type, status, Manual CPC, budget, start date, **EU Political Ads = No**).
2. `02_ad_groups.csv` — 40 ad-group shells (Status Enabled) with Max CPC.
3. `03_keywords.csv` — 563 positive keywords, Status Enabled (confirm the column-mapping dialog).
4. `07_campaign_locations.csv` — apply via **Locations → Make multiple changes → paste** (map
   Campaign / ID / Location / Status), not Account→Import. Spaced display name + numeric ID;
   Type/Reach resolve on Check locations/Post. UTF-16 LE / tab; don't re-save as UTF-8.
5. `05_negatives_campaign_level.csv` — only if you choose the campaign-level negatives fallback (see below).
6. `06_rsa_shells.csv` — one blank Responsive Search Ad per ad group (see "RSA shells").

Status: campaigns follow the build status (**Enabled**) as the launch gate; **ad groups and
keywords are always Enabled** so flipping a campaign live activates everything under it. RSA
shells are Paused (blank placeholders).

## EU political ads (required)
Every campaign must self-declare. `01_campaigns.csv` carries `EU Political Ads = No`. If Editor
doesn't map that column on your version, set it in the UI: Campaigns → select all → Edit →
**Confirm EU Setting** → "No, these campaigns don't have EU political ads."

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
  - `USIA_HOME_BRAND_CARRIER`: $880/day
  - `USIA_HOME_BRAND_GENERIC`: $150/day
  - `USIA_HOME_GEN`: $820/day
  - `USIA_HOME_GEN_CALL`: $25/day
  The bare-carrier campaign (`USIA_HOME_BRAND_GENERIC`) must stay isolated and
  separately measurable — it coexists with the live Quote.com home account on these
  terms (watch for self-competition / CPC inflation).

## Locations
- `07_campaign_locations.csv` (UTF-16 LE / tab) adds the 12 states (Enabled) and
  **removes United States (Removed)** on each campaign. Columns match the proven tier-import sheet:
  **Campaign / ID / Location / Status**, where `Location` is the **spaced display name**
  (`Utah, United States`) and `ID` is the bare numeric Criteria ID (`21177`). Editor matches that
  spaced name + ID to the geo constant and resolves **State** — the raw no-space canonical from
  the geotargets file does not match. IDs verified against Google's geotargets table.
- **Apply via** Locations → Make multiple changes → "My data includes columns for campaigns" →
  Paste from clipboard → map Campaign / ID / Location / Status → Process → Keep. **Type and Reach
  populate once Editor resolves** (Check locations / Post) — that's the resolved state you see in a
  working import.
- The US-removal row (Status Removed) clears the default; if the paste flow doesn't expose Status,
  add the 12 states first, then select the United States target and Remove it.
- Targeting method = **Presence (people in or regularly in your targeted locations)**. Set this per campaign in the location settings.
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

## Negative keywords — the shared list <-> campaign link is NOT importable
Your 8 shared lists exist with the right keyword counts but sit at **Campaigns = 0**. There is
**no Editor CSV/import that attaches a shared list to a campaign** — confirmed across Google's
docs. That link (CampaignSharedSet) is created only by the web-UI "Apply to campaigns" action or
the Scripts/API. So the matrix CSV was never importable; use one of these:

**Route A — run `08_apply_negative_lists.js` (automated, recommended):** Google Ads →
Tools & Settings → Bulk actions → Scripts → + → paste → Authorize → Preview → Run. It calls
`campaign.addNegativeKeywordList()` to attach each existing list to its campaigns per the matrix,
and is idempotent. Run it in the account where the USIA campaigns + these lists live.

**Route B — web UI bulk apply:** Tools → Shared library → Negative keyword lists → tick a list →
Apply to campaigns → check its campaigns → Apply. Repeat per list using `04_negative_apply_matrix.csv`
as the which-list-where checklist (8 lists, a few campaigns each).

`05_negatives_campaign_level.csv` (campaign-level negatives) is only a last resort if you abandon
shared lists; not needed with Route A or B.

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
