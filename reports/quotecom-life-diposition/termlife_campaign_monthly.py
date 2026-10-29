#!/usr/bin/env python3
"""
termlife_campaign_monthly.py  (v1.2)

Monthly Platform-vs-Disposition tables, ONE SHEET PER CAMPAIGN CLUSTER
(CR12 / CR3 / CR45), each broken out by income segment (High / Medium / Low).

Google side : campaign_performance_daily (account 29, device='ALL'),
              clustered by campaign-name regex (CR1x / CR3 / CR4x).
Dispo side  : life_disposition_snapshot, paid WebM codes (CCMQ/CBMQ/CAMQ).
              The feed has NO campaign dimension (trk_utm_campaign is the
              advert id: 820=Low 821=Med 822=High 893=organic), so leads are
              attributed to campaigns by ZIP x MONTH CLICK SHARE from
              campaign_performance_zip_daily (fractional where campaigns
              shared a zip that month), falling back to zip-year click share,
              then to the zip-cluster map CSV (CR3 list / complement=CR45),
              per that map's join rule.
Stitch      : every (lead-day, source_code) stream read from the LAST batch
              containing it -- robust to a stream missing from one batch.

Sheets: CR12 | CR3 | CR45 | Coverage (attribution QA).
Each campaign sheet: per month -> Total row (platform + dispo) and
High/Medium/Low segment rows (dispo + KPI est).

KPI est = prem / (ops x per-op rate). Rates for 2026-06+ observed from
Looker; Jan-May are ESTIMATES (June rates scaled by the blended $78.02/$90.31
pre-step ratio) -- replace SEG_OP_RATES entries with observed Spend/Ops from
a monthly-by-advert Looker export for exact pre-June KPI.

Output: output/termlife_campaign_monthly_{batch}.xlsx
"""
import argparse, os, re, sqlite3, sys
from datetime import timedelta

import pandas as pd
from openpyxl import Workbook
from openpyxl.chart import LineChart, Reference, Series
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

DEF_DB = r"C:\Projects\google-ads-ops\db\paid_search.db"
SEG_OF_CODE = {"CC": "High", "CB": "Medium", "CA": "Low"}
SEGS = ["High", "Medium", "Low"]
CLUSTERS = ["CR12", "CR3", "CR45"]
BREAK_EVEN = 2.96

PRE_SCALE = 78.02 / 90.31          # blended pre-June / post-June per-op payment
SEG_OP_RATES = {                   # per-op payment; Jan-May = ESTIMATE (see docstring)
    "High":   {"2026-01": 150.0*PRE_SCALE, "2026-02": 150.0*PRE_SCALE, "2026-03": 150.0*PRE_SCALE,
               "2026-04": 150.0*PRE_SCALE, "2026-05": 150.0*PRE_SCALE,
               "2026-06": 150.0, "2026-07": 150.0, "2026-08": 149.67, "2026-09": 144.02, "2026-10": 148.53},
    "Medium": {"2026-01": 87.0*PRE_SCALE, "2026-02": 87.0*PRE_SCALE, "2026-03": 87.0*PRE_SCALE,
               "2026-04": 87.0*PRE_SCALE, "2026-05": 87.0*PRE_SCALE,
               "2026-06": 87.0, "2026-07": 87.0, "2026-08": 86.55, "2026-09": 84.33, "2026-10": 86.75},
    "Low":    {"2026-01": 31.0*PRE_SCALE, "2026-02": 31.0*PRE_SCALE, "2026-03": 31.0*PRE_SCALE,
               "2026-04": 31.0*PRE_SCALE, "2026-05": 31.0*PRE_SCALE,
               "2026-06": 31.0, "2026-07": 31.0, "2026-08": 30.98, "2026-09": 31.51, "2026-10": 33.24},
}
EST_RATE_MONTHS = {"2026-01","2026-02","2026-03","2026-04","2026-05"}

SNAP = dict(batch="batch_date", code="source_code", lead="day", op="op_count",
            sqo="sqo", cri="cris", prem="capped_cri_dollars", zip="indv_zipcode")
DEF_MAP = r"C:\Projects\google-ads-ops\reports\quotecom-life-diposition\output\zip_cluster_map_2026-10-20.csv"

# ------------------------------------------------------------------ style ---
NAVY="1F3864"
FILLS={"High":"DDEBF7","Medium":"E2EFDA","Low":"FCE4D6","Total":"D9D9D9"}
MONEY='$#,##0'; KPIF='0.00'; NUMF='#,##0'; PCT='0.0%'; CPCF='$0.00'
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
        c=ws.cell(r,i,h); c.font=F(9,True,"FFFFFF"); c.fill=fill("4472C4")
        c.alignment=Alignment(horizontal="center",wrap_text=True); c.border=THIN
    ws.row_dimensions[r].height=28
    return r+1

def num(s): return pd.to_numeric(s.astype(str).str.replace(r"[$,]","",regex=True),errors="coerce").fillna(0.0)
def pdate(s): return pd.to_datetime(s.astype(str),errors="coerce",format="mixed").dt.date
def rate(seg,m):
    r=SEG_OP_RATES[seg]
    return r.get(m, r[sorted(r)[-1]])

# ------------------------------------------------------------- clustering ---
def cluster_of_name(name):
    n=str(name)
    if re.search(r"CR\s*1", n, re.I): return "CR12"
    if re.search(r"CR\s*3", n, re.I): return "CR3"
    if re.search(r"CR\s*4", n, re.I): return "CR45"
    return None

def zfill5(z):
    s=str(z).strip().split(".")[0]
    return s.zfill(5) if s and s.lower() not in ("none","nan") else ""

def load_zip_weights(con):
    """{(zip, month): {cluster: click_share}} and {zip: {cluster: share}} (year)."""
    q="""SELECT z.zip, z.date, c.campaign_name, SUM(z.clicks) clicks
         FROM campaign_performance_zip_daily z JOIN campaigns c USING(campaign_id)
         WHERE z.account_id=29 GROUP BY 1,2,3"""
    g=pd.read_sql_query(q,con)
    if not len(g): return {}, {}
    g["zip"]=g["zip"].map(zfill5)
    g["date"]=pdate(g["date"]); g=g.dropna(subset=["date"])
    g["month"]=g["date"].map(lambda d: f"{d.year}-{d.month:02d}")
    g["cluster"]=g["campaign_name"].map(cluster_of_name)
    g=g.dropna(subset=["cluster"])
    zm=g.groupby(["zip","month","cluster"])["clicks"].sum()
    zy=g.groupby(["zip","cluster"])["clicks"].sum()
    def to_shares(s, keys):
        out={}
        for k, sub in s.groupby(level=list(range(keys))):
            tot=sub.sum()
            if tot>0:
                kk=k if isinstance(k,tuple) else (k,)
                out[kk]={idx[-1]: v/tot for idx,v in sub.items()}
        return out
    return to_shares(zm,2), to_shares(zy,1)

def load_zip_map(path):
    """CSV from zip_cluster_map.py: zip,cluster[,exclusive,valid_from,valid_to]. Unlisted zip => CR45."""
    m={}
    if not path or not os.path.exists(path):
        print(f"  !! zip map not found at {path} -- map fallback disabled (unlisted->CR45 still applies)")
        return m
    import csv as _csv
    with open(path,newline="",encoding="utf-8-sig") as f:
        for r in _csv.DictReader(f):
            z=zfill5(r.get("zip",""))
            cl=str(r.get("cluster","")).strip()
            if z and cl in ("CR3","CR45","CR12"):
                m.setdefault(z,[]).append((r.get("valid_from","0000"),r.get("valid_to","9999"),cl))
    return m

def map_cluster(zmap, z, lead_date):
    iso=str(lead_date)
    for vf,vt,cl in zmap.get(z,[]):
        if (not vf or iso>=str(vf)[:10]) and (not vt or iso<str(vt)[:10]): return cl
    return "CR45"   # join rule: unmatched zip => CR45

def attribute(D, zm, zy, zmap):
    """Fractional cluster weights per lead row. Returns exploded df with weight col 'w' and method."""
    recs=[]
    for t in D.itertuples(index=False):
        z=t.zp
        shares=None; method=None
        if z:
            shares=zm.get((z,t.month));  method="zip-month"
            if not shares: shares=zy.get((z,)); method="zip-year"
        if not shares:
            if z: shares={map_cluster(zmap,z,t.ld):1.0}; method="map"
            else: shares={"CR45":1.0}; method="no-zip"
        for cl,w in shares.items():
            recs.append((t.month,t.seg,cl,w,t.op*w,t.sqo*w,t.cri*w,t.prem*w,method))
    E=pd.DataFrame(recs,columns=["month","seg","cluster","lw","op","sqo","cri","prem","method"])
    return E

# ------------------------------------------------------------------ pulls ---
def load_final(con):
    """Each (lead-day, source_code) stream read from the LAST batch containing it."""
    q=(f'SELECT s."{SNAP["code"]}" code, s."{SNAP["lead"]}" ld, s."{SNAP["op"]}" op, '
       f's."{SNAP["sqo"]}" sqo, s."{SNAP["cri"]}" cri, s."{SNAP["prem"]}" prem, '
       f's."{SNAP["zip"]}" zp '
       f'FROM life_disposition_snapshot s '
       f'JOIN (SELECT "{SNAP["lead"]}" d, "{SNAP["code"]}" sc, MAX("{SNAP["batch"]}") mb '
       f'      FROM life_disposition_snapshot GROUP BY 1,2) m '
       f'ON s."{SNAP["lead"]}"=m.d AND s."{SNAP["code"]}"=m.sc AND s."{SNAP["batch"]}"=m.mb')
    df=pd.read_sql_query(q,con)
    df["ld"]=pdate(df["ld"]); df=df.dropna(subset=["ld"])
    df["code"]=df["code"].astype(str); df=df[df["code"].str.len()>=3]
    df["seg"]=df["code"].str[:2].map(SEG_OF_CODE)
    df=df[(df["seg"].notna())&(df["code"].str[2]=="M")]     # paid WebM
    for c in ("op","sqo","cri","prem"): df[c]=num(df[c])
    df["zp"]=df["zp"].map(zfill5)
    df["month"]=df["ld"].map(lambda d: f"{d.year}-{d.month:02d}")
    return df

def pull_platform_monthly(con):
    q="""SELECT d.date, c.campaign_name, SUM(d.cost_micros)/1e6 cost,
                SUM(d.clicks) clicks, SUM(d.impressions) impr, SUM(d.conversions) conv
         FROM campaign_performance_daily d JOIN campaigns c USING(campaign_id)
         WHERE d.account_id=29 AND d.device='ALL' GROUP BY 1,2"""
    g=pd.read_sql_query(q,con)
    g["date"]=pdate(g["date"]); g=g.dropna(subset=["date"])
    g["month"]=g["date"].map(lambda d: f"{d.year}-{d.month:02d}")
    g["cluster"]=g["campaign_name"].map(cluster_of_name)
    g=g.dropna(subset=["cluster"])
    return (g.groupby(["month","cluster"])
             .agg(cost=("cost","sum"),clicks=("clicks","sum"),
                  impr=("impr","sum"),conv=("conv","sum")).reset_index())

# ------------------------------------------------------------------ sheet ---
COLS=["Month","Seg","G spend","Impr","Clicks","CPC","Nat conv",
      "Leads","Ops","SQO","SQO/op","CRI","CRI/SQO","Prem","Prem/op","Pay (est)","KPI est"]

def write_cluster_sheet(wb, name, D, P, months):
    ws=wb.create_sheet(name)
    r=title(ws, f"{name}: monthly platform vs disposition, by income segment",
        [f"Google side = campaigns matching '{name}' in campaign_performance_daily (acct 29). "
         "Dispo side = paid WebM leads attributed to this campaign via trk_utm_campaign.",
         "Platform metrics on Total rows only (Google has no segment grain). "
         "KPI est = Prem / Pay(est); Pay(est) = Ops x per-op rate (Jan-May rates ESTIMATED -- see Coverage).",
         f"Break-even KPI ~ {BREAK_EVEN}. Dispo premium is net of removed sales (feed basis); "
         "young months still maturing."],
        len(COLS))
    r=hdr(ws,r,COLS)
    first_data=r
    kpi_rows={s:[] for s in SEGS}; month_rows=[]
    for m in months:
        p=P[(P["month"]==m)].agg({"cost":"sum","clicks":"sum","impr":"sum","conv":"sum"}) \
            if P is not None else None
        dm=D[D["month"]==m]
        tot=dm.agg({"op":"sum","sqo":"sum","cri":"sum","prem":"sum"})
        leads_tot=float(dm["lw"].sum())
        # ---- Total row
        put(ws,r,1,m,b=True,fl=FILLS["Total"]); put(ws,r,2,"Total",b=True,fl=FILLS["Total"])
        if p is not None and p["cost"]>0:
            put(ws,r,3,round(float(p["cost"]),2),MONEY); put(ws,r,4,int(p["impr"]),NUMF)
            put(ws,r,5,int(p["clicks"]),NUMF)
            put(ws,r,6,f"=IFERROR(C{r}/E{r},\"\")",CPCF)
            put(ws,r,7,round(float(p["conv"]),1),NUMF)
        put(ws,r,8,round(leads_tot,1),NUMF); put(ws,r,9,round(float(tot["op"]),1),NUMF); put(ws,r,10,round(float(tot["sqo"]),1),NUMF)
        put(ws,r,11,f"=IFERROR(J{r}/I{r},\"\")",PCT)
        put(ws,r,12,round(float(tot["cri"]),1),NUMF)
        put(ws,r,13,f"=IFERROR(L{r}/J{r},\"\")",PCT)
        put(ws,r,14,round(float(tot["prem"]),2),MONEY)
        put(ws,r,15,f"=IFERROR(N{r}/I{r},\"\")",MONEY)
        seg_pay_rows=[]
        tot_row=r; month_rows.append((m,r)); r+=1
        # ---- Segment rows
        for s in SEGS:
            d=dm[dm["seg"]==s]
            put(ws,r,1,m); put(ws,r,2,s,fl=FILLS[s])
            put(ws,r,8,round(float(d["lw"].sum()),1),NUMF); put(ws,r,9,round(float(d["op"].sum()),1),NUMF)
            put(ws,r,10,round(float(d["sqo"].sum()),1),NUMF)
            put(ws,r,11,f"=IFERROR(J{r}/I{r},\"\")",PCT)
            put(ws,r,12,round(float(d["cri"].sum()),1),NUMF)
            put(ws,r,13,f"=IFERROR(L{r}/J{r},\"\")",PCT)
            put(ws,r,14,round(float(d["prem"].sum()),2),MONEY)
            put(ws,r,15,f"=IFERROR(N{r}/I{r},\"\")",MONEY)
            put(ws,r,16,f"=I{r}*{rate(s,m):.2f}",MONEY)
            put(ws,r,17,f"=IFERROR(N{r}/P{r},\"\")",KPIF)
            kpi_rows[s].append(r); seg_pay_rows.append(r)
            r+=1
        # Total pay/KPI from the segment rows
        put(ws,tot_row,16,"="+"+".join(f"P{x}" for x in seg_pay_rows),MONEY)
        put(ws,tot_row,17,f"=IFERROR(N{tot_row}/P{tot_row},\"\")",KPIF,b=True)
    ws.freeze_panes=ws.cell(first_data,3)
    widths=[9,8,11,10,9,8,9,8,8,8,8,7,9,12,9,11,8]
    for i,w in enumerate(widths,1): ws.column_dimensions[get_column_letter(i)].width=w
    # ---- chart: KPI by segment across months (uses hidden helper block)
    hc=len(COLS)+2
    ws.cell(first_data-1,hc,"m").font=F(8);
    for j,s in enumerate(SEGS):
        ws.cell(first_data-1,hc+1+j,s).font=F(8)
    for i,(m,_) in enumerate(month_rows):
        ws.cell(first_data+i,hc,m).font=F(8)
        for j,s in enumerate(SEGS):
            ws.cell(first_data+i,hc+1+j,f"=Q{kpi_rows[s][i]}").font=F(8)
    for col in range(hc,hc+4): ws.column_dimensions[get_column_letter(col)].hidden=True
    ch=LineChart(); ch.title=f"{name}: KPI est by segment"; ch.height=8; ch.width=18
    ch.y_axis.title="Policy KPI"; ch.style=2
    n=len(month_rows)
    for j,s in enumerate(SEGS):
        ref=Reference(ws,min_col=hc+1+j,min_row=first_data-1,max_row=first_data-1+n)
        ser=Series(ref,title_from_data=True); ch.series.append(ser)
    ch.set_categories(Reference(ws,min_col=hc,min_row=first_data,max_row=first_data-1+n))
    ws.add_chart(ch,f"A{r+2}")
    return ws

# ------------------------------------------------------------------- main ---
def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--db",default=DEF_DB); ap.add_argument("--out-dir",default=None)
    ap.add_argument("--zip-map",default=DEF_MAP)
    a=ap.parse_args()
    con=sqlite3.connect(f"file:{a.db}?mode=ro",uri=True)
    b_last=con.execute("SELECT MAX(batch_date) FROM life_disposition_snapshot").fetchone()[0]
    if not b_last: sys.exit("no batches in life_disposition_snapshot")

    D=load_final(con)
    zm,zy=load_zip_weights(con)
    zmap=load_zip_map(a.zip_map)
    E=attribute(D, zm, zy, zmap)
    P=pull_platform_monthly(con)
    months=sorted(set(E["month"]))
    print(f"[dispo] rows {len(D)} | months {months[0]}..{months[-1]}")
    share=E.groupby("cluster")["op"].sum(); tot=share.sum()
    for k,v in share.items(): print(f"  {k:9} ops {v:7.1f}  ({v/tot:.1%})")
    msh=E.groupby("method")["op"].sum()
    for k,v in msh.items(): print(f"  method {k:10} {v/tot:.1%}")

    wb=Workbook(); wb.remove(wb.active)
    for cl in CLUSTERS:
        write_cluster_sheet(wb, cl, E[E["cluster"]==cl], P[P["cluster"]==cl], months)

    # ---- Coverage sheet
    ws=wb.create_sheet("Coverage")
    r=title(ws,"Attribution coverage & notes",
        ["Leads -> campaigns by ZIP x MONTH click share (campaign_performance_zip_daily); fractional where "
         "campaigns shared a zip-month. Fallbacks: zip-year share, then zip-cluster map (CR3 list / complement=CR45).",
         "'zip-month' share below is the high-confidence portion; 'map'/'no-zip' rows carry no CR12 signal "
         "(assigned CR3/CR45 only), so if material, CR12 is slightly understated.",
         "KPI rates: 2026-06 onward observed from Looker Spend/Ops; 2026-01..05 ESTIMATED "
         f"(June rates x {PRE_SCALE:.3f}, the blended pre/post step ratio). Replace SEG_OP_RATES "
         "with observed monthly Spend/Ops per segment from a Looker monthly-by-advert export for exact pre-June KPI.",
         "Dispo basis: paid WebM codes, each (day, source code) stream read from the last batch containing it; "
         "premium is net of removed sales. Platform basis: campaign_performance_daily, device='ALL', trailing ~3 days provisional."],
        8)
    r=hdr(ws,r,["Month","CR12 ops","CR3 ops","CR45 ops","% zip-month","% zip-year","% map/no-zip","Batch"])
    piv=E.pivot_table(index="month",columns="cluster",values="op",aggfunc="sum").fillna(0)
    mpiv=E.pivot_table(index="month",columns="method",values="op",aggfunc="sum").fillna(0)
    for m in months:
        row=piv.loc[m] if m in piv.index else {}
        mr=mpiv.loc[m] if m in mpiv.index else {}
        c12=float(row.get("CR12",0)); c3=float(row.get("CR3",0)); c45=float(row.get("CR45",0))
        t=c12+c3+c45
        zmv=float(mr.get("zip-month",0)); zyv=float(mr.get("zip-year",0))
        other=float(mr.get("map",0))+float(mr.get("no-zip",0))
        put(ws,r,1,m); put(ws,r,2,round(c12,1),NUMF); put(ws,r,3,round(c3,1),NUMF)
        put(ws,r,4,round(c45,1),NUMF)
        put(ws,r,5,(zmv/t if t else 0),PCT); put(ws,r,6,(zyv/t if t else 0),PCT)
        put(ws,r,7,(other/t if t else 0),PCT)
        if m==months[0]: put(ws,r,8,str(b_last))
        r+=1
    for i,w in enumerate([9,10,10,10,12,11,13,12],1): ws.column_dimensions[get_column_letter(i)].width=w

    out_dir=a.out_dir or os.path.join(os.path.dirname(os.path.abspath(a.db)),"..","output")
    os.makedirs(out_dir,exist_ok=True)
    out=os.path.join(out_dir,f"termlife_campaign_monthly_{str(b_last)[:10].replace('-','')}.xlsx")
    wb.save(out)
    print(f"[done] {out}")

if __name__=="__main__":
    main()
