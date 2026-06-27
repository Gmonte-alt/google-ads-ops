"""
07_seed_brand_registry.py  [v3 — Confie network + mislabel fixes]
--------------------------
Step 1: Migrates search_queries table to add classification columns.
Step 2: Seeds brand_registry and brand_registry_keywords from the
        Buyer_Brand_Negative_List.xlsx workbook.

CHANGELOG v2 → v3
    * FIX: Lemonade was mislabeled 'approved' → now 'restricted'
      (carrier_restricted_requested). NON_BIDDABLE_OVERRIDE stopgap can retire.
    * FIX: TGS Insurance was mislabeled 'approved'/'carrier_regional' → now
      'restricted', parent 'Confie', 'confie_subsidiary'. Fixed in place in
      REGIONAL_BRAND_DATA (keeps its home vertical + Texas note); the Confie
      yaml loader skips it to avoid a duplicate row.
    * NEW: load_confie_brand_data() seeds the ~60-brand Confie competitor agency
      network from analysis/input/confie_brands.yaml as brand_registry IDENTITY
      rows (restricted, parent 'Confie', 'confie_subsidiary'). Skips the
      __COMPETITOR__ flag-list entry and any already-seeded name.
    * DEFERRED (guard reconciliation, separate pass): Confie ALIASES are NOT
      loaded into brand_registry_keywords here. The yaml's disqualifiers /
      require_context guards decide which aliases are safe to exact-match
      (e.g. 'freeway' vs 'freeway 405'); loading bare aliases into the tagger's
      exact-match table without those guards would create false positives. That
      belongs with the confie_negkw.py guard reconciliation, not this pass.

The brand_registry is the master reference for the auto-tagger. It captures:
  - All carrier/aggregator brand names with their bid status
  - Parent/sub-brand relationships (e.g. Safeco -> Liberty Mutual)
  - Which insurance verticals each brand operates in
  - Operational notes from Ian Hopper call notes
  - ~10,000 keyword misspelling variants from QW, SmartFinancial, Media Alpha

The brand_registry_keywords table is used by the tagger for fast exact-match
lookup before falling back to fuzzy matching. Every misspelling variant in the
source workbook is stored here linked to its parent brand.

Usage:
    conda activate paid-search
    python kw_db_scripts/07_seed_brand_registry.py

Safe to re-run -- uses INSERT OR REPLACE / INSERT OR IGNORE throughout.
The workbook path can be overridden via BRAND_WORKBOOK_PATH env variable.

Schema added to search_queries:
    vertical          -- auto | home | medicare | life | cross | unclassified
    modifier_brand    -- canonical brand name if query contains brand signal
    modifier_geo      -- state/city name if geo modifier present
    modifier_price    -- 1 if price-intent signal present
    modifier_coverage -- coverage type if present (flood, fire, renters, etc.)
    tag_method        -- how the tag was assigned
    tag_confidence    -- high | medium | low | unclassified
"""

import sqlite3
import re
from pathlib import Path
from openpyxl import load_workbook
import yaml  # for confie_brands.yaml (Confie network identity load)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DB_PATH       = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")
WORKBOOK_PATH = Path(r"C:\Projects\google-ads-ops\kw_db_scripts\input\Buyer_Brand_Negative_List.xlsx")

# Confie competitor agency network — identity source of truth (aliases/guards
# stay in confie_negkw.py; only brand identity is loaded into brand_registry).
CONFIE_YAML_PATH          = Path(r"C:\Projects\google-ads-ops\analysis\input\confie_brands.yaml")
CONFIE_EFFECTIVE_DATE     = "2026-06-12"  # date this correction was applied
CONFIE_RESTRICTION_SOURCE = "confie_brands.yaml (Confie subsidiary list + affiliate disclosures)"

# ---------------------------------------------------------------------------
# Database connection
# ---------------------------------------------------------------------------

def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# Step 1: Schema migration
# ---------------------------------------------------------------------------

def migrate_schema(conn):
    """
    Adds classification columns to search_queries if they don't exist,
    and creates brand_registry + brand_registry_keywords tables.
    Safe to run on an already-migrated database.
    """
    cursor = conn.cursor()

    # Add columns to search_queries (ALTER TABLE IF NOT EXISTS column
    # not supported in SQLite -- check manually)
    existing_cols = {
        row[1] for row in cursor.execute("PRAGMA table_info(search_queries)")
    }

    new_cols = [
        ("vertical",             "TEXT"),
        ("modifier_brand",       "TEXT"),
        ("modifier_geo",         "TEXT"),
        ("modifier_price",       "INTEGER DEFAULT 0"),
        ("modifier_coverage",    "TEXT"),
        ("modifier_home_type",   "TEXT"),       # NEW: dwelling type (condo, mobile_home, etc.)
        ("modifier_bundle",      "INTEGER DEFAULT 0"),
        ("modifier_near_me",     "INTEGER DEFAULT 0"),
        ("modifier_home_purchase", "INTEGER DEFAULT 0"),
        ("intent_type",          "TEXT"),       # NEW: commercial | informational
        ("intent_action",        "TEXT"),       # NEW: compare | quote | answer | checklist | review | coverage-lookup
        ("brand_category",       "TEXT"),
        ("tag_method",           "TEXT DEFAULT 'unclassified'"),
        ("tag_confidence",       "TEXT DEFAULT 'unclassified'"),
    ]
    for col_name, col_def in new_cols:
        if col_name not in existing_cols:
            cursor.execute(
                f"ALTER TABLE search_queries ADD COLUMN {col_name} {col_def}"
            )
            print(f"  Added column: search_queries.{col_name}")
        else:
            print(f"  Column exists: search_queries.{col_name}")

    # brand_registry -- one row per canonical brand name
    cursor.executescript("""
        CREATE TABLE IF NOT EXISTS brand_registry (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            brand_name          TEXT    NOT NULL,
            normalized_name     TEXT    NOT NULL UNIQUE,
            parent_brand        TEXT,
            bid_status          TEXT    NOT NULL,
            -- bid_status values:
            --   'restricted'   Cannot bid -- firm QW rule or carrier action
            --   'conditional'  Can bid but QW may request stop at any time
            --   'approved'     Cleared to bid
            brand_category      TEXT,
            -- brand_category values:
            --   'carrier_restricted_partner'   Restricted due to QW-carrier partnership
            --   'carrier_restricted_requested' Restricted by QW request (may change)
            --   'carrier_conditional'          Approved but QW can pull at any time
            --   'carrier_approved'             Cleared national carrier brand
            --   'carrier_regional'             Regional/state carrier (not household-name national)
            --   'aggregator_lead_buyer'        Partner we sell leads to/alongside
            --   'aggregator_competitor'        Comparison site, no restriction
            --   'aggregator_unverified'        Aggregator with unknown relationship
            --   'confie_subsidiary'            Confie competitor agency network
            --                                  (parent + ~60 subsidiaries; do-not-bid)
            --   NULL                           Non-carrier/aggregator brand
            restriction_reason  TEXT,
            restriction_source  TEXT,
            effective_date      TEXT,
            verticals           TEXT,
            -- comma-separated: 'home', 'auto', 'medicare', 'life'
            -- 'cross' means brand spans multiple verticals
            notes               TEXT,
            source_sheet        TEXT    DEFAULT 'QW_Brand_names'
        );

        CREATE INDEX IF NOT EXISTS idx_br_normalized
            ON brand_registry (normalized_name);

        CREATE INDEX IF NOT EXISTS idx_br_bid_status
            ON brand_registry (bid_status);

        CREATE INDEX IF NOT EXISTS idx_br_verticals
            ON brand_registry (verticals);

        -- brand_registry_keywords -- every known keyword variant / misspelling
        -- linked to a parent brand. Used for fast exact-match lookup in tagger.
        CREATE TABLE IF NOT EXISTS brand_registry_keywords (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            brand_id     INTEGER NOT NULL REFERENCES brand_registry(id),
            keyword_text TEXT    NOT NULL,
            normalized   TEXT    NOT NULL,
            match_type   TEXT,
            source_sheet TEXT,
            UNIQUE (normalized, match_type)
        );

        CREATE INDEX IF NOT EXISTS idx_brk_normalized
            ON brand_registry_keywords (normalized);

        CREATE INDEX IF NOT EXISTS idx_brk_brand
            ON brand_registry_keywords (brand_id);
    """)
    conn.commit()
    print("  brand_registry and brand_registry_keywords tables verified")

    # Migrate brand_registry -- add columns introduced after initial schema
    # Uses same safe pattern as search_queries migration above
    existing_br_cols = {
        row[1] for row in cursor.execute("PRAGMA table_info(brand_registry)")
    }
    new_br_cols = [
        ("brand_category", "TEXT"),
    ]
    for col_name, col_def in new_br_cols:
        if col_name not in existing_br_cols:
            cursor.execute(
                f"ALTER TABLE brand_registry ADD COLUMN {col_name} {col_def}"
            )
            print(f"  Added column: brand_registry.{col_name}")
        else:
            print(f"  Column exists: brand_registry.{col_name}")
    conn.commit()


# ---------------------------------------------------------------------------
# Step 2: Brand registry data
# ---------------------------------------------------------------------------

# Manually curated brand data -- canonical source of truth.
# verticals: which insurance products this brand sells
# brand_category: strategic classification (see brand_registry DDL for values)
# Each entry: (brand_name, parent_brand, bid_status, restriction_reason,
#              restriction_source, effective_date, verticals, notes,
#              brand_category)

BRAND_DATA = [
    # -----------------------------------------------------------------------
    # RESTRICTED -- Do Not Bid
    # -----------------------------------------------------------------------
    ("Progressive", None,
     "restricted", "Direct QW restriction -- carrier would require turn-off",
     "QW policy", "2024-01-01", "auto,home",
     "Progressive is just clicks per Ian H. Core restriction.",
     "carrier_restricted_partner"),

    ("Homesite", "Progressive",
     "restricted", "Sub-brand of Progressive",
     "QW policy", "2024-01-01", "home",
     "1% of impressions, 1.08 ROAS before negated 10/22",
     "carrier_restricted_partner"),

    ("Maxey Progressive", "Progressive",
     "restricted", "Sub-brand of Progressive",
     "QW policy", "2024-01-01", "auto",
     None,
     "carrier_restricted_partner"),

    ("Allstate", None,
     "restricted", "QW-Allstate strategic partnership -- bidding would threaten relationship",
     "QW-Allstate partnership", "2024-01-01", "auto,home",
     "Allstate is #2 across all LendingTree. Relationship too large to risk.",
     "carrier_restricted_partner"),

    ("Esurance", "Allstate",
     "restricted", "Sub-brand of Allstate -- same partnership restriction applies",
     "QW-Allstate partnership", "2024-01-01", "auto,home",
     None,
     "carrier_restricted_partner"),

    ("Auto Direct", "Allstate",
     "restricted", "Allstate sub-brand -- QW requested stop 1.7.26",
     "QW request 2026-01-07", "2026-01-07", "auto",
     "Variation of Direct Auto",
     "carrier_restricted_partner"),

    ("General Direct Insurance", "Allstate",
     "restricted", "Allstate sub-brand -- QW requested stop 1.7.26",
     "QW request 2026-01-07", "2026-01-07", "auto",
     "Variation of Direct General. Ian still owes confirmation if truly Allstate.",
     "carrier_restricted_partner"),

    ("Direct Auto", "Allstate",
     "restricted", "Allstate sub-brand -- QW requested stop 1.7.26",
     "QW request 2026-01-07", "2026-01-07", "auto",
     None,
     "carrier_restricted_partner"),

    ("Direct General", "Allstate",
     "restricted", "Allstate sub-brand -- QW requested stop 1.7.26",
     "QW request 2026-01-07", "2026-01-07", "auto",
     None,
     "carrier_restricted_partner"),

    ("National General", "Allstate",
     "restricted", "Allstate sub-brand -- QW requested stop 1.7.26",
     "QW request 2026-01-07", "2026-01-07", "auto,home",
     None,
     "carrier_restricted_partner"),

    ("Safe Auto", "Allstate",
     "restricted", "Allstate sub-brand -- QW requested stop 1.7.26",
     "QW request 2026-01-07", "2026-01-07", "auto",
     None,
     "carrier_restricted_partner"),

    ("USAA", None,
     "restricted", "EQ policing; USAA threatened to shut affiliate program",
     "Ian Hopper request 2025-11-03", "2025-11-03", "auto,home,life",
     "USAA might shut down affiliate spend entirely. Wait for corporate resolution.",
     "carrier_restricted_requested"),

    ("Liberty Mutual", None,
     "restricted", "QW requested full pause 2.10.26; fully off 2.22.26",
     "QW request 2026-02-10", "2026-02-22", "auto,home",
     "Liberty sends infractions but tolerated performance. Now fully negated.",
     "carrier_restricted_requested"),

    ("Safeco", "Liberty Mutual",
     "restricted", "Liberty Mutual sub-brand -- same pause applies",
     "QW request 2026-02-10", "2026-02-22", "home",
     None,
     "carrier_restricted_requested"),

    ("Right Track", "Liberty Mutual",
     "restricted", "Liberty Mutual sub-brand -- same pause applies",
     "QW request 2026-02-10", "2026-02-22", "auto",
     None,
     "carrier_restricted_requested"),

    ("Peerless Insurance", "Liberty Mutual",
     "restricted", "Liberty Mutual sub-brand -- same pause applies",
     "QW request 2026-02-10", "2026-02-22", "home",
     None,
     "carrier_restricted_requested"),

    ("Montgomery Insurance", "Liberty Mutual",
     "restricted", "Liberty Mutual sub-brand -- same pause applies",
     "QW request 2026-02-10", "2026-02-22", "home",
     None,
     "carrier_restricted_requested"),

    ("Ohio Casualty", "Liberty Mutual",
     "restricted", "Liberty Mutual sub-brand -- same pause applies",
     "QW request 2026-02-10", "2026-02-22", "home",
     None,
     "carrier_restricted_requested"),

    ("American Fire and Casualty Company", "Liberty Mutual",
     "restricted", "Liberty Mutual sub-brand -- same pause applies",
     "QW request 2026-02-10", "2026-02-22", "home",
     None,
     "carrier_restricted_requested"),

    ("Ironshore", "Liberty Mutual",
     "restricted", "Liberty Mutual sub-brand -- same pause applies",
     "QW request 2026-02-10", "2026-02-22", "home",
     None,
     "carrier_restricted_requested"),

    # -----------------------------------------------------------------------
    # CONDITIONAL -- Bidding OK but QW may request stop
    # -----------------------------------------------------------------------
    ("State Farm", None,
     "conditional", "QW would call to stop; strategic partner for agents",
     "Ian Hopper guidance", "2025-10-31", "auto,home",
     "State Farm is #3; huge agent base. Less policing than Progressive. "
     "Paused 9/30, turned on 10/31. DKI makes it much worse -- avoid.",
     "carrier_conditional"),

    ("Hi Road", "State Farm",
     "conditional", "State Farm sub-brand -- same conditional status",
     "Ian Hopper guidance", "2025-10-31", "auto",
     None,
     "carrier_conditional"),

    ("Root", None,
     "conditional", "QW would call to stop but Root is small post-issues",
     "Ian Hopper guidance", "2025-01-01", "auto",
     "Root went through financial issues. Small partner now. "
     "Another publisher works with them heavily.",
     "carrier_conditional"),

    ("Kin Insurance", None,
     "conditional", "QW polices fairly well for Home vertical",
     "Ian Hopper guidance", "2025-01-01", "home",
     "Big deal for Home. 4% of impressions, 1.19 ROAS. Good to try.",
     "carrier_conditional"),

    # -----------------------------------------------------------------------
    # APPROVED -- OK to bid
    # -----------------------------------------------------------------------
    ("AIG", None,          "approved", None, None, None, "home,life", None,
     "carrier_approved"),
    ("Amica", None,        "approved", None, None, None, "auto,home", None,
     "carrier_approved"),
    ("Assurant", None,     "approved", None, None, None, "home", "Ian H confirmed fine.",
     "carrier_approved"),
    ("Bestow", None,       "approved", None, None, None, "life", None,
     "carrier_approved"),
    ("Brightway", None,    "approved", None, None, None, "home,auto", None,
     "carrier_approved"),
    ("Compare.com", None,  "approved", None, None, None, "auto,home", None,
     "aggregator_competitor"),
    ("Elephant", None,     "approved", None, None, None, "auto", None,
     "carrier_approved"),
    ("Ethos", None,        "approved", None, None, None, "life", None,
     "carrier_approved"),
    ("EverQuote", None,    "approved", None, None, None, "auto,home,medicare,life",
     "Lead buyer partner -- we sell leads to EverQuote",
     "aggregator_lead_buyer"),
    ("Farmers", None,      "approved", None, None, None, "auto,home,life",
     "Don't police aggressively. Similar to State Farm but less risk.",
     "carrier_approved"),
    ("Foremost Insurance", "Farmers",
     "approved", None, None, None, "home", None,
     "carrier_approved"),
    ("Gabi", None,         "approved", None, None, None, "auto,home", None,
     "aggregator_competitor"),
    ("Haggarty Insurance", None, "approved", None, None, None, "home", None,
     "carrier_approved"),
    ("Hartford", None,     "approved", None, None, None, "auto,home,medicare", None,
     "carrier_approved"),
    ("Hippo", None,        "approved", None, None, None, "home", None,
     "carrier_approved"),
    ("Humana", None,       "approved", None, None, None, "medicare", None,
     "carrier_approved"),
    ("Insured Nation", None, "approved", None, None, None, "auto,home", None,
     "carrier_approved"),
    ("Insurance One", None, "approved", None, None, None, "auto,home", None,
     "carrier_approved"),
    ("Lemonade", None,
     "restricted",
     "Confirmed non-biddable — was mislabeled 'approved'; NON_BIDDABLE_OVERRIDE stopgap retired",
     "QW request", CONFIE_EFFECTIVE_DATE, "home,renters", None,
     "carrier_restricted_requested"),
    ("SelectQuote", None,  "approved", None, None, None, "auto,home,medicare,life",
     "Lead buyer partner -- we sell leads to SelectQuote",
     "aggregator_lead_buyer"),
    ("Surebridge", "UHC",  "approved", None, None, None, "medicare", None,
     "carrier_approved"),
    ("Zebra", None,        "approved", None, None, None, "auto,home",
     "Comparison aggregator -- approved to bid",
     "aggregator_competitor"),
    ("Policygenius", None, "approved", None, None, None, "auto,home,life,medicare",
     "Comparison aggregator -- approved to bid",
     "aggregator_competitor"),
    ("Bamboo", None,       "approved", None, None, None, "home",
     "California-focused home insurer. High value in our data.",
     "carrier_approved"),
    ("Travelers", None,    "approved", None, None, None, "auto,home", None,
     "carrier_approved"),
    ("Geico", None,        "approved", None, None, None, "auto,home",
     "Top revenue query driver. $147K offline revenue in backfill.",
     "carrier_approved"),
    ("CSAA", None,         "approved", None, None, None, "auto,home",
     "AAA regional affiliate. Active in our keyword data.",
     "carrier_approved"),
    ("AAA", None,          "approved", None, None, None, "auto,home", None,
     "carrier_approved"),
    ("Costco", None,       "approved", None, None, None, "home,auto",
     "Affinity brand through partner. Top spend keyword group.",
     "carrier_approved"),
    ("Universal Insurance", None, "approved", None, None, None, "home", None,
     "carrier_approved"),
    ("Tower Hill", None,   "approved", None, None, None, "home",
     "Florida-focused carrier. Active in Microsoft data.",
     "carrier_approved"),
    ("SageSure", None,     "approved", None, None, None, "home",
     "Catastrophe-zone home insurer. Seen in Louisiana campaign.",
     "carrier_approved"),
    ("Erie Insurance", None, "approved", None, None, None, "auto,home",
     "Regional carrier. Active in Microsoft data.",
     "carrier_approved"),
    ("Nationwide", None,   "approved", None, None, None, "auto,home,life", None,
     "carrier_approved"),
    ("American Family", None, "approved", None, None, None, "auto,home", None,
     "carrier_approved"),
    ("Hochheim Prairie", None, "approved", None, None, None, "home",
     "Texas-based regional carrier. Seen in Google data.",
     "carrier_approved"),
    ("NorCal AAA", None,   "approved", None, None, None, "auto,home",
     "Regional AAA affiliate.",
     "carrier_approved"),
    ("Quote Wizard", None, "approved", None, None, None, "auto,home,medicare,life",
     "Our primary lead buyer partner. Own brand queries.",
     "aggregator_lead_buyer"),

    # --- Additional brands found in search query revenue data ---
    ("Country Financial", None, "approved", None, None, None, "auto,home,life",
     "$6,118 offline revenue in spot check. Also 'country companies', 'cfpnet com'.",
     "carrier_approved"),
    ("AAA", None,           "approved", None, None, None, "auto,home",
     "Multiple regional affiliates. 'triple a', 'triple aaa', 'aaa texas', "
     "'aaa colorado', 'aaa michigan', 'aaa tidewater', 'norcal aaa'.",
     "carrier_approved"),
    ("California Fair Plan", None, "approved", None, None, None, "home",
     "CA state insurer of last resort. 'fair plan', 'ca fair plan'.",
     "carrier_approved"),
    ("Insurify", None,      "approved", None, None, None, "auto,home",
     "Aggregator/comparison site. $1,569 revenue in spot check.",
     "aggregator_competitor"),
    ("The General", None,   "approved", None, None, None, "auto",
     "Non-standard auto insurer. Ambiguous -- require insurance context.",
     "carrier_approved"),
    ("Vermont Mutual", None, "approved", None, None, None, "home,auto",
     "Regional New England carrier.",
     "carrier_approved"),
    ("American Modern", None, "approved", None, None, None, "home",
     "Specialty home insurer for non-standard properties.",
     "carrier_approved"),
    ("MetLife", None,       "approved", None, None, None, "auto,home,life",
     "Large national carrier.",
     "carrier_approved"),
    ("Alfa Insurance", None, "approved", None, None, None, "auto,home",
     "Southern US regional carrier. Queries: 'alfa', 'alfa home'.",
     "carrier_approved"),
    ("MAPFRE", None,        "approved", None, None, None, "auto,home",
     "International carrier with US presence.",
     "carrier_approved"),
    ("QBE", None,           "approved", None, None, None, "home",
     "Commercial/specialty insurer.",
     "carrier_approved"),
    ("Penn National", None, "approved", None, None, None, "auto,home",
     "Regional PA-based carrier.",
     "carrier_approved"),
    ("Allied Trust", None,  "approved", None, None, None, "home",
     "Specialty home insurer.",
     "carrier_approved"),
    ("Tower Hill", None,    "approved", None, None, None, "home",
     "Florida-focused carrier. Also 'towerhill' one word.",
     "carrier_approved"),
    ("West Bend Mutual", None, "approved", None, None, None, "auto,home",
     "Midwest regional carrier.",
     "carrier_approved"),
    ("SmartFinancial", None, "approved", None, None, None, "auto,home",
     "Lead buyer partner -- we sell leads to SmartFinancial. "
     "Queries: 'smartfinancial com', 'smartfinancial reviews'.",
     "aggregator_lead_buyer"),
    ("Western National", None, "approved", None, None, None, "auto,home",
     "Regional Midwest carrier.",
     "carrier_approved"),
    ("Goosehead Insurance", None, "approved", None, None, None, "auto,home",
     "Insurance agency/aggregator. Queries: 'goosehead ins'.",
     "aggregator_competitor"),
    ("Wawanesa", None,      "approved", None, None, None, "auto,home",
     "Canadian carrier with CA/OR US presence.",
     "carrier_approved"),
    ("California Casualty", None, "approved", None, None, None, "auto,home",
     "Affinity-market carrier (educators, first responders).",
     "carrier_approved"),
    ("Farm Bureau", None,   "approved", None, None, None, "auto,home,life",
     "State-level farm bureau insurers. 'farmbureau com', 'farm bureau'.",
     "carrier_approved"),
    ("Standard Casualty", None, "approved", None, None, None, "home",
     "Specialty home insurer.",
     "carrier_approved"),
    ("Grange Mutual", None, "approved", None, None, None, "auto,home",
     "Ohio-based regional carrier. 'grange mutual casualty'.",
     "carrier_approved"),
    ("AmFam", "American Family", "approved", None, None, None, "auto,home",
     "American Family short-form alias. 'amfam', 'amfam com'.",
     "carrier_approved"),
    ("AgileRates", None,    "approved", None, None, None, "auto,home",
     "Comparison/aggregator site.",
     "aggregator_competitor"),
    ("Matic", None,         "approved", None, None, None, "home",
     "Embedded insurance platform. 'matic com'.",
     "aggregator_competitor"),
    ("Fred Loya", None,     "approved", None, None, None, "auto",
     "Non-standard auto insurer, Texas/Southwest focus.",
     "carrier_approved"),
    ("Universal Property", None, "approved", None, None, None, "home",
     "Florida-focused home insurer. 'universal properties'.",
     "carrier_approved"),
    ("HomeQuote Explorer", None, "approved", None, None, None, "home",
     "NFIP/flood quote tool. Government-adjacent.",
     "aggregator_competitor"),
    ("FloodSmart", None,    "approved", None, None, None, "home",
     "FEMA flood insurance program. 'floodsmart gov'.",
     "carrier_approved"),
    ("California Earthquake Authority", None, "approved", None, None, None, "home",
     "CA state earthquake insurance program.",
     "carrier_approved"),
    ("Texas Windstorm", None, "approved", None, None, None, "home",
     "TX state windstorm insurance association.",
     "carrier_approved"),

    ("Mercury", None,      "approved", None, None, None, "auto,home",
     "Ambiguous -- only classify as Mercury Insurance when 'insurance'/"
     "insurance-adjacent term also present in query.",
     "carrier_approved"),
    ("Dairyland", None,    "approved", None, None, None, "auto", None,
     "carrier_approved"),
    ("21st Century", None, "approved", None, None, None, "auto,home", None,
     "carrier_approved"),
    ("Kemper", None,       "approved", None, None, None, "auto,home", None,
     "carrier_approved"),
    ("Stillwater", None,   "approved", None, None, None, "home", None,
     "carrier_approved"),
    ("Heritage", None,     "approved", None, None, None, "home",
     "Seen in South Carolina campaign data.",
     "carrier_approved"),
    ("Chubb", None,        "approved", None, None, None, "home,auto", None,
     "carrier_approved"),
    ("Encompass", None,    "approved", None, None, None, "home,auto", None,
     "carrier_approved"),
    ("Openly", None,       "approved", None, None, None, "home", None,
     "carrier_approved"),
    ("Kin", None,          "approved", None, None, None, "home",
     "Alias for Kin Insurance -- short form seen in queries.",
     "carrier_conditional"),
    ("AARP", None,         "approved", None, None, None, "auto,home,medicare",
     "AARP affinity brand. Appears in Microsoft modifier strategy.",
     "carrier_approved"),

    # --- Aggregator/marketplace brands found in query data ---
    # brand_category = 'aggregator_unverified': these appear in search query
    # data and convert, but haven't been formally classified. When any
    # of these exceed $500 in revenue, research and promote to
    # aggregator_competitor or add to negative keyword list.
    ("HoneyQuote", None,     "approved", None, None, None, "home",
     "Insurance marketplace. $49 revenue in diagnostic.",
     "aggregator_unverified"),
    ("Orion180", None,       "approved", None, None, None, "home",
     "Home insurance marketplace. $46 revenue in diagnostic.",
     "aggregator_unverified"),
    ("CoverTree", None,      "approved", None, None, None, "home",
     "Insurance comparison site. $28 revenue in diagnostic.",
     "aggregator_unverified"),
    ("The Canopy Group", None, "approved", None, None, None, "home",
     "Insurance group/agency. $33 revenue in diagnostic.",
     "aggregator_unverified"),
    ("TrueQuote", None,      "approved", None, None, None, "home",
     "Insurance quote site. 'true quote home' in diagnostic.",
     "aggregator_unverified"),
    ("RateCompare", None,    "approved", None, None, None, "auto,home",
     "Comparison site. 'ratecompare org' in diagnostic.",
     "aggregator_unverified"),
    ("CFPnet", None,         "approved", None, None, None, "home",
     "California Fair Plan network site. 'cfpnet', 'www cfpnet com'.",
     "aggregator_unverified"),
    ("SolvableHome", None,   "approved", None, None, None, "home",
     "'solvable home insurance' in diagnostic.",
     "aggregator_unverified"),
    ("Spence Mathews", None, "approved", None, None, None, "home",
     "Insurance agency/broker. $148 revenue in diagnostic.",
     "aggregator_unverified"),
    ("Bristol West", None,   "approved", None, None, None, "auto",
     "Non-standard auto insurer. $28 in diagnostic.",
     "aggregator_unverified"),
    ("American Bankers", None, "approved", None, None, None, "home",
     "Specialty insurer. 'american bankers ins co' in diagnostic.",
     "aggregator_unverified"),
    ("Hanover Insurance", None, "approved", None, None, None, "auto,home",
     "'hanover ins co home owners' in diagnostic.",
     "aggregator_unverified"),
    ("American Home Shield", None, "approved", None, None, None, "home",
     "Home warranty (not insurance). $67 in diagnostic. Monitor.",
     "aggregator_unverified"),

    # --- Brands found in diagnostic analysis ---
    ("Plymouth Rock", None, "approved", None, None, None, "auto,home",
     "Regional carrier, Northeast US.",
     "carrier_approved"),
    ("Chubb", None,         "approved", None, None, None, "home,auto",
     "High-net-worth insurer. Standalone 'chubb' queries seen.",
     "carrier_approved"),
    ("Homeowners of America", None, "approved", None, None, None, "home",
     "Regional carrier. 'homeowners of america' seen in query data.",
     "carrier_approved"),
    ("SaveMaxHomes", None,  "approved", None, None, None, "home",
     "Insurance marketplace. Appears in Microsoft query data.",
     "aggregator_competitor"),
    ("NerdWallet", None,    "approved", None, None, None, "auto,home,life",
     "Finance/insurance comparison aggregator. $1,246 in non-brand revenue.",
     "aggregator_competitor"),
    ("Grange Insurance", None, "approved", None, None, None, "auto,home",
     "Ohio-based regional. 'grange insurance' seen in diagnostic.",
     "carrier_approved"),
    ("Hochheim Prairie", None, "approved", None, None, None, "home",
     "Texas regional carrier. 'hochheim insurance' in diagnostic.",
     "carrier_approved"),
    ("Cumberland Mutual", None, "approved", None, None, None, "auto,home",
     "NJ/PA regional carrier. 'cumberland mutual insurance' in diagnostic.",
     "carrier_approved"),
    ("Matic Insurance", None, "approved", None, None, None, "home",
     "Embedded insurance platform. 'matic insurance' $865 in diagnostic.",
     "aggregator_competitor"),
    ("California Fair Plan", None, "approved", None, None, None, "home",
     "CA state insurer of last resort. Multiple queries in diagnostic.",
     "carrier_approved"),
    ("California Earthquake Authority", None, "approved", None, None, None, "home",
     "CA state earthquake program.",
     "carrier_approved"),
]



# ===========================================================================
# MEDICARE BRANDS — merged from 07_seed_brand_registry_medicare_patch.py
# Do NOT edit this section manually — it is generated by merge_brand_registry.py
# To add Medicare brands, add them here and re-run 07_seed_brand_registry.py
# ===========================================================================

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
# Regional carrier brand data
# Carriers with primarily state/regional footprints.
# brand_category uses 'carrier_regional' to distinguish from national carriers.
# National carriers use 'carrier_approved', 'carrier_restricted_*', etc.
#
# Carrier scope classification:
#   NATIONAL  -- licensed in 40+ states, household-name brands
#   REGIONAL  -- licensed in fewer states or regionally dominant
#
# National carriers already in BRAND_DATA:
#   State Farm, Allstate, Progressive, Geico, Farmers, Liberty Mutual,
#   USAA, Nationwide, Travelers, Chubb, Hartford, Amica, Safeco,
#   American Family, Erie, Lemonade, Hippo, Kin, Bamboo, Root, Elephant
#
# These are seeded separately so brand_category can be set to
# 'carrier_regional' and downstream analysis can filter national vs regional.
# ---------------------------------------------------------------------------

REGIONAL_BRAND_DATA = [
    # -----------------------------------------------------------------------
    # HOME INSURANCE -- REGIONAL CARRIERS
    # States covered noted where known
    # -----------------------------------------------------------------------

    # High-revenue misses from diagnostic output
    ("TGS Insurance", "Confie",
     "restricted",
     "Confie subsidiary — competitor agency network, do-not-bid. Was mislabeled 'approved'.",
     "confie_brands.yaml (Confie subsidiary list)", CONFIE_EFFECTIVE_DATE, "home",
     "Texas-focused Confie subsidiary agency. High volume in Texas market. "
     "Queries: 'tgs insurance', 'tgs insurance agency', 'tgs insurance houston tx'. "
     "Kept here (retains home vertical + Texas note); Confie yaml loader skips it to avoid a duplicate row.",
     "confie_subsidiary"),

    ("Loggerhead Insurance", None,
     "approved", None, None, None, "home",
     "Florida-focused home insurance carrier. "
     "Queries: 'loggerhead insurance', 'loggerhead insurance reviews'.",
     "carrier_regional"),

    ("Connect Insurance", None,
     "approved", None, None, None, "home,auto",
     "Regional insurance brand. "
     "Queries: 'connect insurance'.",
     "carrier_regional"),

    ("Kessler Insurance", None,
     "approved", None, None, None, "home,auto",
     "Regional independent insurance agency. "
     "Queries: 'kessler insurance'.",
     "carrier_regional"),

    ("Otto Insurance", None,
     "approved", None, None, None, "home,auto",
     "Digital insurance marketplace/aggregator. "
     "Queries: 'otto home insurance', 'otto insurance reviews'.",
     "carrier_regional"),

    ("Lloyd's of London", None,
     "approved", None, None, None, "home,auto,life",
     "Specialist insurance market (UK-based), high-value and surplus lines. "
     "Queries: 'lloyds of london home insurance'.",
     "carrier_regional"),

    ("Alliance Insurance", None,
     "approved", None, None, None, "home,auto",
     "Regional insurance group. "
     "Queries: 'alliance insurance company'.",
     "carrier_regional"),

    ("GeoVera Insurance", None,
     "approved", None, None, None, "home",
     "Specialty home insurer focused on earthquake and wind. "
     "Operates in CA, OR, WA, HI. "
     "Queries: 'geovera earthquake insurance', 'geovera insurance'.",
     "carrier_regional"),

    ("California FAIR Plan", None,
     "approved", None, None, None, "home",
     "California state insurer of last resort for home insurance. "
     "Queries: 'california fair plan', 'ca fair plan'.",
     "carrier_regional"),

    ("Colorado Homeowners Home Insurance Program", None,
     "approved", None, None, None, "home",
     "Colorado state-backed home insurance program. "
     "Queries: 'colorado homeowners home insurance program'.",
     "carrier_regional"),

    ("Sam's Club Home Insurance", None,
     "approved", None, None, None, "home",
     "Home insurance marketplace partnership via Sam's Club / Walmart. "
     "Queries: 'sams club home insurance'.",
     "carrier_regional"),

    ("Matic Insurance", None,
     "approved", None, None, None, "home",
     "Digital home insurance marketplace, often embedded in mortgage flows. "
     "Queries: 'matic home insurance', 'matic insurance'.",
     "aggregator_competitor"),

    ("American Modern Insurance", None,
     "approved", None, None, None, "home",
     "Specialty home insurer (Munich Re subsidiary). "
     "Manufactured homes, vacation homes, seasonal properties. "
     "Queries: 'american modern insurance', 'american modern home insurance'.",
     "carrier_regional"),

    ("Insurance One", None,
     "approved", None, None, None, "home,auto",
     "Regional independent insurance agency. "
     "Queries: 'insurance one'.",
     "carrier_regional"),

    ("Plymouth Rock", None,
     "approved", None, None, None, "home,auto",
     "Regional carrier operating in NJ, NY, CT, MA, PA, NH. "
     "Queries: 'plymouth rock insurance', 'plymouth rock home insurance'.",
     "carrier_regional"),

    ("Universal Insurance Holdings", None,
     "approved", None, None, None, "home",
     "Florida-focused home insurer. Operates under Universal Property. "
     "Queries: 'universal home insurance', 'universal property insurance'.",
     "carrier_regional"),

    ("Homeowner Relief Portal", None,
     "approved", None, None, None, "home",
     "Lead generation / comparison site targeting home insurance buyers. "
     "Queries: 'homeowner relief portal insurance'.",
     "aggregator_unverified"),

    ("American Homeowners Insurance", None,
     "approved", None, None, None, "home",
     "Regional home insurance brand. "
     "Queries: 'american homeowners insurance', 'american home insurance company'.",
     "carrier_regional"),

    # Unclassified keyword carriers from section E diagnostic
    ("ABANA Insurance", None,
     "approved", None, None, None, "auto,home",
     "Regional auto/home insurer. "
     "Queries: 'abana insurance', 'a abana insurance'.",
     "carrier_regional"),

    ("1st Chicago Insurance", None,
     "approved", None, None, None, "auto,home",
     "Chicago-area regional carrier. "
     "Queries: '1st chicago insurance'.",
     "carrier_regional"),

    ("2GO Insurance", None,
     "approved", None, None, None, "auto,home",
     "Regional insurance brand. "
     "Queries: '2 go insurance', '2go insurance'.",
     "carrier_regional"),

    ("A1 General Insurance", None,
     "approved", None, None, None, "auto,home",
     "Regional carrier. "
     "Queries: 'a1 general insurance', 'a1 general'.",
     "carrier_regional"),

    ("TrueQuote", None,
     "approved", None, None, None, "home,auto",
     "Insurance comparison/quoting platform. "
     "Queries: 'truequote home insurance', 'truequotehome'.",
     "aggregator_competitor"),

    ("SelectQuote", None,
     "approved", None, None, None, "home,auto,life,medicare",
     "Insurance comparison marketplace. "
     "Queries: 'selectquote', 'selectquote auto and home insurance'.",
     "aggregator_competitor"),

    ("CSAA Insurance", None,
     "approved", None, None, None, "home,auto",
     "AAA affiliate insurer in CA, NV, UT. Full name: CSAA Insurance Group. "
     "Queries: 'csaa insurance', 'csaaa', 'csaa home insurance'.",
     "carrier_regional"),
]

def normalize(text: str) -> str:
    """Lowercase, strip, collapse whitespace."""
    return re.sub(r'\s+', ' ', text.lower().strip())


def seed_brand_registry(conn, brands: list) -> dict:
    """Insert all brands into brand_registry. Returns {normalized_name: id}."""
    cursor = conn.cursor()
    brand_ids = {}
    inserted = 0
    updated = 0

    for entry in brands:
        # Unpack -- brand_category is optional last field for backward compat
        if len(entry) == 9:
            (brand_name, parent_brand, bid_status, restriction_reason,
             restriction_source, effective_date, verticals, notes,
             brand_category) = entry
        else:
            (brand_name, parent_brand, bid_status, restriction_reason,
             restriction_source, effective_date, verticals, notes) = entry
            brand_category = None

        norm = normalize(brand_name)

        existing = cursor.execute(
            "SELECT id FROM brand_registry WHERE normalized_name = ?", (norm,)
        ).fetchone()

        if existing is None:
            cursor.execute("""
                INSERT INTO brand_registry
                    (brand_name, normalized_name, parent_brand, bid_status,
                     brand_category,
                     restriction_reason, restriction_source, effective_date,
                     verticals, notes)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (brand_name, norm, parent_brand, bid_status,
                  brand_category,
                  restriction_reason, restriction_source, effective_date,
                  verticals, notes))
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


# ---------------------------------------------------------------------------
# Confie competitor agency network — identity load from confie_brands.yaml
# ---------------------------------------------------------------------------

def _confie_verticals(name: str) -> str:
    """Infer verticals from the brand name. Confie's network is predominantly
    non-standard / SR-22 AUTO; only flip to home when the name implies it."""
    n = name.lower()
    if any(t in n for t in ("home", "homeowner", "house", "property",
                            "renter", "tenant", "apartment")):
        return "home"
    return "auto"


def load_confie_brand_data(yaml_path: Path, already_seeded: set) -> list:
    """Build brand_registry 9-tuples from confie_brands.yaml.

    IDENTITY ONLY. Aliases/legal_entities are intentionally NOT loaded into
    brand_registry_keywords here — the yaml's disqualifiers / require_context
    guards decide which aliases are safe to exact-match, so variant loading
    belongs with the confie_negkw.py guard reconciliation, not this pass.

    Skips:
      - the __COMPETITOR__ entry (a competitor flag-list, not a Confie brand)
      - any brand whose normalized name is already seeded (e.g. TGS Insurance,
        fixed in place in REGIONAL_BRAND_DATA; also guards any other overlap so
        a hardcoded row is never silently overwritten by a thinner yaml row).
    """
    if not yaml_path.exists():
        print(f"  WARNING: Confie yaml not found at {yaml_path} — skipping Confie load.")
        return []

    with open(yaml_path, "r", encoding="utf-8") as f:
        brands = yaml.safe_load(f) or []

    out = []
    skipped_nonbrand = 0
    skipped_dupe = 0
    for b in brands:
        canonical = (b.get("canonical") or "").strip()
        if not canonical or canonical == "__COMPETITOR__":
            skipped_nonbrand += 1
            continue
        norm = normalize(canonical)
        if norm in already_seeded:
            skipped_dupe += 1
            continue

        is_parent = (norm == "confie")
        parent_brand = None if is_parent else "Confie"
        reason = ("Confie parent company — competitor agency network, do-not-bid"
                  if is_parent else
                  "Confie subsidiary — competitor agency network, do-not-bid")
        note = ("Loaded from confie_brands.yaml (identity only). Verticals inferred "
                "(Confie network is predominantly non-standard/SR-22 auto) — review for "
                "home exposure. Match guards (disqualifiers/require_context) live in "
                "confie_negkw.py, not the registry; aliases not loaded as variants here.")
        out.append((canonical, parent_brand, "restricted", reason,
                    CONFIE_RESTRICTION_SOURCE, CONFIE_EFFECTIVE_DATE,
                    _confie_verticals(canonical), note, "confie_subsidiary"))

    print(f"  Confie yaml: {len(out)} brands to seed  "
          f"({skipped_nonbrand} non-brand skipped, {skipped_dupe} already-seeded skipped)")
    return out


# ---------------------------------------------------------------------------
# Step 3: Determine which brand each keyword belongs to
# ---------------------------------------------------------------------------

def infer_brand_for_keyword(kw_text: str, brand_ids: dict) -> str | None:
    """
    Given a keyword text, find the best matching canonical brand name.
    Returns normalized brand name or None if no match.

    Strategy:
    1. Strip broad match modifier (+) syntax
    2. Check all brand normalized names for substring presence in keyword
    3. Sort candidates by length descending (longest match wins -- prevents
       'kin' matching 'kin' inside 'parking' etc.)
    """
    clean = kw_text.lower().replace('+', ' ')
    clean = re.sub(r'\s+', ' ', clean).strip()

    # Collect all candidates
    candidates = []
    for norm_brand in brand_ids:
        if norm_brand in clean:
            candidates.append(norm_brand)

    if not candidates:
        return None

    # Longest match wins
    return max(candidates, key=len)


def seed_brand_keywords_from_workbook(conn, brand_ids: dict, workbook_path: Path):
    """
    Reads keyword misspelling lists from three sheets and inserts into
    brand_registry_keywords. Each keyword is linked to its parent brand.

    Sheets:
        QW _Keyword - Match_type    -- 1,633 rows, QW negative list
        SmartFinancial              -- 4,069 rows, SF negative list
        Media Alpha                 -- 5,049 rows, MA negative list

    Keywords without a determinable brand are inserted under a special
    'unattributed' entry for future manual review.
    """
    cursor = conn.cursor()
    wb = load_workbook(str(workbook_path), read_only=True)

    # Ensure we have an 'unattributed' brand for orphan keywords
    unattr_norm = "unattributed_negative_keyword"
    row = cursor.execute(
        "SELECT id FROM brand_registry WHERE normalized_name = ?",
        (unattr_norm,)
    ).fetchone()
    if row is None:
        cursor.execute("""
            INSERT INTO brand_registry
                (brand_name, normalized_name, bid_status, verticals, notes)
            VALUES (?, ?, 'restricted', NULL,
                    'Keywords from negative lists with no determinable brand parent')
        """, ("Unattributed Negative Keyword", unattr_norm))
        conn.commit()
    unattr_id = cursor.execute(
        "SELECT id FROM brand_registry WHERE normalized_name = ?",
        (unattr_norm,)
    ).fetchone()["id"]

    total_inserted = 0
    total_skipped  = 0
    sheet_counts   = {}

    sheet_configs = [
        ("QW _Keyword - Match_type", 0, 1),   # col 0 = keyword, col 1 = match_type
        ("SmartFinancial",           0, 1),
        ("Media Alpha",              0, None), # col 0 = keyword, no match_type col
    ]

    for sheet_name, kw_col, mt_col in sheet_configs:
        if sheet_name not in wb.sheetnames:
            print(f"  WARNING: sheet '{sheet_name}' not found -- skipping")
            continue

        ws = wb[sheet_name]
        inserted_sheet = 0
        skipped_sheet  = 0

        for row in ws.iter_rows(values_only=True):
            kw = row[kw_col] if row and len(row) > kw_col else None
            if not kw or not isinstance(kw, str):
                continue
            kw = kw.strip()
            if not kw or kw.lower() in ('keyword_text', 'keyword', 'brand'):
                continue

            mt = None
            if mt_col is not None and len(row) > mt_col and row[mt_col]:
                mt = str(row[mt_col]).strip().upper()
                if mt not in ('EXACT', 'PHRASE', 'BROAD'):
                    mt = None

            norm_kw = normalize(kw.replace('+', ' '))

            # Find parent brand
            brand_norm = infer_brand_for_keyword(kw, brand_ids)
            if brand_norm and brand_norm in brand_ids:
                brand_id = brand_ids[brand_norm]
            else:
                brand_id = unattr_id

            try:
                cursor.execute("""
                    INSERT INTO brand_registry_keywords
                        (brand_id, keyword_text, normalized, match_type, source_sheet)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT (normalized, match_type) DO UPDATE SET
                        source_sheet = source_sheet || ',' || excluded.source_sheet
                """, (brand_id, kw, norm_kw, mt, sheet_name))
                inserted_sheet += 1
            except Exception:
                skipped_sheet += 1

        conn.commit()
        sheet_counts[sheet_name] = inserted_sheet
        total_inserted += inserted_sheet
        total_skipped  += skipped_sheet
        print(f"  {sheet_name:<35} {inserted_sheet:>6,} keywords loaded")

    return total_inserted, total_skipped


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def seed_supplemental_variants(conn, brand_ids: dict):
    """
    Inserts standalone short-form brand query variants that aren't in
    the negative keyword sheets (because they were never negated) but
    appear frequently in search query data without insurance modifiers.
    e.g. 'amica', 'statefarm', 'chubb', 'plymouth rock'
    These need to be in brand_registry_keywords so Layer 1 exact match
    catches them before the insurance relevance gate filters them out.
    """
    cursor = conn.cursor()
    inserted = 0
    skipped  = 0

    # (brand_normalized_name, keyword_text, match_type)
    SUPPLEMENTAL = [
        # Amica standalone and variants
        ('amica',             'amica',                None),
        ('amica',             'amica mutual',         None),
        ('amica',             'amica mutual group',   None),
        ('amica',             'amicca',               None),
        ('amica',             'amica com',            None),
        ('amica',             'amica homeowners',     None),
        ('amica',             'amica quote',          None),
        ('amica',             'amica insurance',      None),
        ('amica',             'amica home insurance', None),
        ('amica',             'amica homeowners insurance', None),
        # State Farm one-word variant
        ('state farm',        'statefarm',            None),
        ('state farm',        'statefarm com',        None),
        # Kin standalone
        ('kin insurance',     'kininsurance',         None),
        ('kin insurance',     'kin interinsurance network', None),
        ('kin',               'kininsurance',         None),
        # Plymouth Rock
        ('plymouth rock',     'plymouth rock',        None),
        ('plymouth rock',     'plymouth rock insurance', None),
        # Chubb
        ('chubb',             'chubb',                None),
        ('chubb',             'chubb insurance',      None),
        # Policygenius two-word variant
        ('policygenius',      'policy genius',        None),
        ('policygenius',      'policygenius com',     None),
        # GooseHead standalone
        ('goosehead insurance', 'goosehead',          None),
        # Erie standalone
        ('erie insurance',    'erie',                 None),
        # AAA variants
        ('aaa',               'aaa homeowners',       None),
        ('aaa',               'aaa home insurance',   None),
        ('aaa',               'triple a insurance',   None),
        # Costco misspelling
        ('costco',            'costo',                None),
        ('costco',            'costo home insurance', None),
        # Liberty (standalone -- matches 'liberty mutual' queries without the mutual)
        ('liberty mutual',    'liberty',              None),
        # Homeowners of America
        ('homeowners of america', 'homeowners of america', None),
        # SaveMaxHomes
        ('savemaxhomes',      'savemaxhomes',         None),
        ('savemaxhomes',      'save max homes',       None),
        # CSAA
        ('csaa',              'csaa',                 None),
        ('csaa',              'csaa insurance',       None),
        # Kin -- short brand name, needs explicit variants
        # (too short for fuzzy; dot-com variants break matching)
        ('kin insurance',     'kin com',              None),
        ('kin insurance',     'kin.com',              None),
        ('kin insurance',     'kin com reviews',      None),
        ('kin insurance',     'kin com insurance',    None),
        ('kin insurance',     'kin com txquote',      None),
        ('kin insurance',     'kin com azquote',      None),
        ('kin insurance',     'kin.com azquote',      None),
        ('kin insurance',     'kin.com tx quotes',    None),
        ('kin insurance',     'kin.com/colorado quote', None),
        ('kin insurance',     'kin com tx quote',     None),
        ('kin insurance',     'kininsurance.com',     None),
        ('kin insurance',     'is kin com legit',     None),
        ('kin',               'kin com',              None),
        ('kin',               'kin.com',              None),
        # NerdWallet
        ('nerdwallet',        'nerdwallet',           None),
        ('nerdwallet',        'nerdwallet home insurance', None),
        ('nerdwallet',        'nerdwallet homeowners insurance', None),
        # California Fair Plan
        ('california fair plan', 'california fair plan',         None),
        ('california fair plan', 'california fair plan insurance', None),
        ('california fair plan', 'california fair plan quote',   None),
        ('california fair plan', 'california fair plan calculator', None),
        ('california fair plan', 'california fair plan login',   None),
        ('california fair plan', 'california fair plan login app', None),
        ('california fair plan', 'fair plan',                    None),
        # California Earthquake Authority
        ('california earthquake authority', 'california earthquake authority', None),
        ('california earthquake authority', 'earthquake authority com protect', None),
        # Grange
        ('grange insurance',  'grange insurance',     None),
        ('grange mutual',     'grange insurance',     None),
        # Hochheim
        ('hochheim prairie',  'hochheim insurance',   None),
        ('hochheim prairie',  'hochheim prairie',     None),
        # Cumberland Mutual
        ('cumberland mutual', 'cumberland mutual insurance', None),
        # Matic
        ('matic',             'matic insurance',      None),
        ('matic',             'matic com',            None),
        # Texas Windstorm
        ('texas windstorm',   'texas windstorm',      None),
        ('texas windstorm',   'texas fair plan',      None),
        ('texas windstorm',   'texas fair plan agents', None),
        # Unverified aggregators
        ('honeyquote',        'honeyquote',           None),
        ('orion180',          'orion180',              None),
        ('covertree',         'covertree',             None),
        ('the canopy group',  'the canopy group',      None),
        ('the canopy group',  'the canopy group reviews', None),
        ('truequote',         'true quote home',       None),
        ('ratecompare',       'ratecompare org',       None),
        ('cfpnet',            'cfpnet',                None),
        ('cfpnet',            'www cfpnet com',        None),
        ('solvablehome',      'solvable home insurance', None),
        ('spence mathews',    'spence mathews',        None),
        ('bristol west',      'bristol west',          None),
        ('american bankers',  'american bankers ins co', None),
        ('hanover insurance', 'hanover ins co home owners', None),
        ('american home shield', 'american home shield', None),
        ('american home shield', 'ahs com abc',         None),
        # SmartFinancial variants
        ('smartfinancial',    'smart financial',       None),
        ('smartfinancial',    'smartfinancial com',    None),
        ('smartfinancial',    'smartfinancial reviews', None),
        # Foremost (Farmers sub-brand)
        ('foremost insurance', 'foremost',             None),
        # Lemonde/lemonade misspelling
        ('lemonade',          'lemonde',               None),
        ('lemonade',          'lemonde/home.com',      None),
        # HomeQuote Explorer
        ('homequote explorer', 'homequote explorer',   None),
        ('homequote explorer', 'homequote',            None),
        # Kin vom (typo for kin com)
        ('kin insurance',     'kin vom',               None),
        ('kin insurance',     'kin home insurancew',   None),

        # ── Medicare-specific variants (merged from 07_seed_brand_registry_medicare_patch.py) ──
        # Format: (brand_normalized_name, keyword_text, match_type)
        ('unitedhealthcare',  'unitedhealthcare',  None),
        ('unitedhealthcare',  'united healthcare',  None),
        ('unitedhealthcare',  'united health care',  None),
        ('unitedhealthcare',  'uhc',  None),
        ('unitedhealthcare',  'uhc com',  None),
        ('unitedhealthcare',  'uhc medicare',  None),
        ('unitedhealthcare',  'united healthcare medicare',  None),
        ('unitedhealthcare',  'united healthcare medicare advantage',  None),
        ('aarp medicare',  'aarp medicare supplement',  None),
        ('aarp medicare',  'aarp medigap',  None),
        ('aarp medicare',  'aarp medicare advantage',  None),
        ('aarp medicare',  'aarp medicare plans',  None),
        ('humana',  'humana medicare',  None),
        ('humana',  'humana medicare advantage',  None),
        ('humana',  'humana medicare supplement',  None),
        ('humana',  'humana medigap',  None),
        ('humana',  'humana gold plus',  None),
        ('humana',  'humana part d',  None),
        ('humana gold plus',  'humana gold plus',  None),
        ('humana gold plus',  'humana gold plus hmo',  None),
        ('aetna',  'aetna',  None),
        ('aetna',  'aetna medicare',  None),
        ('aetna',  'aetna medicare advantage',  None),
        ('aetna',  'aetna medicare supplement',  None),
        ('aetna',  'aetna medigap',  None),
        ('aetna',  'aetna medicare part d',  None),
        ('aetna',  'aetna com',  None),
        ('cigna',  'cigna',  None),
        ('cigna',  'cigna medicare',  None),
        ('cigna',  'cigna medicare supplement',  None),
        ('cigna',  'cigna medigap',  None),
        ('cigna',  'cigna medicare advantage',  None),
        ('cigna',  'cigna com',  None),
        ('wellcare',  'wellcare',  None),
        ('wellcare',  'wellcare medicare',  None),
        ('wellcare',  'wellcare medicare advantage',  None),
        ('wellcare',  'wellcare part d',  None),
        ('wellcare',  'well care medicare',  None),
        ('anthem',  'anthem medicare',  None),
        ('anthem',  'anthem medicare advantage',  None),
        ('anthem',  'anthem blue cross medicare',  None),
        ('blue cross blue shield',  'blue cross medicare',  None),
        ('blue cross blue shield',  'bcbs medicare',  None),
        ('blue cross blue shield',  'blue cross blue shield medicare',  None),
        ('blue cross blue shield',  'blue cross medigap',  None),
        ('bcbs',  'bcbs',  None),
        ('bcbs',  'bcbs medicare',  None),
        ('kaiser permanente',  'kaiser medicare',  None),
        ('kaiser permanente',  'kaiser medicare advantage',  None),
        ('kaiser permanente',  'kaiser permanente',  None),
        ('kaiser permanente',  'kaiser permanente medicare',  None),
        ('mutual of omaha',  'mutual of omaha',  None),
        ('mutual of omaha',  'mutual of omaha medicare supplement',  None),
        ('mutual of omaha',  'mutual of omaha medigap',  None),
        ('mutual of omaha',  'mutual of omaha medicare',  None),
        ('mutual of omaha',  'moo medicare',  None),
        ('transamerica',  'transamerica',  None),
        ('transamerica',  'transamerica medicare supplement',  None),
        ('transamerica',  'transamerica medigap',  None),
        ('devoted health',  'devoted health',  None),
        ('devoted health',  'devoted health medicare',  None),
        ('devoted health',  'devoted medicare',  None),
        ('clover health',  'clover health',  None),
        ('clover health',  'clover health medicare',  None),
        ('gohealth',  'gohealth',  None),
        ('gohealth',  'go health',  None),
        ('gohealth',  'gohealth medicare',  None),
        ('gohealth',  'gohealth insurance',  None),
        ('ehealth',  'ehealth',  None),
        ('ehealth',  'ehealthinsurance',  None),
        ('ehealth',  'ehealth medicare',  None),
        ('ehealth',  'ehealth insurance',  None),
        ('silverscript',  'silverscript',  None),
        ('silverscript',  'silverscript part d',  None),
        ('silverscript',  'silverscript medicare',  None),
        ('silverscript',  'silver script',  None),
        ('colonial penn',  'colonial penn',  None),
        ('colonial penn',  'colonial penn medicare',  None),
        ('colonial penn',  'colonial penn medicare supplement',  None),
        ('globe life',  'globe life',  None),
        ('globe life',  'globe life medicare supplement',  None),
        ('globe life',  'globe life insurance',  None),
        ('boomer benefits',  'boomer benefits',  None),
        ('boomer benefits',  'boomer benefits medicare',  None),
        ('healthmarkets',  'healthmarkets',  None),
        ('healthmarkets',  'health markets',  None),
        ('healthmarkets',  'healthmarkets medicare',  None),
        ('hartford',  'hartford medicare supplement',  None),
        ('hartford',  'aarp hartford medicare',  None),
        ('surebridge',  'surebridge medicare',  None),
        ('surebridge',  'surebridge medicare supplement',  None),

        # ── Brand miss fixes from 10_tagging_diagnostic.py section [5] ──────
        # Hartford variants
        ('hartford',      'thehartford',                 None),
        ('hartford',      'thehartford com',             None),
        ('hartford',      'thehartford com requestquote', None),

        # CSAA variants (AAA California affiliate)
        ('csaa insurance', 'csaa',                       None),
        ('csaa insurance', 'csaaa',                      None),
        ('csaa insurance', 'csaa insurance',             None),
        ('csaa insurance', 'csaa home insurance',        None),

        # Bamboo variants
        ('bamboo',        'bamboo ins',                  None),
        ('bamboo',        'bamboo house insurance',      None),
        ('bamboo',        'bamboo house',                None),
        ('bamboo',        'home insurance bamboo',       None),

        # Mercury variants
        ('mercury',       'mercury homeowners insurance', None),
        ('mercury',       'mercury homeowners',           None),
        ('mercury',       'mercury house insurance',      None),
        ('mercury',       'mercury fire insurance',       None),
        ('mercury',       'mercury condo insurance',      None),
        ('mercury',       'mercury home owner insurance', None),
        ('mercury',       'mercury home owners insurance', None),

        # Elephant variants
        ('elephant',      'elephant homeowners insurance', None),
        ('elephant',      'elephant home insurance quote', None),
        ('elephant',      'elephant homeowners',          None),

        # Amica variants
        ('amica',         'amicainsurance com',           None),
        ('amica',         'amica insurance com',          None),

        # TrueQuote variants
        ('truequote',     'truequotehome',                None),
        ('truequote',     'truequote home insurance',     None),
        ('truequote',     'truequote home',               None),

        # Regional carrier variants -- matches brand normalized names above
        ('tgs insurance',    'tgs insurance',             None),
        ('tgs insurance',    'tgs insurance agency',      None),
        ('tgs insurance',    'tgs insurance agency houston tx', None),
        ('loggerhead insurance', 'loggerhead insurance',  None),
        ('loggerhead insurance', 'loggerhead insurance reviews', None),
        ('connect insurance', 'connect insurance',        None),
        ('kessler insurance', 'kessler insurance',        None),
        ('otto insurance',    'otto home insurance',      None),
        ('otto insurance',    'otto home insurance reviews', None),
        ('otto insurance',    'otto insurance reviews',   None),
        ('alliance insurance', 'alliance insurance company', None),
        ('geovera insurance', 'geovera earthquake insurance', None),
        ('geovera insurance', 'geovera insurance',        None),
        ('matic insurance',   'matic home insurance',     None),
        ('matic insurance',   'matic insurance',          None),
        ('selectquote',       'selectquote',              None),
        ('selectquote',       'selectquote auto and home insurance', None),
        ('csaa insurance',    'csaa',                     None),
        ('abana insurance',   'abana insurance',          None),
        ('abana insurance',   'a abana',                  None),
        ('abana insurance',   'a abana insurance',        None),
        ('2go insurance',     '2 go insurance',           None),
        ('2go insurance',     '2go insurance',            None),
        ('a1 general insurance', 'a1 general',            None),
        ('a1 general insurance', 'a1 general insurance',  None),
        ('truequote',         'truequotehome',            None),
    ]

    import re as _re
    def _normalize(t):
        return _re.sub(r'\s+', ' ', t.lower().strip())

    for brand_norm, kw_text, match_type in SUPPLEMENTAL:
        brand_id = brand_ids.get(brand_norm)
        if brand_id is None:
            # Try to find it
            row = cursor.execute(
                "SELECT id FROM brand_registry WHERE normalized_name = ?",
                (brand_norm,)
            ).fetchone()
            if row:
                brand_id = row["id"]
            else:
                skipped += 1
                continue

        norm_kw = _normalize(kw_text)
        try:
            cursor.execute("""
                INSERT INTO brand_registry_keywords
                    (brand_id, keyword_text, normalized, match_type, source_sheet)
                VALUES (?, ?, ?, ?, 'supplemental')
                ON CONFLICT (normalized, match_type) DO NOTHING
            """, (brand_id, kw_text, norm_kw, match_type))
            if cursor.rowcount > 0:
                inserted += 1
        except Exception as e:
            skipped += 1

    conn.commit()
    print(f"  Supplemental variants: {inserted} inserted, {skipped} skipped/existing")



def _patch_brand_verticals(conn):
    """
    Ensures brands already in the registry have correct verticals.
    Runs after seed_brand_registry to catch brands that existed before
    the Medicare patch was merged in.
    """
    cursor  = conn.cursor()
    updated = 0
    PATCHES = [
        ('humana',    'medicare'),
        ('hartford',  'auto,home,medicare'),
        ('aarp',      'auto,home,medicare'),
        ('surebridge', 'medicare'),
    ]
    for norm, correct_verticals in PATCHES:
        row = cursor.execute(
            'SELECT id, verticals FROM brand_registry WHERE normalized_name = ?',
            (norm,)
        ).fetchone()
        if not row:
            continue
        current = row['verticals'] or ''
        verts_set = set(v.strip() for v in current.split(',') if v.strip())
        correct_set = set(v.strip() for v in correct_verticals.split(',') if v.strip())
        if correct_set.issubset(verts_set):
            continue
        merged = ','.join(sorted(verts_set | correct_set))
        cursor.execute(
            'UPDATE brand_registry SET verticals = ? WHERE normalized_name = ?',
            (merged, norm)
        )
        print(f'  Patched verticals: {norm} -> {merged}')
        updated += 1
    conn.commit()
    if updated == 0:
        print('  No vertical patches needed')


def main():
    print("=" * 65)
    print("07_seed_brand_registry.py")
    print("=" * 65)

    if not WORKBOOK_PATH.exists():
        print(f"\nERROR: Workbook not found at:")
        print(f"  {WORKBOOK_PATH}")
        print("\nPlace the workbook at the path above and re-run.")
        print("Expected filename: Buyer_Brand_Negative_List.xlsx")
        return

    conn = get_conn()
    cursor = conn.cursor()

    # Step 1: Schema migration
    print("\n[1] Migrating schema...")
    migrate_schema(conn)

    # Step 2: Seed brand registry
    print(f"\n[2] Seeding brand_registry ({len(BRAND_DATA)} national brands)...")
    brand_ids = seed_brand_registry(conn, BRAND_DATA + MEDICARE_BRAND_DATA)

    # Step 2c: Seed regional carrier brands
    print(f"\n[2c] Seeding regional carrier brands ({len(REGIONAL_BRAND_DATA)} brands)...")
    regional_ids = seed_brand_registry(conn, REGIONAL_BRAND_DATA)
    brand_ids.update(regional_ids)

    # Step 2d: Seed Confie competitor agency network (identity only; aliases
    # deferred to the confie_negkw.py guard reconciliation — see
    # load_confie_brand_data docstring). Skips already-seeded names (e.g. TGS).
    print(f"\n[2d] Seeding Confie network from {CONFIE_YAML_PATH.name}...")
    confie_data = load_confie_brand_data(CONFIE_YAML_PATH, set(brand_ids.keys()))
    if confie_data:
        confie_ids = seed_brand_registry(conn, confie_data)
        brand_ids.update(confie_ids)

    # Step 2b: Add supplemental keyword variants for brands that appear
    # as standalone short-form queries (no 'insurance' word in query text).
    # These are caught by Layer 1 exact match before the relevance gate.
    print(f"\n[2b] Adding supplemental keyword variants...")
    seed_supplemental_variants(conn, brand_ids)

    # Step 3: Load keyword variants from workbook
    print(f"\n[3] Loading keyword misspellings from workbook...")
    # Patch verticals on brands whose vertical list needs medicare added
    # (Handles brands that predate the Medicare patch being merged in)
    _patch_brand_verticals(conn)

    total_kw, skipped_kw = seed_brand_keywords_from_workbook(
        conn, brand_ids, WORKBOOK_PATH
    )

    # Summary
    total_brands = cursor.execute(
        "SELECT COUNT(*) FROM brand_registry"
    ).fetchone()[0]
    total_kw_rows = cursor.execute(
        "SELECT COUNT(*) FROM brand_registry_keywords"
    ).fetchone()[0]

    restricted = cursor.execute(
        "SELECT COUNT(*) FROM brand_registry WHERE bid_status = 'restricted'"
    ).fetchone()[0]
    conditional = cursor.execute(
        "SELECT COUNT(*) FROM brand_registry WHERE bid_status = 'conditional'"
    ).fetchone()[0]
    approved = cursor.execute(
        "SELECT COUNT(*) FROM brand_registry WHERE bid_status = 'approved'"
    ).fetchone()[0]

    print(f"\n{'='*65}")
    print("SEED COMPLETE")
    print(f"{'='*65}")
    print(f"  Brands in registry        : {total_brands:>8,}")
    print(f"    restricted              : {restricted:>8,}")
    print(f"    conditional             : {conditional:>8,}")
    print(f"    approved                : {approved:>8,}")
    print(f"  Brand keyword variants    : {total_kw_rows:>8,}")
    print(f"  Keywords loaded           : {total_kw:>8,}")
    print(f"  Keywords skipped (dup)    : {skipped_kw:>8,}")

    # Show brand distribution across verticals
    print(f"\n  Brands by vertical:")
    for vertical in ['home', 'auto', 'medicare', 'life']:
        count = cursor.execute(
            "SELECT COUNT(*) FROM brand_registry WHERE verticals LIKE ?",
            (f'%{vertical}%',)
        ).fetchone()[0]
        print(f"    {vertical:<12} : {count:>4,} brands")

    # Show national vs regional carrier breakdown
    print(f"\n  Carrier scope breakdown:")
    national_count = cursor.execute("""
        SELECT COUNT(*) FROM brand_registry
        WHERE brand_category IN (
            'carrier_approved', 'carrier_restricted_partner',
            'carrier_restricted_requested', 'carrier_conditional'
        )
    """).fetchone()[0]
    regional_count = cursor.execute("""
        SELECT COUNT(*) FROM brand_registry
        WHERE brand_category = 'carrier_regional'
    """).fetchone()[0]
    aggregator_count = cursor.execute("""
        SELECT COUNT(*) FROM brand_registry
        WHERE brand_category LIKE 'aggregator%'
    """).fetchone()[0]
    print(f"    {'national carriers':<20} : {national_count:>4,}")
    print(f"    {'regional carriers':<20} : {regional_count:>4,}")
    print(f"    {'aggregators':<20} : {aggregator_count:>4,}")

    print(f"\n  Regional carriers seeded:")
    regional_rows = cursor.execute("""
        SELECT brand_name, verticals
        FROM brand_registry
        WHERE brand_category = 'carrier_regional'
        ORDER BY brand_name
    """).fetchall()
    for r in regional_rows:
        print(f"    {r['brand_name']:<35} [{r['verticals']}]")

    # Show top 10 brands by keyword variant count
    print(f"\n  Top 10 brands by keyword variant count:")
    rows = cursor.execute("""
        SELECT br.brand_name, COUNT(brk.id) as kw_count
        FROM brand_registry br
        LEFT JOIN brand_registry_keywords brk ON brk.brand_id = br.id
        GROUP BY br.id
        ORDER BY kw_count DESC
        LIMIT 10
    """).fetchall()
    for r in rows:
        print(f"    {r['brand_name']:<35} {r['kw_count']:>6,} variants")

    conn.close()
    print("\nDone. Ready for Step 3: tagger script.")


if __name__ == "__main__":
    main()
