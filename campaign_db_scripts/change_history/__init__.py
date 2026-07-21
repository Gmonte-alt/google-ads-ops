"""
change_history -- the campaign change-history subsystem.

Pipeline (run in sequence):
    14_migrate_change_tables.py          -- create/migrate the raw + derived tables
    15_..._csv_load.py                   -- load the Google UI grid CSV export (google_change_csv_raw)
    16_derive_campaign_name_history.py   -- SCD2 campaign-name intervals (rename-aware)
    17_derive_campaign_change_events.py  -- dual-source (CSV + API) categorized change events
    18_pull_google_change_events_api.py  -- weekly Google change_event API pull (id-native)

Modules / utilities (imported, not run as pipeline stages):
    change_taxonomy.py       -- prose categorizer + vertical derivation + carrier rule
    validate_categorizer.py  -- test harness: runs change_taxonomy against real grid prose
    gads_change_common.py    -- shared helpers for the pipeline scripts

Convention: numbered files = pipeline stages (run in order); un-numbered files = modules/tests/helpers.
"""

# Convenience re-exports so consumers can `from change_history import categorize, derive_vertical`.
# Wrapped in try/except so importing the package never hard-fails if a module is mid-edit or
# a dependency is missing -- the package still imports; the names just won't be present.
try:
    from .change_taxonomy import (
        categorize,
        derive_vertical,
        is_home_carrier,
    )
except Exception:  # pragma: no cover -- keep the package importable even if the module isn't ready
    pass

__all__ = ["categorize", "derive_vertical", "is_home_carrier"]
