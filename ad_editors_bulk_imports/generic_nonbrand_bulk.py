#!/usr/bin/env python3
"""Bulk sheet for the Wave 1 generic non-brand ad group: phrase heads mirrored across active campaigns.

Phrase-head strategy (not exact-per-query): individual generic queries carry only 2-7 leads each, so
exact anchors would fragment the Smart Bidding signal. Phrase heads capture the observed queries PLUS the
unobserved long tail sharing the same intent.

Close-variant note: Google treats "home" / "homeowners" as synonyms, so we target ONE variant only.
The account's realized data favors "home" (31 queries / 94 leads vs 15 / 35), so all heads use "home".

Excluded deliberately (not generic demand):
  - CORE theme     : all carrier terms / Kin-Hartford misspellings (close variants already cover these)
  - COMPARE theme  : mostly carrier reviews; only the 2 genuinely generic heads kept
  - ca fair plan   : distinct CA FAIR Plan product intent -- belongs with CA FAIR handling, not here
Negatives are NOT included here (per request) -- but phrase match WILL pull carrier queries into this
group until carrier negatives are added, which will cannibalize the carrier ad groups. Add them before
this runs hot.
"""
import argparse
import csv
import os

AD_GROUP = "Home-Generic-NonBrand-Cost-Core"

# phrase heads -> the observed queries each is intended to cover (for the review sheet)
PHRASE_HEADS = [
    ("home insurance cost",
     "home insurance cost; average home insurance cost zip code; how much is homeowners insurance on a $150,000 house"),
    ("home insurance estimate",
     "home insurance estimate by address; home insurance estimate by zip code; home insurance estimate calculator by address"),
    ("home insurance rates",
     "home insurance rates by zip code; compare home insurance rates by zip code"),
    ("best home insurance",
     "best and worst homeowners insurance companies; best homeowners insurance comparison sites"),
    ("compare home insurance",
     "compare home insurance rates by zip code; comparison-intent long tail"),
]


def load_campaigns(path):
    with open(path, newline="", encoding="utf-8-sig") as f:
        return [r["campaign"] for r in csv.DictReader(f) if r.get("campaign")]


def main():
    ap = argparse.ArgumentParser(description="Wave 1 generic non-brand phrase-head bulk sheet")
    ap.add_argument("--campaigns-csv", required=True, help="inventory campaign_summary.csv (active campaigns)")
    ap.add_argument("--out-dir", default="generic_nonbrand_output")
    a = ap.parse_args()
    campaigns = load_campaigns(a.campaigns_csv)
    print("campaigns: %d" % len(campaigns))
    if len(campaigns) != 7:
        print("  [WARN] expected 7 active campaigns, found %d -- verify before importing." % len(campaigns))
    os.makedirs(a.out_dir, exist_ok=True)

    ag_rows = [(c, AD_GROUP, "Enabled") for c in campaigns]
    kw_rows = [(c, AD_GROUP, kw, "Phrase", "Enabled") for c in campaigns for kw, _ in PHRASE_HEADS]

    def dump(name, header, rows):
        with open(os.path.join(a.out_dir, name), "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(header)
            w.writerows(rows)

    dump("bulk_ad_groups.csv", ["Campaign", "Ad Group", "Status"], ag_rows)
    dump("bulk_keywords.csv", ["Campaign", "Ad Group", "Keyword", "Match Type", "Status"], kw_rows)
    dump("phrase_head_coverage_REVIEW.csv", ["Phrase head", "Match Type", "Intended to cover"],
         [[kw, "Phrase", cov] for kw, cov in PHRASE_HEADS])

    print("ad group: %s (new, mirrored across all campaigns)" % AD_GROUP)
    print("phrase heads: %d | keyword rows: %d (%d heads x %d campaigns)"
          % (len(PHRASE_HEADS), len(kw_rows), len(PHRASE_HEADS), len(campaigns)))
    for kw, _ in PHRASE_HEADS:
        print("   \"%s\"" % kw)
    print("wrote -> %s" % os.path.abspath(a.out_dir))


if __name__ == "__main__":
    main()
