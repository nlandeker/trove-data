#!/usr/bin/env python3
"""Offline self-checks for manga_ingest.py pure logic. Run: python3 test_manga_ingest.py"""
from manga_ingest import parse_infobox_volumes, title_matches

# --- parse_infobox_volumes: reads `| volumes = N`, ignores everything else ---
assert parse_infobox_volumes("{{Infobox animanga/Print\n| volumes = 114\n}}") == 114
assert parse_infobox_volumes("|volumes=43<br/>{{Cite}}") == 43
assert parse_infobox_volumes("| volumes = 17 ([[List of ...|list]])") == 17
assert parse_infobox_volumes("| chapters = 1000") is None   # not the volumes field
assert parse_infobox_volumes("") is None
assert parse_infobox_volumes(None) is None

# --- title_matches: guards against confident-but-wrong article picks ---
assert title_matches("One Piece", "One Piece")
assert title_matches("Berserk", "Berserk (manga)")
assert title_matches("Spy x Family", "Spy × Family")           # punctuation stripped
assert not title_matches("Chainsaw Man", "Fire Punch")          # different series
assert not title_matches("", "Anything")

print("OK")
