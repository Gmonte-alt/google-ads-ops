#!/usr/bin/env python3
"""USIA Home -- COLD-START RSA builder (net-new, Ads Editor import). 34 ad groups, 2 RSAs each.
Reads the proposed-structure Editor export (UTF-16 TSV) directly (no paid_search.db) and reuses
the proven engine leaf-helpers from bulk_import_ads_google.py. Live refresh tool untouched."""
import argparse, re, sys, math
from pathlib import Path
import pandas as pd
import bulk_import_ads_google as B

FINAL_URL="https://www.usinsuranceagents.com/"
HL_LIMIT,DESC_LIMIT,MAX_HL,MAX_DESC,BARE_CAP=30,90,15,4,4
KW_DOM=0.50

AGENT_CORE_A=["ub:1","ub:2","ub:9"]; AGENT_CORE_B=["ub:9","ub:2","ub:1"]; COMPARE=["ub:3","ub:4"]; TRUST_PROOF=["ub:5","ub:8"]
ANCHORS_UB=["ub:6"]; LOC="ub:7"; DKI="ub:10"
LOC_STATE="{LOCATION(State):Your State} Home Quotes"   # state-level location insertion (priority)
LOC_CITY="{LOCATION(City):Your City} Home Agents"      # city-level location insertion (secondary)
SENIOR=["sw:Rates for Seniors","sw:Home Rates for Seniors"]
TRUSTSW=["sw:Licensed in Your State","sw:A-Rated Carriers Only"]
LADDER_CQ=["ANCHOR: brand+product","Compare rates","Existing customer","Renewal pain",
           "Fit question","Coverage (Does-Carrier-Have set)","Side by side (bare-only)","How-compare (bare-only)"]
PERIL_BAND=["peril","specialty","disaster","fire","flood","quake","storm","coverage","risk"]
CARRIER_CLEAN={"Kin Insurance":"Kin"}
# Trademark-strict brand-generic carriers: brand must NOT appear as static USIA copy; it surfaces
# only via DKI keyword insertion. Generic fallback defaults keep the brand off the no-match render.
STRICT_BRAND={"State Farm"}
DKI_SAFE=["lit:{KeyWord:Compare} Home Insurance","lit:{KeyWord:Compare} Home Rates","lit:{KeyWord:Get} Home Quotes"]

def gen_tiers(kind,iset):
    """Return {'A':spec,'B':spec}; spec=dict(carrier_concepts,hls,descs)."""
    isH=f"isH:{iset}:all"; isD=f"isD:{iset}:all"
    if kind=="conquest":
        return {"A":dict(carrier_concepts=LADDER_CQ,hls=AGENT_CORE_A+COMPARE+[DKI],
                         descs=["isD:Carrier Conquesting (template):all"]),
                "B":dict(carrier_concepts=LADDER_CQ,hls=AGENT_CORE_B+SENIOR+TRUSTSW+[DKI],
                         descs=["isD:Carrier Conquesting (template):all"])}
    if kind=="conquest_strict":  # trademark-strict conquest: NO static brand; DKI surfaces the user-query brand, else brand-free comparison copy
        return {"A":dict(carrier_concepts=None,hls=AGENT_CORE_A+[DKI,"lit:Compare Top Home Rates"]+COMPARE+ANCHORS_UB+TRUST_PROOF+["sw:Right Coverage, Right Price"],descs=["ubd:1","ubd:2"]),
                "B":dict(carrier_concepts=None,hls=AGENT_CORE_B+[DKI,"lit:Compare Top Home Rates"]+SENIOR+COMPARE+ANCHORS_UB+TRUSTSW,descs=["ubd:3","ubd:1"])}
    if kind=="brand_generic":   # lenient: brand may appear statically; fill spare slots with universal backbones
        return {"A":dict(carrier_concepts=["ANCHOR: brand+product"],hls=AGENT_CORE_A+[isH]+COMPARE+TRUST_PROOF+ANCHORS_UB,descs=[isD,"ubd:1"]),
                "B":dict(carrier_concepts=["ANCHOR: brand+product"],hls=AGENT_CORE_B+[isH]+SENIOR+TRUST_PROOF+ANCHORS_UB,descs=[isD,"ubd:3"])}
    if kind=="brand_generic_strict":  # trademark-strict: NO static brand; brand surfaces only via DKI keyword insertion
        return {"A":dict(carrier_concepts=None,hls=AGENT_CORE_A+DKI_SAFE+COMPARE+TRUST_PROOF+ANCHORS_UB+TRUSTSW,descs=[isD,"ubd:1"]),
                "B":dict(carrier_concepts=None,hls=AGENT_CORE_B+DKI_SAFE+SENIOR+TRUST_PROOF+ANCHORS_UB+TRUSTSW,descs=[isD,"ubd:3"])}
    if kind=="generic_nocarrier":
        return {"A":dict(carrier_concepts=None,hls=AGENT_CORE_A+[isH]+COMPARE+ANCHORS_UB+[DKI],descs=[isD,"ubd:1"]),
                "B":dict(carrier_concepts=None,hls=AGENT_CORE_B+[isH]+SENIOR+COMPARE+[DKI],descs=[isD,"ubd:3"])}
    # generic (default)
    extra_loc=[LOC] if iset=="Near Me" else []
    return {"A":dict(carrier_concepts=None,hls=AGENT_CORE_A+COMPARE+ANCHORS_UB+[isH]+extra_loc+TRUST_PROOF+[DKI],descs=[isD,"ubd:1"]),
            "B":dict(carrier_concepts=None,hls=AGENT_CORE_B+[isH]+SENIOR+extra_loc+ANCHORS_UB+COMPARE+[DKI],descs=[isD,"ubd:3"])}

# ---- Campaign names: must match BOTH the structure CSV Campaign column AND the live account.
# Rename here + in the CSV (and in the account) together; the builder only uses these as exact join keys.
C_GEN_CALL      = "USIA_HOME_GEN_CALL"
C_GEN           = "USIA_HOME_GEN"
C_BRAND_CARRIER = "USIA_HOME_BRAND_CARRIER"
C_BRAND_GENERIC = "USIA_HOME_BRAND_GENERIC"

# ---- DESIGN: one line per ad group keyed (campaign, ad group) ----
def D(kind,iset,band="home",carrier=None,paths=("Home-Insurance","Compare")):
    return dict(kind=kind,iset=iset,band=band,carrier=carrier,paths=paths)
DESIGN={
 (C_GEN_CALL,"AGENT_BROKER"): D("generic","Agent Broker",paths=("Local-Agents","Free-Quote")),
 (C_GEN_CALL,"NEAR_ME"):      D("generic","Near Me",paths=("Local-Agents","Free-Quote")),
 (C_GEN,"CORE"):              D("generic","Generic Core"),
 (C_GEN,"COMPARE"):           D("generic","Compare",paths=("Compare","Free-Quote")),
 (C_GEN,"PRICE"):             D("generic","Price",paths=("Affordable","Free-Quote")),
 (C_GEN,"COST"):              D("generic","Cost",paths=("Home-Cost","Free-Quote")),
 (C_GEN,"COVERAGE"):          D("generic","Coverage",paths=("Coverage","Free-Quote")),
 (C_GEN,"POLICY"):            D("generic","Policy",band="property",paths=("Coverage","Free-Quote")),
 (C_GEN,"BUNDLE"):            D("generic","Bundle",paths=("Bundle","Free-Quote")),
 (C_GEN,"QUOTE"):             D("generic","Quote",paths=("Home-Quote","Compare")),
 (C_GEN,"CONDO"):             D("generic","Condo",band="condo",paths=("Condo","Free-Quote")),
 (C_GEN,"HAZARD"):            D("generic","Hazard",band="hazard",paths=("Hazard","Free-Quote")),
 (C_GEN,"HOMEOWNERS_EXT"):    D("generic","Homeowners Ext",paths=("Second-Home","Free-Quote")),
 (C_GEN,"MOBILE_MANUFACTURED"):D("generic","Mobile Manufactured",paths=("Mobile-Home","Free-Quote")),
 (C_GEN,"PERIL"):             D("generic","Peril",band=PERIL_BAND,paths=("Peril-Coverage","Free-Quote")),
}
# Conquest single-carrier (BRAND_CARRIER)
for c in ["American Family","Amica","Assurant","Chubb","Farmers","Geico","Hartford","Hippo","Mercury","State Farm","Travelers","Universal Property"]:
    k="conquest_strict" if c in STRICT_BRAND else "conquest"
    DESIGN[(C_BRAND_CARRIER,c)]=D(k,"Carrier Conquesting (template)",carrier=c,paths=("Compare","Free-Quote"))
# Multi-brand BRAND_CARRIER
DESIGN[(C_BRAND_CARRIER,"CARRIER_OTHER")]=D("generic_nocarrier","Carrier Other",paths=("Compare","Free-Quote"))
DESIGN[(C_BRAND_CARRIER,"AFFINITY")]   =D("generic_nocarrier","Membership / Affinity",paths=("Compare","Free-Quote"))
DESIGN[(C_BRAND_CARRIER,"AGGREGATOR")] =D("generic_nocarrier","Aggregator",paths=("Compare","Free-Quote"))
# Brand-generic (bare carrier)
for c in ["American Family","Bamboo","Farmers","Geico","Hartford","Kin Insurance","Nationwide","Plymouth Rock","State Farm","Travelers"]:
    k="brand_generic_strict" if c in STRICT_BRAND else "brand_generic"
    DESIGN[(C_BRAND_GENERIC,c)]=D(k,"Brand Generic (DKI suffix)",carrier=CARRIER_CLEAN.get(c,c),paths=("Compare","Free-Quote"))

def load_structure(p):
    # Editor exports are UTF-16 tab-delimited, but a round-trip through Excel can re-save them as
    # UTF-8/CP1252 and/or comma-delimited. Auto-detect encoding + delimiter rather than assuming.
    df=None; tried=[]
    for enc in ("utf-16","utf-16-le","utf-8-sig","utf-8","cp1252"):
        try:
            t=pd.read_csv(p,sep=None,engine="python",encoding=enc,dtype=str,keep_default_na=False)
        except Exception as e:
            tried.append(f"{enc}: {type(e).__name__}"); continue
        t.columns=[str(c).strip().lstrip("\ufeff") for c in t.columns]
        if "Ad group" in t.columns and "Ad Group" not in t.columns: t=t.rename(columns={"Ad group":"Ad Group"})
        if "Campaign" in t.columns and "Ad Group" in t.columns:
            df=t; break
        tried.append(f"{enc}: parsed but no Campaign/Ad Group (got: {', '.join(t.columns[:6])}...)")
    if df is None:
        raise SystemExit("Could not read structure file as an Editor export.\nTried -> "+" | ".join(tried)
                         +"\nRe-export from Ads Editor (Account > Keywords) and keep the original Unicode/UTF-16 text format, header row included.")
    if "Keyword" not in df.columns:
        raise SystemExit("Structure file parsed, but there is no 'Keyword' column.\nColumns found: "+", ".join(df.columns)
                         +"\nThis looks like the wrong export (e.g. ads- or campaign-level). Export the KEYWORDS view so keyword rows are present.")
    return df[df["Keyword"]!=""]
def kw_profile(df,camp,ag,carrier):
    kws=[k.lower() for k in df[(df["Campaign"]==camp)&(df["Ad Group"]==ag)]["Keyword"] if k]
    if not kws: return "unknown",0.0
    hf=sum("home" in k for k in kws)/len(kws); return ("home_dominant" if hf>=KW_DOM else "mixed"),hf
def expand(ref,copy,carrier):
    out=[]
    if ref.startswith("ub:"): out=[copy["ub_headlines"].get(int(ref[3:]),"")]
    elif ref.startswith("lit:"): out=[ref[4:]]
    elif ref.startswith("ubd:"): out=[copy["ub_descriptions"].get(int(ref[4:]),"")]
    elif ref.startswith("sw:"): out=[ref[3:]] if ref[3:] in copy["swaps_add"] else [""]
    elif ref.startswith("isH:"): s=ref[4:].rsplit(":",1)[0]; out=list(copy["intent_sets"].get(s,{}).get("headlines",{}).values())
    elif ref.startswith("isD:"): s=ref[4:].rsplit(":",1)[0]; out=list(copy["intent_sets"].get(s,{}).get("descriptions",{}).values())
    res=[]
    for t in out:
        if not t: continue
        if carrier: t=B.substitute_carrier(t,carrier)
        t,_=B.normalize_location_insertion(t); res.append(t)
    return res
def is_band(text,term):
    s=B.static_home_text(text).lower(); terms=term if isinstance(term,list) else [term]
    return any(t in s for t in terms)
def norm(text): return re.sub(r"\s+"," ",B.static_home_text(text).lower().replace("-"," ")).strip()

def assemble(spec,carrier,copy,band):
    pool=[]; warns=[]; bare_pending=[]; seen=set()
    def push(text,is_bare=False,is_dki=False):
        if not text or B.visible_char_length(text)>HL_LIMIT: return
        n=norm(text)
        if n in seen: return
        seen.add(n); pool.append(dict(text=text,is_bare=is_bare,is_dki=is_dki,is_home=(not is_dki) and is_band(text,band)))
    if spec.get("carrier_concepts"):
        for c in spec["carrier_concepts"]:
            rungs=copy["ladders"].get(c,[]); txt,_=B.resolve_home_rung(rungs,carrier)
            if txt: push(txt)
            else:
                bt,_=B.resolve_bare_rung(rungs,carrier)
                if bt: bare_pending.append(bt)
        if not any(carrier.lower() in p["text"].lower() and "home" in p["text"].lower() for p in pool):
            warns.append(f"no brand+product anchor for {carrier}")
    for ref in spec["hls"]:
        for t in expand(ref,copy,carrier): push(t,is_dki=(ref==DKI))
    floor=max(1,math.ceil(0.30*max(len(pool),1)))
    home_ct=sum(1 for p in pool if p["is_home"])
    anchor_ok=(not carrier) or any(carrier.lower() in p["text"].lower() and "home" in p["text"].lower() for p in pool)
    if home_ct>=floor and anchor_ok:
        bc=0
        for bt in bare_pending:
            if bc>=BARE_CAP: break
            push(bt,is_bare=True); bc+=1
    if len(pool)>MAX_HL:
        homes=[p for p in pool if p["is_home"]]; keep=homes[:floor]
        for p in pool:
            if p in keep: continue
            if len(keep)>=MAX_HL: break
            keep.append(p)
        pool=keep
    headlines=[p["text"] for p in pool][:MAX_HL]
    floor=max(1,math.ceil(0.30*len(headlines)))
    descs=[]; dseen=set()
    for ref in spec["descs"]:
        for t in expand(ref,copy,carrier):
            if B.visible_char_length(t)>DESC_LIMIT or t.lower() in dseen: continue
            dseen.add(t.lower()); descs.append(t)
    descs=descs[:MAX_DESC]
    hb=sum(1 for h in headlines if is_band(h,band))
    if hb<floor: warns.append(f"band floor {hb}/{floor}")
    if sum(1 for p in pool if p["is_bare"])>BARE_CAP: warns.append("bare cap exceeded")
    return headlines,descs,warns

def emit_row(camp,ag,hls,descs,paths,final_url):
    row={"Account":"","Account name":"USIA Home Insurance","Campaign":camp,"Ad Group":ag,
         "Ad type":"Responsive search ad","Labels":""}
    for i in range(1,MAX_HL+1):
        row[f"Headline {i}"]=hls[i-1] if i-1<len(hls) else ""; row[f"Headline {i} position"]=""
    for i in range(1,MAX_DESC+1):
        row[f"Description {i}"]=descs[i-1] if i-1<len(descs) else ""; row[f"Description {i} position"]=""
    row["Path 1"],row["Path 2"]=paths; row["Final URL"]=final_url
    row["Campaign Status"]="Enabled"; row["Ad Group Status"]="Enabled"; row["Status"]="Enabled"
    return row

def ensure_distinct_hl1(hls_a,hls_b):
    """Editor keys RSA identity on Headline 1 (it ignores Final-URL query params), so A and B
    must lead with different headlines or the second is dropped as a duplicate. If they collide,
    rotate a different B headline into position 1 (the original HL1 stays in B's pool)."""
    if not hls_a or not hls_b or hls_b[0]!=hls_a[0]: return hls_b
    for j in range(1,len(hls_b)):
        if hls_b[j]!=hls_a[0]:
            hls_b=list(hls_b); hls_b[0],hls_b[j]=hls_b[j],hls_b[0]; return hls_b
    return hls_b
def reserve_location_slot(hls):
    """Free one slot for the state location insertion when an RSA is full, without dropping a DKI
    headline (remove the last non-DKI line instead)."""
    if len(hls)<MAX_HL: return hls
    for i in range(len(hls)-1,-1,-1):
        if "{KeyWord" not in hls[i]:
            return hls[:i]+hls[i+1:]
    return hls[:MAX_HL-1]
def add_location(hls):
    """Fill spare headline slots: state-level location insertion first (priority), then city-level
    if room. Never displaces existing copy; only uses slots up to MAX_HL."""
    out=list(hls)
    has_state=any("{LOCATION(State)" in h for h in out)
    has_city=any("{LOCATION(City)" in h for h in out)
    if len(out)<MAX_HL and not has_state:
        t,_=B.normalize_location_insertion(LOC_STATE)
        if B.visible_char_length(t)<=HL_LIMIT: out.append(t); has_state=True
    if len(out)<MAX_HL and not has_city:
        t,_=B.normalize_location_insertion(LOC_CITY)
        if B.visible_char_length(t)<=HL_LIMIT: out.append(t)
    return out

def detect_campaigns(names):
    """Map the 4 builder roles onto the structure file's actual campaign names by role token, so the
    builder survives campaign renames without editing constants. Returns {role:name} only if every
    role resolves to exactly one campaign; otherwise None (caller falls back to the constants)."""
    buckets={}
    for n in names:
        u=(n or "").upper()
        if   "BRAND-CARRIER" in u or "BRAND_CARRIER" in u: role="BRAND_CARRIER"
        elif "BRAND-GENERIC" in u or "BRAND_GENERIC" in u: role="BRAND_GENERIC"
        elif "GEN" in u and "CALL" in u:                   role="GEN_CALL"
        elif "GEN" in u:                                   role="GEN"
        else: continue
        buckets.setdefault(role,[]).append(n)
    if all(len(buckets.get(r,[]))==1 for r in ("GEN_CALL","GEN","BRAND_CARRIER","BRAND_GENERIC")):
        return {r:buckets[r][0] for r in buckets}
    return None

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--structure",default="/mnt/user-data/uploads/USIA-Home-Insurance_Current-view_2026-06-23.csv")
    ap.add_argument("--workbook",default="/home/claude/rsa-load-ready-copy_USIA_v2.xlsx")
    ap.add_argument("--out",default="/home/claude/usia_rsa_editor_import_full.csv")
    a=ap.parse_args()
    df=load_structure(Path(a.structure)); copy=B.load_workbook_copy(Path(a.workbook))
    # Resolve the 4 builder roles to the structure file's actual campaign names (survives renames).
    csv_campaigns=sorted({c for c in df["Campaign"].unique() if c})
    role2name=detect_campaigns(csv_campaigns)
    const2role={C_GEN_CALL:"GEN_CALL",C_GEN:"GEN",C_BRAND_CARRIER:"BRAND_CARRIER",C_BRAND_GENERIC:"BRAND_GENERIC"}
    cmap={const:role2name[role] for const,role in const2role.items()} if role2name else {}
    DESIGN_R={(cmap.get(camp,camp),ag):d for (camp,ag),d in DESIGN.items()}
    if cmap: print("Campaign name mapping (role -> actual):\n  "+"\n  ".join(f"{c}  ->  {a_}" for c,a_ in cmap.items())+"\n")
    struct_groups={(c,g) for c,g in df.groupby(["Campaign","Ad Group"]).groups.keys()}
    rows=[]; rows_by={"A":[],"B":[]}; log=[]; warncount=0; missing=[]
    for key in struct_groups:
        if key not in DESIGN_R: missing.append(key)
    for (camp,ag),d in DESIGN_R.items():
        if (camp,ag) not in struct_groups:
            log.append(f"SKIP (not in structure): {camp}/{ag}"); continue
        klass,hf=kw_profile(df,camp,ag,d.get("carrier"))
        tiers=gen_tiers(d["kind"],d["iset"])
        asm={}
        asm_carrier=None if d["kind"] in ("brand_generic_strict","conquest_strict") else d.get("carrier")
        for label in ("A","B"):
            hls,descs,warns=assemble(tiers[label],asm_carrier,copy,d["band"])
            asm[label]=dict(hls=hls,descs=descs,warns=warns)
        asm["B"]["hls"]=ensure_distinct_hl1(asm["A"]["hls"],asm["B"]["hls"])
        asm["B"]["hls"]=reserve_location_slot(asm["B"]["hls"])  # keep DKI, free a slot for state location
        for label in ("A","B"):
            hls=add_location(asm[label]["hls"]); descs=asm[label]["descs"]; warns=list(asm[label]["warns"])
            fb=max(1,math.ceil(0.30*len(hls)))  # re-check band floor on FINAL headlines (post cap + location)
            if sum(1 for h in hls if is_band(h,d["band"]))<fb: warns.append(f"final band floor < {fb}")
            r=emit_row(camp,ag,hls,descs,d["paths"],FINAL_URL); rows.append(r); rows_by[label].append(r)
            if warns: warncount+=1
            log.append(f"{camp}/{ag} RSA-{label} [{d['kind']}/{d['iset']}] hf={hf:.2f}: {len(hls)}HL/{len(descs)}d"
                       +(f"  !! {warns}" if warns else ""))
    base=Path(a.out)
    if not rows:
        raise SystemExit("\n".join(sorted(log))+"\n\nABORT: 0 RSAs built — the script's campaign roles did not match the structure file.\n"
            +("Auto-detection failed; the CSV campaign names lack clear GENERIC / GENERIC-CALL / BRAND-CARRIER / BRAND-GENERIC role tokens.\n" if not cmap else "")
            +"Campaigns found in the structure file:\n  "+"\n  ".join(csv_campaigns)
            +"\nFix: rename so each campaign carries its role token, or set the 4 constants at the top of this script to these exact names. No file was written.")
    pd.DataFrame(rows).to_csv(base,index=False,encoding="utf-16",sep="\t")
    print("\n".join(sorted(log)))
    print(f"\nGROUPS in structure: {len(struct_groups)} | in DESIGN: {len(DESIGN_R)} | missing from DESIGN: {missing}")
    print(f"RSA rows: {len(rows)} (A={len(rows_by['A'])}, B={len(rows_by['B'])}) | rows with warnings: {warncount}")
    print(f"IMPORT THIS SINGLE FILE -> {base}")
    print("  Single clean Final URL on every ad; RSA-A and RSA-B lead with different Headline 1,")
    print("  which is the field Editor uses for RSA identity, so one import creates all 80.")
    return rows
if __name__=="__main__": main()
