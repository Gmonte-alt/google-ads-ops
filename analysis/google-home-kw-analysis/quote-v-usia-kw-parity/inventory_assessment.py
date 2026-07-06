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


def load_instances(conn, s):
    q = ("SELECT %s AS agid, %s AS cid, %s AS status, %s AS match, %s AS text "
         "FROM keyword_instances WHERE account_id=?"
         % (s["ki_agid"], s["ki_cid"], s["ki_status"], s["ki_match"], s["ki_text"]))
    for r in conn.execute(q, (ACCOUNT_ID,)):
        yield r


# --------------------------------------------------------------------------- aggregation
def build_inventory(conn, s, tagger, days):
    active_cids = load_active_campaigns(conn, s)
    adgroups = load_adgroups(conn, s, active_cids)
    leads_by_agid = load_leads_by_agid(conn, s, days)

    # per physical ad group (ad_group_id)
    phys = {}
    carrier_agg = {}  # brand -> kw counts + bare/home split + ad-group membership (placement bridge)
    for r in load_instances(conn, s):
        agid = str(r["agid"])
        ag = adgroups.get(agid)
        if not ag:  # instance not in an active campaign's ad group
            continue
        text = r["text"] or ""
        brand = brand_of(tagger, text)[0]  # (brand, bid_status, category, dnb_flag, source)
        theme = categorize(text)
        mt = (r["match"] or "").upper()
        p = phys.setdefault(agid, dict(
            agid=agid, cid=ag["cid"], campaign=active_cids.get(ag["cid"], ag["cid"]),
            ad_group=ag["name"], ag_status=ag["status"],
            kw_total=0, kw_enabled=0, kw_paused=0,
            match=defaultdict(int), theme=defaultdict(int),
            carriers=set(), leads=leads_by_agid.get(agid, 0.0)))
        p["kw_total"] += 1
        if (r["status"] or "").upper() == "ENABLED":
            p["kw_enabled"] += 1
        else:
            p["kw_paused"] += 1
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
def write_outputs(out_dir, logical, phys, camps, carriers):
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
        wb.save(os.path.join(out_dir, "inventory_assessment.xlsx"))
    except ImportError:
        print("  (openpyxl not available; wrote CSVs only)")


# --------------------------------------------------------------------------- main
def run(db, days, out_dir, brand_db, perf_table=None):
    conn = ro_connect(db)
    print("opening DB (read-only): %s" % os.path.abspath(db))
    s = resolve_schema(conn, perf_table)
    banner(s)
    tagger = make_brand_tagger(brand_db or db)
    phys, active_cids, carrier_agg = build_inventory(conn, s, tagger, days)
    logical = logical_view(phys)
    camps = campaign_summary(phys, active_cids)
    carriers = carrier_coverage(carrier_agg, phys)
    conn.close()
    print("active campaigns: %d | ad groups (physical): %d | logical ad groups: %d | carriers seen: %d"
          % (len(active_cids), len(phys), len(logical), len(carriers)))
    print("top logical ad groups by leads: " + ", ".join(
        "%s(%.0f, %d kw, %d carr)" % (d["ad_group"][:20], d["total_leads"], d["kw_per_campaign"],
                                      d["distinct_carriers"]) for d in logical[:5]))
    write_outputs(out_dir, logical, phys, camps, carriers)
    print("wrote -> %s" % os.path.abspath(out_dir))


def main():
    ap = argparse.ArgumentParser(description="Read-only inventory assessment (account_id=1, active campaigns)")
    ap.add_argument("--db", required=True)
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--out-dir", default="inventory_output")
    ap.add_argument("--brand-db", default=None, help="registry DB for brand tagging (default: --db)")
    ap.add_argument("--perf-table", default=None, help="force the keyword/ad-group leads table name")
    a = ap.parse_args()
    run(a.db, a.days, a.out_dir, a.brand_db, a.perf_table)


if __name__ == "__main__":
    main()
