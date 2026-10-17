"""
ms_expansion_select.py -- SELECTION pass for the Microsoft Home (acct 2) generic
keyword expansion. Consumes the Keyword Planner pulls, reuses ms_expansion_coverage's
classification + structure resolution (imported -- zero drift), and emits an
Editor-ready build recommendation workbook.

MODULES
  PULL 1 (impression-filtered heads): themed selection, PHRASE-FIRST match policy
    (exact is EARNED at proof, ~10 conv at target -- day-one exact on flagship heads
    only), with close-variant synonym surfaces (home/house/homeowners, plurals)
    collapsed to ONE target per variant signature; the rest ride as close variants.
  PULL 2 (unfiltered bundle universe): intent-signature consolidation -> one phrase
    head per clearing class; permutations ride as close variants.
  PULL 3 (mobile/manufactured, optional): same consolidation with product tokens in
    the base set + an INSURANCE-TOKEN screen (home-shopping surfaces excluded), both
    canonical heads seeded ("mobile home insurance", "manufactured home insurance").
    ALL mobile rows are action=HOLD on their own sheet -- documented, NOT in this
    release, pending the mobile-home RPL/payout gate.

AD-GROUP NAMING -- aligned to the Google generic expansion taxonomy (GROUP_FOR):
  Core -> Home-Generic-Home Insurance-Core          (live on MS)
  Quote -> Home-Generic-Quote                       (SPLIT from Core: quote intent
     gets its own group; currently-serving quote exacts listed on the
     'Quote Migration (wave 2)' sheet with the restr-2026-09-move label -- move
     AFTER the new group proves, never in the same wave)
  Best+Compare -> Home-Generic-Compare              (merged, per Google)
  Cheapest -> Home-Generic-Home Insurance-Cheapest  (LIVE MS groups kept as-is;
     Google folds cheap into Cost-Core -- deliberate divergence, don't disturb
     serving structure)
  Cost -> Home-Generic-Cost-Core                    (per Google)
  Companies -> Home-Generic-Companies               (KEPT SEPARATE -- coherent
     carrier-comparison intent; recommend porting the split to Google, not folding
     to Other here. Flip in GROUP_FOR to match Google's Other if overruled.)
  Agent, Policy -> Home-Generic-Other               (per Google)
  Bundle -> Home-Generic-Home Auto Bundle-{Core,Quote}  (no Google equivalent yet)
ROUTING RULE: product beats intent (Peril/Flood/Bundle/Mobile claim their terms
before intent modifiers); intent-over-product is a future iteration.

Every ADD is pending the MS Search Term Report served-check + negatives overlay.

    python ms_expansion_select.py --pull1-csv ...csv --pull2-csv ...csv [--pull3-csv ...csv]
"""
import argparse
import csv
import os
import re
import sqlite3
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ms_expansion_coverage import (normalize_09, normalize_loose, load_tagger,
                                   load_structure, verdict_for, HOME_RE, VOL_MID)

ACCT = 2
VOL = dict(VOL_MID); VOL.setdefault("0 - 10", 5)

BUNDLE_RE = re.compile(r"\b(auto|car|cars|vehicle|vehicles|automobile)\b")
MOBILE_RE = re.compile(r"\b(mobile|manufactured|modular|trailer)\b")
INS_RE = re.compile(r"\b(insurance|insur\w*|ins)\b")
FOREIGN_RE = re.compile(r"\b(life|renters?|business|commercial|medicare|health|pet)\b")

REGISTRY_GAPS = [
    ("Auto-Owners",      re.compile(r"\bauto owners?\b")),
    ("TruStage",         re.compile(r"\btrustage\b")),
    ("Ameriprise",       re.compile(r"\bameriprise\b")),
    ("Midvale",          re.compile(r"\bmidvale\b")),
    ("Unitrin/Kemper",   re.compile(r"\bunitrin\b")),
    ("Erie",             re.compile(r"\berie\b")),
    ("Auto Club/AAA",    re.compile(r"\bauto club\b")),
    ("Certas (Canada)",  re.compile(r"\bcertas\b")),
    ("TD Ins (Canada)",  re.compile(r"\btd\b")),
    ("CAA (Canada)",     re.compile(r"\bcaa\b")),
    ("Connect (AmFam)",  re.compile(r"\bconnect home and auto\b")),
    ("Members (?)",      re.compile(r"\bmembers home and auto\b")),
    ("Choice (?)",       re.compile(r"\bchoice auto and home\b")),
    ("Group (?)",        re.compile(r"\bgroup home and auto\b")),
    ("Clayton (builder)",re.compile(r"\bclayton\b")),
]

KILL_RULES = [
    (re.compile(r"^insure$"),                    "bare ambiguous token -- no intent qualifier"),
    (re.compile(r"^property insurance$"),        "commercial-lines leak risk (out-of-scope product)"),
    (re.compile(r"^(insurance home|insurance house|your home insurance|home homeowner insurance|insurance house car insurance|insurance house auto|auto home owners|homeowners auto)$"),
                                                 "reversed/awkward surface -- rides existing heads as close variant"),
    (re.compile(r"^premium home insurance$"),    "ambiguous ('premium' = cost-of-premium as often as high-end)"),
    (re.compile(r"^(home ins|homeowners ins|home and auto ins|auto home ins|home and car ins)$"),
                                                 "fragment -- MS folds into the full head; no separate target"),
    (re.compile(r"^home and auto insurance in$"),"truncated surface"),
    (re.compile(r"insurnce|isnurance"),          "misspelling -- close-variant matching covers it"),
    (re.compile(r"contents insurance"),          "contents cover (UK/renters flavor) -- poor US home-lead fit"),
    (re.compile(r"insurance home page$"),        "navigational"),
    (re.compile(r"^(need|types) home insurance$"),"informational fragment -- rides phrase heads"),
]
FLAG_RULES = [
    (re.compile(r"\bflorida\b"), "route to the live FLORIDA campaign, not a tier group"),
    (re.compile(r"\bcalifornia\b"), "route to the live CALIFORNIA campaign, not a tier group"),
]

BASE_TOKENS = {"home", "homes", "homeowner", "homeowners", "house", "houses", "owner",
               "owners", "insurance", "ins", "insur", "auto", "car", "cars", "vehicle",
               "vehicles", "automobile", "and", "for", "on", "in", "multi", "new"}
MOBILE_BASE = BASE_TOKENS | {"mobile", "manufactured", "modular", "trailer",
                             "single", "double", "wide", "park"}
MOD_CLASSES = [
    ("Agent",     {"agent", "agents", "agency", "agencies", "broker", "brokers"}),
    ("Quote",     {"quote", "quotes", "quotation", "estimate", "estimates", "online",
                   "instant"}),
    ("Compare",   {"compare", "comparison"}),
    ("Best",      {"best", "top", "rated", "ratings", "rankings", "reviews", "review",
                   "highest", "10", "5"}),
    ("Cheapest",  {"cheap", "cheaper", "cheapest", "affordable", "discount", "lowest",
                   "deal", "deals", "budget", "low"}),
    ("Cost",      {"cost", "costs", "rate", "rates", "pricing", "premium", "average", "much"}),
    ("Companies", {"companies", "company", "carriers", "providers", "provider"}),
]

# theme -> ad-group name, aligned to the GOOGLE generic-expansion taxonomy.
# Two encoded decisions (flip here if overruled):
#   Cheapest stays on the LIVE MS groups (Google folds cheap into Cost-Core);
#   Companies is its own group (Google currently uses Other).
GROUP_FOR = {
    "Core":      "Home-Generic-Home Insurance-Core",
    "Quote":     "Home-Generic-Quote",
    "Best":      "Home-Generic-Compare",
    "Compare":   "Home-Generic-Compare",
    "Cheapest":  "Home-Generic-Home Insurance-Cheapest",
    "Cost":      "Home-Generic-Cost-Core",
    "Companies": "Home-Generic-Companies",
    "Agent":     "Home-Generic-Other",
    "Policy":    "Home-Generic-Other",
}
MIGRATION_LABEL = "restr-2026-09-move"


def mod_class(loose_kw, base=BASE_TOKENS):
    sig = set(loose_kw.split()) - base
    for name, toks in MOD_CLASSES:
        if sig & toks:
            return name, sig
    return "Core", sig


def first_match(rules, loose_kw):
    for rx, reason in rules:
        if rx.search(loose_kw):
            return reason
    return None


def read_planner(path, pull_label):
    rows = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            kw = (row.get("Keyword") or "").strip()
            if not kw:
                continue
            band = (row.get("Average monthly searches") or "").strip()
            rows.append({"kw": kw, "loose": normalize_loose(kw), "n9": normalize_09(kw),
                         "band": band, "vol": VOL.get(band, 0), "pull": pull_label,
                         "bid": row.get("Suggested Bid")})
    return rows


def group_status(conn, name_prefix):
    rows = conn.execute(
        "SELECT ag.ad_group_name, ag.status, c.status AS cstat FROM ad_groups ag "
        "JOIN campaigns c ON c.campaign_id=ag.campaign_id AND c.account_id=ag.account_id "
        "WHERE ag.account_id=? AND ag.ad_group_name LIKE ?", (ACCT, name_prefix + "%")).fetchall()
    if not rows:
        return "NEW -- create"
    if any(r["status"] == "ENABLED" and r["cstat"] == "ENABLED" for r in rows):
        return "LIVE -- add keywords to existing group"
    return "EXISTS DORMANT (%d groups) -- build fresh in live campaigns" % len(rows)


EMIT_VOL = 550

# PHRASE-FIRST MATCH POLICY: exact is EARNED, not granted (the USIA v2 rule --
# promote to exact at proof, ~10 conv at target CPA). Every new target launches as
# PHRASE. The only day-one exacts are the flagship heads the build thesis rests on;
# extend per-run with --exact "term" "term".
FLAGSHIP_EXACT = {"home insurance quote", "home and auto insurance"}


def _stem(t):
    if len(t) > 4 and t.endswith("ies"):
        return t[:-3] + "y"
    if len(t) > 4 and t.endswith(("ses", "xes", "zes", "ches", "shes")):
        return t[:-2]
    if len(t) > 3 and t.endswith("s") and not t.endswith("ss"):
        return t[:-1]
    return t


VARIANT_FOLD = {"house": "home", "houses": "home", "homes": "home",
                "homeowner": "home", "homeowners": "home", "owner": "home",
                "owners": "home", "ins": "insurance", "insur": "insurance"}


def variant_key(loose_kw):
    """Close-variant signature: home-synonyms folded + light plural stem, order-free.
    Two keywords sharing a key are ONE target to MS's matching (synonyms, plurals,
    reorderings) -- keep the highest-volume surface, ride the rest."""
    toks = {_stem(VARIANT_FOLD.get(t, t)) for t in loose_kw.split()}
    return " ".join(sorted(toks))


def match_types_for(term, flagships):
    return ["exact", "phrase"] if term["loose"] in flagships else ["phrase"]


def seed_term(text, by_canon, by_loose, vol):
    sv, sh = verdict_for(by_canon.get(normalize_09(text)) or by_loose.get(text) or [])
    return {"kw": text, "loose": text, "n9": normalize_09(text), "band": "(seeded)",
            "vol": vol, "srcs": {"seed"}, "verdict": sv, "hits": sh, "flag": None,
            "sig": "", "seeded": True}


def consolidate(pool, base, canon_seeds, floor, by_canon, by_loose):
    """Signature consolidation -> (classes dict incl. merged folds, totals, clearing,
    summary rows). canon_seeds = list of canonical head texts to inject into Core."""
    classes = defaultdict(list)
    for t in pool:
        cls, sig = mod_class(t["loose"], base)
        t["sig"] = ",".join(sorted(sig))
        classes[cls].append(t)
    for text in canon_seeds:
        if classes.get("Core") and not any(m["loose"] == text for m in classes["Core"]):
            classes["Core"].append(seed_term(text, by_canon, by_loose,
                                             max(m["vol"] for m in classes["Core"])))
    canon_set = set(canon_seeds)

    def head_of(members):
        def score(m):
            contains = 1 if any(re.search(r"\b%s\b" % re.escape(c), m["loose"])
                                for c in canon_set) else 0
            return (m["loose"] in canon_set, m["vol"], contains, -len(m["loose"]))
        return max(members, key=score)

    totals = {cls: sum(m["vol"] for m in ms if not m.get("seeded"))
              for cls, ms in classes.items()}
    clearing = {cls for cls in classes if cls == "Core" or totals[cls] >= floor}
    summary = []
    for cls, members in sorted(classes.items(), key=lambda kv: -totals[kv[0]]):
        real = [m for m in members if not m.get("seeded")]
        summary.append({"class": cls, "n_terms": len(real), "sum_vol": totals[cls],
                        "decision": "OWN PHRASE HEAD" if cls in clearing else
                                    "FOLD -> Core (below floor %d)" % floor,
                        "head": head_of(members)["kw"] if cls in clearing else "",
                        "members": "; ".join(m["kw"] for m in
                                             sorted(real, key=lambda x: -x["vol"])[:12])
                                   + ("; ..." if len(real) > 12 else "")})
    for cls in [c for c in list(classes) if c not in clearing]:
        classes["Core"].extend(classes.pop(cls))
    return classes, totals, clearing, summary, head_of


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--pull1-csv", required=True, help="head pull (impression-filtered)")
    p.add_argument("--pull2-csv", required=True, help="bundle pull (unfiltered)")
    p.add_argument("--pull3-csv", default=None, help="mobile/manufactured pull -> HOLD sheet")
    p.add_argument("--db", default=r"C:\Projects\google-ads-ops\db\paid_search.db")
    p.add_argument("--tagger-dir", default=None)
    p.add_argument("--bundle-floor", type=int, default=150,
                   help="summed vol_mid a modifier class needs for its own phrase head")
    p.add_argument("--bundle-groups", choices=["collapsed", "expanded"], default="collapsed",
                   help="collapsed (default): 2 bundle groups (Core + Quote); expanded: one per class")
    p.add_argument("--exact", nargs="*", default=None,
                   help="additional flagship heads granted day-one EXACT (phrase-first policy)")
    p.add_argument("--out", default=r"C:\Projects\google-ads-ops\analysis\ms-home-kw-expansion"
                                    r"\output\ms_expansion_selection_%s.xlsx" % date.today().isoformat())
    a = p.parse_args()

    conn = sqlite3.connect("file:%s?mode=ro" % a.db, uri=True)
    conn.row_factory = sqlite3.Row
    tagger, tdir = load_tagger(a.tagger_dir, a.db)
    print("brand tagger: %s" % ("loaded from %s" % tdir if tagger else "NOT FOUND"))
    by_canon, by_loose, n_struct, basis = load_structure(conn)
    print("structure: %s acct-2 rows | %s" % (f"{n_struct:,}", basis))

    raw = read_planner(a.pull1_csv, "pull1") + read_planner(a.pull2_csv, "pull2")
    if a.pull3_csv:
        raw += read_planner(a.pull3_csv, "pull3")
    terms = {}
    for t in raw:
        cur = terms.get(t["loose"])
        if cur is None or t["vol"] > cur["vol"]:
            srcs = (cur["srcs"] if cur else set()) | {t["pull"]}
            t = dict(t); t["srcs"] = srcs; terms[t["loose"]] = t
        else:
            cur["srcs"].add(t["pull"])
    print("planner terms: %d raw -> %d distinct" % (len(raw), len(terms)))
    flagships = set(FLAGSHIP_EXACT) | {normalize_loose(x) for x in (a.exact or [])}
    print("exact policy: PHRASE-FIRST; day-one exact only on flagships: %s"
          % ", ".join(sorted(flagships)))

    excluded, reactivate, active_ref = [], [], []
    pool_core, pool_bundle, pool_mobile = [], [], []
    for t in terms.values():
        loose, n9 = t["loose"], t["n9"]
        brand = bid = ""
        dnb = False
        if tagger:
            brand, bid, _, dnb, _ = tagger.tag(n9)
            if not brand:
                brand, bid, _, dnb, _ = tagger.tag(loose)
        gap = next((nm for nm, rx in REGISTRY_GAPS if rx.search(loose)), None)
        matches = by_canon.get(n9) or by_loose.get(loose) or []
        verdict, hits = verdict_for(matches)
        t.update(brand=brand, bid_status=bid, dnb=int(bool(dnb)), verdict=verdict,
                 hits=hits, flag=first_match(FLAG_RULES, loose))

        if brand:
            excluded.append((t, "BRAND (registry: %s%s) -- conquest/negative decision, not generic"
                             % (brand, ", DNB" if dnb else "")))
        elif gap:
            excluded.append((t, "REGISTRY GAP (%s) -- brand/builder term the tagger missed; route to registry thread" % gap))
        elif FOREIGN_RE.search(loose):
            excluded.append((t, "cross-vertical token -- out of home scope"))
        elif MOBILE_RE.search(loose) and not INS_RE.search(loose):
            excluded.append((t, "home-SHOPPING intent, not insurance (mobile pull; no insurance token)"))
        elif not HOME_RE.search(loose):
            excluded.append((t, "UNQUALIFIED -- no home/property token; cross-vertical leak at these CPCs"))
        elif first_match(KILL_RULES, loose):
            excluded.append((t, "KILL: " + first_match(KILL_RULES, loose)))
        elif verdict == "ACTIVE_TARGET":
            active_ref.append(t)
        elif verdict == "PAUSED_TARGET":
            reactivate.append(t)
        elif MOBILE_RE.search(loose):
            pool_mobile.append(t)
        elif BUNDLE_RE.search(loose):
            pool_bundle.append(t)
        else:
            pool_core.append(t)

    kw_rows, riders, mobile_rows = [], [], []

    # ---- MODULE 1: core-home themed selection (barbell), Google-aligned groups ----
    def theme_core(loose_kw):
        cls, _ = mod_class(loose_kw)
        if cls == "Core" and re.search(r"\b(policy|policies|plans?|coverage)\b", loose_kw):
            return "Policy"
        return cls
    themed = defaultdict(list)
    for t in pool_core:
        themed[theme_core(t["loose"])].append(t)
    gstat_cache = {}
    n_variant_collapsed = 0
    for th, members in sorted(themed.items()):
        members.sort(key=lambda x: -x["vol"])
        gname = GROUP_FOR.get(th, "Home-Generic-Other")
        if gname not in gstat_cache:
            gstat_cache[gname] = group_status(conn, gname)
        gstat = gstat_cache[gname]
        # collapse close-variant synonym surfaces within the theme: ONE target per
        # variant signature (highest volume wins), the rest ride it
        kept, seen_keys = [], {}
        for m in members:
            vk = variant_key(m["loose"])
            if vk in seen_keys and not m["flag"]:
                riders.append({"ad_group": gname, "keyword": m["kw"], "vol_band": m["band"],
                               "rides": "close variant of '%s' (same variant signature)"
                                        % seen_keys[vk]})
                n_variant_collapsed += 1
            else:
                seen_keys.setdefault(vk, m["kw"])
                kept.append(m)
        for m in kept:
            note = "history exists (dormant instance)" if m["verdict"] == "DORMANT_TARGET" else ""
            if m["flag"]:
                note = (note + "; " if note else "") + m["flag"]
            if th != gname.split("-")[-1]:
                note = (note + "; " if note else "") + "theme=%s" % th
            if m["vol"] >= EMIT_VOL or m["flag"]:
                for mt in match_types_for(m, flagships):
                    kw_rows.append({"module": "core", "ad_group": gname, "group_status": gstat,
                                    "keyword": m["kw"], "match_type": mt, "vol_band": m["band"],
                                    "vol_mid": m["vol"], "source": "+".join(sorted(m["srcs"])),
                                    "action": "ADD", "note": note,
                                    "pending_served_check": "yes"})
            else:
                riders.append({"ad_group": gname, "keyword": m["kw"], "vol_band": m["band"],
                               "rides": "phrase heads in this group (below emit floor)"})

    # ---- MIGRATION (wave 2): serving terms whose intent group is being split out
    #      (Quote, Companies) move AFTER the new group proves; never in wave 1. ----
    migration = []
    for t in active_ref:
        th = theme_core(t["loose"])
        if th in ("Quote", "Companies"):
            where = "; ".join(sorted({"%s [%s > %s]" % (h["match_type"],
                        (h["campaign_name"] or "")[:24], (h["ad_group_name"] or "")[:30])
                        for h in t["hits"] if h["is_serving"] == 1}))
            migration.append({"keyword": t["kw"], "vol_band": t["band"],
                              "move_to": GROUP_FOR[th], "label": MIGRATION_LABEL,
                              "current_location": where,
                              "note": "MOVE in wave 2, after the new %s group has data" % th})

    # ---- MODULE 2: bundle consolidation ----
    if pool_bundle:
        classes, totals, clearing, bundle_summary, head_of = consolidate(
            pool_bundle, BASE_TOKENS, ["home and auto insurance"], a.bundle_floor,
            by_canon, by_loose)
        gstat = group_status(conn, "Home-Generic-Home Auto Bundle")
        for cls in [c for c in ("Core", "Quote", "Best", "Compare", "Cheapest",
                                "Companies", "Agent", "Cost") if c in clearing]:
            members = classes[cls]
            tot = sum(m["vol"] for m in members if not m.get("seeded"))
            n_real = sum(1 for m in members if not m.get("seeded"))
            if a.bundle_groups == "collapsed" and cls not in ("Core", "Quote"):
                gname = "Home-Generic-Home Auto Bundle-Core"
            else:
                gname = "Home-Generic-Home Auto Bundle-%s" % ("Core" if cls == "Core" else cls)
            head = head_of(members)
            emit, seen = [], set()
            for m in [head] + sorted((x for x in members if x is not head),
                                     key=lambda x: -x["vol"]):
                if m["loose"] in seen:
                    continue
                if m is head or (m["vol"] >= EMIT_VOL and not m.get("seeded")):
                    emit.append(m); seen.add(m["loose"])
            for m in emit:
                mts = ["exact", "phrase"] if m["loose"] in flagships else ["phrase"]
                note = "consolidates %d surfaces / ~%s searches (class %s)" % (n_real, f"{tot:,}", cls)
                if m.get("seeded"):
                    note = "SEEDED canonical head (absent from planner buckets AND the account); " + note
                if m["verdict"] == "DORMANT_TARGET":
                    note += "; history exists (dormant instance)"
                for mt in mts:
                    kw_rows.append({"module": "bundle", "ad_group": gname, "group_status": gstat,
                                    "keyword": m["kw"], "match_type": mt, "vol_band": m["band"],
                                    "vol_mid": m["vol"], "source": "+".join(sorted(m["srcs"])),
                                    "action": "ADD", "note": note, "pending_served_check": "yes"})
            for r in members:
                if r["loose"] not in seen and not r.get("seeded"):
                    riders.append({"ad_group": gname, "keyword": r["kw"], "vol_band": r["band"],
                                   "rides": "phrase '%s' as close variant" % head["kw"]})
    else:
        bundle_summary = []

    # ---- MODULE 3: mobile/manufactured (HOLD -- RPL gate) ----
    mobile_summary = []
    if pool_mobile:
        classes, totals, clearing, mobile_summary, head_of = consolidate(
            pool_mobile, MOBILE_BASE,
            ["mobile home insurance", "manufactured home insurance"], a.bundle_floor,
            by_canon, by_loose)
        gname = "Home-Generic-Mobile Home Insurance-Core"
        gstat = group_status(conn, "Home-Generic-Mobile Home Insurance")
        for cls in [c for c in ("Core", "Quote", "Best", "Compare", "Cheapest",
                                "Companies", "Agent", "Cost") if c in clearing]:
            members = classes[cls]
            head = head_of(members)
            emit, seen = [], set()
            for m in [head] + sorted((x for x in members if x is not head),
                                     key=lambda x: -x["vol"]):
                if m["loose"] in seen:
                    continue
                if m is head or m.get("seeded") or m["vol"] >= EMIT_VOL:
                    emit.append(m); seen.add(m["loose"])
            for m in emit:
                mts = ["exact", "phrase"] if m.get("seeded") else ["phrase"]
                note = "class %s; HOLD pending mobile-home RPL/payout gate" % cls
                if m.get("seeded"):
                    note = "SEEDED canonical head; " + note
                for mt in mts:
                    mobile_rows.append({"module": "mobile", "ad_group": gname,
                                        "group_status": gstat, "keyword": m["kw"],
                                        "match_type": mt, "vol_band": m["band"],
                                        "vol_mid": m["vol"],
                                        "source": "+".join(sorted(m["srcs"])),
                                        "action": "HOLD", "note": note,
                                        "pending_served_check": "yes"})
    conn.close()

    order = {"NET_NEW": 0, "NEGATIVE_BLOCKED": 1, "PAUSED_TARGET": 2,
             "DORMANT_TARGET": 3, "ACTIVE_TARGET": 4}
    kw_rows.sort(key=lambda x: (x["ad_group"], -x["vol_mid"], x["keyword"], x["match_type"]))

    # ---- console summary ----
    print("\n" + "=" * 78)
    print("  SELECTION SUMMARY (goal: gain impression share PROFITABLY)")
    print("=" * 78)
    print("  targets emitted (kw x match_type rows): %d" % len(kw_rows))
    per_group = defaultdict(int)
    for k in kw_rows:
        per_group[(k["ad_group"], k["group_status"])] += 1
    for (g, s), n in sorted(per_group.items()):
        print("    %-46s %2d rows  [%s]" % (g, n, s))
    print("  wave-2 migration rows (Quote+Companies, label %s): %d" % (MIGRATION_LABEL, len(migration)))
    print("  MOBILE rows on HOLD (RPL gate): %d" % len(mobile_rows))
    print("  reactivations (one status flip): %d" % len(reactivate))
    for t in reactivate:
        where = "; ".join(sorted({"%s [%s > %s]" % (h["match_type"], (h["campaign_name"] or "")[:22],
                                                    (h["ad_group_name"] or "")[:28]) for h in t["hits"]}))
        print("    %-42s %s" % (t["kw"], where))
    print("  riders (NOT targeted; covered by phrase heads): %d (incl. %d close-variant collapses)"
          % (len(riders), n_variant_collapsed))
    print("  excluded/review rows: %d | already-active reference rows: %d"
          % (len(excluded), len(active_ref)))
    gaps = sorted({r.split("(")[1].split(")")[0] for _, r in excluded if r.startswith("REGISTRY GAP")})
    if gaps:
        print("  REGISTRY GAPS to route to the registry thread: %s" % ", ".join(gaps))
    print("  ALL adds pending: served-check (MS search-term load) + negatives overlay.")

    # ---- workbook ----
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.utils import get_column_letter
    HEAD = PatternFill("solid", fgColor="1F3864")
    WHITE = Font(name="Arial", bold=True, color="FFFFFF", size=10)
    BOLD = Font(name="Arial", bold=True, size=10)
    REG = Font(name="Arial", size=10)
    NOTE = Font(name="Arial", size=9, italic=True, color="595959")
    TITLE = Font(name="Arial", bold=True, size=13, color="1F3864")

    def hdr(ws, row, hs):
        for j, h in enumerate(hs, 1):
            c = ws.cell(row, j, h); c.fill = HEAD; c.font = WHITE
            c.alignment = Alignment(horizontal="center", wrap_text=True)

    def sheet_from(wb, title, heading, cols, dicts, widths, note=None):
        ws = wb.create_sheet(title)
        ws["A1"] = heading; ws["A1"].font = TITLE
        ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=len(cols))
        r0 = 3
        if note:
            ws["A2"] = note; ws["A2"].font = NOTE
            ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=len(cols))
            r0 = 4
        hdr(ws, r0, cols)
        r = r0 + 1
        for d in dicts:
            for j, k in enumerate(cols, 1):
                ws.cell(r, j, d.get(k)).font = BOLD if j == 1 else REG
            r += 1
        for j, w in enumerate(widths, 1):
            ws.column_dimensions[get_column_letter(j)].width = w
        ws.freeze_panes = ws.cell(r0 + 1, 1).coordinate
        ws.auto_filter.ref = "A%d:%s%d" % (r0, get_column_letter(len(cols)), max(r - 1, r0))
        return ws

    wb = Workbook(); wb.remove(wb.active)
    ws = wb.create_sheet("Setup")
    ws["A1"] = "MS Home generic expansion -- selection (%s)" % date.today().isoformat()
    ws["A1"].font = TITLE; ws.merge_cells("A1:B1")
    setup = [
        ("Goal", "Gain impression share PROFITABLY. New coverage where the account is absent; "
                 "the Already Active sheet is the bid/rank (not coverage) workstream."),
        ("Naming", "Ad groups aligned to the Google generic-expansion taxonomy (Core / Quote / "
                   "Compare / Cost-Core / Companies / Other). Divergences by design: MS keeps its "
                   "LIVE Cheapest groups (Google folds cheap into Cost-Core); Companies is its own "
                   "group here (recommend porting that split to Google)."),
        ("Routing", "PRODUCT beats INTENT: Peril/Flood/Bundle/Mobile claim their terms before "
                    "intent modifiers. Intent-over-product is a future iteration."),
        ("Intent splits", "Quote and Companies intent leave Core: new terms launch in the new "
                        "groups first; currently-serving terms move in WAVE 2 (Migration sheet, "
                        "label %s) only after the new groups have data." % MIGRATION_LABEL),
        ("Placement", "New groups in the live tier campaigns T1+T2 first; T3/T5 after heads prove. "
                      "State-flagged rows go to the matching live state campaign (FL/CA)."),
        ("Match types", "PHRASE-FIRST: every new target launches phrase; EXACT is earned at proof "
                        "(~10 conv at target CPA), never granted day one. Day-one exact only on "
                        "flagship heads (home insurance quote; home and auto insurance; the mobile "
                        "seeds on the HOLD sheet). ONE target per variant signature -- synonym/"
                        "plural surfaces ride as close variants, never added."),
        ("Mobile", "Mobile/manufactured is documented on the Mobile (HOLD) sheet and EXCLUDED from "
                   "this release pending the mobile-home RPL/payout gate. Real insurance-intent "
                   "demand ~9-12K proxy/mo after stripping home-shopping surfaces."),
        ("Pending", "EVERY add awaits (1) the MS Search Term Report served-check and (2) the "
                    "negatives overlay (MS negatives are not in the DB)."),
        ("Bidding", "Campaigns run TargetCpa portfolios -- new keywords inherit; planner suggested "
                    "bids are untrustworthy, watch realized CPC in week 1."),
    ]
    r = 3
    for k, v in setup:
        ws.cell(r, 1, k).font = BOLD
        c = ws.cell(r, 2, v); c.font = REG; c.alignment = Alignment(wrap_text=True, vertical="top")
        r += 1
    ws.column_dimensions["A"].width = 14; ws.column_dimensions["B"].width = 100

    kcols = ["keyword", "match_type", "ad_group", "group_status", "module", "vol_band",
             "vol_mid", "source", "action", "pending_served_check", "note"]
    sheet_from(wb, "Keywords", "Proposed targets -- Editor-ready (this release)", kcols, kw_rows,
               [36, 10, 40, 34, 8, 10, 8, 12, 8, 10, 52],
               note="Campaign placement per Setup. Sorted by ad group. Mobile is NOT here -- see Mobile (HOLD).")
    sheet_from(wb, "Migration (wave 2)", "Serving terms to MOVE into their split-out intent groups",
               ["keyword", "vol_band", "move_to", "label", "current_location", "note"],
               migration, [32, 10, 22, 20, 80, 40],
               note="Wave 2 only: move after the destination group has performance. Label mirrors the Google migration.")
    rrows = [{"keyword": t["kw"], "vol_band": t["band"],
              "current_location": "; ".join(sorted({"%s/%s [%s > %s]"
                    % (h["match_type"], h["keyword_status"], (h["campaign_name"] or "")[:24],
                       (h["ad_group_name"] or "")[:30]) for h in t["hits"]}))}
             for t in sorted(reactivate, key=lambda x: -x["vol"])]
    sheet_from(wb, "Reactivations", "Paused keywords under LIVE parents -- one status flip",
               ["keyword", "vol_band", "current_location"], rrows, [36, 10, 90])
    sheet_from(wb, "Bundle Consolidation", "Pull-2 signature consolidation by intent class",
               ["class", "n_terms", "sum_vol", "decision", "head", "members"],
               bundle_summary, [12, 8, 9, 30, 34, 90],
               note="sum_vol = band midpoints (proxy). Non-clearing classes fold to Core.")
    sheet_from(wb, "Mobile (HOLD)", "Mobile/manufactured build -- DOCUMENTED, NOT IN THIS RELEASE",
               kcols, mobile_rows, [36, 10, 42, 30, 8, 10, 8, 12, 8, 10, 52],
               note="HOLD pending the mobile-home RPL/payout gate. Flip action to ADD and merge into "
                    "Keywords when the gate clears.")
    if mobile_summary:
        sheet_from(wb, "Mobile Consolidation", "Pull-3 signature consolidation by intent class",
                   ["class", "n_terms", "sum_vol", "decision", "head", "members"],
                   mobile_summary, [12, 8, 9, 30, 34, 90])
    sheet_from(wb, "Riders", "NOT targeted -- covered by phrase heads as close variants",
               ["ad_group", "keyword", "vol_band", "rides"], riders, [40, 36, 10, 52])
    erows = [{"keyword": t["kw"], "vol_band": t["band"], "source": "+".join(sorted(t["srcs"])),
              "reason": reason} for t, reason in
             sorted(excluded, key=lambda x: (x[1].split(":")[0].split(" ")[0], -x[0]["vol"]))]
    sheet_from(wb, "Excluded & Review", "Killed / quarantined / out-of-scope, with reasons",
               ["keyword", "vol_band", "source", "reason"], erows, [38, 10, 12, 84])
    arows = [{"keyword": t["kw"], "vol_band": t["band"],
              "live_location": "; ".join(sorted({"%s [%s > %s]" % (h["match_type"],
                    (h["campaign_name"] or "")[:24], (h["ad_group_name"] or "")[:30])
                    for h in t["hits"] if h["is_serving"] == 1}))}
             for t in sorted(active_ref, key=lambda x: -x["vol"])]
    sheet_from(wb, "Already Active", "Planner terms already serving -- bid/rank workstream, not coverage",
               ["keyword", "vol_band", "live_location"], arows, [36, 10, 90],
               note="~0% impr share on several serving exacts: the profitable-share lever here is "
                    "Ad Rank / bids, not keyword adds.")
    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    wb.save(a.out)
    base = os.path.splitext(a.out)[0]
    with open(base + "_keywords.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=kcols)
        w.writeheader(); w.writerows(kw_rows)
    print("\nwrote %s and %s_keywords.csv (mobile HOLD rows excluded from the CSV)" % (a.out, base))


if __name__ == "__main__":
    main()
