"""
analyze_geo_state_device.py  — read-only: state x device RPL/CvR/CPC, A vs B
============================================================================
Google Home (account_id=1) only. Source: campaign_performance_geo_daily,
aggregated across campaign_id up to geo_state, MOBILE/DESKTOP + a summed TOTAL.

Runs THREE pre-flight checks first (each gates trust in the output):
  PF1  device values present for account_id=1 — confirms there is NO 'ALL'
       rollup row (so TOTAL must be summed from device rows, no double-count).
  PF2  reconciliation on a SETTLED week: native geo conversions/value vs kpd
       routed primary_conv_count/value. If they track (~within a few %), native
       `conversions` ≈ pixel-primary and RPL = value/conversions is sound. If
       not, escalate to a routed geographic_view API pull.
  PF3  NULL geo_state share — revenue/cost on rows with no resolved state, so a
       state-grouped view's undercount is known.

Then: top-N states by period-A revenue, with RPL/CvR/CPC for TOTAL/MOBILE/
DESKTOP in both periods. Full matrix written to CSV.

READ-ONLY.

    python analyze_geo_state_device.py
    python analyze_geo_state_device.py --a-start 2026-04-01 --a-end 2026-04-30 \
        --b-start 2026-05-18 --b-end 2026-06-07 --recon-start 2025-05-05 \
        --recon-end 2025-05-11 --top 15 --out geo_state_device_rpl.csv
"""

import argparse
import csv
import sqlite3
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
DB = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")
ACCT = 1
GEO = "campaign_performance_geo_daily"


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--a-start", default="2026-04-01")
    p.add_argument("--a-end",   default="2026-04-30")
    p.add_argument("--b-start", default="2026-05-18")
    p.add_argument("--b-end",   default="2026-06-07")
    p.add_argument("--recon-start", default="2025-05-05")  # settled historical week
    p.add_argument("--recon-end",   default="2025-05-11")
    p.add_argument("--top", type=int, default=15)
    p.add_argument("--out", default="geo_state_device_rpl.csv")
    return p.parse_args()


def rpl(value, conv):  return round(value / conv, 2) if conv else None
def cvr(conv, clicks): return round(conv / clicks, 4) if clicks else None
def cpc(cost, clicks): return round(cost / clicks, 2) if clicks else None


def period_rollup(conn, start, end):
    """Returns {state: {device_class: {rev,conv,clicks,cost}}} for device_class
    in TOTAL / MOBILE / DESKTOP. TOTAL = sum of all non-ALL device rows."""
    rows = conn.execute(f"""
        SELECT geo_state, device,
               SUM(conversion_value) AS rev,
               SUM(conversions)      AS conv,
               SUM(clicks)           AS clicks,
               SUM(cost_micros)/1e6  AS cost
        FROM {GEO}
        WHERE account_id = ?
          AND date BETWEEN ? AND ?
          AND device <> 'ALL'
          AND geo_state IS NOT NULL
        GROUP BY geo_state, device
    """, (ACCT, start, end)).fetchall()

    out = {}
    for r in rows:
        st = r["geo_state"]
        d  = out.setdefault(st, {})
        for cls in ("TOTAL", r["device"]):
            if cls not in ("TOTAL", "MOBILE", "DESKTOP"):
                continue
            c = d.setdefault(cls, {"rev": 0.0, "conv": 0.0, "clicks": 0, "cost": 0.0})
            c["rev"]    += r["rev"] or 0
            c["conv"]   += r["conv"] or 0
            c["clicks"] += r["clicks"] or 0
            c["cost"]   += r["cost"] or 0
    return out


def main():
    a = parse_args()
    if not DB.exists():
        sys.exit(f"DB not found: {DB}")
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row

    print("=" * 76)
    print(f"  Google Home (account_id={ACCT}) state × device — A vs B")
    print(f"  A: {a.a_start}→{a.a_end}   B: {a.b_start}→{a.b_end}   src: {GEO}")
    print("=" * 76)

    # ── PF1: device values (confirm no 'ALL' rollup) ──────────────────────────
    print("\n  PF1 — device values for account_id=1:")
    devs = conn.execute(f"""
        SELECT device, COUNT(*) n FROM {GEO} WHERE account_id=? GROUP BY device ORDER BY n DESC
    """, (ACCT,)).fetchall()
    has_all = any(r["device"] == "ALL" for r in devs)
    for r in devs:
        print(f"      {str(r['device']):<14} {r['n']:>10,}")
    print(f"      => 'ALL' rollup present: {has_all}.  "
          f"{'TOTAL uses device=ALL? NO — script sums non-ALL rows.' if has_all else 'TOTAL = sum of device rows (correct, no double-count).'}")
    if has_all:
        print("      ⚠ An 'ALL' row exists; this script still sums non-ALL rows for TOTAL,")
        print("        so TOTAL is correct, but verify the ALL row isn't separately needed.")

    # ── PF2: reconciliation on a settled week ─────────────────────────────────
    print(f"\n  PF2 — reconcile native geo conversions vs kpd primary "
          f"(settled week {a.recon_start}→{a.recon_end}):")
    g = conn.execute(f"""
        SELECT SUM(conversions) AS conv, SUM(conversion_value) AS val
        FROM {GEO}
        WHERE account_id=? AND device<>'ALL' AND date BETWEEN ? AND ?
    """, (ACCT, a.recon_start, a.recon_end)).fetchone()
    k = conn.execute("""
        SELECT SUM(kpd.primary_conv_count) AS conv, SUM(kpd.primary_conv_value) AS val
        FROM keyword_performance_daily kpd
        JOIN keyword_instances ki ON ki.id = kpd.instance_id
        WHERE ki.account_id=? AND kpd.device='ALL' AND kpd.report_date BETWEEN ? AND ?
    """, (ACCT, a.recon_start, a.recon_end)).fetchone()
    gc, gv = g["conv"] or 0, g["val"] or 0
    kc, kv = k["conv"] or 0, k["val"] or 0
    print(f"      geo native : conv={gc:,.1f}  value=${gv:,.2f}")
    print(f"      kpd primary: conv={kc:,.1f}  value=${kv:,.2f}")
    if kc:
        dc = 100 * (gc - kc) / kc
        print(f"      conv delta : {dc:+.1f}%   "
              f"({'OK — native ≈ primary, RPL is sound' if abs(dc) <= 5 else 'REVIEW — gap > 5%, prefer routed API pull for exact RPL'})")
    if kv:
        dv = 100 * (gv - kv) / kv
        print(f"      value delta: {dv:+.1f}%")
    print("      (settled week chosen so conversion-date vs click-date lag isn't the cause)")

    # ── PF3: NULL geo_state share over the analysis windows ───────────────────
    print("\n  PF3 — NULL geo_state share (revenue lost from a state-grouped view):")
    for lbl, s, e in (("A", a.a_start, a.a_end), ("B", a.b_start, a.b_end)):
        tot = conn.execute(f"""
            SELECT SUM(conversion_value) AS rev, SUM(cost_micros)/1e6 AS cost
            FROM {GEO} WHERE account_id=? AND device<>'ALL' AND date BETWEEN ? AND ?
        """, (ACCT, s, e)).fetchone()
        nul = conn.execute(f"""
            SELECT SUM(conversion_value) AS rev, SUM(cost_micros)/1e6 AS cost
            FROM {GEO} WHERE account_id=? AND device<>'ALL' AND geo_state IS NULL
              AND date BETWEEN ? AND ?
        """, (ACCT, s, e)).fetchone()
        tr, nr = tot["rev"] or 0, nul["rev"] or 0
        share = 100 * nr / tr if tr else 0
        print(f"      period {lbl}: NULL-state revenue ${nr:,.2f} of ${tr:,.2f} ({share:.2f}%)")

    # ── Main: state × device, A vs B ──────────────────────────────────────────
    A = period_rollup(conn, a.a_start, a.a_end)
    B = period_rollup(conn, a.b_start, a.b_end)
    conn.close()

    states = sorted(A.keys(),
                    key=lambda s: A[s].get("TOTAL", {}).get("rev", 0), reverse=True)
    top = states[:a.top]

    # CSV: full matrix
    out_path = Path(a.out)
    with out_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["geo_state", "device_class", "period",
                    "revenue", "conversions", "clicks", "cost",
                    "RPL", "CvR", "CPC"])
        for st in states:
            for cls in ("TOTAL", "MOBILE", "DESKTOP"):
                for lbl, P in (("A", A), ("B", B)):
                    c = P.get(st, {}).get(cls)
                    if not c:
                        continue
                    w.writerow([st, cls, lbl,
                                round(c["rev"], 2), round(c["conv"], 1), c["clicks"],
                                round(c["cost"], 2), rpl(c["rev"], c["conv"]),
                                cvr(c["conv"], c["clicks"]), cpc(c["cost"], c["clicks"])])

    # Console: RPL-focused top-N
    def fmt(v, nd):
        return ("{:." + str(nd) + "f}").format(v) if v is not None else "—"

    print(f"\n  Top {a.top} states by period-A revenue — RPL (A → B, Δ%) by device:")
    hdr = ("  {:<16} {:<8} {:>11} {:>8} {:>8} {:>8} {:>7} {:>7} {:>7} {:>7}".format(
        "state", "device", "A rev", "A RPL", "B RPL", "RPL Δ%", "A CvR", "B CvR", "A CPC", "B CPC"))
    print(hdr)
    print("  " + "-" * 100)
    for st in top:
        for cls in ("TOTAL", "MOBILE", "DESKTOP"):
            ca = A.get(st, {}).get(cls)
            cb = B.get(st, {}).get(cls)
            if not ca:
                continue
            ar = rpl(ca["rev"], ca["conv"])
            br = rpl(cb["rev"], cb["conv"]) if cb else None
            d  = "{:+.1f}".format(100 * (br - ar) / ar) if (ar and br) else "—"
            a_cvr = cvr(ca["conv"], ca["clicks"])
            b_cvr = cvr(cb["conv"], cb["clicks"]) if cb else None
            a_cpc = cpc(ca["cost"], ca["clicks"])
            b_cpc = cpc(cb["cost"], cb["clicks"]) if cb else None
            name = st if cls == "TOTAL" else ""
            a_rev = "${:>10,.0f}".format(ca["rev"])
            print("  {:<16} {:<8} {:>11} {:>8} {:>8} {:>8} {:>7} {:>7} {:>7} {:>7}".format(
                name, cls, a_rev, fmt(ar, 2), fmt(br, 2), d,
                fmt(a_cvr, 3), fmt(b_cvr, 3), fmt(a_cpc, 2), fmt(b_cpc, 2)))
        print()

    print(f"  Full matrix (all states, TOTAL/MOBILE/DESKTOP, A+B) → {out_path.resolve()}")
    print("\n  Trust gate: only quote RPL/CvR if PF2 reconciled (≤~5%). Otherwise the")
    print("  numbers show shape, not exact pixel-primary level — note that in the writeup.")


if __name__ == "__main__":
    main()
