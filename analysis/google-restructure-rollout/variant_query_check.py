#!/usr/bin/env python3
"""
variant_query_check.py — R3 VALIDATION (one question, one page):
If we pause 'home owners insurance' [BROAD] and 'homeowner insurance'
[BROAD] and keep 'homeowners insurance' [BROAD] + 'home insurance'
[BROAD], what query-level performance is actually at risk?

Method: pull every search query matched by the four keywords in the
REF window (2026-03-01..2026-07-28) and L90. A variant's query is
COVERED if the same normalized query was also matched by a survivor
keyword anywhere in the window (broad eligibility proven by
observation). VARIANT-EXCLUSIVE queries = the theoretical max at
risk — and even those re-route via broad eligibility after pause;
this measures the bound, not a predicted loss.

Read-only DB. Table/columns resolved at runtime; hard-fails with the
candidate list if the search-query table isn't found — paste the
error to the architect if so.

Usage: python variant_query_check.py --db paid_search.db
"""
import argparse
import os
import re
import sqlite3
import sys
from collections import defaultdict

# stored texts per 9/9 DIAG: 'Home Owners Insurance' and
# 'home owner insurance' (two words, singular) — 'homeowner insurance'
# kept for safety but does not exist standalone in this account
VARIANTS = {"home owners insurance", "home owner insurance",
            "homeowner insurance"}
SURVIVORS = {"homeowners insurance", "home insurance"}
MATCH = "BROAD"
REF = ("2026-03-01", "2026-07-28")




def die(m):
    print(f"*** HARD FAIL: {m}")
    sys.exit(1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    a = ap.parse_args()
    con = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    # pinned schema (architect, 9/9): sqp.instance_id is the keyword
    # key; scope platform='google' AND device='ALL'; join
    # search_queries for normalized_text; keep match_type_served
    # distinct from targeted match type; count keyword-join misses
    # (AI Max slice) rather than dropping silently.
    for t, need in (("search_query_performance",
                     {"query_id", "instance_id", "platform", "device",
                      "report_date", "match_type_served"}),
                    ("search_queries", {"id", "normalized_text"})):
        have = {r[1] for r in con.execute(f"PRAGMA table_info({t})")}
        if not need <= have:
            die(f"{t} missing {sorted(need - have)} — schema drifted "
                f"from the 9/9 pinned convention; re-verify with the "
                f"architect.")
    def norm_kw(t):
        # strip legacy BMM '+' and brackets/quotes, collapse whitespace
        t = re.sub(r"[+\[\]\"]", " ", (t or "").lower())
        return " ".join(t.split())

    kw_of = {}
    for r in con.execute(
            "SELECT id, keyword_text, match_type FROM keyword_instances "
            "WHERE account_id=1"):
        kw_of[r["id"]] = (norm_kw(r["keyword_text"]),
                          (r["match_type"] or "").upper())

    devs = [str(r[0]) for r in con.execute(
        "SELECT DISTINCT device FROM search_query_performance "
        "WHERE LOWER(platform)='google'")]
    has_all = next((d for d in devs if d.upper() == "ALL"), None)
    dev_filter = f"AND p.device='{has_all}' " if has_all else ""
    print(f"[DEVICE] sqp device values: {devs} -> "
          + (f"using rollup '{has_all}'" if has_all else
             "NO ALL rollup — summing across devices (convention "
             "amendment for the architect: sqp is per-device grain)"))
    g_max = con.execute(
        "SELECT MAX(report_date) FROM search_query_performance "
        "WHERE LOWER(platform)='google'").fetchone()[0]
    if not g_max:
        die("no google rows in search_query_performance")
    import datetime as dt
    L90 = ((dt.date.fromisoformat(g_max)
            - dt.timedelta(days=89)).isoformat(), g_max)

    qs = {(b, w): defaultdict(lambda: [0.0] * 4)
          for b in ("VAR", "SUR") for w in ("REF", "L90")}
    served_mix = defaultdict(float)
    miss_rows = miss_clicks = 0
    n_seen = 0
    for r in con.execute(
            "SELECT sq.normalized_text nq, p.report_date d, "
            "p.instance_id iid, p.match_type_served srv, "
            "SUM(p.impressions) im, SUM(p.clicks) ck, "
            "SUM(p.cost_micros)/1e6 co, SUM(p.primary_conv_count) cv "
            "FROM search_query_performance p "
            "JOIN search_queries sq ON sq.id = p.query_id "
            "WHERE LOWER(p.platform)='google' " + dev_filter +
            "AND p.report_date >= ? "
            "GROUP BY 1, 2, 3, 4", (REF[0],)):
        n_seen += 1
        kwmt = kw_of.get(r["iid"])
        if kwmt is None:
            miss_rows += 1
            miss_clicks += r["ck"] or 0
            continue
        kw, mt = kwmt
        if not mt.startswith(MATCH):
            continue
        if kw in VARIANTS:
            b = "VAR"
        elif kw in SURVIVORS:
            b = "SUR"
        else:
            continue
        served_mix[(b, r["srv"] or "?")] += r["ck"] or 0
        for w, (s, e) in (("REF", REF), ("L90", L90)):
            if s <= r["d"] <= e:
                cell = qs[(b, w)][r["nq"] or ""]
                cell[0] += r["im"] or 0
                cell[1] += r["ck"] or 0
                cell[2] += r["co"] or 0
                cell[3] += r["cv"] or 0
    print(f"[SCAN] query-day-instance rows in scope: {n_seen:,}")
    if not any(qs[("VAR", w)] for w in ("REF", "L90")):
        print("[DIAG] rows scanned:", n_seen)
        plats = [tuple(r) for r in con.execute(
            "SELECT platform, COUNT(*) FROM search_query_performance "
            "GROUP BY 1")]
        print("[DIAG] sqp platform values:", plats)
        print("[DIAG] candidate instances, summarized "
              "(distinct text/match -> instance count):")
        cand_ids = []
        agg = {}
        for r in con.execute(
                "SELECT id, keyword_text, match_type FROM "
                "keyword_instances WHERE account_id=1 AND ("
                "keyword_text LIKE '%owner%insurance%' OR "
                "keyword_text IN ('home insurance','homeowners "
                "insurance'))"):
            cand_ids.append(r["id"])
            k = (r["keyword_text"], r["match_type"])
            agg[k] = agg.get(k, 0) + 1
        for (kw, mt), n in sorted(agg.items(), key=lambda kv: -kv[1])[:12]:
            print(f"    '{kw}' [{mt}] x{n}")
        if len(agg) > 12:
            print(f"    ... +{len(agg) - 12} more distinct texts")
        # surgical: per exact target text, instance count + sqp rows
        # all-time vs in-REF-window — separates "matching bug" from
        # "no reportable query rows in window"
        print("[DIAG2] per-target sqp coverage "
              "(instances | sqp rows all-time | rows >= REF | "
              "clicks >= REF):")
        for label, texts in (("VARIANT", sorted(VARIANTS)),
                             ("SURVIVOR", sorted(SURVIVORS))):
            for txt in texts:
                ids = [i for i, (k, m) in kw_of.items()
                       if k == txt and m.startswith(MATCH)]
                if not ids:
                    print(f"    {label:8} '{txt}': NO BROAD "
                          f"instances under this exact normalized "
                          f"text")
                    continue
                ph = ",".join("?" * len(ids))
                allr = con.execute(
                    f"SELECT COUNT(*) FROM search_query_performance "
                    f"WHERE instance_id IN ({ph}) AND "
                    f"LOWER(platform)='google'", ids).fetchone()[0]
                inw = con.execute(
                    f"SELECT COUNT(*), SUM(clicks) FROM "
                    f"search_query_performance WHERE instance_id IN "
                    f"({ph}) AND LOWER(platform)='google' AND "
                    f"report_date >= ?", ids + [REF[0]]).fetchone()
                print(f"    {label:8} '{txt}': {len(ids)} inst | "
                      f"{allr:,} rows | {inw[0]:,} >= {REF[0]} | "
                      f"{inw[1] or 0:,.0f} clicks")
        if cand_ids:
            ph = ",".join("?" * len(cand_ids))
            n_link = con.execute(
                f"SELECT COUNT(*), SUM(clicks) FROM "
                f"search_query_performance WHERE instance_id IN ({ph}) "
                f"AND LOWER(platform)='google'",
                cand_ids).fetchone()
            print(f"[DIAG] sqp rows linking to those instances: "
                  f"{n_link[0]:,} ({n_link[1] or 0:,.0f} clicks)")
        die("no query rows matched the variant keywords after "
            "normalization — paste the [DIAG] block back; it names "
            "the mismatch (platform value, keyword text form, match "
            "type, or missing sqp linkage).")
    print(f"[SCOPE] google, device=ALL, rows since {REF[0]}: "
          f"{n_seen:,}; keyword-join misses (AI Max / unlinked): "
          f"{miss_rows:,} rows, {miss_clicks:,.0f} clicks — counted, "
          f"not dropped")
    print("[SERVED] match_type_served click mix (targeted is BROAD "
          "throughout): " + "; ".join(
              f"{b}:{s}={c:,.0f}" for (b, s), c in
              sorted(served_mix.items())))
    print(f"\n[R3 VALIDATION] windows: REF {REF[0]}..{REF[1]} | "
          f"L90 {L90[0]}..{L90[1]} (data through {g_max})")
    for w in ("REF", "L90"):
        var, sur = qs[("VAR", w)], qs[("SUR", w)]
        sur_set = set(sur)
        cov = {q: v for q, v in var.items() if q in sur_set}
        exc = {q: v for q, v in var.items() if q not in sur_set}
        def tot(d, i):
            return sum(v[i] for v in d.values())
        vl, cl, el = tot(var, 3), tot(cov, 3), tot(exc, 3)
        print(f"\n=== {w} ===")
        print(f"variant keywords: {len(var):,} queries | "
              f"{tot(var,1):,.0f} clicks | ${tot(var,2):,.0f} | "
              f"{vl:,.0f} leads")
        print(f"  COVERED (survivors matched same query): "
              f"{len(cov):,} queries, {cl:,.0f} leads "
              f"({cl / vl if vl else 0:.0%} of variant leads)")
        print(f"  VARIANT-EXCLUSIVE (max at risk): {len(exc):,} "
              f"queries, {tot(exc,1):,.0f} clicks, ${tot(exc,2):,.0f}, "
              f"{el:,.0f} leads ({el / vl if vl else 0:.0%})")
        top = sorted(exc.items(), key=lambda kv: -kv[1][3])[:12]
        if top:
            print(f"  top exclusive queries by leads:")
            for q, v in top:
                print(f"    {q[:52]:52} {v[1]:>6,.0f} clk "
                      f"${v[2]:>7,.0f} {v[3]:>5,.1f} leads")
    print("\nREAD: COVERED leads re-route by proven eligibility — the "
          "survivors already match those exact queries. EXCLUSIVE "
          "leads are the theoretical max at risk, and even those "
          "remain broad-eligible for the survivors after pause. "
          "Post-pause watch: Core ad group query count + lead volume "
          "vs this baseline.")


if __name__ == "__main__":
    main()
