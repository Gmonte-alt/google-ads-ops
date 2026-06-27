# reconcile_vertical_match.py  (dry-run by default, --apply to commit)
#
# One-time reconciliation for rows already in kwp_ideas. The classify_vertical change in
# usia_kwp_pull.py only affects NEW pulls; existing stored rows keep their stale 'mixed'.
# retag_demand_research.py does NOT touch vertical_match (it only rewrites brand/bid/cat/dnb),
# so this dedicated pass is needed.
#
# It updates ALL rows per keyword (not GROUP BY representative), because the same keyword can
# currently have rows that disagree (e.g. 52 Geico/mixed + 2 ''/on_vertical). A representative
# approach has a blind spot: if the representative already matches, disagreeing duplicates never
# converge. Updating every row guarantees all duplicates of a keyword agree.
import argparse, os, sqlite3, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from usia_kwp_pull import classify_vertical  # reuse the SAME logic as the pull

DEMAND_DB = r"C:\Projects\google-ads-ops\db\demand_research.db"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--vertical", default="home")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    conn = sqlite3.connect(DEMAND_DB)

    # Schema guard: this pass writes vertical_match / would_exclude / detected_verticals.
    # A missing column otherwise surfaces only on the first write (the dry-run row count
    # would look fine, then --apply throws). Fail loudly up front instead.
    have = {r[1] for r in conn.execute("PRAGMA table_info(kwp_ideas)")}
    need = {"vertical_match", "would_exclude", "detected_verticals"}
    missing = need - have
    if missing:
        sys.exit("ABORT: kwp_ideas is missing column(s): %s. Add them before running "
                 "(they're in the puller's CSV_FIELDS, so a fresh pull would create "
                 "them). No changes made." % ", ".join(sorted(missing)))

    kws = [r[0] for r in conn.execute(
        "SELECT DISTINCT keyword_text FROM kwp_ideas WHERE vertical=?", (a.vertical,))]
    changed = 0
    for kw in kws:
        match, would_excl, detected = classify_vertical(a.vertical, kw)
        # update ALL rows for this keyword (converges disagreeing duplicates)
        cur = conn.execute(
            "UPDATE kwp_ideas SET vertical_match=?, would_exclude=?, detected_verticals=? "
            "WHERE keyword_text=? AND vertical=? AND "
            "(vertical_match IS NOT ? OR would_exclude IS NOT ? OR detected_verticals IS NOT ?)",
            (match, int(would_excl), detected, kw, a.vertical, match, int(would_excl), detected))
        changed += cur.rowcount
    print("rows that %s change: %d" % ("would" if not a.apply else "did", changed))
    if a.apply:
        conn.commit(); print("APPLIED.")
    else:
        print("DRY-RUN: nothing written. Re-run with --apply.")
    conn.close()


if __name__ == "__main__":
    main()
