#!/usr/bin/env python3
"""
Trove ingest pipeline: Rebrickable + Brickset → catalog.json
Schema: { "version": N, "items": [...] }

Modes:
  --sample   use fixtures/*.json instead of live APIs (no keys needed)
  (default)  live mode; requires REBRICKABLE_API_KEY + BRICKSET_API_KEY env vars

Attribution: data from Rebrickable (rebrickable.com) and Brickset (brickset.com).
"""
# ponytail: stdlib HTTP only — no requests, no pandas, no third-party deps

import json
import os
import sys
import urllib.request
import urllib.parse
from pathlib import Path

HERE = Path(__file__).parent
FIXTURES = HERE / "fixtures"
OUT = HERE / "catalog.json"
VERSION = 2  # bump when schema changes

# ---------------------------------------------------------------------------
# Rebrickable helpers
# ---------------------------------------------------------------------------

RB_BASE = "https://rebrickable.com/api/v3/lego"

# Keep slim: only themes worth tracking for collectors
# ponytail: hardcoded allowlist beats downloading all 900 themes
TRACKED_THEMES = {
    "Architecture", "Botanical", "Art", "Icons", "Ideas",
    "Creator Expert", "Star Wars", "Harry Potter", "Marvel",
    "DC", "Technic", "Jurassic World", "Speed Champions",
    "Ninjago", "City", "LOTR",
}


def rb_get(path: str, key: str, params: dict | None = None) -> dict:
    p = dict(params or {})
    p["key"] = key
    url = f"{RB_BASE}{path}?{urllib.parse.urlencode(p)}"
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.loads(r.read())


def fetch_rb_sets(key: str) -> list[dict]:
    """Fetch currently-available + recently-retired sets in tracked themes."""
    sets: list[dict] = []
    for theme_name in TRACKED_THEMES:
        page = 1
        while True:
            data = rb_get("/sets/", key, {
                "theme_id": _theme_id(theme_name, key),
                "page": page,
                "page_size": 100,
                "min_year": 2018,  # ponytail: skip truly vintage; keeps payload small
            })
            sets.extend(data.get("results", []))
            if not data.get("next"):
                break
            page += 1
    return sets


# ponytail: theme-name → ID map populated lazily on first use
_THEME_ID_CACHE: dict[str, int] = {}


def _theme_id(name: str, key: str) -> int:
    if name not in _THEME_ID_CACHE:
        data = rb_get("/themes/", key, {"search": name, "page_size": 5})
        for t in data.get("results", []):
            if t["name"].lower() == name.lower():
                _THEME_ID_CACHE[name] = t["id"]
                break
    return _THEME_ID_CACHE.get(name, -1)


def normalize_rb_set(s: dict) -> dict:
    """Map Rebrickable set dict → partial Trove item dict."""
    set_num = s.get("set_num", "").rstrip("-1").replace("-", "")  # "10307-1" → "10307"
    # ponytail: strip the "-1" variant suffix RB appends
    if set_num.endswith("1") and "-" not in s.get("set_num", ""):
        set_num = s["set_num"].rsplit("-", 1)[0]
    else:
        set_num = s["set_num"].rsplit("-", 1)[0]

    return {
        "id": set_num,
        "category": "LEGO",
        "name": s.get("name", ""),
        "imageURL": s.get("set_img_url") or "",
        "themeOrSeries": s.get("theme_name", ""),
        "retailPrice": s.get("retail_price"),  # may be None
        "lifecycleStatus": "AVAILABLE",  # overridden by Brickset below
        "retirementDate": None,
        "marketPrice": None,
        "volumeCount": None,
        "ongoing": None,
    }


# ---------------------------------------------------------------------------
# Brickset helpers
# ---------------------------------------------------------------------------

BS_API = "https://brickset.com/api/v3.asmx/getSets"


def bs_get(params: dict) -> dict:
    url = f"{BS_API}?{urllib.parse.urlencode(params)}"
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.loads(r.read())


def fetch_bs_retiring(key: str) -> list[dict]:
    """Fetch sets flagged as retiring soon from Brickset."""
    data = bs_get({
        "apiKey": key,
        "userHash": "",
        "params": json.dumps({
            "theme": "",
            "year": "",
            "orderBy": "RetiredRecently",
            "pageSize": 500,
            "pageNumber": 1,
            "extendedData": True,
        }),
    })
    return data.get("sets", [])


def fetch_bs_retired_recent(key: str) -> list[dict]:
    """Fetch recently-retired sets from Brickset (last 2 years)."""
    data = bs_get({
        "apiKey": key,
        "userHash": "",
        "params": json.dumps({
            "theme": "",
            "year": "",
            "orderBy": "YearTo",
            "pageSize": 500,
            "pageNumber": 1,
            "extendedData": True,
        }),
    })
    return data.get("sets", [])


def bs_lifecycle(bs_set: dict) -> tuple[str | None, str | None]:
    """Return (lifecycleStatus, retirementDate) from a Brickset set dict."""
    availability = bs_set.get("availability", "") or ""
    eol = bs_set.get("dateAddedToSAH") or ""  # Brickset's end-of-availability hint

    date_str: str | None = None
    year_to = bs_set.get("yearTo") or ""

    # Brickset exposes retiredDate in extendedData
    retired_date = bs_set.get("dateRetired") or ""
    eoa_date = bs_set.get("dateAvailableToDate") or ""
    for candidate in (retired_date, eoa_date):
        if candidate:
            # Brickset dates arrive as ISO strings; normalise to yyyy-MM-dd
            date_str = candidate[:10]
            break

    if "Retired" in availability or year_to:
        return ("RETIRED", date_str)
    if "EOFY" in availability or "Retiring" in availability or "retiring" in availability.lower():
        return ("RETIRING_SOON", date_str)
    return (None, None)


# ---------------------------------------------------------------------------
# Sample / fixture mode
# ---------------------------------------------------------------------------

def load_fixtures() -> tuple[list[dict], list[dict]]:
    """Load pre-committed fixture files instead of calling live APIs."""
    rb_path = FIXTURES / "rebrickable_sets.json"
    bs_path = FIXTURES / "brickset_retiring.json"
    if not rb_path.exists() or not bs_path.exists():
        print("ERROR: fixtures not found. Run once with live keys first, or see README.", file=sys.stderr)
        sys.exit(1)
    rb_sets = json.loads(rb_path.read_text())
    bs_sets = json.loads(bs_path.read_text())
    return rb_sets, bs_sets


# ---------------------------------------------------------------------------
# Merge logic
# ---------------------------------------------------------------------------

def merge(rb_sets: list[dict], bs_sets: list[dict]) -> list[dict]:
    """Merge Rebrickable base data with Brickset lifecycle enrichment."""
    # Index BS by set number
    bs_index: dict[str, dict] = {}
    for s in bs_sets:
        num = str(s.get("number", ""))
        if num:
            bs_index[num] = s

    items: list[dict] = []
    seen: set[str] = set()

    for s in rb_sets:
        item = normalize_rb_set(s)
        set_id = item["id"]
        if set_id in seen:
            continue
        seen.add(set_id)

        bs = bs_index.get(set_id)
        if bs:
            status, date = bs_lifecycle(bs)
            if status:
                item["lifecycleStatus"] = status
            if date:
                item["retirementDate"] = date
            # Use Brickset retail price as fallback
            if item["retailPrice"] is None:
                item["retailPrice"] = bs.get("ukRetailPrice") or bs.get("usRetailPrice")

        items.append(item)

    # Also add BS-only retiring/retired sets not in our RB pull
    for num, bs in bs_index.items():
        if num in seen:
            continue
        status, date = bs_lifecycle(bs)
        if status in ("RETIRING_SOON", "RETIRED"):
            items.append({
                "id": num,
                "category": "LEGO",
                "name": bs.get("name", ""),
                "imageURL": bs.get("image", {}).get("imageURL") if isinstance(bs.get("image"), dict) else "",
                "themeOrSeries": bs.get("theme", ""),
                "retailPrice": bs.get("ukRetailPrice") or bs.get("usRetailPrice"),
                "lifecycleStatus": status,
                "retirementDate": date,
                "marketPrice": None,
                "volumeCount": None,
                "ongoing": None,
            })

    return items


def build_catalog(items: list[dict]) -> dict:
    return {
        "version": VERSION,
        "_attribution": "LEGO data: Rebrickable (rebrickable.com) + Brickset (brickset.com). Not affiliated with or endorsed by The LEGO Group.",
        "_generated": __import__("datetime").datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "items": items,
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    sample_mode = "--sample" in sys.argv

    if sample_mode:
        print("Running in --sample mode (fixtures only, no API calls)")
        rb_sets, bs_sets = load_fixtures()
    else:
        rb_key = os.environ.get("REBRICKABLE_API_KEY", "")
        bs_key = os.environ.get("BRICKSET_API_KEY", "")
        if not rb_key or not bs_key:
            print(
                "ERROR: REBRICKABLE_API_KEY and BRICKSET_API_KEY must be set (or pass --sample).",
                file=sys.stderr,
            )
            sys.exit(1)
        print("Fetching Rebrickable sets…")
        rb_sets = fetch_rb_sets(rb_key)
        print(f"  {len(rb_sets)} sets from Rebrickable")
        print("Fetching Brickset retiring/retired…")
        bs_retiring = fetch_bs_retiring(bs_key)
        bs_retired = fetch_bs_retired_recent(bs_key)
        bs_sets = bs_retiring + bs_retired
        print(f"  {len(bs_sets)} sets from Brickset")

    items = merge(rb_sets, bs_sets)
    catalog = build_catalog(items)
    OUT.write_text(json.dumps(catalog, indent=2, default=str))
    print(f"Wrote {len(items)} items → {OUT}")


if __name__ == "__main__":
    main()
