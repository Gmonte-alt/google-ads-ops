"""
kw_theme_gp_bridge.py — keyword-THEME GP bridge for Google Home, April -> recent.

Mirrors gp_bridge.py but at the routed keyword grain instead of geo:
  - source  keyword_performance_daily (routed primary conv: primary_conv_count /
            primary_conv_value; cost_micros; device='ALL') joined to keyword_instances
            (ki.id = kpd.instance_id) for campaign_id / ad_group / keyword_text, then
            to campaigns. Scope is ki.campaign_id (kpd.campaign_id is NULL on device rows).
  - theme   derived from the KEYWORD TEXT by default (a keyword like "state farm home
            insurance" -> state_farm; "home insurance quote" -> generic). Keyword text is
            more accurate than ad-group name because some ad groups mix carriers (e.g. the
            CaRegion group: Bamboo/Wawanesa/NorCal AAA/CSAA). Edit BRAND_THEMES to split /
            merge brands; --theme-source ad_group switches to ad-group-name theming. A
            spend floor rolls the long tail into 'other_carriers' so the tab stays ~15-20 rows.
  - output  'KW Theme GP Bridge' (per-theme bridge + chart), 'Theme x Campaign'
            (dGP/day heatmap), 'KW Theme Detail', 'KW Theme Methodology'.

Per-day GP bridge identity (same as gp_bridge.py):
    dGP = volume + value + efficiency + interaction
    volume     = (convB-convA)*gplA      (the volume squeeze)
    value      = convA*(rplB-rplA)        (RPL erosion)
    efficiency = -convA*(cplB-cplA)       (CPL drift)

Standalone:
    python kw_theme_gp_bridge.py --db-path db/paid_search.db
Fold into the review workbook (like gp_bridge): build_performance_review_workbook
calls kw_theme_gp_bridge.append_to_workbook(wb, db_path, apr, rec, output_dir).
"""
import argparse, re, sys, sqlite3
from datetime import date
from pathlib import Path
import numpy as np
import pandas as pd

ACCOUNT_ID = 1
DEVICE_FILTER = "ALL"                       # keyword_performance_daily HAS an 'ALL' rollup row (per the canonical puller)
DEFAULT_DB = r"C:\Projects\google-ads-ops\db\paid_search.db"
DEFAULT_OUT = r"C:\Projects\google-ads-ops\analysis\google-home-kw-analysis\output"
KW_TABLE = "keyword_performance_daily"      # metrics: clicks, cost_micros, primary_conv_*
KI_TABLE = "keyword_instances"              # identity: campaign_id, ad_group_name, keyword_text (ki.id = kpd.instance_id)
TOP_N_CHART = 12                            # themes shown in the bridge chart (by |dGP|)
SPEND_FLOOR_PCT = 1.0                       # themes below this % of total spend roll into 'other_carriers'

# --- theme map: ordered, FIRST match wins. Tested against the ad group name (lowercased). ---
# Edit freely. Put specific carriers before the generic catch-all.
BRAND_THEMES = [
    ("geico",           r"geico"),
    ("state_farm",      r"state\s*-?\s*farm|statefarm"),
    ("farmers",         r"farmers"),
    ("root",            r"\broot\b"),
    ("allstate",        r"allstate"),
    ("progressive",     r"progressive"),
    ("liberty_mutual",  r"liberty"),
    ("usaa",            r"usaa"),
    ("nationwide",      r"nationwide"),
    ("travelers",       r"travelers"),
    ("american_family", r"american\s*family|amfam"),
    ("lemonade",        r"lemonade"),
    ("mercury",         r"mercury"),
    ("aggregator",      r"nerdwallet|bankrate|the\s*zebra|valuepenguin|policygenius|quotewizard"),
    ("membership",      r"\baaa\b|costco|membership|affinity"),
    ("generic",         r"generic|home\s*insur|homeowners|home-?ins|dwelling|condo\s*insur|renters\s*insur|property\s*insur|fire\s*insur|hazard\s*insur"),
]
BRAND_THEMES = [(name, re.compile(pat, re.I)) for name, pat in BRAND_THEMES]
UNMATCHED_THEME = "other"

CURRENT_CAMPAIGNS = ["Tier One", "Tier Two", "Tier Three", "Tier Four", "California", "Texas", "Florida"]


def current_label(name):
    """Map a raw campaign_name to the friendly tier/standalone label."""
    if not name:
        return "Other"
    u = str(name).upper()
    for key, lab in [("TIER ONE", "Tier One"), ("TIER TWO", "Tier Two"), ("TIER THREE", "Tier Three"),
                     ("TIER FOUR", "Tier Four"), ("CALIFORNIA", "California"), ("TEXAS", "Texas"),
                     ("FLORIDA", "Florida")]:
        if key in u:
            return lab
    return "Other"


def theme_of(label):
    if not label:
        return UNMATCHED_THEME
    for name, rx in BRAND_THEMES:
        if rx.search(str(label)):
            return name
    return UNMATCHED_THEME


def _table_cols(conn, tbl):
    return [r[1] for r in conn.execute(f"PRAGMA table_info({tbl})").fetchall()]


def resolve_columns(conn):
    """Resolve needed columns across keyword_performance_daily (metrics) and
    keyword_instances (identity), with fallbacks. Matches the canonical puller:
    kpd.instance_id = ki.id; campaign/ad-group/keyword come from ki because
    kpd.campaign_id is NULL on device rows."""
    kpd = _table_cols(conn, KW_TABLE)
    ki = _table_cols(conn, KI_TABLE)
    if not kpd:
        raise RuntimeError(f"table '{KW_TABLE}' not found. Tables: "
                           + ", ".join(r[0] for r in conn.execute(
                               "SELECT name FROM sqlite_master WHERE type='table'").fetchall()))
    if not ki:
        raise RuntimeError(f"table '{KI_TABLE}' not found (needed for campaign/ad-group/keyword identity).")
    lk = {c.lower(): c for c in kpd}; li = {c.lower(): c for c in ki}

    def pick(src, cands, required=True, what=""):
        m = lk if src == "kpd" else li
        for c in cands:
            if c.lower() in m:
                return m[c.lower()]
        if required:
            tbl = KW_TABLE if src == "kpd" else KI_TABLE
            raise RuntimeError(f"could not find a column for {what} in {tbl}. Tried {cands}.")
        return None

    C = {
        "instance_id": pick("kpd", ["instance_id"], what="kpd.instance_id"),
        "date":        pick("kpd", ["report_date", "date"], what="report_date"),
        "device":      pick("kpd", ["device"], required=False),
        "cost":        pick("kpd", ["cost_micros", "cost"], what="cost"),
        "conv":        pick("kpd", ["primary_conv_count", "primary_conversions", "conversions"], what="routed conv count"),
        "rev":         pick("kpd", ["primary_conv_value", "primary_conv_revenue", "conversion_value", "revenue"], what="routed conv value"),
        "clicks":      pick("kpd", ["clicks"], required=False),
        "ki_id":       pick("ki",  ["id", "instance_id"], what="keyword_instances.id"),
        "account_id":  pick("ki",  ["account_id"], what="keyword_instances.account_id"),
        "campaign_id": pick("ki",  ["campaign_id"], what="keyword_instances.campaign_id"),
        "ad_group":    pick("ki",  ["ad_group_name", "ad_group", "adgroup_name"], required=False),
        "keyword":     pick("ki",  ["keyword_text", "criterion", "keyword", "criteria"], required=False),
    }
    C["_cost_is_micros"] = (C["cost"].lower() == "cost_micros")
    return C, (kpd, ki)


def pull(conn, start, end, C, theme_source):
    src = C["ad_group"] if theme_source == "ad_group" else C["keyword"]
    if src is None:
        src = C["keyword"] if theme_source == "ad_group" else C["ad_group"]
        if src is None:
            raise RuntimeError("neither ad_group_name nor keyword_text on keyword_instances to theme from.")
        print(f"  NOTE: '{theme_source}' column absent; theming off the other identity column instead.")
    cost_expr = f"SUM(kpd.{C['cost']})/1.0e6" if C["_cost_is_micros"] else f"SUM(kpd.{C['cost']})"
    clicks_expr = f"SUM(kpd.{C['clicks']})" if C["clicks"] else "0"
    dev = f"AND kpd.{C['device']} = '{DEVICE_FILTER}'" if C["device"] else ""
    sql = f"""
        SELECT c.campaign_name AS campaign_name, ki.{C['campaign_id']} AS campaign_id,
               ki.{src} AS label,
               {cost_expr} AS cost, {clicks_expr} AS clicks,
               SUM(kpd.{C['conv']}) AS conv, SUM(kpd.{C['rev']}) AS rev
        FROM {KW_TABLE} kpd
        JOIN {KI_TABLE} ki ON ki.{C['ki_id']} = kpd.{C['instance_id']}
        JOIN campaigns c ON c.campaign_id = ki.{C['campaign_id']} AND c.account_id = ki.{C['account_id']}
        WHERE ki.{C['account_id']} = ? {dev}
          AND kpd.{C['date']} BETWEEN ? AND ?
        GROUP BY c.campaign_name, ki.{C['campaign_id']}, ki.{src}
    """
    df = pd.read_sql_query(sql, conn, params=(ACCOUNT_ID, start, end))
    if df.empty:
        return df
    df["theme"] = df["label"].map(theme_of)
    df["campaign"] = df["campaign_name"].map(current_label)
    for col in ["cost", "clicks", "conv", "rev"]:
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)
    return df


def bridge(df, key, order, days_a, days_r):
    """df has per-period columns convA/revA/costA/convB/revB/costB already per-day-normalized."""
    rows = []
    for g in order:
        sub = df[df[key] == g]
        if sub.empty:
            continue
        cA, rA, kA = sub["convA"].sum(), sub["revA"].sum(), sub["costA"].sum()
        cB, rB, kB = sub["convB"].sum(), sub["revB"].sum(), sub["costB"].sum()
        rplA = rA / cA if cA else 0.0; cplA = kA / cA if cA else 0.0; gplA = rplA - cplA
        rplB = rB / cB if cB else 0.0; cplB = kB / cB if cB else 0.0
        GPA, GPB = rA - kA, rB - kB
        vol = (cB - cA) * gplA
        val = cA * (rplB - rplA)
        eff = -cA * (cplB - cplA)
        inter = (cB - cA) * ((rplB - rplA) - (cplB - cplA))
        rows.append({key: g, "spendA/day": kA, "spendB/day": kB, "convA/day": cA, "convB/day": cB,
                     "rplA": rplA, "rplB": rplB, "cplA": cplA, "cplB": cplB,
                     "roasA": (rA / kA if kA else 0.0), "roasB": (rB / kB if kB else 0.0),
                     "GPA/day": GPA, "GPB/day": GPB, "dGP/day": GPB - GPA,
                     "volume": vol, "value": val, "efficiency": eff, "interaction": inter})
    out = pd.DataFrame(rows)
    if not out.empty:
        out = out.sort_values("dGP/day").reset_index(drop=True)             # biggest losers on top
        tot = {key: "TOTAL"}
        for c in out.columns:
            if c != key:
                tot[c] = out[c].sum()
        for c in ["rplA", "rplB", "cplA", "cplB", "roasA", "roasB"]:
            tot[c] = np.nan
        out = pd.concat([out, pd.DataFrame([tot])], ignore_index=True)
    return out


def compute(db_path, apr, rec, output_dir, theme_source="keyword"):
    a0, a1 = apr.split(":"); r0, r1 = rec.split(":")
    days_a = (date.fromisoformat(a1) - date.fromisoformat(a0)).days + 1
    days_r = (date.fromisoformat(r1) - date.fromisoformat(r0)).days + 1
    db = Path(db_path)
    if not db.exists():
        print(f"ERROR: DB not found: {db}", file=sys.stderr); return None
    conn = sqlite3.connect(str(db))
    C, all_cols = resolve_columns(conn)
    src_lbl = C["ad_group"] if theme_source == "ad_group" else C["keyword"]
    print(f"  theme source column: {src_lbl or '(fallback)'}   |   cost: {C['cost']}   conv: {C['conv']}   rev: {C['rev']}")
    A = pull(conn, a0, a1, C, theme_source); B = pull(conn, r0, r1, C, theme_source)
    conn.close()
    if A.empty or B.empty:
        print("ERROR: empty keyword window.", file=sys.stderr); return None

    # per (campaign, theme) per period -> per-day
    def per_day(df, suff, dd):
        g = df.groupby(["campaign", "theme"], as_index=False).agg(
            cost=("cost", "sum"), clicks=("clicks", "sum"), conv=("conv", "sum"), rev=("rev", "sum"))
        for c in ["cost", "clicks", "conv", "rev"]:
            g[c] = g[c] / dd
        return g.rename(columns={"cost": f"cost{suff}", "clicks": f"clicks{suff}",
                                 "conv": f"conv{suff}", "rev": f"rev{suff}"})
    ga = per_day(A, "A", days_a); gb = per_day(B, "B", days_r)
    m = pd.merge(ga, gb, on=["campaign", "theme"], how="outer").fillna(0.0)
    m = m[m["campaign"] != "Other"]

    # spend-floor rollup of tiny themes into other_carriers
    spend_by_theme = m.groupby("theme")["costA"].sum() + m.groupby("theme")["costB"].sum()
    tot_spend = spend_by_theme.sum()
    keep = set(spend_by_theme[spend_by_theme / tot_spend * 100.0 >= SPEND_FLOOR_PCT].index) if tot_spend else set()
    keep |= {"generic"}                                                     # always keep generic visible
    m["theme"] = m["theme"].map(lambda t: t if t in keep else ("other_carriers" if t != "generic" else t))

    # account-wide theme bridge
    theme_order = [t for t in m["theme"].unique()]
    theme_b = bridge(m, "theme", theme_order, days_a, days_r)

    # theme x campaign dGP matrix
    rows = []
    for (camp, th), sub in m.groupby(["campaign", "theme"]):
        gpA = (sub["revA"] - sub["costA"]).sum(); gpB = (sub["revB"] - sub["costB"]).sum()
        rows.append({"theme": th, "campaign": camp, "dGP/day": gpB - gpA})
    mat = pd.DataFrame(rows)
    matrix = mat.pivot_table(index="theme", columns="campaign", values="dGP/day", aggfunc="sum", fill_value=0.0)
    matrix = matrix.reindex(columns=[c for c in CURRENT_CAMPAIGNS if c in matrix.columns])
    # order themes by total dGP (losers first)
    matrix["__tot"] = matrix.sum(axis=1)
    matrix = matrix.sort_values("__tot").drop(columns="__tot")

    # ---- console ----
    print(f"KW THEME GP BRIDGE  April[{a0}..{a1}, {days_a}d] -> Recent[{r0}..{r1}, {days_r}d]  (per-day, routed)")
    print(f"  {'theme':18}{'dGP/day':>9}{'volume':>9}{'value':>9}{'effic':>8}{'RPLa':>6}{'RPLb':>6}")
    for _, r in theme_b.iterrows():
        rpa = "" if pd.isna(r["rplA"]) else f"{r['rplA']:.0f}"
        rpb = "" if pd.isna(r["rplB"]) else f"{r['rplB']:.0f}"
        print(f"  {str(r['theme']):18}{r['dGP/day']:>9.0f}{r['volume']:>9.0f}{r['value']:>9.0f}{r['efficiency']:>8.0f}{rpa:>6}{rpb:>6}")

    # ---- chart ----
    png = Path(output_dir) / "kw_theme_gp_bridge.png"
    try:
        import matplotlib; matplotlib.use("Agg"); import matplotlib.pyplot as plt
        d = theme_b[theme_b["theme"] != "TOTAL"].copy()
        d["abs"] = d["dGP/day"].abs()
        d = d.sort_values("abs", ascending=False).head(TOP_N_CHART).sort_values("dGP/day")
        fig, ax = plt.subplots(figsize=(9, max(3.5, 0.5 * len(d) + 1.5)))
        y = np.arange(len(d))
        left_pos = np.zeros(len(d)); left_neg = np.zeros(len(d))
        for comp, col in [("volume", "#4f81bd"), ("value", "#c0504d"), ("efficiency", "#e1a93b")]:
            vals = d[comp].values
            lefts = np.where(vals >= 0, left_pos, left_neg)
            ax.barh(y, vals, left=lefts, color=col, label=comp, height=0.6)
            left_pos = left_pos + np.where(vals >= 0, vals, 0)
            left_neg = left_neg + np.where(vals < 0, vals, 0)
        ax.plot(d["dGP/day"].values, y, "D", color="#2c2c2a", ms=7, label="net dGP/day")
        ax.axvline(0, color="k", lw=0.8); ax.set_yticks(y); ax.set_yticklabels(d["theme"], fontsize=9)
        ax.set_xlabel("GP change ($/day)"); ax.set_title("April->recent GP bridge by keyword theme")
        ax.legend(fontsize=8, ncol=4, loc="lower right"); fig.tight_layout()
        Path(output_dir).mkdir(parents=True, exist_ok=True)
        fig.savefig(png, dpi=150); plt.close(fig)
    except Exception as e:
        print(f"  (chart skipped: {e})", file=sys.stderr); png = None

    return dict(theme_b=theme_b, matrix=matrix, detail=m, png=png,
                days_a=days_a, days_r=days_r, a0=a0, a1=a1, r0=r0, r1=r1, src=src_lbl)


def add_sheets(wb, data):
    from openpyxl.styles import Font, PatternFill
    from openpyxl.formatting.rule import ColorScaleRule
    from openpyxl.drawing.image import Image as XLImage
    TITLE = Font(name="Arial", bold=True, size=14, color="305496"); NOTE = Font(name="Arial", italic=True, color="595959", size=9)
    HF = Font(name="Arial", bold=True, color="FFFFFF"); HFill = PatternFill("solid", fgColor="305496")
    M = '"$"#,##0'; M1 = '"$"#,##0.0'; theme_b = data["theme_b"]; matrix = data["matrix"]

    # ---- main bridge tab ----
    ws = wb.create_sheet("KW Theme GP Bridge")
    ws["A1"] = "Keyword-theme GP bridge (April -> recent, routed)"; ws["A1"].font = TITLE
    ws["A2"] = (f"Per-day. Account-wide, all campaigns & match types. dGP = volume + value + efficiency. "
                f"volume=(convB-convA)*gplA; value=convA*(rplB-rplA) (RPL erosion); efficiency=-convA*(cplB-cplA). "
                f"Sorted by GP loss. Theme = ad-group brand (edit BRAND_THEMES). Routed primary conv.")
    ws["A2"].font = NOTE
    img_anchor_row = 4
    if data["png"]:
        ws.add_image(XLImage(str(data["png"])), f"A{img_anchor_row}")
        start = img_anchor_row + 24
    else:
        start = img_anchor_row
    cols = ["theme", "dGP/day", "volume", "value", "efficiency", "interaction", "check", "GPA/day", "GPB/day",
            "spendA/day", "spendB/day", "convA/day", "convB/day", "rplA", "rplB", "cplA", "cplB", "roasA", "roasB"]
    for j, c in enumerate(cols, 1):
        x = ws.cell(row=start, column=j, value=c); x.font = HF; x.fill = HFill
    rr = start + 1
    for _, r in theme_b.iterrows():
        ws.cell(row=rr, column=1, value=r["theme"]).font = Font(name="Arial", bold=(r["theme"] == "TOTAL"))
        for j, c in enumerate(cols[1:], 2):
            if c == "check":                                  # volume+value+efficiency+interaction must equal dGP
                v = (r["volume"] + r["value"] + r["efficiency"] + r["interaction"])
            else:
                v = r[c]
            cell = ws.cell(row=rr, column=j, value=(None if pd.isna(v) else float(v)))
            if c in ("dGP/day", "volume", "value", "efficiency", "interaction", "check",
                     "GPA/day", "GPB/day", "spendA/day", "spendB/day"):
                cell.number_format = M
            elif c in ("rplA", "rplB", "cplA", "cplB"):
                cell.number_format = M1
            elif c in ("roasA", "roasB"):
                cell.number_format = '0%'
            else:
                cell.number_format = '#,##0.0'
        rr += 1
    last = rr - 1
    ws.conditional_formatting.add(f"B{start+1}:B{last-1}",
        ColorScaleRule(start_type='min', start_color='F4A6A6', mid_type='num', mid_value=0,
                       mid_color='FFFFFF', end_type='max', end_color='A6D8A6'))
    ws.freeze_panes = f"A{start+1}"
    for col, w in zip("ABCDEFGHIJKLMNOPQRS", [16, 10, 9, 9, 10, 11, 8, 10, 10, 11, 11, 10, 10, 7, 7, 7, 7, 7, 7]):
        ws.column_dimensions[col].width = w

    # ---- theme x campaign matrix ----
    ws = wb.create_sheet("Theme x Campaign")
    ws["A1"] = "GP change $/day by theme x campaign (April -> recent)"; ws["A1"].font = TITLE
    ws["A2"] = "Each cell = dGP/day for that theme in that campaign. Red = GP lost, green = gained. Campaigns are mirror images; this shows whether a theme's bleed is uniform or concentrated."; ws["A2"].font = NOTE
    camps = list(matrix.columns)
    ws.cell(row=4, column=1, value="theme").font = HF; ws.cell(row=4, column=1).fill = HFill
    for j, cmp in enumerate(camps, 2):
        x = ws.cell(row=4, column=j, value=cmp); x.font = HF; x.fill = HFill
    rr = 5
    for th, row in matrix.iterrows():
        ws.cell(row=rr, column=1, value=th).font = Font(name="Arial", bold=True)
        for j, cmp in enumerate(camps, 2):
            cell = ws.cell(row=rr, column=j, value=float(row[cmp])); cell.number_format = M
        rr += 1
    if camps:
        from openpyxl.utils import get_column_letter
        rng = f"B5:{get_column_letter(1+len(camps))}{rr-1}"
        ws.conditional_formatting.add(rng,
            ColorScaleRule(start_type='num', start_value=-50, start_color='F4A6A6',
                           mid_type='num', mid_value=0, mid_color='FFFFFF',
                           end_type='num', end_value=50, end_color='A6D8A6'))
    ws.freeze_panes = "B5"
    ws.column_dimensions["A"].width = 16
    for j in range(len(camps)):
        ws.column_dimensions[chr(66 + j)].width = 11

    # ---- detail ----
    ws = wb.create_sheet("KW Theme Detail")
    ws["A1"] = "Per campaign x theme, per-day (the audit base)"; ws["A1"].font = NOTE
    dcols = ["campaign", "theme", "convA", "revA", "costA", "convB", "revB", "costB"]
    for j, c in enumerate(dcols, 1):
        x = ws.cell(row=2, column=j, value=c); x.font = HF; x.fill = HFill
    d = data["detail"].sort_values(["theme", "campaign"])
    for i, (_, r) in enumerate(d.iterrows(), 3):
        ws.cell(row=i, column=1, value=r["campaign"]); ws.cell(row=i, column=2, value=r["theme"])
        for j, c in enumerate(["convA", "revA", "costA", "convB", "revB", "costB"], 3):
            cell = ws.cell(row=i, column=j, value=float(r[c]))
            if c in ("revA", "costA", "revB", "costB"):
                cell.number_format = M
    ws.freeze_panes = "A3"

    # ---- methodology ----
    ws = wb.create_sheet("KW Theme Methodology")
    notes = [
        ("Keyword-theme GP bridge — method", True),
        (f"Windows: April [{data['a0']}..{data['a1']}, {data['days_a']}d] vs recent [{data['r0']}..{data['r1']}, {data['days_r']}d], per-day.", False),
        (f"Source: {KW_TABLE} (routed primary conv: primary_conv_count / primary_conv_value), device='{DEVICE_FILTER}', joined to campaigns; account {ACCOUNT_ID}.", False),
        (f"Theme = brand parsed from the {data['src']} column via BRAND_THEMES (ordered regex, first match wins). Long tail below {SPEND_FLOOR_PCT}% of spend rolls into 'other_carriers'; 'generic' always kept.", False),
        ("Bridge: dGP = volume + value + efficiency + interaction. volume=(convB-convA)*gplA; value=convA*(rplB-rplA); efficiency=-convA*(cplB-cplA). gpl=rpl-cpl, rpl=rev/conv, cpl=cost/conv.", False),
        ("Read: a theme losing GP mostly in 'value' = its leads got cheaper to us (RPL erosion, e.g. carrier-payout). Mostly 'volume' = we bought fewer leads at held value.", False),
        ("Routed basis (primary conv only) — this is the keyword view the manager reads, and is the truth vs native geo.", False),
    ]
    for i, (t, h) in enumerate(notes, 1):
        ws.cell(row=i, column=1, value=t).font = (TITLE if h else NOTE)
    ws.column_dimensions["A"].width = 140
    return wb


def write_wb(path, data, wb=None):
    from openpyxl import Workbook
    standalone = wb is None
    if standalone:
        wb = Workbook(); wb.remove(wb.active)
    add_sheets(wb, data)
    if standalone:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        wb.save(path)
    return wb


def append_to_workbook(wb, db_path, apr="2026-04-01:2026-04-30", rec="2026-05-18:2026-06-07",
                       output_dir=".", theme_source="keyword"):
    data = compute(db_path, apr, rec, output_dir, theme_source)
    if data is None:
        raise RuntimeError("kw_theme_gp_bridge: empty window or DB/schema problem")
    add_sheets(wb, data)
    return wb


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--apr", default="2026-04-01:2026-04-30")
    p.add_argument("--rec", default="2026-05-18:2026-06-07")
    p.add_argument("--db-path", default=DEFAULT_DB)
    p.add_argument("--output-dir", default=DEFAULT_OUT)
    p.add_argument("--theme-source", choices=["keyword", "ad_group"], default="keyword",
                   help="derive theme from the keyword text (default) or the ad group name.")
    args = p.parse_args(argv)
    data = compute(args.db_path, args.apr, args.rec, args.output_dir, args.theme_source)
    if data is None:
        return 1
    out = Path(args.output_dir) / f"kw_theme_gp_bridge_{data['r1']}.xlsx"
    write_wb(out, data)
    print(f"\n  Wrote: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
