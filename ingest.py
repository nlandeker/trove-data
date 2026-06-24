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

import datetime as _dt
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


def _urlopen_json(req, timeout: int = 30, attempts: int = 4) -> dict:
    """urlopen + json with retry/backoff. ponytail: covers transient TLS/network
    blips (e.g. SSL UNEXPECTED_EOF) that otherwise abort a whole theme/page."""
    last_err: Exception | None = None
    for i in range(attempts):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError:
            raise  # 4xx/5xx are real — don't mask them behind retries
        except Exception as e:  # noqa: BLE001 - transient transport errors only
            last_err = e
            time.sleep(1.5 * (i + 1))
    raise last_err  # type: ignore[misc]

HERE = Path(__file__).parent
FIXTURES = HERE / "fixtures"
OUT = HERE / "catalog.json"
VERSION = 2  # bump when schema changes

# Refuse to overwrite the published catalog with a near-empty result (a bad run
# must never wipe good data). ponytail: simple floor, not a diff-ratio check.
MIN_ITEMS = 5

# ---------------------------------------------------------------------------
# Rebrickable helpers
# ---------------------------------------------------------------------------

RB_BASE = "https://rebrickable.com/api/v3/lego"

# Keep slim: only themes worth tracking for collectors.
# ponytail: names must match Rebrickable's theme list exactly; unresolved names
# are skipped at runtime (no crash). Corrected to real RB names where they differ.
TRACKED_THEMES = {
    "Architecture", "Botanicals", "Icons", "Creator Expert",
    "Star Wars", "Harry Potter", "Marvel", "DC Comics Super Heroes",
    "Technic", "Jurassic World", "Speed Champions", "Ninjago",
    "City", "The Lord of the Rings",
}


def rb_get(path: str, key: str, params: dict | None = None) -> dict:
    p = dict(params or {})
    p["key"] = key
    url = f"{RB_BASE}{path}?{urllib.parse.urlencode(p)}"
    return _urlopen_json(urllib.request.Request(url))


def _load_theme_ids(key: str) -> dict[str, int]:
    """Build {lowercased theme name: id} from the full Rebrickable theme list.

    ponytail: one paginated pull, no per-name lookup — resolving names one at a
    time was fragile and a miss produced theme_id=-1 → HTTP 400 (the first crash).
    """
    ids: dict[str, int] = {}
    page = 1
    while page <= 10:  # ~500 themes; page_size 1000 gets them in one page
        try:
            data = rb_get("/themes/", key, {"page": page, "page_size": 1000})
        except Exception as e:  # noqa: BLE001 - one bad page shouldn't kill the run
            print(f"  WARN: theme list page {page} failed: {e}", file=sys.stderr)
            break
        for t in data.get("results", []):
            ids.setdefault((t.get("name") or "").lower(), t.get("id"))
        if not data.get("next"):
            break
        page += 1
    return ids


def fetch_rb_sets(key: str) -> list[dict]:
    """Fetch sets (year >= 2018) for each tracked theme that resolves to an id.

    Defensive: a failure on any single theme/page is logged and skipped, never fatal.
    """
    theme_ids = _load_theme_ids(key)
    print(f"  resolved {len(theme_ids)} Rebrickable themes")
    sets: list[dict] = []
    for theme_name in sorted(TRACKED_THEMES):
        tid = theme_ids.get(theme_name.lower())
        if not tid:
            print(f"  skip theme (no id): {theme_name}", file=sys.stderr)
            continue
        page = 1
        while page <= 20:  # cap pages per theme; bounds runtime + payload
            try:
                data = rb_get("/sets/", key, {
                    "theme_id": tid,
                    "page": page,
                    "page_size": 100,
                    "min_year": 2018,  # ponytail: skip vintage; keeps payload small
                })
            except Exception as e:  # noqa: BLE001
                print(f"  WARN: {theme_name} page {page} failed: {e}", file=sys.stderr)
                break
            for s in data.get("results", []):
                s["theme_name"] = theme_name  # RB /sets/ omits theme_name; inject it
                sets.append(s)
            if not data.get("next"):
                break
            page += 1
    return sets


def normalize_rb_set(s: dict) -> dict:
    """Map a Rebrickable set dict → partial Trove item dict."""
    raw = s.get("set_num", "")
    set_num = raw.rsplit("-", 1)[0] if "-" in raw else raw  # "10307-1" → "10307"
    return {
        "id": set_num,
        "category": "LEGO",
        "name": s.get("name", ""),
        "imageURL": s.get("set_img_url") or "",
        "themeOrSeries": s.get("theme_name", ""),
        "retailPrice": s.get("retail_price"),  # RB omits price; Brickset fills below
        "lifecycleStatus": "AVAILABLE",        # overridden by Brickset enrichment
        "retirementDate": None,
        "marketPrice": None,
        "volumeCount": None,
        "ongoing": None,
    }


# ---------------------------------------------------------------------------
# Brickset helpers
# ---------------------------------------------------------------------------

BS_API = "https://brickset.com/api/v3.asmx/getSets"


def bs_get(key: str, params: dict) -> dict:
    body = urllib.parse.urlencode({
        "apiKey": key,
        "userHash": "",
        "params": json.dumps(params),
    }).encode()
    # ponytail: POST — getSets accepts form-encoded POST and avoids long query URLs
    return _urlopen_json(urllib.request.Request(BS_API, data=body))


def fetch_bs_sets(key: str) -> list[dict]:
    """Pull recent-year sets with extended data so we can read LEGO.com availability.

    Brickset has no direct 'retiring soon' filter — we infer lifecycle from
    LEGOCom.dateLastAvailable. Defensive + logs the API status for diagnosis.
    """
    out: list[dict] = []
    this_year = _dt.date.today().year
    for year in range(this_year - 1, this_year + 1):  # last ~2 years
        page = 1
        while page <= 10:
            try:
                data = bs_get(key, {
                    "year": str(year),
                    "pageSize": 500,
                    "pageNumber": page,
                    "extendedData": 1,
                })
            except Exception as e:  # noqa: BLE001 - non-fatal; RB data still useful
                print(f"  WARN: Brickset year {year} page {page} failed: {e}", file=sys.stderr)
                break
            status = data.get("status")
            if status != "success":
                print(f"  WARN: Brickset year {year}: status={status} "
                      f"msg={data.get('message')}", file=sys.stderr)
                break
            sets = data.get("sets", [])
            out.extend(sets)
            if len(sets) < 500:
                break
            page += 1
    return out


def _bs_price_and_eol(bs: dict) -> tuple[float | None, str | None]:
    """Extract (retailPrice EUR, dateLastAvailable yyyy-MM-dd) from LEGOCom.

    ponytail: price comes ONLY from the DE region so every price is genuine EUR —
    the app shows a € symbol, so mixing in USD/GBP would be a lie. No FX conversion.
    The retirement date is currency-agnostic, so any region's date is fine.
    """
    lego = bs.get("LEGOCom") or {}
    de = lego.get("DE") or {}
    price = de.get("retailPrice")  # EUR only
    last: str | None = None
    for region in ("DE", "UK", "US"):
        d = (lego.get(region) or {}).get("dateLastAvailable")
        if d:
            last = str(d)[:10]
            break
    return price, last


def bs_lifecycle(bs: dict) -> tuple[str | None, str | None]:
    """Return (lifecycleStatus, retirementDate) from a Brickset set dict.

    Primary signal: LEGOCom.dateLastAvailable (past → RETIRED, future → RETIRING_SOON).
    Falls back to legacy fixture fields so --sample mode keeps working.
    """
    _, last = _bs_price_and_eol(bs)
    if last:
        today = _dt.date.today().isoformat()
        return ("RETIRED", last) if last <= today else ("RETIRING_SOON", last)

    # Legacy fixture shape (offline --sample fixtures predate the LEGOCom parse).
    availability = (bs.get("availability") or "")
    retired_date = bs.get("dateRetired") or bs.get("dateAvailableToDate") or ""
    date_str = retired_date[:10] if retired_date else None
    if "Retired" in availability or bs.get("yearTo"):
        return ("RETIRED", date_str)
    if "retiring" in availability.lower() or "EOFY" in availability:
        return ("RETIRING_SOON", date_str)
    return (None, None)


def _bs_image(bs: dict) -> str:
    img = bs.get("image")
    return img.get("imageURL", "") if isinstance(img, dict) else ""


# ---------------------------------------------------------------------------
# Sample / fixture mode
# ---------------------------------------------------------------------------

def load_fixtures() -> tuple[list[dict], list[dict]]:
    """Load pre-committed fixture files instead of calling live APIs."""
    rb_path = FIXTURES / "rebrickable_sets.json"
    bs_path = FIXTURES / "brickset_retiring.json"
    if not rb_path.exists() or not bs_path.exists():
        print("ERROR: fixtures not found. See README.", file=sys.stderr)
        sys.exit(1)
    return json.loads(rb_path.read_text()), json.loads(bs_path.read_text())


# ---------------------------------------------------------------------------
# Merge logic
# ---------------------------------------------------------------------------

def merge(rb_sets: list[dict], bs_sets: list[dict]) -> list[dict]:
    """Merge Rebrickable base data with Brickset lifecycle enrichment."""
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
        if not set_id or set_id in seen:
            continue
        seen.add(set_id)

        bs = bs_index.get(set_id)
        if bs:
            status, date = bs_lifecycle(bs)
            if status:
                item["lifecycleStatus"] = status
            if date:
                item["retirementDate"] = date
            if item["retailPrice"] is None:
                price, _ = _bs_price_and_eol(bs)
                item["retailPrice"] = price
        items.append(item)

    # Add Brickset-only retiring/retired sets not in our Rebrickable pull.
    for num, bs in bs_index.items():
        if num in seen:
            continue
        status, date = bs_lifecycle(bs)
        if status in ("RETIRING_SOON", "RETIRED"):
            price, _ = _bs_price_and_eol(bs)
            items.append({
                "id": num,
                "category": "LEGO",
                "name": bs.get("name", ""),
                "imageURL": _bs_image(bs),
                "themeOrSeries": bs.get("theme", ""),
                "retailPrice": price,
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
        "currency": "EUR",  # all retailPrice values are euros (Brickset DE region)
        "_attribution": "LEGO data: Rebrickable (rebrickable.com) + Brickset "
                        "(brickset.com). Not affiliated with or endorsed by The LEGO Group.",
        "_generated": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
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
            print("ERROR: REBRICKABLE_API_KEY and BRICKSET_API_KEY must be set "
                  "(or pass --sample).", file=sys.stderr)
            sys.exit(1)

        print("Fetching Rebrickable sets…")
        rb_sets = fetch_rb_sets(rb_key)
        print(f"  {len(rb_sets)} sets from Rebrickable")

        print("Fetching Brickset (lifecycle enrichment)…")
        try:
            bs_sets = fetch_bs_sets(bs_key)
        except Exception as e:  # noqa: BLE001 - Brickset failure must not lose RB data
            print(f"  WARN: Brickset fetch failed entirely: {e}", file=sys.stderr)
            bs_sets = []
        print(f"  {len(bs_sets)} sets from Brickset")

    items = merge(rb_sets, bs_sets)

    if not sample_mode and len(items) < MIN_ITEMS:
        print(f"ERROR: only {len(items)} items produced — refusing to overwrite the "
              f"published catalog (floor is {MIN_ITEMS}).", file=sys.stderr)
        sys.exit(1)

    retiring = sum(1 for i in items if i["lifecycleStatus"] in ("RETIRING_SOON", "RETIRED"))
    catalog = build_catalog(items)
    OUT.write_text(json.dumps(catalog, indent=2, default=str))
    print(f"Wrote {len(items)} items ({retiring} with retirement status) → {OUT}")


if __name__ == "__main__":
    main()
