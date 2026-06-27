#!/usr/bin/env python
"""
USIA Step 1 — Keyword Planner demand pull (DRY-RUN / capture only).

WHAT THIS IS
------------
First-iteration capture tool for the USIA demand-sizing research. It calls the
Google Ads Keyword Planner (GenerateKeywordIdeas), captures the full field set
the build spec asks for, applies the vertical-purity classification, and writes
the result to a CSV + a console summary.

It DELIBERATELY HAS NO DATABASE CONNECTION. There is no code path that touches
demand_research.db (or any DB). The DB writer is a later iteration, added once
the captured columns + filters are settled. So you can dry-run freely.

WORKFLOW
--------
1. --plan-only        -> prints the request plan, makes NO API calls (free).
2. (default)          -> calls KWP, writes CSV, prints summary. No DB.
3. paste the summary  -> we tune seeds / filters / columns, then iterate.

USAGE
-----
  # see what would run, no API call:
  python usia_kwp_pull.py --vertical home --theme generic --plan-only

  # real capture, national, generic home roots:
  python usia_kwp_pull.py --vertical home --theme generic

  # high-intent renters in a specific state:
  python usia_kwp_pull.py --vertical renters --theme high_intent -l 21137

  # override seeds entirely:
  python usia_kwp_pull.py --vertical home --theme generic -k "home insurance" "house insurance"

  # see the cleaned set (cross-vertical bleed dropped, not just tagged):
  python usia_kwp_pull.py --vertical home --theme generic --vertical-filter apply

NOTES / THINGS TO VERIFY AT BUILD TIME  (search "VERIFY")
--------------------------------------------------------
- API pinned to v23. Re-verify GenerateKeywordIdeasRequest field paths against it.
- Month enum -> month-number mapping assumes JANUARY=2..DECEMBER=13. Verify.
- keyword normalization here is a placeholder; align it to the 07-11 tagging
  pipeline's normalizer before the DB-writer iteration so the ATTACH-join matches.
"""

import argparse
import csv
import json
import os
import re
import sys
import time
from datetime import date

# Google Ads import is needed only for live pulls. --plan-only must work without
# a configured yaml, but the package itself is expected to be installed.
from google.ads.googleads.client import GoogleAdsClient
from google.ads.googleads.errors import GoogleAdsException

from brand_tagger import (BrandTagger, normalize_keyword, normalize_for_match,
                          NON_BIDDABLE_OVERRIDE)

# Match the rest of the ops scripts: let .env populate GOOGLE_ADS_CONFIGURATION_FILE_PATH.
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------
DEFAULT_API_VERSION = "v23"  # VERIFY: do not use v16. Some installs ignore the version= kwarg.
DEFAULT_CUSTOMER_ID = "8886502147"  # Google Home account; KWP demand is account-agnostic.
DEFAULT_CONFIG_PATH = "authentication/google-ads.yaml"  # relative fallback only; prefer --config / .env
DEFAULT_GEO_ID = "2840"        # national (United States)
DEFAULT_LANGUAGE_ID = "1000"   # English

# Anchor outputs to this script's own folder so the path is cwd-independent:
#   C:\Projects\google-ads-ops\kw-planner\home_insurance\output\
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_OUTPUT_DIR = os.path.join(SCRIPT_DIR, "output")

# Brand / DNB tagging
DEFAULT_BRAND_DB = r"C:\Projects\google-ads-ops\db\paid_search.db"
DEFAULT_HOME_LP = "https://homeinsurance.quote.com/"  # vertical anchor for keyword_and_url seed
# Stopgap DNB until the registry update lands (Lemonade/TGS still 'approved' there).
# NON_BIDDABLE_OVERRIDE now lives in brand_tagger.py (imported above).

# 50 states + DC -> Google geo target constant IDs (embedded so --geo-loop needs no file).
STATE_GEO_IDS = {
    "Alabama": 21133, "Alaska": 21132, "Arizona": 21136, "Arkansas": 21135,
    "California": 21137, "Colorado": 21138, "Connecticut": 21139, "Delaware": 21141,
    "District of Columbia": 21140, "Florida": 21142, "Georgia": 21143, "Hawaii": 21144,
    "Idaho": 21146, "Illinois": 21147, "Indiana": 21148, "Iowa": 21145,
    "Kansas": 21149, "Kentucky": 21150, "Louisiana": 21151, "Maine": 21154,
    "Maryland": 21153, "Massachusetts": 21152, "Michigan": 21155, "Minnesota": 21156,
    "Mississippi": 21158, "Missouri": 21157, "Montana": 21159, "Nebraska": 21162,
    "Nevada": 21166, "New Hampshire": 21163, "New Jersey": 21164, "New Mexico": 21165,
    "New York": 21167, "North Carolina": 21160, "North Dakota": 21161, "Ohio": 21168,
    "Oklahoma": 21169, "Oregon": 21170, "Pennsylvania": 21171, "Rhode Island": 21172,
    "South Carolina": 21173, "South Dakota": 21174, "Tennessee": 21175, "Texas": 21176,
    "Utah": 21177, "Vermont": 21179, "Virginia": 21178, "Washington": 21180,
    "West Virginia": 21183, "Wisconsin": 21182, "Wyoming": 21184,
}

# ----------------------------------------------------------------------------
# Seed architecture (spec section 3). Lead root = first entry per vertical.
# ----------------------------------------------------------------------------
ROOTS = {
    "home":    ["home insurance", "homeowners insurance", "house insurance", "property insurance"],
    "renters": ["renters insurance", "renter's insurance", "apartment insurance", "tenant insurance"],
}

HIGH_INTENT_MODIFIERS = [
    "agent", "phone number", "contact", "call",
    "near me", "local", "talk to an agent", "quote by phone",
]

# Peril / catastrophe seeds (theme=peril). Grounded in the sqp finding that fire/FAIR and
# flood/wind/catastrophe queries already serve and convert on Home (CvR ~20%, CPL below
# Standard) yet were never seeded into the standard pull. One representative head per peril
# family; KWP expands each into its tail. per_seed mode (theme default) keeps each family's
# expansion provenance-separated so we can see which perils carry demand by state.
PERIL_SEEDS = [
    # fire / wildfire / FAIR plan (insurer of last resort)
    "fire insurance", "wildfire insurance", "fair plan insurance", "home fire insurance",
    # flood / NFIP
    "flood insurance", "home flood insurance",
    # wind / hail / hurricane / tropical
    "windstorm insurance", "wind and hail insurance", "hurricane insurance", "hail damage insurance",
    # earthquake / seismic
    "earthquake insurance",
    # hazard / catastrophe / named peril (umbrella heads)
    "hazard insurance", "catastrophe insurance", "named peril insurance", "disaster insurance",
    # other discrete perils with their own demand
    "tornado insurance", "sinkhole insurance",
]

# ----------------------------------------------------------------------------
# Vertical token map  <-- THIS is the list you'll most want to tweak.
# Word-boundary matching, so "current"/"parent"/"transparent" do NOT match "rent".
# Apostrophes are stripped before matching, so "renter's" -> "renters" matches.
#
# A returned idea is classified by which verticals' tokens it contains:
#   foreign token, no native token   -> cross_vertical  (excluded by default)
#   native token, no foreign token   -> on_vertical
#   both native and foreign          -> mixed           (kept, flagged)
#   no vertical token at all         -> neutral         (kept, flagged)
# The high-intent contact modifiers ("agent", "near me", "phone") expand into the
# whole insurance contact universe -- auto especially -- so multi-vertical detection
# (not just renters) is load-bearing.
# ----------------------------------------------------------------------------
VERTICALS = {
    "home":       ["home", "homeowner", "homeowners", "house", "houses",
                   "household", "households", "dwelling", "hazard",
                   "property", "properties", "condo", "condos",
                   # peril tokens: these ARE home-coverage perils, so a returned idea like
                   # "flood insurance" / "wildfire insurance" should read on_vertical (home),
                   # not neutral. Single-token; multiword "fair plan" handled by re.escape.
                   "fire", "wildfire", "flood", "windstorm", "hurricane", "hail",
                   "earthquake", "tornado", "sinkhole", "catastrophe", "catastrophic",
                   "peril", "perils", "nfip", "fair plan"],
    "renters":    ["rent", "rents", "rental", "rentals", "renter", "renters",
                   "tenant", "tenants", "apartment", "apartments", "apt", "apts"],
    "auto":       ["auto", "autos", "car", "cars", "vehicle", "vehicles",
                   "automobile", "automobiles", "motorcycle", "motorcycles",
                   "truck", "trucks"],
    "health":     ["health", "medical", "medicare", "medicaid", "dental", "vision"],
    "life":       ["life", "funeral", "burial"],
    "pet":        ["pet", "pets", "dog", "dogs", "cat", "cats"],
    "travel":     ["travel", "trip", "flight", "flights"],
    "marine":     ["boat", "boats", "rv", "rvs", "watercraft", "yacht"],
    "commercial": ["business", "commercial"],
}
VERTICAL_RE = {
    v: re.compile(r"\b(?:" + "|".join(map(re.escape, toks)) + r")\b")
    for v, toks in VERTICALS.items()
}

# Month enum (MonthOfYearEnum) -> calendar month number.  VERIFY against v23.
_MONTH_ENUM_TO_NUM = {2: 1, 3: 2, 4: 3, 5: 4, 6: 5, 7: 6,
                      8: 7, 9: 8, 10: 9, 11: 10, 12: 11, 13: 12}

CSV_FIELDS = [
    "pull_date", "vertical", "theme", "seed_method", "seed_term", "seed_url",
    "geo_constant_id", "geo_name",
    "keyword_text", "keyword_text_raw",
    "avg_monthly_searches", "competition", "competition_index",
    "low_cpc_usd", "high_cpc_usd",
    "vertical_match", "detected_verticals", "would_exclude",
    "brand", "brand_bid_status", "brand_category", "dnb_flag", "brand_source",
    "months_captured", "monthly_volumes_json",
]


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------
def detect_verticals(text):
    """Return the verticals whose tokens appear in the text (in map order)."""
    norm = normalize_for_match(text)
    return [v for v, rx in VERTICAL_RE.items() if rx.search(norm)]


def classify_vertical(vertical, text):
    """Return (vertical_match, would_exclude_bool, detected_csv).

    on_vertical    native token present, no foreign vertical
    neutral        no vertical token at all (rode the seed; e.g. 'insurance agency')
    cross_vertical foreign vertical present, native absent  (excluded by default)
    mixed          native + foreign both present (e.g. 'home and auto insurance')
    """
    detected = detect_verticals(text)
    native = vertical in detected
    foreign = [v for v in detected if v != vertical]
    if foreign and native:
        # Home+auto bundle exception: a keyword carrying the home token AND auto is
        # home-buying intent ("home and auto insurance" only makes sense in the home
        # account, never the auto account). Promote to on_vertical so branded bundles
        # flow to conquest and generic bundles flow to the home BUNDLE group, instead of
        # being orphaned as 'mixed'. Scoped to auto deliberately: home+commercial and
        # home+health stay 'mixed' so out-of-scope verticals aren't pulled into the home build.
        if vertical == "home" and "auto" in detected:
            match = "on_vertical"
        else:
            match = "mixed"
    elif foreign:
        match = "cross_vertical"
    elif native:
        match = "on_vertical"
    else:
        match = "neutral"
    return match, (match == "cross_vertical"), ",".join(detected)


# Brand / DNB tagging (BrandTagger) now lives in brand_tagger.py (imported above).


def month_num(month_enum_val):
    try:
        return _MONTH_ENUM_TO_NUM[int(month_enum_val)]
    except Exception:
        iv = int(getattr(month_enum_val, "value", month_enum_val))
        return _MONTH_ENUM_TO_NUM.get(iv, iv)


def extract_monthly(metrics):
    out = []
    for v in getattr(metrics, "monthly_search_volumes", []) or []:
        try:
            ym = "%04d-%02d" % (int(v.year), month_num(v.month))
            out.append({"ym": ym, "searches": int(v.monthly_searches)})
        except Exception:
            continue
    return out


def enum_name(val):
    return getattr(val, "name", str(val))


def micros_to_usd(micros):
    try:
        return round(int(micros) / 1e6, 2)
    except Exception:
        return 0.0


def default_mode_for_theme(theme):
    # Hybrid default: bundle interchangeable synonyms; per-seed where the seed IS the signal.
    return "bundled" if theme in ("generic", "competitor", "brand") else "per_seed"


def load_client(args):
    """Resolve config path (explicit --config > env/.env > relative default) and load,
    tolerating google-ads installs that don't accept the version= kwarg."""
    if args.config:
        os.environ["GOOGLE_ADS_CONFIGURATION_FILE_PATH"] = args.config
    elif not os.environ.get("GOOGLE_ADS_CONFIGURATION_FILE_PATH"):
        os.environ["GOOGLE_ADS_CONFIGURATION_FILE_PATH"] = DEFAULT_CONFIG_PATH
        print("  [warn] no --config and no GOOGLE_ADS_CONFIGURATION_FILE_PATH set; using")
        print("         relative default %r (resolves only from the right cwd)." % DEFAULT_CONFIG_PATH)
    print("  config_path   : %s" % os.environ["GOOGLE_ADS_CONFIGURATION_FILE_PATH"])

    ver = (args.api_version or "").strip()
    if ver and ver.lower() != "default":
        try:
            return GoogleAdsClient.load_from_storage(version=ver)
        except TypeError:
            print("  [warn] installed google-ads ignores version=; loading library default version.")
        except ValueError as e:
            print("  [warn] version %r rejected (%s); loading library default version." % (ver, e))
    return GoogleAdsClient.load_from_storage()


def build_seeds(vertical, theme, override, state_name):
    """Return (seed_method, [seed_terms])."""
    if override:
        return "keyword", list(override)

    roots = ROOTS[vertical]
    lead = roots[0]

    if theme == "generic":
        return "keyword", list(roots)
    if theme == "peril":
        if vertical != "home":
            sys.exit("theme=peril is home-only (peril seeds are home-coverage terms).")
        return "keyword", list(PERIL_SEEDS)
    if theme == "high_intent":
        return "keyword", ["%s %s" % (lead, m) for m in HIGH_INTENT_MODIFIERS]
    if theme == "generic_geo":
        if not state_name:
            sys.exit("theme=generic_geo needs --state \"Texas\" to build [root] [state] seeds.")
        return "keyword", ["%s %s" % (r, state_name) for r in roots[:2]]
    if theme in ("competitor", "brand"):
        sys.exit("theme=%s seeds are not bundled in this iteration; pass them with -k." % theme)
    sys.exit("unknown theme: %s" % theme)


# ----------------------------------------------------------------------------
# KWP call
# ----------------------------------------------------------------------------
def generate_ideas(client, customer_id, geo_id, language_id, seed_terms, seed_url=None):
    svc = client.get_service("KeywordPlanIdeaService")
    geo_rn = client.get_service("GeoTargetConstantService").geo_target_constant_path(geo_id)
    lang_rn = client.get_service("GoogleAdsService").language_constant_path(language_id)

    request = client.get_type("GenerateKeywordIdeasRequest")
    request.customer_id = customer_id
    request.language = lang_rn
    request.geo_target_constants = [geo_rn]
    request.include_adult_keywords = False
    # GOOGLE_SEARCH only -- partners inflate demand (spec section 2 / 5).
    request.keyword_plan_network = client.enums.KeywordPlanNetworkEnum.GOOGLE_SEARCH

    # Exactly one seed oneof may be set. URL anchors expansion to the page topic;
    # keep these runs provenance-separated from pure keyword runs (spec section 6).
    if seed_url and seed_terms:
        request.keyword_and_url_seed.url = seed_url
        request.keyword_and_url_seed.keywords.extend(seed_terms)
    elif seed_url:
        request.url_seed.url = seed_url
    else:
        request.keyword_seed.keywords.extend(seed_terms)

    delay = 1
    last_exc = None
    for attempt in range(3):
        try:
            return list(svc.generate_keyword_ideas(request=request))
        except GoogleAdsException as ex:
            last_exc = ex
            time.sleep(delay)
            delay *= 2
    raise last_exc


def geo_name_for(client, geo_id):
    try:
        ga = client.get_service("GoogleAdsService")
        q = ("SELECT geo_target_constant.name FROM geo_target_constant "
             "WHERE geo_target_constant.id = %s" % geo_id)
        for row in ga.search(customer_id=ARGS.customer_id, query=q):
            return row.geo_target_constant.name
    except Exception:
        pass
    return "geo_%s" % geo_id


# ----------------------------------------------------------------------------
# Seed-category (intent-modifier) classifier -- used for the summary breakdown
# only (not stored), so the capture schema stays locked. Multi-label: a keyword
# can fall in several families. Answers "which categories deserve dedicated seeds".
# ----------------------------------------------------------------------------
MODIFIER_CATEGORIES = {
    "contact":    r"\b(near me|agent|agents|agency|agencies|broker|brokers|phone|call|contact|local|number)\b",
    "price":      r"\b(cheap|cheapest|affordable|inexpensive|low cost|low-cost|discount|budget)\b",
    "quote":      r"\b(quote|quotes|quotation)\b",
    "comparison": r"\b(best|compare|comparison|companies|company|top|reviews?|ratings?)\b",
    "cost":       r"\b(cost|costs|average|how much|rate|rates|premium|pricing)\b",
    "coverage":   r"\b(dwelling|liability|hazard|flood|fire|coverage|policy|deductible|replacement)\b",
    "home_type":  r"\b(condo|condos|mobile home|manufactured|townhouse|townhome)\b",
    "bundle":     r"\b(bundle|bundling|home and auto|auto and home|car and home|home and car)\b",
}
MODIFIER_RE = {k: re.compile(v) for k, v in MODIFIER_CATEGORIES.items()}


def categorize(text):
    n = normalize_for_match(text)
    return [k for k, rx in MODIFIER_RE.items() if rx.search(n)]


def trend_pct(monthly_json):
    """Direction signal: recent half vs older half of the 12-month array.
    Positive = rising. Heavily seasonal for home -- read as direction, not magnitude."""
    try:
        arr = json.loads(monthly_json or "[]")
    except (json.JSONDecodeError, TypeError):
        return None
    if len(arr) < 6:
        return None
    arr = sorted(arr, key=lambda x: x.get("ym", ""))
    half = len(arr) // 2
    first = sum(x.get("searches", 0) for x in arr[:half])
    last = sum(x.get("searches", 0) for x in arr[half:])
    return round(100.0 * (last - first) / first, 1) if first > 0 else None


# ----------------------------------------------------------------------------
# Run
# ----------------------------------------------------------------------------
def run(args):
    seed_method, seeds = build_seeds(args.vertical, args.theme, args.keyword_texts, args.state)
    pull_date = date.today().isoformat()
    mode = args.mode or default_mode_for_theme(args.theme)
    if args.seed_url:
        seed_method = "keyword_and_url" if seeds else "url"

    # geo scope: single geo, or loop over 50 states + DC (+ optional national)
    if args.geo_loop:
        geos = [(str(cid), name) for name, cid in STATE_GEO_IDS.items()]
        if args.include_national:
            geos = [("2840", "United States")] + geos
        geo_tag = "states_natl" if args.include_national else "states"
    else:
        geos = [(args.geo_id, None)]   # name resolved after client load
        geo_tag = args.geo_id

    if args.out is None:
        # include seed_method + mode + geo scope so runs never overwrite each other.
        args.out = os.path.join(
            DEFAULT_OUTPUT_DIR,
            "usia_dryrun_%s_%s_%s_%s_%s.csv"
            % (args.vertical, args.theme, seed_method, mode, geo_tag))

    print("=" * 72)
    print("USIA KWP pull  (DRY-RUN, no DB writes)")
    print("=" * 72)
    print("  pull_date     : %s" % pull_date)
    print("  customer_id   : %s" % args.customer_id)
    print("  vertical      : %s" % args.vertical)
    print("  theme         : %s" % args.theme)
    print("  seed_method   : %s" % seed_method)
    if args.seed_url:
        print("  seed_url      : %s" % args.seed_url)
    print("  mode          : %s%s" % (mode, "" if args.mode else "  (theme default)"))
    if args.geo_loop:
        n_seed_req = len(seeds) if mode == "per_seed" else 1
        print("  geo scope     : %d geos (%s)" % (
            len(geos), "50 states + DC + national" if args.include_national else "50 states + DC"))
        print("  est. requests : ~%d  (%d geos x %d seed-requests)" % (
            len(geos) * n_seed_req, len(geos), n_seed_req))
    else:
        print("  geo_id        : %s" % args.geo_id)
    print("  language_id   : %s" % args.language_id)
    print("  network       : GOOGLE_SEARCH")
    print("  api_version   : %s" % args.api_version)
    print("  vertical_filter: %s" % args.vertical_filter)
    if mode == "bundled":
        print("  [note] bundled mode -> provenance is CELL-level, not per-seed. The spec wants")
        print("         per-seed provenance where seed identity is signal (high_intent, generic_geo).")
    print("  seeds (%d):" % len(seeds))
    for s in seeds:
        print("      - %s" % s)

    if args.plan_only:
        print("\n[plan-only] no API call made. Drop --plan-only to capture for real.")
        return

    client = load_client(args)

    global ARGS
    ARGS = args
    tagger = None if args.no_brand_tag else BrandTagger(args.brand_db)
    if args.no_brand_tag:
        print("  brand tagging : disabled (--no-brand-tag)")
    elif tagger and tagger.sources:
        print("  brand tagging : %s" % " | ".join(tagger.sources))

    if seeds and mode == "per_seed":
        seed_iter = [(s, [s]) for s in seeds]          # one request per seed
    elif seeds:
        seed_iter = [("(bundled)", seeds)]             # one request, all seeds
    else:
        seed_iter = [("(url_only)", [])]               # url_seed, no keywords

    rows = []
    print("\nquerying KWP across %d geo(s) ..." % len(geos))
    for gi, (geo_id, geo_name) in enumerate(geos, 1):
        if geo_name is None:
            geo_name = geo_name_for(client, geo_id)
        if len(geos) > 1:
            print("  [%2d/%d] %s" % (gi, len(geos), geo_name))
        for seed_label, seed_batch in seed_iter:
            ideas = generate_ideas(client, args.customer_id, geo_id,
                                   args.language_id, seed_batch, seed_url=args.seed_url)
            for idea in ideas:
                m = idea.keyword_idea_metrics
                raw = idea.text
                norm = normalize_keyword(raw)
                vmatch, would_excl, detected = classify_vertical(args.vertical, raw)
                if tagger:
                    brand, bid, cat, dnb, bsrc = tagger.tag(norm)
                else:
                    brand, bid, cat, dnb, bsrc = "", "", "", False, ""
                monthly = extract_monthly(m)
                rows.append({
                    "pull_date": pull_date,
                    "vertical": args.vertical,
                    "theme": args.theme,
                    "seed_method": seed_method,
                    "seed_term": seed_label if seed_label not in ("(bundled)", "(url_only)") else "|".join(seeds),
                    "seed_url": args.seed_url or "",
                    "geo_constant_id": geo_id,
                    "geo_name": geo_name,
                    "keyword_text": norm,
                    "keyword_text_raw": raw,
                    "avg_monthly_searches": int(getattr(m, "avg_monthly_searches", 0) or 0),
                    "competition": enum_name(getattr(m, "competition", "")),
                    "competition_index": int(getattr(m, "competition_index", 0) or 0),
                    "low_cpc_usd": micros_to_usd(getattr(m, "low_top_of_page_bid_micros", 0)),
                    "high_cpc_usd": micros_to_usd(getattr(m, "high_top_of_page_bid_micros", 0)),
                    "vertical_match": vmatch,
                    "detected_verticals": detected,
                    "would_exclude": would_excl,
                    "brand": brand,
                    "brand_bid_status": bid,
                    "brand_category": cat,
                    "dnb_flag": dnb,
                    "brand_source": bsrc,
                    "months_captured": len(monthly),
                    "monthly_volumes_json": json.dumps(monthly, separators=(",", ":")),
                })
            time.sleep(1)  # be polite between requests

    if args.vertical_filter == "apply":
        kept = [r for r in rows if not r["would_exclude"]]
    else:
        kept = rows

    write_csv(args.out, kept)
    summarize(rows, kept, args, seeds[0] if seeds else "")


def write_csv(path, rows):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow(r)


def distinct_by_keyword(rows):
    """Collapse provenance-duplicate rows to one per keyword_text (highest-volume instance).
    For display blocks only -- counts/breakdowns still use full provenance rows."""
    best = {}
    for r in rows:
        k = r["keyword_text"]
        cur = best.get(k)
        if cur is None or r["avg_monthly_searches"] > cur["avg_monthly_searches"]:
            best[k] = r
    return list(best.values())


def summarize(all_rows, written_rows, args, primary_seed=""):
    print("\n" + "=" * 72)
    print("CAPTURE SUMMARY")
    print("=" * 72)
    total = len(all_rows)
    distinct = len(set(r["keyword_text"] for r in all_rows))
    print("  rows captured (seed x idea) : %d" % total)
    print("  distinct keyword_text       : %d" % distinct)

    # vertical-purity breakdown
    from collections import Counter
    vc = Counter(r["vertical_match"] for r in all_rows)
    excl = sum(1 for r in all_rows if r["would_exclude"])
    print("\n  vertical_match breakdown:")
    for k in ("on_vertical", "neutral", "mixed", "cross_vertical"):
        if vc.get(k):
            print("      %-15s %d" % (k, vc[k]))
    foreign = Counter()
    for r in all_rows:
        if r["vertical_match"] in ("cross_vertical", "mixed"):
            for v in r["detected_verticals"].split(","):
                if v and v != args.vertical:
                    foreign[v] += 1
    if foreign:
        print("      foreign verticals detected (cross + mixed):")
        for v, n in foreign.most_common():
            print("          %-12s %d" % (v, n))
    pct = (100.0 * excl / total) if total else 0.0
    verb = "DROPPED" if args.vertical_filter == "apply" else "would be dropped"
    print("      -> %d cross_vertical rows %s (%.1f%%) under the filter" % (excl, verb, pct))

    # data quality flags
    zero_vol = sum(1 for r in all_rows if r["avg_monthly_searches"] == 0)
    no_months = sum(1 for r in all_rows if r["months_captured"] == 0)
    zero_cpc = sum(1 for r in all_rows if r["low_cpc_usd"] == 0 and r["high_cpc_usd"] == 0)
    print("\n  data-quality flags:")
    print("      avg_monthly_searches == 0 : %d" % zero_vol)
    print("      empty monthly array       : %d  (expect ~0; if high, month parse may be off)" % no_months)
    print("      no CPC (both bids 0)      : %d" % zero_cpc)

    # brand / DNB breakdown
    branded = [r for r in all_rows if r["brand"]]
    if branded:
        dnb = sum(1 for r in all_rows if r["dnb_flag"])
        print("\n  brand / DNB breakdown:")
        print("      brand-tagged rows         : %d" % len(branded))
        print("      do-not-bid (restricted)   : %d" % dnb)
        bstat = Counter(r["brand_bid_status"] for r in branded if r["brand_bid_status"])
        if bstat:
            print("      by bid_status:")
            for k, n in bstat.most_common():
                print("          %-12s %d" % (k, n))
        topb = Counter(r["brand"] for r in branded)
        print("      top brands detected:")
        for b, n in topb.most_common(8):
            print("          %-22s %d" % (b[:22], n))

    # top by volume (collapsed to distinct keywords so per-seed dupes don't fill the list)
    top = sorted(distinct_by_keyword(all_rows), key=lambda r: r["avg_monthly_searches"], reverse=True)[:15]
    print("\n  top 15 distinct ideas by avg monthly searches:")
    print("      %-38s %9s %8s  %-14s" % ("keyword_text", "avg", "cpc_hi", "vertical_match"))
    for r in top:
        print("      %-38s %9d %8.2f  %-14s" % (
            r["keyword_text"][:38], r["avg_monthly_searches"],
            r["high_cpc_usd"], r["vertical_match"]))

    # a peek at what the filter catches, so you can eyeball the token list
    caught = sorted(distinct_by_keyword([r for r in all_rows if r["would_exclude"]]),
                    key=lambda r: r["avg_monthly_searches"], reverse=True)[:10]
    if caught:
        print("\n  sample of filter-caught ideas (eyeball the token list):")
        for r in caught:
            print("      [%s:%s] %-38s avg=%d" % (
                r["vertical_match"], r["detected_verticals"] or "-",
                r["keyword_text"][:38], r["avg_monthly_searches"]))

    # seed-category coverage -- which intent families the expansion spans (Q3)
    dk = distinct_by_keyword(all_rows)
    catc = Counter()
    catv = Counter()
    for r in dk:
        for c in categorize(r["keyword_text"]):
            catc[c] += 1
            catv[c] += r["avg_monthly_searches"]
    if catc:
        print("\n  seed-category coverage (distinct keywords, multi-label):")
        for c, _ in catv.most_common():
            print("      %-11s %4d kw   vol~%s" % (c, catc[c], "{:,}".format(catv[c])))

    # per-state ranking + trend for the primary seed (geo-loop runs) -- Q1 + Q2
    geos_seen = set(r["geo_constant_id"] for r in all_rows)
    if len(geos_seen) > 1 and primary_seed:
        ps = normalize_keyword(primary_seed)
        bg = {}
        for r in all_rows:
            if r["keyword_text"] == ps:
                nm = r["geo_name"]
                av = r["avg_monthly_searches"]
                if nm not in bg or av > bg[nm][0]:
                    bg[nm] = (av, trend_pct(r["monthly_volumes_json"]))
        ranked = sorted(bg.items(), key=lambda x: -x[1][0])
        if ranked:
            def fmt(nm, av, tr):
                return "          %-22s %9s   %s" % (
                    nm[:22], "{:,}".format(av), ("%+.0f%%" % tr) if tr is not None else "   n/a")
            print("\n  per-state demand for '%s' (volume, H2-vs-H1 trend):" % primary_seed)
            print("      highest:")
            for nm, (av, tr) in ranked[:10]:
                print(fmt(nm, av, tr))
            print("      lowest:")
            for nm, (av, tr) in ranked[-5:]:
                print(fmt(nm, av, tr))
            rising = sorted([(nm, tr) for nm, (av, tr) in bg.items() if tr is not None],
                            key=lambda x: -x[1])[:8]
            if rising:
                trs = [tr for _, (av, tr) in bg.items() if tr is not None]
                pos = sum(1 for t in trs if t > 0)
                if trs and pos / len(trs) > 0.75:
                    print("      [note] %d of %d states positive -> this is the SEASONAL ramp, not growth."
                          % (pos, len(trs)))
                    print("             true YoY needs the historical-metrics endpoint (24-month range).")
                print("      highest H2-vs-H1 (seasonality-confounded, not a growth ranking):")
                for nm, tr in rising:
                    print("          %-22s %+.0f%%" % (nm[:22], tr))

    print("\n  CSV written: %s  (%d rows)" % (args.out, len(written_rows)))
    print("=" * 72)


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------
def parse_args():
    p = argparse.ArgumentParser(description="USIA Step 1 KWP demand pull (dry-run, no DB).")
    p.add_argument("-c", "--customer_id", default=DEFAULT_CUSTOMER_ID,
                   help="Google Ads customer ID (default = Home account 8886502147).")
    p.add_argument("--vertical", required=True, choices=["home", "renters"])
    p.add_argument("--theme", required=True,
                   choices=["generic", "generic_geo", "high_intent", "peril", "competitor", "brand"])
    p.add_argument("-l", "--geo_id", default=DEFAULT_GEO_ID,
                   help="geo target constant ID (default 2840 = national).")
    p.add_argument("--geo-loop", action="store_true",
                   help="loop the pull over all 50 states + DC into one CSV (per-state rows). "
                        "Adds a per-state ranking + trend for the primary seed to the summary.")
    p.add_argument("--include-national", action="store_true",
                   help="with --geo-loop, also include national (2840).")
    p.add_argument("-i", "--language_id", default=DEFAULT_LANGUAGE_ID)
    p.add_argument("-k", "--keyword_texts", nargs="+",
                   help="override seeds (skips the built-in seed set).")
    p.add_argument("--state", help="state name, for theme=generic_geo seed building.")
    p.add_argument("--mode", choices=["bundled", "per_seed"], default=None,
                   help="bundled = one request for all seeds; per_seed = one request each. "
                        "Default is theme-aware: bundled for generic/competitor/brand, "
                        "per_seed for high_intent/generic_geo.")
    p.add_argument("--seed-url", default=None,
                   help="anchor expansion to a page (keyword_and_url seed). For the home "
                        "vertical-purity experiment use %s. Runs are stamped "
                        "seed_method=keyword_and_url and kept provenance-separate." % DEFAULT_HOME_LP)
    p.add_argument("--brand-db", default=DEFAULT_BRAND_DB,
                   help="path to paid_search.db for brand/DNB tagging (read-only).")
    p.add_argument("--no-brand-tag", action="store_true",
                   help="disable brand/DNB tagging entirely.")
    p.add_argument("--vertical-filter", choices=["tag", "apply"], default="tag",
                   help="tag = keep everything, just classify; apply = drop cross/off-vertical from CSV.")
    p.add_argument("--config", default=None,
                   help="absolute path to google-ads.yaml. If omitted, uses "
                        "GOOGLE_ADS_CONFIGURATION_FILE_PATH from the environment / .env.")
    p.add_argument("--api-version", default=DEFAULT_API_VERSION,
                   help="API version passed to load_from_storage(version=...). "
                        "Use 'default' to omit the kwarg (matches the other ops scripts).")
    p.add_argument("--out", default=None,
                   help="output CSV path. Default: <script dir>/output/"
                        "usia_dryrun_<vertical>_<theme>_<geo>.csv")
    p.add_argument("--plan-only", action="store_true",
                   help="print the plan and exit; no API call.")
    return p.parse_args()


if __name__ == "__main__":
    ARGS = None
    args = parse_args()
    try:
        run(args)
    except GoogleAdsException as ex:
        print('Request "%s" failed: %s' % (ex.request_id, ex.error.code().name))
        for e in ex.failure.errors:
            print("  %s" % e.message)
        sys.exit(1)
