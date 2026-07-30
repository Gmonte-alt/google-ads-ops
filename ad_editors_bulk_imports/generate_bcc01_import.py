"""
generate_bcc01_import.py — BCC-01 Rev-2 Build (single wave, DKI-free keeper rule)

Inputs (all from ad_assets export pipeline, 2026-07-24):
  --scope-csv        bcc01_scope_gaps          (86 target ad groups, census-based)
  --ranking-csv      bcc01_pause_ranking       (166 ads with YTD performance + DKI flag)
  --content-csv      bcc01_ordered_content     (slot-ordered headlines/descriptions per ad)
  --urls-csv         bcc01_incumbent_urls      (final URL + paths per ad)
  --campaigns-csv    bcc01_campaign_names      (campaign_id → current name)
  --copy-csv         bcc01_copy                (BCC-01 15 headlines + 4 descriptions)

Outputs:
  bcc01_combined_{date}.csv  — creates + pauses in one file (single wave)

Pause rule (decision record rev 3, §6):
  Per targeted ad group, rank enabled RSAs by YTD clicks AMONG ads whose live
  copy contains no {KeyWord:} insertion — keep that #1, pause every other enabled
  RSA. If no DKI-free RSA exists, BCC-01 runs solo.

Content source for pause rows: bcc01_ordered_content (slot-ordered from live API pull).
"""

import csv
import argparse
import os
import sys
from datetime import datetime
from typing import Dict, List, Tuple
from collections import defaultdict

OUTPUT_DIR = r"C:\Projects\google-ads-ops\ad_editors_bulk_imports\output"
BCC01_LABEL = "BCC-01"


# ============================================================
# LOADERS
# ============================================================

def load_copy(path):
    headlines, descriptions = [], []
    with open(path, "r") as f:
        for row in csv.DictReader(f):
            t = row["type"].strip().upper()
            text = row["text"].strip()
            if t == "HEADLINE":
                headlines.append(text)
            elif t == "DESCRIPTION":
                descriptions.append(text)
    assert len(headlines) == 15, f"Expected 15 headlines, got {len(headlines)}"
    assert len(descriptions) == 4, f"Expected 4 descriptions, got {len(descriptions)}"
    return headlines, descriptions


def load_scope(path):
    with open(path, "r") as f:
        return list(csv.DictReader(f))


def load_ranking(path):
    by_ag = defaultdict(list)
    with open(path, "r") as f:
        for row in csv.DictReader(f):
            by_ag[row["ad_group_id"]].append(row)
    return dict(by_ag)


def load_ordered_content(path):
    """Build {ad_id: {headlines: [(text, pin), ...], descriptions: [...], has_dki: bool}}"""
    raw = defaultdict(lambda: {"hl": {}, "desc": {}, "has_dki": False})
    with open(path, "r") as f:
        for row in csv.DictReader(f):
            ad_id = row["ad_id"]
            field = row["field_type"].strip().upper()
            slot = int(row["slot"])
            text = row["text"].strip()
            pin = row.get("pinned_field", "").strip()

            if field == "HEADLINE":
                raw[ad_id]["hl"][slot] = (text, pin)
            elif field == "DESCRIPTION":
                raw[ad_id]["desc"][slot] = (text, pin)

            if "{keyword" in text.lower():
                raw[ad_id]["has_dki"] = True

    result = {}
    for ad_id, d in raw.items():
        hl_ordered = [d["hl"][s] for s in sorted(d["hl"])]
        desc_ordered = [d["desc"][s] for s in sorted(d["desc"])]
        result[ad_id] = {"headlines": hl_ordered, "descriptions": desc_ordered, "has_dki": d["has_dki"]}
    return result


def load_urls(path):
    lookup = {}
    with open(path, "r") as f:
        for row in csv.DictReader(f):
            lookup[row["ad_id"].strip()] = {
                "final_url": row.get("final_url", "").strip(),
                "path1": row.get("path1", "").strip(),
                "path2": row.get("path2", "").strip(),
                "tracking": row.get("tracking_url_template", "").strip(),
            }
    return lookup


def load_campaigns(path):
    lookup = {}
    with open(path, "r") as f:
        for row in csv.DictReader(f):
            lookup[row["campaign_id"].strip()] = row["campaign_name_current"].strip()
    return lookup


# ============================================================
# PAUSE RULE (rev 3 §6)
# ============================================================

def apply_pause_rule(ads, content):
    """Returns (keeper_ad_id or None, [pause_ad_ids], is_solo)."""
    dki_free = [a for a in ads if not content.get(a["ad_id"], {}).get("has_dki", False)]

    if not dki_free:
        return None, [a["ad_id"] for a in ads], True

    dki_free.sort(key=lambda x: int(x.get("clicks", 0)), reverse=True)
    keeper = dki_free[0]["ad_id"]
    pause_ids = [a["ad_id"] for a in ads if a["ad_id"] != keeper]
    return keeper, pause_ids, False


# ============================================================
# ROW BUILDERS
# ============================================================

def get_headers():
    h = ["Ad status", "Campaign", "Ad group", "Ad type", "Ad ID",
         "Final URL", "Tracking template", "Path 1", "Path 2", "Labels"]
    for i in range(1, 16):
        h += [f"Headline {i}", f"Headline {i} position"]
    for i in range(1, 5):
        h += [f"Description {i}", f"Description {i} position"]
    return h


def make_create(campaign, ad_group, headlines, descriptions, url_entry):
    row = {"Ad status": "Enabled", "Campaign": campaign, "Ad group": ad_group,
           "Ad type": "Responsive search ad", "Ad ID": "",
           "Final URL": url_entry.get("final_url", ""),
           "Path 1": url_entry.get("path1", ""), "Path 2": url_entry.get("path2", ""),
           "Tracking template": url_entry.get("tracking", ""), "Labels": BCC01_LABEL}
    for i, hl in enumerate(headlines, 1):
        row[f"Headline {i}"] = hl
        row[f"Headline {i} position"] = ""
    for i, d in enumerate(descriptions, 1):
        row[f"Description {i}"] = d
        row[f"Description {i} position"] = ""
    return row


def make_pause(campaign, ad_group, ad_id, content_entry, url_entry):
    row = {"Ad status": "Paused", "Campaign": campaign, "Ad group": ad_group,
           "Ad type": "Responsive search ad", "Ad ID": ad_id,
           "Final URL": url_entry.get("final_url", ""),
           "Path 1": url_entry.get("path1", ""), "Path 2": url_entry.get("path2", ""),
           "Tracking template": url_entry.get("tracking", ""), "Labels": ""}
    hls = content_entry.get("headlines", [])
    for i in range(1, 16):
        if i <= len(hls):
            row[f"Headline {i}"] = hls[i - 1][0]
            row[f"Headline {i} position"] = hls[i - 1][1] if hls[i - 1][1] else ""
        else:
            row[f"Headline {i}"] = ""
            row[f"Headline {i} position"] = ""
    descs = content_entry.get("descriptions", [])
    for i in range(1, 5):
        if i <= len(descs):
            row[f"Description {i}"] = descs[i - 1][0]
            row[f"Description {i} position"] = descs[i - 1][1] if descs[i - 1][1] else ""
        else:
            row[f"Description {i}"] = ""
            row[f"Description {i} position"] = ""
    return row


# ============================================================
# MAIN
# ============================================================

def generate(copy_csv, scope_csv, ranking_csv, content_csv, urls_csv, campaigns_csv,
             output_dir, dry_run=False):
    os.makedirs(output_dir, exist_ok=True)
    date_stamp = datetime.now().strftime("%Y-%m-%d")

    print("Loading inputs...")
    headlines, descriptions = load_copy(copy_csv)
    print(f"  Copy: {len(headlines)} HL + {len(descriptions)} DESC")

    scope = load_scope(scope_csv)
    print(f"  Scope: {len(scope)} target ad groups")

    ranking = load_ranking(ranking_csv)
    print(f"  Ranking: {sum(len(v) for v in ranking.values())} ads / {len(ranking)} groups")

    content = load_ordered_content(content_csv)
    print(f"  Ordered content: {len(content)} ads")

    urls = load_urls(urls_csv)
    print(f"  URLs: {len(urls)} entries")

    campaigns = load_campaigns(campaigns_csv)
    print(f"  Campaigns: {len(campaigns)}")
    for cid, name in campaigns.items():
        print(f"    {cid}: {name}")

    # Process each target ad group
    creates, pauses = [], []
    solo_groups, keeper_groups = [], []
    errors = []

    for t in scope:
        ag_id, ag_name, camp_id = t["ad_group_id"], t["ad_group_name"], t["campaign_id"]
        camp_name = campaigns.get(camp_id)
        if not camp_name:
            errors.append(f"Campaign {camp_id} missing for {ag_name}")
            continue

        ads = ranking.get(ag_id, [])
        if not ads:
            errors.append(f"No ads in ranking for {ag_name} ({ag_id})")
            continue

        keeper_id, pause_ids, is_solo = apply_pause_rule(ads, content)

        if is_solo:
            solo_groups.append(ag_name)
        else:
            keeper_groups.append((ag_name, keeper_id))

        # URL from keeper (or first ad if solo)
        src_id = keeper_id or ads[0]["ad_id"]
        url_entry = urls.get(src_id, urls.get(ads[0]["ad_id"], {}))

        creates.append(make_create(camp_name, ag_name, headlines, descriptions, url_entry))

        for pid in pause_ids:
            ce = content.get(pid, {})
            if not ce.get("headlines"):
                errors.append(f"No content for ad_id={pid} in {ag_name}")
                continue
            ue = urls.get(pid, url_entry)
            pauses.append(make_pause(camp_name, ag_name, pid, ce, ue))

    # Report
    if errors:
        print(f"\nERRORS ({len(errors)}):")
        for e in errors[:20]:
            print(f"  {e}")

    dki_paused = sum(1 for r in pauses if content.get(r.get("Ad ID", ""), {}).get("has_dki", False))

    print(f"\n{'='*60}")
    print(f"SUMMARY")
    print(f"{'='*60}")
    print(f"  Creates:  {len(creates)} (BCC-01 fingerprint e1fd7d1595)")
    print(f"  Pauses:   {len(pauses)} ({dki_paused} DKI, {len(pauses)-dki_paused} non-DKI)")
    print(f"  Keepers:  {len(keeper_groups)} (DKI-free incumbent stays)")
    print(f"  Solo:     {len(solo_groups)} (BCC-01 alone)")

    if solo_groups:
        print(f"\n  Solo groups (verify — should be carrier-recognizable):")
        for sg in solo_groups:
            print(f"    {sg}")

    print(f"\n  Assertions:")
    print(f"    Creates == scope: {len(creates)} == {len(scope)}: "
          f"{'PASS' if len(creates) == len(scope) else 'FAIL'}")

    if dry_run:
        print(f"\n{'='*60}")
        print("DRY RUN — no files written")
        print(f"\nPer-group detail:")
        for t in scope:
            ag_id, ag_name = t["ad_group_id"], t["ad_group_name"]
            ads = ranking.get(ag_id, [])
            if ads:
                keeper_id, pause_ids, is_solo = apply_pause_rule(ads, content)
                tag = "SOLO" if is_solo else f"keep={keeper_id}"
                print(f"  {ag_name[:45]:45} {tag:25} pause=[{','.join(pause_ids)}]")
        return

    path = os.path.join(output_dir, f"bcc01_combined_{date_stamp}.csv")
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=get_headers(), extrasaction="ignore")
        writer.writeheader()
        for row in creates + pauses:
            writer.writerow(row)

    print(f"\n  File: {path}")
    print(f"    ({len(creates)} creates + {len(pauses)} pauses)")
    print(f"\n{'='*60}")
    print("POST-IMPORT:")
    print("  1. Sync: 02_sync_rsa.py --days 14")
    print("  2. reconcile_fingerprint.py --expect e1fd7d1595 --label BCC-01 --apply")
    print("  3. Acceptance output → DB-architect gate")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="BCC-01 Rev-2 Import Generator")
    p.add_argument("--copy-csv", required=True)
    p.add_argument("--scope-csv", required=True)
    p.add_argument("--ranking-csv", required=True)
    p.add_argument("--content-csv", required=True)
    p.add_argument("--urls-csv", required=True)
    p.add_argument("--campaigns-csv", required=True)
    p.add_argument("--output-dir", default=OUTPUT_DIR)
    p.add_argument("--dry-run", action="store_true")
    a = p.parse_args()

    generate(a.copy_csv, a.scope_csv, a.ranking_csv, a.content_csv,
             a.urls_csv, a.campaigns_csv, a.output_dir, a.dry_run)
