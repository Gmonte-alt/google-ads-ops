"""Run the categorizer against real grid prose (loaded from fixtures/). Run in place:
    cd change_history && python validate_categorizer.py"""
import collections
import os
import change_taxonomy as T

FIX = os.path.join(os.path.dirname(__file__) or ".", "fixtures", "tier_dump_real_prose.txt")
with open(FIX, encoding="utf-8") as f:
    REAL = [b for b in f.read().split("\n===\n") if b.strip()]

print("%-58s | %-20s | %-5s | %s" % ("PROSE (first line)", "category", "class", "cnt"))
print("-" * 96)
cat_tally, klass_tally, other = collections.Counter(), collections.Counter(), []
for p in REAL:
    r = T.categorize(p)
    print("%-58s | %-20s | %-5s | %d" % (p.split("\n")[0][:56], r["category"], r["klass"][:5], r["count"]))
    cat_tally[r["category"]] += 1; klass_tally[r["klass"]] += 1
    if r["category"] == "other":
        other.append(p.split("\n")[0][:56])

print("\n=== by category ==="); [print("  %-22s %d" % (c, n)) for c, n in cat_tally.most_common()]
print("\n=== by class (management headline) ==="); [print("  %-14s %d" % (k, n)) for k, n in klass_tally.most_common()]
print("\n=== fell through to 'other': %d ===" % len(other)); [print("  ", o) for o in other]
