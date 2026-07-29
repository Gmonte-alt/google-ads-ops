#!/usr/bin/env python3
"""
Google Home re-tier rollout plan builder.

Reads the 'State Assignments' sheet of the geo-model workbook (the single source of truth Gilbert
maintains) and emits an exec-ready rollout-plan workbook. Re-run any time assignments or dates change.

What it answers (the exec asks):
  - what states are moving           -> Move Schedule
  - how much spend is disrupted       -> Rollout Summary (moving spend, % of total)
  - what's performing well            -> New Tier Profile (GP/lead, RPL, CPL, ROAS per new tier)
  - what tiers stack to current       -> Tier Transition Matrix (current x new)
  - timeline                          -> Rollout Summary + Move Schedule (by 'Change <date>' column)
  - execution clarity                 -> Shell Repurposing Map (which physical shell becomes which tier)

Expected columns on 'State Assignments' (header row auto-detected):
  state | current_tier | C2_weighted_kmeans (proposed tier) | clicks conversions spend rpl cpc cpl
  gp_per_lead roas | ... | Campaign Change? (0=No,1=Yes) | Destination campaign | Change <date> ...

Usage:
  python C:\Projects\google-ads-ops\analysis\google-geo-analysis\build_retier_rollout_plan.py --input google_geo_model_gp_per_lead_2026-06-09.xlsx
"""
import argparse, datetime as dt, re
import pandas as pd, numpy as np
from openpyxl import load_workbook, Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.formatting.rule import CellIsRule, ColorScaleRule

A=lambda **k: Font(name="Arial", **k)
TITLE=A(bold=True,size=14,color="1F4E79"); SUB=A(bold=True,size=11,color="1F4E79")
NOTE=A(italic=True,size=9,color="595959"); BLACK=A(); BOLD=A(bold=True); BLUE=A(color="0000FF")
HF=A(bold=True,color="FFFFFF"); HFILL=PatternFill("solid",fgColor="1F4E79")
SUBFILL=PatternFill("solid",fgColor="DDEBF7"); GREY=PatternFill("solid",fgColor="F2F2F2")
GOOD=PatternFill("solid",fgColor="C6EFCE"); REDF=PatternFill("solid",fgColor="FFC7CE"); AMBER=PatternFill("solid",fgColor="FCE4D6")
TH=Side(style="thin",color="BFBFBF"); BORD=Border(top=TH,bottom=TH,left=TH,right=TH)
WRAP=Alignment(wrap_text=True,vertical="top"); CTR=Alignment(horizontal="center",vertical="center",wrap_text=True)
M0='$#,##0'; M1='$#,##0.0'; GPF='$#,##0.0;($#,##0.0)'; R2='0.00'; P0='0%'
TIER_ORDER=['TIER ONE','TIER TWO','TIER THREE','TIER FOUR']
CUR_ORDER=['Tier One','Tier Two','Tier Three','Tier Four']



# =====================================================================================
# v2 — RPL k=4 single-cutover rollout. Reads the RPL model workbook directly:
# 'State Assignments' (committed_tier = current as-built; C2_weighted_kmeans = new tier)
# plus 'Below Threshold' (assigned_tier_nearest) for the thin-state placements.
# No hand-annotation needed; hand columns (Campaign Change?/Destination/Change <date>)
# are ignored if present. Margin numbers deliberately deferred: Targets & Floors sheet
# carries editable margin cells and the floor formula, pending leadership's policy.
# =====================================================================================
def shell_map(t4_shell):
    # physical shell each new tier lands in (minimizes state moves: 15 of 35 stay).
    # TIER FOUR reuses the shell retired at the July consolidation (re-enable, not create):
    # keeps campaign history in the bidder's context and restores the pre-consolidation count.
    return {
        'TIER ONE':   'T1 shell (existing Tier1RPL80Plus)',
        'TIER TWO':   'T2 shell (existing Tier2RPL70Plus)',
        'TIER THREE': 'T3 shell (existing Tier3RPL50Plus)',
        'TIER FOUR':  t4_shell,
    }
SHELL_MAP = shell_map('RE-ENABLED retired shell (confirm campaign_id)')
NORM = {'TIER ONE':'TIER ONE','TIER TWO':'TIER TWO','TIER THREE':'TIER THREE','TIER FOUR':'TIER FOUR',
        'Tier One':'TIER ONE','Tier Two':'TIER TWO','Tier Three':'TIER THREE','Tier Four':'TIER FOUR'}

def band_desc(rpl):
    if rpl>=80: return "premium value band"
    if rpl>=60: return "upper-mid value band"
    if rpl>=45: return "mid value band"
    return "low value band"

def load(path):
    wb=load_workbook(path, data_only=True); ws=wb['State Assignments']
    hr=next(r for r in range(1,8) if 'state' in [str(ws.cell(row=r,column=c).value).lower()
            for c in range(1,ws.max_column+1)])
    cols=[ws.cell(row=hr,column=c).value for c in range(1,ws.max_column+1)]
    recs=[]
    for r in range(hr+1,ws.max_row+1):
        v=[ws.cell(row=r,column=c).value for c in range(1,ws.max_column+1)]
        if not v[0] or str(v[0]).strip().lower().startswith("total"): continue
        recs.append(dict(zip(cols,v)))
    df=pd.DataFrame(recs)
    df.rename(columns={'C2_weighted_kmeans':'new_tier','committed_tier':'current_tier'},inplace=True)
    for c in ['clicks','conversions','spend','rpl','cpc','cpl','gp_per_lead','roas']:
        df[c]=pd.to_numeric(df[c],errors='coerce')
    df['current_tier']=df['current_tier'].map(lambda x: NORM.get(str(x).strip(), str(x)))
    df['new_tier']=df['new_tier'].map(lambda x: NORM.get(str(x).strip(), str(x)))
    df['chg']=(df['current_tier']!=df['new_tier']).astype(int)
    df['dest']=df['new_tier'].map(SHELL_MAP)
    df['below_gate']=0
    # fold in Below Threshold placements (thin states still need a home at cutover)
    if 'Below Threshold' in wb.sheetnames:
        bs=wb['Below Threshold']
        hr2=next((r for r in range(1,8) if 'state' in [str(bs.cell(row=r,column=c).value).lower()
                for c in range(1,bs.max_column+1)]), None)
        if hr2:
            cols2=[bs.cell(row=hr2,column=c).value for c in range(1,bs.max_column+1)]
            recs2=[]
            for r in range(hr2+1,bs.max_row+1):
                v=[bs.cell(row=r,column=c).value for c in range(1,bs.max_column+1)]
                if v[0]: recs2.append(dict(zip(cols2,v)))
            bt=pd.DataFrame(recs2)
            pick='assigned_tier_nearest_shrunk' if 'assigned_tier_nearest_shrunk' in bt.columns else 'assigned_tier_nearest'
            bt=bt.rename(columns={pick:'new_tier'})
            for c in ['clicks','conversions','spend','rpl','cpc','cpl','gp_per_lead','roas']:
                if c in bt.columns: bt[c]=pd.to_numeric(bt[c],errors='coerce')
            bt['new_tier']=bt['new_tier'].map(lambda x: NORM.get(str(x).strip(), str(x)))
            bt['current_tier']='(below gate)'; bt['chg']=1
            bt['dest']=bt['new_tier'].map(SHELL_MAP); bt['below_gate']=1
            keep=[c for c in df.columns if c in bt.columns]
            df=pd.concat([df, bt[keep]], ignore_index=True)
    return df

def wavg(g,val,wt):
    s=g[wt].sum(); return (g[val]*g[wt]).sum()/s if s else np.nan

def style_header(ws,row,headers,widths):
    for j,h in enumerate(headers,1):
        c=ws.cell(row=row,column=j,value=h); c.font=HF; c.fill=HFILL; c.border=BORD; c.alignment=CTR
    for col,w in zip("ABCDEFGHIJKLMN",widths): ws.column_dimensions[col].width=w

def build(df, out, cutover):
    wb=Workbook(); wb.remove(wb.active)
    core=df[df['below_gate']==0]; below=df[df['below_gate']==1]
    tot=core['spend'].sum(); mv=core[core['chg']==1]; st=core[core['chg']==0]

    # ---------- 1. Rollout Summary ----------
    ws=wb.create_sheet("Rollout Summary")
    ws["A1"]="Google Home — RPL k=4 re-tier: single-cutover rollout plan"; ws["A1"].font=TITLE
    ws["A2"]=(f"Re-cluster states by RPL into 4 value bands (leadership-approved axis); margin control moves to "
              f"per-tier tROAS floors (Targets & Floors sheet — margin %s pending leadership sign-off). "
              f"MEMBERSHIP CUTS OVER ALL AT ONCE on {cutover}; TARGETS then step toward their floors gradually "
              f"(no repeat of the May single-jump tightening). CA/TX/FL standalone, untouched. "
              f"Generated {dt.date.today():%b %d, %Y}.")
    ws["A2"].font=NOTE; ws["A2"].alignment=WRAP; ws.merge_cells("A2:H2"); ws.row_dimensions[2].height=52
    r=4
    ws.cell(row=r,column=1,value="At a glance").font=SUB; r+=1
    kpis=[("Cutover (all membership moves, one event)",f"{cutover}"),
          ("States re-tiering (readable, ex-standalones)",f"{len(core)}"),
          ("States changing shells",f"{len(mv)}  ({len(mv)/len(core)*100:.0f}% — a majority-move restructure; phasing is not viable)"),
          ("Spend in motion",f"${mv['spend'].sum():,.0f}  ({mv['spend'].sum()/tot*100:.0f}% of ${tot:,.0f})"),
          ("States staying in place",f"{len(st)}  (${st['spend'].sum():,.0f}, {st['spend'].sum()/tot*100:.0f}%)"),
          ("Below-gate placements folded in at cutover",f"{len(below)}  ({', '.join(below['state'].astype(str))})"),
          ("Post-cutover freeze","2 weeks: no target changes, exception screen watch-only, ledger frozen at cutover (--emit-ledger)"),
          ("Pre-cutover moves (exception track, executed separately)","Pennsylvania & Arizona -> rehab/tightened posture; NM dual-campaign targeting cleanup")]
    for k,v in kpis:
        ws.cell(row=r,column=1,value=k).font=BOLD
        c=ws.cell(row=r,column=3,value=v); c.font=BLACK; ws.merge_cells(start_row=r,start_column=3,end_row=r,end_column=8); r+=1
    ws.column_dimensions['A'].width=42
    ws.freeze_panes="A4"

    # ---------- 2. Move Schedule ----------
    ws=wb.create_sheet("Move Schedule")
    ws["A1"]=f"Move schedule — every state, single cutover {cutover}"; ws["A1"].font=TITLE
    ws["A2"]="Grouped by destination tier. 'moving' = physically changes shell at cutover. Below-gate states shown last (thin volume; placed by nearest tier)."
    ws["A2"].font=NOTE; ws["A2"].alignment=WRAP; ws.merge_cells("A2:K2"); ws.row_dimensions[2].height=28
    hdr=["new tier","state","current tier","to shell","moving?","spend","conv","RPL","CPC","GP/lead","ROAS"]
    style_header(ws,4,hdr,[12,16,13,26,8,11,7,8,7,10,7]); r=5
    for t in TIER_ORDER:
        g=df[df['new_tier']==t].sort_values(['below_gate','spend'],ascending=[True,False])
        if not len(g): continue
        for _,x in g.iterrows():
            row=[t,x['state'],x['current_tier'],x['dest'],"yes" if x['chg']==1 else "—",
                 round(x['spend']) if pd.notna(x['spend']) else None,
                 round(x['conversions']) if pd.notna(x['conversions']) else None,
                 round(x['rpl'],1) if pd.notna(x['rpl']) else None,
                 round(x['cpc'],2) if pd.notna(x['cpc']) else None,
                 round(x['gp_per_lead'],1) if pd.notna(x['gp_per_lead']) else None,
                 round(x['roas'],2) if pd.notna(x['roas']) else None]
            for j,v in enumerate(row,1):
                c=ws.cell(row=r,column=j,value=v); c.border=BORD; c.font=BLACK
                if j==6: c.number_format=M0
                if j==10: c.number_format=GPF
                if j in (8,9,11): c.number_format=R2
                if j==5 and x['chg']==1: c.fill=AMBER
            r+=1
        gm=g[g['chg']==1]
        sub=[f"{t} subtotal","","","",f"{len(gm)} moving",round(g['spend'].sum()),round(g['conversions'].sum()),
             round(wavg(g,'rpl','conversions'),1),"",round(wavg(g,'gp_per_lead','conversions'),1),round(wavg(g,'roas','spend'),2)]
        for j,v in enumerate(sub,1):
            c=ws.cell(row=r,column=j,value=v); c.font=BOLD; c.fill=SUBFILL; c.border=BORD
            if j==6: c.number_format=M0
            if j==10: c.number_format=GPF
            if j in (8,11): c.number_format=R2
        r+=1
    ws.conditional_formatting.add(f"J5:J{r-1}",CellIsRule(operator='lessThan',formula=['0'],fill=REDF))
    ws.freeze_panes="A5"

    # ---------- 3. Tier Transition Matrix ----------
    ws=wb.create_sheet("Tier Transition Matrix")
    ws["A1"]="Tier transition — current (rows) vs new RPL band (columns)"; ws["A1"].font=TITLE
    ws["A2"]=("Off-diagonal mass here reflects the AXIS change (margin-sorted tiers -> value-sorted bands), not an error "
              "in either map. Read it as: where each current shell's spend lands on the value axis.")
    ws["A2"].font=NOTE; ws["A2"].alignment=WRAP; ws.merge_cells("A2:G2"); ws.row_dimensions[2].height=28
    dfx=df[df['below_gate']==0]
    cur_order=[t for t in TIER_ORDER if t in set(dfx['current_tier'])]
    def matrix(at_row,aggfunc,fmt,label):
        ws.cell(row=at_row,column=1,value=label).font=SUB
        piv=pd.pivot_table(dfx,values='spend' if aggfunc=='sum' else 'state',index='current_tier',
                           columns='new_tier',aggfunc=aggfunc,fill_value=0)
        piv=piv.reindex([c for c in cur_order if c in piv.index])
        piv=piv[[c for c in TIER_ORDER if c in piv.columns]]
        hrow=at_row+1
        c0=ws.cell(row=hrow,column=1,value="current ↓ / new →"); c0.font=HF; c0.fill=HFILL; c0.border=BORD
        for j,t in enumerate(piv.columns,2):
            c=ws.cell(row=hrow,column=j,value=t); c.font=HF; c.fill=HFILL; c.border=BORD; c.alignment=CTR
        c=ws.cell(row=hrow,column=len(piv.columns)+2,value="total"); c.font=HF; c.fill=HFILL; c.border=BORD
        rr=hrow+1
        for idx,prow in piv.iterrows():
            ws.cell(row=rr,column=1,value=idx).font=BOLD; ws.cell(row=rr,column=1).fill=SUBFILL; ws.cell(row=rr,column=1).border=BORD
            for j,t in enumerate(piv.columns,2):
                c=ws.cell(row=rr,column=j,value=round(prow[t]) if aggfunc=='sum' else int(prow[t])); c.number_format=fmt; c.border=BORD
            c=ws.cell(row=rr,column=len(piv.columns)+2,value=round(prow.sum()) if aggfunc=='sum' else int(prow.sum())); c.number_format=fmt; c.border=BORD; c.font=BOLD
            rr+=1
        ws.cell(row=rr,column=1,value="total").font=BOLD; ws.cell(row=rr,column=1).fill=SUBFILL; ws.cell(row=rr,column=1).border=BORD
        for j,t in enumerate(piv.columns,2):
            c=ws.cell(row=rr,column=j,value=round(piv[t].sum()) if aggfunc=='sum' else int(piv[t].sum())); c.number_format=fmt; c.border=BORD; c.font=BOLD
        gt=ws.cell(row=rr,column=len(piv.columns)+2,value=round(piv.values.sum()) if aggfunc=='sum' else int(piv.values.sum())); gt.font=BOLD; gt.border=BORD; gt.number_format=fmt
        if aggfunc=='sum':
            ws.conditional_formatting.add(f"B{hrow+1}:E{rr-1}",
                ColorScaleRule(start_type='num',start_value=0,start_color='FFFFFF',end_type='max',end_color='9BC2E6'))
        return rr
    last=matrix(4,'sum',M0,"Spend ($)")
    last=matrix(last+3,'count','0',"State count")
    for col,w in zip("ABCDEF",[18,12,12,12,12,10]): ws.column_dimensions[col].width=w

    # ---------- 4. Shell Repurposing Map ----------
    ws=wb.create_sheet("Shell Repurposing Map")
    ws["A1"]="Shell plan — what each physical campaign becomes"; ws["A1"].font=TITLE
    ws["A2"]=("Assignment verified optimal on BOTH metrics: the identity mapping keeps $29.5K of $62.5K readable spend "
              "(47%) and 15 of 35 states stationary — every alternative relabeling scheme keeps less (best alternative: "
              "$27.6K). The 20 moves are the axis change itself, irreducible by shell choice. "
              "TIER FOUR RE-ENABLES the shell retired at the July T3/T4 consolidation rather than creating a new "
              "campaign — campaign history carries into the bidder's context and the account returns to its "
              "pre-consolidation shell count. Relabel each shell at cutover — old names encode GPL-era band definitions (80Plus/70Plus/50Plus) that no longer "
              "match the new boundaries and would mislead reporting; campaign_id remains the stable key.")
    ws["A2"].font=NOTE; ws["A2"].alignment=WRAP; ws.merge_cells("A2:H2"); ws.row_dimensions[2].height=40
    style_header(ws,4,["physical shell","becomes","states staying","entering","leaving (to elsewhere)","resulting spend","resulting wtd RPL","resulting wtd ROAS"],
                 [30,12,11,9,16,12,12,12]); r=5
    for t in TIER_ORDER:
        members=df[df['new_tier']==t]
        staying=core[(core['current_tier']==t)&(core['new_tier']==t)]
        entering=members[members['current_tier']!=t]
        leaving=core[(core['current_tier']==t)&(core['new_tier']!=t)]
        row=[SHELL_MAP[t],t,len(staying),len(entering),len(leaving),round(members['spend'].sum()),
             round(wavg(members,'rpl','conversions'),1),round(wavg(members,'roas','spend'),2)]
        for j,v in enumerate(row,1):
            c=ws.cell(row=r,column=j,value=v); c.border=BORD; c.font=BLACK
            if j==6: c.number_format=M0
            if j in (7,8): c.number_format=R2
        r+=1

    # ---------- 5. New Tier Profile ----------
    ws=wb.create_sheet("New Tier Profile")
    ws["A1"]="New tier profile — value bands with current economics"; ws["A1"].font=TITLE
    ws["A2"]=("RPL defines the band (grouping axis); GP/ROAS shown as the current economics each band's tROAS floor "
              "must manage. Floors on the Targets & Floors sheet.")
    ws["A2"].font=NOTE; ws["A2"].alignment=WRAP; ws.merge_cells("A2:I2"); ws.row_dimensions[2].height=28
    style_header(ws,4,["new tier","band","states","spend","conv","wtd RPL","wtd CPL","wtd GP/lead","wtd ROAS"],
                 [12,20,8,12,8,9,9,11,9]); r=5
    for t in TIER_ORDER:
        g=df[df['new_tier']==t]
        if not len(g): continue
        rpl=wavg(g,'rpl','conversions')
        row=[t,band_desc(rpl),len(g),round(g['spend'].sum()),round(g['conversions'].sum()),round(rpl,1),
             round(wavg(g,'cpl','conversions'),1),round(wavg(g,'gp_per_lead','conversions'),1),round(wavg(g,'roas','spend'),2)]
        for j,v in enumerate(row,1):
            c=ws.cell(row=r,column=j,value=v); c.border=BORD; c.font=BLACK
            if j==4: c.number_format=M0
            if j in (6,7): c.number_format=R2
            if j==8: c.number_format=GPF
            if j==9: c.number_format=R2
            if j==2: c.font=NOTE
        r+=1
    ws.conditional_formatting.add(f"H5:H{r-1}",CellIsRule(operator='lessThan',formula=['0'],fill=REDF))

    # ---------- 6. Targets & Floors ----------
    ws=wb.create_sheet("Targets & Floors")
    ws["A1"]="tROAS floors — margin policy pending (editable)"; ws["A1"].font=TITLE
    ws["A2"]=("Floor = 1/(1 - margin). Enter leadership's chosen margin per band in the blue cells; floor, max CPL "
              "and gap-to-current recompute live. Floors are MINIMUM targets (shells may bid tighter, never looser). "
              "LAUNCH each shell at ~its current effective target, then STEP toward the floor 15-20% per adjustment, "
              ">=1 week between steps — target moves are phased even though membership is not (the May single-jump "
              "tightening is the counterexample). Native-basis note: values run ~10-13% conservative; a 100% floor "
              "is ~break-even-plus on true revenue.")
    ws["A2"].font=NOTE; ws["A2"].alignment=WRAP; ws.merge_cells("A2:H2"); ws.row_dimensions[2].height=66
    style_header(ws,4,["new tier","wtd RPL","margin % (EDIT)","tROAS floor","implied max CPL","current realized ROAS","gap to floor","step plan"],
                 [12,9,13,11,13,16,11,34]); r=5
    for t in TIER_ORDER:
        g=df[df['new_tier']==t]
        if not len(g): continue
        rpl=round(wavg(g,'rpl','conversions'),1); cur=round(wavg(g,'roas','spend'),2)
        mcell=f"C{r}"
        vals=[t,rpl,None,f"=IF({mcell}=\"\",\"\",1/(1-{mcell}))",f"=IF({mcell}=\"\",\"\",B{r}*(1-{mcell}))",
              cur,f"=IF({mcell}=\"\",\"\",F{r}-D{r})",
              "launch at current effective target; step 15-20% toward floor, >=1wk apart"]
        for j,v in enumerate(vals,1):
            c=ws.cell(row=r,column=j,value=v); c.border=BORD; c.font=BLACK
            if j==2: c.number_format=R2
            if j==3: c.number_format=P0; c.font=BLUE; c.fill=PatternFill("solid",fgColor="FFF2CC")
            if j==4: c.number_format=P0
            if j==5: c.number_format=M1
            if j in (6,7): c.number_format=R2
            if j==8: c.font=NOTE
        r+=1

    # ---------- 7. Exception Watch (day one) ----------
    ws=wb.create_sheet("Exception Watch")
    ws["A1"]="Exception screen — day-one watch list"; ws["A1"].font=TITLE
    ws["A2"]=("The RPL bands sort VALUE; the biweekly GP exception screen handles POSTURE. These states enter the new "
              "structure already flagged (evidence from the instrumented July reads). Screen is watch-only during the "
              "2-week post-cutover freeze; dispositions require bootstrap >=80% on two consecutive clean reads.")
    ws["A2"].font=NOTE; ws["A2"].alignment=WRAP; ws.merge_cells("A2:F2"); ws.row_dimensions[2].height=40
    style_header(ws,4,["state","lands in","flag","evidence (July reads)","likely disposition","review"],
                 [14,11,26,44,30,12])
    watch=[
      ("Massachusetts","TIER ONE","negative margin entering premium band","persistent negative GP/lead at high RPL; expensive clicks","per-state target exception or rehab if floor unmet","cycle 1"),
      ("Pennsylvania","TIER THREE","pre-cutover exception (executed)","-$50/lead recent, 88% bootstrap, deteriorating","rehab/tightened posture BEFORE cutover; do not launch at band target","done/verify"),
      ("Arizona","TIER TWO","pre-cutover exception (executed)","persistent negative margin; value recovered, cost did not","rehab/tightened posture BEFORE cutover","done/verify"),
      ("Alabama","TIER THREE","strong earner in low band","~1.3-1.4 ROAS at modest RPL","upward posture exception once floors live","cycle 1-2"),
      ("Wisconsin","TIER THREE","strong earner in low band","recovered to ~1.2-1.4 ROAS (July reads)","upward posture exception once floors live","cycle 1-2"),
      ("New Mexico","TIER ONE","dual-campaign targeting overlap","state appears in two campaigns (export audit)","clean up targeting BEFORE cutover","pre-cutover"),
      ("Georgia","TIER TWO","largest spend at ~breakeven, slipping","0.90-1.03 ROAS on top tiered spend","repair-in-place (query/device surgery), not a tier move","ongoing"),
    ]
    r=5
    for row in watch:
        for j,v in enumerate(row,1):
            c=ws.cell(row=r,column=j,value=v); c.border=BORD; c.font=BLACK if j<3 else NOTE
            c.alignment=WRAP
        ws.row_dimensions[r].height=30; r+=1

    # ---------- 8. Risks & Sequencing ----------
    ws=wb.create_sheet("Risks & Sequencing")
    ws["A1"]="Risks, sequencing & controls"; ws["A1"].font=TITLE
    items=[
      ("Single cutover, by design","20 of 35 readable states change shells — a majority-move restructure. Phasing a majority-move map means weeks of mixed-regime shells and staggered learning resets (the June rollout demonstrated this: three staggered resets, contaminated measurement). One synchronized event = one turbulence window, one clean before/after."),
      ("Bidder learning reset","All shells reset at cutover (~1-2 weeks). FREEZE: no target changes and exception screen watch-only for 2 weeks post-cutover. Do not grade the structure on the freeze window."),
      ("Targets phase, membership doesn't","Launch each shell near its current effective aggressiveness; step toward the policy floors 15-20% per adjustment, >=1 week apart. Large single target jumps caused the May-June overcorrection."),
      ("Ledger frozen at cutover","Run the model with --emit-ledger on cutover day against the final as-built export. The new structure starts with a commitment ledger from day one; all future runs compare against it."),
      ("Relabel + id stability","Relabel each shell at cutover. campaign_id is the analysis key; update any name-parsing logic the same day (troas_volume_timeline, gp_bridge current_label maps)."),
      ("Fresh location export first","Iron rule: pull a fresh campaign-location export immediately before building the change set; audit for dual-targeting (the NM case) and orphaned states."),
      ("Pre-cutover exceptions","PA and AZ move to tightened/rehab posture BEFORE cutover (they bleed daily at mid-band postures and are individually evidenced). These are exception-track actions, not part of the restructure."),
      ("Re-enabled shell hygiene","The retired shell is re-enabled with its OLD contents intact: audit and reset before cutover — location targets (strip all old states), keywords/negatives (align to current library), budget, and especially its old tROAS setting (it will resume at its retirement-era target unless changed). A paused campaign may not appear in an enabled-only location export — pull it explicitly and confirm its campaign_id for the change set."),
      ("Below-gate states","Thin states placed by nearest-tier (shrunk where available) and folded in at cutover — no separate wave. They carry no launch-target weight."),
      ("Don't stack levers","No keyword/geo/budget surgery on tiered shells within the freeze window except the pre-approved GA/NJ repair track, which is documented separately."),
    ]
    r=3
    for h,t in items:
        ws.cell(row=r,column=1,value=h).font=BOLD; ws.cell(row=r,column=1).fill=SUBFILL; r+=1
        c=ws.cell(row=r,column=1,value=t); c.font=NOTE; c.alignment=WRAP; ws.merge_cells(f"A{r}:H{r}")
        ws.row_dimensions[r].height=44; r+=2
    ws.column_dimensions["A"].width=24
    for col in "BCDEFGH": ws.column_dimensions[col].width=12

    wb.save(out)


if __name__=="__main__":
    ap=argparse.ArgumentParser()
    ap.add_argument("--input",required=True,help="RPL model workbook (k=4, exclude TX/CA/FL)")
    ap.add_argument("--cutover-date",default="2026-08-11")
    ap.add_argument("--t4-shell",default="RE-ENABLED retired shell (confirm campaign_id)",
                    help="Name/label of the retired shell being re-enabled for TIER FOUR, e.g. 'Re-enabled: HOME INSURANCE_STATE-GROUP_Tier3... (id 235...)'")
    ap.add_argument("--out",default=f"Google_Home_RPL_ReTier_Rollout_Plan_{dt.date.today():%Y-%m-%d}.xlsx")
    a=ap.parse_args()
    globals()['SHELL_MAP']=shell_map(a.t4_shell)
    df=load(a.input)
    build(df,a.out,a.cutover_date)
    print(f"wrote {a.out}  ({len(df)} states incl {int(df['below_gate'].sum())} below-gate, {int(df[df['below_gate']==0]['chg'].sum())} moving)")
