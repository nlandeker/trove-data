#!/usr/bin/env python3
"""Offline self-checks for ingest.py pure logic.  Run: python3 test_ingest.py
No network, no API key required."""
# ponytail: plain asserts, no framework

from ingest import build_id_to_root_name, infer_retired, RETIRE_AGE_YEARS

# --- infer_retired (age-based retirement for Brickset-unclassified sets) ---
NOW = 2026
OLD = NOW - RETIRE_AGE_YEARS            # boundary: retired
RECENT = NOW - RETIRE_AGE_YEARS + 1     # one year newer: still available
assert infer_retired("AVAILABLE", False, OLD, NOW) == "RETIRED"       # old + no price
assert infer_retired("AVAILABLE", True, OLD, NOW) == "AVAILABLE"      # old but still sold
assert infer_retired("AVAILABLE", False, RECENT, NOW) == "AVAILABLE"  # too recent to assume
assert infer_retired("AVAILABLE", False, None, NOW) == "AVAILABLE"    # unknown year → leave
assert infer_retired("RETIRED", False, OLD, NOW) == "RETIRED"         # respect Brickset
assert infer_retired("RETIRING_SOON", False, OLD, NOW) == "RETIRING_SOON"  # respect Brickset


# Fixture: root → child → grandchild, a second root, and an orphan with a dangling parent.
# Mirrors real Rebrickable structure (e.g. Star Wars → UCS → Mandalorian sets).
THEMES = [
    {"id": 158, "name": "Star Wars",                "parent_id": None},  # root
    {"id": 171, "name": "Ultimate Collector Series", "parent_id": 158},  # child
    {"id": 300, "name": "The Mandalorian",           "parent_id": 171},  # grandchild
    {"id": 1,   "name": "Technic",                  "parent_id": None},  # second root
    {"id": 18,  "name": "Technic Subtheme",         "parent_id": 1},    # child of Technic
    {"id": 99,  "name": "Orphan",                   "parent_id": 9999}, # dangling parent
]

r = build_id_to_root_name(THEMES)

# Roots map to themselves
assert r.get(158) == "Star Wars",  "root maps to itself"
assert r.get(1)   == "Technic",    "second root maps to itself"

# Children and grandchildren map to their root
assert r.get(171) == "Star Wars",  "child maps to root"
assert r.get(300) == "Star Wars",  "grandchild maps to root"
assert r.get(18)  == "Technic",    "child of Technic maps to Technic"

# Dangling parent → omitted (callers default to "")
assert r.get(99, "") == "", "dangling parent → no entry"

# Self-referencing cycle must not loop
themes_self_cycle = THEMES + [{"id": 500, "name": "Cycle", "parent_id": 500}]
rc = build_id_to_root_name(themes_self_cycle)
assert rc.get(500, "") == "", "self-cycle → no entry"

# Mutual cycle (A→B→A) with no root → both omitted
themes_mutual = THEMES + [
    {"id": 600, "name": "A", "parent_id": 601},
    {"id": 601, "name": "B", "parent_id": 600},
]
rm = build_id_to_root_name(themes_mutual)
assert rm.get(600, "") == "" and rm.get(601, "") == "", "mutual cycle → no entries"

# Empty input returns empty map
assert build_id_to_root_name([]) == {}

# Single root with no children
r_solo = build_id_to_root_name([{"id": 7, "name": "Duplo", "parent_id": None}])
assert r_solo[7] == "Duplo", "solo root maps to itself"

print("OK")
