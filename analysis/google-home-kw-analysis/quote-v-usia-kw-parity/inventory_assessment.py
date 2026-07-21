#!/usr/bin/env python3
"""Inventory assessment for the Quote.com Google Home account (account_id=1). READ-ONLY.

Maps the current ad-group structure across ACTIVE campaigns so carrier keyword additions can be placed
correctly (dedicated vs consolidated ad group) given the 7-way campaign duplication. The seven-way volume
split is surfaced explicitly, because that's the input to the "does this carrier earn its own ad group"
signal-density decision.

Outputs (scoped to account_id=1, ENABLED campaigns only):
  - AdGroup_Logical  : by ad_group_name (collapsing duplicated campaigns) -> kw count, carriers, theme mix,
                       match mix, TOTAL leads + per-campaign lead split (min/avg/max) = the signal input
  - AdGroup_Physical : campaign x ad_group detail
  - Campaign_Summary : per active campaign totals
  - Carrier_Coverage : per carrier -> which ad groups, kw count, bare vs home variants, leads (bridge to adds)

Conventions carried from the parity work: join on id AND account_id (never id alone); device='ALL' rollup
row for performance; primary_conv_count as the truth lead metric; trusted brand tagging (registry), not 08.

Claude is isolated from the live DB; this is validated on a synthetic fixture. Run locally against
C:\\Projects\\google-ads-ops\\db\\paid_search.db and eyeball the schema banner it prints first.
"""
import argparse
import os
import sqlite3
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from parity_common import make_brand_tagger, brand_of, categorize  # noqa: E402

ACCOUNT_ID = 1
HOME_THEMES_KEEP = ("CORE", "GENERIC_HOME")  # the two we care about per carrier; others bucketed 'OTHER'


# --------------------------------------------------------------------------- db helpers
def ro_connect(db):
    if not os.path.exists(db):
        sys.exit("DB not found: %s" % db)
    conn = sqlite3.connect("file:%s?mode=ro" % db, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def table_cols(conn, table):
    try:
        return [r[1] for r in conn.execute("PRAGMA table_info(%s)" % table)]
    except sqlite3.Error:
        return []


def pick(colnames, *candidates):
    """Return the first candidate present in colnames (case-insensitive), else None."""
    low = {c.lower(): c for c in colnames}
    for cand in candidates:
        if cand.lower() in low:
            return low[cand.lower()]
    return None


def find_perf_table(conn):
    """Find a keyword/ad-group performance table exposing leads + ad_group_id + device + a date."""
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    best = None
    for t in tables:
        c = table_cols(conn, t)
        low = [x.lower() for x in c]
        if "primary_conv_count" in low and "ad_group_id" in low and "device" in low and \
           any(d in low for d in ("report_date", "date", "day")):
            # prefer an ad-group-grained table over a keyword-grained one (fewer rows, same answer)
            score = (2 if "keyword" not in t.lower() else 1)
            if not best or score > best[2]:
                best = (t, c, score)
    return (best[0], best[1]) if best else (None, None)


# --------------------------------------------------------------------------- schema resolution
def resolve_schema(conn, perf_override=None):
    s = {}
    ki = table_cols(conn, "keyword_instances")
    ag = table_cols(conn, "ad_groups")
    cp = table_cols(conn, "campaigns")
    if not (ki and ag and cp):
        sys.exit("Missing one of keyword_instances / ad_groups / campaigns. Found: "
                 "ki=%s ag=%s cp=%s" % (bool(ki), bool(ag), bool(cp)))
    s["ki_status"] = pick(ki, "status")
    s["ki_match"] = pick(ki, "match_type", "match")
    s["ki_text"] = pick(ki, "keyword_text", "criterion", "text")
    s["ki_agid"] = pick(ki, "ad_group_id")
    s["ki_cid"] = pick(ki, "campaign_id")
    s["ki_criterion"] = pick(ki, "criterion_id")
    s["ki_id"] = pick(ki, "id", "rowid")
    s["ag_id"] = pick(ag, "ad_group_id")
    s["ag_cid"] = pick(ag, "campaign_id")
    s["ag_status"] = pick(ag, "status")
    s["ag_name"] = pick(ag, "ad_group_name", "name")
    s["cp_id"] = pick(cp, "campaign_id")
    s["cp_status"] = pick(cp, "status")
    s["cp_name"] = pick(cp, "campaign_name", "name")
    missing = [k for k, v in s.items() if v is None and not k.endswith("_name")]
    if missing:
        sys.exit("Could not resolve required columns: %s\n keyword_instances=%s\n ad_groups=%s\n "
                 "campaigns=%s" % (missing, ki, ag, cp))
    if perf_override:
        perf_c = table_cols(conn, perf_override)
        perf_t = perf_override if perf_c else None
        if not perf_t:
            print("  [WARN] --perf-table '%s' not found; falling back to auto-detect." % perf_override)
            perf_t, perf_c = find_perf_table(conn)
    else:
        perf_t, perf_c = find_perf_table(conn)
    s["perf_table"] = perf_t
    if perf_t:
        s["perf_agid"] = pick(perf_c, "ad_group_id")
        s["perf_leads"] = pick(perf_c, "primary_conv_count")
        s["perf_device"] = pick(perf_c, "device")
        s["perf_date"] = pick(perf_c, "report_date", "date", "day")
    return s


def banner(s):
    print("=" * 78)
    print("SCHEMA RESOLVED (verify before trusting output):")
    print("  keyword_instances: status=%s match=%s text=%s ad_group_id=%s campaign_id=%s"
          % (s["ki_status"], s["ki_match"], s["ki_text"], s["ki_agid"], s["ki_cid"]))
    print("  keyword_instances GRAIN: one row per instance (criterion_id=%s NOT unique per instance -> "
          "no GROUP BY); status column not read, filtered to active-campaign ad groups"
          % s["ki_criterion"])
    print("  ad_groups: id=%s campaign_id=%s status=%s name=%s"
          % (s["ag_id"], s["ag_cid"], s["ag_status"], s["ag_name"]))
    print("  campaigns: id=%s status=%s name=%s" % (s["cp_id"], s["cp_status"], s["cp_name"]))
    if s["perf_table"]:
        print("  leads from: %s (ad_group_id=%s, leads=%s, device=%s, date=%s)"
              % (s["perf_table"], s["perf_agid"], s["perf_leads"], s["perf_device"], s["perf_date"]))
    else:
        print("  leads: NO keyword/ad-group performance table found -> structure only, leads=0. "
              "Pass --perf-table to point at it.")
    print("=" * 78)


# --------------------------------------------------------------------------- loaders
def load_active_campaigns(conn, s):
    q = ("SELECT %s AS cid, %s AS status, %s AS name FROM campaigns WHERE account_id=? AND %s='ENABLED'"
         % (s["cp_id"], s["cp_status"], s["cp_name"] or "''", s["cp_status"]))
    return {str(r["cid"]).strip("[]"): (r["name"] or str(r["cid"])) for r in conn.execute(q, (ACCOUNT_ID,))}


def load_adgroups(conn, s, active_cids):
    q = ("SELECT %s AS agid, %s AS cid, %s AS status, %s AS name FROM ad_groups WHERE account_id=?"
         % (s["ag_id"], s["ag_cid"], s["ag_status"], s["ag_name"] or "''"))
    out = {}
    for r in conn.execute(q, (ACCOUNT_ID,)):
        cid = str(r["cid"]).strip("[]")
        if cid in active_cids:
            out[str(r["agid"])] = dict(cid=cid, status=r["status"], name=(r["name"] or str(r["agid"])))
    return out


def load_leads_by_agid(conn, s, days):
    if not s["perf_table"]:
        return {}
    date_pred = "%s >= date('now', ?)" % s["perf_date"]
    q = ("SELECT %s AS agid, SUM(%s) AS leads FROM %s WHERE %s='ALL' AND %s GROUP BY %s"
         % (s["perf_agid"], s["perf_leads"], s["perf_table"], s["perf_device"], date_pred, s["perf_agid"]))
    out = {}
    for r in conn.execute(q, ("-%d day" % days,)):
        out[str(r["agid"])] = float(r["leads"] or 0)
    return out


def load_current_keywords(conn, s):
    """Read the architect's corrected current-state view `keyword_current` (match_type fixed after the
    enum-inversion bug; is_serving + ad_group_criterion_id reliable). One row per placement. Falls back
    to raw keyword_instances only if the view is absent (pre-remediation), with a loud warning."""
    view_cols = [r[1] for r in conn.execute("PRAGMA table_info(keyword_current)")]
    if view_cols:
        return conn.execute(
            "SELECT criterion_id, ad_group_criterion_id, keyword_text, match_type, campaign_id, "
            "ad_group_id, ad_group_name, campaign_name, keyword_status, hierarchy_enabled, is_serving "
            "FROM keyword_current WHERE account_id=? AND campaign_status='ENABLED'", (ACCOUNT_ID,))
    print("  [WARN] keyword_current view not found -> falling back to raw keyword_instances; match_type "
          "may be uncorrected (enum-inversion bug). Point at keyword_current for trustworthy match_type.")
    q = ("SELECT {crit} AS criterion_id, ({agid}||'~'||{crit}) AS ad_group_criterion_id, "
         "{text} AS keyword_text, {match} AS match_type, {cid} AS campaign_id, {agid} AS ad_group_id, "
         "'' AS ad_group_name, '' AS campaign_name, NULL AS keyword_status, NULL AS hierarchy_enabled, "
         "NULL AS is_serving FROM keyword_instances WHERE account_id=?"
         ).format(crit=(s["ki_criterion"] or "NULL"), text=s["ki_text"], match=s["ki_match"],
                  cid=s["ki_cid"], agid=s["ki_agid"])
    return conn.execute(q, (ACCOUNT_ID,))


# --------------------------------------------------------------------------- aggregation
def build_inventory(conn, s, tagger, days):
    active_cids = load_active_campaigns(conn, s)
    adgroups = load_adgroups(conn, s, active_cids)
    leads_by_agid = load_leads_by_agid(conn, s, days)

    # per physical ad group (ad_group_id) -- deduped, hierarchy-live keywords only
    phys = {}
    carrier_agg = {}  # brand -> kw counts + bare/home split + ad-group membership (placement bridge)
    for r in load_current_keywords(conn, s):
        agid = str(r["ad_group_id"])
        ag = adgroups.get(agid)
        if not ag:  # not in an active campaign's ad group
            continue
        text = r["keyword_text"] or ""
        brand = brand_of(tagger, text)[0]  # (brand, bid_status, category, dnb_flag, source)
        theme = categorize(text)
        mt = (r["match_type"] or "").upper()
        p = phys.setdefault(agid, dict(
            agid=agid, cid=ag["cid"], campaign=active_cids.get(ag["cid"], ag["cid"]),
            ad_group=ag["name"], ag_status=ag["status"],
            kw_total=0, kw_enabled=0, kw_paused=0,
            match=defaultdict(int), theme=defaultdict(int),
            carriers=set(), leads=leads_by_agid.get(agid, 0.0)))
        p["kw_total"] += 1
        p["kw_enabled"] += 1  # all deduped current keywords are hierarchy-live (status column not trusted)
        p["match"][mt or "UNKNOWN"] += 1
        p["theme"][theme if theme in HOME_THEMES_KEEP else "OTHER"] += 1
        if brand:
            p["carriers"].add(brand)
            ca = carrier_agg.setdefault(brand, dict(kw=0, core=0, generic_home=0, ad_groups=set()))
            ca["kw"] += 1
            if theme == "CORE":
                ca["core"] += 1
            elif theme == "GENERIC_HOME":
                ca["generic_home"] += 1
            ca["ad_groups"].add(ag["name"])
    return list(phys.values()), active_cids, carrier_agg


def logical_view(phys):
    by_name = defaultdict(list)
    for p in phys:
        by_name[p["ad_group"]].append(p)
    out = []
    for name, group in by_name.items():
        leads = [g["leads"] for g in group]
        carriers = set().union(*[g["carriers"] for g in group]) if group else set()
        kw_counts = [g["kw_total"] for g in group]
        theme = defaultdict(int)
        match = defaultdict(int)
        for g in group:
            for k, v in g["theme"].items():
                theme[k] += v
            for k, v in g["match"].items():
                match[k] += v
        n = len(group)
        out.append(dict(
            ad_group=name, n_campaigns=n,
            kw_per_campaign=round(sum(kw_counts) / n, 1) if n else 0,
            kw_consistent=(min(kw_counts) == max(kw_counts)) if kw_counts else True,
            distinct_carriers=len(carriers),
            carriers=", ".join(sorted(carriers)[:8]) + ("..." if len(carriers) > 8 else ""),
            core=theme.get("CORE", 0), generic_home=theme.get("GENERIC_HOME", 0), other=theme.get("OTHER", 0),
            exact=match.get("EXACT", 0), phrase=match.get("PHRASE", 0), broad=match.get("BROAD", 0),
            total_leads=round(sum(leads), 1),
            leads_min=round(min(leads), 1) if leads else 0,
            leads_avg=round(sum(leads) / n, 1) if n else 0,
            leads_max=round(max(leads), 1) if leads else 0))
    out.sort(key=lambda d: -d["total_leads"])
    return out


def campaign_summary(phys, active_cids):
    by_cid = defaultdict(lambda: dict(ad_groups=0, kw=0, leads=0.0))
    for p in phys:
        c = by_cid[p["cid"]]
        c["ad_groups"] += 1
        c["kw"] += p["kw_total"]
        c["leads"] += p["leads"]
    out = [dict(campaign=active_cids.get(cid, cid), ad_groups=v["ad_groups"], keywords=v["kw"],
                leads=round(v["leads"], 1)) for cid, v in by_cid.items()]
    out.sort(key=lambda d: -d["leads"])
    return out


def _norm_match(mt):
    u = (mt or "").upper()
    if "EXACT" in u:
        return "Exact"
    if "PHRASE" in u:
        return "Phrase"
    if "BROAD" in u:
        return "Broad"
    return (mt or "").capitalize()


def build_pause_split(conn, s, tagger, carriers, grabbag):
    """Keyword rows to PAUSE for split carriers, emitted in the shape Editor matches on: separate
    Campaign ID / Ad group ID / Keyword ID, where Keyword ID is the numeric criterion_id split out of
    `ad_group_criterion_id` ('{ad_group_id}~{criterion_id}'). Editor matches an edit by (Ad group ID,
    Keyword ID), so it edits in place instead of creating a duplicate. match_type is the CORRECTED value
    from keyword_current; only currently-serving keywords are paused."""
    want = [c.strip().lower() for c in carriers if c.strip()]
    rows = []
    touched = defaultdict(set)
    for r in load_current_keywords(conn, s):
        if not r["hierarchy_enabled"]:
            continue  # gate on campaign+ad group ENABLED (reliable); keyword_status is still unreliable,
            #            so pause every carrier keyword in a live grab-bag -- pausing an already-paused one
            #            is a no-op, and this avoids missing keywords the corrupt status marks 'PAUSED'
        agn = r["ad_group_name"] or ""
        if grabbag and agn != grabbag:
            continue
        text = r["keyword_text"] or ""
        brand = brand_of(tagger, text)[0]
        matched = next((w for w in want if brand and w in brand.lower()), None)
        if not matched:
            continue
        if not grabbag and matched in agn.lower():
            continue  # skip the carrier's own dedicated ad group (grab-bags only)
        agcid = r["ad_group_criterion_id"] or ""
        ag_id, _, crit = agcid.partition("~")
        rows.append(dict(ad_group_criterion_id=agcid,
                         campaign_id=r["campaign_id"], ad_group_id=(ag_id or r["ad_group_id"]),
                         keyword_id=(crit or r["criterion_id"]), campaign=(r["campaign_name"] or ""),
                         ad_group=agn, keyword=text, match_type=_norm_match(r["match_type"]), brand=brand))
        touched[matched].add(agn)
    for c in sorted(touched):
        print("    %s -> pausing in: %s" % (c, ", ".join(sorted(touched[c]))))
    rows.sort(key=lambda d: (d["brand"], d["keyword"], d["campaign"]))
    return rows, 0


def carrier_coverage(carrier_agg, phys):
    """Per carrier: kw count, bare (CORE) vs home (GENERIC_HOME) presence, which ad groups, approx leads.
    'bare'=CORE keyword count for the carrier; 'home'=GENERIC_HOME count. Approx leads splits each ad
    group's leads evenly across the carriers in it (ad-group grain can't attribute leads to a carrier)."""
    leads = defaultdict(float)
    for p in phys:
        if p["carriers"]:
            share = p["leads"] / len(p["carriers"])
            for b in p["carriers"]:
                leads[b] += share
    out = [dict(carrier=b, kw=v["kw"], bare_core=v["core"], home_generic=v["generic_home"],
                n_ad_groups=len(v["ad_groups"]), ad_groups=", ".join(sorted(v["ad_groups"])),
                approx_leads=round(leads.get(b, 0.0), 1))
           for b, v in carrier_agg.items()]
    out.sort(key=lambda d: -d["approx_leads"])
    return out


# --------------------------------------------------------------------------- output
def write_outputs(out_dir, logical, phys, camps, carriers, pause_rows=None):
    os.makedirs(out_dir, exist_ok=True)
    import csv

    def dump(name, header, rows):
        with open(os.path.join(out_dir, name), "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(header)
            w.writerows(rows)

    dump("adgroup_logical.csv",
         ["ad_group", "n_campaigns", "kw_per_campaign", "kw_consistent", "distinct_carriers", "carriers",
          "core", "generic_home", "other", "exact", "phrase", "broad",
          "total_leads", "leads_min", "leads_avg", "leads_max"],
         [[d["ad_group"], d["n_campaigns"], d["kw_per_campaign"], d["kw_consistent"], d["distinct_carriers"],
           d["carriers"], d["core"], d["generic_home"], d["other"], d["exact"], d["phrase"], d["broad"],
           d["total_leads"], d["leads_min"], d["leads_avg"], d["leads_max"]] for d in logical])
    dump("adgroup_physical.csv",
         ["campaign", "ad_group", "ag_status", "kw_total", "kw_enabled", "kw_paused",
          "distinct_carriers", "core", "generic_home", "other", "exact", "phrase", "broad", "leads"],
         [[p["campaign"], p["ad_group"], p["ag_status"], p["kw_total"], p["kw_enabled"], p["kw_paused"],
           len(p["carriers"]), p["theme"].get("CORE", 0), p["theme"].get("GENERIC_HOME", 0),
           p["theme"].get("OTHER", 0), p["match"].get("EXACT", 0), p["match"].get("PHRASE", 0),
           p["match"].get("BROAD", 0), round(p["leads"], 1)]
          for p in sorted(phys, key=lambda x: (x["campaign"], -x["leads"]))])
    dump("campaign_summary.csv", ["campaign", "ad_groups", "keywords", "leads"],
         [[c["campaign"], c["ad_groups"], c["keywords"], c["leads"]] for c in camps])
    dump("carrier_coverage.csv",
         ["carrier", "kw", "bare_core", "home_generic", "n_ad_groups", "ad_groups", "approx_leads"],
         [[c["carrier"], c["kw"], c["bare_core"], c["home_generic"], c["n_ad_groups"], c["ad_groups"],
           c["approx_leads"]] for c in carriers])

    if pause_rows:
        # NAME-based, matching the additions format Editor handles cleanly. Match types are correct now
        # (corrected DB), so Editor locates by Campaign/Ad Group name and matches the keyword by text+type
        # -> edits in place, no phantom campaign from unmappable IDs.
        dump("pause_split.csv",
             ["Campaign", "Ad Group", "Keyword", "Match Type", "Status"],
             [[r["campaign"], r["ad_group"], r["keyword"], r["match_type"], "Paused"] for r in pause_rows])
        # review sheet keeps the IDs + brand for auditing
        dump("pause_split_review.csv",
             ["Campaign", "Ad Group", "Ad group ID", "Keyword ID", "Keyword", "Match Type", "Brand"],
             [[r["campaign"], r["ad_group"], r["ad_group_id"], r["keyword_id"], r["keyword"],
               r["match_type"], r["brand"]] for r in pause_rows])

    # xlsx (optional, best-effort)
    try:
        import openpyxl
        from openpyxl.styles import Font, PatternFill
        wb = openpyxl.Workbook()
        wb.remove(wb.active)
        hdr_font = Font(bold=True, color="FFFFFF")
        hdr_fill = PatternFill("solid", fgColor="365F91")

        def sheet(name, header, rows):
            ws = wb.create_sheet(name)
            ws.append(header)
            for c in ws[1]:
                c.font = hdr_font
                c.fill = hdr_fill
            for r in rows:
                ws.append(r)
            ws.freeze_panes = "A2"
        sheet("AdGroup_Logical",
              ["Ad group", "# campaigns", "kw/campaign", "consistent?", "carriers", "carrier list",
               "CORE", "GEN_HOME", "OTHER", "EXACT", "PHRASE", "BROAD",
               "Total leads", "Leads min", "Leads avg", "Leads max"],
              [[d["ad_group"], d["n_campaigns"], d["kw_per_campaign"], d["kw_consistent"],
                d["distinct_carriers"], d["carriers"], d["core"], d["generic_home"], d["other"],
                d["exact"], d["phrase"], d["broad"], d["total_leads"], d["leads_min"], d["leads_avg"],
                d["leads_max"]] for d in logical])
        sheet("AdGroup_Physical",
              ["Campaign", "Ad group", "AG status", "kw total", "kw enabled", "kw paused",
               "carriers", "CORE", "GEN_HOME", "OTHER", "EXACT", "PHRASE", "BROAD", "Leads"],
              [[p["campaign"], p["ad_group"], p["ag_status"], p["kw_total"], p["kw_enabled"], p["kw_paused"],
                len(p["carriers"]), p["theme"].get("CORE", 0), p["theme"].get("GENERIC_HOME", 0),
                p["theme"].get("OTHER", 0), p["match"].get("EXACT", 0), p["match"].get("PHRASE", 0),
                p["match"].get("BROAD", 0), round(p["leads"], 1)]
               for p in sorted(phys, key=lambda x: (x["campaign"], -x["leads"]))])
        sheet("Campaign_Summary", ["Campaign", "Ad groups", "Keywords", "Leads"],
              [[c["campaign"], c["ad_groups"], c["keywords"], c["leads"]] for c in camps])
        sheet("Carrier_Coverage",
              ["Carrier", "kw", "Bare (CORE)", "Home (GEN)", "# ad groups", "Ad groups", "Approx leads"],
              [[c["carrier"], c["kw"], c["bare_core"], c["home_generic"], c["n_ad_groups"], c["ad_groups"],
                c["approx_leads"]] for c in carriers])
        if pause_rows:
            sheet("Pause_Split",
                  ["Campaign ID", "Ad group ID", "Keyword ID", "Keyword", "Match Type", "Status", "Brand"],
                  [[r["campaign_id"], r["ad_group_id"], r["keyword_id"], r["keyword"], r["match_type"],
                    "Paused", r["brand"]] for r in pause_rows])
        wb.save(os.path.join(out_dir, "inventory_assessment.xlsx"))
    except ImportError:
        print("  (openpyxl not available; wrote CSVs only)")


# --------------------------------------------------------------------------- main
def run(db, days, out_dir, brand_db, perf_table=None, pause_carriers=None, pause_grabbag=None):
    conn = ro_connect(db)
    print("opening DB (read-only): %s" % os.path.abspath(db))
    s = resolve_schema(conn, perf_table)
    banner(s)
    tagger = make_brand_tagger(brand_db or db)
    phys, active_cids, carrier_agg = build_inventory(conn, s, tagger, days)
    logical = logical_view(phys)
    camps = campaign_summary(phys, active_cids)
    carriers = carrier_coverage(carrier_agg, phys)
    pause_rows, pause_already = ([], 0)
    if pause_carriers:
        pause_rows, pause_already = build_pause_split(conn, s, tagger, pause_carriers, pause_grabbag)
    conn.close()
    print("active campaigns: %d | ad groups (physical): %d | logical ad groups: %d | carriers seen: %d"
          % (len(active_cids), len(phys), len(logical), len(carriers)))
    print("top logical ad groups by leads: " + ", ".join(
        "%s(%.0f, %d kw, %d carr)" % (d["ad_group"][:20], d["total_leads"], d["kw_per_campaign"],
                                      d["distinct_carriers"]) for d in logical[:5]))
    if pause_carriers:
        print("pause-split: %d ENABLED grab-bag keyword rows for %s (%d already paused, skipped)"
              % (len(pause_rows), ",".join(pause_carriers), pause_already))
    write_outputs(out_dir, logical, phys, camps, carriers, pause_rows)
    print("wrote -> %s" % os.path.abspath(out_dir))


def main():
    ap = argparse.ArgumentParser(description="Read-only inventory assessment (account_id=1, active campaigns)")
    ap.add_argument("--db", required=True)
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--out-dir", default="inventory_output")
    ap.add_argument("--brand-db", default=None, help="registry DB for brand tagging (default: --db)")
    ap.add_argument("--perf-table", default=None, help="force the keyword/ad-group leads table name")
    ap.add_argument("--pause-split-carriers", default=None,
                    help="comma-separated carriers being split into their own ad groups; emits the exact "
                         "grab-bag keywords to pause (e.g. 'Kin,Costco')")
    ap.add_argument("--pause-grabbag", default=None,
                    help="restrict pause to this grab-bag ad group; omit to AUTO-DETECT where each "
                         "carrier lives (recommended -- carriers can live in different grab-bags)")
    a = ap.parse_args()
    pause_carriers = [c for c in (a.pause_split_carriers or "").split(",") if c.strip()] or None
    run(a.db, a.days, a.out_dir, a.brand_db, a.perf_table, pause_carriers, a.pause_grabbag)


if __name__ == "__main__":
    main()
