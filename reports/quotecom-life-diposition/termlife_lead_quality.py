#!/usr/bin/env python3
"""
termlife_lead_quality.py
========================
Lead-quality decomposition for Term Life (acct 29), anchored to the MONTHLY
Policy-KPI decay. Four analyses, one workbook:

  FunnelDecomp  - prem/op = op-rate x (SQO/op) x (CRI/SQO) x (prem/CRI):
                  which stage ate each month's KPI decline (indexed to July).
  DeathReasons  - terminal-status families per lead by month x segment:
                  WHY leads die (cant-reach/ghost vs refused vs cost vs
                  ineligible), with not-yet-worked separated out (SQ capacity,
                  not lead quality).
  IntakeMix     - day-0 declared attributes by month x segment: desired
                  coverage, age, existing insurance, device, SQ lead_tier mix.
                  Leading indicators -- no maturation wait.
  TierValidation- does SQ's own lead_tier predict our realized CRI rate and
                  prem/op? (mature cohorts only) + tier mix by month.

Scope: PAID segments (High/Med/Low; organic QA excluded), channels WebM+Referral+
Transfer -- mirrors the Looker Strategy=Search filter. IntakeMix is WebM-only
(transfer rows lack intake attributes). Stitched like the a1a4 analysis:
each cohort read from the LAST batch containing it (v1.5). In-window cohorts
resolve to the current batch; rolled-out cohorts to the batch where they turned
~90d old -- their true final (frozen) snapshot. v1.4 and earlier read rolled-out
cohorts from the EARLIEST batch, observing July mid-flight at ages ~69-85 and
understating July CRIs/premium vs Looker. Reach: ~2026-06-26 onward (June is
partial -- labeled Jun*).

Output: output/termlife_lead_quality_{batch}.xlsx
"""
import argparse, os, sqlite3, sys
from collections import defaultdict
from datetime import datetime, timedelta

import pandas as pd
from openpyxl import Workbook
from openpyxl.chart import BarChart, LineChart, Reference, Series
from openpyxl.chart.marker import Marker
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

DEF_DB = r"C:\Projects\google-ads-ops\db\paid_search.db"
SEG_OF_CODE = {"CC": "High", "CB": "Medium", "CA": "Low"}
SEG_OP_RATES = {
    "High":   {"2026-06": 150.0, "2026-07": 150.0, "2026-08": 149.67, "2026-09": 144.02, "2026-10": 148.53},
    "Medium": {"2026-06":  87.0, "2026-07":  87.0, "2026-08":  86.55, "2026-09":  84.33, "2026-10":  86.75},
    "Low":    {"2026-06":  31.0, "2026-07":  31.0, "2026-08":  30.98, "2026-09":  31.51, "2026-10":  33.24},
}
BREAK_EVEN = 2.96
BASE_MONTH = "2026-07"          # first full month = index baseline
MATURE_MIN_AGE = 28             # days; TierValidation + stable DeathReasons

SNAP = dict(batch="batch_date", code="source_code", lead="day", op="op_count",
            sqo="sqo", cri="cris", prem="capped_cri_dollars", status="status",
            tier="lead_tier", age="age", exist="existing_ins",
            desire="desired_ins_dollars", device="device_type")

NAVY="1F3864"; GRAY="D9D9D9"
FILL={"High":"DDEBF7","Medium":"E2EFDA","Low":"FCE4D6","Total":"EEEEEE"}
MONEY='$#,##0'; KPIF='0.00'; NUMF='#,##0'; PCT='0.0%'; IDX='0%'
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
    for ln in lines: ws.cell(r,1,ln).font=F(9,False,"404040"); r+=1
    return r+1
def hdr(ws,r,cols):
    for i,h in enumerate(cols,1):
        c=ws.cell(r,i,h); c.font=F(10,True,"FFFFFF"); c.fill=fill("4472C4")
        c.alignment=Alignment(horizontal="center",wrap_text=True); c.border=THIN
    return r+1

def num(s): return pd.to_numeric(s.astype(str).str.replace(r"[$,]","",regex=True),errors="coerce")
def pdate(s): return pd.to_datetime(s.astype(str),errors="coerce",format="mixed").dt.date
def rate(seg,m):
    r=SEG_OP_RATES[seg]; return r.get(m, r[sorted(r)[-1]])

def family(status, cri, sqo=0):
    s=str(status or "").lower()
    if cri>0: return "SOLD"
    if "five9 assignment" in s or "new real time" in s or "send to sqt" in s:
        # queue status is unreliable: rows with SQO>0 have progressed, status just never updated
        return "PIPELINE" if sqo>0 else "ZERO_SIGNAL"
    if "cant reach" in s or "can't reach" in s or "wrong number" in s or s.strip()=="dnc": return "CANT_REACH"
    if "refused" in s or "not interested" in s: return "REFUSED"
    if "- cost" in s: return "COST"
    if "duplicate" in s or "third party" in s or "uninsurable" in s or "ineligible" in s: return "INELIGIBLE"
    return "PIPELINE"
FAMS=["ZERO_SIGNAL","CANT_REACH","REFUSED","COST","INELIGIBLE","SOLD","PIPELINE"]

def load_batch(con,batch):
    cols=", ".join(f'"{v}" AS {k}' for k,v in SNAP.items() if k!="batch")
    df=pd.read_sql_query(
        f'SELECT {cols} FROM life_disposition_snapshot WHERE "{SNAP["batch"]}"=?',con,params=(str(batch),))
    df["lead"]=pdate(df["lead"]); df=df.dropna(subset=["lead"])
    df["code"]=df["code"].astype(str); df=df[df["code"].str.len()>=3]
    df["seg"]=df["code"].str[:2].map(SEG_OF_CODE)
    df["ch"]=df["code"].str[2]
    df=df[(df["seg"].notna())&(df["ch"].isin(("M","R","T")))]   # paid channels = Looker Strategy=Search
    for c in ("op","sqo","cri","prem","age","desire","exist"): df[c]=num(df[c])
    for c in ("op","sqo","cri","prem"): df[c]=df[c].fillna(0.0)
    df["tier"]=num(df["tier"])
    return df

def load_final(con):
    """v1.5 stitch: every cohort read from the LAST batch containing it."""
    cols=", ".join(f's."{v}" AS {k}' for k,v in SNAP.items() if k!="batch")
    df=pd.read_sql_query(
        f'''SELECT {cols}, s."{SNAP["batch"]}" AS src_batch
            FROM life_disposition_snapshot s
            JOIN (SELECT "{SNAP["lead"]}" AS d, "{SNAP["code"]}" AS sc, MAX("{SNAP["batch"]}") AS mb
                  FROM life_disposition_snapshot GROUP BY 1,2) m
              ON s."{SNAP["lead"]}"=m.d AND s."{SNAP["code"]}"=m.sc AND s."{SNAP["batch"]}"=m.mb''',con)
    df["lead"]=pdate(df["lead"]); df=df.dropna(subset=["lead"])
    df["code"]=df["code"].astype(str); df=df[df["code"].str.len()>=3]
    df["seg"]=df["code"].str[:2].map(SEG_OF_CODE)
    df["ch"]=df["code"].str[2]
    df=df[(df["seg"].notna())&(df["ch"].isin(("M","R","T")))]   # paid channels = Looker Strategy=Search
    for c in ("op","sqo","cri","prem","age","desire","exist"): df[c]=num(df[c])
    for c in ("op","sqo","cri","prem"): df[c]=df[c].fillna(0.0)
    df["tier"]=num(df["tier"])
    sb=pdate(df["src_batch"])
    df["src_age"]=[(b-x).days for b,x in zip(sb,df["lead"])]
    return df

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--db",default=DEF_DB); ap.add_argument("--out-dir",default=None)
    a=ap.parse_args()
    con=sqlite3.connect(f"file:{a.db}?mode=ro",uri=True)
    batches=[r[0] for r in con.execute(
        f'SELECT DISTINCT "{SNAP["batch"]}" FROM life_disposition_snapshot ORDER BY 1')]
    if not batches: sys.exit("no batches found")
    b_first,b_last=batches[0],batches[-1]
    A=load_final(con)
    A=A[A["src_age"]>=5]
    A["month"]=A["lead"].map(lambda d:f"{d.year}-{d.month:02d}")
    # mark months whose coverage starts mid-month (archive edge) as partial
    first_cov=A.groupby("month")["lead"].min()
    for m0,d0 in first_cov.items():
        if d0.day>3: A.loc[A["month"]==m0,"month"]=m0+"*"
    A["fam"]=[family(s,c,q) for s,c,q in zip(A["status"],A["cri"],A["sqo"])]
    months=sorted(A["month"].unique())
    base_m = BASE_MONTH if BASE_MONTH in months else next((m for m in months if not m.endswith("*")), months[0])
    print(f"[batches] {b_first}..{b_last} | rows {len(A)} | months {months} | base {base_m}")

    def agg(df):
        leads=len(df); ops=df["op"].sum(); sqo=df["sqo"].sum(); cri=df["cri"].sum(); prem=df["prem"].sum()
        return dict(leads=leads,ops=ops,sqo=sqo,cri=cri,prem=prem)
    def kpi_est(g,seg,m):
        mm=m.replace("*","")
        if seg=="Total":
            pay=sum(agg(A[(A["month"]==m)&(A["seg"]==s)])["ops"]*rate(s,mm) for s in SEG_OF_CODE.values())
        else: pay=g["ops"]*rate(seg,mm)
        return (g["prem"]/pay) if pay else None

    # month x seg aggregates + anchor
    MG={}
    for m in months:
        dm=A[A["month"]==m]
        MG[(m,"Total")]=agg(dm)
        for s in ("High","Medium","Low"): MG[(m,s)]=agg(dm[dm["seg"]==s])
    anchor=" | ".join(f"{m[2:]}: p/op ${MG[(m,'Total')]['prem']/MG[(m,'Total')]['ops']:,.0f}, KPI {kpi_est(MG[(m,'Total')],'Total',m):.2f}"
                      for m in months if MG[(m,'Total')]['ops'])
    min_age={m:int(A[A["month"]==m]["src_age"].min()) for m in months}
    mat_note="Min cohort age by month: "+", ".join(f"{m[2:]} {min_age[m]}d" for m in months)+ \
             f" -- months under ~{MATURE_MIN_AGE}d still maturing (late statuses shift from PIPELINE/NOT_WORKED to terminal)."

    wb=Workbook()

    # ---------------- FunnelDecomp ----------------
    ws=wb.active; ws.title="FunnelDecomp"
    for i,w_ in enumerate([9,8,7,7,8,7,8,7,9,9,9,7, 8,8,8,8,10],1): ws.column_dimensions[get_column_letter(i)].width=w_
    r=title(ws,"Funnel decomposition: prem/op = op-rate x SQO/op x CRI/SQO x prem/CRI",
        [f"KPI decay anchor (feed-based, paid channels M+R+T = Looker Strategy=Search): {anchor}",
         f"Index block = each factor vs {base_m} (first full month): the factor(s) below 100% are where quality leaked. {mat_note}"],16)
    r=hdr(ws,r,["Month","Seg","Leads","Ops","Op rate","SQO","SQO/op","CRI","CRI/SQO","Prem/CRI","Prem/op","KPI est",
                "idx Op%","idx SQO/op","idx CRI/SQO","idx Prem/CRI","Prem"])
    base={}
    for s in ("Total","High","Medium","Low"):
        g=MG[(base_m,s)]
        base[s]=dict(opr=g["ops"]/g["leads"] if g["leads"] else None,
                     so=g["sqo"]/g["ops"] if g["ops"] else None,
                     cs=g["cri"]/g["sqo"] if g["sqo"] else None,
                     pc=g["prem"]/g["cri"] if g["cri"] else None)
    first=r; tot_rows=[]; base_row={}
    for m in months:
        for s in ("Total","High","Medium","Low"):
            g=MG[(m,s)]; fl_=FILL[s]
            if not g["leads"]: continue
            opr=g["ops"]/g["leads"]; so=g["sqo"]/g["ops"] if g["ops"] else None
            cs=g["cri"]/g["sqo"] if g["sqo"] else None; pc=g["prem"]/g["cri"] if g["cri"] else None
            put(ws,r,1,m,fl=fl_); put(ws,r,2,s,b=(s=="Total"),fl=fl_)
            put(ws,r,3,g["leads"],NUMF,fl=fl_); put(ws,r,4,g["ops"],NUMF,fl=fl_)
            put(ws,r,5,f'=IFERROR(D{r}/C{r},"")',PCT,fl=fl_); put(ws,r,6,g["sqo"],NUMF,fl=fl_)
            put(ws,r,7,f'=IFERROR(F{r}/D{r},"")',PCT,fl=fl_); put(ws,r,8,g["cri"],NUMF,fl=fl_)
            put(ws,r,9,f'=IFERROR(H{r}/F{r},"")',PCT,fl=fl_)
            put(ws,r,17,g["prem"],MONEY,fl=fl_)   # prem literal (col Q)
            put(ws,r,10,f'=IFERROR(Q{r}/H{r},"")',MONEY,fl=fl_)
            put(ws,r,11,f'=IFERROR(Q{r}/D{r},"")',MONEY,b=True,fl=fl_)
            put(ws,r,12,kpi_est(g,s,m),KPIF,b=True,fl=fl_)
            if s not in base_row and m==base_m: base_row[s]=r
            br=base_row.get(s)
            for cc,src_c in ((13,"E"),(14,"G"),(15,"I"),(16,"J")):
                put(ws,r,cc,(f'=IFERROR({src_c}{r}/{src_c}${br},"")' if br else None),IDX,fl=fl_)
            if s=="Total": tot_rows.append(r)
            r+=1
    ch=LineChart(); ch.title=f"Stage indices vs {base_m} (Total)"; ch.height,ch.width=9,22
    ch.y_axis.title="index (Jul=100%)"
    hc=18
    ws.cell(first-1,hc,"m")
    for j,nm in enumerate(["Op%","SQO/op","CRI/SQO","Prem/CRI"]): ws.cell(first-1,hc+1+j,nm)
    bs=base["Total"]
    for i,(rr,m) in enumerate(zip(tot_rows,[mm for mm in months if MG[(mm,"Total")]["leads"]])):
        g=MG[(m,"Total")]
        ws.cell(first+i,hc,m)
        vals=[(g["ops"]/g["leads"])/bs["opr"] if bs["opr"] else None,
              (g["sqo"]/g["ops"]/bs["so"]) if (g["ops"] and bs["so"]) else None,
              (g["cri"]/g["sqo"]/bs["cs"]) if (g["sqo"] and bs["cs"]) else None,
              (g["prem"]/g["cri"]/bs["pc"]) if (g["cri"] and bs["pc"]) else None]
        for j,v in enumerate(vals): ws.cell(first+i,hc+1+j,v)
    for j,nm in enumerate(["Op%","SQO/op","CRI/SQO","Prem/CRI"]):
        s_=Series(Reference(ws,min_col=hc+1+j,min_row=first,max_row=first+len(tot_rows)-1),title=nm)
        s_.marker=Marker(symbol="circle",size=5); ch.series.append(s_)
    ch.set_categories(Reference(ws,min_col=hc,min_row=first,max_row=first+len(tot_rows)-1))
    ws.add_chart(ch,f"A{r+1}")
    for j in range(5): ws.column_dimensions[get_column_letter(hc+j)].hidden=True
    ws.freeze_panes=f"A{first}"

    # ---------------- KPI_Bridge ----------------
    # Looker monthly totals (all channels, Strategy=Search), from the 10/09 pull of the
    # Monthly Totals Dispo Report. Literals by design: this sheet answers "is the KPI
    # decline pricing (RPL) or conversion (prem/op)?" in the same units leadership sees.
    LOOKER_MONTHLY = [  # month, CRI prem $, ops, SQ payment $   (Oct = MTD at pull, still maturing)
        ("2026-03", 622221, 1541, 120534), ("2026-04", 495492, 1393, 104358),
        ("2026-05", 439037,  990,  77238), ("2026-06", 624225, 1438, 129871),
        ("2026-07", 767691, 2002, 178700), ("2026-08", 687585, 1971, 171942),
        ("2026-09", 670027, 2305, 194555), ("2026-10 MTD*", 119171, 557, 46956),
    ]
    wsb=wb.create_sheet("KPI_Bridge")
    for i,w_ in enumerate([12,10,7,10,9,8,8, 9,9,9],1): wsb.column_dimensions[get_column_letter(i)].width=w_
    r=title(wsb,"Policy KPI bridge: KPI = (premium per op) / (RPL per op)",
        ["KPI can only move via CONVERSION (prem/op, no dollars in it) or PRICING (RPL = SQ payment per op). This sheet splits every month.",
         "Source: Looker monthly totals (Strategy=Search = paid only) pulled 10/09 -- same filter as the dashboard leadership sees. Oct row is MTD, still maturing.",
         "Read the MoM columns together: KPI% ~= prem/op% - RPL%. June: RPL +16% with conversion ~flat -> the drop was the rate step.",
         "Sept: RPL DOWN ~3% -> the drop is all conversion. Stage-level detail (which conversion stage) is on FunnelDecomp (paid WebM)."],10)
    r=hdr(wsb,r,["Month","CRI Prem","Ops","SQ Payment","Prem/op","RPL","KPI",
                 "MoM prem/op","MoM RPL","MoM KPI"])
    firstb=r
    for i,(m,prem,ops,pay) in enumerate(LOOKER_MONTHLY):
        shade="FCE4D6" if m.startswith("2026-09") or m.startswith("2026-10") else None
        put(wsb,r,1,m,fl=shade); put(wsb,r,2,prem,MONEY,fl=shade); put(wsb,r,3,ops,NUMF,fl=shade)
        put(wsb,r,4,pay,MONEY,fl=shade)
        put(wsb,r,5,f'=IFERROR(B{r}/C{r},"")',MONEY,b=True,fl=shade)
        put(wsb,r,6,f'=IFERROR(D{r}/C{r},"")','$#,##0.00',b=True,fl=shade)
        put(wsb,r,7,f'=IFERROR(B{r}/D{r},"")',KPIF,b=True,fl=shade)
        if i>0:
            for cc,col in ((8,"E"),(9,"F"),(10,"G")):
                put(wsb,r,cc,f'=IFERROR({col}{r}/{col}{r-1}-1,"")','+0.0%;-0.0%',fl=shade)
        r+=1
    lastb=r-1; r+=1
    put(wsb,r,1,"Verdict row-by-row: a KPI drop with RPL UP and prem/op flat = pricing (June). A KPI drop with RPL flat/down = conversion (Sept).").font=F(9,True)
    r+=1
    put(wsb,r,1,f"Break-even {BREAK_EVEN} / target 4.0 apply to the KPI column only; prem/op and RPL have no target -- they explain KPI, not replace it.").font=F(9,False,"808080")
    r+=1
    # chart: indexed to May (pre-rate-step baseline)
    hcb=12
    wsb.cell(firstb-1,hcb,"m")
    for j,nm in enumerate(["prem/op idx","RPL idx","KPI idx"]): wsb.cell(firstb-1,hcb+1+j,nm)
    base=None
    for i,(m,prem,ops,pay) in enumerate(LOOKER_MONTHLY):
        po=prem/ops; rp=pay/ops; k=prem/pay
        if base is None: base=(po,rp,k)
        wsb.cell(firstb+i,hcb,m)
        for j,v in enumerate((po/base[0],rp/base[1],k/base[2])): wsb.cell(firstb+i,hcb+1+j,v)
    chb=LineChart(); chb.title="Indexed to Mar: conversion (prem/op) vs pricing (RPL) vs Policy KPI"
    chb.height,chb.width=9,22; chb.y_axis.title="index (Mar = 100%)"
    for j,nm in enumerate(["Conversion: prem/op","Pricing: RPL","Policy KPI"]):
        s_=Series(Reference(wsb,min_col=hcb+1+j,min_row=firstb,max_row=firstb+len(LOOKER_MONTHLY)-1),title=nm)
        s_.marker=Marker(symbol="circle",size=5); chb.series.append(s_)
    chb.set_categories(Reference(wsb,min_col=hcb,min_row=firstb,max_row=firstb+len(LOOKER_MONTHLY)-1))
    wsb.add_chart(chb,f"A{r+1}")
    for j in range(4): wsb.column_dimensions[get_column_letter(hcb+j)].hidden=True
    wsb.freeze_panes=f"A{firstb}"

    # ---------------- DeathReasons ----------------
    ws2=wb.create_sheet("DeathReasons")
    for i,w_ in enumerate([9,8,7]+[9]*7,1): ws2.column_dimensions[get_column_letter(i)].width=w_
    r=title(ws2,"Terminal-status families (share of leads) by month x segment",
        ["Families: ZERO_SIGNAL (no SQO/CRI AND still in queue status: never progressed -- dial depth or unreachable) | CANT_REACH (worked to exhaustion) | REFUSED (answered, no intent)",
         "| COST (intent, price-killed) | INELIGIBLE (dupe/3rd-party/uninsurable) | SOLD (any CRI) | PIPELINE (in-progress, incl. SQO>0 rows parked in stale queue status).",
         f"Status is CURRENT state at source batch -- young months overweight NOT_WORKED/PIPELINE. {mat_note}"],10)
    r=hdr(ws2,r,["Month","Seg","Leads"]+FAMS)
    first2=r; tot2=[]
    for m in months:
        for s in ("Total","High","Medium","Low"):
            dm=A[(A["month"]==m)] if s=="Total" else A[(A["month"]==m)&(A["seg"]==s)]
            n=len(dm)
            if not n: continue
            put(ws2,r,1,m,fl=FILL[s]); put(ws2,r,2,s,b=(s=="Total"),fl=FILL[s]); put(ws2,r,3,n,NUMF,fl=FILL[s])
            fc=dm["fam"].value_counts()
            for j,f_ in enumerate(FAMS):
                put(ws2,r,4+j,fc.get(f_,0)/n,PCT,fl=FILL[s])
            if s=="Total": tot2.append(r)
            r+=1
    ch2=LineChart(); ch2.title="Death-reason shares by month (Total)"; ch2.height,ch2.width=9,22
    ch2.y_axis.title="share of leads"
    hc=14
    ws2.cell(first2-1,hc,"m")
    plot_f=["ZERO_SIGNAL","CANT_REACH","REFUSED","COST","INELIGIBLE","SOLD"]
    for j,f_ in enumerate(plot_f): ws2.cell(first2-1,hc+1+j,f_)
    for i,rr in enumerate(tot2):
        ws2.cell(first2+i,hc,ws2.cell(rr,1).value)
        for j,f_ in enumerate(plot_f):
            ws2.cell(first2+i,hc+1+j,ws2.cell(rr,4+FAMS.index(f_)).value)
    for j,f_ in enumerate(plot_f):
        s_=Series(Reference(ws2,min_col=hc+1+j,min_row=first2,max_row=first2+len(tot2)-1),title=f_)
        s_.marker=Marker(symbol="circle",size=5); ch2.series.append(s_)
    ch2.set_categories(Reference(ws2,min_col=hc,min_row=first2,max_row=first2+len(tot2)-1))
    ws2.add_chart(ch2,f"A{r+1}")
    for j in range(7): ws2.column_dimensions[get_column_letter(hc+j)].hidden=True
    ws2.freeze_panes=f"A{first2}"

    # ---------------- IntakeMix ----------------
    ws3=wb.create_sheet("IntakeMix")
    for i,w_ in enumerate([9,8,7,10,10,8,9,9,9,9,9,9],1): ws3.column_dimensions[get_column_letter(i)].width=w_
    r=title(ws3,"Day-0 declared attributes by month x segment (leading indicators -- no maturation wait)",
        ["Coverage/age/existing-ins/device/tier are stamped at lead creation: if the mix shifted when volume expanded, dilution is visible same-day.",
         "lead_tier is SQ's own 1-10 score (direction validated on TierValidation; tier 100 = transfer-style rows). WebM only; blanks excluded per metric."],12)
    r=hdr(ws3,r,["Month","Seg","Leads","Med cov $","% cov>=250K","Avg age","% age 35-60","% exist ins","% mobile",
                 "% tier 1-3","% tier 8-10","% tier n/a"])
    first3=r; tot3=[]
    AM=A[A["ch"]=="M"]   # intake attributes exist on WebM rows only
    for m in months:
        for s in ("Total","High","Medium","Low"):
            dm=AM[(AM["month"]==m)] if s=="Total" else AM[(AM["month"]==m)&(AM["seg"]==s)]
            n=len(dm)
            if not n: continue
            fl_=FILL[s]
            put(ws3,r,1,m,fl=fl_); put(ws3,r,2,s,b=(s=="Total"),fl=fl_); put(ws3,r,3,n,NUMF,fl=fl_)
            cov=dm["desire"].dropna(); ag=dm["age"].dropna()
            put(ws3,r,4,cov.median() if len(cov) else None,MONEY,fl=fl_)
            put(ws3,r,5,(cov>=250000).mean() if len(cov) else None,PCT,fl=fl_)
            put(ws3,r,6,ag.mean() if len(ag) else None,'0.0',fl=fl_)
            put(ws3,r,7,((ag>=35)&(ag<=60)).mean() if len(ag) else None,PCT,fl=fl_)
            ex=dm["exist"].dropna()
            put(ws3,r,8,(ex>0).mean() if len(ex) else None,PCT,fl=fl_)
            dev=dm["device"].astype(str).str.lower()
            put(ws3,r,9,(dev=="mobile").mean(),PCT,fl=fl_)
            t=dm["tier"]
            tn=t.notna()&(t<=10)
            put(ws3,r,10,((t>=1)&(t<=3)).mean(),PCT,fl=fl_)
            put(ws3,r,11,((t>=8)&(t<=10)).mean(),PCT,fl=fl_)
            put(ws3,r,12,(~tn).mean(),PCT,fl=fl_)
            if s=="Total": tot3.append(r)
            r+=1
    ch3=LineChart(); ch3.title="Intake mix by month (WebM only): coverage>=250K, age 35-60, mobile"; ch3.height,ch3.width=9,22
    hc=14
    ws3.cell(first3-1,hc,"m")
    for j,(nm,cc) in enumerate((("% cov>=250K",5),("% age 35-60",7),("% mobile",9))):
        ws3.cell(first3-1,hc+1+j,nm)
    for i,rr in enumerate(tot3):
        ws3.cell(first3+i,hc,ws3.cell(rr,1).value)
        for j,cc in enumerate((5,7,9)): ws3.cell(first3+i,hc+1+j,ws3.cell(rr,cc).value)
    for j,nm in enumerate(("% cov>=250K","% age 35-60","% mobile")):
        s_=Series(Reference(ws3,min_col=hc+1+j,min_row=first3,max_row=first3+len(tot3)-1),title=nm)
        s_.marker=Marker(symbol="circle",size=5); ch3.series.append(s_)
    ch3.set_categories(Reference(ws3,min_col=hc,min_row=first3,max_row=first3+len(tot3)-1))
    ws3.add_chart(ch3,f"A{r+1}")
    for j in range(4): ws3.column_dimensions[get_column_letter(hc+j)].hidden=True
    ws3.freeze_panes=f"A{first3}"

    # ---------------- TierValidation ----------------
    ws4=wb.create_sheet("TierValidation")
    for i,w_ in enumerate([10,8,8,9,9,10,10],1): ws4.column_dimensions[get_column_letter(i)].width=w_
    mature=A[A["src_age"]>=MATURE_MIN_AGE]
    r=title(ws4,"Does SQ's lead_tier predict our outcomes?",
        [f"Mature cohorts only (age >= {MATURE_MIN_AGE}d at source batch): {mature['lead'].min()} .. {mature['lead'].max()} ({len(mature):,} leads).",
         "If CRI/op and prem/op slope cleanly across tiers, tier mix is a same-day quality dial (and a candidate offline-value signal for Google).",
         "Payout probe showed price is FLAT across tiers -- tier carries information, not pricing."],7)
    r=hdr(ws4,r,["Tier band","Leads","Ops","CRI","CRI/op","Prem","Prem/op"])
    bands=[("1-3",lambda t:(t>=1)&(t<=3)),("4-5",lambda t:(t>=4)&(t<=5)),
           ("6-7",lambda t:(t>=6)&(t<=7)),("8-10",lambda t:(t>=8)&(t<=10)),
           ("100/other",lambda t:(t>10)),("missing",lambda t:t.isna())]
    first4=r
    for nm,f_ in bands:
        dm=mature[f_(mature["tier"])]
        if not len(dm): continue
        g=agg(dm)
        put(ws4,r,1,nm,b=True); put(ws4,r,2,g["leads"],NUMF); put(ws4,r,3,g["ops"],NUMF)
        put(ws4,r,4,g["cri"],NUMF)
        put(ws4,r,5,f'=IFERROR(D{r}/C{r},"")',PCT,b=True)
        put(ws4,r,6,g["prem"],MONEY)
        put(ws4,r,7,f'=IFERROR(F{r}/C{r},"")',MONEY,b=True)
        r+=1
    last4=r-1
    ch4=BarChart(); ch4.type="col"; ch4.title="Prem/op by SQ tier band (mature cohorts)"
    ch4.height,ch4.width=8,14; ch4.y_axis.title="$/op"
    ch4.series.append(Series(Reference(ws4,min_col=7,min_row=first4,max_row=last4),title="prem/op"))
    ch4.set_categories(Reference(ws4,min_col=1,min_row=first4,max_row=last4))
    ws4.add_chart(ch4,f"A{last4+2}")
    r=last4+20
    put(ws4,r,1,"Tier mix by month (share of leads, tiers 1-10 only)",b=True,fl=GRAY); r+=1
    r=hdr(ws4,r,["Month","1-3","4-5","6-7","8-10","n (tiered)",""][:6])
    for m in months:
        dm=A[(A["month"]==m)]
        t=dm["tier"]; tt=t[(t>=1)&(t<=10)]
        n=len(tt)
        if not n: continue
        put(ws4,r,1,m)
        for j,(lo,hi) in enumerate(((1,3),(4,5),(6,7),(8,10))):
            put(ws4,r,2+j,((tt>=lo)&(tt<=hi)).mean(),PCT)
        put(ws4,r,6,n,NUMF); r+=1

    # within-tier monthly decay (the alignment test: does a tier-3 lead convert worse over time?)
    r+=1
    put(ws4,r,1,"Within-tier decay: SQO/op and prem/op by tier band x month",b=True,fl=GRAY); r+=1
    put(ws4,r,1,"If these rows fall across months at CONSTANT tier, the decline is invisible to SQ's scorer -- contact/working, not lead composition.").font=F(9,False,"808080"); r+=1
    r=hdr(ws4,r,["Month","Band","Leads","SQO/op","prem/op","",""][:5])
    for m in months:
        for nm,lo,hi in (("1-3",1,3),("4-5",4,5),("6-7",6,7),("8-10",8,10)):
            dm=A[(A["month"]==m)&(A["tier"]>=lo)&(A["tier"]<=hi)]
            if len(dm)<20: continue
            g2=agg(dm)
            put(ws4,r,1,m); put(ws4,r,2,nm,b=True); put(ws4,r,3,g2["leads"],NUMF)
            put(ws4,r,4,g2["sqo"]/g2["ops"] if g2["ops"] else None,PCT)
            put(ws4,r,5,g2["prem"]/g2["ops"] if g2["ops"] else None,MONEY)
            r+=1

    # ---- DayOfWeek: volume and connect by weekday x month ----
    wsw=wb.create_sheet("DayOfWeek")
    DOWS=["Mon","Tue","Wed","Thu","Fri","Sat"]
    for i in range(1,len(months)+2):
        wsw.column_dimensions[get_column_letter(i)].width=10 if i==1 else 9
    r=title(wsw,"Day-of-week: lead volume and connect rate by month",
        ["Connect (SQO/op) stabilizes within days of lead creation, so recent months are near-final here. Sundays excluded (account dark).",
         "Read the two MoM delta blocks together: a day whose volume EXPANDED and whose connect FELL is the capacity signature;",
         "a day with flat volume and held connect is the natural control (e.g., Friday in the Aug->Sep step: ops ~flat, connect +1.5pts",
         "while every expanded day fell). Saturday is the stress test: Sep ops +77% vs Aug, connect -11pts."],
        len(months)+1)
    A["dow"]=A["lead"].map(lambda d: d.weekday())          # 0=Mon .. 6=Sun
    AD=A[A["dow"]<=5]
    piv_op=AD.pivot_table(index="dow",columns="month",values="op",aggfunc="sum").fillna(0)
    piv_sq=AD.pivot_table(index="dow",columns="month",values="sqo",aggfunc="sum").fillna(0)
    def gv(piv,di,m):
        try: return float(piv.loc[di,m])
        except KeyError: return 0.0
    op_row={}; sq_row={}; cn_row={}
    def dow_block(lbl, rows_store, fill_vals):
        nonlocal r
        put(wsw,r,1,lbl,b=True,fl=GRAY); r+=1
        r=hdr(wsw,r,["Day"]+months)
        for di,dn in enumerate(DOWS):
            put(wsw,r,1,dn,b=True)
            fill_vals(di,r)
            rows_store[di]=r; r+=1
        r+=1
    dow_block("Ops by weekday", op_row,
        lambda di,rr:[put(wsw,rr,2+mi,gv(piv_op,di,m),NUMF) for mi,m in enumerate(months)])
    dow_block("SQOs by weekday", sq_row,
        lambda di,rr:[put(wsw,rr,2+mi,gv(piv_sq,di,m),NUMF) for mi,m in enumerate(months)])
    def conn_fill(di,rr):
        for mi,m in enumerate(months):
            cl=get_column_letter(2+mi)
            put(wsw,rr,2+mi,f'=IFERROR({cl}{sq_row[di]}/{cl}{op_row[di]},"")',PCT,b=True)
    dow_block("Connect (SQO/op) by weekday", cn_row, conn_fill)
    def dconn_fill(di,rr):
        for mi,m in enumerate(months):
            if mi==0: continue
            cl,pl=get_column_letter(2+mi),get_column_letter(1+mi)
            put(wsw,rr,2+mi,f'=IFERROR({cl}{cn_row[di]}-{pl}{cn_row[di]},"")','+0.0%;-0.0%')
    drow={}; dow_block("MoM change in connect (pts)", drow, dconn_fill)
    def dops_fill(di,rr):
        for mi,m in enumerate(months):
            if mi==0: continue
            cl,pl=get_column_letter(2+mi),get_column_letter(1+mi)
            put(wsw,rr,2+mi,f'=IFERROR({cl}{op_row[di]}/{pl}{op_row[di]}-1,"")','+0%;-0%')
    drow2={}; dow_block("MoM change in ops (%)", drow2, dops_fill)
    chw=LineChart(); chw.title="Connect rate by weekday across months"; chw.height,chw.width=9,22
    chw.y_axis.numFmt='0%'; chw.y_axis.title="SQO/op"
    hdr_row=cn_row[0]-1
    for di,dn in enumerate(DOWS):
        ref=Reference(wsw,min_col=2,max_col=1+len(months),min_row=cn_row[di],max_row=cn_row[di])
        s_=Series(ref,title=dn); chw.series.append(s_)
    chw.set_categories(Reference(wsw,min_col=2,max_col=1+len(months),min_row=hdr_row,max_row=hdr_row))
    wsw.add_chart(chw,f"A{r+1}")

    # ---- WorkedAtAge: SAME-AGE zero-signal & SQO rates from the batch archive ----
    ws5=wb.create_sheet("WorkedAtAge")
    for i,w_ in enumerate([8,12,8,10,9,9],1): ws5.column_dimensions[get_column_letter(i)].width=w_
    r=title(ws5,"Same-age progression check (batch archive): zero-signal share and SQO/op at fixed ages",
        ["Each daily feed re-snapshots every lead, so the archive holds per-lead state history. This sheet reads each cohort at EXACTLY age N days",
         "(batch = lead date + N), removing the maturity confound in the monthly tables. Coverage limited by archive start "+str(b_first)[:10]+":",
         "age 28 covers cohorts ~8/27 on; age 21 ~9/03 on; age 14 ~9/10 on. Weekly cohort rows; ZERO_SIGNAL = no SQO/CRI and still queue-status at that age."],6)
    r=hdr(ws5,r,["Age (d)","Cohort week","Leads","% zero-signal","SQO/op","CRI/op"])
    batch_set={str(b)[:10] for b in batches}
    rows_by_age=defaultdict(dict); rows_by_age_tier=defaultdict(dict)
    for b in batches:
        bd=datetime.strptime(str(b)[:10],"%Y-%m-%d").date()
        df=load_batch(con,b)
        df["agei"]=[(bd-x).days for x in df["lead"]]
        for ag in (14,21,28):
            sub=df[df["agei"]==ag]
            if not len(sub): continue
            sub=sub.copy()
            sub["zs"]=[1 if family(s,c,q)=="ZERO_SIGNAL" else 0 for s,c,q in zip(sub["status"],sub["cri"],sub["sqo"])]
            for ld,g2 in sub.groupby("lead"):
                wk=(ld-timedelta(days=ld.weekday())).isoformat()
                acc=rows_by_age[ag].setdefault(wk,dict(leads=0,zs=0,ops=0,sqo=0,cri=0))
                acc["leads"]+=len(g2); acc["zs"]+=g2["zs"].sum()
                acc["ops"]+=g2["op"].sum(); acc["sqo"]+=g2["sqo"].sum(); acc["cri"]+=g2["cri"].sum()
            for (bnm,lo,hi) in (("1-3",1,3),("4-5",4,5),("6-7",6,7),("8-10",8,10)):
                sb=sub[(sub["tier"]>=lo)&(sub["tier"]<=hi)]
                if not len(sb): continue
                for ld,g2 in sb.groupby("lead"):
                    wk=(ld-timedelta(days=ld.weekday())).isoformat()
                    acc=rows_by_age_tier[ag].setdefault((wk,bnm),dict(leads=0,zs=0,ops=0,sqo=0))
                    acc["leads"]+=len(g2); acc["zs"]+=g2["zs"].sum()
                    acc["ops"]+=g2["op"].sum(); acc["sqo"]+=g2["sqo"].sum()
    first5=r; chart_rows=[]
    for ag in (14,21,28):
        for wk in sorted(rows_by_age[ag]):
            acc=rows_by_age[ag][wk]
            if acc["leads"]<30: continue
            put(ws5,r,1,ag,NUMF); put(ws5,r,2,wk)
            put(ws5,r,3,acc["leads"],NUMF)
            put(ws5,r,4,acc["zs"]/acc["leads"],PCT,b=True)
            put(ws5,r,5,acc["sqo"]/acc["ops"] if acc["ops"] else None,PCT)
            put(ws5,r,6,acc["cri"]/acc["ops"] if acc["ops"] else None,PCT)
            if ag==21: chart_rows.append(r)
            r+=1
    if chart_rows:
        ch5=LineChart(); ch5.title="At age 21d: zero-signal share vs SQO/op by cohort week"; ch5.height,ch5.width=9,20
        for col,nm in ((4,"% zero-signal @21d"),(5,"SQO/op @21d")):
            s_=Series(Reference(ws5,min_col=col,min_row=chart_rows[0],max_row=chart_rows[-1]),title=nm)
            s_.marker=Marker(symbol="circle",size=5); ch5.series.append(s_)
        ch5.set_categories(Reference(ws5,min_col=2,min_row=chart_rows[0],max_row=chart_rows[-1]))
        ws5.add_chart(ch5,f"A{r+2}")
    r+=18 if chart_rows else 1
    put(ws5,r,1,"Zero-signal by SQ TIER BAND at matched age (the dial-prioritization test)",b=True,fl=GRAY); r+=1
    put(ws5,r,1,"If 4-5/6-7 bands show high zero-signal while 1-3 stays low AT THE SAME AGE, SQ's dialer is triaging by tier when volume exceeds capacity.").font=F(9,False,"808080"); r+=1
    r=hdr(ws5,r,["Age (d)","Cohort week","Band","Leads","% zero-signal","SQO/op"])
    for ag in (14,21,28):
        for (wk,bnm) in sorted(rows_by_age_tier[ag]):
            acc=rows_by_age_tier[ag][(wk,bnm)]
            if acc["leads"]<20: continue
            put(ws5,r,1,ag,NUMF); put(ws5,r,2,wk); put(ws5,r,3,bnm,b=True)
            put(ws5,r,4,acc["leads"],NUMF)
            put(ws5,r,5,acc["zs"]/acc["leads"],PCT,b=True)
            put(ws5,r,6,acc["sqo"]/acc["ops"] if acc["ops"] else None,PCT)
            r+=1
    ws5.freeze_panes=f"A{first5}"

    out=a.out_dir or os.path.join(os.path.dirname(os.path.abspath(__file__)),"output")
    os.makedirs(out,exist_ok=True)
    p=os.path.join(out,f"termlife_lead_quality_{str(b_last)[:10].replace('-','')}.xlsx")
    wb.save(p); print(f"[done] {p}")

if __name__=="__main__": main()
