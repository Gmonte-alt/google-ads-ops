#!/usr/bin/env python3
"""
phase1_post_read.py — Phase 1 (+~7d) post-change read, Google Home.

Sections:
  [A] GENERIC BOOK (frozen-tag grain, campaign-agnostic): spend/leads/
      CPL/ROAS/GP per day — BASE28 (the 10/13 pin window) vs PRE10
      (Oct 3-12, post-surge/pre-launch) vs POST (Oct 14 -> data max),
      plus a daily strip since Oct 1.
  [B] S6 EARLY LOOK: the five pinned metrics vs the 2026-10-13 pin
      (early look — the +14 verdict stays on its own date).
  [C] BOOKS SIDE BY SIDE: generic vs brand-home vs flagship, same
      windows — brand books are the market/cycle control.
  [D] CAMPAIGN REACTIONS: per campaign PRE10 vs POST; TIER1 shown with
      a pre-surge rate too; TIER2 split Texas vs ex-Texas (geo grain);
      generic-book spend by campaign (migration/conservation check).
  [E] LEAKAGE: POST spend in tier campaigns' Home-Generic groups
      (expect ~0) and non-generic spend inside the nationwide campaign
      (expect ~0).
  [F] CONFOUND REGISTRY (printed, not footnoted).

Read-only. House conventions: kpd device='ALL' geo_state='ALL';
geo table = native device splits summed (no ALL rollup); runtime
column resolution with hard fails.

Usage:
  python phase1_post_read.py --db paid_search.db
      --assignment keyword_book_assignment_FROZEN_2026-08-27.csv
"""
import argparse
import csv
import datetime as dt
import sqlite3
import sys
from collections import defaultdict

LAUNCH = "2026-10-13"          # posted midday CT -> partial day, excluded
POST_START = "2026-10-14"
PRE10 = ("2026-10-03", "2026-10-12")   # post-surge, pre-launch
BASE28 = ("2026-09-14", "2026-10-11")  # the 10/13 pin window
PRESURGE = ("2026-09-14", "2026-09-30")
RULESET = "8/27+9/2a+9/2b+9/4-aggtail+9/8-prodintent"
# pinned 2026-10-13T09:40 values (BASE28 grain)
PIN = {"gen_cpc": 10.82, "gen_cpl": 71.11, "gen_impr": 36512,
       "gen_share": 0.345, "flag_cpl": 58.11, "blend_cpl": 64.17}
CAMP_IDS = {  # pinned IDs — robust to renames
    23394808297: "GENERIC-NATL (ex-TEXAS)", 23496334752: "TIER1",
    23530860942: "TIER2", 23586637678: "TIER3", 23428389081: "TIER4",
    23385188622: "CALIFORNIA", 23394808300: "FLORIDA"}
SHORT = {23394808297: "TEXAS", 23496334752: "TIER1", 23530860942: "TIER2",
         23586637678: "TIER3", 23428389081: "TIER4",
         23385188622: "CALIFORNIA", 23394808300: "FLORIDA"}


def die(m):
    print(f"*** HARD FAIL: {m}")
    sys.exit(1)


def days(a, b):
    return (dt.date.fromisoformat(b) - dt.date.fromisoformat(a)).days + 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--assignment", required=True)
    a = ap.parse_args()
    con = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row

    f = open(a.assignment, encoding="utf-8-sig")
    meta = f.readline().strip()
    if RULESET not in meta:
        die(f"frozen metadata mismatch: {meta}")
    tags, text_tags = {}, {}
    for r in csv.DictReader(f):
        if r["decision"].startswith("EXCLUDE"):
            continue
        k = (r["keyword"].lower().strip(), r["match_type"].upper())
        tags[(r["campaign"],) + k] = r["book"]
        text_tags.setdefault(k, r["book"])  # text-level fallback

    book_of, camp_of = {}, {}
    fb = 0
    for r in con.execute("SELECT id, campaign_id, keyword_text, "
                         "match_type FROM keyword_instances WHERE "
                         "account_id=1"):
        try:
            cid = int(r["campaign_id"])
        except (TypeError, ValueError):
            continue
        if cid not in SHORT:
            continue
        k = ((r["keyword_text"] or "").lower().strip(),
             (r["match_type"] or "").upper())
        bk = tags.get((SHORT[cid],) + k)
        if bk is None:
            bk = text_tags.get(k)
            if bk:
                fb += 1
        if bk:
            book_of[r["id"]] = bk
            camp_of[r["id"]] = cid
    if not book_of:
        die("no instances mapped")
    print(f"[TAGS] {len(book_of):,} instances mapped "
          f"({fb:,} text-level fallback — incl. post-launch adds)")

    kc = {r[1] for r in con.execute(
        "PRAGMA table_info(keyword_performance_daily)")}
    dcol = "report_date" if "report_date" in kc else "date"
    filt = ""
    if "device" in kc:
        filt += "AND device='ALL' "
    if "geo_state" in kc:
        filt += "AND geo_state='ALL' "
    g_max = con.execute(f"SELECT MAX({dcol}) FROM "
                        f"keyword_performance_daily WHERE 1=1 {filt}"
                        ).fetchone()[0]
    if g_max < POST_START:
        die(f"data only through {g_max} — run the daily syncs first")
    POST = (POST_START, g_max)
    print(f"[WINDOWS] data through {g_max} | POST {POST[0]}..{POST[1]} "
          f"({days(*POST)}d) | PRE10 {PRE10[0]}..{PRE10[1]} | "
          f"BASE28 {BASE28[0]}..{BASE28[1]}")
    print(f"[LAG] leads/value on the last ~2 days still revise upward "
          f"(conversion lag) — POST lead metrics are floors.")

    # pull daily per (book, campaign): im, ck, co, cv, rv
    cell = defaultdict(lambda: [0.0] * 5)
    ids = list(book_of)
    for i in range(0, len(ids), 900):
        chunk = ids[i:i + 900]
        ph = ",".join("?" * len(chunk))
        for r in con.execute(
                f"SELECT instance_id iid, {dcol} d, SUM(impressions) im, "
                f"SUM(clicks) ck, SUM(cost_micros)/1e6 co, "
                f"SUM(primary_conv_count) cv, SUM(primary_conv_value) rv "
                f"FROM keyword_performance_daily WHERE instance_id IN "
                f"({ph}) {filt}AND {dcol} >= ? GROUP BY 1,2",
                chunk + [PRESURGE[0]]):
            c = cell[(r["d"], book_of[r["iid"]], camp_of[r["iid"]])]
            c[0] += r["im"] or 0
            c[1] += r["ck"] or 0
            c[2] += r["co"] or 0
            c[3] += r["cv"] or 0
            c[4] += r["rv"] or 0

    def agg(win, book=None, cid=None, exclude_cid=None):
        s, e = win
        out = [0.0] * 5
        for (d, b, c), v in cell.items():
            if d < s or d > e:
                continue
            if book and b != book:
                continue
            if cid and c != cid:
                continue
            if exclude_cid and c == exclude_cid:
                continue
            for i in range(5):
                out[i] += v[i]
        return out  # im, ck, co, cv, rv

    def line(tag, v, nd):
        im, ck, co, cv, rv = v
        cpl = co / cv if cv else 0
        roas = rv / co if co else 0
        gp = (rv - co) / nd
        print(f"  {tag:10} spend ${co/nd:>7,.0f}/d  leads {cv/nd:>5.1f}/d"
              f"  CPL ${cpl:>6.2f}  ROAS {roas:4.2f}  GP ${gp:>+7,.0f}/d"
              f"  impr {im/nd:>7,.0f}/d")

    print("\n" + "=" * 72)
    print("[A] GENERIC BOOK — frozen-tag grain, all campaigns")
    print("=" * 72)
    for tag, w in (("BASE28", BASE28), ("PRE10", PRE10), ("POST", POST)):
        line(tag, agg(w, book="GENERIC"), days(*w))
    print("  daily strip (spend/d · leads · ROAS), Oct 1 ->:")
    d0 = dt.date.fromisoformat("2026-10-01")
    while d0.isoformat() <= g_max:
        ds = d0.isoformat()
        v = agg((ds, ds), book="GENERIC")
        mark = " <LAUNCH" if ds == LAUNCH else ""
        print(f"    {ds}  ${v[2]:>6,.0f}  {v[3]:>5.1f}L  "
              f"ROAS {v[4]/v[2] if v[2] else 0:4.2f}{mark}")
        d0 += dt.timedelta(days=1)

    print("\n" + "=" * 72)
    print("[B] S6 EARLY LOOK vs the 2026-10-13 pin (verdict stays at +14)")
    print("=" * 72)
    g = agg(POST, book="GENERIC")
    allb = agg(POST)
    fl = agg(POST, book="FLAGSHIP")
    nd = days(*POST)
    tot_im = allb[0]
    rows = [
        ("generic CPC", PIN["gen_cpc"], g[2] / g[1] if g[1] else 0, "$"),
        ("generic CPL", PIN["gen_cpl"], g[2] / g[3] if g[3] else 0, "$"),
        ("generic impr/28d-pace", PIN["gen_impr"], g[0] / nd * 28, ""),
        ("generic share-of-class", PIN["gen_share"],
         g[0] / tot_im if tot_im else 0, "%"),
        ("flagship CPL", PIN["flag_cpl"], fl[2] / fl[3] if fl[3] else 0, "$"),
        ("blended CPL", PIN["blend_cpl"],
         allb[2] / allb[3] if allb[3] else 0, "$")]
    for nm, pin, now, kind in rows:
        if kind == "%":
            print(f"  {nm:24} pin {pin:6.1%}   post {now:6.1%}")
        else:
            dl = (now - pin) / pin if pin else 0
            print(f"  {nm:24} pin {pin:>9,.2f}   post {now:>9,.2f}  "
                  f"({dl:+.0%})")

    print("\n" + "=" * 72)
    print("[C] BOOKS SIDE BY SIDE (brand books = market/cycle control)")
    print("=" * 72)
    for bk in ("GENERIC", "BRAND-HOME", "FLAGSHIP"):
        print(f"  {bk}:")
        for tag, w in (("PRE10", PRE10), ("POST", POST)):
            line("  " + tag, agg(w, book=bk), days(*w))

    print("\n" + "=" * 72)
    print("[D] CAMPAIGN REACTIONS (PRE10 -> POST, daily rates)")
    print("=" * 72)
    for cid, nm in CAMP_IDS.items():
        p = agg(PRE10, cid=cid)
        q = agg(POST, cid=cid)
        n1, n2 = days(*PRE10), days(*POST)
        print(f"  {nm:24} spend ${p[2]/n1:>6,.0f} -> ${q[2]/n2:>6,.0f}/d"
              f"  | leads {p[3]/n1:>5.1f} -> {q[3]/n2:>5.1f}/d"
              f"  | ROAS {p[4]/p[2] if p[2] else 0:4.2f} -> "
              f"{q[4]/q[2] if q[2] else 0:4.2f}")
    ps = agg(PRESURGE, cid=23496334752)
    print(f"  TIER1 pre-surge context ({PRESURGE[0]}..{PRESURGE[1]}): "
          f"${ps[2]/days(*PRESURGE):,.0f}/d, "
          f"{ps[3]/days(*PRESURGE):.1f} leads/d "
          f"(surge pauses 10/01-02 stepped it down BEFORE launch)")

    # TIER2 Texas split from geo grain
    gt = "campaign_performance_geo_daily_search"
    tset = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table','view')")}
    if gt in tset:
        gc = {r[1] for r in con.execute(f"PRAGMA table_info({gt})")}
        gd = "report_date" if "report_date" in gc else "date"
        cvc = "conversions" if "conversions" in gc else "primary_conv_count"
        print(f"\n  TIER2 split (geo grain, native device splits summed):")
        for lbl, cond in (("Texas inflow", "geo_state='Texas'"),
                          ("ex-Texas", "geo_state!='Texas'")):
            for tag, w in (("PRE10", PRE10), ("POST", POST)):
                r = con.execute(
                    f"SELECT SUM(cost_micros)/1e6 co, SUM({cvc}) cv FROM "
                    f"{gt} WHERE campaign_id IN (23530860942,'23530860942') "
                    f"AND {cond} "
                    f"AND {gd} BETWEEN ? AND ?", w).fetchone()
                n = days(*w)
                print(f"    {lbl:12} {tag:5} ${(r['co'] or 0)/n:>6,.0f}/d"
                      f"  {(r['cv'] or 0)/n:>5.1f} leads/d")
    else:
        print("  [TIER2 split skipped — geo table not found; "
              "run from the analysis env]")

    print("\n  GENERIC-BOOK SPEND BY CAMPAIGN (migration check, $/day):")
    for cid, nm in CAMP_IDS.items():
        p = agg(PRE10, book="GENERIC", cid=cid)
        q = agg(POST, book="GENERIC", cid=cid)
        print(f"    {nm:24} ${p[2]/days(*PRE10):>6,.0f} -> "
              f"${q[2]/days(*POST):>6,.0f}/d")

    print("\n" + "=" * 72)
    print("[E] LEAKAGE (POST window)")
    print("=" * 72)
    agt = next((t for t in ("ad_group_performance_daily",) if t in tset),
               None)
    if agt:
        ac = {r[1] for r in con.execute(f"PRAGMA table_info({agt})")}
        ad = "report_date" if "report_date" in ac else "date"
        af = "AND device='ALL' " if "device" in ac else ""
        nc = next((x for x in ("ad_group_name", "adgroup_name", "ad_group")
                   if x in ac), None)
        tiers = ("23496334752,23530860942,23586637678,23428389081,"
                 "'23496334752','23530860942','23586637678','23428389081'")
        r = con.execute(
            f"SELECT SUM(cost_micros)/1e6 FROM {agt} WHERE campaign_id IN "
            f"({tiers}) AND {nc} LIKE 'Home-Generic%' {af}"
            f"AND {ad} >= ?", (POST_START,)).fetchone()[0]
        print(f"  tier campaigns' Home-Generic groups, POST spend: "
              f"${r or 0:,.0f}  (expect ~$0 — groups paused)")
        r2 = con.execute(
            f"SELECT SUM(cost_micros)/1e6 FROM {agt} WHERE "
            f"campaign_id IN (23394808297,'23394808297') "
            f"AND {nc} NOT LIKE 'Home-Generic%' "
            f"{af}AND {ad} >= ?", (POST_START,)).fetchone()[0]
        print(f"  nationwide campaign, NON-generic groups, POST spend: "
              f"${r2 or 0:,.0f}  (expect ~$0 — brand groups paused)")
    else:
        print("  [skipped — ad_group_performance_daily not found]")

    print("\n" + "=" * 72)
    print("[F] CONFOUND REGISTRY — read every number above through these")
    print("=" * 72)
    print("""  1. Cycle phase: launch landed at CREST/ROLLING-OVER; projected
     trough window Oct 08-Nov 01 covers the entire POST window —
     raw declines are partly cycle. Grade vs the brand books [C].
  2. October payout-season repricing: hits ALL books incl. untouched
     ones; CPL levels are market-elevated vs any summer window.
  3. Surge actions 10/01-02 (pauses + MN/NM/AR removals): inside
     PRE10 and POST alike for tiers; TIER1 hit hardest — use its
     pre-surge line for that attribution.
  4. Texas geo moved to TIER2 at launch: TIER2 growth is partly
     mechanical inflow — use the geo split, not the topline.
  5. Negative lists attached at launch: generic query mix tightens
     by design; impressions/clicks compositional shift expected.
  6. Conversion lag: last ~2 days of leads/value revise upward.""")


if __name__ == "__main__":
    main()
