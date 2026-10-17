"""
rpl_decomposition.py — Analysis 2: RPL erosion attribution for Google Home tier campaigns.

Question: why has realized revenue-per-lead eroded in the tiered campaigns since ~March,
while Texas (single-state, never re-tiered) held flat?

Decomposition per campaign (monthly, March 2026 -> now), using account-wide per-state RPL
v(s,m) as each state's intrinsic lead value and w(c,s,m) as the campaign's conversion mix:

    actual(m)       = campaign revenue / conversions           (measured)
    recon(m)        = SUM_s w(m,s) * v(m,s)                    (state-explained RPL)
    fixed_basket(m) = SUM_s w(BASE,s) * v(m,s)                 (March basket, today's values)
    residual(m)     = actual(m) - recon(m)                     (campaign-specific quality)

    H2 state decay  = fixed_basket(now) - fixed_basket(base)
    H1 mix shift    = recon(now) - fixed_basket(now)
    H3 quality      = residual(now) - residual(base)
    identity: H1 + H2 + H3 == actual(now) - actual(base)

TEXAS is the built-in control: single state => H1 == 0 by construction; its H2 is the
Texas state's own value trend; residual ~ 0.

H3 corroboration at keyword grain: class-mix shift (flagship carrier-brand vs home-intent)
per campaign per month, plus a class-mix counterfactual RPL (March class weights held).
NOTE: geo grain (state) and keyword grain (query) cannot be joined lead-level in this DB,
so H3 is measured as (a) the residual above and (b) the keyword-grain mix evidence.

House rules honored: DB opened READ-ONLY (do not run during a sync); geo grain sums
devices (device <> 'ALL'); revenue = platform conversion_value; no lag exclusion.

Usage:
  python rpl_decomposition.py
  python rpl_decomposition.py --baseline 2026-03 --end 2026-08 --min-conv 5
Output: rpl_decomposition_<end>.xlsx in --output-dir (default script dir /output)
"""
import argparse, os, re, sqlite3, sys
from datetime import date
from collections import defaultdict

DB_PATH   = r"C:\Projects\google-ads-ops\db\paid_search.db"
GEO_TABLE = "campaign_performance_geo_daily"
ACCOUNT_ID = 1
CAMPAIGNS = {   # label -> campaign_id (stable keys; names have been relabeled over time)
    "TIER1": 23496334752,
    "TIER2": 23530860942,
    "TIER3": 23586637678,
    "TEXAS": 23394808297,
}
def _norm(x):
    """Storage-agnostic campaign_id key (handles INT, TEXT, REAL '...0')."""
    s = str(x).strip()
    return s[:-2] if s.endswith(".0") else s
ID2LBL = {_norm(v): k for k, v in CAMPAIGNS.items()}
RETIER_EVENTS = {  # month -> annotation (known events; extend as needed)
    "2026-03": "target-change batches (Geico-ramp window)",
    "2026-06": "TIER ONE launch 6/19; TIER TWO launch 6/30; partner RPL cut ~6/18",
    "2026-07": "bottom-tier consolidation (wk of 7/6)",
}
BRAND_RX = re.compile(r"farmers|geico|state ?farm|allstate|progressive|lemonade|\bkin\b|hartford|travelers|amica|usaa|costco|american family|\broot\b|safeco|nationwide|liberty|erie|chubb", re.I)
HOME_RX  = re.compile(r"home|homeowner|house|property|dwelling|hazard|condo|fire|flood", re.I)

def qclass(kw):
    if HOME_RX.search(kw or ""): return "home_intent"
    if BRAND_RX.search(kw or ""): return "flagship"
    return "other"

def month_of(dstr): return dstr[:7]

def months_between(a, b):
    y, m = int(a[:4]), int(a[5:7]); out=[]
    while f"{y:04d}-{m:02d}" <= b:
        out.append(f"{y:04d}-{m:02d}"); m += 1
        if m == 13: y, m = y+1, 1
    return out

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DB_PATH)
    ap.add_argument("--baseline", default="2026-03", help="baseline month YYYY-MM")
    ap.add_argument("--end", default=None, help="last month YYYY-MM (default: max in DB)")
    ap.add_argument("--min-conv", type=int, default=5, help="min monthly conv for a state RPL to count")
    ap.add_argument("--margin-days", type=int, default=60, help="window for the state margin map")
    ap.add_argument("--output-dir", default=None)
    a = ap.parse_args()

    conn = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True)
    cur = conn.cursor()
    maxd = cur.execute(f"SELECT MAX(date) FROM {GEO_TABLE} WHERE account_id=?", (ACCOUNT_ID,)).fetchone()[0]
    end_m = a.end or month_of(maxd)
    months = months_between(a.baseline, end_m)
    base_m = a.baseline
    print(f"DB through {maxd} | months {months[0]}..{months[-1]} | baseline {base_m}")

    ids = tuple(CAMPAIGNS.values()) + tuple(str(v) for v in CAMPAIGNS.values())
    ph = ",".join("?"*len(ids))

    # ---- geo pulls (sum devices; device <> 'ALL') ----
    q = f"""SELECT substr(date,1,7) m, campaign_id, geo_state,
                   SUM(conversions), SUM(conversion_value), SUM(cost_micros)/1e6
            FROM {GEO_TABLE}
            WHERE account_id=? AND device<>'ALL' AND geo_state IS NOT NULL
              AND substr(date,1,7)>=? AND substr(date,1,7)<=? AND campaign_id IN ({ph})
            GROUP BY 1,2,3"""
    camp = defaultdict(lambda: [0.0,0.0,0.0])   # (m, cid, state) -> conv, val, cost
    for m,cid,s,cv,val,cost in cur.execute(q, (ACCOUNT_ID, base_m, end_m, *ids)):
        k=(m,_norm(cid),s); e=camp[k]
        e[0]+=cv or 0; e[1]+=val or 0; e[2]+=cost or 0
    for _lbl,_cr in CAMPAIGNS.items():
        _n=sum(1 for (m,c,s) in camp if c==_norm(_cr))
        print(f"  geo rows {_lbl}: {_n} campaign-state-months")

    q2 = f"""SELECT substr(date,1,7) m, geo_state, SUM(conversions), SUM(conversion_value)
             FROM {GEO_TABLE}
             WHERE account_id=? AND device<>'ALL' AND geo_state IS NOT NULL
               AND substr(date,1,7)>=? AND substr(date,1,7)<=?
             GROUP BY 1,2"""
    acct = defaultdict(lambda: [0.0,0.0])       # (m, state) -> conv, val (account-wide)
    for m,s,cv,val in cur.execute(q2, (ACCOUNT_ID, base_m, end_m)):
        acct[(m,s)] = [cv or 0, val or 0]

    def v(m,s):  # intrinsic state RPL, guarded
        cv,val = acct.get((m,s), (0,0))
        return (val/cv) if cv >= a.min_conv else None

    # ---- decomposition ----
    series = {}   # label -> list of dict(month, actual, recon, fb, resid, conv)
    attrib = {}   # label -> dict
    for label,cid_raw in CAMPAIGNS.items():
        cid = _norm(cid_raw)
        states_base = {s: camp[(base_m,cid,s)][0] for (m,c,s) in camp if m==base_m and c==cid and camp[(base_m,cid,s)][0]>0}
        wbase_tot = sum(states_base.values()) or 1.0
        wbase = {s: cv/wbase_tot for s,cv in states_base.items()}
        rows=[]
        for m in months:
            st = {s: camp[(m,cid,s)] for (mm,c,s) in camp if mm==m and c==cid}
            conv = sum(x[0] for x in st.values()); val = sum(x[1] for x in st.values())
            actual = val/conv if conv>0 else None
            # recon: current weights x intrinsic values (renormalized over states with v)
            num=den=0.0
            for s,(cv,_,_) in st.items():
                vv = v(m,s)
                if vv is not None and cv>0: num += cv*vv; den += cv
            recon = num/den if den>0 else None
            # fixed basket: March weights x today's intrinsic values (renormalized)
            fnum=fden=0.0
            for s,wb in wbase.items():
                vv = v(m,s)
                if vv is not None: fnum += wb*vv; fden += wb
            fb = fnum/fden if fden>0 else None
            resid = (actual-recon) if (actual is not None and recon is not None) else None
            rows.append(dict(month=m, conv=conv, actual=actual, recon=recon, fb=fb, resid=resid))
        series[label]=rows
        b = rows[0]; n = next((r for r in reversed(rows) if r["actual"] is not None and r["conv"]>=a.min_conv), rows[-1])
        if b["actual"] is None or n["actual"] is None:
            attrib[label]=None; continue
        h2 = (n["fb"]-b["fb"]) if (n["fb"] is not None and b["fb"] is not None) else None
        h1 = (n["recon"]-n["fb"]) if (n["recon"] is not None and n["fb"] is not None) else None
        h3 = ((n["resid"] or 0)-(b["resid"] or 0))
        attrib[label]=dict(base_m=b["month"], now_m=n["month"], rpl_base=b["actual"], rpl_now=n["actual"],
                           delta=n["actual"]-b["actual"], H1_mix=h1, H2_decay=h2, H3_quality=h3)

    # ---- state margin map (last N days) ----
    q3 = f"""SELECT campaign_id, geo_state, SUM(conversions), SUM(conversion_value), SUM(cost_micros)/1e6
             FROM {GEO_TABLE}
             WHERE account_id=? AND device<>'ALL' AND geo_state IS NOT NULL
               AND date >= date(?, '-{a.margin_days} day') AND campaign_id IN ({ph})
             GROUP BY 1,2"""
    marg=[]
    for cid,s,cv,val,cost in cur.execute(q3,(ACCOUNT_ID,maxd,*ids)):
        if (cv or 0)<=0 and (cost or 0)<5: continue
        lbl=ID2LBL.get(_norm(cid))
        if lbl is None: continue
        rpl=(val/cv) if cv else 0; cpl=(cost/cv) if cv else None
        marg.append((lbl,s,cv or 0,rpl,cpl,(rpl-cpl) if cpl is not None else None,(val or 0)-(cost or 0)))

    # ---- keyword-grain class mix (H3 corroboration) ----
    kmix=[]; statecls=[]; kw_ok=True
    try:
        cols={r[1] for r in cur.execute("PRAGMA table_info(keyword_performance_daily)")}
        kcols={r[1] for r in cur.execute("PRAGMA table_info(keyword_instances)")}
        def pick(cands, avail, what):
            c=next((x for x in cands if x in avail), None)
            if c is None: raise RuntimeError(f"no {what} column; available: {sorted(avail)}")
            return c
        datecol = pick(['report_date','date','day','stat_date'], cols, "date")
        icol    = pick(['instance_id','keyword_instance_id','ki_id'], cols, "instance-join")
        ccol    = pick(['primary_conv_count','conversions','conv'], cols, "conversions")
        valcol  = pick(['primary_conv_value','conversion_value','conv_value','revenue'], cols, "value")
        costexpr= "SUM(kpd.cost_micros)/1e6" if "cost_micros" in cols else "SUM(kpd.cost)"
        imprexpr= "SUM(kpd.impressions)" if "impressions" in cols else "0"
        if "historical_quality_score" in cols and "impressions" in cols:
            qsx="SUM(CASE WHEN kpd.historical_quality_score IS NOT NULL THEN kpd.historical_quality_score*kpd.impressions ELSE 0 END)"
            qsi="SUM(CASE WHEN kpd.historical_quality_score IS NOT NULL THEN kpd.impressions ELSE 0 END)"
        else:
            qsx="0"; qsi="0"
        kid  = pick(['id','instance_id','ki_id'], kcols, "ki id")
        ktxt = pick(['keyword_text','keyword','criterion_text','text'], kcols, "keyword text")
        # device mode: prefer 'ALL' rollup rows if they exist at this grain, else sum devices
        devmode="1=1"
        if "device" in cols:
            n_all=cur.execute("SELECT COUNT(*) FROM keyword_performance_daily WHERE device='ALL' LIMIT 1").fetchone()[0]
            devmode = "kpd.device='ALL'" if n_all else "kpd.device<>'ALL'"
        # geo mode: if NULL-state rollup rows coexist with state rows, use NULLs for campaign mix
        geomix="1=1"; has_geo=("geo_state" in cols)
        if has_geo:
            n_null=cur.execute("SELECT COUNT(*) FROM keyword_performance_daily WHERE geo_state IS NULL LIMIT 1").fetchone()[0]
            n_st  =cur.execute("SELECT COUNT(*) FROM keyword_performance_daily WHERE geo_state IS NOT NULL LIMIT 1").fetchone()[0]
            if n_null and n_st: geomix="kpd.geo_state IS NULL"
        print(f"  keyword grain: date={datecol} conv={ccol} val={valcol} dev=[{devmode}] mix-rows=[{geomix}] geo_state={'yes' if has_geo else 'no'}")
        qk=f"""SELECT substr(kpd.{datecol},1,7) m, ki.campaign_id, ki.{ktxt},
                      SUM(kpd.{ccol}), SUM(kpd.{valcol}), {costexpr}, {imprexpr}, {qsx}, {qsi}
               FROM keyword_performance_daily kpd
               JOIN keyword_instances ki ON ki.{kid}=kpd.{icol}
               WHERE {devmode} AND {geomix} AND substr(kpd.{datecol},1,7)>=? AND substr(kpd.{datecol},1,7)<=?
                 AND ki.campaign_id IN ({ph})
               GROUP BY 1,2,3"""
        agg=defaultdict(lambda:[0.0]*6)
        for m,cid,kw,cv,val,cost,imp,qx,qi in cur.execute(qk,(base_m,end_m,*ids)):
            k2=(m,_norm(cid),qclass(kw)); e=agg[k2]
            e[0]+=cv or 0; e[1]+=val or 0; e[2]+=cost or 0; e[3]+=imp or 0; e[4]+=qx or 0; e[5]+=qi or 0
        for (m,cid,cl),(cv,val,cost,imp,qx,qi) in sorted(agg.items()):
            lbl=ID2LBL.get(_norm(cid))
            if lbl is None: continue
            kmix.append((m,lbl,cl,cv,val,cost,imp,(qx/qi) if qi else None))
        # within-state class split (last 60d) — the definitive H3 test, now that geo_state exists
        if has_geo:
            qs2=f"""SELECT ki.campaign_id, kpd.geo_state, ki.{ktxt},
                           SUM(kpd.{ccol}), SUM(kpd.{valcol}), {costexpr}
                    FROM keyword_performance_daily kpd
                    JOIN keyword_instances ki ON ki.{kid}=kpd.{icol}
                    WHERE {devmode} AND kpd.geo_state IS NOT NULL
                      AND kpd.{datecol} >= date(?, '-{a.margin_days} day')
                      AND ki.campaign_id IN ({ph})
                    GROUP BY 1,2,3"""
            agg2=defaultdict(lambda:[0.0,0.0,0.0])
            for cid,st,kw,cv,val,cost in cur.execute(qs2,(maxd,*ids)):
                k3=(ID2LBL.get(_norm(cid)),st,qclass(kw))
                if k3[0] is None: continue
                agg2[k3][0]+=cv or 0; agg2[k3][1]+=val or 0; agg2[k3][2]+=cost or 0
            statecls=[(lbl,st,cl,cv,val,cost) for (lbl,st,cl),(cv,val,cost) in sorted(agg2.items())]
    except Exception as e:
        kw_ok=False; print(f"keyword-grain pull skipped: {e}")

    conn.close()
    write_xlsx(a, months, series, attrib, marg, kmix, statecls, kw_ok, end_m)

def write_xlsx(a, months, series, attrib, marg, kmix, statecls, kw_ok, end_m):
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.chart import LineChart, Reference
    BLK=Font(name='Arial'); BOLD=Font(name='Arial',bold=True)
    WHT=Font(name='Arial',bold=True,color='FFFFFF'); HF=PatternFill('solid',fgColor='1F4E79')
    SUB=PatternFill('solid',fgColor='D9E1F2'); th=Side(style='thin',color='BBBBBB')
    BD=Border(left=th,right=th,top=th,bottom=th); CUR='$#,##0.00;($#,##0.00);"-"'
    wb=Workbook()

    ws=wb.active; ws.title="Attribution"; ws.sheet_view.showGridLines=False
    ws.column_dimensions['A'].width=12
    for c in 'BCDEFGHI': ws.column_dimensions[c].width=13
    ws['A1']="RPL EROSION ATTRIBUTION — H1 mix / H2 state decay / H3 quality residual"
    ws['A1'].font=Font(name='Arial',bold=True,size=13)
    ws['A2']=f"Baseline {a.baseline} vs latest readable month. Identity: H1+H2+H3 = ΔRPL. TEXAS = control (single state, H1≡0)."
    ws['A2'].font=Font(name='Arial',italic=True,size=9,color='666666')
    hdr=['Campaign','RPL base','RPL now','Δ RPL','H1 mix','H2 state decay','H3 quality','identity check','share H1/H2/H3']
    for j,h in enumerate(hdr,1):
        c=ws.cell(row=4,column=j,value=h); c.font=WHT; c.fill=HF; c.border=BD; c.alignment=Alignment(horizontal='center',wrap_text=True)
    r=5
    for lbl in series:
        at=attrib.get(lbl)
        ws.cell(row=r,column=1,value=lbl).font=BOLD
        if at:
            for j,k in [(2,'rpl_base'),(3,'rpl_now'),(4,'delta'),(5,'H1_mix'),(6,'H2_decay'),(7,'H3_quality')]:
                cc=ws.cell(row=r,column=j,value=at[k]); cc.number_format=CUR; cc.border=BD
            ws.cell(row=r,column=8,value=f"=E{r}+F{r}+G{r}-D{r}").number_format=CUR
            tot=abs(at['H1_mix'] or 0)+abs(at['H2_decay'] or 0)+abs(at['H3_quality'] or 0) or 1
            ws.cell(row=r,column=9,value=f"{abs(at['H1_mix'] or 0)/tot:.0%} / {abs(at['H2_decay'] or 0)/tot:.0%} / {abs(at['H3_quality'] or 0)/tot:.0%}")
        for j in range(1,10): ws.cell(row=r,column=j).border=BD
        r+=1

    for lbl,rows in series.items():
        w=wb.create_sheet(f"CF_{lbl}"); w.sheet_view.showGridLines=False
        w['A1']=f"{lbl} — actual vs fixed-basket ({a.baseline} weights) vs state-reconstructed RPL"
        w['A1'].font=Font(name='Arial',bold=True,size=12)
        for j,h in enumerate(['Month','Conv','Actual RPL','Reconstructed','Fixed basket','Residual (H3 fingerprint)','Event'],1):
            c=w.cell(row=3,column=j,value=h); c.font=WHT; c.fill=HF; c.border=BD
            w.column_dimensions[chr(64+j)].width=14 if j>1 else 10
        w.column_dimensions['G'].width=44
        for i,rr in enumerate(rows):
            r2=4+i
            w.cell(row=r2,column=1,value=rr['month'])
            w.cell(row=r2,column=2,value=rr['conv'])
            for j,k in [(3,'actual'),(4,'recon'),(5,'fb'),(6,'resid')]:
                cc=w.cell(row=r2,column=j,value=rr[k]); cc.number_format=CUR
            w.cell(row=r2,column=7,value=RETIER_EVENTS.get(rr['month'],""))
            for j in range(1,8): w.cell(row=r2,column=j).border=BD
        ch=LineChart(); ch.title=f"{lbl}: actual vs fixed-basket vs reconstructed"; ch.height=8; ch.width=16
        ch.y_axis.title='RPL ($)'
        d=Reference(w,min_col=3,max_col=5,min_row=3,max_row=3+len(rows))
        cats=Reference(w,min_col=1,min_row=4,max_row=3+len(rows))
        ch.add_data(d,titles_from_data=True); ch.set_categories(cats); w.add_chart(ch,"I3")

    w=wb.create_sheet("State Margin Map"); w.sheet_view.showGridLines=False
    w['A1']=f"Per-state margin, last {a.margin_days} days — which states carry GP vs bleed"
    w['A1'].font=Font(name='Arial',bold=True,size=12)
    for j,h in enumerate(['Campaign','State','Conv','RPL','CPL','Margin/lead','GP $'],1):
        c=w.cell(row=3,column=j,value=h); c.font=WHT; c.fill=HF; c.border=BD
        w.column_dimensions[chr(64+j)].width=14
    for i,(lbl,s,cv,rpl,cpl,mgl,gp) in enumerate(sorted(marg,key=lambda x:(x[0],x[5] if x[5] is not None else 0))):
        r2=4+i
        vals=[lbl,s,cv,rpl,cpl,mgl,gp]
        for j,v_ in enumerate(vals,1):
            cc=w.cell(row=r2,column=j,value=v_); cc.border=BD
            if j in (4,5,6,7): cc.number_format=CUR

    if kw_ok and kmix:
        w=wb.create_sheet("Query Mix"); w.sheet_view.showGridLines=False
        w['A1']="Keyword-grain class mix by campaign-month: flagship vs home_intent vs other (+ avg Quality Score)"
        w['A1'].font=Font(name='Arial',bold=True,size=12)
        for j,h in enumerate(['Month','Campaign','Class','Conv','Value','Cost','Class RPL','Impr','Avg QS'],1):
            c=w.cell(row=3,column=j,value=h); c.font=WHT; c.fill=HF; c.border=BD
            w.column_dimensions[chr(64+j)].width=12
        for i,(m,lbl,cl,cv,val,cost,imp,qs) in enumerate(kmix):
            r2=4+i
            for j,v_ in enumerate([m,lbl,cl,cv,val,cost,(val/cv) if cv else None,imp,qs],1):
                cc=w.cell(row=r2,column=j,value=v_); cc.border=BD
                if j in (5,6,7): cc.number_format=CUR
                if j==9 and v_ is not None: cc.number_format='0.0'
        # class-mix counterfactual per campaign (keyword grain): hold baseline class weights
        w=wb.create_sheet("Class Counterfactual"); w.sheet_view.showGridLines=False
        w['A1']="Keyword-grain RPL: actual vs class-mix-fixed (baseline weights) — the H3 value test"
        w['A1'].font=Font(name='Arial',bold=True,size=12)
        for j,h in enumerate(['Campaign','Month','Actual kw-RPL','Fixed-class-mix RPL','Gap (mix effect)','Flagship conv share'],1):
            c=w.cell(row=3,column=j,value=h); c.font=WHT; c.fill=HF; c.border=BD
            w.column_dimensions[chr(64+j)].width=17
        from collections import defaultdict as dd
        bycamp=dd(lambda: dd(dict))
        for (m,lbl,cl,cv,val,cost,imp,qs) in kmix: bycamp[lbl][m][cl]=(cv,val)
        r2=4
        for lbl,md in bycamp.items():
            ms=sorted(md)
            if not ms: continue
            b=md[ms[0]]; btot=sum(v[0] for v in b.values()) or 1
            wb_cls={cl:v[0]/btot for cl,v in b.items()}
            for m in ms:
                d=md[m]; tot=sum(v[0] for v in d.values()); val=sum(v[1] for v in d.values())
                actual=(val/tot) if tot else None
                num=den=0.0
                for cl,wt in wb_cls.items():
                    if cl in d and d[cl][0]>0:
                        num+=wt*(d[cl][1]/d[cl][0]); den+=wt
                cf=(num/den) if den else None
                fshare=(d.get('flagship',(0,0))[0]/tot) if tot else None
                vals=[lbl,m,actual,cf,(actual-cf) if (actual is not None and cf is not None) else None,fshare]
                for j,v_ in enumerate(vals,1):
                    cc=w.cell(row=r2,column=j,value=v_); cc.border=BD
                    if j in (3,4,5): cc.number_format=CUR
                    if j==6 and v_ is not None: cc.number_format='0%'
                r2+=1
        if statecls:
            w=wb.create_sheet("State x Class"); w.sheet_view.showGridLines=False
            w['A1']=f"Within-state class split, last {a.margin_days}d — same state, flagship vs home_intent value"
            w['A1'].font=Font(name='Arial',bold=True,size=12)
            for j,h in enumerate(['Campaign','State','Class','Conv','Value','Cost','Class RPL'],1):
                c=w.cell(row=3,column=j,value=h); c.font=WHT; c.fill=HF; c.border=BD
                w.column_dimensions[chr(64+j)].width=14
            for i,(lbl,st,cl,cv,val,cost) in enumerate(statecls):
                r3=4+i
                for j,v_ in enumerate([lbl,st,cl,cv,val,cost,(val/cv) if cv else None],1):
                    cc=w.cell(row=r3,column=j,value=v_); cc.border=BD
                    if j in (5,6,7): cc.number_format=CUR
    w=wb.create_sheet("Narrative"); w.sheet_view.showGridLines=False; w.column_dimensions['A'].width=110
    lines=["NARRATIVE (auto-drafted — edit before sending)",""]
    for lbl,at in attrib.items():
        if not at: continue
        parts=[("mix shift from re-tiering (H1)",at['H1_mix']),("per-state value decay (H2)",at['H2_decay']),("lead-quality/query-mix (H3)",at['H3_quality'])]
        parts.sort(key=lambda x:abs(x[1] or 0),reverse=True)
        lines.append(f"{lbl}: RPL {at['rpl_base']:.0f} -> {at['rpl_now']:.0f} ({at['delta']:+.0f}). Largest driver: {parts[0][0]} ({parts[0][1]:+.1f}), then {parts[1][0]} ({parts[1][1]:+.1f}), then {parts[2][0]} ({parts[2][1]:+.1f}).")
    lines += ["","Reversibility map: H1 is fixable by re-tiering (administrative); H2 lives outside Google (payout/assignment side);",
              "H3 is fixable only by reversing the query-mix degradation (the spiral)."]
    for i,t in enumerate(lines):
        w.cell(row=1+i,column=1,value=t).font=Font(name='Arial',bold=(i==0),size=10)

    w=wb.create_sheet("Methodology"); w.sheet_view.showGridLines=False; w.column_dimensions['A'].width=112
    meth=["METHODOLOGY","",
     "v(s,m) = account-wide state RPL (intrinsic lead value, campaign-independent), guarded at min-conv "+str(a.min_conv)+".",
     "w(c,s,m) = campaign's conversion mix. recon = SUM w*v; fixed_basket = SUM w_base*v (renormalized over states with data).",
     "H2 = fixed_basket(now)-fixed_basket(base): what the March basket's value did on its own (state decay).",
     "H1 = recon(now)-fixed_basket(now): value change from holding different states than in March (mix shift).",
     "H3 = residual(now)-residual(base), residual = actual-recon: this campaign's leads from the same states are worth",
     "     more/less than the account-average lead from those states — the lead-quality / query-mix fingerprint.",
     "TEXAS control: single state => H1==0 by construction; its line isolates H2 for that state.",
     "Keyword-grain Query Mix tab corroborates H3 (state x query cannot be joined lead-level in this DB).",
     "House rules: read-only connection; geo devices summed (device<>'ALL'); revenue = platform conversion_value;",
     "all data treated as complete (no lag exclusion, house ruling 8/20). Do not run during a DB sync."]
    for i,t in enumerate(meth):
        w.cell(row=1+i,column=1,value=t).font=Font(name='Arial',bold=(i==0),size=10)

    outdir = a.output_dir or os.path.join(os.path.dirname(os.path.abspath(__file__)),"output")
    os.makedirs(outdir,exist_ok=True)
    out=os.path.join(outdir,f"rpl_decomposition_{end_m}.xlsx")
    wb.save(out)
    print(f"wrote {out}")
    for lbl,at in attrib.items():
        if at: print(f"  {lbl}: dRPL {at['delta']:+.1f} = H1 {at['H1_mix']:+.1f} + H2 {at['H2_decay']:+.1f} + H3 {at['H3_quality']:+.1f}")

if __name__ == "__main__":
    main()
