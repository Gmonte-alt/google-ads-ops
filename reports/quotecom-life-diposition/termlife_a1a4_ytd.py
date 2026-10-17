#!/usr/bin/env python3
"""
termlife_a1a4_ytd.py
====================
Early-bind quality (a1 vs a4) across the YEAR, by cohort week x segment,
for Term Life (acct 29) -- extends the Sept-29 pre/post day-1 analysis to the
full reachable history, to locate WHEN quality degraded and in which segment.

METHODS
  A. BIND-LAG RECONSTRUCTION (reaches back to ~2026-06-26):
     from a single batch, prem bound within lead_date+1 (~a1 proxy) and +4
     (~a4 proxy) via bound_date - lead_date. For each cohort the script uses
     the LATEST batch that still contains it (feed = rolling 90-day window),
     falling back to the earliest batch for cohorts that have aged out.
  B. TRUE BATCH-HISTORY a1/a4 (cohorts >= first_batch - 1 day only):
     exact same-age reads, as in the Sept-29 analysis.
  VALIDATION: cohorts where both exist are compared side by side; trust the
     backward extension only if the deltas are small (clawbacks/revisions are
     the expected source of disagreement -- reconstruction carries each row's
     FINAL premium, not what was visible at the time).

PRIMARY METRIC: premium per op (rate-free -- immune to the June SQ payout
  step). KPI(est) via rate card is secondary. Paid WebM segments only.

Output: output/termlife_a1a4_ytd_{batch}.xlsx
Sheets: Summary | WeeklyQuality | BySegment | Validation | DataNotes
"""
import argparse, os, sqlite3, sys
from collections import defaultdict
from datetime import date, datetime, timedelta

import pandas as pd
from openpyxl import Workbook
from openpyxl.chart import LineChart, Reference, Series
from openpyxl.chart.marker import Marker
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

DEF_DB = r"C:\Projects\google-ads-ops\db\paid_search.db"
SEG_OF_CODE = {"CC": "High", "CB": "Medium", "CA": "Low"}
SEG_OP_RATES = {  # per-op payment by cohort month (observed Looker Spend/Ops)
    "High":   {"2026-06": 150.0, "2026-07": 150.0, "2026-08": 149.67, "2026-09": 144.02, "2026-10": 148.53},
    "Medium": {"2026-06":  87.0, "2026-07":  87.0, "2026-08":  86.55, "2026-09":  84.33, "2026-10":  86.75},
    "Low":    {"2026-06":  31.0, "2026-07":  31.0, "2026-08":  30.98, "2026-09":  31.51, "2026-10":  33.24},
}
BREAK_EVEN = 2.96
EVENTS = [("2026-08-14", "budget flag"), ("2026-08-17", "Google pacing update"),
          ("2026-09-29", "schedule cut + seas adj"), ("2026-10-02", "seas adj removed")]

SNAP = dict(batch="batch_date", code="source_code", lead="day", op="op_count",
            cri="cris", prem="capped_cri_dollars", bound="bound_date")

NAVY="1F3864"; GRAY="D9D9D9"
FILL={"High":"DDEBF7","Medium":"E2EFDA","Low":"FCE4D6"}
MONEY='$#,##0'; KPIF='0.00'; NUMF='#,##0'; PCT='0.0%'
def F(sz=10,b=False,c="000000"): return Font(name="Arial",size=sz,bold=b,color=c)
def fill(h): return PatternFill("solid",fgColor=h)
THIN=Border(*[Side(style="thin",color="BFBFBF")]*4)
def put(ws,r,c,v,fmt=None,b=False,fl=None):
    cell=ws.cell(r,c,v); cell.font=F(10,b)
    if fmt: cell.number_format=fmt
    if fl: cell.fill=fill(fl)
    cell.border=THIN; return cell
def title(ws,t,lines,w):
    ws.cell(1,1,t).font=F(14,True,"FFFFFF")
    for c in range(1,w+1): ws.cell(1,c).fill=fill(NAVY)
    ws.row_dimensions[1].height=24
    r=2
    for ln in lines:
        ws.cell(r,1,ln).font=F(9,False,"404040"); r+=1
    return r+1
def hdr(ws,r,cols):
    for i,h in enumerate(cols,1):
        c=ws.cell(r,i,h); c.font=F(10,True,"FFFFFF"); c.fill=fill("4472C4")
        c.alignment=Alignment(horizontal="center",wrap_text=True); c.border=THIN
    return r+1

def num(s): return pd.to_numeric(s.astype(str).str.replace(r"[$,]","",regex=True),errors="coerce").fillna(0.0)
def pdate(s): return pd.to_datetime(s.astype(str),errors="coerce",format="mixed").dt.date
def monday(d): return d - timedelta(days=d.weekday())
def rate(seg,d):
    m=f"{d.year}-{d.month:02d}"; r=SEG_OP_RATES[seg]
    return r.get(m, r[sorted(r)[-1]])

def load_batch(con, batch):
    q=(f'SELECT "{SNAP["code"]}" code, "{SNAP["lead"]}" ld, "{SNAP["op"]}" op, '
       f'"{SNAP["cri"]}" cri, "{SNAP["prem"]}" prem, "{SNAP["bound"]}" bd '
       f'FROM life_disposition_snapshot WHERE "{SNAP["batch"]}" = ?')
    df=pd.read_sql_query(q,con,params=(str(batch),))
    df["ld"]=pdate(df["ld"]); df["bd"]=pdate(df["bd"])
    df=df.dropna(subset=["ld"])
    df["code"]=df["code"].astype(str)
    df=df[df["code"].str.len()>=3]
    df["seg"]=df["code"].str[:2].map(SEG_OF_CODE)
    df=df[(df["seg"].notna())&(df["code"].str[2]=="M")]
    for c in ("op","cri","prem"): df[c]=num(df[c])
    return df

def load_final(con):
    """v1.2 stitch: every cohort read from the LAST batch containing it.
    Rolled-out cohorts resolve to their day-90 (frozen) snapshot instead of the
    earliest batch -- fixes understated CRI tot / Prem tot on pre-window cohorts."""
    q=(f'SELECT s."{SNAP["code"]}" code, s."{SNAP["lead"]}" ld, s."{SNAP["op"]}" op, '
       f's."{SNAP["cri"]}" cri, s."{SNAP["prem"]}" prem, s."{SNAP["bound"]}" bd, '
       f's."{SNAP["batch"]}" sb '
       f'FROM life_disposition_snapshot s '
       f'JOIN (SELECT "{SNAP["lead"]}" d, MAX("{SNAP["batch"]}") mb '
       f'      FROM life_disposition_snapshot GROUP BY 1) m '
       f'ON s."{SNAP["lead"]}"=m.d AND s."{SNAP["batch"]}"=m.mb')
    df=pd.read_sql_query(q,con)
    df["ld"]=pdate(df["ld"]); df["bd"]=pdate(df["bd"]); df["sb"]=pdate(df["sb"])
    df=df.dropna(subset=["ld"])
    df["code"]=df["code"].astype(str)
    df=df[df["code"].str.len()>=3]
    df["seg"]=df["code"].str[:2].map(SEG_OF_CODE)
    df=df[(df["seg"].notna())&(df["code"].str[2]=="M")]
    for c in ("op","cri","prem"): df[c]=num(df[c])
    return df

def pull_platform_weekly(con):
    """campaign_performance_daily (acct 29, device='ALL') summed to Mon-based weeks, total + cluster split."""
    import re
    q = """SELECT d.date, c.campaign_name, SUM(d.cost_micros)/1e6 cost, SUM(d.clicks) clicks,
                  SUM(d.impressions) impr, SUM(d.conversions) conv
           FROM campaign_performance_daily d JOIN campaigns c USING(campaign_id)
           WHERE d.account_id=29 AND d.device='ALL' GROUP BY 1,2"""
    out=defaultdict(lambda: dict(cost=0.0,clicks=0,impr=0,conv=0.0,c12=0.0,c3=0.0,c45=0.0))
    try:
        for dte,name,cost,clicks,impr,conv in con.execute(q):
            try: wkd=monday(datetime.strptime(str(dte)[:10],"%Y-%m-%d").date())
            except ValueError: continue
            o=out[wkd]; o["cost"]+=cost or 0; o["clicks"]+=clicks or 0
            o["impr"]+=impr or 0; o["conv"]+=conv or 0
            n=name or ""
            if   re.search(r"CR\s*1",n,re.I): o["c12"]+=cost or 0
            elif re.search(r"CR\s*3",n,re.I): o["c3"]+=cost or 0
            elif re.search(r"CR\s*4",n,re.I): o["c45"]+=cost or 0
    except sqlite3.OperationalError as e:
        print(f"[warn] platform pull failed ({e}) - PlatformVsDispo sheet skipped")
        return {}
    return out

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--db",default=DEF_DB)
    ap.add_argument("--out-dir",default=None)
    a=ap.parse_args()
    con=sqlite3.connect(f"file:{a.db}?mode=ro",uri=True)

    batches=[r[0] for r in con.execute(
        f'SELECT DISTINCT "{SNAP["batch"]}" FROM life_disposition_snapshot ORDER BY 1')]
    if not batches: sys.exit("no batches in life_disposition_snapshot")
    b_first,b_last=batches[0],batches[-1]
    d_first=datetime.strptime(str(b_first)[:10],"%Y-%m-%d").date()
    d_last=datetime.strptime(str(b_last)[:10],"%Y-%m-%d").date()
    print(f"[batches] {b_first} .. {b_last} ({len(batches)})")

    early=load_batch(con,b_first); latest=load_batch(con,b_last)
    print(f"[rows] earliest batch {len(early)}, latest batch {len(latest)}")
    bd_cov = latest[latest["cri"]>0]["bd"].notna().mean() if (latest["cri"]>0).any() else 0
    print(f"[bound_date coverage on CRI rows, latest batch] {bd_cov:.1%}")

    # ---- Method A: per-cohort reconstruction, LAST batch containing each cohort ----
    cutoff = latest["ld"].min()   # earliest cohort still in the rolling window
    A=load_final(con)
    A["src"]=[f"batch {b} (day-90 final)" if x<cutoff else f"batch {str(b_last)[:10]}"
              for b,x in zip(A["sb"],A["ld"])]
    A["lag"]=(pd.to_datetime(A["bd"])-pd.to_datetime(A["ld"])).dt.days
    A["prem1"]=A["prem"].where(A["lag"]<=1,0.0).fillna(0.0)
    A["prem4"]=A["prem"].where(A["lag"]<=4,0.0).fillna(0.0)
    A["cri1"]=((A["cri"]>0)&(A["lag"]<=1)).astype(int)
    A["cri4"]=((A["cri"]>0)&(A["lag"]<=4)).astype(int)
    A["week"]=A["ld"].map(monday)
    A["pay"]=[r_*o for r_,o in zip(A.apply(lambda x: rate(x["seg"],x["ld"]),axis=1),A["op"])]
    # maturity floor: drop cohorts younger than 5 days at their source batch (a4 incomplete)
    A["age_at_src"]=[(b-x).days for b,x in zip(A["sb"],A["ld"])]
    A=A[A["age_at_src"]>=5]

    wk=(A.groupby(["week","seg"]).agg(ops=("op","sum"),pay=("pay","sum"),
         cri1=("cri1","sum"),cri4=("cri4","sum"),cri=("cri","sum"),
         prem1=("prem1","sum"),prem4=("prem4","sum"),prem=("prem","sum"),
         src=("src", "max"), age=("age_at_src","min")).reset_index())
    wkT=(A.groupby("week").agg(ops=("op","sum"),pay=("pay","sum"),
         cri1=("cri1","sum"),cri4=("cri4","sum"),cri=("cri","sum"),
         prem1=("prem1","sum"),prem4=("prem4","sum"),prem=("prem","sum"),
         src=("src","max"),age=("age_at_src","min")).reset_index())

    # ---- Method B: true a1/a4 from batch history (cohorts >= d_first-1) ----
    qb=(f'SELECT "{SNAP["batch"]}" b, "{SNAP["code"]}" code, "{SNAP["lead"]}" ld, '
        f'"{SNAP["op"]}" op, "{SNAP["cri"]}" cri, "{SNAP["prem"]}" prem '
        f'FROM life_disposition_snapshot')
    B=pd.read_sql_query(qb,con)
    B["b"]=pdate(B["b"]); B["ld"]=pdate(B["ld"]); B=B.dropna(subset=["b","ld"])
    B["code"]=B["code"].astype(str); B=B[B["code"].str.len()>=3]
    B["seg"]=B["code"].str[:2].map(SEG_OF_CODE)
    B=B[(B["seg"].notna())&(B["code"].str[2]=="M")]
    for c in ("op","cri","prem"): B[c]=num(B[c])
    B["agei"]=(pd.to_datetime(B["b"])-pd.to_datetime(B["ld"])).dt.days
    true_={}
    for ag in (1,4):
        g=B[B["agei"]==ag].groupby("ld").agg(prem=("prem","sum"),ops=("op","sum")).reset_index()
        for r_ in g.itertuples(): true_.setdefault(r_.ld,{})[ag]=(r_.prem,r_.ops)
    # reconstruction at cohort grain from latest batch for the same cohorts
    reco=A[A["src"].str.contains("day-90")==False].groupby("ld").agg(
        prem1=("prem1","sum"),prem4=("prem4","sum"),ops=("op","sum")).reset_index()
    val=[]
    for r_ in reco.itertuples():
        t=true_.get(r_.ld,{})
        if 1 in t or 4 in t:
            val.append(dict(cohort=r_.ld,
                reco1=r_.prem1,true1=t.get(1,(None,))[0],
                reco4=r_.prem4,true4=t.get(4,(None,))[0]))
    val=pd.DataFrame(val).sort_values("cohort") if val else pd.DataFrame()
    if len(val):
        v4=val.dropna(subset=["true4"])
        if len(v4) and v4["true4"].sum():
            print(f"[validation] cohorts with true a4: {len(v4)} | "
                  f"reco4/true4 total: {v4['reco4'].sum()/v4['true4'].sum():.3f}")

    # ---- workbook ----
    wb=Workbook(); ws=wb.active; ws.title="WeeklyQuality"
    widths=[11,7,9,6,6,6,10,10,10,9,9,9,8,8,26]
    for i,w_ in enumerate(widths,1): ws.column_dimensions[get_column_letter(i)].width=w_
    r=title(ws,"Early-bind quality by cohort week (all paid segments)",
        [f"Method A bind-lag reconstruction: prem bound within +1d (~a1) / +4d (~a4) / total-to-date. Source batch per cohort shown at right; batches {str(b_first)[:10]}..{str(b_last)[:10]}.",
         "prem/op is RATE-FREE (immune to the June payout step). KPI(est)=total prem / rate-card payment. Cohorts <5 days old at source excluded.",
         "Caveat: reconstruction carries FINAL premium per row (clawbacks invisible) -- see Validation sheet. Events: " + ", ".join(f"{d_} {n}" for d_,n in EVENTS)],len(widths))
    r=hdr(ws,r,["Week","Ops","Pay (est)","CRI a1","CRI a4","CRI tot","Prem a1","Prem a4","Prem tot",
                "P/op a1","P/op a4","P/op tot","a4 share","KPI est","Source"])
    first=r
    for row in wkT.sort_values("week").itertuples():
        wks=row.week.isoformat()
        shade="FCE4D6" if wks>="2026-09-29" else None
        put(ws,r,1,wks,fl=shade); put(ws,r,2,row.ops,NUMF,fl=shade); put(ws,r,3,row.pay,MONEY,fl=shade)
        put(ws,r,4,row.cri1,NUMF,fl=shade); put(ws,r,5,row.cri4,NUMF,fl=shade); put(ws,r,6,row.cri,NUMF,fl=shade)
        put(ws,r,7,row.prem1,MONEY,fl=shade); put(ws,r,8,row.prem4,MONEY,fl=shade); put(ws,r,9,row.prem,MONEY,fl=shade)
        for cc,src_c in ((10,"G"),(11,"H"),(12,"I")):
            put(ws,r,cc,f'=IFERROR({src_c}{r}/B{r},"")',NUMF,fl=shade)
        put(ws,r,13,f'=IFERROR(H{r}/I{r},"")',PCT,fl=shade)
        put(ws,r,14,f'=IFERROR(I{r}/C{r},"")',KPIF,b=True,fl=shade)
        put(ws,r,15,f"{row.src} (age>={row.age}d)",fl=shade).font=F(8,False,"808080")
        r+=1
    last=r-1
    ch=LineChart(); ch.title="Premium per op by cohort week: bound<=1d vs <=4d vs total"
    ch.height,ch.width=10,26; ch.y_axis.title="prem / op ($)"
    for col,name in ((10,"a1 proxy (bound +1d)"),(11,"a4 proxy (bound +4d)"),(12,"total to date")):
        s=Series(Reference(ws,min_col=col,min_row=first,max_row=last),title=name)
        s.marker=Marker(symbol="circle",size=4); ch.series.append(s)
    ch.set_categories(Reference(ws,min_col=1,min_row=first,max_row=last))
    ws.add_chart(ch,f"A{last+2}")
    ws.freeze_panes=f"A{first}"

    ws2=wb.create_sheet("BySegment")
    for i,w_ in enumerate([11,8]+[9]*9,1): ws2.column_dimensions[get_column_letter(i)].width=w_
    r=title(ws2,"Early-bind quality by cohort week x segment",
        ["Same construction as WeeklyQuality, split High/Medium/Low. Watch WHERE p/op a4 breaks from its own baseline.",
         "Small-n: weekly segment CRI counts are 0-15; read 2-3 week runs, not single weeks."],11)
    r=hdr(ws2,r,["Week","Segment","Ops","Prem a1","Prem a4","Prem tot","P/op a1","P/op a4","P/op tot","a4 share","KPI est"])
    first2=r; seg_rows=defaultdict(list)
    for row in wk.sort_values(["week","seg"]).itertuples():
        wks=row.week.isoformat(); shade=FILL.get(row.seg)
        put(ws2,r,1,wks,fl=shade); put(ws2,r,2,row.seg,fl=shade)
        put(ws2,r,3,row.ops,NUMF,fl=shade)
        put(ws2,r,4,row.prem1,MONEY,fl=shade); put(ws2,r,5,row.prem4,MONEY,fl=shade); put(ws2,r,6,row.prem,MONEY,fl=shade)
        for cc,src_c in ((7,"D"),(8,"E"),(9,"F")):
            put(ws2,r,cc,f'=IFERROR({src_c}{r}/C{r},"")',NUMF,fl=shade)
        put(ws2,r,10,f'=IFERROR(E{r}/F{r},"")',PCT,fl=shade)
        put(ws2,r,11,f'=IFERROR(F{r}/(C{r}*{rate(row.seg,row.week)}),"")',KPIF,fl=shade)
        seg_rows[row.seg].append(r); r+=1
    ch2=LineChart(); ch2.title="P/op a4-proxy by segment (quality at speed)"
    ch2.height,ch2.width=10,26; ch2.y_axis.title="prem/op bound within 4d ($)"
    # per-seg series need contiguous rows; chart from helper columns
    hc=13
    weeks=sorted(wkT["week"])
    ws2.cell(first2-1,hc,"wk"); 
    for j,seg in enumerate(("High","Medium","Low")):
        ws2.cell(first2-1,hc+1+j,seg)
    m={(row.week,row.seg):(row.prem4/row.ops if row.ops else None) for row in wk.itertuples()}
    for i,w_ in enumerate(weeks):
        ws2.cell(first2+i,hc,w_.isoformat())
        for j,seg in enumerate(("High","Medium","Low")):
            ws2.cell(first2+i,hc+1+j,m.get((w_,seg)))
    for j,seg in enumerate(("High","Medium","Low")):
        s=Series(Reference(ws2,min_col=hc+1+j,min_row=first2,max_row=first2+len(weeks)-1),title=seg)
        s.marker=Marker(symbol="circle",size=4); ch2.series.append(s)
    ch2.set_categories(Reference(ws2,min_col=hc,min_row=first2,max_row=first2+len(weeks)-1))
    ws2.add_chart(ch2,f"A{r+2}")
    for j in range(4): ws2.column_dimensions[get_column_letter(hc+j)].hidden=True
    ws2.freeze_panes=f"A{first2}"

    # ---- PlatformVsDispo ----
    plat=pull_platform_weekly(con)
    if plat:
        wsp=wb.create_sheet("PlatformVsDispo")
        for i,w_ in enumerate([11,9,9,8,7,8,8, 6,9,9,9, 8,8,8,8, 9,9],1):
            wsp.column_dimensions[get_column_letter(i)].width=w_
        r=title(wsp,"Google platform vs disposition outcomes, by cohort/serving week",
            ["Platform = campaign_performance_daily (acct 29, device='ALL'); Spend = GOOGLE cost (Quote's); dispo Pay(est) = SQ payment (rate card).",
             "Clicks~leads land same day, so serving week ~= cohort week. prem/Gcost = partner premium per OUR ad dollar; conv/$1K = what Smart Bidding sees.",
             "KPI(est) by segment = prem_tot / (ops x per-op rate). Trailing ~3 platform days provisional; young dispo weeks still maturing."],17)
        r=hdr(wsp,r,["Week","G spend","CR12 $","CR3 $","CR45 $","Impr","Clicks","CPC","Nat conv","conv/$1K",
                     "Ops","Prem a1","Prem a4","Prem tot","prem/Gcost","KPI High","KPI Med","KPI Low"])
        segm={(row.week,row.seg):row for row in wk.itertuples()}
        firstp=r
        for row in wkT.sort_values("week").itertuples():
            w_=row.week; p=plat.get(w_)
            shade="FCE4D6" if w_.isoformat()>="2026-09-29" else None
            put(wsp,r,1,w_.isoformat(),fl=shade)
            if p:
                put(wsp,r,2,p["cost"],MONEY,fl=shade); put(wsp,r,3,p["c12"],MONEY,fl=shade)
                put(wsp,r,4,p["c3"],MONEY,fl=shade);  put(wsp,r,5,p["c45"],MONEY,fl=shade)
                put(wsp,r,6,p["impr"],NUMF,fl=shade); put(wsp,r,7,p["clicks"],NUMF,fl=shade)
                put(wsp,r,8,f'=IFERROR(B{r}/G{r},"")','$#,##0.00',fl=shade)
                put(wsp,r,9,p["conv"],'#,##0.0',fl=shade)
                put(wsp,r,10,f'=IFERROR(1000*I{r}/B{r},"")','#,##0.0',fl=shade)
            put(wsp,r,11,row.ops,NUMF,fl=shade)
            put(wsp,r,12,row.prem1,MONEY,fl=shade); put(wsp,r,13,row.prem4,MONEY,fl=shade)
            put(wsp,r,14,row.prem,MONEY,fl=shade)
            put(wsp,r,15,f'=IFERROR(N{r}/B{r},"")',KPIF,b=True,fl=shade)
            for cc,seg in ((16,"High"),(17,"Medium"),(18,"Low")):
                srow=segm.get((w_,seg))
                v=(srow.prem/(srow.ops*rate(seg,w_))) if (srow is not None and srow.ops) else None
                put(wsp,r,cc,v,KPIF,fl=FILL[seg])
            r+=1
        lastp=r-1
        ch3=LineChart(); ch3.title="What Google sees vs what SQ binds, per Google $"
        ch3.height,ch3.width=10,26; ch3.y_axis.title="prem per Google $ (left) / conv per $1K"
        for col,name in ((15,"SQ prem per Google $"),(10,"native conv per $1K")):
            s=Series(Reference(wsp,min_col=col,min_row=firstp,max_row=lastp),title=name)
            s.marker=Marker(symbol="circle",size=4); ch3.series.append(s)
        ch3.set_categories(Reference(wsp,min_col=1,min_row=firstp,max_row=lastp))
        wsp.add_chart(ch3,f"A{lastp+2}")
        wsp.freeze_panes=f"A{firstp}"

    ws3=wb.create_sheet("Validation")
    for i,w_ in enumerate([11,10,10,9,10,10,9],1): ws3.column_dimensions[get_column_letter(i)].width=w_
    r=title(ws3,"Method check: bind-lag reconstruction vs true batch-history a1/a4",
        [f"Cohorts {str(d_first - timedelta(days=1))} onward have both. Deltas = premium revisions/clawbacks after the early batch + rows restated between batches.",
         "If ratios here are ~0.9-1.1, the backward extension (pre-9/23 cohorts) is trustworthy at weekly grain."],7)
    r=hdr(ws3,r,["Cohort","Reco a1","True a1","ratio","Reco a4","True a4","ratio"])
    for row in (val.itertuples() if len(val) else []):
        put(ws3,r,1,row.cohort.isoformat())
        put(ws3,r,2,row.reco1,MONEY); put(ws3,r,3,row.true1,MONEY)
        put(ws3,r,4,f'=IFERROR(B{r}/C{r},"")',KPIF)
        put(ws3,r,5,row.reco4,MONEY); put(ws3,r,6,row.true4,MONEY)
        put(ws3,r,7,f'=IFERROR(E{r}/F{r},"")',KPIF)
        r+=1

    ws4=wb.create_sheet("DataNotes")
    ws4.column_dimensions["A"].width=22; ws4.column_dimensions["B"].width=110
    r=title(ws4,"Definitions and limits",[],2)
    for k,v in [
        ("Reach","Feed is a rolling 90-day window; batch history starts "+str(b_first)[:10]+". Earliest reachable cohort = that batch's window start (~2026-06-26). Jan-May 2026 is NOT recoverable from the feed; the May KPI peak cannot be given an a1/a4 read.",),
        ("a1/a4 proxy","Premium on rows whose bound_date is within lead_date+1 / +4. True aN = what a batch N days later actually showed. They differ by later premium revisions (e.g. clawbacks) -- quantified on Validation."),
        ("prem/op","Rate-free quality measure; use it for the degradation story. KPI(est) uses the per-op rate card and IS affected by the June payment step."),
        ("Stitching","Each cohort read from the LAST batch containing it: in-window cohorts from the current batch, rolled-out cohorts from their day-90 (frozen) snapshot. The Source column on WeeklyQuality shows which and the minimum age."),
        ("Maturity floor","Cohorts <5 days old at their source batch excluded (a4 proxy incomplete)."),
        ("Small-n","Weekly segment grain runs 0-15 CRIs; judge 2-3 week runs."),
        ("Events","8/14 budget flag, 8/17 Google pacing update, 8/18-21 Tier1/2 kills, ~9/8 SQ Presidents Club week, 9/29 schedule cut + seasonality adj, 10/2 adj removed. June SQ rate step affects KPI(est) only, never prem/op."),
    ]:
        put(ws4,r,1,k if isinstance(k,str) else k[0],b=True)
        c=put(ws4,r,2,v if isinstance(v,str) else v); c.alignment=Alignment(wrap_text=True,vertical="top")
        ws4.row_dimensions[r].height=30; r+=1

    out=a.out_dir or os.path.join(os.path.dirname(os.path.abspath(__file__)),"output")
    os.makedirs(out,exist_ok=True)
    p=os.path.join(out,f"termlife_a1a4_ytd_{str(b_last)[:10].replace('-','')}.xlsx")
    wb.save(p); print(f"[done] {p}")

if __name__=="__main__": main()
