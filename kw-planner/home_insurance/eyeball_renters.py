import sqlite3, tag_products_demand_research as tp

DEMAND = r"C:\Projects\google-ads-ops\db\demand_research.db"
REG    = r"C:\Projects\google-ads-ops\db\paid_search.db"

index = tp.load_product_index(REG)

conn = sqlite3.connect(DEMAND)
rows = conn.execute(
    "SELECT keyword_text, avg_monthly_searches FROM kwp_ideas "
    "WHERE vertical='home' GROUP BY keyword_text").fetchall()
conn.close()

hits = []
for kw, vol in rows:
    m = tp.match_product(kw, index)
    if m and m[0] == "renters":
        hits.append((vol or 0, kw, m[2]))   # m[2] = ambiguous flag

hits.sort(reverse=True)
print(f"{len(hits)} renters-tagged terms. Top 40 by volume:\n")
for vol, kw, amb in hits[:40]:
    flag = "  [ambiguous]" if amb else ""
    print(f"  {vol:>8,}  {kw}{flag}")