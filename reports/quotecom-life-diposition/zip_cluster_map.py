"""
zip_cluster_map.py  [v1 — date-effective zip -> CR3/CR45 cluster map, acct 29]
==================
Folder: wherever the script lives (e.g. reports\\quotecom-life-diposition\\)
Output: <script folder>\\output\\zip_cluster_map_<today>.csv
        (+ headline stats on stdout)

PURPOSE: the SQ disposition feed has no campaign dimension and Looker groups
CR3+CR45. This map assigns a lead's zip to CR3 or CR45, date-effectively,
so CR3-vs-CR45 can be split in disposition data by joining on
(zip, lead_date).

SEMANTICS (from the ratified targeting model):
  CR3  = positive zip list.  CR45 = complement (USA minus its negative list).
  A zip's class at a point in time:
    CR3-positive AND CR45-negative          -> CR3,     exclusive=1
    CR3-positive AND NOT CR45-negative      -> CR3,     exclusive=0 (SHARED —
                                               both campaigns could serve it)
    NOT CR3-positive AND CR45-negative      -> NEITHER  (CR12-only or pure
                                               exclusion; lead belongs to the
                                               CR12 group Looker isolates)
    NOT CR3-positive AND NOT CR45-negative  -> CR45,    exclusive=1

  The CSV carries explicit rows for every OBSERVED zip (targeting lists,
  CR45 exclusions, every zip served in 2026, every transition zip).
  JOIN RULE: lead zips with NO row in the CSV default to CR45, exclusive=1
  (the complement) — a LEFT JOIN with COALESCE('CR45'), never an inner join.

DATE-EFFECTIVENESS: current state from campaign_zip_targeting; history
replayed from zip_targeting_transitions (ADDED/REMOVED). As-of history
begins at the 2026-10-07 baseline — intervals starting 2026-01-01 carry
baseline_approx=1 (pre-baseline targeting is unrecoverable by design; the
baseline state is the best available mapping for Jan-Oct 6 leads).
Join: lead_date >= valid_from AND lead_date < valid_to (valid_to exclusive;
open intervals use 9999-12-31). STATUS_CHANGED transitions are ignored
(membership, not status, decides serving eligibility here).

Scope: currently-ENABLED CR3/CR45/CR12 campaigns (paused clones share the
same lists and never serve). Read-only (mode=ro). Rerun after any zip move;
regenerate the CSV rather than editing it.

RUN:
  conda activate paid-search
  python campaign_db_scripts\\zip_cluster_map.py > zip_cluster_map_run.txt 2>&1
"""

import csv
import sqlite3
from pathlib import Path
from collections import defaultdict
from datetime import date, datetime, timezone

DB_PATH    = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")
OUT_DIR    = Path(__file__).resolve().parent / "output"   # beside the script
ACCOUNT_ID = 29
COVER_FROM = "2026-01-01"
OPEN_END   = "9999-12-31"
BASELINE   = "2026-10-07"   # zip_targeting_transitions day zero


def connect():
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def cluster_ids(conn) -> dict[str, set]:
    """Currently-ENABLED campaign ids per cluster."""
    ids = {"CR3": set(), "CR45": set(), "CR12": set()}
    for r in conn.execute("""
        SELECT campaign_id, campaign_name FROM campaigns
        WHERE account_id = ? AND status = 'ENABLED'
    """, (ACCOUNT_ID,)):
        n = r["campaign_name"].upper()
        if "CR45" in n:
            ids["CR45"].add(r["campaign_id"])
        elif "CR3" in n:
            ids["CR3"].add(r["campaign_id"])
        elif "CR12" in n or "CR1" in n:
            ids["CR12"].add(r["campaign_id"])
    print("Enabled campaign ids:",
          {k: sorted(v) for k, v in ids.items()})
    return ids


def current_sets(conn, ids) -> dict[str, set]:
    """Current membership: CR3 positives, CR45 negatives, CR12 positives."""
    out = {"cr3_pos": set(), "cr45_neg": set(), "cr12_pos": set()}
    for r in conn.execute("""
        SELECT zip, campaign_id, is_negative FROM campaign_zip_targeting
        WHERE account_id = ? AND zip IS NOT NULL
    """, (ACCOUNT_ID,)):
        cid = r["campaign_id"]
        if cid in ids["CR3"] and not r["is_negative"]:
            out["cr3_pos"].add(r["zip"])
        elif cid in ids["CR45"] and r["is_negative"]:
            out["cr45_neg"].add(r["zip"])
        elif cid in ids["CR12"] and not r["is_negative"]:
            out["cr12_pos"].add(r["zip"])
    print(f"Current membership: CR3+={len(out['cr3_pos']):,}  "
          f"CR45−={len(out['cr45_neg']):,}  CR12+={len(out['cr12_pos']):,}")
    return out


def load_transitions(conn, ids) -> dict[tuple, list]:
    """(set_name, zip) -> [(observed_date, change_type)] sorted ascending.
    Sets: cr3_pos (CR3, is_negative=0), cr45_neg (CR45, is_negative=1),
    cr12_pos (CR12, is_negative=0)."""
    ev = defaultdict(list)
    n = 0
    for r in conn.execute("""
        SELECT observed_date, campaign_id, zip, is_negative, change_type
        FROM zip_targeting_transitions
        WHERE account_id = ? AND zip IS NOT NULL
          AND change_type IN ('ADDED','REMOVED')
        ORDER BY observed_date
    """, (ACCOUNT_ID,)):
        cid = r["campaign_id"]
        if cid in ids["CR3"] and not r["is_negative"]:
            key = "cr3_pos"
        elif cid in ids["CR45"] and r["is_negative"]:
            key = "cr45_neg"
        elif cid in ids["CR12"] and not r["is_negative"]:
            key = "cr12_pos"
        else:
            continue
        ev[(key, r["zip"])].append((r["observed_date"], r["change_type"]))
        n += 1
    print(f"Transitions replayed: {n:,} membership events "
          f"on {len(ev):,} (set, zip) pairs since {BASELINE}")
    return ev


def baseline_state(current_member: bool, events: list) -> bool:
    """Membership at the baseline, by reverse-replaying events from now:
    before an ADDED the zip was absent; before a REMOVED it was present."""
    state = current_member
    for _, change in reversed(events):
        state = (change == "REMOVED")
    return state


def classify(z, cr3, cr45n, cr12) -> tuple[str, int]:
    in3, inn, in12 = z in cr3, z in cr45n, z in cr12
    if in3 and inn:
        return "CR3", 1
    if in3:
        return "CR3", 0            # SHARED — CR45 not excluded
    if inn:
        return "NEITHER", 0        # CR12-only or pure exclusion
    return "CR45", 1


def build_intervals(sets, events, observed_zips) -> list[dict]:
    """Per-zip date-effective rows over COVER_FROM -> open end."""
    # per (set, zip): list of (date, member_after). Baseline membership first.
    membership_events = defaultdict(list)
    keys = {"cr3_pos": sets["cr3_pos"], "cr45_neg": sets["cr45_neg"],
            "cr12_pos": sets["cr12_pos"]}
    zip_dates = defaultdict(set)
    base_member = {}
    for key, cur in keys.items():
        touched = {z for (k, z) in events if k == key}
        for z in cur | touched:
            evs = events.get((key, z), [])
            if evs:
                b = baseline_state(z in cur, evs)
            else:
                b = z in cur
            base_member[(key, z)] = b
            for d, change in evs:
                membership_events[(key, z)].append((d, change == "ADDED"))
                zip_dates[z].add(d)

    rows = []
    for z in sorted(observed_zips):
        cuts = sorted(zip_dates.get(z, set()))
        bounds = [COVER_FROM] + cuts + [OPEN_END]
        # state trackers starting at baseline membership
        state = {k: base_member.get((k, z), z in keys[k]) for k in keys}
        for i in range(len(bounds) - 1):
            start, end = bounds[i], bounds[i + 1]
            if i > 0:  # apply events effective at this cut date
                cut = bounds[i]
                for k in keys:
                    for d, member_after in membership_events.get((k, z), []):
                        if d == cut:
                            state[k] = member_after
            cr3 = {z} if state["cr3_pos"] else set()
            cr45n = {z} if state["cr45_neg"] else set()
            cr12 = {z} if state["cr12_pos"] else set()
            cluster, excl = classify(z, cr3, cr45n, cr12)
            rows.append({"zip": z, "cluster": cluster, "exclusive_flag": excl,
                         "valid_from": start, "valid_to": end,
                         "baseline_approx": 1 if start < BASELINE else 0})
        # merge adjacent identical intervals
    merged = []
    for r in rows:
        if (merged and merged[-1]["zip"] == r["zip"]
                and merged[-1]["cluster"] == r["cluster"]
                and merged[-1]["exclusive_flag"] == r["exclusive_flag"]
                and merged[-1]["valid_to"] == r["valid_from"]):
            merged[-1]["valid_to"] = r["valid_to"]
        else:
            merged.append(r)
    return merged


def headline_stats(conn, sets, ids, rows):
    cr3_pos, cr45_neg = sets["cr3_pos"], sets["cr45_neg"]
    cr3_excl = cr3_pos & cr45_neg
    cr3_shared = cr3_pos - cr45_neg
    print(f"\nHEADLINE — zip counts (current state):")
    print(f"  CR3 targeted zips                : {len(cr3_pos):,}")
    print(f"    exclusive to CR3 (CR45-negated): {len(cr3_excl):,} "
          f"({100*len(cr3_excl)/len(cr3_pos):.1f}%)")
    print(f"    SHARED (CR45 not excluded)     : {len(cr3_shared):,}")
    if cr3_shared:
        print(f"    shared zips: {sorted(cr3_shared)[:20]}")
    print(f"  CR45 = complement (every zip not listed above / not NEITHER)")

    # click-weighted, interval-aware (month midpoint), CR3+CR45 campaigns only
    cur_class = {}
    for r in rows:   # map month to interval
        cur_class.setdefault(r["zip"], []).append(r)

    def class_at(z, d):
        for r in cur_class.get(z, []):
            if r["valid_from"] <= d < r["valid_to"]:
                return r["cluster"], r["exclusive_flag"]
        return "CR45", 1   # default rule

    ids_all = ids["CR3"] | ids["CR45"]
    ph = ",".join("?" * len(ids_all))
    clicks = conn.execute(f"""
        SELECT substr(date,1,7) m, zip, campaign_id, SUM(clicks) c
        FROM campaign_performance_zip_daily
        WHERE account_id = ? AND zip IS NOT NULL AND date >= ?
          AND campaign_id IN ({ph})
        GROUP BY 1,2,3
    """, (ACCOUNT_ID, COVER_FROM, *sorted(ids_all))).fetchall()

    tot = excl_clicks = shared_clicks = neither_clicks = 0
    match = {"CR3": [0, 0], "CR45": [0, 0]}   # cluster -> [matched, total]
    for r in clicks:
        d = r["m"] + "-15"
        cluster, excl = class_at(r["zip"], d)
        c = r["c"] or 0
        tot += c
        if cluster == "NEITHER":
            neither_clicks += c
        elif excl:
            excl_clicks += c
        else:
            shared_clicks += c
        serving = "CR3" if r["campaign_id"] in ids["CR3"] else "CR45"
        match[serving][1] += c
        if cluster == serving:
            match[serving][0] += c

    print(f"\nHEADLINE — click-weighted (2026 CR3+CR45 clicks, zip grain):")
    print(f"  Total clicks                  : {tot:,}")
    if tot:
        print(f"  In EXCLUSIVE zips             : {excl_clicks:,} "
              f"({100*excl_clicks/tot:.1f}%)")
        print(f"  In SHARED zips                : {shared_clicks:,} "
              f"({100*shared_clicks/tot:.1f}%)")
        print(f"  In NEITHER zips (CR12/excl)   : {neither_clicks:,} "
              f"({100*neither_clicks/tot:.1f}%)")
    print(f"\nVALIDATION — served campaign vs map (presence leak):")
    for k in ("CR3", "CR45"):
        m, t = match[k]
        pct = 100 * m / t if t else float("nan")
        print(f"  {k} clicks in {k}-assigned zips : {m:,} / {t:,} ({pct:.1f}%)")
    print("  (<100% = presence-based serving outside the targeted lists — "
          "the irreducible noise floor of ANY zip-based lead assignment.)")


def main():
    print("=" * 65)
    print("zip_cluster_map.py  [v1 — date-effective CR3/CR45 map]")
    print(f"Database : {DB_PATH}  (mode=ro)")
    print(f"Started  : {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}")
    print("=" * 65)

    conn = connect()
    ids = cluster_ids(conn)
    if not ids["CR3"] or not ids["CR45"]:
        raise SystemExit("[ABORT] CR3 or CR45 not found among ENABLED campaigns.")
    sets = current_sets(conn, ids)
    events = load_transitions(conn, ids)

    served = {r["zip"] for r in conn.execute("""
        SELECT DISTINCT zip FROM campaign_performance_zip_daily
        WHERE account_id = ? AND zip IS NOT NULL AND date >= ?
    """, (ACCOUNT_ID, COVER_FROM))}
    observed = (sets["cr3_pos"] | sets["cr45_neg"] | sets["cr12_pos"]
                | served | {z for (_, z) in events})
    print(f"Observed zip universe: {len(observed):,} "
          f"(served 2026: {len(served):,})")

    rows = build_intervals(sets, events, observed)
    from collections import Counter
    per_zip = Counter(r["zip"] for r in rows)
    multi = sum(1 for v in per_zip.values() if v > 1)
    print(f"Map rows: {len(rows):,} intervals over {len(per_zip):,} zips "
          f"({multi:,} zips with targeting changes since {BASELINE})")

    today = date.today().isoformat()
    out = OUT_DIR / f"zip_cluster_map_{today}.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["zip", "cluster", "exclusive_flag",
                                          "valid_from", "valid_to",
                                          "baseline_approx"])
        w.writeheader()
        w.writerows(rows)
    print(f"Map written: {out}")
    print("JOIN RULE: LEFT JOIN on zip with lead_date >= valid_from AND "
          "lead_date < valid_to; unmatched zip => cluster='CR45', exclusive=1.")

    headline_stats(conn, sets, ids, rows)
    conn.close()
    print(f"\nDone. {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}")


if __name__ == "__main__":
    main()
