#!/usr/bin/env python
"""
brand_tagger.py -- shared brand / DNB tagging for the USIA demand pipeline.

Extracted from usia_kwp_pull.py so the puller AND retag_demand_research.py apply
identical tagging logic against paid_search.db's brand_registry. This module has no
Google Ads dependency, so it is import-safe anywhere (e.g. the re-tag pass, which
must not require the API client).

Tag, never bid: tagging classifies a keyword's brand + bid_status + a do-not-bid
flag; it does not decide match type or selection.
"""
import os
import re

# Mirrors the NON_BIDDABLE_OVERRIDE pattern in the 08-11 tagger pipeline.
NON_BIDDABLE_OVERRIDE = ["lemonade", "tgs insurance", "tgs"]


def normalize_keyword(text):
    """Placeholder normalizer. VERIFY/ALIGN to the 07-11 pipeline before DB writer."""
    t = text.lower().strip()
    t = re.sub(r"\s+", " ", t)
    return t


def normalize_for_match(text):
    """Lowercase, drop apostrophes, non-alnum -> space, collapse. For token matching only."""
    t = text.lower().replace("\u2019", "").replace("'", "")
    t = re.sub(r"[^a-z0-9]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


class BrandTagger:
    def __init__(self, brand_db_path=None):
        self.exact = {}    # normalized variant   -> normalized_name
        self.meta = {}     # normalized_name      -> (brand_name, bid_status, category, verticals)
        self.embedded_re = None
        self.sources = []
        self._override_res = [re.compile(r"\b" + re.escape(p) + r"\b")
                              for p in NON_BIDDABLE_OVERRIDE]
        if brand_db_path and os.path.exists(brand_db_path):
            try:
                self._load_registry(brand_db_path)
                self.sources.append("registry (%d brands, %d variants)"
                                    % (len(self.meta), len(self.exact)))
            except Exception as e:
                print("  [warn] brand registry load failed (%s); DNB override only." % e)
        self._build_embedded()
        if NON_BIDDABLE_OVERRIDE:
            self.sources.append("dnb-override: %s" % ", ".join(NON_BIDDABLE_OVERRIDE))

    def _load_registry(self, path):
        import sqlite3
        conn = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        id_to_norm = {}
        for r in cur.execute("SELECT id, brand_name, normalized_name, bid_status, "
                             "brand_category, verticals FROM brand_registry"):
            self.meta[r["normalized_name"]] = (r["brand_name"], r["bid_status"],
                                               r["brand_category"], r["verticals"])
            id_to_norm[r["id"]] = r["normalized_name"]
        for r in cur.execute("SELECT brand_id, normalized FROM brand_registry_keywords"):
            nm = id_to_norm.get(r["brand_id"])
            if nm:
                self.exact[r["normalized"]] = nm
        conn.close()

    def _build_embedded(self):
        # Canonical names + override phrases, len>=4, longest-first so
        # "american family" wins over "family". Confie subsidiaries are EXCLUDED
        # from embedded matching: their generic-word names ("most insurance",
        # "insurance now", "capital insurance") need the yaml disqualifier /
        # require_context guards, which the registry update deliberately did NOT
        # load. They'll tag via the variant table once confie_negkw.py reconciles
        # the guards. Override phrases (Lemonade/TGS) are always included.
        safe = [nm for nm, meta in self.meta.items()
                if (meta[2] or "") != "confie_subsidiary"]
        phrases = set(safe) | set(NON_BIDDABLE_OVERRIDE)
        phrases = sorted((p for p in phrases if len(p) >= 4), key=len, reverse=True)
        if phrases:
            self.embedded_re = re.compile(
                r"\b(?:" + "|".join(re.escape(p) for p in phrases) + r")\b")

    def tag(self, norm_kw):
        """Return (brand, bid_status, category, dnb_flag, source)."""
        brand = bid = cat = source = ""
        nm = self.exact.get(norm_kw)
        if nm:
            brand, bid, cat, _ = self.meta.get(nm, (nm, "", "", ""))
            source = "registry_exact"
        elif self.embedded_re:
            mm = self.embedded_re.search(norm_kw)
            if mm:
                ph = mm.group(0)
                if ph in self.meta:
                    brand, bid, cat, _ = self.meta[ph]
                    source = "registry_embedded"
                else:
                    brand, source = ph, "embedded"
        # NON_BIDDABLE_OVERRIDE forces restricted regardless of registry status
        ovr = next((p for p, rx in zip(NON_BIDDABLE_OVERRIDE, self._override_res)
                    if rx.search(norm_kw)), None)
        if ovr:
            if not brand:
                brand = ovr
            bid = "restricted"
            source = (source + "+override") if source else "override"
        return brand, bid, cat, (bid == "restricted"), source
