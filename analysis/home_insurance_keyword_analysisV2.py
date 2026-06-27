"""
home_insurance_keyword_analysis.py
====================================
Keyword performance analysis for:
  Account  : Home Insurance (account_id = 1)
  Campaigns: all 7 enabled NB-SEARCH campaigns (default)
  Windows  : 30 / 60 / 90 days

DATA STRATEGY
─────────────
Fully DB-driven — no CSV dependency. keyword_instances scoped to
account_id=1 is the authoritative keyword source.

OUTPUT — 12 sheets (4 per window):
  Per window (30/60/90d):
    • Keyword Analysis       — one row per keyword + ROAS bucket,
                               cannibalization pause recommendation,
                               KW Label
    • Search Query Analysis  — one row per keyword × query,
                               all tagging columns, Query Label,
                               Query Group, Targeting Status,
                               Targeting Recommendation
    • SQ Summary             — aggregated ROAS table by Query Group
    • KW ROAS Pivot          — keyword × campaign ROAS matrix

RUN
───
  conda activate paid-search
  python analysis/home_insurance_keyword_analysis.py [--end-date YYYY-MM-DD]
    [--include-paused]
"""

import re
import sqlite3
import pandas as pd
import numpy as np
from pathlib import Path
from datetime import date, timedelta

# ── Configuration ──────────────────────────────────────────────────
DB_PATH     = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")
OUTPUT_DIR  = Path(r"C:\Projects\google-ads-ops\analysis\output")
OUTPUT_FILE = OUTPUT_DIR / "home_keyword_analysis.xlsx"

ACCOUNT_ID = 1   # Home Insurance internal account_id

ENABLED_CAMPAIGN_IDS = [
    "23385188622",   # HOME INSURANCE-CALIFORNIA-NB-SEARCH-ALL-USA-STE
    "23394808297",   # HOME INSURANCE-TEXAS-NB-SEARCH-ALL-USA-STE
    "23394808300",   # HOME INSURANCE-FLORIDA-NB-SEARCH-ALL-USA-STE
    "23428389081",   # HOME INSURANCE-TIER ONE-NB-SEARCH-ALL-USA-STE
    "23496334752",   # HOME INSURANCE-TIER TWO-NB-SEARCH-ALL-USA-STE
    "23530860942",   # HOME INSURANCE-TIER THREE-NB-SEARCH-ALL-USA-STE
    "23586637678",   # HOME INSURANCE-TIER FOUR-NB-SEARCH-ALL-USA-STE
]

END_DATE       = date.today() - timedelta(days=1)
INCLUDE_PAUSED = False   # overridden by --include-paused flag

CRITICAL_ROAS = 0.50
WATCH_ROAS    = 0.70
MIN_SPEND     = 5.00

# SQ Excel row cap — full data always written to CSV alongside workbook
SQ_EXCEL_LIMIT   = 50_000
ROW_FORMAT_LIMIT = 10_000   # per-row color formatting ceiling

GEO_PATTERN = (
    r'\b(?:alabama|alaska|arizona|arkansas|california|colorado|'
    r'connecticut|delaware|florida|georgia|hawaii|idaho|illinois|'
    r'indiana|iowa|kansas|kentucky|louisiana|maine|maryland|'
    r'massachusetts|michigan|minnesota|mississippi|missouri|montana|'
    r'nebraska|nevada|new hampshire|new jersey|new mexico|new york|'
    r'north carolina|north dakota|ohio|oklahoma|oregon|pennsylvania|'
    r'rhode island|south carolina|south dakota|tennessee|texas|utah|'
    r'vermont|virginia|washington|west virginia|wisconsin|wyoming)\b'
)

# State names used for the California-city local geo group
CA_CITY_PATTERN = (
    r'\b(?:bay area|sunnyvale|elk grove|encino|pacific palisades|truckee|'
    r'sacramento|bakersfield|riverside|irvine|santa barbara|modesto|'
    r'palm springs|temecula|carlsbad|marin county|north hollywood|'
    r'rancho cucamonga|san luis obispo|big bear|lake arrowhead|'
    r'mammoth lakes|south lake tahoe|burbank|sherman oaks|'
    r'san jose|san mateo|san francisco|los angeles|san diego|'
    r'long beach|fresno|stockton|anaheim|santa ana|chula vista|'
    r'fremont|glendale|huntington beach|santa clara|garden grove|'
    r'oceanside|ontario|rancho cucamonga|oxnard|moreno valley|'
    r'fontana|santa rosa|pomona|torrance|escondido|salinas)\b'
)

HOME_TYPE_LABELS = {
    'condo': 'condo', 'mobile_home': 'mobile home',
    'beach_home': 'beach home', 'vacation_home': 'vacation home',
    'townhouse': 'townhouse', 'cabin': 'cabin', 'lake_home': 'lake home',
    'investment_property': 'investment property', 'second_home': 'second home',
    'duplex': 'duplex', 'tiny_home': 'tiny home',
}

RESTRICTED_BRAND_CATS = {
    'carrier_approved', 'carrier_restricted_partner',
    'carrier_restricted_requested', 'carrier_conditional',
}

# ── Query Group definitions ────────────────────────────────────────
# Applied in priority order — first match wins.
# Each entry: (group_label, callable(row) -> bool)
# The callable receives a row from the pre-rename dataframe (internal col names).

def _make_query_groups():
    """
    Returns the ordered list of (label, mask_fn) tuples.
    mask_fn takes a pandas Series (a row) and returns True if the row
    belongs to that group.

    Priority order matters — evaluated top to bottom, first match wins.
    """

    def q(row):
        """Lowercase query text for pattern matching."""
        return str(row.get('query_text', '') or '').lower().strip()

    def has_brand(row):
        return bool(str(row.get('brand', '') or '').strip())

    def is_unclassified(row):
        """No intent_action AND no intent_type assigned by tagging system."""
        return (
            not str(row.get('intent_action', '') or '').strip() and
            not str(row.get('intent_type',   '') or '').strip()
        )

    def word_count(row):
        return len(q(row).split())

    PRODUCT_PAT = re.compile(
        r'\b(insurance|homeowners|home insurance|homeowners insurance|home owner)\b'
    )
    STATE_PAT   = re.compile(GEO_PATTERN, re.IGNORECASE)
    CA_CITY_PAT = re.compile(CA_CITY_PATTERN, re.IGNORECASE)

    FLOOD_PAT = re.compile(
        # Flood / NFIP
        r'\b(nfip|flood|inundaci|elevation certificate|floodsimple|'
        r'rocketflood|catcoverage|cat coverage|aon edge|beyond floods|'
        r'tsunami|tfia|neptune|shore one|risk rating 2\.0|'
        r'floodplain|aseguranza de inundacion|seguro de inundaci'
        # Wind / storm
        r'|windstorm|wind storm|wind and hail|hail insurance|'
        r'hurricane insurance|tornado insurance|wind insurance|'
        r'wind damage insurance|wind mitigation|'
        r'mississippi wind pool|sc wind and hail|nc wind and hail|'
        r'south carolina wind|texas wind storm|twia|twic|'
        r'named storm|named windstorm'
        # Earthquake
        r'|earthquake insurance|earthquake authority|'
        r'earthquakeauthority|seismic insurance|'
        r'california earthquake authority|ca earthquake|'
        r'cea earthquake|cea policy'
        # General hazard / catastrophic
        r'|hazard insurance|hazard coverage|hazard policy|'
        r'catastrophic insurance|catastrophe insurance|'
        r'catastrophic homeowners|named peril|'
        r'sinkhole insurance|landslide insurance|mudslide insurance|'
        r'disaster insurance)\b',
        re.IGNORECASE
    )
    FIRE_FAIR_PAT = re.compile(
        # FAIR plan / last-resort market
        r'\b(fair act|fair plan|cal fair|calfair|california fair|ca fire|'
        r'fire hazard|fire risk|fire zone|hazard zone|zone zero|'
        r'drop california|cfp net|massachusetts.*fair|rhode island.*fair'
        # Plain fire insurance — word "fire" adjacent to insurance context
        r'|fire insurance|wildfire insurance|wild fire insurance|'
        r'brush fire insurance|forest fire insurance|'
        r'fire damage insurance|fire coverage|'
        r'insurance.*fire|fire.*insurance)\b',
        re.IGNORECASE
    )
    POLICY_DISTRESS_PAT = re.compile(
        r'\b(cancelled|canceled|dropped|non.?renewal|lapses?|lapse|'
        r'drops me|drops you|non standard|non-standard|dropped me|'
        r'dropped by|getting dropped|being dropped)\b',
        re.IGNORECASE
    )
    HIGH_RISK_PAT = re.compile(
        r'\b(high.?risk|knob and tube|dog bite|old roof|denied|'
        r"can.?t get|houses over 100|100 years|non standard)\b",
        re.IGNORECASE
    )
    COST_PAT = re.compile(
        r'\b(average|how much|annual|inflation guard|cheapest|cheapest|'
        r'cost of|how much does|price of|average cost|what.*cost|'
        r'what.*price|what.*average)\b',
        re.IGNORECASE
    )

    # ── Converting intent patterns ────────────────────────────────
    # Carrier reviews: intent_action = review AND a brand is matched.
    # Captures "kin home insurance reviews", "amica insurance reviews" etc.
    # carrier_restricted_requested rows (Safeco etc.) are included but will
    # show low ROAS in the summary — useful signal for negative candidacy.

    # Compare / best: intent_action = compare OR query contains best/compare
    # without a brand (generic vertical shopping). Excludes vs/versus queries
    # which convert at zero and are purely informational.
    COMPARE_PAT = re.compile(
        r'\b(compare|best|who has the best|top rated|highest rated)\b',
        re.IGNORECASE
    )
    VS_PAT = re.compile(r'\b(vs\.?|versus)\b', re.IGNORECASE)

    # Does + carrier + have: "does [brand] have/offer/sell/cover home insurance"
    # Appears under both intent_action=answer and intent_action=quote (Costco).
    # Unified here because the query structure and intent are identical.
    DOES_CARRIER_PAT = re.compile(
        r'\bdoes\b.{0,40}\b(insurance|home|homeowners|house|insure|cover)\b',
        re.IGNORECASE
    )

    groups = [
        # ── Flood, wind, earthquake & general hazard ──────────────
        # Covers: flood/NFIP, windstorm/hurricane/hail, earthquake/CEA,
        # and general hazard insurance (catastrophic, sinkhole, etc.)
        (
            '9. Flood, wind & hazard insurance',
            lambda row: bool(FLOOD_PAT.search(q(row)))
        ),
        # ── Fire insurance & FAIR plan ────────────────────────────
        # Covers: plain fire insurance queries, wildfire, brush fire,
        # and state last-resort FAIR plan markets (CA, TX, MA, RI etc.)
        (
            '8. Fire insurance & FAIR plan',
            lambda row: bool(FIRE_FAIR_PAT.search(q(row)))
        ),
        # ── Policy distress / cancellation ───────────────────────
        (
            '6. Policy distress / cancellation',
            lambda row: bool(POLICY_DISTRESS_PAT.search(q(row)))
        ),
        # ── High risk / hard to insure ────────────────────────────
        (
            '7. High risk / hard to insure',
            lambda row: bool(HIGH_RISK_PAT.search(q(row)))
        ),
        # ── Pure brand name (1-2 words, unclassified) ─────────────
        (
            '2. Pure brand name',
            lambda row: (
                is_unclassified(row) and
                has_brand(row) and
                word_count(row) <= 2
            )
        ),
        # ── Brand + geo (unclassified) ────────────────────────────
        (
            '3. Brand + geo',
            lambda row: (
                is_unclassified(row) and
                has_brand(row) and
                bool(STATE_PAT.search(q(row)))
            )
        ),
        # ── Brand + product name (unclassified, ≤4 words) ─────────
        (
            '1. Brand + product name',
            lambda row: (
                is_unclassified(row) and
                has_brand(row) and
                bool(PRODUCT_PAT.search(q(row))) and
                word_count(row) <= 4
            )
        ),
        # ── brand_direct — all remaining unclassified brand rows ──
        (
            '5. Brand direct (other)',
            lambda row: (
                is_unclassified(row) and
                has_brand(row)
            )
        ),
        # ── Generic homeowners (no brand, unclassified) ───────────
        (
            '4. Generic homeowners (no brand)',
            lambda row: (
                is_unclassified(row) and
                not has_brand(row) and
                bool(re.search(r'\b(homeowners|home owners|home owner|homeowner)\b',
                               q(row), re.IGNORECASE))
            )
        ),
        # ── Cost / pricing informational (no brand) ───────────────
        (
            '10. Cost / pricing informational',
            lambda row: (
                not has_brand(row) and
                bool(COST_PAT.search(q(row)))
            )
        ),
        # ── Local geo search (CA cities, no brand) ────────────────
        (
            '11. Local geo search',
            lambda row: (
                not has_brand(row) and
                bool(CA_CITY_PAT.search(q(row)))
            )
        ),
        # ── Carrier reviews ───────────────────────────────────────
        # intent_action = review AND a brand is matched.
        # "kin insurance reviews", "amica home insurance reviews" etc.
        # Priority after numbered groups so brand-only unclassified rows
        # are already captured above; this catches only tagged review rows.
        (
            '12. Carrier reviews',
            lambda row: (
                str(row.get('intent_action', '') or '').strip() == 'review' and
                has_brand(row)
            )
        ),
        # ── Compare / best ────────────────────────────────────────
        # intent_action = compare, OR query contains best/compare/who-has-best
        # without vs/versus (those convert at zero — purely informational).
        # Catches both tagged and untagged best+vertical queries.
        (
            '13. Compare / best',
            lambda row: (
                (str(row.get('intent_action', '') or '').strip() == 'compare' or
                 bool(COMPARE_PAT.search(q(row)))) and
                not bool(VS_PAT.search(q(row)))
            )
        ),
        # ── Does + carrier + have ─────────────────────────────────
        # "does geico have home insurance", "does costco offer homeowners"
        # Unified across intent_action=answer and intent_action=quote because
        # the query structure and commercial intent are identical.
        (
            '14. Does + carrier + have',
            lambda row: bool(DOES_CARRIER_PAT.search(q(row)))
        ),
    ]
    return groups


QUERY_GROUPS = _make_query_groups()


def classify_query_group(row) -> str:
    """
    Assign a Query Group label to a single SQ row.
    Evaluates QUERY_GROUPS in priority order; returns the first match.
    Falls back to 'Classified' if a tagging intent exists, or
    'Unclassified' if nothing matches and no intent is set.
    """
    for label, fn in QUERY_GROUPS:
        try:
            if fn(row):
                return label
        except Exception:
            pass

    # If the tagging system gave it an intent, group by that.
    # Note: review, compare, and answer are now caught by named groups 12-14
    # above. Only navigate, quote, checklist, and coverage-lookup remain here
    # as Classified fallbacks.
    intent_action = str(row.get('intent_action', '') or '').strip()
    if intent_action:
        action_map = {
            'quote':          'Classified: Quote intent',
            'navigate':       'Classified: Navigate (existing customer)',
            'checklist':      'Classified: Checklist / how-to',
            'coverage-lookup':'Classified: Coverage lookup',
        }
        return action_map.get(intent_action, f'Classified: {intent_action}')

    return 'Unclassified'


# ── SQ Summary builder ─────────────────────────────────────────────
def build_sq_summary(sq_sheet_df: pd.DataFrame, days: int) -> pd.DataFrame:
    """
    Aggregates the Search Query Analysis sheet (post-rename, display columns)
    into the summary table by Query Group.

    Columns: Group | Rows | Clicks | Spend ($) | Revenue ($) | ROAS
    Plus a sheet-total row at the bottom.
    """
    if sq_sheet_df.empty or 'Query Group' not in sq_sheet_df.columns:
        return pd.DataFrame([{'Note': f'No SQ data for {days}d window'}])

    df = sq_sheet_df.copy()

    # Coerce numeric columns
    for col in ['Clicks', 'Spend ($)', 'Revenue ($)']:
        df[col] = pd.to_numeric(df[col], errors='coerce').fillna(0)

    grp = (df.groupby('Query Group', as_index=False)
             .agg(
                 Rows     =('Search Query', 'count'),
                 Clicks   =('Clicks',       'sum'),
                 Spend    =('Spend ($)',     'sum'),
                 Revenue  =('Revenue ($)',   'sum'),
             ))
    grp['ROAS'] = np.where(
        grp['Spend'] > 0,
        (grp['Revenue'] / grp['Spend']).round(3),
        0.0
    )
    grp = grp.rename(columns={'Spend': 'Spend ($)', 'Revenue': 'Revenue ($)'})

    # Sort: numbered groups first (by number prefix), then classified, then unclassified
    def sort_key(label):
        m = re.match(r'^(\d+)\.', str(label))
        if m:
            return (0, int(m.group(1)), label)
        if str(label).startswith('Classified'):
            return (1, 0, label)
        return (2, 0, label)

    grp['_sort'] = grp['Query Group'].apply(sort_key)
    grp = grp.sort_values('_sort').drop(columns='_sort').reset_index(drop=True)

    # Round display columns
    grp['Spend ($)']   = grp['Spend ($)'].round(2)
    grp['Revenue ($)'] = grp['Revenue ($)'].round(2)
    grp['ROAS']        = grp['ROAS'].round(3)

    # Sheet total row
    total_spend   = df['Spend ($)'].sum()
    total_revenue = df['Revenue ($)'].sum()
    total_clicks  = df['Clicks'].sum()
    total_rows    = len(df)
    total_roas    = round(total_revenue / total_spend, 3) if total_spend > 0 else 0.0

    total_row = pd.DataFrame([{
        'Query Group': 'SHEET TOTAL',
        'Rows':        total_rows,
        'Clicks':      int(total_clicks),
        'Spend ($)':   round(total_spend, 2),
        'Revenue ($)': round(total_revenue, 2),
        'ROAS':        total_roas,
    }])

    out = pd.concat([grp, total_row], ignore_index=True)
    return out


# ── Helpers ────────────────────────────────────────────────────────
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def normalize(text: str) -> str:
    """Strip brackets/quotes and lowercase."""
    t = re.sub(r'[\[\]""\u201c\u201d]', '', str(text)).strip().lower()
    return re.sub(r'\s+', ' ', t)


def chunked_query(conn, sql_template: str, ids: list,
                  extra_params: list = None,
                  chunk_size: int = 900) -> pd.DataFrame:
    if extra_params is None:
        extra_params = []
    frames = []
    for i in range(0, max(len(ids), 1), chunk_size):
        chunk = ids[i:i + chunk_size]
        if not chunk:
            break
        ph  = ','.join('?' * len(chunk))
        sql = sql_template.replace('{placeholders}', ph)
        frames.append(pd.read_sql_query(sql, conn,
                                        params=chunk + extra_params))
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def campaign_id_filter_sql(campaign_ids: list) -> tuple:
    ph = ','.join('?' * len(campaign_ids))
    return f"IN ({ph})", list(campaign_ids)


# ── Label builders ─────────────────────────────────────────────────
def build_query_label(row) -> str:
    intent_type   = str(row.get('intent_type',        '') or '').strip()
    intent_action = str(row.get('intent_action',      '') or '').strip()
    brand         = str(row.get('brand',              '') or '').strip()
    vertical      = str(row.get('vertical',           '') or '').strip()
    home_type     = str(row.get('modifier_home_type', '') or '').strip()
    coverage      = str(row.get('modifier_coverage',  '') or '').strip()
    geo           = str(row.get('modifier_geo',       '') or '').strip()
    geo_flag      = str(row.get('geo_flag',           '') or '').strip()
    near_me       = str(row.get('near_me_flag',       '') or '').strip()
    bundle        = str(row.get('bundle_flag',        '') or '').strip()
    home_purchase = str(row.get('home_purchase_flag', '') or '').strip()
    price_flag    = str(row.get('price_flag',         '') or '').strip()
    navigate      = intent_action == 'navigate'

    parts = []
    subject = []
    if navigate:
        subject.append(f"Existing {brand.title()} customer" if brand
                       else "Existing customer")
    elif brand:
        subject.append(f"Brand-aware user researching {brand.title()}")
    else:
        subject.append("Generic shopper")

    if home_type and home_type in HOME_TYPE_LABELS:
        subject.append(f"for {HOME_TYPE_LABELS[home_type]} insurance")
    elif coverage:
        subject.append(f"for {coverage.replace('_', ' ')} coverage")
    elif bundle == 'Yes':
        subject.append("for home + auto bundle")
    elif vertical == 'home':
        subject.append("for home insurance")
    elif vertical == 'cross':
        subject.append("for multi-product insurance")
    parts.append(' '.join(subject))

    action_labels = {
        'compare':        'comparing options',
        'quote':          'seeking a price or quote',
        'answer':         'asking a specific question',
        'checklist':      'researching steps or requirements',
        'review':         'researching brand reputation',
        'coverage-lookup':'researching coverage details',
        'navigate':       'accessing account / managing policy',
    }
    if intent_action in action_labels:
        parts.append(action_labels[intent_action])

    if near_me == 'Yes':
        parts.append('near them')
    elif geo_flag == 'Yes' and geo:
        parts.append(f'in {geo.title()}')

    flags = []
    if price_flag    == 'Yes': flags.append('price-sensitive')
    if home_purchase == 'Yes': flags.append('active home buyer')
    if flags:
        parts.append(f"({', '.join(flags)})")

    return ' — '.join(p for p in parts if p)


def build_kw_label(row) -> str:
    parts    = []
    brand    = str(row.get('brand',            '') or '').strip()
    geo_pct  = float(row.get('geo_query_pct',  0) or 0)
    near_pct = float(row.get('near_me_pct',    0) or 0)
    top_q    = str(row.get('top_search_query', '') or '').strip()

    if brand:
        parts.append(f"Brand: {brand.title()}")
    if geo_pct >= 30:
        parts.append(f"Geo-heavy ({geo_pct:.0f}% state-specific queries)")
    elif geo_pct >= 10:
        parts.append(f"Geo-mixed ({geo_pct:.0f}% state-specific)")
    if near_pct >= 10:
        parts.append(f"Local-intent ({near_pct:.0f}% near-me queries)")
    if top_q:
        parts.append(f'Top query: "{top_q[:50]}"')
    return ' | '.join(parts) if parts else ''


# ── Load keyword list from DB ──────────────────────────────────────
def load_db_keywords(conn) -> pd.DataFrame:
    campaign_ids = (
        pd.read_sql_query(
            "SELECT DISTINCT campaign_id FROM campaigns WHERE account_id = ?",
            conn, params=[ACCOUNT_ID])['campaign_id'].tolist()
        if INCLUDE_PAUSED else ENABLED_CAMPAIGN_IDS
    )
    if not campaign_ids:
        return pd.DataFrame()

    in_clause, cid_params = campaign_id_filter_sql(campaign_ids)
    df = pd.read_sql_query(f"""
        SELECT
            ki.id              AS instance_id,
            ki.criterion_id,
            ki.ad_group_id,
            ki.ad_group_name,
            ki.campaign_id,
            COALESCE(c.campaign_name, ki.campaign_name) AS campaign_name,
            ki.status          AS keyword_status,
            COALESCE(c.status,  'UNKNOWN') AS campaign_status,
            COALESCE(ag.status, 'UNKNOWN') AS ad_group_status,
            ki_id.normalized_text AS keyword,
            ki_id.match_type,
            ki_id.modifier_brand  AS brand
        FROM keyword_instances ki
        JOIN keyword_identities ki_id ON ki.identity_id  = ki_id.id
        LEFT JOIN campaigns  c        ON ki.campaign_id  = c.campaign_id
        LEFT JOIN ad_groups  ag       ON ki.ad_group_id  = ag.ad_group_id
        WHERE ki.account_id  = ?
          AND ki.campaign_id {in_clause}
        ORDER BY ki.campaign_id, ki.ad_group_name, ki_id.normalized_text
    """, conn, params=[ACCOUNT_ID] + cid_params)

    df = df.drop_duplicates(subset=['campaign_id', 'criterion_id'], keep='first')
    df['kw_status'] = df['keyword_status']
    print(f"  DB keywords loaded: {len(df)} rows, "
          f"{df['ad_group_name'].nunique()} ad groups, "
          f"{df['campaign_id'].nunique()} campaigns")
    return df


# ── Fetch performance ──────────────────────────────────────────────
def fetch_keywords(conn, ki_df: pd.DataFrame, days: int) -> pd.DataFrame:
    start_date = (END_DATE - timedelta(days=days - 1)).isoformat()
    end_date   = END_DATE.isoformat()

    if ki_df.empty:
        return pd.DataFrame()

    unique_iids = ki_df['instance_id'].dropna().astype(int).unique().tolist()
    kpd_sql = """
        SELECT
            instance_id,
            COALESCE(SUM(impressions),        0)  AS impressions,
            COALESCE(SUM(clicks),             0)  AS clicks,
            COALESCE(SUM(cost_micros), 0) / 1e6   AS spend,
            COALESCE(SUM(primary_conv_count), 0)  AS conversions,
            COALESCE(SUM(primary_conv_value), 0)  AS conv_value
        FROM keyword_performance_daily
        WHERE instance_id IN ({placeholders})
          AND device      = 'ALL'
          AND report_date BETWEEN ? AND ?
        GROUP BY instance_id
    """
    kpd_df = chunked_query(conn, kpd_sql, unique_iids,
                           extra_params=[start_date, end_date])

    result = ki_df.merge(kpd_df, on='instance_id', how='left')
    for col in ['impressions', 'clicks', 'spend', 'conversions', 'conv_value']:
        result[col] = result[col].fillna(0)

    if 'campaign_status' not in result.columns: result['campaign_status'] = 'UNKNOWN'
    if 'ad_group_status' not in result.columns: result['ad_group_status'] = 'UNKNOWN'

    keep = ['ad_group_name', 'keyword', 'match_type', 'brand', 'kw_status',
            'campaign_name', 'campaign_status', 'ad_group_status', 'keyword_status',
            'criterion_id', 'instance_id', 'campaign_id',
            'impressions', 'clicks', 'spend', 'conversions', 'conv_value']
    result = result[[c for c in keep if c in result.columns]]
    result = result.sort_values(['campaign_name', 'ad_group_name', 'spend'],
                                ascending=[True, True, False]).reset_index(drop=True)
    print(f"  {len(result)} keyword rows, "
          f"{result['ad_group_name'].nunique()} ad groups, "
          f"{result['campaign_name'].nunique()} campaigns")
    return result


# ── Derived metrics ────────────────────────────────────────────────
def add_derived_metrics(df: pd.DataFrame) -> pd.DataFrame:
    d = df.copy()

    d['ctr_pct']         = np.where(d['impressions'] > 0,
                                     d['clicks'] / d['impressions'] * 100, np.nan)
    d['cpc']             = np.where(d['clicks'] > 0,
                                     d['spend'] / d['clicks'], np.nan)
    d['roas']            = np.where(d['spend'] > 0,
                                     d['conv_value'] / d['spend'], np.nan)
    d['cpl']             = np.where(d['conversions'] > 0,
                                     d['spend'] / d['conversions'], np.nan)
    d['conv_rate_pct']   = np.where(d['clicks'] > 0,
                                     d['conversions'] / d['clicks'] * 100, np.nan)
    d['value_per_click'] = np.where(d['clicks'] > 0,
                                     d['conv_value'] / d['clicks'], np.nan)

    ag = d.groupby('ad_group_name').agg(
        ag_spend      =('spend',      'sum'),
        ag_conv_value =('conv_value', 'sum'),
        ag_impressions=('impressions','sum'),
        ag_clicks     =('clicks',     'sum'),
    ).reset_index()
    ag['ag_roas'] = np.where(ag['ag_spend'] > 0,
                              ag['ag_conv_value'] / ag['ag_spend'], np.nan)
    d = d.merge(ag, on='ad_group_name', how='left')

    d['pct_ag_spend']   = np.where(d['ag_spend'] > 0,
                                    d['spend'] / d['ag_spend'] * 100, np.nan)
    d['pct_ag_revenue'] = np.where(d['ag_conv_value'] > 0,
                                    d['conv_value'] / d['ag_conv_value'] * 100, np.nan)
    d['roas_vs_ag_pct'] = np.where(
        (~pd.isna(d['roas'])) & (~pd.isna(d['ag_roas'])) & (d['ag_roas'] > 0),
        d['roas'] / d['ag_roas'] * 100, np.nan)

    def bucket(row):
        if row['spend'] < MIN_SPEND:    return 'Insufficient spend'
        if pd.isna(row['roas']):        return 'No conversions'
        if row['roas'] < CRITICAL_ROAS: return f'Critical (ROAS < {CRITICAL_ROAS})'
        if row['roas'] < WATCH_ROAS:    return f'Watch (ROAS {CRITICAL_ROAS}-{WATCH_ROAS})'
        return f'Healthy (ROAS >= {WATCH_ROAS})'

    d['roas_bucket'] = d.apply(bucket, axis=1)
    return d


# ── Cannibalization detection ──────────────────────────────────────
def detect_cannibalization(conn, kw_df: pd.DataFrame,
                           sq_detail: pd.DataFrame) -> pd.DataFrame:
    empty_cols = ['instance_id', 'pause_recommendation', 'pause_risk',
                  'covering_keyword', 'covering_match_type',
                  'covering_clicks', 'pause_rationale']

    if sq_detail.empty or kw_df.empty:
        return pd.DataFrame(columns=empty_cols)

    all_kws = pd.read_sql_query("""
        SELECT ki.id AS instance_id, ki.campaign_id,
               ki_id.normalized_text AS keyword_text,
               ki_id.match_type, ki.status
        FROM keyword_instances ki
        JOIN keyword_identities ki_id ON ki.identity_id = ki_id.id
        WHERE ki.account_id = ? AND ki.status = 'ENABLED'
    """, conn, params=[ACCOUNT_ID])

    unique_iids_all = all_kws['instance_id'].tolist()
    start_90 = (END_DATE - timedelta(days=89)).isoformat()
    end_str  = END_DATE.isoformat()

    if unique_iids_all:
        clicks_sql = """
            SELECT instance_id, COALESCE(SUM(clicks), 0) AS clicks_90d
            FROM keyword_performance_daily
            WHERE instance_id IN ({placeholders})
              AND device = 'ALL' AND report_date BETWEEN ? AND ?
            GROUP BY instance_id
        """
        clicks_df = chunked_query(conn, clicks_sql, unique_iids_all,
                                  extra_params=[start_90, end_str])
        all_kws = all_kws.merge(clicks_df, on='instance_id', how='left')
        all_kws['clicks_90d'] = all_kws['clicks_90d'].fillna(0).astype(int)
    else:
        all_kws['clicks_90d'] = 0

    crit_to_inst = (kw_df[['criterion_id', 'instance_id', 'campaign_id', 'campaign_name']]
                    .drop_duplicates(subset='criterion_id')
                    .set_index('criterion_id'))

    results = []
    for criterion_id, grp in sq_detail.groupby('criterion_id'):
        if criterion_id not in crit_to_inst.index:
            continue

        inst_row     = crit_to_inst.loc[criterion_id]
        instance_id  = int(inst_row['instance_id'])
        campaign_id  = str(inst_row['campaign_id'])
        campaign_name= str(inst_row['campaign_name'])

        if grp['conv_value'].sum() > 0:
            top_row = grp.loc[grp['conv_value'].idxmax()]
        elif grp['spend'].sum() > 0:
            top_row = grp.loc[grp['spend'].idxmax()]
        else:
            results.append(_no_pause(instance_id))
            continue

        top_query      = str(top_row['query_text'])
        top_query_norm = normalize(top_query)

        candidates = all_kws[
            (all_kws['campaign_id'] == campaign_id) &
            (all_kws['instance_id'] != instance_id) &
            (all_kws['keyword_text'] == top_query_norm)
        ]
        exact_phrase = candidates[candidates['match_type'].isin(['EXACT', 'PHRASE'])]
        broad_only   = candidates[candidates['match_type'] == 'BROAD']

        if not exact_phrase.empty:
            covering  = (exact_phrase
                         .sort_values(['match_type', 'clicks_90d'],
                                      ascending=[True, False]).iloc[0])
            rationale = (
                f"Top converting query '{top_query}' is already captured by "
                f"[{covering['match_type']}] keyword '{covering['keyword_text']}' "
                f"in campaign '{campaign_name}' with {int(covering['clicks_90d']):,} clicks "
                f"over 90 days. "
                f"The relevant question is not whether this keyword has sufficient "
                f"data to evaluate independently — it is whether pausing it creates "
                f"incremental risk. Since the covering keyword already captures this "
                f"converting query with {int(covering['clicks_90d']):,} clicks, "
                f"the downside of pausing is bounded."
            )
            results.append({
                'instance_id':         instance_id,
                'pause_recommendation':'Pause',
                'pause_risk':          'Low',
                'covering_keyword':    covering['keyword_text'],
                'covering_match_type': covering['match_type'],
                'covering_clicks':     int(covering['clicks_90d']),
                'pause_rationale':     rationale,
            })
        elif not broad_only.empty:
            covering  = broad_only.sort_values('clicks_90d', ascending=False).iloc[0]
            rationale = (
                f"Top converting query '{top_query}' is only captured by BROAD "
                f"keyword '{covering['keyword_text']}' in campaign '{campaign_name}'. "
                f"Recommend adding '{top_query}' as EXACT or PHRASE match before "
                f"pausing this keyword, to ensure the converting query remains covered "
                f"with a more precise match type."
            )
            results.append({
                'instance_id':         instance_id,
                'pause_recommendation':'Consider Pausing',
                'pause_risk':          'Medium',
                'covering_keyword':    covering['keyword_text'],
                'covering_match_type': 'BROAD',
                'covering_clicks':     int(covering['clicks_90d']),
                'pause_rationale':     rationale,
            })
        else:
            results.append(_no_pause(instance_id))

    return (pd.DataFrame(results) if results
            else pd.DataFrame(columns=empty_cols))


def _no_pause(instance_id: int) -> dict:
    return {
        'instance_id':         instance_id,
        'pause_recommendation':'',
        'pause_risk':          '',
        'covering_keyword':    '',
        'covering_match_type': '',
        'covering_clicks':     None,
        'pause_rationale':     '',
    }


# ── Targeting status ───────────────────────────────────────────────
def compute_targeting_status(sq_df: pd.DataFrame,
                              all_kw_lookup: pd.DataFrame) -> pd.Series:
    exact_by_camp  = (all_kw_lookup[all_kw_lookup['match_type'] == 'EXACT']
                      .groupby('campaign_id')['keyword_text'].apply(set).to_dict())
    phrase_by_camp = (all_kw_lookup[all_kw_lookup['match_type'] == 'PHRASE']
                      .groupby('campaign_id')['keyword_text'].apply(set).to_dict())
    broad_by_camp  = (all_kw_lookup[all_kw_lookup['match_type'] == 'BROAD']
                      .groupby('campaign_id')['keyword_text'].apply(set).to_dict())

    def status(row):
        q   = normalize(str(row.get('query_text', '') or ''))
        cid = str(row.get('campaign_id', '') or '')
        if q in exact_by_camp.get(cid,  set()): return 'Exact Match'
        if q in phrase_by_camp.get(cid, set()): return 'Phrase Match'
        if q in broad_by_camp.get(cid,  set()): return 'Broad Only'
        return 'Not Targeted'

    return sq_df.apply(status, axis=1)


# ── Targeting recommendation ───────────────────────────────────────
def targeting_recommendation(row) -> str:
    intent_action = str(row.get('intent_action',    '') or '').strip()
    brand_cat     = str(row.get('brand_category',   '') or '').strip()
    segment       = str(row.get('engagement_segment','') or '').strip()
    tgt_status    = str(row.get('targeting_status', '') or '').strip()
    impressions   = float(row.get('impressions',    0)  or 0)
    geo_flag      = str(row.get('geo_flag',         '') or '').strip()
    near_me_flag  = str(row.get('near_me_flag',     '') or '').strip()
    match_type    = str(row.get('match_type',       '') or '').strip()
    vertical      = str(row.get('vertical',         '') or '').strip()
    home_type     = str(row.get('modifier_home_type','') or '').strip()
    coverage      = str(row.get('modifier_coverage', '') or '').strip()

    if intent_action == 'navigate':
        return ('Add as Negative — navigate queries indicate existing customers, '
                'not new shoppers')
    if brand_cat in RESTRICTED_BRAND_CATS:
        if segment in ('Mediocre — Unprofitable', 'Low Engagement'):
            return 'Add as Negative — competitor brand with poor conversion history'
        if segment == 'High Engagement':
            return 'Monitor — converting competitor brand traffic; review margin'
    if (segment == 'Impressions Only' and impressions > 200
            and intent_action in ('answer', 'checklist', 'coverage-lookup')):
        topic = home_type or coverage or vertical or 'home insurance'
        return f'Content gap — create {intent_action} content for {topic}'
    if segment == 'Impressions Only' and impressions > 50:
        if tgt_status == 'Not Targeted':
            return 'Add as EXACT keyword — high impression volume with no current targeting'
        if tgt_status == 'Broad Only':
            return 'Add as PHRASE or EXACT — currently captured only by BROAD match'
    if segment == 'High Engagement':
        if geo_flag == 'Yes' and tgt_status != 'Exact Match':
            return 'Expand — create state-specific ad group or landing page'
        if near_me_flag == 'Yes':
            return 'Expand — local intent; consider zip-level targeting'
        if tgt_status == 'Broad Only':
            return 'Promote to EXACT or PHRASE — converting query only loosely targeted'
        if tgt_status in ('Exact Match', 'Phrase Match'):
            return 'Scale — increase bids on this keyword'
    if segment == 'Mediocre — Unprofitable' and match_type == 'BROAD':
        return 'Review — unprofitable BROAD; check Keyword Analysis pause recommendation'
    if segment == 'Mediocre — Unprofitable':
        return 'Review — spend without return; consider bid reduction'
    return ''


# ── Query summary (per keyword) ────────────────────────────────────
def fetch_query_summary(conn, instance_ids: list, days: int) -> pd.DataFrame:
    start_date = (END_DATE - timedelta(days=days - 1)).isoformat()
    end_date   = END_DATE.isoformat()

    if not instance_ids:
        return pd.DataFrame()

    qp_sql = """
        SELECT sqp.instance_id, sq.query_text, sqp.match_type_served,
               COALESCE(SUM(sqp.impressions),        0)  AS impressions,
               COALESCE(SUM(sqp.clicks),             0)  AS clicks,
               COALESCE(SUM(sqp.cost_micros), 0) / 1e6   AS spend,
               COALESCE(SUM(sqp.primary_conv_count), 0)  AS conversions,
               COALESCE(SUM(sqp.primary_conv_value), 0)  AS conv_value
        FROM search_query_performance sqp
        JOIN search_queries sq ON sqp.query_id = sq.id
        WHERE sqp.instance_id IN ({placeholders})
          AND sqp.device = 'ALL' AND sqp.report_date BETWEEN ? AND ?
        GROUP BY sqp.instance_id, sqp.query_id
    """
    qp = chunked_query(conn, qp_sql, instance_ids,
                       extra_params=[start_date, end_date])

    if qp.empty:
        return pd.DataFrame()

    exact_kws = pd.read_sql_query("""
        SELECT DISTINCT ki_id.normalized_text AS keyword_text
        FROM keyword_instances ki
        JOIN keyword_identities ki_id ON ki.identity_id = ki_id.id
        WHERE ki.account_id = ? AND ki_id.match_type = 'EXACT'
    """, conn, params=(ACCOUNT_ID,))
    exact_set = set(exact_kws['keyword_text'].str.lower().str.strip())

    ki_meta = chunked_query(conn, """
        SELECT ki.id AS instance_id, ki_id.normalized_text AS keyword_text,
               ki_id.match_type
        FROM keyword_instances ki
        JOIN keyword_identities ki_id ON ki.identity_id = ki_id.id
        WHERE ki.id IN ({placeholders})
    """, instance_ids)
    id_map = (ki_meta.drop_duplicates(subset='instance_id', keep='first')
              .set_index('instance_id').to_dict('index'))

    results = []
    for iid, grp in qp.groupby('instance_id'):
        row_meta   = id_map.get(iid, {})
        kw_text    = row_meta.get('keyword_text', '')
        mt         = row_meta.get('match_type', '')
        total_rev  = grp['conv_value'].sum()
        total_impr = grp['impressions'].sum()
        total_sp   = grp['spend'].sum()
        n_queries  = len(grp)

        zero_conv_qs = grp[(grp['conversions'] == 0) & (grp['spend'] > 0)]
        conv_qs      = grp[grp['conversions'] > 0]

        hhi_rev  = float(((grp['conv_value']  / total_rev)  ** 2).sum()) if total_rev  > 0 else np.nan
        hhi_impr = float(((grp['impressions'] / total_impr) ** 2).sum()) if total_impr > 0 else np.nan

        if total_rev > 0:
            top_row = grp.loc[grp['conv_value'].idxmax()]
        elif total_sp > 0:
            top_row = grp.loc[grp['spend'].idxmax()]
        else:
            top_row = grp.iloc[0]
        top_query   = top_row['query_text']
        top_rev_pct = (top_row['conv_value'] / total_rev * 100
                       if total_rev > 0 else np.nan)

        geo_mask  = grp['query_text'].str.contains(GEO_PATTERN, case=False,
                                                    regex=True, na=False)
        near_mask = grp['query_text'].str.contains(r'near me', case=False,
                                                    regex=True, na=False)
        waste_qs  = (grp[(grp['conversions'] == 0) & (grp['spend'] >= MIN_SPEND)]
                     .sort_values('spend', ascending=False)
                     .head(4)['query_text'].tolist())

        results.append({
            'instance_id':       iid,
            'zero_conv_queries': len(zero_conv_qs),
            'converting_queries':len(conv_qs),
            'zero_conv_spend':   round(zero_conv_qs['spend'].sum(), 2),
            'converting_rev':    round(conv_qs['conv_value'].sum(), 2),
            'hhi_revenue':       round(hhi_rev,  4) if not np.isnan(hhi_rev)  else np.nan,
            'hhi_impressions':   round(hhi_impr, 4) if not np.isnan(hhi_impr) else np.nan,
            'top_search_query':  top_query,
            'top_query_rev_pct': round(top_rev_pct, 1) if not np.isnan(top_rev_pct) else np.nan,
            'is_exact_match':    'Yes' if mt == 'EXACT' else 'No',
            'exact_exists':      'Yes' if normalize(kw_text) in exact_set else 'No',
            'geo_query_pct':     round(geo_mask.sum()  / n_queries * 100, 1) if n_queries > 0 else 0.0,
            'near_me_pct':       round(near_mask.sum() / n_queries * 100, 1) if n_queries > 0 else 0.0,
            'top_waste_queries': ' | '.join(waste_qs) if waste_qs else '',
        })

    return pd.DataFrame(results)


# ── Search query detail ────────────────────────────────────────────
def fetch_search_query_detail(conn, instance_ids: list, days: int) -> pd.DataFrame:
    start_date = (END_DATE - timedelta(days=days - 1)).isoformat()
    end_date   = END_DATE.isoformat()

    if not instance_ids:
        return pd.DataFrame()

    unique_iids = list(dict.fromkeys(instance_ids))
    sq_sql = """
        SELECT
            ki.criterion_id, ki.campaign_id,
            ki_id.normalized_text                AS keyword,
            ki.ad_group_name, ki_id.match_type,
            ki.status                            AS keyword_status,
            COALESCE(c.status,  'UNKNOWN')       AS campaign_status,
            COALESCE(ag.status, 'UNKNOWN')       AS ad_group_status,
            COALESCE(c.campaign_name, ki.campaign_name) AS campaign_name,
            sq.query_text,
            COALESCE(sq.vertical,            '')  AS vertical,
            COALESCE(sq.modifier_brand,      '')  AS brand,
            COALESCE(sq.modifier_geo,        '')  AS modifier_geo,
            COALESCE(sq.brand_category,      '')  AS brand_category,
            CASE WHEN sq.modifier_geo      IS NOT NULL
                  AND sq.modifier_geo      != '' THEN 'Yes' ELSE 'No' END AS geo_flag,
            CASE WHEN sq.modifier_near_me  IS NOT NULL
                  AND sq.modifier_near_me  != '' THEN 'Yes' ELSE 'No' END AS near_me_flag,
            CASE WHEN sq.modifier_price    IS NOT NULL
                  AND sq.modifier_price    != '' THEN 'Yes' ELSE 'No' END AS price_flag,
            COALESCE(sq.modifier_home_type,  '')  AS modifier_home_type,
            COALESCE(sq.modifier_coverage,   '')  AS modifier_coverage,
            CASE WHEN sq.modifier_bundle        = 1 THEN 'Yes' ELSE 'No' END AS bundle_flag,
            CASE WHEN sq.modifier_home_purchase = 1 THEN 'Yes' ELSE 'No' END AS home_purchase_flag,
            COALESCE(sq.intent_type,         '')  AS intent_type,
            COALESCE(sq.intent_action,       '')  AS intent_action,
            COALESCE(sq.tag_method,          '')  AS tag_method,
            COALESCE(sq.tag_confidence,      '')  AS tag_confidence,
            sqp.match_type_served, sqp.instance_id,
            SUM(sqp.impressions)                 AS impressions,
            SUM(sqp.clicks)                      AS clicks,
            SUM(sqp.cost_micros) / 1e6           AS spend,
            SUM(sqp.primary_conv_count)          AS conversions,
            SUM(sqp.primary_conv_value)          AS conv_value
        FROM search_query_performance sqp
        JOIN search_queries     sq    ON sqp.query_id    = sq.id
        JOIN keyword_instances  ki    ON sqp.instance_id = ki.id
        JOIN keyword_identities ki_id ON ki.identity_id  = ki_id.id
        LEFT JOIN campaigns     c     ON ki.campaign_id  = c.campaign_id
        LEFT JOIN ad_groups     ag    ON ki.ad_group_id  = ag.ad_group_id
        WHERE sqp.instance_id IN ({placeholders})
          AND sqp.device = 'ALL' AND sqp.report_date BETWEEN ? AND ?
        GROUP BY sqp.instance_id, sqp.query_id
    """
    df = chunked_query(conn, sq_sql, unique_iids,
                       extra_params=[start_date, end_date])

    if df.empty:
        return df

    df = df.drop_duplicates(subset=['instance_id', 'query_text', 'match_type_served'])

    agg_cols = {
        'impressions': 'sum', 'clicks': 'sum', 'spend': 'sum',
        'conversions': 'sum', 'conv_value': 'sum',
        'match_type_served': 'first', 'match_type': 'first',
        'vertical': 'first', 'brand': 'first', 'modifier_geo': 'first',
        'brand_category': 'first', 'geo_flag': 'first',
        'near_me_flag': 'first', 'price_flag': 'first',
        'modifier_home_type': 'first', 'modifier_coverage': 'first',
        'bundle_flag': 'first', 'home_purchase_flag': 'first',
        'intent_type': 'first', 'intent_action': 'first',
        'tag_method': 'first', 'tag_confidence': 'first',
        'keyword': 'first', 'ad_group_name': 'first',
        'keyword_status': 'first', 'campaign_status': 'first',
        'ad_group_status': 'first', 'campaign_name': 'first',
        'campaign_id': 'first', 'instance_id': 'first',
    }
    df = (df.groupby(['criterion_id', 'query_text'], as_index=False)
            .agg({k: v for k, v in agg_cols.items() if k in df.columns}))

    if 'keyword_status' in df.columns:
        df['kw_status'] = df['keyword_status']

    df = df.sort_values(['ad_group_name', 'keyword', 'spend'],
                        ascending=[True, True, False]).reset_index(drop=True)
    return df


# ── Build search query sheet ───────────────────────────────────────
def build_search_query_sheet(df: pd.DataFrame,
                              kw_df: pd.DataFrame,
                              days: int,
                              all_kw_lookup: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame([{'Note': f'No search query data for {days}d window'}])

    kw_roas = kw_df[['instance_id', 'roas']].rename(columns={'roas': 'kw_roas'})
    df = df.merge(kw_roas, on='instance_id', how='left')

    df['query_ctr']       = np.where(df['impressions'] > 0,
                                      df['clicks'] / df['impressions'] * 100, np.nan)
    df['query_cpc']       = np.where(df['clicks'] > 0,
                                      df['spend'] / df['clicks'], np.nan)
    df['query_roas']      = np.where(df['spend'] > 0,
                                      df['conv_value'] / df['spend'], np.nan)
    df['query_conv_rate'] = np.where(df['clicks'] > 0,
                                      df['conversions'] / df['clicks'] * 100, np.nan)
    df['roas_vs_kw_pct']  = np.where(
        (~pd.isna(df['query_roas'])) & (~pd.isna(df['kw_roas'])) & (df['kw_roas'] > 0),
        df['query_roas'] / df['kw_roas'] * 100, np.nan)

    # HHI at keyword level
    kw_rev_totals  = df.groupby('instance_id')['conv_value'].transform('sum')
    kw_impr_totals = df.groupby('instance_id')['impressions'].transform('sum')
    df['query_rev_share']      = np.where(kw_rev_totals  > 0,
                                           df['conv_value']  / kw_rev_totals,  np.nan)
    df['query_impr_share']     = np.where(kw_impr_totals > 0,
                                           df['impressions'] / kw_impr_totals, np.nan)
    df['hhi_rev_contribution'] = df['query_rev_share']  ** 2
    df['hhi_impr_contribution']= df['query_impr_share'] ** 2

    # HHI at ad group level
    ag_qa = (df.groupby(['ad_group_name', 'query_text'], as_index=False)
               .agg(ag_q_rev=('conv_value', 'sum'), ag_q_impr=('impressions', 'sum')))
    ag_tot = (ag_qa.groupby('ad_group_name', as_index=False)
                   .agg(ag_tot_rev=('ag_q_rev', 'sum'), ag_tot_impr=('ag_q_impr', 'sum')))
    ag_qa  = ag_qa.merge(ag_tot, on='ad_group_name', how='left')
    ag_qa['ag_query_rev_share']      = np.where(ag_qa['ag_tot_rev']  > 0,
                                                 ag_qa['ag_q_rev']  / ag_qa['ag_tot_rev'],  np.nan)
    ag_qa['ag_query_impr_share']     = np.where(ag_qa['ag_tot_impr'] > 0,
                                                 ag_qa['ag_q_impr'] / ag_qa['ag_tot_impr'], np.nan)
    ag_qa['hhi_ag_rev_contribution'] = ag_qa['ag_query_rev_share']  ** 2
    ag_qa['hhi_ag_impr_contribution']= ag_qa['ag_query_impr_share'] ** 2
    df = df.merge(ag_qa[['ad_group_name', 'query_text',
                           'ag_query_rev_share', 'ag_query_impr_share',
                           'hhi_ag_rev_contribution', 'hhi_ag_impr_contribution']],
                  on=['ad_group_name', 'query_text'], how='left')

    # Waste flag
    def waste_flag(row):
        if row['spend'] < MIN_SPEND:
            return ''
        if pd.isna(row['query_roas']) or row['query_roas'] == 0:
            return 'Waste — zero conversions'
        if row['query_roas'] < CRITICAL_ROAS:
            return f'Waste — ROAS < {CRITICAL_ROAS}'
        return ''
    df['waste_flag'] = df.apply(waste_flag, axis=1)

    # Engagement segment, targeting, and query label
    df['query_roas_for_seg'] = df['query_roas'].fillna(0)
    def eng_seg(row):
        rev   = float(row.get('conv_value',  0) or 0)
        roas_ = float(row.get('query_roas_for_seg', 0) or 0)
        clks  = float(row.get('clicks',      0) or 0)
        spend = float(row.get('spend',       0) or 0)
        impr  = float(row.get('impressions', 0) or 0)
        if rev > 0 and roas_ >= WATCH_ROAS:                                  return 'High Engagement'
        if clks > 0 and (rev == 0 or roas_ < CRITICAL_ROAS) and spend >= MIN_SPEND:
                                                                              return 'Mediocre — Unprofitable'
        if clks > 0 and spend < MIN_SPEND and rev == 0:                      return 'Low Engagement'
        if impr > 0 and clks == 0:                                           return 'Impressions Only'
        return 'Low Engagement'
    df['engagement_segment']       = df.apply(eng_seg, axis=1)
    df['targeting_status']         = compute_targeting_status(df, all_kw_lookup)
    df['targeting_recommendation'] = df.apply(targeting_recommendation, axis=1)
    df['query_label']              = df.apply(build_query_label, axis=1)

    # ── Query Group classification (applied before rename) ─────────
    df['query_group'] = df.apply(classify_query_group, axis=1)

    col_map = {
        'ad_group_name':            'Ad Group',
        'keyword':                  'Keyword',
        'match_type':               'Match Type',
        'campaign_name':            'Campaign',
        'campaign_status':          'Campaign Status',
        'ad_group_status':          'AG Status',
        'keyword_status':           'KW Status',
        'query_text':               'Search Query',
        'match_type_served':        'Match Type Served',
        'vertical':                 'Vertical',
        'brand':                    'Brand',
        'brand_category':           'Brand Category',
        'geo_flag':                 'Geo Flag',
        'near_me_flag':             'Near Me Flag',
        'price_flag':               'Price Flag',
        'modifier_home_type':       'Home Type',
        'modifier_coverage':        'Coverage Type',
        'bundle_flag':              'Bundle Flag',
        'home_purchase_flag':       'Home Purchase Flag',
        'intent_type':              'Intent Type',
        'intent_action':            'Intent Action',
        'tag_method':               'Tag Method',
        'tag_confidence':           'Tag Confidence',
        'waste_flag':               'Waste Flag',
        'query_label':              'Query Label',
        'query_group':              'Query Group',
        'targeting_status':         'Targeting Status',
        'targeting_recommendation': 'Targeting Recommendation',
        'impressions':              'Impressions',
        'clicks':                   'Clicks',
        'query_ctr':                'CTR %',
        'spend':                    'Spend ($)',
        'query_cpc':                'CPC ($)',
        'conversions':              'Conversions',
        'conv_value':               'Revenue ($)',
        'query_roas':               'Query ROAS',
        'query_conv_rate':          'Conv Rate %',
        'roas_vs_kw_pct':           'Query ROAS % of KW',
        'query_rev_share':          'Rev Share of KW',
        'query_impr_share':         'Impr Share of KW',
        'hhi_rev_contribution':     'HHI Rev Contribution (KW)',
        'hhi_impr_contribution':    'HHI Impr Contribution (KW)',
        'ag_query_rev_share':       'Rev Share of AG',
        'ag_query_impr_share':      'Impr Share of AG',
        'hhi_ag_rev_contribution':  'HHI Rev Contribution (AG)',
        'hhi_ag_impr_contribution': 'HHI Impr Contribution (AG)',
    }

    df_out = df[[c for c in col_map if c in df.columns]].rename(columns=col_map)

    rnd = {
        'CTR %': 2, 'Spend ($)': 2, 'CPC ($)': 2, 'Conversions': 1,
        'Revenue ($)': 2, 'Query ROAS': 2, 'Conv Rate %': 2,
        'Query ROAS % of KW': 1,
        'Rev Share of KW': 4,  'Impr Share of KW': 4,
        'HHI Rev Contribution (KW)': 6, 'HHI Impr Contribution (KW)': 6,
        'Rev Share of AG': 4,  'Impr Share of AG': 4,
        'HHI Rev Contribution (AG)': 6, 'HHI Impr Contribution (AG)': 6,
    }
    for col, dec in rnd.items():
        if col in df_out.columns:
            df_out[col] = pd.to_numeric(df_out[col], errors='coerce').round(dec)

    df_out = df_out.sort_values(
        ['Ad Group', 'Keyword', 'Spend ($)'], ascending=[True, True, False]
    ).reset_index(drop=True)

    return df_out


# ── Build keyword sheet ────────────────────────────────────────────
def build_sheet(df: pd.DataFrame, days: int,
                cannibal_df: pd.DataFrame = None) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame([{'Note': f'No data for {days}d window'}])

    if cannibal_df is not None and not cannibal_df.empty:
        df = df.merge(cannibal_df, on='instance_id', how='left')
        for col in ['pause_recommendation', 'pause_risk', 'covering_keyword',
                    'covering_match_type', 'pause_rationale']:
            if col in df.columns:
                df[col] = df[col].fillna('')

    df['kw_label'] = df.apply(build_kw_label, axis=1)

    col_map = {
        'ad_group_name':       'Ad Group',
        'keyword':             'Keyword',
        'match_type':          'Match Type',
        'brand':               'Brand',
        'campaign_name':       'Campaign',
        'campaign_status':     'Campaign Status',
        'ad_group_status':     'AG Status',
        'keyword_status':      'KW Status',
        'roas_bucket':         'ROAS Bucket',
        'kw_label':            'KW Label',
        'impressions':         'Impressions',
        'clicks':              'Clicks',
        'ctr_pct':             'CTR %',
        'spend':               'Spend ($)',
        'cpc':                 'CPC ($)',
        'conversions':         'Offline Convs',
        'conv_value':          'Revenue ($)',
        'roas':                'ROAS',
        'cpl':                 'CPL ($)',
        'conv_rate_pct':       'Conv Rate %',
        'value_per_click':     'Value / Click',
        'pct_ag_spend':        '% of AG Spend',
        'pct_ag_revenue':      '% of AG Revenue',
        'roas_vs_ag_pct':      'KW ROAS % of AG',
        'zero_conv_queries':   'Zero-Conv Queries',
        'converting_queries':  'Converting Queries',
        'zero_conv_spend':     'Zero-Conv Spend ($)',
        'converting_rev':      'Converting Rev ($)',
        'hhi_revenue':         'HHI (Revenue)',
        'hhi_impressions':     'HHI (Impressions)',
        'top_search_query':    'Top Search Query',
        'top_query_rev_pct':   'Top Query Rev %',
        'is_exact_match':      'Is Exact Match',
        'exact_exists':        'Exact Exists',
        'geo_query_pct':       'Geo Query %',
        'near_me_pct':         'Near Me Query %',
        'top_waste_queries':   'Top Waste Queries',
        'pause_recommendation':'Pause Recommendation',
        'pause_risk':          'Pause Risk',
        'covering_keyword':    'Covering Keyword',
        'covering_match_type': 'Covering Match Type',
        'covering_clicks':     'Covering KW Clicks (90d)',
        'pause_rationale':     'Pause Rationale',
    }

    df_out = df[[c for c in col_map if c in df.columns]].rename(columns=col_map)

    num_rounds = {
        'CTR %': 2, 'Spend ($)': 2, 'CPC ($)': 2, 'Offline Convs': 1,
        'Revenue ($)': 2, 'ROAS': 2, 'CPL ($)': 2, 'Conv Rate %': 2,
        'Value / Click': 2, '% of AG Spend': 1, '% of AG Revenue': 1,
        'KW ROAS % of AG': 1, 'Zero-Conv Spend ($)': 2, 'Converting Rev ($)': 2,
        'HHI (Revenue)': 4, 'HHI (Impressions)': 4,
        'Top Query Rev %': 1, 'Geo Query %': 1, 'Near Me Query %': 1,
    }
    for col, dec in num_rounds.items():
        if col in df_out.columns:
            df_out[col] = pd.to_numeric(df_out[col], errors='coerce').round(dec)

    result_rows = []
    for ag_name, group in df.groupby('ad_group_name', sort=True):
        ag_sp   = group['spend'].sum()
        ag_rev  = group['conv_value'].sum()
        ag_conv = group['conversions'].sum()
        ag_cl   = group['clicks'].sum()
        ag_im   = group['impressions'].sum()

        sub = {
            'Ad Group':    f'-- {ag_name} (subtotal)',
            'Keyword':     '', 'Match Type': '', 'Brand': '',
            'KW Status':   '', 'ROAS Bucket': '', 'KW Label': '',
            'Impressions': int(ag_im), 'Clicks': int(ag_cl),
            'CTR %':       round(ag_cl / ag_im * 100, 2) if ag_im > 0 else None,
            'Spend ($)':   round(ag_sp, 2),
            'CPC ($)':     round(ag_sp / ag_cl, 2)   if ag_cl > 0 else None,
            'Offline Convs': round(ag_conv, 1),
            'Revenue ($)': round(ag_rev, 2),
            'ROAS':        round(ag_rev / ag_sp, 2)  if ag_sp > 0 else None,
            'CPL ($)':     round(ag_sp / ag_conv, 2) if ag_conv > 0 else None,
            'Conv Rate %': round(ag_conv / ag_cl * 100, 2) if ag_cl > 0 else None,
            'Value / Click': round(ag_rev / ag_cl, 2) if ag_cl > 0 else None,
            '% of AG Spend':   100.0,
            '% of AG Revenue': 100.0,
            'KW ROAS % of AG': 100.0,
        }
        result_rows.append(sub)

        ag_kws = df_out[df_out['Ad Group'] == ag_name].sort_values(
            'Spend ($)', ascending=False)
        for _, row in ag_kws.iterrows():
            result_rows.append(row.to_dict())

    return pd.DataFrame(result_rows)


# ── ROAS pivot ─────────────────────────────────────────────────────
def fetch_keyword_roas_pivot(conn, ki_df: pd.DataFrame, days: int) -> pd.DataFrame:
    start_date = (END_DATE - timedelta(days=days - 1)).isoformat()
    end_date   = END_DATE.isoformat()

    unique_iids = ki_df['instance_id'].dropna().astype(int).unique().tolist()
    if not unique_iids:
        return pd.DataFrame()

    kpd_sql = """
        SELECT instance_id, campaign_id,
               COALESCE(SUM(cost_micros), 0) / 1e6  AS spend,
               COALESCE(SUM(primary_conv_value), 0)  AS revenue
        FROM keyword_performance_daily
        WHERE instance_id IN ({placeholders})
          AND device = 'ALL' AND report_date BETWEEN ? AND ?
        GROUP BY instance_id, campaign_id
    """
    kpd = chunked_query(conn, kpd_sql, unique_iids,
                        extra_params=[start_date, end_date])
    if kpd.empty:
        return pd.DataFrame()

    camp_names = pd.read_sql_query(
        "SELECT campaign_id, campaign_name FROM campaigns WHERE account_id = ?",
        conn, params=[ACCOUNT_ID])
    camp_map = dict(zip(camp_names['campaign_id'], camp_names['campaign_name']))
    kpd['campaign_name'] = kpd['campaign_id'].map(camp_map)

    active_cids = set(ki_df['campaign_id'].unique())
    kpd = kpd[kpd['campaign_id'].isin(active_cids) & kpd['campaign_name'].notna()]

    kw_meta = (ki_df[['instance_id', 'keyword', 'match_type']]
               .drop_duplicates(subset='instance_id', keep='first')
               .rename(columns={'keyword': 'keyword_text'}))
    kpd = kpd.merge(kw_meta, on='instance_id', how='left')

    kpd_agg = (kpd.groupby(['keyword_text', 'match_type', 'campaign_name'],
                             as_index=False)
                   .agg(spend=('spend', 'sum'), revenue=('revenue', 'sum')))
    kpd_agg['roas'] = (kpd_agg['revenue'] /
                       kpd_agg['spend'].replace(0, float('nan'))).round(3)

    pivot = kpd_agg.pivot_table(
        index=['keyword_text', 'match_type'],
        columns='campaign_name', values='roas', aggfunc='first'
    ).reset_index()
    pivot.columns.name = None

    totals = (kpd.groupby(['keyword_text', 'match_type'], as_index=False)
                 .agg(total_spend=('spend', 'sum'), total_revenue=('revenue', 'sum')))
    totals['Total ROAS']        = (totals['total_revenue'] /
                                   totals['total_spend'].replace(0, float('nan'))).round(3)
    totals['Total Spend ($)']   = totals['total_spend'].round(2)
    totals['Total Revenue ($)'] = totals['total_revenue'].round(2)
    pivot = pivot.merge(totals[['keyword_text', 'match_type', 'Total ROAS',
                                 'Total Spend ($)', 'Total Revenue ($)']],
                        on=['keyword_text', 'match_type'], how='left')

    def bucket(roas):
        if pd.isna(roas):               return 'No spend'
        if roas < CRITICAL_ROAS:        return f'Critical (ROAS < {CRITICAL_ROAS})'
        if roas < WATCH_ROAS:           return f'Watch (ROAS {CRITICAL_ROAS}-{WATCH_ROAS})'
        return f'Healthy (ROAS >= {WATCH_ROAS})'

    pivot['ROAS Bucket'] = pivot['Total ROAS'].apply(bucket)
    camp_cols = sorted([c for c in pivot.columns
                        if c not in ['keyword_text', 'match_type', 'ROAS Bucket',
                                     'Total ROAS', 'Total Spend ($)', 'Total Revenue ($)']])
    col_order = (['keyword_text', 'match_type', 'ROAS Bucket',
                  'Total ROAS', 'Total Spend ($)', 'Total Revenue ($)'] + camp_cols)
    pivot = pivot[[c for c in col_order if c in pivot.columns]]
    pivot = pivot.rename(columns={'keyword_text': 'Keyword', 'match_type': 'Match Type'})
    pivot = pivot.sort_values(['ROAS Bucket', 'Total Spend ($)'],
                              ascending=[True, False]).reset_index(drop=True)
    return pivot


# ── Formatting ─────────────────────────────────────────────────────
def format_workbook(writer):
    from openpyxl.styles import PatternFill, Font, Alignment, Border, Side
    from openpyxl.utils import get_column_letter

    HDR_FILL   = PatternFill("solid", fgColor="7030A0")
    HDR_FONT   = Font(bold=True, color="FFFFFF", size=10)
    SUB_FILL   = PatternFill("solid", fgColor="1F4E79")
    SUB_FONT   = Font(bold=True, color="FFFFFF", size=10)
    TOTAL_FILL = PatternFill("solid", fgColor="2E4057")   # dark slate for summary total row
    TOTAL_FONT = Font(bold=True, color="FFFFFF", size=10)
    CRIT_FILL  = PatternFill("solid", fgColor="FFCCCC")
    WATCH_FILL = PatternFill("solid", fgColor="FFF2CC")
    HLTH_FILL  = PatternFill("solid", fgColor="E2EFDA")
    PAUSE_FILL = PatternFill("solid", fgColor="FFE0CC")
    ALT_FILL   = PatternFill("solid", fgColor="F2F2F2")   # alternating rows on summary
    THIN       = Border(
        bottom=Side(style='thin', color='D9D9D9'),
        right =Side(style='thin', color='D9D9D9'),
    )

    COL_WIDTHS = {
        'Ad Group': 38, 'Keyword': 36, 'Match Type': 10, 'Brand': 12,
        'KW Status': 10, 'ROAS Bucket': 24, 'Campaign': 48,
        'Search Query': 52, 'Top Search Query': 36, 'Top Waste Queries': 60,
        'KW Label': 55, 'Query Label': 60, 'Query Group': 36,
        'Home Type': 18, 'Coverage Type': 16, 'Bundle Flag': 12,
        'Home Purchase Flag': 18, 'Intent Type': 14, 'Intent Action': 16,
        'Tag Method': 14, 'Tag Confidence': 14,
        'Targeting Status': 18, 'Targeting Recommendation': 55,
        'Pause Recommendation': 20, 'Pause Risk': 12,
        'Covering Keyword': 36, 'Covering Match Type': 18,
        'Covering KW Clicks (90d)': 22, 'Pause Rationale': 80,
    }

    SUMMARY_SHEETS = {s for s in writer.sheets if s.startswith('SQ Summary')}

    for sheet_name, ws in writer.sheets.items():
        if ws.max_row < 2:
            continue

        headers = [ws.cell(1, c).value for c in range(1, ws.max_column + 1)]

        # Header row
        for cell in ws[1]:
            cell.fill      = HDR_FILL
            cell.font      = HDR_FONT
            cell.alignment = Alignment(horizontal='center', wrap_text=True)

        # Column widths
        for i, h in enumerate(headers, 1):
            ws.column_dimensions[get_column_letter(i)].width = COL_WIDTHS.get(h, 13)

        ws.freeze_panes = 'A2'

        # ── SQ Summary sheets — special formatting ────────────────
        if sheet_name in SUMMARY_SHEETS:
            group_idx = (headers.index('Query Group') + 1
                         if 'Query Group' in headers else None)
            roas_idx  = (headers.index('ROAS') + 1
                         if 'ROAS' in headers else None)
            for i, row in enumerate(ws.iter_rows(min_row=2), start=2):
                group_val = str(row[group_idx - 1].value or '') if group_idx else ''
                is_total  = group_val == 'SHEET TOTAL'
                if is_total:
                    for cell in row:
                        cell.fill = TOTAL_FILL
                        cell.font = TOTAL_FONT
                else:
                    fill = ALT_FILL if i % 2 == 0 else None
                    if fill:
                        for cell in row: cell.fill = fill
                    # ROAS coloring on non-total rows
                    if roas_idx:
                        try:
                            roas_val = float(row[roas_idx - 1].value or 0)
                            if roas_val == 0:
                                pass
                            elif roas_val < CRITICAL_ROAS:
                                for cell in row: cell.fill = CRIT_FILL
                            elif roas_val < WATCH_ROAS:
                                for cell in row: cell.fill = WATCH_FILL
                            elif roas_val >= WATCH_ROAS:
                                for cell in row: cell.fill = HLTH_FILL
                        except (ValueError, TypeError):
                            pass
                for cell in row:
                    cell.border    = THIN
                    cell.alignment = Alignment(horizontal='left')
            continue   # done with this sheet

        # ── All other sheets ──────────────────────────────────────
        if ws.max_row > ROW_FORMAT_LIMIT:
            continue

        ag_idx    = headers.index('Ad Group')            + 1 if 'Ad Group'            in headers else None
        bucket_idx= headers.index('ROAS Bucket')         + 1 if 'ROAS Bucket'         in headers else None
        pause_idx = headers.index('Pause Recommendation')+ 1 if 'Pause Recommendation' in headers else None

        for row in ws.iter_rows(min_row=2):
            is_sub = ag_idx and str(row[ag_idx - 1].value or '').startswith('--')
            if is_sub:
                for cell in row:
                    cell.fill = SUB_FILL
                    cell.font = SUB_FONT
                continue

            if pause_idx:
                pval = str(row[pause_idx - 1].value or '')
                if pval == 'Pause':
                    for cell in row: cell.fill = PAUSE_FILL
                    continue

            if bucket_idx:
                bval = str(row[bucket_idx - 1].value or '')
                fill = (CRIT_FILL  if 'Critical' in bval else
                        WATCH_FILL if 'Watch'    in bval else
                        HLTH_FILL  if 'Healthy'  in bval else None)
                if fill:
                    for cell in row: cell.fill = fill


# ── Main ───────────────────────────────────────────────────────────
def main():
    import argparse
    parser = argparse.ArgumentParser(
        description="Home Insurance keyword analysis (DB-only, no CSV required)."
    )
    parser.add_argument('--end-date', default=None, metavar='YYYY-MM-DD',
                        help='Analysis end date (default: yesterday).')
    parser.add_argument('--include-paused', action='store_true', default=False,
                        help='Include all campaigns for account_id=1, not just '
                             'the 7 enabled ones.')
    args = parser.parse_args()

    global END_DATE, INCLUDE_PAUSED
    if args.end_date:
        END_DATE = date.fromisoformat(args.end_date)
    INCLUDE_PAUSED = args.include_paused

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    scope = ("all campaigns" if INCLUDE_PAUSED
             else f"{len(ENABLED_CAMPAIGN_IDS)} enabled campaigns")
    print("=" * 65)
    print("Home Insurance Keyword Analysis  [DB-only]")
    print(f"  Account ID : {ACCOUNT_ID}")
    print(f"  Scope      : {scope}")
    print(f"  End date   : {END_DATE}")
    print(f"  Windows    : 30 / 60 / 90 days")
    print("=" * 65)

    conn  = get_conn()
    ki_df = load_db_keywords(conn)

    if ki_df.empty:
        print("Warning: No keywords found in DB. Exiting.")
        conn.close()
        return

    print("\nLoading all-keyword targeting lookup...")
    all_kw_lookup = pd.read_sql_query("""
        SELECT ki.campaign_id,
               ki_id.normalized_text AS keyword_text,
               ki_id.match_type
        FROM keyword_instances ki
        JOIN keyword_identities ki_id ON ki.identity_id = ki_id.id
        WHERE ki.account_id = ? AND ki.status = 'ENABLED'
    """, conn, params=[ACCOUNT_ID])
    print(f"  {len(all_kw_lookup):,} enabled keyword slots loaded for targeting lookup")

    sheets      = {}
    cannibal_df = pd.DataFrame()

    for days in [30, 60, 90]:
        print(f"\n[{days}d] Fetching keyword performance...")
        raw          = fetch_keywords(conn, ki_df, days)
        with_metrics = add_derived_metrics(raw)

        instance_ids = with_metrics['instance_id'].dropna().astype(int).unique().tolist()
        q_summary    = fetch_query_summary(conn, instance_ids, days)
        if not q_summary.empty:
            with_metrics = with_metrics.merge(q_summary, on='instance_id', how='left')
            for col in ['zero_conv_queries', 'converting_queries',
                        'zero_conv_spend', 'converting_rev']:
                with_metrics[col] = with_metrics[col].fillna(0)

        print(f"  Fetching search query detail...")
        sq_raw = fetch_search_query_detail(conn, instance_ids, days)
        if not sq_raw.empty:
            print(f"    {len(sq_raw):,} query rows, "
                  f"{sq_raw['query_text'].nunique():,} unique queries")

        # Cannibalization detection — only on 90d window
        if days == 90:
            print("  Running cannibalization detection...")
            cannibal_df = detect_cannibalization(conn, ki_df, sq_raw)
            pause_count = (cannibal_df['pause_recommendation'] != '').sum()
            print(f"    {pause_count} pause recommendations generated")

        sheet = build_sheet(with_metrics, days,
                            cannibal_df=cannibal_df if days == 90 else None)

        kw_rows = sheet[~sheet['Ad Group'].str.startswith('--', na=False)]
        tot_sp  = kw_rows['Spend ($)'].sum()  if 'Spend ($)'  in kw_rows else 0
        tot_rev = kw_rows['Revenue ($)'].sum() if 'Revenue ($)' in kw_rows else 0
        print(f"  Spend: ${tot_sp:,.2f}  |  Revenue: ${tot_rev:,.2f}")
        if 'ROAS Bucket' in kw_rows.columns:
            for bkt, cnt in kw_rows['ROAS Bucket'].value_counts().items():
                print(f"    {bkt}: {cnt}")

        sheets[f'Keyword Analysis ({days}d)'] = sheet

        sq_sheet = build_search_query_sheet(sq_raw, with_metrics, days, all_kw_lookup)
        sheets[f'Search Query Analysis ({days}d)'] = sq_sheet

        # SQ Summary — built from the finished SQ sheet (post-rename)
        sq_summary = build_sq_summary(sq_sheet, days)
        sheets[f'SQ Summary ({days}d)'] = sq_summary
        print(f"  SQ Summary ({days}d): {len(sq_summary)} groups")

    # ROAS pivot sheets
    print("\nBuilding keyword ROAS pivot sheets...")
    for days in [30, 60, 90]:
        pivot_df = fetch_keyword_roas_pivot(conn, ki_df, days)
        print(f"  [{days}d] {len(pivot_df):,} keywords in pivot")
        sheets[f'KW ROAS Pivot ({days}d)'] = pivot_df

    conn.close()

    # Sheet ordering: per window — KW Analysis, SQ Analysis, SQ Summary, KW ROAS Pivot
    ordered = {}
    for d in [30, 60, 90]:
        for prefix in ['Keyword Analysis', 'Search Query Analysis',
                       'SQ Summary', 'KW ROAS Pivot']:
            key = f'{prefix} ({d}d)'
            if key in sheets:
                ordered[key] = sheets[key]
    sheets = ordered

    # Write workbook
    print(f"\nWriting: {OUTPUT_FILE}")
    with pd.ExcelWriter(OUTPUT_FILE, engine='openpyxl') as writer:
        for sheet_name, df in sheets.items():
            is_sq = 'Search Query Analysis' in sheet_name
            if is_sq and len(df) > SQ_EXCEL_LIMIT:
                sort_col = 'Spend ($)' if 'Spend ($)' in df.columns else df.columns[0]
                df_excel = (df.sort_values(sort_col, ascending=False)
                              .head(SQ_EXCEL_LIMIT).reset_index(drop=True))
                print(f"  {sheet_name}: {len(df):,} rows -> top {SQ_EXCEL_LIMIT:,} "
                      f"by spend written to Excel")
                df_excel.to_excel(writer, sheet_name=sheet_name, index=False)
                csv_out = OUTPUT_FILE.with_name(
                    OUTPUT_FILE.stem + f'__{sheet_name.replace(" ", "_")}.csv')
                df.to_csv(csv_out, index=False)
                print(f"  Full {sheet_name} -> {csv_out.name}")
            else:
                df.to_excel(writer, sheet_name=sheet_name, index=False)

        format_workbook(writer)

    print(f"Done. Size: {OUTPUT_FILE.stat().st_size / 1024:.1f} KB")
    print(f"Output: {OUTPUT_FILE}")


if __name__ == '__main__':
    main()
