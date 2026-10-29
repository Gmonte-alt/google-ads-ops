"""
termlife_zip_mom.py  [v1 — zip-level MoM movement analysis, Term Life acct 29]
===================
Folder: C:\\Projects\\google-ads-ops\\reports\\quotecom-life-diposition\\
Output: output\\termlife_zip_mom_<today>.xlsx

Joins campaign_performance_zip_daily (Google spend/clicks by zip x month,
SERVED location, summed across native device rows) with the SQLife
disposition feed's zip outcomes (indv_zipcode = consumer-entered zip;
leads / ops / SQO / CRI (sold policy) / capped premium by LEAD month;
paid segments CA/CB/CC, channel M only).

DISPO READ = stitched lead grain: every lead is read from the LAST batch
containing it (ROW_NUMBER over batch_date DESC per source_code x sq_lead_id)
— the v1.5 rule, so Jan-Jun backfill batches and the live window read
consistently. Feed is NET of mid-window sold-row removals (Looker is gross);
recent months' CRI/prem are right-censored (immature). Oct is partial.

SHEETS
  Notes              — caveats + resolved column map + month tags
  ZipMonthly         — zip x month base table (floor-filtered for size)
  Movers_SpendShare  — top +/- MoM movers in share of monthly zip spend
  Movers_PremPerOp   — top +/- MoM movers in prem/op (>= OPS_FLOOR both months)
  SeptStep           — Aug->Sep decomposition: concentrated vs broad
  Zip3_Monthly       — zip3 rollup x month (spend + dispo + prem/op)
  State_Monthly      — state rollup x month (static zip3->state map, approx.)
  NewZipFrontier     — monthly spend into never-before-served zips vs
                       incumbents: CPC, mobile share, dispo quality

RUN:
  conda activate paid-search
  python reports\\quotecom-life-diposition\\termlife_zip_mom.py
"""

import re
import sqlite3
from pathlib import Path
from datetime import date, datetime, timezone

import pandas as pd

DB_PATH    = Path(r"C:\Projects\google-ads-ops\db\paid_search.db")
OUT_DIR    = Path(r"C:\Projects\google-ads-ops\reports\quotecom-life-diposition\output")
ACCOUNT_ID = 29

MONTHS      = [f"2026-{m:02d}" for m in range(1, 11)]   # Jan–Oct 2026
OPS_FLOOR   = 20       # min ops/month for prem/op movers (both months)
SPEND_FLOOR = 250.0    # min $ in either month for spend-share movers
BASE_MIN_COST, BASE_MIN_OPS = 100.0, 5   # ZipMonthly sheet size filter
TOP_N_MOVERS = 15      # per month-pair, each direction

# Policy KPI (est): prem ÷ (ops × segment rate). Rate card per op, seeded from
# the Looker by-advert Spend÷Ops (Aug–Oct'26); refresh monthly or swap in the
# AM's official card. Segment comes from each lead's source_code (CA/CB/CC).
SEG_OP_RATES = {"CC": 149.0, "CB": 86.0, "CA": 31.0}   # High / Medium / Low
SEG_TIER     = {"CC": "High", "CB": "Medium", "CA": "Low"}
KPI_TARGET   = 4.0
KPI_MIN_OPS_YTD = 6    # zip rows in the KPI matrix need >= this many 2026 ops

# Snapshot column overrides — set any the auto-detect gets wrong, e.g.
# SNAP_COLS = {"lead_date": "day", "ops": "op_count"}
SNAP_COLS: dict = {"lead_date": "day"}

_CANDIDATES = {
    "lead_date": ["day", "lead_date", "created_date", "lead_day"],
    "zip":       ["indv_zipcode", "zipcode", "zip"],
    "ops":       ["op_count", "ops", "opcount"],
    "sqo":       ["sqos", "sqo", "sqo_count", "sqo_flag", "is_sqo"],
    "cri":       ["cris", "cri", "cri_count", "cri_flag", "is_cri"],
    "prem":      ["capped_cri_dollars", "capped_cri", "cri_prem",
                  "capped_cri_amount", "capped_cri_dollar"],
}

# ── Static zip3 -> state map (standard SCF ranges; a handful of boundary
#    prefixes are approximate — rollup-grade, not authoritative) ─────────────
_Z3_RANGES = [
    (5, 5, "NY"), (6, 7, "PR"), (8, 8, "VI"), (9, 9, "PR"),
    (10, 27, "MA"), (28, 29, "RI"), (30, 38, "NH"), (39, 49, "ME"),
    (50, 59, "VT"), (60, 69, "CT"), (70, 89, "NJ"), (100, 149, "NY"),
    (150, 196, "PA"), (197, 199, "DE"), (200, 200, "DC"), (201, 201, "VA"),
    (202, 205, "DC"), (206, 219, "MD"), (220, 246, "VA"), (247, 268, "WV"),
    (270, 289, "NC"), (290, 299, "SC"), (300, 319, "GA"), (320, 349, "FL"),
    (350, 369, "AL"), (370, 385, "TN"), (386, 397, "MS"), (398, 399, "GA"),
    (400, 427, "KY"), (430, 459, "OH"), (460, 479, "IN"), (480, 499, "MI"),
    (500, 528, "IA"), (530, 549, "WI"), (550, 567, "MN"), (570, 577, "SD"),
    (580, 588, "ND"), (590, 599, "MT"), (600, 629, "IL"), (630, 658, "MO"),
    (660, 679, "KS"), (680, 693, "NE"), (700, 714, "LA"), (716, 729, "AR"),
    (730, 749, "OK"), (750, 799, "TX"), (800, 816, "CO"), (820, 831, "WY"),
    (832, 838, "ID"), (840, 847, "UT"), (850, 865, "AZ"), (870, 884, "NM"),
    (885, 885, "TX"), (889, 898, "NV"), (900, 961, "CA"), (962, 966, "AA"),
    (967, 968, "HI"), (969, 969, "GU"), (970, 979, "OR"), (980, 994, "WA"),
    (995, 999, "AK"),
]

def zip3_state(z3: str) -> str:
    try:
        n = int(z3)
    except (TypeError, ValueError):
        return "??"
    for lo, hi, st in _Z3_RANGES:
        if lo <= n <= hi:
            return st
    return "??"


def norm_zip(raw) -> str | None:
    """Digits only; 3-4 digits -> zfill(5) (lost leading zeros); 5+ -> first 5."""
    if raw is None:
        return None
    d = re.sub(r"\D", "", str(raw))
    if len(d) >= 5:
        return d[:5]
    if len(d) in (3, 4):
        return d.zfill(5)
    return None


def connect():
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def resolve_snap_cols(conn) -> dict:
    cols = {r["name"].lower(): r["name"] for r in
            conn.execute("PRAGMA table_info(life_disposition_snapshot)")}
    resolved = {}
    for key, cands in _CANDIDATES.items():
        if key in SNAP_COLS:
            resolved[key] = SNAP_COLS[key]
            continue
        hit = next((cols[c] for c in cands if c in cols), None)
        if hit is None:
            raise SystemExit(f"[ABORT] no snapshot column found for '{key}' "
                             f"(candidates {cands}) — set SNAP_COLS.")
        resolved[key] = hit
    print("Snapshot columns resolved:", resolved)
    return resolved


# ── Loads ─────────────────────────────────────────────────────────────────────

def load_spend(conn) -> tuple[pd.DataFrame, pd.Series, str]:
    """zip x month spend for 2026 + first-ever-served month per zip (full history)."""
    df = pd.read_sql_query("""
        SELECT substr(date,1,7) AS month, zip,
               SUM(cost_micros)/1e6 AS cost,
               SUM(clicks)          AS clicks,
               SUM(CASE WHEN device='MOBILE' THEN cost_micros ELSE 0 END)/1e6
                   AS mobile_cost
        FROM campaign_performance_zip_daily
        WHERE account_id = ? AND zip IS NOT NULL AND date >= '2026-01-01'
        GROUP BY 1, 2
    """, conn, params=(ACCOUNT_ID,))
    first = pd.read_sql_query("""
        SELECT zip, MIN(substr(date,1,7)) AS first_month
        FROM campaign_performance_zip_daily
        WHERE account_id = ? AND zip IS NOT NULL
        GROUP BY zip
    """, conn, params=(ACCOUNT_ID,)).set_index("zip")["first_month"]
    max_date = conn.execute(
        "SELECT MAX(date) FROM campaign_performance_zip_daily WHERE account_id=?",
        (ACCOUNT_ID,)).fetchone()[0]
    return df, first, max_date


def load_dispo(conn, sc: dict) -> tuple[pd.DataFrame, str]:
    """Stitched lead grain -> zip x lead-month aggregates (paid CA/CB/CC, channel M)."""
    q = f"""
        WITH latest AS (
            SELECT "{sc['lead_date']}" AS lead_date,
                   "{sc['zip']}"       AS raw_zip,
                   "{sc['ops']}"       AS ops,
                   "{sc['sqo']}"       AS sqo,
                   "{sc['cri']}"       AS cri,
                   "{sc['prem']}"      AS prem,
                   substr(source_code,1,2) AS seg,
                   ROW_NUMBER() OVER (PARTITION BY source_code, sq_lead_id
                                      ORDER BY batch_date DESC) AS rn
            FROM life_disposition_snapshot
            WHERE substr(source_code,1,2) IN ('CA','CB','CC')
              AND substr(source_code,3,1) = 'M'
        )
        SELECT lead_date, raw_zip, ops, sqo, cri, prem, seg
        FROM latest WHERE rn = 1
    """
    leads = pd.read_sql_query(q, conn)
    leads["month"] = leads["lead_date"].astype(str).str[:7]
    leads["zip"] = leads["raw_zip"].map(norm_zip)
    bad = leads["zip"].isna().sum()
    leads = leads[leads["zip"].notna() & leads["month"].isin(MONTHS)]
    for c in ("ops", "sqo", "cri", "prem"):
        leads[c] = pd.to_numeric(leads[c], errors="coerce").fillna(0)
    leads["payment_est"] = leads["ops"] * leads["seg"].map(SEG_OP_RATES)
    agg = (leads.groupby(["month", "zip"])
           .agg(leads=("zip", "size"), ops=("ops", "sum"), sqo=("sqo", "sum"),
                cri=("cri", "sum"), prem=("prem", "sum"),
                payment_est=("payment_est", "sum"))
           .reset_index())
    # per-zip dispo segment mix (ops by segment, full year) — tier cross-check
    seg_mix = (leads.groupby(["zip", "seg"])["ops"].sum()
               .unstack(fill_value=0)
               .rename(columns=lambda s: f"dispo_ops_{SEG_TIER.get(s, s)}"))
    max_lead = leads["lead_date"].max()
    print(f"Dispo: {len(leads):,} stitched leads in scope "
          f"({bad:,} unusable zips dropped); max lead date {max_lead}")
    return agg, seg_mix, str(max_lead)


# ── Analyses ──────────────────────────────────────────────────────────────────

def month_pairs(months_present: list[str]) -> list[tuple[str, str]]:
    ms = [m for m in MONTHS if m in months_present]
    return list(zip(ms, ms[1:]))


def build_base(spend: pd.DataFrame, dispo: pd.DataFrame) -> pd.DataFrame:
    base = spend.merge(dispo, on=["month", "zip"], how="outer").fillna(0)
    tot = base.groupby("month")["cost"].transform("sum")
    base["spend_share_pct"] = (100 * base["cost"] / tot).where(tot > 0)
    base["cpc"] = (base["cost"] / base["clicks"]).where(base["clicks"] > 0)
    base["mobile_share_pct"] = (100 * base["mobile_cost"] / base["cost"]).where(base["cost"] > 0)
    base["prem_per_op"] = (base["prem"] / base["ops"]).where(base["ops"] > 0)
    base["prem_per_google_dollar"] = (base["prem"] / base["cost"]).where(base["cost"] > 0)
    base["policy_kpi_est"] = (base["prem"] / base["payment_est"]).where(base["payment_est"] > 0)
    return base


def load_tiers(conn) -> tuple[dict, dict]:
    """zip -> income tier from ENABLED campaign targeting.
    CR12 positives = High, CR3 positives = Medium, CR45 catchall = Low
    (complement: any zip NOT positively listed and NOT CR45-excluded)."""
    high, med, cr45_neg = set(), set(), set()
    for r in conn.execute("""
        SELECT t.zip, c.campaign_name, t.is_negative
        FROM campaign_zip_targeting t
        JOIN campaigns c ON c.campaign_id = t.campaign_id
        WHERE t.account_id = ? AND c.status = 'ENABLED' AND t.zip IS NOT NULL
    """, (ACCOUNT_ID,)):
        name = r["campaign_name"].upper()
        if "CR12" in name and not r["is_negative"]:
            high.add(r["zip"])
        elif "CR3" in name and not r["is_negative"]:
            med.add(r["zip"])
        elif "CR45" in name and r["is_negative"]:
            cr45_neg.add(r["zip"])
    print(f"Targeting tiers: High(CR12)={len(high):,}  Medium(CR3)={len(med):,}  "
          f"CR45 exclusions={len(cr45_neg):,}")

    def classify(z: str) -> tuple[str, str]:
        if z in high:
            return "High", "CR12 positive list"
        if z in med:
            return "Medium", "CR3 positive list"
        if z in cr45_neg:
            return "EXCLUDED", "CR45 negative, not CR12/CR3 positive"
        return "Low", "CR45 catchall (complement)"

    return {"high": high, "med": med, "cr45_neg": cr45_neg}, classify


def tier_list_table(sets: dict, classify, base: pd.DataFrame,
                    seg_mix: pd.DataFrame, first_month: pd.Series) -> pd.DataFrame:
    """Full zip -> income tier list over every observed zip (targeting lists,
    CR45 exclusions, every zip ever served, every dispo zip)."""
    observed = (sets["high"] | sets["med"] | sets["cr45_neg"]
                | set(first_month.index) | set(base["zip"].unique()))
    rows = []
    ytd = base.groupby("zip")[["ops", "prem", "cost"]].sum()
    for z in sorted(observed):
        tier, src = classify(z)
        rows.append({"zip": z, "income_tier": tier, "tier_source": src})
    df = pd.DataFrame(rows)
    df["state"] = df["zip"].str[:3].map(zip3_state)
    df["first_served_month"] = df["zip"].map(first_month)
    df = df.merge(ytd, left_on="zip", right_index=True, how="left") \
           .rename(columns={"ops": "ops_2026", "prem": "prem_2026",
                            "cost": "google_cost_2026"})
    df = df.merge(seg_mix, left_on="zip", right_index=True, how="left")
    mix_cols = [c for c in df.columns if c.startswith("dispo_ops_")]
    if mix_cols:
        df[mix_cols] = df[mix_cols].fillna(0)
        df["dispo_majority_segment"] = (
            df[mix_cols].idxmax(axis=1).str.replace("dispo_ops_", "", regex=False)
            .where(df[mix_cols].sum(axis=1) > 0))
        df["tier_matches_dispo"] = (
            (df["income_tier"] == df["dispo_majority_segment"])
            .where(df["dispo_majority_segment"].notna()))
    return df.sort_values(["income_tier", "zip"])


def kpi_matrix(base: pd.DataFrame, classify) -> pd.DataFrame:
    """Rows = zips (+ account/tier anchor rows), columns = months,
    values = Policy KPI (est) = prem / (ops x segment rate). Target 4."""
    b = base.copy()
    b["income_tier"] = b["zip"].map(lambda z: classify(z)[0])
    months = [m for m in MONTHS if m in set(b["month"])]

    def row_for(df: pd.DataFrame, label: str, tier: str, state: str) -> dict:
        g = df.groupby("month")[["prem", "payment_est"]].sum()
        r = {"zip": label, "income_tier": tier, "state": state,
             "ops_2026": df["ops"].sum(),
             "ytd_kpi": (df["prem"].sum() / df["payment_est"].sum()
                         if df["payment_est"].sum() else None)}
        for m in months:
            pay = g["payment_est"].get(m, 0)
            r[m] = (g["prem"].get(m, 0) / pay) if pay else None
        return r

    rows = [row_for(b, "— ACCOUNT —", "", "")]
    for t in ("High", "Medium", "Low"):
        rows.append(row_for(b[b["income_tier"] == t], f"— {t.upper()} —", t, ""))

    ytd = b.groupby("zip").agg(ops=("ops", "sum"), pay=("payment_est", "sum"))
    keep = ytd[ytd["ops"] >= KPI_MIN_OPS_YTD].sort_values("pay", ascending=False).index
    for z in keep:
        d = b[b["zip"] == z]
        rows.append(row_for(d, z, d["income_tier"].iloc[0], zip3_state(z[:3])))

    return pd.DataFrame(rows)


def movers_spend_share(base: pd.DataFrame) -> pd.DataFrame:
    out = []
    piv = base.pivot_table(index="zip", columns="month",
                           values=["spend_share_pct", "cost"], aggfunc="sum")
    for m0, m1 in month_pairs(sorted(base["month"].unique())):
        try:
            s0, s1 = piv["spend_share_pct"][m0], piv["spend_share_pct"][m1]
            c0, c1 = piv["cost"][m0].fillna(0), piv["cost"][m1].fillna(0)
        except KeyError:
            continue
        d = pd.DataFrame({"share0": s0, "share1": s1, "cost0": c0, "cost1": c1})
        d = d[(d["cost0"] >= SPEND_FLOOR) | (d["cost1"] >= SPEND_FLOOR)]
        d["delta_pp"] = d["share1"].fillna(0) - d["share0"].fillna(0)
        d = d.dropna(subset=["delta_pp"])
        top = pd.concat([d.nlargest(TOP_N_MOVERS, "delta_pp"),
                         d.nsmallest(TOP_N_MOVERS, "delta_pp")])
        top = top.reset_index().rename(columns={"index": "zip"})
        top.insert(0, "month_pair", f"{m0} -> {m1}")
        out.append(top)
    res = pd.concat(out, ignore_index=True) if out else pd.DataFrame()
    if not res.empty:
        res["state"] = res["zip"].str[:3].map(zip3_state)
    return res


def movers_prem_per_op(base: pd.DataFrame) -> pd.DataFrame:
    out = []
    piv = base.pivot_table(index="zip", columns="month",
                           values=["prem_per_op", "ops", "prem"], aggfunc="sum")
    for m0, m1 in month_pairs(sorted(base["month"].unique())):
        try:
            d = pd.DataFrame({
                "ops0": piv["ops"][m0], "ops1": piv["ops"][m1],
                "ppo0": piv["prem_per_op"][m0], "ppo1": piv["prem_per_op"][m1],
                "prem0": piv["prem"][m0], "prem1": piv["prem"][m1],
            })
        except KeyError:
            continue
        d = d[(d["ops0"] >= OPS_FLOOR) & (d["ops1"] >= OPS_FLOOR)].copy()
        if d.empty:
            continue
        d["delta_ppo"] = d["ppo1"] - d["ppo0"]
        top = pd.concat([d.nlargest(TOP_N_MOVERS, "delta_ppo"),
                         d.nsmallest(TOP_N_MOVERS, "delta_ppo")])
        top = top.reset_index().rename(columns={"index": "zip"})
        top.insert(0, "month_pair", f"{m0} -> {m1}")
        out.append(top)
    res = pd.concat(out, ignore_index=True) if out else pd.DataFrame()
    if not res.empty:
        res["state"] = res["zip"].str[:3].map(zip3_state)
    return res


def sept_step(base: pd.DataFrame, grain_col: str = "zip",
              m0: str = "2026-08", m1: str = "2026-09") -> tuple[pd.DataFrame, pd.DataFrame]:
    """Aug->Sep premium decomposition at a grain: concentrated vs broad."""
    g = (base.groupby(["month", grain_col])
         .agg(prem=("prem", "sum"), ops=("ops", "sum")).reset_index())
    a = g[g["month"] == m0].set_index(grain_col)
    b = g[g["month"] == m1].set_index(grain_col)
    d = pd.DataFrame({"prem0": a["prem"], "ops0": a["ops"],
                      "prem1": b["prem"], "ops1": b["ops"]}).fillna(0)
    d["delta_prem"] = d["prem1"] - d["prem0"]
    d["ppo0"] = (d["prem0"] / d["ops0"]).where(d["ops0"] > 0)
    d["ppo1"] = (d["prem1"] / d["ops1"]).where(d["ops1"] > 0)
    total_delta = d["delta_prem"].sum()
    neg = d[d["delta_prem"] < 0]
    gross_neg = neg["delta_prem"].sum()
    top10 = neg.nsmallest(10, "delta_prem")
    qual = d[(d["ops0"] >= OPS_FLOOR) & (d["ops1"] >= OPS_FLOOR)]
    declining = (qual["ppo1"] < qual["ppo0"]).sum()
    summary = pd.DataFrame([
        ("Total prem Aug", d["prem0"].sum()),
        ("Total prem Sep", d["prem1"].sum()),
        ("Net delta prem", total_delta),
        ("Gross negative delta (declining units only)", gross_neg),
        (f"Top-10 declining {grain_col}s share of gross decline (%)",
         100 * top10["delta_prem"].sum() / gross_neg if gross_neg else float("nan")),
        (f"Qualifying {grain_col}s (>= {OPS_FLOOR} ops both months)", len(qual)),
        ("... of which prem/op declined (count)", declining),
        ("... share declining (%)",
         100 * declining / len(qual) if len(qual) else float("nan")),
    ], columns=["metric", "value"])
    detail = (d.sort_values("delta_prem").head(25).reset_index()
              .rename(columns={"index": grain_col}))
    if grain_col == "zip":
        detail["state"] = detail["zip"].str[:3].map(zip3_state)
    return summary, detail


def rollup(base: pd.DataFrame, level: str) -> pd.DataFrame:
    df = base.copy()
    df["zip3"] = df["zip"].str[:3]
    df["state"] = df["zip3"].map(zip3_state)
    key = "zip3" if level == "zip3" else "state"
    g = (df.groupby(["month", key])
         .agg(cost=("cost", "sum"), clicks=("clicks", "sum"),
              leads=("leads", "sum"), ops=("ops", "sum"),
              sqo=("sqo", "sum"), cri=("cri", "sum"), prem=("prem", "sum"))
         .reset_index())
    g["prem_per_op"] = (g["prem"] / g["ops"]).where(g["ops"] > 0)
    g["prem_per_google_dollar"] = (g["prem"] / g["cost"]).where(g["cost"] > 0)
    if key == "zip3":
        g["state"] = g["zip3"].map(zip3_state)
    return g.sort_values(["month", "cost"], ascending=[True, False])


def frontier(base: pd.DataFrame, first_month: pd.Series) -> pd.DataFrame:
    df = base.copy()
    df["first_month"] = df["zip"].map(first_month)
    df["cohort"] = (df["first_month"] == df["month"]).map(
        {True: "NEW (first ever served)", False: "incumbent"})
    g = (df.groupby(["month", "cohort"])
         .agg(zips=("zip", "nunique"), cost=("cost", "sum"),
              clicks=("clicks", "sum"), mobile_cost=("mobile_cost", "sum"),
              ops=("ops", "sum"), prem=("prem", "sum"))
         .reset_index())
    g["cpc"] = (g["cost"] / g["clicks"]).where(g["clicks"] > 0)
    g["mobile_share_pct"] = (100 * g["mobile_cost"] / g["cost"]).where(g["cost"] > 0)
    g["spend_share_pct"] = 100 * g["cost"] / g.groupby("month")["cost"].transform("sum")
    g["prem_per_op"] = (g["prem"] / g["ops"]).where(g["ops"] > 0)
    return g.sort_values(["month", "cohort"])


# ── Workbook ──────────────────────────────────────────────────────────────────

def write_workbook(path: Path, sheets: dict[str, pd.DataFrame], notes: list[str]):
    path.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(path, engine="openpyxl") as xw:
        pd.DataFrame({"Notes": notes}).to_excel(xw, sheet_name="Notes", index=False)
        for name, df in sheets.items():
            df.round(2).to_excel(xw, sheet_name=name[:31], index=False)
        for ws in xw.book.worksheets:
            ws.freeze_panes = "A2"
            for col in ws.columns:
                width = max((len(str(c.value)) for c in col[:50] if c.value is not None),
                            default=8)
                ws.column_dimensions[col[0].column_letter].width = min(width + 2, 55)
    print(f"Workbook written: {path}")


def main():
    print("=" * 65)
    print("termlife_zip_mom.py  [v1]")
    print(f"Database : {DB_PATH}  (mode=ro)")
    print(f"Started  : {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}")
    print("=" * 65)

    conn = connect()
    sc = resolve_snap_cols(conn)
    spend, first_month, max_spend_date = load_spend(conn)
    dispo, seg_mix, max_lead_date = load_dispo(conn, sc)
    tier_sets, classify = load_tiers(conn)
    conn.close()
    print(f"Spend: {len(spend):,} zip-months 2026; max date {max_spend_date}")

    base = build_base(spend, dispo)
    tiers_df = tier_list_table(tier_sets, classify, base, seg_mix, first_month)

    sheets = {
        "KPI_Matrix": kpi_matrix(base, classify),
        "ZipIncomeTiers": tiers_df,
        "ZipMonthly": base[(base["cost"] >= BASE_MIN_COST) |
                           (base["ops"] >= BASE_MIN_OPS)]
                        .sort_values(["month", "cost"], ascending=[True, False]),
        "Movers_SpendShare": movers_spend_share(base),
        "Movers_PremPerOp":  movers_prem_per_op(base),
    }
    s_sum, s_det = sept_step(base, "zip")
    # state-grain Sept step
    b2 = base.copy(); b2["state_tmp"] = b2["zip"].str[:3].map(zip3_state)
    s_sum_state, s_det_state = sept_step(b2, "state_tmp")
    spacer = pd.DataFrame([("", "")], columns=["metric", "value"])
    sheets["SeptStep"] = pd.concat([
        pd.DataFrame([("— ZIP GRAIN —", "")], columns=["metric", "value"]),
        s_sum, spacer,
        pd.DataFrame([("— STATE GRAIN —", "")], columns=["metric", "value"]),
        s_sum_state], ignore_index=True)
    sheets["SeptStep_TopZipDecliners"] = s_det
    sheets["Zip3_Monthly"]  = rollup(base, "zip3")
    sheets["State_Monthly"] = rollup(base, "state")
    sheets["NewZipFrontier"] = frontier(base, first_month)

    today = date.today().isoformat()
    notes = [
        f"Generated {today}. Spend: campaign_performance_zip_daily (acct 29, SERVED location, "
        f"sum across native device/location_type rows; ~91% of cpd spend recent / ~85% lifetime "
        f"— NOT spend-complete; max date {max_spend_date}).",
        f"Dispo: life_disposition_snapshot, stitched lead grain (last batch per lead), paid "
        f"CA/CB/CC channel M only, lead-month basis; max lead date {max_lead_date}. Feed is NET "
        f"of mid-window sold-row removals (Looker gross runs higher on CRIs/prem).",
        "Zip semantics differ across the join: spend zip = where the ad SERVED (physical "
        "presence); dispo zip = consumer-entered home zip (indv_zipcode). Mostly aligned under "
        "presence targeting, but not identical — treat zip-level prem_per_google_dollar as "
        "directional.",
        "CRI = sold policy; prem = capped premium (NOT revenue; SQ take ~33%). Recent months "
        "right-censored: Sep partially mature, Oct immature AND partial — prem/op movers into "
        "Sep/Oct mix real decline with maturation; ops/op-rate columns are the cleaner "
        "recent-month read.",
        f"Jan–Jun dispo months come from the Google Group backfill batches (each month observed "
        f"at ~age 60–90d — effectively mature).",
        f"Floors: prem/op movers require >= {OPS_FLOOR} ops in BOTH months; spend-share movers "
        f"require >= ${SPEND_FLOOR:.0f} in either month; ZipMonthly filtered to cost >= "
        f"${BASE_MIN_COST:.0f} or ops >= {BASE_MIN_OPS}.",
        "State/zip3 mapping is a static SCF-range table — a handful of boundary prefixes are "
        "approximate; 'AA' = military/other, '??' = unmapped.",
        "NewZipFrontier: 'NEW' = zip never served in the table before that month (history floor "
        "2024-10-01 — Jan-2026 'new' counts are inflated by the floor; read Feb onward). "
        "Tests the spillover mechanism's standing pattern: cheap mobile depth vs frontier widening.",
        f"KPI_Matrix: Policy KPI (est) = prem / (ops x segment rate), rate card "
        f"{SEG_OP_RATES} per op (High=CC/Med=CB/Low=CA from each lead's source_code; "
        f"estimate +/-1-2% vs Looker) — target {KPI_TARGET}, SQ break-even ~3. Zip rows "
        f"need >= {KPI_MIN_OPS_YTD} ops YTD; zip-month cells on 1-3 ops are noise — judge "
        f"against the account/tier anchor rows at top. Blank cell = no ops that month.",
        "ZipIncomeTiers: tier from ENABLED campaign targeting — CR12 positive = High, "
        "CR3 positive = Medium, CR45 catchall (complement) = Low, 'EXCLUDED' = in CR45's "
        "negative list without a positive listing. dispo_majority_segment is the "
        "cross-check (zip->tier is many-to-many per the all-devices caveat); CR12-over-CR3 "
        "on the 4 overlap zips. This sheet (and the CSV beside the workbook) is the "
        "zip_income_tiers config-table seed.",
        f"Snapshot column map used: {sc}",
    ]

    out = OUT_DIR / f"termlife_zip_mom_{today}.xlsx"
    write_workbook(out, sheets, notes)
    tiers_csv = OUT_DIR / f"zip_income_tiers_{today}.csv"
    tiers_df.to_csv(tiers_csv, index=False)
    print(f"Tier list CSV written: {tiers_csv}  ({len(tiers_df):,} zips)")

    # Console headline
    print("\nHeadline (SeptStep, zip grain):")
    for _, r in s_sum.iterrows():
        v = r["value"]
        print(f"  {r['metric']:<55} {v:,.1f}" if isinstance(v, (int, float)) and v == v
              else f"  {r['metric']:<55} {v}")
    print(f"\nDone. {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}")


if __name__ == "__main__":
    main()
