"""
keyword_expansion_sizing.py
============================
Sizes two paid-search initiatives for the Home Insurance account:

  CASE 1 — Keyword Expansion via SQ
    Find untargeted search queries with strong economics, cluster them
    by semantic similarity within (Ad Group × Query Group) buckets, and
    recommend a small set of canonical keyword phrases per bucket.
    Recommendation budget per ad group is tiered (≤10 baseline, ≤20
    expanded, >20 split into new ad group). Long-tail clusters cap at 3
    keywords; head clusters get 5.

  CASE 2 — Non-Brand CID Split via KW+SQ
    Subset the expansion candidates to non-brand-only (Query Group does
    not contain "brand" or "carrier" AND search query Brand field is
    empty). Apply a +20% RPL uplift hypothesis (revenue × 1.20, holding
    cost constant) to size the incremental GP from a separate non-brand
    CID with higher partner RPL.

INPUT
─────
  Reads from home_keyword_analysis.xlsx (the V3 analysis workbook), sheet
  "Search Query Analysis (30d)". This sheet already has Query Group,
  Targeting Status, Brand Category, Brand, and per-query economics — no
  DB access needed.

QUALIFICATION GATE
──────────────────
  A (Ad Group × Query Group) cluster qualifies for recommendations if:
    (a) ≥30 conversions OR ≥1000 impressions in the cluster
    (b) Cluster ROAS ≥ 0.70 (V3 standard)
  Additionally, only Targeting Status = "Not Targeted" rows are
  considered (already-targeted queries are not the expansion opportunity).

CLUSTERING (per qualifying cluster)
───────────────────────────────────
  TF-IDF (1–2 ngrams) + nearest-neighbor search using FAISS if available,
  scikit-learn NearestNeighbors otherwise. Within each cluster:
    - Normalize home-insurance vocabulary (homeowners → home, near me
      synonyms, cost/price synonyms, carrier-name canonicalization).
    - Group similar queries via L2 distance ≤ 0.6 (tuneable).
    - Pick canonical phrase per group by GP-weighted score (not raw
      frequency — V3 picks revenue-bearing canonicals).
    - Take top K canonicals per cluster (5 for head, 3 for long-tail).
  Head/long-tail split: head if cluster impressions ≥ median of all
  qualifying clusters, else long-tail.

PER-AD-GROUP CAPACITY TIERS
───────────────────────────
  After per-cluster recommendations are generated, total recs per ad
  group is bucketed:
    - Tier A (≤10): fits in current ad group at baseline policy
    - Tier B (11–20): exceeds baseline but within Google's hard cap;
                      validate ad copy fit before adding
    - Tier C (>20):  too many for one ad group; split candidates by
                     Query Group theme into new ad groups

NON-BRAND FILTER (Case 2)
─────────────────────────
  Non-brand subset must satisfy BOTH:
    - Query Group name does NOT contain "brand" or "carrier"
    - Search query Brand field is empty after fuzzy-brand sanitization
      (already applied by V3 in the input workbook)

GP DEFINITION
─────────────
  GP = Revenue ($) − Spend ($)  (per-row, then aggregated)
  Uplifted GP = (Revenue × 1.20) − Spend  (Case 2 only)

OUTPUT — keyword_expansion_sizing.xlsx
  Metadata
  Sizing Summary                  point estimates for both cases
  Cluster Economics               every (AG × QG) Not Targeted cluster
                                  with qualification flags
  Recommended Clusters            qualifying clusters with canonical
                                  keywords, head/tail tag, ad-group tier
  Recommended Keywords            flat list of canonical keyword
                                  recommendations
  AG Capacity Rollup              per-ad-group keyword count + tier
  Non-Brand Recommended Clusters  Case 2 subset
  Non-Brand Sizing Detail         per-cluster non-brand sizing with
                                  +20% RPL uplift applied transparently
  Vocab Normalization Log         every (raw → normalized) mapping

RUN
───
  conda activate paid-search
  python analysis/keyword_expansion_sizing.py [--initiative {expansion,nonbrand,all}]
"""

import argparse
import re
import string
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

# ── Configuration ──────────────────────────────────────────────────
INPUT_DIR  = Path(r"C:\Projects\google-ads-ops\analysis\output")
OUTPUT_DIR = Path(r"C:\Projects\google-ads-ops\analysis\output")
INPUT_FILE  = INPUT_DIR  / "home_keyword_analysis.xlsx"
INPUT_SHEET = "Search Query Analysis (30d)"
OUTPUT_FILE = OUTPUT_DIR / "keyword_expansion_sizing.xlsx"

# ROAS / spend thresholds — match V3 conventions
WATCH_ROAS  = 0.70
MIN_SPEND   = 5.00

# Qualification thresholds (per cluster)
MIN_CONVS_PER_CLUSTER       = 30
MIN_IMPRESSIONS_PER_CLUSTER = 1000

# Per-cluster recommendation budget
HEAD_KW_PER_CLUSTER     = 5     # high-volume clusters
LONG_TAIL_KW_PER_CLUSTER = 3    # lower-volume clusters

# Ad-group capacity tiers
AG_TIER_A_MAX = 10   # ≤10: baseline, fits current ad group
AG_TIER_B_MAX = 20   # 11-20: expanded ad group, validate ad copy fit
                     # >20: split into new ad groups

# Case 1 — Keyword Expansion uplift hypothesis
# (Hypothesis-based; no historical reference point in this account.)
# Mechanism: direct exact-match keywords get higher Quality Scores → lower
# CPCs, and ad copy can be themed to the keyword → higher CTR. Volume held
# constant (we are NOT pausing the broad keywords; expansion is additive).
EXPANSION_CPC_EFFICIENCY = 0.05   # CPC drops 5% on targeted queries
EXPANSION_CTR_LIFT       = 0.10   # CTR rises 10% on targeted queries

# Case 2 — Non-Brand RPL uplift hypothesis (+20%)
NON_BRAND_RPL_UPLIFT = 1.20

# TF-IDF clustering params
TFIDF_NGRAM_RANGE  = (1, 2)
TFIDF_MIN_DF       = 1
NEIGHBOR_K         = 5
L2_DISTANCE_MAX    = 0.6   # bathroom-script tuned threshold

# Minimum cluster size for TF-IDF (below this, treat as too small to cluster)
MIN_CLUSTER_QUERIES_FOR_TFIDF = 5


# ── Home-Insurance vocabulary normalization ───────────────────────
# Replaces noisy variants with canonical forms before TF-IDF. Tightly
# scoped to home-insurance vocabulary to avoid over-canonicalizing.
HI_REPLACEMENTS = [
    # Homeowners variants — most important
    (r'\bhome\s*owners?\b',           'home'),
    (r'\bhomeowners?\s+insurance\b',  'home insurance'),
    (r'\bhouse\s+insurance\b',        'home insurance'),
    (r'\bhome\s+ins\b',               'home insurance'),
    (r'\bhomeins\b',                  'home insurance'),

    # Quote variants
    (r'\bquotes\b',                   'quote'),
    (r'\bquoting\b',                  'quote'),
    (r'\bget\s+a\s+quote\b',          'quote'),

    # Near-me / local variants
    (r'\bin\s+my\s+area\b',           'near me'),
    (r'\bnear\s+by\b',                'near me'),
    (r'\bnearby\b',                   'near me'),
    (r'\baround\s+me\b',              'near me'),

    # Cost / price variants
    (r'\bpricing\b',                  'cost'),
    (r'\bprice\b',                    'cost'),
    (r'\bprices\b',                   'cost'),
    (r'\bhow\s+much\b',               'cost'),
    (r'\bestimate\b',                 'cost'),
    (r'\bestimates\b',                'cost'),

    # Cheap variants
    (r'\bcheapest\b',                 'cheap'),
    (r'\baffordable\b',               'cheap'),
    (r'\binexpensive\b',              'cheap'),
    (r'\blow\s+cost\b',               'cheap'),

    # Best variants
    (r'\btop\b',                      'best'),
    (r'\bgood\b',                     'best'),

    # Compare variants
    (r'\bcomparing\b',                'compare'),
    (r'\bcompared\b',                 'compare'),
    (r'\bcomparison\b',               'compare'),
    (r'\bvs\b',                       'compare'),
    (r'\bversus\b',                   'compare'),

    # Coverage variants
    (r'\bcoverages\b',                'coverage'),
    (r'\bcovered\b',                  'coverage'),

    # Policy variants
    (r'\bpolicies\b',                 'policy'),

    # Insurance abbreviation
    (r'\bins\b',                      'insurance'),
    (r'\binsur\b',                    'insurance'),

    # Carrier brand canonicalization (collapse common misspellings)
    (r'\bstate\s+farm\b',             'statefarm'),
    (r'\bcal\s+fair\b',               'calfair'),
    (r'\bcalifornia\s+fair\b',        'calfair'),
    (r'\bca\s+fair\b',                'calfair'),
    (r'\befarmers\b',                 'farmers'),
]

# Stopwords — kept conservative; we don't want to lose meaningful tokens
HI_STOPWORDS = {'the', 'a', 'an', 'of', 'for', 'in', 'to', 'and', 'or'}


def normalize_text(text: str, log: dict = None) -> str:
    """Apply home-insurance vocab normalization to a search query."""
    if not isinstance(text, str):
        return ''
    raw = text
    out = text.lower().strip()

    # Strip basic punctuation (keep spaces and word characters)
    out = out.translate(str.maketrans('', '', string.punctuation))

    # Apply replacements
    for pattern, repl in HI_REPLACEMENTS:
        out = re.sub(pattern, repl, out, flags=re.IGNORECASE)

    # Tokenize, drop stopwords, collapse whitespace
    tokens = [t for t in out.split() if t and t not in HI_STOPWORDS]
    out = ' '.join(tokens)

    # Log non-trivial changes
    if log is not None and out != raw.lower().strip():
        log[raw] = out
    return out


# ── Non-brand filter ──────────────────────────────────────────────
def is_non_brand_query_group(qg: str) -> bool:
    """Query Group does not contain 'brand' or 'carrier' (case-insensitive)."""
    if pd.isna(qg):
        return True
    qg_lower = str(qg).lower()
    return ('brand' not in qg_lower) and ('carrier' not in qg_lower)


def is_non_brand_row(row) -> bool:
    """Two-step: QG name not brand/carrier AND Brand field is empty."""
    qg_ok = is_non_brand_query_group(row.get('Query Group'))
    brand_val = row.get('Brand')
    brand_empty = pd.isna(brand_val) or not str(brand_val).strip()
    return qg_ok and brand_empty


# ── ROAS bucket helper ────────────────────────────────────────────
def roas_bucket(spend: float, roas: float) -> str:
    if pd.isna(spend) or spend < MIN_SPEND:    return 'Insufficient spend'
    if pd.isna(roas):                           return 'No conversions'
    if roas < 0.50:                             return 'Critical (ROAS < 0.50)'
    if roas < WATCH_ROAS:                       return f'Watch (ROAS 0.50-{WATCH_ROAS})'
    return f'Healthy (ROAS >= {WATCH_ROAS})'


# ── Clustering backend (FAISS preferred, sklearn fallback) ────────
def _get_neighbors(tfidf_dense: np.ndarray, k: int):
    """
    Returns (distances, indices) of nearest neighbors. Uses FAISS if
    available, else sklearn NearestNeighbors. Both return the same
    interface: distances[i, j] is the L2 distance from item i to its
    j-th nearest neighbor (j=0 is item i itself, distance 0).
    """
    try:
        import faiss
        dim = tfidf_dense.shape[1]
        index = faiss.IndexFlatL2(dim)
        index.add(tfidf_dense.astype(np.float32))
        return index.search(tfidf_dense.astype(np.float32), k)
    except ImportError:
        from sklearn.neighbors import NearestNeighbors
        actual_k = min(k, len(tfidf_dense))
        nn = NearestNeighbors(n_neighbors=actual_k, metric='euclidean')
        nn.fit(tfidf_dense)
        # sklearn returns distances (not squared) — FAISS returns squared L2
        # We square sklearn's to match FAISS's distance scale.
        dists, idxs = nn.kneighbors(tfidf_dense)
        return dists ** 2, idxs


def cluster_and_pick_canonicals(queries_df: pd.DataFrame,
                                 k_canonicals: int,
                                 normalization_log: dict) -> list:
    """
    Cluster the queries in queries_df via TF-IDF + nearest neighbors.
    Pick top k_canonicals canonical phrases by GP-weighted score.

    Input queries_df must have:
      'Search Query', 'Spend ($)', 'Revenue ($)', 'Conversions',
      'Impressions', 'Clicks'

    Returns a list of dicts, each describing one canonical recommendation.
    """
    from sklearn.feature_extraction.text import TfidfVectorizer

    df = queries_df.copy()
    df['gp'] = df['Revenue ($)'] - df['Spend ($)']
    df['_norm'] = df['Search Query'].apply(
        lambda t: normalize_text(t, normalization_log))
    df = df[df['_norm'].str.len() > 0]
    if df.empty:
        return []

    # If too few queries, skip clustering and just return the top by GP
    if len(df['_norm'].unique()) < MIN_CLUSTER_QUERIES_FOR_TFIDF:
        # Return the highest-GP unique normalized phrases directly
        agg = df.groupby('_norm', as_index=False).agg(
            spend=('Spend ($)','sum'), revenue=('Revenue ($)','sum'),
            convs=('Conversions','sum'),
            impressions=('Impressions','sum'), clicks=('Clicks','sum'),
            gp=('gp','sum'), supporting_query_count=('Search Query','count'))
        agg['roas'] = agg['revenue'] / agg['spend'].replace(0, np.nan)
        agg = agg.sort_values('gp', ascending=False).head(k_canonicals)
        return [_canonical_dict(r['_norm'], r['_norm'],
                                spend=r['spend'], revenue=r['revenue'],
                                gp=r['gp'], convs=r['convs'],
                                impressions=r['impressions'],
                                supporting=r['supporting_query_count'],
                                small_cluster=True)
                for _, r in agg.iterrows()]

    # TF-IDF on UNIQUE normalized terms
    unique_terms = df['_norm'].unique()
    try:
        vectorizer = TfidfVectorizer(ngram_range=TFIDF_NGRAM_RANGE,
                                     min_df=TFIDF_MIN_DF)
        tfidf_matrix = vectorizer.fit_transform(unique_terms)
        tfidf_dense  = tfidf_matrix.astype(np.float32).toarray()
    except ValueError:
        # Empty vocabulary — fall back to direct GP ranking
        return cluster_and_pick_canonicals(
            df.assign(**{'Search Query': df['_norm']}),
            k_canonicals, normalization_log)

    # Nearest neighbors
    distances, indices = _get_neighbors(tfidf_dense, NEIGHBOR_K)

    # Per-normalized-term GP, used for canonical selection
    term_economics = df.groupby('_norm', as_index=False).agg(
        spend=('Spend ($)','sum'), revenue=('Revenue ($)','sum'),
        convs=('Conversions','sum'),
        impressions=('Impressions','sum'), clicks=('Clicks','sum'),
        gp=('gp','sum'), supporting=('Search Query','count'))
    term_to_econ = {r['_norm']: r for _, r in term_economics.iterrows()}

    # Build clusters: each unique term groups with neighbors below distance
    canonical_map = {}    # term → cluster_id
    cluster_members = {}  # cluster_id → list of terms

    for i, term in enumerate(unique_terms):
        if term in canonical_map:
            continue
        cluster_id = len(cluster_members)
        canonical_map[term] = cluster_id
        cluster_members[cluster_id] = [term]

        for j_idx, dist in zip(indices[i, 1:], distances[i, 1:]):
            if dist > L2_DISTANCE_MAX:
                continue
            neighbor = unique_terms[j_idx]
            if neighbor in canonical_map:
                continue
            canonical_map[neighbor] = cluster_id
            cluster_members[cluster_id].append(neighbor)

    # For each cluster, aggregate economics and pick the canonical (highest GP term)
    clusters = []
    for cid, members in cluster_members.items():
        m_econ = [term_to_econ[m] for m in members if m in term_to_econ]
        if not m_econ:
            continue
        # Canonical = member with highest GP (ties broken by highest spend)
        canonical = max(m_econ, key=lambda r: (r['gp'], r['spend']))
        # Aggregate cluster
        c_spend  = sum(r['spend']       for r in m_econ)
        c_rev    = sum(r['revenue']     for r in m_econ)
        c_convs  = sum(r['convs']       for r in m_econ)
        c_impres = sum(r['impressions'] for r in m_econ)
        c_supp   = sum(r['supporting']  for r in m_econ)
        c_gp     = c_rev - c_spend
        clusters.append({
            'canonical':         canonical['_norm'],
            'cluster_size':      len(members),
            'cluster_members':   '; '.join(sorted(members)[:10]) +
                                 (f" (...+{len(members)-10})" if len(members) > 10 else ''),
            'c_spend':           c_spend,
            'c_revenue':         c_rev,
            'c_gp':              c_gp,
            'c_roas':            c_rev / c_spend if c_spend else 0,
            'c_convs':           c_convs,
            'c_impressions':     c_impres,
            'c_supporting':      c_supp,
        })

    # Rank clusters by GP, take top k_canonicals
    clusters.sort(key=lambda x: x['c_gp'], reverse=True)
    top = clusters[:k_canonicals]

    return [_canonical_dict(c['canonical'], c['cluster_members'],
                            spend=c['c_spend'], revenue=c['c_revenue'],
                            gp=c['c_gp'], convs=c['c_convs'],
                            impressions=c['c_impressions'],
                            supporting=c['c_supporting'],
                            cluster_size=c['cluster_size'])
            for c in top]


def _canonical_dict(canonical: str, members: str,
                    spend: float, revenue: float, gp: float,
                    convs: float, impressions: int, supporting: int,
                    cluster_size: int = 1, small_cluster: bool = False) -> dict:
    return {
        'canonical_keyword': canonical,
        'cluster_members':   members,
        'cluster_size':      cluster_size,
        'small_cluster':     small_cluster,
        'cluster_spend':     round(float(spend), 2),
        'cluster_revenue':   round(float(revenue), 2),
        'cluster_gp':        round(float(gp), 2),
        'cluster_roas':      round(float(revenue / spend), 3) if spend else 0,
        'cluster_convs':     round(float(convs), 1),
        'cluster_impressions': int(impressions),
        'cluster_supporting_queries': int(supporting),
    }


# ── Sheet builders ────────────────────────────────────────────────
def build_cluster_economics(nt_df: pd.DataFrame) -> pd.DataFrame:
    """
    Every (Ad Group × Query Group) Not-Targeted cluster, with qualification flags.
    Used for transparency: shows what we considered and what was rejected.
    """
    df = nt_df.copy()
    df['gp'] = df['Revenue ($)'] - df['Spend ($)']
    grp = df.groupby(['Ad Group', 'Query Group'], as_index=False).agg(
        rows           =('Search Query',   'count'),
        impressions    =('Impressions',    'sum'),
        clicks         =('Clicks',         'sum'),
        convs          =('Conversions',    'sum'),
        spend          =('Spend ($)',      'sum'),
        revenue        =('Revenue ($)',    'sum'),
        gp             =('gp',             'sum'),
    )
    grp['roas']             = grp['revenue'] / grp['spend'].replace(0, np.nan)
    grp['meets_volume']     = (grp['convs'] >= MIN_CONVS_PER_CLUSTER) | \
                              (grp['impressions'] >= MIN_IMPRESSIONS_PER_CLUSTER)
    grp['meets_roas']       = grp['roas'] >= WATCH_ROAS
    grp['qualifies']        = grp['meets_volume'] & grp['meets_roas']
    grp['rejection_reason'] = np.where(
        grp['qualifies'], '',
        np.where(~grp['meets_volume'],
                 f'Below volume floor (<{MIN_CONVS_PER_CLUSTER} conv & <{MIN_IMPRESSIONS_PER_CLUSTER} impr)',
                 np.where(~grp['meets_roas'], f'ROAS < {WATCH_ROAS}', ''))
    )
    grp = grp.sort_values(['qualifies', 'gp'], ascending=[False, False]).reset_index(drop=True)
    for col in ['spend', 'revenue', 'gp', 'roas']:
        grp[col] = pd.to_numeric(grp[col], errors='coerce').round(2)
    return grp


def build_recommended_clusters(nt_df: pd.DataFrame,
                                cluster_econ: pd.DataFrame,
                                normalization_log: dict) -> tuple:
    """
    For each qualifying cluster, run TF-IDF clustering and pick canonical
    keyword recommendations. Returns (recommended_clusters_df, recommended_kw_df).

    Head/long-tail split: head if cluster impressions >= median of qualifying.
    """
    qualified = cluster_econ[cluster_econ['qualifies']].copy()
    if qualified.empty:
        empty = pd.DataFrame([{'Note': 'No clusters meet qualification gate'}])
        return empty, empty

    # Head/long-tail boundary
    impr_median = qualified['impressions'].median()
    qualified['cluster_class'] = np.where(
        qualified['impressions'] >= impr_median, 'head', 'long_tail')

    cluster_rows = []
    keyword_rows = []

    for _, c in qualified.iterrows():
        ag, qg = c['Ad Group'], c['Query Group']
        cluster_queries = nt_df[
            (nt_df['Ad Group'] == ag) & (nt_df['Query Group'] == qg)
        ].copy()

        k = HEAD_KW_PER_CLUSTER if c['cluster_class'] == 'head' else LONG_TAIL_KW_PER_CLUSTER
        canonicals = cluster_and_pick_canonicals(
            cluster_queries, k, normalization_log)

        cluster_row = {
            'Ad Group':              ag,
            'Query Group':           qg,
            'cluster_class':         c['cluster_class'],
            'cluster_kw_budget':     k,
            'recommendations_made':  len(canonicals),
            'cluster_rows':          int(c['rows']),
            'cluster_impressions':   int(c['impressions']),
            'cluster_convs':         round(float(c['convs']), 1),
            'cluster_spend':         round(float(c['spend']), 2),
            'cluster_revenue':       round(float(c['revenue']), 2),
            'cluster_gp':            round(float(c['gp']), 2),
            'cluster_roas':          round(float(c['roas']), 3),
        }
        cluster_rows.append(cluster_row)

        for rank, canon in enumerate(canonicals, 1):
            keyword_rows.append({
                'Ad Group':         ag,
                'Query Group':      qg,
                'cluster_class':    c['cluster_class'],
                'kw_rank_in_cluster': rank,
                **canon,
            })

    rec_clusters_df = pd.DataFrame(cluster_rows)
    rec_kw_df       = pd.DataFrame(keyword_rows)
    if not rec_kw_df.empty:
        rec_kw_df = rec_kw_df.sort_values(
            ['Ad Group', 'cluster_gp', 'kw_rank_in_cluster'],
            ascending=[True, False, True]).reset_index(drop=True)
    return rec_clusters_df, rec_kw_df


def build_ag_capacity_rollup(rec_kw_df: pd.DataFrame) -> pd.DataFrame:
    """Per-ad-group keyword count + tier classification."""
    if rec_kw_df.empty or 'Ad Group' not in rec_kw_df.columns:
        return pd.DataFrame([{'Note': 'No recommendations to roll up'}])

    ag = rec_kw_df.groupby('Ad Group').agg(
        recommended_kw_count =('canonical_keyword',     'count'),
        distinct_query_groups=('Query Group',           'nunique'),
        total_cluster_gp     =('cluster_gp',            'sum'),
        total_cluster_spend  =('cluster_spend',         'sum'),
        total_cluster_revenue=('cluster_revenue',       'sum'),
    ).reset_index()

    def tier(n):
        if n <= AG_TIER_A_MAX:
            return f'A: ≤{AG_TIER_A_MAX} — fits current AG (baseline)'
        if n <= AG_TIER_B_MAX:
            return f'B: {AG_TIER_A_MAX+1}-{AG_TIER_B_MAX} — expanded AG, validate ad copy'
        return f'C: >{AG_TIER_B_MAX} — split into new AG(s) by Query Group'

    ag['capacity_tier'] = ag['recommended_kw_count'].apply(tier)
    ag['split_recommendation'] = np.where(
        ag['recommended_kw_count'] > AG_TIER_B_MAX,
        'Yes — split by Query Group into new ad groups',
        np.where(ag['recommended_kw_count'] > AG_TIER_A_MAX,
                 'Optional — split by Query Group if ad copy themes diverge',
                 'No'))
    for col in ['total_cluster_gp', 'total_cluster_spend', 'total_cluster_revenue']:
        ag[col] = pd.to_numeric(ag[col], errors='coerce').round(2)
    return ag.sort_values('total_cluster_gp', ascending=False).reset_index(drop=True)


def build_non_brand_recommended(rec_clusters_df: pd.DataFrame,
                                 rec_kw_df: pd.DataFrame,
                                 nt_df: pd.DataFrame) -> tuple:
    """
    Subset recommendations to non-brand only and apply +20% RPL uplift.
    Returns (non_brand_clusters, non_brand_sizing_detail).
    """
    if rec_clusters_df.empty or 'Query Group' not in rec_clusters_df.columns:
        empty = pd.DataFrame([{'Note': 'No recommendations to subset'}])
        return empty, empty

    # Non-brand at the cluster level: Query Group does not contain brand/carrier.
    # The brand-empty constraint already ran during NT filtering since the
    # input data has Brand pre-sanitized; here we just confirm at QG level.
    rec_clusters_df = rec_clusters_df.copy()
    rec_clusters_df['is_non_brand_qg'] = rec_clusters_df['Query Group'].apply(
        is_non_brand_query_group)

    # For each non-brand cluster, recompute economics from the underlying
    # NT rows after restricting to brand-empty queries (the second filter step).
    detail_rows = []
    for _, c in rec_clusters_df[rec_clusters_df['is_non_brand_qg']].iterrows():
        ag, qg = c['Ad Group'], c['Query Group']
        sub = nt_df[
            (nt_df['Ad Group'] == ag) &
            (nt_df['Query Group'] == qg) &
            nt_df.apply(is_non_brand_row, axis=1)
        ]
        if sub.empty:
            continue
        spend       = sub['Spend ($)'].sum()
        revenue     = sub['Revenue ($)'].sum()
        gp_current  = revenue - spend
        revenue_up  = revenue * NON_BRAND_RPL_UPLIFT
        gp_uplifted = revenue_up - spend
        incremental = gp_uplifted - gp_current
        detail_rows.append({
            'Ad Group':                  ag,
            'Query Group':                qg,
            'cluster_class':              c['cluster_class'],
            'rows_in_subset':             len(sub),
            'spend':                      round(float(spend), 2),
            'current_revenue':            round(float(revenue), 2),
            'current_gp':                 round(float(gp_current), 2),
            'current_roas':               round(float(revenue/spend), 3) if spend else 0,
            'uplifted_revenue (+20%)':    round(float(revenue_up), 2),
            'uplifted_gp':                round(float(gp_uplifted), 2),
            'uplifted_roas':              round(float(revenue_up/spend), 3) if spend else 0,
            'incremental_gp':             round(float(incremental), 2),
        })

    if not detail_rows:
        return (pd.DataFrame([{'Note': 'No qualifying non-brand clusters'}]),
                pd.DataFrame([{'Note': 'No qualifying non-brand clusters'}]))

    detail_df = pd.DataFrame(detail_rows).sort_values(
        'incremental_gp', ascending=False).reset_index(drop=True)

    # The non-brand recommended-keywords sheet: filter the keyword-level recs
    nb_kw = rec_kw_df[rec_kw_df['Query Group'].apply(is_non_brand_query_group)] \
              .copy() if not rec_kw_df.empty else pd.DataFrame()

    return nb_kw, detail_df


def _apply_expansion_uplift(spend: float, revenue: float,
                             cpc_efficiency: float, ctr_lift: float) -> dict:
    """
    Apply Case 1's two-lever uplift model.

    Mechanism (no volume loss — broad keywords stay live; expansion is additive):
      new_clicks  = old_clicks  × (1 + ctr_lift)
      new_spend   = old_spend   × (1 + ctr_lift) × (1 - cpc_efficiency)
      new_revenue = old_revenue × (1 + ctr_lift)
      new_GP      = new_revenue - new_spend

    More clicks at a lower per-click price (better QS), with revenue scaling
    linearly with clicks (assumes conv-rate-per-click holds). The (1 + ctr_lift)
    × (1 - cpc_efficiency) coefficient on spend can land below 1 (efficiency
    wins) or above 1 (CTR lift draws more spend than CPC saves) depending on
    the mix.
    """
    new_revenue = revenue * (1 + ctr_lift)
    new_spend   = spend   * (1 + ctr_lift) * (1 - cpc_efficiency)
    new_gp      = new_revenue - new_spend
    cur_gp      = revenue - spend
    return {
        'new_spend':   new_spend,
        'new_revenue': new_revenue,
        'new_gp':      new_gp,
        'lift_gp':     new_gp - cur_gp,
        'new_roas':    new_revenue / new_spend if new_spend > 0 else 0,
    }


def build_sizing_summary(rec_kw_df: pd.DataFrame,
                          nb_detail: pd.DataFrame,
                          nt_df: pd.DataFrame) -> pd.DataFrame:
    """
    Unified sizing schema for both initiatives. Columns:

      Initiative | Scenario | GP/Month | Current GP/Day | Expected GP Lift
                 |          |          |                | (GP/day)         | (GP/month)

    "Current GP/Day" is the BASELINE state for that initiative — it's the
    same value on every row within a case. "Expected GP Lift" is the
    incremental from baseline; zero on the current-state row.
    """
    rows = []

    def _row(initiative, scenario, current_gp_day,
             gp_month, lift_day, lift_month, note=''):
        rows.append({
            'Initiative':            initiative,
            'Scenario':              scenario,
            'GP / Month ($)':        round(float(gp_month), 2),
            'Current GP / Day ($)':  round(float(current_gp_day), 2),
            'Expected GP Lift / Day ($)':   round(float(lift_day), 2),
            'Expected GP Lift / Month ($)': round(float(lift_month), 2),
            'Note':                  note,
        })

    # ─── CASE 1: Keyword Expansion ─────────────────────────────────
    case1_baseline = None
    if not rec_kw_df.empty and 'cluster_gp' in rec_kw_df.columns:
        unique_clusters = rec_kw_df.drop_duplicates(['Ad Group', 'Query Group'])
        c1_spend  = float(unique_clusters['cluster_spend'].sum())
        c1_rev    = float(unique_clusters['cluster_revenue'].sum())
        c1_gp     = c1_rev - c1_spend
        case1_baseline = (c1_spend, c1_rev, c1_gp)
        c1_gp_day = c1_gp / 30

        # Current state (baseline)
        _row('CASE 1: Keyword Expansion',
             'Current state (queries currently flowing through broad/phrase)',
             current_gp_day=c1_gp_day,
             gp_month=c1_gp,
             lift_day=0, lift_month=0,
             note=f'Baseline. {len(rec_kw_df):,} recommended keywords across '
                  f'{len(unique_clusters)} clusters in '
                  f'{unique_clusters["Ad Group"].nunique()} ad groups. '
                  f'Volume held constant — broad/phrase keywords stay live.')

        # Headline scenario: -5% CPC + +10% CTR
        u = _apply_expansion_uplift(c1_spend, c1_rev,
                                    EXPANSION_CPC_EFFICIENCY,
                                    EXPANSION_CTR_LIFT)
        cpc_pct = round(EXPANSION_CPC_EFFICIENCY*100)
        ctr_pct = round(EXPANSION_CTR_LIFT*100)
        _row('CASE 1: Keyword Expansion',
             f'HEADLINE: −{cpc_pct}% CPC, +{ctr_pct}% CTR  '
             f'[HYPOTHESIS — no historical reference]',
             current_gp_day=c1_gp_day,
             gp_month=u['new_gp'],
             lift_day=u['lift_gp']/30,
             lift_month=u['lift_gp'],
             note=f'Mechanism: better QS → CPC down; themed ad copy → CTR up. '
                  f'Volume held constant. New ROAS: {u["new_roas"]:.2f}.')

        # Sensitivity grid for Case 1: vary CPC efficiency and CTR lift
        sensitivity = [
            (0.05, 0.05,  'conservative (−5% CPC, +5% CTR)'),
            (0.05, 0.15,  'higher CTR (−5% CPC, +15% CTR)'),
            (0.10, 0.05,  'higher CPC efficiency (−10% CPC, +5% CTR)'),
            (0.10, 0.10,  'balanced (−10% CPC, +10% CTR)'),
            (0.15, 0.10,  'aggressive CPC (−15% CPC, +10% CTR)'),
            (0.15, 0.15,  'aggressive both (−15% CPC, +15% CTR)'),
        ]
        for cpc_e, ctr_l, label in sensitivity:
            u = _apply_expansion_uplift(c1_spend, c1_rev, cpc_e, ctr_l)
            _row('CASE 1: Keyword Expansion',
                 f'  └ sensitivity: {label}',
                 current_gp_day=c1_gp_day,
                 gp_month=u['new_gp'],
                 lift_day=u['lift_gp']/30,
                 lift_month=u['lift_gp'],
                 note=f'New ROAS: {u["new_roas"]:.2f}')

    # ─── CASE 2: Non-Brand CID Split ───────────────────────────────
    if not nb_detail.empty and 'incremental_gp' in nb_detail.columns:
        c2_spend  = float(nb_detail['spend'].sum())
        c2_cur_rev = float(nb_detail['current_revenue'].sum())
        c2_cur_gp  = c2_cur_rev - c2_spend
        c2_gp_day  = c2_cur_gp / 30

        # Current state (baseline)
        _row('CASE 2: Non-Brand CID Split',
             'Current state (non-brand clusters, current CID)',
             current_gp_day=c2_gp_day,
             gp_month=c2_cur_gp,
             lift_day=0, lift_month=0,
             note=f'Baseline. {len(nb_detail)} non-brand clusters in '
                  f'{nb_detail["Ad Group"].nunique()} ad groups.')

        # Headline +20% RPL uplift
        up_rev_headline = c2_cur_rev * NON_BRAND_RPL_UPLIFT
        up_gp_headline  = up_rev_headline - c2_spend
        inc_gp_headline = up_gp_headline - c2_cur_gp
        _row('CASE 2: Non-Brand CID Split',
             f'HEADLINE: +{round((NON_BRAND_RPL_UPLIFT-1)*100)}% RPL uplift  '
             f'[HYPOTHESIS — separate non-brand CID]',
             current_gp_day=c2_gp_day,
             gp_month=up_gp_headline,
             lift_day=inc_gp_headline/30,
             lift_month=inc_gp_headline,
             note=f'Revenue × {NON_BRAND_RPL_UPLIFT}, cost held constant. '
                  f'New ROAS: {up_rev_headline/c2_spend:.2f}.')

        # Sensitivity for Case 2
        for uplift_pct in (1.10, 1.15, 1.25, 1.30):
            up_rev = c2_cur_rev * uplift_pct
            up_gp  = up_rev - c2_spend
            inc_gp = up_gp - c2_cur_gp
            _row('CASE 2: Non-Brand CID Split',
                 f'  └ sensitivity: +{round((uplift_pct-1)*100)}% RPL uplift',
                 current_gp_day=c2_gp_day,
                 gp_month=up_gp,
                 lift_day=inc_gp/30,
                 lift_month=inc_gp,
                 note=f'New ROAS: {up_rev/c2_spend:.2f}')

    return pd.DataFrame(rows)


def build_metadata_sheet(input_file: Path, n_input_rows: int,
                         n_nt_rows: int, n_qualifying: int,
                         n_recommendations: int) -> pd.DataFrame:
    rows = [
        ('Run timestamp',          datetime.now().strftime('%Y-%m-%d %H:%M:%S')),
        ('Input workbook',         str(input_file)),
        ('Input sheet',            INPUT_SHEET),
        ('Total input rows',       f'{n_input_rows:,}'),
        ('"Not Targeted" rows',    f'{n_nt_rows:,}'),
        ('Qualifying clusters',    f'{n_qualifying:,}'),
        ('Recommended keywords',   f'{n_recommendations:,}'),
        ('', ''),
        ('Volume gate',            f'≥{MIN_CONVS_PER_CLUSTER} conv OR '
                                   f'≥{MIN_IMPRESSIONS_PER_CLUSTER} impr'),
        ('ROAS gate',              f'≥{WATCH_ROAS}'),
        ('Per-cluster KW budget',  f'head={HEAD_KW_PER_CLUSTER}, '
                                   f'long_tail={LONG_TAIL_KW_PER_CLUSTER}'),
        ('AG capacity tiers',      f'A: ≤{AG_TIER_A_MAX} | '
                                   f'B: {AG_TIER_A_MAX+1}-{AG_TIER_B_MAX} | '
                                   f'C: >{AG_TIER_B_MAX}'),
        ('Head/long-tail boundary', 'cluster impressions ≥ median of qualifying'),
        ('', ''),
        ('Case 1 — Keyword Expansion uplift (HYPOTHESIS)',
         f'CPC efficiency: −{round(EXPANSION_CPC_EFFICIENCY*100)}% | '
         f'CTR lift: +{round(EXPANSION_CTR_LIFT*100)}% | '
         f'Volume held constant. No historical reference for assumption — '
         f'see sensitivity grid in Sizing Summary.'),
        ('Non-brand filter',
         'Query Group does not contain "brand"/"carrier" AND query Brand field empty'),
        ('Non-brand RPL uplift (HYPOTHESIS)',
         f'×{NON_BRAND_RPL_UPLIFT} (i.e. +{round((NON_BRAND_RPL_UPLIFT-1)*100)}%) — '
         f'see sensitivity grid in Sizing Summary.'),
        ('', ''),
        ('TF-IDF ngram range',     str(TFIDF_NGRAM_RANGE)),
        ('Neighbor K',             str(NEIGHBOR_K)),
        ('L2 distance threshold',  str(L2_DISTANCE_MAX)),
        ('Min cluster for TF-IDF', str(MIN_CLUSTER_QUERIES_FOR_TFIDF)),
    ]
    return pd.DataFrame(rows, columns=['Parameter', 'Value'])


# ── Workbook formatting ───────────────────────────────────────────
def format_workbook(writer):
    for sheet_name in writer.sheets:
        ws = writer.sheets[sheet_name]
        ws.freeze_panes = 'A2'
        for col_cells in ws.columns:
            length = max((len(str(c.value)) for c in col_cells if c.value is not None),
                         default=10)
            ws.column_dimensions[col_cells[0].column_letter].width = min(length + 2, 50)


# ── Main ──────────────────────────────────────────────────────────
def main():
    t0 = time.time()
    parser = argparse.ArgumentParser()
    parser.add_argument('--input',  default=str(INPUT_FILE),
                        help=f'Input workbook (default: {INPUT_FILE})')
    parser.add_argument('--output', default=str(OUTPUT_FILE),
                        help=f'Output workbook (default: {OUTPUT_FILE})')
    parser.add_argument('--initiative',
                        choices=['expansion', 'nonbrand', 'all'], default='all',
                        help='Which initiative to size. Default: all')
    args = parser.parse_args()

    input_path  = Path(args.input)
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"\n=== Keyword Expansion + Non-Brand CID Sizing ===")
    print(f"Input:  {input_path}")
    print(f"Output: {output_path}")
    print(f"Initiative: {args.initiative}\n")

    # Read input
    print(f"Reading {INPUT_SHEET}...")
    df = pd.read_excel(input_path, sheet_name=INPUT_SHEET)
    print(f"  Loaded {len(df):,} SQ rows")

    # Restrict to Not Targeted
    nt = df[df['Targeting Status'] == 'Not Targeted'].copy()
    print(f"  Not Targeted rows: {len(nt):,}")

    # Cluster economics (every AG × QG, qualifying or not)
    print("\nComputing (Ad Group × Query Group) cluster economics...")
    cluster_econ = build_cluster_economics(nt)
    n_qualifying = int(cluster_econ['qualifies'].sum()) \
        if 'qualifies' in cluster_econ.columns else 0
    print(f"  Total clusters: {len(cluster_econ):,}")
    print(f"  Qualifying:     {n_qualifying:,}")

    # Recommendations (TF-IDF clustering on each qualifying cluster)
    normalization_log = {}
    print("\nGenerating canonical keyword recommendations...")
    rec_clusters, rec_kws = build_recommended_clusters(
        nt, cluster_econ, normalization_log)
    n_recs = len(rec_kws) if 'canonical_keyword' in rec_kws.columns else 0
    print(f"  Recommended keywords: {n_recs:,}")

    # AG capacity rollup
    ag_rollup = build_ag_capacity_rollup(rec_kws) if n_recs else \
                pd.DataFrame([{'Note': 'No recommendations'}])

    # Non-Brand subset + uplift
    nb_kws, nb_detail = build_non_brand_recommended(rec_clusters, rec_kws, nt)
    print(f"  Non-brand qualifying clusters: "
          f"{len(nb_detail) if 'incremental_gp' in nb_detail.columns else 0}")

    # Sizing summary + metadata
    sizing = build_sizing_summary(rec_kws, nb_detail, nt)
    metadata = build_metadata_sheet(
        input_path, len(df), len(nt), n_qualifying, n_recs)

    # Vocab normalization log
    if normalization_log:
        norm_df = pd.DataFrame(
            sorted(normalization_log.items(), key=lambda x: x[0]),
            columns=['Original query', 'Normalized form'])
    else:
        norm_df = pd.DataFrame([{'Original query': '', 'Normalized form': ''}])

    # Filter outputs based on --initiative selection
    sheets = {'Metadata': metadata, 'Sizing Summary': sizing}
    if args.initiative in ('expansion', 'all'):
        sheets.update({
            'Cluster Economics':       cluster_econ,
            'Recommended Clusters':    rec_clusters,
            'Recommended Keywords':    rec_kws,
            'AG Capacity Rollup':      ag_rollup,
        })
    if args.initiative in ('nonbrand', 'all'):
        sheets.update({
            'Non-Brand Recommended Keywords': nb_kws if not nb_kws.empty else
                pd.DataFrame([{'Note': 'No non-brand recommendations'}]),
            'Non-Brand Sizing Detail':  nb_detail,
        })
    sheets['Vocab Normalization Log'] = norm_df

    print(f"\nWriting {output_path}...")
    with pd.ExcelWriter(output_path, engine='openpyxl') as writer:
        for name, frame in sheets.items():
            frame.to_excel(writer, sheet_name=name, index=False)
        format_workbook(writer)

    sz_kb = output_path.stat().st_size / 1024
    print(f"Done. Size: {sz_kb:.1f} KB  |  Wall time: {time.time()-t0:.1f}s\n")


if __name__ == '__main__':
    main()
