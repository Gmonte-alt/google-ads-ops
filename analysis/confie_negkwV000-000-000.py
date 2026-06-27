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

    def alias_patterns(self) -> list[tuple[str, re.Pattern]]:
        """Compile each alias into a whole-word regex. Returns (alias, pattern) pairs."""
        out = []
        for a in self.aliases + self.legal_entities:
            pat = re.compile(rf"(?<![\w]){re.escape(a.lower())}(?![\w])", re.IGNORECASE)
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
    """
    Decision logic:
      EXACT  — query equals alias OR alias plus 1 generic modifier (insurance/quote/near me)
      PHRASE — alias appears as contiguous phrase inside a longer query (default)
      BROAD  — only if brand.broad_safe AND alias is multi-token
    """
    GENERIC_MODIFIERS = {
        "insurance", "quote", "quotes", "near", "me", "near me", "phone",
        "number", "login", "agent", "office", "location", "locations",
        "reviews", "review", "company", "agency", "auto", "car",
    }
    alias_tokens = alias_hit.lower().split()
    query_tokens = normalized.split()

    # Exact match: query == alias
    if normalized == alias_hit.lower():
        return "EXACT", f"Query is exactly the brand alias '{alias_hit}'"

    # Exact match: query is alias + only generic modifier tokens
    extra = [t for t in query_tokens if t not in alias_tokens]
    if extra and all(t in GENERIC_MODIFIERS for t in extra):
        return "EXACT", (
            f"Query is alias '{alias_hit}' plus only generic modifier(s): "
            f"{' '.join(extra)}"
        )

    # Broad: only if brand explicitly broad-safe and alias is multi-word
    if brand.broad_safe and len(alias_tokens) >= 2:
        return "BROAD", (
            f"Brand '{brand.canonical}' is marked broad_safe and alias "
            f"'{alias_hit}' is multi-token — safe for broad-match negative"
        )

    # Default: phrase match
    return "PHRASE", (
        f"Alias '{alias_hit}' matched inside longer query — phrase match recommended"
    )


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
def aggregate_recommendations(matches: list[Match]) -> dict[str, pd.DataFrame]:
    """
    Group matches by recommended match type, then by the keyword to negate.
    For each negative-keyword recommendation, sum metrics across the queries
    that triggered it and keep up to 5 sample queries.
    """
    buckets = defaultdict(list)
    for m in matches:
        buckets[m.match_type_rec].append(m)

    # For each match-type bucket, group by (brand, suggested negative keyword)
    output = {}
    for bucket, items in buckets.items():
        rows = []
        groups = defaultdict(list)

        for m in items:
            # The negative keyword we'd add to Google/MS Ads:
            #   - EXACT/PHRASE/BROAD: use the alias that fired
            #   - MIXED/NEEDS_REVIEW: keep the full query for human inspection
            if bucket in ("EXACT", "PHRASE", "BROAD"):
                neg_kw = m.alias_hit.lower()
                key = (m.brand, neg_kw)
            else:
                neg_kw = m.query
                key = (m.brand, m.query)
            groups[key].append(m)

        for (brand, neg_kw), ms in groups.items():
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
                "Sample Queries": " | ".join(sorted({m.query for m in ms})[:5]),
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
    all_matches_df: pd.DataFrame,
    no_match_df: pd.DataFrame,
):
    wb = Workbook()
    # Drop default sheet
    wb.remove(wb.active)

    sheets_in_order = [
        ("Summary", summary),
        ("Negative_Exact", by_match_type.get("EXACT", pd.DataFrame())),
        ("Negative_Phrase", by_match_type.get("PHRASE", pd.DataFrame())),
        ("Negative_Broad", by_match_type.get("BROAD", pd.DataFrame())),
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


def build_summary(by_match_type: dict[str, pd.DataFrame], total_queries: int, no_match_count: int) -> pd.DataFrame:
    rows = [
        {"Category": "Total queries analyzed", "Count": total_queries, "Total Cost": ""},
        {"Category": "Queries with no Confie match", "Count": no_match_count, "Total Cost": ""},
        {"Category": "", "Count": "", "Total Cost": ""},
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
    summary = build_summary(by_match_type, len(df), len(no_match_df))

    print(f"Writing output to {args.output}...")
    write_workbook(args.output, summary, by_match_type, all_matches_df, no_match_df)
    print("Done.")
    print()
    print("Summary:")
    for _, r in summary.iterrows():
        if r["Category"]:
            print(f"  {r['Category']}: {r['Count']}  (cost: {r['Total Cost']})")


if __name__ == "__main__":
    main()