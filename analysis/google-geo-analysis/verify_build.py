"""
verify_build.py — cutover acceptance check: AS-BUILT vs FROZEN LEDGER.

Compares the post-build tiers CSV (reshaped Ads Editor locations export) against the
committed ledger YAML, state by state. This is deliberately NOT the model's Ledger
Comparison sheet — that compares the model's fresh PROPOSAL to the ledger; acceptance
must compare what was actually BUILT to what was committed.

    python verify_build.py commitments\google_home_rpl_k4_committed_2026-07-29.yaml ^
        input\Google_Campaign_State_Tiers-v3_<date>_post-build.csv

PASS = every ledger state targeted in exactly its committed shell, no extras, no absences.
Any FAIL line is a build defect: fix in Editor and re-run same day.
"""
import sys
import yaml
import pandas as pd

def main():
    if len(sys.argv) != 3:
        print(__doc__)
        return 2
    led = yaml.safe_load(open(sys.argv[1]))["google_home_geo_tiers"]["commitments"]
    df = pd.read_csv(sys.argv[2])
    df["tier"] = df["tier"].astype(str).str.strip().str.upper()
    tiered = df[df["tier"] != "STANDALONE"]
    dup = tiered.groupby("state")["tier"].nunique()
    conflicts = sorted(dup[dup > 1].index)
    built = dict(zip(tiered["state"], tiered["tier"]))
    problems = []
    for s in sorted(set(led) | set(built)):
        want, got = led.get(s), built.get(s)
        if want != got:
            problems.append(f"  {s}: committed={want or 'ABSENT'}  built={got or 'ABSENT (not targeted)'}")
    for s in conflicts:
        problems.append(f"  {s}: DUAL-TARGETED across shells")
    if problems:
        print(f"FAIL — {len(problems)} defect(s) vs ledger ({len(led)} committed states):")
        print("\n".join(problems))
        return 1
    print(f"PASS — {len(built)}/{len(led)} states match the committed ledger exactly. "
          f"Structure is live and verified.")
    return 0

if __name__ == "__main__":
    sys.exit(main())
