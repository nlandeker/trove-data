#!/usr/bin/env python3
"""Offline self-checks for ingest.py pure logic.  Run: python3 test_ingest.py
No network, no API key required."""
# ponytail: plain asserts, no framework

import tempfile
from pathlib import Path

import datetime as _dt

from ingest import (
    build_id_to_root_name, infer_retired, RETIRE_AGE_YEARS,
    parse_lego_retiring_html, apply_lego_retiring_soon, lego_retiring_step,
    load_overrides, apply_overrides, FIXTURES,
    load_retiring_cache, save_retiring_cache, cache_is_fresh,
    resolve_lego_retiring_soon, build_status, CACHE_MAX_AGE_DAYS,
    is_set_not_piece, MIN_PARTS,
)

# --- is_set_not_piece (set vs single-element piece/gear filter) ---
assert MIN_PARTS == 3
assert not is_set_not_piece(0)     # gear / zero-part promo
assert not is_set_not_piece(1)     # single printed brick
assert not is_set_not_piece(2)     # two-part promo — still a "piece", not a set
assert is_set_not_piece(3)         # boundary: smallest real set kept
assert is_set_not_piece(9036)      # Colosseum-scale
assert not is_set_not_piece(None)  # missing count (Brickset gear) → excluded

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

# --- LEGO.com retiring-soon parser + apply (fixture: fixtures/lego_retiring_soon.html) ---

lego_html = (FIXTURES / "lego_retiring_soon.html").read_text()
nums = parse_lego_retiring_html(lego_html)
assert nums == {"10316", "99999999"}, f"parser extracted wrong set numbers: {nums}"

sample_items = [
    {"id": "10316", "lifecycleStatus": "AVAILABLE", "retirementDate": None},
    {"id": "99999999", "lifecycleStatus": "RETIRED", "retirementDate": "2020-01-01"},
    {"id": "10273", "lifecycleStatus": "AVAILABLE", "retirementDate": None},
]
changed = apply_lego_retiring_soon(sample_items, nums)
assert changed == 1, f"expected exactly 1 promotion, got {changed}"
assert sample_items[0]["lifecycleStatus"] == "RETIRING_SOON", "matching AVAILABLE item promoted"
assert sample_items[0]["retirementDate"] is None, "retirementDate left null, not fabricated"
assert sample_items[1]["lifecycleStatus"] == "RETIRED", "already-RETIRED must not be touched"
assert sample_items[2]["lifecycleStatus"] == "AVAILABLE", "non-matching id left untouched"

# zero-result scrape (whether fetch failure or genuine 0 sets) must never modify statuses
zero_items = [{"id": "10316", "lifecycleStatus": "AVAILABLE", "retirementDate": None}]
result = lego_retiring_step(zero_items, set())
assert result == 0, "zero-result scrape must report 0 changes"
assert zero_items[0]["lifecycleStatus"] == "AVAILABLE", "zero-result scrape must not change statuses"


# --- overrides/retiring.json (manual escape hatch, authoritative) ---

with tempfile.TemporaryDirectory() as td:
    good_path = Path(td) / "retiring.json"
    good_path.write_text('{"10316": "2026-12-31", "10273": null}')
    overrides = load_overrides(good_path)
    assert overrides == {"10316": "2026-12-31", "10273": None}

    override_items = [
        {"id": "10316", "lifecycleStatus": "AVAILABLE", "retirementDate": None},
        {"id": "10273", "lifecycleStatus": "RETIRED", "retirementDate": "2025-01-01"},
        {"id": "99999", "lifecycleStatus": "AVAILABLE", "retirementDate": None},
    ]
    changed = apply_overrides(override_items, overrides)
    assert changed == 2
    assert override_items[0]["lifecycleStatus"] == "RETIRING_SOON", "override promotes"
    assert override_items[0]["retirementDate"] == "2026-12-31", "override sets date"
    assert override_items[1]["lifecycleStatus"] == "RETIRING_SOON", \
        "override is authoritative even over an existing RETIRED status"
    assert override_items[1]["retirementDate"] == "2025-01-01", \
        "null override date leaves existing retirementDate untouched"
    assert override_items[2]["lifecycleStatus"] == "AVAILABLE", "non-matching id untouched"

    # missing file -> skip silently
    assert load_overrides(Path(td) / "does_not_exist.json") == {}

    # malformed JSON -> loud nonzero exit (a typo must never silently ship a catalog
    # missing the overrides)
    bad_path = Path(td) / "bad.json"
    bad_path.write_text("{not valid json")
    try:
        load_overrides(bad_path)
        raise AssertionError("malformed overrides.json must sys.exit, not return")
    except SystemExit as e:
        assert e.code != 0, "malformed overrides.json must exit nonzero"

# --- last-known-good cache + status.json failsafes ---

NOW_DT = _dt.datetime(2026, 7, 3, 12, 0, 0, tzinfo=_dt.timezone.utc)

with tempfile.TemporaryDirectory() as td:
    cache_path = Path(td) / "cache" / "retiring_last_good.json"

    # missing cache -> empty, never fatal
    assert load_retiring_cache(cache_path) == {"scrapedAt": None, "setNums": []}

    # scrape succeeds -> cache is written (and dir created) with now + the set list
    fresh, ok, scraped_at = resolve_lego_retiring_soon({"10316", "10281"}, NOW_DT, cache_path)
    assert ok is True, "nonzero scrape reports scrape_ok=True"
    assert fresh == {"10316", "10281"}, "successful scrape used as-is"
    assert scraped_at == "2026-07-03T12:00:00Z"
    on_disk = load_retiring_cache(cache_path)
    assert on_disk["scrapedAt"] == "2026-07-03T12:00:00Z"
    assert on_disk["setNums"] == ["10281", "10316"], "cache stores sorted set list"

    # fresh cache (written moments ago) is reused on a subsequent scrape failure
    later = NOW_DT + _dt.timedelta(days=5)
    fallback_nums, ok2, scraped_at2 = resolve_lego_retiring_soon(set(), later, cache_path)
    assert ok2 is False, "fallback run reports scrape_ok=False"
    assert fallback_nums == {"10316", "10281"}, "fresh cache reused on scrape failure"
    assert scraped_at2 == "2026-07-03T12:00:00Z", "reports the cache's original scrapedAt"

    # cache_is_fresh boundary
    assert cache_is_fresh("2026-07-03T12:00:00Z", NOW_DT + _dt.timedelta(days=20)) is True
    assert cache_is_fresh("2026-07-03T12:00:00Z", NOW_DT + _dt.timedelta(days=22)) is False
    assert cache_is_fresh(None, NOW_DT) is False
    assert cache_is_fresh("not-a-date", NOW_DT) is False

    # stale cache (> CACHE_MAX_AGE_DAYS) is NOT reused — proceeds with zero, like no cache
    stale_at = NOW_DT + _dt.timedelta(days=CACHE_MAX_AGE_DAYS + 1)
    stale_nums, ok3, scraped_at3 = resolve_lego_retiring_soon(set(), stale_at, cache_path)
    assert ok3 is False
    assert stale_nums == set(), "stale cache must not be used"
    assert scraped_at3 is None

    # missing cache entirely + scrape failure -> zero sets, scrapedAt None (current behavior)
    no_cache_path = Path(td) / "cache_missing" / "retiring_last_good.json"
    none_nums, ok4, scraped_at4 = resolve_lego_retiring_soon(set(), NOW_DT, no_cache_path)
    assert ok4 is False and none_nums == set() and scraped_at4 is None

# --- status.json / degraded semantics ---

# healthy run: scrape ok, nonzero retiring-soon -> not degraded
s = build_status(scrape_ok=True, scraped_at="2026-07-03T12:00:00Z", retiring_count=5, degraded=False)
assert s["scrapeOK"] is True and s["retiringSoonCount"] == 5 and s["degraded"] is False
assert "generatedAt" in s and s["scrapedAt"] == "2026-07-03T12:00:00Z"

# degraded=True is caller-computed (used_cache OR zero retiring-soon everywhere) —
# verify both triggering conditions produce degraded status here.
fell_back = build_status(scrape_ok=False, scraped_at="2026-06-20T00:00:00Z", retiring_count=3,
                          degraded=True)
assert fell_back["degraded"] is True, "fallback-to-cache run must be flagged degraded"

zero_result = build_status(scrape_ok=True, scraped_at="2026-07-03T12:00:00Z", retiring_count=0,
                            degraded=True)
assert zero_result["degraded"] is True, "zero retiring-soon from all sources must be degraded"

print("OK")
