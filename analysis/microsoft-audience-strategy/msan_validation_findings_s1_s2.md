# MSAN Rebuild Strategy — Empirical Validation of Sections 1–2

**Account:** Microsoft Home Insurance (account_id=2, F149SN63) · **Date:** 2026-08-06 (v2)
**Sources:** `msan_strategy_validation.xlsx` (windowed run, c_min=15), `microsoft_campaign_audience_persistence.py` diagnostic, `campaign_change_events`.
**Data certification:** campaign pair (search + audience) reconciles to `geo_performance_daily` within $1/month for May–Jul 2026; keyword-table revenue matches the pair at 1.00/1.00/0.97. Microsoft platform revenue was non-existent in 2025; tracking began in 2026, so all dollar figures are windowed to the certified 2026-05-01+ period. Lead, click, and CvR figures are valid over full history.
**Structural context:** in the first week of June 2026, the remaining single-state campaigns were retired into the consolidated Tier 1 and Tier 2 campaigns. Both tiers surged on both networks at launch, then decelerated; the launch surge has not been replicated. Tier 2 audience ROAS subsequently collapsed and audience was excluded (~7/06). Tier 2 impression share remains low with substantial headroom; its tCPA has been raised over the last 3–4 weeks and search impressions are climbing.

---

## Verdict summary

| # | Original claim | Verdict | Certified finding |
|---|---|---|---|
| A | Audience lags search ~1 week, decays ~2 weeks post-spike | **Reframed — the lag is not fixed, the pattern is real** | Search↑ → audience↑ same-week (first-diff corr +0.59). Search↓ → audience volume holds or rises (T1 wk 7/13: search −$2.8K, audience +$0.6K to 61% share). The risk is delayed *economics* decay, not delayed volume (T2 arc) — A2 test: audience ROAS tracks search direction at k=0 (+0.36 to +0.62) with modest carry into the following week at account level (+0.35) and T2 (+0.32) |
| B | Conquest 1–4% CvR, GP-negative on audience; generic holds up | **CvR divergence confirmed; GP sign REVERSED in current window** | Conquest retains 31% of search CvR on audience (14.6%→4.5%) vs generic 62% (11.8%→7.3%). But conquest audience is GP-positive: ROAS 1.41, +$9.5K GP, $61 RPL |
| C | Audience RPLs hover near search RPLs by state | **Confirmed** | Median state delta −5.3% (30 qualified states); 47% within ±15%; matches account-level −7% (Jul: audience $57.7 vs search $61.8) |
| D | ~19% WoW GP decline, ~97% audience, concentrated in conquest | **Not reproduced; June episode is confounded** | Largest recent decline (6/15→6/22, −47%) was search-driven (−$4.7K search vs +$0.4K audience) — and is substantially the post-consolidation launch surge normalizing plus state campaigns going dark, not pure demand |
| E | tCPA can't see the conquest RPL penalty | **Mechanism stands; the penalized bucket moved** | Audience RPL dispersion is real ($43.85–$61.14/lead) but conquest is currently the HIGHEST-RPL audience population (+19% vs average). tCPA blindness cuts both ways: it under-values conquest audience today as much as it over-valued it before |

---

## A. The echo thesis — restated and evidenced

**The operative thesis (replacing the fixed-lag version):** when search volume rises, audience volume rises with it; when search volume falls, audience volume holds — but audience economics (GP/ROAS) eventually decay if search stays down. The lag is in the *value* of the harvested intent, not in the volume.

**Volume co-movement (certified):** weekly first-differenced cross-correlation +0.59 at k=0 on the consolidated period; daily-grain analysis (lags 0–14d) also peaks at k=0. No fixed volume lag exists at any tested resolution.

**Persistence under contraction (certified, tier-level):**
- Account, late June: search fell −$5,446 (−36%) on 6/22 while audience held ($5.0K→$5.3K), share 24.7%→34.9%; audience then surged to $8.7K (45.5% share) on 6/29.
- Tier 1, week of 7/13: search −$2,809 WoW while audience rose +$555 to $3,159 — **60.9% audience share**. The cleanest single-campaign instance of the pattern.
- Tier 3 (7/27) and Tier 5 (6/29, 7/27) show the same signature at their large search drops.

**Economics decay (the Tier 2 arc, now quantified):** T2 surged at the June consolidation (search $4.0K→$5.4K, audience $1.7K), decelerated (search $2.5K by 6/22 while audience *held* at ~$850–890), and then audience ROAS collapsed — leading to the 7/06 exclusion. The A2 statistic backs the pattern directionally: audience ROAS correlates with search revenue change at k=0 in every tier (+0.36 to +0.62 — audience economics move with search direction the same week), with positive carry into the following week at the account level (+0.35) and, tellingly, in Tier 2 itself (+0.32) — the one campaign where the decay actually ran to collapse. By k=2 the correlations turn negative (mean reversion). With only 9–13 weekly observations per tier this is directional evidence, so it should be read alongside the T2 arc rather than instead of it.

**Does audience lead search? (the Tier 5 question):** tested account-wide with negative lags — weekly correlations at k=−1..−3 are all negative or near zero (−0.16 at k=−1), and the best daily audience-leading correlation is a weak +0.18 at −7 days. Audience leading search is not a systematic account-level pattern; the Tier 5 sequence (audience up 6/29–7/6, search following 7/13) stands as an episode-specific observation, plausibly reflecting supply elasticity in an impression-share-constrained tier, and its 7/6 peak is confounded by the T2 audience exclusion the same week. Fine to mention as color; not a §1 claim.

**Reallocation vs persistence (A3 — the mechanics test):** the strategy thread raised the possibility that persistence is partly budget machinery — freed search budget flooding audience inventory during declines. The signature test says no, at the account level: when search leads fall, audience impressions *contract with them* (corr +0.42, the opposite of the reallocation prediction), CPCs stay roughly flat (+0.19), and — the standout — audience CvR falls with search demand (corr +0.58). That last number is the decay mechanism made directly visible: the intent pool cools when search demand cools, at similar delivery and prices. Persistence is therefore predominantly *differential decay* — audience's signal base (browse, context, in-market) thins more slowly than query volume — not machinery expansion. The one exception is diagnostic gold: in Tier 2's worst search week (6/15), audience impressions spiked to 283% of its other-week average while CvR halved to 52% — the reallocation failure mode did occur, once, in exactly the campaign whose audience ROAS then collapsed and was excluded. Tier 1's same week showed the opposite (impressions 87%, CPCs 118% — a harder auction, not a cheaper one). Two consequences: (1) the post-split recapture baseline should be the co-moving audience level, and most of it should survive the split, since the persistence is intent-side; the CvR-follows-search dynamic will also survive the split — it is a property of the pool, not the structure — so standalone audience campaigns need CvR/ROAS-based tripwires from day one; (2) the T2 signature (audience impression surge + CvR collapse during a search-down week) is the leading indicator the governance telemetry should alarm on. Caveat: 14 weekly observations; directional.

**The incrementality caveat (keep it in the doc):** after T2 audience was cut, T2 search held flat at $3.1–3.4K/week — no backfill of the removed ~$575–890/week. A portion of audience volume is genuinely incremental, which argues for selective preservation and restructure, not elimination. Note the forward-looking wrinkle: T2 tCPA raises over the last 3–4 weeks are lifting search impressions, so the clean post-exclusion baseline is now ending.

**Recommended §1 thesis language:** *"Audience volume co-moves with search-generated intent: it rises with search the same week, and when search demand contracts it persists — in the sharpest 2026 episodes audience revenue held or grew while search fell 30–40%, lifting audience share as high as 61% of a tier's revenue. The exposure is not volume, it is value: sustained search declines are followed by audience economics decay, as Tier 2's post-consolidation arc (volume persistence, then ROAS collapse, then exclusion) demonstrates. The Tier 2 exclusion also shows search does not backfill removed audience revenue, so a portion of audience is incremental. The rebuild therefore structures audience to capture the co-moving and incremental volume while cutting exposure to the decaying tail — rather than treating audience as pure echo or eliminating it."*

## B. Conquest vs generic — divergence confirmed, GP sign updated

The CvR collapse is confirmed and starker in the certified window: conquest **14.6% search → 4.5% audience** (retains 31%); generic **11.8% → 7.3%** (retains 62%). Signal translation is real and differential.

The GP claim needs updating: in the certified window **every ad-group type is profitable on audience** — conquest audience ROAS 1.41 (+$9.5K GP, RPL $61.14, GP/lead $17.88), generic 1.64, affinity 2.17, pool 1.61. The GP-negative conquest audience of the original claim is not present in current data (and cannot be verified historically — no 2025 revenue exists). The honest §2 framing: conquest audience converts at a third of its search rate and costs more per lead ($43.25 CPL vs $37.66 search), but the leads it does produce are currently the highest-RPL audience population. The structural risk is the CvR fragility and the T2-style decay exposure, not a standing GP drain.

**Carrier break-out (B2 — the masking check):** the GP-positive conquest-audience aggregate was challenged on suspicion that large carrier brands hide drains. It survives: 4 of 21 carriers are GP-negative on audience, totaling −$376 on $741 spend, against +$9,872 from the rest. What the break-out adds is a margin gradient — State Farm (largest carrier audience spend, $5.5K) runs ROAS 1.1 and Farmers/Nationwide/Hartford/AAA sit at 1.3–1.4, versus Amica/Geico-Quote/Travelers-Core/Erie at 1.6–1.9 — plus one localized problem: in Tier 1, State Farm and Hartford audience CPLs ($115–140) exceed RPL. Governance implication for §4/§5: carrier-tiered tripwires scaled to margin buffer, a T1 ad-group audience bid-down for SF/Hartford, and a watch-list on Geico-Core (ROAS 0.4, worst CvR translation, 3.1% vs 24.4%). Most other negative rows trace to the retired state campaigns and self-resolved at consolidation. Caveat: 13 weeks of certified data — pre-2026 carrier performance is unverifiable, not disproven.

## C. RPL parity and tier transfer — confirmed

30 states qualify (≥15 leads/cell): median audience-vs-search RPL delta **−5.3%**, 57% of states slightly negative, 46.7% within ±15% — consistent with the account-level −7% (Jul: audience $57.7 vs search $61.8). The earlier "+60% above" reading was an artifact of unwindowed value data and is retired. Verdict for §4: the state tier model transfers directionally; dispersion at the edges (NJ +74%, SC −32%) supports the already-planned audience-specific tier re-cut at quarter-end (§7).

## D. The GP-decline claim — replace, with the consolidation caveat

The largest recent WoW decline (6/15→6/22, −47%) decomposes to search −$4,682 vs audience **+$390** — audience cushioned it. But the episode is confounded: it sits directly in the post-consolidation window (T1/T2 launch surge normalizing from the 6/08 peak) and overlaps the retirement of the remaining state campaigns (IL and NJ dark from 6/15; KY, MA, NC, VA shortly after). Change events show budgets and tCPAs being *raised* through the window, ruling out self-inflicted throttling — but "market demand fell" is not clean either. §2 should present late June as: consolidation transition + genuine softness, during which audience demonstrated the persistence pattern. Do not carry the original "~19%/97% audience" figures; they are not reproducible in the certified window.

## E. The tCPA blind spot — mechanism intact, direction updated

Audience RPL dispersion across ad-group types is real: $43.85 (affinity) to $61.14 (conquest), a ~$17/lead spread that lead-count optimization prices identically. The direction reversed from the original claim — conquest audience is currently the premium population, not the penalized one — which strengthens rather than weakens the structural argument: tCPA is blind to value differences in *either* direction, so it under-buys today's premium populations just as it over-bought yesterday's weak ones. Structure (tier × type separation) is what lets bidding see value; that is §4's justification, stated without depending on which bucket happens to be penalized this quarter.

---

## Outstanding

1. Architect backlog (non-blocking): document `campaign_performance_network_daily` as audience-only by design; note 2025 revenue is permanently absent (platform-side), so historical GP claims should be retired from institutional memory rather than awaiting backfill.
