import sys
print("step 1: starting", flush=True)
try:
    import sqlite3
    print("step 2: sqlite3 ok", flush=True)
    import os, json, logging
    print("step 3: stdlib ok", flush=True)
    from pathlib import Path
    from datetime import date, datetime, timezone
    from dotenv import load_dotenv
    print("step 4: dotenv ok", flush=True)
    from google.ads.googleads.client import GoogleAdsClient
    print("step 5: google ads client ok", flush=True)
except Exception as e:
    print(f"FAILED at import: {type(e).__name__}: {e}", flush=True)
    sys.exit(1)

print("step 6: all imports ok — testing load_dotenv", flush=True)
try:
    load_dotenv()
    import os
    cids = [k for k in os.environ if k.startswith("GOOGLE_ADS_CUSTOMER_ID")]
    print(f"step 7: found {len(cids)} customer ID vars: {cids}", flush=True)
except Exception as e:
    print(f"FAILED at dotenv: {type(e).__name__}: {e}", flush=True)
    sys.exit(1)

print("step 8: testing GoogleAdsClient.load_from_storage()", flush=True)
try:
    client = GoogleAdsClient.load_from_storage()
    print("step 9: client loaded ok", flush=True)
except Exception as e:
    print(f"FAILED at client load: {type(e).__name__}: {e}", flush=True)
    sys.exit(1)

print("All checks passed.", flush=True)
