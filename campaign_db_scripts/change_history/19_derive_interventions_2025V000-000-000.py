"""
19_derive_interventions_2025.py  (Google change-history -- 2025 intervention layer)

Pipeline step 19. Reads the Tier-1 staging table google_change_csv_2025_raw, decomposes
each cell into sub-change BLOCKS (change_taxonomy.split_blocks + categorize), and clusters
coordinated blocks into INTERVENTIONS -- the decision grain -- writing change_interventions_2025.

WHY: 2025 managed target ROAS at the AD-GROUP grain on single-state campaigns, so one decision
("raise everyone 120->130") is thousands of near-simultaneous ad-group rows spanning states.
Counting/plotting raw rows over-represents the old architecture. The intervention layer collapses
each coordinated sweep to one row with member_count, so 2025 is comparable to 2026 campaign-grain.

CLUSTER KEY (pinned to real data):
  (user_email, category, old_value, new_value) within a RUN-WINDOW that closes on IDLE_GAP idle.
  - old->new used WHEN PRESENT (tROAS: 'Target ROAS changed from X% to Y%' via parse_from_to);
    category-only when absent (Ad group created / Label added -> member_count = how many together).
  - clusters ACROSS campaigns/states (a sweep marches state-by-state ~2-3s apart; the whole
    cross-campaign action is ONE intervention).
  - IDLE_GAP default 120s: the densest real day (May 21: 73,700 blocks) ran 10:08:49->10:10:54
    with ZERO gaps >30s, so 120s idle safely folds a sweep while splitting a genuinely separate
    later session.

SWEEP / EXCEPTION model:
  A "sweep" = one coordinated session = (user, category, run-window) REGARDLESS of value.
  Value-variants within a sweep become SEPARATE intervention rows (honest: 120->130 and 120->125
  are different decisions) that share a sweep_id. Within a sweep, the largest-member intervention
  is the main line; the rest are is_exception=1. sweep_member_share = this row's members / sweep total.
  -> (a) every decision is its own countable row incl. exceptions; (b) visuals GROUP BY sweep_id to
     show "raised tROAS, 737 ad groups, 3 exceptions" as one event. Both, without either lying.

First pass prints a CATEGORY-COVERAGE histogram (surfaces any 2025 change types the categorizer
sends to 'other') BEFORE clustering. Streaming, progress-logged. Dry-run default; --apply writes.

Usage (run from inside change_history/ so change_taxonomy imports):
    python 19_derive_interventions_2025.py --db PATH                 # dry-run + histogram
    python 19_derive_interventions_2025.py --db PATH --apply
"""

import argparse
import collections
import datetime
import sqlite3
import sys

import change_taxonomy as T

TABLE = "change_interventions_2025"
SRC = "google_change_csv_2025_raw"
IDLE_GAP_DEFAULT = 120          # seconds of idle that closes a run-window


def parse_from_to(text):
    """old,new from Google prose. Reuses the tROAS/status/rename patterns."""
    import re
    m = re.search(r"changed from\s+(.+?)\s+to\s+(.+?)\s*$", text or "", re.I | re.M)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    m = re.search(r'named\s+"(.+?)"\s+to\s+"(.+?)"', text or "", re.I)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return "", ""


_STATE_RE = None
def parse_state(campaign_name):
    """Extract the state token from a 2025 campaign name like
    'HOME INSURANCE-NEW MEXICO-SEARCH-MOBILE-CPA-STE' -> 'NEW MEXICO'.
    Convention: the segment between the first and second '-'. Returns '' if it doesn't fit."""
    if not campaign_name:
        return ""
    parts = campaign_name.split("-")
    if len(parts) >= 2:
        return parts[1].strip()
    return ""


def iter_blocks(conn, account):
    """Yield (dt, user, campaign, ad_group, category, klass, old, new) per sub-change block,
    ordered by time so the run-window logic sees a chronological stream."""
    q = ("SELECT change_datetime, user_email, campaign_col, ad_group_col, changes_raw "
         "FROM %s WHERE account_id=? ORDER BY change_datetime, id" % SRC)
    for dt, user, camp, ag, raw in conn.execute(q, (account,)):
        for block, rec in zip(T.split_blocks(raw), T.categorize_cell(raw)):
            old, new = parse_from_to(block)
            yield dt, user or "", camp or "", ag or "", rec["category"], rec["klass"], old, new, block.split("\n")[0].strip()[:70]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--account", type=int, default=1)
    ap.add_argument("--idle-gap", type=int, default=IDLE_GAP_DEFAULT)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--progress", type=int, default=100000)
    ap.add_argument("--dump-other", action="store_true", help="print the prose behind the 'other' bucket")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    conn.execute("PRAGMA busy_timeout=30000")

    # ---- pass 1: stream blocks, histogram, and accumulate open clusters keyed by (user,cat,old,new)
    # a cluster stays open until IDLE_GAP passes with no matching block; then it's finalized.
    cat_hist = collections.Counter()
    other_samples = collections.Counter()
    open_clusters = {}        # key -> cluster dict
    finalized = []            # list of finalized cluster dicts
    n_blocks = 0
    idle = datetime.timedelta(seconds=args.idle_gap)

    def finalize_due(now):
        due = [k for k, cl in open_clusters.items() if now - cl["last"] > idle]
        for k in due:
            finalized.append(open_clusters.pop(k))

    for dt, user, camp, ag, cat, klass, old, new, head in iter_blocks(conn, args.account):
        n_blocks += 1
        cat_hist[cat] += 1
        if cat == "other":
            other_samples[head] += 1
        t = datetime.datetime.fromisoformat(dt)
        finalize_due(t)
        key = (user, cat, old, new)
        cl = open_clusters.get(key)
        if cl is None or t - cl["last"] > idle:
            if cl is not None:
                finalized.append(cl)
            cl = {"user": user, "category": cat, "klass": klass, "old": old, "new": new,
                  "start": t, "last": t, "members": 0,
                  "campaigns": set(), "ad_groups": set(), "states": set(),
                  "sample": block_sample(cat, old, new)}
            open_clusters[key] = cl
        cl["last"] = t
        cl["members"] += 1
        cl["campaigns"].add(camp)
        cl["ad_groups"].add(ag)
        st = parse_state(camp)
        if st:
            cl["states"].add(st)
        if args.progress and n_blocks % args.progress == 0:
            print("   ... %d blocks | %d open clusters | %d finalized" % (
                n_blocks, len(open_clusters), len(finalized)))
    finalized.extend(open_clusters.values())

    print("\n[ok] decomposed %d raw rows into %d sub-change blocks" % (
        conn.execute("SELECT COUNT(*) FROM %s WHERE account_id=?" % SRC, (args.account,)).fetchone()[0],
        n_blocks))
    print("[ok] %d interventions (clusters)" % len(finalized))
    print("\n=== category coverage histogram (watch for 'other') ===")
    for c, n in cat_hist.most_common():
        print("  %-22s %d" % (c, n))
    if args.dump_other and other_samples:
        print("\n=== 'other' bucket -- top prose headers (%d distinct) ===" % len(other_samples))
        for head, n in other_samples.most_common(25):
            print("  %6d  %s" % (n, head))

    # ---- assign sweep_id across value-variants of the same session ----
    # sweep = (user, category, overlapping run-window). Group finalized clusters that share
    # (user, category) AND overlap in time (start within another's [start,last]+idle).
    finalized.sort(key=lambda cl: (cl["user"], cl["category"], cl["start"]))
    sweeps = []                # list of lists of clusters
    for cl in finalized:
        placed = False
        for sw in sweeps:
            s0 = sw[0]
            if (s0["user"] == cl["user"] and s0["category"] == cl["category"]
                    and cl["start"] - sw[-1]["last"] <= idle):
                sw.append(cl); placed = True; break
        if not placed:
            sweeps.append([cl])

    rows = []
    for sid, sw in enumerate(sweeps, 1):
        total = sum(cl["members"] for cl in sw)
        main = max(sw, key=lambda cl: cl["members"])
        for cl in sw:
            rows.append((
                sid, args.account, cl["user"], cl["category"], cl["klass"],
                cl["old"] or None, cl["new"] or None,
                cl["start"].strftime("%Y-%m-%d %H:%M:%S"), cl["last"].strftime("%Y-%m-%d %H:%M:%S"),
                cl["members"], len(cl["states"]), len(cl["campaigns"]), len(cl["ad_groups"]),
                0 if cl is main else 1,
                round(cl["members"] / total, 4) if total else 0.0,
                cl["sample"]))

    n_exc = sum(1 for r in rows if r[13] == 1)
    print("\n[ok] %d sweeps -> %d intervention rows (%d flagged is_exception)" % (
        len(sweeps), len(rows), n_exc))
    print("\n=== largest interventions ===")
    for r in sorted(rows, key=lambda x: -x[9])[:10]:
        print("  %s  %-18s %s->%s  members=%d states=%d camps=%d ag=%d %s" % (
            r[7][:16], r[3], r[5] or "-", r[6] or "-", r[9], r[10], r[11], r[12],
            "EXC" if r[13] else ""))

    if args.apply:
        conn.execute("DROP TABLE IF EXISTS %s" % TABLE)
        conn.execute(
            "CREATE TABLE %s ("
            " id INTEGER PRIMARY KEY, sweep_id INTEGER, account_id INTEGER, user_email TEXT,"
            " category TEXT, klass TEXT, old_value TEXT, new_value TEXT,"
            " window_start TEXT, window_end TEXT, member_count INTEGER,"
            " states_touched INTEGER, campaigns_touched INTEGER, ad_groups_touched INTEGER,"
            " is_exception INTEGER, sweep_member_share REAL, sample_detail TEXT)" % TABLE)
        conn.executemany(
            "INSERT INTO %s (sweep_id, account_id, user_email, category, klass, old_value, new_value,"
            " window_start, window_end, member_count, states_touched, campaigns_touched, ad_groups_touched,"
            " is_exception, sweep_member_share, sample_detail) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)" % TABLE, rows)
        conn.execute("CREATE INDEX ix_%s_sweep ON %s (sweep_id)" % (TABLE, TABLE))
        conn.execute("CREATE INDEX ix_%s_start ON %s (window_start)" % (TABLE, TABLE))
        conn.commit()
        print("\n[apply] wrote %d intervention rows to %s" % (len(rows), TABLE))
    else:
        print("\nDry run only. Re-run with --apply to write %s." % TABLE)
    conn.close()


def block_sample(cat, old, new):
    if old or new:
        return "%s: %s -> %s" % (cat, old or "-", new or "-")
    return cat


if __name__ == "__main__":
    main()
