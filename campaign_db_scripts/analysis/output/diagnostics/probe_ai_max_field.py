#!/usr/bin/env python
"""
probe_ai_max_field.py -- asks the Google Ads API (your installed version) which
AI-Max-related fields exist and whether they're selectable on campaign. No
account data touched; this queries field METADATA only.

    python probe_ai_max_field.py
"""
from dotenv import load_dotenv
load_dotenv()
from google.ads.googleads.client import GoogleAdsClient

client = GoogleAdsClient.load_from_storage()
svc = client.get_service("GoogleAdsFieldService")

for term in ("ai_max", "keywordless", "search_term_matching", "asset_automation"):
    q = ("SELECT name, category, selectable, filterable, data_type "
         "WHERE name LIKE '%%%s%%'" % term)
    print("=== fields matching '%s' ===" % term)
    try:
        results = list(svc.search_google_ads_fields(query=q))
        if not results:
            print("   (none)")
        for f in results:
            print("   %-58s %s selectable=%s type=%s"
                  % (f.name, f.category.name, f.selectable, f.data_type.name))
    except Exception as ex:
        print("   ERROR:", ex)
    print()
