"""
diag_fire_insurance_kw.py
==========================
Targeted diagnostic for the 'fire insurance' keyword aggregation issue.

Queries the DB directly to show exactly what data exists for
'fire insurance' BROAD and EXACT instances — bypassing all script
aggregation logic so we can verify ground-truth numbers before
the full keyword analysis script runs.

Compares against known UI values:
  fire insurance BROAD  — Tier Four:  ~$505  spend, 10 conversions
  fire insurance BROAD  — Tier Three: ~$387  spend

RUN
───
    conda activate paid-search
    python analysis/diag_fire_insurance_kw.py
    python analysis/diag_fire_insurance_kw.py --end-date 2026-04-14 --days 30
"""

import sqlite3
import argparse
from pathlib import Path
from datetime import date, timedelta

DB_PATH    = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")
ACCOUNT_ID = 1

ENABLED_CAMPAIGN_IDS = [
    "23385188622",
    "23394808297",
    "23394808300",
    "23428389081",
    "23496334752",
    "23530860942",
    "23586637678",
]


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def sep(label=""):
    w = 72
    if label:
        print(f"\n── {label} {'─' * (w - len(label) - 4)}")
    else:
        print("─" * w)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--end-date", default=None)
    p.add_argument("--days", type=int, default=30)
    p.add_argument("--keyword", default="fire insurance",
                   help="Keyword text to diagnose (default: 'fire insurance')")
    args = p.parse_args()

    end   = date.fromisoformat(args.end_date) if args.end_date \
            else date.today() - timedelta(days=1)
    start = end - timedelta(days=args.days - 1)
    kw    = args.keyword.lower().strip()

    conn  = get_conn()
    ph    = ",".join("?" * len(ENABLED_CAMPAIGN_IDS))

    print("=" * 72)
    print(f"diag_fire_insurance_kw.py")
    print(f"  Keyword  : '{kw}'")
    print(f"  Window   : {start} → {end}  ({args.days}d)")
    print(f"  DB       : {DB_PATH}")
    print("=" * 72)

    # ── 1. All keyword_instances for this keyword text ─────────────────────
    sep("1. keyword_instances — all instances matching keyword text")
    instances = conn.execute(f"""
        SELECT
            ki.id            AS instance_id,
            ki.criterion_id,
            ki.ad_group_id,
            ki.ad_group_name,
            ki.campaign_id,
            ki.campaign_name,
            ki.match_type,
            ki.status,
            ki_id.normalized_text,
            ki_id.match_type  AS identity_match_type
        FROM keyword_instances ki
        JOIN keyword_identities ki_id ON ki_id.id = ki.identity_id
        WHERE ki.account_id = ?
          AND LOWER(ki_id.normalized_text) = ?
        ORDER BY ki.campaign_id, ki.ad_group_name, ki.match_type
    """, (ACCOUNT_ID, kw)).fetchall()

    if not instances:
        print(f"  ✗ No keyword_instances found for '{kw}'")
        print("    Check the normalized_text — it may be stored differently.")
        # Show closest matches
        close = conn.execute("""
            SELECT DISTINCT ki_id.normalized_text
            FROM keyword_identities ki_id
            JOIN keyword_instances ki ON ki.identity_id = ki_id.id
            WHERE ki.account_id = ?
              AND ki_id.normalized_text LIKE ?
            LIMIT 10
        """, (ACCOUNT_ID, f"%{kw}%")).fetchall()
        if close:
            print(f"  Similar normalized_text values in DB:")
            for r in close:
                print(f"    '{r['normalized_text']}'")
        conn.close()
        return

    print(f"  Found {len(instances)} instance(s):\n")
    print(f"  {'iid':>8}  {'crit_id':>18}  {'match':>8}  "
          f"{'status':>8}  {'campaign':>50}  {'ad_group'}")
    print(f"  {'─'*8}  {'─'*18}  {'─'*8}  {'─'*8}  {'─'*50}  {'─'*30}")
    for r in instances:
        print(f"  {r['instance_id']:>8}  {str(r['criterion_id']):>18}  "
              f"{r['match_type']:>8}  {str(r['status'] or ''):>8}  "
              f"{str(r['campaign_name'] or '')[:50]:>50}  "
              f"{str(r['ad_group_name'] or '')[:30]}")

    # ── 2. Raw kpd rows for these instances — ALL filters ─────────────────
    sep("2. keyword_performance_daily — raw rows (device=ALL, geo_state=ALL)")
    iids = [r['instance_id'] for r in instances]
    ph_iids = ",".join("?" * len(iids))

    kpd_rows = conn.execute(f"""
        SELECT
            kpd.instance_id,
            kpd.report_date,
            kpd.device,
            kpd.geo_state,
            kpd.clicks,
            kpd.impressions,
            kpd.cost_micros / 1e6        AS spend,
            kpd.primary_conv_count       AS convs,
            kpd.primary_conv_value       AS revenue
        FROM keyword_performance_daily kpd
        WHERE kpd.instance_id IN ({ph_iids})
          AND kpd.report_date BETWEEN ? AND ?
          AND kpd.device    = 'ALL'
          AND kpd.geo_state = 'ALL'
        ORDER BY kpd.instance_id, kpd.report_date
    """, iids + [start.isoformat(), end.isoformat()]).fetchall()

    print(f"  {len(kpd_rows)} rows (device=ALL, geo_state=ALL)\n")
    if kpd_rows:
        print(f"  {'iid':>8}  {'date':>12}  {'clicks':>7}  "
              f"{'spend':>10}  {'convs':>7}  {'revenue':>10}")
        print(f"  {'─'*8}  {'─'*12}  {'─'*7}  {'─'*10}  {'─'*7}  {'─'*10}")
        for r in kpd_rows[:30]:   # cap at 30 rows for readability
            print(f"  {r['instance_id']:>8}  {r['report_date']:>12}  "
                  f"{r['clicks']:>7}  ${r['spend']:>9.2f}  "
                  f"{r['convs']:>7.1f}  ${r['revenue']:>9.2f}")
        if len(kpd_rows) > 30:
            print(f"  ... ({len(kpd_rows) - 30} more rows not shown)")

    # ── 3. Aggregated totals per instance — what fetch_keywords() returns ──
    sep("3. Aggregated totals per instance (what fetch_keywords sees)")
    agg = conn.execute(f"""
        SELECT
            kpd.instance_id,
            SUM(kpd.clicks)                AS clicks,
            SUM(kpd.impressions)           AS impressions,
            SUM(kpd.cost_micros) / 1e6     AS spend,
            SUM(kpd.primary_conv_count)    AS convs,
            SUM(kpd.primary_conv_value)    AS revenue
        FROM keyword_performance_daily kpd
        WHERE kpd.instance_id IN ({ph_iids})
          AND kpd.report_date BETWEEN ? AND ?
          AND kpd.device    = 'ALL'
          AND kpd.geo_state = 'ALL'
        GROUP BY kpd.instance_id
        ORDER BY kpd.instance_id
    """, iids + [start.isoformat(), end.isoformat()]).fetchall()

    print(f"\n  {'iid':>8}  {'match':>8}  {'campaign':>48}  "
          f"{'clicks':>7}  {'spend':>10}  {'convs':>7}  {'revenue':>10}  {'ROAS':>6}")
    print(f"  {'─'*8}  {'─'*8}  {'─'*48}  {'─'*7}  {'─'*10}  {'─'*7}  {'─'*10}  {'─'*6}")

    inst_map = {r['instance_id']: r for r in instances}
    for r in agg:
        meta  = inst_map.get(r['instance_id'], {})
        roas  = r['revenue'] / r['spend'] if r['spend'] > 0 else None
        roas_str = f"{roas:.3f}" if roas else "n/a"
        cname = str(meta.get('campaign_name') or '')
        # Shorten campaign name for display
        cname = cname.replace("HOME INSURANCE-", "").replace("-NB-SEARCH-ALL-USA-STE", "")
        print(f"  {r['instance_id']:>8}  "
              f"{str(meta.get('match_type') or ''):>8}  "
              f"{cname:>48}  "
              f"{int(r['clicks']):>7}  "
              f"${r['spend']:>9.2f}  "
              f"{r['convs']:>7.1f}  "
              f"${r['revenue']:>9.2f}  "
              f"{roas_str:>6}")

    # ── 4. WITHOUT geo_state filter — shows the overcounting bug if present ─
    sep("4. WITHOUT geo_state filter (reveals overcounting if bug still present)")
    agg_no_geo = conn.execute(f"""
        SELECT
            kpd.instance_id,
            SUM(kpd.clicks)                AS clicks,
            SUM(kpd.cost_micros) / 1e6     AS spend,
            SUM(kpd.primary_conv_count)    AS convs,
            SUM(kpd.primary_conv_value)    AS revenue,
            COUNT(DISTINCT kpd.geo_state)  AS geo_states
        FROM keyword_performance_daily kpd
        WHERE kpd.instance_id IN ({ph_iids})
          AND kpd.report_date BETWEEN ? AND ?
          AND kpd.device = 'ALL'
        GROUP BY kpd.instance_id
        ORDER BY kpd.instance_id
    """, iids + [start.isoformat(), end.isoformat()]).fetchall()

    print(f"\n  (If geo_states > 1, the bug would cause overcounting)\n")
    print(f"  {'iid':>8}  {'match':>8}  {'clicks':>7}  {'spend':>10}  "
          f"{'convs':>7}  {'revenue':>10}  {'geo_states':>10}")
    print(f"  {'─'*8}  {'─'*8}  {'─'*7}  {'─'*10}  {'─'*7}  {'─'*10}  {'─'*10}")
    for r in agg_no_geo:
        meta = inst_map.get(r['instance_id'], {})
        print(f"  {r['instance_id']:>8}  "
              f"{str(meta.get('match_type') or ''):>8}  "
              f"{int(r['clicks']):>7}  "
              f"${r['spend']:>9.2f}  "
              f"{r['convs']:>7.1f}  "
              f"${r['revenue']:>9.2f}  "
              f"{r['geo_states']:>10}")

    # ── 5. Cross-check: does any instance share a criterion_id? ───────────
    sep("5. criterion_id uniqueness check (detects instance collision)")
    crit_ids = [r['criterion_id'] for r in instances]
    ph_crits = ",".join("?" * len(crit_ids))
    shared = conn.execute(f"""
        SELECT criterion_id, COUNT(*) AS n, GROUP_CONCAT(id) AS instance_ids,
               GROUP_CONCAT(match_type) AS match_types
        FROM keyword_instances
        WHERE account_id = ?
          AND criterion_id IN ({ph_crits})
        GROUP BY criterion_id
        HAVING COUNT(*) > 1
    """, [ACCOUNT_ID] + crit_ids).fetchall()

    if shared:
        print(f"\n  ⚠  {len(shared)} criterion_id(s) shared across multiple instances:")
        for r in shared:
            print(f"     criterion_id={r['criterion_id']}  "
                  f"instances={r['instance_ids']}  match_types={r['match_types']}")
        print("\n  This means BROAD and EXACT share a criterion_id.")
        print("  If the script resolves criterion_id → instance_id using")
        print("  drop_duplicates, it will pick one and misattribute the other's data.")
    else:
        print(f"\n  ✓ All criterion_ids are unique — no instance collision via criterion_id")

    # ── 6. Summary verdict ─────────────────────────────────────────────────
    sep("6. VERDICT")
    total_broad_spend   = sum(r['spend']   for r in agg
                              if inst_map.get(r['instance_id'], {}).get('match_type') == 'BROAD')
    total_broad_revenue = sum(r['revenue'] for r in agg
                              if inst_map.get(r['instance_id'], {}).get('match_type') == 'BROAD')
    total_broad_convs   = sum(r['convs']   for r in agg
                              if inst_map.get(r['instance_id'], {}).get('match_type') == 'BROAD')
    total_exact_spend   = sum(r['spend']   for r in agg
                              if inst_map.get(r['instance_id'], {}).get('match_type') == 'EXACT')
    total_exact_revenue = sum(r['revenue'] for r in agg
                              if inst_map.get(r['instance_id'], {}).get('match_type') == 'EXACT')

    print(f"\n  BROAD total — spend: ${total_broad_spend:,.2f}  "
          f"revenue: ${total_broad_revenue:,.2f}  convs: {total_broad_convs:.1f}")
    print(f"  EXACT total — spend: ${total_exact_spend:,.2f}  "
          f"revenue: ${total_exact_revenue:,.2f}")
    print(f"\n  Expected (from UI screenshot):")
    print(f"    BROAD Tier Four:  ~$505 spend")
    print(f"    BROAD Tier Three: ~$387 spend")
    print(f"    BROAD combined:   ~$893 spend")

    if total_broad_spend < 100:
        print(f"\n  ✗ BROAD spend is suspiciously low (${total_broad_spend:.2f}).")
        print("    Revenue/conversions are likely still routing to EXACT instances.")
        print("    Check section 5 above for criterion_id collisions.")
    elif abs(total_broad_spend - 893) < 50:
        print(f"\n  ✓ BROAD spend (~${total_broad_spend:.0f}) is close to UI expected (~$893)")
        print("    If revenue is still wrong, the issue is in the script's merge logic,")
        print("    not in the raw DB data.")
    else:
        print(f"\n  ℹ  BROAD spend ${total_broad_spend:.2f} — compare manually to UI.")

    conn.close()
    print("\nDone.")


if __name__ == "__main__":
    main()
