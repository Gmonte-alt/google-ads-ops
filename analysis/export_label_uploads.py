"""
export_label_uploads.py [v3 — direct SQLite source, no Excel dependency]
========================================================================
Reads the SQLite database directly to find brand-matched keyword instances
and produces import-ready CSVs per account.

CHANGES FROM v2:
  - No longer reads the kw_target_brand_lookup Excel. The Excel coerces
    integer IDs to floats and corrupts ad_group_id values via scientific
    notation. Direct SQLite access preserves IDs as strings.
  - Brand substring matching, false-positive exclusion, and live-name
    overlay all happen in this script via direct SQL — same logic as
    kw_target_lookup.py but reused here so we don't depend on the
    intermediate Excel artifact.
  - Output CSVs use the live campaigns.campaign_name and
    ad_groups.ad_group_name as the source of truth.
  - Rows whose campaign_id or ad_group_id can't be resolved against
    campaigns/ad_groups are skipped and logged.

OUTPUT
──────
  reports/label_uploads_YYYY-MM-DD/
    google_3863660890_auto.csv          (UTF-16, tab-delimited)
    google_1006729211_auto_quote.csv    (UTF-16, tab-delimited)
    microsoft_F149Y8EN_auto.csv         (UTF-8,  comma-delimited)
    microsoft_F149SN63_home_insurance.csv (UTF-8, comma-delimited)
    skipped_rows.csv

USAGE
─────
    conda activate paid-search
    python analysis/export_label_uploads.py
    python analysis/export_label_uploads.py --tier1-only
    python analysis/export_label_uploads.py --csv path/to/brands.csv
    python analysis/export_label_uploads.py --label "Brand:Confie"
"""

import argparse
import csv
import re
import sqlite3
from datetime import date
from pathlib import Path


# ── Configuration ─────────────────────────────────────────────────────────────

DB_PATH     = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")
OUTPUT_BASE = Path(r"C:\Projects\google-ads-ops\reports")
DEFAULT_CSV = Path(r"C:\Projects\google-ads-ops\analysis\input\confie-list-of-subsidiearies.csv")
DEFAULT_LABEL = "Brand:Confie"

# Accounts to export
EXPORT_ACCOUNTS = [
    ("google",    "3863660890"),  # Auto MCC
    ("google",    "1006729211"),  # Auto Quote
    ("microsoft", "F149Y8EN"),    # Microsoft Auto
    ("microsoft", "F149SN63"),    # Microsoft Home
]

# False-positive exclusions (mirrored from kw_target_lookup.py)
BRAND_FALSE_POSITIVES = {
    "family auto": ["american family"],
}

MATCH_TYPE_MAP = {"EXACT": "Exact", "PHRASE": "Phrase", "BROAD": "Broad"}
STATUS_MAP     = {"ENABLED": "Enabled", "PAUSED": "Paused", "REMOVED": "Removed"}


# ── Helpers ───────────────────────────────────────────────────────────────────

def slugify(s: str) -> str:
    s = (s or "").strip().lower()
    s = re.sub(r"[^a-z0-9]+", "_", s)
    return s.strip("_")


def determine_tier(kw_status, camp_status, ag_status) -> str:
    if (kw_status   or "").upper() == "ENABLED" \
       and (camp_status or "").upper() == "ENABLED" \
       and (ag_status   or "").upper() == "ENABLED":
        return "Tier 1 (actively serving)"
    return "Tier 2 (dormant)"


def load_brand_list(csv_path: Path) -> list[str]:
    if not csv_path.exists():
        raise FileNotFoundError(f"Brand list not found: {csv_path}")
    seen   = set()
    brands = []
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        for row in csv.reader(f):
            if not row:
                continue
            term = row[0].strip().lower()
            if term and term not in seen:
                seen.add(term)
                brands.append(term)
    return brands


# ── Direct SQLite query ───────────────────────────────────────────────────────

def fetch_matched_instances(
    conn: sqlite3.Connection,
    target_accounts: list[tuple[str, str]],
    brands: list[str],
) -> tuple[list[dict], list[dict]]:
    """
    For each (account, brand) pair, finds keyword_instances whose
    normalized text contains the brand term. Joins campaigns and
    ad_groups for live names. Applies false-positive filtering.

    Returns (matched_rows, skipped_rows).
    """
    cur = conn.cursor()

    # Resolve account ids
    placeholders = ",".join(["(?, ?)"] * len(target_accounts))
    flat = [v for pair in target_accounts for v in pair]
    acct_rows = cur.execute(
        f"""
        SELECT id, platform, platform_account_id, account_name
        FROM accounts
        WHERE (platform, platform_account_id) IN ({placeholders})
        """,
        flat,
    ).fetchall()
    acct_map = {
        (r["platform"], str(r["platform_account_id"])): {
            "id": r["id"],
            "name": r["account_name"],
        }
        for r in acct_rows
    }

    print(f"  Searching {len(acct_map)} accounts × {len(brands)} brands "
          f"= {len(acct_map) * len(brands)} sub-queries")

    matched = []
    skipped = []
    seen_instances = set()  # dedupe: instance_id only included once

    for (platform, pid), acct in acct_map.items():
        account_db_id = acct["id"]
        account_name  = acct["name"]

        for brand in brands:
            pattern    = f"%{brand}%"
            exclusions = BRAND_FALSE_POSITIVES.get(brand, [])

            ki_rows = cur.execute("""
                SELECT
                    ki.id              AS instance_id,
                    ki.criterion_id,
                    CAST(ki.campaign_id AS TEXT)  AS campaign_id,
                    CAST(ki.ad_group_id AS TEXT)  AS ad_group_id,
                    ki.keyword_text,
                    ki.match_type,
                    ki.status          AS kw_status,
                    ki_id.normalized_text,
                    c.campaign_name    AS live_campaign_name,
                    c.status           AS campaign_status,
                    ag.ad_group_name   AS live_ad_group_name,
                    ag.status          AS ad_group_status
                FROM keyword_instances ki
                JOIN keyword_identities ki_id ON ki_id.id = ki.identity_id
                LEFT JOIN campaigns c
                       ON c.account_id = ki.account_id
                      AND CAST(c.campaign_id AS TEXT) = CAST(ki.campaign_id AS TEXT)
                LEFT JOIN ad_groups ag
                       ON ag.account_id = ki.account_id
                      AND CAST(ag.ad_group_id AS TEXT) = CAST(ki.ad_group_id AS TEXT)
                WHERE ki.account_id = ?
                  AND LOWER(ki_id.normalized_text) LIKE ?
            """, (account_db_id, pattern)).fetchall()

            for ki in ki_rows:
                ntext = (ki["normalized_text"] or "").lower()
                if exclusions and any(ex in ntext for ex in exclusions):
                    continue  # FP-excluded silently

                instance_id = ki["instance_id"]
                if instance_id in seen_instances:
                    continue
                seen_instances.add(instance_id)

                row_base = {
                    "platform":         platform,
                    "account_id_str":   pid,
                    "account_name":     account_name,
                    "instance_id":      instance_id,
                    "criterion_id":     ki["criterion_id"],
                    "campaign_id":      ki["campaign_id"],
                    "ad_group_id":      ki["ad_group_id"],
                    "keyword_text":     ki["keyword_text"],
                    "match_type":       ki["match_type"],
                    "kw_status":        ki["kw_status"],
                    "campaign_name":    ki["live_campaign_name"],
                    "campaign_status":  ki["campaign_status"],
                    "ad_group_name":    ki["live_ad_group_name"],
                    "ad_group_status":  ki["ad_group_status"],
                }

                # Skip if campaign or ad group can't be resolved live
                if ki["live_campaign_name"] is None:
                    skipped.append({**row_base, "skip_reason":
                        f"campaign_id {ki['campaign_id']} not in campaigns table"})
                    continue
                if ki["live_ad_group_name"] is None:
                    skipped.append({**row_base, "skip_reason":
                        f"ad_group_id {ki['ad_group_id']} not in ad_groups table"})
                    continue

                matched.append(row_base)

    print(f"  Matched : {len(matched):,} instances")
    print(f"  Skipped : {len(skipped):,} instances (no live campaign/ad_group)")
    return matched, skipped


# ── Per-platform writers ──────────────────────────────────────────────────────

def write_google_csv(path: Path, rows: list[dict], label: str) -> int:
    headers = [
        "Campaign", "Ad Group", "Keyword", "Criterion Type", "Labels",
        "Status", "Campaign Status", "Ad Group Status",
        "Campaign ID", "Ad Group ID", "_tier",
    ]
    with open(path, "w", encoding="utf-16", newline="") as f:
        w = csv.writer(f, delimiter="\t")
        w.writerow(headers)
        for r in rows:
            mt = MATCH_TYPE_MAP.get((r.get("match_type") or "").upper(), "")
            kws = STATUS_MAP.get((r.get("kw_status")    or "").upper(), "")
            cs  = STATUS_MAP.get((r.get("campaign_status") or "").upper(), "")
            ags = STATUS_MAP.get((r.get("ad_group_status") or "").upper(), "")
            tier = determine_tier(
                r.get("kw_status"), r.get("campaign_status"), r.get("ad_group_status")
            )
            w.writerow([
                r.get("campaign_name") or "",
                r.get("ad_group_name") or "",
                r.get("keyword_text")  or "",
                mt, label,
                kws, cs, ags,
                str(r.get("campaign_id") or ""),
                str(r.get("ad_group_id") or ""),
                tier,
            ])
    return len(rows)


def write_microsoft_csv(path: Path, rows: list[dict], label: str) -> int:
    headers = [
        "Type", "Campaign", "Ad Group", "Keyword", "Match Type", "Labels",
        "Campaign ID", "Ad Group ID", "_tier",
    ]
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(headers)
        for r in rows:
            mt = MATCH_TYPE_MAP.get((r.get("match_type") or "").upper(), "")
            tier = determine_tier(
                r.get("kw_status"), r.get("campaign_status"), r.get("ad_group_status")
            )
            w.writerow([
                "Keyword",
                r.get("campaign_name") or "",
                r.get("ad_group_name") or "",
                r.get("keyword_text")  or "",
                mt, label,
                str(r.get("campaign_id") or ""),
                str(r.get("ad_group_id") or ""),
                tier,
            ])
    return len(rows)


def write_skipped_log(path: Path, skipped: list[dict]) -> None:
    if not skipped:
        return
    fields = [
        "platform", "account_id_str", "account_name",
        "campaign_id", "campaign_name",
        "ad_group_id", "ad_group_name",
        "keyword_text", "match_type", "kw_status",
        "skip_reason",
    ]
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in skipped:
            w.writerow({k: r.get(k, "") for k in fields})


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--csv",   default=str(DEFAULT_CSV))
    p.add_argument("--label", default=DEFAULT_LABEL)
    p.add_argument("--tier1-only", action="store_true")
    args = p.parse_args()

    print("=" * 70)
    print("export_label_uploads.py  [v3 — direct SQLite, no Excel dep]")
    print(f"  Database  : {DB_PATH}")
    print(f"  Brand list: {Path(args.csv).name}")
    print(f"  Label     : {args.label}")
    print(f"  Filter    : {'Tier 1 only' if args.tier1_only else 'All tiers'}")
    print("=" * 70)

    print("\n[1] Loading brand list...")
    brands = load_brand_list(Path(args.csv))
    print(f"  Loaded {len(brands)} unique brand terms")

    print("\n[2] Querying matched instances directly from DB...")
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    matched, skipped = fetch_matched_instances(conn, EXPORT_ACCOUNTS, brands)
    conn.close()

    if args.tier1_only:
        before = len(matched)
        matched = [
            r for r in matched
            if determine_tier(
                r.get("kw_status"), r.get("campaign_status"),
                r.get("ad_group_status")
            ).startswith("Tier 1")
        ]
        print(f"\n[3] Tier 1 filter: {before:,} → {len(matched):,}")

    # Group by account
    print("\n[4] Grouping by account...")
    groups: dict[tuple[str,str,str], list[dict]] = {}
    for r in matched:
        key = (r["platform"], r["account_id_str"], r["account_name"])
        groups.setdefault(key, []).append(r)

    out_dir = OUTPUT_BASE / f"label_uploads_{date.today().isoformat()}"
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"\n[5] Writing per-account files to:")
    print(f"      {out_dir}")

    total_t1 = total_t2 = 0
    for (platform, pid, name), rows in sorted(groups.items()):
        out_path = out_dir / f"{platform}_{pid}_{slugify(name)}.csv"
        if platform == "google":
            n = write_google_csv(out_path, rows, args.label)
            fmt = "UTF-16 / tab"
        elif platform == "microsoft":
            n = write_microsoft_csv(out_path, rows, args.label)
            fmt = "UTF-8 / comma"
        else:
            continue

        t1 = sum(1 for r in rows if determine_tier(
            r.get("kw_status"), r.get("campaign_status"),
            r.get("ad_group_status")
        ).startswith("Tier 1"))
        t2 = n - t1
        total_t1 += t1
        total_t2 += t2
        print(f"  {platform:<10} {pid:<12} {name:<25} "
              f"{n:>5,} rows  (T1={t1:,}  T2={t2:,})  [{fmt}]")

    if skipped:
        skip_path = out_dir / "skipped_rows.csv"
        write_skipped_log(skip_path, skipped)
        print(f"\n[6] Skipped audit log: {skip_path}")
        print(f"    {len(skipped):,} rows excluded (no live campaign/ad_group)")

    print(f"\n[7] Summary:")
    print(f"  Tier 1 (actively serving): {total_t1:,}")
    print(f"  Tier 2 (dormant)         : {total_t2:,}")
    print(f"  Total exported           : {total_t1 + total_t2:,}")
    print(f"  Skipped                  : {len(skipped):,}")
    print("\nDone.")


if __name__ == "__main__":
    main()