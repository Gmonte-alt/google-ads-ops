#!/usr/bin/env python3
"""
Cutover change-set generator — Microsoft Home geo re-tier.

Reads the FRESH Microsoft Ads Editor location export (the authoritative
current state) plus the committed ledger, maps every committed state to its
operating-group shell, and emits:

  1. <out>_changeset.xlsx   — human-readable per-campaign ADD / REMOVE / KEEP
                              plan with a pre-flight audit sheet (dual-targeting,
                              orphans, uncommitted-but-targeted states)
  2. <out>_editor_import.csv — Microsoft Ads Editor-importable rows:
                              removals as the ORIGINAL exported rows with
                              Status=Deleted (criterion IDs preserved), adds as
                              new Active rows (Target id reused when the state
                              appears anywhere in the export; otherwise Name-only
                              for Editor to resolve)
  3. serving_audit.sql       — post-cutover verification query (state x campaign
                              spend share from geo_performance_daily)

Operating-group mapping mirrors build_microsoft_retier_rollout_plan.py.

Usage:
  python build_cutover_changeset.py ^
    --export Home_Insurance_PositiveLocations_current_view_2026-09-22.csv ^
    --ledger C:\\Projects\\google-ads-ops\\commitments\\microsoft_home_baseline.yaml
"""
import argparse
import datetime as dt
import sys

import pandas as pd
import yaml

# ---- group config (keep in sync with the rollout builder) -------------------
GROUPING_PLANS = {
    "A": {
        "Premium": {"tiers": ["TIER ONE", "TIER TWO"], "campaign_pattern": "Tier1"},
        "Core":    {"tiers": ["TIER THREE"],            "campaign_pattern": "Tier2"},
        "Value":   {"tiers": ["TIER FOUR", "TIER FIVE"], "campaign_pattern": "Tier3"},
        "Floor":   {"tiers": ["TIER SIX"],              "campaign_pattern": "Tier5"},
    },
    "B": {
        "Premium": {"tiers": ["TIER ONE", "TIER TWO"], "campaign_pattern": "Tier1"},
        "Core":    {"tiers": ["TIER THREE", "TIER FOUR"], "campaign_pattern": "Tier2"},
        "Value":   {"tiers": ["TIER FIVE"],             "campaign_pattern": "Tier3"},
        "Floor":   {"tiers": ["TIER SIX"],              "campaign_pattern": "Tier5"},
    },
}
GROUPS = GROUPING_PLANS["A"]
STANDALONE = {"California": "CALIFORNIA", "Florida": "FLORIDA"}


def load_export(path):
    df = pd.read_csv(path, encoding="utf-16", sep=None, engine="python")
    df = df[df["Type"].astype(str).str.strip() == "Campaign Location Criterion"].copy()
    df["state"] = df["Name"].astype(str).str.split(",").str[0].str.strip()
    df["campaign"] = df["Campaign"].astype(str).str.strip()
    return df


def load_ledger(path):
    with open(path) as f:
        root = yaml.safe_load(f)["microsoft_home_geo_tiers"]
    return dict(root["commitments"]), root.get("baseline_date")


def desired_campaign(state, tier, campaigns):
    if state in STANDALONE:
        pat = STANDALONE[state]
    else:
        pat = next(cfg["campaign_pattern"] for g, cfg in GROUPS.items()
                   if tier in cfg["tiers"])
    for c in campaigns:
        if pat.lower() in c.lower():
            return c
    return f"(no campaign matches '{pat}')"


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--export", required=True)
    ap.add_argument("--ledger", default=r"C:\Projects\google-ads-ops\commitments\microsoft_home_baseline.yaml")
    ap.add_argument("--out-prefix", default=f"cutover_{dt.date.today():%Y-%m-%d}")
    ap.add_argument("--grouping", choices=["A", "B"], default="A",
                    help="Must match the rollout plan's grouping")
    a = ap.parse_args(argv)
    global GROUPS
    GROUPS = GROUPING_PLANS[a.grouping]

    exp = load_export(a.export)
    commitments, baseline_date = load_ledger(a.ledger)
    campaigns = sorted(exp["campaign"].unique())

    # ---- current + desired maps --------------------------------------------
    current = exp.groupby("state")["campaign"].apply(list).to_dict()
    target_ids = exp.drop_duplicates("state").set_index("state")["Target"].to_dict()
    desired = {s: desired_campaign(s, t, campaigns) for s, t in commitments.items()}

    # ---- pre-flight audit ---------------------------------------------------
    audit = []
    for s, camps in current.items():
        if len(set(camps)) > 1:
            audit.append(("DUAL_TARGETED", s, " | ".join(sorted(set(camps))),
                          "Same state active in multiple campaigns — remove all but the destination."))
        if s not in commitments:
            audit.append(("TARGETED_NOT_COMMITTED", s, " | ".join(sorted(set(camps))),
                          "Targeted but absent from the ledger — decide: commit or remove."))
    for s in commitments:
        if s not in current:
            audit.append(("ORPHAN_COMMITTED", s, "(none)",
                          "Committed but targeted by no campaign — true targeting ADD."))

    # ---- change set ---------------------------------------------------------
    rows = []
    for s, tier in sorted(commitments.items(), key=lambda kv: (kv[1], kv[0])):
        dest = desired[s]
        cur = sorted(set(current.get(s, [])))
        for c in cur:
            rows.append({"state": s, "tier": tier, "campaign": c,
                         "action": "KEEP" if c == dest else "REMOVE",
                         "destination": dest})
        if dest not in cur:
            rows.append({"state": s, "tier": tier, "campaign": dest,
                         "action": "ADD", "destination": dest})
    cs = pd.DataFrame(rows)

    # ---- Editor import file --------------------------------------------------
    removes = cs[cs["action"] == "REMOVE"]
    adds = cs[cs["action"] == "ADD"]
    imp_rows = []
    for _, r in removes.iterrows():
        orig = exp[(exp["state"] == r["state"]) & (exp["campaign"] == r["campaign"])]
        for _, o in orig.iterrows():
            d = o.drop(labels=["state", "campaign"]).to_dict()
            d["Status"] = "Deleted"
            imp_rows.append(d)
    for _, r in adds.iterrows():
        imp_rows.append({
            "Type": "Campaign Location Criterion", "Campaign": r["campaign"],
            "Status": "Active", "Target": target_ids.get(r["state"], ""),
            "Name": f"{r['state']}, United States",
            "Location Type": "State/Province", "Bid Adjustment": 0,
        })
    imp = pd.DataFrame(imp_rows)
    if not imp.empty:
        cols = [c for c in exp.columns if c not in ("state", "campaign")]
        imp = imp.reindex(columns=cols)

    # ---- write outputs -------------------------------------------------------
    xlsx = f"{a.out_prefix}_changeset.xlsx"
    with pd.ExcelWriter(xlsx, engine="openpyxl") as w:
        summary = (cs.groupby(["campaign", "action"]).size().unstack(fill_value=0)
                     .reindex(columns=["KEEP", "REMOVE", "ADD"], fill_value=0))
        summary.to_excel(w, sheet_name="Summary")
        cs[cs["action"] != "KEEP"].sort_values(["campaign", "action", "state"]) \
            .to_excel(w, sheet_name="Changes (ADD-REMOVE)", index=False)
        cs.sort_values(["campaign", "action", "state"]) \
            .to_excel(w, sheet_name="Full Map", index=False)
        pd.DataFrame(audit, columns=["issue", "state", "where", "resolution"]) \
            .to_excel(w, sheet_name="Pre-flight Audit", index=False)
    imp_path = f"{a.out_prefix}_editor_import.csv"
    imp.to_csv(imp_path, index=False, encoding="utf-8-sig")

    sql = f"""-- Post-cutover serving audit (run daily for ~1 week after cutover)
-- Expect each committed state's spend concentrated ~100% in its destination
-- campaign within a few days; stragglers = a missed location change.
SELECT g.geo_state AS state,
       g.campaign_id,
       ROUND(SUM(g.cost_micros)/1e6, 2)                       AS spend,
       ROUND(100.0 * SUM(g.cost_micros)
             / SUM(SUM(g.cost_micros)) OVER (PARTITION BY g.geo_state), 1)
                                                              AS pct_of_state_spend
FROM geo_performance_daily g
WHERE g.account_id = 2 AND g.location_type = 'PHYSICAL' AND g.device = 'ALL'
  AND g.date >= '<CUTOVER_DATE>'
  AND g.network IN ('Microsoft sites and select traffic', 'AOL search')  -- match model's search networks
GROUP BY g.geo_state, g.campaign_id
HAVING SUM(g.cost_micros) > 0
ORDER BY g.geo_state, spend DESC;
"""
    with open("serving_audit.sql", "w") as f:
        f.write(sql)

    # ---- console -------------------------------------------------------------
    print(f"ledger baseline: {baseline_date} | committed states: {len(commitments)} "
          f"| targeted rows in export: {len(exp)}")
    print(f"changes: {len(adds)} ADD, {len(removes)} REMOVE "
          f"| KEEP: {int((cs['action']=='KEEP').sum())}")
    print(f"audit findings: {len(audit)}")
    for kind, s, where, _ in audit:
        print(f"  [{kind}] {s}: {where}")
    print(f"\nwrote {xlsx}, {imp_path}, serving_audit.sql")
    return 0


if __name__ == "__main__":
    sys.exit(main())
