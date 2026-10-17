# Analysis Brief — Validate MSAN Strategy Sections 1 & 2 (Microsoft Home)

**Purpose:** produce the empirical backing for sections 1 (Objective & Thesis) and 2 (Current State / Findings) of the Microsoft Audience Network rebuild strategy. Each analysis below maps to a specific claim in those sections; the output should let those claims be stated with numbers instead of assertions.

**DB:** `C:\Projects\google-ads-ops\db\paid_search.db` (SQLite, read-only for this work; open `mode=ro`).
**Account:** Microsoft Home Insurance, `account_id = 2` (`F149SN63`).
**Env:** conda `paid-search` (`C:\ProgramData\anaconda3\envs\paid-search\python.exe`) — required for pandas.
**Output:** multi-sheet Excel workbook, one sheet per analysis module, plus a Params/QA sheet.

Read this whole brief before writing SQL. Several conventions will silently corrupt the numbers if missed, and the correct data source differs per analysis because they live at different grains and time scopes.

---

## The five claims to quantify

| # | Section claim | Analysis | Grain / source |
|---|---|---|---|
| A | Audience is a lagged harvest of search intent; tracks search ~1wk lag, decays ~2wk post-spike (§1 thesis, §2 lag pattern) | Weekly lead cross-correlation search vs audience at 0/1/2/3-wk lags + decay | account-total weekly, `campaign_performance_network_daily` |
| B | Conquest GP-negative on audience (1–4% CvR vs 12–14% search); generic holds up (§1, §2 signal translation) | CvR/RPL/GP/spend by ad-group type × network | ad-group grain, `keyword_performance_network_daily` (Path B) |
| C | Audience RPLs hover near search RPLs by state → tier model transfers (§1) | RPL by state × network, parity check | state grain, `geo_performance_daily` |
| D | ~19% WoW GP decline, ~97% traced to audience, concentrated in conquest (§2) | GP-by-week network split + conquest concentration | campaign grain (time) + ad-group grain (concentration) |
| E | tCPA optimizes to leads not GP; can't see the conquest RPL penalty (§2) | RPL dispersion across ad-group types on audience | ad-group grain, `keyword_performance_network_daily` |

---

## Conventions that will silently corrupt the numbers (read first)

1. **`device = 'ALL'` filter is mandatory** on every aggregation. All performance tables store per-device rows PLUS a synthesized `device='ALL'` rollup. Omitting the filter double-counts (~2×). This is the #1 silent error.
2. **Leads / revenue = `primary_conv_count` / `primary_conv_value`** where those columns exist (keyword and geo tables), NOT the generic `conversions`/`all_conversions`. Primary = the zip-to-quote lead the strategy means. (Exception: the campaign network table only has generic `conversions` — see §A verification.)
3. **`cost_micros` is micros** — divide by 1e6 for dollars.
4. **Network buckets:** `network = 'Audience'` → audience; everything else (`'Microsoft sites and select traffic'`, `'AOL search'`, `'Bing and web'`) → search. Collapse with `CASE WHEN network='Audience' THEN 'audience' ELSE 'search' END`.
5. **Revenue-lag cutoff:** Microsoft offline revenue posts with a lag; exclude the trailing 1–2 days from any GP/trend read or recent GP looks catastrophically negative. Parameterize it.
6. **Structural break:** the account ran a single-state campaign setup in 2025 (~50 state campaigns) and consolidated to ~12 then ~7 campaigns in 2026. Any time series spanning that boundary has a structural break — see §A.

---

## A — Echo / lag analysis (the thesis centerpiece)

**Claim:** audience volume is a lagged echo of search-generated intent, not independent demand — tracks search with ~1-week lag, decays ~2 weeks post-spike.

**Method:** build two weekly series — total audience leads and total search leads — and cross-correlate audience[t] against search[t−k] for k = 0,1,2,3 weeks. A correlation peak at k≥1 supports "audience lags search" (echo). Also characterize the decay: after a search-lead spike, how audience leads rise then fall over the following ~2 weeks (impulse-response view).

**Source & grain:** `campaign_performance_network_daily`, aggregated to **account-total per ISO week per network bucket**. Use account-total, NOT campaign-level — the 2025→2026 restructure changed the campaign set mid-series, so a campaign-level series is confounded; account-total is robust to it.

**Two methodological must-dos:**
- **Detrend / first-difference both series before correlating.** Two series sharing a seasonal or budget trend correlate at every lag for reasons unrelated to echo. Report the cross-correlation on first-differenced (week-over-week change) or detrended series, not raw levels. Raw-level correlation will overstate the thesis — state both if you want, but lead with the detrended one.
- **Flag the structural break** at the 2026 consolidation. Consider running the correlation on the consolidated period (2026) separately, since it's the regime the rebuild operates in; the full-history version is a robustness check, not the headline.

**Verification before trusting lead counts:** `campaign_performance_network_daily` carries generic `conversions`/`conversion_value`, not `primary_conv_*`. Confirm its `conversions` equals the primary zip-to-quote lead before using it as "leads" — cross-check one month's audience `conversions` against the same month's audience `primary_conv_count` summed from `keyword_performance_network_daily`. If they diverge materially, the campaign table is counting all-conversions and the lead series needs a correction factor or a different source. Print this check on the QA sheet.

**Output:** a correlation-by-lag table (k=0..3, raw and detrended), a weekly dual-line series (search vs audience leads), and the decay characterization. A chart of correlation-by-lag is the single most persuasive artifact for §1 — include it.

---

## B — Conquest vs generic on the audience network (the structural-break claim)

**Claim:** conquest ad groups run 1–4% CvR and GP-negative on audience while search is healthy at 12–14%; generic ad groups hold up on audience. The divergence is signal translation (conquest keyword → brand-affinity/service-intent population; generic → real shopping intent).

**This is the analysis Path B was built to enable.** Network split at ad-group grain did not exist in the DB until `keyword_performance_network_daily`. Source it from there, joined to `keyword_instances` for `ad_group_name` (and `campaign_name` if needed).

**Ad-group classification (order matters — carve out affinity/pool BEFORE carrier bucketing):**
1. `Costco` → affinity (NOT conquest)
2. `Home-Group` / `Home Insurance-Group` / pooled → pool (NOT conquest)
3. `Home-NonGeneric-<Carrier>` → conquest
4. `Home-Generic-...` → generic
5. else → other

Getting this order wrong is the exact error that once made carrier audience look profitable because Costco was carrying it. (This matches the sibling carrier-GP analysis — keep the classification identical for consistency.)

**Cut:** ad-group type × network bucket → CvR, RPL, GP, spend. The headline table is conquest vs generic vs affinity, each split search vs audience, showing the CvR collapse (search ~12–14% → audience ~1–4%) and GP sign on audience.

**Scope note:** the keyword network table is current-consolidated-structure scope. For a forward-looking rebuild that's the correct scope — it reflects how conquest audience performs *now*, not in the retired state campaigns. State this on the sheet so nobody reads it as full-history.

---

## C — RPL parity by state × network (the tier-transfer claim)

**Claim:** audience RPLs hover near search RPLs by state, so the search tier model transfers to audience (justifies §4's tier×geo structure).

**Source:** `geo_performance_daily` (has `geo_state` and `network`). RPL = `primary_conv_value / primary_conv_count` by state × network bucket.

**Verify first:** confirm `geo_performance_daily` carries `network` and the `primary_conv_*` columns and that `geo_state` is populated (not all 'ALL'). If geo isn't network-split at this grain, this analysis can't run as specified — flag it rather than forcing it.

**Output:** per-state search RPL vs audience RPL, the delta, and a parity summary (share of states within ±X% of search RPL). This is what tells Gilbert whether "the tier model transfers" is true or whether audience needs its own tier cut (which §7 already anticipates re-running at quarter-end).

---

## D — GP-decline decomposition (the crisis number)

**Claim:** the ~19% WoW GP decline was ~97% traced to audience, concentrated in conquest ad groups.

**Two parts, two sources:**
- **Network split over time:** weekly GP by network bucket from `campaign_performance_network_daily`, to locate the decline window and show the audience share of the drop.
- **Conquest concentration:** for the decline window, GP by ad-group type × network from `keyword_performance_network_daily`, to show the drop concentrated in conquest audience.

Identify the specific WoW window the ~19% refers to (or reproduce the largest recent WoW GP drop) and decompose it. If the exact historical window isn't recoverable, quantify the *current* audience GP drag by ad-group type as the standing version of the same finding.

---

## E — The tCPA blind spot (RPL dispersion)

**Claim:** tCPA optimizes to lead count, treats every lead as equal, so it cannot see the conquest RPL penalty — structure must remove what bidding can't.

**Method:** from the same ad-group-type × audience data as §B, show the RPL *dispersion* — conquest audience RPL vs generic audience RPL vs the account audience average. The point is the spread: a lead-count optimizer bids the same for a conquest audience lead and a generic audience lead, but their RPLs differ materially, so tCPA structurally overspends on the low-RPL conquest population. Present it as: RPL by ad-group type on audience, with the implied GP-per-lead gap that tCPA is blind to.

---

## Verify before building (introspection-first)

Run `PRAGMA table_info(...)` and confirm before writing the analyses:
1. `keyword_performance_network_daily` — columns, `device='ALL'` present, network values, the FK to `keyword_instances` (likely `instance_id`).
2. `campaign_performance_network_daily` — does its conversion column = primary leads? (the §A cross-check).
3. `geo_performance_daily` — has `network`, `primary_conv_*`, and populated `geo_state`? (gates §C).
4. Spend unit (micros) and the exact network string values in each table.
5. A **spend QA assertion**: total account spend over a window should match a figure already trusted from the tracker/campaign workbook within a few percent — a ~2× result instantly flags a `device='ALL'` miss before any number reaches the strategy doc.

---

## Output workbook

One sheet per module (A–E), plus:
- **Params & QA** — window, revenue-lag cutoff, the §A conversion-definition cross-check result, the spend QA assertion, classification coverage (what landed in `other` with material spend), and any table that failed introspection (e.g., geo not network-split).
- Each sheet labeled with its grain and time scope (full-history vs current-structure) so no one misreads a current-structure conquest cut as all-history.

Compute all ratios (CvR, RPL, GP, ROAS) within-sheet from summed base measures (leads, revenue, spend, clicks) — never average pre-computed ratios. Impression-weight any impression-share metric; never sum it.

---

## Sibling analysis (keep consistent)

There is a parallel carrier-GP / audience-drainer analysis in flight that uses the **same** ad-group classification (conquest/generic/affinity, same order), the same network conventions, and the same `device='ALL'`/`primary_conv_*`/revenue-lag rules. Keep classification and conventions identical so the two analyses reconcile and Gilbert can cite them together. The difference in scope: that analysis is keyword-drainer + campaign-audience focused; this one is strategy-thesis validation (echo, parity, tCPA blind spot).

---

## One-line summary to open the thread with

*"Read-only validation analysis on `paid_search.db`, Microsoft account 2, to put numbers behind sections 1–2 of the MSAN rebuild strategy. Five modules: (A) search→audience lead echo cross-correlation [account-weekly, campaign network table, DETREND before correlating, mind the 2026 restructure break], (B) conquest-vs-generic on audience [ad-group grain via the new keyword_performance_network_daily, classify affinity/pool before carriers], (C) RPL parity by state×network [geo table], (D) the ~19% WoW GP-decline decomposition, (E) the tCPA-can't-see-RPL blind spot. Verify device='ALL', use primary_conv_* not all-conversions, and confirm the campaign network table's conversions = primary leads before trusting the echo lead counts."*
