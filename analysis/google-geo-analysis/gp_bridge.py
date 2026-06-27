"""
gp_bridge.py — April->recent GP decomposition (bridge) for Google Home geo.

Answers: of the GP we lost from April to recent, how much came from VOLUME (fewer leads),
how much from VALUE (RPL erosion), and how much from EFFICIENCY (CPL/CPC) — split by RPL
band (High>=75 / Mid 45-75 / Low<45 / California) and by the model's tiers (Tier One-Four).

Per-band per-day GP = conv/day * (rpl - cpl). Two-period bridge (A=April, B=recent), per day:
   dGP = volume + value + efficiency + interaction
   volume     = (convB - convA) * gpl_A           # the Mid-volume squeeze lands here
   value      = convA * (rplB - rplA)             # the High-RPL erosion lands here
   efficiency = -convA * (cplB - cplA)
   interaction= (convB - convA) * ((rplB-rplA) - (cplB-cplA))
Identity holds exactly: volume+value+efficiency+interaction = convB*gplB - convA*gplA.

Basis: campaign_performance_geo_daily, account 1, native conversions (sum device rows;
~10-13% conservative on $; the SPLIT is robust, absolute $ run light). Single-state campaigns
(California/Texas/Florida) appear in the band view (CA carved out) and as 'Standalone' in the
zone view. Per-day normalization makes the unequal windows comparable (matches the workbook).

Usage:
    python gp_bridge.py --model-xlsx output\\google_geo_model_gp_per_lead_2026-06-09.xlsx
    python gp_bridge.py --apr 2026-04-01:2026-04-30 --rec 2026-05-18:2026-06-07
"""
from __future__ import annotations
import argparse, sqlite3, sys
from datetime import date
from pathlib import Path
import numpy as np, pandas as pd

DEFAULT_DB = r"C:\Projects\google-ads-ops\db\paid_search.db"
DEFAULT_OUT = r"C:\Projects\google-ads-ops\analysis\google-geo-analysis\output"
ACCOUNT_ID = 1
GEO = "campaign_performance_geo_daily"
HIGH, MIDLO = 75.0, 45.0
CARVE = {"California", "Texas", "Florida"}
BANDS = ["California", "High (>=75)", "Mid (45-75)", "Low (<45)"]
# Tiers are used verbatim from the model output (Tier One-Four); no renaming.
TIER_TO_ZONE = {}   # empty -> read_zones_from_model returns the raw model tier labels
TIER_ORDER = ["TIER ONE", "TIER TWO", "TIER THREE", "TIER FOUR", "TIER FIVE",
              "TIER SIX", "TIER SEVEN", "Standalone", "Unassigned"]


def pull(conn, start, end):
    sql = f"""SELECT geo_state AS state, SUM(clicks) clicks, SUM(cost_micros)/1e6 cost,
                     SUM(conversions) conv, SUM(conversion_value) rev
              FROM {GEO}
              WHERE account_id=? AND device<>'ALL' AND geo_state IS NOT NULL AND date BETWEEN ? AND ?
              GROUP BY geo_state"""
    return pd.read_sql_query(sql, conn, params=(ACCOUNT_ID, start, end))


CURRENT_CAMPAIGNS = ["Tier One", "Tier Two", "Tier Three", "Tier Four",
                     "California", "Texas", "Florida"]


def current_label(name):
    u = (name or "").upper()
    for c in CURRENT_CAMPAIGNS:
        if c.upper() in u:
            return c
    return "Other"


def pull_by_campaign(conn, start, end):
    """Per (state, current campaign) aggregates, so volume changes can be attributed to the
    campaign a state actually ran in. Defensive: returns empty if campaign_id isn't joinable."""
    try:
        sql = f"""SELECT g.geo_state AS state, c.campaign_name AS campaign,
                         SUM(g.clicks) clicks, SUM(g.cost_micros)/1e6 cost,
                         SUM(g.conversions) conv, SUM(g.conversion_value) rev
                  FROM {GEO} g
                  JOIN campaigns c ON c.campaign_id = g.campaign_id AND c.account_id = g.account_id
                  WHERE g.account_id=? AND g.device<>'ALL' AND g.geo_state IS NOT NULL
                    AND g.date BETWEEN ? AND ?
                  GROUP BY g.geo_state, c.campaign_name"""
        return pd.read_sql_query(sql, conn, params=(ACCOUNT_ID, start, end))
    except Exception as e:
        print(f"  (campaign-grain pull skipped: {e})", file=sys.stderr)
        return pd.DataFrame()


def read_zones_from_model(path):
    """Read zone assignments straight from the google_geo_model output workbook — the
    authoritative source — so the bridge can never drift from the model. Pulls the headline
    C2_weighted_kmeans column from 'State Assignments' plus 'Below Threshold' nearest-tier,
    and maps the model's tier labels to zone names (falls back to the raw label).
    Returns {state: zone}."""
    if not path or not Path(path).exists():
        return {}
    from openpyxl import load_workbook
    wb = load_workbook(path, data_only=True)
    out = {}

    def grab(sheet, tier_col):
        if sheet not in wb.sheetnames:
            return
        rows = list(wb[sheet].iter_rows(values_only=True))
        hdr = next((r for r in rows if r and r[0] == "state"), None)
        if not hdr:
            return
        H = {h: i for i, h in enumerate(hdr)}
        if "state" not in H or tier_col not in H:
            return
        for r in rows:
            if r and r[0] not in (None, "state") and r[H[tier_col]]:
                out[r[H["state"]]] = TIER_TO_ZONE.get(r[H[tier_col]], r[H[tier_col]])

    grab("State Assignments", "C2_weighted_kmeans")
    grab("Below Threshold", "assigned_tier_nearest")
    return out


def band_of(state, rpl_apr, rpl_all):
    if state in CARVE:
        return "California" if state == "California" else f"_carve_{state}"
    r = rpl_apr if pd.notna(rpl_apr) and rpl_apr > 0 else rpl_all
    if pd.isna(r):
        return "Low (<45)"
    return "High (>=75)" if r >= HIGH else ("Mid (45-75)" if r >= MIDLO else "Low (<45)")


def compute_bridges(db_path, apr, rec, model_xlsx, output_dir):
    """Pull windows, build the three bridges (+ console + chart). Returns the tuple write_wb consumes, or None."""
    a0, a1 = apr.split(":"); r0, r1 = rec.split(":")
    days_a = (date.fromisoformat(a1) - date.fromisoformat(a0)).days + 1
    days_r = (date.fromisoformat(r1) - date.fromisoformat(r0)).days + 1

    db = Path(db_path)
    if not db.exists():
        print(f"ERROR: DB not found: {db}", file=sys.stderr); return None
    conn = sqlite3.connect(str(db))
    A = pull(conn, a0, a1); B = pull(conn, r0, r1)
    Ac = pull_by_campaign(conn, a0, a1); Bc = pull_by_campaign(conn, r0, r1)
    conn.close()
    if A.empty or B.empty:
        print("ERROR: empty geo window.", file=sys.stderr); return None

    zmap = read_zones_from_model(model_xlsx)
    model_used = model_xlsx
    if not zmap:
        # auto-discover the newest model workbook in the output dir (prefer gp_per_lead)
        cands = sorted(Path(output_dir).glob("google_geo_model_gp_per_lead_*.xlsx"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
        if not cands:
            cands = sorted(Path(output_dir).glob("google_geo_model_*.xlsx"),
                           key=lambda p: p.stat().st_mtime, reverse=True)
        if cands:
            model_used = str(cands[0])
            zmap = read_zones_from_model(model_used)
    print(f"  tiers from model: {model_used or 'NONE — pass --model-xlsx'} ({len(zmap)} states mapped)")
    if not zmap:
        print("  WARNING: 0 states mapped -> the 'Bridge by Tier' view will be all Unassigned.")
    m = pd.merge(A, B, on="state", how="outer", suffixes=("_a", "_b")).fillna(0.0)
    # per-day
    for c in ["clicks", "cost", "conv", "rev"]:
        m[f"{c}A"] = m[f"{c}_a"] / days_a
        m[f"{c}B"] = m[f"{c}_b"] / days_r
    m["rpl_apr"] = np.where(m["convA"] > 0, m["revA"] / m["convA"], np.nan)
    m["rpl_all"] = np.where((m["convA"] + m["convB"]) > 0,
                            (m["revA"] + m["revB"]) / (m["convA"] + m["convB"]), np.nan)
    m["band"] = [band_of(s, ra, rl) for s, ra, rl in zip(m["state"], m["rpl_apr"], m["rpl_all"])]
    m = m[~m["band"].str.startswith("_carve_")]            # drop TX/FL from band view; keep CA
    m["tier"] = m["state"].map(lambda s: zmap.get(s, "Standalone" if s in CARVE else "Unassigned"))

    def bridge(df, key, order):
        rows = []
        for g in order:
            sub = df[df[key] == g]
            if sub.empty:
                continue
            cA, rA, kA = sub["convA"].sum(), sub["revA"].sum(), sub["costA"].sum()
            cB, rB, kB = sub["convB"].sum(), sub["revB"].sum(), sub["costB"].sum()
            rplA = rA / cA if cA else 0.0; cplA = kA / cA if cA else 0.0; gplA = rplA - cplA
            rplB = rB / cB if cB else 0.0; cplB = kB / cB if cB else 0.0; gplB = rplB - cplB
            GPA, GPB = rA - kA, rB - kB
            vol = (cB - cA) * gplA
            val = cA * (rplB - rplA)
            eff = -cA * (cplB - cplA)
            inter = (cB - cA) * ((rplB - rplA) - (cplB - cplA))
            rows.append({key: g, "convA/day": cA, "convB/day": cB,
                         "rplA": rplA, "rplB": rplB, "cplA": cplA, "cplB": cplB,
                         "GPA/day": GPA, "GPB/day": GPB, "dGP/day": GPB - GPA,
                         "volume": vol, "value": val, "efficiency": eff, "interaction": inter,
                         "check": vol + val + eff + inter})
        out = pd.DataFrame(rows)
        if not out.empty:
            tot = {key: "TOTAL"}
            for c in out.columns:
                if c != key:
                    tot[c] = out[c].sum()
            # totals for ratios are sums of components (dGP etc.) — rpl/cpl left blank
            for c in ["rplA", "rplB", "cplA", "cplB"]:
                tot[c] = np.nan
            out = pd.concat([out, pd.DataFrame([tot])], ignore_index=True)
        return out

    band_b = bridge(m, "band", BANDS)
    tier_b = bridge(m, "tier", TIER_ORDER)

    # ---- campaign x band: where the volume moved, inside each current campaign ----
    cb_b = pd.DataFrame(); mc = pd.DataFrame()
    if not Ac.empty and not Bc.empty:
        band_map = dict(zip(m["state"], m["band"]))
        mc = pd.merge(Ac, Bc, on=["state", "campaign"], how="outer", suffixes=("_a", "_b")).fillna(0.0)
        for c in ["clicks", "cost", "conv", "rev"]:
            mc[f"{c}A"] = mc[f"{c}_a"] / days_a
            mc[f"{c}B"] = mc[f"{c}_b"] / days_r
        mc["campaign"] = mc["campaign"].map(current_label)
        mc["band"] = [("(standalone)" if camp in CARVE else band_map.get(s, "Low (<45)"))
                      for s, camp in zip(mc["state"], mc["campaign"])]
        mc = mc[mc["campaign"] != "Other"]
        mc["camp_band"] = mc["campaign"] + " | " + mc["band"]
        cb_order = [f"{c} | {b}" for c in CURRENT_CAMPAIGNS for b in (BANDS + ["(standalone)"])]
        cb_order = [g for g in cb_order if g in set(mc["camp_band"])]
        cb_b = bridge(mc, "camp_band", cb_order)

    # ---- console ----
    print(f"GP BRIDGE  April[{a0}..{a1}, {days_a}d]  ->  Recent[{r0}..{r1}, {days_r}d]  (per-day, native conv)")
    print("\n  BY RPL BAND:")
    print(f"    {'band':14}{'dGP/day':>9}{'volume':>9}{'value':>9}{'effic':>8}{'inter':>8}")
    for _, r in band_b.iterrows():
        print(f"    {r['band']:14}{r['dGP/day']:>9.0f}{r['volume']:>9.0f}{r['value']:>9.0f}{r['efficiency']:>8.0f}{r['interaction']:>8.0f}")
    print("\n  BY TIER (model output):")
    print(f"    {'tier':14}{'dGP/day':>9}{'volume':>9}{'value':>9}{'effic':>8}{'inter':>8}")
    for _, r in tier_b.iterrows():
        print(f"    {r['tier']:14}{r['dGP/day']:>9.0f}{r['volume']:>9.0f}{r['value']:>9.0f}{r['efficiency']:>8.0f}{r['interaction']:>8.0f}")
    if not cb_b.empty:
        print("\n  BY CURRENT CAMPAIGN x BAND (where the volume moved):")
        print(f"    {'campaign | band':28}{'dGP/day':>9}{'volume':>9}{'value':>9}{'effic':>8}")
        for _, r in cb_b.iterrows():
            print(f"    {str(r['camp_band']):28}{r['dGP/day']:>9.0f}{r['volume']:>9.0f}{r['value']:>9.0f}{r['efficiency']:>8.0f}")

    # ---- chart ----
    png = Path(output_dir) / "gp_bridge_by_band.png"
    try:
        import matplotlib; matplotlib.use("Agg"); import matplotlib.pyplot as plt
        d = band_b[band_b["band"] != "TOTAL"]
        fig, ax = plt.subplots(figsize=(8, 4.5))
        x = np.arange(len(d)); w = 0.6
        bot = np.zeros(len(d))
        for comp, col in [("volume", "#4f81bd"), ("value", "#c0504d"),
                          ("efficiency", "#9bbb59"), ("interaction", "#c9c9c9")]:
            ax.bar(x, d[comp], w, bottom=bot, label=comp, color=col)
            bot = bot + d[comp].values
        ax.plot(x, d["dGP/day"], "ko", label="net dGP/day")
        ax.axhline(0, color="k", lw=0.8); ax.set_xticks(x); ax.set_xticklabels(d["band"], fontsize=9)
        ax.set_ylabel("GP/day change ($)"); ax.set_title("April->recent GP bridge by RPL band")
        ax.legend(fontsize=8, ncol=5); fig.tight_layout()
        Path(output_dir).mkdir(parents=True, exist_ok=True)
        fig.savefig(png, dpi=150); plt.close(fig)
    except Exception as e:
        print(f"  (chart skipped: {e})", file=sys.stderr); png = None

    return (m, band_b, tier_b, cb_b, mc, days_a, days_r, a0, a1, r0, r1, png)


def append_to_workbook(wb, db_path, apr="2026-04-01:2026-04-30", rec="2026-05-18:2026-06-07",
                       model_xlsx=None, output_dir="."):
    """Compute the bridges and add the bridge sheets to an existing workbook (no standalone file)."""
    res = compute_bridges(db_path, apr, rec, model_xlsx, output_dir)
    if res is None:
        raise RuntimeError("gp_bridge: empty geo window or DB not found")
    write_wb(None, *res, wb=wb)
    return wb


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--apr", default="2026-04-01:2026-04-30")
    p.add_argument("--rec", default="2026-05-18:2026-06-07")
    p.add_argument("--db-path", default=DEFAULT_DB)
    p.add_argument("--output-dir", default=DEFAULT_OUT)
    p.add_argument("--model-xlsx", default=None,
                   help="google_geo_model output workbook; zones are read from its State Assignments (authoritative).")
    args = p.parse_args(argv)
    res = compute_bridges(args.db_path, args.apr, args.rec, args.model_xlsx, args.output_dir)
    if res is None:
        return 1
    r1 = res[10]
    out = Path(args.output_dir) / f"gp_bridge_{r1}.xlsx"
    write_wb(out, *res)
    print(f"\n  Wrote: {out}")
    return 0


def write_wb(path, detail, band_b, tier_b, cb_b, mc, days_a, days_r, a0, a1, r0, r1, png, wb=None):
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    TITLE = Font(bold=True, size=14, color="305496"); NOTE = Font(italic=True, color="595959", size=9)
    HF = Font(bold=True, color="FFFFFF"); HFill = PatternFill("solid", fgColor="305496")
    M = '"$"#,##0'
    standalone = wb is None
    if standalone:
        wb = Workbook(); wb.remove(wb.active)

    # Detail (per-state per-day raw — the audit base)
    ws = wb.create_sheet("Detail")
    ws["A1"] = "Per-state per-day raw (native conv). band & zone drive the bridges."; ws["A1"].font = NOTE
    cols = ["state", "band", "tier", "convA", "revA", "costA", "convB", "revB", "costB"]
    for j, c in enumerate(cols, 1):
        x = ws.cell(row=2, column=j, value=c); x.font = HF; x.fill = HFill
    d = detail.sort_values(["band", "rpl_all"], ascending=[True, False])
    for i, (_, r) in enumerate(d.iterrows(), 3):
        ws.cell(row=i, column=1, value=r["state"]); ws.cell(row=i, column=2, value=r["band"])
        ws.cell(row=i, column=3, value=r["tier"])
        for j, c in enumerate(["convA", "revA", "costA", "convB", "revB", "costB"], 4):
            cell = ws.cell(row=i, column=j, value=float(r[c]))
            if c in ("revA", "costA", "revB", "costB"):
                cell.number_format = M
    n = len(d); last = 2 + n
    ws.freeze_panes = "A3"

    def bridge_sheet(name, key, dfb, detail_sheet, key_letter, data_letters, last_row, title, chart=False):
        ws = wb.create_sheet(name)
        ws["A1"] = title; ws["A1"].font = TITLE
        ws["A2"] = (f"Per-day. dGP = volume + value + efficiency + interaction. "
                    f"volume=(convB-convA)*gplA (the volume squeeze); value=convA*(rplB-rplA) (RPL erosion); "
                    f"efficiency=-convA*(cplB-cplA). Aggregates are live SUMIFS off {detail_sheet}; native-conv basis.")
        ws["A2"].font = NOTE
        heads = [key, "convA/day", "revA/day", "costA/day", "convB/day", "revB/day", "costB/day",
                 "GPA/day", "GPB/day", "dGP/day", "volume", "value", "efficiency", "interaction", "check"]
        hr = 4
        for j, c in enumerate(heads, 1):
            x = ws.cell(row=hr, column=j, value=c); x.font = HF; x.fill = HFill
        groups = [g for g in dfb[key].tolist() if g != "TOTAL"]
        sn = f"'{detail_sheet}'"   # quote so sheet names with spaces/parens evaluate
        dr = f"{sn}!${key_letter}$3:${key_letter}${last_row}"
        cA_, rA_, kA_, cB_, rB_, kB_ = data_letters

        def rng(L):
            return f"{sn}!${L}$3:${L}${last_row}"

        for i, g in enumerate(groups, hr + 1):
            R = i
            ws.cell(row=R, column=1, value=g)
            ws.cell(row=R, column=2, value=f'=SUMIFS({rng(cA_)},{dr},$A{R})')
            ws.cell(row=R, column=3, value=f'=SUMIFS({rng(rA_)},{dr},$A{R})')
            ws.cell(row=R, column=4, value=f'=SUMIFS({rng(kA_)},{dr},$A{R})')
            ws.cell(row=R, column=5, value=f'=SUMIFS({rng(cB_)},{dr},$A{R})')
            ws.cell(row=R, column=6, value=f'=SUMIFS({rng(rB_)},{dr},$A{R})')
            ws.cell(row=R, column=7, value=f'=SUMIFS({rng(kB_)},{dr},$A{R})')
            ws.cell(row=R, column=8, value=f'=C{R}-D{R}')
            ws.cell(row=R, column=9, value=f'=F{R}-G{R}')
            ws.cell(row=R, column=10, value=f'=I{R}-H{R}')
            ws.cell(row=R, column=11, value=f'=(E{R}-B{R})*((C{R}/B{R})-(D{R}/B{R}))')
            ws.cell(row=R, column=12, value=f'=B{R}*((F{R}/E{R})-(C{R}/B{R}))')
            ws.cell(row=R, column=13, value=f'=-B{R}*((G{R}/E{R})-(D{R}/B{R}))')
            ws.cell(row=R, column=14, value=f'=(E{R}-B{R})*(((F{R}/E{R})-(C{R}/B{R}))-((G{R}/E{R})-(D{R}/B{R})))')
            ws.cell(row=R, column=15, value=f'=K{R}+L{R}+M{R}+N{R}')
            for col in (3, 4, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15):
                ws.cell(row=R, column=col).number_format = M
        tot = hr + 1 + len(groups)
        ws.cell(row=tot, column=1, value="TOTAL").font = Font(bold=True)
        for col in range(2, 16):
            L = chr(64 + col)
            ws.cell(row=tot, column=col, value=f'=SUM({L}{hr+1}:{L}{tot-1})')
            if col >= 3:
                ws.cell(row=tot, column=col).number_format = M
            ws.cell(row=tot, column=col).font = Font(bold=True)
        ws.freeze_panes = "B5"
        if png and Path(png).exists() and chart:
            from openpyxl.drawing.image import Image as XLImage
            ws.add_image(XLImage(str(png)), f"A{tot+3}")

    bridge_sheet("Bridge by Band", "band", band_b, "Detail", "B",
                 ("D", "E", "F", "G", "H", "I"), last, "GP bridge by RPL band (April -> recent)", chart=True)
    bridge_sheet("Bridge by Tier", "tier", tier_b, "Detail", "C",
                 ("D", "E", "F", "G", "H", "I"), last,
                 "GP bridge by tier (April -> recent) — tiers from the model output")

    # ---- Campaign x Band: the squeeze inside each current campaign ----
    if cb_b is not None and not cb_b.empty and mc is not None and not mc.empty:
        wsc = wb.create_sheet("Detail (campaign)")
        wsc["A1"] = "Per (state x current campaign) per-day raw. camp_band drives the Campaign x Band bridge."
        wsc["A1"].font = NOTE
        ccols = ["state", "campaign", "band", "camp_band", "convA", "revA", "costA", "convB", "revB", "costB"]
        for j, c in enumerate(ccols, 1):
            x = wsc.cell(row=2, column=j, value=c); x.font = HF; x.fill = HFill
        dc = mc.sort_values(["campaign", "band", "state"])
        for i, (_, r) in enumerate(dc.iterrows(), 3):
            wsc.cell(row=i, column=1, value=r["state"]); wsc.cell(row=i, column=2, value=r["campaign"])
            wsc.cell(row=i, column=3, value=r["band"]); wsc.cell(row=i, column=4, value=r["camp_band"])
            for j, c in enumerate(["convA", "revA", "costA", "convB", "revB", "costB"], 5):
                cell = wsc.cell(row=i, column=j, value=float(r[c]))
                if c in ("revA", "costA", "revB", "costB"):
                    cell.number_format = M
        last_c = 2 + len(dc)
        wsc.freeze_panes = "A3"
        # camp_band key is col D; data convA..costB in cols E..J
        bridge_sheet("Bridge by Campaign x Band", "camp_band", cb_b, "Detail (campaign)", "D",
                     ("E", "F", "G", "H", "I", "J"), last_c,
                     "GP bridge by current campaign x RPL band — the Mid-volume squeeze inside Tier One & Tier Four")

    ws = wb.create_sheet("Methodology")
    notes = [
        ("GP bridge — methodology", True), ("", False),
        (f"Windows: April {a0}..{a1} ({days_a}d) vs Recent {r0}..{r1} ({days_r}d). All per-day.", False),
        ("Source: campaign_performance_geo_daily, account 1, native conv (sum device rows). ~10-13% conservative on $.", False),
        ("RPL bands by April RPL (fallback combined): High>=75 / Mid 45-75 / Low<45 / California carve-out.", False),
        ("Tiers read directly from the google_geo_model output (State Assignments + Below Threshold),", False),
        ("so they always equal the model run; CA/TX/FL = Standalone. No separate ledger.", False),
        ("", False),
        ("dGP/day = volume + value + efficiency + interaction (identity, check column verifies):", False),
        ("  volume     = (convB-convA) * gplA     <- volume gained/lost at the old margin (the squeeze)", False),
        ("  value      = convA * (rplB-rplA)       <- RPL erosion/gain at the old volume", False),
        ("  efficiency = -convA * (cplB-cplA)      <- CPL/CPC change at the old volume", False),
        ("  interaction= (convB-convA)*(dgpl)      <- second-order", False),
        ("Read: a Mid band losing GP mostly in 'volume' with ~0 'value' = held RPL, lost volume.", False),
        ("      a High band losing GP mostly in 'value' = RPL eroded at roughly held volume.", False),
    ]
    for r, (t, h) in enumerate(notes, 1):
        ws.cell(row=r, column=1, value=t).font = TITLE if h else NOTE
    ws.column_dimensions["A"].width = 110

    if standalone:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        wb.save(path)
    return wb


if __name__ == "__main__":
    sys.exit(main())
