"""
Checks if Geico/AARP/AAA/Costco come back brand='' while state farm… comes back brand='State Farm', 
that's cause A confirmed airtight — same intent, brand token present, only the variant phrasing missing. 
(I added State Farm as the control row, which the planner's original query omitted — it's the row that distinguishes A from B.)
"""


import sqlite3
db = r"C:\Projects\google-ads-ops\db\demand_research.db"   # adjust path if different
c = sqlite3.connect(db); c.row_factory = sqlite3.Row
terms = ['geico home and auto insurance','aarp home and auto insurance',
         'aaa home and auto insurance','costco home and auto insurance',
         'state farm home and auto insurance']  # added State Farm as the control
q = "SELECT keyword_text, brand, vertical_match, brand_bid_status, dnb_flag FROM kwp_ideas WHERE keyword_text=? AND vertical='home'"
for t in terms:
    rows = c.execute(q, (t,)).fetchall()
    if not rows: print(f"{t!r}: (no row)")
    for r in rows: print(f"{t!r}: brand={r['brand']!r} vmatch={r['vertical_match']} bid={r['brand_bid_status']} dnb={r['dnb_flag']}")
c.close()