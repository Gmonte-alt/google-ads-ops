"""
gads_change_common.py
Shared helpers for the Google change-history pipeline (Phase 1).

CONVENTION: this is a BARE helper (no numeric prefix), imported by name
(`import gads_change_common as C`) by scripts 14-17. Do NOT rename or number it --
that would break the imports. Matches the refresh_helper.py helper convention.

Conventions enforced here (match the Microsoft build + codebase):
  - PRAGMA busy_timeout (30s) + PRAGMA foreign_keys=ON, asserted to actually be on.
  - Single-writer lock check via BEGIN IMMEDIATE for any write path.
  - Runtime account resolution from platform_account_id (never a hardcoded id).
  - U+202F / U+00A0 normalized out of dates before strptime.

Pure stdlib. No third-party deps.
"""

import sqlite3
import datetime
import hashlib
import re

PLATFORM = "google"

# Google Home customer id as the operator gave it ("888-650-2147"); we try the
# dashed and undashed forms so we match whatever accounts.platform_account_id stores.
DEFAULT_CID = "888-650-2147"


def cid_variants(cid):
    digits = cid.replace("-", "")
    return list(dict.fromkeys([cid, digits]))  # de-dup, preserve order


# ---------------------------------------------------------------- connections
def connect(db_path, write=False, timeout_s=30):
    """Open a connection with the mandated PRAGMAs. If write=True, acquire a
    RESERVED lock immediately (BEGIN IMMEDIATE) so a second writer fails fast
    instead of corrupting a half-applied change."""
    conn = sqlite3.connect(db_path, timeout=timeout_s, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = %d" % (timeout_s * 1000))
    conn.execute("PRAGMA foreign_keys = ON")
    if conn.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        conn.close()
        raise RuntimeError("foreign_keys could not be enabled on this connection")
    if write:
        try:
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as e:
            conn.close()
            raise RuntimeError(
                "could not acquire write lock (another writer active?): %s" % e
            )
    return conn


def resolve_account_id(conn, cid=DEFAULT_CID):
    """platform_account_id -> accounts.id at runtime. Never hardcode the int."""
    vs = cid_variants(cid)
    q = "SELECT id, platform_account_id FROM accounts WHERE platform=? AND platform_account_id IN (%s)" % (
        ",".join("?" * len(vs))
    )
    rows = conn.execute(q, [PLATFORM] + vs).fetchall()
    if not rows:
        raise RuntimeError(
            "no %s account with platform_account_id in %r" % (PLATFORM, vs)
        )
    if len(rows) > 1:
        raise RuntimeError("ambiguous account match: %r" % [dict(r) for r in rows])
    return rows[0]["id"]


def table_columns(conn, table):
    return [r["name"] for r in conn.execute("PRAGMA table_info(%s)" % table)]


def table_exists(conn, table):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def backup_table(conn, table):
    """CREATE TABLE {t}_backup_{ts} AS SELECT * FROM {t}. Returns backup name."""
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    bk = "%s_backup_%s" % (table, ts)
    conn.execute("CREATE TABLE %s AS SELECT * FROM %s" % (bk, table))
    return bk


def now_iso():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------- date parsing
_NBSP = ("\u202f", "\u00a0")


def normalize_dt(raw):
    """Google UI CSV date -> ISO 'YYYY-MM-DD HH:MM:SS'.
    The narrow no-break space (U+202F) before AM/PM is the highest-probability
    silent breakage; strip it (and U+00A0) before strptime."""
    s = raw
    for ch in _NBSP:
        s = s.replace(ch, " ")
    s = s.strip()
    dt = datetime.datetime.strptime(s, "%b %d, %Y, %I:%M:%S %p")
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def row_hash(account_id, date_time_raw, campaign, ad_group, changes):
    h = hashlib.sha1()
    h.update(("%s|%s|%s|%s|%s" % (account_id, date_time_raw, campaign, ad_group, changes)).encode("utf-8"))
    return h.hexdigest()


# ----------------------------------------------------- change-event extraction
# CHANGES (included), all normalized to fraction for tROAS / dollars for budget:
RX_TROAS_PCT = re.compile(r"Target ROAS changed from (\d+)% to (\d+)%")          # campaign-level, pct old->new
RX_TROAS_FR  = re.compile(r"Target ROAS changed from (\d+\.\d+) to (\d+\.\d+)")  # portfolio, fraction old->new
RX_BUDGET    = re.compile(r"Budget amount changed from \$([\d,]+\.\d{2}) to \$([\d,]+\.\d{2})")
# CREATION anchor (null-old), only when the cell is a portfolio-strategy creation:
RX_PORT_CREATED = re.compile(r"Target ROAS portfolio strategy created")
RX_PORT_VALUE   = re.compile(r"Target ROAS:\s*(\d+\.\d+)")
# DELIBERATELY NOT EMITTED:
#   r"Target ROAS is (\d+)%"   -> STATE snapshot inside label/budget-create events (not a change)
#   r"Target ROAS was (\d+)%"  -> ad-group-grain old-only (also filtered upstream by ad_group_col)
#   "Budget changed" / "Budget name changed from" -> budget RENAME, not an amount change


def parse_campaign_events(changes_raw):
    """Return list of (change_type, old_value, new_value). old_value=None marks a
    timeline-start anchor (portfolio-strategy creation) with no 'before'.
    Caller must already have excluded ad-group-grain rows (ad_group_col non-empty)."""
    out = []
    if RX_PORT_CREATED.search(changes_raw):
        m = RX_PORT_VALUE.search(changes_raw)
        if m:
            out.append(("TARGET_ROAS", None, float(m.group(1))))
    for m in RX_TROAS_PCT.finditer(changes_raw):
        out.append(("TARGET_ROAS", int(m.group(1)) / 100.0, int(m.group(2)) / 100.0))
    for m in RX_TROAS_FR.finditer(changes_raw):
        out.append(("TARGET_ROAS", float(m.group(1)), float(m.group(2))))
    for m in RX_BUDGET.finditer(changes_raw):
        old = float(m.group(1).replace(",", ""))
        new = float(m.group(2).replace(",", ""))
        out.append(("DAILY_BUDGET", old, new))
    return out


def collapse_same_day(pairs):
    """Value-link intraday walk-ups into one net (old, new, raw_event_count).
    pairs: list of (old, new); old may be None for anchors.
    Returns (events, ambiguous) where events is a list of (old, new, raw_count)
    and ambiguous is None on success or (starts, ends) when the tune chain has no
    unique start/end (a real round-trip) -- caller warns + skips those."""
    anchors = [(o, n) for (o, n) in pairs if o is None]
    tunes = [(o, n) for (o, n) in pairs if o is not None]
    events = [(None, n, 1) for (o, n) in anchors]  # anchors never collapse
    if tunes:
        olds = [o for (o, n) in tunes]
        news = [n for (o, n) in tunes]
        starts = sorted(set(o for o in olds if o not in news))
        ends = sorted(set(n for n in news if n not in olds))
        if len(starts) == 1 and len(ends) == 1:
            events.append((starts[0], ends[0], len(tunes)))
        else:
            return events, (starts, ends)  # ambiguous tune chain
    return events, None


# ------------------------------------------------- live-constraint prechecks
def notnull_columns(conn, table):
    """NOT NULL columns of a live table, EXCLUDING the autoincrement pk
    (which sqlite fills itself). Read from the live DB -- never assumed."""
    return {r["name"] for r in conn.execute("PRAGMA table_info(%s)" % table)
            if r["notnull"] == 1 and r["pk"] == 0}


def check_rows_notnull(conn, table, insert_columns, rows):
    """Return a list of (column, n_violations) for any NOT NULL live column that
    a prepared row would write NULL into. Lets the caller print a clear [ABORT]
    in dry-run -- before any backup/delete -- instead of a raw IntegrityError
    mid-transaction. 'rows' are positional tuples aligned to 'insert_columns'."""
    nn = notnull_columns(conn, table)
    idx = {c: i for i, c in enumerate(insert_columns)}
    viol = []
    for col in nn:
        if col not in idx:
            viol.append((col, -1))  # NOT NULL live column we don't populate at all
            continue
        i = idx[col]
        bad = sum(1 for r in rows if r[i] is None)
        if bad:
            viol.append((col, bad))
    return viol


# ============================================================ API change_event
# Category is keyed STRICTLY off change_resource_type + the changed_fields path,
# never off which sub-resource is populated (parent fields auto-stamp on child
# changes and would otherwise inflate campaign events).
TROAS_CAMPAIGN_PATHS = {"campaign.target_roas.target_roas",
                        "campaign.maximize_conversion_value.target_roas"}
TROAS_STRATEGY_PATHS = {"bidding_strategy.target_roas.target_roas"}
BUDGET_PATH = "campaign_budget.amount_micros"
NAME_PATH = "campaign.name"


def parse_resource_segment_id(resource_name):
    """'customers/123/campaignBudgets/456' -> ('campaignBudgets', '456')."""
    parts = (resource_name or "").split("/")
    if len(parts) >= 2:
        return parts[-2], parts[-1]
    return None, None


def _to_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def normalize_api_record(change_resource_type, change_resource_name, changes):
    """Pure normalizer. changes = {changed_path: (old, new)} already extracted
    from the proto. Returns a list of dicts, each either:
      {'kind':'naming','campaign_id', 'old','new'}                          (rename)
      {'kind':'event','change_type','scope','scope_id','old','new'}         (tROAS/budget)
    scope in {'campaign','budget','strategy'} -> caller resolves scope_id to
    campaign(s). Anything whose change_resource_type isn't one we track returns []
    (so a keyword/ad change can't be miscategorized as a campaign event)."""
    t = (change_resource_type or "").upper()
    _, rid = parse_resource_segment_id(change_resource_name)
    out = []
    if t == "CAMPAIGN":
        for path, (old, new) in changes.items():
            if path == NAME_PATH:
                out.append({"kind": "naming", "campaign_id": rid, "old": old, "new": new})
            elif path in TROAS_CAMPAIGN_PATHS:
                out.append({"kind": "event", "change_type": "TARGET_ROAS",
                            "scope": "campaign", "scope_id": rid,
                            "old": _to_float(old), "new": _to_float(new)})
    elif t == "CAMPAIGN_BUDGET":
        for path, (old, new) in changes.items():
            if path == BUDGET_PATH:
                o = _to_float(old); n = _to_float(new)
                out.append({"kind": "event", "change_type": "DAILY_BUDGET",
                            "scope": "budget", "scope_id": rid,
                            "old": None if o is None else o / 1e6,
                            "new": None if n is None else n / 1e6})
    elif t == "BIDDING_STRATEGY":
        for path, (old, new) in changes.items():
            if path in TROAS_STRATEGY_PATHS:
                out.append({"kind": "event", "change_type": "TARGET_ROAS",
                            "scope": "strategy", "scope_id": rid,
                            "old": _to_float(old), "new": _to_float(new)})
    # else: AD_GROUP_CRITERION / AD / etc. -> not tracked -> []
    return out


def day_before(date_str):
    import datetime
    d = datetime.date.fromisoformat(date_str[:10]) - datetime.timedelta(days=1)
    return d.isoformat()


def reconstruct_name_intervals(current_name, first_seen, renames):
    """Derive-from-scratch SCD2 reconstruction (no in-place append).
    renames: list of (rename_date, old_name, new_name, source). Returns
    (intervals, warning). Zero renames -> single open baseline interval.
    Boundary convention: a rename day belongs to the NEW name -- the closing
    interval ends the day before (clean for inclusive BETWEEN resolution)."""
    floor = first_seen or "1970-01-01"
    if not renames:
        return [{"name": current_name, "valid_from": floor, "valid_to": None,
                 "source": "campaigns_seed"}], None
    rs = sorted(renames, key=lambda r: r[0])
    intervals = []
    prev_name, prev_from = rs[0][1], floor
    for (date, old, new, src) in rs:
        d = date[:10]
        intervals.append({"name": prev_name, "valid_from": prev_from,
                          "valid_to": day_before(d), "source": src})
        prev_name, prev_from = new, d
    intervals.append({"name": prev_name, "valid_from": prev_from,
                      "valid_to": None, "source": rs[-1][3]})
    warn = None
    if prev_name != current_name:
        warn = ("reconstructed latest name %r != current campaigns name %r "
                "(uncaptured rename?)" % (prev_name, current_name))
    return intervals, warn


def event_dedup_key(campaign_id, change_date, change_type, old, new):
    """Cross-source identity of a single change. row_hash dedupes within a
    source; this dedupes the SAME change captured by both the CSV and the API."""
    r = lambda v: None if v is None else round(float(v), 6)
    return (campaign_id, change_date, change_type, r(old), r(new))
