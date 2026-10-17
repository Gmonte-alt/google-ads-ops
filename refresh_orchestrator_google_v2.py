#!/usr/bin/env python
"""
refresh_orchestrator_google_v2.py -- ONE command for the daily Google refresh.
Built 2026-09-25 against the live script interfaces; supersedes the legacy
orchestrator (which referenced retired script names).

Encodes every lesson from the Term Life incident week:
  * ROLLING WINDOWS -- dates computed at runtime (yesterday back N days);
    the hardcoded-date staleness class is extinct.
  * SCOPE IS EXPLICIT -- --accounts home,term_life (the 9/24 scope-by-
    responsibility ruling); every run prints its scope.
  * STRUCTURE BEFORE PERFORMANCE, ENFORCED -- if an account's structure
    sync fails, its performance steps are SKIPPED (not attempted into a
    stale-FK wall) and the run exits non-zero.
  * LOUD FAILURES -- per-step logs, a status table at the end, exit code =
    failure count. Silence can never again hide a broken account.
  * WEEKLY GATE -- --weekly additionally runs commands listed in
    orchestrator_weekly.txt (one per line, # comments), so cadence items
    (ad_assets, change history, 08 --untagged) live in editable config.

Usage:
    python refresh_orchestrator_google_v2.py                 (daily, both accounts)
    python refresh_orchestrator_google_v2.py --weekly        (daily + weekly items)
    python refresh_orchestrator_google_v2.py --accounts home (scope override)
    python refresh_orchestrator_google_v2.py --dry-run       (print commands only)
"""
import argparse
import datetime
import os
import subprocess
import sys
import time

ROOT = r"C:\Projects\google-ads-ops"
LOGROOT = os.path.join(ROOT, "logs", "orchestrator")
WEEKLY_FILE = os.path.join(ROOT, "orchestrator_weekly.txt")
PY = sys.executable


def build_steps(accounts, start, end, refresh_days):
    """Ordered step list: (step_name, account_or_None, [cmd...], is_structure)."""
    steps = []
    # -- structure first, every account (THE invariant) --
    for a in accounts:
        steps.append(("kw02_structure", a,
                      [PY, r"kw_db_scripts\02_sync_keyword_structure.py", "--account", a], True))
        steps.append(("camp02_structure", a,
                      [PY, r"campaign_db_scripts\02_sync_campaigns_adgroups.py", "--account", a], True))
    # -- settings snapshot (home-hardcoded internally; runs once) --
    steps.append(("ai_max_flags", None,
                  [PY, r"campaign_db_scripts\sync_ai_max_flags.py", "--apply"], False))
    # -- campaign performance: single scoped runs (03 takes --accounts) --
    acct_csv = ",".join(accounts)
    steps.append(("camp03_daily", None,
                  [PY, r"campaign_db_scripts\03_sync_campaign_performance.py",
                   "--accounts", acct_csv, "--daily-only",
                   "--daily-start", start, "--daily-end", end], False))
    steps.append(("camp03_geo", None,
                  [PY, r"campaign_db_scripts\03_sync_campaign_performance.py",
                   "--accounts", acct_csv, "--geo-only",
                   "--geo-start", start, "--geo-end", end], False))
    # -- per-account performance --
    for a in accounts:
        steps.append(("camp05_adgroup", a,
                      [PY, r"campaign_db_scripts\05_add_adgroup_performance.py",
                       "--account", a, "--start", start, "--end", end], False))
        steps.append(("kw03_perf", a,
                      [PY, r"kw_db_scripts\03_sync_keyword_performance.py",
                       "--account", a, "--refresh", str(refresh_days)], False))
        steps.append(("kw05_queries", a,
                      [PY, r"kw_db_scripts\05_sync_search_queries.py",
                       "--account", a, "--refresh", str(refresh_days)], False))
        steps.append(("kw06_quality", a,
                      [PY, r"kw_db_scripts\06_sync_keyword_quality.py",
                       "--account", a, "--refresh", str(refresh_days)], False))
    return steps


def weekly_commands():
    cmds = []
    if os.path.isfile(WEEKLY_FILE):
        for line in open(WEEKLY_FILE, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#"):
                cmds.append(line)
    return cmds


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--accounts", default="home,term_life",
                    help="comma-separated vertical labels (default: home,term_life)")
    ap.add_argument("--days", type=int, default=14,
                    help="rolling window length for date-arg scripts (default 14)")
    ap.add_argument("--refresh", type=int, default=15,
                    help="refresh-days for kw scripts (default 15)")
    ap.add_argument("--weekly", action="store_true",
                    help="also run commands from orchestrator_weekly.txt")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    accounts = [a.strip() for a in args.accounts.split(",") if a.strip()]
    end = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
    start = (datetime.date.today() - datetime.timedelta(days=args.days)).isoformat()

    run_dir = os.path.join(LOGROOT, datetime.date.today().isoformat())
    os.makedirs(run_dir, exist_ok=True)
    env = dict(os.environ, PYTHONIOENCODING="utf-8")

    steps = build_steps(accounts, start, end, args.refresh)
    wk = weekly_commands() if args.weekly else []

    print("=" * 70)
    print("ORCHESTRATOR v2  |  scope: %s  |  window %s..%s  |  refresh %dd%s"
          % (", ".join(accounts), start, end, args.refresh,
             "  |  +%d weekly" % len(wk) if args.weekly else ""))
    print("logs: %s" % run_dir)
    print("=" * 70)

    results = []                 # (name, account, status, seconds)
    failed_structure = set()     # accounts whose structure failed -> perf skipped

    for i, (name, acct, cmd, is_structure) in enumerate(steps, 1):
        label = "%s%s" % (name, ("[%s]" % acct) if acct else "")
        # THE FK LESSON: never write performance for an account whose
        # structure didn't refresh this run.
        if acct in failed_structure and not is_structure:
            print("  %02d %-28s SKIPPED (structure failed for %s)" % (i, label, acct))
            results.append((label, "SKIPPED", 0.0))
            continue
        if None is not acct and acct in failed_structure and is_structure:
            pass  # second structure step for a failed account still attempts
        if args.dry_run:
            print("  %02d %-28s DRY: %s" % (i, label, " ".join(cmd)))
            results.append((label, "DRY", 0.0))
            continue
        logpath = os.path.join(run_dir, "%02d_%s.log" % (i, label.replace("[", "_").replace("]", "")))
        t0 = time.time()
        with open(logpath, "w", encoding="utf-8") as lf:
            rc = subprocess.run(cmd, cwd=ROOT, env=env, stdout=lf,
                                stderr=subprocess.STDOUT).returncode
        dt = time.time() - t0
        status = "OK" if rc == 0 else "FAIL(rc=%d)" % rc
        print("  %02d %-28s %-12s %5.1fs" % (i, label, status, dt))
        results.append((label, status, dt))
        if rc != 0 and is_structure and acct:
            failed_structure.add(acct)

    for j, cmdline in enumerate(wk, 1):
        label = "weekly_%02d" % j
        if args.dry_run:
            print("  W%d %-28s DRY: %s" % (j, label, cmdline))
            results.append((label, "DRY", 0.0)); continue
        logpath = os.path.join(run_dir, "%s.log" % label)
        t0 = time.time()
        with open(logpath, "w", encoding="utf-8") as lf:
            rc = subprocess.run(cmdline, cwd=ROOT, env=env, shell=True,
                                stdout=lf, stderr=subprocess.STDOUT).returncode
        status = "OK" if rc == 0 else "FAIL(rc=%d)" % rc
        print("  W%d %-28s %-12s %5.1fs" % (j, label, status, time.time() - t0))
        results.append((label, status, time.time() - t0))

    fails = [r for r in results if r[1].startswith("FAIL")]
    skips = [r for r in results if r[1] == "SKIPPED"]
    print("=" * 70)
    print("STATUS: %d steps | %d OK | %d FAIL | %d SKIPPED"
          % (len(results), len([r for r in results if r[1] == "OK"]),
             len(fails), len(skips)))
    for label, status, _ in fails + skips:
        print("   %-30s %s" % (label, status))
    if fails:
        print("NON-ZERO EXIT: read the step logs above. A failing structure")
        print("step skips that account's performance steps by design.")
    sys.exit(len(fails))


if __name__ == "__main__":
    main()
