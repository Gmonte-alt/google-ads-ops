"""
build_performance_review_workbook.py — assembles the three-bucket leadership
deliverable for the Google Home April->now review. Consumes artifacts the
analyst already produces (no new DB query):
  - data-tables workbook  (campaign_performance_data_google_*.xlsx) -> weekly GP
  - geo_tier_weekly_rpl.csv                                         -> value-tier trend
  - geo_state_device_weekly_matrix.csv                              -> state movers
  - Matched_locations_report*.csv                                   -> state->campaign
  - keyword_theme_cut.csv (optional)                                -> theme GP (issue campaigns)
  - geo_rpl_effect_summary.csv / geo_rpl_effect_weekly.csv          -> state RPL redistribution
  Mechanism_Model covers ALL campaigns (per-day, GP drainers on top) off the data-tables Daily sheets.

Sheets: Exec_Summary | B1_Phase_GP | B2_Event | RPL_Redistribution |
        Mechanism_Model | B3_Geo_Tiers | B3_Issue_Campaigns | Mechanism_by_Theme |
        B3_Keyword_Themes (+ Raw_* tabs)

    python build_performance_review_workbook.py --datatables campaign_performance_data_google_2026-06-09.xlsx
"""
import argparse, csv, datetime as dt
from collections import defaultdict
from pathlib import Path
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.chart import BarChart, LineChart, Reference
from openpyxl.drawing.image import Image as XLImage
from openpyxl.utils import get_column_letter

HFILL="305496"; AMBER="FFF2CC"; GREY="E7E6E6"; BAD="FFC7CE"; GOOD="C6EFCE"; WARN="FCE4D6"
THIN=Side(border_style="thin",color="BFBFBF")
def F(**k): return Font(name="Arial",**k)
def fill(c): return PatternFill("solid",fgColor=c)
title=F(bold=True,size=14,color="305496"); h2=F(bold=True,size=12,color="305496")
hf=F(bold=True,color="FFFFFF"); bf=F(); note=F(italic=True,color="595959",size=9); lbl=F(bold=True)
M='"$"#,##0'; PCT='0.0"%"'; F1="#,##0.0"
# Keyword cut windows (from analyze_keyword_themes.py): A = 2026-04-01..04-30 (30d),
# B = 2026-05-18..06-07 (21d). Level metrics are normalized /day so the unequal
# windows compare apples-to-apples and reconcile with the per-day RPL sheet.
KW_DAYS_A, KW_DAYS_B = 30, 21
def hdr(ws,r,cols):
    for i,c in enumerate(cols,1):
        x=ws.cell(row=r,column=i,value=c); x.fill=fill(HFILL); x.font=hf
        x.alignment=Alignment(horizontal="center",wrap_text=True)
def box(ws,r,c): ws.cell(row=r,column=c).border=Border(top=THIN,bottom=THIN,left=THIN,right=THIN)
def mon(d): return d - dt.timedelta(days=d.weekday())

def require(path, label):
    p = Path(path)
    if not p.exists():
        raise SystemExit(f"  MISSING {label}: {path}\n  Pass the correct path via its --flag.")
    return p

def args():
    p=argparse.ArgumentParser()
    REPO=r"C:\Projects\google-ads-ops"
    # NOTE: --datatables is date-stamped; update the date when the data-tables
    # workbook is regenerated. --locations-csv: confirm where the matched-
    # locations export lives (or save it to the repo root with this name).
    p.add_argument("--datatables",default=REPO+r"\analysis\output\campaign_performance_data_google_2026-06-09.xlsx")
    p.add_argument("--tier-csv",default=REPO+r"\geo_tier_weekly_rpl.csv")
    p.add_argument("--tier-vol-csv",default=REPO+r"\geo_tier_weekly_vol.csv")
    p.add_argument("--tier-clicks-csv",default=REPO+r"\geo_tier_weekly_clicks.csv")
    p.add_argument("--matrix-csv",default=REPO+r"\geo_state_device_weekly_matrix.csv")
    p.add_argument("--locations-csv",default=REPO+r"\analysis\input\Matched_locations_report__4_.csv")
    p.add_argument("--keyword-csv",default=REPO+r"\keyword_theme_cut.csv")
    p.add_argument("--rpl-summary-csv",default=REPO+r"\geo_rpl_effect_summary.csv")
    p.add_argument("--rpl-weekly-csv",default=REPO+r"\geo_rpl_effect_weekly.csv")
    p.add_argument("--out",default=REPO+r"\Google_Home_AprToNow_Performance_Review.xlsx")
    return p.parse_args()

def weekly_gp(path):
    wb=load_workbook(path,read_only=True); ws=wb["Daily_Account"]
    rows=list(ws.iter_rows(values_only=True)); wb.close()
    wk=defaultdict(lambda:{"rev":0.0,"cost":0.0})
    for r in rows[4:]:
        if not isinstance(r[0],dt.datetime): continue
        w=mon(r[0].date()); wk[w]["cost"]+=float(r[4] or 0); wk[w]["rev"]+=float(r[6] or 0)
    return sorted((w,v["rev"]-v["cost"]) for w,v in wk.items())

CODE2NAME={'CA':'California','FL':'Florida','TX':'Texas','T1':'Tier One',
           'T2':'Tier Two','T3':'Tier Three','T4':'Tier Four'}
def campaign_daily(path):
    """Read per-campaign Daily_*-NB sheets from the data-tables workbook ->
    consolidated daily rows + per-campaign A/B window sums (same primary-conv
    basis as B1). Returns (rows[(campaign,date,clicks,cost,conv,rev)], camp{name:{A,B}})."""
    wb=load_workbook(path,read_only=True,data_only=True)
    A0,A1=dt.date(2026,4,1),dt.date(2026,4,30); B0,B1=dt.date(2026,5,18),dt.date(2026,6,7)
    rows=[]; camp=defaultdict(lambda:{'A':{'ck':0,'cs':0,'cv':0,'rv':0},'B':{'ck':0,'cs':0,'cv':0,'rv':0}})
    for sn in wb.sheetnames:
        if not sn.startswith("Daily_") or sn=="Daily_Account": continue
        name=CODE2NAME.get(sn.replace("Daily_","").replace("-NB",""),sn.replace("Daily_",""))
        for r in wb[sn].iter_rows(min_row=5,values_only=True):
            d=r[0]
            if isinstance(d,dt.datetime): d=d.date()
            if not isinstance(d,dt.date): continue
            ck,cs,cv,rv=float(r[3] or 0),float(r[4] or 0),float(r[5] or 0),float(r[6] or 0)
            rows.append((name,d,ck,cs,cv,rv))
            w='A' if A0<=d<=A1 else ('B' if B0<=d<=B1 else None)
            if w:
                c=camp[name][w]; c['ck']+=ck; c['cs']+=cs; c['cv']+=cv; c['rv']+=rv
    wb.close()
    return rows, camp

def state_movers(path):
    rows=[r for r in csv.DictReader(open(path)) if r["device_class"]=="TOTAL"]
    weeks=sorted({r["week_start"] for r in rows}); half=len(weeks)//2
    T=defaultdict(dict)
    for r in rows: T[r["bucket"]][r["week_start"]]={"rev":float(r["revenue"]),"conv":float(r["conversions"])}
    def w(s,wks): 
        rv=sum(T[s][x]["rev"] for x in wks if x in T[s]); cv=sum(T[s][x]["conv"] for x in wks if x in T[s]); return rv,cv
    out={}
    for s in T:
        if s=="All Other": continue
        rA,cA=w(s,weeks[:half]); rB,cB=w(s,weeks[half:])
        out[s]=dict(rplA=rA/cA if cA else 0, rplB=rB/cB if cB else 0, rev=rA+rB,
                    d=((rB/cB)/(rA/cA)-1)*100 if (cA and cB and rA) else 0)
    return out

def raw_tab(wb, name, csv_path, date_cols=(), num_cols=()):
    """Dump a CSV verbatim into a sheet so the workbook carries its own raw data.
    date_cols -> real Excel dates (so SUMIFS date criteria work); num_cols -> floats."""
    ws=wb.create_sheet(name); rows=list(csv.reader(open(csv_path))); header=rows[0]
    hdr(ws,1,header)
    for ri,row in enumerate(rows[1:],2):
        for ci,v in enumerate(row,1):
            col=header[ci-1]
            if col in date_cols and v:
                cell=ws.cell(row=ri,column=ci,value=dt.datetime.strptime(v,"%Y-%m-%d")); cell.number_format="yyyy-mm-dd"
            elif col in num_cols and v not in ("",None):
                ws.cell(row=ri,column=ci,value=float(v))
            else:
                ws.cell(row=ri,column=ci,value=v)
    ws.freeze_panes="A2"
    return ws


def make_mechanism_png(path, A, B, days_a=KW_DAYS_A, days_b=KW_DAYS_B, label="Tier One"):
    """Render the RPL->GP feedback-loop flowchart from a campaign's live numbers
    (A,B dicts: v=revenue, c=cost, cv=conv, ck=clicks per window). Level metrics
    are per-day (windows compare apples-to-apples); ratios are window-invariant."""
    try:
        import matplotlib; matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib.patches import FancyBboxPatch, FancyArrowPatch
    except Exception as ex:
        print(f"  (mechanism diagram skipped - matplotlib unavailable: {ex})"); return None
    if not (A.get("cv") and B.get("cv") and A.get("ck") and B.get("ck")): return None
    pc=lambda a,b:(b/a-1)*100 if a else 0
    rA,rB=A["v"]/A["cv"],B["v"]/B["cv"]; vA,vB=A["v"]/A["ck"],B["v"]/B["ck"]   # ratios (window-invariant)
    oA,oB=A["v"]/A["c"],B["v"]/B["c"]
    ckA,ckB=A["ck"]/days_a,B["ck"]/days_b                                      # per-day levels
    gA,gB=(A["v"]-A["c"])/days_a,(B["v"]-B["c"])/days_b
    rvA,rvB=A["v"]/days_a,B["v"]/days_b; csA,csB=A["c"]/days_a,B["c"]/days_b
    CORAL=("#FAECE7","#993C1D"); GREY=("#F1EFE8","#5F5E5A"); RED=("#FCEBEB","#A32D2D")
    steps=[("Lead value falls (RPL)",f"RPL {rA:.0f} -> {rB:.0f}   ({pc(rA,rB):+.0f}%)",CORAL),
           ("Value per click drops",f"VPC ${vA:.2f} -> ${vB:.2f}   ({pc(vA,vB):+.0f}%)",CORAL),
           ("Bids lose position",f"clicks/day {ckA:.0f} -> {ckB:.0f}   ({pc(ckA,ckB):+.0f}%)",GREY),
           ("CPC held, efficiency erodes",f"ROAS {oA:.2f} -> {oB:.2f}",GREY),
           ("GP collapses",f"GP/day ${gA:,.0f} -> ${gB:,.0f}   ({pc(gA,gB):+.0f}%)",RED)]
    fig,ax=plt.subplots(figsize=(4.9,6.6)); ax.set_xlim(0,10); ax.set_ylim(0,15); ax.axis("off")
    ax.text(5,14.4,f"Worked example: {label} (per day)",ha="center",fontsize=11,fontweight="bold",family="Arial")
    bh,gap,top=2.0,0.7,13.4
    for i,(t,s,(fc,ec)) in enumerate(steps):
        y=top-i*(bh+gap)
        ax.add_patch(FancyBboxPatch((1.2,y-bh),7.6,bh,boxstyle="round,pad=0.08,rounding_size=0.18",
                     facecolor=fc,edgecolor=ec,linewidth=1.2))
        ax.text(5,y-0.62,t,ha="center",fontsize=10.5,fontweight="bold",color=ec,family="Arial")
        ax.text(5,y-1.34,s,ha="center",fontsize=9.5,color=ec,family="Arial")
        if i<len(steps)-1:
            ax.add_patch(FancyArrowPatch((5,y-bh),(5,y-bh-gap+0.06),arrowstyle="-|>",
                         mutation_scale=14,color="#5F5E5A",linewidth=1.2))
    ax.text(5,top-5*(bh+gap)-0.1,f"Revenue ({pc(rvA,rvB):+.0f}%) falls faster than cost "
            f"({pc(csA,csB):+.0f}%)\n- RPL decay is the wedge",ha="center",fontsize=9,
            style="italic",color="#444441",family="Arial")
    fig.tight_layout(pad=0.3); fig.savefig(path,dpi=150,bbox_inches="tight"); plt.close(fig)
    return path


def state_campaign(path):
    import pandas as pd
    df=pd.read_csv(path,skiprows=2); df=df[df["Campaign"].notna()&df["Matched location"].notna()]
    df["Clicks"]=pd.to_numeric(df["Clicks"].astype(str).str.replace(r'[,"]','',regex=True),errors="coerce")
    df["state"]=df["Matched location"].astype(str).str.replace(", United States","",regex=False).str.strip()
    def cmp(c):
        c=str(c)
        for t in ["TIER ONE","TIER TWO","TIER THREE","TIER FOUR","CALIFORNIA","TEXAS","FLORIDA"]:
            if t in c: return t.title()
        return c[:18]
    df["camp"]=df["Campaign"].apply(cmp)
    added=df[df["Added/Excluded"]=="Added"].groupby(["state","camp"]).agg(clk=("Clicks","sum")).reset_index()
    assign={}
    for st in added["state"].unique():
        assign[st]=added[added.state==st].sort_values("clk",ascending=False).iloc[0]["camp"]
    return assign

def main():
    a=args(); wb=Workbook()
    require(a.datatables, "data-tables workbook (--datatables)")
    require(a.tier_csv, "geo tier CSV (--tier-csv)")
    require(a.matrix_csv, "geo matrix CSV (--matrix-csv)")
    require(a.locations_csv, "matched-locations CSV (--locations-csv)")
    require(a.rpl_summary_csv, "RPL-effect summary CSV (--rpl-summary-csv)")
    assign=state_campaign(a.locations_csv)   # state -> actual current campaign (reused by 2 sheets)
    # keyword CSV is optional — handled where the sheet is written

    # ── Exec_Summary ──
    ws=wb.active; ws.title="Exec_Summary"
    ws["A1"]="Google Home Insurance — April → Now Performance Review"; ws["A1"].font=title
    ws["A2"]=("Manager's question: 'April was strong, we've regressed — what shifted?' "
              "Short answer: the premise needs one correction, then the change splits into three causes.")
    ws["A2"].font=note; ws["A2"].alignment=Alignment(wrap_text=True)
    blocks=[
        ("Premise reframe","April wasn't a stable-strong baseline — it was VOLATILE ($10.7K & $8.6K weeks next to $5.0K & $4.1K). "
         "May stabilized ~9% lower with the swings gone. Only the most recent week actually broke. So it's 'steady through May, one event' — not a slide."),
        ("Bucket 1 — CHOICE (not a problem)","We raised tROAS (Apr 21, May 6; partial rollback May 14, still net above April) to trade volume for efficiency. "
         "It worked where intended — Texas +20% RPL, Tier Three profit up. This is deliberate, not regression."),
        ("Bucket 2 — EVENT (recoverable)","The last-week break is the bidder over-correcting. A brief, real lead-acceptance dip Jun 2-3 (zip→lead -12%) hit bidding "
         "running lean at the higher tROAS; it pulled back mid-week, cost stayed up while revenue softened, margin compressed (ROAS 1.19→1.08). "
         "RPL/volume barely moved — a margin event. Releasing tROAS recovers it."),
        ("Bucket 3 — STRUCTURAL (the real regression)","Per-lead value is eroding in our highest-value states (Northeast corridor + California) while mid-value Southern states grow. "
         "At the state level ~$535/day of GP is destroyed by value decay (NJ, CA, IL, CT, NY), almost exactly offset by ~$526/day gained in growth states (TX, GA, OH) — net ~zero, so the account blend HIDES it. "
         "Concentrated in Tier One (owns CT/NJ/NY, the 3 worst decliners) and California. Root cause: stale tiering — high-value states (NJ, CT) "
         "stuck in a campaign built for low-value workhorses, bid under one wrong tROAS/model. See RPL_Redistribution + Mechanism_Model."),
    ]
    r=4
    for k,v in blocks:
        ws.cell(row=r,column=1,value=k).font=F(bold=True,color="305496")
        c=ws.cell(row=r,column=2,value=v); c.font=bf; c.alignment=Alignment(wrap_text=True,vertical="top")
        ws.row_dimensions[r].height=58; r+=1
    ws.column_dimensions["A"].width=26; ws.column_dimensions["B"].width=104

    # ── B1_Phase_GP ──
    ws=wb.create_sheet("B1_Phase_GP")
    ws["A1"]="Bucket 1 — Weekly account GP (the three phases)"; ws["A1"].font=title
    ws["A2"]=("April volatile, May stable ~9% lower, last week the break. Last bar amber = soft from T+1 offline-upload lag.")
    ws["A2"].font=note; ws["A2"].alignment=Alignment(wrap_text=True)
    gp=weekly_gp(a.datatables)
    hdr(ws,4,["week_start","weekly GP","phase"])
    def phase(w):
        if w<dt.date(2026,4,28): return "April — volatile"
        if w<dt.date(2026,6,1): return "May — stable"
        return "Jun — break"
    r=5
    for w,g in gp:
        ws.cell(row=r,column=1,value=str(w)); ws.cell(row=r,column=2,value=round(g)).number_format=M
        ws.cell(row=r,column=3,value=phase(w))
        for c in (1,2,3):
            box(ws,r,c)
            if w>=dt.date(2026,6,1): ws.cell(row=r,column=c).fill=fill(AMBER)
        r+=1
    ch=BarChart(); ch.title="Weekly account GP"; ch.type="col"; ch.height=8; ch.width=20; ch.legend=None
    ch.add_data(Reference(ws,min_col=2,min_row=4,max_row=r-1)); ch.set_categories(Reference(ws,min_col=1,min_row=5,max_row=r-1))
    ws.add_chart(ch,"E4")
    ws.column_dimensions["A"].width=12; ws.column_dimensions["B"].width=12; ws.column_dimensions["C"].width=18

    # ── B2_Event ──
    ws=wb.create_sheet("B2_Event")
    ws["A1"]="Bucket 2 — The Jun 2-3 event & bidder over-correction"; ws["A1"].font=title
    pts=["Jun 2-3: real, brief lead-ACCEPTANCE dip (not a funnel break — Looker zip→lead held in-band).",
         "Weekday-matched zip→lead: Tue -12%, Wed -6%. Volume -4%, RPL -3% — both small.",
         "Bidder, lean at elevated tROAS, pulled back from Jun 5 (impressions -40-60%, CPC fell).",
         "Cost stayed up while revenue softened -> ROAS 1.19→1.08, GP cratered. A MARGIN event, not RPL/volume.",
         "Confirmed via two Looker pulls: Jun 3 leads stable 130→131; the conversions settled, the bidder over-reacted to incomplete same-day data.",
         "FIX: temporary tROAS reduction to re-accelerate (data exclusion not warranted — data was real, not corrupted)."]
    r=3
    for p in pts:
        ws.cell(row=r,column=1,value="•"); c=ws.cell(row=r,column=2,value=p); c.font=bf; c.alignment=Alignment(wrap_text=True); r+=1
    ws.column_dimensions["A"].width=3; ws.column_dimensions["B"].width=110

    # ── B3_Geo_Tiers ──
    ws=wb.create_sheet("B3_Geo_Tiers")
    ws["A1"]="Bucket 3a — RPL by value-tier (compression toward the middle)"; ws["A1"].font=title
    ws["A2"]="High tier loses premium, Mid tier rises, they cross. Native-conv basis (RPL reconciles ~2-3%)."
    ws["A2"].font=note
    trows=list(csv.reader(open(a.tier_csv)))
    weeks=trows[0][1:]
    hdr(ws,4,["tier"]+ [w[5:] for w in weeks])
    r=5
    for tr in trows[1:]:
        ws.cell(row=r,column=1,value=tr[0])
        for i,v in enumerate(tr[1:],2):
            cell=ws.cell(row=r,column=i,value=float(v) if v else None); cell.number_format="0"
        for c in range(1,len(weeks)+2): box(ws,r,c)
        r+=1
    lc=LineChart(); lc.title="RPL by value-tier"; lc.height=9; lc.width=22
    lc.add_data(Reference(ws,min_col=2,max_col=len(weeks)+1,min_row=4,max_row=r-1),titles_from_data=True,from_rows=True)
    lc.set_categories(Reference(ws,min_col=2,max_col=len(weeks)+1,min_row=4,max_row=4))
    ws.add_chart(lc,f"A{r+2}")
    ws.column_dimensions["A"].width=16
    # conversions/day by the SAME value-tiers × week (volume alongside value)
    if Path(a.tier_vol_csv).exists():
        vrows=list(csv.reader(open(a.tier_vol_csv))); cols=vrows[0]; nc=len(cols)
        vs=r+22
        ws.cell(row=vs,column=1,value="Conversions per day by the same value-tier × week (volume alongside value)").font=h2
        hdr(ws,vs+1,["tier"]+[w[5:] for w in cols[1:]])
        rr=vs+2
        for tr in vrows[1:]:
            ws.cell(row=rr,column=1,value=tr[0])
            for i,v in enumerate(tr[1:],2):
                cell=ws.cell(row=rr,column=i,value=float(v) if v else None); cell.number_format="0.0"
            for c in range(1,nc+1): box(ws,rr,c)
            rr+=1
        lv=LineChart(); lv.title="Conversions/day by value-tier"; lv.height=9; lv.width=22
        lv.add_data(Reference(ws,min_col=1,max_col=nc,min_row=vs+2,max_row=rr-1),titles_from_data=True,from_rows=True)
        lv.set_categories(Reference(ws,min_col=2,max_col=nc,min_row=vs+1,max_row=vs+1))
        ws.add_chart(lv,f"A{rr+2}")
        conv_top=vs+2
        # clicks/day + estimated CvR (conv ÷ clicks) — decomposes the conv/day move
        if Path(a.tier_clicks_csv).exists():
            crows=list(csv.reader(open(a.tier_clicks_csv))); ntiers=len(crows)-1
            cstart=rr+20
            ws.cell(row=cstart,column=1,value="Clicks per day by the same value-tier × week").font=h2
            hdr(ws,cstart+1,["tier"]+[w[5:] for w in crows[0][1:]])
            clk_top=cstart+2; cr2=clk_top
            for tr in crows[1:]:
                ws.cell(row=cr2,column=1,value=tr[0])
                for i,v in enumerate(tr[1:],2):
                    cell=ws.cell(row=cr2,column=i,value=float(v) if v else None); cell.number_format="#,##0"
                for c in range(1,nc+1): box(ws,cr2,c)
                cr2+=1
            lk=LineChart(); lk.title="Clicks/day by value-tier"; lk.height=9; lk.width=22
            lk.add_data(Reference(ws,min_col=1,max_col=nc,min_row=clk_top,max_row=cr2-1),titles_from_data=True,from_rows=True)
            lk.set_categories(Reference(ws,min_col=2,max_col=nc,min_row=cstart+1,max_row=cstart+1))
            ws.add_chart(lk,f"A{cr2+2}")
            cvs=cr2+22
            ws.cell(row=cvs,column=1,value="Estimated CvR by value-tier × week (conv ÷ clicks — splits the conv/day move into clicks vs CvR)").font=h2
            hdr(ws,cvs+1,["tier"]+[w[5:] for w in crows[0][1:]])
            cvr_top=cvs+2; cvr=cvr_top
            for ti in range(ntiers):
                ws.cell(row=cvr,column=1,value=crows[1+ti][0])
                for ci in range(2,nc+1):
                    L=get_column_letter(ci)
                    ws.cell(row=cvr,column=ci,value=f'=IFERROR({L}{conv_top+ti}/{L}{clk_top+ti},"")').number_format="0.0%"
                for c in range(1,nc+1): box(ws,cvr,c)
                cvr+=1
            lcv=LineChart(); lcv.title="Estimated CvR by value-tier"; lcv.height=9; lcv.width=22
            lcv.add_data(Reference(ws,min_col=1,max_col=nc,min_row=cvr_top,max_row=cvr-1),titles_from_data=True,from_rows=True)
            lcv.set_categories(Reference(ws,min_col=2,max_col=nc,min_row=cvs+1,max_row=cvs+1))
            ws.add_chart(lcv,f"A{cvr+2}")

    # ── B3_Issue_Campaigns ──
    ws=wb.create_sheet("B3_Issue_Campaigns")
    ws["A1"]="Bucket 3b — Issue campaigns & stale tiering"; ws["A1"].font=title
    ws["A2"]=("State→campaign (actual targeting) × RPL trajectory. Decay states cluster in Tier One (CT/NJ/NY) & spill into Tier Four (IL/MA/MI). "
              "High-value NJ/CT/IL/MA sit in campaigns built for low-value states — the stale-tier mismatch.")
    ws["A2"].font=note; ws["A2"].alignment=Alignment(wrap_text=True)
    mov=state_movers(a.matrix_csv)  # assign hoisted to top of main
    hdr(ws,4,["campaign","state","RPL Apr","RPL recent","RPL Δ%","12w rev","flag"])
    order=["Tier One","California","Tier Four","Tier Three","Tier Two","Texas","Florida"]
    bycamp=defaultdict(list)
    for st,cmp in assign.items(): bycamp[cmp].append(st)
    r=5
    for cmp in order:
        for st in sorted(bycamp.get(cmp,[]),key=lambda s:mov.get(s,{}).get("d",0)):
            m=mov.get(st)
            if not m: continue
            flag="DECAY" if m["d"]<=-10 else ("GROW" if m["d"]>=10 else "")
            col=BAD if flag=="DECAY" else (GOOD if flag=="GROW" else None)
            vals=[cmp,st,round(m["rplA"],1),round(m["rplB"],1),round(m["d"],1),round(m["rev"]),flag]
            for i,v in enumerate(vals,1):
                cell=ws.cell(row=r,column=i,value=v); box(ws,r,i)
                if i in (3,4): cell.number_format="0.0"
                if i==5: cell.number_format=PCT
                if i==6: cell.number_format=M
                if col and i in (1,2,7): cell.fill=fill(col)
            r+=1
    for c,w in zip("ABCDEFG",[12,16,9,11,8,10,8]): ws.column_dimensions[c].width=w

    # ── B3_Keyword_Themes (FORMULA-DRIVEN off Raw_Keyword_Themes; 3 problem campaigns + All other) ──
    ws=wb.create_sheet("B3_Keyword_Themes")
    ws["A1"]="Bucket 3c — Keyword themes (geico / farmers / all other carriers / generic)"; ws["A1"].font=title
    kp=Path(a.keyword_csv)
    if kp.exists():
        raw_tab(wb,"Raw_Keyword_Themes",str(kp),
                num_cols=("revenue","cost","conversions","clicks","GP","RPL","CPL"))
        # add a grouping key: problem campaigns keep their name; everything else -> "All other campaigns"
        rkt=wb["Raw_Keyword_Themes"]; PROBLEM=["Tier One","Tier Four","California"]
        gcol=rkt.max_column+1; gL=get_column_letter(gcol)
        gc=rkt.cell(row=1,column=gcol,value="campaign_group"); gc.fill=fill(HFILL); gc.font=hf
        for rr in range(2,rkt.max_row+1):
            cn=rkt.cell(row=rr,column=1).value
            rkt.cell(row=rr,column=gcol,value=cn if cn in PROBLEM else "All other campaigns")
        rkt.column_dimensions[gL].width=18
        ws["A2"]=("GP/day by theme — three problem campaigns + All other campaigns baseline. Every cell is a live SUMIFS off "
                  "Raw_Keyword_Themes (April ÷30, recent ÷21; RPL window-invariant). Summed across ad groups; ad-group detail in Raw_Keyword_Themes.")
        ws["A2"].font=note; ws["A2"].alignment=Alignment(wrap_text=True)
        RKT="Raw_Keyword_Themes"; THEME_ORDER=["geico","farmers","all_other_carriers","generic"]
        groups=["Tier One","Tier Four","California","All other campaigns"]
        def ks(col,per,r):  # SUMIFS on group(col M) + theme($B) + period
            return (f'SUMIFS({RKT}!${col}:${col},{RKT}!${gL}:${gL},$A{r},'
                    f'{RKT}!$D:$D,$B{r},{RKT}!$E:$E,"{per}")')
        hdr(ws,4,["group","theme","GP/day Apr","GP/day recent","GP/day Δ","RPL Apr","RPL recent"])
        r=5
        for g in groups:
            for th in THEME_ORDER:
                ws.cell(row=r,column=1,value=g); ws.cell(row=r,column=2,value=th)
                ws.cell(row=r,column=3,value=f'=({ks("F","A",r)}-{ks("G","A",r)})/{KW_DAYS_A}').number_format=M
                ws.cell(row=r,column=4,value=f'=({ks("F","B",r)}-{ks("G","B",r)})/{KW_DAYS_B}').number_format=M
                ws.cell(row=r,column=5,value=f"=D{r}-C{r}").number_format=M
                ws.cell(row=r,column=6,value=f'=IFERROR({ks("F","A",r)}/{ks("H","A",r)},0)').number_format="0.0"
                ws.cell(row=r,column=7,value=f'=IFERROR({ks("F","B",r)}/{ks("H","B",r)},0)').number_format="0.0"
                if g=="All other campaigns": ws.cell(row=r,column=1).font=note
                for c in range(1,8): box(ws,r,c)
                r+=1
        from openpyxl.formatting.rule import ColorScaleRule
        ws.conditional_formatting.add(f"E5:E{r-1}",ColorScaleRule(
            start_type='min',start_color='F4A6A6',mid_type='num',mid_value=0,mid_color='FFFFFF',
            end_type='max',end_color='9FD18B'))
        # ── read-across the account: theme (rows) × campaign group (cols) ──
        def mk(col,per,g,rr): return (f'SUMIFS({RKT}!${col}:${col},{RKT}!${gL}:${gL},"{g}",'
                                      f'{RKT}!$D:$D,$A{rr},{RKT}!$E:$E,"{per}")')
        def matrix(start,titletxt,cellfn,fmt,colorscale):
            ws.cell(row=start,column=1,value=titletxt).font=h2
            hdr(ws,start+1,["theme"]+groups)
            rr=start+2
            for th in THEME_ORDER:
                ws.cell(row=rr,column=1,value=th).font=lbl
                for ci,g in enumerate(groups,2):
                    ws.cell(row=rr,column=ci,value=cellfn(g,rr)).number_format=fmt
                for c in range(1,len(groups)+2): box(ws,rr,c)
                rr+=1
            if colorscale:
                ws.conditional_formatting.add(f"B{start+2}:{get_column_letter(len(groups)+1)}{rr-1}",ColorScaleRule(
                    start_type='min',start_color='F4A6A6',mid_type='num',mid_value=0,mid_color='FFFFFF',
                    end_type='max',end_color='9FD18B'))
            return rr
        pctred=r'0.0%;[Red]\-0.0%'
        m1=matrix(r+2,"Read across — GP/day Δ (recent − April) by theme × campaign group",
                  lambda g,rr:f'=({mk("F","B",g,rr)}-{mk("G","B",g,rr)})/{KW_DAYS_B}-({mk("F","A",g,rr)}-{mk("G","A",g,rr)})/{KW_DAYS_A}',
                  M,True)
        m2=matrix(m1+2,"Read across — RPL Δ% (recent vs April) by theme × campaign group",
                  lambda g,rr:f'=IFERROR(({mk("F","B",g,rr)}/{mk("H","B",g,rr)})/({mk("F","A",g,rr)}/{mk("H","A",g,rr)})-1,"")',
                  pctred,False)
        for c,w in zip("ABCDEFG",[18,14,14,14,18,11,11]): ws.column_dimensions[c].width=w
    else:
        ws["A3"]="PENDING — run analyze_keyword_themes.py, then rerun this builder to populate."; ws["A3"].font=F(bold=True,color="C00000")

    # ── Mechanism_by_Theme (per problem campaign: ROAS decomposition split by theme; live off Raw_Keyword_Themes) ──
    if kp.exists():
        ws=wb.create_sheet("Mechanism_by_Theme",5)
        ws["A1"]="Bucket 3 — Per-theme mechanism inside the problem campaigns"; ws["A1"].font=title
        ws["A2"]=("ROAS = RPL × CvR ÷ CPC, decomposed by keyword theme for each problem campaign. Every cell is a live SUMIFS off "
                  "Raw_Keyword_Themes; clicks & GP per day (April ÷30, recent ÷21), ratio Δ% window-invariant. Read a Δ% COLUMN down the four "
                  "themes: red across all four = a BROAD move (e.g. CvR↓ / CPC↑); red in one theme only = concentrated.")
        ws["A2"].font=note; ws["A2"].alignment=Alignment(wrap_text=True)
        RKT="Raw_Keyword_Themes"; THEMES=["geico","farmers","all_other_carriers","generic"]
        PCTR=r'0.0%;[Red]\-0.0%'
        def ms(col,per,cmp,rr):
            return (f'SUMIFS({RKT}!${col}:${col},{RKT}!$A:$A,"{cmp}",'
                    f'{RKT}!$D:$D,$A{rr},{RKT}!$E:$E,"{per}")')
        cols=["theme","clicks/d Apr","clicks/d rec","clicks Δ%","CvR Δ%","CPC Δ%","RPL Δ%","ROAS Δ%","GP/d Apr","GP/d rec","GP/d Δ%"]
        r=4
        for cmp in ["Tier One","Tier Four","California"]:
            ws.cell(row=r,column=1,value=cmp).font=h2
            hdr(ws,r+1,cols)
            base=r+2
            for k,th in enumerate(THEMES):
                rr=base+k
                ws.cell(row=rr,column=1,value=th).font=lbl
                ws.cell(row=rr,column=2,value=f'={ms("I","A",cmp,rr)}/{KW_DAYS_A}').number_format="#,##0.0"
                ws.cell(row=rr,column=3,value=f'={ms("I","B",cmp,rr)}/{KW_DAYS_B}').number_format="#,##0.0"
                ws.cell(row=rr,column=4,value=f'=IFERROR(C{rr}/B{rr}-1,"")').number_format=PCTR
                ws.cell(row=rr,column=5,value=f'=IFERROR(({ms("H","B",cmp,rr)}/{ms("I","B",cmp,rr)})/({ms("H","A",cmp,rr)}/{ms("I","A",cmp,rr)})-1,"")').number_format=PCTR
                ws.cell(row=rr,column=6,value=f'=IFERROR(({ms("G","B",cmp,rr)}/{ms("I","B",cmp,rr)})/({ms("G","A",cmp,rr)}/{ms("I","A",cmp,rr)})-1,"")').number_format=PCTR
                ws.cell(row=rr,column=7,value=f'=IFERROR(({ms("F","B",cmp,rr)}/{ms("H","B",cmp,rr)})/({ms("F","A",cmp,rr)}/{ms("H","A",cmp,rr)})-1,"")').number_format=PCTR
                ws.cell(row=rr,column=8,value=f'=IFERROR(({ms("F","B",cmp,rr)}/{ms("G","B",cmp,rr)})/({ms("F","A",cmp,rr)}/{ms("G","A",cmp,rr)})-1,"")').number_format=PCTR
                ws.cell(row=rr,column=9,value=f'=({ms("F","A",cmp,rr)}-{ms("G","A",cmp,rr)})/{KW_DAYS_A}').number_format=M
                ws.cell(row=rr,column=10,value=f'=({ms("F","B",cmp,rr)}-{ms("G","B",cmp,rr)})/{KW_DAYS_B}').number_format=M
                ws.cell(row=rr,column=11,value=f'=IFERROR(J{rr}/I{rr}-1,"")').number_format=PCTR
                for c in range(1,12): box(ws,rr,c)
            r=base+len(THEMES)+2
        for c,w in zip("ABCDEFGHIJK",[18,11,11,9,9,9,9,9,10,10,9]): ws.column_dimensions[c].width=w

    # ── Raw data tab: geo matrix (workbook carries its own forensics) ──
    raw_tab(wb,"Raw_Geo_Matrix",a.matrix_csv,date_cols=("week_start",),
            num_cols=("revenue","conversions","clicks","cost","RPL","CvR","CPC"))

    # ── RPL_Redistribution (Bucket 3 lead exhibit; FORMULA-DRIVEN off Raw_Geo_Matrix) ──
    ws=wb.create_sheet("RPL_Redistribution",3)
    ws["A1"]="Bucket 3 — State-level RPL effect on GP (the redistribution)"; ws["A1"].font=title
    ws["A2"]=("LOCKED: RPL effect/day = Σconv_recent × (RPL_recent − RPL_April) ÷ 21, holding volume to isolate per-lead value. "
              "Every number below computes live from Raw_Geo_Matrix via SUMIFS (April = 04-06..04-27 Mondays, recent = 05-18 on). "
              "Native-conv basis — $ levels ~13% conservative, RPL ratio sound.")
    ws["A2"].font=note; ws["A2"].alignment=Alignment(wrap_text=True)
    from openpyxl.formatting.rule import ColorScaleRule
    def diverging():
        return ColorScaleRule(start_type='min',start_color='F4A6A6',
                              mid_type='num',mid_value=0,mid_color='FFFFFF',
                              end_type='max',end_color='9FD18B')
    GM="Raw_Geo_Matrix"
    allrows=list(csv.DictReader(open(a.rpl_summary_csv)))
    named_rows=[d for d in allrows if d["state"]!="All Other"]
    other_rows=[d for d in allrows if d["state"]=="All Other"]
    hdr(ws,4,["state","tier","Σrev Apr","Σconv Apr","RPL Apr","Σrev rec","Σconv rec",
              "RPL rec","leads/day","RPL effect $/day","py check $/day","Δ vs formula"])
    def write_state(rr,d,grey=False):
        st=d["state"]; ws.cell(row=rr,column=1,value=st)
        ws.cell(row=rr,column=2,value=("18 states" if grey else assign.get(st,"—")))
        c_apr=(f'=SUMIFS({GM}!$D:$D,{GM}!$A:$A,$A{rr},{GM}!$B:$B,"TOTAL",'
               f'{GM}!$C:$C,">="&DATE(2026,4,1),{GM}!$C:$C,"<="&DATE(2026,4,27))')
        f_rec=(f'=SUMIFS({GM}!$D:$D,{GM}!$A:$A,$A{rr},{GM}!$B:$B,"TOTAL",'
               f'{GM}!$C:$C,">="&DATE(2026,5,18))')
        ws.cell(row=rr,column=3,value=c_apr).number_format=M
        ws.cell(row=rr,column=4,value=c_apr.replace("$D:$D","$E:$E")).number_format="0.0"
        ws.cell(row=rr,column=5,value=f"=IF(D{rr}=0,0,C{rr}/D{rr})").number_format="0.0"
        ws.cell(row=rr,column=6,value=f_rec).number_format=M
        ws.cell(row=rr,column=7,value=f_rec.replace("$D:$D","$E:$E")).number_format="0.0"
        ws.cell(row=rr,column=8,value=f"=IF(G{rr}=0,0,F{rr}/G{rr})").number_format="0.0"
        ws.cell(row=rr,column=9,value=f"=G{rr}/21").number_format="0.0"
        ws.cell(row=rr,column=10,value=f"=G{rr}*(H{rr}-E{rr})/21").number_format=M
        ws.cell(row=rr,column=11,value=round(float(d["rpl_effect_per_day"]),1)).number_format=M
        ws.cell(row=rr,column=12,value=f"=J{rr}-K{rr}").number_format="0.000"
        for c in range(1,13):
            box(ws,rr,c)
            if grey: ws.cell(row=rr,column=c).font=F(italic=True,color="808080")
    r=5
    for d in named_rows: write_state(r,d); r+=1
    lastnamed=r-1
    for d in other_rows: write_state(r,d,grey=True); r+=1
    last=r-1
    ws.conditional_formatting.add(f"J5:J{lastnamed}",diverging())   # graded by magnitude, white at 0
    # totals (named decay/growth; net covers the full account incl All Other)
    tots=[("Σ decay (named)",f'=SUMIF(J5:J{lastnamed},"<0")',BAD),
          ("Σ growth (named)",f'=SUMIF(J5:J{lastnamed},">0")',GOOD)]
    if other_rows: tots.append(("All Other (18 states)",f"=J{last}",None))
    tots.append(("net — full account",f"=SUM(J5:J{last})",None))
    tr=last+1
    for k,(lab,formula,col) in enumerate(tots):
        ws.cell(row=tr+k,column=9,value=lab).font=lbl
        cc=ws.cell(row=tr+k,column=10,value=formula); cc.number_format=M; cc.font=lbl
        if col: cc.fill=fill(col)
    # by-tier subtotals (which tier is bleeding — without re-sorting the states)
    tier_tot=defaultdict(float)
    for d in named_rows: tier_tot[assign.get(d["state"],"—")]+=float(d["rpl_effect_per_day"])
    tier_order=sorted(tier_tot,key=lambda t:tier_tot[t])
    tb=tr+len(tots)+1
    ws.cell(row=tb,column=9,value="By campaign tier ($/day):").font=h2
    for k,t in enumerate(tier_order,1):
        ws.cell(row=tb+k,column=9,value=t).font=bf
        cc=ws.cell(row=tb+k,column=10,value=f'=SUMIF($B$5:$B${lastnamed},"{t}",$J$5:$J${lastnamed})')
        cc.number_format=M
    # build self-check: live formula must match the analysis script (max |Δ| ≈ 0)
    ws.cell(row=3,column=1,value="Build self-check — max |live formula − analysis script|, $/day →").font=note
    sc=ws.cell(row=3,column=10,value=f"=MAX(MAX(L5:L{last}),-MIN(L5:L{last}))"); sc.number_format="0.000"; sc.font=lbl
    # diverging chart off the live effect column (named states only)
    ch=BarChart(); ch.type="bar"; ch.title="RPL effect on GP ($/day): decay vs growth"; ch.height=13; ch.width=17; ch.legend=None
    ch.add_data(Reference(ws,min_col=10,min_row=4,max_row=lastnamed),titles_from_data=True)
    ch.set_categories(Reference(ws,min_col=1,min_row=5,max_row=lastnamed))
    ws.add_chart(ch,"N4")
    # weekly reference table (light; ties to the live summary via recent-3 Σ; color scale to read trajectory)
    if Path(a.rpl_weekly_csv).exists():
        wr=tb+len(tier_order)+2
        ws.cell(row=wr,column=1,value="Weekly RPL effect $ (reference — color scale: red destroyed, green gained, white ~0; recent-3 Σ ties to summary $/day × 21)").font=h2
        wrows=list(csv.reader(open(a.rpl_weekly_csv)))
        keep=[i for i,w in enumerate(wrows[0]) if i==0 or w>="2026-04-01"]
        ncol=len(keep)
        hdr(ws,wr+1,["state"]+[wrows[0][i][5:] for i in keep[1:]]+["recent-3 Σ"])
        rr=wr+2
        for row in wrows[1:]:
            if row[0]=="All Other": continue
            ws.cell(row=rr,column=1,value=row[0]); box(ws,rr,1)
            for j2,i in enumerate(keep[1:],2):
                v=row[i]; cell=ws.cell(row=rr,column=j2,value=float(v) if v not in("",None) else None)
                cell.number_format=M; box(ws,rr,j2)
            tc=ws.cell(row=rr,column=ncol+1,
                       value=f"=SUM({get_column_letter(ncol-2)}{rr}:{get_column_letter(ncol)}{rr})")
            tc.number_format=M; box(ws,rr,ncol+1)
            rr+=1
        ws.conditional_formatting.add(f"B{wr+2}:{get_column_letter(ncol)}{rr-1}",diverging())
    for c,w in zip("ABCDEFGHIJKL",[16,14,11,10,9,11,10,9,9,15,14,12]): ws.column_dimensions[c].width=w

    # ── Mechanism_Model (ALL campaigns, per-day, live formulas off Raw_Campaign_Daily) ──
    ws=wb.create_sheet("Mechanism_Model",4)
    ws["A1"]="Bucket 3 — How RPL decay becomes GP loss (all campaigns, GP drainers on top)"; ws["A1"].font=title
    ws["A2"]=("All campaigns, ordered by GP/day lost April→recent (biggest drainer first). Inputs normalized PER DAY "
              "(April ÷30, recent ÷21) via SUMIFS off Raw_Campaign_Daily; VPC, CVR, RPL, CPC, ROAS, GP and the ROAS "
              "drivers are LIVE formulas (ratios window-invariant). Primary-conv basis (matches B1_Phase_GP).")
    ws["A2"].font=note; ws["A2"].alignment=Alignment(wrap_text=True)
    cd_rows,camp_daily=campaign_daily(a.datatables)
    rcd=wb.create_sheet("Raw_Campaign_Daily")      # workbook carries its own campaign-daily forensics
    hdr(rcd,1,["campaign","date","clicks","cost","primary_conv","revenue"])
    for i,(nm,d,ck,cs,cv,rv) in enumerate(cd_rows,2):
        rcd.cell(row=i,column=1,value=nm)
        c=rcd.cell(row=i,column=2,value=dt.datetime(d.year,d.month,d.day)); c.number_format="yyyy-mm-dd"
        rcd.cell(row=i,column=3,value=ck); rcd.cell(row=i,column=4,value=cs)
        rcd.cell(row=i,column=5,value=cv); rcd.cell(row=i,column=6,value=rv)
    rcd.freeze_panes="A2"
    for c,w in zip("ABCDEF",[14,12,10,12,12,12]): rcd.column_dimensions[c].width=w
    RCD="Raw_Campaign_Daily"
    def gpd_loss(nm):
        A=camp_daily[nm]['A']; B=camp_daily[nm]['B']
        return (A['rv']-A['cs'])/KW_DAYS_A-(B['rv']-B['cs'])/KW_DAYS_B   # +ve = GP drained
    order=sorted([nm for nm in camp_daily if camp_daily[nm]['A']['ck'] and camp_daily[nm]['B']['ck']],
                 key=gpd_loss,reverse=True)
    def sif(col,cmp,win):
        if win=="A": crit=f'{RCD}!$B:$B,">="&DATE(2026,4,1),{RCD}!$B:$B,"<="&DATE(2026,4,30)'; days=KW_DAYS_A
        else:        crit=f'{RCD}!$B:$B,">="&DATE(2026,5,18),{RCD}!$B:$B,"<="&DATE(2026,6,7)'; days=KW_DAYS_B
        return f'=SUMIFS({RCD}!${col}:${col},{RCD}!$A:$A,"{cmp}",{crit})/{days}'
    ANNOT={
     "Tier One":("Per-lead value collapse (structural).",
        "GP/day $348→$11. ROAS −21% is entirely RPL −21%; CvR & CPC ~flat.",
        "Stale tiering — premium NE states (NJ/CT/NY) RPL eroded but bid as one blend. Value falls, the bidder can't fix it via CPC, so it sheds position.",
        "Re-tier: carve the premium states out and bid them on their own value. Not a bidding problem."),
     "Tier Four":("Volume pullback + efficiency squeeze, not value.",
        "GP/day $348→$137. RPL +2.3% (value held). ROAS −9% = CvR −6.3% + CPC +5.5%; clicks down ~22–32% across ALL themes.",
        "Account-wide tROAS pullback cut clicks broadly; at higher auction prices the lost volume now costs more and converts worse.",
        "Bid simulator: lost volume is NOT profitably recoverable now (marginal ROAS ≈0.68). Hold or tighten toward GP-max (~125%); redeploy freed budget to growers."),
     "California":("Orderly contraction — benign.",
        "GP/day $145→$97. ROAS ~flat (+0.2%): RPL −12% offset by CPC −10%, CvR steady.",
        "Generic-driven: generic RPL −19% while both carrier buckets grew. Volume scaled down at stable economics.",
        "Lowest urgency. Monitor generic RPL; no structural fix needed."),
    }
    gp_row={}
    r=4
    for cmp in order:
        ws.cell(row=r,column=1,value=cmp).font=h2
        hdr(ws,r+1,["metric (→ = formula)","April","recent","Δ%"])
        base=r+2
        for k,(name,col,fmt) in enumerate([("clicks/day","C","#,##0.0"),("conversions/day","E","#,##0.0"),
                ("value/day (revenue)","F",M),("cost/day","D",M)]):
            rr=base+k; ws.cell(row=rr,column=1,value=name).font=lbl
            ws.cell(row=rr,column=2,value=sif(col,cmp,"A")).number_format=fmt
            ws.cell(row=rr,column=3,value=sif(col,cmp,"B")).number_format=fmt
            ws.cell(row=rr,column=4,value=f"=IF(B{rr}=0,0,C{rr}/B{rr}-1)").number_format="0.0%"
            for c in range(1,5): box(ws,rr,c)
        clk,cnv,val,cst=base,base+1,base+2,base+3; rr=base+4
        gp_row[cmp]=base+9   # GP/day row (VPC,CVR,RPL,CPC,ROAS,GP = base+4..+9)
        for name,fa,fb,fmt in [("→ VPC = value/clicks",f"=B{val}/B{clk}",f"=C{val}/C{clk}","0.00"),
                ("→ CVR = conv/clicks",f"=B{cnv}/B{clk}",f"=C{cnv}/C{clk}","0.0%"),
                ("→ RPL = value/conv",f"=B{val}/B{cnv}",f"=C{val}/C{cnv}","0.00"),
                ("→ CPC = cost/clicks",f"=B{cst}/B{clk}",f"=C{cst}/C{clk}","0.00"),
                ("→ ROAS = value/cost",f"=B{val}/B{cst}",f"=C{val}/C{cst}","0.00"),
                ("→ GP/day = value − cost",f"=B{val}-B{cst}",f"=C{val}-C{cst}",M)]:
            ws.cell(row=rr,column=1,value=name)
            ws.cell(row=rr,column=2,value=fa).number_format=fmt
            ws.cell(row=rr,column=3,value=fb).number_format=fmt
            ws.cell(row=rr,column=4,value=f"=IF(B{rr}=0,0,C{rr}/B{rr}-1)").number_format="0.0%"
            if name.startswith("→ GP"):
                gpcol=GOOD if gpd_loss(cmp)<0 else BAD
                for c in range(1,5): ws.cell(row=rr,column=c).fill=fill(gpcol)
            for c in range(1,5): box(ws,rr,c)
            rr+=1
        cvr_r,rpl_r,cpc_r=base+5,base+6,base+7
        ws.cell(row=rr,column=1,value="ROAS drivers (ROAS = RPL × CvR ÷ CPC; ≈ add to ROAS Δ%)").font=lbl
        drv=[("  from RPL",f"=C{rpl_r}/B{rpl_r}-1"),
             ("  from CvR",f"=C{cvr_r}/B{cvr_r}-1"),
             ("  from CPC (inverse)",f"=-(C{cpc_r}/B{cpc_r}-1)"),
             ("  = combined (exact, ties to ROAS Δ%)",
              f"=(C{rpl_r}/B{rpl_r})*(C{cvr_r}/B{cvr_r})*(B{cpc_r}/C{cpc_r})-1")]
        for k,(lab,fo) in enumerate(drv,1):
            ws.cell(row=rr+k,column=1,value=lab).font=(lbl if "combined" in lab else bf)
            cc=ws.cell(row=rr+k,column=4,value=fo); cc.number_format="0.0%"
            if "combined" in lab: cc.font=lbl
            for c in range(1,5): box(ws,rr+k,c)
        # Problem / Root Cause / Fix annotation (problem campaigns) or grower note, in column F
        if cmp in ANNOT:
            head,prob,root,fix=ANNOT[cmp]
            ws.cell(row=r,column=6,value=head).font=F(bold=True,color="C00000")
            for off,(tag,txt) in zip((2,6,10),[("Problem",prob),("Root cause",root),("Fix",fix)]):
                cell=ws.cell(row=r+off,column=6,value=f"{tag} — {txt}")
                cell.alignment=Alignment(wrap_text=True,vertical="top"); cell.font=bf
        else:
            ws.cell(row=r,column=6,value="Grower — offsetting the drainers (+GP/day).").font=note
        r=rr+len(drv)+3
    # total GP/day lost across the problem campaigns (live)
    prob=[c for c in ["Tier One","Tier Four","California"] if c in gp_row]
    if prob:
        expr="+".join(f"(B{gp_row[c]}-C{gp_row[c]})" for c in prob)
        ws.cell(row=3,column=1,value="Total GP/day lost — problem campaigns (Tier One + Tier Four + California), April→recent:").font=lbl
        d=ws.cell(row=3,column=4,value=f"={expr}"); d.number_format=M; d.font=F(bold=True,color="C00000")
        ws.cell(row=3,column=6,value=f"=D3*365").number_format=M
        ws.cell(row=3,column=7,value="≈ annualized GP at risk").font=note
    for c,w in zip("ABCDEFG",[26,14,14,11,4,60,18]): ws.column_dimensions[c].width=w
    # flowchart from the top GP drainer (regenerated each build)
    if order:
        top=camp_daily[order[0]]; AB=lambda w:{'v':top[w]['rv'],'c':top[w]['cs'],'cv':top[w]['cv'],'ck':top[w]['ck']}
        outdir=Path(a.out).parent if Path(a.out).is_absolute() else Path("/mnt/user-data/outputs")
        png=make_mechanism_png(str(outdir/"mechanism_flow.png"),AB('A'),AB('B'),label=order[0])
        if png: ws.add_image(XLImage(png),f"A{r+1}")

    # ── Tier4_Bid_Sim (Google bid simulator snapshot + GP curve / marginal ROAS; the action quant) ──
    ws=wb.create_sheet("Tier4_Bid_Sim")
    ws["A1"]="Tier Four — bid-target action, quantified (Google bid simulator)"; ws["A1"].font=title
    ws["A2"]=("Simulator snapshot for HOME INSURANCE-TIER FOUR-NB-SEARCH-ALL-USA-STE, week May 31–Jun 6 2026 (weekly figures). "
              "GP and marginal ROAS are LIVE off the snapshot. Marginal ROAS = Δconv-value ÷ Δcost vs the current 117% row — the decision metric: "
              "loosen only while it clears your GP threshold (~1.0 if conv-value ≈ routed revenue). NOTE: simulator optimizes to Google conv-value; "
              "confirm value/conv (~$78) ≈ your routed RPL — if routed RPL is materially higher, the GP-max target shifts looser.")
    ws["A2"].font=note; ws["A2"].alignment=Alignment(wrap_text=True)
    sim=[("Last week (117%)",8844.56,113,8745.59,None),("136%",4898.20,72,5515.14,None),
         ("125%",6853.09,93,7185.57,None),("117% (current)",8991.36,115,8938.36,None),
         ("109%",10834.77,131,10189.60,54.36),("103%",12683.54,146,11433.54,468.83),
         ("98%",15026.70,164,12892.57,987.50),("91%",16785.42,177,13986.34,1468.91),
         ("90%",19266.75,195,15463.63,2128.20),("86%",22703.02,220,17455.19,2956.73),
         ("81%",27505.54,251,20016.33,4090.23)]
    hdr(ws,4,["target ROAS","cost/wk","conv","conv value","GP/wk (=val−cost)","marginal ROAS vs current","addl budget/day"])
    cur=5+next(i for i,s in enumerate(sim) if "current" in s[0])
    r=5
    for nm,c,n,v,b in sim:
        ws.cell(row=r,column=1,value=nm)
        ws.cell(row=r,column=2,value=c).number_format=M
        ws.cell(row=r,column=3,value=n)
        ws.cell(row=r,column=4,value=v).number_format=M
        ws.cell(row=r,column=5,value=f"=D{r}-B{r}").number_format=M
        if r!=cur:
            ws.cell(row=r,column=6,value=f"=IFERROR((D{r}-D{cur})/(B{r}-B{cur}),\"\")").number_format="0.00"
        ws.cell(row=r,column=7,value=(b if b is not None else "—"))
        if b is not None and not isinstance(b,str): ws.cell(row=r,column=7).number_format=M
        if r==cur:
            for c2 in range(1,8): ws.cell(row=r,column=c2).fill=fill(AMBER)
        for c2 in range(1,8): box(ws,r,c2)
        r+=1
    rec=("Read-out: GP/wk peaks at the TIGHT end (136% ≈ +$617) and is ≈breakeven at current 117% (−$53); every loosening step is GP-negative. "
         "Marginal ROAS of loosening is ~0.68 and falls to ~0.53 — the lost volume can't be bought back profitably at today's auction prices "
         "(the CPC +5.5% / CvR −6.3% squeeze is why). Action: do NOT loosen to chase volume. Hold, or tighten toward ~125% "
         "(modeled +$385/wk GP, −19% conv, frees ~$2,138/wk) and redeploy that budget to the growers (Tier Three, Texas) where marginal ROAS is higher. "
         "Caveat: a campaign-wide target change removes existing ad-group targets.")
    ws.cell(row=r+1,column=1,value=rec).font=bf
    ws.cell(row=r+1,column=1).alignment=Alignment(wrap_text=True,vertical="top")
    ws.merge_cells(start_row=r+1,start_column=1,end_row=r+5,end_column=7)
    for c,w in zip("ABCDEFG",[18,12,8,12,16,20,15]): ws.column_dimensions[c].width=w

    Path("/mnt/user-data/outputs").mkdir(parents=True,exist_ok=True)
    out=Path(a.out)
    if not out.is_absolute(): out=Path("/mnt/user-data/outputs")/out
    wb.save(out); print("saved:",out)

if __name__=="__main__":
    main()
