"""
termlife_cluster_day1.py
========================
The 9/29 seasonality-adjustment test: day-1 lead economics by CAMPAIGN CLUSTER
(CR12 / CR3 / CR45), using the zip-targeting subsystem (10/6 build) to map
each disposition lead's consumer zip onto the campaign structure.

The adjustment (9/29 -> removed 10/2) touched CR3 + CR45 only. Verdict logic:
  - CR12 steady while CR3/CR45 broke at 9/29  -> adjustment implicated
  - CR12 broke too                             -> adjustment acquitted;
                                                  cause is account-wide or
                                                  SQ-side (their floor)

Method: every cohort day reconstructed AT AGE ONE from batch history (same-age
comparison, per the day-1 tracker), M-channel paid segments only. Spend(est)
uses each LEAD'S LP-SEGMENT rate (SQ pays by segment, not by campaign), so
cluster KPI1 needs no Google-spend table.

Caveats printed in output:
  - zip -> cluster uses CURRENT targeting state (baseline 2026-10-07); valid
    for this window iff no zip-list edits since ~mid-Sept (confirmed none).
  - consumer-entered zip != serving zip (mobile); medium-correlation proxy.
  - blank / invalid zips -> UNKNOWN (never defaulted into CR45).

Read-only (house rule: ro URI). Console only.
"""

import sqlite3
import sys
from datetime import date
from pathlib import Path
from statistics import median

DB_PATH = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")
BREAK_DATE = date(2026, 9, 29)          # seasonality adjustment start
PAID = ("CA", "CB", "CC")
CLUSTERS = ("CR12", "CR3", "CR45", "UNKNOWN")

SEG_OP_RATES = {                        # keep in sync with the main report
    "CC": {"2026-08": 149.67, "2026-09": 144.02, "2026-10": 148.53,
           "default": 148.53},
    "CB": {"2026-08": 86.55, "2026-09": 84.34, "2026-10": 86.75,
           "default": 86.75},
    "CA": {"2026-08": 30.98, "2026-09": 31.51, "2026-10": 33.24,
           "default": 33.24},
}


def rate(seg, d: date) -> float:
    r = SEG_OP_RATES[seg]
    return r.get(d.strftime("%Y-%m"), r["default"])


def load_cluster_sets(con):
    cr12, cr3, cr45_excl = set(), set(), set()
    for z, name, neg in con.execute("""
            SELECT t.zip, c.campaign_name, t.is_negative
            FROM campaign_zip_targeting t
            JOIN campaigns c ON c.campaign_id = t.campaign_id
            WHERE t.account_id = 29 AND c.status = 'ENABLED'
              AND t.zip IS NOT NULL AND t.country_code = 'US'"""):
        if "CR12" in name and not neg:
            cr12.add(z)
        elif "CR3" in name and not neg:
            cr3.add(z)
        elif "CR45" in name and neg:
            cr45_excl.add(z)
    if not cr12 or not cr3:
        sys.exit("zip-targeting tables empty or campaign names not matched")
    return cr12, cr3, cr45_excl


def main():
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    cr12, cr3, _ = load_cluster_sets(con)
    print(f"zip map loaded: CR12 {len(cr12)} zips | CR3 {len(cr3)} zips | "
          f"CR45 = complement | tie-break: CR12 wins the 4 overlaps\n")

    def cluster_for(zip_raw):
        z = (zip_raw or "").strip()
        if not z:
            return "UNKNOWN"
        z = z.zfill(5)
        if len(z) != 5 or not z.isdigit():
            return "UNKNOWN"
        if z in cr12:
            return "CR12"
        if z in cr3:
            return "CR3"
        return "CR45"

    # day-1 lead-grain rows (M channel, paid segments)
    rows = con.execute(f"""
        SELECT batch_date, day, substr(source_code,1,2), indv_zipcode,
               op_count, cris, capped_cri_dollars
        FROM life_disposition_snapshot
        WHERE day = date(batch_date, '-1 day')
          AND substr(source_code,1,2) IN {PAID}
          AND substr(source_code,3,1) = 'M'""").fetchall()
    con.close()
    if not rows:
        sys.exit("no day-1 observations in snapshot history")

    days = {}       # cohort day -> cluster -> acc
    unknown_n = 0
    for batch, day, seg, zip_raw, op, cris, prem in rows:
        d = date.fromisoformat(day)
        cl = cluster_for(zip_raw)
        if cl == "UNKNOWN":
            unknown_n += 1
        a = days.setdefault(d, {}).setdefault(
            cl, dict(leads=0, ops=0, cris=0, prem=0.0, spend=0.0))
        a["leads"] += 1
        a["ops"] += int(op or 0)
        a["cris"] += int(cris or 0)
        a["prem"] += prem or 0.0
        a["spend"] += int(op or 0) * rate(seg, d)   # payment follows LP segment

    def kpi(a):
        return a["prem"] / a["spend"] if a and a["spend"] else None

    all_days = sorted(days)
    yday = all_days[-1]

    print("== A. DAY-1 KPI BY CLUSTER (same-age; spend = per-lead segment rate) ==")
    print(f"  {'cohort':>10} {'wd':>3}" + "".join(
        f" | {c:>5} ops  KPI1" for c in ("CR12", "CR3", "CR45")))
    for d in all_days:
        line = f"  {d.isoformat():>10} {d.strftime('%a'):>3}"
        for c in ("CR12", "CR3", "CR45"):
            a = days[d].get(c)
            k = kpi(a)
            line += (f" | {a['ops'] if a else 0:5d}  "
                     f"{k:5.2f}" if k is not None else
                     f" | {a['ops'] if a else 0:5d}   --- ")
        print(line + ("   <-- yesterday" if d == yday else ""))

    wk = [d for d in all_days if d.weekday() < 5]
    pre = [d for d in wk if d < BREAK_DATE]
    post = [d for d in wk if d >= BREAK_DATE]

    print(f"\n== B. PRE vs POST {BREAK_DATE} (weekday cohorts: "
          f"{len(pre)} pre, {len(post)} post) ==")
    print(f"  {'cluster':>8} {'pre med KPI1':>13} {'post med KPI1':>14} "
          f"{'pre ops/d':>10} {'post ops/d':>11}")
    verdict = {}
    for c in ("CR12", "CR3", "CR45"):
        pk = [kpi(days[d].get(c)) for d in pre if kpi(days[d].get(c)) is not None]
        qk = [kpi(days[d].get(c)) for d in post if kpi(days[d].get(c)) is not None]
        po = [days[d].get(c, {}).get("ops", 0) for d in pre]
        qo = [days[d].get(c, {}).get("ops", 0) for d in post]
        pm = median(pk) if pk else None
        qm = median(qk) if qk else None
        verdict[c] = (pm, qm)
        print(f"  {c:>8} {pm if pm is not None else '--':>13.2f} "
              f"{qm if qm is not None else '--':>14.2f} "
              f"{median(po) if po else 0:>10.0f} {median(qo) if qo else 0:>11.0f}"
              if pm is not None and qm is not None else
              f"  {c:>8} {'--':>13} {'--':>14} "
              f"{median(po) if po else 0:>10.0f} {median(qo) if qo else 0:>11.0f}")

    print("\n== C. VERDICT HEURISTIC ==")
    def dropped(c, thresh=0.6):
        pm, qm = verdict.get(c, (None, None))
        return pm and qm is not None and qm < pm * thresh
    cr12_broke = dropped("CR12")
    others_broke = dropped("CR3") or dropped("CR45")
    if others_broke and not cr12_broke:
        print("  CR3/CR45 degraded while CR12 held -> consistent with the "
          "seasonality adjustment (9/29-10/2) as the driver.\n"
          "  Next: check whether post-10/2 cohorts recover as bidding re-learns.")
    elif cr12_broke:
        print("  CR12 degraded too -> the adjustment (CR3/45 only) cannot be "
          "the sole cause.\n  Look account-wide and SQ-side: what changed on "
          "their floor ~9/29?")
    else:
        print("  No cluster shows a >40% median drop -> the segment-level dip "
          "may be noise/mix; re-run as more post days accrue.")

    print(f"\n  hygiene: {unknown_n} of {len(rows)} day-1 M-leads had "
          f"blank/invalid zip -> UNKNOWN (excluded from cluster KPIs)")
    print("  caveats: current-state zip map (baseline 10/7) assumed stable "
          "over window; consumer zip != serving zip (medium-correlation proxy)")


if __name__ == "__main__":
    main()
