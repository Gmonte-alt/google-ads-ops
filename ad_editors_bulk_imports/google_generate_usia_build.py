#!/usr/bin/env python3
"""
google_generate_usia_build.py
================================================================================
README
================================================================================
WHAT THIS DOES
  Turns the approved USIA (US Insurance Agency) home-insurance keyword/ad-group
  workbooks + the eight pre-built negative-list CSVs into an import-ready Google
  Ads Editor build: campaign / ad-group / keyword CSVs, paste-ready shared
  negative lists, blank RSA shells for the copy thread, and a settings checklist.

  The workbooks are AUTHORITATIVE (see USIA_Editor_Build_Handoff_Prompt.md). This
  script does not invent or drop keywords. It only: normalizes campaign names,
  strips the workbooks' companion-row indent glyph, wires bare-carrier bids from
  the Carrier Selection sheet, appends the 8 homeowners additions, applies the
  negative routing matrix, validates, and writes Editor-friendly files.

HOW TO RUN
  1. Stage all 12 inputs in the input folder (default <repo>/input/usia/):
       - 3 workbooks + usia_conquest_homeowners_additions.csv + the 8 NEG_*.csv
  2. From the repo, in the paid-search env:
       python google_generate_usia_build.py
  3. Read the printed summary; follow output/usia/<run>/SETTINGS_CHECKLIST.md.

  The script preflights all inputs and lists every missing file at once before
  doing any work. It exits 0 on success, 1 if validation raises any FAIL, and 2
  if inputs are missing — so a pipeline can gate on the exit code.

INPUT CONTRACT  (default dir <repo>/input/usia/, override --input-dir)
  usia_home_keywords_v<N>.xlsx           generic, non-brand  -> USIA_HOME_GEN / USIA_HOME_GEN_CALL
  usia_home_brand_v<N>.xlsx              brand conquest      -> USIA_HOME_BRAND_CARRIER (Affinity/Aggregator are ad groups)
  usia_home_brand_generic.xlsx           bare-carrier        -> USIA_HOME_BRAND_GENERIC
  usia_conquest_homeowners_additions.csv 8 supplemental conquest phrase keywords
  usia_adgroup_maxcpc.csv                Max CPC per ad group (Campaign / Ad Group / Max CPC)
  usia_launch_locations.csv              launch states w/ Google criteria IDs (State / Criteria ID / Canonical Name)
  NEG_ADJACENT_VERTICAL.csv  NEG_OFF_VERTICAL.csv  NEG_AUTO_VEHICLE.csv  NEG_SERVICE_NAV.csv
  NEG_BRAND_BUY.csv  NEG_BRAND_DNB.csv  NEG_JUNK.csv  NEG_HOME_ROUTING.csv
       (each: columns keyword, match_type, ui_paste_format)
  The two workbooks are version-resolved automatically (highest _v<N> wins).

OUTPUTS  (default <repo>/output/usia/<run-tag-or-timestamp>/)
  01_campaigns.csv                          campaign shells: type, status, Manual CPC, budget, start date, EU Political Ads
  02_ad_groups.csv                          ad-group shells (Status Enabled) with resolved Max CPC
  03_keywords.csv                           every positive keyword (Status Enabled)
  06_rsa_shells.csv                         one blank RSA per ad group (copy thread fills it)
  07_campaign_locations.csv                 UTF-16 LE / tab (Campaign/ID/Location/Status/Type): 12 state ADDS + US removal + non-US country EXCLUSIONS (Type Negative), one upload
  negative_lists/NEG_*.txt                  paste-ready shared-list content (UI route)
  04_negative_apply_matrix.csv              which shared list attaches to which campaign (UI checklist)
  08_apply_negative_lists.js                Google Ads Script: attach the existing shared lists to campaigns per the matrix
  05_negatives_campaign_level.csv           last-resort fallback: campaign-level negatives (Criterion Type = Negative Phrase/Broad)
  SETTINGS_CHECKLIST.md                     UI steps Editor can't do (portfolios, presence targeting, EU bulk action, call assets)
  build_summary.txt                         validation results, counts, and flags

FLAGS
  --input-dir PATH        input folder (default <repo>/input/usia)
  --output-dir PATH       output parent (default <repo>/output/usia); a subfolder is created per run
  --run-tag NAME          name the output subfolder (default: timestamp)
  --status {Paused,Enabled}   CAMPAIGN status (default Enabled); ad groups + keywords are always Enabled
  --start-date YYYY-MM-DD launch start date (default from config)
  --default-max-cpc 6.00  fallback Max CPC if an ad group has no explicit or neighbor value
  --ai-max                bare-carrier exact-only: drop the redundant same-text phrase twins (Phase-2 regen)

NOTES
  - Four pooled, function-labeled campaigns. Affinity & Aggregator are FORCE-MERGED into
    USIA_HOME_BRAND_CARRIER as ad groups (robust even if a workbook ships them as campaigns).
  - The MetLife ad group is dropped from USIA_HOME_BRAND_CARRIER (DROP_AD_GROUPS).
  - Ad-group Max CPC: explicit value from usia_adgroup_maxcpc.csv, else the campaign's
    neighboring (modal) value, else --default-max-cpc.
  - Negatives: the shared lists can't be linked to campaigns via Editor import. Run
    08_apply_negative_lists.js (Google Ads Script) to attach the existing shared lists per
    the matrix, or apply by hand via 04_negative_apply_matrix.csv. 05_*.csv is a fallback only.
  - Locations: one upload (Campaign/ID/Location/Status) with the 12 state adds + US (2840) removal,
    spaced display name and bare numeric Criteria ID (verified vs Google's geotargets table). Apply via
    Locations -> Make multiple changes -> paste; resolve one location via Edit/find location -> cascades to all.
  - The printed summary contains em-dashes; relies on PYTHONIOENCODING=utf-8 on Windows.

REQUIRES: python 3.9+, pandas, openpyxl.
================================================================================
"""

import argparse
import csv
import re
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

# =============================================================================
# CONFIG  —  resolved from USIA_Launch_Settings.md + the updated handoff.
# =============================================================================
ACCOUNT_LABEL = "USIA Home"

# v3 workbooks carry final campaign names; this map only catches older labels.
CAMPAIGN_NAME_MAP = {
    "Call": "USIA_HOME_GEN_CALL",
    "Generic (tier)": "USIA_HOME_GEN",
}

# Force-merge these campaigns into a target (their ad groups + keywords are absorbed).
# Robust against workbooks that still ship Affinity/Aggregator as their own campaigns.
CAMPAIGN_MERGE = {
    "USIA_HOME_BRAND_AFFINITY": "USIA_HOME_BRAND_CARRIER",
    "USIA_HOME_BRAND_AGGREGATOR": "USIA_HOME_BRAND_CARRIER",
}

# Ad groups to drop entirely, by (campaign, normalized ad group). Applied after merge.
DROP_AD_GROUPS = {
    ("USIA_HOME_BRAND_CARRIER", "metlife"),
    ("USIA_HOME_BRAND_CARRIER", "california fair plan"),
}

# Four pooled, function-labeled campaigns. Campaign groups drive the routing matrix.
# (Affinity & Aggregator are ad groups inside USIA_HOME_BRAND_CARRIER in v3.)
GENERIC_CAMPAIGNS = {"USIA_HOME_GEN", "USIA_HOME_GEN_CALL"}
CONQUEST_CAMPAIGNS = {"USIA_HOME_BRAND_CARRIER"}
BARE_CAMPAIGNS = {"USIA_HOME_BRAND_GENERIC"}
EXPECTED_CAMPAIGNS = GENERIC_CAMPAIGNS | CONQUEST_CAMPAIGNS | BARE_CAMPAIGNS

# Negative routing matrix (handoff). Tuple = (apply to Generic, Conquest, Bare-carrier).
ROUTING_MATRIX = {
    "NEG_ADJACENT_VERTICAL": (True,  True,  True),
    "NEG_OFF_VERTICAL":      (True,  True,  True),
    "NEG_AUTO_VEHICLE":      (False, False, True),
    "NEG_SERVICE_NAV":       (True,  True,  True),
    "NEG_BRAND_BUY":         (True,  False, False),
    "NEG_BRAND_DNB":         (True,  True,  True),
    "NEG_JUNK":              (True,  True,  True),
    "NEG_HOME_ROUTING":      (False, False, True),
}
NEG_LISTS = list(ROUTING_MATRIX.keys())

# Sanity-check reference counts from the handoff (warn — not fail — on mismatch).
NEG_EXPECTED_COUNTS = {"NEG_SERVICE_NAV": 14, "NEG_BRAND_DNB": 50, "NEG_HOME_ROUTING": 5}

# Campaign settings (from USIA_Launch_Settings.md).
CAMPAIGN_TYPE = "Search"
BID_STRATEGY = "Manual CPC"   # import on Manual CPC; portfolios are attached in the UI afterward
START_DATE = "2026-06-24"     # configurable via --start-date
LOCATION_MODE = "Presence (people in or regularly in your targeted locations)"
PHASE2_STATES = ["CA", "TX", "GA", "FL"]   # do NOT enable at launch

# EU political ads self-declaration (required on all campaigns). Column "EU Political Ads".
EU_POLITICAL_ADS = "No"       # = DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING

# Location file is written in the format Google Ads Editor resolves reliably:
# UTF-16 LE, tab-delimited, columns Campaign / ID / Location / Status.
LOC_ENCODING = "utf-16-le"
LOC_DELIM = "\t"
US_GEO_ID = 2840              # United States (country) — removed per campaign to kill the default

# Per-campaign daily budgets (opening settings — calibrate week 1 from search_budget_lost_is).
CAMPAIGN_BUDGETS = {
    "USIA_HOME_GEN":           820,
    "USIA_HOME_GEN_CALL":       25,
    "USIA_HOME_BRAND_CARRIER": 880,
    "USIA_HOME_BRAND_GENERIC": 150,
}

# Portfolio Maximize Clicks bid strategies — BUILT IN THE UI, attached after import.
# (name, max-CPC cap, [campaigns]). Emitted to the checklist as a build step.
BID_PORTFOLIOS = {
    "USIA_MaxClicks_Conquest": {"cap": 13, "campaigns": ["USIA_HOME_BRAND_CARRIER"]},
    "USIA_MaxClicks_Generic":  {"cap": 12, "campaigns": ["USIA_HOME_GEN", "USIA_HOME_GEN_CALL",
                                                          "USIA_HOME_BRAND_GENERIC"]},
}

VALID_MATCH = {"exact", "phrase", "broad"}

# --- Campaign display rename (output-only) -----------------------------------
# Rename campaigns WITHOUT touching the input workbooks or any config above.
# Keys are the canonical internal names; values are the names to emit. All internal
# logic (budgets, negative routing, the 08 script, merge/drop, portfolios, validation)
# keeps using the canonical names; only the strings written to output change.
# An optional usia_campaign_rename.csv (columns: canonical,new_name) in the input dir
# OVERRIDES entries here at runtime — so the next rename is a CSV edit, not a code edit.
CAMPAIGN_RENAME = {
    "USIA_HOME_BRAND_CARRIER": "HOME INSURANCE-USIA_STATE-GROUP_TOPRPL_BRAND-CARRIER_ROAS_SEARCH_ALLDEVICE_USA-STE",
    "USIA_HOME_BRAND_GENERIC": "HOME INSURANCE-USIA_STATE-GROUP_TOPRPL_BRAND-GENERIC_ROAS_SEARCH_ALLDEVICE_USA-STE",
    "USIA_HOME_GEN":           "HOME INSURANCE-USIA_STATE-GROUP_TOPRPL_GENERIC_ROAS_SEARCH_ALLDEVICE_USA-STE",
    "USIA_HOME_GEN_CALL":      "HOME INSURANCE-USIA_STATE-GROUP_TOPRPL_GENERIC-CALL_ROAS_SEARCH_ALLDEVICE_USA-STE",
}


def disp(campaign: str) -> str:
    """Canonical campaign name -> the name to write in output files."""
    return CAMPAIGN_RENAME.get(campaign, campaign)

# Editor display strings for match type.
MATCH_DISPLAY = {"exact": "Exact", "phrase": "Phrase", "broad": "Broad"}

# RSA shell schema (Google Ads Editor). Copy thread fills the blank cells.
RSA_AD_TYPE = "Responsive search ad"
RSA_HEADLINES = 15
RSA_DESCRIPTIONS = 4


# =============================================================================
# Loading helpers
# =============================================================================
def detect_header_row(raw: pd.DataFrame, key: str = "Campaign", scan: int = 15) -> int:
    """Find the 0-based row index whose cells contain `key` (handles the variable
    title-block height across workbooks: generic/conquest land on row 4, bare-carrier
    Keywords/Carrier Selection on row 3)."""
    for i in range(min(len(raw), scan)):
        cells = [str(c).strip() for c in raw.iloc[i].tolist()]
        if key in cells:
            return i
    raise ValueError(f"Could not locate a header row containing '{key}' in the first {scan} rows.")


def read_sheet(path: Path, sheet: str, key: str = "Campaign") -> pd.DataFrame:
    raw = pd.read_excel(path, sheet_name=sheet, header=None)
    hdr = detect_header_row(raw, key=key)
    df = pd.read_excel(path, sheet_name=sheet, header=hdr).dropna(how="all")
    df.columns = [str(c).strip() for c in df.columns]
    return df


_ARROW_RE = re.compile(r"^[\s\u21b3]+")  # leading whitespace + the ↳ companion-indent glyph


def clean_keyword(value) -> str:
    """Strip the companion-row display indent ('   ↳ ') and surrounding whitespace."""
    return _ARROW_RE.sub("", str(value)).strip()


def norm(text) -> str:
    return re.sub(r"\s+", " ", str(text)).strip().lower()


def map_campaign(name: str) -> str:
    return CAMPAIGN_NAME_MAP.get(str(name).strip(), str(name).strip())


# =============================================================================
# Loaders — each returns a normalized keyword frame: campaign, ad_group, keyword, match_type, role, source
# =============================================================================
def load_generic(path: Path) -> pd.DataFrame:
    df = read_sheet(path, "Selected Keywords")
    out = pd.DataFrame({
        "campaign": df["Campaign"].map(map_campaign),
        "ad_group": df["Ad Group"].astype(str).str.strip(),
        "keyword": df["Keyword"].map(clean_keyword),
        "match_type": df["Match Type"].astype(str).str.strip().str.lower(),
        "role": df["Role"].astype(str).str.strip(),
    })
    out["source"] = "generic"
    return out


def load_conquest(path: Path) -> pd.DataFrame:
    df = read_sheet(path, "Brand Keywords")
    out = pd.DataFrame({
        "campaign": df["Campaign"].map(map_campaign),
        "ad_group": df["Ad Group"].astype(str).str.strip(),
        "keyword": df["Keyword"].map(clean_keyword),
        "match_type": df["Match Type"].astype(str).str.strip().str.lower(),
        "role": df["Role"].astype(str).str.strip(),
    })
    out["source"] = "conquest"
    return out


def load_bare(path: Path) -> pd.DataFrame:
    df = read_sheet(path, "Keywords")
    out = pd.DataFrame({
        "campaign": df["Campaign"].map(map_campaign),
        "ad_group": df["Ad Group"].astype(str).str.strip(),
        "keyword": df["Keyword"].map(clean_keyword),
        "match_type": df["Match Type"].astype(str).str.strip().str.lower(),
        "role": "head",
    })
    out["source"] = "bare"
    return out


def load_additions(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    df.columns = [str(c).strip() for c in df.columns]
    out = pd.DataFrame({
        "campaign": df["Campaign"].map(map_campaign),
        "ad_group": df["Ad Group"].astype(str).str.strip(),
        "keyword": df["Keyword"].map(clean_keyword),
        "match_type": df["Match Type"].astype(str).str.strip().str.lower(),
        "role": df.get("Role", "head_homeowners"),
    })
    out["source"] = "additions"
    return out


def load_adgroup_maxcpc(path: Path) -> dict:
    """(campaign, normalized ad group) -> Max CPC, from usia_adgroup_maxcpc.csv."""
    df = pd.read_csv(path)
    df.columns = [str(c).strip() for c in df.columns]
    out = {}
    for _, r in df.iterrows():
        key = (map_campaign(r["Campaign"]), norm(r["Ad Group"]))
        cpc = r["Max CPC"]
        if pd.notna(cpc):
            out[key] = round(float(cpc), 2)
    return out


def load_locations(path: Path) -> pd.DataFrame:
    """Launch locations with Google criteria IDs (State / Criteria ID / Canonical Name)."""
    df = pd.read_csv(path)
    df.columns = [str(c).strip() for c in df.columns]
    return df


def load_campaign_rename(input_dir: Path, msgs) -> dict:
    """Start from the hardcoded CAMPAIGN_RENAME; if usia_campaign_rename.csv exists in the
    input dir (columns: canonical,new_name), let it override per key. Warns on any canonical
    key that isn't one of the four real campaigns (typo guard)."""
    mapping = dict(CAMPAIGN_RENAME)
    fp = input_dir / "usia_campaign_rename.csv"
    if fp.exists():
        with open(fp, encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                key = (row.get("canonical") or "").strip()
                val = (row.get("new_name") or "").strip()
                if key and val:
                    mapping[key] = val
        msgs.append(("OK", f"Loaded campaign rename overrides from {fp.name}."))
    unknown = sorted(k for k in mapping if k not in EXPECTED_CAMPAIGNS)
    if unknown:
        msgs.append(("WARN", f"CAMPAIGN_RENAME has keys that aren't real campaigns "
                             f"(won't apply): {unknown}. Valid: {sorted(EXPECTED_CAMPAIGNS)}."))
    return mapping


def load_foreign_countries(input_dir: Path, msgs) -> list:
    """All non-US countries (id, name) from a Google geo-targets table in the input dir
    (glob 'geotargets*.csv'). Used to negate every foreign country in one locations upload.
    Returns [] (with a WARN) if no geotargets file is present, so the build still runs."""
    matches = sorted(input_dir.glob("geotargets*.csv"))
    if not matches:
        msgs.append(("WARN", "No geotargets*.csv in input dir; skipped foreign-country "
                             "exclusions. Drop Google's geo-targets CSV in the input folder "
                             "to enable, or pass --no-exclude-foreign."))
        return []
    gt = matches[-1]
    out = []
    with open(gt, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            if row.get("Target Type") == "Country" and row.get("Country Code") != "US":
                out.append((int(row["Criteria ID"]), str(row["Name"]).strip()))
    out.sort(key=lambda t: t[1])
    return out


def merge_and_drop(keywords: pd.DataFrame) -> pd.DataFrame:
    """Absorb merged campaigns (Affinity/Aggregator -> Carrier) and drop excluded ad groups."""
    df = keywords.copy()
    df["campaign"] = df["campaign"].map(lambda c: CAMPAIGN_MERGE.get(c, c))
    mask_drop = df.apply(lambda r: (r["campaign"], norm(r["ad_group"])) in DROP_AD_GROUPS, axis=1)
    return df[~mask_drop].reset_index(drop=True)


def adgroup_cpc_with_neighbors(keywords: pd.DataFrame, adgroup_cpc: dict, default_cpc: str) -> dict:
    """Resolve a Max CPC for every (campaign, ad group): explicit value first, else the
    'neighboring' default = the most common Max CPC among other ad groups in the same
    campaign, else --default-max-cpc. Returns {(campaign, norm ad group): cpc}."""
    from collections import Counter
    by_campaign = {}
    for (camp, _ag), cpc in adgroup_cpc.items():
        by_campaign.setdefault(camp, []).append(cpc)
    campaign_default = {c: Counter(v).most_common(1)[0][0] for c, v in by_campaign.items()}

    resolved = {}
    for camp, ag in set(zip(keywords["campaign"], keywords["ad_group"])):
        key = (camp, norm(ag))
        if key in adgroup_cpc:
            resolved[key] = adgroup_cpc[key]
        elif camp in campaign_default:
            resolved[key] = campaign_default[camp]
        elif default_cpc:
            resolved[key] = default_cpc
    return resolved


def resolve_versioned(input_dir: Path, stem: str, ext: str = ".xlsx") -> Path:
    """Return the highest-version file matching <stem>_v<N><ext> (e.g. usia_home_keywords_v3.xlsx).
    Falls back to an unversioned <stem><ext> if no versioned file exists."""
    candidates = sorted(input_dir.glob(f"{stem}_v*{ext}"))
    best, best_n = None, -1
    for c in candidates:
        m = re.search(rf"{re.escape(stem)}_v(\d+){re.escape(ext)}$", c.name)
        if m and int(m.group(1)) > best_n:
            best, best_n = c, int(m.group(1))
    if best:
        return best
    plain = input_dir / f"{stem}{ext}"
    return plain


def load_negatives(input_dir: Path) -> dict:
    """list_name -> DataFrame(keyword, match_type, ui_paste_format)."""
    out = {}
    for name in NEG_LISTS:
        fp = input_dir / f"{name}.csv"
        if not fp.exists():
            raise FileNotFoundError(f"Missing negative list: {fp}")
        df = pd.read_csv(fp)
        df.columns = [str(c).strip() for c in df.columns]
        out[name] = df
    return out


# =============================================================================
# Validation
# =============================================================================
def validate(keywords: pd.DataFrame, negatives: dict, adgroup_cpc_resolved: dict,
             locations: pd.DataFrame) -> list:
    """Return a list of (level, message). level in {OK, WARN, FAIL}."""
    msgs = []
    adgroup_cpc_resolved_keys = set((c, norm(a)) for c, a in
                                    zip(keywords["campaign"], keywords["ad_group"]))

    def ok(m):   msgs.append(("OK", m))
    def warn(m): msgs.append(("WARN", m))
    def fail(m): msgs.append(("FAIL", m))

    # 1. match types valid
    bad_mt = sorted(set(keywords["match_type"]) - VALID_MATCH)
    if bad_mt:
        fail(f"Unexpected match types: {bad_mt}")
    else:
        ok("All positive-keyword match types are exact/phrase/broad.")

    # 2. companions are phrase
    comp = keywords[keywords["role"].str.startswith("companion")]
    bad_comp = comp[comp["match_type"] != "phrase"]
    if len(bad_comp):
        fail(f"{len(bad_comp)} companion row(s) are not phrase match.")
    else:
        ok(f"All {len(comp)} companion rows are phrase (as specified).")

    # 3. no residual indent glyph in any keyword
    dirty = keywords[keywords["keyword"].str.contains("\u21b3", regex=False)]
    if len(dirty):
        fail(f"{len(dirty)} keyword(s) still contain the companion-indent glyph.")
    else:
        ok("No residual companion-indent glyphs in keyword text.")

    # 4. empty keywords
    empt = keywords[keywords["keyword"].str.len() == 0]
    if len(empt):
        fail(f"{len(empt)} empty keyword row(s) after cleaning.")

    # 5. campaign set matches expectation
    seen = set(keywords["campaign"].unique())
    extra = seen - EXPECTED_CAMPAIGNS
    missing = EXPECTED_CAMPAIGNS - seen
    if extra:
        warn(f"Campaigns present but not in expected set: {sorted(extra)}")
    if missing:
        warn(f"Expected campaigns with no keywords: {sorted(missing)}")
    if not extra and not missing:
        ok(f"All {len(EXPECTED_CAMPAIGNS)} expected campaigns present: {sorted(seen)}")

    # 6. additions landed in real ad groups
    adds = keywords[keywords["source"] == "additions"]
    homeowner_targets = set(zip(adds["campaign"], adds["ad_group"]))
    real_groups = set(zip(keywords[keywords["source"] != "additions"]["campaign"],
                          keywords[keywords["source"] != "additions"]["ad_group"]))
    orphan_adds = homeowner_targets - real_groups
    if orphan_adds:
        fail(f"Homeowners-additions targeting ad groups that don't exist: {sorted(orphan_adds)}")
    else:
        ok(f"All {len(adds)} homeowners additions land in existing ad groups (AAA->AFFINITY, rest->CARRIER).")

    # 7. duplicate (campaign, ad_group, keyword, match_type)
    dup = keywords.duplicated(subset=["campaign", "ad_group", "keyword", "match_type"], keep=False)
    if dup.any():
        warn(f"{dup.sum()} exact duplicate keyword rows (same campaign/ad group/text/match).")
    else:
        ok("No exact duplicate keyword rows.")

    # 8. collision checks (the ones that silently break a live build)
    dnb = set(norm(k) for k in negatives["NEG_BRAND_DNB"]["keyword"])
    bid_brands = set(norm(a) for a in
                     keywords[keywords["campaign"].isin(CONQUEST_CAMPAIGNS | BARE_CAMPAIGNS)]["ad_group"])
    dnb_collide = sorted(b for b in bid_brands for n in dnb if n == b or f" {n} " in f" {b} ")
    if dnb_collide:
        fail(f"NEG_BRAND_DNB collides with brand(s) we bid: {dnb_collide}")
    else:
        ok("NEG_BRAND_DNB does not collide with any conquest/bare-carrier brand.")

    buy = set(norm(k) for k in negatives["NEG_BRAND_BUY"]["keyword"])
    gen_pos = [norm(k) for k in keywords[keywords["campaign"].isin(GENERIC_CAMPAIGNS)]["keyword"]]
    buy_collide = [(k, n) for k in gen_pos for n in buy if n == k or f" {n} " in f" {k} "]
    if buy_collide:
        fail(f"NEG_BRAND_BUY would block {len(buy_collide)} Generic positive keyword(s).")
    else:
        ok("NEG_BRAND_BUY blocks no Generic positive keyword.")

    # 9. negative-list reference counts
    for name, exp in NEG_EXPECTED_COUNTS.items():
        got = len(negatives[name])
        (ok if got == exp else warn)(f"{name}: {got} rows (handoff reference {exp}).")

    # 10. HOME_ROUTING must be broad
    hr_mt = set(negatives["NEG_HOME_ROUTING"]["match_type"].astype(str).str.lower())
    if hr_mt == {"broad"}:
        ok("NEG_HOME_ROUTING is all broad match.")
    else:
        warn(f"NEG_HOME_ROUTING match types = {hr_mt} (expected only broad).")

    # 11. every ad group resolves to a Max CPC (explicit, neighbor default, or --default-max-cpc)
    no_cpc = sorted(g for g in adgroup_cpc_resolved_keys if g not in adgroup_cpc_resolved)
    if no_cpc:
        warn(f"{len(no_cpc)} ad group(s) could not resolve a Max CPC: {no_cpc}")
    else:
        ok(f"All {len(adgroup_cpc_resolved_keys)} ad groups resolve to a Max CPC.")

    # 11b. merged campaigns absorbed; dropped ad groups gone
    leftover_merge = sorted(set(keywords["campaign"]) & set(CAMPAIGN_MERGE))
    if leftover_merge:
        fail(f"Merged campaigns still present (should be absorbed): {leftover_merge}")
    else:
        ok("Affinity/Aggregator absorbed into USIA_HOME_BRAND_CARRIER.")
    dropped_present = [(c, a) for c, a in set(zip(keywords["campaign"], keywords["ad_group"]))
                       if (c, norm(a)) in DROP_AD_GROUPS]
    if dropped_present:
        fail(f"Dropped ad group(s) still present: {dropped_present}")
    else:
        ok(f"Dropped ad groups removed: {sorted(c+'/'+a for c, a in DROP_AD_GROUPS)}.")

    # 12. launch locations sane (12 states with criteria IDs)
    if "Criteria ID" in locations.columns and locations["Criteria ID"].notna().all():
        ok(f"{len(locations)} launch locations with criteria IDs "
           f"({', '.join(locations['State'].astype(str))}).")
    else:
        fail("usia_launch_locations.csv is missing criteria IDs.")

    # 13. every expected campaign has a budget
    no_budget = sorted(c for c in keywords["campaign"].unique() if c not in CAMPAIGN_BUDGETS)
    if no_budget:
        warn(f"Campaign(s) with no configured budget (01_campaigns.csv Budget blank): {no_budget}")
    else:
        ok("All campaigns have a configured daily budget.")

    return msgs


# =============================================================================
# Writers
# =============================================================================
def write_campaigns(out_dir: Path, campaigns: list, start_date: str) -> Path:
    fp = out_dir / "01_campaigns.csv"
    rows = [{
        "Campaign": disp(c),
        "Campaign Type": CAMPAIGN_TYPE,
        "Status": STATUS,                       # campaign is the launch gate
        "Bid Strategy Type": BID_STRATEGY,      # Manual CPC at import; portfolios attached in UI
        "Budget": CAMPAIGN_BUDGETS.get(c, ""),
        "Start Date": start_date,
        "EU Political Ads": EU_POLITICAL_ADS,   # required self-declaration
    } for c in campaigns]
    pd.DataFrame(rows).to_csv(fp, index=False)
    return fp


def write_ad_groups(out_dir: Path, keywords: pd.DataFrame, adgroup_cpc_resolved: dict,
                    default_cpc: str) -> Path:
    fp = out_dir / "02_ad_groups.csv"
    rows = []
    for (camp, ag), _ in keywords.groupby(["campaign", "ad_group"]):
        max_cpc = adgroup_cpc_resolved.get((camp, norm(ag)), default_cpc)
        rows.append({"Campaign": disp(camp), "Ad Group": ag, "Status": "Enabled", "Max CPC": max_cpc})
    df = pd.DataFrame(rows).sort_values(["Campaign", "Ad Group"])
    df.to_csv(fp, index=False)
    return fp


def write_locations(out_dir: Path, keywords: pd.DataFrame, locations: pd.DataFrame,
                    foreign: list = None) -> Path:
    """Editor location import (one combined upload): UTF-16 LE, tab-delimited, columns
    Campaign / ID / Location / Status / Type. Per campaign: the 12 states as Enabled adds
    (spaced display name '<State>, United States', bare numeric Criteria ID), the United States
    default (2840) as a Removed row, and — if `foreign` is given — every non-US country as an
    EXCLUDED row (Status Enabled, Type 'Negative'). Editor reads Type 'Negative' as an exclusion;
    blank Type = positive target (Google Ads Editor help answer/30841). IDs verified against
    geotargets-2026-06-15.csv.

    Apply via Locations -> Make multiple changes -> 'My data includes columns for campaigns'
    -> Paste from clipboard -> map Campaign / ID / Location / Status / Type -> Process -> Keep.
    New targets import unresolved (Type Unknown); resolve ONE via Edit/find location and it
    cascades to the rest. (Negatives land under 'Locations, Negatives' in the left nav.)"""
    foreign = foreign or []
    fp = out_dir / "07_campaign_locations.csv"
    headers = ["Campaign", "ID", "Location", "Status", "Type"]
    rows = []
    for camp in sorted(keywords["campaign"].unique()):
        for _, loc in locations.iterrows():
            rows.append({"Campaign": disp(camp), "ID": int(loc["Criteria ID"]),
                         "Location": f"{str(loc['State']).strip()}, United States",
                         "Status": "Enabled", "Type": ""})
        rows.append({"Campaign": disp(camp), "ID": US_GEO_ID,
                     "Location": "United States", "Status": "Removed", "Type": ""})
        for cid, name in foreign:
            rows.append({"Campaign": disp(camp), "ID": cid, "Location": name,
                         "Status": "Enabled", "Type": "Negative"})
    with open(fp, "w", encoding=LOC_ENCODING, newline="") as f:
        w = csv.DictWriter(f, fieldnames=headers, delimiter=LOC_DELIM)
        w.writeheader()
        w.writerows(rows)
    return fp


def write_keywords(out_dir: Path, keywords: pd.DataFrame) -> Path:
    fp = out_dir / "03_keywords.csv"
    df = keywords.copy()
    df = df.assign(
        **{
            "Campaign": df["campaign"].map(disp),
            "Ad Group": df["ad_group"],
            "Keyword": df["keyword"],
            "Match Type": df["match_type"].map(MATCH_DISPLAY),
            "Max CPC": "",          # inherits ad-group bid; per-keyword bids not specified
            "Status": "Enabled",    # keywords always active; campaign is the gate
        }
    )[["Campaign", "Ad Group", "Keyword", "Match Type", "Max CPC", "Status"]]
    df = df.sort_values(["Campaign", "Ad Group", "Match Type", "Keyword"])
    df.to_csv(fp, index=False)
    return fp


def write_rsa_shells(out_dir: Path, keywords: pd.DataFrame) -> Path:
    """One Responsive Search Ad shell per ad group, full Editor RSA schema, blank copy.
    NOT directly importable until the copy thread adds >=3 headlines, >=2 descriptions,
    and a Final URL — it's the fill-in template that lets copy drop in cleanly."""
    fp = out_dir / "06_rsa_shells.csv"
    cols = ["Campaign", "Ad Group", "Ad type", "Status", "Final URL", "Path 1", "Path 2"]
    cols += [f"Headline {i}" for i in range(1, RSA_HEADLINES + 1)]
    cols += [f"Description {i}" for i in range(1, RSA_DESCRIPTIONS + 1)]
    rows = []
    for (camp, ag), _ in keywords.groupby(["campaign", "ad_group"]):
        row = {c: "" for c in cols}
        row.update({"Campaign": disp(camp), "Ad Group": ag, "Ad type": RSA_AD_TYPE, "Status": "Paused"})
        rows.append(row)
    pd.DataFrame(rows, columns=cols).sort_values(["Campaign", "Ad Group"]).to_csv(fp, index=False)
    return fp


def write_negative_lists(out_dir: Path, negatives: dict) -> list:
    """Paste-ready: one term per line, in match-type syntax (bare = broad, "quoted" = phrase),
    no header, no CSV escaping — select-all and paste straight into the Shared Library list."""
    nl_dir = out_dir / "negative_lists"
    nl_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for name, df in negatives.items():
        fp = nl_dir / f"{name}.txt"
        lines = [str(v) for v in df["ui_paste_format"].tolist()]
        fp.write_text("\n".join(lines) + "\n", encoding="utf-8")
        paths.append(fp)
    return paths


def write_apply_matrix(out_dir: Path) -> Path:
    fp = out_dir / "04_negative_apply_matrix.csv"
    rows = []
    groups = [("Generic", GENERIC_CAMPAIGNS, 0),
              ("Conquest", CONQUEST_CAMPAIGNS, 1),
              ("Bare-carrier", BARE_CAMPAIGNS, 2)]
    for name, flags in ROUTING_MATRIX.items():
        for _, camps, idx in groups:
            if flags[idx]:
                for camp in sorted(camps):
                    rows.append({"Negative List": name, "Campaign": disp(camp), "Apply": "Yes"})
    pd.DataFrame(rows).sort_values(["Negative List", "Campaign"]).to_csv(fp, index=False)
    return fp


def write_negative_apply_script(out_dir: Path, keywords: pd.DataFrame) -> Path:
    """Emit a Google Ads Script that attaches the EXISTING shared negative keyword lists to
    their campaigns per the routing matrix. This is the automated alternative to clicking
    'Apply to campaigns' in the UI (Editor cannot import the shared-set <-> campaign link)."""
    fp = out_dir / "08_apply_negative_lists.js"
    present = set(keywords["campaign"].unique())
    groups = [(GENERIC_CAMPAIGNS, 0), (CONQUEST_CAMPAIGNS, 1), (BARE_CAMPAIGNS, 2)]
    mapping = {}  # list_name -> [campaigns]
    for name, flags in ROUTING_MATRIX.items():
        camps = []
        for cset, idx in groups:
            if flags[idx]:
                camps += sorted(cset & present)
        mapping[name] = sorted(set(camps))

    js_map = ",\n".join(
        f'  "{name}": [{", ".join(chr(34)+disp(c)+chr(34) for c in camps)}]'
        for name, camps in mapping.items())

    script = f"""// USIA Home — apply shared negative keyword lists to campaigns.
// Generated by google_generate_usia_build.py. Run in Google Ads:
//   Tools & Settings -> Bulk actions -> Scripts -> + -> paste -> Authorize -> Preview -> Run.
// Idempotent: re-running only adds missing associations.
// Requires the 8 shared lists to already exist (they do — built in Editor).

var MATRIX = {{
{js_map}
}};

function main() {{
  var applied = 0, skipped = 0, missing = [];
  for (var listName in MATRIX) {{
    var lists = AdsApp.negativeKeywordLists()
        .withCondition('shared_set.name = "' + listName + '"').get();
    if (!lists.hasNext()) {{ missing.push(listName); continue; }}
    var list = lists.next();
    var campaigns = MATRIX[listName];
    for (var i = 0; i < campaigns.length; i++) {{
      var it = AdsApp.campaigns().withCondition('campaign.name = "' + campaigns[i] + '"').get();
      if (!it.hasNext()) {{ Logger.log('WARN missing campaign: ' + campaigns[i]); continue; }}
      var camp = it.next();
      var already = false;
      var existing = camp.negativeKeywordLists().get();
      while (existing.hasNext()) {{
        if (existing.next().getName() === listName) {{ already = true; break; }}
      }}
      if (already) {{ skipped++; continue; }}
      camp.addNegativeKeywordList(list);
      applied++;
      Logger.log('applied ' + listName + ' -> ' + campaigns[i]);
    }}
  }}
  Logger.log('DONE. applied=' + applied + ' already=' + skipped +
             (missing.length ? ' MISSING LISTS=' + missing.join(',') : ''));
}}
"""
    fp.write_text(script, encoding="utf-8")
    return fp


def write_negatives_campaign_level(out_dir: Path, keywords: pd.DataFrame, negatives: dict) -> Path:
    """Editor-importable campaign-level negative keywords. Ad Group blank = campaign-level;
    Criterion Type = 'Negative Phrase' / 'Negative Broad' (the format Editor accepts on import).
    Expanded over the routing matrix against the campaigns actually present in the build."""
    fp = out_dir / "05_negatives_campaign_level.csv"
    present = set(keywords["campaign"].unique())
    groups = [(GENERIC_CAMPAIGNS, 0), (CONQUEST_CAMPAIGNS, 1), (BARE_CAMPAIGNS, 2)]
    crit = {"phrase": "Negative Phrase", "broad": "Negative Broad", "exact": "Negative Exact"}
    rows = []
    for name, flags in ROUTING_MATRIX.items():
        df = negatives[name]
        for camps, idx in groups:
            if not flags[idx]:
                continue
            for camp in sorted(camps & present):
                for _, r in df.iterrows():
                    rows.append({
                        "Campaign": disp(camp),
                        "Ad Group": "",   # blank => campaign-level negative
                        "Keyword": str(r["keyword"]),
                        "Criterion Type": crit.get(str(r["match_type"]).lower(), "Negative Phrase"),
                    })
    pd.DataFrame(rows).to_csv(fp, index=False)
    return fp


def write_checklist(out_dir: Path, keywords: pd.DataFrame, locations: pd.DataFrame,
                    start_date: str) -> Path:
    fp = out_dir / "SETTINGS_CHECKLIST.md"
    n_campaigns = keywords["campaign"].nunique()
    n_groups = keywords.groupby(["campaign", "ad_group"]).ngroups
    n_kw = len(keywords)

    budget_rows = "\n".join(
        f"  - `{disp(c)}`: ${CAMPAIGN_BUDGETS.get(c, '—')}/day" for c in sorted(keywords["campaign"].unique()))
    portfolio_rows = "\n".join(
        f"  - **{name}** (Maximize Clicks, max-CPC cap ${cfg['cap']}): "
        f"{', '.join(disp(c) for c in cfg['campaigns'])}"
        for name, cfg in BID_PORTFOLIOS.items())
    state_list = ", ".join(locations["State"].astype(str))

    text = f"""# USIA Home — Google Ads Editor build: settings checklist

Generated {datetime.now():%Y-%m-%d %H:%M}. Build status for all entities: **{STATUS}**.

> **HOLD until greenlit.** Per USIA_Launch_Settings.md, stage this build but do not
> push it live until the launch is greenlit. Start date is set to **{start_date}**.

The CSVs handle campaigns, ad groups, keywords, and locations. The items below are
the settings Google Ads Editor does **not** reliably set via CSV import — apply them
by hand (or in the UI) on the created shells.

## Import order (Editor → File → Import, or paste in "Make multiple changes")
1. `01_campaigns.csv` — {n_campaigns} campaign shells (type, status, Manual CPC, budget, start date, **EU Political Ads = No**).
2. `02_ad_groups.csv` — {n_groups} ad-group shells (Status Enabled) with Max CPC.
3. `03_keywords.csv` — {n_kw} positive keywords, Status Enabled (confirm the column-mapping dialog).
4. `07_campaign_locations.csv` — apply via **Locations → Make multiple changes → paste** (map
   Campaign / ID / Location / Status). One combined upload: 12 state adds + US (2840) removal.
   Resolve one location via Edit/find location; it cascades to all. UTF-16 LE / tab; keep encoding.
5. `05_negatives_campaign_level.csv` — only if you choose the campaign-level negatives fallback (see below).
6. `06_rsa_shells.csv` — one blank Responsive Search Ad per ad group (see "RSA shells").

Status: campaigns follow the build status (**{STATUS}**) as the launch gate; **ad groups and
keywords are always Enabled** so flipping a campaign live activates everything under it. RSA
shells are Paused (blank placeholders).

## EU political ads (required)
Every campaign must self-declare. `01_campaigns.csv` carries `EU Political Ads = No`. If Editor
doesn't map that column on your version, set it in the UI: Campaigns → select all → Edit →
**Confirm EU Setting** → "No, these campaigns don't have EU political ads."

## Bidding — Manual CPC now, portfolios in the UI after
Import every campaign on **Manual CPC** (already set in 01_campaigns.csv) with the
ad-group Max CPCs from `02_ad_groups.csv` — this clears Editor's missing-bid warning
and lets the account run if it goes live before the portfolios are attached. Then,
**in the Google Ads UI** (Shared library → Bid strategies), build two **portfolio
Maximize Clicks** strategies and attach them:
{portfolio_rows}
  Caps are ~10% under breakeven for a new-account Quality-Score buffer. Conquest is
  isolated on its own portfolio + budget; bare-carrier rides the Generic portfolio.

## Daily budgets (opening settings — calibrate week 1 from search_budget_lost_is)
01_campaigns.csv carries these; confirm on import:
{budget_rows}
  The bare-carrier campaign (`USIA_HOME_BRAND_GENERIC`) must stay isolated and
  separately measurable — it coexists with the live Quote.com home account on these
  terms (watch for self-competition / CPC inflation).

## Locations
- `07_campaign_locations.csv` (UTF-16 LE / tab) is one combined upload with columns
  Campaign / ID / Location / Status / **Type**. Per campaign: the {len(locations)} states (Enabled,
  Type blank), the United States default removal (2840, Removed, Type blank), and **every non-US
  country as an exclusion** (Enabled, **Type Negative**). Spaced state display name
  (`Ohio, United States`), bare numeric Criteria IDs verified against Google's geotargets table.
- **Apply via** Locations → Make multiple changes → "My data includes columns for campaigns" →
  Paste from clipboard → map Campaign / ID / Location / Status / **Type** → Process → Keep.
  Negatives appear under **Locations, Negatives** in the left nav.
- New targets import **unresolved** (Type Unknown, yellow). Resolve **one** location via
  Edit/find location and it cascades to all — states → State type, negatives confirm.
- **Reality check on the foreign exclusions:** with {len(locations)}-state inclusive targeting and
  **Presence** targeting, ads can't serve outside those states regardless — Google won't show ads
  in places you haven't targeted. So the country exclusions are a **defensive safety net** (they'd
  only ever matter if targeting were switched to "presence OR interest," or a campaign were set to
  "all countries"), not a behavior change. Google's guidance is that exclusions shouldn't exceed
  your targeted-location count; the 122-country-exclusion cap is specific to "all countries"
  positive targeting, which doesn't apply here. Build with `--no-exclude-foreign` to omit them.
- Targeting method = **{LOCATION_MODE}**. Set this per campaign in the location settings.
- Launch states ({len(locations)}): {state_list}.
- **Phase 2 — do NOT enable at launch:** {', '.join(PHASE2_STATES)}.
- State revenue differences: handle now with **per-state bid adjustments** (don't split
  campaigns); Phase 2 moves to tROAS-with-RPL-values inside the pooled campaigns.

## RSA shells (hand-off to the copy thread)
`06_rsa_shells.csv` has one RSA row per ad group with the full Editor schema
(Headline 1–15, Description 1–4, Path 1/2, Final URL), blank everywhere copy goes.
It is a **fill-in template**, not directly importable: Editor rejects an RSA with
fewer than **3 headlines, 2 descriptions, and a Final URL**. The copy thread fills
the blanks (from `USIA_AdGroup_Intent_Map.md`) and adds the Final URL, then imports.

## Negative keywords — the shared list <-> campaign link is NOT importable
Your 8 shared lists exist with the right keyword counts but sit at **Campaigns = 0**. There is
**no Editor CSV/import that attaches a shared list to a campaign** — confirmed across Google's
docs. That link (CampaignSharedSet) is created only by the web-UI "Apply to campaigns" action or
the Scripts/API. So the matrix CSV was never importable; use one of these:

**Route A — run `08_apply_negative_lists.js` (automated, recommended):** Google Ads →
Tools & Settings → Bulk actions → Scripts → + → paste → Authorize → Preview → Run. It calls
`campaign.addNegativeKeywordList()` to attach each existing list to its campaigns per the matrix,
and is idempotent. Run it in the account where the USIA campaigns + these lists live.

**Route B — web UI bulk apply:** Tools → Shared library → Negative keyword lists → tick a list →
Apply to campaigns → check its campaigns → Apply. Repeat per list using `04_negative_apply_matrix.csv`
as the which-list-where checklist (8 lists, a few campaigns each).

`05_negatives_campaign_level.csv` (campaign-level negatives) is only a last resort if you abandon
shared lists; not needed with Route A or B.

Routing reminders (do **not** "fix" the gaps — they're deliberate):
- NEG_AUTO_VEHICLE → Bare-carrier only (Generic/Conquest hold home+auto bundle terms).
- NEG_BRAND_BUY → Generic only (routes our brand queries to the Conquest campaign).
- NEG_HOME_ROUTING → Bare-carrier only, broad (routes home-qualified brand queries to Conquest).
- NEG_BRAND_DNB → every campaign (universal restricted-brand block).

## Other campaign settings
- **Network:** Google Search only — **exclude** Search Partners and Display.
- **AI Max:** **OFF** at launch. When later enabled, regenerate the Bare-carrier
  campaign with `--ai-max` (exact-only, drops the redundant phrase twins).
- **Call-first assets:** enable Call Assets + call reporting on every campaign;
  "speak to an agent" is the differentiator. Count qualified calls as **primary** conversions.

## Phase 2 (post-launch — do not build now)
At ~30 conversions/campaign, pivot both portfolios from Maximize Clicks to **tROAS**,
feeding **RPL as conversion value** (parallel portfolios `USIA_tROAS_Conquest` /
`USIA_tROAS_Generic`), so Smart Bidding handles state-level economics inside the
pooled campaigns — no geo-split campaigns.
"""
    fp.write_text(text, encoding="utf-8")
    return fp


def write_summary(out_dir: Path, keywords: pd.DataFrame, negatives: dict,
                  msgs: list, args) -> Path:
    fp = out_dir / "build_summary.txt"
    lines = []
    lines.append("=" * 78)
    lines.append(f"USIA Home — Google Ads Editor build summary   ({datetime.now():%Y-%m-%d %H:%M})")
    lines.append("=" * 78)
    lines.append(f"input_dir : {args.input_dir}")
    lines.append(f"output_dir: {out_dir}")
    lines.append(f"status    : {STATUS}    ai_max: {args.ai_max}    default_max_cpc: {args.default_max_cpc or '(none)'}")
    lines.append("")

    lines.append("POSITIVE KEYWORDS")
    lines.append("-" * 78)
    for camp in sorted(keywords["campaign"].unique()):
        g = keywords[keywords["campaign"] == camp]
        mt = g["match_type"].value_counts().to_dict()
        lines.append(f"  {camp:28s} ad_groups={g['ad_group'].nunique():2d}  rows={len(g):4d}  {mt}")
    lines.append(f"  {'TOTAL':28s} ad_groups={keywords.groupby(['campaign','ad_group']).ngroups:2d}"
                 f"  rows={len(keywords):4d}")
    lines.append("")

    lines.append("AD GROUPS")
    lines.append("-" * 78)
    for camp in sorted(keywords["campaign"].unique()):
        ags = sorted(keywords[keywords["campaign"] == camp]["ad_group"].unique())
        lines.append(f"  {camp}: {', '.join(ags)}")
    lines.append("")

    lines.append("NEGATIVE LISTS (shared)")
    lines.append("-" * 78)
    for name in NEG_LISTS:
        df = negatives[name]
        mt = df["match_type"].value_counts().to_dict()
        applied = [grp for grp, flag in
                   zip(["Generic", "Conquest", "Bare"], ROUTING_MATRIX[name]) if flag]
        lines.append(f"  {name:24s} terms={len(df):4d}  {mt}  -> {', '.join(applied)}")
    lines.append("")

    lines.append("VALIDATION")
    lines.append("-" * 78)
    for level, m in msgs:
        lines.append(f"  [{level:4s}] {m}")
    n_fail = sum(1 for lvl, _ in msgs if lvl == "FAIL")
    n_warn = sum(1 for lvl, _ in msgs if lvl == "WARN")
    lines.append("")
    lines.append(f"RESULT: {n_fail} FAIL, {n_warn} WARN, {sum(1 for l,_ in msgs if l=='OK')} OK")
    lines.append("=" * 78)

    text = "\n".join(lines)
    fp.write_text(text, encoding="utf-8")
    return fp, text


# =============================================================================
# Master combined import (one file to share with the team)
# =============================================================================
MASTER_COLS = ["Campaign", "Campaign Type", "Status", "Bid Strategy Type", "Budget",
               "Start Date", "EU Political Ads", "Ad Group", "Max CPC", "Keyword", "Match Type"]


def write_master_import_csv(out_dir: Path) -> Path:
    """Combine campaigns + ad groups + keywords into ONE Editor-importable CSV (Account ->
    Import -> From file). Each row type fills only its columns; Editor infers the type. Does
    NOT include locations (different import path) or negative shared-list links (not importable)
    or the blank RSA shells (not importable until filled) — those stay in their own sheets."""
    camp = pd.read_csv(out_dir / "01_campaigns.csv", dtype=str).fillna("")
    ag = pd.read_csv(out_dir / "02_ad_groups.csv", dtype=str).fillna("")
    kw = pd.read_csv(out_dir / "03_keywords.csv", dtype=str).fillna("")
    master = pd.concat([f.reindex(columns=MASTER_COLS, fill_value="") for f in (camp, ag, kw)],
                       ignore_index=True)
    fp = out_dir / "00_master_import.csv"
    master.to_csv(fp, index=False)
    return fp


def _read_rows(fp: Path):
    """Read any generated CSV (utf-8 comma or utf-16-le tab) into a list of row lists."""
    if fp.suffix.lower() != ".csv":
        return None
    try:
        with open(fp, encoding="utf-8-sig", newline="") as f:
            sample = f.read(2048)
        if "\x00" in sample:
            raise UnicodeError
        delim = "\t" if sample.count("\t") > sample.count(",") else ","
        with open(fp, encoding="utf-8-sig", newline="") as f:
            return list(csv.reader(f, delimiter=delim))
    except UnicodeError:
        with open(fp, encoding="utf-16-le", newline="") as f:
            return list(csv.reader(f, delimiter="\t"))


def write_master_workbook(out_dir: Path, msgs) -> Path:
    """One workbook to share with the team. Each component is its own labeled tab, with a
    READ_ME tab explaining the import order and which Editor path each tab uses. Editor can't
    import an .xlsx directly, so each tab is copy-paste-ready (or save-as-CSV) for its path."""
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill, Alignment
        from openpyxl.utils import get_column_letter
    except ImportError:
        msgs.append(("WARN", "openpyxl not installed; skipped USIA_master_build.xlsx "
                             "(pip install openpyxl to enable)."))
        return None

    HEAD = Font(name="Arial", bold=True, color="FFFFFF")
    HEAD_FILL = PatternFill("solid", fgColor="1F4E78")
    BODY = Font(name="Arial")
    WRAP = Alignment(wrap_text=True, vertical="top")

    wb = Workbook()
    wb.remove(wb.active)

    def add_table(title, rows, widths=None):
        ws = wb.create_sheet(title[:31])
        if not rows:
            return ws
        for r in rows:
            ws.append(r)
        for c in range(1, len(rows[0]) + 1):
            cell = ws.cell(row=1, column=c)
            cell.font = HEAD
            cell.fill = HEAD_FILL
        for row in ws.iter_rows(min_row=2):
            for cell in row:
                cell.font = BODY
        ws.freeze_panes = "A2"
        for c in range(1, len(rows[0]) + 1):
            letter = get_column_letter(c)
            if widths and c <= len(widths):
                ws.column_dimensions[letter].width = widths[c - 1]
            else:
                maxlen = max((len(str(r[c - 1])) for r in rows if c - 1 < len(r)), default=10)
                ws.column_dimensions[letter].width = min(max(maxlen + 2, 10), 60)
        return ws

    def add_text(title, text):
        ws = wb.create_sheet(title[:31])
        for line in text.splitlines():
            ws.append([line])
        ws.column_dimensions["A"].width = 110
        for row in ws.iter_rows():
            for cell in row:
                cell.font = BODY
                cell.alignment = WRAP
        return ws

    readme = (
        "USIA HOME — Google Ads Editor build (master workbook)\n"
        "Generated by google_generate_usia_build.py\n"
        "\n"
        "Editor cannot import an .xlsx directly. Each tab below is copy-paste-ready for its\n"
        "specific Editor path. Suggested order:\n"
        "\n"
        "1. Editor_Import  — campaigns + ad groups + keywords in one sheet.\n"
        "     Save this tab as CSV (or copy all rows) -> Editor: Account > Import > From file\n"
        "     (or Paste text) > review column mapping > Process > Keep.\n"
        "     Campaigns import on Manual CPC; attach the Maximize-Clicks portfolios in the UI after.\n"
        "\n"
        "2. Locations  — one upload, Campaign / ID / Location / Status / Type:\n"
        "     - 12 state ADDS (Status Enabled, Type blank)\n"
        "     - United States (2840) removal (Status Removed, Type blank)\n"
        "     - every non-US country as an EXCLUSION (Status Enabled, Type Negative)\n"
        "     Editor left nav: Keywords and Targeting > Locations > Make multiple changes >\n"
        "     'My data includes columns for campaigns' > Paste from clipboard > map columns\n"
        "     (incl. Type) > Process > Keep. Resolve ONE location via Edit/find location and it\n"
        "     cascades; targeted states -> Type State, negatives land under 'Locations, Negatives'.\n"
        "     NOTE: with 12-state + Presence targeting, ads can't serve abroad anyway, so the\n"
        "     country exclusions are a defensive safety net (guards a future 'presence OR interest'\n"
        "     switch), not a behavior change. Build with --no-exclude-foreign to omit them.\n"
        "\n"
        "3. Negatives  — the 8 shared lists already exist; they just need attaching to campaigns.\n"
        "     There is NO Editor import for the shared-list<->campaign link. Use ONE of:\n"
        "       a) Apply_Negatives_Script tab -> Google Ads > Tools > Bulk actions > Scripts.\n"
        "       b) Web UI: Tools > Shared library > Negative keyword lists > tick a list >\n"
        "          Apply to campaigns, per the Negatives_Apply_Matrix tab.\n"
        "     Negatives_CampaignLevel is a fallback only (don't use with a) or b)).\n"
        "     Negatives_ListContents is the keyword content of each list, for reference/rebuild.\n"
        "\n"
        "4. RSA_Shells  — one blank Responsive Search Ad per ad group. NOT importable until the\n"
        "     copy thread fills >=3 headlines, >=2 descriptions, and a Final URL.\n"
        "\n"
        "5. Checklist  — campaign settings to confirm (status gate, budgets, conversions, etc.).\n"
    )
    add_text("READ_ME", readme)

    master_fp = out_dir / "00_master_import.csv"
    if master_fp.exists():
        add_table("Editor_Import", _read_rows(master_fp),
                  widths=[26, 13, 9, 16, 8, 12, 14, 22, 9, 40, 11])

    sheet_map = [
        ("Locations", "07_campaign_locations.csv", [26, 8, 26, 10]),
        ("Negatives_Apply_Matrix", "04_negative_apply_matrix.csv", None),
        ("Negatives_CampaignLevel", "05_negatives_campaign_level.csv", None),
        ("RSA_Shells", "06_rsa_shells.csv", None),
    ]
    for title, fname, widths in sheet_map:
        fp = out_dir / fname
        if fp.exists():
            add_table(title, _read_rows(fp), widths=widths)

    # Negative list contents (all NEG_*.txt -> one sheet: List / Keyword)
    neg_dir = out_dir / "negative_lists"
    if neg_dir.exists():
        rows = [["Negative List", "Keyword (as pasted: bare=broad, \"quoted\"=phrase)"]]
        for txt in sorted(neg_dir.glob("NEG_*.txt")):
            for line in txt.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    rows.append([txt.stem, line])
        add_table("Negatives_ListContents", rows, widths=[26, 60])

    # Ads Script as text
    js_fp = out_dir / "08_apply_negative_lists.js"
    if js_fp.exists():
        add_text("Apply_Negatives_Script", js_fp.read_text(encoding="utf-8"))

    # Checklist
    chk_fp = out_dir / "SETTINGS_CHECKLIST.md"
    if chk_fp.exists():
        add_text("Checklist", chk_fp.read_text(encoding="utf-8"))

    fp = out_dir / "USIA_master_build.xlsx"
    wb.save(fp)
    return fp


# =============================================================================
# Main
# =============================================================================
def parse_args(argv=None):
    here = Path(__file__).resolve().parent
    p = argparse.ArgumentParser(description="Generate the USIA Home Google Ads Editor build.")
    p.add_argument("--input-dir", type=Path, default=here / "input" / "usia",
                   help="Folder with the workbooks, additions CSV, and NEG_*.csv lists.")
    p.add_argument("--output-dir", type=Path, default=here / "output" / "usia",
                   help="Parent output folder; a timestamped (or --run-tag) subfolder is created.")
    p.add_argument("--run-tag", default=None, help="Output subfolder name (default: timestamp).")
    p.add_argument("--ai-max", action="store_true",
                   help="Bare-carrier exact-only: drop the redundant same-text phrase twins.")
    p.add_argument("--status", default="Enabled", choices=["Paused", "Enabled"],
                   help="Build status for all entities (default Enabled per launch settings; "
                        "use Paused for a staged dry-run).")
    p.add_argument("--start-date", default=START_DATE,
                   help=f"Campaign start date (default {START_DATE}).")
    p.add_argument("--default-max-cpc", default="",
                   help="Fallback Max CPC for any ad group missing from usia_adgroup_maxcpc.csv.")
    p.add_argument("--exclude-foreign", action="store_true", default=True,
                   help="Add every non-US country as a negative location (needs geotargets*.csv "
                        "in the input dir). On by default.")
    p.add_argument("--no-exclude-foreign", dest="exclude_foreign", action="store_false",
                   help="Skip the foreign-country exclusions.")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    global STATUS
    STATUS = args.status

    in_dir = args.input_dir

    # Preflight: verify every required input up front and report ALL missing at once.
    # Workbooks are version-resolved (usia_home_keywords_v3.xlsx beats _v2, etc.).
    wb_generic = resolve_versioned(in_dir, "usia_home_keywords")
    wb_conquest = resolve_versioned(in_dir, "usia_home_brand")
    wb_bare = in_dir / "usia_home_brand_generic.xlsx"
    additions = in_dir / "usia_conquest_homeowners_additions.csv"
    maxcpc_csv = in_dir / "usia_adgroup_maxcpc.csv"
    locations_csv = in_dir / "usia_launch_locations.csv"
    required = [wb_generic, wb_conquest, wb_bare, additions, maxcpc_csv, locations_csv] + \
               [in_dir / f"{name}.csv" for name in NEG_LISTS]
    n_required = len(required)

    if not in_dir.exists():
        sys.exit(f"[inputs] Input dir not found: {in_dir}\n"
                 f"         Create it and stage the {n_required} input files there "
                 f"(or pass --input-dir).")

    missing = [p for p in required if not p.exists()]
    if missing:
        present = [p.name for p in required if p.exists()]
        lines = [f"[inputs] {len(missing)} of {n_required} required files missing from {in_dir}:"]
        lines += [f"           - {p.name}" for p in missing]
        if present:
            lines.append(f"         Found: {', '.join(present)}")
        lines.append("         Stage all inputs (generic + conquest workbooks, bare-carrier workbook, "
                     "additions CSV, adgroup_maxcpc CSV, launch_locations CSV, 8 NEG_*.csv) and re-run.")
        print("\n".join(lines), file=sys.stderr)
        return 2

    print(f"[inputs] generic={wb_generic.name}  conquest={wb_conquest.name}  bare={wb_bare.name}")

    # Load positive keywords
    frames = [load_generic(wb_generic), load_conquest(wb_conquest),
              load_bare(wb_bare), load_additions(additions)]
    keywords = pd.concat(frames, ignore_index=True)

    # Force the Affinity/Aggregator merge and drop excluded ad groups (e.g. MetLife).
    keywords = merge_and_drop(keywords)

    # --ai-max: bare-carrier exact-only (drop phrase rows whose exact twin exists)
    if args.ai_max:
        bare = keywords[keywords["campaign"].isin(BARE_CAMPAIGNS)]
        exact_text = set(zip(bare[bare.match_type == "exact"]["ad_group"],
                             bare[bare.match_type == "exact"]["keyword"]))
        drop_mask = (keywords["campaign"].isin(BARE_CAMPAIGNS) &
                     (keywords["match_type"] == "phrase") &
                     keywords.apply(lambda r: (r["ad_group"], r["keyword"]) in exact_text, axis=1))
        keywords = keywords[~drop_mask].reset_index(drop=True)

    adgroup_cpc = load_adgroup_maxcpc(maxcpc_csv)
    adgroup_cpc_resolved = adgroup_cpc_with_neighbors(keywords, adgroup_cpc, args.default_max_cpc)
    locations = load_locations(locations_csv)
    negatives = load_negatives(in_dir)

    # Validate
    msgs = validate(keywords, negatives, adgroup_cpc_resolved, locations)
    global CAMPAIGN_RENAME
    CAMPAIGN_RENAME = load_campaign_rename(in_dir, msgs)
    foreign = load_foreign_countries(in_dir, msgs) if args.exclude_foreign else []

    # Output dir
    tag = args.run_tag or datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = args.output_dir / tag
    out_dir.mkdir(parents=True, exist_ok=True)

    # Write everything
    campaigns = sorted(keywords["campaign"].unique())
    write_campaigns(out_dir, campaigns, args.start_date)
    write_ad_groups(out_dir, keywords, adgroup_cpc_resolved, args.default_max_cpc)
    write_keywords(out_dir, keywords)
    write_rsa_shells(out_dir, keywords)
    write_locations(out_dir, keywords, locations, foreign)
    write_negative_lists(out_dir, negatives)
    write_apply_matrix(out_dir)
    write_negative_apply_script(out_dir, keywords)
    write_negatives_campaign_level(out_dir, keywords, negatives)
    write_checklist(out_dir, keywords, locations, args.start_date)
    write_master_import_csv(out_dir)
    write_master_workbook(out_dir, msgs)
    _, summary_text = write_summary(out_dir, keywords, negatives, msgs, args)

    print(summary_text)
    print(f"\nWrote build to: {out_dir}")
    n_fail = sum(1 for lvl, _ in msgs if lvl == "FAIL")
    return 1 if n_fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
