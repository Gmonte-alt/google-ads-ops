#!/usr/bin/env python3
"""
validate_carrier_size_axis.py  (rev 4 -- carrier-size axis FROZEN, framework v1.1)
=============================
Profit Attribution Framework v1.1 -- Section 4.2 carrier-size cut report.
The seed-vs-data validation gate CLOSED 2026-07-13 (brand_category seed
rejected; manual mapping approved 2026-07-14). This script now reports the
approved cut and flags bleeders / breakeven treadmills / unmapped carriers.

QUESTION THIS ANSWERS
---------------------
Does the national/regional line in the brand registry (brand_category:
carrier_approved -> 'large', carrier_regional -> 'small') actually separate
the profit pattern for Google Home (account_id=1) over the trailing 12 weeks?
Expected: "large carriers don't drive profit, small carriers do" mostly holds,
with a handful of exceptions -> those become size_override candidates.

REV 2 SCHEMA CORRECTIONS (2026-07-08, per DB architect)
-------------------------------------------------------
- keyword_performance_daily has NO account_id and NO ad_group_criterion_id;
  it keys on instance_id (-> keyword_instances.id) and dates on report_date.
  Account scoping comes through the keyword_instances join.
- brand_registry join key is brand_name (brand_key does not exist).
- FRAGMENTATION-CORRECT JOIN: performance for one placement is scattered
  across MULTIPLE instance_ids (same ad_group_criterion_id accumulated many
  keyword_instances rows over syncs). This script joins
      perf.instance_id -> keyword_instances.id -> ad_group_criterion_id
  and GROUPs BY placement, summing across ALL of a placement's instance_ids.
  Joining perf to keyword_current.latest_instance_id only would UNDERCOUNT
  GP. Never do that here or in the waterfall.

WHAT IT DOES
------------
1. Preflight: verifies tables/columns; prints diagnostics on mismatch.
2. Diagnostics: fragmentation rate (placements whose 12w perf spans >1
   instance_id) and orphan perf rows (instance_id with no keyword_instances
   parent) so any residual undercount is visible, not silent.
3. Pulls trailing-12-week perf (device='ALL'), rolled up per placement.
4. Resolves keyword text -> carrier brand via brand_matcher.py (canonical);
   filters to home-vertical carriers via the registry.
5. GP = primary_conv_value - cost_micros/1e6 per carrier; GP/lead; ranks;
   large-vs-small rollup; exception flags; verdict; CSV detail.

USAGE
-----
    python validate_carrier_size_axis.py --db "C:/path/to/paid_search.db" \
        --brand-matcher-dir "C:/Projects/google-ads-ops/campaign_db_scripts/change_history"

    (--brand-matcher-dir is the DIRECTORY containing brand_matcher.py,
     not the .py file itself.)

If it fails, send the full console output to the DB architect thread.
"""

from __future__ import annotations

import argparse
import datetime as dt
import importlib
import os
import sqlite3
import sys

import pandas as pd

# ---------------------------------------------------------------------------
# CONFIG -- corrected to actual schema per DB architect (2026-07-08)
# ---------------------------------------------------------------------------
ACCOUNT_ID = 1                     # Google Home Insurance
LOOKBACK_DAYS = 84                 # trailing 12 weeks
DEVICE_ROLLUP_VALUE = "ALL"

PERF_TABLE = "keyword_performance_daily"
PERF_COLS = {
    "instance_id": "instance_id",  # -> keyword_instances.id (NOT placement)
    "date": "report_date",
    "device": "device",
    "cost_micros": "cost_micros",
    "revenue": "primary_conv_value",
    "leads": "primary_conv_count",
}
# NOTE: keyword_performance_daily carries NO account_id / ad_group_criterion_id.
# Scope and placement identity come from the keyword_instances join.

KI_TABLE = "keyword_instances"
KI_COLS = {
    "id": "id",                              # perf.instance_id joins here
    "account_id": "account_id",
    "placement_id": "ad_group_criterion_id", # edit key (backfilled)
}

KW_VIEW = "keyword_current"        # canonical current-state view
KW_COLS = {
    "account_id": "account_id",
    "placement_id": "ad_group_criterion_id",
    "keyword_text": "keyword_text",
    "match_type": "match_type",
}

REGISTRY_TABLE = "brand_registry"
REGISTRY_COLS = {
    "brand_key": "brand_name",     # actual join key (brand_matcher returns brand_name)
    "brand_category": "brand_category",
    "verticals": "verticals",
}

# ---------------------------------------------------------------------------
# APPROVED carrier-size mapping -- framework v1.1 section 4.2, approved
# 2026-07-14. This is the interim authority until carrier_size lives in the
# DB (Phase 2). brand_category is bid-status and is NEVER used for size.
# Active carriers not listed here classify as 'unmapped_size' and are
# reported loudly -- add them here (and to the DB attribute) when they appear.
# ---------------------------------------------------------------------------
CARRIER_SIZE_MAP = {
    # membership / affinity (framework 4.1) -- AAA-branded demand in one bucket
    "Costco": "membership_affinity",
    "AAA": "membership_affinity",
    "CSAA Insurance": "membership_affinity",   # AAA affiliate
    # true DTC nationals
    "Farmers": "carrier_large",
    "Hartford": "carrier_large",
    "Geico": "carrier_large",
    "Travelers": "carrier_large",
    "American Family": "carrier_large",
    "State Farm": "carrier_large",
    "Amica": "carrier_large",
    "21st Century": "carrier_large",           # Farmers brand
    # regional / specialist / insurtech
    "Kin Insurance": "carrier_small",
    "Lemonade": "carrier_small",
    "Bamboo": "carrier_small",
    "Universal Property": "carrier_small",
    "Mercury": "carrier_small",
    "Wawanesa": "carrier_small",
    "Hochheim Prairie": "carrier_small",
    "Allied Trust": "carrier_small",
    "Homeowners of America": "carrier_small",
    "Allied": "carrier_small",
    "Plymouth Rock": "carrier_small",
    "American Home": "carrier_small",
}

BRAND_MATCHER_MODULE = "brand_matcher"

# ---------------------------------------------------------------------------
# Console encoding guard (Windows: mirrors the PYTHONIOENCODING fix)
# ---------------------------------------------------------------------------
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass


def fail(msg: str, code: int = 1) -> None:
    print("\n[FAIL] " + msg)
    print("[FAIL] Send this full output to the DB architect thread.")
    sys.exit(code)


# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------
def table_columns(conn: sqlite3.Connection, name: str) -> list[str]:
    try:
        rows = conn.execute(f"PRAGMA table_info('{name}')").fetchall()
        return [r[1] for r in rows]
    except sqlite3.Error:
        return []


def preflight(conn: sqlite3.Connection) -> None:
    print("=" * 78)
    print("PREFLIGHT")
    print("=" * 78)
    objs = conn.execute(
        "SELECT name, type FROM sqlite_master WHERE type IN ('table','view')"
    ).fetchall()
    names = {n for n, _ in objs}
    print(f"DB objects found: {len(names)}")

    problems = []
    for label, tbl, colmap in [
        ("performance table", PERF_TABLE, PERF_COLS),
        ("keyword instances", KI_TABLE, KI_COLS),
        ("keyword state view", KW_VIEW, KW_COLS),
        ("brand registry", REGISTRY_TABLE, REGISTRY_COLS),
    ]:
        if tbl not in names:
            candidates = sorted(n for n in names if any(
                tok in n.lower() for tok in tbl.lower().split("_")[:2]))
            problems.append(
                f"- {label} '{tbl}' NOT FOUND. Similar names: {candidates or 'none'}")
            continue
        cols = table_columns(conn, tbl)
        missing = [v for v in colmap.values() if v not in cols]
        if missing:
            problems.append(
                f"- {label} '{tbl}': missing columns {missing}.\n"
                f"    Actual columns: {cols}")
        else:
            print(f"[OK] {label}: '{tbl}' with all required columns")

    if problems:
        print("\nSchema mismatches (edit CONFIG block, or ask DB architect):")
        print("\n".join(problems))
        fail("Preflight failed on schema. Nothing was queried.")
    print()


# ---------------------------------------------------------------------------
# Brand matcher adapter -- THE one place to adjust if the API differs
# ---------------------------------------------------------------------------
def load_brand_matcher(matcher_dir: str | None):
    if matcher_dir:
        if os.path.isfile(matcher_dir):
            # Friendly correction: user passed the .py file, not its folder
            print(f"[INFO] --brand-matcher-dir points at a file; using its "
                  f"parent directory instead.")
            matcher_dir = os.path.dirname(os.path.abspath(matcher_dir))
        sys.path.insert(0, matcher_dir)
    try:
        return importlib.import_module(BRAND_MATCHER_MODULE)
    except ImportError as e:
        print(f"[WARN] Could not import '{BRAND_MATCHER_MODULE}': {e}")
        return None


MATCH_METHOD_NAMES = ("match_brand", "match", "resolve", "match_keyword",
                      "get_brand", "classify", "tag", "find_brand",
                      "match_text", "lookup")


def _extract_brand_name(out):
    """Normalize whatever the matcher returns into a brand_name str or None."""
    if out is None:
        return None
    if isinstance(out, str):
        return out or None
    if isinstance(out, dict):
        for key in ("brand_name", "brand_key", "brand"):
            if key in out and isinstance(out[key], str):
                return out[key]
        return None
    for attr in ("brand_name", "brand"):          # BrandMatch-style object
        if hasattr(out, attr):
            val = getattr(out, attr)
            if isinstance(val, str):
                return val
    if isinstance(out, (list, tuple)) and out:
        return _extract_brand_name(out[0])
    return None


def _introspect_dump(mod) -> str:
    """Human-readable dump of the module's public API for adapter repair."""
    import inspect
    lines = [f"--- Public API of {mod.__name__} "
             f"({getattr(mod, '__file__', '?')}) ---"]
    for name in sorted(dir(mod)):
        if name.startswith("_"):
            continue
        obj = getattr(mod, name)
        try:
            if inspect.isclass(obj) and obj.__module__ == mod.__name__:
                try:
                    sig = str(inspect.signature(obj.__init__))
                except (ValueError, TypeError):
                    sig = "(?)"
                lines.append(f"  class {name}  __init__{sig}")
                for m in sorted(dir(obj)):
                    if m.startswith("_"):
                        continue
                    attr = getattr(obj, m, None)
                    if callable(attr):
                        try:
                            msig = str(inspect.signature(attr))
                        except (ValueError, TypeError):
                            msig = "(...)"
                        lines.append(f"    .{m}{msig}")
            elif inspect.isfunction(obj):
                lines.append(f"  def {name}{inspect.signature(obj)}")
            elif not inspect.ismodule(obj) and not inspect.isclass(obj):
                lines.append(f"  {name}: {type(obj).__name__}")
        except Exception:
            lines.append(f"  {name}  (introspection failed)")
    lines.append("--- end dump ---")
    return "\n".join(lines)


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
    """Try common constructor shapes: (), (db_path), (sqlite3 connection)."""
    req = _required_params(cls.__init__)
    if req is None:
        return None
    attempts = []
    if len(req) == 0:
        attempts.append(("no-arg", lambda: cls()))
    elif len(req) == 1:
        nm = req[0].name.lower()
        if any(t in nm for t in ("path", "db", "database", "file")):
            attempts.append((f"({req[0].name}=db_path)", lambda: cls(db_path)))
        if any(t in nm for t in ("conn", "connection")):
            attempts.append((f"({req[0].name}=connection)",
                             lambda: cls(sqlite3.connect(db_path))))
        # last resort for a single unknown required arg: try the db path
        attempts.append((f"({req[0].name}=db_path, blind)",
                         lambda: cls(db_path)))
    for label, make in attempts:
        try:
            inst = make()
            print(f"[OK] Instantiated {cls.__name__} {label}")
            return inst
        except Exception:
            continue
    return None


def build_resolver(matcher_mod, db_path: str):
    """ADAPTER: return a callable keyword_text -> brand_name | None.

    Resolution order:
      1. Module-level function with a known name and one required arg.
      2. A class defined in the module exposing a known match method;
         instantiated via common constructor shapes (no-arg / db path /
         sqlite connection). Classes with 'match' in the name are tried
         first.
    On failure, prints a full introspection dump of the module so the
    exact API is visible -- paste that output back for a precise fix.
    """
    import inspect

    for fn_name in MATCH_METHOD_NAMES:                 # 1) module functions
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

    for cls in classes:                                 # 2) class-based
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
            print(f"[WARN] Found {cls.__name__}.{method_name}() but could "
                  f"not construct {cls.__name__} automatically.")
            continue
        meth = getattr(inst, method_name)
        print(f"[OK] Using {cls.__name__}.{method_name}()")
        return lambda text, _m=meth: _extract_brand_name(_m(text))

    print("\n" + _introspect_dump(matcher_mod) + "\n")
    raise AttributeError(
        f"Could not auto-detect the entry point on {BRAND_MATCHER_MODULE}. "
        f"The module's actual API is dumped above -- paste this full output "
        f"back to the profit-attribution thread for a one-line adapter fix.")


def registry_fallback_match(kw_text: str, registry: pd.DataFrame,
                            brand_key_col: str) -> str | None:
    """DIRECTIONAL ONLY fallback: longest brand-name substring match."""
    t = f" {kw_text.lower()} "
    best = None
    for bk in registry[brand_key_col].dropna().astype(str):
        needle = bk.lower().replace("_", " ").strip()
        if needle and needle in t and (best is None or len(needle) > len(best)):
            best = bk
    return best


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser(
        description="Carrier-size axis validation (framework 4.2 gate)")
    ap.add_argument("--db", required=True, help="Path to paid_search.db")
    ap.add_argument("--brand-matcher-dir", default=None,
                    help="DIRECTORY containing brand_matcher.py (the folder, "
                         "not the .py file)")
    ap.add_argument("--allow-registry-fallback", action="store_true",
                    help="If brand_matcher import fails, use registry "
                         "substring matching (DIRECTIONAL ONLY)")
    ap.add_argument("--end-date", default=None,
                    help="YYYY-MM-DD window end (default: yesterday)")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        fail(f"DB not found at: {args.db}")
    conn = sqlite3.connect(args.db)
    conn.execute("PRAGMA query_only = ON")   # read-only; verification-first
    preflight(conn)

    end = (dt.date.fromisoformat(args.end_date) if args.end_date
           else dt.date.today() - dt.timedelta(days=1))
    start = end - dt.timedelta(days=LOOKBACK_DAYS - 1)
    print(f"Window: {start} .. {end}  (trailing {LOOKBACK_DAYS} days)")
    print(f"Account: {ACCOUNT_ID} | device rollup: '{DEVICE_ROLLUP_VALUE}'\n")

    p, ki, k = PERF_COLS, KI_COLS, KW_COLS
    params = {"acct": ACCOUNT_ID, "device": DEVICE_ROLLUP_VALUE,
              "start": start.isoformat(), "end": end.isoformat()}

    # ---------------- Diagnostics: fragmentation + orphans ----------------
    frag = conn.execute(f"""
        SELECT
            COUNT(*)                                   AS placements,
            SUM(CASE WHEN n_inst > 1 THEN 1 ELSE 0 END) AS fragmented
        FROM (
            SELECT ki.{ki['placement_id']} AS placement,
                   COUNT(DISTINCT perf.{p['instance_id']}) AS n_inst
            FROM {PERF_TABLE} perf
            JOIN {KI_TABLE} ki ON ki.{ki['id']} = perf.{p['instance_id']}
            WHERE ki.{ki['account_id']} = :acct
              AND perf.{p['device']} = :device
              AND perf.{p['date']} BETWEEN :start AND :end
            GROUP BY ki.{ki['placement_id']}
        )""", params).fetchone()
    if frag and frag[0]:
        pct = 100.0 * (frag[1] or 0) / frag[0]
        print(f"Fragmentation: {frag[1] or 0:,} of {frag[0]:,} active "
              f"placements ({pct:.1f}%) have 12w perf across >1 instance_id "
              f"-- rollup join captures all of it.")

    orphan = conn.execute(f"""
        SELECT COUNT(*), COALESCE(SUM(perf.{p['cost_micros']}),0)/1e6
        FROM {PERF_TABLE} perf
        LEFT JOIN {KI_TABLE} ki ON ki.{ki['id']} = perf.{p['instance_id']}
        WHERE ki.{ki['id']} IS NULL
          AND perf.{p['device']} = :device
          AND perf.{p['date']} BETWEEN :start AND :end
        """, params).fetchone()
    if orphan and orphan[0]:
        print(f"[WARN] Orphan perf rows (no keyword_instances parent): "
              f"{orphan[0]:,} rows, ${orphan[1]:,.2f} cost in window -- "
              f"this spend is EXCLUDED from the analysis; flag to DB "
              f"architect if material.")
    print()

    # ---------------- Main pull: fragmentation-correct placement rollup ----
    # perf.instance_id -> keyword_instances.id -> placement; SUM across ALL
    # of a placement's instance_ids; keyword text/match from keyword_current.
    sql = f"""
        SELECT
            ki.{ki['placement_id']}                       AS placement_id,
            kw.{k['keyword_text']}                        AS keyword_text,
            kw.{k['match_type']}                          AS match_type,
            SUM(perf.{p['cost_micros']}) / 1e6            AS cost,
            SUM(perf.{p['revenue']})                      AS revenue,
            SUM(perf.{p['leads']})                        AS leads
        FROM {PERF_TABLE} perf
        JOIN {KI_TABLE} ki
          ON  ki.{ki['id']} = perf.{p['instance_id']}
        JOIN {KW_VIEW} kw
          ON  kw.{k['placement_id']} = ki.{ki['placement_id']}
          AND kw.{k['account_id']}   = ki.{ki['account_id']}
        WHERE ki.{ki['account_id']} = :acct
          AND perf.{p['device']}    = :device
          AND perf.{p['date']} BETWEEN :start AND :end
        GROUP BY ki.{ki['placement_id']}, kw.{k['keyword_text']},
                 kw.{k['match_type']}
    """
    df = pd.read_sql_query(sql, conn, params=params)
    if df.empty:
        fail("Query returned zero rows. Check date coverage / device "
             "rollup value / account scoping via keyword_instances.")
    print(f"Placements with 12w activity: {len(df):,}")

    # Registry: home-vertical carriers only (canonical carrier definition)
    r = REGISTRY_COLS
    registry = pd.read_sql_query(
        f"""SELECT {r['brand_key']} AS brand_key,
                   {r['brand_category']} AS brand_category,
                   {r['verticals']} AS verticals
            FROM {REGISTRY_TABLE}
            WHERE {r['brand_category']} LIKE 'carrier_%'
              AND {r['verticals']} LIKE '%home%'""", conn)
    if registry.empty:
        fail("Brand registry returned zero home carriers -- check "
             "REGISTRY_TABLE / column names in CONFIG.")
    print(f"Home carriers in registry: {len(registry):,}\n")

    # Resolve placements -> brands
    matcher = load_brand_matcher(args.brand_matcher_dir)
    unique_kw = df["keyword_text"].dropna().unique()
    kw_to_brand: dict[str, str | None] = {}
    if matcher is not None:
        resolver = build_resolver(matcher, args.db)
        print(f"Resolving {len(unique_kw):,} unique keyword texts via "
              f"brand_matcher (canonical)...")
        for kw_text in unique_kw:
            kw_to_brand[kw_text] = resolver(kw_text)
    elif args.allow_registry_fallback:
        print("!" * 78)
        print("! FALLBACK MODE: registry substring matching. DIRECTIONAL")
        print("! ONLY -- do NOT freeze the size axis on this output alone.")
        print("!" * 78)
        for kw_text in unique_kw:
            kw_to_brand[kw_text] = registry_fallback_match(
                kw_text, registry, "brand_key")
    else:
        fail("brand_matcher import failed and fallback not enabled. "
             "Re-run with --brand-matcher-dir pointing at the FOLDER "
             "containing brand_matcher.py, or (directional only) with "
             "--allow-registry-fallback.")

    df["brand_key"] = df["keyword_text"].map(kw_to_brand)
    df = df.merge(registry, on="brand_key", how="left")
    carriers = df[df["brand_category"].notna()].copy()   # home carriers only
    if carriers.empty:
        fail("No placements resolved to home carriers. Likely the value "
             "returned by the matcher doesn't join to the registry's "
             f"'{r['brand_key']}' column -- adjust resolve_brand().")

    carriers["size_group"] = carriers["brand_key"].map(CARRIER_SIZE_MAP)
    unmapped = sorted(carriers.loc[carriers["size_group"].isna(),
                                   "brand_key"].unique())
    if unmapped:
        print(f"[WARN] Active carriers with NO approved size assignment "
              f"(classified 'unmapped_size'): {unmapped}")
        print("[WARN] Add them to CARRIER_SIZE_MAP (and the DB carrier_size "
              "attribute) -- do not leave them unmapped in the waterfall.")
    carriers["size_group"] = carriers["size_group"].fillna("unmapped_size")

    # ---------------- Per-carrier rollup ----------------
    by_carrier = (carriers.groupby(
        ["brand_key", "brand_category", "size_group"], as_index=False)
        .agg(cost=("cost", "sum"), revenue=("revenue", "sum"),
             leads=("leads", "sum"),
             placements=("placement_id", "nunique")))
    by_carrier["gp"] = by_carrier["revenue"] - by_carrier["cost"]
    by_carrier["gp_per_lead"] = by_carrier["gp"] / by_carrier["leads"].replace(0, pd.NA)
    total_gp = by_carrier["gp"].sum()
    by_carrier["gp_share"] = by_carrier["gp"] / total_gp if total_gp else pd.NA
    by_carrier = by_carrier.sort_values("gp", ascending=False)

    # ---------------- Group rollup (approved v1.1 taxonomy) ----------------
    grp = (by_carrier.groupby("size_group", as_index=False)
           .agg(carriers=("brand_key", "nunique"), cost=("cost", "sum"),
                revenue=("revenue", "sum"), leads=("leads", "sum"),
                gp=("gp", "sum")))
    grp["gp_per_lead"] = grp["gp"] / grp["leads"].replace(0, pd.NA)
    grp["gp_share"] = grp["gp"] / total_gp if total_gp else pd.NA
    grp = grp.sort_values("gp", ascending=False)

    pd.set_option("display.float_format", lambda v: f"{v:,.2f}")
    print("\n" + "=" * 78)
    print("GROUP ROLLUP -- approved carrier-size taxonomy (framework v1.1)")
    print("=" * 78)
    print(grp.to_string(index=False))

    print("\n" + "=" * 78)
    print("TOP 25 CARRIERS BY 12W GP")
    print("=" * 78)
    print(by_carrier.head(25).to_string(index=False))

    # ---------------- Flags: bleeders, breakeven treadmills, unmapped ------
    MATERIAL_COST = 500.0            # 12w cost floor for a breakeven flag
    flags = by_carrier[
        (by_carrier["gp"] < 0) |
        ((by_carrier["cost"] >= MATERIAL_COST)
         & (by_carrier["gp_per_lead"].notna())
         & (by_carrier["gp_per_lead"].abs() < 1.0)) |
        (by_carrier["size_group"] == "unmapped_size")
    ].copy()

    def _flag(row):
        if row["size_group"] == "unmapped_size":
            return "no size assignment -- classify"
        if row["gp"] < 0:
            return "bleeding -- reallocation candidate"
        return "breakeven treadmill on material spend"
    flags["flag"] = flags.apply(_flag, axis=1)

    print("\n" + "=" * 78)
    print("FLAGS -- bleeders / breakeven / unmapped")
    print("=" * 78)
    print(flags.to_string(index=False) if len(flags)
          else "None -- all mapped carriers GP-positive.")
    if len(flags):
        bleed = flags[flags["gp"] < 0]
        if len(bleed):
            print(f"\nBleeding tail: {len(bleed)} carriers, "
                  f"${bleed['cost'].sum():,.0f} cost -> "
                  f"${bleed['gp'].sum():,.0f} GP in window.")

    # ---------------- Report footer (axis frozen 2026-07-14) ---------------
    print("\n" + "=" * 78)
    print("CARRIER-SIZE CUT (framework v1.1 -- axis FROZEN 2026-07-14)")
    print("=" * 78)
    for _, row in grp.iterrows():
        gpl = ("n/a" if pd.isna(row["gp_per_lead"])
               else f"{row['gp_per_lead']:,.2f}")
        print(f"  {row['size_group']:<20} {int(row['carriers']):>3} carriers"
              f" | GP {row['gp']:>12,.0f} | GP/lead {gpl:>8}"
              f" | share {row['gp_share']:>6.1%}")
    print("\nThe seed-vs-data validation gate is closed; this script now "
          "reports the approved cut.\nUnmapped or flagged carriers above "
          "are the action items.")

    out_csv = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        f"carrier_size_validation_{end.isoformat()}.csv")
    by_carrier.to_csv(out_csv, index=False)
    print(f"\nPer-carrier detail written to: {out_csv}")


if __name__ == "__main__":
    main()
