#!/usr/bin/env python3
"""Offline self-checks for ingest.py pure logic.  Run: python3 test_ingest.py
No network, no API key required."""
# ponytail: plain asserts, no framework

from ingest import build_id_to_top_name

# Real-shaped fixture: Rebrickable has FOUR themes named "Star Wars".
# The real one (158) is a root; the sets live under it and its subtheme UCS (171).
# The other three "Star Wars" nodes sit under unrelated parents.
THEMES = [
    {"id": 158, "name": "Star Wars",                "parent_id": None},   # the real root
    {"id": 171, "name": "Ultimate Collector Series", "parent_id": 158},   # holds the Falcon
    {"id": 300, "name": "The Mandalorian",          "parent_id": 171},    # deeper nesting
    {"id": 18,  "name": "Star Wars",                "parent_id": 1},      # a Technic sub-line
    {"id": 1,   "name": "Technic",                  "parent_id": None},
    {"id": 99,  "name": "Unrelated",                "parent_id": None},
]

# --- build_id_to_top_name: lineage assignment ---
m = build_id_to_top_name({"Star Wars", "Technic"}, THEMES)
assert m.get(158) == "Star Wars", "root Star Wars must map to itself"
assert m.get(171) == "Star Wars", "UCS (child of Star Wars) must map to Star Wars"
assert m.get(300) == "Star Wars", "deep descendant must map to Star Wars"
assert m.get(1) == "Technic", "Technic root maps to itself"
# id 18 is itself named 'Star Wars' though parented under Technic — nearest tracked
# ancestor is SELF, so it maps to Star Wars (not Technic).
assert m.get(18) == "Star Wars", "self-named tracked theme wins over parent"
assert 99 not in m, "unrelated theme must not appear"

# unresolved tracked theme name returns empty map (no crash)
assert build_id_to_top_name({"NonExistent Theme"}, THEMES) == {}

# cycle guard: self-referencing theme must not loop
themes_cycle = THEMES + [{"id": 500, "name": "Cycle", "parent_id": 500}]
mc = build_id_to_top_name({"Star Wars"}, themes_cycle)
assert 500 not in mc  # not tracked, terminates without looping

# mutual-cycle guard: A→B→A, neither tracked → terminates, no entry
themes_mutual = THEMES + [
    {"id": 600, "name": "A", "parent_id": 601},
    {"id": 601, "name": "B", "parent_id": 600},
]
mm = build_id_to_top_name({"Star Wars"}, themes_mutual)
assert 600 not in mm and 601 not in mm

print("OK")
