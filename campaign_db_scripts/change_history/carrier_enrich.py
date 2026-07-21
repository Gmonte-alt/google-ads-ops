"""
carrier_enrich.py -- keyword classification for the change log (un-numbered module).

Reuses the CANONICAL brand matcher (brand_matcher.BrandMatcher, extracted from
09_tag_keyword_identities.py) so the change log's carrier split reconciles WITH the
keyword analysis by construction -- same containment/word-boundary/longest-first logic,
same brand_registry_keywords variant table, same is_home_carrier rule.

Pipeline: extract added-keyword strings from grid prose -> bm.match(kw) -> classify.
The carrier decision is bm's is_home_carrier (brand_category LIKE 'carrier_%' AND home vertical).
"""
import re
import brand_matcher as BM   # canonical matcher; flat import, run in place

# header lines ("2 exact match keywords added") and annotation lines ("... : Label is ...",
# "Status changed", "Target ROAS is 120%") are NOT keywords -- skip them so we don't count
# sub-headers/metadata as keywords (the over-extraction bug the live run exposed).
_HEADER = re.compile(r"^\s*\d+\s+\S+.*\b(added|removed|paused|updated|created|increased|"
                     r"decreased|enabled|active|applied)\b", re.I)
_ANNOT = re.compile(r"\b(label is|status|max cp[cm]|target roas|delivery method|targeting "
                    r"setting|tracking template|extension type|bid strategy|headline|"
                    r"description|final url|is paused|is enabled)\b", re.I)


def extract_keywords(changes_raw):
    """Clean, de-duped list of added-keyword strings from a keyword-change cell.
    Skips every header/annotation line (not just the first); takes the text before a
    ':' label suffix; strips exact/phrase/negative punctuation."""
    seen, out = set(), []
    for ln in (changes_raw or "").replace("\u202f", " ").split("\n"):
        prefix = ln.split(":")[0].strip()
        if not prefix or _HEADER.search(prefix) or _ANNOT.search(prefix):
            continue
        kw = prefix.strip("[]\"'")
        kw = re.sub(r"^-+\s*", "", kw)                  # negative marker
        kw = re.sub(r"\s+", " ", kw).strip().lower()
        if kw and kw not in seen:
            seen.add(kw)
            out.append(kw)
    return out


def classify_match(m):
    """BrandMatch or None -> carrier | carrier_other | competitor | branded_other | generic."""
    if m is None:
        return "generic"
    if m.is_home_carrier:
        return "carrier"
    cat = m.brand_category or ""
    if cat.startswith("carrier_"):
        return "carrier_other"      # a carrier, but not home line-of-business (e.g. Aetna)
    if cat.startswith("aggregator"):
        return "competitor"
    return "branded_other"          # confie_subsidiary, state_residual_pool, membership, None, etc.


def to_bucket(label, three_way=True):
    """Management rollup. three_way: carrier | competitor | generic. binary: carrier | generic."""
    if label == "carrier":
        return "carrier"
    if three_way and label == "competitor":
        return "competitor"
    return "generic"
