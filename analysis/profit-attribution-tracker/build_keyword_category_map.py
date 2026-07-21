#!/usr/bin/env python3
"""
build_keyword_category_map.py
=============================
Profit Attribution Framework v1.2 -- builds `keyword_category_map`, the
versioned framework artifact resolving every Google Home placement to exactly
one category. The weekly waterfall and (later) dashboard are dumb consumers
of this table; classification logic lives HERE and nowhere downstream.

OWNERSHIP & CADENCE (framework section 8 Q1)
--------------------------------------------
Owned by the profit-attribution thread; lives in
analysis\\profit-attribution-tracker\\. Runs weekly AFTER the 18 -> 16 -> 17
pipeline, BEFORE the waterfall. Not a kw_db_scripts pipeline stage.

CLASSIFICATION FLOW (frozen, section 8 Q1)
------------------------------------------
Source rows: keyword_instances (ALL placements, account 1) -- NOT
keyword_current filtered to serving, because placements paused mid-window
still carry window performance and must classify.

  keyword text empty/garbage              -> unmapped   (reason: empty_text)
  brand_matcher.match(text) -> no match   -> generic
  matched, brand_category aggregator_%    -> aggregator_competitor
  matched carrier, home-vertical gate FAILS -> unmapped (reason: non_home_carrier)
  gate passes, carrier_size_map lookup:
      membership_affinity                 -> membership_affinity
      large                               -> carrier_large
      small                               -> carrier_small
      no current row                      -> unmapped   (reason: size_unassigned)

STATE TIER (section 8 Q2)
-------------------------
Classification-time snapshot: campaign_id -> tier via CAMPAIGN_TIER_MAP
(immutable ids; tier shells map directly); else the campaign's
parsed_geo_state -> tier via STATE_TIER_MAP (geo-model assignment); else
'unassigned' (reported loudly with spend-free campaign list -- fill the maps).
COMPLETE THE TWO MAPS IN CONFIG BEFORE FIRST APPLY.

VERSIONING (section 7)
----------------------
Append-only: a row is inserted when a placement is NEW or its classification
tuple (category, brand, size, tier, match_type) CHANGED vs. its latest row.
Prior rows are never updated. Current state = latest effective_date per
placement (companion view `keyword_category_map_current` is created for
consumers). Point-in-time = latest row with effective_date <= date of
interest.

DISCIPLINE
----------
Explicit --db required | DRY-RUN BY DEFAULT (--apply to write) | append-only
| small-DB tripwire | single transaction on write.

USAGE
-----
    python build_keyword_category_map.py --db "C:\\...\\paid_search.db"            # dry run
    python build_keyword_category_map.py --db "C:\\...\\paid_search.db" --apply    # write
"""

from __future__ import annotations

import argparse
import datetime as dt
import importlib
import os
import sqlite3
import sys
from collections import Counter

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------
ACCOUNT_ID = 1
MIN_DB_BYTES = 1_000_000_000          # small-DB tripwire (~1 GB)
FRAMEWORK_VERSION = "fw_v1.2"
SCRIPT_ID = "build_keyword_category_map.py"

KI_TABLE = "keyword_instances"
KI_COLS = {
    "account_id": "account_id",
    "placement_id": "ad_group_criterion_id",
    "campaign_id": "campaign_id",
    "keyword_text": "keyword_text",
    "match_type": "match_type",
}

CAMPAIGNS_TABLE = "campaigns"
CAMPAIGNS_COLS = {
    "campaign_id": "campaign_id",
    "account_id": "account_id",
    "parsed_geo_state": "parsed_geo_state",
}

REGISTRY_TABLE = "brand_registry"
REGISTRY_COLS = {
    "brand_name": "brand_name",
    "normalized_name": "normalized_name",
    "brand_category": "brand_category",
    "verticals": "verticals",
}

SIZE_TABLE = "carrier_size_map"        # seeded by seed_carrier_size_map.py

BRAND_MATCHER_MODULE = "brand_matcher"

# --- Tier maps: derived from google_generate_tier_import_v3.py (2026-07-14).
# Physical shells cascaded one tier up during the rollout; mapping is by
# IMMUTABLE campaign_id (keys normalized to str; see resolve_tier).
CAMPAIGN_TIER_MAP: dict[str, str] = {
    "23496334752": "tier1",              # "Tier Two" shell  -> Tier1RPL80Plus (wk1 6/16)
    "23530860942": "tier2",              # "Tier Three" shell -> Tier2RPL70Plus (wk2 6/30)
    "23586637678": "tier3_consolidated", # "Tier Four" shell -> Tier3RPL50Plus merged bottom (wk3 7/8)
    "23428389081": "retired",            # "Tier One" shell  -> zz-RETIRED, emptied+paused 7/8
}
# Per-state resolution for legacy per-state campaigns (via parsed_geo_state
# or name parsing). Post-consolidation, every targeted state not in tier1/
# tier2 lives in the merged bottom -- so unlisted states default to
# tier3_consolidated (see STATE_DEFAULT_TIER).
TIER1_STATES = {"UT", "OH", "AL", "MN", "WI", "OR", "DE"}   # DE per wk1 config -- confirm vs live
TIER2_STATES = {"AZ", "GA", "NJ", "NC", "PA", "MD", "IL", "KS", "MO", "NM"}
STATE_DEFAULT_TIER = "tier3_consolidated"
STATE_TIER_MAP: dict[str, str] = (
    {s: "tier1" for s in TIER1_STATES} | {s: "tier2" for s in TIER2_STATES})
# Full-name variants (parsed_geo_state may hold names, not codes)
_STATE_NAMES = {
    "Utah": "UT", "Ohio": "OH", "Alabama": "AL", "Minnesota": "MN",
    "Wisconsin": "WI", "Oregon": "OR", "Delaware": "DE", "Arizona": "AZ",
    "Georgia": "GA", "New Jersey": "NJ", "North Carolina": "NC",
    "Pennsylvania": "PA", "Maryland": "MD", "Illinois": "IL",
    "Kansas": "KS", "Missouri": "MO", "New Mexico": "NM",
}
for _name, _code in _STATE_NAMES.items():
    STATE_TIER_MAP[_name] = STATE_TIER_MAP[_code]

# Full state-name -> code map for parsing legacy campaign NAMES
# (e.g. 'HOME INSURANCE-TEXAS-SEARCH-MOBILE-USA-STE'). Uppercase keys;
# includes known in-name misspellings.
STATE_NAME_TO_CODE = {
    "ALABAMA": "AL", "ALASKA": "AK", "ARIZONA": "AZ", "ARKANSAS": "AR",
    "CALIFORNIA": "CA", "COLORADO": "CO", "CONNECTICUT": "CT",
    "DELAWARE": "DE", "DISTRICT OF COLUMBIA": "DC", "FLORIDA": "FL",
    "GEORGIA": "GA", "HAWAII": "HI", "IDAHO": "ID", "ILLINOIS": "IL",
    "INDIANA": "IN", "IOWA": "IA", "KANSAS": "KS", "KENTUCKY": "KY",
    "LOUISIANA": "LA", "LOUISANA": "LA",          # misspelling in live name
    "MAINE": "ME", "MARYLAND": "MD", "MASSACHUSETTS": "MA",
    "MICHIGAN": "MI", "MINNESOTA": "MN", "MISSISSIPPI": "MS",
    "MISSOURI": "MO", "MONTANA": "MT", "NEBRASKA": "NE", "NEVADA": "NV",
    "NEW HAMPSHIRE": "NH", "NEW JERSEY": "NJ", "NEW MEXICO": "NM",
    "NEW YORK": "NY", "NORTH CAROLINA": "NC", "NORTH DAKOTA": "ND",
    "OHIO": "OH", "OKLAHOMA": "OK", "OREGON": "OR", "PENNSYLVANIA": "PA",
    "RHODE ISLAND": "RI", "SOUTH CAROLINA": "SC", "SOUTH DAKOTA": "SD",
    "TENNESSEE": "TN", "TEXAS": "TX", "UTAH": "UT", "VERMONT": "VT",
    "VIRGINIA": "VA", "WASHINGTON": "WA", "WEST VIRGINIA": "WV",
    "WISCONSIN": "WI", "WYOMING": "WY",
}


def parse_state_from_name(campaign_name):
    """Extract a state code from a hyphen-delimited legacy campaign name.
    Returns a 2-letter code or None. Longest-token match wins (so
    'WEST VIRGINIA' beats 'VIRGINIA' when both could apply)."""
    if not campaign_name:
        return None
    tokens = [t.strip().upper() for t in str(campaign_name).split("-")]
    best = None
    for tok in tokens:
        if tok in STATE_NAME_TO_CODE and (best is None or len(tok) > len(best)):
            best = tok
    return STATE_NAME_TO_CODE[best] if best else None

CATEGORIES = ("generic", "carrier_large", "carrier_small",
              "membership_affinity", "aggregator_competitor", "unmapped")

DDL = f"""
CREATE TABLE IF NOT EXISTS keyword_category_map (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id            INTEGER NOT NULL,
    ad_group_criterion_id TEXT NOT NULL,
    campaign_id           INTEGER,
    keyword_category      TEXT NOT NULL CHECK (keyword_category IN
        {CATEGORIES!r}),
    carrier_brand         TEXT,
    carrier_size          TEXT,
    state_tier            TEXT,
    match_type            TEXT,
    effective_date        TEXT NOT NULL,
    version               TEXT NOT NULL,
    classified_by         TEXT NOT NULL,
    UNIQUE(account_id, ad_group_criterion_id, effective_date)
);
"""

VIEW_DDL = """
CREATE VIEW IF NOT EXISTS keyword_category_map_current AS
SELECT m.*
FROM keyword_category_map m
JOIN (
    SELECT account_id, ad_group_criterion_id,
           MAX(effective_date) AS max_eff
    FROM keyword_category_map
    GROUP BY account_id, ad_group_criterion_id
) latest
  ON  latest.account_id            = m.account_id
  AND latest.ad_group_criterion_id = m.ad_group_criterion_id
  AND latest.max_eff               = m.effective_date;
"""


def fail(msg: str, code: int = 1) -> None:
    print("\n[FAIL] " + msg)
    sys.exit(code)


# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------
def table_columns(conn: sqlite3.Connection, name: str) -> list[str]:
    try:
        return [r[1] for r in conn.execute(
            f"PRAGMA table_info('{name}')").fetchall()]
    except sqlite3.Error:
        return []


def preflight(conn: sqlite3.Connection) -> None:
    print("=" * 74)
    print("PREFLIGHT")
    print("=" * 74)
    names = {n for (n,) in conn.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table','view')")}
    problems = []
    for label, tbl, colmap in [
        ("keyword instances", KI_TABLE, KI_COLS),
        ("campaigns", CAMPAIGNS_TABLE, CAMPAIGNS_COLS),
        ("brand registry", REGISTRY_TABLE, REGISTRY_COLS),
    ]:
        if tbl not in names:
            problems.append(f"- {label} '{tbl}' NOT FOUND")
            continue
        cols = table_columns(conn, tbl)
        missing = [v for v in colmap.values() if v not in cols]
        if missing:
            problems.append(f"- {label} '{tbl}': missing {missing}. "
                            f"Actual: {cols}")
        else:
            print(f"[OK] {label}: '{tbl}'")
    if SIZE_TABLE not in names:
        problems.append(f"- '{SIZE_TABLE}' NOT FOUND -- run "
                        f"seed_carrier_size_map.py --apply first.")
    else:
        n = conn.execute(f"SELECT COUNT(*) FROM {SIZE_TABLE} "
                         f"WHERE end_date IS NULL").fetchone()[0]
        if n == 0:
            problems.append(f"- '{SIZE_TABLE}' exists but has ZERO current "
                            f"assignments -- seed it first.")
        else:
            print(f"[OK] {SIZE_TABLE}: {n} current assignment(s)")
    if problems:
        print("\nPreflight problems:")
        print("\n".join(problems))
        fail("Preflight failed. Nothing was classified.")
    print()


# ---------------------------------------------------------------------------
# Brand matcher adapter (same auto-detection as validate_carrier_size_axis)
# ---------------------------------------------------------------------------
MATCH_METHOD_NAMES = ("match_brand", "match", "resolve", "match_keyword",
                      "get_brand", "classify", "tag", "find_brand",
                      "match_text", "lookup")


def _extract_brand_name(out):
    if out is None:
        return None
    if isinstance(out, str):
        return out or None
    if isinstance(out, dict):
        for key in ("brand_name", "brand_key", "brand"):
            if key in out and isinstance(out[key], str):
                return out[key]
        return None
    for attr in ("brand_name", "brand"):
        if hasattr(out, attr):
            val = getattr(out, attr)
            if isinstance(val, str):
                return val
    if isinstance(out, (list, tuple)) and out:
        return _extract_brand_name(out[0])
    return None


def _required_params(callable_obj):
    import inspect
    try:
        sig = inspect.signature(callable_obj)
    except (ValueError, TypeError):
        return None
    return [pm for pm in sig.parameters.values()
            if pm.name != "self"
            and pm.default is inspect.Parameter.empty
            and pm.kind in (pm.POSITIONAL_ONLY, pm.POSITIONAL_OR_KEYWORD)]


def _try_instantiate(cls, db_path: str):
    req = _required_params(cls.__init__)
    if req is None:
        return None
    attempts = []
    if len(req) == 0:
        attempts.append(lambda: cls())
    elif len(req) == 1:
        nm = req[0].name.lower()
        if any(t in nm for t in ("path", "db", "database", "file")):
            attempts.append(lambda: cls(db_path))
        if any(t in nm for t in ("conn", "connection")):
            attempts.append(lambda: cls(sqlite3.connect(db_path)))
        attempts.append(lambda: cls(db_path))
    for make in attempts:
        try:
            return make()
        except Exception:
            continue
    return None


def build_resolver(matcher_mod, db_path: str):
    import inspect
    for fn_name in MATCH_METHOD_NAMES:
        fn = getattr(matcher_mod, fn_name, None)
        if inspect.isfunction(fn):
            req = _required_params(fn)
            if req is not None and len(req) == 1:
                print(f"[OK] Using {BRAND_MATCHER_MODULE}.{fn_name}()")
                return lambda text, _f=fn: _extract_brand_name(_f(text))
    classes = [getattr(matcher_mod, n) for n in dir(matcher_mod)
               if not n.startswith("_")
               and inspect.isclass(getattr(matcher_mod, n))
               and getattr(matcher_mod, n).__module__ == matcher_mod.__name__]
    classes.sort(key=lambda c: "match" not in c.__name__.lower())
    for cls in classes:
        method_name = None
        for m in MATCH_METHOD_NAMES:
            meth = getattr(cls, m, None)
            if callable(meth):
                req = _required_params(meth)
                if req is not None and len(req) == 1:
                    method_name = m
                    break
        if method_name is None:
            continue
        inst = _try_instantiate(cls, db_path)
        if inst is None:
            continue
        meth = getattr(inst, method_name)
        print(f"[OK] Using {cls.__name__}.{method_name}()")
        return lambda text, _m=meth: _extract_brand_name(_m(text))
    fail(f"Could not auto-detect the entry point on {BRAND_MATCHER_MODULE} "
         f"-- same adapter as validate_carrier_size_axis.py; if that script "
         f"resolves, this one will.")


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------
def classify(text, brand_name, registry_row, size_by_norm):
    """Return (keyword_category, carrier_brand, carrier_size, unmapped_reason).

    Implements the frozen flow from framework section 8 Q1 exactly.
    """
    if text is None or not str(text).strip():
        return "unmapped", None, None, "empty_text"
    if brand_name is None:
        return "generic", None, None, None
    if registry_row is None:
        # matcher returned a brand the registry doesn't know -- visible
        return "unmapped", brand_name, None, "brand_not_in_registry"
    norm, brand_category, verticals = registry_row
    bc = (brand_category or "").lower()
    if bc.startswith("aggregator"):
        return "aggregator_competitor", brand_name, None, None
    if bc.startswith("carrier"):
        if "home" not in (verticals or "").lower():
            return "unmapped", brand_name, None, "non_home_carrier"
        size = size_by_norm.get(norm)
        if size == "membership_affinity":
            return "membership_affinity", brand_name, size, None
        if size == "large":
            return "carrier_large", brand_name, size, None
        if size == "small":
            return "carrier_small", brand_name, size, None
        return "unmapped", brand_name, None, "size_unassigned"
    # brand known but neither aggregator nor carrier (e.g. other taxonomy)
    return "unmapped", brand_name, None, f"brand_category={brand_category}"


def resolve_tier(campaign_id, state_by_campaign):
    cid = str(campaign_id) if campaign_id is not None else None
    if cid in CAMPAIGN_TIER_MAP:
        return CAMPAIGN_TIER_MAP[cid]
    state = state_by_campaign.get(cid)
    if state:
        s = str(state).strip()
        if s in STATE_TIER_MAP:
            return STATE_TIER_MAP[s]
        code = STATE_NAME_TO_CODE.get(s.upper())
        if code and code in STATE_TIER_MAP:
            return STATE_TIER_MAP[code]
        # Post-consolidation, any other targeted state is in the merged bottom
        return STATE_DEFAULT_TIER
    return "unassigned"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser(
        description="Build keyword_category_map (dry-run by default)")
    ap.add_argument("--db", required=True, help="Path to paid_search.db")
    ap.add_argument("--brand-matcher-dir", default=None,
                    help="DIRECTORY containing brand_matcher.py")
    ap.add_argument("--apply", action="store_true",
                    help="Write new/changed classification rows")
    ap.add_argument("--effective-date", default=None,
                    help="YYYY-MM-DD (default: today)")
    ap.add_argument("--force-small-db", action="store_true")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        fail(f"DB not found at: {args.db}")
    size = os.path.getsize(args.db)
    if size < MIN_DB_BYTES and not args.force_small_db:
        fail(f"DB is only {size/1e9:.2f} GB -- tripwire. "
             f"Use --force-small-db only if certain.")

    eff = args.effective_date or dt.date.today().isoformat()
    mode = "APPLY" if args.apply else "DRY-RUN (no writes; use --apply)"
    print("=" * 74)
    print(f"build_keyword_category_map | mode: {mode} | "
          f"effective_date: {eff} | version: {FRAMEWORK_VERSION}")
    print("=" * 74)

    conn = sqlite3.connect(args.db)
    preflight(conn)

    # --- Load reference data -------------------------------------------------
    r = REGISTRY_COLS
    registry_by_brand = {row[0]: (row[1], row[2], row[3]) for row in
                         conn.execute(
        f"SELECT {r['brand_name']}, {r['normalized_name']}, "
        f"{r['brand_category']}, {r['verticals']} FROM {REGISTRY_TABLE}")}
    size_by_norm = {row[0]: row[1] for row in conn.execute(
        f"SELECT normalized_name, carrier_size FROM {SIZE_TABLE} "
        f"WHERE end_date IS NULL")}
    c = CAMPAIGNS_COLS
    # detect a campaign-name column for diagnostics (best effort)
    camp_cols = table_columns(conn, CAMPAIGNS_TABLE)
    name_col = next((n for n in ("campaign_name", "name") if n in camp_cols),
                    None)
    name_sel = f", {name_col}" if name_col else ""
    state_by_campaign, name_by_campaign = {}, {}
    for row in conn.execute(
            f"SELECT {c['campaign_id']}, {c['parsed_geo_state']}{name_sel} "
            f"FROM {CAMPAIGNS_TABLE} WHERE {c['account_id']} = ?",
            (ACCOUNT_ID,)):
        cid = str(row[0])
        nm = row[2] if name_col else None
        state_by_campaign[cid] = row[1] or parse_state_from_name(nm)
        name_by_campaign[cid] = nm

    if not CAMPAIGN_TIER_MAP and not STATE_TIER_MAP:
        print("[WARN] Both tier maps in CONFIG are effectively empty -- "
              "every placement will get state_tier='unassigned'. Fill "
              "CAMPAIGN_TIER_MAP / STATE_TIER_MAP before relying on tiers.")

    # --- Load placements (ALL, not serving-only) -----------------------------
    k = KI_COLS
    placements = conn.execute(
        f"SELECT {k['placement_id']}, {k['campaign_id']}, "
        f"{k['keyword_text']}, {k['match_type']} "
        f"FROM {KI_TABLE} WHERE {k['account_id']} = ?",
        (ACCOUNT_ID,)).fetchall()
    if not placements:
        fail("Zero placements in keyword_instances for account "
             f"{ACCOUNT_ID}.")
    print(f"Placements to classify (all, incl. paused): {len(placements):,}")

    # --- Resolve brands (memoized per unique text) ---------------------------
    if args.brand_matcher_dir:
        d = args.brand_matcher_dir
        if os.path.isfile(d):
            d = os.path.dirname(os.path.abspath(d))
        sys.path.insert(0, d)
    try:
        matcher_mod = importlib.import_module(BRAND_MATCHER_MODULE)
    except ImportError as e:
        fail(f"Could not import {BRAND_MATCHER_MODULE}: {e}. Pass "
             f"--brand-matcher-dir (the FOLDER containing it).")
    resolver = build_resolver(matcher_mod, args.db)

    text_cache: dict[str, str | None] = {}
    def brand_of(text):
        if text not in text_cache:
            text_cache[text] = resolver(text) if text else None
        return text_cache[text]

    # --- Classify -------------------------------------------------------------
    computed = {}          # placement_id -> row tuple
    reasons = Counter()
    cats = Counter()
    tiers = Counter()
    unassigned_campaigns = Counter()
    for placement_id, campaign_id, text, match_type in placements:
        brand = brand_of(text)
        reg_row = registry_by_brand.get(brand) if brand else None
        cat, cbrand, csize, reason = classify(text, brand, reg_row,
                                              size_by_norm)
        tier = resolve_tier(campaign_id, state_by_campaign)
        computed[placement_id] = (campaign_id, cat, cbrand, csize, tier,
                                  match_type)
        cats[cat] += 1
        tiers[tier] += 1
        if reason:
            reasons[reason] += 1
        if tier == "unassigned":
            unassigned_campaigns[campaign_id] += 1

    # --- Diff vs current table state ------------------------------------------
    have_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='keyword_category_map'").fetchone() is not None
    current = {}
    if have_table:
        for row in conn.execute("""
            SELECT ad_group_criterion_id, campaign_id, keyword_category,
                   carrier_brand, carrier_size, state_tier, match_type
            FROM keyword_category_map m
            WHERE account_id = ?
              AND effective_date = (
                  SELECT MAX(effective_date) FROM keyword_category_map
                  WHERE account_id = m.account_id
                    AND ad_group_criterion_id = m.ad_group_criterion_id)
        """, (ACCOUNT_ID,)):
            current[row[0]] = tuple(row[1:])

    new_rows = {p: v for p, v in computed.items() if p not in current}
    changed = {p: v for p, v in computed.items()
               if p in current and current[p] != v}
    unchanged = len(computed) - len(new_rows) - len(changed)

    # --- Report ---------------------------------------------------------------
    print("\n" + "=" * 74)
    print("CLASSIFICATION SUMMARY")
    print("=" * 74)
    for cat in CATEGORIES:
        if cats.get(cat):
            print(f"  {cat:<22} {cats[cat]:>7,}")
    if reasons:
        print("\n  unmapped reasons:")
        for reason, n in reasons.most_common():
            print(f"    {reason:<28} {n:>7,}")
    print("\n  state_tier distribution:")
    for tier, n in tiers.most_common():
        print(f"    {tier:<22} {n:>7,}")
    if unassigned_campaigns:
        print(f"\n[WARN] {len(unassigned_campaigns)} campaign(s) resolve to "
              f"tier 'unassigned' (top 15 by placement count):")
        for cid, n in unassigned_campaigns.most_common(15):
            scid = str(cid)
            st = state_by_campaign.get(scid)
            nm = name_by_campaign.get(scid)
            print(f"    campaign_id {cid}  placements {n:,}  "
                  f"parsed_geo_state={st!r}  name={nm!r} -- add to "
                  f"CAMPAIGN_TIER_MAP or STATE_TIER_MAP")

    print(f"\nVERSIONING: new {len(new_rows):,} | changed {len(changed):,} "
          f"| unchanged (no write) {unchanged:,}")
    if changed:
        print("  sample changes (up to 10):")
        for p, v in list(changed.items())[:10]:
            print(f"    {p}: {current[p]} -> {v}")

    if not args.apply:
        conn.close()
        print("\nDry-run complete. Re-run with --apply to write "
              f"{len(new_rows) + len(changed):,} row(s).")
        return

    # --- Write (append-only, single transaction) -------------------------------
    to_write = [(ACCOUNT_ID, p, v[0], v[1], v[2], v[3], v[4], v[5],
                 eff, FRAMEWORK_VERSION, SCRIPT_ID)
                for p, v in list(new_rows.items()) + list(changed.items())]
    try:
        with conn:
            conn.execute(DDL)
            conn.execute(VIEW_DDL)
            conn.executemany("""
                INSERT INTO keyword_category_map
                (account_id, ad_group_criterion_id, campaign_id,
                 keyword_category, carrier_brand, carrier_size, state_tier,
                 match_type, effective_date, version, classified_by)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)""", to_write)
    except sqlite3.Error as e:
        fail(f"Write failed, transaction rolled back: {e}")

    n_cur = conn.execute("SELECT COUNT(*) FROM keyword_category_map_current "
                         "WHERE account_id = ?", (ACCOUNT_ID,)).fetchone()[0]
    conn.close()
    print(f"\n[OK] Wrote {len(to_write):,} row(s). "
          f"keyword_category_map_current now covers {n_cur:,} placement(s).")
    print("Done. The waterfall reads keyword_category_map_current (or "
          "point-in-time via effective_date).")


if __name__ == "__main__":
    main()
