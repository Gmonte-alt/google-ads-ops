"""
ms_branded_device_mix.py — Deterioration #1 first check, Microsoft Home (acct 2)
═══════════════════════════════════════════════════════════════════════════════
QUESTION: how much of the branded (Non-Generic) book's RPL drop is DEVICE MIX
(cheaper-device leads taking share) vs WITHIN-DEVICE value decline?

Vendor payouts are priced state × device on a platform-blind list, and branded
desktop RPL runs well above mobile — so a device-share shift moves blended RPL
with no cell losing value. This probe decomposes ΔRPL exactly:

    ΔRPL = Σ (share_cur − share_base)·RPL_base   [MIX — composition]
         + Σ share_cur·(RPL_cur − RPL_base)      [WITHIN — real value change]

per scope (BRANDED all / per tier shell, GENERIC for contrast) per window
(14/7/3d vs immediately preceding baselines — same END detection as the
diagnosis script). Console-only; no workbook.

SOURCES & CONVENTIONS (per the 10/13 diagnosis verifications):
  - keyword_performance_daily SPLIT-device rows (DESKTOP/MOBILE/TABLET carry
    full metrics and sum exactly to the ALL rollup — verified 10/13); the
    ALL-like rollup rows are excluded so devices never double-count
  - leads = primary_conv_count, revenue = all_conv_value (pinned MS keyword
    grain); campaign/ad-group linkage ONLY via keyword_instances
    (kpd.campaign_id is NULL on acct 2); branded = the Non-Generic name token
  - CAVEAT printed in output: kpd includes the 9/18–21 syndicated-partner
    slice, so the 14d BASELINE is partner-inflated at keyword grain; the 7d
    windows (both post-9/22) are partner-free — read the decomposition there
  - geo_state is 'ALL' on kpd, so STATE mix inside branded is NOT testable
    here — that is the Google same-cell control's job (separate module)

USAGE (paid-search conda env):
  python ms_branded_device_mix.py
  python ms_branded_device_mix.py --end-date 2026-10-11
"""

import argparse
import re
import sqlite3
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

ACCOUNT_ID = 2
DEFAULT_DB = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")
WINDOWS = [14, 7, 3]

BRANDED_RX = re.compile(r"non[\s_\-]?generic", re.IGNORECASE)
TIER_RX = re.compile(r"TIER\s*(\d+)", re.IGNORECASE)


def die(msg):
    print(f"\n*** HARD FAIL: {msg}")
    sys.exit(1)


def table_columns(con, table):
    return [r[1] for r in con.execute(f"PRAGMA table_info({table})")]


def build_windows(end):
    out = {}
    for n in WINDOWS:
        cur_a = end - timedelta(days=n - 1)
        base_b = cur_a - timedelta(days=1)
        out[f"{n}d"] = {"cur": (cur_a, end),
                        "base": (base_b - timedelta(days=n - 1), base_b)}
    return out


def detect_end(con, cli_end):
    if cli_end:
        return date.fromisoformat(cli_end)
    rows = con.execute(
        """SELECT t.date, SUM(t.conversion_value)
           FROM campaign_performance_daily t
           JOIN campaigns c ON c.campaign_id = t.campaign_id
           WHERE c.account_id = ? AND t.device = 'ALL'
           GROUP BY t.date ORDER BY t.date DESC LIMIT 8""",
        (ACCOUNT_ID,)).fetchall()
    for d, rv in rows:
        if (rv or 0) > 0:
            return date.fromisoformat(str(d)[:10])
    die("no positive-revenue day in the last 8")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--end-date", default=None)
    args = ap.parse_args()

    db = Path(args.db)
    if not db.exists():
        die(f"DB not found at {db}")
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)

    end = detect_end(con, args.end_date)
    windows = build_windows(end)
    span_a = windows["14d"]["base"][0]
    print("═" * 78)
    print("BRANDED DEVICE-MIX DECOMPOSITION — Microsoft Home (acct 2)")
    print(f"END = {end}; span {span_a} → {end}")
    print("═" * 78)
    print("CAVEAT: kpd includes the 9/18–21 partner slice → the 14d BASELINE "
          "is\npartner-inflated at this grain; the 7d windows are clean — "
          "read those first.\nState mix is NOT testable here (kpd geo_state="
          "'ALL'); that's the Google-control's job.")

    # ── resolve columns / device literals ──────────────────────────────
    kcols = table_columns(con, "keyword_performance_daily")
    d_col = "report_date" if "report_date" in kcols else "date"
    for need in ("primary_conv_count", "all_conv_value", "cost_micros"):
        if need not in kcols:
            die(f"kpd lacks {need}")
    ag_cols = table_columns(con, "ad_groups")
    ag_key = "ad_group_id" if "ad_group_id" in ag_cols else "id"
    ag_name = "ad_group_name" if "ad_group_name" in ag_cols else "name"

    # ── pull split-device rows (exclude ALL-like rollup) ───────────────
    sql = f"""
    SELECT p.{d_col} AS dte, p.device AS device, k.campaign_id AS cid,
           g.{ag_name} AS ag_name,
           SUM(p.cost_micros)/1.0e6  AS spend,
           SUM(p.primary_conv_count) AS leads,
           SUM(p.all_conv_value)     AS rev
    FROM keyword_performance_daily p
    JOIN keyword_instances k ON k.id = p.instance_id
    JOIN ad_groups g ON g.{ag_key} = k.ad_group_id
    WHERE k.account_id = ? AND UPPER(TRIM(p.device)) != 'ALL'
      AND p.{d_col} BETWEEN ? AND ?
    GROUP BY 1, 2, 3, 4
    """
    rows = con.execute(sql, (ACCOUNT_ID, span_a.isoformat(),
                             end.isoformat())).fetchall()
    if not rows:
        die("no split-device kpd rows in span for acct 2")

    camp_name = {str(r[0]): r[1] for r in con.execute(
        "SELECT campaign_id, campaign_name FROM campaigns "
        "WHERE account_id = ?", (ACCOUNT_ID,))}

    def tier_of(cid):
        m = TIER_RX.search((camp_name.get(str(cid)) or "").upper())
        return f"T{m.group(1)}" if m else "OTHER"

    # cells[(scope, window, side, device)] = [spend, leads, rev]
    cells = defaultdict(lambda: [0.0, 0.0, 0.0])
    for dte, dev, cid, agn, sp, ld, rv in rows:
        d = date.fromisoformat(str(dte)[:10])
        branded = bool(BRANDED_RX.search(agn or ""))
        scopes = (["BRANDED (all)", f"BRANDED {tier_of(cid)}"]
                  if branded else ["GENERIC (all)"])
        for w, rng in windows.items():
            for side in ("cur", "base"):
                a, b = rng[side]
                if a <= d <= b:
                    for sc in scopes:
                        c = cells[(sc, w, side, str(dev).strip().upper())]
                        c[0] += sp or 0.0
                        c[1] += ld or 0.0
                        c[2] += rv or 0.0

    scopes = ["BRANDED (all)", "BRANDED T1", "BRANDED T2", "GENERIC (all)"]
    scopes += sorted({s for (s, *_ ) in cells}
                     - set(scopes) - {"BRANDED OTHER"})

    for sc in scopes:
        if not any(k[0] == sc for k in cells):
            continue
        print()
        print("─" * 78)
        print(f"SCOPE: {sc}")
        for w in windows:
            devs = sorted({k[3] for k in cells
                           if k[0] == sc and k[1] == w})
            cur = {d: cells[(sc, w, "cur", d)] for d in devs}
            base = {d: cells[(sc, w, "base", d)] for d in devs}
            L1 = sum(v[1] for v in cur.values())
            L0 = sum(v[1] for v in base.values())
            if L1 <= 0 or L0 <= 0:
                print(f"  {w}: insufficient leads (cur {L1:.0f} / "
                      f"base {L0:.0f}) — skipped")
                continue
            R1 = sum(v[2] for v in cur.values()) / L1
            R0 = sum(v[2] for v in base.values()) / L0
            print(f"  {w}: leads {L1:,.0f} (b {L0:,.0f})   blended RPL "
                  f"${R1:,.2f} (b ${R0:,.2f})   ΔRPL ${R1 - R0:+,.2f}")
            print(f"     {'device':<10}{'share':>7}{'b_share':>9}"
                  f"{'RPL':>9}{'b_RPL':>9}{'CPL':>9}{'b_CPL':>9}")
            mix = within = 0.0
            for d in devs:
                sp1, l1, r1 = cur[d]
                sp0, l0, r0 = base[d]
                s1, s0 = l1 / L1, l0 / L0
                rpl1 = r1 / l1 if l1 else 0.0
                rpl0 = r0 / l0 if l0 else 0.0
                cpl1 = sp1 / l1 if l1 else 0.0
                cpl0 = sp0 / l0 if l0 else 0.0
                mix += (s1 - s0) * rpl0
                within += s1 * (rpl1 - rpl0)
                print(f"     {d:<10}{s1:>6.1%}{s0:>8.1%}"
                      f"{rpl1:>9,.2f}{rpl0:>9,.2f}"
                      f"{cpl1:>9,.2f}{cpl0:>9,.2f}")
            resid = (R1 - R0) - (mix + within)
            tag = ("MIX-led" if abs(mix) > abs(within)
                   else "WITHIN-led — real value change")
            print(f"     ΔRPL split: MIX ${mix:+,.2f} + WITHIN "
                  f"${within:+,.2f} (identity resid ${resid:+,.2f}) → {tag}")

    print()
    print("READ: WITHIN-led on the 7d windows = branded leads genuinely "
          "worth less\nper device → vendor-vs-us question goes to the "
          "Google same-cell control.\nMIX-led = composition (cheaper-device "
          "share up) → check what shifted share\n(cutover membership, "
          "serving) before touching bids.")


if __name__ == "__main__":
    main()
