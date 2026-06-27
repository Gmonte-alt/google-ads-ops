"""
12_seed_product_taxonomy.py  [v1]
=================================================================
Builds the PRODUCT / COVERAGE TAXONOMY in paid_search.db — a reference
dimension that classifies insurance demand by *product intent* (homeowners vs
renters vs landlord vs commercial), distinct from BRAND (who) and from the
search-query modifier columns (per-query tags).

WHY THIS EXISTS
  brand_registry answers "which company is this query about." It cannot answer
  "which PRODUCT is this query about" — and homeowners / renters / landlord /
  commercial are product distinctions, not companies. This taxonomy is the
  controlled vocabulary for product intent. It is ACCOUNT-AGNOSTIC and SHARED:
  Quote.com Home and USIA Home both read the same product/vertical tagging, so
  the dimension describes only what each product intrinsically IS.

  It deliberately does NOT carry "scope" (is this product in/out, online vs
  agent, which domain/engine). That is a per-account/per-domain DECISION, not a
  property of the product — e.g. renters may be Quote.com online-only while
  landlord/commercial may run on both Quote.com and USIA, both engines. Those
  belong in a separate per-account product↔campaign MAPPING layer, built when
  the accounts/campaigns exist (USIA launches shortly). Keeping scope out of
  here lets one shared taxonomy serve every account without conflict.

THREE GRAINS, KEPT SEPARATE (node_type)
    product            -> the policy type (homeowners, renters, landlord,
                          commercial_property). The campaign-structure driver.
    coverage_component -> parts of a policy (dwelling coverage, liability,
                          replacement cost). A finer modifier.
    peril              -> what's insured against (fire, wind, flood). Finer
                          still. (Kept distinct so 'fire'/'wind' never get
                          confused with carrier brand names — cf. the brand
                          registry 'fire' cluster.)  [v1: perils seeded sparsely;
                          extend later.]

vertical  (intrinsic — home vs commercial; matches brand_registry's vertical)
    The home-vs-commercial split IS a property of the product. Whether a given
    account bids it is the per-account mapping layer's job, not this column's.

This script is idempotent: the dimension upserts on product_code; terms use
ON CONFLICT(normalized, match_type) DO NOTHING. Run with --dry-run to inspect
the seed without writing.

    python 12_seed_product_taxonomy.py --dry-run
    python 12_seed_product_taxonomy.py
"""

import argparse
import re
import sqlite3
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
DB = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")
SOURCE = "12_seed_product_taxonomy.py v1"
EFFECTIVE = "2026-06-18"


def normalize(text: str) -> str:
    """Lowercase, strip, collapse whitespace — mirrors 07_seed_brand_registry."""
    return re.sub(r"\s+", " ", text.lower().strip())


# ---------------------------------------------------------------------------
# DIMENSION: (product_code, product_name, node_type, parent_code, vertical,
#             description)
# product_code is the stable machine key (snake_case) used for joins, the value
# the tagger will write to search_queries, and the per-account campaign mapping.
# NO scope column — scope is a per-account/per-domain decision (see header).
# ---------------------------------------------------------------------------
TAXONOMY = [
    # ---- PRODUCTS (policy types — campaign-structure driver) ----
    ("homeowners", "Homeowners Insurance", "product", None, "home",
     "Owner-occupied primary residence (HO-3)."),
    ("condo", "Condo Insurance (HO-6)", "product", "homeowners", "home",
     "Owner-occupied condominium unit."),
    ("renters", "Renters Insurance (HO-4)", "product", None, "home",
     "Tenant contents + liability; tenant-occupied. (Note: leans online/self-service intent.)"),
    ("mobile_home", "Mobile / Manufactured Home", "product", "homeowners", "home",
     "Mobile/manufactured/modular home. (Relevant to the MOBILE ad group.)"),
    ("homeowners_extension", "Homeowners Extension (vacant/second/seasonal/land)", "product", "homeowners", "home",
     "Homeowner-side extensions kept and segmented: vacant/unoccupied home, "
     "second/seasonal home, and land (land grouped here per user-intent review; "
     "land WITH a barn/agriculture → ranch_farm, out of scope today)."),
    ("landlord", "Landlord / Rental Property", "product", None, "home",
     "Structure coverage for a rental property (DP-3); non-owner-occupied residential."),
    ("dwelling_fire", "Dwelling Fire (DP)", "product", "landlord", "home",
     "Dwelling Fire policy family (DP-1/DP-3); often non-owner-occupied."),
    ("flood", "Flood Insurance", "product", None, "home",
     "NFIP/private flood; peril-specific standalone product."),
    ("commercial_property", "Commercial Property", "product", None, "commercial",
     "Business buildings/warehouses/offices."),
    ("bop", "Business Owners Policy (BOP)", "product", "commercial_property", "commercial",
     "Bundled small-business property + liability."),

    # ---- COVERAGE COMPONENTS (parts of a policy — modifiers) ----
    ("dwelling_coverage", "Dwelling Coverage (Cov A)", "coverage_component", "homeowners", "home",
     "Structure coverage within a homeowners policy."),
    ("personal_property", "Personal Property (Cov C)", "coverage_component", "homeowners", "home",
     "Contents coverage."),
    ("liability_coverage", "Liability Coverage (Cov E)", "coverage_component", "homeowners", "home",
     "Personal liability within a policy."),
    ("replacement_cost", "Replacement Cost", "coverage_component", None, "home",
     "Valuation basis: full rebuild cost."),
    ("actual_cash_value", "Actual Cash Value", "coverage_component", None, "home",
     "Valuation basis: replacement cost minus depreciation."),
    ("building_ordinance", "Building Ordinance / Code", "coverage_component", None, "home",
     "Extra cost to rebuild to current code."),
    ("fair_rental_income", "Fair Rental Income", "coverage_component", "landlord", "home",
     "Lost-rent coverage; signals landlord intent."),
    ("business_interruption", "Business Interruption", "coverage_component", "commercial_property", "commercial",
     "Lost business income; signals commercial intent."),

    # ---- PERILS (what's insured against — finest grain; v1 sparse) ----
    ("peril_fire", "Fire (peril)", "peril", None, "home",
     "Peril, NOT a carrier. Kept in its own grain so it isn't confused with "
     "carrier brands carrying 'fire' in their legal name."),
    ("peril_wind_hail", "Wind / Hail (peril)", "peril", None, "home",
     "Windstorm/hail peril."),
    ("peril_water", "Water / Flood (peril)", "peril", None, "home",
     "Water-damage / flood peril (distinct from the flood PRODUCT)."),
]

# ---------------------------------------------------------------------------
# TERMS: (product_code, term_text, match_type, is_ambiguous)
# Only product-DISTINCTIVE terms. is_ambiguous=1 marks roots that genuinely span
# products ("building insurance" = homeowners OR commercial) — a future tagger
# must NOT hard-assign these without a corroborating signal.
# ---------------------------------------------------------------------------
TERMS = [
    # homeowners
    ("homeowners", "homeowners insurance", None, 0),
    ("homeowners", "homeowner insurance", None, 0),
    ("homeowners", "homeowners coverage", None, 0),
    ("homeowners", "home insurance", None, 0),
    ("homeowners", "insure my home", None, 0),
    ("homeowners", "insure my house", None, 0),
    ("homeowners", "house insurance", None, 0),
    ("homeowners", "ho3", None, 0),
    ("homeowners", "ho-3", None, 0),
    # condo
    ("condo", "condo insurance", None, 0),
    ("condo", "condominium insurance", None, 0),
    ("condo", "condo owners insurance", None, 0),
    ("condo", "ho6", None, 0),
    ("condo", "ho-6", None, 0),
    # renters
    ("renters", "renters insurance", None, 0),
    ("renters", "renter insurance", None, 0),
    ("renters", "renters coverage", None, 0),
    ("renters", "tenant insurance", None, 0),
    ("renters", "apartment insurance", None, 0),
    ("renters", "ho4", None, 0),
    ("renters", "ho-4", None, 0),
    # mobile_home
    ("mobile_home", "mobile home insurance", None, 0),
    ("mobile_home", "manufactured home insurance", None, 0),
    ("mobile_home", "modular home insurance", None, 0),
    ("mobile_home", "mobile home coverage", None, 0),
    # homeowners_extension
    ("homeowners_extension", "vacant home insurance", None, 0),
    ("homeowners_extension", "unoccupied home insurance", None, 0),
    ("homeowners_extension", "second home insurance", None, 0),
    ("homeowners_extension", "seasonal home insurance", None, 0),
    ("homeowners_extension", "land insurance", None, 0),
    # v3.7 — regex-caught forms (planner's brand_leak expansion; all NEW per pre-flight)
    ("homeowners_extension", "vacant property", None, 0),
    ("homeowners_extension", "vacant dwelling", None, 0),
    ("homeowners_extension", "empty home", None, 0),
    ("homeowners_extension", "empty property", None, 0),
    ("homeowners_extension", "empty house", None, 0),
    ("homeowners_extension", "holiday home", None, 0),
    ("homeowners_extension", "second home", None, 0),
    ("homeowners_extension", "2nd home", None, 0),
    ("homeowners_extension", "unoccupied home", None, 0),
    # landlord
    ("landlord", "landlord insurance", None, 0),
    ("landlord", "rental property insurance", None, 0),
    ("landlord", "rental home insurance", None, 0),
    ("landlord", "landlord policy", None, 0),
    ("landlord", "rental dwelling insurance", None, 0),
    ("landlord", "dp3", None, 0),
    ("landlord", "dp-3", None, 0),
    # dwelling_fire
    ("dwelling_fire", "dwelling fire insurance", None, 0),
    ("dwelling_fire", "dwelling fire policy", None, 0),
    ("dwelling_fire", "dp1", None, 0),
    ("dwelling_fire", "dp-1", None, 0),
    # flood
    ("flood", "flood insurance", None, 0),
    ("flood", "nfip", None, 0),
    ("flood", "flood coverage", None, 0),
    ("flood", "fema flood insurance", None, 0),
    ("flood", "private flood insurance", None, 0),
    # commercial_property
    ("commercial_property", "commercial property insurance", None, 0),
    ("commercial_property", "commercial building insurance", None, 0),
    ("commercial_property", "business property insurance", None, 0),
    ("commercial_property", "warehouse insurance", None, 0),
    ("commercial_property", "commercial real estate insurance", None, 0),
    # bop
    ("bop", "business owners policy", None, 0),
    ("bop", "bop insurance", None, 0),
    ("bop", "business owner policy", None, 0),
    ("bop", "business insurance", None, 0),
    # coverage components
    ("dwelling_coverage", "dwelling coverage", None, 0),
    ("dwelling_coverage", "dwelling protection", None, 0),
    ("personal_property", "personal property coverage", None, 0),
    ("personal_property", "contents coverage", None, 0),
    ("liability_coverage", "personal liability coverage", None, 0),
    ("replacement_cost", "replacement cost", None, 0),
    ("actual_cash_value", "actual cash value", None, 0),
    ("building_ordinance", "ordinance or law coverage", None, 0),
    ("fair_rental_income", "fair rental income", None, 0),
    ("fair_rental_income", "loss of rents", None, 0),
    ("business_interruption", "business interruption", None, 0),
    ("business_interruption", "business income coverage", None, 0),
    # perils
    ("peril_water", "water damage insurance", None, 0),

    # ---- AMBIGUOUS ROOTS (is_ambiguous=1 — best-guess product, do NOT hard-assign) ----
    ("homeowners", "building insurance", None, 1),     # UK term / could be commercial or dwelling
    ("homeowners", "dwelling insurance", None, 1),     # could be landlord (DP) vs homeowners
    ("homeowners", "property insurance", None, 1),     # very generic; could be commercial
]


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true",
                   help="Print what would be seeded; write nothing.")
    return p.parse_args()


def ensure_tables(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS product_taxonomy (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            product_code  TEXT UNIQUE NOT NULL,
            product_name  TEXT NOT NULL,
            node_type     TEXT NOT NULL,          -- product | coverage_component | peril
            parent_code   TEXT,                   -- self-ref to product_code
            vertical      TEXT,                   -- home | commercial (intrinsic)
            description   TEXT,
            notes         TEXT,
            effective_date TEXT,
            source        TEXT
        );
        CREATE TABLE IF NOT EXISTS product_taxonomy_terms (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id   INTEGER NOT NULL REFERENCES product_taxonomy(id),
            term_text    TEXT NOT NULL,
            normalized   TEXT NOT NULL,
            match_type   TEXT NOT NULL DEFAULT '',   -- '' not NULL: NULLs defeat UNIQUE dedupe in SQLite
            is_ambiguous INTEGER NOT NULL DEFAULT 0,
            source       TEXT,
            UNIQUE(normalized, match_type)
        );
        CREATE INDEX IF NOT EXISTS idx_pt_terms_norm  ON product_taxonomy_terms(normalized);
        CREATE INDEX IF NOT EXISTS idx_pt_vertical    ON product_taxonomy(vertical);
        CREATE INDEX IF NOT EXISTS idx_pt_node        ON product_taxonomy(node_type);
    """)


def seed_taxonomy(conn, dry):
    """Upsert dimension rows on product_code. Returns {product_code: id}."""
    ids, ins, upd = {}, 0, 0
    for code, name, node, parent, vert, desc in TAXONOMY:
        existing = conn.execute(
            "SELECT id FROM product_taxonomy WHERE product_code = ?", (code,)).fetchone()
        if existing:
            if not dry:
                conn.execute("""UPDATE product_taxonomy SET product_name=?, node_type=?,
                    parent_code=?, vertical=?, description=?, source=?
                    WHERE product_code=?""",
                    (name, node, parent, vert, desc, SOURCE, code))
            ids[code] = existing["id"]
            upd += 1
        else:
            if not dry:
                cur = conn.execute("""INSERT INTO product_taxonomy
                    (product_code, product_name, node_type, parent_code,
                     vertical, description, effective_date, source)
                    VALUES (?,?,?,?,?,?,?,?)""",
                    (code, name, node, parent, vert, desc, EFFECTIVE, SOURCE))
                ids[code] = cur.lastrowid
            else:
                ids[code] = -1
            ins += 1
    return ids, ins, upd


def seed_terms(conn, ids, dry):
    ins, skip, orphan = 0, 0, 0
    for code, term, mt, amb in TERMS:
        mt = mt or ''  # '' not NULL — NULLs are all-distinct in a UNIQUE constraint
        pid = ids.get(code)
        if not pid:
            print(f"  ORPHAN TERM: '{term}' → unknown product_code '{code}'")
            orphan += 1
            continue
        if dry:
            ins += 1
            continue
        cur = conn.execute("""INSERT INTO product_taxonomy_terms
            (product_id, term_text, normalized, match_type, is_ambiguous, source)
            VALUES (?,?,?,?,?,?) ON CONFLICT(normalized, match_type) DO NOTHING""",
            (pid, term, normalize(term), mt, amb, SOURCE))
        if cur.rowcount:
            ins += 1
        else:
            skip += 1
    return ins, skip, orphan


def main():
    a = parse_args()
    if not DB.exists():
        sys.exit(f"DB not found: {DB}")

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 15000")  # fail-fast-ish on lock, per DB lock discipline

    print("=" * 70)
    print(f"  12_seed_product_taxonomy.py  [v1]{'   (DRY RUN)' if a.dry_run else ''}")
    print("=" * 70)

    ensure_tables(conn)
    ids, ins_d, upd_d = seed_taxonomy(conn, a.dry_run)
    ins_t, skip_t, orphan_t = seed_terms(conn, ids, a.dry_run)

    if not a.dry_run:
        conn.commit()

    print(f"\n  product_taxonomy : {ins_d} inserted, {upd_d} updated  "
          f"({len(TAXONOMY)} nodes)")
    print(f"  taxonomy_terms   : {ins_t} inserted, {skip_t} skipped/existing, "
          f"{orphan_t} orphan")

    # Summary by node_type × vertical
    if not a.dry_run:
        print("\n  By node_type × vertical:")
        for r in conn.execute("""SELECT node_type, vertical, COUNT(*) n
            FROM product_taxonomy GROUP BY node_type, vertical
            ORDER BY node_type, vertical"""):
            print(f"    {r['node_type']:<20} {r['vertical']:<12} {r['n']}")
        print("\n  Products (node_type='product') — the campaign-structure drivers:")
        for r in conn.execute("""SELECT product_code, vertical, parent_code
            FROM product_taxonomy WHERE node_type='product'
            ORDER BY vertical, product_code"""):
            par = f"  (under {r['parent_code']})" if r['parent_code'] else ""
            print(f"    {r['product_code']:<22} [{r['vertical']}]{par}")
    else:
        print("\n  (dry run — nothing written; re-run without --dry-run to apply)")

    print("\n  NOTE: this dimension is account-agnostic. Scope (which products run on")
    print("  which account/domain/engine, online vs agent) is a per-account product↔")
    print("  campaign MAPPING — built when USIA's campaigns exist, not stored here.")
    conn.close()


if __name__ == "__main__":
    main()
