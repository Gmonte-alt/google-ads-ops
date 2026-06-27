# USIA Home — Google Ads Editor build: settings checklist

Generated 2026-06-22 16:59. Build status for all entities: **Paused**.

The CSVs handle campaigns, ad groups, and keywords. The items below are the
settings Google Ads Editor does **not** reliably set via CSV import — apply them
by hand on the created shells before enabling anything.

## Import order (Editor → File → Import, or paste in "Make multiple changes")
1. `01_campaigns.csv` — creates the 6 campaign shells (type, status, bid strategy, budget).
2. `02_ad_groups.csv` — creates the 42 ad-group shells (+ bare-carrier Max CPC).
3. `03_keywords.csv` — loads the 571 positive keywords.
   - On import, Editor shows a column-mapping dialog; confirm Campaign / Ad Group / Keyword / Match Type map through.
4. `06_rsa_shells.csv` — one blank Responsive Search Ad per ad group (see "RSA shells" below).

## RSA shells (hand-off to the copy thread)
`06_rsa_shells.csv` has one RSA row per ad group with the full Editor schema
(Headline 1–15, Description 1–4, Path 1/2, Final URL), pre-filled with Campaign /
Ad Group / Ad type / Status and blank everywhere copy goes. It is a **fill-in
template**, not directly importable as-is: Editor rejects an RSA with fewer than
**3 headlines, 2 descriptions, and a Final URL**. The copy thread populates the
blanks (feeding from `USIA_AdGroup_Intent_Map.md`), adds the Final URL once the USIA
Home domain/paths are confirmed, then imports. Until then the ad-group shells from
`02_ad_groups.csv` already hold the structure.

## Shared negative lists (do this in Shared Library, then apply)
The eight lists are **shared negative keyword lists**, not per-campaign one-offs.
- For each `negative_lists/NEG_*.txt`: Shared Library → Negative keyword lists → create a list of the same name → paste the file's contents (already in match-type syntax: bare = broad, "quoted" = phrase).
- Apply each list to campaigns per **`04_negative_apply_matrix.csv`**.
- `05_negatives_campaign_level_FALLBACK.csv` is a QA view of that matrix expanded to rows — use it to verify, or as a fallback only if you are not using shared lists.

Routing reminders (do **not** "fix" the gaps — they're deliberate):
- NEG_AUTO_VEHICLE → Bare-carrier only (Generic/Conquest hold home+auto bundle terms).
- NEG_BRAND_BUY → Generic only (routes our brand queries to the brand campaigns).
- NEG_HOME_ROUTING → Bare-carrier only, broad (routes home-qualified brand queries to Conquest).
- NEG_BRAND_DNB → every campaign (universal restricted-brand block).

## Campaign settings to set by hand
- **Network:** Google Search only — **exclude** Search Partners and Display.
- **Locations (Presence (people in your targeted locations)):** NJ, IL, PA, OH, MO, AL, TN, CT, MN, WI, OK, OR.
  - **Phase 2 — do NOT enable at launch:** CA, TX, GA, FL.
- **Budgets:** each campaign on its **own** budget (01_campaigns.csv Budget is blank — fill from the media plan). The **Bare-carrier** campaign must be isolated and separately measurable (it coexists with the existing Quote.com home account on these terms — watch for self-competition / CPC inflation).
- **Bidding:** Manual CPC at launch (blank-pixel account). Bare-carrier ad-group Max CPC is pre-filled from Carrier Selection; **set a default Max CPC for the non-bare ad groups** (no per-keyword guidance exists). Move to tCPA/tROAS after ~30–50 conversions bank. Blended ROAS reference ≈ 1.29.
- **AI Max:** **OFF** at launch — confirm search-term matching, text customization, and final URL expansion are all off (new Search campaigns may default them on). When AI Max is later enabled, regenerate the Bare-carrier campaign with `--ai-max` (exact-only, drops the redundant phrase twins).
- **Call-first assets:** enable Call Assets + call reporting on every campaign; "speak to an agent" is the differentiator. Coordinate call-conversion tracking so qualified calls count as **primary** conversions.

## Note on the media plan (DRAFT)
`USIA_Home_Google_Media_Plan_DRAFT_v2.docx` agrees on launch states, Phase-2 states, ROAS ≈ 1.29, AI Max off, and call-as-primary. Its *draft* architecture (two consolidated campaigns on Target ROAS) differs from this approved six-campaign / Manual-CPC build; the workbooks/handoff are the build authority. Reconcile before exec sign-off if needed.
