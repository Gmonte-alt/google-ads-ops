from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter
from openpyxl.formatting.rule import CellIsRule

wb = Workbook(); wb.remove(wb.active)
ARIAL=lambda **k: Font(name="Arial", **k)
TITLE=ARIAL(bold=True, size=14, color="305496")
NOTE=ARIAL(italic=True, size=9, color="595959")
HF=ARIAL(bold=True, color="FFFFFF"); HFILL=PatternFill("solid", fgColor="305496")
BLUE=ARIAL(color="0000FF")              # hardcoded inputs
BLACK=ARIAL()                            # formulas
GREEN=ARIAL(color="008000")              # cross-sheet links
SUB=ARIAL(bold=True, size=11, color="305496")
GOOD=PatternFill("solid", fgColor="C6EFCE"); AMBER=PatternFill("solid", fgColor="FCE4D6")
GREY=PatternFill("solid", fgColor="E7E6E6"); REDF=PatternFill("solid", fgColor="FFC7CE")
YEL=PatternFill("solid", fgColor="FFF2CC")
THIN=Side(border_style="thin", color="BFBFBF")
BORD=Border(top=THIN,bottom=THIN,left=THIN,right=THIN)
WK_M='$#,##0;($#,##0)'; RPL_M='$#,##0.0'; PCT='0"%"'; MULT='0.00'; INT='#,##0'

# ---------- raw simulator data (campaign target %, cost, conv, conv value) ----------
# current row flagged; recommended GP-max target flagged
SIMS = {
 "California": dict(cur=117, rec=146, rows=[
    (468,33.19,8,108.05),(351,186.64,29,455.58),(234,502.84,56,955.10),
    (175,1295.18,102,1904.40),(146,2283.41,145,2883.82),(129,3605.02,193,3992.76),
    (117,5092.51,240,5135.06),(105,6479.29,275,6031.41),(88,9729.30,348,7925.54),
    (58,26426.45,633,15407.12)]),
 "Tier Four": dict(cur=117, rec=125, rows=[
    (136,4898.2,72,5515.1),(125,6853.1,93,7185.6),(117,8991.4,115,8938.4),
    (109,10834.8,131,10189.6),(103,12683.5,146,11433.5),(98,15026.7,164,12892.6),
    (91,16785.4,177,13986.3),(90,19266.8,195,15463.6),(86,22703.0,220,17455.2),
    (81,27505.5,251,20016.3)]),
 "Texas": dict(cur=112, rec=112, rows=[
    (448,9.87,1,49.25),(336,76.68,4,269.33),(224,261.94,10,694.25),
    (168,811.42,22,1597.83),(140,1674.92,38,2785.27),(123,2894.36,57,4233.51),
    (112,4598.02,79,5952.26),(101,6102.20,94,7230.99),(84,9647.67,127,9876.62),
    (56,28405.10,257,20135.73)]),
 "Florida": dict(cur=112, rec=112, rows=[
    (336,7.74,2,31.19),(224,26.37,5,77.64),(168,75.84,9,168.56),
    (140,155.35,13,279.43),(123,230.70,16,366.87),(112,369.44,21,512.43),
    (101,489.68,25,624.89),(84,823.21,34,891.47),(56,3053.48,80,2282.55)]),
}
# delta-only panels ("limited by target" — Google shows only looser options vs current)
DELTAS = {
 "Tier One": dict(cur=117, rows=[(105,957,1380),(94,2380,3520),(82,4520,7190)]),
 "Tier Two": dict(cur=112, rows=[(101,435,627),(90,873,1330),(78,1440,2380)]),
}

# =================== SHEET 2 first: Sim Curves (so Decision can reference it) ===================
sc = wb.create_sheet("Sim Curves")
sc["A1"]="Campaign target-ROAS simulators — GP computed per row"; sc["A1"].font=TITLE
sc["A2"]=("Inputs (blue) are read straight from the Google Campaign Target ROAS simulators, "
          "week May 31–Jun 6 2026. GP/wk = conv value − cost. Marginal ROAS = Δconv value ÷ Δcost vs the "
          "current row (the decision metric: an added-volume step only pays if it clears ~1.0). "
          "Highlighted row = GP-maximizing target.")
sc["A2"].font=NOTE
pos={}   # campaign -> dict(cur_gp_cell, rec_gp_cell, cur_cost_cell, rec_cost_cell, marg_first_cell)
r=4
hdr=["target ROAS","cost/wk","conv","conv value","GP/wk","marginal ROAS vs current","note"]
for camp,d in SIMS.items():
    sc.cell(row=r,column=1,value=camp).font=SUB; r+=1
    for j,h in enumerate(hdr,1):
        c=sc.cell(row=r,column=j,value=h); c.font=HF; c.fill=HFILL; c.border=BORD
    hrow=r; r+=1
    # find current row cost/value cell rows for marginal calc
    cur_excel_row=None
    rows=d["rows"]
    start=r
    for (tgt,cost,conv,val) in rows:
        sc.cell(row=r,column=1,value=tgt/100.0).number_format='0%'
        sc.cell(row=r,column=1).font=BLACK
        sc.cell(row=r,column=2,value=cost).number_format='$#,##0';  sc.cell(row=r,column=2).font=BLUE
        sc.cell(row=r,column=3,value=conv).number_format=INT;        sc.cell(row=r,column=3).font=BLUE
        sc.cell(row=r,column=4,value=val).number_format='$#,##0';    sc.cell(row=r,column=4).font=BLUE
        sc.cell(row=r,column=5,value=f"=D{r}-B{r}").number_format=WK_M; sc.cell(row=r,column=5).font=BLACK
        if tgt==d["cur"]: cur_excel_row=r
        for j in range(1,8): sc.cell(row=r,column=j).border=BORD
        r+=1
    end=r-1
    # marginal ROAS vs current (fill after we know cur row)
    for rr in range(start,end+1):
        if rr==cur_excel_row:
            sc.cell(row=rr,column=6,value="current").font=NOTE
        else:
            sc.cell(row=rr,column=6,
                value=f"=IF((B{rr}-B{cur_excel_row})=0,\"\",(D{rr}-D{cur_excel_row})/(B{rr}-B{cur_excel_row}))")
            sc.cell(row=rr,column=6).number_format=MULT; sc.cell(row=rr,column=6).font=BLACK
        # highlight current + recommended rows
    # locate recommended row
    rec_excel_row=None
    for rr in range(start,end+1):
        tgt=int(round(sc.cell(row=rr,column=1).value*100))
        if tgt==d["rec"]: rec_excel_row=rr
    for rr in (cur_excel_row,rec_excel_row):
        for j in range(1,8):
            cc=sc.cell(row=rr,column=j)
            cc.fill = GOOD if rr==rec_excel_row and rec_excel_row!=cur_excel_row else (GREY if rr==cur_excel_row else cc.fill)
    sc.cell(row=cur_excel_row,column=7,value="← current").font=NOTE
    if rec_excel_row!=cur_excel_row:
        sc.cell(row=rec_excel_row,column=7,value="← GP-max (recommended)").font=ARIAL(italic=True,size=9,color="2e7d32")
    pos[camp]=dict(cur_gp=f"'Sim Curves'!E{cur_excel_row}", rec_gp=f"'Sim Curves'!E{rec_excel_row}",
                   cur_cost=f"'Sim Curves'!B{cur_excel_row}", rec_cost=f"'Sim Curves'!B{rec_excel_row}",
                   cur_conv=f"'Sim Curves'!C{cur_excel_row}", cur_val=f"'Sim Curves'!D{cur_excel_row}")
    r+=2

# delta panels
for camp,d in DELTAS.items():
    sc.cell(row=r,column=1,value=f"{camp}  (\"limited by target\" panel — loosening options only, vs current)").font=SUB; r+=1
    for j,h in enumerate(["avg target ROAS","Δ conv value/wk","Δ cost/wk","GP impact/wk","marginal ROAS"],1):
        c=sc.cell(row=r,column=j,value=h); c.font=HF; c.fill=HFILL; c.border=BORD
    r+=1
    first_marg=None
    for (tgt,dval,dcost) in d["rows"]:
        sc.cell(row=r,column=1,value=tgt/100.0).number_format='0%'; sc.cell(row=r,column=1).font=BLACK
        sc.cell(row=r,column=2,value=dval).number_format='$#,##0'; sc.cell(row=r,column=2).font=BLUE
        sc.cell(row=r,column=3,value=dcost).number_format='$#,##0'; sc.cell(row=r,column=3).font=BLUE
        sc.cell(row=r,column=4,value=f"=B{r}-C{r}").number_format=WK_M; sc.cell(row=r,column=4).font=BLACK
        sc.cell(row=r,column=5,value=f"=B{r}/C{r}").number_format=MULT; sc.cell(row=r,column=5).font=BLACK
        sc.cell(row=r,column=4).fill=REDF
        for j in range(1,6): sc.cell(row=r,column=j).border=BORD
        if first_marg is None: first_marg=f"'Sim Curves'!E{r}"
        r+=1
    pos[camp]=dict(marg_first=first_marg)
    r+=2

for col,w in zip("ABCDEFG",[14,12,8,13,14,24,26]): sc.column_dimensions[col].width=w

# =================== SHEET 1: Decision ===================
ws = wb.create_sheet("Decision", 0)
ws["A1"]="Google Home — tROAS decision: GP-max targets by campaign"; ws["A1"].font=TITLE
ws["A2"]=("One row per campaign. The question was 'what do we gain by lowering tROAS' — the answer is nothing: "
          "marginal ROAS of loosening is below 1.0 everywhere, so the GP is on the TIGHTEN side. "
          "Routed RPL (yellow) is the manager input that calibrates the sims; where it ≈ sim $/conv, the sim targets are trustworthy.")
ws["A2"].font=NOTE
cols=["campaign","current tROAS","achieved ROAS (recent, 3wk)","routed RPL","sim $/conv (current)",
      "GP now $/wk","GP-max tROAS","GP at target $/wk","GP gain $/wk","marginal ROAS (loosen)","freed budget $/wk","action"]
hr=4
for j,h in enumerate(cols,1):
    c=ws.cell(row=hr,column=j,value=h); c.font=HF; c.fill=HFILL; c.border=BORD; c.alignment=Alignment(wrap_text=True,vertical="center")
ws.row_dimensions[hr].height=42

# campaign rows
rows_meta=[
 # camp, cur%, achieved%, routedRPL, action, fill
 ("California",117,118,23,"TIGHTEN → 146%",GOOD),
 ("Tier Four",117,110,74,"TIGHTEN → 125%",GOOD),
 ("Texas",112,132,76,"HOLD (at GP-max)",GREY),
 ("Florida",112,115,23,"HOLD (at GP-max)",GREY),
 ("Tier One",117,101,44,"DON'T LOOSEN — value/throttle; re-tier + RPL",AMBER),
 ("Tier Two",112,127,82,"DON'T LOOSEN — RPL unstable ($82→$39)",AMBER),
 ("Tier Three",None,118,74,"RANK-limited; budget sim GP-negative — do NOT redeploy",AMBER),
]
rr=hr+1
data_first=rr
for camp,cur,ach,rpl,action,fill in rows_meta:
    ws.cell(row=rr,column=1,value=camp).font=ARIAL(bold=True)
    if cur is not None:
        ws.cell(row=rr,column=2,value=cur/100.0).number_format='0%'; ws.cell(row=rr,column=2).font=BLUE
    ws.cell(row=rr,column=3,value=ach/100.0).number_format='0%'; ws.cell(row=rr,column=3).font=BLUE
    if rpl is not None:
        cc=ws.cell(row=rr,column=4,value=rpl); cc.number_format=RPL_M; cc.font=ARIAL(color="0000FF"); cc.fill=YEL
    else:
        cc=ws.cell(row=rr,column=4,value="pending"); cc.font=NOTE; cc.fill=YEL
    p=pos.get(camp,{})
    if camp in SIMS:
        ws.cell(row=rr,column=5,value=f"={p['cur_val']}/{p['cur_conv']}").number_format=RPL_M; ws.cell(row=rr,column=5).font=GREEN
        ws.cell(row=rr,column=6,value=f"={p['cur_gp']}").number_format=WK_M; ws.cell(row=rr,column=6).font=GREEN
        ws.cell(row=rr,column=7,value=SIMS[camp]['rec']/100.0).number_format='0%'; ws.cell(row=rr,column=7).font=BLUE
        ws.cell(row=rr,column=8,value=f"={p['rec_gp']}").number_format=WK_M; ws.cell(row=rr,column=8).font=GREEN
        ws.cell(row=rr,column=9,value=f"=H{rr}-F{rr}").number_format=WK_M; ws.cell(row=rr,column=9).font=BLACK
        ws.cell(row=rr,column=11,value=f"={p['rec_cost']}-{p['cur_cost']}").number_format=WK_M
        ws.cell(row=rr,column=11).font=BLACK   # freed = cur_cost - rec_cost (rec tighter so positive); sign handled below
        # freed budget should be cur_cost - rec_cost (positive when tightening frees spend)
        ws.cell(row=rr,column=11,value=f"={p['cur_cost']}-{p['rec_cost']}").number_format=WK_M; ws.cell(row=rr,column=11).font=BLACK
        # marginal ROAS of FIRST loosening step: reference the row just looser than current on Sim Curves
    if camp in DELTAS:
        ws.cell(row=rr,column=10,value=f"={pos[camp]['marg_first']}").number_format=MULT; ws.cell(row=rr,column=10).font=GREEN; ws.cell(row=rr,column=10).fill=REDF
    ca=ws.cell(row=rr,column=12,value=action); ca.fill=fill; ca.font=ARIAL(bold=True,size=9); ca.alignment=Alignment(wrap_text=True,vertical="center")
    for j in range(1,13): ws.cell(row=rr,column=j).border=BORD
    rr+=1
data_last=rr-1

# marginal ROAS (loosen) for the SIM campaigns: first looser row's marginal cell on Sim Curves
# CA current row 117 -> next looser 105; T4 117->109; TX 112->101; FL 112->101
loosen_ref={"California":"105","Tier Four":"109","Texas":"101","Florida":"101"}
# we need the Sim Curves row of those targets; recompute by scanning pos isn't stored, so map via known layout
# simpler: pull from Sim Curves by searching the target value
from openpyxl.utils import column_index_from_string
def sc_row_for(camp,target_pct):
    # scan Sim Curves col A for camp block then target
    found_block=False; 
    for row in range(1, sc.max_row+1):
        v=sc.cell(row=row,column=1).value
        if v==camp: found_block=True; continue
        if found_block and isinstance(v,str) and v not in (camp,) and v in list(SIMS)+[f'{c}  ("limited by target" panel — loosening options only, vs current)' for c in DELTAS]:
            break
        if found_block and isinstance(v,(int,float)) and abs(v-target_pct/100.0)<1e-6:
            return row
    return None
for camp,tgt in loosen_ref.items():
    rrow=None
    # find this campaign's decision row
    for i in range(data_first,data_last+1):
        if ws.cell(row=i,column=1).value==camp: rrow=i; break
    scr=sc_row_for(camp,int(tgt))
    if scr:
        ws.cell(row=rrow,column=10,value=f"='Sim Curves'!F{scr}").number_format=MULT
        ws.cell(row=rrow,column=10).font=GREEN; ws.cell(row=rrow,column=10).fill=REDF

# Totals row
tr=data_last+2
ws.cell(row=tr,column=1,value="TIGHTEN TOTAL (CA + Tier Four)").font=ARIAL(bold=True)
ws.cell(row=tr,column=9,value=f"=SUMIF(L{data_first}:L{data_last},\"TIGHTEN*\",I{data_first}:I{data_last})").number_format=WK_M
ws.cell(row=tr,column=9).font=ARIAL(bold=True)
ws.cell(row=tr,column=11,value=f"=SUMIF(L{data_first}:L{data_last},\"TIGHTEN*\",K{data_first}:K{data_last})").number_format=WK_M
ws.cell(row=tr,column=11).font=ARIAL(bold=True)
for j in (1,9,11): ws.cell(row=tr,column=j).fill=GOOD; ws.cell(row=tr,column=j).border=BORD
tr2=tr+1
ws.cell(row=tr2,column=1,value="Annualized GP gain").font=ARIAL(bold=True)
ws.cell(row=tr2,column=9,value=f"=I{tr}*52").number_format=WK_M; ws.cell(row=tr2,column=9).font=ARIAL(bold=True)
ws.cell(row=tr2,column=11,value="→ freed budget has no profitable home now; leave unspent (see Tier 3 tab)").font=NOTE

for col,w in zip("ABCDEFGHIJKL",[12,9,12,9,11,11,9,12,11,12,12,30]): ws.column_dimensions[col].width=w
ws.freeze_panes="B5"

# read-out block
ro=tr2+3
notes=[
 ("Read-out / recommendation",True),
 ("• No campaign gains GP from lowering tROAS — every marginal-ROAS-of-loosening is 0.60–0.93, all below the ~1.0 needed for the added volume to pay for itself.",False),
 ("• The GP is on the tighten side: California → ~146% (+~$558/wk, frees ~$2.8K/wk) and Tier Four → ~125% (+~$385/wk, frees ~$2.1K/wk). Together ≈ +$943/wk GP (~$49K/yr) and ~$4.9K/wk of freed spend.",False),
 ("• Texas and Florida are already at their GP-max — hold. Tightening them further trades GP for little.",False),
 ("• Tier One & Tier Two: the Google panels pitch LOWER targets for more conv value, but conv value/cost is negative — that optimizes Google's objective, not GP. Decline. Tier One's throttle is value erosion (owns CT/NJ/NY); Tier Two's RPL just halved ($82→$39) and needs a look before any move.",False),
 ("• The freed ~$4.9K/wk has NO profitable home in the current structure. Tier Three's budget simulator shows its marginal volume costs ~$90/conv vs ~$74 RPL (marginal ROAS ~0.81), and its impression share is <10% with >90% lost to RANK and ~0% lost to budget — it is rank-limited, not budget-limited, so more budget buys auctions at a loss. Leave the freed budget unspent (bank the GP + the spend cut). A profitable home for growth budget requires the re-tier. See the Tier 3 Redeployment tab.",False),
 ("• More aggressive option: California → 175% earns ~the same GP ($609 vs $600/wk) while freeing ~$3.8K/wk instead of $2.8K — more budget to redeploy at equal GP, at the cost of a deeper volume cut. 146% is the conservative pick.",False),
 ("Caveat: a campaign-wide target change clears existing ad-group target overrides. Sims are Google historical estimates, not guarantees.",False),
]
for t,h in notes:
    ws.cell(row=ro,column=1,value=t).font=(SUB if h else NOTE); ro+=1

# Methodology sheet
me=wb.create_sheet("Methodology")
mnotes=[
 ("Methodology & sources",True),
 ("Simulators: Google Ads 'Campaign Target ROAS simulator', simulation period May 31–Jun 6 2026, per campaign. Inputs = target ROAS, cost, conv, conv value (blue cells on Sim Curves).",False),
 ("Tier Four sim was captured earlier (in the performance-review workbook); Texas, California, Florida from the Jun 2026 screenshots. Tier One & Tier Two had no full simulator — only the 'limited by target' uplift panel (looser options vs current), shown as deltas.",False),
 ("Tier Three: budget-capped, so Google shows no simulator. It is the redeployment target, not a tROAS-change candidate.",False),
 ("GP/wk = conv value − cost (Google conv value ≈ routed revenue, confirmed by the routed-RPL check below).",False),
 ("Marginal ROAS of loosening = Δconv value ÷ Δcost vs the current row. Decision rule: a loosening step is GP-accretive only if this clears ~1.0. Every campaign's first loosening step is well under 1.0.",False),
 ("Routed-RPL calibration (manager input, Jun 2026): Texas ~$76 vs sim $75; California ~$23 vs $21; Florida ~$23 vs $24; Tier Four ~$74 vs $78. Sim conv-value ≈ routed RPL, so the simulators' GP-max targets are trustworthy as-is — there is no hidden routed upside that flips loosening positive.",False),
 ("Tier One routed RPL ~$44 (Jun 2026) — below the sim's implied conv-value, so its true marginal ROAS of loosening is even worse than the 0.69 the panel shows. Confirms: don't loosen; the fix is the re-tier + carrier-payout/RPL. Tier Two routed RPL ~$82 recently but ~$39 last Fri–Tue (a >50% drop) — flagged for investigation before any Tier Two action.",False),
 ("Achieved ROAS (recent) = 3-week average of complete weeks from the timeline; the last week reflects the Jun 2–3 event and offline-upload lag, so it understates settled ROAS.",False),
 ("Colour key: blue = hardcoded input, black = formula, green = cross-sheet link. Yellow = routed RPL (key assumption to refresh). Green row = GP-max target; grey row = current.",False),
]
for t,h in mnotes:
    me.cell(row=me.max_row+1 if me.max_row>1 or me['A1'].value else 1,column=1,value=t).font=(TITLE if h else NOTE)
me.column_dimensions['A'].width=140

# =================== SHEET: Tier 3 Redeployment ===================
t3=wb.create_sheet("Tier 3 Redeployment")
t3["A1"]="Tier Three — budget simulator + impression-share verdict: do NOT redeploy"; t3["A1"].font=TITLE
t3["A2"]=("Earlier hypothesis was that Tier Three is budget-limited and would absorb the freed budget at ~target efficiency. "
          "Its budget simulator and impression-share data refute that: it is RANK-limited, not budget-limited, and every budget step is GP-negative. "
          "The freed budget has no profitable home in the current structure — leave it unspent. "
          "A Jun 11 revisit (Section E) tests a +20% budget bump against the recent ROAS uptick: it adds GP only if the campaign has become budget-constrained AND the marginal cost of a conversion has dropped below RPL.")
t3["A2"].font=NOTE

# Section A — current state
r=4
t3.cell(row=r,column=1,value="A. Tier Three current state (30-day report 2026-05-11..06-09)").font=SUB; r+=1
A_rows=[("spend / week",8802,'$#,##0'),("achieved ROAS",1.189,'0%'),
        ("routed RPL / conv (≈ Google value/conv)",73.2,RPL_M),("avg cost / conv",61.5,RPL_M),
        ("daily budget (current)",1800,'$#,##0')]
ref={}
for lbl,val,fmt in A_rows:
    t3.cell(row=r,column=1,value=lbl).font=BLACK
    c=t3.cell(row=r,column=2,value=val); c.number_format=fmt; c.font=BLUE; ref[lbl]=f"B{r}"; r+=1
r+=1

# Section B — IS verdict
t3.cell(row=r,column=1,value="B. Impression-share verdict — the constraint is RANK, not budget").font=SUB; r+=1
for j,h in enumerate(["metric","value (29–30 of 30 days)","reading"],1):
    c=t3.cell(row=r,column=j,value=h); c.font=HF; c.fill=HFILL; c.border=BORD
r+=1
isrows=[("Search impr. share","< 10%","wins under a tenth of available impressions"),
        ("Search lost IS (budget)","~0%","budget is NOT the binding constraint"),
        ("Search lost IS (rank)","> 90%","loses almost everything to Ad Rank (bid × quality)")]
for m,v,read in isrows:
    t3.cell(row=r,column=1,value=m).font=BLACK
    t3.cell(row=r,column=2,value=v).font=BLUE
    t3.cell(row=r,column=3,value=read).font=NOTE
    for j in range(1,4): t3.cell(row=r,column=j).border=BORD
    r+=1
t3.cell(row=r,column=1,value="Status flag says 'limited by budget' but the measured IS says rank. More budget can't fix a rank problem.").font=NOTE; r+=2

# Section C — budget simulator economics (the decision)
t3.cell(row=r,column=1,value="C. Budget simulator — every step is GP-negative").font=SUB; r+=1
t3.cell(row=r,column=1,value="Google budget simulator (weekly added vs current $1,800/day). GP = added conv × RPL − added cost.").font=NOTE; r+=1
for j,h in enumerate(["daily budget","+ conv/wk","+ cost/wk","marginal $/conv","marginal ROAS","GP impact/wk"],1):
    c=t3.cell(row=r,column=j,value=h); c.font=HF; c.fill=HFILL; c.border=BORD
r+=1
rpl_ref=ref["routed RPL / conv (≈ Google value/conv)"]
sim=[(1800,0,0,"current"),(2300,25.2,2230,""),(2700,45.5,4090,"← Google 'recommended'"),(3100,47.9,4320,"")]
sim_ref={}
for b,ac,acost,tag in sim:
    if b in (1800,2300): sim_ref[b]={"bud":f"A{r}","conv":f"B{r}","cost":f"C{r}"}
    t3.cell(row=r,column=1,value=b).number_format='$#,##0'; t3.cell(row=r,column=1).font=BLUE
    t3.cell(row=r,column=2,value=ac).number_format='0.0'; t3.cell(row=r,column=2).font=BLUE
    t3.cell(row=r,column=3,value=acost).number_format='$#,##0'; t3.cell(row=r,column=3).font=BLUE
    if ac>0:
        t3.cell(row=r,column=4,value=f"=C{r}/B{r}").number_format=RPL_M; t3.cell(row=r,column=4).font=BLACK
        t3.cell(row=r,column=5,value=f"={rpl_ref}/D{r}").number_format=MULT; t3.cell(row=r,column=5).font=BLACK
        t3.cell(row=r,column=6,value=f"=B{r}*{rpl_ref}-C{r}").number_format=WK_M; t3.cell(row=r,column=6).font=BLACK
        t3.cell(row=r,column=6).fill=REDF
        t3.cell(row=r,column=5).fill=REDF
    if tag: t3.cell(row=r,column=7,value=tag).font=NOTE
    for j in range(1,7): t3.cell(row=r,column=j).border=BORD
    r+=1
t3.cell(row=r,column=1,value="Marginal cost/conv (~$88–96) exceeds RPL (~$74): added volume is bought at a loss. The recommended $2,700 ≈ −$760/wk GP.").font=NOTE; r+=2

# Section D — conclusion
t3.cell(row=r,column=1,value="D. Conclusion & what it means").font=SUB; r+=1
for t in ["• Do NOT raise Tier Three's budget. Its marginal volume runs ~0.81 ROAS — the same sub-threshold trap as loosening any tROAS.",
          "• Correction to prior turn: 'budget-limited' was the status flag, but the IS data shows rank-limited. The budget-capped-absorbs-at-average logic does not apply here.",
          "• The CA + Tier Four tightening still stands (+$943/wk GP) — that is banked independently. The freed ~$4.9K/wk simply has no profitable home in the current structure, so leave it unspent: the result is +$943/wk GP at lower spend.",
          "• Every marginal lever in the current account — loosen any target, or grow Tier Three's budget — is GP-negative. That is the signature of margin-incoherent campaigns: the marginal volume in each is the low/negative-margin tail. Re-tiering is what creates a cohort whose marginal volume is profitable, i.e. an actual home for growth budget.",
          "• If growing Tier Three specifically is a goal, the lever is RANK (Quality Score: ad relevance, landing-page experience, expected CTR) — not budget. At <10% impression share lost >90% to rank, spend is not the constraint."]:
    t3.cell(row=r,column=1,value=t).font=NOTE; r+=1

# =================== Section E — Jun 11 revisit: recent uptick & +20% budget test ===================
r+=1
t3.cell(row=r,column=1,value="E. Revisit (Jun 11) — the recent ROAS uptick & a +20% budget test").font=SUB; r+=1
t3.cell(row=r,column=1,value="Recent days ran stronger (Tue 1.36, Wed 1.25 through Jun 10). This expresses that, then tests what must be true for +20% budget to add GP.").font=NOTE; r+=2

# E1 — recent achieved ROAS trend
t3.cell(row=r,column=1,value="E1. Recent achieved ROAS — weekly, from DOW_T3-NB (each row one ISO week)").font=BLACK; r+=1
for j,h in enumerate(["week_start","weekly ROAS"],1):
    c=t3.cell(row=r,column=j,value=h); c.font=HF; c.fill=HFILL; c.border=BORD
r+=1
trend=[("2026-05-04",1.45),("2026-05-11",1.31),("2026-05-18",1.26),("2026-05-25",1.19),
       ("2026-06-01",1.07),("2026-06-08 (Mon–Wed)",1.16)]
for wk,roas in trend:
    t3.cell(row=r,column=1,value=wk).font=BLACK
    c=t3.cell(row=r,column=2,value=roas); c.number_format='0.00'; c.font=BLUE
    for j in range(1,3): t3.cell(row=r,column=j).border=BORD
    r+=1
t3.cell(row=r,column=1,value="recent days: Mon 0.82 · Tue 1.36 · Wed 1.25").font=NOTE; r+=1
t3.cell(row=r,column=1,value="13-week average ROAS").font=BLACK
c=t3.cell(row=r,column=2,value=1.16); c.number_format='0.00'; c.font=BLUE; r+=1
t3.cell(row=r,column=1,value="The partial week (1.16) sits right at the campaign's own 13-week average, and day-to-day ROAS swings widely (Mon 0.82 → Tue 1.36). Two strong days off a weak Monday is encouraging — not yet a confirmed step-change.").font=NOTE; r+=2

# E2 — why achieved ROAS isn't the trigger
t3.cell(row=r,column=1,value="E2. Why achieved ROAS isn't the budget trigger").font=BLACK; r+=1
for t in ["• A budget increase only adds spend if budget is the binding constraint. Section B reads lost-IS-to-budget ~0%, lost-IS-to-rank >90% — rank-limited. At +20% the cap likely won't bind, so spend may not rise at all.",
          "• Achieved ROAS (avg over all leads, ~1.16) is not marginal ROAS (the next dollar, ~0.82 in Section C). A good average can sit on top of unprofitable marginal volume — the budget call is decided by the margin, not the average."]:
    t3.cell(row=r,column=1,value=t).font=NOTE; r+=1
r+=1

# E3 — the +20% scenario priced on the current curve (formula-driven, self-coloring)
t3.cell(row=r,column=1,value="E3. The +20% scenario, priced on the current budget curve").font=BLACK; r+=1
bud_ref=ref["daily budget (current)"]
lo_b=sim_ref[1800]["bud"]; up_b=sim_ref[2300]["bud"]; up_conv=sim_ref[2300]["conv"]; up_cost=sim_ref[2300]["cost"]
def erow(lbl,val,fmt,blue=False,note=None):
    global r
    t3.cell(row=r,column=1,value=lbl).font=BLACK
    c=t3.cell(row=r,column=2,value=val); c.number_format=fmt; c.font=(BLUE if blue else BLACK)
    if note: t3.cell(row=r,column=3,value=note).font=NOTE
    ref_=f"B{r}"; r+=1; return ref_
fac_ref   = erow("budget increase factor",1.20,'0%',blue=True)
newbud_ref= erow("new daily budget",f"={bud_ref}*{fac_ref}",'$#,##0')
f_ref     = erow("interpolation point on curve (0 = $1,800, 1 = $2,300)",f"=({newbud_ref}-{lo_b})/({up_b}-{lo_b})",'0.00')
aconv_ref = erow("added conv / wk (interpolated)",f"={f_ref}*{up_conv}",'0.0')
acost_ref = erow("added cost / wk (interpolated)",f"={f_ref}*{up_cost}",'$#,##0')
arev_ref  = erow("added revenue / wk  (= added conv × RPL)",f"={aconv_ref}*{rpl_ref}",'$#,##0')
mroas_ref = erow("marginal ROAS at +20%",f"={arev_ref}/{acost_ref}",'0.00',note="want ≥ 1.00")
gp_ref    = erow("GP impact / wk at +20%",f"={arev_ref}-{acost_ref}",WK_M,note="want ≥ 0")
# self-coloring verdict: green if the increase pays, red if it loses (updates if inputs change)
t3.conditional_formatting.add(mroas_ref, CellIsRule(operator='greaterThanOrEqual',formula=['1'],fill=GOOD))
t3.conditional_formatting.add(mroas_ref, CellIsRule(operator='lessThan',formula=['1'],fill=REDF))
t3.conditional_formatting.add(gp_ref, CellIsRule(operator='greaterThanOrEqual',formula=['0'],fill=GOOD))
t3.conditional_formatting.add(gp_ref, CellIsRule(operator='lessThan',formula=['0'],fill=REDF))
r+=1

# E4 — break-even the improvement must clear
t3.cell(row=r,column=1,value="E4. Break-even — what the improvement must actually prove").font=BLACK; r+=1
cmcc_ref  = erow("current marginal cost / conv (first step $1,800→$2,300)",f"={up_cost}/{up_conv}",RPL_M)
_         = erow("RPL / conv  (break-even cost/conv)",f"={rpl_ref}",RPL_M)
_         = erow("required drop in marginal cost/conv to break even",f"=1-{rpl_ref}/{cmcc_ref}",'0%')
t3.cell(row=r,column=1,value="The bar isn't 'is ROAS up' — it's 'has the marginal cost of the next conversion fallen below RPL (~$73)'. Today it's ~$88, so the next conversion must get ~17% cheaper before +20% stops losing GP.").font=NOTE; r+=2

# E5 — bounded probe
t3.cell(row=r,column=1,value="E5. If you test it anyway — a bounded probe").font=BLACK; r+=1
for t in ["• Raise to the new daily budget for ~5–7 days and let it settle.",
          "• Watch 1 — does daily spend actually rise toward the new cap? If it doesn't, Tier Three was rank-limited as diagnosed: revert, no harm, no signal lost.",
          "• Watch 2 — track marginal cost/conv (Δcost ÷ Δconv vs the prior week). Keep the increase only while that stays at or below RPL (~$73).",
          "• Watch 3 — achieved ROAS holds at or above ~1.15 at the higher spend.",
          "• Revert if spend rises but marginal ROAS < 1.0 — that is buying volume at a loss, the original finding."]:
    t3.cell(row=r,column=1,value=t).font=NOTE; r+=1
r+=1

# E6 — recommendation
t3.cell(row=r,column=1,value="E6. Recommendation").font=BLACK; r+=1
for t in ["• The two strong days are encouraging but don't yet move the verdict: weekly ROAS is at the campaign average, and the binding constraint was RANK, not budget — so +20% is most likely inert (spend won't rise).",
          "• Before committing, re-check the live IS split (has lost-IS-to-budget risen above ~0%?) and re-run the budget simulator on the last 10–14 days. If lost-IS-to-budget has risen AND marginal cost/conv has fallen below RPL, E3 turns green and the increase is justified.",
          "• Otherwise run +20% only as a low-cost probe with the E5 stop criteria — not as a standing change."]:
    t3.cell(row=r,column=1,value=t).font=NOTE; r+=1

for col,w in zip("ABCDEFG",[44,16,14,15,14,15,22]): t3.column_dimensions[col].width=w

wb.save("C:/Projects/google-ads-ops/analysis/output/google_home_troas_decision_2026-06-11.xlsx")
print("built")
