"""probe_recent_campaign_rows.py -- read-only: dump raw campaign-grain UI-grid rows
(full Changes prose) for an account since a given date. The week-in-review at the
source-of-truth grain: exact wording, campaigns, timestamps.

    python probe_recent_campaign_rows.py --db <db> --account 1 --since 2026-09-24
    python probe_recent_campaign_rows.py --db <db> --account 29 --since 2026-09-24 --contains "bid"
"""
import argparse
import sqlite3

ap = argparse.ArgumentParser()
ap.add_argument("--db", required=True)
ap.add_argument("--account", type=int, default=1)
ap.add_argument("--since", required=True, help="YYYY-MM-DD inclusive")
ap.add_argument("--contains", default=None, help="optional case-insensitive substring filter on the prose")
ap.add_argument("--max-chars", type=int, default=400, help="prose truncation per row")
ap.add_argument("--include-adgroup-rows", action="store_true",
                help="also show ad-group-grain rows (default: campaign-grain only)")
ap.add_argument("--parse", action="store_true",
                help="run each row through the pipeline's prose parser and annotate the "
                     "events it yields; rows producing NULL-old (timeline-start anchor) "
                     "statements are flagged. Run from the repo root with PYTHONPATH set.")
args = ap.parse_args()

parse_fn = None
if args.parse:
    try:
        import campaign_db_scripts.change_history.gads_change_common as C
        parse_fn = C.parse_campaign_events
    except ImportError as e:
        print("[WARN] --parse needs the pipeline package on PYTHONPATH "
              "(cd repo root; set PYTHONPATH=C:\\Projects\\google-ads-ops): %s" % e)

conn = sqlite3.connect("file:%s?mode=ro" % args.db, uri=True)
conn.row_factory = sqlite3.Row
q = ("SELECT change_datetime, campaign_col, ad_group_col, changes_raw "
     "FROM google_change_csv_raw WHERE account_id=? AND change_datetime >= ? ")
params = [args.account, args.since]
if not args.include_adgroup_rows:
    q += "AND trim(coalesce(ad_group_col,''))='' "
if args.contains:
    q += "AND lower(changes_raw) LIKE ? "
    params.append("%%%s%%" % args.contains.lower())
q += "ORDER BY change_datetime"
rows = conn.execute(q, params).fetchall()
print("%d %s rows for account %d since %s%s" % (
    len(rows), "campaign-grain" if not args.include_adgroup_rows else "grid",
    args.account, args.since,
    " containing '%s'" % args.contains if args.contains else ""))
n_anchor_rows = 0
for r in rows:
    events = parse_fn(r["changes_raw"]) if parse_fn else None
    if args.parse and parse_fn and not events:
        continue  # with --parse, show only rows the pipeline actually turns into events
    print("-" * 72)
    print("%s | campaign: %s" % (r["change_datetime"], r["campaign_col"] or "(blank)"))
    prose = (r["changes_raw"] or "").strip()
    print(prose[:args.max_chars] + (" [...]" if len(prose) > args.max_chars else ""))
    if events:
        for ctype, old_v, new_v in events:
            tag = "  <-- NULL-OLD ANCHOR (timeline start)" if old_v is None else ""
            print("   => %s %s -> %s%s" % (ctype, old_v, new_v, tag))
        if any(ev[1] is None for ev in events):
            n_anchor_rows += 1
if args.parse and parse_fn:
    print("=" * 72)
    print("%d row(s) produce NULL-old anchor statements" % n_anchor_rows)
conn.close()
