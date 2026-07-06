#!/usr/bin/env python
"""
Carrier GP Drainers (keyword grain) + Audience-Network Crisis (campaign grain) — Microsoft Home (account_id=2)

READ-ONLY (mode=ro). Introspection-first; fails loud on schema surprises.

Two grains, two sources — because network does NOT exist at keyword/ad-group grain in this DB:
  Section A  keyword_performance_daily        -> per-carrier keyword drainers, pause recovery, match-type, top performers
  Section B  campaign_performance_network_daily -> audience vs search + weekly dependency + crisis assertion (CAMPAIGN grain, NOT per carrier)

Per-carrier audience split is not computable here (carriers are ad groups; network is campaign grain only).

Usage (conda paid-search):
  python carrier_gp_audience_analysis.py --db "C:\\Projects\\google-ads-ops\\db\\paid_search.db" --out out.xlsx
--db REQUIRED (no production default).
"""
import argparse, sqlite3, sys, re
from datetime import datetime, timedelta
import pandas as pd
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

ACCOUNT_ID = 2
BIG_CARRIERS = ["state farm","geico","farmers","hartford","nationwide","travelers"]  # substring match, lowercase
CANT_BID = ["usaa","liberty mutual","lemonade"]                                       # substring, excluded entirely
AUDIENCE_VALUE = "Audience"
NET_ROLLUP_SENTINELS = {"ALL","TOTAL","COMBINED","-","","SUM"}
OFF_VERTICAL = [r"\bauto\b",r"\bcar\b",r"\bvehicle\b",r"\bmotorcycle\b",r"\bmedicare\b",r"\bmedigap\b",
                r"\bhealth\b",r"\bterm\s*life\b",r"\blife\s*insurance\b",r"\brenters?\b",r"\brental\b",
                r"\bmobile\s*home\b",r"\bpet\b",r"\bboat\b"]
MALFORMED_MIN_LEN = 3
CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\ufffd]")
TRUNC_RE = re.compile(r"[a-z]{15,}$|[-_+]$|\s{2,}", re.I)
EXPECTED_GP_WOW = 0.19; GP_BAND = 0.04
EXPECTED_AUD_SHARE = 0.70; SHARE_BAND = 0.10
SUBTOTAL_TOL = 0.02
FONT = "Arial"

def die(m): print(f"\n*** HARD STOP: {m}\n", file=sys.stderr); sys.exit(2)
def warn(flags,m): print(f"[QA FLAG] {m}"); flags.append(m)
def cols_of(con,t):
    try: return [r[1] for r in con.execute(f"PRAGMA table_info({t})")]
    except sqlite3.Error: return []
def has_table(con,t): return con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?",(t,)).fetchone() is not None

# ---------------- introspection ----------------
def introspect(con, flags, args):
    info={}
    for t in ("keyword_performance_daily","keyword_instances","ad_groups"):
        if not has_table(con,t): die(f"required table missing: {t}")
    kpd=cols_of(con,"keyword_performance_daily"); ki=cols_of(con,"keyword_instances"); ag=cols_of(con,"ad_groups")
    # FK
    if args.fk:
        if args.fk not in kpd: die(f"--fk '{args.fk}' not on kpd. Columns: {', '.join(kpd)}")
        info["fk"]=args.fk
    else:
        SAFE=["instance_id","keyword_instance_id","keyword_instances_id","kw_instance_id","kwi_id","ki_id"]
        fk=next((c for c in SAFE if c in kpd),None)
        if not fk:
            inst=[c for c in kpd if "instance" in c.lower()]
            fk=inst[0] if len(inst)==1 else None
            if len(inst)>1: die(f"ambiguous FK {inst}; use --fk")
        if not fk: die(f"cannot resolve kpd->ki FK. kpd columns: {', '.join(kpd)}. Use --fk.")
        info["fk"]=fk
    # date col
    info["date_col"]="report_date" if "report_date" in kpd else ("date" if "date" in kpd else die("no date col on kpd"))
    # spend
    info["spend_col"],info["spend_div"]=("cost_micros",1e6) if "cost_micros" in kpd else \
        (("spend",1.0) if "spend" in kpd else die("no spend col on kpd"))
    # geo
    info["has_geo"]="geo_state" in kpd
    if info["has_geo"]:
        gv=[r[0] for r in con.execute("SELECT DISTINCT geo_state FROM keyword_performance_daily LIMIT 5")]
        info["geo_all_only"]=(gv==["ALL"])
    # names from ki
    info["ki_has_names"]=("ad_group_name" in ki and "campaign_name" in ki)
    if not info["ki_has_names"]: warn(flags,"keyword_instances lacks name columns; will join campaigns/ad_groups for names")
    # keyword-grain IS (none expected)
    info["is_cols"]={k:a for k,a in {"impression_share":["impression_share","search_impression_share"],
        "is_lost_rank":["is_lost_rank","search_is_lost_rank"],"is_lost_budget":["is_lost_budget","search_is_lost_budget"]}.items()
        for a in a if a in kpd} if False else {}
    for canon,al in {"impression_share":["impression_share"],"is_lost_rank":["is_lost_rank"],"is_lost_budget":["is_lost_budget"]}.items():
        f=next((x for x in al if x in kpd),None)
        if f: info["is_cols"][canon]=f
    info["has_kw_is"]=len(info["is_cols"])>=1
    if not info["has_kw_is"]:
        warn(flags,"no keyword-grain impression-share -> Top Performers sheet shows GP/ROAS only (bid-headroom needs ad-group grain)")
    # ad_groups status
    info["ag_status_col"]="status" if "status" in ag else None
    if not info["ag_status_col"]: warn(flags,"ad_groups has no status -> paused exclusion via can't-bid name list only")
    _cpc=cols_of(con,"campaigns")
    info["camp_status_col"]="status" if "status" in _cpc else None
    if not info["camp_status_col"]: warn(flags,"campaigns has no status -> is_serving uses keyword+ad_group only (campaign-level pause undetected)")
    # network on kpd?
    info["kpd_has_network"]="network" in kpd
    if info["kpd_has_network"]: warn(flags,"kpd unexpectedly has a network column; Section A ignores it (Analysis 2 uses campaign table)")
    # network table (Section B)
    info["net_table"]="campaign_performance_network_daily" if has_table(con,"campaign_performance_network_daily") else None
    if info["net_table"]:
        nc=cols_of(con,info["net_table"]); info["net_cols"]=nc
        info["net_date"]="date" if "date" in nc else ("report_date" if "report_date" in nc else None)
        info["net_has_device"]="device" in nc
        devs=[r[0] for r in con.execute(f"SELECT DISTINCT device FROM {info['net_table']} WHERE account_id={ACCOUNT_ID}")] if info["net_has_device"] else []
        info["net_device_all"]="ALL" in devs
        info["net_conv_count"]=args.net_conv_count if args.net_conv_count else ("conversions" if "conversions" in nc else None)
        info["net_conv_value"]=args.net_conv_value if args.net_conv_value else ("conversion_value" if "conversion_value" in nc else None)
        nets=[r[0] for r in con.execute(f"SELECT DISTINCT network FROM {info['net_table']} WHERE account_id={ACCOUNT_ID}")]
        info["net_values"]=nets
        bad=[n for n in nets if str(n).strip().upper() in NET_ROLLUP_SENTINELS]
        if bad: die(f"network column has rollup sentinel(s) {bad} in {info['net_table']}; summing would double-count.")
    else:
        warn(flags,"campaign_performance_network_daily not found -> Section B (audience/crisis) skipped")
    return info

# ---------------- window + doubling guard ----------------
def resolve_window(con,info,args):
    dc,fk=info["date_col"],info["fk"]
    md=con.execute(f"SELECT MAX({dc}) FROM keyword_performance_daily kpd JOIN keyword_instances ki ON ki.id=kpd.{fk} WHERE ki.account_id={ACCOUNT_ID}").fetchone()[0]
    if not md: die("no kpd rows for account 2")
    md=datetime.strptime(str(md)[:10],"%Y-%m-%d").date()
    lag=md-timedelta(days=args.revenue_lag_days)
    we=datetime.strptime(args.window_end,"%Y-%m-%d").date() if args.window_end else lag
    ws=we-timedelta(days=args.window_days-1)
    return str(ws),str(we),str(lag),str(md)

def doubling_guard(con,info,ws,we,flags):
    dc,fk,sp,dv=info["date_col"],info["fk"],info["spend_col"],info["spend_div"]
    geo = " AND kpd.geo_state='ALL'" if (info["has_geo"] and info.get("geo_all_only")) else ""
    base=(f"FROM keyword_performance_daily kpd JOIN keyword_instances ki ON ki.id=kpd.{fk} "
          f"WHERE ki.account_id={ACCOUNT_ID} AND kpd.{dc} BETWEEN '{ws}' AND '{we}' "
          f"AND ki.keyword_text IS NOT NULL AND ki.keyword_text<>'-'{geo}")
    a=con.execute(f"SELECT COALESCE(SUM({sp}),0)/{dv} {base} AND kpd.device='ALL'").fetchone()[0]
    d=con.execute(f"SELECT COALESCE(SUM({sp}),0)/{dv} {base} AND kpd.device<>'ALL'").fetchone()[0]
    ok=a>0 and abs(a-d)/a<=SUBTOTAL_TOL
    if not ok: warn(flags,f"device='ALL' rollup inconsistent: ALL=${a:,.0f} vs per-device=${d:,.0f} — possible double-count")
    return {"all_cost":a,"dev_cost":d,"consistent":ok}

# ---------------- Section A base (keyword grain) ----------------
def pull_kpd(con,info,ws,we):
    dc,fk,sp,dv=info["date_col"],info["fk"],info["spend_col"],info["spend_div"]
    geo=" AND kpd.geo_state='ALL'" if (info["has_geo"] and info.get("geo_all_only")) else ""
    agj = f"JOIN ad_groups ag ON ag.ad_group_id=ki.ad_group_id AND ag.account_id=ki.account_id" if info["ag_status_col"] else ""
    ag_sel = f", ag.{info['ag_status_col']} AS ag_status" if info["ag_status_col"] else ", NULL AS ag_status"
    cj = "LEFT JOIN campaigns c ON c.campaign_id=ki.campaign_id AND c.account_id=ki.account_id" if info["camp_status_col"] else ""
    c_sel = f", c.{info['camp_status_col']} AS campaign_status" if info["camp_status_col"] else ", NULL AS campaign_status"
    sql=f"""
      SELECT ki.campaign_name, ki.ad_group_name, ki.keyword_text, ki.match_type, ki.status AS keyword_status
             {ag_sel}{c_sel},
             kpd.{dc} AS date, COALESCE(kpd.impressions,0) impressions, COALESCE(kpd.clicks,0) clicks,
             COALESCE(kpd.{sp},0)/{dv} AS spend,
             COALESCE(kpd.primary_conv_count,0) AS leads, COALESCE(kpd.primary_conv_value,0) AS revenue
      FROM keyword_performance_daily kpd
      JOIN keyword_instances ki ON ki.id=kpd.{fk}
      {agj} {cj}
      WHERE ki.account_id={ACCOUNT_ID} AND kpd.device='ALL'
        AND kpd.{dc} BETWEEN '{ws}' AND '{we}'
        AND ki.keyword_text IS NOT NULL AND ki.keyword_text<>'-'{geo}
    """
    return pd.read_sql_query(sql,con)

# ---------------- classification (generic parse of naming convention) ----------------
def big_tier(carrier):
    c=(carrier or "").lower()
    return "big" if any(b in c for b in BIG_CARRIERS) else "small"

def parse_adgroup(name):
    n=str(name).strip(); low=n.lower()
    if "costco" in low: return ("affinity","Costco","n/a")
    if re.search(r"\baaa\b",low): return ("affinity","AAA","n/a")
    if re.search(r"home[\s\-]*group|pooled",low): return ("pool",None,"n/a")
    parts=[p.strip() for p in n.split("-")]
    if len(parts)>=5 and parts[0].lower()=="home":
        kind=parts[1].lower(); carrier="-".join(parts[2:-2]).strip()
        if "nongeneric" in kind: return ("carrier",carrier,big_tier(carrier))
        if "generic" in kind:    return ("generic",None,"n/a")
    if "nongeneric" in low: return ("other",None,"n/a")
    if "generic" in low:    return ("generic",None,"n/a")
    return ("other",None,"n/a")

def classify(df):
    k=df["ad_group_name"].map(parse_adgroup)
    df["kind"]=k.map(lambda t:t[0]); df["carrier"]=k.map(lambda t:t[1]); df["carrier_tier"]=k.map(lambda t:t[2])
    return df

def flag_nonperf(df):
    kw=df["keyword_text"].astype(str)
    df["flag_off_vertical"]=kw.str.lower().apply(lambda s:any(re.search(t,s) for t in OFF_VERTICAL))
    def mal(s):
        return bool(CTRL_RE.search(s) or len(s.strip())<MALFORMED_MIN_LEN or s!=s.strip() or TRUNC_RE.search(s))
    df["flag_broken"]=kw.apply(mal)
    return df

def add_is_serving(df):
    def en(c): return df[c].astype(str).str.upper().isin(ENABLED) if c in df.columns else pd.Series(True,index=df.index)
    kwe=en("keyword_status"); age=en("ag_status")
    if "campaign_status" in df.columns:
        cs=df["campaign_status"].astype(str)
        cpe=cs.str.upper().isin(ENABLED) | cs.isin(["None","nan",""])
    else:
        cpe=pd.Series(True,index=df.index)
    df["is_serving"]=(kwe & age & cpe)
    return df

ENABLED={"ENABLED","ELIGIBLE","ACTIVE","SERVING"}
def apply_exclusions(df,info,flags,silent=False,exclude_nonserving=False):
    d=df.copy(); n0=len(d); sp0=d["spend"].sum() if "spend" in d else 0
    if exclude_nonserving and "is_serving" in d.columns:
        d=d[d["is_serving"]]
    _cbpat="|".join(re.escape(x) for x in CANT_BID)
    cb=d["carrier"].astype(str).str.lower().str.contains(_cbpat,regex=True,na=False) if "carrier" in d else pd.Series(False,index=d.index)
    d=d[~cb]
    if not silent:
        excl=sp0-(d["spend"].sum() if "spend" in d else 0)
        if info["ag_status_col"] and "ag_status" in df.columns:
            surp=df[(df["carrier"].astype(str).str.lower().str.contains(_cbpat,regex=True,na=False)) &
                    (df["ag_status"].astype(str).str.upper().isin(ENABLED))]
            if len(surp): warn(flags,f"can't-bid carrier ENABLED (excluded, investigate): {sorted(surp['carrier'].dropna().unique())}")
        other=df[(df["kind"]=="other")&(df["spend"]>0)]
        if len(other) and other["spend"].sum()>50:
            warn(flags,f"${other['spend'].sum():,.0f} in unclassified 'other' ({other['ad_group_name'].nunique()} groups)")
        pol="can't-bid only; non-serving surfaced" if not exclude_nonserving else "can't-bid + non-serving dropped"
        flags.append(f"[info] exclusions removed {n0-len(d)} rows / ${excl:,.0f} ({pol})")
    return d

BASE=["impressions","clicks","spend","leads","revenue"]
def agg(df,keys):
    return pd.DataFrame(columns=keys+BASE) if df.empty else df.groupby(keys,dropna=False)[BASE].sum().reset_index()

# ---------------- Section B (campaign-grain network) ----------------
def pull_network(con,info,ws,we):
    t,dc=info["net_table"],info["net_date"]; sp,dv=("cost_micros",1e6) if "cost_micros" in info["net_cols"] else ("cost",1.0)
    cc,cv=info["net_conv_count"],info["net_conv_value"]
    dev=" AND device='ALL'" if info["net_device_all"] else ""
    extra=""
    for c in ("audience_impression_share","audience_rank_lost_is","audience_budget_lost_is","all_conversions"):
        if c in info["net_cols"]: extra+=f", COALESCE({c},0) AS {c}"
    sql=f"""SELECT campaign_id, {dc} AS date,
              CASE WHEN network='{AUDIENCE_VALUE}' THEN 'audience' ELSE 'search' END AS net,
              network AS network_raw,
              COALESCE(impressions,0) impressions, COALESCE(clicks,0) clicks,
              COALESCE({sp},0)/{dv} AS spend, COALESCE({cc},0) AS leads, COALESCE({cv},0) AS revenue{extra}
            FROM {t} WHERE account_id={ACCOUNT_ID} AND {dc} BETWEEN '{ws}' AND '{we}'{dev}"""
    df=pd.read_sql_query(sql,con)
    # campaign names
    cn=pd.read_sql_query(f"SELECT DISTINCT campaign_id, campaign_name FROM keyword_instances WHERE account_id={ACCOUNT_ID}",con)
    return df.merge(cn,on="campaign_id",how="left")

def conv_reconcile(con,info,ws,we,flags):
    t=info["net_table"]; dc=info["net_date"]; dev=" AND device='ALL'" if info["net_device_all"] else ""
    nc=con.execute(f"SELECT COALESCE(SUM({info['net_conv_count']}),0) FROM {t} WHERE account_id={ACCOUNT_ID} AND {dc} BETWEEN '{ws}' AND '{we}'{dev}").fetchone()[0]
    nall=None
    if "all_conversions" in info["net_cols"]:
        nall=con.execute(f"SELECT COALESCE(SUM(all_conversions),0) FROM {t} WHERE account_id={ACCOUNT_ID} AND {dc} BETWEEN '{ws}' AND '{we}'{dev}").fetchone()[0]
    dck,fk=info["date_col"],info["fk"]; geo=" AND kpd.geo_state='ALL'" if (info["has_geo"] and info.get("geo_all_only")) else ""
    kp=con.execute(f"SELECT COALESCE(SUM(primary_conv_count),0) FROM keyword_performance_daily kpd JOIN keyword_instances ki ON ki.id=kpd.{fk} WHERE ki.account_id={ACCOUNT_ID} AND kpd.device='ALL' AND kpd.{dck} BETWEEN '{ws}' AND '{we}' AND ki.keyword_text<>'-'{geo}").fetchone()[0]
    if nall and nall>0 and nc/nall>0.98:
        warn(flags,f"network '{info['net_conv_count']}' ≈ all_conversions ({nc:,.0f}/{nall:,.0f}); it may NOT be the primary lead — verify or set --net-conv-count")
    return {"net_conv":nc,"net_all_conv":nall,"kpd_primary":kp}

def week_key(s,wk):
    d=pd.to_datetime(s); off={"MON":0,"TUE":1,"WED":2,"THU":3,"FRI":4,"SAT":5,"SUN":6}[wk]
    return (d-pd.to_timedelta((d.dt.weekday-off)%7,unit="D")).dt.date.astype(str)

def crisis_check(netdf,wk,flags,res):
    d=netdf.copy(); d["week"]=week_key(d["date"],wk); weeks=sorted(d["week"].unique())
    if len(weeks)<2: warn(flags,"crisis skipped: <2 weeks in network window"); res["crisis_ran"]=False; return
    w0,w1=weeks[-2],weeks[-1]; pr=d[d["week"]==w0]; la=d[d["week"]==w1]
    gp=lambda x:x["revenue"].sum()-x["spend"].sum()
    gp0,gp1=gp(pr),gp(la); dGP=gp1-gp0; decl=(gp0-gp1)/gp0 if gp0 else float("nan")
    ps=sum((gp(la[la["campaign_id"]==c])-gp(pr[pr["campaign_id"]==c])) for c in sorted(d["campaign_id"].unique()))
    if abs(ps-dGP)>0.01: die(f"GP decomposition !=blended: parts={ps:,.2f} vs {dGP:,.2f}")
    ct0,ct1=pr["clicks"].sum(),la["clicks"].sum()
    cvr0=pr["leads"].sum()/ct0 if ct0 else float("nan"); cvr1=la["leads"].sum()/ct1 if ct1 else float("nan")
    drop=cvr0-cvr1
    contr=lambda seg:-((la[la["net"]==seg]["leads"].sum()/ct1 if ct1 else 0)-(pr[pr["net"]==seg]["leads"].sum()/ct0 if ct0 else 0))
    ac,sc=contr("audience"),contr("search")
    if abs((ac+sc)-drop)>1e-9: die(f"CvR-drop decomposition !=drop: {ac+sc:.6f} vs {drop:.6f}")
    gp_ok=abs(decl-EXPECTED_GP_WOW)<=GP_BAND
    if not gp_ok: warn(flags,f"WoW GP decline {decl:.1%} OUTSIDE band {EXPECTED_GP_WOW:.0%}±{GP_BAND:.0%} — reconcile week convention/scope with tracker")
    if drop<=1e-9:
        share=float("nan"); shr_ok=None
        warn(flags,f"blended CvR did not drop ({cvr0:.2%}->{cvr1:.2%}); audience-share undefined this window")
    else:
        share=ac/drop; shr_ok=abs(share-EXPECTED_AUD_SHARE)<=SHARE_BAND
        if not shr_ok: warn(flags,f"audience share of CvR drop {share:.1%} OUTSIDE band {EXPECTED_AUD_SHARE:.0%}±{SHARE_BAND:.0%}")
    res.update(dict(crisis_ran=True,w0=w0,w1=w1,gp0=gp0,gp1=gp1,decl=decl,cvr0=cvr0,cvr1=cvr1,drop=drop,
                    share=share,gp_ok=gp_ok,shr_ok=shr_ok))

# ---------------- writer ----------------
HF=PatternFill("solid",fgColor="1F3864"); HFO=Font(name=FONT,bold=True,color="FFFFFF",size=10)
TF=Font(name=FONT,bold=True,size=13,color="1F3864"); FF=PatternFill("solid",fgColor="FCE4D6")
def col(i): return get_column_letter(i)
def hdr(ws,row,hs):
    for j,h in enumerate(hs):
        c=ws.cell(row,1+j,h); c.font=HFO; c.fill=HF; c.alignment=Alignment(horizontal="center",wrap_text=True)
    ws.row_dimensions[row].height=28
def widths(ws,w):
    for i,x in enumerate(w,1): ws.column_dimensions[col(i)].width=x
def title(ws,t,sub=None):
    ws["A1"]=t; ws["A1"].font=TF
    if sub: ws["A2"]=sub; ws["A2"].font=Font(name=FONT,italic=True,size=9,color="595959")
    return 4

def build_and_write(path,ctx):
    wb=Workbook(); wb.remove(wb.active)
    W_params(wb,ctx); W_drainers(wb,ctx); W_pause(wb,ctx); W_match(wb,ctx); W_top(wb,ctx)
    W_window(wb,ctx); W_window_rollup(wb,ctx); W_pause_cand(wb,ctx)
    W_aud(wb,ctx); W_trend(wb,ctx)
    wb.save(path)

def W_params(wb,ctx):
    ws=wb.create_sheet("Params & QA"); r=title(ws,"Params & QA",
        "Section A = keyword grain (kpd). Section B = campaign grain (network table). Ratios are within-sheet formulas.")
    I=ctx["info"]; rows=[
        ("Run",datetime.now().strftime("%Y-%m-%d %H:%M")),("DB",ctx["db"]),
        ("Analysis window",f"{ctx['ws']}..{ctx['we']} ({ctx['args'].window_days}d)"),
        ("Latest date / lag",f"{ctx['md']} / {ctx['args'].revenue_lag_days}d (< {ctx['lag']})"),
        ("Trend weeks / week start",f"{ctx['args'].trend_weeks} / {ctx['args'].week_start}"),
        ("Materiality",f"spend>=${ctx['args'].mat_spend:g} OR clicks>={ctx['args'].mat_clicks:g}"),
        ("Big carriers (substr)",", ".join(BIG_CARRIERS)),("Can't-bid (excluded)",", ".join(CANT_BID)),
        ("PHRASE","included, own row"),("Costco","separate contrast row (Section B N/A per-carrier)"),
        ("","")]
    rows+=[("— Section A schema —",""),("kpd->ki FK",I["fk"]),("date col",I["date_col"]),
        ("spend col/div",f"{I['spend_col']}/{I['spend_div']:g}"),
        ("geo",("geo_state present, all 'ALL' (no geo split)" if I.get("geo_all_only") else ("geo_state present, MULTIPLE values — check!" if I["has_geo"] else "no geo_state"))),
        ("keyword-grain IS",", ".join(I["is_cols"].values()) or "NONE (Top Performers = GP/ROAS only)"),
        ("ad_groups status",I["ag_status_col"] or "NONE (name-list only)"),
        ("names source","keyword_instances" if I["ki_has_names"] else "campaigns/ad_groups join")]
    rows+=[("","") ,("— Doubling guard (kpd) —",""),("device='ALL' spend",f"${ctx['dbl']['all_cost']:,.0f}"),
        ("per-device sum",f"${ctx['dbl']['dev_cost']:,.0f}"),("consistent?","YES" if ctx["dbl"]["consistent"] else "NO"),
        ("(eyeball vs tracker total)","manual")]
    rows+=[("","") ,("— Exclusions —",""),("rows raw/analysis",f"{ctx['n_raw']}/{ctx['n_base']}"),
        ("spend excluded",f"${ctx['excl']:,.0f}")]
    rows+=[("","") ,("— Section B (network) —","")]
    if I["net_table"]:
        rc=ctx["recon"]
        rows+=[("network table",I["net_table"]),("device rollup used","device='ALL'" if I["net_device_all"] else "sum of devices"),
            ("network values",", ".join(map(str,I["net_values"]))),
            ("leads/revenue cols",f"{I['net_conv_count']} / {I['net_conv_value']}"),
            ("conv reconcile",f"net {I['net_conv_count']}={rc['net_conv']:,.0f}"+(f" vs all_conversions={rc['net_all_conv']:,.0f}" if rc['net_all_conv'] else "")+f"; kpd primary(kw only)={rc['kpd_primary']:,.0f}"),
            ("GRAIN CAVEAT","Section B is CAMPAIGN grain; per-carrier audience split is NOT in this DB (network is campaign-only)")]
    else:
        rows+=[("network table","NOT FOUND — Section B skipped")]
    rows+=[("","") ,("— Crisis (campaign grain) —","")]
    cr=ctx["crisis"]
    if cr.get("crisis_ran"):
        rows+=[("WoW weeks",f"{cr['w0']}->{cr['w1']}"),("Blended GP prior/last",f"${cr['gp0']:,.0f} / ${cr['gp1']:,.0f}"),
            ("WoW GP decline",f"{cr['decl']:.1%} (expect {EXPECTED_GP_WOW:.0%}±{GP_BAND:.0%} -> {'OK' if cr['gp_ok'] else 'OUT OF BAND'})"),
            ("GP decomposition sums","YES (hard check)"),("Blended CvR prior/last",f"{cr['cvr0']:.2%} / {cr['cvr1']:.2%}"),
            ("Audience share of CvR drop","UNDEFINED (no drop)" if cr["shr_ok"] is None else f"{cr['share']:.1%} (expect {EXPECTED_AUD_SHARE:.0%}±{SHARE_BAND:.0%} -> {'OK' if cr['shr_ok'] else 'OUT OF BAND'})"),
            ("CvR-drop decomposition sums","YES (hard check)")]
    else: rows+=[("Crisis","skipped (see flags)")]
    rows+=[("","") ,("— QA flags —","")]+[(f"flag {i+1}",f) for i,f in enumerate(ctx["flags"])]
    for k,v in rows:
        ws.cell(r,1,k).font=Font(name=FONT,bold=str(k).startswith("—"),size=10)
        ws.cell(r,2,v).font=Font(name=FONT,size=10)
        if "OUT OF BAND" in str(v) or str(v)=="NO" or str(k).startswith("flag") or "CAVEAT" in str(k) or "NOT the primary" in str(v):
            ws.cell(r,2).fill=FF
        r+=1
    widths(ws,[34,110])

def _ratios(ws,r,imp,clk,spd,led,rev,want):
    L=dict(imp=col(imp),clk=col(clk),spd=col(spd),led=col(led),rev=col(rev))
    f={"GP":f"={L['rev']}{r}-{L['spd']}{r}","CvR":f"=IF({L['clk']}{r}=0,\"\",{L['led']}{r}/{L['clk']}{r})",
       "RPL":f"=IF({L['led']}{r}=0,\"\",{L['rev']}{r}/{L['led']}{r})","ROAS":f"=IF({L['spd']}{r}=0,\"\",{L['rev']}{r}/{L['spd']}{r})"}
    return [f[w] for w in want]

def W_drainers(wb,ctx):
    ws=wb.create_sheet("A1 — Keyword Drainers"); title(ws,"A1 — Keyword Drainers (big carriers)",
        "NETWORK-BLENDED (search+audience). GP ascending within carrier. is_serving=kw&adgroup&campaign enabled.")
    H=["carrier","keyword","match_type","kw_status","is_serving","impr","clicks","spend","leads","revenue","GP","CvR","RPL","broken","off_vertical","zero_conv"]
    hdr(ws,4,H); df=ctx["drainers"]; r=5
    for _,x in df.iterrows():
        ws.cell(r,1,x["carrier"]);ws.cell(r,2,x["keyword_text"]);ws.cell(r,3,x["match_type"]);ws.cell(r,4,x["keyword_status"])
        ws.cell(r,5,"Y" if x.get("is_serving",True) else "no")
        ws.cell(r,6,round(x["impressions"]));ws.cell(r,7,round(x["clicks"]));ws.cell(r,8,round(x["spend"],2))
        ws.cell(r,9,round(x["leads"],2));ws.cell(r,10,round(x["revenue"],2))
        gp,cvr,rpl=_ratios(ws,r,6,7,8,9,10,["GP","CvR","RPL"]);ws.cell(r,11,gp);ws.cell(r,12,cvr);ws.cell(r,13,rpl)
        z=(x["leads"]==0 and x["spend"]>0)
        ws.cell(r,14,"Y" if x["flag_broken"] else "");ws.cell(r,15,"Y" if x["flag_off_vertical"] else "");ws.cell(r,16,"Y" if z else "")
        if x["flag_broken"] or x["flag_off_vertical"] or z or not x.get("is_serving",True):
            for j in range(1,len(H)+1): ws.cell(r,j).fill=FF
        r+=1
    ws.freeze_panes="A5"; widths(ws,[20,34,11,10,10,9,8,10,8,11,11,8,9,7,11,9])

def W_pause(wb,ctx):
    ws=wb.create_sheet("A1 — Pause Recovery"); title(ws,"A1 — Pause Recovery","GP recovery = SUM(spend)-SUM(revenue) per bucket per carrier. Positive = freed by pausing.")
    H=["carrier","bucket","keywords","spend","revenue","GP_recovery"]; hdr(ws,4,H); df=ctx["pause"]; r=5
    for _,x in df.iterrows():
        ws.cell(r,1,x["carrier"]);ws.cell(r,2,x["bucket"]);ws.cell(r,3,int(x["keywords"]))
        ws.cell(r,4,round(x["spend"],2));ws.cell(r,5,round(x["revenue"],2));ws.cell(r,6,f"={col(4)}{r}-{col(5)}{r}");r+=1
    if r>5:
        ws.cell(r,1,"TOTAL").font=Font(name=FONT,bold=True)
        for c in (3,4,5): ws.cell(r,c,f"=SUM({col(c)}5:{col(c)}{r-1})").font=Font(name=FONT,bold=True)
        ws.cell(r,6,f"={col(4)}{r}-{col(5)}{r}").font=Font(name=FONT,bold=True)
    widths(ws,[20,16,10,12,12,13])

def W_match(wb,ctx):
    ws=wb.create_sheet("A1 — Match-Type Cut"); title(ws,"A1 — Carrier × Match Type","PHRASE its own row. Ratios within-sheet.")
    H=["carrier","match_type","impr","clicks","spend","leads","revenue","GP","CvR","RPL"];hdr(ws,4,H);df=ctx["match"];r=5
    for _,x in df.iterrows():
        ws.cell(r,1,x["carrier"]);ws.cell(r,2,x["match_type"]);ws.cell(r,3,round(x["impressions"]));ws.cell(r,4,round(x["clicks"]))
        ws.cell(r,5,round(x["spend"],2));ws.cell(r,6,round(x["leads"],2));ws.cell(r,7,round(x["revenue"],2))
        gp,cvr,rpl=_ratios(ws,r,3,4,5,6,7,["GP","CvR","RPL"]);ws.cell(r,8,gp);ws.cell(r,9,cvr);ws.cell(r,10,rpl);r+=1
    ws.freeze_panes="A5"; widths(ws,[20,11,9,8,10,8,11,11,8,9])

def W_top(wb,ctx):
    ws=wb.create_sheet("A1 — Top Performers"); 
    title(ws,"A1 — Top Performers (by GP)","Bid-headroom (IS-lost-rank/budget) needs ad-group-grain IS — not on kpd. GP/ROAS shown; pull IS from campaign_performance_network_daily for audience IS.")
    H=["carrier","keyword","impr","spend","leads","revenue","GP","ROAS"];hdr(ws,4,H);df=ctx["top"];r=5
    for _,x in df.iterrows():
        ws.cell(r,1,x["carrier"]);ws.cell(r,2,x["keyword_text"]);ws.cell(r,3,round(x["impressions"]))
        ws.cell(r,4,round(x["spend"],2));ws.cell(r,5,round(x["leads"],2));ws.cell(r,6,round(x["revenue"],2))
        gp,roas=_ratios(ws,r,3,4,4,5,6,["GP","ROAS"]);ws.cell(r,7,gp);ws.cell(r,8,roas);r+=1
    ws.freeze_panes="A5"; widths(ws,[20,34,9,10,8,11,11,8])

def W_aud(wb,ctx):
    ws=wb.create_sheet("A2 — Audience vs Search"); 
    title(ws,"A2 — Audience vs Search (CAMPAIGN grain)","Per campaign, not per carrier (network is campaign-grain only). audience_flag: NEG=aud GP<0. search_share_of_GP = search GP / total GP.")
    if not ctx.get("aud") is not None or ctx.get("aud") is None or ctx["aud"].empty:
        ws.cell(4,1,"Section B unavailable — campaign_performance_network_daily not found or empty.").font=Font(name=FONT,italic=True)
        return
    H=["campaign","s_spend","s_leads","s_rev","s_GP","s_CvR","s_RPL","a_spend","a_leads","a_rev","a_GP","a_CvR","a_RPL","audience_flag","search_share_of_GP"]
    hdr(ws,4,H); df=ctx["aud"]; r=5; ci={h:i+1 for i,h in enumerate(H)}
    sc=len(H)+1; ac=len(H)+2
    ws.cell(4,sc,"s_clicks").font=HFO; ws.cell(4,sc).fill=HF; ws.cell(4,ac,"a_clicks").font=HFO; ws.cell(4,ac).fill=HF
    for _,x in df.iterrows():
        ws.cell(r,1,x["campaign"])
        ws.cell(r,2,round(x["s_spend"],2));ws.cell(r,3,round(x["s_leads"],2));ws.cell(r,4,round(x["s_rev"],2))
        ws.cell(r,8,round(x["a_spend"],2));ws.cell(r,9,round(x["a_leads"],2));ws.cell(r,10,round(x["a_rev"],2))
        ws.cell(r,sc,round(x["s_clicks"]));ws.cell(r,ac,round(x["a_clicks"]))
        ws.cell(r,5,f"={col(4)}{r}-{col(2)}{r}");ws.cell(r,6,f"=IF({col(sc)}{r}=0,\"\",{col(3)}{r}/{col(sc)}{r})");ws.cell(r,7,f"=IF({col(3)}{r}=0,\"\",{col(4)}{r}/{col(3)}{r})")
        ws.cell(r,11,f"={col(10)}{r}-{col(8)}{r}");ws.cell(r,12,f"=IF({col(ac)}{r}=0,\"\",{col(9)}{r}/{col(ac)}{r})");ws.cell(r,13,f"=IF({col(9)}{r}=0,\"\",{col(10)}{r}/{col(9)}{r})")
        ws.cell(r,14,f'=IF({col(11)}{r}<-1,"NEG",IF(ABS({col(11)}{r})<=1,"BE",""))')
        ws.cell(r,15,f"=IF(({col(5)}{r}+{col(11)}{r})=0,\"\",{col(5)}{r}/({col(5)}{r}+{col(11)}{r}))")
        r+=1
    ws.column_dimensions[col(sc)].hidden=True; ws.column_dimensions[col(ac)].hidden=True
    ws.freeze_panes="A5"; widths(ws,[22,10,8,11,10,8,9,10,8,11,10,8,9,13,16,9,9])

def W_trend(wb,ctx):
    ws=wb.create_sheet("A2 — Weekly Audience Trend"); 
    title(ws,f"A2 — Weekly Audience-Dependency Trend ({ctx['args'].trend_weeks}w, campaign grain)","aud_cost_share / aud_rev_share rising => growing dependency. Account-blended.")
    if ctx.get("trend") is None or ctx["trend"].empty:
        ws.cell(4,1,"Section B unavailable.").font=Font(name=FONT,italic=True); return
    H=["week","aud_cost","srch_cost","aud_rev","srch_rev","aud_cost_share","aud_rev_share"];hdr(ws,4,H);df=ctx["trend"];r=5
    for _,x in df.iterrows():
        ws.cell(r,1,x["week"]);ws.cell(r,2,round(x["aud_cost"],2));ws.cell(r,3,round(x["srch_cost"],2))
        ws.cell(r,4,round(x["aud_rev"],2));ws.cell(r,5,round(x["srch_rev"],2))
        ws.cell(r,6,f"=IF(({col(2)}{r}+{col(3)}{r})=0,\"\",{col(2)}{r}/({col(2)}{r}+{col(3)}{r}))")
        ws.cell(r,7,f"=IF(({col(4)}{r}+{col(5)}{r})=0,\"\",{col(4)}{r}/({col(4)}{r}+{col(5)}{r}))")
        r+=1
    ws.freeze_panes="A5"; widths(ws,[12,12,12,12,12,15,14])

# ---------------- assembly ----------------
def build(con,info,base,raw,args,flags):
    ctx={}
    car=base[base["kind"]=="carrier"].copy(); big=car[car["carrier_tier"]=="big"].copy()
    keys=["carrier","keyword_text","match_type","keyword_status"]
    aspec=dict(impressions=("impressions","sum"),clicks=("clicks","sum"),spend=("spend","sum"),
        leads=("leads","sum"),revenue=("revenue","sum"),
        flag_broken=("flag_broken","max"),flag_off_vertical=("flag_off_vertical","max"))
    if "is_serving" in big.columns: aspec["is_serving"]=("is_serving","max")
    kw=big.groupby(keys,dropna=False).agg(**aspec).reset_index()
    if "is_serving" not in kw.columns: kw["is_serving"]=True
    kw=kw[(kw["spend"]>=args.mat_spend)|(kw["clicks"]>=args.mat_clicks)].copy()
    kw["GP"]=kw["revenue"]-kw["spend"]; kw=kw.sort_values(["carrier","GP"],ascending=[True,True]); ctx["drainers"]=kw
    kw["zero"]=(kw["leads"]==0)&(kw["spend"]>0); rec=[]
    for c,g in kw.groupby("carrier"):
        for b,m in [("broken",g["flag_broken"]),("off_vertical",g["flag_off_vertical"]),("zero_conv",g["zero"])]:
            s=g[m.astype(bool)]
            if len(s): rec.append(dict(carrier=c,bucket=b,keywords=len(s),spend=s["spend"].sum(),revenue=s["revenue"].sum()))
    ctx["pause"]=pd.DataFrame(rec) if rec else pd.DataFrame(columns=["carrier","bucket","keywords","spend","revenue"])
    ctx["match"]=agg(car,["carrier","match_type"]).sort_values(["carrier","match_type"])
    tp=big.groupby(["carrier","keyword_text"],dropna=False).agg(impressions=("impressions","sum"),
        clicks=("clicks","sum"),spend=("spend","sum"),leads=("leads","sum"),revenue=("revenue","sum")).reset_index()
    tp["GP"]=tp["revenue"]-tp["spend"]; ctx["top"]=tp.sort_values(["carrier","GP"],ascending=[True,False])
    return ctx

def build_section_b(con,info,args,flags,ctx):
    if not info["net_table"]:
        ctx["aud"]=None; ctx["trend"]=None; ctx["crisis"]={"crisis_ran":False}; ctx["recon"]={"net_conv":0,"net_all_conv":None,"kpd_primary":0}; return
    ws,we=ctx["ws"],ctx["we"]
    ctx["recon"]=conv_reconcile(con,info,ws,we,flags)
    net=pull_network(con,info,ws,we)
    # crisis over its own analysis window
    cr={}; crisis_check(net,args.week_start,flags,cr); ctx["crisis"]=cr
    # audience vs search per campaign (analysis window)
    piv=net.groupby(["campaign_name","net"]).agg(spend=("spend","sum"),clicks=("clicks","sum"),leads=("leads","sum"),rev=("revenue","sum")).reset_index()
    rows=[]
    for cn,g in piv.groupby("campaign_name"):
        s=g[g["net"]=="search"];a=g[g["net"]=="audience"]; get=lambda d,c:float(d[c].iloc[0]) if len(d) else 0.0
        rows.append(dict(campaign=cn,s_spend=get(s,"spend"),s_clicks=get(s,"clicks"),s_leads=get(s,"leads"),s_rev=get(s,"rev"),
                         a_spend=get(a,"spend"),a_clicks=get(a,"clicks"),a_leads=get(a,"leads"),a_rev=get(a,"rev")))
    ctx["aud"]=pd.DataFrame(rows).sort_values("campaign") if rows else pd.DataFrame()
    # weekly trend (independent trend window), account-blended
    tnet=pull_network_trend(con,info,args)
    if tnet is None or tnet.empty: ctx["trend"]=pd.DataFrame(); return
    tnet["week"]=week_key(tnet["date"],args.week_start)
    g=tnet.groupby(["week","net"]).agg(cost=("spend","sum"),rev=("revenue","sum")).reset_index()
    out=[]
    for wk,sub in g.groupby("week"):
        a=sub[sub["net"]=="audience"];s=sub[sub["net"]=="search"]
        out.append(dict(week=wk,aud_cost=a["cost"].sum(),srch_cost=s["cost"].sum(),aud_rev=a["rev"].sum(),srch_rev=s["rev"].sum()))
    ctx["trend"]=pd.DataFrame(out).sort_values("week")

def pull_network_trend(con,info,args):
    t,dc=info["net_table"],info["net_date"]; sp,dv=("cost_micros",1e6) if "cost_micros" in info["net_cols"] else ("cost",1.0)
    cc,cv=info["net_conv_count"],info["net_conv_value"]; dev=" AND device='ALL'" if info["net_device_all"] else ""
    md=con.execute(f"SELECT MAX({dc}) FROM {t} WHERE account_id={ACCOUNT_ID}").fetchone()[0]
    md=datetime.strptime(str(md)[:10],"%Y-%m-%d").date(); end=md-timedelta(days=args.revenue_lag_days); start=end-timedelta(weeks=args.trend_weeks)
    return pd.read_sql_query(f"""SELECT {dc} AS date, CASE WHEN network='{AUDIENCE_VALUE}' THEN 'audience' ELSE 'search' END net,
        COALESCE({sp},0)/{dv} spend, COALESCE({cv},0) revenue FROM {t}
        WHERE account_id={ACCOUNT_ID} AND {dc} BETWEEN '{start}' AND '{end}'{dev}""",con)


# ---------------- trailing-window comparison (network-blended; search-only-ready) ----------------
WINDOWS=[7,14,21,30,60,90]
def pull_window(con,info,md,lag_days,max_n,kw_network="blended"):
    dc,fk,sp,dv=info["date_col"],info["fk"],info["spend_col"],info["spend_div"]
    geo=" AND kpd.geo_state=" + "'ALL'" + " " if (info["has_geo"] and info.get("geo_all_only")) else ""
    agj="JOIN ad_groups ag ON ag.ad_group_id=ki.ad_group_id AND ag.account_id=ki.account_id" if info["ag_status_col"] else ""
    ag_sel=f", ag.{info['ag_status_col']} AS ag_status" if info["ag_status_col"] else ", NULL AS ag_status"
    cj="LEFT JOIN campaigns c ON c.campaign_id=ki.campaign_id AND c.account_id=ki.account_id" if info["camp_status_col"] else ""
    c_sel=f", c.{info['camp_status_col']} AS campaign_status" if info["camp_status_col"] else ", NULL AS campaign_status"
    like=" OR ".join(f"LOWER(ki.ad_group_name) LIKE '%{b}%'" for b in BIG_CARRIERS)
    anchor=md-timedelta(days=lag_days); start=anchor-timedelta(days=max_n)
    # kw_network switch: 'blended' = kpd (no network at kw grain). A future keyword-grain network
    # source would be wired here (search-only) with zero downstream change.
    net_clause=""  # inert today; see Params note
    sql=f"""
      SELECT ki.keyword_text, ki.match_type, ki.status AS keyword_status {ag_sel}{c_sel},
             ki.campaign_name AS campaign_name, ki.ad_group_name AS ad_group_name,
             ki.id AS kw_id, ki.criterion_id AS criterion_id, ki.ad_group_id AS ad_group_id,
             kpd.{dc} AS date,
             COALESCE(kpd.{sp},0)/{dv} AS spend, COALESCE(kpd.primary_conv_count,0) AS leads,
             COALESCE(kpd.primary_conv_value,0) AS revenue
      FROM keyword_performance_daily kpd JOIN keyword_instances ki ON ki.id=kpd.{fk}
      {agj} {cj}
      WHERE ki.account_id={ACCOUNT_ID} AND kpd.device=""" + "'ALL'" + f""" {geo}
        AND kpd.{dc} > '{start}' AND kpd.{dc} <= '{anchor}'
        AND LOWER(ki.ad_group_name) LIKE '%nongeneric%' AND ({like})
        AND ki.keyword_text IS NOT NULL AND ki.keyword_text<>'-' {net_clause}
    """
    df=pd.read_sql_query(sql,con)
    if df.empty: return df, str(anchor)
    df=classify(df); df=flag_nonperf(df); df=add_is_serving(df)
    df=df[(df["kind"]=="carrier")&(df["carrier_tier"]=="big")].copy()
    _cbpat="|".join(re.escape(x) for x in CANT_BID)
    df=df[~df["carrier"].astype(str).str.lower().str.contains(_cbpat,regex=True,na=False)]
    return df, str(anchor)

def verdict(g7,g14,g30,g60,g90,short_material):
    long_neg=(g30<0) and (g60<0) and (g90<0)
    long_all_pos=(g30>0) and (g60>0) and (g90>0)
    short_neg=(g7<0) or (g14<0); short_pos=(g7>0) and (g14>0)
    # recovery outranks persistent-pause: long windows are negative because they still contain the
    # old bad period, but recent 7/14 turned positive (and are material enough to trust).
    if (g60<0 or g90<0) and short_pos and short_material: return "RECOVERING - old problem resolving"
    if long_neg: return "PAUSE - persistent drainer"
    if long_all_pos and short_neg: return "WATCH - newly softening" if short_material else "NOISE - low-volume bad week"
    return "keep"

def compute_windows(df,anchor,short_mat,mat_spend):
    keys=["carrier","keyword_text","match_type"]
    def any_en(s): return "ENABLED" if (s.astype(str).str.upper()=="ENABLED").any() else "PAUSED"
    attr=df.groupby(keys).agg(kw_status=("keyword_status",any_en),is_serving=("is_serving","max"),
        off_vertical=("flag_off_vertical","max")).reset_index()
    grid=attr
    for N in WINDOWS:
        cut=str(pd.to_datetime(anchor).date()-timedelta(days=N))
        sub=df[(df["date"]>cut)&(df["date"]<=anchor)]
        g=sub.groupby(keys).agg(**{f"sp_{N}":("spend","sum"),f"rv_{N}":("revenue","sum"),f"ld_{N}":("leads","sum")}).reset_index()
        grid=grid.merge(g,on=keys,how="left")
    grid=grid.fillna(0)
    for N in WINDOWS:
        grid[f"GP_{N}"]=grid[f"rv_{N}"]-grid[f"sp_{N}"]
        grid[f"ROAS_{N}"]=grid.apply(lambda r:(r[f"rv_{N}"]/r[f"sp_{N}"]) if r[f"sp_{N}"]>0 else float("nan"),axis=1)
    grid["RPL_30"]=grid.apply(lambda r:(r["rv_30"]/r["ld_30"]) if r["ld_30"]>0 else float("nan"),axis=1)
    grid["RPL_90"]=grid.apply(lambda r:(r["rv_90"]/r["ld_90"]) if r["ld_90"]>0 else float("nan"),axis=1)
    grid["low_mat_short"]=grid["sp_14"]<short_mat
    grid["verdict"]=grid.apply(lambda r:verdict(r["GP_7"],r["GP_14"],r["GP_30"],r["GP_60"],r["GP_90"],not r["low_mat_short"]),axis=1)
    grid=grid[(grid["sp_90"]>=mat_spend)|(grid["sp_30"]>=mat_spend)].copy()
    order={"PAUSE":0,"WATCH":1,"RECOVE":2,"NOISE":3,"keep":4}
    grid["_o"]=grid["verdict"].str[:6].map(lambda x:order.get(x,4))
    return grid.sort_values(["carrier","_o","GP_30"],ascending=[True,True,True])

def build_window(con,info,args,ctx,flags):
    md=datetime.strptime(ctx["md"],"%Y-%m-%d").date()
    df,anchor=pull_window(con,info,md,args.revenue_lag_days,max(WINDOWS),args.kw_network)
    if df.empty:
        ctx["window"]=pd.DataFrame(); ctx["win_rollup"]=pd.DataFrame(); ctx["pause_cand"]=pd.DataFrame(); ctx["win_anchor"]=anchor; return
    grid=compute_windows(df,anchor,args.short_mat_spend,args.mat_spend); ctx["window"]=grid; ctx["win_anchor"]=anchor
    # carrier rollup
    rl=[]
    for car,g in df.groupby("carrier"):
        row={"carrier":car}
        for N in WINDOWS:
            cut=str(pd.to_datetime(anchor).date()-timedelta(days=N)); sub=g[(g["date"]>cut)&(g["date"]<=anchor)]
            row[f"sp_{N}"]=sub["spend"].sum(); row[f"rv_{N}"]=sub["revenue"].sum()
        rl.append(row)
    ctx["win_rollup"]=pd.DataFrame(rl).sort_values("carrier")
    # pause candidates (decision window)
    dw=args.pause_decision_window
    pc=grid[grid["verdict"].str.startswith("PAUSE")].copy()
    if len(pc):
        pc["spend_dec"]=pc[f"sp_{dw}"] if f"sp_{dw}" in pc else pc["sp_30"]
        pc["rev_dec"]=pc[f"rv_{dw}"] if f"rv_{dw}" in pc else pc["rv_30"]
        pc["recoverable"]=pc["spend_dec"]-pc["rev_dec"]
        pc=pc.sort_values("recoverable",ascending=False)
    ctx["pause_cand"]=pc

# ---------------- window sheet writers ----------------
def W_window(wb,ctx):
    ws=wb.create_sheet("A1 - Window Comparison")
    title(ws,f"A1 - Trailing-Window GP/ROAS (anchor {ctx.get('win_anchor','')})",
        "NETWORK-BLENDED, search-only-ready (--kw-network). Trailing/overlapping windows -> read as trajectory. verdict keys on persistence, not one window.")
    g=ctx["window"]
    if g is None or g.empty:
        ws.cell(4,1,"No big-carrier keyword rows in the 90d window.").font=Font(name=FONT,italic=True); return
    left=["carrier","keyword","match_type","kw_status","is_serving","off_vertical","low_mat_short","verdict"]
    gp_h=[f"GP_{N}" for N in WINDOWS]; roas_h=[f"ROAS_{N}" for N in WINDOWS]; rpl_h=["RPL_30","RPL_90"]
    H=left+gp_h+roas_h+rpl_h
    hdr(ws,4,H)
    # hidden base columns start after H
    nb=len(H); base_sp=nb+1; base_rv=nb+1+len(WINDOWS); ld30=base_rv+len(WINDOWS); ld90=ld30+1
    for k,lab in enumerate(["ld_30_base","ld_90_base"]): pass
    ci={h:i+1 for i,h in enumerate(H)}
    r=5
    for _,x in g.iterrows():
        ws.cell(r,1,x["carrier"]);ws.cell(r,2,x["keyword_text"]);ws.cell(r,3,x["match_type"]);ws.cell(r,4,x["kw_status"])
        ws.cell(r,5,"Y" if x["is_serving"] else "no");ws.cell(r,6,"Y" if x["off_vertical"] else "")
        ws.cell(r,7,"Y" if x["low_mat_short"] else "");ws.cell(r,8,x["verdict"])
        # base values (hidden)
        for i,N in enumerate(WINDOWS):
            ws.cell(r,base_sp+i,round(float(x[f"sp_{N}"]),2)); ws.cell(r,base_rv+i,round(float(x[f"rv_{N}"]),2))
        ws.cell(r,ld30,round(float(x["ld_30"]),2)); ws.cell(r,ld90,round(float(x["ld_90"]),2))
        # formulas
        for i,N in enumerate(WINDOWS):
            spc=col(base_sp+i); rvc=col(base_rv+i)
            ws.cell(r,ci[f"GP_{N}"],f"={rvc}{r}-{spc}{r}")
            ws.cell(r,ci[f"ROAS_{N}"],f"=IF({spc}{r}=0,\"\",{rvc}{r}/{spc}{r})")
        ws.cell(r,ci["RPL_30"],f"=IF({col(ld30)}{r}=0,\"\",{col(base_rv+3)}{r}/{col(ld30)}{r})")
        ws.cell(r,ci["RPL_90"],f"=IF({col(ld90)}{r}=0,\"\",{col(base_rv+5)}{r}/{col(ld90)}{r})")
        vv=x["verdict"]
        if vv.startswith("PAUSE"):
            for j in range(1,len(H)+1): ws.cell(r,j).fill=PatternFill("solid",fgColor="F8CBAD")
        elif vv.startswith("WATCH") or vv.startswith("RECOV"):
            ws.cell(r,ci["verdict"]).fill=FF
        r+=1
    for cc in range(nb+1,ld90+1): ws.column_dimensions[col(cc)].hidden=True
    ws.freeze_panes="C5"
    widths(ws,[20,30,10,9,9,9,9,26]+[9]*6+[9]*6+[9,9])

def W_window_rollup(wb,ctx):
    ws=wb.create_sheet("A1 - Window Rollup")
    title(ws,"A1 - Window Rollup by Carrier","Total GP by trailing window per big carrier -> which carriers are deteriorating vs stable. Network-blended.")
    df=ctx.get("win_rollup")
    if df is None or df.empty: ws.cell(4,1,"n/a").font=Font(name=FONT,italic=True); return
    H=["carrier"]+[f"GP_{N}" for N in WINDOWS]; hdr(ws,4,H)
    nb=len(H); base_sp=nb+1; base_rv=nb+1+len(WINDOWS)
    r=5
    for _,x in df.iterrows():
        ws.cell(r,1,x["carrier"])
        for i,N in enumerate(WINDOWS):
            ws.cell(r,base_sp+i,round(float(x[f"sp_{N}"]),2)); ws.cell(r,base_rv+i,round(float(x[f"rv_{N}"]),2))
            ws.cell(r,2+i,f"={col(base_rv+i)}{r}-{col(base_sp+i)}{r}")
        r+=1
    for cc in range(nb+1,base_rv+len(WINDOWS)+1): ws.column_dimensions[col(cc)].hidden=True
    ws.freeze_panes="B5"; widths(ws,[22]+[11]*6)

def W_pause_cand(wb,ctx):
    ws=wb.create_sheet("A1 - Pause Candidates")
    dw=ctx["args"].pause_decision_window
    title(ws,f"A1 - Pause Candidates (decision window {dw}d)",
        "PAUSE verdicts only. recoverable_GP = spend-revenue over decision window (positive=freed by pausing). is_serving=no => already dead spend, not recoverable.")
    df=ctx.get("pause_cand")
    if df is None or df.empty: ws.cell(4,1,"No PAUSE-verdict keywords.").font=Font(name=FONT,italic=True); return
    H=["carrier","keyword","match_type","is_serving","spend_dec","revenue_dec","recoverable_GP","GP_90","RPL_30","RPL_90","note"]
    hdr(ws,4,H); r=5
    for _,x in df.iterrows():
        ws.cell(r,1,x["carrier"]);ws.cell(r,2,x["keyword_text"]);ws.cell(r,3,x["match_type"])
        ws.cell(r,4,"Y" if x["is_serving"] else "no")
        ws.cell(r,5,round(float(x["spend_dec"]),2));ws.cell(r,6,round(float(x["rev_dec"]),2))
        ws.cell(r,7,f"={col(5)}{r}-{col(6)}{r}")
        ws.cell(r,8,round(float(x["GP_90"]),2))
        ws.cell(r,9,None if pd.isna(x["RPL_30"]) else round(float(x["RPL_30"]),2))
        ws.cell(r,10,None if pd.isna(x["RPL_90"]) else round(float(x["RPL_90"]),2))
        ws.cell(r,11,"already dead (historical spend)" if not x["is_serving"] else "serving - real recovery")
        if not x["is_serving"]: ws.cell(r,11).fill=FF
        r+=1
    if r>5:
        ws.cell(r,1,"TOTAL recoverable (serving only)").font=Font(name=FONT,bold=True)
        sv=df[df["is_serving"]]
        ws.cell(r,7,round(float((sv["spend_dec"]-sv["rev_dec"]).sum()),2)).font=Font(name=FONT,bold=True)
    ws.freeze_panes="A5"; widths(ws,[20,30,10,10,11,12,13,11,9,9,28])



# ---------------- Microsoft Ads bulk export (Format Version 6.0) ----------------
MATCH_TITLE={"BROAD":"Broad","PHRASE":"Phrase","EXACT":"Exact"}
def build_bulk_export(df,anchor,args):
    keys=["carrier","campaign_name","ad_group_name","keyword_text","match_type","kw_id","criterion_id","ad_group_id"]
    def any_en(s): return "ENABLED" if (s.astype(str).str.upper()=="ENABLED").any() else "PAUSED"
    a=df.groupby(keys,dropna=False).agg(is_serving=("is_serving","max"),
        off_vertical=("flag_off_vertical","max"),kw_status=("keyword_status",any_en)).reset_index()
    for N in WINDOWS:
        cut=str(pd.to_datetime(anchor).date()-timedelta(days=N)); sub=df[(df["date"]>cut)&(df["date"]<=anchor)]
        g=sub.groupby(keys,dropna=False).agg(**{f"sp_{N}":("spend","sum"),f"rv_{N}":("revenue","sum")}).reset_index()
        a=a.merge(g,on=keys,how="left")
    for N in WINDOWS:
        a[f"sp_{N}"]=a[f"sp_{N}"].fillna(0); a[f"rv_{N}"]=a[f"rv_{N}"].fillna(0); a[f"GP_{N}"]=a[f"rv_{N}"]-a[f"sp_{N}"]
    a["verdict"]=a.apply(lambda r:verdict(r["GP_7"],r["GP_14"],r["GP_30"],r["GP_60"],r["GP_90"],r["sp_14"]>=args.short_mat_spend),axis=1)
    dw=args.pause_decision_window; a["recoverable"]=a[f"sp_{dw}"]-a[f"rv_{dw}"]
    a["recommend"]=a["off_vertical"].map(lambda o:"pause now (off-vertical)" if o else "HOLD for search-only (in-vertical, blended)")
    return a[a["verdict"].str.startswith("PAUSE")].copy().sort_values("recoverable",ascending=False)

def emit_bulk(paused,args,flags):
    import csv
    pre=args.emit_bulk; cid=args.client_id or "carrier_gp_pause"
    serving=paused[paused["is_serving"]].copy(); dead=paused[~paused["is_serving"]]
    acols=["carrier","campaign_name","ad_group_name","keyword_text","match_type","criterion_id","ad_group_id","kw_id",
           "is_serving","off_vertical","verdict","recommend"]+[f"GP_{N}" for N in [7,14,30,90]]+["recoverable"]
    paused.to_csv(f"{pre}_action.csv",index=False,columns=[c for c in acols if c in paused.columns])
    safe=serving if args.bulk_all_pauses else serving[serving["off_vertical"]]
    hdr=["Type","Status","Id","Parent Id","Campaign","Ad Group","Client Id","Keyword","Match Type"]
    def _id(v):
        try: return "" if pd.isna(v) else str(int(v))
        except Exception: return "" if (v is None or (isinstance(v,float) and pd.isna(v))) else str(v)
    def write_bulk(path,status,rows):
        with open(path,"w",newline="",encoding="utf-8-sig") as f:
            w=csv.writer(f); w.writerow(hdr); w.writerow(["Format Version","6.0"]+[""]*(len(hdr)-2))
            for _,x in rows.iterrows():
                w.writerow(["Keyword",status,_id(x["criterion_id"]),_id(x["ad_group_id"]),
                            x["campaign_name"],x["ad_group_name"],cid,x["keyword_text"],
                            MATCH_TITLE.get(str(x["match_type"]).upper(),str(x["match_type"]).title())])
    write_bulk(f"{pre}_pause_bulk_draft.csv","Paused",safe)
    write_bulk(f"{pre}_rollback_draft.csv","Active",safe)
    flags.append(f"[bulk] {len(paused)} PAUSE kw -> {len(serving)} serving; bulk draft={len(safe)} "
                 f"({'ALL serving' if args.bulk_all_pauses else 'off-vertical safe tier'}); "
                 f"{len(dead)} non-serving skipped (dead); {len(serving)-len(safe)} in-vertical HELD for search-only")
    return len(safe)


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--db",required=True);ap.add_argument("--out",default="carrier_gp_audience.xlsx")
    ap.add_argument("--window-days",type=int,default=30,dest="window_days")
    ap.add_argument("--window-end",default=None,dest="window_end")
    ap.add_argument("--revenue-lag-days",type=int,default=2,dest="revenue_lag_days")
    ap.add_argument("--trend-weeks",type=int,default=10,dest="trend_weeks")
    ap.add_argument("--week-start",default="MON",dest="week_start",choices=["MON","TUE","WED","THU","FRI","SAT","SUN"])
    ap.add_argument("--mat-spend",type=float,default=50.0,dest="mat_spend")
    ap.add_argument("--mat-clicks",type=float,default=20.0,dest="mat_clicks")
    ap.add_argument("--fk",default=None)
    ap.add_argument("--net-conv-count",default=None,dest="net_conv_count")
    ap.add_argument("--net-conv-value",default=None,dest="net_conv_value")
    ap.add_argument("--short-mat-spend",type=float,default=30.0,dest="short_mat_spend")
    ap.add_argument("--pause-decision-window",type=int,default=30,dest="pause_decision_window",choices=WINDOWS)
    ap.add_argument("--exclude-nonserving",action="store_true",dest="exclude_nonserving")
    ap.add_argument("--kw-network",default="blended",dest="kw_network",help="'blended' today; future keyword-grain net source flips grid to search-only")
    ap.add_argument("--emit-bulk",default=None,dest="emit_bulk",help="path prefix; writes _action.csv,_pause_bulk_draft.csv,_rollback_draft.csv")
    ap.add_argument("--client-id",default=None,dest="client_id")
    ap.add_argument("--bulk-all-pauses",action="store_true",dest="bulk_all_pauses",help="include in-vertical PAUSEs in bulk draft (default: off-vertical safe tier only)")
    args=ap.parse_args()
    flags=[]; con=sqlite3.connect(f"file:{args.db}?mode=ro",uri=True); con.execute("PRAGMA foreign_keys=ON")
    info=introspect(con,flags,args)
    ws,we,lag,md=resolve_window(con,info,args)
    dbl=doubling_guard(con,info,ws,we,flags)
    raw=pull_kpd(con,info,ws,we)
    if len(raw)==0: die(f"kpd base join returned 0 rows {ws}..{we}. Verify FK '{info['fk']}' (--fk) / window.")
    if raw["keyword_text"].isna().all() or (raw["clicks"].sum()==0 and raw["spend"].sum()==0):
        die(f"kpd join all-NULL/zero — FK '{info['fk']}' likely wrong. Use --fk.")
    raw=classify(raw); raw=flag_nonperf(raw); raw=add_is_serving(raw); n_raw=len(raw); rsp=raw["spend"].sum()
    base=apply_exclusions(raw,info,flags,exclude_nonserving=args.exclude_nonserving); n_base=len(base); excl=rsp-base["spend"].sum()
    ctx=build(con,info,base,raw,args,flags)
    ctx.update(dict(db=args.db,info=info,args=args,flags=flags,dbl=dbl,ws=ws,we=we,lag=lag,md=md,n_raw=n_raw,n_base=n_base,excl=excl))
    build_window(con,info,args,ctx,flags)
    if args.emit_bulk and ctx.get("window") is not None and len(ctx["window"]):
        _pw=pull_window(con,info,datetime.strptime(ctx["md"],"%Y-%m-%d").date(),args.revenue_lag_days,max(WINDOWS),args.kw_network)[0]
        if not _pw.empty:
            _pw=_pw[(_pw["kind"]=="carrier")&(_pw["carrier_tier"]=="big")]
            emit_bulk(build_bulk_export(_pw,ctx["win_anchor"],args),args,flags)
    build_section_b(con,info,args,flags,ctx)
    build_and_write(args.out,ctx); con.close()
    print(f"\nWrote {args.out} | window {ws}..{we} | {n_base} A-rows | ${excl:,.0f} excluded")
    if flags:
        print("QA flags:"); [print("  -",f) for f in flags]

if __name__=="__main__": main()
