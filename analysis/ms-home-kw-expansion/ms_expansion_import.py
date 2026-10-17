"""
ms_expansion_import.py -- FINAL step: turn the reviewed selection CSV into the
Microsoft Ads Editor import file, replicated across the live target campaigns.

Reads the ROW-level reviewed keywords CSV (decision / kill_reason /
ad_group_override columns honored), optionally annotates each term with its
served verdict from the absorption output, resolves the LIVE campaigns from the
DB (never hardcoded names), and emits:

  - <out>.xlsx      Import sheet (Campaign, Ad Group, Keyword, Match Type, Status)
                    + New Ad Groups sheet (which groups each campaign gains)
                    + Reference sheet (per-term served verdict, source, notes)
  - <out>_import.csv  the same Import rows, Editor-paste-ready

PLACEMENT (per the Setup ruling):
  - default rows -> the tier campaigns named by --tiers (default 1,2)
  - rows whose note routes them to FLORIDA / CALIFORNIA -> that state campaign ONLY
Campaigns are found live: account_id=2, campaigns.status='ENABLED', tier parsed
from the TierN token in the name, CA/FL by name token. If a requested tier has no
live campaign the script aborts loudly.

Keywords import with Status=%s by default; ad groups that don't exist are created
by Editor on import. NOTE: a new group serves NOTHING until its RSAs are added --
build ads before or immediately after posting.

    python ms_expansion_import.py --keywords-csv output\\..._keywords-killkeep.csv ^
                                  --served-csv output\\..._keywords-killkeep_served.csv
""" % "Active"
import argparse
import csv
import os
import re
import sqlite3
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ms_expansion_coverage import normalize_loose

ACCT = 2
TIER_RE = re.compile(r"tier\s*_?(\d)", re.I)


def live_campaigns(conn):
    rows = conn.execute(
        "SELECT campaign_name FROM campaigns WHERE account_id=? AND status='ENABLED'",
        (ACCT,)).fetchall()
    tiers, states = {}, {}
    for (name,) in rows:
        m = TIER_RE.search(name or "")
        if m:
            tiers[int(m.group(1))] = name
        u = (name or "").upper()
        if "FLORIDA" in u:
            states["florida"] = name
        elif "CALIFORNIA" in u:
            states["california"] = name
    return tiers, states


def existing_groups(conn, campaign_name):
    """name -> (ad_group_id, status) for every ad group in this campaign.

    STATUS-AWARE (was a bare name set). Account 2 keeps PAUSED generic
    shells alongside the renamed canonical groups inside the live tier
    campaigns -- e.g. Tier1 holds a paused 'Home-Generic-Home
    Insurance-Core-NA' next to the enabled 'Home-Generic-Home
    Insurance-Core'. A name-only existence check treats that shell as a
    valid target, so keywords import Active into a paused ad group and
    silently never serve. Returning status lets the caller separate
    NEW / SERVING / PAUSED targets and refuse the paused ones.
    """
    out = {}
    for r in conn.execute(
        "SELECT ag.ad_group_name, ag.ad_group_id, ag.status FROM ad_groups ag "
        "JOIN campaigns c ON c.campaign_id=ag.campaign_id AND c.account_id=ag.account_id "
        "WHERE ag.account_id=? AND c.campaign_name=?", (ACCT, campaign_name)):
        name, gid, st = r[0], r[1], (r[2] or "").upper()
        cur = out.get(name)
        if cur is None:
            out[name] = (gid, st)
        elif st == "ENABLED" and cur[1] != "ENABLED":
            print("  [note] duplicate ad-group name %r in %s: preferring ENABLED id=%s over %s/%s"
                  % (name, campaign_name[:34], gid, cur[0], cur[1]))
            out[name] = (gid, st)
        elif st != "ENABLED" and cur[1] == "ENABLED":
            print("  [note] duplicate ad-group name %r in %s: keeping ENABLED id=%s, ignoring %s/%s"
                  % (name, campaign_name[:34], cur[0], gid, st))
        else:
            print("  [warn] duplicate ad-group name %r in %s with SAME status tier (%s): keeping id=%s, "
                  "ignoring id=%s -- resolve by renaming one" % (name, campaign_name[:34], st, cur[0], gid))
    return out


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--keywords-csv", required=True, help="reviewed row-level selection CSV")
    p.add_argument("--served-csv", default=None, help="absorption *_served.csv for reference annotation")
    p.add_argument("--db", default=r"C:\Projects\google-ads-ops\db\paid_search.db")
    p.add_argument("--tiers", default="1,2,3,5", help="tier campaigns to build in (default: all live tiers 1,2,3,5)")
    p.add_argument("--states", default="all", choices=["all", "flagged"],
                   help="all (default): every row also lands in the live CA/FL campaigns; "
                        "flagged: only state-flagged rows go to state campaigns")
    p.add_argument("--allow-paused-groups", action="store_true",
                   help="post even when a target ad group is PAUSED (default: abort). "
                        "You must enable those groups manually or the keywords never serve.")
    p.add_argument("--status", default="Active", choices=["Active", "Paused"],
                   help="import status for new keywords (Paused = post now, enable after RSAs)")
    p.add_argument("--out", default=r"C:\Projects\google-ads-ops\analysis\ms-home-kw-expansion"
                                    r"\output\ms_expansion_import_%s.xlsx" % date.today().isoformat())
    a = p.parse_args()

    conn = sqlite3.connect("file:%s?mode=ro" % a.db, uri=True)
    tiers, states = live_campaigns(conn)
    want = [int(x) for x in a.tiers.split(",") if x.strip()]
    missing = [t for t in want if t not in tiers]
    if missing:
        sys.exit("ABORT: no LIVE campaign found for tier(s) %s. Live tiers: %s"
                 % (missing, sorted(tiers)))
    tier_camps = [tiers[t] for t in want]
    print("target campaigns: %s" % " | ".join(tier_camps))
    print("state campaigns : FL=%s | CA=%s" % (states.get("florida", "(none)"),
                                               states.get("california", "(none)")))

    served = {}
    if a.served_csv and os.path.exists(a.served_csv):
        for r in csv.DictReader(open(a.served_csv, newline="", encoding="utf-8-sig")):
            served[normalize_loose(r.get("keyword") or "")] = r

    rows, killed, ref = [], 0, []
    with open(a.keywords_csv, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            kw = (row.get("keyword") or "").strip()
            if not kw:
                continue
            if (row.get("decision") or "").strip().upper().startswith("KILL"):
                killed += 1
                continue
            gname = (row.get("ad_group_override") or "").strip() or (row.get("ad_group") or "").strip()
            note = (row.get("note") or "")
            lo = normalize_loose(kw)
            sv = served.get(lo, {})
            if "FLORIDA" in note.upper():
                camps = [states["florida"]] if "florida" in states else []
                placement = "FLORIDA campaign"
            elif "CALIFORNIA" in note.upper():
                camps = [states["california"]] if "california" in states else []
                placement = "CALIFORNIA campaign"
            else:
                camps = list(tier_camps)
                placement = "tiers %s" % ",".join(str(t) for t in want)
                if a.states == "all":
                    camps += [states[s] for s in ("florida", "california") if s in states]
                    placement = "all live campaigns (tiers %s + FL/CA)" % ",".join(str(t) for t in want)
            if not camps:
                sys.exit("ABORT: row '%s' routes to a state campaign that is not live." % kw)
            for c in camps:
                rows.append({"Campaign": c, "Ad Group": gname, "Keyword": kw,
                             "Match Type": (row.get("match_type") or "").capitalize(),
                             "Status": a.status})
            ref.append({"keyword": kw, "ad_group": gname, "match_type": row.get("match_type"),
                        "placement": placement, "module": row.get("module"),
                        "served_verdict": sv.get("served_verdict", "(no served data)"),
                        "served_impr": sv.get("served_impr", ""),
                        "served_clicks": sv.get("served_clicks", ""),
                        "override_applied": "yes" if (row.get("ad_group_override") or "").strip() else "",
                        "note": note})
    rows.sort(key=lambda r: (r["Campaign"], r["Ad Group"], r["Keyword"], r["Match Type"]))
    print("import rows: %d (from %d kept keyword rows; %d killed rows skipped)"
          % (len(rows), len(ref), killed))

    # which (campaign, ad group) pairs are NEW -- Editor will create them on import
    new_groups, paused_targets, serving_targets = [], [], []
    seen_pairs = {(r["Campaign"], r["Ad Group"]) for r in rows}
    eg_cache = {}
    for camp, grp in sorted(seen_pairs):
        if camp not in eg_cache:
            eg_cache[camp] = existing_groups(conn, camp)
        nkw = sum(1 for r in rows if r["Campaign"] == camp and r["Ad Group"] == grp)
        hit = eg_cache[camp].get(grp)
        if hit is None:
            new_groups.append({"Campaign": camp, "New Ad Group": grp, "keywords": nkw,
                               "note": "created on import; NO ads yet -- will not serve until RSAs added"})
        elif hit[1] == "ENABLED":
            serving_targets.append({"Campaign": camp, "Ad Group": grp,
                                    "ad_group_id": hit[0], "status": hit[1], "keywords": nkw})
        else:
            paused_targets.append({"Campaign": camp, "Ad Group": grp,
                                   "ad_group_id": hit[0], "status": hit[1], "keywords": nkw})
    conn.close()

    print("new ad groups to be created: %d" % len(new_groups))
    for g in new_groups:
        print("    %-42s +%d kw   [%s]" % (g["New Ad Group"], g["keywords"], g["Campaign"][:36]))
    print("existing SERVING target groups: %d" % len(serving_targets))
    for g in serving_targets:
        print("    %-42s +%d kw   id=%s  [%s]"
              % (g["Ad Group"], g["keywords"], g["ad_group_id"], g["Campaign"][:36]))

    if paused_targets:
        print("")
        print("!" * 78)
        print("ABORT: %d target (campaign, ad group) pair(s) resolve to a PAUSED ad group."
              % len(paused_targets))
        print("Keywords would import as Active into a paused group and NEVER SERVE.")
        print("These are almost certainly pre-rename shells -- check the ad_group values")
        print("in the reviewed CSV against the canonical (renamed) group names.")
        print("!" * 78)
        for g in paused_targets:
            print("    %-42s %-8s id=%-17s +%d kw  [%s]"
                  % (g["Ad Group"], g["status"], g["ad_group_id"], g["keywords"], g["Campaign"][:36]))
        if not a.allow_paused_groups:
            sys.exit("Aborted. Fix the ad_group names, or pass --allow-paused-groups "
                     "to post anyway (you must then enable those groups manually).")
        print("--allow-paused-groups set: continuing. ENABLE THESE GROUPS MANUALLY after import.")

    # ---- outputs ----
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.utils import get_column_letter
    HEADF = PatternFill("solid", fgColor="1F3864")
    WHITE = Font(name="Arial", bold=True, color="FFFFFF", size=10)
    BOLD = Font(name="Arial", bold=True, size=10)
    REG = Font(name="Arial", size=10)
    NOTE = Font(name="Arial", size=9, italic=True, color="595959")
    TITLE = Font(name="Arial", bold=True, size=13, color="1F3864")

    def sheet_from(wb, title, heading, cols, dicts, widths, note=None):
        ws = wb.create_sheet(title)
        ws["A1"] = heading; ws["A1"].font = TITLE
        ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=len(cols))
        r0 = 3
        if note:
            ws["A2"] = note; ws["A2"].font = NOTE
            ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=len(cols))
            r0 = 4
        for j, h in enumerate(cols, 1):
            cell = ws.cell(r0, j, h); cell.fill = HEADF; cell.font = WHITE
            cell.alignment = Alignment(horizontal="center", wrap_text=True)
        r = r0 + 1
        for d in dicts:
            for j, k in enumerate(cols, 1):
                ws.cell(r, j, d.get(k)).font = BOLD if j <= 2 else REG
            r += 1
        for j, w in enumerate(widths, 1):
            ws.column_dimensions[get_column_letter(j)].width = w
        ws.freeze_panes = ws.cell(r0 + 1, 1).coordinate
        ws.auto_filter.ref = "A%d:%s%d" % (r0, get_column_letter(len(cols)), max(r - 1, r0))

    wb = Workbook(); wb.remove(wb.active)
    sheet_from(wb, "Import", "Editor import rows (status=%s)" % a.status,
               ["Campaign", "Ad Group", "Keyword", "Match Type", "Status"], rows,
               [44, 40, 34, 11, 9],
               note="Paste into MS Ads Editor (or import the _import.csv). New ad groups are "
                    "created on import and serve NOTHING until RSAs are added.")
    sheet_from(wb, "New Ad Groups", "Ad groups this import creates",
               ["Campaign", "New Ad Group", "keywords", "note"], new_groups,
               [44, 40, 9, 56])
    sheet_from(wb, "Target Groups", "Existing ad groups these keywords land in (resolved by id)",
               ["Campaign", "Ad Group", "ad_group_id", "status", "keywords"],
               serving_targets + paused_targets, [44, 40, 20, 9, 9],
               note="Resolved from ad_groups at generation time. status must be ENABLED; "
                    "a PAUSED row means the keywords would not serve.")
    sheet_from(wb, "Reference", "Per-row provenance: placement, served verdict, overrides",
               ["keyword", "match_type", "ad_group", "placement", "module",
                "served_verdict", "served_impr", "served_clicks", "override_applied", "note"],
               ref, [34, 10, 40, 18, 8, 20, 9, 9, 8, 44],
               note="Served-side data only (Jan-Aug backfill); verdicts from ms_expansion_absorption.py.")
    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    wb.save(a.out)
    base = os.path.splitext(a.out)[0]
    with open(base + "_import.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["Campaign", "Ad Group", "Keyword", "Match Type", "Status"])
        w.writeheader(); w.writerows(rows)
    print("\nwrote %s and %s_import.csv" % (a.out, base))
    print("REMINDERS: RSAs before/immediately after posting; log the change (%s); "
          "watch week-1 CPC vs tCPA; expect brand-group query bleed (costo/SelectQuote/etc.) to drop." % "restr-2026-09")


if __name__ == "__main__":
    main()
