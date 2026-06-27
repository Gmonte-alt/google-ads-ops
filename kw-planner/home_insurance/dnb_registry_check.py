#!/usr/bin/env python
"""Pull the true do-not-bid brand set from the registry, so NEG_BRAND_DNB reflects registry
status (dnb_flag / brand_bid_status) -- not 'brands we didn't conquest'. Read-only."""
import argparse, sqlite3

def cols(c,t):
    try: return [r[1] for r in c.execute("PRAGMA table_info(%s)"%t)]
    except sqlite3.OperationalError: return []

def main():
    p=argparse.ArgumentParser()
    p.add_argument("--demand", default=r"C:\Projects\google-ads-ops\db\demand_research.db")
    p.add_argument("--paid",   default=r"C:\Projects\google-ads-ops\db\paid_search.db")
    a=p.parse_args()

    # 1) bid status as the conquest seeding saw it (demand_research.kwp_ideas)
    print("=== demand_research.kwp_ideas: per-brand bid status (home vertical) ===")
    c=sqlite3.connect(a.demand)
    q="""SELECT brand, MAX(dnb_flag) AS dnb,
                GROUP_CONCAT(DISTINCT brand_bid_status) AS statuses,
                SUM(avg_monthly_searches) AS vol
         FROM kwp_ideas WHERE vertical='home' AND brand!=''
         GROUP BY brand ORDER BY brand"""
    rows=c.execute(q).fetchall(); c.close()
    OK={"approved","conditional"}
    dnb=[]; biddable=[]
    for brand,dnbf,statuses,vol in rows:
        sset={s.strip().lower() for s in (statuses or "").split(",") if s}
        is_dnb = (dnbf==1) or (not (sset & OK))
        (dnb if is_dnb else biddable).append((brand, dnbf, statuses, int(vol or 0)))
    print("total brands: %d | do-not-bid: %d | biddable: %d\n" % (len(rows),len(dnb),len(biddable)))
    print("DO-NOT-BID brands (dnb_flag=1 or status not approved/conditional):")
    for b,d,s,v in sorted(dnb,key=lambda x:-x[3]): print("   %-28s dnb=%s status=%-20s vol=%d"%(b,d,s,v))
    print("\nspot-check brands Gilbert flagged:")
    for name in ("citizens","erie","allstate","liberty","usaa"):
        hit=[r for r in rows if name in (r[0] or "").lower()]
        for brand,dnbf,statuses,vol in hit:
            print("   %-28s dnb=%s status=%-20s vol=%d"%(brand,dnbf,statuses,int(vol or 0)))

    # 2) cross-check brand_registry in paid_search.db (canonical registry)
    print("\n=== paid_search.brand_registry (canonical) ===")
    c=sqlite3.connect(a.paid); bc=cols(c,"brand_registry")
    print("columns:", bc)
    namecol=next((x for x in ("brand","brand_name","normalized","canonical","name") if x in bc),None)
    statuscol=next((x for x in ("brand_bid_status","bid_status","status") if x in bc),None)
    dnbcol=next((x for x in ("dnb_flag","dnb","do_not_bid") if x in bc),None)
    if namecol and (statuscol or dnbcol):
        sel=", ".join([x for x in (namecol,dnbcol,statuscol) if x])
        for r in c.execute("SELECT %s FROM brand_registry"%sel):
            pass  # (full dump available; summarized below)
        # list registry do-not-bid
        conds=[]
        if dnbcol: conds.append("%s=1"%dnbcol)
        if statuscol: conds.append("LOWER(%s) NOT IN ('approved','conditional')"%statuscol)
        where=" OR ".join(conds)
        print("registry do-not-bid brands:")
        for r in c.execute("SELECT %s FROM brand_registry WHERE %s ORDER BY %s"%(sel,where,namecol)):
            print("   ", r)
    else:
        print("(no bid-status/dnb columns found on brand_registry -- rely on demand_research above)")
    c.close()

if __name__ == "__main__":
    main()
