#!/usr/bin/env python3
"""spiral_ratchet.py -- Workstream 3: Ad Rank spiral assembly, the ratchet quantified.
HYPOTHESIS TO TEST, NOT ASSUME. Explanation only; no recommendations.

Chain under test: L1 low targets -> deep participation; L2 Mar->mid-May raises ->
participation loss with lag; L3 adverse survivorship (low-QS flagship mass) ->
elevated CPC; L4 CPC vs fixed targets -> realized below target -> controller
contracts; L5 contraction starves click accrual -> QS/Ad-Rank decay -> CPC up ->
loop. Jun-Aug fall-through-floor under frozen targets = candidate self-propulsion.

CONSTRAINT RULING (8/24): the account sits below Google's 10% IS reporting floor
for the entire record -> IS channel CENSORED, eligible-impressions proxy DEAD,
UI pulls demoted to optional. Demand-vs-share adjudication replaced by:
 (2) CPC-on-survivors trajectory (ratchet column)
 (3) exit-ordering test: does early QS predict home-intent keyword death order?
     rank-ordered exit = share/rank loss; proportional = demand
 (4) top/absolute-top impression RATES from agp (serving-conditioned, immune to
     the IS floor) -- declining = direct position decay
 (5) Google Trends overlay via --trends-csv (account-independent demand +
     first real seasonality test)
L2 status stays PENDING until (2)-(5) land in combination.

LIMITATION (state verbatim in deliverable): "Search impression share sits below
Google's 10% reporting floor for the entire observable record; the IS channel is
censored and the eligible-impressions proxy is uninformative. Demand-vs-share is
adjudicated through serving-conditioned and account-independent instruments."

Outputs (--out-dir):
  ws3_ratchet_table.csv     month x (campaign|ACCOUNT): target_in_force,
                            realized_roas, roas_gap, HI/total impressions +
                            index vs Dec25-Jan26 baseline, within-class CPC
                            (HI, FL) + Y/Y, iw_QS_HI + rated_share, spend/impr
                            share on QS<=4 (rated rows), position rates if agp
                            has them. Read across = state; down = ratchet.
  ws3_exit_ordering.csv     HI instances: early iw_QS (Feb-Apr) vs survival
  ws3_position_rates.csv    monthly top/abs-top rates (if columns exist)
  ws3_trends_overlay.csv    trends vs account HI impressions (if --trends-csv)
  charts/                   ratchet per campaign+account, exit scatter,
                            five-link loop diagram w/ status, trends overlay
  console                   verdict blocks V1 (lag), V2 (freeze), V3 (mix
                            guard), exit-ordering, position, alternatives

Iron rules carried: QS from keyword_quality_daily only, daily-grain join, iw_QS
weighted-sum over rated rows, rated_share alongside, device='ALL', kpd scoped
via keyword_instances, read-only, ids normalized (TEXT campaign_id).

Run: python spiral_ratchet.py --db C:\\Projects\\google-ads-ops\\db\\paid_search.db
     [--trends-csv path\\to\\multiTimeline.csv]
"""
import argparse, csv, math, os, sqlite3, sys
from collections import defaultdict

ACCOUNT_ID = 1
import re
PRODUCT = re.compile(r"\b(home|homeowner|homeowners|house)\b")
HOME, FLAG, GEN = "HOME_INTENT", "FLAGSHIP", "GENERIC"
CLASSES = (HOME, FLAG, GEN)
CORE = {23385188622: "CA", 23394808297: "TX", 23394808300: "FL",
        23428389081: "T4", 23496334752: "T1", 23530860942: "T2", 23586637678: "T3"}
BASELINE = ("2025-12", "2026-01")
LIMITATION = ("LIMITATION: Search impression share sits below Google's 10% reporting floor "
              "for the entire observable record; the IS channel is censored and the "
              "eligible-impressions proxy is uninformative. Demand-vs-share is adjudicated "
              "through serving-conditioned and account-independent instruments.")


def cols(conn, t):
    try:
        return [r[1] for r in conn.execute("PRAGMA table_info(%s)" % t)]
    except sqlite3.Error:
        return []


def pick(av, cands, what, t, required=True):
    low = {c.lower(): c for c in av}
    for c in cands:
        if c.lower() in low:
            return low[c.lower()]
    if required:
        sys.exit("FATAL: no %s on %s (has: %s)" % (what, t, ", ".join(av) or "missing table"))
    return None


def nid(x):
    if isinstance(x, str) and x.strip().isdigit():
        return int(x.strip())
    return x


def d(v):
    return v or 0


def rd(x, n=4):
    return round(x, n) if isinstance(x, float) else x


def spearman(xs, ys):
    def ranks(v):
        order = sorted(range(len(v)), key=lambda i: v[i])
        r = [0.0] * len(v)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and v[order[j + 1]] == v[order[i]]:
                j += 1
            avg = (i + j) / 2.0 + 1
            for k in range(i, j + 1):
                r[order[k]] = avg
            i = j + 1
        return r
    rx, ry = ranks(xs), ranks(ys)
    n = len(xs)
    mx, my = sum(rx) / n, sum(ry) / n
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    den = math.sqrt(sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry))
    return num / den if den else 0.0


def classify(text, brand_tokens):
    t = (text or "").lower()
    if PRODUCT.search(t):
        return HOME
    if any(p in t for p in brand_tokens[0]) or any(w in brand_tokens[1] for w in t.split()):
        return FLAG
    return GEN


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--start", default="2025-01-01", help="pull start (2025 needed for Y/Y CPC)")
    ap.add_argument("--end", default="2026-12-31")
    ap.add_argument("--table-start", default="2026-01", help="first month shown in ratchet table")
    ap.add_argument("--min-march-impr", type=int, default=300)
    ap.add_argument("--trends-csv", default=None)
    ap.add_argument("--out-dir", default=r"C:\Projects\google-ads-ops\analysis\google-home-april-collapse\ws3-output")
    a = ap.parse_args()
    conn = sqlite3.connect("file:%s?mode=ro" % a.db, uri=True)
    os.makedirs(a.out_dir, exist_ok=True)
    hdr = ["spiral_ratchet.py -- WS3 run header", LIMITATION, ""]
    print(LIMITATION + "\n")

    # ---- resolve sources ------------------------------------------------------
    for t, need in (("keyword_performance_daily", ["instance_id", "report_date", "device",
                                                   "impressions", "clicks", "cost_micros"]),
                    ("keyword_quality_daily", ["instance_id", "report_date", "historical_quality_score"])):
        have = cols(conn, t)
        miss = [c for c in need if c not in have]
        if miss:
            sys.exit("FATAL: %s missing %s" % (t, miss))
    ki = cols(conn, "keyword_instances")
    ki_camp = "campaign_id" if "campaign_id" in ki else None
    ag = cols(conn, "ad_groups")
    ag_key = pick(ag, ["id", "ad_group_id"], "ad_groups key", "ad_groups") if not ki_camp else None

    cpd_t = None
    for cand in ("campaign_performance_daily", "campaign_daily", "campaign_performance"):
        if cols(conn, cand):
            cpd_t = cand
            break
    src = {}
    if cpd_t:
        cc = cols(conn, cpd_t)
        src["cpd_date"] = pick(cc, ["report_date", "date", "day"], "date", cpd_t)
        src["cpd_cost"] = pick(cc, ["cost_micros", "cost"], "cost", cpd_t)
        src["cpd_rev"] = pick(cc, ["primary_conv_value", "conv_value", "revenue", "conversion_value"],
                              "revenue", cpd_t, required=False)
        src["cpd_camp"] = pick(cc, ["campaign_id"], "campaign key", cpd_t)
        src["cpd_dev"] = pick(cc, ["device"], "device", cpd_t, required=False)
        src["cpd_plat"] = pick(cc, ["platform"], "platform", cpd_t, required=False)
    cce = cols(conn, "campaign_change_events")
    src["cce_date"] = pick(cce, ["event_date", "change_date", "event_time", "changed_at",
                                 "occurred_at", "report_date", "created_at", "date"], "date",
                           "campaign_change_events")
    src["cce_new"] = pick(cce, ["new_value", "new_target"], "new_value", "campaign_change_events",
                          required=False)
    agp = cols(conn, "ad_group_performance_daily")
    agp_date = pick(agp, ["report_date", "date", "report_day", "day"], "date",
                    "ad_group_performance_daily", required=False)
    agp_dev = pick(agp, ["device", "segment_device"], "device", "ad_group_performance_daily",
                   required=False)
    pos_cols = [c for c in agp if re.search(r"(absolute_)?top_impression_(rate|share)|abs_top", c, re.I)]
    hdr.append("resolved: cpd=%s rev_col=%s | agp position columns: %s"
               % (cpd_t, src.get("cpd_rev"), pos_cols or "NONE (item 4 -> not available in DB)"))
    print(hdr[-1])

    # brand vocab from classification tables (already rebuilt 8/24)
    qcols = cols(conn, "query_classification")
    bcol = pick(qcols, ["brand", "brand_name"], "brand", "query_classification")
    vocab = set(r[0].strip().lower() for r in conn.execute(
        "SELECT DISTINCT %s FROM query_classification WHERE %s IS NOT NULL AND %s != ''"
        % (bcol, bcol, bcol)) if r[0])
    phrases = [b for b in vocab if " " in b]
    tokens = set(b.split(" ")[0] for b in vocab) | set(b.replace(" ", "") for b in vocab)
    brand_tokens = (phrases, tokens)

    # ---- Pull A: kpd class pass + HI instance grain for exit test -------------
    ki_join = "ki.campaign_id" if ki_camp else "ag.campaign_id"
    ag_join = "" if ki_camp else (" JOIN ad_groups ag ON ag.%s = ki.ad_group_id " % ag_key)
    K = defaultdict(lambda: defaultdict(float))        # (ym,cid,class)
    inst = defaultdict(lambda: defaultdict(float))     # HI exit test: iid -> fields
    inst_m = defaultdict(lambda: defaultdict(float))   # (iid, ym) -> impr
    kclass = {}
    q = ("SELECT SUBSTR(p.report_date,1,7) ym, %s cid, p.instance_id, LOWER(ki.keyword_text), "
         " SUM(p.impressions), SUM(p.clicks), SUM(p.cost_micros), "
         " SUM(CASE WHEN q.historical_quality_score IS NOT NULL THEN p.impressions ELSE 0 END), "
         " SUM(CASE WHEN q.historical_quality_score IS NOT NULL THEN q.historical_quality_score*p.impressions END), "
         " SUM(CASE WHEN q.historical_quality_score IS NOT NULL THEN p.cost_micros ELSE 0 END), "
         " SUM(CASE WHEN q.historical_quality_score <= 4 THEN p.impressions ELSE 0 END), "
         " SUM(CASE WHEN q.historical_quality_score <= 4 THEN p.cost_micros ELSE 0 END) "
         " FROM keyword_performance_daily p "
         " JOIN keyword_instances ki ON ki.id = p.instance_id AND ki.account_id = ? %s"
         " LEFT JOIN keyword_quality_daily q ON q.instance_id = p.instance_id "
         "   AND q.report_date = p.report_date "
         " WHERE p.device='ALL' AND p.report_date BETWEEN ? AND ? GROUP BY 1,2,3"
         % (ki_join, ag_join))
    for ym, cid, iid, text, im, ck, cm, ri, qsw, rcost, l4i, l4c in conn.execute(
            q, (ACCOUNT_ID, a.start, a.end)):
        cid = nid(cid)
        cl = kclass.get(iid)
        if cl is None:
            cl = kclass[iid] = classify(text, brand_tokens)
        x = K[(ym, cid, cl)]
        x["impr"] += d(im); x["clicks"] += d(ck); x["cost"] += d(cm) / 1e6
        x["ri"] += d(ri); x["qsw"] += d(qsw); x["rcost"] += d(rcost) / 1e6
        x["l4i"] += d(l4i); x["l4c"] += d(l4c) / 1e6
        if cl == HOME and ym >= "2026-01":
            inst_m[(iid, ym)]["impr"] += d(im)
            if "2026-02" <= ym <= "2026-04":
                inst[iid]["eqs_w"] += d(qsw); inst[iid]["eqs_i"] += d(ri)
            if ym == "2026-03":
                inst[iid]["march"] += d(im)
            inst[iid]["text"] = text; inst[iid]["cid"] = cid

    # account rollup
    A = defaultdict(lambda: defaultdict(float))
    for (ym, cid, cl), x in K.items():
        for k, v in x.items():
            A[(ym, cl)][k] += v

    # ---- Pull B: realized ROAS ------------------------------------------------
    R = {}
    if cpd_t and src.get("cpd_rev"):
        w = []
        if src.get("cpd_dev"):
            w.append("p.%s='ALL'" % src["cpd_dev"])
        if src.get("cpd_plat"):
            w.append("LOWER(p.%s)='google'" % src["cpd_plat"])
        wh = (" AND " + " AND ".join(w)) if w else ""
        qq = ("SELECT SUBSTR(p.%s,1,7), p.%s, SUM(p.%s), SUM(p.%s) FROM %s p "
              " JOIN campaigns c ON c.campaign_id = p.%s AND c.account_id = ? "
              " WHERE p.%s BETWEEN ? AND ? %s GROUP BY 1,2"
              % (src["cpd_date"], src["cpd_camp"], src["cpd_cost"], src["cpd_rev"], cpd_t,
                 src["cpd_camp"], src["cpd_date"], wh))
        for ym, cid, cost, rev in conn.execute(qq, (ACCOUNT_ID, a.start, a.end)):
            cost = d(cost) / (1e6 if "micros" in src["cpd_cost"] else 1.0)
            R[(ym, nid(cid))] = (cost, d(rev))
    else:
        hdr.append("WARNING: no campaign-grain revenue source resolved -- realized ROAS omitted; "
                   "pin the table/columns and rerun")

    # ---- Pull C: targets step function + freeze -------------------------------
    events = []
    qq = ("SELECT e.%s, e.campaign_id, %s FROM campaign_change_events e "
          " JOIN campaigns c ON c.campaign_id = e.campaign_id AND c.account_id = ? "
          " WHERE e.change_type='TARGET_ROAS' ORDER BY 1"
          % (src["cce_date"], ("e.%s" % src["cce_new"]) if src["cce_new"] else "NULL"))
    for dt, cid, nv in conn.execute(qq, (ACCOUNT_ID,)):
        try:
            nvf = float(str(nv).replace("%", "")) if nv is not None else None
        except ValueError:
            nvf = None
        events.append((str(dt)[:10], nid(cid), nvf))
    months = sorted(set(ym for (ym, cid, cl) in K if ym >= a.table_start))
    tgt = {}
    for cid in set(e[1] for e in events):
        cur = None
        evs = [e for e in events if e[1] == cid]
        i = 0
        for ym in months:
            while i < len(evs) and evs[i][0][:7] <= ym:
                if evs[i][2] is not None:
                    cur = evs[i][2]
                i += 1
            tgt[(ym, cid)] = cur
    last_ev = max(e[0] for e in events) if events else None
    per_last = {cid: max(e[0] for e in events if e[1] == cid) for cid in set(e[1] for e in events)}

    # ---- Pull D: position rates (item 4) --------------------------------------
    POS = defaultdict(lambda: defaultdict(float))
    if pos_cols and agp_date:
        sel = ", ".join("SUM(p.impressions * COALESCE(p.%s,0))" % c for c in pos_cols)
        wdev = ("p.%s='ALL' AND " % agp_dev) if agp_dev else ""
        qq = ("SELECT SUBSTR(p.%s,1,7) ym, ag.campaign_id, SUM(p.impressions), %s "
              " FROM ad_group_performance_daily p JOIN ad_groups ag ON ag.%s = p.ad_group_id "
              " JOIN campaigns c ON c.campaign_id = ag.campaign_id AND c.account_id = ? "
              " WHERE %s p.%s BETWEEN ? AND ? GROUP BY 1,2"
              % (agp_date, sel, ag_key or "id", wdev, agp_date))
        try:
            for row in conn.execute(qq, (ACCOUNT_ID, a.start, a.end)):
                ym, cid, im = row[0], nid(row[1]), d(row[2])
                x = POS[(ym, cid)]
                x["impr"] += im
                for j, c in enumerate(pos_cols):
                    x[c] += d(row[3 + j])
        except sqlite3.Error as e:
            hdr.append("position pull failed: %r" % e)
            POS = {}

    # ---- ratchet table ---------------------------------------------------------
    def base_val(cid, cl, key):
        vals = [K.get((bm, cid, cl), {}).get(key, 0.0) for bm in BASELINE] if cid else \
               [A.get((bm, cl), {}).get(key, 0.0) for bm in BASELINE]
        return sum(vals) / len(BASELINE)

    def cell(ym, cid):
        get = (lambda cl: K.get((ym, cid, cl), defaultdict(float))) if cid else \
              (lambda cl: A.get((ym, cl), defaultdict(float)))
        h, f = get(HOME), get(FLAG)
        tot_i = sum(get(c)["impr"] for c in CLASSES)
        tot_c = sum(get(c)["cost"] for c in CLASSES)
        ri, qsw = h["ri"], h["qsw"]
        r4i = sum(get(c)["l4i"] for c in CLASSES)
        r4c = sum(get(c)["l4c"] for c in CLASSES)
        rri = sum(get(c)["ri"] for c in CLASSES)
        rrc = sum(get(c)["rcost"] for c in CLASSES)
        cost, rev = (R.get((ym, cid), (None, None)) if cid else
                     (sum(v[0] for k, v in R.items() if k[0] == ym),
                      sum(v[1] for k, v in R.items() if k[0] == ym)) if R else (None, None))
        roas = (rev / cost) if (cost and rev is not None) else None
        t = tgt.get((ym, cid)) if cid else None
        prev = "%d-%s" % (int(ym[:4]) - 1, ym[5:7])
        ph = K.get((prev, cid, HOME), {}) if cid else A.get((prev, HOME), {})
        pf = K.get((prev, cid, FLAG), {}) if cid else A.get((prev, FLAG), {})
        cpc_h = h["cost"] / h["clicks"] if h["clicks"] else None
        cpc_f = f["cost"] / f["clicks"] if f["clicks"] else None
        pch = (ph.get("cost", 0) / ph["clicks"]) if ph.get("clicks") else None
        pcf = (pf.get("cost", 0) / pf["clicks"]) if pf.get("clicks") else None
        bh, bt = base_val(cid, HOME, "impr"), sum(base_val(cid, c, "impr") for c in CLASSES)
        pos = POS.get((ym, cid), {}) if cid else None
        if not cid and POS:
            pos = defaultdict(float)
            for (ym2, c2), x in POS.items():
                if ym2 == ym:
                    for k, v in x.items():
                        pos[k] += v
        row = [ym, cid or "", (CORE.get(cid) or str(cid)) if cid else "ACCOUNT",
               t if t is not None else "",
               rd(roas, 3) if roas is not None else "",
               rd(roas - t, 3) if (roas is not None and t) else "",
               int(h["impr"]), rd(h["impr"] / bh, 2) if bh else "",
               int(tot_i), rd(tot_i / bt, 2) if bt else "",
               rd(cpc_h, 2) if cpc_h else "", rd(cpc_f, 2) if cpc_f else "",
               rd((cpc_h / pch - 1) * 100, 1) if (cpc_h and pch) else "",
               rd((cpc_f / pcf - 1) * 100, 1) if (cpc_f and pcf) else "",
               rd(qsw / ri, 2) if ri else "", rd(ri / h["impr"], 3) if h["impr"] else "",
               rd(r4i / rri, 3) if rri else "", rd(r4c / rrc, 3) if rrc else ""]
        for c in pos_cols:
            row.append(rd(pos[c] / pos["impr"], 3) if pos and pos.get("impr") else "")
        return row

    header = ["month", "campaign_id", "campaign", "target_in_force", "realized_roas",
              "roas_gap", "HI_impr", "HI_idx_vs_base", "total_impr", "total_idx_vs_base",
              "cpc_HI", "cpc_FL", "cpc_HI_yoy_pct", "cpc_FL_yoy_pct", "iw_qs_HI",
              "rated_share_HI", "qs_le4_impr_share", "qs_le4_spend_share"] + \
             ["pos_" + c for c in pos_cols]
    rows = []
    for ym in months:
        rows.append(cell(ym, None))
    for cid in CORE:
        for ym in months:
            if any((ym, cid, cl) in K for cl in CLASSES):
                rows.append(cell(ym, cid))
    path = os.path.join(a.out_dir, "ws3_ratchet_table.csv")
    with open(path, "w", newline="", encoding="utf-8") as fo:
        w = csv.writer(fo); w.writerow(header); w.writerows(rows)
    print("wrote %s (%d rows)" % (path, len(rows)))

    # ---- exit-ordering test ----------------------------------------------------
    ex_rows, xs, ys = [], [], []
    unrated = 0
    for iid, x in inst.items():
        if x.get("march", 0) < a.min_march_impr:
            continue
        surv = 0
        death = ""
        dead_run = 0
        for ym in ("2026-05", "2026-06", "2026-07", "2026-08"):
            im = inst_m.get((iid, ym), {}).get("impr", 0)
            if im >= 0.25 * x["march"]:
                surv += 1
                dead_run = 0
            else:
                dead_run += 1
                if not death:
                    death = ym
        if dead_run == 0:
            death = ""  # alive at end
        eqs = (x["eqs_w"] / x["eqs_i"]) if x.get("eqs_i") else None
        if eqs is None:
            unrated += 1
        else:
            xs.append(eqs); ys.append(surv)
        ex_rows.append([iid, x.get("text", ""), CORE.get(x.get("cid"), x.get("cid")),
                        int(x["march"]), rd(eqs, 2) if eqs else "", surv, death])
    path = os.path.join(a.out_dir, "ws3_exit_ordering.csv")
    with open(path, "w", newline="", encoding="utf-8") as fo:
        w = csv.writer(fo)
        w.writerow(["instance_id", "keyword", "campaign", "march_impr",
                    "early_iwqs_FebApr", "months_survived_MayAug", "death_month"])
        w.writerows(sorted(ex_rows, key=lambda r: (r[5], -(r[3]))))
    print("wrote %s (%d instances, %d unrated excluded from rank test)"
          % (path, len(ex_rows), unrated))
    rho = spearman(xs, ys) if len(xs) >= 8 else None
    quart = ""
    if xs:
        srt = sorted(zip(xs, ys))
        qn = max(1, len(srt) // 4)
        parts = [srt[i * qn:(i + 1) * qn if i < 3 else len(srt)] for i in range(4)]
        quart = " | ".join("Q%d(qs<=%.1f): surv %.2f" % (i + 1, p[-1][0],
                           sum(y for _, y in p) / len(p)) for i, p in enumerate(parts) if p)

    # ---- trends overlay --------------------------------------------------------
    trends = {}
    if a.trends_csv and os.path.exists(a.trends_csv):
        with open(a.trends_csv, encoding="utf-8-sig") as fo:
            rdr = list(csv.reader(fo))
        hi_row = next((i for i, r in enumerate(rdr)
                       if r and r[0].strip().lower() in ("month", "week")), None)
        if hi_row is not None:
            names = rdr[hi_row][1:]
            for r in rdr[hi_row + 1:]:
                if not r or not r[0]:
                    continue
                ym = r[0][:7]
                for j, nm in enumerate(names):
                    try:
                        trends.setdefault(nm, defaultdict(list))[ym].append(float(r[1 + j].replace("<1", "0.5")))
                    except (ValueError, IndexError):
                        pass
            trows = []
            tmonths = sorted(set(m for s in trends.values() for m in s))
            for ym in tmonths:
                row = [ym] + [rd(sum(trends[nm][ym]) / len(trends[nm][ym]), 1)
                              if ym in trends[nm] else "" for nm in names]
                row.append(int(A.get((ym, HOME), {}).get("impr", 0)))
                trows.append(row)
            path = os.path.join(a.out_dir, "ws3_trends_overlay.csv")
            with open(path, "w", newline="", encoding="utf-8") as fo:
                w = csv.writer(fo)
                w.writerow(["month"] + names + ["acct_HI_impr_kpd"])
                w.writerows(trows)
            print("wrote %s" % path)
        else:
            print("trends csv: could not find Month/Week header row -- skipped")

    # ---- verdict blocks --------------------------------------------------------
    ev_m = defaultdict(int)
    for dt, cid, nv in events:
        ev_m[dt[:7]] += 1
    print("\n" + "=" * 78)
    print("V1 -- LAG STRUCTURE (n=%d months; not statistically resolvable, shown aligned)"
          % len(months))
    for ym in months:
        h = A.get((ym, HOME), {})
        b = sum(A.get((bm, HOME), {}).get("impr", 0) for bm in BASELINE) / 2
        print("  %s  target events: %2d   HI idx vs base: %s"
              % (ym, ev_m.get(ym, 0), rd(h.get("impr", 0) / b, 2) if b else "?"))
    print("V2 -- FREEZE: last TARGET_ROAS event account-wide %s; per campaign: %s"
          % (last_ev, ", ".join("%s %s" % (CORE.get(c, c), dt) for c, dt in sorted(
              per_last.items(), key=lambda kv: str(kv[0])))))
    print("V3 -- MIX GUARD: within-class CPC Y/Y is in the ratchet table "
          "(cpc_HI_yoy_pct / cpc_FL_yoy_pct); elevation within BOTH classes = not mix.")
    print("EXIT-ORDERING: spearman(early_QS, months_survived) = %s on %d rated instances"
          % (rd(rho, 3) if rho is not None else "n<8, not computed", len(xs)))
    if quart:
        print("  quartiles: " + quart)
    print("  read: positive rho (low QS dies first) = rank-ordered exit = SHARE/RANK loss;")
    print("        rho ~ 0 (proportional shrink) = DEMAND-side; negative = investigate.")
    if pos_cols:
        print("POSITION RATES: columns %s in ratchet table -- declining trend = direct "
              "position decay (serving-conditioned, floor-immune)." % pos_cols)
    else:
        print("POSITION RATES: no top/abs-top rate columns in ad_group_performance_daily -- "
              "item 4 not available in DB; note for sync wishlist.")
    print("\nALTERNATIVES (kill-conditions for the spiral):")
    print("  demand normalization -- trends overlay flat-while-we-fell kills it for the")
    print("    spiral; trends falling ~proportionally supports it. Status: %s"
          % ("COMPUTED (see overlay)" if trends else "PENDING --trends-csv"))
    print("  seasonality -- first real test = trends 2025-vs-2026 same-month shape; both")
    print("    in-account prior windows confounded. Status: %s"
          % ("COMPUTED" if trends else "PENDING --trends-csv"))
    print("  query-mix drift -- adjudicated by exit-ordering above (uniform-vs-ranked).")
    print("L2 STATUS: PENDING until CPC trajectory + exit-ordering + position + trends")
    print("read together; components above.")
    print("=" * 78)

    # ---- charts ----------------------------------------------------------------
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        cd = os.path.join(a.out_dir, "charts")
        os.makedirs(cd, exist_ok=True)
        # ratchet: account + per campaign
        def ratchet_chart(cid, label):
            ms = [ym for ym in months]
            hi = [(K.get((ym, cid, HOME), {}) if cid else A.get((ym, HOME), {})).get("impr", 0)
                  for ym in ms]
            cpc = [((K.get((ym, cid, HOME), {}) if cid else A.get((ym, HOME), {}))) for ym in ms]
            cpc = [x.get("cost", 0) / x["clicks"] if x.get("clicks") else None for x in cpc]
            fig, ax = plt.subplots(figsize=(10, 5))
            ax.bar(ms, hi, alpha=0.35, label="HI impressions")
            ax2 = ax.twinx()
            ax2.plot(ms, cpc, marker="o", color="tab:red", label="HI CPC")
            if cid:
                tt = [tgt.get((ym, cid)) for ym in ms]
                rr = [(R.get((ym, cid), (None, None))) for ym in ms]
                rr = [(v[1] / v[0]) if (v[0] and v[1] is not None) else None for v in rr]
                ax2.plot(ms, tt, ls="--", color="0.3", label="target")
                ax2.plot(ms, rr, ls=":", color="tab:green", marker="s", label="realized ROAS")
            ax.set_title("%s: the ratchet -- volume, CPC, target vs realized" % label)
            h1, l1 = ax.get_legend_handles_labels(); h2, l2 = ax2.get_legend_handles_labels()
            ax.legend(h1 + h2, l1 + l2, fontsize=8)
            fig.tight_layout(); fig.savefig(os.path.join(cd, "ratchet_%s.png" % label), dpi=150)
            plt.close(fig)
        ratchet_chart(None, "ACCOUNT")
        for cid, nm in CORE.items():
            if any((ym, cid, HOME) in K for ym in months):
                ratchet_chart(cid, nm)
        # exit scatter
        if xs:
            fig, ax = plt.subplots(figsize=(7, 5))
            ax.scatter(xs, ys, alpha=0.5)
            ax.set_xlabel("early iw_QS (Feb-Apr)"); ax.set_ylabel("months survived May-Aug")
            ax.set_title("Exit ordering: does QS predict death order? rho=%s"
                         % (rd(rho, 3) if rho is not None else "n/a"))
            fig.tight_layout(); fig.savefig(os.path.join(cd, "exit_ordering.png"), dpi=150)
            plt.close(fig)
        # loop diagram
        fig, ax = plt.subplots(figsize=(11, 8))
        ax.axis("off")
        nodes = [
            ("L1 participation era\nlow targets 1.00-1.10\n[MEASURED: Jan cheap YoY,\nsurge 2.2x baseline]", "#cde"),
            ("L2 raises -> exit w/ lag\nMar/Apr/May batches\n[PENDING: CPC traj +\nexit-order + position + trends]", "#fed"),
            ("L3 adverse survivorship\nlow-QS flagship mass\n[see qs<=4 spend share col]", "#fdd"),
            ("L4 CPC vs fixed targets\nrealized < target\n[gap column; A5 trough\ngeometry: 6/7 below BE]", "#fdd"),
            ("L5 contraction -> accrual\nstarves -> QS decay -> CPC up\n[Jun-Aug fall under FREEZE\n(last events 5/14, T1 6/25)]", "#dfd"),
        ]
        import math as m2
        for i, (txt, col) in enumerate(nodes):
            ang = m2.pi / 2 - 2 * m2.pi * i / 5
            x, y = 0.5 + 0.38 * m2.cos(ang), 0.5 + 0.38 * m2.sin(ang)
            ax.annotate(txt, (x, y), ha="center", va="center", fontsize=9,
                        bbox=dict(boxstyle="round,pad=0.5", fc=col, ec="0.4"))
            ang2 = m2.pi / 2 - 2 * m2.pi * ((i + 1) % 5) / 5
            x2, y2 = 0.5 + 0.30 * m2.cos(ang2), 0.5 + 0.30 * m2.sin(ang2)
            xs_, ys_ = 0.5 + 0.30 * m2.cos(ang), 0.5 + 0.30 * m2.sin(ang)
            ax.annotate("", xy=(x2, y2), xytext=(xs_, ys_),
                        arrowprops=dict(arrowstyle="->", color="0.4", lw=1.5,
                                        connectionstyle="arc3,rad=0.25"))
        ax.set_title("Ad Rank spiral -- five-link chain, evidence status per link")
        fig.savefig(os.path.join(cd, "spiral_loop_diagram.png"), dpi=150)
        plt.close(fig)
        # trends overlay chart
        if trends:
            fig, ax = plt.subplots(figsize=(11, 5))
            tm = sorted(set(m for s in trends.values() for m in s))
            for nm, s in trends.items():
                base = [sum(s[m2_]) / len(s[m2_]) for m2_ in tm if m2_ in s][:1]
                vals = [(sum(s[m2_]) / len(s[m2_]) / base[0] if base and m2_ in s else None)
                        for m2_ in tm]
                ax.plot(tm, vals, label="trends: " + nm[:30], alpha=0.7)
            hi = [A.get((m2_, HOME), {}).get("impr", 0) for m2_ in tm]
            b = next((v for v in hi if v), 1)
            ax.plot(tm, [v / b for v in hi], color="k", lw=2.5, label="acct HI impr (kpd)")
            ax.set_title("Account-independent demand vs our exposure (each normalized to first month)")
            ax.legend(fontsize=7)
            ax.tick_params(axis="x", labelrotation=75, labelsize=7)
            fig.tight_layout(); fig.savefig(os.path.join(cd, "trends_overlay.png"), dpi=150)
            plt.close(fig)
        print("charts written to %s" % cd)
    except Exception as e:
        print("chart layer failed (CSVs unaffected): %r" % e)

    with open(os.path.join(a.out_dir, "run_header.txt"), "w", encoding="utf-8") as fo:
        fo.write("\n".join(hdr) + "\n")
    conn.close()


if __name__ == "__main__":
    main()
