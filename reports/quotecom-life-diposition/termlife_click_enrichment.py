#!/usr/bin/env python3
"""
termlife_click_enrichment.py  (v1.5)

Joins SQLife disposition leads to click-time truth and answers the two live
questions: (1) is the connect decline HOUR-MIX or IN-HOURS RATE (Ben's
hour/DoW ask), and (2) does the September break concentrate in particular
MATCH TYPES / devices / slots (the marginal-traffic hypothesis).

Join chain (LEFT joins, match counts reported at every hop per architect memo):
  disposition lead (per day+source_code final stitch, paid WebM)
    -> lead_click_map  on first-pipe-part of trk_click_id = request_id
         (gives submitted_ct hour, Central, and gclid)
    -> gclid_clicks    on gclid  (gives keyword/match_type-as-served/device/
         slot/campaign; ledger horizon starts 2026-07-25 -- leads whose click
         predates that can NEVER match; segmented accordingly)

Hazards honored: 90-day ledger horizon; ledger has NO device='ALL' rollup;
ledger counts never tied to cpd; trailing ~3 days settling; feed-grain lead
counts never mixed with platform conversion columns; enums matched exactly.

Sheets: Gates | HourOfDay | ClickLag | MatchType | DeviceSlot | one weekly
sheet per campaign (campaign-level weekly table + per-ad-group weekly blocks,
ad group names as they appear in the account; Policy KPI est on every table).
KPI est = prem / (ops x per-op rate by segment & lead month).
Output: output/termlife_click_enrichment_{batch}.xlsx
"""
import argparse, os, sqlite3, sys
from datetime import date, timedelta

import pandas as pd
from openpyxl import Workbook
from openpyxl.chart import BarChart, LineChart, Reference, Series
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

DEF_DB = r"C:\Projects\google-ads-ops\db\paid_search.db"
SEG_OF_CODE = {"CC": "High", "CB": "Medium", "CA": "Low"}
SEG_OP_RATES = {   # per-op payment by lead month (observed Looker Spend/Ops)
    "High":   {"2026-06": 150.0, "2026-07": 150.0, "2026-08": 149.67, "2026-09": 144.02, "2026-10": 148.53},
    "Medium": {"2026-06":  87.0, "2026-07":  87.0, "2026-08":  86.55, "2026-09":  84.33, "2026-10":  86.75},
    "Low":    {"2026-06":  31.0, "2026-07":  31.0, "2026-08":  30.98, "2026-09":  31.51, "2026-10":  33.24},
}
def op_rate(seg,m):
    r=SEG_OP_RATES[seg]; return r.get(m, r[sorted(r)[-1]])
BREAK_EVEN = 2.96
WEEK_START = "2026-07-27"   # first full Mon-Sat week inside the ledger horizon
IN_HOURS = (8, 17)          # provisional Central business window [8:00,17:00)
LEDGER_START = "2026-07-25" # gclid_clicks horizon (architect memo)

NAVY="1F3864"; GRAY="D9D9D9"
MONEY='$#,##0'; NUMF='#,##0'; PCT='0.0%'; DPT='+0.0%;-0.0%'
def F(sz=10,b=False,c="000000"): return Font(name="Arial",size=sz,bold=b,color=c)
def fillc(h): return PatternFill("solid",fgColor=h)
THIN=Border(*[Side(style="thin",color="BFBFBF")]*4)
def put(ws,r,c,v,fmt=None,b=False,fl=None):
    cell=ws.cell(r,c,v); cell.font=F(10,b)
    if fmt: cell.number_format=fmt
    if fl: cell.fill=fillc(fl)
    cell.border=THIN; return cell
def title(ws,t,lines,w):
    ws.cell(1,1,t).font=F(14,True,"FFFFFF")
    for c in range(1,w+1): ws.cell(1,c).fill=fillc(NAVY)
    ws.row_dimensions[1].height=24
    r=2
    for ln in lines:
        ws.cell(r,1,ln).font=F(9,False,"404040"); r+=1
    return r+1
def hdr(ws,r,cols):
    for i,h in enumerate(cols,1):
        c=ws.cell(r,i,h); c.font=F(9,True,"FFFFFF"); c.fill=fillc("4472C4")
        c.alignment=Alignment(horizontal="center",wrap_text=True); c.border=THIN
    ws.row_dimensions[r].height=26
    return r+1
def num(s): return pd.to_numeric(s.astype(str).str.replace(r"[$,]","",regex=True),errors="coerce").fillna(0.0)
def pdate(s): return pd.to_datetime(s.astype(str),errors="coerce",format="mixed").dt.date

def load(con):
    D=pd.read_sql_query("""
      SELECT s.day ld, s.source_code code, s.op_count op, s.sqo sqo, s.cris cri,
             s.capped_cri_dollars prem, s.lead_tier tier, s.trk_click_id tci
      FROM life_disposition_snapshot s
      JOIN (SELECT day d, source_code sc, MAX(batch_date) mb
            FROM life_disposition_snapshot GROUP BY 1,2) x
        ON s.day=x.d AND s.source_code=x.sc AND s.batch_date=x.mb""",con)
    D["ld"]=pdate(D["ld"]); D=D.dropna(subset=["ld"])
    D["code"]=D["code"].astype(str); D=D[D["code"].str.len()>=3]
    D["seg"]=D["code"].str[:2].map(SEG_OF_CODE)
    D=D[(D["seg"].notna())&(D["code"].str[2]=="M")]
    for c in ("op","sqo","cri","prem"): D[c]=num(D[c])
    D["tier"]=num(D["tier"])
    D["band"]=D["tier"].map(lambda t: "1-3" if 1<=t<=3 else ("4-7" if 4<=t<=7 else ("8-10" if 8<=t<=10 else None)))
    D["month"]=D["ld"].map(lambda d:f"{d.year}-{d.month:02d}")
    D["req"]=D["tci"].astype(str).str.split("|").str[0].str.strip()
    D.loc[D["req"].isin(("","None","nan")),"req"]=None

    M=pd.read_sql_query("""SELECT request_id req, MIN(submitted_ct) ts,
                                  MAX(gclid) gclid
                           FROM lead_click_map GROUP BY 1""",con)
    M=M[M["req"]!=""]
    D=D.merge(M,on="req",how="left")
    D["hour"]=pd.to_datetime(D["ts"],errors="coerce").dt.hour
    D["inh"]=D["hour"].map(lambda h: None if pd.isna(h) else (IN_HOURS[0]<=h<IN_HOURS[1]))

    G=pd.read_sql_query("""SELECT gclid, click_date, keyword_text, match_type,
                                  device, slot, campaign_id, ad_group_id, instance_id
                           FROM gclid_clicks WHERE account_id=29""",con)
    D=D.merge(G,on="gclid",how="left")
    D["click_date"]=pdate(D["click_date"])
    D["pay"]=[o*op_rate(s,m) for o,s,m in zip(D["op"],D["seg"],D["month"])]
    return D

def name_maps(con):
    """Campaign + ad group display names, discovered from sqlite_master (architect memo)."""
    camp={}
    try:
        camp={str(r[0]):str(r[1]) for r in con.execute("SELECT campaign_id,campaign_name FROM campaigns")}
    except sqlite3.OperationalError: pass
    ag={}
    tables=[r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view')")]
    for t in tables:
        try: cols={r[1].lower() for r in con.execute(f"PRAGMA table_info({t})")}
        except sqlite3.OperationalError: continue
        if "ad_group_id" in cols and ("ad_group_name" in cols or "adgroup_name" in cols):
            nc="ad_group_name" if "ad_group_name" in cols else "adgroup_name"
            for r in con.execute(f"SELECT DISTINCT ad_group_id,{nc} FROM {t}"):
                ag.setdefault(str(r[0]),str(r[1]))
            if ag:
                print(f"[names] ad-group names from {t}")
                break
    if not ag: print("  !! no ad-group name table found -- labeling by ad_group_id")
    return camp, ag

def adgroup_spend(con):
    """ad_group_id -> spend from the first table carrying ad-group cost; ledger-click fallback."""
    tables=[r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view')")]
    for t in tables:
        try: cols={r[1].lower() for r in con.execute(f"PRAGMA table_info({t})")}
        except sqlite3.OperationalError: continue
        if "ad_group_id" in cols and "cost_micros" in cols and t!="gclid_clicks":
            w=["1=1"]
            if "account_id" in cols: w.append("account_id=29")
            if "device" in cols and con.execute(f"SELECT 1 FROM {t} WHERE device='ALL' LIMIT 1").fetchone():
                w.append("device='ALL'")
            sp={str(r[0]):(r[1] or 0) for r in con.execute(
                f"SELECT ad_group_id, SUM(cost_micros)/1e6 FROM {t} WHERE {' AND '.join(w)} GROUP BY 1")}
            if sp:
                print(f"[spend] ad-group spend ranked from {t}"); return sp
    print("[spend] !! no ad-group cost table found -- ranking by ledger click volume")
    return {str(r[0]):r[1] for r in con.execute(
        "SELECT ad_group_id, COUNT(*) FROM gclid_clicks WHERE account_id=29 GROUP BY 1")}

def monday(d): return d - timedelta(days=d.weekday())

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--db",default=DEF_DB); ap.add_argument("--out-dir",default=None)
    a=ap.parse_args()
    con=sqlite3.connect(f"file:{a.db}?mode=ro",uri=True)
    b_last=con.execute("SELECT MAX(batch_date) FROM life_disposition_snapshot").fetchone()[0]
    D=load(con)
    months=sorted(D["month"].unique())
    print(f"[rows] {len(D)} leads | months {months[0]}..{months[-1]}")

    wb=Workbook(); wb.remove(wb.active)

    # ---------------- Gates ----------------
    ws=wb.create_sheet("Gates")
    r=title(ws,"Join-chain match rates (gates everything downstream)",
        ["Hop 1: lead -> lead_click_map on request-id (hour + gclid). Hop 2: gclid -> gclid_clicks ledger.",
         f"Ledger horizon starts {LEDGER_START}: leads whose click predates it can NEVER match hop 2 --",
         "hop-2 rate is therefore reported only for leads dated on/after the horizon (architect memo, hazard 1)."],8)
    r=hdr(ws,r,["Lead month","Leads","Mapped (hop1)","hop1 %","With gclid","Ledger-matched (hop2)","hop2 % (post-horizon)",""])
    for m in months:
        d=D[D["month"]==m]
        post=d[d["ld"]>=date(2026,7,25)]
        mapped=d["ts"].notna().sum(); withg=(d["gclid"].fillna("")!="").sum()
        led=post[(post["gclid"].fillna("")!="")&post["click_date"].notna()]
        ledn=len(led); posg=(post["gclid"].fillna("")!="").sum()
        put(ws,r,1,m); put(ws,r,2,len(d),NUMF); put(ws,r,3,int(mapped),NUMF)
        put(ws,r,4,mapped/len(d) if len(d) else 0,PCT)
        put(ws,r,5,int(withg),NUMF)
        put(ws,r,6,int(ledn),NUMF)
        put(ws,r,7,(ledn/posg) if posg else None,PCT)
        r+=1
    for i,w in enumerate([10,8,12,8,10,16,16,4],1): ws.column_dimensions[get_column_letter(i)].width=w

    # ---------------- HourOfDay ----------------
    ws=wb.create_sheet("HourOfDay")
    r=title(ws,"Connect rate by lead submit hour (Central): mix vs rate decomposition",
        [f"In-hours = {IN_HOURS[0]:02d}:00-{IN_HOURS[1]:02d}:00 Central, PROVISIONAL until SQ states official operating hours.",
         "Decomposition: 'mix pts' = connect change explained by in/off-hours share shift at base-month rates;",
         "'rate pts' = change in the rates themselves. If in-hours connect fell, the decline is NOT hour mix.",
         "Mapped leads only (hop-1); unmapped share on Gates. Feed-grain counts throughout (conversion canon)."],12)
    r=hdr(ws,r,["Month","Ops (mapped)","% off-hours","Ops in","SQO in","Connect in","Ops off","SQO off","Connect off",
                "Blended","MoM mix pts","MoM rate pts"])
    first=r; prev=None
    Dm=D[D["inh"].notna()]
    for m in months:
        d=Dm[Dm["month"]==m]
        oi=d[d["inh"]==True]["op"].sum(); si=d[d["inh"]==True]["sqo"].sum()
        oo=d[d["inh"]==False]["op"].sum(); so=d[d["inh"]==False]["sqo"].sum()
        tot=oi+oo
        put(ws,r,1,m,b=True); put(ws,r,2,int(tot),NUMF)
        put(ws,r,3,oo/tot if tot else None,PCT)
        put(ws,r,4,int(oi),NUMF); put(ws,r,5,int(si),NUMF)
        put(ws,r,6,f'=IFERROR(E{r}/D{r},"")',PCT,b=True)
        put(ws,r,7,int(oo),NUMF); put(ws,r,8,int(so),NUMF)
        put(ws,r,9,f'=IFERROR(H{r}/G{r},"")',PCT,b=True)
        put(ws,r,10,f'=IFERROR((E{r}+H{r})/B{r},"")',PCT)
        if prev:
            # mix pts: (share_off_m - share_off_prev)*(rate_off_prev - rate_in_prev)
            put(ws,r,11,f'=IFERROR((C{r}-C{prev})*(I{prev}-F{prev}),"")',DPT)
            put(ws,r,12,f'=IFERROR(J{r}-J{prev}-K{r},"")',DPT)
        prev=r; r+=1
    nrow=r
    # hour histogram: ops + connect by hour for the two break-adjacent months with data
    r+=1
    put(ws,r,1,"Hour histogram (ops and connect by submit hour)",b=True,fl=GRAY); r+=1
    hist_months=[m for m in months if m in ("2026-08","2026-09")] or months[-2:]
    r=hdr(ws,r,["Hour"]+[x for m in hist_months for x in (f"{m} ops",f"{m} connect")])
    for h in range(24):
        put(ws,r,1,h,NUMF)
        c=2
        for m in hist_months:
            d=Dm[(Dm["month"]==m)&(Dm["hour"]==h)]
            o=d["op"].sum(); s=d["sqo"].sum()
            put(ws,r,c,int(o),NUMF); put(ws,r,c+1,(s/o) if o else None,PCT)
            c+=2
        r+=1
    ch=LineChart(); ch.title="Connect: in-hours vs off-hours vs blended"; ch.height,ch.width=9,20
    ch.y_axis.numFmt='0%'
    for col,nm in ((6,"In-hours"),(9,"Off-hours"),(10,"Blended")):
        ref=Reference(ws,min_col=col,min_row=first-1,max_row=nrow-1)
        ch.series.append(Series(ref,title_from_data=True))
    ch.set_categories(Reference(ws,min_col=1,min_row=first,max_row=nrow-1))
    ws.add_chart(ch,f"N{first}")
    for i,w in enumerate([9,11,10,8,8,9,8,8,9,9,10,10],1): ws.column_dimensions[get_column_letter(i)].width=w

    # ---------------- ClickLag ----------------
    ws=wb.create_sheet("ClickLag")
    r=title(ws,"Click -> lead-date lag (ledger-matched leads only)",
        ["Calibrates freshness caveats: dispositions arrive after the click, so trailing days look lead-less (hazard 4)."],6)
    r=hdr(ws,r,["Lag (days)","Leads","Share","","",""])
    L=D[D["click_date"].notna()].copy()
    L["lag"]=[(a-b).days for a,b in zip(L["ld"],L["click_date"])]
    tot=len(L)
    for lo,hi,lbl in ((0,0,"0"),(1,1,"1"),(2,3,"2-3"),(4,7,"4-7"),(8,10**6,"8+")):
        n=((L["lag"]>=lo)&(L["lag"]<=hi)).sum()
        put(ws,r,1,lbl); put(ws,r,2,int(n),NUMF); put(ws,r,3,n/tot if tot else None,PCT); r+=1
    neg=(L["lag"]<0).sum()
    if neg: put(ws,r,1,f"negative lag rows: {neg} (clock/attribution quirks -- inspect before trusting)"); r+=1

    # ---------------- MatchType ----------------
    ws=wb.create_sheet("MatchType")
    r=title(ws,"Lead outcomes by match type AS-SERVED at click (ledger-matched leads, post-horizon months)",
        ["Tests the marginal-traffic hypothesis: if the September connect break concentrates in BROAD,",
         "the marginal expansion clicks were the harder-to-reach people -- our-side mechanism.",
         "match_type is materialized at click time (architect memo): what served then, not what the keyword is now."],8)
    r=hdr(ws,r,["Month","Match type","Leads","Ops","SQO","Connect","CRI","Prem/op","Pay (est)","KPI est"])
    ML=D[(D["click_date"].notna())&(D["match_type"].notna())]
    def metric_row(ws,r,lbl1,lbl2,d,b2=False):
        o=d["op"].sum(); pay=d["pay"].sum()
        put(ws,r,1,lbl1); put(ws,r,2,lbl2,b=b2)
        put(ws,r,3,len(d),NUMF); put(ws,r,4,int(o),NUMF); put(ws,r,5,int(d["sqo"].sum()),NUMF)
        put(ws,r,6,(d["sqo"].sum()/o) if o else None,PCT,b=True)
        put(ws,r,7,int(d["cri"].sum()),NUMF)
        put(ws,r,8,(d["prem"].sum()/o) if o else None,MONEY)
        put(ws,r,9,round(pay,2),MONEY)
        put(ws,r,10,(d["prem"].sum()/pay) if pay else None,'0.00',b=True)
        return r+1
    tbl_first=r; cat_vals={}
    mts=("EXACT","PHRASE","BROAD")
    for m in [x for x in months if x>="2026-08"]:
        for mt in mts:
            d=ML[(ML["month"]==m)&(ML["match_type"]==mt)]
            if not len(d): continue
            o=d["op"].sum(); pay=d["pay"].sum()
            cat_vals[(m,mt)]=((d["sqo"].sum()/o) if o else None,(d["prem"].sum()/pay) if pay else None)
            r=metric_row(ws,r,m,mt,d,b2=(mt=="BROAD"))
    for i,w in enumerate([9,10,8,8,8,9,7,9,10,8],1): ws.column_dimensions[get_column_letter(i)].width=w
    globals()["metric_row"]=metric_row

    def cat_charts(ws, anchor_row, cats, series_names, vals, hc=13):
        """Hidden helper grid (col hc = category labels) + Op-SQO% line + Policy KPI bar to the right."""
        g0=anchor_row
        for j,nm in enumerate(series_names):
            ws.cell(g0,hc+1+j,nm).font=F(8)             # connect grid headers
            ws.cell(g0,hc+2+len(series_names)+j,nm).font=F(8)   # kpi grid headers
        for i,cat in enumerate(cats):
            rr=g0+1+i
            ws.cell(rr,hc,cat).font=F(8)
            for j,nm in enumerate(series_names):
                cv,kv=vals.get((cat,nm),(None,None))
                if cv is not None: ws.cell(rr,hc+1+j,cv).font=F(8)
                if kv is not None: ws.cell(rr,hc+2+len(series_names)+j,kv).font=F(8)
        n=len(cats); k=len(series_names)
        for col in range(hc,hc+2+2*k): ws.column_dimensions[get_column_letter(col)].hidden=True
        ln=LineChart(); ln.title="Connect rate (Op-SQO %)"; ln.height,ln.width=7,12
        ln.y_axis.numFmt='0%'; ln.y_axis.title="Op-SQO %"
        for j in range(k):
            ln.series.append(Series(Reference(ws,min_col=hc+1+j,min_row=g0,max_row=g0+n),title_from_data=True))
        ln.set_categories(Reference(ws,min_col=hc,min_row=g0+1,max_row=g0+n))
        ws.add_chart(ln,f"{get_column_letter(hc+2+2*k+1)}{anchor_row}")
        bc=BarChart(); bc.title="Policy KPI"; bc.height,bc.width=7,12
        bc.y_axis.numFmt='0.00'
        for j in range(k):
            bc.series.append(Series(Reference(ws,min_col=hc+2+k+j,min_row=g0,max_row=g0+n),title_from_data=True))
        bc.set_categories(Reference(ws,min_col=hc,min_row=g0+1,max_row=g0+n))
        ws.add_chart(bc,f"{get_column_letter(hc+2+2*k+1)}{anchor_row+15}")
    globals()["cat_charts"]=cat_charts
    cat_charts(ws, tbl_first, [x for x in months if x>="2026-08"], list(mts), cat_vals)

    # ---------------- DeviceSlot ----------------
    ws=wb.create_sheet("DeviceSlot")
    r=title(ws,"Lead outcomes by click device and slot (ledger-matched, post-horizon)",
        ["Device here is per-click from the ledger (NO device='ALL' rollup -- opposite of cpd convention)."],8)
    dv_vals={}; dv_first=None
    for dim,vals in (("device",("MOBILE","DESKTOP","TABLET")),("slot",("SEARCH_TOP","SEARCH_OTHER"))):
        put(ws,r,1,f"By {dim}",b=True,fl=GRAY); r+=1
        r=hdr(ws,r,["Month",dim,"Leads","Ops","SQO","Connect","CRI","Prem/op","Pay (est)","KPI est"])
        if dim=="device": dv_first=r
        for m in [x for x in months if x>="2026-08"]:
            for v in vals:
                d=ML[(ML["month"]==m)&(ML[dim]==v)]
                if not len(d): continue
                if dim=="device":
                    o=d["op"].sum(); pay=d["pay"].sum()
                    dv_vals[(m,v)]=((d["sqo"].sum()/o) if o else None,(d["prem"].sum()/pay) if pay else None)
                r=metric_row(ws,r,m,v,d)
        r+=1
    for i,w in enumerate([9,13,8,8,8,9,7,9,10,8],1): ws.column_dimensions[get_column_letter(i)].width=w
    cat_charts(ws, dv_first, [x for x in months if x>="2026-08"], ["MOBILE","DESKTOP","TABLET"], dv_vals)

    # ---------------- RawDaily: keyword x day, platform + disposition ----------------
    kpd=None
    try: ki_cols={r[1].lower() for r in con.execute("PRAGMA table_info(keyword_instances)")}
    except sqlite3.OperationalError: ki_cols=set()
    names=[r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view')")]
    names=sorted(names, key=lambda n: n!="keyword_performance_daily")   # canonical table first
    for t in names:
        if t=="gclid_clicks" or "search_query" in t.lower(): continue   # sqp is query-grain, not keyword-daily
        try: cols={r[1].lower() for r in con.execute(f"PRAGMA table_info({t})")}
        except sqlite3.OperationalError: continue
        datec="date" if "date" in cols else ("report_date" if "report_date" in cols else None)
        if datec is None or not ({"instance_id","impressions","clicks"} <= cols): continue
        w=[f"t.{datec} >= '{LEDGER_START}'"]; join=""; scope=None
        kik="id" if "id" in ki_cols else ("instance_id" if "instance_id" in ki_cols else None)
        if "account_id" in cols:
            w.append("t.account_id=29"); scope="t.account_id"
        elif kik and "account_id" in ki_cols:
            join=f" JOIN keyword_instances ki ON ki.{kik}=t.instance_id"
            w.append("ki.account_id=29"); scope=f"keyword_instances join (ki.{kik})"
        if scope is None:
            raise SystemExit(f"[rawdaily] ABORT: cannot scope {t} to account 29 (no account_id on {t}; "
                             "keyword_instances key/account_id not found) -- refusing an unscoped multi-account pull")
        if "device" in cols and con.execute(f"SELECT 1 FROM {t} WHERE device='ALL' LIMIT 1").fetchone():
            w.append("t.device='ALL'")
        conv=("SUM(t.conversions) conv" if "conversions" in cols else
              "SUM(t.primary_conv_count) conv" if "primary_conv_count" in cols else "NULL conv")
        cost=("SUM(t.cost_micros)/1e6 cost" if "cost_micros" in cols else
              "SUM(t.cost) cost" if "cost" in cols else
              "SUM(t.spend) cost" if "spend" in cols else "NULL cost")
        kpd=pd.read_sql_query(
            f"""SELECT t.{datec} date, t.instance_id, SUM(t.impressions) impr, SUM(t.clicks) clicks,
                       {conv}, {cost} FROM {t} t{join} WHERE {' AND '.join(w)} GROUP BY 1,2""",con)
        print(f"[rawdaily] platform keyword-daily from {t} (date col={datec}; scope={scope}; {len(kpd)} rows)")
        break
    if kpd is None:
        print("[rawdaily] !! no keyword-daily platform table found -- platform columns will be empty")
        kpd=pd.DataFrame(columns=["date","instance_id","impr","clicks","conv","cost"])
    kpd["date"]=pdate(kpd["date"])
    kpd["instance_id"]=pd.to_numeric(kpd["instance_id"],errors="coerce").astype("float64")
    # keyword dim: ledger as-served attributes win; keyword_instances fills platform-only rows
    dim=(D[D["instance_id"].notna()]
         .sort_values("click_date")
         .groupby("instance_id")[["keyword_text","match_type","campaign_id","ad_group_id"]].last()
         .reset_index())
    want=[c for c in ("keyword_text","match_type","campaign_id","ad_group_id") if c in ki_cols]
    kik="id" if "id" in ki_cols else ("instance_id" if "instance_id" in ki_cols else None)
    if kik and want:
        kw=("WHERE account_id=29" if "account_id" in ki_cols else "")
        kidim=pd.read_sql_query(f"SELECT {kik} AS instance_id,{','.join(want)} FROM keyword_instances {kw}",con)
        kidim["instance_id"]=pd.to_numeric(kidim["instance_id"],errors="coerce").astype("float64")
        for c in ("keyword_text","match_type","campaign_id","ad_group_id"):
            if c not in kidim.columns: kidim[c]=None
        dim=(pd.concat([kidim[dim.columns],dim],ignore_index=True)
               .drop_duplicates("instance_id",keep="last"))
    dim=dim.set_index("instance_id")
    # disposition aggregated to CLICK day x instance (architect ruling: align grains with the
    # platform table -- a ledger-matched lead guarantees >=1 click on that keyword-day)
    dd=(D[D["instance_id"].notna()]
        .groupby(["click_date","instance_id"])
        .agg(leads=("op","size"),op=("op","sum"),sqo=("sqo","sum"),cri=("cri","sum"),
             prem=("prem","sum"),pay=("pay","sum")).reset_index()
        .rename(columns={"click_date":"cd"}))
    RD=kpd.merge(dd,left_on=["date","instance_id"],right_on=["cd","instance_id"],how="outer")
    RD["date"]=RD["date"].fillna(RD["cd"])
    RD=RD.merge(dim,on="instance_id",how="left").sort_values(["date","campaign_id","ad_group_id","keyword_text"])
    ws=wb.create_sheet("RawDaily")
    r=title(ws,"Raw daily: keyword x match type x day -- platform + disposition",
        [f"Platform = keyword-daily table (impr/clicks/cost/conv). Disposition = ledger-matched leads by CLICK day (clicks from {LEDGER_START}) -- grains aligned.",
         "A lead row with NO platform data is an anomaly (its click-day should have served -- investigate); rows without leads = served, no ledger lead.",
         "Plat conv = kpd conversions column AS SYNCED (confirm primary vs all actions) -- platform canon; never reconcile against feed Leads/Ops.",
         "KPI est = Prem/Pay(est). Values computed, not formulas, to keep this sheet light for pivoting."],19)
    r=hdr(ws,r,["Date","Campaign","Ad group","Keyword","Match","Impr","Clicks","Cost","Plat conv",
                "Leads","Ops","SQO","Op-SQO%","CRI","CRI/SQO%","Prem","Prem/op","Pay (est)","KPI est"])
    cN=camp_names if 'camp_names' in dir() else {}
    camp_names_rd, ag_names_rd = name_maps(con)
    f8=F(8)
    import math
    def sval(ws,r,c,v,fmt=None):
        cell=ws.cell(r,c,v); cell.font=f8
        if fmt: cell.number_format=fmt
    nrows=0
    for t_ in RD.itertuples(index=False):
        op=0.0 if (t_.op is None or (isinstance(t_.op,float) and math.isnan(t_.op))) else float(t_.op)
        sqo=0.0 if (t_.sqo is None or (isinstance(t_.sqo,float) and math.isnan(t_.sqo))) else float(t_.sqo)
        cri=0.0 if (t_.cri is None or (isinstance(t_.cri,float) and math.isnan(t_.cri))) else float(t_.cri)
        prem=0.0 if (t_.prem is None or (isinstance(t_.prem,float) and math.isnan(t_.prem))) else float(t_.prem)
        pay=0.0 if (t_.pay is None or (isinstance(t_.pay,float) and math.isnan(t_.pay))) else float(t_.pay)
        sval(ws,r,1,str(t_.date))
        sval(ws,r,2,camp_names_rd.get(str(t_.campaign_id),str(t_.campaign_id) if t_.campaign_id==t_.campaign_id else ""))
        sval(ws,r,3,ag_names_rd.get(str(t_.ad_group_id),str(t_.ad_group_id) if t_.ad_group_id==t_.ad_group_id else ""))
        sval(ws,r,4,t_.keyword_text if isinstance(t_.keyword_text,str) else "")
        sval(ws,r,5,t_.match_type if isinstance(t_.match_type,str) else "")
        if t_.impr==t_.impr and t_.impr is not None:
            sval(ws,r,6,int(t_.impr),NUMF); sval(ws,r,7,int(t_.clicks or 0),NUMF)
            if t_.cost==t_.cost and t_.cost is not None: sval(ws,r,8,round(float(t_.cost),2),MONEY)
            if t_.conv==t_.conv and t_.conv is not None: sval(ws,r,9,round(float(t_.conv),1),NUMF)
        if t_.leads==t_.leads and t_.leads is not None and t_.leads>0:
            sval(ws,r,10,int(t_.leads),NUMF); sval(ws,r,11,int(op),NUMF); sval(ws,r,12,int(sqo),NUMF)
            if op: sval(ws,r,13,sqo/op,PCT)
            sval(ws,r,14,int(cri),NUMF)
            if sqo: sval(ws,r,15,cri/sqo,PCT)
            sval(ws,r,16,round(prem,2),MONEY)
            if op: sval(ws,r,17,round(prem/op,2),MONEY)
            sval(ws,r,18,round(pay,2),MONEY)
            if pay: sval(ws,r,19,round(prem/pay,2),'0.00')
        r+=1; nrows+=1
    ws.freeze_panes="A"+str(r-nrows)
    for i,w in enumerate([11,22,20,20,8,8,7,9,8,7,6,6,8,6,8,10,9,10,7],1): ws.column_dimensions[get_column_letter(i)].width=w
    print(f"[rawdaily] {nrows} rows written")

    # ---------------- Campaign weekly sheets ----------------
    camp_names, ag_names = name_maps(con)
    sp = adgroup_spend(con)
    TOP_ADGROUPS = 8
    CL=D[(D["click_date"].notna())&(D["campaign_id"].notna())].copy()
    CL["week"]=CL["ld"].map(monday)
    CL=CL[CL["week"]>=date(*map(int,WEEK_START.split("-")))]
    weeks=sorted(CL["week"].unique())
    def weekly_block(ws,r,sub,label):
        lbl=r
        put(ws,r,1,label,b=True,fl=GRAY); r+=1
        r=hdr(ws,r,["Week","","Leads","Ops","SQO","Connect","CRI","Prem/op","Pay (est)","KPI est",
                    "Ops T1-3","Ops T4-7","Ops T8-10"])
        first=r
        for wk in weeks:
            d=sub[sub["week"]==wk]
            if not len(d): continue
            r=metric_row(ws,r,str(wk),"",d)
            for j,bnd in enumerate(("1-3","4-7","8-10")):
                put(ws,r-1,11+j,int(d[d["band"]==bnd]["op"].sum()),NUMF)
        last=r-1
        if last>=first:
            ln=LineChart(); ln.title="Connect rate (Op-SQO %)"; ln.height,ln.width=6,11
            ln.y_axis.numFmt='0%'; ln.y_axis.title="Op-SQO %"
            ln.legend=None; ln.varyColors=False
            s=Series(Reference(ws,min_col=6,min_row=first,max_row=last),title="Op-SQO %")
            s.graphicalProperties.line.solidFill="1F3864"; s.graphicalProperties.line.width=20000
            ln.series.append(s)
            ln.set_categories(Reference(ws,min_col=1,min_row=first,max_row=last))
            ws.add_chart(ln,f"O{lbl}")
            bc=BarChart(); bc.title="Policy KPI"; bc.height,bc.width=6,11
            bc.y_axis.numFmt='0.00'
            bc.legend=None; bc.varyColors=False
            s=Series(Reference(ws,min_col=10,min_row=first,max_row=last),title="Policy KPI")
            s.graphicalProperties.solidFill="4472C4"
            bc.series.append(s)
            bc.set_categories(Reference(ws,min_col=1,min_row=first,max_row=last))
            ws.add_chart(bc,f"V{lbl}")
        return max(r, lbl+13)+1
    for cid in sorted(CL["campaign_id"].astype(str).unique()):
        cname=camp_names.get(cid, f"Campaign {cid}")
        sheet=cname[:31].replace("/","-").replace("\\","-").replace("?","").replace("*","").replace("[","(").replace("]",")")
        wsx=wb.create_sheet(sheet)
        sub=CL[CL["campaign_id"].astype(str)==cid]
        r2=title(wsx,f"{cname}: weekly (Mon-Sat) lead outcomes, campaign + ad group",
            [f"Ledger-matched leads only, weeks from {WEEK_START} (gclid ledger horizon). Policy KPI est = Prem / (Ops x per-op rate "
             f"by segment & lead month); break-even ~ {BREAK_EVEN}. Feed-grain counts; premium net of removed sales; recent weeks still maturing.",
             "Ad group names as they appear in the account structure."],10)
        r2=weekly_block(wsx,r2,sub,f"{cname} -- ALL AD GROUPS")
        top=sorted(sub["ad_group_id"].astype(str).unique(), key=lambda g:-sp.get(g,0))[:TOP_ADGROUPS]
        for agid in top:
            agname=ag_names.get(agid, f"AdGroup {agid}")
            r2=weekly_block(wsx,r2,sub[sub["ad_group_id"].astype(str)==agid],
                            f"{agname}   (spend ${sp.get(agid,0):,.0f})")
        for i,w in enumerate([11,4,8,8,8,9,7,9,10,8,8,8,8],1): wsx.column_dimensions[get_column_letter(i)].width=w

    out_dir=a.out_dir or os.path.join(os.path.dirname(os.path.abspath(a.db)),"..","output")
    os.makedirs(out_dir,exist_ok=True)
    out=os.path.join(out_dir,f"termlife_click_enrichment_{str(b_last)[:10].replace('-','')}.xlsx")
    wb.save(out)
    print(f"[done] {out}")

if __name__=="__main__":
    main()
