"""
07_seed_brand_registry_medicare_patch.py
------------------------------------------
Adds Medicare-specific carrier brands, sub-brands, and supplemental
keyword variants to brand_registry and brand_registry_keywords.

Run AFTER 07_seed_brand_registry.py has already populated the Home Insurance
brands. This script is additive -- uses INSERT OR REPLACE / INSERT OR IGNORE
throughout. Safe to re-run.

Medicare carrier universe:
    Already in brand_registry from 07_seed_brand_registry.py:
        Humana, Hartford, AARP, EverQuote, SelectQuote, Policygenius, Surebridge
    Added by this script:
        UnitedHealthcare/UHC, Aetna, Cigna, Wellcare, Anthem, BCBS, Kaiser,
        Molina, Oscar, Devoted, Clover, Mutual of Omaha, Transamerica,
        Bankers Life, Colonial Penn, Globe Life, SilverScript, GoHealth,
        HealthMarkets, eHealth, Boomer Benefits, Integrity Marketing, and more

Usage:
    conda activate paid-search
    python kw_db_scripts/07_seed_brand_registry_medicare_patch.py

Prerequisites:
    07_seed_brand_registry.py has been run successfully.
"""

import sqlite3
import re
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DB_PATH = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")

# ---------------------------------------------------------------------------
# Database connection
# ---------------------------------------------------------------------------

def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.row_factory = sqlite3.Row
    return conn


def normalize(text: str) -> str:
    return re.sub(r'\s+', ' ', text.lower().strip())


# ---------------------------------------------------------------------------
# Medicare-specific brand data
#
# Format: (brand_name, parent_brand, bid_status, restriction_reason,
#          restriction_source, effective_date, verticals, notes,
#          brand_category)
# ---------------------------------------------------------------------------

MEDICARE_BRAND_DATA = [

    # -----------------------------------------------------------------------
    # MAJOR NATIONAL MEDICARE CARRIERS
    # -----------------------------------------------------------------------

    ("UnitedHealthcare", None,
     "approved", None, None, None, "medicare,life",
     "Largest Medicare Advantage insurer by enrollment. Often marketed as UHC. "
     "AARP Medicare Supplement plans are underwritten by UHC -- handle both "
     "'aarp' and 'unitedhealthcare' queries as medicare-vertical. "
     "SureBridge is a UHC sub-brand (already in registry).",
     "carrier_approved"),

    ("UHC", "UnitedHealthcare",
     "approved", None, None, None, "medicare",
     "Short-form alias for UnitedHealthcare. 'uhc com', 'uhc medicare'.",
     "carrier_approved"),

    ("AARP Medicare", "UnitedHealthcare",
     "approved", None, None, None, "medicare",
     "AARP-branded Medicare Supplement plans underwritten by UHC. "
     "Queries: 'aarp medicare supplement', 'aarp medigap', "
     "'aarp medicare advantage'. High volume during AEP.",
     "carrier_approved"),

    ("Aetna", None,
     "approved", None, None, None, "medicare,life,auto,home",
     "Major national carrier across multiple verticals. "
     "Medicare Advantage and Medigap products. CVS Health parent since 2018. "
     "Queries: 'aetna medicare', 'aetna medicare advantage', 'aetna medigap'.",
     "carrier_approved"),

    ("Cigna", None,
     "approved", None, None, None, "medicare,life",
     "Major national carrier. Medicare Supplement and Medicare Advantage. "
     "Queries: 'cigna medicare', 'cigna medicare supplement', 'cigna medigap'.",
     "carrier_approved"),

    ("Wellcare", None,
     "approved", None, None, None, "medicare",
     "Medicare-focused carrier (Centene subsidiary). "
     "Medicare Advantage, Part D prescription plans. "
     "Queries: 'wellcare medicare', 'wellcare medicare advantage', 'wellcare part d'.",
     "carrier_approved"),

    ("Anthem", None,
     "approved", None, None, None, "medicare,auto,home,life",
     "Multi-vertical insurer, large Medicare Advantage footprint. BCBS licensee. "
     "Queries: 'anthem medicare', 'anthem medicare advantage', 'anthem blue cross medicare'.",
     "carrier_approved"),

    ("Blue Cross Blue Shield", None,
     "approved", None, None, None, "medicare,auto,home,life",
     "BCBS federation -- regional plans across all states. "
     "Strong Medicare Supplement and Medicare Advantage presence. "
     "Queries: 'blue cross medicare', 'bcbs medicare', 'blue cross medigap'.",
     "carrier_approved"),

    ("BCBS", "Blue Cross Blue Shield",
     "approved", None, None, None, "medicare,auto,home,life",
     "Short-form alias for Blue Cross Blue Shield.",
     "carrier_approved"),

    ("Kaiser Permanente", None,
     "approved", None, None, None, "medicare",
     "Integrated health plan, large Medicare Advantage enrollment. "
     "Regional focus: CA, CO, GA, HI, MD, OR, VA, WA. "
     "Queries: 'kaiser medicare', 'kaiser medicare advantage'.",
     "carrier_approved"),

    ("Molina Healthcare", None,
     "approved", None, None, None, "medicare",
     "Managed care organization, Medicare Advantage plans. "
     "Queries: 'molina medicare', 'molina healthcare medicare'.",
     "carrier_approved"),

    ("Centene", None,
     "approved", None, None, None, "medicare",
     "Parent company of Wellcare and other Medicare plans.",
     "carrier_approved"),

    ("Oscar Health", None,
     "approved", None, None, None, "medicare",
     "Technology-focused insurer with Medicare Advantage plans. "
     "Queries: 'oscar medicare', 'oscar health medicare'.",
     "carrier_approved"),

    ("Devoted Health", None,
     "approved", None, None, None, "medicare",
     "Medicare Advantage insurer (Medicare-only). Technology-focused. "
     "Queries: 'devoted health', 'devoted health medicare'.",
     "carrier_approved"),

    ("Clover Health", None,
     "approved", None, None, None, "medicare",
     "Medicare Advantage insurer, tech-forward. "
     "Queries: 'clover health', 'clover health medicare'.",
     "carrier_approved"),

    ("CVS Health", None,
     "approved", None, None, None, "medicare",
     "Parent of Aetna. Some searches use CVS Health as proxy for Aetna Medicare.",
     "carrier_approved"),

    ("SilverScript", "CVS Health",
     "approved", None, None, None, "medicare",
     "CVS Health Medicare Part D prescription drug plan brand. "
     "Queries: 'silverscript', 'silverscript part d', 'silverscript medicare'.",
     "carrier_approved"),

    ("Humana Gold Plus", "Humana",
     "approved", None, None, None, "medicare",
     "Humana Medicare Advantage sub-brand. "
     "Queries: 'humana gold plus', 'humana gold plus hmo'.",
     "carrier_approved"),

    ("Mutual of Omaha", None,
     "approved", None, None, None, "medicare,life",
     "Major Medicare Supplement (Medigap) carrier. Also life insurance. "
     "Queries: 'mutual of omaha medicare supplement', 'mutual of omaha medigap'.",
     "carrier_approved"),

    ("Transamerica", None,
     "approved", None, None, None, "medicare,life",
     "Medicare Supplement and life insurance carrier. "
     "Queries: 'transamerica medicare supplement', 'transamerica medigap'.",
     "carrier_approved"),

    ("Bankers Life", None,
     "approved", None, None, None, "medicare,life",
     "Medicare Supplement and life insurance. "
     "Queries: 'bankers life medicare', 'bankers life medigap'.",
     "carrier_approved"),

    ("Colonial Penn", None,
     "approved", None, None, None, "medicare,life",
     "Medicare Supplement and final expense life insurance. "
     "Heavy TV advertising drives branded search volume. "
     "Queries: 'colonial penn medicare', 'colonial penn life insurance'.",
     "carrier_approved"),

    ("American Continental", "Cigna",
     "approved", None, None, None, "medicare",
     "Cigna sub-brand for Medicare Supplement. "
     "Queries: 'american continental medicare supplement'.",
     "carrier_approved"),

    ("Globe Life", None,
     "approved", None, None, None, "medicare,life",
     "Medicare Supplement and life insurance carrier. "
     "Queries: 'globe life medicare supplement', 'globe life insurance'.",
     "carrier_approved"),

    # -----------------------------------------------------------------------
    # MEDICARE AGGREGATORS / LEAD BUYERS / MARKETPLACES
    # -----------------------------------------------------------------------

    ("GoHealth", None,
     "approved", None, None, None, "medicare",
     "Medicare insurance marketplace/aggregator. "
     "We sell Medicare leads to/alongside GoHealth. "
     "Queries: 'gohealth', 'gohealth medicare', 'go health insurance'.",
     "aggregator_lead_buyer"),

    ("HealthMarkets", None,
     "approved", None, None, None, "medicare,life,auto",
     "Insurance marketplace (UHC subsidiary). Medicare, life, and other products. "
     "Queries: 'healthmarkets', 'health markets medicare'.",
     "aggregator_lead_buyer"),

    ("eHealth", None,
     "approved", None, None, None, "medicare",
     "Online Medicare insurance marketplace. "
     "Queries: 'ehealth medicare', 'ehealth insurance', 'ehealthinsurance'.",
     "aggregator_competitor"),

    ("MedicareWire", None,
     "approved", None, None, None, "medicare",
     "Medicare information/comparison site. "
     "Queries: 'medicarewire', 'medicare wire'.",
     "aggregator_competitor"),

    ("Boomer Benefits", None,
     "approved", None, None, None, "medicare",
     "Medicare Supplement brokerage, strong educational content. "
     "Queries: 'boomer benefits', 'boomer benefits medicare'.",
     "aggregator_competitor"),

    ("Integrity Marketing", None,
     "approved", None, None, None, "medicare,life",
     "Insurance distribution company / lead buyer. "
     "Partners with multiple Medicare carriers.",
     "aggregator_lead_buyer"),

    ("Medicare Supplement Reviews", None,
     "approved", None, None, None, "medicare",
     "Medicare comparison site.",
     "aggregator_unverified"),
]


# ---------------------------------------------------------------------------
# Supplemental keyword variants
# Short-form, common queries, and variants not covered by the workbook sheets
# ---------------------------------------------------------------------------

MEDICARE_SUPPLEMENTAL = [
    # (brand_normalized_name, keyword_text, match_type)

    # UnitedHealthcare / UHC
    ("unitedhealthcare",   "unitedhealthcare",                      None),
    ("unitedhealthcare",   "united healthcare",                     None),
    ("unitedhealthcare",   "united health care",                    None),
    ("unitedhealthcare",   "uhc",                                   None),
    ("unitedhealthcare",   "uhc com",                               None),
    ("unitedhealthcare",   "uhc medicare",                          None),
    ("unitedhealthcare",   "united healthcare medicare",            None),
    ("unitedhealthcare",   "united healthcare medicare advantage",  None),
    ("aarp medicare",      "aarp medicare supplement",              None),
    ("aarp medicare",      "aarp medigap",                          None),
    ("aarp medicare",      "aarp medicare advantage",               None),
    ("aarp medicare",      "aarp medicare plans",                   None),

    # Humana (already in registry, add medicare-specific variants)
    ("humana",             "humana medicare",                       None),
    ("humana",             "humana medicare advantage",             None),
    ("humana",             "humana medicare supplement",            None),
    ("humana",             "humana medigap",                        None),
    ("humana",             "humana gold plus",                      None),
    ("humana",             "humana part d",                         None),
    ("humana gold plus",   "humana gold plus",                      None),
    ("humana gold plus",   "humana gold plus hmo",                  None),

    # Aetna
    ("aetna",              "aetna",                                 None),
    ("aetna",              "aetna medicare",                        None),
    ("aetna",              "aetna medicare advantage",              None),
    ("aetna",              "aetna medicare supplement",             None),
    ("aetna",              "aetna medigap",                         None),
    ("aetna",              "aetna medicare part d",                 None),
    ("aetna",              "aetna com",                             None),

    # Cigna
    ("cigna",              "cigna",                                 None),
    ("cigna",              "cigna medicare",                        None),
    ("cigna",              "cigna medicare supplement",             None),
    ("cigna",              "cigna medigap",                         None),
    ("cigna",              "cigna medicare advantage",              None),
    ("cigna",              "cigna com",                             None),

    # Wellcare
    ("wellcare",           "wellcare",                              None),
    ("wellcare",           "wellcare medicare",                     None),
    ("wellcare",           "wellcare medicare advantage",           None),
    ("wellcare",           "wellcare part d",                       None),
    ("wellcare",           "well care medicare",                    None),

    # Anthem / BCBS
    ("anthem",             "anthem medicare",                       None),
    ("anthem",             "anthem medicare advantage",             None),
    ("anthem",             "anthem blue cross medicare",            None),
    ("blue cross blue shield", "blue cross medicare",              None),
    ("blue cross blue shield", "bcbs medicare",                    None),
    ("blue cross blue shield", "blue cross blue shield medicare",  None),
    ("blue cross blue shield", "blue cross medigap",               None),
    ("bcbs",               "bcbs",                                  None),
    ("bcbs",               "bcbs medicare",                         None),

    # Kaiser Permanente
    ("kaiser permanente",  "kaiser medicare",                       None),
    ("kaiser permanente",  "kaiser medicare advantage",             None),
    ("kaiser permanente",  "kaiser permanente",                     None),
    ("kaiser permanente",  "kaiser permanente medicare",            None),

    # Mutual of Omaha
    ("mutual of omaha",    "mutual of omaha",                       None),
    ("mutual of omaha",    "mutual of omaha medicare supplement",   None),
    ("mutual of omaha",    "mutual of omaha medigap",               None),
    ("mutual of omaha",    "mutual of omaha medicare",              None),
    ("mutual of omaha",    "moo medicare",                          None),

    # Transamerica
    ("transamerica",       "transamerica",                          None),
    ("transamerica",       "transamerica medicare supplement",      None),
    ("transamerica",       "transamerica medigap",                  None),

    # Devoted Health
    ("devoted health",     "devoted health",                        None),
    ("devoted health",     "devoted health medicare",               None),
    ("devoted health",     "devoted medicare",                      None),

    # Clover Health
    ("clover health",      "clover health",                         None),
    ("clover health",      "clover health medicare",                None),

    # GoHealth
    ("gohealth",           "gohealth",                              None),
    ("gohealth",           "go health",                             None),
    ("gohealth",           "gohealth medicare",                     None),
    ("gohealth",           "gohealth insurance",                    None),

    # eHealth
    ("ehealth",            "ehealth",                               None),
    ("ehealth",            "ehealthinsurance",                      None),
    ("ehealth",            "ehealth medicare",                      None),
    ("ehealth",            "ehealth insurance",                     None),

    # SilverScript (CVS Part D)
    ("silverscript",       "silverscript",                          None),
    ("silverscript",       "silverscript part d",                   None),
    ("silverscript",       "silverscript medicare",                 None),
    ("silverscript",       "silver script",                         None),

    # Colonial Penn
    ("colonial penn",      "colonial penn",                         None),
    ("colonial penn",      "colonial penn medicare",                None),
    ("colonial penn",      "colonial penn medicare supplement",     None),

    # Globe Life
    ("globe life",         "globe life",                            None),
    ("globe life",         "globe life medicare supplement",        None),
    ("globe life",         "globe life insurance",                  None),

    # Boomer Benefits
    ("boomer benefits",    "boomer benefits",                       None),
    ("boomer benefits",    "boomer benefits medicare",              None),

    # HealthMarkets
    ("healthmarkets",      "healthmarkets",                         None),
    ("healthmarkets",      "health markets",                        None),
    ("healthmarkets",      "healthmarkets medicare",                None),

    # Hartford Medicare (Hartford already in registry)
    ("hartford",           "hartford medicare supplement",          None),
    ("hartford",           "aarp hartford medicare",                None),

    # Surebridge (UHC sub-brand, already in registry)
    ("surebridge",         "surebridge medicare",                   None),
    ("surebridge",         "surebridge medicare supplement",        None),
]


# ---------------------------------------------------------------------------
# Upsert functions
# ---------------------------------------------------------------------------

def seed_medicare_brands(conn) -> dict:
    """
    Inserts/updates Medicare brands in brand_registry.
    Returns {normalized_name: id} for use in supplemental seeding.
    """
    cursor   = conn.cursor()
    brand_ids = {}
    inserted = 0
    updated  = 0

    for entry in MEDICARE_BRAND_DATA:
        (brand_name, parent_brand, bid_status, restriction_reason,
         restriction_source, effective_date, verticals, notes,
         brand_category) = entry

        norm = normalize(brand_name)

        existing = cursor.execute(
            "SELECT id FROM brand_registry WHERE normalized_name = ?", (norm,)
        ).fetchone()

        if existing is None:
            cursor.execute("""
                INSERT INTO brand_registry
                    (brand_name, normalized_name, parent_brand, bid_status,
                     brand_category, restriction_reason, restriction_source,
                     effective_date, verticals, notes)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (brand_name, norm, parent_brand, bid_status,
                  brand_category, restriction_reason, restriction_source,
                  effective_date, verticals, notes))
            brand_ids[norm] = cursor.lastrowid
            inserted += 1
        else:
            cursor.execute("""
                UPDATE brand_registry SET
                    bid_status         = ?,
                    brand_category     = ?,
                    restriction_reason = ?,
                    restriction_source = ?,
                    effective_date     = ?,
                    verticals          = ?,
                    notes              = ?,
                    parent_brand       = ?
                WHERE normalized_name = ?
            """, (bid_status, brand_category, restriction_reason,
                  restriction_source, effective_date, verticals, notes,
                  parent_brand, norm))
            brand_ids[norm] = existing["id"]
            updated += 1

    conn.commit()
    print(f"  brand_registry: {inserted} inserted, {updated} updated")
    return brand_ids


def seed_medicare_supplemental(conn, brand_ids: dict):
    """
    Inserts Medicare-specific keyword variants into brand_registry_keywords.
    """
    cursor   = conn.cursor()
    inserted = 0
    skipped  = 0

    def _norm(t):
        return re.sub(r'\s+', ' ', t.lower().strip())

    for brand_norm, kw_text, match_type in MEDICARE_SUPPLEMENTAL:
        # Look up brand_id from passed-in dict first, then fall back to DB
        brand_id = brand_ids.get(brand_norm)
        if brand_id is None:
            row = cursor.execute(
                "SELECT id FROM brand_registry WHERE normalized_name = ?",
                (brand_norm,)
            ).fetchone()
            if row:
                brand_id = row["id"]
            else:
                print(f"  WARNING: brand '{brand_norm}' not found for variant '{kw_text}' -- skipping")
                skipped += 1
                continue

        norm_kw = _norm(kw_text)
        try:
            cursor.execute("""
                INSERT INTO brand_registry_keywords
                    (brand_id, keyword_text, normalized, match_type, source_sheet)
                VALUES (?, ?, ?, ?, 'medicare_supplemental')
                ON CONFLICT (normalized, match_type) DO NOTHING
            """, (brand_id, kw_text, norm_kw, match_type))
            if cursor.rowcount > 0:
                inserted += 1
        except Exception:
            skipped += 1

    conn.commit()
    print(f"  Medicare supplemental variants: {inserted} inserted, {skipped} skipped/existing")


def patch_existing_brand_verticals(conn):
    """
    Ensures brands already in the registry from 07_seed_brand_registry.py
    have 'medicare' in their verticals field where applicable.
    Patches conservatively -- only adds medicare where it's clearly missing.
    """
    cursor  = conn.cursor()
    updated = 0

    # (normalized_name, correct_verticals)
    PATCHES = [
        ("humana",       "medicare"),           # already correct in 07 -- no-op expected
        ("hartford",     "auto,home,medicare"),
        ("aarp",         "auto,home,medicare"),
        ("surebridge",   "medicare"),           # already correct in 07 -- no-op expected
    ]

    for norm, correct_verticals in PATCHES:
        row = cursor.execute(
            "SELECT id, verticals FROM brand_registry WHERE normalized_name = ?",
            (norm,)
        ).fetchone()
        if not row:
            continue
        current = row["verticals"] or ""
        if "medicare" in current:
            continue
        merged = current.rstrip(",") + ",medicare" if current else "medicare"
        cursor.execute(
            "UPDATE brand_registry SET verticals = ? WHERE normalized_name = ?",
            (merged, norm)
        )
        print(f"  Patched verticals: {norm} -> {merged}")
        updated += 1

    conn.commit()
    if updated == 0:
        print("  No vertical patches needed (all already include medicare or not found)")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("=" * 65)
    print("07_seed_brand_registry_medicare_patch.py")
    print("=" * 65)

    conn   = get_conn()
    cursor = conn.cursor()

    # Verify base brand registry was seeded first
    count = cursor.execute("SELECT COUNT(*) FROM brand_registry").fetchone()[0]
    if count == 0:
        print("\nERROR: brand_registry is empty.")
        print("Run 07_seed_brand_registry.py first, then re-run this script.")
        conn.close()
        return
    print(f"\nExisting brands in registry: {count}")

    print(f"\n[1] Seeding {len(MEDICARE_BRAND_DATA)} Medicare brands...")
    brand_ids = seed_medicare_brands(conn)

    print(f"\n[2] Adding {len(MEDICARE_SUPPLEMENTAL)} Medicare supplemental keyword variants...")
    seed_medicare_supplemental(conn, brand_ids)

    print(f"\n[3] Patching verticals on pre-existing brands...")
    patch_existing_brand_verticals(conn)

    # Summary
    total_brands   = cursor.execute("SELECT COUNT(*) FROM brand_registry").fetchone()[0]
    total_kw_rows  = cursor.execute("SELECT COUNT(*) FROM brand_registry_keywords").fetchone()[0]
    medicare_brands = cursor.execute(
        "SELECT COUNT(*) FROM brand_registry WHERE verticals LIKE '%medicare%'"
    ).fetchone()[0]

    print(f"\n{'='*65}")
    print("PATCH COMPLETE")
    print(f"{'='*65}")
    print(f"  Total brands in registry       : {total_brands:>8,}")
    print(f"  Brands with medicare vertical  : {medicare_brands:>8,}")
    print(f"  Total keyword variants         : {total_kw_rows:>8,}")

    print(f"\n  All brands with medicare vertical:")
    rows = cursor.execute("""
        SELECT brand_name, bid_status, brand_category, verticals
        FROM brand_registry
        WHERE verticals LIKE '%medicare%'
        ORDER BY bid_status, brand_name
    """).fetchall()
    for r in rows:
        print(f"    [{r['bid_status']:<11}]  [{r['brand_category'] or 'none':<32}]  {r['brand_name']}")

    conn.close()
    print("\nDone.")
    print("\nNext steps:")
    print("  python kw_db_scripts/08_tag_search_queries.py --untagged")
    print("  python kw_db_scripts/09_tag_keyword_identities.py")


if __name__ == "__main__":
    main()
