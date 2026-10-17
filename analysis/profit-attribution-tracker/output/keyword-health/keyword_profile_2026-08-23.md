# Keyword profile — base / incremental / probes
**Data through 2026-08-23** · sources: keyword_health_detail_2026-08-23.csv · marginal_roas_map_2026-08-23.csv · dormant_winners_2026-08-23.csv

## BASE — carries the P&L today: hold and contain
- Carrier terms: **73% of revenue, 86% of GP** ($1,746/d rev, $240/d GP @ 1.16 ROAS — account $2,383/d / $278/d @ 1.13; 51.6 conv/d).
- HEALTHY generic home-intent kernel: **22 rows, $68/d GP @ 1.41 ROAS** (inside a $509/d, $9/d-GP generic home-intent mass) — the rebuild nucleus.
- Protected Farmers home-intent exposure bet: 12 not-healthy rows, $80/d spend, -34/d GP — held by standing rule.
- Ad-group split (authoritative: any 'Non-Generic'/'NonGeneric' variant = branded; last 14d/day): BRANDED $2,215/d rev, $263/d GP @ 1.13 ROAS · GENERIC $471/d rev, $-18/d GP @ 0.96 ROAS.
- Classifier check: 118 rows ($257/d spend) where the text-based segment disagrees with the ad-group class — the ad-group class is authoritative for structure decisions.

Top BRANDED rows (hold; containment targets):

| Keyword | Match | Camp | Ad group | Class | ROAS | KW QS14d | AG iwQS | AG ROAS | GP/d | Spend/d |
|---|---|---|---|---|---:|---:|---:|---:|---:|---:|
| kin insurance | EXACT | TIER2 | Home-NonGeneric-Kin Insura | HEALTHY | 1.70 | 4.1 | 4.9 | 1.10 | $73 | $105 |
| farmers insurance | EXACT | TIER3 | Home-NonGeneric-Farmers In* | HEALTHY | 1.59 | 3.0 | 4.8 | 0.96 | $36 | $61 |
| kin insurance | EXACT | TEXAS | Home-NonGeneric-Kin Insura | HEALTHY | 1.76 | 3.5 | 4.9 | 1.10 | $21 | $27 |
| bamboo home insurance | EXACT | CALIFORNIA | Home-Non-Generic-Home_CaRe | HEALTHY | 1.38 | 5.0 | 5.0 | 1.66 | $16 | $41 |
| geico | BROAD | TIER1 | Home-NonGeneric-Geico-Core | HEALTHY | 2.23 | 1.2 | 2.7 | 1.22 | $15 | $12 |
| farmers insurance | EXACT | TIER1 | Home-NonGeneric-Farmers In* | HEALTHY | 1.15 | 3.0 | 4.8 | 0.96 | $14 | $92 |
| geico | BROAD | TIER2 | Home-NonGeneric-Geico-Core | HEALTHY | 1.58 | 3.0 | 2.7 | 1.22 | $11 | $19 |
| hartford insurance | EXACT | TIER1 | Home-NonGeneric-Hartford I | HEALTHY | 2.05 | 3.0 | 4.0 | 1.21 | $11 | $10 |

Top GENERIC rows (the rebuild book — keyword QS vs its ad group's iwQS shows who is dragging whom):

| Keyword | Match | Camp | Ad group | Class | ROAS | KW QS14d | AG iwQS | AG ROAS | GP/d | Spend/d |
|---|---|---|---|---|---:|---:|---:|---:|---:|---:|
| fire insurance | BROAD | TIER3 | Home-Generic-Fire-Core | HEALTHY | 2.25 | 5.8 | 7.6 | 1.32 | $9 | $7 |
| home insurance quotes | EXACT | TEXAS | Home-Generic-Home Insuranc | HEALTHY | 2.48 | 8.0 | 7.2 | 0.84 | $8 | $5 |
| home insurance estimate | PHRASE | CALIFORNIA | Home-Generic-Cost-Core | HEALTHY | 2.67† | 7.0 | 6.9 | 1.22 | $6 | $4 |
| fire insurance california | BROAD | CALIFORNIA | Home-Generic-Fire-Core | HEALTHY | 1.24 | 8.0 | 7.6 | 1.32 | $5 | $21 |
| fire insurance in california | EXACT | CALIFORNIA | Home-Generic-Fire-Core | HEALTHY | 2.01† | 8.0 | 7.6 | 1.32 | $4 | $4 |
| home insurance quotes | BROAD | TEXAS | Home-Generic-Home Insuranc | HEALTHY | 1.68 | 7.8 | 7.2 | 0.84 | $4 | $6 |
| homeowners insurance | EXACT | CALIFORNIA | Home-Generic-Home Insuranc | UNRATED | 3.79† | 7.0 | 7.2 | 0.84 | $4 | $1 |
| home insurance cost | PHRASE | TIER3 | Home-Generic-Cost-Core | UNRATED | 3.95† | 5.0 | 6.9 | 1.22 | $4 | $1 |

*QS columns impression-weighted over the last 14d ('—' = no rated impressions; QS never summed, unrated excluded). Keyword ROAS = recent 28d run-rate († = spend under $5/d — thin, read with care); AG ROAS = last 14d. Ad group = dominant by impressions; '*' = keyword spans multiple ad groups.*

## INCREMENTAL — measured margin, capacity-capped
- Funding: S1 cleanup frees **$411/day** (91 unprotected UNHEALTHY rows; removing them adds $171/d GP before redeploy).

| Campaign | Segment | Pooled inc ROAS | Δspend absorbed | Cap $/day | Episodes |
|---|---|---:|---:|---:|---:|
| TIER2 | Travelers | 2.29 | $385 | $32 | 1 |
| TIER1 | Hartford | 1.87 | $317 | $26 | 1 |
| TIER1 | Farmers | 1.72 | $465 | $39 | 1 |
| TIER2 | Costco | 1.63 | $862 | $72 | 1 |
| TIER2 | Hartford | 1.40 | $767 | $64 | 1 |
| ACCOUNT | Geico | 1.19 | $702 | $176 | 1 |
| TEXAS | Costco | 1.08 | $759 | $84 | 1 |
| CALIFORNIA | Kin | 1.08 | $454 | $50 | 1 |
| TIER2 | Kin | 1.07 | $963 | $80 | 1 |
| CALIFORNIA | Other/generic | 1.01 | $1,512 | $115 | 2 |

*Classes rest on 1–2 episodes until the wave-close rerun; quote as provisional.*

## PROBES — test before money
- UNTESTED segments (never absorbed a qualifying expansion): CALIFORNIA: AAA, Amica, Farmers, Geico, Hartford, State Farm, Travelers · FLORIDA: AAA, Amica, Costco, Farmers, Geico, Hartford, Other/generic, State Farm, Travelers · TEXAS: AAA, Amica, Geico, Hartford, State Farm, Travelers · TIER1: AAA, Amica, Costco, Geico, Other/generic, State Farm, Travelers · TIER2: AAA, Amica, Farmers excl. flagship, State Farm · TIER3: AAA, Amica, Farmers excl. flagship, Geico, Hartford, Kin, State Farm, Travelers
- Top biddable MISSING re-adds (2025-era GP; modification-grade): state farm ($10,681 @ 1.32), nationwide ($6,330 @ 1.28), state farm insurance ($5,556 @ 1.33), geico insurance ($5,223 @ 1.22), hippo insurance ($5,166 @ 1.31), tgs insurance ($4,810 @ 1.32), insurance quotes ($4,188 @ 1.23), hartford insurance ($3,617 @ 1.18)
- PAUSED winners (toggle-grade after ledger check): costco homeowners insurance ($5,105), costco condo insurance ($290), costco home insurance quote ($237)
- PRODUCT-ADJACENT (verify vertical fit first): geico landlord insurance ($2,687), geico condo insurance ($1,283), aaa home and auto insurance ($675), metlife home insurance ($290), geico home and auto quote ($228)

*Era caveat: 2025 GP sizes what was lost, not what resurrection earns. Under campaign tROAS none of the incremental/probe rows can be force-fed — allocation control (negatives / ad-group structure / targets) is the enabling condition.*