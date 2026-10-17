"""
termlife_zip_probe.py  [Phase 1 — READ-ONLY probe, no DDL, no writes]
=====================
Term Life Zip-Targeting Subsystem — Quote.com Google Ads
Folder: C:\\Projects\\google-ads-ops\\campaign_db_scripts\\

Charter: TERM LIFE ZIP-TARGETING SUBSYSTEM (acct 29 / customer 1671155176).
Cardinal rule: verify, then build — this probe establishes what Google
actually exposes at zip grain BEFORE any schema is proposed at the gate.

WHAT IT DOES (all read-only):
  A. TARGETING SIDE
     - Pulls campaign_criterion type=LOCATION for acct 29 (negative flag,
       bid_modifier, status), resolves every criterion to geo_target_constant
       details via the API (NOT the local geo_target_constants table, which
       holds almost no postal constants — the Life geo-incident lesson).
     - Counts postal-code vs state vs other criteria per campaign; validates
       postal names as 5-digit zips (leading-zero check); counts non-US;
       checks zip overlap across ENABLED campaigns (income-segmentation
       disjointness); reports local geo_target_constants coverage
       (informational — supports the no-FK ruling).
     - Writes the full targeting map preview to
       output\\zip_probe_targeting_map.csv (gate exhibit; becomes the
       02a_sync_zip_targeting.py acceptance reference).

  B. PERFORMANCE SIDE (one recent complete week, default Sun→Sat)
     - geographic_view + segments.geo_target_postal_code, base grain
       (campaign x date x location_type x zip): row counts, distinct zips.
     - Same query + segments.device: measures the device multiplication
       factor directly.
     - user_location_view + postal segment (physical-location variant):
       row counts, or a loud SKIP if the API rejects the combination.
     - Spend coverage: zip-grain cost vs campaign_performance_daily
       (device='ALL') for the same window — how much spend has NO zip
       association (geographic_view omits unmatched rows, like the state
       table's "Matched Locations" behavior).
     - Volume projection: rows per active day and projected rows/year at
       the account's 6-day week (no-Sunday schedule). This number decides
       Phase 2's fate.

  DB access is READ-ONLY (URI mode=ro). Nothing is created or written to
  paid_search.db. Each section runs in its own try/except — one API error
  does not kill the probe; failures print loudly and are listed at the end.

RUN:
  conda activate paid-search
  python campaign_db_scripts\\termlife_zip_probe.py > probe_zip_targeting.txt 2>&1

  # explicit window:
  python campaign_db_scripts\\termlife_zip_probe.py --start 2026-09-27 --end 2026-10-03

OUTPUT GOES TO THE ARCHITECT GATE with a proposed schema before any DDL.
"""

import csv
import os
import re
import sqlite3
import argparse
import logging
from pathlib import Path
from collections import defaultdict
from datetime import date, timedelta, datetime, timezone
from dotenv import load_dotenv

# ── Logging — attach handler before Google Ads client import ──────────────────
_handler = logging.StreamHandler()
_handler.setFormatter(logging.Formatter(
    "%(asctime)s  %(levelname)-8s  %(message)s", datefmt="%H:%M:%S"
))
logging.root.setLevel(logging.INFO)
logging.root.addHandler(_handler)
log = logging.getLogger(__name__)

from google.ads.googleads.client import GoogleAdsClient  # noqa: E402 — after logging setup

# ── Configuration ─────────────────────────────────────────────────────────────
load_dotenv()

DB_PATH    = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")
OUTPUT_DIR = Path(r"C:\Projects\google-ads-ops\campaign_db_scripts\output")
MCC_ID     = os.getenv("GOOGLE_ADS_MCC_ID")

TARGET_VERTICAL = "term_life"      # probe is acct-29-only by charter
ZIP_RE          = re.compile(r"^\d{5}$")
GEO_CHUNK       = 1000             # criterion ids per geo_target_constant query
ACTIVE_DAYS_PER_YEAR = 312         # 6-day weeks — acct-29 no-Sunday schedule


# ── Database (READ-ONLY) ──────────────────────────────────────────────────────

def get_db_connection_readonly():
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def get_account_internal_id(conn, platform_account_id: str) -> int | None:
    row = conn.execute(
        "SELECT id FROM accounts WHERE platform_account_id = ? AND platform = 'google'",
        (platform_account_id,),
    ).fetchone()
    return row["id"] if row else None


# ── Account discovery (same pattern as 02/03) ─────────────────────────────────

def discover_accounts() -> list[dict]:
    accounts = []
    prefix   = "GOOGLE_ADS_CUSTOMER_ID_"
    for key, value in os.environ.items():
        if not key.startswith(prefix):
            continue
        suffix    = key[len(prefix):]
        cid       = value.strip()
        login_key = f"GOOGLE_ADS_LOGIN_CUSTOMER_ID_{suffix}"
        login_id  = os.getenv(login_key, "").strip()
        accounts.append({
            "customer_id":       cid,
            "vertical":          suffix.lower(),
            "login_customer_id": login_id if login_id else MCC_ID,
            "is_standalone":     bool(login_id),
        })
    return accounts


def get_google_ads_client(login_customer_id: str) -> GoogleAdsClient:
    client = GoogleAdsClient.load_from_storage()
    client.login_customer_id = login_customer_id
    return client


# ── Helpers ───────────────────────────────────────────────────────────────────

def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def last_complete_week() -> tuple[str, str]:
    """Most recent complete Sun→Sat window (a Life week ends Saturday)."""
    today = date.today()
    # most recent Saturday strictly before today
    offset = (today.weekday() - 5) % 7
    offset = 7 if offset == 0 else offset
    sat = today - timedelta(days=offset)
    sun = sat - timedelta(days=6)
    return sun.isoformat(), sat.isoformat()


def resource_tail(resource_name: str) -> str | None:
    if not resource_name:
        return None
    parts = str(resource_name).split("/")
    return parts[-1] if parts else None


def micros_to_dollars(m) -> float:
    return (m or 0) / 1e6


# ── A. Targeting-side probe ───────────────────────────────────────────────────

CRITERION_QUERY = """
    SELECT
        campaign.id,
        campaign.name,
        campaign.status,
        campaign_criterion.criterion_id,
        campaign_criterion.negative,
        campaign_criterion.bid_modifier,
        campaign_criterion.status,
        campaign_criterion.location.geo_target_constant
    FROM campaign_criterion
    WHERE campaign_criterion.type = 'LOCATION'
      AND campaign_criterion.status != 'REMOVED'
      AND campaign.status != 'REMOVED'
    ORDER BY campaign.id
"""

GEO_CONSTANT_QUERY_TEMPLATE = """
    SELECT
        geo_target_constant.id,
        geo_target_constant.name,
        geo_target_constant.country_code,
        geo_target_constant.target_type,
        geo_target_constant.canonical_name,
        geo_target_constant.status
    FROM geo_target_constant
    WHERE geo_target_constant.id IN ({ids})
"""


def pull_location_criteria(client, ga_service, customer_id: str) -> list[dict]:
    request = client.get_type("SearchGoogleAdsRequest")
    request.customer_id = customer_id
    request.query = CRITERION_QUERY

    criteria = []
    for row in ga_service.search(request=request):
        c  = row.campaign
        cc = row.campaign_criterion
        geo_id = resource_tail(cc.location.geo_target_constant)
        criteria.append({
            "campaign_id":     str(c.id),
            "campaign_name":   c.name,
            "campaign_status": c.status.name,
            "criterion_id":    str(cc.criterion_id),
            "is_negative":     1 if cc.negative else 0,
            "bid_modifier":    float(cc.bid_modifier) if cc.bid_modifier else None,
            "criterion_status": cc.status.name if cc.status else None,
            "geo_id":          int(geo_id) if geo_id and geo_id.isdigit() else None,
        })
    return criteria


def resolve_geo_constants(client, ga_service, customer_id: str,
                          geo_ids: set[int]) -> dict[int, dict]:
    """Resolve criterion ids to geo constant details via the API, chunked."""
    details: dict[int, dict] = {}
    ids = sorted(geo_ids)
    for i in range(0, len(ids), GEO_CHUNK):
        batch = ids[i:i + GEO_CHUNK]
        request = client.get_type("SearchGoogleAdsRequest")
        request.customer_id = customer_id
        request.query = GEO_CONSTANT_QUERY_TEMPLATE.format(
            ids=", ".join(str(x) for x in batch)
        )
        for row in ga_service.search(request=request):
            g = row.geo_target_constant
            details[int(g.id)] = {
                "name":           g.name,
                "country_code":   g.country_code,
                "target_type":    g.target_type,
                "canonical_name": g.canonical_name,
                "status":         g.status.name if g.status else None,
            }
        print(f"    geo constants resolved: {len(details):,} / {len(ids):,}",
              flush=True)
    return details


def targeting_summary(criteria: list[dict], geo: dict[int, dict],
                      local_geo_ids: set[int]) -> dict:
    """Builds + prints the per-campaign targeting breakdown. Returns totals."""
    per_campaign: dict[str, dict] = {}
    campaign_meta: dict[str, dict] = {}
    zip_to_campaigns_pos: dict[str, set] = defaultdict(set)   # positive only
    invalid_zip_names: list[tuple] = []
    non_us: list[tuple] = []
    unresolved = 0

    for cr in criteria:
        cid  = cr["campaign_id"]
        campaign_meta[cid] = {"name": cr["campaign_name"],
                              "status": cr["campaign_status"]}
        agg = per_campaign.setdefault(cid, {
            "postal_pos": 0, "postal_neg": 0, "state": 0,
            "country": 0, "other": 0, "unresolved": 0,
            "bid_mod_set": 0,
        })
        if cr["bid_modifier"] is not None and cr["bid_modifier"] != 0.0:
            agg["bid_mod_set"] += 1

        g = geo.get(cr["geo_id"]) if cr["geo_id"] is not None else None
        if g is None:
            agg["unresolved"] += 1
            unresolved += 1
            continue

        ttype = (g["target_type"] or "").lower()
        if "postal" in ttype:
            zip_name = g["name"]
            if not ZIP_RE.match(zip_name or ""):
                invalid_zip_names.append((cid, cr["criterion_id"], zip_name,
                                          g["canonical_name"]))
            if g["country_code"] != "US":
                non_us.append((cid, zip_name, g["country_code"]))
            if cr["is_negative"]:
                agg["postal_neg"] += 1
            else:
                agg["postal_pos"] += 1
                zip_to_campaigns_pos[zip_name].add(cid)
        elif ttype == "state":
            agg["state"] += 1
        elif ttype == "country":
            agg["country"] += 1
        else:
            agg["other"] += 1

    # ── print per-campaign table ──────────────────────────────────────────
    print(f"\n  Per-campaign LOCATION criteria "
          f"(postal+ / postal- / state / country / other / unresolved):")
    print(f"  {'Campaign':<52} {'St':<8} {'P+':>7} {'P-':>6} "
          f"{'State':>6} {'Ctry':>5} {'Oth':>5} {'?':>4}")
    print(f"  {'─'*52} {'─'*8} {'─'*7} {'─'*6} {'─'*6} {'─'*5} {'─'*5} {'─'*4}")

    def sort_key(item):
        cid, agg = item
        status = campaign_meta[cid]["status"]
        return (0 if status == "ENABLED" else 1, -agg["postal_pos"])

    for cid, agg in sorted(per_campaign.items(), key=sort_key):
        meta = campaign_meta[cid]
        print(f"  {meta['name'][:51]:<52} {meta['status'][:7]:<8} "
              f"{agg['postal_pos']:>7,} {agg['postal_neg']:>6,} "
              f"{agg['state']:>6,} {agg['country']:>5,} "
              f"{agg['other']:>5,} {agg['unresolved']:>4,}")

    # ── totals ────────────────────────────────────────────────────────────
    tot = {k: sum(a[k] for a in per_campaign.values())
           for k in ("postal_pos", "postal_neg", "state", "country",
                     "other", "unresolved", "bid_mod_set")}
    all_pos_zips = set(zip_to_campaigns_pos.keys())

    enabled_ids = {cid for cid, m in campaign_meta.items()
                   if m["status"] == "ENABLED"}
    overlap_enabled = {z: cids & enabled_ids
                       for z, cids in zip_to_campaigns_pos.items()
                       if len(cids & enabled_ids) > 1}

    print(f"\n  TOTALS")
    print(f"    LOCATION criteria pulled        : {len(criteria):,}")
    print(f"    Positive postal (zip) criteria  : {tot['postal_pos']:,}")
    print(f"    Negative postal criteria        : {tot['postal_neg']:,}")
    print(f"    State criteria                  : {tot['state']:,}")
    print(f"    Country criteria                : {tot['country']:,}")
    print(f"    Other geo types                 : {tot['other']:,}")
    print(f"    Unresolved geo ids              : {tot['unresolved']:,}")
    print(f"    Criteria with bid_modifier set  : {tot['bid_mod_set']:,}")
    print(f"    DISTINCT positive zips (all)    : {len(all_pos_zips):,}")
    print(f"    Zip names failing ^\\d{{5}}$ check : {len(invalid_zip_names):,}")
    for row in invalid_zip_names[:10]:
        print(f"      INVALID: campaign {row[0]} criterion {row[1]} "
              f"name={row[2]!r} ({row[3]})")
    print(f"    Non-US postal constants         : {len(non_us):,}")
    for row in non_us[:10]:
        print(f"      NON-US: campaign {row[0]} zip={row[1]} country={row[2]}")

    print(f"\n  Segmentation disjointness (ENABLED campaigns only):")
    print(f"    Zips targeted by >1 enabled campaign : {len(overlap_enabled):,}")
    if overlap_enabled:
        sample = list(overlap_enabled.items())[:10]
        for z, cids in sample:
            names = ", ".join(campaign_meta[c]["name"][:30] for c in sorted(cids))
            print(f"      {z}: {names}")

    local_hits = len({cr['geo_id'] for cr in criteria
                      if cr['geo_id'] in local_geo_ids})
    distinct_geo = len({cr['geo_id'] for cr in criteria
                        if cr['geo_id'] is not None})
    print(f"\n  Local geo_target_constants coverage (informational — FK ruling):")
    print(f"    Distinct geo ids in criteria    : {distinct_geo:,}")
    print(f"    Present in local table          : {local_hits:,} "
          f"({(local_hits / distinct_geo * 100) if distinct_geo else 0:.1f}%)")

    return {"criteria": len(criteria), "postal_pos": tot["postal_pos"],
            "distinct_zips": len(all_pos_zips)}


def write_targeting_csv(criteria: list[dict], geo: dict[int, dict], path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["campaign_id", "campaign_name", "campaign_status",
                    "criterion_id", "geo_target_constant_id", "target_type",
                    "zip_or_name", "country_code", "canonical_name",
                    "is_negative", "bid_modifier", "criterion_status"])
        for cr in criteria:
            g = geo.get(cr["geo_id"], {})
            w.writerow([
                cr["campaign_id"], cr["campaign_name"], cr["campaign_status"],
                cr["criterion_id"], cr["geo_id"],
                g.get("target_type"), g.get("name"), g.get("country_code"),
                g.get("canonical_name"),
                cr["is_negative"], cr["bid_modifier"], cr["criterion_status"],
            ])
    print(f"\n  Targeting map written: {path}")


# ── B. Performance-side probe ─────────────────────────────────────────────────

GEOVIEW_ZIP_BASE_QUERY = """
    SELECT
        campaign.id,
        campaign.status,
        segments.date,
        geographic_view.location_type,
        segments.geo_target_postal_code,
        metrics.impressions,
        metrics.clicks,
        metrics.cost_micros,
        metrics.conversions,
        metrics.conversions_value,
        metrics.all_conversions,
        metrics.all_conversions_value
    FROM geographic_view
    WHERE segments.date BETWEEN '{start}' AND '{end}'
      AND campaign.status != 'REMOVED'
"""

GEOVIEW_ZIP_DEVICE_QUERY = """
    SELECT
        campaign.id,
        campaign.status,
        segments.date,
        segments.device,
        geographic_view.location_type,
        segments.geo_target_postal_code,
        metrics.impressions,
        metrics.cost_micros
    FROM geographic_view
    WHERE segments.date BETWEEN '{start}' AND '{end}'
      AND campaign.status != 'REMOVED'
"""

USERLOC_ZIP_QUERY = """
    SELECT
        campaign.id,
        segments.date,
        user_location_view.targeting_location,
        segments.geo_target_postal_code,
        metrics.impressions,
        metrics.cost_micros
    FROM user_location_view
    WHERE segments.date BETWEEN '{start}' AND '{end}'
"""


def run_perf_probe(client, ga_service, customer_id: str,
                   start: str, end: str) -> dict:
    out = {"base_rows": None, "device_rows": None, "userloc_rows": None,
           "base_cost": 0.0, "distinct_zips": 0, "active_days": 0,
           "loc_type_counts": {}, "no_zip_rows": 0}

    # B1 — base grain: campaign x date x location_type x zip
    print(f"\n  B1. geographic_view + postal segment (base grain) "
          f"{start} → {end} ...", flush=True)
    request = client.get_type("SearchGoogleAdsRequest")
    request.customer_id = customer_id
    request.query = GEOVIEW_ZIP_BASE_QUERY.format(start=start, end=end)

    zips, days = set(), set()
    loc_counts: dict[str, int] = defaultdict(int)
    n, cost, no_zip = 0, 0, 0
    for row in ga_service.search(request=request):
        n += 1
        zp = resource_tail(row.segments.geo_target_postal_code)
        if zp is None:
            no_zip += 1
        else:
            zips.add(zp)
        days.add(row.segments.date)
        loc_counts[row.geographic_view.location_type.name] += 1
        cost += row.metrics.cost_micros
    out.update(base_rows=n, base_cost=micros_to_dollars(cost),
               distinct_zips=len(zips), active_days=len(days),
               loc_type_counts=dict(loc_counts), no_zip_rows=no_zip)
    print(f"      rows={n:,}  distinct postal ids={len(zips):,}  "
          f"active days={len(days)}  cost=${out['base_cost']:,.2f}")
    print(f"      location_type split: {dict(loc_counts)}")
    print(f"      rows with NO postal segment value: {no_zip:,}")
    print(f"      NOTE: postal segment returns geoTargetConstants/<id> — "
          f"phase 3 must resolve id→zip (same lookup as targeting side).")

    # B2 — device multiplication
    print(f"\n  B2. same + segments.device (multiplication test) ...", flush=True)
    try:
        request = client.get_type("SearchGoogleAdsRequest")
        request.customer_id = customer_id
        request.query = GEOVIEW_ZIP_DEVICE_QUERY.format(start=start, end=end)
        n_dev = sum(1 for _ in ga_service.search(request=request))
        out["device_rows"] = n_dev
        factor = (n_dev / n) if n else 0
        print(f"      rows={n_dev:,}  (x{factor:.2f} vs base grain)")
    except Exception as e:
        print(f"      ERROR (device variant): {e}")

    # B3 — user_location_view (physical location) variant
    print(f"\n  B3. user_location_view + postal segment (physical) ...",
          flush=True)
    try:
        request = client.get_type("SearchGoogleAdsRequest")
        request.customer_id = customer_id
        request.query = USERLOC_ZIP_QUERY.format(start=start, end=end)
        n_ul = sum(1 for _ in ga_service.search(request=request))
        out["userloc_rows"] = n_ul
        print(f"      rows={n_ul:,}")
    except Exception as e:
        print(f"      SKIP — API rejected the combination: {e}")

    return out


def spend_coverage_check(conn, account_internal_id: int,
                         start: str, end: str, zip_cost: float):
    """Zip-grain cost vs campaign_performance_daily truth (device='ALL')."""
    try:
        row = conn.execute("""
            SELECT SUM(cost_micros) AS cost
            FROM campaign_performance_daily
            WHERE account_id = ? AND device = 'ALL'
              AND date BETWEEN ? AND ?
        """, (account_internal_id, start, end)).fetchone()
        cpd_cost = micros_to_dollars(row["cost"])
        pct = (zip_cost / cpd_cost * 100) if cpd_cost else 0.0
        print(f"\n  Spend coverage vs campaign_performance_daily (truth):")
        print(f"    cpd cost ({start} → {end})     : ${cpd_cost:,.2f}")
        print(f"    zip-grain cost (geographic_view): ${zip_cost:,.2f}  "
              f"({pct:.1f}% coverage)")
        if cpd_cost and pct < 95:
            print(f"    NOTE: {100 - pct:.1f}% of spend has no zip association "
                  f"— expected (matched-locations behavior); phase 2 table "
                  f"doc line must state zip-grain is not a spend-complete view.")
    except Exception as e:
        print(f"\n  ERROR (spend coverage check — read-only DB): {e}")


def volume_projection(perf: dict):
    rows = perf.get("base_rows") or 0
    days = perf.get("active_days") or 0
    if not rows or not days:
        print(f"\n  Volume projection: no zip-grain rows returned — "
              f"phase 2 moot unless the window was anomalous.")
        return
    per_day_base = rows / days
    dev_rows     = perf.get("device_rows")
    per_day_dev  = (dev_rows / days) if dev_rows else None

    print(f"\n  VOLUME PROJECTION (the number that decides phase 2):")
    print(f"    Base grain  : {per_day_base:,.0f} rows/active-day  →  "
          f"~{per_day_base * ACTIVE_DAYS_PER_YEAR:,.0f} rows/yr "
          f"(6-day weeks, no-Sunday schedule)")
    if per_day_dev:
        print(f"    Device grain: {per_day_dev:,.0f} rows/active-day  →  "
              f"~{per_day_dev * ACTIVE_DAYS_PER_YEAR:,.0f} rows/yr")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="READ-ONLY zip-targeting probe for Term Life (acct 29). "
                    "No DDL, no DB writes. Output feeds the architect gate.")
    default_start, default_end = last_complete_week()
    parser.add_argument("--start", metavar="YYYY-MM-DD", default=default_start,
                        help=f"Perf-probe window start (default {default_start} "
                             f"— last complete Sun→Sat week).")
    parser.add_argument("--end", metavar="YYYY-MM-DD", default=default_end,
                        help=f"Perf-probe window end (default {default_end}).")
    parser.add_argument("--skip-perf", action="store_true",
                        help="Targeting side only (skip section B).")
    parser.add_argument("--perf-only", action="store_true",
                        help="Performance side only (skip section A — use "
                             "when the targeting pull already succeeded).")
    args = parser.parse_args()

    print("=" * 65)
    print("termlife_zip_probe.py  [Phase 1 — READ-ONLY]")
    print(f"Database : {DB_PATH}  (mode=ro)")
    print(f"Started  : {now_utc()}")
    print(f"Window   : {args.start} → {args.end}  (perf probe)")
    print("=" * 65)

    accounts = [a for a in discover_accounts()
                if a["vertical"] == TARGET_VERTICAL]
    if not accounts:
        raise SystemExit(f"[ABORT] no GOOGLE_ADS_CUSTOMER_ID_"
                         f"{TARGET_VERTICAL.upper()} in .env")
    account = accounts[0]
    cid = account["customer_id"]
    print(f"\nAccount  : {TARGET_VERTICAL} ({cid})  "
          f"login={account['login_customer_id']}")

    conn = get_db_connection_readonly()
    account_internal_id = get_account_internal_id(conn, cid)
    print(f"accounts.id = {account_internal_id}")
    if account_internal_id is None:
        print("  WARNING: accounts.id not found — spend coverage check will skip.")

    try:
        local_geo_ids = {r["criterion_id"] for r in conn.execute(
            "SELECT criterion_id FROM geo_target_constants").fetchall()}
        print(f"Local geo_target_constants rows: {len(local_geo_ids):,}")
    except Exception as e:
        local_geo_ids = set()
        print(f"  WARNING: could not read geo_target_constants: {e}")

    client     = get_google_ads_client(account["login_customer_id"])
    ga_service = client.get_service("GoogleAdsService")

    failures = []

    # ── A. Targeting side ─────────────────────────────────────────────────
    criteria, geo = [], {}
    if args.perf_only:
        print(f"\n[--perf-only] Section A skipped.")
    else:
      print(f"\n{'─'*65}\nA. TARGETING SIDE — campaign_criterion LOCATION pull"
            f"\n{'─'*65}")
      try:
        criteria = pull_location_criteria(client, ga_service, cid)
        print(f"  LOCATION criteria pulled: {len(criteria):,}")
        geo_ids = {c["geo_id"] for c in criteria if c["geo_id"] is not None}
        print(f"  Distinct geo ids to resolve: {len(geo_ids):,}")
        geo = resolve_geo_constants(client, ga_service, cid, geo_ids)
        targeting_summary(criteria, geo, local_geo_ids)
        write_targeting_csv(criteria, geo,
                            OUTPUT_DIR / "zip_probe_targeting_map.csv")
      except Exception as e:
        print(f"  ERROR (targeting side): {e}")
        failures.append("A:targeting")

    # ── B. Performance side ───────────────────────────────────────────────
    perf = {}
    if args.skip_perf:
        print(f"\n[--skip-perf] Section B skipped.")
    else:
        print(f"\n{'─'*65}\nB. PERFORMANCE SIDE — zip-grain reporting test"
              f"\n{'─'*65}")
        try:
            perf = run_perf_probe(client, ga_service, cid, args.start, args.end)
            if account_internal_id is not None:
                spend_coverage_check(conn, account_internal_id,
                                     args.start, args.end, perf["base_cost"])
            volume_projection(perf)
        except Exception as e:
            print(f"  ERROR (performance side): {e}")
            failures.append("B:performance")

    conn.close()

    print(f"\n{'='*65}\nPROBE COMPLETE\n{'='*65}")
    if failures:
        print(f"  FAILURES: {failures}")
    print(f"  Next: probe output + proposed schema → DB architect gate.")
    print(f"  No DDL executes before ratification.")
    print(f"\nDone. {now_utc()}")


if __name__ == "__main__":
    main()
