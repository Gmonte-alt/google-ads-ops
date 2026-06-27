"""
seed_auto_routing.py
--------------------
Seeds conversion_actions rows for the Auto MCC (autoinsurance.org) and
Auto Quote standalone (auto.quote.com) accounts based on live conversion
action names captured from the Google Ads UI on 2026-04-27.

Idempotent — uses INSERT OR IGNORE. Re-running will not duplicate rows.

After seeding, re-run:
    python kw_db_scripts/03_sync_keyword_performance.py --account auto       --refresh 60
    python kw_db_scripts/03_sync_keyword_performance.py --account auto_quote --refresh 60

The --refresh 60 will re-pull the last 60 days from the API and overwrite
keyword_performance_daily rows with correctly-routed primary_conv_value.
Older rows (>60 days) will retain $0 primary_conv_value until separately
refreshed.
"""

import sqlite3
from pathlib import Path

DB_PATH = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")

# Routing decisions captured from Google Ads UI screenshots on 2026-04-27.
# Names must match Google Ads exactly (case-insensitive at routing time).
SEEDS = {
    # Auto MCC — autoinsurance.org — platform_account_id 3863660890
    "3863660890": [
        ("QW Auto Offline Conversions", "primary",
         "Main revenue signal. $610,457.92 / 30d (Mar 25 - Apr 23 2026). "
         "QW offline upload pipeline. Active."),
        ("SF Auto Offline Conversions", "primary",
         "Dormant in last 30d but classified Primary by Google. Seeded primary "
         "so revenue routes correctly when/if it resumes firing."),
        ("EQ Conversions with Value", "monitor",
         "Originally documented as primary in our routing plan, but Google UI "
         "classifies it as Secondary and it is near-dormant. Routing to monitor "
         "until EverQuote integration status is reconfirmed."),
        ("Zaiper test for auto", "excluded",
         "Old Zapier test action confirmed by user 2026-04-27. No real revenue."),
        ("AutoInsurance.org - Google Ads Click - Zip Code Submit",
         "secondary_count",
         "Top-of-funnel zip submit. 20,212 conversions / 30d at $0 value. "
         "Count-only signal."),
        ("QW - Submit Lead", "excluded",
         "Top-of-funnel lead form. 10,275 conversions / 30d at ~$0 value. "
         "Not revenue — exclude from primary and all_conv totals."),
        ("EverQuote Completion (External)", "excluded",
         "Dormant external action. Excluded per routing plan."),
        ("YouTube channel subscriptions", "excluded",
         "YouTube engagement signal, not revenue. Excluded per routing plan."),
    ],

    # Auto Quote standalone — auto.quote.com — platform_account_id 1006729211
    "1006729211": [
        ("QW Offline Conversions", "primary",
         "Main revenue signal. $1,020,527.93 / 30d (Mar 25 - Apr 23 2026). "
         "QW offline upload pipeline. Active."),
        ("RB-Ringba - Auto Calls - Quote", "primary",
         "Call-based revenue. $2,899.32 / 30d. NEWLY DOCUMENTED 2026-04-27. "
         "OPEN QUESTION: verify this is distinct from QW Offline Conversions "
         "and not double-counting the same lead through different pipelines."),
        ("Submit lead form", "secondary_count",
         "Top-of-funnel lead form. 16,927 conversions / 30d at $0 value. "
         "Count-only signal."),
    ],
}


def main():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    print("=" * 78)
    print("Seeding conversion_actions for auto and auto_quote")
    print(f"DB: {DB_PATH}")
    print("=" * 78)

    grand_total = {"inserted": 0, "skipped": 0}

    for platform_account_id, actions in SEEDS.items():
        # Resolve account_id
        row = cursor.execute(
            "SELECT id, account_name FROM accounts "
            "WHERE platform_account_id = ? AND platform = 'google'",
            (platform_account_id,),
        ).fetchone()
        if not row:
            print(f"\n  SKIP — account {platform_account_id} not in DB")
            continue

        account_id   = row["id"]
        account_name = row["account_name"]
        print(f"\n── {account_name} (id={account_id}, "
              f"platform_account_id={platform_account_id}) ──")

        local = {"inserted": 0, "skipped": 0}
        for conv_name, include_mode, notes in actions:
            existing = cursor.execute(
                "SELECT id FROM conversion_actions "
                "WHERE account_id = ? AND conversion_name = ?",
                (account_id, conv_name),
            ).fetchone()
            if existing:
                print(f"  [skip — already seeded]  {conv_name}")
                local["skipped"] += 1
                continue
            cursor.execute(
                """
                INSERT INTO conversion_actions
                    (account_id, conversion_name, include_mode, notes)
                VALUES (?, ?, ?, ?)
                """,
                (account_id, conv_name, include_mode, notes),
            )
            print(f"  [{include_mode:<16}]  {conv_name}")
            local["inserted"] += 1

        print(f"  → {local['inserted']} inserted, {local['skipped']} skipped")
        grand_total["inserted"] += local["inserted"]
        grand_total["skipped"]  += local["skipped"]

    conn.commit()
    conn.close()

    print("\n" + "=" * 78)
    print(f"Total: {grand_total['inserted']} inserted, "
          f"{grand_total['skipped']} skipped")
    print("=" * 78)
    print("\nNext steps:")
    print("  python kw_db_scripts\\03_sync_keyword_performance.py "
          "--account auto       --refresh 60")
    print("  python kw_db_scripts\\03_sync_keyword_performance.py "
          "--account auto_quote --refresh 60")
    print("\nThen re-run diagnose_auto_routing.py to confirm primary_conv_value")
    print("is no longer near-zero for both accounts.")


if __name__ == "__main__":
    main()