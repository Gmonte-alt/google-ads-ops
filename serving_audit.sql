-- Post-cutover serving audit (run daily for ~1 week after cutover)
-- Expect each committed state's spend concentrated ~100% in its destination
-- campaign within a few days; stragglers = a missed location change.
SELECT g.geo_state AS state,
       g.campaign_id,
       ROUND(SUM(g.cost_micros)/1e6, 2)                       AS spend,
       ROUND(100.0 * SUM(g.cost_micros)
             / SUM(SUM(g.cost_micros)) OVER (PARTITION BY g.geo_state), 1)
                                                              AS pct_of_state_spend
FROM geo_performance_daily g
WHERE g.account_id = 2 AND g.location_type = 'PHYSICAL' AND g.device = 'ALL'
  AND g.date >= '<CUTOVER_DATE>'
  AND g.network IN ('Microsoft sites and select traffic', 'AOL search')  -- match model's search networks
GROUP BY g.geo_state, g.campaign_id
HAVING SUM(g.cost_micros) > 0
ORDER BY g.geo_state, spend DESC;
