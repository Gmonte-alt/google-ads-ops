"""
Confie Negative Keyword Analyzer
================================
Analyzes Google Ads / Microsoft Ads search query reports (SQRs) and recommends
negative keywords with appropriate match types for Confie subsidiary brand terms.

Usage:
    python confie_negkw.py \
        --sqr "Confie-google-microsoft-L30D.xlsx" \
        --config confie_brands.yaml \
        --output confie_negative_keywords.xlsx \
        --sheet "SQR-master"

Outputs an xlsx with the following tabs:
    Summary           Counts and totals per category
    Negative_Exact    Recommended exact-match negatives
    Negative_Phrase   Recommended phrase-match negatives
    Negative_Broad    Recommended broad-match negatives (only for broad_safe brands)
    Mixed_Queries     Queries containing both Confie + competitor brand (manual review)
    Needs_Review      Ambiguous matches: short tokens, missing context, weak signals
    All_Matches       Every classified query with full attribution
    No_Match          Queries that didn't match any brand (sanity check)

Column-name handling:
    The script auto-detects standard Google Ads, Microsoft Ads, and Confie-style
    column headers. If your SQR uses non-standard names, edit COLUMN_ALIASES below.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import pandas as pd
import yaml
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter


# ---------------------------------------------------------------------------
# Column-name resolution
# ---------------------------------------------------------------------------
# Maps logical field -> list of accepted SQR header names (case-insensitive).
# Add your column names here if auto-detection misses them.
COLUMN_ALIASES = {
    "search_term": [
        "search term", "search query", "query", "user search term",
        "customer search term", "search keyword", "search_term",
    ],
    "impressions": ["impressions", "impr.", "impr", "impressions."],
    "clicks": ["clicks"],
    "cost": ["cost", "spend", "cost (usd)", "cost (account currency)", "amount spent"],
    "conversions": ["conversions", "conv.", "conv"],
    "conv_value": ["conv. value", "conversion value", "all conv. value", "total conv. value"],
    "campaign": ["campaign", "campaign name"],
    "ad_group": ["ad group", "ad group name", "adgroup", "ad_group"],
    "match_type": ["match type", "keyword match type", "search keyword match type"],
    "keyword": ["keyword", "search keyword", "added/excluded"],
    "platform": ["platform", "source", "engine", "network"],
}


def resolve_columns(df: pd.DataFrame) -> dict[str, Optional[str]]:
    """Map logical field names to actual DataFrame column names."""
    lower_to_actual = {c.lower().strip(): c for c in df.columns}
    resolved = {}
    for logical, candidates in COLUMN_ALIASES.items():
        match = next((lower_to_actual[c] for c in candidates if c in lower_to_actual), None)
        resolved[logical] = match
    return resolved


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------
@dataclass
class Brand:
    canonical: str
    aliases: list[str] = field(default_factory=list)
    legal_entities: list[str] = field(default_factory=list)
    disqualifiers: list[str] = field(default_factory=list)
    require_context: list[str] = field(default_factory=list)
    broad_safe: bool = False

    @property
    def is_competitor(self) -> bool:
        return self.canonical == "__COMPETITOR__"

    @property
    def cleaned_legal_names(self) -> list[str]:
        """Return suffix-stripped, lowercased versions of each legal entity."""
        cleaned = []
        seen = set()
        for legal in self.legal_entities:
            c = normalize_legal_name(legal)
            if c and c not in seen:
                seen.add(c)
                cleaned.append(c)
        return cleaned

    def alias_patterns(self) -> list[tuple[str, re.Pattern]]:
        """Compile each alias AND each cleaned legal name into a whole-word regex."""
        out = []
        seen = set()
        # Original aliases first, then raw legal entities, then cleaned legal names
        for a in self.aliases + self.legal_entities + self.cleaned_legal_names:
            key = a.lower().strip()
            if key in seen:
                continue
            seen.add(key)
            pat = re.compile(rf"(?<![\w]){re.escape(key)}(?![\w])", re.IGNORECASE)
            out.append((a, pat))
        return out


def load_config(path: Path) -> list[Brand]:
    with path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    return [Brand(**entry) for entry in raw]


# ---------------------------------------------------------------------------
# Matching engine
# ---------------------------------------------------------------------------
@dataclass
class Match:
    query: str
    normalized: str
    brand: str            # canonical brand name (or "__COMPETITOR__")
    alias_hit: str        # the alias that fired
    match_type_rec: str   # EXACT | PHRASE | BROAD | NEEDS_REVIEW | MIXED
    reason: str           # human-readable explanation
    metrics: dict         # impressions/clicks/cost/conversions/etc.


def normalize(q: str) -> str:
    """Lowercase, replace separators with spaces, collapse whitespace."""
    if not isinstance(q, str):
        return ""
    s = q.lower()
    s = re.sub(r"[^\w\s\-&']", " ", s)  # keep word chars, hyphens, apostrophes, ampersands
    s = re.sub(r"\s+", " ", s).strip()
    return s


def query_contains(normalized: str, phrase: str) -> bool:
    """Check whole-word phrase containment, case-insensitive."""
    pat = re.compile(rf"(?<![\w]){re.escape(phrase.lower())}(?![\w])")
    return bool(pat.search(normalized))


# ---------------------------------------------------------------------------
# Legal-name normalization
# ---------------------------------------------------------------------------
# Multi-word phrases stripped from legal entity names. Order matters — longest
# first. We KEEP "insurance" since it's the anchor keyword.
LEGAL_SUFFIX_PHRASES = [
    "insurance services america",
    "insurance services llc",
    "insurance services inc",
    "casualty insurance company",
    "insurance services",
    "insurance agency",
    "insurance group",
    "insurance holdings",
    "insurance company",
    "insurance center",
    "insurance brokerage",
    "dealer services",
]

LEGAL_SUFFIX_TOKENS = {
    # Corporate entity types
    "inc", "inc.", "llc", "l.l.c.", "lp", "l.p.", "llp", "corp", "corporation",
    "ltd", "limited", "co", "co.", "company", "incorporated",
    # Business descriptors
    "services", "service", "agency", "agencies", "group", "holdings", "holding",
    "center", "centers", "brokerage", "brokers", "broker",
    "america", "american", "usa", "u.s.a.", "national",
    # Articles
    "the", "of",
}


def normalize_legal_name(legal_name: str) -> str:
    """Lowercase legal name, strip corporate suffixes/descriptors, keep 'insurance'.

    Examples:
      'Freeway Insurance Services Inc.'           -> 'freeway insurance'
      'InsureOne Insurance Services America LLC'  -> 'insureone insurance'
      'US Agencies Casualty Insurance Company'    -> 'us agencies casualty insurance'
      'NII Brokerage Services'                    -> 'nii'
    """
    if not legal_name:
        return ""

    s = legal_name.lower()
    s = re.sub(r"[^\w\s\-']", " ", s)
    s = re.sub(r"\s+", " ", s).strip()

    # Strip multi-word phrases first, longest-first
    for phrase in sorted(LEGAL_SUFFIX_PHRASES, key=len, reverse=True):
        if s.endswith(" " + phrase) or s == phrase:
            s = s[: -len(phrase)].strip()

    # Then strip trailing single-word tokens repeatedly
    tokens = s.split()
    while tokens and tokens[-1] in LEGAL_SUFFIX_TOKENS:
        tokens.pop()

    cleaned = " ".join(tokens).strip()

    # Re-append "insurance" if the original had it but cleaning removed it
    if "insurance" in legal_name.lower() and cleaned and "insurance" not in cleaned.split():
        cleaned = cleaned + " insurance"

    while cleaned.startswith("the "):
        cleaned = cleaned[4:]

    return cleaned.strip()


INSURANCE_CTX = {
    "insurance", "insurances", "ins", "auto", "car", "vehicle", "automobile",
    "home", "homeowners", "renters", "life", "health", "policy", "policies",
    "premium", "quote", "quotes", "coverage", "claim", "claims",
    "broker", "brokers", "brokerage", "agent", "agents", "agency", "agencies",
    "seguro", "seguros", "aseguranza", "aseguranzas",
}
BUSINESS_CTX = {"agency", "agencies", "brokerage", "company", "group"}


def has_context(phrase: str) -> bool:
    """True if phrase contains an insurance or business context word."""
    toks = set(phrase.lower().split())
    return bool(toks & INSURANCE_CTX) or bool(toks & BUSINESS_CTX)


def is_multi_word(phrase: str) -> bool:
    return len(phrase.split()) >= 2


def legal_name_conflicts_with_disqualifiers(
    legal_phrase: str, disqualifiers: list[str]
) -> bool:
    """Return True if a phrase overlaps with any of a brand's own disqualifiers.

    Used to downgrade phrase-match negatives to exact-only when there's a known
    false-positive risk (e.g. 'family auto insurance' shares the bigram
    'family auto' with the disqualifier 'american family auto').
    """
    if not legal_phrase or not disqualifiers:
        return False
    legal_tokens = legal_phrase.split()
    for disq in disqualifiers:
        if query_contains(disq, legal_phrase):
            return True
        for i in range(len(legal_tokens) - 1):
            bigram = " ".join(legal_tokens[i : i + 2])
            if query_contains(disq, bigram):
                return True
    return False


def classify_query(
    query: str,
    confie_brands: list[Brand],
    competitor: Optional[Brand],
    metrics: dict,
) -> list[Match]:
    """
    Return all matches for a single query. A query may match multiple Confie
    brands (rare but possible — e.g. 'freeway insurance vs estrella').
    """
    norm = normalize(query)
    if not norm:
        return []

    # Step 1: detect competitor presence
    competitor_hit = None
    if competitor:
        for alias, pat in competitor.alias_patterns():
            if pat.search(norm):
                competitor_hit = alias
                break

    matches = []
    for brand in confie_brands:
        # Find the first alias that hits
        alias_hit = None
        for alias, pat in brand.alias_patterns():
            if pat.search(norm):
                alias_hit = alias
                break
        if not alias_hit:
            continue

        # Disqualifier check — if any disqualifier phrase is present, skip
        dq_hit = next(
            (d for d in brand.disqualifiers if query_contains(norm, d)),
            None,
        )
        if dq_hit:
            continue

        # Required-context check — at least one context word must appear
        if brand.require_context:
            has_ctx = any(query_contains(norm, c) for c in brand.require_context)
            if not has_ctx:
                matches.append(Match(
                    query=query, normalized=norm, brand=brand.canonical,
                    alias_hit=alias_hit, match_type_rec="NEEDS_REVIEW",
                    reason=f"Alias '{alias_hit}' matched but no required context "
                           f"word found ({', '.join(brand.require_context)})",
                    metrics=metrics,
                ))
                continue

        # Mixed query — competitor + Confie brand
        if competitor_hit:
            matches.append(Match(
                query=query, normalized=norm, brand=brand.canonical,
                alias_hit=alias_hit, match_type_rec="MIXED",
                reason=f"Query contains Confie brand '{brand.canonical}' AND "
                       f"competitor '{competitor_hit}' — review intent before negating",
                metrics=metrics,
            ))
            continue

        # Recommend match type
        rec, reason = recommend_match_type(norm, alias_hit, brand)
        matches.append(Match(
            query=query, normalized=norm, brand=brand.canonical,
            alias_hit=alias_hit, match_type_rec=rec, reason=reason,
            metrics=metrics,
        ))

    return matches


def recommend_match_type(normalized: str, alias_hit: str, brand: Brand) -> tuple[str, str]:
    """Decide match type for an SQR-driven recommendation.

    Phrase-first strategy: prefer PHRASE for almost everything because one
    phrase negative blocks all longer queries containing it. EXACT is reserved
    for cases where phrase match wouldn't help OR would be unsafe.
    """
    alias_tokens = alias_hit.lower().split()
    alias_lc = alias_hit.lower()

    # Bare single-token brand without context — EXACT only
    if len(alias_tokens) == 1 and not has_context(alias_lc):
        return "EXACT", f"Bare single-token brand alias '{alias_hit}' — EXACT to avoid over-blocking."

    # Disqualifier-conflict check — if the alias overlaps with the brand's
    # own disqualifiers, phrase or broad match would risk false positives.
    # Downgrade to EXACT. Mirrors the static path's logic.
    if legal_name_conflicts_with_disqualifiers(alias_lc, brand.disqualifiers):
        return "EXACT", (
            f"Alias '{alias_hit}' overlaps with brand disqualifiers — "
            f"EXACT only to avoid blocking competitor/distinct-brand queries."
        )

    # Broad opt-in
    if brand.broad_safe and len(alias_tokens) >= 2:
        return "BROAD", f"broad_safe + multi-token alias"

    # Default: phrase match — single phrase entry blocks alias and all longer variants
    return "PHRASE", f"Phrase match on '{alias_hit}' — blocks all longer queries containing it."


# ---------------------------------------------------------------------------
# SQR loading
# ---------------------------------------------------------------------------
def load_sqr(path: Path, sheet_name: str) -> tuple[pd.DataFrame, dict]:
    """Load the SQR worksheet and resolve column names."""
    df = pd.read_excel(path, sheet_name=sheet_name)
    df.columns = [str(c).strip() for c in df.columns]
    cols = resolve_columns(df)
    if cols["search_term"] is None:
        raise ValueError(
            f"Could not find a 'search term' column. "
            f"Available columns: {list(df.columns)}\n"
            f"Edit COLUMN_ALIASES['search_term'] in this script to add your column name."
        )
    return df, cols


def metrics_for_row(row: pd.Series, cols: dict) -> dict:
    """Pull metric values, defaulting to 0 / None."""
    def get(k):
        c = cols.get(k)
        if c is None:
            return None
        v = row.get(c)
        if pd.isna(v):
            return 0 if k in ("impressions", "clicks", "cost", "conversions", "conv_value") else None
        return v

    return {
        "impressions": get("impressions") or 0,
        "clicks": get("clicks") or 0,
        "cost": float(get("cost") or 0),
        "conversions": float(get("conversions") or 0),
        "conv_value": float(get("conv_value") or 0),
        "campaign": get("campaign"),
        "ad_group": get("ad_group"),
        "match_type": get("match_type"),
        "keyword": get("keyword"),
        "platform": get("platform"),
    }


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------
def pick_canonical_phrase(brand):
    """Pick the canonical phrase that absorbs the most aliases.

    Preference order:
      1. Cleaned legal name (if multi-word and has context) — most natural form
      2. Shortest multi-word alias with insurance context — broadest coverage
    """
    for legal in brand.cleaned_legal_names:
        if is_multi_word(legal) and has_context(legal):
            return legal, "legal_name"

    candidates = []
    for alias in brand.aliases:
        a = alias.lower().strip()
        if is_multi_word(a) and has_context(a):
            candidates.append((len(a.split()), len(a), a))

    if not candidates:
        return None, None
    candidates.sort()
    return candidates[0][2], "alias"


def build_consolidated_negatives(confie_brands, include_broad_singletons=False):
    """Generate ONE phrase per brand + only the irreducible EXACT extras.

    Per brand:
      - 1 PHRASE (the canonical, absorbs all queries containing it)
      - 0+ EXACT (no-space variants, single-token bare brands, disqualifier-conflicted forms)
      - 1 BROAD if include_broad_singletons=True AND broad_safe=true
    """
    rows = []
    seen = set()

    for brand in confie_brands:
        phrase, phrase_source = pick_canonical_phrase(brand)
        phrase_conflicts = phrase and legal_name_conflicts_with_disqualifiers(phrase, brand.disqualifiers)

        all_candidates = []
        for legal in brand.cleaned_legal_names:
            all_candidates.append((legal.lower().strip(), "legal_name"))
        for alias in brand.aliases:
            a = alias.lower().strip()
            if a:
                all_candidates.append((a, "alias"))

        deduped = []
        seen_local = set()
        for kw, src in all_candidates:
            if kw not in seen_local:
                seen_local.add(kw)
                deduped.append((kw, src))

        absorbed_by_phrase = []

        if phrase and not phrase_conflicts:
            for kw, src in deduped:
                if kw == phrase:
                    continue
                if query_contains(kw, phrase):
                    absorbed_by_phrase.append(kw)

            key = (phrase, "PHRASE")
            if key not in seen:
                seen.add(key)
                rows.append({
                    "Brand": brand.canonical,
                    "Suggested Negative Keyword": phrase,
                    "Match Type": "PHRASE",
                    "Source": phrase_source,
                    "Absorbed Aliases": " | ".join(absorbed_by_phrase),
                    "Reason": (
                        f"Canonical phrase. Absorbs {len(absorbed_by_phrase)} alias(es)."
                        if absorbed_by_phrase else "Canonical phrase."
                    ),
                })
        elif phrase and phrase_conflicts:
            key = (phrase, "EXACT")
            if key not in seen:
                seen.add(key)
                rows.append({
                    "Brand": brand.canonical,
                    "Suggested Negative Keyword": phrase,
                    "Match Type": "EXACT",
                    "Source": phrase_source,
                    "Absorbed Aliases": "",
                    "Reason": f"Canonical phrase '{phrase}' overlaps with disqualifiers — EXACT only.",
                })

        for kw, src in deduped:
            if kw == phrase:
                continue
            if kw in absorbed_by_phrase:
                continue

            kw_has_ctx = has_context(kw)
            kw_multi = is_multi_word(kw)
            kw_conflicts = legal_name_conflicts_with_disqualifiers(kw, brand.disqualifiers)

            if kw_multi and kw_has_ctx and not kw_conflicts:
                if phrase and query_contains(kw, phrase):
                    continue
                key = (kw, "PHRASE")
                if key not in seen:
                    seen.add(key)
                    rows.append({
                        "Brand": brand.canonical,
                        "Suggested Negative Keyword": kw,
                        "Match Type": "PHRASE",
                        "Source": src,
                        "Absorbed Aliases": "",
                        "Reason": "Distinct multi-word phrase, not absorbed by canonical.",
                    })
            else:
                if not kw_has_ctx and not kw_multi:
                    if any(query_contains(d, kw) for d in brand.disqualifiers):
                        continue  # too risky — drop entirely
                key = (kw, "EXACT")
                if key not in seen:
                    seen.add(key)
                    if not kw_has_ctx and not kw_multi:
                        reason = "Bare brand name — EXACT only."
                    elif kw_conflicts:
                        reason = "Variant overlaps with disqualifiers — EXACT only."
                    elif not kw_multi:
                        reason = "Single-token variant — EXACT only."
                    else:
                        reason = "EXACT only."
                    rows.append({
                        "Brand": brand.canonical,
                        "Suggested Negative Keyword": kw,
                        "Match Type": "EXACT",
                        "Source": src,
                        "Absorbed Aliases": "",
                        "Reason": reason,
                    })

        if include_broad_singletons and brand.broad_safe:
            single_tokens = [a.lower().strip() for a in brand.aliases
                             if len(a.split()) == 1 and a.lower().strip()]
            if single_tokens:
                candidate = sorted(single_tokens, key=len)[0]
                if not any(query_contains(d, candidate) for d in brand.disqualifiers):
                    key = (candidate, "BROAD")
                    if key not in seen:
                        seen.add(key)
                        rows.append({
                            "Brand": brand.canonical,
                            "Suggested Negative Keyword": candidate,
                            "Match Type": "BROAD",
                            "Source": "alias",
                            "Absorbed Aliases": "",
                            "Reason": "broad_safe + no disqualifier conflicts.",
                        })

    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.sort_values(["Match Type", "Brand", "Suggested Negative Keyword"]).reset_index(drop=True)
    return df


def aggregate_recommendations(matches):
    """Group SQR matches by recommended match type, consolidating sub-phrases.

    Within each brand's PHRASE bucket, if alias B contains alias A as a
    whole-word substring AND A is shorter, A absorbs B's metrics. So
    'freeway insurance' absorbs 'freeway insurance phone number',
    'freeway insurance reviews', etc.
    """
    buckets = defaultdict(list)
    for m in matches:
        buckets[m.match_type_rec].append(m)

    output = {}
    for bucket, items in buckets.items():
        groups = defaultdict(list)
        for m in items:
            if bucket in ("EXACT", "PHRASE", "BROAD"):
                neg_kw = m.alias_hit.lower()
                key = (m.brand, neg_kw)
            else:
                key = (m.brand, m.query)
            groups[key].append(m)

        # Phrase consolidation: absorb longer sub-phrases into shorter parents
        if bucket == "PHRASE":
            by_brand = defaultdict(list)
            for (brand, kw), ms in groups.items():
                by_brand[brand].append((kw, ms))
            consolidated = {}
            for brand, items_list in by_brand.items():
                kws = [kw for kw, _ in items_list]
                for kw, ms in items_list:
                    absorbing_kw = next(
                        (other for other in kws
                         if other != kw and len(other) < len(kw) and query_contains(kw, other)),
                        None,
                    )
                    target_key = (brand, absorbing_kw or kw)
                    consolidated.setdefault(target_key, []).extend(ms)
            groups = consolidated

        rows = []
        for (brand, neg_kw), ms in groups.items():
            sample_queries = sorted({m.query for m in ms})[:5]
            absorbed_set = sorted({m.alias_hit.lower() for m in ms
                                   if m.alias_hit.lower() != neg_kw})
            rows.append({
                "Brand": brand,
                "Suggested Negative Keyword": neg_kw,
                "Match Type": bucket if bucket in ("EXACT", "PHRASE", "BROAD") else "",
                "Triggering Query Count": len(ms),
                "Total Impressions": sum(m.metrics["impressions"] for m in ms),
                "Total Clicks": sum(m.metrics["clicks"] for m in ms),
                "Total Cost": round(sum(m.metrics["cost"] for m in ms), 2),
                "Total Conversions": round(sum(m.metrics["conversions"] for m in ms), 2),
                "Total Conv Value": round(sum(m.metrics["conv_value"] for m in ms), 2),
                "Sample Queries": " | ".join(sample_queries),
                "Absorbed Aliases": " | ".join(absorbed_set),
                "Reason": ms[0].reason,
                "Platforms": ", ".join(sorted({str(m.metrics["platform"]) for m in ms if m.metrics["platform"]})),
            })
        df = pd.DataFrame(rows)
        if not df.empty:
            df = df.sort_values("Total Cost", ascending=False).reset_index(drop=True)
        output[bucket] = df
    return output


def all_matches_dataframe(matches: list[Match]) -> pd.DataFrame:
    rows = []
    for m in matches:
        rows.append({
            "Query": m.query,
            "Brand Matched": m.brand,
            "Alias Hit": m.alias_hit,
            "Recommended Match Type": m.match_type_rec,
            "Reason": m.reason,
            "Impressions": m.metrics["impressions"],
            "Clicks": m.metrics["clicks"],
            "Cost": round(m.metrics["cost"], 2),
            "Conversions": round(m.metrics["conversions"], 2),
            "Conv Value": round(m.metrics["conv_value"], 2),
            "Campaign": m.metrics["campaign"],
            "Ad Group": m.metrics["ad_group"],
            "Platform": m.metrics["platform"],
        })
    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.sort_values(["Recommended Match Type", "Cost"], ascending=[True, False]).reset_index(drop=True)
    return df


# ---------------------------------------------------------------------------
# Excel output
# ---------------------------------------------------------------------------
HEADER_FILL = PatternFill("solid", start_color="1F4E79")
HEADER_FONT = Font(name="Arial", size=11, bold=True, color="FFFFFF")
BODY_FONT = Font(name="Arial", size=10)


def write_workbook(
    output_path: Path,
    summary: pd.DataFrame,
    by_match_type: dict[str, pd.DataFrame],
    static_negs: pd.DataFrame,
    all_matches_df: pd.DataFrame,
    no_match_df: pd.DataFrame,
    deploy_df: pd.DataFrame = None,
):
    wb = Workbook()
    # Drop default sheet
    wb.remove(wb.active)

    sheets_in_order = [
            ("Summary", summary),
            ("Deploy_This", deploy_df if deploy_df is not None else pd.DataFrame()),
            ("Negative_Exact", by_match_type.get("EXACT", pd.DataFrame())),
            ("Negative_Phrase", by_match_type.get("PHRASE", pd.DataFrame())),
            ("Negative_Broad", by_match_type.get("BROAD", pd.DataFrame())),
            ("All_Brands_Phrase", static_negs[static_negs["Match Type"] == "PHRASE"] if not static_negs.empty else pd.DataFrame()),
            ("All_Brands_Exact", static_negs[static_negs["Match Type"] == "EXACT"] if not static_negs.empty else pd.DataFrame()),
            ("All_Brands_Broad", static_negs[static_negs["Match Type"] == "BROAD"] if not static_negs.empty else pd.DataFrame()),
            ("Mixed_Queries", by_match_type.get("MIXED", pd.DataFrame())),
            ("Needs_Review", by_match_type.get("NEEDS_REVIEW", pd.DataFrame())),
            ("All_Matches", all_matches_df),
            ("No_Match", no_match_df),
        ]

    for name, df in sheets_in_order:
        ws = wb.create_sheet(name)
        if df is None or df.empty:
            ws["A1"] = "(no rows)"
            ws["A1"].font = BODY_FONT
            continue

        # Header
        for col_idx, col_name in enumerate(df.columns, start=1):
            cell = ws.cell(row=1, column=col_idx, value=col_name)
            cell.font = HEADER_FONT
            cell.fill = HEADER_FILL
            cell.alignment = Alignment(horizontal="left", vertical="center")

        # Body
        for row_idx, row in enumerate(df.itertuples(index=False), start=2):
            for col_idx, value in enumerate(row, start=1):
                cell = ws.cell(row=row_idx, column=col_idx, value=value)
                cell.font = BODY_FONT

        # Column widths
        for col_idx, col_name in enumerate(df.columns, start=1):
            max_len = max(
                [len(str(col_name))]
                + [len(str(v)) for v in df.iloc[:, col_idx - 1].astype(str).tolist()[:200]]
            )
            ws.column_dimensions[get_column_letter(col_idx)].width = min(max_len + 2, 60)

        ws.freeze_panes = "A2"

    wb.save(output_path)


def build_deploy_sheet(
    static_negs: pd.DataFrame,
    by_match_type: dict[str, pd.DataFrame],
) -> pd.DataFrame:
    """Build a single deployable negative-keyword sheet.

    Combines static (proactive) negatives with SQR-driven negatives, deduped
    on (Suggested Negative Keyword, Match Type). When a static row matches an
    SQR row, the SQR metrics are merged onto the static row so deployment
    decisions can be cost-prioritized.

    Output columns:
        Brand, Suggested Negative Keyword, Match Type, Source,
        Triggering Query Count, Total Impressions, Total Clicks, Total Cost,
        Total Conversions, Sample Queries, Reason

    Sorted by Total Cost descending. Proactive-only rows (zero SQR exposure)
    sort to the bottom.
    """
    # Collect all SQR-driven rows into one frame
    sqr_frames = []
    for mt in ("EXACT", "PHRASE", "BROAD"):
        df = by_match_type.get(mt, pd.DataFrame())
        if df is not None and not df.empty:
            sqr_frames.append(df)
    sqr_combined = pd.concat(sqr_frames, ignore_index=True) if sqr_frames else pd.DataFrame()

    metric_cols = [
        "Triggering Query Count", "Total Impressions", "Total Clicks",
        "Total Cost", "Total Conversions", "Sample Queries",
    ]

    rows = []
    seen_keys = set()  # (keyword_lower, match_type)

    # Pass 1: static rows (proactive base) — merge SQR metrics where the
    # static keyword+match_type matches an SQR row.
    if not static_negs.empty:
        for _, srow in static_negs.iterrows():
            kw = str(srow["Suggested Negative Keyword"]).lower().strip()
            mt = srow["Match Type"]
            key = (kw, mt)
            seen_keys.add(key)

            metrics = {c: 0 for c in metric_cols if c != "Sample Queries"}
            metrics["Sample Queries"] = ""
            source_label = "static"
            if not sqr_combined.empty:
                hit = sqr_combined[
                    (sqr_combined["Suggested Negative Keyword"].astype(str).str.lower().str.strip() == kw)
                    & (sqr_combined["Match Type"] == mt)
                ]
                if not hit.empty:
                    h = hit.iloc[0]
                    for c in metric_cols:
                        if c in hit.columns:
                            metrics[c] = h[c]
                    source_label = "static + SQR"

            rows.append({
                "Brand": srow["Brand"],
                "Suggested Negative Keyword": srow["Suggested Negative Keyword"],
                "Match Type": mt,
                "Source": source_label,
                "Triggering Query Count": metrics.get("Triggering Query Count", 0) or 0,
                "Total Impressions": metrics.get("Total Impressions", 0) or 0,
                "Total Clicks": metrics.get("Total Clicks", 0) or 0,
                "Total Cost": metrics.get("Total Cost", 0) or 0,
                "Total Conversions": metrics.get("Total Conversions", 0) or 0,
                "Sample Queries": metrics.get("Sample Queries", "") or "",
                "Reason": srow.get("Reason", ""),
            })

    # Pass 2: SQR-only rows (showed up in live traffic but not in static config)
    if not sqr_combined.empty:
        for _, qrow in sqr_combined.iterrows():
            kw = str(qrow["Suggested Negative Keyword"]).lower().strip()
            mt = qrow["Match Type"]
            key = (kw, mt)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            rows.append({
                "Brand": qrow["Brand"],
                "Suggested Negative Keyword": qrow["Suggested Negative Keyword"],
                "Match Type": mt,
                "Source": "SQR-only",
                "Triggering Query Count": qrow.get("Triggering Query Count", 0) or 0,
                "Total Impressions": qrow.get("Total Impressions", 0) or 0,
                "Total Clicks": qrow.get("Total Clicks", 0) or 0,
                "Total Cost": qrow.get("Total Cost", 0) or 0,
                "Total Conversions": qrow.get("Total Conversions", 0) or 0,
                "Sample Queries": qrow.get("Sample Queries", "") or "",
                "Reason": qrow.get("Reason", ""),
            })

    deploy = pd.DataFrame(rows)
    if not deploy.empty:
        deploy = deploy.sort_values(
            ["Total Cost", "Brand", "Suggested Negative Keyword"],
            ascending=[False, True, True],
        ).reset_index(drop=True)
    return deploy


def build_summary(
    by_match_type: dict[str, pd.DataFrame],
    total_queries: int,
    no_match_count: int,
    static_negs: pd.DataFrame = None,
) -> pd.DataFrame:
    rows = [
        {"Category": "Total queries analyzed", "Count": total_queries, "Total Cost": ""},
        {"Category": "Queries with no Confie match", "Count": no_match_count, "Total Cost": ""},
        {"Category": "", "Count": "", "Total Cost": ""},
        {"Category": "— SQR-driven recommendations —", "Count": "", "Total Cost": ""},
    ]
    label_map = {
        "EXACT": "Negative Exact recommendations",
        "PHRASE": "Negative Phrase recommendations",
        "BROAD": "Negative Broad recommendations",
        "MIXED": "Mixed queries (Confie + competitor) — review",
        "NEEDS_REVIEW": "Needs review (missing context)",
    }
    for key, label in label_map.items():
        df = by_match_type.get(key, pd.DataFrame())
        count = len(df)
        cost = df["Total Cost"].sum() if not df.empty else 0
        rows.append({"Category": label, "Count": count, "Total Cost": round(cost, 2)})

    if static_negs is not None and not static_negs.empty:
        rows.append({"Category": "", "Count": "", "Total Cost": ""})
        rows.append({"Category": "— All-brands static negatives —", "Count": "", "Total Cost": ""})
        for mt in ("PHRASE", "EXACT", "BROAD"):
            n = int((static_negs["Match Type"] == mt).sum())
            rows.append({"Category": f"All-brands {mt.lower()}-match negatives", "Count": n, "Total Cost": ""})

    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--sqr", required=True, type=Path, help="Path to SQR xlsx file")
    p.add_argument("--config", required=True, type=Path, help="Path to confie_brands.yaml")
    p.add_argument("--output", type=Path, default=Path("confie_negative_keywords.xlsx"))
    p.add_argument("--sheet", default="SQR-master", help="SQR worksheet name")
    p.add_argument("--include-broad-singletons", action="store_true",
               help="Emit broad-match negatives for brands marked broad_safe.")
    args = p.parse_args()

    if not args.sqr.exists():
        sys.exit(f"SQR file not found: {args.sqr}")
    if not args.config.exists():
        sys.exit(f"Config file not found: {args.config}")

    print(f"Loading config from {args.config}...")
    brands = load_config(args.config)
    competitor = next((b for b in brands if b.is_competitor), None)
    confie_brands = [b for b in brands if not b.is_competitor]
    print(f"  Loaded {len(confie_brands)} Confie brand(s) + competitor list")

    print(f"Loading SQR from {args.sqr} (sheet: {args.sheet})...")
    df, cols = load_sqr(args.sqr, args.sheet)
    print(f"  {len(df):,} rows, columns resolved: "
          f"{ {k: v for k, v in cols.items() if v} }")

    print("Classifying queries...")
    all_matches: list[Match] = []
    matched_query_set: set[str] = set()

    for _, row in df.iterrows():
        q = row[cols["search_term"]]
        if not isinstance(q, str) or not q.strip():
            continue
        metrics = metrics_for_row(row, cols)
        ms = classify_query(q, confie_brands, competitor, metrics)
        if ms:
            all_matches.extend(ms)
            matched_query_set.add(q)

    print(f"  {len(all_matches):,} matches found across {len(matched_query_set):,} unique queries")

    # Build no-match dataframe
    no_match_rows = []
    for _, row in df.iterrows():
        q = row[cols["search_term"]]
        if isinstance(q, str) and q.strip() and q not in matched_query_set:
            metrics = metrics_for_row(row, cols)
            no_match_rows.append({
                "Query": q,
                "Impressions": metrics["impressions"],
                "Clicks": metrics["clicks"],
                "Cost": round(metrics["cost"], 2),
                "Campaign": metrics["campaign"],
                "Platform": metrics["platform"],
            })
    no_match_df = pd.DataFrame(no_match_rows)
    if not no_match_df.empty:
        no_match_df = no_match_df.sort_values("Cost", ascending=False).reset_index(drop=True)

    # Aggregate
    print("Aggregating recommendations...")
    by_match_type = aggregate_recommendations(all_matches)
    all_matches_df = all_matches_dataframe(all_matches)

    # Build proactive static negatives across ALL brands (not just SQR-active ones)
    print("Building static all-brands negative list...")
    static_negs = build_consolidated_negatives(confie_brands, include_broad_singletons=args.include_broad_singletons,)
    if not static_negs.empty:
        counts = static_negs["Match Type"].value_counts().to_dict()
        print(f"  Static negatives: "
              f"{counts.get('PHRASE', 0)} phrase, "
              f"{counts.get('EXACT', 0)} exact, "
              f"{counts.get('BROAD', 0)} broad")

    print("Building Deploy_This sheet (static + SQR, deduped, cost-sorted)...")
    deploy_df = build_deploy_sheet(static_negs, by_match_type)
    print(f"  Deploy_This: {len(deploy_df)} total rows "
          f"(${deploy_df['Total Cost'].sum():,.2f} aggregate SQR cost)")

    summary = build_summary(by_match_type, len(df), len(no_match_df), static_negs)

    print(f"Writing output to {args.output}...")
    write_workbook(args.output, summary, by_match_type, static_negs,
                   all_matches_df, no_match_df, deploy_df)
    print("Done.")
    print()
    print("Summary:")
    for _, r in summary.iterrows():
        if r["Category"]:
            print(f"  {r['Category']}: {r['Count']}  (cost: {r['Total Cost']})")


if __name__ == "__main__":
    main()