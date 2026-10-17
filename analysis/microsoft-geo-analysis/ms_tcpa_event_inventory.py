"""
ms_tcpa_event_inventory.py  (v2) — Deliverable 0 of the Microsoft Home
tCPA Elasticity & Lag-Profile Study. Read-only; no account changes.

WHAT CHANGED FROM v1
  - The change log (campaign_change_events, reloaded through the 9/21
    export) is the ONLY event source. The working-memory history is gone;
    known time-of-day facts survive as TIMING overrides.
  - Scope = live SEARCH campaigns only. Audience campaigns (name token
    _AUD_) are excluded by name; performance comes only from
    campaign_performance_daily, which is search-only by design. No
    audience-network table is read anywhere in this script.
  - Network regime per event: 'hybrid' (campaign still served audience
    under the same tCPA) vs 'search_only' (T2 from 7/9, all from 8/14).
    No pre or post window is allowed to span a regime change — the pre
    window is clipped to start at the regime boundary instead.
  - Coverage comes from --log-through (the export date), not from the
    last logged change.
  - Detector matching is direction-aware; each event gets a grade.
  - v2.1: DKI removal (7/14, never restored) is a permanent BOUNDARY like
    the audience exclusions — no window spans it — not a confound window.

DAY-0 RULE
  The raw ledger holds dates only, no times. Day 0 = change date + 1 for
  every event; the change date itself is a transition day in neither
  window. TIMING records the confirmed time of day where known.

WINDOWS
  pre  = day0-14 .. day0-2, clipped at a regime boundary or a prior event
  post = day0+2 .. day0+16, capped at 9/17 and the day before the next
         same-campaign event
  (Charter: pre t-14..t-1, post t+3..t+17 with t = change date. Same
  calendar days — expressed here from day 0 = t+1.)

OUTPUTS (--out-dir)
  event_inventory.csv       one row per logged tCPA event, graded
  detector_candidates.csv   every CPC step, with its matched event (if any)

USAGE
  python ms_tcpa_event_inventory.py            (DKI removal defaults to 7/14)
"""

import argparse
import re
import sqlite3
import sys
import warnings
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

ACCOUNT_ID = 2
DEFAULT_DB_PATH = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")
DEFAULT_OUT_DIR = Path(r"C:\Projects\google-ads-ops\analysis\output\ms_tcpa_study")
STUDY_START = date(2026, 6, 1)
STUDY_END = date(2026, 9, 17)       # hard cap: keyword expansion launched 9/18
DEFAULT_LOG_THROUGH = date(2026, 9, 21)

KNOWN_IDS = {"T1": 531494235, "T2": 531494261, "T3": 531494273,
             "T5": 531494275, "CA": 531494232, "FL": 531494260}

# Confirmed time of day: (campaign_key or '*', change_date) -> label
TIMING = {
    ("T3", date(2026, 7, 8)): "end of day (confirmed)",
    ("T2", date(2026, 7, 15)): "mid-day (confirmed)",
    ("*", date(2026, 8, 20)): "late end of day (confirmed)",
}

# First day each campaign's search ran WITHOUT audience under its tCPA.
AUDIENCE_OFF = {"T2": date(2026, 7, 9)}
AUDIENCE_OFF_ALL = date(2026, 8, 15)   # excluded during 8/14 -> 8/15 = first full day
DEFAULT_DKI_REMOVED = date(2026, 7, 14)  # removed, never restored -> permanent boundary


def boundaries(key: str, dki_removed: date) -> list:
    """Permanent level shifts no window may span, as (first_full_day, label).
    The day before each is a transition day, excluded from both sides."""
    b = [(dki_removed + timedelta(days=1), "DKI removal (permanent)")]
    # A campaign that dropped audience early (T2, 7/9) has no network change
    # at the account-wide date, so only its own date is a boundary.
    if key in AUDIENCE_OFF:
        b.append((AUDIENCE_OFF[key], f"{key} audience excluded"))
    else:
        b.append((AUDIENCE_OFF_ALL, "audience excluded account-wide"))
    return sorted(b)


def build_confounds():
    # Transient contamination only. Permanent shifts (DKI removal, audience
    # exclusions) are window BOUNDARIES — see boundaries().
    return [  # (start, end, keys or None, label)
        (date(2026, 7, 24), date(2026, 7, 25), ["T1", "T2"],
         "unexplained T1/T2 CPC jump 7/24-25 (cause TBD)"),
        (date(2026, 8, 28), date(2026, 9, 8), ["T1", "T2"],
         "Geico comparison ad disapproved ~8/28-9/8"),
        (date(2026, 9, 10), STUDY_END, None, "USAA account negative 9/10"),
    ]


def campaign_key(name: str) -> str:
    up = name.upper()
    m = re.search(r"TIER\s*(\d+)", up)
    if m:
        return f"T{m.group(1)}"
    for st, ab in (("CALIFORNIA", "CA"), ("FLORIDA", "FL")):
        if st in up:
            return ab
    return "".join(c for c in up if c.isalnum())[:10] or "CAMP"


def is_audience(name: str) -> bool:
    return "_AUD_" in name.upper()


def regime_start(key: str, day: date) -> date | None:
    """Start of the network regime in force on `day` (None = hybrid era)."""
    if day >= AUDIENCE_OFF_ALL:
        return min(AUDIENCE_OFF.get(key, AUDIENCE_OFF_ALL), AUDIENCE_OFF_ALL)
    if key in AUDIENCE_OFF and day >= AUDIENCE_OFF[key]:
        return AUDIENCE_OFF[key]
    return None


def cols(conn, table):
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]


# ═══════════════════════════════════════════════════════════════════
# DATA
# ═══════════════════════════════════════════════════════════════════

def fetch_daily(conn) -> pd.DataFrame:
    """SEARCH-ONLY daily series (campaign_performance_daily, device='ALL')."""
    c = cols(conn, "campaign_performance_daily")
    dcol = "date" if "date" in c else "report_date"
    df = pd.read_sql(f"""
        SELECT cpd.campaign_id, c.campaign_name, cpd.{dcol} AS dt,
               SUM(cpd.cost_micros)/1e6 AS spend, SUM(cpd.clicks) AS clicks,
               SUM(cpd.conversions) AS leads
        FROM campaign_performance_daily cpd
        JOIN campaigns c ON c.campaign_id = cpd.campaign_id
        WHERE c.account_id = ? AND cpd.device = 'ALL'
          AND cpd.{dcol} BETWEEN ? AND ?
        GROUP BY 1, 2, 3""", conn, params=(
        ACCOUNT_ID, (STUDY_START - timedelta(days=21)).isoformat(),
        STUDY_END.isoformat()))
    df["campaign_id"] = pd.to_numeric(df["campaign_id"], errors="coerce").astype("Int64")
    df["dt"] = pd.to_datetime(df["dt"]).dt.date
    return df[~df["campaign_name"].map(is_audience)]


def live_search_campaigns(daily: pd.DataFrame) -> pd.DataFrame:
    """Campaigns with search spend in the last 28 days of the study."""
    recent = daily[(daily["dt"] > STUDY_END - timedelta(days=28)) & (daily["spend"] > 0)]
    live = recent[["campaign_id", "campaign_name"]].drop_duplicates("campaign_id")
    live["campaign_key"] = live["campaign_name"].map(campaign_key)
    return live


def fetch_log(conn) -> pd.DataFrame:
    need = {"campaign_id", "platform", "account_id", "change_date",
            "change_type", "old_value", "new_value"}
    miss = need - set(cols(conn, "campaign_change_events"))
    if miss:
        sys.exit(f"ERROR: campaign_change_events missing {sorted(miss)}")
    df = pd.read_sql("""
        SELECT campaign_id, change_date, change_type, old_value, new_value
        FROM campaign_change_events
        WHERE LOWER(platform)='microsoft' AND account_id=?
          AND UPPER(change_type) LIKE '%CPA%'
          AND substr(change_date,1,10) BETWEEN ? AND ?""",
        conn, params=(ACCOUNT_ID, STUDY_START.isoformat(), STUDY_END.isoformat()))
    df["campaign_id"] = pd.to_numeric(df["campaign_id"], errors="coerce").astype("Int64")
    df["change_date"] = pd.to_datetime(df["change_date"]).dt.date
    for c in ("old_value", "new_value"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


def fetch_renames(conn) -> dict:
    try:
        rows = conn.execute("""
            SELECT h.campaign_id, h.valid_from FROM campaign_name_history h
            JOIN campaigns c ON c.campaign_id = h.campaign_id
            WHERE c.account_id = ? ORDER BY 1, 2""", (ACCOUNT_ID,)).fetchall()
    except sqlite3.Error:
        print("  ⚠ campaign_name_history unavailable — no consolidation flags")
        return {}
    by: dict = {}
    for cid, vf in rows:
        if vf:
            by.setdefault(int(cid), []).append(pd.to_datetime(vf).date())
    return {cid: sorted(v)[1:] for cid, v in by.items() if len(v) > 1}


# ═══════════════════════════════════════════════════════════════════
# DETECTOR (unchanged method: adjacent 7-day blocks, DOW-balanced)
# ═══════════════════════════════════════════════════════════════════

def detect_steps(daily, z_thresh, min_clicks, sep_days=5):
    out = []
    for cid, g in daily.groupby("campaign_id"):
        g = g.set_index("dt").sort_index()
        idx = pd.date_range(g.index.min(), g.index.max(), freq="D").date
        g = g.reindex(idx).fillna({"spend": 0, "clicks": 0, "leads": 0})
        name = g["campaign_name"].dropna().iloc[0]
        ok = g["clicks"] >= min_clicks
        if ok.sum() < 21:
            continue                      # too sparse for a CPC reading
        lc = pd.Series(np.where(ok, np.log(g["spend"].where(ok) / g["clicks"].where(ok)),
                                np.nan), index=idx)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            resid = lc - lc.rolling(15, center=True, min_periods=7).median()
            sigma = 1.4826 * np.nanmedian(np.abs(resid - np.nanmedian(resid)))
        if not np.isfinite(sigma) or sigma <= 0:
            continue
        rows = []
        for i in range(7, len(idx) - 6):
            pre, post = lc.iloc[i - 7:i], lc.iloc[i:i + 7]
            n1, n2 = pre.notna().sum(), post.notna().sum()
            if n1 < 5 or n2 < 5:
                continue
            diff = post.mean() - pre.mean()
            sp0, sp1 = g["spend"].iloc[i - 7:i].sum(), g["spend"].iloc[i:i + 7].sum()
            ck0, ck1 = g["clicks"].iloc[i - 7:i].sum(), g["clicks"].iloc[i:i + 7].sum()
            rows.append({"campaign_id": int(cid), "campaign": name,
                         "campaign_key": campaign_key(name), "step_date": idx[i],
                         "z": diff / (sigma * np.sqrt(1 / n1 + 1 / n2)),
                         "cpc_step_pct": np.exp(diff) - 1,
                         "spend_step_pct": sp1 / sp0 - 1 if sp0 > 0 else np.nan,
                         "clicks_step_pct": ck1 / ck0 - 1 if ck0 > 0 else np.nan})
        if not rows:
            continue
        s = pd.DataFrame(rows)
        s = s[s["z"].abs() >= z_thresh].sort_values("z", key=np.abs, ascending=False)
        kept = []
        for _, r in s.iterrows():
            if all(abs((r["step_date"] - k["step_date"]).days) > sep_days for k in kept):
                kept.append(r)
        out.extend(kept)
    cand = pd.DataFrame(out)
    if cand.empty:
        return cand
    cand = cand[(cand["step_date"] >= STUDY_START) & (cand["step_date"] <= STUDY_END)]
    cand = cand.sort_values("step_date").reset_index(drop=True)
    cand["co_movers"] = [
        ", ".join(sorted(set(cand[(cand["step_date"].map(lambda x: abs((x - r["step_date"]).days) <= 3))
                                  & (np.sign(cand["z"]) == np.sign(r["z"]))
                                  & (cand["campaign_id"] != r["campaign_id"])]["campaign_key"])))
        for _, r in cand.iterrows()]
    cand["matched_event"] = ""
    return cand


# ═══════════════════════════════════════════════════════════════════
# INVENTORY
# ═══════════════════════════════════════════════════════════════════

def build_inventory(log, live, cand, renames, confounds, min_post_days, dki_removed):
    name_of = dict(zip(live["campaign_id"], live["campaign_name"]))
    key_of = dict(zip(live["campaign_id"], live["campaign_key"]))
    for k, cid in KNOWN_IDS.items():
        if key_of.get(cid) != k:
            print(f"  ⚠ tripwire: {k} expected id {cid}, resolved key "
                  f"{key_of.get(cid, 'NOT LIVE')}")

    ev = log[log["campaign_id"].isin(list(name_of))].copy()
    dropped = log[~log["campaign_id"].isin(list(name_of))]
    ev["campaign_key"] = ev["campaign_id"].map(key_of)
    ev["campaign"] = ev["campaign_id"].map(name_of)
    ev["day0"] = ev["change_date"].map(lambda x: x + timedelta(days=1))
    ev["timing"] = [TIMING.get((k, d), TIMING.get(("*", d), "unknown (log date only)"))
                    for k, d in zip(ev["campaign_key"], ev["change_date"])]
    ev["tcpa_change_pct"] = ev["new_value"] / ev["old_value"] - 1
    ev = ev.rename(columns={"old_value": "tcpa_from", "new_value": "tcpa_to"})
    ev = ev.sort_values(["campaign_id", "day0"]).reset_index(drop=True)

    recs = []
    for _, e in ev.iterrows():
        d0, key, cid = e["day0"], e["campaign_key"], int(e["campaign_id"])
        same = ev[ev["campaign_id"] == cid]["day0"]
        nxt = [x for x in same if x > d0]
        prv = [x for x in same if x < d0]
        regime = "search_only" if regime_start(key, e["change_date"]) else "hybrid"

        pre_s, pre_e = d0 - timedelta(days=14), d0 - timedelta(days=2)
        post_s = d0 + timedelta(days=2)
        notes = []
        bnds = boundaries(key, dki_removed)
        # Pre window starts no earlier than the latest boundary before the change.
        prior_b = [(b, lbl) for b, lbl in bnds if b <= e["change_date"]]
        if prior_b and prior_b[-1][0] > pre_s:
            pre_s = prior_b[-1][0]
            notes.append(f"pre clipped at {prior_b[-1][1]} ({pre_s})")
        # Post window ends before the next boundary's transition day.
        post_cap = [d0 + timedelta(days=16), STUDY_END]
        if nxt:
            post_cap.append(min(nxt) - timedelta(days=2))   # stop before next transition
        next_b = [(b, lbl) for b, lbl in bnds if b > e["change_date"]]
        if next_b:
            post_cap.append(next_b[0][0] - timedelta(days=2))
        post_e = min(post_cap)
        if nxt and min(nxt) - timedelta(days=2) < d0 + timedelta(days=16):
            notes.append(f"post truncated by next event (day0 {min(nxt)})")
        if next_b and next_b[0][0] - timedelta(days=2) < d0 + timedelta(days=16):
            notes.append(f"post truncated at {next_b[0][1]} ({next_b[0][0]})")
        if prv and max(prv) >= pre_s:
            pre_s = max(prv) + timedelta(days=2)
            notes.append(f"pre clipped after prior event (day0 {max(prv)})")

        flags = []
        for cs, ce, keys, label in confounds:
            if keys and key not in keys:
                continue
            if cs <= post_e and ce >= post_s:
                flags.append(f"[post] {label}")
            elif cs <= pre_e and ce >= pre_s:
                flags.append(f"[pre] {label}")

        # Grade
        consolidation = any(abs((rd - e["change_date"]).days) <= 1
                            for rd in renames.get(cid, []))
        post_days = max((post_e - post_s).days + 1, 0)
        pre_days = max((pre_e - pre_s).days + 1, 0)
        if pd.isna(e["tcpa_from"]):
            grade, why = "EXCLUDED", "initial target (no prior value)"
        elif consolidation:
            grade, why = "EXCLUDED", "same-day rename = shell consolidation (scope change)"
        elif e["tcpa_change_pct"] <= 0:
            grade, why = "EXCLUDED", "cut (study measures raises)"
        elif post_days < min_post_days or pre_days < 5:
            grade, why = "EXCLUDED", f"windows too short (pre {pre_days}d / post {post_days}d)"
        elif any(f.startswith("[post]") for f in flags) or any(f.startswith("[pre]") for f in flags):
            grade, why = "USABLE_FLAGGED", ""
        else:
            grade, why = "CLEAN", ""

        recs.append({**e.to_dict(), "regime": regime, "grade": grade,
                     "exclude_reason": why, "pre_start": pre_s, "pre_end": pre_e,
                     "pre_days": pre_days, "post_start": post_s, "post_end": post_e,
                     "post_days": post_days, "confound_flags": "; ".join(flags),
                     "window_notes": "; ".join(notes)})
    inv = pd.DataFrame(recs)

    # Direction-aware detector match: step in [day0-2, day0+4], same sign.
    inv["detector_date"], inv["detector_z"], inv["detector_cpc_step_pct"] = None, np.nan, np.nan
    for i, e in inv.iterrows():
        if cand.empty:
            break
        m = cand[(cand["campaign_id"] == e["campaign_id"])
                 & cand["step_date"].map(lambda x: -2 <= (x - e["day0"]).days <= 4)
                 & (np.sign(cand["z"]) == np.sign(e["tcpa_change_pct"]))]
        if m.empty:
            continue
        b = m.loc[m["z"].abs().idxmax()]
        inv.at[i, "detector_date"] = b["step_date"]
        inv.at[i, "detector_z"] = round(b["z"], 2)
        inv.at[i, "detector_cpc_step_pct"] = round(b["cpc_step_pct"], 4)
        cand.loc[b.name, "matched_event"] = f"{e['campaign_key']} {e['change_date']}"

    inv.insert(0, "event_id", [f"E{n:02d}" for n in range(1, len(inv) + 1)])
    order = ["event_id", "grade", "exclude_reason", "regime", "campaign_key",
             "campaign_id", "campaign", "change_date", "day0", "timing",
             "tcpa_from", "tcpa_to", "tcpa_change_pct", "pre_start", "pre_end",
             "pre_days", "post_start", "post_end", "post_days", "detector_date",
             "detector_z", "detector_cpc_step_pct", "confound_flags", "window_notes"]
    return inv[order], cand, dropped


# ═══════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════

def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db-path", default=str(DEFAULT_DB_PATH))
    p.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    p.add_argument("--z", type=float, default=2.5)
    p.add_argument("--min-clicks", type=int, default=5)
    p.add_argument("--min-post-days", type=int, default=7)
    p.add_argument("--dki-removed", default=DEFAULT_DKI_REMOVED.isoformat(),
                   help="date DKI was removed from the Costco ad group (never restored)")
    p.add_argument("--log-through", default=DEFAULT_LOG_THROUGH.isoformat(),
                   help="last date covered by the loaded change-history export")
    a = p.parse_args()

    db = Path(a.db_path)
    if not db.exists():
        sys.exit(f"ERROR: DB not found at {db}")
    out = Path(a.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    log_through = date.fromisoformat(a.log_through)
    dki_removed = date.fromisoformat(a.dki_removed)

    conn = sqlite3.connect(db)
    print("═" * 70)
    print("MS HOME tCPA EVENT INVENTORY v2 — Deliverable 0 (search network only)")
    print("═" * 70)
    daily = fetch_daily(conn)
    live = live_search_campaigns(daily)
    daily = daily[daily["campaign_id"].isin(live["campaign_id"])]
    print(f"  Live search campaigns: {', '.join(sorted(live['campaign_key']))}")
    print(f"  Daily rows (search only): {len(daily):,}")
    log = fetch_log(conn)
    print(f"  Change log covered through {log_through} (export date)"
          + ("" if log_through >= STUDY_END else "  ⚠ SHORT OF STUDY END"))
    renames = fetch_renames(conn)
    conn.close()

    cand = detect_steps(daily, a.z, a.min_clicks)
    inv, cand, dropped = build_inventory(log, live, cand, renames,
                                         build_confounds(), a.min_post_days,
                                         dki_removed)
    inv.to_csv(out / "event_inventory.csv", index=False)
    cand.to_csv(out / "detector_candidates.csv", index=False)

    print(f"  Logged tCPA events: {len(log)}  (on non-live/audience campaigns, "
          f"dropped: {len(dropped)})")
    print(f"  Permanent boundaries: DKI removed {dki_removed} (never restored), "
          f"T2 audience off {AUDIENCE_OFF['T2']}, all audience off {AUDIENCE_OFF_ALL}")
    print()
    show = inv[["event_id", "grade", "regime", "campaign_key", "change_date",
                "tcpa_from", "tcpa_to", "tcpa_change_pct", "pre_days", "post_days",
                "detector_cpc_step_pct"]].copy()
    show["tcpa_change_pct"] = show["tcpa_change_pct"].map(lambda v: f"{v:+.1%}")
    print(show.to_string(index=False))
    print()
    for _, r in inv[inv["grade"] == "EXCLUDED"].iterrows():
        print(f"  {r['event_id']} excluded: {r['exclude_reason']}")
    um = cand[cand["matched_event"] == ""] if not cand.empty else cand
    print(f"\n  CPC steps with no tCPA event: {len(um)}")
    for _, r in um.iterrows():
        cm = f"  (with {r['co_movers']})" if r["co_movers"] else ""
        print(f"    {r['step_date']}  {r['campaign_key']:<3} CPC {r['cpc_step_pct']:+.0%}  "
              f"z={r['z']:+.1f}{cm}")
    print(f"\n  ✓ {out / 'event_inventory.csv'}\n  ✓ {out / 'detector_candidates.csv'}")


if __name__ == "__main__":
    main()
