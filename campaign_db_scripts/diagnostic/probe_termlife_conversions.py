#!/usr/bin/env python
"""
probe_termlife_conversions.py -- READ-ONLY (API + DB). Lists Term Life's
(customer 1671155176) conversion actions so Gilbert can rule each one
primary / secondary_count / excluded before any keyword sync routes for
acct 29. Mirrors the map structure conversion_actions expects.

    python probe_termlife_conversions.py
"""
from dotenv import load_dotenv
load_dotenv()
from google.ads.googleads.client import GoogleAdsClient

CUSTOMER_ID = "1671155176"

client = GoogleAdsClient.load_from_storage()
ga = client.get_service("GoogleAdsService")

print("Term Life conversion actions (rule each: primary / secondary_count / excluded):")
print("%-6s %-44s %-10s %-22s %-8s %s" % ("id", "name", "status", "type", "in_conv", "counting"))
rows = 0
for batch in ga.search_stream(customer_id=CUSTOMER_ID, query="""
        SELECT conversion_action.id,
               conversion_action.name,
               conversion_action.status,
               conversion_action.type,
               conversion_action.include_in_conversions_metric,
               conversion_action.counting_type,
               conversion_action.category
        FROM conversion_action
        ORDER BY conversion_action.id"""):
    for r in batch.results:
        ca = r.conversion_action
        print("%-6s %-44s %-10s %-22s %-8s %s | %s"
              % (ca.id, ca.name[:44], ca.status.name, ca.type_.name,
                 "Y" if ca.include_in_conversions_metric else "N",
                 ca.counting_type.name, ca.category.name))
        rows += 1
print("\n%d actions. 'in_conv=Y' = counts in Google's native Conversions column" % rows)
print("(what Smart Bidding optimizes to) -- informative for the ruling, not binding.")
print("Reply with per-action rulings; the seed script follows.")
