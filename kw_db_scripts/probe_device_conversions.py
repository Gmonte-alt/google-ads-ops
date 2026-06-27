"""
probe_device_conversions.py  — click-date device×action feasibility + reconciliation
=====================================================================================
ONE campaign, a few days. Answers, before we touch the loader:

  (Q2) Does keyword_view accept device × conversion_action on CLICK-DATE metrics
       (metrics.all_conversions / _value)?  If it errors, you'll see the API
       message verbatim (it should NOT — click-date avoids by_conversion_date's
       segmentation limits).
  (1)  CROSS-DEVICE LEAKAGE: device-summed primary vs the click-date ALL total
       (unsegmented). Google puts cross-device / UNKNOWN conversions in the total
       but not in any single device, so device-sum will run a few % under. This is
       the "complete" gap you keep even on Path B.
  (2)  ATTRIBUTION GAP: click-date ALL primary vs the CONVERSION-DATE ALL primary
       already stored in kpd for the same window. This is what switching kpd to
       click-date (Path B) would change in your historical numbers.

Routing mirrors 03 exactly: conversion_action resource → name → conversion_actions
.include_mode → primary feeds primary_conv_*.

READ-ONLY against the DB (only reads kpd for the comparison). Hits the live API.

    conda activate paid-search
    python probe_device_conversions.py --campaign-id <TIER_ONE_ID>
    python probe_device_conversions.py --campaign-id <ID> --start 2026-04-07 --end 2026-04-13
"""

import argparse
import os
import sqlite3
import sys
from pathlib import Path

from dotenv import load_dotenv
from google.ads.googleads.client import GoogleAdsClient
from google.ads.googleads.errors import GoogleAdsException

load_dotenv()
os.environ["GOOGLE_ADS_CONFIGURATION_FILE_PATH"] = os.getenv("GOOGLE_ADS_CONFIGURATION_FILE_PATH")

DB_PATH = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")
HOME_PLATFORM_ACCOUNT_ID = "8886502147"   # Google Home
ACCOUNT_ID = 1


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--customer-id", default=os.getenv("GOOGLE_ADS_CUSTOMER_ID_HOME"),
                   help="Google Ads customer id for Home (defaults to env).")
    p.add_argument("--campaign-id", required=True, help="Campaign to probe (e.g. Tier One).")
    p.add_argument("--start", default="2026-04-07", help="Settled window start (default a settled April week).")
    p.add_argument("--end",   default="2026-04-13")
    return p.parse_args()


def conv_map(cursor):
    rows = cursor.execute(
        "SELECT LOWER(conversion_name) AS name, include_mode FROM conversion_actions WHERE account_id=?",
        (ACCOUNT_ID,)).fetchall()
    return {r["name"]: r["include_mode"] for r in rows}


def action_name(resource, cache, svc, cid):
    if resource in cache:
        return cache[resource]
    aid = resource.split("/")[-1]
    name = f"unknown_{aid}"
    try:
        q = f"SELECT conversion_action.name FROM conversion_action WHERE conversion_action.id = {aid}"
        for batch in svc.search_stream(customer_id=cid, query=q):
            for row in batch.results:
                name = row.conversion_action.name
                break
    except Exception:
        pass
    cache[resource] = name
    return name


def fetch(svc, cid, query, label):
    """Returns (rows, error_message_or_None)."""
    out = []
    try:
        for batch in svc.search_stream(customer_id=cid, query=query):
            for row in batch.results:
                out.append(row)
        return out, None
    except GoogleAdsException as ex:
        msgs = "; ".join(f"[{e.error_code}] {e.message}" for e in ex.failure.errors)
        return None, msgs


def main():
    a = parse_args()
    if not a.customer_id:
        sys.exit("No customer id. Pass --customer-id or set GOOGLE_ADS_CUSTOMER_ID_HOME.")
    cid = str(a.customer_id).replace("-", "")

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cmap = conv_map(conn.cursor())

    client = GoogleAdsClient.load_from_storage()
    svc = client.get_service("GoogleAdsService")
    cache = {}

    base_where = f"""
        segments.date BETWEEN '{a.start}' AND '{a.end}'
        AND campaign.id = {a.campaign_id}
        AND ad_group_criterion.type = 'KEYWORD'
        AND ad_group_criterion.status IN ('ENABLED','PAUSED')
        AND campaign.advertising_channel_type = 'SEARCH'
        AND segments.ad_network_type IN ('SEARCH','SEARCH_PARTNERS')
    """

    print("=" * 72)
    print(f"  Device×action click-date probe — campaign {a.campaign_id}")
    print(f"  window {a.start} → {a.end}  (settled, so gaps are real, not lag)")
    print("=" * 72)

    # ── Q2 + (1): device × action, CLICK-DATE ────────────────────────────────
    q_dev = f"""
        SELECT segments.date, segments.device, segments.ad_network_type,
               segments.conversion_action,
               metrics.all_conversions, metrics.all_conversions_value
        FROM keyword_view WHERE {base_where}
    """
    dev_rows, err = fetch(svc, cid, q_dev, "device×action click-date")
    print("\n  Q2 — does device × conversion_action on click-date work?")
    if err:
        print(f"     ✗ REJECTED: {err}")
        print("     => click-date does NOT avoid the limit here. Fall back to plan B")
        print("        in chat (per-device separate from per-action) — tell the architect.")
        conn.close()
        return
    print(f"     ✓ ACCEPTED — {len(dev_rows):,} rows returned.")

    by_device = {}
    for r in dev_rows:
        mode = cmap.get(action_name(r.segments.conversion_action, cache, svc, cid).lower(), "monitor")
        if mode != "primary":
            continue
        d = r.segments.device.name
        agg = by_device.setdefault(d, {"conv": 0.0, "val": 0.0})
        agg["conv"] += r.metrics.all_conversions
        agg["val"]  += r.metrics.all_conversions_value

    dev_sum_conv = sum(v["conv"] for v in by_device.values())
    dev_sum_val  = sum(v["val"]  for v in by_device.values())

    print("\n  Primary conversions by device (click-date):")
    for d, v in sorted(by_device.items(), key=lambda kv: -kv[1]["conv"]):
        print(f"     {d:<14} conv={v['conv']:>8.1f}  value=${v['val']:>12,.2f}")
    print(f"     {'DEVICE SUM':<14} conv={dev_sum_conv:>8.1f}  value=${dev_sum_val:>12,.2f}")

    # ── (1) cross-device leakage: unsegmented click-date ALL ──────────────────
    q_all = f"""
        SELECT segments.date, segments.ad_network_type, segments.conversion_action,
               metrics.all_conversions, metrics.all_conversions_value
        FROM keyword_view WHERE {base_where}
    """
    all_rows, err2 = fetch(svc, cid, q_all, "unsegmented click-date")
    cd_conv = cd_val = 0.0
    if not err2:
        for r in all_rows:
            mode = cmap.get(action_name(r.segments.conversion_action, cache, svc, cid).lower(), "monitor")
            if mode == "primary":
                cd_conv += r.metrics.all_conversions
                cd_val  += r.metrics.all_conversions_value
    print(f"\n  (1) Cross-device leakage — device-sum vs click-date ALL:")
    print(f"     click-date ALL (unsegmented): conv={cd_conv:>8.1f}  value=${cd_val:>12,.2f}")
    if cd_conv:
        print(f"     device-sum is {100*(dev_sum_conv-cd_conv)/cd_conv:+.1f}% vs ALL on conv "
              f"(expect slightly negative — cross-device/UNKNOWN lives only in ALL)")

    # ── (2) attribution gap: stored conversion-date ALL in kpd ────────────────
    stored = conn.execute("""
        SELECT SUM(kpd.primary_conv_count) AS conv, SUM(kpd.primary_conv_value) AS val
        FROM keyword_performance_daily kpd
        JOIN keyword_instances ki ON ki.id = kpd.instance_id
        WHERE ki.account_id=? AND kpd.campaign_id=? AND kpd.device='ALL'
          AND kpd.report_date BETWEEN ? AND ?
    """, (ACCOUNT_ID, str(a.campaign_id), a.start, a.end)).fetchone()
    sc, sv = stored["conv"] or 0, stored["val"] or 0
    print(f"\n  (2) Attribution gap — click-date ALL vs STORED conversion-date ALL (kpd):")
    print(f"     stored conv-date ALL: conv={sc:>8.1f}  value=${sv:>12,.2f}")
    if sc:
        print(f"     click-date is {100*(cd_conv-sc)/sc:+.1f}% on conv, "
              f"{100*(cd_val-sv)/sv:+.1f}% on value vs the stored conversion-date number")
        print("     => THIS is what Path B (switch kpd to click-date) shifts in history.")

    conn.close()
    print("\n" + "=" * 72)
    print("  Paste this back. If Q2 ✓ and gaps are small/expected, I'll write the")
    print("  v4 device-conversion pass + scoped backfill (Path A or B per your call).")
    print("=" * 72)


if __name__ == "__main__":
    main()
